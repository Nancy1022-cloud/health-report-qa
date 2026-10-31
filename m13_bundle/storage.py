"""Quiescent backup fingerprints and file archive operations; no model requests."""
import hashlib
import json
import os
import zipfile
from pathlib import Path


def file_manifest(folder):
    result = {}
    if not folder.is_dir():
        raise RuntimeError('UPLOAD_DIR is missing; check the actual persistent mount')
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('File backup stopped at a symbolic link')
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            result[path.relative_to(folder).as_posix()] = {'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}
    return result


def database_manifest(connect):
    from psycopg import sql
    with connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        tables = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
        rows = {}
        for (table,) in tables:
            query = sql.SQL("SELECT count(*),md5(COALESCE(string_agg(md5(row_to_json(t)::text),'' ORDER BY md5(row_to_json(t)::text)),'')) FROM public.{} t").format(sql.Identifier(table))
            count, digest = conn.execute(query).fetchone()
            rows[table] = {'count': count, 'content_md5': digest}
        columns = conn.execute("SELECT table_name,column_name,ordinal_position,data_type,udt_name,is_nullable,column_default,is_identity,identity_generation FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position").fetchall()
        constraints = conn.execute("SELECT c.relname,p.conname,pg_get_constraintdef(p.oid) FROM pg_constraint p JOIN pg_class c ON c.oid=p.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname,p.conname").fetchall()
        indexes = conn.execute("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname").fetchall()
        sequences = {}
        for (sequence,) in conn.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename").fetchall():
            sequences[sequence] = list(conn.execute(sql.SQL('SELECT last_value,is_called FROM public.{}').format(sql.Identifier(sequence))).fetchone())
        extensions = conn.execute("SELECT extname,extversion FROM pg_extension ORDER BY extname").fetchall()
    # Normalize tuples to the same JSON types used when reading a saved snapshot.
    return json.loads(json.dumps({'tables': rows, 'columns': columns, 'constraints': constraints,
                                  'indexes': indexes, 'sequences': sequences, 'extensions': extensions}, default=str))


def snapshot():
    from app.db import connect_db
    with connect_db() as conn:
        active = conn.execute("SELECT count(*) FROM processing_tasks WHERE status IN ('queued','running')").fetchone()[0]
    if active:
        raise RuntimeError('Queued/running tasks exist. Finish or reconcile them before a quiescent backup')
    return {'database': database_manifest(connect_db),
            'files': file_manifest(Path(os.environ.get('UPLOAD_DIR', 'uploads')).resolve()),
            'active_tasks': active, 'external_model_calls': 0}


def archive_uploads(destination):
    folder = Path(os.environ.get('UPLOAD_DIR', 'uploads')).resolve()
    expected = file_manifest(folder)
    destination = destination.resolve()
    if destination.is_relative_to(folder):
        raise RuntimeError('Archive destination must be outside UPLOAD_DIR')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in expected:
            archive.write(folder / name, name)
    if file_manifest(folder) != expected:
        raise RuntimeError('Uploads changed during backup; retain the files and retry while writers are stopped')
    return {'passed': True, 'files': len(expected), 'archive': str(destination), 'external_model_calls': 0}


def restore_files(archive_path, destination):
    destination = destination.resolve()
    if destination.exists():
        raise RuntimeError('Restore directory already exists; use a fresh drill directory')
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination) or '\\' in member.filename or member.filename.startswith('/'):
                raise RuntimeError('Unsafe upload archive entry')
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError('Symbolic links are not allowed in a restore archive')
        destination.mkdir(parents=True)
        archive.extractall(destination)
    return {'passed': True, 'files': len(file_manifest(destination)), 'external_model_calls': 0}
