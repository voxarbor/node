# node-updater: automatic node updates (since 0.13.0)

Status: implemented in 0.13.0 (design approved 2026-11-17). Protocol changes are additive to docs/connect.md (protocol 1) and to
`/capabilities` (schema 1).

## Pieces and trust

```
coordinator --(WebSocket, existing)--> connector --(file in volume updater-ctl)--> updater --(docker.sock)--> docker
                                                                                      |
                                                       GitHub release voxarbor/node <-+ (files + signed manifest)
                                                       ghcr.io/voxarbor/node-*@sha256 <-+ (images by digest)
```

- New service `updater` (image `ghcr.io/voxarbor/node-updater`, ~250 MB: python, docker CLI + compose plugin,
  cosign, all pinned with checksums). It is the only container with `/var/run/docker.sock`. No listening port, no coordinator connection.
- The connector never gets the socket. It only writes the coordinator's wish into the shared volume `updater-ctl`
  (`desired.json`) and the node's load (`busy.json`). The updater only reads those two and writes `status.json`.
- What the coordinator can say: "desired version vX.Y.Z" (+ mode, + "now"). Nothing else: no URLs, files, images
  or commands. Worst case of a hostile coordinator or connector: it asks for another *signed* release.
- Downgrades are refused unless `UPDATER_ALLOW_DOWNGRADE=1` in `.env` (rollback after a failed update is local and
  does not need it).

## Release manifest (CI)

`.github/workflows/release.yml` runs on every `v*` tag (pushed after images.yml built the release) and attaches to
the GitHub release:

| asset | content |
|---|---|
| `compose.yaml`, `compose.gpu.yaml`, `Caddyfile`, `prune.sh` | node files of that tag |
| `manifest.json` | `{version, opus_models_tag, files: {name: sha256}, images: {"ghcr.io/voxarbor/node-tts:v0.13.0": "ghcr.io/voxarbor/node-tts@sha256:…", …}, opus_image: {ref, digest_ref}, min_updater}`: every image of the CPU and GPU configurations, keyed by the reference compose uses |
| `manifest.json.sigstore.json` | cosign keyless bundle (GitHub OIDC, Rekor entry) |

The updater verifies the bundle with `cosign verify-blob --bundle … --certificate-oidc-issuer
https://token.actions.githubusercontent.com --certificate-identity
https://github.com/voxarbor/node/.github/workflows/release.yml@refs/tags/<tag>`, then every file by sha256 from the
manifest (since 0.14.4 offline first, against Sigstore's trusted root built into the updater image at its release; live
TUF only when that root is too old, 3 tries; an identity/signature mismatch fails at once). Images are pulled by digest from the manifest and tagged locally as `:<tag>`; compose then runs with
`--pull never`, so nothing unverified is started. If cosign cannot reach Sigstore, the update waits (no fallback to
unsigned).

## Control channel (connector <-> updater, volume `updater-ctl`)

Additive to connect.md (protocol stays 1). Coordinator -> node, any of:
- `welcome` with a field `"update": {...}`;
- its `ping` with a field `"update": {...}`;
- a frame `{"type": "update", "version": "v0.13.1", "mode": "auto", "now": false, "paused": false}` whenever it
  changes (recommended).

`update` fields (all optional): `version` (`vX.Y.Z`; null/absent = no wish), `mode` (`auto` | `notify` | `off`),
`now` (bool), `paused` (bool). Each message replaces the previous wish completely. Older connectors ignore it.

Node -> coordinator: every node `ping` carries `"updater": {"state": "<state>", "seq": <int>}`; `seq` grows on every
change of the updater's public status, so the coordinator sends `caps_get` (or waits for the next `caps`) when it
changes. Capabilities also change by themselves (the connector sends `caps` on any change).
`mode` (optional): the coordinator-side setting from «Мої вузли»; `now`: the «Оновити» button (needed in `notify`
mode); `paused`: the owner paused the node on the site. The connector writes it as `desired.json`
(`{..., "received_at"}`) and every 5 s `busy.json` `{"streams": <active streams>, "at"}`.

Effective mode = the stricter of `.env` `UPDATER_MODE` (auto | notify | off, default auto) and the coordinator's
`mode` (off > notify > auto): the node owner can always switch updates off locally.

| mode | behaviour |
|---|---|
| auto | updates to the desired version in the next safe window |
| notify | only reports `available`; updates when the coordinator sends `now: true` |
| off | does nothing except report its state |

## Safe window

All of: `busy.json` fresh (< 30 s) with 0 streams for 2 minutes; TTS `/health` busy 0 and jobs 0; not `paused`.
`paused` blocks everything, downloads included, and `now` does not override it (pause = «don't touch my PC»).
Downloads (images by digest) run only while the node has no stream (a stream starting cancels the pull, it resumes
later), so they never compete with live work; with `NODE_MODE=gentle` only inside the 2-minute idle window. The
window itself is needed for `up -d` (~1 min of downtime).

Cheap local checks: only this container's own compose project; manifest images only `ghcr.io/voxarbor/node-*`,
`ghcr.io/speaches-ai/speaches` and `caddy`, always `repo@sha256:…` matching the reference's repository.

## Update run

1. Verify manifest and files (above). Pull images by digest, tag them.
2. Wait for the safe window.
3. Back up the current `compose*.yaml`, `Caddyfile`, `prune.sh` and `VOXARBOR_NODE_TAG` to `.updater/prev/`.
4. Write the new files, set `VOXARBOR_NODE_TAG=<tag>` in `.env` (only that line changes).
5. `init-opus` only if the manifest's `opus_models_tag` differs from the volume's `.models-version`.
6. `docker compose up -d --pull never --remove-orphans`, for all services except `updater`.
7. Health check, up to `UPDATER_HEALTH_MIN` (default 10) minutes: all services in `/capabilities` healthy and the
   connector connected (when `COORDINATOR_URL` is set).
8. Success: `./prune.sh`, then the updater replaces itself (step below). Failure: restore `.updater/prev/`,
   `up -d` the previous tag, report `ok: false` with the reason; that version is not retried until the
   coordinator asks for a different one or sends `now: true`.

Self-replacement: a container cannot recreate itself through compose (the client dies with it), so the updater starts
a one-shot `docker run --rm` of the *new* updater image with the socket, which runs `docker compose up -d --no-deps
updater` and exits.

How the updater runs compose like the owner does: it reads its own container labels
(`com.docker.compose.project`, `…project.working_dir`, `…project.config_files`, `…project.environment_file`) through
the socket and reuses them. The node folder is mounted at the same absolute path inside the updater, so relative
paths (`./Caddyfile`) resolve the same way. Works on Linux, WSL and Docker Desktop.

The updater also removes `NODE_JOIN_CODE` from `.env` once the connector is enrolled and connected.

## Draining before an update (since 0.16.5)

Once the update's images are local, the updater does not wait for a quiet moment any more: it sets
`state: "draining"` (in `/capabilities.updater` with `draining_since` and `drain_timeout_s`, and in every node ping as
`updater: {state, seq}` with the same `seq`). The coordinator then gives the node no new tasks; the connector
refuses a new request with the `res_error` code `draining` (re-send it elsewhere, no penalty). Running work — coordinator
streams and TTS jobs — finishes; diarization sessions are not waited for (the coordinator moves them). When nothing
runs, or after `drain_timeout_s` (`UPDATER_DRAIN_S`, 600 s), the update is applied (~1 min) and the state goes back to
`idle`. Pause and a withdrawn wish end the draining. Updating the nodes one by one is the coordinator's part: the next
node gets its `update.version` once the previous one reports `idle`, `last_update.ok` for that version and is healthy.

## Opus-MT models version (since 0.14.1)

The signed manifest's `opus_models_tag` decides the translation models version. On every update the updater removes a
lower `OPUS_MODELS_TAG=` pin from `.env` (old installers wrote `1`, which silently kept nodes on the old models) and
runs `init-opus` with the manifest's value. On a node that already runs the release but has older models (updated by
updater ≤ 0.1.2 with such a pin) it reports `warnings: ["opus models 1 on the node, release v0.14.0 has 2"]` and fixes
it in the next idle window (pull the models image by digest, copy, restart translate, prune). Installers must not
write `OPUS_MODELS_TAG` into `.env`.

## Reported in /capabilities (node-info reads `status.json`, read-only)

```json
"updater": {"version": "0.1.0", "mode": "auto", "state": "idle|downloading|waiting_window|updating|checking|rolled_back|error",
            "desired": "v0.13.0", "available": "v0.13.0",
            "last_update": {"from": "v0.12.3", "to": "v0.13.0", "ok": true, "at": "2026-…Z", "error": null},
            "warnings": []}
```
The connector also sends it with its periodic capabilities, so the coordinator sees progress and rollbacks.

## Rollout of the updater itself

`compose.yaml` of the release that introduces it gains the `updater` service and the `updater-ctl` volume (no new
files on the node: old `update.sh` keeps working). From then on nodes update themselves. Nodes with a custom layout
(several `-f` files, `.env` elsewhere) work through the labels above; a node can set `UPDATER_MODE=off`.

## Decided (2026-11-17)

- `paused` blocks updates and downloads; `now` does not override it.
- No separate status message: `capabilities.updater` + `ping.updater {state, seq}`.
- Staged rollout ("selected nodes first") is the coordinator's choice of which nodes get which `update.version`.
- No socket-filter container: the signature, sha256, digests and the absence of any input except the version are the
  security boundary.

## Custom layouts

The updater reuses the compose labels of its own container. Every file there (compose files, env file) must be
visible inside it: the node folder is mounted (`.:/node`); files elsewhere (an override, an env file in another
folder) need an extra bind mount at the same path in the override file, or `UPDATER_MODE=off`.
