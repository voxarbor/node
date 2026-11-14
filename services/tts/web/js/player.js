// Gapless streaming playback of job segments with the Web Audio API.
// Segments arrive one by one (SSE); each WAV is fetched, decoded and scheduled on the AudioContext clock at its
// offset in the final audio (start_s), so the pauses match the final file. If the next segment is not ready in time
// the timeline shifts (buffering) instead of overlapping.

let ctx = null;

export function audioContext() {
  if (!ctx) {
    const C = window.AudioContext || window.webkitAudioContext;
    ctx = new C({ latencyHint: 'playback' });
  }
  return ctx;
}

/** Call synchronously inside a click handler: iOS Safari only starts audio from a user gesture. */
export function unlockAudio() {
  try { if (navigator.audioSession) navigator.audioSession.type = 'playback'; } catch { /* iOS 17+ only */ }
  const c = audioContext();
  if (c.state !== 'running') c.resume().catch(() => {});
  const src = c.createBufferSource();
  src.buffer = c.createBuffer(1, 1, c.sampleRate);
  src.connect(c.destination);
  src.start(0);
  return c;
}

function decode(c, data) {
  return new Promise((resolve, reject) => {
    const p = c.decodeAudioData(data, resolve, reject); // callback form for old Safari
    if (p && typeof p.then === 'function') p.then(resolve, reject);
  });
}

const FETCH_AHEAD = 5;      // segments fetched ahead of the play head
const FETCH_AHEAD_S = 90;   // ... but not further than this many seconds
const SCHEDULE_AHEAD_S = 20;

export class StreamPlayer extends EventTarget {
  constructor() {
    super();
    this.ctx = audioContext();
    this.out = this.ctx.createGain();
    this.out.connect(this.ctx.destination);
    this.segs = [];            // index -> {index, start_s, duration_s, url, skip}
    this.buffers = new Map();  // decoded, not yet scheduled
    this.loading = new Set();
    this.sources = [];
    this.next = 0;             // next segment to schedule
    this.origin = null;        // ctx time of audio offset 0
    this.lastEnd = 0;          // ctx time where the scheduled audio ends
    this.endOffset = 0;        // audio offset where the scheduled audio ends
    this.complete = false;
    this.total = Infinity;
    this.paused = false;
    this.stopped = false;
    this.state = 'buffering';
    this.timer = setInterval(() => this.pump(), 200);
  }

  add(seg) {
    if (this.stopped || this.segs[seg.index]) return;
    this.segs[seg.index] = seg;
    this.pump();
  }

  /** No more segments will come; total = number of segments. */
  finish(total) {
    this.complete = true;
    this.total = total;
    this.pump();
  }

  /** Current play head as an offset in the final audio (seconds). */
  get position() {
    if (this.origin == null) return this.segs[this.next]?.start_s ?? this.endOffset;
    return Math.max(0, Math.min(this.ctx.currentTime - this.origin, this.endOffset));
  }

  get duration() {
    let end = 0;
    for (const s of this.segs) if (s && s.start_s != null) end = Math.max(end, s.start_s + (s.duration_s || 0));
    return end;
  }

  play() {
    if (this.stopped) return;
    this.paused = false;
    if (this.state === 'ended') { this.seek(0); return; }
    if (this.ctx.state !== 'running') this.ctx.resume().catch(() => {});
    this.pump();
  }

  pause() {
    if (this.stopped) return;
    this.paused = true;
    this.ctx.suspend().catch(() => {});
    this._setState('paused');
  }

  /** Jump to segment `index`, `offset` seconds into it (resume after a reload lands mid-paragraph). */
  seek(index, offset = 0) {
    if (this.stopped) return;
    this._stopSources();
    this.next = Math.max(0, index);
    this.seekOffset = Math.max(0, offset);
    this.origin = null;
    this.lastEnd = 0;
    this.endOffset = (this.segs[this.next]?.start_s ?? 0) + this.seekOffset;
    this.buffers.forEach((_, i) => { if (i < this.next) this.buffers.delete(i); });
    if (this.state === 'ended') this._setState('buffering');
    this.pump();
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    clearInterval(this.timer);
    this._stopSources();
    this.buffers.clear();
    try { this.out.disconnect(); } catch { /* already */ }
    if (this.ctx.state === 'suspended' && this.paused) this.ctx.resume().catch(() => {});
    this._setState('stopped');
  }

  _stopSources() {
    for (const s of this.sources) { s.onended = null; try { s.stop(); } catch { /* not started */ } }
    this.sources = [];
  }

  _setState(st) {
    if (st === this.state) return;
    this.state = st;
    this.dispatchEvent(new CustomEvent('state', { detail: st }));
    if (st === 'ended') this.dispatchEvent(new Event('ended'));
  }

  async _load(i) {
    const seg = this.segs[i];
    this.loading.add(i);
    try {
      let res = null;
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          res = await fetch(seg.url, { credentials: 'same-origin' });
          if (res.ok || res.status === 404 || res.status === 401) break;
        } catch (e) {
          if (attempt === 2) throw e;
        }
        await new Promise((r) => setTimeout(r, 700 * (attempt + 1)));
      }
      if (!res || !res.ok) throw new Error(`HTTP ${res ? res.status : '?'}`);
      const buf = await decode(this.ctx, await res.arrayBuffer());
      if (!this.stopped && i >= this.next) this.buffers.set(i, buf);
    } catch (e) {
      // keep going: a missing segment becomes silence of its length
      if (!this.stopped && i >= this.next) {
        const dur = Math.max(0.2, seg.duration_s || 0.5);
        this.buffers.set(i, this.ctx.createBuffer(1, Math.ceil(dur * this.ctx.sampleRate), this.ctx.sampleRate));
        this.dispatchEvent(new CustomEvent('segmenterror', { detail: { index: i, error: e } }));
      }
    } finally {
      this.loading.delete(i);
      if (!this.stopped) this.pump();
    }
  }

  pump() {
    if (this.stopped) return;
    const now = this.ctx.currentTime;
    const pos = this.position;
    for (let i = this.next; i < this.next + FETCH_AHEAD && this.loading.size < 2; i++) {
      const s = this.segs[i];
      if (!s) break;
      if (s.skip || this.buffers.has(i) || this.loading.has(i)) continue;
      if (s.start_s != null && s.start_s > pos + FETCH_AHEAD_S) break;
      this._load(i);
    }
    for (;;) {
      const seg = this.segs[this.next];
      if (seg && seg.skip) { this.next++; continue; } // empty segment (duration 0, no audio)
      if (!this.buffers.has(this.next)) break;
      const buf = this.buffers.get(this.next);
      const offset = seg.start_s ?? this.endOffset;
      const skip = this.seekOffset ? Math.min(this.seekOffset, Math.max(0, buf.duration - 0.05)) : 0;
      if (this.origin == null) this.origin = now + 0.08 - offset - skip;
      let at = this.origin + offset + skip;
      if (at > now + SCHEDULE_AHEAD_S) break;
      const min = Math.max(now + 0.03, this.lastEnd);
      if (at < min) { this.origin += min - at; at = min; } // late or overlapping: shift the timeline
      const src = this.ctx.createBufferSource();
      src.buffer = buf;
      src.connect(this.out);
      src.start(at, skip);
      this.seekOffset = 0;
      src.onended = () => { this.sources = this.sources.filter((x) => x !== src); this.pump(); };
      this.sources.push(src);
      this.lastEnd = at + buf.duration - skip;
      this.endOffset = offset + buf.duration;
      this.buffers.delete(this.next);
      this.next++;
    }
    let st;
    if (this.paused) st = 'paused';
    else if (this.complete && this.next >= this.total && now >= this.lastEnd) st = 'ended';
    else if (now >= this.lastEnd - 0.02) st = 'buffering';
    else st = 'playing';
    this._setState(st);
  }
}
