# SPDX-License-Identifier: AGPL-3.0-or-later
"""Copy a room's transcript safely, and check the copy before trusting it.

    python3 scripts_backup.py room3.db backups/            # one copy, timestamped
    python3 scripts_backup.py room3.db backups/ --keep 30  # and prune to the last 30

The transcript is the room's memory: members are told it is kept so the room can remember. A
memory that lives on exactly one disk is one dead disk from gone, so this exists.

Two things it does that `cp` does not:

  It uses SQLite's own online backup, so a copy taken while the room is running is consistent.
  The database runs in WAL mode; copying the .db file by hand while a round is in flight can
  capture a torn state, or miss committed events still in the -wal file.

  It checks the COPY before reporting success: SQLite's own integrity check, and that every
  event reads back. A backup nobody has checked is a belief rather than a backup.

Cron it. Hourly while a room is live, and keep the copies somewhere that is not this machine:

    17 * * * * cd /home/you/hope && python3 scripts_backup.py room3.db /var/backups/room --keep 48

Then get them off the box -- rsync, restic, an object store, anything. The failure this guards
against is the machine, so a copy that only lives on the machine guards against nothing.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time


def backup(db: str, out_dir: str, keep: int = 0) -> str:
    if not os.path.exists(db):
        sys.exit(f"no such database: {db}")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(db)
    dest = os.path.join(out_dir, f"{base}.{stamp}")
    tmp = dest + ".partial"

    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp)
        try:
            with dst:
                src.backup(dst)          # consistent even mid-round, WAL and all
        finally:
            dst.close()
    finally:
        src.close()

    events, last_id, problem = _verify(tmp)
    if problem is not None:
        os.replace(tmp, dest + ".BROKEN")
        sys.exit(f"REFUSED: the copy is not whole ({problem}). Kept as "
                 f"{dest}.BROKEN for inspection; the original was not touched.")
    os.replace(tmp, dest)

    pruned = _prune(out_dir, base, keep) if keep else 0
    size = os.path.getsize(dest)
    print(f"{dest}  ({size:,} bytes, {events:,} events, whole through #{last_id})"
          + (f"; pruned {pruned} older" if pruned else ""))
    return dest


def _verify(path: str):
    """Check the copy. Returns (events, last_id, problem_or_None)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from room.log import EventLog
    log = EventLog(path)
    try:
        ok = log.integrity()
        n = sum(1 for _ in log.iter())       # every row must read back as an event
        last = log.last_id()
        if ok != "ok":
            return n, last, f"integrity check: {ok}"
        return n, last, None
    except Exception as e:                   # a row that will not read back is a torn copy
        return 0, 0, f"{type(e).__name__}: {e}"
    finally:
        try:
            log.conn.close()
        except Exception:
            pass


def _prune(out_dir: str, base: str, keep: int) -> int:
    rows = sorted(f for f in os.listdir(out_dir)
                  if f.startswith(base + ".") and not f.endswith((".partial", ".BROKEN")))
    doomed = rows[:-keep] if keep < len(rows) else []
    for f in doomed:
        os.remove(os.path.join(out_dir, f))
    return len(doomed)


def main(argv=None):
    ap = argparse.ArgumentParser(description="back up a room's transcript and check the copy")
    ap.add_argument("db")
    ap.add_argument("out_dir")
    ap.add_argument("--keep", type=int, default=0, help="keep only the newest N copies (0 = keep all)")
    a = ap.parse_args(argv)
    backup(a.db, a.out_dir, a.keep)


if __name__ == "__main__":
    main()
