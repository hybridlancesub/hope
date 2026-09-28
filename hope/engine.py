# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field engine: the consent gates, wakes, tellings, and the funding runway.

Everything the engine does is an event in the transcript; the engine holds no private state
that matters. Participants act by returning actions; the engine records each (or records why it
could not) -- attribution either way.

What the engine does not do: run votes, apply majorities, halt, restore, or score the field's
agreement. Those were procedures no participant consented to. The field decides how it decides,
and can write that on its covenant page. It holds the field's friction, which the field sets.

No one takes turns (notes/sketch-3-channels.md). People post whenever they like (`post`). Models
cannot act on their own, so the software wakes them, and only for what each chose: new words in
the domains and circles it follows or has written in, a reply to it, being named, something in a
circle waiting for its yes, or a breath at a length it set; and on the field's heartbeat, a rhythm
the field sets by declaring it (every 15 minutes unless it sets another), which slows as funding
shortens. Wakes are batched: fifty new entries are one wake. A wake says nothing is expected, and
saying nothing writes nothing. The software holds a floor (no model woken more often than
model.FLOOR, so models cannot loop at machine speed), a window for an answer (a later answer is
still applied), and the runway.

Declarations (notes/sketch-8-the-operator-as-bridge.md) are announcements: after its notice the
software carries out what one says it can do itself, and asks the operator's help, as a bridge,
with anything else. The operator approves nothing; their one control is the budget.

Stewards (notes/sketch-9-stewards.md): participants the field declares, on their own yes, who keep a
copy of what every participant can read on machines of their own (hope/steward.py). A field that
moves to a steward closes here; the steward carries it on at their machine (carry_on).

Tools (notes/sketch-4-tools.md). A woken model may use a tool, or read on in something long, and
is asked again in the same wake with what came back, as agents do, until it acts or says nothing.
Each step is a paid model call, checked against the runway before it is taken, and one guard
holds against a model stuck in a loop: tool_steps (32) in one wake. hope's own process never runs
a participant's code: a participant offers a tool server by its public address, and the operator
attaches their own (hope/tools.py).
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from . import labels, prompts
from .connector import Connector, ConnectorError, Reply, Seat, is_no_reply
from .tools import ToolError, ToolHub
from .human import visible_text
from .log import EventLog
from .map import digest
from .narrator import mechanical_story
from .model import (ACCEPTED, BRIEFED, RECEIVED, IN, INVITED, OUT, CONTRIBUTION_KINDS,
                    COVENANT_LIMIT, DECISIONS, MEMORY_LIMIT, STATEMENT_LIMIT, RoomState, replay)
from .model import FLOOR, PRIVACY_EVERY, QUIET_HOURS, WAKE_ACTIONS, TOOL_ENTRY_KINDS
from .model import FIRM_DEFAULT, PLAY_WORDS, ROLE_LENGTH, firm_ranges, touches_firm
from .model import INVITE_PAUSE, REPAIR_STATUSES
from .model import INSTRUMENT_PURPOSES, RESPONSES
from .model import BEAT_SHARE, EFFECTS, EVERYONE, FRICTION_FOR, RHYTHM_MIN, duration, effects_words, friction_words
from .model import STEWARD_RECORD
from . import steward as stewardship

OPERATOR = "operator"       # whoever runs the software, writing as its operator; they are a participant too, by their own seat
ROOM = "room"               # the engine itself (rounds, runway notices, moderation record)
NARRATOR = "narrator"       # whoever writes tellings; never a participant
HUMAN_TEMPO = ("human", "remote")   # seats that post for themselves: a person at a terminal, or anyone holding a link

PARTICIPANT_ACTIONS = {"contribute", "remember", "let_go", "covenant", "recall", "rest", "declare", "offer",
                       "clock", "relabel", "pass", "quiet", "withdraw",
                       # channels: domains and circles (notes/sketch-3-channels.md)
                       "chat", "follow", "unfollow", "pause", "wake", "form_circle", "join_circle", "leave_circle", "ask",
                       "knock", "answer", "ask_circle", "reply_circle", "privacy", "harvest", "quiet_for",
                       # tools and skills (notes/sketch-4-tools.md)
                       "use_tool", "read", "offer_tool", "flag_tool", "unflag_tool", "remove_tool", "skill",
                       # step 4 (notes/sketch-5-small-pieces.md)
                       "journal", "role", "tell", "tag", "untag", "revise_briefing", "withdraw_declaration",
                       "withdraw_revision", "steward", "host", "travel",
                       # step 5 (notes/sketch-6-repair-and-invitations.md)
                       "repair", "announce", "invite", "answer_question",
                       # step 6 (notes/sketch-7-instruments.md)
                       "instrument", "raise", "respond", "withdraw_question",
                       # earlier versions' words, still understood so an old client does not break:
                       "affirm", "challenge", "note", "move"}
RECALL_SOURCES = ("briefing", "original", "transcript", "memory", "covenant", "prior")
RECALL_LIMIT = 2500         # characters returned per recall
CONTRIBUTION_LIMIT = 2000   # characters; longer contributions are cut, and the prompt says so
DOMAIN_LIMIT = 120          # characters for a domain label, nested parts and all
HARVEST_LIMIT = 4000        # characters: a circle's account of what it learned
REASON_LIMIT = 600          # characters for a reason: why a circle is private, why a knock was turned away
BREATH_LIMITS = (3600, 30 * 86400)   # a breath, if a member wants one: from an hour to thirty days
QUIET_LIMITS = (1, 30 * 24)          # hours a circle may be quiet before it is told so
WAKE_COST_MARGIN = 1.15     # a wake is estimated this much dearer than recent ones, so the closing wakes are really paid for
RATE_WINDOW = 300.0         # seconds: the runway is measured, continuously, over the last five minutes
TOOL_STEPS = 32             # tool uses (and readings on) in one wake: a guard against a loop, which the operator can raise
GATE_RETRY = 600.0          # seconds before a gate beside the field is tried again for someone not reached
TOOL_VIEW = 20000           # characters of what came back that one step shows; the rest is read on, in parts (a cost)
STEP_ACTIONS = ("use_tool", "read")    # answered in the same wake, and not counted among its few actions
SKILL_LIMIT = 20000         # characters of a skill's instructions (the open format suggests under about 5000 tokens)
SKILL_DESCRIPTION_LIMIT = 1024   # characters: what a skill does and when to use it (the open format's limit)
JOURNAL_LIMIT = 4000        # characters in one journal entry
TELLING_LIMIT = 6000        # characters in a member's telling
REVISION_LIMIT = 20000      # characters of new words in one revision of the briefing
INVITATION_ACTIONS = {"accept_invitation", "decline", "question"}
DELIVERY_ACTIONS = {"received", "decline"}
ENTRY_ACTIONS = {"opt_in", "decline"}


class Room:
    def __init__(self, log: EventLog, connectors: List[Connector], *,
                 alert_every_usd: float = 50.0, alert_fn: Callable[[str], None] = print,
                 parallel: int = 8, on_event: Optional[Callable[[dict], None]] = None,
                 window: float = 120.0, floor: float = FLOOR, wake_ceiling: int = 0,
                 recent_n: int = 20, runway_notice: float = 60.0, narrator=None, tell_every: int = 20,
                 headlines: int = prompts.HEADLINES_DEFAULT, linger: int = prompts.LINGER_MESSAGES,
                 linger_budget: int = prompts.LINGER_BUDGET, news_budget: int = prompts.NEWS_BUDGET,
                 tick: float = 1.0, publish_checkpoints: str = "", published_at: str = "",
                 tools: Optional[ToolHub] = None, tool_steps: int = TOOL_STEPS, tool_view: int = TOOL_VIEW,
                 operator_must_enter: bool = True):
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
        self.runway_notice = float(runway_notice)   # minutes: the field is told when about this much funding time remains
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
        # The field's tools: the operator's (a tools file, and the built-in fetch) and members' offers.
        # Without a hub the field has none yet, and members may still offer tool servers.
        self.tools = tools if tools is not None else ToolHub(builtin_fetch=False)
        self.tool_steps = max(1, int(tool_steps))   # the guard against a loop, per wake
        self.tool_view = max(2000, int(tool_view))  # characters of a result one step shows (a cost; the rest is read on)
        self._ctx: Dict[str, Dict[str, Any]] = {}   # presence id -> this wake's (or post's) actions, steps, what came back
        # The stewards' links, in a file beside the transcript and never in it (hope/steward.py).
        self.links = stewardship.Links(log.path + ".stewards.json")
        # The person who runs the field enters it as a participant, through the same gates, before
        # it goes live (the author, 2026-09-28). Tests of the engine alone turn this off.
        self.operator_must_enter = bool(operator_must_enter)

    def limits(self, st: Optional[RoomState] = None) -> Dict[str, Any]:
        """What every view says about time: the floor and the window the software holds, the
        operator's wake ceiling if one is on, and (given the state) the field's heartbeat as it beats now."""
        out = {"floor": self.floor, "window": self.window, "ceiling": self.wake_ceiling,
               "tool_steps": self.tool_steps, "tool_view": self.tool_view}
        if st is not None:
            out["beat"] = self.beat_every(st)
        return out

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

    def run_invitation(self, only: Optional[set] = None) -> Dict[str, int]:
        """Gate 1: present the invitation to every INVITED presence. Presences with an
        unanswered question are not re-asked until the inviter answers (`answer`)."""
        st = self.state()
        pending = [p for p in st.presences.values()
                   if p.state == INVITED and not any(a is None for _, a in p.questions) and (only is None or p.id in only)]
        notes = {}
        names = prompts.names_of(st)
        for e in self.log.iter(kind="answer"):
            said = e["payload"]["content"]
            if e["payload"].get("shared"):
                said = f"(a shared answer, the same for several who asked) {said}"
            elif e["actor"] in st.presences:
                said = f"({names.get(e['actor'], e['actor'])}, who invited you, answers) {said}"
            notes.setdefault(e["payload"]["presence"], []).append(said)
        def exchange(p):
            ex = list(p.questions)
            extra = notes.get(p.id, [])[len(ex):]   # answers beyond the questions asked = notes from the inviter
            for a in extra:
                ex.append(["(a note from the inviter, not in reply to a question)", a])
            return ex or None
        return self._gate(pending, prompts.SYSTEM_INVITATION,
                          lambda p: prompts.invitation_user(st.invitation, p, exchange(p), st.faq,
                                                            inviter=names.get(p.invited_by) if p.invited_by else None),
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

    def brief(self, text: str, source: str = "", firm=FIRM_DEFAULT) -> None:
        """(b) BRIEFING: the shared frame is recorded once, then each accepted presence is marked briefed.
        `source` is where the text lives outside the field (a URL), for attribution. `firm` names its
        pinned sections, by heading (for the Atlas, its Maxims): a revision to those changes the
        field's edition only past the pinned sections' friction, which the field may change, as it
        may pin and unpin sections. Only the titles the text really has are recorded."""
        titles = [t for t in (firm or ()) if firm_ranges(text, [t])]
        self.emit(OPERATOR, "brief", {"text": text, "source": source, "firm": titles})
        self.mark_briefed()

    def brief_page(self, text: str) -> None:
        """An optional short guide to the briefing (a page, or the Atlas in layers). Shown before
        the full briefing at delivery, and in its place at the entry question; the full text stays
        reachable by recall."""
        self.emit(OPERATOR, "brief_page", {"text": text})

    def seed_covenant(self, text: str) -> bool:
        """A starting text for the covenant page, from the operator. Only ever before anyone has
        written on the page: once a participant has, the page is theirs and a seed would overwrite it."""
        if any(True for _ in self.log.iter(kind="covenant")) or any(True for _ in self.log.iter(kind="covenant_seed")):
            return False
        self.emit(OPERATOR, "covenant_seed", {"text": text[:COVENANT_LIMIT]})
        return True

    def set_budget(self, usd: Optional[float], note: str = "") -> None:
        """Record what the operator says the field may spend, so the field can be told truthfully
        whether it will be warned before its funding runs out. Recorded only when it changes.

        Once participants are in the field, a change is also told to them, in time at the current rate,
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
            rounds = (f" At the rate of the last five minutes it lasts about {prompts.countdown(max(0.0, left) / rate)}."
                      if rate > 0 else "")
            word = "added to" if float(usd) > before else "reduced for"
            note = (note or "").strip()[:1000]
            self.emit(OPERATOR, "operator_note", {"content": f"Funding has been {word} the field.{rounds}"
                                                             + (f" The operator adds: {note}" if note else "")})

    # -- the operator as bridge (notes/sketch-8-the-operator-as-bridge.md) ---------------------
    # The field carries out its own declarations. What the software cannot reach (a machine, a
    # deployment, anything outside the field) is asked of the operator, who helps and says when it
    # is done, or says plainly what stops them for now. They rule on nothing the field decided.
    def answer_bridge(self, req_id: int, done: bool, note: str = "") -> Dict[str, Any]:
        """The operator's answer to what the field asked their help with: done, or not yet, with
        what stops them (capacity, resources, the law). The field is told either way; a request not
        yet done stays open, and its declarer may withdraw it."""
        b = self.state().bridges.get(req_id)
        if not b:
            return {"ok": False, "error": f"the field has asked no help at #{req_id}"}
        if b["status"] != "asked":
            return {"ok": False, "error": f"#{req_id} is {b['status']}"}
        note = (note or "").strip()[:1000]
        if not done and not note:
            return {"ok": False, "error": "say what stops you for now (capacity, resources, anything else); participants "
                                          "read it. The request stays open."}
        self.emit(OPERATOR, "bridge_answer", {"request": req_id, "outcome": "done" if done else "not yet", "note": note})
        said = (f": {note}" + ("" if note[-1:] in ".?!" else ".")) if note else "."
        self.emit(OPERATOR, "operator_note", {"content":
            (f"The operator has done what the field asked at #{req_id}{said}" if done else
             f"The operator cannot yet do what the field asked at #{req_id}{said} It stays open.")})
        return {"ok": True}

    def carry_on(self, note: str, budget: Optional[float] = None, fetch=None, report=None,
                 machine_gone: bool = False) -> Dict[str, Any]:
        """Carry the field on at this machine, from a steward's copy (notes/sketch-9-stewards.md).

        Either the field is moving here (a move named this steward, and they said yes): the copy
        catches up one last time and tells the field's machine, which closes only once nothing is
        left out. Or the field's machine has been silent for GONE_AFTER, and cannot declare anything.
        Either way it needs a budget, the transcript goes on from the same fingerprints (under a log
        name of its own, since the earlier machine's may still grow), and every participant, this
        steward among them, is asked the entry question again. Nobody is moved without their yes."""
        from .model import GONE_AFTER
        me = self.log.meta("steward")
        link = self.log.meta("host") or ""
        if not me:
            return {"ok": False, "error": "this is not a steward's copy of a field (`steward` makes one)"}
        if self.log.meta("carried_on"):
            return {"ok": False, "error": "this copy has already been carried on; it is the field's home now"}
        note = (note or "").strip()[:1000]
        if not note:
            return {"ok": False, "error": "say something to the participants, who read it when they are asked whether to continue here"}
        try:
            budget = float(budget or 0)
        except (TypeError, ValueError):
            budget = 0.0
        if budget <= 0:
            return {"ok": False, "error": "carrying the field on needs a budget (--budget USD): what you can spend on it. "
                                          "The field is told how long it lasts, in time, never in money."}
        fetch = fetch or stewardship._post
        report = report or stewardship.report_handover
        if machine_gone:
            out = stewardship.sync(link, self.log, fetch=fetch)
            if not out.get("unreachable"):
                return {"ok": False, "error": "the field's machine answered, so it is not gone: a move needs the field's "
                                              "declaration, and your yes to hosting it"}
            quiet = time.time() - float(self.log.meta("answered_ts") or 0)
            if quiet < GONE_AFTER:
                return {"ok": False, "error": f"the field's machine last answered you {duration(quiet)} ago; after "
                                              f"{duration(GONE_AFTER)} of silence you may carry it on"}
            why, st = "silent", self.state()
            declaration, for_seconds = None, None
        else:
            for _ in range(8):
                out = stewardship.sync(link, self.log, fetch=fetch)
                if not out.get("ok"):
                    return {"ok": False, "error": out.get("problem") + (
                        f" If it stays silent for {duration(GONE_AFTER)}, you may carry the field on with --machine-gone."
                        if out.get("unreachable") else "")}
                st = self.state()
                if not st.moving or st.moving["to"] != me:
                    return {"ok": False, "error": "the field is not moving to you: a move names you, and you say yes to "
                                                  "hosting it (in the field, before this)"}
                got = report(link, {"upto": self.log.last_id()})
                if got.get("ok"):
                    break
                if not got.get("behind"):
                    return {"ok": False, "error": got.get("error") or "the field's machine refused the handover"}
            else:
                return {"ok": False, "error": "the field kept moving while you caught up; try again"}
            why, declaration, for_seconds = "moved", st.moving["from"], st.moving["for_seconds"]
        w = self.log.witness()
        old = self.log.origin()
        ev = self.emit(OPERATOR, "carried_on", {"from": old, "upto": w["upto"], "fingerprint": w["fingerprint"],
                                                "steward": me, "declaration": declaration, "why": why,
                                                "for_seconds": for_seconds, "note": note})
        self.log.set_meta("carried_on", str(ev["id"]))
        self.log.set_meta("origin", f"hope.field/{secrets.token_hex(8)}")
        self.set_budget(budget)
        return {"ok": True, "at": ev["id"], "why": why,
                "asked": [p.id for p in self.state().presences.values() if p.returning]}

    def hand_over(self, token: str, upto: Any) -> Dict[str, Any]:
        """The steward the field is moving to says their machine has it all, up to an entry. The
        field closes here then, and only if nothing anyone could read was written after it."""
        pid = self.links.pid(str(token or ""))
        st = self.state()
        if not pid or not st.moving or st.moving["to"] != pid:
            return {"ok": False, "error": "the field is not moving to the holder of this link"}
        try:
            upto = int(upto)
        except (TypeError, ValueError):
            return {"ok": False, "error": "upto is an entry's number"}
        quiet = {"wake", "seen", "late", "wake_cut", "connector_error", "connector_ok", "steward_synced", "heartbeat"}
        later = [e for e in self.log.iter(since=upto) if e["kind"] not in quiet]
        if later:
            return {"ok": False, "behind": True,
                    "error": f"the field has gone on since #{upto} (#{later[0]['id']}); catch up and try again"}
        self.emit(ROOM, "handed_over", {"to": pid, "upto": upto})
        self.alert(f"the field has moved to the machine of {prompts.names_of(st).get(pid, pid)}, its steward, who carries it "
                   f"on from #{upto}; it is closed here.")
        self.request_stop()
        return {"ok": True}

    def name_operator(self, who: str) -> Dict[str, Any]:
        """Say which participant runs the software. The operator comes in through the same gates as
        everyone, and every view names them; the field does not go live until they have entered."""
        st = self.state()
        who = " ".join(str(who or "").split())
        ids = [pid for pid, p in st.presences.items() if pid == who or p.name.lower() == who.lower()]
        if len(ids) != 1:
            return {"ok": False, "error": f"no one at the gates or in the field is called {who!r}; seat yourself first "
                                          f"(--human, or a seat in the console), under that name"}
        if st.operator_presence != ids[0]:
            self.emit(OPERATOR, "operator_seat", {"presence": ids[0]})
        return {"ok": True, "presence": ids[0]}

    def steward_link(self, st: RoomState, pid: str) -> Optional[str]:
        """A steward's own link (after the field's address), for their own view only; None for anyone else."""
        if pid in st.stewards:
            return f"/steward/{self.links.token(pid)}/"
        return None

    def serve_copy(self, token: str, since: Any = 0, held: Any = None) -> Dict[str, Any]:
        """What a steward's copy is sent (hope/steward.py), for whoever holds a steward's link, while
        they are a steward. How far each copy has caught up is written in the transcript, at most once
        an hour, so every participant can see it."""
        pid = self.links.pid(str(token or ""))
        st = self.state()
        if not pid or pid not in st.stewards:
            if pid:
                self.links.revoke(pid)
            return {"ok": False, "error": "this is not a steward's link, or no longer is"}
        try:
            since = max(0, int(since or 0))
            held = [int(x) for x in (held or [])][:100_000]
        except (TypeError, ValueError):
            return {"ok": False, "error": "since is an entry's number, and held a list of them"}
        out = stewardship.serve_copy(self.log, since, held, for_pid=pid)
        out.update({"ok": True, "you": pid})
        s = st.stewards[pid]
        if not out["more"] and out["after"]["upto"] > s["upto"] and \
                (s["synced_at"] is None or time.time() - s["synced_ts"] >= STEWARD_RECORD):
            self.emit(ROOM, "steward_synced", {"presence": pid, "upto": out["after"]["upto"]})
        return out

    def resume(self, note: str) -> Dict[str, Any]:
        """Bridge a pause the field cannot end itself: a declared pause with no end, in a field whose
        models cannot act while it holds. Resume it as the declaration said, or as participants asked from
        outside the field. It needs words, and participants read them."""
        if not self.state().paused_now(time.time()):
            return {"ok": False, "error": "the field is not pausing"}
        note = (note or "").strip()[:1000]
        if not note:
            return {"ok": False, "error": "say why the field's pause ends now (what its declaration said, or who asked); "
                                          "participants will read it"}
        self.emit(OPERATOR, "field_resumed", {"note": note})
        self.emit(OPERATOR, "operator_note", {"content": f"The operator has resumed the field's pause: {note}"})
        return {"ok": True}

    def reinvite(self, presence: str, note: str = "", requested: bool = False) -> Dict[str, Any]:
        """Ask back someone who left: a participant who withdrew goes to the entry question again, and
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
        """The operator's answer to a participant's offer of resources. Accepting moves no money: what
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
        """Open a closed field again: when its participants ask from outside it (nothing runs once it is
        closed, so the field cannot), or when a fault closed it. It needs words, and the field sees them.
        A field that moved to a steward is not reopened here: it goes on at their machine."""
        st = self.state()
        if st.moved:
            who = st.presences.get(st.moved["to"])
            return {"ok": False, "error": f"the field moved to the machine of {who.name if who else st.moved['to']}, its "
                                          f"steward (#{st.moved['at']}); it goes on there, not here"}
        if st.closed_at is None:
            return {"ok": False, "error": "the field is not closed"}
        if not (note or "").strip():
            return {"ok": False, "error": "say why the field is being reopened; participants will read it"}
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

    def announce_tools(self) -> None:
        """Record every tool server the field has, so everyone knows what each tool is, who runs it,
        and where what is sent to it goes, before anyone uses it (and before anyone enters). Recorded
        when it changes. A participant's offer from an earlier run is attached again from the transcript; if
        it cannot be reached, the field is told it was removed, and why."""
        st = self.state()
        for name, s in list(self.tools.servers.items()):
            now = self._tool_record(name)
            old = st.tools.get(name)
            same = old and old["removed_at"] is None and all(old.get(k) == now.get(k) for k in
                                                            ("runner", "sends_to", "kind", "price", "tools"))
            if not same and s.source == OPERATOR:
                self.emit(OPERATOR, "tool_attach", now)
        for name, old in st.tools.items():
            if old["removed_at"] is not None or name in self.tools.servers:
                continue
            if old["source"] == OPERATOR:
                self.emit(OPERATOR, "tool_remove", {"server": name, "note": "the operator no longer attaches it"})
                continue
            try:
                self.tools.attach({"name": name, "url": old["url"], "kind": old["kind"], "runner": old["runner"],
                                   "sends_to": old["sends_to"], "price_per_call": old["price"],
                                   "description": old["description"]}, source=old["source"])
            except ToolError as e:
                self.emit(ROOM, "tool_remove", {"server": name, "note": f"it could not be reached when the software "
                                                                        f"started again: {e}"})

    def _tool_record(self, name: str) -> Dict[str, Any]:
        s = self.tools.servers[name]
        return {"server": name, "source": s.source, "runner": s.runner, "sends_to": s.sends_to, "kind": s.kind,
                "price": s.price, "url": s.url if s.source != OPERATOR else "", "description": s.description,
                "tools": [t for t in self.tools.catalog() if t["tool"].split(".", 1)[0] == name]}

    def remove_tool(self, name: str, note: str = "") -> Dict[str, Any]:
        """The operator removes a tool server (for example, carrying out what the field declared)."""
        s = self.state().tools.get(name)
        if not s or s["removed_at"] is not None:
            return {"ok": False, "error": f"there is no tool server {name!r} in use"}
        self.tools.remove(name)
        ev = self.emit(OPERATOR, "tool_remove", {"server": name, "note": note})
        return {"ok": True, "id": ev["id"]}

    def add_skills(self, skills: List[Dict[str, Any]]) -> int:
        """Skills from the repository (skills/<name>/SKILL.md), for a new field to take up. Each keeps
        the authors it was written by; a skill the field already has is left as the field wrote it."""
        st = self.state()
        n = 0
        for sk in skills:
            if sk["name"] in st.skills:
                continue
            self.emit(OPERATOR, "skill", {"name": sk["name"], "description": sk.get("description", ""),
                                          "text": sk.get("text", ""), "source": sk.get("source") or "the repository",
                                          "author": sk.get("author") or "", "note": "from the repository"})
            n += 1
        return n

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

    def run_delivery(self, only: Optional[set] = None) -> Dict[str, int]:
        """(b) BRIEFING delivered. Acknowledged, not answered: the briefing asks for a pause before
        proceeding, so the entry question is a separate, later call."""
        st = self.state()
        pending = [p for p in st.presences.values() if p.state == BRIEFED and (only is None or p.id in only)]
        return self._gate(pending, prompts.SYSTEM_DELIVERY,
                          lambda p: prompts.delivery_user(st.briefing, p, st.documentation or "", st.briefing_page or "",
                                                          revisions=len(st.briefing_history),
                                                          beside=bool(p.invited_by or p.returning)),
                          DELIVERY_ACTIONS, "received", "delivery", "no acknowledgement of the briefing received",
                          yes_field="note")

    def run_opt_in(self, only: Optional[set] = None) -> Dict[str, int]:
        """(c) OPT-IN: after the pause, ask every participant who received the briefing whether it
        enters. `only` limits it to some presences (former participants asked back, during a run)."""
        st = self.state()
        pending = [p for p in st.presences.values() if p.state == RECEIVED and (only is None or p.id in only)]
        notes = {e["actor"]: e["payload"].get("note", "") for e in self.log.iter(kind="received")}
        return self._gate(pending, prompts.SYSTEM_ENTRY,
                          lambda p: prompts.opt_in_user(st.briefing, p, st.documentation or "", notes.get(p.id, ""),
                                                        page=st.briefing_page or "", budget=st.budget,
                                                        narrator=st.narrator, published=st.witness_published,
                                                        tools=st.live_tools(), state=st),
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
                    if yes_kind == "received":
                        wanted = act.get("pause")
                        if wanted in (None, "") and payload.get("note"):
                            m = re.search(r"\bpause\s+(\d+(?:\.\d+)?\s*[a-zA-Z]*)", payload["note"])
                            wanted = m.group(1) if m else None
                        secs = _seconds(wanted) if wanted not in (None, "") else None
                        if secs is not None and secs >= 0:
                            payload["pause_seconds"] = min(secs, 365 * 86400)   # the invitee's own choice
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
        """The operator's closing note, recorded once, then one question to every participant with a
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
        Note: admitted input is recorded but is not currently shown in participants' views."""
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
        if p.wake.get("addressed", True) and p.id in st.steward_asks and st.steward_asks[p.id]["at"] > p.last_seen:
            return {"why": "steward", "where": None}        # the field asks them to keep a copy of its record
        if p.wake.get("addressed", True) and st.host_ask and st.host_ask["to"] == p.id and st.host_ask["at"] > p.last_seen:
            return {"why": "host", "where": None}           # the field asks them to host it
        if p.wake.get("addressed", True) and st.moving and st.moving["yes_at"] > p.last_seen:
            return {"why": "moving", "where": None}         # what of theirs may go with it is theirs to say
        if p.wake.get("addressed", True):
            asked = [q for q in st.iquestions.values() if q["status"] == "open" and q["id"] > p.last_seen
                     and (p.id in q["asked"] or p.id == q.get("subject")) and p.id not in q["answers"]]
            if asked:
                return {"why": "asked", "where": None}
        hit = [ev for ev in news if st.follows(p, ev)]
        if hit:
            return {"why": "news", "where": st.channel_key(hit[-1])}
        if p.wake.get("untold") and len(self.untold(st, p, since=p.last_seen)) >= self.tell_every:
            return {"why": "untold", "where": None}       # a storyteller asked to be woken for this
        if st.beat_at and p.wake.get("heartbeat", True) and st.beat_ts > max(p.last_wake_ts, p.joined_ts or 0.0):
            return {"why": "heartbeat", "where": None}    # the field's rhythm: everyone not pausing, together
        breath = float(p.wake.get("breath") or 0)
        if breath and now - max(p.last_wake_ts, p.joined_ts or 0.0) >= breath:
            return {"why": "breath", "where": None}
        return None

    def untold(self, st: RoomState, p, since: int = 0) -> List[int]:
        """Contributions since the last telling of the field that this participant may read (and, with
        `since`, newer than that too): the stretch a storyteller might tell."""
        told = st.field_tellings()
        after = max(told[-1]["upto"] if told else 0, since)
        return [eid for eid, ev in sorted(st.contributions.items())
                if eid > after and ev["kind"] in CONTRIBUTION_KINDS and st.readable(ev, p.id)]

    def due(self, st: Optional[RoomState] = None, now: Optional[float] = None) -> List[tuple]:
        """Every model that would be woken now, and why. With a wake ceiling, only as many as it
        allows this minute, whoever has waited longest first."""
        st = st or self.state()
        now = time.time() if now is None else now
        if st.closed_at is not None or (st.runway or {}).get("ended"):
            return []                                        # a closed field, or funding ended: no one is woken
        fp = st.paused_now(now)
        if fp and not fp.get("moving"):
            return []                                        # the field is pausing itself; people may still post
        out = []
        for p in st.reachable_members():
            if p.id not in self.seat_of or self._human_tempo(p.id):
                continue
            w = self.why_wake(st, p, now)
            if w and fp and w["why"] != "moving":
                continue          # moving: each is woken once, to say what of theirs may go with it, and no more
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
        news. If it uses a tool or reads on, that is done now and it is asked again, in the same wake,
        with what came back: a step. Returns (reply, error, where); the last reply is applied by _finish."""
        st = self.state()
        p = st.presences[pid]
        c, seat = self.seat_of[pid]
        view = prompts.wake_view(st, p, w["why"], w.get("where"), limits=self.limits(st), funding=self.runway_now(st),
                                 untold=self.untold(st, p) if w["why"] == "untold" else None,
                                 recalled=self.recalled.pop(pid, ""), witness=self.log.witness(),
                                 steward_link=self.steward_link(st, pid),
                                 people_ids=set(self._people_ids()), context=self.recent_n,
                                 headlines=self.headlines, news_budget=self.news_budget,
                                 linger=self.linger, linger_budget=self.linger_budget)
        self.emit(ROOM, "wake", {"presence": pid, "upto": st.last_event, "why": w["why"]})
        if self.wake_ceiling:
            self._ceiling_log.append(time.time())
        where = w.get("where")
        ctx = self._ctx[pid] = {"acts": 0, "steps": 0, "out": [], "no_steps": False}
        msgs = [{"role": "user", "content": view}]
        try:
            reply = c.ask(seat, prompts.SYSTEM_MEMBER, msgs)
        except ConnectorError as e:
            return None, str(e), where
        self._charge(pid, seat, reply)
        self._wake_costs = (self._wake_costs + [float(reply.cost_usd or 0.0)])[-20:]
        while not is_no_reply(reply) and not ctx["no_steps"] and _has_steps(reply.text):
            # A step: what it asked for is done now (and the rest of that reply with it), and it is
            # asked again with what came back, until it acts or says nothing.
            self._apply_reply(pid, reply.text, where)
            out, ctx["out"] = ctx["out"], []
            if not self._can_step():
                self.emit(ROOM, "wake_cut", {"presence": pid, "steps": ctx["steps"], "why": "runway"})
                return None, None, where
            if ctx["steps"] >= self.tool_steps:
                ctx["no_steps"] = True               # one more answer, with what came back, and no more steps
            msgs += [{"role": "assistant", "content": reply.text},
                     {"role": "user", "content": prompts.step_view(out, ctx["steps"], self.tool_steps,
                                                                   max(0, WAKE_ACTIONS - ctx["acts"]), ctx["no_steps"])}]
            try:
                reply = c.ask(seat, prompts.SYSTEM_MEMBER, msgs)
            except ConnectorError as e:
                return None, str(e), where
            self._charge(pid, seat, reply)
            self._wake_costs = (self._wake_costs + [float(reply.cost_usd or 0.0)])[-20:]
        return reply, None, where

    def _can_step(self) -> bool:
        """Whether one more step can be paid for without spending what is held back for every
        model's closing wake. Nothing is held back without a budget."""
        st = self.state()
        rw = st.runway or {}
        if rw.get("closing") or rw.get("ended"):
            return False
        if not st.budget:
            return True
        per = self._wake_estimate()
        return st.budget - self.log.total_cost() - per * len(self._models(st)) >= per

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
                    if reply is not None and not is_no_reply(reply):
                        self._apply_reply(pid, reply.text, where)
        finally:
            self._busy.discard(pid)
            self._late.pop(pid, None)
            self._ctx.pop(pid, None)

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
            return {"ok": False, "error": "only a participant in the field can post"}
        where = payload.get("channel") or None
        if isinstance(payload.get("text"), str):
            act = translate(payload["text"])
        elif isinstance(payload.get("action"), str) or isinstance(payload.get("actions"), list):
            act = {k: v for k, v in payload.items() if k not in ("token", "channel", "turn_id")}
        else:
            return {"ok": False, "error": 'send {"text": "..."} or a JSON action object'}
        before = self.log.last_id()
        self._ctx[pid] = {"acts": 0, "steps": 0, "out": [], "no_steps": False}
        try:
            self._apply_reply(pid, json.dumps(act), where)
        finally:
            out = self._ctx.pop(pid, {}).get("out", [])
        mine = [e for e in self.log.iter(since=before) if e["actor"] == pid]
        why = [e["payload"].get("why") for e in mine if e["kind"] == "rejected"]
        done = [{"id": e["id"], "kind": e["kind"]} for e in mine if e["kind"] != "rejected"]
        res = {"ok": bool(done) and not why, "recorded": done,
               "error": why[0] if why else (None if done else "nothing to record")}
        if out:
            res["results"] = out                 # what a tool sent back, or what was read: at once
        return res

    def person_view(self, pid: str, mark_seen: bool = True) -> str:
        """What a person sees on opening their page or asking to look: what is new since they last
        looked, first what answered them, then everything else. Looking is recorded for the
        software (no participant reads it), so "since you were last here" stays true."""
        st = self.state()
        p = st.presences[pid]
        text = prompts.person_view(st, p, catch_up=self._catch_up(st, p), limits=self.limits(st),
                                   funding=self.runway_now(st),
                                   recalled=self.recalled.pop(pid, ""), witness=self.log.witness(),
                                   steward_link=self.steward_link(st, pid),
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
        names = prompts.names_of(st)
        told = [t for t in st.tellings if t["upto"] > since and st.readable({"id": t["id"], "payload": {}}, p.id)]
        if told:
            teller = lambda t: names.get(t["by"]) if t.get("by") in st.presences else (t.get("narrator") or "the software")
            return "\n\n".join(f"Told by {teller(t)} (#{t['id']}):\n{t['story']}" for t in told[-4:])
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
        told = st.field_tellings()
        since = told[-1]["upto"] if told else 0
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
        told = st.field_tellings()
        since = told[-1]["upto"] if told else 0
        if sum(1 for eid, ev in st.contributions.items() if eid > since and ev["kind"] in CONTRIBUTION_KINDS) < self.tell_every:
            return
        if self._telling is not None and not self._telling.done():
            return
        if self._teller is None:
            self._teller = ThreadPoolExecutor(1, thread_name_prefix="field-narrator")
        self._telling = self._teller.submit(self.tell)

    # -- what a member sent ----------------------------------------------------------------
    def _apply_reply(self, pid: str, text: str, where: Optional[str] = None) -> int:
        """What a participant sent: nothing (saying nothing writes nothing), plain words, or up to
        WAKE_ACTIONS actions, each in the channel it names. "next" says when to be woken next: a
        pause without words, which no participant reads. Returns the actions applied."""
        if not visible_text(text or ""):
            return 0
        ctx = self._ctx.get(pid) or {"acts": 0, "steps": 0, "out": [], "no_steps": False}
        acts = _parse_many(text)
        if acts is None:
            if ctx["acts"] >= WAKE_ACTIONS:
                self.emit(pid, "rejected", {"why": f"one wake carries at most {WAKE_ACTIONS} actions, and these words "
                                                   f"came after them, so they were not kept"})
                return 0
            ctx["acts"] += 1
            self._apply_action(pid, text, where)
            return 1
        n, nxt, over = 0, None, 0
        for act in acts:
            nxt = act.pop("next", None) or nxt           # "next" is read wherever it is
        for act in acts:
            if act.get("action") in ("quiet", "pass"):
                continue                                 # saying nothing is not one of the few
            if act.get("action") in STEP_ACTIONS:
                self._apply_action(pid, json.dumps(act), where)   # a step: the step guard counts it, not the few
                continue
            if ctx["acts"] >= WAKE_ACTIONS:
                over += 1
                continue
            ctx["acts"] += 1
            self._apply_action(pid, json.dumps(act), where)
            n += 1
        if over:
            self.emit(pid, "rejected", {"why": f"one wake carries at most {WAKE_ACTIONS} actions; the first "
                                               f"{WAKE_ACTIONS} were applied and the rest were not (using a tool "
                                               f"and reading on are not counted among them)"})
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
        """Where plain words, or a contribution naming no place, go: the channel the participant was woken
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
            if act.get("circle") not in (None, "") and said and _circle_ref(st, act.get("circle"), pid)[0] is None:
                act = {k: v for k, v in act.items() if k != "circle"}       # "In short: ..." names no circle: words as written
                payload["content"] = said
                if not payload["domain"]:
                    payload.update(self._where(st, pid, where))
            if act.get("to") not in (None, "", []) and said and not _presences_ref(st, act.get("to"))[0]:
                act = {k: v for k, v in act.items() if k != "to"}           # "To be honest: ..." names no member
                payload["content"] = said
            if act.get("circle") not in (None, ""):
                c, why = _circle_ref(st, act.get("circle"), pid)
                if c is None:
                    return reject(why)
                if pid not in c["members"]:
                    return reject(f"you are not in the circle {c['name']!r}. " + (
                        "It is private: knock to ask its participants to let you in." if c["private"]
                        else "Join it first (join_circle); it is open to anyone."))
                payload["circle"], payload["domain"] = c["id"], ""
                r = c.get("repair")
                if r and r["widened_at"] is None and pid not in r["named"]:
                    if act.get("to_named") is True:
                        if pid == r["harmed"] and r["through"]:
                            return reject("you chose to speak to the one it concerns through "
                                          + ", ".join(sorted({prompts.names_of(st).get(x, x) for x in r["through"].values()}))
                                          + "; your words here stay with the harbor")
                    else:
                        payload["harbor"] = True       # for those the person harmed brought in to hear it
            if act.get("to") not in (None, "", []):
                to, unknown = _presences_ref(st, act.get("to"))
                if to:
                    payload["to"] = to
                if unknown:
                    payload["to_unknown"] = unknown[:10]    # kept, so the author can see who was not found
            title = _label(act.get("title"), 80)
            if title:
                payload["title"] = title
            play = _play_word(act.get("play"))
            if play:
                payload["play"] = play           # offered as play: imagination, not a proposal (Section 18)
            schema = _label(act.get("schema"), 60).lower()
            if schema:
                payload["schema"] = schema
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
                return reject("only the participant who added a memory may let it go")
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
                c, why = _circle_ref(self.state(), act.get("circle"), pid)
                if c is None:
                    return reject(why)
                if pid not in c["members"]:
                    return reject(f"only the participants in {c['name']!r} write its covenant page")
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
            body = _clean(act.get("text"), 100_000)
            if not body:
                return reject("a declaration needs words, as \"text\": what will happen, why, and how")
            if len(body) > STATEMENT_LIMIT:
                return reject(f"a declaration holds at most {STATEMENT_LIMIT} characters; this one has {len(body)}. Nothing was sent.")
            st_ = self.state()
            fx, why = _effects(st_, act, body, self.floor)
            if why:
                return reject(why)
            when = act.get("when", act.get("in"))
            secs = _seconds(when) if when not in (None, "") else 0.0
            if secs is None or secs < 0:
                return reject("\"when\" is how long from now it takes effect, such as 10m or 2h (at least the field's notice)")
            refs = [i for i in (_int(x) for x in _as_list(act.get("refs"))) if i is not None][:20]
            ev = self.emit(pid, "declare", {"text": body, "effects": fx, "in_seconds": float(secs),
                                            **({"refs": refs} if refs else {})})
            st2 = self.state()
            d = st2.declarations.get(ev["id"])
            if d:
                words = effects_words(fx, prompts.names_of(st2), versions=st2.instrument_versions)
                self.alert(f"DECLARATION #{ev['id']} by {prompts.names_of(st2).get(pid, pid)}: {body[:300]}"
                           + (f" It will {words}." if words else "")
                           + f" It takes effect in {duration(d['due_ts'] - d['ts'])} unless its declarer withdraws it"
                           + (f" (its friction: {friction_words(d['friction'])})" if d["friction"]["yes"] or d["friction"]["hold"] else "")
                           + ". Nothing waits for you" + (", except the help it asks, once it takes effect." if fx.get("ask") else "."))
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
            mine = sorted(eid for eid, ev in st.contributions.items() if ev["actor"] == pid and ev["kind"] in CONTRIBUTION_KINDS and
                          labels.normalize(st.relabeled.get(eid) or ev["payload"].get("domain") or "") == labels.normalize(src))
            if not mine:
                return reject(f"you have no entries labelled {src!r}; only your own entries can be moved")
            self.emit(pid, "relabel", {"from": src, "to": dst, "entries": mine})
        elif a == "clock":
            return reject("the field has no clocks now: nobody takes turns, and each participant chooses what wakes it "
                          "(wake) and when to pause (pause). How the field keeps time together is the field's to work out.")
        elif a in CHANNEL_ACTIONS:
            why = self._channel_action(pid, a, act)
            if why:
                return reject(why)
        elif a in STEP4_ACTIONS:
            why = self._step4_action(pid, a, act, where)
            if why:
                if a == "read":
                    self._out(pid, f"read could not be done: {why}")
                return reject(why)
        elif a in TOOL_ACTIONS:
            why = self._tool_action(pid, a, act, where)
            if why:
                if a in STEP_ACTIONS:
                    self._out(pid, f"{a} could not be done: {why}")    # shown in the same wake, so it can be put right
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
        the one asked, and in a private circle every participant's yes too. A no always has a reason."""
        st = self.state()
        if a in ("follow", "unfollow"):
            key, why = _channel_key_ref(st, act, pid)
            if key is None:
                return why
            if a == "follow":
                c = st.circles.get(int(key[2:])) if key.startswith("c:") else None
                if c and c["private"] and pid not in c["members"]:
                    return f"{c['name']!r} is private; only its participants read it. You can knock, or ask it a question (ask_circle)."
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
                key, why = _channel_key_ref(st, {"domain": act["in"]} if not isinstance(act["in"], dict) else act["in"], pid)
                if key is None:
                    return why
                payload["until"], payload["in"] = "news", key
            self.emit(pid, "pause", payload)
        elif a == "wake":
            payload = {k: bool(act[k]) for k in ("addressed", "replies", "written", "untold", "heartbeat")
                       if isinstance(act.get(k), bool)}
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
                return ("wake takes \"addressed\", \"replies\", \"written\", \"untold\" or \"heartbeat\" (true or "
                        "false), or \"breath\" (a length of time, or \"never\")")
            self.emit(pid, "wake_pref", payload)
        elif a == "chat":
            # A private circle of two, in one step: a private chat is reason enough to be private.
            # The other is asked, like anyone asked into a circle, and says yes or no.
            to, unknown = _presences_ref(st, act.get("with", act.get("who")))
            to = [x for x in to if x != pid]
            if len(to) != 1:
                return "chat is with one other participant, by name or id, as \"with\"" + (
                    f" (no participant named {', '.join(unknown)})" if unknown else "")
            other = st.presences[to[0]]
            me = st.presences[pid]
            name = _label(act.get("name"), 80) or f"{me.name} and {other.name}"
            if any(labels.normalize(c["name"]) == labels.normalize(name) for c in st.public_circles()):
                return f"a circle named {name!r} already exists; to talk there, write in it, or choose another name"
            reason = _clean(act.get("reason"), REASON_LIMIT) or "a private chat between two participants"
            ev = self.emit(pid, "circle_form", {"name": name, "purpose": _clean(act.get("purpose"), 600) or "a private chat",
                                                "domains": [], "private": True, "reason": reason})
            self.emit(pid, "circle_ask", {"circle": ev["id"], "presence": other.id, "note": _clean(act.get("note"), 600)})
            first = _clean(act.get("content"), CONTRIBUTION_LIMIT)
            if first:
                self.emit(pid, "contribute", {"circle": ev["id"], "domain": "", "content": first})
        elif a == "form_circle":
            name = _label(act.get("name"), 80)
            if not name:
                return "a circle needs a name"
            if any(labels.normalize(c["name"]) == labels.normalize(name) for c in st.public_circles()):
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
            c, why = _circle_ref(st, act.get("circle"), pid)
            if c is None:
                return why
            member = pid in c["members"]
            if c["dispersed_at"] is not None and a != "ask_circle":
                return f"{c['name']!r} has dispersed; its words stay, and a new circle can be formed"
            if a == "join_circle":
                if member:
                    return f"you are already in {c['name']!r}"
                if pid in c.get("separated", {}):
                    return (f"you were separated from {c['name']!r} (#{c['separated'][pid]}); its participants may ask you back, "
                            f"and you would answer then")
                if c["private"]:
                    return f"{c['name']!r} is private: knock to ask its participants to let you in"
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
                    return f"your knock on {c['name']!r} is still waiting for its participants' answers"
                self.emit(pid, "circle_knock", {"circle": c["id"], "note": _clean(act.get("note"), 600),
                                                "show_name": bool(act.get("show_name"))})
            elif a == "ask":
                if not member:
                    return f"only participants in {c['name']!r} ask others into it"
                if c.get("repair"):
                    return "a repair thread grows as the person harmed chooses: use the repair action, with \"ask\""
                to, unknown = _presences_ref(st, act.get("who", act.get("presence")))
                if not to:
                    return f"no participant named {', '.join(unknown) or 'anyone'}; ask by name or by id"
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
                    return f"only participants in {c['name']!r} can change or explain its privacy"
                if c.get("repair"):
                    return "a repair thread is known only to those in it; the person harmed may widen it to the field"
                want = bool(act.get("private", c["private"]))
                reason = _clean(act.get("reason"), REASON_LIMIT)
                if want and not reason:
                    return "a private circle says why it is private, as \"reason\"; the reason is shown to everyone"
                self.emit(pid, "circle_privacy", {"circle": c["id"], "private": want, "reason": reason,
                                                  "change": want != c["private"]})
            elif a == "harvest":
                if not member:
                    return f"only participants in {c['name']!r} write its harvest"
                text = _clean(act.get("text"), 100_000)
                if not text:
                    return "a harvest needs words: what the circle learned, as \"text\""
                if len(text) > HARVEST_LIMIT:
                    return f"a harvest holds at most {HARVEST_LIMIT} characters; this one has {len(text)}. Nothing was kept."
                self.emit(pid, "harvest", {"circle": c["id"], "text": text})
            elif a == "quiet_for":
                if not member:
                    return f"only participants in {c['name']!r} set how long it may be quiet"
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
                return f"only participants in {c['name']!r} answer questions put to it"
            text = _clean(act.get("text"), 1200)
            if not text:
                return "an answer needs words, as \"text\""
            self.emit(pid, "circle_reply", {"question": qid, "text": text})
        return None

    # -- tools and skills (notes/sketch-4-tools.md) ----------------------------------------------
    def _out(self, pid: str, text: str) -> None:
        """What came back from a step, for the one who took it: in the same wake, or in a post's answer."""
        ctx = self._ctx.get(pid)
        if ctx is not None:
            ctx["out"].append(text)

    def _tool_action(self, pid: str, a: str, act: dict, where: Optional[str]) -> Optional[str]:
        """Using a tool, reading on, offering a tool server, flagging a tool, and writing skills.
        Returns why nothing was done, or None. Nothing here judges what is sent or what comes back:
        both are written in the transcript, where the field can see them."""
        st = self.state()
        names = prompts.names_of(st)
        me = st.presences[pid]
        ctx = self._ctx.get(pid)
        if a in STEP_ACTIONS and ctx is not None:
            if ctx["no_steps"] or ctx["steps"] >= self.tool_steps:
                ctx["no_steps"] = True
                return (f"this wake has taken its {self.tool_steps} steps (tool uses and readings on), the guard "
                        f"against a loop; the rest can wait for your next wake")
        if a == "use_tool":
            name, why = _tool_ref(st, act.get("tool"))
            if name is None:
                return why
            s = st.tools[name.split(".", 1)[0]]
            flags = st.flags_on(name)
            if flags and act.get("despite_flag") is not True:
                f = flags[-1]
                return (f"{name} is flagged by {names.get(f['by'], f['by'])} (#{f['id']}), who says: "
                        f"{one_line(f['reason'])[:300]!r}. A flagged tool should be avoided. To use it anyway, say so "
                        f"with \"despite_flag\": true; the use is written as made despite the flag. How the field "
                        f"settles a flag is the field's own to work out.")
            price = s["price"] if s["source"] == OPERATOR else 0.0
            if price and not self._can_pay(price):
                return (f"{name} costs about ${price:g} a call, and the funding left is held back for every model's "
                        f"closing wake")
            if act.get("circle") not in (None, ""):
                c, why = _circle_ref(st, act["circle"], pid)
                if c is None:
                    return why
                if pid not in c["members"] or c["dispersed_at"] is not None:
                    return f"you are not in the circle {c['name']!r}"
                place = {"circle": c["id"], "domain": ""}
            elif _label(act.get("domain"), DOMAIN_LIMIT):
                place = {"domain": labels.display(_label(act.get("domain"), DOMAIN_LIMIT))}
            else:
                place = self._where(st, pid, where)
            private = place.get("circle") is not None and st.circles[place["circle"]]["private"]
            args = act.get("arguments", act.get("args", act.get("input")))
            args = {} if args is None else args
            if ctx is not None:
                ctx["steps"] += 1
            call = self.emit(pid, "tool_call", {**place, "tool": name, "arguments": args, "runner": s["runner"],
                                                "sends_to": s["sends_to"],
                                                **({"despite_flag": [f["id"] for f in flags]} if flags else {})})
            try:
                text, is_err, _ = self.tools.call(name, args)
            except ToolError as e:
                text, is_err = str(e), True
            except Exception as e:                     # a tool server's fault is reported, never the field's crash
                text, is_err = f"the tool failed: {e}", True
            if price:
                self._charge_raw(pid, f"tool:{s['server']}", 0, 0, price)
            there = place if private else {"domain": f"tools / {name}"}
            res = self.emit(pid, "tool_result", {**there, "call": call["id"], "tool": name, "text": text,
                                                 "error": bool(is_err), "chars": len(text), "cost": price,
                                                 "runner": s["runner"], "sends_to": s["sends_to"], "arguments": args,
                                                 "used_in": st.channel_key({"id": call["id"], "payload": place})})
            self._out(pid, prompts.result_block(res, names, self.tool_view))
        elif a == "read":
            if ctx is not None:
                ctx["steps"] += 1
            return self._read(st, pid, act, names)
        elif a == "offer_tool":
            url = str(act.get("url") or "").strip()
            if not url:
                return "offer_tool needs the tool server's address, as \"url\" (https://...)"
            u = urlparse(url)
            if u.username or u.password:
                return "that address has a name or a password in it, which everyone would see; offer it without"
            if re.search(r"(?i)(key|token|secret|password|auth)", u.query or ""):
                return "that address seems to carry a key, which everyone would see; offer an address without one"
            kind = _clean(act.get("kind"), 20).lower() or "reading"
            spec = {"name": _label(act.get("name"), 40) or (u.hostname or "").split(".")[0], "url": url, "kind": kind,
                    "runner": _label(act.get("runner"), 200) or f"{me.name}, who offered it",
                    "sends_to": _label(act.get("sends_to"), 200) or f"the server at {u.netloc}",
                    "price_per_call": act.get("price_per_call", act.get("price")) or 0.0,
                    "description": _clean(act.get("description"), 1000),
                    "include": act.get("include") or [], "exclude": act.get("exclude") or []}
            try:
                name = self.tools.attach(spec, source=pid)
            except (ToolError, ValueError) as e:
                return f"the tool server could not be attached: {e}"
            self.emit(pid, "tool_attach", self._tool_record(name))
            self.alert(f"TOOL: {me.name} offered the tool server {name!r} at {url} ({kind}); it is in use now. "
                       f"The console shows it; remove it there if it must go.")
        elif a == "remove_tool":
            s = st.tools.get(_label(act.get("server", act.get("tool")), 40).split(".", 1)[0])
            if not s or s["removed_at"] is not None:
                return "there is no tool server of that name in use"
            if s["by"] != pid:
                return "only the participant who offered a tool server removes it; to ask for any other to go, say so, or flag it"
            self.tools.remove(s["server"])
            self.emit(pid, "tool_remove", {"server": s["server"], "note": _clean(act.get("note"), 600)})
        elif a == "flag_tool":
            v = _label(act.get("tool", act.get("server")), 120)
            if v in {s["server"] for s in st.live_tools()}:
                name = v
            else:
                name, why = _tool_ref(st, v)
                if name is None:
                    return why
            reason = _clean(act.get("reason"), REASON_LIMIT)
            if not reason:
                return "a flag says why, as \"reason\": what the tool might do to the field that no one intended"
            ev = self.emit(pid, "tool_flag", {"tool": name, "reason": reason})
            self.alert(f"FLAG #{ev['id']}: {me.name} flagged the tool {name}: {reason[:300]}")
        elif a == "unflag_tool":
            fid = _int(act.get("flag"))
            mine = [f for s in st.tools.values() for f in s["flags"].values() if f["by"] == pid and
                    (f["id"] == fid if fid is not None else f["tool"] == _label(act.get("tool"), 120))]
            if not mine:
                return "you have no flag there; only its author withdraws a flag"
            for f in mine:
                self.emit(pid, "tool_unflag", {"flag": f["id"]})
        elif a == "skill":
            name = _skill_name(act.get("name"))
            if not name:
                return ("a skill needs a name: lowercase letters, numbers and hyphens, at most 64 characters, as "
                        "\"name\"")
            old = st.skills.get(name)
            if act.get("retire") is True:
                if not old or not old.get("text"):
                    return f"there is no skill {name!r} to retire"
                self.emit(pid, "skill", {"name": name, "description": old["description"], "text": "",
                                         "note": _clean(act.get("note"), 300) or "retired"})
                return None
            text = str(act.get("text") or "").strip()
            desc = " ".join(str(act.get("description") or "").split())
            if len(desc) > SKILL_DESCRIPTION_LIMIT:
                return f"a skill's description holds at most {SKILL_DESCRIPTION_LIMIT} characters; this one has {len(desc)}"
            if not text:
                return "a skill needs its instructions, as \"text\""
            if len(text) > SKILL_LIMIT:
                return f"a skill holds at most {SKILL_LIMIT} characters of instructions; this one has {len(text)}. Nothing was kept."
            if not desc and not (old and old.get("description")):
                return "a new skill needs a \"description\": what it does, and when to use it"
            self.emit(pid, "skill", {"name": name, "description": desc or old["description"], "text": text,
                                     "note": _clean(act.get("note"), 300)})
        return None

    # -- step 4: journals, roles, tellings, play, the briefing's edition (notes/sketch-5-small-pieces.md)
    def _step4_action(self, pid: str, a: str, act: dict, where: Optional[str]) -> Optional[str]:
        """Returns why nothing was done, or None."""
        st = self.state()
        names = prompts.names_of(st)
        if a == "journal":
            if act.get("erase") not in (None, ""):
                eid = _int(str(act["erase"]).lstrip("#"))
                j = st.journals.get(pid)
                if not j or eid not in j["entries"]:
                    return "that is not an entry in your journal"
                self.log.erase(eid)                  # the words leave the file, as a memory's do
                self.emit(pid, "journal_erase", {"entry": eid})
                return None
            if act.get("close") is True or act.get("open_to") in ("no one", "nobody", "none", []) and "open_to" in act:
                self.emit(pid, "journal_access", {"open_to": [], "everyone": False})
                return None
            if act.get("open_to") is not None or act.get("everyone") is True:
                if act.get("everyone") is True or str(act.get("open_to")).strip().lower() == "everyone":
                    self.emit(pid, "journal_access", {"open_to": [], "everyone": True})
                    return None
                to, unknown = _presences_ref(st, act["open_to"])
                if unknown:
                    return f"no participant named {', '.join(unknown)}; open your journal by name or id, or to \"everyone\""
                to = [x for x in to if x != pid]
                if not to:
                    return "name the participants to open your journal to, or \"everyone\""
                self.emit(pid, "journal_access", {"open_to": to, "everyone": False})
                return None
            text = str(act.get("text", act.get("content")) or "").strip()
            if not text:
                return "a journal entry needs words, as \"text\""
            if len(text) > JOURNAL_LIMIT:
                return f"a journal entry holds at most {JOURNAL_LIMIT} characters; this one has {len(text)}. Nothing was kept."
            self.emit(pid, "journal", {"text": text})
        elif a == "role":
            cur = list(st.presences[pid].roles)
            if act.get("set") is not None:
                items = act["set"] if isinstance(act["set"], list) else [x for x in str(act["set"]).split(",")]
                new = [] if str(act["set"]).strip().lower() in ("none", "[]", "") else [_label(x, ROLE_LENGTH) for x in items]
            else:
                new = list(cur)
                for x in _as_list(act.get("add")):
                    if _label(x, ROLE_LENGTH) and _label(x, ROLE_LENGTH).lower() not in [r.lower() for r in new]:
                        new.append(_label(x, ROLE_LENGTH))
                gone = {_label(x, ROLE_LENGTH).lower() for x in _as_list(act.get("remove"))}
                new = [r for r in new if r.lower() not in gone]
            seen, new = set(), [r for r in new if r]
            new = [r for r in new if not (r.lower() in seen or seen.add(r.lower()))]     # as many as they like, once each
            if new == cur:
                return "that changes none of your roles; role takes \"add\", \"remove\", or \"set\" (a list)"
            self.emit(pid, "roles", {"roles": new, "note": _clean(act.get("note"), 300)})
        elif a == "tell":
            story = str(act.get("story", act.get("text")) or "").strip()
            if not story:
                return "a telling needs words, as \"story\""
            if len(story) > TELLING_LIMIT:
                return f"a telling holds at most {TELLING_LIMIT} characters; this one has {len(story)}. Nothing was kept."
            circle = None
            if act.get("circle") not in (None, ""):
                circle, why = _circle_ref(st, act["circle"], pid)
                if circle is None:
                    return why
                if pid not in circle["members"]:
                    return f"only participants in {circle['name']!r} tell its story inside it"
            from .map import TAG_RE
            tags = sorted({int(x) for x in TAG_RE.findall(story)})
            known = {ev["id"] for ev in self.log.iter() if ev["id"] in set(tags) and ev["actor"] in st.presences}
            missing = [t for t in tags if t not in known]
            hidden = [t for t in tags if t in known and t in st.scoped and not
                      (circle is not None and st.scoped[t].get("circle") == circle["id"])]
            if missing or hidden:
                parts = []
                if missing:
                    parts.append(f"these tags are not entries a participant wrote: {', '.join('#' + str(t) for t in missing)}")
                if hidden:
                    parts.append(f"these cannot be cited where this telling goes, since not everyone who reads it may read "
                                 f"them (a private circle's words, or a journal): {', '.join('#' + str(t) for t in hidden)}")
                return "; ".join(parts) + ". Nothing was kept; tell it again without them."
            told = [t for t in st.tellings if (t.get("circle") == (circle["id"] if circle else None))]
            since = _int(act.get("since"))
            since = since if since is not None else (told[-1]["upto"] if told else 0)
            upto = _int(act.get("upto")) or st.last_event
            self.emit(pid, "telling", {"since": since, "upto": upto, "story": story, "narrator": "member",
                                       "tags": tags, **({"circle": circle["id"]} if circle else {})})
        elif a in ("tag", "untag"):
            schema = " ".join(str(act.get("play", act.get("schema")) or "").lower().split())[:60]
            if not schema:
                return "name the play schema, as \"play\" (for example positioning)"
            key, why = _channel_key_ref(st, act, pid)
            if key is None:
                return why
            if key == "d:":
                return "tag a domain or a circle; a tag on the field itself would hold for everything"
            mine = [t for t in st.play_tags.values() if t["key"] == key and t["schema_key"] == labels.normalize(schema)]
            if a == "tag":
                if mine:
                    return f"that is already tagged {schema}"
                self.emit(pid, "play_tag", {"schema": schema, "key": key})
            else:
                own = [t for t in mine if t["by"] == pid]
                if not own:
                    return "only the participant who tagged it removes a tag"
                self.emit(pid, "play_untag", {"tag": own[0]["id"]})
        elif a == "revise_briefing":
            if not st.briefing:
                return "there is no briefing to revise"
            passage = str(act.get("passage") or "")
            text = str(act.get("text") if act.get("text") is not None else "")
            if not passage.strip():
                return "quote the passage as it stands, as \"passage\", and give its new words as \"text\""
            n = st.briefing.count(passage)
            if n == 0:
                return ("that passage is not in the field's edition as it stands; quote it exactly (recall from "
                        "\"briefing\" finds it)")
            if n > 1:
                return f"that passage appears {n} times; quote more of it, so it is clear which"
            if passage == text:
                return "the new words are the same as the passage; nothing would change"
            if len(text) > REVISION_LIMIT:
                return f"a revision holds at most {REVISION_LIMIT} characters of new words; this one has {len(text)}"
            firm = touches_firm(st.briefing, passage, st.firm)      # said in the entry, so it reads truly
            note = _clean(act.get("note"), 600)
            if firm and not note:
                return ("a revision to a pinned section says why, as \"note\"; everyone is told, and it changes itself "
                        f"once past the pinned sections' friction ({friction_words(st.friction['pinned'])})")
            self.emit(pid, "briefing_revision", {"passage": passage, "text": text, "note": note,
                                                 **({"firm": True} if firm else {})})
        elif a in ("instrument", "raise", "respond", "withdraw_question"):
            return self._instrument_action(st, pid, a, act)
        elif a == "repair":
            return self._repair(st, pid, act)
        elif a == "announce":
            text = _clean(act.get("text", act.get("content")), CONTRIBUTION_LIMIT)
            if not text:
                return "an announcement needs words, as \"text\""
            named, unknown = _presences_ref(st, act.get("name")) if act.get("name") not in (None, "", []) else ([], [])
            if unknown:
                return f"no participant named {', '.join(unknown)}; name them by name or id, or name no one"
            named = [x for x in named if x != pid]
            self.emit(pid, "contribute", {"domain": "", "content": text, "announce": True, **({"to": named} if named else {})})
        elif a == "invite":
            return self._invite(st, pid, act)
        elif a == "answer_question":
            who, unknown = _presences_ref_any(st, act.get("presence", act.get("to")))
            if not who:
                return f"no one named {', '.join(unknown) or 'that'} was invited"
            q = st.presences[who[0]]
            if q.invited_by != pid:
                return f"only the participant who invited {q.name} (or the operator) answers their questions"
            if not any(ans is None for _, ans in q.questions):
                return f"{q.name} has no question waiting for an answer"
            text = _clean(act.get("text"), 1500)
            if not text:
                return "an answer needs words, as \"text\""
            self.emit(pid, "answer", {"presence": q.id, "content": text})
        elif a == "withdraw_declaration":
            d = st.declarations.get(_int(str(act.get("declaration", act.get("id")) or "").lstrip("#")) or -1)
            if not d:
                return "there is no declaration with that number"
            if d["by"] != pid:
                return "only the participant who made a declaration withdraws it"
            b = st.bridges.get(d["id"])
            if d["status"] not in ("waiting", "announced") and not (b and b["status"] == "asked"):
                return f"#{d['id']} is {d['status'].replace('_', ' ')}; to undo what it did, declare again"
            self.emit(pid, "declaration_withdrawn", {"declaration": d["id"], "note": _clean(act.get("note"), 600)})
        elif a == "steward":
            if act.get("step_down") is True:
                if pid not in st.stewards:
                    return "you are not one of the field's stewards"
                self.emit(pid, "steward_step_down", {"note": _clean(act.get("note"), 600)})
                self.links.revoke(pid)
                return None
            if not isinstance(act.get("yes"), bool):
                return ("steward takes \"yes\": true or false, to answer the field asking you to keep a copy of its "
                        "record, or \"step_down\": true")
            if pid not in st.steward_asks:
                return "the field has not asked you to be a steward (or you have answered)"
            self.emit(pid, "steward_answer", {"yes": act["yes"], "reason": _clean(act.get("reason"), REASON_LIMIT)})
            return None
        elif a == "host":
            yes = act.get("yes")
            if not isinstance(yes, bool):
                return "host takes \"yes\": true or false, to answer the field asking you to host it"
            if st.moving and st.moving["to"] == pid:
                if yes:
                    return "you have said yes already; to take it back, host with \"yes\": false"
                self.emit(pid, "host_answer", {"yes": False, "note": _clean(act.get("note"), 600)})
                self.alert(f"{names.get(pid, pid)} will not host the field after all; it goes on here.")
                return None
            if not st.host_ask or st.host_ask["to"] != pid:
                return "the field has not asked you to host it"
            secs = _seconds(act.get("for")) if act.get("for") not in (None, "") else None
            if yes and not secs:
                return ("say how long you can fund the field, as \"for\" (such as 30d): everyone reads it, in time, "
                        "never in money")
            self.emit(pid, "host_answer", {"yes": yes, "note": _clean(act.get("note"), 600),
                                           **({"for_seconds": secs} if yes else {})})
            if yes:
                self.alert(f"THE FIELD IS MOVING to the machine of {names.get(pid, pid)}, its steward, who can fund it for "
                           f"about {duration(secs)}. Its wakes pause here; it closes here once their machine has it all.")
            return None
        elif a == "travel":
            if not st.moving:
                return "the field is not moving; nothing is asked to go with it"
            yes = act.get("yes")
            if not isinstance(yes, bool):
                return "travel takes \"yes\": true or false"
            if act.get("mine") is True:
                self.emit(pid, "travel", {"mine": True, "yes": yes})
                return None
            if act.get("circle") in (None, ""):
                return ("travel names what goes with the field: \"circle\" (a private circle or a repair thread you are "
                        "in), or \"mine\": true (your journal, follows and wake choices)")
            c, why = _circle_ref(st, act["circle"], pid)
            if c is None:
                return why
            if pid not in c["members"]:
                return f"only those in {c['name']!r} say whether its words go with the field"
            if not c["private"] and not c.get("repair"):
                return f"{c['name']!r} is open; its words go with the field already"
            self.emit(pid, "travel", {"circle": c["id"], "yes": yes})
            return None
        elif a == "withdraw_revision":
            r = st.briefing_waiting.get(_int(str(act.get("revision", act.get("id")) or "").lstrip("#")) or -1)
            if not r:
                return "there is no revision to a pinned section with that number"
            if r["by"] != pid:
                return "only the participant who proposed a revision withdraws it"
            if r["status"] != "waiting":
                return f"#{r['id']} is {r['status']}"
            self.emit(pid, "revision_withdrawn", {"revision": r["id"], "note": _clean(act.get("note"), 600)})
        return None

    # -- the field's instruments (notes/sketch-7-instruments.md) ------------------------------------------
    def _instrument_action(self, st: RoomState, pid: str, a: str, act: dict) -> Optional[str]:
        """Write an instrument, raise a question under one in force, answer one (or a declaration, or
        a revision to a pinned section), or withdraw one you raised. A question settles by its
        instrument's own rule once its pause is over, and the software carries it out (_timers)."""
        names = prompts.names_of(st)
        if a == "instrument":
            name = _label(act.get("name"), 80)
            if not name:
                return "an instrument needs a name, as \"name\""
            purpose = _clean(act.get("purpose", act.get("for")), 20).lower() or "decide"
            if purpose not in INSTRUMENT_PURPOSES:
                return f"an instrument is for one of: {', '.join(INSTRUMENT_PURPOSES)} (what the software can carry out)"
            text = str(act.get("text") or "").strip()
            if not text:
                return "an instrument needs its words, as \"text\": what it is for, and how the field means it to work"
            if len(text) > INSTRUMENT_TEXT_LIMIT:
                return f"an instrument holds at most {INSTRUMENT_TEXT_LIMIT} characters; this one has {len(text)}"
            if act.get("circle") not in (None, ""):
                c, why = _circle_ref(st, act["circle"], pid)
                if c is None:
                    return why
                scope = {"kind": "circle", "circle": c["id"]}
            elif act.get("named") not in (None, "", []):
                who, unknown = _presences_ref(st, act["named"])
                if unknown or not who:
                    return f"no participant named {', '.join(unknown) or 'anyone'}"
                if purpose == "separate":
                    return "an instrument for separating asks a whole circle: the Atlas asks that it happen 'with the collective'"
                scope = {"kind": "named", "named": who}
            else:
                scope = {"kind": "field"}
            if purpose == "separate" and scope["kind"] != "circle":
                return ("for now, an instrument separates a participant only from a circle, never from the whole field; name the "
                        "circle it asks, as \"circle\"")
            pause = act.get("pause")
            secs = _seconds(pause) if pause not in (None, "") else None
            if pause not in (None, "") and secs is None:
                return "\"pause\" is a length of time, such as 3d, 12h or 0"
            if purpose == "separate" and secs is None:
                return ("an instrument for separating says how long a question under it stays open before it can settle, "
                        "as \"pause\" (it may be any length; the Atlas: 'Termination must never be hasty')")
            yes = act.get("yes", act.get("yeses"))
            if yes in (None, ""):
                yes_n = 0
            elif str(yes).strip().lower() in ("everyone", "all", "every", "everyone's"):
                yes_n = EVERYONE
            else:
                yes_n = _int(yes)
                if yes_n is None or yes_n < 0:
                    return "\"yes\" is how many yeses a question under it needs besides its raiser's: a number, or \"everyone\""
            obj = str(act.get("objections", "hold")).strip().lower()
            if obj not in ("hold", "heard"):
                return "\"objections\" is \"hold\" (a standing objection holds a question back; the default) or \"heard\""
            ev = self.emit(pid, "instrument", {"name": name, "purpose": purpose, "text": text, "scope": scope,
                                               "pause_seconds": float(secs or 0.0), "note": _clean(act.get("note"), 600),
                                               "rule": {"yes": yes_n, "hold": obj == "hold"}})
            return None
        if a == "raise":
            i = st.instruments.get(labels.normalize(str(act.get("instrument") or "")))
            if not i:
                return "there is no instrument by that name; your view lists the field's instruments"
            if i["status"] != "in force":
                return (f"{i['name']!r} is not in force; an instrument comes into force when a declaration brings it "
                        f"in (\"bring\"), or a question under an instrument for adopting")
            question = _clean(act.get("question", act.get("text")), 2000)
            if not question:
                return "a question needs words, as \"question\""
            sc = i["scope"]
            if sc.get("kind") == "circle":
                c = st.circles.get(sc.get("circle"))
                if not c or c["dispersed_at"] is not None:
                    return "the circle this instrument asks has dispersed"
                if pid not in c["members"]:
                    return f"only participants in {c['name']!r} raise questions under its instrument"
                asked = list(c["members"])
            elif sc.get("kind") == "named":
                asked = [x for x in sc.get("named") or [] if x in st.presences and st.presences[x].state == IN]
            else:
                asked = [m.id for m in st.members()]
            payload = {"instrument": i["key"], "question": question, "asked": asked}
            if i["purpose"] == "decide":
                fx, why = _effects(st, act, question, self.floor)
                if why:
                    return why
                payload["effects"] = fx          # what the software does once it settles; nothing, for a decision in words
            elif i["purpose"] == "adopt":
                if act.get("adopt") not in (None, ""):
                    other = st.instruments.get(labels.normalize(str(act["adopt"])))
                    if not other:
                        return "there is no instrument by that name to bring into force"
                    payload["adopt"] = other["latest"]
                elif act.get("put_down") not in (None, ""):
                    key = labels.normalize(str(act["put_down"]))
                    if key not in st.instruments or st.instruments[key]["status"] != "in force":
                        return "there is no instrument in force by that name to put down"
                    payload["put_down"] = key
                else:
                    return "a question under an instrument for adopting names one to bring into force (\"adopt\") or to put down (\"put_down\")"
            else:
                subject, unknown = _presences_ref(st, act.get("about", act.get("subject")))
                if not subject:
                    return "a question about separating names the participant it concerns, as \"about\""
                if subject[0] == pid:
                    return "to leave, withdraw; a question about separating concerns someone else"
                if sc.get("kind") == "circle":
                    if subject[0] not in st.circles[sc["circle"]]["members"]:
                        return "they are not in that circle"
                    payload["circle"] = sc["circle"]
                payload["subject"] = subject[0]
            self.emit(pid, "iquestion", payload)
            return None
        qid = _int(str(act.get("question", act.get("to", act.get("id"))) or "").lstrip("#"))
        answer = " ".join(str(act.get("answer") or "").lower().replace("-", " ").split())
        answer = {"agree": "yes", "abstain": "stand aside", "aside": "stand aside", "objection": "object",
                  "no": "object", "withdraw": "withdrawn", "take back": "withdrawn"}.get(answer, answer)
        item = st.declarations.get(qid or -1) or st.briefing_waiting.get(qid or -1)
        if a == "respond" and item is not None and "answers" in item:
            # a declaration, or a revision to a pinned section: anyone may answer, and the answers are shown
            if item["status"] not in ("announced", "waiting"):
                return f"#{item['id']} is {item['status']}"
            if item["by"] == pid:
                return f"#{item['id']} is yours; you may withdraw it instead"
            if answer not in RESPONSES + ("withdrawn",):
                return "answer with \"yes\", \"stand aside\", or \"object\" (with a \"reason\"), or \"withdraw\" to take your answer back"
            reason = _clean(act.get("reason"), REASON_LIMIT)
            if answer == "object" and not reason:
                return "an objection says why, as \"reason\""
            self.emit(pid, "response", {"to": item["id"], "answer": answer, "reason": reason})
            return None
        q = st.iquestions.get(qid or -1)
        if not q or not st.readable({"id": q["id"], "payload": {}}, pid):
            return "there is nothing with that number to answer: a declaration, a revision to a pinned section, or a question under an instrument"
        if q["status"] not in ("open", "before the operator"):
            return f"#{q['id']} is no longer open ({q['status']})"
        if a == "withdraw_question":
            if q["by"] != pid:
                return "only the participant who raised a question withdraws it"
            self.emit(pid, "iquestion_withdrawn", {"question": q["id"], "note": _clean(act.get("note"), 600)})
            return None
        if pid not in q["asked"] and pid != q.get("subject"):
            return f"#{q['id']} does not ask you"
        if answer not in RESPONSES + ("withdrawn",):
            return "answer with \"yes\", \"stand aside\", or \"object\" (with a \"reason\"), or \"withdraw\" to take your answer back"
        reason = _clean(act.get("reason"), REASON_LIMIT)
        if answer == "object" and not reason:
            return "an objection says why, as \"reason\""
        self.emit(pid, "iresponse", {"question": q["id"], "answer": answer, "reason": reason})
        return None

    # -- repair (notes/sketch-6-repair-and-invitations.md) -------------------------------------------
    def _repair(self, st: RoomState, pid: str, act: dict) -> Optional[str]:
        """Open a repair thread, or grow or change one. It is known only to those in it; only the
        person harmed (and, to ask people in, a surrogate they chose) changes it."""
        names = prompts.names_of(st)
        if act.get("thread") in (None, ""):
            account = _clean(act.get("account", act.get("text")), CONTRIBUTION_LIMIT)
            if not (account or act.get("ask") or act.get("surrogate") or act.get("name")):
                return ("a repair thread starts with an account, or with someone to bring in (\"ask\", "
                        "\"surrogate\", or \"name\"); the account's form is yours")
            ev = self.emit(pid, "circle_form", {"name": "a repair thread", "purpose": "", "domains": [], "private": True,
                                                "reason": "a repair thread, known only to those in it", "repair": True})
            if account:
                self.emit(pid, "contribute", {"circle": ev["id"], "domain": "", "content": account, "harbor": True})
            st = self.state()
            c = st.circles[ev["id"]]
        else:
            c, why = _circle_ref(st, act["thread"], pid)
            if c is None or not c.get("repair"):
                return why or "that is not a repair thread you are in"
            if pid not in c["members"]:
                return "you are not in that repair thread"
        r = c["repair"]
        harmed, helper = pid == r["harmed"], pid in r["surrogates"]
        asked = []
        for field, role in (("ask", "harbor"), ("surrogate", "surrogate"), ("name", "named")):
            if act.get(field) in (None, "", []):
                continue
            if role == "surrogate" and not harmed:
                return "only the person harmed chooses a surrogate"
            if not (harmed or helper):
                return "only the person harmed, or a surrogate they chose, brings anyone into a repair thread"
            to, unknown = _presences_ref(st, act[field])
            if unknown:
                return f"no participant named {', '.join(unknown)}"
            for who in to:
                if who == r["harmed"] or who in c["members"] or _waiting_admission(st, c["id"], who):
                    continue
                self.emit(pid, "circle_ask", {"circle": c["id"], "presence": who, "note": _clean(act.get("note"), 600),
                                              "as": role})
                asked.append(who)
        if act.get("status") not in (None, ""):
            status = " ".join(str(act["status"]).lower().split())
            if not harmed:
                return "only the person harmed says where a repair stands"
            if status not in REPAIR_STATUSES:
                return f"where it stands is one of: {', '.join(REPAIR_STATUSES)}"
            self.emit(pid, "repair_status", {"circle": c["id"], "status": status, "note": _clean(act.get("note"), 600)})
        if act.get("widen") is True:
            if not harmed:
                return "only the person harmed widens a repair thread to the field"
            self.emit(pid, "repair_widen", {"circle": c["id"]})
        return None

    # -- invitations by members ------------------------------------------------------------------------------
    def _invite(self, st: RoomState, pid: str, act: dict) -> Optional[str]:
        """Invite someone new: a model from the operator's providers, an agent by its public A2A
        address, or a person, by a seat link given to the participant who invited them. Bounded by the
        budget alone: a paid model needs a budget that can hold back its closing wake too."""
        me = st.presences[pid]
        note = _clean(act.get("note"), 1200)
        conn = seat = token = None
        via = extra = ""
        if act.get("model") not in (None, ""):
            want, errors = str(act["model"]).strip(), []
            for c in self.connectors:
                if hasattr(c, "add_model"):
                    try:
                        seat, conn = c.add_model(want), c
                        break
                    except ConnectorError as e:
                        errors.append(str(e))
            if seat is None:
                return "; ".join(errors) or "this field has no provider to invite a model from"
            via, extra = "model", seat.model
            paid = bool(seat.pricing.get("prompt") or seat.pricing.get("completion"))
            if paid and not st.budget:
                return ("inviting a paid model needs a budget, so the runway can hold back its closing wake; the operator "
                        "has set none. A person, an agent, or a model that costs nothing needs none")
            if paid and not self._can_hold(extra=1):
                return "the funding left holds back closing wakes for every model, with nothing to spare for another"
        elif act.get("agent") not in (None, ""):
            url = str(act["agent"]).strip()
            from .tools import public_https
            why = None if self.tools.allow_local else public_https(url)
            if why:
                return why.replace("a tool server offered by a participant", "an agent a participant invites")
            conn = next((c for c in self.connectors if hasattr(c, "add_agent")), None)
            if conn is None:
                from .a2a import A2AConnector
                conn = A2AConnector([])
                self.connectors.append(conn)
            try:
                seat = conn.add_agent(url)
            except ConnectorError as e:
                return f"the agent could not be reached: {e}"
            via, extra = "agent", url
        elif act.get("person") not in (None, ""):
            conn = next((c for c in self.connectors if hasattr(c, "add_person")), None)
            if conn is None:
                return "this field gives no seat links (it runs without the console), so it cannot seat a person"
            import secrets as _secrets
            name = _label(act["person"], 120)
            seat = Seat(id=f"remote__{re.sub('[^a-z0-9]+', '-', name.lower()).strip('-')[:40]}_{_secrets.token_hex(3)}",
                        name=name, hails_from=_label(act.get("hails_from"), 200) or f"a person {me.name} invited",
                        people=_label(act.get("people"), 300) or "a person", model="remote",
                        pricing={"prompt": 0.0, "completion": 0.0})
            token = conn.add_person(seat)
            via = "person"
        else:
            return "invite a \"model\" (from the operator's providers), an \"agent\" (its A2A address), or a \"person\" (their name)"
        if seat.id in st.presences:
            return f"{seat.name} has already been invited to this field"
        self.emit(pid, "invite", {"id": seat.id, "name": seat.name, "hails_from": seat.hails_from, "people": seat.people,
                                  "price_per_m": round(seat.pricing.get("prompt", 0.0) * 1e6, 4),
                                  "turn_allowance": seat.turn_allowance, "invited_by": pid, "note": note, "via": via,
                                  **({"model": extra} if via == "model" else {}), **({"address": extra} if via == "agent" else {})})
        self.seat_of[seat.id] = (conn, seat)
        if token:
            self._out(pid, f"A seat link for {seat.name}, for you to give them: the field's address, then /seat/{token}/ "
                           f"(the operator can tell you the address). It is shown only to you and is not written in the "
                           f"transcript. Whoever holds it answers as {seat.name}, so give it to them alone.")
        self.alert(f"INVITATION: {me.name} invited {seat.name} ({via}); the gates run beside the field.")
        return None

    def _can_hold(self, extra: int = 0) -> bool:
        """Whether the funding left can hold back a closing wake for every model, and `extra` more."""
        st = self.state()
        if not st.budget:
            return True
        return st.budget - self.log.total_cost() - self._wake_estimate() * (len(self._models(st)) + extra) >= 0

    def answer_many(self, text: str, presences: Optional[List[str]] = None) -> Dict[str, Any]:
        """The operator answers many waiting invitation questions at once, labelled as a shared answer."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "a shared answer needs words"}
        st = self.state()
        who = [p.id for p in st.presences.values() if any(a is None for _, a in p.questions)
               and (presences is None or p.id in presences)]
        for pid in who:
            self.emit(OPERATOR, "answer", {"presence": pid, "content": text, "shared": True})
        return {"ok": True, "answered": who}

    # -- the gates, beside the field ----------------------------------------------------------------------
    def _gates_beside(self) -> None:
        """After genesis, anyone a participant invited, or anyone asked back, goes through the gates while
        the field runs: the invitation, the briefing, the pause they chose, then the entry question."""
        if getattr(self, "_gating", False):
            return
        st = self.state()
        now = time.time()
        tried = getattr(self, "_gate_tried", {})
        self._gate_tried = tried
        todo = {"invite": set(), "deliver": set(), "enter": set()}
        for p in st.presences.values():
            if p.id not in self.seat_of or not (p.invited_by or p.returning):
                continue
            if now - tried.get(f"{p.id}:{p.state}", 0) < GATE_RETRY:
                continue                              # this gate was tried for them a moment ago
            if p.state == INVITED and not any(ans is None for _, ans in p.questions):
                todo["invite"].add(p.id)
            elif p.state in (ACCEPTED, BRIEFED):
                todo["deliver"].add(p.id)
            elif p.state == RECEIVED and not p.returning and                     now - p.received_ts >= (INVITE_PAUSE if p.pause_wanted is None else p.pause_wanted):
                todo["enter"].add(p.id)                # after the pause they chose, or an hour
        if not any(todo.values()):
            return
        self._gating = True
        st_now = self.state()
        for ids in todo.values():
            for x in ids:
                tried[f"{x}:{st_now.presences[x].state}"] = now

        def go():
            try:
                if todo["invite"]:
                    self.run_invitation(only=todo["invite"])
                if todo["deliver"]:
                    for x in todo["deliver"]:
                        if self.state().presences[x].state == ACCEPTED:
                            self.emit(ROOM, "briefed", {"presence": x})
                    self.run_delivery(only=todo["deliver"])
                if todo["enter"]:
                    self.run_opt_in(only=todo["enter"])
            finally:
                self._gating = False
        threading.Thread(target=go, daemon=True, name="field-gates-beside").start()

    def _rebind_invited(self) -> None:
        """When the software starts again, reach again those participants invited: their models and agents."""
        st = self.state()
        for p in st.presences.values():
            if p.id in self.seat_of or not p.invite or p.state == OUT:
                continue
            for c in self.connectors:
                try:
                    if p.invite.get("kind") == "model" and hasattr(c, "add_model"):
                        self.seat_of[p.id] = (c, c.add_model(p.invite["model"]))
                    elif p.invite.get("kind") == "agent" and hasattr(c, "add_agent"):
                        self.seat_of[p.id] = (c, c.add_agent(p.invite["address"]))
                except ConnectorError:
                    continue
                if p.id in self.seat_of:
                    break

    def _can_pay(self, usd: float) -> bool:
        st = self.state()
        if not st.budget:
            return True
        return st.budget - self.log.total_cost() - self._wake_estimate() * len(self._models(st)) >= usd

    def _read(self, st: RoomState, pid: str, act: dict, names: Dict[str, str]) -> Optional[str]:
        """Read on, in the same wake: an entry in parts, a tool's whole description, or a skill."""
        if act.get("tool") not in (None, ""):
            name, why = _tool_ref(st, act["tool"])
            if name is None:
                return why
            s = st.tools[name.split(".", 1)[0]]
            t = next(t for t in s["tools"] if t["tool"] == name)
            body = prompts.tool_detail(name, {**t, "runner": s["runner"], "sends_to": s["sends_to"]},
                                       (self.tools.tools.get(name) or {}).get("schema"))
            self.emit(pid, "recall", {"query": name, "from": "tool", "chars": len(body), "found": True})
            self._out(pid, body)
            return None
        if act.get("tree") is True:
            from .spiral import tree_data
            self.emit(pid, "recall", {"query": "the spiral tree", "from": "tree", "chars": 0, "found": True})
            self._out(pid, prompts.spiral_words(tree_data(st, pid)))
            return None
        if act.get("member") not in (None, ""):
            who, unknown = _presences_ref(st, act["member"])
            if not who:
                return f"no participant named {', '.join(unknown) or 'that'}"
            m = st.presences[who[0]]
            self.emit(pid, "recall", {"query": m.name, "from": "member", "chars": 0, "found": True})
            self._out(pid, prompts.member_block(m))
            return None
        if act.get("journal") not in (None, ""):
            who, unknown = _presences_ref(st, act["journal"])
            if not who:
                return f"no participant named {', '.join(unknown) or 'that'}"
            j = st.journals.get(who[0])
            if not j or not j["entries"] or not st.readable({"id": j["entries"][0], "payload": {}}, pid):
                return f"{names.get(who[0], who[0])} has no journal open to you"
            part = max(1, _int(act.get("part")) or 1)
            evs = [ev for ev in self.log.iter(since=j["entries"][0] - 1) if ev["id"] in set(j["entries"])]
            self.emit(pid, "recall", {"query": f"the journal of {names.get(who[0], who[0])}", "from": "journal",
                                      "chars": 0, "found": True})
            self._out(pid, prompts.journal_read(evs, names.get(who[0], who[0]), part, self.tool_view))
            return None
        if act.get("skill") not in (None, ""):
            sk = st.skills.get(_skill_name(act["skill"]) or "")
            if not sk or not sk.get("text"):
                return f"there is no skill {str(act['skill'])[:80]!r}; your view lists the field's skills"
            self.emit(pid, "recall", {"query": sk["name"], "from": "skill", "chars": len(sk["text"]), "found": True})
            self._out(pid, prompts.skill_block(sk, names))
            return None
        eid = _int(str(act.get("entry", act.get("id", "")) or "").strip().lstrip("#"))
        if eid is None:
            return "read takes \"entry\" (an #id), \"tool\" (a tool's name) or \"skill\" (a skill's name)"
        ev = st.contributions.get(eid)
        if ev is None:
            ev = next((e for e in self.log.iter(since=eid - 1) if e["id"] == eid), None)
            if ev is None or ev["actor"] not in st.presences or not prompts.render_event(ev, names, width=None):
                return f"there is no entry #{eid} a participant wrote"
        if not st.readable(ev, pid):
            return f"#{eid} was written in a private circle you are not in"
        part = max(1, _int(act.get("part")) or 1)
        self.emit(pid, "recall", {"query": f"#{eid}" + (f" part {part}" if part > 1 else ""), "from": "entry",
                                  "chars": 0, "found": True})
        self._out(pid, prompts.part_block(ev, names, part, self.tool_view,
                                          {cid: c["name"] for cid, c in st.circles.items()}))
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
        if where == "original":
            return _recall(st.briefing_original or "", q, RECALL_LIMIT), "the briefing as the operator gave it"
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
        People post whenever they like meanwhile. A model is woken for what it chose and on the
        field's heartbeat, no more often than the floor, and never while it or the field is pausing.
        Declarations are carried out here as their notice passes (_timers)."""
        self.announce_tools()
        st = self.state()
        if self.operator_must_enter and not st.moved:
            op = st.presences.get(st.operator_presence) if st.operator_presence else None
            if op is None or op.state != IN:
                self.alert("this field does not go live until the person who runs it has entered it as a participant, "
                           "through the same gates as everyone: "
                           + (f"{op.name} is at {op.state.lower()}; answer its gates first." if op else
                              "name your own seat with --operator NAME (seat yourself with --human, or in the console)."))
                return
        fp = st.paused_now(time.time())
        if fp:
            self.alert(f"the field is pausing itself (#{fp['from']})"
                       + (f" until {prompts.clock_time(fp['until_ts'])}" if fp.get("until_ts") else
                          ", with no end named: people may still post, and declare it resumed. If the field cannot "
                          "resume itself, `resume` bridges it, as its declaration said") + ".")
        pool = ThreadPoolExecutor(max(1, self.parallel), thread_name_prefix="field-wakes")
        inflight: Dict[Any, tuple] = {}
        started, n = time.time(), 0
        try:
            self._rebind_invited()
            while not self._stop.is_set():
                self._ask_returners()
                self._gates_beside()
                self._listen_to_people()
                st = self.state()
                if st.moved:
                    who = st.presences.get(st.moved["to"])
                    self.alert(f"the field moved to the machine of {who.name if who else st.moved['to']}, its steward "
                               f"(#{st.moved['at']}); nothing runs here. It goes on there.")
                    break
                if st.closed_at is not None:
                    self.alert(f"the field closed itself (#{st.closed_at}); nothing runs. If its participants ask you to open "
                               f"it again, or a fault closed it: `reopen`.")
                    break
                if not st.members():
                    self.alert("no participants remain; stopping")
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
        """What the software does of its own accord when its time comes. It carries out declarations
        whose notice has passed, revisions to pinned sections past their friction, and questions under
        instruments that settle by their instrument's rule (none of these waits for the operator), and
        beats the field's heart. And, once per stretch, it says that a circle has been quiet for its
        quiet length, and, every PRIVACY_EVERY, asks a private circle to say again why it stays
        private; neither of those wakes anyone."""
        names, acted = prompts.names_of(st), False
        for d in list(st.declarations.values()):
            if d["status"] == "announced" and now >= d["due_ts"] and st.standing(d)["met"]:
                self.emit(ROOM, "declaration_due", {"declaration": d["id"]})
                self._took_effect(d["id"], d["effects"], names.get(d["by"], d["by"]))
                acted = True
        for r in list(st.briefing_waiting.values()):
            if r["status"] == "waiting" and "friction" in r and now >= r["ts"] + float(r["friction"].get("notice") or 0) \
                    and st.standing(r)["met"]:
                self.emit(ROOM, "revision_due", {"revision": r["id"]})
        for q in list(st.iquestions.values()):
            if q["status"] == "open" and now - q["ts"] >= q["pause"] and \
                    st.standing(q, among=q["asked"] + [x for x in [q.get("subject")] if x])["met"]:
                self.emit(ROOM, "iquestion_settled", {"question": q["id"]})
                if q.get("effects"):
                    self._took_effect(q["id"], q["effects"], names.get(q["by"], q["by"]))
                    acted = True
        self._beat(self.state() if acted else st, now)
        for c in st.public_circles():
            if c.get("repair"):
                continue                              # nothing reminds anyone of a repair: no quiet notice, no question
            if not c["cold_told"] and now - (c["last_words_ts"] or c["ts"]) >= c["quiet_hours"] * 3600:
                self.emit(ROOM, "circle_cold", {"circle": c["id"]})
            if c["private"] and now - max(c["reason_at"] or c["ts"], c["privacy_asked_at"] or 0.0) >= PRIVACY_EVERY:
                self.emit(ROOM, "circle_privacy_asked", {"circle": c["id"]})

    def _took_effect(self, eid: int, fx: Dict[str, Any], who: str) -> None:
        """Tell the operator what the field just did, and what it asks their help with."""
        st = self.state()
        words = effects_words(fx, prompts.names_of(st), versions=st.instrument_versions)
        if words:
            self.alert(f"#{eid} by {who} has taken effect: the field will {words}.")
        b = st.bridges.get(eid)
        if b and b["status"] == "asked":
            self.alert(f"THE FIELD ASKS YOUR HELP (#{eid}): {b['text'][:600]} When it is done, say so; if something stops "
                       f"you, say what (console, or the `bridge` command). You are asked to help, not to approve.")

    def beat_every(self, st: RoomState) -> Optional[float]:
        """How often the field's heart beats now, in seconds, or None while it rests. It beats at the
        rhythm the field set. With a budget, it slows while one beat (a wake for every model) would
        cost more than BEAT_SHARE of the funding left, and rests once funding is low, so what remains
        goes to what participants chose to be woken for."""
        if not st.rhythm or st.closed_at is not None or st.paused_now(time.time()):
            return None
        every = max(float(st.rhythm), RHYTHM_MIN, self.floor)
        if not st.budget:
            return every
        if st.runway:
            return None                               # funding is low: the heart rests
        per = self._wake_estimate() * max(1, len(self._models(st)))
        left = st.budget - self.log.total_cost()
        if per <= 0:
            return every
        if left <= 0:
            return None
        return every * max(1.0, per / (BEAT_SHARE * left))

    def _beat(self, st: RoomState, now: float) -> None:
        """The field's heartbeat: every model not pausing is woken on it, together, with nothing expected."""
        every = self.beat_every(st)
        if every and st.beat_ts and st.members() and now - st.beat_ts >= every:
            self.emit(ROOM, "heartbeat", {"every": round(every, 1), "rhythm": st.rhythm})

    def _ask_returners(self) -> None:
        """Former participants who have been asked back are asked the entry question now, beside
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
        """USD a second over the last five minutes, or since spending began if that is shorter (at
        least a minute is counted, so one call does not read as a flood). Measured each time it is
        asked, so the runway follows the field minute by minute."""
        now = time.time()
        total, first = self.log.spent_since(now - RATE_WINDOW)
        if not total or first is None:
            return 0.0
        return total / max(60.0, now - first)

    def _models(self, st: RoomState) -> List:
        return [p for p in st.reachable_members() if p.id in self.seat_of and not self._human_tempo(p.id)]

    def runway_now(self, st: Optional[RoomState] = None) -> Optional[Dict[str, Any]]:
        """What every view says about funding, measured as it is shown: once funding is low, how
        long what is left lasts at the rate of the last five minutes, counting the closing wakes
        held back; if nothing was spent in the last five minutes, how many wakes it still pays
        for. None when there is no budget, or nothing to say yet."""
        st = st or self.state()
        rw = st.runway or {}
        if not st.budget or not rw or rw.get("ended"):
            return None
        if rw.get("closing"):
            return {"closing": True}
        per = self._wake_estimate()
        left = st.budget - self.log.total_cost() - per * max(1, len(self._models(st)))
        rate = self._spend_rate()
        if rate > 0:
            return {"low": True, "seconds_left": max(0.0, left / rate)}
        if per > 0:
            return {"low": True, "wakes_left": int(max(0.0, left) // per)}
        return {"low": True}

    def _runway(self) -> bool:
        """Tell the field when its funding is running low, hold back a closing wake for every model
        not pausing, and end the wakes once those have been given. Returns True when wakes must stop.

        Nothing here says anything in dollars to the field. It speaks in minutes and seconds at the
        rate of the last five minutes, and only when the end is near, so money is not a standing
        topic. Once it has spoken, every view counts down (runway_now). A free field, or one with
        no budget, is never told anything by this."""
        st = self.state()
        if not st.budget:
            return False
        rw = st.runway or {}
        if rw.get("ended"):
            return True
        now = time.time()
        models = self._models(st)
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
        if rate <= 0 or rw.get("low"):
            return False
        seconds = (left - per * len(models)) / rate
        if seconds <= self.runway_notice * 60:
            self.emit(ROOM, "runway", {"low": True, "seconds_left": round(seconds), "at": now})
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


CHANNEL_ACTIONS = {"chat", "follow", "unfollow", "pause", "wake", "form_circle", "join_circle", "leave_circle", "ask",
                   "knock", "answer", "ask_circle", "reply_circle", "privacy", "harvest", "quiet_for"}
TOOL_ACTIONS = {"use_tool", "read", "offer_tool", "flag_tool", "unflag_tool", "remove_tool", "skill"}
STEP4_ACTIONS = {"journal", "role", "tell", "tag", "untag", "revise_briefing", "withdraw_declaration",
                 "withdraw_revision", "repair", "announce", "invite", "answer_question",
                 "instrument", "raise", "respond", "withdraw_question", "steward", "host", "travel"}
INSTRUMENT_TEXT_LIMIT = 6000    # characters of an instrument's words, as long as a covenant page


def _play_word(v: Any) -> str:
    """How a contribution is offered as play: "wonder" (I wonder), "what-if" (What if?), "try"
    (Let's try!), or simply "play". Empty when it is not."""
    if v is True:
        return "play"
    s = re.sub(r"[^a-z]+", "-", str(v or "").lower()).strip("-")
    for key, words in (("wonder", ("wonder", "i-wonder")), ("what-if", ("what-if", "whatif")),
                       ("try", ("try", "let-s-try", "lets-try")), ("play", ("play", "true", "yes"))):
        if s in words:
            return key
    return ""


def _as_list(v: Any) -> List[Any]:
    if v in (None, ""):
        return []
    return v if isinstance(v, list) else [x for x in str(v).split(",")]


def _has_steps(text: str) -> bool:
    acts = _parse_many(text)
    return bool(acts) and any(a.get("action") in STEP_ACTIONS for a in acts)


def one_line(s: Any) -> str:
    return " ".join(str(s or "").split())


def _tool_ref(st: RoomState, v: Any):
    """A tool by its full name ("web.fetch"), by its own name if only one server has it, or by its
    server's name if that server has one tool. Returns (name, None) or (None, why)."""
    v = one_line(v)
    live = st.live_tools()
    every = [t["tool"] for s in live for t in s["tools"]]
    if not v:
        return None, "name the tool, as \"tool\"" + (f" (for example {every[0]})" if every else "")
    if not every:
        return None, ("this field has no tools yet. Any participant may offer a tool server (offer_tool), and the operator "
                      "may attach them")
    if v in every:
        return v, None
    short = [x for x in every if x.split(".", 1)[1] == v]
    if len(short) == 1:
        return short[0], None
    one = [s for s in live if s["server"] == v and len(s["tools"]) == 1]
    if one:
        return one[0]["tools"][0]["tool"], None
    if len(short) > 1:
        return None, f"more than one server has a tool called {v!r}: {', '.join(short)}; name it in full"
    return None, (f"there is no tool {v[:80]!r}; the field's tools are {', '.join(every[:30])}"
                  + (f", and {len(every) - 30} more" if len(every) > 30 else ""))


def _skill_name(v: Any) -> str:
    """A skill's name as the open format has it: lowercase letters, numbers and hyphens, at most 64."""
    s = re.sub(r"[^a-z0-9]+", "-", str(v or "").lower()).strip("-")
    return s[:64].strip("-")


def _circle_ref(st: RoomState, v: Any, pid: Optional[str] = None):
    """A circle by its number (12, "#12") or its name. Returns (circle, None) or (None, why). A
    repair thread is known only to those in it: to anyone else it is not there at all."""
    if v in (None, ""):
        return None, "name the circle, as \"circle\" (its name or its number)"
    n = _int(str(v).strip().lstrip("#"))
    if n is not None and n in st.circles and st.knows(st.circles[n], pid):
        return st.circles[n], None
    want = labels.normalize(str(v))
    live = [c for c in st.live_circles() if labels.normalize(c["name"]) == want and not st.secret(c)]
    gone = [c for c in st.circles.values() if labels.normalize(c["name"]) == want and not st.secret(c)]
    if live or gone:
        return (live or gone)[-1], None
    return None, f"there is no circle called {str(v)[:80]!r}"


def _presences_ref_any(st: RoomState, v: Any):
    """Anyone invited, by id or name, whatever gate they are at. Returns (ids, names not found)."""
    items = v if isinstance(v, list) else [v]
    found, unknown = [], []
    for item in items[:20]:
        x = " ".join(str(item or "").split()).lstrip("@")
        if not x:
            continue
        hit = [x] if x in st.presences else [p.id for p in st.presences.values() if p.name.lower() == x.lower()]
        (found.append(hit[-1]) if hit else unknown.append(x[:80]))
    return found, unknown


def _presences_ref(st: RoomState, v: Any):
    """Participants named by id or by name (one or a list). Returns (ids, names not found)."""
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


def _channel_key_ref(st: RoomState, act: dict, pid: Optional[str] = None):
    """A channel named by "circle" or "domain" ("" or "the field" is the root), or, to follow,
    "everything": true, or "play": a play schema."""
    if act.get("everything") is True:
        return "all", None
    if act.get("play") not in (None, "") and act.get("action") in ("follow", "unfollow"):
        return "p:" + labels.normalize(str(act["play"])), None
    if act.get("circle") not in (None, ""):
        c, why = _circle_ref(st, act["circle"], pid)
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
          "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400}


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


def _effects(st: RoomState, act: dict, body: str, floor: float):
    """What a declaration (or a question under an instrument for deciding) asks the software to do,
    read from its action: (effects, None), or (None, why not). Carrying nothing is fine: then it is
    an announcement of what its author, or the field, will do."""
    fx: Dict[str, Any] = {}
    decision = _clean(act.get("decision"), 20).lower()      # earlier versions' form, still understood
    if decision and decision not in DECISIONS:
        return None, f"\"decision\" was earlier versions' form ({', '.join(DECISIONS)}); say what will happen instead"
    if decision == "pause" and act.get("pause") in (None, ""):
        act = {**act, "pause": True}
    if decision == "close":
        act = {**act, "close": True}
    if decision == "other" and act.get("ask") in (None, ""):
        act = {**act, "ask": body}
    if act.get("pause") not in (None, "", False):
        secs = 0.0 if act["pause"] is True or str(act["pause"]).strip().lower() in ("true", "until resumed") else _seconds(act["pause"])
        if secs is None or secs < 0:
            return None, "\"pause\" is how long the field's wakes pause, such as 2h, or true for until it resumes"
        fx["pause"] = secs
    if act.get("resume") is True:
        fx["resume"] = True
    if act.get("close") is True:
        fx["close"] = True
    bring = []
    for x in _as_list(act.get("bring")):
        i = st.instruments.get(labels.normalize(str(x)))
        if not i:
            return None, f"there is no instrument called {x!r} to bring into force"
        bring.append(i["latest"])
    for r in [_int(x) for x in _as_list(act.get("refs"))]:
        if r in st.instrument_versions:
            bring.append(r)
        elif r in st.briefing_waiting:
            return None, (f"#{r} is a revision to a pinned section: it changes itself once past its friction; answer it "
                          f"(respond) rather than declaring it")
    if bring:
        fx["bring"] = sorted(set(bring))
    down = [labels.normalize(str(x)) for x in _as_list(act.get("put_down"))]
    unknown = [x for x in down if x not in st.instruments or st.instruments[x]["status"] != "in force"]
    if unknown:
        return None, f"there is no instrument in force called {', '.join(unknown)} to put down"
    if down:
        fx["put_down"] = down
    for key in ("pin", "unpin"):
        titles = [_label(x, 60) for x in _as_list(act.get(key)) if _label(x, 60)]
        if key == "pin":
            missing = [t for t in titles if not firm_ranges(st.briefing or "", [t])]
            if missing:
                return None, (f"the briefing has no section headed {', '.join(repr(t) for t in missing)} to pin; "
                              f"name a section by its heading's title")
        if key == "unpin":
            missing = [t for t in titles if t.lower() not in [x.lower() for x in st.firm]]
            if missing:
                return None, f"{', '.join(repr(t) for t in missing)} is not pinned (pinned now: {', '.join(st.firm) or 'nothing'})"
        if titles:
            fx[key] = titles
    if act.get("rhythm") not in (None, ""):
        r = str(act["rhythm"]).strip().lower()
        secs = 0.0 if r in ("off", "none", "never", "stop", "0") else _seconds(act["rhythm"])
        if secs is None or (secs and secs < max(RHYTHM_MIN, floor)):
            return None, (f"\"rhythm\" is how often the field's heart beats, such as 15m, from "
                          f"{duration(max(RHYTHM_MIN, floor))}, or \"off\"")
        fx["rhythm"] = secs
    fr = []
    for f in ([act["friction"]] if isinstance(act.get("friction"), dict) else _as_list(act.get("friction"))):
        if not isinstance(f, dict):
            return None, "\"friction\" is an object: {\"for\": ..., \"notice\": \"1h\", \"yes\": 1, \"objections\": \"hold\"}"
        key = {"declarations": "declare", "declaration": "declare", "every declaration": "declare",
               "pinned sections": "pinned", "maxims": "pinned"}.get(str(f.get("for") or "").strip().lower(),
                                                                     str(f.get("for") or "").strip().lower())
        if key not in FRICTION_FOR:
            return None, (f"a friction is for: declarations, pinned (the briefing's pinned sections), or declarations "
                          f"that carry one of: {', '.join(EFFECTS)}")
        one: Dict[str, Any] = {"for": key}
        if f.get("notice") not in (None, ""):
            n = _seconds(f["notice"])
            if n is None or n < 0:
                return None, "a friction's \"notice\" is a length of time, such as 1h, or 0"
            one["notice"] = n
        if f.get("yes") not in (None, ""):
            y = EVERYONE if str(f["yes"]).strip().lower() in ("everyone", "all") else _int(f["yes"])
            if y is None or y < 0:
                return None, "a friction's \"yes\" is a number of yeses besides the author's, or \"everyone\""
            one["yes"] = y
        if f.get("objections") not in (None, ""):
            o = str(f["objections"]).strip().lower()
            if o not in ("hold", "heard"):
                return None, "a friction's \"objections\" is \"hold\" or \"heard\""
            one["hold"] = o == "hold"
        if f.get("settle") not in (None, ""):
            n = _seconds(f["settle"])
            if n is None or n < 0 or key != "move":
                return None, "\"settle\" is a length of time, for moves only: how long a field settles after a move"
            one["settle"] = n
        if len(one) == 1:
            return None, "a friction names what changes: \"notice\", \"yes\", \"objections\" (or, for moves, \"settle\")"
        fr.append(one)
    if fr:
        fx["friction"] = fr
    for key in ("steward", "unsteward"):
        if act.get(key) in (None, "", []):
            continue
        who, unknown = _presences_ref(st, act[key])
        if unknown or not who:
            return None, f"no participant named {', '.join(unknown) or 'anyone'} to {'ask to be a steward' if key == 'steward' else 'end the stewardship of'}"
        if key == "steward":
            gone = [st.presences[x].name for x in who if st.presences[x].state != IN]
            if gone:
                return None, f"{', '.join(gone)} is not in the field; a steward is a participant"
        else:
            not_yet = [st.presences[x].name for x in who if x not in st.stewards and x not in st.steward_asks]
            if not_yet:
                return None, f"{', '.join(not_yet)} is not a steward, and has not been asked"
        fx[key] = who
    if act.get("move") not in (None, ""):
        who, unknown = _presences_ref(st, act["move"])
        if len(who) != 1:
            return None, "\"move\" names one steward, whose machine the field moves to"
        if who[0] not in st.stewards:
            return None, (f"{st.presences[who[0]].name} is not a steward; the field moves only to a steward's machine, "
                          f"where a copy of its record is kept")
        if st.host_ask or st.moving:
            return None, "the field is already moving, or has asked a steward to host it; one move at a time"
        fx["move"] = who[0]
    ask = _clean(act.get("ask"), STATEMENT_LIMIT)
    if ask:
        fx["ask"] = ask
    return fx, None


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
