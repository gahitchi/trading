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
    # Newly-added group can be used without logging out; both paths run as the desktop user.
    sg docker -c 'exec python3 tools/start_app.py'
  else
    printf 'No Docker access. Run bash install-arch.sh, then open Trading Lab again.\n' >&2
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
