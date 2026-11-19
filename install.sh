#!/bin/sh
# VoxArbor Node installer for Linux and WSL — https://github.com/voxarbor/node (docs/install.md explains every step).
#
#   sh install.sh --coordinator https://<coordinator> --join <code> [--name NAME] [--services tts,stt,diarize,fetch]
#                 [--gpu auto|off] [--mode normal|gentle] [--dry-run] [--uninstall [--yes]] [--help]
#
# Trust: the release's SHA256SUMS and manifest.json are verified with cosign (signed by this repository's release
# workflow); the node files are checked against SHA256SUMS; every image is pulled by the digest the signed manifest
# names. Docker is installed only from Docker's own apt repository, its GPG key checked. No secrets in this file.
set -eu

COORD="__COORDINATOR__"   # a coordinator serving this file may fill this in; otherwise pass --coordinator
REPO="voxarbor/node"
DIR="${NODE_DIR:-$HOME/voxarbor-node}"
PORT="${GATEWAY_PORT:-8790}"
RELEASE="${NODE_RELEASE:-}"   # vX.Y.Z; empty = the newest release
COSIGN="ghcr.io/sigstore/cosign/cosign:v3.1.4@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d"
DOCKER_GPG_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"   # Docker's apt key, as published on docs.docker.com

die() { echo "ПОМИЛКА: $*" >&2; exit 1; }
say() { echo "==> $*"; }
usage() {
  cat <<'HELP'
Встановлення вузла VoxArbor Node (Linux або WSL) і підключення до координатора.

  sh install.sh --coordinator https://<координатор> --join <код> [параметри]

  --coordinator URL     адреса координатора (https://…)
  --join CODE           одноразовий код приєднання з сайту координатора («Мої вузли»)
  --name NAME           назва вузла (типово: ім'я цього комп'ютера)
  --services LIST       tts, stt, diarize (хоча б один) і за бажанням fetch; типово tts,stt,diarize.
                        Переклад працює завжди. fetch — завантаження з YouTube з IP цього ПК.
  --gpu auto|off        auto: відеокарта NVIDIA, якщо Docker її бачить (типово); off: лише процесор
  --mode normal|gentle  gentle — економний режим: одна задача за раз, половина потоків
  --dry-run             лише перевірити параметри, нічого не змінюючи
  --uninstall [--yes]   повністю видалити вузол: контейнери, томи з моделями, образи, теку ~/voxarbor-node
  --help                ця довідка

Змінні: GATEWAY_PORT (8790), NODE_DIR (~/voxarbor-node), NODE_RELEASE (vX.Y.Z).
HELP
}

# ---------- 1. parameters ----------
JOIN=""; NAME=""; WANT="tts,stt,diarize"; GPU_MODE="auto"; MODE="normal"; DRY=""; UNINSTALL=""; YES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --join) [ $# -ge 2 ] || die "--join: потрібен код"; JOIN="$2"; shift ;;
    --join=*) JOIN="${1#--join=}" ;;
    --coordinator) [ $# -ge 2 ] || die "--coordinator: потрібна адреса"; COORD="$2"; shift ;;
    --coordinator=*) COORD="${1#--coordinator=}" ;;
    --name) [ $# -ge 2 ] || die "--name: потрібна назва"; NAME="$2"; shift ;;
    --name=*) NAME="${1#--name=}" ;;
    --services) [ $# -ge 2 ] || die "--services: потрібен список"; WANT="$2"; shift ;;
    --services=*) WANT="${1#--services=}" ;;
    --gpu) [ $# -ge 2 ] || die "--gpu: auto або off"; GPU_MODE="$2"; shift ;;
    --gpu=*) GPU_MODE="${1#--gpu=}" ;;
    --mode) [ $# -ge 2 ] || die "--mode: normal або gentle"; MODE="$2"; shift ;;
    --mode=*) MODE="${1#--mode=}" ;;
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "невідомий параметр: $1 (--help)" ;;
  esac
  shift
done

SUDO=""; [ "$(id -u)" = 0 ] || { command -v sudo >/dev/null 2>&1 && SUDO="sudo"; }
docker_cmd() {  # prints "docker" or "sudo docker": whichever may talk to the Docker daemon
  if docker info >/dev/null 2>&1; then echo docker
  elif [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then echo "$SUDO docker"
  else return 1; fi
}

# ---------- uninstall: the whole node goes, nothing else on the PC is touched ----------
if [ -n "$UNINSTALL" ]; then
  DOCKER=$(docker_cmd) || die "docker не запущений або немає прав"
  say "видаляю вузол: контейнери й томи проєкту voxarbor-node, його образи і теку $DIR"
  if [ -z "$YES" ]; then
    printf 'Продовжити? Введіть yes: '; read -r answer </dev/tty || answer=""
    [ "$answer" = yes ] || die "скасовано"
  fi
  if [ -f "$DIR/compose.yaml" ]; then
    (cd "$DIR" && $DOCKER compose --env-file .env down -v --rmi all --remove-orphans </dev/null) || true
  fi
  case "$DIR" in /|"$HOME"|"") die "дивна тека вузла: $DIR, не видаляю" ;; esac
  $SUDO rm -rf "$DIR"
  say "готово. Docker лишився встановленим"
  exit 0
fi

SERVICES="translate"; OFF=""; FETCH=""; HEAVY=0
for s in $(echo "$WANT" | tr ',' ' '); do
  case "$s" in
    tts|stt|diarize|fetch) ;;
    translate) die "переклад працює завжди, у --services його вказувати не треба" ;;
    *) die "невідомий сервіс: $s (можна: tts, stt, diarize, fetch)" ;;
  esac
done
for s in tts stt diarize; do
  case ",$WANT," in *",$s,"*) SERVICES="$SERVICES,$s"; HEAVY=$((HEAVY + 1)) ;; *) OFF="$OFF $s" ;; esac
done
case ",$WANT," in *",fetch,"*) FETCH=1; SERVICES="$SERVICES,fetch" ;; esac
[ "$HEAVY" -gt 0 ] || die "потрібен хоча б один із tts, stt, diarize: вузол лише з перекладом координатору не потрібен"
case "$GPU_MODE" in auto|off) ;; *) die "--gpu: auto або off" ;; esac
case "$MODE" in normal|gentle) ;; *) die "--mode: normal або gentle" ;; esac
COORD="${COORD%/}"
case "$COORD" in https://*|http://*) ;; *) die "потрібна адреса координатора: --coordinator https://…" ;; esac
[ -n "$JOIN" ] || die "потрібен код приєднання: --join <код> (сайт координатора, «Мої вузли»)"
case "$JOIN" in *[!A-Za-z0-9_-]*) die "код приєднання: лише літери, цифри, - і _" ;; esac
NAME=$(printf '%s' "${NAME:-$(hostname 2>/dev/null || true)}" | tr -cd 'A-Za-z0-9._-' | cut -c1-40)
case "$PORT" in ''|*[!0-9]*) die "GATEWAY_PORT: потрібне число" ;; esac
case "$RELEASE" in ''|v[0-9]*.[0-9]*.[0-9]*) ;; *) die "NODE_RELEASE: версія виду v0.16.0" ;; esac
command -v curl >/dev/null 2>&1 || die "немає curl"
command -v sha256sum >/dev/null 2>&1 || die "немає sha256sum"
if [ -n "$DRY" ]; then
  say "сухий прогін: координатор $COORD, вузол «$NAME», сервіси $SERVICES, відеокарта $GPU_MODE, режим $MODE, тека $DIR"
  exit 0
fi

# ---------- 2. Docker: from Docker's official apt repository only, after checking its signing key ----------
if ! command -v docker >/dev/null 2>&1; then
  ID=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"'); CODENAME=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release | tr -d '"')
  case "$ID" in ubuntu|debian) ;; *) die "Docker не встановлено: поставте його за https://docs.docker.com/engine/install/ і повторіть" ;; esac
  say "встановлюю Docker з download.docker.com ($ID $CODENAME), спершу звіряю ключ репозиторію"
  $SUDO apt-get update -qq </dev/null && $SUDO apt-get install -y -qq ca-certificates curl gnupg </dev/null >/dev/null
  key=$(mktemp); curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o "$key"
  fpr=$(gpg --show-keys --with-colons "$key" | awk -F: '/^fpr/ {print $10; exit}')
  [ "$fpr" = "$DOCKER_GPG_FPR" ] || die "ключ репозиторію Docker не той ($fpr)"
  $SUDO install -m 0755 -d /etc/apt/keyrings && $SUDO gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg "$key" && rm -f "$key"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID $CODENAME stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -qq </dev/null
  $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin </dev/null >/dev/null
  $SUDO systemctl enable --now docker >/dev/null 2>&1 || $SUDO service docker start >/dev/null 2>&1 || true
fi
DOCKER=$(docker_cmd) || die "docker не запущений або немає прав (sudo, або користувач у групі docker)"
$DOCKER compose version >/dev/null 2>&1 || die "немає docker compose v2 (пакет docker-compose-plugin)"

# ---------- 3. GPU: used when Docker can reach an NVIDIA card (nvidia-container-toolkit) ----------
GPU=""
if [ "$GPU_MODE" = auto ]; then
  SMI=$(command -v nvidia-smi || true); [ -n "$SMI" ] || [ ! -x /usr/lib/wsl/lib/nvidia-smi ] || SMI=/usr/lib/wsl/lib/nvidia-smi
  if [ -n "$SMI" ] && $DOCKER run --rm --gpus all ubuntu:24.04 nvidia-smi -L </dev/null >/dev/null 2>&1; then
    GPU=1; say "відеокарта: $("$SMI" --query-gpu=name --format=csv,noheader | head -n 1) — розпізнавання, озвучення й мовці на ній"
  else say "відеокарта NVIDIA для Docker не знайдена — усе на процесорі"; fi
fi

# ---------- 4. release: check the signatures, then the files ----------
if [ -n "$RELEASE" ]; then BASE="https://github.com/$REPO/releases/download/$RELEASE"
else BASE="https://github.com/$REPO/releases/latest/download"; fi
mkdir -p "$DIR" && cd "$DIR"
say "завантажую реліз у $DIR і перевіряю підпис (cosign)"
for f in SHA256SUMS SHA256SUMS.sigstore.json manifest.json manifest.json.sigstore.json; do
  curl -fsSL "$BASE/$f" -o "$f" || die "не вдалося завантажити $f"
done
TAG=$(sed -n 's/^ *"version": *"\(v[0-9][0-9.]*\)".*/\1/p' manifest.json | head -n 1)
[ -n "$TAG" ] || die "manifest.json без версії"
for f in SHA256SUMS manifest.json; do   # signer must be this repository's release workflow at this very tag
  $DOCKER run --rm -v "$DIR:/w" -w /w "$COSIGN" verify-blob "$f" --bundle "$f.sigstore.json" \
    --certificate-identity "https://github.com/$REPO/.github/workflows/release.yml@refs/tags/$TAG" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com </dev/null >/dev/null 2>&1 \
    || die "$f: підпис релізу не підтверджено"
done
FILES="compose.yaml Caddyfile env.example prune.sh"; [ -z "$GPU" ] || FILES="$FILES compose.gpu.yaml"
for f in $FILES; do   # each file must match its line in the signed SHA256SUMS
  curl -fsSL "$BASE/$f" -o "$f" || die "не вдалося завантажити $f"
  grep -q "^$(sha256sum "$f" | cut -d' ' -f1)  $f\$" SHA256SUMS || die "$f: контрольна сума не збігається"
done
chmod 755 prune.sh

# ---------- 5. .env: settings of this node (mode 600, the token never leaves this PC) ----------
[ -f .env ] || cp env.example .env
N=$(nproc 2>/dev/null || echo 4); half=$((N / 2)); [ "$half" -ge 2 ] || half=2; quarter=$((N / 4)); [ "$quarter" -ge 1 ] || quarter=1
TOKEN=$(sed -n 's/^NODE_TOKEN=\([0-9a-f]\{32,\}\)$/\1/p' .env); [ -n "$TOKEN" ] || TOKEN=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
setenv() {  # KEY VALUE: replace or add one line
  grep -v "^$1=" .env > .env.new || true; [ -z "$2" ] || printf '%s=%s\n' "$1" "$2" >> .env.new; mv .env.new .env
}
setenv NODE_TOKEN "$TOKEN";              setenv GATEWAY_PORT "127.0.0.1:$PORT"   # local only: the node dials out
setenv COORDINATOR_URL "$COORD";         setenv NODE_JOIN_CODE "$JOIN";          setenv NODE_NAME "$NAME"
setenv VOXARBOR_NODE_TAG "$TAG";         setenv NODE_SERVICES "$SERVICES"
setenv STT_THREADS "$half";  setenv TTS_THREADS "$quarter";  setenv TRANSLATE_THREADS "$quarter";  setenv DIARIZE_THREADS "$quarter"
for s in STT TTS TRANSLATE DIARIZE; do setenv "${s}_CPUS" "$N"; done
setenv COMPOSE_PROFILES "${FETCH:+fetch}"
setenv NODE_MODE "$([ "$MODE" = normal ] || echo "$MODE")"
OFF_ALL=""; for s in $OFF; do OFF_ALL="$OFF_ALL $s"; [ "$s" != stt ] || OFF_ALL="$OFF_ALL init-stt"; done
if [ -n "$OFF" ]; then   # unchosen services: scale 0 in compose.override.yaml, their images are not even pulled
  { echo "services:"; for s in $OFF_ALL; do echo "  $s: { scale: 0, profiles: [off] }"; done; } > compose.override.yaml
else rm -f compose.override.yaml; fi
CF=""; [ -z "$GPU" ] || CF="compose.yaml:compose.gpu.yaml"; [ -z "$CF" ] || [ ! -f compose.override.yaml ] || CF="$CF:compose.override.yaml"
setenv COMPOSE_FILE "$CF"
chmod 600 .env
say "вузол «$NAME», реліз $TAG, сервіси: $SERVICES, ядер $N"

# ---------- 6. images: each one by the digest the signed manifest names ----------
say "завантажую образи за digest із підписаного маніфесту (перший раз кілька ГБ)"
for ref in $($DOCKER compose config --images </dev/null | sort -u) \
           $(sed -n 's/^ *"ref": *"\([^"]*\)".*/\1/p' manifest.json); do   # + the translation models image
  # the images map has "ref": "repo@sha256:…" on one line; the models image has its digest on the next line
  dig=$(grep -F -A1 "\"$ref\"" manifest.json | sed -n 's/.*"\([^"]*@sha256:[0-9a-f]\{64\}\)".*/\1/p' | head -n 1)
  [ -n "$dig" ] || die "образ $ref не описаний у підписаному маніфесті"
  $DOCKER pull -q "$dig" </dev/null >/dev/null && $DOCKER tag "$dig" "$ref"
done

# ---------- 7. start ----------
say "копіюю моделі перекладу в том (один раз) і запускаю вузол"
$DOCKER compose --profile init run --rm init-opus </dev/null
$DOCKER compose up -d --pull never </dev/null
DOCKER="$DOCKER" ./prune.sh </dev/null >/dev/null 2>&1 || true   # old images of this node, if any

# ---------- 8. join: the connector enrolls with the code by itself; then the code is deleted ----------
say "чекаю, доки вузол підключиться до $COORD"
i=0
until $DOCKER compose exec -T connector python -c "import urllib.request as u;print(u.urlopen('http://127.0.0.1:8000/health').read().decode())" \
      </dev/null 2>/dev/null | grep -q '"connected": true'; do
  i=$((i + 1)); [ $i -lt 60 ] || die "вузол не підключився (подробиці: cd $DIR && $DOCKER compose logs connector)"
  sleep 5
done
setenv NODE_JOIN_CODE ""   # one-time code: used up, the node now holds its own token
say "готово: вузол «$NAME» ($TAG) підключився до $COORD"
say "оновлюється сам; вимкнути: UPDATER_MODE=off у $DIR/.env. Зупинити: cd $DIR && $DOCKER compose stop. Видалити: sh install.sh --uninstall"
