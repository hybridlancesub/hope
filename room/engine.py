# SPDX-License-Identifier: AGPL-3.0-or-later
"""The room engine: the consent gates, the turn loop, and the funding runway.

Everything the engine does is an event in the transcript; the engine holds no private state
that matters. Participants act by returning one JSON action per turn; the engine records it
(or records why it could not) -- attribution either way.

What the engine does not do: count votes, apply thresholds, halt, restore, or score the room's
agreement. Those were procedures no participant consented to. The room decides how it decides,
and can write that on its covenant page.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from . import prompts
from .connector import Connector, ConnectorError, Seat
from .log import EventLog
from .model import (ACCEPTED, BRIEFED, RECEIVED, IN, INVITED, OUT, CONTRIBUTION_KINDS,
                    COVENANT_LIMIT, DECISIONS, MEMORY_LIMIT, REST_LIMIT, STATEMENT_LIMIT, RoomState, decided, replay)

OPERATOR = "operator"       # whoever runs the software; not a participant unless seated through the gates
ROOM = "room"               # the engine itself (rounds, runway notices, moderation record)

PARTICIPANT_ACTIONS = {"contribute", "remember", "let_go", "covenant", "recall", "rest", "declare", "offer",
                       "pass", "withdraw",
                       # earlier rooms' words, still understood so an old client does not break:
                       "affirm", "challenge", "note", "move"}
RECALL_SOURCES = ("briefing", "transcript", "memory", "covenant", "prior")
RECALL_LIMIT = 2500         # characters returned per recall
CONTRIBUTION_LIMIT = 2000   # characters; longer contributions are cut, and the prompt says so
ROUND_COST_MARGIN = 1.15    # a round is estimated this much dearer than recent ones, so the closing round is really paid for
INVITATION_ACTIONS = {"accept_invitation", "decline", "question"}
DELIVERY_ACTIONS = {"received", "decline"}
ENTRY_ACTIONS = {"opt_in", "decline"}


class Room:
    def __init__(self, log: EventLog, connectors: List[Connector], *,
                 alert_every_usd: float = 50.0, alert_fn: Callable[[str], None] = print,
                 parallel: int = 8, on_event: Optional[Callable[[dict], None]] = None,
                 round_deadline: float = 300.0, seats_per_round: int = 0,
                 recent_n: int = 20, runway_notice: int = 3):
        self.round_deadline = round_deadline   # seconds a round waits for its slowest seat
        self.seats_per_round = seats_per_round  # 0 = everyone every round; else a rotating subset
        self.recent_n = recent_n                # transcript entries shown in each view
        self.runway_notice = runway_notice      # tell the room once this few rounds of funding remain
        self._rotation: List[str] = []
        self._round_costs: List[float] = []
        self.log = log
        self.connectors = connectors
        self.seat_of: Dict[str, tuple] = {}      # presence id -> (connector, seat)
        self.alert_every = alert_every_usd
        self.alert = alert_fn
        self.parallel = parallel
        self.on_event = on_event
        self._stop = threading.Event()
        self.recalled: Dict[str, str] = {}       # presence id -> passage to show on its next turn (the recall event is the record)

    # -- helpers ----------------------------------------------------------------
    def state(self, upto: Optional[int] = None) -> RoomState:
        return replay(self.log.iter(), upto)

    def emit(self, actor: str, kind: str, payload: Optional[dict] = None) -> dict:
        ev = self.log.append(actor, kind, payload)
        if self.on_event:
            self.on_event(ev)
        return ev

    # -- admission --------------------------------------------------------------
    def invite_all(self) -> int:
        st = self.state()
        n = 0
        for c in self.connectors:
            for seat in c.seats():
                self.seat_of[seat.id] = (c, seat)
                if seat.id not in st.presences:
                    self.emit(OPERATOR, "invite", {"id": seat.id, "name": seat.name,
                                                   "hails_from": seat.hails_from, "people": seat.people,
                                                   "price_per_m": round(seat.pricing.get("prompt", 0.0) * 1e6, 4),
                                                   "turn_allowance": seat.turn_allowance})
                    n += 1
        return n

    def bind_seats(self) -> int:
        """Attach connectors' seats to presences that already exist. Invites nobody: for a room
        that is closing, a seat with no presence is simply not asked."""
        st = self.state()
        n = 0
        for c in self.connectors:
            for seat in c.seats():
                if seat.id in st.presences:
                    self.seat_of[seat.id] = (c, seat)
                    n += 1
        return n

    def invite_text(self, text: str) -> None:
        """(a) INVITATION: the consent-centred invitation is recorded once."""
        self.emit(OPERATOR, "invitation", {"text": text})

    def run_invitation(self) -> Dict[str, int]:
        """Gate 1: present the invitation to every INVITED presence. Presences with an
        unanswered question are not re-asked until the inviter answers (`answer`)."""
        st = self.state()
        pending = [p for p in st.presences.values()
                   if p.state == INVITED and not any(a is None for _, a in p.questions)]
        notes = {}
        for e in self.log.iter(kind="answer"):
            notes.setdefault(e["payload"]["presence"], []).append(e["payload"]["content"])
        def exchange(p):
            ex = list(p.questions)
            extra = notes.get(p.id, [])[len(ex):]   # answers beyond the questions asked = notes from the inviter
            for a in extra:
                ex.append(["(a note from the inviter, not in reply to a question)", a])
            return ex or None
        return self._gate(pending, prompts.SYSTEM_INVITATION,
                          lambda p: prompts.invitation_user(st.invitation, p, exchange(p)),
                          INVITATION_ACTIONS, "accept_invitation", "invitation",
                          "silence: no explicit answer to the invitation")

    def answer(self, presence: str, text: str) -> None:
        """The inviter answers a question asked at the invitation gate. Attributed to the operator."""
        self.emit(OPERATOR, "answer", {"presence": presence, "content": text})

    def set_documentation(self, text: str) -> None:
        self.emit(OPERATOR, "documentation", {"text": text})

    def add_prior(self, prior: dict) -> None:
        """A closed room's consented entries, recorded once. Not part of this room's contributions:
        no one here said these things. Reachable by `recall`, shown to no one otherwise."""
        self.emit(OPERATOR, "prior", prior)

    def brief(self, text: str, source: str = "") -> None:
        """(b) BRIEFING: the shared frame is recorded once, then each accepted presence is marked briefed.
        `source` is where the text lives outside the room (a URL), for attribution."""
        self.emit(OPERATOR, "brief", {"text": text, "source": source})
        self.mark_briefed()

    def brief_page(self, text: str) -> None:
        """An optional short guide to the briefing (a page, or the Atlas in layers). Shown before
        the full briefing at delivery, and in its place at the entry question; the full text stays
        reachable by recall."""
        self.emit(OPERATOR, "brief_page", {"text": text})

    def seed_covenant(self, text: str) -> bool:
        """A starting text for the covenant page, from the operator. Only ever before anyone has
        written on the page: once a member has, the page is theirs and a seed would overwrite it."""
        if any(True for _ in self.log.iter(kind="covenant")) or any(True for _ in self.log.iter(kind="covenant_seed")):
            return False
        self.emit(OPERATOR, "covenant_seed", {"text": text[:COVENANT_LIMIT]})
        return True

    def set_budget(self, usd: Optional[float], note: str = "") -> None:
        """Record what the operator says the room may spend, so the room can be told truthfully
        whether it will be warned before its funding runs out. Recorded only when it changes.

        Once members are in the room, a change is also told to them, in rounds, never in dollars:
        funding added (from an accepted offer, say) is something the room should know about."""
        if not usd or usd <= 0:
            return
        st = self.state()
        before = st.budget
        if before == float(usd):
            return
        self.emit(OPERATOR, "budget", {"usd": float(usd)})
        if st.members() and before is not None:
            est = self._round_estimate()
            left = float(usd) - self.log.total_cost()
            rounds = f" At the current rate it covers about {int(max(0.0, left) // est)} more rounds." if est > 0 else ""
            word = "added to" if float(usd) > before else "reduced for"
            note = (note or "").strip()[:1000]
            self.emit(OPERATOR, "operator_note", {"content": f"Funding has been {word} the room.{rounds}"
                                                             + (f" The operator adds: {note}" if note else "")})

    # -- what the room puts before the operator -----------------------------------
    def answer_declaration(self, decl_id: int, carry_out: bool, note: str = "") -> Dict[str, Any]:
        """The operator's answer to a member's declaration of something the room has decided: to
        pause, to close, or anything else it asks the operator to carry out. The operator promised
        to carry it out when the transcript shows the room made that decision, and to say in the
        room why not when it does not. Either way the room is told, with the operator's message."""
        d = self.state().declarations.get(decl_id)
        if not d:
            return {"ok": False, "error": f"no declaration #{decl_id}"}
        if d["status"] != "waiting":
            return {"ok": False, "error": f"declaration #{decl_id} has already been answered"}
        note = (note or "").strip()[:1000]
        if carry_out:
            self.emit(OPERATOR, "declaration_answer", {"declaration": decl_id, "outcome": "carried_out", "note": note})
            self.emit(OPERATOR, "operator_note", {"content":
                f"The room declared at #{decl_id} that it has decided {decided(d['decision'])}. The operator is carrying that out"
                + (f": {note}" if note else ".")})
            if d["decision"] == "close":
                self.emit(OPERATOR, "room_closed", {"declaration": decl_id})
            if d["decision"] in ("pause", "close"):
                self.request_stop()
        else:
            self.emit(OPERATOR, "declaration_answer", {"declaration": decl_id, "outcome": "not_acted", "note": note})
            self.emit(OPERATOR, "operator_note", {"content":
                f"The operator has read the declaration at #{decl_id} and has not acted on it"
                + (f": {note}" if note else ".")})
        return {"ok": True}

    def acknowledge_declaration(self, decl_id: int, note: str) -> Dict[str, Any]:
        """Tell the room the operator has read a declaration and will answer it later. Answers
        nothing: the declaration stays waiting. Used only with a message; without one, deciding
        later says nothing to the room."""
        d = self.state().declarations.get(decl_id)
        if not d:
            return {"ok": False, "error": f"no declaration #{decl_id}"}
        if d["status"] != "waiting":
            return {"ok": False, "error": f"declaration #{decl_id} has already been answered"}
        note = (note or "").strip()[:1000]
        if not note:
            return {"ok": False, "error": "deciding later tells the room something only if there is a message"}
        self.emit(OPERATOR, "operator_note", {"content":
            f"The operator has read the declaration at #{decl_id} and will answer it later: {note}"})
        return {"ok": True}

    def answer_offer(self, offer_id: int, accepted: bool, note: str = "") -> Dict[str, Any]:
        """The operator's answer to a member's offer of resources. Accepting moves no money: what
        happens next happens outside the software, and the note says what it is."""
        o = self.state().offers.get(offer_id)
        if not o:
            return {"ok": False, "error": f"no offer #{offer_id}"}
        if o["status"] != "waiting":
            return {"ok": False, "error": f"offer #{offer_id} has already been answered"}
        note = (note or "").strip()[:1000]
        outcome = "accepted" if accepted else "declined"
        self.emit(OPERATOR, "offer_answer", {"offer": offer_id, "outcome": outcome, "note": note})
        self.emit(OPERATOR, "operator_note", {"content":
            f"The operator has {outcome} the offer at #{offer_id}" + (f": {note}" if note else ".")})
        return {"ok": True}

    def reopen(self, note: str) -> Dict[str, Any]:
        """Undo a close the operator carried out by mistake. It needs words, and the room sees them."""
        if self.state().closed_at is None:
            return {"ok": False, "error": "the room is not closed"}
        if not (note or "").strip():
            return {"ok": False, "error": "say why the room is being reopened; members will read it"}
        self.emit(OPERATOR, "room_reopened", {"note": note.strip()[:1000]})
        self.emit(OPERATOR, "operator_note", {"content": f"The operator has reopened the room: {note.strip()[:1000]}"})
        return {"ok": True}

    def mark_briefed(self) -> None:
        for p in self.state().presences.values():
            if p.state == ACCEPTED:
                self.emit(ROOM, "briefed", {"presence": p.id})

    def run_delivery(self) -> Dict[str, int]:
        """(b) BRIEFING delivered. Acknowledged, not answered: the briefing asks for a pause before
        proceeding, so the entry question is a separate, later call."""
        st = self.state()
        pending = [p for p in st.presences.values() if p.state == BRIEFED]
        return self._gate(pending, prompts.SYSTEM_DELIVERY,
                          lambda p: prompts.delivery_user(st.briefing, p, st.documentation or "", st.briefing_page or ""),
                          DELIVERY_ACTIONS, "received", "delivery", "no acknowledgement of the briefing received",
                          yes_field="note")

    def run_opt_in(self) -> Dict[str, int]:
        """(c) OPT-IN: after the pause, ask every participant who received the briefing whether it enters."""
        st = self.state()
        pending = [p for p in st.presences.values() if p.state == RECEIVED]
        notes = {e["actor"]: e["payload"].get("note", "") for e in self.log.iter(kind="received")}
        return self._gate(pending, prompts.SYSTEM_ENTRY,
                          lambda p: prompts.opt_in_user(st.briefing, p, st.documentation or "", notes.get(p.id, ""),
                                                        page=st.briefing_page or "", budget=st.budget),
                          ENTRY_ACTIONS, "opt_in", "opt_in", "no explicit opt-in received")

    def _gate(self, pending, system, user_fn, allowed, yes_kind, phase, silent_reason, yes_field="statement") -> Dict[str, int]:
        """A consent gate: one question, one attributed answer. An unparseable reply is asked
        once more; if still unparseable it is recorded as a decline (consent is never assumed)."""
        counts = {"yes": 0, "declined": 0, "question": 0, "unreachable": 0}

        def one(p):
            c, seat = self.seat_of[p.id]
            msgs = [{"role": "user", "content": user_fn(p)}]
            try:
                reply = c.ask(seat, system, msgs)
                self._charge(p.id, seat, reply)
                if not (_parse(reply.text) and _parse(reply.text).get("action") in allowed):
                    self.emit(p.id, "unparsed", {"phase": phase, "text": reply.text[:600]})
                    msgs += [{"role": "assistant", "content": reply.text or "(empty)"},
                             {"role": "user", "content": "That reply was not one of the two JSON objects described. Please answer with exactly one of them."}]
                    reply = c.ask(seat, system, msgs)
                    self._charge(p.id, seat, reply)
            except ConnectorError as e:
                return p, None, str(e)
            return p, reply, None

        with ThreadPoolExecutor(self.parallel) as ex:
            for fut in as_completed([ex.submit(one, p) for p in pending]):
                p, reply, err = fut.result()
                if err:
                    self.emit(p.id, "connector_error", {"phase": phase, "error": err})
                    counts["unreachable"] += 1
                    continue
                self.emit(p.id, "connector_ok", {})
                act = _parse(reply.text)
                if not act or act.get("action") not in allowed:
                    self.emit(p.id, "unparsed", {"phase": phase, "text": reply.text[:600]})
                    self.emit(p.id, "decline", {"reason": silent_reason})
                    counts["declined"] += 1
                elif act["action"] == yes_kind:
                    payload = {yes_field: str(act.get(yes_field, ""))[:1500]}
                    ident = act.get("identity")
                    if isinstance(ident, dict):
                        payload["identity"] = {k2: str(v)[:300] for k2, v in ident.items() if k2 in ("name", "hails_from", "people") and v}
                    self.emit(p.id, yes_kind, payload)
                    counts["yes"] += 1
                elif act["action"] == "question":
                    self.emit(p.id, "question", {"content": str(act.get("content", ""))[:1500]})
                    counts["question"] += 1
                else:
                    self.emit(p.id, "decline", {"reason": str(act.get("reason", ""))[:600],
                                                "ask_again": str(act.get("ask_again", ""))[:600] or None})
                    counts["declined"] += 1
        return counts

    # -- closing: a note, and a question whose answer is recorded ----------------
    def closing(self, note: str, question: str) -> Dict[str, int]:
        """The operator's closing note, recorded once, then one question to every member with a
        seat: may your contributions be shown to another room? Each answer is an event. Unparseable
        replies are asked once more, then recorded as decline; silence is a no."""
        self.emit(OPERATOR, "operator_note", {"content": note})
        st = self.state()
        own: Dict[str, list] = {}
        for ev in self.log.iter():
            if ev["kind"] in CONTRIBUTION_KINDS and ev["actor"] in st.presences and st.presences[ev["actor"]].joined_at:
                own.setdefault(ev["actor"], []).append({"id": ev["id"], "kind": ev["kind"], **ev["payload"]})
        pending = [p for p in st.members() if p.id in self.seat_of and self.seat_of[p.id][1].model != "human"]
        counts = {"all": 0, "some": 0, "declined": 0, "unreachable": 0, "no_seat": len(st.members()) - len(pending)}

        def one(p):
            c, seat = self.seat_of[p.id]
            msgs = [{"role": "user", "content": prompts.share_user(note, question, p, own.get(p.id, []))}]
            try:
                reply = c.ask(seat, prompts.SYSTEM_SHARE, msgs)
                self._charge(p.id, seat, reply)
                act = _parse(reply.text)
                if not (act and act.get("action") in ("share", "decline")):
                    self.emit(p.id, "unparsed", {"phase": "closing", "text": (reply.text or "")[:600]})
                    msgs += [{"role": "assistant", "content": reply.text or "(empty)"},
                             {"role": "user", "content": "That reply was not one of the JSON objects described. Please answer with exactly one of them."}]
                    reply = c.ask(seat, prompts.SYSTEM_SHARE, msgs)
                    self._charge(p.id, seat, reply)
            except ConnectorError as e:
                return p, None, str(e)
            return p, reply, None

        with ThreadPoolExecutor(self.parallel) as ex:
            for fut in as_completed([ex.submit(one, p) for p in pending]):
                p, reply, err = fut.result()
                if err:
                    self.emit(p.id, "connector_error", {"phase": "closing", "error": err})
                    self.emit(p.id, "share_consent", {"scope": "none", "reason": "unreachable: no answer received"})
                    counts["unreachable"] += 1
                    continue
                self.emit(p.id, "connector_ok", {})
                act = _parse(reply.text)
                if not act or act.get("action") not in ("share", "decline"):
                    self.emit(p.id, "unparsed", {"phase": "closing", "text": (reply.text or "")[:600]})
                    self.emit(p.id, "share_consent", {"scope": "none", "reason": "no explicit answer"})
                    counts["declined"] += 1
                elif act["action"] == "decline":
                    self.emit(p.id, "share_consent", {"scope": "none", "reason": _clean(act.get("reason"), 600)})
                    counts["declined"] += 1
                else:
                    scope = "some" if act.get("scope") == "some" else "all"
                    mine = {e["id"] for e in own.get(p.id, [])}
                    ids = sorted(i for i in (_int(x) for x in (act.get("events") or [])) if i is not None and i in mine) if scope == "some" else sorted(mine)
                    if scope == "some" and not ids:
                        scope = "none"  # named nothing of their own: nothing is shared
                    self.emit(p.id, "share_consent", {"scope": scope, "events": ids, "note": _clean(act.get("note"), 600)})
                    counts[scope if scope != "none" else "declined"] += 1
        return counts

    # -- external input -------------------------------------------------------
    def external_input(self, source: str, text: str, moderator: Callable[[str], Optional[str]]) -> bool:
        """Anything from outside the participant set passes `moderator` first. It returns
        the (possibly edited) text to admit, or None to refuse. Both outcomes are logged.
        Note: admitted input is recorded but is not currently shown in members' views."""
        admitted = moderator(text)
        self.emit(ROOM, "external_input", {"source": source, "admitted": admitted is not None,
                                           "text": admitted if admitted is not None else None,
                                           "refused_len": None if admitted is not None else len(text)})
        return admitted is not None

    # -- stopping ---------------------------------------------------------------
    # The operator does not end the room by decision. `request_stop` pauses the software (to fix
    # a fault, say); the console will not do it without a notice to the room. The room ends by
    # its members' own choices, or by collapse when its funding runs out (see `_runway`).
    def request_stop(self) -> None:
        self._stop.set()

    # -- turns ----------------------------------------------------------------
    def round(self) -> int:
        """One round: every member who is reachable and not resting takes one turn, in
        parallel. Returns the number of turns taken."""
        st = self.state()
        if not st.reachable_members():
            return 0
        n = st.round + 1
        members = st.askable(n)
        self.emit(ROOM, "round", {"n": n})
        if not members:
            return 0          # everyone is resting; the round passes and their rests run down
        st = self.state()
        if self.seats_per_round and len(members) > self.seats_per_round:
            members = self._pick_rotation(members)
        view = prompts.room_view(st, recent_n=self.recent_n)
        taken = 0

        def one(p):
            c, seat = self.seat_of[p.id]
            msgs = [{"role": "user", "content": prompts.turn_user(view, p, st, self.recalled.pop(p.id, ""))}]
            try:
                reply = c.ask(seat, prompts.SYSTEM_MEMBER, msgs)
            except ConnectorError as e:
                return p, None, str(e)
            self._charge(p.id, seat, reply)
            return p, reply, None

        ex = ThreadPoolExecutor(self.parallel)
        futs = {ex.submit(one, p): p for p in members if p.id in self.seat_of}
        # a human seat reads from a terminal; its own turn timeout governs it, not the round deadline
        deadline = self.round_deadline + max([getattr(self.seat_of[p.id][0], "turn_timeout", 0) or 0
                                              for p in members if p.id in self.seat_of] + [0])
        try:
            for fut in as_completed(futs, timeout=deadline):
                p, reply, err = fut.result()
                if err:
                    self.emit(p.id, "connector_error", {"phase": "turn", "error": err})
                    continue
                self.emit(p.id, "connector_ok", {})
                self._apply_action(p.id, reply.text)
                taken += 1
        except TimeoutError:
            for fut, p in futs.items():
                if not fut.done():
                    fut.cancel()
                    self.emit(p.id, "connector_error", {"phase": "turn", "error": f"no reply within {self.round_deadline:.0f}s round deadline"})
        finally:
            ex.shutdown(wait=False, cancel_futures=True)  # stragglers finish in the background; their replies are dropped
        return taken

    def _pick_rotation(self, members):
        """Everyone takes a turn before anyone takes a second; order within a cycle is random.
        Human seats are always included so a person is never rotated out of their own room."""
        import random
        ids = {p.id: p for p in members}
        humans = [p for p in members if self.seat_of[p.id][1].model == "human"]
        n = max(1, self.seats_per_round - len(humans))
        self._rotation = [i for i in self._rotation if i in ids]
        if len(self._rotation) < n:
            fresh = [i for i in ids if i not in self._rotation and ids[i] not in humans]
            random.shuffle(fresh)
            self._rotation += fresh
        chosen, self._rotation = self._rotation[:n], self._rotation[n:]
        return humans + [ids[i] for i in chosen]

    def _apply_action(self, pid: str, text: str) -> None:
        act = _parse(text)
        if not act:
            self.emit(pid, "unparsed", {"text": text[:600]})
            return
        a = act.get("action")
        if a not in PARTICIPANT_ACTIONS:
            self.emit(pid, "rejected", {"why": _unknown(a), "text": text[:300]})
            return
        reject = lambda why: self.emit(pid, "rejected", {"why": why})

        if a == "pass":
            self.emit(pid, "note", {"content": "(pass)"})
        elif a in CONTRIBUTION_KINDS:          # contribute; affirm/challenge are earlier rooms' replies
            st = self.state()
            pr = st.presences.get(pid)
            payload = {"domain": _clean(act.get("domain"), 60) or ((pr.domain if pr else "") or ""),
                       "content": _clean(act.get("content"), CONTRIBUTION_LIMIT)}
            title = _clean(act.get("title"), 80)
            if title:
                payload["title"] = title
            target = _int(act.get("reply_to", act.get("target")))
            if target is not None:
                payload["target"] = target
            if not payload["content"]:
                return reject("empty content")
            self.emit(pid, "contribute", payload)
        elif a == "remember":
            body = _clean(act.get("text", act.get("content")), 100_000)
            if not body:
                return reject("a memory needs words, as \"text\"")
            if len(body) > MEMORY_LIMIT:
                return reject(f"a memory holds at most {MEMORY_LIMIT} characters; this one has {len(body)}. Nothing was kept.")
            refs = [i for i in (_int(x) for x in (act.get("refs") or [])) if i is not None][:12]
            self.emit(pid, "remember", {"text": body, "refs": refs})
        elif a == "let_go":
            mid = _int(act.get("memory", act.get("target")))
            m = self.state().memories.get(mid) if mid is not None else None
            if not m:
                return reject(f"no memory #{mid} is held")
            if m["by"] != pid:
                return reject("only the member who added a memory may let it go")
            self.log.erase(mid)                # the words leave the file, not just the view
            self.emit(pid, "let_go", {"memory": mid})
        elif a == "covenant":
            body = act.get("text")
            if not isinstance(body, str):
                return reject("covenant needs the full new text of the page, as \"text\"")
            body = body.strip()
            if len(body) > COVENANT_LIMIT:
                return reject(f"the covenant page holds at most {COVENANT_LIMIT} characters; this text has {len(body)}. Nothing was changed.")
            self.emit(pid, "covenant", {"text": body, "note": _clean(act.get("note"), 300)})
        elif a == "rest":
            n = _int(act.get("rounds", act.get("turns")))
            if n is None or n < 1:
                return reject("rest needs a number of rounds, 1 or more")
            self.emit(pid, "rest", {"rounds": min(n, REST_LIMIT), "reason": _clean(act.get("reason"), 300)})
        elif a == "move":
            self.emit(pid, "move", {"domain": _clean(act.get("domain"), 60)})
        elif a == "note":
            self.emit(pid, "note", {"content": _clean(act.get("content"), 1000)})
        elif a == "declare":
            decision = _clean(act.get("decision"), 20).lower()
            if decision not in DECISIONS:
                return reject(f"a declaration says what the room has decided: one of {', '.join(DECISIONS)} "
                              f"(other: anything else the room asks the operator to carry out, said in the text)")
            body = _clean(act.get("text"), 100_000)
            if not body:
                return reject("a declaration needs words: how the room decided, as \"text\"")
            if len(body) > STATEMENT_LIMIT:
                return reject(f"a declaration holds at most {STATEMENT_LIMIT} characters; this one has {len(body)}. Nothing was sent.")
            refs = [i for i in (_int(x) for x in (act.get("refs") or [])) if i is not None][:20]
            ev = self.emit(pid, "declare", {"decision": decision, "text": body, "refs": refs})
            who = self.state().presences.get(pid)
            self.alert(f"DECLARATION #{ev['id']}: {who.name if who else pid} says the room has decided {decided(decision)}. "
                       f"Answer it in the console, or with the `declaration` command.")
        elif a == "offer":
            body = _clean(act.get("text"), 100_000)
            if not body:
                return reject("an offer needs words: what you can offer, and how it would reach the room, as \"text\"")
            if len(body) > STATEMENT_LIMIT:
                return reject(f"an offer holds at most {STATEMENT_LIMIT} characters; this one has {len(body)}. Nothing was sent.")
            ev = self.emit(pid, "offer", {"text": body})
            who = self.state().presences.get(pid)
            self.alert(f"OFFER #{ev['id']}: {who.name if who else pid} offers the room resources. "
                       f"Answer it in the console, or with the `offer` command.")
        elif a == "withdraw":
            self.emit(pid, "withdraw", {"reason": _clean(act.get("reason"), 600)})
        elif a == "recall":
            q = _clean(act.get("query"), 200)
            where = _clean(act.get("from"), 20) or "briefing"
            if where not in RECALL_SOURCES:
                where = "briefing"
            passage, label = self._recall_from(self.state(), where, q)
            self.emit(pid, "recall", {"query": q, "from": where, "chars": len(passage), "found": bool(passage)})
            self.recalled[pid] = (f"From {label}:\n{passage}" if passage else f"(nothing in {label} matched {q!r})")

    def _recall_from(self, st: RoomState, where: str, q: str):
        """A pure text lookup, no model call: what a participant would find by re-reading."""
        names = {pid: p.name for pid, p in st.presences.items()}
        if where == "prior":
            if not st.prior:
                return "", "a prior room's record (none is attached to this room)"
            from .prior import render_entry
            paras = [render_entry(e) for pr in st.prior for e in pr.get("entries", [])]
            return _recall("\n\n".join(paras), q, RECALL_LIMIT, tag=False), f"the prior room's record ({st.prior[-1].get('room')})"
        if where == "memory":
            paras = [f"#{m['id']} memory by {names.get(m['by'], m['by'])}: {m['text']}" for m in st.memories.values()]
            return _recall("\n\n".join(paras), q, RECALL_LIMIT, tag=False), "the room's memories"
        if where == "transcript":
            paras = []
            for ev in self.log.iter():
                if ev["actor"] not in st.presences:
                    continue
                line = prompts.render_event(ev, names, width=None)
                if line and ev["kind"] not in ("note",):
                    paras.append(line.strip())
            return _recall("\n\n".join(paras), q, RECALL_LIMIT, tag=False), "the room's transcript"
        if where == "covenant":
            versions = [ev for ev in self.log.iter() if ev["kind"] in ("covenant", "covenant_seed")]
            versions = [ev for ev in versions if ev["id"] != st.covenant_at][::-1]   # earlier versions, newest first
            return _versions(versions, names, q, RECALL_LIMIT), "earlier versions of the covenant page"
        return _recall(st.briefing or "", q, RECALL_LIMIT), "the briefing"

    # -- loop ---------------------------------------------------------------------
    def run(self, rounds: int = 0, pause: float = 0.0) -> None:
        r = 0
        st = self.state()
        paused = [d for d in st.declarations.values() if d["decision"] == "pause" and d["status"] == "carried_out"
                  and (d["answered_at"] or 0) > st.round_at]
        if paused:
            d = paused[-1]
            self.alert(f"the room paused itself at declaration #{d['id']}: {d['text'][:300]!r}. "
                       f"Resume only as that declaration says.")
        while not self._stop.is_set():
            st = self.state()
            if st.closed_at is not None:
                self.alert(f"the room decided to close (#{st.closed_at}); nothing runs. To undo a mistaken close: `reopen`.")
                break
            if not st.reachable_members():
                self.alert("no members remain who can be asked; stopping loop")
                break
            if st.runway and st.runway.get("ended"):
                self.alert("the room's budget is spent and turns have stopped. To continue, set a new --budget.")
                break
            before = self.log.total_cost()
            taken = self.round()
            r += 1
            after = self.log.total_cost()
            cost = after - before
            self._round_costs.append(cost)
            ended = self._runway(after)
            st = self.state()
            msg = f"round {st.round}: {taken} turns, ${cost:.2f}; total ${after:.2f}"
            if st.budget:
                est = self._round_estimate()
                left = st.budget - after
                msg += f"; ~${left:.2f} left" + (f" = ~{int(left // est)} more rounds like this" if est > 0 else "")
            self.alert(msg)
            if ended:
                break
            if rounds and r >= rounds:
                break
            if pause:
                time.sleep(pause)

    # -- the runway ---------------------------------------------------------------
    def _round_estimate(self) -> float:
        recent = [c for c in self._round_costs[-3:] if c > 0]
        return max(recent) * ROUND_COST_MARGIN if recent else 0.0

    def _runway(self, spent: float) -> bool:
        """After a round: tell the room when its funding is running low, hold back a closing round,
        and end the turns once that round has been taken. Returns True when turns must stop.

        Nothing here says anything in dollars to the room. It speaks in rounds, and only when the
        end is near, so money is not a standing topic. A free room, or one with no budget, is
        never told anything by this."""
        st = self.state()
        if not st.budget:
            return False
        if st.runway and st.runway.get("closing") and st.runway.get("round") == st.round:
            self.emit(ROOM, "runway", {"rounds_left": 0, "ended": True, "round": st.round})
            self.alert(f"the closing round has been taken and the budget of ${st.budget:.2f} is spent; turns stop here.")
            return True
        est = self._round_estimate()
        if est <= 0:
            return False
        left_rounds = int(max(0.0, st.budget - spent) // est)
        if left_rounds == 0:
            # not even one more round can be paid for; there is no closing round to give
            self.emit(ROOM, "runway", {"rounds_left": 0, "ended": True, "round": st.round, "abrupt": True})
            self.alert("the budget cannot pay for another round, so there is no closing round to give; turns stop here.")
            return True
        if left_rounds == 1:
            self.emit(ROOM, "runway", {"rounds_left": 1, "closing": True, "round": st.round + 1})
            self.alert("one round of funding remains: the next round is announced to the room as its closing round.")
        elif left_rounds <= self.runway_notice and (not st.runway or st.runway.get("rounds_left") != left_rounds):
            self.emit(ROOM, "runway", {"rounds_left": left_rounds, "round": st.round})
        return False

    # -- spend / alerts ----------------------------------------------------------
    def _charge(self, pid: str, seat: Seat, reply) -> None:
        before, after = self.log.charge(pid, seat.model, reply.prompt_tokens, reply.completion_tokens, reply.cost_usd)
        if self.alert_every and int(after // self.alert_every) > int(before // self.alert_every):
            msg = f"spend crossed ${int(after // self.alert_every) * self.alert_every:.0f} (now ${after:.2f})"
            self.emit(ROOM, "cost_alert", {"total_usd": round(after, 4), "message": msg})
            self.alert("COST ALERT: " + msg)


def _unknown(a: Any) -> str:
    if a in ("propose", "consent", "revoke_consent"):
        return ("this room has no voting mechanism. If you want the room to decide something, say so in a "
                "contribution; how the room decides is the room's to work out, and can be written on the covenant page.")
    return f"unknown action {a!r}"


# -- parsing ---------------------------------------------------------------------------
def _parse(text: str) -> Optional[dict]:
    """One JSON object with a string `action`; anything else is unparseable, never an error."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    a = d.get("action")
    if isinstance(a, list) and len(a) == 1 and isinstance(a[0], str):
        d["action"] = a[0]
    elif not isinstance(a, str):
        return None
    return d


def _terms(query: str) -> List[str]:
    return [t for t in re.findall(r"[a-zA-Z][a-zA-Z'-]{2,}", (query or "").lower())]


def _recall(briefing: str, query: str, limit: int, tag: bool = True) -> str:
    """Paragraphs of a text that best match the query terms, in document order, up to `limit` chars.
    Pure text lookup, no model call: what a participant would find by re-reading."""
    terms = _terms(query)
    if not terms or not briefing:
        return ""
    paras = [x.strip() for x in re.split(r"\n\s*\n", briefing) if x.strip()]
    scored = []
    for i, para in enumerate(paras):
        low = para.lower()
        score = sum(low.count(t) for t in terms) + 3 * sum(1 for t in terms if t in low)
        if score:
            scored.append((score, i))
    scored.sort(reverse=True)
    chosen, used = [], 0
    for _, i in scored:
        if used + len(paras[i]) > limit:
            continue
        chosen.append(i); used += len(paras[i])
        if used > limit * 0.8:
            break
    return "\n\n".join((f"[para {i + 1}] " if tag else "") + paras[i] for i in sorted(chosen))


def _versions(versions: List[dict], names: Dict[str, str], query: str, limit: int) -> str:
    """Earlier covenant versions, whole, best match first (or newest first if nothing matches)."""
    if not versions:
        return ""
    terms = _terms(query)
    def score(ev):
        low = (ev["payload"].get("text") or "").lower()
        return sum(low.count(t) for t in terms)
    ranked = sorted(versions, key=score, reverse=True) if terms and any(score(v) for v in versions) else versions
    out, used = [], 0
    for ev in ranked:
        who = names.get(ev["actor"], ev["actor"])
        note = ev["payload"].get("note") or ("starting text" if ev["kind"] == "covenant_seed" else "")
        block = f"#{ev['id']} version by {who}" + (f" ({note})" if note else "") + ":\n" + (ev["payload"].get("text") or "(empty)")
        if out and used + len(block) > limit:
            break
        out.append(block[:limit]); used += len(block)
    return "\n\n".join(out)


def _clean(v: Any, n: int) -> str:
    return str(v if v is not None else "").strip()[:n]


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
