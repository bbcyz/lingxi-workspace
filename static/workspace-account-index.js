(() => {
  'use strict';

  for (const rail of document.querySelectorAll('[data-workspace-account-table]')) {
    const table = document.getElementById(rail.dataset.workspaceAccountTable);
    const list = rail.querySelector('[data-account-links]');
    const count = rail.querySelector('[data-account-count]');
    const cells = () => [...table.querySelectorAll('[data-workspace-account]')];
    let signature;

    function sync() {
      const accounts = new Map();
      for (const cell of cells()) {
        const id = cell.dataset.workspaceAccount;
        if (accounts.has(id)) continue;
        accounts.set(id, [cell.querySelector('strong').textContent, cell.dataset.workspacePhone]);
      }
      const next = JSON.stringify([...accounts]);
      if (next === signature) return;
      signature = next;
      count.textContent = accounts.size;
      const buttons = [...accounts].map(([id, [name, phone]]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'workspace-account-jump';
        button.dataset.accountJump = id;
        const label = document.createElement('strong');
        label.textContent = name;
        button.append(label);
        if (phone && phone !== name) {
          const number = document.createElement('small');
          number.textContent = phone;
          button.append(number);
        }
        return button;
      });
      if (!buttons.length) {
        const empty = document.createElement('p');
        empty.className = 'workspace-account-index-empty';
        empty.textContent = '当前没有账号记录。';
        buttons.push(empty);
      }
      list.replaceChildren(...buttons);
    }

    list.addEventListener('click', event => {
      const button = event.target.closest('[data-account-jump]');
      if (!button) return;
      if (rail.dataset.accountView && table.closest('[hidden]')) {
        document.getElementById(rail.dataset.accountView).click();
      }
      const cell = cells().find(item => item.dataset.workspaceAccount === button.dataset.accountJump);
      const row = cell?.closest('tr');
      if (!row) return;
      row.classList.add('workspace-account-target');
      row.tabIndex = -1;
      row.scrollIntoView({block: 'nearest', inline: 'nearest'});
      row.focus({preventScroll: true});
    });

    new MutationObserver(sync).observe(table, {childList: true});
    sync();
  }
})();
