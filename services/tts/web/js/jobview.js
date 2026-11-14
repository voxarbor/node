// One job: live progress over SSE, streaming playback while it runs, the final file when done (own player bar,
// no native <audio controls>), and the text split into sentences that follow the playback.
import { api, apiUrl } from './api.js';
import { StreamPlayer, unlockAudio } from './player.js';
import { bracketsDesc, normBrackets } from './settings.js';
import { setProgress } from './title.js';
import { btn, dialog, fmtDate, fmtEta, fmtNum, fmtPct, fmtTime, h, icon, nextId, toast } from './util.js';
import { ENGINE_LABELS, getVoices, prettyVoice } from './voices.js';

export const TERMINAL = new Set(['done', 'error', 'canceled']);
export const STATUS_LABELS = { queued: 'У черзі', running: 'Озвучується', done: 'Готово', error: 'Помилка', canceled: 'Зупинено' };
const STREAM_STATES = { buffering: 'Буферизація…', playing: 'Наживо', paused: 'Пауза', ended: 'Прослухано', stopped: '' };
const RATES = [0.75, 1, 1.25, 1.5, 1.75, 2];

export function statusBadge(status, percent = null) {
  const label = STATUS_LABELS[status] || status;
  return h('span', { class: `badge s-${status}` }, status === 'running' && percent != null ? `${label} · ${fmtPct(percent)}` : label);
}

/** Progress of a job object in percent (by characters from the API; segments as a fallback). */
export function jobPercent(job) {
  const p = job?.progress || {};
  if (job?.status === 'done') return 100;
  if (p.percent != null) return p.percent;
  return p.total ? (100 * (p.done || 0)) / p.total : 0;
}

/** Join segments back into paragraphs: look each one up in the original text and keep its line breaks. */
function layout(text, segs) {
  const out = [];
  let cursor = 0;
  for (const s of segs) {
    let brk = 0;
    const t = (s.text || '').trim();
    // the shown text is cleaned (Markdown gone, a heading may gain a period): try shorter prefixes too
    const probes = [t.slice(0, 24), t.replace(/[.!?…:;]+$/, '').slice(0, 24), t.slice(0, 10)].filter(Boolean);
    let at = -1;
    let probe = '';
    for (probe of probes) { at = text ? text.indexOf(probe, cursor) : -1; if (at >= 0) break; }
    if (text && probe && at >= 0 && at - cursor < 2000) {
      const gap = text.slice(cursor, at);
      const nl = (gap.match(/\n/g) || []).length;
      brk = nl >= 2 || /\n\s*\n/.test(gap) ? 2 : nl;
      cursor = at + probe.length;
    }
    out.push({ seg: s, brk: out.length ? brk : 0 });
  }
  return out;
}

const yesNo = (v) => (v === undefined || v === null ? null : v ? 'так' : 'ні');

export class JobView {
  /** opts: {manage, historyLink, onDeleted(), onRepeat(job, text), onChanged(job), onClose()} */
  constructor(root, opts = {}) {
    this.root = root;
    this.opts = opts;
    this.alive = false;
    this.onExclusive = (e) => { if (e.detail !== this) this.pauseAll(); };
    this.onUserScroll = () => { this.userScrollAt = Date.now(); };
  }

  async open(id, { job = null, autoplay = false, detail = null, resumeAt = 0 } = {}) {
    this.destroy();
    Object.assign(this, {
      id, job: job || { id, status: 'queued', title: '…', params: {}, progress: { done: 0, total: 0 } },
      detail: null, segs: [], current: -1, player: null, audio: null, es: null, pollTimer: null, mode: null,
      alive: true, userScrollAt: 0, gotAt: Date.now(), handing: false, finalShown: false, seeking: false,
      warnedSeg: false, sseFails: 0, resumeAt: resumeAt > 1 ? resumeAt : 0, posSavedAt: 0,
    });
    document.addEventListener('ozv-play', this.onExclusive);
    window.addEventListener('wheel', this.onUserScroll, { passive: true });
    window.addEventListener('touchmove', this.onUserScroll, { passive: true });
    this.render();
    this.update(this.job);
    getVoices().then(() => { if (this.alive && this.id === id) this.update({}); }).catch(() => {}); // voice labels may load after the panel
    if (autoplay) this.startStream(0);
    this.ticker = setInterval(() => this.tick(), 250);
    try {
      const d = detail || await api(`/v1/jobs/${encodeURIComponent(id)}`);
      if (!this.alive || this.id !== id) return;
      if (this.resumeAt && d.duration_s && TERMINAL.has(d.status) && this.resumeAt >= d.duration_s - 1) this.resumeAt = 0; // listened to the end
      this.applyDetail(d);
      if (this.resumeAt) this.highlight(this.resumeAt, true);
    } catch (e) {
      if (!this.alive) return;
      this.showError(e.message);
      if (e.status === 404) return;
    }
    if (!TERMINAL.has(this.job.status)) this.connect();
  }

  destroy() {
    if (!this.alive) return;
    this.savePosition(true);
    this.alive = false;
    setProgress(this, null);
    clearInterval(this.ticker);
    clearTimeout(this.pollTimer);
    if (this.es) this.es.close();
    if (this.player) this.player.stop();
    if (this.audio) { this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load(); }
    document.removeEventListener('ozv-play', this.onExclusive);
    window.removeEventListener('wheel', this.onUserScroll);
    window.removeEventListener('touchmove', this.onUserScroll);
    this.player = this.audio = this.es = null;
    this.root.replaceChildren();
  }

  claim() {
    document.dispatchEvent(new CustomEvent('ozv-play', { detail: this }));
    this.opts.onListen?.(this.job);
    if (this.resumeAt) { this.resumeAt = 0; this.renderLive(); }
  }

  /** Current play head (seconds) while something is loaded; null when nothing plays. */
  playPosition() {
    if (this.mode === 'stream' && this.player && this.player.state !== 'stopped') return this.player.position;
    if (this.mode === 'final' && this.audio && (this.audio.currentTime > 0 || !this.audio.paused)) return this.audio.currentTime;
    return null;
  }

  isPlaying() {
    return (this.mode === 'stream' && this.player?.state === 'playing') || (this.mode === 'final' && this.audio && !this.audio.paused);
  }

  /** Remember the position (throttled to ~2 s; `now` forces it, e.g. on pause). */
  savePosition(now = false) {
    if (!this.opts.onPosition || !this.alive) return;
    const pos = this.playPosition();
    if (pos == null || (!now && Date.now() - this.posSavedAt < 2000)) return;
    this.posSavedAt = Date.now();
    this.opts.onPosition(this.id, pos);
  }

  /** «Продовжити з 3:12» after a reload: one click (autoplay is blocked), from the saved position. */
  resume() {
    const pos = this.resumeAt;
    this.resumeAt = 0;
    unlockAudio();
    if (this.audio) {
      if (this.player) this.killStream();
      this.setMode('final');
      this.claim();
      this.audio.currentTime = pos;
      this.audio.play().catch(() => {});
    } else {
      const i = Math.max(0, this.indexAt(pos));
      this.startStream(i, Math.max(0, pos - (this.segs[i]?.start_s || 0)));
    }
    this.renderLive();
  }

  pauseAll() {
    if (this.player && this.player.state !== 'paused') this.player.pause();
    if (this.audio && !this.audio.paused) this.audio.pause();
    this.renderBar();
  }

  // ---------------------------------------------------------------- rendering
  render() {
    const titleId = nextId('job');
    this.titleEl = h('h2', { id: titleId, class: 'job-title' });
    this.badgeEl = h('span', { class: 'job-badge' });
    this.metaEl = h('p', { class: 'meta' });
    this.bar = h('span');
    this.progressEl = h('div', { class: 'progress', role: 'progressbar', 'aria-label': 'Прогрес озвучення', 'aria-valuemin': 0, 'aria-valuemax': 100 }, this.bar);
    this.progressText = h('p', { class: 'progress-text' });
    this.progressWrap = h('div', { class: 'progress-wrap' }, this.progressEl, this.progressText);
    this.connEl = h('p', { class: 'conn', role: 'status', hidden: true });
    this.errorEl = h('div', { class: 'alert error', role: 'alert', hidden: true });
    this.liveEl = h('div', { class: 'live-row', hidden: true });

    // player bar (stream and final file share it)
    this.playBtn = h('button', { class: 'pbtn', type: 'button', 'aria-label': 'Відтворити', onClick: () => this.togglePlay() }, icon('play'));
    this.timeEl = h('span', { class: 'ptime', 'aria-hidden': 'true' }, '0:00');
    this.seekEl = h('input', {
      type: 'range', class: 'pseek', min: 0, max: 1, step: 0.1, value: 0, 'aria-label': 'Позиція відтворення',
      onInput: () => { this.seeking = true; this.timeEl.textContent = `${fmtTime(Number(this.seekEl.value))} / ${this.durText()}`; },
      onChange: () => { this.seeking = false; this.seekTime(Number(this.seekEl.value)); },
    });
    this.durEl = h('span', { class: 'ptime pdur', 'aria-hidden': 'true' });
    this.stateEl = h('span', { class: 'pstate', 'aria-live': 'polite' });
    this.rateEl = h('select', { class: 'prate', 'aria-label': 'Швидкість відтворення', onChange: () => { if (this.audio) this.audio.playbackRate = Number(this.rateEl.value); } },
      RATES.map((r) => h('option', { value: r, selected: r === 1 }, `${r}×`)));
    this.cancelBtn = btn('Зупинити', { icon: 'stop', cls: 'btn small danger', onClick: () => this.cancel() });
    this.barEl = h('div', { class: 'player-bar', hidden: true, role: 'group', 'aria-label': 'Програвач' },
      this.playBtn, this.timeEl, this.seekEl, this.durEl, this.stateEl, this.rateEl, this.cancelBtn);
    this.downloadsEl = h('div', { class: 'downloads', hidden: true });

    this.textEl = h('div', { class: 'job-text', lang: 'uk', 'aria-label': 'Текст', onClick: (e) => this.onTextClick(e), onKeydown: (e) => this.onTextKey(e) });
    this.textHint = h('p', { class: 'text-hint', hidden: true }, 'Торкніться речення, щоб перейти до нього.');
    this.paramsEl = h('dl', { class: 'params' });
    this.actionsEl = h('div', { class: 'job-actions' });
    const close = this.opts.onClose ? h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Закрити панель', title: 'Закрити', onClick: () => this.opts.onClose() }, icon('x')) : null;

    this.root.replaceChildren(h('article', { class: 'panel job', 'aria-labelledby': titleId },
      h('div', { class: 'job-top' },
        h('div', { class: 'job-head' }, h('div', { class: 'job-headline' }, this.titleEl, this.metaEl), this.badgeEl, close),
        this.progressWrap, this.connEl, this.errorEl, this.liveEl, this.barEl, this.downloadsEl),
      h('div', { class: 'job-body' },
        h('div', { class: 'job-main' }, this.textHint, this.textEl),
        h('aside', { class: 'job-aside', 'aria-label': 'Параметри і дії' },
          h('h3', { class: 'section-label' }, 'Параметри'), this.paramsEl, this.actionsEl))));
    this.renderActions();
  }

  renderActions() {
    const items = [];
    if (this.opts.historyLink) items.push(h('a', { class: 'btn small', href: `#/history/${encodeURIComponent(this.id)}` }, icon('history'), h('span', null, 'В історії')));
    if (this.opts.manage) {
      items.push(btn('Перейменувати', { icon: 'edit', cls: 'btn small', onClick: () => this.rename() }));
      items.push(btn('Повторити', { icon: 'repeat', cls: 'btn small', onClick: () => this.opts.onRepeat?.(this.job, this.detail?.text || '') }));
      items.push(btn('Видалити', { icon: 'trash', cls: 'btn small danger', onClick: () => this.remove() }));
    }
    this.actionsEl.replaceChildren(...items);
    this.actionsEl.hidden = !items.length;
  }

  update(job) {
    if (!job) return;
    const prevStatus = this.job?.status;
    this.job = { ...this.job, ...job };
    this.gotAt = Date.now();
    const j = this.job;
    const p = j.params || {};
    this.titleEl.textContent = j.title || 'Без назви';
    this.badgeEl.replaceChildren(statusBadge(j.status, jobPercent(j)));
    const voice = p.model === 'single' ? 'один диктор' : prettyVoice(p.voice, p.engine);
    const meta = [ENGINE_LABELS[p.engine] || p.engine, voice, p.speed && p.speed !== 1 ? `${p.speed}×` : null,
      p.initials === 'skip' ? 'без ініціалів' : null,
      p.brackets && (normBrackets(p).brackets !== 'aside' || normBrackets(p).brackets_announce) ? `дужки: ${bracketsDesc(p).toLowerCase()}` : null,
      j.chars ? `${fmtNum(j.chars)} симв.` : null, j.duration_s ? fmtTime(j.duration_s) : null,
      (p.response_format || '').toUpperCase() || null, fmtDate(j.created)];
    this.metaEl.textContent = meta.filter(Boolean).join(' · ');
    const rows = [
      ['Рушій', ENGINE_LABELS[p.engine] || p.engine], ['Голос', voice], ['Швидкість', p.speed ? `${p.speed}×` : null],
      ['Пауза', p.pause_ms != null ? `${p.pause_ms} мс` : null], ['Числа словами', yesNo(p.verbalize)],
      ['Англійські назви', yesNo(p.english)], ['Очищення Markdown', yesNo(p.clean)],
      ['Ініціали', p.initials === 'skip' ? 'пропускати' : null],
      ['Дужки', p.brackets ? bracketsDesc(p) : null],
      ['Додатковий голос', p.brackets === 'voice' && p.brackets_voice ? `${prettyVoice(p.brackets_voice, p.engine)} · ${String(p.brackets_gain_db ?? -3).replace('-', '−')} дБ` : null],
      ['Формат', (p.response_format || '').toUpperCase() || null], ['Символів', j.chars ? fmtNum(j.chars) : null],
      ['Тривалість', j.duration_s ? fmtTime(j.duration_s) : null], ['Синтез', j.elapsed_s ? fmtTime(j.elapsed_s) : null],
      ['Створено', fmtDate(j.created)],
    ].filter(([, v]) => v);
    this.paramsEl.replaceChildren(...rows.flatMap(([k, v]) => [h('dt', null, k), h('dd', null, v)]));
    const running = !TERMINAL.has(j.status);
    setProgress(this, running ? jobPercent(j) : null);
    this.progressWrap.hidden = !running;
    if (running) this.renderProgress();
    if (j.status === 'error') this.showError(`Помилка озвучення: ${j.error || 'невідома помилка'}`);
    else if (j.status === 'canceled' && prevStatus !== 'canceled') this.showInfo('Озвучення зупинено. Готова частина збережена.');
    this.renderLive();
    if (TERMINAL.has(j.status)) this.onTerminal();
  }

  renderProgress() {
    const { done = 0, total = 0, eta_s: eta = null } = this.job.progress || {};
    const pct = jobPercent(this.job);
    const queued = this.job.status === 'queued';
    this.progressEl.classList.toggle('indeterminate', queued || (!total && !pct));
    this.bar.style.width = `${Math.min(100, pct)}%`;
    this.progressEl.setAttribute('aria-valuenow', String(Math.floor(pct)));
    const since = (Date.now() - this.gotAt) / 1000;
    const elapsed = (this.job.elapsed_s || 0) + (this.job.status === 'running' ? since : 0);
    let left;
    if (queued) left = 'У черзі — чекає на попередні завдання…';
    else if (!total) left = 'Підготовка тексту…';
    else left = [fmtPct(pct), eta != null ? `залишилось ${fmtEta(Math.max(1, eta - since))}` : null].filter(Boolean).join(' · ');
    const right = [total ? `речень ${done} з ${total}` : null, `минуло ${fmtTime(elapsed)}`].filter(Boolean);
    this.progressText.replaceChildren(h('span', { class: 'pct' }, left), h('span', null, right.join(' · ')));
  }


  /** Running job without own stream: offer to listen live, and to stop it. */
  renderLive() {
    const running = !TERMINAL.has(this.job.status);
    const resume = this.resumeAt > 1 && !this.isPlaying() && this.mode !== 'stream';
    const items = [];
    if (resume) {
      items.push(btn(`Продовжити з ${fmtTime(this.resumeAt)}`, { icon: 'play', cls: 'btn primary small resume-btn', onClick: () => this.resume() }));
      items.push(btn('З початку', { cls: 'btn small', onClick: () => { this.resumeAt = 0; if (this.audio) { this.audio.currentTime = 0; this.seekTo(0); } else { unlockAudio(); this.startStream(0); } } }));
    } else if (running && this.mode !== 'stream') {
      items.push(btn('Слухати наживо', { icon: 'play', cls: 'btn primary small', onClick: () => { unlockAudio(); this.startStream(0); } }));
    }
    if (running && this.mode !== 'stream') items.push(btn('Зупинити', { icon: 'stop', cls: 'btn small danger', onClick: () => this.cancel() }));
    this.liveEl.replaceChildren(...items);
    this.liveEl.hidden = !items.length;
    this.renderBar();
  }


  showError(msg) { this.errorEl.className = 'alert error'; this.errorEl.setAttribute('role', 'alert'); this.errorEl.textContent = msg; this.errorEl.hidden = false; }
  showInfo(msg) { this.errorEl.className = 'alert info'; this.errorEl.setAttribute('role', 'status'); this.errorEl.textContent = msg; this.errorEl.hidden = false; }

  // ---------------------------------------------------------------- player bar
  setMode(mode) {
    this.mode = mode;
    this.renderLive();
  }

  durText() {
    if (this.mode === 'final' && this.audio && isFinite(this.audio.duration)) return fmtTime(this.audio.duration);
    if (this.mode === 'stream' && this.player) return `${TERMINAL.has(this.job.status) ? '' : '≥ '}${fmtTime(this.player.duration)}`;
    return fmtTime(this.job.duration_s || 0);
  }

  renderBar() {
    if (!this.barEl) return;
    const m = (this.mode === 'stream' && !this.player) || (this.mode === 'final' && !this.audio) ? null : this.mode;
    this.barEl.hidden = !m;
    if (!m) return;
    let paused;
    let pos;
    let dur;
    let state = '';
    if (m === 'stream') {
      const p = this.player;
      paused = p.state === 'paused' || p.state === 'ended';
      pos = p.position;
      dur = p.duration;
      state = STREAM_STATES[p.state] || '';
      if (p.state === 'buffering' && !this.doneCount()) state = this.job.status === 'queued' ? 'Очікування в черзі…' : 'Готується перше речення…';
    } else {
      const a = this.audio;
      paused = a.paused;
      pos = a.currentTime || 0;
      dur = isFinite(a.duration) ? a.duration : (this.job.duration_s || 0);
      if (!paused && a.readyState < 3) state = 'Завантаження…';
    }
    const want = paused ? 'play' : 'pause';
    if (this.playBtn.dataset.icon !== want) {
      this.playBtn.dataset.icon = want;
      this.playBtn.replaceChildren(icon(want));
      this.playBtn.setAttribute('aria-label', paused ? 'Відтворити' : 'Пауза');
    }
    this.seekEl.max = String(Math.max(0.1, dur));
    if (!this.seeking) {
      this.seekEl.value = String(Math.min(pos, dur));
      this.timeEl.textContent = fmtTime(pos);
    }
    this.seekEl.setAttribute('aria-valuetext', `${fmtTime(pos)} з ${fmtTime(dur)}`);
    this.durEl.textContent = this.durText();
    if (this.stateEl.textContent !== state) this.stateEl.textContent = state;
    this.stateEl.hidden = !state;
    this.rateEl.hidden = m !== 'final';
    this.cancelBtn.hidden = m !== 'stream' || TERMINAL.has(this.job.status);
  }

  togglePlay() {
    if (this.mode === 'stream' && this.player) {
      const p = this.player;
      if (p.state === 'paused' || p.state === 'ended') { unlockAudio(); this.claim(); p.play(); } else { p.pause(); this.savePosition(true); }
    } else if (this.mode === 'final' && this.audio) {
      if (this.audio.paused) { this.claim(); this.audio.play().catch(() => {}); } else this.audio.pause();
    }
    this.renderBar();
  }

  seekTime(t) {
    this.userScrollAt = 0;
    if (this.mode === 'final' && this.audio) {
      this.audio.currentTime = t;
    } else if (this.mode === 'stream' && this.player) {
      const i = this.indexAt(t);
      if (i >= 0) { this.claim(); this.player.seek(i, t - (this.segs[i]?.start_s || 0)); this.player.play(); }
    }
    this.renderBar();
  }

  // ---------------------------------------------------------------- text
  applyDetail(d) {
    this.detail = d;
    this.update(d);
    const segs = (d.segments || []).map((s) => ({ ...s }));
    if (segs.length) {
      this.segs = [];
      segs.forEach((s) => { this.segs[s.index] = s; });
      this.renderText();
    } else if (!this.segs.length && d.text) {
      this.textEl.replaceChildren(...d.text.split(/\n\s*\n/).map((p) => h('p', { class: 'muted' }, p)));
    }
    for (const s of this.segs) if (s && s.done) this.feed(s);
  }

  renderText() {
    const list = this.segs.filter(Boolean);
    const blocks = [];
    let p = h('p');
    for (const { seg, brk } of layout(this.detail?.text || '', list)) {
      if (brk >= 2 && p.childNodes.length) { blocks.push(p); p = h('p'); } else if (brk === 1 && p.childNodes.length) p.append(h('br'));
      else if (p.childNodes.length) p.append(' ');
      seg.el = h('span', { class: `sent${seg.done ? '' : ' pending'}`, dataset: { i: seg.index }, title: seg.spoken && seg.spoken !== seg.text ? `Читається: ${seg.spoken}` : null }, seg.text);
      p.append(seg.el);
    }
    if (p.childNodes.length) blocks.push(p);
    this.textEl.replaceChildren(...blocks);
    this.textHint.hidden = !list.length;
    this.current = -1;
  }

  onSegment(s) {
    let seg = this.segs[s.index];
    if (!seg) {
      seg = this.segs[s.index] = { index: s.index, text: s.text };
      seg.el = h('span', { class: 'sent', dataset: { i: s.index } }, s.text || '');
      let p = this.textEl.lastElementChild;
      if (!p || p.tagName !== 'P') { p = h('p'); this.textEl.append(p); }
      if (p.childNodes.length) p.append(' ');
      p.append(seg.el);
      this.textHint.hidden = false;
    }
    Object.assign(seg, { start_s: s.start_s, duration_s: s.duration_s, done: true, audio_url: s.audio_url });
    seg.el?.classList.remove('pending');
    this.feed(seg);
  }

  feed(seg) {
    if (!this.player) return;
    if (!seg.duration_s || seg.audio_url === null) { // empty segment: no audio, nothing to wait for
      this.player.add({ index: seg.index, start_s: seg.start_s ?? null, duration_s: 0, skip: true });
      return;
    }
    if (seg.start_s == null) return;
    const url = apiUrl(seg.audio_url || `/v1/jobs/${encodeURIComponent(this.id)}/segments/${seg.index}`);
    this.player.add({ index: seg.index, start_s: seg.start_s, duration_s: seg.duration_s, url });
  }

  indexAt(pos) {
    let best = -1;
    for (const s of this.segs) {
      if (!s || s.start_s == null) continue;
      if (s.start_s <= pos + 0.05) best = s.index; else break;
    }
    return best;
  }

  highlight(pos, follow) {
    const i = this.indexAt(pos);
    if (i === this.current) return;
    this.segs[this.current]?.el?.classList.remove('playing');
    this.segs[this.current]?.el?.removeAttribute('aria-current');
    this.current = i;
    const el = this.segs[i]?.el;
    if (!el) return;
    el.classList.add('playing');
    el.setAttribute('aria-current', 'true');
    if (follow && Date.now() - this.userScrollAt > 5000) this.scrollToSentence(el);
  }

  /** Keep the playing sentence visible: inside the text's own scroll box when it has one, else in the page. */
  scrollToSentence(el) {
    const box = this.textEl;
    if (box.scrollHeight > box.clientHeight + 4) {
      const er = el.getBoundingClientRect();
      const br = box.getBoundingClientRect();
      if (er.top < br.top + 8 || er.bottom > br.bottom - 8) {
        box.scrollTo({ top: Math.max(0, box.scrollTop + (er.top - br.top) - box.clientHeight / 3), behavior: 'smooth' });
      }
      return;
    }
    const r = el.getBoundingClientRect();
    const bottomBar = window.innerWidth < 760 ? 140 : 20;
    if (r.top < 70 || r.bottom > window.innerHeight - bottomBar) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  onTextClick(e) {
    const el = e.target.closest('.sent');
    if (!el || el.classList.contains('pending')) return;
    this.seekTo(Number(el.dataset.i));
  }

  onTextKey(e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const el = e.target.closest?.('.sent');
    if (el) { e.preventDefault(); this.seekTo(Number(el.dataset.i)); }
  }

  seekTo(i) {
    const seg = this.segs[i];
    if (!seg || seg.start_s == null) return;
    this.userScrollAt = 0;
    if (this.audio) {
      if (this.player) this.killStream();
      this.setMode('final');
      this.claim();
      this.audio.currentTime = seg.start_s + 0.01;
      this.audio.play().catch(() => {});
    } else if (this.player) {
      this.claim();
      this.player.seek(i);
      this.player.play();
    } else {
      unlockAudio();
      this.startStream(i);
    }
    this.highlight(seg.start_s + 0.02, false);
    this.renderBar();
  }

  // ---------------------------------------------------------------- live updates
  connect() {
    if (this.es || !this.alive) return;
    const es = new EventSource(apiUrl(`/v1/jobs/${encodeURIComponent(this.id)}/events`));
    this.es = es;
    es.addEventListener('segment', (e) => { try { this.onSegment(JSON.parse(e.data)); } catch (err) { console.warn(err); } });
    es.addEventListener('job', (e) => {
      let job;
      try { job = JSON.parse(e.data); } catch { return; }
      this.update(job);
      if (TERMINAL.has(job.status)) { es.close(); if (this.es === es) this.es = null; }
    });
    es.onopen = () => { this.connEl.hidden = true; this.sseFails = 0; };
    es.onerror = () => {
      if (!this.alive || this.es !== es) return;
      this.sseFails = (this.sseFails || 0) + 1;
      if (es.readyState === EventSource.CLOSED || this.sseFails > 3) {
        es.close();
        this.es = null;
        this.poll();
      } else {
        this.connEl.textContent = 'З’єднання перервано — перепідключення…';
        this.connEl.hidden = false;
      }
    };
  }

  /** Fallback when SSE is not available: poll the job every 3 s. */
  async poll() {
    if (!this.alive || TERMINAL.has(this.job.status)) return;
    this.connEl.textContent = 'Оновлення кожні кілька секунд (без живого з’єднання).';
    this.connEl.hidden = false;
    try {
      const d = await api(`/v1/jobs/${encodeURIComponent(this.id)}`);
      if (!this.alive) return;
      this.detail = d;
      for (const s of d.segments || []) if (s.done && !this.segs[s.index]?.done) this.onSegment(s);
      this.update(d);
    } catch (e) {
      if (e.status === 404 || e.status === 401) { this.showError(e.message); return; }
    }
    if (!TERMINAL.has(this.job.status)) this.pollTimer = setTimeout(() => this.poll(), 3000);
  }

  // ---------------------------------------------------------------- streaming
  startStream(from, offset = 0) {
    if (this.player) this.player.stop();
    if (this.audio && !this.audio.paused) this.audio.pause();
    this.claim();
    const p = new StreamPlayer();
    this.player = p;
    p.addEventListener('state', () => this.renderBar());
    p.addEventListener('segmenterror', () => {
      if (!this.warnedSeg) { this.warnedSeg = true; toast('Частину аудіо не вдалося завантажити — пропущено.', { type: 'error' }); }
    });
    p.addEventListener('ended', () => { if (this.audio) this.killStream(true); });
    for (const s of this.segs) if (s && s.done) this.feed(s);
    if (from || offset) p.seek(from, offset);
    if (TERMINAL.has(this.job.status)) p.finish(this.doneCount());
    this.setMode('stream');
    if (!TERMINAL.has(this.job.status)) this.connect();
  }

  doneCount() { return this.segs.filter((s) => s && s.done).length || this.job.progress?.done || 0; }

  killStream(atEnd = false) {
    if (!this.player) return;
    this.player.stop();
    this.player = null;
    if (atEnd && this.audio) this.audio.currentTime = 0;
    this.setMode(this.audio ? 'final' : null);
  }

  async cancel() {
    if (this.player) this.player.pause();
    try {
      const job = await api(`/v1/jobs/${encodeURIComponent(this.id)}/cancel`, { method: 'POST' });
      this.update(job);
    } catch (e) { toast(e.message, { type: 'error' }); }
  }

  tick() {
    if (!this.alive) return;
    if (this.isPlaying()) this.savePosition();
    if (this.job.status === 'running') this.renderProgress();
    if (this.mode === 'stream' && this.player) {
      if (this.player.state !== 'stopped') this.highlight(this.player.position, this.player.state === 'playing');
    } else if (this.mode === 'final' && this.audio && (this.audio.currentTime > 0 || !this.audio.paused)) {
      this.highlight(this.audio.currentTime, !this.audio.paused);
    }
    this.renderBar();
  }

  // ---------------------------------------------------------------- final file
  onTerminal() {
    if (this.player) this.player.finish(this.doneCount());
    if (this.job.audio_url && !this.finalShown) this.showFinal();
  }

  showFinal() {
    this.finalShown = true;
    const j = this.job;
    const fmt = j.params?.response_format || 'mp3';
    const base = (j.title || 'ozvuchka').replace(/[\\/:*?"<>|\n\r]+/g, ' ').trim().slice(0, 80) || 'ozvuchka';
    const a = h('audio', { preload: 'metadata', src: apiUrl(j.audio_url) });
    this.audio = a;
    a.playbackRate = Number(this.rateEl.value) || 1;
    a.addEventListener('play', () => {
      if (!this.alive) return;
      this.claim();
      if (this.player && !this.handing) this.killStream();
      if (this.mode !== 'final') this.setMode('final');
      this.mediaSession();
    });
    for (const ev of ['pause', 'loadedmetadata', 'durationchange', 'waiting', 'playing', 'ended']) a.addEventListener(ev, () => { if (this.alive) this.renderBar(); });
    a.addEventListener('pause', () => this.savePosition(true));
    if (this.resumeAt) {
      a.addEventListener('loadedmetadata', () => { if (this.alive && this.resumeAt && a.paused) { a.currentTime = this.resumeAt; this.highlight(this.resumeAt, false); } }, { once: true });
    }
    a.addEventListener('timeupdate', () => { if (this.alive && this.mode === 'final') this.highlight(a.currentTime, !a.paused); });
    a.addEventListener('seeked', () => { if (this.alive && this.mode === 'final') { this.userScrollAt = 0; this.highlight(a.currentTime, true); } });
    a.addEventListener('error', () => { if (this.alive && this.audio === a && a.error) this.showError('Не вдалося завантажити аудіофайл.'); });
    const q = (u, s) => `${u}${u.includes('?') ? '&' : '?'}${s}`;
    const subs = j.subtitles_url ? [
      h('a', { class: 'btn small', href: apiUrl(q(j.subtitles_url, 'format=srt')), download: `${base}.srt` }, icon('cc'), h('span', null, 'SRT')),
      h('a', { class: 'btn small', href: apiUrl(q(j.subtitles_url, 'format=vtt')), download: `${base}.vtt` }, icon('cc'), h('span', null, 'VTT')),
    ] : [];
    this.downloadsEl.replaceChildren(a,
      h('a', { class: 'btn small primary', href: apiUrl(q(j.audio_url, 'download=1')), download: `${base}.${fmt}` }, icon('download'), h('span', null, `Завантажити ${fmt.toUpperCase()}`)),
      ...subs);
    this.downloadsEl.hidden = false;
    if (this.player) this.handoff();
    else this.setMode('final');
  }

  /** The job finished while streaming: continue from the same spot in the final file (seekable). If the browser
   *  refuses to start it without a tap (iOS), the stream simply plays on. */
  async handoff() {
    const p = this.player;
    const a = this.audio;
    if (!p || !a) return;
    if (p.state === 'ended') { this.killStream(true); return; }
    const playing = p.state === 'playing' || p.state === 'buffering';
    this.handing = true;
    try {
      if (a.readyState < 1) {
        await new Promise((resolve, reject) => {
          const t = setTimeout(() => reject(new Error('timeout')), 15000);
          a.addEventListener('loadedmetadata', () => { clearTimeout(t); resolve(); }, { once: true });
          a.addEventListener('error', () => { clearTimeout(t); reject(new Error('error')); }, { once: true });
        });
      }
      if (this.player !== p || p.stopped) return;
      a.currentTime = p.position;
      if (!playing || p.paused) { this.killStream(); return; }
      await a.play();
      if (this.player === p) this.killStream();
    } catch { /* keep streaming */ } finally {
      this.handing = false;
    }
  }

  mediaSession() {
    if (!('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: this.job.title || 'Озвучка', artist: 'Озвучка', album: prettyVoice(this.job.params?.voice, this.job.params?.engine),
        artwork: [{ src: '/icon-512.png', sizes: '512x512', type: 'image/png' }],
      });
    } catch { /* unsupported */ }
  }

  // ---------------------------------------------------------------- manage
  async rename() {
    const title = await dialog({ title: 'Перейменувати', ok: 'Зберегти', input: { value: this.job.title || '', label: 'Назва' } });
    if (title === false || !title.trim()) return;
    try {
      this.update(await api(`/v1/jobs/${encodeURIComponent(this.id)}`, { method: 'PATCH', json: { title: title.trim() } }));
      this.opts.onChanged?.(this.job);
      toast('Назву змінено', { type: 'ok' });
    } catch (e) { toast(e.message, { type: 'error' }); }
  }

  async remove() {
    const running = !TERMINAL.has(this.job.status);
    const ok = await dialog({
      title: 'Видалити запис?', message: `«${this.job.title || 'Без назви'}» буде видалено разом з аудіо.${running ? ' Озвучення буде зупинено.' : ''}`,
      ok: 'Видалити', danger: true,
    });
    if (!ok) return;
    try {
      await api(`/v1/jobs/${encodeURIComponent(this.id)}`, { method: 'DELETE' });
      toast('Запис видалено', { type: 'ok' });
      this.destroy();
      this.opts.onDeleted?.();
    } catch (e) { toast(e.message, { type: 'error' }); }
  }
}
