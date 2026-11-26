import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from features.checkin.partner_qr import PartnerQr,json_reply
from features.checkin.client import CheckinError


def reply(value):return SimpleNamespace(text=json.dumps(value))


class PartnerQrTests(unittest.TestCase):
    def test_jsonp_is_parsed_without_executing_javascript(self):
        response=SimpleNamespace(text='callback('+json.dumps({'errno':0,'sign':'synthetic'})+');')
        self.assertEqual(json_reply(response)['errno'],0)
        with self.assertRaises(CheckinError):json_reply(SimpleNamespace(text='<html>verification required</html>'))

    def test_taobao_pending_scan_then_confirmation_follows_only_official_grants(self):
        flow=PartnerQr('taobao');self.addCleanup(flow.close)
        flow.form={'_csrf':'synthetic'};flow.apis={'checkQRCodeApi':'/test/query'};flow.ticket={'t':'time','ck':'challenge'}
        flow._request=Mock(side_effect=[reply({'hasError':False,'content':{'data':{'qrCodeStatus':'NEW'}}}),
            reply({'hasError':False,'content':{'data':{'qrCodeStatus':'SCANED'}}}),
            reply({'hasError':False,'content':{'data':{'qrCodeStatus':'CONFIRMED','asyncUrls':['https://login.taobao.com/test/grant']}}})])
        flow._visit_taobao=Mock()
        self.assertEqual(flow.poll(),'waiting');self.assertEqual(flow.poll(),'scanned');self.assertEqual(flow.poll(),'confirmed')
        flow._visit_taobao.assert_called_once_with('https://login.taobao.com/test/grant')
        count=flow._request.call_count;self.assertEqual(flow.poll(),'confirmed');self.assertEqual(count,flow._request.call_count)

    def test_untrusted_grant_url_is_rejected_before_sending(self):
        flow=PartnerQr('taobao');self.addCleanup(flow.close);flow.client.get=Mock()
        with self.assertRaises(CheckinError):flow._visit_taobao('https://taobao.com.evil.example/?ticket=synthetic')
        flow.client.get.assert_not_called()

    def test_taobao_login_entry_follows_the_current_official_redirect(self):
        flow=PartnerQr('taobao');self.addCleanup(flow.close)
        target='https://login.taobao.com/havanaone/login/login.htm'
        page='window.viewData='+json.dumps({'loginFormData':{'_csrf':'test-only'}})+';window.viewConfig='+json.dumps({'api':{'getQRCodeApi':'/test/generate','checkQRCodeApi':'/test/query'}})+';'
        flow.client.get=Mock(side_effect=[SimpleNamespace(status_code=302,headers={'location':target}),
            SimpleNamespace(status_code=200,headers={},url=target,text=page)])
        flow._request=Mock(return_value=reply({'content':{'data':{'t':'time','ck':'challenge','codeContent':'https://login.taobao.com/test-only'}}}))
        self.assertTrue(flow.start().startswith('data:image/png;base64,'))
        self.assertEqual(flow.client.get.call_args.args[0],target)

    def test_baidu_confirmation_exchanges_the_official_ticket_before_success(self):
        flow=PartnerQr('baidu');self.addCleanup(flow.close);flow.ticket={'sign':'channel'}
        flow._request=Mock(side_effect=[reply({'errno':0,'channel_v':json.dumps({'status':1})}),
            reply({'errno':0,'channel_v':json.dumps({'status':0,'v':'official-ticket','u':'https%3A%2F%2Fmap.baidu.com%2F'})}),
            reply({'errInfo':{'no':'0'}})])
        self.assertEqual(flow.poll(),'scanned');self.assertEqual(flow.poll(),'confirmed')
        final=flow._request.call_args
        self.assertIn('/qrbdusslogin',final.args[1]);self.assertEqual(final.kwargs['params']['u'],'https://map.baidu.com/')

    def test_baidu_cancelled_confirmation_never_exchanges_a_ticket(self):
        flow=PartnerQr('baidu');self.addCleanup(flow.close);flow.ticket={'sign':'channel'}
        flow._request=Mock(return_value=reply({'errno':0,'channel_v':json.dumps({'status':'2'})}))
        self.assertEqual(flow.poll(),'expired');flow._request.assert_called_once()

    def test_changed_official_qr_shape_returns_an_actionable_error(self):
        flow=PartnerQr('taobao');self.addCleanup(flow.close)
        flow.form={};flow.apis={'checkQRCodeApi':'/test/query'};flow.ticket={'t':'time','ck':'challenge'}
        flow._request=Mock(return_value=reply({'content':{'data':{'qrCodeStatus':'UNRECOGNIZED'}}}))
        with self.assertRaises(CheckinError) as error:flow.poll()
        self.assertEqual(error.exception.code,'PARTNER_QR_FORMAT')


if __name__=='__main__':unittest.main()
