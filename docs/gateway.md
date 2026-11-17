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

Errors are JSON `{"error", "detail"}`: 400 bad URL, 422 `unavailable` (private, removed, age/region-locked,
upcoming), 429 `bot_check` (YouTube asks to sign in: use another node and let this one rest; also
`services.fetch.last_bot_check`) or `busy` (more than `FETCH_QUEUE_MAX` at once), 504 `timeout`, 502 `fetch_failed`.
Timing: info/subs take 5–40 s (JS challenge solving), so give them a 120 s client timeout.

## GET /capabilities (schema 1)

Fields are only added within a schema version. `voice_core_version` is the release the node runs: its pinned `VOICE_CORE_TAG`, or, when it pins
`latest`, the release baked into the node-info image (since 0.7.4; also `voice_core_tag` and `voice_core_release`). `services.<name>.available` = reachable, `healthy` = its health is OK,
`busy` = jobs submitted and not finished, `queue_max` = above it the service answers 429, `loaded` = models in RAM.
`tts.lexicon_sha256` (sha256 of the lexicon file text, as `GET /tts/v1/lexicon` returns it) and `lexicon_rules`
let a coordinator check that a node's stress lexicon matches its source; push a new one with `PUT /tts/v1/lexicon`.
A service whose /health is slow (over 3 s) keeps its last known state for up to 120 s, marked `stale: true` with
`stale_s` (since 0.10.7), so a busy node is not reported as gone. `services.tts.features` lists optional request features (`lexicon_off` since 0.10.6). TTS engines carry `markup` (what a phrase editor may offer as buttons, docs/API.md "Text markup", since 0.10.1). `services.diarize.gender_model` names the gender classifier (`wespeaker-lr-real-1` since 0.12.1: real voices, calibrated; older nodes report `tts-voices-0`); with the calibrated model a client can trust `gender_confidence` ≥ 0.25 (held-out: 97 % of speakers answered, 96 % right). Every service has `device` (`cpu` | `cuda`) since 0.8.0. `fetch` (when enabled) adds `ytdlp_version`, `deno_version`, `last_bot_check`, `last_ok`; when it is off it shows
`available: false`.
TTS gives every voice's `genders` (male | female | child) and display `labels`; `voices` order = priority (favorites
first). Real example from a test node (lists shortened with "..."; `local_ai_version` is a deprecated alias of
`voice_core_version`):

```json
{
  "schema": 1,
  "voice_core_version": "0.6.0", "local_ai_version": "0.6.0",
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

## Updater (since 0.13.0)

`/capabilities.updater` (present when the node runs node-updater): `version`, `mode` (auto | notify | off, effective),
`state` (idle | available | downloading | waiting_window | updating | checking | rolled_back | paused | error | off),
`desired`, `available`, `error`, `seq`, `last_update` `{from, to, ok, at, error}`. Design and protocol:
docs/updater.md.

## Gentle mode (since 0.12.3)

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
  `want=$(sed -n 's/^OPUS_MODELS_TAG=//p' .env); want=${want:-1}`,
  `have=$(docker compose run --rm --no-deps --entrypoint cat translate /cache/opus-mt/.models-version 2>/dev/null)`,
  `[ "$have" = "$want" ] || docker compose --profile init run --rm init-opus`, then `docker compose pull` and
  `docker compose up -d`, and finally `./prune.sh`.
- `./prune.sh` (`--dry-run` to only list) removes the node's own images the current configuration no longer
  references: old tags of ghcr.io/voxarbor/node-* and the speaches STT image, the models image after `init-opus`,
  and untagged layers of this project (by its source label). Nothing else on the host is touched.
- Images share layers: tts and diarize are built on node-base-av (python + ffmpeg) and node-base-torch (cpu or
  cu128), so a node stores ffmpeg and torch once.

## Internal names (kept on purpose)

The product is called VoxArbor Node (voxarbor-node) since 0.11.0; it was voice-core before. These internal names stay
as they were, so existing nodes need no migration (renaming volumes would mean copying gigabytes of models and
risking the connector's node token): the compose project `voice-core-worker`, the volumes (`connector-state`,
`hf-cache`, `tts-cache`, …), the `.env` variables (`VOICE_CORE_TAG`, …), the `/capabilities` fields
`voice_core_version` / `voice_core_tag` / `voice_core_release` (now also `product` and `product_version`) and the
`X-Voice-Core-Protocol` header of docs/connect.md. Images are `ghcr.io/voxarbor/node-<service>`.
