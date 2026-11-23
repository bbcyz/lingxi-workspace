"""Account-bound recharge C1 device registration using truthful Python metadata.

Only an accepted C1 response supplies a device ticket. The twenty-minute
deadline below reproduces the official client Cookie policy, not a guaranteed
server token lifetime. A device ticket is not a captcha verification token.
"""
from concurrent.futures import Future
from datetime import datetime
from http.cookiejar import Cookie
import os
import platform
import secrets
from threading import RLock
import time
from urllib.parse import quote, unquote_plus

from coupon_device import APP_ID, DOMAIN, LID_COOKIE, LOCAL_ID_ALPHABET, TOKEN_COOKIE, _cookie_value, encode_param
from http_transport import RequestException
from login_storage import LoginStorageError
from runtime_paths import VERSION


SERVER = 'https://yxfk.market.chinamobile.com:8081/mgt/decision-engine/udid/c1'
SCOPE = 'coupon-recharge-device'
COOKIE_SECONDS = 20 * 60


class RechargeDeviceError(Exception):
    def __init__(self, message, code='RISK_INITIALIZATION_FAILED', status=502, *, remote_status=None):
        super().__init__(message)
        self.code, self.status, self.remote_status = code, status, remote_status


def _ticket(value):
    return (_cookie_value(value) and not value.startswith('#')
            and value not in ('unknown', 'undefined', 'null', 'true', 'false')
            and all(33 <= ord(char) <= 126 and char not in '\"\\,;' for char in value))


class RechargeDevices:
    """Reuse one account's tickets without sharing its operation HTTP handle.

    The supplied storage's activity-data API encrypts values at rest. Unknown
    Cookie expiry permits reuse only while that Cookie remains in the supplied
    session; it does not establish validity after a restart. The caller owns
    the operation client lock while temporary request headers are installed.
    """
    def __init__(self, storage=None, clock=time.time):
        self.storage, self.clock = storage, clock
        self._records = (storage.load_activity_data(SCOPE) or {}) if storage else {}
        self._lock = RLock()
        self._pending = {}
        self._generation = {}
        self._ignore_client_cookie = set()

    def _store(self, login_id, record, generation):
        with self._lock:
            if self._generation.get(login_id, 0) != generation:
                raise RechargeDeviceError('充值设备准备已取消。', 'RISK_PREPARATION_CANCELLED', 409)
            records = {**self._records, login_id: record}
            if self.storage:
                try:
                    self.storage.save_activity_data(SCOPE, records)
                except LoginStorageError as error:
                    raise RechargeDeviceError('充值设备凭据保存失败。', 'RISK_STORAGE_FAILED', 500) from error
            self._records = records

    def _mount(self, client, record):
        jar = client.cookies.jar
        for cookie in list(jar):
            if (cookie.name == TOKEN_COOKIE and cookie.domain.lstrip('.') == DOMAIN
                    and cookie.path == '/i'):
                jar.clear(cookie.domain, cookie.path, cookie.name)
        values = [(TOKEN_COOKIE, record['token'], '/i', True, record['expires_at'])]
        if record['lid']:
            values.append((LID_COOKIE, quote(record['lid'], safe="~()*!.'-"), '/', False, None))
        for name, value, path, secure, expires in values:
            jar.set_cookie(Cookie(version=0, name=name, value=value, port=None, port_specified=False,
                domain=DOMAIN, domain_specified=False, domain_initial_dot=False,
                path=path, path_specified=True, secure=secure, expires=expires,
                discard=expires is None, comment=None, comment_url=None, rest={}, rfc2109=False))

    def _request(self, client, user_agent, payload):
        saved_headers = client.headers
        client.headers = {'User-Agent': user_agent, 'Accept': 'application/json, text/plain, */*',
                          'param': encode_param(payload)}
        try:
            try:
                response = client.request('GET', SERVER, timeout=(5, 30), allow_redirects=False)
            except RequestException as error:
                raise RechargeDeviceError('官方充值设备校验连接失败。', 'RISK_NETWORK_FAILED') from error
        finally:
            client.headers = saved_headers
        if not 200 <= response.status_code < 300:
            raise RechargeDeviceError('官方充值设备校验请求失败。', 'RISK_HTTP_FAILED',
                                      remote_status=response.status_code)
        try:
            result = response.json()
        except ValueError as error:
            raise RechargeDeviceError('官方充值设备校验响应无法识别。', 'RISK_RESPONSE_INVALID') from error
        if not isinstance(result, dict) or type(result.get('status')) is not int:
            raise RechargeDeviceError('官方充值设备校验状态无效。', 'RISK_RESPONSE_INVALID')
        status = result['status']
        if status == -800:
            raise RechargeDeviceError('官方提示请求频繁，请稍后主动重试。', 'RISK_RATE_LIMITED', 429,
                                      remote_status=status)
        if status == -5:
            raise RechargeDeviceError('官方要求的充值设备校验传输尚未接入。', 'RISK_TRANSPORT_REQUIRED',
                                      remote_status=status)
        if status == -3:
            raise RechargeDeviceError('官方充值设备认证未通过。', remote_status=status)
        return result

    def prepare(self, login_id, client):
        if not isinstance(login_id, str) or not login_id:
            raise ValueError('充值设备凭据必须绑定账号。')
        user_agent = (f'LingxiWorkspace/{VERSION} (Python/{platform.python_version()}; '
                      f'{platform.system()} {platform.release()})')
        with self._lock:
            generation = self._generation.get(login_id, 0)
            future = self._pending.get(login_id)
            owner = future is None
            if owner:
                record = self._records.get(login_id)
                if record and (record.get('account_key') != login_id or record.get('provider') != SERVER
                               or record.get('app_id') != APP_ID):
                    record = None
                now = self.clock()
                lid = record.get('lid', '') if record else ''
                lid_type = record.get('lid_type', '0') if record else '0'
                if not lid:
                    existing_lid = next((c for c in client.cookies.jar if c.name == LID_COOKIE
                        and c.domain.lstrip('.') == DOMAIN and c.path == '/'
                        and (c.expires is None or c.expires > now) and _cookie_value(c.value)), None)
                    if existing_lid:
                        lid, lid_type = unquote_plus(existing_lid.value), 1
                cookie = next((c for c in client.cookies.jar if c.name == TOKEN_COOKIE
                    and c.domain.lstrip('.') == DOMAIN and c.path == '/i' and c.secure
                    and (c.expires is None or c.expires > now) and _ticket(c.value)), None)
                if cookie and login_id not in self._ignore_client_cookie:
                    record = {'account_key': login_id, 'provider': SERVER, 'app_id': APP_ID,
                        'lid': lid, 'lid_type': lid_type, 'token': cookie.value,
                        'issued_at': record.get('issued_at') if record and record.get('token') == cookie.value else None,
                        'expires_at': cookie.expires}
                    self._store(login_id, record, generation)
                    self._mount(client, record)
                    return {'prepared': True, 'reused': True, 'expires_at': cookie.expires}
                if (record and _ticket(record.get('token')) and record.get('expires_at') is not None
                        and record['expires_at'] > now):
                    self._mount(client, record)
                    return {'prepared': True, 'reused': True, 'expires_at': record['expires_at']}
                future = Future()
                self._pending[login_id] = future
                if not lid:
                    lid = str(int(now * 1000)) + ''.join(secrets.choice(LOCAL_ID_ALPHABET) for _ in range(32))
                record = {'account_key': login_id, 'provider': SERVER, 'app_id': APP_ID,
                    'lid': lid, 'lid_type': 1, 'token': '', 'issued_at': None, 'expires_at': None}
        if owner:
            try:
                self._store(login_id, record, generation)
                payload = {'lid': record['lid'], 'lidType': lid_type, 'cache': True,
                           'appKey': quote(APP_ID, safe="~()*!.'-")}
                result = self._request(client, user_agent, payload)
                if result['status'] in (-4, -6):
                    if result['status'] == -4:
                        if not _cookie_value(result.get('data')):
                            raise RechargeDeviceError('官方返回的充值设备标识无效。', 'RISK_RESPONSE_INVALID',
                                                      remote_status=-4)
                        record = {**record, 'lid': result['data'], 'lid_type': 1}
                        self._store(login_id, record, generation)
                    descriptor = {'pro': -1, 'ua': user_agent, 'np': platform.system(),
                        'to': -int(datetime.now().astimezone().utcoffset().total_seconds() / 60)}
                    concurrency = os.cpu_count()
                    if concurrency is not None:
                        descriptor['hc'] = concurrency
                    result = self._request(client, user_agent, {**payload, 'lid': record['lid'],
                        'lidType': 1, **descriptor})
                    accepted = result['status'] == 2
                else:
                    accepted = result['status'] in (1, 2)
                if not accepted or not _ticket(result.get('data')):
                    raise RechargeDeviceError('官方未返回有效的充值设备凭据。', 'RISK_RESPONSE_INVALID',
                                              remote_status=result['status'])
                issued_at = self.clock()
                record = {**record, 'lid_type': 1, 'token': result['data'], 'issued_at': issued_at,
                          'expires_at': int(issued_at + COOKIE_SECONDS)}
                self._store(login_id, record, generation)
                future.set_result(record)
            except Exception as error:
                future.set_exception(error)
                raise
            finally:
                with self._lock:
                    if self._pending.get(login_id) is future:
                        del self._pending[login_id]
        else:
            record = future.result()
        with self._lock:
            if self._generation.get(login_id, 0) != generation:
                raise RechargeDeviceError('充值设备准备已取消。', 'RISK_PREPARATION_CANCELLED', 409)
            self._ignore_client_cookie.discard(login_id)
            self._mount(client, record)
        return {'prepared': True, 'reused': not owner, 'expires_at': record['expires_at']}

    def forget(self, login_id):
        with self._lock:
            self._generation[login_id] = self._generation.get(login_id, 0) + 1
            self._pending.pop(login_id, None)
            self._ignore_client_cookie.add(login_id)
            records = {key: value for key, value in self._records.items() if key != login_id}
            if self.storage:
                self.storage.save_activity_data(SCOPE, records)
            self._records = records
