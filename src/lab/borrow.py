"""Point-in-time stock-borrow evidence for qualified historical short research.

A model is not allowed to turn a guessed borrow fee into a valid locate.
The stock-loan snapshots are explicit data with provenance; absence, future,
stale or not-shortable records cause the proposed short to abstain.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path


class BorrowUnavailable(ValueError):
    """No valid point-in-time evidence for a qualified short."""


@dataclass(frozen=True, slots=True)
class BorrowQuote:
    ticker: str
    as_of: date
    available: bool
    apr: float | None
    source: str

    def __post_init__(self):
        if not self.ticker or self.ticker != self.ticker.strip().upper():
            raise ValueError("ticker must be nonempty uppercase")
        if type(self.as_of) is not date:
            raise ValueError("as_of must be a calendar date")
        if type(self.available) is not bool:
            raise ValueError("available must be a boolean")
        if not self.source or not self.source.strip() or self.source.strip().lower() in {"assumed", "unknown"}:
            raise ValueError("borrow evidence needs a named, non-assumed data source")
        if self.available and (self.apr is None or not math.isfinite(self.apr) or self.apr < 0):
            raise ValueError("available shares need a finite nonnegative observed APR")
        if self.apr is not None and (not math.isfinite(self.apr) or self.apr < 0):
            raise ValueError("borrow APR must be finite and nonnegative")


class BorrowBook:
    """Deterministic, strictly backward-looking borrow lookup."""

    def __init__(self, quotes: list[BorrowQuote], *, max_age_days: int = 3):
        if type(max_age_days) is not int or max_age_days < 0:
            raise ValueError("max_age_days must be a nonnegative integer")
        self.max_age_days = max_age_days
        by_ticker: dict[str, list[BorrowQuote]] = {}
        seen: set[tuple[str, date]] = set()
        for q in quotes:
            key = (q.ticker, q.as_of)
            if key in seen:
                raise ValueError(f"{key}: duplicate borrow snapshot; provenance ambiguous")
            seen.add(key)
            by_ticker.setdefault(q.ticker, []).append(q)
        self._quotes = {k: sorted(v, key=lambda q: q.as_of) for k, v in by_ticker.items()}

    def borrow_rate(self, ticker: str, day: date) -> tuple[float, dict]:
        """Return (APR, verifiable provenance) or fail closed; never use future data."""
        return self._rate(ticker, day, require_available=True)

    def _rate(self, ticker: str, day: date, *, require_available: bool) -> tuple[float, dict]:
        if type(day) is not date:
            raise ValueError("day must be a date")
        if not ticker or ticker != ticker.strip().upper():
            raise ValueError("ticker must be uppercase")
        q = next((x for x in reversed(self._quotes.get(ticker, ())) if x.as_of <= day), None)
        if q is None:
            raise BorrowUnavailable(f"{ticker} on {day}: no historical shortability/borrow evidence")
        age = (day - q.as_of).days
        if age > self.max_age_days:
            raise BorrowUnavailable(f"{ticker} on {day}: borrow evidence stale by {age} days")
        if (require_available and not q.available) or q.apr is None:
            raise BorrowUnavailable(f"{ticker} on {day}: shares unavailable or no verified fee")
        return q.apr, {"ticker": ticker, "source": q.source, "as_of": q.as_of.isoformat(), "age_days": age}

    def fingerprint(self) -> str:
        """Identity of the parsed evidence and freshness policy, independent of CSV row order."""
        payload = {
            "max_age_days": self.max_age_days,
            "quotes": [asdict(q) for t in sorted(self._quotes) for q in self._quotes[t]],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def restrict_target(self, ticker: str, current: float, target: float, day: date) -> tuple[float, str | None]:
        """Block only new short exposure; closing longs and covering shorts remain possible."""
        if target >= min(current, 0.0):
            return target, None
        try:
            self.borrow_rate(ticker, day)
        except BorrowUnavailable as exc:
            return min(current, 0.0), str(exc)
        return target, None

    def cost(
        self, signed_positions: dict[str, float], prices: dict[str, float], *, day: date, holding_days: int
    ) -> tuple[float, dict[str, dict]]:
        """Accrue qualified borrow fees on the short side only, with per-name evidence."""
        if type(holding_days) is not int or holding_days < 0:
            raise ValueError("holding_days must be a nonnegative integer")
        cost = 0.0
        evidence = {}
        for ticker, qty in sorted(signed_positions.items()):
            if not math.isfinite(qty):
                raise ValueError("position quantity must be finite")
            if qty >= 0:
                continue
            px = prices.get(ticker)
            if px is None or not math.isfinite(px) or px <= 0:
                raise BorrowUnavailable(f"{ticker}: no valid mark for the short borrow accrual")
            apr, proof = self.borrow_rate(ticker, day)
            cost += -qty * px * apr * holding_days / 365
            evidence[ticker] = {"apr": apr, **proof}
        return cost, evidence

    @classmethod
    def from_csv(cls, path: str | Path, *, max_age_days: int = 3) -> BorrowBook:
        """CSV columns: ticker, as_of, available (true/false), apr, source."""
        quotes = []
        with Path(path).open(newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or not {"ticker", "as_of", "available", "apr", "source"} <= set(reader.fieldnames):
                raise ValueError("borrow CSV must include ticker, as_of, available, apr, source")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"borrow CSV row {reader.line_num}: wrong number of fields")
                flag = row["available"].strip().lower()
                if flag not in {"true", "false"}:
                    raise ValueError("available must be true or false")
                quotes.append(
                    BorrowQuote(
                        row["ticker"].strip(),
                        date.fromisoformat(row["as_of"].strip()),
                        flag == "true",
                        float(row["apr"]) if row["apr"].strip() else None,
                        row["source"].strip(),
                    )
                )
        return cls(quotes, max_age_days=max_age_days)


class HistoricalBorrowLedger:
    """Daily close-book fee convention for historical research, never a live locate.

    A close's short quantities/marks accrue on [that date, next close date).
    Weekend days retain that book but look up each day's own as-of fee. Thus a
    Monday entry is never charged for the preceding weekend, and Monday's new
    fee cannot be backfilled over Friday. Missing held-book fees invalidate the run.
    """

    def __init__(self, book: BorrowBook):
        self.book = book
        self.day: date | None = None
        self.shorts: dict[str, float] = {}  # positive market values at the previous close
        self.evidence: list[dict] = []

    def close(self, day: date, positions: dict[str, float], prices: dict[str, float]) -> float:
        if type(day) is not date or (self.day is not None and day < self.day):
            raise ValueError("borrow ledger dates must be ordered calendar dates")
        shorts = {}
        for ticker, qty in positions.items():
            if not math.isfinite(qty):
                raise BorrowUnavailable(f"{ticker}: invalid short quantity")
            if qty >= 0:
                continue
            px = prices.get(ticker)
            if px is None or not math.isfinite(px) or px <= 0:
                raise BorrowUnavailable(f"{ticker}: no valid short closing mark")
            shorts[ticker] = -qty * px
        fee, evidence = 0.0, []
        d = self.day
        while d is not None and d < day:
            for ticker, value in sorted(self.shorts.items()):
                # A denial of new inventory does not make an existing loan free.
                apr, proof = self.book._rate(ticker, d, require_available=False)
                charge = value * apr / 365
                fee += charge
                evidence.append({**proof, "day": d.isoformat(), "apr": apr, "short_value": value, "fee": charge})
            d += timedelta(days=1)
        # Commit only after every required day validated; retries cannot partially double-charge.
        self.day, self.shorts = day, shorts
        self.evidence.extend(evidence)
        return fee
