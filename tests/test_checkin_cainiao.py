from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from features.checkin.cainiao import CheckinCainiao, PROFILE, CALLBACK
from features.checkin.beans import AWARD
from features.checkin.client import CheckinError
import test_checkin_beans as bean_fixtures


class CainiaoProtocolTests(unittest.TestCase):
    def setUp(self):
        self.adapter=CheckinCainiao(Mock(),Mock());self.client=Mock()
        self.client.get.return_value=SimpleNamespace(status_code=200)
        @contextmanager
        def session():yield self.client
        self.adapter._session=session
        self.item={'taskCategory':'6','taskType':'1','jumpUrl':'cainiao'}
        self.info={'backUrl':'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301','scanTime':0,'cToken':'task+token='}

    def test_uses_the_real_channel_callback_after_identity_query(self):
        self.adapter._call=Mock(side_effect=[{'result':{'accountType':'taobao'}},{'result':True}])
        before=Mock();result=self.adapter.complete(self.item,self.info,before_submit=before)
        self.assertTrue(result['callback_acknowledged']);before.assert_called_once()
        calls=self.adapter._call.call_args_list
        self.assertEqual([c.args[1] for c in calls],[PROFILE,CALLBACK])
        body=calls[1].args[2];self.assertEqual(body['source'],'cnMobile')
        self.assertEqual(json.loads(body['param'])['token'],'task+token=')
        self.assertTrue(calls[1].kwargs['writing'])

    def test_guest_identity_never_reports_completion(self):
        self.adapter._call=Mock(return_value={'result':{'accountType':'guest'}});before=Mock()
        with self.assertRaises(CheckinError):self.adapter.complete(self.item,self.info,before_submit=before)
        before.assert_not_called();self.client.get.assert_not_called();self.adapter._call.assert_called_once()

    def test_wrong_parent_never_queries_or_submits(self):
        self.info['backUrl']='https://example.com/other';self.adapter._call=Mock()
        with self.assertRaises(CheckinError):self.adapter.complete(self.item,self.info,before_submit=Mock())
        self.adapter._call.assert_not_called()


class CainiaoBeanIntegrationTests(unittest.TestCase):
    def setUp(self):
        bean_fixtures.BeanTaskTests.setUp(self)
        self.client.task.update(taskName='去菜鸟0元领好物',taskCategory='6',taskType='1',jumpUrl='cainiao')
        self.client._current=Mock();self.adapter=Mock();self.manager.cainiao_factory=lambda _:self.adapter

    def test_only_verified_parent_completion_can_be_claimed(self):
        def complete(item,info,*,before_submit):
            before_submit();self.client.status=1
            return {'callback_acknowledged':True}
        self.adapter.complete.side_effect=complete
        self.manager._perform_tasks('a')
        self.assertEqual([p for p,_ in self.client.writes],[AWARD])
        self.assertEqual(self.manager.data['a']['balance'],'3')

    def test_callback_ack_without_parent_completion_is_not_success_or_replayed(self):
        def complete(item,info,*,before_submit):before_submit();return {'callback_acknowledged':True}
        self.adapter.complete.side_effect=complete
        self.manager._perform_tasks('a');self.manager._perform_tasks('a')
        self.assertTrue(self.manager.pending['a']);self.assertFalse(self.client.writes)
        self.adapter.complete.assert_called_once();self.assertEqual(self.manager.data['a']['state'],'failed')


if __name__=='__main__':unittest.main()
