# Termux pocket diagnostic device acceptance — operator-reported

**Date (operator device):** 2026-10-26 12:13 Europe/Rome
**Revision tested:** `4b20de4f324f46c5787c43dd9096277f6b7f0d04`
**Device:** Android 17, aarch64, native Termux, Python 3.13.13
**Application:** `PYTHONPATH=src python -m lab.mobile`
**Evidence classification:** user-reported console transcript and mobile-browser screenshot, not a remotely controlled or independently observed session.

## Reproducible evidence

- `git fetch origin; git switch chatgpt/termux-portable-20261026; git pull --ff-only` reported `Already up to date`. The branch on GitHub at the time was the exact revision above.
- `python -m lab.mobile doctor`: `mode=OFFLINE_DIAGNOSTIC_ONLY`, platform `Android-17-aarch64-64bit-ELF`, Python `3.13.13`, `git_checkout=true`, broker, live money, native Nautilus and order submissions all `false`.
- `python -m lab.mobile check`: nine `PocketAppTests` and nine `ReductionSafetyTests`, `Ran 18 tests in 0.106s`, `OK`.
- `python -m lab.mobile serve --port 8787`: console printed `http://127.0.0.1:8787/` and GET `/ HTTP/1.1` status `200` at 12:13:06.
- Screenshot supplied by operator shows correct dark mobile layout and labels: execution authority `DISABLED — offline diagnostics only`, Python 3.13.13, aarch64, native engine unavailable. The screenshot is user-supplied and not stored in the repository to avoid personal status/navigation metadata.

## Qualification boundary

**Passing:** mobile stdlib diagnostic startup, bundled portable reference tests, loopback web view rendering on this Android/Termux environment.
**Not tested:** the real `lab.markets` offline simulator, the Nautilus native engine, third-party scientific dependencies, Android background reliability, broker authentication, reconciled order state, F1 economic settlement, F6 broker demo, independent F8 release signoff.
Do not infer full Nautilus mobile support or productive trading from this evidence.

The following branch adds a separate experimental offline reference-simulator option; it must be **retested on device** before its mobile simulation behavior is qualified.
