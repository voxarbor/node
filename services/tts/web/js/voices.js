// Voice list (/v1/voices) and short previews via /v1/audio/speech
import { api } from './api.js';
import { icon, toast } from './util.js';

export const ENGINE_LABELS = { holos: 'HolosTTS', styletts2: 'StyleTTS2' };
export const ENGINE_HINTS = { holos: 'швидший, 27 голосів', styletts2: 'повільніший, 31 голос' };
// purpose aliases only: gender aliases (male2, female…) would clash with the numbered labels («Чоловічий 13»)
const PURPOSE_LABELS = { history: 'історія, факти', news: 'новини', tale: 'казки' };
const GENDER_ALIAS = /^(male|female|child)\d*$/;
export const GENDER_GROUPS = [['male', 'Чоловічі'], ['female', 'Жіночі'], ['child', 'Дитячі']];
export const PREVIEW_TEXT = 'Привіт! Так звучить цей голос. Сьогодні чудова погода для прогулянки.';

let voicesPromise = null;
let metaByEngine = {};   // engine -> {voice: {gender, label}} once /v1/voices has loaded
export function getVoices() {
  if (!voicesPromise) {
    voicesPromise = api('/v1/voices').then((info) => {
      metaByEngine = Object.fromEntries(Object.entries(info.engines || {}).map(([e, v]) => [e, v.meta || {}]));
      return info;
    }).catch((e) => { voicesPromise = null; throw e; });
  }
  return voicesPromise;
}

/** Display name of a voice id: the API's label («Чоловічий 13» for Speaker_53), else the old «Голос 53». */
export function prettyVoice(name, engine = null) {
  if (!name) return '';
  const m = (engine && metaByEngine[engine]?.[name]) || Object.values(metaByEngine).map((x) => x[name]).find(Boolean);
  return m?.label || String(name).replace(/^Speaker_(\d+)$/, 'Голос $1');
}

/** [{name, label, gender, alias, aliasLabel (purpose), favorite, isDefault}] in API order (favorites first). */
export function voiceList(info, engine) {
  const e = info.engines[engine];
  if (!e) return [];
  const aliasOf = {};
  for (const [alias, name] of Object.entries(e.aliases || {})) {
    if (!GENDER_ALIAS.test(alias) && !(name in aliasOf)) aliasOf[name] = alias;
  }
  const fav = new Set(e.favorites || []);
  const meta = e.meta || {};
  return (e.voices || []).map((name) => ({
    name, label: meta[name]?.label || prettyVoice(name, engine), gender: meta[name]?.gender || null,
    alias: aliasOf[name] || null, aliasLabel: PURPOSE_LABELS[aliasOf[name]] || null,
    favorite: fav.has(name), isDefault: name === e.default,
  }));
}

/** Short tag for a voice: «за замовчуванням» and/or its purpose («історія, факти»). */
export const voiceTag = (v) => [v.isDefault ? 'за замовчуванням' : null, v.aliasLabel].filter(Boolean).join(' · ');

export function voiceLabel(v) {
  const tag = voiceTag(v);
  return `${v.favorite ? '★ ' : ''}${v.label || prettyVoice(v.name)}${tag ? ` · ${tag}` : ''}`;
}

const labelKey = (v) => {
  const m = /(\d+)$/.exec(v.label || '');
  return m ? [0, Number(m[1]), ''] : [1, 0, v.label || v.name];
};
const byLabel = (a, b) => {
  const [x, y] = [labelKey(a), labelKey(b)];
  return x[0] - y[0] || x[1] - y[1] || x[2].localeCompare(y[2], 'uk');
};

/** Picker / grid groups: «Улюблені» (API order), then «Чоловічі», «Жіночі», «Дитячі» sorted by number, then name.
 *  Without gender metadata (older API): «Улюблені» + «Усі голоси». `keep(v)` filters voices. */
export function voiceGroups(info, engine, keep = () => true) {
  const list = voiceList(info, engine).filter(keep);
  const fav = list.filter((v) => v.favorite);
  const rest = list.filter((v) => !v.favorite);
  const groups = [{ key: 'fav', label: 'Улюблені', voices: fav }];
  if (rest.some((v) => v.gender)) {
    for (const [g, label] of GENDER_GROUPS) groups.push({ key: g, label, voices: rest.filter((v) => v.gender === g).sort(byLabel) });
    const other = rest.filter((v) => !v.gender);
    if (other.length) groups.push({ key: 'other', label: 'Інші', voices: other.sort(byLabel) });
  } else {
    groups.push({ key: 'all', label: fav.length ? 'Усі голоси' : 'Голоси', voices: rest });
  }
  return groups.filter((gr) => gr.voices.length);
}

// ---------- previews: one shared <audio>, cached blobs
const cache = new Map();
let audio = null;
let playingBtn = null;

function resetBtn() {
  if (!playingBtn) return;
  playingBtn.replaceChildren(icon('play'));
  playingBtn.setAttribute('aria-label', playingBtn.dataset.label || 'Прослухати');
  playingBtn.setAttribute('aria-pressed', 'false');
  playingBtn = null;
}

export function stopPreview() {
  if (audio) audio.pause();
  resetBtn();
}

document.addEventListener('ozv-play', (e) => { if (e.detail !== 'preview') stopPreview(); });

/** Toggle a preview of `text` with engine/voice; btn shows play/stop and a spinner while synthesizing. */
export async function preview({ engine, voice, model = 'multi', text = PREVIEW_TEXT, btn }) {
  if (btn && btn === playingBtn) { stopPreview(); return; }
  stopPreview();
  document.dispatchEvent(new CustomEvent('ozv-play', { detail: 'preview' }));
  const key = JSON.stringify([engine, voice, model, text]);
  let url = cache.get(key);
  if (btn) { btn.dataset.label = btn.dataset.label || btn.getAttribute('aria-label') || 'Прослухати'; btn.setAttribute('aria-busy', 'true'); }
  try {
    if (!url) {
      const res = await api('/v1/audio/speech', {
        method: 'POST', raw: true,
        json: { text, engine, voice: model === 'single' ? undefined : voice, model, response_format: 'mp3', verbalize: true, pause_ms: 150 },
      });
      url = URL.createObjectURL(await res.blob());
      cache.set(key, url);
      if (cache.size > 40) { const [k, u] = cache.entries().next().value; URL.revokeObjectURL(u); cache.delete(k); }
    }
    audio = audio || new Audio();
    audio.src = url;
    audio.onended = resetBtn;
    await audio.play();
    if (btn) {
      playingBtn = btn;
      btn.replaceChildren(icon('stop'));
      btn.setAttribute('aria-label', 'Зупинити прослуховування');
      btn.setAttribute('aria-pressed', 'true');
    }
  } catch (e) {
    if (e.name !== 'AbortError') toast(e.status ? e.message : `Не вдалося відтворити: ${e.message}`, { type: 'error' });
  } finally {
    if (btn) btn.removeAttribute('aria-busy');
  }
}
