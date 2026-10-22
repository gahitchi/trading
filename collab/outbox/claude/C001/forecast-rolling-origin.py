"""Evidence for F12: run on the hardening branch (needs the dev extras for the GOOG sample).

    PYTHONPATH=src python collab/outbox/claude/C001/forecast-rolling-origin.py

Result at 9a04385: 39 evaluations; beats train-mean MAE baseline 0.26; beats base-rate Brier 0.41;
90% interval coverage mean 0.883, min 0.583.
"""

import pandas as pd

from lab import data
from lab.predictive_research import ForecastSpec, forecast_research

goog = data.validate(data.sample_goog(), "GOOG")
rows = []
for d in [ts.date() for ts in goog.index[600::40]]:  # rolling forecast origins
    try:
        r = forecast_research("GOOG", goog, as_of=d, spec=ForecastSpec(horizon=1, min_train=252, calibration=60, test=60))
    except Exception:
        continue
    t = r["frozen_oos_test"]
    rows.append(
        (
            t["model_mae_log_return"] < t["train_mean_baseline_mae_log_return"],
            t["empirical_up_probability_brier"] < t["train_frequency_baseline_brier"],
            t["observed_interval_coverage"],
        )
    )
df = pd.DataFrame(rows, columns=["beats_mae", "beats_brier", "coverage90"])
print(len(df), "evaluations")
print("beats train-mean MAE baseline:", round(df.beats_mae.mean(), 2))
print("beats base-rate Brier baseline:", round(df.beats_brier.mean(), 2))
print("90% coverage: mean", round(df.coverage90.mean(), 3), "min", round(df.coverage90.min(), 3))
