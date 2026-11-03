from __future__ import annotations

"""Housekeeping for recordings that produced nothing.

Silent fragments and failed transcriptions have no content, so they are hidden
from the dashboard and cleaned up automatically once they are old enough.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import ROOT, settings
from recycle import recycle_paths
from storage import CONTENT_SQL, delete_segments, transaction

STATE_PATH = ROOT / "runtime" / "maintenance-state.json"
LOG_PATH = ROOT / "runtime" / "maintenance.log"
CLEANUP_HOUR = 3

logger = logging.getLogger("maintenance")


@dataclass(frozen=True)
class PurgePlan:
    count: int = 0
    bytes: int = 0
    paths: tuple[Path, ...] = ()

    @property
    def megabytes(self) -> float:
        return self.bytes / 1024**2


def _cutoff(days: int) -> str:
    moment = datetime.now().astimezone() - timedelta(days=days)
    return moment.astimezone(timezone.utc).isoformat()


def empty_segments(days: int) -> list[sqlite3.Row]:
    """Rows without content that are older than `days`."""
    with transaction() as conn:
        return conn.execute(
            f"SELECT id, path, recorded_at FROM segments WHERE NOT {CONTENT_SQL} AND recorded_at < ? ORDER BY recorded_at",
            (_cutoff(days),),
        ).fetchall()


def plan_purge(days: int | None = None) -> PurgePlan:
    rows = empty_segments(days if days is not None else settings.empty_retention_days)
    paths: list[Path] = []
    total = 0
    for row in rows:
        path = Path(str(row["path"]))
        paths.append(path)
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return PurgePlan(count=len(rows), bytes=total, paths=tuple(paths))


def purge_empty_segments(days: int | None = None) -> PurgePlan:
    """Move old empty recordings to the recycle bin and drop their rows."""
    days = days if days is not None else settings.empty_retention_days
    rows = empty_segments(days)
    if not rows:
        return PurgePlan()
    paths = [Path(str(row["path"])) for row in rows]
    removed, freed = recycle_paths(paths)
    removed_set = {path for path in removed}
    ids = [
        int(row["id"])
        for row in rows
        if Path(str(row["path"])) in removed_set or not Path(str(row["path"])).exists()
    ]
    if ids:
        delete_segments(ids)
    plan = PurgePlan(count=len(ids), bytes=freed, paths=tuple(removed))
    logger.info("Moved %s empty recordings to the recycle bin (%.1f MB)", plan.count, plan.megabytes)
    return plan


def _load_state() -> dict[str, str]:
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_scheduled_cleanup(days: int | None = None, hour: int = CLEANUP_HOUR) -> PurgePlan | None:
    """Run at most once per day, after `hour`, so old silence does not pile up."""
    retention = settings.empty_retention_days if days is None else days
    if retention <= 0:
        return None
    now = datetime.now().astimezone()
    if now.hour < hour:
        return None
    state = _load_state()
    today = now.date().isoformat()
    if state.get("last_cleanup") == today:
        return None
    plan = purge_empty_segments(days)
    state["last_cleanup"] = today
    state["last_count"] = str(plan.count)
    _save_state(state)
    return plan


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Delete old recordings that have no content.")
    parser.add_argument("--days", type=int, default=None, help="Keep empty recordings newer than this many days.")
    parser.add_argument("--dry-run", action="store_true", help="Only report what would be deleted.")
    args = parser.parse_args()
    plan = plan_purge(args.days) if args.dry_run else purge_empty_segments(args.days)
    print(f"{'可清理' if args.dry_run else '已清理'} {plan.count} 条，约 {plan.megabytes:.1f} MB")


if __name__ == "__main__":
    main()
