# voice-core

Self-hosted voice services for Ukrainian, running on CPU: speech-to-text, text-to-speech, translation into
Ukrainian, punctuation restoration and speaker diarization, behind one gateway port with one token. Runs on one PC or as many worker nodes
of a coordinator. No GPU, no cloud, no API keys of third parties.

| path (gateway) | what |
|---|---|
| `/tts/v1/audio/speech`, `/tts/v1/audio/speech/stream`, `/tts/v1/jobs` | Ukrainian TTS (HolosTTS; StyleTTS2 opt-in): whole lectures, streaming (first sound ~1.5 s warm), jobs with history and subtitles; Markdown/URL cleanup, English names, numbers, Roman numerals, initials, brackets, stress lexicon |
| `/stt/v1/audio/transcriptions` | Whisper large-v3-turbo (OpenAI-compatible) |
| `/translate/v1/translate`, `/translate/v1/punctuate` | Opus-MT → Ukrainian (NLLB-200 opt-in), punctuation + capitals + sentences for raw captions |
| `/diarize/v1/diarize` | who speaks when, each speaker's gender |
| `/fetch/v1/info`, `/fetch/v1/subs`, `/fetch/v1/audio` | optional (`COMPOSE_PROFILES=fetch`): YouTube metadata, subtitles and audio fetched from this node's IP |
| `/capabilities` | what this node runs (schema 1) |

## Run

Docker with compose v2, ~25 GB disk, 16 GB RAM recommended (4+ cores).
```
cp .env.example .env
sed -i "s/^NODE_TOKEN=.*/NODE_TOKEN=$(openssl rand -hex 32)/" .env
docker compose --env-file .env up -d
```
The first start pulls the images (~11 GB) and downloads ~3 GB of models. Then every request carries
`Authorization: Bearer <NODE_TOKEN>` to `http://<host>:8790` (`GATEWAY_PORT`). Optional web UI «Озвучка» (history,
streaming playback, voice and lexicon tools): `docker compose -f compose.yaml -f compose.ui.yaml --env-file .env up -d`.

NVIDIA GPU: add `-f compose.gpu.yaml` (STT and TTS on the GPU, docs/gateway.md "GPU nodes").

Size threads and CPU limits to the machine in `.env`. Models load on demand and unload after 10 minutes idle.

## Docs

- `docs/gateway.md`: gateway paths, `/capabilities` contract (schema 1), images, operations.
- `docs/API.md`: the TTS API (parameters, jobs, SSE, lexicon).
- `NOTICE`: model licenses. NLLB-200 is non-commercial and StyleTTS2 voices are named after real narrators: both
  are off by default.

## Development

Images are built by GitHub Actions (`.github/workflows/images.yml`) into `ghcr.io/voxarbor/node-<service>`
(tags `v<release>` from the root `VERSION`, the service's own version, `sha-<12>`, `latest`; pin
`VOICE_CORE_TAG=v<release>` on nodes); `models-opus.yml` builds the Opus-MT data image. Tests:
`tests/test_speech.py` (text pipeline, no models) and `tests/test_pool.py` (worker queue), run inside the images.

License: Apache-2.0.
