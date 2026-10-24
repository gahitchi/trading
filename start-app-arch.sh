#!/usr/bin/env bash
# KDE/Arch launcher. Reuses the existing, paper-only guided app and IB Gateway stack.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"
interactive="${1:-}"

# Broker login is performed inside the GUI; no terminal is needed on first launch.

launch() {
  if docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
    python3 tools/start_app.py
  elif id -nG "$(id -un)" | tr ' ' '\n' | grep -qx docker; then
    # An on-disk group change may not yet be active in the KDE login session.
    # On minimal Arch installs 'sg' is not installed; it must stay optional.
    if command -v sg >/dev/null 2>&1; then
      sg docker -c 'exec python3 tools/start_app.py'
    else
      printf "Docker group access is not active in this login, and 'sg' is unavailable.\n" >&2
      printf 'Log out of KDE completely, log back in, verify: docker info\n' >&2
      printf 'Then reopen Trading Lab; your existing installation is preserved.\n' >&2
      return 1
    fi
  else
    printf 'No Docker access. Check: sudo systemctl status docker; id -nG; docker info\n' >&2
    printf 'If just added to the docker group, log out of KDE and back in.\n' >&2
    return 1
  fi
}

# Always retain failures, including interactive/terminal sessions. Keep three
# bounded recent logs, private to the desktop user, without touching broker data.
umask 077
state="${XDG_STATE_HOME:-$HOME/.local/state}/trading-lab"
mkdir -p -- "$state"
chmod 700 -- "$state"
log="$state/launcher.log"
if [[ -L "$log" ]]; then
  printf 'Refusing to follow a symbolic-link diagnostic file: %s\n' "$log" >&2
  exit 1
fi
if [[ -f "$log" && $(stat -c %s -- "$log") -ge 1000000 ]]; then
  rm -f -- "$log.3"
  [[ ! -e "$log.2" ]] || mv -f -- "$log.2" "$log.3"
  [[ ! -e "$log.1" ]] || mv -f -- "$log.1" "$log.2"
  mv -f -- "$log" "$log.1"
fi
touch -- "$log"
chmod 600 -- "$log"
printf '\n[%s] Trading Lab launch start\n' "$(date -u +%FT%TZ)" >>"$log"
if [[ -t 1 || "$interactive" == "--interactive" ]]; then
  # tee preserves visible prompts while retaining their diagnostic output.
  set +e
  launch 2>&1 | tee -a "$log"
  rc=${PIPESTATUS[0]}
  set -e
else
  set +e
  launch >>"$log" 2>&1
  rc=$?
  set -e
fi
printf '[%s] Launcher exit code: %s\n' "$(date -u +%FT%TZ)" "$rc" >>"$log"
if [[ "$rc" -ne 0 ]]; then
  printf 'Trading Lab failed to start. Diagnostic log: %s\n' "$log" >&2
  if [[ "$interactive" != "--interactive" && ! -t 1 ]]; then
    if command -v kdialog >/dev/null 2>&1; then
      kdialog --error "Trading Lab could not start. Review $log before sharing it."
    elif command -v notify-send >/dev/null 2>&1; then
      notify-send 'Trading Lab did not start' "Review $log for the reason."
    fi
  fi
fi
exit "$rc"
