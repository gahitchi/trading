"""Verified, resumable gzip archival for *finalized* market journals.

The archived bytes must decompress to exactly the original source bytes.
Archiving never reads live .jsonl.open streams. Existing raw files are
retained by default; --prune-verified removes them only after durable
archive and manifest publication and re-verification.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

CHUNK = 1024 * 1024


class ArchiveIntegrityError(ValueError):
    """Archive verification failed; original raw data is preserved."""


@dataclass(frozen=True)
class ArchiveResult:
    path: Path
    records: int
    original_bytes: int
    compressed_bytes: int
    pruned: bool


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _scan(reader: BinaryIO, *, require_newline: bool = True) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    size = 0
    records = 0
    final_byte = b""
    for chunk in iter(lambda: reader.read(CHUNK), b""):
        digest.update(chunk)
        size += len(chunk)
        records += chunk.count(b"\n")
        final_byte = chunk[-1:]
    if size and require_newline and final_byte != b"\n":
        raise ArchiveIntegrityError("finalized journal is missing its terminating newline")
    return digest.hexdigest(), size, records


def _scan_path(path: Path, *, require_newline: bool = True) -> tuple[str, int, int]:
    with path.open("rb") as source:
        return _scan(source, require_newline=require_newline)


def _scan_archive(path: Path) -> tuple[str, int, int]:
    try:
        with gzip.open(path, "rb") as source:
            return _scan(source)
    except (OSError, EOFError, zlib.error) as exc:
        raise ArchiveIntegrityError(f"incomplete or invalid gzip archive: {path}") from exc


def _paths(source: Path) -> tuple[Path, Path, Path, Path]:
    if not source.name.endswith(".jsonl"):
        raise ValueError("expected a finalized .jsonl file")
    archive = source.with_name(source.name + ".gz")
    manifest = source.with_name(source.name + ".gz.manifest.json")
    return archive, manifest, archive.with_name(archive.name + ".partial"), manifest.with_name(manifest.name + ".tmp")


def _atomic_manifest(manifest: Path, temp: Path, metadata: dict) -> None:
    with temp.open("xb") as f:
        f.write((json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode())
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, manifest)
    _sync_dir(manifest.parent)


def _verify_archive(archive: Path, manifest: Path, source: Path | None = None) -> ArchiveResult:
    if not archive.exists() or not manifest.exists():
        raise ArchiveIntegrityError(f"archive and manifest both required: {archive}")
    metadata = json.loads(manifest.read_text())
    decompressed_sha, decompressed_size, rows = _scan_archive(archive)
    compressed_sha, compressed_size, _ = _scan_path(archive, require_newline=False)
    for key, actual in (
        ("source_sha256", decompressed_sha),
        ("source_bytes", decompressed_size),
        ("records", rows),
        ("gzip_sha256", compressed_sha),
        ("gzip_bytes", compressed_size),
    ):
        if metadata.get(key) != actual:
            raise ArchiveIntegrityError(f"manifest mismatch for {archive}: {key}")
    if source is not None and source.exists() and _scan_path(source) != (decompressed_sha, decompressed_size, rows):
        raise ArchiveIntegrityError(f"source differs from gzip archive: {source}")
    return ArchiveResult(archive, rows, decompressed_size, compressed_size, False)


def _archive_one(source: Path, *, prune_verified: bool = False) -> ArchiveResult:
    """Called only with the containing directory's capture lock held."""
    archive, manifest, partial, temp_manifest = _paths(source)
    if not source.is_file() or source.is_symlink():
        raise ArchiveIntegrityError(f"source is missing or not a regular file: {source}")
    if manifest.exists() and not archive.exists():
        raise ArchiveIntegrityError(f"manifest without archive: {manifest}")
    if not archive.exists():
        # Original finalized journal is intact and lock is held: stale partial
        # compressed output can safely be regenerated after a crash.
        partial.unlink(missing_ok=True)
        with source.open("rb") as raw, partial.open("xb") as packed:
            with gzip.GzipFile(filename="", mode="wb", fileobj=packed, compresslevel=3, mtime=0) as compressed:
                for chunk in iter(lambda: raw.read(CHUNK), b""):
                    compressed.write(chunk)
            packed.flush()
            os.fsync(packed.fileno())
        if _scan_archive(partial) != _scan_path(source):
            raise ArchiveIntegrityError(f"compression round-trip mismatch: {source}")
        os.replace(partial, archive)
        _sync_dir(source.parent)
    if not manifest.exists():
        temp_manifest.unlink(missing_ok=True)
        original_sha, original_bytes, records = _scan_path(source)
        if _scan_archive(archive) != (original_sha, original_bytes, records):
            raise ArchiveIntegrityError(f"incomplete or mismatched gzip archive: {archive}")
        gzip_sha, gzip_bytes, _ = _scan_path(archive, require_newline=False)
        _atomic_manifest(
            manifest,
            temp_manifest,
            {
                "schema_version": 1,
                "source_sha256": original_sha,
                "source_bytes": original_bytes,
                "records": records,
                "gzip_sha256": gzip_sha,
                "gzip_bytes": gzip_bytes,
            },
        )
    result = _verify_archive(archive, manifest, source)
    if prune_verified:
        source.unlink()
        _sync_dir(source.parent)
        return ArchiveResult(result.path, result.records, result.original_bytes, result.compressed_bytes, True)
    return result


def archive_directory(directory: Path, *, prune_verified: bool = False) -> list[ArchiveResult]:
    """Only finalized segments; refuse to race another recorder or archiver."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".capture.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("capture or archival already holds this directory") from exc
        try:
            output = [_archive_one(src, prune_verified=prune_verified) for src in sorted(directory.glob("*.jsonl"))]
            # Previously archived, pruned segments must still verify on reruns.
            handled = {item.path for item in output}
            for path in sorted(directory.glob("*.jsonl.gz")):
                if path not in handled:
                    manifest = path.with_name(path.name + ".manifest.json")
                    output.append(_verify_archive(path, manifest))
            return output
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and gzip finalized public-market journals")
    parser.add_argument("--out", type=Path, default=Path("data/market-journal"))
    parser.add_argument(
        "--prune-verified", action="store_true", help="remove raw files only after verified gzip + manifest"
    )
    args = parser.parse_args(argv)
    try:
        reports = archive_directory(args.out, prune_verified=args.prune_verified)
    except (ArchiveIntegrityError, RuntimeError, ValueError, OSError) as exc:
        parser.error(str(exc))
    for report in reports:
        print(
            f"{report.path.name}: {report.records} records "
            f"{report.original_bytes} -> {report.compressed_bytes} bytes "
            f"({'raw pruned' if report.pruned else 'raw retained or already archived'})"
        )
    print(f"Verified archives: {len(reports)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
