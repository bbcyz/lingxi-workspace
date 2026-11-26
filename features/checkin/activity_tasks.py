"""The Wednesday activity's public, token-bound browse-task widget.

Protocol source: act-wedrecharge index.DZx0Ii6d.js / hooks.B4wXkHRT.js.
These callbacks complete the sign-in browse task, not recharge or draw prizes.
"""
import time

from workspace.http_transport import requests
from .client import CheckinError, _data


OPEN_API = 'https://wx.10086.cn/hlwyxhdhub/api/open/_pub/task/'
OPEN_TASKS = {
    '7日打卡 充值8折': ('10', '1', 'wx.10086.cn', '/hlwyxhdhub/act-wedrecharge/index.html'),
    '抢周三88元': ('10', '1', 'wx.10086.cn', '/hlwyxhdhub/act-wedrecharge/index.html'),
}


def _post(session, path, body, *, writing=False):
    try:
        response = session.post(OPEN_API + path, json=body,
            headers={'login-check':'1', 'Content-Type':'application/json;charset=UTF-8'},
            timeout=(8,20), allow_redirects=False)
    except requests.RequestsError as error:
        raise CheckinError('活动浏览回传连接中断，请核对任务进度。',
                           'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK', uncertain=writing) from error
    if response.status_code != 200:
        raise CheckinError(f'活动浏览任务 HTTP {response.status_code}，请核对任务进度。',
                           'UPSTREAM_HTTP', uncertain=writing)
    try:
        result = response.json()
    except ValueError as error:
        raise CheckinError('活动浏览任务响应无效。', 'UPSTREAM_FORMAT', uncertain=writing) from error
    if not isinstance(result, dict):
        raise CheckinError('活动浏览任务响应无效。', 'UPSTREAM_FORMAT', uncertain=writing)
    if writing and result.get('code') != 'SUCCESS':
        raise CheckinError('官方尚未确认活动浏览任务完成。', 'RESULT_UNCERTAIN', 409, uncertain=True)
    return _data(result)


def complete_activity_browse(session, info, *, before_submit):
    start = time.monotonic()
    detail = _post(session, 'getOneTaskInfo', {'jtToken': info['taskToken']})
    if str(detail['status']) in {'1', '2'}:
        return
    duration = float(detail['scanTime'])
    if str(detail['status']) != '0' or not 0 < duration <= 60 or str(detail['taskType']) != '10':
        raise CheckinError('官方活动浏览任务要求已变化。', 'TASK_CHANGED', 409)
    time.sleep(max(0, duration - (time.monotonic() - start)))
    before_submit()
    _post(session, 'openFinish', {'cToken': detail['cToken']}, writing=True)
