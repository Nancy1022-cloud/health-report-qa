"""真实 PostgreSQL/pgvector 隔离 schema；合成图片和受控模型；禁止外部模型。"""
import copy
import hashlib
import importlib
import io
import json
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

TEXT='空腹血糖：5.7 mmol/L；参考范围：3.9-6.1。'
BODY={'title':'M10合成报告','report_date':'2026-10-30','report_type':'体检报告','raw_text':TEXT,'reviewed':True,
      'metrics':[{'metric_code':'fasting_glucose','metric_name':'空腹血糖','value':'5.7','unit':'mmol/L',
                  'reference_range':'3.9-6.1','source_text':TEXT}]}
TABLES=['family_members','reports','metrics','report_chunks','query_logs','source_uploads','report_revisions','revision_sources','idempotency_records']


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


@contextmanager
def isolated(root,output):
    sys.path.insert(0,str(root))
    import psycopg
    from psycopg import sql
    from app import db,report_extract,report_ingest
    from app.migrate_m10 import migrate
    base=db.connect_db
    schema='m10_probe_'+uuid4().hex
    created=False
    dimensions=int(os.getenv('EMBEDDING_DIMENSIONS','1024'))
    env={'base':base,'schema':schema,'temporary_schema_cleaned':False,'calls':{'extraction':0,'embedding':0},'database':'real_postgresql_isolated_schema'}
    def scoped():
        conn=base()
        try:
            conn.execute(sql.SQL('SET search_path TO {},public').format(sql.Identifier(schema)))
            return conn
        except BaseException:
            conn.close();raise
    env['connect']=scoped
    try:
        with base() as conn:
            if conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone() is None:raise RuntimeError('当前数据库没有 pgvector')
            env['server_version']=conn.execute('SHOW server_version').fetchone()[0]
            env['pgvector_version']=conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created=True
        with tempfile.TemporaryDirectory(prefix='m10_files_') as temporary,ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'UPLOAD_DIR':temporary,'EMBEDDING_MODEL':'controlled-m10',
                'EMBEDDING_DIMENSIONS':str(dimensions),'LLM_MODEL':'controlled-m10','VISION_MODEL':'controlled-m10','M9_LOG_PATH':''}))
            for name in ['db','rag','retrieval','agent','agent_tools','report_ingest','dependencies','migrate_m3']:
                module=importlib.import_module('app.'+name)
                if hasattr(module,'connect_db'):stack.enter_context(patch.object(module,'connect_db',side_effect=scoped))
            def fields(*_):
                env['calls']['extraction']+=1
                return copy.deepcopy({k:v for k,v in BODY.items() if k!='reviewed'}),{'model':'controlled','usage':{}}
            def embed(texts):
                env['calls']['embedding']+=1
                return [[1.]+[0.]*(dimensions-1) for _ in texts],{}
            stack.enter_context(patch.object(report_extract,'request_fields',side_effect=fields))
            for name in ['rag','retrieval','report_ingest']:
                stack.enter_context(patch.object(importlib.import_module('app.'+name),'embed_texts',side_effect=embed))
            stack.enter_context(patch('app.llm.OpenAI',side_effect=AssertionError('M10探针禁止外部模型调用')))
            stack.enter_context(patch('app.agent.AsyncOpenAI',side_effect=AssertionError('M10探针禁止外部模型调用')))
            with scoped() as conn:conn.execute((root/'app/schema.sql').read_text(encoding='utf-8-sig'))
            importlib.import_module('app.migrate_m3').migrate()
            migrate(scoped)
            with scoped() as conn:conn.execute("INSERT INTO family_members(id,name) VALUES ('a','合成甲'),('b','合成乙')")
            env['uploads']=temporary
            yield env
    finally:
        if created:
            try:
                with base() as conn:conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                env['temporary_schema_cleaned']=True
            except Exception as exc:
                env['cleanup_error_type']=type(exc).__name__
        write(output/'environment.json',{k:v for k,v in env.items() if k not in {'base','connect','uploads'}})


def state(connect):
    from psycopg import sql
    with connect() as conn:
        return {table:conn.execute(sql.SQL("SELECT count(*),md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'[]')) FROM {} t").format(sql.Identifier(table))).fetchone()
                if table in {'family_members','reports','metrics','report_chunks','query_logs','source_uploads'} else
                conn.execute(sql.SQL("SELECT count(*),md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]')) FROM {} t").format(sql.Identifier(table))).fetchone() for table in TABLES}


def execute(root,output):
    sys.path.insert(0,str(root))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from PIL import Image
    from psycopg import sql
    from psycopg.rows import dict_row
    from app import upload_routes,record_routes,query_routes,report_ingest
    from app.api_errors import install_api_contract
    from app.data_audit import audit
    from app.repositories.drafts import DraftRepository
    rows=[];serial=0;outcome={'passed':False,'external_model_calls':0,'checks':rows,'model_source':'controlled_test_double'}
    def record(name,passed,**detail):
        rows.append({'name':name,'passed':bool(passed),**detail})
        write(output/'checks.json',rows)
        if not passed:raise AssertionError(name)
    try:
        with isolated(root,output) as env:
            connect=env['connect']
            app=FastAPI()
            for router in [upload_routes.router,record_routes.router,query_routes.router]:app.include_router(router)
            install_api_contract(app,configure=False)
            def client():return TestClient(app,raise_server_exceptions=False)
            def upload(c,member='a',key=None,data=None):
                nonlocal serial
                serial+=1
                if data is None:
                    f=io.BytesIO();Image.new('RGB',(16,16),(serial%255,serial//255,0)).save(f,format='PNG');data=f.getvalue()
                response=c.post('/api/ingest/upload',data={'member_id':member},files={'file':('m10.png',data,'image/png')},
                                headers={'Idempotency-Key':key} if key else {})
                if response.status_code!=200:raise AssertionError('upload '+response.text)
                return response.json(),data
            def confirm(c,rid,body=None,key=None):
                return c.post(f'/api/ingest/{rid}/confirm?member_id=a',json=body or BODY,headers={'Idempotency-Key':key} if key else {})
            def revision(c,rid,version,body,key,**extra):
                return c.post(f'/api/reports/{rid}/revisions?member_id=a',json={'expected_version':version,'reason':'合成数据复核',
                             'review':body,**extra},headers={'Idempotency-Key':key})
            with client() as c:
                draft,data=upload(c,key='upload-1');rid=draft['id'];calls=env['calls'].copy()
                second,_=upload(c,key='upload-2',data=data)
                record('duplicate_upload_reuses_id',second['id']==rid and second['upload_reused'] and env['calls']==calls,upload_id=rid)
                f=io.BytesIO();Image.new('RGB',(17,17),'red').save(f,format='PNG')
                conflict=c.post('/api/ingest/upload',data={'member_id':'a'},files={'file':('m10.png',f.getvalue(),'image/png')},headers={'Idempotency-Key':'upload-1'})
                record('upload_key_content_conflict',conflict.status_code==409 and conflict.json()['code']=='idempotency_conflict',http=conflict.status_code)
                other,_=upload(c,member='b',data=data)
                record('duplicate_file_member_scope',other['id']!=rid)
                first=confirm(c,rid,key='confirm-1');repeat=confirm(c,rid,key='confirm-1')
                record('confirm_replay',first.status_code==repeat.status_code==200 and repeat.json()['replayed'] and repeat.json()['report_version']==1)
                changed=confirm(c,rid,BODY|{'title':'不同内容'},key='confirm-1')
                record('confirm_key_conflict',changed.status_code==409 and changed.json()['code']=='idempotency_conflict')
                original_before=c.get(f'/api/ingest/{rid}/file?member_id=a').content
                provenance=c.get(f'/api/reports/{rid}/provenance?member_id=a').json()
                old_source=provenance['revisions'][0]['metric_sources'][0]
                query=c.post('/api/query',json={'question':'空腹血糖','member_id':'a','report_ids':[rid],'with_answer':False,'retrieval_mode':'hybrid'})
                record('original_query_with_version',query.status_code==200 and bool(query.json()['evidence']) and all(e['report_version']==1 for e in query.json()['evidence']))
                body=copy.deepcopy(BODY);body['raw_text']=body['raw_text'].replace('5.7','6.2');body['metrics'][0].update(value='6.2',source_text=body['raw_text'])
                rev=revision(c,rid,1,body,'revision-1')
                record('revision_atomic_publish',rev.status_code==200 and rev.json()['report_version']==rev.json()['index_version']==2)
                stale=revision(c,rid,1,body,'revision-stale')
                record('stale_revision_conflict',stale.status_code==409 and stale.json()['code']=='version_conflict')
                saved=c.get(f'/api/ingest/{rid}?member_id=a').json()
                record('extracted_and_confirmed_values_separate',saved['extracted_text']==TEXT and saved['metrics'][0]['value']=='6.2')
                resolved=c.get('/api/sources/resolve',params={'member_id':'a','source_id':old_source}).json()
                record('historical_metric_source_resolves',resolved['report_version']==1 and not resolved['is_current'] and resolved['payload']['value']=='5.7',source_id=old_source)
                metrics=c.get('/api/metrics?member_id=a').json()['items']
                record('metrics_current_version_only',len(metrics)==1 and metrics[0]['report_version']==2 and metrics[0]['value']=='6.2')
                after_query=c.post('/api/query',json={'question':'空腹血糖','member_id':'a','report_ids':[rid],'with_answer':False,'retrieval_mode':'hybrid'})
                record('retrieval_current_version_only',after_query.status_code==200 and bool(after_query.json()['evidence']) and all(e['report_version']==2 and '6.2' in e['content'] for e in after_query.json()['evidence']))
                trace=c.get('/api/query/'+query.json()['query_id']+'/trace').json()
                record('old_query_marked_historical',rid in trace['stale_report_ids'])
                record('original_file_unchanged',c.get(f'/api/ingest/{rid}/file?member_id=a').content==original_before)
                wrong=[c.get(f'/api/reports/{rid}/provenance?member_id=b'),c.get('/api/sources/resolve',params={'member_id':'b','source_id':old_source}),
                       c.post(f'/api/reports/{rid}/revisions?member_id=b',json={'expected_version':2,'reason':'范围检查','review':body})]
                record('provenance_revision_source_member_scope',all(r.status_code==404 for r in wrong),http=[r.status_code for r in wrong])
                calls=env['calls'].copy();noop=revision(c,rid,2,body,'noop')
                record('identical_revision_is_noop',noop.status_code==200 and not noop.json()['changed'] and env['calls']==calls)
                rebuild=revision(c,rid,2,body,'rebuild',rebuild_index=True)
                replayed=revision(c,rid,1,body,'revision-1')
                record('rebuild_and_historical_receipt',rebuild.status_code==replayed.status_code==200 and rebuild.json()['report_version']==3 and replayed.json()['report_version']==2 and replayed.json()['replayed'])
                write(output/'revision_case.json',c.get(f'/api/reports/{rid}/provenance?member_id=a').json())
                # 原件相同的重叠上传：一个执行抽取，另一个得到明确的处理中冲突。
                from app import report_extract
                entered=threading.Event();release=threading.Event();controlled_fields=report_extract.request_fields
                image=io.BytesIO();Image.new('RGB',(19,19),'blue').save(image,format='PNG');overlap=image.getvalue()
                def blocked_fields(*args):
                    entered.set()
                    if not release.wait(15):raise RuntimeError('受控上传等待超时')
                    return controlled_fields(*args)
                def overlapped_upload():
                    with client() as parallel:return parallel.post('/api/ingest/upload',data={'member_id':'a'},files={'file':('parallel.png',overlap,'image/png')})
                with patch.object(report_extract,'request_fields',side_effect=blocked_fields),ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(overlapped_upload)
                    try:
                        if not entered.wait(15):raise AssertionError('未进入受控抽取')
                        competing=c.post('/api/ingest/upload',data={'member_id':'a'},files={'file':('parallel.png',overlap,'image/png')})
                    finally:release.set()
                    primary=future.result(timeout=30)
                record('overlapping_upload_is_bounded',primary.status_code==200 and competing.status_code==409 and competing.json()['code']=='upload_in_progress',http=[primary.status_code,competing.status_code])
                # 数据库外键与 ready 版本检查不能被直接 SQL 绕过。
                import psycopg
                violations=[]
                for statement,params,expected_error in [
                    ("INSERT INTO metrics(report_id,metric_code,metric_name,value,unit,source_text,confirmed,data_version) VALUES (%s,'x','x',1,'u','x',TRUE,99)",(rid,),psycopg.errors.ForeignKeyViolation),
                    ("UPDATE reports SET index_version=99 WHERE id=%s",(rid,),psycopg.errors.CheckViolation)]:
                    try:
                        with connect() as conn:conn.execute(statement,params)
                    except expected_error:violations.append(True)
                    else:violations.append(False)
                record('database_version_constraints',all(violations),checks=violations)
                # PostgreSQL 触发器只存在本次隔离 schema，生产服务不增加故障开关。
                for table in ['reports','metrics','report_chunks','report_revisions','idempotency_records']:
                    candidate,_=upload(c);before=state(connect)
                    with connect() as conn:
                        conn.execute("CREATE OR REPLACE FUNCTION m10_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled_m10_fault'; END $$")
                        conn.execute(sql.SQL('CREATE TRIGGER m10_fault BEFORE INSERT ON {} FOR EACH ROW EXECUTE FUNCTION m10_fail()').format(sql.Identifier(table)))
                    try:response=confirm(c,candidate['id'],key='fault-'+table)
                    finally:
                        with connect() as conn:conn.execute(sql.SQL('DROP TRIGGER m10_fault ON {}').format(sql.Identifier(table)))
                    record('rollback_at_'+table,response.status_code==503 and before==state(connect),http=response.status_code,response=response.json(),before=before,after=state(connect))
                before=state(connect)
                with connect() as conn:conn.execute('CREATE TRIGGER m10_fault BEFORE INSERT ON report_chunks FOR EACH ROW EXECUTE FUNCTION m10_fail()')
                try:response=revision(c,rid,3,body|{'title':'应回滚'},'revision-fault')
                finally:
                    with connect() as conn:conn.execute('DROP TRIGGER m10_fault ON report_chunks')
                record('revision_rollback_keeps_previous_version',response.status_code==503 and before==state(connect))
                # 查询已取到旧证据，但日志晚于修订提交：仍需拒绝并标记这条迟到日志。
                from app import rag
                entered=threading.Event();release=threading.Event();original_save=rag.save_log;late_query=[]
                def delayed_save(*args):
                    late_query.append(args[0]);entered.set()
                    if not release.wait(15):raise RuntimeError('受控日志等待超时')
                    return original_save(*args)
                def concurrent_query():
                    with client() as parallel:return parallel.post('/api/query',json={'question':'空腹血糖','member_id':'a','report_ids':[rid],'with_answer':False,'retrieval_mode':'keyword'})
                with patch.object(rag,'save_log',side_effect=delayed_save),ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(concurrent_query)
                    try:
                        if not entered.wait(15):raise AssertionError('未进入受控日志保存阶段')
                        updated=revision(c,rid,3,body|{'title':'查询期间发生修订'},'during-query')
                    finally:release.set()
                    rejected=future.result(timeout=30)
                with connect() as conn:
                    logged=conn.execute('SELECT error_code,stale_report_ids FROM query_logs WHERE id=%s',(late_query[0],)).fetchone()
                record('query_revision_race_invalidates_late_log',updated.status_code==200 and rejected.status_code==409 and logged[0]=='version_conflict' and rid in logged[1],http=rejected.status_code)
                # 在事务外的索引准备处设置屏障，确保两个请求都进入确认流程。
                for same in [True,False]:
                    candidate,_=upload(c);barrier=threading.Barrier(2);indexer=report_ingest.prepare_chunks
                    def synchronized_index(text):barrier.wait(timeout=15);return indexer(text)
                    def run(index):
                        with client() as parallel:return confirm(parallel,candidate['id'],BODY if same else BODY|{'title':f'并发内容{index}'},key=f'parallel-{same}-{index}')
                    with patch.object(report_ingest,'prepare_chunks',side_effect=synchronized_index),ThreadPoolExecutor(max_workers=2) as pool:
                        futures=[pool.submit(run,i) for i in range(2)];responses=[f.result(timeout=30) for f in futures]
                    statuses=sorted(r.status_code for r in responses)
                    with connect() as conn:
                        counts=conn.execute('SELECT (SELECT count(*) FROM reports WHERE id=%s),(SELECT count(*) FROM metrics WHERE report_id=%s),'
                                            '(SELECT count(*) FROM report_revisions WHERE report_id=%s)',(candidate['id'],)*3).fetchone()
                    record('parallel_same_content' if same else 'parallel_conflicting_content',statuses==([200,200] if same else [200,409]) and counts==(1,1,1),http=statuses,responses=[r.json() for r in responses],final_counts=counts)
                pending,_=upload(c);invalid=copy.deepcopy(BODY);invalid['raw_text']=invalid['raw_text'].replace('mmol/L','kg');invalid['metrics'][0].update(unit='kg',source_text=invalid['raw_text'])
                invalid_response=confirm(c,pending['id'],invalid)
                record('unit_rule_keeps_pending_original',invalid_response.status_code==422 and c.get(f"/api/ingest/{pending['id']}?member_id=a").json()['status']=='draft')
                outside,_=upload(c);outlier=copy.deepcopy(BODY);outlier['raw_text']=outlier['raw_text'].replace('5.7','99');outlier['metrics'][0].update(value='99',source_text=outlier['raw_text'])
                response=confirm(c,outside['id'],outlier)
                record('outside_reference_range_not_deleted',response.status_code==200 and c.get(f"/api/ingest/{outside['id']}?member_id=a").json()['metrics'][0]['value']=='99')
                quality=audit(connect,DraftRepository())
                write(output/'quality.json',quality);record('synthetic_quality_report',quality['passed'])
                outcome.update(passed=all(r['passed'] for r in rows),scenarios=len(rows),passed_scenarios=sum(r['passed'] for r in rows),
                               controlled_calls=env['calls'],database=env['database'],server_version=env['server_version'],pgvector_version=env['pgvector_version'])
        outcome['temporary_schema_cleaned']=env['temporary_schema_cleaned']
        outcome['passed'] &= env['temporary_schema_cleaned']
    except Exception as exc:
        outcome.update(passed=False,error_type=type(exc).__name__,message=str(exc))
    finally:
        write(output/'summary.json',outcome)
    return outcome


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    result=execute(Path.cwd(),Path(args.output));print(json.dumps(result,ensure_ascii=False,default=str));raise SystemExit(0 if result['passed'] else 2)
