import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from features.coupons.use import CouponUseManager, CouponUseError
from features.coupons.alipay import CouponAlipayError


class PendingOrderOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.manager = CouponUseManager(lambda _: {'credential_revision': 1})
        self.coupon = {'id': 'a'*24, 'name': '7元话费加赠券'}
        self.key = hashlib.sha256(json.dumps(['original-account', self.coupon['id']], separators=(',', ':')).encode()).hexdigest()
        self.manager.data[self.key] = {'state': 'payment_required', 'order_id': 'fixture-order',
                                       'operation_id': 'old-operation', 'result': {'state': 'payment_required'}}

    def test_legacy_order_attaches_only_to_its_original_account_and_coupon(self):
        self.manager.bind_pending('another-account', [self.coupon])
        self.assertNotIn('login_id', self.manager.data[self.key])
        self.assertEqual(self.manager.pending('another-account'), [])
        self.manager.bind_pending('original-account', [self.coupon])
        order, = self.manager.pending('original-account')
        self.assertEqual(order['order_id'], 'fixture-order')
        self.assertEqual(order['coupon_name'], '7元话费加赠券')
        self.assertIsNone(order['amount'])  # Historical records cannot invent a charge amount.

    def test_unknown_submission_is_not_presented_as_a_payable_order(self):
        self.manager.data[self.key]['state'] = 'unknown'
        self.manager.bind_pending('original-account', [self.coupon])
        self.assertEqual(self.manager.pending('original-account'), [])

    def test_preparing_submitted_coupon_resumes_original_order_without_sso_or_resubmission(self):
        with patch('features.coupons.use.CouponClient') as sso:
            result = self.manager.prepare('original-account', self.coupon['id'])
        self.assertEqual(result['state'], 'payment_required')
        self.assertEqual(result['order']['order_id'], 'fixture-order')
        self.assertEqual(self.manager.data[self.key]['login_id'], 'original-account')
        sso.assert_not_called()

    def test_payment_uses_bound_original_order_and_filters_private_protocol_fields(self):
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.data[self.key].update(charge_amount='30', credit_amount='35.00')
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.return_value = {
                'payment_type': 'alipay_web', 'payment_url': 'https://mclient.alipay.com/cashierRoutePay.htm?awid=fixture',
                'account_masked': '138****0000', 'payload': 'private-signed-order', 'pay_order_id': 'private-reference'}
            result = self.manager.payment('original-account', 'fixture-order')
            gateway.return_value.get_payment.assert_called_once_with(
                'fixture-order', expected_amount='3000', ip_address='192.168.1.2')
            gateway.return_value.query_order.assert_not_called()
        self.assertEqual(result['amount'], '30.00')
        self.assertEqual(result['credit_amount'], '35.00')
        self.assertNotIn('payload', result)
        self.assertNotIn('pay_order_id', result)

    def test_legacy_amount_is_quoted_from_official_order_not_inferred_from_coupon(self):
        self.manager.bind_pending('original-account', [self.coupon])
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.query_order.return_value = {'amount': '5000'}
            gateway.return_value.get_payment.return_value = {'payment_type': 'alipay_web',
                'payment_url': 'https://mclient.alipay.com/cashierRoutePay.htm?awid=fixture', 'account_masked': '138****0000'}
            result = self.manager.payment('original-account', 'fixture-order')
            gateway.return_value.get_payment.assert_called_once_with(
                'fixture-order', expected_amount='5000', ip_address='192.168.1.2')
        self.assertEqual(result['amount'], '50.00')

    def test_order_of_another_account_never_reaches_gateway(self):
        self.manager.bind_pending('original-account', [self.coupon])
        with patch('features.coupons.use.CouponAlipay') as gateway:
            with self.assertRaises(CouponUseError):
                self.manager.payment('another-account', 'fixture-order')
            gateway.assert_not_called()

    def test_reopening_reuses_saved_web_link_and_explicit_refresh_preserves_it_on_failure(self):
        self.manager.bind_pending('original-account', [self.coupon])
        old = {'state': 'payment_required', 'order_id': 'fixture-order', 'amount': '30.00',
               'payment_type': 'alipay_web', 'payment_url': 'https://mclient.alipay.com/cashierRoutePay.htm?awid=original'}
        self.manager.data[self.key].update(charge_amount='30', credit_amount='35.00', payment=old)
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            self.assertEqual(self.manager.payment('original-account', 'fixture-order'), {**old, 'credit_amount': '35.00'})
            gateway.assert_not_called()
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.side_effect = CouponAlipayError('未返回网页', 'ALIPAY_WEB_UNAVAILABLE')
            with self.assertRaises(CouponAlipayError):
                self.manager.payment('original-account', 'fixture-order', refresh=True)
        self.assertEqual(self.manager.data[self.key]['payment'], old)

    def test_confirmed_expiry_closes_only_original_order_and_does_not_create_another(self):
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.data[self.key]['charge_amount'] = '30'
        error = CouponAlipayError('订单已超时', 'ALIPAY_REJECTED', 409)
        error.safe = {'closed': True, 'return_code': 'SP13'}
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.side_effect = error
            result = self.manager.payment('original-account', 'fixture-order')
        self.assertEqual(result['state'], 'closed')
        self.assertEqual(self.manager.data[self.key]['state'], 'closed')
        self.assertEqual(self.manager.pending('original-account'), [])

    def test_old_intermediate_cashier_cache_is_replaced_without_new_business_order(self):
        self.manager.bind_pending('original-account', [self.coupon])
        old = {'state': 'payment_required', 'order_id': 'fixture-order', 'amount': '30.00',
               'payment_type': 'alipay_web', 'payment_url': 'https://cash.open.chinaums.com/h5-cashier/#/old'}
        self.manager.data[self.key].update(charge_amount='30', payment=old)
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route, patch('features.coupons.use.CouponClient') as orders:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.return_value = {'payment_type': 'alipay_web',
                'payment_url': 'https://mclient.alipay.com/cashierRoutePay.htm?awid=new', 'account_masked': '138****0000'}
            result = self.manager.payment('original-account', 'fixture-order')
            gateway.return_value.get_payment.assert_called_once_with('fixture-order', expected_amount='3000', ip_address='192.168.1.2')
            orders.assert_not_called()
        self.assertNotIn('chinaums', result['payment_url'])
        self.assertEqual(self.manager.data[self.key]['payment']['payment_url'], result['payment_url'])

    def test_human_payment_challenge_is_bound_to_order_and_token_is_consumed_once(self):
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.data[self.key]['charge_amount'] = '30'
        error = CouponAlipayError('完成人工验证', 'ALIPAY_VERIFICATION_REQUIRED', 409)
        error.challenge = {'app_id': 'provider-app', 'risk_id': 'provider-risk'}
        error.quote = {'order_id': 'fixture-order', 'amount': '3000', 'gateways': [{'gateway_id': 'ALIPAY'}],
                       'credential_revision': 1}
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.side_effect = error
            result = self.manager.payment('original-account', 'fixture-order')
            self.assertEqual(result['state'], 'verification_required')
            self.assertEqual(result['challenge'], error.challenge)
            self.assertNotIn('quote', result)
            self.manager.payment('original-account', 'fixture-order', refresh=True)
            self.assertEqual(gateway.return_value.get_payment.call_count, 1)
            gateway.return_value.get_payment.side_effect = None
            gateway.return_value.get_payment.return_value = {'payment_type': 'alipay_web',
                'payment_url': 'https://mclient.alipay.com/cashier/mobilepay.htm?awid=fixture', 'account_masked': '138****0000'}
            self.manager.payment('original-account', 'fixture-order', refresh=True, captcha_token='human-token')
            gateway.return_value.get_payment.assert_called_with('fixture-order', expected_amount='3000',
                ip_address='192.168.1.2', verify_key='human-token', verified_quote=error.quote)
            with self.assertRaises(CouponUseError) as used:
                self.manager.payment('original-account', 'fixture-order', refresh=True, captcha_token='human-token')
            self.assertEqual(used.exception.code, 'PAYMENT_CAPTCHA_EXPIRED')

    def test_relogin_discards_old_challenge_and_queries_same_order_with_fresh_credentials(self):
        import time
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.data[self.key]['charge_amount'] = '30'
        self.manager.payment_challenges[('original-account', 'fixture-order')] = {
            'config': {'app_id': 'old-app', 'risk_id': 'old-risk'},
            'quote': {'credential_revision': 0}, 'expires': time.monotonic()+600}
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.return_value = {'payment_type': 'alipay_web',
                'payment_url': 'https://mclient.alipay.com/cashierRoutePay.htm?awid=new',
                'account_masked': '138****0000'}
            result = self.manager.payment('original-account', 'fixture-order', refresh=True)
            gateway.return_value.get_payment.assert_called_once_with('fixture-order',
                expected_amount='3000', ip_address='192.168.1.2')
        self.assertEqual(result['state'], 'payment_required')
        self.assertEqual(self.manager.payment_challenges, {})

    def payment_challenge(self):
        error = CouponAlipayError('需要官方安全验证', 'ALIPAY_VERIFICATION_REQUIRED', 409)
        error.challenge = {'app_id':'payment-app', 'risk_id':'bound-risk-id'}
        error.quote = {'order_id':'fixture-order', 'amount':'3000', 'gateways':[{'gateway_id':'ALIPAY'}],
                       'credential_revision':1}
        return error

    def test_payment_stage_automatically_solves_its_own_challenge_and_resumes_same_order(self):
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.data[self.key]['charge_amount'] = '30'
        solver = Mock()
        solver.solve_payment.return_value = 'official-payment-proof:payment-device'
        self.manager.recharge_captcha = solver
        challenge = self.payment_challenge()
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.side_effect = [challenge, {'payment_type':'alipay_web',
                'payment_url':'https://mclient.alipay.com/cashier/mobilepay.htm?awid=fixture', 'account_masked':'138****0000'}]
            result = self.manager.payment('original-account', 'fixture-order')
            self.assertEqual(gateway.return_value.get_payment.call_count, 2)
            gateway.return_value.get_payment.assert_called_with('fixture-order', expected_amount='3000',
                ip_address='192.168.1.2', verify_key=solver.solve_payment.return_value, verified_quote=challenge.quote)
            gateway.return_value.query_order.assert_not_called()
        solver.solve_payment.assert_called_once_with(login_id='original-account', **challenge.challenge)
        solver.solve.assert_not_called()
        self.assertEqual(result['state'], 'payment_required')
        self.assertEqual(self.manager.data[self.key]['order_id'], 'fixture-order')
        self.assertNotIn(solver.solve_payment.return_value, json.dumps(self.manager.data))
        self.assertFalse(self.manager.payment_running)

    def test_real_gateway_protocol_keeps_quote_and_sends_automatic_token_as_verify_key(self):
        from features.coupons.alipay import CouponAlipay
        from tests.test_coupon_alipay import Session, SNAPSHOT, WEB
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.data[self.key]['charge_amount'] = '30'
        self.manager.payment_credentials = lambda _:dict(SNAPSHOT)
        session = Session()
        session.amount = '3000'
        session.begin.update(returnCode='CV4',riskAppId='payment-app',riskId='actual-order-risk')
        self.manager.recharge_captcha = Mock()
        def solve(**context):
            self.assertEqual(context,{'login_id':'original-account','app_id':'payment-app','risk_id':'actual-order-risk'})
            session.begin['returnCode'] = '0'
            return 'verified-token:payment-c1-device'
        self.manager.recharge_captcha.solve_payment.side_effect = solve
        with patch('features.coupons.use.CouponAlipay',side_effect=lambda provider:CouponAlipay(provider,session_factory=lambda:session)), \
             patch('features.coupons.use.socket.socket') as route, \
             patch('features.coupons.alipay_h5.AlipayH5.payment_url',return_value=WEB):
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2',50000)
            result = self.manager.payment('original-account','fixture-order')
        self.assertEqual([call['fields']['msgType'] for call in session.calls],['QueryPayTypeReq','SDKPayReq','SDKPayReq'])
        self.assertEqual(session.calls[-1]['fields']['verifyKey'],'verified-token:payment-c1-device')
        self.assertEqual(session.calls[-1]['fields']['orderId'],'fixture-order')
        self.assertNotIn('fkToken',session.calls[-1]['fields'])
        self.assertEqual(result['payment_url'],WEB)

    def test_payment_repeated_verification_request_does_not_loop_or_reuse_a_proof(self):
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.data[self.key]['charge_amount'] = '30'
        self.manager.recharge_captcha = Mock()
        self.manager.recharge_captcha.solve_payment.return_value = 'one-use-proof'
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.side_effect = self.payment_challenge()
            result = self.manager.payment('original-account', 'fixture-order')
            self.assertEqual(result['state'], 'verification_required')
            self.manager.payment('original-account', 'fixture-order', refresh=True)
            self.assertEqual(gateway.return_value.get_payment.call_count, 2)
        self.manager.recharge_captcha.solve_payment.assert_called_once()

    def test_automatic_payment_verification_failure_preserves_order_and_reports_its_cause(self):
        from features.coupons.native_captcha import RechargeCaptchaError
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.data[self.key]['charge_amount'] = '30'
        self.manager.recharge_captcha = Mock()
        self.manager.recharge_captcha.solve_payment.side_effect = RechargeCaptchaError('图片无法可靠识别', 'CAPTCHA_IMAGE_AMBIGUOUS')
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.side_effect = self.payment_challenge()
            result = self.manager.payment('original-account', 'fixture-order')
            self.assertEqual(result['message'], '图片无法可靠识别')
            self.manager.payment('original-account', 'fixture-order')
            gateway.return_value.get_payment.assert_called_once()
        self.manager.recharge_captcha.solve_payment.assert_called_once()
        self.assertEqual(self.manager.data[self.key]['state'], 'payment_required')

    def test_concurrent_payment_queries_share_one_verification_and_keep_login_bound(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.data[self.key]['charge_amount'] = '30'
        self.manager.recharge_captcha = Mock()
        entered, release = Event(), Event()
        def solve(**kwargs):
            entered.set()
            if not release.wait(3):raise AssertionError('没有释放测试中的验证请求')
            return 'one-proof'
        self.manager.recharge_captcha.solve_payment.side_effect = solve
        with patch('features.coupons.use.CouponAlipay') as gateway, patch('features.coupons.use.socket.socket') as route:
            route.return_value.__enter__.return_value.getsockname.return_value = ('192.168.1.2', 50000)
            gateway.return_value.get_payment.side_effect = [self.payment_challenge(), {'payment_type':'alipay_web',
                'payment_url':'https://mclient.alipay.com/cashier/mobilepay.htm?awid=fixture', 'account_masked':'138****0000'}]
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(self.manager.payment, 'original-account', 'fixture-order')
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.manager.busy_login('original-account'))
                self.assertFalse(self.manager.busy_login('another-account'))
                second = executor.submit(self.manager.payment, 'original-account', 'fixture-order')
                release.set()
                self.assertEqual(first.result(3), second.result(3))
            self.assertEqual(gateway.return_value.get_payment.call_count, 2)
        self.manager.recharge_captcha.solve_payment.assert_called_once()

    def test_token_from_before_relogin_is_rejected_without_reaching_gateway(self):
        import time
        self.manager.bind_pending('original-account', [self.coupon])
        self.manager.payment_challenges[('original-account', 'fixture-order')] = {
            'config': {}, 'quote': {'credential_revision': 0}, 'expires': time.monotonic()+600}
        with patch('features.coupons.use.CouponAlipay') as gateway:
            with self.assertRaises(CouponUseError) as caught:
                self.manager.payment('original-account', 'fixture-order', captcha_token='old-token')
            self.assertEqual(caught.exception.code, 'PAYMENT_CAPTCHA_EXPIRED')
            gateway.assert_not_called()
