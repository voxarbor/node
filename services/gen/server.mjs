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
const GL_DEFAULT = env.GEN_GL || (DEVICE === "cuda" ? "vulkan" : "swangle");
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
  if (b && b.streams > 0 && now() - (b.at || 0) < 30) return "serving viewers";
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
  const r = await fetch(`${MEDIA_URL}/${sha256}`, { headers: { Authorization: `Bearer ${API_KEY}` }, signal: job.abort.signal });
  if (!r.ok) throw Object.assign(new Error(`bundle ${sha256.slice(0, 12)}: HTTP ${r.status}`), { code: "input_unavailable" });
  const hash = createHash("sha256");
  let got = 0;
  await pipeline(r.body, async function* (src) {
    for await (const chunk of src) { hash.update(chunk); got += chunk.length; job.fetched_bytes = got; yield chunk; }
  }, fs.createWriteStream(tmp));
  if (hash.digest("hex") !== sha256) {
    await fsp.rm(tmp, { force: true });
    throw Object.assign(new Error("bundle: sha256 does not match"), { code: "input_corrupt" });
  }
  const work = path.join(BUNDLES, `.${sha256}.${process.pid}.dir`);
  await fsp.rm(work, { recursive: true, force: true });
  await fsp.mkdir(work);
  const head = Buffer.alloc(4);
  const fd = await fsp.open(tmp); await fd.read(head, 0, 4, 0); await fd.close();
  if (head[0] === 0x50 && head[1] === 0x4b) await run("unzip", ["-q", tmp, "-d", work]);
  else await run("tar", ["-xf", tmp, "-C", work, "--no-same-owner", "--no-same-permissions"]);
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
  return { settings: s, input: { sha256: inp.sha256, bytes: Number(inp.bytes) || 0 } };
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
  const req = { serveUrl: path.join(BUNDLES, job.input.sha256, meta.root), composition: s.composition, props: s.props || {},
                part, frames: s.frames, codec: s.codec || "h264", crf: s.crf ?? 18, pix_fmt: s.pix_fmt || "yuv420p",
                concurrency: Math.min(CORES, s.concurrency || Math.max(1, Math.floor(CORES / 2))),  // Remotion refuses more
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
    p.on("close", (code) => resolve({ code, last, stderr }));
  });
  if (job.status === "canceled") return;
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
  const i = queue.indexOf(job.id); if (i >= 0) queue.splice(i, 1);
  if (job.proc) { try { process.kill(-job.proc.pid, "SIGKILL"); } catch { /* gone */ } }  // render.mjs and its Chrome
}

async function drop(job) {
  cancel(job);
  for (const f of job.files || []) await fsp.rm(f, { force: true });
  jobs.delete(job.id);
}

function fpsHour() {
  const cut = now() - 3600;
  while (perf.length && perf[0][0] < cut) perf.shift();
  const f = perf.reduce((s, p) => s + p[1], 0), t = perf.reduce((s, p) => s + p[2], 0);
  return { fps: t ? Math.round((f / t) * 100) / 100 : null, frames: f, seconds: Math.round(t), parts: perf.length };
}

// ---------------------------------------------------------------- http ----------------------------------------
async function serveFile(req, res, sha) {
  const f = [...jobs.values()].flatMap((j) => j.files || []).find((p) => path.basename(p).startsWith(sha + "."));
  if (!f || !fs.existsSync(f)) return json(res, 404, { detail: "no such file" });
  const size = (await fsp.stat(f)).size;
  const type = f.endsWith(".wav") ? "audio/wav" : "video/mp4";
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || "");
  if (m && (m[1] || m[2])) {
    let start = m[1] ? Number(m[1]) : size - Number(m[2]);
    let end = m[1] && m[2] ? Number(m[2]) : size - 1;
    if (start < 0) start = 0;
    if (start >= size || end < start) { res.writeHead(416, { "Content-Range": `bytes */${size}` }); return res.end(); }
    end = Math.min(end, size - 1);
    res.writeHead(206, { "Content-Type": type, "Content-Length": end - start + 1, "Content-Range": `bytes ${start}-${end}/${size}`,
                         "Accept-Ranges": "bytes" });
    return fs.createReadStream(f, { start, end }).pipe(res);
  }
  res.writeHead(200, { "Content-Type": type, "Content-Length": size, "Accept-Ranges": "bytes" });
  fs.createReadStream(f).pipe(res);
}

async function health() {
  const reason = running ? null : await busyReason();
  return { status: "ok", version: VERSION, remotion: REMOTION, device: DEVICE, gl_default: GL_DEFAULT, slots: 1,
           busy: Boolean(running) || Boolean(reason), busy_reason: running ? "rendering" : reason,
           paused: env.GEN_PAUSE === "1", running: running ? running.id : null, queued: queue.length,
           bundles: cachedBundles(), cache_gb: CACHE_GB, perf: fpsHour(), settings: SETTINGS, versions: [REMOTION],
           cores: CORES, host_cores: HOST_CORES,
           cpu_free_cores: Math.max(0, Math.min(CORES, Math.round((HOST_CORES - os.loadavg()[0]) * 10) / 10)) };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
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
                    created: now(), abort: new AbortController() };
      jobs.set(job.id, job); queue.push(job.id);
      setImmediate(pump);
      return json(res, 202, view(job));
    }
    if ((m = /^\/v1\/jobs\/([^/]+)$/.exec(url.pathname))) {
      const job = jobs.get(decodeURIComponent(m[1]));
      if (!job) return json(res, 404, { detail: "no such job" });
      if (req.method === "GET") return json(res, 200, view(job));
      if (req.method === "DELETE") { const was = job.status; await drop(job); return json(res, 200, { id: job.id, status: was === "done" ? "deleted" : "canceled" }); }
    }
    if (req.method === "GET" && url.pathname === "/v1/jobs")
      return json(res, 200, { jobs: [...jobs.values()].map(view) });
    if (req.method === "GET" && (m = /^\/v1\/files\/([0-9a-f]{64})$/.exec(url.pathname))) return serveFile(req, res, m[1]);
    json(res, 404, { detail: "not found" });
  } catch (e) {
    log("error", e.stack || e);
    if (!res.headersSent) json(res, 500, { detail: "internal error" });
  }
});

setInterval(() => { sampleLoad().catch(() => {}); pump().catch(() => {}); }, 5000);
setInterval(async () => {  // results the coordinator did not fetch and delete
  const cut = now() - OUTPUT_TTL_S;
  for (const j of [...jobs.values()]) if (j.ended && j.ended < cut) await drop(j);
}, 300000);
fs.rmSync(OUT, { recursive: true, force: true });  // parts of a previous run are of no use (the coordinator retries)
server.listen(8000, "0.0.0.0", () => log(`gen ${VERSION}: remotion ${REMOTION}, ${DEVICE}, gl ${GL_DEFAULT}, ${CORES} of ${HOST_CORES} cores`));
