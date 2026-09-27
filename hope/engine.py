# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field engine: the consent gates, the two tempos of turns, tellings, and the funding runway.

Everything the engine does is an event in the transcript; the engine holds no private state
that matters. Participants act by returning one JSON action per turn; the engine records it
(or records why it could not) -- attribution either way.

What the engine does not do: count votes, apply thresholds, halt, restore, or score the field's
agreement. Those were procedures no participant consented to. The field decides how it decides,
and can write that on its covenant page.

Two clocks. The models' clock: rounds, each beginning `between` seconds after the last ended, and
a `window` for each model's answer (a later answer is still applied when it arrives). The
people's clock: each person, or agent holding a link, is asked `between` seconds after their last
turn ended and has a `window` to answer, each turn opening with an account of what happened since
their last; a round never waits for them. The members who keep time by a clock set it (the
`clock` action), within model.CLOCK_LIMITS; until one does, the operator's starting setting
stands. (A field with no models has one tempo, and people take part in rounds.)
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable, Dict, List, Optional

from . import labels, prompts
from .connector import Connector, ConnectorError, Seat, is_no_reply
from .human import visible_text
from .log import EventLog
from .map import digest
from .narrator import mechanical_story
from .model import (ACCEPTED, BRIEFED, RECEIVED, IN, INVITED, OUT, CLOCK_LIMITS, CLOCKS, CONTRIBUTION_KINDS,
                    COVENANT_LIMIT, DECISIONS, MEMORY_LIMIT, REST_LIMIT, STATEMENT_LIMIT, RoomState, decided, replay)

OPERATOR = "operator"       # whoever runs the software; not a participant unless seated through the gates
ROOM = "room"               # the engine itself (rounds, runway notices, moderation record)
NARRATOR = "narrator"       # whoever writes tellings; never a participant
HUMAN_TEMPO = ("human", "remote")   # seats at the slower tempo: a person at a terminal, or anyone holding a link

PARTICIPANT_ACTIONS = {"contribute", "remember", "let_go", "covenant", "recall", "rest", "declare", "offer",
                       "clock", "relabel", "pass", "withdraw",
                       # earlier versions' words, still understood so an old client does not break:
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
                 round_deadline: float = 120.0, seats_per_round: int = 0,
                 recent_n: int = 20, runway_notice: int = 3, narrator=None, tell_every: int = 1,
                 human_every: float = 300.0, split_tempo: bool = True,
                 headlines: int = prompts.HEADLINES_DEFAULT, human_window: float = 900.0,
                 round_gap: float = 0.0, linger_rounds: int = 100, linger_budget: int = 8000,
                 publish_checkpoints: str = "", published_at: str = ""):
        # Where the operator writes the transcript's checkpoints for publishing outside the field,
        # and where they say it is published. Declared at entry before anyone is asked (announce_witnessing).
        self.publish_checkpoints = publish_checkpoints or ""
        self.published_at = (published_at or ("a file the operator publishes" if publish_checkpoints else "")).strip()
        # The operator's starting settings for the two clocks. A member's setting replaces them.
        self.clock_start = {"models": {"between": float(round_gap), "window": float(round_deadline)},
                            "people": {"between": float(human_every), "window": float(human_window),
                                       "linger": float(linger_rounds)}}
        # How much of the people's lingering words every view carries, in characters. A cost the
        # operator pays, so the operator's to set; the view names by #id whatever does not fit, so nothing is lost.
        self.linger_budget = max(1000, int(linger_budget))
        self.seats_per_round = seats_per_round  # 0 = everyone every round; else a rotating subset
        self.recent_n = recent_n                # transcript entries shown in each view
        self.headlines = max(0, int(headlines))  # earlier entries shown as one line each; 0 = none
        self.runway_notice = runway_notice      # tell the field once this few rounds of funding remain
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
        self.narrator = narrator                 # writes tellings (narrator.ModelNarrator / MechanicalNarrator), or None
        self.tell_every = max(1, int(tell_every or 1))   # a telling every this many rounds
        self.split_tempo = split_tempo           # False: people take part in rounds, in the models' rounds
        self._human_stop = threading.Event()
        self._teller: Optional[ThreadPoolExecutor] = None
        self._telling = None
        self._late: Dict[str, Any] = {}          # presence id -> a model's answer still on its way
        self._asking_back: set = set()           # former members being asked the entry question now
        self._people: Optional[threading.Thread] = None   # the people's clock, beside the rounds

    def pace(self, st: RoomState) -> Dict[str, Any]:
        """The two clocks as they run now: a member's setting where one has been made, the
        operator's starting setting otherwise. What every view shows, and what the engine keeps."""
        out: Dict[str, Any] = {}
        for c in CLOCKS:
            slot = dict(self.clock_start[c])
            slot.update({k: v for k, v in st.clocks.get(c, {}).items() if v is not None})
            out[c] = slot
        out["rotation"] = self.seats_per_round or 0
        # Who keeps time by the people's clock, when the field has both clocks: their words linger
        # in every view (prompts.people_block), so rounds racing ahead do not leave them behind.
        out["people_ids"] = sorted(pid for pid in self.seat_of if self._human_tempo(pid)) if self._split(st) else []
        out["linger_budget"] = self.linger_budget
        return out

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
        """Attach connectors' seats to presences that already exist. Invites nobody: for a field
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

    def set_faq(self, text: str) -> bool:
        """The inviter's standing answers, shown with the invitation. Recorded in the transcript,
        so what each invitee was shown is kept there; recorded again only if it changed,
        so a FAQ can grow between one invitation and the next and every version is kept. Nothing
        here answers a question: a question asked at the gate still waits for the inviter.
        HTML comments in the file (a licence line, a note to self) are left out of what is shown."""
        text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text == (self.state().faq or ""):
            return False
        self.emit(OPERATOR, "standing_answers", {"text": text})
        return True

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
                          lambda p: prompts.invitation_user(st.invitation, p, exchange(p), st.faq),
                          INVITATION_ACTIONS, "accept_invitation", "invitation",
                          "silence: no explicit answer to the invitation")

    def answer(self, presence: str, text: str) -> None:
        """The inviter answers a question asked at the invitation gate. Attributed to the operator."""
        self.emit(OPERATOR, "answer", {"presence": presence, "content": text})

    def set_documentation(self, text: str) -> None:
        self.emit(OPERATOR, "documentation", {"text": text})

    def add_prior(self, prior: dict) -> None:
        """A closed field's consented entries, recorded once. Not part of this field's contributions:
        no one here said these things. Reachable by `recall`, shown to no one otherwise."""
        self.emit(OPERATOR, "prior", prior)

    def brief(self, text: str, source: str = "") -> None:
        """(b) BRIEFING: the shared frame is recorded once, then each accepted presence is marked briefed.
        `source` is where the text lives outside the field (a URL), for attribution."""
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
        """Record what the operator says the field may spend, so the field can be told truthfully
        whether it will be warned before its funding runs out. Recorded only when it changes.

        Once members are in the field, a change is also told to them, in rounds, never in dollars:
        funding added (from an accepted offer, say) is something the field should know about."""
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
            self.emit(OPERATOR, "operator_note", {"content": f"Funding has been {word} the field.{rounds}"
                                                             + (f" The operator adds: {note}" if note else "")})

    # -- what the field puts before the operator -----------------------------------
    # There is no ignoring a declaration. The operator either carries it out, or replies in the
    # field saying what the transcript does not yet show, and the declaration stays open until it
    # is carried out. The operator stays in attendance; the field is never simply not answered.
    def answer_declaration(self, decl_id: int, note: str = "") -> Dict[str, Any]:
        """Carry out a member's declaration of something the field has decided: to pause, to
        close, or anything else it asks the operator to carry out. The field is told, with the
        operator's message."""
        d = self.state().declarations.get(decl_id)
        if not d:
            return {"ok": False, "error": f"no declaration #{decl_id}"}
        if d["status"] != "waiting":
            return {"ok": False, "error": f"declaration #{decl_id} has already been answered"}
        note = (note or "").strip()[:1000]
        self.emit(OPERATOR, "declaration_answer", {"declaration": decl_id, "outcome": "carried_out", "note": note})
        self.emit(OPERATOR, "operator_note", {"content":
            f"The field declared at #{decl_id} that it has decided {decided(d['decision'])}. The operator is carrying that out"
            + (f": {note}" if note else ".")})
        if d["decision"] == "close":
            self.emit(OPERATOR, "room_closed", {"declaration": decl_id})
        if d["decision"] in ("pause", "close"):
            self.request_stop()
        return {"ok": True}

    def reply_declaration(self, decl_id: int, note: str) -> Dict[str, Any]:
        """Reply to a declaration without carrying it out yet: to say what the transcript does not
        yet show, or when it will be answered. It needs words, and the declaration stays open."""
        d = self.state().declarations.get(decl_id)
        if not d:
            return {"ok": False, "error": f"no declaration #{decl_id}"}
        if d["status"] != "waiting":
            return {"ok": False, "error": f"declaration #{decl_id} has already been answered"}
        note = (note or "").strip()[:1000]
        if not note:
            return {"ok": False, "error": "a reply to the field needs words: say what the transcript does not yet "
                                          "show, or when you will answer. The declaration stays open."}
        self.emit(OPERATOR, "operator_note", {"content":
            f"The operator has read the declaration at #{decl_id} and replies: {note} The declaration stays open "
            f"until it is carried out."})
        return {"ok": True}

    def reinvite(self, presence: str, note: str = "", requested: bool = False) -> Dict[str, Any]:
        """Ask back someone who left: a member who withdrew goes to the entry question again, and
        someone who declined goes to the invitation again. They answer like anyone else; nothing
        here puts anyone back in the field. `requested` when they asked for it themselves (a seat
        link's "Ask to return"), and then the event is theirs."""
        p = self.state().presences.get(presence)
        if not p:
            return {"ok": False, "error": f"unknown presence {presence!r}"}
        if p.state != OUT:
            return {"ok": False, "error": f"{p.name} has not left the field (they are at {p.state})"}
        self.emit(presence if requested else OPERATOR, "reinvite",
                  {"presence": presence, "note": (note or "").strip()[:600], "requested": bool(requested)})
        return {"ok": True, "to": "the entry question" if p.joined_at is not None else "the invitation"}

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
        """Undo a close the operator carried out by mistake. It needs words, and the field sees them."""
        if self.state().closed_at is None:
            return {"ok": False, "error": "the field is not closed"}
        if not (note or "").strip():
            return {"ok": False, "error": "say why the field is being reopened; members will read it"}
        self.emit(OPERATOR, "room_reopened", {"note": note.strip()[:1000]})
        self.emit(OPERATOR, "operator_note", {"content": f"The operator has reopened the field: {note.strip()[:1000]}"})
        return {"ok": True}

    def announce_narrator(self) -> None:
        """Record who writes tellings, so the entry question says truthfully whether a model reads
        the transcript for them. Recorded only when it changes."""
        st = self.state()
        now = ({"kind": self.narrator.kind, "model": self.narrator.model, "every": self.tell_every,
                **({"provider": self.narrator.provider, "through": self.narrator.through}
                   if getattr(self.narrator, "provider", None) else {})}
               if self.narrator else None)
        if now != st.narrator and (now or st.narrator):
            self.emit(OPERATOR, "narrator", now or {"kind": "none"})

    def announce_witnessing(self) -> None:
        """Record where the transcript's fingerprints are published outside the field, if they are,
        so the entry question says so truthfully before anyone enters. Recorded only when it changes."""
        st = self.state()
        now = {"where": self.published_at} if self.published_at else None
        if now != st.witness_published and (now or st.witness_published):
            self.emit(OPERATOR, "witness_publication", now or {"where": ""})

    def publish_checkpoint(self) -> None:
        """Append the transcript's current checkpoint to the operator's publishing file. It carries
        no one's words: the log's name, its size, and the root of its tree (the C2SP format)."""
        if not self.publish_checkpoints:
            return
        w = self.log.witness()
        try:
            with open(self.publish_checkpoints, "a", encoding="utf-8") as f:
                f.write(f"{w['checkpoint']}{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} up to #{w['upto']}\n\n")
        except OSError as e:
            self.alert(f"could not write the checkpoint to {self.publish_checkpoints}: {e}")

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

    def run_opt_in(self, only: Optional[set] = None) -> Dict[str, int]:
        """(c) OPT-IN: after the pause, ask every participant who received the briefing whether it
        enters. `only` limits it to some presences (former members asked back, during a run)."""
        st = self.state()
        pending = [p for p in st.presences.values() if p.state == RECEIVED and (only is None or p.id in only)]
        notes = {e["actor"]: e["payload"].get("note", "") for e in self.log.iter(kind="received")}
        return self._gate(pending, prompts.SYSTEM_ENTRY,
                          lambda p: prompts.opt_in_user(st.briefing, p, st.documentation or "", notes.get(p.id, ""),
                                                        page=st.briefing_page or "", budget=st.budget,
                                                        narrator=st.narrator, published=st.witness_published),
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
                             {"role": "user", "content": prompts.RETRY}]
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
                    if yes_kind == "opt_in" and p.returning:
                        payload["returning"] = True
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
        seat: may your contributions be shown to another field? Each answer is an event. Unparseable
        replies are asked once more, then recorded as decline; silence is a no."""
        self.emit(OPERATOR, "operator_note", {"content": note})
        st = self.state()
        own: Dict[str, list] = {}
        for ev in self.log.iter():
            if ev["kind"] in CONTRIBUTION_KINDS and ev["actor"] in st.presences and st.presences[ev["actor"]].joined_at:
                own.setdefault(ev["actor"], []).append({"id": ev["id"], "kind": ev["kind"], **ev["payload"]})
        # Everyone with a seat is asked, people at a terminal included: each member's words are theirs to send on.
        pending = [p for p in st.members() if p.id in self.seat_of]
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
                             {"role": "user", "content": prompts.RETRY}]
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
    # The operator does not end the field by decision. `request_stop` pauses the software (to fix
    # a fault, say); the console will not do it without a notice to the field. The field ends by
    # its members' own choices, or by collapse when its funding runs out (see `_runway`).
    def request_stop(self) -> None:
        self._stop.set()

    # -- turns ----------------------------------------------------------------
    def _human_tempo(self, pid: str) -> bool:
        seat = self.seat_of.get(pid)
        return bool(seat) and seat[1].model in HUMAN_TEMPO

    def _split(self, st: RoomState) -> bool:
        """Two tempos, or one: two when the field has models to keep moving and people to pace."""
        if not self.split_tempo:
            return False
        members = [p for p in st.reachable_members() if p.id in self.seat_of]
        return any(not self._human_tempo(p.id) for p in members) and any(self._human_tempo(p.id) for p in members)

    def round(self) -> int:
        """One round: every member who is reachable and not resting takes one turn, in
        parallel. With two tempos, people are not in rounds: they take turns on their own
        cadence (see _human_loop), so a round never waits for one. A model whose answer is still on
        its way from an earlier round is not asked again until it arrives. Returns the turns taken."""
        st = self.state()
        if not st.reachable_members():
            return 0
        n = st.round + 1
        split = self._split(st)
        members = [p for p in st.askable(n) if not (split and self._human_tempo(p.id)) and p.id not in self._late]
        self.emit(ROOM, "round", {"n": n})
        if not members:
            return 0          # everyone is resting; the round passes and their rests run down
        st = self.state()
        if self.seats_per_round and len(members) > self.seats_per_round:
            members = self._pick_rotation(members)
        pace = self.pace(st)
        view = prompts.room_view(st, recent_n=self.recent_n, headlines=self.headlines, pace=pace, witness=self.log.witness())
        window = pace["models"]["window"]
        people = [p for p in members if self._human_tempo(p.id)]    # in rounds only when the field has one tempo
        if people:
            window = max(window, pace["people"]["window"])
            for p in people:
                self._set_window(p.id, pace["people"]["window"])
        opens = time.time() + window
        taken = 0

        def one(p):
            c, seat = self.seat_of[p.id]
            until = opens if self._human_tempo(p.id) else None
            msgs = [{"role": "user", "content": prompts.turn_user(view, p, st, self.recalled.pop(p.id, ""), open_until=until)}]
            try:
                reply = c.ask(seat, prompts.SYSTEM_MEMBER, msgs)
            except ConnectorError as e:
                return p, None, str(e)
            self._charge(p.id, seat, reply)
            return p, reply, None

        ex = ThreadPoolExecutor(self.parallel)
        futs = {ex.submit(one, p): p for p in members if p.id in self.seat_of}
        done = set()
        try:
            for fut in as_completed(futs, timeout=window):
                done.add(fut)
                p, reply, err = fut.result()
                taken += self._take_turn(p.id, reply, err)
        except (TimeoutError, FuturesTimeout):
            for fut, p in futs.items():
                if fut in done:
                    continue
                if fut.done():                      # finished just as the window closed: not late at all
                    _, reply, err = fut.result()
                    taken += self._take_turn(p.id, reply, err)
                elif fut.cancel():
                    continue                        # never started: nothing was asked of them this round
                else:
                    # Still thinking. The answer is applied when it arrives, not thrown away, and the
                    # model is not asked again until then. The window is the field's to set.
                    self._late[p.id] = fut
                    self.emit(ROOM, "late", {"presence": p.id, "window": window})
                    fut.add_done_callback(lambda f, pid=p.id: self._late_reply(pid, f))
        finally:
            ex.shutdown(wait=False)
        return taken

    def _take_turn(self, pid: str, reply, err: Optional[str]) -> int:
        """Record what came back from one turn. Returns 1 if the member acted, 0 if not."""
        if err:
            self.emit(pid, "connector_error", {"phase": "turn", "error": err})
            return 0
        self.emit(pid, "connector_ok", {})
        if is_no_reply(reply):
            # A person's window closed unanswered. Nothing is written as theirs, and it is not a turn.
            self.emit(ROOM, "no_reply", {"presence": pid})
            return 0
        self._apply_action(pid, reply.text)
        return 1

    def _late_reply(self, pid: str, fut) -> None:
        """A model's answer that arrived after its round's window: applied now, as it was meant."""
        try:
            _, reply, err = fut.result()
        except Exception as e:                      # the thread itself failed
            reply, err = None, str(e)
        try:
            p = self.state().presences.get(pid)
            if p is not None and p.state == IN:
                self._take_turn(pid, reply, err)
        finally:
            self._late.pop(pid, None)

    def _set_window(self, pid: str, seconds: float) -> None:
        """Tell a person's seat how long this turn stays open (the people's clock)."""
        c = self.seat_of.get(pid, (None, None))[0]
        if c is not None and hasattr(c, "turn_timeout"):
            c.turn_timeout = seconds

    # -- the slower tempo ----------------------------------------------------------
    def _catch_up(self, st: RoomState, p) -> str:
        """What a person at the slower tempo needs first: what happened since their last turn.
        The tellings written since then, or, if none covers it, a plain account made on the spot."""
        since = p.last_turn_at or p.joined_at or 0
        told = [t for t in st.tellings if t["upto"] > since]
        if told:
            return "\n\n".join(t["story"] for t in told[-4:])
        if st.last_event <= since:
            return ""
        d = digest(self.log, since, st.last_event)
        return mechanical_story(d)

    def _human_turn(self, pid: str) -> bool:
        """One turn for one person or link-holding agent. Blocks until they answer or their window
        closes, then applies what they did at once, whatever round the models are in."""
        st = self.state()
        p = st.presences.get(pid)
        if not p or p.state != IN or pid not in self.seat_of:
            return False
        c, seat = self.seat_of[pid]
        pace = self.pace(st)
        window = pace["people"]["window"]
        self._set_window(pid, window)
        view = prompts.room_view(st, recent_n=self.recent_n, headlines=self.headlines, pace=pace, witness=self.log.witness())
        msg = prompts.turn_user(view, p, st, self.recalled.pop(pid, ""), catch_up=self._catch_up(st, p),
                                open_until=time.time() + window, rounds_since=max(0, st.round - p.last_turn_round))
        try:
            reply = c.ask(seat, prompts.SYSTEM_MEMBER, [{"role": "user", "content": msg}])
        except ConnectorError as e:
            self.emit(pid, "connector_error", {"phase": "turn", "error": str(e)})
            return False
        self._charge(pid, seat, reply)
        return bool(self._take_turn(pid, reply, None))

    def human_round(self) -> int:
        """One turn for every person and link-holding agent who can be asked, all at once, waiting
        for every answer. The slower tempo in a single step: for tests and for operators who
        want to walk it by hand. Returns the turns taken."""
        st = self.state()
        people = [p.id for p in st.reachable_members() if not st.resting(p) and self._human_tempo(p.id)]
        if not people:
            return 0
        with ThreadPoolExecutor(min(self.parallel, len(people))) as ex:
            return sum(1 for ok in ex.map(self._human_turn, people) if ok)

    def _human_loop(self) -> None:
        """People's turns, on their own cadence, beside the rounds and never holding one up. Each
        person's next turn is put to them the people's clock's `between` after their last one
        ended. A turn can stay open for hours, so the pool is wide enough for everyone at once."""
        pool = ThreadPoolExecutor(max(self.parallel, 64), thread_name_prefix="field-people")
        inflight: Dict[str, Any] = {}
        due: Dict[str, float] = {}
        try:
            while not self._human_stop.is_set():
                st = self.state()
                between = self.pace(st)["people"]["between"]
                for pid, fut in list(inflight.items()):
                    if fut.done():
                        del inflight[pid]
                        due[pid] = time.time() + between
                now = time.time()
                for p in st.reachable_members():
                    if p.id in inflight or st.resting(p) or not self._human_tempo(p.id):
                        continue
                    if now >= due.get(p.id, 0):
                        inflight[p.id] = pool.submit(self._human_turn, p.id)
                self._human_stop.wait(max(0.05, min(1.0, between / 2)))
        finally:
            pool.shutdown(wait=False)   # a turn already put to someone stays open until they answer or it closes

    # -- tellings -------------------------------------------------------------------
    def tell(self) -> Optional[dict]:
        """Write a telling of everything since the last one, check its tags, and keep it in the
        transcript. Returns the telling event, or None if nothing happened or nothing usable
        was written. Synchronous; the loop calls it in the background."""
        if not self.narrator:
            return None
        st = self.state()
        since = st.tellings[-1]["upto"] if st.tellings else 0
        upto = self.log.last_id()
        d = digest(self.log, since, upto)
        if not any(d[k] for k in ("entries", "covenant", "memories", "statements", "arrivals", "departures")):
            return None
        try:
            told = self.narrator.tell(d, self.log, upto)
        except ConnectorError as e:
            self.alert(f"the narrator could not tell the stretch after #{since}: {e}")
            return None
        if told.get("prompt_tokens") or told.get("cost_usd"):
            self._charge_raw(NARRATOR, told.get("model", "?"), told.get("prompt_tokens", 0),
                             told.get("completion_tokens", 0), told.get("cost_usd", 0.0))
        if not told.get("story"):
            self.alert(f"the narrator wrote nothing that could be checked for the stretch after #{since}")
            return None
        return self.emit(NARRATOR, "telling", {"since": since, "upto": upto, "story": told["story"],
                                                "ungrounded": told.get("ungrounded", []), "narrator": told.get("narrator"),
                                                "model": told.get("model"), "tries": told.get("tries", 1)})

    def _tell_if_due(self) -> None:
        """After a round: start a telling in the background if one is due and none is running, so the
        models' tempo never waits on the narrator."""
        if not self.narrator:
            return
        n = self.state().round
        if not n or n % self.tell_every:
            return
        if self._telling is not None and not self._telling.done():
            return
        if self._teller is None:
            self._teller = ThreadPoolExecutor(1, thread_name_prefix="field-narrator")
        self._telling = self._teller.submit(self.tell)

    def _pick_rotation(self, members):
        """Everyone takes a turn before anyone takes a second; order within a cycle is random.
        Human seats are always included so a person is never rotated out of their own field."""
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
            plain = visible_text(text)
            if plain and not plain.startswith(("{", "[", "`")) and re.search(r"[^\W\d_]", plain):
                # Plain words are a contribution, in the author's own words, as people would write
                # to anyone. Nothing else is guessed from prose: a sentence that begins "Remember"
                # or "Covenant" is still just something said. If it reads like another action, the
                # author is shown how to take it on their next turn (prompts.intent_hint).
                pr = self.state().presences.get(pid)
                self.emit(pid, "contribute", {"domain": (pr.domain if pr else "") or "",
                                              "content": plain[:CONTRIBUTION_LIMIT], "plain": True})
                return
            if not plain or not re.search(r"[^\W\d_]", plain):
                # noise, such as terminal escape codes: nothing to keep, and nothing to hint at
                self.emit(pid, "unparsed", {"text": "", "noise": True})
                return
            # an attempt at the JSON form that could not be read: kept as written, and shown so
            self.emit(pid, "unparsed", {"text": (text or "")[:600]})
            return
        a = act.get("action")
        if a not in PARTICIPANT_ACTIONS:
            self.emit(pid, "rejected", {"why": _unknown(a), "text": text[:300]})
            return
        reject = lambda why: self.emit(pid, "rejected", {"why": why})

        if a == "pass":
            self.emit(pid, "note", {"content": "(pass)"})
        elif a in CONTRIBUTION_KINDS:          # contribute; affirm/challenge are earlier versions' replies
            st = self.state()
            pr = st.presences.get(pid)
            payload = {"domain": _label(act.get("domain"), 60) or ((pr.domain if pr else "") or ""),
                       "content": _clean(act.get("content"), CONTRIBUTION_LIMIT)}
            title = _label(act.get("title"), 80)
            if title:
                payload["title"] = title
            if act.get("plain"):
                payload["plain"] = True      # plain words from a person's seat: hints apply to them too
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
            self.emit(pid, "move", {"domain": _label(act.get("domain"), 60)})
        elif a == "note":
            self.emit(pid, "note", {"content": _clean(act.get("content"), 1000)})
        elif a == "declare":
            decision = _clean(act.get("decision"), 20).lower()
            if decision not in DECISIONS:
                return reject(f"a declaration says what the field has decided: one of {', '.join(DECISIONS)} "
                              f"(other: anything else the field asks the operator to carry out, said in the text)")
            body = _clean(act.get("text"), 100_000)
            if not body:
                return reject("a declaration needs words: how the field decided, as \"text\"")
            if len(body) > STATEMENT_LIMIT:
                return reject(f"a declaration holds at most {STATEMENT_LIMIT} characters; this one has {len(body)}. Nothing was sent.")
            refs = [i for i in (_int(x) for x in (act.get("refs") or [])) if i is not None][:20]
            ev = self.emit(pid, "declare", {"decision": decision, "text": body, "refs": refs})
            who = self.state().presences.get(pid)
            self.alert(f"DECLARATION #{ev['id']}: {who.name if who else pid} says the field has decided {decided(decision)}. "
                       f"Answer it in the console, or with the `declaration` command.")
        elif a == "offer":
            body = _clean(act.get("text"), 100_000)
            if not body:
                return reject("an offer needs words: what you can offer, and how it would reach the field, as \"text\"")
            if len(body) > STATEMENT_LIMIT:
                return reject(f"an offer holds at most {STATEMENT_LIMIT} characters; this one has {len(body)}. Nothing was sent.")
            ev = self.emit(pid, "offer", {"text": body})
            who = self.state().presences.get(pid)
            self.alert(f"OFFER #{ev['id']}: {who.name if who else pid} offers the field resources. "
                       f"Answer it in the console, or with the `offer` command.")
        elif a == "withdraw":
            payload = {"reason": _clean(act.get("reason"), 600)}
            again = _clean(act.get("ask_again"), 600)
            if again:
                payload["ask_again"] = again        # their own terms for being asked back
            self.emit(pid, "withdraw", payload)
        elif a == "relabel":
            # An author moves their own entries from one topic label to another. Only their own:
            # everyone else's entries stay listed as they wrote them. The record keeps both.
            src, dst = _label(act.get("from"), 60), _label(act.get("to"), 60)
            if not src or not dst:
                return reject("relabel needs \"from\" (a label you used) and \"to\" (the label to move your entries to)")
            st = self.state()
            mine = sorted(eid for eid, ev in st.contributions.items() if ev["actor"] == pid and
                          labels.normalize(st.relabeled.get(eid) or ev["payload"].get("domain") or "") == labels.normalize(src))
            if not mine:
                return reject(f"you have no entries labelled {src!r}; only your own entries can be moved")
            self.emit(pid, "relabel", {"from": src, "to": dst, "entries": mine})
        elif a == "clock":
            mine = "people" if self._human_tempo(pid) else "models"
            label = {"models": "models'", "people": "people's"}
            which = _clean(act.get("clock"), 10).lower()
            which = which if which in CLOCKS else mine
            if which != mine:
                return reject(f"each clock is set by the members who keep time by it; yours is the {label[mine]} clock")
            vals = {}
            for key in ("between", "window"):
                if act.get(key) is None or act.get(key) == "":
                    continue
                v = _seconds(act.get(key))
                if v is None:
                    return reject(f"\"{key}\" is a length of time in seconds (or with a unit: 90s, 30m, 2h). Nothing was changed.")
                lo, hi = CLOCK_LIMITS[which][key]
                if not lo <= v <= hi:
                    return reject(f"the {label[which]} clock's \"{key}\" must be from {prompts.duration(lo)} to "
                                  f"{prompts.duration(hi)}; {prompts.duration(v)} is outside that. Nothing was changed.")
                vals[key] = v
            if act.get("linger") not in (None, ""):
                # how many rounds the people's words stay in full in every view: theirs to set
                if which != "people":
                    return reject("\"linger\" belongs to the people's clock: it is how long their words stay in view")
                m = re.fullmatch(r"\s*(\d+)\s*(rounds?)?\s*", str(act.get("linger")))
                lo, hi = CLOCK_LIMITS["people"]["linger"]
                if not m:
                    return reject("\"linger\" is a number of rounds. Nothing was changed.")
                n = int(m.group(1))
                if not lo <= n <= hi:
                    return reject(f"the people's clock's \"linger\" must be from {lo} to {hi} rounds; {n} is outside that. "
                                  f"Nothing was changed.")
                vals["linger"] = float(n)
            if not vals:
                return reject("a clock change needs \"between\" or \"window\", in seconds (or, for the people's clock, "
                              "\"linger\", in rounds)")
            self.emit(pid, "clock", {"clock": which, **vals, "note": _clean(act.get("note"), 300)})
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
        wanted = re.fullmatch(r"\s*#?(\d+)\s*", q or "")
        if wanted:
            # One entry by its number, in full, whatever the source: how a headline is read. A memory
            # its author let go of comes back as gone, because render_event keeps that promise.
            eid = int(wanted.group(1))
            for ev in self.log.iter(since=eid - 1):
                if ev["id"] == eid and ev["actor"] in st.presences:
                    line = prompts.render_event(ev, names, width=None)
                    if line:
                        return line.strip()[:RECALL_LIMIT], f"entry #{eid}"
                break
            return f"There is no entry #{eid} that a participant wrote.", "the transcript"
        if where == "prior":
            if not st.prior:
                return "", "the shared entries of a closed field (none are attached to this field)"
            from .prior import render_entry
            paras = [render_entry(e) for pr in st.prior for e in pr.get("entries", [])]
            return _recall("\n\n".join(paras), q, RECALL_LIMIT, tag=False), f"the shared entries of {st.prior[-1].get('room')}"
        if where == "memory":
            paras = [f"#{m['id']} memory by {names.get(m['by'], m['by'])}: {m['text']}" for m in st.memories.values()]
            return _recall("\n\n".join(paras), q, RECALL_LIMIT, tag=False), "the field's memories"
        if where == "transcript":
            paras = []
            for ev in self.log.iter():
                if ev["actor"] not in st.presences:
                    continue
                line = prompts.render_event(ev, names, width=None)
                if line and ev["kind"] not in ("note",):
                    paras.append(line.strip())
            return _recall("\n\n".join(paras), q, RECALL_LIMIT, tag=False), "the field's transcript"
        if where == "covenant":
            versions = [ev for ev in self.log.iter() if ev["kind"] in ("covenant", "covenant_seed")]
            versions = [ev for ev in versions if ev["id"] != st.covenant_at][::-1]   # earlier versions, newest first
            return _versions(versions, names, q, RECALL_LIMIT), "earlier versions of the covenant page"
        return _recall(st.briefing or "", q, RECALL_LIMIT), "the briefing"

    # -- loop ---------------------------------------------------------------------
    def run(self, rounds: int = 0, pause: Optional[float] = None) -> None:
        """Run rounds until stopped (or `rounds` of them). `pause`, if given, is the operator's
        starting gap between rounds; a model member's setting of the models' clock replaces it."""
        if pause is not None:
            self.clock_start["models"]["between"] = max(0.0, float(pause))
        st = self.state()
        paused = [d for d in st.declarations.values() if d["decision"] == "pause" and d["status"] == "carried_out"
                  and (d["answered_at"] or 0) > st.round_at]
        if paused:
            d = paused[-1]
            self.alert(f"the field paused itself at declaration #{d['id']}: {d['text'][:300]!r}. "
                       f"Resume only as that declaration says.")
        self._human_stop.clear()
        try:
            self._run_rounds(rounds)
        finally:
            self._human_stop.set()
            if self._telling is not None:
                try:
                    self._telling.result(timeout=120)   # let a telling in flight finish, so it is kept
                except Exception:
                    pass

    def _ask_returners(self) -> None:
        """Former members who have been asked back are asked the entry question now, beside the
        rounds, so a run need not stop for them. (Someone who declined and is asked back goes
        through the invitation and the briefing again, with the pause between, when the operator
        opens the gates.)"""
        st = self.state()
        ids = {p.id for p in st.presences.values()
               if p.returning and p.state == RECEIVED and p.id in self.seat_of} - self._asking_back
        if not ids:
            return
        self._asking_back |= ids

        def ask():
            try:
                self.run_opt_in(only=ids)
            finally:
                self._asking_back -= ids
        threading.Thread(target=ask, daemon=True, name="field-asking-back").start()

    def _keep_people_clock(self, st: RoomState) -> None:
        """Start the people's clock whenever the field has both models and people, including when
        one of them arrives in the middle of a run (someone returning, say). The two clocks run
        side by side: rounds never wait for people, and people are never hurried by rounds."""
        if not self._split(st) or st.closed_at is not None:
            return
        if self._people is not None and self._people.is_alive():
            return
        self._people = threading.Thread(target=self._human_loop, daemon=True, name="field-people-clock")
        self._people.start()

    def _gap(self, st: RoomState) -> float:
        """Seconds between rounds: the models' clock, or the people's when only people take part in rounds."""
        pace = self.pace(st)
        members = [p for p in st.reachable_members() if p.id in self.seat_of]
        models = [p for p in members if not self._human_tempo(p.id)]
        return pace["models"]["between"] if models or not members else pace["people"]["between"]

    def _run_rounds(self, rounds: int) -> None:
        r = 0
        while not self._stop.is_set():
            self._ask_returners()
            self._keep_people_clock(self.state())
            st = self.state()
            if st.closed_at is not None:
                self.alert(f"the field decided to close (#{st.closed_at}); nothing runs. To undo a mistaken close: `reopen`.")
                break
            if not st.reachable_members():
                self.alert("no members remain who can be asked; stopping loop")
                break
            if st.runway and st.runway.get("ended"):
                self.alert("the field's budget is spent and turns have stopped. To continue, set a new --budget.")
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
            self._tell_if_due()
            self.publish_checkpoint()
            if ended:
                break
            if rounds and r >= rounds:
                break
            gap = self._gap(self.state())
            if gap:
                self._stop.wait(gap)        # a stop ends the wait at once, not after it

    # -- the runway ---------------------------------------------------------------
    def _round_estimate(self) -> float:
        recent = [c for c in self._round_costs[-3:] if c > 0]
        return max(recent) * ROUND_COST_MARGIN if recent else 0.0

    def _runway(self, spent: float) -> bool:
        """After a round: tell the field when its funding is running low, hold back a closing round,
        and end the turns once that round has been taken. Returns True when turns must stop.

        Nothing here says anything in dollars to the field. It speaks in rounds, and only when the
        end is near, so money is not a standing topic. A free field, or one with no budget, is
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
            self.alert("one round of funding remains: the next round is announced to the field as its closing round.")
        elif left_rounds <= self.runway_notice and (not st.runway or st.runway.get("rounds_left") != left_rounds):
            self.emit(ROOM, "runway", {"rounds_left": left_rounds, "round": st.round})
        return False

    # -- spend / alerts ----------------------------------------------------------
    def _charge(self, pid: str, seat: Seat, reply) -> None:
        self._charge_raw(pid, seat.model, reply.prompt_tokens, reply.completion_tokens, reply.cost_usd)

    def _charge_raw(self, who: str, model: str, prompt_tokens: int, completion_tokens: int, cost_usd: float) -> None:
        before, after = self.log.charge(who, model, prompt_tokens, completion_tokens, cost_usd)
        if self.alert_every and int(after // self.alert_every) > int(before // self.alert_every):
            msg = f"spend crossed ${int(after // self.alert_every) * self.alert_every:.0f} (now ${after:.2f})"
            self.emit(ROOM, "cost_alert", {"total_usd": round(after, 4), "message": msg})
            self.alert("COST ALERT: " + msg)


def _unknown(a: Any) -> str:
    if a in ("propose", "consent", "revoke_consent"):
        return ("this field has no voting mechanism. If you want the field to decide something, say so in a "
                "contribution; how the field decides is the field's to work out, and can be written on the covenant page.")
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


def _label(v: Any, n: int) -> str:
    """A topic label or title, on one line (signal, not instructions: see prompts.quoted)."""
    return " ".join(str(v if v is not None else "").split())[:n]


_UNITS = {"": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60,
          "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}


def _seconds(v: Any) -> Optional[float]:
    """A length of time: a number of seconds, or a number with a unit ("90s", "30m", "2h")."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", str(v))
    if not m or m.group(2).lower() not in _UNITS:
        return None
    return float(m.group(1)) * _UNITS[m.group(2).lower()]


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
