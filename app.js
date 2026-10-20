'use strict';

// Preserve form input on validation/network failure. A request id stays unchanged
// until a successful navigation, including retries after an uncertain response.
const menuButton = document.querySelector('.mobile-menu');
menuButton?.addEventListener('click', () => {
  const expanded = menuButton.getAttribute('aria-expanded') !== 'true';
  menuButton.setAttribute('aria-expanded', String(expanded));
  document.querySelector('.sidebar')?.classList.toggle('menu-open', expanded);
});

const presetNode = document.getElementById('operation-preset');
const operationPreset = presetNode ? JSON.parse(presetNode.textContent) : null;
const tabs = [...document.querySelectorAll('.operation-tab')];
function showOperation(op) {
  if (!tabs.some(tab => tab.dataset.op === op)) return;
  document.querySelectorAll('.operation-panel').forEach(panel => { panel.hidden = panel.dataset.operation !== op; });
  tabs.forEach(tab => {
    const active = tab.dataset.op === op;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-current', active ? 'page' : 'false');
  });
}
tabs.forEach(tab => tab.addEventListener('click', event => {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  showOperation(tab.dataset.op);
  history.pushState({}, '', tab.href);
}));
window.addEventListener('popstate', () => showOperation(new URL(location.href).searchParams.get('op') || 'receipt'));

if (operationPreset?.product && ['receipt','issue','writeoff'].includes(operationPreset.op)) {
  const {product, op} = operationPreset;
  fetch('/api/products?' + new URLSearchParams({q: product.code, mode: op}))
    .then(response => { if (!response.ok) throw new Error(); return response.json(); })
    .then(items => {
      const item = items.find(item => item.id === product.id);
      if (item) window.productPickers?.[op]?.(item);
      else formError(document.getElementById(op + '-form'), 'Позиция недоступна для этой операции. Проверьте остаток.');
    })
    .catch(() => formError(document.getElementById(op + '-form'), 'Не удалось загрузить выбранную позицию. Повторите поиск.'));
}

// Going back from a product restores the actual filtered catalog URL.
try {
  if (location.pathname === '/catalog') sessionStorage.setItem('catalogURL', location.pathname + location.search);
  const previous = sessionStorage.getItem('catalogURL');
  if (previous?.startsWith('/catalog')) document.querySelectorAll('.catalog-back').forEach(link => { link.href = previous; });
} catch (_) { /* Storage can be unavailable in private browser configurations. */ }

function formError(form, message) {
  if (!form) return;
  let box = form.querySelector('.form-error');
  if (!box) {
    box = document.createElement('div');
    box.className = 'form-error';
    box.setAttribute('role', 'alert');
    box.tabIndex = -1;
    form.prepend(box);
  }
  box.textContent = message;
  box.hidden = false;
  box.focus();
  box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}
const valueOf = (form, name) => form.elements.namedItem(name)?.value || '';
const labelOf = (form, name) => form.elements.namedItem(name)?.selectedOptions?.[0]?.textContent || '';
const actionNames = {
  '/transaction/receipt': 'Поступление', '/transaction/issue': 'Выдача',
  '/transaction/settle': 'Принять сдачу', '/transaction/writeoff': 'Списание со склада',
  '/transaction/regrind/send': 'Отправка на переточку',
  '/transaction/regrind/send-all': 'Отправка всей очереди',
  '/transaction/regrind/receive': 'Приём после переточки',
};
function summary(form) {
  const path = new URL(form.getAttribute('action'), location.href).pathname;
  const kind = path.split('/').at(-1);
  const item = window.pickedProducts?.[kind];
  const lines = [];
  if (item) lines.push(item.name + ' · ' + item.code);
  else if (valueOf(form, 'product_id') === 'new') lines.push('Новая карточка: ' + valueOf(form, 'new_name'));
  else if (labelOf(form, 'product_id')) lines.push(labelOf(form, 'product_id'));
  if (path.endsWith('/send-all')) {
    lines.push(...(operationPreset?.queue || []));
    lines.push('Будет отправлена вся текущая очередь.');
  }
  if (valueOf(form, 'qty')) lines.push('Количество: ' + valueOf(form, 'qty') + ' шт.');
  if (labelOf(form, 'worker_id')) lines.push('Рабочий: ' + labelOf(form, 'worker_id'));
  if (labelOf(form, 'machine_id')) lines.push('Станок: ' + labelOf(form, 'machine_id'));
  if (path.endsWith('/settle')) {
    lines.push(labelOf(form, 'holding_key'));
    lines.push('Действие: ' + labelOf(form, 'action'));
  }
  if (labelOf(form, 'state')) lines.push('Состояние: ' + labelOf(form, 'state'));
  if (item && ['receipt','issue','writeoff'].includes(kind)) {
    const state = kind === 'receipt' ? 'new' : valueOf(form, 'state');
    const stock = item.stocks.find(stock => stock.state === state)?.qty || 0;
    const next = stock + (kind === 'receipt' ? 1 : -1) * Number(valueOf(form, 'qty'));
    lines.push('Остаток в этом состоянии после операции: ' + next + ' ' + item.unit + '.');
  }
  if (valueOf(form, 'diameter')) lines.push('Ø после переточки: ' + valueOf(form, 'diameter') + ' мм');
  if (valueOf(form, 'l1')) lines.push('l1 после переточки: ' + valueOf(form, 'l1') + ' мм');
  if (valueOf(form, 'note')) lines.push('Комментарий: ' + valueOf(form, 'note'));
  return {title: actionNames[path], lines};
}

const dialog = document.createElement('dialog');
dialog.className = 'operation-confirm';
dialog.setAttribute('aria-labelledby', 'confirm-title');
dialog.innerHTML = '<div class="confirm-heading"><span class="eyebrow">ПРОВЕРЬТЕ ДАННЫЕ</span><h2 id="confirm-title"></h2></div><ul class="confirm-lines"></ul><p class="form-help">После подтверждения операция будет записана в журнал. Доступный остаток проверяется при проведении.</p><div class="confirm-actions"><button type="button" class="button secondary" data-cancel>Вернуться к форме</button><button type="button" class="button primary" data-approve>Подтвердить</button></div>';
document.body.append(dialog);
let confirmation = null;
function confirmOperation(form) {
  const content = summary(form);
  dialog.querySelector('h2').textContent = content.title;
  const list = dialog.querySelector('ul');
  list.replaceChildren(...content.lines.map(line => {
    const li = document.createElement('li'); li.textContent = line; return li;
  }));
  return new Promise(resolve => {
    confirmation = resolve;
    dialog.showModal();
    dialog.querySelector('[data-cancel]').focus();
  });
}
function finishConfirmation(accepted) {
  dialog.close();
  const resolve = confirmation; confirmation = null;
  resolve?.(accepted);
}
dialog.querySelector('[data-cancel]').addEventListener('click', () => finishConfirmation(false));
dialog.querySelector('[data-approve]').addEventListener('click', () => finishConfirmation(true));
dialog.addEventListener('cancel', event => { event.preventDefault(); finishConfirmation(false); });

const busyForms = new WeakSet();
document.querySelectorAll('form[method="post"]').forEach(form => {
  const path = new URL(form.getAttribute('action'), location.href).pathname;
  if (['/login','/setup','/logout'].includes(path)) return;
  form.addEventListener('submit', async event => {
    if (event.defaultPrevented) return;
    event.preventDefault();
    if (busyForms.has(form)) return;
    busyForms.add(form);
    const buttons = [...form.querySelectorAll('button')];
    const oldStates = buttons.map(button => button.disabled);
    try {
      if (actionNames[path]) {
        const pid = valueOf(form, 'product_id');
        if (!path.endsWith('/send-all') && !pid) {
          formError(form, 'Выберите позицию из результатов поиска.'); return;
        }
        if (!await confirmOperation(form)) return;
      }
      form.querySelector('.form-error')?.setAttribute('hidden', '');
      // Capture data before disabling any controls.
      const data = new FormData(form);
      buttons.forEach(button => { button.disabled = true; });
      form.setAttribute('aria-busy', 'true');
      const response = await fetch(form.getAttribute('action'), {method: 'POST', body: data});
      if (response.ok && response.redirected) {
        location.assign(response.url); return;
      }
      const text = await response.text();
      const message = new DOMParser().parseFromString(text, 'text/html').querySelector('.error-panel h2')?.textContent;
      formError(form, message || 'Не удалось провести операцию. Проверьте данные и повторите отправку.');
    } catch (_) {
      formError(form, 'Нет связи с сервером. Данные сохранены в форме. Повторите отправку — операция не задвоится.');
    } finally {
      busyForms.delete(form);
      buttons.forEach((button, index) => { button.disabled = oldStates[index]; });
      form.removeAttribute('aria-busy');
    }
  });
});

// Directory sections share the page but keep only the selected workspace visible.
const adminTabs = [...document.querySelectorAll('[data-admin-tab]')];
function showAdminSection(section) {
  if (!adminTabs.some(tab => tab.dataset.adminTab === section)) return;
  document.querySelectorAll('[data-admin-section]').forEach(panel => {
    panel.hidden = panel.dataset.adminSection !== section;
  });
  adminTabs.forEach(tab => {
    const active = tab.dataset.adminTab === section;
    tab.classList.toggle('primary', active);
    tab.classList.toggle('secondary', !active);
    if (active) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  });
}
adminTabs.forEach(tab => tab.addEventListener('click', event => {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  showAdminSection(tab.dataset.adminTab);
  history.pushState({}, '', tab.href);
}));
window.addEventListener('popstate', () => showAdminSection(new URL(location.href).searchParams.get('section') || 'machines'));

function searchableList(input, container, selector, getText, message) {
  const rows = [...container.querySelectorAll(selector)];
  if (!rows.length) { input.closest('label').hidden = true; return; }
  const empty = document.createElement('p');
  empty.className = 'form-help'; empty.hidden = true; empty.textContent = message;
  container.append(empty);
  const indexed = rows.map(row => ({row, text: getText(row).toLocaleLowerCase('ru')}));
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') event.preventDefault();
  });
  input.addEventListener('input', () => {
    const words = input.value.trim().toLocaleLowerCase('ru').split(/\s+/).filter(Boolean);
    let visible = 0;
    indexed.forEach(({row, text}) => {
      row.hidden = !words.every(word => text.includes(word));
      if (!row.hidden) visible++;
    });
    empty.hidden = visible > 0;
  });
}
document.querySelectorAll('[data-directory-search]').forEach(input => {
  searchableList(input, input.closest('label').nextElementSibling, '.directory-row',
    row => [...row.querySelectorAll('.directory-content > b, .directory-content > small')].map(el => el.textContent).join(' '),
    'По вашему запросу записи не найдены');
});
document.querySelectorAll('[data-machine-search]').forEach(input => {
  searchableList(input, input.closest('.machine-options').querySelector('.machine-check-grid'), '.machine-check',
    row => row.textContent, 'Станки не найдены');
});
