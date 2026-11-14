// Last-used synthesis settings and the text draft, per browser (localStorage may be unavailable: private mode etc.)
const KEY = 'ozv.settings.v1';
const DRAFT = 'ozv.draft.v1';

export const DEFAULTS = {
  engine: null, voice: null, model: 'multi', speed: 1, pause_ms: 150,
  verbalize: true, english: true, clean: true, response_format: 'mp3', initials: 'letters',   // initials: letters | skip
  extra_voice: null, brackets_gain_db: -3,          // «Додатковий голос» (for brackets now) and its volume
  brackets: 'aside', brackets_announce: false, brackets_voice_on: false,   // brackets: aside (read) | skip
};

/** API params -> {brackets: aside|skip|voice, brackets_announce}; the old value "announce" = aside + announce. */
export function normBrackets(p = {}) {
  const mode = p.brackets || 'aside';
  if (mode === 'announce') return { brackets: 'aside', brackets_announce: true };
  return { brackets: ['aside', 'skip', 'voice'].includes(mode) ? mode : 'aside', brackets_announce: mode !== 'skip' && !!p.brackets_announce };
}

/** Human description of a job's bracket handling, e.g. «Читати · оголошувати · іншим голосом». */
export function bracketsDesc(p = {}) {
  const { brackets, brackets_announce: ann } = normBrackets(p);
  if (brackets === 'skip') return 'Пропускати';
  return ['Читати', ann ? 'оголошувати' : null, brackets === 'voice' ? 'іншим голосом' : null].filter(Boolean).join(' · ');
}

let memory = null;
let draftMemory = '';
const listeners = new Set();

export function load() {
  if (memory) return { ...memory };
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch { /* unavailable */ }
  memory = { ...DEFAULTS, ...saved };
  if (memory.brackets === 'announce') Object.assign(memory, { brackets: 'aside', brackets_announce: true });
  if (memory.brackets === 'voice') Object.assign(memory, { brackets: 'aside', brackets_voice_on: true });
  if (memory.brackets_voice && !memory.extra_voice) memory.extra_voice = memory.brackets_voice;
  delete memory.brackets_voice;
  return { ...memory };
}

export function save(patch) {
  memory = { ...load(), ...patch };
  try { localStorage.setItem(KEY, JSON.stringify(memory)); } catch { /* unavailable */ }
  for (const fn of listeners) fn(memory, patch);
  return { ...memory };
}

/** fn(settings, patch) after every save (e.g. a voice picked in Tools updates the main screen). */
export const subscribe = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };

export function loadDraft() {
  try { return localStorage.getItem(DRAFT) ?? draftMemory; } catch { return draftMemory; }
}

export function saveDraft(text) {
  draftMemory = text;
  try { localStorage.setItem(DRAFT, text); } catch { /* quota or unavailable */ }
}
