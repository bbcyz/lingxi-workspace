from base64 import b64encode
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from member_benefits_protocol import (
    DFP_STATE_SCOPE, EDGE_UA, GLUE_SHA256, PAGE_URL, WASM_SHA256, WHEEL_MIN_LIFETIME_MS,
    CampusProtocolError, CampusRiskProtocol,
)


def dfp_result():
    return {
        'schema_version': 1, 'ok': True,
        'requests': [{'param_length': 1600}],
        'result': {'gr-risk-p3': 'p' * 40},
    }


def wheel_result(second_size=529):
    values = [b64encode(b'A' * 256).decode(), b64encode(b'B' * second_size).decode()]
    return {
        'schema_version': 1,
        'config': {'user_agent': EDGE_UA, 'page_url': PAGE_URL, 'wheel_min_lifetime_ms': WHEEL_MIN_LIFETIME_MS},
        'assets': {'glue_sha256': GLUE_SHA256, 'wasm_sha256': WASM_SHA256},
        'context': {'local_storage_keys': 15, 'session_storage_keys': 7},
        'module_url_kind': 'file',
        'roto_simple': {'value': values},
        'calls': [{'bodyLen': 9000, 'status': 200, 'responseCode': '0',
                   'responseSuccess': True, 'responseDataLength': 32,
                   'transport': 'curl_cffi:fixture'}],
    }


class MemberBenefitsProtocolTests(unittest.TestCase):
    def protocol(self, directory):
        runtime = CampusRiskProtocol(state_dir=Path(directory))
        runtime._node = Mock(side_effect=[dfp_result(), wheel_result()])
        return runtime

    def test_order_uses_accepted_minimal_template_once(self):
        with TemporaryDirectory() as directory:
            protocol = self.protocol(directory)
            response = Mock(status_code=200, content=b'{"resultCode":-1006}',
                            json=Mock(return_value={'resultCode': -1006}))
            with patch('member_benefits_protocol.requests.post', return_value=response) as send:
                result = protocol.prepare((), 't' * 36, 'ignored', '', order={
                    'skuId': 84095, 'activityId': 12502, 'smsCode': 'cipher',
                    'channelCode': 'P00000011939', 'selfChannelCode': 'P00000011939',
                    'dealType': 0, 'receipt': True, 'faultyReceipt': True, 'referer': 'zeromzone',
                })
            self.assertEqual(result['order']['body']['resultCode'], -1006)
            self.assertEqual(send.call_count, 1)
            kwargs = send.call_args.kwargs
            self.assertEqual(kwargs['headers']['phone'], 't' * 36)
            self.assertEqual(kwargs['headers']['Referer'], PAGE_URL)
            self.assertNotIn('gr-risk-p3', kwargs['headers'])
            self.assertNotIn('Cookie', kwargs['headers'])
            self.assertNotIn('memberId', kwargs['json'])
            self.assertIsNone(kwargs['json']['operDfp'])
            self.assertFalse(kwargs['allow_redirects'])

    def test_empty_http_200_is_preserved_as_success_contract(self):
        with TemporaryDirectory() as directory:
            protocol = self.protocol(directory)
            response = Mock(status_code=200, content=b'')
            with patch('member_benefits_protocol.requests.post', return_value=response):
                result = protocol.prepare((), 't' * 36, '', '', order={'smsCode': 'cipher'})
            self.assertIsNone(result['order']['body'])

    def test_order_timeout_is_uncertain_and_never_retried(self):
        with TemporaryDirectory() as directory:
            protocol = self.protocol(directory)
            with patch('member_benefits_protocol.requests.post',
                       side_effect=requests.Timeout('fixture')) as send:
                with self.assertRaises(CampusProtocolError) as raised:
                    protocol.prepare((), 't' * 36, '', '', order={'smsCode': 'cipher'})
            self.assertTrue(raised.exception.uncertain)
            self.assertEqual(send.call_count, 1)

    def test_wheel_accepts_both_observed_lengths_and_rejects_a_shorter_lifetime(self):
        with TemporaryDirectory() as directory:
            protocol = CampusRiskProtocol(state_dir=Path(directory))
            for second_size in (529, 530):
                protocol._node = Mock(side_effect=[dfp_result(), wheel_result(second_size)])
                result = protocol.prepare((), 't' * 36, '', '')
                self.assertEqual(result['wasm_params'][1], b64encode(b'B' * second_size).decode())
            early = wheel_result()
            early['config']['wheel_min_lifetime_ms'] = WHEEL_MIN_LIFETIME_MS - 1
            protocol._node = Mock(side_effect=[dfp_result(), early])
            with self.assertRaises(CampusProtocolError):
                protocol.prepare((), 't' * 36, '', '')

    def test_order_records_attempt_before_send_and_skips_json_for_http_errors(self):
        with TemporaryDirectory() as directory:
            protocol = self.protocol(directory)
            record = Mock()
            response = Mock(status_code=429, content=b'<html>Too many requests</html>')

            def send_order(*args, **kwargs):
                record.assert_called_once_with()
                return response

            with patch('member_benefits_protocol.requests.post', side_effect=send_order) as send:
                result = protocol.prepare((), 't' * 36, '', '', order={'smsCode': 'cipher'}, before_submit=record)
            self.assertEqual(result['order'], {'status': 429, 'body': None})
            send.assert_called_once()
            response.json.assert_not_called()

            protocol = self.protocol(directory)
            record.side_effect = OSError('fixture storage unavailable')
            with patch('member_benefits_protocol.requests.post') as send:
                with self.assertRaises(OSError):
                    protocol.prepare((), 't' * 36, '', '', order={'smsCode': 'cipher'}, before_submit=record)
            send.assert_not_called()

    def test_json_null_order_body_is_uncertain(self):
        with TemporaryDirectory() as directory:
            protocol = self.protocol(directory)
            response = Mock(status_code=200, content=b'null', json=Mock(return_value=None))
            with patch('member_benefits_protocol.requests.post', return_value=response) as send:
                with self.assertRaises(CampusProtocolError) as error:
                    protocol.prepare((), 't' * 36, '', '', order={'smsCode': 'cipher'})
            self.assertTrue(error.exception.uncertain)
            send.assert_called_once()

    def test_dfp_state_is_saved_through_encrypted_storage_without_plaintext_residue(self):
        class Storage:
            data = {}

            def load_activity_data(self, scope):
                return self.data.get(scope)

            def save_activity_data(self, scope, value):
                self.data[scope] = value

        with TemporaryDirectory() as directory:
            storage = Storage()
            protocol = CampusRiskProtocol(storage, Path(directory))

            def node(script, args, _env, timeout=180):
                if script.name == 'dfp_node.mjs':
                    state = Path(args[args.index('--state-path') + 1])
                    state.write_text('{"cache":"fixture"}', encoding='utf-8')
                    return dfp_result()
                return wheel_result()

            protocol._node = node
            protocol.prepare((), 't' * 36, '', '')
            saved = storage.data[DFP_STATE_SCOPE]
            self.assertEqual(list(saved.values()), [{'cache': 'fixture'}])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_member_runtime_uses_its_official_page_as_referer_and_wheel_location(self):
        for path in ('halfprize?memberId=1447', 'zero?memberId=1'):
            with self.subTest(path=path), TemporaryDirectory() as directory:
                page = 'https://dev.coc.10086.cn/coc3/canvas/rightsmarket-h5-canvas/online/' + path
                protocol = CampusRiskProtocol(state_dir=Path(directory))
                wheel = wheel_result()
                wheel['config']['page_url'] = page
                wheel['roto_simple']['value'][1] = b64encode(b'C' * 490).decode()
                protocol._node = Mock(side_effect=[dfp_result(), wheel])
                response = Mock(status_code=200, content=b'{"resultCode":-1006}',
                                json=Mock(return_value={'resultCode': -1006}))
                with patch('member_benefits_protocol.requests.post', return_value=response) as send:
                    protocol.prepare((), 't' * 36, '', page, order={'smsCode': 'cipher'})
                self.assertEqual(send.call_args.kwargs['headers']['Referer'], page)
                wheel_args = protocol._node.call_args_list[1].args[1]
                self.assertEqual(wheel_args[wheel_args.index('--page-url') + 1], page)


if __name__ == '__main__':
    unittest.main()
