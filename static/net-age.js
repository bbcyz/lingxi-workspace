(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const accountList = $('age-accounts');
  const dialog = $('age-confirm');
  const rows = new Map([...document.querySelectorAll('[data-age-group]')].map(row => [row.dataset.ageGroup, row]));
  const accountStates = {active:'已登录', login_required:'需重新登录', restoring:'正在恢复', retry:'等待重连'};
  let sharedAccounts = [], currentAccount, needsRelogin = false;
  let model, sevenDay, selected, operation, queryVersion = 0, accountVersion = 0, submitting = false, drawTimer;

  async function api(path, values) {
    const response = await fetch(path, values && {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(values)});
    if (response.status === 404 && path === '/api/seven-day/status') return null;
    const data = await response.json();
    if (!response.ok) throw Object.assign(new Error(data.error || '本地服务请求失败'), {code:data.code});
    return data;
  }

  function message(id, value) {
    $(id).textContent = value;
    $(id).hidden = !value;
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  }

  function accountLabel(account) {
    return account.name && account.name !== account.phone_masked ? `${account.name} · ${account.phone_masked}` : account.phone_masked;
  }

  function renderAccounts() {
    $('age-account-count').textContent = sharedAccounts.length;
    message('age-account-empty', sharedAccounts.length ? '' : '请在账号管理中添加共享账号。');
    accountList.replaceChildren(...sharedAccounts.map(account => {
      const active = account.id === currentAccount?.id;
      const card = node('button', 'coupons-account' + (active ? ' selected' : ''));
      card.type = 'button';
      card.dataset.loginId = account.id;
      card.setAttribute('aria-pressed', String(active));
      const label = node('span', 'coupons-account-label');
      label.append(node('strong', '', account.name || account.phone_masked),
        node('span', 'coupons-account-state ' + (account.status === 'active' ? 'active' : 'attention'),
          accountStates[account.status] || '暂不可用'));
      card.append(label, node('small', '', account.phone_masked));
      card.addEventListener('click', () => {
        currentAccount = account;
        for (const other of accountList.children) {
          const pressed = other === card;
          other.classList.toggle('selected', pressed);
          other.setAttribute('aria-pressed', String(pressed));
        }
        return loadStatus();
      });
      return card;
    }));
  }

  function action(area, text, handler, disabled = false) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'secondary'; button.textContent = text; button.disabled = disabled;
    button.addEventListener('click', handler);
    area.append(button);
  }

  function confirm(group, offers) {
    selected = {id:currentAccount.id, group};
    operation = null;
    $('age-confirm-title').textContent = group === 'seven' ? '确认参加今日抽奖' : rows.get(group).querySelector('h3').textContent;
    $('age-confirm-account').textContent = accountLabel(currentAccount);
    const choices = $('age-options'); choices.replaceChildren();
    choices.hidden = false;
    $('age-verification').hidden = true;
    $('age-code').value = ''; $('age-code').required = false;
    $('age-slider-panel').hidden = true;
    $('age-challenge').disabled = false; $('age-slider-submit').disabled = false;
    offers.forEach((offer, index) => {
      const label = document.createElement('label'), input = document.createElement('input');
      input.type = 'radio'; input.name = 'age-choice'; input.value = offer.id; input.required = true; input.checked = index === 0;
      label.append(input, offer.name); choices.append(label);
    });
    $('age-submit').textContent = group === 'seven' ? '确认抽奖' : '确认领取';
    message('age-confirm-note', '');
    dialog.showModal();
  }

  function render() {
    $('age-current-account').textContent = currentAccount ? accountLabel(currentAccount) : '尚未选择账号';
    message('age-profile', model ? [model.age, model.level, model.birthday && '网龄生日 '+model.birthday].filter(Boolean).join(' · ') : '');
    const relogin = $('age-relogin');
    relogin.hidden = !currentAccount || (currentAccount.status !== 'login_required' && !needsRelogin);
    relogin.dataset.accountRelogin = currentAccount?.id || '';
    const unavailable = !currentAccount ? '待选账号' : needsRelogin ? '需重新登录'
      : currentAccount.status !== 'active' ? accountStates[currentAccount.status] || '暂不可用' : '';
    for (const [group, row] of rows) {
      const status = row.querySelector('[data-age-status]');
      const detail = row.querySelector('[data-age-detail]');
      const area = row.querySelector('[data-age-actions]');
      area.replaceChildren();
      if (unavailable) {
        status.textContent = unavailable;
        detail.textContent = '';
        continue;
      }
      if (group === 'seven') {
        const account = sevenDay?.accounts.find(item => item.id === currentAccount.id);
        const value = account?.observation;
        if (sevenDay) {
          const link = document.createElement('a'); link.href = '/seven-day'; link.textContent = '打卡详情'; area.append(link);
        }
        status.textContent = sevenDay === undefined ? '待查询' : !sevenDay ? '活动未启用' : !sevenDay.campaign.active ? (sevenDay.campaign.ended ? '活动已结束' : '活动未开放') : account?.running ? '正在核对'
          : value?.chances === null || value?.chances === undefined ? '次数待查询' : value.chances ? '今日可抽 '+value.chances+' 次' : '今日无剩余次数';
        detail.textContent = account?.observation?.draw_prize || account?.message || '每个符合资格的账号每日一次';
        if (sevenDay?.campaign.active && account?.status === 'active') {
          action(area, '查询次数', () => sevenRun('refresh'), account.running || submitting);
          action(area, '抽奖', () => confirm(group, []), !value?.chances || account.running || submitting);
        }
        continue;
      }
      const value = model?.rows[group];
      const offers = value?.offers || [];
      const received = offers.filter(offer => offer.received).map(offer => offer.name);
      const choices = offers.filter(offer => !offer.received);
      status.textContent = value?.status || '待查询';
      detail.textContent = [value?.description, received.length ? '已领 '+received.join('、') : ''].filter(Boolean).join(' · ');
      if (value?.available && choices.length) action(area, '选择领取', () => confirm(group, choices));
    }
  }

  async function loadStatus() {
    const account = currentAccount, version = ++queryVersion;
    model = null; sevenDay = undefined; needsRelogin = false;
    clearTimeout(drawTimer);
    render();
    if (account?.status !== 'active') {
      message('age-feedback', '');
      return;
    }
    const {id} = account;
    message('age-feedback', '正在查询官方权益…');
    const campaignQuery = api('/api/seven-day/status').then(campaign => {
      if (version === queryVersion) {
        sevenDay = campaign;
        render();
        if (campaign?.campaign.active) {
          const account = campaign.accounts.find(item => item.id === id);
          if (account?.running) pollSeven(id, false);
          else if (account?.status === 'active') sevenRun('refresh', id, false);
        }
      }
    });
    const [age] = await Promise.allSettled([api('/api/net-age/status?id='+encodeURIComponent(id)), campaignQuery]);
    if (version !== queryVersion) return;
    model = age.status === 'fulfilled' ? age.value : null;
    needsRelogin = age.status === 'rejected' && ['APP_LOGIN_REQUIRED','ACTIVITY_LOGIN_REQUIRED'].includes(age.reason.code);
    message('age-feedback', age.status === 'rejected' ? age.reason.message : '');
    render();
  }

  async function loadAccounts() {
    const version = ++accountVersion;
    try {
      const data = await api('/api/net-age/accounts');
      if (version !== accountVersion) return;
      const previous = currentAccount?.id;
      sharedAccounts = data.accounts;
      currentAccount = sharedAccounts.find(account => account.id === previous)
        || sharedAccounts.find(account => account.status === 'active') || sharedAccounts[0];
      renderAccounts();
      await loadStatus();
    } catch (error) {
      if (version !== accountVersion) return;
      if (!sharedAccounts.length) message('age-account-empty', '账号读取失败，请刷新重试。');
      message('age-feedback', error.message);
    }
  }

  async function pollSeven(id, announce = true, version = queryVersion) {
    if (version !== queryVersion || currentAccount?.id !== id) return;
    clearTimeout(drawTimer);
    try {
      const result = await api('/api/seven-day/status');
      if (version !== queryVersion || currentAccount?.id !== id) return;
      sevenDay = result; render();
      if (sevenDay?.accounts.find(account => account.id === id)?.running) {
        drawTimer = setTimeout(() => pollSeven(id, announce, version), 1200);
      }
    } catch (error) { if (announce && version === queryVersion) message('age-feedback', error.message); }
  }

  async function sevenRun(kind, id = currentAccount.id, announce = true) {
    const version = queryVersion;
    try {
      const data = await api('/api/seven-day/run', {id, kind});
      if (version !== queryVersion || currentAccount?.id !== id) return;
      if (announce) message('age-feedback', data.message);
      await pollSeven(id, announce, version);
    } catch (error) { if (announce && version === queryVersion) message('age-feedback', error.message); }
  }

  async function verify(action, value = {}) {
    return api('/api/net-age/verification/'+action, {id:selected.id, operation:operation.operation, ...value});
  }

  function setBusy(busy) {
    submitting = busy;
    $('age-submit').disabled = busy || !!(operation && !operation.sms);
    $('age-send-sms').disabled = busy || !!(operation?.needs_slider && !operation.slider);
  }

  async function verificationStep(action, value, done) {
    if (submitting) return;
    setBusy(true);
    message('age-confirm-note', '');
    try { done(await verify(action, value)); }
    catch (error) { message('age-confirm-note', error.message); }
    finally { setBusy(false); }
  }

  function showVerification(data) {
    operation = data;
    $('age-options').hidden = true;
    $('age-options').replaceChildren();
    $('age-confirm-title').textContent = data.offer;
    $('age-verification').hidden = false;
    $('age-slider-section').hidden = !data.needs_slider;
    $('age-code').required = true;
    message('age-confirm-note', data.notice || '');
  }

  $('age-challenge').addEventListener('click', () => verificationStep('challenge', {}, data => {
    const ground = $('age-slider-ground'), piece = $('age-slider-piece');
    $('age-slider-position').value = '0'; $('age-slider-piece').style.left = '0';
    ground.onload = () => { piece.style.top = (data.ordinate / ground.naturalHeight * 100)+'%'; };
    piece.onload = () => {
      piece.style.width = (piece.naturalWidth / 360 * 100)+'%';
      $('age-slider-position').max = 360 - piece.naturalWidth;
    };
    ground.src = 'data:image/png;base64,'+data.ground;
    piece.src = 'data:image/png;base64,'+data.patch;
    $('age-slider-panel').hidden = false;
    $('age-slider-submit').disabled = false;
    operation.slider = false;
  }));
  $('age-slider-position').addEventListener('input', event => {
    $('age-slider-piece').style.left = (Number(event.target.value) / 360 * 100)+'%';
  });
  $('age-slider-submit').addEventListener('click', () => {
    $('age-slider-submit').disabled = true;
    verificationStep('slider', {position:Number($('age-slider-position').value)}, data => {
      operation.slider = true;
      $('age-slider-panel').hidden = true;
      $('age-challenge').disabled = true;
      message('age-confirm-note', data.message);
    });
  });
  $('age-send-sms').addEventListener('click', () => verificationStep('sms', {}, data => {
    operation.sms = true;
    message('age-confirm-note', data.message);
    $('age-code').focus();
  }));

  $('age-confirm-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting) return;
    setBusy(true);
    try {
      if (selected.group === 'seven') {
        dialog.close(); await sevenRun('draw', selected.id);
      } else {
        const data = operation ? await verify('submit', {code:$('age-code').value})
          : await api('/api/net-age/claim', {...selected, choice:new FormData(event.currentTarget).get('age-choice')});
        if (data.operation) { showVerification(data); return; }
        operation = null;
        dialog.close();
        if (selected.id === currentAccount?.id) {
          await loadStatus();
          if (selected.id === currentAccount?.id) message('age-feedback', data.message);
        }
      }
    } catch (error) { message('age-confirm-note', error.message); }
    finally { setBusy(false); render(); }
  });
  $('age-cancel').addEventListener('click', () => { if (!submitting) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (submitting) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    if (operation) verify('cancel').catch(error => message('age-feedback', error.message));
    operation = null;
  });
  $('age-refresh').addEventListener('click', loadAccounts);
  window.addEventListener('accounts-changed', loadAccounts);
  loadAccounts();
})();
