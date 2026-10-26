# Native Termux independent financial kernel — operator acceptance

**Date:** 2026-10-26 (Europe/Rome)  
**Operator-supplied tested commit:** `41f9175d6a5b9406fbbcaf33374c4435c66a4b85`  
**Tested branch:** `chatgpt/independent-ledger-kernel-20261026`  
**Evidence:** owner-provided terminal transcript; not a signed or remotely observed test execution. Earlier device logs identify Android 17/aarch64, Python 3.13.13, but this invocation did not separately print Python version.

Commands:
```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/independent-ledger-kernel-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

Observed:
- `Already up to date` at the selected branch before testing.
- **60 tests passed in 1.213 seconds**, `OK`.
- Subcategories: 13 original corporate actions, 12 corporate journal tests, 12 independent double-entry tests, 9 mobile inspector tests, 9 reduction-safety checks, and 5 offline simulator tests.
- Including positive/negative corporate receivables, no cash before actual receipt, forged zero-sum postings rejected, single-transaction posting with existing SQLite journal, explicit journal v1->v2 incompatibility, unauthenticated data rejected by pure domain assumptions, and no site-packages.
- `python tools/collab.py check`: `111 file(s) in this outbox tree, 35 thread(s) across branches, 0 error(s)`.
- `git rev-parse HEAD`: exactly `41f9175d6a5b9406fbbcaf33374c4435c66a4b85`.
- GitHub Actions run [40323298372](https://github.com/gahitchi/trading/actions/runs/40323298372) for the exact tested SHA has completed **success** across the full workflow, including Python 3.13/3.14, lint, packaging, collaboration and Docker.

**Qualifications:** this establishes independent **portable exact-Decimal financial-reference accounting** and constrained SQLite crash replay, not a complete custody ledger or actual broker reconciliation. The standalone kernel does not submit orders, authenticate broker statements, update Nautilus Account/Cache/Portfolio, account for securities financing or multiple currencies, or establish market-data quality or positive investment research. F1 is still incomplete, and F3/F4 execution plus F6/F8 remain gated. No live capital is authorized.

This evidence note is on a descendant development branch, **not part of the SHA that the operator tested**.
