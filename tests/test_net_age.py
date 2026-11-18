from datetime import datetime
import json
import re
import unittest
from unittest.mock import MagicMock, Mock, patch

from activities.coc_client import CocError
from net_age import AI_IDS, BIRTH_IDS, MEMBER_ID, PAGE_ID, NetAge, NetAgeClient, _config
from recharge.cmcc import SHANGHAI


class NetAgeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 18, tzinfo=SHANGHAI)
        self.client = NetAgeClient(lambda: None, None, None)
        self.service = NetAge(lambda _: {}, lambda: [], None, None, None, now=lambda: self.now)

    def test_shared_product_code_does_not_cross_activity(self):
        record = {'activity_id': MEMBER_ID, 'skuCode': '200000043084'}
        rows = self.client.benefits(5, None, False, [record], [])
        self.assertEqual(rows['member']['status'], '已领取')
        self.assertEqual(rows['ai']['description'], '本月已选 0/2 款')

        record['activity_id'] = AI_IDS[1]
        rows = self.client.benefits(5, None, False, [record], [])
        self.assertTrue(rows['member']['available'])
        self.assertEqual(rows['ai']['description'], '本月已选 1/2 款')

    def test_uncertain_claim_requires_official_receipt_to_clear_intent(self):
        offer = self.client.benefits(5, None, False, [], [])['ai']['offers'][0]
        client = Mock(user={'provinceCode': '100'})

        def disconnect(*_, before_submit, **__):
            before_submit()
            raise CocError('连接中断', 'UPSTREAM_NETWORK', uncertain=True)

        client.post.side_effect = disconnect
        client.orders.return_value = []
        result = self.service._submit(client, 'a', 'ai', offer, '2026-11')
        self.assertFalse(result['confirmed'])
        self.assertEqual(client.post.call_count, 1)
        self.assertEqual(self.service.submissions['a']['ai']['choice'], offer['id'])

        record = {'activity_id': offer['activity'], 'prizeId': offer['sku'], 'createTime': '2026-11-18'}
        client.inspect.return_value = {'rows': self.client.benefits(5, None, False, [record], [])}
        client.surprise_chances.return_value = 0
        self.service.status('a', client)
        self.assertNotIn('ai', self.service.submissions['a'])

    def test_sms_requires_same_account_and_official_slider(self):
        client = Mock()
        client.inspect.return_value = {'rows': self.client.benefits(5, None, False, [], [])}
        with patch('net_age.NetAgeClient', return_value=client):
            preparation = self.service.begin('a', 'member', 'qq-music')
        self.assertTrue(preparation['needs_slider'])
        self.assertIn('QQ 音乐', preparation['notice'])
        self.assertEqual(client.post.call_args.args[0], 'activities/growth/verify/status')

        with self.assertRaises(CocError) as wrong_account:
            self.service.verification('b', preparation['operation'], 'sms', {})
        self.assertEqual(wrong_account.exception.code, 'VERIFICATION_MISSING')
        with self.assertRaises(CocError) as unverified:
            self.service.verification('a', preparation['operation'], 'sms', {})
        self.assertEqual(unverified.exception.code, 'SLIDER_REQUIRED')
        self.assertEqual(client.post.call_count, 1)
        self.service.verification('a', preparation['operation'], 'cancel', {})
        client.close.assert_called_once()

    def test_birthday_coupon_uses_sms_with_the_regular_prize_endpoint(self):
        years = re.match(r'(\d+)年', '10年3个月')
        offer = self.client.benefits(5, years, True, [], [])['birthday']['offers'][0]
        client = Mock(user={'provinceCode': '100'})
        client.orders.return_value = []
        self.service._submit(client, 'a', 'birthday', offer, '2026', code='123456')
        path, payload = client.post.call_args.args
        self.assertEqual(path, 'activities/prize/templatePrize')
        self.assertEqual(payload['activityId'], offer['activity'])
        self.assertEqual(client.post.call_args.kwargs['encryption'], 'aurora')
        self.assertEqual(json.loads(payload['jsonExtParam'])['productCode'], '130000006303')

    def test_actual_birthday_gift_details_resolve_a_different_pending_choice(self):
        annual = [{'activity_id': BIRTH_IDS[0], 'skuCode': '444444', 'skuName': '4张*网龄回馈-充值100立减5元',
                   'createTime': '2026-11-18 18:20:10'}]
        client = Mock()
        client.inspect.return_value = {'rows': self.client.benefits(5, None, False, [], annual)}
        client.surprise_chances.return_value = 0
        self.service.mark('a', 'birthday', {'choice': 'cinema', 'period': '2026'})

        row = self.service.status('a', client)['rows']['birthday']

        self.assertEqual(row['status'], '已领取')
        self.assertEqual([offer['name'] for offer in row['offers'] if offer['received']], ['4 张 5 元话费充值满减券（满 100 元可用）'])
        self.assertFalse(row['available'])
        self.assertNotIn('birthday', self.service.submissions['a'])

    def test_birthday_with_no_official_receipt_stays_blocked_for_resubmission(self):
        years = re.match(r'(\d+)年', '13年')
        client = Mock()
        client.inspect.return_value = {'rows': self.client.benefits(5, years, True, [], [])}
        client.surprise_chances.return_value = 0
        self.service.mark('a', 'birthday', {'choice': 'cinema', 'period': '2026'})

        row = self.service.status('a', client)['rows']['birthday']

        self.assertEqual(row['status'], '结果待确认')
        self.assertIn('已查询官方订单', row['description'])
        self.assertFalse(row['available'])
        self.assertIn('birthday', self.service.submissions['a'])

    def test_expired_app_uid_recovery_stops_before_order_or_wheel_requests(self):
        snapshot = {'id':'a', 'phone':'13800000000', 'cookie':'JSESSIONID=test; UID=test'}
        recover = Mock()
        client = NetAgeClient(lambda: snapshot, None, recover)
        http = Mock()
        client.session_factory = lambda: http
        with patch('activities.coc_client.AppLoginSession.from_credentials') as factory:
            flow = factory.return_value
            flow.call.side_effect = [(200, {'retCode':'410000'}, {}), (200, {'retCode':'400007'}, {})]
            with self.assertRaises(CocError) as failure:
                client.connect()

        self.assertEqual(failure.exception.code, 'APP_LOGIN_REQUIRED')
        self.assertEqual(failure.exception.status, 401)
        self.assertEqual(flow.call.call_count, 2)
        recover.assert_called_once_with(snapshot)
        http.post.assert_not_called()

    def test_flow_config_fetch_works_with_session_only_transport(self):
        transport = MagicMock(spec=['Session', 'RequestsError'])
        session = transport.Session.return_value.__enter__.return_value
        session.get.return_value.text = 'var configData = ' + json.dumps({
            'pageId': PAGE_ID, 'configItemList': [{'configContent_27': '931'}]})
        with patch('net_age.requests', transport):
            self.assertEqual(_config(), {'configContent_27': '931'})
        session.get.assert_called_once()

    def test_generic_claim_failure_reconciles_the_official_record(self):
        offer = self.client.benefits(5, None, False, [], [])['flow']['offers'][0]
        client = Mock(user={'provinceCode': '100'})

        def rejected(*_, before_submit, **__):
            before_submit()
            raise CocError('领取失败！（10）', '10')

        client.post.side_effect = rejected
        client.orders.return_value = [{'activity_id': offer['activity'], 'skuCode': offer['sms_product'],
                                       'createTime': '2026-11-18 22:01:03'}]
        result = self.service._submit(client, 'a', 'flow', offer, '2026-11', code='123456')
        self.assertTrue(result['confirmed'])
        self.assertEqual(client.post.call_count, 1)
        self.assertNotIn('flow', self.service.submissions['a'])


if __name__ == '__main__':
    unittest.main()
