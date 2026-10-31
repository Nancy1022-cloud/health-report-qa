"""已进入版本治理的数据库，禁止旧导入/索引脚本绕过修订事务。"""


def require_legacy_database(conn):
    if conn.execute("SELECT to_regclass('engineering_migrations')").fetchone()[0] is not None:
        raise RuntimeError("此数据库已进入工程化版本治理；旧写入脚本停止。请使用 M10 修订接口，重建时传 rebuild_index=true。")
