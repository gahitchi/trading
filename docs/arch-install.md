# Arch Linux / KDE installer (paper trading only)

This is the local desktop installer. It is separate from the experimental Windows setup. It creates an application-menu entry and optional desktop shortcut, installs any missing Docker/Compose packages through `pacman`, enables the Docker service, and uses the existing guided paper-broker app. It does **not** place orders or activate a trading mandate.

## Install

Use a regular desktop account **without `sudo`**. Install Git first if needed: `sudo pacman -S --needed git`. Choose a permanent directory (moving the checkout later breaks the shortcut):

```bash
mkdir -p "$HOME/Applications"
git clone https://github.com/gahitchi/trading.git "$HOME/Applications/trading-lab"
cd "$HOME/Applications/trading-lab"
bash install-arch.sh
```

The installer asks for permission before granting Docker group access. **The `docker` group is equivalent to root access**; decline and configure rootless Docker separately if you do not accept that permission. On Arch, `sg` is an **optional** convenience and may not be installed. If group membership was just granted but Docker cannot be used from the current KDE session, the installer creates the shortcut and defers launch until a complete KDE logout and login. Check `docker info` without `sudo` after logging back in. The installer never silently runs the trading app as root. First-time dependencies and image builds require network access.

After installation, open **Trading Lab** from KDE's application menu (or run
`bash start-app-arch.sh --interactive`). The **complete local GUI opens immediately, even before IBKR approves your account**.
The Docker app runs independently of the Gateway, with paper trading disabled until setup.

When your IBKR paper account becomes available, use the *Interactive Brokers paper account* form at the top of
the GUI. Supply the IBKR username, **DU-prefixed paper account** and password. The password goes only to a
user-owned loopback helper on `127.0.0.1:8766`, never to the app's trading engine, and is stored locally
in `secrets/tws_password` (mode 0600), while `.env` stores only the username and account number.
The helper starts **only** the IBKR paper Gateway without restarting the GUI. Approve any separate
IBKR two-factor prompt, then save the trading plan and explicitly choose **Start paper trading**.
If the Gateway fails to start, select *Retry Gateway connection* in the GUI.

The installation can still be performed without launching the GUI:

```bash
bash install-arch.sh --no-launch
```

On subsequent launches, the existing GUI is reused; it is not rebuilt while it is running.
The GUI is reachable at `http://127.0.0.1:8765` and the credential helper at
`127.0.0.1:8766` only; neither is exposed on the network. The Gateway API and VNC,
once explicitly configured, are likewise published on loopback.


## Test, stop or troubleshoot

```bash
# See the services and their status
docker compose -f compose.app.yml ps
# Stop the app and Gateway (does not automatically liquidate broker positions)
docker compose -f compose.app.yml down
# Run the focused offline tests (requires pytest; no Docker or brokerage account)
python3 -m pytest -q tests/test_arch_install.py
```

These commands require working Docker access for your desktop user; after granting group membership, log out of KDE and back in before running them. The KDE launcher now appends to `~/.local/state/trading-lab/launcher.log` (or `$XDG_STATE_HOME/trading-lab/launcher.log`) **even when started from a terminal**. It rotates when the file reaches 1 MB, keeping three previous files. Python setup exceptions are in `~/.local/state/trading-lab/crash.log` (also rotated). UI/server crashes are in `results/app/app-crash.log` and strategy/engine output in `results/app/engine.log` under the repository if the container uses the default results path; if configured otherwise, check the selected results directory. The application records fatal monitor, HTTP and server exceptions without automatically uploading logs.

To inspect startup failures without exposing credentials in chat:

```bash
tail -n 100 "${XDG_STATE_HOME:-$HOME/.local/state}/trading-lab/launcher.log"
tail -n 100 "${XDG_STATE_HOME:-$HOME/.local/state}/trading-lab/crash.log"
# If the app container dies before it writes an app-crash log:
docker compose -f compose.app.yml logs --tail 100 lab
```

**Security:** all new local diagnostic files are private and rotating, but broker/library output may still include usernames, account details or tokens not recognized by automated masking. Review/redact any log **before** sending it to anyone. Never send `.env`, `secrets/`, raw broker captures, or a complete `results/` directory. If the Docker service cannot start, check `sudo systemctl status docker` and `journalctl -u docker -b`. If an account has already placed paper orders or holds positions, do not treat container shutdown as liquidation: inspect broker positions and perform the documented stop/recovery procedure.

This remains **paper-account testing software**, not a qualified live-money trading release. Broker acceptance, research calibration and other release gates in `docs/release-criteria.json` remain open. The app currently exposes daily US-stock strategies through the simplified guided UI; FX/crypto, quick-speed selection, and advanced strategy-bank controls are not wired into this launcher.
