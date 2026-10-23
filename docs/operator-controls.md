# Paper operating controls

`lab paper` starts autonomous paper operation on the first run. It creates a local
24-hour mandate bound to the account, strategy artifacts, universe and implementation
identity. A restart preserves the mandate and **does not renew it**. Use
`--authority-hours 168` when explicitly issuing a week-long mandate. Real-money
accounts remain prohibited.

```sh
lab paper --signals sma_trend --tickers SPY QQQ --mode supervised
lab control status
lab control approve --request EXACT_REQUEST_ID
lab control renew --mode autonomous --hours 24
lab control renew --mode manual --hours 8 --target SPY=0.2 --target QQQ=0.1
```

Manual targets are fractions of ledger equity; omitted stocks target zero. They
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
retire a strategy on an execution exception, or leave its allocation in cash. Dated
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
