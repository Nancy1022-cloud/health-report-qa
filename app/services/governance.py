"""M10 可靠写入：外部准备 -> 加锁复查 -> 正文/指标/索引/版本/回执同事务提交。"""

import hashlib

from app.chunking import VERSIONS
from app.data_quality import fingerprint, review_issues
from app.domain_errors import BusinessError
from app.observability import observed, stage
from app.services.reports import ReportService, confirmed_result


class ConsistentReportService(ReportService):
    @observed("report.create_draft")
    def create_draft(self, member_id, filename, data, idempotency_key=None):
        self.member_checker(member_id)
        extension = self.inspector(data, filename)
        file_hash = hashlib.sha256(data).hexdigest()
        with self.repository.upload_lock(member_id, file_hash, idempotency_key) as conn:
            previous = self.repository.replay(conn, member_id, "upload", idempotency_key, file_hash)
            source = self.repository.source(conn, member_id, file_hash)
            if previous or source:
                report_id = previous["id"] if previous else source["id"]
                result = self.get_draft(report_id, member_id)
                if previous is None:
                    with conn.transaction():
                        self.repository.receipt(conn, member_id, "upload", idempotency_key, file_hash, {"id": report_id})
                return {**result, "upload_reused": True}
            with stage("report.extract"):
                extracted = self.extractor(data, extension)
            with stage("report.draft_store"):
                draft = self.drafts.create(member_id, filename, data, extension, extracted)
            _, original = self.drafts.load(draft["id"], member_id)
            # 文件先完整写入，再发布数据库元信息。提交结果不明时保留原件，不能删掉可能已被引用的文件。
            with conn.transaction():
                self.repository.register_source(conn, draft, self.drafts.relative_original(original))
                self.repository.receipt(conn, member_id, "upload", idempotency_key, file_hash, {"id": draft["id"]})
        return {**draft, "status": "draft", "upload_reused": False,
                "file_url": f"/api/ingest/{draft['id']}/file?member_id={member_id}"}

    def get_draft(self, report_id, member_id):
        draft, _ = self.drafts.load(report_id, member_id)
        row = self.repository.find_current(report_id, member_id)
        result = {**draft, "status": "confirmed" if row else "draft", "extracted_text": draft["raw_text"],
                  "file_url": f"/api/ingest/{report_id}/file?member_id={member_id}"}
        if row:
            result.update(row["snapshot"], report_version=row["data_version"], index_version=row["index_version"], index_state=row["index_state"])
        return result

    @observed("report.confirm")
    def confirm(self, report_id, member_id, review, idempotency_key=None):
        return self._commit(report_id, member_id, review, "confirm", idempotency_key)

    @observed("report.revise")
    def revise(self, report_id, member_id, request, idempotency_key=None):
        return self._commit(report_id, member_id, request.review, "revise", idempotency_key,
                            request.expected_version, request.actor, request.reason, request.rebuild_index)

    def _result(self, operation, report_id, member_id, version, *, reused, changed, usage=None):
        fields = dict(report_version=version, index_version=version, index_state="ready", replayed=reused)
        if operation == "confirm":
            return confirmed_result(report_id, member_id, already=reused, **fields, **(usage or {}))
        return {"status": "confirmed", "report_id": report_id, "member_id": member_id,
                **fields, "changed": changed, **(usage or {})}

    def _precheck(self, conn, operation, report_id, member_id, review, key, request_hash, expected, force):
        previous = self.repository.replay(conn, member_id, operation, key, request_hash)
        if previous is not None:
            previous = {**previous, "replayed": True}
            if operation == "confirm":
                previous["already_confirmed"] = True
            return previous
        row = self.repository.current(conn, report_id, member_id, required=operation == "revise")
        if operation == "confirm" and row:
            if row["index_state"] != "ready" or row["index_version"] != row["data_version"]:
                raise BusinessError("version_conflict", "当前索引不可用，请按当前版本显式重建。")
            if row["snapshot"] != review.canonical():
                raise BusinessError("already_confirmed_changed", "已确认内容不同；请使用报告修订入口并提供当前版本。")
            result = self._result(operation, report_id, member_id, row["data_version"], reused=True, changed=False)
        elif operation == "revise":
            if row["data_version"] != expected:
                raise BusinessError("version_conflict", "报告已更新，请重新读取后再修订。")
            if row["snapshot"] != review.canonical() or force:
                return None
            if row["index_state"] != "ready" or row["index_version"] != row["data_version"]:
                raise BusinessError("version_conflict", "索引不可用，请选择重建索引。")
            result = self._result(operation, report_id, member_id, row["data_version"], reused=False, changed=False)
        else:
            return None
        self.repository.receipt(conn, member_id, operation, key, request_hash, result)
        return result

    def _commit(self, report_id, member_id, review, operation, key, expected=None,
                actor="local_user", reason="首次人工确认", force=False):
        if operation == "revise" and not report_id.startswith("upload_"):
            row = self.repository.find_current(report_id, member_id, required=True)
            draft, original, file_path = None, None, row["file_path"]
        else:
            draft, original = self.drafts.load(report_id, member_id)
            file_path = self.drafts.relative_original(original)
        self.member_checker(member_id)
        if any(i["severity"] == "error" for i in review_issues(review)):
            raise BusinessError("quality_rule_violation", "指标名、编码或单位不匹配；请保留草稿并对照原件复核。")
        request_hash = fingerprint({"report_id": report_id, "review": review.canonical(), "expected_version": expected,
                                    "actor": actor, "reason": reason, "rebuild_index": force})
        with self.repository.write(report_id, member_id, operation, key) as conn:
            existing = self._precheck(conn, operation, report_id, member_id, review, key, request_hash, expected, force)
            if existing is not None:
                return existing
        with stage("report.index_prepare"):
            prepared = self.indexer(review.raw_text)
        chunks, _, dimensions, usage = prepared
        if {c["chunk_version"] for c in chunks} != set(VERSIONS.values()) or any(
                c["content"] != review.raw_text[c["start_char"]:c["end_char"]] or len(c["vector"]) != dimensions for c in chunks):
            raise BusinessError("invalid_embedding", "索引准备不完整，未修改已保存版本。")
        # 原件在外部调用后再核对一次；不接受准备期间已经变更的文件。
        if draft is not None:
            self.drafts.load(report_id, member_id)
        with stage("report.confirm_transaction"), self.repository.write(report_id, member_id, operation, key) as conn:
            existing = self._precheck(conn, operation, report_id, member_id, review, key, request_hash, expected, force)
            if existing is not None:
                return existing
            version = 1 if operation == "confirm" else expected + 1
            if draft is not None:
                self.repository.register_source(conn, draft, file_path)
            self.repository.save_version(conn, report_id, member_id, review, prepared,
                                         file_path, version, actor, reason,
                                         "confirmation" if version == 1 else "revision")
            result = self._result(operation, report_id, member_id, version, reused=False, changed=True, usage=usage)
            self.repository.receipt(conn, member_id, operation, key, request_hash, result)
        return result
