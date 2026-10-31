"""可恢复的解析/索引流水线；最终业务数据和任务成功标记同事务提交。"""

import hashlib
import time

from app.chunking import VERSIONS
from app.clients.report_processing import prepare_chunks
from app.clients.task_processing import ModelAdapter
from app.domain_errors import BusinessError
from app.report_extract import image_data_url, normalize_fields, pdf_text
from app.task_files import TaskFiles
from app.task_policy import LostExecution, TaskFailure, classify, processing_config
from app.upload_models import MAX_TEXT, Review


class TaskContext:
    def __init__(self, repository, task, adapter=None, hook=None):
        self.repository, self.task, self.token = repository, task, task['execution_token']
        self.adapter = adapter or ModelAdapter()
        self.hook = hook or (lambda point, **detail: None)

    def local(self, stage, function):
        saved = self.repository.artifact(self.task, self.token, stage)
        if saved is not None:
            return saved
        result = function()
        self.repository.save_artifact(self.task, self.token, stage, result)
        self.hook('stage_saved', stage=stage)
        return result

    def call(self, stage, prefix, function):
        saved = self.repository.artifact(self.task, self.token, stage)
        if saved is not None:
            return saved['data'], saved['usage']
        while True:
            reservation = self.repository.reserve_call(self.task, self.token, stage, prefix)
            if 'wait' not in reservation:
                break
            time.sleep(min(1, max(0.01, reservation['wait'])))
        self.hook('before_external', stage=stage, call_id=reservation['id'])
        try:
            data, usage = function(reservation['timeout'])
        except Exception as error:
            failure = classify(error)
            self.repository.finish_call(self.task, self.token, reservation['id'], error=failure.code,
                                        usage=getattr(error, 'usage', None))
            raise failure from None
        # 这里是无法完全消除的中断窗口；恢复可能重发，之前的预留不会退回。
        self.hook('external_returned', stage=stage, call_id=reservation['id'])
        self.repository.finish_call(self.task, self.token, reservation['id'], stage=stage,
                                    usage=usage, result={'data': data, 'usage': usage})
        self.hook('stage_saved', stage=stage)
        return data, usage


class Pipeline:
    def __init__(self, repository, report_service, adapter=None, hook=None):
        self.repository, self.reports, self.adapter, self.hook = repository, report_service, adapter, hook
        self.files = TaskFiles(report_service.drafts)

    def execute(self, task):
        ctx = TaskContext(self.repository, task, self.adapter, self.hook)
        try:
            if task['payload']['config'] != processing_config():
                raise TaskFailure('processing_config_changed')
            if task['kind'] == 'extract':
                self.extract(ctx)
            else:
                self.write(ctx)
            return True
        except LostExecution:
            # 结果提交权限已撤回；不能用旧 token 改写任务终态。
            return False
        except Exception as error:
            try:
                self.repository.fail(task, ctx.token, classify(error))
            except LostExecution:
                return False
            return False

    def extract(self, ctx):
        task, payload = ctx.task, ctx.task['payload']
        metadata = payload['input']
        data = self.files.read(metadata)
        if metadata['extension'] == '.pdf':
            text = ctx.local('pdf_text', lambda: {'text': pdf_text(data)})['text']
            fields, usage = ctx.call('fields', 'LLM', lambda timeout: ctx.adapter.fields(
                'LLM', '请从以下原文抽取候选字段：\n' + text, timeout))
        else:
            fields, usage = ctx.call('fields', 'VISION', lambda timeout: ctx.adapter.fields('VISION', [
                {'type': 'text', 'text': '逐字转录这张报告，并输出约定的 JSON。'},
                {'type': 'image_url', 'image_url': {'url': image_data_url(data)}},
            ], timeout))
            text = fields['fields'].get('raw_text')
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise TaskFailure('invalid_image_text')
        normalized = ctx.local('normalized', lambda: {**normalize_fields(fields['fields'], text),
                                'model_info': {'model': fields['model'], 'usage': usage}})
        draft = self.files.publish(metadata, normalized)
        original = self.reports.drafts.path(task['report_id']) / ('source' + metadata['extension'])
        ctx.hook('before_commit', kind='extract')
        with self.repository.execution(task, ctx.token) as (conn, current):
            # 原件再次核对，防止准备期间文件被替换。
            self.files.read(metadata)
            self.repository.governance.register_source(conn, draft, self.reports.drafts.relative_original(original))
            result = {**draft, 'status': 'draft', 'upload_reused': False,
                      'file_url': f"/api/ingest/{task['report_id']}/file?member_id={task['member_id']}"}
            self.repository.complete(conn, current, result)
        ctx.hook('after_commit', kind='extract')

    def write(self, ctx):
        task, payload = ctx.task, ctx.task['payload']
        review = Review.model_validate(payload['review'])
        if task['report_id'].startswith('upload_'):
            draft, _ = self.reports.drafts.load(task['report_id'], task['member_id'])
            if draft['file_sha256'] != payload['file_sha256']:
                raise BusinessError('source_changed', '任务原件已改变。')

        def embed(texts):
            name = 'embedding:' + hashlib.sha256(texts[0].encode()).hexdigest()
            result, usage = ctx.call(name, 'EMBEDDING', lambda timeout: ctx.adapter.embedding(texts[0], timeout))
            return [result['vector']], usage or {}

        def build():
            config = payload['config']
            value = prepare_chunks(review.raw_text, config=lambda: (
                config['EMBEDDING_MODEL'], int(config['EMBEDDING_DIMENSIONS'] or '1024')), embedder=embed)
            return {'prepared': list(value)}

        prepared = ctx.local('index_prepared', build)['prepared']
        chunks, model, dimensions, usage = prepared
        if {c['chunk_version'] for c in chunks} != set(VERSIONS.values()) or any(
                c['content'] != review.raw_text[c['start_char']:c['end_char']] or len(c['vector']) != dimensions for c in chunks):
            raise TaskFailure('invalid_embedding')
        ctx.hook('before_commit', kind=task['kind'])
        with self.repository.execution(task, ctx.token) as (conn, current):
            row = self.repository.governance.current(conn, task['report_id'], task['member_id'], required=task['kind'] == 'revise')
            changed = True
            if task['kind'] == 'confirm' and row:
                if row['snapshot'] != review.canonical() or row['index_state'] != 'ready':
                    raise BusinessError('version_conflict', '报告在任务执行期间已确认或更新。')
                version, changed = row['data_version'], False
            else:
                if task['kind'] == 'revise' and row['data_version'] != payload['expected_version']:
                    raise BusinessError('version_conflict', '报告在任务执行期间已经更新。')
                version = 1 if task['kind'] == 'confirm' else payload['expected_version'] + 1
                if task['report_id'].startswith('upload_'):
                    draft, _ = self.reports.drafts.load(task['report_id'], task['member_id'])
                    if draft['file_sha256'] != payload['file_sha256']:
                        raise BusinessError('source_changed', '任务原件已改变。')
                    self.repository.governance.register_source(conn, draft, payload['file_path'])
                self.repository.governance.save_version(conn, task['report_id'], task['member_id'], review, prepared,
                    payload['file_path'], version, payload['actor'], payload['reason'], 'confirmation' if version == 1 else 'revision')
            result = self.reports._result(task['kind'], task['report_id'], task['member_id'], version,
                                          reused=not changed, changed=changed)
            # 用量详见 processing_calls；不把本次阶段缓存用量重复加到“真实调用”中。
            self.repository.complete(conn, current, result)
        ctx.hook('after_commit', kind=task['kind'])
