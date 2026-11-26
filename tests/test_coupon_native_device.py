from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib,io,json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock
import zipfile

from workspace.http_transport import requests,RequestException
from features.coupons.device import APP_ID
from features.coupons.native_codec import request_packet,parse_message,message,xxtea_encrypt,xxtea_decrypt
from features.coupons.native_device import NativeRechargeDevices,SERVER,SCOPE,TICKET_SECONDS,CLIENT_CONTEXT
from features.coupons.recharge_device import RechargeDeviceError
from workspace.accounts.storage import LoginStorageError


def response(token='native-fixture-ticket',code=0,status=200):
    inner=message([(1,token)])
    plain=message([(1,1002),(2,inner),(3,code)])
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w')as archive:archive.writestr('data',plain)
    key=hashlib.sha1(APP_ID.encode()*3).hexdigest().encode()[:16]
    return Mock(status_code=status,content=xxtea_encrypt(buffer.getvalue(),key))


class Storage:
    def __init__(self):self.data={};self.writes=0;self.fail=False
    def load_activity_data(self,scope):return deepcopy(self.data.get(scope))
    def save_activity_data(self,scope,data):
        if self.fail:raise LoginStorageError('fixture storage failed')
        self.data[scope]=deepcopy(data);self.writes+=1


def client(reply=None):
    return Mock(headers={'User-Agent':'existing registered client','Origin':'https://touch.10086.cn'},
        cookies=requests.Cookies(),post=Mock(return_value=reply or response()))


class NativeDeviceTests(unittest.TestCase):
    def setUp(self):
        self.clock=[10000]
        self.storage=Storage()
        self.snapshots={key:{'phone':'13800000000','cookie':'JSESSIONID='+key,
            'app_context':{'device_id':'registered-'+key},'credential_revision':1}for key in ('A','B')}
        self.provider=lambda key:deepcopy(self.snapshots[key])
        self.devices=NativeRechargeDevices(self.provider,self.storage,clock=lambda:self.clock[0])

    def test_native_issued_ticket_is_mounted_after_persist_and_business_headers_are_preserved(self):
        http=client();headers=http.headers
        result=self.devices.prepare('A',http)
        self.assertEqual((result['prepared'],result['reused'],result['provider']),(True,False,'m1'))
        self.assertIs(http.headers,headers)
        args,kwargs=http.post.call_args
        self.assertEqual(args,(SERVER,))
        self.assertEqual(kwargs['params']['appKey'],APP_ID)
        key=hashlib.sha1(APP_ID.encode()*3).hexdigest().encode()[:16]
        with zipfile.ZipFile(io.BytesIO(xxtea_decrypt(kwargs['data'],key)))as archive:plain=archive.read('data')
        outer=parse_message(plain);header=parse_message(outer[1][0])
        self.assertEqual(set(header),{1,2,3,4,5,6,8,9,10})
        self.assertEqual(header[2],[b'12.5.6']);self.assertEqual(header[3],[125600])
        self.assertEqual(header[6],[b'36'])
        pairs=[parse_message(item)for item in parse_message(outer[3][0])[1]]
        values={item[1][0].decode():item[2][0].decode()for item in pairs}
        self.assertEqual(set(values),{'K2','K3','K4','K7','K666','K667','K850','K11','K906','K56','K451','K452'})
        self.assertEqual((values['K2'],values['K3'],values['K4'],values['K850']),
            ('com.greenpoint.android.mc10086.activity','中国移动','12.5.6','125600'))
        self.assertEqual((values['K11'],values['K906'],values['K56']),('16','36','registered-A'))
        self.assertEqual((values['K7'],values['K666'],values['K667']),
            ('e0bcb95cdaf4777715e4fa5989a4446c','230290259','中国移动'))
        self.assertEqual(kwargs['params']['sign'],hashlib.md5(APP_ID.encode()+plain+APP_ID.encode()).hexdigest())
        cookie=next(iter(http.cookies.jar))
        self.assertEqual((cookie.value,cookie.domain,cookie.path,cookie.secure,cookie.expires),
            ('native-fixture-ticket','touch.10086.cn','/i',True,11200))
        self.assertFalse(cookie.domain_specified)
        self.assertEqual(self.storage.data[SCOPE]['A']['expires_at'],10000+TICKET_SECONDS)
        self.assertNotIn('native-fixture-ticket',json.dumps(result))

    def test_valid_business_cookie_is_reused_without_extending_its_deadline(self):
        http=client();self.devices.prepare('A',http)
        self.clock[0]+=400
        result=self.devices.prepare('A',http)
        self.assertTrue(result['reused']);self.assertEqual(http.post.call_count,1)
        self.assertEqual(next(iter(http.cookies.jar)).expires,11200)

    def test_ticket_survives_restart_and_app_revision_update_and_renews_only_cookie(self):
        first=client();self.devices.prepare('A',first)
        self.clock[0]+=1300
        self.snapshots['A'].update(cookie='JSESSIONID=new-valid-session',credential_revision=2)
        restored=NativeRechargeDevices(self.provider,self.storage,clock=lambda:self.clock[0]);http=client()
        result=restored.prepare('A',http)
        http.post.assert_not_called();self.assertTrue(result['reused'])
        self.assertEqual(result['expires_at'],12500)
        self.assertEqual(result['ticket_expires_at'],10000+TICKET_SECONDS)

    def test_native_ticket_expiry_requires_one_new_request(self):
        http=client();self.devices.prepare('A',http)
        self.clock[0]+=TICKET_SECONDS+1
        new=client(response('new-native-fixture-ticket'));result=self.devices.prepare('A',new)
        self.assertFalse(result['reused']);new.post.assert_called_once()
        self.assertEqual(next(iter(new.cookies.jar)).value,'new-native-fixture-ticket')
        body=new.post.call_args.kwargs['data']
        key=hashlib.sha1(APP_ID.encode()*3).hexdigest().encode()[:16]
        with zipfile.ZipFile(io.BytesIO(xxtea_decrypt(body,key)))as archive:plain=archive.read('data')
        header=parse_message(parse_message(plain)[1][0])
        self.assertEqual(header[7],[b'native-fixture-ticket'])

    def test_accounts_do_not_reuse_one_anothers_ticket_records(self):
        first,second=client(response('ticket-A')),client(response('ticket-B'))
        self.devices.prepare('A',first);self.devices.prepare('B',second)
        self.assertEqual(next(iter(first.cookies.jar)).value,'ticket-A')
        self.assertEqual(next(iter(second.cookies.jar)).value,'ticket-B')
        self.assertEqual(set(self.storage.data[SCOPE]),{'A','B'})
        for key,http in [('A',first),('B',second)]:
            cipher_key=hashlib.sha1(APP_ID.encode()*3).hexdigest().encode()[:16]
            with zipfile.ZipFile(io.BytesIO(xxtea_decrypt(http.post.call_args.kwargs['data'],cipher_key)))as archive:plain=archive.read('data')
            items=parse_message(parse_message(plain)[3][0])[1]
            fields={parse_message(item)[1][0]:parse_message(item)[2][0]for item in items}
            self.assertEqual(fields[b'K56'],('registered-'+key).encode())

    def test_legacy_valid_ticket_is_reused_until_explicit_missing_context_repair(self):
        self.devices.prepare('A',client());self.devices.records['A'].pop('request_context')
        ordinary=client();self.assertTrue(self.devices.prepare('A',ordinary)['reused']);ordinary.post.assert_not_called()
        completed=client(response('completed-client-ticket'))
        result=self.devices.prepare('A',completed,repair_registered_client=True)
        self.assertFalse(result['reused']);completed.post.assert_called_once()
        self.assertEqual(self.storage.data[SCOPE]['A']['request_context'],CLIENT_CONTEXT)
        final=client();self.assertTrue(self.devices.prepare('A',final,repair_registered_client=True)['reused'])
        final.post.assert_not_called()

    def test_concurrent_requests_share_ticket_but_not_http_handles(self):
        entered,finish=threading.Event(),threading.Event();first,second=client(),client()
        def post(*args,**kwargs):entered.set();self.assertTrue(finish.wait(2));return response()
        first.post.side_effect=post
        with ThreadPoolExecutor(2)as pool:
            owner=pool.submit(self.devices.prepare,'A',first);self.assertTrue(entered.wait(2))
            follower=pool.submit(self.devices.prepare,'A',second);finish.set()
            self.assertTrue(owner.result()['prepared']);self.assertTrue(follower.result()['reused'])
        first.post.assert_called_once();second.post.assert_not_called()
        self.assertIsNot(first.cookies,second.cookies)

    def test_rejected_or_malformed_response_never_becomes_a_ticket(self):
        for reply in [response(code=17),Mock(status_code=200,content=b'not native ciphertext')]:
            with self.subTest(reply=reply):
                http=client(reply)
                with self.assertRaises(RechargeDeviceError):self.devices.prepare('A',http)
                self.assertEqual(list(http.cookies.jar),[]);self.assertNotIn('A',self.devices.records)

    def test_rate_limit_and_network_failure_preserve_headers_and_do_not_retry(self):
        for reply,error,code in [(Mock(status_code=429),None,'RISK_RATE_LIMITED'),(None,RequestException('fixture'),'RISK_NETWORK_FAILED')]:
            http=client(reply);headers=http.headers
            if error:http.post.side_effect=error
            with self.assertRaises(RechargeDeviceError)as raised:self.devices.prepare('A',http)
            self.assertEqual(raised.exception.code,code);self.assertIs(http.headers,headers)
            http.post.assert_called_once();self.assertEqual(list(http.cookies.jar),[])

    def test_missing_registered_context_does_not_create_a_device_identity(self):
        self.snapshots['A']['app_context']={};http=client()
        with self.assertRaises(RechargeDeviceError)as raised:self.devices.prepare('A',http)
        self.assertEqual(raised.exception.code,'RISK_CLIENT_CONTEXT_MISSING');http.post.assert_not_called()
        self.assertEqual(self.snapshots['A']['app_context'],{})

    def test_storage_failure_stops_before_mounting_the_issued_ticket(self):
        self.storage.fail=True;http=client()
        with self.assertRaises(RechargeDeviceError)as raised:self.devices.prepare('A',http)
        self.assertEqual(raised.exception.code,'RISK_STORAGE_FAILED');self.assertEqual(list(http.cookies.jar),[])

    def test_forget_cancels_an_inflight_registration_before_mounting(self):
        http=client();http.post.side_effect=lambda *a,**kw:(self.devices.forget('A'),response())[1]
        with self.assertRaises(RechargeDeviceError)as raised:self.devices.prepare('A',http)
        self.assertEqual(raised.exception.code,'RISK_PREPARATION_CANCELLED')
        self.assertEqual(list(http.cookies.jar),[]);self.assertNotIn('A',self.devices.records)


class NativeCodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.fixture=json.loads((Path(__file__).parent/'fixtures/coupon-native-codec.json').read_text('utf-8'))

    def test_python_xxtea_matches_independent_shipped_native_fixtures(self):
        for case in self.fixture['xxtea_cases']:
            with self.subTest(bytes=len(case['plain_hex'])//2):
                plain,key,cipher=(bytes.fromhex(case[name])for name in ('plain_hex','key_hex','cipher_hex'))
                self.assertEqual(xxtea_encrypt(plain,key),cipher);self.assertEqual(xxtea_decrypt(cipher,key),plain)

    def test_request_bytes_match_shipped_native_protobuf_packer(self):
        plain,_,_=request_packet(self.fixture['app_id'],self.fixture['pairs'],package_name='fixture.package',
            software_version='fixture-version',software_version_code=123,android_api='fixture-api')
        self.assertEqual(plain.hex(),self.fixture['request_plain_hex'])


if __name__=='__main__':unittest.main()
