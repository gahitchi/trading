"""Settings from lab.toml: where recorded market data and run outputs live."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class LabConfig:
    root: Path
    data_dir: Path
    results_dir: Path

    @property
    def kill_path(self) -> Path:
        """The kill switch file: while it exists, strategies send no new orders (`lab kill`, `lab resume`)."""
        return self.results_dir / "KILL"


def find_config(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "lab.toml").is_file():
            return d / "lab.toml"
    raise ConfigError("lab.toml not found in this directory or any parent")


def load_config(path: str | Path | None = None) -> LabConfig:
    path = Path(path) if path else find_config()
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    paths = raw.get("paths", {})
    if not isinstance(paths, dict) or set(paths) - {"data", "results"}:
        raise ConfigError("[paths]: expected data and results only")
    if not all(isinstance(v, str) and v for v in paths.values()):
        raise ConfigError("[paths]: data and results must be non-empty path strings")
    root = path.parent.resolve()
    return LabConfig(root, root / paths.get("data", "data"), root / paths.get("results", "results"))
