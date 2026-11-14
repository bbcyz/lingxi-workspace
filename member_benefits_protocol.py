"""Fixed-asset protocol runtime for the campus benefit risk and order flow."""
from __future__ import annotations

from base64 import b64decode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from urllib.parse import urlsplit

if os.environ.get('LINGXI_ANDROID') != '1':
    import requests
    from curl_cffi import requests as curl_requests

from runtime_paths import DATA_DIR


ORIGIN = 'https://dev.coc.10086.cn'
PAGE_URL = (ORIGIN + '/coc3/canvas/rightsmarket-h5-canvas/online/zeromzone'
            '?channelCode=P00000011939&pageKey=3&pageRecorded=true')
GETTOKEN_URL = ORIGIN + '/coc3/gr/risk-ability/wasm/gettoken'
ORDER_URL = ORIGIN + '/coc3/coc3-market-order/api/external/order/placeOrder.do'
EDGE_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0')
EDGE157_JA3 = ('771,4865-4866-4867-49195-49199-49196-49200-52393-52392-'
               '49171-49172-156-157-47-53,0-65281-11-65037-13-43-35-17613-'
               '51-18-23-10-16-27-5-45,4588-29-23-24,0')
EDGE157_AKAMAI = '1:65536;2:0;4:6291456;6:262144|15663105|0|m,a,s,p'
GLUE_SHA256 = '42446b9c1ef85df1094d0d06b8bae6da74f002f1407d5a8e783139e967add8a2'
WASM_SHA256 = '4f6f69cf9a46a8a2c3fb23ed4c31b08a9da5b2afc1bc2149ee7364c77f0570a8'
ROOT = Path(__file__).resolve().parent / 'recharge' / 'member_benefits_runtime'
DFP_STATE_SCOPE = 'member-campus-dfp-state'
WHEEL_MIN_LIFETIME_MS = 500


class CampusProtocolError(Exception):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class _GettokenTransport:
    def __init__(self):
        self.session = curl_requests.Session(
            impersonate='chrome150', ja3=EDGE157_JA3, akamai=EDGE157_AKAMAI)
        self.token = secrets.token_urlsafe(24)
        self.lock = Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                try:
                    if self.headers.get('X-Bridge-Token') != owner.token:
                        raise ValueError('设备请求桥认证失败')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1_000_000:
                        raise ValueError('设备请求桥报文长度无效')
                    payload = json.loads(self.rfile.read(length))
                    if payload.get('url') != GETTOKEN_URL or str(payload.get('method')).upper() != 'POST':
                        raise ValueError('设备请求桥拒绝了非预期接口')
                    headers = {str(key): str(value) for key, value in (payload.get('headers') or {}).items()
                               if str(key).lower() not in ('host', 'content-length', 'connection')}
                    with owner.lock:
                        response = owner.session.post(GETTOKEN_URL, headers=headers,
                                                      data=str(payload.get('body') or '').encode(),
                                                      timeout=20, allow_redirects=False)
                    result = {'status': response.status_code,
                              'headers': {'content-type': response.headers.get('content-type', 'application/json')},
                              'body': response.text, 'transport': 'curl_cffi:chrome150-edge157-persistent'}
                    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
                    self.send_response(200)
                except Exception as error:
                    encoded = json.dumps({'error': str(error)}, ensure_ascii=False,
                                         separators=(',', ':')).encode()
                    self.send_response(502)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05},
                             name='campus-gettoken', daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def url(self):
        host, port = self.server.server_address
        return f'http://{host}:{port}/gettoken'

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.session.close()


class CampusRiskProtocol:
    def __init__(self, storage=None, state_dir=None):
        self.storage = storage
        self.state_dir = Path(state_dir or DATA_DIR / 'member-benefits')

    @staticmethod
    def _node(script, args, env, timeout=180):
        node = os.environ.get('CMCC_NODE_PATH') or shutil.which('node')
        if not node or not script.is_file():
            raise CampusProtocolError('福袋风控运行组件不完整。')
        try:
            process = subprocess.run([node, str(script), *args], cwd=ROOT, env=env,
                                     capture_output=True, text=True, encoding='utf-8', errors='replace',
                                     timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired as error:
            raise CampusProtocolError('福袋风控参数生成超时。') from error
        if process.returncode:
            raise CampusProtocolError(f'{script.name} 未完成风控参数生成。')
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise CampusProtocolError(f'{script.name} 返回格式有误。') from error

    def prepare(self, _cookies, token, _user_agent, entry, *, order=None, before_submit=None):
        if not isinstance(token, str) or len(token) != 36:
            raise CampusProtocolError('会员权益登录令牌格式无效。')
        page_url = PAGE_URL
        if entry:
            parsed = urlsplit(entry)
            allowed = (
                '/coc3/canvas/rightsmarket-h5-canvas/online/zeromzone',
                '/coc3/canvas/rightsmarket-h5-canvas/online/zero',
                '/coc3/canvas/rightsmarket-h5-canvas/online/halfprizemzone',
                '/coc3/canvas/rightsmarket-h5-canvas/online/halfprize',
            )
            if parsed.scheme != 'https' or parsed.netloc != 'dev.coc.10086.cn' or parsed.path not in allowed:
                raise CampusProtocolError('会员权益风控页面地址无效。')
            if parsed.path != allowed[0]:
                page_url = entry
        self.state_dir.mkdir(parents=True, exist_ok=True)
        account_key = sha256(token.encode()).hexdigest()[:24]
        env = dict(os.environ)
        saved_states = (self.storage.load_activity_data(DFP_STATE_SCOPE) or {}) if self.storage else {}
        legacy = self.state_dir / f'dfp-{account_key}.json'
        if self.storage and account_key not in saved_states and legacy.is_file():
            try:
                legacy_state = json.loads(legacy.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                legacy_state = None
            if isinstance(legacy_state, dict):
                saved_states = {**saved_states, account_key: legacy_state}
                self.storage.save_activity_data(DFP_STATE_SCOPE, saved_states)
                legacy.unlink()
        with TemporaryDirectory(dir=self.state_dir) as temporary:
            state = Path(temporary) / 'dfp-state.json'
            if isinstance(saved_states.get(account_key), dict):
                state.write_text(json.dumps(saved_states[account_key], ensure_ascii=False), encoding='utf-8')
            dfp = self._node(ROOT / 'dfp_node.mjs', [
                '--emit-token', '--user-agent', EDGE_UA, '--page-url', page_url,
                '--platform', 'Win32', '--state-path', str(state),
            ], env)
            if self.storage and state.is_file():
                try:
                    current_state = json.loads(state.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    current_state = None
                if isinstance(current_state, dict):
                    self.storage.save_activity_data(
                        DFP_STATE_SCOPE, {**saved_states, account_key: current_state})
        details = dfp.get('result') or {}
        p3 = details.get('gr-risk-p3')
        if dfp.get('schema_version') != 1 or dfp.get('ok') is not True or not isinstance(p3, str) or len(p3) != 40:
            raise CampusProtocolError('官方服务风控没有返回有效设备令牌。')
        full_lengths = [int(item.get('param_length') or 0) for item in dfp.get('requests') or []
                        if int(item.get('param_length') or 0) > 500]
        if full_lengths and not all(1500 <= length <= 1800 for length in full_lengths):
            raise CampusProtocolError('官方服务风控参数长度异常。')

        with _GettokenTransport() as transport:
            wheel_env = dict(env)
            wheel_env.update(PHONE_TOKEN=token, COC_GR_RISK_P3=p3,
                             COC_FETCH_BRIDGE_URL=transport.url,
                             COC_FETCH_BRIDGE_TOKEN=transport.token,
                             COC_TLS_IMPERSONATE='chrome150',
                             WHEEL_ASSET_CACHE=str(self.state_dir / 'wheel-cache'))
            wheel = self._node(ROOT / 'wheel_node.mjs', [
                '--emit-values', '--user-agent', EDGE_UA, '--page-url', page_url,
                '--platform', 'Win32',
            ], wheel_env)

        values = (wheel.get('roto_simple') or {}).get('value')
        calls = wheel.get('calls') or []
        call = calls[0] if calls else {}
        assets = wheel.get('assets') or {}
        config = wheel.get('config') or {}
        context = wheel.get('context') or {}
        decoded = []
        if isinstance(values, list) and len(values) == 2:
            try:
                decoded = [len(b64decode(str(value), validate=True)) for value in values]
            except ValueError:
                decoded = []
        if (wheel.get('schema_version') != 1 or wheel.get('error') or
                not isinstance(values, list) or len(values) != 2 or
                len(str(values[0])) != 344 or not 600 <= len(str(values[1])) <= 900 or
                decoded[:1] != [256] or len(decoded) != 2 or not 480 <= decoded[1] <= 532 or
                not 8000 <= int(call.get('bodyLen') or 0) <= 12000 or
                call.get('status') != 200 or str(call.get('responseCode')) != '0' or
                call.get('responseSuccess') is not True or int(call.get('responseDataLength') or 0) != 32 or
                not str(call.get('transport') or '').startswith('curl_cffi:') or
                assets.get('glue_sha256') != GLUE_SHA256 or assets.get('wasm_sha256') != WASM_SHA256 or
                wheel.get('module_url_kind') != 'file' or
                config.get('user_agent') != EDGE_UA or config.get('page_url') != page_url or
                int(config.get('wheel_min_lifetime_ms') or 0) < WHEEL_MIN_LIFETIME_MS or
                int(context.get('local_storage_keys') or 0) < 15 or
                int(context.get('session_storage_keys') or 0) < 7):
            raise CampusProtocolError('官方 Wheel 风控参数未通过本地完整性检查。')
        values = [str(value) for value in values]

        result = {'risk_token': p3, 'wasm_params': values, 'cookies': [], 'device': ''}
        if order is None:
            return result
        payload = {**order, 'operDfp': None}
        headers = {'phone': token, 'Referer': page_url,
                   'wasmParams': json.dumps(values, ensure_ascii=False, separators=(',', ':')),
                   'Accept': 'application/json, text/plain, */*',
                   'Content-Type': 'application/json;charset=UTF-8'}
        if before_submit is not None:
            before_submit()
        try:
            response = requests.post(ORDER_URL, headers=headers, json=payload,
                                     timeout=30, allow_redirects=False)
        except requests.RequestException as error:
            raise CampusProtocolError('未收到官方领取回执，请刷新核对领取状态。', uncertain=True) from error
        body = None
        if 200 <= response.status_code < 300 and response.content:
            try:
                body = response.json()
            except ValueError as error:
                raise CampusProtocolError('官方领取响应无法确认，请刷新核对领取状态。', uncertain=True) from error
            if not isinstance(body, dict):
                raise CampusProtocolError('官方领取响应无法确认，请刷新核对领取状态。', uncertain=True)
        result['order'] = {'status': response.status_code, 'body': body}
        return result
