"""Official partner QR authentication in Python; no browser or connected phone."""
import base64
from io import BytesIO
import json
import re
import uuid
from urllib.parse import unquote,urljoin,urlsplit

import qrcode

from workspace.http_transport import requests
from .client import CheckinError

WEB_UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/142.0.0.0 Safari/537.36'


def json_reply(response):
    text=response.text.strip()
    start=text.find('{')
    try:value,_=json.JSONDecoder().raw_decode(text[start:]) if start>=0 else (None,0)
    except ValueError as error:raise CheckinError('官方扫码响应格式异常。','PARTNER_QR_FORMAT') from error
    if not isinstance(value,dict):raise CheckinError('官方扫码响应格式异常。','PARTNER_QR_FORMAT')
    return value


def taobao_data(reply):
    if reply.get('hasError'):raise CheckinError('淘宝扫码未通过，请检查官方验证提示。','VERIFICATION_REQUIRED',409)
    content=reply.get('content');data=content.get('data') if isinstance(content,dict) else None
    if not isinstance(data,dict):raise CheckinError('淘宝扫码响应格式已变化。','PARTNER_QR_FORMAT')
    return data


class PartnerQr:
    def __init__(self,service):
        self.service=service
        self.client=requests.Session(impersonate='chrome',headers={'User-Agent':WEB_UA,'Accept':'application/json'})
        self.state='waiting'
        self.guid=str(uuid.uuid4()).upper()
        self.form=self.apis=self.ticket=None

    def close(self):
        self.client.close()

    def _request(self,method,url,**kwargs):
        try:response=self.client.request(method,url,timeout=(10,40),allow_redirects=False,**kwargs)
        except requests.RequestsError as error:
            raise CheckinError('连接官方扫码服务失败，请稍后检查。','PARTNER_LOGIN_NETWORK') from error
        if response.status_code!=200:
            raise CheckinError('官方扫码服务暂未正常返回。','PARTNER_LOGIN_HTTP')
        return response

    def start(self):
        if self.service=='taobao':
            page=self._visit_taobao('https://login.taobao.com/member/login.jhtml')
            def assigned(name):
                match=re.search(r'(?:window\.)?'+name+r'\s*=\s*',page.text)
                if not match:raise CheckinError('官方淘宝登录配置已变化。','PARTNER_QR_FORMAT')
                try:return json.JSONDecoder().raw_decode(page.text[match.end():])[0]
                except ValueError as error:raise CheckinError('官方淘宝登录配置已变化。','PARTNER_QR_FORMAT') from error
            self.form=assigned('viewData')['loginFormData']
            self.apis=assigned('viewConfig')['api']
            self.client.headers.update({'Referer':page.url,'X-Requested-With':'XMLHttpRequest'})
            result=json_reply(self._request('GET',urljoin(page.url,self.apis['getQRCodeApi']),params=self.form))
            data=taobao_data(result)
            if not data.get('codeContent') or not all(key in data for key in ('t','ck')):
                raise CheckinError('淘宝暂未允许生成二维码，请完成官方要求的验证。','VERIFICATION_REQUIRED',409)
            self.ticket=data
            image=BytesIO();qrcode.make(self.ticket['codeContent']).save(image,format='PNG')
            return 'data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()
        self.client.headers['Referer']='https://passport.baidu.com/v2/?login'
        result=json_reply(self._request('GET','https://passport.baidu.com/v2/api/getqrcode',params={
            'lp':'pc','qrloginfrom':'pc','gid':self.guid,'tpl':'map','apiver':'v3','callback':'__partner_cb'}))
        if result.get('errno')!=0 or not result.get('sign') or not result.get('imgurl'):
            raise CheckinError('百度暂未允许生成二维码，请完成官方要求的验证。','VERIFICATION_REQUIRED',409)
        self.ticket=result
        image_url='https:'+result['imgurl'] if result['imgurl'].startswith('//') else 'https://'+result['imgurl'] if not result['imgurl'].startswith('https://') else result['imgurl']
        parts=urlsplit(image_url)
        if parts.netloc not in {'passport.baidu.com','wappass.baidu.com'}:
            raise CheckinError('百度二维码图片入口已变化。','PARTNER_QR_FORMAT')
        image=self._request('GET',image_url).content
        if not image.startswith((b'\x89PNG\r\n',b'\xff\xd8\xff',b'GIF8')):
            raise CheckinError('百度未返回有效二维码图片。','PARTNER_QR_FORMAT')
        mime='image/png' if image.startswith(b'\x89PNG') else 'image/jpeg' if image.startswith(b'\xff\xd8') else 'image/gif'
        return 'data:'+mime+';base64,'+base64.b64encode(image).decode()

    def _visit_taobao(self,url):
        url=urljoin('https://login.taobao.com/',url)
        for _ in range(6):
            parts=urlsplit(url)
            host=parts.hostname or ''
            if parts.scheme!='https' or parts.port not in (None,443) or not (host=='taobao.com' or host.endswith('.taobao.com')
                                             or host=='tmall.com' or host.endswith('.tmall.com')):
                raise CheckinError('官方登录返回了尚未支持的认证入口。','PARTNER_QR_ROUTE',409)
            try:response=self.client.get(url,timeout=(10,25),allow_redirects=False)
            except requests.RequestsError as error:raise CheckinError('官方登录确认连接失败。','PARTNER_LOGIN_NETWORK') from error
            if response.status_code in {301,302,303,307,308}:
                url=urljoin(url,response.headers['location']);continue
            if response.status_code!=200:raise CheckinError('官方登录确认尚未完成。','PARTNER_LOGIN_HTTP')
            return response
        raise CheckinError('官方登录确认跳转次数已超出支持范围。','PARTNER_QR_ROUTE',409)

    def poll(self):
        if self.state in {'confirmed','expired'}:return self.state
        if self.service=='taobao':
            reply=json_reply(self._request('POST',urljoin('https://login.taobao.com/',self.apis['checkQRCodeApi']),
                data={**self.form,'t':self.ticket['t'],'ck':self.ticket['ck']}))
            data=taobao_data(reply);status=data.get('qrCodeStatus')
            if status not in {'NEW','SCANED','CONFIRMED','EXPIRED','CANCELED','ERROR'}:
                raise CheckinError('淘宝扫码状态已变化，请重新获取并核对。','PARTNER_QR_FORMAT')
            if status in {'EXPIRED','CANCELED'}:self.state='expired'
            elif status=='SCANED':self.state='scanned'
            elif status=='ERROR':raise CheckinError('淘宝要求额外验证，尚未更新登录态。','VERIFICATION_REQUIRED',409)
            elif status=='CONFIRMED':
                for key in ('asyncUrls','miniVsts'):
                    urls=data.get(key) or []
                    for url in urls.values() if isinstance(urls,dict) else urls:self._visit_taobao(url)
                for flag,key in [('redirect','redirectUrl'),('parentRedirect','parentRedirectUrl'),('iframeRedirect','iframeRedirectUrl'),('replace','replaceUrl')]:
                    if data.get(flag) and data.get(key):self._visit_taobao(data[key])
                self.state='confirmed'
            return self.state
        reply=json_reply(self._request('GET','https://passport.baidu.com/channel/unicast',params={
            'channel_id':self.ticket['sign'],'gid':self.guid,'tpl':'map','_sdkFrom':1,'callback':'__partner_cb'}))
        channel=reply.get('channel_v') or {}
        if isinstance(channel,str):
            try:channel=json.loads(channel)
            except ValueError:raise CheckinError('百度扫码确认格式已变化。','PARTNER_QR_FORMAT')
        if not isinstance(channel,dict):raise CheckinError('百度扫码确认格式已变化。','PARTNER_QR_FORMAT')
        if str(reply.get('errno'))=='0' and str(channel.get('status'))=='0':
            result=json_reply(self._request('GET','https://passport.baidu.com/v3/login/main/qrbdusslogin',params={
                'bduss':channel['v'],'u':unquote(channel.get('u') or 'https://map.baidu.com/'),'loginVersion':'v4','qrcode':1,
                'tpl':'map','callback':'__partner_cb'}))
            if str(result.get('errInfo',{}).get('no'))!='0':
                raise CheckinError('百度要求额外验证，尚未更新登录态。','VERIFICATION_REQUIRED',409)
            self.state='confirmed'
        elif str(reply.get('errno'))=='0' and str(channel.get('status'))=='1':self.state='scanned'
        elif str(reply.get('errno'))=='0' and str(channel.get('status'))=='2':self.state='expired'
        return self.state
