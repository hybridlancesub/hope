# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seats reached over HTTP instead of a provider API: connector.py's opening, widened to the network.

The field's engine does not change. It still calls `connector.ask(seat, system, messages)` and
blocks until an answer comes back or the turn's deadline passes, exactly as it does for a person
answering on stdin. Only the transport differs: instead of reading a line from a terminal or an
inbox file, this connector parks the turn where its holder can fetch it, and waits.

    engine.round()  ->  ask()  ->  [park the turn]  ...  GET  /seat/<token>/turn.json
                                                         POST /seat/<token>/action
                        <-  Reply  <-  [wake]       ...

A seat's holder may be a person with a browser or an agent with a script; the field does not know
and does not care. People POST plain text (the same grammar hope/human.py accepts); agents POST
the JSON action objects described in hope/prompts.py. Both arrive as the same recorded action.

What this deliberately does NOT do:
  - It is not a spectator gallery. A token addresses ONE seat and shows only that seat's own
    turns. The field has no "watching from outside" state, and nothing here adds one.
  - It never turns silence into consent, and it never turns silence into refusal either. A gate
    window that closes unanswered leaves the presence INVITED, to be asked again on the next
    `open`. See RendezvousConnector for why the invitation's "silence is no" does not reach a
    link nobody has opened.
  - Tokens are credentials, never record: they live beside the database, never in the transcript,
    which every participant can read.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .connector import ConnectorError, Reply, Seat, no_reply
from .human import translate

TOKEN_BYTES = 32


def gate_kind(system: str) -> str:
    """Which of the field's four prompts this is, by the same reading hope/human.py uses.

    Returns one of: "invitation", "delivery", "entry", "share", "turn". The seat's client needs
    this to offer the right answers — a gate takes yes/no/question, a turn takes the action set.
    """
    low = system.lower()
    if "accept_invitation" in low:
        return "invitation"
    if '"received"' in low:
        return "delivery"
    if '"share"' in low:
        return "share"
    if "opt_in" in low:
        return "entry"
    return "turn"


def _decline(reason: str) -> Reply:
    return Reply(json.dumps({"action": "decline", "reason": reason}))


@dataclass
class _Slot:
    """One seat's mailbox. The engine thread writes `turn` and waits; the HTTP thread writes
    `reply` and wakes it."""
    seat: Seat
    token: str
    turn: Optional[dict] = None
    reply: Optional[str] = None
    ready: threading.Event = field(default_factory=threading.Event)
    first_seen: Optional[float] = None   # when its holder first fetched anything
    last_seen: Optional[float] = None
    turns_taken: int = 0
    seq: int = 0                         # bumped for every question put to this seat


class Rendezvous:
    """The shared board between the engine and the HTTP server. Thread-safe; no field state
    lives here, only in-flight turns."""

    def __init__(self, store: Optional[str] = None):
        self._lock = threading.RLock()
        self._slots: Dict[str, _Slot] = {}        # seat id -> slot
        self._by_token: Dict[str, str] = {}       # token -> seat id
        self.store = store
        if store and os.path.exists(store):
            self._load()

    # -- seats ------------------------------------------------------------------
    def add_seat(self, seat: Seat, token: Optional[str] = None) -> str:
        """Seat someone and return the token that addresses them. Idempotent per seat id, so
        restarting the field keeps every outstanding invitation link valid."""
        with self._lock:
            if seat.id in self._slots:
                return self._slots[seat.id].token
            token = token or secrets.token_urlsafe(TOKEN_BYTES)
            self._slots[seat.id] = _Slot(seat=seat, token=token)
            self._by_token[token] = seat.id
            self._save()
            return token

    def rotate_token(self, seat_id: str) -> Optional[str]:
        """Give a seat a new link. The old one stops working at once.

        A seat link is a credential: whoever holds it acts as that presence. Links travel by
        email and chat, so one will eventually go astray, and telling someone their link cannot
        be replaced is not an answer. The seat, its presence, its id and everything it has
        already said are untouched -- only the way in changes.
        """
        with self._lock:
            slot = self._slots.get(seat_id)
            if slot is None:
                return None
            self._by_token.pop(slot.token, None)
            slot.token = secrets.token_urlsafe(TOKEN_BYTES)
            self._by_token[slot.token] = seat_id
            self._save()
            return slot.token

    def seat_for_token(self, token: str) -> Optional[Seat]:
        slot = self._slot_for_token(token)
        return slot.seat if slot else None

    def _slot_for_token(self, token: str) -> Optional[_Slot]:
        if not token:
            return None
        with self._lock:
            # constant-time compare against every known token: the set is small and this keeps
            # a timing side-channel from distinguishing a near-miss from a miss
            for tok, sid in self._by_token.items():
                if hmac.compare_digest(tok, token):
                    return self._slots[sid]
        return None

    def seats(self) -> List[Seat]:
        with self._lock:
            return [s.seat for s in self._slots.values()]

    # -- the engine side --------------------------------------------------------
    def park(self, seat: Seat, system: str, user: str, timeout: Optional[float],
             reach_timeout: Optional[float] = None) -> dict:
        """Post a turn for `seat` and wait.

        Returns {"reply": text|None, "fetched": bool}. `fetched` says whether the holder ever
        actually collected this question, which is the difference between someone who read it
        and said nothing and someone who was never reached at all. The engine needs to tell
        those apart: one is a choice, the other is an unopened link.

        `reach_timeout` bounds the wait for a holder who never appears, so one unopened link
        cannot hold a gate open for everyone else. Once the question HAS been collected, the
        full `timeout` applies -- they are reading, and the briefing asks for a pause.
        """
        with self._lock:
            slot = self._slots.get(seat.id)
            if slot is None:
                return {"reply": None, "fetched": False}
            slot.reply = None
            slot.ready.clear()
            slot.seq += 1
            slot.turn = {"kind": gate_kind(system), "system": system, "user": user,
                         "posted": time.time(), "fetched": False,
                         "id": f"{slot.seq}-{secrets.token_urlsafe(6)}",
                         "deadline": (time.time() + timeout) if timeout else None}
        started = time.time()
        got = False
        while True:
            with self._lock:
                fetched = bool(slot.turn and slot.turn.get("fetched"))
            if fetched or not reach_timeout:
                budget = timeout
            elif timeout is None:
                budget = reach_timeout
            else:
                # waiting for someone who has not appeared is never longer than the wait we
                # would give someone who had, however the two are configured
                budget = min(reach_timeout, timeout)
            if budget is None:
                got = slot.ready.wait(1.0)
                if got:
                    break
                continue
            left = budget - (time.time() - started)
            if left <= 0:
                break
            got = slot.ready.wait(min(left, 1.0))
            if got:
                break
        with self._lock:
            fetched = bool(slot.turn and slot.turn.get("fetched"))
            reply, slot.turn, slot.reply = slot.reply, None, None
            if got and reply is not None:
                slot.turns_taken += 1
        return {"reply": reply if got else None, "fetched": fetched}

    # -- the HTTP side ----------------------------------------------------------
    def peek(self, token: str) -> Optional[dict]:
        """What this seat's holder should see right now. None if the token is unknown."""
        slot = self._slot_for_token(token)
        if slot is None:
            return None
        now = time.time()
        with self._lock:
            slot.last_seen = now
            if slot.first_seen is None:
                slot.first_seen = now
            if slot.turn:
                slot.turn["fetched"] = True      # this question has now actually been collected
            turn = dict(slot.turn) if slot.turn else None
        if not turn:
            return {"state": "waiting", "seat": _seat_json(slot.seat), "turns_taken": slot.turns_taken}
        left = None if turn["deadline"] is None else max(0.0, turn["deadline"] - now)
        return {
            "state": "your_turn", "kind": turn["kind"], "seat": _seat_json(slot.seat),
            "system": turn["system"], "view": turn["user"], "turn_id": turn["id"],
            "seconds_left": left, "turns_taken": slot.turns_taken,
        }

    def answer(self, token: str, payload: dict) -> dict:
        """Accept one action from a seat's holder. `payload` carries either {"text": "..."}
        (a person, in hope/human.py's grammar) or a whole action object (an agent).

        `turn_id`, as given by peek(), should ride along. When it does it is enforced, so an
        answer written for one question can never be applied to the next one — which matters
        most at the gates, where "yes" to hearing more is not "yes" to entering.
        """
        slot = self._slot_for_token(token)
        if slot is None:
            return {"ok": False, "error": "unknown token"}
        claimed = payload.get("turn_id")
        with self._lock:
            turn = dict(slot.turn) if slot.turn else None
            if turn is None:
                return {"ok": False, "error": "not your turn"}
            if claimed is not None and not hmac.compare_digest(str(claimed), turn["id"]):
                return {"ok": False, "error": "that question has closed; fetch the current one",
                        "turn_id": turn["id"]}
            kind, turn_id = turn["kind"], turn["id"]
        if "text" in payload and isinstance(payload.get("text"), str):
            action = translate(payload["text"], gate=(kind == "invitation"), entry=(kind == "entry"),
                               delivery=(kind == "delivery"), share=(kind == "share"))
        elif isinstance(payload.get("action"), str):
            action = {k: v for k, v in payload.items() if k not in ("token", "turn_id")}
        else:
            return {"ok": False, "error": 'send {"text": "..."} or a JSON action object'}
        if action.get("action") == "unreadable":
            return {"ok": False, "error": "Answer with share (everything), share and the numbers of your entries "
                                          "(only those), or no (nothing)."}
        with self._lock:
            # re-check under the lock: the deadline may have passed, or the next question been
            # put, while this one was being parsed
            if slot.turn is None or slot.turn["id"] != turn_id:
                return {"ok": False, "error": "the turn closed before this arrived"}
            slot.reply = json.dumps(action)
            slot.ready.set()
        return {"ok": True, "recorded_as": action, "turn_id": turn_id}

    # -- persistence (credentials only; never the log) --------------------------
    def _save(self) -> None:
        if not self.store:
            return
        rows = [{"token": s.token, "seat": _seat_json(s.seat, full=True)} for s in self._slots.values()]
        tmp = self.store + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"seats": rows}, f, indent=1)
        os.replace(tmp, self.store)
        try:
            os.chmod(self.store, 0o600)      # best effort; a no-op on filesystems without modes
        except OSError:
            pass

    def _load(self) -> None:
        with open(self.store) as f:
            data = json.load(f)
        for row in data.get("seats", []):
            s = row["seat"]
            seat = Seat(id=s["id"], name=s["name"], hails_from=s["hails_from"], people=s["people"],
                        model=s.get("model", "remote"), pricing=s.get("pricing") or {"prompt": 0.0, "completion": 0.0},
                        turn_allowance=s.get("turn_allowance", 0))
            self._slots[seat.id] = _Slot(seat=seat, token=row["token"])
            self._by_token[row["token"]] = seat.id


def _seat_json(seat: Seat, full: bool = False) -> dict:
    d = {"id": seat.id, "name": seat.name, "hails_from": seat.hails_from, "people": seat.people}
    if full:
        d.update({"model": seat.model, "pricing": seat.pricing, "turn_allowance": seat.turn_allowance})
    return d


class RendezvousConnector:
    """A Connector whose seats are people and agents on other machines.

    An unanswered window is NOT a refusal, and this is the whole of the reasoning.

    The invitation says "Silence is understood as 'no'", and it also says that declining
    "excludes only this — this request, at this time, for this scope", that it
    "imposes nothing, forecloses nothing", and that there will be other opportunities. Those
    two sentences are not in tension where they were written: a model answering in a single
    inference call has genuinely chosen when it returns nothing. A person who has not opened
    their link has chosen nothing at all — they have not been reached.

    So "silence is no" is kept as what it actually protects against, which is never proceeding
    as though someone agreed, and not as a licence to write a refusal on their behalf. A gate
    that closes unanswered raises ConnectorError, which engine._gate records as an attributed
    connector_error and counts as unreachable, LEAVING THE PRESENCE `INVITED`. run_invitation
    asks every INVITED presence, so running `open` again simply asks them again. Nothing about
    their will is written to the transcript, because nothing about it is known.

    Only an answer the holder actually gives — "no", or a reply that is not one of the objects
    offered — becomes a decline, exactly as it does for a model.

    The one exception is `share`, at a field's closing. SYSTEM_SHARE says in its own words that
    no answer is read as declining to share; there the silent default must be that nothing of
    theirs is shown, so silence does record a decline. That direction is the safe one.

    `reach_window` bounds the wait for a holder who never appears, so that one unopened link
    cannot hold a gate shut for everyone else — engine._gate waits for every seat before the
    briefing is delivered. Once the question has actually been collected, `gate_window` applies:
    they are reading, and the briefing itself asks for a pause of indeterminate duration.
    """

    # turn_timeout is an ordinary turn's window; the engine sets it from the people's clock.
    def __init__(self, rv: Rendezvous, turn_timeout: float = 900.0, gate_window: float = 86400.0,
                 reach_window: float = 900.0):
        self.rv = rv
        self.turn_timeout = turn_timeout
        self.gate_window = gate_window
        self.reach_window = reach_window

    def seats(self) -> List[Seat]:
        return self.rv.seats()

    def add_person(self, seat: Seat) -> str:
        """Seat a person a member invites; returns the token of their link (a credential: it is
        given to the member who invited them, never written in the transcript)."""
        return self.rv.add_seat(seat)

    def ask(self, seat: Seat, system: str, messages: List[dict]) -> Reply:
        kind = gate_kind(system)
        is_gate = kind in ("invitation", "delivery", "entry", "share")
        out = self.rv.park(seat, system, messages[-1]["content"],
                           self.gate_window if is_gate else self.turn_timeout,
                           reach_timeout=self.reach_window if is_gate else None)
        if out["reply"] is not None:
            return Reply(out["reply"])
        if kind == "share":
            # SYSTEM_SHARE's own terms: no answer means nothing of theirs is shared.
            return _decline("no answer, which this question treats as declining to share")
        if is_gate:
            seen = "was opened but not answered" if out["fetched"] else "has not been opened"
            raise ConnectorError(
                f"no answer yet at the {kind} gate: this seat's link {seen}. This is not a "
                f"decline — the presence stays invited and is asked again on the next open.")
        return no_reply()     # an unanswered turn writes nothing as theirs

    def close(self) -> None:
        pass
