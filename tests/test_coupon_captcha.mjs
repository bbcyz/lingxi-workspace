import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function load(search='', width=300) {
  const nodes=Object.fromEntries(['status','verify'].map(id=>[id,{hidden:true,textContent:'',replaceChildren(){this.cleared=true;}}]));
  nodes.verify.clientWidth=width;
  const timers=[];
  let config,script,message,loadFail,resizeObserver,windowResize,height,reloaded=false;
  const ctx={URLSearchParams,Date,location:{origin:'http://localhost:5500',search,reload(){reloaded=true;}},
    document:{getElementById:id=>nodes[id],createElement:()=>({}),head:{append(value){script=value;}},
      documentElement:{classList:{add(){}}},
      body:{getBoundingClientRect:()=>({height})}},
    ResizeObserver:class{constructor(callback){resizeObserver=callback;} observe(){}},
    parent:{postMessage(value,origin){message={value,origin};}},
    setTimeout(fn,delay){timers.push({fn,delay});return timers.length;},
    window:{addEventListener(event,callback){if(event==='resize')windowResize=callback;},
      _zw:{Captcha(el,options){config=options;return {on(event,fn){loadFail=fn;}};}}}};
  vm.runInNewContext(fs.readFileSync(new URL('../static/coupon-captcha.js',import.meta.url),'utf8'),ctx);
  script.onload();
  return {nodes,config,script,timers,get loadFail(){return loadFail;},get message(){return message;},get reloaded(){return reloaded;},
    resize(nextWidth,nextHeight){nodes.verify.clientWidth=nextWidth;height=nextHeight;windowResize();resizeObserver();}};
}

test('payment challenge preserves provider IDs and automatically replaces a failed slider',()=>{
  const env=load('?app_id=official-pay-app&risk_id=official-risk');
  assert.equal(env.config.appId,'official-pay-app');
  assert.equal(env.config.m.riskId,'official-risk');
  env.config.fail();
  assert.equal(env.nodes.verify.cleared,true);
  assert.match(env.nodes.status.textContent,/自动获取新的滑块/);
  assert.equal(env.timers.length,1);
  assert.equal(env.timers[0].delay,500);
  env.timers[0].fn();assert.equal(env.reloaded,true);
});

test('successful human token returns only to same-origin parent',()=>{
  const env=load();env.config.success('official-human-token');
  assert.equal(env.message.origin,'http://localhost:5500');
  assert.equal(env.message.value.type,'coupon-captcha-success');
  assert.equal(env.message.value.token,'official-human-token');
  assert.equal(env.config.m,'');
  env.config.fail();env.loadFail();
  assert.equal(env.timers.length,0,'completed verification must not reload');
});

test('login challenge uses the APP slider identity and m1 device endpoint',()=>{
  const env=load('?context=login&app_id=02aecbb71abe2113e42a602056797e09&device_path=m1');
  assert.equal(env.config.appId,'02aecbb71abe2113e42a602056797e09');
  assert.match(env.config.constIDServer,/\/udid\/m1$/);
  assert.equal(env.config.cacheStorage,true);
  assert.equal(env.config.showVoice,true);
  env.config.success('official-login-risk-token');
  assert.equal(env.message.value.type,'login-captcha-success');
  assert.equal(env.message.value.token,'official-login-risk-token');
  assert.match(env.nodes.status.textContent,/继续登录/);
});

test('login challenge fits a narrow frame and reports its full content height',()=>{
  const env=load('?context=login',218);
  assert.equal(env.config.width,218);
  env.resize(218,311.5);
  assert.equal(env.message.value.type,'login-captcha-size');
  assert.equal(env.message.value.height,312);
  assert.equal(env.message.origin,'http://localhost:5500');
  assert.equal(env.timers.length,0);

  env.resize(200,280);
  env.resize(200,80);
  assert.equal(env.timers.length,1);
});

test('duplicate failure callbacks schedule one reload and discard success from old slider',()=>{
  const env=load();env.config.fail();env.config.fail();env.config.failSafe();env.loadFail();
  assert.equal(env.timers.length,1);
  env.config.success('stale-token');assert.equal(env.message,undefined);
});

test('provider load failures automatically fetch a new verification without a button',()=>{
  for(const type of ['script','widget']){
    const env=load();
    if(type==='script')env.script.onerror();else env.loadFail();
    assert.match(env.nodes.status.textContent,/自动重新获取/);
    assert.equal(env.timers.length,1);assert.equal(env.timers[0].delay,3000);
    env.timers[0].fn();assert.equal(env.reloaded,true);
  }
  assert.doesNotMatch(fs.readFileSync(new URL('../static/coupon-captcha.html',import.meta.url),'utf8'),/id="retry"/);
});

test('account manager embeds the login slider and resumes with its official token',()=>{
  const controller=fs.readFileSync(new URL('../static/portal-controller.js',import.meta.url),'utf8');
  const template=fs.readFileSync(new URL('../templates/accounts.html',import.meta.url),'utf8');
  assert.match(template,/id="login-slider-frame"/);
  assert.match(template,/验证通过后会自动继续当前登录/);
  assert.doesNotMatch(template,/请在手机中国移动 APP 完成滑块/);
  assert.match(controller,/type==='login-captcha-success'/);
  assert.match(controller,/slider_token:sliderToken/);
  assert.match(controller,/manager-login-form'\)\.requestSubmit\(\)/);
});
