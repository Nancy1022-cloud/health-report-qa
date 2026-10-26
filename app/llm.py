"""两个独立连通测试：文本生成与文本向量化。"""

import os
from urllib.parse import urlsplit

from fastapi import HTTPException
from openai import OpenAI


def make_client(prefix):
    api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
    base_url = os.getenv(f"{prefix}_BASE_URL", "").strip()
    model = os.getenv(f"{prefix}_MODEL", "").strip()
    if not api_key or not base_url or not model:
        raise HTTPException(
            status_code=503,
            detail=f"请在 .env 填写 {prefix}_API_KEY、{prefix}_BASE_URL、{prefix}_MODEL。",
        )
    url = urlsplit(base_url)
    if url.scheme not in {"http", "https"} or not url.netloc:
        raise HTTPException(status_code=503, detail=f"{prefix}_BASE_URL 格式不正确。")
    # 不自动重试，避免一次点击触发多次请求。
    return OpenAI(api_key=api_key, base_url=base_url, timeout=30.0, max_retries=0)


def test_llm():
    with make_client("LLM") as client:
        model = os.environ["LLM_MODEL"]
        result = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "这是接口连通测试，请只回复：模型连接成功"}],
            max_completion_tokens=128,
            extra_body={"enable_thinking": False},
        )
    if not result.choices or not result.choices[0].message.content:
        raise ValueError("模型响应中没有文本")
    return {
        "status": "ok",
        "model": model,
        "reply": result.choices[0].message.content,
        "usage": result.usage.model_dump() if result.usage else None,
    }


def test_embedding():
    try:
        dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
        if dimensions <= 0:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=503, detail="EMBEDDING_DIMENSIONS 应为正整数。") from None
    with make_client("EMBEDDING") as client:
        model = os.environ["EMBEDDING_MODEL"]
        result = client.embeddings.create(
            model=model,
            input=["这是一份用于开发测试的合成健康报告。"],
            dimensions=dimensions,
            encoding_format="float",
        )
    if len(result.data) != 1 or len(result.data[0].embedding) != dimensions:
        raise ValueError("Embedding 返回为空或向量维度与配置不一致")
    vector = result.data[0].embedding
    return {
        "status": "ok",
        "model": model,
        "dimensions": len(vector),
        "preview": vector[:5],
        "usage": result.usage.model_dump() if result.usage else None,
    }
