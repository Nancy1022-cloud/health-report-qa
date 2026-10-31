"""当前数据库只读质量检查；输出规则和计数，不输出报告正文。"""
import hashlib

from psycopg.rows import dict_row
from pydantic import ValidationError

from app.domain_errors import BusinessError
from app.chunking import VERSIONS
from app.data_quality import fingerprint, review_issues
from app.repositories.reports import existing_review


def audit(connection_factory, drafts=None):
    findings=[]
    def issue(report,rule,severity='error'):
        findings.append({'report_id':report,'rule':rule,'severity':severity})
    with connection_factory() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.row_factory=dict_row
        reports=conn.execute("SELECT * FROM reports WHERE status='confirmed' ORDER BY id").fetchall()
        for report in reports:
            rid=report['id'];version=report['data_version']
            previous=None
            history=conn.execute('SELECT * FROM report_revisions WHERE report_id=%s ORDER BY version',(rid,)).fetchall()
            if [r['version'] for r in history]!=list(range(1,version+1)):
                issue(rid,'continuous_versions')
            for row in history:
                if row['member_id']!=report['member_id']:issue(rid,'revision_member_scope')
                if row['snapshot_sha256']!=fingerprint(row['snapshot']):issue(rid,'snapshot_hash')
                if row['previous_sha256']!=previous:issue(rid,'revision_hash_chain')
                previous=row['snapshot_sha256']
            try:
                review=existing_review(conn,rid,report['member_id'])
                for found in review_issues(review):issue(rid,found['rule'],found['severity'])
                if not history or review.canonical()!=history[-1]['snapshot']:issue(rid,'projection_matches_snapshot')
            except ValidationError:
                issue(rid,'required_date_duplicate_or_source_rule')
            if report['index_state']!='ready' or report['index_version']!=version:issue(rid,'index_ready_version')
            chunks=conn.execute('SELECT *,vector_dims(embedding) AS actual_dimensions FROM report_chunks WHERE report_id=%s',(rid,)).fetchall()
            if {c['chunk_version'] for c in chunks}!=set(VERSIONS.values()):issue(rid,'both_chunk_strategies_present')
            sha=hashlib.sha256(report['raw_text'].encode()).hexdigest()
            for chunk in chunks:
                if chunk['data_version']!=version:issue(rid,'current_chunk_version')
                if chunk['source_sha256']!=sha or chunk['content']!=report['raw_text'][chunk['start_char']:chunk['end_char']]:issue(rid,'chunk_matches_current_text')
                if chunk['embedding'] is None or chunk['actual_dimensions']!=chunk['embedding_dimensions']:issue(rid,'embedding_dimensions')
                if conn.execute('SELECT 1 FROM revision_sources WHERE source_id=%s AND report_id=%s AND version=%s',
                                (chunk['id'],rid,version)).fetchone() is None:issue(rid,'chunk_source_archived')
            for metric in conn.execute('SELECT id,data_version FROM metrics WHERE report_id=%s',(rid,)).fetchall():
                if metric['data_version']!=version:issue(rid,'current_metric_version')
                if conn.execute('SELECT 1 FROM revision_sources WHERE source_id=%s AND report_id=%s AND version=%s',
                                (f"metric:{metric['id']}",rid,version)).fetchone() is None:issue(rid,'metric_source_archived')
            source=conn.execute('SELECT * FROM source_uploads WHERE id=%s AND member_id=%s',(rid,report['member_id'])).fetchone()
            if source is None and report['source_type']=='uploaded':issue(rid,'legacy_original_extraction_not_registered','warning')
        sources=conn.execute('SELECT id,member_id,file_sha256 FROM source_uploads').fetchall()
        if drafts:
            for source in sources:
                try:
                    draft,_=drafts.load(source['id'],source['member_id'])
                    if draft['file_sha256']!=source['file_sha256']:issue(source['id'],'original_metadata_hash')
                except (OSError,ValueError,KeyError,BusinessError):
                    issue(source['id'],'original_file_available_and_unchanged')
    errors=sum(f['severity']=='error' for f in findings)
    return {'passed':errors==0,'reports_checked':len(reports),'registered_uploads_checked':len(sources),
            'error_count':errors,'warning_count':len(findings)-errors,'findings':findings,
            'clinical_reference_range_used_for_deletion':False,'external_model_calls':0}
