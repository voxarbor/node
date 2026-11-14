// App shell: login, hash routing between the views, server status.
import { api, auth, onUnauthorized } from './api.js';
import * as history from './history.js';
import * as settings from './settings.js';
import * as speak from './speak.js';
import * as tools from './tools.js';
import { setBase } from './title.js';
import { getVoices } from './voices.js';
import { $, toast } from './util.js';

const VIEWS = { speak, history, tools };
const TITLES = { speak: 'Озвучити', history: 'Історія', tools: 'Інструменти' };
let currentView = null;
let statusTimer = null;
let loggedIn = false;

// ---------------------------------------------------------------- login
function showLogin(message = '') {
  loggedIn = false;
  clearTimeout(statusTimer);
  if (currentView) { VIEWS[currentView].hide(); currentView = null; }
  $('#shell').hidden = true;
  $('#login').hidden = false;
  $('#login-error').textContent = message;
  setBase('Вхід · Озвучка');
  setTimeout(() => $('#login-pass').focus(), 0);
}

function showShell() {
  loggedIn = true;
  $('#login').hidden = true;
  $('#shell').hidden = false;
  route();
  getVoices().catch(() => {});   // voice labels («Чоловічий 13») for job panels and History
  warmup();
  pollStatus();
}

/** Load the models in the background (cold start ~25 s) so the first «Озвучити» does not wait. */
const lastWarm = {};
export function warmup(engine = settings.load().engine || 'holos') {
  if (!loggedIn || Date.now() - (lastWarm[engine] || 0) < 60000) return;
  lastWarm[engine] = Date.now();
  api(`/v1/warmup?engine=${encodeURIComponent(engine)}`, { method: 'POST' })
    .then(() => setTimeout(pollStatus, 1500))
    .catch(() => { lastWarm[engine] = 0; });
}
settings.subscribe((s, patch) => { if ('engine' in patch && s.engine) warmup(s.engine); });

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const user = $('#login-user').value.trim();
  const pass = $('#login-pass').value;
  const err = $('#login-error');
  const submit = $('#login-submit');
  if (!pass) { err.textContent = 'Введіть пароль.'; $('#login-pass').focus(); return; }
  submit.disabled = true;
  submit.setAttribute('aria-busy', 'true');
  try {
    const r = await auth.login(user, pass);
    if (r.ok) {
      $('#login-pass').value = '';
      err.textContent = '';
      showShell();
    } else {
      err.textContent = r.data.detail || (r.status === 429 ? 'Забагато спроб, зачекайте.' : 'Не вдалося увійти.');
      $('#login-pass').select();
    }
  } catch {
    err.textContent = 'Немає з’єднання з сервером.';
  } finally {
    submit.disabled = false;
    submit.removeAttribute('aria-busy');
  }
});

$('#logout').addEventListener('click', async () => {
  try { await auth.logout(); } catch { /* offline: drop the session view anyway */ }
  showLogin();
});

onUnauthorized(() => { if (loggedIn) showLogin('Сесія завершилась — увійдіть знову.'); });

// ---------------------------------------------------------------- routing
function route() {
  if (!loggedIn) return;
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  const name = VIEWS[parts[0]] ? parts[0] : 'speak';
  const params = parts.slice(1);
  for (const a of document.querySelectorAll('.tabbar a')) {
    if (a.dataset.tab === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  if (currentView && currentView !== name) VIEWS[currentView].hide();
  for (const v of Object.keys(VIEWS)) $(`#v-${v}`).hidden = v !== name;
  const changed = currentView !== name;
  currentView = name;
  VIEWS[name].show($(`#v-${name}`), params);
  setBase(`${TITLES[name]} · Озвучка`);
  if (changed || name !== 'speak') {
    window.scrollTo(0, 0);
    if (document.activeElement && document.activeElement.closest('.tabbar')) $('#view').focus({ preventScroll: true });
  }
}
window.addEventListener('hashchange', route);

// ---------------------------------------------------------------- status
async function pollStatus() {
  clearTimeout(statusTimer);
  if (!loggedIn) return;
  const box = $('#status');
  const text = box.querySelector('.status-text');
  let delay = 20000;
  try {
    const st = await api('/health');
    const jobs = st.jobs || {};
    const active = (jobs.running || 0) + (jobs.queued || 0);
    if (active) {
      box.dataset.state = 'busy';
      text.textContent = jobs.queued ? `Озвучення: ${jobs.running || 0} + ${jobs.queued} у черзі` : 'Озвучення…';
      delay = 5000;
    } else if (st.loading) {
      box.dataset.state = 'busy';
      text.textContent = 'Завантаження моделі…';
      delay = 3000;
    } else if (st.loaded) {
      box.dataset.state = 'loaded';
      text.textContent = 'Модель готова';
    } else {
      box.dataset.state = 'idle';
      text.textContent = 'Очікує';
    }
    box.title = st.loaded
      ? `Модель завантажена; вивантажиться після ${Math.round((st.idle_ttl || 600) / 60)} хв простою`
      : 'Модель не завантажена: перший запуск займе ~15 с довше';
  } catch (e) {
    if (e.status === 401) return;
    box.dataset.state = 'down';
    text.textContent = 'Немає зв’язку';
    box.title = e.message;
    delay = 10000;
  }
  statusTimer = setTimeout(pollStatus, document.hidden ? Math.max(delay, 60000) : delay);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && loggedIn) { warmup(); pollStatus(); } });
window.addEventListener('online', () => { if (loggedIn) { pollStatus(); toast('З’єднання відновлено', { type: 'ok' }); } });
window.addEventListener('offline', () => toast('Немає інтернету', { type: 'error' }));

// ---------------------------------------------------------------- start
(async () => {
  try {
    const r = await auth.me();
    if (r.ok && r.data.user) showShell(); else showLogin();
  } catch {
    showLogin('Немає з’єднання з сервером.');
  }
})();
