"""Baidu Map's published gold-mall H5 protocol, with an imported own login.

No phone, native bridge, or browser is used by this adapter. The homepage must
confirm the imported account before issuing its per-request signing material.
"""
from contextlib import contextmanager
import hashlib
import json
import time
from urllib.parse import urlsplit

from workspace.http_transport import requests
from .client import CheckinError

TASK_NAME = '去百度地图领现金'
TASK = ('6', '1', 'baidumap')
SCOPE = 'checkin:baidu-map'
ENTRY = 'https://map.baidu.com/goldmall/index.html'
HOME = 'https://newclient.map.baidu.com/goldmall/index/shopindex'
CALLBACK = 'https://newclient.map.baidu.com/client/incentive/externaltask/event_report'
USER_AGENT = 'Mozilla/5.0 (Linux; Android 16; 2210132C) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Mobile Safari/537.36 baidumap_ANDR 22.0.0'


def sign_callback(parameters, token, timestamp):
    values = {**parameters, 't': int(timestamp), 'ctime_str': token['ctime_str']}
    plain = '&'.join(f'{key}={values[key]}' for key in sorted(values)) + token['key']
    return {**values, 'sign': hashlib.md5(plain.encode()).hexdigest()}


class CheckinBaiduMap:
    def __init__(self, credentials, cache):
        self.credentials, self.cache = credentials, cache

    def _request(self, client, url, parameters, *, writing=False):
        try:
            response = client.get(url, params=parameters, timeout=(10,20), allow_redirects=False)
        except requests.RequestsError as error:
            raise CheckinError('百度地图连接中断，请核对任务结果。', 'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK',
                               uncertain=writing) from error
        if response.status_code != 200:
            raise CheckinError(f'百度地图 HTTP {response.status_code}，未确认任务结果。',
                               'RESULT_UNCERTAIN' if writing else 'UPSTREAM_HTTP', uncertain=writing)
        try:
            value = response.json()
        except ValueError as error:
            raise CheckinError('百度地图响应格式异常。', 'UPSTREAM_FORMAT', uncertain=writing) from error
        if not isinstance(value,dict):
            raise CheckinError('百度地图响应格式异常。', 'UPSTREAM_FORMAT', uncertain=writing)
        return value

    @contextmanager
    def _session(self):
        snapshot = self.credentials()
        with requests.Session(impersonate='chrome', headers={'User-Agent':USER_AGENT,'Referer':ENTRY}) as client:
            def missing():
                raise CheckinError('该账号尚无有效的本人百度地图授权，请先同步一次登录。', 'BAIDU_MAP_LOGIN_REQUIRED', 401)
            self.cache.connect(SCOPE,snapshot,client,missing,expiry_cookies=('BDUSS','BDUSS_BFESS'))
            yield client

    def complete(self, item, info, *, before_submit):
        if ((str(item['taskCategory']),str(item['taskType']),item['jumpUrl']) != TASK
                or urlsplit(info['backUrl']).netloc != 'wx.10086.cn'
                or urlsplit(info['backUrl']).path != '/qwhdhub/qwhdmark/1021122301'):
            raise CheckinError('百度地图任务入口已变化，未提交回传。', 'TASK_CHANGED', 409)
        with self._session() as client:
            started = time.monotonic()
            identity = client._credential_data
            context = {**identity['phone_info'], 'task_from':'zgyd', 'from':'ChinaMobiletask', 'user_token':info['cToken']}
            page = client.get(ENTRY, params={key:context[key] for key in ('task_from','from','user_token')},
                              timeout=(10,20),allow_redirects=False)
            if page.status_code != 200:
                raise CheckinError('百度地图任务页面未成功返回。', 'TASK_PAGE_FAILED')
            home = self._request(client,HOME,{**context,'version':2,'activity':'goldcoinshop'})
            if home.get('errno') != 0:
                raise CheckinError('百度地图尚未返回有效任务资料。', 'BAIDU_MAP_QUERY_REJECTED', 409)
            data = home['data']
            if data['is_login'] != 1:
                self.cache.invalidate(client)
                raise CheckinError('百度地图本人登录已失效，请重新同步一次授权。', 'BAIDU_MAP_LOGIN_REQUIRED', 401)
            if str(data['uid']) != identity['uid']:
                raise CheckinError('百度地图账号与导入身份不一致，未提交任务。', 'ACCOUNT_MISMATCH', 409)
            time.sleep(max(0,float(info['scanTime'])-(time.monotonic()-started)))
            callback = sign_callback({'app':2, 'task_from':'zgyd', 'user_token':info['cToken'],
                'phone_info':json.dumps(context,ensure_ascii=False,separators=(',',':')),
                'cuid':context['cuid']},data['token'],time.time())
            before_submit()
            reply = self._request(client,CALLBACK,callback,writing=True)
            result = reply.get('result')
            if not isinstance(result,dict) or result.get('error') != 0:
                error = CheckinError('百度地图尚未确认任务回传，请查询官方进度。', 'BAIDU_MAP_CALLBACK_REJECTED', 409,
                                     uncertain=not isinstance(result,dict))
                error.definitive_rejection = isinstance(result,dict)
                raise error
            self.cache.sync(client)
            return {'login_verified':True,'business_callback_verified':True}
