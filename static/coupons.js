(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const categories = {fee:'话费券',flow:'流量券',other:'其他卡券'};
  const accountStates = {active:'已登录',login_required:'需重新登录',restoring:'正在恢复',retry:'等待重连'};
  const couponStates = {available:'可使用',used:'已使用',expired:'已过期',unavailable:'不可使用',invalid:'已失效','官方状态 9':'已锁定／占用'};
  const useNames = {exchange:'兑换话费',recharge:'充值使用',rights:'兑换流量',flow:'兑换流量'};
  const redemptionKinds = new Set(['exchange','rights','flow']);
  const formatter = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  let accounts = [], loginId = '', state = 'available', availableSort = 'default', items = [], page = 0, total = null, hasMore = false, loading = false, refreshing = false, queryFailed = false, requestVersion = 0;
  let useOperation = null, useBusy = false, useConfirming = false, useSubmitted = false, useGeneration = 0;
  let captchaToken = '', captchaRequired = false;
  let pendingOrders = [], paymentOrder = '';
  let useItem = null, relogin = null, useRecheck = null;
  const loginRequiredCodes = ['ALIPAY_RELOGIN_REQUIRED','APP_LOGIN_REQUIRED','APP_SESSION_EXPIRED','COUPON_LOGIN_REQUIRED','500003','531022'];
  let exchangeRiskToken = '', riskSdk = null;
  let successToastTimer = null;
  const reminderQueries = new Map();
  let reminderCounts = {};
  let reminderGeneration = 0;
  let refreshPending = false;
  let pendingExpiryTimer = null;
  let noticesBusy=false,noticesPending=false,noticesTimer=null;

  function node(tag,className='',text){const item=document.createElement(tag);item.className=className;if(text!==undefined)item.textContent=text;return item;}
  function dateText(value){if(!value)return '—';const date=new Date(value);return Number.isNaN(date.getTime())?value:formatter.format(date);}
  function amountText(item){return item.amount?`${item.amount}${item.unit||''}`:'—';}
  function statusText(item){return couponStates[item.status]||item.status||(state==='available'?'可使用':'历史记录');}
  function applyAccount(result,accountId){
    if(result.account?.id!==accountId)return;
    const current=accounts.find(account=>account.id===accountId);
    if(current&&((result.account.credential_revision||0)<(current.credential_revision||0)||
      result.account.credential_revision===current.credential_revision&&(result.account.checked_at||0)<(current.checked_at||0)))return false;
    accounts=accounts.map(account=>account.id===accountId?result.account:account);renderAccounts();
    return true;
  }
  async function api(url,payload){
    const accountId=payload?.login_id||loginId;
    const response=await fetch(url,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(!(response.headers.get('Content-Type')||'').includes('application/json')){
      const error=new Error(`本机服务返回异常页面（HTTP ${response.status}），请重启或更新应用后重试。`);
      error.status=response.status;error.code='LOCAL_API_RESPONSE_INVALID';throw error;
    }
    const result=await response.json();
    const accountApplied=applyAccount(result,accountId);
    if(accountApplied!==false&&accountId&&loginRequiredCodes.includes(result.code)&&!$('coupons-use').open){
      window.dispatchEvent(new CustomEvent('open-account-manager',{detail:{loginId:accountId,message:'当前账号需要恢复登录，请在高亮行点击“重新登录”，完成后返回重试。'}}));
    }
    if(!response.ok||loginRequiredCodes.includes(result.code)){const error=new Error(result.error||result.message||'操作未完成，请稍后重试。');error.status=response.status;error.code=result.code;throw error;}
    return result;
  }
  function feedback(message){$('coupons-feedback').textContent=message;$('coupons-feedback').hidden=!message;}
  function emptyRow(message){const row=node('tr'),cell=node('td','coupons-empty',message);cell.colSpan=5;row.append(cell);return row;}

  async function loadNotices(){
    if(!queryFailed)return;
    if(noticesBusy){noticesPending=true;return;}
    noticesBusy=true;clearTimeout(noticesTimer);let delay=300000;
    try{
      const response=await fetch('/api/coupons/notices');
      if(!response.ok)throw new Error('官方公告暂未同步，稍后自动更新。');
      const result=await response.json();
      $('coupons-notices-list').replaceChildren(...result.notices.map(notice=>{
        const detail=node('details','coupons-notice');
        detail.open=result.notices.length===1;
        detail.append(node('summary','',notice.title),node('p','coupons-notice-content',notice.content));
        if(notice.published_at)detail.append(node('small','','发布于 '+dateText(notice.published_at)));
        if(notice.end_at)delay=Math.min(delay,Math.max(1000,Date.parse(notice.end_at)-Date.now()));
        return detail;
      }));
      $('coupons-notices-sync').textContent=result.checked_at?(result.stale?'缓存公告 · 上次同步 ':'同步于 ')+dateText(result.checked_at*1000):'尚未同步';
      $('coupons-notices-message').textContent=result.message||'';$('coupons-notices-message').hidden=!result.message;
      $('coupons-notices').hidden=!queryFailed||(!result.notices.length&&!result.message);
    }catch(error){
      $('coupons-notices').hidden=!queryFailed;$('coupons-notices-message').textContent=error.message;$('coupons-notices-message').hidden=false;
    }finally{
      noticesBusy=false;
      if(!queryFailed){noticesPending=false;}
      else if(noticesPending){noticesPending=false;loadNotices();}
      else noticesTimer=setTimeout(()=>{if(!document.hidden)loadNotices();else noticesTimer=setTimeout(loadNotices,300000);},delay);
    }
  }

  function setPendingOrders(orders){
    clearTimeout(pendingExpiryTimer);pendingExpiryTimer=null;
    const now=Date.now();
    pendingOrders=orders.map(order=>({...order,_deadline:order._deadline!==undefined?order._deadline:typeof order.expires_in==='number'?now+order.expires_in*1000:null}));
    const deadlines=pendingOrders.map(order=>order._deadline).filter(deadline=>deadline!==null);
    if(!deadlines.length)return;
    const accountId=loginId,version=requestVersion,delay=Math.max(0,Math.ceil(Math.min(...deadlines)-now));
    pendingExpiryTimer=setTimeout(async()=>{
      if(accountId!==loginId||version!==requestVersion)return;
      const expired=new Set(pendingOrders.filter(order=>order._deadline!==null&&order._deadline<=Date.now()).map(order=>order.order_id));
      setPendingOrders(pendingOrders.filter(order=>!expired.has(order.order_id)));
      if(expired.has(paymentOrder)){closeUse(true);feedback('该订单付款窗口已结束，已移出待支付列表。');}
      render();
      try{
        const result=await api('/api/coupons/pending?'+new URLSearchParams({login_id:accountId}));
        if(accountId===loginId&&version===requestVersion){setPendingOrders(result.pending_orders);render();}
      }catch{
        // Local expiry already applied. A later normal refresh will sync storage;
        // a network failure must not start a payment/query retry loop.
      }
    },delay);
  }

  function renderAccounts(){
    const selected=accounts.find(account=>account.id===loginId);
    $('coupons-account-count').textContent=accounts.length;
    $('coupons-account-hint').textContent=accounts.length?'与其他功能共用登录':'请从左侧“账号管理”添加账号';
    $('coupons-current-account').textContent=selected?`${selected.name||selected.phone_masked} · ${selected.phone_masked}`:'尚未选择账号';
    $('coupons-accounts').replaceChildren(...accounts.map(account=>{
      const button=node('button','coupons-account'+(account.id===loginId?' selected':''));button.type='button';
      button.setAttribute('aria-pressed',String(account.id===loginId));
      const label=node('span','coupons-account-label'),name=node('strong','',account.name||account.phone_masked),status=node('span','coupons-account-state '+(account.status==='active'?'active':'attention'),accountStates[account.status]||'暂不可用');
      const reminder=reminderCounts[account.id],amount=reminder?.count||0;
      const bubble=node('span','coupons-account-reminder',String(amount));bubble.hidden=amount===0;
      bubble.title=`${amount} 张可用优惠话费券：话费兑换券、最优充值折扣低于9.1折的券`+(reminder?.unpriced?`；另有 ${reminder.unpriced} 张充值券规则待核对`:'');
      bubble.setAttribute('aria-label',bubble.title);
      label.append(name,bubble,status);button.append(label,node('small','',account.phone_masked));
      button.addEventListener('click',()=>{if(account.id===loginId)return loadCards();closeUse(true);loginId=account.id;renderAccounts();return loadCards();});return button;
    }));
  }

  function updateReminder(accountId, reminder){
    if(!reminder)return;
    reminderCounts[accountId]=reminder;
    renderAccounts();
    window.dispatchEvent(new CustomEvent('coupon-reminders-changed'));
  }

  async function refreshOtherReminders(force=false){
    const generation=++reminderGeneration,known=new Set(accounts.map(account=>account.id));
    for(const id of reminderQueries.keys())if(!known.has(id))reminderQueries.delete(id);
    const queue=accounts.filter(account=>account.status==='active'&&!(state==='available'&&account.id===loginId)&&
      (force||!reminderQueries.has(account.id)||reminderQueries.get(account.id).loading||reminderQueries.get(account.id).revision!==account.credential_revision));
    await Promise.all(Array.from({length:Math.min(3,queue.length)},async()=>{
      while(generation===reminderGeneration&&queue.length){
        const account=queue.shift(),record={revision:account.credential_revision,loading:true};
        reminderQueries.set(account.id,record);
        try{
          const response=await fetch('/api/coupons?'+new URLSearchParams({login_id:account.id,state:'available',page:'1',sort:'default'}));
          const result=await response.json();
          if(generation!==reminderGeneration||reminderQueries.get(account.id)!==record)continue;
          if(applyAccount(result,account.id)!==false&&response.ok)updateReminder(account.id,result.reminder);
        }catch{ /* Keep the last confirmed reminder. */ }
        finally{record.loading=false;}
      }
    }));
  }

  function render(){
    const selected=accounts.find(account=>account.id===loginId);
    $('coupons-refresh').disabled=loading||refreshing;
    $('coupons-more').disabled=loading;
    $('coupons-more').textContent=loading?'正在加载…':'加载更多';
    $('coupons-more').hidden=!hasMore;
    $('coupons-sort').disabled=state==='history';
    $('coupons-order-note').hidden=state!=='history';
    for(const button of document.querySelectorAll('[data-coupons-state]')){
      const active=button.dataset.couponsState===state;button.classList.toggle('selected',active);button.setAttribute('aria-pressed',String(active));
    }
    const search=$('coupons-search').value.trim().toLocaleLowerCase(),category=$('coupons-category').value;
    const shown=items.filter(item=>(category==='all'||item.category===category)&&(!search||item.name.toLocaleLowerCase().includes(search)));
    const filtered=search||category!=='all';
    $('coupons-result-count').textContent=items.length?`${filtered?`当前已加载中筛选出 ${shown.length} 张 · `:''}已加载 ${items.length} 张${total===null?'':` / 共 ${total} 张`}`:loading?'正在查询…':queryFailed?'查询未完成':total===0?'共 0 张':'选择账号后查看卡券';
    const groupedRows={fee:[],flow:[],other:[]};
    const now=Date.now();
    for(const item of shown){
      const row=node('tr'),name=node('td'),heading=node('div','coupons-name-line'),title=node('strong','coupons-name',item.name);
      heading.append(title);
      if(state==='available'){
        const remaining=Date.parse(item.expires_at)-now;
        const usable=item.status==='available'&&!(remaining<=0);
        const worthCharging=usable&&item.worth_recharging;
        if(usable&&item.category==='fee'&&item.use_kind==='exchange'){
          const tag=node('span','coupons-tag coupons-tag-redeem','值得兑');tag.title='无需充值，可直接兑换话费';heading.append(tag);
        }else if(worthCharging&&item.recharge_discount){const tag=node('span','coupons-tag coupons-tag-worth',item.recharge_discount);tag.title='按最优充值方案的实付金额与到账金额计算，四舍五入至一位小数';heading.append(tag);}
        if((item.use_kind!=='recharge'||worthCharging)&&remaining>0&&remaining<=3*24*60*60*1000){const tag=node('span','coupons-tag coupons-tag-expiring','快过期');tag.title='距离卡券到期不超过3天';heading.append(tag);}
      }
      name.append(heading,node('small','coupons-source',item.source||'来源未提供'));
      const value=node('td','coupons-value',amountText(item));
      const status=node('td'),statusBadge=node('span','coupons-status '+(item.status==='available'||(!item.status&&state==='available')?'available':'history'),statusText(item));status.append(statusBadge);
      const action=node('td'),actions=node('div','coupons-row-actions');
      const pending=pendingOrders.find(order=>order.coupon_id===item.id);
      const useLabel=pending?'继续支付':useNames[item.use_kind];
      const button=node('button',useLabel?'secondary coupons-use-button':'quiet',useLabel||'详情');
      button.type='button';
      button.setAttribute('aria-label',useLabel?`${useLabel}：${item.name}`:`查看${item.name}详情`);
      button.setAttribute('aria-haspopup','dialog');
      button.addEventListener('click',()=>pending?openPayment(pending,item):useLabel?openUse(item):openDetails(item));
      actions.append(button);action.append(actions);
      row.append(name,value,node('td','coupons-time',dateText(item.expires_at)),status,action);
      groupedRows[item.category in categories?item.category:'other'].push(row);
    }
    const columns=[];
    if(shown.length){
      const right=category==='all'?node('div','coupons-column-right'):null;
      for(const [key,label] of Object.entries(categories)){
        if(category!=='all'&&category!==key||key==='other'&&!groupedRows.other.length)continue;
        const group=node('section','coupons-group coupons-group-'+key),heading=node('h3','coupons-group-heading',label);
        group.setAttribute('aria-label',label);
        heading.append(node('span','coupons-group-count',`${groupedRows[key].length} 张`));
        const wrap=node('div','coupons-table-wrap'),table=node('table','coupons-table'),cols=node('colgroup'),head=node('thead'),header=node('tr'),body=node('tbody');
        table.setAttribute('aria-label',label+'列表');
        for(const field of ['name','value','time','state','action'])cols.append(node('col','coupons-col-'+field));
        for(const title of ['卡券名称 / 来源','面额','有效期至 · 北京时间','状态','操作']){const th=node('th','',title);th.scope='col';header.append(th);}
        head.append(header);body.append(...(groupedRows[key].length?groupedRows[key]:[emptyRow(filtered?'没有匹配的'+label:'暂无'+label)]));
        table.append(cols,head,body);wrap.append(table);group.append(heading,wrap);
        if(right&&key!=='fee')right.append(group);
        else columns.push(group);
      }
      if(right)columns.push(right);
    }else{
      const message=!selected?(refreshing?'正在读取共享账号…':'共享账号池为空，请从左侧“账号管理”登录。'):selected.status!=='active'?'此账号暂不可用，请从左侧“账号管理”恢复登录。':loading?'正在查询该账号卡券…':queryFailed?'本次查询未完成，点击“刷新卡券”重试。':filtered?'已加载的卡券中没有匹配结果。':state==='available'?'此账号暂无可用卡券。':'此账号暂无卡券历史。';
      columns.push(node('p','coupons-empty',message));
    }
    $('coupons-list').classList.toggle('coupons-columns-filtered',category!=='all');
    $('coupons-list').replaceChildren(...columns);
    $('coupons-pending').hidden=!pendingOrders.length;
    $('coupons-pending-list').replaceChildren(...pendingOrders.map(order=>{
      const line=node('div','coupons-pending-order'),label=node('div');
      label.append(node('strong','',order.coupon_name||'充值订单'),node('small','',`订单 ${order.order_id}${order.amount?' · 实付 '+order.amount+' 元':''}`));
      const button=node('button','secondary','继续支付');button.type='button';button.addEventListener('click',()=>openPayment(order));
      line.append(label,button);return line;
    }));
  }

  async function loadCards(more=false){
    const selected=accounts.find(account=>account.id===loginId),version=++requestVersion;
    if(!more){items=[];setPendingOrders([]);page=0;total=null;hasMore=false;queryFailed=false;feedback('');$('coupons-notices').hidden=true;clearTimeout(noticesTimer);noticesPending=false;}
    if(!selected){loading=false;$('coupons-sync').textContent='请选择账号后查询卡券。';render();return;}
    loading=true;$('coupons-sync').textContent=selected.status==='active'?(more?'正在加载更多卡券…':'正在查询卡券，请稍候…'):'该账号需要恢复登录后再查询。';render();
    try{
      if(!more){
        const pending=await api('/api/coupons/pending?'+new URLSearchParams({login_id:loginId}));
        if(version!==requestVersion)return;
        setPendingOrders(pending.pending_orders);render();
      }
      if(selected.status!=='active')return;
      const query=new URLSearchParams({login_id:loginId,state,page:String(more?page+1:1),sort:$('coupons-sort').value});
      const result=await api('/api/coupons?'+query);
      if(version!==requestVersion)return;
      if(state==='available')reminderQueries.set(selected.id,{revision:selected.credential_revision,loading:false});
      updateReminder(selected.id,result.reminder);
      items=more?[...items,...result.items]:result.items;setPendingOrders(result.pending_orders||[]);page=result.page;total=result.total;hasMore=result.has_more;queryFailed=false;
      $('coupons-notices').hidden=true;clearTimeout(noticesTimer);noticesPending=false;
      $('coupons-sync').textContent='北京时间 · 最近查询 '+formatter.format(new Date());
    }catch(error){
      if(version!==requestVersion)return;
      queryFailed=true;feedback(error.message);if(!more)$('coupons-sync').textContent='查询未完成，点击“刷新卡券”重试。';
      loadNotices();
    }finally{if(version===requestVersion){loading=false;render();}}
  }

  async function refresh(forceReminders=false){
    if(refreshing){refreshPending=true;return;}refreshing=true;render();
    try{
      const result=await api('/api/coupons/accounts');accounts=result.accounts;reminderCounts=result.reminders?.accounts||{};
      if(!accounts.some(account=>account.id===loginId)){closeUse(true);loginId=accounts.find(account=>account.status==='active')?.id||accounts[0]?.id||'';}
      if(accounts.find(account=>account.id===loginId)?.status!=='active'&&!relogin?.required)closeUse(true);
      renderAccounts();refreshOtherReminders(forceReminders);await loadCards();
    }catch(error){feedback(error.message);$('coupons-account-hint').textContent='账号读取失败，请刷新重试。';}
    finally{refreshing=false;render();if(refreshPending){refreshPending=false;await refresh();}}
  }

  function metaRows(entries){
    return entries.flatMap(([label,value])=>[node('dt','',label),node('dd','',value||'—')]);
  }

  function couponDetails(item){
    return [['卡券来源',item.source],['有效期至',dateText(item.expires_at)]];
  }

  function openDetails(item){
    $('coupons-details-title').textContent=item.name;$('coupons-details-category').textContent=categories[item.category]||categories.other;
    $('coupons-details-amount').textContent=amountText(item);$('coupons-details-state').textContent=statusText(item);
    const account=accounts.find(account=>account.id===loginId);
    $('coupons-details-meta').replaceChildren(...metaRows([['所属账号',account?.phone_masked],...couponDetails(item)]));
    $('coupons-details-description').textContent=item.description||'官方暂未提供详细使用说明，请在中国移动 APP 中查看。';$('coupons-details').showModal();
  }

  function renderUseDetails(item, description=item?.description, orderId=''){
    const redemption=!orderId&&redemptionKinds.has(useOperation?.kind||useItem?.use_kind);
    const entries=orderId?[['订单号',orderId]]:[];
    if(item){
      $('coupons-use-category').textContent=categories[item.category]||categories.other;
      if(redemption)entries.push(['卡券名称',item.name]);
      entries.push(...couponDetails(item));
      if(orderId)entries.push(['卡券面额',amountText(item)]);
      else if(!redemption||item.status!=='available')entries.push(['使用状态',statusText(item)]);
    }
    const details=$('coupons-use-details'),summary=$('coupons-use-details-title');
    const slot=$(redemption?'coupons-redemption-help':'coupons-use-details-slot');
    if(details.parentElement!==slot)slot.append(details);
    if(redemption){
      summary.textContent='?';
      summary.setAttribute('aria-label','查看卡券使用说明和详情');
    }else{
      summary.textContent=orderId?(item?'订单与卡券详情':'订单详情'):'卡券详情';
      summary.removeAttribute('aria-label');
    }
    $('coupons-use-details-meta').replaceChildren(...metaRows(entries));
    $('coupons-use-instructions').hidden=!item;
    $('coupons-use-description').textContent=item?(description||'官方暂未提供详细使用说明，请在中国移动 APP 中查看。'):'';
    details.hidden=false;
  }

  function renderUseMeta(item,accountId,prepared){
    const account=accounts.find(account=>account.id===accountId);
    const amount=prepared?.coupon_amount;
    const accountLabel=prepared?.account_masked||account?.phone_masked;
    const value=amount&&amount!=='—'?`${amount}${prepared.coupon_unit||item.unit||''}`:amountText(item);
    const redemption=redemptionKinds.has(prepared?.kind||item.use_kind);
    $('coupons-use').classList.toggle('coupons-redemption-mode',redemption);
    if(redemption){
      const metrics=[['兑换账号',accountLabel],['兑换面额',value]].map(entry=>{
        const metric=node('div','coupons-redemption-metric');
        metric.append(...metaRows([entry]));
        return metric;
      });
      $('coupons-use-meta').replaceChildren(...metrics);
      return;
    }
    $('coupons-use-meta').replaceChildren(...metaRows([
      ['使用账号',accountLabel],
      ['卡券名称',prepared?.coupon_name||item.name],
      ['卡券面额',value],
    ]));
  }

  function renderUseControls(){
    const needsLogin=Boolean(relogin?.required);
    $('coupons-use-recheck').hidden=needsLogin||!useRecheck;
    $('coupons-use-recheck').disabled=useBusy||useConfirming;
    $('coupons-use-recheck').textContent=useRecheck?.expired?'重新准备':'核实上次结果';
    $('coupons-use-retry').hidden=needsLogin||!useRecheck?.can_retry;
    $('coupons-use-retry').disabled=useBusy||useConfirming;
    const terminal=useSubmitted&&!useConfirming;
    $('coupons-use-confirm').disabled=needsLogin||useBusy||useSubmitted||!useOperation||(captchaRequired&&!captchaToken)||(useOperation.kind==='recharge'&&!useOperation.choices.length);
    $('coupons-use-confirm').hidden=needsLogin||terminal||(!useBusy&&!useOperation);
    $('coupons-use-confirm').textContent=useConfirming?'正在提交…':useOperation?(useOperation.kind==='recharge'?'生成支付订单':'确认兑换'):'请稍候';
    $('coupons-use-sms').disabled=needsLogin||useBusy||useSubmitted;
    $('coupons-use-code').disabled=needsLogin||useBusy||useSubmitted;
    $('coupons-use-charge').disabled=useBusy||useSubmitted;
    $('coupons-use-close').disabled=useConfirming;
    $('coupons-use-cancel').disabled=useConfirming;
    $('coupons-use-cancel').textContent=terminal?'关闭':'取消';
    const getPayment=$('coupons-use-get-payment');
    const paymentControls=$($('coupons-payment-panel').hidden?'coupons-payment-retry':'coupons-payment-links');
    if(getPayment.parentElement!==paymentControls)paymentControls.append(getPayment);
    getPayment.disabled=useBusy||(captchaRequired&&!captchaToken);
    getPayment.hidden=needsLogin||!paymentOrder;
    $('coupons-use-relogin').hidden=!needsLogin;
    $('coupons-relogin-open').disabled=Boolean(relogin?.busy);
  }

  function clearPaymentLink(){
    $('coupons-payment-panel').hidden=true;
    $('coupons-payment-url').value='';
    $('coupons-payment-copy-fallback').hidden=true;
    $('coupons-payment-copy-status').textContent='';
    $('coupons-use-payment').hidden=true;$('coupons-use-payment').removeAttribute('href');
    $('coupons-payment-qr').hidden=true;$('coupons-payment-qr').removeAttribute('src');
  }

  function closeUse(force=false){
    if(useConfirming&&!force)return;
    const refreshAfter=useSubmitted;
    ++useGeneration;useOperation=null;useItem=null;useBusy=false;useConfirming=false;useSubmitted=false;relogin=null;useRecheck=null;
    exchangeRiskToken='';
    $('coupons-use-relogin').hidden=true;
    captchaToken='';captchaRequired=false;$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
    paymentOrder='';$('coupons-use-get-payment').hidden=true;clearPaymentLink();
    $('coupons-use').classList.remove('coupons-payment-mode','coupons-redemption-mode');
    $('coupons-use-category').textContent='使用卡券';$('coupons-use-category').hidden=false;
    $('coupons-payment-context').hidden=true;$('coupons-use-meta').hidden=false;
    $('coupons-use-details').hidden=true;$('coupons-use-details').open=false;
    $('coupons-use-form').reset();
    $('coupons-use-sms').textContent='获取验证码';
    $('coupons-use-message').hidden=true;$('coupons-use-error').hidden=true;
    if($('coupons-use').open)$('coupons-use').close();
    if(refreshAfter&&!force)loadCards();
  }

  async function openUse(item){
    if(useConfirming)return;
    closeUse();const generation=useGeneration,accountId=loginId;
    useItem=item;
    useBusy=true;$('coupons-use-title').textContent=useNames[item.use_kind];
    renderUseMeta(item,accountId);
    renderUseDetails(item);$('coupons-use-details').open=!redemptionKinds.has(item.use_kind);
    $('coupons-use-sms-field').hidden=true;$('coupons-use-charge-field').hidden=true;renderUseControls();$('coupons-use').showModal();
    await prepareUse(item,generation,accountId);
  }

  function showCaptcha(challenge){
    captchaToken='';captchaRequired=true;
    if(!paymentOrder)useSubmitted=false;
    $('coupons-use-verification').hidden=false;
    $('coupons-use-verification').querySelector('p').textContent=paymentOrder
      ?'官方验证通过后会自动获取付款链接，无需再次点击。':'官方验证通过后会自动继续，无需再次点击。';
    const frame=node('iframe');
    frame.src='/static/coupon-captcha.html'+(challenge?'?'+new URLSearchParams(challenge):'');
    frame.title=paymentOrder?'支付宝支付前的官方验证':'中国移动官方验证';
    $('coupons-use-captcha').replaceChildren(frame);
  }

  function showUseSuccess(message){
    useConfirming=false;useSubmitted=true;closeUse();
    const toast=$('coupons-toast');
    clearTimeout(successToastTimer);toast.classList.remove('fading');
    toast.textContent='兑换成功 · '+message;toast.hidden=false;
    successToastTimer=setTimeout(()=>{toast.classList.add('fading');successToastTimer=setTimeout(()=>{toast.hidden=true;},250);},2750);
  }

  async function prepareUse(item,generation,accountId,retry=false){
    useRecheck=null;$('coupons-use-error').hidden=true;
    $('coupons-use-message').textContent='正在核对官方用券条件…';$('coupons-use-message').hidden=false;
    try{
      const prepared=await api('/api/coupons/use/prepare',{login_id:accountId,coupon_id:item.id,...(retry?{retry:true}:{})});
      if(generation!==useGeneration)return;
      if(prepared.state==='accepted'){showUseSuccess(prepared.message);return;}
      if(prepared.state==='unconfirmed'){
        useOperation=null;useRecheck=prepared;
        if(prepared.coupon)renderUseDetails(prepared.coupon);
        $('coupons-use-message').textContent=prepared.message;$('coupons-use-message').hidden=false;
        $('coupons-use-sms-field').hidden=true;$('coupons-use-charge-field').hidden=true;
        return;
      }
      if(prepared.state==='payment_required'){
        await openPayment({...prepared.order,coupon_name:prepared.order.coupon_name||item.name},item);
        return;
      }
      useOperation=prepared;$('coupons-use-title').textContent=useNames[prepared.kind];
      renderUseMeta(item,accountId,prepared);
      renderUseDetails({...item,name:prepared.coupon_name||item.name,use_kind:prepared.kind},prepared.description||item.description);
      const redemption=redemptionKinds.has(prepared.kind);
      if(redemption){
        await loadExchangeRiskSdk();
        if(generation!==useGeneration)return;
      }
      $('coupons-use-sms-field').hidden=!prepared.requires_sms;$('coupons-use-code').required=prepared.requires_sms;
      $('coupons-use-charge-field').hidden=prepared.kind!=='recharge';$('coupons-use-charge').required=prepared.kind==='recharge';
      const best=prepared.choices[0];
      $('coupons-use-charge').value=best?.value||'';
      $('coupons-use-best').textContent=best?.label||'暂无可用充值方案';
      $('coupons-use-message').textContent=prepared.message||'核对后点击确认才会提交用券。';
      $('coupons-use-message').hidden=redemption&&(!prepared.message||prepared.message==='确认后提交兑换申请，无需支付费用。')
        &&prepared.state!=='verification_required';
      if(prepared.state==='verification_required'&&prepared.verification==='captcha')showCaptcha(prepared.challenge);
    }catch(error){
      if(generation!==useGeneration)return;
      if(loginRequiredCodes.includes(error.code)){requireSharedLogin('prepare');return;}
      useOperation=null;
      $('coupons-use-message').hidden=true;
      $('coupons-use-error').textContent=error.message;$('coupons-use-error').hidden=false;
    }finally{if(generation===useGeneration){useBusy=false;renderUseControls();}}
  }

  $('coupons-use-recheck').addEventListener('click',()=>{if(!useBusy&&useItem){useBusy=true;renderUseControls();return prepareUse(useItem,useGeneration,loginId);}});
  $('coupons-use-retry').addEventListener('click',()=>{if(!useBusy&&useItem&&useRecheck?.can_retry){useBusy=true;renderUseControls();return prepareUse(useItem,useGeneration,loginId,true);}});

  const smsCode=$('coupons-use-code');
  smsCode.pattern='[0-9]{6}';
  smsCode.addEventListener('input',event=>{
    const field=event.target;
    const digits=field.value.replace(/[^0-9]/g,'').slice(0,6);
    if(digits!==field.value)field.value=digits;
  });

  $('coupons-use-sms').addEventListener('click',async()=>{
    if(!useOperation||useBusy||useSubmitted)return;
    const generation=useGeneration;useBusy=true;$('coupons-use-error').hidden=true;renderUseControls();
    try{
      const result=await api('/api/coupons/use/sms',{operation_id:useOperation.operation_id});
      if(generation!==useGeneration)return;
      $('coupons-use-message').textContent=result.message;$('coupons-use-message').hidden=false;$('coupons-use-code').value='';
      $('coupons-use-sms').textContent='重新获取验证码';
    }catch(error){if(generation===useGeneration){
      if(loginRequiredCodes.includes(error.code)){requireSharedLogin('prepare');return;}
      $('coupons-use-error').textContent=error.message;$('coupons-use-error').hidden=false;
    }}
    finally{if(generation===useGeneration){useBusy=false;renderUseControls();}}
  });

  $('coupons-use-form').addEventListener('submit',async event=>{
    event.preventDefault();if(relogin?.required||!useOperation||useBusy||useSubmitted||captchaRequired&&!captchaToken)return;
    const generation=useGeneration;useBusy=true;useConfirming=true;useSubmitted=true;$('coupons-use-error').hidden=true;renderUseControls();
    try{
      const payload={operation_id:useOperation.operation_id,sms_code:$('coupons-use-code').value.trim(),charge_amount:$('coupons-use-charge').value,captcha_token:captchaToken};
      captchaToken='';
      if(redemptionKinds.has(useOperation.kind)){
        const verified=await api('/api/coupons/use/confirm',{...payload,prepare_risk:true});
        if(generation!==useGeneration)return;
        if(verified.state!=='risk_required')throw new Error('官方用券步骤未完成，请重新打开用券窗口。');
        exchangeRiskToken=await prepareExchangeRisk(verified.sdk_server);
        if(generation!==useGeneration)return;
        const lid=document.cookie.match(/(?:^|;\s*)_zw_kvani5r=([^;]*)/);
        if(!lid?.[1]){
          const error=new Error('官方用券校验未完成，请重试；已通过的短信校验会保留。');
          error.code='RISK_INITIALIZATION_FAILED';throw error;
        }
        Object.assign(payload,{risk_token:exchangeRiskToken,risk_lid:lid[1]});
      }
      const result=await api('/api/coupons/use/confirm',payload);
      if(generation!==useGeneration)return;
      if(result.state==='accepted'){
        showUseSuccess(result.message);
        return;
      }
      captchaToken='';
      if(result.state==='verification_required'&&result.verification==='captcha'){
        showCaptcha(result.challenge);
      }else{$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();}
      if(result.state==='payment_required'){
        useConfirming=false;
        enterPaymentView(result.order_id,useOperation.coupon_name,useItem,$('coupons-use-description').textContent);
        await loadPayment(generation);
        return;
      }
      $('coupons-use-message').textContent=(result.state==='unknown'?'结果待核实 · ':'')+result.message;
      $('coupons-use-message').hidden=false;
    }catch(error){
      if(generation!==useGeneration)return;
      if(loginRequiredCodes.includes(error.code)){useConfirming=false;useSubmitted=false;requireSharedLogin('prepare');return;}
      if(['OPERATION_MISSING','OPERATION_EXPIRED'].includes(error.code)){
        useOperation=null;useSubmitted=false;useConfirming=false;
        captchaRequired=false;captchaToken='';exchangeRiskToken='';
        $('coupons-use-code').value='';$('coupons-use-sms').textContent='获取验证码';
        $('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
        useRecheck={expired:true};
        $('coupons-use-message').textContent='原用券会话已结束，请重新准备并核对当前方案后再确认。';
        $('coupons-use-message').hidden=false;
        $('coupons-use-error').textContent=error.message;$('coupons-use-error').hidden=false;
        return;
      }
      if(error.code==='CAPTCHA_REQUIRED'){
        showCaptcha();
        $('coupons-use-message').textContent='正在等待中国移动官方验证，通过后会自动继续。';
        $('coupons-use-message').hidden=false;
        return;
      }
      if(['SMS_REQUIRED','510002','510003','INVALID_AMOUNT','CAPTCHA_REQUIRED','RISK_INITIALIZATION_FAILED'].includes(error.code))useSubmitted=false;
      $('coupons-use-error').textContent=error.status||error.code==='RISK_INITIALIZATION_FAILED'?error.message:'提交结果待核实，请先在中国移动 APP 中查看，本页不会自动重试。';$('coupons-use-error').hidden=false;
    }finally{if(generation===useGeneration){useBusy=false;useConfirming=false;renderUseControls();}}
  });

  async function loadPayment(generation=useGeneration,refreshLink=false){
    useBusy=true;renderUseControls();$('coupons-use-error').hidden=true;
    if(!refreshLink||!/^https:\/\/(?:[a-z0-9-]+\.)*alipay\.com(?::443)?(?:[/?#]|$)/i.test($('coupons-payment-url').value)){
      clearPaymentLink();
    }
    $('coupons-use-message').textContent='正在核对订单并获取官方支付链接…';$('coupons-use-message').hidden=false;
    try{
      const submittedToken=captchaToken;captchaToken='';
      const result=await api('/api/coupons/use/payment',{login_id:loginId,order_id:paymentOrder,refresh:refreshLink,captcha_token:submittedToken});
      if(generation!==useGeneration)return;
      captchaToken='';
      if(result.state==='verification_required'){
        showCaptcha(result.challenge);
        $('coupons-use-message').textContent=result.message;
        $('coupons-use-get-payment').textContent='验证后继续获取支付链接';
        return;
      }
      captchaRequired=false;$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
      if(result.state==='closed'){
        const closedOrder=paymentOrder;paymentOrder='';$('coupons-use-message').textContent=result.message;
        setPendingOrders(pendingOrders.filter(order=>order.order_id!==closedOrder));render();
        clearPaymentLink();return;
      }
      $('coupons-use-message').hidden=true;
      $('coupons-payment-amount').textContent=result.amount;
      $('coupons-credit-amount').textContent=result.credit_amount??'';
      $('coupons-credit-field').hidden=result.credit_amount==null;
      $('coupons-use-payment').href=result.payment_url;$('coupons-use-payment').hidden=false;
      $('coupons-payment-qr').src=result.qr_image;$('coupons-payment-qr').hidden=false;
      $('coupons-payment-copy-status').textContent='';$('coupons-payment-copy-fallback').hidden=true;
      $('coupons-payment-url').value=result.payment_url;$('coupons-payment-panel').hidden=false;
      $('coupons-use-get-payment').textContent='刷新支付链接';
    }catch(error){
      if(generation!==useGeneration)return;
      captchaRequired=false;captchaToken='';$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
      if(loginRequiredCodes.includes(error.code)){requireSharedLogin('payment');return;}
      $('coupons-use-error').textContent=error.message;$('coupons-use-error').hidden=false;
      $('coupons-use-message').textContent=$('coupons-payment-url').value?'本次未更新链接，已保留之前取得的网页入口，请以官方收银台的订单状态为准。':'订单已保留，获取链接不会重新下单。';
    }finally{if(generation===useGeneration){useBusy=false;renderUseControls();}}
  }

  function requireSharedLogin(resume){
    const first=!relogin;
    if(first)relogin={accountId:loginId,generation:useGeneration,busy:false};
    relogin.required=true;relogin.resume=resume;
    if(resume==='prepare'){
      useOperation=null;exchangeRiskToken='';$('coupons-use-code').value='';
      $('coupons-use-sms').textContent='获取验证码';
    }
    captchaRequired=false;captchaToken='';$('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
    $('coupons-use-error').hidden=true;clearPaymentLink();
    $('coupons-use-message').textContent=paymentOrder?'订单已保留。登录成功后自动获取这笔订单的支付宝链接。':'登录成功后重新读取此券，核对后再确认用券。';
    $('coupons-use-message').hidden=false;
    $('coupons-relogin-message').textContent=first?'请在账号管理恢复当前账号登录。':'官方仍要求验证，请在官方 APP 核对账号状态。';
    renderUseControls();
    if(first)void openSharedLogin();
  }

  async function openSharedLogin(){
    const context=relogin;if(!context?.required||context.busy)return;
    context.busy=true;renderUseControls();
    try{
      const account=await window.requestSharedRelogin(context.accountId);
      if(!account||context!==relogin||context.generation!==useGeneration)return;
      applyAccount({account},context.accountId);
      context.required=false;
      renderUseControls();
      if(context.resume==='payment')await loadPayment(context.generation,true);
      else{
        useSubmitted=false;useBusy=true;renderUseControls();
        await prepareUse(useItem,context.generation,context.accountId);
      }
    }catch(error){
      if(context!==relogin)return;
      $('coupons-use-error').textContent=error.message;$('coupons-use-error').hidden=false;
    }finally{if(context===relogin){context.busy=false;renderUseControls();}}
  }
  $('coupons-relogin-open').addEventListener('click',openSharedLogin);

  function enterPaymentView(orderId,couponName,item,description){
    paymentOrder=orderId;useSubmitted=true;
    const dialog=$('coupons-use');
    dialog.classList.remove('coupons-redemption-mode');
    dialog.classList.add('coupons-payment-mode');
    dialog.scrollTop=0;
    $('coupons-use-title').textContent='支付宝付款';
    $('coupons-use-category').hidden=true;$('coupons-use-meta').hidden=true;
    const account=accounts.find(account=>account.id===loginId);
    const context=$('coupons-payment-context');
    context.textContent=[account?.phone_masked,couponName||item?.name||'话费充值'].filter(Boolean).join(' · ');
    context.hidden=false;
    renderUseDetails(item,description,orderId);$('coupons-use-details').open=false;
    $('coupons-use-sms-field').hidden=true;$('coupons-use-charge-field').hidden=true;
    $('coupons-use-get-payment').textContent='获取支付链接';
  }

  async function openPayment(order,item=items.find(card=>card.id===order.coupon_id)){
    if(useConfirming)return;
    closeUse();
    enterPaymentView(order.order_id,order.coupon_name,item);
    renderUseControls();$('coupons-use').showModal();await loadPayment();
  }
  $('coupons-use-get-payment').addEventListener('click',()=>{if(!useBusy&&paymentOrder)loadPayment(useGeneration,true);});
  $('coupons-copy-payment').addEventListener('click',async()=>{
    const field=$('coupons-payment-url');
    const url=field.value,generation=useGeneration;
    if(!url)return;
    let copied=true;
    try{await navigator.clipboard.writeText(url);}
    catch{copied=false;}
    if(generation!==useGeneration||field.value!==url)return;
    $('coupons-payment-copy-status').textContent=copied?'已复制支付链接':'请手动复制下方链接';
    $('coupons-payment-copy-fallback').hidden=copied;
    if(!copied){field.focus();field.select();}
  });

  function loadExchangeRiskSdk(){
    if(!riskSdk)riskSdk=new Promise((resolve,reject)=>{
      const script=node('script');script.src='/coupon-verification/wallet/index-private-web_a54cc0c.js';
      script.onload=resolve;script.onerror=()=>{riskSdk=null;reject(new Error('官方用券组件加载失败，请重新打开用券窗口。'));};
      document.head.append(script);
    });
    return riskSdk;
  }

  function prepareExchangeRisk(server){
    $('coupons-use-message').textContent='正在获取官方用券校验凭据…';$('coupons-use-message').hidden=false;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error('官方用券校验超时，请重试；已通过的短信校验会保留。')),40000);
      loadExchangeRiskSdk().then(()=>window._zw.ConstID({
        appId:'e1f92650d20263d38dbe327e7237f516',server:location.origin+server,cache:false
      },(error,token)=>{
        clearTimeout(timer);
        if(error||typeof token!=='string'||!token)reject(new Error('官方用券校验未完成，请重试；已通过的短信校验会保留。'));
        else resolve(token);
      })).catch(error=>{clearTimeout(timer);reject(error);});
    }).catch(error=>{error.code='RISK_INITIALIZATION_FAILED';throw error;});
  }

  window.addEventListener('message',event=>{
    const frame=$('coupons-use-captcha').querySelector('iframe');
    if(useBusy||!captchaRequired||!frame||event.source!==frame.contentWindow||event.origin!==location.origin)return;
    if(event.data?.type==='coupon-captcha-success'&&typeof event.data.token==='string'&&event.data.token.length>0&&event.data.token.length<=4096){
      captchaToken=event.data.token;captchaRequired=false;
      $('coupons-use-verification').hidden=true;$('coupons-use-captcha').replaceChildren();
      $('coupons-use-message').textContent='验证已完成，正在自动继续…';
      $('coupons-use-message').hidden=false;renderUseControls();
      if(paymentOrder)loadPayment(useGeneration,true);
      else $('coupons-use-form').requestSubmit();
    }
  });

  $('coupons-refresh').addEventListener('click',()=>refresh(true));
  $('coupons-more').addEventListener('click',()=>{if(!loading)loadCards(true);});
  $('coupons-search').addEventListener('input',render);$('coupons-category').addEventListener('change',render);
  $('coupons-sort').addEventListener('change',()=>{availableSort=$('coupons-sort').value;loadCards();});
  for(const button of document.querySelectorAll('[data-coupons-state]'))button.addEventListener('click',()=>{if(state===button.dataset.couponsState)return;state=button.dataset.couponsState;$('coupons-sort').value=state==='history'?'default':availableSort;loadCards();});
  $('coupons-details-close').addEventListener('click',()=>$('coupons-details').close());
  $('coupons-use-close').addEventListener('click',()=>closeUse());$('coupons-use-cancel').addEventListener('click',()=>closeUse());
  $('coupons-use').addEventListener('click',event=>{
    const help=$('coupons-redemption-help').querySelector('details[open]');
    if(help&&!help.contains(event.target))help.open=false;
  });
  $('coupons-use').addEventListener('cancel',event=>{
    event.preventDefault();
    const help=$('coupons-redemption-help').querySelector('details[open]');
    if(help){
      help.open=false;
      $('coupons-use-details-title').focus();
    }else closeUse();
  });
  window.addEventListener('accounts-changed',()=>refresh());
  refresh();
})();
