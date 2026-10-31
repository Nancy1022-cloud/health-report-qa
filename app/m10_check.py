"""M10 业务编排单元检查；MemoryRepository 的结果不冒充 PostgreSQL 事务证据。"""
import asyncio
import copy
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from app.clients.report_processing import prepare_chunks
from app.data_quality import fingerprint, review_issues
from app.domain_errors import BusinessError
from app.repositories.drafts import DraftRepository
from app.schemas.governance import RevisionRequest
from app.schemas.queries import AgentRequest, QueryRequest
from app.services.governance import ConsistentReportService
from app.services.queries import QueryService
from app.upload_models import Review

TEXT = "空腹血糖：5.7 mmol/L；参考范围：3.9-6.1。"
BODY = {"title":"M10合成报告","report_date":"2026-10-30","report_type":"体检报告","raw_text":TEXT,"reviewed":True,
        "metrics":[{"metric_code":"fasting_glucose","metric_name":"空腹血糖","value":"5.7","unit":"mmol/L",
                    "reference_range":"3.9-6.1","source_text":TEXT}]}


class MemoryRepository:
    def __init__(self):
        self.rows, self.sources, self.requests, self.versions = {}, {}, {}, []
        self.lock = threading.RLock()
        self.in_write = False
        self.fail = False

    def require_member(self, member):
        if member not in {"a","b"}:
            raise BusinessError("member_not_found","不存在")

    @contextmanager
    def transaction(self):
        before = copy.deepcopy((self.rows,self.sources,self.requests,self.versions))
        try:
            yield
        except BaseException:
            self.rows,self.sources,self.requests,self.versions = before
            raise

    @contextmanager
    def upload_lock(self,*args):
        with self.lock:
            yield self

    @contextmanager
    def write(self,*args):
        with self.lock,self.transaction():
            self.in_write=True
            try:
                yield self
            finally:
                self.in_write=False

    def source(self,conn,member,sha):
        return self.sources.get((member,sha))

    def register_source(self,conn,draft,file_path):
        key=draft['member_id'],draft['file_sha256']
        if key in self.sources and self.sources[key]['id']!=draft['id']:
            raise BusinessError('duplicate_upload','重复')
        self.sources.setdefault(key,copy.deepcopy(draft))

    def replay(self,conn,member,operation,key,sha):
        if key is None:return None
        result=self.requests.get((member,operation,key))
        if result is None:return None
        if result[0]!=sha:raise BusinessError('idempotency_conflict','冲突')
        return copy.deepcopy(result[1])

    def receipt(self,conn,member,operation,key,sha,result):
        if key is not None:self.requests[(member,operation,key)]=(sha,copy.deepcopy(result))

    def current(self,conn,report,member,required=False):
        row=self.rows.get((report,member))
        if row is None and required:raise BusinessError('report_not_found','不存在')
        return copy.deepcopy(row)

    def find_current(self,report,member,required=False):
        return self.current(self,report,member,required)

    def find_review(self,report,member):
        row=self.find_current(report,member)
        return Review.model_validate({**row['snapshot'],'reviewed':True}) if row else None

    def save_version(self,conn,report,member,review,prepared,path,version,actor,reason,origin):
        self.rows[(report,member)]={'data_version':version,'index_version':version,'index_state':'ready',
                                    'snapshot':review.canonical(),'file_path':path}
        if self.fail:raise RuntimeError('controlled write failure')
        self.versions.append((report,version,review.canonical(),actor,reason))


class ConsistencyChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.repo=MemoryRepository();self.drafts=DraftRepository(lambda:Path(self.temp.name))
        extracted={k:v for k,v in BODY.items() if k!='reviewed'}|{'warnings':[],'model_info':{}}
        self.extract=Mock(return_value=extracted)
        def index(text):
            self.assertFalse(self.repo.in_write)
            return prepare_chunks(text,config=lambda:('controlled',3),embedder=lambda texts:([[1.,0.,0.] for _ in texts],{}))
        self.index=Mock(side_effect=index)
        self.service=ConsistentReportService(self.repo,self.drafts,lambda *_:'.png',self.extract,self.index)
        self.review=Review.model_validate(BODY)

    def upload(self,data=b'fixture',member='a',key=None):
        return self.service.create_draft(member,'sample.png',data,key)

    def confirmed(self):
        draft=self.upload();self.service.confirm(draft['id'],'a',self.review,'confirm-1');return draft['id']

    def request(self,version=1,title='人工修订',**extra):
        return RevisionRequest(expected_version=version,reason='核对原件',review=Review.model_validate(BODY|{'title':title}),**extra)

    def test_same_file_reuses_upload_and_extraction(self):
        a=self.upload(key='u1');b=self.upload(key='u2')
        self.assertEqual(a['id'],b['id']);self.assertTrue(b['upload_reused']);self.extract.assert_called_once()

    def test_upload_key_reuse_with_different_content_rejected(self):
        self.upload(key='u1')
        with self.assertRaises(BusinessError) as ctx:self.upload(b'other',key='u1')
        self.assertEqual(ctx.exception.code,'idempotency_conflict')

    def test_file_dedup_is_scoped_to_member(self):
        self.assertNotEqual(self.upload(member='a')['id'],self.upload(member='b')['id'])

    def test_unknown_member_precedes_extraction(self):
        with self.assertRaises(BusinessError):self.upload(member='missing')
        self.extract.assert_not_called()

    def test_same_confirmation_skips_external_index(self):
        report=self.confirmed();self.index.reset_mock()
        result=self.service.confirm(report,'a',self.review,'confirm-1')
        self.assertTrue(result['replayed']);self.index.assert_not_called();self.assertEqual(len(self.repo.versions),1)

    def test_confirmation_key_payload_conflict_is_explicit(self):
        report=self.confirmed()
        with self.assertRaises(BusinessError) as ctx:self.service.confirm(report,'a',Review.model_validate(BODY|{'title':'改变'}),'confirm-1')
        self.assertEqual(ctx.exception.code,'idempotency_conflict')

    def test_changed_confirmation_requires_revision(self):
        report=self.confirmed()
        with self.assertRaises(BusinessError) as ctx:self.service.confirm(report,'a',Review.model_validate(BODY|{'title':'改变'}))
        self.assertEqual(ctx.exception.code,'already_confirmed_changed')

    def test_revision_increments_version_and_keeps_extraction(self):
        report=self.confirmed();result=self.service.revise(report,'a',self.request(),'r1')
        self.assertEqual(result['report_version'],2)
        self.assertEqual(self.drafts.load(report,'a')[0]['raw_text'],TEXT)
        self.assertEqual(self.repo.versions[0][2]['title'],BODY['title'])

    def test_detail_version_and_content_share_one_snapshot(self):
        report=self.confirmed();first=self.repo.find_current(report,'a')
        second=copy.deepcopy(first);second.update(data_version=2,index_version=2)
        second['snapshot']['metrics'][0]['value']='6.2'
        with patch.object(self.repo,'find_current',side_effect=[first,second]):
            result=self.service.get_draft(report,'a')
        self.assertEqual((result['report_version'],result['metrics'][0]['value']),(1,'5.7'))

    def test_stale_revision_rejected_before_index(self):
        report=self.confirmed();self.service.revise(report,'a',self.request(),'r1');self.index.reset_mock()
        with self.assertRaises(BusinessError) as ctx:self.service.revise(report,'a',self.request(title='再改'),'r2')
        self.assertEqual(ctx.exception.code,'version_conflict');self.index.assert_not_called()

    def test_revision_replay_returns_original_receipt_after_new_version(self):
        report=self.confirmed();req=self.request();self.service.revise(report,'a',req,'r1')
        self.service.revise(report,'a',self.request(2,'第三版'),'r2')
        result=self.service.revise(report,'a',req,'r1')
        self.assertEqual(result['report_version'],2);self.assertTrue(result['replayed'])
        self.assertEqual(self.repo.find_current(report,'a')['data_version'],3)

    def test_no_change_is_noop(self):
        report=self.confirmed();self.index.reset_mock()
        result=self.service.revise(report,'a',self.request(title=BODY['title']),'r0')
        self.assertFalse(result['changed']);self.assertEqual(result['report_version'],1);self.index.assert_not_called()

    def test_explicit_rebuild_creates_processing_version(self):
        report=self.confirmed()
        result=self.service.revise(report,'a',self.request(title=BODY['title'],rebuild_index=True),'rebuild')
        self.assertEqual(result['report_version'],2);self.assertTrue(result['changed'])

    def test_write_error_keeps_previous_projection_and_receipts(self):
        report=self.confirmed();self.repo.fail=True
        with self.assertRaises(RuntimeError):self.service.revise(report,'a',self.request(),'failed')
        self.assertEqual(self.repo.find_current(report,'a')['data_version'],1)
        self.assertNotIn(('a','revise','failed'),self.repo.requests)

    def test_external_failure_never_publishes_new_version(self):
        report=self.confirmed();self.index.side_effect=TimeoutError('controlled')
        with self.assertRaises(TimeoutError):self.service.revise(report,'a',self.request(),'fail')
        self.assertEqual(self.repo.find_current(report,'a')['data_version'],1)

    def test_wrong_member_precedes_index(self):
        report=self.confirmed();self.index.reset_mock()
        with self.assertRaises(BusinessError):self.service.revise(report,'b',self.request(),'r1')
        self.index.assert_not_called()

    def test_bad_unit_leaves_draft_unconfirmed(self):
        draft=self.upload();body=copy.deepcopy(BODY)
        body['raw_text']=body['raw_text'].replace('mmol/L','kg');body['metrics'][0].update(unit='kg',source_text=body['raw_text'])
        with self.assertRaises(BusinessError) as ctx:self.service.confirm(draft['id'],'a',Review.model_validate(body))
        self.assertEqual(ctx.exception.code,'quality_rule_violation');self.assertEqual(self.repo.versions,[])
        self.assertTrue(self.drafts.load(draft['id'],'a')[0]);self.index.assert_not_called()

    def test_outside_reference_range_is_retained(self):
        body=copy.deepcopy(BODY);body['raw_text']=body['raw_text'].replace('5.7','99')
        body['metrics'][0].update(value='99',source_text=body['raw_text'])
        review=Review.model_validate(body);self.assertEqual(review_issues(review),[])
        draft=self.upload();self.service.confirm(draft['id'],'a',review)
        self.assertEqual(self.repo.find_review(draft['id'],'a').metrics[0].value,'99')

    def test_invalid_name_code_pair_is_rejected(self):
        body=copy.deepcopy(BODY);body['metrics'][0]['metric_code']='uric_acid'
        self.assertTrue(any(i['rule']=='metric_name_code' for i in review_issues(Review.model_validate(body))))

    def test_custom_metric_is_warning(self):
        body=copy.deepcopy(BODY);body['metrics'][0].update(metric_code='custom_example',metric_name='自定义')
        self.assertEqual(review_issues(Review.model_validate(body))[0]['severity'],'warning')

    def test_expected_version_is_strict(self):
        for version in [True,'1',0]:
            with self.subTest(version=version),self.assertRaises(ValidationError):self.request(version)

    def test_duplicate_and_invalid_date_still_rejected(self):
        for body in [BODY|{'report_date':'2026-02-30'},BODY|{'metrics':BODY['metrics']*2}]:
            with self.subTest(body=body),self.assertRaises(ValidationError):Review.model_validate(body)

    def test_query_changed_version_is_rejected(self):
        read=Mock(side_effect=[{'r':1},{'r':2}]);run=Mock(return_value={'status':'retrieved'})
        with self.assertRaises(BusinessError) as ctx:QueryService(run,AsyncMock(),read).query(QueryRequest(question='原文',member_id='a'))
        self.assertEqual(ctx.exception.code,'version_conflict')

    def test_agent_changed_version_is_rejected(self):
        read=Mock(side_effect=[{'r':1},{'r':2}]);run=AsyncMock(return_value={'status':'answered'})
        with self.assertRaises(BusinessError):asyncio.run(QueryService(Mock(),run,read).agent_query(AgentRequest(question='原文',member_ids=['a'])))


if __name__=='__main__':unittest.main(verbosity=2)
