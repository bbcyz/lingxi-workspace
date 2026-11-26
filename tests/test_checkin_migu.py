import base64
from contextlib import contextmanager
import copy
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from features.checkin.client import CheckinError
from features.checkin.migu import CheckinMigu, encode_request, decode_response, decode_mobile, SCOPE, RESPONSE_KEY
from workspace.accounts.credentials import CredentialCache
from workspace.http_transport import requests


PHONE = '13800138000'


def identity():
    return {'resultCode':'LOGIN_SUCCESS','sign':'server-auth-sign','userInfo':{
        'mobile':PHONE,'userId':'12345','userToken':'opaque-migu-token',
        'expiredOn':int((time.time()+3600)*1000),'areaId':'851','cityId':'851',
        'carrierCode':'CMCC','encrypted':True}}


class MiguCryptoTests(unittest.TestCase):
    def test_request_matches_published_sdk_vectors(self):
        # Independently obtained from the public mgEd.js GatewayCrypto implementation.
        for payload,encoded,signature in [
            ({'cToken':'synthetic-token'},'sIONXOnEIVY66AkLNhMg7MZ3fc8LJi/m5sR1GIVhW0A=','AC977DA9BFA726A42CDF700FBF315C03'),
            ({'value':'中文'},'2oqExUSjOs6K609AKQ9pIh+L0kRcl1GzZ2SuVkf7qC4=','5972A97A41A3D1AAC89CD7CA7D559E1D'),
        ]:
            body,headers=encode_request(payload,'1795560000123')
            self.assertEqual(body,encoded)
            self.assertEqual(headers['ensign'],signature)

    def test_encrypted_empty_callback_body_is_not_assumed_to_be_json(self):
        pad=PKCS7(128).padder();plain=pad.update(b'')+pad.finalize()
        cipher=Cipher(algorithms.AES(RESPONSE_KEY),modes.ECB()).encryptor()
        value=base64.b64encode(cipher.update(plain)+cipher.finalize()).decode()
        self.assertIsNone(decode_response(SimpleNamespace(text=value,headers={'x-amz-meta-re':'1'})))

    def test_official_mobile_encoding_is_reversed_before_identity_comparison(self):
        encoded=base64.b64encode((PHONE+'+').encode()).decode()
        encoded=(encoded+encoded[1]+encoded[5]+encoded[8])[::-1]
        self.assertEqual(decode_mobile(encoded),PHONE)
        self.assertNotEqual(decode_mobile('unrelated-mobile'),PHONE)


class MiguCredentialTests(unittest.TestCase):
    def test_official_expiry_and_same_account_revision_reuse(self):
        snapshot={'phone':PHONE,'id':'a','credential_revision':1}
        cache=CredentialCache()
        adapter=CheckinMigu(lambda:snapshot,cache)
        value=identity();sessions=[]
        factory=requests.Session
        def session(**kwargs):
            client=factory(**kwargs)
            client.post=Mock(return_value=SimpleNamespace(status_code=200,json=lambda:copy.deepcopy(value)))
            sessions.append(client)
            return client
        parent=Mock()
        parent_session=Mock()
        parent_session.get.return_value=SimpleNamespace(status_code=200,json=lambda:{'code':'SUCCESS','data':'source-ticket'})
        with patch('features.checkin.migu.requests.Session',side_effect=session):
            with adapter._session(parent,parent_session) as client:
                self.assertEqual(client._credential_data['phone'],PHONE)
            snapshot['credential_revision']=2
            with adapter._session(parent,parent_session) as client:
                self.assertEqual(client.headers['userToken'],'opaque-migu-token')
        parent_session.get.assert_called_once()
        self.assertEqual(sum(c.post.call_count for c in sessions),1)
        self.assertEqual(sessions[0].headers['Accept'],'application/json, text/plain, */*')
        self.assertEqual(cache.load(SCOPE,snapshot)['expires_at'],int(value['userInfo']['expiredOn']/1000))

    def test_wrong_account_is_rejected_before_installing_credentials(self):
        adapter=CheckinMigu(Mock(),Mock());value=identity()
        value['userInfo']['mobile']='13900139000'
        with requests.Session() as client:
            with self.assertRaises(CheckinError) as error:
                adapter._attach_identity(client,value,{'phone':PHONE})
            self.assertEqual(error.exception.code,'ACCOUNT_MISMATCH')
            self.assertNotIn('userToken',client.headers)

    def test_expired_server_session_is_not_cached(self):
        adapter=CheckinMigu(Mock(),Mock());value=identity();value['userInfo']['expiredOn']=1
        with requests.Session() as client:
            with self.assertRaises(CheckinError):adapter._attach_identity(client,value,{'phone':PHONE})
            self.assertNotIn('userToken',client.headers)

    def test_unknown_write_result_never_triggers_reauthentication(self):
        cache=Mock();adapter=CheckinMigu(Mock(),cache);client=Mock()
        client.request.return_value=SimpleNamespace(status_code=401,headers={},text='{"code":401}')
        with self.assertRaises(CheckinError) as error:
            adapter._call(client,'POST','/gateway/ability/videox/task/openFinish',{'cToken':'task-token'},writing=True)
        self.assertTrue(error.exception.uncertain)
        cache.recover.assert_not_called()
        client.request.assert_called_once()


class MiguTaskTests(unittest.TestCase):
    def setUp(self):
        self.adapter=CheckinMigu(Mock(),Mock())
        self.client=SimpleNamespace(_credential_data={'user_id':'12345'})
        @contextmanager
        def session(*_args):yield self.client
        self.adapter._session=session
        self.adapter._call=Mock(side_effect=[{'code':200,'body':{'data':{'userId':'12345'}}},None])

    def test_login_identity_and_actual_page_precede_completion(self):
        before=Mock()
        with patch('features.checkin.migu.requests.Session') as page:
            page.return_value.__enter__.return_value.get.return_value.status_code=200
            result=self.adapter.complete({}, {'cToken':'partner-token'},Mock(),Mock(),before_submit=before)
        self.assertTrue(result['login_verified'])
        before.assert_called_once()
        self.assertEqual(self.adapter._call.call_args_list[0].args[1],'GET')
        self.assertEqual(self.adapter._call.call_args_list[1].args[3],{'cToken':'partner-token'})

    def test_different_user_info_never_reports_completion(self):
        self.adapter._call.side_effect=[{'code':200,'body':{'data':{'userId':'different'}}}]
        before=Mock()
        with self.assertRaises(CheckinError):
            self.adapter.complete({}, {'cToken':'partner-token'},Mock(),Mock(),before_submit=before)
        before.assert_not_called()
        self.adapter._call.assert_called_once()


class CheckinScopedIdentityTests(unittest.TestCase):
    def setUp(self):
        from app import create_app
        self.app=create_app()
        self.pool=self.app.extensions['unified_logins']
        self.saved=self.pool.remember(PHONE,'JSESSIONID=old-primary',{},'')
        self.cache=self.app.extensions['credential_cache']
        self.snapshot={**self.pool.credentials(self.saved.id),'id':self.saved.id}
        with requests.Session() as client:
            self.cache.connect('qwhdmark:1021122301',self.snapshot,client,
                               lambda:{'channel':'app','mobile':PHONE})
        self.pool.mark(self.saved,'login_required','APP needs verification')

    def test_valid_activity_session_survives_primary_app_rejection(self):
        scheduler=self.app.extensions['checkin_scheduler']
        credentials=scheduler.credentials(self.saved.id)
        self.assertEqual(credentials['primary_status'],'login_required')
        self.assertEqual(scheduler.accounts()[0]['status'],'active')
        self.assertEqual(self.pool.public()[0]['status'],'login_required')
        client=scheduler.client_factory(lambda:scheduler.credentials(self.saved.id))
        with patch.object(client,'_authenticate',side_effect=AssertionError('must reuse activity session')):
            with client._session():pass
        result=self.app.test_client().get('/api/checkin/beans/tasks').json
        self.assertTrue(result['accounts'][0]['activity_session_valid'])

    def test_missing_activity_session_does_not_retry_an_invalid_primary_cookie(self):
        self.cache.forget(self.saved.id,scope='qwhdmark:1021122301')
        with self.assertRaises(CheckinError):
            self.app.extensions['checkin_scheduler'].credentials(self.saved.id)

    def test_explicit_account_logout_blocks_even_a_cached_activity_identity(self):
        self.pool.forget(PHONE)
        with self.assertRaises(CheckinError):
            self.app.extensions['checkin_scheduler'].credentials(self.saved.id)
