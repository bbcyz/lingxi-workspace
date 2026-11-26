"""Qualified target visits using existing COC and QQT identities."""
from contextlib import contextmanager
import hashlib
import time
from urllib.parse import urljoin, urlsplit

from features.checkin.client import CheckinError
from features.net_age.service import NetAgeClient
from activities.coc_client import COC_SCOPE, CocError
from recharge.cmcc import APP_UA
from workspace.http_transport import requests

GIFT_SCOPE = 'global-tone:privileges'
QQT = 'https://qqt.cmicrwx.cn/2016tyjf/xhmqqthy'
GIFT_ENTRY = QQT + '/res/wap/xdrNormal.html?sspId=75900'


class TargetVisits:
    def __init__(self, provider, cache, star, *, now=time.time):
        self.provider, self.cache, self.star, self.now = provider, cache, star, now
        self._gift_context = None
        self._gift_client = self._gift_profile = None

    def close(self):
        if self._gift_context is not None:
            self._gift_context.__exit__(None, None, None)
            self._gift_context = None

    @staticmethod
    def _tag(cookies):
        token = next((c['value'] for c in cookies if c['name'] == 'access_token'), '')
        return hashlib.sha256(token.encode()).hexdigest() if token else ''

    def v5(self):
        snapshot = self.provider()
        existing = any(self.cache.load(scope, snapshot) is not None for scope in (COC_SCOPE,'net-age','net-age-ydrz'))
        if snapshot.get('primary_status') == 'login_required' and not existing:
            raise CheckinError('网龄会话缺失，需先恢复共享 APP 登录。', 'APP_LOGIN_REQUIRED', 401)
        client = NetAgeClient(self.provider, self.cache, None)
        client.channel = 'P00000006088'
        try:
            client.connect()
            expected = snapshot['phone'][:3]+'****'+snapshot['phone'][-4:]
            if client.user.get('maskedMobile') != expected:
                raise CheckinError('网龄活动身份与当前账号不一致。', 'ACCOUNT_MISMATCH', 409)
            growth = client.post('activities/growth/query', {'touchCode': client.channel,
                'provinceCode': client.user.get('provinceCode', '')})
            level = growth['level']
            if level not in {'V1','V2','V3','V4','V5'}:
                raise CheckinError('官方尚未返回有效成长等级，暂不提交 V5 任务。', 'QUALIFICATION_UNCONFIRMED')
            if level != 'V5':
                raise CheckinError(f'官方当前成长等级为 {level or "未评定"}，该任务仅限 V5。', 'TASK_INELIGIBLE', 403)
            return {'level': level, 'message': '官方已核对网龄成长等级 V5。'}
        except CocError as error:
            raise CheckinError(str(error), error.code, error.status) from error
        finally:
            client.close()

    def ready(self):
        try:
            snapshot = self.provider()
        except CheckinError:
            return ''
        saved = self.cache.load(GIFT_SCOPE, snapshot) or self.star.sessions.load(snapshot['id'], snapshot['phone'])
        return self._tag(saved['cookies']) if saved else ''

    def _profile(self, client, snapshot):
        response = client.post(QQT+'/member/func/queryVipLevel.do',
            data={'sspId': '75900', 'tag': 'qqtindex'}, timeout=(10, 20), allow_redirects=False)
        try:
            result = response.json()
        except ValueError as error:
            raise CheckinError('全球通资料未完整返回，请稍后查询。', 'GIFT_FORMAT') from error
        if response.status_code != 200 or not isinstance(result, dict):
            raise CheckinError('全球通资料查询暂不可用。', 'GIFT_NETWORK')
        code = str(result.get('status'))
        if code == '-10':
            raise CheckinError('全球通身份已失效。', 'GIFT_LOGIN_REQUIRED', 401)
        if code == '-800':
            raise CheckinError('全球通服务请求频繁，暂停当日自动尝试。', 'RATE_LIMITED', 429)
        if code != '1':
            raise CheckinError(str(result.get('msg') or '全球通服务暂未允许查询'), code, 409)
        profile = result['data']
        expected = snapshot['phone']
        if profile['phone'] not in {expected, expected[:3]+'****'+expected[-4:]}:
            raise CheckinError('全球通资料账号不一致，已停止任务。', 'ACCOUNT_MISMATCH', 409)
        return profile

    def _busy(self, login_id):
        pending = self.star.sms.pending.get(login_id)
        return self.star.busy_login(login_id) or pending is not None and self.now()-pending['sent_at'] < 600

    @contextmanager
    def gift_session(self, drama):
        snapshot = drama.snapshot
        login_id, phone = snapshot['id'], snapshot['phone']
        with requests.Session(impersonate='chrome', headers={'User-Agent': APP_UA,
                'Origin': 'https://qqt.cmicrwx.cn', 'Referer': GIFT_ENTRY,
                'X-Requested-With': 'XMLHttpRequest'}) as client:
            fresh_profile = None
            def initialize():
                nonlocal fresh_profile
                donor = self.star.sessions.load(login_id, phone)
                if donor:
                    for item in donor['cookies']:
                        client.cookies.set(item['name'], item['value'], domain=item['domain'], path=item['path'])
                    for cookie in client.cookies.jar:
                        original = next(c for c in donor['cookies'] if (c['name'],c['domain'],c['path']) == (cookie.name,cookie.domain,cookie.path))
                        cookie.expires, cookie.secure = original['expires'], original['secure']
                    try:
                        fresh_profile = self._profile(client, snapshot)
                    except CheckinError as error:
                        if error.code != 'GIFT_LOGIN_REQUIRED':
                            raise
                        fresh_profile = None
                    if fresh_profile is not None:
                        return {'entry': GIFT_ENTRY, 'identity_created_at': donor['created_at'],
                                'identity_source': donor.get('source', 'app'), 'phone_verified': True}
                if getattr(drama, 'automatic', False):
                    error = CheckinError('等待已有全球通身份或每日星动日凭据准备；可主动执行本任务按需认证。',
                                         'GIFT_SESSION_WAITING', 409)
                    error.identity_tag = self._tag(donor['cookies']) if donor else ''
                    raise error
                if self._busy(login_id):
                    raise CheckinError('全球通验证或提交正在进行，完成后复用有效身份。', 'GIFT_SESSION_WAITING', 409)
                issued = self.now()
                ticket = drama._call('/user/getYDRZToken', {'targetSourceId': '001191'})
                url = QQT+'/new/ssoTokenLogin.action'
                for attempt in range(6):
                    response = client.get(url, params={'sspId': '75900', 'token': ticket} if attempt == 0 else None,
                                          timeout=(10, 20), allow_redirects=False)
                    if response.status_code not in {301,302,303,307,308}:
                        break
                    url = urljoin(url, response.headers['location'])
                    parts = urlsplit(url)
                    if parts.scheme != 'https' or parts.netloc != 'qqt.cmicrwx.cn':
                        raise CheckinError('全球通认证入口已变化。', 'GIFT_LOGIN_REQUIRED', 401)
                if response.status_code != 200:
                    raise CheckinError('全球通认证未完成。', 'GIFT_LOGIN_REQUIRED', 401)
                fresh_profile = self._profile(client, snapshot)
                return {'entry': response.url, 'identity_created_at': issued, 'identity_source': 'app',
                        'phone_verified': True}
            with self.star.sessions.account_lock(login_id):
                # Share the authentication lock with Star Day so concurrent
                # identity preparation reuses the first successful session.
                self.cache.connect(GIFT_SCOPE, snapshot, client, initialize, expiry_cookies=('access_token',))
                try:
                    profile = fresh_profile if fresh_profile is not None else self._profile(client, snapshot)
                except CheckinError as error:
                    if error.code != 'GIFT_LOGIN_REQUIRED' or not self.cache.recover(client):
                        raise
                    profile = fresh_profile if fresh_profile is not None else self._profile(client, snapshot)
                meta = client._credential_data
                saved = self.cache.load(GIFT_SCOPE, snapshot)
                issued = meta.get('identity_created_at', saved['created_at'])
                self.cache.sync(client, data={**meta, 'identity_created_at': issued, 'phone_verified': True})
                # Publish only an already authenticated identity, without
                # Star-Day login, sign-in, parameters, or preparation requests.
                if (issued+7200 > self.now() and not self._busy(login_id)
                        and self.star.sessions.load(login_id, phone) is None):
                    self.star.sessions.save(login_id, phone, client,
                        source=meta.get('identity_source', 'app'), created_at=issued)
            yield client, profile

    def global_tone(self, drama):
        if self._gift_context is None:
            context = self.gift_session(drama)
            self._gift_client, self._gift_profile = context.__enter__()
            self._gift_context = context
        profile = self._gift_profile
        tier = str(profile['userLevel'])
        if tier in {'-1','7','9999'} or profile['levelName'] == '非全球通用户':
            raise CheckinError('官方识别为非全球通用户，该任务仅限全球通客户。', 'TASK_INELIGIBLE', 403)
        if tier not in {'0','1','2','3','4','5','8'}:
            raise CheckinError('官方尚未返回有效全球通等级，暂不提交礼遇任务。', 'QUALIFICATION_UNCONFIRMED')
        return {'tier': tier, 'tier_name': profile['levelName'], 'message': '官方已核对：'+profile['levelName']}

    def global_page(self, drama):
        if self._gift_context is None:
            self.global_tone(drama)
        client = self._gift_client
        response = client.get(client._credential_data['entry'], timeout=(10, 20), allow_redirects=False)
        if response.status_code != 200:
            raise CheckinError('全球通礼遇页面未成功返回。', 'TASK_PAGE_FAILED')
        return response
