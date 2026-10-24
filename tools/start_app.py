"""One-time local Docker setup, then open the app in its own window. Never submit an order."""

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

URL = "http://127.0.0.1:8765"
SETUP_PROTOCOL_VERSION = 2
SETUP_PORT = 8766
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


def install_shortcuts(root):
    """Windows: a Trading Lab icon on the desktop and in the Start menu, once. A failure never blocks the app.
    (Arch Linux and KDE: tools/install_arch.py installs the menu entry.)"""
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
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", "--window-size=1280,900"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except OSError:
                continue  # a broken install: try the next browser (ChatGPT G014-F4)
    webbrowser.open(url)


def running_app(root):
    """Reopening the window must not rebuild or recreate a running paper app."""
    result = subprocess.run(
        ["docker", "compose", "-f", "compose.app.yml", "ps", "--status", "running", "--services"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return "lab" in result.stdout.splitlines()


def diagnostic_log() -> Path:
    """Keep startup exceptions outside install files, in user-only rotating logs."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        directory = base / "TradingLab" / "logs"
    else:
        directory = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "trading-lab"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "crash.log"
    if path.is_symlink():
        raise OSError("Refusing to write diagnostics to a symbolic link")
    path.touch(mode=0o600, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o600)
    return path


def record_launch_failure(exc: Exception) -> Path:
    """Capture a bounded traceback without source lines, credentials or environment dumps."""
    path = diagnostic_log()
    line = str(exc)
    line = re.sub(r"\b(?:IB-)?DUR?[0-9]{5,}\b", "[PAPER_ACCOUNT]", line, flags=re.IGNORECASE)
    line = re.sub(
        r"(?i)(password|token|api[_-]?key|secret)\s*[=:]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        line,
    )
    frames = traceback.extract_tb(exc.__traceback__)
    details = "\n".join(f"  {f.filename}:{f.lineno} in {f.name}" for f in frames[-20:])
    entry = f"{datetime.now(UTC).isoformat()} | {type(exc).__name__}: {line}\n{details}"
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    try:
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.emit(logging.LogRecord("launcher", logging.ERROR, "", 0, entry, (), None))
    finally:
        handler.close()
    return path


def owned_setup_processes(root: Path) -> list[int]:
    """Find only our own exact helper argv under Linux; never kill by substring/pkill.

    Older launcher versions started the helper detached without a PID file.
    /proc identifies it without accidentally matching another user's process.
    """
    if sys.platform != "linux":
        return []
    script = (root / "tools" / "broker_setup.py").resolve()
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = (entry / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            if (
                len(argv) == 4
                and Path(os.fsdecode(argv[1])).resolve() == script
                and argv[2] == b"--root"
                and Path(os.fsdecode(argv[3])).resolve() == root.resolve()
            ):
                result.append(int(entry.name))
        except (OSError, ValueError):
            continue
    return result


def start_setup_service(root):
    """Keep localhost helper upgraded without touching the engine or broker Gateway."""
    url = f"http://127.0.0.1:{SETUP_PORT}/status"
    expected = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def status():
        try:
            with opener.open(urllib.request.Request(url, headers={"Origin": URL}), timeout=1) as response:
                if response.status != 200:
                    return None
                return json.load(response)
        except (OSError, ValueError):
            return None

    state = status()
    if state is not None:
        if state.get("installation") != expected:
            raise ValueError("A different Trading Lab installation is using local setup port 8766.")
        if state.get("protocol_version") == SETUP_PROTOCOL_VERSION:
            return
        # An old helper cannot validate Free Trial IDs and must be replaced.
        matches = owned_setup_processes(root)
        if len(matches) != 1:
            raise ValueError(
                "Outdated broker setup service on port 8766. Its owner could not be verified safely. "
                "Restart your desktop login session, then open Trading Lab again."
            )
        os.kill(matches[0], signal.SIGTERM)
        for _ in range(30):
            if status() is None:
                break
            time.sleep(0.1)
        else:
            raise ValueError("Old broker helper did not release port 8766; restart the desktop login session.")

    process_group = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    )
    subprocess.Popen(
        [sys.executable, str(root / "tools" / "broker_setup.py"), "--root", str(root)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **process_group,
    )
    for _ in range(40):
        fresh = status()
        if fresh is not None:
            if fresh.get("installation") != expected or fresh.get("protocol_version") != SETUP_PROTOCOL_VERSION:
                raise ValueError("Port 8766 is served by a different or outdated broker setup helper.")
            return
        time.sleep(0.1)
    raise ValueError("The upgraded broker setup helper did not start. Verify port 8766 is available.")


def main():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["docker", "compose", "version"], check=True, cwd=root, capture_output=True)
    ensure_docker()
    for name in ("results", "research", "data"):
        (root / name).mkdir(exist_ok=True)
    env = dict(os.environ)
    env["LAB_APP_UID"] = str(os.getuid()) if hasattr(os, "getuid") else "1000"
    env["LAB_APP_GID"] = str(os.getgid()) if hasattr(os, "getgid") else "1000"
    if running_app(root):
        print("Trading Lab is already running; opening the existing window without rebuilding the engine.")
    else:
        subprocess.run(
            ["docker", "compose", "-f", "compose.app.yml", "up", "--build", "-d", "lab"], check=True, cwd=root, env=env
        )
    install_shortcuts(root)
    start_setup_service(root)
    print("Trading Lab is ready without IBKR. Connect a paper account later in the GUI.")
    print("No orders are enabled by the installer.")
    if not wait_until_ready(URL):
        print(f"The app is not answering yet. Open {URL} once the container is healthy.")
    open_app(URL)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        try:
            path = record_launch_failure(exc)
            print(f"Setup stopped. Crash details: {path}", file=sys.stderr)
        except OSError:
            print(f"Setup stopped: {type(exc).__name__} (diagnostic log unavailable)", file=sys.stderr)
        raise SystemExit(1) from exc
