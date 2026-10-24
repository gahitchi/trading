"""Installer contract: Arch prerequisites, desktop integration, non-trading startup."""

import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def installer():
    path = Path(__file__).parents[1] / "tools" / "install_arch.py"
    spec = importlib.util.spec_from_file_location("install_arch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_arch_family_distributions(monkeypatch):
    m = installer()
    monkeypatch.setattr(m.platform, "freedesktop_os_release", lambda: {"ID": "arch"})
    assert m.arch_family()
    monkeypatch.setattr(m.platform, "freedesktop_os_release", lambda: {"ID": "endeavouros", "ID_LIKE": "arch"})
    assert m.arch_family()
    monkeypatch.setattr(m.platform, "freedesktop_os_release", lambda: {"ID": "debian"})
    assert not m.arch_family()


def test_shortcut_is_user_scoped_and_rerunnable(tmp_path, monkeypatch):
    m = installer()
    root = tmp_path / "my repository with spaces"
    icon = root / "src/lab/ui/icons/icon.svg"
    icon.parent.mkdir(parents=True)
    icon.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    (root / "start-app-arch.sh").write_text("#!/bin/bash\n")
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setattr(m.shutil, "which", lambda name: None)
    desktop_file = home / "Desktop/Trading Lab.desktop"
    desktop_file.write_text("unrelated desktop file")
    link = m.install_shortcuts(root)
    assert link.parent == home / ".local/share/applications"
    assert 'Exec=/usr/bin/bash "' + str(root / "start-app-arch.sh") + '"' in link.read_text()
    assert "Terminal=false" in link.read_text() and "trading-lab" in link.read_text()
    assert (link.stat().st_mode & 0o111) != 0
    assert desktop_file.read_text() == "unrelated desktop file"
    assert (home / ".local/share/icons/hicolor/scalable/apps/trading-lab.svg").read_text() == icon.read_text()
    before = link.read_bytes()
    assert m.install_shortcuts(root) == link
    assert link.read_bytes() == before


def test_desktop_escaping_does_not_break_exec():
    m = installer()
    assert m.desktop_exec(Path('/tmp/a % $ " b')) == '"/tmp/a %% \\$ \\" b"'


def test_installer_never_runs_engine_in_no_launch_mode(tmp_path, monkeypatch):
    m = installer()
    root = tmp_path
    (root / "tools").mkdir()
    (root / "tools/start_app.py").write_text("")
    (root / "compose.app.yml").write_text("")
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "ensure_system", lambda: None)
    monkeypatch.setattr(m, "install_shortcuts", lambda: root / "launcher.desktop")
    called = Mock(side_effect=AssertionError("No commands allowed"))
    monkeypatch.setattr(m.subprocess, "run", called)
    assert m.main(["--no-launch"]) == 0
    called.assert_not_called()


def test_installer_routes_first_launch_through_paper_only_wrapper(tmp_path, monkeypatch):
    m = installer()
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/start_app.py").write_text("")
    (tmp_path / "compose.app.yml").write_text("")
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "ensure_system", lambda: None)
    monkeypatch.setattr(m, "install_shortcuts", lambda: tmp_path / "launcher.desktop")
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(m.subprocess, "run", run)
    assert m.main([]) == 0
    run.assert_called_once_with(
        ["bash", str(tmp_path / "start-app-arch.sh"), "--interactive"], cwd=tmp_path, check=True
    )


def test_missing_docker_provisions_only_needed_packages_and_asks_about_root_equivalent_group(monkeypatch):
    m = installer()
    monkeypatch.setattr(m, "arch_family", lambda: True)
    monkeypatch.setattr(m.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setattr(m.shutil, "which", lambda name: None if name in {"docker", "xdg-open"} else "/bin/" + name)
    runs = []

    def fake_run(args, **kwargs):
        runs.append(args)
        success = list(args[:2]) in (["sg", "docker"], ["sudo", "docker"])
        return SimpleNamespace(returncode=0 if success else 1)

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    m.ensure_system()
    assert ["sudo", "pacman", "-S", "--needed", "docker", "docker-compose", "docker-buildx", "xdg-utils"] in runs
    assert ["sudo", "systemctl", "enable", "--now", "docker"] in runs
    assert ["sudo", "usermod", "-aG", "docker", "desktop-user"] in runs
    assert not any("paper" in repr(cmd) for cmd in runs)


def test_declining_docker_group_is_safe(monkeypatch):
    m = installer()
    monkeypatch.setattr(m, "arch_family", lambda: True)
    monkeypatch.setattr(m.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("USER", "user")
    monkeypatch.setattr(m.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(m, "command_ok", lambda *a: False)
    calls = []
    monkeypatch.setattr(m.subprocess, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(RuntimeError, match="not enabled"):
        m.ensure_system()
    assert not any("usermod" in a for a in calls)


def test_launcher_uses_existing_docker_socket_without_root(tmp_path):
    root = Path(__file__).parents[1]
    fake = tmp_path / "bin"
    fake.mkdir()
    outfile = tmp_path / "invoked"
    for name, body in {
        "docker": '#!/bin/sh\n[ "$1" = info ] && exit 0\nexit 1\n',
        "python3": '#!/bin/sh\nprintf "%s\\n" "$*" > "$TEST_OUTPUT"\n',
    }.items():
        path = fake / name
        path.write_text(body)
        path.chmod(0o755)
    env = {**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"], "TEST_OUTPUT": str(outfile)}
    result = subprocess.run(
        ["bash", str(root / "start-app-arch.sh"), "--interactive"], env=env, cwd=root, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert outfile.read_text().strip() == "tools/start_app.py"


def test_launcher_uses_sg_after_new_group_membership(tmp_path):
    root = Path(__file__).parents[1]
    fake = tmp_path / "bin"
    fake.mkdir()
    outfile = tmp_path / "invoked"
    scripts = {
        "docker": "#!/bin/sh\nexit 1\n",
        "id": '#!/bin/sh\nif [ "$1" = -un ]; then echo desktop-user; else echo "desktop-user docker"; fi\n',
        "sg": '#!/bin/sh\n[ "$1" = docker ] && [ "$2" = -c ] || exit 2\nexec sh -c "$3"\n',
        "python3": '#!/bin/sh\nprintf "%s\\n" "$*" > "$TEST_OUTPUT"\n',
    }
    for name, body in scripts.items():
        path = fake / name
        path.write_text(body)
        path.chmod(0o755)
    env = {**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"], "TEST_OUTPUT": str(outfile)}
    result = subprocess.run(
        ["bash", str(root / "start-app-arch.sh"), "--interactive"],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert outfile.read_text().strip() == "tools/start_app.py"


def test_first_gui_launch_opens_a_real_setup_terminal(tmp_path):
    launcher = Path(__file__).parents[1] / "start-app-arch.sh"
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    (fake_root / "start-app-arch.sh").write_text(launcher.read_text())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    konsole = fake_bin / "konsole"
    konsole.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$TEST_OUTPUT"\n')
    konsole.chmod(0o755)
    dest = tmp_path / "gui-command"
    env = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"], "TEST_OUTPUT": str(dest)}
    result = subprocess.run(
        ["bash", str(fake_root / "start-app-arch.sh")],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--hold -e bash" in dest.read_text()
    assert "--interactive" in dest.read_text()


def test_gui_launch_failure_creates_private_diagnostic_log(tmp_path):
    launcher = Path(__file__).parents[1] / "start-app-arch.sh"
    root = tmp_path / "repo"
    (root / "secrets").mkdir(parents=True)
    (root / "secrets/tws_password").write_text("not-a-real-password")
    (root / ".env").write_text("not-real")
    (root / "start-app-arch.sh").write_text(launcher.read_text())
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, source in {
        "docker": "#!/bin/sh\nexit 1\n",
        "id": '#!/bin/sh\nif [ "$1" = -un ]; then echo test; else echo test; fi\n',
    }.items():
        p = fake / name
        p.write_text(source)
        p.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(fake) + os.pathsep + os.environ["PATH"],
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    result = subprocess.run(
        ["bash", str(root / "start-app-arch.sh")],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    log = tmp_path / "state/trading-lab/launcher.log"
    assert "No Docker access" in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600


def test_interactive_launcher_retains_recent_failures_and_appends(tmp_path):
    """Both terminal and KDE launches must save failures without overwriting history."""
    launcher = Path(__file__).parents[1] / "start-app-arch.sh"
    root = tmp_path / "repo"
    (root / "secrets").mkdir(parents=True)
    (root / ".env").write_text("fake")
    (root / "secrets/tws_password").write_text("fake")
    (root / "start-app-arch.sh").write_text(launcher.read_text())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\necho simulated-docker-failure >&2\nexit 1\n")
    docker.chmod(0o755)
    ident = bin_dir / "id"
    ident.write_text('#!/bin/sh\nif [ "$1" = "-un" ]; then echo desktop-user; else echo desktop-user; fi\n')
    ident.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
    }
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(root / "start-app-arch.sh"), "--interactive"],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
    log = tmp_path / "state/trading-lab/launcher.log"
    body = log.read_text()
    assert body.count("No Docker access") == 2
    assert body.count("Launcher exit code: 1") == 2
    assert log.stat().st_mode & 0o777 == 0o600


def test_arch_launcher_rolls_large_log_and_refuses_symlink(tmp_path):
    launcher = Path(__file__).parents[1] / "start-app-arch.sh"
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("fake")
    (root / "secrets").mkdir()
    (root / "secrets/tws_password").write_text("fake")
    (root / "start-app-arch.sh").write_text(launcher.read_text())
    state = tmp_path / "state/trading-lab"
    state.mkdir(parents=True)
    log = state / "launcher.log"
    log.write_text("X" * 1_000_020)
    env = {**os.environ, "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    first = subprocess.run(
        ["bash", str(root / "start-app-arch.sh"), "--interactive"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert first.returncode != 0
    assert (state / "launcher.log.1").stat().st_size >= 1_000_020
    assert log.stat().st_size < 1_000_000
    log.unlink()
    log.symlink_to(tmp_path / "outside")
    second = subprocess.run(
        ["bash", str(root / "start-app-arch.sh"), "--interactive"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 1
    assert "Refusing to follow a symbolic-link" in second.stderr
    assert not (tmp_path / "outside").exists()


def test_existing_group_membership_is_not_added_twice_and_install_finishes(tmp_path, monkeypatch):
    """A stale KDE login should not trigger another usermod or prevent installing the icon."""
    m = installer()
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tools/start_app.py").write_text("")
    (root / "compose.app.yml").write_text("")
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "arch_family", lambda: True)
    monkeypatch.setattr(m.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Already a member: no prompt permitted"))
    monkeypatch.setattr(m, "install_shortcuts", lambda path=root: root / "Trading Lab.desktop")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["id", "-nG", "desktop-user"]:
            return SimpleNamespace(returncode=0, stdout="desktop-user docker")
        return SimpleNamespace(returncode=0 if args[:2] == ["sudo", "docker"] else 1, stdout="")

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert m.main([]) == 0
    assert not any(args[:2] == ["sudo", "usermod"] for args in calls)
    assert not any(args and args[0] == "bash" for args in calls)


def test_daemon_failure_is_not_mistaken_for_group_membership(monkeypatch):
    m = installer()
    monkeypatch.setattr(m, "arch_family", lambda: True)
    monkeypatch.setattr(m.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=1),
    )
    with pytest.raises(RuntimeError, match="daemon is not responding even with sudo"):
        m.ensure_system()
    assert ["sudo", "systemctl", "enable", "--now", "docker"] in calls
    assert ["sudo", "docker", "info", "--format", "{{.ServerVersion}}"] in calls
    assert not any("usermod" in args for args in calls)
