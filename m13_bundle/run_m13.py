"""Final engineering checks and evidence-driven delivery. Never calls paid models."""
import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.cwd().resolve()
BUNDLE = Path(__file__).resolve().parent
EVIDENCE = ROOT / 'app/data/m13/v1'
sys.path.insert(0, str(ROOT))


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    os.replace(temporary, path)


def digest(path, binary=False):
    content = path.read_bytes() if binary else path.read_text(encoding='utf-8-sig').replace('\r\n', '\n').encode()
    return hashlib.sha256(content).hexdigest()


def relative_path(name):
    path = (ROOT / name).resolve()
    if path == ROOT or not path.is_relative_to(ROOT):
        raise RuntimeError('Path must be below the project root')
    return path


def integrity():
    spec = read(BUNDLE / 'manifest.json')
    for name, expected in spec['files'].items():
        path = BUNDLE / name
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError('M13 package checksum mismatch: ' + name)
    return spec


def source_manifest():
    names = set()
    for folder in [ROOT / 'app', *[ROOT / f'm{n}_bundle' for n in range(8, 14)]]:
        for path in folder.rglob('*'):
            if path.is_file() and path.suffix in {'.py', '.ps1', '.sql', '.html', '.json', '.yaml', '.yml'}:
                name = path.relative_to(ROOT).as_posix()
                if not name.startswith('app/data/') and '__pycache__' not in path.parts and not path.name.startswith('LOCAL_VALIDATION'):
                    names.add(name)
    for name in ['Dockerfile', 'requirements.txt', 'compose.yaml', 'compose.m11.yaml', 'compose.m12.yaml', '.github/workflows/health-m13.yml']:
        if (ROOT / name).is_file():
            names.add(name)
    return {name: digest(ROOT / name) for name in sorted(names)}


def prior_signature():
    paths = []
    for n in range(9, 13):
        folder = ROOT / f'app/data/m{n}/v1'
        if not (folder / 'summary.json').is_file() or read(folder / 'summary.json').get('passed') is not True:
            raise RuntimeError(f'M{n} acceptance is missing or failed. Restore its original evidence')
        paths.extend(p for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    return {p.relative_to(ROOT).as_posix(): digest(p, True) for p in sorted(paths)}


def preflight():
    integrity()
    from m12_bundle import run_m12
    run_m12.require_applied()
    current_m12 = run_m12.source_manifest()
    m12 = read(ROOT / 'app/data/m12/v1/summary.json')
    if m12.get('source_manifest') != current_m12 or m12.get('technical_gate_passed') is not True:
        raise RuntimeError('Installed source does not match completed M12 evidence; do not recreate V0 or edit passed flags')
    signature = prior_signature()
    saved = EVIDENCE / 'prior_evidence_signature.json'
    if saved.is_file() and read(saved) != signature:
        raise RuntimeError('M9-M12 evidence changed after M13 began; preserve originals and investigate')
    if not saved.is_file():
        write(saved, signature)
    value = {'passed': True, 'M12_acceptance_passed': True, 'M12_full_scope_completed': m12.get('full_scope_completed') is True,
             'real_measurement_completed': m12.get('real_measurement_completed') is True,
             'previous_evidence_preserved': True, 'database_migration_required': False,
             'app_stage_remains': 'M12', 'external_model_calls': 0}
    write(EVIDENCE / 'preflight.json', value)
    return value


def require_prior():
    if not (EVIDENCE / 'prior_evidence_signature.json').is_file():
        raise RuntimeError('Run preflight first')
    return preflight()


def recompute():
    require_prior()
    from m12_bundle.analysis import summarize_groups, comparison
    load = read(ROOT / 'app/data/m12/v1/load/summary.json')
    raw = relative_path(load['output'])
    requests, tasks, calls = [read(raw / (name + '.json')) for name in ['requests', 'tasks', 'calls']]
    keys = {'group', 'batch_size', 'concurrency', 'n', 'workload', 'wall_seconds', 'passed', 'fixture_sha256',
            'task_policy', 'started_monotonic_s', 'ended_monotonic_s'}
    groups = summarize_groups(requests, tasks, calls, [{k: v for k, v in g.items() if k in keys} for g in load['groups']])
    saved_groups = read(ROOT / 'app/data/m12/v1/analysis/recomputed_groups.json')
    saved_comparison = read(ROOT / 'app/data/m12/v1/analysis/comparison.json')
    result = {'passed': groups == saved_groups and comparison(groups) == saved_comparison,
              'raw_records_recomputed': True, 'groups_equal': groups == saved_groups,
              'comparison_equal': comparison(groups) == saved_comparison,
              'source_manifest': source_manifest(), 'external_model_calls': 0,
              'scope': 'Recompute saved controlled results without rerunning or overwriting M12'}
    write(EVIDENCE / 'analysis_check/summary.json', result)
    return result


def apply():
    require_prior()
    spec = integrity()
    installation = EVIDENCE / 'installation.json'
    if installation.is_file():
        for name, expected in spec['payload'].items():
            if not (ROOT / name).is_file() or digest(ROOT / name) != expected:
                raise RuntimeError('Installed delivery file changed; no later edits were overwritten: ' + name)
        return {'passed': True, 'already_applied': True, 'backup': read(installation)['backup']}
    backup = ROOT / 'backups' / ('health_m13_' + stamp())
    entries = []
    for name in spec['payload']:
        path = relative_path(name)
        if path.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, saved)
        entries.append((name, path.exists()))
    ignore = ROOT / '.gitignore'
    if ignore.is_file():
        backup.mkdir(parents=True, exist_ok=True); shutil.copyfile(ignore, backup / '.gitignore')
    try:
        for name in spec['payload']:
            path = relative_path(name); path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(BUNDLE / 'payload' / name, path)
        old = ignore.read_text(encoding='utf-8-sig') if ignore.is_file() else ''
        extra = ['/' + f'app/data/m{n}/' for n in range(8, 14)] + ['/backups/', '/artifacts/', '/Health_Report_*acceptance_materials.zip', '/M13-ci.zip']
        ignore.write_text(old.rstrip() + '\n\n# M13 local evidence and private backups\n' + '\n'.join(x for x in extra if x not in old.splitlines()) + '\n', encoding='utf-8')
    except BaseException:
        for name, existed in reversed(entries):
            path = relative_path(name)
            if existed: shutil.copyfile(backup / name, path)
            else: path.unlink(missing_ok=True)
        if (backup / '.gitignore').is_file(): shutil.copyfile(backup / '.gitignore', ignore)
        raise
    result = {'passed': True, 'backup': backup.relative_to(ROOT).as_posix(), 'installed_files': list(spec['payload']),
              'application_source_changed': False, 'external_model_calls': 0}
    write(installation, result)
    return result


def subprocess_environment():
    env = os.environ.copy()
    env['M13_BLOCK_EXTERNAL'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONPATH'] = os.pathsep.join([str(BUNDLE / 'netguard'), str(ROOT), env.get('PYTHONPATH', '')])
    for name in ['LLM', 'VISION', 'EMBEDDING']:
        env[name + '_API_KEY'] = 'm13-controlled-no-secret'
        env[name + '_BASE_URL'] = 'http://127.0.0.1:9/v1'
        env[name + '_MODEL'] = 'controlled-m13'
    env.setdefault('EMBEDDING_DIMENSIONS', '1024')
    return env


def command(args, log, timeout=600):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('w', encoding='utf-8') as stream:
        try:
            result = subprocess.run(args, cwd=ROOT, env=subprocess_environment(), stdout=stream, stderr=subprocess.STDOUT,
                                    text=True, timeout=timeout)
            return result.returncode
        except subprocess.TimeoutExpired:
            stream.write('\nM13 command timed out; partial output retained.\n')
            return 124


def bootstrap_ci():
    if os.getenv('M13_CI_DISPOSABLE') != '1':
        raise RuntimeError('CI bootstrap is allowed only in its disposable database')
    from app.db import connect_db
    with connect_db() as conn:
        if conn.execute("SELECT to_regclass('public.family_members')").fetchone()[0] is not None:
            raise RuntimeError('CI bootstrap requires an empty disposable database')
        conn.execute('CREATE EXTENSION IF NOT EXISTS vector')


def check(database=False, ci=False):
    integrity()
    if ci: bootstrap_ci()
    output = ROOT / 'artifacts/m13' if ci else EVIDENCE / 'checks' / ('run_' + stamp())
    modules = ['app.m5_check', 'app.m6_check', 'app.m7_check', 'app.m8_check', 'app.m9_check', 'app.m10_check', 'app.m11_check',
               'm9_bundle.tests.test_installer', 'm10_bundle.tests.test_installer', 'm11_bundle.tests.test_installer',
               'm12_bundle.tests.test_m12', 'm12_bundle.tests.test_installer', 'm13_bundle.tests.test_m13',
               'm13_bundle.tests.test_recovery_support']
    code = command([sys.executable, '-m', 'unittest', *modules, '-q'], output / 'unit_tests.txt')
    unit_text = (output / 'unit_tests.txt').read_text(encoding='utf-8')
    count = re.search(r'Ran (\d+) tests', unit_text)
    runs = []
    shared_unchanged = None
    if database and code == 0:
        from m12_bundle.environment import shared_signature
        shared_before = shared_signature()
        for name, probe in [('legacy_business', 'm12_bundle/tests/legacy_probe.py'),
                            ('m10_consistency', 'm11_bundle/tests/m10_regression_probe.py'),
                            ('m11_recovery', 'm13_bundle/recovery_probe.py'),
                            ('m12_batch_recovery', 'm12_bundle/tests/integration_probe.py')]:
            folder = output / name
            probe_args = [sys.executable, str(ROOT / probe)]
            if name == 'legacy_business': probe_args.extend(['--root', str(ROOT)])
            code_probe = command(probe_args + ['--output', str(folder / 'behavior.json' if name == 'legacy_business' else folder)], folder / 'probe_output.txt')
            path = folder / ('behavior.json' if name == 'legacy_business' else 'summary.json')
            item = read(path) if path.is_file() else {}
            runs.append({'name': name, 'passed': code_probe == 0 and item.get('passed') is True
                         and item.get('temporary_schema_cleaned') is True,
                         'scenario_checks': len(item.get('checks', [])), 'result': path.relative_to(ROOT).as_posix()})
            print(json.dumps(runs[-1]), flush=True)
        shared_unchanged = shared_before == shared_signature()
    try:
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        sha = None
    result = {'passed': code == 0 and (not database or len(runs) == 4 and all(r['passed'] for r in runs) and shared_unchanged is True),
              'unit_tests': int(count[1]) if count else None, 'unit_exit_code': code,
              'database_checks_executed': database and len(runs) == 4,
              'scenario_checks': sum(r['scenario_checks'] for r in runs), 'runs': runs,
              'shared_business_rows_unchanged': shared_unchanged,
              'external_model_calls': 0, 'external_connections_blocked': True,
              'source_manifest': source_manifest(), 'output': output.relative_to(ROOT).as_posix(),
              'commit_sha': sha, 'github_run_id': os.getenv('GITHUB_RUN_ID'),
              'scope': 'Controlled engineering and citation regression; test count is not model accuracy'}
    write(output / ('ci_summary.json' if ci else 'summary.json'), result)
    if not ci: write(EVIDENCE / 'checks/summary.json', result)
    print(unit_text[-500:])
    return result


def recovery():
    """Run only the failing recovery probe and retain diagnostics; do not reuse it as full acceptance."""
    require_prior()
    from m12_bundle.environment import shared_signature
    before = shared_signature()
    output = EVIDENCE / 'recovery_diagnostic' / ('run_' + stamp())
    code = command([sys.executable, str(BUNDLE / 'recovery_probe.py'), '--output', str(output)], output / 'probe_output.txt')
    item = read(output / 'summary.json') if (output / 'summary.json').is_file() else {}
    result = {'passed': code == 0 and item.get('passed') is True and item.get('temporary_schema_cleaned') is True,
              'checks': item.get('checks', []), 'scenario_checks': len(item.get('checks', [])),
              'shared_business_rows_unchanged': before == shared_signature(),
              'temporary_schema_cleaned': item.get('temporary_schema_cleaned') is True,
              'error_type': item.get('error_type'), 'message': item.get('message'),
              'output': output.relative_to(ROOT).as_posix(), 'source_manifest': source_manifest(),
              'external_model_calls': 0, 'full_engineering_acceptance': False}
    result['passed'] = result['passed'] and result['shared_business_rows_unchanged']
    write(output / 'diagnostic_summary.json', result)
    write(EVIDENCE / 'recovery_diagnostic/summary.json', result)
    return result


def require_result(name):
    path = EVIDENCE / name / 'summary.json'
    item = read(path) if path.is_file() else {}
    if item.get('passed') is not True or item.get('source_manifest') != source_manifest():
        raise RuntimeError('Missing or stale successful result: ' + name)
    return item


def pending(name):
    write(EVIDENCE / name / 'summary.json', {'passed': False, 'state': 'started_not_completed', 'source_manifest': source_manifest()})


def start_backup():
    require_prior()
    for name in ['restore', 'restart', 'live']: pending(name)
    return {'passed': True, 'new_backup_attempt_started': True, 'external_model_calls': 0}


def demo():
    require_prior()
    pending('demos')
    checked = require_result('checks')
    if checked.get('database_checks_executed') is not True:
        raise RuntimeError('Run check --database first')
    output = EVIDENCE / 'demos' / ('run_' + stamp())
    code = command([sys.executable, str(BUNDLE / 'demo_probe.py'), '--output', str(output / 'normal_and_revision')], output / 'probe_output.txt')
    item_path = output / 'normal_and_revision/summary.json'
    item = read(item_path) if item_path.is_file() else {}
    recovery_run = next((r for r in checked['runs'] if r['name'] == 'm12_batch_recovery'), {})
    recovery = read(relative_path(recovery_run['result'])) if recovery_run else {}
    needed = {'persisted_batch_reused_after_process_crash', 'unsaved_batch_may_resend_and_is_unknown', 'batch_idempotent_replay'}
    satisfied = {r['name'] for r in recovery.get('checks', []) if r.get('passed') is True}
    result = {'passed': code == 0 and item.get('passed') is True and needed.issubset(satisfied),
              'normal_upload_cited_qa': item.get('passed') is True,
              'duplicate_and_process_recovery': needed.issubset(satisfied),
              'revision_and_provenance': item.get('passed') is True,
              'process_recovery_result': recovery_run.get('result'),
              'output': output.relative_to(ROOT).as_posix(), 'source_manifest': source_manifest(),
              'external_model_calls': 0, 'model_source': 'controlled', 'real_model_quality_evaluation': 'not_executed_by_M13'}
    write(output / 'summary.json', result); write(EVIDENCE / 'demos/summary.json', result)
    return result


def live(base_url):
    require_prior()
    pending('live')
    checks = []
    for route in ['/health', '/api/tasks/worker', '/openapi.json', '/', '/upload']:
        with urllib.request.urlopen(base_url.rstrip('/') + route, timeout=15) as response:
            raw = response.read(); ok = response.status == 200
            data = json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else None
            if route == '/health': ok = ok and data.get('stage') == 'M12'
            if route == '/api/tasks/worker': ok = ok and data.get('online') is True
            if route == '/openapi.json': ok = ok and data.get('info', {}).get('version') == '0.12.0'
            checks.append({'route': route, 'passed': ok})
    result = {'passed': all(r['passed'] for r in checks), 'checks': checks, 'source_manifest': source_manifest(), 'external_model_calls': 0}
    write(EVIDENCE / 'live/summary.json', result)
    return result


def compare_snapshot(expected_path, restored=False):
    from m13_bundle.storage import snapshot
    before, after = read(expected_path), snapshot()
    if not restored:
        # Worker heartbeat is expected to change after service startup; task/business rows must stay identical.
        for item in [before, after]:
            item['database']['tables'].pop('processing_worker_state', None)
    quality_ok, migrations_ok = True, True
    if restored:
        from app.db import connect_db
        from app.data_audit import audit
        from app.repositories.drafts import DraftRepository
        from app.migrate_m10 import migrate as m10
        from app.migrate_m11 import migrate as m11
        quality_ok = audit(connect_db, DraftRepository())['passed'] is True
        migrations_ok = m10(connect_db).get('already_applied') is True and m11(connect_db).get('already_applied') is True
    result = {'passed': before == after and quality_ok and migrations_ok, 'database_equal': before['database'] == after['database'],
              'upload_files_equal': before['files'] == after['files'], 'file_count': len(after['files']),
              'table_count': len(after['database']['tables']), 'active_tasks': after['active_tasks'],
              'restored_quality_passed': quality_ok, 'migration_replay_unchanged': migrations_ok,
              'mode': 'isolated_database_and_upload_restore' if restored else 'service_restart_persistence',
              'source_manifest': source_manifest(), 'external_model_calls': 0}
    write(EVIDENCE / ('restore' if restored else 'restart') / 'summary.json', result)
    return result


def import_ci(artifact, url):
    require_prior()
    pending('ci')
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/actions/runs/(\d+)/?', url)
    if not match: raise RuntimeError('Expected the GitHub Actions run URL')
    owner, repo, run_id = match.groups()
    git_dir = ROOT / '.git'
    if git_dir.is_file():
        line = git_dir.read_text(encoding='utf-8').strip()
        if not line.startswith('gitdir: '): raise RuntimeError('Unrecognized Git metadata')
        git_dir = (ROOT / line[8:]).resolve()
        if (git_dir / 'commondir').is_file():
            git_dir = (git_dir / (git_dir / 'commondir').read_text().strip()).resolve()
    config = configparser.ConfigParser(interpolation=None)
    config.read(git_dir / 'config', encoding='utf-8')
    origin = config.get('remote "origin"', 'url', fallback='')
    if not re.search(re.escape(owner + '/' + repo) + r'(?:\.git)?$', origin, re.I):
        raise RuntimeError('CI URL does not match this project origin')
    with zipfile.ZipFile(artifact) as archive:
        members = [x for x in archive.namelist() if x == 'ci_summary.json' or x.endswith('/ci_summary.json')]
        if len(members) != 1 or archive.getinfo(members[0]).file_size > 10_000_000:
            raise RuntimeError('Expected exactly one bounded ci_summary.json in the downloaded artifact')
        item = json.loads(archive.read(members[0]))
    api = f'https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}'
    req = urllib.request.Request(api, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'health-report-qa-m13'})
    with urllib.request.urlopen(req, timeout=30) as response:
        run = json.load(response)
    passed = all([run.get('status') == 'completed', run.get('conclusion') == 'success',
                  run.get('path') == '.github/workflows/health-m13.yml',
                  item.get('passed') is True, item.get('database_checks_executed') is True,
                  item.get('external_model_calls') == 0, item.get('external_connections_blocked') is True,
                  str(item.get('github_run_id')) == run_id, item.get('commit_sha') == run.get('head_sha'),
                  item.get('source_manifest') == source_manifest()])
    result = {'passed': passed, 'url': url, 'head_sha': run.get('head_sha'), 'conclusion': run.get('conclusion'),
              'unit_tests': item.get('unit_tests'), 'scenario_checks': item.get('scenario_checks'),
              'tested_source_matches': item.get('source_manifest') == source_manifest(),
              'source_manifest': source_manifest(), 'external_model_calls': 0}
    write(EVIDENCE / 'ci/summary.json', result)
    return result


def facts():
    m12 = read(ROOT / 'app/data/m12/v1/summary.json')
    load = read(ROOT / 'app/data/m12/v1/load/summary.json')
    analysis = read(ROOT / 'app/data/m12/v1/analysis/summary.json')
    local = require_result('checks')
    real_path = ROOT / 'app/data/m12/v1/real/summary.json'
    real = read(real_path) if real_path.is_file() else {}
    return {'local_unit_tests': local.get('unit_tests'), 'engineering_scenario_checks': local.get('scenario_checks'),
            'independent_requests': load.get('independent_requests'), 'duplicate_requests': load.get('duplicate_requests'),
            'M12_full_scope_completed': m12.get('full_scope_completed') is True,
            'real_embedding_tasks': real.get('observed_unique_tasks') if m12.get('real_measurement_completed') is True else None,
            'real_measurement_completed': m12.get('real_measurement_completed') is True,
            'deployment_recommendation': analysis.get('findings', {}).get('deployment_recommendation'),
            'real_model_QA_quality': 'M13 does not measure real-model QA quality',
            'scope': 'Local synthetic controlled engineering evidence; no production-capacity claim'}


def documents():
    require_prior()
    pending('documents')
    data = facts()
    for name in ['demos', 'restore', 'restart', 'live', 'analysis_check']: require_result(name)
    template = (BUNDLE / 'README_TEMPLATE.md').read_text(encoding='utf-8')
    for key, value in data.items():
        template = template.replace('{{' + key + '}}', '未完成／未知' if value is None else str(value))
    target = ROOT / 'README.md'
    backup = EVIDENCE / 'readme_original_saved.json'
    if not backup.is_file():
        folder = ROOT / 'backups' / ('health_m13_readme_' + stamp()); folder.mkdir(parents=True)
        if target.is_file(): shutil.copyfile(target, folder / 'README.md')
        write(backup, {'backup': folder.relative_to(ROOT).as_posix()})
    target.write_text(template, encoding='utf-8')
    write(ROOT / 'docs/results/m13_public_facts.json', data)
    for filename in ['comparison.csv', 'latency.svg']:
        shutil.copyfile(ROOT / 'app/data/m12/v1/analysis' / filename, ROOT / 'docs/results' / ('m12_' + filename))
    write(EVIDENCE / 'documents/summary.json', {'passed': True, 'source_manifest': source_manifest(),
          'README_sha256': digest(target), 'public_facts_sha256': digest(ROOT / 'docs/results/m13_public_facts.json'),
          'facts': data, 'external_model_calls': 0})
    return {'passed': True, 'README_updated': True, 'facts': data}


def completion(gates, manual, ci_ok, m12_full, gap_reason):
    technical = bool(gates) and all(gates.values())
    acknowledged = ci_ok or bool(gap_reason and len(gap_reason.strip()) >= 4)
    return {'passed': technical and manual and acknowledged, 'technical_gate_passed': technical,
            'manual_review_completed': manual, 'remote_ci_verified': ci_ok,
            'full_scope_completed': technical and manual and ci_ok and m12_full,
            'completion_scope': 'full_project_delivery' if technical and manual and ci_ok and m12_full
            else 'local_delivery_with_explicit_remaining_gaps', 'ci_gap_reason': None if ci_ok else gap_reason}


def finalize(manual, gap_reason):
    prior = require_prior()
    gates = {}
    for name in ['checks', 'demos', 'restore', 'restart', 'live', 'analysis_check', 'documents']:
        try:
            item = require_result(name)
            gates[name] = name != 'checks' or item.get('database_checks_executed') is True
        except RuntimeError:
            gates[name] = False
    docs_path = EVIDENCE / 'documents/summary.json'
    if docs_path.is_file():
        docs = read(docs_path)
        gates['README_matches_reviewed_generation'] = (ROOT / 'README.md').is_file() and docs.get('README_sha256') == digest(ROOT / 'README.md')
        gates['public_facts_match'] = (ROOT / 'docs/results/m13_public_facts.json').is_file() and docs.get('public_facts_sha256') == digest(ROOT / 'docs/results/m13_public_facts.json')
    try: ci_ok = require_result('ci').get('tested_source_matches') is True
    except RuntimeError: ci_ok = False
    result = completion(gates, manual, ci_ok, prior['M12_full_scope_completed'], gap_reason)
    result.update(checks=gates, M12_full_scope_completed=prior['M12_full_scope_completed'],
                  real_measurement_completed=prior['real_measurement_completed'], previous_evidence_preserved=True,
                  source_manifest=source_manifest(), external_model_calls=0, facts=facts())
    write(EVIDENCE / 'summary.json', result)
    text = '# 健康报告项目最终验收\n\n' + '\n'.join(f'- {k}：{v}' for k, v in result.items() if k not in {'source_manifest', 'facts'})
    text += '\n\n本记录区分单元测试数、工程场景检查、受控负载与真实 Embedding 用量；M13 未重新评测真实模型问答准确率。\n'
    (EVIDENCE / 'M13_ACCEPTANCE.md').write_text(text, encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest='action', required=True)
    for name in ['preflight', 'apply', 'demo', 'documents', 'recompute', 'start-backup', 'recovery']: subs.add_parser(name)
    p = subs.add_parser('check'); p.add_argument('--database', action='store_true')
    subs.add_parser('ci')
    p = subs.add_parser('live'); p.add_argument('--base-url', default='http://app:8000')
    p = subs.add_parser('snapshot'); p.add_argument('--output', required=True)
    p = subs.add_parser('archive-files'); p.add_argument('--output', required=True)
    p = subs.add_parser('restore-files'); p.add_argument('--archive', required=True); p.add_argument('--destination', required=True)
    for name in ['compare-restore', 'compare-restart']:
        p = subs.add_parser(name); p.add_argument('--snapshot', required=True)
    p = subs.add_parser('import-ci'); p.add_argument('--artifact', required=True); p.add_argument('--url', required=True)
    p = subs.add_parser('finalize'); p.add_argument('--manual-reviewed', action='store_true'); p.add_argument('--ci-gap-reason')
    args = parser.parse_args()
    try:
        if args.action == 'preflight': result = preflight()
        elif args.action == 'apply': result = apply()
        elif args.action == 'check': require_prior(); result = check(args.database)
        elif args.action == 'recovery': result = recovery()
        elif args.action == 'ci': result = check(True, True)
        elif args.action == 'demo': result = demo()
        elif args.action == 'documents': result = documents()
        elif args.action == 'recompute': result = recompute()
        elif args.action == 'start-backup': result = start_backup()
        elif args.action == 'live': result = live(args.base_url)
        elif args.action == 'import-ci': result = import_ci(relative_path(args.artifact), args.url)
        elif args.action == 'finalize': result = finalize(args.manual_reviewed, args.ci_gap_reason)
        else:
            from m13_bundle import storage
            if args.action == 'snapshot':
                result = storage.snapshot(); write(relative_path(args.output), result)
                result = {'passed': True, 'snapshot_saved': True, 'file_count': len(result['files']), 'external_model_calls': 0}
            elif args.action == 'archive-files': result = storage.archive_uploads(relative_path(args.output))
            elif args.action == 'restore-files': result = storage.restore_files(relative_path(args.archive), relative_path(args.destination))
            else: result = compare_snapshot(relative_path(args.snapshot), args.action == 'compare-restore')
        visible = {k: v for k, v in result.items() if k not in {'source_manifest', 'facts'}}
        if args.action == 'recovery': visible.pop('checks', None)
        print(json.dumps(visible, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get('passed', True) else 1
    except Exception as error:
        result = {'passed': False, 'error_type': type(error).__name__, 'message': str(error)[:800]}
        write(EVIDENCE / ('error_' + args.action + '.json'), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
