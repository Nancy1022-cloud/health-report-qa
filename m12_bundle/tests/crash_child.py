"""M12-only crash injection; no crash flags are added to production APIs."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))


def main():
    from app.repositories.tasks import TaskRepository
    from app.task_pipeline import Pipeline
    from app.task_worker import report_service, worker_connection
    from m12_bundle.environment import ControlledAdapter
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--point', required=True)
    args = parser.parse_args()
    repository = TaskRepository(worker_connection)
    with repository.connection() as conn:
        task = conn.execute('SELECT * FROM processing_tasks WHERE id=%s', (args.task_id,)).fetchone()
    if task['execution_token'] != args.token:
        raise RuntimeError('Synthetic crash token mismatch')
    def hook(point, **details):
        if point == args.point and details.get('stage', '').startswith('embedding_batch:'):
            os._exit(77)
    Pipeline(repository, report_service(repository), ControlledAdapter(), hook).execute(task)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
