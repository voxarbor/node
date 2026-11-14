// «Інструменти»: check text, English names, stress lexicon, voices. One frame for every tool: a header row
// (title, description, actions) and full-width panes that fill the content height on desktop.
import { api } from './api.js';
import * as settings from './settings.js';
import { btn, debounce, dialog, h, icon, nextId, setBusy, toast } from './util.js';
import { ENGINE_HINTS, ENGINE_LABELS, getVoices, PREVIEW_TEXT, preview, voiceGroups, voiceTag } from './voices.js';

const TABS = [
  ['check', 'Перевірити текст', 'Що саме прочитає голос'],
  ['names', 'Англійські назви', 'Вимова латиниці кирилицею'],
  ['lexicon', 'Словник наголосів', 'Власні правила наголосу'],
  ['voices', 'Голоси', 'Прослухати й вибрати'],
];
let root = null;
let namesPrefill = '';

export function show(el, params) {
  root = el;
  const tab = TABS.some(([t]) => t === params[0]) ? params[0] : 'check';
  const panel = h('div', { id: 'tool-panel', class: 'tool-panel' });
  root.replaceChildren(h('div', { class: 'tools' },
    h('nav', { class: 'subtabs', 'aria-labelledby': 'h-tools' },
      h('h1', { id: 'h-tools', class: 'panel-title' }, 'Інструменти'),
      TABS.map(([t, label, desc]) => h('a', { href: `#/tools/${t}`, 'aria-current': t === tab ? 'page' : null },
        h('span', { class: 'st-label' }, label), h('span', { class: 'st-desc' }, desc)))),
    panel));
  ({ check, names, lexicon, voices })[tab](panel);
}
export function hide() {}

/** The common tool frame. */
function frame(panel, { title, desc, actions = [], bar = null, body, cls = '' }) {
  const t = h('section', { class: `panel tool ${cls}`, 'aria-label': title },
    h('header', { class: 'tool-head' },
      h('div', { class: 'tool-headline' }, h('h2', null, title), h('p', { class: 'desc' }, desc)),
      actions.length ? h('div', { class: 'tool-actions' }, actions) : null),
    bar ? h('div', { class: 'tool-bar' }, bar) : null,
    h('div', { class: 'tool-body' }, body));
  panel.replaceChildren(t);
  return t;
}

const pane = (title, { extra = null, body, foot = null, cls = '' }) => h('section', { class: `pane ${cls}`, 'aria-label': title },
  h('div', { class: 'pane-head' }, h('h3', { class: 'section-label' }, title), extra),
  body, foot ? h('div', { class: 'pane-foot' }, foot) : null);

function toggle(name, label, value) {
  const id = nextId(name);
  const input = h('input', { type: 'checkbox', id, role: 'switch', checked: value });
  return { input, el: h('label', { class: 'switch', for: id }, input, h('span', { class: 'track', 'aria-hidden': 'true' }), h('span', { class: 'sw-text' }, h('b', null, label))) };
}

const errorBox = () => h('p', { class: 'form-error', role: 'alert' });
const emptyNote = (...children) => h('div', { class: 'empty-note' }, ...children);

// stress marks: combining acute (from the stress model) or "+"/"++" (user / lexicon)
const STRESS = /([аеєиіїоуюяАЕЄИІЇОУЮЯaeiouyAEIOUY])(́|\+\+?)/g;
const plain = (s) => String(s || '').replace(/́|\+/g, '').toLowerCase();

function stressNodes(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(STRESS)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(h('span', { class: 'st' }, `${m[1]}\u0301`));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// ---------------------------------------------------------------- check text
function check(panel) {
  const s = settings.load();
  const id = nextId('chk');
  const text = h('textarea', { id, class: 'fill', lang: 'uk', placeholder: 'Вставте текст, щоб побачити, як його прочитає голос.', spellcheck: 'true' });
  text.value = settings.loadDraft();
  const counter = h('span', { class: 'pane-meta' });
  const updateCount = () => { counter.textContent = `${text.value.length.toLocaleString('uk-UA')} симв.`; };
  text.addEventListener('input', updateCount);
  updateCount();
  const verb = toggle('verbalize', 'Числа словами', s.verbalize);
  const eng = toggle('english', 'Англійські назви', s.english);
  const cln = toggle('clean', 'Очистити Markdown', s.clean);
  const ini = toggle('initials', 'Пропускати ініціали', s.initials === 'skip');
  const brackets = h('div', { class: 'seg small-seg', role: 'radiogroup', 'aria-label': 'Текст у дужках' },
    [['read', 'Читати'], ['skip', 'Пропускати']].map(([v, label]) => h('label', null,
      h('input', { type: 'radio', name: 'chk-brackets', value: v, checked: (s.brackets === 'skip') === (v === 'skip') }), h('span', null, label))));
  Object.defineProperty(brackets, 'value', { get: () => brackets.querySelector('input:checked')?.value || 'read' });
  const ann = toggle('brackets_announce', 'Оголошувати «у дужках»', !!s.brackets_announce);
  const syncAnn = () => { ann.el.hidden = brackets.value === 'skip'; };
  brackets.addEventListener('change', syncAnn);
  syncAnn();
  const summary = h('span', { class: 'pane-meta' });
  const out = h('div', { class: 'pane-body', 'aria-live': 'polite' },
    emptyNote(h('p', null, 'Тут з’явиться текст так, як його прочитає голос:'),
      h('ul', null, h('li', null, 'числа, дати й скорочення — словами;'), h('li', null, 'наголоси — позначкою над голосною;'),
        h('li', null, 'англійські назви — виділені, з українською вимовою.')),
      h('p', null, 'Натисніть «Перевірити» або Ctrl+Enter.')));
  const err = errorBox();
  const run = btn('Перевірити', { icon: 'check', cls: 'btn primary', onClick: go });
  text.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(); } });

  async function go() {
    const t = text.value.trim();
    err.textContent = '';
    if (!t) { err.textContent = 'Введіть текст.'; text.focus(); return; }
    setBusy(run, true);
    try {
      const r = await api('/v1/text/prepare', { method: 'POST', json: { text: t, verbalize: verb.input.checked, english: eng.input.checked, clean: cln.input.checked, initials: ini.input.checked ? 'skip' : 'letters', brackets: brackets.value === 'skip' ? 'skip' : 'aside', brackets_announce: brackets.value !== 'skip' && ann.input.checked } });
      render(t, r);
    } catch (e) {
      err.textContent = e.message;
    } finally { setBusy(run, false); }
  }

  function render(original, r) {
    const names = r.names || [];
    const byUk = new Map(names.map((n) => [plain(n.uk), n]));
    const sentences = r.stressed || [];
    // keep the input's paragraphs: find each sentence in the original (stress marks removed)
    const orig = plain(original);
    const paras = [];
    let p = [];
    let cursor = 0;
    for (const sent of sentences) {
      const probe = plain(sent).replace(/^[^\p{L}\d]+/u, '').slice(0, 12);
      const at = probe ? orig.indexOf(probe, cursor) : -1;
      if (at >= 0 && at - cursor < 3000) {
        if (/\n\s*\n/.test(orig.slice(cursor, at)) && p.length) { paras.push(p); p = []; }
        cursor = at + probe.length;
      }
      p.push(sent);
    }
    if (p.length) paras.push(p);
    const sentenceEl = (sent) => h('span', { class: 'sentence' }, sent.split(/(\s+)/).map((tok) => {
      const n = byUk.get(plain(tok).replace(/^[^\p{L}]+|[^\p{L}]+$/gu, ''));
      return n ? h('mark', { class: 'name', title: `${n.word} → ${n.uk}` }, stressNodes(tok)) : stressNodes(tok);
    }));
    const blocks = paras.map((sents) => h('p', null, sents.flatMap((x, i) => (i ? [' ', sentenceEl(x)] : [sentenceEl(x)]))));
    summary.textContent = `${sentences.length} реч. · ${names.length} назв · ${(r.text || '').length.toLocaleString('uk-UA')} симв.`;
    const namesTable = names.length ? h('div', { class: 'names-found' }, h('h4', { class: 'section-label' }, 'Англійські назви'),
      h('table', { class: 'data' }, h('thead', null, h('tr', null, h('th', null, 'Слово'), h('th', null, 'Як прочитає'), h('th', null, h('span', { class: 'sr-only' }, 'Дія')))),
        h('tbody', null, names.map((n) => h('tr', null, h('td', null, n.word), h('td', { lang: 'uk' }, stressNodes(n.uk)),
          h('td', { class: 'right' }, h('a', { href: '#/tools/names', onClick: () => { namesPrefill = n.word; } }, 'Виправити'))))))) : null;
    out.replaceChildren(...[h('div', { class: 'reading', lang: 'uk' }, blocks), namesTable].filter(Boolean)); // native replaceChildren prints null as text
  }

  frame(panel, {
    title: 'Перевірити текст', desc: 'Покаже точно те, що прочитає голос: числа словами, наголоси, англійські назви.',
    actions: [btn('Взяти з головної', { icon: 'paste', cls: 'btn small', onClick: () => { text.value = settings.loadDraft(); updateCount(); } })],
    cls: 'tool-check',
    body: h('div', { class: 'split even' },
      pane('Текст', { extra: counter, body: text, foot: [verb.el, eng.el, cln.el, ini.el, h('div', { class: 'inline-label' }, h('span', { 'aria-hidden': 'true' }, 'Дужки'), brackets), ann.el, h('span', { class: 'grow' }), run, err] }),
      pane('Що прочитає голос', { extra: summary, body: out })),
  });
}

// ---------------------------------------------------------------- English names
function names(panel) {
  const id = nextId('names');
  const text = h('textarea', { id, class: 'fill', placeholder: 'Наприклад: GitHub, Kubernetes, Elon Musk, iPhone — або вставте цілий текст.', spellcheck: 'false' });
  if (namesPrefill) { text.value = namesPrefill; namesPrefill = ''; }
  const lang = h('select', { 'aria-label': 'Вимова', class: 'auto' }, h('option', { value: 'en-us' }, 'американська'), h('option', { value: 'en-gb' }, 'британська'));
  const err = errorBox();
  const count = h('span', { class: 'pane-meta' });
  const tbody = h('tbody');
  const table = h('table', { class: 'data names-table' },
    h('thead', null, h('tr', null, h('th', null, 'Слово'), h('th', null, 'IPA'), h('th', null, 'Українською'), h('th', null, h('span', { class: 'sr-only' }, 'Дії')))),
    tbody);
  const body = h('div', { class: 'pane-body' }, emptyNote(
    h('p', null, 'Введіть ліворуч англійські назви (або вставте текст — латинські слова знайдуться самі) і натисніть «Розібрати».'),
    h('p', null, 'Для кожного слова буде фонетика espeak-ng і кирилиця з наголосом «+». Виправте написання і додайте в словник — далі голос завжди читатиме назву так.')));
  const run = btn('Розібрати', { icon: 'search', cls: 'btn primary', onClick: go });
  let rules = [];

  async function go() {
    err.textContent = '';
    const t = text.value.trim();
    if (!t) { err.textContent = 'Введіть слова або текст з англійськими назвами.'; text.focus(); return; }
    setBusy(run, true);
    try {
      const [r, lex] = await Promise.all([
        api('/v1/text/phonemize', { method: 'POST', json: { text: t, lang: lang.value } }),
        api('/v1/lexicon').catch(() => ({ rules: [] })),
      ]);
      rules = lex.rules || [];
      const items = r.items || [];
      tbody.replaceChildren(...items.map(row));
      count.textContent = items.length ? `${items.length} сл.` : '';
      body.replaceChildren(items.length ? table : emptyNote(h('p', null, 'Латинських слів не знайдено.')));
      body.classList.toggle('flush', items.length > 0);
    } catch (e) { err.textContent = e.message; } finally { setBusy(run, false); }
  }

  function row(item) {
    const existing = rules.find((r) => r.split('=>')[0].trim() === item.word);
    const saved = existing ? existing.split('=>').slice(1).join('=>').trim() : null;
    const input = h('input', { type: 'text', value: saved || item.uk || '', lang: 'uk', 'aria-label': `Українське написання для ${item.word}` });
    const rowErr = h('div', { class: 'form-error row-error', role: 'alert' });
    const play = h('button', { class: 'icon-btn bordered', type: 'button', 'aria-label': `Прослухати ${item.word}`, title: 'Прослухати' }, icon('play'));
    play.addEventListener('click', () => {
      const s = settings.load();
      preview({ engine: s.engine || 'holos', voice: s.voice, model: s.model, text: input.value || item.word, btn: play });
    });
    const add = btn('', { cls: 'btn small' });
    let current = saved;
    const sync = () => {
      const same = current !== null && input.value.trim() === current;
      add.replaceChildren(icon(same ? 'check' : 'plus'), h('span', null, same ? 'У словнику' : current !== null ? 'Оновити' : 'У словник'));
      add.classList.toggle('done', same);
      add.disabled = same;
    };
    input.addEventListener('input', sync);
    sync();
    add.addEventListener('click', async () => {
      rowErr.textContent = '';
      const uk = input.value.trim();
      if (!uk) { rowErr.textContent = 'Введіть українське написання.'; return; }
      if (!uk.includes('+')) { rowErr.textContent = 'Позначте наголос «+» після наголошеної голосної.'; input.focus(); return; }
      setBusy(add, true);
      try {
        if (current !== null) await api('/v1/lexicon', { method: 'DELETE', json: { rule: `${item.word} => ${current}` } }).catch(() => {});
        await api('/v1/lexicon', { method: 'POST', json: { rule: `${item.word} => ${uk}` } });
        current = uk;
        toast(`Додано: ${item.word} => ${uk}`, { type: 'ok' });
      } catch (e) { rowErr.textContent = ruleError(e.message); } finally { setBusy(add, false); sync(); }
    });
    return h('tr', null, h('td', { class: 'word' }, item.word), h('td', { class: 'ipa mono' }, item.ipa || ''),
      h('td', { class: 'uk' }, input, rowErr), h('td', { class: 'cell-actions' }, h('div', { class: 'row' }, play, add)));
  }

  text.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(); } });
  frame(panel, {
    title: 'Англійські назви', desc: 'Як українські голоси вимовлять латиницю: фонетика espeak-ng → кирилиця з наголосом.',
    cls: 'tool-names',
    body: h('div', { class: 'split names-split' },
      pane('Слова або текст', { body: text, foot: [h('label', { class: 'inline-label' }, 'Вимова', lang), h('span', { class: 'grow' }), run, err] }),
      pane('Вимова', { extra: count, body })),
  });
  if (text.value) go();
}

// ---------------------------------------------------------------- lexicon
const RULE_ERRORS = [
  [/missing '=>'/, 'немає «=>» між лівою і правою частинами'],
  [/right side has no '\+' stress mark/, 'у правій частині немає наголосу «+»'],
  [/right side must be the left side with '\+' added/, 'права частина має бути лівою з доданим «+» (ті самі літери)'],
  [/bad regex \((.*)\)/, 'помилка в регулярному виразі: $1'],
  [/empty side/, 'ліва або права частина порожня'],
  [/name rule: write the right side in Cyrillic/, 'для англійської назви права частина має бути кирилицею'],
];

/** API rule errors are English ("line 3: missing '=>': ..."): translate the known ones, keep the rest. */
export function ruleError(msg) {
  const m = String(msg || '');
  for (const [re, uk] of RULE_ERRORS) {
    const hit = m.match(re);
    if (hit) return `Правило не прийнято: ${uk.replace('$1', hit[1] || '')}.`;
  }
  return m;
}

const KINDS = { regex: 'regex', name: 'назва', replace: 'заміна', phrase: 'фраза', word: 'слово' };
function ruleKind(rule) {
  if (rule.trim().startsWith('~')) return 'regex';
  const left = rule.split('=>')[0].trim();
  if (/[A-Za-z]/.test(left)) return 'name';
  const right = rule.split('=>').slice(1).join('=>').trim();
  if (right.replace(/\+/g, '').toLowerCase() !== left.toLowerCase()) return 'replace';   // same check as the API
  return /\s/.test(left) ? 'phrase' : 'word';
}

function lexicon(panel) {
  const id = nextId('rule');
  const input = h('input', { type: 'text', id, class: 'mono', placeholder: 'наприкінці => наприкі+нці', autocomplete: 'off', spellcheck: 'false', 'aria-describedby': `${id}-e ${id}-k` });
  const kind = h('span', { class: 'kind', id: `${id}-k`, 'aria-live': 'polite' }, '—');
  const err = h('p', { class: 'form-error', id: `${id}-e`, role: 'alert' });
  const filter = h('input', { type: 'search', placeholder: 'Фільтр правил', 'aria-label': 'Фільтр правил' });
  const count = h('span', { class: 'pane-meta' });
  const tbody = h('tbody');
  let rules = [];
  const addBtn = btn('Додати', { icon: 'plus', cls: 'btn primary', type: 'submit' });
  const help = h('div', { class: 'rule-help', id: `${id}-help`, hidden: true },
    h('dl', null,
      h('div', null, h('dt', null, 'Слово'), h('dd', null, h('code', null, 'наприкінці => наприкі+нці'), h('span', null, 'кожне входження, будь-який регістр'))),
      h('div', null, h('dt', null, 'Фраза'), h('dd', null, h('code', null, 'замок на дверях => замо+к на дверях'), h('span', null, 'ціла фраза, справа та сама з «+»'))),
      h('div', null, h('dt', null, 'Regex'), h('dd', null, h('code', null, '~ (?<=\\bваш\\s)замок\\b => за+мок'), h('span', null, 'збіг замінюється правою частиною'))),
      h('div', null, h('dt', null, 'Назва'), h('dd', null, h('code', null, 'GitHub => ґі+тхаб'), h('span', null, 'латиниця зліва, кирилиця з «+» справа'))),
      h('div', null, h('dt', null, 'Заміна'), h('dd', null, h('code', null, 'Т. Г. Шевченко => Тарас Григорович Шевченко'), h('code', null, 'т. зв. => так зва+ний'),
        h('span', null, 'будь-який текст → що сказати; «+» необов’язковий, «Т.Г.» = «Т. Г.»')))),
    h('p', { class: 'hint' }, 'Повні імена для ініціалів записуйте заміною — вона сильніша за перемикач «Пропускати ініціали».'),
    h('p', { class: 'hint' }, '«++» ставить наголос і подовжує голосну (за++мок). «+», поставлений прямо в тексті, завжди сильніший за словник.'));
  const helpBtn = btn('Як писати правила', { icon: 'text', cls: 'btn small', 'aria-expanded': 'false', 'aria-controls': `${id}-help` });
  helpBtn.addEventListener('click', () => { help.hidden = !help.hidden; helpBtn.setAttribute('aria-expanded', String(!help.hidden)); });

  const updateKind = () => {
    const v = input.value.trim();
    kind.textContent = v ? (v.includes('=>') ? KINDS[ruleKind(v)] : 'без «=>»') : '—';
    kind.className = `kind${v && v.includes('=>') ? ` k-${ruleKind(v)}` : ''}`;
  };
  input.addEventListener('input', () => { updateKind(); err.textContent = ''; input.removeAttribute('aria-invalid'); });

  function render() {
    const q = filter.value.trim().toLowerCase();
    const shown = rules.filter((r) => !q || r.toLowerCase().includes(q));
    tbody.replaceChildren(...shown.map((r) => {
      const k = ruleKind(r);
      const [left, ...rest] = r.split('=>');
      const find = k === 'regex' ? left.trim().replace(/^~\s*/, '') : left.trim();
      const del = btn('Видалити правило', { icon: 'trash', cls: 'icon-btn', iconOnly: true });
      del.addEventListener('click', async () => {
        if (!(await dialog({ title: 'Видалити правило?', message: r, ok: 'Видалити', danger: true }))) return;
        try {
          rules = (await api('/v1/lexicon', { method: 'DELETE', json: { rule: r } })).rules || [];
          render();
          toast('Правило видалено', { type: 'ok' });
        } catch (e) { toast(e.message, { type: 'error' }); }
      });
      return h('tr', null, h('td', { class: 'c-kind' }, h('span', { class: `kind k-${k}` }, KINDS[k])),
        h('td', { class: 'c-find' }, h('code', null, find)), h('td', { class: 'c-read' }, h('code', null, stressNodes(rest.join('=>').trim()))),
        h('td', { class: 'c-del' }, del));
    }));
    if (!shown.length) tbody.replaceChildren(h('tr', null, h('td', { colspan: 4, class: 'muted empty-cell' }, rules.length ? 'Нічого не знайдено.' : 'Правил ще немає.')));
    count.textContent = q ? `${shown.length} з ${rules.length}` : `${rules.length} правил`;
  }

  async function load() {
    try {
      rules = (await api('/v1/lexicon')).rules || [];
      render();
    } catch (e) {
      tbody.replaceChildren(h('tr', null, h('td', { colspan: 4, class: 'form-error' }, e.message)));
    }
  }

  const form = h('form', { class: 'rule-form', novalidate: true },
    h('label', { for: id, class: 'inline-label' }, 'Нове правило'), h('div', { class: 'rule-input' }, input, kind), addBtn);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.textContent = '';
    const rule = input.value.trim();
    if (!rule) { err.textContent = 'Введіть правило.'; input.focus(); return; }
    if (!rule.includes('=>')) { err.textContent = 'Правило має містити «=>»: ліва частина => права з «+».'; input.setAttribute('aria-invalid', 'true'); input.focus(); return; }
    setBusy(addBtn, true);
    try {
      rules = (await api('/v1/lexicon', { method: 'POST', json: { rule } })).rules || [];
      input.value = '';
      updateKind();
      render();
      toast('Правило додано', { type: 'ok' });
    } catch (ex) {
      err.textContent = ruleError(ex.message);
      input.setAttribute('aria-invalid', 'true');
      input.focus();
    } finally { setBusy(addBtn, false); }
  });
  filter.addEventListener('input', render);

  frame(panel, {
    title: 'Словник наголосів', desc: 'Правила застосовуються до тексту перед автоматичними наголосами.',
    actions: [helpBtn], cls: 'tool-lexicon',
    bar: [form, err, help],
    body: pane('Правила', {
      extra: [count, h('div', { class: 'search pane-search' }, icon('search'), filter)],
      body: h('div', { class: 'pane-body flush' }, h('table', { class: 'data rules-table' },
        h('thead', null, h('tr', null, h('th', { class: 'c-kind' }, 'Тип'), h('th', null, 'Що шукати'), h('th', null, 'Як читати'), h('th', { class: 'c-del' }, h('span', { class: 'sr-only' }, 'Видалити')))),
        tbody)),
    }),
  });
  tbody.replaceChildren(h('tr', null, h('td', { colspan: 4, class: 'muted empty-cell' }, 'Завантаження…')));
  load();
}

// ---------------------------------------------------------------- voices
function voices(panel) {
  let info = null;
  let engine = settings.load().engine;
  const sample = h('input', { type: 'text', value: PREVIEW_TEXT, lang: 'uk', 'aria-label': 'Текст для прослуховування' });
  const search = h('input', { type: 'search', placeholder: 'Пошук голосу', 'aria-label': 'Пошук голосу' });
  const tabs = h('div', { class: 'seg', role: 'radiogroup', 'aria-label': 'Рушій' });
  const list = h('div', { class: 'pane-body voices-body' }, h('p', { class: 'muted' }, 'Завантаження…'));
  const count = h('span', { class: 'pane-meta' });
  const genderFilter = h('div', { class: 'seg small-seg', role: 'radiogroup', 'aria-label': 'Стать голосу' },
    [['all', 'Усі'], ['male', 'Чоловічі'], ['female', 'Жіночі']].map(([v, label]) => h('label', null,
      h('input', { type: 'radio', name: 'v-gender', value: v, checked: v === 'all', onChange: () => info && render() }), h('span', null, label))));

  function row(v) {
    const s = settings.load();
    const selected = s.engine === engine && s.voice === v.name && s.model !== 'single';
    const name = v.label;
    const play = h('button', { class: 'icon-btn bordered', type: 'button', 'aria-label': `Прослухати: ${name}`, title: 'Прослухати' }, icon('play'));
    play.addEventListener('click', () => preview({ engine, voice: v.name, text: sample.value.trim() || PREVIEW_TEXT, btn: play }));
    const pick = selected
      ? h('span', { class: 'picked' }, icon('check'), h('span', null, 'Вибрано'))
      : btn('Вибрати', { cls: 'btn small' });
    if (!selected) {
      pick.addEventListener('click', () => {
        settings.save({ engine, voice: v.name, model: 'multi' });
        toast(`Голос «${name}» вибрано для озвучення`, { type: 'ok' });
        render();
      });
    }
    return h('li', { class: `vrow${selected ? ' selected' : ''}`, title: name !== v.name ? v.name : null },
      play, h('span', { class: 'vname', title: name }, name), h('span', { class: 'vlabel' }, voiceTag(v)),
      h('span', { class: 'vstar', 'aria-label': v.favorite ? 'улюблений' : null }, v.favorite ? '★' : ''), pick);
  }

  function render() {
    tabs.replaceChildren(...Object.keys(info.engines).sort((a, b) => (b === info.default_engine) - (a === info.default_engine)).map((e) => h('label', { title: ENGINE_HINTS[e] },
      h('input', { type: 'radio', name: 'v-engine', value: e, checked: e === engine, onChange: () => { engine = e; render(); } }),
      h('span', null, `${ENGINE_LABELS[e] || e} · ${info.engines[e].voices.length}`))));
    const q = search.value.trim().toLowerCase();
    const g = genderFilter.querySelector('input:checked')?.value || 'all';
    const groups = voiceGroups(info, engine, (v) => (g === 'all' || v.gender === g)
      && (!q || [v.name, v.label, v.aliasLabel].some((x) => x && x.toLowerCase().includes(q))));
    const n = groups.reduce((k, gr) => k + gr.voices.length, 0);
    count.textContent = `${n} голосів`;
    list.replaceChildren(...groups.flatMap((gr) => [
      h('h3', { class: 'section-label group-label' }, `${gr.label} · ${gr.voices.length}`),
      h('ul', { class: 'vlist' }, gr.voices.map(row)),
    ]));
    if (!n) list.replaceChildren(emptyNote(h('p', null, 'Нічого не знайдено.')));
  }
  search.addEventListener('input', debounce(() => info && render(), 120));

  frame(panel, {
    title: 'Голоси', desc: 'Прослухайте голоси обох рушіїв і виберіть голос для озвучення.', cls: 'tool-voices',
    bar: [h('div', { class: 'bar-field' }, h('span', { class: 'inline-label' }, 'Рушій'), tabs),
      h('label', { class: 'bar-field grow' }, h('span', { class: 'inline-label' }, 'Текст'), sample),
      genderFilter, h('div', { class: 'search bar-search' }, icon('search'), search)],
    body: pane('Список', { extra: count, body: list }),
  });
  getVoices().then((i) => {
    info = i;
    if (!engine || !info.engines[engine]) engine = info.default_engine || Object.keys(info.engines)[0];
    render();
  }).catch((e) => list.replaceChildren(h('p', { class: 'form-error' }, e.message)));
}
