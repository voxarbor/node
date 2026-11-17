# VoxArbor Node

*English version. The main README is in Ukrainian: [README.md](README.md).*

The worker node of [VoxArbor UA](https://github.com/voxarbor): self-hosted voice services for Ukrainian. Speech-to-text,
text-to-speech, translation into Ukrainian, punctuation restoration and speaker diarization, behind one gateway port
with one token. Runs on one PC or as one of many worker nodes of a coordinator. No cloud, no third-party API keys.

**What is open.** The node (all services, the updater) and its installer `install.sh` are open: this repository. The
VoxArbor coordinator (the site and the scheduling across nodes) is closed; a node gives it only requests to its voice
services, no access to your computer (docs/security.md, Ukrainian). About your PC it sees only general facts (CPU and GPU model,
threads, RAM size, OS name, measured speed), no serials, MAC addresses, user names or paths (full list in
docs/security.md). NLLB-200 (non-commercial, CC-BY-NC) is off by default.

The idea of VoxArbor: people pool their computers into a network that translates and voices videos in Ukrainian.
More at [github.com/voxarbor](https://github.com/voxarbor).

It runs on a CPU alone, but slowly: on an old laptop (i7-4800MQ) speech recognition is slower than real time and the
first voiced phrase of a video comes after ~42 s; with an NVIDIA GPU (RTX 5070 Ti Laptop) after ~2 s.

| path (gateway) | what |
|---|---|
| `/tts/v1/audio/speech`, `/tts/v1/audio/speech/stream`, `/tts/v1/jobs` | Ukrainian TTS (HolosTTS; StyleTTS2 opt-in): whole lectures, streaming (first sound ~1.5 s warm), jobs with history and subtitles; Markdown/URL cleanup, English names, numbers, Roman numerals, initials, brackets, stress lexicon |
| `/stt/v1/audio/transcriptions` | Whisper large-v3-turbo (OpenAI-compatible) |
| `/translate/v1/translate`, `/translate/v1/punctuate` | → Ukrainian: Opus-MT (NLLB-200 opt-in), punctuation + capitals + sentences for raw captions |
| `/diarize/v1/diarize` | who speaks when, each speaker's gender (pyannote community-1 on a GPU, sherpa-onnx on a CPU) |
| `/fetch/v1/info`, `/fetch/v1/subs`, `/fetch/v1/audio` | optional (`COMPOSE_PROFILES=fetch`): YouTube metadata, subtitles and audio fetched from this node's IP |
| `/capabilities` | what this node runs (schema 1) |

## Easiest: the command from the coordinator's site

For a node of the VoxArbor network, the coordinator's site has a ready install command in «Мої вузли» (My nodes) for
Linux and WSL: copy it into a terminal; it installs the node and links it to your account. It is this repository's
`install.sh`; how to check its SHA256 before running it and what it does step by step: docs/install.md.

## Run by hand

Docker with compose v2, ~25 GB disk (~35 GB with a GPU), 16 GB RAM and 4+ cores recommended.
```
cp .env.example .env
sed -i "s/^NODE_TOKEN=.*/NODE_TOKEN=$(openssl rand -hex 32)/" .env
docker compose --env-file .env pull
docker compose --env-file .env --profile init run --rm init-opus     # once: the translation models (~3.7 GB)
docker compose --env-file .env up -d
```
The first start pulls the images (~11 GB without a GPU, ~27 GB with one), ~3.7 GB of translation models and the
Whisper model. Then every request carries `Authorization: Bearer <NODE_TOKEN>` to `http://<host>:8790`
(`GATEWAY_PORT`). Optional web UI «Озвучка» (history, streaming playback, voice and lexicon tools):
`docker compose -f compose.yaml -f compose.ui.yaml --env-file .env up -d`.

Behind NAT / for a public coordinator: the node can dial out instead (`COORDINATOR_URL` + `NODE_JOIN_CODE` in `.env`,
docs/connect.md); then `GATEWAY_PORT` need not be open.

NVIDIA GPU: `COMPOSE_FILE=compose.yaml:compose.gpu.yaml` in `.env`: STT, TTS and diarization (pyannote) on the GPU. Every plain `docker compose` command
and update keeps it (docs/gateway.md "GPU nodes").

Economy mode: `NODE_MODE=gentle` (one task at a time, half the CPU threads) leaves most of the machine to its owner.

Automatic updates (since 0.13.0): the `updater` service updates the node from signed releases only, in an idle
window, and rolls back on failure. `UPDATER_MODE=auto` (default) | `notify` | `off` in `.env` (docs/updater.md).

Size threads and CPU limits to the machine in `.env`. Models load on demand and unload after 2 minutes idle.

## Docs

- `docs/install.md` (Ukrainian): install (`install.sh` and all its options), update, GPU, economy mode, cleanup.
- `docs/security.md` (Ukrainian): what the node does on the PC, the updater's rights, release verification.
- `docs/updater.md`: automatic updates: security, protocol, rollback.
- `docs/gateway.md`: gateway paths, `/capabilities` contract (schema 1), images, operations.
- `docs/API.md`: the TTS API (parameters, jobs, SSE, lexicon).
- `docs/connect.md`: the node's outbound link to a coordinator (protocol 1).
- `NOTICE`: model licenses. NLLB-200 is non-commercial and StyleTTS2 voices are named after real narrators: both
  are off by default.

## Development

Images are built by GitHub Actions (`.github/workflows/images.yml`) into `ghcr.io/voxarbor/node-<service>` (public, no
login needed; tags `v<release>` from the root `VERSION`, the service's own version, `sha-<12>`, `latest`; pin
`VOICE_CORE_TAG=v<release>` on nodes); `models-opus.yml` builds the Opus-MT data image; `release.yml` attaches the node
files and a signed `manifest.json` to every release. Tests: `tests/test_speech.py` (text pipeline, no models),
`tests/test_pool.py` (worker queue) and `tests/test_connect.py` (connector), run inside the images.

## Community

Questions, help with installation, news: [Спільнота в Telegram](https://t.me/+AnydDXHJOitjMmI0) (Telegram community, Ukrainian). Bugs and
proposals: GitHub Issues.

## License

Apache-2.0 (LICENSE). Models and third-party components keep their own licenses: see NOTICE (some are
opt-in and non-commercial, e.g. NLLB-200).
