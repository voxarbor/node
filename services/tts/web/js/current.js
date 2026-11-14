// The job shown on «Озвучити» (restored after a page reload) and playback positions per job, in localStorage.
const CUR = 'ozv.current.v1';
const POS = 'ozv.pos.v1';
const MAX_POS = 30;

function read(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function write(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* unavailable or full */ }
}

export const getCurrent = () => read(CUR, null)?.id || null;
export const setCurrent = (id) => write(CUR, { id, at: Date.now() });
export function clearCurrent() { try { localStorage.removeItem(CUR); } catch { /* unavailable */ } }

export function savePos(id, pos) {
  if (!id || !(pos >= 0)) return;
  const all = read(POS, {});
  all[id] = { pos: Math.round(pos * 10) / 10, at: Date.now() };
  const ids = Object.keys(all).sort((a, b) => all[b].at - all[a].at);
  for (const old of ids.slice(MAX_POS)) delete all[old];
  write(POS, all);
}
export const getPos = (id) => read(POS, {})[id]?.pos || 0;
export function forgetPos(id) {
  const all = read(POS, {});
  if (id in all) { delete all[id]; write(POS, all); }
}
