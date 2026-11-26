from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from activities.coc_client import CocError
from activities.net_age_surprise_protocol import SurpriseProtocolError
from workspace.http_transport import requests
from features.net_age.service import NetAge,SURPRISE_LOTTERY


class SurpriseDrawTests(unittest.TestCase):
    def setUp(self):
        self.snapshot={'id':'a','phone':'13800138000'}
        self.service=NetAge(lambda _:self.snapshot,lambda:[],None,None,None)
        self.session=SimpleNamespace(headers={'phone':'market-a'},cookies=requests.Cookies())
        self.service._surprise_session=Mock(side_effect=lambda _:nullcontext((self.snapshot,self.session)))
        self.wheel=SimpleNamespace(token='market-a',sign=Mock(return_value={'wasm_params':['rsa','cipher'],'device_token':'device-a'}))
        self.service.surprise_wheel_factory=Mock(return_value=self.wheel)

    def state(self,count,*ids):
        return {'remaining':count,'prizes':[],'record_ids':set(ids)}

    def outcome(self,record,unit=-1,name='未中奖'):
        return {'code':60000,'data':{'activitySales':{'salesUnitId':unit,'showName':name},
            'lotteryRecord':{'id':record,'createTime':'2026-11-21 10:00:00'}}}

    def test_zero_actual_quota_never_prepares_a_device_or_submits_a_draw(self):
        self.service._surprise_snapshot=Mock(return_value=self.state(0))
        self.service._surprise_request=Mock()
        result=self.service.auto_surprise('a')
        self.assertEqual(result['remaining'],0);self.assertEqual(result['draws'],[])
        self.service.surprise_wheel_factory.assert_not_called()
        self.service._surprise_request.assert_not_called()

    def test_every_draw_is_precommitted_then_checked_against_quota_and_official_records(self):
        self.service._surprise_snapshot=Mock(side_effect=[self.state(2),self.state(1,'r1'),self.state(0,'r1','r2')])
        replies=iter([self.outcome('r1'),self.outcome('r2',23,'测试权益券')])
        def draw(session,path,payload,**kwargs):
            self.assertEqual(path,SURPRISE_LOTTERY+'lottery')
            self.assertTrue(kwargs['mutation'])
            self.assertIsNotNone(self.service.surprise_draws['a']['pending'])
            self.assertEqual(payload,{'activityId':16096,'channelNo':'P00000122499','referer':'wljxmh','operDfp':None})
            self.assertEqual(kwargs['headers']['wasmParams'],'["rsa","cipher"]')
            return next(replies)
        self.service._surprise_request=Mock(side_effect=draw)
        with patch('features.net_age.service.time.sleep'):
            result=self.service.auto_surprise('a')
        self.assertEqual(result['remaining'],0)
        self.assertEqual([d['won']for d in result['draws']],[False,True])
        self.assertTrue(all(d['confirmed']for d in result['draws']))
        self.assertEqual(self.service._surprise_request.call_count,2)
        self.assertIsNone(self.service.surprise_draws['a']['pending'])

    def test_transport_uncertainty_is_never_replayed_on_a_new_run(self):
        self.service._surprise_snapshot=Mock(return_value=self.state(1))
        self.service._surprise_request=Mock(side_effect=CocError('中断','SURPRISE_NETWORK',uncertain=True))
        with self.assertRaises(CocError):self.service.auto_surprise('a')
        self.assertIsNotNone(self.service.surprise_draws['a']['pending'])
        with self.assertRaises(CocError)as result:self.service.auto_surprise('a')
        self.assertTrue(result.exception.uncertain)
        self.assertEqual(self.service._surprise_request.call_count,1)

    def test_restart_keeps_the_unknown_draw_guard(self):
        pending={'pending':{'attempt_id':'unknown-a','before_remaining':1,'outcome':None}}
        store=Mock()
        store.load_activity_data.side_effect=lambda scope:{'a':pending}if scope=='net-age-surprise-draws'else{}
        service=NetAge(lambda _:self.snapshot,lambda:[],None,store,None)
        service._surprise_session=Mock(side_effect=lambda _:nullcontext((self.snapshot,self.session)))
        service._surprise_snapshot=Mock(return_value=self.state(1))
        service._surprise_request=Mock()
        with self.assertRaises(CocError)as error:service.auto_surprise('a')
        self.assertTrue(error.exception.uncertain)
        service._surprise_request.assert_not_called()

    def test_known_receipt_waits_for_official_sync_before_another_request(self):
        self.service._surprise_snapshot=Mock(side_effect=[self.state(1),self.state(1),self.state(0,'r1')])
        self.service._surprise_request=Mock(return_value=self.outcome('r1',23,'测试券'))
        with self.assertRaises(CocError)as result:self.service.auto_surprise('a')
        self.assertTrue(result.exception.uncertain)
        resumed=self.service.auto_surprise('a')
        self.assertEqual(resumed['remaining'],0)
        self.assertEqual(resumed['draws'][0]['record_id'],'r1')
        self.assertEqual(self.service._surprise_request.call_count,1)

    def test_rate_limit_and_required_verification_stop_without_retry(self):
        for response,code in [({'code':-105},'SURPRISE_RATE_LIMITED'),
            ({'code':10,'data':{'risk':True,'url':'https://dev.coc.10086.cn/verify'}},'SURPRISE_VERIFICATION_REQUIRED')]:
            with self.subTest(code=code):
                self.service._surprise_snapshot=Mock(return_value=self.state(3))
                self.service._surprise_request=Mock(return_value=response)
                with self.assertRaises(CocError)as error:self.service.auto_surprise('a')
                self.assertEqual(error.exception.code,code)
                self.assertEqual(self.service._surprise_request.call_count,1)
                self.assertIsNone(self.service.surprise_draws['a']['pending'])

    def test_malformed_success_and_no_code_are_uncertain_not_completed(self):
        for response in [self.outcome('r1'),{'code':60000,'data':{}},{'data':None}]:
            with self.subTest(response=response):
                self.service.surprise_draws={}
                self.service._surprise_snapshot=Mock(side_effect=[self.state(1),self.state(0)])
                self.service._surprise_request=Mock(return_value=response)
                with self.assertRaises(CocError)as error:self.service.auto_surprise('a')
                self.assertTrue(error.exception.uncertain)
                self.assertIsNotNone(self.service.surprise_draws['a']['pending'])

    def test_signing_failure_happens_before_any_draw_intent_or_submission(self):
        self.service._surprise_snapshot=Mock(return_value=self.state(1))
        self.wheel.sign.side_effect=SurpriseProtocolError('设备认证未通过','GDT_REJECTED')
        self.service._surprise_request=Mock()
        with self.assertRaises(CocError)as error:self.service.auto_surprise('a')
        self.assertEqual(error.exception.code,'GDT_REJECTED')
        self.assertNotIn('a',self.service.surprise_draws)
        self.service._surprise_request.assert_not_called()

    def test_feature_shutdown_prevents_a_following_round(self):
        self.service._surprise_snapshot=Mock(side_effect=[self.state(2),self.state(1,'r1')])
        self.service._surprise_request=Mock(return_value=self.outcome('r1'))
        with patch('features.net_age.service.feature_running',side_effect=[True,False]):
            with self.assertRaises(CocError)as error:self.service.auto_surprise('a')
        self.assertEqual(error.exception.code,'FEATURE_DISABLED')
        self.assertEqual(self.service._surprise_request.call_count,1)
        self.assertIsNone(self.service.surprise_draws['a']['pending'])

    def test_a_later_failure_keeps_confirmed_wins_for_the_local_status(self):
        self.service._surprise_snapshot=Mock(side_effect=[self.state(2),self.state(1,'r1')])
        self.service._surprise_request=Mock(side_effect=[self.outcome('r1',-2,'测试实物'),{'code':-105}])
        with patch('features.net_age.service.time.sleep'):
            with self.assertRaises(CocError):self.service.auto_surprise('a')
        progress=self.service.surprise_progress('a')
        self.assertEqual(progress['remaining'],1)
        self.assertEqual(len(progress['draws']),1)
        self.assertTrue(progress['draws'][0]['confirmed'])
        self.assertTrue(progress['draws'][0]['needs_address'])
        self.assertEqual(self.service._surprise_request.call_count,2)

    def test_active_sms_operation_is_not_interrupted_by_background_draws(self):
        self.service.busy_login=Mock(return_value=True)
        self.service._surprise_snapshot=Mock()
        with self.assertRaises(CocError)as error:self.service.auto_surprise('a')
        self.assertEqual(error.exception.code,'ACCOUNT_BUSY')
        self.service._surprise_snapshot.assert_not_called()

    def test_physical_prize_result_does_not_submit_an_address(self):
        self.service._surprise_snapshot=Mock(side_effect=[self.state(1),self.state(0,'r1')])
        self.service._surprise_request=Mock(return_value=self.outcome('r1',-2,'测试实物奖品'))
        result=self.service.auto_surprise('a')
        self.assertTrue(result['draws'][0]['needs_address'])
        self.assertEqual(self.service._surprise_request.call_count,1)

    def test_mutation_http_failures_are_classified_as_uncertain(self):
        session=Mock()
        session.post.return_value=Mock(status_code=502)
        with self.assertRaises(CocError)as error:
            NetAge._surprise_request(session,'/test',{},mutation=True)
        self.assertTrue(error.exception.uncertain)


if __name__=='__main__':unittest.main()
