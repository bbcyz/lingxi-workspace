"""Real CMLIVE playback for the official five-minute video task."""
import os
from datetime import datetime, timedelta
import subprocess
import time
from urllib.parse import urlsplit, parse_qs

from features.checkin.client import CheckinError
from features.checkin.video import CheckinVideo
from recharge.cmcc import SHANGHAI

AUTHOR_TASK = '关注优质作者'
AUTHOR_ID = 10  # 官方 gzzz 推荐池中的“中国移动”，不取消或循环重建关注。


def play_video(url, seconds, stopped):
    if os.environ.get('LINGXI_ANDROID') == '1':
        from java import jclass
        player = jclass('android.media.MediaPlayer')()
        try:
            player.setVolume(0, 0)
            player.setDataSource(url)
            player.setLooping(True)
            player.prepare()
            player.start()
            previous = played = 0
            deadline = time.monotonic() + seconds + 30
            while played < seconds * 1000 and time.monotonic() < deadline:
                if stopped():
                    raise CheckinError('视频任务已停止，未提交完成记录。', 'STOPPED', 409)
                position = player.getCurrentPosition()
                played += position - previous if position >= previous else player.getDuration() - previous + position
                previous = position
                time.sleep(.2)
            if played < seconds * 1000:
                raise CheckinError('视频未实际播放到要求时长。', 'PLAYBACK_INCOMPLETE')
            return {'played_seconds': played / 1000, 'player': 'MediaPlayer'}
        finally:
            player.release()
    from imageio_ffmpeg import get_ffmpeg_exe
    started = time.monotonic()
    process = subprocess.Popen([get_ffmpeg_exe(), '-nostdin', '-v', 'error', '-re',
        '-stream_loop', '-1', '-i', url, '-t', str(seconds), '-map', '0:v:0', '-an',
        '-progress', 'pipe:1', '-nostats', '-f', 'null', '-'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        while True:
            if stopped():
                raise CheckinError('视频任务已停止，未提交完成记录。', 'STOPPED', 409)
            if time.monotonic() - started > seconds + 30:
                raise CheckinError('视频播放超时，未提交完成记录。', 'PLAYBACK_INCOMPLETE')
            try:
                output, _ = process.communicate(timeout=.5)
                break
            except subprocess.TimeoutExpired:
                continue
        progress = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        played = float(progress.get('out_time_us', '0')) / 1_000_000
        frames = int(progress.get('frame', '0'))
        if process.returncode or not frames or played < seconds or time.monotonic() - started < seconds:
            raise CheckinError('视频未实际播放到要求时长，未提交完成记录。', 'PLAYBACK_INCOMPLETE')
        return {'played_seconds': played, 'frames': frames}
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


class DramaVideo(CheckinVideo):
    def author_status(self):
        with self._session() as (session, _user):
            profile = self._call(session, 'POST', 'user/author/queryAuthorInfo', {'authorId': AUTHOR_ID})
            if int(profile['authorId']) != AUTHOR_ID:
                raise CheckinError('官方作者身份未核对成功，未提交关注。', 'AUTHOR_CHANGED', 409)
            return profile

    def complete_author(self, task, *, load_lease, save_lease, before_submit,
                        after_submit=lambda: None, stopped=lambda: False):
        with self._session() as (session, _user):
            profile = self._call(session, 'POST', 'user/author/queryAuthorInfo', {'authorId': AUTHOR_ID})
            if int(profile['authorId']) != AUTHOR_ID:
                raise CheckinError('官方作者身份未核对成功，未提交关注。', 'AUTHOR_CHANGED', 409)
            today = datetime.now(SHANGHAI).date()
            period = (today-timedelta(days=today.weekday())).isoformat()
            lease = load_lease()
            if not lease or lease['task_id'] != task['taskId'] or lease.get('period') != period:
                reply = self._call(session, 'GET', 'advert/lottery/getFollowAuthorTask', {'token': task['param']})
                lease = {'task_id':task['taskId'], 'period':period, 'detail':reply['data']}
                save_lease(lease)
            detail = lease['detail']
            if detail['backUrl'].split('?')[0] != 'https://wx.10086.cn/qwhdhub/diy-client/1126082530':
                raise CheckinError('作者任务不属于当前活动。', 'TASK_CHANGED', 409)
            if str(detail['status']) == '0':
                return {'already_completed':True, 'author_id':AUTHOR_ID, 'author_name':profile['authorName']}
            if str(detail['status']) != '1':
                raise CheckinError('官方作者任务参数已变化。', 'TASK_CHANGED', 409)
            already = str(profile['isFollower']) == '1'
            if not already:
                if stopped():
                    raise CheckinError('任务已停止，未提交关注。', 'STOPPED', 409)
                before_submit('follow')
                try:
                    self._call(session, 'POST', 'user/customer/following', {'authorId':AUTHOR_ID,'status':1}, writing=True)
                finally:
                    after_submit()
                profile = self._call(session, 'POST', 'user/author/queryAuthorInfo', {'authorId':AUTHOR_ID})
                if int(profile['authorId']) != AUTHOR_ID or str(profile['isFollower']) != '1':
                    raise CheckinError('关注状态待核实，未提交任务奖励回传。', 'RESULT_UNCERTAIN', 409, uncertain=True)
            if stopped():
                raise CheckinError('任务已停止，未提交任务回传。', 'STOPPED', 409)
            before_submit('callback')
            try:
                result = self._call(session, 'GET', 'advert/lottery/followAuthorTaskFinish',
                                    {'token':detail['cToken']}, writing=True)
            finally:
                after_submit()
            if not result:
                raise CheckinError('作者任务回传结果待核实。', 'RESULT_UNCERTAIN', 409, uncertain=True)
            return {'author_id':AUTHOR_ID,'author_name':profile['authorName'],'already_followed':already}

    def complete_short(self, url, *, before_submit, stopped=lambda: False, player=play_video):
        query = parse_qs(urlsplit(url).query)
        if query.get('id') != ['65424']:
            raise CheckinError('官方短剧任务内容已变化。', 'TASK_CHANGED', 409)
        with self._session() as (session, _user):
            token = query.get('jtToken', [''])[0]
            detail = self._call(session, 'GET', 'advert/video/activity/getOneTaskInfo', {'jtToken': token}) if token else None
            clip = self._call(session, 'GET', 'video/video/feedDetail', {'id': query['id'][0]})
            media = urlsplit(clip.get('videoUrl', ''))
            if clip.get('videoSource') not in {'0', 0} or media.scheme != 'https' or media.netloc != 'activity.cmcc-cs.cn':
                raise CheckinError('该短剧当前没有已授权的可播放内容。', 'VIDEO_CONTENT_UNAVAILABLE')
            playback = player(clip['videoUrl'], 30, stopped)
            if detail and str(detail['status']) == '0':
                before_submit()
                self._call(session, 'GET', 'advert/video/activity/openFinish', {'jtToken': token}, writing=True)
            return playback

    def complete_drama(self, task, *, load_lease, save_lease, before_submit, stopped=lambda: False,
                       player=play_video):
        with self._session() as (session, _user):
            lease = load_lease()
            today = datetime.now(SHANGHAI).date()
            period = (today - timedelta(days=today.weekday())).isoformat()
            if not lease or lease['task_id'] != task['taskId'] or lease.get('period') != period:
                reply = self._call(session, 'GET', 'advert/lottery/getFiveMinuteTaskInfo', {'token': task['param']})
                detail = reply['data']
                lease = {'task_id': task['taskId'], 'detail': detail, 'period': period}
                save_lease(lease)
            detail = lease['detail']
            if str(detail['status']) == '0':
                return {'already_completed': True}
            if str(detail['status']) != '1' or detail['backUrl'].split('?')[0] != 'https://wx.10086.cn/qwhdhub/diy-client/1126082530':
                raise CheckinError('官方视频任务参数已变化，未提交完成记录。', 'TASK_CHANGED', 409)
            # The current official player overrides scanTime with 300 seconds.
            # The task-info response's scanTime=5 must not shorten that playback.
            clip = self._call(session, 'GET', 'video/video/feedDetail', {'id': '107230'})
            url = urlsplit(clip.get('videoUrl', ''))
            if clip.get('videoSource') != '0' and clip.get('videoSource') != 0 or url.scheme != 'https' or url.netloc != 'activity.cmcc-cs.cn':
                raise CheckinError('当前没有可验证播放的官方视频。', 'VIDEO_CONTENT_UNAVAILABLE')
            playback = player(clip['videoUrl'], 300, stopped)
            if stopped():
                raise CheckinError('任务已停止，未提交完成记录。', 'STOPPED', 409)
            before_submit()
            reply = self._call(session, 'GET', 'advert/lottery/fiveMinuteTaskFinish',
                               {'token': detail['cToken']}, writing=True)
            if not isinstance(reply, dict) or not reply.get('data'):
                raise CheckinError('视频完成结果待核实，不自动重放。', 'RESULT_UNCERTAIN', 409, uncertain=True)
            return playback
