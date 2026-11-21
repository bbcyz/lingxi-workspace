import {createHash,createCipheriv,webcrypto} from 'node:crypto';
import fs from 'node:fs';
import readline from 'node:readline';
import vm from 'node:vm';
import {getHeapStatistics} from 'node:v8';
import {createCanvas} from '@napi-rs/canvas';
import jsdom from 'jsdom';
import {chromiumMemory,seedDeviceInputs} from './net_age_device.mjs';

const {JSDOM,CookieJar,VirtualConsole}=jsdom;
const entry='https://dev.coc.10086.cn/coc/web6/ageFeedbackV2/?pageId=1879720710683729920&channelId=P00000001090&iteration=true';
const sdkUrl='https://res.coc.10086.cn/res/cdn/coc1-online/fixedPath/coc1/ageFeeRevision/wheel.js';
const gdpUrl='https://res.coc.10086.cn/res/cdn/coc1-online/fixedPath/coc1/gdp-full-H5-3.8.3.js';
const assets={
  'wheel.mjs':'59d08954cb97f10f3905e59e4ae2204393a055e9cd548888fe72b5afa824bc8f',
  'wheel_bg.wasm':'541c0fd95270c5db94228f1a84e5dca6d3508053f8eb3224d56432f70389ca91',
  'page.html':'382396ef861ffefdcacb7b77f125cf97db6c4786a1bf58f1f291d7881642711a',
  'gdp-full-H5-3.8.3.js':'1bac3eb79a6ada15a9a63023ece2289ebceb98cc5cc2b03cc16d0df87dfc10a0',
  'chunk-vendors.a9882ff4.js':'b6caf32eeff2e1ea7b2529fa261bca5d665fc287d51de0c5a6f6e588455f9b3f',
  'app.d7ae5a09.js':'171b4c8362d299aa964f670b05a50f53c64db5ecd0f990032fb8d9d8dfd7131c',
  'chunk-4bb6761c.ba410b8c.js':'2bbdbc97cc268f0cbb27e25d5b99c53dff337f61bdfc0ad3358c75065dec376b',
  'chunk-0ef63f4e.0128ad4f.js':'6b614535555d0a43a6c9f6514c35d662c52252dac30ee1ab8597e1c1cee259b0',
  'chunk-a3acce52.e54386e0.js':'ea3fc3be8d498da5e0d09c5b4cc4ee3c44e4703c46a8bb469bcc777c581c29dc',
};
const files=Object.fromEntries(Object.entries(assets).map(([name,hash])=>{
  const bytes=fs.readFileSync(new URL('./net_age_runtime/'+name,import.meta.url));
  if(createHash('sha256').update(bytes).digest('hex')!==hash)throw new Error('官方网龄资源校验失败：'+name);
  return [name,bytes];
}));

function validate(command){
  if(!Array.isArray(command.cookies)||!command.storage||typeof command.storage!=='object'||
      typeof command.userAgent!=='string'||!command.userAgent.includes('Mobile')||!command.userAgent.includes('leadeon/')){
    throw new Error('网龄 Node 页面参数无效。');
  }
}

function storageValues(storage){
  return Object.fromEntries(Object.keys(storage).map(key=>[key,storage.getItem(key)]));
}

class NetAgePage {
  constructor(command,transport){
    validate(command);
    this.command=command;
    this.transport=transport;
    this.jar=new CookieJar();
    this.local={...command.storage};
    this.session={...(command.sessionStorage||{})};
    this.calls=[];
    this.loads=[];
    for(const cookie of command.cookies){
      const attributes=[cookie.name+'='+cookie.value,'Path='+(cookie.path||'/')];
      if(cookie.domain&&!cookie.hostOnly)attributes.push('Domain='+cookie.domain);
      if(cookie.secure)attributes.push('Secure');
      if(cookie.httpOnly)attributes.push('HttpOnly');
      if(cookie.expires)attributes.push('Expires='+new Date(cookie.expires*1000).toUTCString());
      this.jar.setCookieSync(attributes.join('; '),entry);
    }
  }

  async load(){
    if(this.dom){
      this.local=storageValues(this.dom.window.localStorage);
      this.session=storageValues(this.dom.window.sessionStorage);
      this.dom.window.close();
    }
    this.dom=new JSDOM(files['page.html'].toString('utf8'),{
      url:entry,cookieJar:this.jar,runScripts:'outside-only',pretendToBeVisual:true,
      includeNodeLocations:Boolean(this.command.sourceLocations),
      virtualConsole:new VirtualConsole(),
    });
    const w=this.dom.window;
    const thisPage=this;
    Object.defineProperty(w.Performance.prototype,'memory',{get(){
      if(thisPage.command.deviceProfile)return chromiumMemory();
      const heap=getHeapStatistics();
      return {jsHeapSizeLimit:heap.heap_size_limit,totalJSHeapSize:heap.total_heap_size,
        usedJSHeapSize:heap.used_heap_size};
    }});
    Object.defineProperty(w.navigator,'userAgent',{value:this.command.userAgent,configurable:true});
    Object.defineProperty(w.navigator,'platform',{value:'Linux armv8l',configurable:true});
    Object.defineProperty(w.navigator,'language',{value:'zh-CN',configurable:true});
    Object.defineProperty(w.navigator,'languages',{value:w.Array.from(['zh-CN']),configurable:true});
    Object.defineProperty(w.navigator,'hardwareConcurrency',{value:8,configurable:true});
    Object.defineProperty(w.navigator,'maxTouchPoints',{value:5,configurable:true});
    for(const [key,value] of Object.entries({innerWidth:393,innerHeight:797,outerWidth:393,outerHeight:873,devicePixelRatio:2.75,isSecureContext:true})){
      Object.defineProperty(w,key,{value,configurable:true});
    }
    for(const [key,value] of Object.entries({width:393,height:873,colorDepth:24,pixelDepth:24,orientation:{type:'portrait-primary',angle:0}})){
      Object.defineProperty(w.screen,key,{value,configurable:true});
    }
    w.ontouchstart=null;
    Object.defineProperty(w,'crypto',{value:webcrypto});
    Object.assign(w,{TextEncoder,TextDecoder,Request,Response,Headers,AbortController});
    const canvases=new WeakMap();
    const owner=this;
    const nativeCanvas=element=>{
      let canvas=canvases.get(element);
      if(!canvas){canvas=createCanvas(element.width,element.height);canvases.set(element,canvas);}
      if(canvas.width!==element.width)canvas.width=element.width;
      if(canvas.height!==element.height)canvas.height=element.height;
      return canvas;
    };
    w.CanvasRenderingContext2D=createCanvas(1,1).getContext('2d').constructor;
    w.HTMLCanvasElement.prototype.getContext=function(type){return type==='2d'?nativeCanvas(this).getContext('2d'):null;};
    w.HTMLCanvasElement.prototype.toDataURL=function(){
      const data=nativeCanvas(this).toDataURL();
      owner.canvas={length:data.length,sha256:createHash('sha256').update(data).digest('hex')};
      return data;
    };
    if(this.command.deviceProfile)seedDeviceInputs(w,this.command.deviceProfile);
    // Analytics can initialize its local state but cannot send remote telemetry.
    // Only the original Wheel's gDT is forwarded to the Python account session.
    w.navigator.sendBeacon=()=>false;
    w.XMLHttpRequest.prototype.send=function(){this.dispatchEvent(new w.ProgressEvent('error'));};
    w.fetch=async(input,options={})=>{
      const request=input instanceof Request?input:new Request(String(input),options);
      if(request.url===new URL('./wheel_bg.wasm',sdkUrl).href){
        const response=new Response(files['wheel_bg.wasm'],{status:200,headers:{'content-type':'application/wasm'}});
        if(this.command.responseUrls)Object.defineProperty(response,'url',{value:request.url});
        return response;
      }
      if(request.url!=='https://dev.coc.10086.cn/coc/wasmrc/gDT'||request.method!=='POST'){
        throw new Error('官方网龄组件发出了预期之外的请求。');
      }
      const headers=Object.fromEntries(request.headers);
      Object.assign(headers,{
        'user-agent':this.command.userAgent,'referer':entry,'origin':'https://dev.coc.10086.cn',
        'accept-language':'zh-CN,zh;q=0.9',
        'sec-ch-ua':'"Chromium";v="143", "Not A(Brand";v="24", "Android WebView";v="143"',
        'sec-ch-ua-mobile':'?1','sec-ch-ua-platform':'"Android"',
        'sec-fetch-dest':'empty','sec-fetch-mode':'cors','sec-fetch-site':'same-origin',
        'x-requested-with':'com.greenpoint.android.mc10086.activity',
      });
      const cookie=this.jar.getCookieStringSync(entry);
      if(cookie)headers.cookie=cookie;
      const body=JSON.parse(await request.text());
      if(this.pendingSubmitInteraction){
        const interaction=this.pendingSubmitInteraction;
        this.pendingSubmitInteraction=null;
        this.recordInteraction(interaction);
      }
      const result=await this.transport(body,headers);
      this.calls.push({url:request.url,method:'POST',status:200,responseCode:result.code,
        responseDataLength:typeof result.data==='string'?result.data.length:0});
      const response=new Response(JSON.stringify(result),{status:200,headers:{'content-type':'application/json'}});
      if(this.command.responseUrls)Object.defineProperty(response,'url',{value:request.url});
      return response;
    };
    for(const [key,value] of Object.entries(this.local))w.localStorage.setItem(key,String(value));
    for(const [key,value] of Object.entries(this.session))w.sessionStorage.setItem(key,String(value));
    // The APP's current tab entry and original HomeView.mounted both set this.
    w.sessionStorage.setItem('iteration',new URL(entry).searchParams.get('iteration'));
    const context=this.dom.getInternalVMContext();
    new vm.Script(files['gdp-full-H5-3.8.3.js'].toString('utf8'),{filename:gdpUrl}).runInContext(context);
    w.gdp('init','9e4e5fa7244c6b6e','aa410d5cd21666f5',{
      host:'jiguang.coc.10086.cn',scheme:'https',autotrack:false,compress:true,
    });
    w.gdp('setGeneralProps',{WT_plat:'000_JTAPP'});
    if(this.local.boundMobile){
      const aes=createCipheriv('aes-128-ecb',Buffer.from('9e5702ead4d643fd'),null);
      const user=Buffer.concat([aes.update(this.local.boundMobile,'utf8'),aes.final()]).toString('base64');
      w.gdp('setUserId',user);
    }
    w.gdp('track','pageview',{WT_et:'pageview',WT_event:'H5PageShow'});
    await new Promise(resolve=>setImmediate(resolve));
    for(const name of ['chunk-vendors.a9882ff4.js','app.d7ae5a09.js','chunk-4bb6761c.ba410b8c.js','chunk-0ef63f4e.0128ad4f.js','chunk-a3acce52.e54386e0.js']){
      let source=files[name].toString('utf8');
      if(name.startsWith('app.'))source=source.replace('c.push([0,"chunk-vendors"]),n()','window.__cocRequire=i');
      if(name==='chunk-a3acce52.e54386e0.js'){
        // Expose the original singleton factory at the end of its module, so
        // the original SDK and claim-method source locations remain intact.
        source=source.replace('t["default"]=xs.exports},fa9f:',
          't["default"]=xs.exports;window.__netAgeWarmWheel=jn},fa9f:');
      }
      new vm.Script(source,{filename:'https://res.coc.10086.cn/res/cdn/coc1-online/fixedPath/coc1/ageFeedbackV2/js/'+name}).runInContext(context);
    }
    const axios=w.__cocRequire('82ae');
    axios.defaults.adapter=async config=>{
      if(config.method.toUpperCase()!=='POST'||!String(config.url).endsWith('activities/prize/templatePrize/new')){
        throw new Error('Node preparation attempted an unexpected business request.');
      }
      const wire=typeof config.data==='string'?config.data:JSON.stringify(config.data);
      this.preparedRequest={data:JSON.parse(wire),wire_body:wire,
        headers:Object.fromEntries(Object.entries(config.headers).filter(([,value])=>typeof value==='string'))};
      throw new Error('NODE_PREPARED_BOUNDARY');
    };
    this.component=w.__cocRequire('f5b8').default.components.AfterLogin;
    this.componentConstructor=this.command.vueRuntime?w.__cocRequire('e22e').a.extend(this.component):null;
    this.requestWm=w.__cocRequire('b775').d;
    const module=new vm.SourceTextModule(files['wheel.mjs'].toString('utf8'),{
      context,identifier:sdkUrl,initializeImportMeta(meta){meta.url=sdkUrl;},
    });
    await module.link(()=>{throw new Error('官方网龄 WASM 胶水出现了额外依赖。');});
    await module.evaluate();
    const bootScript=w.document.querySelector("body > script[type='module']");
    const bootSource=bootScript.textContent.replace(/\bmain\(\);\s*$/,'await main();');
    const location=this.command.sourceLocations?this.dom.nodeLocation(bootScript).startTag:null;
    const boot=new vm.SourceTextModule(bootSource,{
      context,identifier:entry,
      lineOffset:location?location.endLine-1:0,columnOffset:location?location.endCol-1:0,
      importModuleDynamically:async url=>{
        if(url!==sdkUrl)throw new Error('Unexpected official module URL.');
        return module;
      },
    });
    await boot.link(()=>{throw new Error('Unexpected static boot import.');});
    await boot.evaluate();
    this.preparedWheel=await w.__netAgeWarmWheel();
    this.wheelPreparedAt=Date.now();
    this.loads.push({cookie_count:this.jar.getCookiesSync(entry).length,
      local_storage_keys:Object.keys(w.localStorage),initialization_time:Date.now(),wheel_ready:true});
  }

  state(){
    const local=storageValues(this.dom.window.localStorage);
    const session=storageValues(this.dom.window.sessionStorage);
    const cookies=this.jar.getCookiesSync(entry).map(cookie=>({
      name:cookie.key,value:cookie.value,domain:cookie.domain,path:cookie.path,
      secure:cookie.secure,httpOnly:cookie.httpOnly,hostOnly:cookie.hostOnly,
      expires:cookie.expires instanceof Date?cookie.expires.getTime()/1000:null,
    }));
    return {cookies,user_agent:this.command.userAgent,
      page_state:{entry,local_storage:local,session_storage:session},
      diagnostics:{calls:this.calls,loads:this.loads,context:{
        local_storage_keys:Object.keys(local).length,canvas:this.canvas,
        wheel_reused:this.wheelReused,wheel_age_ms:Date.now()-this.wheelPreparedAt,
        memory_source:'node-v8',memory:this.dom.window.performance.memory,
        cookie_parts:this.dom.window.document.cookie.split('; ').filter(Boolean).map(part=>({
          name:part.slice(0,part.indexOf('=')),value_length:part.length-part.indexOf('=')-1,
        })),
      }}};
  }

  async encrypt(payload){
    this.pendingSubmitInteraction=this.command.submitInteraction;
    const extra=JSON.parse(payload.jsonExtParam||'{}');
    const state=this.componentConstructor?new this.componentConstructor():this.component.data.call({});
    if(!this.componentConstructor)Object.assign(state,this.component.methods);
    Object.assign(state,{
      showId:payload.lotteryId,sms:extra.smsCode||'',touchCode:extra.touchCode||this.local.channelId,
      messageProductCode:extra.productCode,
      flowSlideVerifyConfigLoaded:true,flowSlideVerifyProvinces:this.command.flowSlideProvinces||'',
      flowSlideAccessToken:extra.accessToken||'',flowSlideVerifiedActivityId:payload.lotteryId,
      isHuoqu:!this.command.probe,
      $route:{query:{channelId:new URL(entry).searchParams.get('channelId')}},
      $requestWm:this.requestWm,
    });
    this.preparedRequest=null;
    if(this.command.probe)state.getPrizeDraw();
    else state.chooseClick();
    await new Promise((resolve,reject)=>{
      const check=()=>{
        if(this.preparedRequest)resolve();
        else if(!state.isSubmitting)reject(new Error('官方领取方法未生成可提交请求。'));
        else setTimeout(check,5);
      };
      check();
    });
    if(!this.preparedRequest)throw new Error('官方领取方法未生成可提交请求。');
    this.wheelReused=state.wheel===this.preparedWheel;
    if(this.componentConstructor)state.$destroy();
    return {...this.state(),...this.preparedRequest};
  }

  close(){this.dom?.window.close();}

  recordInteraction(value){
    if(value?.type!=='click'||!Number.isFinite(value.clientX)||!Number.isFinite(value.clientY)){
      throw new Error('Invalid forwarded click');
    }
    const w=this.dom.window;
    w.document.dispatchEvent(new w.MouseEvent('click',{
      bubbles:true,clientX:value.clientX,clientY:value.clientY,
    }));
    return {};
  }
}

const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
let page,pendingHttp;
const transport=(body,headers)=>new Promise((resolve,reject)=>{
  pendingHttp={resolve,reject};
  process.stdout.write(JSON.stringify({type:'http',body,headers})+'\n');
});
input.on('line',async line=>{
  const message=JSON.parse(line);
  if(message.type==='http_result'){
    const pending=pendingHttp;pendingHttp=undefined;
    if(message.error)pending.reject(new Error(message.error));else pending.resolve(message.value);
    return;
  }
  try{
    if(message.type==='interaction'){
      process.stdout.write(JSON.stringify({ok:true,result:page.recordInteraction(message.value)})+'\n');
      return;
    }
    if(message.type==='prepare'||!page){
      page=new NetAgePage(message,transport);
      await page.load();
      await page.load();
      if(message.type==='prepare'){
        process.stdout.write(JSON.stringify({ok:true,result:page.state()})+'\n');
        return;
      }
    }
    const result=await page.encrypt(message.payload);
    process.stdout.write(JSON.stringify({ok:true,result})+'\n');
  }catch(error){
    process.stdout.write(JSON.stringify({ok:false,code:'NET_AGE_SDK',
      error:'官方网龄安全组件初始化未完成。',detail:String(error.message||'').slice(0,200)})+'\n');
  }finally{
    if(!['prepare','encrypt','interaction'].includes(message.type)){page?.close();input.close();}
  }
});
input.on('close',()=>page?.close());
