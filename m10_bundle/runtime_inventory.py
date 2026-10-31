"""只读实际运行配置；不读取或输出 Key、数据库连接口令。"""
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

def inventory(root):
    ROOT = root
    sys.path.insert(0, str(ROOT))
    from psycopg import sql
    from app.db import connect_db
    tables = ["family_members", "reports", "report_chunks", "metrics", "query_logs"]
    with connect_db() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        server = conn.execute("SELECT current_setting('server_version'), current_database(), current_schema()").fetchone()
        extensions = conn.execute("SELECT extname,extversion FROM pg_extension WHERE extname='vector'").fetchall()
        if not extensions:
            raise RuntimeError("没有查到 pgvector；先核对当前部署")
        counts = {name: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0] for name in tables}
        indexes = conn.execute("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename=ANY(%s) ORDER BY tablename,indexname", (tables,)).fetchall()
        constraints = conn.execute("SELECT c.relname,p.conname,pg_get_constraintdef(p.oid) FROM pg_constraint p JOIN pg_class c ON c.oid=p.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND c.relname=ANY(%s) ORDER BY c.relname,p.conname", (tables,)).fetchall()
        vector_configs = conn.execute("SELECT embedding_model,embedding_dimensions,chunk_version,count(*) FROM report_chunks GROUP BY embedding_model,embedding_dimensions,chunk_version ORDER BY 1,2,3").fetchall()
    models = {}
    for prefix in ["LLM", "EMBEDDING", "VISION"]:
        models[prefix] = {"model": os.getenv(prefix + "_MODEL"), "endpoint_host": urlsplit(os.getenv(prefix + "_BASE_URL", "")).hostname,
                          "key_present": bool(os.getenv(prefix + "_API_KEY", "").strip())}
    packages = {}
    for name in ["fastapi", "pydantic", "psycopg", "openai", "uvicorn", "jieba", "httpx"]:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"database_engine": "PostgreSQL", "server": server, "extensions": extensions,
            "row_counts": counts, "indexes": indexes, "constraints": constraints,
            "vector_configs": vector_configs, "retrieval": "pgvector + PostgreSQL tsvector/ts_rank_cd + RRF",
            "models": models, "embedding_dimensions": os.getenv("EMBEDDING_DIMENSIONS", "1024"),
            "python": sys.version.split()[0], "packages": packages,
            "deployment": "compose db + app; postgres_data volume; uploads bind mount"}

