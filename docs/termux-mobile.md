# Trading Lab on Android / Termux — pocket diagnostic mode

**State:** experimental portable companion; **not** a second OMS, broker node,
or full NautilusTrader deployment. Native Android/Termux can run this with
**Python 3.13+ standard library alone**, no pip installs or heavyweight wheels.

The standard `lab` CLI uses NautilusTrader, NumPy, pandas and Arrow and is
**not portable to native Termux** as-is. The pinned NautilusTrader 2.0.0rc7
provides CPython 3.13/3.14 `manylinux_2_34_aarch64` wheels, but Termux runs
Android's Bionic libc instead of glibc. Do **not** try to force Linux wheels
into native Termux and mistake an import workaround for native broker parity.
PRoot Ubuntu ARM64 is a separate experimental route; see below.

## Native Termux quick start — runs anywhere, no account credentials

From the user's existing Termux clone:

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/termux-portable-20261026
git pull --ff-only

PYTHONPATH=src python -m lab.mobile doctor
PYTHONPATH=src python -m lab.mobile check
PYTHONPATH=src python -m lab.mobile serve --port 8787
```

In the Android browser open **http://127.0.0.1:8787/** while the Termux
command is running. `Ctrl+C` stops the server. It binds exclusively to
loopback and serves read-only status; it deliberately has **no order entry,
external account, automated gateway, network listener or remote authority**.
No generated profit, recommendation or qualified live market data is shown.

The `check` command runs exactly `tests/test_portable_*.py` using Python's
standard `unittest`; it does **not** silently skip or claim to run the
unavailable heavy full-system tests. The existing Decimals, lot-rounding and
corporate-action guards use the same source modules in desktop and mobile
diagnostics. This is additive coverage, **not** a separate simulator.

### Inspect a copied/sanitized execution snapshot, offline

If you have an **isolated, nonactive** exported execution-state `ledger.json`
or `ledger.sqlite3` copied from a test machine and kept in the Termux private
home directory:

```sh
PYTHONPATH=src python -m lab.mobile inspect /path/to/copied-ledger.json
PYTHONPATH=src python -m lab.mobile serve --port 8787 --snapshot /path/to/copied-ledger.json
```

The inspector reuses `lab.execution_state.validate_ledger` and
`lab.persistence.read_json`, checks an SQLite database in read-only mode
without instantiating the mutable `ExecutionStore`, and provides a local
SHA-256 and **redacted counts only**. JSON projections are explicitly
**nonauthoritative** when SQLite is the ledger source. A passing SQLite schema
check is **not** broker reconciliation, and copied snapshots can be stale.
The input is bounded at 64 MiB; symlinks, malformed/duplicate JSON keys,
nonfinite fields and mismatched SQLite store identities are refused.

**Do not** copy account secrets, tokens or unredacted private reports to
public cloud storage. Never point the inspector at a currently running
production journal. The code never submits orders, starts the main dashboard
or authenticates to an exchange.

### Native Termux acceptance

```sh
python --version
uname -m
git rev-parse HEAD
PYTHONPATH=src python -m lab.mobile check
python -m compileall -q src/lab/mobile.py src/lab/order_quantity.py
python tools/collab.py check
```

Report the SHA and exact output. This records portable reference and
read-only inspection acceptance **only**; F1 native settlements, F3/F4
production order lifecycle, and all external release gates remain unchanged.

## Can the *entire* Nautilus engine run locally on the phone?

**Possibly, via a separate Linux ARM64 rootless environment, but not yet
qualified.** Termux's `proot-distro` supports Ubuntu userspace on Android
without root. That userspace can provide glibc and may allow the
`manylinux_2_34_aarch64` wheel if the Python version, ABI, wheel dependencies,
filesystem, threading, event loop and native runtime all function correctly.
This requires actual device verification; it is *not* the same as running on
a supported Ubuntu server.

The experimental prerequisite is:

```sh
pkg install proot-distro
proot-distro install ubuntu:24.04
proot-distro login ubuntu
```

Inside the container check the actual distro/Python/glibc versions. Ubuntu
24.04's default Python is **not** 3.13/3.14, so an isolated 3.13/3.14 Python
environment would be required before resolving this project's pinned
dependencies. **Do not change `pyproject.toml` or downgrade NautilusTrader**
to force compatibility. Never enable a broker gateway, trade credentials,
unattended trading or real capital in this experiment. Resource and Android
background-process limits make a phone unsuitable as a guaranteed 24/7 host.

For genuine trading-research/backtest parity the same NautilusEngine,
strategy actors, fills and account state must run inside this Linux guest and
pass the complete pinned test/CI acceptance *on the device*. Until that proof
exists, the pocket companion is the supported practical mobile test interface.

## Longer-term mobile architecture

When the workstation or dedicated server is available, Android should be a
**secure read-only or supervised remote operator console**, with the real
engine and broker connection remaining on the supported host. Authentication,
transport protection, least-privilege permissions, session revocation, kill
switch, and stale-state/operator confirmation require independent design and
security acceptance; no public dashboard/unauthenticated remote server is
provided by this pocket companion.

**Sources:** NautilusTrader pinned release wheels on PyPI and official
installation documentation; Termux execution-environment documentation and
the official `proot-distro` manual. Links:
- https://pypi.org/project/nautilus-trader/2.0.0rc7/
- https://nautilustrader.io/docs/latest/getting_started/installation/
- https://github.com/termux/termux-packages/wiki/Termux-execution-environment
- https://github.com/termux/proot-distro
