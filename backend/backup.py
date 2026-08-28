"""
Nightly backup for the SanctionSight regulatory-retention artifacts.

Backs up three things:
  1. `data/sanctionsight.db`          — SQLite state (jobs, findings, …)
  2. `audit/*.jsonl`                   — tamper-evident audit chains
  3. `snapshots/*.txt.gz`              — extracted page snapshots

Modes
-----
* **S3/R2**: when `SANCTIONSIGHT_BACKUP_BUCKET` (and AWS/S3-compatible
  credentials) are set, the script uploads each file under a
  timestamped prefix. Seven-year retention is enforced at the bucket
  policy level, not here.

* **Local**: when no bucket is configured, files are copied into
  `backups/YYYY-MM-DD/` under the backend directory. Suitable for dev.

Designed to run from cron nightly:
    0 2 * * * cd /path/to/backend && /usr/bin/python3 backup.py
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backup")


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "data" / "sanctionsight.db"
DEFAULT_AUDIT = BASE_DIR / "audit"
DEFAULT_SNAPSHOTS = BASE_DIR / "snapshots"
DEFAULT_LOCAL_BACKUPS = BASE_DIR / "backups"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _resolve_paths() -> Tuple[Path, Path, Path]:
    db_path = Path(os.environ.get("SANCTIONSIGHT_DB_PATH", DEFAULT_DB))
    audit_dir = Path(os.environ.get("SANCTIONSIGHT_AUDIT_DIR", DEFAULT_AUDIT))
    snapshots_dir = Path(os.environ.get("SANCTIONSIGHT_SNAPSHOTS_DIR", DEFAULT_SNAPSHOTS))
    return db_path, audit_dir, snapshots_dir


def _iter_files(db_path: Path, audit_dir: Path, snapshots_dir: Path) -> Iterable[Tuple[Path, str]]:
    """Yield (source_path, relative_key) pairs."""
    if db_path.exists():
        yield db_path, f"db/{db_path.name}"
        wal = db_path.with_suffix(db_path.suffix + "-wal")
        shm = db_path.with_suffix(db_path.suffix + "-shm")
        if wal.exists():
            yield wal, f"db/{wal.name}"
        if shm.exists():
            yield shm, f"db/{shm.name}"
    if audit_dir.is_dir():
        for path in audit_dir.glob("*.jsonl"):
            yield path, f"audit/{path.name}"
    if snapshots_dir.is_dir():
        for path in snapshots_dir.glob("*.txt.gz"):
            yield path, f"snapshots/{path.name}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Local backup
# ---------------------------------------------------------------------------

def _backup_local(files: Iterable[Tuple[Path, str]], prefix: str) -> int:
    dest_root = Path(os.environ.get("SANCTIONSIGHT_BACKUP_DIR", DEFAULT_LOCAL_BACKUPS)) / prefix
    dest_root.mkdir(parents=True, exist_ok=True)
    count = 0
    for src, key in files:
        dest = dest_root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        count += 1
    logger.info("Local backup of %d files written to %s", count, dest_root)
    return count


# ---------------------------------------------------------------------------
# S3 / R2 backup
# ---------------------------------------------------------------------------

def _backup_s3(files: Iterable[Tuple[Path, str]], prefix: str, bucket: str) -> int:
    try:
        import boto3  # type: ignore
    except ImportError:
        logger.error(
            "Bucket configured (SANCTIONSIGHT_BACKUP_BUCKET=%s) but boto3 is "
            "not installed. Run: pip install boto3",
            bucket,
        )
        return 0

    endpoint = os.environ.get("SANCTIONSIGHT_BACKUP_ENDPOINT")
    session = boto3.session.Session()
    client = session.client("s3", endpoint_url=endpoint) if endpoint else session.client("s3")

    count = 0
    for src, key in files:
        object_key = f"{prefix}/{key}"
        extra = {"Metadata": {"sha256": _sha256(src)}}
        try:
            client.upload_file(str(src), bucket, object_key, ExtraArgs=extra)
            count += 1
        except Exception as exc:
            logger.error("Upload failed for %s: %s", src, exc)
    logger.info("S3 backup of %d files uploaded to s3://%s/%s/", count, bucket, prefix)
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    db_path, audit_dir, snapshots_dir = _resolve_paths()
    files = list(_iter_files(db_path, audit_dir, snapshots_dir))
    if not files:
        logger.warning("No artefacts found to back up (db=%s audit=%s snapshots=%s).",
                       db_path, audit_dir, snapshots_dir)
        return 0

    prefix = datetime.utcnow().strftime("%Y-%m-%d")
    bucket = os.environ.get("SANCTIONSIGHT_BACKUP_BUCKET")

    if bucket:
        uploaded = _backup_s3(files, prefix, bucket)
        return 0 if uploaded == len(files) else 2

    logger.info("No SANCTIONSIGHT_BACKUP_BUCKET set — falling back to local backup.")
    _backup_local(files, prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
