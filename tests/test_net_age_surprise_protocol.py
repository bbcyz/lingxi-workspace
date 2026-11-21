import base64
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import threading
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from activities import net_age_surprise_protocol as protocol


UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0')
PAGE = ('https://dev.coc.10086.cn/coc3/canvas/rightsmarket-h5-canvas/online/zeromzone'
        '?channelCode=P00000011939&pageKey=3&pageRecorded=true')
TOKEN = '00000000-0000-4000-8000-000000000000'
DEVICE = 'device-token-returned-by-official-mock'

# Captured from the original, hash-pinned 0.0.8 SDK with mock gettoken only.
# The fixture contains synthetic account state, a deterministic random pool,
# and ten explicitly controlled SDK Date.now() values. No live HTTP was used.
SDK_HASHES = [
    '34ca932cd9fbc1fbd8aa85c8bf953f4487ec2d43d8dda5f3e672d7f848c036bd',
    '1785a1a5ca3a69a46c0fb79a54c503a418e3b7539705636a521a051c8070b354',
    '41179aecf6faab32894693770022709f3237322ab6bd8a638929047c7c9fdf5c',
    '131ff21025c2a03466366e65d197bcf3603e51b85336799b254155802d63f005',
    'e55491e3f6f06e7ccef411086d8a513f60223c9cbf7d8b1822a9c5d1793242ae',
    'aa2093b85f4faca162e8e9ba45575c7371f55fca8b17724ee40ea20a8cd06e65',
    '2587adbde4ede16f51d7e903e84b921087c491bf1a84fee0a26643c310d87f43',
    'dbddb913d4598f9e91ca18a636ae530d9143105488394a25b71b3b99251ae026',
    '2c627d8c8c53fb91e5323ca670534244558db3fbc96dd504eb4fc25785f95b23',
    'e14d9ed1d9a0903ad400b16dce687989f62c670a5f8e034ff4bef8314916f2d4',
]
SDK_CIPHER = (
    'zs/Q0dLT1NXW19jZ4ec+cX/cGa7hO6z9fZZA0pJ0LviZKf2fa3b0XA0gWWL3xnswiOTE5W2LCcPU9iCU'
    '+dkG33yTVcGIYwq+7hVvit6hVFHG8fKwTlnFRo+hHFyKVKmQkCp9zqW6666bLD6S6pKdX0+L36PWc1rr8'
    'GbRmsKkMUuJE552ZP5sDS+/ZPX/2ba0atmiRhw5zIT9tyfjeHV6nX2UsXWbzpp8sL0ZF972RrAepC00koSiJ'
    'OdNfvie5m5CyTINwZTbKixl/GnrcUcjP1zLzU8TevVMa6Ub/EWRmPiYzLwHmyL2BLIduc0sQCRK5wfT+FY'
    'QaebhZX8EPKXKdsZG6LKkpW+W370lJowULDWZejCzhfSxMiZyNr6Nl5beESzUKN+UzXa28yqnTbvlHttEUVh'
    'mOh301BUcy3J39tbPjxtJE/JyDAQafGq1urk6PmqBVEWKPqNvW858yin3ftkmAG4bY50jtmJqf/44O9K17SWPq'
    'f8uiLUIRZXf9vuBQ4dU+iZ/igBH6nSiPWXiGeDQJad37mCKQkM4T/Wij+SiPSvKO15E0H1Gxepc5Ao7XmgIXY'
    'O9ImHaPhdcsK3qet+CfqkaoGaJt6BEhlMg39NU1OulshDxCaeuvA3m6R19Uob4QepBJP1Zki/StCjqZ4zU1wBy'
    'ur+eTNxALsNAtesmj0ksHtpgfIYaQNKrqrMNH1N6R3lzPm8r8UENg9xZYCTxvIbCdM/Axo3ZAu0Ja8V+RXWq'
    'OVAUAJVC89hC65YY8g/F2+E2'
)


class MemoryStorage:
    def __init__(self):
        self.data = {}

    def load_activity_data(self, scope):
        return deepcopy(self.data.get(scope))

    def save_activity_data(self, scope, data):
        self.data[scope] = deepcopy(data)


class SurpriseProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        protocol._states.clear()
        self.patch_key = patch.object(protocol, '_public_key', self.private_key.public_key())
        self.patch_key.start()
        self.addCleanup(self.patch_key.stop)

    def decode(self, pair):
        key = self.private_key.decrypt(base64.b64decode(pair[0]), padding.PKCS1v15())
        self.assertEqual(len(key), 32)
        cipher = base64.b64decode(pair[1])
        return json.loads(AESGCM(key).decrypt(cipher[:12], cipher[12:], None))

    def test_original_sdk_vectors_all_ten_timestamp_endings(self):
        payload = {'unicode': '中国', 'array': [True, 3, 1.25], 'empty': None}
        for offset, expected in enumerate(SDK_HASHES):
            stamp = 1795259680260 + offset
            environment = {'deviceToken': '0' * 32, 'platform': 'Win32',
                           'time_elapsed': 532 + offset, 'url': PAGE, 'user_agent': UA}
            pair = protocol.encrypt_frame(payload, environment, stamp,
                                          key=bytes(range(174, 206)), nonce=bytes(range(206, 218)))
            frame = self.decode(pair)
            self.assertEqual(frame['verification'], expected)
            self.assertEqual(frame['data'], payload)
            self.assertEqual(frame['environment'], environment)
            if offset == 0:
                self.assertEqual(pair[1], SDK_CIPHER)

    def test_public_key_is_new_wheel_key(self):
        key = serialization.load_pem_public_key(protocol.PUBLIC_KEY)
        encoded = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),
                         '9b7e0f992a3af19ee3fe74f51734c2b4cb63fc0b9a8aa5a92ca43f3d95cd4179')

    def test_full_gettoken_context_and_minimal_sign_semantics(self):
        now = [1795259680.0]
        wheel = protocol.SurpriseWheel(TOKEN, UA, PAGE, clock=lambda: now[0], cookies=[
            {'name': 'visible', 'value': 'current-value'},
            {'name': 'secret-session', 'value': 'http-only-value', 'httpOnly': True},
        ])
        requests = []

        def transport(body, headers):
            requests.append((body, headers))
            full = self.decode([headers['coc-aurora'], body['encryptedText']])
            self.assertEqual(set(body), {'benefit_phone', 'encryptedText', 'timestamp'})
            self.assertEqual(body['benefit_phone'], TOKEN)
            self.assertEqual(full['data'], {'activityId': '109230918290', 'deviceToken': ''})
            environment = full['environment']
            self.assertEqual(len(environment), 50)
            self.assertEqual(environment['local_storage'], {'benefit_phone': TOKEN})
            self.assertEqual(environment['cookie'], {'visible': 'current-value'})
            self.assertEqual(environment['device_token'], [])
            self.assertEqual(environment['page_url'], PAGE)
            self.assertEqual(environment['user_agent'], UA)
            self.assertEqual(environment['init_stack'], [])
            self.assertEqual(environment['last_call_stack'], [])
            self.assertEqual(environment['wasm_ver'], '0.0.8')
            self.assertEqual(environment['collection_time'], full['timestamp'])
            now[0] += 0.5
            return {'code': '0', 'success': True, 'data': DEVICE}

        payload = {'deviceToken': 'business-value', 'choice': 3}
        result = wheel.sign(payload, transport=transport)
        simple = self.decode(result['wasm_params'])
        self.assertEqual(simple['data'], payload)
        self.assertEqual(set(simple['environment']), {'deviceToken', 'platform', 'time_elapsed', 'url', 'user_agent'})
        self.assertEqual(simple['environment']['deviceToken'], DEVICE)
        self.assertEqual(simple['environment']['time_elapsed'], 500)
        self.assertEqual(result['device_token'], DEVICE)
        wheel.sign(transport=transport)
        self.assertEqual(len(requests), 1)

    def test_persisted_token_survives_restart_and_has_no_guessed_expiry(self):
        storage = MemoryStorage()
        first = protocol.SurpriseWheel(TOKEN, UA, PAGE, clock=lambda: 1000, storage=storage)
        fetch = Mock(return_value={'code': '0', 'data': DEVICE})
        first.sign(transport=fetch)
        protocol._states.clear()  # Simulate losing all process-local state.
        restarted = protocol.SurpriseWheel(TOKEN, UA, PAGE, clock=lambda: 1000 + 100 * 86400, storage=storage)
        restarted.sign(transport=fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(restarted._state.history, [DEVICE])
        for token, agent, page in [(TOKEN + '-other', UA, PAGE), (TOKEN, UA + ' changed', PAGE),
                                    (TOKEN, UA, PAGE + '&differentPage=1')]:
            protocol.SurpriseWheel(token, agent, page, storage=storage).sign(transport=fetch)
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(len(storage.data), 4)

    def test_concurrent_contexts_share_one_token_acquisition(self):
        wheels = [protocol.SurpriseWheel(TOKEN, UA, PAGE) for _ in range(6)]
        barrier = threading.Barrier(6)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def transport(_body, _headers):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(5))
            return {'code': '0', 'data': DEVICE}

        def work(wheel):
            barrier.wait(5)
            return wheel.sign(transport=transport)['device_token']

        with ThreadPoolExecutor(max_workers=6) as executor:
            jobs = [executor.submit(work, wheel) for wheel in wheels]
            self.assertTrue(entered.wait(5))
            release.set()
            self.assertEqual([job.result(5) for job in jobs], [DEVICE] * 6)
        self.assertEqual(len(calls), 1)

    def test_device_failure_is_not_retried_or_replaced_by_an_empty_token(self):
        for response in [{'code': '-800', 'message': '请求频繁'}, {'code': '0', 'data': ''},
                         {'code': '0', 'data': 123}, {'code': '0', 'data': DEVICE, 'success': False}, None]:
            protocol._states.clear()
            wheel = protocol.SurpriseWheel(TOKEN, UA, PAGE)
            fetch = Mock(return_value=response)
            with self.assertRaises(protocol.SurpriseProtocolError):
                wheel.sign(transport=fetch)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(wheel._state.token, '')
            self.assertEqual(wheel._state.history, [])
        protocol._states.clear()
        wheel = protocol.SurpriseWheel(TOKEN, UA, PAGE)
        fetch = Mock(side_effect=ConnectionError('offline failure'))
        with self.assertRaises(ConnectionError):
            wheel.sign(transport=fetch)
        self.assertEqual(fetch.call_count, 1)

    def test_concurrent_failure_is_shared_without_a_second_gettoken(self):
        barrier = threading.Barrier(6)
        waiters_ready = threading.Event()
        count_lock = threading.Lock()
        waiter_count = [0]
        wheels = [protocol.SurpriseWheel(TOKEN, UA, PAGE) for _ in range(6)]

        class ObservedFuture(Future):
            def result(self, timeout=None):
                with count_lock:
                    waiter_count[0] += 1
                    if waiter_count[0] == 5:
                        waiters_ready.set()
                return super().result(timeout)

        def reject(_body, _headers):
            self.assertTrue(waiters_ready.wait(5))
            return {'code': '-800', 'message': '请求频繁'}

        fetch = Mock(side_effect=reject)

        def work(wheel):
            barrier.wait(5)
            with self.assertRaises(protocol.SurpriseProtocolError):
                wheel.sign(transport=fetch)

        with patch.object(protocol, 'Future', ObservedFuture), ThreadPoolExecutor(max_workers=6) as executor:
            jobs = [executor.submit(work, wheel) for wheel in wheels]
            for job in jobs:
                job.result(5)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(waiter_count[0], 5)

    def test_explicit_invalidation_only_clears_the_bound_context(self):
        storage = MemoryStorage()
        wheel = protocol.SurpriseWheel(TOKEN, UA, PAGE, storage=storage)
        other = protocol.SurpriseWheel(TOKEN + '-other', UA, PAGE, storage=storage)
        fetch = Mock(return_value={'code': '0', 'data': DEVICE})
        wheel.sign(transport=fetch)
        other.sign(transport=fetch)
        wheel.invalidate()
        self.assertEqual(storage.data[wheel.scope], {'device_token': '', 'history': []})
        self.assertEqual(storage.data[other.scope]['device_token'], DEVICE)
        wheel.sign(transport=fetch)
        other.sign(transport=fetch)
        self.assertEqual(fetch.call_count, 3)

    def test_mobile_context_does_not_copy_desktop_graphics_or_stacks(self):
        wheel = protocol.SurpriseWheel(TOKEN, 'Mozilla/5.0 (Linux; Android 16) leadeon/12.5.6', PAGE)
        environment = wheel._environment(wheel.started)
        self.assertEqual(len(environment), 50)
        self.assertEqual(environment['platform'], 'Linux armv8l')
        self.assertEqual(environment['canvas_fingerprint'], '')
        self.assertEqual(environment['webgl'], {})
        self.assertEqual(environment['plugins'], [])
        self.assertEqual(environment['init_stack'], [])


if __name__ == '__main__':
    unittest.main()
