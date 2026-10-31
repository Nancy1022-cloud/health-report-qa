"""Replay the preserved M9 source and probe without changing the original V0 evidence."""

import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

from m12_bundle.analysis import write, distribution, csv_file


def replay(root, output, repetitions=3):
    original = root / 'app/data/m9/v1/V0'
    if not (original / 'source_snapshot.zip').is_file() or not (original / 'harness/tests/baseline_probe.py').is_file():
        raise RuntimeError('The preserved M9 V0 source snapshot or probe is missing. Restore the original evidence; do not rebuild V0.')
    rows, runs, failure = [], [], None
    with tempfile.TemporaryDirectory(prefix='health_m12_v0_') as temporary:
        frozen = Path(temporary)
        with zipfile.ZipFile(original / 'source_snapshot.zip') as archive:
            for name in archive.namelist():
                path = (frozen / name).resolve()
                if not path.is_relative_to(frozen):
                    raise RuntimeError('Unsafe V0 archive member')
            archive.extractall(frozen)
        manifest = json.loads((original / 'source_manifest.json').read_text(encoding='utf-8-sig'))
        for name, expected in manifest.items():
            path = frozen / name
            actual = hashlib.sha256(path.read_text(encoding='utf-8-sig').replace('\r\n', '\n').encode()).hexdigest()
            if actual != expected:
                raise RuntimeError('V0 source snapshot does not match its saved manifest: ' + name)
        for i in range(repetitions):
            order = ['V0_replay', 'current'] if i % 2 == 0 else ['current', 'V0_replay']
            for label in order:
                folder = output / f'{i + 1:02d}_{label}'
                folder.mkdir(parents=True)
                probe = original / 'harness/tests/baseline_probe.py' if label == 'V0_replay' else root / 'm12_bundle/tests/legacy_probe.py'
                source = frozen if label == 'V0_replay' else root
                command = [sys.executable, str(probe), '--root', str(source), '--output', str(folder / 'behavior.json')]
                result = subprocess.run(command, cwd=source, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
                (folder / 'probe_output.txt').write_text(result.stdout + result.stderr, encoding='utf-8')
                path = folder / 'behavior.json'
                item = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
                ok = result.returncode == 0 and item.get('passed') is True and item.get('temporary_schema_cleaned') is True and len(item.get('checks', [])) == 26
                runs.append({'version': label, 'run': i + 1, 'passed': ok, 'counts': item.get('counts'),
                             'fixture_sha256': item.get('fixture_sha256'), 'controlled_calls': item.get('controlled_calls')})
                rows.extend({'version': label, 'run': i + 1, **row} for row in item.get('checks', []))
                if not ok:
                    failure = f'{label} run {i + 1} failed; see behavior.json and probe_output.txt'
                    break
            if failure:
                break
    groups = defaultdict(list)
    for row in rows:
        groups[(row['name'], row['version'])].append(row)
    table = []
    for name in sorted({row['name'] for row in rows}):
        old, new = groups.get((name, 'V0_replay'), []), groups.get((name, 'current'), [])
        a, b = distribution(r['client_duration_ms'] for r in old), distribution(r['client_duration_ms'] for r in new)
        table.append({'scenario': name, 'old_n': a['n'], 'current_n': b['n'], 'old_median_ms': a['median'],
                      'current_median_ms': b['median'], 'delta_ms': b['median'] - a['median'] if a['n'] and b['n'] else None,
                      'same_expected_http': bool(old and new and old[0]['expected_http'] == new[0]['expected_http'])})
    fixture_equal = len({r['fixture_sha256'] for r in runs}) == 1
    counts_equal = len({json.dumps(r['counts'], sort_keys=True) for r in runs}) == 1
    calls_equal = len({json.dumps(r['controlled_calls'], sort_keys=True) for r in runs}) == 1
    result = {'passed': failure is None and len(runs) == repetitions * 2 and all(r['passed'] for r in runs)
              and fixture_equal and counts_equal and calls_equal,
              'runs': runs, 'fixture_equal': fixture_equal, 'final_counts_equal': counts_equal,
              'controlled_call_counts_equal': calls_equal, 'failure': failure,
              'comparison': table, 'external_model_calls': 0,
              'scope': 'Fresh same-host replay of the preserved M9 synchronous 26-scenario probe; this measures synchronous reliability overhead, not async completion speed or model quality.',
              'historical_V0_overwritten': False,
              'limits': 'Three repetitions are descriptive only; new M10/M11 fault guarantees are verified separately; response payloads are validated by their scenario assertions, not claimed byte-identical.'}
    write(output / 'observations.json', rows)
    write(output / 'summary.json', result)
    csv_file(output / 'overhead.csv', table, ['scenario', 'old_n', 'current_n', 'old_median_ms', 'current_median_ms', 'delta_ms', 'same_expected_http'])
    return result
