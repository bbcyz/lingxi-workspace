"""Official DIY video-day protocol; all business requests run in Python."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from datetime import timedelta
import base64
import hashlib
import html
import json
import re
import time
from urllib.parse import urljoin, urlsplit, parse_qs
import zlib

from features.checkin.client import CheckinClient, CheckinError
from recharge.cmcc import SHANGHAI
from workspace.feature_runtime import feature_running
from workspace.http_transport import requests

ORIGIN = 'https://wx.10086.cn'
BASE = ORIGIN + '/qwhdhub'
ACTIVITY = '1126082530'
ENTRY = BASE + '/diy-client/' + ACTIVITY + '?A_C_CODE=10hep1ZhkL'
DRAW_COMPONENT = '4EC96l0-9btuobajC9Yu'
INVITE_COMPONENT = '4DwOvs0-f1qlH03siz5_'
VIDEO = '/api/diyVideoDayRedesign/task/queryDiyVideoTask'
BROWSE_TASKS = {
    '体验全球通礼遇': ('qqt.cmicrwx.cn', '/2016tyjf/xhmqqthy/new/ssoTokenLogin.action'),
    '浏览网龄成长计划活动': ('dev.coc.10086.cn', '/coc/web6/ageFeedbackV2/'),
    '浏览签到活动': ('wx.10086.cn', '/qwhdhub/qwhdmark/1021122301'),
    '浏览周三充值日活动': ('wx.10086.cn', '/hlwyxhdhub/act-wedrecharge/index.html'),
    '体验余量查询': ('h.app.coc.10086.cn', '/cmcc-app/setMealSurplus/setMealSurplusNew.html'),
    '查看本月账单': ('h.app.coc.10086.cn', '/cmcc-app/personalBill/phoneBillsNew.html'),
    '浏览爱奇艺热播短剧': ('v.cmcc-cs.cn', '/cmlive/live/feed'),
}
CANONICAL_TARGETS = {
    '体验余量查询': ('wx.10086.cn', '/cmcc-service/app-pages/main/marginQuery'),
    '查看本月账单': ('wx.10086.cn', '/cmcc-service/app-pages/main/phoneBills'),
}
DEFAULT_CAMPAIGN = {
    'title': '周日追剧盛宴', 'start_at': '2026-09-07T00:00:00+08:00',
    'end_at': '2026-11-30T23:59:59+08:00', 'announcement': '', 'rules': '',
    'draw_component': DRAW_COMPONENT, 'invite_component': INVITE_COMPONENT,
    'signin_paused': True, 'browse_seconds': {}, 'prizes': [],
}


def invitation_link(command):
    for value in re.findall(r'https?://[^\s<>]+', command):
        value = value.rstrip('。，；！,;!\"\'）)')
        url = urlsplit(value)
        query = parse_qs(url.query)
        if (url.scheme == 'https' and url.netloc == 'wx.10086.cn'
                and url.path == '/qwhdhub/diy-client/' + ACTIVITY
                and query.get('inviteId') and query.get('verCode')):
            return value
    return ''


def configuration(data):
    """Decode the same base64/zlib page configuration used by the official H5."""
    if data['templateActivity']['code'] != ACTIVITY:
        raise CheckinError('官方活动标识已变化，未提交任何操作。', 'ACTIVITY_CHANGED', 409)
    page = json.loads(zlib.decompress(base64.b64decode(data['activityPageConfigJson'])))
    elements = []
    def collect(value):
        if isinstance(value, dict):
            if value.get('elementId'):
                elements.append(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(page['pages'])
    rule = next(x['pluginProps']['content'] for x in elements if x['componentName'] == 'activityRulesWindow')
    rule = html.unescape(re.sub(r'<[^>]+>', '\n', rule))
    rule = re.sub(r'\n\s*\n+', '\n\n', rule).strip()
    dates = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*-\s*(\d{4})年(\d{1,2})月(\d{1,2})日', rule)
    if dates is None:
        raise CheckinError('未读取到官方规则中的活动期限。', 'CONFIG_CHANGED', 409)
    numbers = [int(x) for x in dates.groups()]
    start = datetime(*numbers[:3], tzinfo=SHANGHAI)
    end = datetime(*numbers[3:], 23, 59, 59, tzinfo=SHANGHAI)
    play = next(x for x in data['playMethodInfos'] if x['actPlayMethod']['name'] == '幸运抽奖机')
    task_config = next(x['pluginProps']['taskList'] for x in elements if x['componentName'] == 'taskGroup')
    announcements = [str(item.get('text', '')) for x in elements if x['componentName'] == 'notice'
                     for item in x.get('pluginProps', {}).get('contentGroup', [])]
    announcement = ' '.join(announcements)
    return {**deepcopy(DEFAULT_CAMPAIGN), 'start_at': start.isoformat(), 'end_at': end.isoformat(),
            'rules': rule, 'announcement': announcement,
            'signin_paused': '签到' in announcement and '下线' in announcement,
            'draw_component': play['actPlayMethod']['componentId'],
            'browse_seconds': {x['name']: x['browseSeconds'] for x in task_config if x['type'] == 'browse'},
            'prizes': [{'id': str(x['id']), 'name': x['name'], 'status': x.get('useStatus') or ''}
                       for x in play['actLotteryLogicInfo']['prizes']]}


class DramaClient:
    def __init__(self, provider, cache, *, auth=None, query=None, wait=time.sleep,
                 load_route=lambda: None, save_route=lambda state: None, play_short=None,
                 after_submit=lambda: None, visit_global=None, close_visits=lambda: None):
        self.auth = auth or CheckinClient(provider, credential_cache=cache)
        self.cache, self.query, self.wait = cache, query, wait
        self.session = self.snapshot = None
        self.campaign = deepcopy(DEFAULT_CAMPAIGN)
        self.load_route, self.save_route = load_route, save_route
        self.play_short = play_short
        self.after_submit = after_submit
        self.visit_global, self.close_visits = visit_global, close_visits
        self.automatic = False

    def _ensure_entry(self):
        session = self.session
        token = next(c.value for c in session.cookies.jar if c.name == 'QWHD_SESSION_TOKEN')
        tag = hashlib.sha256(token.encode()).hexdigest()
        previous = self.load_route() or {}
        today = datetime.now(SHANGHAI)
        sunday = (today.date()-timedelta(days=today.weekday())).isoformat() if today.weekday() == 6 else ''
        if previous.get('auth_tag') != tag or sunday and previous.get('sunday') != sunday:
            page = self.auth._follow(session, ENTRY, source='追剧活动入口')
            if urlsplit(page.url).path != '/qwhdhub/diy-client/' + ACTIVITY:
                raise CheckinError('官方活动入口暂不可用，请稍后重新查询。', 'ENTRY_UNAVAILABLE', 409)
            current_token = next(c.value for c in session.cookies.jar if c.name == 'QWHD_SESSION_TOKEN')
            if current_token != token and self.cache is not None:
                self.cache.sync(session)
            self.save_route({'auth_tag': hashlib.sha256(current_token.encode()).hexdigest(), 'sunday': sunday})

    @contextmanager
    def connect(self):
        # This activity uses the same confirmed APP realm and cookie as the
        # sign-in page. Keep the existing expiry, login binding and single flight.
        with self.auth._session() as (session, snapshot):
            self.session, self.snapshot = session, snapshot
            session.headers['Referer'] = ENTRY
            try:
                self._ensure_entry()
                yield self
            finally:
                self.close_visits()
                self.session = self.snapshot = None

    def _call(self, path, body=None, *, method='GET', writing=False, before_submit=None, recover=True):
        if not feature_running():
            raise CheckinError('活动已停止。', 'STOPPED', 409)
        if writing:
            self.auth._current(self.snapshot)
            before_submit()
        try:
            response = self.session.request(method, BASE + path,
                headers={'Content-Type': 'application/json', 'login-check': '1'},
                **({'json': body or {}} if method == 'POST' else {'params': body or {}}),
                timeout=(10, 25), allow_redirects=False)
            value = response.json()
        except (requests.RequestsError, ValueError) as error:
            raise CheckinError('官方结果暂未核实，请刷新查询。' if writing else '连接活动服务失败，请稍后刷新。',
                'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK', uncertain=writing) from error
        finally:
            if writing:
                self.after_submit()
        if response.status_code == 401 or isinstance(value, dict) and value.get('code') == 'GOTO_LOGIN':
            if not writing and recover and self.cache is not None and self.cache.recover(self.session):
                self.session.headers['Referer'] = ENTRY
                self._ensure_entry()
                return self._call(path, body, method=method, recover=False)
            error = CheckinError('活动身份需要恢复，请重新查询或恢复共享账号登录。', 'ACTIVITY_LOGIN_REQUIRED', 401)
            error.definitive_rejection = writing
            raise error
        if response.status_code != 200 or not isinstance(value, dict):
            raise CheckinError('官方响应格式异常，未确认操作结果。', 'UPSTREAM_FORMAT', uncertain=writing)
        if value.get('code') != 'SUCCESS':
            code = str(value.get('status') or value.get('code') or 'UPSTREAM_REJECTED')
            message = str(value.get('msg') or '官方暂未允许此操作')[:350]
            error = CheckinError(message, code, 409)
            error.definitive_rejection = writing
            error.operation = path
            raise error
        return value.get('data')

    def config(self):
        self.campaign = configuration(self._call('/diyActivity/info/' + ACTIVITY))
        return self.campaign

    def tasks(self):
        return self._call('/diyTask/list/' + self.campaign['draw_component']) or []

    def remaining(self):
        return self._call('/diyLottery/period/remain/' + self.campaign['draw_component'], method='POST')

    def prizes(self):
        return self._call('/diyLottery/queryWinRecord', {'withStatus': 'true'}) or {'data': []}

    def video(self):
        return self._call(VIDEO)

    def observe(self, *, config=False):
        if config:
            self.config()
        return {'tasks': self.tasks(), 'remaining': self.remaining(), 'prizes': self.prizes(),
                'video': self.video(), 'invite': self.invite()}

    def browse(self, task, *, before_submit, remember_destination=lambda url: None):
        target = BROWSE_TASKS.get(task['name'])
        if task['type'] != 'browse' or task['launchSign'] != 'normal' or target is None:
            raise CheckinError('该任务的实际业务流程尚未接通。', 'TASK_UNSUPPORTED', 409)
        if task['name'] == '浏览爱奇艺热播短剧' and self.play_short is None:
            raise CheckinError('当前客户端未接入短剧实际播放。', 'PLAYBACK_UNAVAILABLE', 409)
        if task['name'] == '体验全球通礼遇' and self.visit_global is None:
            raise CheckinError('当前客户端未接入全球通礼遇身份。', 'GIFT_LOGIN_REQUIRED', 409)
        url = urljoin(ORIGIN, task['url'])
        parts = urlsplit(url)
        if parts.netloc != 'wx.10086.cn' or parts.path != '/qwhdhub/diyTask/startBrowse/' + task['taskId']:
            raise CheckinError('官方任务入口已变化。', 'TASK_CHANGED', 409)
        seconds = self.campaign['browse_seconds'][task['name']]
        if not 0 <= seconds <= 60:
            raise CheckinError('官方任务时长已变化。', 'TASK_CHANGED', 409)
        queries = {'体验余量查询':'usage', '查看本月账单':'bill', '体验全球通礼遇':'global_tone',
                   '浏览网龄成长计划活动':'v5'}
        if self.query is not None and task['name'] in queries:
            self.query(queries[task['name']])
        self.auth._current(self.snapshot)
        before_submit('start')
        began = time.monotonic()
        try:
            response = self.session.get(url, timeout=(10, 25), allow_redirects=False)
        finally:
            self.after_submit()
        visited = url
        if task['name'] == '体验全球通礼遇' and response.status_code in {200,301,302,303,307,308}:
            # The already authenticated QQT session visits the actual landing
            # page; do not consume a fresh SSO ticket for a valid identity.
            response = self.visit_global()
            visited = response.url
            remember_destination(visited)
        for _ in range(5):
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            visited = urljoin(visited, response.headers['location'])
            destination = urlsplit(visited)
            allowed = {target}
            if task['name'] in CANONICAL_TARGETS:
                allowed.add(CANONICAL_TARGETS[task['name']])
            if destination.scheme != 'https' or (destination.netloc, destination.path) not in allowed:
                raise CheckinError('任务跳转到了尚未接通的业务入口，未提交完成记录。', 'TASK_DESTINATION_CHANGED', 409)
            remember_destination(visited)
            response = self.session.get(visited, timeout=(10, 25), allow_redirects=False)
        if response.status_code != 200:
            raise CheckinError('任务页面未成功打开，未提交完成记录。', 'TASK_PAGE_FAILED')
        # startBrowse may return an intermediate HTML; the implementation must
        # locate and visit its actual destination before reporting completion.
        if urlsplit(visited).path.startswith('/qwhdhub/diyTask/startBrowse/'):
            raise CheckinError('任务承接页尚需解析，未提交完成记录。', 'TASK_INTERMEDIATE_PAGE', 409)
        if task['name'] == '浏览爱奇艺热播短剧':
            self.play_short(visited, before_submit=lambda: before_submit('video_finish'))
        self.wait(max(0, seconds - (time.monotonic() - began)))
        # Normal H5 tasks are credited by startBrowse. finishBrowse is used
        # only by the native-launch branch; calling it here would repeat a write.
        return {'visited': target[0] + target[1], 'seconds': seconds}

    def receive(self, task, *, before_submit):
        return self._call('/diyTask/receive', {'taskId': task['taskId']}, method='POST',
                          writing=True, before_submit=before_submit)

    def draw(self, *, before_submit):
        return self._call('/diyLottery/lotterySafely/' + self.campaign['draw_component'],
                          writing=True, before_submit=before_submit)

    def invite(self):
        sessions = self._call('/diyLottery/querySeckillCoupons',
                             {'componentId': self.campaign['invite_component']}, method='POST') or []
        if not sessions:
            return {'available': False, 'count': 0, 'prizes': []}
        session = sessions[0]
        body = {'componentId': self.campaign['invite_component'],
                'activityId': session['activitySession']['activityId']}
        count = self._call('/diyTeam/progress', body, method='POST')
        return {'available': bool(session.get('canParticipate')), 'count': count,
                'session': session['activitySession'], 'prizes': session['prizes']}

    def invite_assets(self, invite):
        body = {'componentId': self.campaign['invite_component'], 'activityId': invite['session']['activityId']}
        command = self._call('/diyTeam/getKeyword', body, method='POST')
        poster = self._call('/diyTeam/invite/qrcode', body, method='POST')
        return {'command': command, 'url': invitation_link(command), 'poster_url': poster['posterUrl'],
                'session_id': invite['session']['id']}

    def invite_claim(self, invite, prize, *, before_submit):
        return self._call('/diyLottery/lotterySafely', {'componentId': self.campaign['invite_component'],
            'activityId': invite['session']['activityId'], 'prizeCode': prize['id']}, method='POST',
            writing=True, before_submit=before_submit)
