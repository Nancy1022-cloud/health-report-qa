"""只负责草稿和原件的文件持久化，不调用模型或数据库。"""

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from app.domain_errors import BusinessError


def storage_root():
    return Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()


class DraftRepository:
    def __init__(self, root_factory=storage_root):
        self.root_factory = root_factory

    def path(self, report_id):
        if not re.fullmatch(r"upload_[a-f0-9]{32}", report_id):
            raise BusinessError("draft_not_found", "上传草稿不存在。")
        return self.root_factory() / report_id

    def load(self, report_id, member_id):
        folder = self.path(report_id)
        try:
            draft = json.loads((folder / "draft.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise BusinessError("draft_not_found", "上传草稿不存在。") from None
        if draft["member_id"] != member_id:
            raise BusinessError("draft_not_found", "该成员下不存在此上传草稿。")
        original = folder / ("source" + draft["extension"])
        if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != draft["file_sha256"]:
            raise BusinessError("source_changed", "上传原件缺失或已改变，请重新上传。")
        return draft, original

    def create(self, member_id, filename, data, extension, extracted):
        report_id = "upload_" + uuid4().hex
        folder = self.path(report_id)
        folder.mkdir(parents=True, exist_ok=False)
        draft = {"id": report_id, "member_id": member_id, "extension": extension,
                 "filename": Path(filename.replace("\\", "/")).name[:200],
                 "file_sha256": hashlib.sha256(data).hexdigest(), **extracted}
        try:
            (folder / ("source" + extension)).write_bytes(data)
            (folder / "draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            shutil.rmtree(folder)
            raise
        return draft

    def relative_original(self, original):
        return str(original.relative_to(self.root_factory().parent))
