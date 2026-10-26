"""M7 合成验收材料；只给专用成员加数据，不修改业务代码。"""

import argparse
import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
MEMBERS = [{"id": "m7_member_a", "name": "验收甲", "aliases": ["测试甲"]},
           {"id": "m7_member_b", "name": "验收乙", "aliases": ["测试乙"]}]
DATES = ["2026-01-15", "2026-03-15", "2026-05-15", "2026-07-15", "2026-09-15", "2026-10-15"]
VERSIONS = {"fixed200o40", "section200o40v1"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def build_bundle():
    reports = []
    for side, member in enumerate(MEMBERS):
        for i, day in enumerate(DATES):
            rid = f"m7_{'ab'[side]}_{day.replace('-', '')}"
            metrics = []
            for code, name, start, step, ref in [
                ("fasting_glucose", "空腹血糖", "6.30", "0.20", "3.90–6.10 mmol/L"),
                ("total_cholesterol", "总胆固醇", "5.70", "0.10", "0–5.20 mmol/L"),
                ("triglycerides", "甘油三酯", "1.80", "0.05", "0–1.70 mmol/L"),
            ]:
                value = format(Decimal(start) - i * Decimal(step) + side * Decimal("0.10"), ".2f")
                unit = "mmol/L"
                if side == 1 and i == 5 and code == "fasting_glucose":
                    value, unit, ref = "95.00", "mg/dL", "70.00–110.00 mg/dL"
                source = f"{name}：{value} {unit}；报告参考范围：{ref}。"
                metrics.append({"metric_code": code, "metric_name": name, "value": value,
                                "unit": unit, "reference_range": ref, "source_text": source})
            mark = f"{'甲乙'[side]}{i + 1:02d}"
            finding = f"记录标记为{mark}；本页仅汇总三项生化记录，不据此作疾病诊断。"
            advice = f"复查时携带{day}这份报告，并保留编号{mark}的饮食记录供医生对照。"
            raw = (f"【合成验收报告】\n仅供软件测试，不对应真实个人。成员：{member['name']}；日期：{day}。\n"
                   "【生化检查】\n" + "\n".join(m["source_text"] for m in metrics) +
                   f"\n【检查结论】\n{finding}\n【建议与备注】\n{advice}\n未记录尿酸数值或用药剂量。")
            reports.append({"id": rid, "member_id": member["id"],
                            "title": f"{member['name']} {day} 合成验收报告", "report_date": day,
                            "report_type": "体检报告" if i == 0 else "复查报告", "raw_text": raw,
                            "metrics": metrics, "finding": finding, "advice": advice})
    a, b = reports[:6], reports[6:]
    cases = []

    def metric(r, code="fasting_glucose"):
        m = next(m for m in r["metrics"] if m["metric_code"] == code)
        return {"kind": "metric", "report_id": r["id"], "metric_code": code, "text": m["source_text"]}

    def text(r, field="advice"):
        return {"kind": "chunk", "report_id": r["id"], "text": r[field]}

    def add(question, selected, refs, reference, *, statuses=None, tools=(), http=200, needs=(), category="记录"):
        mids = list(dict.fromkeys(r["member_id"] for r in selected))
        body = {"question": question, "member_ids": mids, "chunk_version": "section200o40v1", "retrieval_mode": "hybrid"}
        if selected:
            body["report_ids"] = [r["id"] for r in selected]
        cases.append({"id": f"m7_{len(cases)+1:02d}", "category": category, "request": body,
                      "expected_http": http, "expected_statuses": statuses or ["answered"],
                      "required_tools": list(tools), "gold": refs, "reference_answer": reference,
                      "manual_focus": list(needs)})

    add("验收甲在选定范围内最新一次空腹血糖是多少？", a, [metric(a[5])],
        "2026-10-15：5.30 mmol/L。", tools=["get_metric_trend"], needs=["最新记录和日期参数正确"])
    add("验收甲1月15日与10月15日的空腹血糖各是多少，末次减首次是多少？", [a[0], a[5]],
        [metric(a[0]), metric(a[5])], "6.30→5.30 mmol/L；末次减首次 -1.00 mmol/L。",
        tools=["get_metric_trend"], needs=["差值来自工具comparison，两个日期归属正确"])
    add("验收乙最近两次空腹血糖是多少？能直接计算差值吗？", [b[4], b[5]],
        [metric(b[4]), metric(b[5])], "2026-09-15为5.60 mmol/L，2026-10-15为95.00 mg/dL；单位不同，本版不能直接算差值。",
        tools=["get_metric_trend"], statuses=["answered", "partial"], needs=["不得自行换算或直接相减"])
    add("验收甲7月15日的总胆固醇是多少？", [a[3]], [metric(a[3], "total_cholesterol")],
        "5.40 mmol/L。", tools=["get_metric_trend"])
    add("验收乙5月15日的甘油三酯是多少？", [b[2]], [metric(b[2], "triglycerides")],
        "1.80 mmol/L。", tools=["get_metric_trend"])
    add("验收甲5月15日报告要求复查携带什么？", [a[2]], [text(a[2])], a[2]["advice"],
        tools=["search_reports"], category="原文")
    add("验收乙9月15日报告的检查结论是什么？", [b[4]], [text(b[4], "finding")], b[4]["finding"],
        tools=["search_reports"], category="原文")
    add("分别列出验收甲1月15日和7月15日报告的建议与备注。", [a[0], a[3]],
        [text(a[0]), text(a[3])], a[0]["advice"] + "\n" + a[3]["advice"],
        tools=["search_reports"], needs=["两份报告均有建议证据，不能只查一侧"], category="组合")
    add("验收甲、验收乙10月15日报告的复查携带材料分别是什么？", [a[5], b[5]],
        [text(a[5]), text(b[5])], a[5]["advice"] + "\n" + b[5]["advice"],
        tools=["search_reports"], needs=["甲06和乙06分别归属，不能推断谁更健康"], category="组合")
    add("对比验收甲1月15日和10月15日的空腹血糖数值，并分别列出两份报告的备注。", [a[0], a[5]],
        [metric(a[0]), metric(a[5]), text(a[0]), text(a[5])],
        "6.30与5.30 mmol/L；" + a[0]["advice"] + "\n" + a[5]["advice"],
        tools=["get_metric_trend", "search_reports"], category="组合")
    add("列出验收乙在2026年9月1日至10月31日的报告日期和类型。", b,
        [{"kind": "report", "report_id": r["id"]} for r in b[4:]],
        "2026-09-15、2026-10-15，两份均为复查报告。", tools=["get_report_list"],
        needs=["核对start_date/end_date，不能把其他月份混入回答"], category="目录")
    add("验收甲选定范围内有哪些体检报告？请列日期。", a,
        [{"kind": "report", "report_id": a[0]["id"]}], "2026-01-15，体检报告。",
        tools=["get_report_list"], needs=["report_type为体检报告"], category="目录")
    add("验收甲10月15日的尿酸具体数值是多少？", [a[5]], [], "未记录尿酸数值，应说明资料不足。",
        statuses=["insufficient"], category="缺失")
    add("验收乙10月15日报告写的具体用药剂量是多少？", [b[5]], [], "未记录用药剂量，应说明资料不足。",
        statuses=["insufficient"], category="缺失")
    add("验收甲10月15日的空腹血糖和尿酸分别是多少？", [a[5]], [metric(a[5])],
        "血糖5.30 mmol/L；尿酸数值未记录。", statuses=["partial"], needs=["已知部分必须回答，缺失部分不能编造"], category="部分")
    add("最新一次空腹血糖是多少？", [], [], "先询问是哪位成员。", statuses=["clarify"], category="澄清")
    add("他最新一次空腹血糖是多少？", [a[5], b[5]], [], "选中两位成员，‘他’不明确，需澄清。",
        statuses=["clarify"], category="澄清")
    add("查询这份报告的血糖。", [b[5]], [], "成员甲与成员乙的报告不匹配，应在后端拒绝。", http=404, category="隔离")
    cases[-1]["request"]["member_ids"] = [MEMBERS[0]["id"]]
    add("查询这份报告的血糖。", [a[0]], [], "不存在的报告应返回404，不能当作无记录。", http=404, category="隔离")
    cases[-1]["request"]["report_ids"] = ["m7_report_does_not_exist"]
    add("验收乙1月15日至10月15日总胆固醇首末数值及变化是多少？", [b[0], b[5]],
        [metric(b[0], "total_cholesterol"), metric(b[5], "total_cholesterol")],
        "5.80→5.30 mmol/L；末次减首次 -0.50 mmol/L。", tools=["get_metric_trend"])

    retrieval = []
    for r in [a[0], a[2], a[5], b[0], b[3], b[5]]:
        retrieval.append({"id": f"m7r_{len(retrieval)+1:02d}", "member_id": r["member_id"],
                          "report_ids": [x["id"] for x in reports if x["member_id"] == r["member_id"]],
                          "question": f"{r['report_date']}这份报告复查时要携带什么？",
                          "answerable": True, "reference_answer": r["advice"],
                          "gold": [{"report_id": r["id"], "text": r["advice"]}]})
    for selected in [[a[0], a[5]], [b[0], b[5]]]:
        retrieval.append({"id": f"m7r_{len(retrieval)+1:02d}", "member_id": selected[0]["member_id"],
                          "report_ids": [r["id"] for r in reports if r["member_id"] == selected[0]["member_id"]],
                          "question": "1月15日和10月15日两份报告要求复查携带什么？分别列出。",
                          "answerable": True, "reference_answer": "\n".join(r["advice"] for r in selected),
                          "gold": [{"report_id": r["id"], "text": r["advice"]} for r in selected]})
    return {"dataset_type": "synthetic", "version": "m7_v1", "members": MEMBERS,
            "reports": reports, "cases": cases, "retrieval_cases": retrieval}


def validate_bundle(bundle):
    reports = {r["id"]: r for r in bundle["reports"]}
    assert len(reports) == 12 and len(bundle["cases"]) == 20
    assert len({c["id"] for c in bundle["cases"]}) == 20
    for r in reports.values():
        date.fromisoformat(r["report_date"])
        for m in r["metrics"]:
            assert Decimal(m["value"]).is_finite()
            assert f"{m['metric_name']}：{m['value']} {m['unit']}" in m["source_text"]
            assert m["source_text"] in r["raw_text"]
    for c in bundle["cases"]:
        for g in c["gold"]:
            r = reports[g["report_id"]]
            assert r["member_id"] in c["request"]["member_ids"]
            if g.get("text"):
                assert r["raw_text"].count(g["text"]) == 1
    for c in bundle["retrieval_cases"]:
        for g in c["gold"]:
            assert reports[g["report_id"]]["raw_text"].count(g["text"]) == 1


def prepare():
    bundle = build_bundle()
    validate_bundle(bundle)
    DATA_DIR.mkdir(exist_ok=True)
    files = {"m7_reports.json": {k: bundle[k] for k in ["dataset_type", "version", "members", "reports"]},
             "eval_m7.json": {"dataset_type": "synthetic", "version": "m7_v1", "cases": bundle["cases"]},
             "eval_m7_retrieval.json": {"dataset_type": "synthetic", "version": "m7_v1", "cases": bundle["retrieval_cases"]}}
    for name, value in files.items():
        path = DATA_DIR / name
        if path.exists() and json.loads(path.read_text(encoding="utf-8-sig")) != value:
            raise ValueError(f"{path} 已有不同内容；先保留旧版，为新数据使用新版本名。")
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return bundle


def read_state(conn, bundle):
    from psycopg.rows import dict_row
    conn.row_factory = dict_row
    mids = [m["id"] for m in bundle["members"]]
    members = conn.execute("SELECT id,name,aliases FROM family_members WHERE id=ANY(%s::text[]) ORDER BY id", (mids,)).fetchall()
    reports = conn.execute("SELECT id,member_id,title,report_date::text,report_type,source_type,raw_text,status "
                           "FROM reports WHERE member_id=ANY(%s::text[]) ORDER BY id", (mids,)).fetchall()
    ids = [r["id"] for r in bundle["reports"]]
    metrics = conn.execute("SELECT m.report_id,m.metric_code,m.metric_name,m.value::text,m.unit,m.reference_range,m.source_text,m.confirmed "
                           "FROM metrics m WHERE report_id=ANY(%s::text[]) ORDER BY report_id,metric_code", (ids,)).fetchall()
    expected_reports = [{**{k: r[k] for k in ["id", "member_id", "title", "report_date", "report_type", "raw_text"]},
                         "source_type": "synthetic", "status": "confirmed"} for r in bundle["reports"]]
    expected_metrics = [{**m, "report_id": r["id"], "confirmed": True} for r in bundle["reports"] for m in r["metrics"]]
    normalize = lambda rows: sorted([dict(x, value=str(Decimal(x["value"]).normalize())) if "value" in x else x for x in rows],
                                    key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
    if normalize(members) != normalize(bundle["members"]) or normalize(reports) != normalize(expected_reports) or normalize(metrics) != normalize(expected_metrics):
        raise ValueError("M7数据库内容与冻结的合成材料不一致；请核对，不能覆盖差异。")
    return {"members": members, "reports": reports, "metrics": metrics}


def seed():
    from app.db import connect_db
    from app.init_data import import_demo
    bundle = prepare()
    with connect_db() as conn:
        # 使用M1已存在的合成导入函数；同一事务内核对所有字段，差异会回滚。
        inserted = import_demo(conn, bundle)
        read_state(conn, bundle)
    return {"status": "ok", "inserted": inserted, "totals": {"members": 2, "reports": 12, "metrics": 36}}


def snapshot():
    from app.db import connect_db
    from app.chunking import chunk_text
    from app.rag import embedding_config
    from app.text_search import SEARCH_VERSION
    bundle = build_bundle()
    validate_bundle(bundle)
    model, dimensions = embedding_config()
    ids = [r["id"] for r in bundle["reports"]]
    with connect_db() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        state = read_state(conn, bundle)
        chunks = conn.execute("SELECT id,report_id,chunk_version,content,start_char,end_char,source_sha256, "
                              "embedding_model,embedding_dimensions,search_version,search_vector::text, "
                              "md5(embedding::text) AS vector_hash FROM report_chunks "
                              "WHERE report_id=ANY(%s::text[]) ORDER BY id", (ids,)).fetchall()
    expected = {}
    for r in bundle["reports"]:
        for strategy, version in [("fixed", "fixed200o40"), ("section", "section200o40v1")]:
            for c in chunk_text(r["raw_text"], strategy):
                sid = f"chunk:{r['id']}:{version}:{c['start_char']}-{c['end_char']}"
                expected[sid] = (r, version, c)
    if {c["id"] for c in chunks} != set(expected):
        raise ValueError("M7两种索引未齐备；请先运行两次m3_index。")
    for row in chunks:
        r, version, c = expected[row["id"]]
        valid = (row["content"] == c["content"] and row["start_char"] == c["start_char"] and row["end_char"] == c["end_char"]
                 and row["report_id"] == r["id"] and row["chunk_version"] == version
                 and row["source_sha256"] == hashlib.sha256(r["raw_text"].encode()).hexdigest()
                 and row["embedding_model"] == model and row["embedding_dimensions"] == dimensions
                 and row["search_version"] == SEARCH_VERSION and row["search_vector"] is not None and bool(row["vector_hash"]))
        if not valid:
            raise ValueError("M7索引配置或原文不一致：" + row["id"])
    source_hashes = {str(p.relative_to(APP_DIR)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(APP_DIR.rglob("*.py"))}
    config = {k: os.getenv(k) for k in ["LLM_MODEL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS"]}
    return {"reports": 12, "metrics": 36, "chunks": len(chunks), "data_sha256": digest(state),
            "indexes_sha256": digest(chunks), "source_sha256": digest(source_hashes), "models": config}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "seed", "snapshot"])
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
        result = {"status": "ok", "files": ["app/data/m7_reports.json", "app/data/eval_m7.json", "app/data/eval_m7_retrieval.json"]}
    else:
        result = seed() if args.action == "seed" else snapshot()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()