import base64,json,os,struct,subprocess,sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import cv2
import numpy as np

from coupon_device import APP_ID
from coupon_native_captcha import RechargeCaptcha,RechargeCaptchaError,strip_order,restore_background,locate_slot,ALPHABET
from http_transport import requests


ORDER=[16,17,18,19,20,21,22,23,24,25,1,2,3,4,5,6,26,27,28,29,30,31,0,7,8,9,10,11,12,13,14,15]
KEY='0123456789abcdef0123456789abcdef'


def images(*,ambiguous=False,gap_x=190,near_shape=False):
    background=np.full((150,300,3),190,np.uint8)
    piece=np.zeros((50,50,4),np.uint8)
    polygon=np.array([[7,12],[34,3],[46,35],[15,47]],np.int32)
    cv2.fillPoly(piece,[polygon],(90,120,160,255));cv2.polylines(piece,[polygon],True,(20,240,20,255),3)
    alpha=(piece[:,:,3]>=128).astype(np.uint8)*255
    outlines,_=cv2.findContours(alpha,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)
    for x,color in [(55,(0,0,0)if near_shape else(10,10,10)if ambiguous else(170,170,170)),(gap_x,(10,10,10))]:
        region=background[45:95,x:x+50]
        silhouette=cv2.erode(alpha,np.ones((3,3),np.uint8))if near_shape and x==55 else alpha
        boundary,_=cv2.findContours(silhouette,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)
        region[silhouette>0]=color;cv2.drawContours(region,boundary,-1,(75,75,75),1)
    scrambled=background.copy();width=300//32
    for target,source in enumerate(ORDER):scrambled[:,source*width:(source+1)*width]=background[:,target*width:(target+1)*width]
    return background,piece,cv2.imencode('.png',scrambled)[1].tobytes(),cv2.imencode('.png',piece)[1].tobytes()


class SolverTests(unittest.TestCase):
    def fixture(self,*,ambiguous_first=False,always_ambiguous=False,auto=False,rejected=False,rate=False,bad_binding=False,gap_x=190,image_latency=0):
        clear=images(gap_x=gap_x);uncertain=images(ambiguous=True);clock=[10000.0];calls=[];state={'apply':0}
        http=SimpleNamespace(headers={'User-Agent':'business-fixture','Origin':'https://touch.10086.cn'},cookies=requests.Cookies())
        http.cookies.set('stc-rrt-token','D'*40,domain='touch.10086.cn',path='/i',secure=True)
        def request(method,url,**options):
            calls.append((method,url,options,http.headers.copy()))
            if url.endswith('/api/a'):
                if rate:return SimpleNamespace(status_code=429)
                state['apply']+=1;sid='sid-'+str(state['apply'])
                if auto:return SimpleNamespace(status_code=200,json=lambda:{'success':True,'result':0,'token':'S'*53})
                value={'success':True,'result':1,'type':0,'sid':sid,'o':KEY,'y':45,
                    'p1':f'/api/p1?sid={sid}&ak={APP_ID}&imageType=0',
                    'p2':f'/api/p1?sid={sid}&ak={APP_ID}&imageType=1'}
                if bad_binding:value['p1']='https://unrelated.invalid/image.png'
                return SimpleNamespace(status_code=200,json=lambda:value)
            if '/api/p1?'in url:
                clock[0]+=image_latency
                selected=uncertain if always_ambiguous or ambiguous_first and state['apply']==1 else clear
                return SimpleNamespace(status_code=200,content=selected[2 if 'imageType=0'in url else 3])
            self.assertTrue(url.endswith('/api/v1'))
            if rejected:value={'success':False,'code':'4011','msg':'HIGH_RISK'}
            else:value={'success':True,'token':'S'*53}
            return SimpleNamespace(status_code=200,json=lambda:value)
        http.request=Mock(side_effect=request)
        solver=RechargeCaptcha(clock=lambda:clock[0],monotonic=lambda:clock[0],sleep=lambda delta:clock.__setitem__(0,clock[0]+delta))
        return solver,http,calls

    def test_original_sdk_permutation_fixture_and_original_tail_pixels(self):
        # ORDER was evaluated from the retained official Grd8 module in a pure
        # JavaScript sandbox; no browser/runtime objects were supplied.
        self.assertEqual(strip_order(KEY),ORDER)
        original=np.zeros((1,98,3),np.uint8)
        for i in range(32):original[:,i*3:(i+1)*3]=i
        original[:,96:]=99
        actual=restore_background(original,KEY)
        self.assertEqual([int(actual[0,i*3,0])for i in range(32)],ORDER)
        self.assertTrue(np.all(actual[:,96:]==99))

    def test_dark_cutout_is_selected_over_matching_light_shape(self):
        background,piece,_,_=images();candidate=locate_slot(background,piece,45)
        self.assertIsNotNone(candidate);self.assertLessEqual(abs(candidate['x']-190),1)

    def test_equal_dark_candidates_are_not_sent_as_an_arbitrary_answer(self):
        background,piece,_,_=images(ambiguous=True)
        self.assertIsNone(locate_slot(background,piece,45))

    def test_exact_boundary_wins_over_a_darker_near_shape_distractor(self):
        background,piece,_,_=images(near_shape=True)
        candidate=locate_slot(background,piece,45)
        self.assertIsNotNone(candidate);self.assertLessEqual(abs(candidate['x']-190),1)

    def test_solver_preserves_sid_device_binding_and_returns_complete_94_character_callback(self):
        solver,http,calls=self.fixture();headers=http.headers
        with patch('coupon_native_captcha.secrets.choice',return_value=1.1):result=solver.solve(http)
        self.assertEqual(result,'S'*53+':'+'D'*40);self.assertEqual(len(result),94)
        self.assertIs(http.headers,headers)
        verified=[r for r in calls if r[0]=='POST'];self.assertEqual(len(verified),1)
        body=verified[0][2]['data'];self.assertEqual((body['sid'],body['c']),('sid-1','D'*40))
        self.assertLessEqual(abs((body['x']-10)*1.1+10-190),2)
        raw=base64.b64decode(body['ac'].split('#',1)[1].translate(str.maketrans(ALPHABET,'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=')))
        blocks=[];at=0
        while at<len(raw):
            kind,length=struct.unpack_from('>BH',raw,at);at+=3;blocks.append((kind,raw[at:at+length]));at+=length
        sid=next(data for kind,data in blocks if kind==9)
        decoded=bytes(value^((2422+2*i)&255)for i,value in enumerate(sid))
        self.assertEqual(decoded[2:].decode(),body['sid'])
        temp=next(data for kind,data in blocks if kind==10);key=b'C6Br4b6f7NgK';index=44;decoded=bytearray()
        for value in temp:index=(index+4)%len(key);decoded.append(value^key[index])
        self.assertEqual(decoded[2:].decode(),f"x={body['x']}&y=45&speed=1.1")
        self.assertEqual(sum(kind==17 for kind,_ in blocks),20)
        self.assertIn('Python/',verified[0][3]['User-Agent'])

    def test_one_image_refresh_precedes_single_verification_when_first_image_is_ambiguous(self):
        solver,http,calls=self.fixture(ambiguous_first=True)
        self.assertEqual(solver.solve(http),'S'*53+':'+'D'*40)
        apply=[r for r in calls if r[1].endswith('/api/a')];verified=[r for r in calls if r[0]=='POST']
        self.assertEqual((len(apply),len(verified)),(2,1))
        self.assertEqual(apply[1][2]['params']['sid'],'sid-1')
        self.assertEqual(verified[0][2]['data']['sid'],'sid-2')

    def test_position_elapsed_time_includes_this_challenges_real_image_loading(self):
        solver,http,calls=self.fixture(image_latency=.25)
        solver.solve(http)
        body=next(row[2]['data']for row in calls if row[0]=='POST')
        raw=base64.b64decode(body['ac'].split('#',1)[1].translate(str.maketrans(ALPHABET,'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=')))
        at=0;positions=[]
        while at<len(raw):
            kind,size=struct.unpack_from('>BH',raw,at);at+=3;payload=raw[at:at+size];at+=size
            if kind==17:
                decoded=[];previous=33265
                for value in payload:decoded.append((value^previous)&255);previous=value
                positions.append(struct.unpack('>IHH',bytes(decoded)))
        self.assertGreaterEqual(positions[0][0],519)
        self.assertGreaterEqual(positions[-1][0],899)

    def test_right_hand_cutout_does_not_produce_an_out_of_bounds_slider_state(self):
        solver,http,calls=self.fixture(gap_x=246)
        with patch('coupon_native_captcha.secrets.choice',side_effect=lambda values:values[0])as choice:
            self.assertEqual(solver.solve(http),'S'*53+':'+'D'*40)
        self.assertNotIn(.9,choice.call_args.args[0])
        body=next(row[2]['data']for row in calls if row[0]=='POST')
        self.assertLessEqual(body['x'],250)
        speed=choice.call_args.args[0][0]
        self.assertLessEqual(abs(10+min((body['x']-10)*speed,240)-246),2)

    def test_ambiguous_images_have_a_fixed_bound_and_never_reach_verification(self):
        solver,http,calls=self.fixture(always_ambiguous=True)
        with self.assertRaises(RechargeCaptchaError)as error:solver.solve(http)
        self.assertEqual(error.exception.code,'CAPTCHA_IMAGE_AMBIGUOUS')
        self.assertEqual(sum(r[1].endswith('/api/a')for r in calls),2)
        self.assertFalse(any(r[0]=='POST'for r in calls))

    def test_server_rejection_and_rate_limit_stop_without_a_new_challenge_or_success_token(self):
        for options,code in [({'rejected':True},'CAPTCHA_REJECTED'),({'rate':True},'CAPTCHA_RATE_LIMITED')]:
            solver,http,calls=self.fixture(**options);headers=http.headers
            with self.assertRaises(RechargeCaptchaError)as error:solver.solve(http)
            self.assertEqual(error.exception.code,code);self.assertIs(http.headers,headers)
            self.assertEqual(sum(r[1].endswith('/api/a')for r in calls),1)

    def test_server_direct_pass_also_uses_official_device_suffix_and_does_not_fetch_images(self):
        solver,http,calls=self.fixture(auto=True)
        self.assertEqual(solver.solve(http),'S'*53+':'+'D'*40);self.assertEqual(len(calls),1)
        self.assertEqual(RechargeCaptcha._callback({'token':'S'*53},'D'*101),'S'*53+':')

    def test_c1_captcha_uses_own_client_and_preserves_the_orders_native_ticket(self):
        solver,sdk,calls=self.fixture(auto=True);sdk.cookies.clear();sdk.close=Mock()
        business=SimpleNamespace(headers={'User-Agent':'bound-order-client'},cookies=requests.Cookies())
        business.cookies.set('stc-rrt-token','N'*40,domain='touch.10086.cn',path='/i',secure=True)
        business.cookies.set('jsessionid-cmcc','private-wallet',domain='.10086.cn',path='/')
        original_headers=business.headers
        devices=Mock()
        def prepare(owner,client):
            self.assertEqual(owner,'account-A');self.assertIs(client,sdk)
            self.assertFalse(list(client.cookies.jar))
            client.cookies.set('stc-rrt-token','C'*40,domain='touch.10086.cn',path='/i',secure=True)
        devices.prepare.side_effect=prepare;solver.devices=devices;solver.session_factory=lambda:sdk
        result=solver.solve(business,login_id='account-A')
        self.assertEqual(result,'S'*53+':'+'C'*40)
        self.assertEqual(calls[0][2]['params']['c'],'C'*40)
        self.assertEqual(business.cookies.get('stc-rrt-token'),'N'*40)
        self.assertEqual(business.cookies.get('jsessionid-cmcc'),'private-wallet')
        self.assertIs(business.headers,original_headers);sdk.close.assert_called_once()
        devices.prepare.assert_called_once_with('account-A',sdk)

    def test_c1_preparation_rejection_does_not_fall_back_to_native_or_request_a_challenge(self):
        from coupon_recharge_device import RechargeDeviceError
        solver,sdk,calls=self.fixture();sdk.close=Mock()
        solver.devices=Mock();solver.devices.prepare.side_effect=RechargeDeviceError('请求频繁','RISK_RATE_LIMITED',remote_status=-800)
        solver.session_factory=lambda:sdk
        with self.assertRaises(RechargeCaptchaError)as failed:solver.solve(SimpleNamespace(),login_id='account-A')
        self.assertEqual((failed.exception.code,failed.exception.remote_code),('RISK_RATE_LIMITED',-800))
        self.assertEqual(calls,[]);sdk.close.assert_called_once()

    def test_mismatched_image_origin_is_rejected_before_fetching_it(self):
        solver,http,calls=self.fixture(bad_binding=True)
        with self.assertRaises(RechargeCaptchaError)as error:solver.solve(http)
        self.assertEqual(error.exception.code,'CAPTCHA_IMAGE_FORMAT');self.assertEqual(len(calls),1)

    def test_android_module_import_does_not_require_desktop_cv2_or_numpy(self):
        code="""import coupon_device,http_transport,os,builtins
os.environ['LINGXI_ANDROID']='1'
original=builtins.__import__
def import_module(name,*args,**kwargs):
 if name.split('.')[0] in ('cv2','numpy'):raise AssertionError('Desktop image module imported on Android')
 return original(name,*args,**kwargs)
builtins.__import__=import_module
import coupon_native_captcha
print('loaded')
"""
        result=subprocess.run([sys.executable,'-X','utf8','-c',code],text=True,encoding='utf-8',capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'loaded')


if __name__=='__main__':unittest.main()
