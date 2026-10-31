"""M12: attempt spans, persisted separately from business writes; missing spans stay unknown."""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

active = ContextVar('health_task_measurement', default=None)
logger = logging.getLogger(__name__)


@contextmanager
def timed(name, category='processing', **detail):
    recorder = active.get()
    if recorder is None:
        yield
        return
    start, identifier = time.perf_counter(), uuid4().hex
    parent = recorder['stack'][-1] if recorder['stack'] else None
    recorder['stack'].append(identifier)
    outcome = 'ok'
    try:
        yield
    except BaseException:
        outcome = 'error'
        raise
    finally:
        recorder['stack'].pop()
        row = {'span_id': identifier, 'parent_id': parent, 'name': name, 'category': category,
               'start_ms': round((start - recorder['start']) * 1000, 3),
               'duration_ms': round((time.perf_counter() - start) * 1000, 3),
               'outcome': outcome, 'detail': detail}
        if len(recorder['spans']) < 2048:
            recorder['spans'].append(row)
        else:
            recorder['dropped_spans'] += 1


@contextmanager
def attempt(repository, task):
    recorder = {'start': time.perf_counter(), 'stack': [], 'spans': [], 'dropped_spans': 0}
    token = active.set(recorder)
    outcome = 'returned'
    try:
        with timed('pipeline', 'attempt'):
            yield
    except BaseException:
        outcome = 'interrupted'
        raise
    finally:
        duration_ms = round((time.perf_counter() - recorder['start']) * 1000, 3)
        active.reset(token)
        # This event does not grant commit rights or change a task's state.
        # A process killed before this point has no complete timing event.
        detail = {'version': 'm12.spans.v1', 'duration_ms': duration_ms,
                  'outcome': outcome, 'spans': recorder['spans'], 'dropped_spans': recorder['dropped_spans']}
        try:
            with repository.connection_factory() as conn:
                repository.event(conn, task, 'attempt_timing', **detail)
        except Exception:
            logger.warning('task timing unavailable task_id=%s attempt=%s', task['id'], task['attempts'])
