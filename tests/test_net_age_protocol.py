import base64
import json
import unittest
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from activities import coc_wheel
from activities.net_age_protocol import PythonWheel, MOBILE_H5_UA, MOBILE_H5_HEADERS
from activities.net_age_transport import BUSINESS_URLS, NetAgeSession


class NetAgeProtocolTests(unittest.TestCase):
    def test_manual_event_bytes_match_the_sdk_fixture_and_are_consumed_once(self):
        runtime=PythonWheel()
        runtime.prepare([],MOBILE_H5_UA,{'page_url':'https://dev.coc.10086.cn/',
            'local_storage':{},'interaction':{'started_at':1795221221000,
                'events':[[1795221221446,2,283,478,0]]}})
        self.assertEqual(runtime._environment('gdt',[],MOBILE_H5_UA)['events'],'AGAEX7gfekICGwHeAQAA')
        self.assertEqual(runtime.initialized,1795221221000)
        self.assertEqual(runtime._environment('claim',[],MOBILE_H5_UA)['events'],'')
        runtime.close()

    def test_actual_xhr_wire_has_no_navigation_headers(self):
        captured={}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.update({k.lower():v for k,v in self.headers.items()})
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        worker=Thread(target=server.handle_request,daemon=True);worker.start()
        try:
            with NetAgeSession() as session:
                session.headers.update(MOBILE_H5_HEADERS)
                session.post(f'http://127.0.0.1:{server.server_address[1]}/xhr',json={},timeout=3)
        finally:
            server.server_close();worker.join(timeout=2)
        self.assertNotIn('upgrade-insecure-requests',captured)
        self.assertNotIn('sec-fetch-user',captured)
        self.assertEqual(captured['sec-fetch-mode'],'cors')
        self.assertEqual(captured['accept'],'application/json, text/plain, */*')

    def test_cookie_wire_preserves_the_order_used_by_the_sdk(self):
        captured={}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured['cookies']=self.headers.get_all('Cookie')
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        worker=Thread(target=server.handle_request,daemon=True);worker.start()
        try:
            with NetAgeSession() as session:
                for name,value in [('first','a'),('second','b'),('third','c')]:
                    session.cookies.set(name,value,domain='127.0.0.1',path='/')
                session.post(f'http://127.0.0.1:{server.server_address[1]}/cookie-order',json={},timeout=3)
        finally:
            server.server_close();worker.join(timeout=2)
        self.assertEqual(captured['cookies'],['first=a; second=b; third=c'])

    def test_account_context_and_device_token_stay_bound_without_external_runtime(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        context = {'page_url':'https://dev.coc.10086.cn/coc/web6/ageFeedbackV2/',
                   'local_storage':{'boundToken':'account-a','userId':'user-a'}}
        runtime = PythonWheel()
        runtime.prepare([], MOBILE_H5_UA, context)
        frames=[]
        def decode(body, headers):
            key = private.decrypt(base64.b64decode(headers['coc-aurora']), padding.PKCS1v15())
            raw = base64.b64decode(next(iter(body.values())))
            frame = json.loads(AESGCM(key).decrypt(raw[:12],raw[12:],None))
            self.assertEqual(frame['verification'],coc_wheel.verification(frame['environment'],frame['timestamp']))
            frames.append(frame)
            return frame
        def transport(body, headers):
            decode(body,headers)
            return {'code':'0','data':'official-device-a'}
        gateway = Mock(side_effect=transport)
        with patch.object(coc_wheel,'_public_key',private.public_key()):
            for code in ('123456','654321'):
                result=runtime.encrypt({'smsCode':code},[],MOBILE_H5_UA,transport=gateway)
                frame=decode(result['data'],result['headers'])
                self.assertEqual(frame['data']['deviceToken'],'official-device-a')
                self.assertEqual(frame['environment']['local_storage'],{**context['local_storage'],'benefit_phone':''})
        gateway.assert_called_once()
        self.assertEqual(frames[0]['data']['deviceToken'],'')
        self.assertEqual(frames[0]['environment']['device_token'],[])
        self.assertEqual(frames[1]['environment']['device_token'],['official-device-a'])
        runtime.close()
        self.assertEqual(runtime.token,'')
        self.assertIsNone(runtime.context)

    def test_storage_selection_and_post_gdt_cookies_match_the_official_sdk(self):
        runtime=PythonWheel()
        cookies=[]
        runtime.prepare(lambda:cookies,MOBILE_H5_UA,{'page_url':'https://dev.coc.10086.cn/',
            'local_storage':{'boundToken':'token-a','pageId':'page','channelId':'channel','userId':'user-a',
                'boundMobile':'138****0000','encryptedData':'exclude-from-telemetry',
                'canvasworkbenchweb_auth':json.dumps({'user_info':{'phone':'authorized-benefit-a'}}),
                'session_gdp_last_vst':'analytics'}})
        before=runtime._environment('gdt',[],MOBILE_H5_UA)
        self.assertEqual(before['local_storage'],{'boundToken':'token-a','pageId':'page',
            'channelId':'channel','userId':'user-a','benefit_phone':'authorized-benefit-a',
            'session_gdp_last_vst':'analytics'})
        self.assertEqual(before['cookie'],{})
        cookies.append({'name':'RiskToken','value':'official-risk-a','httpOnly':False})
        after=runtime._environment('claim',[],MOBILE_H5_UA)
        self.assertEqual(after['cookie'],{'RiskToken':'official-risk-a'})
        runtime.close()

    def test_tunnel_keeps_slider_sms_and_claim_on_the_same_connection_path(self):
        with patch('activities.net_age_transport._proxy',return_value='http://127.0.0.1:1') as proxy, \
                patch('http_transport.FeatureSession.request',return_value='fixture') as send:
            session=NetAgeSession()
            try:
                for url in sorted(BUSINESS_URLS):
                    session.post(url,json={})
                    self.assertEqual(send.call_args.kwargs['proxy'],'http://127.0.0.1:1')
                session.post('https://dev.coc.10086.cn/coc/activities/growth/query',json={})
                self.assertNotIn('proxy',send.call_args.kwargs)
                self.assertEqual(proxy.call_count,len(BUSINESS_URLS))
            finally:
                session.close()

    def test_member_initialization_and_claim_share_transport_and_preserve_headers(self):
        with patch('activities.net_age_transport._proxy',return_value='http://127.0.0.1:1'), \
                patch('http_transport.FeatureSession.request',return_value='fixture') as send:
            session=NetAgeSession(member=True)
            session.headers.update({'User-Agent':'member-fixture','Origin':'https://dev.coc.10086.cn'})
            original=session.headers
            try:
                for method,path in [('GET','web6/ageFeedbackV2/'),('POST','user/loginCheck'),
                        ('POST','user/auth/checkSliderImg'),('POST','wasmrc/gDT'),('POST','activities/prize/templatePrize/new')]:
                    session.request(method,'https://dev.coc.10086.cn/coc/'+path)
                    self.assertEqual(send.call_args.kwargs['proxy'],'http://127.0.0.1:1')
                    self.assertNotIn('extra_fp',send.call_args.kwargs)
                    self.assertEqual(send.call_args.kwargs['headers']['user-agent'],'member-fixture')
                    self.assertIs(session.headers,original)
                self.assertEqual(session.impersonate,'chrome142')
            finally:
                session.close()


if __name__ == '__main__':
    unittest.main()
