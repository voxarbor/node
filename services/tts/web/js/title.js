// Browser tab title: the current view, prefixed with the progress of a running job («45 % · Озвучка»).
let base = 'Озвучка';
const running = new Map(); // owner -> percent

function render() {
  const v = [...running.values()];
  document.title = v.length ? `${Math.floor(v[v.length - 1])} % · Озвучка` : base;
}

export function setBase(title) { base = title; render(); }
export function setProgress(owner, percent) {
  if (percent == null) running.delete(owner); else running.set(owner, percent);
  render();
}
