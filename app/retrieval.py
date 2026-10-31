"""同一数据库快照内核验索引并执行双通道检索，返回报告版本。"""
import hashlib
from fastapi import HTTPException
from psycopg.rows import dict_row
from app.chunking import VERSIONS
from app.db import connect_db
from app.rag import embed_texts, embedding_config, select_reports, vector_literal
from app.rrf import CANDIDATE_K, RRF_K, fuse
from app.text_search import SEARCH_VERSION, query_expression, query_terms

FIELDS = ("c.id AS source_id, c.report_id, c.chunk_index, c.section, c.content, "
          "c.start_char, c.end_char, c.chunk_version, c.data_version AS report_version, r.member_id, r.title, r.report_date")
SCOPE = ("r.member_id=%s AND r.status='confirmed' AND r.id=ANY(%s::text[]) "
         "AND c.chunk_version=%s AND c.data_version=r.data_version AND r.index_version=r.data_version AND r.index_state='ready'")


def _normalize(rows):
    for row in rows:
        row['report_date'] = row['report_date'].isoformat()
        for field in ('distance', 'keyword_score'):
            if field in row:
                row[field] = float(row[field])
    return rows


def retrieve(question, member_id, report_ids, top_k, trace, usage, *,
             chunk_version='fixed200o40', retrieval_mode='vector'):
    if chunk_version not in VERSIONS.values() or retrieval_mode not in {'vector','keyword','hybrid'}:
        raise ValueError('未知切分版本或检索模式。')
    if type(top_k) is not int or not 1 <= top_k <= 8:
        raise ValueError('top_k 必须在1到8之间。')
    ids = select_reports(member_id, report_ids)
    trace.append({'step':'scope','member_id':member_id,'report_ids':ids})
    if not ids:
        return []
    model, dimensions = embedding_config()
    query_vector = None
    # 外部调用不占用数据库快照事务。
    if retrieval_mode in {'vector','hybrid'}:
        trace.append({'step':'embedding_request'})
        vectors, usage['embedding'] = embed_texts([question])
        query_vector = vector_literal(vectors[0])
        trace.append({'step':'query_embedding','model':model,'dimensions':dimensions})
    terms = query_terms(question) if retrieval_mode in {'keyword','hybrid'} else []
    vector_rows, keyword_rows = [], []
    with connect_db() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.row_factory = dict_row
        checks = conn.execute(
            "SELECT r.id,r.raw_text,r.data_version,r.index_version,r.index_state,count(c.id) AS chunks, "
            "count(c.id) FILTER (WHERE c.embedding IS NULL OR c.embedding_model IS DISTINCT FROM %s "
            "OR c.embedding_dimensions IS DISTINCT FROM %s) AS bad_embeddings, "
            "count(c.id) FILTER (WHERE c.search_vector IS NULL OR c.search_version IS DISTINCT FROM %s) AS bad_keywords, "
            "array_agg(DISTINCT c.source_sha256) AS hashes FROM reports r LEFT JOIN report_chunks c "
            "ON c.report_id=r.id AND c.chunk_version=%s AND c.data_version=r.data_version "
            "WHERE r.member_id=%s AND r.status='confirmed' AND r.id=ANY(%s::text[]) GROUP BY r.id",
            (model,dimensions,SEARCH_VERSION,chunk_version,member_id,ids)).fetchall()
        valid = len(checks) == len(ids)
        for row in checks:
            digest = hashlib.sha256(row['raw_text'].encode()).hexdigest()
            valid &= row['chunks'] > 0 and row['hashes'] == [digest]
            valid &= row['index_state']=='ready' and row['index_version']==row['data_version']
            if retrieval_mode in {'vector','hybrid'}:
                valid &= row['bad_embeddings']==0
            if retrieval_mode in {'keyword','hybrid'}:
                valid &= row['bad_keywords']==0
        if not valid:
            raise HTTPException(409,'索引缺失、过期或配置不一致，请使用当前版本重建索引。')
        trace.append({'step':'report_versions','versions':{r['id']:r['data_version'] for r in checks}})
        if query_vector is not None:
            vector_rows = conn.execute('SELECT '+FIELDS+', c.embedding <=> %s::vector AS distance '
                'FROM report_chunks c JOIN reports r ON r.id=c.report_id WHERE '+SCOPE+
                ' AND c.embedding_model=%s AND c.embedding_dimensions=%s ORDER BY distance,c.id LIMIT %s',
                (query_vector,member_id,ids,chunk_version,model,dimensions,CANDIDATE_K)).fetchall()
            _normalize(vector_rows)
            trace.append({'step':'vector_candidates','limit':CANDIDATE_K,'items':vector_rows})
        if retrieval_mode in {'keyword','hybrid'}:
            trace.append({'step':'keyword_query','tokens':terms,'search_version':SEARCH_VERSION})
            if terms:
                keyword_rows = conn.execute("WITH q AS (SELECT to_tsquery('simple',%s) AS query) SELECT "+FIELDS+
                    ', ts_rank_cd(c.search_vector,q.query,32) AS keyword_score FROM report_chunks c '
                    'JOIN reports r ON r.id=c.report_id CROSS JOIN q WHERE '+SCOPE+
                    ' AND c.search_version=%s AND c.search_vector @@ q.query ORDER BY keyword_score DESC,c.id LIMIT %s',
                    (query_expression(terms),member_id,ids,chunk_version,SEARCH_VERSION,CANDIDATE_K)).fetchall()
                _normalize(keyword_rows)
            trace.append({'step':'keyword_candidates','limit':CANDIDATE_K,'items':keyword_rows})
    rows = fuse(vector_rows,keyword_rows,top_k) if retrieval_mode=='hybrid' else [
        dict(r) for r in (vector_rows if retrieval_mode=='vector' else keyword_rows)[:top_k]]
    for rank,row in enumerate(rows,1):
        row['rank']=rank
    trace.append({'step':'retrieval','retrieval_mode':retrieval_mode,'chunk_version':chunk_version,
                  'top_k':top_k,'candidate_k':CANDIDATE_K,'rrf_k':RRF_K if retrieval_mode=='hybrid' else None,'evidence':rows})
    return rows
