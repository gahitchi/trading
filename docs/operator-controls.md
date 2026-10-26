# Paper operating controls

`lab paper` uses the same paper-only engine in supervised, manual and autonomous
modes. **Supervised paper is the default in the guided UI and Docker Compose**, and
requires an explicit approval for each proposed order. Built-in strategies are not
unattended merely because their code runs successfully. An autonomous mandate is
available only for a separately approved one-strategy daily policy with matching
train/test research, point-in-time data, source integrity, exact risk/capital settings,
and a valid on-disk approval manifest. Run `lab research qualify-builtin` to evaluate
an eligible built-in; results that fail are never promoted. Multiple-strategy banks
and quick-speed built-ins remain diagnostic/supervised. Generated research strategies
still require their existing bound promotion and approval evidence.

After qualification, an autonomous mandate can be standing (no expiry) or given an
opt-in `--authority-hours N` lease (maximum 168). Supervised and manual mandates
have 24-hour default leases; restarts never silently renew them. Changing/removing
the source evidence revokes autonomous permission at the next authority read.
Use `lab control renew --mode supervised` to replace a standing mandate or
`lab kill` to stop trading. Real-money accounts remain prohibited.

```sh
lab paper --signals sma_trend --tickers SPY QQQ --mode supervised
lab control status
lab control approve --request EXACT_REQUEST_ID
lab control renew --mode autonomous            # standing (no expiry)
lab control renew --mode autonomous --hours 24 # opt-in lease
lab control renew --mode manual --hours 8 --target SPY=0.2 --target QQQ=0.1
```

Manual targets are fractions of ledger equity; omitted stocks target zero. Starting
manual mode with no targets creates a cash target and can close existing positions
at the next decision. Supply the intended target book explicitly when switching. They
are applied at the next normal decision, through reconciliation, price freshness,
quantity rounding, cost/gap reservations, portfolio limits, financing and daily caps.
Supervised mode proposes exact quantities against a particular book and completed
price timestamp. Approvals are consumed durably once, expire within one hour, and
become invalid if the book, proposal or mandate changes. The heartbeat checks for
approved proposals each minute and re-runs execution checks. A crash after consuming
an approval may require a fresh decision/approval; it cannot replay that approval.

Expiry blocks new normal orders and requests cancellation of outstanding orders
on the next heartbeat (at most one minute). A cancellation request is not a fill
or cancellation guarantee. Reconciled emergency liquidation remains available in
every mode. Hard halts are never silently cleared. Switching mode does not cancel
already submitted orders; use `lab kill` first when an immediate halt is required.

Authentication is the local operating-system user with a private, mode-0600 HMAC
key. This is not a multi-user identity service. Keep the runtime and backups private.
The isolated generated-code worker must never be granted access to them.

## Optional daily strategy bank

A bank is a predeclared fixed-priority regime policy, not a live optimizer. It can
select an eligible, unexpired strategy for an up/down/range/high-volatility regime,
retire a strategy on a signal exception, or leave its allocation in cash. Dated
retirement and eligibility use completed common-timestamp marks. Retired strategies
stay retired across restart. The selected strategy retains its original sleeve
budget; unselected budgets remain in cash. Risk limits still apply.

```sh
uv run python tools/make_bank.py --signals sma_trend rsi_reversion --tickers SPY QQQ \
  --benchmark SPY --eligible-after 2026-10-23T00:00:00+00:00 \
  --expires-at 2026-11-23T00:00:00+00:00 --output bank.json
lab paper --signals sma_trend rsi_reversion --tickers SPY QQQ --bank bank.json --check-only
```

Review the generated manifest's regime assignments and dates before using it.
The bank waits for its configured history window; missing history falls back to
cash. It is daily-only, disabled by default, and experimental until the **whole
policy** has independent out-of-sample qualification. Existing research promotion
and artifact checks are not bypassed; selecting a built-in does not establish edge.
Changing a bank changes runtime identity. There is no automatic clearing of
retirement, no automatic strategy promotion, and no authority to use a live account.
