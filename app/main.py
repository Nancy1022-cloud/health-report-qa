"""应用入口：启动数据库检查、提供页面与 M0 测试接口。"""

from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from app.db import database_status, init_db
from app.llm import test_embedding, test_llm
from app.record_routes import router as record_router
from app.query_routes import router as query_router
from app.agent_routes import router as agent_router
from app.upload_routes import router as upload_router

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="健康报告智能检索问答", version="0.0.1", lifespan=lifespan)
INDEX_FILE = Path(__file__).parent / "static" / "index.html"
app.include_router(record_router)
app.include_router(query_router)
app.include_router(agent_router)
app.include_router(upload_router)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(INDEX_FILE)


@app.get("/health")
def health():
    try:
        db = database_status()
    except (psycopg.Error, RuntimeError):
        raise HTTPException(status_code=503, detail="数据库或 pgvector 异常，请查看 app、db 日志。") from None
    # 这里真实查询数据库；不调用收费的外部模型。
    return {"status": "ok", "stage": "M2", "database": db}


def run_model_test(test_func):
    try:
        return test_func()
    except APITimeoutError:
        raise HTTPException(status_code=504, detail="模型请求超时，请检查网络与服务状态。") from None
    except APIStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "model_http_error",
                "upstream_status": exc.status_code,
                "provider_code": exc.code,
                "message": "模型服务返回错误，请检查 Key 所属地域、模型名、权限与额度。",
            },
        ) from None
    except APIConnectionError:
        raise HTTPException(status_code=502, detail="无法连接模型服务，请检查 BASE_URL 与网络。") from None
    except (APIError, ValueError):
        raise HTTPException(status_code=502, detail="模型响应格式或向量维度异常，请检查服务与模型配置。") from None


@app.post("/api/dev/test-llm")
def check_llm():
    return run_model_test(test_llm)


@app.post("/api/dev/test-embedding")
def check_embedding():
    return run_model_test(test_embedding)

@app.middleware("http")
async def add_json_charset(request, call_next):
    response = await call_next(request)

    content_type = response.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() == "application/json":
        response.headers["content-type"] = "application/json; charset=utf-8"

    return response