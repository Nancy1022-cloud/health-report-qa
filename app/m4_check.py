"""不调用外部模型：真实数据库工具、范围、参数与关键词检索检查。"""

import json

from fastapi import HTTPException

from app.agent_tools import execute_tool, prepare_scope


def main():
    scope = prepare_scope(["member_dad", "member_mom"],
                          ["demo_dad_20260110", "demo_dad_20260710", "demo_mom_20260712"])
    results = []

    def call(name, **args):
        result, trace, usage = execute_tool(name, json.dumps(args, ensure_ascii=False), scope,
                                            "section200o40v1", "keyword")
        return result

    def check(name, condition, observed):
        results.append({"name": name, "passed": bool(condition), "observed": observed})
        if not condition:
            print(json.dumps({"status": "failed", "checks": results}, ensure_ascii=False, indent=2))
            raise SystemExit(1)

    result = call("get_report_list", member_id="member_dad", end_date="2026-02-01")
    check("按成员和日期列报告", result["status"] == "ok" and
          [r["report_id"] for r in result["items"]] == ["demo_dad_20260110"], result)
    result = call("get_metric_trend", member_id="member_dad", metric_code="fasting_glucose")
    check("爸爸两次血糖及代码差值", result["status"] == "ok" and
          [r["value"] for r in result["items"]] == ["6.40", "5.80"] and
          result["comparison"]["delta"] == "-0.60", result)
    result = call("get_metric_trend", member_id="member_dad", metric_code="fasting_glucose", latest_n=1)
    check("最新值只取一条", result["status"] == "ok" and
          [r["value"] for r in result["items"]] == ["5.80"], result)
    result = call("get_metric_trend", member_id="member_mom", metric_code="fasting_glucose")
    check("妈妈数值与爸爸分开", result["status"] == "ok" and
          [r["value"] for r in result["items"]] == ["5.20"] and
          all(r["member_id"] == "member_mom" for r in result["items"]), result)
    result = call("get_metric_trend", member_id="member_dad", metric_code="uric_acid")
    check("无指标返回empty", result["status"] == "empty" and result["items"] == [], result)
    result = call("get_report_list", member_id="member_mom", report_ids=["demo_dad_20260110"])
    check("错成员报告被拦截", result.get("code") == "SCOPE_VIOLATION", result)
    result = call("search_reports", member_id="member_dad", query="检查结论", top_k="2")
    check("不把字符串数字转为整数", result.get("code") == "INVALID_ARGUMENT", result)
    result = call("get_report_list", member_id="member_dad", start_date="2026-08-01", end_date="2026-01-01")
    check("起止日期倒置被拦截", result.get("code") == "INVALID_ARGUMENT", result)
    result = call("unknown_tool", member_id="member_dad")
    check("非白名单工具被拦截", result.get("code") == "UNKNOWN_TOOL", result)
    try:
        prepare_scope(["member_mom"], ["demo_dad_20260110"])
        outer_scope_status = None
    except HTTPException as exc:
        outer_scope_status = exc.status_code
    check("请求范围错误预期404", outer_scope_status == 404, {"http_status": outer_scope_status})
    result = call("search_reports", member_id="member_mom", report_ids=["demo_mom_20260712"],
                  query="检查结论 总胆固醇", top_k=2)
    check("复用M3关键词检索", result["status"] == "ok" and
          any("检查结论" == r.get("section") for r in result["items"]) and
          all(r["member_id"] == "member_mom" and r["report_id"] == "demo_mom_20260712"
              for r in result["items"]), result)
    print(json.dumps({"status": "ok", "checks_passed": len(results), "external_model_calls": 0,
                      "checks": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()