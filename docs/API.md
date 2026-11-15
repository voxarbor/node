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
- `GET /v1/voices` -> `{default_engine, default_voice, stress, engines: {holos|styletts2: {default, aliases, favorites, voices, meta: {voice: {gender: male|female|child, label}}, models?}}}`; `label` is for display ("Ярослав — оповідач" for Speaker_53; Holos presets also carry `name` and `description`; an unknown Speaker_N gets "Чоловічий N"; named voices keep their names), requests still use voice ids/aliases
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

## Text markup (since 0.10.1)

What the text may contain besides plain words; `GET /v1/voices` -> `engines.<engine>.markup` and `/capabilities` ->
`services.tts.engines.<engine>.markup` list it for editors as `[{id, title, kind, syntax?, example?, insert?, open?,
close?, param?, values?, min?, max?, min_s?, max_s?, default?, note?}]`. `kind`: `inline` (insert `insert` at the
cursor), `wrap` (put `open`/`close` around the selection), `replace` (rewrite the selected word), `param` (not markup:
the request parameter `param`).

- `+` right after the stressed vowel (`за+мок`); `++` = stressed and lengthened (`за++мок`). A word that already has
  `+`/`++` keeps exactly that mark: neither the stress model nor a lexicon word rule changes it (also when the same
  word appears unmarked or marked differently in the same text).
- Pronunciation of a word: write it as it should sound, in Cyrillic with `+` (`Ма+йнкрафт`); for every text: a
  lexicon rule.
- `{пауза N}` (also `{pause N}`, `{пауза 0,8}`, `{пауза 0.8 с}`): exactly N seconds of silence at that point instead of
  the usual pause, N = 0.1-5 (values outside are clamped). Text right before it without punctuation is read as
  unfinished (as before a comma). It never reaches the captions, `segments[].text`/`spoken`, or the verbalizer;
  `/v1/text/prepare` shows it as `{пауза N}` (also as an item of its own in `stressed`). In jobs and
  `/speech/stream` a pause before the first words or after the last ones is dropped; one-shot `/v1/audio/speech`
  keeps it. With `target_duration` pauses are stretched together with the speech.
- `( )` / `[ ]`: an aside, read as the `brackets` parameter says (aside | skip | announce | voice).
- An empty line between paragraphs: +400 ms; punctuation `, . … — ? !`: natural pauses.

## Custom voices (zero-shot Holos, since 0.9.0)

A new Holos voice from a reference recording, without training: the encoder turns the recording into a vector of
256 numbers, and synthesis uses that vector instead of a preset. Only the owner's own voice or voices whose owners
gave explicit consent; every installed voice carries the coordinator's `consent_ref`.

Best reference: one speaker, 15-30 s of continuous natural speech, close microphone, no music / reverb / noise, no
clipping, the same style as the wanted output. Zero-shot gives a *similar* voice, not an identical one.

- `POST /v1/voices/encode` (multipart, field `audio`: any format ffmpeg reads, max 50 MB, 3 s .. 300 s; over 90 s only
  the first 90 s are used) -> `{vector: [256 float], sha256, dim: 256, model: "holos@<rev12>", duration_s, peak,
  speech_level_db, snr_db, part_similarity, warnings: [{code, detail}]}`. Nothing is stored on the node (audio
  deleted, vector only in the reply). Runs in the TTS worker (queue as for synthesis: 429 when full); ~5-12 s on CPU.
  Warning codes: `short` (< 10 s), `long` (> 90 s, cut), `clipping`, `quiet` (speech < -35 dBFS), `noisy`
  (speech/background < 20 dB), `little_speech` (< 40 % of the recording), `several_speakers` (parts of the recording
  give different voices: `part_similarity` < 0.80; one voice is ~0.98, two ~0.40; checked from 15 s).
  Errors: 413 > 50 MB; 422 unreadable audio or shorter than 3 s.
- `PUT /v1/voices/custom/{id}` `{name (1-64), gender: male|female|child, vector: [256], sha256, consent_ref (1-200)}`
  -> `{id, voice: "custom:<id>", sha256, status: created|replaced}`. `id`: 1-48 of `a-z 0-9 _ -`, starting with a
  letter or digit. `sha256` = sha256 of the vector packed as 256 little-endian float32 (the same value encode
  returns). Errors: 400 bad id or sha256 mismatch; 422 missing/invalid field (e.g. no consent_ref, vector not 256);
  409 the node already holds `TTS_CUSTOM_MAX` (500) voices. Stored in `/cache/custom-voices/<id>.json`.
- `DELETE /v1/voices/custom/{id}` -> `{id, status: "deleted"}`; 404 when missing.
- `GET /v1/voices/custom` -> `{zero_shot: true, finetune: false, voices: {id: {name, gender, sha256, consent_ref,
  created}}}` (no vectors).
- Use: `voice: "custom:<id>"` in every synthesis endpoint and job (engine holos; 422 for an unknown id or another
  engine). `GET /v1/voices` lists them after the presets with `meta: {gender, label, name, custom: true, sha256}`.
- `/capabilities` (node-info): `services.tts.voice_create: {zero_shot, finetune}` and `services.tts.custom_voices:
  {id: sha256}`, so a coordinator can keep every node in sync by hash (PUT on mismatch, DELETE extras).

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
