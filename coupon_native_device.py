"""Account-bound native recharge device tickets, using the Python M1 protocol."""
from concurrent.futures import Future
from http.cookiejar import Cookie
import platform
from threading import RLock
import time
from zipfile import BadZipFile

from app_login import (AppLoginSession,APP_CLIENT_VERSION_NAME,APP_CLIENT_VERSION_CODE,
    APP_CLIENT_ANDROID_API,APP_CLIENT_APP_NAME,APP_CLIENT_LEGACY_SIGN_MD5,APP_CLIENT_APK_BYTES)
from coupon_device import APP_ID, DOMAIN, TOKEN_COOKIE, _cookie_value
from coupon_native_codec import request_packet, response_packet, SDK_VERSION
from coupon_recharge_device import RechargeDeviceError, RechargeDevices, COOKIE_SECONDS
from http_transport import RequestException
from login_storage import LoginStorageError
from runtime_paths import VERSION


SERVER='https://yxfk.market.chinamobile.com:8081/mgt/decision-engine/udid/m1'
SCOPE='coupon-recharge-native-device'
# Native RiskApp adds this client reuse window after an accepted response.
# It is not a promise of server validity and differs from the Touch Cookie.
TICKET_SECONDS=24*60*60
CLIENT_CONTEXT={'version_name':APP_CLIENT_VERSION_NAME,'version_code':APP_CLIENT_VERSION_CODE,
    'android_api':APP_CLIENT_ANDROID_API,'registered_client_fields':2}


class NativeRechargeDevices:
    def __init__(self,credentials,storage=None,clock=time.time):
        self.credentials,self.storage,self.clock=credentials,storage,clock
        self.records=(storage.load_activity_data(SCOPE)or{})if storage else{}
        self.lock=RLock();self.pending={};self.generation={};self.ignore_cookie=set()
        self.legacy=RechargeDevices(storage,clock)

    def _save(self,login_id,record,generation):
        with self.lock:
            if self.generation.get(login_id,0)!=generation:
                raise RechargeDeviceError('充值设备准备已取消。','RISK_PREPARATION_CANCELLED',409)
            updated={**self.records,login_id:record}
            if self.storage:
                try:self.storage.save_activity_data(SCOPE,updated)
                except LoginStorageError as error:
                    raise RechargeDeviceError('充值设备凭据保存失败。','RISK_STORAGE_FAILED',500)from error
            self.records=updated

    def _mount(self,client,record):
        deadline=min(int(self.clock()+COOKIE_SECONDS),record['expires_at'])
        jar=client.cookies.jar
        for cookie in list(jar):
            if cookie.name==TOKEN_COOKIE and cookie.domain.lstrip('.')==DOMAIN and cookie.path=='/i':
                jar.clear(cookie.domain,cookie.path,cookie.name)
        jar.set_cookie(Cookie(version=0,name=TOKEN_COOKIE,value=record['token'],port=None,port_specified=False,
            domain=DOMAIN,domain_specified=False,domain_initial_dot=False,path='/i',path_specified=True,
            secure=True,expires=deadline,discard=False,comment=None,comment_url=None,rest={},rfc2109=False))
        return deadline

    def _request(self,login_id,client,*,previous_token=None):
        snapshot=self.credentials(login_id)
        if snapshot is None or not(snapshot.get('app_context')or{}).get('device_id'):
            raise RechargeDeviceError('此账号缺少已登记的 APP 客户端上下文。','RISK_CLIENT_CONTEXT_MISSING',409)
        registered=AppLoginSession.from_credentials(snapshot)
        envelope=registered.envelope({});package=envelope['packageName']
        started=int(self.clock()*1000)
        # Native app_info/build_strs collectors plus K56 AndroidId. Reuse the
        # software and installation identity actually sent during this client's
        # APP registration, not the desktop host or another account's identity.
        pairs={'K2':package,'K3':APP_CLIENT_APP_NAME,'K4':envelope['cv'],
            'K7':APP_CLIENT_LEGACY_SIGN_MD5,'K666':str(APP_CLIENT_APK_BYTES),'K667':APP_CLIENT_APP_NAME,
            'K850':str(APP_CLIENT_VERSION_CODE),'K11':envelope['sv'],'K906':str(APP_CLIENT_ANDROID_API),
            'K56':registered.initialization_body()['androidId'],
            'K451':str(started),'K452':str(int(self.clock()*1000))}
        _,body,sign=request_packet(APP_ID,pairs,package_name=package,previous_token=previous_token,
            software_version=APP_CLIENT_VERSION_NAME,software_version_code=APP_CLIENT_VERSION_CODE,android_api=APP_CLIENT_ANDROID_API)
        user_agent=f'LingxiWorkspace/{VERSION} (Python/{platform.python_version()}; {platform.system()} {platform.release()})'
        headers=client.headers
        client.headers={'User-Agent':user_agent,'Content-Type':'application/octet-stream','Accept':'*/*'}
        try:
            try:
                response=client.post(SERVER,params={'sign':sign,'appKey':APP_ID,'app':package,'version':SDK_VERSION},
                    data=body,timeout=(5,20),allow_redirects=False)
            except RequestException as error:
                raise RechargeDeviceError('官方原生设备校验连接失败。','RISK_NETWORK_FAILED')from error
        finally:client.headers=headers
        if response.status_code==429:
            raise RechargeDeviceError('官方提示设备请求频繁，请稍后主动重试。','RISK_RATE_LIMITED',429,remote_status=429)
        if response.status_code!=200:
            raise RechargeDeviceError('官方原生设备校验请求失败。','RISK_HTTP_FAILED',remote_status=response.status_code)
        try:
            outer,inner=response_packet(APP_ID,response.content)
            code=outer.get(3,[None])[0]
            if outer.get(1)!=[1002]or code!=0:
                raise RechargeDeviceError('官方原生设备校验未通过。','RISK_INITIALIZATION_FAILED',409,remote_status=code)
            raw=(inner or{}).get(1,[None])[0]
            if not isinstance(raw,bytes):raise ValueError('Missing native ticket')
            token=raw.decode('ascii')
            if not _cookie_value(token):raise ValueError('Invalid native ticket')
        except (ValueError,KeyError,IndexError,UnicodeError,BadZipFile)as error:
            raise RechargeDeviceError('官方原生设备校验响应无法识别。','RISK_RESPONSE_INVALID')from error
        return token

    def prepare(self,login_id,client,*,repair_registered_client=False):
        if not isinstance(login_id,str)or not login_id:raise ValueError('充值设备凭据必须绑定账号。')
        with self.lock:
            now=self.clock();generation=self.generation.get(login_id,0)
            cookie=next((c for c in client.cookies.jar if c.name==TOKEN_COOKIE and c.domain.lstrip('.')==DOMAIN
                and c.path=='/i'and c.secure and(c.expires is None or c.expires>now)and _cookie_value(c.value)),None)
            record=self.records.get(login_id)
            if(record and(record.get('account_key')!=login_id or record.get('provider')!=SERVER
                    or record.get('app_id')!=APP_ID or not _cookie_value(record.get('token')))):
                record=None
            completing=repair_registered_client and record is not None and record.get('request_context')!=CLIENT_CONTEXT
            if cookie is not None and login_id not in self.ignore_cookie and not completing:
                return {'prepared':True,'reused':True,'provider':'business_cookie','expires_at':cookie.expires}
            if record and record.get('expires_at',0)>now and not completing:
                deadline=self._mount(client,record)
                return {'prepared':True,'reused':True,'provider':'m1','expires_at':deadline,'ticket_expires_at':record['expires_at']}
            future=self.pending.get(login_id);owner=future is None
            if owner:future=Future();self.pending[login_id]=future
        if owner:
            try:
                token=(self._request(login_id,client,previous_token=record['token'])if record else self._request(login_id,client))
                issued=self.clock()
                record={'account_key':login_id,'provider':SERVER,'app_id':APP_ID,'token':token,
                    'issued_at':issued,'expires_at':int(issued+TICKET_SECONDS),'request_context':dict(CLIENT_CONTEXT)}
                self._save(login_id,record,generation);future.set_result(record)
            except Exception as error:
                future.set_exception(error);raise
            finally:
                with self.lock:
                    if self.pending.get(login_id)is future:del self.pending[login_id]
        else:record=future.result()
        with self.lock:
            if self.generation.get(login_id,0)!=generation:
                raise RechargeDeviceError('充值设备准备已取消。','RISK_PREPARATION_CANCELLED',409)
            self.ignore_cookie.discard(login_id);deadline=self._mount(client,record)
        return {'prepared':True,'reused':not owner,'provider':'m1','expires_at':deadline,'ticket_expires_at':record['expires_at']}

    def forget(self,login_id):
        with self.lock:
            self.generation[login_id]=self.generation.get(login_id,0)+1
            self.pending.pop(login_id,None);self.ignore_cookie.add(login_id)
            updated={key:value for key,value in self.records.items()if key!=login_id}
            if self.storage:self.storage.save_activity_data(SCOPE,updated)
            self.records=updated
        self.legacy.forget(login_id)

    def complete_registered_client(self,login_id,client):
        """One missing-context repair; complete, valid records still reuse."""
        return self.prepare(login_id,client,repair_registered_client=True)
