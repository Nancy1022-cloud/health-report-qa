"""离线验证策略、阶段复用、文件完整性和接口契约。数据库并发另测。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.api_errors import install_api_contract
from app.domain_errors import BusinessError
from app.repositories.drafts import DraftRepository
from app.schemas.tasks import TaskResponse
from app.task_files import TaskFiles
from app.task_pipeline import TaskContext
from app.task_policy import LostExecution, TaskFailure, TaskPolicy, backoff, classify


class PolicyChecks(unittest.TestCase):
    def test_invalid_budgets_rejected(self):
        for kwargs in [{'max_attempts':0},{'max_calls':257},{'timeout_seconds':0},{'heartbeat_seconds':10},{'min_call_interval':-1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):TaskPolicy(**kwargs)

    def test_retry_backoff_finite(self):
        self.assertEqual([backoff(i) for i in range(1,7)],[1,2,4,8,8,8])

    def test_network_errors_retryable(self):
        request=httpx.Request('POST','https://controlled.invalid')
        for error in [APITimeoutError(request=request),APIConnectionError(request=request)]:
            self.assertTrue(classify(error).retryable)

    def test_http_retry_whitelist(self):
        for status in [400,401,403,404,408,429,500,502,503,504]:
            request=httpx.Request('POST','https://controlled.invalid')
            error=APIStatusError('controlled',response=httpx.Response(status,request=request),body=None)
            self.assertEqual(classify(error).retryable,status in {408,429,500,502,503,504})

    def test_quota_429_not_blindly_retried(self):
        error=APIStatusError('controlled',response=httpx.Response(429,request=httpx.Request('POST','https://controlled.invalid')),
                             body={'code':'insufficient_quota'})
        self.assertFalse(classify(error).retryable)

    def test_invalid_config_or_business_not_retried(self):
        for error in [HTTPException(503,'missing config'),BusinessError('version_conflict','changed'),ValueError('bad response'),OSError('disk')]:
            self.assertFalse(classify(error).retryable)


class ContextChecks(unittest.TestCase):
    def setUp(self):
        self.repository=MagicMock();self.task={'id':'task_x','execution_token':'token'}
        self.context=TaskContext(self.repository,self.task)

    def test_saved_stage_reused_without_call(self):
        self.repository.artifact.return_value={'data':{'fields':{}},'usage':None}
        function=MagicMock()
        self.assertEqual(self.context.call('fields','LLM',function),({'fields':{}},None))
        function.assert_not_called();self.repository.reserve_call.assert_not_called()

    def test_missing_usage_remains_unknown(self):
        self.repository.artifact.return_value=None;self.repository.reserve_call.return_value={'id':'call','timeout':2}
        self.context.call('fields','LLM',lambda _:({'value':1},None))
        self.assertIsNone(self.repository.finish_call.call_args.kwargs['usage'])

    def test_failed_attempt_recorded_then_retry_classified(self):
        self.repository.artifact.return_value=None;self.repository.reserve_call.return_value={'id':'call','timeout':2}
        def broken(_):raise APITimeoutError(request=httpx.Request('POST','https://controlled.invalid'))
        with self.assertRaises(TaskFailure) as caught:self.context.call('fields','LLM',broken)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.repository.finish_call.call_args.kwargs['error'],'upstream_timeout')

    def test_reservation_budget_stops_before_external(self):
        self.repository.artifact.return_value=None;self.repository.reserve_call.side_effect=TaskFailure('call_budget')
        function=MagicMock()
        with self.assertRaises(TaskFailure):self.context.call('fields','LLM',function)
        function.assert_not_called()

    def test_lost_token_does_not_issue_external(self):
        self.repository.artifact.side_effect=LostExecution()
        function=MagicMock()
        with self.assertRaises(LostExecution):self.context.call('fields','LLM',function)
        function.assert_not_called()

    def test_local_checkpoint_reused(self):
        self.repository.artifact.return_value={'text':'saved'}
        function=MagicMock()
        self.assertEqual(self.context.local('pdf_text',function),{'text':'saved'});function.assert_not_called()

    def test_rate_wait_does_not_count_as_request(self):
        self.repository.artifact.return_value=None
        self.repository.reserve_call.side_effect=[{'wait':.1},{'id':'call','timeout':1}]
        function=MagicMock(return_value=({'x':1},{}))
        with patch('app.task_pipeline.time.sleep'):self.context.call('x','LLM',function)
        function.assert_called_once()


class FileChecks(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.drafts=DraftRepository(lambda:Path(self.folder.name))
        self.files=TaskFiles(self.drafts)
        self.meta=self.files.stage('upload_'+'a'*32,'member','../report.png',b'controlled','.png')

    def test_filename_and_id_stable(self):
        self.assertEqual(self.meta['filename'],'report.png')
        self.assertEqual(self.files.read(self.meta),b'controlled')

    def test_original_tampering_rejected(self):
        (self.drafts.path(self.meta['id'])/'source.png').write_bytes(b'changed')
        with self.assertRaises(BusinessError):self.files.read(self.meta)

    def test_member_tampering_rejected(self):
        with self.assertRaises(BusinessError):self.files.read(self.meta|{'member_id':'other'})

    def test_repeat_publish_same_draft(self):
        extracted={'raw_text':'合成文本','metrics':[]}
        first=self.files.publish(self.meta,extracted)
        self.assertEqual(self.files.publish(self.meta,extracted),first)
        self.assertEqual(len(list(Path(self.folder.name).iterdir())),1)

    def test_changed_draft_cannot_overwrite(self):
        self.files.publish(self.meta,{'raw_text':'first'})
        with self.assertRaises(BusinessError):self.files.publish(self.meta,{'raw_text':'later'})

    def test_path_traversal_rejected(self):
        with self.assertRaises(BusinessError):self.files.stage('../report','member','report.png',b'x','.png')


class ContractChecks(unittest.TestCase):
    def setUp(self):
        from app.task_routes import router
        from app.dependencies import get_task_service
        self.service=MagicMock()
        self.service.repository.get.side_effect=BusinessError('task_not_found','missing')
        app=FastAPI();app.include_router(router);install_api_contract(app,configure=False)
        app.dependency_overrides[get_task_service]=lambda:self.service
        self.client=TestClient(app,raise_server_exceptions=False)

    def test_missing_idempotency_key_is_422(self):
        response=self.client.post('/api/tasks/upload',data={'member_id':'a'},files={'file':('x.png',b'x','image/png')})
        self.assertEqual(response.status_code,422);self.assertEqual(response.json()['code'],'validation_error')
        self.service.upload.assert_not_called()

    def test_member_required_for_task_read(self):
        response=self.client.get('/api/tasks/task_x')
        self.assertEqual(response.status_code,422);self.service.repository.get.assert_not_called()

    def test_wrong_member_404_uses_request_id(self):
        response=self.client.get('/api/tasks/task_x?member_id=wrong')
        self.assertEqual(response.status_code,404)
        self.assertEqual(response.headers['X-Request-ID'],response.json()['request_id'])

    def test_confirmation_still_requires_manual_review(self):
        response=self.client.post('/api/tasks/reports/upload_x/confirm?member_id=a',json={'reviewed':False},headers={'Idempotency-Key':'key'})
        self.assertEqual(response.status_code,422);self.service.confirm.assert_not_called()

    def test_task_state_not_confused_with_report_state(self):
        with self.assertRaises(ValueError):TaskResponse.model_validate({'status':'confirmed'})


class AdapterChecks(unittest.TestCase):
    def test_sdk_retry_disabled_and_timeout_forwarded(self):
        from app.clients.task_processing import ModelAdapter
        client=MagicMock();response=MagicMock();response.choices[0].finish_reason='stop';response.choices[0].message.content='{}';response.usage=None
        client.with_options.return_value.chat.completions.create.return_value=response
        with patch.dict('os.environ',{'LLM_MODEL':'controlled'}),patch('app.clients.task_processing.make_client') as factory:
            factory.return_value.__enter__.return_value=client
            _,usage=ModelAdapter().fields('LLM','controlled',1.5)
        client.with_options.assert_called_once_with(timeout=1.5,max_retries=0)
        self.assertIsNone(usage)


if __name__=='__main__':unittest.main()
