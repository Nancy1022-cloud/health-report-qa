"""M0 只建立数据库连接并启用 pgvector，业务表在 M1 创建。"""

import os

import psycopg


def connect_db():
    return psycopg.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5,
        options="-c statement_timeout=5000",
    )


def init_db():
    # IF NOT EXISTS 允许应用重复启动；不会删除已有数据。
    with connect_db() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")


def database_status():
    with connect_db() as conn:
        row = conn.execute(
            "SELECT current_database(), "
            "(SELECT extversion FROM pg_extension WHERE extname = 'vector')"
        ).fetchone()
    if not row or not row[1]:
        raise RuntimeError("pgvector 尚未启用")
    return {"status": "ok", "name": row[0], "pgvector": row[1]}
