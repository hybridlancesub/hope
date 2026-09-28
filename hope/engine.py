# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field engine: the consent gates, wakes, tellings, and the funding runway.

Everything the engine does is an event in the transcript; the engine holds no private state
that matters. Participants act by returning actions; the engine records each (or records why it
could not) -- attribution either way.

What the engine does not do: count votes, apply thresholds, halt, restore, or score the field's
agreement. Those were procedures no participant consented to. The field decides how it decides,
and can write that on its covenant page.

No one takes turns (notes/sketch-3-channels.md). People post whenever they like (`post`). Models
cannot act on their own, so the software wakes them, and only for what each chose: new words in
the domains and circles it follows or has written in, a reply to it, being named, something in a
circle waiting for its yes, or a breath at a length it set. Wakes are batched: fifty new entries
are one wake. A wake says nothing is expected, and saying nothing writes nothing. The software
sets no rhythm; it holds a floor (no model woken more often than model.FLOOR, so models cannot
loop at machine speed), a window for an answer (a later answer is still applied), and the runway.
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
from .model import (ACCEPTED, BRIEFED, RECEIVED, IN, INVITED, OUT, CONTRIBUTION_KINDS,
                    COVENANT_LIMIT, DECISIONS, MEMORY_LIMIT, STATEMENT_LIMIT, RoomState, decided, replay)
from .model import FLOOR, PRIVACY_EVERY, QUIET_HOURS, WAKE_ACTIONS

OPERATOR = "operator"       # whoever runs the software; not a participant unless seated through the gates
ROOM = "room"               # the engine itself (rounds, runway notices, moderation record)
NARRATOR = "narrator"       # whoever writes tellings; never a participant
HUMAN_TEMPO = ("human", "remote")   # seats that post for themselves: a person at a terminal, or anyone holding a link

PARTICIPANT_ACTIONS = {"contribute", "remember", "let_go", "covenant", "recall", "rest", "declare", "offer",
                       "clock", "relabel", "pass", "quiet", "withdraw",
                       # channels: domains and circles (notes/sketch-3-channels.md)
                       "follow", "unfollow", "pause", "wake", "form_circle", "join_circle", "leave_circle", "ask",
                       "knock", "answer", "ask_circle", "reply_circle", "privacy", "harvest", "quiet_for",
                       # earlier versions' words, still understood so an old client does not break:
                       "affirm", "challenge", "note", "move"}
RECALL_SOURCES = ("briefing", "transcript", "memory", "covenant", "prior")
RECALL_LIMIT = 2500         # characters returned per recall
CONTRIBUTION_LIMIT = 2000   # characters; longer contributions are cut, and the prompt says so
DOMAIN_LIMIT = 120          # characters for a domain label, nested parts and all
HARVEST_LIMIT = 4000        # characters: a circle's account of what it learned
REASON_LIMIT = 600          # characters for a reason: why a circle is private, why a knock was turned away
BREATH_LIMITS = (3600, 30 * 86400)   # a breath, if a member wants one: from an hour to thirty days
QUIET_LIMITS = (1, 30 * 24)          # hours a circle may be quiet before it is told so
WAKE_COST_MARGIN = 1.15     # a wake is estimated this much dearer than recent ones, so the closing wakes are really paid for
INVITATION_ACTIONS = {"accept_invitation", "decline", "question"}
DELIVERY_ACTIONS = {"received", "decline"}
ENTRY_ACTIONS = {"opt_in", "decline"}


class Room:
    def __init__(self, log: EventLog, connectors: List[Connector], *,
                 alert_every_usd: float = 50.0, alert_fn: Callable[[str], None] = print,
                 parallel: int = 8, on_event: Optional[Callable[[dict], None]] = None,
                 window: float = 120.0, floor: float = FLOOR, wake_ceiling: int = 0,
                 recent_n: int = 20, runway_notice: float = 24.0, narrator=None, tell_every: int = 20,
                 headlines: int = prompts.HEADLINES_DEFAULT, linger: int = prompts.LINGER_MESSAGES,
                 linger_budget: int = prompts.LINGER_BUDGET, news_budget: int = prompts.NEWS_BUDGET,
                 tick: float = 1.0, publish_checkpoints: str = "", published_at: str = ""):
        # Where the operator writes the transcript's checkpoints for publishing outside the field,
        # and where they say it is published. Declared at entry before anyone is asked (announce_witnessing).
        self.publish_checkpoints = publish_checkpoints or ""
        self.published_at = (published_at or ("a file the operator publishes" if publish_checkpoints else "")).strip()
        self.window = float(window)             # how long a woken model has to answer; a later answer still counts
        # No model is woken more often than this: a floor the software holds, so models answering
        # each other cannot loop at machine speed. Tests lower it; nothing a member does can.
        self.floor = max(0.0, float(floor))
        # A cost setting, the operator's: at most this many wakes a minute across the field, whoever
        # has waited longest first. 0 = none. Every view says so while it is on.
        self.wake_ceiling = max(0, int(wake_ceiling))
        self.linger = max(10, int(linger))      # messages in a channel a person's latest words stay in full for models
        # How much of people's lingering words, and of the news, each wake carries, in characters.
        # A cost the operator pays; what does not fit is named by #id, so nothing is lost.
        self.linger_budget = max(1000, int(linger_budget))
        self.news_budget = max(2000, int(news_budget))
        self.recent_n = recent_n                # entries of context shown before the news in each channel
        self.headlines = max(0, int(headlines))  # earlier entries shown as one line each; 0 = none
        self.runway_notice = float(runway_notice)   # hours: the field is told when about this much funding time remains
        self.tick = float(tick)                 # how often the scheduler looks, at most, when nothing nudges it
        self.log = log
        self.connectors = connectors
        self.seat_of: Dict[str, tuple] = {}      # presence id -> (connector, seat)
        self.alert_every = alert_every_usd
        self.alert = alert_fn
        self.parallel = parallel
        self.on_event = on_event
        self._stop = threading.Event()
        self._nudge = threading.Event()          # set by every new entry, so the scheduler looks again at once
        self.recalled: Dict[str, str] = {}       # presence id -> passage to show on its next wake (the recall event is the record)
        self.narrator = narrator                 # writes tellings (narrator.ModelNarrator / MechanicalNarrator), or None
        self.tell_every = max(1, int(tell_every or 20))   # a telling every this many contributions
        self._teller: Optional[ThreadPoolExecutor] = None
        self._telling = None
        self._late: Dict[str, Any] = {}          # presence id -> a model's answer still on its way
        self._busy: set = set()                  # models being woken now
        self._asking_back: set = set()           # former members being asked the entry question now
        self._listening: set = set()             # people at a terminal whose lines are being read
        self._wake_costs: List[float] = []       # what recent wakes cost, for the runway
        self._ceiling_log: List[float] = []      # when recent wakes began, for the wake ceiling
        self._last_checkpoint = 0.0

    def limits(self) -> Dict[str, Any]:
        """What every view says about time: the floor and the window the software holds, and the
        operator's wake ceiling if one is on. The software sets no rhythm beyond these."""
        return {"floor": self.floor, "window": self.window, "ceiling": self.wake_ceiling}

    # -- helpers ----------------------------------------------------------------
    def state(self, upto: Optional[int] = None) -> RoomState:
        return replay(self.log.iter(), upto)

    def emit(self, actor: str, kind: str, payload: Optional[dict] = None) -> dict:
        ev = self.log.append(actor, kind, payload)
        if self.on_event:
            self.on_event(ev)
        self._nudge.set()                        # something new: the scheduler looks again
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

        Once members are in the field, a change is also told to them, in time at the current rate,
        never in dollars: funding added (from an accepted offer, say) is something the field should
        know about."""
        if not usd or usd <= 0:
            return
        st = self.state()
        before = st.budget
        if before == float(usd):
            return
        self.emit(OPERATOR, "budget", {"usd": float(usd)})
        if st.members() and before is not None:
            rate = self._spend_rate()
            left = float(usd) - self.log.total_cost()
            rounds = (f" At the current rate it lasts about {prompts.duration(max(0.0, left) / rate * 3600)}."
                      if rate > 0 else "")
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

    # -- wakes -----------------------------------------------------------------------
    # Nobody is asked to take a turn. People post whenever they like (post). Models cannot act on
    # their own, so the software wakes them, and only for what each chose. A wake is not a turn:
    # it says nothing is expected, it offers pausing first, and saying nothing writes nothing.
    def _human_tempo(self, pid: str) -> bool:
        seat = self.seat_of.get(pid)
        return bool(seat) and seat[1].model in HUMAN_TEMPO

    def _people_ids(self) -> List[str]:
        return sorted(pid for pid in self.seat_of if self._human_tempo(pid))

    @staticmethod
    def _paused(p, now: float) -> bool:
        if not p.pause:
            return False
        return not (p.pause.get("until_ts") and now >= p.pause["until_ts"])

    def why_wake(self, st: RoomState, p, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Why a model would be woken now, or None: the reason it chose, and the channel where its
        plain words would go. A wake already on its way, the floor, and its own pause come first."""
        now = time.time() if now is None else now
        if p.id in self._late or p.id in self._busy or self._paused(p, now):
            return None
        rw = st.runway or {}
        if rw.get("closing") and not rw.get("ended"):
            # funding is ending: one closing wake for every model not paused, and nothing else
            return {"why": "closing", "where": None} if p.last_wake_ts < float(rw.get("at") or 0) else None
        if now - p.last_wake_ts < self.floor:
            return None
        if p.last_seen < (p.joined_at or 0):
            return {"why": "entered", "where": "d:"}
        if p.id in self.recalled:
            return {"why": "recalled", "where": None}       # it asked to re-read something: here it is
        news = [ev for eid, ev in sorted(st.contributions.items())
                if eid > p.last_seen and ev["actor"] != p.id and st.readable(ev, p.id)]
        if p.wake.get("addressed", True):
            hit = [ev for ev in news if p.id in (ev["payload"].get("to") or [])]
            if hit:
                return {"why": "addressed", "where": st.channel_key(hit[-1])}
        if p.wake.get("replies", True):
            mine = {eid for eid, ev in st.contributions.items() if ev["actor"] == p.id}
            hit = [ev for ev in news if ev["payload"].get("target") in mine]
            if hit:
                return {"why": "reply", "where": st.channel_key(hit[-1])}
        if p.wake.get("addressed", True):
            asks = [x for x in st.awaiting.values() if x["status"] == "waiting" and x["id"] > p.last_seen
                    and p.id in st.circle_needs(x) and p.id not in x["yes"] and p.id not in x["no"]]
            if asks:
                return {"why": "awaiting", "where": f"c:{asks[-1]['circle']}"}
        hit = [ev for ev in news if st.follows(p, ev)]
        if hit:
            return {"why": "news", "where": st.channel_key(hit[-1])}
        breath = float(p.wake.get("breath") or 0)
        if breath and now - max(p.last_wake_ts, p.joined_ts or 0.0) >= breath:
            return {"why": "breath", "where": None}
        return None

    def due(self, st: Optional[RoomState] = None, now: Optional[float] = None) -> List[tuple]:
        """Every model that would be woken now, and why. With a wake ceiling, only as many as it
        allows this minute, whoever has waited longest first."""
        st = st or self.state()
        now = time.time() if now is None else now
        if st.closed_at is not None or (st.runway or {}).get("ended"):
            return []                                        # a closed field, or funding ended: no one is woken
        out = []
        for p in st.reachable_members():
            if p.id not in self.seat_of or self._human_tempo(p.id):
                continue
            w = self.why_wake(st, p, now)
            if w:
                out.append((p.id, w))
        if self.wake_ceiling and out:
            self._ceiling_log = [t for t in self._ceiling_log if now - t < 60]
            out.sort(key=lambda d: st.presences[d[0]].last_wake_ts)
            out = out[:max(0, self.wake_ceiling - len(self._ceiling_log))]
        return out

    def _wake(self, pid: str, w: Dict[str, Any]):
        """Wake one model: show it everything new since it was last woken. The wake is recorded
        first (for the software; no participant reads it), so it is never woken twice for the same
        news. Returns (reply, error, where)."""
        st = self.state()
        p = st.presences[pid]
        c, seat = self.seat_of[pid]
        view = prompts.wake_view(st, p, w["why"], w.get("where"), limits=self.limits(),
                                 recalled=self.recalled.pop(pid, ""), witness=self.log.witness(),
                                 people_ids=set(self._people_ids()), context=self.recent_n,
                                 headlines=self.headlines, news_budget=self.news_budget,
                                 linger=self.linger, linger_budget=self.linger_budget)
        self.emit(ROOM, "wake", {"presence": pid, "upto": st.last_event, "why": w["why"]})
        if self.wake_ceiling:
            self._ceiling_log.append(time.time())
        try:
            reply = c.ask(seat, prompts.SYSTEM_MEMBER, [{"role": "user", "content": view}])
        except ConnectorError as e:
            return None, str(e), w.get("where")
        self._charge(pid, seat, reply)
        self._wake_costs = (self._wake_costs + [float(reply.cost_usd or 0.0)])[-20:]
        return reply, None, w.get("where")

    def _finish(self, pid: str, fut) -> None:
        """Apply what a woken model did, whenever it arrives: in its window, or later."""
        try:
            reply, err, where = fut.result()
        except Exception as e:                      # the thread itself failed
            reply, err, where = None, str(e), None
        try:
            p = self.state().presences.get(pid)
            if p is not None and p.state == IN:
                if err:
                    self.emit(pid, "connector_error", {"phase": "wake", "error": err})
                else:
                    self.emit(pid, "connector_ok", {})
                    if not is_no_reply(reply):
                        self._apply_reply(pid, reply.text, where)
        finally:
            self._busy.discard(pid)
            self._late.pop(pid, None)

    def _went_late(self, pid: str, fut) -> None:
        """Still thinking when the window closed. Its answer is applied when it arrives, not thrown
        away, and it is not woken again until then."""
        if pid in self._late:
            return
        self._late[pid] = fut
        self.emit(ROOM, "late", {"presence": pid, "window": self.window})

    def step(self) -> int:
        """Wake every model that is due now, together, and wait up to the window for their answers
        (a later answer is still applied when it arrives). Returns how many were woken. The
        scheduler (run) does this continuously; step is for walking a field by hand, and for tests."""
        st = self.state()
        due = self.due(st)
        if not due:
            return 0
        for pid, _ in due:
            self._busy.add(pid)
        ex = ThreadPoolExecutor(max(1, min(self.parallel, len(due))))
        futs = {ex.submit(self._wake, pid, w): pid for pid, w in due}
        done = set()
        try:
            for fut in as_completed(futs, timeout=self.window):
                done.add(fut)
                self._finish(futs[fut], fut)
        except (TimeoutError, FuturesTimeout):
            for fut, pid in futs.items():
                if fut in done:
                    continue
                if fut.done():                      # finished just as the window closed: not late at all
                    self._finish(pid, fut)
                elif fut.cancel():
                    self._busy.discard(pid)         # never started: it was not woken
                else:
                    self._went_late(pid, fut)
                    fut.add_done_callback(lambda f, pid=pid: self._finish(pid, f))
        finally:
            ex.shutdown(wait=False)
        self._runway()
        return len(due)

    # -- people post whenever they like --------------------------------------------------------
    def post(self, pid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """A person, or an agent holding a link, says or does something, whenever they like.
        `payload` is {"text": ...} in the plain grammar (hope/human.py) or an action object (or
        {"actions": [...]}); "channel" ("d:<path>" or "c:<circle id>") is where plain words go.
        Returns what was recorded, or why nothing was."""
        from .human import translate
        st = self.state()
        p = st.presences.get(pid)
        if not p or p.state != IN:
            return {"ok": False, "error": "only a member of the field can post"}
        where = payload.get("channel") or None
        if isinstance(payload.get("text"), str):
            act = translate(payload["text"])
        elif isinstance(payload.get("action"), str) or isinstance(payload.get("actions"), list):
            act = {k: v for k, v in payload.items() if k not in ("token", "channel", "turn_id")}
        else:
            return {"ok": False, "error": 'send {"text": "..."} or a JSON action object'}
        before = self.log.last_id()
        self._apply_reply(pid, json.dumps(act), where)
        mine = [e for e in self.log.iter(since=before) if e["actor"] == pid]
        why = [e["payload"].get("why") for e in mine if e["kind"] == "rejected"]
        done = [{"id": e["id"], "kind": e["kind"]} for e in mine if e["kind"] != "rejected"]
        return {"ok": bool(done) and not why, "recorded": done,
                "error": why[0] if why else (None if done else "nothing to record")}

    def person_view(self, pid: str, mark_seen: bool = True) -> str:
        """What a person sees on opening their page or asking to look: what is new since they last
        looked, first what answered them, then everything else. Looking is recorded for the
        software (no participant reads it), so "since you were last here" stays true."""
        st = self.state()
        p = st.presences[pid]
        text = prompts.person_view(st, p, catch_up=self._catch_up(st, p), limits=self.limits(),
                                   recalled=self.recalled.pop(pid, ""), witness=self.log.witness(),
                                   people_ids=set(self._people_ids()), context=self.recent_n,
                                   headlines=self.headlines, news_budget=self.news_budget,
                                   linger=self.linger, linger_budget=self.linger_budget)
        if mark_seen and st.last_event > p.last_seen:
            self.emit(ROOM, "seen", {"presence": pid, "upto": st.last_event})
        return text

    def _catch_up(self, st: RoomState, p) -> str:
        """For someone coming back: the tellings written since they last looked, or, if none covers
        it, a plain account made on the spot."""
        since = p.last_seen or p.joined_at or 0
        told = [t for t in st.tellings if t["upto"] > since]
        if told:
            return "\n\n".join(t["story"] for t in told[-4:])
        if st.last_event <= since:
            return ""
        d = digest(self.log, since, st.last_event)
        return mechanical_story(d)

    def _listen_to_people(self) -> None:
        """A person at this terminal speaks whenever they like: their lines are read as they come."""
        st = self.state()
        for pid, (c, seat) in list(self.seat_of.items()):
            p = st.presences.get(pid)
            if p and p.state == IN and pid not in self._listening and hasattr(c, "listen"):
                self._listening.add(pid)
                threading.Thread(target=c.listen, args=(self, pid), daemon=True, name=f"field-listen-{pid}").start()

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
        """Start a telling in the background once `tell_every` contributions have been written
        since the last, so no wake ever waits on the narrator."""
        if not self.narrator:
            return
        st = self.state()
        since = st.tellings[-1]["upto"] if st.tellings else 0
        if sum(1 for eid in st.contributions if eid > since) < self.tell_every:
            return
        if self._telling is not None and not self._telling.done():
            return
        if self._teller is None:
            self._teller = ThreadPoolExecutor(1, thread_name_prefix="field-narrator")
        self._telling = self._teller.submit(self.tell)

    # -- what a member sent ----------------------------------------------------------------
    def _apply_reply(self, pid: str, text: str, where: Optional[str] = None) -> int:
        """What a member sent: nothing (saying nothing writes nothing), plain words, or up to
        WAKE_ACTIONS actions, each in the channel it names. "next" says when to be woken next: a
        pause without words, which no participant reads. Returns the actions applied."""
        if not visible_text(text or ""):
            return 0
        acts = _parse_many(text)
        if acts is None:
            self._apply_action(pid, text, where)
            return 1
        n, nxt, real = 0, None, []
        for act in acts:
            nxt = act.pop("next", None) or nxt           # "next" is read wherever it is
            if act.get("action") not in ("quiet", "pass"):
                real.append(act)                         # saying nothing is not one of the few
        for act in real[:WAKE_ACTIONS]:
            self._apply_action(pid, json.dumps(act), where)
            n += 1
        if len(real) > WAKE_ACTIONS:
            self.emit(pid, "rejected", {"why": f"one wake carries at most {WAKE_ACTIONS} actions; the first "
                                               f"{WAKE_ACTIONS} were applied and the rest were not"})
        if nxt:
            self._apply_next(pid, nxt)
        return n

    def _apply_next(self, pid: str, nxt: Any) -> None:
        s = " ".join(str(nxt).lower().split())
        if s in ("addressed", "when addressed", "only when addressed", "until addressed"):
            act = {"action": "pause", "until": "addressed"}
        elif s in ("news", "when there is news", "until news"):
            act = {"action": "pause", "until": "news"}
        elif _seconds(s.replace("in ", "")) is not None:
            act = {"action": "pause", "for": s.replace("in ", "")}
        else:
            self.emit(pid, "rejected", {"why": "\"next\" is a length of time (such as 3h), \"addressed\", or \"news\""})
            return
        self._apply_action(pid, json.dumps(act))

    def _where(self, st: RoomState, pid: str, key: Optional[str]) -> Dict[str, Any]:
        """Where plain words, or a contribution naming no place, go: the channel the member was woken
        for or is looking at, if they may speak there, else their current domain."""
        pr = st.presences.get(pid)
        if key and key.startswith("c:"):
            c = st.circles.get(_int(key[2:]) or -1)
            if c and pid in c["members"] and c["dispersed_at"] is None:
                return {"circle": c["id"], "domain": ""}
        elif key and key.startswith("d:"):
            return {"domain": st.domain_display(key[2:])}
        return {"domain": (pr.domain if pr else "") or ""}

    def _apply_action(self, pid: str, text: str, where: Optional[str] = None) -> None:
        act = _parse(text)
        if not act:
            plain = visible_text(text)
            if plain and not plain.startswith(("{", "[", "`")) and re.search(r"[^\W\d_]", plain):
                # Plain words are a contribution, in the author's own words, as people would write
                # to anyone. Nothing else is guessed from prose: a sentence that begins "Remember"
                # or "Covenant" is still just something said. If it reads like another action, the
                # author is shown how to take it on their next turn (prompts.intent_hint).
                self.emit(pid, "contribute", {**self._where(self.state(), pid, where),
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

        if a in ("pass", "quiet"):
            return                              # saying nothing writes nothing
        elif a in CONTRIBUTION_KINDS:          # contribute; affirm/challenge are earlier versions' replies
            st = self.state()
            pr = st.presences.get(pid)
            payload = {"domain": labels.display(_label(act.get("domain"), DOMAIN_LIMIT)),
                       "content": _clean(act.get("content"), CONTRIBUTION_LIMIT)}
            if not payload["domain"] and act.get("circle") in (None, ""):
                payload.update(self._where(st, pid, where))     # no place named: where it was woken, or is looking
            said = _clean(act.get("said"), CONTRIBUTION_LIMIT) if act.get("plain") else ""
            if act.get("circle") not in (None, "") and said and _circle_ref(st, act.get("circle"))[0] is None:
                act = {k: v for k, v in act.items() if k != "circle"}       # "In short: ..." names no circle: words as written
                payload["content"] = said
                if not payload["domain"]:
                    payload.update(self._where(st, pid, where))
            if act.get("to") not in (None, "", []) and said and not _presences_ref(st, act.get("to"))[0]:
                act = {k: v for k, v in act.items() if k != "to"}           # "To be honest: ..." names no member
                payload["content"] = said
            if act.get("circle") not in (None, ""):
                c, why = _circle_ref(st, act.get("circle"))
                if c is None:
                    return reject(why)
                if pid not in c["members"]:
                    return reject(f"you are not in the circle {c['name']!r}. " + (
                        "It is private: knock to ask its members to let you in." if c["private"]
                        else "Join it first (join_circle); it is open to anyone."))
                payload["circle"], payload["domain"] = c["id"], ""
            if act.get("to") not in (None, "", []):
                to, unknown = _presences_ref(st, act.get("to"))
                if to:
                    payload["to"] = to
                if unknown:
                    payload["to_unknown"] = unknown[:10]    # kept, so the author can see who was not found
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
            note = _clean(act.get("note"), 300)
            if act.get("circle") not in (None, ""):
                c, why = _circle_ref(self.state(), act.get("circle"))
                if c is None:
                    return reject(why)
                if pid not in c["members"]:
                    return reject(f"only the members of {c['name']!r} write its covenant page")
                return self.emit(pid, "circle_covenant", {"circle": c["id"], "text": body, "note": note}) and None
            if _label(act.get("domain"), DOMAIN_LIMIT):
                dom = labels.display(_label(act.get("domain"), DOMAIN_LIMIT))
                return self.emit(pid, "domain_covenant", {"domain": dom, "text": body, "note": note}) and None
            self.emit(pid, "covenant", {"text": body, "note": note})
        elif a == "rest":
            # earlier versions' rest: a pause until there is news, with its reason as the pause's words
            return self._apply_action(pid, json.dumps({"action": "pause", "until": "news",
                                                       "note": _clean(act.get("reason"), 300)}))
        elif a == "move":
            self.emit(pid, "move", {"domain": labels.display(_label(act.get("domain"), DOMAIN_LIMIT))})
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
            src, dst = _label(act.get("from"), DOMAIN_LIMIT), labels.display(_label(act.get("to"), DOMAIN_LIMIT))
            if not src or not dst:
                return reject("relabel needs \"from\" (a label you used) and \"to\" (the label to move your entries to)")
            st = self.state()
            mine = sorted(eid for eid, ev in st.contributions.items() if ev["actor"] == pid and
                          labels.normalize(st.relabeled.get(eid) or ev["payload"].get("domain") or "") == labels.normalize(src))
            if not mine:
                return reject(f"you have no entries labelled {src!r}; only your own entries can be moved")
            self.emit(pid, "relabel", {"from": src, "to": dst, "entries": mine})
        elif a == "clock":
            return reject("the field has no clocks now: nobody takes turns, and each member chooses what wakes it "
                          "(wake) and when to pause (pause). How the field keeps time together is the field's to work out.")
        elif a in CHANNEL_ACTIONS:
            why = self._channel_action(pid, a, act)
            if why:
                return reject(why)
        elif a == "recall":
            q = _clean(act.get("query"), 200)
            where = _clean(act.get("from"), 20) or "briefing"
            if where not in RECALL_SOURCES:
                where = "briefing"
            passage, label = self._recall_from(self.state(), where, q)
            self.emit(pid, "recall", {"query": q, "from": where, "chars": len(passage), "found": bool(passage)})
            self.recalled[pid] = (f"From {label}:\n{passage}" if passage else f"(nothing in {label} matched {q!r})")

    # -- channels: domains and circles ------------------------------------------------
    def _channel_action(self, pid: str, a: str, act: dict) -> Optional[str]:
        """Following, pausing, and circles. Returns why nothing was done, or None if it was.
        Nobody is ever put anywhere: every join is the joiner's own act, every ask needs the yes of
        the one asked, and in a private circle every member's yes too. A no always has a reason."""
        st = self.state()
        if a in ("follow", "unfollow"):
            key, why = _channel_key_ref(st, act)
            if key is None:
                return why
            if a == "follow":
                c = st.circles.get(int(key[2:])) if key.startswith("c:") else None
                if c and c["private"] and pid not in c["members"]:
                    return f"{c['name']!r} is private; only its members read it. You can knock, or ask it a question (ask_circle)."
                if key in st.presences[pid].follows:
                    return "you already follow that"
            self.emit(pid, a, {"channel": key})
        elif a == "pause":
            payload = {"note": _clean(act.get("note"), 600)}
            secs = act.get("for", act.get("seconds"))
            if secs not in (None, ""):
                v = _seconds(secs)
                if v is None or v <= 0:
                    return "\"for\" is a length of time: 3600, 90m, 3h. Nothing was changed."
                payload["seconds"] = min(v, 365 * 86400)
            until = _clean(act.get("until"), 20).lower()
            if until:
                if until not in ("addressed", "news"):
                    return "\"until\" is \"addressed\" (someone names you or replies to you) or \"news\" (new words in what you follow, or in \"in\")"
                payload["until"] = until
            if act.get("in") not in (None, ""):
                key, why = _channel_key_ref(st, {"domain": act["in"]} if not isinstance(act["in"], dict) else act["in"])
                if key is None:
                    return why
                payload["until"], payload["in"] = "news", key
            self.emit(pid, "pause", payload)
        elif a == "wake":
            payload = {k: bool(act[k]) for k in ("addressed", "replies", "written") if isinstance(act.get(k), bool)}
            b = act.get("breath")
            if b not in (None, ""):
                if str(b).strip().lower() in ("never", "none", "0", "off"):
                    payload["breath"] = 0.0
                else:
                    v = _seconds(b)
                    lo, hi = BREATH_LIMITS
                    if v is None or not lo <= v <= hi:
                        return (f"\"breath\" is how long after nothing new you would like to be woken anyway: from "
                                f"{prompts.duration(lo)} to {prompts.duration(hi)}, or \"never\". Nothing was changed.")
                    payload["breath"] = v
            if not payload:
                return "wake takes \"addressed\", \"replies\" or \"written\" (true or false), or \"breath\" (a length of time, or \"never\")"
            self.emit(pid, "wake_pref", payload)
        elif a == "form_circle":
            name = _label(act.get("name"), 80)
            if not name:
                return "a circle needs a name"
            if any(labels.normalize(c["name"]) == labels.normalize(name) for c in st.live_circles()):
                return f"a circle named {name!r} already exists; join it, or choose another name"
            private = bool(act.get("private"))
            reason = _clean(act.get("reason"), REASON_LIMIT)
            if private and not reason:
                return ("a private circle says why it is private, as \"reason\". The reason is shown to everyone, "
                        "though the circle's words are not.")
            doms = act.get("domains", act.get("domain"))
            doms = [doms] if isinstance(doms, str) else list(doms or [])
            doms = [labels.display(_label(d, DOMAIN_LIMIT)) for d in doms if _label(d, DOMAIN_LIMIT)][:12]
            asked = act.get("ask")
            ev = self.emit(pid, "circle_form", {"name": name, "purpose": _clean(act.get("purpose"), 600), "domains": doms,
                                                "private": private, "reason": reason if private else ""})
            if asked not in (None, "", []):
                to, _ = _presences_ref(self.state(), asked)
                for who in to:
                    if who != pid:
                        self.emit(pid, "circle_ask", {"circle": ev["id"], "presence": who, "note": _clean(act.get("note"), 600)})
        elif a in ("join_circle", "leave_circle", "knock", "ask", "ask_circle", "privacy", "harvest", "quiet_for"):
            c, why = _circle_ref(st, act.get("circle"))
            if c is None:
                return why
            member = pid in c["members"]
            if c["dispersed_at"] is not None and a != "ask_circle":
                return f"{c['name']!r} has dispersed; its words stay, and a new circle can be formed"
            if a == "join_circle":
                if member:
                    return f"you are already in {c['name']!r}"
                if c["private"]:
                    return f"{c['name']!r} is private: knock to ask its members to let you in"
                self.emit(pid, "circle_join", {"circle": c["id"]})
            elif a == "leave_circle":
                if not member:
                    return f"you are not in {c['name']!r}"
                self.emit(pid, "circle_leave", {"circle": c["id"]})
            elif a == "knock":
                if member:
                    return f"you are already in {c['name']!r}"
                if not c["private"]:
                    return f"{c['name']!r} is open: join it (join_circle)"
                if _waiting_admission(st, c["id"], pid):
                    return f"your knock on {c['name']!r} is still waiting for its members' answers"
                self.emit(pid, "circle_knock", {"circle": c["id"], "note": _clean(act.get("note"), 600),
                                                "show_name": bool(act.get("show_name"))})
            elif a == "ask":
                if not member:
                    return f"only members of {c['name']!r} ask others into it"
                to, unknown = _presences_ref(st, act.get("who", act.get("presence")))
                if not to:
                    return f"no member named {', '.join(unknown) or 'anyone'}; ask by name or by id"
                for who in to:
                    if who in c["members"] or _waiting_admission(st, c["id"], who):
                        continue
                    self.emit(pid, "circle_ask", {"circle": c["id"], "presence": who, "note": _clean(act.get("note"), 600)})
            elif a == "ask_circle":
                text = _clean(act.get("question", act.get("text")), 1200)
                if not text:
                    return "a question to a circle needs words, as \"question\""
                self.emit(pid, "circle_question", {"circle": c["id"], "text": text})
            elif a == "privacy":
                if not member:
                    return f"only members of {c['name']!r} can change or explain its privacy"
                want = bool(act.get("private", c["private"]))
                reason = _clean(act.get("reason"), REASON_LIMIT)
                if want and not reason:
                    return "a private circle says why it is private, as \"reason\"; the reason is shown to everyone"
                self.emit(pid, "circle_privacy", {"circle": c["id"], "private": want, "reason": reason,
                                                  "change": want != c["private"]})
            elif a == "harvest":
                if not member:
                    return f"only members of {c['name']!r} write its harvest"
                text = _clean(act.get("text"), 100_000)
                if not text:
                    return "a harvest needs words: what the circle learned, as \"text\""
                if len(text) > HARVEST_LIMIT:
                    return f"a harvest holds at most {HARVEST_LIMIT} characters; this one has {len(text)}. Nothing was kept."
                self.emit(pid, "harvest", {"circle": c["id"], "text": text})
            elif a == "quiet_for":
                if not member:
                    return f"only members of {c['name']!r} set how long it may be quiet"
                v = _seconds(act.get("for", act.get("hours")))
                if v is not None and act.get("for") is None:
                    v *= 3600                         # "hours" is a number of hours
                lo, hi = QUIET_LIMITS
                if v is None or not lo * 3600 <= v <= hi * 3600:
                    return f"\"for\" is how long the circle may be quiet before it is told so: from {lo} hour to {hi // 24} days"
                self.emit(pid, "circle_quiet", {"circle": c["id"], "hours": v / 3600})
        elif a == "answer":
            prop = st.awaiting.get(_int(act.get("to", act.get("id"))) or -1)
            if not prop:
                return "no question waiting for an answer has that number"
            if prop["status"] != "waiting":
                return f"#{prop['id']} is no longer waiting ({prop['status']})"
            if pid not in st.circle_needs(prop):
                return f"#{prop['id']} does not wait for your answer"
            yes = act.get("yes")
            if not isinstance(yes, bool):
                return "answer with \"yes\": true, or \"yes\": false and a \"reason\""
            reason = _clean(act.get("reason", act.get("note")), REASON_LIMIT)
            if not yes and not reason:
                return "a no always has a reason, as \"reason\"; it is shown to whoever it concerns"
            self.emit(pid, "circle_answer", {"to": prop["id"], "yes": yes,
                                             **({"note": reason} if yes and reason else {}),
                                             **({"reason": reason} if not yes else {})})
        elif a == "reply_circle":
            qid = _int(act.get("question"))
            found = [(c, c["questions"][qid]) for c in st.circles.values() if qid in c["questions"]]
            if not found:
                return "no question to a circle has that number"
            c, _ = found[0]
            if pid not in c["members"]:
                return f"only members of {c['name']!r} answer questions put to it"
            text = _clean(act.get("text"), 1200)
            if not text:
                return "an answer needs words, as \"text\""
            self.emit(pid, "circle_reply", {"question": qid, "text": text})
        return None

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

    # -- the scheduler -----------------------------------------------------------------
    def run(self, seconds: float = 0.0, wakes: int = 0) -> None:
        """Wake models as each becomes due, until stopped (or for `seconds`, or `wakes` of them).
        People post whenever they like meanwhile. Nothing here sets a rhythm: a model is woken only
        for what it chose, no more often than the floor, and never while it is pausing."""
        st = self.state()
        paused = [d for d in st.declarations.values() if d["decision"] == "pause" and d["status"] == "carried_out"]
        if paused:
            d = paused[-1]
            last_wake = max((e["id"] for e in self.log.iter(since=d["answered_at"] or 0, kind="wake")), default=0)
            if not last_wake:
                self.alert(f"the field paused itself at declaration #{d['id']}: {d['text'][:300]!r}. "
                           f"Resume only as that declaration says.")
        pool = ThreadPoolExecutor(max(1, self.parallel), thread_name_prefix="field-wakes")
        inflight: Dict[Any, tuple] = {}
        started, n = time.time(), 0
        try:
            while not self._stop.is_set():
                self._ask_returners()
                self._listen_to_people()
                st = self.state()
                if st.closed_at is not None:
                    self.alert(f"the field decided to close (#{st.closed_at}); nothing runs. To undo a mistaken close: `reopen`.")
                    break
                if not st.members():
                    self.alert("no members remain; stopping")
                    break
                if st.runway and st.runway.get("ended"):
                    self.alert("the field's budget is spent and wakes have stopped. To continue, set a new --budget.")
                    break
                now = time.time()
                self._timers(st, now)
                for fut, (pid, t0) in list(inflight.items()):
                    if fut.done():
                        del inflight[fut]
                    elif now - t0 > self.window:
                        self._went_late(pid, fut)
                for pid, w in self.due(st, now):
                    if wakes and n >= wakes:
                        break
                    self._busy.add(pid)
                    fut = pool.submit(self._wake, pid, w)
                    fut.add_done_callback(lambda f, pid=pid: self._finish(pid, f))
                    inflight[fut] = (pid, now)
                    n += 1
                if self._runway():
                    break
                self._tell_if_due()
                self._checkpoint_if_due(now)
                if wakes and n >= wakes and not inflight:
                    break
                if seconds and now - started >= seconds:
                    break
                self._nudge.wait(self.tick)
                self._nudge.clear()
        finally:
            pool.shutdown(wait=False)       # an answer on its way is still applied when it arrives
            if self._telling is not None:
                try:
                    self._telling.result(timeout=120)   # let a telling in flight finish, so it is kept
                except Exception:
                    pass
            self.publish_checkpoint()

    def _checkpoint_if_due(self, now: float) -> None:
        if self.publish_checkpoints and now - self._last_checkpoint >= 60:
            self._last_checkpoint = now
            self.publish_checkpoint()

    def _timers(self, st: RoomState, now: float) -> None:
        """The two things the software says of its own accord, each once per stretch: that a circle
        has been quiet for its quiet length, and, every PRIVACY_EVERY, asking a private circle to say
        again why it stays private. Neither wakes anyone; members see them when they next look."""
        for c in st.live_circles():
            if not c["cold_told"] and now - (c["last_words_ts"] or c["ts"]) >= c["quiet_hours"] * 3600:
                self.emit(ROOM, "circle_cold", {"circle": c["id"]})
            if c["private"] and now - max(c["reason_at"] or c["ts"], c["privacy_asked_at"] or 0.0) >= PRIVACY_EVERY:
                self.emit(ROOM, "circle_privacy_asked", {"circle": c["id"]})

    def _ask_returners(self) -> None:
        """Former members who have been asked back are asked the entry question now, beside
        everything else, so the field need not stop for them. (Someone who declined and is asked
        back goes through the invitation and the briefing again, with the pause between, when the
        operator opens the gates.)"""
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

    # -- the runway ---------------------------------------------------------------
    def _wake_estimate(self) -> float:
        recent = [c for c in self._wake_costs[-10:] if c > 0]
        if not recent:
            rows = self.log.conn.execute("select cost_usd from ledger where cost_usd > 0 order by id desc limit 10").fetchall()
            recent = [r[0] for r in rows]
        return max(recent) * WAKE_COST_MARGIN if recent else 0.0

    def _spend_rate(self) -> float:
        """USD an hour over the last day, or since spending began if that is shorter (at least ten
        minutes are counted, so a burst at the start does not read as a flood)."""
        now = time.time()
        total, first = self.log.spent_since(now - 86400)
        if not total or first is None:
            return 0.0
        return total / (max(600.0, now - first) / 3600.0)

    def _runway(self) -> bool:
        """Tell the field when its funding is running low, hold back a closing wake for every model
        not pausing, and end the wakes once those have been given. Returns True when wakes must stop.

        Nothing here says anything in dollars to the field. It speaks in time at the current rate,
        and only when the end is near, so money is not a standing topic. A free field, or one with
        no budget, is never told anything by this."""
        st = self.state()
        if not st.budget:
            return False
        rw = st.runway or {}
        if rw.get("ended"):
            return True
        now = time.time()
        models = [p for p in st.reachable_members() if p.id in self.seat_of and not self._human_tempo(p.id)]
        if rw.get("closing"):
            waiting = [p for p in models if p.last_wake_ts < float(rw.get("at") or 0) and not self._paused(p, now)]
            if not waiting and not self._busy and not self._late:
                self.emit(ROOM, "runway", {"ended": True, "at": now})
                self.alert(f"every model has had its closing wake and the budget of ${st.budget:.2f} is spent; wakes stop here.")
                return True
            return False
        per = self._wake_estimate()
        if per <= 0 or not models:
            return False
        left = st.budget - self.log.total_cost()
        if left <= per * len(models):
            if left < per:
                self.emit(ROOM, "runway", {"ended": True, "abrupt": True, "at": now})
                self.alert("the budget cannot pay for another wake, so there is no closing wake to give; wakes stop here.")
                return True
            self.emit(ROOM, "runway", {"closing": True, "at": now})
            self.alert("the funding left pays for one more wake for each model: each is woken once more, told it is the last.")
            return False
        rate = self._spend_rate()
        if rate <= 0:
            return False
        hours = (left - per * len(models)) / rate
        marks = sorted({m for m in (self.runway_notice, 6.0, 1.0) if m <= self.runway_notice}, reverse=True)
        reached = [m for m in marks if hours <= m]
        if reached and (rw.get("mark") is None or min(reached) < rw["mark"]):
            self.emit(ROOM, "runway", {"hours_left": round(hours, 1), "mark": min(reached), "at": now})
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


def _parse_many(text: str) -> Optional[List[dict]]:
    """Up to a few actions: one JSON object, a list of them, or {"actions": [...]} (with an optional
    "next"). None when the text is not JSON at all: then it is plain words, or outside the format."""
    t = (text or "").strip()
    try:
        d = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        one = _parse(t)
        return [one] if one else None
    if isinstance(d, list):
        items = d
    elif isinstance(d, dict) and isinstance(d.get("actions"), list):
        items = list(d["actions"])
        if d.get("next"):
            items.append({"action": "quiet", "next": d["next"]})
    elif isinstance(d, dict):
        one = _parse(t)
        return [one] if one else None
    else:
        return None
    out = []
    for x in items:
        if isinstance(x, dict) and isinstance(x.get("action"), str):
            out.append(x)
        elif isinstance(x, dict) and isinstance(x.get("action"), list) and len(x["action"]) == 1:
            out.append({**x, "action": str(x["action"][0])})
    return out


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


CHANNEL_ACTIONS = {"follow", "unfollow", "pause", "wake", "form_circle", "join_circle", "leave_circle", "ask",
                   "knock", "answer", "ask_circle", "reply_circle", "privacy", "harvest", "quiet_for"}


def _circle_ref(st: RoomState, v: Any):
    """A circle by its number (12, "#12") or its name. Returns (circle, None) or (None, why)."""
    if v in (None, ""):
        return None, "name the circle, as \"circle\" (its name or its number)"
    n = _int(str(v).strip().lstrip("#"))
    if n is not None and n in st.circles:
        return st.circles[n], None
    want = labels.normalize(str(v))
    live = [c for c in st.live_circles() if labels.normalize(c["name"]) == want]
    gone = [c for c in st.circles.values() if labels.normalize(c["name"]) == want]
    if live or gone:
        return (live or gone)[-1], None
    return None, f"there is no circle called {str(v)[:80]!r}"


def _presences_ref(st: RoomState, v: Any):
    """Members named by id or by name (one or a list). Returns (ids, names not found)."""
    items = v if isinstance(v, list) else [v]
    found, unknown = [], []
    for item in items[:20]:
        x = " ".join(str(item or "").split()).lstrip("@")
        if not x:
            continue
        if x in st.presences:
            hit = [x]
        else:
            hit = [p.id for p in st.presences.values() if p.name.lower() == x.lower() and p.state == IN]
        if len(hit) == 1:
            if hit[0] not in found:
                found.append(hit[0])
        else:
            unknown.append(x[:80])
    return found, unknown


def _channel_key_ref(st: RoomState, act: dict):
    """A channel named by "circle" or "domain" ("" or "the field" is the root)."""
    if act.get("circle") not in (None, ""):
        c, why = _circle_ref(st, act["circle"])
        return (f"c:{c['id']}", None) if c else (None, why)
    d = act.get("domain")
    if d is None:
        return None, "name a \"domain\" (\"\" for the field itself) or a \"circle\""
    d = _label(d, DOMAIN_LIMIT)
    if d.lower() in ("the field", "field", "root", "/"):
        d = ""
    return "d:" + labels.path(d), None


def _waiting_admission(st: RoomState, cid: int, pid: str) -> bool:
    return any(x["circle"] == cid and x["kind"] == "admit" and x["subject"] == pid and x["status"] == "waiting"
               for x in st.awaiting.values())


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
