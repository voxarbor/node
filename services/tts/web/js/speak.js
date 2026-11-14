// «Озвучити»: editor + settings sidebar; creates a job and plays it while it is synthesized.
import { api } from './api.js';
import * as current from './current.js';
import { JobView, TERMINAL } from './jobview.js';
import { unlockAudio } from './player.js';
import * as settings from './settings.js';
import { btn, debounce, fmtNum, h, icon, nextId, readFile, setBusy, toast } from './util.js';
import { ENGINE_HINTS, ENGINE_LABELS, getVoices, preview, voiceGroups, voiceLabel, voiceList } from './voices.js';

/** <optgroup>s «Улюблені», «Чоловічі», «Жіночі», «Дитячі»; the voice id stays in the tooltip. */
const voiceOptions = (engine) => voiceGroups(info, engine).map((g) => h('optgroup', { label: g.label },
  g.voices.map((v) => h('option', { value: v.name, title: v.name !== v.label ? v.name : null }, voiceLabel(v)))));

export const MAX_CHARS = 300000;
const FORMATS = [['mp3', 'MP3'], ['wav', 'WAV'], ['flac', 'FLAC'], ['opus', 'Opus']];

let built = false;
let s = settings.load();
let info = null;
const el = {};
let jobView = null;

const RESTORE_HOURS = 12;

export function show(root) {
  if (!built) build(root);
  const id = current.getCurrent();
  if (id && (!jobView || !jobView.alive || jobView.id !== id) && !(jobView?.alive && jobView.isPlaying())) restore(id);
}

function jobPanel() {
  if (!jobView) {
    jobView = new JobView(el.job, {
      historyLink: true, onClose: closeJob,
      onPosition: (id, pos) => current.savePos(id, pos),
      onListen: (job) => current.setCurrent(job.id),
    });
  }
  return jobView;
}

/** After a reload: bring back the panel of the remembered job (running, or finished in the last 12 h). */
let restoring = null;
async function restore(id) {
  if (restoring === id) return;
  restoring = id;
  try {
    const d = await api(`/v1/jobs/${encodeURIComponent(id)}`);
    const age = (Date.now() - Date.parse(d.updated || d.created)) / 3600e3;
    if (TERMINAL.has(d.status) && !(age < RESTORE_HOURS)) { current.clearCurrent(); return; }
    if (current.getCurrent() !== id) return;
    el.ws.classList.add('has-job');
    jobPanel().open(id, { job: d, detail: d, resumeAt: current.getPos(id) });
  } catch (e) {
    if (e.status === 404) { current.clearCurrent(); current.forgetPos(id); }
  } finally {
    restoring = null;
  }
}
export function hide() {}

/** «Повторити» from history: fill the form with a job's text and settings. */
export function prefill(text, params = {}) {
  const keep = ['engine', 'voice', 'model', 'speed', 'pause_ms', 'verbalize', 'english', 'clean', 'response_format', 'initials', 'brackets_gain_db'];
  const patch = {};
  for (const k of keep) if (params[k] !== undefined && params[k] !== null) patch[k] = params[k];
  const b = settings.normBrackets(params);
  Object.assign(patch, { brackets: b.brackets === 'skip' ? 'skip' : 'aside', brackets_announce: b.brackets_announce, brackets_voice_on: b.brackets === 'voice' });
  if (b.brackets === 'voice' && params.brackets_voice) patch.extra_voice = params.brackets_voice;
  else if (params.engine && params.engine !== s.engine) patch.extra_voice = null;
  s = settings.save(patch);
  settings.saveDraft(text);
  if (built) {
    el.text.value = text;
    syncForm();
    updateCounter();
  }
}

function switchRow(name, title, hint, { checked = !!s[name], onChange = (v) => { s = settings.save({ [name]: v }); updateSummary(); } } = {}) {
  const id = nextId(name);
  const input = h('input', { type: 'checkbox', id, role: 'switch', checked, 'aria-describedby': `${id}-h`, onChange: (e) => onChange(e.target.checked) });
  el[name] = input;
  return h('label', { class: 'switch', for: id }, input, h('span', { class: 'track', 'aria-hidden': 'true' }),
    h('span', { class: 'sw-text' }, h('b', null, title), h('span', { class: 'hint', id: `${id}-h` }, hint)));
}

function rangeRow(name, label, { min, max, step, fmt }) {
  const id = nextId(name);
  const out = h('output', { for: id });
  const input = h('input', {
    type: 'range', id, min, max, step, value: s[name],
    onInput: (e) => { out.textContent = fmt(Number(e.target.value)); input.setAttribute('aria-valuetext', out.textContent); },
    onChange: (e) => { s = settings.save({ [name]: Number(e.target.value) }); updateSummary(); },
  });
  out.textContent = fmt(Number(s[name]));
  input.setAttribute('aria-valuetext', out.textContent);
  el[name] = input;
  el[`${name}Out`] = out;
  return h('div', { class: 'field range-row' }, h('label', { for: id }, label), out, input);
}

function segGroup(name, legend, options, onChange) {
  const box = h('div', { class: 'seg' });
  for (const [value, label, title] of options) {
    box.append(h('label', { title }, h('input', { type: 'radio', name, value, checked: s[name] === value, onChange: () => onChange(value) }), h('span', null, label)));
  }
  el[`${name}Group`] = box;
  return h('fieldset', null, h('legend', null, legend), box);
}

const section = (label, ...children) => h('section', { class: 'side-section' }, h('h2', { class: 'section-label' }, label), ...children);

function build(root) {
  built = true;
  const textId = nextId('text');
  el.text = h('textarea', {
    id: textId, name: 'text', spellcheck: 'true', lang: 'uk', 'aria-describedby': `${textId}-c`,
    placeholder: 'Вставте або введіть текст українською. «+» після голосної ставить наголос: замо+к.',
    onInput: debounce(() => { settings.saveDraft(el.text.value); updateCounter(); }, 250),
    onKeydown: (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(); } },
  });
  el.text.value = settings.loadDraft();
  el.counter = h('span', { class: 'counter', id: `${textId}-c`, 'aria-live': 'off' });
  el.file = h('input', { type: 'file', accept: '.txt,.md,.markdown,.text,text/plain,text/markdown', hidden: true, onChange: onFile });

  const tools = [
    navigator.clipboard?.readText ? btn('Вставити', { icon: 'paste', cls: 'btn small', onClick: onPaste }) : null,
    btn('Файл', { icon: 'file', cls: 'btn small', title: 'Відкрити .txt або .md з пристрою', onClick: () => el.file.click() }),
    btn('Очистити', { icon: 'x', cls: 'btn small', onClick: onClear }),
    h('a', { class: 'btn small', href: '#/tools/check', onClick: () => settings.saveDraft(el.text.value) }, icon('text'), h('span', null, 'Перевірити')),
  ];
  const editor = h('section', { class: 'panel ws-editor', 'aria-label': 'Текст' },
    h('div', { class: 'panel-head' }, h('label', { for: textId, class: 'panel-title' }, 'Текст'), el.counter, h('div', { class: 'toolbar' }, tools)),
    el.text, el.file,
    h('p', { class: 'editor-foot' }, h('kbd', null, 'Ctrl'), '+', h('kbd', null, 'Enter'), ' — озвучити · ', h('code', null, '+'), ' після голосної — наголос · ', h('code', null, '++'), ' — довший наголос'));

  // settings sidebar
  const voiceId = nextId('voice');
  el.voice = h('select', { id: voiceId, disabled: true, onChange: () => { s = settings.save({ voice: el.voice.value }); updateSummary(); } }, h('option', null, 'Завантаження голосів…'));
  el.previewBtn = h('button', { class: 'icon-btn bordered', type: 'button', 'aria-label': 'Прослухати голос', title: 'Прослухати голос', onClick: () => preview({ engine: s.engine, voice: el.voice.value, model: s.model, btn: el.previewBtn }) }, icon('play'));
  el.singleRow = switchRow('single', 'Один диктор (single)', 'StyleTTS2: окрема модель з одним чоловічим голосом', {
    checked: s.model === 'single',
    onChange: (v) => { s = settings.save({ model: v ? 'single' : 'multi' }); syncVoiceEnabled(); syncBrackets(); },
  });
  el.singleInput = el.single;
  el.titleInput = h('input', { type: 'text', id: nextId('title'), maxlength: 200, placeholder: 'Перші слова тексту', autocomplete: 'off' });
  el.summary = h('span', { class: 'summary-value' });
  const sectionC = (label, ...children) => { const sec = section(label, ...children); sec.classList.add('collapsible'); return sec; };
  el.moreToggle = h('button', { class: 'more-toggle', type: 'button', 'aria-expanded': 'false',
    onClick: () => { const open = el.side.classList.toggle('expanded'); el.moreToggle.setAttribute('aria-expanded', String(open)); } },
  h('span', null, 'Швидкість і параметри'), el.summary, h('span', { class: 'chev', 'aria-hidden': 'true' }));
  const more = [
    sectionC('Звучання',
      rangeRow('speed', 'Швидкість', { min: 0.5, max: 2, step: 0.05, fmt: (v) => `${v.toFixed(2)}×` }),
      rangeRow('pause_ms', 'Пауза між реченнями', { min: 0, max: 2000, step: 50, fmt: (v) => `${v} мс` })),
    sectionC('Обробка тексту',
      switchRow('verbalize', 'Числа словами', 'Цифри, дати, скорочення — словами'),
      switchRow('english', 'Англійські назви', 'GitHub, iPhone, API — по-англійськи'),
      switchRow('clean', 'Очистити Markdown', 'Без розмітки; посилання — лише домен'),
      initialsRow(),
      bracketsControls()),
    sectionC('Файл',
      segGroup('response_format', 'Формат', FORMATS, (v) => { s = settings.save({ response_format: v }); updateSummary(); }),
      h('div', { class: 'field' }, h('label', { for: el.titleInput.id }, 'Назва в історії'), el.titleInput)),
  ];

  const engineField = segGroup('engine', 'Рушій', [], (v) => { s = settings.save({ engine: v, voice: null, extra_voice: null }); fillVoices(); });
  el.go = h('button', { class: 'btn primary big block', type: 'button', onClick: go }, icon('speak'), h('span', null, 'Озвучити'));
  const side = h('aside', { class: 'panel ws-side', 'aria-label': 'Налаштування озвучення' },
    h('div', { class: 'side-scroll' },
      section('Голос', engineField,
        h('div', { class: 'field' }, h('label', { for: voiceId }, 'Голос'), h('div', { class: 'voice-row' }, el.voice, el.previewBtn)),
        extraVoiceControls(),
        el.singleRow),
      el.moreToggle, ...more),
    h('div', { class: 'go-bar' }, el.go));
  el.side = side;

  el.job = h('div', { class: 'ws-job', id: 'speak-job' });
  el.ws = h('div', { class: 'workspace' }, editor, side, el.job);
  root.replaceChildren(h('h1', { id: 'h-speak', class: 'sr-only' }, 'Озвучити текст'), el.ws);

  updateCounter();
  updateSummary();
  loadVoices();
  settings.subscribe((next, patch) => {
    s = next;
    if ('voice' in patch || 'engine' in patch) syncForm();
  });
}

async function loadVoices() {
  try {
    info = await getVoices();
  } catch (e) {
    el.voice.replaceChildren(h('option', null, 'Голоси недоступні'));
    toast(`Не вдалося завантажити голоси: ${e.message}`, { type: 'error', action: { label: 'Ще раз', fn: loadVoices } });
    return;
  }
  const engines = Object.keys(info.engines).sort((a, b) => (b === info.default_engine) - (a === info.default_engine));
  if (!s.engine || !engines.includes(s.engine)) s = settings.save({ engine: info.default_engine || engines[0] });
  el.engineGroup.replaceChildren(...engines.map((e) => h('label', { title: ENGINE_HINTS[e] || '' },
    h('input', { type: 'radio', name: 'engine', value: e, checked: s.engine === e, onChange: () => { s = settings.save({ engine: e, voice: null, extra_voice: null }); fillVoices(); } }),
    h('span', null, ENGINE_LABELS[e] || e))));
  fillVoices();
  updateSummary();
}

function fillVoices() {
  if (!info) return;
  const list = voiceList(info, s.engine);
  el.voice.replaceChildren(...voiceOptions(s.engine));
  const names = list.map((v) => v.name);
  const voice = names.includes(s.voice) ? s.voice : info.engines[s.engine]?.default || names[0];
  el.voice.value = voice;
  if (s.voice !== voice) s = settings.save({ voice });
  el.singleRow.hidden = !info.engines[s.engine]?.models;
  syncVoiceEnabled();
  fillExtraVoices();
}

const INITIALS_HINT = { skip: 'Лише прізвище: «Т. Г. Шевченко» → «Шевченко»', letters: 'Ініціали читаються літерами: «те ге Шевченко»' };

/** «Пропускати ініціали»: initials next to a surname are read as letter names or dropped. */
function initialsRow() {
  el.initialsRow = switchRow('initials', 'Пропускати ініціали', INITIALS_HINT[s.initials === 'skip' ? 'skip' : 'letters'], {
    checked: s.initials === 'skip',
    onChange: (v) => { s = settings.save({ initials: v ? 'skip' : 'letters' }); syncInitials(); },
  });
  return el.initialsRow;
}

function syncInitials() {
  if (!el.initialsRow) return;
  const skip = s.initials === 'skip';
  el.initials.checked = skip;
  el.initialsRow.querySelector('.hint').textContent = INITIALS_HINT[skip ? 'skip' : 'letters'];
}

/** «Додатковий голос»: optional second voice of the same engine for special parts of the text (brackets for now). */
function extraVoiceControls() {
  const id = nextId('extra');
  el.extraVoice = h('select', { id, 'aria-describedby': `${id}-h`, onChange: () => { s = settings.save({ extra_voice: el.extraVoice.value || null }); syncBrackets(); } },
    h('option', { value: '' }, 'Немає'));
  el.extraPreview = h('button', { class: 'icon-btn bordered', type: 'button', 'aria-label': 'Прослухати додатковий голос', title: 'Прослухати додатковий голос',
    onClick: () => { if (el.extraVoice.value) preview({ engine: s.engine, voice: el.extraVoice.value, text: 'Це пояснення в дужках, яке читає додатковий голос.', btn: el.extraPreview }); } }, icon('play'));
  el.extraGain = h('div', { class: 'extra-gain' },
    rangeRow('brackets_gain_db', 'Гучність додаткового голосу', { min: -12, max: 0, step: 1, fmt: dbText }));
  return h('div', { class: 'field extra-field' }, h('label', { for: id }, 'Додатковий голос'),
    h('div', { class: 'voice-row' }, el.extraVoice, el.extraPreview),
    h('p', { class: 'hint', id: `${id}-h` }, 'Читає особливі частини тексту — зараз текст у дужках.'), el.extraGain);
}

const dbText = (v) => `${v < 0 ? '−' : ''}${Math.abs(v)} дБ`;
const isSingle = () => s.engine === 'styletts2' && s.model === 'single';
const extraVoice = () => (isSingle() ? null : s.extra_voice || null);

function fillExtraVoices() {
  if (!info || !el.extraVoice) return;
  const list = voiceList(info, s.engine);
  el.extraVoice.replaceChildren(h('option', { value: '' }, 'Немає'), ...voiceOptions(s.engine));
  if (s.extra_voice && !list.some((v) => v.name === s.extra_voice)) s = settings.save({ extra_voice: null });
  el.extraVoice.value = s.extra_voice || '';
  syncBrackets();
}

/** «Текст у дужках»: Читати | Пропускати; when read, two independent switches: announce, additional voice. */
function bracketsControls() {
  el.bracketsRead = h('div', { class: 'seg' }, [['read', 'Читати'], ['skip', 'Пропускати']].map(([v, label]) => h('label', null,
    h('input', { type: 'radio', name: 'brackets-mode', value: v, onChange: () => { s = settings.save({ brackets: v === 'read' ? 'aside' : 'skip' }); syncBrackets(); } }),
    h('span', null, label))));
  el.bracketsHint = h('p', { class: 'hint' });
  const announce = switchRow('brackets_announce', 'Оголошувати «у дужках»', 'Перед текстом основний голос каже «у дужках:»', {
    checked: !!s.brackets_announce, onChange: (v) => { s = settings.save({ brackets_announce: v }); },
  });
  el.bracketsVoiceRow = switchRow('brackets_voice_on', 'Іншим голосом', 'Читає додатковий голос', {
    checked: !!s.brackets_voice_on, onChange: (v) => { s = settings.save({ brackets_voice_on: v }); syncBrackets(); },
  });
  el.bracketsVoiceHint = el.bracketsVoiceRow.querySelector('.hint');
  el.bracketsOptions = h('div', { class: 'brackets-options' }, announce, el.bracketsVoiceRow);
  syncBrackets();
  return h('fieldset', { class: 'brackets-field' }, h('legend', null, 'Текст у дужках'), el.bracketsRead, el.bracketsHint, el.bracketsOptions);
}

function syncBrackets() {
  if (el.extraVoice) {
    el.extraVoice.disabled = !info || isSingle();
    el.extraPreview.disabled = !extraVoice();
    el.extraGain.hidden = !extraVoice();
  }
  if (!el.bracketsRead) return;
  const read = s.brackets !== 'skip';
  for (const input of el.bracketsRead.querySelectorAll('input')) input.checked = input.value === (read ? 'read' : 'skip');
  el.bracketsHint.textContent = read ? 'Читається з короткими паузами, як вставне речення.' : 'Текст у дужках не читається.';
  el.bracketsOptions.hidden = !read;
  el.brackets_announce.checked = !!s.brackets_announce;
  const extra = extraVoice();
  el.brackets_voice_on.disabled = !extra;
  el.brackets_voice_on.checked = !!(extra && s.brackets_voice_on);
  el.bracketsVoiceRow.classList.toggle('disabled', !extra);
  el.bracketsVoiceHint.textContent = extra
    ? `Читає додатковий голос: ${voiceLabel((info && voiceList(info, s.engine).find((v) => v.name === extra)) || { name: extra })}` // info is null on the first render
    : isSingle() ? 'Недоступно з моделлю single' : 'Спершу виберіть додатковий голос';
}

function syncVoiceEnabled() {
  const single = s.engine === 'styletts2' && s.model === 'single';
  el.voice.disabled = !info || single;
  el.previewBtn.disabled = !info;
}

function syncForm() {
  if (!built) return;
  for (const input of el.engineGroup.querySelectorAll('input')) input.checked = input.value === s.engine;
  for (const input of el.response_formatGroup.querySelectorAll('input')) input.checked = input.value === s.response_format;
  for (const k of ['verbalize', 'english', 'clean']) if (el[k]) el[k].checked = !!s[k];
  syncInitials();
  el.speed.value = s.speed; el.speedOut.textContent = `${Number(s.speed).toFixed(2)}×`;
  el.pause_ms.value = s.pause_ms; el.pause_msOut.textContent = `${s.pause_ms} мс`;
  el.singleInput.checked = s.model === 'single';
  el.brackets_gain_db.value = s.brackets_gain_db;
  el.brackets_gain_dbOut.textContent = dbText(s.brackets_gain_db);
  syncBrackets();
  fillVoices();
  updateSummary();
}

function updateCounter() {
  const n = el.text.value.length;
  el.counter.textContent = `${fmtNum(n)} / ${fmtNum(MAX_CHARS)}`;
  el.counter.classList.toggle('over', n > MAX_CHARS);
  el.text.setAttribute('aria-invalid', n > MAX_CHARS ? 'true' : 'false');
}

function updateSummary() {
  if (!el.summary) return;
  el.summary.textContent = [`${Number(s.speed).toFixed(2)}×`, `${s.pause_ms} мс`, String(s.response_format).toUpperCase()].join(' · ');
}

async function onPaste() {
  try {
    const t = await navigator.clipboard.readText();
    if (!t) { toast('Буфер обміну порожній'); return; }
    const { selectionStart: a, selectionEnd: b, value } = el.text;
    el.text.value = value.slice(0, a) + t + value.slice(b);
    el.text.selectionStart = el.text.selectionEnd = a + t.length;
    settings.saveDraft(el.text.value);
    updateCounter();
    el.text.focus();
  } catch {
    toast('Немає доступу до буфера обміну — вставте вручну (утримуйте поле → Вставити).', { type: 'error' });
  }
}

async function onFile() {
  const file = el.file.files[0];
  el.file.value = '';
  if (!file) return;
  if (file.size > 3 * 1024 * 1024) { toast('Файл завеликий (понад 3 МБ).', { type: 'error' }); return; }
  try {
    const text = (await readFile(file)).replace(/^﻿/, '').replace(/\r\n?/g, '\n');
    const prev = el.text.value;
    el.text.value = text;
    if (!el.titleInput.value) el.titleInput.value = file.name.replace(/\.(txt|md|markdown|text)$/i, '');
    settings.saveDraft(text);
    updateCounter();
    toast(`Відкрито «${file.name}»`, prev ? { action: { label: 'Повернути', fn: () => { el.text.value = prev; settings.saveDraft(prev); updateCounter(); } } } : {});
  } catch (e) {
    toast(`Не вдалося прочитати файл: ${e.message}`, { type: 'error' });
  }
}

function onClear() {
  const prev = el.text.value;
  if (!prev) return;
  el.text.value = '';
  el.titleInput.value = '';
  settings.saveDraft('');
  updateCounter();
  el.text.focus();
  toast('Текст очищено', { action: { label: 'Повернути', fn: () => { el.text.value = prev; settings.saveDraft(prev); updateCounter(); } } });
}

function closeJob() {
  current.clearCurrent();
  if (jobView) jobView.destroy();
  el.ws.classList.remove('has-job');
  el.text.focus();
}

async function go() {
  const text = el.text.value.trim();
  if (!text) { toast('Введіть текст для озвучення', { type: 'error' }); el.text.focus(); return; }
  if (text.length > MAX_CHARS) { toast(`Текст задовгий: максимум ${fmtNum(MAX_CHARS)} символів`, { type: 'error' }); return; }
  unlockAudio(); // inside the click: required by iOS Safari before any Web Audio playback
  setBusy(el.go, true);
  const single = s.engine === 'styletts2' && s.model === 'single';
  const body = {
    text, engine: s.engine || undefined, voice: single ? undefined : (el.voice.value || s.voice || undefined),
    model: s.engine === 'styletts2' ? s.model : undefined, speed: Number(s.speed), pause_ms: Number(s.pause_ms),
    verbalize: !!s.verbalize, english: !!s.english, clean: !!s.clean, response_format: s.response_format,
    initials: s.initials === 'skip' ? 'skip' : 'letters',
  };
  const extra = extraVoice();
  if (s.brackets === 'skip') body.brackets = 'skip';
  else {
    body.brackets = s.brackets_voice_on && extra ? 'voice' : 'aside';
    body.brackets_announce = !!s.brackets_announce;
    if (body.brackets === 'voice') Object.assign(body, { brackets_voice: extra, brackets_gain_db: Number(s.brackets_gain_db) });
  }
  const title = el.titleInput.value.trim();
  if (title) body.title = title;
  try {
    const job = await api('/v1/jobs', { method: 'POST', json: body });
    el.titleInput.value = '';
    current.setCurrent(job.id);
    jobPanel();
    el.ws.classList.add('has-job');
    jobView.open(job.id, { job, autoplay: true });
    // side-by-side layouts show the job at once; stacked ones scroll to it
    if (el.job.getBoundingClientRect().top > window.innerHeight * 0.6) {
      requestAnimationFrame(() => el.job.scrollIntoView({ behavior: 'smooth', block: 'start' }));
    }
  } catch (e) {
    toast(e.message, { type: 'error' });
  } finally {
    setBusy(el.go, false);
  }
}
