"""Python implementation of the published Cloud App sign-in browse tasks.

Source: capp.139.com app.7d8ed31a.js and home.b7061215.js, 2026-11-24.
The DES key and MD5 salt below belong to the public frontend protocol.
Account tokens are obtained through APP SSO and kept in CredentialCache.
"""
import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.padding import PKCS7
try:  # Android ships cryptography 42; desktop also supports newer versions.
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

from recharge.cmcc import APP_UA, SHANGHAI
from workspace.accounts.login import AppLoginSession, AppLoginError
from workspace.http_transport import requests
from .client import CheckinError


ORIGIN = 'https://capp.139.com'
API = ORIGIN + '/cloud-trial/'
SCOPE = 'checkin:cloudtrial'
APP_TOKEN = '/leadeon-abilityopen-biz/BN/obtainToken/getBigNetToken'
PUBLIC_KEY = b'KHEsnN7Q'
CLOUD_TASKS = {
    '赢10元红包等好礼': ('10', '3', '14086'),
    '体验云观影领红包': ('10', '2', '13340'),
}


def des_text(value, *, decrypt=False):
    cipher = Cipher(TripleDES(PUBLIC_KEY * 3), modes.ECB())
    if decrypt:
        decoder = cipher.decryptor()
        raw = decoder.update(base64.b64decode(value, validate=True)) + decoder.finalize()
        unpadder = PKCS7(64).unpadder()
        return (unpadder.update(raw) + unpadder.finalize()).decode()
    padder = PKCS7(64).padder()
    raw = padder.update(value.encode()) + padder.finalize()
    encoder = cipher.encryptor()
    return base64.b64encode(encoder.update(raw) + encoder.finalize()).decode()


def supported_task(task):
    expected = CLOUD_TASKS.get(task['taskName'])
    target = urlsplit(task.get('jumpUrl', ''))
    if (expected is None or target.scheme != 'https' or target.netloc != 'capp.139.com'
            or target.path != '/' or target.query):
        return False
    query = parse_qs(target.fragment.partition('?')[2])
    try:
        app_id = des_text(query.get('i', [''])[0], decrypt=True)
    except (ValueError, UnicodeError):
        return False
    return (str(task['taskCategory']), str(task['taskType']), app_id) == expected


class CheckinCloud:
    def __init__(self, credentials, cache, *, recover=None):
        self.credentials, self.cache, self.recover = credentials, cache, recover

    @staticmethod
    def _headers(token):
        now = str(int(time.time() * 1000))
        request = datetime.now(SHANGHAI).strftime('%Y%m%d%H%M%S') + now + secrets.token_hex(4)
        coded = des_text(token + '#' + now + '#' + secrets.token_hex(3) + '#12345686') if token else ''
        sign = hashlib.md5((request + '12345686' + coded
                           + 'QcIQtMartvQdW3ehPPHwH1xfXAoWP8Oq' + now).encode()).hexdigest()
        return {'requestId': request, 'appId': '12345686', 'token': coded,
                'timestamp': now, 'sign': sign, 'test_field': token, 'x-channelSrc': '02199'}

    def _call(self, client, path, body, *, writing=False):
        try:
            response = client.post(API + path, json=body, headers=self._headers(client.headers.get('token', '')),
                                   timeout=(8, 20), allow_redirects=False)
        except requests.RequestsError as error:
            raise CheckinError('云盘任务服务连接中断，请核对官方进度。',
                               'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK', uncertain=writing) from error
        try:
            value = response.json()
        except ValueError as error:
            raise CheckinError('云盘任务响应格式异常。', 'UPSTREAM_FORMAT', uncertain=writing) from error
        if not isinstance(value, dict) or not isinstance(value.get('header'), dict):
            raise CheckinError('云盘任务响应格式异常。', 'UPSTREAM_FORMAT', uncertain=writing)
        status = str(value['header'].get('status'))
        if response.status_code == 401 or status in {'401', '1024'}:
            if not writing and hasattr(client, '_credential_binding') and self.cache.recover(client):
                return self._call(client, path, body)
            raise CheckinError('云盘任务授权需要恢复，请重新查询。', 'CLOUD_LOGIN_REQUIRED', 401, uncertain=writing)
        if response.status_code != 200 or status != '200':
            raise CheckinError('云盘服务尚未确认本次操作，请核对官方进度。', 'CLOUD_REJECTED', uncertain=writing)
        return value['data']

    @contextmanager
    def _session(self, entry, app_id):
        snapshot = self.credentials()
        with requests.Session(impersonate='chrome', headers={
                'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': ORIGIN + '/'}) as client:
            identity_recovered = False
            def initialize():
                nonlocal identity_recovered
                latest = self.credentials()
                if latest['phone'] != snapshot['phone']:
                    raise CheckinError('账号已变更，请重新查询。', 'ACCOUNT_MISMATCH', 409)
                try:
                    _, result, _ = AppLoginSession.from_credentials(latest).call(APP_TOKEN, {
                        'sourceId': '001304', 'url': entry, 'cellNum': snapshot['phone']})
                except AppLoginError as error:
                    raise CheckinError('云盘任务换票失败，请重新查询。', 'CLOUD_LOGIN_REQUIRED', 401) from error
                code = str(result.get('retCode'))
                if (code in {'410000', '400007', '400009', '420000'}
                        and self.recover is not None and not identity_recovered):
                    identity_recovered = True
                    try:
                        self.recover(latest)
                    except AppLoginError as error:
                        raise CheckinError('该号码的 APP 身份未能恢复，请在账号管理重新登录。',
                                           'CLOUD_LOGIN_REQUIRED', 401) from error
                    return initialize()
                body = result.get('rspBody') or {}
                if isinstance(body, str):
                    body = json.loads(body)
                if code != '000000' or not body.get('token'):
                    raise CheckinError('官方云盘换票未完成，请在账号管理核对登录。', 'CLOUD_LOGIN_REQUIRED', 401)
                identity = self._call(client, 'user/login/ssoHW', {
                    'version': '1.0', 'pintype': 13, 'token': body['token'], 'appId': app_id})
                phone = identity['phone']
                if not (len(phone) == 11 and phone.isdigit()):
                    phone = des_text(phone, decrypt=True)
                if phone != snapshot['phone']:
                    raise CheckinError('云盘活动账号不一致。', 'ACCOUNT_MISMATCH', 409)
                identity = {**identity, 'phone': phone}
                client.headers['token'] = identity['token']
                return identity
            self.cache.connect(SCOPE, snapshot, client, initialize, headers=('token',))
            if client._credential_data['phone'] != snapshot['phone']:
                raise CheckinError('云盘活动账号不一致。', 'ACCOUNT_MISMATCH', 409)
            yield client

    def complete(self, task, info, *, before_submit):
        if not supported_task(task):
            raise CheckinError('官方云盘任务入口已变化。', 'TASK_CHANGED', 409)
        app_id = CLOUD_TASKS[task['taskName']][2]
        with self._session(task['jumpUrl'], app_id) as client:
            config = self._call(client, 'appInfo/appManage/query', {'appId': app_id})
            auth = self._call(client, 'appInfo/appAuthLogin/query', {'appId': app_id})
            tickets = self._call(client, 'user/getRcsTokenHW', {
                'hwToken': client._credential_data['hwToken'], 'appId': app_id})
            if str(config['canPlay']) != '1' or auth['checkAuth']:
                raise CheckinError('官方云盘入口当前需要额外授权，任务尚未执行。', 'CLOUD_AUTH_REQUIRED', 409)
            params = parse_qs(urlsplit(auth['schemePath']).query)['params'][0]
            landing = json.loads(base64.b64decode(params))['linkUrl'].replace('#ssoToken#', tickets['ssoToken'])
            target = urlsplit(landing)
            paths = {'14086': '/portal/hcyview/index.html', '13340': '/portal/cloudDisk/index.html'}
            if target.scheme != 'https' or target.netloc != 'm.mcloud.139.com' or target.path != paths[app_id]:
                raise CheckinError('官方云盘浏览入口已变化。', 'TASK_PAGE_CHANGED', 409)
            start = time.monotonic()
            # An activity's cloud login header must never leak to the landing host.
            with requests.Session(impersonate='chrome', headers={'User-Agent': APP_UA}) as page_client:
                page = page_client.get(landing, timeout=(8, 20), allow_redirects=False)
            if page.status_code != 200:
                raise CheckinError(f'云盘任务页面 HTTP {page.status_code}，未提交完成记录。', 'TASK_PAGE_FAILED')
            detail = self._call(client, 'cloudDisk/getOneTaskInfo', {'taskToken': info['taskToken']})['data']
            if str(detail['status']) in {'1', '2'}:
                return {'app_id': app_id, 'already_completed': True}
            duration = max(float(detail['scanTime']), float(config['marketConfig'].get('browseTaskDuration') or 5))
            if str(detail['status']) != '0' or not 0 < duration <= 60:
                raise CheckinError('官方云盘任务要求已变化。', 'TASK_CHANGED', 409)
            time.sleep(max(0, duration - (time.monotonic() - start)))
            before_submit()
            result = self._call(client, 'cloudDisk/openFinish', {'token': detail['ctoken']}, writing=True)
            if not result.get('finished'):
                raise CheckinError('官方尚未确认云盘任务完成。', 'RESULT_UNCERTAIN', 409, uncertain=True)
            return {'app_id': app_id, 'browsed_seconds': round(time.monotonic() - start, 2)}
