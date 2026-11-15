# Node connect: outbound link to a coordinator (protocol 1)

Status: implemented in voice-core 0.10.0 (service `connector`, approved 2026-11-15). Enable on a node: set
`COORDINATOR_URL` and `NODE_JOIN_CODE` (and optionally `NODE_NAME`) in `.env`, then `docker compose up -d`; check
`docker compose exec connector python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"`
or its log. Self-check: `tests/test_connect.py` (fake coordinator + fake gateway). Nothing in the existing gateway contract (`docs/gateway.md`) changes; this adds a
second transport for the same requests.

## Why

Nodes sit behind NAT at home, and a public coordinator must not open connections to addresses that users type in
(SSRF into private networks). So the node dials out: a `connector` service opens one WebSocket to the coordinator and
serves the coordinator's requests through it. The local gateway port keeps working, so old (coordinator dials the node)
and new (node dials the coordinator) setups run side by side.

## Node side

- New compose service `connector` (image `voice-core-connector`, ~60 MB, stdlib + `websockets` + `aiohttp`, 0.25 CPU /
  128 MB). It runs on every node; without `COORDINATOR_URL` it idles.
- Env: `COORDINATOR_URL` (`https://coord.example`, the WebSocket URL is derived), `NODE_JOIN_CODE` (first start only),
  optional `NODE_NAME`, `CONNECT_MAX_STREAMS` (default 16). `NODE_TOKEN` stays the local gateway token only.
- Two different credentials: the local `NODE_TOKEN` (connector -> local gateway, never sent to the coordinator) and the
  coordinator's `node_token` (connector -> coordinator), kept in the connector's volume `connector-state`
  (`/state/node.json`, mode 600), never in `.env`, so a rotation needs no file edits on the host. The coordinator stores
  only the sha256 of `node_token`.
- TLS: the coordinator's certificate is always verified (system CAs). Only `CONNECT_INSECURE_TLS=1`, meant for tests,
  turns verification off, and the connector then logs a warning on every connect.
- It forwards only to the local gateway `http://gateway:8080` and only these path prefixes: `/stt/ /tts/ /translate/
  /diarize/ /fetch/ /capabilities`. Anything else is refused (`403 forbidden_path`). The connector adds the local
  `Authorization` itself; the coordinator never sends one, and the node token never travels inside request frames.
- The gateway port no longer needs to be reachable from outside; it can be bound to 127.0.0.1 (`GATEWAY_PORT=127.0.0.1:8790`).

## Enrollment (one command on the node)

1. The coordinator admin creates a join code (single-use, short-lived, e.g. 24 h, at least 100 bits of randomness).
2. Installer: `install.sh --coordinator https://coord.example --join <code>` writes `COORDINATOR_URL`, `NODE_NAME` and
   `NODE_JOIN_CODE` into `.env` and starts the stack. On its first start the connector calls
   `POST {COORDINATOR_URL}/v1/nodes/enroll {code, request_id, name, connector_version, node_id?}` -> `200 {node_id,
   node_token}` and stores the result in `/state/node.json`.
   - `request_id`: a random id the connector generates once per enrollment attempt and keeps in `/state`; repeating
     `{code, request_id}` within 10 min returns the same `node_id` and a fresh `node_token` (a lost reply does not
     strand the node).
   - `node_id` (optional): a reinstalled node of the same owner asks for its old record instead of a duplicate; the
     coordinator honours it only if that node belongs to the code's owner.
   - Errors: 401 bad or used code, 410 expired, 429 + `Retry-After` (attempt limit per code and per IP). When that file exists the connector never enrolls again and ignores
   `NODE_JOIN_CODE` (logging once that it can be removed from `.env`; the code is single-use anyway).
3. Re-enrollment (since 0.10.3): `/state/node.json` also keeps `join_code_sha256` (the sha256 of the code it enrolled
   with, never the code). Every enroll after the first sends the node's old `node_id` (from `/state/node.json`), so the
   coordinator keeps the same record (rules, history) for a node of the same owner. When the coordinator refuses the
   node (close 4001 or HTTP 401 at the upgrade, e.g. revoked) and `.env` holds a NODE_JOIN_CODE different from the one
   it already used, the connector enrolls again by itself at once (with that `node_id`) and connects with the new
   token; with the used code it just waits (10 min) as before. So after a revocation the admin only gives the node
   a new join code (edit `.env`, `docker compose up -d`).
4. From then on the node authenticates with `node_token`. Revoking it in the coordinator closes the node out (4001);
   re-enrolling takes a new join code. The coordinator can rotate it at any time (`rotate_token`, below).

## Connection

`GET wss://coord.example/v1/nodes/connect` with headers `Authorization: Bearer <node_token>`,
`X-Voice-Core-Protocol: 1`. TLS required (`ws://` only for `localhost` / tests). One connection per node: a second
connection with the same token replaces the first (the old one is closed with 4003).

Frames:
- **text frames**: one JSON object each, `{"type": ...}`, at most 1 MiB (capabilities can be large); unknown types and
  unknown fields are ignored (additive changes need no new protocol version).
- **binary frames**: body chunks: 4 bytes stream id (uint32 big-endian) + payload, max 256 KiB payload per frame.

### Session

| direction | frame | fields |
|---|---|---|
| node -> coord | `hello` | `protocol: 1`, `node_name` (`NODE_NAME`), `max_streams` (`CONNECT_MAX_STREAMS`), `connector_version`, `voice_core_version`, `capabilities` (the full `/capabilities` object, schema 1) |
| coord -> node | `welcome` | `protocol: 1`, `node_id`, `heartbeat_s` (the coordinator sets it; 10 s recommended, 5-60 accepted), `max_streams` (≤ the node's) |
| node -> coord | `caps` | `capabilities`: sent every 30 s and immediately when a service, voice, lexicon hash or version changes |
| coord -> node | `caps_get` | asks for a `caps` now |
| both | `ping` / `pong` | `t` (sender's clock, ms); each side pings every `heartbeat_s`; no frame at all for 3 × `heartbeat_s` = dead link |
| coord -> node | `rotate_token` | `node_token`: the new credential; the connector writes it to `/state/node.json` (atomically) and answers |
| node -> coord | `rotated` | after the new token is stored. The old token stays valid until `rotated` arrives or the node connects with the new one, whichever is first (a crash mid-rotation never locks the node out); the open connection stays |

### Requests (multiplexed by stream id)

Stream ids are chosen by the coordinator (uint32, never reused within a connection).

| direction | frame | fields |
|---|---|---|
| coord -> node | `req` | `id`, `method`, `path` (with query, e.g. `/tts/v1/jobs?limit=20`), `headers` (allowlist below), `body`: `none` \| `inline` \| `stream`, `inline` (UTF-8 string when `body = inline`, ≤ 64 KiB), optional `deadline_ms` (the connector aborts the local request after it and answers `res_error deadline`), optional `priority` (0 = a viewer is waiting, higher = background; default 1) |
| coord -> node | binary + `req_end` | body chunks when `body = stream`, then `{type: req_end, id}` |
| node -> coord | `res` | `id`, `status`, `headers` (allowlist below) |
| node -> coord | binary + `res_end` | response body chunks (streamed as they come: TTS audio streams, SSE events, encode results), then `{type: res_end, id}` |
| node -> coord | `res_error` | `id`, `code`, `detail`: the request failed before or during the response (see errors) |
| both | `cancel` | `id`: the other side stops the stream (the connector aborts the local HTTP request and frees the slot). Every cancelled stream still ends with exactly one closing frame from the node: `res_error cancelled`, or `res_end` if the response was already complete |
| both | `window` | `id`, `bytes`: flow control credit, see below |

Request headers passed: `content-type`, `accept`, `last-event-id`, `range`, `x-request-id`. Response headers passed:
`content-type`, `content-length`, `content-disposition`, `retry-after`, `x-*` (X-RTF, X-Elapsed, X-Audio-Ext, X-Fit …).

**Scheduling**: the connector sends response bodies round-robin across streams (one chunk per stream in turn, viewer
streams with `priority 0` first), so a long download never starves a live TTS stream. Control frames (`res`, `res_end`,
`res_error`, `window`, `ping`, `pong`, `caps`) are never queued behind body chunks.

**Flow control** (so a 1 GB diarize upload or a slow consumer never fills memory): each side may send at most
`window` bytes of body per stream before the receiver grants more. Initial window 1 MiB per stream in each direction;
the receiver sends `window` frames as it consumes data. Body bytes sent past the granted window are a protocol error
(the stream is cancelled).

### Limits and timeouts

- Request body up to 1 GiB (as the gateway), response bodies unlimited (streamed).
- Concurrent streams: `max_streams` from `welcome`; above it the node answers `res_error too_many_streams`.
- The coordinator may cancel a stream whose response grows past its own limit; that is normal behaviour, not an error.
- A stream with no frames for 10 min is cancelled by either side (`idle_timeout`). Upstream response-header timeouts
  are the gateway's (10-60 min per service).
- Reconnect: exponential backoff 1 s, 2 s, 4 s … max 60 s, ±20 % jitter; the backoff resets after 60 s connected.
  Special cases (log + the connector's `/health` show the reason):
  - 4001 / HTTP 401 at upgrade, 4002 / HTTP 426, 4004: wait 10 min;
  - 4003: wait at least 30 s (two nodes sharing a token must not keep kicking each other off);
  - HTTP 429 / 503 at upgrade and 4009: wait `Retry-After` if given, else the normal backoff.

### Errors

`res_error.code`: `forbidden_path` (403), `bad_request` (malformed frame, 400), `too_many_streams` (429),
`upstream_unreachable` (502: local gateway or service down), `upstream_timeout` (504), `deadline` (the request's
`deadline_ms` passed), `idle_timeout`, `cancelled`, `protocol_error`. HTTP errors of the services themselves (401 from a service, 422, 429 busy, …) are normal `res`
frames with that status.

Close codes: `4001` unauthorized (bad/revoked token), `4002` protocol version not supported, `4003` replaced by a newer
connection of the same node, `4004` node disabled by the admin, `4008` heartbeat timeout, `4009` limit exceeded (frame
too large, too many frames or streams), `1001` going away (restart). A refusal at the HTTP upgrade (401, 426, 429, 503 with
`Retry-After`) is handled like the matching close code. On any close every open stream
fails; the coordinator decides whether to retry a request elsewhere (no automatic replay: POSTs are not idempotent).

## Versioning

`protocol` is the transport version (this document = 1); the request/response contract inside is the gateway's
(`/capabilities` schema 1). Additive fields and frame types do not change `protocol`; a breaking change raises it, and a
coordinator serves both for a transition period.

## Rollout

1. voice-core minor release: `connector` service + `COORDINATOR_URL`; nodes without it behave exactly as now.
2. The coordinator adds `/v1/nodes/enroll` and `/v1/nodes/connect`; a node may be reachable both ways meanwhile.
3. When all nodes dial out, close the public gateway ports (bind to 127.0.0.1).
