"""M11 是增量新增任务表；在现有业务行的隔离副本上执行并验证重复迁移。"""

from uuid import uuid4

TABLES = ['family_members','reports','metrics','report_chunks','query_logs','source_uploads','report_revisions','revision_sources','idempotency_records']


def signature(conn, schema):
    from psycopg import sql
    return {table: conn.execute(sql.SQL("SELECT count(*),md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]')) FROM {}.{} t")
            .format(sql.Identifier(schema),sql.Identifier(table))).fetchone() for table in TABLES}


def rehearse(root, base_connect):
    from psycopg import sql
    from app.migrate_m11 import migrate
    schema='m11_rehearsal_'+uuid4().hex
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
            if conn.execute("SELECT 1 FROM engineering_migrations WHERE version='health_m11_v1'").fetchone():
                raise RuntimeError('实际库已迁移 M11；应保留原演练记录，不在升级后重造升级前证据。')
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created=True
        with base_connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            before=signature(conn,source)
            for table in [*TABLES,'engineering_migrations']:
                conn.execute(sql.SQL('CREATE TABLE {}.{} (LIKE {}.{} INCLUDING ALL)').format(
                    sql.Identifier(schema),sql.Identifier(table),sql.Identifier(source),sql.Identifier(table)))
                conn.execute(sql.SQL('INSERT INTO {}.{} OVERRIDING SYSTEM VALUE SELECT * FROM {}.{}').format(
                    sql.Identifier(schema),sql.Identifier(table),sql.Identifier(source),sql.Identifier(table)))
            copied=signature(conn,schema)
        first=migrate(scoped);second=migrate(scoped)
        with base_connect() as conn:
            after=signature(conn,schema);shared_after=signature(conn,source)
        result.update(passed=before==copied==after and before==shared_after and second['already_applied'],
                      original_fields_unchanged=before==after,shared_rows_unchanged=before==shared_after,
                      source_rows_and_hashes=before,clone_before=copied,clone_after=after,shared_after=shared_after,
                      migration_first=first,migration_second=second,real_data_in_output=False,
                      clone_scope='LIKE INCLUDING ALL plus rows; existing business foreign keys are not recreated; M11 new foreign keys are validated')
    except Exception as error:
        result.update(passed=False,error_type=type(error).__name__,message=str(error))
    finally:
        if created:
            try:
                with base_connect() as conn:conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                result['temporary_schema_cleaned']=True
            except Exception as error:
                result.update(passed=False,cleanup_error_type=type(error).__name__)
    return result
