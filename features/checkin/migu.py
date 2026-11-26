"""Migu Video sign-in task, using the published H5 SSO and gateway protocol.

Sources: migu_login_callback.htm, resetMGU.js, vue_mgvendor.js and mgEd.js,
observed 2026-11-25. No phone or native SDK is used at runtime.
"""
import base64
from contextlib import contextmanager
from http.cookiejar import Cookie
import hashlib
import json
import re
import time
from urllib.parse import quote
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from recharge.cmcc import APP_UA
from workspace.http_transport import requests
from .client import CheckinError, _data


TASK_NAME = '登录咪咕视频领好礼'
TASK = ('6', '1', 'migu')
ORIGIN = 'https://m.miguvideo.com'
ENTRY = ORIGIN + '/mgs/promotion/subject/provinceMiddlePage/prd/index.html?pageId=467a4e3cc7fa46ac99f01dc20eaba6e4'
API = 'https://webapi.miguvideo.com'
CLIENT_ID = '27fb3129-5a54-45bc-8af1-7dc8f1155501'
SCOPE = 'checkin:migu-video'
# Public mgEd.js protocol keys, not account credentials.
GATEWAY_KEY = b'3f28f81cf91adb9665c51d3d0331b432'
RESPONSE_KEY = b'b96fadd81503d27c212d126e78155d9c'
AUTH_HEADERS = ('userId', 'userToken', 'clientId', 'SDKCEId', 'sign', 'userInfo')


def encode_request(body, timestamp):
    text = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
    padder = PKCS7(128).padder()
    raw = padder.update(text.encode()) + padder.finalize()
    encoder = Cipher(algorithms.AES(GATEWAY_KEY), modes.ECB()).encryptor()
    headers = {'ensign': hashlib.md5((text + GATEWAY_KEY.decode() + timestamp).encode()).hexdigest().upper(),
               'timestamp': timestamp, 'codeVersion': 'V1', 'Content-Type': 'application/json'}
    return base64.b64encode(encoder.update(raw) + encoder.finalize()).decode(), headers


def decode_response(response):
    text = response.text
    if response.headers.get('x-amz-meta-re') == '1' or response.headers.get('responseencryption') == 'true':
        key = RESPONSE_KEY if response.headers.get('x-amz-meta-re') == '1' else GATEWAY_KEY
        if response.headers.get('x-amz-meta-rk'):
            table = (3, 5, 7, 0, 15, 10, 13, 1, 11, 14, 4, 6, 9, 12, 8, 2)
            key = bytes((table[b >> 4] << 4) | table[b & 15]
                        for b in base64.b64decode(response.headers['x-amz-meta-rk']))
        decoder = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        raw = decoder.update(base64.b64decode(text)) + decoder.finalize()
        unpadder = PKCS7(128).unpadder()
        text = (unpadder.update(raw) + unpadder.finalize()).decode()
    # An empty acknowledgement still requires the parent activity's readback.
    return json.loads(text) if text else None


def decode_mobile(value):
    if re.fullmatch(r'1[3-9]\d{9}', value):
        return value
    reverse = value[::-1]
    end = len(reverse) - 3
    if len(reverse) > 8 and reverse[1] + reverse[5] + reverse[8] == reverse[end:]:
        return re.sub(r'\++', '', base64.b64decode(reverse[:end]).decode(), count=1)
    return value


class CheckinMigu:
    def __init__(self, credentials, cache):
        self.credentials, self.cache = credentials, cache

    def _call(self, client, method, path, body=None, *, writing=False):
        kwargs = {}
        if method == 'POST':
            kwargs['data'], kwargs['headers'] = encode_request(body, str(int(time.time() * 1000)))
        try:
            response = client.request(method, API + path, **kwargs, timeout=(8, 20), allow_redirects=False)
        except requests.RequestsError as error:
            raise CheckinError('咪咕服务连接中断，请核对任务进度。',
                               'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK', uncertain=writing) from error
        try:
            result = decode_response(response)
        except (ValueError, UnicodeError) as error:
            raise CheckinError('咪咕响应格式异常，请核对官方进度。', 'UPSTREAM_FORMAT', uncertain=writing) from error
        code = str(result.get('code')) if isinstance(result, dict) else ''
        if response.status_code == 401 or code == '401':
            if not writing and self.cache.recover(client):
                return self._call(client, method, path, body)
            raise CheckinError('咪咕活动登录需要恢复，请重新查询。', 'MIGU_LOGIN_REQUIRED', 401, uncertain=writing)
        if response.status_code != 200 or result is not None and code != '200':
            raise CheckinError('咪咕尚未确认本次操作，请核对官方进度。', 'MIGU_REJECTED', uncertain=writing)
        if not writing and result is None:
            raise CheckinError('咪咕查询返回空数据。', 'UPSTREAM_FORMAT')
        return result

    def _attach_identity(self, client, identity, snapshot):
        if identity.get('resultCode') != 'LOGIN_SUCCESS':
            raise CheckinError('咪咕登录未成功，未提交任务完成记录。', 'MIGU_LOGIN_REQUIRED', 401)
        user = identity['userInfo']
        if decode_mobile(user['mobile']) != snapshot['phone']:
            raise CheckinError('咪咕活动账号与所选号码不一致。', 'ACCOUNT_MISMATCH', 409)
        # resetMGU checks expiredOn against Date.now(); this is an official ms expiry.
        expiry = int(user['expiredOn']) / 1000
        if expiry <= time.time():
            raise CheckinError('官方返回的咪咕会话已到期。', 'MIGU_LOGIN_REQUIRED', 401)
        client.headers.update({'userId': user['userId'], 'userToken': user['userToken'],
            'clientId': CLIENT_ID, 'SDKCEId': CLIENT_ID, 'sign': identity['sign'],
            'userInfo': json.dumps(user, ensure_ascii=False, separators=(',', ':'))})
        cookie = {'userId': user['userId'], 'userToken': user['userToken'], 'clientId': CLIENT_ID,
                  'mobile': user['mobile'], 'sign': identity['sign'],
                  'userInfo': client.headers['userInfo'], 'clientProvinceCode': user['areaId'],
                  'clientCityId': user['cityId'], 'carrierCode': user['carrierCode']}
        client.cookies.jar.set_cookie(Cookie(0, 'userInfo', quote(json.dumps(cookie, separators=(',', ':')), safe=''),
            None, False, '.miguvideo.com', True, True, '/', True, True, int(expiry), False, None, None, {}))
        return {'phone': snapshot['phone'], 'user_id': user['userId'], 'expires_at': expiry}

    @contextmanager
    def _session(self, parent, parent_session):
        snapshot = self.credentials()
        with requests.Session(impersonate='chrome', headers={
                'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': ENTRY, 'channel': 'H5',
                'terminalId': 'h5', 'appId': 'miguvideo', 'appCode': 'miguvideo_default_h5',
                'Support-Pendant': '1', 'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*'}) as client:
            def initialize():
                latest = self.credentials()
                if latest['phone'] != snapshot['phone']:
                    raise CheckinError('账号已变更，请重新查询。', 'ACCOUNT_MISMATCH', 409)
                for attempt in range(2):
                    response = parent_session.get('https://wx.10086.cn/qwhdhub/user/getYDRZToken',
                        params={'targetSourceId': '012012'},
                        headers={'login-check': '1', 'x-requested-with': 'XMLHttpRequest'},
                        timeout=(8, 20), allow_redirects=False)
                    if response.status_code != 200:
                        raise CheckinError('咪咕登录换票未完成。', 'MIGU_LOGIN_REQUIRED', 401)
                    value = response.json()
                    if value.get('code') != 'GOTO_LOGIN':
                        break
                    if attempt or parent.credential_cache is None or not parent.credential_cache.recover(parent_session):
                        raise CheckinError('签到活动会话需要恢复，请重新查询。', 'ACTIVITY_LOGIN_REQUIRED', 401)
                ticket = _data(value, stage='咪咕换票')
                storage = self.cache.storage
                key = 'checkin-migu-device:' + self.cache.account(snapshot)
                device = storage.load_activity_data(key) if storage else None
                if not device:
                    device = {'device_id': str(uuid.uuid4())}
                    if storage:
                        storage.save_activity_data(key, device)
                response = client.post(API + '/userCenterLogin/login/migutokenforencrypt',
                    params={'sign': 'abcde', 'signType': 'AES', 'clientId': CLIENT_ID},
                    json={'miguToken': 'ANDT' + ticket, 'deviceId': device['device_id'],
                          'timestamp': int(time.time() * 1000), 'loginType': 'SSO_AUTO'},
                    headers={'sourceId': '203021', 'appType': '2', 'version': '2.0',
                             'Content-Type': 'application/json;charset=UTF-8'},
                    timeout=(8, 20), allow_redirects=False)
                if response.status_code != 200:
                    raise CheckinError('咪咕登录服务暂不可用。', 'MIGU_LOGIN_REQUIRED', 401)
                return self._attach_identity(client, response.json(), snapshot)
            self.cache.connect(SCOPE, snapshot, client, initialize, headers=AUTH_HEADERS, expiry_cookies=('userInfo',))
            if client._credential_data['phone'] != snapshot['phone']:
                raise CheckinError('咪咕活动账号与所选号码不一致。', 'ACCOUNT_MISMATCH', 409)
            yield client

    def complete(self, task, info, parent, parent_session, *, before_submit):
        with self._session(parent, parent_session) as client:
            result = self._call(client, 'GET', '/gateway/user/videox/v1/user-info/')
            user = result['body']['data']
            if str(user['userId']) != client._credential_data['user_id']:
                raise CheckinError('咪咕查询账号不一致，未提交任务。', 'ACCOUNT_MISMATCH', 409)
            with requests.Session(impersonate='chrome', headers={'User-Agent': APP_UA}) as page_client:
                page = page_client.get(ENTRY, timeout=(8, 20), allow_redirects=False)
            if page.status_code != 200:
                raise CheckinError('咪咕活动页面访问失败，未提交任务。', 'TASK_PAGE_FAILED')
            before_submit()
            self._call(client, 'POST', '/gateway/ability/videox/task/openFinish',
                       {'cToken': info['cToken']}, writing=True)
            return {'login_verified': True}
