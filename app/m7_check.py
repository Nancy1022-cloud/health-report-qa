"""验收脚本的离线检查：不连数据库、不调用模型。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.m7_data import build_bundle, digest, validate_bundle
from app.m7_eval import gold_covered, inspect_result, report, run_eval, source_valid, summarize, write_json


def sample():
    bundle = build_bundle()
    reports = {r["id"]: r for r in bundle["reports"]}
    case = bundle["cases"][0]
    r = reports[case["gold"][0]["report_id"]]
    m = next(m for m in r["metrics"] if m["metric_code"] == "fasting_glucose")
    source = {**m, "source_id": "metric:42", "member_id": r["member_id"], "report_id": r["id"],
              "report_date": r["report_date"], "title": r["title"]}
    trace = [{"step": "agent_config", "chunk_version": "section200o40v1", "retrieval_mode": "hybrid"},
             {"step": "agent_model_request"},
             {"step": "tool_request", "origin": "model", "name": "get_metric_trend", "arguments": "{}"},
             {"step": "tool_result", "executed": True, "name": "get_metric_trend", "result": {"status": "ok", "items": [source]}},
             {"step": "agent_model_request"}]
    response = {"query_id": "q1", "status": "answered", "answer": "5.30 mmol/L。[[metric:42]]",
                "evidence": [source], "citations": [source], "tool_calls": 1, "llm_calls": 2,
                "supplemental_calls": 0, "revision_calls": 0,
                "token_usage": {"totals": {"total_tokens": 50}, "missing_usage": False}}
    log = {"id": "q1", "trace": trace, "token_usage": response["token_usage"]}
    return bundle, reports, case, response, log


class FixtureChecks(unittest.TestCase):
    def test_fixture_size_and_literal_evidence(self):
        bundle = build_bundle()
        validate_bundle(bundle)
        self.assertEqual(len(bundle["reports"]), 12)
        self.assertEqual(sum(len(r["metrics"]) for r in bundle["reports"]), 36)
        self.assertEqual(len(bundle["cases"]), 20)
        self.assertEqual(len(bundle["retrieval_cases"]), 8)

    def test_materials_are_deterministic(self):
        self.assertEqual(digest(build_bundle()), digest(build_bundle()))

    def test_does_not_reuse_existing_members(self):
        self.assertTrue(all(r["member_id"].startswith("m7_member_") for r in build_bundle()["reports"]))

    def test_unit_mismatch_case_has_no_expected_conversion(self):
        bundle = build_bundle()
        self.assertEqual(bundle["reports"][10]["metrics"][0]["unit"], "mmol/L")
        self.assertEqual(bundle["reports"][11]["metrics"][0]["unit"], "mg/dL")


class ScoringChecks(unittest.TestCase):
    def setUp(self):
        self.bundle, self.reports, self.case, self.response, self.log = sample()

    def score(self, status=200):
        return inspect_result(self.case, status, self.response, self.log, self.reports)

    def test_valid_trace_and_sources_pass_structure(self):
        self.assertTrue(self.score()["auto_pass"])

    def test_503_is_not_successful_no_answer(self):
        self.response = {"detail": {"code": "tool_execution_failed"}}
        self.assertFalse(self.score(503)["auto_pass"])

    def test_expected_404_requires_real_trace_and_no_calls(self):
        self.case = self.bundle["cases"][17]
        self.log = {"trace": [{"step": "agent_error", "code": "request_404"}]}
        self.assertTrue(self.score(404)["auto_pass"])
        self.log = None
        self.assertFalse(self.score(404)["auto_pass"])

    def test_scope_rejection_cannot_execute_tools(self):
        self.case = self.bundle["cases"][17]
        self.assertFalse(self.score(404)["auto_pass"])

    def test_fabricated_citation_fails(self):
        self.response["answer"] = "5.30。[[metric:999999]]"
        self.assertFalse(self.score()["auto_pass"])

    def test_unacquired_evidence_fails(self):
        self.log["trace"] = [e for e in self.log["trace"] if e.get("step") != "tool_result"]
        self.assertFalse(self.score()["auto_pass"])

    def test_other_member_fails(self):
        self.response["evidence"][0]["member_id"] = "m7_member_b"
        self.assertFalse(self.score()["auto_pass"])

    def test_wrong_metric_value_fails_even_with_known_source_id(self):
        self.response["evidence"][0]["value"] = "99.00"
        self.assertFalse(self.score()["auto_pass"])

    def test_wrong_unit_fails(self):
        self.response["evidence"][0]["unit"] = "mg/dL"
        self.assertFalse(self.score()["auto_pass"])

    def test_counts_must_match_trace(self):
        self.response["tool_calls"] = 0
        self.assertFalse(self.score()["auto_pass"])

    def test_requested_retrieval_configuration_must_match_trace(self):
        self.log["trace"][0]["retrieval_mode"] = "vector"
        self.assertFalse(self.score()["auto_pass"])

    def test_total_tool_budget_is_six(self):
        self.log["trace"] += [{"step": "tool_request", "origin": "model"}] * 6
        self.response["tool_calls"] = 7
        self.assertFalse(self.score()["auto_pass"])

    def test_two_supplements_fail(self):
        self.log["trace"] += [{"step": "tool_request", "origin": "evidence_controller"}] * 2
        self.response.update(tool_calls=3, supplemental_calls=2)
        self.assertFalse(self.score()["auto_pass"])

    def test_two_revisions_fail(self):
        self.log["trace"] += [{"step": "m5_answer_revision"}] * 2
        self.response["revision_calls"] = 2
        self.assertFalse(self.score()["auto_pass"])

    def test_partial_required_evidence_is_not_full_coverage(self):
        self.case = self.bundle["cases"][1]
        self.assertFalse(self.score()["checks"]["required_evidence_covered"])

    def test_adjacent_chunk_intervals_jointly_cover_gold(self):
        gold = self.bundle["cases"][5]["gold"][0]
        raw = self.reports[gold["report_id"]]["raw_text"]
        start = raw.index(gold["text"])
        mid = start + len(gold["text"]) // 2
        rows = [{"source_id": "chunk:x", "report_id": gold["report_id"], "start_char": start, "end_char": mid},
                {"source_id": "chunk:y", "report_id": gold["report_id"], "start_char": mid, "end_char": start+len(gold["text"])}]
        self.assertTrue(gold_covered(gold, rows, self.reports))
        rows[1]["start_char"] += 1
        self.assertFalse(gold_covered(gold, rows, self.reports))

    def test_scope_correct_citation_is_not_semantic_answer_validation(self):
        self.response["answer"] = "空腹血糖999 mmol/L。[[metric:42]]"
        self.assertTrue(self.score()["auto_pass"])
        # 故意验证机器检查的边界：语义错误必须由人工answer_correct判为false。


class ReportingChecks(unittest.TestCase):
    def make_run(self):
        _, reports, case, response, log = sample()
        item = {"case_id": case["id"], "expected_http": 200, "http_status": 200, "response": response,
                "log": log, "elapsed_ms": 100, "automatic": inspect_result(case, 200, response, log, reports)}
        return {"run_id": "test", "label": "test", "expected_cases": 20, "valid_frozen_run": True, "items": [item]}

    def test_unreviewed_is_not_manual_pass(self):
        summary = summarize(self.make_run())
        self.assertEqual(summary["manual_completed"], "0/20")
        self.assertEqual(summary["manual_all_three_pass"], "0/20")

    def test_partial_run_keeps_original_denominator(self):
        self.assertEqual(summarize(self.make_run())["automatic_pass"], "1/20")

    def test_incomplete_usage_remains_visible(self):
        run = self.make_run()
        run["items"][0]["response"]["token_usage"]["missing_usage"] = True
        summary = summarize(run)
        self.assertEqual(summary["observed_total_tokens"], 50)
        self.assertEqual(summary["requests_with_incomplete_usage"], 1)

    def test_manual_file_cannot_belong_to_other_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / "results.json", self.make_run())
            write_json(root / "manual_review.json", {"run_id": "other", "results_sha256": "other", "items": {}})
            with self.assertRaises(ValueError):
                report(root)

    def test_http_collection_preserves_failure_and_writes_result_files(self):
        bundle = build_bundle()
        bundle["cases"] = bundle["cases"][:1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir()
            write_json(root / "data" / "eval_m7.json", {"cases": bundle["cases"]})

            def fake_request(base, path, body=None, timeout=150):
                if path == "/health":
                    return 200, {"status": "ok"}
                if path == "/openapi.json":
                    return 200, {"paths": {"/api/agent/query": {}, "/api/query/{query_id}/trace": {}}}
                return 503, {"detail": {"code": "tool_execution_failed"}}

            with patch("app.m7_eval.prepare", return_value=bundle), patch("app.m7_eval.snapshot", return_value={"hash": "fixed"}), \
                    patch("app.m7_eval.APP_DIR", root), patch("app.m7_eval.request_json", side_effect=fake_request):
                run_dir = run_eval("http://localhost", "offline-failure-test", root / "runs")
            run = json.loads((run_dir / "results.json").read_text())
            self.assertEqual(run["items"][0]["http_status"], 503)
            self.assertFalse(run["items"][0]["automatic"]["auto_pass"])
            self.assertTrue((run_dir / "manual_review.json").is_file())
            self.assertTrue((run_dir / "summary.md").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)