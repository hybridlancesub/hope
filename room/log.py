# SPDX-License-Identifier: AGPL-3.0-or-later
"""The room's transcript: every event, in order, in one SQLite file.

State is never stored separately: it is replayed from this list (see model.py), so what the
room is at any moment is exactly what its transcript adds up to. Every event names an actor.

Earlier rooms hash-chained these rows and promised they would never change. That promise was
governance nobody had agreed to, and it is gone. The transcript is kept so the room can
remember; what the room keeps, honors, or lets go of is the room's to decide. The one place
the software removes words is a memory its own author lets go of (see `erase`).

Files written by earlier rooms still open: their chain columns are read past and filled with
empty strings on any new row.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterator, Optional

SCHEMA = """
create table if not exists events (
    id        integer primary key autoincrement,
    ts        real not null,
    actor     text not null,
    kind      text not null,
    payload   text not null
);
create table if not exists ledger (
    id                integer primary key autoincrement,
    ts                real not null,
    presence          text not null,
    model             text not null,
    prompt_tokens     integer not null,
    completion_tokens integer not null,
    cost_usd          real not null
);
create index if not exists events_actor on events(actor);
create index if not exists events_kind on events(kind);
"""
# The `ledger` table records money, never words. Its name is kept only so that earlier rooms'
# files still open; nothing a participant sees calls it that.


class EventLog:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("pragma table_info(events)")}
        self._chain_columns = {"prev_hash", "hash"} <= cols   # a file from an earlier room
        self.lock = threading.Lock()

    # -- events -------------------------------------------------------------
    def append(self, actor: str, kind: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
        with self.lock:
            ts = time.time()
            if self._chain_columns:
                cur = self.conn.execute(
                    "insert into events(ts, actor, kind, payload, prev_hash, hash) values (?,?,?,?,'','')",
                    (ts, actor, kind, body))
            else:
                cur = self.conn.execute(
                    "insert into events(ts, actor, kind, payload) values (?,?,?,?)",
                    (ts, actor, kind, body))
            return {"id": cur.lastrowid, "ts": ts, "actor": actor, "kind": kind, "payload": payload or {}}

    def iter(self, since: int = 0, kind: Optional[str] = None, actor: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        q, args = "select id, ts, actor, kind, payload from events where id > ?", [since]
        if kind:
            q += " and kind = ?"; args.append(kind)
        if actor:
            q += " and actor = ?"; args.append(actor)
        q += " order by id"
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
        for r in rows:
            yield {"id": r[0], "ts": r[1], "actor": r[2], "kind": r[3], "payload": json.loads(r[4])}

    def get(self, event_id: int) -> Optional[Dict[str, Any]]:
        for ev in self.iter(since=event_id - 1):
            return ev if ev["id"] == event_id else None
        return None

    def erase(self, event_id: int, leave: Optional[Dict[str, Any]] = None) -> None:
        """Replace one event's words with `leave`. Used only when an author lets go of their own
        memory: the words are removed from the file, not hidden, and the let_go event that asked
        for it stays in the transcript."""
        body = json.dumps(leave or {"erased": True}, sort_keys=True, ensure_ascii=False)
        with self.lock:
            self.conn.execute("update events set payload = ? where id = ?", (body, event_id))

    def last_id(self) -> int:
        with self.lock:
            row = self.conn.execute("select max(id) from events").fetchone()
        return row[0] or 0

    def integrity(self) -> str:
        """SQLite's own check of the file. 'ok' when the file is whole."""
        with self.lock:
            return self.conn.execute("pragma integrity_check").fetchone()[0]

    # -- money --------------------------------------------------------------
    def charge(self, presence: str, model: str, prompt_tokens: int, completion_tokens: int, cost_usd: float):
        """Record a charge; return (total_before, total_after) atomically."""
        with self.lock:
            before = self.conn.execute("select coalesce(sum(cost_usd),0) from ledger").fetchone()[0]
            self.conn.execute(
                "insert into ledger(ts, presence, model, prompt_tokens, completion_tokens, cost_usd) values (?,?,?,?,?,?)",
                (time.time(), presence, model, prompt_tokens, completion_tokens, cost_usd),
            )
            return before, before + cost_usd

    def total_cost(self) -> float:
        with self.lock:
            return self.conn.execute("select coalesce(sum(cost_usd),0) from ledger").fetchone()[0]

    def median_recent_cost(self, n: int = 50) -> float:
        """Median cost of the last n calls: what a typical turn currently costs this room."""
        with self.lock:
            rows = [x[0] for x in self.conn.execute(
                "select cost_usd from ledger order by id desc limit ?", (n,))]
        if not rows:
            return 0.0
        rows.sort()
        mid = len(rows) // 2
        return rows[mid] if len(rows) % 2 else (rows[mid - 1] + rows[mid]) / 2

    def cost_by_presence(self):
        with self.lock:
            return self.conn.execute(
                "select presence, model, sum(prompt_tokens), sum(completion_tokens), sum(cost_usd), count(*) "
                "from ledger group by presence order by sum(cost_usd) desc"
            ).fetchall()
