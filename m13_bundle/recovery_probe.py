"""M13 compatibility runner of the preserved M11 checks; assertions unchanged.

Normal inline tasks renew leases; crash children still exit and wait for real expiration.
The original m11_bundle source and historical evidence are never modified.
"""

import argparse
import copy
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path.cwd().resolve()
sys.path.insert(0, str(ROOT))

from m11_bundle.tests.support import BODY, ControlledAdapter, png, write
from m13_bundle.recovery_support import isolated


def execute(output):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api_errors import install_api_contract
    from app.dependencies import get_task_service
    from app.repositories.tasks import TaskRepository
    from app.schemas.governance import RevisionRequest
    from app.services.tasks import TaskService
    from m13_bundle.recovery_support import RecoveryPipeline as Pipeline
    from app.task_policy import LostExecution, TaskFailure, TaskPolicy
    from app.task_worker import report_service
    from app.task_routes import router
    from app.upload_models import Review
    checks = []
    result = {'passed': False, 'checks': checks, 'external_model_calls': 0, 'real_model_evaluation': 'not_executed'}
    def record(name, passed, **detail):
        checks.append({'name': name, 'passed': bool(passed), **detail})
        write(output / 'checks.json', checks)
        if not passed:
            raise AssertionError(name)
    env = None
    try:
        with isolated(ROOT, output) as (connect, env):
            policy = TaskPolicy(lease_seconds=3, heartbeat_seconds=.5, min_call_interval=0)
            repo = TaskRepository(connect, policy)
            reports = report_service(repo)
            service = TaskService(repo, reports)
            adapter = ControlledAdapter()
            pipeline = Pipeline(repo, reports, adapter)
            app = FastAPI(); app.include_router(router); install_api_contract(app, configure=False)
            app.dependency_overrides[get_task_service] = lambda: service
            def raw(task_id):
                with repo.connection() as conn:
                    return conn.execute('SELECT * FROM processing_tasks WHERE id=%s', (task_id,)).fetchone()
            def now_ready(task_id):
                with repo.connection() as conn:
                    conn.execute("UPDATE processing_tasks SET available_at=clock_timestamp()-interval '1 second' WHERE id=%s", (task_id,))
            def run_task():
                task = repo.claim()
                if task is None: raise AssertionError('no queued task')
                pipeline.execute(task)
                return raw(task['id'])
            def crash(point, job, claimed):
                proc=subprocess.Popen([sys.executable,str(ROOT / 'm11_bundle/tests/crash_child.py'),'--task-id',job['id'],
                    '--token',claimed['execution_token'],'--point',point],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
                end=time.monotonic()+30
                try:
                    while proc.poll() is None:
                        # 启动慢的电脑也先正常续租；子进程中断后才停止续租并等待真实过期。
                        repo.heartbeat(job['id'],claimed['execution_token'])
                        if time.monotonic()>end:raise TimeoutError('crash child startup/execution timeout')
                        time.sleep(.2)
                    content=proc.communicate()[0]
                    if proc.returncode!=77:raise AssertionError('crash child '+point+': '+content)
                finally:
                    if proc.poll() is None:proc.kill();proc.communicate()
            serial = 0
            def new_upload():
                nonlocal serial
                serial += 1
                row = service.upload('a', 'synthetic.png', png(serial), 'upload-' + str(serial))
                return row, run_task()
            def counts(report_id):
                with repo.connection() as conn:
                    return [conn.execute('SELECT count(*) AS n FROM ' + table + ' WHERE ' + field + '=%s',
                                         (report_id,)).fetchone()['n'] for table, field in
                            [('reports','id'), ('metrics','report_id'), ('report_revisions','report_id')]]

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post('/api/tasks/upload', data={'member_id': 'a'}, files={'file': ('demo.png', png(0), 'image/png')},
                                       headers={'Idempotency-Key': 'http-upload'})
                task = response.json(); tid = task.get('id')
                record('http_accept_202_without_model', response.status_code == 202 and task['status'] == 'queued' and task['calls_reserved'] == 0,
                       http=response.status_code, task_id=tid)
                again = service.upload('a', 'renamed.png', png(0), 'another-upload-key')
                record('duplicate_upload_task_reused', again['id'] == tid)
                wrong = client.get(f'/api/tasks/{tid}?member_id=b')
                invalid = client.post('/api/tasks/upload', data={'member_id': 'a'}, files={'file': ('demo.png', png(0), 'image/png')})
                record('task_scope_and_contract', wrong.status_code == 404 and wrong.json()['code'] == 'resource_not_found' and invalid.status_code == 422,
                       wrong_status=wrong.status_code,wrong_body=wrong.json(),invalid_status=invalid.status_code,invalid_body=invalid.json())
                completed = run_task(); rid = completed['report_id']
                record('extract_and_draft_ready', completed['status'] == 'succeeded' and counts(rid) == [0,0,0] and
                       reports.drafts.load(rid,'a')[0]['raw_text'] == BODY['raw_text'])
                job = service.confirm(rid,'a',Review.model_validate(BODY),'confirm-main')
                duplicate = service.confirm(rid,'a',Review.model_validate(BODY),'confirm-main-2')
                done = run_task()
                record('confirm_atomic_and_idempotent', duplicate['id'] == job['id'] and done['status'] == 'succeeded' and counts(rid) == [1,1,1], final_counts=counts(rid), task_status=done['status'], task_stage=done['stage'], error_code=done['error_code'],
                       calls_reserved=done['calls_reserved'], same_task_id=duplicate['id'] == job['id'])
                replay = service.confirm(rid,'a',Review.model_validate(BODY),'confirm-main')
                record('completed_task_receipt_replay', replay['id'] == job['id'] and replay['status'] == 'succeeded')

            # 每个故障是独立进程直接退出，保留数据库中已写的阶段/调用记录。
            for point, expected_calls in [('before_external',2), ('external_returned',2), ('stage_saved',1), ('before_commit',1), ('after_commit',1)]:
                serial += 1
                job = service.upload('a','crash.png',png(serial),'crash-'+point)
                claimed = repo.claim()
                started = time.monotonic()
                crash(point, job, claimed)
                if point != 'after_commit':
                    # 等待真实租约过期；不修改租约时间来冒充重启恢复。
                    end = time.monotonic()+5
                    while time.monotonic()<end and raw(job['id'])['status']=='running':
                        time.sleep(.15);repo.recover()
                    now_ready(job['id']); recovered=run_task()
                else:
                    recovered=raw(job['id'])
                detail=repo.get(job['id'],'a')
                record('process_crash_'+point, recovered['status']=='succeeded' and recovered['calls_reserved']==expected_calls,
                       task_id=job['id'],calls_reserved=recovered['calls_reserved'],call_states=detail['call_states'],
                       attempts=recovered['attempts'],recovery_seconds=round(time.monotonic()-started,3))
                if point=='external_returned':
                    record('unknown_call_not_faked_as_zero', detail['call_states'].get('unknown')==1)
                if point in {'stage_saved','before_commit'}:
                    record('saved_stage_reused_'+point, recovered['calls_reserved']==1 and recovered['attempts']==2)

            # 两个领任务者不能同时取得同一任务。
            for point in ['stage_saved','before_commit','after_commit']:
                upload_job,_=new_upload()
                confirm_job=service.confirm(upload_job['report_id'],'a',Review.model_validate(BODY),'index-crash-'+point)
                claimed=repo.claim()
                crash(point,confirm_job,claimed)
                if point!='after_commit':
                    end=time.monotonic()+5
                    while time.monotonic()<end and raw(confirm_job['id'])['status']=='running':
                        time.sleep(.15);repo.recover()
                    now_ready(confirm_job['id']);recovered=run_task()
                else:recovered=raw(confirm_job['id'])
                record('index_crash_'+point,recovered['status']=='succeeded' and recovered['calls_reserved']==1 and counts(upload_job['report_id'])==[1,1,1],
                       calls_reserved=recovered['calls_reserved'],attempts=recovered['attempts'],final_counts=counts(upload_job['report_id']))

            # 两个领任务者不能同时取得同一任务。
            serial+=1; job=service.upload('a','parallel.png',png(serial),'claim-parallel')
            with ThreadPoolExecutor(max_workers=2) as pool:
                claimed=list(pool.map(lambda _:repo.claim(),range(2)))
            active=[row for row in claimed if row]
            record('parallel_claim_once', len(active)==1 and active[0]['id']==job['id'])
            pipeline.execute(active[0])
            with repo.singleton('first'):
                try:
                    with repo.singleton('second'): singleton=False
                except RuntimeError: singleton=True
            record('second_worker_rejected',singleton)

            # 使用实际监督进程终止一个不返回的子进程，验证总时限而非仅 SDK 超时。
            from app.task_worker import supervise
            serial+=1;deadline_job=service.upload('a','hang.png',png(serial),'hard-deadline')
            task=repo.claim()
            with repo.connection() as conn:conn.execute("UPDATE processing_tasks SET deadline=clock_timestamp()+interval '1 second' WHERE id=%s",(task['id'],))
            started=time.monotonic()
            # 替换命令只在此测试进程生效；监督/续租/终止逻辑使用生产实现。
            import app.task_worker as worker_module
            from unittest.mock import patch
            original_popen=subprocess.Popen
            with patch.object(worker_module.subprocess,'Popen',side_effect=lambda _cmd:original_popen([sys.executable,'-c','import time;time.sleep(20)'])):
                supervise(repo,task,'controlled-supervisor')
            expired=raw(task['id'])
            record('supervisor_enforces_task_deadline',expired['status']=='failed' and expired['error_code']=='task_deadline' and time.monotonic()-started<5,
                   elapsed_seconds=round(time.monotonic()-started,3),calls_reserved=expired['calls_reserved'])

            # 旧修订已经完成索引准备后阻塞；新修订先提交，再放行旧执行。
            def revision_body(title):
                return RevisionRequest(expected_version=1,reason='合成修订',review=Review.model_validate(BODY|{'title':title}))
            old=service.revise(rid,'a',revision_body('old-late'),'old-late')
            old_claim=repo.claim();entered=threading.Event();release=threading.Event()
            def hold(point,**_):
                if point=='before_commit': entered.set();release.wait(timeout=10)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(Pipeline(repo,reports,adapter,hold).execute,old_claim)
                if not entered.wait(timeout=5):raise AssertionError('old task did not reach fence')
                newer=service.revise(rid,'a',revision_body('new-current'),'new-current')
                newer_done=run_task();release.set();future.result(timeout=5)
            current=repo.governance.find_current(rid,'a',required=True)
            record('late_old_generation_cannot_overwrite',raw(old['id'])['status']=='superseded' and newer_done['status']=='succeeded' and
                   current['data_version']==2 and current['snapshot']['title']=='new-current',
                   old_task=old['id'],new_task=newer['id'],current_version=current['data_version'],final_counts=counts(rid))

            # 同一任务旧 token 迟到，新的执行 token 才能保存阶段。
            serial+=1; job=service.upload('a','token.png',png(serial),'token')
            first=repo.claim()
            with repo.connection() as conn:
                conn.execute("UPDATE processing_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(job['id'],))
            repo.recover();now_ready(job['id']);second=repo.claim()
            rejected=False
            try:repo.save_artifact(first,first['execution_token'],'forbidden',{})
            except LostExecution:rejected=True
            pipeline.execute(second)
            record('late_same_task_token_rejected',rejected and raw(job['id'])['status']=='succeeded' and first['execution_token']!=second['execution_token'])

            # 有限重试、永久错误、共享调用预算与排队超时。
            class Flaky(ControlledAdapter):
                def __init__(self, failures, retryable=True):self.failures,self.retryable=failures,retryable
                def fields(self,*args):
                    if self.failures:
                        self.failures-=1;raise TaskFailure('upstream_timeout' if self.retryable else 'upstream_http_403',retryable=self.retryable)
                    return super().fields(*args)
            for name,failures,retryable,expected_status,attempts in [('temporary',1,True,'succeeded',2),('persistent',5,True,'failed',3),('permanent',1,False,'failed',1)]:
                serial+=1; job=service.upload('a','fail.png',png(serial),'failure-'+name)
                engine=Pipeline(repo,reports,Flaky(failures,retryable))
                for _ in range(4):
                    task=repo.claim()
                    if task:engine.execute(task)
                    outcome=raw(job['id'])
                    if outcome['status'] in {'succeeded','failed'}:break
                    now_ready(job['id'])
                record('retry_'+name,outcome['status']==expected_status and outcome['attempts']==attempts,
                       attempts=outcome['attempts'],calls_reserved=outcome['calls_reserved'],error_code=outcome['error_code'])
            serial+=1;job=service.upload('a','budget.png',png(serial),'call-budget')
            with repo.connection() as conn:conn.execute('UPDATE processing_tasks SET max_calls=1 WHERE id=%s',(job['id'],))
            Pipeline(repo,reports,Flaky(5)).execute(repo.claim())
            outcome=raw(job['id'])
            record('all_attempts_share_call_budget',outcome['status']=='failed' and outcome['error_code']=='call_budget' and outcome['calls_reserved']==1)
            serial+=1;job=service.upload('a','deadline.png',png(serial),'deadline')
            with repo.connection() as conn:conn.execute("UPDATE processing_tasks SET deadline=clock_timestamp()-interval '1 second' WHERE id=%s",(job['id'],))
            repo.recover();outcome=raw(job['id'])
            record('queue_time_counts_towards_deadline',outcome['status']=='failed' and outcome['error_code']=='task_deadline' and outcome['calls_reserved']==0)

            # 在任务成功标记处故障，业务数据也必须回滚；重试复用已保存的索引。
            new,extracted=new_upload();rid2=new['report_id']
            job=service.confirm(rid2,'a',Review.model_validate(BODY),'atomic-success')
            with repo.connection() as conn:
                conn.execute("CREATE FUNCTION fail_task_success() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status='succeeded' AND NEW.kind='confirm' THEN RAISE EXCEPTION 'injected task completion failure'; END IF; RETURN NEW; END $$")
                conn.execute('CREATE TRIGGER fail_success BEFORE UPDATE ON processing_tasks FOR EACH ROW EXECUTE FUNCTION fail_task_success()')
            outcome=run_task()
            record('business_and_task_success_rollback_together',outcome['status']=='failed' and counts(rid2)==[0,0,0],final_counts=counts(rid2))
            with repo.connection() as conn:conn.execute('DROP TRIGGER fail_success ON processing_tasks')

            # 持久化限速位置会阻挡下一次模型调用，不因重试/新进程归零。
            serial+=1;job=service.upload('a','rate.png',png(serial),'rate')
            task=repo.claim()
            with repo.connection() as conn:conn.execute("UPDATE processing_worker_state SET next_call_at=clock_timestamp()+interval '1 second' WHERE id=1")
            reservation=repo.reserve_call(task,task['execution_token'],'rate','VISION')
            record('persistent_rate_limit',0<reservation.get('wait',0)<=1 and raw(job['id'])['calls_reserved']==0)
            with repo.connection() as conn:conn.execute("UPDATE processing_worker_state SET next_call_at=clock_timestamp() WHERE id=1")
            pipeline.execute(task)

            result['passed']=all(r['passed'] for r in checks)
            result['scenarios']=len(checks)
            result['environment']={k:v for k,v in env.items() if k!='temporary_schema_cleaned'}
    except Exception as error:
        import traceback
        result.update(passed=False,error_type=type(error).__name__,message=str(error),traceback=traceback.format_exc())
    result['temporary_schema_cleaned']=bool(env and env['temporary_schema_cleaned'])
    result['passed']=result['passed'] and result['temporary_schema_cleaned']
    write(output/'summary.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    outcome=execute(Path(args.output));print(json.dumps(outcome,ensure_ascii=False,indent=2,default=str));raise SystemExit(0 if outcome['passed'] else 2)
