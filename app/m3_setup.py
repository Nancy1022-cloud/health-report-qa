"""一次性小补丁：先核对 M2 结构，再备份并接入 M3。无需安装新依赖即可执行。"""

import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path

MARKER = "# HEALTH_QA_M3_PATCH_V1"
APP_DIR = Path(__file__).resolve().parent


def function_text(source, name):
    node = next((n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise ValueError("找不到函数：" + name)
    lines = source.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1:node.end_lineno])


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("现有代码与 M2 约定不同，停止写入。未匹配到唯一位置：" + old[:100])
    return source.replace(old, new, 1)


def patch_rag(source):
    old = function_text(source, "retrieve")
    if "select_reports(" not in old or "embed_texts(" not in old:
        raise ValueError("retrieve 已被另外修改，请先核对现有文件。")
    new = '''def retrieve(question, member_id, report_ids, top_k, trace, usage, *,
             chunk_version=CHUNK_VERSION, retrieval_mode="vector"):
    from app.retrieval import retrieve as retrieve_m3
    return retrieve_m3(question, member_id, report_ids, top_k, trace, usage,
                       chunk_version=chunk_version, retrieval_mode=retrieval_mode)
'''
    source = replace_once(source, old, new)
    old = function_text(source, "run_query")
    new = replace_once(old,
        "def run_query(question, member_id, report_ids=None, top_k=DEFAULT_TOP_K, with_answer=True):",
        "def run_query(question, member_id, report_ids=None, top_k=DEFAULT_TOP_K, with_answer=True, *,\n"
        "              chunk_version=CHUNK_VERSION, retrieval_mode=\"vector\"):")
    new = replace_once(new, '"chunk_version": CHUNK_VERSION,',
                       '"chunk_version": chunk_version, "retrieval_mode": retrieval_mode,')
    new = replace_once(new, "retrieve(question, member_id, report_ids, top_k, trace, usage)",
                       "retrieve(question, member_id, report_ids, top_k, trace, usage,\n"
                       "                            chunk_version=chunk_version, retrieval_mode=retrieval_mode)")
    source = replace_once(source, old, new)
    old = function_text(source, "save_log")
    new = replace_once(old, '"vector_baseline"',
        '(trace[0].get("retrieval_mode", "vector") + ":" + trace[0].get("chunk_version", CHUNK_VERSION))')
    source = replace_once(source, old, new)
    # 保留 generate 原函数，包括用户此前设置的 timeout=60.0 和错误处理。
    return source.rstrip() + "\n\n" + MARKER + "\n"


def patch_routes(source):
    source = "from typing import Literal\n\n" + source
    pattern = r"(?m)^    top_k: int = Field\([^\n]+\)\n"
    matches = list(re.finditer(pattern, source))
    if len(matches) != 1:
        raise ValueError("QueryRequest 的 top_k 字段不匹配，停止写入。")
    end = matches[0].end()
    source = source[:end] + (
        '    chunk_version: Literal["fixed200o40", "section200o40v1"] = "fixed200o40"\n'
        '    retrieval_mode: Literal["vector", "keyword", "hybrid"] = "vector"\n'
        '    with_answer: bool = True\n'
    ) + source[end:]
    source = replace_once(source, function_text(source, "ask"), '''def ask(body: QueryRequest):
    return run_query(body.question, body.member_id, body.report_ids, body.top_k,
                     with_answer=body.with_answer, chunk_version=body.chunk_version,
                     retrieval_mode=body.retrieval_mode)
''')
    source = replace_once(source, function_text(source, "index_stats"), '''def index_stats(conn=Depends(database_connection)):
    rows = conn.execute(
        "SELECT r.member_id, r.id AS report_id, r.report_date, c.chunk_version, "
        "count(c.id) AS chunks, count(c.embedding) AS vectors, count(c.search_vector) AS keyword_vectors, "
        "min(c.embedding_model) AS embedding_model, min(c.embedding_dimensions) AS dimensions "
        "FROM reports r LEFT JOIN report_chunks c ON c.report_id=r.id "
        "WHERE r.status='confirmed' GROUP BY r.id, c.chunk_version ORDER BY r.member_id, r.report_date, r.id, c.chunk_version"
    ).fetchall()
    return {"items": rows, "report_count": len({r["report_id"] for r in rows}),
            "chunk_count": sum(r["chunks"] for r in rows)}
''')
    return source.rstrip() + "\n\n" + MARKER + "\n"


def install():
    paths = [APP_DIR / name for name in ("rag.py", "query_routes.py", "build_index.py")]
    originals = {path: path.read_text(encoding="utf-8-sig") for path in paths}
    installed = [MARKER in text for text in originals.values()]
    if all(installed):
        return {"status": "already_installed", "message": "M3 接入补丁已安装，无需重复执行。"}
    if any(installed):
        raise ValueError("三个文件的补丁状态不一致，停止写入，请保留文件后检查。")
    updates = {
        paths[0]: patch_rag(originals[paths[0]]),
        paths[1]: patch_routes(originals[paths[1]]),
        paths[2]: '"""M3：按版本构建索引，默认 fixed 保持 M2 命令兼容。"""\n'
                  'from app.m3_index import build_index, main\n\n'
                  'if __name__ == "__main__":\n    main()\n\n' + MARKER + '\n',
    }
    for source in updates.values():
        ast.parse(source)  # 所有检查通过后才开始备份和写入。
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    backup = APP_DIR / "data" / "m3_backups" / stamp
    backup.mkdir(parents=True)
    for path in paths:
        (backup / path.name).write_bytes(path.read_bytes())
    for path, source in updates.items():
        path.write_text(source, encoding="utf-8")
    return {"status": "ok", "backup": str(backup), "modified": [p.name for p in paths]}


if __name__ == "__main__":
    print(json.dumps(install(), ensure_ascii=False, indent=2))