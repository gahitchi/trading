#!/bin/sh
# Trading Lab: one-command install and update for Linux (paper trading only).
#
#   curl -fsSL https://raw.githubusercontent.com/gahitchi/trading/main/install.sh | sh
#
# Installs what is missing (Docker with Compose, Python) with your package manager, downloads Trading Lab into
# ~/.local/share/trading-lab, asks for your IBKR paper login once, starts it, and adds a Trading Lab entry to
# your application menu. Running it again updates the program and keeps your settings, credentials and history.
# It never places an order: trading starts only when you press Start in the app.
set -eu

REPO="gahitchi/trading"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/trading-lab"
PRIVATE=".env secrets results data research lab.toml" # never overwritten by an update

step() { printf '\n==> %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

need=""
have docker || need="docker"
have docker && ! docker compose version >/dev/null 2>&1 && need="$need compose"
have python3 || need="$need python"
have curl || need="$need curl"
if [ -n "$need" ]; then
  step "Installing:$need (sudo will ask for your password)"
  if have pacman; then
    sudo pacman -S --needed --noconfirm docker docker-compose python curl
  elif have apt-get; then
    sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 python3 curl
  elif have dnf; then
    sudo dnf install -y docker docker-compose python3 curl
  else
    echo "Install Docker (with Compose) and Python 3 with your package manager, then run this again." >&2
    exit 1
  fi
fi

# The Docker service, and permission to use it without sudo (takes effect for new logins; `sg` uses it now)
if ! docker info >/dev/null 2>&1; then
  step "Starting the Docker service"
  sudo systemctl enable --now docker
fi
DOCKER_GROUP=""
if ! docker info >/dev/null 2>&1; then
  sudo usermod -aG docker "$USER"
  DOCKER_GROUP="docker"
fi

step "Downloading Trading Lab into $DEST"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C "$tmp"
src="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d -name 'trading-*' | head -n 1)"
mkdir -p "$DEST"
for item in "$src"/* "$src"/.[!.]*; do
  [ -e "$item" ] || continue
  name="$(basename "$item")"
  case " $PRIVATE " in *" $name "*) [ -e "$DEST/$name" ] && continue ;; esac
  rm -rf "${DEST:?}/$name"
  mv "$item" "$DEST/$name"
done

step "Starting Trading Lab (the first start builds it: allow 5 to 15 minutes)"
cd "$DEST"
if [ -n "$DOCKER_GROUP" ]; then
  sg docker -c "python3 tools/start_app.py" </dev/tty
else
  python3 tools/start_app.py </dev/tty
fi
printf '\nDone. Next time, open Trading Lab from your application menu.\n'
