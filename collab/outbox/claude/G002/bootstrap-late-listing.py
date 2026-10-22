"""Evidence for G002-F2. Run on the hardening branch: PYTHONPATH=src python bootstrap-late-listing.py

One stock that lists late (last 400 of 2148 sessions) is added to a panel with GOOG. With the union-calendar
bootstrap, GOOG's null over that window is resampled only from LATE's lifetime, so GOOG's null shrinks.
At 63a3140: alone vol mean 0.337 sd 0.033 | with LATE mean 0.248 sd 0.019 (true full-history vol 0.342).
"""

import numpy as np

from lab import data

goog = data.validate(data.sample_goog(), "GOOG")
late = goog.iloc[-400:].copy()


def ann_vol(df):
    r = np.log(df.close).diff().dropna()
    return r.std() * np.sqrt(252)


alone = [ann_vol(data.block_bootstrap_panel({"GOOG": goog}, s)["GOOG"].iloc[-400:]) for s in range(40)]
panel = [ann_vol(data.block_bootstrap_panel({"GOOG": goog, "LATE": late}, s)["GOOG"].iloc[-400:]) for s in range(40)]
print(f"GOOG null vol, last 400 sessions: alone mean {np.mean(alone):.3f} sd {np.std(alone):.3f} | "
      f"with LATE mean {np.mean(panel):.3f} sd {np.std(panel):.3f}")
