from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from http.cookiejar import CookieJar
from threading import Event, Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request

from features.coupons.device import APP_ID, DOMAIN, LID_COOKIE, TOKEN_COOKIE, decode_param
from features.coupons.recharge_device import COOKIE_SECONDS, SERVER, SCOPE, RechargeDeviceError, RechargeDevices
from workspace.http_transport import RequestException, requests


NOW = 2_000_000_000
TOKEN = 'official-recharge-device-fixture'


class Storage:
    def __init__(self):
        self.data = {}
        self.loads = []

    def load_activity_data(self, scope):
        self.loads.append(scope)
        return deepcopy(self.data.get(scope))

    def save_activity_data(self, scope, value):
        self.data[scope] = deepcopy(value)


class DeviceAppIsolationTests(unittest.TestCase):
    def test_payment_application_reuses_only_its_own_account_ticket_across_restart(self):
        storage = Storage()
        devices = RechargeDevices(storage,clock=lambda:NOW)
        order_client = Client({'status':1,'data':'order-app-device-token'})
        devices.prepare('account-a',order_client)
        payment = devices.for_app('official-payment-app')
        payment_client = Client({'status':1,'data':'payment-app-device-token'})
        payment.prepare('account-a',payment_client)
        self.assertIs(payment,devices.for_app('official-payment-app'))
        self.assertEqual(decode_param(payment_client.calls[0][2]['param'])['appKey'],'official-payment-app')
        self.assertEqual(storage.data[SCOPE]['account-a']['token'],'order-app-device-token')
        self.assertEqual(storage.data[payment.scope]['account-a']['token'],'payment-app-device-token')
        restored = RechargeDevices(storage,clock=lambda:NOW+1).for_app('official-payment-app')
        new_client = Client()
        self.assertTrue(restored.prepare('account-a',new_client)['reused'])
        self.assertEqual(new_client.calls,[])
        self.assertEqual(new_client.cookies.get(TOKEN_COOKIE),'payment-app-device-token')
        devices.forget('account-a')
        self.assertNotIn('account-a',storage.data[SCOPE])
        self.assertNotIn('account-a',storage.data[payment.scope])


class Client:
    def __init__(self, *responses):
        self.headers = {'User-Agent': 'old-browser-fixture', 'payPhoneNo': 'PRIVATE_PHONE',
                        'msgId': 'PRIVATE_MESSAGE', 'Cookie': 'PRIVATE_EXPLICIT_COOKIE'}
        self.cookies = requests.Cookies(CookieJar())
        self.cookies.set('JSESSIONID_CMCC', 'PRIVATE_AUTH', domain=DOMAIN, path='/')
        self.responses = list(responses)
        self.calls = []
        self.entered = self.release = None

    def request(self, method, url, **options):
        cookie_request = Request(url)
        self.cookies.jar.add_cookie_header(cookie_request)
        self.calls.append((method, url, dict(self.headers), options, cookie_request.get_header('Cookie')))
        if self.entered:
            self.entered.set()
            if not self.release.wait(3):
                raise AssertionError('test request was not released')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if hasattr(response, 'status_code') else SimpleNamespace(status_code=200, json=lambda: response)


def state(token=TOKEN, *, expires=NOW + 600, account='a'):
    return {'account_key': account, 'provider': SERVER, 'app_id': APP_ID,
            'lid': 'PERSISTED_LID', 'lid_type': 1, 'token': token, 'issued_at': NOW - 600,
            'expires_at': expires}


class RechargeDevicesTests(unittest.TestCase):
    def test_direct_registration_uses_same_client_safe_headers_and_true_python_identity(self):
        client = Client({'status': 1, 'data': TOKEN})
        headers = client.headers
        service = RechargeDevices(clock=lambda: NOW)
        result = service.prepare('a', client)
        self.assertEqual(result, {'prepared': True, 'reused': False, 'expires_at': NOW + COOKIE_SECONDS})
        method, url, sent, options, cookie_header = client.calls[0]
        self.assertEqual((method, url), ('GET', SERVER))
        self.assertEqual(set(sent), {'User-Agent', 'Accept', 'param'})
        self.assertIsNone(cookie_header)
        self.assertFalse(options['allow_redirects'])
        self.assertIn('LingxiWorkspace/', sent['User-Agent'])
        self.assertIn('Python/', sent['User-Agent'])
        self.assertNotIn('Chrome', sent['User-Agent'])
        self.assertEqual(client.headers['User-Agent'], 'old-browser-fixture')
        self.assertIs(client.headers, headers)
        self.assertEqual(client.headers['payPhoneNo'], 'PRIVATE_PHONE')
        self.assertEqual(client.headers['msgId'], 'PRIVATE_MESSAGE')
        self.assertEqual(client.headers['Cookie'], 'PRIVATE_EXPLICIT_COOKIE')
        payload = decode_param(sent['param'])
        self.assertEqual(set(payload), {'lid', 'lidType', 'cache', 'appKey'})
        self.assertRegex(payload['lid'], r'^2000000000000[0-9a-zA-Z]{32}$')
        self.assertEqual(payload['lidType'], '0')
        self.assertEqual((payload['cache'], payload['appKey']), (True, APP_ID))
        self.assertNotIn(TOKEN, repr(result))
        ticket = next(c for c in client.cookies.jar if c.name == TOKEN_COOKIE)
        self.assertEqual((ticket.domain, ticket.path, ticket.secure, ticket.expires),
                         (DOMAIN, '/i', True, NOW + COOKIE_SECONDS))
        self.assertFalse(ticket.domain_specified)
        self.assertFalse(ticket.domain_initial_dot)
        self.assertFalse(ticket.discard)
        self.assertEqual(client.cookies.get('JSESSIONID_CMCC'), 'PRIVATE_AUTH')

    def test_minus_four_corrects_lid_and_sends_only_actual_minimal_environment(self):
        client = Client({'status': -4, 'data': 'CORRECTED_LID'}, {'status': 2, 'data': TOKEN})
        with patch('features.coupons.recharge_device.platform.system', return_value='TestOS'), \
             patch('features.coupons.recharge_device.platform.release', return_value='R1'), \
             patch('features.coupons.recharge_device.platform.python_version', return_value='3.12.10'), \
             patch('features.coupons.recharge_device.VERSION', 'test-version'), \
             patch('features.coupons.recharge_device.os.cpu_count', return_value=7):
            RechargeDevices(clock=lambda: NOW).prepare('a', client)
        first, second = [decode_param(c[2]['param']) for c in client.calls]
        self.assertEqual(set(second), {'lid', 'lidType', 'cache', 'appKey', 'pro', 'ua', 'np', 'hc', 'to'})
        self.assertEqual((second['lid'], second['lidType'], second['pro'], second['np'], second['hc']),
                         ('CORRECTED_LID', 1, -1, 'TestOS', 7))
        self.assertEqual(second['ua'], client.calls[1][2]['User-Agent'])
        self.assertEqual(second['ua'], 'LingxiWorkspace/test-version (Python/3.12.10; TestOS R1)')
        self.assertIs(type(second['to']), int)
        self.assertNotEqual(first['lid'], second['lid'])
        self.assertEqual(client.cookies.get(LID_COOKIE), 'CORRECTED_LID')
        self.assertEqual(len(client.calls), 2)

    def test_minus_six_retains_lid_and_unknown_cpu_is_not_fabricated(self):
        client = Client({'status': -6}, {'status': 2, 'data': TOKEN})
        with patch('features.coupons.recharge_device.os.cpu_count', return_value=None):
            RechargeDevices(clock=lambda: NOW).prepare('a', client)
        first, second = [decode_param(c[2]['param']) for c in client.calls]
        self.assertEqual(first['lid'], second['lid'])
        self.assertEqual(second['lidType'], 1)
        self.assertNotIn('hc', second)

    def test_known_failures_and_unknown_status_stop_without_extra_stages(self):
        for remote, expected_code in [(-800, 'RISK_RATE_LIMITED'), (-3, 'RISK_INITIALIZATION_FAILED'),
                                      (-5, 'RISK_TRANSPORT_REQUIRED'), (0, 'RISK_RESPONSE_INVALID')]:
            client = Client({'status': remote, 'data': 'NOT_A_TOKEN'})
            with self.subTest(remote=remote), self.assertRaises(RechargeDeviceError) as caught:
                RechargeDevices(clock=lambda: NOW).prepare('a', client)
            self.assertEqual((caught.exception.code, caught.exception.remote_status), (expected_code, remote))
            self.assertEqual(len(client.calls), 1)
            self.assertFalse(any(c.name == TOKEN_COOKIE for c in client.cookies.jar))

    def test_second_stage_requires_status_two_and_never_runs_a_third_stage(self):
        for remote in (1, -4, -6, -800, -3, -5):
            client = Client({'status': -6}, {'status': remote, 'data': 'NOT_ACCEPTED'})
            with self.subTest(remote=remote), self.assertRaises(RechargeDeviceError):
                RechargeDevices(clock=lambda: NOW).prepare('a', client)
            self.assertEqual(len(client.calls), 2)
            self.assertFalse(any(c.name == TOKEN_COOKIE for c in client.cookies.jar))

    def test_bad_statuses_and_bad_success_data_never_turn_into_ticket(self):
        responses = [{'status': True, 'data': TOKEN}, {'status': '2', 'data': TOKEN}, []]
        responses += [{'status': 2, 'data': value} for value in
                      (None, {}, [], 3, '', '#encoded-param', 'bad;cookie', 'bad\nvalue', 'bad\x00value', 'unknown', '中文')]
        responses += [{'status': -4, 'data': value} for value in (None, {}, '', 'bad;lid')]
        for response in responses:
            client = Client(response)
            with self.subTest(response=response), self.assertRaises(RechargeDeviceError):
                RechargeDevices(clock=lambda: NOW).prepare('a', client)
            self.assertEqual(len(client.calls), 1)
            self.assertFalse(any(c.name == TOKEN_COOKIE for c in client.cookies.jar))

    def test_headers_restore_after_network_http_and_json_errors(self):
        bad_json = SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError('not JSON')))
        for response, code in [(RequestException('offline'), 'RISK_NETWORK_FAILED'),
                               (SimpleNamespace(status_code=503), 'RISK_HTTP_FAILED'),
                               (bad_json, 'RISK_RESPONSE_INVALID')]:
            client = Client(response)
            original = client.headers
            with self.subTest(code=code), self.assertRaises(RechargeDeviceError) as caught:
                RechargeDevices(clock=lambda: NOW).prepare('a', client)
            self.assertEqual(caught.exception.code, code)
            self.assertIs(client.headers, original)
            self.assertEqual(client.headers['msgId'], 'PRIVATE_MESSAGE')
            self.assertNotIn('param', client.headers)
            self.assertEqual(client.headers['User-Agent'], 'old-browser-fixture')

    def test_restart_and_version_cookie_changes_reuse_record_without_refresh(self):
        storage = Storage()
        storage.data['wallet-device-state'] = {'untouched': True}
        client = Client({'status': 2, 'data': TOKEN})
        RechargeDevices(storage, lambda: NOW).prepare('a', client)
        saved = deepcopy(storage.data[SCOPE]['a'])
        restarted = RechargeDevices(storage, lambda: NOW + 30)
        second = Client()
        second.cookies.set('APP_COOKIE_VERSION', 'new', domain=DOMAIN)
        with patch('features.coupons.recharge_device.VERSION', 'future-version'):
            result = restarted.prepare('a', second)
        self.assertEqual(result, {'prepared': True, 'reused': True, 'expires_at': NOW + COOKIE_SECONDS})
        self.assertEqual(second.calls, [])
        self.assertEqual(second.cookies.get(TOKEN_COOKIE), TOKEN)
        self.assertEqual(storage.data[SCOPE]['a'], saved)
        self.assertEqual(storage.data['wallet-device-state'], {'untouched': True})
        self.assertEqual(set(storage.loads), {SCOPE})

    def test_existing_cookie_has_priority_and_keeps_real_expiry_without_extension(self):
        storage = Storage()
        storage.data[SCOPE] = {'a': state()}
        client = Client()
        client.cookies.set(TOKEN_COOKIE, 'session-ticket', domain=DOMAIN, path='/i', secure=True)
        existing = next(c for c in client.cookies.jar if c.name == TOKEN_COOKIE)
        existing.expires = NOW + 70
        result = RechargeDevices(storage, lambda: NOW).prepare('a', client)
        self.assertEqual(result, {'prepared': True, 'reused': True, 'expires_at': NOW + 70})
        self.assertEqual(client.cookies.get(TOKEN_COOKIE), 'session-ticket')
        self.assertEqual(storage.data[SCOPE]['a']['expires_at'], NOW + 70)
        self.assertIsNone(storage.data[SCOPE]['a']['issued_at'])
        self.assertEqual(client.calls, [])

    def test_unknown_cookie_expiry_is_reused_only_while_present_not_after_restart(self):
        storage = Storage()
        service = RechargeDevices(storage, lambda: NOW)
        client = Client()
        client.cookies.set(TOKEN_COOKIE, TOKEN, domain=DOMAIN, path='/i', secure=True)
        result = service.prepare('a', client)
        self.assertIsNone(result['expires_at'])
        self.assertTrue(service.prepare('a', client)['reused'])
        self.assertEqual(client.calls, [])
        self.assertIsNone(storage.data[SCOPE]['a']['expires_at'])
        fresh = Client({'status': 1, 'data': 'NEW_SERVER_TICKET'})
        self.assertFalse(RechargeDevices(storage, lambda: NOW).prepare('a', fresh)['reused'])
        self.assertEqual(len(fresh.calls), 1)

    def test_cookie_expiry_triggers_one_preparation_using_persisted_lid(self):
        storage = Storage()
        storage.data[SCOPE] = {'a': state(expires=NOW)}
        client = Client({'status': 1, 'data': 'RENEWED_SERVER_TICKET'})
        client.cookies.set(TOKEN_COOKIE, TOKEN, domain=DOMAIN, path='/i', secure=True)
        next(c for c in client.cookies.jar if c.name == TOKEN_COOKIE).expires = NOW
        result = RechargeDevices(storage, lambda: NOW).prepare('a', client)
        self.assertFalse(result['reused'])
        payload = decode_param(client.calls[0][2]['param'])
        self.assertEqual((payload['lid'], payload['lidType']), ('PERSISTED_LID', 1))

    def test_account_provider_and_app_bindings_are_isolated(self):
        for changed in ({'account_key': 'b'}, {'provider': 'https://other.invalid/c1'}, {'app_id': 'another-app'}):
            storage = Storage()
            storage.data[SCOPE] = {'a': {**state(), **changed}, 'b': state(account='b')}
            client = Client({'status': 1, 'data': 'ACCOUNT_A_SERVER_TICKET'})
            with self.subTest(changed=changed):
                result = RechargeDevices(storage, lambda: NOW).prepare('a', client)
                self.assertFalse(result['reused'])
                self.assertEqual(storage.data[SCOPE]['b']['token'], TOKEN)
                self.assertEqual(storage.data[SCOPE]['a']['token'], 'ACCOUNT_A_SERVER_TICKET')
                self.assertNotEqual(decode_param(client.calls[0][2]['param'])['lid'], 'PERSISTED_LID')

    def test_foreign_expired_insecure_and_fallback_cookies_are_not_reused(self):
        for domain, secure, expiry, token in [('other.invalid', True, None, TOKEN),
                (DOMAIN, False, None, TOKEN), (DOMAIN, True, NOW, TOKEN),
                (DOMAIN, True, None, '#sdk-param')]:
            client = Client({'status': 1, 'data': 'ACCEPTED_NEW_TICKET'})
            client.cookies.set(TOKEN_COOKIE, token, domain=domain, path='/i', secure=secure)
            next(c for c in client.cookies.jar if c.name == TOKEN_COOKIE).expires = expiry
            with self.subTest(domain=domain, secure=secure, expiry=expiry, token=token):
                self.assertFalse(RechargeDevices(clock=lambda: NOW).prepare('a', client)['reused'])
                self.assertEqual(len(client.calls), 1)

    def test_corrected_lid_survives_a_failed_registration_without_new_random_identity(self):
        storage = Storage()
        client = Client({'status': -4, 'data': 'CORRECTED_LID'}, {'status': -3})
        service = RechargeDevices(storage, lambda: NOW)
        with self.assertRaises(RechargeDeviceError):
            service.prepare('a', client)
        self.assertEqual(storage.data[SCOPE]['a']['lid'], 'CORRECTED_LID')
        self.assertEqual(storage.data[SCOPE]['a']['token'], '')
        fresh = Client({'status': 1, 'data': TOKEN})
        RechargeDevices(storage, lambda: NOW).prepare('a', fresh)
        payload = decode_param(fresh.calls[0][2]['param'])
        self.assertEqual((payload['lid'], payload['lidType']), ('CORRECTED_LID', 1))

    def test_forget_clears_only_one_account_and_does_not_reuse_its_old_cookie(self):
        storage = Storage()
        storage.data[SCOPE] = {'a': state(), 'b': state(account='b')}
        service = RechargeDevices(storage, lambda: NOW)
        client = Client({'status': 1, 'data': 'AFTER_FORGET_TICKET'})
        service.prepare('a', client)
        service.forget('a')
        self.assertNotIn('a', storage.data[SCOPE])
        self.assertIn('b', storage.data[SCOPE])
        self.assertFalse(service.prepare('a', client)['reused'])
        self.assertEqual(client.cookies.get(TOKEN_COOKIE), 'AFTER_FORGET_TICKET')

    def test_simultaneous_success_and_failure_share_future_without_sharing_clients(self):
        for failure in (False, True):
            waiting = Event()
            count_lock = Lock()
            count = [0]
            class ObservedFuture(Future):
                def result(self, timeout=None):
                    with count_lock:
                        count[0] += 1
                        if count[0] == 3:
                            waiting.set()
                    return super().result(timeout)
            leader = Client({'status': -800} if failure else {'status': 2, 'data': TOKEN})
            leader.entered, leader.release = Event(), Event()
            followers = [Client() for _ in range(3)]
            service = RechargeDevices(clock=lambda: NOW)
            with self.subTest(failure=failure), patch('features.coupons.recharge_device.Future', ObservedFuture), \
                    ThreadPoolExecutor(max_workers=4) as pool:
                tasks = [pool.submit(service.prepare, 'a', leader)]
                self.assertTrue(leader.entered.wait(2))
                tasks += [pool.submit(service.prepare, 'a', c) for c in followers]
                self.assertTrue(waiting.wait(2))
                leader.release.set()
                if failure:
                    errors = []
                    for task in tasks:
                        with self.assertRaises(RechargeDeviceError) as caught:
                            task.result(timeout=2)
                        errors.append(caught.exception)
                    self.assertTrue(all(error is errors[0] for error in errors))
                else:
                    results = [task.result(timeout=2) for task in tasks]
                    self.assertEqual([r['reused'] for r in results], [False, True, True, True])
                    self.assertTrue(all(c.cookies.get(TOKEN_COOKIE) == TOKEN for c in [leader, *followers]))
                self.assertEqual(len(leader.calls), 1)
                self.assertTrue(all(not c.calls for c in followers))

    def test_forget_during_preparation_prevents_late_ticket_commit(self):
        storage = Storage()
        service = RechargeDevices(storage, lambda: NOW)
        client = Client({'status': 2, 'data': TOKEN})
        client.entered, client.release = Event(), Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(service.prepare, 'a', client)
            self.assertTrue(client.entered.wait(2))
            service.forget('a')
            client.release.set()
            with self.assertRaises(RechargeDeviceError) as caught:
                task.result(timeout=2)
        self.assertEqual(caught.exception.code, 'RISK_PREPARATION_CANCELLED')
        self.assertNotIn('a', storage.data[SCOPE])
        self.assertFalse(any(c.name == TOKEN_COOKIE for c in client.cookies.jar))


if __name__ == '__main__':
    unittest.main()
