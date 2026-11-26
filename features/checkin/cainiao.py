"""Cainiao's own-login homepage exchange callback, using compatible MTOP auth."""
import json
import time
from urllib.parse import quote, urlsplit

from .client import CheckinError
from .taobao import CheckinTaobao

TASK_NAME = '去菜鸟0元领好物'
TASK = ('6','1','cainiao')
ENTRY = 'https://page.cainiao.com/mcn/cainiao-score/web/index.html'
PROFILE = 'mtop.cncreditmarket.base.queryUserInfo'
CALLBACK = 'mtop.cnactivitycenter.task.finishExternalTask'


class CheckinCainiao(CheckinTaobao):
    def complete(self,item,info,*,before_submit):
        back = urlsplit(info['backUrl'])
        if ((str(item['taskCategory']),str(item['taskType']),item['jumpUrl'])!=TASK
                or (back.netloc,back.path)!=('wx.10086.cn','/qwhdhub/qwhdmark/1021122301')):
            raise CheckinError('菜鸟任务入口已变化，未提交回调。','TASK_CHANGED',409)
        parameters={'disableNav':'YES','__webview_options__':'fullScreen=YES&fullscreen=YES&showStateBar=YES',
                    'source':'cnMobile','token':info['cToken']}
        target=ENTRY+'?disableNav=YES&__webview_options__=fullScreen%3DYES%26fullscreen%3DYES%26showStateBar%3DYES#/?source=cnMobile&token='+quote(info['cToken'],safe="~()*!.'-")
        with self._session() as client:
            began=time.monotonic()
            user=self._call(client,PROFILE,{'bizType':'1'})['result']
            if user.get('accountType') not in {'taobao','cainiao'}:
                raise CheckinError('菜鸟未确认本人关联身份，未提交任务。','TAOBAO_LOGIN_REQUIRED',401)
            page=client.get(target,timeout=(10,20),allow_redirects=False)
            if page.status_code!=200:
                raise CheckinError('菜鸟任务页面未成功返回。','TASK_PAGE_FAILED')
            time.sleep(max(0,float(info['scanTime'])-(time.monotonic()-began)))
            before_submit()
            result=self._call(client,CALLBACK,{'token':info['cToken'],'source':'cnMobile',
                'param':json.dumps(parameters,ensure_ascii=False,separators=(',',':'))},writing=True)
            if result.get('result') is False:
                raise CheckinError('菜鸟回调未确认成功，请先查询移动任务结果。','RESULT_UNCERTAIN',409,uncertain=True)
            # The host verifies the parent task and award separately; a normal
            # MTOP acknowledgement alone is never counted as a completed task.
            return {'login_verified':True,'callback_acknowledged':True}
