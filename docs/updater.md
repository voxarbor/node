# node-updater: automatic node updates (DESIGN, not implemented yet)

Status: proposal for review (2026-11-17). Protocol changes are additive to docs/connect.md (protocol 1) and to
`/capabilities` (schema 1).

## Pieces and trust

```
coordinator --(WebSocket, existing)--> connector --(file in volume updater-ctl)--> updater --(docker.sock)--> docker
                                                                                      |
                                                       GitHub release voxarbor/node <-+ (files + signed manifest)
                                                       ghcr.io/voxarbor/node-*@sha256 <-+ (images by digest)
```

- New service `updater` (image `ghcr.io/voxarbor/node-updater`, ~60 MB: python, docker CLI + compose plugin, cosign,
  all pinned). It is the only container with `/var/run/docker.sock`. No listening port, no coordinator connection.
- The connector never gets the socket. It only writes the coordinator's wish into the shared volume `updater-ctl`
  (`desired.json`) and the node's load (`busy.json`). The updater only reads those two and writes `status.json`.
- What the coordinator can say: "desired version vX.Y.Z" (+ mode, + "now"). Nothing else: no URLs, files, images
  or commands. Worst case of a hostile coordinator or connector: it asks for another *signed* release.
- Downgrades are refused unless `UPDATER_ALLOW_DOWNGRADE=1` in `.env` (rollback after a failed update is local and
  does not need it).

## Release manifest (CI)

A new workflow job on every `v*` tag (after images are built) attaches to the GitHub release:

| asset | content |
|---|---|
| `compose.yaml`, `compose.gpu.yaml`, `Caddyfile`, `prune.sh` | node files of that tag |
| `manifest.json` | `{version, opus_models_tag, files: {name: sha256}, images: {"node-tts": "ghcr.io/voxarbor/node-tts@sha256:…", …, "speaches-cpu": …, "speaches-cuda": …, "caddy": …}, min_updater}` |
| `manifest.json.sigstore.json` | cosign keyless bundle (GitHub OIDC, Rekor entry) |

The updater verifies the bundle with `cosign verify-blob --bundle … --certificate-oidc-issuer
https://token.actions.githubusercontent.com --certificate-identity
https://github.com/voxarbor/node/.github/workflows/images.yml@refs/tags/<tag>`, then every file by sha256 from the
manifest. Images are pulled by digest from the manifest and tagged locally as `:<tag>`; compose then runs with
`--pull never`, so nothing unverified is started. If cosign cannot reach Sigstore, the update waits (no fallback to
unsigned).

## Control channel (connector <-> updater, volume `updater-ctl`)

Additive to connect.md. Coordinator -> node, in `welcome` and in any later `caps_ack`/heartbeat reply:
```json
"update": {"version": "v0.13.0", "mode": "auto", "now": false, "paused": false}
```
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

All of: `busy.json` fresh (< 30 s) with 0 streams for 2 minutes; TTS `/health` busy 0 and jobs 0; not `paused`
(the owner took the machine back: no multi-GB downloads now). Downloads (images by digest) happen *before* the window,
while the node still works; the window is only needed for `up -d` (~1 min of downtime).

## Update run

1. Verify manifest and files (above). Pull images by digest, tag them.
2. Wait for the safe window.
3. Back up the current `compose*.yaml`, `Caddyfile`, `prune.sh` and `VOICE_CORE_TAG` to `.updater/prev/`.
4. Write the new files, set `VOICE_CORE_TAG=<tag>` in `.env` (only that line changes).
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

## Reported in /capabilities (node-info reads `status.json`, read-only)

```json
"updater": {"version": "0.1.0", "mode": "auto", "state": "idle|downloading|waiting_window|updating|checking|rolled_back|error",
            "desired": "v0.13.0", "available": "v0.13.0",
            "last_update": {"from": "v0.12.3", "to": "v0.13.0", "ok": true, "at": "2026-…Z", "error": null}}
```
The connector also sends it with its periodic capabilities, so the coordinator sees progress and rollbacks.

## Rollout of the updater itself

`compose.yaml` of the release that introduces it gains the `updater` service and the `updater-ctl` volume (no new
files on the node: old `update.sh` keeps working). From then on nodes update themselves. Nodes with a custom layout
(several `-f` files, `.env` elsewhere) work through the labels above; a node can set `UPDATER_MODE=off`.

## Open questions for the coordinator side (Opus)

- Should `paused` block updates (as proposed) or should a paused node be the preferred update target?
- Staged rollout ("selected nodes first"): only the coordinator decides which nodes get which `update.version`.
- Does the coordinator want a separate `update_status` message instead of reading `capabilities.updater`?
