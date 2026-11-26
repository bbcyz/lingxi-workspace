'use strict';
const $=id=>document.getElementById(id);
const kindNames={signin:'每日签到',flash:'签到秒杀'};
const flashNames={weekly:'每周秒杀',monthly:'月末秒杀'};
const taskName=task=>flashNames[task.flash_kind]||kindNames[task.kind]||task.kind;
const accountStateNames={login_required:'需重新登录',restoring:'正在恢复',retry:'等待重连'};
const stateNames={waiting:'待执行',preparing:'准备中',running:'执行中',receiving:'接收结果',stopping:'正在停止',success:'已完成',completed:'已完成',signed:'今日已签到',already_signed:'今日已签到',unsigned:'今日未签到',not_signed:'今日未签到',pending:'今日未签到',unknown:'未查询',not_configured:'未配置',skipped:'本次未参与',available:'当前可参与',already_claimed:'本月已领取',unavailable:'暂不可参与',failed:'执行失败',error:'执行失败',cancelled:'已停止',stopped:'已停止',login_required:'需要重新登录',retry:'等待重试',interrupted:'已中断'};
const activeStates=new Set(['preparing','running','receiving','stopping']);
const signedStates=new Set(['signed','already_signed','success','completed']);
let model={accounts:[],tasks:[],products:[]},loaded=false,loading=false,working=false,manual=false,historyId='',historyAccountId='';
let selectedAccountId=sessionStorage.getItem('checkin:selected-account')||'',accountSignature='',refreshPending=false,pendingOfficial='';
let taskSignature='';
const selectedTasks=new Set();
const selectedAccount=()=>model.accounts.find(account=>account.id===selectedAccountId);
window.CheckinAccount={current:selectedAccount,accounts:()=>model.accounts};
const dateFormat=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
function node(tag,className='',text){const element=document.createElement(tag);element.className=className;if(text!==undefined)element.textContent=text;return element;}
function checkinResultMessage(message) {
  const officialLimitMessage='当前参与用户太多啦，请稍后重试';
  return (message || '')
    .replace(/(?:官方(?:未允许领取|暂时未允许此操作|限制了本次请求)|当前参与用户太多啦，请稍后重试)[（(](?:TRAFFIC_LIMITED|GWT_LIMITED)[）)]/g,officialLimitMessage)
    .replace(/\b(?:TRAFFIC_LIMITED|GWT_LIMITED)\b/g,officialLimitMessage);
}
async function api(path,payload){
  const response=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'操作未完成，请重试。');return data;
}
function feedback(message,success=false){$('checkin-feedback').textContent=message;$('checkin-feedback').hidden=!message;$('checkin-feedback').classList.toggle('success',success);}
function badge(state,label=stateNames[state]||state){
  const tone=activeStates.has(state)?'active':['success','completed','signed','already_signed','already_claimed'].includes(state)?'success':['failed','error','login_required','interrupted'].includes(state)?'error':['waiting','retry','available'].includes(state)?'waiting':'ended';
  return node('span','checkin-badge '+tone,label);
}
function stoppable(task){return task.enabled||activeStates.has(task.state);}
function accountTasks(){return model.tasks.filter(task=>task.login_id===selectedAccountId);}
function renderAccountSelector(){
  const signature=JSON.stringify(model.accounts.map(a=>[a.id,a.name,a.phone_masked,a.status]));
  if(signature!==accountSignature){
    accountSignature=signature;
    $('checkin-pool-count').textContent=model.accounts.length;
    $('checkin-account-list').replaceChildren(...(model.accounts.length?model.accounts.map(account=>{
      const button=node('button','workspace-account-jump');button.type='button';button.dataset.checkinAccount=account.id;
      button.append(node('strong','',account.name||account.phone_masked),node('small','',account.phone_masked));
      if(account.status!=='active')button.append(node('small','checkin-account-unavailable',accountStateNames[account.status]||'暂不可用'));
      button.addEventListener('click',()=>selectAccount(account.id));return button;
    }):[node('p','workspace-account-index-empty','请先在账号管理中登录。')]));
    $('checkin-account-select').replaceChildren(...model.accounts.map(account=>{
      const option=node('option','',`${account.name||account.phone_masked} · ${account.phone_masked}${account.status==='active'?'':' · 需登录'}`);
      option.value=account.id;return option;
    }));
  }
  for(const button of $('checkin-account-list').querySelectorAll('button')){
    button.setAttribute('aria-pressed',String(button.dataset.checkinAccount===selectedAccountId));button.disabled=working;
  }
  $('checkin-account-select').value=selectedAccountId;$('checkin-account-select').disabled=working||!model.accounts.length;
}
function selectAccount(id){
  if(working)return;
  selectedAccountId=id;sessionStorage.setItem('checkin:selected-account',id);selectedTasks.clear();
  render();window.dispatchEvent(new CustomEvent('checkin-account-changed',{detail:{id}}));refresh(id);
}
function renderAccount(account, tasks, flashTitle) {
  const summary = $('checkin-accounts');
  summary.hidden = !account;
  $('checkin-account-empty').hidden = !!account;
  $('checkin-open-history').disabled = !account;
  const login = $('checkin-relogin');
  login.hidden = account?.status !== 'login_required';
  if (!account) {
    $('checkin-account-empty').textContent = '请先从“账号管理”登录并选择账号。';
    return;
  }

  summary.dataset.loginId = account.id;
  const todayState = account.today_state || 'unknown';
  const today = badge(signedStates.has(todayState) ? 'signed' : todayState);
  const todayLabel = today.textContent;
  $('checkin-today-state').replaceChildren(today);
  $('checkin-month-count').textContent = account.month_count ?? '—';
  $('checkin-month-unit').hidden = account.month_count == null;

  const lastSignin = $('checkin-last-signin');
  lastSignin.dateTime = account.last_signin_at || '';
  lastSignin.textContent = dateFormat(account.last_signin_at);
  const signinMessage = checkinResultMessage(account.signin_message);
  const signinResult = $('checkin-signin-result');
  signinResult.hidden = !signinMessage || signinMessage === todayLabel;
  signinResult.textContent = signinResult.hidden ? '' : signinMessage;

  $('checkin-account-flash-title').textContent = flashNames[account.flash_kind] || flashTitle;
  const weeklyClaimed = account.flash_state === 'already_claimed' && account.flash_kind === 'weekly';
  const flash = badge(account.flash_state || 'unknown', weeklyClaimed ? '本周已领取' : undefined);
  $('checkin-flash-state').replaceChildren(flash);
  const flashMessage = checkinResultMessage(account.flash_message);
  const flashResult = $('checkin-flash-result');
  flashResult.hidden = !flashMessage || flashMessage === flash.textContent;
  flashResult.textContent = flashResult.hidden ? '' : flashMessage;

  login.dataset.accountRelogin = account.id;
  const nextSession = Date.parse(account.flash_next_at) > Date.parse(model.server_time) ? account.flash_next_at : '';
  const flashTask = tasks.find(task => task.kind === 'flash');
  const nextCheckIsSession = !!nextSession && Date.parse(nextSession) === Date.parse(flashTask?.next_run_at);
  const showNextSession = !!nextSession && !nextCheckIsSession;
  $('checkin-flash-session').hidden = !showNextSession;
  $('checkin-flash-next').dateTime = showNextSession ? nextSession : '';
  $('checkin-flash-next').textContent = showNextSession ? dateFormat(nextSession) : '';

  renderTasks(tasks, {
    signin: [todayLabel, signinMessage],
    flash: [flash.textContent, flashMessage],
  }, nextCheckIsSession);
}

function updateSelection() {
  const available = accountTasks().filter(stoppable);
  const count = selectedTasks.size;
  const selectAll = $('checkin-select-tasks');
  $('checkin-select-all').hidden = available.length < 2;
  selectAll.checked = count > 0 && count === available.length;
  selectAll.indeterminate = count > 0 && count < available.length;
  selectAll.disabled = working || !available.length;
  $('checkin-selection-bar').hidden = !count;
  $('checkin-selected-tasks').textContent = `已选 ${count} 项`;
  $('checkin-stop-tasks').disabled = working || !count;
  $('checkin-clear-tasks').disabled = working;
  for (const check of $('checkin-accounts').querySelectorAll('input[type=checkbox]')) {
    check.checked = selectedTasks.has(check.value);
  }
}

function renderTasks(tasks, officialResults, nextCheckIsSession) {
  const signature = JSON.stringify([selectedAccountId, working, officialResults, nextCheckIsSession, tasks.map(({history, ...task}) => task)]);
  if (signature === taskSignature) return;
  taskSignature = signature;

  for (const kind of ['signin', 'flash']) {
    const card = $('checkin-' + kind + '-task');
    const task = tasks.find(item => item.kind === kind);
    if (!task) {
      card.replaceChildren(node('p', 'checkin-result-note', '未配置自动任务'));
      continue;
    }
    card.dataset.taskId = task.id;
    const name = taskName(task);
    const heading = node('div', 'checkin-automation-heading');
    const title = node('div', 'checkin-auto-state');
    title.append(node('span', '', '自动安排'));
    const isActive = activeStates.has(task.state);
    const isPaused = !isActive && !!task.paused_until;
    const pauseLabel = task.flash_kind === 'weekly' ? '本周暂停' : '本月暂停';
    title.append(isActive ? badge(task.state) : isPaused ? badge('stopped', pauseLabel)
      : badge(task.enabled ? 'waiting' : 'stopped', task.enabled ? '已开启' : '已关闭'));
    heading.append(title);
    const canStop = stoppable(task);
    if (canStop) {
      const label = node('label', 'checkin-task-select');
      const check = node('input');
      check.type = 'checkbox';
      check.value = task.id;
      check.disabled = working;
      check.setAttribute('aria-label', `选择${name}任务`);
      check.addEventListener('change', () => {
        if (check.checked) selectedTasks.add(task.id);
        else selectedTasks.delete(task.id);
        updateSelection();
      });
      label.append(check, '选择');
      heading.append(label);
    }

    const schedule = node('dl', 'checkin-activity-details');
    const nextLabel = isPaused ? '恢复检查' : kind === 'flash' && nextCheckIsSession ? '下场检查' : '下次检查';
    const previousLabel = kind === 'flash' ? '上次检查' : '上次执行';
    const lastState = stateNames[task.state];
    const officialLabels = officialResults[kind];
    for (const [label, value] of [[nextLabel, task.next_run_at], [previousLabel, task.last_run_at]]) {
      if (!value) continue;
      const row = node('div');
      const time = node('time', '', dateFormat(value));
      time.dateTime = value;
      const detail = node('dd');
      detail.append(time);
      if (label === previousLabel && !isActive && lastState &&
          !['waiting', 'stopped', 'already_signed', 'already_claimed'].includes(task.state) &&
          !officialLabels.includes(lastState)) {
        detail.append(node('span', 'checkin-run-state', task.state === 'skipped' ? '已跳过' : lastState));
      }
      row.append(node('dt', '', label), detail);
      schedule.append(row);
    }
    const content = [heading, schedule];
    if (task.kind === 'flash') {
      content.push(node('p', 'checkin-result-note', `最多 ${task.burst_count ?? 40} 次 · 间隔 ${(task.burst_interval ?? 0.05) * 1000} 毫秒`));
    }
    const result = checkinResultMessage(task.message);
    const repeatsOfficialResult = officialLabels.includes(result) ||
      (task.state === 'already_signed' && officialLabels[0] === '今日已签到' && result === '今日已签到，已核对官方记录');
    if (result && !repeatsOfficialResult) content.push(node('p', 'checkin-result-note', result));

    const actions = node('div', 'checkin-row-actions');
    const history = node('button', 'quiet', '记录');
    history.type = 'button';
    history.setAttribute('aria-label', `查看${name}记录`);
    history.addEventListener('click', () => openHistory(task.id));
    actions.append(history);
    if (canStop) {
      const stop = node('button', 'quiet', '停止');
      stop.type = 'button';
      stop.disabled = working;
      stop.setAttribute('aria-label', `停止${name}任务`);
      stop.addEventListener('click', () => stopTasks([task.id]));
      actions.append(stop);
    }
    card.replaceChildren(...content, actions);
  }
}
function render(){
  renderAccountSelector();
  const current=selectedAccount();
  const shown=accountTasks(),available=new Set(shown.filter(stoppable).map(task=>task.id));
  for(const id of selectedTasks)if(!available.has(id))selectedTasks.delete(id);
  $('checkin-sign-now').disabled=working||!current||current.status!=='active'||signedStates.has(current.today_state);
  $('checkin-sign-batch').disabled=working||!model.accounts.some(account=>account.status==='active');
  $('checkin-configure').disabled=working||!loaded;
  $('checkin-dashboard').setAttribute('aria-busy',String(!!current?.refreshing));
  const calibration=model.calibration,clockStatus=$('checkin-clock-status');
  if(clockStatus)clockStatus.textContent=calibration?.state==='ready'
    ? `已统一校时 · 服务器相对本机 ${calibration.offset_ms>=0?'+':''}${calibration.offset_ms.toFixed(1)} 毫秒 · ${calibration.samples} 个有效样本 · 所有账号共用`
    :calibration?.state==='running'?'正在统一校时，所有账号共用同一结果…'
    :calibration?.state==='fallback'?`统一校时未完成，本轮使用本机时间：${calibration.message}`
    :'执行前自动统一校时一次，所有账号共用同一结果。';
  const awardStatus=$('checkin-team-award-status');
  if(awardStatus&&model.team_awards){
    const {checking,claimed,unconfirmed}=model.team_awards;
    awardStatus.textContent=`达标自动领奖 · 本月已领 ${claimed} 个账号`
      +(checking?` · 正在检查 ${checking} 个`:'')
      +(unconfirmed?` · ${unconfirmed} 个待核实`:'');
  }
  const flashKinds=[...new Set(model.accounts.map(account=>account.flash_kind).filter(Boolean))];
  const flashTitle=flashKinds.length===1?flashNames[flashKinds[0]]:'签到秒杀';
  $('checkin-flash-config-title').textContent=flashTitle;
  const coverage=kind=>new Set(model.tasks.filter(task=>task.kind===kind&&task.enabled).map(task=>task.login_id)).size;
  $('checkin-coverage').textContent=`自动任务覆盖范围 · 签到 ${coverage('signin')} 个账号 · 秒杀 ${coverage('flash')} 个账号`;
  renderAccount(current, shown, flashTitle);
  updateSelection();
  if($('checkin-history').open)renderHistory();
}
async function refresh(loginId=''){
  if(loading){refreshPending=true;if(loginId)pendingOfficial=loginId;return loaded;}loading=true;
  try{
    model=await api('/api/checkin/status'+(loginId?'?login_id='+encodeURIComponent(loginId):''));loaded=true;
    const previous=selectedAccountId;
    if(!model.accounts.some(account=>account.id===selectedAccountId))selectedAccountId=(model.accounts.find(account=>account.status==='active')||model.accounts[0])?.id||'';
    sessionStorage.setItem('checkin:selected-account',selectedAccountId);render();
    if(previous!==selectedAccountId){
      selectedTasks.clear();window.dispatchEvent(new CustomEvent('checkin-account-changed',{detail:{id:selectedAccountId}}));
      if(selectedAccountId){refreshPending=true;pendingOfficial=selectedAccountId;}
    }
    $('checkin-warning').textContent=model.warning||'';$('checkin-warning').hidden=!model.warning;
    $('checkin-sync-state').textContent=selectedAccount()?.refreshing?'正在核对当前账号的官方进度…':'北京时间 · 最近同步 '+dateFormat(model.server_time);
    return true;
  }catch(error){
    feedback('读取失败：'+error.message);$('checkin-sync-state').textContent='本地服务暂未连接';
    if(!loaded){
      $('checkin-coverage').textContent='暂无法读取自动任务的账号覆盖范围';
      $('checkin-account-empty').textContent='暂时无法读取，稍后点击“刷新”重试。';
    }
    return false;
  }finally{loading=false;if(refreshPending){const next=pendingOfficial;refreshPending=false;pendingOfficial='';queueMicrotask(()=>refresh(next));}}
}
function updateAccountSelection(){
  const available=[...$('checkin-account-options').querySelectorAll('input:not(:disabled)')],selected=available.filter(input=>input.checked).length;
  $('checkin-account-count').textContent=`已选 ${selected} / ${available.length} 个`;
  $('checkin-select-accounts').textContent=available.length>0&&selected===available.length?'取消全选':'全选可用账号';
  $('checkin-select-accounts').disabled=!available.length;
}
function productLabel(product){return product.label||('消耗：'+(product.cost??'待官方确认'));}
function updateConfiguration(){
  $('checkin-signin-fields').hidden=!$('checkin-signin-enabled').checked;
  $('checkin-flash-fields').hidden=!$('checkin-flash-enabled').checked;
  const product=model.products.find(item=>item.id===$('checkin-product').value);
  $('checkin-product-description').textContent=product?productLabel(product)+(product.available===false?'。当前状态仅供参考，已预约任务仍按设置发送。':''):$('checkin-product').value===model.automation.product_id?'执行时取得当期领取参数，按预约设置发送。':'选择商品后显示其消耗和参与条件。';
}
async function openEditor(asManual=false,loginId='',batch=false){
  if(working||!await refresh())return;
  manual=asManual;$('checkin-form').reset();$('checkin-form-error').hidden=true;
  $('checkin-editor-title').textContent=manual?'立即签到':'配置自动任务';
  if(manual&&!batch)loginId=loginId||selectedAccountId;
  const target=model.accounts.find(account=>account.id===loginId);
  if(manual&&!batch&&target?.status!=='active'){feedback('请先恢复当前账号的登录。');return;}
  $('checkin-editor-description').textContent=manual?(batch?'选择本次需要签到的账号，每个账号今天最多一次。':`本次为 ${target.name||target.phone_masked}（${target.phone_masked}）签到。`):'应用于所有有效账号，新增或恢复登录后自动纳入，无需选择账号。';
  $('checkin-account-heading').hidden=!manual||!batch;$('checkin-account-options').hidden=!manual||!batch;
  $('checkin-automatic-options').hidden=manual;$('checkin-save').textContent=manual?'开始签到':'保存自动任务';
  const options=model.accounts.map(account=>{
    const label=node('label','checkin-account-option'),check=node('input');check.type='checkbox';check.value=account.id;
    check.disabled=account.status!=='active';check.checked=!check.disabled&&(batch||account.id===loginId);
    const text=node('span');text.append(node('strong','',account.name||account.phone_masked),node('small','',account.phone_masked));
    if(check.disabled)text.append(node('small','checkin-account-unavailable',accountStateNames[account.status]||'暂不可用'));
    label.append(check,text);return label;
  });
  if(!options.length)options.push(node('p','checkin-setting-note','共享账号池为空，请先从左侧“账号管理”登录账号。'));
  $('checkin-account-options').replaceChildren(...options);
  const products=[node('option','','请选择商品')];products[0].value='';
  for(const product of model.products){const option=node('option','',`${product.name} · ${productLabel(product)}${product.available===false?'（当前未开放）':''}`);option.value=product.id;option.disabled=product.selectable!==true;products.push(option);}
  $('checkin-product').replaceChildren(...products);
  const flashReady=model.products.some(product=>product.selectable===true);
  const config=model.automation;
  if(!flashReady&&config?.product_id){const option=node('option','','官方秒杀券 · 自动识别场次');option.value=config.product_id;$('checkin-product').append(option);}
  $('checkin-flash-enabled').disabled=!flashReady&&!config?.product_id;$('checkin-flash-unavailable').hidden=flashReady||!!config?.product_id;
  if(!manual){
    $('checkin-signin-enabled').checked=config.signin_enabled;$('checkin-flash-enabled').checked=config.flash_enabled;
    $('checkin-signin-time').value=config.signin_time;
    $('checkin-flash-time').value=config.flash_time??'12:00';
    $('checkin-product').value=config.product_id;
    $('checkin-burst-count').value=config.burst_count;
    $('checkin-burst-interval').value=config.burst_interval*1000;
  }
  updateAccountSelection();updateConfiguration();$('checkin-editor').showModal();
}
async function stopTasks(ids){
  if(working)return;working=true;render();
  try{const result=await api('/api/checkin/tasks/stop',{ids});feedback(result.message||'停止请求已提交，正在执行的结果会保留。',true);await refresh();}
  catch(error){feedback(error.message);}
  finally{working=false;render();}
}
function renderHistory(){
  let records=[];
  if(historyAccountId){
    const account=model.accounts.find(item=>item.id===historyAccountId);
    $('checkin-history-description').textContent=account?(account.name||account.phone_masked)+' · 签到与秒杀记录':'账号已退出共享池';
    records=model.tasks.filter(task=>task.login_id===historyAccountId).flatMap(task=>(task.history||[]).map(record=>({...record,kind:task.kind}))).sort((a,b)=>(b.time||'').localeCompare(a.time||''));
  }else{
    const task=model.tasks.find(item=>item.id===historyId);
    if(!task){$('checkin-history-description').textContent='任务已不存在。';$('checkin-history-body').replaceChildren();return;}
    $('checkin-history-description').textContent=`${task.account_name} · ${kindNames[task.kind]||task.kind}`;
    records=[...(task.history||[])].reverse();
  }
  const rows=records.map(record=>{
    const row=node('article','checkin-history-item'),head=node('div');head.append(node('time','',(record.kind||record.flash_kind?taskName(record)+' · ':'')+dateFormat(record.time)),badge(record.state));row.append(head,node('p','',checkinResultMessage(record.message)));
    if(record.attempts?.length){
      const details=node('details','checkin-attempts'),table=node('table'),thead=node('thead'),titles=node('tr'),body=node('tbody');
      details.append(node('summary','',`查看 ${record.attempt_count} 次请求明细`));
      for(const title of ['请求','实际发起 · 北京时间','结果'])titles.append(node('th','',title));
      thead.append(titles);
      for(const attempt of record.attempts){
        const item=node('tr');item.append(node('td','',attempt.followup?'延后补试':`第 ${attempt.index} 次`),node('td','',attempt.sent_at?dateFormat(attempt.sent_at)+'.'+(attempt.sent_at.match(/\.(\d{3})/)?.[1]||'000'):'未发送'),node('td','',checkinResultMessage(attempt.message)));body.append(item);
      }
      table.append(thead,body);details.append(table);row.append(details);
    }
    return row;
  });
  if(!rows.length)rows.push(node('p','checkin-empty',historyAccountId?'尚无客户端执行记录，最近结果可在签到情况中查看。':'还没有执行记录。'));
  const scroll=$('checkin-history').scrollTop;$('checkin-history-body').replaceChildren(...rows);$('checkin-history').scrollTop=scroll;
}
function openHistory(id){historyAccountId='';historyId=id;renderHistory();$('checkin-history').showModal();}
function openAccountHistory(id){historyId='';historyAccountId=id;renderHistory();$('checkin-history').showModal();}
$('checkin-configure').disabled=true;$('checkin-sign-now').disabled=true;
$('checkin-refresh').addEventListener('click',()=>refresh(selectedAccountId));
$('checkin-configure').addEventListener('click',()=>openEditor());$('checkin-sign-now').addEventListener('click',()=>openEditor(true,selectedAccountId));
$('checkin-sign-batch').addEventListener('click',()=>openEditor(true,'',true));
$('checkin-open-history').addEventListener('click',()=>openAccountHistory(selectedAccountId));
$('checkin-account-select').addEventListener('change',event=>selectAccount(event.target.value));
$('checkin-account-options').addEventListener('change',updateAccountSelection);
$('checkin-select-accounts').addEventListener('click',()=>{const options=[...$('checkin-account-options').querySelectorAll('input:not(:disabled)')],check=!options.every(input=>input.checked);options.forEach(input=>input.checked=check);updateAccountSelection();});
for(const id of ['checkin-signin-enabled','checkin-flash-enabled','checkin-product'])$(id).addEventListener('change',updateConfiguration);
$('checkin-form').addEventListener('submit',async event=>{
  event.preventDefault();if(working)return;
  try{
    const loginIds=[...$('checkin-account-options').querySelectorAll('input:checked')].map(input=>input.value);
    if(manual&&!loginIds.length)throw new Error('请选择至少一个可用账号。');
    const signin=$('checkin-signin-enabled').checked,flash=$('checkin-flash-enabled').checked,productId=$('checkin-product').value;
    if(!manual&&!signin&&!flash)throw new Error('请至少启用一项自动任务。');
    if(!manual&&flash&&productId!==model.automation.product_id)throw new Error('请选择已确认可参与的秒杀商品。');
    working=true;$('checkin-save').disabled=true;$('checkin-form-error').hidden=true;
    const result=await api(manual?'/api/checkin/run':'/api/checkin/tasks/save',manual?{login_ids:loginIds,kind:'signin'}:{signin_enabled:signin,signin_time:$('checkin-signin-time').value,flash_enabled:flash,flash_time:$('checkin-flash-time').value,product_id:productId,burst_count:Number($('checkin-burst-count').value),burst_interval:Number($('checkin-burst-interval').value)/1000});
    $('checkin-editor').close();feedback(result.message||(manual?'签到请求已完成，请查看各账号执行记录。':'自动任务已保存。'),true);await refresh();
  }catch(error){$('checkin-form-error').textContent=error.message;$('checkin-form-error').hidden=false;}
  finally{working=false;$('checkin-save').disabled=false;render();}
});
$('checkin-editor').addEventListener('cancel',event=>{if(working)event.preventDefault();});
document.querySelectorAll('[data-checkin-close]').forEach(button=>button.addEventListener('click',()=>{if(!working)$(button.dataset.checkinClose).close();}));
$('checkin-select-tasks').addEventListener('change',event=>{
  selectedTasks.clear();
  if(event.target.checked){
    for(const task of accountTasks().filter(stoppable))selectedTasks.add(task.id);
  }
  updateSelection();
});
$('checkin-clear-tasks').addEventListener('click',()=>{selectedTasks.clear();updateSelection();});
$('checkin-stop-tasks').addEventListener('click',()=>stopTasks([...selectedTasks]));
window.addEventListener('accounts-changed',()=>refresh(selectedAccountId));window.addEventListener('focus',()=>refresh());
refresh(selectedAccountId);setInterval(()=>{if(!document.hidden&&!working)refresh();},5000);
