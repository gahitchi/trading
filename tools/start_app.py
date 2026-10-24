"""One-time local Docker setup, then open the app in its own window. Never submit an order."""

import getpass
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

URL = "http://127.0.0.1:8765"
# Browsers that can open a page as a standalone app window (no tabs or address bar), in order of preference.
APP_BROWSERS = [
    *(
        str(Path(base) / app)
        for app in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe")
        for base in filter(None, map(os.environ.get, ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")))
    ),
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "microsoft-edge",
    "google-chrome",
    "chromium",
    "chromium-browser",
]
# Desktop and Start menu shortcuts with the app icon. Paths arrive through the environment, never the script.
SHORTCUTS = r"""
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in 'Desktop', 'Programs') {
  $path = Join-Path ([Environment]::GetFolderPath($folder)) 'Trading Lab.lnk'
  if (Test-Path -LiteralPath $path) { continue }
  $link = $shell.CreateShortcut($path)
  $link.TargetPath = $env:TRADING_LAB_TARGET
  $link.WorkingDirectory = $env:TRADING_LAB_DIR
  $link.IconLocation = $env:TRADING_LAB_ICON
  $link.WindowStyle = 7
  $link.Description = 'Open Trading Lab (paper trading only)'
  $link.Save()
}
"""


def private_file(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(value)
        file.flush()
        os.fsync(file.fileno())


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Trading Lab
Comment=Local control for paper trading
Exec="{launcher}"
Icon={icon}
Terminal=true
Categories=Office;Finance;
"""


def install_shortcuts(root):
    """A Trading Lab icon, once: on Windows on the desktop and in the Start menu, on Linux in the application
    menu. A failure never blocks the app."""
    if sys.platform.startswith("linux"):
        entry = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications"
        path = entry / "trading-lab.desktop"
        if not path.exists():
            try:
                entry.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    DESKTOP_ENTRY.format(
                        launcher=root / "start-app.sh", icon=root / "src" / "lab" / "ui" / "icons" / "icon-512.png"
                    )
                )
            except OSError as exc:
                print(f"No menu entry ({exc}); start Trading Lab with {root / 'start-app.sh'}")
        return
    if sys.platform != "win32":
        return
    env = dict(os.environ)
    env["TRADING_LAB_TARGET"] = str(root / "start-app.cmd")
    env["TRADING_LAB_DIR"] = str(root)
    env["TRADING_LAB_ICON"] = str(root / "src" / "lab" / "ui" / "icons" / "trading-lab.ico")
    command = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", SHORTCUTS]
    subprocess.run(command, env=env, check=False)


DOCKER_DESKTOP = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe",
    Path("/Applications/Docker.app"),
]


def docker_running() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def ensure_docker(seconds=300):
    """Start Docker Desktop when its engine is not running (Windows and macOS), and wait for it. On Linux the
    engine is a system service: say how to start it."""
    if docker_running():
        return
    app = next((p for p in DOCKER_DESKTOP if p.exists()), None)
    if app is None:
        raise ValueError("Docker is not running. Start it (Linux: sudo systemctl start docker) and try again.")
    print("Starting Docker Desktop (the first start can take a few minutes; accept its terms if it asks)...")
    if app.suffix == ".app":
        subprocess.run(["open", "-a", str(app)], check=False)
    else:
        subprocess.Popen([str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(3)
        if docker_running():
            return
    raise ValueError(
        "Docker Desktop did not start. If it asked to install WSL or restart Windows, do that, then open "
        "Trading Lab again."
    )


def wait_until_ready(url, seconds=120):
    """The app container answers /health; opening the window earlier shows a connection error."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local address: never via a proxy
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with opener.open(url + "/health", timeout=3) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def open_app(url):
    """Open the app in its own window when Edge or Chrome is installed, otherwise in the default browser."""
    for candidate in APP_BROWSERS:
        exe = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if exe:
            subprocess.Popen(
                [exe, f"--app={url}", "--window-size=1280,900"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return
    webbrowser.open(url)


def main():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["docker", "compose", "version"], check=True, cwd=root, capture_output=True)
    ensure_docker()
    if not (root / ".env").exists():
        print("Connect IB Gateway in PAPER mode. Credentials are saved only on this computer.")
        username = input("IBKR username: ").strip()
        account = input("Paper account ID (DU...): ").strip().upper()
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}", username) or not re.fullmatch(r"DU\d{5,}", account):
            raise ValueError("Invalid username or paper account ID.")
        secret = root / "secrets" / "tws_password"
        if not secret.exists():
            password = getpass.getpass("IBKR password (hidden): ")
            if not password:
                raise ValueError("A Gateway password is required.")
            secret.parent.mkdir(mode=0o700, exist_ok=True)
            private_file(secret, password)
        private_file(root / ".env", f"TWS_USERID={username}\nLAB_PAPER_ACCOUNT={account}\n")
    elif not (root / "secrets" / "tws_password").is_file():
        raise ValueError("Existing .env found, but secrets/tws_password is missing. Complete Gateway setup first.")
    for name in ("results", "research", "data"):
        (root / name).mkdir(exist_ok=True)
    env = dict(os.environ)
    env["LAB_APP_UID"] = str(os.getuid()) if hasattr(os, "getuid") else "1000"
    env["LAB_APP_GID"] = str(os.getgid()) if hasattr(os, "getgid") else "1000"
    subprocess.run(["docker", "compose", "-f", "compose.app.yml", "up", "--build", "-d"], check=True, cwd=root, env=env)
    install_shortcuts(root)
    print("Complete any IBKR two-factor prompt. Next time, open Trading Lab from its icon.")
    print("Save your plan, then click Start paper trading. No orders are enabled by this installer.")
    if not wait_until_ready(URL):
        print(f"The app is not answering yet. Open {URL} once the container is healthy.")
    open_app(URL)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Setup stopped: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
