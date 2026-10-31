"""仅验收使用的合成数据、受控适配器与临时 schema。"""

import copy
import io
import json
import os
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

TEXT = '空腹血糖：5.7 mmol/L；参考范围：3.9-6.1。'
BODY = {'title': 'M11合成报告', 'report_date': '2026-10-31', 'report_type': '体检报告', 'raw_text': TEXT,
        'metrics': [{'metric_code': 'fasting_glucose', 'metric_name': '空腹血糖', 'value': '5.7', 'unit': 'mmol/L',
                     'reference_range': '3.9-6.1', 'source_text': TEXT}], 'reviewed': True}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def png(serial=1):
    from PIL import Image
    data = io.BytesIO()
    Image.new('RGB', (16, 16), (serial % 255, serial // 255, 0)).save(data, format='PNG')
    return data.getvalue()


class ControlledAdapter:
    def fields(self, prefix, content, timeout):
        return {'fields': copy.deepcopy({k: v for k, v in BODY.items() if k != 'reviewed'}), 'model': 'controlled'}, None

    def embedding(self, content, timeout):
        dimensions = int(os.getenv('EMBEDDING_DIMENSIONS', '1024'))
        return {'vector': [1.] + [0.] * (dimensions - 1)}, {'total_tokens': 3}


@contextmanager
def isolated(root, output):
    from psycopg import sql
    from app.db import connect_db
    from app.migrate_m10 import migrate as m10
    from app.migrate_m11 import migrate as m11
    from app import migrate_m3
    base = connect_db
    schema = 'm11_probe_' + uuid4().hex
    result = {'schema': schema, 'temporary_schema_cleaned': False, 'external_model_calls': 0}
    def connect():
        conn = base()
        try:
            conn.execute(sql.SQL('SET search_path TO {},public').format(sql.Identifier(schema)))
            return conn
        except BaseException:
            conn.close(); raise
    with base() as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        result['postgresql'] = conn.execute('SHOW server_version').fetchone()[0]
        result['pgvector'] = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]
    try:
        with tempfile.TemporaryDirectory(prefix='health_m11_') as folder, ExitStack() as stack:
            settings = {'UPLOAD_DIR': folder, 'M11_DB_SCHEMA': schema, 'EMBEDDING_DIMENSIONS': os.getenv('EMBEDDING_DIMENSIONS', '1024')}
            for prefix in ['LLM', 'VISION', 'EMBEDDING']:
                settings.update({prefix + '_MODEL': 'controlled-m11', prefix + '_BASE_URL': 'http://controlled.invalid/v1', prefix + '_API_KEY': 'synthetic-no-secret'})
            stack.enter_context(patch.dict(os.environ, settings))
            stack.enter_context(patch('app.llm.OpenAI', side_effect=AssertionError('M11 probe prohibits external models')))
            with connect() as conn:
                conn.execute((root / 'app/schema.sql').read_text(encoding='utf-8-sig'))
            with patch.object(migrate_m3, 'connect_db', side_effect=connect):
                migrate_m3.migrate()
            m10(connect)
            m11(connect)
            with connect() as conn:
                conn.execute("INSERT INTO family_members(id,name) VALUES ('a','合成甲'),('b','合成乙')")
            yield connect, result
    finally:
        with base() as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        result['temporary_schema_cleaned'] = True
        write(output / 'environment.json', result)
