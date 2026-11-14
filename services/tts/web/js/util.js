// DOM helpers, formatting, toasts and dialogs
const SVG_NS = 'http://www.w3.org/2000/svg';
const PROPS = new Set(['value', 'checked', 'disabled', 'hidden', 'selected', 'indeterminate', 'open']);

export const $ = (sel, root = document) => root.querySelector(sel);

/** h('button', {class: 'btn', onClick: fn}, 'text', child...) */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false && !PROPS.has(k)) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (PROPS.has(k)) el[k] = v;
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function icon(name, cls = 'ic') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', cls);
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

/** Button with optional icon. */
export function btn(label, { icon: ic, cls = 'btn', onClick, title, type = 'button', iconOnly = false, ...rest } = {}) {
  return h('button', { class: cls, type, onClick, title: title || (iconOnly ? label : null), 'aria-label': iconOnly ? label : null, ...rest },
    ic ? icon(ic) : null, iconOnly ? null : h('span', null, label));
}

export function setBusy(button, busy) {
  if (!button) return;
  if (busy) { button.setAttribute('aria-busy', 'true'); button.disabled = true; }
  else { button.removeAttribute('aria-busy'); button.disabled = false; }
}

let uid = 0;
export const nextId = (p = 'id') => `${p}-${++uid}`;

// ---------- formatting
export function fmtTime(sec) {
  if (sec == null || !isFinite(sec)) return '–';
  sec = Math.max(0, Math.round(sec));
  const h_ = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  return h_ ? `${h_}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

const dateFmt = new Intl.DateTimeFormat('uk-UA', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const dateFmtYear = new Intl.DateTimeFormat('uk-UA', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
export function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return (d.getFullYear() === new Date().getFullYear() ? dateFmt : dateFmtYear).format(d);
}

/** Remaining time, rounded: «~40 с», «~3 хв», «~1 год 10 хв». */
export function fmtEta(sec) {
  if (sec == null || !isFinite(sec)) return '';
  sec = Math.max(1, Math.round(sec));
  if (sec < 60) return `~${sec < 10 ? sec : Math.round(sec / 5) * 5}\u00a0с`;
  const min = Math.round(sec / 60);
  if (min < 60) return `~${min}\u00a0хв`;
  const hh = Math.floor(min / 60), mm = min % 60;
  return `~${hh}\u00a0год${mm ? ` ${mm}\u00a0хв` : ''}`;
}
export const fmtPct = (p) => `${Math.floor(Math.max(0, Math.min(100, p || 0)))}\u00a0%`;

const numFmt = new Intl.NumberFormat('uk-UA');
export const fmtNum = (n) => numFmt.format(n);

export function plural(n, one, few, many) {
  const n10 = n % 10, n100 = n % 100;
  if (n10 === 1 && n100 !== 11) return one;
  if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
  return many;
}

export const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- toasts
export function toast(message, { type = 'info', action = null, timeout } = {}) {
  const root = document.getElementById('toasts');
  const el = h('div', { class: `toast ${type}`, role: type === 'error' ? 'alert' : 'status' }, h('span', { class: 'msg' }, message));
  const close = () => el.remove();
  if (action) el.append(h('button', { type: 'button', onClick: () => { close(); action.fn(); } }, action.label));
  el.append(h('button', { type: 'button', 'aria-label': 'Закрити', onClick: close }, '×'));
  root.append(el);
  while (root.children.length > 3) root.firstChild.remove();
  setTimeout(close, timeout ?? (type === 'error' ? 8000 : action ? 7000 : 3500));
  return close;
}

// ---------- dialogs (native <dialog>: focus trap, Esc, backdrop)
export function dialog({ title, message, ok = 'Гаразд', cancel = 'Скасувати', danger = false, input = null }) {
  return new Promise((resolve) => {
    const titleId = nextId('dlg');
    const field = input ? h('input', { type: 'text', value: input.value || '', 'aria-label': input.label || title, maxlength: input.maxlength || 300 }) : null;
    const form = h('form', { method: 'dialog' },
      h('h2', { id: titleId }, title),
      message ? h('p', { class: 'muted' }, message) : null,
      field,
      h('div', { class: 'row' },
        h('button', { class: 'btn', type: 'button', value: 'cancel', onClick: () => { result = false; dlg.close(); } }, cancel),
        h('button', { class: `btn ${danger ? 'danger-solid' : 'primary'}`, type: 'submit', value: 'ok' }, ok)));
    let result = false;
    const dlg = h('dialog', { 'aria-labelledby': titleId }, form);
    form.addEventListener('submit', () => { result = field ? field.value : true; });
    dlg.addEventListener('close', () => { dlg.remove(); resolve(result); });
    document.body.append(dlg);
    dlg.showModal();
    if (field) { field.focus(); field.select(); }
  });
}

/** Read a File as UTF-8 text. */
export function readFile(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ''));
    r.onerror = () => reject(r.error);
    r.readAsText(file, 'utf-8');
  });
}
