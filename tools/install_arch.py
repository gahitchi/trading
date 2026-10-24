"""Install Trading Lab's local paper-trading launcher on Arch-family Linux.

Only system prerequisites and user desktop integration are installed. This tool does not
change trading policy, issue authority, or submit an order.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "trading-lab"


def arch_family() -> bool:
    try:
        distro = platform.freedesktop_os_release()
    except OSError:
        return False
    return "arch" in {distro.get("ID", "").lower(), *distro.get("ID_LIKE", "").lower().split()}


def command_ok(*args: str) -> bool:
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def ensure_system() -> None:
    if not arch_family() or not shutil.which("pacman"):
        raise RuntimeError("This installer supports Arch Linux and Arch-based distributions with pacman.")
    if os.geteuid() == 0:
        raise RuntimeError("Run this installer as your normal desktop user, not root or sudo.")
    if not shutil.which("sudo"):
        raise RuntimeError("sudo is required to install Docker and manage the docker service.")

    packages = []
    if not shutil.which("docker"):
        packages.append("docker")
    if not shutil.which("docker") or not command_ok("docker", "compose", "version"):
        packages.append("docker-compose")
    if not shutil.which("xdg-open"):
        packages.append("xdg-utils")
    if packages:
        print("Installing missing Arch packages:", ", ".join(packages), flush=True)
        subprocess.run(["sudo", "pacman", "-S", "--needed", *packages], check=True)
    if not command_ok("docker", "info", "--format", "{{.ServerVersion}}"):
        # The supported default is system Docker, not an unconfigured rootless context.
        subprocess.run(["sudo", "systemctl", "enable", "--now", "docker"], check=True)
    if command_ok("docker", "info", "--format", "{{.ServerVersion}}"):
        return

    # A fresh group membership is visible to 'sg docker' immediately, without a logout.
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user or user == "root":
        raise RuntimeError("Cannot identify the desktop user for Docker permissions.")
    print(
        "Docker access requires membership in the 'docker' group (this grants root-equivalent host access).\n"
        "The app can instead be configured manually with rootless Docker."
    )
    choice = input("Add this user to the docker group? [y/N] ").strip().lower()
    if choice not in {"y", "yes"}:
        raise RuntimeError("Docker access was not enabled. Nothing will be started.")
    subprocess.run(["sudo", "usermod", "-aG", "docker", user], check=True)
    if not shutil.which("sg") or not command_ok("sg", "docker", "-c", "docker info --format '{{.ServerVersion}}'"):
        raise RuntimeError(
            "Docker group membership was added, but is not usable in this session. "
            "Log out and log back in before opening Trading Lab."
        )
    print("Docker permission is ready through 'sg docker'; a logout is not required for this launcher.")


def desktop_exec(path: Path) -> str:
    """Freedesktop Exec quoting (not shell quoting); %% is literal percent."""
    value = str(path).replace("\\", "\\\\").replace('"', '\\"').replace("\x60", "\\\x60").replace("$", "\\$")
    return '"' + value.replace("%", "%%") + '"'


def install_shortcuts(root: Path = ROOT) -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share").expanduser()
    apps = data / "applications"
    icons = data / "icons" / "hicolor" / "scalable" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / "src/lab/ui/icons/icon.svg", icons / "trading-lab.svg")
    launcher = apps / (APP_ID + ".desktop")
    content = (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        "Name=Trading Lab\n"
        "GenericName=Paper trading control panel\n"
        "Comment=Local control of the Interactive Brokers paper trading lab\n"
        f"Exec=/usr/bin/bash {desktop_exec(root / 'start-app-arch.sh')}\n"
        "TryExec=/usr/bin/bash\n"
        "Icon=trading-lab\n"
        "Terminal=false\n"
        "Categories=Office;Finance;\n"
        "StartupNotify=true\n"
    )
    launcher.write_text(content)
    launcher.chmod(0o755)
    # A KDE desktop shortcut is optional. Do not create a Desktop directory or overwrite unrelated files.
    desktop = Path.home() / "Desktop"
    if shutil.which("xdg-user-dir"):
        found = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, check=False)
        if found.returncode == 0 and found.stdout.strip():
            desktop = Path(found.stdout.strip())
    if desktop.is_dir():
        target = desktop / "Trading Lab.desktop"
        if not target.exists() or target.read_text() == content:
            target.write_text(content)
            target.chmod(0o755)
        else:
            print(f"Existing desktop shortcut left untouched: {target}")
    return launcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up Trading Lab on Arch Linux (paper accounts only).")
    parser.add_argument("--no-launch", action="store_true", help="install launcher now; enter IBKR setup later")
    args = parser.parse_args(argv)
    if not (ROOT / "tools/start_app.py").is_file() or not (ROOT / "compose.app.yml").is_file():
        raise RuntimeError("Run from a complete trading-lab repository checkout.")
    ensure_system()
    launcher = install_shortcuts()
    print(f"Trading Lab application-menu entry installed: {launcher}")
    print("Paper accounts only. This installer never grants trading authority or submits an order.")
    if not args.no_launch:
        subprocess.run(["bash", str(ROOT / "start-app-arch.sh"), "--interactive"], cwd=ROOT, check=True)
    else:
        print("Open Trading Lab from the KDE application menu to finish paper-account setup.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Arch installer stopped: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
