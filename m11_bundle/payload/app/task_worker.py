"""独立单 worker：父进程续租，子进程处理，超过总时限或丢租约时终止子进程。"""

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import time
from uuid import uuid4

from psycopg import sql

from app.db import connect_db
from app.repositories.drafts import DraftRepository
from app.repositories.tasks import TaskRepository
from app.services.governance import ConsistentReportService
from app.task_pipeline import Pipeline
from app.task_policy import LostExecution

logger = logging.getLogger(__name__)


def worker_connection():
    conn = connect_db()
    # 仅 CLI/隔离验收使用；HTTP API 不接受 schema 参数。
    schema = os.getenv('M11_DB_SCHEMA', '')
    if schema:
        if not re.fullmatch(r'm11_[a-z0-9_]+', schema):
            conn.close(); raise RuntimeError('M11 验收 schema 名称非法')
        try:
            conn.execute(sql.SQL('SET search_path TO {},public').format(sql.Identifier(schema)))
        except BaseException:
            conn.close(); raise
    return conn


def report_service(repository):
    return ConsistentReportService(repository.governance, DraftRepository(), None, None, None)


def execute_task(task_id, token):
    repository = TaskRepository(worker_connection)
    with repository.connection() as conn:
        task = conn.execute('SELECT * FROM processing_tasks WHERE id=%s AND execution_token=%s', (task_id, token)).fetchone()
    if task is None:
        return 0
    Pipeline(repository, report_service(repository)).execute(task)
    return 0


def supervise(repository, task, worker_id):
    command = [sys.executable, '-m', 'app.task_worker', '--execute', task['id'], '--token', task['execution_token']]
    child = subprocess.Popen(command)
    try:
        while child.poll() is None:
            if not repository.heartbeat(task['id'], task['execution_token']):
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill(); child.wait()
                repository.recover()
                return
            repository.worker_heartbeat(worker_id)
            try: child.wait(timeout=repository.policy.heartbeat_seconds)
            except subprocess.TimeoutExpired: pass
        if child.returncode:
            # 非正常进程退出交给租约恢复；不能重置预算或误记任务成功。
            logger.error('task child exited task_id=%s exit=%s', task['id'], child.returncode)
    finally:
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired: child.kill(); child.wait()


def run(once=False):
    repository, worker_id = TaskRepository(worker_connection), uuid4().hex
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
        # 保持 finally 可执行，终止当前子进程；任务等租约过期后恢复。
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        with repository.singleton(worker_id) as lock_connection:
            while not stopping:
                # 确认单 worker 的会话锁连接仍存在；丢失后不再领新任务。
                lock_connection.execute('SELECT 1')
                repository.worker_heartbeat(worker_id)
                task = repository.claim()
                if task:
                    supervise(repository, task, worker_id)
                elif once:
                    break
                else:
                    time.sleep(1)
                if once:
                    break
    except KeyboardInterrupt:
        pass
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--execute')
    parser.add_argument('--token')
    parser.add_argument('--healthcheck', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.healthcheck:
        return 0 if TaskRepository(worker_connection).worker_status()['online'] else 1
    return execute_task(args.execute, args.token) if args.execute else run(args.once)


if __name__ == '__main__':
    raise SystemExit(main())
