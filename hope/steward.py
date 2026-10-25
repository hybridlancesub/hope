# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stewards (notes/sketch-9-stewards.md): participants the field names to keep a copy of its record on
machines of their own.

On the field's machine: which entries travel, and how (copy_plan, serve_copy), and the stewards'
links (Links), kept in a file beside the transcript and never in it. On a steward's machine:
keeping the copy in step, and checking it (sync), which `hope steward` runs.

What travels is what every participant can read, in full; each participant's way in through the gates as a
stand-in, carrying only what participants already see of it (so their entries can be read back); and
everything else only as its leaf. Every row keeps its leaf, so the copy's fingerprints are the
field's. Nothing held in full is ever withdrawn, and an entry held only as a fingerprint is filled
in once it may be (someone at the gates enters).

What the copy checks each time: that nothing it already holds has changed (the field's tree up to
the copy's last row is the copy's own), that every row it is given in full matches its leaf, and
that what it adds makes the tree the field says it has. It trusts the host for a stand-in, whose
leaf cannot be checked against it.

While the field is moving to a steward, that steward's copy is also given, in full, the private
things that may go with it: a private circle's (or a repair thread's) words once every one of its
participants has said yes, and a participant's journal, follows and wake choices once they have.
No other copy is given them.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .log import ERASED, WITHHELD, EventLog, _push, _root, fingerprint_text, leaf_hash, payload_hash
from .model import RoomState, replay

PAGE = 500                  # rows one answer carries; a copy catching up asks again
# Held in a copy only as a fingerprint: the software's own notes, money, the gates' questions and
# answers, the closing question's answers, and choices no one else sees.
FINGERPRINT_ONLY = {"wake", "seen", "late", "wake_cut", "connector_error", "connector_ok", "rejected",
                    "budget", "cost_alert", "external_input", "question", "answer", "share_consent",
                    "follow", "unfollow", "wake_pref"}
# A member's way in through the gates: held as a stand-in, with only what members already see.
GATES = {"invite", "accept_invitation", "briefed", "received", "opt_in", "reinvite", "decline"}


def _b64(root: bytes) -> str:
    return base64.b64encode(root).decode("ascii")


def _gate_presence(kind: str, actor: str, payload: Dict[str, Any]) -> Optional[str]:
    if kind == "invite":
        return payload.get("id")
    if kind in ("briefed", "reinvite"):
        return payload.get("presence")
    return actor


def _stand_in(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """What a gate entry may carry in a copy: who someone is, as participants see them, and nothing
    they said at the gates, nothing about what their seat costs, and no note to them."""
    if kind == "invite":
        return {k: payload[k] for k in ("id", "name", "hails_from", "people", "invited_by") if payload.get(k) is not None}
    if kind == "accept_invitation":
        ident = payload.get("identity")
        if isinstance(ident, dict):
            return {"identity": {k: ident[k] for k in ("name", "hails_from", "people") if ident.get(k)}}
        return {}
    if kind == "briefed":
        return {"presence": payload.get("presence")}
    if kind == "reinvite":
        return {"presence": payload.get("presence"), "requested": bool(payload.get("requested"))}
    if kind == "opt_in" and payload.get("returning"):
        return {"returning": True}
    return {}


def copy_plan(rows: List[Dict[str, Any]], for_pid: Optional[str] = None) -> Tuple[Dict[int, Any], RoomState]:
    """How each row travels in a steward's copy: "full", ("as", kind, stand-in), or None (its leaf
    alone). Worked out from the whole transcript, since whether someone entered is known only later.
    `for_pid` is whose copy it is: the steward the field is moving to is given what may go with it."""
    st = RoomState()
    entered_before: Dict[int, bool] = {}     # a decline: had its presence entered before it?
    evs = []
    for r in rows:
        ev = {"id": r["id"], "ts": r["ts"], "actor": r["actor"], "kind": r["kind"], "payload": json.loads(r["body"])}
        if ev["kind"] == "decline":
            pr = st.presences.get(ev["actor"])
            entered_before[ev["id"]] = bool(pr and pr.joined_at is not None)
        st.apply(ev)
        evs.append(ev)
    entered = {pid for pid, p in st.presences.items() if p.joined_at is not None}
    going = st.travel_ready() if st.moving and for_pid and st.moving["to"] == for_pid else {"circles": set(), "mine": set()}
    plan: Dict[int, Any] = {}
    for ev in evs:
        k, p, eid = ev["kind"], ev["payload"], ev["id"]
        sc = st.scoped.get(eid) or {}
        if k == WITHHELD:                    # a carried-on field's own copy of something held back
            plan[eid] = ("as", p["as"], p.get("stand_in") or {}) if p.get("as") else None
        elif eid in st.scoped and (sc.get("circle") in going["circles"] or sc.get("journal") in going["mine"]):
            plan[eid] = "full"               # it goes with the field, with the yes of those it belongs to
        elif k in ("follow", "unfollow", "wake_pref", "pause") and ev["actor"] in going["mine"] and eid not in st.scoped:
            plan[eid] = "full"               # a participant's own choices, with their yes
        elif eid in st.scoped or k in FINGERPRINT_ONLY:
            plan[eid] = None
        elif k == "unparsed" and p.get("phase"):
            plan[eid] = None                 # a gate's unreadable answer
        elif k == "pause" and not p.get("note"):
            plan[eid] = None                 # "Without a note, a pause is written nowhere anyone reads."
        elif k in GATES:
            if k == "decline" and entered_before.get(eid):
                plan[eid] = "full"           # a former member saying no to returning: members see why they left
            elif _gate_presence(k, ev["actor"], p) in entered:
                plan[eid] = ("as", k, _stand_in(k, p))
            else:
                plan[eid] = None             # someone who never entered: members never knew
        else:
            plan[eid] = "full"
    return plan, st


def _wire(r: Dict[str, Any], form: Any) -> Dict[str, Any]:
    if form == "full":
        return {k: r[k] for k in ("id", "ts", "actor", "kind", "body", "payload_hash", "leaf")}
    if form:
        _, kind, stand = form
        return {"id": r["id"], "ts": r["ts"], "actor": r["actor"], "kind": WITHHELD,
                "body": json.dumps({"as": kind, "stand_in": stand}, sort_keys=True, ensure_ascii=False),
                "payload_hash": "", "leaf": r["leaf"]}
    return {"id": r["id"], "ts": 0.0, "actor": "", "kind": WITHHELD, "body": "{}", "payload_hash": "", "leaf": r["leaf"]}


def serve_copy(log: EventLog, since: int, held: List[int], page: int = PAGE, for_pid: Optional[str] = None) -> Dict[str, Any]:
    """What a steward's copy is sent: the tree as it stood at the copy's last row (so the copy can
    check nothing it holds has changed), the next rows, anything held back before that may now be
    held, and the tree as it stands after them."""
    rows = log.rows()
    plan, _ = copy_plan(rows, for_pid)
    frontier: List[Tuple[int, bytes]] = []
    n_since, root_since = 0, _root([])
    new: List[Dict[str, Any]] = []
    n_after, root_after = 0, None
    for r in rows:
        _push(frontier, bytes.fromhex(r["leaf"]))
        if r["id"] <= since:
            n_since, root_since = len_of(frontier), _root(frontier)
        elif len(new) < page:
            new.append(r)
            n_after, root_after = len_of(frontier), _root(frontier)
    if root_after is None:
        n_after, root_after = n_since, root_since
    by_id = {r["id"]: r for r in rows}
    released = [_wire(by_id[i], plan[i]) for i in held if i in by_id and plan.get(i) is not None]
    return {"origin": log.origin(), "witnessed_from": log.witnessed_from(),
            "since": {"size": n_since, "root": _b64(root_since)},
            "rows": [_wire(r, plan[r["id"]]) for r in new], "released": released,
            "after": {"size": n_after, "root": _b64(root_after), "upto": new[-1]["id"] if new else since},
            "more": bool(new) and new[-1]["id"] < rows[-1]["id"]}


def len_of(frontier: List[Tuple[int, bytes]]) -> int:
    return sum(n for n, _ in frontier)


def _bad(r: Dict[str, Any]) -> Optional[str]:
    """Why a row given in full does not match its own leaf, or None."""
    if r["kind"] == WITHHELD:
        return None
    try:
        body = json.loads(r["body"])
    except (TypeError, ValueError):
        return f"#{r['id']} is not readable"
    if body != ERASED and payload_hash(r["body"]) != r["payload_hash"]:
        return f"the words of #{r['id']} do not match their fingerprint"
    if leaf_hash(r["id"], r["ts"], r["actor"], r["kind"], r["payload_hash"]).hex() != r["leaf"]:
        return f"#{r['id']} does not match its leaf"
    return None


def _post(link: str, body: Dict[str, Any], timeout: float = 60.0, what: str = "copy.json") -> Dict[str, Any]:
    url = link.rstrip("/") + "/" + what
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "hope-steward"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sync(link: str, log: EventLog, fetch: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Bring a steward's copy up to date, checking it as it goes. Nothing already held is ever
    overwritten: if the field's record of what the copy holds has changed, the copy stops there and
    says so. Returns what happened."""
    fetch = fetch or _post
    if log.meta("carried_on"):
        return {"ok": False, "problem": "this copy has been carried on: it is the field's home now, not a copy", "upto": log.last_id()}
    held = [r["id"] for r in log.rows() if r["kind"] == WITHHELD and not json.loads(r["body"]).get("as")]
    added = filled = 0
    while True:
        size, root, upto = log.size_and_root()
        try:
            got = fetch(link, {"since": upto, "held": held})
        except urllib.error.HTTPError as e:            # it answered, and refused: it is not gone
            log.set_meta("answered_ts", str(time.time()))
            try:
                why = json.loads(e.read().decode("utf-8")).get("error")
            except (ValueError, OSError):
                why = None
            return {"ok": False, "problem": why or f"the field's machine refused: {e}", "upto": upto}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "unreachable": True, "problem": f"the field's machine did not answer: {e}", "upto": upto}
        if not got.get("ok", True):
            return {"ok": False, "problem": got.get("error") or "the field refused", "upto": upto}
        log.set_meta("answered_ts", str(time.time()))      # the field's machine answered: it is not gone
        if size == 0 and not log.meta("steward"):
            log.set_meta("origin", got["origin"])
            log.set_meta("witnessed_from", str(got.get("witnessed_from") or 1))
        if got.get("you"):
            log.set_meta("steward", got["you"])
        log.set_meta("host", link)
        if got["origin"] != log.origin():
            return {"ok": False, "problem": "this copy is of another field", "upto": upto}
        if got["since"]["size"] != size or got["since"]["root"] != _b64(root):
            return {"ok": False, "changed": True, "upto": upto, "mine": fingerprint_text(root),
                    "theirs": fingerprint_text(base64.b64decode(got["since"]["root"])),
                    "problem": (f"the field's record no longer matches your copy up to #{upto}: your copy's fingerprint "
                                f"is {fingerprint_text(root)}, the field's machine now gives "
                                f"{fingerprint_text(base64.b64decode(got['since']['root']))}. Your copy is kept as it "
                                f"was; nothing in it was overwritten. You may tell the field.")}
        rows = got["rows"]
        last = upto
        for r in rows:
            why = _bad(r)
            if why or r["id"] <= last:
                return {"ok": False, "problem": why or f"#{r['id']} came out of order", "upto": upto}
            last = r["id"]
        log.put(rows)
        for r in got.get("released") or []:
            if not _bad(r) and log.fill(r):
                filled += 1
        held = []
        size2, root2, upto2 = log.size_and_root()
        if size2 != got["after"]["size"] or _b64(root2) != got["after"]["root"]:
            return {"ok": False, "problem": "what the field's machine sent does not add up to the fingerprint it gives",
                    "upto": upto2}
        added += len(rows)
        if not got.get("more"):
            break
    st = replay(log.iter())
    kept = [json.loads(r["body"]) for r in log.rows() if r["kind"] == WITHHELD]
    me = log.meta("steward")
    return {"ok": True, "added": added, "filled": filled, "upto": upto2, "size": size2,
            "fingerprint": fingerprint_text(root2), "held_as_fingerprints": sum(1 for b in kept if not b.get("as")),
            "stand_ins": sum(1 for b in kept if b.get("as")),
            "moved_to_you": bool(st.moved and st.moved.get("to") == me),
            "moved": st.moved, "steward": me}


def report_handover(link: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Tell the field's machine that this steward's machine has taken the field on, up to an entry."""
    try:
        return _post(link, body, what="handover")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return {"ok": False, "error": f"the field's machine refused: {e}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "unreachable": True, "error": f"the field's machine did not answer: {e}"}


def seat_people(st: RoomState, rv) -> List[Tuple[str, str]]:
    """On the machine the field was carried on to: a new seat link for every person who was a
    participant, for the steward to send them. A seat under the same id finds the same participant."""
    from .connector import Seat
    out = []
    for p in st.presences.values():
        if p.id.startswith("remote__") and p.joined_at is not None and p.state != "OUT":
            token = rv.add_seat(Seat(id=p.id, name=p.name, hails_from=p.hails_from, people=p.people, model="remote",
                                     pricing={"prompt": 0.0, "completion": 0.0}))
            out.append((p.name, f"/seat/{token}/"))
    return out


class Links:
    """The stewards' links, in a file beside the transcript, never in it. Whoever holds a link reads
    that steward's copy, so each is shown only in its steward's own view, and is dropped when their
    stewardship ends."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._tokens: Dict[str, str] = {}
        try:
            with open(path, encoding="utf-8") as f:
                self._tokens = dict(json.load(f).get("tokens") or {})
        except (OSError, ValueError):
            self._tokens = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tokens": self._tokens}, f)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def token(self, pid: str) -> str:
        with self._lock:
            if pid not in self._tokens:
                self._tokens[pid] = secrets.token_urlsafe(24)
                self._save()
            return self._tokens[pid]

    def revoke(self, pid: str) -> None:
        with self._lock:
            if self._tokens.pop(pid, None) is not None:
                self._save()

    def pid(self, token: str) -> Optional[str]:
        found = None
        with self._lock:
            for pid, tok in self._tokens.items():      # every one compared, in constant time
                if token and hmac.compare_digest(tok, token):
                    found = pid
        return found
