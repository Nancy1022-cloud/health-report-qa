"""M8 兼容入口和组装：旧 CLI/检查仍可调用，业务流程已移到 services/reports.py。"""

from app.clients.report_processing import prepare_chunks as _prepare_chunks
from app.db import connect_db
from app.http_errors import business_errors
from app.rag import embed_texts, embedding_config
from app.report_extract import extract_report, inspect_file
from app.repositories.drafts import DraftRepository, storage_root
from app.repositories.reports import PostgresReportRepository, existing_review as _existing_review
from app.services.reports import ReportService, confirmed_result, replay_result
from app.services.governance import ConsistentReportService
from app.repositories.governance import GovernanceRepository


@business_errors
def existing_review(conn, report_id, member_id):
    return _existing_review(conn, report_id, member_id)


@business_errors
def member_exists(member_id):
    return PostgresReportRepository(connect_db).require_member(member_id)


@business_errors
def draft_path(report_id):
    return DraftRepository(storage_root).path(report_id)


@business_errors
def load_draft(report_id, member_id):
    return DraftRepository(storage_root).load(report_id, member_id)


@business_errors
def prepare_chunks(raw_text):
    return _prepare_chunks(raw_text, config=embedding_config, embedder=embed_texts)


def make_report_service():
    repository = GovernanceRepository(connect_db)
    return ConsistentReportService(repository, DraftRepository(storage_root), inspect_file, extract_report,
                         prepare_chunks, member_checker=member_exists)


@business_errors
def check_existing(conn, report_id, member_id, review):
    return replay_result(existing_review(conn, report_id, member_id), review, report_id, member_id)


@business_errors
def create_draft(member_id, filename, data):
    return make_report_service().create_draft(member_id, filename, data)


@business_errors
def get_draft(report_id, member_id):
    return make_report_service().get_draft(report_id, member_id)


@business_errors
def confirm_report(report_id, member_id, review):
    return make_report_service().confirm(report_id, member_id, review)
