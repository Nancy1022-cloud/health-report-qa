"""接受任务只登记输入；模型请求由独立 worker 执行。"""

import hashlib
from uuid import uuid4

from app.data_quality import fingerprint, review_issues
from app.domain_errors import BusinessError
from app.report_extract import inspect_file
from app.task_files import TaskFiles
from app.task_policy import processing_config


class TaskService:
    def __init__(self, repository, report_service, files=None):
        self.repository, self.reports = repository, report_service
        self.files = files or TaskFiles(report_service.drafts)

    def upload(self, member_id, filename, data, key):
        self.reports.member_checker(member_id)
        extension = inspect_file(data, filename)
        file_hash = hashlib.sha256(data).hexdigest()
        request_hash = fingerprint({'file_sha256': file_hash})
        with self.repository.governance.upload_lock(member_id, file_hash, 'task:' + key) as conn:
            with conn.transaction():
                previous = self.repository.request(conn, member_id, 'extract', key, request_hash)
                if previous:
                    return self.repository.public(previous)
                previous = conn.execute("SELECT * FROM processing_tasks WHERE member_id=%s AND kind='extract' "
                    "AND payload->>'file_sha256'=%s AND status IN ('queued','running','succeeded') ORDER BY created_at DESC LIMIT 1",
                    (member_id, file_hash)).fetchone()
                if previous:
                    self.repository.remember(conn, previous, key, request_hash)
                    return self.repository.public(previous)
                source = self.repository.governance.source(conn, member_id, file_hash)
                if source:
                    result = self.reports.get_draft(source['id'], member_id)
                    payload = {'file_sha256': file_hash, 'source_reused': True}
                    task = self.repository.create(conn, member_id, source['id'], 'extract', request_hash, payload, result=result)
                else:
                    report_id = 'upload_' + uuid4().hex
                    metadata = self.files.stage(report_id, member_id, filename, data, extension)
                    payload = {'file_sha256': file_hash, 'input': metadata, 'config': processing_config()}
                    task = self.repository.create(conn, member_id, report_id, 'extract', request_hash, payload)
                self.repository.remember(conn, task, key, request_hash)
                return self.repository.public(task)

    def confirm(self, report_id, member_id, review, key):
        return self._write(report_id, member_id, review, key, 'confirm')

    def revise(self, report_id, member_id, request, key):
        return self._write(report_id, member_id, request.review, key, 'revise', request.expected_version,
                           request.actor, request.reason, request.rebuild_index)

    def _write(self, report_id, member_id, review, key, kind, expected=0,
               actor='local_user', reason='首次人工确认', force=False):
        self.reports.member_checker(member_id)
        if any(i['severity'] == 'error' for i in review_issues(review)):
            raise BusinessError('quality_rule_violation', '指标字段与单位不匹配，请对照原件复核。')
        request_hash = fingerprint({'report_id': report_id, 'review': review.canonical(), 'expected_version': expected,
                                    'actor': actor, 'reason': reason, 'rebuild_index': force})
        with self.repository.governance.write(report_id, member_id, 'task:' + kind, key) as conn:
            previous = self.repository.request(conn, member_id, kind, key, request_hash)
            if previous:
                return self.repository.public(previous)
            identical = conn.execute("SELECT * FROM processing_tasks WHERE report_id=%s AND member_id=%s AND kind=%s "
                "AND fingerprint=%s AND status IN ('queued','running','succeeded') ORDER BY created_at DESC LIMIT 1",
                (report_id, member_id, kind, request_hash)).fetchone()
            if identical:
                self.repository.remember(conn, identical, key, request_hash)
                return self.repository.public(identical)
            current = self.repository.governance.current(conn, report_id, member_id, required=kind == 'revise')
            reused = None
            if kind == 'confirm' and current:
                if current['snapshot'] != review.canonical():
                    raise BusinessError('already_confirmed_changed', '此报告已确认其他内容，请通过修订入口更新。')
                if current['index_state'] != 'ready':
                    raise BusinessError('version_conflict', '当前索引不可用，请显式重建。')
                reused = self.reports._result(kind, report_id, member_id, current['data_version'], reused=True, changed=False)
            if kind == 'revise' and current['data_version'] != expected:
                raise BusinessError('version_conflict', '报告已更新，请重新读取后再修订。')
            if kind == 'revise' and current['snapshot'] == review.canonical() and not force and current['index_state'] == 'ready':
                reused = self.reports._result(kind, report_id, member_id, expected, reused=False, changed=False)
            if report_id.startswith('upload_'):
                draft, original = self.reports.drafts.load(report_id, member_id)
                file_path = self.reports.drafts.relative_original(original)
                source_hash = draft['file_sha256']
            else:
                if current is None:
                    raise BusinessError('report_not_found', '报告不存在。')
                file_path, source_hash = current['file_path'], None
            payload = {'review': review.model_dump(mode='json'), 'expected_version': expected, 'actor': actor,
                       'reason': reason, 'rebuild_index': force, 'file_path': file_path, 'file_sha256': source_hash,
                       'config': processing_config()}
            task = self.repository.create(conn, member_id, report_id, kind, request_hash, payload, result=reused)
            self.repository.remember(conn, task, key, request_hash)
            return self.repository.public(task)
