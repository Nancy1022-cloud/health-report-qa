"""隔离验收子进程；故障钩子只存在此脚本，不进入生产 worker。"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path.cwd().resolve()
sys.path.insert(0, str(ROOT))

from app.repositories.tasks import TaskRepository
from app.task_pipeline import Pipeline
from app.task_worker import report_service, worker_connection
from m11_bundle.tests.support import ControlledAdapter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--point', required=True)
    args = parser.parse_args()
    repository = TaskRepository(worker_connection)
    with repository.connection() as conn:
        task = conn.execute('SELECT * FROM processing_tasks WHERE id=%s', (args.task_id,)).fetchone()
    if task['execution_token'] != args.token:
        raise RuntimeError('测试 token 不匹配')
    def hook(point, **details):
        if point == args.point:
            os._exit(77)
    Pipeline(repository, report_service(repository), ControlledAdapter(), hook).execute(task)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
