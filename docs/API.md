# TTS HTTP API (contract for the web UI, the MCP gateway and other clients)

Base: the TTS service (port 8000 in the container; through the gateway: `/tts`) with `Authorization: Bearer <API_KEY>`. The optional web UI (port 8001, `compose.ui.yaml`)
reaches the same API in-process under `/api` (e.g. `/api/v1/jobs`) with its login session instead of the key.
Errors: `{"detail": "..."}` with 4xx/5xx; `429` = the model worker is busy (retry later).

## Speech parameters (shared by every synthesis endpoint)

| field | default | meaning |
|---|---|---|
| `text` (alias `input`) | – | Ukrainian text; `+` after a vowel forces stress, `++` stresses and lengthens it |
| `engine` | `holos` | `holos` \| `styletts2` |
| `voice` | engine default | preset name or alias `male`, `male2`, `male3`, `female`, `female2` (see `/v1/voices`) |
| `model` | `multi` | styletts2 only: `multi` \| `single` |
| `speed` | `1.0` | 0.5..2.0 |
| `pause_ms` | `0` (`/v1/audio/speech`), `150` elsewhere | silence between sentences; paragraphs get +400 ms |
| `verbalize` | `true`* | spell out numbers, dates, abbreviations (*`false` on `/v1/audio/speech` for compatibility) |
| `english` | `true` | Latin words (English names, CamelCase, acronyms) are read the English way: espeak-ng phonetics in Cyrillic, or the lexicon's name rules |
| `clean` | `true` | strip Markdown (headings, lists, emphasis, code fences, `[text](url)` -> text) and shorten bare URLs to their domain |
| `initials` | `letters` | initials next to a surname ("Т. Г. Шевченко", "Шевченко Т. Г.", "А. Прізвище"): `letters` ("те ге Шевченко") or `skip`; a lexicon replace rule (`Т. Г. Шевченко => Тарас Григорович Шевченко`) wins |
| `brackets` | `aside` | text in `()`/`[]`: `aside` (read with comma pauses), `skip`, `announce` ("у дужках: …"), `voice` (second voice); footnotes `[1]` are never read |
| `brackets_announce` | `false` | say "у дужках:" before the bracket text; combines with `aside` and `voice` (main voice announces, second voice reads); ignored with `skip` |
| `brackets_voice` | female favorite (male if the main voice is female) | `brackets=voice`: the second voice, same engine |
| `brackets_gain_db` | `-3` | `brackets=voice`: volume of the aside (-20..6 dB) |
| `target_duration` | – | `/v1/audio/speech` only: make the audio last exactly N seconds (re-synthesis at the needed speed, then a small time-stretch); response headers `X-Fit` (`ok` \| `overflow`), `X-Fit-Speed`, `X-Speech-Duration`, `X-Target-Duration` |
| `fit_max_speed` / `fit_min_speed` | `1.35` / `0.85` | allowed speed-up / slow-down for `target_duration` (× `speed`) |
| `fit_pad` | `end` | shorter speech padded with silence at the end (`end`) or left shorter (`none`) |
| `response_format` | `wav` (`/v1/audio/speech`), `mp3` elsewhere | `wav` (24-bit), `flac`, `mp3` (320k/48 kHz), `opus`; stream also `aac`, `pcm` (s16le 24 kHz mono) |

## Endpoints

- `GET /health` (no auth) -> `{status, loaded, loading (models being loaded by /v1/warmup), idle_ttl, ui, jobs: {queued, running}}`
- `POST /v1/warmup?engine=holos` -> `{loaded, engine}` at once; starts loading the models in the background when they
  are unloaded (cold start ~25 s; warm, the first sound of a stream/job comes ~1.5 s after the request). Call it when
  a page opens so the first synthesis does not wait.
- `GET /v1/voices` -> `{default_engine, default_voice, stress, engines: {holos|styletts2: {default, aliases, favorites, voices, meta: {voice: {gender: male|female|child, label}}, models?}}}`; `label` is for display ("Чоловічий 13" for Speaker_53, consecutive per gender; named voices keep their names), requests still use voice ids/aliases
- `POST /v1/audio/speech` -> whole audio file (≤ 5000 chars). Headers `X-Audio-Duration`, `X-Elapsed`, `X-RTF`, `X-Engine`, `X-Voice` (URL-encoded).
- `POST /v1/audio/speech/stream` -> one continuous audio stream (chunked), audio starts after the first sentence
  (~1.5 s warm, ~25 s when the models were unloaded: call `/v1/warmup` early). ≤ 300 000 chars. Header `X-Segments`. Not stored in history.
- `POST /v1/text/prepare` `{text, verbalize=true, english=true, clean=true, brackets="aside"}` -> `{text, stressed: [..], names: [{word, uk}]}`:
  exactly what the voice will read.
- `POST /v1/text/phonemize` `{text, lang="en-us"}` -> `{lang, items: [{word, ipa, uk}]}`
- Lexicon: `GET /v1/lexicon` -> `{path, rules, text}`; `PUT` (whole file, text/plain or `{"text"}`), `POST`/`DELETE` `{"rule"}`.
  Rule forms: `наприкінці => наприкі+нці` (word), `замок на дверях => замо+к на дверях` (phrase),
  `~ regex => replacement` (regex), `GitHub => гі+тхаб` (name: Latin left side, any Cyrillic right side with `+`),
  `Т. Г. Шевченко => Тарас Григорович Шевченко` / `т. зв. => так званий` (replace: any other text, `+` optional;
  "Т.Г." matches "Т. Г." too).

## Jobs = synthesis with history (long texts, streaming playback in the UI)

Job object:
```json
{"id": "20261113-153012-a1b2c3", "title": "first words…", "status": "queued|running|done|error|canceled",
 "created": "2026-11-13T15:30:12Z", "updated": "…", "chars": 1234,
 "params": {"engine": "holos", "voice": "Speaker_1", "speed": 1.0, "pause_ms": 150, "verbalize": true,
            "english": true, "clean": true, "model": "multi", "response_format": "mp3"},
 "progress": {"done": 3, "total": 12, "percent": 27.5, "eta_s": 95}, "duration_s": 41.2, "elapsed_s": 18.0, "error": null,
 "audio_url": "/v1/jobs/<id>/audio", "subtitles_url": "/v1/jobs/<id>/subtitles"}
```
`progress.percent` counts characters (100 when done); `progress.eta_s` = estimated seconds left while running
(null at the start and when not running). `audio_url`/`subtitles_url` are null until `status == "done"`. URLs are relative to the API root (the UI prefixes `/api`).

- `POST /v1/jobs` speech parameters + optional `title` (≤ 300 000 chars) -> `202` job object. Jobs run one at a
  time in order; their sentences interleave with direct `/v1/audio/speech` calls, so short requests are not blocked.
- `GET /v1/jobs?limit=50&offset=0&q=` -> `{jobs: [job…], total}` newest first (history); `q` searches titles, voices and the whole text.
- `GET /v1/jobs/{id}` -> job object + `text` (input as given) + `segments: [{index, text, spoken, start_s, duration_s, done}]`
  (`text` = cleaned original shown to people, `spoken` = what the voice reads; `start_s` = offset in the final audio).
- `GET /v1/jobs/{id}/events` -> Server-Sent Events. On connect it replays every finished segment, then live:
  - `event: segment` `data: {"index", "text", "start_s", "duration_s", "audio_url": "/v1/jobs/<id>/segments/<index>"}`
  - `event: job` `data: <job object>` (on every status/progress change)
  - the stream ends after a `job` event with status `done`, `error` or `canceled`.
- `GET /v1/jobs/{id}/segments/{index}` -> `audio/wav` (16-bit, 24 kHz) of one finished segment. Kept while the job runs
  and for 1 hour after it ends (then `404`: use the final audio and `start_s`).
- `GET /v1/jobs/{id}/audio` -> final file in `params.response_format` (supports HTTP Range, `?download=1` adds
  `Content-Disposition: attachment`).
- `GET /v1/jobs/{id}/subtitles?format=srt|vtt` -> subtitles of the final audio (one cue per segment).
- `PATCH /v1/jobs/{id}` `{title}` -> job object (rename).
- `POST /v1/jobs/{id}/cancel` -> job object (stops after the current sentence; finished part is kept as audio).
- `DELETE /v1/jobs/{id}` -> `{deleted: id}` (cancels first if running).

Retention: jobs older than `TTS_JOBS_KEEP_DAYS` (30) or beyond `TTS_JOBS_MAX_GB` (5) are deleted, oldest first.
