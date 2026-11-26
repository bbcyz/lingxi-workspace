from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from features.checkin.taobao import CheckinTaobao, request_parameters, PROFILE, CALLBACK
from features.checkin.beans import AWARD
from features.checkin.client import CheckinError
from workspace.http_transport import requests
import test_checkin_beans as bean_fixtures


class TaobaoProtocolTests(unittest.TestCase):
    def test_signature_matches_published_sdk_vector(self):
        p=request_parameters(PROFILE,{'subSource':'yidong','text':'中文'},'example-token',1795619000.123)
        self.assertEqual(p['sign'],'b02b0057de364f4ef26540780893fef8')
        self.assertEqual(json.loads(p['data'])['text'],'中文')

    def test_only_read_token_rejection_recovers_once(self):
        cache=Mock();adapter=CheckinTaobao(Mock(),cache)
        with requests.Session() as client:
            def response(value):return SimpleNamespace(status_code=200,json=lambda:value)
            client.get=Mock(side_effect=[response({'ret':['FAIL_SYS_TOKEN_EMPTY::令牌为空'],'data':{}}),
                response({'ret':['SUCCESS::成功'],'data':{'code':'0000'}})])
            self.assertEqual(adapter._call(client,PROFILE,{})['code'],'0000')
            self.assertEqual(client.get.call_count,2)

    def test_rejected_callback_is_not_replayed_for_token_recovery(self):
        adapter=CheckinTaobao(Mock(),Mock())
        with requests.Session() as client:
            client.get=Mock(return_value=SimpleNamespace(status_code=200,
                json=lambda:{'ret':['FAIL_SYS_TOKEN_EXOIRED::令牌过期'],'data':{}}))
            with self.assertRaises(CheckinError) as error:adapter._call(client,CALLBACK,{},writing=True)
            self.assertTrue(error.exception.definitive_rejection)
            client.get.assert_called_once();adapter.cache.recover.assert_not_called()

    def test_account_expiry_is_distinguished_from_verification(self):
        for code,expected,invalidate in [('FAIL_SYS_SESSION_EXPIRED','TAOBAO_LOGIN_REQUIRED',True),
                                          ('FAIL_SYS_USER_VALIDATE','VERIFICATION_REQUIRED',False)]:
            with self.subTest(code=code):
                cache=Mock();adapter=CheckinTaobao(Mock(),cache)
                with requests.Session() as client:
                    client.get=Mock(return_value=SimpleNamespace(status_code=200,json=lambda:{'ret':[code+'::拒绝'],'data':{}}))
                    with self.assertRaises(CheckinError) as error:adapter._call(client,PROFILE,{})
                    self.assertEqual(error.exception.code,expected)
                    self.assertEqual(cache.invalidate.called,invalidate)

    def test_real_profile_and_activity_page_precede_callback(self):
        adapter=CheckinTaobao(Mock(),Mock());client=Mock();client.get.return_value.status_code=200
        @contextmanager
        def session():yield client
        adapter._session=session;adapter._call=Mock(side_effect=[{'code':'0000','data':{}},{'result':True}])
        before=Mock();result=adapter.complete({'taskCategory':'6','taskType':'1','jumpUrl':'yangmao'},
            {'backUrl':'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301','scanTime':0,'cToken':'task+token='},before_submit=before)
        self.assertTrue(result['business_callback_verified']);before.assert_called_once()
        self.assertEqual([c.args[1] for c in adapter._call.call_args_list],[PROFILE,CALLBACK])
        body=adapter._call.call_args.args[2]
        self.assertEqual(body['subSource'],'yidong');self.assertIn('token=task%2Btoken%3D',body['requestUrl'])

    def test_invalid_profile_never_calls_partner_callback(self):
        adapter=CheckinTaobao(Mock(),Mock())
        @contextmanager
        def session():yield Mock()
        adapter._session=session;adapter._call=Mock(return_value={'code':'not-authorized'})
        before=Mock()
        with self.assertRaises(CheckinError):adapter.complete({'taskCategory':'6','taskType':'1','jumpUrl':'yangmao'},
            {'backUrl':'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301','scanTime':0,'cToken':'own-token'},before_submit=before)
        before.assert_not_called();adapter._call.assert_called_once()


class TaobaoBeanIntegrationTests(unittest.TestCase):
    def setUp(self):
        bean_fixtures.BeanTaskTests.setUp(self)
        self.client.task.update(taskName='去薅羊毛免费领话费',taskCategory='6',taskType='1',jumpUrl='yangmao')
        self.client._current=Mock();self.adapter=Mock();self.manager.taobao_factory=lambda _:self.adapter

    def test_only_partner_callback_and_parent_award_are_used(self):
        def complete(item,info,*,before_submit):
            before_submit();self.client.status=1
            return {'business_callback_verified':True}
        self.adapter.complete.side_effect=complete
        self.manager._perform_tasks('a')
        self.assertEqual([p for p,_ in self.client.writes],[AWARD])
        self.manager._perform_tasks('a');self.adapter.complete.assert_called_once()

    def test_explicit_rejection_does_not_leave_an_unknown_submission(self):
        def reject(item,info,*,before_submit):
            before_submit()
            error=CheckinError('登录失效','TAOBAO_LOGIN_REQUIRED',401);error.definitive_rejection=True
            raise error
        self.adapter.complete.side_effect=reject;self.manager._perform_tasks('a')
        self.assertFalse(self.manager.pending['a']);self.assertFalse(self.client.writes)
        self.assertEqual(self.manager.data['a']['state'],'partial')

    def test_unknown_callback_is_durable_and_never_replayed(self):
        def lose(item,info,*,before_submit):
            before_submit();raise CheckinError('响应丢失','RESULT_UNCERTAIN',409,uncertain=True)
        self.adapter.complete.side_effect=lose
        self.manager._perform_tasks('a');self.manager._perform_tasks('a')
        self.assertTrue(self.manager.pending['a']);self.adapter.complete.assert_called_once()
        self.assertFalse(self.client.writes)


if __name__=='__main__':unittest.main()
