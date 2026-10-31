import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.db import connect_db
from app.write_guard import require_legacy_database

APP_DIR = Path(__file__).resolve().parent


def load_demo():
    data = json.loads((APP_DIR / "data" / "demo_reports.json").read_text(encoding="utf-8-sig"))
    if data.get("dataset_type") != "synthetic":
        raise ValueError("此脚本仅导入明确标记为 synthetic 的合成样例。")
    member_ids = [member["id"] for member in data["members"]]
    report_ids = [report["id"] for report in data["reports"]]
    if len(set(member_ids)) != len(member_ids) or len(set(report_ids)) != len(report_ids):
        raise ValueError("样例中的成员 ID 或报告 ID 重复。")
    for report in data["reports"]:
        if report["member_id"] not in member_ids:
            raise ValueError(f"报告 {report['id']} 的成员不存在。")
        date.fromisoformat(report["report_date"])
        codes = set()
        for metric in report["metrics"]:
            if metric["metric_code"] in codes:
                raise ValueError(f"报告 {report['id']} 的指标代码重复。")
            codes.add(metric["metric_code"])
            value = Decimal(metric["value"])
            expected = f"{metric['metric_name']}：{metric['value']} {metric['unit']}"
            if not value.is_finite() or expected not in metric["source_text"]:
                raise ValueError(f"指标 {metric['metric_code']} 的数值、单位与来源不一致。")
            if metric["source_text"] not in report["raw_text"]:
                raise ValueError(f"指标 {metric['metric_code']} 的来源不在报告原文中。")
    return data


def import_demo(conn, data):
    require_legacy_database(conn)
    inserted = {"members": 0, "reports": 0, "metrics": 0}
    for member in data["members"]:
        row = conn.execute(
            "INSERT INTO family_members (id, name, aliases) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            (member["id"], member["name"], member["aliases"]),
        ).fetchone()
        inserted["members"] += int(row is not None)

    for report in data["reports"]:
        report_date = date.fromisoformat(report["report_date"])
        row = conn.execute(
            "INSERT INTO reports "
            "(id, member_id, title, report_date, report_type, source_type, raw_text, status, confirmed_at) "
            "VALUES (%s, %s, %s, %s, %s, 'synthetic', %s, 'confirmed', CURRENT_TIMESTAMP) "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            (report["id"], report["member_id"], report["title"], report_date,
             report["report_type"], report["raw_text"]),
        ).fetchone()
        inserted["reports"] += int(row is not None)
        if row is None:
            existing = conn.execute(
                "SELECT member_id, report_date, raw_text, source_type FROM reports WHERE id = %s",
                (report["id"],),
            ).fetchone()
            if existing != (report["member_id"], report_date, report["raw_text"], "synthetic"):
                raise ValueError(f"报告 ID {report['id']} 已有不同内容；请为新报告使用新的 ID。")

        for metric in report["metrics"]:
            row = conn.execute(
                "INSERT INTO metrics "
                "(report_id, metric_code, metric_name, value, unit, reference_range, source_text, confirmed) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE) "
                "ON CONFLICT (report_id, metric_code) DO NOTHING RETURNING id",
                (report["id"], metric["metric_code"], metric["metric_name"], Decimal(metric["value"]),
                 metric["unit"], metric.get("reference_range"), metric["source_text"]),
            ).fetchone()
            inserted["metrics"] += int(row is not None)
    return inserted


def main():
    data = load_demo()
    schema = (APP_DIR / "schema.sql").read_text(encoding="utf-8-sig")
    # 一次事务：全部成功才提交，出错则回滚，避免只导入一半。
    with connect_db() as conn:
        conn.execute(schema)
        inserted = import_demo(conn, data)
        # 只统计本数据集，之后上传更多报告也不会影响这组验收数字。
        member_count = conn.execute(
            "SELECT count(*) FROM family_members WHERE id = ANY(%s::text[])",
            ([member["id"] for member in data["members"]],),
        ).fetchone()[0]
        report_ids = [report["id"] for report in data["reports"]]
        report_count = conn.execute(
            "SELECT count(*) FROM reports WHERE id = ANY(%s::text[])", (report_ids,),
        ).fetchone()[0]
        metric_count = conn.execute(
            "SELECT count(*) FROM metrics WHERE report_id = ANY(%s::text[])", (report_ids,),
        ).fetchone()[0]
    print(json.dumps({
        "status": "ok", "stage": "M1", "inserted": inserted,
        "demo_totals": {"members": member_count, "reports": report_count, "metrics": metric_count},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
