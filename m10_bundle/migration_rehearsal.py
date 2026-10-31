"""在临时 schema 复制当前五张业务表，演练迁移，核对原字段及重复执行。"""
import importlib
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

TABLES=['family_members','reports','metrics','report_chunks','query_logs']
NEW_COLUMNS=['data_version','index_version','index_state','stale_report_ids']


def signature(conn,schema):
    from psycopg import sql
    return {table:conn.execute(sql.SQL("SELECT count(*),md5(COALESCE(jsonb_agg(to_jsonb(t)-%s::text[] ORDER BY id)::text,'[]')) FROM {}.{} t")
        .format(sql.Identifier(schema),sql.Identifier(table)),(NEW_COLUMNS,)).fetchone() for table in TABLES}


def rehearse(root,base_connect):
    from psycopg import sql
    from app.migrate_m10 import migrate
    schema='m10_rehearsal_'+uuid4().hex
    result={'passed':False,'schema':schema,'temporary_schema_cleaned':False,'external_model_calls':0}
    created=False
    def scoped():
        conn=base_connect()
        try:
            conn.execute(sql.SQL('SET search_path TO {},public').format(sql.Identifier(schema)))
            return conn
        except BaseException:
            conn.close();raise
    try:
        with base_connect() as conn:
            source=conn.execute('SELECT current_schema()').fetchone()[0]
            if conn.execute("SELECT to_regclass('engineering_migrations')").fetchone()[0] is not None:
                raise RuntimeError('当前数据库已经迁移；保留原演练证据，不在迁移后重造升级前数据。')
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created=True
        with scoped() as conn:conn.execute((root/'app/schema.sql').read_text(encoding='utf-8-sig'))
        module=importlib.import_module('app.migrate_m3')
        with patch.object(module,'connect_db',side_effect=scoped):module.migrate()
        with base_connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            before=signature(conn,source)
            for table in TABLES:
                source_cols=[r[0] for r in conn.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position',(source,table)).fetchall()]
                clone_cols=[r[0] for r in conn.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position',(schema,table)).fetchall()]
                if set(source_cols)!=set(clone_cols):raise RuntimeError('源表与已核对的 M9 结构不同：'+table)
                columns=sql.SQL(',').join(map(sql.Identifier,source_cols))
                conn.execute(sql.SQL('INSERT INTO {}.{} ({}) {} SELECT {} FROM {}.{}').format(
                    sql.Identifier(schema),sql.Identifier(table),columns,
                    sql.SQL('OVERRIDING SYSTEM VALUE') if table=='metrics' else sql.SQL(''),columns,sql.Identifier(source),sql.Identifier(table)))
            maximum=conn.execute(sql.SQL('SELECT max(id) FROM {}.metrics').format(sql.Identifier(schema))).fetchone()[0]
            sequence=conn.execute('SELECT pg_get_serial_sequence(%s,%s)',(schema+'.metrics','id')).fetchone()[0]
            conn.execute('SELECT setval(%s::regclass,%s,%s)',(sequence,maximum or 1,maximum is not None))
            cloned=signature(conn,schema)
        from app.repositories.drafts import DraftRepository
        first=migrate(scoped,DraftRepository());second=migrate(scoped,DraftRepository())
        with base_connect() as conn:
            after=signature(conn,schema);shared_after=signature(conn,source)
        result.update(passed=before==cloned==after and before==shared_after and second['already_applied'],
                      source_rows_and_hashes=before,clone_before=cloned,clone_after=after,shared_after=shared_after,
                      original_fields_unchanged=before==after,shared_rows_unchanged=before==shared_after,
                      migration_first=first,migration_second=second,real_data_in_output=False)
    except Exception as exc:
        result.update(passed=False,error_type=type(exc).__name__,message=str(exc))
    finally:
        if created:
            try:
                with base_connect() as conn:conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                result['temporary_schema_cleaned']=True
            except Exception as exc:result['cleanup_error_type']=type(exc).__name__;result['passed']=False
    return result
