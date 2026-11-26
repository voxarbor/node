# VoxArbor Node: gateway and node contract

A VoxArbor Node runs the voice services (STT, TTS, translate + punctuation, diarize) behind one gateway port with
one token, so one PC or a coordinator spreading work over several PCs can use them through a single stable contract.

## Images (GHCR, public)

Built by `.github/workflows/images.yml` on every push to `main` that touches a service (only that service is
rebuilt; "Run workflow" rebuilds all). Tags: the service version (`services/<name>/VERSION`), `sha-<12>`, `latest`.

| image | what |
|---|---|
| `ghcr.io/voxarbor/node-tts` | TTS API (UI off on nodes); HolosTTS + StyleTTS2 + accentuk **light** weights are inside (~7 GB) |
| `ghcr.io/voxarbor/node-tts-cuda` | the same TTS built with CUDA 12.8 PyTorch (incl. RTX 50xx), for `compose.gpu.yaml` |
| `ghcr.io/voxarbor/node-translate` | translate + punctuation; models download on first use into a volume |
| `ghcr.io/voxarbor/node-diarize` | diarization; its two small ONNX models (~35 MB) are inside |
| `ghcr.io/voxarbor/node-fetch` | optional YouTube fetch: yt-dlp + deno (JS challenges), no models (~400 MB) |
| `ghcr.io/voxarbor/node-node-info` | `/capabilities` + the one-shot Whisper download |
| `ghcr.io/voxarbor/node-models-opus:1` | data-only: Opus-MT models (CT2 int8) copied once into the translate volume (manual workflow `models-opus.yml`) |
| `ghcr.io/speaches-ai/speaches:0.8.3-cpu` | STT, upstream public image; the Whisper model downloads once (`init-stt`) |

Why the TTS weights stay in its image: ~1.4 GB of 7 GB, and large downloads at first start are the most fragile
step; a node then starts complete. The other models download at first start into named volumes.

## Run a node

See README "Run". Copy `compose.yaml`, `Caddyfile` and `.env.example` (plus `compose.ui.yaml` for the web UI); no
repository access is needed on the node, only the images. Open `GATEWAY_PORT` only to the coordinator. Update:
`docker compose pull && docker compose up -d`.

## Gateway

`http://<node>:<GATEWAY_PORT>`, every request `Authorization: Bearer <NODE_TOKEN>` (401 otherwise; `/health` open).
Paths are stripped and passed on to the services (TTS contract: `docs/API.md`):

| path | service |
|---|---|
| `/stt/v1/audio/transcriptions` | Whisper (OpenAI-compatible) |
| `/tts/v1/audio/speech`, `/tts/v1/audio/speech/stream`, `/tts/v1/jobs…`, `/tts/v1/voices`, `/tts/v1/warmup` … | TTS |
| `/translate/v1/translate`, `/translate/v1/punctuate`, `/translate/v1/languages` | translate + punctuation |
| `/diarize/v1/diarize` | diarization (multipart up to 1 GB, sessions/windows) |
| `/fetch/v1/info`, `/fetch/v1/subs`, `/fetch/v1/audio` | optional YouTube fetch (below); 502 when not enabled |
| `/capabilities` | this node's description (below) |

Request bodies and responses are streamed (no buffering: TTS streams, SSE, diarize uploads); upstream response
timeouts are 30–60 min. 

Nodes behind NAT can instead dial out to the coordinator over one WebSocket and serve the same requests through it:
`docs/connect.md` (protocol 1, since 0.10.0: service `connector`, enabled by `COORDINATOR_URL`).

## GPU nodes (since 0.8.0)

An NVIDIA node (driver for CUDA 12.8+, nvidia-container-toolkit; check `docker run --rm --gpus all ubuntu:24.04 nvidia-smi`)
adds `compose.gpu.yaml` (best pinned in `.env`: `COMPOSE_FILE=compose.yaml:compose.gpu.yaml`): STT runs the upstream speaches CUDA image (`STT_GPU_IMAGE`, float16), TTS the
`node-tts-cuda` image with `TTS_DEVICE=cuda` and `queue_max` 16 (`TTS_GPU_QUEUE_MAX`); translate, diarize and fetch
stay on the CPU. `/capabilities` then shows `services.stt.device` / `services.tts.device` = `cuda` (TTS reports what its
worker really uses once it has loaded; it falls back to `cpu` if torch sees no GPU) and `node.gpu_name`, `vram_gb`,
`vram_used_gb`, `gpu_util_pct`, `gpu_used`.

## Fetch (optional, since 0.7.0)

For a coordinator whose own IP YouTube blocks: the node fetches with its IP. Off by default; enable with
`COMPOSE_PROFILES=fetch` in `.env`. Only `youtube.com` / `youtu.be` URLs (400 otherwise), no playlists.
Optional cookies: a Netscape `cookies.txt` in the `fetch-config` volume (`/config/cookies.txt`); optional
`YTDLP_PROXY`. yt-dlp goes stale quickly: the coordinator can read `services.fetch.ytdlp_version`.

| request | answer |
|---|---|
| `POST /fetch/v1/info {"url"}` | yt-dlp info dict (`extract_info`, no download) without `formats` and signed URLs; `subtitles` / `automatic_captions` as `{lang: [{ext, name}]}` |
| `POST /fetch/v1/subs {"url", "lang", "kind": "manual"\|"auto"}` | the json3 track as YouTube returns it (404 `no_track`) |
| `POST /fetch/v1/audio {"url"}` | best audio stream as is (no re-encode), header `X-Audio-Ext` (webm, m4a …) |
| `POST /fetch/v1/list {"url", "limit": 1..200 (50)}` | since 0.13.5 (`services.fetch.features` has `"list"`): videos of a channel («Videos» tab), a playlist, or the channel of a given video, newest first, one flat request: `{channel_id, channel, entries: [{id, title, duration, upload_date (often null in flat mode), live}]}`; Shorts left out |

Errors are JSON `{"error", "detail"}`: 400 bad URL, 422 `unavailable` (private, removed, age/region-locked,
upcoming), 429 `bot_check` (YouTube asks to sign in: use another node and let this one rest; also
`services.fetch.last_bot_check`) or `busy` (more than `FETCH_QUEUE_MAX` at once), 504 `timeout`, 502 `fetch_failed`.
Politeness (since 0.13.5): at least `FETCH_MIN_INTERVAL_S` (3) s between yt-dlp starts; after a bot check every request
gets 429 `bot_check` for `FETCH_BOT_COOLDOWN_S` (900) s (`services.fetch.resting_s` shows what is left).
Timing: info/subs take 5–40 s (JS challenge solving), so give them a 120 s client timeout.

## GET /capabilities (schema 1)

Fields are only added within a schema version. `voxarbor_node_version` is the release the node runs: its pinned `VOXARBOR_NODE_TAG`, or, when it pins
`latest`, the release baked into the node-info image (since 0.7.4; also `voxarbor_node_tag` and `voxarbor_node_release`). `services.<name>.available` = reachable, `healthy` = its health is OK,
`busy` = jobs submitted and not finished, `queue_max` = above it the service answers 429, `loaded` = models in RAM.
`tts.lexicon_sha256` (sha256 of the lexicon file text, as `GET /tts/v1/lexicon` returns it) and `lexicon_rules`
let a coordinator check that a node's stress lexicon matches its source; push a new one with `PUT /tts/v1/lexicon`.
A service whose /health is slow (over 3 s) keeps its last known state for up to 120 s, marked `stale: true` with
`stale_s` (since 0.10.7), so a busy node is not reported as gone. `services.tts.features` lists optional request features (`lexicon_off` since 0.10.6). TTS engines carry `markup` (what a phrase editor may offer as buttons, docs/API.md "Text markup", since 0.10.1). `services.diarize.gender_model` names the gender classifier (`wespeaker-lr-real-1` since 0.12.1: real voices, calibrated; older nodes report `tts-voices-0`); with the calibrated model a client can trust `gender_confidence` ≥ 0.25 (held-out: 97 % of speakers answered, 96 % right). Every service has `device` (`cpu` | `cuda`) since 0.8.0. `fetch` (when enabled) adds `ytdlp_version`, `deno_version`, `last_bot_check`, `last_ok`; when it is off it shows
`available: false`.
TTS gives every voice's `genders` (male | female | child) and display `labels`; `voices` order = priority (favorites
first). Real example from a test node (lists shortened with "..."; `local_ai_version` is a deprecated alias of
`voxarbor_node_version`):

```json
{
  "schema": 1,
  "product": "voxarbor-node", "product_version": "v0.16.0", "voxarbor_node_version": "v0.16.0",
  "generated_at": "2026-11-14T07:41:46Z",
  "node": {
    "cpu_cores": 7,
    "ram_total_gb": 26.4,
    "ram_available_gb": 18.0,
    "load_avg": [
      0.84,
      0.92,
      0.87
    ],
    "gpu": false,
    "gpu_used": false
  },
  "services": {
    "stt": {
      "image": "ghcr.io/speaches-ai/speaches:0.8.3-cpu",
      "available": true,
      "healthy": true,
      "loaded": null,
      "busy": null,
      "queue_max": null,
      "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
      "endpoints": [
        "/v1/audio/transcriptions"
      ]
    },
    "tts": {
      "image": "ghcr.io/voxarbor/node-tts:0.6.0",
      "available": true,
      "healthy": true,
      "loaded": true,
      "busy": 0,
      "queue_max": 8,
      "jobs": {
        "queued": 0,
        "running": 0
      },
      "default_engine": "holos",
      "engines": {
        "styletts2": {
          "default": "Денис Денисенко",
          "favorites": [
            "Денис Денисенко",
            "Матвій Ніколаєв",
            "Марина Панас",
            "..."
          ],
          "voices": [
            "Денис Денисенко",
            "Матвій Ніколаєв",
            "Марина Панас",
            "..."
          ],
          "genders": {
            "Анастасія Павленко": "female",
            "Артем Окороков": "male",
            "Вероніка Дорош": "female",
            "...": "..."
          },
          "labels": {
            "Анастасія Павленко": "Анастасія Павленко",
            "Артем Окороков": "Артем Окороков",
            "Вероніка Дорош": "Вероніка Дорош",
            "...": "..."
          }
        },
        "holos": {
          "default": "Speaker_1",
          "favorites": [
            "Speaker_1",
            "Speaker_53",
            "Speaker_30",
            "..."
          ],
          "voices": [
            "Speaker_1",
            "Speaker_53",
            "Speaker_30",
            "..."
          ],
          "genders": {
            "Гаська Шиян": "female",
            "Speaker_0": "male",
            "Speaker_1": "male",
            "...": "..."
          },
          "labels": {
            "Гаська Шиян": "Гаська Шиян",
            "Speaker_0": "Іван — рівний",
            "Speaker_1": "Андрій — основний",
            "...": "..."
          }
        }
      },
      "endpoints": [
        "/v1/audio/speech",
        "/v1/audio/speech/stream",
        "/v1/jobs",
        "/v1/voices",
        "/v1/text/prepare",
        "/v1/text/phonemize",
        "/v1/warmup"
      ]
    },
    "translate": {
      "image": "ghcr.io/voxarbor/node-translate:0.6.0",
      "available": true,
      "healthy": true,
      "loaded": true,
      "busy": 0,
      "queue_max": 8,
      "opus_pairs": [
        "de-zle",
        "en-zle",
        "es-zle",
        "fr-zle",
        "it-zle",
        "pt-zle",
        "zle-zle",
        "zlw-zle"
      ],
      "fallback_model": "nllb-600m",
      "punctuation_model": "1-800-BAD-CODE/xlm-roberta_punctuation_fullstop_truecase",
      "languages": [
        "ar",
        "az",
        "be",
        "bg",
        "cs",
        "..."
      ],
      "endpoints": [
        "/v1/translate",
        "/v1/punctuate",
        "/v1/languages"
      ]
    },
    "diarize": {
      "image": "ghcr.io/voxarbor/node-diarize:0.6.0",
      "available": true,
      "healthy": true,
      "loaded": true,
      "busy": 0,
      "queue_max": 4,
      "model": "pyannote segmentation-3.0 + wespeaker_en_voxceleb_resnet34_LM (sherpa-onnx)",
      "sessions": 0,
      "endpoints": [
        "/v1/diarize"
      ]
    }
  },
  "gateway_paths": {
    "stt": "/stt/",
    "tts": "/tts/",
    "translate": "/translate/",
    "diarize": "/diarize/",
    "fetch": "/fetch/"
  }
}
```

## Hardware and speed (since 0.14.3)

`node`: `cpu_model`, `cpu_threads`, `os` (host's PRETTY_NAME, «· WSL2» under WSL), `gpu_vendor` (nvidia | amd | intel |
none | unknown — WSL passes a GPU that Linux cannot name), `gpu_name` (NVIDIA via nvidia-smi; AMD/Intel via
/sys/class/drm on native Linux), plus the earlier `ram_total_gb`, `vram_gb` …. `services.stt|tts|translate.perf`:
`{jobs, units, seconds, per_second, window_s: 3600}` from real work in the last hour — audio seconds per second of work
(stt: measured by the connector on the coordinator's verbose_json requests; tts: per synthesized segment) and
sentences per second (translate); null/0 jobs when idle. No identifiers are reported (docs/security.md).

## Models per service (since 0.13.2)

Every service in `/capabilities.services` reports the same fields (added, nothing renamed): `model` (human name),
`model_revision` (short HF commit, sha256 prefix of the weights, data version or tool version), `device` (cpu | cuda)
and `engine`. Per service:

| service | engine | model / model_revision | extra |
|---|---|---|---|
| stt | `faster-whisper (speaches)` | `STT_MODEL` / HF commit from the shared cache | |
| tts | `holos` | HolosTTS PyTorch FP32 (+ holos-tts version) / HF commit | `stress_model` (accentuk version and mode), `verbalizer`; voices: `engines` |
| translate | `opus` | Opus-MT tc-big pairs, CT2 int8 / `opus-models-<n>` | `punctuation_model`, `punctuation_revision` |
| diarize | `pyannote` or `sherpa-onnx` | community-1 / HF commit, or the two ONNX models / their sha256 prefixes | `gender_model` |
| fetch | `yt-dlp` | yt-dlp + deno / yt-dlp version | `ytdlp_version`, `deno_version` |

## Models and resources (since 0.17.0)

Two schema 1 additions for the coordinator's model system; old coordinators ignore them, and a coordinator that finds
no `models` builds the list from `services.*` as before.

`models`: one entry per model the node can run, built from the service entries:

| field | meaning |
|---|---|
| `id` | stable model id, the same as in the coordinator's catalog (below) |
| `kind` | tts, stt, translate, punctuate, diarize (later video, image) |
| `engine` | the node's engine name (for tts: as in `services.tts.engines`) |
| `revision` | weights: repository and revision (`patriotyk/HolosTTS@5d40fbaa99e5`, `opus-models-3`) |
| `service` | the gateway service that runs it |
| `device` | cpu or cuda |
| `available`, `loaded`, `busy` | from the service's `/health` |
| `vram_gb` | rough VRAM need on a GPU, 0 on CPU |
| `exclusive` | true: the whole GPU is taken while it runs (none of today's models) |
| `settings` | JSON Schema of the settings the node accepts (empty for today's models) |
| `license` | the weights' license id |

Ids today: `holos`, `styletts2`, `whisper-<name>` (from `STT_MODEL`, e.g. `whisper-large-v3-turbo`), `opus-mt`,
`nllb-200` (only with `TRANSLATE_NLLB=1`), `punct-uk`, `sherpa-diarize` (CPU: pyannote segmentation-3.0 +
WeSpeaker on sherpa-onnx), `pyannote-community-1` (GPU).

`resources`: `gpus` [{`name`, `vram_gb`, `vram_free_gb`}] (empty without an NVIDIA GPU), `ram_gb`, `ram_free_gb`,
`disk_free_gb` (the Docker data disk), `slots` {`gpu`: 1 when a service uses the GPU, `cpu`: the connector's stream
limit, 1 in economy mode}.

## Models in speech requests (since 0.17.0)

`POST /tts/v1/audio/speech` (and `/stream`, `/jobs`) take two more optional fields:

- `model`: a model id from `/capabilities` `models` of kind tts (`holos`, `styletts2`): it picks the engine.
  `multi` / `single` keep their old StyleTTS2 meaning; without `model` nothing changes.
- `settings`: the model's settings, by the JSON Schema in its `models` entry. Unknown keys or values out of range:
  422. Holos and StyleTTS2 take none so far.

`features` of the tts service has `model_settings` from this version on. Custom voices carry `model` (`holos`);
`GET /tts/v1/voices/custom` lists it, `/capabilities` `services.tts.custom_voices` stays {id: sha256} and
`custom_voice_models` adds {id: model}.

OmniVoice (0.17.0-0.17.2, GPU, profile `omnivoice`) was removed in 0.18.0 (owner: poor Ukrainian voicing). The
updater removes what a node still has: the `tts-omnivoice` container, the `omnivoice-models` volume, its image and
`omnivoice` in `COMPOSE_PROFILES` / `TTS_ENGINES`; node-tts drops custom voices of that model. Its schema 1 fields
simply stop appearing.

## Settings of STT and translation (since 0.17.2)

- **STT** (`whisper-*` in `models`): `vad_filter` (boolean, default true: silence and music are skipped),
  `temperature` (0-1, default 0), `hotwords` (string up to 500 characters: names and game terms to prefer). They are
  form fields of `POST /stt/v1/audio/transcriptions` with the same names (speaches 0.8.3). Not available per request
  in speaches 0.8.3: `beam_size` (5) and the VAD silence parameters.
- **Translation** (`opus-mt`, `nllb-200`): `POST /translate/v1/translate` takes `settings`: `beam_size` (1-5, default
  2; overrides the top-level `beam_size`), `max_length` (16-400 tokens per sentence, default 400), `repetition_penalty`
  (1.0-1.5, default 1.1). Unknown keys or values out of range: 422. `/translate/health` publishes the schema.
- **Punctuation** (`punct-uk`) has no settings.

## Generation: /gen, Remotion Render (since 0.18.0)

Optional service `gen` (compose profile `gen`, image `node-gen`: Node 22, Remotion 4.0.545, Chrome Headless Shell).
Model `remotion` (kind `render`) renders one part of a Remotion composition per job. One job at a time.

- `POST /gen/v1/jobs` `{id, model: "remotion", kind: "render", settings: {part: "video" | "audio" | "info",
  composition, props (≤ 256 KB), frames: [from, to] (inclusive), codec: h264 | h265, crf 0-51, pix_fmt: yuv420p |
  yuv444p, concurrency 0-64 (0: half the container's cores; more is clamped to them), gl: "" (swangle) | swangle |
  angle | vulkan (GPU nodes only; not working under WSL) | egl,
  remotion_version}, inputs: [{role: "bundle", sha256, mime, bytes}]}` → 202. The same `id` again is the same job.
  Unknown keys, values out of range, another `remotion_version` → 422.
- `GET /gen/v1/jobs/{id}` → `{status: queued | paused | fetching | running | done | error | canceled, progress,
  frames_done, frames, eta_s, frames_total, error, error_code, busy_reason, outputs}`. `paused`: the part waits
  because the node serves viewers or TTS, drains for an update, runs in economy mode, `GEN_PAUSE=1`, or the machine
  is busy (CPU load > 70 % of the cores or GPU > 40 % for a minute while the node itself is idle). It is a live
  state: the part starts when that ends. A started part is not interrupted.
- outputs: video `[{sha256, mime: video/mp4, bytes, frames, seconds, frames_total, fps, width, height}]` (no audio,
  every part a separate encode starting with a key frame, x264 preset medium); audio `audio/wav` (48 kHz) of
  `frames` or of the whole composition; info `[{frames_total, fps, width, height, seconds}]` without rendering.
  Errors: `frames_out_of_range` (with `frames_total`), `no_composition`, `unsupported_remotion_version`,
  `input_unavailable`, `input_corrupt`, `disk_full`, `render_failed`.
- `GET /gen/v1/files/{sha256}` (Range) → the part; kept until `DELETE /gen/v1/jobs/{id}` or 2 h.
  `DELETE` also cancels a running part at once (the render process and its Chrome are killed).
- Bundle: the archive (tar.gz or zip) of `remotion bundle` output with `package.json` or `package-lock.json`
  inside (the remotion version). The node fetches it once from the coordinator (`GET /v1/nodes/media/{sha256}`
  through the connector's internal `/media/{sha256}`: the node token stays in the connector), checks sha256,
  unpacks it into the `gen-cache` volume: LRU up to `GEN_CACHE_GB` (20), refused when less than `GEN_MIN_FREE_GB`
  (10) would stay free. The download runs in curl (retries, then sha256), so a broken transfer only fails the part.
- `/capabilities` `models[]` `remotion`: `busy`, `paused`, `busy_reason`, `bundles` (cached sha256), `versions`,
  `fps` (frames per second of rendering, last hour), `perf`, `slots` 1, `settings` (JSON Schema). `resources` has
  `cpu_cores` and `cpu_free_cores`.
- An update drains the node as usual; a part still running then is cut by the restart (the coordinator retries it).

## Translation engine

Since 0.16.4 the EN->UK model is our third fine-tune of Opus-MT en-zle (`model_revision` `opus-models-3`; 0.14.0: `opus-models-2`; NOTICE,
tools/opus_finetune); the other pairs are the original Opus-MT tc-big.

Placeholders (since 0.13.6): a sentence whose output lost or doubled an `N0`/`N1`… token is translated again with a
wider search (beam 6, 6 hypotheses) and the first hypothesis that keeps them all is used (our sets: 140/140).

`services.translate.engine` is `opus` (Opus-MT on the CPU) and `model` names the EN->UK model. (0.13.1 had an
optional EuroLLM engine on GPU nodes; it was removed in 0.13.2: on a 12 GB card it took nearly the whole GPU.)

## Updater (since 0.13.0)

`/capabilities.updater` (present when the node runs node-updater): `version`, `mode` (auto | notify | off, effective),
`state` (idle | available | downloading | waiting_window | updating | checking | rolled_back | paused | error | off),
`desired`, `available`, `error`, `seq`, `last_update` `{from, to, ok, at, error}`. Design and protocol:
docs/updater.md.

## Economy mode (`NODE_MODE=gentle`, since 0.12.3)

`NODE_MODE=gentle` in `.env` (then `docker compose up -d`, or the next update) makes the node leave most of the
machine to its owner: the connector takes one task at a time (it tells the coordinator `max_streams: 1`, so GPU work
is serial too) and STT/TTS/translate/diarize use half their configured CPU threads (4/2/2/2 -> 2/1/1/1). Optional
`NODE_CPU_SHARES=128` also lowers the node's CPU weight against other containers on the host (default 256 for STT and
diarize, 1024 for TTS and translate). Default mode `normal`. `/capabilities` reports it as `node.mode`
(`normal` | `gentle`). Only `compose.yaml` and `.env` are involved: no extra files, older update scripts keep working.

## Install, update, prune (since 0.12.0)

- The Opus-MT models are copied into the translate volume by the one-off `init-opus` (compose profile `init`; the
  image node-models-opus is ~3.7 GB and is not needed afterwards). The volume keeps the copied version in
  `opus-mt/.models-version`; translate `/health` and `/capabilities` show it as `opus_models_version`.
- Install: `docker compose pull && docker compose --profile init run --rm init-opus && docker compose up -d`.
- Update (`./update.sh`): fresh node files, then
  `want=$(docker compose config --format json | jq -r '.services["init-opus"].environment.OPUS_MODELS_TAG')` (compose's
  own default, unless `.env` overrides it; 2 since 0.14.0),
  `have=$(docker compose run --rm --no-deps --entrypoint cat translate /cache/opus-mt/.models-version 2>/dev/null)`,
  `[ "$have" = "$want" ] || docker compose --profile init run --rm init-opus`, then `docker compose pull` and
  `docker compose up -d`, and finally `./prune.sh`.
- `./prune.sh` (`--dry-run` to only list) removes the node's own images the current configuration no longer
  references: old tags of ghcr.io/voxarbor/node-* and the speaches STT image, the models image after `init-opus`,
  and untagged layers of this project (by its source label). Nothing else on the host is touched.
- Images share layers: tts and diarize are built on node-base-av (python + ffmpeg) and node-base-torch (cpu or
  cu128), so a node stores ffmpeg and torch once.

## Names

Everything on a node is called voxarbor-node: the compose project `voxarbor-node` (containers
`voxarbor-node-<service>-1`, volumes `voxarbor-node_*`), the `.env` variable `VOXARBOR_NODE_TAG`, the folder
`~/voxarbor-node`, the protocol header `X-VoxArbor-Node-Protocol` and the `/capabilities` keys `voxarbor_node_version`,
`voxarbor_node_tag`, `voxarbor_node_release` (plus `product`, `product_version`). Images: `ghcr.io/voxarbor/node-*`.
