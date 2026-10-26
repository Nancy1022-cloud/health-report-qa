"""M5 离线回归：模拟模型/工具返回；不访问模型服务、不读写真实数据库。"""

import asyncio
import copy
import json
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import patch

from fastapi import HTTPException

from app import agent
from app.evidence_guard import (EvidenceToolError, assess_coverage, resolve_answer,
                                select_supplement, simple_metric_answer, validate_final, validate_review)


SCOPE = {
    "members": [{"id": "member_dad", "name": "爸爸", "aliases": ["父亲"]}],
    "reports": [
        {"id": "r1", "member_id": "member_dad", "report_date": "2026-01-10", "title": "首次", "report_type": "体检"},
        {"id": "r2", "member_id": "member_dad", "report_date": "2026-07-10", "title": "复查", "report_type": "复查"}],
    "reports_by_member": {"member_dad": ["r1", "r2"]},
    "metric_catalog": [{"metric_code": "fasting_glucose", "metric_name": "空腹血糖"}],
}
C1 = {"source_id": "chunk:r1:0-30", "member_id": "member_dad", "report_id": "r1",
      "report_date": "2026-01-10", "content": "【建议与备注】建议三个月后复查。", "section": "建议与备注"}
C2 = {"source_id": "chunk:r2:0-30", "member_id": "member_dad", "report_id": "r2",
      "report_date": "2026-07-10", "content": "【建议与备注】继续规律作息，按医嘱复查。", "section": "建议与备注"}
METRIC = {"source_id": "metric:99", "member_id": "member_dad", "report_id": "r2",
          "report_date": "2026-07-10", "metric_code": "fasting_glucose", "metric_name": "空腹血糖",
          "value": "5.80", "unit": "mmol/L"}


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def final(answer, ids, status="answered"):
    return encode({"status": status, "answer": answer, "source_ids": ids})


def need(index, covered=True, **updates):
    item = C1 if index == 1 else C2
    result = {"need_id": f"n{index}", "kind": "report_text", "member_id": "member_dad",
              "report_id": item["report_id"], "topic": "复查建议", "metric_code": None,
              "query": "建议与备注 复查建议", "source_ids": [item["source_id"]] if covered else [],
              "supported": covered, "reason": "原文支持" if covered else "尚未取得该报告的建议原文"}
    result.update(updates)
    return result


def review(needs, supported=True, issues=None):
    return encode({"answer_supported": supported, "issues": issues or [], "needs": needs})


ONE = final("首次建议三个月后复查。[[chunk:r1:0-30]]", [C1["source_id"]])
BOTH = final("首次建议三个月后复查。[[chunk:r1:0-30]]\n复查建议规律作息、按医嘱复查。[[chunk:r2:0-30]]",
             [C1["source_id"], C2["source_id"]])
PART = final("首次建议三个月后复查。[[chunk:r1:0-30]] 第二份建议尚缺原文。", [C1["source_id"]], "partial")
EMPTY = final("本次没有足够证据。", [], "insufficient")


class PureChecks(unittest.TestCase):
    def test_m4_insufficient_normalization_kept(self):
        result = validate_final(final("相关原文。[[chunk:r1:0-30]]", [], "insufficient"), {C1["source_id"]: C1})
        self.assertEqual(result["citations"], [C1])

    def test_fabricated_and_mismatched_citations_rejected(self):
        for raw in [final("[[chunk:fake]]", [], "insufficient"), final("[[chunk:r1:0-30]]", ["metric:fake"]),
                    final("5.80", []), final("请澄清[[chunk:r1:0-30]]", [C1["source_id"]], "clarify")]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_final(raw, {C1["source_id"]: C1})

    def test_scope_and_requirement_freeze(self):
        first = validate_review(review([need(1), need(2, False)]), SCOPE)
        with self.assertRaises(ValueError):
            validate_review(review([need(1)]), SCOPE, first.needs)
        with self.assertRaises(ValueError):
            validate_review(review([need(1, member_id="member_mom")]), SCOPE)
        with self.assertRaises(ValueError):
            validate_review(review([need(1), need(1)]), SCOPE)

    def test_metadata_wrong_member_and_wrong_metric_cannot_cover(self):
        fixtures = [
            (need(1, source_ids=["report:r1"]), {"report:r1": {**C1, "source_id": "report:r1"}}),
            (need(1), {C1["source_id"]: {**C1, "member_id": "member_mom"}}),
            (need(1, kind="metric", metric_code="uric_acid", source_ids=[METRIC["source_id"]]),
             {METRIC["source_id"]: {**METRIC, "report_id": "r1"}}),
            (need(1, source_ids=["chunk:invented"]), {C1["source_id"]: C1}),
        ]
        for requirement, available in fixtures:
            with self.subTest(requirement=requirement):
                rows = assess_coverage(validate_review(review([requirement]), SCOPE), available)
                self.assertFalse(rows[0]["covered"])

    def test_one_precise_search_and_budget_zero(self):
        rows = assess_coverage(validate_review(review([need(1, False), need(2, False)]), SCOPE), {})
        args, _ = select_supplement(rows, 1)
        self.assertEqual(args["report_ids"], ["r1"])
        self.assertEqual(args["member_id"], "member_dad")
        self.assertIsNone(select_supplement(rows, 0)[0])
        metric = need(1, False, kind="metric", metric_code="uric_acid")
        metric_rows = assess_coverage(validate_review(review([metric]), SCOPE), {})
        self.assertIsNone(select_supplement(metric_rows, 6)[0])

    def test_combined_question_cannot_use_single_value_fast_path(self):
        history = [{"name": "get_metric_trend", "arguments": encode({"member_id": "member_dad", "latest_n": 1}),
                    "result": {"status": "ok", "items": [METRIC]}}]
        self.assertIsNone(simple_metric_answer("爸爸最近一次空腹血糖是多少并说明是否异常？", SCOPE,
                                              {METRIC["source_id"]: METRIC}, history, []))


class ControllerChecks(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, raw, script, *, available=None, tools_left=4, supplement=None, failures=None):
        available = copy.deepcopy(available if available is not None else {C1["source_id"]: C1})
        script = list(script)
        stages, executions, trace, history = [], [], [], []
        failures = failures if failures is not None else []

        async def ask(stage, system, payload):
            stages.append(stage)
            self.assertTrue(script, "出现预算之外的模型请求")
            return script.pop(0)

        async def perform(name, args):
            executions.append((name, args))
            outcome = copy.deepcopy(supplement or {"status": "empty", "items": []})
            if outcome["status"] == "error":
                failures.append(outcome["code"])
            for item in outcome.get("items", []):
                available[item["source_id"]] = item
            history.append({"name": name, "arguments": encode(args), "result": outcome})
            return outcome

        result, state = await resolve_answer(raw, question="分别列出爸爸两份报告的复查建议。", scope=SCOPE,
                                            available=available, history=history, trace=trace, ask=ask,
                                            perform=perform, tools_left=lambda: tools_left, failures=failures)
        self.assertFalse(script, "预设输出尚未消耗；检查条件或测试预期有误")
        self.assertLessEqual(len(executions), 1)
        self.assertLessEqual(stages.count("answer_revision"), 1)
        self.assertLessEqual(stages.count("evidence_review"), 2)
        self.assertTrue({c["source_id"] for c in result["citations"]}.issubset(available))
        return result, state, executions, trace

    async def test_complete_does_not_search_or_revise(self):
        result, state, calls, _ = await self.run_case(BOTH, [review([need(1), need(2)])],
                                                     available={C1["source_id"]: C1, C2["source_id"]: C2})
        self.assertEqual(result["status"], "answered")
        self.assertEqual((state["supplemental_calls"], state["revision_calls"], len(calls)), (0, 0, 0))

    async def test_missing_side_completed_once(self):
        result, state, calls, _ = await self.run_case(ONE,
            [review([need(1), need(2, False)]), BOTH, review([need(1), need(2)])],
            supplement={"status": "ok", "items": [C2]})
        self.assertEqual(result["status"], "answered")
        self.assertEqual((state["supplemental_calls"], state["revision_calls"], state["review_calls"]), (1, 1, 2))
        self.assertEqual(calls[0][1]["report_ids"], ["r2"])

    async def test_still_missing_stops_as_partial(self):
        result, state, calls, _ = await self.run_case(ONE,
            [review([need(1), need(2, False)]), PART, review([need(1), need(2, False)])])
        self.assertEqual(result["status"], "partial")
        self.assertIn("r2", result["answer"])
        self.assertEqual(len(calls), 1)

    async def test_no_relevant_evidence_returns_insufficient(self):
        result, state, _, _ = await self.run_case(EMPTY,
            [review([need(1, False)]), EMPTY, review([need(1, False)])], available={})
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["citations"], [])
        self.assertEqual(state["supplemental_calls"], 1)

    async def test_tool_failure_is_not_empty(self):
        with self.assertRaises(EvidenceToolError):
            await self.run_case(EMPTY, [review([need(1, False)]), EMPTY, review([need(1, False)])],
                                available={}, supplement={"status": "error", "code": "EMBEDDING_TIMEOUT"})

    async def test_no_budget_cannot_start_supplement(self):
        result, state, calls, _ = await self.run_case(ONE,
            [review([need(1), need(2, False)]), PART, review([need(1), need(2, False)])], tools_left=0)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(state["supplemental_calls"], 0)
        self.assertEqual(calls, [])

    async def test_unsupported_answer_after_revision_extracts_related_only(self):
        wrong = final("可以停药。[[chunk:r1:0-30]]", [C1["source_id"]])
        result, state, _, _ = await self.run_case(wrong,
            [review([need(1)], False, ["原文不支持停药"]), wrong, review([need(1)], False, ["仍有无依据结论"])],
            available={C1["source_id"]: C1, C2["source_id"]: C2})
        self.assertEqual(state["answer_mode"], "extractive")
        self.assertNotIn("停药", result["answer"])
        self.assertEqual([c["source_id"] for c in result["citations"]], [C1["source_id"]])

    async def test_revised_review_cannot_drop_missing_report(self):
        result, state, _, trace = await self.run_case(ONE,
            [review([need(1), need(2, False)]), ONE, review([need(1)])])
        self.assertEqual(result["status"], "partial")
        self.assertIn("r2", result["answer"])
        self.assertTrue(any(t["step"] == "m5_revision_rejected" for t in trace))

    async def test_invalid_checks_do_not_dump_irrelevant_retrieval(self):
        result, state, calls, _ = await self.run_case(ONE, ["{}", ONE, "{}"])
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["citations"], [])
        self.assertEqual(calls, [])


def tool_call(index, name="search_reports", args=None):
    return {"calls": [{"id": f"tool-{index}", "name": name, "arguments": encode(args or {"member_id": "member_dad", "query": "建议"})}]}


class AgentChecks(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, script, execute, members=None, scope_error=None):
        script, requests, executed, logs = list(script), [], [], []

        async def create(**kwargs):
            requests.append(kwargs)
            if not script:
                raise AssertionError("调用循环超出预设预算")
            value = script.pop(0)
            if isinstance(value, Exception):
                raise value
            calls = [NS(id=c["id"], function=NS(name=c["name"], arguments=c["arguments"]))
                     for c in value.get("calls", [])] if isinstance(value, dict) else []
            content = "" if isinstance(value, dict) else value
            return NS(choices=[NS(finish_reason="tool_calls" if calls else "stop",
                                  message=NS(content=content, tool_calls=calls))],
                      usage=NS(model_dump=lambda: {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}))

        class FakeClient:
            def __init__(self, **kwargs):
                self.chat = NS(completions=NS(create=create))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        def dispatcher(name, raw, *args):
            executed.append((name, raw))
            return execute(name, raw), [], {}

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"LLM_MODEL": "offline-test"}))
            stack.enter_context(patch.object(agent, "prepare_scope", side_effect=scope_error or (lambda *_: copy.deepcopy(SCOPE))))
            configured = stack.enter_context(patch.object(agent, "make_client"))
            configured.return_value.__enter__.return_value = NS(api_key="offline", base_url="http://offline.invalid")
            stack.enter_context(patch.object(agent, "AsyncOpenAI", FakeClient))
            stack.enter_context(patch.object(agent, "execute_tool", side_effect=dispatcher))
            stack.enter_context(patch.object(agent, "save_agent_log", side_effect=lambda *args: logs.append(args)))
            result = await agent.run_agent("爸爸最近一次空腹血糖是多少？" if any(
                isinstance(s, dict) and any(c["name"] == "get_metric_trend" for c in s.get("calls", [])) for s in script
            ) else "分别列出爸爸两份报告的复查建议。", ["member_dad"] if members is None else members)
        self.assertFalse(script)
        return result, requests, executed, logs

    async def test_missing_member_has_zero_model_and_tools(self):
        result, requests, executed, logs = await self.invoke([], lambda *_: None, members=[])
        self.assertEqual(result["status"], "clarify")
        self.assertEqual((len(requests), len(executed), len(logs)), (0, 0, 1))

    async def test_scope_error_precedes_models(self):
        with self.assertRaises(HTTPException) as context:
            await self.invoke([], lambda *_: None, scope_error=HTTPException(404, "范围不匹配"))
        self.assertEqual(context.exception.status_code, 404)

    async def test_single_value_is_formatted_from_database(self):
        calls = tool_call(1, "get_metric_trend", {"member_id": "member_dad", "metric_code": "fasting_glucose", "latest_n": 1})
        result, requests, executed, _ = await self.invoke(
            [calls, final("爸爸是9.99 mmol/L。[[metric:99]]", [METRIC["source_id"]])],
            lambda *_: {"status": "ok", "items": [METRIC]})
        self.assertIn("5.80", result["answer"])
        self.assertNotIn("9.99", result["answer"])
        self.assertEqual(result["evidence_check"]["answer_mode"], "structured")
        self.assertEqual((result["tool_calls"], result["llm_calls"], result["evidence_check"]["review_calls"]), (1, 2, 0))

    async def test_eight_parallel_calls_execute_at_most_six(self):
        parallel = {"calls": [tool_call(i)["calls"][0] for i in range(8)]}
        result, requests, executed, logs = await self.invoke(
            [parallel, ONE, review([need(1)])], lambda *_: {"status": "ok", "items": [C1]})
        self.assertEqual(len(executed), 6)
        self.assertEqual(result["tool_calls"], 6)
        self.assertEqual(requests[1]["tool_choice"], "none")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(sum(t["step"] == "tool_result" and not t["executed"] for t in logs[0][3]), 2)

    async def test_all_phases_share_six_tools_ten_models(self):
        script = [tool_call(i) for i in range(6)] + [ONE,
                  review([need(1), need(2, False)]), PART, review([need(1), need(2, False)])]
        result, requests, executed, _ = await self.invoke(script, lambda *_: {"status": "ok", "items": [C1]})
        self.assertEqual((len(executed), result["tool_calls"], len(requests), result["llm_calls"]), (6, 6, 10, 10))
        self.assertEqual(result["supplemental_calls"], 0)
        self.assertEqual(result["revision_calls"], 1)

    async def test_controller_search_uses_same_tool_counter_and_log(self):
        script = [tool_call(1), ONE, review([need(1), need(2, False)]), BOTH, review([need(1), need(2)])]

        def execute(name, raw):
            item = C2 if json.loads(raw).get("report_ids") == ["r2"] else C1
            return {"status": "ok", "items": [item]}

        result, requests, executed, logs = await self.invoke(script, execute)
        self.assertEqual((result["tool_calls"], result["supplemental_calls"], result["revision_calls"]), (2, 1, 1))
        origins = [t["origin"] for t in logs[0][3] if t["step"] == "tool_request"]
        self.assertEqual(origins, ["model", "evidence_controller"])
        self.assertEqual(result["llm_calls"], 5)

    async def test_two_parameter_errors_stop_tools_and_return_real_error(self):
        metric_need = need(1, False, kind="metric", metric_code="uric_acid")
        script = [tool_call(1), tool_call(2), EMPTY, review([metric_need])]
        with self.assertRaises(HTTPException) as context:
            await self.invoke(script, lambda *_: {"status": "error", "code": "INVALID_ARGUMENT", "message": "错误参数"})
        self.assertEqual(context.exception.status_code, 503)
        self.assertEqual(context.exception.detail["code"], "tool_execution_failed")


if __name__ == "__main__":
    print("M5 离线验收：模型与数据库均被替换为受控返回，不消耗接口额度。", flush=True)
    unittest.main(verbosity=2)