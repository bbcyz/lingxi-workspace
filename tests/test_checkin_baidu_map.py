from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from features.checkin.baidu_map import CheckinBaiduMap, sign_callback, SCOPE, HOME, CALLBACK
from features.checkin.beans import _task_support, AWARD
from features.checkin.client import CheckinError
from workspace.accounts.credentials import CredentialCache
from workspace.http_transport import requests
import test_checkin_beans as bean_fixtures


class BaiduMapProtocolTests(unittest.TestCase):
    def setUp(self):
        self.adapter=CheckinBaiduMap(Mock(),Mock())
        self.client=SimpleNamespace(_credential_data={'uid':'own-user','phone_info':{'cuid':'own-device','sv':'22.0.0'}},
                                    get=Mock(return_value=SimpleNamespace(status_code=200)))
        @contextmanager
        def session():yield self.client
        self.adapter._session=session
        self.home={'errno':0,'data':{'is_login':1,'uid':'own-user','token':{'key':'server-key','ctime_str':'server-time'}}}
        self.adapter._request=Mock(side_effect=[deepcopy(self.home),{'result':{'error':0}}])
        self.item={'taskCategory':'6','taskType':'1','jumpUrl':'baidumap'}
        self.info={'backUrl':'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301', 'scanTime':0,'cToken':'current-task-token'}

    def test_signature_matches_independently_run_published_javascript(self):
        values=sign_callback({'app':2,'task_from':'zgyd','user_token':'synthetic+token=',
            'phone_info':'{"cuid":"example","sv":"22.0.0"}','cuid':'example'},
            {'key':'server-key','ctime_str':'test-server-time'},1795620000)
        self.assertEqual(values['sign'],'320942534f93a0e63b327bdb949de469')

    def test_real_login_query_precedes_signed_callback(self):
        before=Mock()
        result=self.adapter.complete(self.item,self.info,before_submit=before)
        self.assertTrue(result['business_callback_verified'])
        before.assert_called_once()
        home,callback=self.adapter._request.call_args_list
        self.assertEqual(home.args[1],HOME);self.assertEqual(callback.args[1],CALLBACK)
        self.assertEqual(callback.args[2]['user_token'],'current-task-token')
        self.assertEqual(callback.args[2]['ctime_str'],'server-time')
        self.assertTrue(callback.kwargs['writing'])

    def test_unrelated_account_never_sends_callback(self):
        self.home['data']['uid']='another-user';self.adapter._request.side_effect=[self.home]
        before=Mock()
        with self.assertRaises(CheckinError) as error:self.adapter.complete(self.item,self.info,before_submit=before)
        self.assertEqual(error.exception.code,'ACCOUNT_MISMATCH');before.assert_not_called()
        self.adapter._request.assert_called_once()

    def test_explicit_logged_out_state_invalidates_only_own_scope(self):
        self.home['data']['is_login']=0;self.adapter._request.side_effect=[self.home]
        before=Mock()
        with self.assertRaises(CheckinError) as error:self.adapter.complete(self.item,self.info,before_submit=before)
        self.assertEqual(error.exception.code,'BAIDU_MAP_LOGIN_REQUIRED')
        before.assert_not_called();self.adapter.cache.invalidate.assert_called_once_with(self.client)

    def test_wrong_parent_activity_stops_before_any_network(self):
        self.info['backUrl']='https://wx.10086.cn/qwhdhub/diy-client/unrelated'
        with self.assertRaises(CheckinError):self.adapter.complete(self.item,self.info,before_submit=Mock())
        self.client.get.assert_not_called();self.adapter._request.assert_not_called()

    def test_lost_callback_response_is_uncertain_and_never_reauthenticated(self):
        adapter=CheckinBaiduMap(Mock(),Mock());client=Mock();client.get.return_value.status_code=503
        with self.assertRaises(CheckinError) as error:adapter._request(client,CALLBACK,{},writing=True)
        self.assertTrue(error.exception.uncertain);client.get.assert_called_once()
        adapter.cache.recover.assert_not_called()


class BaiduCredentialTests(unittest.TestCase):
    def test_account_and_revision_reuse_without_phone_access(self):
        snapshot={'id':'a','phone':'13800138000','credential_revision':1}
        cache=CredentialCache()
        with requests.Session() as client:
            client.cookies.set('BDUSS','own-token',domain='.baidu.com')
            cache.connect(SCOPE,snapshot,client,lambda:{'uid':'own-user','phone_info':{'cuid':'device'}},
                          expiry_cookies=('BDUSS','BDUSS_BFESS'))
        adapter=CheckinBaiduMap(lambda:snapshot,cache)
        snapshot['credential_revision']=9
        with adapter._session() as client:self.assertEqual(client._credential_data['uid'],'own-user')
        snapshot['id']='another'
        with self.assertRaises(CheckinError) as error:
            with adapter._session():pass
        self.assertEqual(error.exception.code,'BAIDU_MAP_LOGIN_REQUIRED')
        self.assertIsNone(cache.load(SCOPE,{**snapshot,'phone':'13900139000'}))


class BaiduBeanIntegrationTests(unittest.TestCase):
    def setUp(self):
        bean_fixtures.BeanTaskTests.setUp(self)

    def test_uses_only_partner_callback_and_parent_award(self):
        self.client.task.update(taskName='去百度地图领现金',taskCategory='6',taskType='1',jumpUrl='baidumap')
        adapter=Mock()
        def complete(item,info,*,before_submit):
            before_submit();self.client.status=1
            return {'business_callback_verified':True}
        adapter.complete.side_effect=complete
        self.client._current=Mock()
        self.manager.baidu_map_factory=lambda _:adapter
        self.manager._perform_tasks('a')
        self.assertEqual([path for path,_ in self.client.writes],[AWARD])
        self.assertEqual(self.manager.data['a']['balance'],'3')
        self.assertTrue(_task_support(self.client.task)[0])
        self.manager._perform_tasks('a');adapter.complete.assert_called_once()


if __name__=='__main__':unittest.main()
