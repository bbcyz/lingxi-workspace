import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import readline from 'node:readline';

for(const probe of [false,true])test(`official net-age WASM reuses its instance across two encrypt calls (probe=${probe})`, async () => {
  const child = spawn(process.execPath, ['--experimental-vm-modules','activities/net_age_wheel.mjs'], {stdio:['pipe','pipe','pipe']});
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  const exited = new Promise(resolve => child.on('exit', (code, signal) => resolve({code, signal})));
  const input = {
    probe,
    payload:{lotteryId:'1920407540822495232', provinceCode:'851',
      jsonExtParam:'{"smsCode":"000000","touchCode":"FIXTURE_CHANNEL","productCode":"130000006837"}'},
    cookies:[
      {name:'fixture',value:'public-fixture',domain:'dev.coc.10086.cn',path:'/',httpOnly:false},
      {name:'hidden',value:'private-fixture',domain:'dev.coc.10086.cn',path:'/',httpOnly:true,hostOnly:true},
    ],
    storage:{pageId:'FIXTURE_PAGE',channelId:'FIXTURE_CHANNEL',boundProvinceCode:'851'},
    userAgent:'Mozilla/5.0 (Linux; Android 16; 2211133C; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/143.0.7499.192 Mobile Safari/537.36 leadeon/12.5.6',
  };
  const lines = readline.createInterface({input:child.stdout,crlfDelay:Infinity});
  const timer = setTimeout(() => child.kill(), 15000);
  let requests = 0, results = 0, final, prepared;
  try {
    child.stdin.write(JSON.stringify({type:'prepare',...input})+'\n');
    for await (const line of lines) {
      const message = JSON.parse(line);
      if (message.type === 'http') {
        requests += 1;
        assert.deepEqual(Object.keys(message.body), ['encryptedText']);
        assert.equal(Buffer.from(message.headers['coc-aurora'],'base64').length,256);
        assert.equal(message.headers['user-agent'],input.userAgent);
        assert.match(message.headers.cookie,/^fixture=public-fixture; hidden=private-fixture; /);
        assert.match(message.headers.cookie,/gdp_user_id=/);
        child.stdin.write(JSON.stringify({type:'http_result',value:{code:'0',success:true,
          data:'00000000-0000-4000-8000-'+String(requests).padStart(12,'0')}})+'\n');
      } else if(message.ok && !message.result.data){
        prepared=message.result;
        assert.equal(requests,0);
        assert.equal(prepared.diagnostics.loads.length,2);
        assert.ok(prepared.diagnostics.loads.every(load=>load.wheel_ready));
        child.stdin.write(JSON.stringify({type:'encrypt',payload:input.payload})+'\n');
      } else {
        final = message;
        if(message.ok && ++results===1)child.stdin.write(JSON.stringify({type:'encrypt',payload:input.payload})+'\n');
        else child.stdin.end();
      }
    }
    const {code} = await exited;
    assert.equal(code,0,stderr);
    assert.equal(requests,2);
    assert.equal(results,2);
    assert.equal(final.ok,true,JSON.stringify(final));
    const {data,headers,diagnostics} = final.result;
    assert.deepEqual(JSON.parse(final.result.wire_body),data);
    assert.equal(headers['coc-en-version'],'1586234121417629696');
    assert.equal(Buffer.from(headers['coc-aurora'],'base64').length,256);
    assert.ok(Buffer.from(data.cocEnContent,'base64').length>1000);
    assert.ok(diagnostics.context.cookie_parts.some(cookie=>cookie.name==='fixture'&&cookie.value_length===14));
    assert.ok(!diagnostics.context.cookie_parts.some(cookie=>cookie.name==='hidden'));
    assert.equal(final.result.cookies.find(cookie=>cookie.name==='hidden').hostOnly,true);
    assert.ok(diagnostics.context.local_storage_keys>2);
    assert.equal(diagnostics.loads.length,2);
    assert.deepEqual(diagnostics.loads,prepared.diagnostics.loads);
    assert.match(final.result.page_state.entry,/&iteration=true$/);
    assert.equal(final.result.page_state.session_storage.iteration,'true');
    const visitor=values=>values.cookies.find(cookie=>cookie.name==='gdp_user_id').value;
    assert.equal(visitor(final.result),visitor(prepared));
    assert.ok(Object.keys(final.result.page_state.local_storage).some(key=>key.endsWith('_gdp_last_vst')));
    assert.ok(diagnostics.context.canvas.length>400);
    assert.equal(diagnostics.calls[0].responseCode,'0');
    assert.equal(diagnostics.context.wheel_reused,true);
    assert.ok(diagnostics.context.wheel_age_ms>=0);
    assert.equal(diagnostics.context.memory_source,'node-v8');
    assert.ok(diagnostics.context.memory.jsHeapSizeLimit>=diagnostics.context.memory.totalJSHeapSize);
    assert.ok(diagnostics.context.memory.totalJSHeapSize>=diagnostics.context.memory.usedJSHeapSize);
    assert.ok(diagnostics.context.memory.usedJSHeapSize>0);
  } finally {
    clearTimeout(timer); lines.close();
    if (child.exitCode === null) child.kill();
  }
});

test('a rejected device token stops the Node bridge before returning business ciphertext', async () => {
  const child = spawn(process.execPath, ['--experimental-vm-modules','activities/net_age_wheel.mjs'], {stdio:['pipe','pipe','ignore']});
  const lines = readline.createInterface({input:child.stdout,crlfDelay:Infinity});
  const timer = setTimeout(() => child.kill(),15000);
  let requests = 0, final;
  try {
    child.stdin.write(JSON.stringify({
      payload:{lotteryId:'1920407540822495232',jsonExtParam:'{"smsCode":"000000","productCode":"130000006837"}'}, cookies:[], storage:{boundProvinceCode:'851'},
      userAgent:'Mozilla/5.0 Mobile leadeon/12.5.6',
    })+'\n');
    for await (const line of lines) {
      const message = JSON.parse(line);
      if (message.type === 'http') {
        requests += 1;
        child.stdin.write(JSON.stringify({type:'http_result',value:{code:'1',success:false,data:null}})+'\n');
      } else {
        final = message;
        child.stdin.end();
      }
    }
    assert.equal(requests,1);
    assert.equal(final.ok,false);
    assert.equal(final.result,undefined);
  } finally {
    clearTimeout(timer); lines.close();
    if (child.exitCode === null) child.kill();
  }
});
