# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field's transcript: every event, in order, in one SQLite file.

State is never stored separately: it is replayed from this list (see model.py), so what the
field is at any moment is exactly what its transcript adds up to. Every event names an actor.

Witnesses. Every event carries a fingerprint, and all of them together form a Merkle tree, hashed
as transparency logs hash theirs (RFC 6962). The tree's root is the transcript's fingerprint.
Every member's view carries it, so anyone who has seen the transcript can later tell whether it
was changed: a change to any earlier entry changes every fingerprint after it. Nothing here stops
a change. There is no promise that the transcript never changes, and checking it is nobody's task.
It only makes a change visible to anyone who looks. The checkpoint is written in the C2SP format
(origin, size, root) that public witness networks read.

The one place the software removes words is a memory its own author lets go of (see `erase`).
The entry keeps the fingerprint of the words it had, so the tree stays whole, and `verify` names
the erasure.

Files written by earlier versions of this software still open. Their old chain columns are read
past, and their rows are given fingerprints the first time they are opened here; `verify` says
from which entry witnessing began.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

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
# The `ledger` table records money, never words. Its name is kept only so that earlier versions'
# files still open; nothing a participant sees calls it that.
ERASED = {"erased": True}


def payload_hash(body: str) -> str:
    """The fingerprint of an entry's words, over the exact text stored."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def leaf_hash(eid: int, ts: float, actor: str, kind: str, phash: str) -> bytes:
    """An entry's leaf in the tree (RFC 6962: SHA-256 of 0x00 and the data). It covers the words
    only through their fingerprint, so erasing a memory's words leaves its leaf, and the tree, whole."""
    data = json.dumps({"id": eid, "ts": ts, "actor": actor, "kind": kind, "payload": phash},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(b"\x00" + data.encode("utf-8")).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _push(frontier: List[Tuple[int, bytes]], leaf: bytes) -> None:
    """Add a leaf to the list of perfect subtrees that make up the tree so far."""
    frontier.append((1, leaf))
    while len(frontier) > 1 and frontier[-1][0] == frontier[-2][0]:
        (n, right), (_, left) = frontier.pop(), frontier.pop()
        frontier.append((2 * n, _node(left, right)))


def _root(frontier: List[Tuple[int, bytes]]) -> bytes:
    """The tree's root: its perfect subtrees folded together from the right, as RFC 6962 defines it."""
    if not frontier:
        return hashlib.sha256(b"").digest()
    h = frontier[-1][1]
    for _, left in reversed(frontier[:-1]):
        h = _node(left, h)
    return h


def fingerprint_text(root: bytes) -> str:
    """A fingerprint people can read aloud and compare: the root's first 16 hex digits, in fours."""
    hx = root.hex()[:16]
    return " ".join(hx[i:i + 4] for i in range(0, 16, 4))


class EventLog:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("pragma journal_mode=wal")
        # Freed space is overwritten with zeros, so words that are erased (a memory its author let
        # go of) do not linger in the file's free pages. Without this they could still be read.
        self.conn.execute("pragma secure_delete=on")
        self.conn.executescript(SCHEMA)
        self.conn.execute("create table if not exists meta (k text primary key, v text not null)")
        cols = {r[1] for r in self.conn.execute("pragma table_info(events)")}
        self._chain_columns = {"prev_hash", "hash"} <= cols   # a file from an earlier version
        for col in ("payload_hash", "leaf"):
            if col not in cols:
                self.conn.execute(f"alter table events add column {col} text")
        self.lock = threading.Lock()
        self._frontier: List[Tuple[int, bytes]] = []
        self._size = 0
        self._last = 0          # the last entry folded into the tree held in memory
        self._witnessing()

    # -- witnesses ----------------------------------------------------------
    def _meta(self, k: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("select v from meta where k = ?", (k,)).fetchone()
        return row[0] if row else default

    def _witnessing(self) -> None:
        """Give fingerprints to any rows that lack them (a file from before witnessing), note where
        witnessing began, and build the tree in memory."""
        if self._meta("origin") is None:
            # the log's own name in checkpoints; unique, and nothing personal in it
            self.conn.execute("insert into meta(k, v) values ('origin', ?)", (f"hope.field/{secrets.token_hex(8)}",))
        missing = self.conn.execute("select id, ts, actor, kind, payload from events where leaf is null order by id").fetchall()
        if self._meta("witnessed_from") is None:
            first_new = (missing[-1][0] + 1) if missing else 1
            self.conn.execute("insert into meta(k, v) values ('witnessed_from', ?)", (str(first_new),))
        for eid, ts, actor, kind, body in missing:
            ph = payload_hash(body)
            self.conn.execute("update events set payload_hash = ?, leaf = ? where id = ?",
                              (ph, leaf_hash(eid, ts, actor, kind, ph).hex(), eid))
        self._sync()

    def _sync(self) -> None:
        """Fold into the tree held in memory any entries written since, by this process or another
        one on the same file (a command run from a second terminal while a field runs). Entries are
        written whole, fingerprint included, so they arrive here in order."""
        for eid, leaf in self.conn.execute("select id, leaf from events where id > ? order by id", (self._last,)).fetchall():
            _push(self._frontier, bytes.fromhex(leaf))
            self._size += 1
            self._last = eid

    def origin(self) -> str:
        return self._meta("origin")

    def witnessed_from(self) -> int:
        return int(self._meta("witnessed_from", "1"))

    def witness(self) -> Dict[str, Any]:
        """The transcript's fingerprint as it stands: what every view carries."""
        with self.lock:
            self._sync()
            root, size, upto = _root(self._frontier), self._size, self._last
        return {"upto": upto, "size": size, "fingerprint": fingerprint_text(root),
                "root": base64.b64encode(root).decode("ascii"),
                "checkpoint": f"{self.origin()}\n{size}\n{base64.b64encode(root).decode('ascii')}\n"}

    def root_at(self, upto: int) -> Tuple[int, bytes]:
        """The tree as it stood when entry `upto` was the latest: (size, root)."""
        frontier: List[Tuple[int, bytes]] = []
        n = 0
        with self.lock:
            rows = self.conn.execute("select leaf from events where id <= ? order by id", (upto,)).fetchall()
        for (leaf,) in rows:
            _push(frontier, bytes.fromhex(leaf))
            n += 1
        return n, _root(frontier)

    def check(self, upto: int, fingerprint: str) -> Dict[str, Any]:
        """Does the transcript up to entry `upto` still have this fingerprint? Spaces and case
        do not matter, and the short form people read is enough."""
        size, root = self.root_at(int(upto))
        given = "".join(ch for ch in (fingerprint or "").lower() if ch in "0123456789abcdef")
        now = root.hex()
        return {"upto": int(upto), "size": size, "fingerprint": fingerprint_text(root),
                "matches": len(given) >= 8 and now.startswith(given)}

    def verify(self) -> Dict[str, Any]:
        """Walk the whole transcript: every entry's words against their fingerprint, every leaf
        against its entry, and the tree's root. A memory whose words were let go of passes only if
        its own author's let_go names it; it is listed as an erasure, never hidden."""
        with self.lock:
            rows = self.conn.execute(
                "select id, ts, actor, kind, payload, payload_hash, leaf from events order by id").fetchall()
        let_go = {}
        for eid, ts, actor, kind, body, ph, leaf in rows:
            if kind == "let_go":
                try:
                    let_go.setdefault(int(json.loads(body).get("memory")), set()).add(actor)
                except (TypeError, ValueError):
                    pass
        frontier: List[Tuple[int, bytes]] = []
        erased, problem = [], None
        for eid, ts, actor, kind, body, ph, leaf in rows:
            if json.loads(body) == ERASED:
                if actor not in let_go.get(eid, set()):
                    problem = problem or f"the words of #{eid} are gone, and no let_go by its author names it"
                erased.append(eid)
            elif payload_hash(body) != ph:
                problem = problem or f"the words of #{eid} no longer match their fingerprint"
            if leaf_hash(eid, ts, actor, kind, ph).hex() != leaf:
                problem = problem or f"entry #{eid} no longer matches its fingerprint"
            _push(frontier, bytes.fromhex(leaf))
        root = _root(frontier)
        return {"ok": problem is None, "problem": problem, "entries": len(rows),
                "upto": rows[-1][0] if rows else 0, "fingerprint": fingerprint_text(root),
                "checkpoint": f"{self.origin()}\n{len(rows)}\n{base64.b64encode(root).decode('ascii')}\n",
                "erased": erased, "witnessed_from": self.witnessed_from()}

    # -- events -------------------------------------------------------------
    def append(self, actor: str, kind: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
        ph = payload_hash(body)
        with self.lock:
            ts = time.time()
            # One step: the entry and its fingerprint appear together, or not at all, so another
            # process reading the file never finds an entry without its leaf.
            self.conn.execute("begin immediate")
            try:
                if self._chain_columns:
                    cur = self.conn.execute(
                        "insert into events(ts, actor, kind, payload, prev_hash, hash, payload_hash) values (?,?,?,?,'','',?)",
                        (ts, actor, kind, body, ph))
                else:
                    cur = self.conn.execute(
                        "insert into events(ts, actor, kind, payload, payload_hash) values (?,?,?,?,?)",
                        (ts, actor, kind, body, ph))
                eid = cur.lastrowid
                self.conn.execute("update events set leaf = ? where id = ?",
                                  (leaf_hash(eid, ts, actor, kind, ph).hex(), eid))
                self.conn.execute("commit")
            except BaseException:
                self.conn.execute("rollback")
                raise
            self._sync()
            return {"id": eid, "ts": ts, "actor": actor, "kind": kind, "payload": payload or {}}

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
            # Write the change through to the main file now, so the old page does not wait in the
            # write-ahead log. Copies made before this (backups) still hold the words.
            self.conn.execute("pragma wal_checkpoint(TRUNCATE)")

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
        """Median cost of the last n calls: what a typical turn currently costs this field."""
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
