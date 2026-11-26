"""CMLIVE task login, actual media playback and official progress reporting."""
import base64
from contextlib import contextmanager
import json
import os
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from workspace.accounts.login import AppLoginSession, AppLoginError
from workspace.http_transport import requests
from recharge.cmcc import APP_UA
from .client import CheckinError


ORIGIN = 'https://v.cmcc-cs.cn'
API = ORIGIN + '/cmlive/api/'
ENTRY = ORIGIN + '/cmlive/live/feed'
SCOPE = 'checkin:cmlive'
APP_TOKEN = '/leadeon-abilityopen-biz/BN/obtainToken/getBigNetToken'
VIDEO_TASKS = {
    '看精彩视频': ('10', '2', 'v.cmcc-cs.cn', '/cmlive/live/feed'),
    '看直播0元领会员': ('10', '3', 'v.cmcc-cs.cn', '/cmlive/live/room'),
}


def play_media(url, seconds):
    if os.environ.get('LINGXI_ANDROID') == '1':
        from java import jclass
        player = jclass('android.media.MediaPlayer')()
        try:
            player.setVolume(0, 0)
            player.setDataSource(url)
            player.prepare()
            player.start()
            start = time.monotonic()
            while time.monotonic() - start < seconds + 5:
                if player.getCurrentPosition() >= seconds * 1000:
                    return {'played_seconds': player.getCurrentPosition() / 1000}
                time.sleep(0.2)
            raise CheckinError('视频未实际播放到要求的时长。', 'PLAYBACK_INCOMPLETE')
        finally:
            player.release()
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except ImportError as error:
        raise CheckinError('此客户端缺少视频播放组件，任务尚未完成。', 'PLAYBACK_UNAVAILABLE') from error
    try:
        started = time.monotonic()
        result = subprocess.run([
            get_ffmpeg_exe(), '-nostdin', '-v', 'error', '-re', '-i', url,
            '-t', str(seconds), '-map', '0:v:0', '-an', '-progress', 'pipe:1',
            '-nostats', '-f', 'null', '-',
        ], capture_output=True, text=True, encoding='utf8', errors='replace',
           timeout=seconds + 30,
           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckinError('视频播放未完成，尚未提交任务完成记录。', 'PLAYBACK_INCOMPLETE') from error
    progress = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    played = float(progress.get('out_time_us', '0')) / 1000000
    frames = int(progress.get('frame', '0'))
    if result.returncode or not frames or played < seconds or time.monotonic() - started < seconds:
        raise CheckinError('视频未实际播放到要求的时长。', 'PLAYBACK_INCOMPLETE')
    return {'played_seconds': played, 'frames': frames}


class CheckinVideo:
    def __init__(self, credentials, cache, *, player=play_media, recover=None):
        self.credentials, self.cache, self.player = credentials, cache, player
        self.recover = recover

    def _call(self, client, method, path, body=None, *, writing=False):
        try:
            response = client.request(method, API + path,
                **({'json': body or {}} if method == 'POST' else {'params': body or {}}),
                timeout=(10, 25), allow_redirects=False)
        except requests.RequestsError as error:
            raise CheckinError('视频服务连接中断，请核对官方进度。', 'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK',
                               uncertain=writing) from error
        try:
            value = response.json()
        except ValueError as error:
            raise CheckinError('视频服务响应格式异常。', 'UPSTREAM_FORMAT', uncertain=writing) from error
        if not isinstance(value, dict):
            raise CheckinError('视频服务响应格式异常。', 'UPSTREAM_FORMAT', uncertain=writing)
        if response.status_code == 401 or str(value.get('rtnCode')) == '401':
            if not writing and hasattr(client, '_credential_binding') and self.cache.recover(client):
                return self._call(client, method, path, body)
            raise CheckinError('视频活动会话需要恢复，请重新查询。', 'VIDEO_LOGIN_REQUIRED', 401, uncertain=writing)
        if response.status_code != 200 or str(value.get('rtnCode')) != '0':
            raise CheckinError('视频服务未确认本次操作，请核对官方进度。', 'VIDEO_REJECTED', uncertain=writing)
        return value.get('object')

    @contextmanager
    def _session(self):
        snapshot = self.credentials()
        with requests.Session(impersonate='chrome', headers={
                'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': ENTRY}) as client:
            identity_recovered = False
            def initialize():
                nonlocal identity_recovered
                latest = self.credentials()
                if latest['phone'] != snapshot['phone']:
                    raise CheckinError('账号已变更，请重新查询。', 'ACCOUNT_MISMATCH', 409)
                flow = AppLoginSession.from_credentials(latest)
                try:
                    _, result, _ = flow.call(APP_TOKEN, {
                        'sourceId': '017027', 'url': ENTRY, 'cellNum': snapshot['phone']})
                except AppLoginError as error:
                    raise CheckinError('视频活动换票失败，请重新查询。', 'VIDEO_LOGIN_REQUIRED', 401) from error
                body = result.get('rspBody') or {}
                if isinstance(body, str):
                    body = json.loads(body)
                code = str(result.get('retCode'))
                if (code in {'410000', '400007', '400009', '420000'}
                        and self.recover is not None and not identity_recovered):
                    identity_recovered = True
                    try:
                        self.recover(latest)
                    except AppLoginError as error:
                        raise CheckinError('该号码的 APP 身份未能恢复，请在账号管理重新登录。',
                                           'VIDEO_LOGIN_REQUIRED', 401) from error
                    return initialize()
                if str(result.get('retCode')) != '000000' or not body.get('token'):
                    safe = code if code.isalnum() and len(code) <= 20 else 'unknown'
                    raise CheckinError(f'官方视频换票未完成（{safe}）。', 'VIDEO_LOGIN_REQUIRED', 401)
                token = self._call(client, 'POST', 'user/auth/cmpassLoginByCmToken',
                                   {'cmpassToken': body['token']})
                if not isinstance(token, str) or not token:
                    raise CheckinError('视频登录响应无效。', 'UPSTREAM_FORMAT')
                client.headers['Authorization'] = token
                return {'phone_verified': False}
            self.cache.connect(SCOPE, snapshot, client, initialize, headers=('Authorization',))
            user = self._call(client, 'GET', 'user/auth/getCurrentUserInfoNew')
            decoder = Cipher(algorithms.AES(b'1234123412ABCDEF'),
                             modes.CBC(b'ABCDEF1234123412')).decryptor()
            raw = decoder.update(base64.b64decode(user['phone'])) + decoder.finalize()
            unpadder = PKCS7(128).unpadder()
            phone = (unpadder.update(raw) + unpadder.finalize()).decode()
            if phone != snapshot['phone']:
                raise CheckinError('视频活动账号不一致。', 'ACCOUNT_MISMATCH', 409)
            self.cache.sync(client, data={'phone_verified': True})
            yield client, user

    def complete(self, task, info, *, before_submit):
        with self._session() as (client, user):
            detail = self._call(client, 'POST', 'advert/signIn/getOneTaskInfo',
                                {'taskToken': info['taskToken']})['data']
            if str(detail['status']) in {'1', '2'}:
                return []
            seconds = float(detail['scanTime'])
            if str(detail['status']) != '0' or not 0 < seconds <= 30:
                raise CheckinError('官方视频任务要求已变化。', 'TASK_CHANGED', 409)
            query = parse_qs(urlsplit(task['jumpUrl']).query)
            if task['taskName'] == '看直播0元领会员':
                room_id, channel = query['id'][0], query['cmliveChannelId'][0]
                room = self._call(client, 'POST', 'room/front/roomBeat', {
                    'password': '', 'roomId': room_id, 'channelId': channel, 'sourceApp': ''})
                status = str(room['status'])
                url = room.get('playbackUrl') if status == '6' else room.get('pullUrl')
                playback = []
                try:
                    if status not in {'1', '2', '6'} or not isinstance(url, str) or not url.startswith(('https://', 'http://')):
                        raise CheckinError('官方直播间当前没有可播放的直播或回放。', 'VIDEO_CONTENT_UNAVAILABLE')
                    playback.append({'room_id': room_id, 'replay': status == '6', **self.player(url, seconds)})
                finally:
                    try:
                        self._call(client, 'POST', 'room/front/leaveRoom', {'roomId': room_id, 'channelId': channel})
                    except CheckinError:
                        if playback:
                            playback[0]['room_leave_confirmed'] = False
                return self._finish(client, detail, playback, before_submit)
            payload = {
                'applyChannel': query['cmliveChannelId'][0],
                'belongChannelId': query['belongChannelId'][0],
                'collectId': int(query['collectId'][0]),
                'provinceCode': user['provinceCode'], 'cityCode': user['cityCode'],
                'userInfo': {'historyVideos': [], 'provinceCode': user['provinceCode'],
                    'cityCode': user['cityCode'], 'tagId': '', 'videoId': '',
                    'videoType': '', 'seeTime': '', 'totalTime': ''},
                'recommendPage': 1, 'recommendNum': 10, 'recSwitch': 1,
                'abVersion': '', 'timestamp': int(time.time() * 1000),
                'servNumber': base64.b64decode(user['phone']).hex().upper(), 'careVersion': 1,
            }
            feed = self._call(client, 'POST', 'video/video/mixFeedList', payload)
            clips = [clip for clip in feed['feedVideoList']
                     if str(clip.get('videoSource')) == '0'
                     and clip.get('videoUrl', '').startswith('https://activity.cmcc-cs.cn/')][:4]
            if len(clips) != 4:
                raise CheckinError('未取得任务需要的四个可播放视频。', 'VIDEO_CONTENT_UNAVAILABLE')
            playback = []
            for clip in clips:
                playback.append({'clip_id': clip['id'], **self.player(clip['videoUrl'], seconds)})
            return self._finish(client, detail, playback, before_submit)

    def _finish(self, client, detail, playback, before_submit):
        before_submit()
        result = self._call(client, 'POST', 'advert/signIn/openFinish',
                            {'cToken': detail['cToken']}, writing=True)
        if not isinstance(result, dict) or not result.get('data'):
            raise CheckinError('官方尚未确认视频任务完成。', 'RESULT_UNCERTAIN', 409, uncertain=True)
        return playback
