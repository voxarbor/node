# Node connect: outbound link to a coordinator (protocol 1) — DRAFT for review

Status: proposal, not implemented. Nothing in the existing gateway contract (`docs/gateway.md`) changes; this adds a
second transport for the same requests.

## Why

Nodes sit behind NAT at home, and a public coordinator must not open connections to addresses that users type in
(SSRF into private networks). So the node dials out: a `connector` service opens one WebSocket to the coordinator and
serves the coordinator's requests through it. The local gateway port keeps working, so old (coordinator dials the node)
and new (node dials the coordinator) setups run side by side.

## Node side

- New compose service `connector` (image `voice-core-connector`, ~60 MB, stdlib + `websockets` + `aiohttp`, 0.25 CPU /
  128 MB). It runs on every node; without `COORDINATOR_URL` it idles.
- Env: `COORDINATOR_URL` (`https://coord.example`, the WebSocket URL is derived), `NODE_TOKEN` (also the local gateway
  token), optional `NODE_NAME`, `CONNECT_MAX_STREAMS` (default 16).
- It forwards only to the local gateway `http://gateway:8080` and only these path prefixes: `/stt/ /tts/ /translate/
  /diarize/ /fetch/ /capabilities`. Anything else is refused (`403 forbidden_path`). The connector adds the local
  `Authorization` itself; the coordinator never sends one, and the node token never travels inside request frames.
- The gateway port no longer needs to be reachable from outside; it can be bound to 127.0.0.1 (`GATEWAY_PORT=127.0.0.1:8790`).

## Enrollment (one command on the node)

1. The coordinator admin creates a join code (one-time, short-lived, e.g. 24 h).
2. Installer: `install.sh --coordinator https://coord.example --join <code>`. It calls
   `POST {COORDINATOR_URL}/v1/nodes/enroll {code, name, connector_version}` -> `200 {node_id, node_token}` (401 bad or
   used code, 410 expired), writes `COORDINATOR_URL` and `NODE_TOKEN=<node_token>` into `.env` and starts the stack.
3. From then on the node authenticates with `node_token`. Revoking it in the coordinator closes the node out (4001).

## Connection

`GET wss://coord.example/v1/nodes/connect` with headers `Authorization: Bearer <node_token>`,
`X-Voice-Core-Protocol: 1`. TLS required (`ws://` only for `localhost` / tests). One connection per node: a second
connection with the same token replaces the first (the old one is closed with 4003).

Frames:
- **text frames**: one JSON object each, `{"type": ...}`; unknown types and unknown fields are ignored (additive changes
  need no new protocol version).
- **binary frames**: body chunks: 4 bytes stream id (uint32 big-endian) + payload, max 256 KiB payload per frame.

### Session

| direction | frame | fields |
|---|---|---|
| node -> coord | `hello` | `protocol: 1`, `connector_version`, `voice_core_version`, `capabilities` (the full `/capabilities` object, schema 1) |
| coord -> node | `welcome` | `protocol: 1`, `node_id`, `heartbeat_s` (default 20), `max_streams` (≤ node's `CONNECT_MAX_STREAMS`) |
| node -> coord | `caps` | `capabilities`: sent every 30 s and immediately when a service, voice, lexicon hash or version changes |
| coord -> node | `caps_get` | asks for a `caps` now |
| both | `ping` / `pong` | `t` (sender's clock, ms); each side pings every `heartbeat_s`; no frame at all for 3 × `heartbeat_s` = dead link |

### Requests (multiplexed by stream id)

Stream ids are chosen by the coordinator (uint32, never reused within a connection).

| direction | frame | fields |
|---|---|---|
| coord -> node | `req` | `id`, `method`, `path` (with query, e.g. `/tts/v1/jobs?limit=20`), `headers` (allowlist below), `body`: `none` \| `inline` \| `stream`, `inline` (UTF-8 string when `body = inline`, ≤ 64 KiB) |
| coord -> node | binary + `req_end` | body chunks when `body = stream`, then `{type: req_end, id}` |
| node -> coord | `res` | `id`, `status`, `headers` (allowlist below) |
| node -> coord | binary + `res_end` | response body chunks (streamed as they come: TTS audio streams, SSE events, encode results), then `{type: res_end, id}` |
| node -> coord | `res_error` | `id`, `code`, `detail`: the request failed before or during the response (see errors) |
| both | `cancel` | `id`: the other side stops the stream (the connector aborts the local HTTP request) |
| both | `window` | `id`, `bytes`: flow control credit, see below |

Request headers passed: `content-type`, `accept`, `last-event-id`, `range`, `x-request-id`. Response headers passed:
`content-type`, `content-length`, `content-disposition`, `retry-after`, `x-*` (X-RTF, X-Elapsed, X-Audio-Ext, X-Fit …).

**Flow control** (so a 1 GB diarize upload or a slow consumer never fills memory): each side may send at most
`window` bytes of body per stream before the receiver grants more. Initial window 1 MiB per stream in each direction;
the receiver sends `window` frames as it consumes data. Body bytes sent past the granted window are a protocol error
(the stream is cancelled).

### Limits and timeouts

- Request body up to 1 GiB (as the gateway), response bodies unlimited (streamed).
- Concurrent streams: `max_streams` from `welcome`; above it the node answers `res_error too_many_streams`.
- A stream with no frames for 10 min is cancelled by either side (`idle_timeout`). Upstream response-header timeouts
  are the gateway's (10-60 min per service).
- Reconnect: exponential backoff 1 s, 2 s, 4 s … max 60 s, ±20 % jitter; the backoff resets after 60 s connected.
  401/4001 and 4002 stop retrying for 10 min (log + `/health` of the connector shows why).

### Errors

`res_error.code`: `forbidden_path` (403), `bad_request` (malformed frame, 400), `too_many_streams` (429),
`upstream_unreachable` (502: local gateway or service down), `upstream_timeout` (504), `idle_timeout`, `cancelled`,
`protocol_error`. HTTP errors of the services themselves (401 from a service, 422, 429 busy, …) are normal `res`
frames with that status.

Close codes: `4001` unauthorized (bad/revoked token), `4002` protocol version not supported, `4003` replaced by a newer
connection of the same node, `4008` heartbeat timeout, `1001` going away (restart). On any close every open stream
fails; the coordinator decides whether to retry a request elsewhere (no automatic replay: POSTs are not idempotent).

## Versioning

`protocol` is the transport version (this document = 1); the request/response contract inside is the gateway's
(`/capabilities` schema 1). Additive fields and frame types do not change `protocol`; a breaking change raises it, and a
coordinator serves both for a transition period.

## Rollout

1. voice-core minor release: `connector` service + `COORDINATOR_URL`; nodes without it behave exactly as now.
2. The coordinator adds `/v1/nodes/enroll` and `/v1/nodes/connect`; a node may be reachable both ways meanwhile.
3. When all nodes dial out, close the public gateway ports (bind to 127.0.0.1).
