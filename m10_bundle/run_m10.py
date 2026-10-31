"""M10 安装、离线检查、在线契约检查、受控 V0 与验收。所有结果由运行生成。"""
import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
ROOT = Path.cwd().resolve()
EVIDENCE = ROOT / 'app/data/m10/v1'


def stamp():return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
def digest(path):
    text=path.read_text(encoding='utf-8-sig').replace('\r\n','\n')
    return hashlib.sha256(text.encode()).hexdigest()
def binary_digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def target(relative):
    result=(ROOT/relative).resolve()
    if not result.is_relative_to(ROOT) or result==ROOT:raise RuntimeError('非法目标路径')
    return result

def manifest():
    value=read(BUNDLE/'manifest.json')
    for relative,expected in value['payload'].items():
        target(relative)
        path=BUNDLE/'payload'/relative
        if not path.is_file() or digest(path)!=expected:raise RuntimeError('包内容校验失败：'+relative)
        if path.suffix=='.py':compile(path.read_text(encoding='utf-8-sig'),relative,'exec')
    for relative,expected in value['harness'].items():
        path=BUNDLE/relative
        if not path.is_file() or digest(path)!=expected:raise RuntimeError('验收脚本校验失败：'+relative)
    return value

def inspect_source():
    if not (ROOT/'compose.yaml').is_file():raise RuntimeError('请在 health-report-qa 项目根目录运行')
    spec=manifest();before=after=True;differences=[]
    for relative,expected in spec['base_files'].items():
        path=target(relative);actual=digest(path) if path.is_file() else None
        new=spec['payload'].get(relative,expected)
        before &= actual==expected;after &= actual==new
        if actual not in {expected,new}:differences.append(relative)
    for relative,new in spec['payload'].items():
        if relative in spec['base_files']:continue
        path=target(relative);actual=digest(path) if path.is_file() else None
        before &= actual is None;after &= actual==new
        if actual is not None and actual!=new:differences.append(relative)
    state='diverged' if differences else 'applied' if after else 'm9_ready' if before else 'partial'
    comparison=ROOT/'app/data/m9/v1/summary.json'
    m9_passed=comparison.is_file() and read(comparison).get('passed') is True
    return {'state':state,'source_matches':state in {'m9_ready','applied'},'differences':differences,
            'm9_acceptance_passed':m9_passed,'homepage_present':(ROOT/'app/static/index.html').is_file(),
            'base_file_count':len(spec['base_files']),'payload_file_count':len(spec['payload'])}

def require_applied():
    state=inspect_source()
    if state['state']!='applied':raise RuntimeError('当前文件不是完整 M10，请查看 preflight 结果')
    return state

def source_files():
    spec=manifest();paths=set(spec['base_files'])|set(spec['payload'])
    if (ROOT/'app/static/index.html').is_file():paths.add('app/static/index.html')
    return sorted(paths)

def source_manifest():return {p:digest(target(p)) for p in source_files()}

def apply():
    state=inspect_source()
    if state['state']=='applied':return {'already_applied':True}
    if state['state']!='m9_ready':raise RuntimeError('源码有未知差异，停止覆盖；请提供 preflight.json')
    if not state['m9_acceptance_passed']:raise RuntimeError('未找到 M9 passed=true 的 summary.json，先完成 M9 收尾')
    preserved_v0=v0_signature()
    spec=manifest();backup=ROOT/'backups'/('health_m10_'+stamp());entries=[]
    backup.mkdir(parents=True)
    for relative in spec['payload']:
        path=target(relative);existed=path.is_file()
        if existed:
            copy=backup/'original'/relative;copy.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,copy)
        entries.append({'path':relative,'existed':existed,'before_binary_sha256':binary_digest(path) if existed else None,
                        'after_sha256':spec['payload'][relative]})
    write(backup/'rollback.json',{'backup':str(backup.relative_to(ROOT)),'entries':entries})
    installed=[]
    try:
        for entry in entries:
            relative=entry['path'];path=target(relative);path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_name(path.name+'.m10-install-tmp')
            shutil.copyfile(BUNDLE/'payload'/relative,temporary);os.replace(temporary,path);installed.append(entry)
    except Exception:
        for entry in reversed(installed):
            path=target(entry['path'])
            if entry['existed']:shutil.copyfile(backup/'original'/entry['path'],path)
            else:path.unlink(missing_ok=True)
        raise
    write(EVIDENCE/'v0_preservation.json',preserved_v0)
    result={'applied':True,'backup':str(backup.relative_to(ROOT)),'files':len(entries),'installed_at':stamp()}
    write(EVIDENCE/'installation.json',result)
    return result

def rollback(backup_name):
    backup=target(backup_name)
    if not backup.is_relative_to(ROOT/'backups'):raise RuntimeError('备份路径必须在本项目 backups 内')
    state=read(backup/'rollback.json')
    # 所有检查先完成，再开始还原；不覆盖安装之后的用户编辑。
    for entry in state['entries']:
        path=target(entry['path'])
        if not path.is_file() or digest(path)!=entry['after_sha256']:raise RuntimeError('回滚拦截后续编辑：'+entry['path'])
        if entry['existed']:
            original=backup/'original'/entry['path']
            if not original.is_file() or binary_digest(original)!=entry['before_binary_sha256']:raise RuntimeError('备份校验失败')
    for entry in reversed(state['entries']):
        path=target(entry['path'])
        if entry['existed']:shutil.copyfile(backup/'original'/entry['path'],path)
        else:path.unlink()
    result={'rolled_back':True,'backup':backup_name,'at':stamp()};write(EVIDENCE/'rollback_result.json',result);return result

def execute(command,output,timeout=240):
    try:
        result=subprocess.run(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                              encoding='utf-8',errors='replace',timeout=timeout)
        content=result.stdout;code=result.returncode
    except subprocess.TimeoutExpired as exc:
        content=exc.stdout or b''
        if isinstance(content,bytes):content=content.decode('utf-8','replace')
        content+='\nM10 runner timeout\n';code=124
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(content,encoding='utf-8')
    return code,content

def checks():
    require_applied();output=EVIDENCE/'checks'/'runs'/stamp();output.mkdir(parents=True)
    modules=['app.m5_check','app.m6_check','app.m7_check','app.m8_check','app.m9_check','app.m10_check','m9_bundle.tests.test_installer','m10_bundle.tests.test_installer']
    code,text=execute([sys.executable,'-m','unittest',*modules,'-q'],output/'unit_checks.txt')
    count=re.search(r'Ran (\d+) tests',text)
    result={'passed':code==0,'exit_code':code,'unit_tests':int(count[1]) if count else None,
            'modules':modules,'external_model_calls':0,'database_mode':'offline_test_doubles',
            'source_manifest':source_manifest(),'output':str(output.relative_to(ROOT))}
    write(output/'summary.json',result);write(EVIDENCE/'checks/summary.json',result)
    write(EVIDENCE/'source_manifest.json',source_manifest())
    with zipfile.ZipFile(EVIDENCE/'source_snapshot.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for relative in source_files():archive.write(target(relative),relative)
    for relative in [*manifest()['harness'],'manifest.json']:
        destination=EVIDENCE/'harness'/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(BUNDLE/relative,destination)
    print(text[-1500:]);return result

def fetch(url,*,body=None):
    request=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,
                                   headers={'Content-Type':'application/json'} if body is not None else {})
    try:response=urllib.request.urlopen(request,timeout=20)
    except urllib.error.HTTPError as exc:response=exc
    with response:
        raw=response.read();ctype=response.headers.get('Content-Type','')
        payload=json.loads(raw) if 'application/json' in ctype else None
        return {'http':response.status,'content_type':ctype,'request_id':response.headers.get('X-Request-ID'),
                'body':payload,'bytes':len(raw)}


def v0_signature():
    folder=ROOT/'app/data/m9/v1/V0'
    files=['summary.json','source_manifest.json','source_snapshot.zip','task_manifest.json']
    if not all((folder/p).is_file() for p in files) or not read(folder/'summary.json').get('passed'):
        raise RuntimeError('缺少已通过的 M9 V0；先保留 M9 原验收材料。')
    return {p:binary_digest(folder/p) for p in files}


def require_v0_preserved():
    path=EVIDENCE/'v0_preservation.json'
    if not path.is_file() or read(path)!=v0_signature():raise RuntimeError('M9 V0 与安装时不同，停止；不要覆盖优化前记录。')


def backup_record(relative):
    if inspect_source()['state'] not in {'m9_ready','applied'}:raise RuntimeError('先通过 preflight')
    path=target(relative)
    if not path.is_relative_to(ROOT/'backups') or not path.is_file():raise RuntimeError('备份必须是本项目 backups 内的实际文件')
    with path.open('rb') as stream:
        if stream.read(5)!=b'PGDMP':raise RuntimeError('需要 pg_dump -Fc 的原始备份，不能使用 PowerShell 文本重定向创建')
    result={'passed':True,'path':str(path.relative_to(ROOT)),'size_bytes':path.stat().st_size,'sha256':binary_digest(path)}
    write(EVIDENCE/'database_backup.json',result);return result


def require_checks():
    require_applied();require_v0_preserved()
    path=EVIDENCE/'checks/summary.json'
    if not path.is_file() or not read(path).get('passed') or read(path).get('source_manifest')!=source_manifest():
        raise RuntimeError('请先在当前源码上通过 check')


def connection_factory():
    sys.path.insert(0,str(ROOT))
    from app.db import connect_db
    return connect_db


def database_migrated():
    from psycopg import sql
    with connection_factory()() as conn:
        if conn.execute("SELECT to_regclass('engineering_migrations')").fetchone()[0] is None:return False
        return conn.execute("SELECT 1 FROM engineering_migrations WHERE version='health_m10_v1'").fetchone() is not None


def migration_rehearsal():
    require_checks();path=EVIDENCE/'rehearsal/summary.json'
    if path.is_file() and read(path).get('passed'):
        if read(path).get('source_manifest')==source_manifest():return read(path)
        raise RuntimeError('已有不同源码的演练，请先核对，不覆盖原结果')
    sys.path.insert(0,str(BUNDLE))
    from migration_rehearsal import rehearse
    result=rehearse(ROOT,connection_factory());result['source_manifest']=source_manifest()
    write(path,result);return result


def migrate_database():
    require_checks()
    prior=EVIDENCE/'rehearsal/summary.json'
    if not prior.is_file() or not read(prior).get('passed') or read(prior).get('source_manifest')!=source_manifest():raise RuntimeError('先通过当前源码的 rehearse')
    backup=read(EVIDENCE/'database_backup.json')
    if not backup.get('passed') or binary_digest(target(backup['path']))!=backup['sha256']:raise RuntimeError('升级前数据库备份缺失或改变')
    from app.migrate_m10 import migrate
    from migration_rehearsal import signature
    connect=connection_factory()
    with connect() as conn:
        schema=conn.execute('SELECT current_schema()').fetchone()[0];before=signature(conn,schema)
    from app.repositories.drafts import DraftRepository
    result=migrate(connect,DraftRepository())
    with connect() as conn:after=signature(conn,schema)
    result.update(passed=result['passed'] and before==after,original_fields_unchanged=before==after,
                  before=before,after=after,source_manifest=source_manifest(),external_model_calls=0)
    write(EVIDENCE/'migration/summary.json',result);return result


def database_checks():
    require_checks()
    if not database_migrated():raise RuntimeError('先完成 migrate')
    from migration_rehearsal import signature
    connect=connection_factory()
    with connect() as conn:
        schema=conn.execute('SELECT current_schema()').fetchone()[0];before=signature(conn,schema)
    output=EVIDENCE/'database'/('run_'+stamp());output.mkdir(parents=True)
    records=[]
    for name in ['integration_probe','legacy_probe']:
        directory=output/name;directory.mkdir()
        command=[sys.executable,str(BUNDLE/'tests'/f'{name}.py'),'--output',str(directory if name=='integration_probe' else directory/'behavior.json')]
        if name=='legacy_probe':command.extend(['--root',str(ROOT)])
        code,content=execute(command,directory/'probe_output.txt',timeout=300)
        result_path=directory/('summary.json' if name=='integration_probe' else 'behavior.json')
        result=read(result_path) if result_path.is_file() else {'passed':False,'message':'探针未生成结果'}
        records.append({'name':name,'exit_code':code,'passed':code==0 and result.get('passed') is True,
                        'temporary_schema_cleaned':result.get('temporary_schema_cleaned'),
                        'result':str(result_path.relative_to(ROOT)),
                        'scenarios':len(result.get('checks',[]))})
    with connect() as conn:after=signature(conn,schema)
    result={'passed':all(r['passed'] and r['temporary_schema_cleaned'] for r in records) and before==after,
            'runs':records,'scenarios':sum(r['scenarios'] for r in records),'shared_rows_unchanged':before==after,
            'shared_before':before,'shared_after':after,'external_model_calls':0,
            'real_model_evaluation':'not_executed','source_manifest':source_manifest(),'output':str(output.relative_to(ROOT))}
    write(output/'summary.json',result);write(EVIDENCE/'database/summary.json',result);return result


def quality_report():
    require_checks()
    if not database_migrated():raise RuntimeError('先完成 migrate')
    from app.data_audit import audit
    from app.repositories.drafts import DraftRepository
    result=audit(connection_factory(),DraftRepository())
    result['source_manifest']=source_manifest()
    write(EVIDENCE/'quality/summary.json',result);return result


def live(base_url):
    require_checks();rows=[]
    for path in ['/health','/openapi.json','/','/upload','/docs']:
        row=fetch(base_url.rstrip('/')+path);row['path']=path
        row['passed']=row['http']==200 and row['bytes']>0
        if path=='/health':row['passed'] &= row['body'].get('stage')=='M10'
        if path=='/openapi.json':
            from app.main import app
            row['passed'] &= row['body']==app.openapi() and row['body']['info']['version']=='0.10.0'
            write(EVIDENCE/'live/openapi.json',row['body'])
        if path in {'/','/upload','/docs'}:row['passed'] &= 'text/html' in row['content_type']
        rows.append({k:v for k,v in row.items() if k!='body'})
    invalid=fetch(base_url.rstrip('/')+'/api/reports/missing/revisions?member_id=a',body={'expected_version':True})
    invalid['passed']=invalid['http']==422 and invalid['body'].get('code')=='validation_error' and invalid['body'].get('request_id')==invalid['request_id']
    rows.append(invalid)
    result={'passed':all(r['passed'] for r in rows),'checks':rows,'source_manifest':source_manifest(),'external_model_calls':0}
    write(EVIDENCE/'live/summary.json',result);return result


def finalize(manual):
    require_applied();require_v0_preserved();current=source_manifest()
    checks={}
    for name,path in [('offline','checks/summary.json'),('rehearsal','rehearsal/summary.json'),('migration','migration/summary.json'),
                      ('database','database/summary.json'),('live','live/summary.json'),('quality','quality/summary.json')]:
        file=EVIDENCE/path;value=read(file) if file.is_file() else {}
        checks[name]=value.get('passed') is True
        checks[name+'_same_source']=value.get('source_manifest')==current
    checks['v0_preserved']=True
    technical=all(checks.values())
    result={'passed':technical and manual,'technical_gate_passed':technical,'manual_review_completed':manual,
            'checks':checks,'external_model_calls':0,'real_model_evaluation':'not_executed',
            'source_manifest_file':'source_manifest.json',
            'unit_tests':read(EVIDENCE/'checks/summary.json').get('unit_tests') if (EVIDENCE/'checks/summary.json').exists() else None,
            'database_scenarios':read(EVIDENCE/'database/summary.json').get('scenarios') if (EVIDENCE/'database/summary.json').exists() else None}
    write(EVIDENCE/'summary.json',result)
    lines=['# 健康报告 M10 验收','',f'- 技术验收：{technical}',f'- 人工核对：{manual}',f'- 整体完成：{result["passed"]}',
           '- 模型接口：受控替身；真实 PostgreSQL 检查与离线单元检查分别保存。','- M9 V0 原件已保留；本轮不补写模型准确率、生产可用性或性能提升。','',
           '| 检查 | 结果 |','| --- | --- |']+[f'| {k} | {v} |' for k,v in checks.items()]
    (EVIDENCE/'M10_ACCEPTANCE.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result


def main():
    sys.path.insert(0,str(ROOT));sys.path.insert(0,str(BUNDLE))
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    for name in ['preflight','apply','check','rehearse','migrate','verify','quality']:sub.add_parser(name)
    p=sub.add_parser('backup');p.add_argument('--path',required=True)
    p=sub.add_parser('live');p.add_argument('--base-url',default='http://app:8000')
    p=sub.add_parser('finalize');p.add_argument('--manual-reviewed',action='store_true')
    p=sub.add_parser('rollback');p.add_argument('--backup',required=True)
    args=parser.parse_args()
    try:
        if args.command=='preflight':
            result=inspect_source();result['V0_available']=bool(v0_signature())
            write(EVIDENCE/'preflight.json',result);ok=result['source_matches'] and result['m9_acceptance_passed'] and result['homepage_present']
        elif args.command=='backup':result=backup_record(args.path);ok=result['passed']
        elif args.command=='apply':
            initial=v0_signature();result=apply()
            if not (EVIDENCE/'v0_preservation.json').exists():write(EVIDENCE/'v0_preservation.json',initial)
            require_v0_preserved();ok=True
        elif args.command=='check':result=checks();ok=result['passed']
        elif args.command=='rehearse':result=migration_rehearsal();ok=result['passed']
        elif args.command=='migrate':result=migrate_database();ok=result['passed']
        elif args.command=='verify':result=database_checks();ok=result['passed']
        elif args.command=='quality':result=quality_report();ok=result['passed']
        elif args.command=='live':result=live(args.base_url);ok=result['passed']
        elif args.command=='finalize':result=finalize(args.manual_reviewed);ok=result['technical_gate_passed']
        else:
            if database_migrated():raise RuntimeError('数据库已迁移，禁止仅回退代码。请按指南恢复升级前数据库与代码，或向前修复。')
            result=rollback(args.backup);ok=True
        visible={k:v for k,v in result.items() if k not in {'source_manifest','shared_before','shared_after','before','after'}}
        print(json.dumps(visible,ensure_ascii=False,indent=2,default=str));return 0 if ok else 2
    except Exception as exc:
        result={'passed':False,'error_type':type(exc).__name__,'message':str(exc)}
        write(EVIDENCE/(args.command+'_failed.json'),result);print(json.dumps(result,ensure_ascii=False));return 2
if __name__=='__main__':raise SystemExit(main())
