// VoxArbor Node /gen (docs/gateway.md «Generation: /gen, Remotion Render»): long jobs with progress and cancel.
// Model "remotion" (kind render): one part of a Remotion composition per job (video frames a-b, its audio, or
// composition info). Bundles come from the coordinator by sha256 (via the connector) and stay in an LRU cache.
// One job runs at a time (slots 1); a new job waits while the node serves viewers or TTS, or the machine is busy.
import { spawn, execFile } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { pipeline } from "node:stream/promises";

const env = process.env;
const API_KEY = env.API_KEY || "";
const CACHE = env.GEN_CACHE_DIR || "/cache";
const BUNDLES = path.join(CACHE, "bundles");
const OUT = path.join(CACHE, "out");
const CACHE_GB = Number(env.GEN_CACHE_GB || 20);
const MIN_FREE_GB = Number(env.GEN_MIN_FREE_GB || 10);
const MEDIA_URL = (env.GEN_MEDIA_URL || "http://connector:8000/media").replace(/\/$/, "");
const CTL = env.UPDATER_CTL || "/ctl";
const TTS_URL = (env.TTS_URL || "http://tts:8000").replace(/\/$/, "");
const DEVICE = env.GEN_DEVICE || "cpu";                  // cuda on GPU nodes (compose.gpu.yaml): vulkan allowed
// swangle everywhere by default: in WSL there is no NVIDIA Vulkan driver and Chrome silently falls back to software
// (fi · vot · gpu: vulkan / angle / egl gave the same frames and were not faster). GEN_GL overrides.
const GL_DEFAULT = env.GEN_GL || "swangle";
const OUTPUT_TTL_S = Number(env.GEN_OUTPUT_TTL_S || 7200);
const PROPS_MAX = 256 * 1024;
const REMOTION = JSON.parse(fs.readFileSync(new URL("./node_modules/remotion/package.json", import.meta.url))).version;
const VERSION = fs.readFileSync(new URL("./VERSION", import.meta.url), "utf8").trim();
const HOST_CORES = os.cpus().length;  // the whole machine: for "someone else loads it" (loadavg is host-wide)
// cores this container may use: the cgroup CPU limit (compose deploy.resources.limits.cpus), as Remotion counts them
function cgroupCores() {
  try {
    const [q, p] = fs.readFileSync("/sys/fs/cgroup/cpu.max", "utf8").trim().split(/\s+/);
    if (q !== "max") return Math.max(1, Math.floor(Number(q) / Number(p)));
  } catch { /* cgroup v1 or no limit */ }
  return null;
}
const CORES = Math.min(os.availableParallelism ? os.availableParallelism() : HOST_CORES, cgroupCores() || Infinity);
// memory this container may use: its cgroup limit, else the node's RAM (no limit by default since 0.18.6)
function memBytes() {
  try { const m = fs.readFileSync("/sys/fs/cgroup/memory.max", "utf8").trim(); if (m !== "max") return Number(m); } catch { /* v1 */ }
  return os.totalmem();
}
// default render threads: all cores, but at most one per GB of 3/4 of the memory (a Chrome tab of a 1080p render
// takes ~0.5-1 GB), so an uncapped gen does not push the node into swap
const CONCURRENCY = Math.max(1, Math.min(CORES, Math.floor((memBytes() * 0.75) / 1e9)));

export const SETTINGS = {
  type: "object", additionalProperties: false, properties: {
    part: { type: "string", enum: ["video", "audio", "info"], default: "video" },
    composition: { type: "string", maxLength: 200 },
    props: { type: "object", description: "input props, JSON up to 256 KB" },
    frames: { type: "array", items: { type: "integer", minimum: 0 }, minItems: 2, maxItems: 2,
              description: "[from, to] inclusive; default the whole composition" },
    codec: { type: "string", enum: ["h264", "h265"], default: "h264" },
    crf: { type: "integer", minimum: 0, maximum: 51, default: 18 },
    pix_fmt: { type: "string", enum: ["yuv420p", "yuv444p"], default: "yuv420p" },
    concurrency: { type: "integer", minimum: 0, maximum: 64, default: 0, description: "0: the node decides" },
    gl: { type: "string", enum: ["", "swangle", "angle", "vulkan", "egl"], default: "",
          description: "'': the node decides (swangle on CPU, vulkan on a GPU node)" },
    remotion_version: { type: "string", maxLength: 32 },
    chunk_frames: { type: "integer", minimum: 60, maximum: 1200, default: 300, description: "for the coordinator" },
  },
};

const jobs = new Map();      // id -> job
// audit #38: a job POSTed with lease_s is canceled when the coordinator has not asked about it (status, its files)
// for that long: a part it gave to another node does not keep rendering here. Finished files are left to the TTL.
const LEASE_MAX_S = 86400;
const touch = (j) => { j.seen = now(); };
const queue = [];            // ids waiting
let running = null;          // the job being worked on
const perf = [];             // [time, frames, seconds] of video parts, last hour
const load = { busySince: null, external: false };

const log = (...a) => console.log(new Date().toISOString(), ...a);
const now = () => Date.now() / 1000;
const json = (res, code, body) => { res.writeHead(code, { "Content-Type": "application/json" }); res.end(JSON.stringify(body)); };

// ---------------------------------------------------------------- busy ----------------------------------------
function readJSON(p) { try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch { return null; } }

async function ttsBusy() {
  try {
    const r = await fetch(`${TTS_URL}/health`, { signal: AbortSignal.timeout(2000) });
    const h = await r.json();
    return (h.busy || 0) > 0 || ((h.jobs || {}).running || 0) > 0;
  } catch { return false; }  // no TTS on this node, or it is restarting: not a reason to wait
}

function gpuUtil() {
  return new Promise((resolve) => execFile("nvidia-smi", ["--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
    { timeout: 3000 }, (err, stdout) => resolve(err ? null : Math.max(...stdout.trim().split("\n").map(Number)))));
}

// someone else loads the machine (the owner at the PC): sampled only while gen itself is idle
async function sampleLoad() {
  if (running) return;
  const cpu = os.loadavg()[0] / HOST_CORES;
  const gpu = DEVICE === "cuda" ? await gpuUtil() : null;
  const heavy = cpu > 0.7 || (gpu !== null && gpu > 40);
  if (!heavy) { load.busySince = null; load.external = false; return; }
  load.busySince ??= now();
  load.external = now() - load.busySince >= 60;
}

// why a new part may not start now (null = it may)
async function busyReason() {
  if (env.GEN_PAUSE === "1") return "paused (GEN_PAUSE=1)";
  if (env.NODE_MODE === "gentle") return "economy mode";
  const b = readJSON(path.join(CTL, "busy.json"));
  // connector 0.8.2+: "viewers" = live requests of viewer work (not the coordinator's /gen polling, not /capabilities)
  if (b && (b.viewers ?? b.streams) > 0 && now() - (b.at || 0) < 30) return "serving viewers";
  const st = readJSON(path.join(CTL, "status.json"));
  if (st && st.state === "draining") return "draining for an update";
  if (await ttsBusy()) return "voicing";
  if (load.external) return "the machine is busy";
  return null;
}

// ---------------------------------------------------------------- bundle cache --------------------------------
async function diskFreeGB(dir) {
  const s = await fsp.statfs(dir);
  return (s.bavail * s.bsize) / 1e9;
}

async function dirBytes(dir) {
  let n = 0;
  for (const e of await fsp.readdir(dir, { withFileTypes: true, recursive: true }).catch(() => [])) {
    if (e.isFile()) n += (await fsp.stat(path.join(e.parentPath ?? e.path, e.name)).catch(() => ({ size: 0 }))).size;
  }
  return n;
}

function cachedBundles() {
  try {
    return fs.readdirSync(BUNDLES).filter((d) => /^[0-9a-f]{64}$/.test(d) && fs.existsSync(path.join(BUNDLES, d, "meta.json")));
  } catch { return []; }
}

async function evict(needBytes) {
  const items = [];
  for (const sha of cachedBundles()) {
    const m = readJSON(path.join(BUNDLES, sha, "meta.json"));
    if (running && running.bundle === sha) continue;
    items.push({ sha, bytes: m?.bytes || 0, used: m?.used || 0 });
  }
  let total = items.reduce((s, i) => s + i.bytes, 0);
  items.sort((x, y) => x.used - y.used);
  while (items.length && total + needBytes > CACHE_GB * 1e9) {
    const i = items.shift();
    await fsp.rm(path.join(BUNDLES, i.sha), { recursive: true, force: true });
    total -= i.bytes;
    log("bundle evicted", i.sha.slice(0, 12));
  }
}

function findRoot(dir, depth = 0) {  // the folder of `remotion bundle` output: the shallowest one with index.html
  if (fs.existsSync(path.join(dir, "index.html"))) return dir;
  if (depth > 3) return null;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory()) { const r = findRoot(path.join(dir, e.name), depth + 1); if (r) return r; }
  }
  return null;
}

function remotionVersionIn(dir) {  // from package.json or package-lock.json inside the archive (shallow search)
  const stack = [[dir, 0]];
  while (stack.length) {
    const [d, depth] = stack.pop();
    const lock = readJSON(path.join(d, "package-lock.json"));
    const v = lock?.packages?.["node_modules/remotion"]?.version;
    if (v) return v;
    const pkg = readJSON(path.join(d, "package.json"));
    const dep = pkg?.dependencies?.remotion || pkg?.devDependencies?.remotion;
    if (dep) return dep.replace(/^[\^~=v]+/, "");
    if (depth < 3) for (const e of fs.readdirSync(d, { withFileTypes: true }))
      if (e.isDirectory() && e.name !== "node_modules") stack.push([path.join(d, e.name), depth + 1]);
  }
  return null;
}

function run(cmd, args) {
  return new Promise((resolve, reject) => execFile(cmd, args, { maxBuffer: 1 << 20 }, (err, _o, stderr) =>
    err ? reject(new Error(`${cmd}: ${String(stderr || err.message).slice(0, 300)}`)) : resolve()));
}

// the bundle download runs in curl, a separate process: whatever happens to the transfer cannot take gen down
// (undici's fetch asserted on large bodies, fi · vot · gpu: 600 MB). curl retries itself; sha256 is checked after.
async function download(url, dest, job) {  // -> sha256 hex of what was written
  const hdr = path.join(os.tmpdir(), `gen-auth-${process.pid}-${Date.now()}`);
  await fsp.writeFile(hdr, `Authorization: Bearer ${API_KEY}\n`, { mode: 0o600 });  // not in the process list
  try {
    await new Promise((resolve, reject) => {
      const p = spawn("curl", ["-fsS", "--retry", "5", "--retry-all-errors", "--retry-delay", "3",
                               "--connect-timeout", "15", "--speed-limit", "1024", "--speed-time", "120",
                               "-H", `@${hdr}`, "-o", dest, url], { stdio: ["ignore", "ignore", "pipe"] });
      job.fetch_proc = p;
      let err = "";
      p.stderr.on("data", (d) => { err = (err + d).slice(-500); });
      const tick = setInterval(() => fsp.stat(dest).then((st) => { job.fetched_bytes = st.size; }, () => {}), 1000);
      p.on("close", (code) => {
        clearInterval(tick); job.fetch_proc = null;
        if (job.abort.signal.aborted) return reject(Object.assign(new Error("canceled"), { code: "canceled" }));
        if (code === 0) return resolve();
        const http4 = /error: (4\d\d)/.exec(err);
        reject(Object.assign(new Error(`bundle download: curl ${code} ${err.trim()}`),
                             { code: http4 ? "input_unavailable" : "input_fetch" }));
      });
    });
  } finally {
    await fsp.rm(hdr, { force: true });
  }
  const hash = createHash("sha256");
  await pipeline(fs.createReadStream(dest), hash);
  job.fetched_bytes = (await fsp.stat(dest)).size;
  return hash.digest("hex");
}

async function ensureBundle(job) {
  const { sha256, bytes } = job.input;
  const dir = path.join(BUNDLES, sha256);
  const meta = readJSON(path.join(dir, "meta.json"));
  if (meta) {
    meta.used = now();
    await fsp.writeFile(path.join(dir, "meta.json"), JSON.stringify(meta));
    return meta;
  }
  await fsp.mkdir(BUNDLES, { recursive: true });
  const need = (bytes || 0) * 3;  // archive + unpacked, roughly
  await evict(need);
  if ((await diskFreeGB(BUNDLES)) - need / 1e9 < MIN_FREE_GB) throw Object.assign(new Error(
    `less than ${MIN_FREE_GB} GB would stay free on the disk`), { code: "disk_full" });
  job.status = "fetching";
  const tmp = path.join(BUNDLES, `.${sha256}.${process.pid}.part`);
  let digest;
  try {
    digest = await download(`${MEDIA_URL}/${sha256}`, tmp, job);
  } catch (e) {
    await fsp.rm(tmp, { force: true });
    throw e;
  }
  if (digest !== sha256) {
    await fsp.rm(tmp, { force: true });
    throw Object.assign(new Error("bundle: sha256 does not match"), { code: "input_corrupt" });
  }
  const work = path.join(BUNDLES, `.${sha256}.${process.pid}.dir`);
  await fsp.rm(work, { recursive: true, force: true });
  await fsp.mkdir(work);
  const head = Buffer.alloc(4);
  const fd = await fsp.open(tmp); await fd.read(head, 0, 4, 0); await fd.close();
  try {  // audit #11: a failed or too large unpack leaves nothing behind and is reported as such
    if (head[0] === 0x50 && head[1] === 0x4b) await run("unzip", ["-q", tmp, "-d", work]);
    else await run("tar", ["-xf", tmp, "-C", work, "--no-same-owner", "--no-same-permissions"]);
    if ((await diskFreeGB(BUNDLES)) < MIN_FREE_GB) throw Object.assign(new Error(
      `the unpacked bundle left less than ${MIN_FREE_GB} GB free on the disk`), { code: "disk_full" });
  } catch (e) {
    await fsp.rm(work, { recursive: true, force: true });
    await fsp.rm(tmp, { force: true });
    if (e.code !== "disk_full") e.code = /No space left/i.test(e.message) ? "disk_full" : "input_corrupt";
    throw e;
  }
  await fsp.rm(tmp, { force: true });
  const root = findRoot(work);
  if (!root) { await fsp.rm(work, { recursive: true, force: true }); throw Object.assign(
    new Error("bundle: no index.html (expected the output folder of `remotion bundle`)"), { code: "input_corrupt" }); }
  const m = { sha256, root: path.relative(work, root), remotion: remotionVersionIn(work), bytes: await dirBytes(work),
              used: now(), at: now() };
  await fsp.writeFile(path.join(work, "meta.json"), JSON.stringify(m));
  await fsp.rename(work, dir);
  log("bundle cached", sha256.slice(0, 12), `remotion ${m.remotion || "?"}`, `${(m.bytes / 1e6).toFixed(0)} MB`);
  return m;
}

// ---------------------------------------------------------------- jobs ----------------------------------------
function validate(body) {
  const err = (m) => { throw Object.assign(new Error(m), { status: 422 }); };
  if (!body || typeof body !== "object") err("JSON object expected");
  if (typeof body.id !== "string" || !/^[A-Za-z0-9._-]{1,100}$/.test(body.id)) err("id: 1-100 of A-Z a-z 0-9 . _ -");
  if (body.model !== "remotion") err("model: remotion is the only /gen model on this node");
  if (body.kind !== "render") err("kind: render");
  const s = body.settings || {};
  for (const [k, v] of Object.entries(s)) {
    const spec = SETTINGS.properties[k];
    if (!spec) err(`settings.${k}: not a setting of model remotion`);
    if (spec.type === "integer" && !(Number.isInteger(v) && v >= spec.minimum && v <= spec.maximum))
      err(`settings.${k}: integer ${spec.minimum}..${spec.maximum}`);
    if (spec.type === "string" && (typeof v !== "string" || (spec.enum && !spec.enum.includes(v)) || v.length > (spec.maxLength || 1e9)))
      err(`settings.${k}: ${spec.enum ? spec.enum.map((x) => JSON.stringify(x)).join(" | ") : "string"}`);
  }
  if (!s.composition) err("settings.composition is required");
  if (s.props !== undefined && (typeof s.props !== "object" || Array.isArray(s.props) || s.props === null)) err("settings.props: object");
  if (JSON.stringify(s.props || {}).length > PROPS_MAX) err("settings.props: larger than 256 KB");
  if (s.frames !== undefined && !(Array.isArray(s.frames) && s.frames.length === 2 && s.frames.every((x) => Number.isInteger(x) && x >= 0) && s.frames[0] <= s.frames[1]))
    err("settings.frames: [from, to], integers, from <= to");
  if (s.gl === "vulkan" && DEVICE !== "cuda") err("settings.gl: vulkan only on a GPU node");
  if (s.remotion_version && s.remotion_version !== REMOTION) err(`unsupported_remotion_version: this node renders ${REMOTION}`);
  const inp = (body.inputs || []).find((i) => i && i.role === "bundle");
  if (!inp || !/^[0-9a-f]{64}$/.test(inp.sha256 || "")) err("inputs: [{role: \"bundle\", sha256}] required");
  if (body.lease_s !== undefined && !(Number.isInteger(body.lease_s) && body.lease_s >= 60 && body.lease_s <= LEASE_MAX_S))
    err(`lease_s: integer 60..${LEASE_MAX_S}`);
  return { settings: s, input: { sha256: inp.sha256, bytes: Number(inp.bytes) || 0 }, lease_s: body.lease_s || 0 };
}

function view(j) {
  return { id: j.id, model: "remotion", kind: "render", status: j.status, progress: j.progress, frames_done: j.frames_done,
           frames: j.frames, eta_s: j.eta_s ?? null, error: j.error || null, error_code: j.error_code || null,
           frames_total: j.info?.frames_total ?? null, outputs: j.outputs, busy_reason: j.busy_reason || null,
           fetched_bytes: j.fetched_bytes || 0, created_at: j.created, started_at: j.started || null, ended_at: j.ended || null };
}

async function pump() {
  if (running || !queue.length) return;
  const reason = await busyReason();
  for (const id of queue) { const j = jobs.get(id); j.status = reason ? "paused" : "queued"; j.busy_reason = reason; }
  if (reason || running) return;
  const job = jobs.get(queue.shift());
  if (!job) return;
  running = job;
  try { await work(job); }
  catch (e) {
    if (job.status !== "canceled") { job.status = "error"; job.error = e.message; job.error_code = e.code || job.error_code || "failed"; }
    log("job", job.id, job.status, job.error || "");
  } finally {
    if (job.status !== "done" && job.partial) await fsp.rm(job.partial, { force: true }).catch(() => {});
    job.ended = now(); job.proc = null; running = null;
    setImmediate(pump);
  }
}

async function work(job) {
  job.started = now();
  const meta = await ensureBundle(job);
  if (job.status === "canceled") return;
  if (meta.remotion && meta.remotion !== REMOTION)
    throw Object.assign(new Error(`unsupported_remotion_version: the bundle uses remotion ${meta.remotion}, this node renders ${REMOTION}`),
                        { code: "unsupported_remotion_version" });
  const s = job.settings;
  const part = s.part || "video";
  await fsp.mkdir(OUT, { recursive: true });
  const file = path.join(OUT, `${job.id}.${part === "audio" ? "wav" : "mp4"}`);
  job.partial = file;  // removed when the part does not end "done" (audit #11)
  if ((await diskFreeGB(OUT)) < MIN_FREE_GB) throw Object.assign(new Error(
    `less than ${MIN_FREE_GB} GB free on the disk for the output`), { code: "disk_full" });
  const req = { serveUrl: path.join(BUNDLES, job.input.sha256, meta.root), composition: s.composition, props: s.props || {},
                part, frames: s.frames, codec: s.codec || "h264", crf: s.crf ?? 18, pix_fmt: s.pix_fmt || "yuv420p",
                concurrency: Math.min(CORES, s.concurrency || CONCURRENCY),  // Remotion refuses more than CORES
                gl: s.gl || GL_DEFAULT, output: file };
  job.status = "running";
  const t0 = now();
  const res = await new Promise((resolve) => {
    const p = spawn("nice", ["-n", "10", process.execPath, new URL("./render.mjs", import.meta.url).pathname],
                    { stdio: ["pipe", "pipe", "pipe"], detached: true });
    job.proc = p;
    let buf = "", stderr = "", last = null;
    p.stdout.on("data", (d) => {
      buf += d;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        let m; try { m = JSON.parse(line); } catch { continue; }
        if (m.type === "info") {
          job.info = m;
          if (part !== "info") job.frames = (s.frames ? s.frames[1] - s.frames[0] : m.frames_total - 1) + 1;
        } else if (m.type === "progress") {
          job.frames_done = m.frames_done; job.progress = Math.round(m.progress * 1000) / 1000;
          const el = now() - t0; job.eta_s = m.progress > 0.02 ? Math.round(el / m.progress - el) : null;
        } else last = m;
      }
    });
    p.stderr.on("data", (d) => { stderr = (stderr + d).slice(-2000); });
    p.stdin.end(JSON.stringify(req));
    const watch = setInterval(async () => {  // the output grows: stop before the disk is full (audit #11)
      if ((await diskFreeGB(OUT).catch(() => Infinity)) < MIN_FREE_GB / 2) {
        job.disk_full = true;
        try { process.kill(-p.pid, "SIGKILL"); } catch { /* gone */ }
      }
    }, 5000);
    p.on("close", (code) => { clearInterval(watch); resolve({ code, last, stderr }); });
  });
  if (job.status === "canceled") return;
  if (job.disk_full) throw Object.assign(new Error(`the disk ran low (< ${MIN_FREE_GB / 2} GB free) while rendering`),
                                         { code: "disk_full" });
  const last = res.last || (part === "info" && res.code === 0 && job.info ? { type: "info" } : null);
  if (!last || last.type === "error") {
    if (last?.frames_total) job.info = { ...(job.info || {}), frames_total: last.frames_total };
    throw Object.assign(new Error(last?.message || `render exited ${res.code}: ${res.stderr.trim().slice(-300)}`),
                        { code: last?.code || "render_failed" });
  }
  const info = job.info || {};
  if (part === "info") {
    job.outputs = [{ frames_total: info.frames_total, fps: info.fps, width: info.width, height: info.height, seconds: info.seconds }];
  } else {
    const hash = createHash("sha256");
    await pipeline(fs.createReadStream(file), hash);
    const sha = hash.digest("hex");
    const final = path.join(OUT, sha + path.extname(file));
    await fsp.rename(file, final);
    const bytes = (await fsp.stat(final)).size;
    job.outputs = [{ sha256: sha, mime: part === "audio" ? "audio/wav" : "video/mp4", bytes, frames: last.frames,
                     seconds: Math.round(last.seconds * 1000) / 1000, frames_total: info.frames_total, fps: info.fps,
                     width: info.width, height: info.height }];
    job.files = [final];
    if (part === "video") perf.push([now(), last.frames, now() - t0]);
  }
  job.status = "done"; job.progress = 1; job.frames_done = job.frames || 0; job.eta_s = 0;
  log("job", job.id, "done", part, `${(now() - t0).toFixed(1)} s`);
}

function cancel(job) {
  if (["done", "error", "canceled"].includes(job.status)) return;
  job.status = "canceled"; job.ended = now();
  job.abort.abort();
  if (job.fetch_proc) { try { job.fetch_proc.kill("SIGKILL"); } catch { /* gone */ } }
  const i = queue.indexOf(job.id); if (i >= 0) queue.splice(i, 1);
  if (job.proc) { try { process.kill(-job.proc.pid, "SIGKILL"); } catch { /* gone */ } }  // render.mjs and its Chrome
}

async function drop(job) {
  cancel(job);
  jobs.delete(job.id);
  // two parts can render to the same bytes (same sha256 = the same file): it goes with the last job using it (#22)
  const inUse = new Set([...jobs.values()].flatMap((j) => j.files || []));
  for (const f of job.files || []) if (!inUse.has(f)) await fsp.rm(f, { force: true });
}

function fpsHour() {
  const cut = now() - 3600;
  while (perf.length && perf[0][0] < cut) perf.shift();
  const f = perf.reduce((s, p) => s + p[1], 0), t = perf.reduce((s, p) => s + p[2], 0);
  return { fps: t ? Math.round((f / t) * 100) / 100 : null, frames: f, seconds: Math.round(t), parts: perf.length };
}

// ---------------------------------------------------------------- http ----------------------------------------
async function serveFile(req, res, sha) {
  const own = [...jobs.values()].filter((j) => (j.files || []).some((p) => path.basename(p).startsWith(sha + ".")));
  own.forEach(touch);
  const f = own.flatMap((j) => j.files).find((p) => path.basename(p).startsWith(sha + "."));
  let fh;
  try { fh = f && await fsp.open(f); } catch { fh = null; }
  if (!fh) return json(res, 404, { detail: "no such file" });
  const size = (await fh.stat()).size;
  const send = (opts) => {  // from the open handle: a file removed meanwhile still streams; a read error ends the reply
    const st = fh.createReadStream({ ...opts, autoClose: true });
    st.on("error", () => res.destroy());
    st.pipe(res);
  };
  const type = f.endsWith(".wav") ? "audio/wav" : "video/mp4";
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || "");
  if (m && (m[1] || m[2])) {
    let start = m[1] ? Number(m[1]) : size - Number(m[2]);
    let end = m[1] && m[2] ? Number(m[2]) : size - 1;
    if (start < 0) start = 0;
    if (start >= size || end < start) { await fh.close(); res.writeHead(416, { "Content-Range": `bytes */${size}` }); return res.end(); }
    end = Math.min(end, size - 1);
    res.writeHead(206, { "Content-Type": type, "Content-Length": end - start + 1, "Content-Range": `bytes ${start}-${end}/${size}`,
                         "Accept-Ranges": "bytes" });
    return send({ start, end });
  }
  res.writeHead(200, { "Content-Type": type, "Content-Length": size, "Accept-Ranges": "bytes" });
  send({});
}

async function health() {
  const reason = running ? null : await busyReason();
  return { status: "ok", version: VERSION, remotion: REMOTION, device: DEVICE, gl_default: GL_DEFAULT, slots: 1,
           busy: Boolean(running) || Boolean(reason), busy_reason: running ? "rendering" : reason,
           paused: env.GEN_PAUSE === "1", running: running ? running.id : null, queued: queue.length,
           // finished results the coordinator has not fetched and deleted yet: the updater waits for them (#16)
           unfetched: [...jobs.values()].filter((j) => j.status === "done" && (j.files || []).length).length,
           lease_s_max: LEASE_MAX_S,
           bundles: cachedBundles(), cache_gb: CACHE_GB, perf: fpsHour(), settings: SETTINGS, versions: [REMOTION],
           cores: CORES, host_cores: HOST_CORES, concurrency_default: CONCURRENCY,
           memory_gb: Math.round(memBytes() / 1e8) / 10,
           cpu_free_cores: Math.max(0, Math.min(CORES, Math.round((HOST_CORES - os.loadavg()[0]) * 10) / 10)) };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  const t0 = Date.now();
  res.on("finish", () => { const ms = Date.now() - t0; if (ms > 1000) log(`slow request: ${req.method} ${url.pathname} ${ms} ms`); });
  try {
    if (url.pathname === "/health") return json(res, 200, await health());
    if (req.headers.authorization !== `Bearer ${API_KEY}` || !API_KEY) return json(res, 401, { detail: "invalid token" });
    let m;
    if (req.method === "POST" && url.pathname === "/v1/jobs") {
      let raw = "";
      for await (const c of req) { raw += c; if (raw.length > PROPS_MAX + 64 * 1024) return json(res, 413, { detail: "body too large" }); }
      let body; try { body = JSON.parse(raw); } catch { return json(res, 422, { detail: "invalid JSON" }); }
      let v; try { v = validate(body); } catch (e) { return json(res, e.status || 422, { detail: e.message }); }
      const old = jobs.get(body.id);
      if (old) return json(res, 202, view(old));  // the same id again: the same job (idempotent)
      const job = { id: body.id, ...v, status: "queued", progress: 0, frames_done: 0, frames: null, outputs: [],
                    created: now(), seen: now(), abort: new AbortController() };
      jobs.set(job.id, job); queue.push(job.id);
      setImmediate(pump);
      return json(res, 202, view(job));
    }
    if ((m = /^\/v1\/jobs\/([^/]+)$/.exec(url.pathname))) {
      const job = jobs.get(decodeURIComponent(m[1]));
      if (!job) return json(res, 404, { detail: "no such job" });
      touch(job);
      if (req.method === "GET") return json(res, 200, view(job));
      if (req.method === "DELETE") { const was = job.status; await drop(job); return json(res, 200, { id: job.id, status: was === "done" ? "deleted" : "canceled" }); }
    }
    if (req.method === "GET" && url.pathname === "/v1/jobs") {
      for (const j of jobs.values()) touch(j);
      return json(res, 200, { jobs: [...jobs.values()].map(view) });
    }
    if (req.method === "GET" && (m = /^\/v1\/files\/([0-9a-f]{64})$/.exec(url.pathname))) return serveFile(req, res, m[1]);
    json(res, 404, { detail: "not found" });
  } catch (e) {
    log("error", e.stack || e);
    if (!res.headersSent) json(res, 500, { detail: "internal error" });
  }
});

// last line of defence: an unexpected error fails the running part instead of ending the service
process.on("uncaughtException", (e) => {
  log("uncaught:", e && e.stack || e);
  if (running && !["done", "canceled"].includes(running.status)) {
    running.status = "error"; running.error = String(e && e.message || e).slice(0, 300); running.error_code = "internal";
  }
});
process.on("unhandledRejection", (e) => log("unhandled rejection:", e && e.stack || e));
function expireLeases() {
  for (const j of jobs.values()) {
    if (!j.lease_s || ["done", "error", "canceled"].includes(j.status) || now() - j.seen <= j.lease_s) continue;
    log("job", j.id, `lease expired: no request about it for ${j.lease_s} s`);
    cancel(j); j.error = `lease expired: no request about it for ${j.lease_s} s`; j.error_code = "lease_expired";
  }
}
setInterval(() => { expireLeases(); sampleLoad().catch(() => {}); pump().catch(() => {}); }, 5000);
let tick = Date.now();  // event-loop lag: a blocked loop would delay every status request
setInterval(() => { const lag = Date.now() - tick - 1000; if (lag > 1000) log(`event loop lagged ${lag} ms`); tick = Date.now(); }, 1000);
setInterval(async () => {  // results the coordinator did not fetch and delete
  const cut = now() - OUTPUT_TTL_S;
  for (const j of [...jobs.values()]) if (j.ended && j.ended < cut) await drop(j);
}, 300000);
fs.rmSync(OUT, { recursive: true, force: true });  // parts of a previous run are of no use (the coordinator retries)
for (const d of (() => { try { return fs.readdirSync(BUNDLES); } catch { return []; } })())  // half-done downloads, unpacks
  if (/^\.[0-9a-f]{64}\.\d+\.(part|dir)$/.test(d)) fs.rmSync(path.join(BUNDLES, d), { recursive: true, force: true });
server.listen(8000, "0.0.0.0", () => log(`gen ${VERSION}: remotion ${REMOTION}, ${DEVICE}, gl ${GL_DEFAULT}, ${CORES} of ${HOST_CORES} cores, ${(memBytes() / 1e9).toFixed(1)} GB, ${CONCURRENCY} render threads by default`));
