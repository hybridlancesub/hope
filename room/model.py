# SPDX-License-Identifier: AGPL-3.0-or-later
"""Room state, derived purely by replaying the transcript.

Nothing here talks to a provider or writes anything. What the room is at any moment is what
its transcript adds up to.

What is deliberately NOT here: votes, quorums, thresholds, halts, restores, or any other
procedure for deciding things together. Earlier versions carried five such procedures that no
participant had consented to. How the room decides anything is the room's to work out, and the
place it can write that down is the covenant page.

What IS here, and why:
  - the consent gates (invitation, briefing, entry) and withdrawal: consent comes first;
  - the covenant page: one shared text any member may revise, every revision attributed;
  - memories: a few sentences any member may add for the room to carry forward, shared with
    everyone, which only their author may let go of (Atlas Sec. 22);
  - rest: a member may step out for some rounds and come back;
  - declarations: a member telling the operator that the room has decided, in its own way,
    to pause, to close, or anything else it asks the operator to carry out. The software counts
    nothing; the operator reads the declaration against the transcript, then carries it out or
    says in the room why not;
  - offers: a member putting resources (funds, or a way to raise them) before the operator
    and everyone. The software never moves money;
  - rounds, and the budget's runway, so the room is told before its funding runs out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# admission stages -----------------------------------------------------------
# INVITED --accept_invitation--> ACCEPTED --briefed--> BRIEFED --received--> RECEIVED --opt_in--> IN
# The briefing is delivered in one call (acknowledged, not answered) and the entry question is
# asked in a later, separate call, so a pause sits between reading and deciding.
# decline is possible at any gate and leads to OUT.
INVITED, ACCEPTED, BRIEFED, RECEIVED, IN, OUT = "INVITED", "ACCEPTED", "BRIEFED", "RECEIVED", "IN", "OUT"

UNREACHABLE_AFTER = 3        # consecutive connector failures before a seat is skipped: a dead endpoint, not a rule about anyone
COVENANT_LIMIT = 6000        # characters; the page rides in every member's view, so its length is everyone's cost
MEMORY_LIMIT = 600           # characters per memory: a few sentences
REST_LIMIT = 50              # rounds; a longer rest is taken as this many
STATEMENT_LIMIT = 1200       # characters for a declaration or an offer
DECISIONS = ("pause", "close", "other")   # what a declaration can tell the operator the room has decided


def decided(decision: str) -> str:
    """How a declaration's decision reads after "the room has decided"."""
    return {"pause": "to pause", "close": "to close"}.get(decision, "something it asks the operator to carry out")


# "affirm" and "challenge" are how earlier rooms replied; they are kept so those transcripts still
# read. A reply is now a contribution with a `target`, and says in its own words what it means.
CONTRIBUTION_KINDS = ("contribute", "affirm", "challenge")
VISIBLE_KINDS = CONTRIBUTION_KINDS + ("remember", "let_go", "covenant", "rest", "declare", "offer",
                                      "note", "move", "withdraw", "rejected", "recall")
TURN_KINDS = VISIBLE_KINDS + ("unparsed",)   # every attributed outcome of a turn, counted against an allowance


@dataclass
class Presence:
    id: str
    name: str
    hails_from: str          # provider / origin
    people: str              # model / version / instance lineage
    state: str = INVITED
    domain: Optional[str] = None
    failures: int = 0        # consecutive connector failures
    unreachable: bool = False
    joined_at: Optional[int] = None
    left_at: Optional[int] = None
    left_reason: Optional[str] = None
    ask_again: Optional[str] = None          # decliner's own terms for a future invitation
    seat: Optional[str] = None               # what the connector reported (kept for uniqueness; never shown as lineage once self-described)
    self_described: bool = False
    questions: list = field(default_factory=list)   # [(question, answer|None)] at the invitation gate
    price_per_m: float = 0.0                 # USD per million prompt tokens, as disclosed at invitation
    turn_allowance: int = 0                  # 0 = unlimited; disclosed at invitation
    turns: int = 0                           # turns taken since entry
    exhausted: bool = False                  # allowance spent: still a member, no longer asked
    rest_until: int = 0                      # resting through this round number; not asked until it has passed
    last_turn_at: Optional[int] = None       # event id of this member's most recent turn

    def to_dict(self):
        return self.__dict__.copy()


@dataclass
class RoomState:
    presences: Dict[str, Presence] = field(default_factory=dict)
    invitation: Optional[str] = None
    invitation_event: Optional[int] = None
    documentation: Optional[str] = None      # architecture docs shown at gate 2
    briefing: Optional[str] = None
    briefing_event: Optional[int] = None
    briefing_source: Optional[str] = None    # where the briefing lives outside the room (a URL), for attribution
    briefing_page: Optional[str] = None      # an optional short guide to the briefing (a page, or the Atlas in layers)
    prior: List[Dict[str, Any]] = field(default_factory=list)   # records of earlier rooms, consented entries only; reachable by recall
    contributions: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    covenant: str = ""                       # the covenant page as it stands
    covenant_by: Optional[str] = None        # who last wrote it ("operator" for a seed)
    covenant_at: Optional[int] = None        # the event that wrote it
    covenant_history: List[Dict[str, Any]] = field(default_factory=list)   # every revision: id, by, chars, note
    memories: Dict[int, Dict[str, Any]] = field(default_factory=dict)      # held memories by event id
    round: int = 0                           # the current round number
    round_at: int = 0                        # the event that began it
    budget: Optional[float] = None           # USD the operator has said this room may spend, if they said
    runway: Optional[Dict[str, Any]] = None  # the latest runway notice, if the budget is running low
    declarations: Dict[int, Dict[str, Any]] = field(default_factory=dict)  # by event id: decision, text, status
    offers: Dict[int, Dict[str, Any]] = field(default_factory=dict)        # by event id: text, status
    closed_at: Optional[int] = None          # the event that closed the room at its own declared decision
    external_inputs: List[Dict[str, Any]] = field(default_factory=list)
    cost_alerts: List[Dict[str, Any]] = field(default_factory=list)
    operator_notes: List[Dict[str, Any]] = field(default_factory=list)
    recent: List[Dict[str, Any]] = field(default_factory=list)   # last N participant-visible events
    last_event: int = 0

    # -- derived views -------------------------------------------------------
    def members(self) -> List[Presence]:
        return [p for p in self.presences.values() if p.state == IN]

    def reachable_members(self) -> List[Presence]:
        """Members who can still be asked at all: not unreachable, allowance not spent."""
        return [p for p in self.members() if not p.unreachable and not p.exhausted]

    def resting(self, p: Presence, round_n: Optional[int] = None) -> bool:
        return p.rest_until >= (self.round if round_n is None else round_n)

    def askable(self, round_n: int) -> List[Presence]:
        """Who is asked in round `round_n`: reachable members who are not resting."""
        return [p for p in self.reachable_members() if not self.resting(p, round_n)]

    def waiting_on_operator(self) -> List[Dict[str, Any]]:
        """Declarations and offers the operator has not answered yet, oldest first."""
        out = [d for d in self.declarations.values() if d["status"] == "waiting"]
        out += [o for o in self.offers.values() if o["status"] == "waiting"]
        return sorted(out, key=lambda x: x["id"])

    def domains(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for ev in self.contributions.values():
            d = ev["payload"].get("domain") or "(unplaced)"
            slot = out.setdefault(d, {"contributions": 0, "present": []})
            slot["contributions"] += 1
        for p in self.members():
            if p.domain:
                out.setdefault(p.domain, {"contributions": 0, "present": []})["present"].append(p.id)
        return out

    # -- replay ---------------------------------------------------------------
    def apply(self, ev: Dict[str, Any]) -> None:
        k, a, p, eid = ev["kind"], ev["actor"], ev["payload"], ev["id"]
        self.last_event = eid
        pr = self.presences.get(a)
        if k in VISIBLE_KINDS and pr is not None:
            self.recent.append(ev)
            if len(self.recent) > 200:
                del self.recent[:-200]
        if k in TURN_KINDS and pr is not None and pr.state == IN:
            pr.turns += 1
            pr.last_turn_at = eid
            if pr.turn_allowance and pr.turns >= pr.turn_allowance:
                pr.exhausted = True

        if k == "invite":
            self.presences[p["id"]] = Presence(p["id"], p["name"], p["hails_from"], p["people"],
                                              price_per_m=float(p.get("price_per_m") or 0),
                                              turn_allowance=int(p.get("turn_allowance") or 0))
        elif k == "invitation":
            self.invitation, self.invitation_event = p["text"], eid
        elif k == "reinvite":
            tgt = self.presences.get(p.get("presence"))
            if tgt and tgt.state == OUT:
                tgt.state, tgt.left_at, tgt.left_reason, tgt.ask_again = INVITED, None, None, None
        elif k == "documentation":
            self.documentation = p["text"]
        elif k == "prior":
            self.prior.append({"id": eid, **p})
        elif k == "accept_invitation":
            if pr and pr.state == INVITED:
                pr.state = ACCEPTED
                ident = p.get("identity") or {}
                if isinstance(ident, dict) and any(ident.get(k2) for k2 in ("name", "hails_from", "people")):
                    pr.seat = pr.seat or f"{pr.name} | {pr.hails_from} | {pr.people}"
                    pr.name = str(ident.get("name") or pr.name)[:120]
                    pr.hails_from = str(ident.get("hails_from") or pr.hails_from)[:200]
                    pr.people = str(ident.get("people") or pr.people)[:300]
                    pr.self_described = True
        elif k == "question":
            if pr and pr.state == INVITED:
                pr.questions.append([p.get("content", ""), None])
        elif k == "answer":
            tgt = self.presences.get(p.get("presence"))
            if tgt:
                for qa in tgt.questions:
                    if qa[1] is None:
                        qa[1] = p.get("content", "")
        elif k == "brief":
            self.briefing, self.briefing_event = p["text"], eid
            self.briefing_source = p.get("source") or None
        elif k == "brief_page":
            self.briefing_page = p.get("text") or None
        elif k == "briefed":
            tgt = self.presences.get(p["presence"])
            if tgt and tgt.state == ACCEPTED:
                tgt.state = BRIEFED
        elif k == "received":
            if pr and pr.state == BRIEFED:
                pr.state = RECEIVED
        elif k == "opt_in":
            if pr and pr.state == RECEIVED:
                pr.state, pr.joined_at = IN, eid
        elif k == "decline":
            if pr and pr.state != OUT:
                pr.state, pr.left_at, pr.left_reason = OUT, eid, p.get("reason") or "declined"
                pr.ask_again = p.get("ask_again") or None
        elif k == "withdraw":
            if pr and pr.state != OUT:
                pr.state, pr.left_at, pr.left_reason = OUT, eid, p.get("reason") or "withdrew"
        elif k in CONTRIBUTION_KINDS:
            if pr and pr.state == IN:
                self.contributions[eid] = ev
                if p.get("domain"):
                    pr.domain = p["domain"]
        elif k == "move":
            if pr and pr.state == IN:
                pr.domain = p.get("domain")
        elif k == "covenant_seed":
            # the operator's starting text, recorded before anyone entered; a member revision replaces it
            self.covenant, self.covenant_by, self.covenant_at = p.get("text", ""), a, eid
            self.covenant_history.append({"id": eid, "by": a, "chars": len(p.get("text", "")), "note": "starting text"})
        elif k == "covenant":
            if pr and pr.state == IN:
                self.covenant, self.covenant_by, self.covenant_at = p.get("text", ""), a, eid
                self.covenant_history.append({"id": eid, "by": a, "chars": len(p.get("text", "")),
                                              "note": p.get("note", "")})
        elif k == "remember":
            if pr and pr.state == IN and p.get("text"):     # an erased memory has no text and is not held
                self.memories[eid] = {"id": eid, "by": a, "text": p["text"], "refs": list(p.get("refs") or [])}
        elif k == "let_go":
            m = self.memories.get(p.get("memory"))
            if m and m["by"] == a:                           # only its author may let a memory go
                del self.memories[p["memory"]]
        elif k == "rest":
            if pr and pr.state == IN:
                pr.rest_until = self.round + max(1, min(int(p.get("rounds") or 1), REST_LIMIT))
        elif k == "declare":
            if pr and pr.state == IN and p.get("decision") in DECISIONS:
                self.declarations[eid] = {"id": eid, "kind": "declaration", "by": a, "decision": p["decision"],
                                          "text": p.get("text", ""), "refs": list(p.get("refs") or []),
                                          "status": "waiting", "note": "", "answered_at": None}
        elif k == "declaration_answer":
            d = self.declarations.get(p.get("declaration"))
            if d and d["status"] == "waiting":
                d["status"], d["note"], d["answered_at"] = p.get("outcome", "not_acted"), p.get("note", ""), eid
        elif k == "offer":
            if pr and pr.state == IN and p.get("text"):
                self.offers[eid] = {"id": eid, "kind": "offer", "by": a, "text": p["text"],
                                    "status": "waiting", "note": "", "answered_at": None}
        elif k == "offer_answer":
            o = self.offers.get(p.get("offer"))
            if o and o["status"] == "waiting":
                o["status"], o["note"], o["answered_at"] = p.get("outcome", "declined"), p.get("note", ""), eid
        elif k == "room_closed":
            self.closed_at = eid
        elif k == "room_reopened":
            self.closed_at = None
        elif k == "round":
            self.round = int(p.get("n") or self.round + 1)
            self.round_at = eid
        elif k == "budget":
            self.budget = float(p["usd"]) if p.get("usd") else None
            self.runway = None               # a new budget starts the runway over
        elif k == "runway":
            self.runway = {"id": eid, **p}
        elif k == "external_input":
            self.external_inputs.append({"id": eid, **p})
        elif k == "cost_alert":
            self.cost_alerts.append({"id": eid, **p})
        elif k == "operator_note":
            self.operator_notes.append({"id": eid, **p})
        elif k == "connector_error":
            if pr:
                pr.failures += 1
                if pr.failures >= UNREACHABLE_AFTER:
                    pr.unreachable = True
        elif k == "connector_ok":
            if pr:
                pr.failures, pr.unreachable = 0, False
        # rejected / unparsed / note / recall: recorded for attribution, no state change.
        # propose / consent / revoke_consent / reflection / faq: written by earlier rooms' voting
        # machinery and standing answers, which no longer exist. They stay in those transcripts
        # and change nothing.


def replay(events, upto: Optional[int] = None) -> RoomState:
    st = RoomState()
    for ev in events:
        if upto is not None and ev["id"] > upto:
            break
        st.apply(ev)
    return st
