'use strict';

(() => {
  const dialog = $('checkin-beans');
  const accountLabel = $('checkin-beans-account-name');
  const refreshButton = $('checkin-beans-refresh');
  const endpoint = '/api/checkin/beans';
  const views = [...dialog.querySelectorAll('[data-beans-view]')];
  const filters = [...dialog.querySelectorAll('[data-beans-filter]')];
  const labels = {
    unfinished: '待完成', claimable: '待领取', claimed: '已领取',
    before_open: '08:00 开放', sold_out: '今日已兑完', insufficient: 'AI 豆不足',
    available: '可兑换', processing: '兑换处理中', redeemed: '已兑换', unknown: '待官方确认',
  };
  const expanded = new Set();
  let state = null;
  let renderedState;
  let selectedPrize = null;
  let view = 'tasks';
  let filter = 'automatic';
  let busy = false;
  let stale = false;
  let accountsChanged = false;
  let refreshPending = false;
  let polling = false;
  let enqueueing = false;
  let currentRunning = false;
  let progressDate;
  let accountId = '';

  function syncAccount() {
    const current = window.CheckinAccount.current();
    const next = current?.id || '';
    if (next !== accountId) {
      accountId = next;
      state = null; selectedPrize = null; currentRunning = false; stale = false;
      expanded.clear(); progressDate = undefined;
    }
    accountLabel.textContent = current ? (current.name && !current.phone_masked.endsWith(current.name)
      ? `${current.name} · ${current.phone_masked}` : current.phone_masked) : '请先选择账号';
  }

  function matches(task, key) {
    if (key === 'automatic') return task.automatic || task.state === 'claimable';
    if (key === 'unsupported') return !task.automatic && task.state === 'unfinished';
    return key === 'all' || task.state === key;
  }

  function statusLabel(item) {
    return node('span', `checkin-beans-state ${item.state}`, labels[item.state] || labels.unknown);
  }

  function actionButton(text, action) {
    const button = node('button', 'primary', text);
    button.type = 'button';
    button.dataset.beansAction = 'true';
    button.addEventListener('click', action);
    return button;
  }

  function emptyMessage(title, description) {
    const message = node('div', 'checkin-beans-empty');
    message.append(node('strong', '', title), node('p', '', description));
    return message;
  }

  function renderTasks() {
    const query = $('checkin-beans-search').value.trim().toLocaleLowerCase();
    const tasks = state ? state.tasks.filter(task => matches(task, filter)
      && task.name.toLocaleLowerCase().includes(query)) : [];
    const groups = new Map();
    const rank = task => task.state === 'claimable' ? 0 : task.state === 'claimed' ? 2 : 1;
    for (const task of [...tasks].sort((a, b) => rank(a) - rank(b))) {
      const name = task.group || '签到任务';
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(task);
    }
    const sections = [];
    for (const [name, items] of groups) {
      const section = node('section', 'checkin-beans-task-group');
      const heading = node('h4', '', name);
      heading.append(node('span', '', `${items.length} 项`));
      const list = node('ul', 'checkin-beans-task-grid');
      for (const task of items) {
        const card = node('li', 'checkin-beans-task-card');
        card.dataset.state = task.state;
        const title = node('div', 'checkin-beans-task-title');
        const reward = node('span', 'checkin-beans-reward', `+${task.reward}`);
        reward.append(node('small', '', '豆'));
        title.append(node('h5', '', task.name), reward);
        const meta = node('div', 'checkin-beans-task-meta');
        meta.append(statusLabel(task));
        if (task.state === 'claimable') {
          meta.append(actionButton('领取奖励', () => load({operation: {kind: 'claim', item_id: task.id}})));
        } else {
          const progress = task.progress != null && task.target ? ` · ${task.progress}/${task.target}` : '';
          meta.append(node('span', '', `${task.automatic ? '自动任务' : '未接入自动执行'}${progress}`));
        }
        card.append(title, meta);
        if (task.partner_service && task.state === 'unfinished' && $('checkin-partners-open')) {
          const login = node('button', 'secondary', task.partner_login_required ? '更新合作方登录' : '合作方登录');
          login.type = 'button';
          login.addEventListener('click', () => window.dispatchEvent(new CustomEvent('partner-login-request', {detail: {service: task.partner_service}})));
          meta.append(login);
        }
        if (task.reason || task.requirement) {
          const detail = node('details', 'checkin-beans-task-detail');
          detail.open = expanded.has(task.id);
          detail.append(node('summary', '', !task.automatic && task.state === 'unfinished' ? '未接通原因' : '任务说明'));
          if (task.requirement) detail.append(node('p', '', task.requirement));
          if (task.reason) detail.append(node('p', '', task.reason));
          detail.addEventListener('toggle', () => {
            if (detail.open) expanded.add(task.id);
            else expanded.delete(task.id);
          });
          card.append(detail);
        }
        list.append(card);
      }
      section.append(heading, list);
      sections.push(section);
    }
    const empty = !state ? ['暂无账号数据', '选择有效账号后刷新，查看官方任务记录。']
      : query ? ['没有找到匹配的任务', '试试其他名称，或清空搜索条件。']
      : filter === 'claimable' ? ['暂无待领取奖励', '已确认可领取的奖励也会由自动任务领取。']
      : filter === 'unsupported' ? ['没有待接通任务', '可在其他分类中查看任务记录。']
      : ['当前分类暂无任务', '切换到「全部」查看官方返回的完整任务列表。'];
    $('checkin-beans-task-list').replaceChildren(...(sections.length ? sections : [emptyMessage(...empty)]));
    $('checkin-beans-filter-summary').textContent = state ? `显示 ${tasks.length} 项${query ? '匹配的' : ''}任务` : '';
  }

  function renderPrizes() {
    const cards = (state?.prizes || []).map(prize => {
      const card = node('li', 'checkin-beans-prize-card');
      card.dataset.prizeId = prize.id;
      const cost = node('p', 'checkin-beans-prize-cost');
      cost.append(node('strong', '', prize.cost), node('span', '', 'AI 豆兑换'));
      const bottom = node('div', 'checkin-beans-prize-bottom');
      bottom.append(statusLabel(prize));
      if (prize.state === 'available') {
        bottom.append(actionButton('立即兑换', () => {
          selectedPrize = prize;
          render();
          $('checkin-beans-confirm').scrollIntoView({block: 'nearest', behavior: 'smooth'});
          $('checkin-beans-cancel').focus({preventScroll: true});
        }));
      } else if (prize.state === 'redeemed') {
        const link = node('a', 'secondary', '查看卡券');
        link.href = '/coupons';
        bottom.append(link);
      }
      card.append(node('span', 'checkin-beans-ticket-label', '话费好礼'), node('h4', '', prize.name), cost, bottom);
      return card;
    });
    $('checkin-beans-prize-list').replaceChildren(...(cards.length ? cards : [emptyMessage('暂无可兑换的话费券', '奖品与库存以官方当前返回为准。')]));
  }

  function renderControls() {
    dialog.dataset.busy = String(busy);
    dialog.dataset.stale = String(stale);
    $('checkin-beans-content').setAttribute('aria-busy', String(busy));
    refreshButton.disabled = busy || enqueueing || !accountId;
    const blocked = busy || enqueueing || stale || !state || state.pending || window.CheckinAccount.current()?.status !== 'active';
    $('checkin-beans-run').disabled = blocked || currentRunning || !accountId;
    $('checkin-beans-run').textContent = currentRunning ? '任务执行中…' : '执行当前账号';
    $('checkin-beans-run-all').disabled = busy || enqueueing || !window.CheckinAccount.accounts().some(a => a.status === 'active');
    $('checkin-beans-redeem').disabled = blocked;
    $('checkin-beans-go-prizes').disabled = !state;
    $('checkin-beans-search').disabled = !state;
    for (const button of dialog.querySelectorAll('[data-beans-action]')) {
      button.disabled = blocked || state.tasks_available === false && button.closest('.checkin-beans-task-card') != null;
    }
    for (const button of [...views, ...filters]) button.disabled = !state;
  }

  function render() {
    if (state !== renderedState) {
      renderedState = state;
      $('checkin-beans-balance').textContent = state?.balance ?? '—';
      $('checkin-beans-earned').textContent = state?.earned ?? '—';
      $('checkin-beans-spent').textContent = state?.spent ?? '—';
      $('checkin-beans-task-total').textContent = state?.tasks.length ?? '—';
      $('checkin-beans-prize-total').textContent = state?.prizes.length ?? '—';
      const claimed = state?.tasks.filter(task => task.state === 'claimed').length || 0;
      const claimable = state?.tasks.filter(task => task.state === 'claimable').length || 0;
      $('checkin-beans-progress-count').textContent = state ? `${claimed} / ${state.tasks.length}` : '—';
      $('checkin-beans-progress-text').textContent = state ? `已领取 ${claimed} 项 · 待领取 ${claimable} 项` : '等待查询官方记录';
      $('checkin-beans-progress').max = state?.tasks.length || 1;
      $('checkin-beans-progress').value = claimed;
      for (const button of filters) {
        button.querySelector('span').textContent = state ? state.tasks.filter(task => matches(task, button.dataset.beansFilter)).length : '—';
      }
      renderTasks();
      renderPrizes();
    }
    $('checkin-beans-placeholder').hidden = !!state || !busy;
    $('checkin-beans-tasks').hidden = view !== 'tasks' || !state && busy;
    $('checkin-beans-prizes').hidden = view !== 'prizes' || !state && busy;
    $('checkin-beans-confirm').hidden = !selectedPrize;
    if (selectedPrize) {
      $('checkin-beans-confirm-text').textContent = `${accountLabel.textContent} 将消耗 ${selectedPrize.cost} AI 豆兑换「${selectedPrize.name}」。`;
    }
    for (const card of dialog.querySelectorAll('[data-prize-id]')) card.dataset.selected = String(card.dataset.prizeId === selectedPrize?.id);
    renderControls();
  }

  async function refreshExecution() {
    if (polling || !dialog.open) return;
    polling = true;
    const loginId = accountId;
    try {
      const progress = await api(endpoint + '/tasks');
      if (loginId !== accountId || !dialog.open) return;
      const current = progress.records[loginId];
      currentRunning = progress.running.includes(loginId);
      const status = $('checkin-beans-run-status');
      status.dataset.running = String(currentRunning);
      status.textContent = currentRunning ? current?.message || '正在执行并核对官方结果…'
        : current ? current.message + (current.balance != null ? ` · 上次核对 ${current.balance} AI 豆` : '') : '等待今日自动检查';
      renderControls();
      const nextDate = current?.checked_at ?? null;
      const changed = progressDate !== undefined && progressDate !== nextDate;
      progressDate = nextDate;
      if (!currentRunning && changed) {
        if (busy) refreshPending = true;
        else load();
      }
    } catch (error) {
      if (loginId === accountId) $('checkin-beans-run-status').textContent = error.message;
    } finally {
      polling = false;
    }
  }

  async function load({syncAccounts = false, operation = null} = {}) {
    if (syncAccounts) syncAccount();
    if (busy) {
      accountsChanged ||= syncAccounts;
      refreshPending ||= !syncAccounts;
      return;
    }
    const status = $('checkin-beans-status');
    const loginId = accountId;
    selectedPrize = null;
    busy = true;
    status.textContent = operation ? '正在提交并核对官方结果…' : state ? '正在更新官方状态，保留上次数据…' : '正在查询官方状态…';
    render();
    try {
      if (!loginId) {
        state = null;
        status.textContent = '暂无有效账号，请先到「账号管理」登录共享账号。';
        return;
      }
      const result = operation ? await api(endpoint, {login_id: loginId, ...operation})
        : await api(`${endpoint}?login_id=${encodeURIComponent(loginId)}`);
      if (loginId !== accountId) return;
      state = result;
      stale = false;
      status.textContent = state.message || state.task_warning || (state.pending
        ? '上次操作结果待核实，请刷新查询。' : `官方状态 · ${dateFormat(state.server_time)}`);
    } catch (error) {
      if (loginId !== accountId) return;
      stale = true;
      status.textContent = error.message;
    } finally {
      busy = false;
      render();
      if ((accountsChanged || refreshPending) && dialog.open) {
        const syncAccounts = accountsChanged;
        accountsChanged = refreshPending = false;
        load({syncAccounts});
      }
    }
  }

  function setView(next) {
    view = next;
    for (const button of views) {
      const selected = button.dataset.beansView === view;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    }
    render();
  }

  async function execute(all) {
    if (busy || enqueueing) return;
    enqueueing = true;
    renderControls();
    try {
      const result = await api(endpoint + '/tasks', all ? {} : {login_id: accountId});
      $('checkin-beans-run-status').textContent = result.queued ? `已安排 ${result.queued} 个账号执行，正在核对进度…` : '任务正在执行或账号暂不可用';
      refreshExecution();
    } catch (error) {
      $('checkin-beans-run-status').textContent = error.message;
    } finally {
      enqueueing = false;
      renderControls();
    }
  }

  $('checkin-beans-open').addEventListener('click', () => {
    syncAccount();
    dialog.showModal();
    accountsChanged = refreshPending = false;
    progressDate = undefined;
    load({syncAccounts: true});
    refreshExecution();
  });
  refreshButton.addEventListener('click', () => load({syncAccounts: true}));
  $('checkin-beans-run').addEventListener('click', () => execute(false));
  $('checkin-beans-run-all').addEventListener('click', () => execute(true));
  $('checkin-beans-cancel').addEventListener('click', () => { selectedPrize = null; render(); });
  $('checkin-beans-redeem').addEventListener('click', () => {
    if (!selectedPrize) return;
    const {id, cost} = selectedPrize;
    load({operation: {kind: 'redeem', item_id: id, cost}});
  });
  $('checkin-beans-go-prizes').addEventListener('click', () => setView('prizes'));
  for (const button of views) button.addEventListener('click', () => setView(button.dataset.beansView));
  for (const button of filters) button.addEventListener('click', () => {
    filter = button.dataset.beansFilter;
    for (const item of filters) {
      item.classList.toggle('selected', item === button);
      item.setAttribute('aria-pressed', String(item === button));
    }
    renderTasks();
    renderControls();
  });
  $('checkin-beans-search').addEventListener('input', () => { renderTasks(); renderControls(); });
  window.addEventListener('accounts-changed', () => { if (dialog.open) load({syncAccounts: true}); });
  window.addEventListener('checkin-account-changed', () => {
    syncAccount(); render();
    if (dialog.open) { load({syncAccounts: true}); refreshExecution(); }
  });
  window.addEventListener('partner-logins-updated', event => {
    if (dialog.open && event.detail.loginId === accountId) { load({syncAccounts:true}); refreshExecution(); }
  });
  setInterval(refreshExecution, 3000);
})();
