(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const accountStates = {active:'已登录',login_required:'需重新登录',restoring:'正在恢复',retry:'等待重连'};
  const loginRequiredCodes = new Set(['APP_LOGIN_REQUIRED','APP_SESSION_EXPIRED','BILL_LOGIN_REQUIRED','BILLS_LOGIN_REQUIRED','USAGE_LOGIN_REQUIRED','SERVICES_LOGIN_REQUIRED']);
  const moneyFields = ['total','payable','original','discount','rebate','balance'];
  const refreshingResults = '正在刷新，显示上次查询结果…';
  const flowOrder = ['国内通用流量','定向流量','国内其他流量','WLAN流量'];
  const formatter = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  const planControl=$('usage-plan-control'),planTrigger=$('usage-plan'),planPopover=$('usage-plan-popover');
  let accounts = [], months = [], loginId = '', month = '', bill = null, errorMessage = '', errorCode = '';
  let usage = null, usageErrorMessage = '', usageErrorCode = '', informationErrorMessage = '';
  let services = null, servicesErrorMessage = '', servicesErrorCode = '', servicesCategory = '';
  let loading = false, usageLoading = false, servicesLoading = false, refreshing = false, refreshPending = false, requestVersion = 0, usageRequestVersion = 0, servicesRequestVersion = 0;
  const expanded = new Set(), usageExpanded = new Set(), serviceDetails = new Set();
  let cancellation = null, cancelPending = false, cancelVersion = 0, cancelTimer = null, cancelCloseTimer = null;
  let renderedPlanUsage,planPinned=false;

  function node(tag,className='',text){const item=document.createElement(tag);item.className=className;if(text!==undefined)item.textContent=text;return item;}
  function loadingPlaceholder(message,count){
    const placeholder=node('div','information-loading');
    placeholder.append(node('span','bills-visually-hidden',message));
    for(let index=0;index<count;index++){
      const block=node('div','information-skeleton');block.setAttribute('aria-hidden','true');placeholder.append(block);
    }
    return placeholder;
  }
  function usagePlaceholderCard(){
    const blank='　';
    const card=node('div','usage-overview-card usage-card-placeholder');
    const reading=node('div','usage-reading'),remaining=node('div','usage-remaining');
    remaining.append(node('span','usage-remaining-label',blank),node('span','usage-quantity',blank));
    const counters=node('dl','usage-overview-counters');
    for(let index=0;index<2;index++){
      const line=node('div');line.append(node('dt','',blank),node('dd','',blank));counters.append(line);
    }
    reading.append(remaining,counters);
    const summary=node('div','usage-card-summary'),meter=node('div','usage-meter');
    meter.append(node('span','usage-meter-ring'));
    summary.append(reading,meter);
    const footer=node('div','usage-group usage-attached');
    footer.append(node('div','usage-group-heading',blank));
    card.append(node('h3','usage-overview-title',blank),summary,footer);
    return card;
  }
  function monthText(value){return value ? `${value.slice(0,4)} 年 ${Number(value.slice(4))} 月` : '月度账单';}
  function amountText(value){return value===null||value===undefined||value===''?'—':String(value);}
  function hasBillDetails(item){return Boolean(item.children?.length)&&item.compact_details!==true;}
  function isActivityRebate(section,item){return section.name==='代付与返费明细'&&item.name==='活动返费';}
  function showBillRebate(item){
    const total=amountText(item.amount);
    $('bills-rebate-detail-total').textContent=total==='—'?total:`${total.replace(/^-/,'')} 元`;
    const details=item.compact_details?[]:(item.children||[]).flatMap(child=>child.children?.length?child.children:[child]);
    $('bills-rebate-details').replaceChildren(...details.map(detail=>{
      const row=node('div'),name=node('dt','',detail.name);
      if(detail.date)name.append(node('small','',detail.date));
      const amount=amountText(detail.amount);
      row.append(name,node('dd','',amount==='—'?amount:`${amount} 元`));
      return row;
    }));
    $('bills-rebate-empty').hidden=Boolean(details.length);
    $('bills-rebate-dialog').showModal();
  }
  function setPlanOpen(open){
    planPopover.hidden=!open;
    planTrigger.setAttribute('aria-expanded',String(open));
    if(!open)planPinned=false;
  }
  function applyAccount(account,accountId){
    if(account?.id!==accountId)return;
    const current=accounts.find(item=>item.id===accountId);
    if(current&&(account.credential_revision||0)<(current.credential_revision||0))return;
    accounts=accounts.map(item=>item.id===accountId?account:item);
    renderAccounts();
  }
  async function api(url,data){
    const response=await fetch(url,data===undefined?undefined:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(!(response.headers.get('Content-Type')||'').includes('application/json'))throw new Error(`本机服务返回异常页面（HTTP ${response.status}），请重启或更新应用后重试。`);
    const result=await response.json();
    if(!response.ok||result.error){const error=new Error(result.error||'信息查询未完成，请稍后重试。');error.code=result.code;error.account=result.account;error.retryAfter=result.retry_after;throw error;}
    return result;
  }
  function renderAccounts(){
    const selected=accounts.find(account=>account.id===loginId);
    $('bills-account-count').textContent=accounts.length;
    $('bills-account-hint').textContent=accounts.length?'与其他功能共用登录':'请从左侧“账号管理”添加账号';
    $('bills-current-account').textContent=selected?`${selected.name||selected.phone_masked} · ${selected.phone_masked}`:'尚未选择账号';
    $('usage-current-account').textContent=selected?.phone_masked||'尚未选择账号';
    $('services-current-account').textContent=$('bills-current-account').textContent;
    $('bills-accounts').replaceChildren(...accounts.map(account=>{
      const button=node('button','bills-account'+(account.id===loginId?' selected':''));button.type='button';
      button.setAttribute('aria-pressed',String(account.id===loginId));
      const label=node('span','bills-account-label');
      label.append(node('strong','',account.name||account.phone_masked),node('span','bills-account-state '+(account.status==='active'?'active':'attention'),accountStates[account.status]||'暂不可用'));
      button.append(label,node('small','',account.phone_masked));
      button.addEventListener('click',()=>{
        const sameAccount=loginId===account.id;
        loginId=account.id;renderAccounts();return loadInformation(sameAccount);
      });
      return button;
    }));
  }
  function renderMonths(){
    const options=months.map(value=>{const option=node('option','',monthText(value));option.value=value;return option;});
    if(!options.length){const option=node('option','','暂无可查月份');option.value='';options.push(option);}
    $('bills-month').replaceChildren(...options);$('bills-month').value=month;
  }
  function expandItems(items,path=''){
    items.forEach((item,index)=>{const key=path+index;if(hasBillDetails(item)){expanded.add(key);expandItems(item.children,key+'.');}});
  }
  function renderUsage(){
    const selected=accounts.find(account=>account.id===loginId);
    const needsLogin=selected&&(loginRequiredCodes.has(usageErrorCode)||(!usage&&selected.status==='login_required'));
    $('usage-content').setAttribute('aria-busy',String(usageLoading));
    $('usage-feedback-message').textContent=usageErrorMessage||(needsLogin?'该账号需要恢复登录，可在账号管理中继续。':'');
    $('usage-feedback').hidden=!usageErrorMessage&&!needsLogin;
    $('usage-relogin').hidden=!needsLogin;
    function quantity(item,field,raw=false){
      const display=raw?null:item.display?.[field];
      const label=item.unlimited&&(field==='total'||field==='remaining')?'不限量':amountText(display?.value??item[field]);
      const value=node('span','usage-quantity',label);
      if(label!=='不限量'&&label!=='—'){
        const unit=display?.unit??item.unit;
        if(unit)value.append(node('small','',unit));
        if(display)value.setAttribute('title',`原始值：${item[field]} ${item.unit||''}`.trim());
      }
      return value;
    }
    function counters(item,className,fields=[['total','总量'],['used','已用'],['remaining','剩余']],raw=false){
      const values=node('dl',className);
      for(const [field,label] of fields){
        const entry=node('div'),value=node('dd');value.append(quantity(item,field,raw));entry.append(node('dt','',label),value);values.append(entry);
      }
      return values;
    }
    function metadata(item,inherited={},resourceName=''){
      const result=node('div','usage-metadata');
      if(item.scope&&item.scope!==inherited.scope)result.append(node('p','',`适用范围：${item.scope}`));
      if((item.start_at||item.end_at)&&(item.start_at!==inherited.start_at||item.end_at!==inherited.end_at))result.append(node('p','',`有效期：${item.start_at||'—'} 至 ${item.end_at||'—'}`));
      if(resourceName||item.description&&item.description!==inherited.description){
        const description=node('details','usage-description');
        description.append(node('summary','','适用说明'));
        if(resourceName)description.append(node('p','',`资源名称：${resourceName}`));
        if(item.description&&item.description!==inherited.description)description.append(node('p','',item.description));result.append(description);
      }
      return result;
    }
    function detailItem(item,inherited={}){
      const detail=node('article','usage-item');
      const hasAmounts=entry=>entry.unlimited||['total','used','remaining'].some(field=>entry[field]!==null&&entry[field]!==undefined&&entry[field]!=='');
      const children=item.children||[];
      const resource=!hasAmounts(item)&&children.length===1&&!children[0].children?.length&&hasAmounts(children[0])?children[0]:item;
      const title=node('div','usage-item-title');title.append(node('h4','',item.name));detail.append(title);
      if(hasAmounts(resource)){
        const usedFields=[['used','已用']];
        const fields=resource.used_only?usedFields:[...usedFields,['remaining','剩余'],['total','总量']];
        const values=counters(resource,'usage-item-counters',fields);
        if(resource.remaining_percent!==null&&resource.remaining_percent!==undefined){
          const track=node('div','usage-progress');track.setAttribute('role','progressbar');track.setAttribute('aria-label',`${item.name}剩余比例`);
          track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax','100');track.setAttribute('aria-valuenow',resource.remaining_percent);
          const fill=node('span');fill.style.width=`${resource.remaining_percent}%`;track.append(fill);values.children[1].append(track);
        }
        detail.append(values);
        if(resource.display){
          const original=node('details','usage-original');original.append(node('summary','','查看原始用量'),counters(resource,'usage-original-counters',resource.used_only?usedFields:undefined,true));title.append(original);
        }
      }
      const meta=node('div','usage-item-meta');
      meta.append(metadata(item,inherited,resource!==item&&resource.name!==item.name?resource.name:''));detail.append(meta);
      if(resource!==item){
        meta.append(metadata(resource,{...inherited,...item}));
      }else if(children.length){
        const nested=node('div','usage-item-children');nested.append(...children.map(child=>detailItem(child,{...inherited,...item})));detail.append(nested);
      }
      return detail;
    }
    function planItem(item,inherited={scope:'套餐说明'}){
      const detail=node('article','usage-item usage-plan-item');
      const title=node('div','usage-plan-line');title.append(node('h4','',item.name));
      if(item.unlimited||item.total!=null&&item.total!=='')title.append(counters(item,'usage-plan-total',[['total','总量']]));
      detail.append(title);
      const context=metadata(item,{
        ...inherited,
        scope:item.scope==='套餐包含（非实时余量）'?item.scope:inherited.scope,
        description:item.description===item.name?item.name:inherited.description,
      });
      if(context.children.length){const meta=node('div','usage-item-meta');meta.append(context);detail.append(meta);}
      if(item.children?.length){
        const children=node('div','usage-plan-children');
        children.append(...item.children.map(child=>planItem(child,item)));detail.append(children);
      }
      return detail;
    }
    function detailGroup(group,index,attached=false){
      const open=usageExpanded.has(index);
      const card=node('section','usage-group'+(attached?' usage-attached':'')+(open?' expanded':''));
      const heading=node('button','usage-group-heading');heading.type='button';heading.id=`usage-detail-${index}`;
      heading.setAttribute('aria-expanded',String(open));heading.setAttribute('aria-label',`${open?'收起':'展开'}${group.name}`);
      heading.append(node('span','',attached?(open?'收起详细信息':'展开详细信息'):group.name));
      if(!attached)heading.append(node('span','usage-group-count',`${group.items.length} 项`));
      const mark=node('span','usage-group-chevron',attached?(open?'⌃':'⌄'):(open?'−':'+'));
      mark.setAttribute('aria-hidden','true');heading.append(mark);
      heading.addEventListener('click',()=>{
        if(open)usageExpanded.delete(index);else usageExpanded.add(index);
        renderUsage();$(heading.id).focus();
      });
      card.append(heading);
      if(open){
        const shared={},first=group.items[0];
        if(group.items.length>1){
          if(first.scope&&group.items.every(item=>item.scope===first.scope))shared.scope=first.scope;
          if((first.start_at||first.end_at)&&group.items.every(item=>item.start_at===first.start_at&&item.end_at===first.end_at)){
            shared.start_at=first.start_at;shared.end_at=first.end_at;
          }
        }
        const common=metadata(shared,{scope:group.name});
        if(common.children.length){common.className+=' usage-group-context';card.append(common);}
        const items=node('div','usage-group-items');
        items.append(...group.items.map(item=>detailItem(item,shared)));card.append(items);
      }
      return card;
    }
    const allGroups=(usage?.groups||[]).filter(group=>group.name!=='其他资源'&&group.name!=='套餐信息');
    if(renderedPlanUsage!==usage){
      renderedPlanUsage=usage;
      const plans=(usage?.groups||[]).filter(group=>group.name==='套餐信息').flatMap(group=>group.items);
      setPlanOpen(false);
      planControl.hidden=!plans.length;
      planTrigger.textContent=plans[0]?.name||'';
      planPopover.replaceChildren(...plans.map(item=>planItem(item)));
    }
    function rank(item){
      if(item.key!=='flow')return flowOrder.length+1;
      const index=flowOrder.indexOf(item.name);
      return index===-1?flowOrder.length:index;
    }
    const overviewItems=[...(usage?.overview||[])].sort((a,b)=>rank(a)-rank(b));
    const smsCount=overviewItems.filter(item=>item.key==='sms').length;
    const attachedGroups=new Set();
    const flowRow=node('div','usage-card-row'), otherRow=node('div','usage-card-row');
    const showPreview=usageLoading&&!usage;
    if(showPreview){
      flowRow.setAttribute('aria-hidden','true');otherRow.setAttribute('aria-hidden','true');
      flowRow.append(usagePlaceholderCard(),usagePlaceholderCard(),usagePlaceholderCard());
      otherRow.append(usagePlaceholderCard(),usagePlaceholderCard());
    }
    for(const item of overviewItems){
      const exhausted=item.remaining_percent!=null&&item.remaining!=null&&Number(item.remaining)===0;
      const card=node('article','usage-overview-card'+(exhausted?' usage-exhausted':''));
      card.setAttribute('data-usage-kind',item.key);
      card.append(node('h3','usage-overview-title',item.name));
      const reading=node('div','usage-reading'),remaining=node('div','usage-remaining');
      remaining.append(node('span','usage-remaining-label',item.used_only?'已用流量':'可用余量'),quantity(item,item.used_only?'used':'remaining'));
      reading.append(remaining);
      if(!item.used_only)reading.append(counters(item,'usage-overview-counters',[['used','已用'],['total','总量']]));
      const summary=node('div','usage-card-summary');summary.append(reading);card.append(summary);
      if(item.remaining_percent!=null){
        const percent=Number(item.remaining_percent);
        const tiny=percent<1&&Number(item.remaining)>0;
        const label=tiny?'剩余不足':Number.isInteger(percent)?'剩余':'剩余约';
        const displayPercent=tiny?'1%':`${Math.floor(percent)}%`;
        const meter=node('div','usage-meter');
        meter.setAttribute('role','meter');meter.setAttribute('aria-label',`${item.name}剩余比例`);
        meter.setAttribute('aria-valuemin','0');meter.setAttribute('aria-valuemax','100');meter.setAttribute('aria-valuenow',item.remaining_percent);
        meter.setAttribute('aria-valuetext',`${label} ${displayPercent}`);
        const overAllowance=Number(item.total)>0&&Number(item.used)>Number(item.total);
        const usedPercent=item.used_percent??(overAllowance?100:null);
        const sweep=(Number(usedPercent)*1.8).toFixed(3)+'deg';
        const ring=node('span','usage-meter-ring');
        ring.style.background=`conic-gradient(from 270deg, var(--usage-ring-fill) ${sweep}, var(--usage-track) 0 180deg, transparent 0)`;
        ring.setAttribute('aria-hidden','true');
        meter.append(ring);
        if(usedPercent!==null){
          const pointer=node('span','usage-meter-pointer');
          pointer.style.transform=`rotate(${sweep})`;
          pointer.setAttribute('aria-hidden','true');meter.append(pointer);
        }
        const readout=node('div','usage-meter-readout');
        readout.append(node('span','',label+' '),node('strong','',displayPercent),node('span','usage-status',exhausted?'已用尽':'使用中'));
        meter.append(readout);summary.append(meter);
      }
      const meta=metadata(item,{scope:item.name});
      if(meta.children.length)card.append(meta);
      const detailIndex=allGroups.findIndex((group,index)=>group.key===item.key&&!attachedGroups.has(index)&&
        (group.name===item.name||(item.key==='sms'&&smsCount===1)));
      if(detailIndex!==-1){
        card.append(detailGroup(allGroups[detailIndex],detailIndex,true));attachedGroups.add(detailIndex);
      }
      (item.key==='flow'?flowRow:otherRow).append(card);
    }
    const overview=[flowRow,otherRow].filter(row=>row.children.length);
    if(showPreview)overview.unshift(node('span','bills-visually-hidden','正在查询该账号套餐余量…'));
    const groups=allGroups.flatMap((group,index)=>attachedGroups.has(index)?[]:[detailGroup(group,index)]);
    if(!overview.length&&!groups.length){
      const message=!selected?(refreshing?'正在读取共享账号…':'共享账号池为空，请从左侧“账号管理”登录。'):usageErrorMessage?'本次余量查询未完成，点击“刷新信息”重试。':usage?usage.message||'官方暂未提供套餐余量。':'选择账号后查询套餐余量。';
      overview.push(node('p','bills-empty',message));
    }
    $('usage-overview').replaceChildren(...overview);
    $('usage-overview').hidden=!overview.length;
    $('usage-groups').replaceChildren(...groups);
    $('usage-groups').hidden=!groups.length;
    const notes=usage?.notes||[];$('usage-notes-list').replaceChildren(...notes.map(note=>node('li','',note)));$('usage-notes').hidden=!notes.length;
    $('usage-sync').textContent=usageLoading?(usage?refreshingResults:'正在查询官方套餐余量…'):usage?.queried_at?'北京时间 · 最近查询 '+formatter.format(new Date(usage.queried_at)):usageErrorMessage?'查询未完成':refreshing?'正在读取共享账号…':'尚未查询套餐余量';
  }
  function renderCancellation(){
    const ready=Boolean(cancellation?.cancel_id)&&!cancellation.finished;
    $('services-cancel-submit').disabled=cancelPending||!ready||!cancellation.smsReady||!/^\d{6}$/.test($('services-cancel-code').value);
    $('services-cancel-submit').textContent=cancellation?.stage==='submitting'?'提交中…':'确认退订';
    $('services-cancel-submit').hidden=Boolean(cancellation?.finished);
    $('services-cancel-sms').disabled=cancelPending||!ready||Boolean(cancellation.retryAfter);
    $('services-cancel-sms').textContent=cancellation?.retryAfter?`${cancellation.retryAfter} 秒后重发`:'获取验证码';
    $('services-cancel-code').disabled=cancelPending||!ready;
    $('services-cancel-close').disabled=cancelPending;
    $('services-cancel-dismiss').disabled=cancelPending;
    $('services-cancel-dismiss').textContent=cancellation?.finished?'关闭':'取消';
  }
  function cancellationMessage(message,kind=''){
    $('services-cancel-message').textContent=message;
    $('services-cancel-message').hidden=!message;
    $('services-cancel-message').className='services-cancel-message'+(kind?' '+kind:'');
  }
  function cancelCountdown(seconds){
    if(cancelTimer!==null)clearInterval(cancelTimer);
    cancellation.retryAfter=seconds;renderCancellation();
    cancelTimer=setInterval(()=>{
      cancellation.retryAfter=Math.max(0,cancellation.retryAfter-1);
      if(!cancellation.retryAfter){clearInterval(cancelTimer);cancelTimer=null;}
      renderCancellation();
    },1000);
  }
  function closeCancellation(){
    if(cancelPending)return;
    if(cancelTimer!==null){clearInterval(cancelTimer);cancelTimer=null;}
    if(cancelCloseTimer!==null){clearTimeout(cancelCloseTimer);cancelCloseTimer=null;}
    cancelVersion++;cancellation=null;$('services-cancel-dialog').close();
  }
  async function openCancellation(item){
    const version=++cancelVersion,selected=accounts.find(account=>account.id===loginId);
    cancellation={login_id:loginId,name:item.name,stage:'preparing',smsReady:false,retryAfter:0};cancelPending=true;
    $('services-cancel-account').textContent=`${selected.name||selected.phone_masked} · ${selected.phone_masked}`;
    $('services-cancel-name').textContent=item.name;$('services-cancel-info').hidden=true;
    $('services-cancel-code').value='';
    cancellationMessage('正在核对官方退订条件和关联影响…');renderCancellation();$('services-cancel-dialog').showModal();
    try{
      const result=await api('/api/services/cancel/prepare',{login_id:cancellation.login_id,action_id:item.cancel.action_id});
      if(version!==cancelVersion)return;
      applyAccount(result.account,cancellation.login_id);
      cancellation={...cancellation,...result,stage:'prepared',effective_type:result.effective_options[0].value};
      $('services-cancel-name').textContent=result.name;
      $('services-cancel-warnings').replaceChildren(...(result.warnings||[]).map(message=>node('p','',message)));
      const impacts=[];
      for(const [key,label] of [['bound_services','以下业务将一同退订'],['related_services','退订后将无法享受以下优惠']]){
        if(result[key]?.length){const block=node('section'),list=node('ul');block.append(node('h4','',label));list.append(...result[key].map(name=>node('li','',name)));block.append(list);impacts.push(block);}
      }
      $('services-cancel-related').replaceChildren(...impacts);
      $('services-cancel-description').textContent=result.description||'';
      $('services-cancel-effective').textContent='失效时间：'+result.effective_options[0].label;
      $('services-cancel-info').hidden=false;
      cancellationMessage('');
    }catch(error){
      if(version!==cancelVersion)return;
      applyAccount(error.account,cancellation.login_id);cancellationMessage(error.message,'error');
    }finally{if(version===cancelVersion){cancelPending=false;renderCancellation();}}
  }
  async function sendCancelSms(){
    cancelPending=true;cancellation.smsReady=false;cancellationMessage('正在发送退订验证码…');renderCancellation();
    try{
      const result=await api('/api/services/cancel/sms',{login_id:cancellation.login_id,cancel_id:cancellation.cancel_id,effective_type:cancellation.effective_type});
      cancellation.smsReady=true;$('services-cancel-code').value='';cancelCountdown(result.retry_after||60);
      cancellationMessage(result.message||'退订验证码已发送，请填写后确认。');$('services-cancel-code').focus();
    }catch(error){cancellationMessage(error.message,'error');if(error.retryAfter)cancelCountdown(error.retryAfter);}
    finally{cancelPending=false;renderCancellation();}
  }
  async function confirmCancellation(){
    cancelPending=true;cancellation.stage='submitting';cancellationMessage('正在提交退订，请等待官方结果…');renderCancellation();
    try{
      const result=await api('/api/services/cancel/confirm',{login_id:cancellation.login_id,cancel_id:cancellation.cancel_id,effective_type:cancellation.effective_type,sms_code:$('services-cancel-code').value,confirmed:true});
      cancellation.finished=true;cancellationMessage(result.message,result.status==='success'?'success':'notice');
      applyAccount(result.account,cancellation.login_id);
      if(result.status==='success'){
        cancelCloseTimer=setTimeout(closeCancellation,3000);
        if(loginId===cancellation.login_id)void loadServices(true);
      }
    }catch(error){
      cancellationMessage(error.message,'error');
      if(['312009','SERVICES_SMS_EXPIRED','SERVICES_SMS_MISMATCH'].includes(error.code))cancellation.smsReady=false;
      if(!['312008','312009','SERVICES_SMS_EXPIRED','SERVICES_SMS_MISMATCH','INVALID_REQUEST'].includes(error.code))cancellation.finished=true;
    }finally{cancellation.stage='prepared';cancelPending=false;renderCancellation();}
  }
  function renderServices(selectMatchingCategory=false){
    const selected=accounts.find(account=>account.id===loginId);
    const needsLogin=selected&&(loginRequiredCodes.has(servicesErrorCode)||(!services&&selected.status==='login_required'));
    $('services-content').setAttribute('aria-busy',String(servicesLoading));
    $('services-feedback-message').textContent=servicesErrorMessage||(needsLogin?'该账号需要恢复登录，可在账号管理中继续。':'');
    $('services-feedback').hidden=!servicesErrorMessage&&!needsLogin;
    $('services-relogin').hidden=!needsLogin;
    const query=$('services-search').value.trim().toLocaleLowerCase(),cancelOnly=Boolean($('services-cancel-only').checked);
    $('services-search').disabled=!services;
    $('services-cancel-only').disabled=!services;
    const sort=$('services-sort');
    sort.disabled=!services;
    function containsQuery(item){
      return item.name.toLocaleLowerCase().includes(query)||(item.fields||[]).some(field=>field.name.includes('说明')&&String(field.value??'').toLocaleLowerCase().includes(query))||(item.children||[]).some(containsQuery);
    }
    const matches=item=>(!cancelOnly||item.cancel?.available)&&(!query||containsQuery(item));
    function orderKey(item){
      const value=item.fields?.find(field=>field.name==='订购时间')?.value;
      const digits=String(value??'').trim().replace(/[-/: T]/g,'');
      return /^\d{8}(?:\d{4}(?:\d{2})?)?$/.test(digits)?digits.padEnd(14,'0'):'';
    }
    function displayTime(value){
      return String(value).trim()
        .replace(/^(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})(?:\d{2})?)?$/,
          (_,year,month,day,hour,minute)=>`${year}-${month}-${day}${hour?` ${hour}:${minute}`:''}`)
        .replace(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2})?$/,'$1 $2');
    }
    function serviceCards(items,path,label,sourceItems=items){
      const positions=new Map(sourceItems.map((item,index)=>[item,index]));
      const list=node('div','services-card-list');list.setAttribute('role','group');list.setAttribute('aria-label',label);
      const direction=sort.value==='newest'?-1:sort.value==='oldest'?1:0;
      const ordered=[...items].sort((a,b)=>{
        const byStatus=Number(b.status==='已退订')-Number(a.status==='已退订');
        if(byStatus||!direction)return byStatus;
        const first=orderKey(a),second=orderKey(b);
        if(!first||!second)return Number(Boolean(second))-Number(Boolean(first));
        return direction*first.localeCompare(second);
      });
      for(const item of ordered){
        const key=path+positions.get(item),fields=[{name:'资费',value:item.fee},{name:'状态',value:item.status},...(item.fields||[])];
        if(item.cancel?.reason)fields.push({name:'退订说明',value:item.cancel.reason});
        const cancelled=item.status==='已退订';
        const card=node('article','services-card'+(cancelled?' services-cancelled':''));
        const row=node('div','services-row'+(cancelled?' services-cancelled':''));
        const heading=node('button','services-name-label');
        const info=node('span','services-info-mark','?');info.setAttribute('aria-hidden','true');
        heading.type='button';heading.setAttribute('aria-label',`${item.name}详情`);
        heading.append(node('span','',item.name),info);row.append(heading);
        if(cancelled)heading.append(node('small','services-cancelled-label','已退订'));
        const dates=new Map((item.fields||[]).filter(field=>amountText(field.value)!=='—').map(field=>[field.name,field.value]));
        const order=dates.get('订购时间'),start=dates.get('生效时间'),end=dates.get('失效时间');
        const period=dates.get('有效期限')||dates.get('有效期');
        const facts=node('dl','services-card-facts');
        const addFact=(label,value)=>{
          const fact=node('div');fact.append(node('dt','',label),node('dd','',value));facts.append(fact);
        };
        if(order)addFact('订购时间',displayTime(order));
        if(period)addFact('有效期限',period);
        else if(start&&end){
          const endTime=displayTime(end);
          const rangeEnd=/^至/.test(endTime)?endTime:endTime==='长期有效'?`· ${endTime}`:`至 ${endTime}`;
          addFact('有效期限',`${displayTime(start)} 起 ${rangeEnd}`);
        }else if(start)addFact('生效时间',displayTime(start));
        else if(end)addFact('失效时间',displayTime(end));
        if(facts.children.length)row.append(facts);
        if(item.cancel?.available){
          const actions=node('div','services-actions');
          const cancel=node('button','services-cancel-action','退订');cancel.type='button';cancel.setAttribute('aria-label',`退订${item.name}`);
          cancel.addEventListener('click',()=>openCancellation(item));actions.append(cancel);row.append(actions);
        }
        const detailRow=node('div','services-detail-row');
        const detailId='services-detail-'+key.replaceAll('.','-');detailRow.setAttribute('id',detailId);heading.setAttribute('aria-controls',detailId);
        card.append(row,detailRow);list.append(card);
        const showDetails=()=>{
          const open=serviceDetails.has(key);detailRow.hidden=!open;heading.setAttribute('aria-expanded',String(open));
          if(!open||detailRow.children.length)return;
          const details=node('dl','services-fields');
          for(const field of fields){const entry=node('div');entry.append(node('dt','',field.name),node('dd','',amountText(field.value)));details.append(entry);}
          detailRow.append(details);
          if(item.children?.length){
            const children=node('div','services-related');children.append(node('h4','','关联业务'),serviceCards(item.children,key+'.',`${item.name}关联业务`));detailRow.append(children);
          }
        };
        heading.addEventListener('click',()=>{if(serviceDetails.has(key))serviceDetails.delete(key);else serviceDetails.add(key);showDetails();});showDetails();
      }
      return list;
    }
    const categoryNames=['我的套餐','增值业务','基础功能','活动'];
    const categories=categoryNames.map(name=>({name,sections:[]}));
    let firstCategory='';
    for(const group of services?.groups||[]){
      const label=group.name.replace(/^手机(?=\s*·|线路)/,'').replace(/^\s*·\s*/,'').trim();
      const name=categoryNames.find(name=>label===name||label.endsWith(' · '+name))||label;
      let category=categories.find(category=>category.name===name);
      if(!category){category={name,sections:[]};categories.push(category);}
      category.sections.push({...group,name:label});
      if(!firstCategory)firstCategory=name;
    }
    const counts=categories.map(category=>category.sections.reduce((count,section)=>count+section.items.filter(matches).length,0));
    let activeName=categories.some(category=>category.name===servicesCategory)?servicesCategory:firstCategory||categories[0].name;
    if(selectMatchingCategory&&!counts[categories.findIndex(category=>category.name===activeName)]&&counts.some(Boolean))activeName=categories[counts.findIndex(Boolean)].name;
    if(services)servicesCategory=activeName;
    const activeIndex=categories.findIndex(category=>category.name===activeName);
    const tabs=categories.map((category,index)=>{
      const selected=category.name===activeName,button=node('button','services-tab'+(selected?' selected':''));button.type='button';
      button.disabled=!services;button.tabIndex=selected?0:-1;
      button.setAttribute('id',`services-tab-${index}`);button.setAttribute('role','tab');button.setAttribute('aria-selected',String(selected));button.setAttribute('aria-controls','services-groups');
      const count=counts[index];
      const badge=node('small','services-tab-count',services?String(count):'—');
      badge.setAttribute('aria-label',services?`${count} 项业务`:'数量待查询');
      button.append(node('span','',category.name),badge);
      button.addEventListener('click',()=>{servicesCategory=category.name;renderServices();});
      button.addEventListener('keydown',event=>{
        if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
        event.preventDefault();
        const next=event.key==='Home'?0:event.key==='End'?categories.length-1:(index+(event.key==='ArrowLeft'?-1:1)+categories.length)%categories.length;
        servicesCategory=categories[next].name;renderServices();$('services-tabs').children[next].focus();
      });
      return button;
    });
    $('services-tabs').replaceChildren(...tabs);
    $('services-groups').setAttribute('aria-labelledby',`services-tab-${activeIndex}`);
    $('services-filter-count').textContent=services?(query||cancelOnly?`匹配 ${counts.reduce((sum,count)=>sum+count,0)} 项 · 当前 ${counts[activeIndex]} 项`:`当前 ${counts[activeIndex]} 项`):'';
    const groups=categories[activeIndex].sections.flatMap((section,index)=>{
      const items=section.items.filter(matches);
      if(!items.length)return [];
      const content=node('section','services-section');
      if(section.name!==activeName)content.append(node('h3','services-section-heading',section.name));
      content.append(serviceCards(items,`${activeIndex}.${index}.`,section.name,section.items));return [content];
    });
    if(!groups.length){
      const message=servicesLoading?'正在查询该账号已订业务…':!selected?(refreshing?'正在读取共享账号…':'共享账号池为空，请从左侧“账号管理”登录。'):servicesErrorMessage?'本次已订业务查询未完成，点击“刷新信息”重试。':services?(query||cancelOnly?'当前分类没有匹配的业务，请调整关键词或筛选条件。':services.message||`${activeName}暂无已订业务记录。`):'选择账号后查询已订业务。';
      groups.push(servicesLoading?loadingPlaceholder(message,3):node('p','services-empty',message));
    }
    $('services-groups').replaceChildren(...groups);
    const notes=services?.notes||[];$('services-notes-list').replaceChildren(...notes.map(note=>node('li','',note)));$('services-notes').hidden=!notes.length;
    $('services-sync').textContent=servicesLoading?(services?refreshingResults:'正在查询官方已订业务…'):services?.queried_at?'北京时间 · 最近查询 '+formatter.format(new Date(services.queried_at)):servicesErrorMessage?'查询未完成':refreshing?'正在读取共享账号…':'尚未查询已订业务';
  }
  function renderRefreshState(){
    const busy=loading||usageLoading||servicesLoading||refreshing;
    $('bills-refresh').disabled=busy;
    $('bills-refresh').setAttribute('aria-busy',String(busy));
    $('bills-refresh').textContent=busy?'查询中…':'刷新信息';
    $('information-feedback-message').textContent=informationErrorMessage;
    $('information-feedback').hidden=!informationErrorMessage;
  }
  function showQueryStart(panel,keepData,renderPanel){
    if(keepData){
      $(panel+'-content').setAttribute('aria-busy','true');
      $(panel+'-sync').textContent=refreshingResults;
    }else renderPanel();
    renderRefreshState();
  }
  function renderBill(){
    const selected=accounts.find(account=>account.id===loginId),index=months.indexOf(month);
    const needsLogin=selected&&(loginRequiredCodes.has(errorCode)||(!bill&&selected.status==='login_required'));
    $('bills-content').setAttribute('aria-busy',String(loading));
    $('bills-month').disabled=!months.length;
    $('bills-previous').disabled=index<0||index===months.length-1;
    $('bills-next').disabled=index<=0;
    $('information-current-account').textContent=selected?`${selected.name||selected.phone_masked} · ${selected.phone_masked}`:'尚未选择账号';
    $('bills-title').textContent='月度账单';
    const start=Math.max(0,Math.min(index-2,months.length-7));
    $('bills-months').replaceChildren(...months.slice(start,start+7).map(value=>{
      const button=node('button','bills-month-tab'+(value===month?' selected':''));button.type='button';
      button.setAttribute('aria-pressed',String(value===month));button.setAttribute('aria-label',monthText(value));
      button.append(node('strong','',`${Number(value.slice(4))}月`),node('span','',value.slice(0,4)));
      button.addEventListener('click',()=>changeMonth(value));return button;
    }));
    $('bills-period').textContent=bill?.period||(month?monthText(month):'选择账号和月份后查看');
    for(const field of moneyFields)$('bills-'+field).textContent=amountText(bill?.[field]);
    $('bills-breakdown-payable').textContent=amountText(bill?.payable);
    const [payable,original,discount,rebate]=['payable','original','discount','rebate']
      .map(field=>Math.round(Number(amountText(bill?.[field]))*100));
    const balanced=[payable,original,discount,rebate].every(Number.isSafeInteger)&&payable===original-discount-rebate;
    $('bills-breakdown').setAttribute('data-balanced',String(balanced));
    const payments=bill?.sections?.find(section=>section.name==='代付与返费明细')?.items||[];
    const activityRebate=payments.length===1&&payments[0].name==='活动返费'&&
      Math.round(Math.abs(Number(amountText(payments[0].amount)))*100)===rebate;
    $('bills-rebate-label').textContent=activityRebate?'活动返费':'返费抵扣';
    const summary=(bill?.summary||[]).filter(item=>amountText(item.amount)!=='—');
    const saving=summary.find(item=>item.name==='本月为您节省');
    const extraSummary=summary.filter(item=>item!==saving);
    $('bills-extra-summary').replaceChildren(...extraSummary.map(item=>{
      const entry=node('div'),amount=node('dd',Number(item.amount)<0?'bills-deduction':'',amountText(item.amount));amount.append(node('span','',' 元'));
      entry.append(node('dt','',item.name),amount);return entry;
    }));
    $('bills-extra-summary').hidden=!extraSummary.length;
    const notes=bill?.notes||[];
    $('bills-notes-list').replaceChildren(...notes.map(note=>node('li','',note)));
    $('bills-notes').hidden=!notes.length;
    $('bills-reading-guide').hidden=!notes.length;
    $('bills-feedback-message').textContent=errorMessage||(needsLogin?'该账号需要恢复登录，可在账号管理中继续。':'');
    $('bills-feedback').hidden=!errorMessage&&!needsLogin;
    $('bills-relogin').hidden=!needsLogin;
    function appendRows(items,rows,depth=0,path=''){
      items.forEach((item,itemIndex)=>{
        const key=path+itemIndex,hasChildren=hasBillDetails(item),open=hasChildren&&expanded.has(key);
        const row=node('tr',depth===0?'bills-row-total':'bills-row-detail'),name=node('td'),label=node('div','bills-item-name');
        name.style.paddingInlineStart=`${8+Math.min(depth,4)*8}px`;
        label.append(node('span','bills-item-text',item.name));name.append(label);
        const actual=node('td','bills-number'+(Number(item.amount)<0?' bills-deduction':''));
        const value=amountText(item.amount);
        if(hasChildren){
          const toggle=node('button','bills-item-toggle',value);toggle.type='button';
          toggle.setAttribute('aria-expanded',String(open));
          toggle.setAttribute('aria-label',`${open?'收起':'展开'}${item.name}明细，实际消费 ${value}`);
          toggle.id=`bill-row-${key}`;
          toggle.addEventListener('click',()=>{if(expanded.has(key))expanded.delete(key);else expanded.add(key);renderBill();$(toggle.id).focus();});
          actual.append(toggle);
        }else actual.textContent=value;
        row.append(name,node('td','bills-number'+(Number(item.original)<0?' bills-deduction':''),amountText(item.original)),node('td','bills-number'+(Math.abs(Number(item.discount))>0?' bills-deduction':''),amountText(item.discount)),actual);
        rows.push(row);
        if(hasChildren&&open)appendRows(item.children,rows,depth+1,key+'.');
      });
    }
    function billTable(items,label,path='',withTotals=false){
      const wrap=node('div','bills-table-wrap'),table=node('table','bills-table'),head=node('thead'),header=node('tr'),body=node('tbody'),rows=[];
      table.setAttribute('aria-label',label);
      for(const title of ['消费项目','原价','减免','实际消费']){
        const cell=node('th','',title);cell.setAttribute('scope','col');header.append(cell);
      }
      appendRows(items,rows,withTotals?0:1,path);
      head.append(header);body.append(...rows);table.append(head,body);
      if(withTotals){
        const foot=node('tfoot');
        if(bill.total!=null){
          const total=node('tr','bills-total-row'),discount=amountText(bill.discount);
          const title=node('th','','合计');title.setAttribute('scope','row');
          total.append(title,node('td','bills-number',amountText(bill.original)),
            node('td',Number(discount)>0?'bills-number bills-deduction':'bills-number',Number(discount)>0?'-'+discount:discount),
            node('td','bills-number',amountText(bill.total)));
          foot.append(total);
        }
        for(const item of payments.filter(item=>item.name==='活动返费')){
          const row=node('tr','bills-rebate-row'),label=node('th');
          label.setAttribute('scope','row');label.colSpan=3;
          const trigger=node('button','bills-rebate-trigger','活动返费 ›');trigger.type='button';
          trigger.setAttribute('aria-haspopup','dialog');
          trigger.addEventListener('click',()=>showBillRebate(item));
          label.append(trigger);row.append(label,node('td','bills-number',amountText(item.amount)));
          foot.append(row);
        }
        if(bill.payable!=null){
          const row=node('tr','bills-payable-row'),label=node('th','','实际应付');
          label.setAttribute('scope','row');label.colSpan=3;
          row.append(label,node('td','bills-number',amountText(bill.payable)));foot.append(row);
        }
        if(foot.children.length)table.append(foot);
      }
      wrap.append(table);return wrap;
    }
    const cards=[];
    function appendSection(section,path){
      section.items.forEach((item,itemIndex)=>{
        const key=path+itemIndex,opensRebate=isActivityRebate(section,item);
        if(opensRebate&&bill.items.length)return;
        const hasChildren=!opensRebate&&hasBillDetails(item),open=hasChildren&&expanded.has(key);
        const card=node('article','bills-detail-card'+(open?' expanded':''));
        const heading=node(hasChildren||opensRebate?'button':'div','bills-card-heading');
        const label=node('span','bills-card-label',item.name),amount=node('span','bills-card-value');
        amount.append(node('strong',Number(item.amount)<0?'bills-deduction':'',amountText(item.amount)),node('small','','元'));
        if(opensRebate){
          heading.type='button';heading.setAttribute('aria-haspopup','dialog');
          label.append(node('span','bills-card-chevron','›'));
          heading.addEventListener('click',()=>showBillRebate(item));
        }else if(hasChildren){
          heading.type='button';heading.setAttribute('aria-expanded',String(open));heading.setAttribute('aria-label',`${open?'收起':'展开'}${item.name}明细`);
          label.append(node('span','bills-card-chevron',open?'−':'+'));
          heading.addEventListener('click',()=>{if(expanded.has(key))expanded.delete(key);else expanded.add(key);renderBill();});
        }
        heading.append(label,amount);
        const summary=node('div','bills-card-summary');
        for(const [field,title] of [['original','优惠前'],['discount','减免']]){
          const value=amountText(item[field]);
          if(value!=='—')summary.append(node('span',field==='discount'&&Math.abs(Number(item.discount))>0?'bills-deduction':'',`${title} ${value} 元`));
        }
        card.append(heading);
        if(summary.children.length)card.append(summary);else card.className+=' no-summary';
        if(open)card.append(billTable(item.children,`${item.name}费用明细（元）`,key+'.'));
        cards.push(card);
      });
    }
    if(bill?.items.length)cards.push(billTable(bill.items,'月度账单明细（元）','',true));
    (bill?.sections||[]).forEach((section,sectionIndex)=>{
      if(saving&&section.name==='节省明细')return;
      const visible=section.items.filter(item=>!(bill.items.length&&isActivityRebate(section,item)));
      if(!visible.length)return;
      if(visible.length!==1||visible[0].compact_details!==true)cards.push(node('h3','bills-section-heading',section.name));
      appendSection(section,`section-${sectionIndex}.`);
    });
    if(saving){
      const details=bill.sections?.find(section=>section.name==='节省明细')?.items||[];
      const panel=node(details.length?'details':'section','bills-savings');
      panel.id='bills-savings';
      if(details.length)panel.open=$('bills-savings')?.open===true;
      const heading=node(details.length?'summary':'div','bills-savings-heading');
      heading.append(node('span','','本月节省'),node('strong','',`${saving.amount} 元`));
      panel.append(heading);
      if(details.length){
        const rows=node('dl');
        for(const item of details){
          const row=node('div'),amount=amountText(item.amount);
          row.append(node('dt','',item.name),node('dd','',amount==='—'?amount:`${amount} 元`));rows.append(row);
        }
        panel.append(rows);
      }
      cards.push(panel);
    }
    if(!cards.length){
      const message=loading?'正在查询该账号账单…':!selected?(refreshing?'正在读取共享账号…':'共享账号池为空，请从左侧“账号管理”登录。'):errorMessage?'本次查询未完成，点击“刷新信息”重试。':!month?'当前没有可查询的账单月份。':bill?bill.message||'该月暂无账单明细。':'选择账号和月份后查询账单。';
      cards.push(loading?loadingPlaceholder(message,3):node('p','bills-empty',message));
    }
    $('bills-items').replaceChildren(...cards);
    $('bills-expand').disabled=!(bill?.items.some(hasBillDetails)||(bill?.sections||[]).some(section=>section.items.some(item=>!isActivityRebate(section,item)&&hasBillDetails(item))));
    $('bills-collapse').disabled=$('bills-expand').disabled;
    $('bills-sync').textContent=loading?(bill?refreshingResults:'正在查询官方账单…'):bill?.queried_at?'北京时间 · 最近查询 '+formatter.format(new Date(bill.queried_at)):errorMessage?'查询未完成':refreshing?'正在读取共享账号…':'尚未查询账单';
  }
  async function loadBill(sameAccount=false){
    const version=++requestVersion,accountId=loginId,queryMonth=month;
    if($('bills-rebate-dialog').open)$('bills-rebate-dialog').close();
    const keepData=Boolean(sameAccount&&bill&&bill.month===queryMonth);
    if(!keepData){bill=null;expanded.clear();}
    errorMessage='';errorCode='';
    loading=Boolean(accountId&&queryMonth);
    showQueryStart('bills',keepData,renderBill);
    if(!loading)return;
    try{
      const result=await api('/api/bills/query?'+new URLSearchParams({login_id:accountId,month:queryMonth}));
      if(version!==requestVersion)return;
      applyAccount(result.account,accountId);bill=result;
      if(!keepData)expandItems(bill.items);
    }catch(error){
      if(version!==requestVersion)return;
      applyAccount(error.account,accountId);bill=null;errorMessage=error.message;errorCode=error.code||'';
    }finally{if(version===requestVersion){loading=false;renderBill();renderRefreshState();}}
  }
  async function loadUsage(sameAccount=false){
    const version=++usageRequestVersion,accountId=loginId;
    const keepData=Boolean(sameAccount&&usage);
    if(!keepData){usage=null;usageExpanded.clear();}
    usageErrorMessage='';usageErrorCode='';
    usageLoading=Boolean(accountId);
    showQueryStart('usage',keepData,renderUsage);
    if(!usageLoading)return;
    try{
      const result=await api('/api/usage/query?'+new URLSearchParams({login_id:accountId}));
      if(version!==usageRequestVersion)return;
      applyAccount(result.account,accountId);usage=result;
    }catch(error){
      if(version!==usageRequestVersion)return;
      applyAccount(error.account,accountId);usage=null;usageErrorMessage=error.message;usageErrorCode=error.code||'';
    }finally{if(version===usageRequestVersion){usageLoading=false;renderUsage();renderRefreshState();}}
  }
  async function loadServices(sameAccount=false){
    const version=++servicesRequestVersion,accountId=loginId;
    const keepData=Boolean(sameAccount&&services);
    if(!keepData){services=null;serviceDetails.clear();}
    servicesErrorMessage='';servicesErrorCode='';
    servicesLoading=Boolean(accountId);
    $('services-groups').inert=servicesLoading;
    showQueryStart('services',keepData,renderServices);
    if(!servicesLoading)return;
    try{
      const result=await api('/api/services/query?'+new URLSearchParams({login_id:accountId}));
      if(version!==servicesRequestVersion)return;
      applyAccount(result.account,accountId);services=result;
    }catch(error){
      if(version!==servicesRequestVersion)return;
      applyAccount(error.account,accountId);services=null;servicesErrorMessage=error.message;servicesErrorCode=error.code||'';
    }finally{
      if(version===servicesRequestVersion){
        servicesLoading=false;$('services-groups').inert=false;renderServices();renderRefreshState();
      }
    }
  }
  function loadInformation(sameAccount){return Promise.all([loadBill(sameAccount),loadUsage(sameAccount),loadServices(sameAccount)]);}
  async function refresh(){
    if(refreshing){refreshPending=true;return;}
    refreshing=true;informationErrorMessage='';renderRefreshState();
    try{
      const result=await api('/api/bills/accounts'),previousAccount=loginId;
      accounts=result.accounts;months=[...new Set(result.months)].sort().reverse();
      if(!accounts.some(account=>account.id===loginId))loginId=accounts.find(account=>account.status==='active')?.id||accounts[0]?.id||'';
      if(!months.includes(month))month=months.includes(result.default_month)?result.default_month:months[0]||'';
      renderAccounts();renderMonths();await loadInformation(Boolean(loginId&&loginId===previousAccount));
    }catch(error){informationErrorMessage=error.message;$('bills-account-hint').textContent='账号或月份读取失败，请刷新重试。';}
    finally{
      refreshing=false;
      if(refreshPending){refreshPending=false;await refresh();}
      else{
        if(!loginId){renderBill();renderUsage();renderServices();}
        renderRefreshState();
      }
    }
  }
  function changeMonth(value){month=value;$('bills-month').value=month;return loadBill(true);}
  planControl.addEventListener('pointerenter',event=>{if(event.pointerType==='mouse')setPlanOpen(true);});
  planControl.addEventListener('pointerleave',event=>{
    if(event.pointerType==='mouse'&&!planPinned&&!planPopover.contains(document.activeElement))setPlanOpen(false);
  });
  planTrigger.addEventListener('click',()=>{planPinned=!planPinned;setPlanOpen(planPinned);});
  const closePlanOutside=event=>{if(!planPopover.hidden&&!planControl.contains(event.target))setPlanOpen(false);};
  window.addEventListener('pointerdown',closePlanOutside);
  window.addEventListener('focusin',closePlanOutside);
  window.addEventListener('keydown',event=>{
    if(event.key!=='Escape'||planPopover.hidden)return;
    const restoreFocus=planPopover.contains(document.activeElement);
    setPlanOpen(false);
    if(restoreFocus)planTrigger.focus();
  });
  $('bills-refresh').addEventListener('click',refresh);
  $('bills-month').addEventListener('change',()=>changeMonth($('bills-month').value));
  $('bills-previous').addEventListener('click',()=>changeMonth(months[months.indexOf(month)+1]));
  $('bills-next').addEventListener('click',()=>changeMonth(months[months.indexOf(month)-1]));
  $('bills-expand').addEventListener('click',()=>{expandItems(bill.items);(bill.sections||[]).forEach((section,index)=>expandItems(section.items,`section-${index}.`));renderBill();});
  $('bills-collapse').addEventListener('click',()=>{expanded.clear();renderBill();});
  $('bills-rebate-close').addEventListener('click',()=>$('bills-rebate-dialog').close());
  function openAccountManager(){window.dispatchEvent(new CustomEvent('open-account-manager',{detail:{loginId,reloginId:loginId}}));}
  $('bills-relogin').addEventListener('click',openAccountManager);
  $('usage-relogin').addEventListener('click',openAccountManager);
  $('services-relogin').addEventListener('click',openAccountManager);
  $('services-search').addEventListener('input',()=>renderServices(true));
  $('services-cancel-only').addEventListener('change',()=>renderServices(true));
  $('services-sort').addEventListener('change',()=>renderServices());
  $('services-cancel-close').addEventListener('click',closeCancellation);
  $('services-cancel-dismiss').addEventListener('click',closeCancellation);
  $('services-cancel-dialog').addEventListener('cancel',event=>{event.preventDefault();closeCancellation();});
  $('services-cancel-sms').addEventListener('click',sendCancelSms);
  $('services-cancel-submit').addEventListener('click',confirmCancellation);
  $('services-cancel-code').addEventListener('input',renderCancellation);
  window.addEventListener('accounts-changed',refresh);
  refresh();
})();
