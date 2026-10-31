"""上传/确认业务流程：注入存储和处理函数，业务规则可脱离 HTTP 验证。"""

from app.chunking import VERSIONS
from app.domain_errors import BusinessError


def confirmed_result(report_id, member_id, *, already=False, **extra):
    return {"status": "confirmed", "report_id": report_id, "member_id": member_id,
            "already_confirmed": already, "chunk_versions": list(VERSIONS.values()), **extra}


def replay_result(previous, review, report_id, member_id):
    if previous is None:
        return None
    if previous.canonical() != review.canonical():
        raise BusinessError("already_confirmed_changed", "此报告已确认；本次提交与已保存内容不同。首版确认后不覆盖，请用新上传修正。")
    return confirmed_result(report_id, member_id, already=True)


class ReportService:
    def __init__(self, repository, drafts, inspector, extractor, indexer, member_checker=None):
        self.repository, self.drafts = repository, drafts
        self.inspector, self.extractor, self.indexer = inspector, extractor, indexer
        self.member_checker = member_checker or repository.require_member

    def create_draft(self, member_id, filename, data):
        self.member_checker(member_id)
        extension = self.inspector(data, filename)
        extracted = self.extractor(data, extension)
        draft = self.drafts.create(member_id, filename, data, extension, extracted)
        return {**draft, "status": "draft", "file_url": f"/api/ingest/{draft['id']}/file?member_id={member_id}"}

    def get_draft(self, report_id, member_id):
        draft, _ = self.drafts.load(report_id, member_id)
        previous = self.repository.find_review(report_id, member_id)
        result = {**draft, "status": "confirmed" if previous else "draft",
                  "extracted_text": draft["raw_text"],
                  "file_url": f"/api/ingest/{report_id}/file?member_id={member_id}"}
        if previous:
            result.update(previous.model_dump(mode="json", exclude={"reviewed"}))
        return result

    def original(self, report_id, member_id):
        return self.drafts.load(report_id, member_id)

    def confirm(self, report_id, member_id, review):
        _, original = self.drafts.load(report_id, member_id)
        self.member_checker(member_id)
        previous = self.repository.find_review(report_id, member_id)
        result = replay_result(previous, review, report_id, member_id)
        if result:
            return result
        # 外部调用先结束；不在数据库写事务内等待模型。
        chunks, model, dimensions, usage = self.indexer(review.raw_text)
        with self.repository.confirmation(report_id) as transaction:
            result = replay_result(transaction.review(report_id, member_id), review, report_id, member_id)
            if result:
                return result
            transaction.save(report_id, member_id, review, self.drafts.relative_original(original),
                             chunks, model, dimensions)
        return confirmed_result(report_id, member_id, chunks_written=len(chunks),
                                metrics_written=len(review.metrics), **usage)
