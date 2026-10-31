"""原件先持久化；草稿由已保存的抽取结果确定，不在重试中生成新 ID。"""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from app.data_quality import fingerprint
from app.domain_errors import BusinessError
from app.repositories.drafts import DraftRepository


def atomic_write(path, data):
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class TaskFiles:
    def __init__(self, drafts=None):
        self.drafts = drafts or DraftRepository()

    def stage(self, report_id, member_id, filename, data, extension):
        folder = self.drafts.path(report_id)
        folder.mkdir(parents=True, exist_ok=False)
        metadata = {'id': report_id, 'member_id': member_id, 'extension': extension,
                    'filename': Path(filename.replace('\\', '/')).name[:200],
                    'file_sha256': hashlib.sha256(data).hexdigest()}
        atomic_write(folder / ('source' + extension), data)
        atomic_write(folder / 'task_input.json', json.dumps(metadata, ensure_ascii=False).encode())
        return metadata

    def read(self, metadata):
        folder = self.drafts.path(metadata['id'])
        saved = json.loads((folder / 'task_input.json').read_text(encoding='utf-8'))
        data = (folder / ('source' + metadata['extension'])).read_bytes()
        if saved != metadata or hashlib.sha256(data).hexdigest() != metadata['file_sha256']:
            raise BusinessError('source_changed', '任务原件或成员归属已改变。')
        return data

    def publish(self, metadata, extracted):
        self.read(metadata)
        path = self.drafts.path(metadata['id']) / 'draft.json'
        draft = {**metadata, **extracted}
        if path.exists():
            if fingerprint(json.loads(path.read_text(encoding='utf-8'))) != fingerprint(draft):
                raise BusinessError('source_changed', '已有草稿与任务结果不一致。')
        else:
            atomic_write(path, json.dumps(draft, ensure_ascii=False, indent=2).encode())
        return draft
