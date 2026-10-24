#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  if ! command -v pacman >/dev/null 2>&1; then
    printf 'This installer requires Arch Linux with pacman.\n' >&2
    exit 1
  fi
  sudo pacman -S --needed python
fi
exec python3 "$root/tools/install_arch.py" "$@"
