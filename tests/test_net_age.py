from contextlib import nullcontext
from datetime import datetime, timedelta
from email.message import Message
from http.cookiejar import Cookie
import json
import re
import unittest
from unittest.mock import MagicMock, Mock, patch

from activities.coc_client import CocError
from activities import net_age_wheel
from workspace.accounts.credentials import CredentialCache
from workspace.http_transport import requests
from features.net_age.service import AI_IDS, APP_ENTRY, BIRTH_IDS, CHANNEL, ENTRY, FLOW_CODES, FLOW_IDS, MEMBER_ALREADY_CLAIMED, MEMBER_HISTORY_IDS, MEMBER_ID, PAGE_ID, WASM_PROFILE, NetAge, NetAgeClient, _config, _wrong_sms
from recharge.cmcc import SHANGHAI
from workspace.accounts.login import AppLoginSession


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
        with patch('features.net_age.service.NetAgeClient', return_value=client):
            preparation = self.service.begin('a', 'member', 'qq-music')
        self.assertTrue(preparation['needs_slider'])
        client.prepare_wheel.assert_called_once_with()
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

    def test_monthly_flow_sms_and_claim_share_the_current_app_channel(self):
        client = Mock(channel=CHANNEL, user={
            'provinceCode':'851', 'maskedMobile':'182****1621', 'userId':'official-user-id',
        })
        client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [], [])}

        def reply(path, _payload, **options):
            if before_submit := options.get('before_submit'):
                before_submit()
            if path == 'activities/prize/templatePrize/new':
                raise CocError('领取失败（10）', '10')
            return {}

        client.post.side_effect = reply
        with patch('features.net_age.service.NetAgeClient', return_value=client), patch('features.net_age.service._config', return_value={}):
            operation = self.service.begin('a', 'flow', 'monthly')['operation']
            self.service.verification('a', operation, 'sms', {})
            result = self.service.verification('a', operation, 'submit', {'code':'123456'})

        self.assertEqual(result, {'message':'本次领取失败', 'confirmed':False, 'code':'10'})
        sms = next(call for call in client.post.call_args_list if call.args[0] == 'user/smsCode')
        self.assertEqual(sms.args[1], {
            'mobile':'182****1621', 'userId':'official-user-id', 'productCode':FLOW_CODES[2],
            'pageId':PAGE_ID, 'touchCode':CHANNEL, 'smsCodeTypeEnum':'505',
        })
        claim = next(call for call in client.post.call_args_list
                     if call.args[0] == 'activities/prize/templatePrize/new')
        self.assertEqual(claim.kwargs['wheel_environment'], 'net-age-python')
        self.assertNotIn('wheel_encrypt', claim.kwargs)
        self.assertEqual(json.loads(claim.args[1]['jsonExtParam']), {
            'smsCode':'123456', 'touchCode':CHANNEL, 'productCode':FLOW_CODES[2],
        })
        self.assertEqual(list(claim.args[1]), ['lotteryId', 'provinceCode', 'jsonExtParam'])
        client.inspect.assert_called_once_with(self.now, recover=True)
        self.assertEqual([call.args[0] for call in client.post.call_args_list], [
            'activities/growth/verify/status', 'user/smsCode', 'activities/prize/templatePrize/new',
        ])

    def test_flow_probe_reuses_login_and_refreshes_crypto_for_explicit_rechecks(self):
        client = Mock(channel=CHANNEL, user={'provinceCode':'851'})
        client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [], [])}
        def reply(path, _payload, **options):
            if path.endswith('/new'):
                options['before_submit']()
                raise CocError('验证码输入有误', '10')
            return {}
        client.post.side_effect = reply
        with patch('features.net_age.service.NetAgeClient', return_value=client), patch('features.net_age.service._config', return_value={}):
            prepared = self.service.begin('a', 'flow', 'monthly', probe=True)
        self.assertTrue(prepared['probe'])
        client.prepare_wheel.assert_called_once_with(flow_slide_provinces='', probe=True)
        for _ in range(2):
            result = self.service.verification('a', prepared['operation'], 'probe', {'code':'123456'})
            self.assertTrue(result['diagnostic_only'])
            self.assertFalse(result['confirmed'])
            self.assertTrue(result['passed'])
            self.assertIn('验证码输入有误', result['message'])
        self.assertEqual([call.args[0] for call in client.post.call_args_list],
            ['activities/growth/verify/status', 'activities/prize/templatePrize/new', 'activities/prize/templatePrize/new'])
        for call in client.post.call_args_list[1:]:
            code = json.loads(call.args[1]['jsonExtParam'])['smsCode']
            self.assertEqual(code, '123456')
        client.connect.assert_called_once()
        self.assertEqual(client.prepare_wheel.call_count, 2)
        client.close.assert_not_called()
        self.assertNotIn('a', self.service.submissions)
        with self.assertRaises(CocError) as blocked:
            self.service.verification('a', prepared['operation'], 'sms', {})
        self.assertEqual(blocked.exception.code, 'PROBE_ONLY')
        self.service.verification('a', prepared['operation'], 'cancel', {})
        client.close.assert_called_once()

    def test_probe_only_passes_on_explicit_wrong_sms_not_generic_failure_or_busy(self):
        client = Mock(channel=CHANNEL, user={'provinceCode':'851'})
        client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [], [])}
        for code, message, passed in [('10','验证码输入有误',True), ('10','领取失败！',False),
                                     ('1000008051','活动太火爆了，请稍后再试',False),
                                     ('10','请求太频繁',False)]:
            with self.subTest(message=message):
                def reply(path, _payload, **options):
                    if path.endswith('/new'):
                        options['before_submit']()
                        raise CocError(message, code)
                    return {}
                client.post.side_effect = reply
                with patch('features.net_age.service.NetAgeClient', return_value=client), patch('features.net_age.service._config', return_value={}):
                    prepared = self.service.begin('a', 'flow', 'monthly', probe=True)
                result = self.service.verification('a', prepared['operation'], 'probe', {})
                self.assertEqual(result['passed'], passed)
                self.assertFalse(result['confirmed'])
                self.service.verification('a', prepared['operation'], 'cancel', {})

    def test_flow_reuses_compatible_coc_session_without_getting_another_app_ticket(self):
        snapshot = {'id':'a','phone':'13800138000','cookie':'JSESSIONID=fixture'}
        cache = CredentialCache()
        cache.entries[('seven-day','a')] = {'phone':snapshot['phone'],
            'data':{'user':{'userId':'fixture'}}, 'cookies':[], 'headers':{},
            'expires_at':None, 'generation':'fixture', 'created_at':0}
        client = NetAgeClient(lambda:snapshot,cache,None,app_auth=True)
        client.session_factory=lambda:Mock(headers={},cookies=requests.Cookies())
        with patch.object(client,'_authenticate',side_effect=AssertionError('must reuse compatible session')):
            client.connect()
        self.assertEqual(client.user['userId'],'fixture')
        client.close()

    def test_flow_probe_does_not_skip_an_official_slider_requirement(self):
        client = Mock(channel=CHANNEL, user={'provinceCode':'931'})
        client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [], [])}
        with patch('features.net_age.service.NetAgeClient', return_value=client), \
                patch('features.net_age.service._config', return_value={'configContent_27':'931'}):
            with self.assertRaises(CocError) as blocked:
                self.service.begin('a', 'flow', 'monthly', probe=True)
        self.assertEqual(blocked.exception.code, 'SLIDER_REQUIRED')
        client.prepare_wheel.assert_not_called()
        client.close.assert_called_once()

    def test_probe_acceptance_or_uncertain_response_stops_without_replaying_or_claiming_success(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                client = Mock(channel=CHANNEL,user={'provinceCode':'851'})
                client.inspect.return_value = {'rows':self.client.benefits(5,None,False,[],[])}
                def response(path, _payload, **options):
                    if path.endswith('/new'):
                        options['before_submit']()
                        if uncertain:
                            raise CocError('连接中断', 'UPSTREAM_NETWORK', uncertain=True)
                    return {}
                client.post.side_effect = response
                self.service.submissions = {}
                with patch('features.net_age.service.NetAgeClient',return_value=client), patch('features.net_age.service._config',return_value={}):
                    prepared = self.service.begin('a','flow','monthly',probe=True)
                if uncertain:
                    with self.assertRaises(CocError):
                        self.service.verification('a',prepared['operation'],'probe',{})
                else:
                    result = self.service.verification('a',prepared['operation'],'probe',{})
                    self.assertTrue(result['stop'])
                    self.assertFalse(result['confirmed'])
                self.assertTrue(self.service.submissions['a']['flow']['diagnostic_only'])
                self.assertNotIn('a',self.service.pending)
                self.assertEqual(sum(call.args[0].endswith('/new') for call in client.post.call_args_list),1)
                client.close.assert_called_once()

    def test_member_probe_automatically_verifies_and_consumes_fresh_sliders_without_sms(self):
        client = Mock(channel=CHANNEL,user={'provinceCode':'851'})
        client.last_wheel_exchange = {}
        client.orders.return_value=[]
        client.inspect.return_value = {'rows':self.client.benefits(5,None,False,[],[])}
        def reply(path,payload,**options):
            if path.endswith('/new'):
                options['before_submit']()
                extra=json.loads(payload['jsonExtParam'])
                self.assertEqual(extra['accessToken'],'official-slider')
                self.assertEqual(extra['smsCode'],'123456')
                raise CocError('图验校验成功，短信验证码校验失败！','1000009990')
            if path.endswith('getSliderImg'):
                return {'ground':'fixture','patch':'fixture','ordinate':10,'authToken':'challenge'}
            if path.endswith('checkSliderImg'):
                return {'accessToken':'official-slider'}
            return {}
        client.post.side_effect=reply
        with patch('features.net_age.service.NetAgeClient',return_value=client):
            prepared=self.service.begin('a','member','youku',probe=True)
        operation=prepared['operation']
        with patch('features.net_age.service.slide_position',return_value=100):
            for _ in range(2):
                result=self.service.verification('a',operation,'probe',{})
                self.assertTrue(result['passed'])
                self.assertTrue(result['refresh_slider'])
                self.assertEqual(self.service.pending['a'].access_token,'')
        checks=[c for c in client.post.call_args_list if c.args[0].endswith('checkSliderImg')]
        self.assertEqual([c.args[1]['abscissa'] for c in checks],[100,100])
        self.assertFalse(any(c.args[0]=='user/smsCode' for c in client.post.call_args_list))
        self.service.verification('a',operation,'cancel',{})

    def test_member_submit_refreshes_slider_after_queries_and_keeps_sms_for_explicit_retries(self):
        client=Mock(channel=CHANNEL,user={'provinceCode':'851','userId':'user-a'})
        client.orders.return_value=[]
        client.inspect.return_value={'rows':self.client.benefits(5,None,False,[],[])}
        challenges=[];claims=[];codes=iter(('1000008051','1000009990'))
        def reply(path,payload,**options):
            if path.endswith('getSliderImg'):
                challenges.append('challenge-'+str(len(challenges)+1))
                return {'ground':'fixture','patch':'fixture','ordinate':10,'authToken':challenges[-1]}
            if path.endswith('checkSliderImg'):
                return {'accessToken':'access-'+payload['authToken']}
            if path=='user/smsCode':
                options['before_submit']();return {}
            if path.endswith('/new'):
                options['before_submit']();claims.append(json.loads(payload['jsonExtParam']))
                code=next(codes)
                raise CocError('活动太火爆了' if code=='1000008051' else '图验校验成功，短信验证码校验失败！',code)
            return {}
        client.post.side_effect=reply
        with patch('features.net_age.service.NetAgeClient',return_value=client),patch('features.net_age.service.slide_position',return_value=125):
            operation=self.service.begin('a','member','youku')['operation']
            self.service.verification('a',operation,'auto-slider',{})
            self.service.verification('a',operation,'sms',{})
            for expected in ('1000008051','1000009990'):
                with self.assertRaises(CocError) as error:
                    self.service.verification('a',operation,'submit',{'code':'123456','interaction':{
                        'started_at':1,'events':[[2,1,1600,900,0]]}})
                self.assertEqual(error.exception.code,expected)
                self.assertEqual(self.service.pending['a'].access_token,'')
        self.assertEqual([c['accessToken'] for c in claims],['access-challenge-1','access-challenge-2'])
        self.assertEqual(sum(c.args[0]=='user/smsCode' for c in client.post.call_args_list),1)
        paths=[c.args[0] for c in client.post.call_args_list]
        self.assertEqual(paths[-3:],['user/auth/getSliderImg','user/auth/checkSliderImg','activities/prize/templatePrize/new'])
        self.assertEqual(paths.count('activities/growth/verify/status'),1)
        self.assertTrue(all(not c.args and not c.kwargs for c in client.prepare_wheel.call_args_list))
        client.close.assert_not_called()
        self.service.verification('a',operation,'cancel',{})

    def test_auto_slider_failure_stops_before_sms_or_claim(self):
        client=Mock(user={'userId':'user-a'})
        client.inspect.return_value={'rows':self.client.benefits(5,None,False,[],[])}
        client.post.side_effect=lambda path,*a,**k: {'ground':'fixture','patch':'fixture','ordinate':10,
            'authToken':'challenge'} if path.endswith('getSliderImg') else {}
        with patch('features.net_age.service.NetAgeClient',return_value=client),patch('features.net_age.service.slide_position',
                side_effect=CocError('识别失败','SLIDER_RECOGNITION')):
            operation=self.service.begin('a','member','youku')['operation']
            with self.assertRaises(CocError):self.service.verification('a',operation,'auto-slider',{})
        self.assertFalse(any(c.args[0] in ('user/smsCode','activities/prize/templatePrize/new')
                             for c in client.post.call_args_list))

    def test_flow_duplicate_after_sms_is_reconciled_without_replaying_claim(self):
        offer = self.client.benefits(5, None, False, [], [])['flow']['offers'][0]
        receipt = {'activity_id':offer['activity'], 'skuCode':FLOW_CODES[2],
                   'createTime':'2026-11-18 10:00:00'}
        for orders in ([], [receipt]):
            with self.subTest(order_found=bool(orders)):
                client = Mock(user={'provinceCode':'851'})
                def duplicate(*_, before_submit, **__):
                    before_submit()
                    raise CocError('请勿重复领取', MEMBER_ALREADY_CLAIMED)
                client.post.side_effect = duplicate
                client.orders.return_value = orders

                result = self.service._submit(client, 'a', 'flow', offer, '2026-11', code='123456')

                self.assertEqual(result['confirmed'], bool(orders))
                if not orders:
                    self.assertIn('领取记录待核对', result['message'])
                client.post.assert_called_once()
                client.orders.assert_called_once_with([offer['activity']], recover=False)

    def test_node_wheel_bridge_passes_page_storage_and_cookies(self):
        import io
        reply = Mock()
        reply.stdin = io.StringIO()
        reply.stdout = io.StringIO(json.dumps({'ok':True,'result':{'cookies':[]}})+'\n'+json.dumps({'type':'http','body':{'encryptedText':'fixture'},'headers':{'coc-aurora':'fixture'}})+'\n'+json.dumps({'ok':True, 'result':{
            'data':{'cocEnContent':'fixture'}, 'headers':{'coc-aurora':'fixture'},
            'cookies':[], 'user_agent':'fixture-agent',
        }}) + '\n')
        captured = []
        reply.stdin.close = lambda:captured.append(reply.stdin.getvalue())
        transport = Mock(return_value={'code':'0','data':'fixture-token','success':True})
        with patch('activities.net_age_wheel.shutil.which', return_value='node'), \
                patch('activities.net_age_wheel.subprocess.Popen', return_value=reply):
            result = net_age_wheel.encrypt(
                {'lotteryId':'fixture'},
                [{'name':'User-Token','value':'fixture','domain':'dev.coc.10086.cn','path':'/'}],
                'Mozilla/5.0 Mobile leadeon/12.5.6',
                transport=transport,
                environment=lambda:{'local_storage':{'pageId':PAGE_ID,'channelId':CHANNEL}})

        commands = [json.loads(line) for line in captured[0].splitlines()]
        command = commands[0]
        self.assertEqual(command['storage'], {'pageId':PAGE_ID,'channelId':CHANNEL})
        self.assertEqual(command['cookies'][0]['name'], 'User-Token')
        self.assertIn('leadeon/', command['userAgent'])
        self.assertEqual(result['user_agent'], 'fixture-agent')
        transport.assert_called_once_with({'encryptedText':'fixture'}, {'coc-aurora':'fixture'})
        self.assertEqual(commands[1], {'type':'encrypt','payload':{'lotteryId':'fixture'}})
        self.assertEqual(commands[2], {'type':'http_result','value':transport.return_value})

    def test_wheel_user_agent_matches_the_workbench_app_ticket_device_model(self):
        device = AppLoginSession().envelope({})
        self.assertIn('; '+device['sn']+';', net_age_wheel.MOBILE_APP_UA)
        self.assertIn('Android '+device['sv']+';', net_age_wheel.MOBILE_APP_UA)
        self.assertIn('leadeon/'+device['cv']+'/CMCCIT', net_age_wheel.MOBILE_APP_UA)

    def test_wheel_snapshot_seeds_page_cookies_before_node_generation(self):
        session = Mock(headers={'User-Agent':'official-agent'}, cookies=requests.Cookies())
        session.cookies.set('User-Token', 'fixture', domain='dev.coc.10086.cn', path='/')
        self.client.client = session
        self.client.user = {'provinceCode':'851', 'cityCode':'851', 'userId':'fixture-user'}
        captured = {}

        def sdk(payload, cookies, user_agent, *, transport, environment):
            captured.update(payload=payload, cookies=cookies, user_agent=user_agent, environment=environment)
            return {'data':{'cocEnContent':'fixture'}, 'headers':{'coc-aurora':'fixture'},
                    'cookies':cookies, 'user_agent':user_agent, 'diagnostics':{}}

        with patch.object(self.client, 'current'):
            self.client.wheel_request({'lotteryId':'fixture'}, sdk_encrypt=sdk)

        names = {cookie['name'] for cookie in captured['cookies']}
        self.assertIn('User-Token', names)
        self.assertIn('gdp_user_id', names)
        self.assertIn('9e4e5fa7244c6b6e_gdp_session_id', names)
        self.assertEqual(captured['environment']['local_storage']['boundProvinceCode'], '851')

    def test_wheel_submits_the_official_frontend_wire_body_without_reserializing_it(self):
        response=Mock(status_code=200,headers={})
        response.json.return_value={'code':'0','data':{'accepted':True}}
        self.client.client=Mock(headers={'User-Agent':'fixture'},cookies=requests.Cookies())
        self.client.client.post.return_value=response
        wire='{"cocEnContent":"fixture"}'
        encrypted={'data':{'cocEnContent':'fixture'},'wire_body':wire,
                   'headers':{'coc-aurora':'fixture','Content-Type':'application/json'}}
        with patch.object(self.client,'current'),patch.object(self.client,'environment',return_value={}), \
                patch('activities.coc_wheel.encrypt',return_value=encrypted):
            result=self.client.post('activities/prize/templatePrize/new',{},encryption='wheel',mutation=True)
        self.assertEqual(result,{'accepted':True})
        sent=self.client.client.post.call_args.kwargs
        self.assertEqual(sent['data'],wire)
        self.assertNotIn('json',sent)

    def test_node_cookie_roundtrip_preserves_host_scope_and_does_not_duplicate_normalized_domains(self):
        self.client.client = Mock(cookies=requests.Cookies())
        self.client.client.cookies.set('User-Token', 'fixture', domain='dev.coc.10086.cn', path='/')
        current = next(iter(self.client.client.cookies.jar))
        current.domain_specified = False
        original = self.client._wheel_cookies()[0]
        self.assertTrue(original['hostOnly'])
        self.client._apply_wheel_result({'cookies':[{**original, 'value':'rotated-fixture'}]})
        self.assertFalse(current.domain_specified)
        self.assertEqual(current.value, 'rotated-fixture')

        self.client.client.cookies.set('analytics', 'fixture', domain='.10086.cn', path='/')
        self.client._apply_wheel_result({'cookies':[{'name':'analytics','value':'new-fixture',
            'domain':'10086.cn','path':'/','hostOnly':False,'httpOnly':False,'secure':True}]})
        matching = [c for c in self.client.client.cookies.jar if c.name == 'analytics']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].value, 'new-fixture')
        self.assertEqual(matching[0].domain, '.10086.cn')
        self.assertTrue(matching[0].secure)

    def test_node_business_rejection_retains_the_original_response_for_local_diagnosis(self):
        response = Mock(status_code=200, headers={'trace-id':'fixture-trace'})
        response.json.return_value = {'code':'1000008051','message':'活动太火爆了',
                                      'data':{'diagnostic':'fixture-reason'}}
        self.client.client = Mock(headers={'User-Agent':'fixture'}, cookies=requests.Cookies())
        self.client.client.post.return_value = response
        with patch.object(self.client, 'current'), patch.object(self.client, 'wheel_request',
                return_value=({'cocEnContent':'fixture'}, {'coc-aurora':'fixture'})):
            with self.assertRaises(CocError):
                self.client.post('activities/prize/templatePrize/new', {}, encryption='wheel',
                                 mutation=True, wheel_environment='node-page')
        captured = self.client.last_wheel_exchange['claim']
        self.assertEqual(captured['response'], response.json.return_value)
        self.assertEqual(captured['response_headers']['trace-id'], 'fixture-trace')
        self.assertEqual(captured['body'], {'cocEnContent':'fixture'})

    def test_python_probe_transport_preserves_cookies_payload_and_certificate_validation(self):
        self.client.client = Mock(headers={'User-Agent':'fixture-agent'}, cookies=requests.Cookies())
        self.client.client.cookies.set('User-Token','fixture-token',domain='dev.coc.10086.cn',path='/')
        self.client.last_wheel_exchange = {'claim':{}}
        headers = Message()
        headers.add_header('Set-Cookie','rotated=fixture; Path=/; Secure; HttpOnly')
        response = Mock(status=200, headers=headers)
        response.info.return_value = headers
        response.read.return_value = b'{"code":"fixture-rejected"}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch('features.net_age.service.HTTPSConnection',return_value=connection) as factory:
            result = self.client.python_business_post('https://dev.coc.10086.cn/coc/activities/prize/templatePrize/new',
                data='{"cocEnContent":"fixture"}',headers={'coc-aurora':'fixture'},
                timeout=(8,20),allow_redirects=False)
        self.assertEqual(result.json(), {'code':'fixture-rejected'})
        context = factory.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertNotEqual(context.verify_mode, 0)
        sent = connection.request.call_args
        self.assertEqual(sent.args, ('POST','/coc/activities/prize/templatePrize/new'))
        self.assertEqual(sent.kwargs['body'], b'{"cocEnContent":"fixture"}')
        self.assertEqual(sent.kwargs['headers']['Cookie'], 'User-Token=fixture-token')
        self.assertEqual(sent.kwargs['headers']['User-agent'], 'fixture-agent')
        self.assertEqual(self.client.client.cookies.get('rotated'), 'fixture')
        connection.close.assert_called_once()

    def test_valid_activity_credentials_survive_primary_app_login_expiry(self):
        snapshot = {'id':'a','phone':'13800138000','cookie':'JSESSIONID=fixture',
                    'primary_status':'login_required'}
        cache = CredentialCache()
        cache.entries[('net-age', 'a')] = {'phone':snapshot['phone'], 'data':{'user':{'userId':'fixture'}},
            'cookies':[], 'headers':{}, 'expires_at':None, 'generation':'fixture', 'created_at':0}
        client = NetAgeClient(lambda:snapshot, cache, None)
        client.session_factory = lambda:Mock(headers={}, cookies=requests.Cookies())
        with patch.object(client, '_authenticate', side_effect=AssertionError('must reuse activity login')):
            client.connect()
            client.current()
        self.assertEqual(client.user, {'userId':'fixture'})
        client.close()

    def test_read_only_queries_reuse_the_preferred_flow_session_when_present(self):
        self.service.credentials = lambda _: {'id':'a','phone':'13800138000','primary_status':'login_required'}
        self.service.cache = Mock(load=Mock(return_value={'data':{'user':{'userId':'fixture'}}}))
        with patch('features.net_age.service.NetAgeClient') as factory:
            with self.service.connected('a'):
                pass
        self.assertTrue(factory.call_args.kwargs['app_auth'])
        factory.return_value.connect.assert_called_once()
        factory.return_value.close.assert_called_once()

    def test_missing_activity_session_requires_primary_login_without_attempting_exchange(self):
        snapshot = {'id':'a','phone':'13800138000','cookie':'JSESSIONID=fixture',
                    'primary_status':'login_required'}
        client = NetAgeClient(lambda:snapshot, CredentialCache(), None)
        client.session_factory = lambda:Mock(headers={}, cookies=requests.Cookies())
        with patch('activities.coc_client.AppLoginSession.from_credentials') as authenticate:
            with self.assertRaises(CocError) as required:
                client.connect()
            authenticate.assert_not_called()
        self.assertEqual(required.exception.code, 'APP_LOGIN_REQUIRED')
        client.close()

    def test_member_sms_resend_requires_a_fresh_slider(self):
        client = Mock(user={'userId':'fixture'})
        client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [], [])}

        def reply(path, _, **options):
            if path == 'user/auth/getSliderImg':
                return {'ground':'fixture', 'patch':'fixture', 'authToken':'fixture', 'ordinate':20}
            if path == 'user/auth/checkSliderImg':
                return {'accessToken':'fixture'}
            if path == 'user/smsCode':
                options['before_submit']()
            return {}

        client.post.side_effect = reply
        with patch('features.net_age.service.NetAgeClient', return_value=client), \
                patch('features.net_age.service.time.monotonic', return_value=1000) as clock:
            operation = self.service.begin('a', 'member', 'bilibili')['operation']
            self.service.verification('a', operation, 'challenge', {})
            self.service.verification('a', operation, 'slider', {'position':20})
            self.service.verification('a', operation, 'sms', {})

            with self.assertRaises(CocError) as cooldown:
                self.service.verification('a', operation, 'challenge', {})
            self.assertEqual(cooldown.exception.code, 'SMS_COOLDOWN')
            clock.return_value = 1060
            with self.assertRaises(CocError) as old_slider:
                self.service.verification('a', operation, 'sms', {})
            self.assertEqual(old_slider.exception.code, 'SLIDER_REQUIRED')

            self.service.verification('a', operation, 'challenge', {})
            self.service.verification('a', operation, 'slider', {'position':20})
            with self.assertRaises(CocError) as old_code:
                self.service.verification('a', operation, 'submit', {'code':'123456'})
            self.assertEqual(old_code.exception.code, 'SMS_REQUIRED')
            self.service.verification('a', operation, 'sms', {})

        self.assertEqual([call.args[0] for call in client.post.call_args_list], [
            'activities/growth/verify/status', 'user/auth/getSliderImg', 'user/auth/checkSliderImg', 'user/smsCode',
            'user/auth/getSliderImg', 'user/auth/checkSliderImg', 'user/smsCode',
        ])

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

    def test_birthday_h5_preparation_is_python_only_and_checks_the_page_once(self):
        snapshot={'id':'a','phone':'13800138000','cookie':'JSESSIONID=fixture; UID=fixture'}
        cache=Mock()
        cache.connect.return_value={'user':{'userId':'birthday-user','maskedMobile':'138****8000','token':'birthday-token'}}
        client=NetAgeClient(lambda:snapshot,cache,None,birthday=True)
        client.session_factory=lambda:Mock(headers={},cookies=requests.Cookies(),close=Mock(),
            get=Mock(return_value=Mock(status_code=200)))
        with patch.object(client,'post',return_value=False) as post:
            client.connect();client.prepare_wheel();client.prepare_wheel()
        post.assert_called_once_with('activities/growth/query/evaluate',{})
        self.assertEqual(client.entry,APP_ENTRY)
        self.assertIn('Chrome/143.',client.client.headers['User-Agent'])
        self.assertEqual(client.wheel_runtime.context['local_storage']['boundToken'],'birthday-token')
        self.assertTrue(client.wheel_runtime.context['member_page_checked'])
        self.assertTrue(any('closeShareBirthdayNote2' in line and ':324789)' in line
            for line in client.wheel_runtime.profile['last_call_stack']['claim']))
        client.close()

    def test_birthday_wrong_sms_is_not_reported_as_an_encryption_failure(self):
        years=re.match(r'(\d+)年','13年')
        offer=self.client.benefits(5,years,True,[],[])['birthday']['offers'][0]
        client=Mock(user={'provinceCode':'851'})
        def rejected(*args,before_submit,**kwargs):
            before_submit()
            raise CocError('验证码输入有误（10）','10')
        client.post.side_effect=rejected
        with self.assertRaises(CocError) as result:
            self.service._submit(client,'a','birthday',offer,'2026',code='123456')
        self.assertIn('验证码输入有误',str(result.exception))
        self.assertIsNone(self.service.submissions.get('a',{}).get('birthday'))
        client.orders.assert_not_called()

    def test_graphic_verification_error_is_not_a_successful_sms_protocol_probe(self):
        self.assertFalse(_wrong_sms(CocError('图形验证码错误','10')))
        self.assertFalse(_wrong_sms(CocError('图验校验失败，验证码错误','10')))
        self.assertTrue(_wrong_sms(CocError('验证码输入有误','10')))
        self.assertTrue(_wrong_sms(CocError('图验校验成功，短信验证码校验失败！','1000009990')))

    def test_birthday_cinema_probe_uses_a_fresh_official_slider_and_python_envelope(self):
        years=re.match(r'(\d+)年','13年')
        client=Mock(user={'userId':'birthday-user','provinceCode':'851'},channel=CHANNEL,last_wheel_exchange={})
        client.inspect.return_value={'rows':self.client.benefits(5,years,True,[],[])}
        client.orders.return_value=[]
        def response(path,payload,**options):
            if path=='user/auth/getSliderImg':return {'ground':'fixture','patch':'fixture','authToken':'challenge','ordinate':20}
            if path=='user/auth/checkSliderImg':return {'accessToken':'one-use-access'}
            if path=='activities/prize/templatePrize/new':
                options['before_submit']()
                raise CocError('图验校验成功，短信验证码校验失败！','1000009990')
            return {}
        client.post.side_effect=response
        with patch('features.net_age.service.NetAgeClient',return_value=client),patch('features.net_age.service.slide_position',return_value=20):
            begin=self.service.begin('a','birthday','cinema',probe=True)
            result=self.service.verification('a',begin['operation'],'probe',{})
        self.assertTrue(result['passed']);self.assertFalse(result['confirmed'])
        submitted=client.post.call_args
        self.assertEqual(submitted.args[0],'activities/prize/templatePrize/new')
        self.assertEqual(submitted.kwargs['encryption'],'wheel')
        self.assertEqual(submitted.kwargs['wheel_environment'],'net-age-python')
        extra=json.loads(submitted.args[1]['jsonExtParam'])
        self.assertEqual(extra,{'smsCode':'123456','productCode':'200000043066','touchCode':CHANNEL,'accessToken':'one-use-access'})
        self.assertEqual(self.service.pending['a'].access_token,'')
        self.assertFalse(any(c.args[0]=='user/smsCode'for c in client.post.call_args_list))

    def test_birthday_phone_probe_keeps_the_official_non_slider_interface(self):
        years=re.match(r'(\d+)年','13年')
        client=Mock(user={'provinceCode':'851'},channel=CHANNEL,last_wheel_exchange={})
        client.inspect.return_value={'rows':self.client.benefits(5,years,True,[],[])}
        client.orders.return_value=[]
        def response(path,payload,**options):
            if path=='activities/prize/templatePrize':
                options['before_submit']()
                raise CocError('验证码输入有误（10）','10')
            if path.startswith('user/auth/') or path=='user/smsCode':raise AssertionError('phone probe must not request slider or SMS')
            return {}
        client.post.side_effect=response
        with patch('features.net_age.service.NetAgeClient',return_value=client):
            begin=self.service.begin('a','birthday','phone',probe=True)
            result=self.service.verification('a',begin['operation'],'probe',{})
        self.assertFalse(begin['needs_slider']);self.assertTrue(result['passed'])
        submitted=client.post.call_args
        self.assertEqual(submitted.args[0],'activities/prize/templatePrize')
        self.assertEqual(submitted.kwargs['encryption'],'aurora')
        self.assertEqual(json.loads(submitted.args[1]['jsonExtParam']),
            {'smsCode':'123456','productCode':'130000006303','touchCode':CHANNEL})
        self.assertIsNone(self.service.submissions.get('a',{}).get('birthday'))

    def test_birthday_probe_blocks_any_official_annual_order_before_verifying(self):
        years=re.match(r'(\d+)年','13年')
        client=Mock(user={'provinceCode':'851'},channel=CHANNEL)
        client.inspect.return_value={'rows':self.client.benefits(5,years,True,[],[])}
        client.orders.return_value=[{'activity_id':BIRTH_IDS[0],'skuCode':'unrecognized-product','createTime':'2026-11-18 10:00:00'}]
        with patch('features.net_age.service.NetAgeClient',return_value=client):
            begin=self.service.begin('a','birthday','cinema',probe=True)
            with self.assertRaises(CocError) as result:
                self.service.verification('a',begin['operation'],'probe',{})
        self.assertEqual(result.exception.code,'NOT_AVAILABLE')
        client.post.assert_called_once_with('activities/growth/verify/status',{},recover=True)

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
        self.assertFalse(row['retry_available'])
        self.assertIn('birthday', self.service.submissions['a'])

    def test_birthday_order_with_an_unrecognized_product_blocks_another_attempt(self):
        years = re.match(r'(\d+)年', '13年')
        order = {'activity_id': BIRTH_IDS[0], 'skuCode': 'new-code', 'createTime': '2026-11-18 18:20:10'}
        client = Mock(surprise_chances=Mock(return_value=0))
        client.inspect.side_effect = lambda *_, **__: {'rows':self.client.benefits(5, years, True, [], [order])}
        self.service.mark('a', 'birthday', {'choice':'cinema', 'period':'2026', 'response_code':'10'})

        row = self.service.status('a', client)['rows']['birthday']
        self.assertIn('今年已有官方生日礼订单', row['description'])
        self.assertFalse(row['retry_available'])
        with self.assertRaises(CocError) as blocked:
            self.service._offer(client, 'a', 'birthday', 'cinema', self.now, retry=True)
        self.assertEqual(blocked.exception.code, 'NOT_AVAILABLE')
        client.post.assert_not_called()

    def test_birthday_cinema_refreshes_slider_on_resend_and_keeps_failure_evidence(self):
        years = re.match(r'(\d+)年', '13年')
        client = Mock(user={'provinceCode':'851'}, surprise_chances=Mock(return_value=0))
        client.inspect.side_effect = lambda *_, **__: {'rows':self.client.benefits(5, years, True, [], [])}
        client.orders.return_value = []
        failure = CocError('领取失败！（10）', '10')
        failure.trace_id = 'birthday-trace-fixture'

        def reply(path, _, **options):
            if path == 'user/auth/getSliderImg':
                return {'ground':'fixture', 'patch':'fixture', 'authToken':'challenge-fixture', 'ordinate':20}
            if path == 'user/auth/checkSliderImg':
                return {'accessToken':'slider-fixture'}
            if path == 'user/smsCode':
                options['before_submit']()
            if path == 'activities/prize/templatePrize/new':
                options['before_submit']()
                raise failure
            return {}

        client.post.side_effect = reply
        with patch('features.net_age.service.NetAgeClient', return_value=client), patch('features.net_age.service.time.monotonic', return_value=1000) as clock:
            preparation = self.service.begin('a', 'birthday', 'cinema')
            operation = preparation['operation']
            self.assertTrue(preparation['needs_slider'])
            self.service.verification('a', operation, 'challenge', {})
            self.service.verification('a', operation, 'slider', {'position':20})
            self.service.verification('a', operation, 'sms', {})
            clock.return_value = 1060
            with self.assertRaises(CocError) as stale_slider:
                self.service.verification('a', operation, 'sms', {})
            self.assertEqual(stale_slider.exception.code, 'SLIDER_REQUIRED')
            self.service.verification('a', operation, 'challenge', {})
            self.service.verification('a', operation, 'slider', {'position':20})
            with self.assertRaises(CocError) as stale_code:
                self.service.verification('a', operation, 'submit', {'code':'123456'})
            self.assertEqual(stale_code.exception.code, 'SMS_REQUIRED')
            self.service.verification('a', operation, 'sms', {})
            result = self.service.verification('a', operation, 'submit', {'code':'654321'})

        self.assertFalse(result['confirmed'])
        self.assertEqual(result['code'], '10')
        self.assertIn('birthday-trace-fixture', result['message'])
        client.orders.assert_not_called()
        row = self.service.status('a', client)['rows']['birthday']
        self.assertEqual(row['status'], '上次领取失败')
        self.assertNotIn('加密参数',row['description'])
        self.assertIn('birthday-trace-fixture', row['description'])
        self.assertTrue(row['recheck'])
        self.assertTrue(row['retry_available'])
        self.assertNotIn('retry_offer_ids', row)
        self.assertFalse(row['available'])

    def test_birthday_failure_keeps_both_choices(self):
        years = re.match(r'(\d+)年', '13年')
        client = Mock(surprise_chances=Mock(return_value=0))
        client.inspect.side_effect = lambda *_, **__: {'rows':self.client.benefits(5, years, True, [], [])}
        self.service.mark('a', 'birthday', {
            'choice':'cinema', 'period':'2026', 'response_code':'10', 'wasm_profile':WASM_PROFILE,
        })

        row = self.service.status('a', client)['rows']['birthday']

        self.assertTrue(row['retry_available'])
        self.assertNotIn('retry_offer_ids', row)
        self.assertNotIn('移动环境修复前', row['description'])
        with patch('features.net_age.service.NetAgeClient', return_value=client):
            cinema = self.service.begin('a', 'birthday', 'cinema', retry=True)
        self.assertTrue(cinema['needs_slider'])

    def test_old_birthday_failure_allows_explicit_retry(self):
        years = re.match(r'(\d+)年', '13年')
        client = Mock(surprise_chances=Mock(return_value=0))
        client.inspect.side_effect = lambda *_, **__: {'rows':self.client.benefits(5, years, True, [], [])}
        self.service.mark('a', 'birthday', {
            'choice':'cinema', 'period':'2026', 'response_code':'10', 'trace_id':'old-trace',
        })

        row = self.service.status('a', client)['rows']['birthday']
        self.assertTrue(row['retry_available'])
        self.assertIn('old-trace', row['description'])
        with patch('features.net_age.service.NetAgeClient', return_value=client):
            preparation = self.service.begin('a', 'birthday', 'cinema', retry=True)
        self.assertTrue(preparation['needs_slider'])
        client.post.assert_called_once_with('activities/growth/verify/status', {}, recover=True)

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
        with patch('features.net_age.service.requests', transport):
            self.assertEqual(_config(), {'configContent_27': '931'})
        session.get.assert_called_once()

    def test_wheel_environment_uses_official_storage_and_cookie_visibility(self):
        self.client.snapshot = {'phone':'13800138000'}
        self.client.user = {'token':'official-bound-token', 'userId':'official-user-id'}
        self.client.client = Mock(headers={'User-Agent':'official-agent'}, cookies=requests.Cookies())
        for index in range(1, 8):
            self.client.client.cookies.jar.set_cookie(Cookie(
                0, f'visible{index}', f'value{index}', None, False, 'dev.coc.10086.cn', True, False,
                '/', True, False, None, True, None, None, {'http_only':'false'}))
        self.client.client.cookies.jar.set_cookie(Cookie(
            0, 'hidden', 'secret', None, False, 'dev.coc.10086.cn', True, False,
            '/', True, False, None, True, None, None, {'http_only':'true'}))

        environment = self.client.environment()

        self.assertEqual(environment['local_storage'], {
            'pageId':PAGE_ID, 'channelId':'P00000001090', 'sellerId':'undefined',
            'boundToken':'official-bound-token', 'userId':'official-user-id',
        })
        self.assertEqual(list(environment['cookie']), [f'visible{index}' for index in range(1, 6)])
        self.assertNotIn('13800138000', json.dumps(environment, ensure_ascii=False))
        self.assertNotIn('hidden', environment['cookie'])
        self.assertEqual(environment['runtime_type'], 'browser')
        self.assertEqual(environment['platform'], 'Linux armv8l')
        self.assertEqual(environment['screen_width'], 390)
        self.assertEqual(environment['max_touch_points'], 1)
        self.assertEqual(self.client.request_headers['Sec-CH-UA-Mobile'], '?1')
        self.assertIn('Android WebView', self.client.request_headers['Sec-CH-UA'])

        truthful = self.client.environment('truthful')
        self.assertEqual(truthful['runtime_type'], 'Unknown non-browser environment')
        self.assertIn('python_version', truthful)
        self.assertNotIn('screen_width', truthful)

    def test_net_age_entry_uses_the_current_official_app_channel(self):
        self.assertEqual(CHANNEL, 'P00000001090')
        self.assertEqual(ENTRY, 'https://dev.coc.10086.cn/coc/web6/ageFeedbackV2/'
                         '?pageId=1879720710683729920&channelId=P00000001090')
        self.assertTrue(net_age_wheel.MOBILE_APP_UA.endswith(' leadeon/12.5.6/CMCCIT'))

    def test_net_age_uses_an_activity_session_separate_from_seven_day(self):
        snapshot = {'id':'account-a', 'phone':'13800138000', 'cookie':'JSESSIONID=test; UID=test'}
        cache = Mock()
        cache.connect.return_value = {'user':{'userId':'fixture'}}
        client = NetAgeClient(lambda:snapshot, cache, None)
        client.session_factory = lambda:Mock(headers={}, cookies=requests.Cookies(), close=Mock())

        client.connect()

        self.assertEqual(client.credential_scope, 'seven-day')
        self.assertEqual(cache.connect.call_args.args[0], 'seven-day')
        client.close()

    def test_flow_uses_official_app_ydrz_login_and_reuses_it_after_app_revision_changes(self):
        snapshot = {'id':'account-a', 'phone':'13800138000', 'cookie':'JSESSIONID=fixture; UID=fixture',
                    'credential_revision':1}
        cache = CredentialCache()
        sessions = []

        def session():
            result = Mock(headers={}, cookies=requests.Cookies(), close=Mock())
            sessions.append(result)
            return result

        client = NetAgeClient(lambda:snapshot, cache, None, app_auth=True)
        client.session_factory = session
        with patch('activities.coc_client.AppLoginSession.from_credentials') as factory, \
                patch.object(client, 'post', return_value={'userId':'official-user'}) as post:
            factory.return_value.call.return_value = (200, {
                'retCode':'000000', 'rspBody':json.dumps({'token':'official-app-ticket','newUsessionId':'fixture'}),
            }, {})
            client.connect()
            client.close()
            snapshot['credential_revision'] = 2
            client.connect()

        factory.return_value.call.assert_called_once_with(
            '/leadeon-abilityopen-biz/BN/obtainToken/getBigNetToken',
            {'sourceId':'056002', 'url':APP_ENTRY, 'cellNum':'13800138000'})
        post.assert_called_once_with('user/loginCheck', {'verifyType':18,'ext':'official-app-ticket'}, encryption=None)
        self.assertEqual(client.user, {'userId':'official-user'})
        self.assertEqual(client.credential_scope, 'seven-day')
        client.close()

    def test_empty_ydrz_ticket_stops_before_activity_login(self):
        snapshot = {'id':'account-a', 'phone':'13800138000', 'cookie':'JSESSIONID=fixture'}
        client = NetAgeClient(lambda:snapshot, None, None, app_auth=True)
        client.session_factory = lambda:Mock(headers={}, cookies=requests.Cookies(), close=Mock())
        with patch('activities.coc_client.AppLoginSession.from_credentials') as factory, \
                patch.object(client, 'post') as post:
            factory.return_value.call.return_value = (200, {'retCode':'000000','rspBody':{'token':''}}, {})
            with self.assertRaises(CocError) as failure:
                client.connect()
        self.assertEqual(failure.exception.code, 'APP_TOKEN_FORMAT')
        post.assert_not_called()
        client.close()

    def test_member_preparation_reuses_activity_identity_and_prepares_each_business_session_once(self):
        snapshot={'id':'account-a','phone':'13800138000','cookie':'JSESSIONID=fixture; UID=fixture','credential_revision':1}
        cache=CredentialCache()
        client=NetAgeClient(lambda:snapshot,cache,None,app_auth=True,member=True)
        sessions=[]
        def session():
            value=Mock(headers={},cookies=requests.Cookies(),close=Mock())
            value.get.return_value.status_code=200
            sessions.append(value)
            return value
        client.session_factory=session
        def official(path,*args,**kwargs):
            return {'userId':'official-user'} if path=='user/loginCheck' else False
        with patch('activities.coc_client.AppLoginSession.from_credentials') as factory, \
                patch.object(client,'post',side_effect=official) as post:
            factory.return_value.call.return_value=(200,{'retCode':'000000','rspBody':{'token':'official-app-ticket'}},{})
            client.connect();client.prepare_wheel();client.prepare_wheel();client.close()
            snapshot['credential_revision']=2
            client.connect();client.prepare_wheel();client.close()
        factory.return_value.call.assert_called_once()
        paths=[call.args[0] for call in post.call_args_list]
        self.assertEqual(paths.count('user/loginCheck'),1)
        self.assertEqual(paths.count('activities/growth/query/evaluate'),2)
        self.assertEqual([s.get.call_count for s in sessions],[1,1])
        self.assertTrue(all('Chrome/143.' in s.headers['User-Agent'] for s in sessions))

    def test_member_device_cookies_survive_restart_without_regeneration_and_remain_account_isolated(self):
        saved={}
        store=Mock()
        store.load_activity_data.side_effect=lambda key:saved.get(key)
        store.save_activity_data.side_effect=lambda key,value:saved.__setitem__(key,value)
        service=NetAge(lambda _:None,lambda:[],None,store,None)
        cache=Mock()
        cache.connect.return_value={'user':{'userId':'official-user'}}
        cache.load.return_value=None
        snapshot={'id':'a','phone':'13800138000','cookie':'JSESSIONID=fixture','credential_revision':1}
        def client(owner,account):
            value=NetAgeClient(lambda:snapshot,cache,None,app_auth=True,member=True,
                device_cookies=owner.device_cookies.get(account,[]),
                save_device_cookies=lambda cookies:owner.save_device_cookies(account,cookies))
            value.session_factory=lambda:Mock(headers={},cookies=requests.Cookies(),close=Mock(),
                get=Mock(return_value=Mock(status_code=200)),post=Mock(return_value=Mock(
                    status_code=200,json=Mock(return_value={'code':'0','data':False}),headers={})))
            return value
        first=client(service,'a');first.connect();first.prepare_wheel();first.close()
        original={c['name']:c['value'] for c in service.device_cookies['a']}
        self.assertEqual(len(original),7)
        self.assertNotIn('User-Token',original)
        self.assertNotIn('RiskToken',original)
        restarted=NetAge(lambda _:None,lambda:[],None,store,None)
        snapshot['credential_revision']=2
        again=client(restarted,'a')
        with patch('activities.coc_client.secrets.choice',side_effect=AssertionError('device ID regenerated')), \
                patch('activities.coc_client.uuid4',side_effect=AssertionError('session ID regenerated')):
            again.connect();again.prepare_wheel();again.prepare_wheel();again.close()
        current={c['name']:c['value'] for c in restarted.device_cookies['a']}
        for name in original:
            if not name.endswith('_sequence_ids'):
                self.assertEqual(current[name],original[name])
        self.assertNotEqual(current['9e4e5fa7244c6b6e_gdp_sequence_ids'],original['9e4e5fa7244c6b6e_gdp_sequence_ids'])
        self.assertEqual(store.save_activity_data.call_count,2)
        other=client(restarted,'b');other.connect();other.prepare_wheel();other.close()
        self.assertNotEqual(restarted.device_cookies['a'],restarted.device_cookies['b'])
        self.assertEqual({c['name']:c['value'] for c in restarted.device_cookies['a']},current)

    def test_activity_connect_seeds_one_coherent_growingio_page_session(self):
        snapshot = {'phone':'13800138000', 'cookie':'JSESSIONID=test; UID=test'}
        client = NetAgeClient(lambda: snapshot, None, None)
        session = Mock(headers={}, cookies=requests.Cookies(), close=Mock())
        client.session_factory = lambda: session

        def authenticated():
            client.user = {'userId':'fixture'}

        with patch.object(client, '_authenticate', side_effect=authenticated):
            client.connect()
        clean = client.environment('mobile_clean')
        self.assertEqual(clean['runtime_type'], 'browser')
        self.assertFalse(any('gdp' in cookie.name for cookie in session.cookies.jar))
        client.environment()

        cookies = {cookie.name: cookie.value for cookie in session.cookies.jar}
        self.assertRegex(cookies['gdp_user_id'], r'^[^,]+%2C[^,]+%2C[^,]+%2C[^,]+%2C[^,]+$')
        self.assertEqual(cookies['9e4e5fa7244c6b6e_gdp_session_id'],
                         cookies['9e4e5fa7244c6b6e_gdp_session_id_sent'])
        self.assertIn('%22PAGE%22%3A10', cookies['9e4e5fa7244c6b6e_gdp_sequence_ids'])
        client.close()

    def test_net_age_does_not_read_device_state_from_another_activity(self):
        storage = Mock()
        storage.load_activity_data.return_value = {}
        service = NetAge(lambda _: {}, lambda: [], None, storage, None, now=lambda:self.now)
        storage.load_activity_data.reset_mock()
        client = Mock()
        client.connect.return_value = client
        client.client.cookies = requests.Cookies()
        with patch('features.net_age.service.NetAgeClient', return_value=client):
            with service.connected('a') as connected:
                self.assertIs(connected, client)
                self.assertIsNone(connected.client.cookies.get('_zw_kvani5r'))
        storage.load_activity_data.assert_not_called()

    def test_badge_counts_only_two_confirmed_available_groups_and_excludes_pending(self):
        years = re.match(r'(\d+)年', '13年')
        client = Mock(surprise_chances=Mock(return_value=4))
        rows = self.client.benefits(5, years, True, [], [])
        self.assertTrue(rows['flow']['available'] and rows['ai']['available'])
        client.inspect.return_value = {'rows': rows}
        self.assertEqual(self.service.status('a', client)['available_count'], 2)
        self.assertEqual(self.service.available_counts({'a','b'}), {'a':2})

        self.service.mark('a','birthday',{'choice':'phone','period':'2026'})
        self.assertEqual(self.service.available_counts({'a'}), {'a':1})
        self.now = self.now.replace(month=12, day=1)
        self.assertEqual(self.service.available_counts({'a'}), {})

    def test_badge_scan_checks_other_accounts_once_per_day_without_surprise_or_claims(self):
        years = re.match(r'(\d+)年', '13年')
        client = Mock()
        rows = self.client.benefits(5, years, False, [], [])
        client.inspect.return_value = {'rows': {group:rows[group] for group in ('member','birthday')}}
        self.service.connected = Mock(side_effect=lambda _:nullcontext(client))

        self.assertEqual(self.service.check_availability('a'), 1)
        self.assertEqual(self.service.check_availability('a'), 1)
        client.inspect.assert_called_once_with(self.now, badges_only=True)
        self.now += timedelta(days=1)
        self.assertEqual(self.service.check_availability('a'), 1)
        self.assertEqual(client.inspect.call_count, 2)
        client.surprise_chances.assert_not_called()
        client.post.assert_not_called()

    def test_confirmed_member_claim_immediately_decreases_the_cached_badge(self):
        years = re.match(r'(\d+)年', '13年')
        rows = self.client.benefits(5, years, True, [], [])
        self.service._record_availability('a', self.now, rows)
        offer = next(offer for offer in rows['member']['offers'] if offer['id']=='iqiyi')
        client = Mock(user={'provinceCode':'100'})
        client.post.side_effect = lambda *_, before_submit, **__: before_submit()
        client.orders.return_value = [{'activity_id':MEMBER_ID, 'skuCode':offer['products'][0],
                                      'createTime':'2026-11-18 17:00:00'}]
        self.assertTrue(self.service._submit(client,'a','member',offer,'2026-11',code='123456')['confirmed'])
        self.assertEqual(self.service.available_counts({'a'}), {'a':1})

    def test_no_monthly_order_permits_explicit_repreparation(self):
        client = Mock(user={'provinceCode':'100'}, surprise_chances=Mock(return_value=0))
        client.inspect.side_effect = lambda *_args, **_kwargs: {'rows':self.client.benefits(5, None, False, [], [])}
        self.service.mark('a','flow',{'choice':'monthly','period':'2026-11'})
        self.service.mark('a','member',{'choice':'bilibili','period':'2026-11'})
        rows = self.service.status('a', client)['rows']
        self.assertEqual(rows['flow']['status'], '结果待确认')
        self.assertTrue(rows['flow']['retry_available'])
        self.assertIn('本月暂未查到领取记录', rows['flow']['description'])
        self.assertEqual(rows['member']['status'], '结果待确认')
        self.assertTrue(rows['member']['recheck'])
        self.assertFalse(rows['member']['available'])
        self.assertTrue(rows['member']['retry_available'])

        with patch('features.net_age.service.NetAgeClient', return_value=client):
            with self.assertRaises(CocError) as blocked:
                self.service.begin('a','member','bilibili')
            self.assertEqual(blocked.exception.code, 'SUBMISSION_PENDING')
            client.post.assert_not_called()
            prepared = self.service.begin('a','member','bilibili',retry=True)
        self.assertTrue(prepared['needs_slider'])
        self.assertTrue(self.service.pending['a'].retry)
        client.post.assert_called_once_with('activities/growth/verify/status', {}, recover=True)
        self.assertIn('member', self.service.submissions['a'])

    def test_monthly_flow_with_unknown_product_order_cannot_be_claimed_again(self):
        receipt = {'activity_id':FLOW_IDS[2], 'skuCode':'unknown-flow-sku', 'createTime':'2026-11-18 10:00:00'}
        client = Mock(surprise_chances=Mock(return_value=0))
        client.inspect.side_effect = lambda *_args, **_kwargs: {'rows':self.client.benefits(5, None, False, [receipt], [])}

        row = self.service.status('a', client)['rows']['flow']
        self.assertEqual(row['status'], '结果待确认')
        self.assertTrue(row['recheck'])
        self.assertFalse(row['available'])
        self.assertFalse(row['offers'][0]['received'])
        self.service.mark('a', 'flow', {'choice':'monthly', 'period':'2026-11', 'response_code':'10'})
        with self.assertRaises(CocError) as blocked:
            self.service._offer(client, 'a', 'flow', 'monthly', self.now, retry=True)
        self.assertEqual(blocked.exception.code, 'NOT_AVAILABLE')
        client.post.assert_not_called()

    def test_monthly_flow_displays_the_received_tier_and_ignores_prior_month(self):
        prior = {'activity_id':FLOW_IDS[2], 'skuCode':FLOW_CODES[2], 'createTime':'2026-10-30 10:00:00'}
        current = {'activity_id':FLOW_IDS[0], 'skuCode':FLOW_CODES[0], 'createTime':'2026-11-01 10:00:00'}
        profile = {'userChinaMobileage':'3年'}
        growth = {'level':'V5'}
        with patch.object(self.client, 'post', side_effect=[profile, growth, profile, growth]), \
                patch.object(self.client, 'orders', side_effect=[[prior, current], [prior]]):
            claimed = self.client.inspect(self.now)['rows']['flow']
            available = self.client.inspect(self.now)['rows']['flow']

        self.assertEqual(claimed['status'], '已领取')
        self.assertEqual(claimed['offers'][0]['name'], '1GB 流量月包券')
        self.assertTrue(available['available'])
        self.assertEqual(available['offers'][0]['name'], '3GB 流量月包券')

    def test_monthly_flow_shows_recorded_rejection_until_an_order_confirms_receipt(self):
        self.service.mark('a', 'flow', {'choice':'monthly', 'period':'2026-11', 'response_code':'10'})
        receipt = {'activity_id':FLOW_IDS[2], 'skuCode':FLOW_CODES[2], 'createTime':'2026-11-18 10:00:00'}
        client = Mock(user={'provinceCode':'100'}, surprise_chances=Mock(return_value=0))
        client.inspect.side_effect = [
            {'rows':self.client.benefits(5, None, False, [], [])},
            {'rows':self.client.benefits(5, None, False, [], [])},
            {'rows':self.client.benefits(5, None, False, [], [])},
            {'rows':self.client.benefits(5, None, False, [receipt], [])},
        ]

        rejected = self.service.status('a', client)['rows']['flow']
        self.assertEqual(rejected['status'], '本次领取失败')
        self.assertEqual(rejected['description'], '')
        self.assertTrue(rejected['recheck'])
        self.assertTrue(rejected['retry_available'])
        with patch('features.net_age.service.NetAgeClient', return_value=client), patch('features.net_age.service._config', return_value={}):
            with self.assertRaises(CocError) as blocked:
                self.service.begin('a', 'flow', 'monthly')
            self.assertEqual(blocked.exception.code, 'SUBMISSION_PENDING')
            client.post.assert_not_called()
            prepared = self.service.begin('a', 'flow', 'monthly', retry=True)
        with self.assertRaises(CocError) as missing_sms:
            self.service.verification('a', prepared['operation'], 'submit', {'code':'123456'})
        self.assertEqual(missing_sms.exception.code, 'SMS_REQUIRED')
        client.post.assert_called_once_with('activities/growth/verify/status', {}, recover=True)
        claimed = self.service.status('a', client)['rows']['flow']
        self.assertEqual(claimed['status'], '已领取')
        self.assertNotIn('flow', self.service.submissions['a'])

    def test_member_order_appearing_during_verification_blocks_submission(self):
        receipt = {'activity_id':MEMBER_ID, 'skuCode':'200000034558', 'createTime':'2026-11-18 10:00:00'}
        client = Mock()
        client.orders.return_value=[receipt]
        client.inspect.side_effect = [
            {'rows':self.client.benefits(5, None, False, [], [])},
            {'rows':self.client.benefits(5, None, False, [receipt], [])},
        ]
        self.service.mark('a', 'member', {'choice':'bilibili', 'period':'2026-11'})
        with patch('features.net_age.service.NetAgeClient', return_value=client):
            preparation = self.service.begin('a', 'member', 'bilibili', retry=True)
        operation = self.service.pending['a']
        operation.access_token = 'verified'
        operation.sms_sent_at = 1
        operation.sms_attempted_for_slider = True

        with self.assertRaises(CocError) as blocked:
            self.service.verification('a', preparation['operation'], 'submit', {'code':'123456'})

        self.assertEqual(blocked.exception.code, 'NOT_AVAILABLE')
        client.inspect.assert_called_once_with(self.now,recover=True)
        client.orders.assert_called_once_with(MEMBER_HISTORY_IDS,recover=False)
        client.post.assert_called_once_with('activities/growth/verify/status', {}, recover=True)

    def test_old_member_crypto_failure_can_prepare_new_python_verification_after_order_check(self):
        client = Mock()
        client.inspect.side_effect = lambda *_args, **_kwargs: {'rows':self.client.benefits(5,None,False,[],[])}
        self.service.mark('a','member',{'choice':'youku','period':'2026-11',
            'response_code':'10','wasm_profile':'node-webview-v5','trace_id':'old-trace'})
        row = self.service.status('a',client)['rows']['member']
        self.assertTrue(row['retry_available'])
        with patch('features.net_age.service.NetAgeClient',return_value=client) as factory:
            prepared = self.service.begin('a','member','youku',retry=True)
        self.assertTrue(factory.call_args.kwargs['app_auth'])
        self.assertTrue(prepared['needs_slider'])
        client.prepare_wheel.assert_called_once_with()
        self.assertEqual(self.service.pending['a'].access_token,'')
        self.service.verification('a',prepared['operation'],'cancel',{})

    def test_existing_monthly_order_blocks_retry_even_with_an_unknown_sku(self):
        receipt = {'activity_id':MEMBER_ID, 'skuCode':'unknown-provider-sku', 'createTime':'2026-11-18 23:00:00'}
        client = Mock()
        client.inspect.side_effect = lambda *_args, **_kwargs: {'rows':self.client.benefits(5, None, False, [receipt], [])}
        row = self.service.status('a', client)['rows']['member']
        self.assertFalse(row['available'])
        self.assertTrue(row['recheck'])
        self.assertIn('发现本月官方订单', row['description'])
        with self.assertRaises(CocError) as blocked:
            self.service._offer(client,'a','member','bilibili',self.now)
        self.assertEqual(blocked.exception.code, 'NOT_AVAILABLE')
        client.post.assert_not_called()

    def test_member_order_from_previous_month_is_visible_while_current_result_is_pending(self):
        previous = [
            {'activity_id': MEMBER_ID, 'skuCode': '200000034558', 'skuName': '哔哩哔哩月度大会员',
             'createTime': '2026-10-29 16:29:35'},
            {'activity_id': MEMBER_ID, 'skuCode': '200000034558', 'createTime': '2026-09-02 14:35:10'},
        ]
        with patch.object(self.client, 'post', side_effect=[{}, {'level':'V5'}]), \
                patch.object(self.client, 'orders', return_value=previous) as orders, \
                patch.object(self.client, 'surprise_chances', return_value=0):
            self.service.mark('a', 'member', {'choice':'bilibili', 'period':'2026-11'})
            result = self.service.status('a', self.client)

        row = result['rows']['member']
        self.assertEqual(row['last_received'], {'name':'哔哩哔哩月度大会员', 'date':'2026-10-29'})
        self.assertEqual(row['status'], '结果待确认')
        self.assertFalse(any(offer['received'] for offer in row['offers']))
        self.assertEqual(result['available_count'], 0)
        self.assertIn(MEMBER_HISTORY_IDS[1], orders.call_args.args[0])

    def test_current_bilibili_orders_recognize_official_legacy_codes(self):
        for field, code in (('skuCode','2025999800006825'), ('prizeId',2026483731145236480)):
            with self.subTest(field=field):
                receipt = {'activity_id': MEMBER_HISTORY_IDS[1], field: code, 'createTime': '2026-11-18 15:10:00'}
                client = Mock(surprise_chances=Mock(return_value=0))
                client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [receipt], [])}
                self.service.mark('a', 'member', {'choice':'bilibili', 'period':'2026-11'})

                result = self.service.status('a', client)

                row = result['rows']['member']
                self.assertEqual(row['status'], '已领取')
                self.assertEqual([offer['id'] for offer in row['offers'] if offer['received']], ['bilibili'])
                self.assertNotIn('member', self.service.submissions['a'])
                self.assertEqual(result['available_count'], 0)

    def test_member_receipt_or_duplicate_response_survives_a_missing_order(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                rows = self.client.benefits(5, None, False, [], [])
                offer = next(item for item in rows['member']['offers'] if item['id']=='bilibili')
                client = Mock(user={'provinceCode':'100'}, surprise_chances=Mock(return_value=0))

                def submit(*_, before_submit, **__):
                    before_submit()
                    if duplicate:
                        raise CocError('请勿重复领取', MEMBER_ALREADY_CLAIMED)
                    return {'skuCode': '2025999800006825'}

                client.post.side_effect = submit
                client.orders.side_effect = CocError('订单查询失败', 'UPSTREAM_NETWORK')
                client.inspect.return_value = {'rows':rows}

                self.assertFalse(self.service._submit(client,'a','member',offer,'2026-11',code='123456')['confirmed'])
                row = self.service.status('a', client)['rows']['member']

                self.assertEqual(row['status'], '结果待确认' if duplicate else '官方已受理')
                self.assertIn('请勿重复领取' if duplicate else '哔哩哔哩', row['description'])
                self.assertTrue(row['recheck'])
                self.assertFalse(row['available'])
                self.assertFalse(row.get('retry_available'))
                client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [], [])}
                with self.assertRaises(CocError) as blocked:
                    self.service._offer(client,'a','member','bilibili',self.now,retry=True)
                self.assertEqual(blocked.exception.code, 'SUBMISSION_PENDING')

    def test_confirmed_member_order_resolves_a_different_pending_choice(self):
        receipt = {'activity_id':MEMBER_ID, 'skuCode':'200000034571', 'createTime':'2026-11-18 23:00:00'}
        client = Mock()
        client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [receipt], [])}
        self.service.mark('a','member',{'choice':'bilibili','period':'2026-11'})
        row = self.service.status('a', client)['rows']['member']
        self.assertEqual(row['status'], '已领取')
        self.assertEqual([offer['id'] for offer in row['offers'] if offer['received']], ['qq-music'])
        self.assertNotIn('member', self.service.submissions['a'])

    def test_pre_submit_failure_does_not_clear_an_earlier_pending_attempt(self):
        previous = {'choice':'monthly','period':'2026-11'}
        self.service.mark('a','flow',previous)
        client = Mock(user={'provinceCode':'100'})
        client.post.side_effect = CocError('官方设备认证暂未通过','GDT_REJECTED')
        offer = self.client.benefits(5, None, False, [], [])['flow']['offers'][0]

        with self.assertRaises(CocError):
            self.service._submit(client,'a','flow',offer,'2026-11',code='123456')

        self.assertEqual(self.service.submissions['a']['flow'],previous)
        client.orders.assert_not_called()

    def test_device_rejection_retains_its_trace_and_stops_before_claim(self):
        response = Mock(status_code=200, headers={'trace-id':'device-check-fixture'})
        response.json.return_value = {'code':'1', 'message':'环境信息解析失败'}
        self.client.client = Mock(headers={'User-Agent':'fixture'}, cookies=requests.Cookies())
        self.client.client.post.return_value = response
        before_submit = Mock()

        with patch.object(self.client, 'current'), self.assertRaises(CocError) as rejected:
            self.client.post('activities/prize/templatePrize/new', {}, encryption='wheel',
                             mutation=True, before_submit=before_submit)

        self.assertEqual(rejected.exception.code, 'GDT_REJECTED')
        self.assertEqual(rejected.exception.trace_id, 'device-check-fixture')
        self.assertIn('追踪号 device-check-fixture', str(rejected.exception))
        before_submit.assert_not_called()
        self.client.client.post.assert_called_once()
        self.assertTrue(self.client.client.post.call_args.args[0].endswith('/wasmrc/gDT'))

    def test_surprise_checks_actual_quota_and_prizes_reusing_the_scoped_session(self):
        snapshot = {'id':'a','phone':'13800000000'}
        self.service.credentials = lambda _:snapshot
        self.service.cache = CredentialCache()
        activity = Mock(user={'maskedMobile':'138****0000','token':'coc-token'})
        activity.post.side_effect = [{}, {'cmPassPortToken':'scoped-ticket'}]
        self.service.connected = Mock(side_effect=lambda _:nullcontext(activity))

        def response(code, data):
            value = Mock(status_code=200)
            value.json.return_value = {'code':code, 'data':data}
            return value

        def session(replies):
            client = MagicMock()
            client.__enter__.return_value = client
            client.headers = {}
            client.cookies = requests.Cookies()
            client.post.side_effect = replies
            return client

        count = response('200', 0)
        prizes = response('200', {'total':2,'list':[
            {'salesUnitId':10,'showName':'测试券','createTime':'2026-11-18'},
            {'salesUnitId':-1,'showName':'未中奖'},
        ]})
        first = session([response('0',{'mobileMask':'138****0000','token':'market-token'}),count,prizes])
        second = session([count,prizes])
        expected = {'remaining':0, 'prizes':[{'name':'测试券','date':'2026-11-18'}]}
        with patch('features.net_age.service.requests.Session', side_effect=[first,second]):
            self.assertEqual(self.service.surprise_details('a'), expected)
            self.assertEqual(self.service.surprise_details('a'), expected)

        self.service.connected.assert_called_once_with('a')
        self.assertEqual(activity.post.call_count, 2)
        self.assertEqual(second.headers['phone'],'market-token')
        self.assertEqual([call.args[0].rsplit('/',1)[1] for call in second.post.call_args_list],
                         ['residueLotteryCount','lotteryList'])

    def test_definite_monthly_flow_failure_returns_without_querying_orders_again(self):
        offer = self.client.benefits(5, None, False, [], [])['flow']['offers'][0]
        client = Mock(channel='test-channel',
                      user={'provinceCode':'100', 'token':'test-account-token', 'userId':'test-account-id'})

        def rejected(*_, before_submit, **__):
            before_submit()
            error = CocError('领取失败！（10）', '10',
                             internal_message='测试商品状态异常，手机号13800138000，验证码123456，绑定test-account-id')
            error.trace_id = 'flow-trace-fixture'
            raise error

        client.post.side_effect = rejected
        result = self.service._submit(client, 'a', 'flow', offer, '2026-11', code='123456')
        self.assertEqual(result, {'message':'本次领取失败', 'confirmed':False, 'code':'10'})
        client.orders.assert_not_called()
        client.post.assert_called_once()
        record = self.service.submissions['a']['flow']
        self.assertEqual(record['response_code'], '10')
        self.assertEqual(record['trace_id'], 'flow-trace-fixture')
        self.assertEqual(record['business_request'], {
            'method': 'POST',
            'offer_name': '3GB 流量月包券',
            'activity_id': FLOW_IDS[2],
            'product_code': FLOW_CODES[2],
            'channel_id': 'test-channel',
            'province_code': '100',
        })
        self.assertIn('测试商品状态异常', record['response_detail'])
        for sensitive in ('13800138000', '123456', 'test-account-id'):
            self.assertNotIn(sensitive, record['response_detail'])

    def test_monthly_flow_busy_is_not_a_claim_and_requires_explicit_retry(self):
        offer = self.client.benefits(5, None, False, [], [])['flow']['offers'][0]
        receipt = {'activity_id':offer['activity'], 'skuCode':offer['sms_product'], 'createTime':'2026-11-19 20:48:13'}
        for code in ('10', 'other-provider-code'):
            with self.subTest(code=code):
                client = Mock(user={}, surprise_chances=Mock(return_value=0))

                def busy(*_, before_submit, **__):
                    before_submit()
                    raise CocError('领取失败', code, internal_message='活动太火爆了，请稍后再试；内部附加字段')

                client.post.side_effect = busy
                client.orders.return_value = []
                client.inspect.side_effect = [
                    {'rows':self.client.benefits(5, None, False, [], [])},
                    {'rows':self.client.benefits(5, None, False, [receipt], [])},
                ]
                result = self.service._submit(client, 'a', 'flow', offer, '2026-11', code='123456')
                self.assertFalse(result['confirmed'])
                self.assertIn('活动太火爆了，请稍后再试', result['message'])
                self.assertNotIn('内部附加字段', result['message'])
                pending = self.service.status('a', client)['rows']['flow']
                self.assertEqual(pending['status'], '活动繁忙')
                self.assertTrue(pending['retry_available'])
                self.assertTrue(pending['recheck'])
                self.assertFalse(pending['available'])
                claimed = self.service.status('a', client)['rows']['flow']
                self.assertEqual(claimed['status'], '已领取')
                self.assertNotIn('flow', self.service.submissions['a'])
                client.post.assert_called_once()

    def test_member_claim_failure_remains_visible_and_a_later_receipt_resolves_it(self):
        rows = self.client.benefits(5, None, False, [], [])
        offer = next(item for item in rows['member']['offers'] if item['id'] == 'bilibili')
        client = Mock(user={'provinceCode':'851'}, surprise_chances=Mock(return_value=0))

        def rejected(*_, before_submit, **__):
            before_submit()
            raise CocError('领取失败！（10）', '10')

        client.post.side_effect = rejected
        previous = {'activity_id': MEMBER_ID, 'skuCode': '200000034558', 'createTime': '2026-10-29 16:29:35'}
        client.orders.return_value = [previous]
        client.inspect.return_value = {'rows':rows}

        result = self.service._submit(client, 'a', 'member', offer, '2026-11', code='123456')
        path, payload = client.post.call_args.args
        self.assertEqual(path, 'activities/prize/templatePrize/new')
        self.assertEqual(client.post.call_args.kwargs['wheel_environment'], 'net-age-python')
        self.assertEqual(payload['jsonExtParam'], json.dumps({
            'activitySkuId':offer['sku'], 'touchCode':'P00000001090', 'smsCode':'123456',
            'productCode':offer['sms_product'], 'rightSkuId':offer['right'], 'accessToken':'',
        }, separators=(',', ':')))
        self.assertEqual(list(payload), ['lotteryId', 'jsonExtParam', 'provinceCode'])
        self.assertFalse(result['confirmed'])
        self.assertIn('官方加密参数校验失败（10）', result['message'])
        self.assertIn('本月暂未查到会员领取订单', result['message'])
        self.assertEqual(client.post.call_count, 1)
        self.assertEqual(self.service.submissions['a']['member']['response_code'], '10')
        row = self.service.status('a', client)['rows']['member']
        self.assertEqual(row['status'], '上次领取失败')
        self.assertIn('（10）', row['description'])
        self.assertFalse(row.get('retry_available'))
        self.assertTrue(row['recheck'])

        receipt = {**previous, 'createTime':'2026-11-18 10:00:00'}
        client.inspect.return_value = {'rows':self.client.benefits(5, None, False, [receipt], [])}
        self.assertEqual(self.service.status('a', client)['rows']['member']['status'], '已领取')
        self.assertNotIn('member', self.service.submissions['a'])

    def test_member_diagnostic_keeps_official_trace_without_request_secrets(self):
        client = self.client
        client.user = {'provinceCode':'851', 'userId':'test-user-id', 'token':'test-user-token'}
        rows = client.benefits(5, None, False, [], [])
        offer = next(item for item in rows['member']['offers'] if item['id'] == 'bilibili')
        response = Mock(status_code=200, headers={'trace-id':'test-trace-100'})
        response.json.return_value = {
            'code':'10', 'message':'领取失败',
            'messageInternal':'测试商品状态异常，手机号13800138000，验证码123456，凭证test-slider',
            'data':{'opaque':'private-response-fixture'},
        }
        client.client = Mock(headers={}, cookies=requests.Cookies())
        client.client.post.return_value = response
        with patch.object(client,'wheel_request',return_value=({},{})), patch.object(client,'current'), \
                patch.object(client,'orders',return_value=[]), patch.object(client,'inspect',return_value={'rows':rows}), \
                patch.object(client,'surprise_chances',return_value=0):
            result = self.service._submit(client, 'a', 'member', offer, '2026-11',
                                          code='123456', access='test-slider')
            row = self.service.status('a', client)['rows']['member']

        record = self.service.submissions['a']['member']
        self.assertEqual(record['trace_id'], 'test-trace-100')
        self.assertIn('测试商品状态异常', record['response_detail'])
        self.assertIn('测试商品状态异常', row['description'])
        self.assertIn('test-trace-100', row['description'])
        self.assertIn('测试商品状态异常', result['message'])
        for sensitive in ('13800138000', '123456', 'test-slider', 'private-response-fixture'):
            self.assertNotIn(sensitive, str(record))
            self.assertNotIn(sensitive, result['message'])
        client.client.post.assert_called_once()


if __name__ == '__main__':
    unittest.main()
