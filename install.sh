#!/bin/sh
# VoxArbor Node installer (Linux or WSL). Open source: https://github.com/voxarbor/node (install.sh, docs/install.md).
# Installs the node on this PC and joins it to a VoxArbor coordinator. Checked install (recommended):
#   curl -fsSLO https://github.com/voxarbor/node/releases/latest/download/install.sh
#   curl -fsSLO https://github.com/voxarbor/node/releases/latest/download/SHA256SUMS
#   sha256sum --ignore-missing -c SHA256SUMS
#   sh install.sh --coordinator https://<coordinator> --join <code>
# Options: --coordinator URL --join CODE [--name NAME] [--services tts,stt,diarize,fetch] [--gpu auto|off]
#          [--mode normal|gentle] [--dry-run] [--uninstall [--yes]] [--help]      (docs/install.md: every step)
# What it trusts: the release's SHA256SUMS and manifest.json are verified with cosign (keyless signature of this
# repository's release workflow) before anything is used; node files are checked against SHA256SUMS; every image
# (ours and the pinned third-party ones) is pulled by the digest from the signed manifest. Docker comes only from
# Docker's official apt repository with its GPG key checked. Nothing secret is in this file.
set -eu

COORD="__COORDINATOR__"  # a coordinator that serves this file may fill this in; otherwise --coordinator is required
REPO="voxarbor/node"
PORT="${GATEWAY_PORT:-8790}"
RELEASE="${NODE_RELEASE:-}"
HEAVY="tts stt diarize"          # at least one of these
OPT="fetch"                      # off unless chosen (compose profile)
# cosign (Sigstore), pinned by digest: verifies the release signatures
COSIGN="ghcr.io/sigstore/cosign/cosign:v3.1.4@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d"
OIDC="https://token.actions.githubusercontent.com"
DOCKER_GPG_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"  # Docker's apt repository key (docs.docker.com)
if [ -n "${NODE_DIR:-}" ]; then DIR="$NODE_DIR"
elif [ ! -d "$HOME/voxarbor-node" ] && [ -f "$HOME/voice-core-node/.env" ]; then DIR="$HOME/voice-core-node"  # an earlier install
else DIR="$HOME/voxarbor-node"; fi

die() { echo "ПОМИЛКА: $*" >&2; exit 1; }
say() { echo "==> $*"; }
usage() {
  cat <<'HELP'
Встановлення вузла VoxArbor Node (Linux або WSL) і підключення до координатора.

  sh install.sh --coordinator https://<координатор> --join <код> [параметри]

  --coordinator URL   адреса координатора (https://…)
  --join CODE         одноразовий код приєднання з сайту координатора («Мої вузли»)
  --name NAME         назва вузла (типово: ім'я цього комп'ютера)
  --services LIST     важкі сервіси через кому: tts,stt,diarize (хоча б один) і за бажанням fetch
                      (типово tts,stt,diarize). Переклад і службові сервіси працюють завжди.
                      fetch — завантаження з YouTube з IP цього ПК — лише якщо вказати.
  --gpu auto|off      auto: відеокарта NVIDIA, якщо Docker її бачить (типово); off: лише процесор
  --mode normal|gentle  gentle — економний режим: одна задача за раз, половина потоків
  --dry-run           лише перевірити параметри й комп'ютер, нічого не змінюючи
  --uninstall [--yes] зупинити й повністю видалити вузол: контейнери, томи з моделями, образи, теку
  --help              ця довідка

Змінні: GATEWAY_PORT (8790), NODE_DIR ($HOME/voxarbor-node), NODE_RELEASE (vX.Y.Z; типово найновіший реліз).
Подробиці: https://github.com/voxarbor/node/blob/main/docs/install.md
HELP
}

JOIN=""; NAME=""; WANT="tts,stt,diarize"; GPU_MODE="auto"; MODE="normal"; DRY=""; UNINSTALL=""; YES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --join) [ $# -ge 2 ] || die "--join: потрібен код з сайту координатора"; JOIN="$2"; shift ;;
    --join=*) JOIN="${1#--join=}" ;;
    --coordinator) [ $# -ge 2 ] || die "--coordinator: потрібна адреса координатора"; COORD="$2"; shift ;;
    --coordinator=*) COORD="${1#--coordinator=}" ;;
    --name) [ $# -ge 2 ] || die "--name: потрібна назва вузла"; NAME="$2"; shift ;;
    --name=*) NAME="${1#--name=}" ;;
    --services) [ $# -ge 2 ] || die "--services: потрібен список, напр. --services tts,stt"; WANT="$2"; shift ;;
    --services=*) WANT="${1#--services=}" ;;
    --gpu) [ $# -ge 2 ] || die "--gpu: auto або off"; GPU_MODE="$2"; shift ;;
    --gpu=*) GPU_MODE="${1#--gpu=}" ;;
    --mode) [ $# -ge 2 ] || die "--mode: normal або gentle"; MODE="$2"; shift ;;
    --mode=*) MODE="${1#--mode=}" ;;
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "невідомий параметр: $1 (--help)" ;;
    *) die "зайвий параметр: $1 (--help)" ;;
  esac
  shift
done

SUDO=""
[ "$(id -u)" = 0 ] || { command -v sudo >/dev/null 2>&1 && SUDO="sudo"; }
docker_cmd() {  # "docker" or "sudo docker", whichever can talk to the daemon
  if docker info >/dev/null 2>&1; then echo docker
  elif [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then echo "$SUDO docker"
  else return 1; fi
}

# ---------- uninstall: one command, everything of the node goes ----------
if [ -n "$UNINSTALL" ]; then
  DOCKER=$(docker_cmd) || die "docker не запущений або немає прав"
  say "буде видалено: вузол у $DIR (контейнери, томи з моделями, образи вузла) і саму теку"
  if [ -z "$YES" ]; then
    printf 'Продовжити? Введіть yes: '
    read -r answer </dev/tty || answer=""
    [ "$answer" = yes ] || die "скасовано"
  fi
  if [ -f "$DIR/compose.yaml" ]; then
    (cd "$DIR" && $DOCKER compose --env-file .env down -v --rmi all --remove-orphans </dev/null) || true
  fi
  for p in voxarbor-node voice-core-worker; do  # also a stack left by an older install
    $DOCKER compose -p "$p" down -v --remove-orphans </dev/null >/dev/null 2>&1 || true
  done
  case "$DIR" in /|"$HOME"|"") die "дивна тека вузла: $DIR, не видаляю" ;; esac
  $SUDO rm -rf "$DIR"
  say "вузол видалено. Docker лишився (видалити: sudo apt-get purge docker-ce docker-ce-cli containerd.io)"
  exit 0
fi

# ---------- parameters ----------
SERVICES="translate"; OFF=""; FETCH=""; NHEAVY=0
for s in $(echo "$WANT" | tr ',' ' '); do
  case " $HEAVY $OPT " in
    *" $s "*) ;;
    *) case "$s" in
         translate) die "переклад працює завжди, у --services його вказувати не треба" ;;
         *) die "невідомий сервіс: $s (можна: tts, stt, diarize, fetch)" ;;
       esac ;;
  esac
done
for s in $HEAVY; do
  case ",$WANT," in *",$s,"*) SERVICES="$SERVICES,$s"; NHEAVY=$((NHEAVY + 1)) ;; *) OFF="$OFF $s" ;; esac
done
case ",$WANT," in *",fetch,"*) FETCH=1; SERVICES="$SERVICES,fetch" ;; esac
[ "$NHEAVY" -gt 0 ] || die "потрібен хоча б один із сервісів tts, stt, diarize: лише з перекладом вузол координатору не потрібен"
case "$GPU_MODE" in auto|off) ;; *) die "--gpu: auto або off (не «$GPU_MODE»)" ;; esac
case "$MODE" in normal|gentle) ;; *) die "--mode: normal або gentle (не «$MODE»)" ;; esac
COORD="${COORD%/}"
case "$COORD" in
  __*) die "потрібна адреса координатора: --coordinator https://<координатор>" ;;
  http://*|https://*) ;;
  *) die "--coordinator: потрібна адреса виду https://хост" ;;
esac
[ -n "$JOIN" ] || die "потрібен код приєднання: --join <код> (сайт координатора, «Мої вузли»)"
case "$JOIN" in *[!A-Za-z0-9_-]*) die "код приєднання: лише літери, цифри, - і _" ;; esac
NAME=$(printf '%s' "${NAME:-$(hostname 2>/dev/null || true)}" | tr -cd 'A-Za-z0-9._-' | cut -c1-40)
case "$PORT" in ''|*[!0-9]*) die "GATEWAY_PORT: потрібне число" ;; esac
case "$RELEASE" in ''|v[0-9]*.[0-9]*.[0-9]*) ;; *) die "NODE_RELEASE: потрібна версія виду v0.15.0" ;; esac
command -v curl >/dev/null 2>&1 || die "немає curl"
command -v sha256sum >/dev/null 2>&1 || die "немає sha256sum (coreutils)"

if [ -n "$DRY" ]; then
  say "сухий прогін: нічого не змінюю"
  say "координатор: $COORD; вузол: «$NAME»; сервіси: $SERVICES; відеокарта: $GPU_MODE; режим: $MODE"
  say "тека вузла: $DIR; шлюз: 127.0.0.1:$PORT; реліз: ${RELEASE:-найновіший}"
  if command -v docker >/dev/null 2>&1; then say "docker: є ($(docker --version 2>/dev/null || echo '?'))"
  else say "docker: немає, буде встановлено з офіційного apt-репозиторію Docker (Ubuntu/Debian, потрібен sudo)"; fi
  exit 0
fi

# ---------- Docker: only from Docker's official apt repository, its key checked ----------
if ! command -v docker >/dev/null 2>&1; then
  [ "$(id -u)" = 0 ] || [ -n "$SUDO" ] || die "немає docker, а встановити його можна лише від root (або з sudo)"
  ID=$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | tr -d '"'); CODENAME=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release 2>/dev/null | tr -d '"')
  case "$ID" in ubuntu|debian) ;; *)
    die "Docker не встановлено. На $ID поставте його за інструкцією https://docs.docker.com/engine/install/ і запустіть цей скрипт знову" ;;
  esac
  [ -n "$CODENAME" ] || die "не вдалося визначити версію $ID: поставте Docker за https://docs.docker.com/engine/install/"
  say "встановлюю Docker з офіційного репозиторію download.docker.com ($ID $CODENAME)"
  $SUDO apt-get update -qq </dev/null
  $SUDO apt-get install -y -qq ca-certificates curl gnupg </dev/null >/dev/null
  t=$(mktemp)
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o "$t" || die "не вдалося завантажити ключ Docker"
  fpr=$(gpg --show-keys --with-colons "$t" 2>/dev/null | awk -F: '/^fpr/ {print $10; exit}')
  [ "$fpr" = "$DOCKER_GPG_FPR" ] || { rm -f "$t"; die "ключ репозиторію Docker не той ($fpr), зупиняюсь"; }
  $SUDO install -m 0755 -d /etc/apt/keyrings
  $SUDO gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg "$t"; rm -f "$t"
  $SUDO chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID $CODENAME stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -qq </dev/null
  $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin </dev/null >/dev/null
  $SUDO systemctl enable --now docker >/dev/null 2>&1 || $SUDO service docker start >/dev/null 2>&1 || true
fi
DOCKER=$(docker_cmd) || die "docker не запущений або немає прав (sudo, або користувач у групі docker)"
$DOCKER compose version >/dev/null 2>&1 || die "немає docker compose v2 (плагін docker-compose-plugin)"

# ---------- GPU: STT, TTS and diarization on an NVIDIA GPU when Docker can reach it (compose.gpu.yaml) ----------
GPU=""
if [ "$GPU_MODE" = off ]; then
  say "відеокарта: не використовується (--gpu off), розпізнавання, озвучення й мовці на процесорі"
else
  SMI=$(command -v nvidia-smi 2>/dev/null || true)
  [ -n "$SMI" ] || [ ! -x /usr/lib/wsl/lib/nvidia-smi ] || SMI=/usr/lib/wsl/lib/nvidia-smi  # WSL: not always on PATH
  GPU_NAME=""
  [ -z "$SMI" ] || GPU_NAME=$("$SMI" --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1 | tr -d '\r' || true)
  if [ -z "$GPU_NAME" ]; then
    say "відеокарта: не знайдено (немає NVIDIA або nvidia-smi), розпізнавання, озвучення й мовці на процесорі"
  elif $DOCKER run --rm --gpus all ubuntu:24.04 nvidia-smi -L </dev/null >/dev/null 2>&1; then
    GPU=1
    say "відеокарта: $GPU_NAME, розпізнавання, озвучення й мовці на відеокарті"
  else
    say "відеокарта: $GPU_NAME є, але Docker її не бачить (потрібен nvidia-container-toolkit), усе на процесорі"
  fi
fi

# ---------- release: signatures first, then files by SHA256SUMS ----------
if [ -n "$RELEASE" ]; then BASE="https://github.com/$REPO/releases/download/$RELEASE"
else BASE="https://github.com/$REPO/releases/latest/download"; fi
say "файли вузла в $DIR (реліз ${RELEASE:-найновіший} з github.com/$REPO)"
mkdir -p "$DIR"
cd "$DIR"
for f in SHA256SUMS SHA256SUMS.sigstore.json manifest.json manifest.json.sigstore.json; do
  curl -fsSL "$BASE/$f" -o "$f.new" || die "не вдалося завантажити $f з $BASE"
done
# the tag this release is: from the signed manifest (checked right below)
TAG=$(sed -n 's/^ *"version": *"\(v[0-9][0-9.]*\)".*/\1/p' manifest.json.new | head -n 1)
[ -n "$TAG" ] || die "manifest.json без версії"
IDENTITY="https://github.com/$REPO/.github/workflows/release.yml@refs/tags/$TAG"
for f in SHA256SUMS manifest.json; do
  $DOCKER run --rm -v "$DIR:/w" -w /w "$COSIGN" verify-blob "$f.new" --bundle "$f.sigstore.json.new" \
    --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$OIDC" </dev/null >/dev/null 2>&1 \
    || die "$f: підпис релізу не підтверджено (cosign), зупиняюсь"
done
say "підпис релізу $TAG перевірено (cosign, Sigstore)"
FILES="compose.yaml Caddyfile env.example prune.sh"
[ -z "$GPU" ] || FILES="$FILES compose.gpu.yaml"
for f in $FILES; do
  curl -fsSL "$BASE/$f" -o "$f.new" || die "не вдалося завантажити $f з $BASE"
  want=$(awk -v f="$f" '$2 == f || $2 == "*"f {print $1}' SHA256SUMS.new)
  have=$(sha256sum "$f.new" | cut -d' ' -f1)
  [ -n "$want" ] && [ "$want" = "$have" ] || die "$f: контрольна сума не збігається з SHA256SUMS релізу"
done
for f in $FILES SHA256SUMS SHA256SUMS.sigstore.json manifest.json manifest.json.sigstore.json; do mv "$f.new" "$f"; done
chmod 755 prune.sh
[ -n "$GPU" ] || rm -f compose.gpu.yaml
mv env.example .env.example
[ -f .env ] || cp .env.example .env
sed -i -e '/^OPUS_MODELS_TAG=/d' -e '/^VOICE_CORE_TAG=/d' .env  # models version: the release's; old tag variable goes
line=$(grep -m1 '^REGISTRY=' .env.example || true)  # the release's registry, not an old pin
if [ -n "$line" ]; then
  grep -v '^REGISTRY=' .env > .env.new || true; printf '%s\n' "$line" >> .env.new; mv .env.new .env
fi

# threads sized to this PC: STT gets half the cores, the others a quarter each; every service may use all cores
N=$(nproc 2>/dev/null || echo 4)
half=$((N / 2)); [ "$half" -ge 2 ] || half=2
quarter=$((N / 4)); [ "$quarter" -ge 1 ] || quarter=1
# the gateway's own token stays on this PC (the connector uses it); an earlier one is kept, else a random one
TOKEN=$(sed -n 's/^NODE_TOKEN=\([0-9a-f]\{32,\}\)$/\1/p' .env | head -n 1)
[ -n "$TOKEN" ] || TOKEN=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
BIND="127.0.0.1:$PORT"  # nobody calls this node from outside: it dials out to the coordinator
grep -v -e '^COORDINATOR_URL=' -e '^NODE_JOIN_CODE=' -e '^NODE_NAME=' -e '^VOXARBOR_NODE_TAG=' .env > .env.new || true
printf 'COORDINATOR_URL=%s\nNODE_JOIN_CODE=%s\nNODE_NAME=%s\nVOXARBOR_NODE_TAG=%s\n' "$COORD" "$JOIN" "$NAME" "$TAG" >> .env.new
mv .env.new .env
sed -i -e "s/^NODE_TOKEN=.*/NODE_TOKEN=$TOKEN/" -e "s/^GATEWAY_PORT=.*/GATEWAY_PORT=$BIND/" \
  -e "s/^STT_THREADS=.*/STT_THREADS=$half/" -e "s/^TTS_THREADS=.*/TTS_THREADS=$quarter/" \
  -e "s/^TRANSLATE_THREADS=.*/TRANSLATE_THREADS=$quarter/" -e "s/^DIARIZE_THREADS=.*/DIARIZE_THREADS=$quarter/" \
  -e "s/^\(STT\|TTS\|TRANSLATE\|DIARIZE\)_CPUS=.*/\1_CPUS=$N/" .env
if grep -q '^NODE_SERVICES=' .env; then sed -i "s/^NODE_SERVICES=.*/NODE_SERVICES=$SERVICES/" .env
else printf '# services this node runs (written by install.sh; compose.override.yaml turns the rest off)\nNODE_SERVICES=%s\n' "$SERVICES" >> .env; fi
sed -i '/^COMPOSE_PROFILES=/d' .env  # fetch (YouTube from this PC's IP) runs only with its profile
[ -z "$FETCH" ] || echo "COMPOSE_PROFILES=fetch" >> .env
sed -i '/^NODE_MODE=/d' .env  # normal is the node's default
[ "$MODE" = normal ] || echo "NODE_MODE=$MODE" >> .env
chmod 600 .env
say "реліз $TAG; ядер: $N (потоки: розпізнавання $half, решта по $quarter)$([ "$MODE" = normal ] || echo ', економний режим: одна задача за раз, половина потоків')"

# ---------- services: the unselected heavy ones are off (scale: 0) in compose.override.yaml ----------
say "сервіси: $SERVICES"
OFF_ALL=""  # init-stt needs stt: they go together
for s in $OFF; do
  case "$s" in stt) OFF_ALL="$OFF_ALL stt init-stt" ;; *) OFF_ALL="$OFF_ALL $s" ;; esac
done
if [ -n "$OFF" ]; then
  {
    echo "# Generated by install.sh: this node runs only $SERVICES (gateway, node-info, connector and updater always run)."
    echo "# docker compose loads this file next to compose.yaml; the inactive profile keeps the other images from being"
    echo "# pulled. To change: run install.sh again with --services."
    echo "services:"
    for s in $OFF_ALL; do echo "  $s: { scale: 0, profiles: [voxarbor-off] }"; done
  } > compose.override.yaml.new
  mv compose.override.yaml.new compose.override.yaml
else
  rm -f compose.override.yaml
fi
# GPU: compose.gpu.yaml for every later `docker compose` here; COMPOSE_FILE replaces the automatic override, so it
# is listed explicitly (last: its scale: 0 wins)
sed -i '/^COMPOSE_FILE=/d' .env
if [ -n "$GPU" ]; then
  CF="compose.yaml:compose.gpu.yaml"; [ ! -f compose.override.yaml ] || CF="$CF:compose.override.yaml"
  echo "COMPOSE_FILE=$CF" >> .env
fi

# ---------- images: every one by the digest the signed manifest names, then tagged for compose ----------
say "образи: за digest із підписаного маніфесту (перший раз кілька ГБ, це надовго)"
digest_of() {  # "ref" -> "repo@sha256:…" from manifest.json (images or opus_image)
  sed -n "s#^ *\"$(printf '%s' "$1" | sed 's/[.[\*^$/]/\\&/g')\": *\"\\([^\"]*@sha256:[0-9a-f]\\{64\\}\\)\".*#\\1#p" manifest.json | head -n 1
}
REFS=$($DOCKER compose --env-file .env config --images </dev/null | sort -u)
case " $OFF " in *" translate "*) ;; *)
  OPUS_REF=$(sed -n 's/^ *"ref": *"\([^"]*node-models-opus[^"]*\)".*/\1/p' manifest.json | head -n 1)
  OPUS_DIG=$(sed -n 's/^ *"digest_ref": *"\([^"]*@sha256:[0-9a-f]\{64\}\)".*/\1/p' manifest.json | head -n 1) ;;
esac
for ref in $REFS; do
  dig=$(digest_of "$ref")
  [ -n "$dig" ] || die "образ $ref не описаний у підписаному маніфесті релізу $TAG (змінений REGISTRY?)"
  $DOCKER pull -q "$dig" </dev/null >/dev/null || die "не вдалося завантажити $dig"
  $DOCKER tag "$dig" "$ref"
done

# ---------- start ----------
EXISTED=$($DOCKER compose --env-file .env ps -a -q gateway </dev/null 2>/dev/null || true)
STOP="$OFF_ALL"; [ -n "$FETCH" ] || STOP="$STOP fetch"
# shellcheck disable=SC2086  # a re-install with fewer services: stop what was running before
[ -z "$STOP" ] || $DOCKER compose --env-file .env rm -sf $STOP </dev/null >/dev/null 2>&1 || true
# shellcheck disable=SC2016  # the ${…} is compose's text, matched literally
want=$(sed -n 's/^ *OPUS_MODELS_TAG: ${OPUS_MODELS_TAG:-\([^}]*\)}.*/\1/p' compose.yaml | head -n 1)
have=$($DOCKER compose --env-file .env run --rm --no-deps --entrypoint cat translate /cache/opus-mt/.models-version </dev/null 2>/dev/null || true)
if [ "$have" != "$want" ]; then  # the Opus-MT models: copied into the translate volume once per version
  if [ -n "${OPUS_DIG:-}" ] && [ -n "${OPUS_REF:-}" ]; then
    $DOCKER pull -q "$OPUS_DIG" </dev/null >/dev/null && $DOCKER tag "$OPUS_DIG" "$OPUS_REF"
  fi
  say "моделі перекладу (Opus-MT, версія $want): копіюю в том перекладу, один раз"
  $DOCKER compose --env-file .env --profile init run --rm init-opus </dev/null
fi
$DOCKER compose --env-file .env up -d --pull never </dev/null
# the Caddyfile is mounted, not built in: an existing gateway keeps the old routes until recreated
[ -z "$EXISTED" ] || $DOCKER compose --env-file .env up -d --pull never --force-recreate gateway </dev/null
DOCKER="$DOCKER" ./prune.sh </dev/null || say "старі образи вузла не прибрано (./prune.sh): вузол працює, місце звільниться згодом"

say "чекаю, доки шлюз відповість на :$PORT"
i=0
until curl -fs -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null; do
  i=$((i + 1)); [ $i -lt 120 ] || die "шлюз не відповідає: $DOCKER compose logs gateway"
  sleep 5
done

# ---------- join: the connector enrolls with the code and connects by itself ----------
say "чекаю, доки вузол підключиться до $COORD"
ASK="import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
i=0
while :; do
  state=$($DOCKER compose --env-file .env exec -T connector python -c "$ASK" </dev/null 2>/dev/null || true)
  case "$state" in *'"connected": true'*) break ;; esac
  i=$((i + 1)); [ $i -lt 36 ] || die "вузол не підключився: $state (докладніше: $DOCKER compose logs connector)"
  sleep 5
done
sed -i '/^NODE_JOIN_CODE=/d' .env  # one-time: used up, the node now has its own token (volume connector-state)
say "готово: вузол «$NAME» ($TAG) підключився до $COORD"
say "далі вузол оновлюється сам (сервіс updater, лише з підписаних релізів). Вимкнути: UPDATER_MODE=notify або off у $DIR/.env"
say "зупинити: cd $DIR && $DOCKER compose stop   ·   видалити повністю: sh install.sh --uninstall"
