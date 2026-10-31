"""M9 安装、离线检查、在线契约检查、受控 V0 与验收。所有结果由运行生成。"""
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
EVIDENCE = ROOT / 'app/data/m9/v1'


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
    state='diverged' if differences else 'applied' if after else 'm8_ready' if before else 'partial'
    comparison=ROOT/'app/data/m8/v1/comparison.json'
    m8_passed=comparison.is_file() and read(comparison).get('passed') is True
    return {'state':state,'source_matches':state in {'m8_ready','applied'},'differences':differences,
            'm8_comparison_passed':m8_passed,'homepage_present':(ROOT/'app/static/index.html').is_file(),
            'base_file_count':len(spec['base_files']),'payload_file_count':len(spec['payload'])}

def require_applied():
    state=inspect_source()
    if state['state']!='applied':raise RuntimeError('当前文件不是完整 M9，请查看 preflight 结果')
    return state

def source_files():
    spec=manifest();paths=set(spec['base_files'])|set(spec['payload'])
    if (ROOT/'app/static/index.html').is_file():paths.add('app/static/index.html')
    return sorted(paths)

def source_manifest():return {p:digest(target(p)) for p in source_files()}

def apply():
    state=inspect_source()
    if state['state']=='applied':return {'already_applied':True}
    if state['state']!='m8_ready':raise RuntimeError('源码有未知差异，停止覆盖；请提供 preflight.json')
    if not state['m8_comparison_passed']:raise RuntimeError('未找到 M8 passed=true 的 comparison.json，先完成 M8 收尾')
    spec=manifest();backup=ROOT/'backups'/('health_m9_'+stamp());entries=[]
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
            temporary=path.with_name(path.name+'.m9-install-tmp')
            shutil.copyfile(BUNDLE/'payload'/relative,temporary);os.replace(temporary,path);installed.append(entry)
    except Exception:
        for entry in reversed(installed):
            path=target(entry['path'])
            if entry['existed']:shutil.copyfile(backup/'original'/entry['path'],path)
            else:path.unlink(missing_ok=True)
        raise
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
        content+='\nM9 runner timeout\n';code=124
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(content,encoding='utf-8')
    return code,content

def checks():
    require_applied();output=EVIDENCE/'checks'/'runs'/stamp();output.mkdir(parents=True)
    modules=['app.m5_check','app.m6_check','app.m7_check','app.m8_check','app.m9_check','m9_bundle.tests.test_installer']
    code,text=execute([sys.executable,'-m','unittest',*modules,'-q'],output/'unit_checks.txt')
    count=re.search(r'Ran (\d+) tests',text)
    result={'passed':code==0,'exit_code':code,'unit_tests':int(count[1]) if count else None,
            'modules':modules,'external_model_calls':0,'database_mode':'offline_test_doubles',
            'source_manifest':source_manifest(),'output':str(output.relative_to(ROOT))}
    write(output/'summary.json',result);write(EVIDENCE/'checks/summary.json',result)
    print(text[-4500:]);return result

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

def live(base_url):
    require_applied();output=EVIDENCE/'live';rows=[]
    for path in ['/health','/openapi.json','/','/upload','/docs']:
        row=fetch(base_url.rstrip('/')+path);row['path']=path
        row['passed']=row['http']==200 and row['bytes']>0
        if path=='/health':row['passed'] &= row['body'].get('status')=='ok' and row['body'].get('stage')=='M9'
        if path=='/openapi.json':
            spec=row['body'];write(output/'openapi.json',spec)
            row['passed'] &= spec.get('info',{}).get('version')=='0.9.0'
            sys.path.insert(0,str(ROOT));from app.main import app
            row['passed'] &= spec==app.openapi()
        if path in {'/','/upload','/docs'}:row['passed'] &= 'text/html' in row['content_type']
        rows.append({k:v for k,v in row.items() if k!='body'})
    invalid=fetch(base_url.rstrip('/')+'/api/query',body={'question':'','member_id':'a'})
    body=invalid['body'] or {}
    invalid['passed']=(invalid['http']==422 and body.get('code')=='validation_error' and
                       body.get('request_id')==invalid['request_id'])
    invalid['path']='/api/query';rows.append(invalid)
    log_path=ROOT/'app/data/m9/runtime/requests.jsonl'
    samples=[]
    if log_path.is_file():
        with log_path.open(encoding='utf-8') as handle:
            for line in deque(handle,maxlen=1200):
                row=json.loads(line)
                if row.get('request_id')==invalid['request_id']:samples.append(row)
    linked=[r for r in samples if r.get('event')=='request.completed' and r.get('http_status')==422 and r.get('error_code')=='validation_error']
    output.mkdir(parents=True,exist_ok=True)
    (output/'request_log_sample.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in samples),encoding='utf-8')
    result={'passed':all(r['passed'] for r in rows) and len(linked)==1,'checks':rows,'request_id_log_linked':len(linked)==1,
            'external_model_calls':0,'source_manifest':source_manifest()}
    write(output/'summary.json',result);return result

def inventory():
    sys.path.insert(0,str(BUNDLE));sys.path.insert(0,str(ROOT))
    from runtime_inventory import inventory as collect
    return collect(ROOT)

def baseline(repetitions):
    require_applied()
    if not 1<=repetitions<=10:raise RuntimeError('重复次数限定为 1–10；默认 3')
    check_file=EVIDENCE/'checks/summary.json'
    if not check_file.is_file() or not read(check_file).get('passed'):raise RuntimeError('先运行 check 并通过')
    if read(check_file)['source_manifest']!=source_manifest():raise RuntimeError('check 后源码有变化，请重新 check')
    output=EVIDENCE/'V0'
    if (output/'summary.json').is_file() and read(output/'summary.json').get('passed'):
        raise RuntimeError('已有成功 V0，保留原记录；不要覆盖或补造优化前数据')
    if output.exists():output.rename(EVIDENCE/('V0_failed_'+stamp()))
    output.mkdir(parents=True)
    snapshot=source_manifest();write(output/'source_manifest.json',snapshot)
    with zipfile.ZipFile(output/'source_snapshot.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for relative in snapshot:archive.write(target(relative),relative)
    for name in ['run_m9.py','runtime_inventory.py','tests/baseline_probe.py','manifest.json']:
        dest=output/'harness'/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(BUNDLE/name,dest)
    write(output/'harness_manifest.json',{name:binary_digest(output/'harness'/name) for name in
          ['run_m9.py','runtime_inventory.py','tests/baseline_probe.py','manifest.json']})
    started=time.perf_counter();runs=[];observations=[];failure=None
    try:
        before=inventory();write(output/'inventory_before.json',before)
        for i in range(repetitions):
            folder=output/f'run_{i+1:02d}';folder.mkdir()
            code,_=execute([sys.executable,str(BUNDLE/'tests/baseline_probe.py'),'--root',str(ROOT),
                            '--output',str(folder/'behavior.json')],folder/'probe_output.txt')
            path=folder/'behavior.json'
            item=read(path) if path.is_file() else {'passed':False,'error_type':'probe_output_missing'}
            passed=(code==0 and item.get('passed') is True and item.get('temporary_schema_cleaned') is True
                    and len(item.get('checks',[]))==26 and all(r.get('passed') for r in item.get('checks',[]))
                    and item.get('external_model_calls')==0)
            runs.append({'run':i+1,'passed':passed,'exit_code':code,'scenarios':len(item.get('checks',[]))})
            for row in item.get('checks',[]):observations.append({'run':i+1,**row})
            if not passed:raise RuntimeError(f'第 {i+1} 轮失败；查看该轮 behavior.json / probe_output.txt')
        after=inventory();write(output/'inventory_after.json',after)
        if before['row_counts']!=after['row_counts']:raise RuntimeError('业务表计数在采集期间发生变化，停止冻结 V0')
        if snapshot!=source_manifest():raise RuntimeError('采集期间源码发生变化，停止冻结 V0')
    except Exception as exc:failure={'type':type(exc).__name__,'message':str(exc)}
    write(output/'observations.json',observations)
    groups=defaultdict(list)
    for row in observations:groups[row['name']].append(row)
    timing={name:{'n':len(rows),'expected_http':rows[0]['expected_http'],'passed':sum(r['passed'] for r in rows),
            'client_ms':{'median':statistics.median(r['client_duration_ms'] for r in rows),
                         'min':min(r['client_duration_ms'] for r in rows),'max':max(r['client_duration_ms'] for r in rows)},
            'server_ms':{'median':statistics.median(r['server_duration_ms'] for r in rows),
                         'min':min(r['server_duration_ms'] for r in rows),'max':max(r['server_duration_ms'] for r in rows)}}
            for name,rows in groups.items() if all(r.get('server_duration_ms') is not None for r in rows)}
    write(output/'timing_by_scenario.json',timing)
    tasks=[{'name':name,'expected_http':rows[0]['expected_http'],'request':rows[0]['request']} for name,rows in groups.items()]
    write(output/'task_manifest.json',{'version':'m9.v0.fixed26.v1','tasks':tasks,'repetitions':repetitions,
         'concurrency':1,'fixture_path':'run_01/fixture.json','provider':'controlled_test_double',
         'comparison_metrics':['scenario_passed','counts','client_duration_ms','server_duration_ms'],
         'timing_scope':'TestClient 同进程 HTTP 调度 + 同部署 PostgreSQL；无真实模型耗时；不是生产并发测试',
         'usage_scope':'usage 是替身字段，不是真实 Token；真实用量/价格未知，不填 0',
         'future_async_rule':'异步引入后保留 API 接收耗时，另测排队到任务终态的端到端耗时，禁止直接对比立即返回与同步完成'})
    result={'passed':failure is None and len(runs)==repetitions and all(r['passed'] for r in runs),
            'baseline_version':'V0','repetitions':repetitions,'scenarios_per_run':26,'observations':len(observations),
            'passed_observations':sum(r['passed'] for r in observations),'runs':runs,'failure':failure,
            'external_model_calls':0,'real_model_usage':None,'data_source':'fixed_synthetic',
            'database':'real_postgresql_isolated_schema','duration_s':round(time.perf_counter()-started,3),
            'source_manifest':snapshot,'shared_row_counts_unchanged':failure is None}
    write(output/'summary.json',result);return result

def finalize(manual):
    require_applied();state=source_manifest();conditions={}
    for name,path in [('offline_checks','checks/summary.json'),('live_contract','live/summary.json'),('V0','V0/summary.json')]:
        file=EVIDENCE/path
        result=read(file) if file.is_file() else {}
        conditions[name]=result.get('passed') is True
        conditions[name+'_same_source']=result.get('source_manifest')==state
    technical=all(conditions.values());passed=technical and manual
    result={'passed':passed,'technical_gate_passed':technical,'manual_review_completed':manual,
            'checks':conditions,'external_model_calls':0,'real_model_evaluation':'not_executed'}
    write(EVIDENCE/'summary.json',result)
    checks=read(EVIDENCE/'checks/summary.json') if (EVIDENCE/'checks/summary.json').exists() else {}
    v0=read(EVIDENCE/'V0/summary.json') if (EVIDENCE/'V0/summary.json').exists() else {}
    mark='已核对' if manual else '待核对'
    text=f'''# 健康报告 M9 验收\n\n- 技术门槛通过：{technical}\n- 全部完成：{passed}\n- 离线测试：{checks.get('unit_tests','未执行')} 项，实际结果见 checks。\n- V0：{v0.get('passed_observations',0)} / {v0.get('observations',0)} 个场景执行通过。\n- 外部模型调用：0；真实模型用量与效果评测：未执行。\n\n## 自动核对\n\n'''
    text+='\n'.join(f'- {k}: {v}' for k,v in conditions.items())
    text+=f'''\n\n## 人工核对\n\n- 首页、上传页、接口文档能打开：[{mark}]\n- 已核对上传页按 code / message / request_id 展示失败，已理解请求校验与响应违规区别：[{mark}]\n- 已阅读 docs/engineering_m9.md，能用 request_id 找请求日志，理解 V0 口径：[{mark}]\n\n## 当前边界\n\n接口失败与业务空结果已区分；保存受控测量基线。报告修订、上传幂等扩展与任务恢复分别在 M10/M11 验收。\n不把受控 V0 当作真实模型质量、Token 消耗或生产性能结论。\n'''
    (EVIDENCE/'M9_ACCEPTANCE.md').write_text(text,encoding='utf-8')
    return result

def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    for name in ['preflight','apply','check']:sub.add_parser(name)
    p=sub.add_parser('live');p.add_argument('--base-url',default='http://app:8000')
    p=sub.add_parser('baseline');p.add_argument('--repetitions',type=int,default=3)
    p=sub.add_parser('finalize');p.add_argument('--manual-reviewed',action='store_true')
    p=sub.add_parser('rollback');p.add_argument('--backup',required=True)
    args=parser.parse_args()
    try:
        if args.command=='preflight':
            result=inspect_source();write(EVIDENCE/'preflight.json',result)
            ok=result['source_matches'] and result['m8_comparison_passed']
        elif args.command=='apply':result=apply();ok=True
        elif args.command=='check':result=checks();ok=result['passed']
        elif args.command=='live':result=live(args.base_url);ok=result['passed']
        elif args.command=='baseline':result=baseline(args.repetitions);ok=result['passed']
        elif args.command=='finalize':result=finalize(args.manual_reviewed);ok=result['technical_gate_passed']
        else:result=rollback(args.backup);ok=True
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str));return 0 if ok else 2
    except Exception as exc:
        result={'passed':False,'error_type':type(exc).__name__,'message':str(exc)}
        write(EVIDENCE/(args.command+'_failed.json'),result)
        print(json.dumps(result,ensure_ascii=False));return 2
if __name__=='__main__':raise SystemExit(main())
