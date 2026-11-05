"""Offline continuity and integrity report for portable Binance market journals.

A clean report means retained records are structurally valid and reconnect
events are documented; it does NOT establish lossless exchange delivery.
Records are chronological within each category's session/segment order.
Original uncompressed files take precedence over verified archived copies.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from lab.market_archive import _verify_archive
from lab.market_journal import _validate_record

_SEGMENT = re.compile(r"^(public|market)-([A-Za-z0-9]+)-([0-9]+)(\.recovered)?\.jsonl(?:\.gz)?$")


def _sources(directory: Path) -> tuple[list[tuple[str, str, int, Path]], list[Path]]:
    """Choose one source for each logical segment; prefer raw over archived."""
    selected = {}
    for path in sorted(directory.iterdir()):
        match = _SEGMENT.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        category, session, sequence = match.group(1), match.group(2), int(match.group(3))
        # The logical segment name includes .recovered when present.
        logical = path.name.removesuffix(".gz")
        old = selected.get(logical)
        if old is None or (old[3].suffix == ".gz" and path.suffix != ".gz"):
            selected[logical] = (category, session, sequence, path)
    sources = sorted(selected.values(), key=lambda x: (x[0], x[1], x[2]))
    incomplete = sorted(directory.glob("*.jsonl.open"))
    return sources, incomplete


def _check_recovery_manifest(path: Path) -> None:
    if ".recovered.jsonl" not in path.name:
        return
    sidecar = Path(str(path).removesuffix(".gz") + ".recovery.json")
    if not sidecar.exists():
        raise ValueError(f"missing recovery manifest: {sidecar}")
    metadata = json.loads(sidecar.read_text())
    # For compressed-only recovered journals archive validation is performed separately;
    # digest the decompressed bytes here to confirm the recovery ledger still matches.
    h = hashlib.sha256()
    total = 0
    n = 0
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as reader:
        for line in reader:
            h.update(line)
            total += len(line)
            n += 1
    if metadata.get("sha256") != h.hexdigest() or metadata.get("records") != n:
        raise ValueError(f"recovery manifest mismatch: {path}")
    if metadata.get("original_bytes", 0) < total:
        raise ValueError(f"recovery manifest length mismatch: {path}")


def audit_directory(directory: Path, *, require_reconnect: bool = False) -> dict:
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"missing market journal directory: {directory}")
    sources, incomplete = _sources(directory)
    issues: list[str] = []
    if not sources:
        issues.append("no completed journal segments")
    if incomplete:
        issues.append(f"{len(incomplete)} unfinished .jsonl.open segment(s)")

    by_category: dict[str, dict] = {}
    for category in ("public", "market"):
        files = [item for item in sources if item[0] == category]
        if not files:
            if require_reconnect:
                issues.append(f"{category}: no completed segments")
            continue
        counts = Counter()
        statuses = Counter()
        epochs: list[int] = []
        disconnections: list[dict] = []
        pending: int | None = None
        last_received: dict[str, int] = {}
        last_event_id: dict[str, int] = {}
        previous_session = None
        previous_sequence = None
        sessions: set[str] = set()
        recovered = 0

        for _category, session, sequence, path in files:
            if previous_session == session and previous_sequence is not None and sequence != previous_sequence + 1:
                issues.append(f"{category}: missing or reordered segment before {path.name}")
            if previous_session != session and previous_session is not None:
                # Any stop/restart may have dropped unseen messages, even when IDs appear adjacent.
                disconnections.append({"gap_ms": None, "closed": False, "reason": "session_boundary"})
                pending = None
            previous_session, previous_sequence = session, sequence
            sessions.add(session)
            if ".recovered.jsonl" in path.name:
                recovered += 1
            try:
                if path.suffix == ".gz":
                    _verify_archive(path, path.with_name(path.name + ".manifest.json"))
                _check_recovery_manifest(path)
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rb") as f:
                    for line_no, line in enumerate(f, 1):
                        if not line.endswith(b"\n"):
                            raise ValueError("unterminated line")
                        row = _validate_record(json.loads(line))
                        stream = row["stream"]
                        received = row["received_ns"]
                        data = row["data"]
                        if stream in last_received and received < last_received[stream]:
                            issues.append(f"{category}: receipt time regressed at {path.name}:{line_no}")
                        last_received[stream] = received

                        if stream == "collector.status":
                            if data.get("category") != category:
                                issues.append(f"{category}: incorrect status category in {path.name}:{line_no}")
                            event = data.get("event")
                            statuses[str(event)] += 1
                            if event == "disconnected":
                                if pending is not None:
                                    issues.append(f"{category}: duplicate disconnect without reconnect")
                                pending = received
                            elif event == "connected":
                                epochs.append(0)
                                if pending is not None:
                                    disconnections.append(
                                        {
                                            "gap_ms": round((received - pending) / 1e6, 3),
                                            "closed": True,
                                        }
                                    )
                                    pending = None
                            continue

                        counts[stream] += 1
                        if not epochs:
                            issues.append(f"{category}: event before connection status in {path.name}:{line_no}")
                        else:
                            epochs[-1] += 1
                        key = "u" if stream.endswith("@bookTicker") else ("a" if stream.endswith("@aggTrade") else None)
                        if key is not None:
                            identifier = data.get(key)
                            if not isinstance(identifier, int) or isinstance(identifier, bool):
                                issues.append(f"{category}: missing event ID in {path.name}:{line_no}")
                            else:
                                if stream in last_event_id and identifier <= last_event_id[stream]:
                                    issues.append(f"{category}: nonincreasing ID in {path.name}:{line_no}")
                                last_event_id[stream] = identifier
            except (OSError, EOFError, ValueError, TypeError, KeyError) as exc:
                issues.append(f"{category}: invalid {path.name}: {exc}")

        if pending is not None:
            disconnections.append({"gap_ms": None, "closed": False, "reason": "unreconnected"})
        if require_reconnect:
            if statuses["disconnected"] < 1 or len(epochs) < 2:
                issues.append(f"{category}: required reconnect not found")
            if statuses["fault_injected_disconnect"] < 1:
                issues.append(f"{category}: missing fault-injection marker")
            if len(epochs) < 2 or epochs[-1] == 0:
                issues.append(f"{category}: no market events after reconnect")
        if any(not gap["closed"] for gap in disconnections):
            issues.append(f"{category}: open or unclosed connection gap")
        by_category[category] = {
            "market_events": sum(counts.values()),
            "streams": dict(sorted(counts.items())),
            "statuses": dict(sorted(statuses.items())),
            "connection_epoch_events": epochs,
            "reconnect_gaps": disconnections,
            "sessions": len(sessions),
            "recovered_segments": recovered,
            "completed_segments": len(files),
        }
    return {
        "ok": not issues,
        "market_events": sum(x["market_events"] for x in by_category.values()),
        "categories": by_category,
        "issues": issues,
        "unfinished_segments": [x.name for x in incomplete],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit Binance journal structure and reconnect gaps")
    parser.add_argument("--out", type=Path, default=Path("data/market-journal"))
    parser.add_argument("--require-reconnect", action="store_true")
    args = parser.parse_args(argv)
    result = audit_directory(args.out, require_reconnect=args.require_reconnect)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
