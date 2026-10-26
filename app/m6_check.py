"""M6 离线检查；不访问真实数据库或模型服务。运行：python -m app.m6_check。"""

import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfWriter

from app import report_extract as extract, report_ingest as ingest
from app.upload_models import Review, code_for
from app.upload_routes import router

TEXT = "【生化检查】\n空腹血糖：5.70 mmol/L。\n【建议与备注】\n请保留本报告。"
METRIC = {"metric_code": "fasting_glucose", "metric_name": "空腹血糖", "value": "5.70",
          "unit": "mmol/L", "reference_range": None, "source_text": "空腹血糖：5.70 mmol/L。"}
BODY = {"title": "合成报告", "report_date": "2026-10-20", "report_type": "体检报告",
        "raw_text": TEXT, "metrics": [METRIC], "reviewed": True}


def png_bytes():
    out = io.BytesIO()
    Image.new("RGB", (120, 120), "white").save(out, format="PNG")
    return out.getvalue()


class ReviewChecks(unittest.TestCase):
    def test_decimal_and_canonical_roundtrip(self):
        review = Review.model_validate(BODY)
        self.assertEqual(review.metrics[0].value, "5.7")
        self.assertEqual(Review.model_validate_json(review.model_dump_json()).canonical(), review.canonical())

    def test_unchecked_and_nonboolean_confirmation_rejected(self):
        for value in [False, 1, "true"]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Review.model_validate({**BODY, "reviewed": value})

    def test_invalid_date_rejected(self):
        with self.assertRaises(ValidationError):
            Review.model_validate({**BODY, "report_date": "2026-02-30"})

    def test_duplicate_metric_rejected(self):
        with self.assertRaises(ValidationError):
            Review.model_validate({**BODY, "metrics": [METRIC, METRIC]})

    def test_wrong_value_and_unit_and_source_rejected(self):
        for delta in [{"value": "8.9"}, {"unit": "mg/dL"}, {"source_text": "空腹血糖：5.70 mmol/L，正常。"}]:
            with self.subTest(delta=delta), self.assertRaises(ValidationError):
                Review.model_validate({**BODY, "metrics": [{**METRIC, **delta}]})

    def test_inequality_not_turned_into_number(self):
        with self.assertRaises(ValidationError):
            Review.model_validate({**BODY, "metrics": [{**METRIC, "value": "<5.7"}]})

    def test_member_cannot_be_injected_into_confirmation(self):
        with self.assertRaises(ValidationError):
            Review.model_validate({**BODY, "member_id": "other_member"})

    def test_narrative_only_allowed_and_codes_stable(self):
        self.assertEqual(Review.model_validate({**BODY, "metrics": []}).metrics, [])
        self.assertEqual(code_for("空腹血糖"), "fasting_glucose")
        self.assertEqual(code_for("新指标"), code_for("新指标"))


class ExtractChecks(unittest.TestCase):
    def test_empty_unsupported_and_fake_image_rejected(self):
        for data, name in [(b"", "x.pdf"), (b"abc", "x.txt"), (b"abc", "x.jpg")]:
            with self.subTest(name=name), self.assertRaises(HTTPException):
                extract.inspect_file(data, name)

    def test_image_verified_and_prepared_as_data_url(self):
        data = png_bytes()
        self.assertEqual(extract.inspect_file(data, "../../x.png"), ".png")
        self.assertTrue(extract.image_data_url(data).startswith("data:image/jpeg;base64,"))

    def test_scanned_or_blank_pdf_not_silently_empty(self):
        out = io.BytesIO()
        writer = PdfWriter(); writer.add_blank_page(width=200, height=200); writer.write(out)
        with self.assertRaises(HTTPException) as context:
            extract.pdf_text(out.getvalue())
        self.assertEqual(context.exception.detail["code"], "pdf_needs_ocr")

    def test_text_pdf_model_failure_preserves_text_and_error(self):
        with patch.object(extract, "pdf_text", return_value=TEXT), patch.object(
                extract, "request_fields", side_effect=HTTPException(504, {"code": "extraction_timeout"})):
            result = extract.extract_report(b"%PDF-", ".pdf")
        self.assertEqual(result["raw_text"], TEXT)
        self.assertEqual(result["metrics"], [])
        self.assertIn("extraction_timeout", str(result["warnings"]))

    def test_pdf_model_cannot_replace_source_text(self):
        with patch.object(extract, "pdf_text", return_value=TEXT), patch.object(extract, "request_fields",
                return_value=({"raw_text": "模型改写", "metrics": []}, {})):
            self.assertEqual(extract.extract_report(b"%PDF-", ".pdf")["raw_text"], TEXT)

    def test_candidate_with_fabricated_source_is_dropped(self):
        raw = {k: v for k, v in METRIC.items() if k != "metric_code"}
        result = extract.normalize_fields({"metrics": [{**raw, "source_text": "不存在的原句"}]}, TEXT)
        self.assertFalse(result["metrics"])
        self.assertTrue(result["warnings"])


class UploadChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"UPLOAD_DIR": self.temp.name}); self.env.start()
        self.scope = patch.object(ingest, "member_exists"); self.scope.start()
        self.extractor = patch.object(ingest, "extract_report", return_value={
            "title": BODY["title"], "report_date": BODY["report_date"], "report_type": BODY["report_type"],
            "raw_text": TEXT, "metrics": [METRIC], "warnings": [], "model_info": {"model": "mock"}})
        self.extractor.start()
        app = FastAPI(); app.include_router(router); self.client = TestClient(app)

    def tearDown(self):
        self.extractor.stop(); self.scope.stop(); self.env.stop(); self.temp.cleanup()

    def draft(self):
        response = self.client.post("/api/ingest/upload", data={"member_id": "member_mom"},
                                    files={"file": ("../../report.png", png_bytes(), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_upload_is_draft_and_archives_source(self):
        draft = self.draft()
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["filename"], "report.png")
        saved, original = ingest.load_draft(draft["id"], "member_mom")
        self.assertEqual(saved["raw_text"], TEXT)
        self.assertEqual(original.read_bytes(), png_bytes())

    def test_wrong_member_and_traversal_cannot_open_source(self):
        draft = self.draft()
        response = self.client.get(f"/api/ingest/{draft['id']}/file?member_id=member_dad")
        self.assertEqual(response.status_code, 404)
        with self.assertRaises(HTTPException):
            ingest.draft_path("../other")

    def test_changed_original_rejected(self):
        draft = self.draft()
        folder = ingest.draft_path(draft["id"])
        (folder / "source.png").write_bytes(b"changed")
        with self.assertRaises(HTTPException) as context:
            ingest.load_draft(draft["id"], "member_mom")
        self.assertEqual(context.exception.detail["code"], "source_changed")

    def test_confirmation_validation_runs_before_embedding(self):
        draft = self.draft()
        with patch.object(ingest, "prepare_chunks") as prepare:
            response = self.client.post(f"/api/ingest/{draft['id']}/confirm?member_id=member_mom", json={**BODY, "reviewed": False})
        self.assertEqual(response.status_code, 422)
        prepare.assert_not_called()

    def test_prepare_reuses_vectors_and_keeps_original_offsets(self):
        with patch.object(ingest, "embedding_config", return_value=("mock", 3)), patch.object(
                ingest, "embed_texts", return_value=([[1.0, 0.0, 0.0]], {"total_tokens": 2})) as embed:
            chunks, _, _, usage = ingest.prepare_chunks(TEXT)
        self.assertEqual({c["chunk_version"] for c in chunks}, {"fixed200o40", "section200o40v1"})
        self.assertTrue(all(c["content"] == TEXT[c["start_char"]:c["end_char"]] for c in chunks))
        self.assertEqual(embed.call_count, len({c["content"] for c in chunks}))
        self.assertTrue(all(len(c.args[0]) == 1 for c in embed.call_args_list))

    def test_embedding_failure_has_no_database_inserts(self):
        draft = self.draft()
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = None
        with patch.object(ingest, "connect_db", return_value=connection), patch.object(
                ingest, "prepare_chunks", side_effect=HTTPException(504, "模拟超时")):
            with self.assertRaises(HTTPException):
                ingest.confirm_report(draft["id"], "member_mom", Review.model_validate(BODY))
        self.assertTrue(all(call.args[0].lstrip().upper().startswith("SELECT")
                            for call in connection.execute.call_args_list))
        ingest.load_draft(draft["id"], "member_mom")

    def test_repeat_confirmation_returns_without_embedding(self):
        draft = self.draft()
        connection = MagicMock(); connection.__enter__.return_value = connection
        with patch.object(ingest, "connect_db", return_value=connection), patch.object(
                ingest, "existing_review", return_value=Review.model_validate(BODY)), patch.object(
                ingest, "prepare_chunks") as prepare:
            result = ingest.confirm_report(draft["id"], "member_mom", Review.model_validate(BODY))
        self.assertTrue(result["already_confirmed"])
        prepare.assert_not_called()

    def test_different_payload_cannot_overwrite_confirmed_report(self):
        draft = self.draft()
        connection = MagicMock(); connection.__enter__.return_value = connection
        with patch.object(ingest, "connect_db", return_value=connection), patch.object(
                ingest, "existing_review", return_value=Review.model_validate(BODY)), patch.object(
                ingest, "prepare_chunks") as prepare:
            with self.assertRaises(HTTPException) as context:
                ingest.confirm_report(draft["id"], "member_mom", Review.model_validate({**BODY, "title": "修改标题"}))
        self.assertEqual(context.exception.status_code, 409)
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)