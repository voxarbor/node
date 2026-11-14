# voice-core gateway and node contract

A voice-core node runs the voice services (STT, TTS, translate + punctuation, diarize) behind one gateway port with
one token, so one PC or a coordinator spreading work over several PCs can use them through a single stable contract.

## Images (GHCR, private)

Built by `.github/workflows/images.yml` on every push to `main` that touches a service (only that service is
rebuilt; "Run workflow" rebuilds all). Tags: the service version (`services/<name>/VERSION`), `sha-<12>`, `latest`.

| image | what |
|---|---|
| `ghcr.io/voxarbor/node-tts` | TTS API (UI off on nodes); HolosTTS + StyleTTS2 + accentuk **light** weights are inside (~7 GB) |
| `ghcr.io/voxarbor/node-translate` | translate + punctuation; models download on first use into a volume |
| `ghcr.io/voxarbor/node-diarize` | diarization; its two small ONNX models (~35 MB) are inside |
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
| `/capabilities` | this node's description (below) |

Request bodies and responses are streamed (no buffering: TTS streams, SSE, diarize uploads); upstream response
timeouts are 30–60 min. 

## GET /capabilities (schema 1)

Fields are only added within a schema version. `services.<name>.available` = reachable, `healthy` = its health is OK,
`busy` = jobs submitted and not finished, `queue_max` = above it the service answers 429, `loaded` = models in RAM.
`tts.lexicon_sha256` (sha256 of the lexicon file text, as `GET /tts/v1/lexicon` returns it) and `lexicon_rules`
let a coordinator check that a node's stress lexicon matches its source; push a new one with `PUT /tts/v1/lexicon`.
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
            "Speaker_0": "Чоловічий 1",
            "Speaker_1": "Чоловічий 2",
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
    "diarize": "/diarize/"
  }
}
```
