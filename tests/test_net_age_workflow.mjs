import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.handlers = {};
    this.dataset = {};
    this.style = {};
    this.classList = {toggle() {}};
  }
  addEventListener(event, handler) { this.handlers[event] = handler; }
  setAttribute() {}
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  querySelector(selector) {
    return this.children.find(item => item.tag === selector || '.' + item.className === selector)
      || this.children.map(item => item.querySelector(selector)).find(Boolean);
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() { this.focused = true; }
  trigger(event = 'click', details = {}) {
    return this.handlers[event]?.({preventDefault() {}, currentTarget:this, target:this, ...details});
  }
}

const flush = () => new Promise(resolve => setImmediate(resolve));

async function fixture({needsSlider = false, group = needsSlider ? 'member' : 'flow',
  smsResponses = [], sliderFails = false, benefit, campaignWait, submitResult, submitStatus = 200,
  accountStatus = 'active', notice = '', surpriseAccount = {}, surpriseWait, statusFails = false, timers} = {}) {
  const elements = new Map(), calls = [], documentEvents = new Element('document');
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const choice = group === 'birthday' ? 'cinema' : needsSlider ? 'bilibili' : 'monthly';
  const row = new Element('li');
  row.dataset.ageGroup = group;
  const title = new Element('h3'); title.textContent = '待领取权益';
  const actions = new Element('[data-age-actions]');
  row.append(title, new Element('[data-age-status]'), new Element('[data-age-detail]'), actions);
  const fetch = async (path, options) => {
    const endpoint = path.split('?')[0];
    const body = options?.body && JSON.parse(options.body);
    calls.push({endpoint, body});
    const response = (data, status = 200) => ({ok:status === 200, status, json:async () => data});
    switch (endpoint) {
      case '/api/net-age/accounts': return response({accounts:[{id:'account-a',status:accountStatus,phone_masked:'138****0314'}]});
      case '/api/net-age/status': return statusFails ? response({error:'主会话需重新登录',code:'APP_LOGIN_REQUIRED'},401)
        : response({rows:{[group]:benefit ?? {available:true,offers:[{id:choice,name:'待领取权益'}]}}});
      case '/api/net-age/surprise/auto':
        if (body) {
          if (surpriseWait) await surpriseWait;
          return response({queued:true,message:'已安排核对并使用官方可用次数。'});
        }
        return response({enabled:true,active:true,accounts:[{id:'account-a',state:'completed',remaining:0,
          message:'今日已核对，暂无可用抽奖次数。',draws:[],prizes:[],...surpriseAccount}]});
      case '/api/seven-day/status':
        if (campaignWait) await campaignWait;
        return response({},404);
      case '/api/net-age/claim':
        assert.equal('probe' in body,false);
        return response({operation:'operation-a',offer:'待领取权益',needs_slider:needsSlider,notice});
      case '/api/net-age/verification/challenge': return response({ground:'fixture',patch:'fixture',ordinate:20});
      case '/api/net-age/verification/slider': return sliderFails ? response({error:'官方滑块校验未通过'},400) : response({});
      case '/api/net-age/verification/auto-slider': return sliderFails ? response({error:'官方滑块校验未通过'},400) : response({});
      case '/api/net-age/verification/submit': return response(submitResult, submitStatus);
      case '/api/net-age/verification/sms': {
        const next = smsResponses.shift();
        return next ? await next(response) : response({message:'官方已发送验证码'});
      }
      default: throw new Error(`Unexpected request ${path}`);
    }
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/net-age.js', import.meta.url), 'utf8'), {
    document:{getElementById:get,querySelectorAll:() => [row],createElement:tag => new Element(tag),
      addEventListener:(...args) => documentEvents.addEventListener(...args)},
    window:{addEventListener() {},dispatchEvent() {}},
    fetch, Event, setTimeout:timers ? callback => {timers.push(callback);return timers.length;} : setTimeout, clearTimeout, FormData:class {
      get() {
        return get('age-options').children.find(label => label.children[0].checked)?.children[0].value || choice;
      }
    },
  });
  await flush();
  const smsCalls = () => calls.filter(call => call.endpoint === '/api/net-age/verification/sms');
  const open = () => actions.children.find(item => ['领取','选择领取','重新领取'].includes(item.textContent)).trigger();
  const confirm = () => get('age-confirm-form').trigger('submit');
  return {get,calls,smsCalls,open,confirm,row,actions,documentEvents};
}

test('an expired primary APP login does not block querying a valid net-age session', async () => {
  const {row,actions,calls,smsCalls} = await fixture({accountStatus:'login_required',
    benefit:{status:'可领取', available:true, offers:[{id:'monthly',name:'3GB 流量月包券'}]},
  });
  assert.ok(calls.some(call => call.endpoint === '/api/net-age/status'));
  assert.equal(row.querySelector('[data-age-status]').textContent, '可领取');
  assert.equal(actions.children[0].textContent, '领取');
  assert.equal(smsCalls().length, 0);
});

test('surprise page loads only local automatic-task status and uses actual quota', async () => {
  const {row,calls,actions,smsCalls} = await fixture({group:'surprise',
    benefit:{status:'专区剩余 1 次',section_remaining:1}});
  assert.equal(row.querySelector('[data-age-status]').textContent,'剩余 0 次');
  assert.match(row.querySelector('[data-age-detail]').textContent,/每日检查全部有效账号，有次数就抽/);
  assert.equal(actions.children[0].textContent,'立即检查');
  assert.equal(calls.filter(call=>call.endpoint==='/api/net-age/surprise/auto').length,1);
  assert.equal(calls.some(call=>call.body),false);
  assert.equal(calls.some(call=>call.endpoint==='/api/net-age/surprise'),false);
  assert.equal(smsCalls().length,0);
});

test('surprise manual check queues once and then reads local results', async () => {
  let release;
  const surpriseWait=new Promise(resolve=>{release=resolve;});
  const {actions,calls,smsCalls}=await fixture({group:'surprise',surpriseWait});
  const pending=actions.children[0].trigger();
  await flush();
  assert.equal(actions.children[0].disabled,true);
  await actions.children[0].trigger();
  assert.equal(calls.filter(call=>call.body).length,1);
  assert.deepEqual(calls.find(call=>call.body).body,{id:'account-a'});
  release();await pending;
  assert.equal(calls.filter(call=>call.endpoint==='/api/net-age/surprise/auto'&&!call.body).length,2);
  assert.equal(actions.children[0].disabled,false);
  assert.equal(smsCalls().length,0);
});

test('surprise running status polls locally without requeueing business', async () => {
  const timers=[];
  const {row,actions,calls}=await fixture({group:'surprise',surpriseAccount:{state:'running',running:true},timers});
  assert.equal(row.querySelector('[data-age-status]').textContent,'正在核对并抽取');
  assert.equal(actions.children[0].disabled,true);
  await timers.shift()();
  assert.equal(calls.filter(call=>call.endpoint==='/api/net-age/surprise/auto').length,2);
  assert.equal(calls.some(call=>call.body),false);
});

test('surprise results distinguish past prizes, draws and required shipping', async () => {
  const {row}=await fixture({group:'surprise',surpriseAccount:{draws:[
    {confirmed:true,won:true,name:'实物测试奖品',needs_address:true}],
    prizes:[{name:'历史流量券',date:'2026-10-01'}]}});
  const detail=row.querySelector('[data-age-detail]').textContent;
  assert.match(detail,/今日已核对 1 次抽奖，抽中 实物测试奖品/);
  assert.match(detail,/48 小时/);
  assert.match(detail,/历史奖品：历史流量券（2026-10-01）/);
  assert.doesNotMatch(detail,/已到账|今日中奖/);
});

test('surprise uncertainty is visible despite an expired primary profile session', async () => {
  const {row,actions}=await fixture({group:'surprise',accountStatus:'login_required',statusFails:true,
    surpriseAccount:{state:'failed',uncertain:true,message:'上次抽奖结果待官方核对'}});
  assert.equal(row.querySelector('[data-age-status]').textContent,'结果待核对');
  assert.match(row.querySelector('[data-age-detail]').textContent,/结果待官方核对/);
  assert.equal(actions.children[0].disabled,false);
});

test('a single offer goes straight to SMS without a radio choice or diagnostic mode', async () => {
  const {get,open,calls,smsCalls} = await fixture();
  assert.equal(calls.some(call=>call.endpoint==='/api/net-age/claim'),false);
  assert.equal(smsCalls().length,0);
  await open();
  assert.equal(get('age-options').hidden,true);
  assert.equal(get('age-options').children.length,0);
  assert.equal(get('age-verification').hidden,false);
  assert.equal(get('age-sms-section').hidden,false);
  assert.equal(get('age-code').required,true);
  assert.equal(get('age-code').focused,true);
  assert.equal(get('age-submit').textContent,'确认领取');
  assert.equal(smsCalls().length,1);
  assert.deepEqual(calls.find(call=>call.endpoint==='/api/net-age/claim').body,
    {id:'account-a',group:'flow',choice:'monthly'});
  assert.equal(calls.some(call=>call.endpoint.endsWith('/probe') || call.endpoint.endsWith('/submit')),false);
});

test('multiple offers still wait for a selection and preserve the binding notice', async () => {
  const {get,open,confirm,calls,smsCalls} = await fixture({needsSlider:true,group:'member',
    notice:'请确认已绑定 QQ 音乐账号。本月只能领取一次。',
    benefit:{available:true,offers:[{id:'youku',name:'优酷视频'},{id:'qq-music',name:'QQ 音乐'}]}});
  await open();
  assert.equal(get('age-options').hidden,false);
  assert.equal(smsCalls().length,0);
  assert.equal(calls.some(call=>call.endpoint==='/api/net-age/claim'),false);
  const inputs=get('age-options').children.map(label=>label.children[0]);
  inputs[0].checked=false;inputs[1].checked=true;
  await confirm();
  assert.deepEqual(calls.find(call=>call.endpoint==='/api/net-age/claim').body,
    {id:'account-a',group:'member',choice:'qq-music'});
  assert.equal(smsCalls().length,1);
  assert.match(get('age-confirm-note').textContent,/已绑定 QQ 音乐账号/);
  assert.equal(calls.some(call=>call.endpoint.endsWith('/submit')),false);
});

test('the net-age dialog no longer contains a diagnostic checkbox', () => {
  const html=fs.readFileSync(new URL('../templates/net_age.html',import.meta.url),'utf8');
  assert.doesNotMatch(html,/age-probe|仅验证，不发送短信/);
});

test('desktop pointer coordinates are not attached to the mobile protocol', async () => {
  const {get,open,confirm,calls,documentEvents} = await fixture({needsSlider:true,group:'member',
    submitResult:{code:'1000008051',error:'火爆',operation_active:true},submitStatus:502});
  await open();
  documentEvents.trigger('mousemove',{isTrusted:false,clientX:1,clientY:2});
  documentEvents.trigger('mousemove',{isTrusted:true,clientX:1600,clientY:900});
  get('age-code').value='123456';await confirm();
  const submit=calls.find(x=>x.endpoint.endsWith('/submit')).body;
  assert.equal('interaction' in submit,false);
});

test('the previous member receipt is visible and the member can explicitly prepare again', async () => {
  const {row,actions,calls,get,open,confirm,smsCalls} = await fixture({needsSlider:true, benefit:{
    status:'结果待确认', description:'本月暂未查到会员领取订单', recheck:true, retry_available:true,
    offers:[{id:'bilibili', name:'哔哩哔哩'}],
    last_received:{name:'哔哩哔哩月度大会员', date:'2026-10-29'},
  }});
  assert.equal(row.querySelector('[data-age-status]').textContent, '结果待确认');
  assert.equal(row.querySelector('[data-age-detail]').textContent,
    '本月暂未查到会员领取订单 · 最近领取 哔哩哔哩月度大会员（2026-10-29）');
  assert.deepEqual(actions.children.map(item => item.textContent), ['核对结果','重新领取']);
  assert.equal(calls.some(call => call.endpoint === '/api/net-age/claim'), false);
  await open();
  assert.equal(get('age-options').children.length,0);
  assert.equal(get('age-submit').textContent, '确认领取');
  assert.deepEqual(calls.find(call => call.endpoint === '/api/net-age/claim').body,
    {id:'account-a',group:'member',choice:'bilibili',retry:true});
  assert.equal(smsCalls().length, 1);
});

test('a rejected monthly flow claim offers retry and requests new verification only after confirmation', async () => {
  const {row,actions,calls,get,confirm,smsCalls} = await fixture({
    benefit:{status:'本次领取失败', recheck:true, retry_available:true, offers:[{id:'monthly', name:'3GB 流量月包券'}]},
    submitResult:{code:'10', confirmed:false, message:'本次领取失败'},
  });
  assert.deepEqual(actions.children.map(item => item.textContent), ['核对结果','重新领取']);
  await actions.children[1].trigger();
  assert.equal(get('age-options').children.length,0);
  assert.equal(get('age-submit').textContent, '确认领取');
  assert.deepEqual(calls.find(call => call.endpoint === '/api/net-age/claim').body,
    {id:'account-a',group:'flow',choice:'monthly',retry:true});
  assert.equal(smsCalls().length, 1);
  assert.equal(calls.some(call => call.endpoint === '/api/net-age/verification/submit'), false);
  get('age-code').value = '123456';
  await confirm();
  assert.equal(get('age-confirm').open, true);
  assert.equal(get('age-confirm-note').textContent, '本次领取失败');
  assert.equal(get('age-confirm-note').hidden, false);
  assert.equal(get('age-code').value, '');
  assert.equal(get('age-verification').hidden, true);
  assert.equal(get('age-submit').textContent, '确认重新领取');
  assert.equal(row.querySelector('[data-age-status]').textContent, '本次领取失败');
  assert.deepEqual(actions.children.map(item => item.textContent), ['核对结果','重新领取']);
  assert.equal(calls.filter(call => call.endpoint === '/api/net-age/status').length, 1);
  assert.equal(smsCalls().length, 1);
  await confirm();
  assert.equal(calls.filter(call => call.endpoint === '/api/net-age/verification/submit').length, 1);
  assert.equal(calls.filter(call => call.endpoint === '/api/net-age/claim').at(-1).body.retry, true);
  assert.equal(smsCalls().length, 2);
});

test('a first monthly claim failure switches to explicit retry without retaining its OTP', async () => {
  const {get,calls,open,confirm,smsCalls} = await fixture({
    submitResult:{code:'10', confirmed:false, message:'本次领取失败'},
  });
  await open();
  assert.equal(calls.find(call => call.endpoint === '/api/net-age/claim').body.retry, undefined);
  get('age-code').value = '123456';
  await confirm();
  assert.equal(get('age-confirm').open, true);
  assert.equal(get('age-code').value, '');
  assert.equal(smsCalls().length, 1);
  await confirm();
  assert.equal(calls.filter(call => call.endpoint === '/api/net-age/claim').at(-1).body.retry, true);
  assert.equal(calls.filter(call => call.endpoint === '/api/net-age/verification/submit').length, 1);
  assert.equal(smsCalls().length, 2);
});

test('an approved claim opens the form and requests one code without another click', async () => {
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  const {get,smsCalls,open,confirm} = await fixture({smsResponses:[async response => {
    await waiting; return response({message:'官方已发送验证码'});
  }]});
  assert.equal(smsCalls().length, 0);
  const submitting = open();
  await flush();
  await open();
  await confirm();
  await get('age-send-sms').trigger();
  assert.deepEqual(smsCalls().map(call => call.body), [{id:'account-a',operation:'operation-a'}]);
  assert.equal(get('age-submit').disabled, true);
  release();
  await submitting;
  assert.equal(get('age-code').focused, true);
  assert.equal(get('age-submit').disabled, false);
  assert.equal(get('age-send-sms').textContent, '重新获取验证码');
});

test('birthday choices appear while the separate check-in query is pending', async () => {
  let release;
  const campaignWait = new Promise(resolve => { release = resolve; });
  const {get,calls,actions,confirm,smsCalls} = await fixture({needsSlider:true,group:'birthday',campaignWait,benefit:{
    status:'上次领取失败', recheck:true, retry_available:true,
    offers:[{id:'phone',name:'话费充值满减券'},{id:'cinema',name:'电影票抵扣券'}],
  }});
  assert.deepEqual(actions.children.map(item => item.textContent), ['核对结果','重新准备']);
  actions.children[1].trigger();
  assert.match(get('age-confirm-note').textContent, /请确认今年尚未领取/);
  assert.equal(calls.some(call => call.endpoint === '/api/net-age/claim'), false);
  const inputs = get('age-options').children.map(label => label.children[0]);
  assert.deepEqual(inputs.map(input => input.value), ['phone','cinema']);
  inputs[0].checked = false;
  inputs[1].checked = true;
  await confirm();
  assert.deepEqual(calls.find(call => call.endpoint === '/api/net-age/claim').body,
    {id:'account-a',group:'birthday',choice:'cinema',retry:true});
  assert.equal(smsCalls().length, 1);
  release();
});

test('birthday error 10 is shown immediately without requerying orders', async () => {
  const {get,calls,open,confirm,row,actions} = await fixture({needsSlider:true,group:'birthday',
    submitResult:{code:'10',confirmed:false,message:'官方加密参数校验失败（10）'},
  });
  await open();
  get('age-code').value = '123456';
  await confirm();
  assert.equal(calls.filter(call=>call.endpoint==='/api/net-age/status').length, 1);
  assert.equal(row.querySelector('[data-age-status]').textContent, '上次领取失败');
  assert.equal(row.querySelector('[data-age-detail]').textContent, '官方加密参数校验失败（10）');
  assert.deepEqual(actions.children.map(item=>item.textContent), ['核对结果','重新领取']);
});

test('rejected birthday device verification resets the form and keeps both choices', async () => {
  const {get,calls,open,confirm,smsCalls} = await fixture({needsSlider:true,group:'birthday',submitStatus:502,
    benefit:{available:true,offers:[{id:'phone',name:'话费充值满减券'},{id:'cinema',name:'电影票抵扣券'}]},
    submitResult:{code:'GDT_REJECTED',error:'官方设备认证未完成 · 追踪号 fixture-123'},
  });
  open();
  const inputs = get('age-options').children.map(label=>label.children[0]);
  inputs[0].checked = false;
  inputs[1].checked = true;
  await confirm();
  get('age-code').value = '123456';
  await confirm();

  assert.equal(get('age-options').hidden, false);
  assert.equal(get('age-verification').hidden, true);
  assert.equal(get('age-code').value, '');
  assert.equal(get('age-code').required, false);
  assert.equal(get('age-submit').textContent, '确认领取');
  assert.deepEqual(inputs.map(input=>[input.value,input.checked]), [['phone',false],['cinema',true]]);
  assert.match(get('age-confirm-note').textContent, /追踪号 fixture-123/);
  assert.equal(calls.filter(call=>call.endpoint === '/api/net-age/verification/submit').length, 1);
  assert.equal(smsCalls().length, 1);
});

for (const sliderFails of [false, true]) {
  test(`automatic slider gates SMS (accepted: ${!sliderFails})`, async () => {
    const {get,calls,smsCalls,open,confirm} = await fixture({needsSlider:true,sliderFails});
    await open();
    assert.deepEqual(calls.filter(call => call.endpoint.startsWith('/api/net-age/verification/')).map(call=>call.endpoint.split('/').at(-1)),
      sliderFails ? ['auto-slider'] : ['auto-slider','sms']);
    assert.equal(get('age-submit').disabled, sliderFails);
    assert.equal(get('age-send-sms').disabled,false);
    assert.equal(smsCalls().length,sliderFails ? 0 : 1);
  });
}

test('busy member response waits for an explicit retry and preserves SMS', async () => {
  const {get,calls,smsCalls,open,confirm} = await fixture({needsSlider:true,submitStatus:502,
    submitResult:{error:'活动火爆',code:'1000008051',trace_id:'busy-trace',operation_active:true}});
  await open();
  get('age-code').value='123456';await confirm();
  assert.match(get('age-confirm-note').textContent,/busy-trace/);
  assert.equal(calls.filter(c=>c.endpoint.endsWith('/submit')).length,1);
  assert.equal(get('age-code').value,'123456');
  assert.equal(get('age-submit').disabled,false);
  await confirm();
  assert.equal(calls.filter(c=>c.endpoint.endsWith('/submit')).length,2);
  assert.equal(calls.filter(c=>c.endpoint==='/api/net-age/claim').length,1);
  assert.equal(smsCalls().length,1);
});

for (const group of ['member', 'birthday']) {
  test(`resending a ${group} SMS clears the old code and verifies automatically`, async () => {
    const {get,calls,smsCalls,open,confirm} = await fixture({needsSlider:true,group});
    await open();
    get('age-code').value = '123456';
    await get('age-send-sms').trigger();
    assert.deepEqual(calls.slice(-2).map(x=>x.endpoint.split('/').at(-1)),['auto-slider','sms']);
    assert.equal(get('age-code').value, '');
    assert.equal(smsCalls().length, 2);
    assert.equal(get('age-submit').disabled, false);
  });
}

test('a failed automatic send leaves an explicit manual retry', async () => {
  const {get,smsCalls,open,confirm} = await fixture({smsResponses:[response=>response({error:'短信接口暂不可用'},502)]});
  await open(); await flush();
  assert.equal(smsCalls().length, 1);
  assert.equal(get('age-confirm-note').textContent, '短信接口暂不可用');
  assert.equal(get('age-submit').disabled, true);
  assert.equal(get('age-send-sms').disabled, false);
  await get('age-send-sms').trigger();
  assert.equal(smsCalls().length, 2);
  assert.equal(get('age-submit').disabled, false);
});
