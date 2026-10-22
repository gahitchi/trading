from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab import data
from lab.config import load_config

REPO = Path(__file__).resolve().parents[1]


def _write_lab(tmp: Path, universe: dict, warmup: str, train, test, holdout) -> Path:
    s = (REPO / "lab.toml").read_text()
    start, end = s.index("[universe]"), s.index("[periods]")
    uni = "[universe]\n" + "".join(f'{t} = "{v}"\n' for t, v in universe.items()) + "\n"
    s = s[:start] + uni + s[end:]
    s = s.replace('warmup_start = "2009-01-01"', f'warmup_start = "{warmup}"')
    s = s.replace('train = ["2010-01-01", "2018-12-31"]', f'train = ["{train[0]}", "{train[1]}"]')
    s = s.replace('test = ["2019-01-01", "2022-12-31"]', f'test = ["{test[0]}", "{test[1]}"]')
    s = s.replace('holdout = ["2023-01-01", ""]', f'holdout = ["{holdout}", ""]')
    s = s.replace("null_samples = 30", "null_samples = 6")
    p = tmp / "lab.toml"
    p.write_text(s)
    return p


@pytest.fixture
def goog_lab(tmp_path):
    """A lab configured for the real GOOG 2004-2013 sample."""
    p = _write_lab(tmp_path, {"GOOG": "XNAS"}, "2004-08-19", ("2005-07-01", "2009-12-31"),
                   ("2010-01-01", "2011-12-31"), "2012-01-01")
    cfg = load_config(p)
    data.save(cfg, "GOOG", data.sample_goog(), source="test", adjusted=False)
    return cfg


@pytest.fixture
def goog():
    return data.validate(data.sample_goog(), "GOOG")


def staircase(n=12, start="2024-01-02", o0=100.0, step=10.0):
    """Deterministic bars: open_i = o0 + step*i, close_i = open_i + 5."""
    idx = pd.bdate_range(start, periods=n)
    o = o0 + step * np.arange(n)
    c = o + 5
    return pd.DataFrame({"open": o, "high": c + 1, "low": o - 1, "close": c, "volume": 1e7}, index=idx)
