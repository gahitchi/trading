# F0 Arch Linux acceptance — operator procedure

**Status:** operator evidence requested, **not** an F0 qualification or broker
approval. All commands below work on a locally installed Arch Linux checkout.
Do not run as root. They do not submit orders or authenticate to a broker.

This is an **F0-only** inspection of source, locked install, and private
reference-data byte identity. It does not claim the correctness of trades,
market feeds, borrow economics or F1–F8 gates.

## 1. Freeze the exact candidate being tested

Use a separate, disposable checkout, not a modified working branch:

```bash
git clone https://github.com/gahitchi/trading.git trading-f0-acceptance
cd trading-f0-acceptance
git fetch origin --prune
git switch --detach origin/chatgpt/f0-independent-wheel-build-20261027
git rev-parse HEAD
git status --porcelain
```

Save the full **40-character** commit SHA and compare it to the final
GitHub CI candidate I give you. `git status --porcelain` must print
nothing. If it does not match, do **not** rebase, stash, merge, amend or
overwrite any other contributor's branch. Stop and report the exact SHA.

## 2. Install and verify the locked research environment

On Arch, with `uv` installed:

```bash
uv sync --frozen --python 3.13 --extra dev
uv run --no-sync python -c 'import lab, nautilus_trader; print("imports: OK")'
uv run --no-sync lab --help
uv run --no-sync pytest -q tests/test_foundation_inputs.py tests/test_foundation_vulnerabilities.py tests/test_f0_workflow_contract.py
```

Keep the complete exit codes and any errors. These do not require a broker
login or market subscriptions, and none should place an order. A Python,
native-package, driver or runtime installation error **blocks operator
acceptance**, even if GitHub CI is green.

## 3. Capture write-once private receipts

Create a private evidence directory **outside the Git checkout**:

```bash
umask 077
mkdir -p "$HOME/trading-f0-private"
chmod 700 "$HOME/trading-f0-private"
git rev-parse HEAD
python3 tools/foundation_manifest.py create --out "$HOME/trading-f0-private/source.json"
python3 tools/foundation_manifest.py verify --input "$HOME/trading-f0-private/source.json"
```

Capture a separate F0-only configuration without touching tracked
`lab.toml`. The private test config contains **no broker identifiers**:

```bash
cat > "$HOME/trading-f0-private/reference.toml" <<'EOF'
[universe]
SPY = "ARCX"

[trading]
speed = "daily"

[paper]
mode = "paper"
EOF
```

The local market-data custody check additionally requires **real, lawfully
obtained** files `data/manifest.json` and `data/SPY.parquet` in the
checkout's ignored `data/` directory. **Do not** generate fake prices
or a fabricated vendor manifest to make the check green.

Once those existing inputs are present, run:

```bash
python3 -m tools.foundation_inputs --config "$HOME/trading-f0-private/reference.toml" \
  capture --out "$HOME/trading-f0-private/inputs.json"
python3 -m tools.foundation_inputs --config "$HOME/trading-f0-private/reference.toml" \
  verify --input "$HOME/trading-f0-private/inputs.json"
git status --porcelain
```

A missing input, invalid manifest, dirty Git source or changed byte hash must
fail. The receipts are **exclusive-write**; do not rerun `capture` on
an existing output, because overwriting evidence is prohibited. For a new
candidate SHA, use a new private folder.

## 4. What to share back for independent review

Report the exact commit SHA, your Arch kernel and architecture, Python
version, the pass/fail exit codes of each step, and whether the **private**
source/data receipts verified. Keep original `lab.toml`, private
reference TOML, actual market data, account IDs, tokens and raw receipts
**on your machine**. Do not upload them to GitHub Actions.

The independent F0 reviewer must still compare this operator evidence
with the exact signed CI artifacts and approve any remaining risks.
The known-CVE research exception is limited to **offline experiments
without broker sessions, credentials, exposed services or untrusted code**.
It grants no paper broker/trading authorization.

**This runbook is deliberately not a release checklist**. F1 accounting,
F2 source/time integrity, F3/F4 order safety, F5 statistical qualification,
F6 broker lifecycle, F7 deployment, and F8 release remain separate gates.
