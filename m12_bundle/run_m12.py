"""M12 install, evidence-preserving experiments, analysis and acceptance."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
ROOT = Path.cwd().resolve()
EVIDENCE = ROOT / 'app/data/m12/v1'
sys.path.insert(0, str(ROOT))


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, value):
    from m12_bundle.analysis import write as save
    save(path, value)


def digest(path, binary=False):
    data = path.read_bytes() if binary else path.read_text(encoding='utf-8-sig').replace('\r\n', '\n').encode()
    return hashlib.sha256(data).hexdigest()


def target(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise RuntimeError('Invalid project-relative path')
    return path


def manifest():
    value = read(BUNDLE / 'manifest.json')
    for group, prefix in [('payload', BUNDLE / 'payload'), ('harness', BUNDLE)]:
        for relative, expected in value[group].items():
            path = (prefix / relative).resolve()
            if not path.is_relative_to(prefix) or not path.is_file() or digest(path) != expected:
                raise RuntimeError('Bundle checksum mismatch: ' + relative)
            if path.suffix == '.py':
                compile(path.read_text(encoding='utf-8-sig'), str(path), 'exec')
    for relative, expected in value.get('prerequisite_files', {}).items():
        path = target(relative)
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError('Preserved prerequisite probe changed: ' + relative)
    return value


def source_manifest():
    value = manifest()
    paths = set(value['base_files']) | set(value['payload'])
    if (ROOT / 'app/static/index.html').is_file():
        paths.add('app/static/index.html')
    result = {p: digest(target(p)) for p in sorted(paths)}
    result.update({'m12_bundle/' + p: digest(BUNDLE / p) for p in value['harness']})
    result.update({p: digest(target(p)) for p in value.get('prerequisite_files', {})})
    result['m12_bundle/manifest.json'] = digest(BUNDLE / 'manifest.json')
    return result


def protected_signature():
    folder = ROOT / 'app/data/m9/v1/V0'
    required = ['summary.json', 'source_manifest.json', 'source_snapshot.zip', 'task_manifest.json', 'harness/tests/baseline_probe.py', 'harness_manifest.json']
    if not all((folder / p).is_file() for p in required) or not read(folder / 'summary.json').get('passed'):
        raise RuntimeError('Missing original successful M9 V0, source snapshot or probe; restore the originals, do not recreate V0.')
    for relative, expected in read(folder / 'harness_manifest.json').items():
        path = (folder / 'harness' / relative).resolve()
        if not path.is_relative_to((folder / 'harness').resolve()) or not path.is_file() or digest(path, True) != expected:
            raise RuntimeError('Original V0 probe/harness checksum changed: ' + relative)
    preservation = ROOT / 'app/data/m11/v1/v0_preservation.json'
    if not preservation.is_file():
        raise RuntimeError('Missing M11 v0_preservation.json; restore the original M11 evidence.')
    prior = read(preservation)
    originals = {p: digest(folder / p, True) for p in ['summary.json', 'source_manifest.json', 'source_snapshot.zip', 'task_manifest.json']}
    m10 = ROOT / 'app/data/m10/v1/summary.json'
    if prior.get('M9_V0') != originals or not m10.is_file() or prior.get('M10_summary') != digest(m10, True):
        raise RuntimeError('V0 or M10 evidence no longer matches the signature saved during M11. Preserve and restore the originals.')
    paths = [p for p in folder.rglob('*') if p.is_file()]
    for stage in [10, 11]:
        path = ROOT / f'app/data/m{stage}/v1/summary.json'
        if not path.is_file() or read(path).get('passed') is not True:
            raise RuntimeError(f'Complete M{stage} finalize before starting M12.')
        paths.extend(p for p in path.parent.rglob('*') if p.is_file())
    return {str(p.relative_to(ROOT)): digest(p, True) for p in sorted(paths)}


def preflight():
    if not (ROOT / 'compose.yaml').is_file():
        raise RuntimeError('Run from the health-report-qa project root.')
    value = manifest()
    before = after = True
    differences = []
    for relative in set(value['base_files']) | set(value['payload']):
        path = target(relative)
        actual = digest(path) if path.is_file() else None
        old, new = value['base_files'].get(relative), value['payload'].get(relative, value['base_files'].get(relative))
        before = before and actual == old
        after = after and actual == new
        if actual not in {old, new}:
            differences.append(relative)
    state = 'diverged' if differences else 'applied' if after else 'm11_ready' if before else 'partial'
    signature = protected_signature()
    return {'state': state, 'passed': state in {'m11_ready', 'applied'}, 'source_matches': not differences,
            'differences': sorted(differences), 'previous_evidence_files': len(signature),
            'M9_V0_available': True, 'M11_acceptance_passed': True, 'database_migration_required': False,
            'external_model_calls': 0}


def require_applied():
    value = preflight()
    if value['state'] != 'applied':
        raise RuntimeError('M12 source is not fully installed; inspect preflight.json.')
    saved = EVIDENCE / 'previous_evidence_signature.json'
    if not saved.is_file() or read(saved) != protected_signature():
        raise RuntimeError('Previous-stage evidence changed or signature is missing; preserve the originals and investigate.')


def apply():
    value = preflight()
    if value['state'] == 'applied':
        require_applied()
        return {'already_applied': True, 'passed': True}
    if value['state'] != 'm11_ready':
        raise RuntimeError('Unknown source changes; no files were overwritten.')
    spec = manifest()
    preserved = protected_signature()
    backup = ROOT / 'backups' / ('health_m12_' + stamp())
    entries = []
    backup.mkdir(parents=True)
    for relative in spec['payload']:
        path = target(relative)
        existed = path.is_file()
        if existed:
            original = backup / 'original' / relative
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, original)
        entries.append({'path': relative, 'existed': existed,
                        'before_binary_sha256': digest(path, True) if existed else None, 'after_sha256': spec['payload'][relative]})
    write(backup / 'rollback.json', {'entries': entries, 'backup': str(backup.relative_to(ROOT))})
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(EVIDENCE / 'M11_source_snapshot.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for relative in spec['base_files']:
            archive.write(target(relative), relative)
    installed = []
    try:
        for entry in entries:
            path = target(entry['path'])
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.m12-install-tmp')
            shutil.copyfile(BUNDLE / 'payload' / entry['path'], temporary)
            os.replace(temporary, path)
            installed.append(entry)
    except BaseException:
        for entry in reversed(installed):
            path = target(entry['path'])
            if entry['existed']:
                shutil.copyfile(backup / 'original' / entry['path'], path)
            else:
                path.unlink(missing_ok=True)
        for entry in entries:
            path = target(entry['path'])
            path.with_name(path.name + '.m12-install-tmp').unlink(missing_ok=True)
        raise
    write(EVIDENCE / 'previous_evidence_signature.json', preserved)
    result = {'passed': True, 'applied': True, 'backup': str(backup.relative_to(ROOT)), 'payload_files': len(entries),
              'new_tasks_default_batch_size': 1, 'database_migration_required': False}
    write(EVIDENCE / 'installation.json', result)
    return result


def command(command, log, timeout=360):
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        text, code = result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        text, code = 'Command timed out; inspect partial evidence.\n', 124
        for part in [error.stdout, error.stderr]:
            if part:
                text += part.decode('utf-8', 'replace') if isinstance(part, bytes) else part
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding='utf-8')
    return code, text


def check():
    require_applied()
    output = EVIDENCE / 'checks' / ('run_' + stamp())
    modules = ['app.m5_check', 'app.m6_check', 'app.m7_check', 'app.m8_check', 'app.m9_check', 'app.m10_check', 'app.m11_check',
               'm9_bundle.tests.test_installer', 'm10_bundle.tests.test_installer', 'm11_bundle.tests.test_installer',
               'm12_bundle.tests.test_m12', 'm12_bundle.tests.test_installer']
    code, text = command([sys.executable, '-m', 'unittest', *modules, '-q'], output / 'unit_tests.txt')
    count = re.search(r'Ran (\d+) tests', text)
    result = {'passed': code == 0, 'exit_code': code, 'unit_tests': int(count[1]) if count else None,
              'external_model_calls': 0, 'source_manifest': source_manifest(), 'output': str(output.relative_to(ROOT))}
    write(output / 'summary.json', result); write(EVIDENCE / 'checks/summary.json', result)
    print(text[-2200:])
    return result


def require_check():
    require_applied()
    path = EVIDENCE / 'checks/summary.json'
    if not path.is_file() or read(path).get('passed') is not True or read(path).get('source_manifest') != source_manifest():
        raise RuntimeError('Run check successfully on the installed source first.')


def verify():
    require_check()
    from m12_bundle.environment import shared_signature
    before = shared_signature()
    output = EVIDENCE / 'database' / ('run_' + stamp())
    runs = []
    for name, path in [('m11_recovery', 'm11_bundle/tests/integration_probe.py'),
                       ('m10_consistency', 'm11_bundle/tests/m10_regression_probe.py'),
                       ('m12_batch_recovery', 'm12_bundle/tests/integration_probe.py')]:
        folder = output / name
        code, _ = command([sys.executable, str(ROOT / path), '--output', str(folder)], folder / 'probe_output.txt', timeout=420)
        item = read(folder / 'summary.json') if (folder / 'summary.json').is_file() else {}
        runs.append({'name': name, 'passed': code == 0 and item.get('passed') is True and item.get('temporary_schema_cleaned') is True,
                     'exit_code': code, 'scenarios': len(item.get('checks', [])), 'result': str((folder / 'summary.json').relative_to(ROOT))})
        print(json.dumps(runs[-1]), flush=True)
    after = shared_signature()
    result = {'passed': all(r['passed'] for r in runs) and before == after, 'runs': runs,
              'shared_rows_unchanged': before == after, 'external_model_calls': 0,
              'source_manifest': source_manifest(), 'output': str(output.relative_to(ROOT))}
    write(output / 'summary.json', result); write(EVIDENCE / 'database/summary.json', result)
    return result


def saved_experiment(name, result, output):
    require_applied()
    result = {**result, 'source_manifest': source_manifest(), 'output': str(output.relative_to(ROOT))}
    write(output / 'summary.json', result); write(EVIDENCE / name / 'summary.json', result)
    return result


def baseline():
    require_check()
    from m12_bundle.compatibility import replay
    from m12_bundle.environment import shared_signature
    before = shared_signature()
    output = EVIDENCE / 'baseline_replay' / ('run_' + stamp())
    result = replay(ROOT, output)
    result['shared_rows_unchanged'] = before == shared_signature()
    result['passed'] = result['passed'] and result['shared_rows_unchanged']
    return saved_experiment('baseline_replay', result, output)


def controlled(n):
    require_check()
    from m12_bundle.experiments import load
    output = EVIDENCE / ('load' if n >= 100 else 'smoke') / ('run_' + stamp())
    result = load(ROOT, output, n)
    return saved_experiment('load' if n >= 100 else 'smoke', result, output)


def real(pairs, budget_yuan, max_calls, reviewed):
    require_check()
    from m12_bundle.budget import configuration, Ledger
    from m12_bundle.experiments import real as measure
    prices = read(BUNDLE / 'prices.json')
    config = configuration(prices)
    write(EVIDENCE / 'real/configuration.json', config)
    if not all([config['key_present'], config['beijing_endpoint_matches'], config['model_price_present'], config['embedding_model_allowed']]):
        raise RuntimeError('Real-run model/Beijing endpoint/price does not match. See real/configuration.json; no charged request was sent.')
    if not reviewed:
        raise RuntimeError('Check the price URL in the guide, then add --prices-reviewed. No charged request was sent.')
    run_id = stamp()
    folder = EVIDENCE / ('real_pilot' if pairs == 1 else 'real') / ('run_' + run_id)
    folder.mkdir(parents=True)
    lock = EVIDENCE / 'real_measurement.lock'
    try:
        with lock.open('x', encoding='utf-8') as stream:
            json.dump({'run_id': run_id, 'pid': os.getpid()}, stream)
    except FileExistsError:
        raise RuntimeError('A real-run lock exists. Do not start a second run; inspect the previous run and budget ledger first.') from None
    try:
        ledger = Ledger(EVIDENCE / 'real_budget_ledger.json', prices, budget_yuan, max_calls, run_id)
        if ledger.blocked:
            raise RuntimeError('A previous real SDK attempt has unknown usage/outcome. Keep the ledger and reconcile it before more paid calls.')
        result = measure(ROOT, folder, pairs, prices, ledger)
        result['configuration'] = config
        return saved_experiment('real_pilot' if pairs == 1 else 'real', result, folder)
    finally:
        lock.unlink(missing_ok=True)


def live(base_url):
    require_check()
    rows = []
    for route in ['/health', '/api/tasks/worker', '/openapi.json', '/', '/upload']:
        with urllib.request.urlopen(base_url.rstrip('/') + route, timeout=15) as response:
            raw = response.read()
            body = json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else None
            ok = response.status == 200
            if route == '/health': ok = ok and body.get('stage') == 'M12'
            if route == '/api/tasks/worker': ok = ok and body.get('online') is True
            if route == '/openapi.json': ok = ok and body['info']['version'] == '0.12.0'
            rows.append({'route': route, 'passed': ok, 'http': response.status, 'bytes': len(raw)})
    result = {'passed': all(r['passed'] for r in rows), 'checks': rows, 'external_model_calls': 0,
              'source_manifest': source_manifest()}
    write(EVIDENCE / 'live/summary.json', result)
    return result


def report():
    require_check()
    from m12_bundle.reporting import build
    return build(ROOT, EVIDENCE, source_manifest())


def export():
    require_check()
    from m12_bundle.reporting import operational
    return operational(ROOT, EVIDENCE, source_manifest())


def finalize(manual, skip_reason):
    require_check()
    analysis = report()
    checks = {}
    for name in ['checks', 'live', 'database', 'baseline_replay', 'load', 'operational', 'analysis']:
        path = EVIDENCE / name / 'summary.json'
        item = read(path) if path.is_file() else {}
        checks[name] = item.get('passed') is True and item.get('source_manifest') == source_manifest()
    load_value = read(EVIDENCE / 'load/summary.json') if (EVIDENCE / 'load/summary.json').is_file() else {}
    checks['load_full_size'] = load_value.get('full_size_complete') is True
    real_path = EVIDENCE / 'real/summary.json'
    real_value = read(real_path) if real_path.is_file() else {}
    real_done = real_value.get('passed') is True and real_value.get('observed_unique_tasks', 0) >= 10 and real_value.get('source_manifest') == source_manifest()
    real_documented = real_done or bool(skip_reason and len(skip_reason.strip()) >= 4)
    technical = all(checks.values()) and real_documented
    result = {'passed': technical and manual, 'technical_gate_passed': technical, 'manual_review_completed': manual,
              'checks': checks, 'real_model_evaluation': 'embedding_index_tasks_measured' if real_done else 'not_completed',
              'real_measurement_completed': real_done, 'full_scope_completed': technical and manual and real_done,
              'completion_scope': 'controlled_and_real_index_measurement' if real_done else 'controlled_only_with_explicit_real_measurement_gap',
              'real_measurement_skip_reason': skip_reason if not real_done else None,
              'source_manifest': source_manifest(), 'previous_evidence_preserved': True,
              'report': str((EVIDENCE / 'analysis/M12_ANALYSIS.md').relative_to(ROOT))}
    write(EVIDENCE / 'summary.json', result)
    text = '# 健康报告 M12 验收\n\n' + '\n'.join(f'- {k}：{v}' for k, v in result.items() if k not in {'source_manifest', 'checks'})
    text += '\n\n## 技术检查\n\n' + '\n'.join(f'- {k}：{v}' for k, v in checks.items())
    text += '\n\n仅使用原始记录里的实际数字。受控替身 Token 不是真实用量；真实任务只覆盖索引确认，不代表问答质量或生产并发容量。\n'
    (EVIDENCE / 'M12_ACCEPTANCE.md').write_text(text, encoding='utf-8')
    return result


def rollback(backup_name):
    backup = target(backup_name)
    if not backup.is_relative_to(ROOT / 'backups'):
        raise RuntimeError('Backup must be under the project backups directory.')
    from app.db import connect_db
    with connect_db() as conn:
        count = conn.execute("SELECT count(*) FROM processing_tasks WHERE status IN ('queued','running')").fetchone()[0]
    if count:
        raise RuntimeError('Active tasks exist; preserve source and finish/reconcile them before a code rollback.')
    state = read(backup / 'rollback.json')
    for entry in state['entries']:
        path = target(entry['path'])
        if not path.is_file() or digest(path) != entry['after_sha256']:
            raise RuntimeError('Rollback stopped because of later edits: ' + entry['path'])
        if entry['existed'] and digest(backup / 'original' / entry['path'], True) != entry['before_binary_sha256']:
            raise RuntimeError('Backup checksum mismatch')
    for entry in reversed(state['entries']):
        path = target(entry['path'])
        if entry['existed']: shutil.copyfile(backup / 'original' / entry['path'], path)
        else: path.unlink()
    return {'passed': True, 'rolled_back': True}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ['preflight', 'apply', 'check', 'verify', 'baseline', 'report', 'export']:
        sub.add_parser(name)
    p = sub.add_parser('load'); p.add_argument('--n', type=int, default=100)
    p = sub.add_parser('real'); p.add_argument('--pairs', type=int, default=5); p.add_argument('--budget-yuan', type=float, default=1)
    p.add_argument('--max-calls', type=int, default=80); p.add_argument('--prices-reviewed', action='store_true')
    p = sub.add_parser('live'); p.add_argument('--base-url', default='http://app:8000')
    p = sub.add_parser('finalize'); p.add_argument('--manual-reviewed', action='store_true'); p.add_argument('--skip-real-reason')
    p = sub.add_parser('rollback'); p.add_argument('--backup', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'preflight': result = preflight(); write(EVIDENCE / 'preflight.json', result)
        elif args.action == 'apply': result = apply()
        elif args.action == 'check': result = check()
        elif args.action == 'verify': result = verify()
        elif args.action == 'baseline': result = baseline()
        elif args.action == 'load':
            if not 1 <= args.n <= 300: raise RuntimeError('--n must be in 1..300')
            result = controlled(args.n)
        elif args.action == 'real':
            if not 1 <= args.pairs <= 10 or not 0 < args.budget_yuan <= 5 or not 1 <= args.max_calls <= 200:
                raise RuntimeError('Real run limits: pairs 1..10, budget 0..5 yuan, max-calls 1..200.')
            result = real(args.pairs, args.budget_yuan, args.max_calls, args.prices_reviewed)
        elif args.action == 'live': result = live(args.base_url)
        elif args.action == 'report': result = report()
        elif args.action == 'export': result = export()
        elif args.action == 'finalize': result = finalize(args.manual_reviewed, args.skip_real_reason)
        else: result = rollback(args.backup)
        visible = {k: v for k, v in result.items() if k not in {'source_manifest', 'groups', 'price_snapshot', 'shared_before', 'shared_after'}}
        if 'usage' in visible:
            visible['usage'] = {k: v for k, v in visible['usage'].items() if k != 'records'}
        if args.action == 'baseline':
            visible.pop('comparison', None)
        print(json.dumps(visible, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get('passed', True) else 1
    except Exception as error:
        value = {'passed': False, 'error_type': type(error).__name__, 'message': str(error)}
        write(EVIDENCE / ('error_' + args.action + '.json'), value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
