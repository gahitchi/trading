# Native Termux causal-fence run: partial transcript

**Reported date:** 2026-10-26 (Europe/Rome), operator's native Termux checkout.

The operator supplied:

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/causal-source-fence-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

The visible output confirms switching to `chatgpt/causal-source-fence-20261026`, an up-to-date checkout, and every **fully visible** test through the start of `test_breaking_previous_digest_fails_even_with_recomputed_frame_hash` reported `ok`. The message **stops mid-test**, and **does not contain** the final `Ran 120 tests`, `OK`, `collab.py check` summary, or `git rev-parse HEAD`.

Hence **not enough evidence to attest 120/120 Termux tests or precise tested SHA**. At inspection the branch head was `37d90370ebb99fcad492d465b1599e34369eb324`; it is a plausible candidate but not independently proven to be the local checkout. Hosted GitHub Actions [40337566206](https://github.com/gahitchi/trading/actions/runs/40337566206) **completed SUCCESS** at that exact SHA (Python 3.13/3.14, Ruff, package, Docker, collaboration). This is a separate hosted CI fact and must never be substituted for the missing end of the Android transcript.

This record is committed only to a descendant code branch. It does not qualify the source for broker authentication, true cursor coverage, order release or external paper/live trading.
