"""Account-isolated weekly tasks, immediate reads and durable submission intents."""
from copy import deepcopy
from datetime import datetime, timedelta
import re
from threading import Event, RLock, Thread

from features.checkin.client import CheckinError
from recharge.cmcc import SHANGHAI
from workspace.feature_runtime import ThreadPoolExecutor, feature_running
from workspace.accounts.storage import LoginStorageError
from workspace.http_transport import requests
from .client import BROWSE_TASKS, DEFAULT_CAMPAIGN, invitation_link
from .video import AUTHOR_TASK, AUTHOR_ID

SCOPE = 'sunday-drama'
SETTINGS = {'enabled': True, 'task_time': '00:05', 'draw_time': '00:10', 'video_enabled': True}
LIMITATIONS = {
    '浏览爱奇艺热播综艺': '该视频任务的真实观看和独立回传尚未接通。',
}
SUPPORT = '|'.join(sorted([*BROWSE_TASKS, AUTHOR_TASK]))


def week(now):
    return (now.date() - timedelta(days=now.weekday())).isoformat()


def clean_message(value):
    return re.sub(r'1[3-9]\d{9}', lambda m: m[0][:3]+'****'+m[0][-4:], str(value))[:500]


class DramaScheduler:
    def __init__(self, accounts, factory, video_factory, storage, *, now=None, gift_ready=lambda _id: '', membership_reader=lambda _id: []):
        self.accounts, self.factory, self.video_factory, self.storage = accounts, factory, video_factory, storage
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.gift_ready = gift_ready
        self.membership_reader = membership_reader
        self.lock, self.quit = RLock(), Event()
        self.running, self.reading, self.read_again, self.submitting, self.cancelled = set(), set(), set(), set(), set()
        self.thread = None
        self.share_locks = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix=SCOPE)
        self.read_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix=SCOPE+'-read')
        self.data = {'settings': dict(SETTINGS), 'campaign': deepcopy(DEFAULT_CAMPAIGN),
                     'observations': {}, 'records': {}, 'pending': {}, 'automatic': {},
                     'video_leases': {}, 'author_leases': {}, 'invite_assets': {}, 'route_tags': {}, 'task_failures': {}, 'memberships': {}}
        saved = storage.load_activity_data(SCOPE) if storage else None
        if saved is not None:
            if not isinstance(saved, dict):
                raise LoginStorageError('周日追剧盛宴记录格式无效，已保留原数据。')
            saved.setdefault('route_tags', {})
            saved.setdefault('task_failures', {})
            saved.setdefault('author_leases', {})
            saved.setdefault('memberships', {})
            if any(not isinstance(saved.get(k), dict) for k in self.data):
                raise LoginStorageError('周日追剧盛宴记录格式无效，已保留原数据。')
            self.data = saved

    def _change(self, edit):
        with self.lock:
            updated = deepcopy(self.data)
            edit(updated)
            if self.storage:
                self.storage.save_activity_data(SCOPE, updated)
            self.data = updated

    def metadata(self):
        with self.lock:
            campaign = deepcopy(self.data['campaign'])
        now = self.now()
        start, end = datetime.fromisoformat(campaign['start_at']), datetime.fromisoformat(campaign['end_at'])
        return {**campaign, 'active': start <= now <= end, 'ended': now > end, 'server_time': now.isoformat()}

    def status(self):
        with self.lock:
            data = deepcopy(self.data)
            rows = [{**a, 'observation': data['observations'].get(a['id']),
                     'membership':data['memberships'].get(a['id']),
                     'records': data['records'].get(a['id'], []), 'pending': bool(data['pending'].get(a['id'])),
                     'running': a['id'] in self.running, 'refreshing': a['id'] in self.reading}
                    for a in self.accounts()]
        return {'accounts': rows, 'settings': data['settings'], 'campaign': self.metadata(),
                'server_time': self.now().isoformat()}

    def settings(self, payload):
        if (not isinstance(payload, dict) or type(payload.get('enabled')) is not bool
                or type(payload.get('video_enabled')) is not bool
                or any(not isinstance(payload.get(k), str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', payload[k])
                       for k in ('task_time', 'draw_time'))):
            raise CheckinError('请填写是否自动执行及正确的北京时间。', 'INVALID_SETTINGS', 400)
        self._change(lambda data: data.update(settings={k: payload[k] for k in SETTINGS}))
        return {'message': '设置已保存，覆盖全部有效共享账号。'}

    def _record(self, login_id, state, message, **extra):
        if login_id in self.cancelled:
            return
        row = {'at': self.now().isoformat(), 'state': state, 'message': clean_message(message), **extra}
        def edit(data):
            data['records'][login_id] = [row, *data['records'].get(login_id, [])][:80]
        self._change(edit)

    def _store(self, login_id, raw, stamp, campaign):
        if login_id in self.cancelled:
            return
        rows = []
        for item in raw['tasks']:
            supported = (item['name'] in BROWSE_TASKS and item['type'] == 'browse' and item['launchSign'] == 'normal'
                         or item['name'] == AUTHOR_TASK and item['type'] == 'business')
            rows.append({k: item.get(k) for k in ('taskId', 'name', 'description', 'type', 'todayRemain', 'maxPerDay', 'taskStage', 'earnOpportunity')}
                        | {'automatic': supported, 'reason': '' if supported else LIMITATIONS.get(item['name'], '该任务尚未接通自动执行。')})
            if item['name'] == AUTHOR_TASK:
                rows[-1]['reason'] = '使用官方推荐作者“中国移动”；已有关注直接核对，不取消或重复关注。'
            failed = self.data['task_failures'].get(login_id, {}).get(item['name'])
            if failed and failed['date'] == self.now().date().isoformat() and item['taskStage'] != 'DONE':
                rows[-1].update(reason=failed['message'], qualification='ineligible' if failed['code']=='TASK_INELIGIBLE' else 'waiting')
        prizes = [{k: p.get(k) for k in ('id', 'prizeName', 'name', 'winAt', 'winTime', 'createTime', 'useStatus', 'useUrl')}
                  for p in raw['prizes'].get('data', [])]
        invitation = deepcopy(raw['invite'])
        if 'session' in invitation:
            invitation['prizes'] = [{k: p.get(k) for k in ('id', 'name', 'stage', 'useStatus', 'stockId')}
                                    for p in invitation['prizes']]
        video = {k: raw['video'].get(k) for k in ('doneDayCount', 'canDayCount', 'doneWeekCount', 'canWeekCount')}
        observation = {'request_started': stamp, 'queried_at': self.now().isoformat(), 'tasks': rows,
                       'remaining': raw['remaining'], 'prizes': prizes, 'video': video, 'invite': invitation}
        def edit(data):
            data['campaign'] = deepcopy(campaign)
            failures = data['task_failures'].get(login_id, {})
            for task in rows:
                if task['taskStage'] == 'DONE':
                    failures.pop(task['name'], None)
            if stamp >= data['observations'].get(login_id, {}).get('request_started', ''):
                data['observations'][login_id] = observation
            pending = data['pending'].get(login_id)
            if not pending:
                return
            complete = (pending.get('month') != self.now().strftime('%Y-%m') if pending['kind'] == 'invite_claim'
                        else pending['week'] != week(self.now()))
            if pending['kind'] in {'browse', 'receive', 'author'}:
                confirmed = {'DONE', 'WAIT_RECEIVE'} if pending['kind'] == 'browse' else {'DONE'}
                complete |= any(t['name'] == pending['task_name'] and t['taskStage'] in confirmed for t in rows)
            elif pending['kind'] == 'video':
                complete |= pending['date'] != self.now().date().isoformat() or video['doneDayCount'] > pending['done_before']
            elif pending['kind'] == 'draw':
                complete |= raw['remaining']['remain'] < pending['remaining_before']
            elif pending['kind'] == 'invite_claim':
                complete |= any(str(p.get('id')) == pending['prize_id'] and p.get('useStatus') in {'UNUSED', 'USED'}
                                for p in invitation.get('prizes', []))
            if complete:
                data['pending'].pop(login_id, None)
        self._change(edit)

    def _resolve_author(self, login_id):
        pending = deepcopy(self.data['pending'].get(login_id))
        if not pending or pending['kind'] != 'author' or pending.get('phase') != 'follow':
            return
        profile = self.video_factory(login_id).author_status()
        if str(profile['isFollower']) == '1':
            def edit(data):
                if data['pending'].get(login_id) == pending:
                    data['pending'].pop(login_id, None)
                    auto = data['automatic'].get(login_id+':tasks')
                    if auto and auto.get('code') in {'RESULT_UNCERTAIN','VIDEO_REJECTED'}:
                        auto.update(complete=False,retry_at=self.now().isoformat())
            self._change(edit)

    def _before(self, login_id, kind, **values):
        if self.quit.is_set() or login_id in self.cancelled or not feature_running():
            raise CheckinError('任务已停止，未继续提交。', 'STOPPED', 409)
        intent = {'kind': kind, 'date': self.now().date().isoformat(), 'week': week(self.now()),
                  'month': self.now().strftime('%Y-%m'), **values}
        self._change(lambda data: data['pending'].__setitem__(login_id, intent))
        with self.lock:
            self.submitting.add(login_id)

    def submitted(self, login_id):
        with self.lock:
            self.submitting.discard(login_id)

    def queue(self, login_id=None, kind='refresh', *, task_id=None, prize_id=None, automatic=False):
        if self.quit.is_set() or not feature_running():
            raise CheckinError('活动已停止。', 'STOPPED', 409)
        if not self.metadata()['active'] and kind != 'refresh':
            raise CheckinError('活动未开放或已结束。', 'ACTIVITY_CLOSED', 409)
        known = self.accounts()
        if login_id is not None and not any(a['id'] == login_id for a in known):
            raise CheckinError('请选择有效共享账号。', 'ACCOUNT_MISSING', 404)
        count = 0
        for account in known:
            key = account['id']
            if account['status'] != 'active' or login_id is not None and key != login_id:
                continue
            with self.lock:
                if kind == 'refresh':
                    if key in self.reading:
                        self.read_again.add(key)
                        continue
                    self.reading.add(key)
                else:
                    if key in self.running:
                        continue
                    self.running.add(key)
                    self.cancelled.discard(key)
            if kind == 'refresh':
                self.read_executor.submit(self._refresh, key)
            else:
                self.executor.submit(self._work, key, kind, task_id, prize_id, automatic)
            count += 1
        return {'queued': count, 'message': '已安排查询。' if kind == 'refresh' else '已安排执行，将核对官方结果。'}

    def _refresh(self, login_id):
        try:
            stamp = self.now().isoformat()
            with self.factory(login_id).connect() as client:
                raw = client.observe(config=True)
                self._store(login_id, raw, stamp, client.campaign)
                if login_id not in self.running:
                    self._resolve_author(login_id)
        except (CheckinError, requests.RequestsError, KeyError, TypeError, ValueError) as error:
            if not isinstance(error, CheckinError):
                error = CheckinError('官方数据未完整返回，请刷新核对。', 'UPSTREAM_FORMAT')
            self._record(login_id, 'error', str(error), code=error.code)
        finally:
            with self.lock:
                self.reading.discard(login_id)
                again = login_id in self.read_again
                self.read_again.discard(login_id)
            if again and not self.quit.is_set():
                self.queue(login_id)

    def _work(self, login_id, kind, task_id, prize_id, automatic):
        date = self.now().date().isoformat()
        unknown_check = False
        try:
            with self.factory(login_id).connect() as client:
                client.automatic = automatic
                raw = client.observe(config=True)
                self._store(login_id, raw, self.now().isoformat(), client.campaign)
                self._resolve_author(login_id)
                if self.data['pending'].get(login_id):
                    unknown_check = True
                    raise CheckinError('上次提交结果待核实，已查询官方状态，不重复提交。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                if kind in {'tasks', 'task'}:
                    if kind == 'task' and not any(t['taskId'] == task_id for t in raw['tasks']):
                        raise CheckinError('任务列表已变化，请刷新后再操作。', 'TASK_CHANGED', 409)
                    for original in raw['tasks']:
                        task = next((t for t in raw['tasks'] if t['taskId'] == original['taskId']), None)
                        if task is None:
                            continue
                        if kind == 'task' and task['taskId'] != task_id:
                            continue
                        if task['taskStage'] == 'DONE':
                            continue
                        failed = self.data['task_failures'].get(login_id, {}).get(task['name'])
                        if automatic and failed and failed['date'] == date:
                            if failed['code'] == 'GIFT_SESSION_WAITING':
                                tag = self.gift_ready(login_id)
                                if not tag or tag == failed.get('identity_tag'):
                                    continue
                            elif failed.get('terminal',True) or failed.get('failures',0) >= 3 or self.now() < datetime.fromisoformat(failed['retry_at']):
                                continue
                        if task['taskStage'] == 'WAIT_RECEIVE':
                            client.receive(task, before_submit=lambda t=task: self._before(login_id, 'receive', task_name=t['name']))
                        elif task['taskStage'] == 'UNDO' and task['name'] == AUTHOR_TASK:
                            self._record(login_id,'running','正在核对官方作者“中国移动”的真实关注与任务进度。')
                            def save_author(lease):
                                self._change(lambda data:data['author_leases'].__setitem__(login_id,lease))
                            def author_before(phase):
                                client.auth._current(client.snapshot)
                                self._before(login_id,'author',task_name=AUTHOR_TASK,author_id=AUTHOR_ID,phase=phase)
                            followed = self.video_factory(login_id).complete_author(task,
                                load_lease=lambda:self.data['author_leases'].get(login_id),save_lease=save_author,
                                before_submit=author_before,after_submit=lambda:self.submitted(login_id),
                                stopped=lambda:self.quit.is_set() or login_id in self.cancelled or not feature_running())
                            self._record(login_id,'verified',('已核对已有关注：' if followed.get('already_followed') else '已真实关注：')+followed['author_name'])
                        elif task['taskStage'] == 'UNDO' and task['name'] in BROWSE_TASKS:
                            self._record(login_id, 'running', '正在访问并核对：'+task['name'])
                            def remember(url):
                                self._change(lambda data: data['pending'][login_id].update(destination=url))
                            try:
                                client.browse(task, before_submit=lambda _stage, t=task: self._before(login_id, 'browse', task_name=t['name']),
                                              remember_destination=remember)
                            except CheckinError as error:
                                if self.data['pending'].get(login_id) or task['name'] not in {'体验全球通礼遇','浏览网龄成长计划活动'}:
                                    raise
                                if error.code == 'ACCOUNT_MISMATCH':
                                    raise
                                previous = self.data['task_failures'].get(login_id,{}).get(task['name'],{})
                                failures = previous.get('failures',0)+1 if previous.get('date')==date else 1
                                value = {'date':date,'code':error.code,'message':clean_message(error),
                                    'identity_tag':getattr(error,'identity_tag',''),'failures':failures,
                                    'terminal':error.status in {401,403,429} or error.code in {'GIFT_SESSION_WAITING','VERIFICATION_REQUIRED'},
                                    'retry_at':(self.now()+timedelta(minutes=5)).isoformat()}
                                self._change(lambda data:data['task_failures'].setdefault(login_id,{}).__setitem__(task['name'],value))
                                self._store(login_id,raw,self.now().isoformat(),client.campaign)
                                if kind == 'task':
                                    raise
                                self._record(login_id,'deferred',task['name']+'：'+str(error),code=error.code)
                                continue
                            self._change(lambda data:data['task_failures'].get(login_id,{}).pop(task['name'],None))
                        else:
                            if kind == 'task' and task['taskStage'] != 'DONE':
                                raise CheckinError('该任务尚未接通自动执行。', 'TASK_UNSUPPORTED', 409)
                            continue
                        with self.lock:
                            self.submitting.discard(login_id)
                        raw = client.observe()
                        self._store(login_id, raw, self.now().isoformat(), client.campaign)
                        if self.data['pending'].get(login_id):
                            raise CheckinError('任务已提交，官方结果待核对，不重复提交。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                    for task in raw['tasks']:
                        if task['taskStage'] == 'WAIT_RECEIVE' and (kind == 'tasks' or task['taskId'] == task_id):
                            client.receive(task, before_submit=lambda t=task: self._before(login_id, 'receive', task_name=t['name']))
                            raw = client.observe()
                            self._store(login_id, raw, self.now().isoformat(), client.campaign)
                            if self.data['pending'].get(login_id):
                                raise CheckinError('奖励领取结果待核实，不自动重复提交。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                if kind == 'video' or kind == 'tasks' and self.data['settings']['video_enabled']:
                    for task in raw['video']['undoTodayTaskList']:
                        done = raw['video']['doneDayCount']
                        self._record(login_id, 'running', '正在实际播放官方视频，完成 5 分钟后核对机会。')
                        def save(lease):
                            self._change(lambda data: data['video_leases'].__setitem__(login_id, lease))
                        playback = self.video_factory(login_id).complete_drama(task,
                            load_lease=lambda: self.data['video_leases'].get(login_id), save_lease=save,
                            before_submit=lambda: self._before(login_id, 'video', done_before=done),
                            stopped=lambda: self.quit.is_set() or login_id in self.cancelled or not feature_running())
                        self.submitted(login_id)
                        raw = client.observe()
                        self._store(login_id, raw, self.now().isoformat(), client.campaign)
                        if self.data['pending'].get(login_id) or raw['video']['doneDayCount'] <= done:
                            raise CheckinError('视频完成结果待核实，不自动重放。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                        self._record(login_id, 'completed', '视频任务已由官方确认。', playback=playback)
                if kind == 'draw':
                    if self.now().weekday() != 6:
                        raise CheckinError('抽奖仅在北京时间周日开放，机会会在周一清零。', 'NOT_SUNDAY', 409)
                    while raw['remaining']['remain'] > 0:
                        if self.now().weekday() != 6:
                            break
                        previous = raw['remaining']['remain']
                        client.draw(before_submit=lambda: self._before(login_id, 'draw', remaining_before=previous))
                        raw = client.observe()
                        self._store(login_id, raw, self.now().isoformat(), client.campaign)
                        if self.data['pending'].get(login_id):
                            raise CheckinError('抽奖结果待核实，请刷新查询，不重复提交。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                        self._record(login_id, 'completed', '官方已确认使用 1 次抽奖机会，中奖情况请查看奖品列表。')
                        if not automatic:
                            break
                        if self.quit.wait(1):
                            break
                if kind == 'invite_claim':
                    invite = raw['invite']
                    prize = next((p for p in invite['prizes'] if str(p['id']) == prize_id), None)
                    if (prize is None or not invite['available'] or prize.get('useStatus') != 'WITHOUT'
                            or invite['count'] < (3, 5, 7)[int(prize['stage']) - 1]
                            or any(p.get('useStatus') in {'UNUSED', 'USED'} for p in invite['prizes'])):
                        raise CheckinError('该邀请奖励当前不可领取，请刷新核对。', 'PRIZE_UNAVAILABLE', 409)
                    client.invite_claim(invite, prize, before_submit=lambda: self._before(login_id, 'invite_claim', prize_id=prize_id))
                    raw = client.observe()
                    self._store(login_id, raw, self.now().isoformat(), client.campaign)
                    if self.data['pending'].get(login_id):
                        raise CheckinError('邀请奖励领取结果待核实。', 'RESULT_UNCERTAIN', 409, uncertain=True)
                unfinished = sum(t['taskStage'] == 'UNDO' and t['name'] not in {*BROWSE_TASKS,AUTHOR_TASK} for t in raw['tasks'])
                deferred_count = sum(t['taskStage']=='UNDO' and t['name'] in self.data['task_failures'].get(login_id,{}) for t in raw['tasks'])
                self._record(login_id, 'partial' if (unfinished or deferred_count) and kind in {'tasks', 'task'} else 'completed',
                    f'官方进度已核对，当前剩余 {raw["remaining"]["remain"]} 次机会。'
                    + (f'仍有 {unfinished} 项任务尚未接通。' if unfinished and kind in {'tasks', 'task'} else '')
                    + (f'另有 {deferred_count} 项资格或身份待处理，见任务说明。' if deferred_count and kind in {'tasks','task'} else ''))
                if automatic:
                    self._change(lambda data: data['automatic'].__setitem__(login_id+':'+kind,
                        {'date': date, 'complete': True, 'support':SUPPORT}))
        except (CheckinError, requests.RequestsError, KeyError, TypeError, ValueError) as error:
            if not isinstance(error, CheckinError):
                error = CheckinError('官方数据未完整返回，请刷新核对。', 'UPSTREAM_FORMAT')
            if self.data['pending'].get(login_id) and not getattr(error, 'definitive_rejection', False):
                error.uncertain = True
            if not error.uncertain:
                self._change(lambda data: data['pending'].pop(login_id, None))
            self._record(login_id, 'uncertain' if error.uncertain else 'error', str(error), code=error.code)
            if automatic:
                previous = self.data['automatic'].get(login_id+':'+kind, {})
                failures = previous.get('failures', 0) + 1 if previous.get('date') == date else 1
                checks = previous.get('unknown_checks', 0)+1 if unknown_check and previous.get('date') == date else 0
                daily_stop = (error.status in {401, 403, 429} or error.code=='ACCOUNT_MISMATCH' or
                              any(text in str(error) for text in ('不符合', '不满足', '无资格', '非目标', '仅限', '验证', '频繁')) or failures >= 3)
                if error.uncertain:
                    daily_stop = checks >= 2
                self._change(lambda data: data['automatic'].__setitem__(login_id+':'+kind,
                    {'date': date, 'complete': daily_stop, 'failures': failures, 'code': error.code, 'unknown_checks': checks,'support':SUPPORT,
                     'retry_at': (self.now()+timedelta(minutes=5)).isoformat()}))
        finally:
            with self.lock:
                self.running.discard(login_id)
                self.submitting.discard(login_id)

    def invitation(self, login_id):
        if not any(a['id'] == login_id for a in self.accounts()):
            raise CheckinError('请选择有效共享账号。', 'ACCOUNT_MISSING', 404)
        with self.lock:
            share_lock = self.share_locks.setdefault(login_id, RLock())
        with share_lock, self.factory(login_id).connect() as client:
            client.campaign = deepcopy(self.data['campaign'])
            invite = client.invite()
            if not invite.get('session'):
                raise CheckinError('当前未开放邀请。', 'INVITE_UNAVAILABLE', 409)
            cached = self.data['invite_assets'].get(login_id)
            if cached and cached['session_id'] == invite['session']['id']:
                return {**deepcopy(cached), 'url': invitation_link(cached['command'])}
            assets = client.invite_assets(invite)
            self._change(lambda data: data['invite_assets'].__setitem__(login_id, assets))
            return assets

    def membership(self, login_id):
        if not any(a['id']==login_id for a in self.accounts()):
            raise CheckinError('请选择有效共享账号。','ACCOUNT_MISSING',404)
        items = self.membership_reader(login_id)
        result = {'queried_at':self.now().isoformat(),'services':items,'reward_verified':False,
                  'message':'当前尚无已核对的活动会员办理与权益领取接口，未提交付费订单或领取。'}
        self._change(lambda data:data['memberships'].__setitem__(login_id,result))
        return result

    def tick(self):
        if self.quit.is_set() or not feature_running() or not self.metadata()['active']:
            return
        now = self.now()
        settings = self.data['settings']
        if not settings['enabled']:
            return
        for a in self.accounts():
            if a['status'] != 'active':
                continue
            kinds = ['tasks'] if now.strftime('%H:%M') >= settings['task_time'] else []
            if now.weekday() == 6 and now.strftime('%H:%M') >= settings['draw_time']:
                kinds.append('draw')
            for kind in kinds:
                old = self.data['automatic'].get(a['id']+':'+kind, {})
                deferred = self.data['task_failures'].get(a['id'], {}).get('体验全球通礼遇', {})
                tag = self.gift_ready(a['id']) if deferred.get('code') == 'GIFT_SESSION_WAITING' else ''
                new_identity = bool(kind == 'tasks' and not self.data['pending'].get(a['id'])
                                    and tag and tag != deferred.get('identity_tag'))
                retry_due = (kind=='tasks' and not self.data['pending'].get(a['id']) and any(
                    value.get('date')==now.date().isoformat() and not value.get('terminal',True)
                    and value.get('failures',0)<3 and now>=datetime.fromisoformat(value['retry_at'])
                    for value in self.data['task_failures'].get(a['id'],{}).values()))
                if (old.get('support') == SUPPORT and not new_identity and not retry_due and old.get('date') == now.date().isoformat() and (old.get('complete') or
                        old.get('retry_at') and now < datetime.fromisoformat(old['retry_at']))):
                    continue
                self.queue(a['id'], kind, automatic=True)

    def login_restored(self, login_id):
        with self.lock:
            if login_id in self.reading:
                self.read_again.add(login_id)
        def edit(data):
            for kind in ('tasks', 'draw'):
                key = login_id+':'+kind
                if data['automatic'].get(key, {}).get('code') in {'APP_LOGIN_REQUIRED', 'ACTIVITY_LOGIN_REQUIRED', 'VIDEO_LOGIN_REQUIRED'}:
                    data['automatic'].pop(key, None)
            failures = data['task_failures'].get(login_id,{})
            for name in list(failures):
                if failures[name]['code'] in {'APP_LOGIN_REQUIRED','ACTIVITY_LOGIN_REQUIRED','GIFT_LOGIN_REQUIRED'}:
                    failures.pop(name,None)
        self._change(edit)
        self.tick()

    def busy_login(self, login_id):
        with self.lock:
            return login_id in self.submitting

    def forget(self, login_id):
        self.cancelled.add(login_id)
        def edit(data):
            for field in ('observations', 'records', 'pending', 'video_leases', 'author_leases', 'invite_assets', 'route_tags', 'task_failures', 'memberships'):
                data[field].pop(login_id, None)
            data['automatic'] = {k:v for k,v in data['automatic'].items() if not k.startswith(login_id+':')}
        self._change(edit)

    def start(self):
        if self.thread is not None:
            return
        def loop():
            while not self.quit.is_set():
                self.tick()
                self.quit.wait(1)
        self.thread = Thread(target=loop, name=SCOPE+'-timer', daemon=True)
        self.thread.start()

    def shutdown(self, wait=True):
        self.quit.set()
        if self.thread is not None and wait:
            self.thread.join(2)
        self.executor.shutdown(wait=wait, cancel_futures=True)
        self.read_executor.shutdown(wait=wait, cancel_futures=True)
