"""Taobao HYM partner task using an imported own login and Python MTOP."""
from contextlib import contextmanager
import hashlib
import json
import time
from urllib.parse import quote, urlsplit

from workspace.http_transport import requests
from .client import CheckinError

TASK_NAME = '去薅羊毛免费领话费'
TASK = ('6','1','yangmao')
SCOPE = 'partner:taobao'
ENTRY = 'https://pages-fast.m.taobao.com/wow/z/app/wt/hym/home'
PROFILE = 'mtop.alicom.dz.tt.member.info.head.withSource'
CALLBACK = 'mtop.alicom.dz.partner.callback'
APP_KEY = '12574478'
TOKEN_ERRORS = {'FAIL_SYS_TOKEN_EMPTY','FAIL_SYS_TOKEN_EXOIRED','FAIL_SYS_TOKEN_EXPIRED'}


def request_parameters(api, data, token, timestamp):
    text = json.dumps(data,ensure_ascii=False,separators=(',',':'))
    stamp = str(int(timestamp*1000))
    sign = hashlib.md5((token+'&'+stamp+'&'+APP_KEY+'&'+text).encode()).hexdigest()
    return {'jsv':'2.7.3','appKey':APP_KEY,'t':stamp,'sign':sign,'api':api,'v':'1.0',
            'type':'originaljson','dataType':'json','data':text}


class CheckinTaobao:
    def __init__(self, credentials, cache):
        self.credentials, self.cache = credentials, cache

    def _call(self, client, api, data, *, writing=False, recovered=False):
        cookies = [c for c in client.cookies.jar if c.name=='_m_h5_tk'
                   and 'h5api.m.taobao.com'.endswith(c.domain.lstrip('.'))
                   and (c.expires is None or c.expires>time.time())]
        token = max(cookies,key=lambda c:len(c.domain)).value.split('_')[0] if cookies else ''
        try:
            response = client.get('https://h5api.m.taobao.com/h5/'+api.lower()+'/1.0/',
                params=request_parameters(api,data,token,time.time()),timeout=(10,20),allow_redirects=False)
        except requests.RequestsError as error:
            raise CheckinError('淘宝请求中断，请核对任务结果。','RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK',
                               uncertain=writing) from error
        if response.status_code != 200:
            raise CheckinError(f'淘宝 HTTP {response.status_code}，未确认任务结果。','UPSTREAM_HTTP',uncertain=writing)
        try:
            value = response.json()
        except ValueError as error:
            raise CheckinError('淘宝接口响应格式异常。','UPSTREAM_FORMAT',uncertain=writing) from error
        if not isinstance(value,dict) or not isinstance(value.get('ret'),list):
            raise CheckinError('淘宝接口响应格式异常。','UPSTREAM_FORMAT',uncertain=writing)
        codes = {str(v).split('::')[0] for v in value['ret']}
        self.cache.sync(client)
        if codes & TOKEN_ERRORS and not writing and not recovered:
            return self._call(client,api,data,recovered=True)
        if 'SUCCESS' not in codes:
            auth = 'FAIL_SYS_SESSION_EXPIRED' in codes
            verification = 'FAIL_SYS_USER_VALIDATE' in codes
            if auth:
                self.cache.invalidate(client)
            error = CheckinError('淘宝登录已失效，请恢复授权后重试。' if auth else '淘宝要求完成官方验证。' if verification
                else '淘宝未允许本次请求（'+','.join(sorted(codes))[:120]+'）。',
                'TAOBAO_LOGIN_REQUIRED' if auth else 'VERIFICATION_REQUIRED' if verification else 'TAOBAO_REJECTED',401 if auth else 409)
            error.definitive_rejection = writing
            raise error
        return value['data']

    @contextmanager
    def _session(self):
        snapshot = self.credentials()
        with requests.Session(impersonate='chrome',headers={
                'User-Agent':'Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Mobile Safari/537.36',
                'Referer':ENTRY,'Origin':'https://pages-fast.m.taobao.com'}) as client:
            def missing():
                raise CheckinError('该账号尚无有效的本人淘宝登录，请先同步一次授权。','TAOBAO_LOGIN_REQUIRED',401)
            self.cache.connect(SCOPE,snapshot,client,missing,expiry_cookies=('cookie2','sgcookie'))
            yield client

    def complete(self,item,info,*,before_submit):
        back = urlsplit(info['backUrl'])
        if ((str(item['taskCategory']),str(item['taskType']),item['jumpUrl'])!=TASK
                or (back.netloc,back.path)!=('wx.10086.cn','/qwhdhub/qwhdmark/1021122301')):
            raise CheckinError('淘宝任务入口已变化，未提交回调。','TASK_CHANGED',409)
        target = ENTRY+'?x-ssr=true&disableNav=YES&status_bar_transparent=true&subSource=yidong&token='+quote(info['cToken'],safe="~()*!.'-")
        with self._session() as client:
            began = time.monotonic()
            profile = self._call(client,PROFILE,{'subSource':'yidong','fromHym':'true'})
            if profile.get('code')!='0000':
                raise CheckinError('淘宝未确认本人会员资料，未提交任务。','TAOBAO_LOGIN_REQUIRED',401)
            page = client.get(target,timeout=(10,20),allow_redirects=False)
            if page.status_code!=200:
                raise CheckinError('淘宝任务页面未成功返回。','TASK_PAGE_FAILED')
            time.sleep(max(0,float(info['scanTime'])-(time.monotonic()-began)))
            before_submit()
            result = self._call(client,CALLBACK,{'subSource':'yidong','requestUrl':target},writing=True)
            if str(result.get('result')).lower()!='true':
                raise CheckinError('淘宝合作回调结果待核实，请先查询移动任务进度。','RESULT_UNCERTAIN',409,uncertain=True)
            return {'login_verified':True,'business_callback_verified':True}
