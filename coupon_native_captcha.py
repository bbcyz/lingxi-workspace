"""Python recharge verification: real device ticket, images and program records.

No browser runtime or browser-only fingerprint values are used. Only an
accepted official response supplies a success value, consumed by one order.
"""
import base64
from contextlib import closing
import math
import os
import secrets
import struct
import time
from urllib.parse import parse_qs,urljoin,urlsplit

if os.environ.get('LINGXI_ANDROID')!='1':
    import cv2
    import numpy as np
    IMAGE_ERRORS=(KeyError,TypeError,ValueError,cv2.error)
else:IMAGE_ERRORS=(KeyError,TypeError,ValueError)

from coupon_device import APP_ID,DOMAIN,TOKEN_COOKIE,_cookie_value
from http_transport import RequestException,requests
from coupon_recharge_device import RechargeDeviceError
from runtime_paths import VERSION
import platform


BASE='https://yxfk.market.chinamobile.com:8081/mgt/decision-captcha'
ALPHABET='XmYj3u1PnvisIZUF8ThR/a6DfO+kW4JHrCELycAzSxleoQp02MtwV9Nd57qGgbKB='


class RechargeCaptchaError(Exception):
    def __init__(self,message,code='CAPTCHA_FAILED',*,remote_code=None):
        super().__init__(message);self.code,self.remote_code=code,remote_code


def _transform(kind,data):
    result=bytearray()
    if kind=='sample':
        state=33265
        for value in data:state=(value^state)&255;result.append(state)
    elif kind=='sid':
        state=2422
        for value in data:
            result.append((value^state)&255);state+=2
            if state>=2147483647:state=2372
    elif kind=='time':result.extend((((value>>4)+(value<<4)+15273)&255)for value in data)
    else:
        key,index,step=(b'C6Br4b6f7NgK',44,4)if kind=='temp'else(b'VxMpoN86g7lA',32,3)
        for value in data:index=(index+step)%len(key);result.append(value^key[index])
    return bytes(result)


def _text(value):
    data=value.encode('ascii');return struct.pack('>H',len(data))+data


class ProgramPacket:
    """SDK wire encoding of actual program position updates, without human claims."""
    def __init__(self,sid,clock=time.time,monotonic=time.monotonic):
        self.data=bytearray();self.monotonic=monotonic;self.started=monotonic()
        self.append(1,_transform('time',struct.pack('>Q',int(clock()*1000))))
        self.append(8,_transform('version',struct.pack('>I',1)))
        self.append(9,_transform('sid',_text(sid)))

    def append(self,kind,payload):self.data.extend(struct.pack('>BH',kind,len(payload))+payload)

    def position(self,x,y):
        elapsed=int((self.monotonic()-self.started)*1000)
        self.append(17,_transform('sample',struct.pack('>IHH',elapsed,x,y)))

    def finish(self,x,y,speed):
        self.append(10,_transform('temp',_text(f'x={x}&y={y}&speed={speed:g}')))
        encoded=base64.b64encode(self.data).decode('ascii')
        return 's_v3#'+encoded.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=',ALPHABET))


def strip_order(order_key):
    units=order_key.encode('utf-16-le')
    order=[]
    for at in range(0,min(len(units),64),2):
        value=(units[at]|units[at+1]<<8)%32
        while value in order:value=(value+1)%32
        order.append(value)
    if len(order)!=32:raise RechargeCaptchaError('官方验证图片格式已变化。','CAPTCHA_IMAGE_FORMAT')
    return order


def restore_background(image,order_key):
    order=strip_order(order_key)
    width=image.shape[1]//len(order);restored=image.copy()
    for target,source in enumerate(order):
        restored[:,target*width:(target+1)*width]=image[:,source*width:(source+1)*width]
    return restored


def locate_slot(background,piece,official_y):
    """Combine alpha-boundary alignment and a distinct dark cutout.

    RGB template correlation is unsuitable: decorative green borders can give
    high scores on intact image content. Ambiguous candidates are not submitted.
    """
    height,width=piece.shape[:2]
    mask=(piece[:,:,3]>=128).astype(np.uint8)*255
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)
    if not contours:return None
    points=max(contours,key=cv2.contourArea)[:,0,:]
    gray=cv2.cvtColor(background,cv2.COLOR_BGR2GRAY)
    edges=cv2.Canny(cv2.GaussianBlur(gray,(3,3),0),50,150)
    distances=cv2.distanceTransform(255-edges,cv2.DIST_L2,5)
    kernel=np.ones((5,5),np.uint8)
    inside=cv2.erode(mask,kernel)>0;outside=(cv2.dilate(mask,kernel)>0)&(mask==0)
    if not np.any(inside)or not np.any(outside):return None
    return rank_slots(distances.tolist(),gray.tolist(),points.tolist(),np.argwhere(inside).tolist(),
        np.argwhere(outside).tolist(),height,width,official_y)


def rank_slots(distances,gray,points,inside,outside,height,width,official_y):
    """Shared Python ranking; Android supplies the same OpenCV primitive data."""
    if not inside or not outside:return None
    candidates=[]
    for x in range(len(gray[0])-width+1):
        best=None
        for y in range(max(0,official_y-4),min(len(gray)-height,official_y+4)+1):
            values=[distances[py+y][px+x]for px,py in points]
            item=(sum(values)/len(values),y,sum(v<=2 for v in values)/len(values))
            if best is None or item<best:best=item
        if best is None:continue
        mean,y,support=best
        if mean>1.5 or support<.8:continue
        contrast=(sum(gray[y+dy][x+dx]for dy,dx in outside)/len(outside)
            -sum(gray[y+dy][x+dx]for dy,dx in inside)/len(inside))
        if contrast>30:candidates.append({'x':x,'y':y,'distance':mean,'support':support,'contrast':contrast})
    separated=[]
    for candidate in sorted(candidates,key=lambda c:c['distance']):
        if all(abs(candidate['x']-old['x'])>=width*.6 for old in separated):separated.append(candidate)
    # A distractor may be darker yet have a visibly different boundary. Keep
    # the contour evidence rather than discarding it when brightness is tied.
    if separated:
        best=separated[0]
        if(best['distance']<=.8 and best['support']>=.97 and abs(best['y']-official_y)<=1
                and(len(separated)==1 or separated[1]['distance']-best['distance']>=.25)):
            return best
    separated.sort(key=lambda c:c['contrast'],reverse=True)
    if not separated or len(separated)>1 and separated[0]['contrast']-separated[1]['contrast']<20:return None
    return separated[0]


def locate_images(raw_images,order_key,official_y):
    if os.environ.get('LINGXI_ANDROID')=='1':
        from coupon_native_captcha_android import locate_images as android_images
        return android_images(raw_images,order_key,official_y)
    background=cv2.imdecode(np.frombuffer(raw_images[0],np.uint8),cv2.IMREAD_COLOR)
    piece=cv2.imdecode(np.frombuffer(raw_images[1],np.uint8),cv2.IMREAD_UNCHANGED)
    if(background is None or piece is None or piece.ndim!=3 or piece.shape[2]!=4):
        raise ValueError('Unsupported images')
    background=restore_background(background,order_key)
    raw_y=math.floor(official_y*background.shape[0]/150+.5)
    return locate_slot(background,piece,raw_y),background.shape[1]


class RechargeCaptcha:
    def __init__(self,*,devices=None,session_factory=None,clock=time.time,monotonic=time.monotonic,sleep=time.sleep):
        self.devices=devices
        self.session_factory=session_factory or(lambda:requests.Session(impersonate='chrome'))
        self.clock,self.monotonic,self.sleep=clock,monotonic,sleep

    def _request(self,client,method,url,**options):
        try:response=client.request(method,url,timeout=(5,15),allow_redirects=False,**options)
        except RequestException as error:
            raise RechargeCaptchaError('官方验证连接失败，请稍后主动重试。','CAPTCHA_NETWORK')from error
        if response.status_code==429:
            raise RechargeCaptchaError('官方提示验证请求频繁，请稍后主动重试。','CAPTCHA_RATE_LIMITED')
        if response.status_code!=200:
            raise RechargeCaptchaError('官方验证服务暂未完成请求。','CAPTCHA_HTTP',remote_code=response.status_code)
        return response

    def _result(self,response):
        try:value=response.json()
        except ValueError as error:raise RechargeCaptchaError('官方验证响应无法识别。','CAPTCHA_FORMAT')from error
        if not isinstance(value,dict)or value.get('success')is not True:
            raise RechargeCaptchaError('官方验证未通过，请稍后主动重试。','CAPTCHA_REJECTED',
                remote_code=value.get('code')if isinstance(value,dict)else None)
        return value

    @staticmethod
    def _callback(value,device):
        token=value.get('token')
        suffix=device if len(device)<=100 else''
        if not isinstance(token,str)or not token or len(token)+1+len(suffix)>4096:
            raise RechargeCaptchaError('官方未返回有效验证凭据。','CAPTCHA_TOKEN_MISSING')
        # Both the normal Basic and server-direct-success SDK paths do this.
        return token+':'+suffix

    def solve(self,client,*,login_id=None):
        if self.devices is None:return self._solve(client)
        if not isinstance(login_id,str)or not login_id:raise ValueError('图验设备必须绑定当前账号。')
        # The official recharge page uses native M1 for its Touch Cookie, but
        # initGraphCom configures a separate C1 ConstID for the captcha. Never
        # replace the order's native Cookie or copy APP/wallet cookies here.
        with closing(self.session_factory())as sdk:
            try:self.devices.prepare(login_id,sdk)
            except RechargeDeviceError as error:
                raise RechargeCaptchaError(str(error),error.code,remote_code=error.remote_status)from error
            return self._solve(sdk)

    def _solve(self,client):
        cookie=next((c for c in client.cookies.jar if c.name==TOKEN_COOKIE and c.domain.lstrip('.')==DOMAIN
            and c.path=='/i'and c.secure and(c.expires is None or c.expires>self.clock())and _cookie_value(c.value)),None)
        if cookie is None:raise RechargeCaptchaError('设备验证尚未就绪，请稍后重试。','CAPTCHA_DEVICE_REQUIRED')
        device=cookie.value;headers=client.headers
        client.headers={'User-Agent':f'LingxiWorkspace/{VERSION} (Python/{platform.python_version()}; {platform.system()} {platform.release()})',
            'Accept':'application/json, text/plain, */*'}
        try:
            params={'w':300,'h':150,'s':50,'sid':'','ak':APP_ID,'c':device,'jsv':'v1.4.0(104)',
                'aid':f'zw-{int(self.clock()*1000)}-{secrets.randbelow(100000000)}-0',
                'wp':0,'de':0,'uid':'','lf':0,'tpc':'','m':'','wtf':'false'}
            for attempt in range(2):
                params['_r']=secrets.randbelow(10**12)/10**12
                challenge=self._result(self._request(client,'GET',BASE+'/api/a',params=params.copy()))
                if challenge.get('result')==0:return self._callback(challenge,device)
                if challenge.get('result')!=1 or challenge.get('type')!=0:
                    raise RechargeCaptchaError('此次官方验证暂未自动完成，请稍后重试。','CAPTCHA_TYPE_UNSUPPORTED')
                # Basic initializes its SID-bound collector on receipt of the
                # challenge, before loading its images. Record that real time
                # here so position elapsed times include image preparation.
                try:
                    sid=challenge['sid'];packet=ProgramPacket(sid,self.clock,self.monotonic);images=[]
                    for key,kind in [('p1','0'),('p2','1')]:
                        url=urljoin(BASE+'/',challenge[key].lstrip('/'));parts=urlsplit(url);query=parse_qs(parts.query,keep_blank_values=True)
                        if(parts.scheme!='https'or parts.netloc!='yxfk.market.chinamobile.com:8081'
                            or parts.path!='/mgt/decision-captcha/api/p1'or query.get('sid')!=[sid]
                            or query.get('ak')!=[APP_ID]or query.get('imageType')!=[kind]):
                            raise ValueError('Challenge image binding differs')
                        images.append(self._request(client,'GET',url).content)
                    y=int(challenge['y']);candidate,background_width=locate_images(images,challenge['o'],y)
                except IMAGE_ERRORS as error:
                    raise RechargeCaptchaError('官方验证图片暂时无法识别。','CAPTCHA_IMAGE_FORMAT')from error
                if candidate is not None:break
                params['sid']=sid
            else:raise RechargeCaptchaError('此次图片未能可靠识别，请稍后重试。','CAPTCHA_IMAGE_AMBIGUOUS')
            image_x=candidate['x']*300/background_width
            # Basic clamps both bar and piece to width minus its 60px button.
            # A low multiplier cannot reach every right-hand cutout. Select
            # an SDK-supported multiplier whose actual bounded state matches.
            max_drag=300-60
            speeds=[value for value in (.9,1.1,1.2)
                if 10<=math.floor((image_x-10)/value+.5)<=max_drag
                and abs(10+min(math.floor((image_x-10)/value+.5)*value,max_drag)-image_x)<=2]
            if not speeds:
                raise RechargeCaptchaError('此次图片位置无法可靠匹配，请稍后重试。','CAPTCHA_IMAGE_AMBIGUOUS')
            speed=secrets.choice(speeds);bar_left=math.floor((image_x-10)/speed+.5);x=bar_left+10
            for step in range(1,21):
                self.sleep(.02);packet.position(30+math.floor(bar_left*step/20+.5),180)
            verified=self._result(self._request(client,'POST',BASE+'/api/v1',data={
                'ac':packet.finish(x,y,speed),'ak':APP_ID,'c':device,'uid':'','jsv':params['jsv'],
                'sid':sid,'aid':params['aid'],'x':x,'y':y}))
            return self._callback(verified,device)
        finally:client.headers=headers
