// «Історія»: master-detail. Wide screens show the list and the open job side by side; phones show one at a time.
import { api } from './api.js';
import * as current from './current.js';
import { JobView, jobPercent, statusBadge, TERMINAL } from './jobview.js';
import * as speak from './speak.js';
import { btn, debounce, fmtDate, fmtEta, fmtTime, h, icon, setBusy, toast } from './util.js';
import { ENGINE_LABELS, getVoices, prettyVoice } from './voices.js';

const PAGE = 50;
const WIDE = window.matchMedia('(min-width: 1024px)');
let root = null;
let built = false;
let items = [];
let total = 0;
let query = '';
let loadedOnce = false;
let pollTimer = null;
let detail = null;
let detailId = null;
let visible = false;
let wrap, listEl, moreBtn, infoEl, searchInput, detailBox, backLink, emptyDetail;

export function show(el, params) {
  root = el;
  visible = true;
  if (!built) build();
  select(params[0] ? decodeURIComponent(params[0]) : null);
  refresh();
}

export function hide() {
  visible = false;
  stopPoll();
  closeDetail();
}

function stopPoll() { clearTimeout(pollTimer); pollTimer = null; }

function build() {
  built = true;
  searchInput = h('input', {
    type: 'search', placeholder: 'Пошук за назвою, голосом або текстом', 'aria-label': 'Пошук в історії', autocomplete: 'off',
    onInput: debounce(() => { query = searchInput.value.trim().toLowerCase(); items = []; refresh(); }, 300),
  });
  listEl = h('ul', { class: 'jobs', 'aria-label': 'Записи' }, h('li', { class: 'empty' }, 'Завантаження…'));
  infoEl = h('p', { class: 'list-info', role: 'status' });
  moreBtn = btn('Показати ще', { cls: 'btn small block', onClick: loadMore });
  backLink = h('a', { class: 'back', href: '#/history' }, icon('back'), h('span', null, 'Усі записи'));
  emptyDetail = h('div', { class: 'panel detail-empty' }, icon('history'), h('p', null, 'Виберіть запис у списку, щоб прослухати його й переглянути текст.'));
  detailBox = h('div', { class: 'detail-box' });
  wrap = h('div', { class: 'history' },
    h('section', { class: 'panel list-pane', 'aria-labelledby': 'h-history' },
      h('div', { class: 'panel-head' }, h('h1', { id: 'h-history', class: 'panel-title' }, 'Історія'), infoEl,
        btn('Оновити', { icon: 'repeat', cls: 'icon-btn', iconOnly: true, onClick: (e) => refresh(e.currentTarget) })),
      h('div', { class: 'search' }, icon('search'), searchInput),
      h('div', { class: 'list-scroll' }, listEl, moreBtn)),
    h('section', { class: 'detail-pane', 'aria-label': 'Запис' }, backLink, emptyDetail, detailBox));
  root.replaceChildren(wrap);
  getVoices().then(() => { if (loadedOnce) renderItems(); }).catch(() => {}); // voice labels may arrive after the list
}

// ---------------------------------------------------------------- list
async function refresh(button) {
  setBusy(button, true);
  try {
    const data = await api(`/v1/jobs?limit=${Math.max(PAGE, Math.min(items.length, 500))}&offset=0&q=${encodeURIComponent(query)}`);
    if (!visible) return;
    items = data.jobs || [];
    total = data.total ?? items.length;
    loadedOnce = true;
    renderItems();
    // wide screens: open the newest record instead of an empty detail pane
    if (!detailId && WIDE.matches && items.length && !query) location.replace(`#/history/${encodeURIComponent(items[0].id)}`);
  } catch (e) {
    if (!visible) return;
    if (!items.length) listEl.replaceChildren(h('li', { class: 'empty' }, e.message));
    toast(e.message, { type: 'error' });
  } finally {
    setBusy(button, false);
    schedulePoll();
  }
}

async function loadMore() {
  setBusy(moreBtn, true);
  try {
    const data = await api(`/v1/jobs?limit=${PAGE}&offset=${items.length}&q=${encodeURIComponent(query)}`);
    const seen = new Set(items.map((j) => j.id));
    items = items.concat((data.jobs || []).filter((j) => !seen.has(j.id)));
    total = data.total ?? total;
    renderItems();
  } catch (e) {
    toast(e.message, { type: 'error' });
  } finally {
    setBusy(moreBtn, false);
  }
}

function schedulePoll() {
  stopPoll();
  if (visible && items.some((j) => !TERMINAL.has(j.status))) pollTimer = setTimeout(() => refresh(), 3000);
}

function renderItems() {
  if (!items.length) {
    listEl.replaceChildren(h('li', { class: 'empty' }, query ? 'Нічого не знайдено.' : ['Тут з’являться ваші озвучення. ', h('a', { href: '#/speak' }, 'Озвучити текст')]));
  } else {
    listEl.replaceChildren(...items.map(itemEl));
  }
  infoEl.textContent = loadedOnce ? (query ? `знайдено ${total}` : `${total}`) : '';
  moreBtn.hidden = items.length >= total;
}

function itemEl(j) {
  const p = j.params || {};
  const running = !TERMINAL.has(j.status);
  const { total: t = 0, eta_s: eta = null } = j.progress || {};
  const pct = jobPercent(j);
  const meta = [fmtDate(j.created), j.duration_s ? fmtTime(j.duration_s) : null,
    p.model === 'single' ? 'один диктор' : prettyVoice(p.voice, p.engine), ENGINE_LABELS[p.engine] || p.engine].filter(Boolean);
  return h('li', null, h('a', { class: 'job-item', href: `#/history/${encodeURIComponent(j.id)}`, 'aria-current': j.id === detailId ? 'true' : null },
    h('div', { class: 't' }, h('span', { class: 'title' }, j.title || 'Без назви'), statusBadge(j.status, pct)),
    h('div', { class: 'm' }, meta.map((m) => h('span', null, m)), running && eta != null ? h('span', null, `залишилось ${fmtEta(eta)}`) : null),
    running ? h('div', { class: `progress${t || pct ? '' : ' indeterminate'}`, role: 'progressbar', 'aria-label': 'Прогрес', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': Math.floor(pct) },
      bar(pct)) : null));
}

function bar(pct) {
  const span = h('span');
  span.style.width = `${Math.round(pct)}%`;
  return span;
}

// ---------------------------------------------------------------- detail
function closeDetail() {
  if (detail) detail.destroy();
  detail = null;
  detailId = null;
}

function select(id) {
  wrap.classList.toggle('has-detail', !!id);
  emptyDetail.hidden = !!id;
  if (id === detailId && detail) return;
  closeDetail();
  for (const a of listEl.querySelectorAll('.job-item')) {
    if (a.getAttribute('href') === `#/history/${encodeURIComponent(id)}`) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current');
  }
  if (!id) return;
  detailId = id;
  detail = new JobView(detailBox, {
    manage: true,
    onListen: (job) => current.setCurrent(job.id),   // «Слухати» here makes it the job restored on «Озвучити»
    onPosition: (jid, pos) => current.savePos(jid, pos),
    onDeleted: () => {
      items = items.filter((j) => j.id !== id);
      total = Math.max(0, total - 1);
      detail = null;
      detailId = null;
      renderItems();
      location.hash = WIDE.matches && items.length ? `#/history/${encodeURIComponent(items[0].id)}` : '#/history';
    },
    onRepeat: (job, text) => {
      if (!text) { toast('Текст запису недоступний', { type: 'error' }); return; }
      speak.prefill(text, job.params || {});
      location.hash = '#/speak';
      toast('Текст і налаштування перенесено — натисніть «Озвучити».', { type: 'ok' });
    },
    onChanged: (job) => { items = items.map((j) => (j.id === job.id ? { ...j, ...job } : j)); renderItems(); },
  });
  detail.open(id, { resumeAt: current.getPos(id) });
  if (!WIDE.matches) window.scrollTo(0, 0);
}
