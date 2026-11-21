import base64
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote

from activities.net_age_member_state import MemberPageState, PREFIX
from activities.net_age_transport import NetAgeSession
from activities.net_age_protocol import PythonWheel, MEMBER_PROFILE, MEMBER_H5_HEADERS


def decode(value):
    raw=unquote(value)[7:]
    return ''.join(c if c in 'AaZz@{' else chr(ord(c)^1) for c in raw)


class MemberStateTests(unittest.TestCase):
    def test_initial_storage_matches_the_original_sdk_fixture(self):
        state=MemberPageState([],'182****1270')
        cookies={c['name']:c['value'] for c in state.cookies()}
        # Captured from the original SDK's AES alias and initial event sequence.
        self.assertEqual(decode(cookies[PREFIX+'cs1']),'55jFP42WXJ7Qt2RtSxrAEQ==')
        self.assertEqual(json.loads(unquote(cookies[PREFIX+'sequence_ids'])),{'globalKey':4,'VISIT':3,'PAGE':2})
        stored=json.loads(next(iter(state.local_storage.values())))
        visit=json.loads(decode(stored['value']))
        self.assertEqual(visit['deviceId'],decode(cookies['gdp_user_id']))
        self.assertEqual(visit['sessionId'],cookies[PREFIX+'session_id'])
        self.assertEqual(visit['gioId'],decode(cookies[PREFIX+'gio_id']))
        self.assertEqual(visit['globalSequenceId'],3)
        self.assertNotIn('User-Token',cookies)
        self.assertNotIn('RiskToken',cookies)

    def test_restart_reuses_device_and_live_session_without_new_ids(self):
        first=MemberPageState([],'182****1270')
        with patch('activities.net_age_member_state.uuid4',side_effect=AssertionError('unnecessary ID regeneration')):
            again=MemberPageState(first.cookies(),'182****1270')
        self.assertEqual(first.state['device'],again.state['device'])
        self.assertEqual(first.state['session'],again.state['session'])
        self.assertEqual(first.local_storage,again.local_storage)
        self.assertEqual(again.state['sequence']['PAGE'],first.state['sequence']['PAGE']+1)
        self.assertEqual(again.state['sequence']['VISIT'],first.state['sequence']['VISIT'])

    def test_expired_analytics_session_does_not_replace_the_device(self):
        first=MemberPageState([],'182****1270')
        saved=deepcopy(first.cookies())
        visitor=next(c for c in saved if c['name']=='gdp_user_id')
        visitor['net_age_sdk']['session_expires']=0
        renewed=MemberPageState(saved,'182****1270')
        self.assertEqual(first.state['device'],renewed.state['device'])
        self.assertNotEqual(first.state['session'],renewed.state['session'])
        self.assertNotEqual(first.local_storage.keys(),renewed.local_storage.keys())

    def test_account_alias_changes_do_not_reuse_another_accounts_session(self):
        first=MemberPageState([],'182****1270')
        other=MemberPageState(first.cookies(),'188****8967')
        self.assertNotEqual(first.state['alias'],other.state['alias'])
        self.assertNotEqual(first.state['session'],other.state['session'])
        self.assertEqual(first.state['device'],other.state['device'])

    def test_official_clock_is_bound_to_the_current_account(self):
        session=NetAgeSession(member=True)
        try:
            session.net_age_user_id='12345678901234567'
            issued=1795243188693
            def response(uid):
                return Mock(headers={'unused':None}) if not uid else Mock(headers=Mock(
                    get_list=Mock(return_value=['RiskToken='+str(issued)+'a'*32+uid+'B'*32+'; Max-Age=1800']),
                    __getitem__=Mock(return_value=format_datetime(datetime.fromtimestamp(issued//1000,timezone.utc),usegmt=True))))
            with patch('activities.net_age_transport.time.monotonic',side_effect=[100.04,100.09]):
                session._observe_clock(response(session.net_age_user_id),100)
                self.assertEqual(session.business_time_ms(),issued+70)
            original=session._clock_sample
            session._observe_clock(response('76543210987654321'),200)
            self.assertEqual(session._clock_sample,original)
        finally:session.close()

    def test_collection_time_and_signed_request_time_use_the_same_clock(self):
        wheel=PythonWheel(MEMBER_PROFILE,clock=lambda:1795243188693)
        wheel.prepare([],MEMBER_H5_HEADERS['User-Agent'],{'page_url':'https://dev.coc.10086.cn/','local_storage':{}})
        requests=[]
        def prepare(payload,environment,*args,**kwargs):
            requests.append((deepcopy(environment),kwargs['timestamp']))
            return {'data':{'cocEnContent':'fixture'},'headers':{'coc-aurora':'fixture'}}
        with patch('activities.net_age_protocol.prepare',side_effect=prepare):
            wheel.encrypt({},[],MEMBER_H5_HEADERS['User-Agent'],transport=lambda *_:{'code':'0','data':'device'})
        for environment,stamp in requests:
            self.assertEqual(environment['collection_time'],stamp)
            self.assertEqual(environment['init_time']+environment['time_elapsed'],stamp)
        wheel.close()


if __name__=='__main__':unittest.main()
