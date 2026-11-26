import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.attributes={};this.style={};this.value='';this.hidden=false;this.disabled=false;this.className='';this.textContent='';}
  addEventListener(name,handler){this.handlers[name]=handler;}
  setAttribute(name,value){this.attributes[name]=value;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  contains(node){return this===node||this.children.some(child=>child.contains(node));}
  click(){if(!this.disabled)return this.handlers.click?.();}
  focus(){this.focused=true;}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const text=node=>[node.textContent,...node.children.map(text)].join(' ');
const withClass=(root,name)=>[...(root.className.split(' ').includes(name)?[root]:[]),...root.children.flatMap(child=>withClass(child,name))];
const account=(id,extra={})=>({id,name:id==='first'?'主号':'副号',phone_masked:id==='first'?'138****0000':'139****0001',status:'active',credential_revision:1,...extra});
const bill=(extra={})=>({month:'202611',period:'2026年11月01日 — 2026年11月30日',total:'555.40',payable:'545.87',original:'1087.00',discount:'531.60',rebate:'9.53',balance:'17044.64',queried_at:'2026-11-06T10:15:30+08:00',items:[
  {name:'套餐及固定费',original:'590.70',discount:'487.90',amount:'102.80',children:[
    {name:'主套餐',original:'590.70',discount:'487.90',amount:'102.80',children:[{name:'套餐月费',original:null,discount:null,amount:'102.80',children:[]}]},
  ]},
  {name:'增值业务费',original:'496.30',discount:'43.70',amount:'452.60',children:[]},
],...extra});
const usage=(extra={})=>({overview:[
  {key:'flow',name:'通用流量',unit:'GB',total:'60.00',used:'12.50',remaining:'47.50',scope:'国内通用'},
  {key:'voice',name:'国内通话',unit:'分钟',total:'500',used:'0',remaining:'500'},
  {key:'sms',name:'短信',unit:'条',total:null,used:0,remaining:null},
],groups:[{key:'flow',name:'国内通用流量',items:[{name:'5G套餐流量',unit:'GB',total:'60.00',used:'12.50',remaining:'47.50',scope:'国内（不含港澳台）',start_at:'2026-11-01',end_at:'2026-11-30',description:'当月有效',children:[{name:'结转流量',unit:'MB',total:'1024',used:0,remaining:'1024'}]}]}],notes:['套餐余量以实际使用情况为准。'],queried_at:'2026-11-06T10:15:30+08:00',...extra});
const services=(extra={})=>({groups:[{name:'手机 · 我的套餐',items:[{name:'5G畅享套餐',fee:'128元/月',status:'已生效',fields:[{name:'订购时间',value:'2026-01-01 08:00:00'},{name:'有效期',value:'2026-01-01 至 2026-12-31'},{name:'业务说明',value:'套餐内含国内语音和流量。'}],children:[{name:'来电显示',fee:'免费',status:'已生效',fields:[{name:'业务类型',value:'套餐附属业务'}],children:[]}]}]},{name:'增值业务',items:[{name:'视频会员',fee:null,status:null,fields:[{name:'业务说明',value:null}],children:[]}]}],notes:['已订业务资费以实际生效规则为准。'],queried_at:'2026-11-06T10:15:30+08:00',...extra});

async function fixture(options={}){
  const elements=new Map(),calls=[],posts=[],timers=new Map(),timeouts=new Map(),events={},dispatched=[],get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const document={getElementById:get,createElement:tag=>new Element(tag),activeElement:null};
  get('usage-plan-control').append(get('usage-plan'),get('usage-plan-popover'));
  const control={accounts:options.accounts||[account('first'),account('second')],months:options.months||['202611','202609','202610'],default_month:options.default_month||'202611',query:options.query,usageQuery:options.usageQuery,servicesQuery:options.servicesQuery,accountError:null};
  const fetch=async (url,init)=>{
    calls.push(url);
    let response;
    if(url==='/api/bills/accounts')response=control.accountError||{data:{accounts:structuredClone(control.accounts),months:[...control.months],default_month:control.default_month}};
    else if(url.startsWith('/api/bills/query?')){
      const query=new URLSearchParams(url.split('?')[1]);
      response=control.query?await control.query({id:query.get('login_id'),month:query.get('month'),calls}):{data:bill({month:query.get('month')})};
    }else if(url.startsWith('/api/usage/query?')){
      const query=new URLSearchParams(url.split('?')[1]);
      response=control.usageQuery?await control.usageQuery({id:query.get('login_id'),calls}):{data:usage()};
    }else if(url.startsWith('/api/services/query?')){
      const query=new URLSearchParams(url.split('?')[1]);
      response=control.servicesQuery?await control.servicesQuery({id:query.get('login_id'),calls}):{data:services()};
    }else if(url.startsWith('/api/services/cancel/')){
      assert.equal(init.method,'POST');assert.equal(init.headers['Content-Type'],'application/json');
      const body=JSON.parse(init.body),operation=url.split('/').at(-1);posts.push({operation,body});
      response=await options.cancel({operation,body,posts});
    }else throw new Error('Unexpected API '+url);
    return {ok:(response.status||200)===200,status:response.status||200,headers:{get:()=>response.contentType||'application/json'},json:async()=>structuredClone(response.data)};
  };
  vm.runInNewContext(fs.readFileSync(new URL('../static/bills.js',import.meta.url),'utf8'),{
    document,
    window:{addEventListener:(name,handler)=>{events[name]=handler;},dispatchEvent:event=>{dispatched.push(event);}},
    CustomEvent:class{constructor(type,options={}){this.type=type;this.detail=options.detail;}},fetch,Intl,Date,URLSearchParams,
    setInterval(callback){if(!options.cancel)throw new Error('账单页面不得启动自动官方查询');timers.set(1,callback);return 1;},clearInterval(id){timers.delete(id);},
    setTimeout(callback,delay){if(!options.cancel)throw new Error('账单页面不得排程自动重试');timeouts.set(1,{callback,delay});return 1;},clearTimeout(id){timeouts.delete(id);},
  });
  await flush();return {get,calls,posts,timers,timeouts,events,dispatched,control,document};
}
const queries=env=>env.calls.filter(url=>url.startsWith('/api/bills/query?')).map(url=>Object.fromEntries(new URLSearchParams(url.split('?')[1])));
const usageQueries=env=>env.calls.filter(url=>url.startsWith('/api/usage/query?')).map(url=>Object.fromEntries(new URLSearchParams(url.split('?')[1])));
const servicesQueries=env=>env.calls.filter(url=>url.startsWith('/api/services/query?')).map(url=>Object.fromEntries(new URLSearchParams(url.split('?')[1])));

test('opening queries only the selected account, preserves official values and allows reselecting it',async()=>{
  const env=await fixture();
  assert.deepEqual(queries(env),[{login_id:'first',month:'202611'}]);
  assert.deepEqual(usageQueries(env),[{login_id:'first'}]);
  assert.deepEqual(servicesQueries(env),[{login_id:'first'}]);
  assert.equal(env.get('bills-total').textContent,'555.40');
  assert.equal(env.get('bills-payable').textContent,'545.87');
  assert.equal(env.get('bills-balance').textContent,'17044.64');
  assert.equal(env.get('bills-period').textContent,'2026年11月01日 — 2026年11月30日');
  assert.match(env.get('bills-sync').textContent,/2026.*11.*06.*10:15:30/);
  assert.equal(env.get('bills-accounts').children[0].attributes['aria-pressed'],'true');
  assert.equal(env.events.focus,undefined);
  await env.get('bills-accounts').children[0].click();
  assert.equal(queries(env).length,2);
  await env.get('bills-refresh').click();
  assert.equal(queries(env).length,3);
  assert.equal(usageQueries(env).length,3);
  assert.equal(servicesQueries(env).length,3);
  assert.equal(env.calls.some(url=>url.startsWith('/api/logins')),false);
});

test('month navigation uses the official month list and does not manufacture missing months',async()=>{
  const env=await fixture({months:['202611','202609'],default_month:'202611'});
  assert.deepEqual(env.get('bills-month').children.map(option=>option.value),['202611','202609']);
  assert.equal(env.get('bills-next').disabled,true);
  await env.get('bills-previous').click();
  assert.deepEqual(queries(env).at(-1),{login_id:'first',month:'202609'});
  assert.equal(usageQueries(env).length,1);
  assert.equal(servicesQueries(env).length,1);
  assert.equal(env.get('bills-previous').disabled,true);
  assert.equal(env.get('bills-month').value,'202609');
  await env.get('bills-next').click();
  assert.deepEqual(queries(env).at(-1),{login_id:'first',month:'202611'});
  env.get('bills-month').value='202609';await env.get('bills-month').handlers.change();
  assert.deepEqual(queries(env).at(-1),{login_id:'first',month:'202609'});
  assert.equal(usageQueries(env).length,1);assert.equal(servicesQueries(env).length,1);
});

test('missing amounts remain unknown and an official zero remains zero',async()=>{
  const env=await fixture({query:async()=>({data:bill({total:null,payable:null,original:'0.00',discount:0,rebate:null,balance:undefined,items:[
    {name:'未提供金额',original:null,discount:0,amount:null,children:[]},
    {name:'仅提供实付',original:null,discount:null,amount:'10.00',children:[]},
  ]})})});
  for(const field of ['total','payable','rebate','balance'])assert.equal(env.get('bills-'+field).textContent,'—');
  assert.equal(env.get('bills-original').textContent,'0.00');
  assert.equal(env.get('bills-discount').textContent,'0');
  assert.equal(env.get('bills-breakdown-payable').textContent,'—');
  assert.equal(env.get('bills-breakdown').attributes['data-balanced'],'false');
  const rows=withClass(env.get('bills-items'),'bills-row-total');
  assert.deepEqual(rows[0].children.slice(1).map(cell=>cell.textContent),['—','0','—']);
  assert.deepEqual(rows[1].children.slice(1).map(cell=>cell.textContent),['—','—','10.00']);
});

test('the bill overview shows every official amount and only displays a matching equation',async()=>{
  const env=await fixture({query:async({month})=>({data:bill({month,
    payable:month==='202611'?'468.67':'468.68',total:'478.20',original:'1009.80',discount:'531.60',rebate:'9.53',
    sections:[{name:'代付与返费明细',items:[{name:'活动返费',amount:'-9.53',children:[]}]}],
    notes:['账单以实际出账为准。'],
  })})});
  assert.equal(env.get('bills-breakdown-payable').textContent,'468.67');
  assert.equal(env.get('bills-breakdown').attributes['data-balanced'],'true');
  assert.equal(env.get('bills-rebate-label').textContent,'活动返费');
  assert.equal(env.get('bills-reading-guide').hidden,false);
  assert.equal(env.get('bills-notes').hidden,false);
  await env.get('bills-previous').click();
  assert.equal(env.get('bills-breakdown-payable').textContent,'468.68');
  assert.equal(env.get('bills-breakdown').attributes['data-balanced'],'false');
});

test('bill details open by default and can collapse to category totals without querying again',async()=>{
  const env=await fixture();
  assert.equal(withClass(env.get('bills-items'),'bills-row-total').length,2);
  assert.equal(withClass(env.get('bills-items'),'bills-table').length,1);
  assert.match(text(env.get('bills-items')),/套餐月费/);
  const toggle=withClass(env.get('bills-items'),'bills-item-toggle')[0];
  assert.equal(toggle.attributes['aria-expanded'],'true');await toggle.click();
  assert.doesNotMatch(text(env.get('bills-items')),/主套餐|套餐月费/);
  assert.equal(withClass(env.get('bills-items'),'bills-row-detail').length,0);
  await env.get('bills-expand').click();assert.match(text(env.get('bills-items')),/套餐月费/);
  await env.get('bills-collapse').click();assert.doesNotMatch(text(env.get('bills-items')),/主套餐|套餐月费/);
  assert.equal(withClass(env.get('bills-items'),'bills-row-total').length,2);
  assert.equal(env.get('bills-total').textContent,'555.40');
  assert.equal(queries(env).length,1);
});

test('switching accounts clears old amounts immediately and ignores an older response',async()=>{
  const first=deferred(),second=deferred();
  const env=await fixture({query:({id})=>id==='first'?first.promise:second.promise});
  const switchRequest=env.get('bills-accounts').children[1].click();
  assert.equal(env.get('bills-total').textContent,'—');
  assert.match(text(env.get('bills-items')),/正在查询/);
  assert.match(env.get('bills-current-account').textContent,/139\*\*\*\*0001/);
  second.resolve({data:bill({total:'2.00',account:account('second')})});await switchRequest;
  first.resolve({data:bill({total:'999.00',account:account('first',{status:'login_required'})})});await flush();
  assert.equal(env.get('bills-total').textContent,'2.00');
  assert.equal(env.get('bills-accounts').children[1].attributes['aria-pressed'],'true');
  assert.match(text(env.get('bills-accounts').children[0]),/已登录/);
});

test('a login notification arriving during a refresh is coalesced and continues after the request',async()=>{
  const pending=deferred();let count=0;
  const env=await fixture({query:async()=>++count===1?pending.promise:{data:bill({total:'20.00'})}});
  env.control.accounts=[account('first',{credential_revision:2})];
  env.events['accounts-changed']();env.events['accounts-changed']();env.events['accounts-changed']();
  assert.equal(queries(env).length,1);
  pending.resolve({status:401,data:{code:'APP_LOGIN_REQUIRED',error:'原凭据已失效',account:account('first',{status:'login_required'})}});
  await flush();
  assert.equal(queries(env).length,2);
  assert.equal(usageQueries(env).length,2);
  assert.equal(servicesQueries(env).length,2);
  assert.equal(env.calls.filter(url=>url==='/api/bills/accounts').length,2);
  assert.equal(env.get('bills-total').textContent,'20.00');
  assert.equal(env.get('bills-feedback').hidden,true);
  assert.equal(env.dispatched.length,0);
});

test('an expired shared status still reaches the backend and recovery opens only on user click',async()=>{
  const env=await fixture({accounts:[account('first',{status:'login_required'})],query:async()=>({status:401,data:{error:'请恢复账号登录后查询',code:'APP_LOGIN_REQUIRED'}})});
  assert.equal(queries(env).length,1);
  assert.equal(env.get('bills-relogin').hidden,false);
  assert.match(env.get('bills-feedback-message').textContent,/请恢复账号登录/);
  assert.equal(env.dispatched.length,0);
  await env.get('bills-relogin').click();
  assert.equal(env.dispatched[0].type,'open-account-manager');
  assert.equal(env.dispatched[0].detail.loginId,'first');
  assert.equal(env.calls.length,4);
});

test('a failed query clears prior values and has no automatic retry',async()=>{
  const env=await fixture();
  env.control.query=async()=>({status:429,data:{error:'请求频繁，请稍后再试',code:'RATE_LIMITED'}});
  await env.get('bills-refresh').click();
  assert.equal(env.get('bills-total').textContent,'—');
  assert.equal(env.get('bills-payable').textContent,'—');
  assert.match(env.get('bills-feedback-message').textContent,/请求频繁/);
  assert.match(text(env.get('bills-items')),/本次查询未完成/);
  assert.equal(queries(env).length,2);await flush();assert.equal(queries(env).length,2);
});

test('a local service HTML error produces an actionable message',async()=>{
  const env=await fixture({query:async()=>({status:500,contentType:'text/html',data:null})});
  assert.match(env.get('bills-feedback-message').textContent,/本机服务.*HTTP 500/);
  assert.doesNotMatch(env.get('bills-feedback-message').textContent,/Unexpected token|doctype/);
  assert.equal(env.get('bills-refresh').disabled,false);
});

test('empty accounts or an empty official month list never triggers a bill request',async()=>{
  const noAccounts=await fixture({accounts:[]});
  assert.equal(queries(noAccounts).length,0);assert.match(text(noAccounts.get('bills-items')),/账号池为空/);
  assert.equal(usageQueries(noAccounts).length,0);
  assert.equal(servicesQueries(noAccounts).length,0);
  const noMonths=await fixture({months:[]});
  assert.equal(queries(noMonths).length,0);assert.equal(noMonths.get('bills-month').disabled,true);
  assert.equal(usageQueries(noMonths).length,1);
  assert.equal(servicesQueries(noMonths).length,1);
  assert.match(text(noMonths.get('bills-items')),/没有可查询/);
});

test('extra official summaries, signed section details and notes render without more queries',async()=>{
  const env=await fixture({query:async()=>({data:bill({
    summary:[{name:'合账消费',amount:'555.40'},{name:'本月节省',amount:'0.00'},{name:'未提供金额',amount:null}],
    sections:[{name:'返费 / 代付明细',items:[{name:'返费抵扣',original:null,discount:null,amount:'-9.53',children:[{name:'活动返费',amount:'-9.53',children:[]}]}]}],
    notes:['优惠减免以实际生效时间为准。','账户余额为查询时的余额。'],
  })})});
  assert.equal(env.get('bills-extra-summary').hidden,false);
  assert.match(text(env.get('bills-extra-summary')),/合账消费.*555\.40.*本月节省.*0\.00/);
  assert.doesNotMatch(text(env.get('bills-extra-summary')),/未提供金额/);
  assert.match(text(env.get('bills-items')),/返费 \/ 代付明细.*返费抵扣.*-9\.53/);
  assert.doesNotMatch(text(env.get('bills-items')),/活动返费/);
  assert.equal(env.get('bills-rebate').textContent,'9.53');
  assert.equal(env.get('bills-notes').hidden,false);
  assert.match(text(env.get('bills-notes-list')),/优惠减免以实际生效时间为准/);
  await env.get('bills-collapse').click();assert.doesNotMatch(text(env.get('bills-items')),/活动返费/);
  await env.get('bills-expand').click();assert.match(text(env.get('bills-items')),/活动返费/);
  assert.equal(queries(env).length,1);
});

test('a confirmed duplicate rebate is one signed row even when expanding all bill details',async()=>{
  const env=await fixture({query:async()=>({data:bill({items:[],sections:[{name:'代付与返费明细',items:[
    {name:'活动返费',amount:'-20.10',original:null,discount:null,compact_details:true,children:[{name:'返费项目 2',amount:'20.10',children:[]}]},
  ]}]})})});
  const items=env.get('bills-items');
  assert.equal(items.children.length,1);assert.match(text(items),/活动返费.*-20\.10/);
  assert.doesNotMatch(text(items),/返费项目 2|代付与返费明细|优惠前|减免/);
  assert.equal(withClass(items,'bills-table').length,0);
  assert.equal(withClass(items,'bills-deduction')[0].textContent,'-20.10');
  assert.equal(env.get('bills-expand').disabled,true);
  await env.get('bills-expand').click();assert.equal(withClass(items,'bills-table').length,0);
  await withClass(items,'bills-card-heading')[0].click();
  assert.equal(env.get('bills-rebate-dialog').open,true);
  assert.equal(env.get('bills-rebate-empty').hidden,false);
  assert.equal(queries(env).length,1);
});

test('the collapsed bill retains the official totals, dated rebate and savings details',async()=>{
  const env=await fixture({query:async({month})=>({data:bill({month,summary:[{name:'本月为您节省',amount:'551.70'}],sections:[{name:'代付与返费明细',items:[
    {name:'活动返费',amount:'-20.10',children:[{name:'返费项目 2',amount:'20.10',children:[
      {name:'50元话费券',date:'2025-08-08',amount:'19.59'},
      {name:'40元话费券',date:'2025-08-04',amount:'0.51'},
    ]}]},
  ]},{name:'节省明细',items:[{name:'套餐优惠',amount:'551.70',children:[]}]}]})})});
  const items=env.get('bills-items');
  await env.get('bills-collapse').click();
  assert.match(text(items),/合计.*活动返费.*-20\.10.*实际应付.*545\.87.*本月节省.*551\.70/);
  assert.equal(withClass(items,'bills-savings')[0].open,false);
  assert.equal(env.get('bills-extra-summary').hidden,true);
  const trigger=withClass(items,'bills-rebate-trigger')[0];
  assert.equal(trigger.attributes['aria-haspopup'],'dialog');
  await trigger.click();
  assert.equal(env.get('bills-rebate-dialog').open,true);
  assert.equal(env.get('bills-rebate-detail-total').textContent,'20.10 元');
  assert.match(text(env.get('bills-rebate-details')),/50元话费券.*2025-08-08.*19\.59.*40元话费券.*0\.51/);
  assert.equal(queries(env).length,1);
  await env.get('bills-previous').click();
  assert.equal(env.get('bills-rebate-dialog').open,false);
});

test('month cards stay in sync with the selector, including older months, without background requests',async()=>{
  const months=['202611','202610','202609','202608','202607','202606','202605','202604','202603','202602','202601','202512','202511'];
  const env=await fixture({months});
  assert.equal(env.get('bills-months').children.length,7);
  assert.equal(env.get('bills-months').children[0].attributes['aria-pressed'],'true');
  await env.get('bills-months').children[1].click();
  assert.equal(env.get('bills-month').value,'202610');
  assert.deepEqual(queries(env).at(-1),{login_id:'first',month:'202610'});
  env.get('bills-month').value='202511';await env.get('bills-month').handlers.change();
  const selected=env.get('bills-months').children.find(item=>item.attributes['aria-pressed']==='true');
  assert.match(text(selected),/11月.*2025/);
  assert.equal(queries(env).length,3);
});

test('an empty month uses the official message and clears optional data when changing months',async()=>{
  const env=await fixture({query:async({month})=>({data:month==='202611'?bill({summary:[{name:'合账应付',amount:'545.87'}],sections:[{name:'节省明细',items:[{name:'套餐优惠',amount:'-531.60',children:[]}]}],notes:['本月账单说明']}):bill({month,total:null,payable:null,items:[],message:'该月账单尚未出账，请选择其他月份。'})})});
  await env.get('bills-previous').click();
  assert.match(text(env.get('bills-items')),/该月账单尚未出账，请选择其他月份/);
  assert.doesNotMatch(text(env.get('bills-items')),/节省明细/);
  assert.equal(env.get('bills-extra-summary').hidden,true);
  assert.equal(env.get('bills-notes').hidden,true);
  assert.equal(env.get('bills-total').textContent,'—');
  assert.equal(queries(env).length,2);
});

test('bill and usage requests start together and each result renders without waiting for the other',async()=>{
  const billsPending=deferred(),usagePending=deferred();
  const env=await fixture({query:()=>billsPending.promise,usageQuery:()=>usagePending.promise});
  assert.equal(queries(env).length,1);assert.equal(usageQueries(env).length,1);
  assert.equal(env.get('bills-content').attributes['aria-busy'],'true');
  assert.equal(env.get('usage-content').attributes['aria-busy'],'true');
  usagePending.resolve({data:usage()});await flush();
  assert.match(text(env.get('usage-overview')),/通用流量.*47\.50.*GB/);
  assert.equal(env.get('usage-content').attributes['aria-busy'],'false');
  assert.equal(env.get('bills-content').attributes['aria-busy'],'true');
  assert.equal(env.get('bills-total').textContent,'—');
  billsPending.resolve({data:bill()});await flush();
  assert.equal(env.get('bills-total').textContent,'555.40');

  const slowUsage=deferred();env.control.usageQuery=()=>slowUsage.promise;
  const refresh=env.get('bills-refresh').click();await flush();
  assert.equal(env.get('bills-total').textContent,'555.40');
  assert.equal(env.get('bills-content').attributes['aria-busy'],'false');
  assert.equal(env.get('usage-content').attributes['aria-busy'],'true');
  slowUsage.resolve({data:usage()});await refresh;
});

test('refresh keeps all three panels in place until their own current results arrive',async()=>{
  const env=await fixture();
  const panels=['bills-items','usage-overview','services-groups'];
  const previous=panels.map(id=>env.get(id).children[0]);
  const pendingBill=deferred(),pendingUsage=deferred(),pendingServices=deferred();
  env.control.query=()=>pendingBill.promise;
  env.control.usageQuery=()=>pendingUsage.promise;
  env.control.servicesQuery=()=>pendingServices.promise;
  const refresh=env.get('bills-refresh').click();await flush();
  assert.equal(env.get('bills-refresh').attributes['aria-busy'],'true');
  panels.forEach((id,index)=>assert.equal(env.get(id).children[0],previous[index]));
  assert.equal(env.get('bills-total').textContent,'555.40');
  assert.equal(env.get('services-groups').inert,true);
  pendingUsage.resolve({data:usage({overview:[{key:'flow',name:'新余量',remaining:'18',unit:'GB'}]})});
  await flush();
  assert.match(text(env.get('usage-overview')),/新余量.*18/);
  assert.equal(env.get('bills-items').children[0],previous[0]);
  assert.equal(env.get('services-groups').children[0],previous[2]);
  pendingBill.resolve({data:bill({total:'25.00'})});
  pendingServices.resolve({data:services()});await refresh;
  assert.equal(env.get('bills-total').textContent,'25.00');
  assert.equal(env.get('services-groups').inert,false);
  assert.equal(env.get('bills-refresh').attributes['aria-busy'],'false');
});

test('switching away from a loaded account shows placeholders without the previous account data',async()=>{
  const env=await fixture();
  const pending=deferred();
  env.control.query=()=>pending.promise;
  env.control.usageQuery=()=>pending.promise;
  env.control.servicesQuery=()=>pending.promise;
  const switching=env.get('bills-accounts').children[1].click();
  assert.equal(env.get('bills-total').textContent,'—');
  for(const panel of ['bills-items','services-groups']){
    assert.equal(withClass(env.get(panel),'information-skeleton').length,3);
  }
  const rows=withClass(env.get('usage-overview'),'usage-card-row');
  assert.deepEqual(rows.map(row=>withClass(row,'usage-card-placeholder').length),[3,2]);
  assert.ok(rows.every(row=>row.attributes['aria-hidden']==='true'));
  assert.doesNotMatch(text(env.get('services-groups')),/5G畅享套餐/);
  pending.resolve({status:429,data:{code:'RATE_LIMITED',error:'查询太频繁'}});
  await switching;
  assert.equal(env.get('bills-refresh').attributes['aria-busy'],'false');
  assert.equal(withClass(env.get('usage-overview'),'usage-card-placeholder').length,0);
});

test('the usage overview shows all measured resources and their official ratios together',async()=>{
  const env=await fixture({usageQuery:async()=>({data:usage({overview:[
    {key:'flow',name:'定向流量',unit:'GB',total:'75',used:'21.32',remaining:'53.68',used_percent:'28.42',remaining_percent:'71.57'},
    {key:'flow',name:'国内通用流量',scope:'国内通用流量',unit:'MB',total:'10240',used:'10240',remaining:'0',used_percent:'100.00',remaining_percent:'0.00',display:{total:{value:'10',unit:'GB'},used:{value:'10',unit:'GB'},remaining:{value:'0',unit:'MB'}}},
    {key:'voice',name:'国内通话',scope:'仅限国内主叫',unit:'分钟',total:'100',used:'0',remaining:'100',used_percent:'0.00',remaining_percent:'100.00'},
    {key:'sms',name:'短信',unit:'条',total:null,used:null,remaining:null,remaining_percent:null},
  ]})})});
  const overview=env.get('usage-overview'),meters=withClass(overview,'usage-meter');
  let cards=withClass(overview,'usage-overview-card');
  assert.deepEqual(withClass(overview,'usage-overview-title').map(title=>title.textContent),['国内通用流量','定向流量','国内通话','短信']);
  assert.deepEqual(overview.children.map(row=>row.children.map(card=>card.attributes['data-usage-kind'])),[['flow','flow'],['voice','sms']]);
  assert.equal(meters.length,3);
  assert.equal(meters[0].attributes['aria-valuenow'],'0.00');
  assert.equal(withClass(meters[0],'usage-meter-pointer')[0].style.transform,'rotate(180.000deg)');
  assert.match(text(cards[0]),/国内通用流量.*已用.*10.*GB.*已用尽/);
  assert.doesNotMatch(text(cards[0]),/适用范围|适用说明|5G套餐流量/);
  assert.equal(meters[1].attributes['aria-valuenow'],'71.57');
  assert.equal(meters[1].attributes['aria-valuetext'],'剩余约 71%');
  assert.match(withClass(meters[1],'usage-meter-ring')[0].style.background,/51\.156deg/);
  assert.equal(withClass(meters[1],'usage-meter-pointer')[0].style.transform,'rotate(51.156deg)');
  assert.match(text(cards[1]),/定向流量.*剩余约.*71%.*使用中/);
  const toggle=withClass(cards[0],'usage-group-heading')[0];
  await toggle.click();
  cards=withClass(overview,'usage-overview-card');
  assert.match(text(cards[0]),/收起详细信息.*5G套餐流量.*结转流量/);
  assert.doesNotMatch(text(cards[1]),/5G套餐流量/);
  assert.match(text(cards[2]),/国内通话.*剩余\s+100%.*适用范围：仅限国内主叫/);
  assert.equal(withClass(cards[2],'usage-meter-pointer')[0].style.transform,'rotate(0.000deg)');
  assert.match(text(cards[3]),/短信.*可用余量.*—/);
  assert.equal(withClass(cards[3],'usage-meter').length,0);
  assert.equal(usageQueries(env).length,1);
});

test('usage values preserve units, zeros, unknowns, unlimited data and separate scopes',async()=>{
  const env=await fixture({usageQuery:async()=>({data:usage({overview:[
    {key:'flow',name:'通用流量',unit:'MB',total:'1024.50',used:0,remaining:'1024.50',scope:'国内'},
    {key:'flow',name:'定向流量',unit:'GB',total:null,used:'3.25',remaining:null,unlimited:true,scope:'指定视频'},
    {key:'sms',name:'短信',unit:'条',total:null,used:0,remaining:null},
  ]})})});
  const overview=text(env.get('usage-overview'));
  assert.match(overview,/通用流量.*1024\.50.*MB.*已用.*0.*MB.*总量.*1024\.50.*MB.*国内/);
  assert.match(overview,/定向流量.*不限量.*已用.*3\.25.*GB.*总量.*不限量.*指定视频/);
  assert.doesNotMatch(overview,/元/);
  assert.equal(withClass(env.get('usage-overview'),'usage-overview-card').length,3);
  assert.doesNotMatch(text(env.get('usage-groups')),/5G套餐流量|结转流量/);
  const group=env.get('usage-groups').children[0].children[0];
  assert.equal(group.attributes['aria-expanded'],'false');await group.click();
  const details=text(env.get('usage-groups'));
  assert.match(details,/5G套餐流量.*已用.*12\.50.*GB.*剩余.*47\.50.*GB.*总量.*60\.00.*GB/);
  assert.match(details,/适用范围：国内（不含港澳台）.*有效期：2026-11-01 至 2026-11-30.*当月有效.*结转流量/);
  assert.match(text(env.get('usage-overview')),/短信.*可用余量.*—.*已用.*0.*条.*总量.*—/);
  assert.match(text(env.get('usage-notes-list')),/套餐余量以实际使用情况为准/);
  assert.equal(usageQueries(env).length,1);assert.equal(queries(env).length,1);
});

test('a speed-limited flow card shows used data without an allowance or gauge',async()=>{
  const measured={unit:'GB',used:'12',used_only:true};
  const env=await fixture({usageQuery:async()=>({data:usage({overview:[
    {key:'flow',name:'国内通用流量',...measured},
    {key:'flow',name:'定向流量',unit:'GB',total:'10',used:'0',remaining:'10',remaining_percent:'100'},
  ],groups:[{key:'flow',name:'国内通用流量',items:[{name:'达量降速流量包',...measured}]}]})})});
  const overview=env.get('usage-overview');
  let [limited,finite]=withClass(overview,'usage-overview-card');
  assert.match(text(limited),/已用流量.*12.*GB/);
  assert.doesNotMatch(text(limited),/可用余量|总量/);
  assert.equal(withClass(limited,'usage-meter').length,0);
  assert.match(text(finite),/可用余量.*10.*GB/);
  await withClass(limited,'usage-group-heading')[0].click();
  [limited]=withClass(overview,'usage-overview-card');
  assert.match(text(limited),/达量降速流量包.*已用.*12.*GB/);
  assert.doesNotMatch(text(limited),/可用余量|总量/);
});

test('a single measured resource uses one usage card without empty wrapper counters or repeated metadata',async()=>{
  const metadata={scope:'定向流量 / 不可共享',start_at:'2026-11-01 00:00:00',end_at:'2026-12-01 00:00:00',description:'移动云盘定向'};
  const resource={name:'0元和彩云定向流量（包月）',unit:'MB',total:'30720.00',used:'0.00',remaining:'30720.00',remaining_percent:'100.00',...metadata,
    display:{total:{value:'30',unit:'GB'},used:{value:'0',unit:'MB'},remaining:{value:'30',unit:'GB'}},children:[]};
  const env=await fixture({usageQuery:async()=>({data:usage({groups:[{key:'flow',name:'定向流量',items:[{name:'30GB移动云盘定向流量免费服务',total:null,used:null,remaining:null,...metadata,children:[resource]}]}]})})});
  await env.get('usage-groups').children[0].children[0].click();
  const group=env.get('usage-groups'),content=text(group);
  assert.equal(withClass(group,'usage-item').length,1);
  assert.equal(withClass(group,'usage-item-counters').length,1);
  assert.doesNotMatch(text(withClass(group,'usage-item-counters')[0]),/—/);
  assert.match(content,/30GB移动云盘.*已用.*0.*MB.*剩余.*30.*GB.*总量.*30.*GB/);
  assert.equal((content.match(/适用范围：定向流量 \/ 不可共享/g)||[]).length,1);
  assert.equal((content.match(/有效期：2026-11-01 00:00:00 至 2026-12-01 00:00:00/g)||[]).length,1);
  assert.equal((content.match(/移动云盘定向/g)||[]).length,2,'套餐标题及一份说明保留');
  assert.match(content,/资源名称：0元和彩云定向流量/);
  assert.equal(withClass(group,'usage-description')[0].tag,'details');
  assert.equal(withClass(group,'usage-description')[0].attributes.open,undefined);
  const original=withClass(group,'usage-original')[0];
  assert.equal(original.tag,'details');assert.match(text(original),/30720\.00.*MB.*0\.00.*MB.*30720\.00.*MB/);
  const track=withClass(group,'usage-progress')[0];
  assert.equal(track.attributes['aria-valuenow'],'100.00');
  assert.equal(track.children[0].style.width,'100.00%');
  assert.equal(usageQueries(env).length,1);
});

test('multiple resources retain separate quantities and their distinct metadata without a fake package aggregate',async()=>{
  const env=await fixture({usageQuery:async()=>({data:usage({groups:[{key:'flow',name:'通用流量',items:[{name:'组合套餐',total:null,used:null,remaining:null,scope:'国内',children:[
    {name:'本月流量',unit:'GB',total:'5',used:'1',remaining:'4',scope:'国内',end_at:'2026-12-01'},
    {name:'结转流量',unit:'MB',total:'1024',used:'0',remaining:'1024',scope:'上月结转',end_at:'2026-11-30'},
  ]}]}]})})});
  const overview=env.get('usage-overview');
  await withClass(overview,'usage-group-heading')[0].click();
  const group=withClass(overview,'usage-group')[0];
  assert.equal(withClass(group,'usage-item-counters').length,2);
  assert.match(text(group),/本月流量.*4.*GB.*上月结转.*2026-11-30/);
  assert.equal((text(group).match(/适用范围：国内/g)||[]).length,1);
  assert.equal(withClass(group,'usage-progress').length,0,'未返回比例时不由前端猜算');
});

test('other resources are hidden while the current plan and separate usage details remain available',async()=>{
  const env=await fixture({usageQuery:async()=>({data:usage({groups:[
    {name:'其他资源',items:[{name:'示例附加权益',used:'0',unit:'分钟'}]},
    {name:'套餐信息',items:[{name:'黔心选套餐',children:[{name:'套餐通话',unit:'分钟',total:'100'}]}]},
    {name:'国际及港澳台用量',items:[{name:'境外流量包',unit:'MB',used:'3'}]},
    {key:'voice',name:'套餐外通话',items:[{name:'套餐外主叫',unit:'分钟',used:'2'}]},
  ]})})});
  const plan=env.get('usage-plan'),groups=env.get('usage-groups'),popup=env.get('usage-plan-popover');
  assert.equal(plan.textContent,'黔心选套餐');
  assert.equal(groups.hidden,false);
  assert.match(text(groups),/国际及港澳台用量.*套餐外通话/);
  assert.doesNotMatch(text(groups),/其他资源|套餐信息|黔心选套餐/);
  assert.equal(popup.hidden,true);
  const trigger=env.get('usage-plan-control');
  trigger.handlers.pointerenter({pointerType:'mouse'});
  assert.equal(popup.hidden,false);
  assert.match(text(popup),/黔心选套餐.*套餐通话.*总量.*100.*分钟/);
  assert.doesNotMatch(text(popup),/已用|剩余|查看原始用量/);
  assert.equal(plan.attributes['aria-expanded'],'true');
  trigger.handlers.pointerleave({pointerType:'mouse'});
  assert.equal(popup.hidden,true);
  assert.equal(withClass(env.get('usage-overview'),'usage-overview-card').length,3);
  assert.equal(usageQueries(env).length,1);
});

test('plan details present each allowance in a compact total row and keep plan descriptions',async()=>{
  const env=await fixture({usageQuery:async()=>({data:usage({groups:[{name:'套餐信息',items:[{
    name:'家庭套餐',scope:'套餐说明',description:'套餐附带有效说明',children:[
      {name:'通话',unit:'分钟',total:'100',used:'20',remaining:'80',scope:'套餐包含（非实时余量）'},
      {name:'流量',unit:'MB',total:'5120',display:{total:{value:'5',unit:'GB'}},scope:'套餐包含（非实时余量）'},
      {name:'短信',unit:'条',unlimited:true},
      {name:'下月套餐',scope:'预约套餐',description:'下月生效'},
    ],
  }]}]})})});
  env.get('usage-plan').click();
  const group=env.get('usage-plan-popover');
  assert.deepEqual(withClass(group,'usage-plan-total').map(item=>text(item).trim().replace(/\s+/g,' ')),['总量 100 分钟','总量 5 GB','总量 不限量']);
  assert.match(text(group),/套餐附带有效说明.*下月套餐.*下月生效/);
  assert.doesNotMatch(text(group),/已用|剩余|查看原始用量|套餐包含/);
});

test('touch and keyboard can open and dismiss plan details, and switching clears the previous account',async()=>{
  const pending=deferred();
  const env=await fixture({usageQuery:({id})=>id==='second'?pending.promise:Promise.resolve({data:usage({groups:[
    {name:'套餐信息',items:[{name:'主号套餐',description:'主号专用说明'}]},
  ]})})});
  const trigger=env.get('usage-plan'),control=env.get('usage-plan-control'),panel=env.get('usage-plan-popover');
  control.handlers.pointerenter({pointerType:'touch'});
  assert.equal(panel.hidden,true);
  trigger.click();
  env.events.pointerdown({target:panel.children[0]});
  assert.equal(panel.hidden,false);
  env.events.pointerdown({target:env.get('usage-overview')});
  assert.equal(panel.hidden,true);
  trigger.click();
  env.document.activeElement=panel.children[0];
  env.events.keydown({key:'Escape'});
  assert.equal(panel.hidden,true);
  assert.equal(trigger.focused,true);
  trigger.click();
  const switched=env.get('bills-accounts').children[1].click();
  assert.equal(panel.hidden,true);
  assert.equal(control.hidden,true);
  assert.doesNotMatch(text(panel),/主号套餐/);
  pending.resolve({data:usage({groups:[{name:'套餐信息',items:[{name:'副号套餐'}]}]})});
  await switched;
  assert.equal(trigger.textContent,'副号套餐');
  assert.equal(panel.hidden,true);
  assert.equal(usageQueries(env).length,2);
});

test('package grid lifts identical scope and validity once while preserving per-package descriptions',async()=>{
  const common={scope:'指定应用 / 不可共享',start_at:'2026-11-01',end_at:'2026-12-01'};
  const items=[{name:'云盘权益',total:'30',used:'0',remaining:'30',description:'适用于云盘'},
    {name:'视频权益',total:'10',used:'2',remaining:'8',description:'适用于视频'}].map(item=>({...common,...item,unit:'GB',children:[]}));
  const env=await fixture({usageQuery:async()=>({data:usage({groups:[{key:'flow',name:'定向流量',items}]})})});
  await env.get('usage-groups').children[0].children[0].click();
  const group=env.get('usage-groups');
  assert.equal(withClass(group,'usage-group-context').length,1);
  assert.equal((text(group).match(/适用范围：指定应用 \/ 不可共享/g)||[]).length,1);
  assert.equal((text(group).match(/有效期：2026-11-01 至 2026-12-01/g)||[]).length,1);
  const cards=withClass(group,'usage-item');assert.equal(cards.length,2);
  assert.match(text(cards[0]),/云盘权益.*30.*适用于云盘/);
  assert.match(text(cards[1]),/视频权益.*2.*8.*10.*适用于视频/);
  assert.doesNotMatch(text(cards[0]),/适用范围|有效期/);
  assert.equal(usageQueries(env).length,1);
});

test('package grid does not merge different validity ranges or lose nested resource constraints',async()=>{
  const env=await fixture({usageQuery:async()=>({data:usage({groups:[{key:'flow',name:'流量',items:[
    {name:'当月包',scope:'国内',start_at:'2026-11-01',end_at:'2026-11-30',total:'5',used:'1',remaining:'4',unit:'GB',children:[]},
    {name:'结转包',scope:'结转专用',start_at:'2026-11-01',end_at:'2026-11-15',total:'2',used:'0',remaining:'2',unit:'GB',children:[]},
  ]}]})})});
  await env.get('usage-groups').children[0].children[0].click();
  const group=env.get('usage-groups');assert.equal(withClass(group,'usage-group-context').length,0);
  const cards=withClass(group,'usage-item');
  assert.match(text(cards[0]),/适用范围：国内.*2026-11-30/);
  assert.match(text(cards[1]),/适用范围：结转专用.*2026-11-15/);
  assert.equal(usageQueries(env).length,1);
});

test('switching accounts isolates both results even when old usage returns last',async()=>{
  const firstBill=deferred(),firstUsage=deferred(),secondBill=deferred(),secondUsage=deferred();
  const env=await fixture({query:({id})=>id==='first'?firstBill.promise:secondBill.promise,usageQuery:({id})=>id==='first'?firstUsage.promise:secondUsage.promise});
  const switched=env.get('bills-accounts').children[1].click();
  assert.match(text(env.get('usage-overview')),/正在查询/);
  assert.match(env.get('usage-current-account').textContent,/139\*\*\*\*0001/);
  firstBill.resolve({data:bill({total:'999.00'})});firstUsage.resolve({data:usage({overview:[{key:'flow',name:'旧号码流量',unit:'GB',remaining:'999'}],account:account('first',{status:'login_required'})})});
  await flush();assert.doesNotMatch(text(env.get('usage-overview')),/旧号码流量|999/);
  secondUsage.resolve({data:usage({overview:[{key:'flow',name:'副号流量',unit:'GB',total:'20',used:'1',remaining:'19'}]})});await flush();
  assert.match(text(env.get('usage-overview')),/副号流量.*19/);assert.equal(env.get('bills-total').textContent,'—');
  secondBill.resolve({data:bill({total:'2.00'})});await switched;
  assert.equal(env.get('bills-total').textContent,'2.00');
  assert.match(text(env.get('bills-accounts').children[0]),/已登录/);
});

test('one query may fail while the other stays usable, with independent recovery and no SMS',async()=>{
  const env=await fixture({usageQuery:async()=>({status:401,data:{error:'套餐余量登录已失效，请恢复登录',code:'USAGE_LOGIN_REQUIRED'}})});
  assert.equal(env.get('bills-total').textContent,'555.40');
  assert.equal(env.get('bills-feedback').hidden,true);
  assert.equal(env.get('usage-relogin').hidden,false);
  assert.match(env.get('usage-feedback-message').textContent,/套餐余量登录已失效/);
  assert.equal(env.dispatched.length,0);
  await env.get('usage-relogin').click();assert.equal(env.dispatched[0].type,'open-account-manager');
  assert.equal(env.dispatched[0].detail.loginId,'first');
  env.control.usageQuery=async()=>({data:usage()});
  env.control.query=async()=>({status:429,data:{error:'账单请求频繁',code:'RATE_LIMITED'}});
  await env.get('bills-refresh').click();
  assert.match(text(env.get('usage-overview')),/通用流量.*47\.50/);
  assert.equal(env.get('usage-feedback').hidden,true);
  assert.match(env.get('bills-feedback-message').textContent,/账单请求频繁/);
  assert.equal(env.get('bills-total').textContent,'—');
  assert.equal(env.calls.some(url=>/sms|send|login\//.test(url)),false);
  assert.equal(queries(env).length,2);assert.equal(usageQueries(env).length,2);
});

test('changing a month leaves pending usage intact and an older bill cannot replace the selected month',async()=>{
  const oldBill=deferred(),usagePending=deferred();
  const env=await fixture({query:({month})=>month==='202611'?oldBill.promise:Promise.resolve({data:bill({month,total:'20.00'})}),usageQuery:()=>usagePending.promise});
  await env.get('bills-previous').click();
  assert.equal(env.get('bills-month').value,'202610');
  assert.equal(env.get('bills-total').textContent,'20.00');
  assert.equal(usageQueries(env).length,1);
  assert.equal(env.get('usage-content').attributes['aria-busy'],'true');
  oldBill.resolve({data:bill({total:'999.00'})});usagePending.resolve({data:usage()});await flush();
  assert.equal(env.get('bills-total').textContent,'20.00');
  assert.match(text(env.get('usage-overview')),/通用流量.*47\.50/);
});

test('all three queries start together and subscribed services can finish before bills and usage',async()=>{
  const pendingBill=deferred(),pendingUsage=deferred(),pendingServices=deferred();
  const env=await fixture({query:()=>pendingBill.promise,usageQuery:()=>pendingUsage.promise,servicesQuery:()=>pendingServices.promise});
  assert.deepEqual(queries(env),[{login_id:'first',month:'202611'}]);
  assert.deepEqual(usageQueries(env),[{login_id:'first'}]);assert.deepEqual(servicesQueries(env),[{login_id:'first'}]);
  for(const section of ['bills','usage','services'])assert.equal(env.get(section+'-content').attributes['aria-busy'],'true');
  pendingServices.resolve({data:services()});await flush();
  assert.match(text(env.get('services-tabs')),/我的套餐.*增值业务.*基础功能.*活动/);
  assert.match(text(env.get('services-groups')),/5G畅享套餐/);
  assert.equal(env.get('services-content').attributes['aria-busy'],'false');
  assert.equal(env.get('usage-content').attributes['aria-busy'],'true');
  assert.equal(env.get('bills-content').attributes['aria-busy'],'true');
  pendingBill.resolve({data:bill()});pendingUsage.resolve({data:usage()});await flush();
  assert.equal(env.get('bills-total').textContent,'555.40');assert.match(text(env.get('usage-overview')),/47\.50/);

  const slowServices=deferred();env.control.servicesQuery=()=>slowServices.promise;
  const refresh=env.get('bills-refresh').click();await flush();
  assert.equal(env.get('services-content').attributes['aria-busy'],'true');
  assert.equal(env.get('bills-content').attributes['aria-busy'],'false');assert.equal(env.get('usage-content').attributes['aria-busy'],'false');
  assert.equal(env.get('bills-total').textContent,'555.40');assert.match(text(env.get('usage-overview')),/47\.50/);
  slowServices.resolve({data:services()});await refresh;
});

test('subscribed business cards show dates and keep full details expandable',async()=>{
  const env=await fixture({servicesQuery:async()=>({data:services({groups:[{name:'手机 · 我的套餐',items:[{
    name:'5G畅享套餐',fee:'首月免费，次月128元/月',status:'已生效',fields:[{name:'订购时间',value:'20261031235955'},{name:'生效时间',value:'2026-11-01 00:00:00'},{name:'失效时间',value:'2026-11-30 23:59:59'},{name:'业务说明',value:'国内语音与流量\n不含港澳台'},{name:'剩余次数',value:0},{name:'未知字段',value:null}],children:[{name:'来电显示',fee:'免费',status:'使用中',fields:[{name:'业务类型',value:'套餐附属业务'}],children:[{name:'附加说明',fee:null,status:null,fields:[{name:'说明',value:'长期有效'}],children:[]}]}],
  }]}]})})});
  assert.equal(env.get('services-tabs').children[0].attributes['aria-selected'],'true');
  const list=withClass(env.get('services-groups'),'services-card-list')[0];
  assert.equal(list.attributes['aria-label'],'我的套餐');
  const main=withClass(list,'services-row')[0],detail=withClass(list,'services-detail-row')[0],toggle=withClass(main,'services-name-label')[0];
  assert.match(text(main),/5G畅享套餐.*订购时间.*2026-10-31 23:59.*有效期限.*2026-11-01 00:00 起 至 2026-11-30 23:59/);
  assert.doesNotMatch(text(main),/128元|已生效/);
  assert.equal(detail.hidden,true);assert.equal(toggle.attributes['aria-expanded'],'false');
  assert.doesNotMatch(text(list),/国内语音|来电显示|附加说明/,'完整详情默认不铺开');
  await toggle.click();assert.equal(detail.hidden,false);assert.equal(toggle.attributes['aria-expanded'],'true');
  let content=text(env.get('services-groups'));
  assert.match(content,/资费.*首月免费，次月128元\/月.*状态.*已生效/);
  assert.match(content,/生效时间.*2026-11-01 00:00:00.*失效时间.*2026-11-30 23:59:59/);
  assert.match(content,/业务说明.*国内语音与流量\n不含港澳台.*剩余次数.*0.*未知字段.*—/);
  assert.match(content,/来电显示/);assert.doesNotMatch(content,/免费.*使用中/,'关联业务的资费状态也默认收起');
  const childToggle=withClass(detail,'services-name-label')[0];await childToggle.click();
  const grandchildToggle=withClass(detail,'services-name-label')[1];await grandchildToggle.click();
  content=text(env.get('services-groups'));
  assert.match(content,/业务类型.*套餐附属业务.*附加说明.*—.*—.*说明.*长期有效/);
  assert.doesNotMatch(content,/免费\s*元|128元\/月\s*元/);
  assert.match(text(env.get('services-notes-list')),/已订业务资费以实际生效规则为准/);
  assert.match(env.get('services-sync').textContent,/2026.*11.*06.*10:15:30/);
  await env.get('services-tabs').children[1].click();assert.doesNotMatch(text(env.get('services-groups')),/5G畅享套餐/);
  await env.get('services-tabs').children[0].click();assert.match(text(env.get('services-groups')),/5G畅享套餐.*来电显示.*附加说明/s);
  await withClass(env.get('services-groups'),'services-name-label')[0].click();assert.equal(withClass(env.get('services-groups'),'services-detail-row')[0].hidden,true);
  await env.get('bills-accounts').children[1].click();assert.equal(withClass(env.get('services-groups'),'services-detail-row')[0].hidden,true,'切换账号后详情重新收起');
  assert.doesNotMatch(text(env.get('services-groups')),/国内语音|来电显示|附加说明/);
  assert.equal(servicesQueries(env).length,2);assert.equal(queries(env).length,2);assert.equal(usageQueries(env).length,2);
});

test('late subscribed business responses cannot cross accounts or update the old account status',async()=>{
  const first=deferred(),second=deferred();
  const env=await fixture({servicesQuery:({id})=>id==='first'?first.promise:second.promise});
  const switched=env.get('bills-accounts').children[1].click();
  assert.match(env.get('services-current-account').textContent,/139\*\*\*\*0001/);assert.match(text(env.get('services-groups')),/正在查询/);
  second.resolve({data:services({groups:[{name:'副号业务',items:[]}],account:account('second')})});await switched;
  first.resolve({data:services({groups:[{name:'旧主号业务',items:[]}],account:account('first',{status:'login_required'})})});await flush();
  assert.match(text(env.get('services-groups')),/副号业务/);assert.doesNotMatch(text(env.get('services-groups')),/旧主号业务/);
  assert.match(text(env.get('bills-accounts').children[0]),/已登录/);
  assert.equal(env.get('bills-accounts').children[1].attributes['aria-pressed'],'true');
});

test('a third-query failure has its own recovery and never removes successful bills or usage',async()=>{
  const env=await fixture({servicesQuery:async()=>({status:401,data:{error:'已订业务会话失效，请恢复登录',code:'SERVICES_LOGIN_REQUIRED'}})});
  assert.equal(servicesQueries(env).length,1);assert.equal(env.get('services-relogin').hidden,false);
  assert.match(env.get('services-feedback-message').textContent,/已订业务会话失效/);
  assert.equal(env.get('bills-total').textContent,'555.40');assert.match(text(env.get('usage-overview')),/47\.50/);
  assert.equal(env.get('bills-feedback').hidden,true);assert.equal(env.get('usage-feedback').hidden,true);assert.equal(env.dispatched.length,0);
  await env.get('services-relogin').click();assert.equal(env.dispatched[0].type,'open-account-manager');assert.equal(env.dispatched[0].detail.loginId,'first');
  assert.equal(env.calls.some(url=>/sms|send|login\//.test(url)),false);
  env.control.servicesQuery=async()=>({data:services()});env.control.query=async()=>({status:429,data:{error:'账单请求频繁',code:'RATE_LIMITED'}});
  env.control.usageQuery=async()=>({status:502,data:{error:'余量暂时无法获取',code:'USAGE_NETWORK'}});
  await env.get('bills-refresh').click();assert.equal(env.get('services-feedback').hidden,true);assert.match(text(env.get('services-groups')),/5G畅享套餐/);
  assert.match(env.get('bills-feedback-message').textContent,/账单请求频繁/);assert.match(env.get('usage-feedback-message').textContent,/余量暂时无法获取/);
  assert.equal(servicesQueries(env).length,2);await flush();assert.equal(servicesQueries(env).length,2);
});

test('login notifications during a slow third query merge and refresh all three after it finishes',async()=>{
  const pending=deferred();let count=0;
  const env=await fixture({servicesQuery:async()=>++count===1?pending.promise:{data:services({groups:[{name:'登录后业务',items:[]}]})}});
  assert.equal(env.get('bills-total').textContent,'555.40');assert.equal(env.get('services-content').attributes['aria-busy'],'true');
  env.control.accounts=[account('first',{credential_revision:2})];
  env.events['accounts-changed']();env.events['accounts-changed']();env.events['accounts-changed']();
  assert.equal(servicesQueries(env).length,1);assert.equal(queries(env).length,1);
  pending.resolve({status:401,data:{error:'旧会话已失效',code:'SERVICES_LOGIN_REQUIRED',account:account('first',{status:'login_required'})}});await flush();
  assert.equal(env.calls.filter(url=>url==='/api/bills/accounts').length,2);
  assert.equal(queries(env).length,2);assert.equal(usageQueries(env).length,2);assert.equal(servicesQueries(env).length,2);
  assert.match(text(env.get('services-groups')),/登录后业务/);assert.equal(env.get('services-feedback').hidden,true);assert.equal(env.dispatched.length,0);
});

test('a shared account requiring login still queries all three and changing month leaves services untouched',async()=>{
  const env=await fixture({accounts:[account('first',{status:'login_required'})]});
  assert.equal(queries(env).length,1);assert.equal(usageQueries(env).length,1);assert.equal(servicesQueries(env).length,1);
  assert.match(text(env.get('services-groups')),/5G畅享套餐/);
  await env.get('bills-previous').click();assert.match(text(env.get('services-groups')),/5G畅享套餐/);
  assert.equal(servicesQueries(env).length,1);assert.equal(usageQueries(env).length,1);assert.equal(queries(env).length,2);
});

test('four service category tabs switch local content and preserve linked lines without the phone prefix',async()=>{
  const product=name=>({name,fee:'0元',status:'已订',fields:[],children:[]});
  const env=await fixture({servicesQuery:async()=>({data:services({groups:[
    {name:'手机 · 我的套餐',items:[product('套餐A')]},
    {name:'手机 · 增值业务',items:[product('手机钱包')]},
    {name:'手机 · 基础功能',items:[]},
    {name:'手机 · 活动',items:[product('活动优惠')]},
    {name:'固话 · 我的套餐',items:[product('关联固话套餐')]},
  ]})})});
  const tabs=()=>env.get('services-tabs').children;
  assert.deepEqual(tabs().map(tab=>text(tab.children[0])),['我的套餐','增值业务','基础功能','活动']);
  assert.doesNotMatch(text(env.get('services-tabs')),/手机/);
  assert.equal(withClass(tabs()[0],'services-tab-count')[0].textContent,'2');
  assert.equal(withClass(tabs()[0],'services-tab-count')[0].attributes['aria-label'],'2 项业务');
  assert.match(text(env.get('services-groups')),/套餐A.*固话 · 我的套餐.*关联固话套餐/);
  await tabs()[1].click();
  assert.equal(tabs()[1].attributes['aria-selected'],'true');assert.equal(tabs()[0].attributes['aria-selected'],'false');
  assert.equal(env.get('services-groups').attributes['aria-labelledby'],'services-tab-1');
  assert.match(text(env.get('services-groups')),/手机钱包/,'业务本身的名称保留');
  assert.doesNotMatch(text(env.get('services-groups')),/套餐A|关联固话套餐/);
  await tabs()[2].click();assert.match(text(env.get('services-groups')),/基础功能暂无已订业务/);
  await tabs()[3].click();assert.match(text(env.get('services-groups')),/活动优惠/);
  await env.get('bills-previous').click();assert.equal(tabs()[3].attributes['aria-selected'],'true');
  assert.equal(servicesQueries(env).length,1);assert.equal(usageQueries(env).length,1);
});

test('service tabs support keyboard navigation and keep the selected category after a refresh',async()=>{
  const env=await fixture();let prevented=0;
  env.get('services-tabs').children[0].handlers.keydown({key:'ArrowRight',preventDefault(){prevented++;}});
  assert.equal(env.get('services-tabs').children[1].attributes['aria-selected'],'true');
  assert.equal(env.get('services-tabs').children[1].focused,true);assert.match(text(env.get('services-groups')),/视频会员/);
  await env.get('bills-refresh').click();assert.equal(env.get('services-tabs').children[1].attributes['aria-selected'],'true');
  env.get('services-tabs').children[1].handlers.keydown({key:'End',preventDefault(){prevented++;}});
  assert.equal(env.get('services-tabs').children[3].attributes['aria-selected'],'true');
  env.get('services-tabs').children[3].handlers.keydown({key:'Home',preventDefault(){prevented++;}});
  assert.equal(env.get('services-tabs').children[0].attributes['aria-selected'],'true');
  assert.equal(prevented,3);assert.equal(servicesQueries(env).length,2);
});

const cancelableServices=()=>services({groups:[{name:'增值业务',items:[{name:'视频会员',fee:'10元/月',status:'已订',fields:[{name:'生效时间',value:'20261101000000'}],children:[],cancel:{available:true,action_id:'server-action'}},{name:'合约业务',fee:null,status:null,fields:[],children:[],cancel:{available:false,reason:'需在官方 APP 核对合约及违约金。'}}]}]});
const cancellationPlan={cancel_id:'bound-session',name:'视频会员',phone_masked:'138****0000',effective_options:[{value:'0',label:'下月失效'},{value:'1',label:'立即失效'}],warnings:['此业务已停售，退订后无法再次订购。'],bound_services:['附属会员'],related_services:['视频优惠'],description:'套餐优惠将随业务退订失效。',sms_required:true};

test('service search finds names, descriptions and nested business names locally and locates matching categories',async()=>{
  const product=(name,available,fields=[],children=[])=>({name,fee:null,status:null,fields,children,cancel:{available}});
  const env=await fixture({servicesQuery:async()=>({data:services({groups:[
    {name:'手机 · 我的套餐',items:[product('主套餐',false)]},
    {name:'手机 · 增值业务',items:[product('支付宝立减金（NSB03）',true),product('会员包',false,[{name:'业务说明',value:'包含视频会员权益'}])]},
    {name:'手机 · 活动',items:[product('权益组合',true,[],[product('专享阅读包',false)])]},
  ]})})});
  env.get('services-search').value=' nsb03 ';env.get('services-search').handlers.input();
  assert.equal(env.get('services-tabs').children[1].attributes['aria-selected'],'true');
  assert.match(text(env.get('services-groups')),/支付宝立减金/);assert.doesNotMatch(text(env.get('services-groups')),/会员包|主套餐/);
  assert.equal(env.get('services-filter-count').textContent,'匹配 1 项 · 当前 1 项');
  await env.get('services-tabs').children[0].click();assert.match(text(env.get('services-groups')),/没有匹配/);
  env.get('services-search').value='视频';env.get('services-search').handlers.input();assert.match(text(env.get('services-groups')),/会员包/);
  env.get('services-cancel-only').checked=true;env.get('services-cancel-only').handlers.change();assert.match(text(env.get('services-groups')),/没有匹配/);
  env.get('services-search').value='阅读';env.get('services-search').handlers.input();
  assert.equal(env.get('services-tabs').children[3].attributes['aria-selected'],'true');assert.match(text(env.get('services-groups')),/权益组合/);
  env.get('services-search').value='';env.get('services-search').handlers.input();
  assert.match(env.get('services-filter-count').textContent,/匹配 2 项/);
  env.get('services-cancel-only').checked=false;env.get('services-cancel-only').handlers.change();
  await env.get('services-tabs').children[1].click();assert.match(text(env.get('services-groups')),/支付宝立减金.*会员包/);
  assert.equal(servicesQueries(env).length,1);assert.equal(queries(env).length,1);assert.equal(usageQueries(env).length,1);assert.equal(env.posts.length,0);
});

test('filtering retains each business detail identity and uses the matching unsubscribe action',async()=>{
  const env=await fixture({servicesQuery:async()=>({data:services({groups:[{name:'增值业务',items:[
    {name:'会员A',fields:[{name:'业务说明',value:'A的说明'}],cancel:{available:true,action_id:'action-a'}},
    {name:'会员B',fields:[{name:'业务说明',value:'B的说明'}],cancel:{available:true,action_id:'action-b'}},
  ]}]})}),cancel:async({body})=>{assert.equal(body.action_id,'action-b');return {data:cancellationPlan};}});
  await withClass(env.get('services-groups'),'services-name-label')[0].click();
  env.get('services-search').value='会员B';env.get('services-search').handlers.input();
  assert.equal(withClass(env.get('services-groups'),'services-detail-row')[0].hidden,true,'过滤后不能套用另一业务的展开状态');
  await withClass(env.get('services-groups'),'services-name-label')[0].click();assert.match(text(env.get('services-groups')),/B的说明/);
  await withClass(env.get('services-groups'),'services-cancel-action')[0].click();
  assert.deepEqual(env.posts,[{operation:'prepare',body:{login_id:'first',action_id:'action-b'}}]);
  await env.get('services-cancel-dismiss').click();
  env.get('services-search').value='';env.get('services-search').handlers.input();
  assert.equal(withClass(env.get('services-groups'),'services-detail-row')[0].hidden,false);
  assert.equal(withClass(env.get('services-groups'),'services-detail-row')[1].hidden,false);
  assert.equal(servicesQueries(env).length,1);
});

test('confirmed unsubscribed services come first without mixing details, pending states or cancel actions',async()=>{
  const product=(name,status,extra={})=>({name,status,fields:[],children:[],...extra});
  const env=await fixture({servicesQuery:async()=>({data:services({groups:[{name:'增值业务',items:[
    product('正常会员','已订',{cancel:{available:true,action_id:'active-member'}}),
    product('下月失效会员','已退订',{fields:[{name:'失效时间',value:'20261201000000'},{name:'业务说明',value:'下月失效说明'}]}),
    product('已到期会员','已失效'),product('另一已退订会员','已退订'),product('受理中会员','撤销中'),
  ]}]})}),cancel:async({body})=>{assert.equal(body.action_id,'active-member');return {data:cancellationPlan};}});
  const root=env.get('services-groups'),rows=()=>withClass(root,'services-row');
  assert.deepEqual(rows().map(row=>text(withClass(row,'services-name-label')[0]).trim()),[
    '下月失效会员 ? 已退订','另一已退订会员 ? 已退订','正常会员 ?','已到期会员 ?','受理中会员 ?',
  ]);
  assert.equal(rows().filter(row=>row.className.includes('services-cancelled')).length,2);
  assert.match(text(rows()[0]),/失效时间.*2026-12-01 00:00/);
  await withClass(rows()[0],'services-name-label')[0].click();
  assert.match(text(withClass(root,'services-detail-row')[0]),/下月失效说明/);
  env.get('services-search').value='下月';env.get('services-search').handlers.input();
  assert.equal(withClass(root,'services-detail-row')[0].hidden,false);
  env.get('services-search').value='';env.get('services-search').handlers.input();
  await withClass(rows()[2],'services-cancel-action')[0].click();
  assert.equal(env.posts.length,1);assert.equal(servicesQueries(env).length,1);
});

test('subscription date sorting uses the returned times and retains the correct open card',async()=>{
  const item=(name,fields)=>({name,fields,children:[]});
  const env=await fixture({servicesQuery:async()=>({data:services({groups:[{name:'增值业务',items:[
    item('无订购时间',[]),
    item('旧订购', [{name:'订购时间',value:'20180513124400'},{name:'业务说明',value:'旧业务说明'}]),
    item('新订购', [{name:'订购时间',value:'2024-05-24 23:11'},{name:'失效时间',value:'长期有效'}]),
  ]}]})})});
  const root=env.get('services-groups'),sort=env.get('services-sort');
  await withClass(root,'services-name-label')[1].click();
  sort.value='newest';sort.handlers.change();
  let cards=withClass(root,'services-card');
  assert.match(text(cards[0]),/新订购.*2024-05-24 23:11.*失效时间.*长期有效/);
  assert.match(text(cards[1]),/旧订购.*旧业务说明/);
  assert.equal(withClass(cards[1],'services-detail-row')[0].hidden,false);
  assert.match(text(cards[2]),/无订购时间/);
  sort.value='oldest';sort.handlers.change();
  cards=withClass(root,'services-card');
  assert.match(text(cards[0]),/旧订购/);
  assert.match(text(cards[2]),/无订购时间/);
  assert.equal(servicesQueries(env).length,1);
});

test('unsubscribe opens official impact confirmation without sending SMS, preserving the selected account',async()=>{
  const env=await fixture({servicesQuery:async()=>({data:cancelableServices()}),cancel:async({operation})=>{
    assert.equal(operation,'prepare');return {data:cancellationPlan};
  }});
  assert.equal(withClass(env.get('services-groups'),'services-cancel-action').length,1);
  const row=withClass(env.get('services-groups'),'services-row')[0];assert.doesNotMatch(text(row),/10元\/月|已订/);assert.match(text(row),/生效时间.*2026-11-01/);
  await withClass(row,'services-cancel-action')[0].click();
  assert.equal(env.get('services-cancel-dialog').open,true);
  assert.deepEqual(env.posts,[{operation:'prepare',body:{login_id:'first',action_id:'server-action'}}]);
  assert.match(text(env.get('services-cancel-warnings')),/停售/);assert.match(text(env.get('services-cancel-related')),/附属会员.*视频优惠/);
  assert.equal(env.get('services-cancel-message').hidden,true);assert.equal(env.get('services-cancel-effective').textContent,'失效时间：下月失效');
  assert.equal(env.get('services-cancel-submit').disabled,true);assert.equal(env.get('services-cancel-sms').disabled,false);
  await env.get('services-cancel-dismiss').click();assert.equal(env.get('services-cancel-dialog').open,false);assert.equal(env.posts.length,1);
});

test('unsubscribe uses the official default effect, submits once and refreshes only services after success',async()=>{
  const done=deferred();
  const env=await fixture({servicesQuery:async()=>({data:cancelableServices()}),cancel:async({operation})=>operation==='prepare'?{data:cancellationPlan}:operation==='sms'?{data:{message:'验证码已发送',retry_after:60}}:done.promise});
  await withClass(env.get('services-groups'),'services-cancel-action')[0].click();
  await env.get('services-cancel-sms').click();assert.equal(env.posts.length,2);assert.equal(env.timers.size,1);
  env.get('services-cancel-code').value='654321';env.get('services-cancel-code').handlers.input();assert.equal(env.get('services-cancel-submit').disabled,false);
  const pending=env.get('services-cancel-submit').click();await env.get('services-cancel-submit').click();
  assert.equal(env.posts.filter(row=>row.operation==='confirm').length,1);assert.equal(env.get('services-cancel-dismiss').disabled,true);
  assert.deepEqual(env.posts.at(-1).body,{login_id:'first',cancel_id:'bound-session',effective_type:'0',sms_code:'654321',confirmed:true});
  done.resolve({data:{status:'success',message:'退订成功'}});await pending;
  assert.equal(env.get('services-cancel-submit').hidden,true);assert.match(env.get('services-cancel-message').textContent,/退订成功/);
  assert.equal(queries(env).length,1);assert.equal(usageQueries(env).length,1);assert.equal(servicesQueries(env).length,2);
  assert.equal(env.timeouts.get(1).delay,3000);assert.equal(env.get('services-cancel-dialog').open,true);
  env.timeouts.get(1).callback();assert.equal(env.get('services-cancel-dialog').open,false);
  assert.equal(env.timers.size,0);assert.equal(env.timeouts.size,0);
});

test('uncertain unsubscribe result stops further submission and does not automatically query or retry',async()=>{
  const env=await fixture({servicesQuery:async()=>({data:cancelableServices()}),cancel:async({operation})=>({data:operation==='prepare'?cancellationPlan:operation==='sms'?{retry_after:60}:{status:'unknown',message:'结果待核实，不会重复提交。'}})});
  await withClass(env.get('services-groups'),'services-cancel-action')[0].click();await env.get('services-cancel-sms').click();
  env.get('services-cancel-code').value='123456';env.get('services-cancel-code').handlers.input();
  await env.get('services-cancel-submit').click();assert.equal(env.get('services-cancel-submit').hidden,true);
  for(let i=0;i<61;i++)env.timers.get(1)?.();
  assert.equal(env.posts.length,3);assert.equal(servicesQueries(env).length,1);assert.equal(env.get('services-cancel-sms').disabled,true);
  assert.match(env.get('services-cancel-message').textContent,/待核实/);
  assert.equal(env.timeouts.size,0,'结果待核实时不能按成功自动关闭');
});

test('successful unsubscribe closes after three seconds even while its read-only refresh is pending',async()=>{
  const refresh=deferred();let count=0;
  const env=await fixture({servicesQuery:async()=>++count===1?{data:cancelableServices()}:refresh.promise,cancel:async({operation})=>({data:operation==='prepare'?cancellationPlan:operation==='sms'?{retry_after:60}:{status:'success',message:'退订成功'}})});
  await withClass(env.get('services-groups'),'services-cancel-action')[0].click();await env.get('services-cancel-sms').click();
  env.get('services-cancel-code').value='123456';env.get('services-cancel-code').handlers.input();
  await env.get('services-cancel-submit').click();
  assert.equal(env.get('services-content').attributes['aria-busy'],'true');assert.equal(env.timeouts.get(1).delay,3000);
  env.timeouts.get(1).callback();assert.equal(env.get('services-cancel-dialog').open,false);
  refresh.resolve({data:cancelableServices()});await flush();
  assert.equal(env.get('services-cancel-dialog').open,false);assert.equal(env.posts.length,3);
});

test('closing a successful unsubscribe early cancels its auto-close before opening another dialog',async()=>{
  const env=await fixture({servicesQuery:async()=>({data:cancelableServices()}),cancel:async({operation})=>({data:operation==='prepare'?cancellationPlan:operation==='sms'?{retry_after:60}:{status:'success',message:'退订成功'}})});
  await withClass(env.get('services-groups'),'services-cancel-action')[0].click();await env.get('services-cancel-sms').click();
  env.get('services-cancel-code').value='123456';env.get('services-cancel-code').handlers.input();await env.get('services-cancel-submit').click();await flush();
  await env.get('services-cancel-dismiss').click();assert.equal(env.timeouts.size,0);
  await withClass(env.get('services-groups'),'services-cancel-action')[0].click();
  assert.equal(env.get('services-cancel-dialog').open,true);assert.equal(env.timeouts.size,0);
});
