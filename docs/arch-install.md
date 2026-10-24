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

Before Claude merges the installer to `main`, use this review branch instead:

```bash
git fetch origin chatgpt/arch-installer-20261024
git switch --track origin/chatgpt/arch-installer-20261024
bash install-arch.sh
```

The installer asks for permission before granting Docker group access. **The `docker` group is equivalent to root access**; decline and configure rootless Docker separately if you do not accept that permission. When you accept, the new launcher can use `sg docker` before you next log in. First-time dependencies and image builds require network access.

On the first launch, the existing guided startup asks for an **IBKR paper username**, **DU-prefixed paper account**, and a hidden paper password. It stores the password in `secrets/tws_password` (mode 0600), not in the Git repository or app form. Then it starts the local paper Gateway and displays Trading Lab in its own Chrome/Chromium/Edge window when available, otherwise in your default browser. Approve any separate IBKR two-factor prompt. Finally, save the trading plan and explicitly choose **Start paper trading** in the app.

If you do not have paper account credentials yet, install the launcher without opening the Gateway:

```bash
bash install-arch.sh --no-launch
```

Open **Trading Lab** in KDE's application menu when ready. A Konsole window opens automatically if credentials have not been configured. The app is reachable at `http://127.0.0.1:8765` only; the Gateway API and VNC are also published on loopback.

## Test, stop or troubleshoot

```bash
# See the services and their status
sg docker -c 'docker compose -f compose.app.yml ps'
# Stop the app and Gateway (does not automatically liquidate broker positions)
sg docker -c 'docker compose -f compose.app.yml down'
# Run the focused offline tests (requires pytest; no Docker or brokerage account)
python3 -m pytest -q tests/test_arch_install.py
```

For an already logged-in Docker user, omit the `sg docker -c` wrapper. The KDE launcher writes startup failures to `~/.local/state/trading-lab/launcher.log` (or `$XDG_STATE_HOME/trading-lab/launcher.log`); treat it as private. If the Docker service cannot start, check `sudo systemctl status docker` and `journalctl -u docker -b`. If an account has already placed paper orders or holds positions, do not treat container shutdown as liquidation: inspect broker positions and perform the documented stop/recovery procedure.

This remains **paper-account testing software**, not a qualified live-money trading release. Broker acceptance, research calibration and other release gates in `docs/release-criteria.json` remain open. The app currently exposes daily US-stock strategies through the simplified guided UI; FX/crypto, quick-speed selection, and advanced strategy-bank controls are not wired into this launcher.
