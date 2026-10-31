"""M7/M8 共用行为探针：真实 PostgreSQL，独立 schema，受控模型，不调用外部接口。"""

import argparse
import asyncio
import hashlib
import importlib
import io
import json
import os
import re
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from uuid import uuid4

TEXT = "M8合成体检报告\n检查指标\n空腹血糖：5.7 mmol/L；参考范围：3.9-6.1。\n检查结论\n请按报告原文记录。"
SOURCE = "空腹血糖：5.7 mmol/L；参考范围：3.9-6.1。"
METRIC = {"metric_code": "fasting_glucose", "metric_name": "空腹血糖", "value": "5.7",
          "unit": "mmol/L", "reference_range": "3.9-6.1", "source_text": SOURCE}
BODY = {"title": "M8合成体检报告", "report_date": "2026-10-30", "report_type": "体检报告",
        "raw_text": TEXT, "metrics": [METRIC], "reviewed": True}


def execute(root, output):
    sys.path.insert(0, str(root))
    import psycopg
    from psycopg import sql
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from PIL import Image
    from app import db, agent, rag, report_extract
    from app import query_routes, agent_routes, record_routes, upload_routes

    schema = "m8_probe_" + uuid4().hex
    assert re.fullmatch(r"m8_probe_[a-f0-9]{32}", schema)
    base_connect = db.connect_db
    created = False
    records = []
    report_id = None
    model_calls = {"extraction": 0, "embedding": 0, "generation": 0, "agent": 0}

    def scoped_connect():
        conn = base_connect()
        try:
            conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
            return conn
        except BaseException:
            conn.close()
            raise

    def fake_fields(content, prefix):
        model_calls["extraction"] += 1
        fields = {key: value for key, value in BODY.items() if key != "reviewed"}
        return fields, {"model": "controlled", "usage": {"total_tokens": 1}}

    def fake_embed(texts):
        model_calls["embedding"] += 1
        return [[1.0] + [0.0] * 7 for _ in texts], {"total_tokens": len(texts)}

    class SyncClient:
        api_key = "controlled"
        base_url = "https://controlled.invalid/v1"
        def __enter__(self):
            self.chat = NS(completions=NS(create=self.create))
            return self
        def __exit__(self, *args):
            return False
        def create(self, **kwargs):
            model_calls["generation"] += 1
            evidence = json.loads(kwargs["messages"][-1]["content"])["evidence"]
            sid = evidence[0]["source_id"]
            content = json.dumps({"answerable": True, "answer": f"原文记录的空腹血糖参考范围为3.9-6.1。[[{sid}]]", "source_ids": [sid]}, ensure_ascii=False)
            return NS(choices=[NS(finish_reason="stop", message=NS(content=content))], usage=NS(model_dump=lambda: {"total_tokens": 1}))

    class AsyncClient:
        def __init__(self, **kwargs):
            self.calls = 0
            self.chat = NS(completions=NS(create=self.create))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def create(self, **kwargs):
            self.calls += 1
            model_calls["agent"] += 1
            if self.calls == 1:
                call = NS(id="m8-tool-1", function=NS(name="get_metric_trend", arguments=json.dumps(
                    {"member_id": "m8_a", "metric_code": "fasting_glucose", "report_ids": [report_id]})))
                message = NS(content="", tool_calls=[call])
            elif self.calls == 2:
                message = NS(content=json.dumps({"status": "answered", "answer": "空腹血糖5.7 mmol/L。[[metric:S1]]", "source_ids": ["metric:S1"]}, ensure_ascii=False), tool_calls=[])
            else:
                raise AssertionError("受控单指标问题出现额外模型调用")
            return NS(choices=[NS(finish_reason="stop", message=message)], usage=NS(model_dump=lambda: {"total_tokens": 1}))

    def normalize(value):
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items() if k not in {"query_id", "latency_ms", "created_at"}}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, str) and report_id:
            return value.replace(report_id, "<report>")
        return value

    try:
        with base_connect() as conn:
            if conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone() is None:
                raise RuntimeError("现有数据库未启用 pgvector；先按 M7 修复，不在探针中修改共享扩展")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True
        with tempfile.TemporaryDirectory(prefix="m8_files_") as temporary, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "UPLOAD_DIR": temporary, "LLM_MODEL": "controlled-m8", "EMBEDDING_MODEL": "controlled-m8",
                "EMBEDDING_DIMENSIONS": "8", "VISION_MODEL": "controlled-m8"}))
            # 所有连接仍进同一个业务数据库，但只读写本次唯一 schema。
            names = ["db", "rag", "retrieval", "agent", "agent_tools", "report_ingest", "record_routes", "upload_routes", "migrate_m3"]
            if (root / "app/dependencies.py").exists():
                names.append("dependencies")
            for name in names:
                module = importlib.import_module("app." + name)
                if hasattr(module, "connect_db"):
                    stack.enter_context(patch.object(module, "connect_db", side_effect=scoped_connect))
            for name in ["rag", "retrieval", "report_ingest"]:
                module = importlib.import_module("app." + name)
                stack.enter_context(patch.object(module, "embed_texts", side_effect=fake_embed))
            stack.enter_context(patch.object(report_extract, "request_fields", side_effect=fake_fields))
            stack.enter_context(patch.object(rag, "make_client", side_effect=lambda *_: SyncClient()))
            stack.enter_context(patch.object(agent, "make_client", side_effect=lambda *_: SyncClient()))
            stack.enter_context(patch.object(agent, "AsyncOpenAI", AsyncClient))
            # 任何没被显式替换的 OpenAI 实例都失败，防止探针意外调用收费接口。
            stack.enter_context(patch("app.llm.OpenAI", side_effect=AssertionError("探针禁止外部模型请求")))
            with scoped_connect() as conn:
                conn.execute((root / "app/schema.sql").read_text(encoding="utf-8-sig"))
            importlib.import_module("app.migrate_m3").migrate()
            with scoped_connect() as conn:
                conn.execute("INSERT INTO family_members (id,name) VALUES ('m8_a','M8甲'),('m8_b','M8乙')")

            app = FastAPI()
            for router in [record_routes.router, query_routes.router, agent_routes.router, upload_routes.router]:
                app.include_router(router)
            image = io.BytesIO()
            Image.new("RGB", (16, 16), "white").save(image, format="PNG")

            def record(name, response, expected, check=lambda payload: True):
                payload = response.json()
                passed = response.status_code == expected and bool(check(payload))
                records.append({"name": name, "expected_http": expected, "http": response.status_code,
                                "passed": passed, "body": normalize(payload)})
                if not passed:
                    raise AssertionError(f"{name}: HTTP={response.status_code}, expected={expected}")
                return payload

            with TestClient(app) as client:
                record("members", client.get("/api/members"), 200, lambda p: p["count"] == 2)
                draft = record("upload_draft", client.post("/api/ingest/upload", data={"member_id": "m8_a"},
                               files={"file": ("m8.png", image.getvalue(), "image/png")}), 200, lambda p: p["status"] == "draft")
                report_id = draft["id"]
                # 第一条草稿响应也统一动态 ID。
                records[-1]["body"] = normalize(draft)
                record("draft_not_queryable", client.get("/api/reports?member_id=m8_a"), 200, lambda p: p["count"] == 0)
                url = f"/api/ingest/{report_id}/confirm?member_id=m8_a"
                record("confirm", client.post(url, json=BODY), 200, lambda p: p["status"] == "confirmed" and not p["already_confirmed"])
                before_repeat = model_calls["embedding"]
                record("same_confirmation", client.post(url, json=BODY), 200, lambda p: p["already_confirmed"])
                assert before_repeat == model_calls["embedding"], "同内容重放又调用了 Embedding"
                record("conflicting_confirmation", client.post(url, json={**BODY, "title": "不同内容"}), 409)
                record("wrong_member_draft", client.get(f"/api/ingest/{report_id}?member_id=m8_b"), 404)
                record("wrong_member_metrics", client.get("/api/metrics?member_id=m8_b"), 200, lambda p: p["count"] == 0)
                record("metrics", client.get("/api/metrics?member_id=m8_a&metric_code=fasting_glucose"), 200,
                       lambda p: p["count"] == 1 and p["items"][0]["value"] == "5.7")
                record("confirmed_detail", client.get(f"/api/ingest/{report_id}?member_id=m8_a"), 200, lambda p: p["status"] == "confirmed")
                record("normal_query", client.post("/api/query", json={"question": "空腹血糖参考范围", "member_id": "m8_a",
                       "report_ids": [report_id], "chunk_version": "section200o40v1", "retrieval_mode": "hybrid"}), 200,
                       lambda p: p["status"] == "answered" and len(p["citations"]) > 0)
                record("metric_query", client.post("/api/agent/query", json={"question": "这份报告的空腹血糖是多少？", "member_ids": ["m8_a"],
                       "report_ids": [report_id]}), 200, lambda p: p["status"] == "answered" and "5.7" in p["answer"])
                record("scope_blocked", client.post("/api/agent/query", json={"question": "空腹血糖", "member_ids": ["m8_b"],
                       "report_ids": [report_id]}), 404)
                with scoped_connect() as conn:
                    counts = {name: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0]
                              for name in ["reports", "metrics", "report_chunks", "query_logs"]}
                assert counts["reports"] == counts["metrics"] == 1
        result = {"passed": True, "fixture_sha256": hashlib.sha256(json.dumps(BODY, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                  "data_source": "fixed_synthetic", "database": "real_postgresql_isolated_schema",
                  "model_source": "controlled_test_double", "external_model_calls": 0,
                  "controlled_calls": model_calls, "counts": counts, "checks": records}
    except Exception as exc:
        result = {"passed": False, "error_type": type(exc).__name__, "error": str(exc), "checks": records,
                  "external_model_calls": 0}
        raise
    finally:
        if created:
            # schema 由本进程生成并验证格式，不接收用户参数；不删除 public 或业务表。
            try:
                with base_connect() as conn:
                    conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                if "result" in locals():
                    result["temporary_schema_cleaned"] = True
            except Exception:
                if "result" in locals():
                    result["temporary_schema_cleaned"] = False
                raise
        if "result" in locals():
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "checks": len(records), "external_model_calls": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    execute(args.root.resolve(), args.output.resolve())
