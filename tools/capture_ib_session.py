"""Read-only evidence capture for the supervised IBKR paper session (C004). See lab/capture.py.

    python tools/capture_ib_session.py snapshot B01-preflight --out ~/trading-evidence
    python tools/capture_ib_session.py bundle   B03-daily-open --out ~/trading-evidence
    python tools/capture_ib_session.py sanitize ~/trading-evidence docs/evidence/2026-10-27 \
        --key ~/trading-evidence-key

snapshot connects with a node that has no strategy (it cannot place, change or cancel orders) and takes the
paper runtime lock, so stop `lab paper` first. Raw output stays outside the repository; only `sanitize`
output belongs in Git. Nothing here grades a case: both reviewers do, against docs/paper-acceptance.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    from lab.capture import GRADING_NOTE, bundle, sanitize, snapshot
    from lab.config import load_config

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="lab.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="read-only broker snapshot: account, positions, orders, raw bars")
    s.add_argument("phase")
    s.add_argument("--out", type=Path, required=True, help="private evidence store, outside the repository")
    s.add_argument("--tickers", nargs="+")
    s.add_argument("--wait", type=float, default=45.0, help="seconds to let reconciliation and bars arrive")
    s.add_argument("--days", type=int, default=14, help="days of bar history to request")
    b = sub.add_parser("bundle", help="copy the lab's runtime records for this phase")
    b.add_argument("phase")
    b.add_argument("--out", type=Path, required=True)
    z = sub.add_parser("sanitize", help="redacted copy plus hash manifest, for the repository")
    z.add_argument("src", type=Path)
    z.add_argument("dst", type=Path)
    z.add_argument("--key", type=Path, required=True, help="private pseudonymization key (created if missing)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "sanitize":
            m = sanitize(a.src, a.dst, a.key)
            print(f"{len(m['files'])} file(s) listed in {a.dst / 'MANIFEST.json'}; {GRADING_NOTE}")
            return 0
        cfg = load_config(a.config)
        if a.cmd == "snapshot":
            tickers = [t.upper() for t in (a.tickers or cfg.universe)]
            d = snapshot(cfg, tickers, a.out, a.phase, wait_s=a.wait, history_days=a.days)
        else:
            d = bundle(cfg, a.out, a.phase)
    except (ValueError, OSError) as e:
        print(f"capture refused: {e}", file=sys.stderr)
        return 2
    print(f"written: {d}; {GRADING_NOTE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
