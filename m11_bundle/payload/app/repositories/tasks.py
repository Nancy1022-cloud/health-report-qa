"""PostgreSQL 持久队列。报告锁 -> 任务行锁；模型调用不占写事务。"""

from contextlib import contextmanager
from uuid import uuid4
from urllib.parse import urlencode

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain_errors import BusinessError
from app.task_policy import LostExecution, TaskFailure, TaskPolicy, backoff
from app.repositories.governance import GovernanceRepository

TERMINAL = {'succeeded', 'failed', 'superseded'}


class TaskRepository:
    def __init__(self, connection_factory, policy=None):
        self.connection_factory = connection_factory
        self.governance = GovernanceRepository(connection_factory)
        self.policy = policy or TaskPolicy.from_env()

    @contextmanager
    def connection(self):
        with self.connection_factory() as conn:
            conn.row_factory = dict_row
            yield conn

    def event(self, conn, task, event, **detail):
        conn.execute('INSERT INTO processing_events(task_id,event,attempt,execution_token,detail) VALUES (%s,%s,%s,%s,%s)',
                     (task['id'], event, task['attempts'], task['execution_token'], Jsonb(detail)))

    def request(self, conn, member_id, kind, key, request_hash):
        row = conn.execute('SELECT fingerprint,task_id FROM processing_receipts WHERE member_id=%s AND kind=%s AND request_key=%s',
                           (member_id, kind, key)).fetchone()
        if row:
            if row['fingerprint'] != request_hash:
                raise BusinessError('idempotency_conflict', '同一任务幂等标识已用于不同内容。')
            return conn.execute('SELECT * FROM processing_tasks WHERE id=%s', (row['task_id'],)).fetchone()

    def remember(self, conn, task, key, request_hash):
        conn.execute('INSERT INTO processing_receipts(member_id,kind,request_key,fingerprint,task_id) VALUES (%s,%s,%s,%s,%s)',
                     (task['member_id'], task['kind'], key, request_hash, task['id']))

    def create(self, conn, member_id, report_id, kind, request_hash, payload, result=None):
        # 调用者持有同一 report 锁；新处理代次一次性使未完成的旧任务失效。
        previous = conn.execute('SELECT generation FROM processing_heads WHERE member_id=%s AND report_id=%s',
                                (member_id, report_id)).fetchone()
        generation = previous['generation'] + 1 if previous and result is None else previous['generation'] if previous else 1
        previous_tasks = conn.execute("UPDATE processing_tasks SET status='superseded',stage='superseded',error_code='new_generation',"
            "execution_token=NULL,lease_until=NULL,finished_at=clock_timestamp(),updated_at=clock_timestamp() "
            "WHERE member_id=%s AND report_id=%s AND status IN ('queued','running') AND %s RETURNING *", (member_id, report_id, result is None)).fetchall()
        for old in previous_tasks:
            conn.execute("UPDATE processing_calls SET status='unknown',error_code='superseded',finished_at=clock_timestamp() "
                         "WHERE task_id=%s AND status='reserved'", (old['id'],))
            self.event(conn, old, 'superseded', next_generation=generation)
        task_id = 'task_' + uuid4().hex
        task = conn.execute('INSERT INTO processing_tasks(id,member_id,report_id,kind,generation,fingerprint,payload,max_attempts,max_calls,deadline) '
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) RETURNING *",
            (task_id, member_id, report_id, kind, generation, request_hash, Jsonb(payload), self.policy.max_attempts,
             self.policy.max_calls, self.policy.timeout_seconds)).fetchone()
        if result is None:
            conn.execute('INSERT INTO processing_heads(member_id,report_id,generation,task_id) VALUES (%s,%s,%s,%s) '
                         'ON CONFLICT(member_id,report_id) DO UPDATE SET generation=EXCLUDED.generation,task_id=EXCLUDED.task_id',
                         (member_id, report_id, generation, task_id))
        self.event(conn, task, 'queued')
        if result is not None:
            task = conn.execute("UPDATE processing_tasks SET status='succeeded',stage='completed',result=%s,finished_at=clock_timestamp() "
                                'WHERE id=%s RETURNING *', (Jsonb(result), task_id)).fetchone()
            self.event(conn, task, 'completed', reused=True)
        return task

    def get(self, task_id, member_id):
        self.recover()
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM processing_tasks WHERE id=%s AND member_id=%s', (task_id, member_id)).fetchone()
            if row is None:
                raise BusinessError('task_not_found', '该成员下不存在此任务。')
            events = conn.execute('SELECT event,attempt,detail,created_at FROM processing_events WHERE task_id=%s ORDER BY id', (task_id,)).fetchall()
            calls = conn.execute('SELECT status,count(*) AS count FROM processing_calls WHERE task_id=%s GROUP BY status', (task_id,)).fetchall()
            artifacts = conn.execute('SELECT stage FROM processing_artifacts WHERE task_id=%s ORDER BY stage', (task_id,)).fetchall()
        return self.public(row, events=events, call_states={r['status']: r['count'] for r in calls},
                           saved_stages=[r['stage'] for r in artifacts])

    @staticmethod
    def public(row, **extra):
        fields = ['id', 'member_id', 'report_id', 'kind', 'generation', 'status', 'stage', 'attempts', 'max_attempts',
                  'calls_reserved', 'max_calls', 'deadline', 'created_at', 'updated_at', 'finished_at', 'error_code', 'result']
        return {**{k: row[k] for k in fields}, 'status_url': f"/api/tasks/{row['id']}?" + urlencode({'member_id': row['member_id']}), **extra}

    def recover(self):
        """由 worker 和状态查询清理已过期执行；排队时间也计入任务时限。"""
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM processing_tasks WHERE (status='queued' AND deadline<=clock_timestamp()) "
                "OR (status='running' AND (lease_until<=clock_timestamp() OR deadline<=clock_timestamp())) FOR UPDATE SKIP LOCKED").fetchall()
            for row in rows:
                expired = conn.execute('SELECT clock_timestamp()>=%s AS expired', (row['deadline'],)).fetchone()['expired']
                failed = expired or row['attempts'] >= row['max_attempts']
                error = 'task_deadline' if expired else 'attempt_budget' if failed else 'lease_expired'
                conn.execute("UPDATE processing_calls SET status='unknown',error_code='execution_lost',finished_at=clock_timestamp() "
                             "WHERE task_id=%s AND status='reserved'", (row['id'],))
                self.event(conn, row, 'recovered', reason=error, previous_token=row['execution_token'])
                conn.execute("UPDATE processing_tasks SET status=%s,stage=%s,error_code=%s,execution_token=NULL,lease_until=NULL,"
                    "available_at=clock_timestamp()+(%s*interval '1 second'),updated_at=clock_timestamp(),"
                    "finished_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END WHERE id=%s",
                    ('failed' if failed else 'queued', 'failed' if failed else 'retry_wait', error,
                     0 if expired else backoff(row['attempts']), failed, row['id']))
            return len(rows)

    def claim(self):
        self.recover()
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM processing_tasks WHERE status='queued' AND available_at<=clock_timestamp() "
                               'AND deadline>clock_timestamp() ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1').fetchone()
            if row is None:
                return None
            if row['attempts'] >= row['max_attempts']:
                raise RuntimeError('任务尝试次数异常')
            task = conn.execute("UPDATE processing_tasks SET status='running',stage='starting',attempts=attempts+1,"
                "execution_token=%s,lease_until=LEAST(deadline,clock_timestamp()+(%s*interval '1 second')),"
                'updated_at=clock_timestamp() WHERE id=%s RETURNING *', (uuid4().hex, self.policy.lease_seconds, row['id'])).fetchone()
            self.event(conn, task, 'claimed')
            return task

    def heartbeat(self, task_id, token):
        with self.connection() as conn:
            return conn.execute("UPDATE processing_tasks SET lease_until=LEAST(deadline,clock_timestamp()+(%s*interval '1 second')),"
                "updated_at=clock_timestamp() WHERE id=%s AND status='running' AND execution_token=%s "
                'AND lease_until>clock_timestamp() AND deadline>clock_timestamp() RETURNING id',
                (self.policy.lease_seconds, task_id, token)).fetchone() is not None

    def guard(self, conn, task_id, token):
        row = conn.execute('SELECT *,EXTRACT(EPOCH FROM(deadline-clock_timestamp()))::float AS remaining, '
                           'lease_until>clock_timestamp() AS lease_valid FROM processing_tasks WHERE id=%s FOR UPDATE', (task_id,)).fetchone()
        if row is None or row['status'] != 'running' or row['execution_token'] != token or not row['lease_valid'] or row['remaining'] <= 0:
            raise LostExecution('执行标识或租约已失效')
        head = conn.execute('SELECT generation,task_id FROM processing_heads WHERE member_id=%s AND report_id=%s',
                            (row['member_id'], row['report_id'])).fetchone()
        if head is None or head['generation'] != row['generation'] or head['task_id'] != row['id']:
            raise LostExecution('处理版本已失效')
        return row

    @contextmanager
    def execution(self, task, token):
        with self.governance.write(task['report_id'], task['member_id'], 'confirm', None) as conn:
            current = self.guard(conn, task['id'], token)
            yield conn, current

    def artifact(self, task, token, stage):
        with self.execution(task, token) as (conn, current):
            row = conn.execute('SELECT * FROM processing_artifacts WHERE task_id=%s AND stage=%s', (task['id'], stage)).fetchone()
            if row and row['fingerprint'] != current['fingerprint']:
                raise TaskFailure('artifact_version_conflict')
            return row['result'] if row else None

    def save_artifact(self, task, token, stage, result):
        with self.execution(task, token) as (conn, current):
            conn.execute('INSERT INTO processing_artifacts(task_id,stage,fingerprint,result) VALUES (%s,%s,%s,%s) '
                         'ON CONFLICT(task_id,stage) DO NOTHING', (task['id'], stage, current['fingerprint'], Jsonb(result)))
            conn.execute('UPDATE processing_tasks SET stage=%s,updated_at=clock_timestamp() WHERE id=%s', (stage, task['id']))
            self.event(conn, current, 'stage_saved', stage=stage)

    def reserve_call(self, task, token, stage, prefix):
        with self.execution(task, token) as (conn, current):
            if current['calls_reserved'] >= current['max_calls']:
                raise TaskFailure('call_budget')
            worker = conn.execute('SELECT EXTRACT(EPOCH FROM(next_call_at-clock_timestamp()))::float AS wait '
                                  'FROM processing_worker_state WHERE id=1 FOR UPDATE').fetchone()
            if worker['wait'] > 0:
                return {'wait': min(worker['wait'], current['remaining'])}
            call_id = uuid4().hex
            conn.execute('INSERT INTO processing_calls(id,task_id,attempt,execution_token,stage,prefix,status) '
                "VALUES (%s,%s,%s,%s,%s,%s,'reserved')", (call_id, task['id'], current['attempts'], token, stage, prefix))
            conn.execute('UPDATE processing_tasks SET calls_reserved=calls_reserved+1,stage=%s WHERE id=%s', (stage, task['id']))
            conn.execute("UPDATE processing_worker_state SET next_call_at=clock_timestamp()+(%s*interval '1 second') WHERE id=1",
                         (self.policy.min_call_interval,))
            self.event(conn, current, 'call_reserved', call_id=call_id, stage=stage, prefix=prefix)
            return {'id': call_id, 'timeout': min(self.policy.call_timeout_seconds, current['remaining'])}

    def finish_call(self, task, token, call_id, *, usage=None, error=None, stage=None, result=None):
        with self.execution(task, token) as (conn, current):
            row = conn.execute("UPDATE processing_calls SET status=%s,usage=%s,error_code=%s,finished_at=clock_timestamp() "
                "WHERE id=%s AND task_id=%s AND execution_token=%s AND status='reserved' RETURNING id",
                ('error' if error else 'ok', Jsonb(usage) if usage is not None else None, error, call_id, task['id'], token)).fetchone()
            if row is None:
                raise LostExecution('调用记录已被其他执行关闭')
            if not error:
                # 调用用量与阶段结果一起保存；不会留下“已记成功但丢了阶段”的可避免窗口。
                conn.execute('INSERT INTO processing_artifacts(task_id,stage,fingerprint,result) VALUES (%s,%s,%s,%s)',
                             (task['id'], stage, current['fingerprint'], Jsonb(result)))
                self.event(conn, current, 'stage_saved', stage=stage, call_id=call_id)

    def complete(self, conn, task, result):
        # 再检一次事务内耗时；超时或失去提交权限时连业务写入一起回滚。
        self.guard(conn, task['id'], task['execution_token'])
        conn.execute("UPDATE processing_tasks SET status='succeeded',stage='completed',result=%s,error_code=NULL,"
                     'execution_token=NULL,lease_until=NULL,finished_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s',
                     (Jsonb(result), task['id']))
        self.event(conn, task, 'completed')

    def fail(self, task, token, failure):
        with self.execution(task, token) as (conn, current):
            conn.execute("UPDATE processing_calls SET status='unknown',error_code='execution_failed',finished_at=clock_timestamp() "
                         "WHERE task_id=%s AND execution_token=%s AND status='reserved'", (task['id'], token))
            retry = failure.retryable and current['attempts'] < current['max_attempts'] and current['calls_reserved'] < current['max_calls']
            delay = backoff(current['attempts'])
            retry = retry and current['remaining'] > delay
            reason = failure.code
            if failure.retryable and not retry:
                reason = 'call_budget' if current['calls_reserved'] >= current['max_calls'] else 'attempt_budget' if current['attempts'] >= current['max_attempts'] else 'task_deadline'
            conn.execute("UPDATE processing_tasks SET status=%s,stage=%s,error_code=%s,execution_token=NULL,lease_until=NULL,"
                "available_at=clock_timestamp()+(%s*interval '1 second'),updated_at=clock_timestamp(),"
                'finished_at=CASE WHEN %s THEN NULL ELSE clock_timestamp() END WHERE id=%s',
                ('queued' if retry else 'failed', 'retry_wait' if retry else 'failed', reason, delay, retry, task['id']))
            self.event(conn, current, 'retry_scheduled' if retry else 'failed', error_code=reason, cause=failure.code, delay=delay if retry else None)

    @contextmanager
    def singleton(self, worker_id):
        conn = self.connection_factory()
        try:
            conn.commit(); conn.autocommit = True
            # 两个独立整数键含 schema，隔离验收 worker 与本机业务 worker。
            row = conn.execute("SELECT pg_try_advisory_lock(hashtext(current_schema()),hashtext('health_m11_worker'))").fetchone()
            if not row[0]:
                raise RuntimeError('同一 schema 已有 M11 worker；首版只运行一个')
            yield conn
        finally:
            conn.close()

    def worker_heartbeat(self, worker_id):
        with self.connection() as conn:
            conn.execute('UPDATE processing_worker_state SET heartbeat_at=clock_timestamp(),worker_id=%s WHERE id=1', (worker_id,))

    def worker_status(self):
        with self.connection() as conn:
            row = conn.execute("SELECT heartbeat_at,worker_id,COALESCE(heartbeat_at>clock_timestamp()-interval '12 seconds',FALSE) AS online "
                               'FROM processing_worker_state WHERE id=1').fetchone()
        return dict(row)
