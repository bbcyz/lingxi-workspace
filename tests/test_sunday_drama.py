from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from io import BytesIO
from pathlib import Path
from threading import RLock
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from flask import Flask
from activities.sunday_drama.client import DramaClient, DEFAULT_CAMPAIGN, ENTRY, invitation_link
from activities.sunday_drama.scheduler import DramaScheduler, SCOPE
from activities.sunday_drama.video import DramaVideo
from activities.sunday_drama.visits import TargetVisits
from features.star_day.auth import StarDaySessionCache
from features.checkin.client import CheckinError
from recharge.cmcc import SHANGHAI

NOW = datetime(2026, 11, 25, 13, tzinfo=SHANGHAI)
TODAY = datetime.now(SHANGHAI).date()
VIDEO_PERIOD = (TODAY - timedelta(days=TODAY.weekday())).isoformat()


class Memory:
    def __init__(self): self.values = {}
    def load_activity_data(self, key): return deepcopy(self.values.get(key))
    def save_activity_data(self, key, value): self.values[key] = deepcopy(value)


class FakeClient:
    def __init__(self):
        self.campaign = deepcopy(DEFAULT_CAMPAIGN)
        self.raw = {'tasks': [dict(taskId='owned-a', name='浏览签到活动', type='browse', launchSign='normal',
            taskStage='UNDO', description='浏览页面', earnOpportunity=1)], 'remaining': {'remain': 2},
            'video': dict(doneDayCount=1, canDayCount=1, doneWeekCount=1, canWeekCount=8, undoTodayTaskList=[]),
            'prizes': {'data': []}, 'invite': {'available': True, 'count': 0, 'prizes': [], 'session': {'id': 1}}}
        self.browsed = self.draws = self.received = self.observations = 0
        self.lose_result = self.fail_verification = self.reject = False
    @contextmanager
    def connect(self): yield self
    def observe(self, **_kwargs):
        self.observations += 1
        if self.reject: raise CheckinError('该账号不符合活动资格', 'INELIGIBLE', 403)
        if self.fail_verification and self.browsed:
            self.fail_verification = False
            raise CheckinError('查询暂时失败', 'UPSTREAM_NETWORK')
        return deepcopy(self.raw)
    def browse(self, task, *, before_submit, **_kwargs):
        before_submit('start'); self.browsed += 1
        if self.lose_result: raise CheckinError('响应丢失', 'RESULT_UNCERTAIN', uncertain=True)
        self.raw['tasks'][0]['taskStage'] = 'DONE'; self.raw['remaining']['remain'] += 1
    def receive(self, task, *, before_submit):
        before_submit(); self.received += 1; self.raw['tasks'][0]['taskStage'] = 'DONE'
    def draw(self, *, before_submit):
        before_submit(); self.draws += 1; self.raw['remaining']['remain'] -= 1


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.client = Memory(), FakeClient()
        self.accounts = lambda: [dict(id='a', status='active', phone_masked='138****0001')]
        self.scheduler = self.make()
    def make(self):
        scheduler = DramaScheduler(self.accounts, lambda _id:self.client, Mock(), self.storage, now=lambda:NOW)
        scheduler.data['settings']['video_enabled'] = False
        self.addCleanup(scheduler.shutdown)
        return scheduler
    def test_done_task_is_not_replayed(self):
        self.scheduler._work('a', 'tasks', None, None, False)
        self.scheduler._work('a', 'tasks', None, None, False)
        self.assertEqual(self.client.browsed, 1)
        self.assertFalse(self.scheduler.data['pending'])
        self.assertEqual(self.scheduler.status()['accounts'][0]['observation']['remaining']['remain'], 3)
    def test_uncertain_write_survives_restart_without_replay(self):
        self.client.lose_result = True
        self.scheduler._work('a', 'tasks', None, None, False)
        restored = self.make()
        restored._work('a', 'tasks', None, None, False)
        self.assertEqual(self.client.browsed, 1)
        self.assertTrue(restored.data['pending']['a'])
        self.client.lose_result = False
        self.client.raw['tasks'][0]['taskStage'] = 'DONE'
        restored._refresh('a')
        self.assertFalse(restored.data['pending'])
    def test_failed_readback_does_not_clear_submitted_intent(self):
        self.client.fail_verification = True
        self.scheduler._work('a', 'tasks', None, None, False)
        self.assertTrue(self.scheduler.data['pending']['a'])
        self.assertEqual(self.scheduler.data['records']['a'][0]['state'], 'uncertain')
        self.scheduler._refresh('a')
        self.assertFalse(self.scheduler.data['pending'])
    def test_unknown_automatic_result_has_bounded_read_only_checks(self):
        self.client.lose_result=True
        self.scheduler.executor.submit=lambda fn,*args:fn(*args)
        self.scheduler.tick()
        self.scheduler.now=lambda:NOW+timedelta(minutes=5)
        self.scheduler.tick()
        self.scheduler.now=lambda:NOW+timedelta(minutes=10)
        self.scheduler.tick()
        reads=self.client.observations
        self.scheduler.now=lambda:NOW+timedelta(minutes=15)
        self.scheduler.tick()
        self.assertEqual(self.client.browsed,1)
        self.assertEqual(self.client.observations,reads)
        self.assertTrue(self.scheduler.data['pending']['a'])
        self.assertEqual(self.scheduler.data['records']['a'][0]['state'],'uncertain')
    def test_wait_receive_is_claimed_and_verified(self):
        self.client.raw['tasks'][0]['taskStage'] = 'WAIT_RECEIVE'
        self.scheduler._work('a', 'tasks', None, None, False)
        self.assertEqual(self.client.received, 1)
        self.assertEqual(self.client.browsed, 0)
        self.assertFalse(self.scheduler.data['pending'])
    def test_wrong_account_or_stale_task_cannot_submit(self):
        with self.assertRaises(CheckinError): self.scheduler.queue('other', 'tasks')
        self.scheduler._work('a', 'task', 'other-owner-token', None, False)
        self.assertEqual(self.client.browsed, 0)
    def test_weekday_never_submits_draw(self):
        self.scheduler._work('a', 'draw', None, None, False)
        self.assertEqual(self.client.draws, 0)
    def test_manual_draw_uses_one_and_automatic_draw_uses_remaining(self):
        self.scheduler.now = lambda:NOW+timedelta(days=4)
        self.scheduler.quit.wait = Mock(return_value=False)
        self.scheduler._work('a', 'draw', None, None, False)
        self.assertEqual(self.client.draws, 1)
        self.scheduler._work('a', 'draw', None, None, True)
        self.assertEqual(self.client.draws, 2)
        self.assertEqual(self.client.raw['remaining']['remain'], 0)
    def test_ineligible_automatic_check_once_daily_including_restart(self):
        self.client.reject = True
        self.scheduler.executor.submit = lambda fn,*args:fn(*args)
        self.scheduler.tick(); self.scheduler.tick()
        self.assertEqual(self.client.observations, 1)
        restored = self.make();restored.executor.submit = lambda fn,*args:fn(*args)
        restored.tick();self.assertEqual(self.client.observations, 1)
    def test_restored_login_catches_up(self):
        self.scheduler._change(lambda data:data['automatic'].update({'a:tasks':{'date':NOW.date().isoformat(), 'complete':True, 'code':'APP_LOGIN_REQUIRED'}}))
        self.scheduler.executor.submit = lambda fn,*args:fn(*args)
        self.scheduler.login_restored('a')
        self.assertEqual(self.client.browsed, 1)
    def test_invite_does_not_claim_below_threshold(self):
        self.client.invite_claim = Mock()
        self.client.raw['invite']['prizes'] = [dict(id=1, name='1GB券', stage=1, useStatus='WITHOUT')]
        self.scheduler._work('a', 'invite_claim', None, '1', False)
        self.client.invite_claim.assert_not_called()
    def test_unknown_invite_claim_does_not_expire_on_new_week_same_month(self):
        self.scheduler._before('a', 'invite_claim', prize_id='1')
        self.scheduler.now = lambda:NOW+timedelta(days=5)
        self.scheduler._store('a',self.client.raw,self.scheduler.now().isoformat(),self.client.campaign)
        self.assertTrue(self.scheduler.data['pending']['a'])
    def test_public_status_never_exposes_video_tokens(self):
        self.scheduler.data['video_leases']['a']={'detail':{'cToken':'private-lease'}}
        self.assertNotIn('private-lease',str(self.scheduler.status()))
    def test_playback_does_not_hold_shared_submission_lock(self):
        self.scheduler._before('a','browse',task_name='浏览签到活动')
        self.assertTrue(self.scheduler.busy_login('a'))
        self.scheduler.submitted('a')
        self.assertFalse(self.scheduler.busy_login('a'))
        self.assertTrue(self.scheduler.data['pending']['a'])
    def test_negative_qualification_does_not_block_other_tasks_or_repeat_automatically(self):
        task=deepcopy(self.client.raw['tasks'][0]);task.update(taskId='v5',name='浏览网龄成长计划活动')
        self.client.raw['tasks'].insert(0,task)
        attempts=[]
        def browse(row,*,before_submit,**_kw):
            attempts.append(row['name'])
            if row['name']=='浏览网龄成长计划活动':raise CheckinError('官方等级为 V4，仅限 V5','TASK_INELIGIBLE',403)
            before_submit('start');next(t for t in self.client.raw['tasks'] if t['taskId']==row['taskId'])['taskStage']='DONE'
        self.client.browse=browse
        self.scheduler._work('a','tasks',None,None,True)
        self.scheduler._work('a','tasks',None,None,True)
        self.assertEqual(attempts.count('浏览网龄成长计划活动'),1)
        self.assertIn('浏览签到活动',attempts)
        self.assertEqual(self.scheduler.data['observations']['a']['tasks'][0]['qualification'],'ineligible')
        self.scheduler._work('a','task','v5',None,False)
        self.assertEqual(attempts.count('浏览网龄成长计划活动'),2)
    def test_temporary_qualification_failure_is_retried_same_day(self):
        self.client.raw['tasks'][0].update(name='浏览网龄成长计划活动')
        attempts=[]
        def browse(row,*,before_submit,**_kw):
            attempts.append(1)
            if len(attempts)==1:raise CheckinError('等级暂未返回','QUALIFICATION_UNCONFIRMED')
            before_submit('start');self.client.raw['tasks'][0]['taskStage']='DONE'
        self.client.browse=browse;self.scheduler.executor.submit=lambda fn,*args:fn(*args)
        self.scheduler.tick();self.scheduler.tick();self.assertEqual(len(attempts),1)
        self.scheduler.now=lambda:NOW+timedelta(minutes=5);self.scheduler.tick()
        self.assertEqual(len(attempts),2)
        self.assertFalse(self.scheduler.data['task_failures'].get('a'))
    def test_waiting_identity_does_not_poll_network_until_identity_changes(self):
        self.client.raw['tasks'][0].update(name='体验全球通礼遇');attempts=[]
        tag={'value':''}
        def browse(row,*,before_submit,**_kw):
            attempts.append(1)
            if not tag['value']:
                error=CheckinError('等待已有身份','GIFT_SESSION_WAITING',409);error.identity_tag='';raise error
            before_submit('start');self.client.raw['tasks'][0]['taskStage']='DONE'
        self.client.browse=browse;self.scheduler.gift_ready=lambda _id:tag['value']
        self.scheduler.executor.submit=lambda fn,*args:fn(*args)
        self.scheduler.tick();self.scheduler.tick();self.assertEqual(len(attempts),1)
        tag['value']='real-new-cookie';self.scheduler.tick();self.scheduler.tick()
        self.assertEqual(len(attempts),2)
    def test_unknown_follow_only_resumes_after_reading_true_follow_state(self):
        self.scheduler._before('a','author',task_name='关注优质作者',phase='follow',author_id=10)
        self.scheduler.video_factory=Mock(return_value=SimpleNamespace(author_status=lambda:{'isFollower':1}))
        self.scheduler._resolve_author('a');self.assertFalse(self.scheduler.data['pending'])
        self.scheduler._before('a','author',task_name='关注优质作者',phase='callback',author_id=10)
        self.scheduler._resolve_author('a');self.assertTrue(self.scheduler.data['pending']['a'])
    def test_follow_readback_cannot_remove_newer_callback_intent(self):
        self.scheduler._before('a','author',task_name='关注优质作者',phase='follow',author_id=10)
        def profile():
            self.scheduler._before('a','author',task_name='关注优质作者',phase='callback',author_id=10)
            return {'isFollower':1}
        self.scheduler.video_factory=Mock(return_value=SimpleNamespace(author_status=profile))
        self.scheduler._resolve_author('a')
        self.assertEqual(self.scheduler.data['pending']['a']['phase'],'callback')
    def test_membership_query_never_claims_a_reward(self):
        self.scheduler.membership_reader=Mock(return_value=[])
        result=self.scheduler.membership('a');self.assertFalse(result['reward_verified'])
        self.assertEqual(self.client.browsed,0);self.assertEqual(self.client.draws,0)


class ProtocolTests(unittest.TestCase):
    def author_video(self,followed):
        video=DramaVideo(lambda:{},Mock())
        @contextmanager
        def session():yield Mock(),{}
        video._session=session
        profile={'authorId':10,'authorName':'中国移动','isFollower':int(followed)}
        detail={'data':dict(status='1',cToken='private',backUrl='https://wx.10086.cn/qwhdhub/diy-client/1126082530')}
        values=[profile,detail]
        if not followed:values += [True,{**profile,'isFollower':1}]
        values += [{'data':True}]
        video._call=Mock(side_effect=values)
        return video
    def test_existing_follow_is_reused_without_follow_or_unfollow(self):
        video=self.author_video(True);before=Mock()
        result=video.complete_author({'taskId':'a','param':'owned'},load_lease=lambda:None,save_lease=Mock(),before_submit=before)
        self.assertTrue(result['already_followed'])
        self.assertFalse(any(c.args[2]=='user/customer/following' for c in video._call.call_args_list))
        before.assert_called_once_with('callback')
    def test_author_follow_is_verified_before_reward_callback(self):
        video=self.author_video(False);before=Mock()
        result=video.complete_author({'taskId':'a','param':'owned'},load_lease=lambda:None,save_lease=Mock(),before_submit=before)
        self.assertFalse(result['already_followed'])
        calls=video._call.call_args_list
        self.assertEqual(calls[2].args[3],{'authorId':10,'status':1})
        self.assertEqual(calls[3].args[2],'user/author/queryAuthorInfo')
        self.assertEqual(calls[4].args[2],'advert/lottery/followAuthorTaskFinish')
        self.assertEqual([c.args[0] for c in before.call_args_list],['follow','callback'])
    def test_unverified_follow_never_sends_callback(self):
        video=self.author_video(False)
        video._call.side_effect=[{'authorId':10,'authorName':'中国移动','isFollower':0},
            {'data':dict(status='1',cToken='private',backUrl='https://wx.10086.cn/qwhdhub/diy-client/1126082530')},True,
            {'authorId':10,'isFollower':0}]
        with self.assertRaises(CheckinError):
            video.complete_author({'taskId':'a','param':'owned'},load_lease=lambda:None,save_lease=Mock(),before_submit=Mock())
        self.assertEqual(video._call.call_count,4)
    def test_entry_context_reused_across_clients_without_sso_or_revision_churn(self):
        route={}; session=SimpleNamespace(headers={},cookies=SimpleNamespace(jar=[SimpleNamespace(name='QWHD_SESSION_TOKEN',value='same-valid-auth')]))
        @contextmanager
        def connected():yield session,{'id':'a','credential_revision':1}
        auth=SimpleNamespace(_session=connected,_follow=Mock(return_value=SimpleNamespace(url=ENTRY)))
        def save(value):route.update(value)
        for _ in range(2):
            client=DramaClient(lambda:{},None,auth=auth,load_route=lambda:route,save_route=save)
            with client.connect():pass
        auth._follow.assert_called_once()
        session.cookies.jar[0].value='new-official-auth'
        with DramaClient(lambda:{},None,auth=auth,load_route=lambda:route,save_route=save).connect():pass
        self.assertEqual(auth._follow.call_count,2)

    def test_invite_link_must_be_the_official_owned_invitation(self):
        url='https://wx.10086.cn/qwhdhub/diy-client/1126082530?inviteId=owned&verCode=code'
        self.assertEqual(invitation_link('点击邀请 '+url+'。'),url)
        self.assertEqual(invitation_link('https://evil.example/?inviteId=owned&verCode=code'),'')
        self.assertEqual(invitation_link(ENTRY),'')

    def test_normal_browse_visits_destination_without_duplicate_finish(self):
        session=Mock()
        session.get.side_effect=[SimpleNamespace(status_code=302,headers={'location':'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301'}),SimpleNamespace(status_code=200)]
        auth=SimpleNamespace(_current=Mock())
        client=DramaClient(lambda:{},None,auth=auth,wait=Mock());client.session=session;client.snapshot={}
        client.campaign['browse_seconds']={'浏览签到活动':5}
        task=dict(taskId='owned',name='浏览签到活动',type='browse',launchSign='normal',url='/qwhdhub/diyTask/startBrowse/owned')
        marker=Mock();client.browse(task,before_submit=marker)
        self.assertEqual(session.get.call_count,2);session.request.assert_not_called()
        marker.assert_called_once_with('start');self.assertGreater(client.wait.call_args.args[0],4)
    def test_video_uses_300_real_seconds_despite_info_scan_time_5(self):
        video=DramaVideo(lambda:{},Mock())
        @contextmanager
        def session():yield Mock(),{}
        video._session=session
        video._call=Mock(side_effect=[dict(videoUrl='https://activity.cmcc-cs.cn/clip.mp4',videoSource='0'),{'data':True}])
        player=Mock(return_value={'played_seconds':300,'frames':7500});before=Mock()
        lease={'task_id':'a','period':VIDEO_PERIOD,'detail':dict(status='1',scanTime='5',cToken='private',backUrl='https://wx.10086.cn/qwhdhub/diy-client/1126082530')}
        result=video.complete_drama({'taskId':'a','param':'owned'},load_lease=lambda:lease,save_lease=Mock(),before_submit=before,player=player)
        self.assertEqual(player.call_args.args[1],300);self.assertEqual(result['frames'],7500);before.assert_called_once()
    def test_failed_playback_never_reports_completion(self):
        video=DramaVideo(lambda:{},Mock())
        @contextmanager
        def session():yield Mock(),{}
        video._session=session;video._call=Mock(return_value=dict(videoUrl='https://activity.cmcc-cs.cn/clip.mp4',videoSource='0'))
        lease={'task_id':'a','period':VIDEO_PERIOD,'detail':dict(status='1',scanTime=5,cToken='private',backUrl='https://wx.10086.cn/qwhdhub/diy-client/1126082530')}
        before=Mock()
        with self.assertRaises(CheckinError):
            video.complete_drama({'taskId':'a'},load_lease=lambda:lease,save_lease=Mock(),before_submit=before,player=Mock(side_effect=CheckinError('播放失败')))
        before.assert_not_called();self.assertEqual(video._call.call_count,1)
    def test_invite_claim_route_requires_explicit_confirmation(self):
        from activities.sunday_drama.plugin import register
        app=Flask(__name__);app.extensions['checkin_scheduler']=Mock()
        scheduler=Mock();scheduler.queue.return_value={'queued':1}
        with patch('activities.sunday_drama.plugin.DramaScheduler',return_value=scheduler):register(app,None,None,None)
        client=app.test_client();payload={'id':'a','kind':'invite_claim','prize_id':'1'}
        self.assertEqual(client.post('/api/sunday-drama/run',json=payload).status_code,400)
        scheduler.queue.assert_not_called()
        self.assertEqual(client.post('/api/sunday-drama/run',json={**payload,'confirmed':True}).status_code,200)


class PackageUpgradeTests(unittest.TestCase):
    def test_builtin_and_imported_update_keep_the_same_data_namespace(self):
        from app import create_app
        from activity_packages import ActivityPackageStorage, read_manifest, export_package
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'accounts.sqlite3'
            first=create_app(database_path=database)
            self.addCleanup(lambda:[p['controller'].stop(wait=False) for p in first.extensions['platform_manager'].platforms.values() if p.get('controller')])
            scheduler=first.extensions['platform_manager'].platforms[SCOPE]['controller'].scheduler
            self.assertIsInstance(scheduler.storage,ActivityPackageStorage)
            scheduler._change(lambda data:data['route_tags'].update({'probe':{'auth_tag':'keep'}}))
            self.assertIsNone(first.extensions['checkin_beans'].storage.load_activity_data(SCOPE))
            manifest=read_manifest(Path('activities/sunday_drama').resolve())
            next_version='.'.join(map(str,(*manifest.version_key[:2],manifest.version_key[2]+1)))
            original=export_package(manifest).getvalue()
            output=BytesIO()
            with ZipFile(BytesIO(original)) as old,ZipFile(output,'w') as new:
                for name in old.namelist():
                    contents=old.read(name)
                    if name.endswith('manifest.json'):
                        value=json.loads(contents);value['version']=next_version;contents=json.dumps(value).encode()
                    new.writestr(name,contents)
            first.extensions['platform_manager'].install(output.getvalue())
            second=create_app(database_path=database)
            self.addCleanup(lambda:[p['controller'].stop(wait=False) for p in second.extensions['platform_manager'].platforms.values() if p.get('controller')])
            loaded=second.extensions['platform_manager'].platforms[SCOPE]
            self.assertEqual(loaded['manifest'].version,next_version)
            self.assertEqual(loaded['controller'].scheduler.data['route_tags']['probe']['auth_tag'],'keep')


class VisitPolicyTests(unittest.TestCase):
    def target(self):
        self.star=SimpleNamespace(sessions=Mock(),sms=SimpleNamespace(pending={}),busy_login=Mock(return_value=False))
        self.star.sessions.load.return_value=None
        self.star.sessions.account_lock.return_value=RLock()
        return TargetVisits(lambda:{'id':'a','phone':'13800000001','primary_status':'active'},Mock(),self.star,now=lambda:1000)
    def test_background_gift_does_not_create_star_identity(self):
        target=self.target();target.cache.connect.side_effect=lambda scope,snap,client,initialize,**kw:initialize()
        drama=SimpleNamespace(snapshot={'id':'a','phone':'13800000001'},automatic=True,_call=Mock())
        with self.assertRaises(CheckinError) as error:
            with target.gift_session(drama):pass
        self.assertEqual(error.exception.code,'GIFT_SESSION_WAITING');drama._call.assert_not_called()
    def test_qqt_profile_must_match_current_account(self):
        target=self.target();client=Mock();client.post.return_value=SimpleNamespace(status_code=200,json=lambda:{'status':'1','data':{'phone':'139****0002','userLevel':'4'}})
        with self.assertRaises(CheckinError) as error:target._profile(client,{'phone':'13800000001'})
        self.assertEqual(error.exception.code,'ACCOUNT_MISMATCH')
    def test_global_tone_distinguishes_qualification_from_missing_data(self):
        target=self.target();target._gift_context=object()
        for level,name,code in [('5','全球通会员',''),('-1','非全球通用户','TASK_INELIGIBLE'),('','', 'QUALIFICATION_UNCONFIRMED')]:
            with self.subTest(level=level):
                target._gift_profile={'userLevel':level,'levelName':name}
                if code:
                    with self.assertRaises(CheckinError) as error:target.global_tone(None)
                    self.assertEqual(error.exception.code,code)
                else:self.assertEqual(target.global_tone(None)['tier'],'5')
    def test_borrowed_star_identity_keeps_original_two_hour_deadline(self):
        cache=StarDaySessionCache(now=lambda:10000)
        client=SimpleNamespace(cookies=SimpleNamespace(jar=[]),headers={})
        saved=cache.save('a','13800000001',client,created_at=4000)
        self.assertEqual(saved['created_at'],4000);self.assertEqual(saved['expires_at'],11200)
    def test_v5_reads_actual_level_and_reuses_scope(self):
        target=self.target();client=Mock();client.user={'maskedMobile':'138****0001','provinceCode':'851'}
        client.post.return_value={'level':'V4'}
        with patch('activities.sunday_drama.visits.NetAgeClient',return_value=client):
            with self.assertRaises(CheckinError) as error:target.v5()
        self.assertEqual(error.exception.code,'TASK_INELIGIBLE');client.close.assert_called_once()
    def test_empty_growth_is_unconfirmed_not_a_negative_qualification(self):
        target=self.target();client=Mock();client.user={'maskedMobile':'138****0001','provinceCode':'851'};client.post.return_value={'level':''}
        with patch('activities.sunday_drama.visits.NetAgeClient',return_value=client):
            with self.assertRaises(CheckinError) as error:target.v5()
        self.assertEqual(error.exception.code,'QUALIFICATION_UNCONFIRMED')


if __name__=='__main__':unittest.main()
