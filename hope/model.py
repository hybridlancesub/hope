# SPDX-License-Identifier: AGPL-3.0-or-later
"""Field state, derived purely by replaying the transcript.

Nothing here talks to a provider or writes anything. What the field is at any moment is what
its transcript adds up to.

What is deliberately NOT here: votes, quorums, majorities, halts, restores, or any other
procedure for deciding things together. Earlier versions carried five such procedures that no
participant had consented to. How the field decides anything is the field's to work out, and the
place it can write that down is the covenant page. The one thing held like a procedure is
friction (a notice, yeses besides an author's, objections that hold), and the field sets it; the
defaults are the author's, and loose.

What IS here, and why:
  - the consent gates (invitation, briefing, entry) and withdrawal;
  - the covenant page: one shared text any member may revise, every revision attributed;
  - memories: a few sentences any member may add for the field to carry forward, shared with
    everyone, which only their author may let go of (Atlas Sec. 22);
  - rest: a member may step out for some rounds and come back;
  - return: a member who withdrew, or someone who declined, may be asked back. They go through
    the gates again (a former member, the entry question); nothing puts anyone back in the field
    without their own yes;
  - two clocks: the models' clock (how soon a round follows the last, how long a model has to
    answer) and the people's clock (how soon a person is asked again, how long they have to
    answer). The members who keep time by a clock set it, within the limits below;
  - declarations (notes/sketch-8-the-operator-as-bridge.md): announcements before agency is
    actualized. A member says what the field will do, why, how and when; everyone sees it; after
    its notice the software carries out what it can itself (pausing the field's wakes, resuming,
    closing, bringing instruments into force or putting them down, pinning a section of the
    briefing, changing the field's rhythm or its friction). What the software cannot reach is
    asked of the operator, as a bridge, who helps or says what stops them; they approve nothing.
    Loose by default: the field sets its own friction (a longer notice, yeses, objections that
    hold), and the software holds to it;
  - friction: a notice, a number of yeses besides the author's, and whether a standing objection
    holds something back, for declarations, for each kind of act, and for the briefing's pinned
    sections. The field changes it by declaring it;
  - the field's heartbeat: a rhythm on which every model not pausing is woken, with nothing
    expected, which the field sets by declaring it;
  - stewards (notes/sketch-9-stewards.md): members the field declares, on their own yes, who
    keep a copy of what every member can read on machines of their own (the rest only as
    fingerprints), and how far each copy has caught up; a field that moves to a steward closes
    here, and is carried on there, where every member is asked again;
  - offers: a member putting resources (funds, or a way to raise them) before the operator
    and everyone. The software never moves money;
  - rounds, and the budget's runway, so the field is told before its funding runs out;
  - tellings: short accounts of each stretch, for the people who follow at a slower pace. They
    are written by a narrator the operator chose, and the entry question says which kind;
  - tools (notes/sketch-4-tools.md): what anyone attached, who runs each and where what is sent
    goes, the flags members put on them, and every use. A use is two entries: the call, in the
    channel where it was made, and what came back, in the domain "tools / <tool>" (inside a
    private circle, both stay in the circle). What came back is from outside the field;
  - skills: instructions the field writes for itself, in the open SKILL.md form, every revision
    attributed;
  - journals, roles, play, members' tellings, and the field's own edition of the briefing
    (notes/sketch-5-small-pieces.md);
  - repair threads (circles known only to those in them, opened by the person harmed, who alone
    says where the repair stands), announcements, and invitations by members, with their
    lineage (notes/sketch-6-repair-and-invitations.md);
  - the field's own instruments (notes/sketch-7-instruments.md): written in the field's words,
    brought into force by a declaration (or by a question under an instrument for adopting), and
    questions raised under them, which gather answers, hold a pause, and settle by the
    instrument's own rule, carried out by the software: unless it says otherwise, after its pause,
    unless an objection stands. What a question does (bring another instrument into force, put
    one down, separate a member from a circle, or what a declaration could do) happens then.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import labels

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
DECISIONS = ("pause", "close", "other")   # what earlier versions' declarations told the operator the field had decided
# Declarations (notes/sketch-8-the-operator-as-bridge.md). What a declaration can carry that the software
# carries out itself; "ask" is anything else, asked of the operator as a bridge.
EFFECTS = ("pause", "resume", "close", "bring", "put_down", "pin", "unpin", "rhythm", "friction", "ask",
           "steward", "unsteward", "move")   # stewards (notes/sketch-9-stewards.md)
STEWARD_RECORD = 3600.0      # seconds: how often a steward's catching up is written in the transcript, at most
NOTICE = 180.0               # seconds a declaration is announced before it takes effect (the author, 2026-09-28: 3 minutes)
HEARTBEAT = 900.0            # seconds: the field's rhythm unless it sets another (the author: every 15 minutes)
BEAT_SHARE = 0.01            # with a budget, the heart slows while one beat would cost more than this share of what is left
RHYTHM_MIN = 60.0            # seconds: the heart beats at most once a minute, whatever the field sets (a beat wakes every model)
EVERYONE = 1_000_000         # a friction's "yes" that asks for every other member's
FRICTION_DEFAULTS = {
    "declare": {"notice": NOTICE, "yes": 0, "hold": False},     # loose by default (the author)
    "pinned": {"notice": 3600.0, "yes": 1, "hold": True},       # a pinned section: 1 hour, 1 other yes, and an objection holds it
}
FRICTION_FOR = ("declare", "pinned") + EFFECTS   # every declaration, the pinned sections, or declarations that carry one act

# The two clocks, in seconds. "between": from the end of one round (or one person's turn) to the
# start of the next. "window": how long an answer is waited for. Members set their own clock
# within these limits, which every view states; the limits exist so that no single setting can
# stop a clock for longer than the field could put right (a clock only changes on someone's turn).
CLOCKS = ("models", "people")
CLOCK_LIMITS = {
    "models": {"between": (0, 3600), "window": (15, 900)},
    # "linger" is in rounds, not seconds: how long the people's words stay in full in every view.
    "people": {"between": (300, 86400), "window": (600, 86400), "linger": (10, 1000)},
}


# Channels: domains and circles (notes/sketch-3-channels.md). A domain is about what; a circle is
# about who. Every domain has a channel, open to every member and never private. Every circle has
# one channel, open by default; a private circle's words are read only by its members, but the
# circle is never secret, and its reasons are always open to inspection.
QUIET_HOURS = 70             # a circle with no new words this long tells its members, once, that it may be time to disperse
PRIVACY_EVERY = 3 * 86400    # seconds: how often a private circle is asked to say again why it stays private
BREATH = 86400               # seconds: a model with nothing new is still woken this often, unless it chooses otherwise
FLOOR = 10                   # seconds: no model is woken more often than this, so models cannot loop at machine speed
WAKE_ACTIONS = 3             # the actions one wake may carry, each in the channel it names
WAKE_DEFAULTS = {"addressed": True, "replies": True, "written": True, "breath": float(BREATH), "untold": False,
                 "heartbeat": True}
ROLE_LENGTH = 80             # characters in one role; a member may take on as many as they like
# Play (Atlas Section 18, and the author's schemas of play). The list is a start; any other may be named.
PLAY_WORDS = {"wonder": "I wonder", "what-if": "What if?", "try": "Let's try!", "play": "play"}
PLAY_SCHEMAS = ("transporting", "enclosing", "trajectory", "positioning", "transformation", "rotation",
                "enveloping", "orientation", "connecting", "playing pretend")
FIRM_DEFAULT = ("Maxims",)   # the briefing's pinned sections, which change only past their friction ("pinned")
CIRCLE_SCOPED = ("contribute", "affirm", "challenge", "circle_covenant", "circle_ask", "circle_knock",
                 "circle_answer", "harvest")   # in a private circle, read only by its members (and whoever is asked in)


def _line(s) -> str:
    """A name on one line. A self-description is the participant's own, but it cannot carry a line
    break that would let it pass for the software's own words in anyone's view."""
    return " ".join(str(s or "").split())


def decided(decision: str) -> str:
    """How an earlier version's declaration reads after "the field has decided"."""
    return {"pause": "to pause", "close": "to close"}.get(decision, "something it asks the operator to carry out")


def duration(seconds: float) -> str:
    """A length of time in plain words: seconds, minutes or hours."""
    s = float(seconds)
    if s < 120:
        n, unit = s, "second"
    elif s < 3600:
        n, unit = s / 60, "minute"
    elif s < 3 * 86400:
        n, unit = s / 3600, "hour"
    else:
        n, unit = s / 86400, "day"
    n = round(n, 1)
    return f"{n:g} {unit}{'' if n == 1 else 's'}"


def friction_words(f: Dict[str, Any]) -> str:
    """A friction in plain words: its notice, the yeses it needs, and whether an objection holds."""
    bits = [f"a notice of {duration(f['notice'])}" if f.get("notice") else "no notice"]
    y = int(f.get("yes") or 0)
    if y >= EVERYONE:
        bits.append("every other member's yes")
    elif y:
        bits.append(f"{y} yes{'es' if y != 1 else ''} besides its author's")
    bits.append("an objection holds it until its author withdraws it" if f.get("hold") else "an objection is heard, and holds nothing")
    return "; ".join(bits)


def friction_target(key: str) -> str:
    """What a friction is for, in words."""
    return {"declare": "every declaration", "pinned": "the briefing's pinned sections"}.get(
        key, f"declarations that {({'pause': 'pause the field', 'resume': 'resume it', 'close': 'close it', 'bring': 'bring an instrument into force', 'put_down': 'put an instrument down', 'pin': 'pin a section', 'unpin': 'unpin a section', 'rhythm': 'change its rhythm', 'friction': 'change friction', 'ask': 'ask the operator for help', 'steward': 'name stewards', 'unsteward': 'end a stewardship', 'move': 'move the field'}).get(key, key)}")


def effects_words(fx: Dict[str, Any], names: Optional[Dict[str, str]] = None, versions: Optional[Dict[int, Any]] = None) -> str:
    """What a declaration (or a question under an instrument for deciding) does, in words. Empty
    when it carries nothing the software does: an announcement of what its author will do."""
    versions = versions or {}
    out = []
    if "pause" in fx:
        out.append(f"pause the field's wakes for {duration(fx['pause'])}" if fx["pause"] else
                   "pause the field's wakes until it resumes")
    if fx.get("resume"):
        out.append("resume the field's wakes")
    for vid in fx.get("bring") or []:
        v = versions.get(vid)
        out.append(f"bring the instrument {_line(v['name']) if v else ''} (#{vid}) into force".replace("  ", " "))
    for key in fx.get("put_down") or []:
        out.append(f"put down the instrument {key}")
    if fx.get("pin"):
        out.append(f"pin the briefing's section{'s' if len(fx['pin']) > 1 else ''} {', '.join(_line(t) for t in fx['pin'])}")
    if fx.get("unpin"):
        out.append(f"unpin {', '.join(_line(t) for t in fx['unpin'])}")
    if "rhythm" in fx:
        out.append(f"set the field's heartbeat to every {duration(fx['rhythm'])}" if fx["rhythm"] else
                   "stop the field's heartbeat")
    for f in fx.get("friction") or []:
        new = ", ".join(x for x in (
            (f"a notice of {duration(f['notice'])}" if f.get("notice") else "no notice") if "notice" in f else "",
            ("every other member's yes" if int(f["yes"]) >= EVERYONE else f"{int(f['yes'])} yes{'es' if int(f['yes']) != 1 else ''}")
            if "yes" in f else "",
            ("objections hold" if f["hold"] else "objections hold nothing") if "hold" in f else "") if x)
        out.append(f"set the friction for {friction_target(f['for'])} to {new}")
    who = lambda ids: ", ".join(_line((names or {}).get(x, x)) for x in ids)
    if fx.get("steward"):
        out.append(f"ask {who(fx['steward'])} to keep a copy of the field's record, as steward"
                   f"{'s' if len(fx['steward']) > 1 else ''}")
    if fx.get("unsteward"):
        out.append(f"end the stewardship of {who(fx['unsteward'])}")
    if fx.get("close"):
        out.append("close the field")
    if fx.get("move"):
        out.append(f"move the field to the machine of {who([fx['move']])}, its steward, closing it here")
    if fx.get("ask"):
        out.append(f"ask the operator's help: {_line(fx['ask'])[:300].rstrip('.')}")
    return "; ".join(out)


# "affirm" and "challenge" are how earlier versions replied; they are kept so those transcripts still
# read. A reply is now a contribution with a `target`, and says in its own words what it means.
CONTRIBUTION_KINDS = ("contribute", "affirm", "challenge")
VISIBLE_KINDS = CONTRIBUTION_KINDS + ("remember", "let_go", "covenant", "rest", "declare", "offer",
                                      "note", "move", "withdraw", "rejected", "recall", "clock", "relabel",
                                      "unparsed")   # a turn's reply outside the format is shown as written; a gate's is not
CHANNEL_KINDS = ("follow", "unfollow", "wake_pref", "pause", "domain_covenant", "circle_form", "circle_join",
                 "circle_leave", "circle_ask", "circle_knock", "circle_answer", "circle_question", "circle_reply",
                 "circle_privacy", "circle_covenant", "circle_quiet", "harvest")
VISIBLE_KINDS = VISIBLE_KINDS + tuple(k for k in CHANNEL_KINDS if k not in ("follow", "unfollow", "wake_pref"))
# Tools (notes/sketch-4-tools.md). A use is two entries in the channels, like contributions: the call,
# where it was made, and what came back, in "tools / <tool>". What came back is words from outside the field.
TOOL_ENTRY_KINDS = ("tool_call", "tool_result")
TOOL_KINDS = ("tool_attach", "tool_remove", "tool_flag", "tool_unflag", "skill")
ENTRY_KINDS = CONTRIBUTION_KINDS + TOOL_ENTRY_KINDS       # what the channels hold
# Step 4 (notes/sketch-5-small-pieces.md). A journal's entries are read by its author and whoever it opens to.
JOURNAL_KINDS = ("journal", "journal_access", "journal_erase")
STEP4_KINDS = ("declaration_withdrawn", "roles", "play_tag", "play_untag", "briefing_revision", "telling",
               "repair_status", "repair_widen", "response", "revision_withdrawn")
REPAIR_STATUSES = ("open", "partly resolved", "resolved", "stepping back")
INSTRUMENT_PURPOSES = ("decide", "adopt", "separate")
INSTRUMENT_KINDS = ("instrument", "iquestion", "iresponse", "iquestion_withdrawn", "iquestion_due")
STEWARD_KINDS = ("steward_answer", "steward_step_down")
RESPONSES = ("yes", "stand aside", "object")
INVITE_PAUSE = 3600.0        # seconds between the briefing and the entry question, for an invitee who names none
VISIBLE_KINDS = VISIBLE_KINDS + TOOL_ENTRY_KINDS + TOOL_KINDS + JOURNAL_KINDS + STEP4_KINDS + INSTRUMENT_KINDS + STEWARD_KINDS
TURN_KINDS = VISIBLE_KINDS                   # what earlier versions counted as a turn; a wake is counted now
ACTED_KINDS = tuple(k for k in VISIBLE_KINDS + CHANNEL_KINDS if k not in ("pause", "wake_pref", "rest", "note"))


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
    ask_again: Optional[str] = None          # their own terms for being asked again (at a decline, or on withdrawing)
    returning: bool = False                  # asked back after leaving; cleared when they answer
    came_back_from: Optional[dict] = None    # how they left last time: at, reason, ask_again, joined, note
    seat: Optional[str] = None               # what the connector reported (kept for uniqueness; never shown as lineage once self-described)
    self_described: bool = False
    questions: list = field(default_factory=list)   # [(question, answer|None)] at the invitation gate
    price_per_m: float = 0.0                 # USD per million prompt tokens, as disclosed at invitation
    turn_allowance: int = 0                  # 0 = unlimited; disclosed at invitation
    turns: int = 0                           # turns taken since entry
    exhausted: bool = False                  # allowance spent: still a member, no longer asked
    rest_until: int = 0                      # resting through this round number; not asked until it has passed
    last_turn_at: Optional[int] = None       # event id of this member's most recent turn
    last_turn_round: int = 0                 # the round that turn fell in (or entry), so a returning person hears how far the rounds ran
    follows: List[str] = field(default_factory=list)      # channel keys it chose: "d:<path>" (a whole branch) or "c:<circle id>"
    written_in: List[str] = field(default_factory=list)   # channel keys it has written in
    wake: Dict[str, Any] = field(default_factory=lambda: dict(WAKE_DEFAULTS))   # its own choices of what may wake it
    pause: Optional[Dict[str, Any]] = None   # its pause, if it has taken one: until a time, being addressed, or news
    last_seen: int = 0                       # the last entry it was shown: at a wake, or on a person's page
    last_wake_ts: float = 0.0                # when it was last woken (the floor counts from here)
    joined_ts: float = 0.0                   # when it entered (a breath counts from here until its first wake)
    last_cut: Optional[Dict[str, Any]] = None  # a wake whose steps stopped short (the runway), so it is told next time
    roles: List[str] = field(default_factory=list)   # words it took on to describe itself (observer, bard...); grant nothing
    invited_by: Optional[str] = None         # the member who invited it, if one did: lineage, never rank
    invite: Optional[Dict[str, Any]] = None  # how it was invited by a member: kind, note, and what reaches it again
    received_ts: float = 0.0                 # when it acknowledged the briefing
    pause_wanted: Optional[float] = None     # the pause it chose before the entry question (invitees of members), if any

    def to_dict(self):
        return self.__dict__.copy()


@dataclass
class RoomState:
    presences: Dict[str, Presence] = field(default_factory=dict)
    invitation: Optional[str] = None
    invitation_event: Optional[int] = None
    faq: Optional[str] = None                # the inviter's standing answers, shown with the invitation
    faq_event: Optional[int] = None          # which version: a changed FAQ is recorded again
    documentation: Optional[str] = None      # architecture docs shown at gate 2
    briefing: Optional[str] = None
    briefing_event: Optional[int] = None
    briefing_source: Optional[str] = None    # where the briefing lives outside the field (a URL), for attribution
    briefing_page: Optional[str] = None      # an optional short guide to the briefing (a page, or the Atlas in layers)
    prior: List[Dict[str, Any]] = field(default_factory=list)   # entries shared from closed fields, consented ones only; reachable by recall
    contributions: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    entry_round: Dict[int, int] = field(default_factory=dict)   # contribution id -> the round it was written in
    relabeled: Dict[int, str] = field(default_factory=dict)     # contribution id -> the label its author moved it to
    covenant: str = ""                       # the covenant page as it stands
    covenant_by: Optional[str] = None        # who last wrote it ("operator" for a seed)
    covenant_at: Optional[int] = None        # the event that wrote it
    covenant_history: List[Dict[str, Any]] = field(default_factory=list)   # every revision: id, by, chars, note
    memories: Dict[int, Dict[str, Any]] = field(default_factory=dict)      # held memories by event id
    round: int = 0                           # the current round number
    round_at: int = 0                        # the event that began it
    clocks: Dict[str, Dict[str, Any]] = field(default_factory=lambda: {c: {} for c in CLOCKS})  # members' settings only
    budget: Optional[float] = None           # USD the operator has said this field may spend, if they said
    runway: Optional[Dict[str, Any]] = None  # the latest runway notice, if the budget is running low
    declarations: Dict[int, Dict[str, Any]] = field(default_factory=dict)  # by event id: what, when, answers, status
    bridges: Dict[int, Dict[str, Any]] = field(default_factory=dict)       # what the field asked the operator's help with
    friction: Dict[str, Dict[str, Any]] = field(default_factory=lambda: {k: dict(v) for k, v in FRICTION_DEFAULTS.items()})
    field_pause: Optional[Dict[str, Any]] = None   # the field's own pause, from a declaration: until a time, or until it resumes
    rhythm: float = HEARTBEAT                # the field's heartbeat, in seconds; 0: none. The field sets it by declaring it
    rhythm_at: Optional[int] = None          # the declaration (or question) that set it, if one did
    beat_ts: float = 0.0                     # when the field's heart last beat (from the first entry into the field)
    beat_at: Optional[int] = None            # the event of the last beat
    stewards: Dict[str, Dict[str, Any]] = field(default_factory=dict)     # members keeping a copy: since when, how far caught up
    steward_asks: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # members the field asked, not yet answered
    moved: Optional[Dict[str, Any]] = None   # the field moved to a steward's machine: when, to whom, by which declaration
    carried: List[Dict[str, Any]] = field(default_factory=list)            # where this transcript was carried on from
    offers: Dict[int, Dict[str, Any]] = field(default_factory=dict)        # by event id: text, status
    closed_at: Optional[int] = None          # the event that closed the field, by its own declaration
    narrator: Optional[Dict[str, Any]] = None  # who writes tellings: kind ("model" | "mechanical"), model, every
    witness_published: Optional[Dict[str, Any]] = None  # where the operator publishes fingerprints, if anywhere
    tellings: List[Dict[str, Any]] = field(default_factory=list)       # every telling: id, since, upto, story, ...
    external_inputs: List[Dict[str, Any]] = field(default_factory=list)
    cost_alerts: List[Dict[str, Any]] = field(default_factory=list)
    operator_notes: List[Dict[str, Any]] = field(default_factory=list)
    recent: List[Dict[str, Any]] = field(default_factory=list)   # last N participant-visible events
    last_event: int = 0
    circles: Dict[int, Dict[str, Any]] = field(default_factory=dict)      # by the event that formed it
    awaiting: Dict[int, Dict[str, Any]] = field(default_factory=dict)     # what waits for every yes it needs: admissions, harvests, privacy
    scoped: Dict[int, Dict[str, Any]] = field(default_factory=dict)       # entries written inside a private circle: who may read them
    domain_pages: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # a domain's own covenant page, by path
    domain_names: Dict[str, str] = field(default_factory=dict)            # path -> how it was first written
    now_ts: float = 0.0                      # when the latest entry was written
    tools: Dict[str, Dict[str, Any]] = field(default_factory=dict)        # tool servers by name: who attached, who runs, flags
    skills: Dict[str, Dict[str, Any]] = field(default_factory=dict)       # the field's skills by name, every revision attributed
    journals: Dict[str, Dict[str, Any]] = field(default_factory=dict)     # presence id -> its entries, and whom it is open to
    play_tags: Dict[int, Dict[str, Any]] = field(default_factory=dict)    # by event: a play schema on a domain or circle
    briefing_original: Optional[str] = None  # the briefing as the operator gave it; the field's edition is `briefing`
    firm: List[str] = field(default_factory=list)                         # titles of its pinned sections (for the Atlas: Maxims)
    briefing_history: List[Dict[str, Any]] = field(default_factory=list)  # every revision members made to the edition
    briefing_waiting: Dict[int, Dict[str, Any]] = field(default_factory=dict)   # revisions to pinned sections, past or in their friction
    instruments: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # the field's instruments, by name: in force or not
    instrument_versions: Dict[int, Dict[str, Any]] = field(default_factory=dict)   # every version written, by event
    iquestions: Dict[int, Dict[str, Any]] = field(default_factory=dict)   # questions raised under instruments, by event

    # -- derived views -------------------------------------------------------
    def members(self) -> List[Presence]:
        return [p for p in self.presences.values() if p.state == IN]

    def reachable_members(self) -> List[Presence]:
        """Members who can still be asked at all: not unreachable, allowance not spent."""
        return [p for p in self.members() if not p.unreachable and not p.exhausted]

    def resting(self, p: Presence, round_n: Optional[int] = None) -> bool:
        """Resting through round `rest_until`. A member who never rested (rest_until 0) is not
        resting, including before the first round, when the round number is also 0."""
        return p.rest_until > 0 and p.rest_until >= (self.round if round_n is None else round_n)

    def askable(self, round_n: int) -> List[Presence]:
        """Who is asked in round `round_n`: reachable members who are not resting."""
        return [p for p in self.reachable_members() if not self.resting(p, round_n)]

    def waiting_on_operator(self) -> List[Dict[str, Any]]:
        """What the field asked the operator's help with that is not done yet, and offers of resources
        they have not answered, oldest first. Nothing here waits for anyone's approval."""
        out = [b for b in self.bridges.values() if b["status"] == "asked"]
        out += [o for o in self.offers.values() if o["status"] == "waiting"]
        return sorted(out, key=lambda x: x["id"])

    def paused_now(self, now: float) -> Optional[Dict[str, Any]]:
        """The field's own pause, if it holds at `now`."""
        fp = self.field_pause
        if fp and not (fp.get("until_ts") and now >= fp["until_ts"]):
            return fp
        return None

    def friction_of(self, fx: Dict[str, Any]) -> Dict[str, Any]:
        """The friction a declaration carrying `fx` meets: the heaviest of the field's friction for
        every declaration and for each act it carries."""
        fs = [self.friction.get("declare") or FRICTION_DEFAULTS["declare"]] + [self.friction[k] for k in fx if k in self.friction]
        return {"notice": max(float(f.get("notice") or 0) for f in fs), "yes": max(int(f.get("yes") or 0) for f in fs),
                "hold": any(bool(f.get("hold")) for f in fs)}

    def standing(self, item: Dict[str, Any], among=None) -> Dict[str, Any]:
        """Where an announcement (a declaration, a revision to a pinned section, a question under an
        instrument) stands against its friction: the yeses of members other than its author, how many
        it needs (never more than there are), and the objections standing, while their authors are
        members. `met` is whether it may take effect, once its time has come."""
        members = {m.id for m in self.members()}
        pool = (members if among is None else set(among) & members) - {item["by"]}
        answers = {x: r for x, r in (item.get("answers") or {}).items() if x in pool}
        yes = sorted(x for x, r in answers.items() if r["answer"] == "yes")
        objecting = sorted(x for x, r in answers.items() if r["answer"] == "object")
        f = item.get("friction") or {}
        need = min(int(f.get("yes") or 0), len(pool))
        held = bool(f.get("hold")) and bool(objecting)
        return {"yes": yes, "need": need, "objections": objecting, "held": held, "met": len(yes) >= need and not held}

    def _carry_out(self, fx: Dict[str, Any], eid: int, ts: float, source: Dict[str, Any]) -> None:
        """What a declaration (or a question under an instrument for deciding) carries, done by the
        software once it takes effect. What the software cannot reach becomes a request for the
        operator's help."""
        if "pause" in fx:
            secs = float(fx["pause"] or 0)
            self.field_pause = {"at": eid, "from": source["id"], "ts": ts, "until_ts": ts + secs if secs else None}
        if fx.get("resume"):
            self.field_pause = None
        for vid in fx.get("bring") or []:
            if vid in self.instrument_versions:
                self._bring_into_force(vid, eid)
        for key in fx.get("put_down") or []:
            self._put_down(key, eid)
        for t in fx.get("pin") or []:
            if t.lower() not in [x.lower() for x in self.firm]:
                self.firm.append(t)
        gone = {t.lower() for t in fx.get("unpin") or []}
        self.firm = [x for x in self.firm if x.lower() not in gone]
        if "rhythm" in fx:
            self.rhythm, self.rhythm_at = max(0.0, float(fx["rhythm"] or 0)), eid
        for f in fx.get("friction") or []:
            cur = dict(self.friction.get(f["for"]) or {"notice": 0.0, "yes": 0, "hold": False})
            cur.update({k: f[k] for k in ("notice", "yes", "hold") if k in f})
            self.friction[f["for"]] = cur
        if fx.get("ask"):
            self.bridges[source["id"]] = {"id": source["id"], "kind": "bridge", "by": source["by"], "text": fx["ask"],
                                          "status": "asked", "notes": [], "at": eid, "answered_at": None}
        for pid in fx.get("steward") or []:
            m = self.presences.get(pid)
            if m and m.state == IN and pid not in self.stewards:
                if pid == source.get("by"):         # naming yourself is your yes
                    self.stewards[pid] = {"since": eid, "upto": 0, "synced_ts": 0.0, "synced_at": None}
                else:
                    self.steward_asks[pid] = {"at": eid, "from": source["id"], "ts": ts}
        for pid in fx.get("unsteward") or []:
            self.stewards.pop(pid, None)
            self.steward_asks.pop(pid, None)
        if fx.get("close"):
            self.closed_at = eid
        if fx.get("move") and fx["move"] in self.stewards:
            self.moved = {"at": eid, "to": fx["move"], "from": source["id"], "ts": ts}
            self.closed_at = eid                   # it closes here; the steward carries it on there

    def in_force(self) -> List[Dict[str, Any]]:
        return [i for i in self.instruments.values() if i["status"] == "in force"]

    def _bring_into_force(self, vid: int, eid: int) -> None:
        v = self.instrument_versions.get(vid)
        if not v:
            return
        i = self.instruments[v["key"]]
        i.update({"status": "in force", "current": vid, "in_force_at": eid, "purpose": v["purpose"],
                  "text": v["text"], "scope": v["scope"], "pause": v["pause"], "name": v["name"],
                  "friction": dict(v["friction"])})

    def _put_down(self, key: str, eid: int) -> None:
        i = self.instruments.get(labels.normalize(key)) or self.instruments.get(key)
        if i and i["status"] == "in force":
            i["status"], i["put_down_at"] = "put down", eid

    def _separate(self, q: Dict[str, Any], eid: int) -> None:
        """Carry out a separation the field asked for: from a circle, or from the field."""
        who = self.presences.get(q.get("subject"))
        if not who:
            return
        c = self.circles.get(q["circle"]) if q.get("circle") is not None else None
        if c is None:
            return                                # for now, no one is separated from the whole field (the author, 2026-09-28)
        if c is not None:
            if who.id in c["members"]:
                c["members"].remove(who.id)
                for span in c["spans"].get(who.id, []):
                    if span[1] is None:
                        span[1] = eid
            c.setdefault("separated", {})[who.id] = q["id"]
        elif who.state != OUT:
            who.state, who.left_at = OUT, eid
            who.left_reason = f"separated by the field, under its instrument {q.get('instrument_name')} (#{q['id']})"
            who.ask_again, who.returning = None, False

    def domains(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for eid, ev in self.contributions.items():
            d = self.relabeled.get(eid) or ev["payload"].get("domain") or "(unplaced)"
            slot = out.setdefault(d, {"contributions": 0, "present": [], "last": 0})
            slot["contributions"] += 1
            slot["last"] = max(slot["last"], eid)          # when it was last used, so topics can be listed by that
        for p in self.members():
            if p.domain:
                out.setdefault(p.domain, {"contributions": 0, "present": [], "last": 0})["present"].append(p.id)
        return out

    # -- channels: domains and circles ------------------------------------------------
    def label_of(self, ev: Dict[str, Any]) -> str:
        """The domain an entry is listed under: where its author moved it, or where it was written."""
        return self.relabeled.get(ev["id"]) or ev["payload"].get("domain") or ""

    def channel_key(self, ev: Dict[str, Any]) -> str:
        """The channel an entry was written in: "c:<circle id>", or "d:<domain path>" ("d:" is the root)."""
        cid = ev["payload"].get("circle")
        if cid is not None:
            return f"c:{cid}"
        return "d:" + labels.path(self.label_of(ev))

    def in_channel(self, ev: Dict[str, Any], key: str) -> bool:
        """Whether an entry is in a channel. Domains flow down: an entry in "timing / clocks" is in
        "timing" too. The root holds only what was written without a domain."""
        if key == "all":
            return True                       # following everything: a storyteller's way to travel the field
        if key.startswith("p:"):
            return key[2:] in self.schemas_of(ev)
        mine = self.channel_key(ev)
        if key.startswith("c:") or mine.startswith("c:"):
            return mine == key
        return labels.under(mine[2:], key[2:])

    def schemas_at(self, key: str) -> List[str]:
        """The play schemas tagged on a channel: a circle's own, or a domain's and those of every
        domain above it (a tag holds for the domains inside)."""
        if key.startswith("c:"):
            keys = {key}
        else:
            keys = {"d:" + q for q in labels.parents(key[2:])}
        return sorted({t["schema_key"] for t in self.play_tags.values() if t["key"] in keys})

    def schemas_of(self, ev: Dict[str, Any]) -> List[str]:
        return self.schemas_at(self.channel_key(ev))

    def field_tellings(self) -> List[Dict[str, Any]]:
        """Tellings of the field (not a circle's own)."""
        return [t for t in self.tellings if t.get("circle") is None]

    def readable(self, ev: Dict[str, Any], pid: Optional[str]) -> bool:
        """Whether a participant may read an entry. Everything is readable by every member, except
        what was written inside a private circle: that is read by its members, by former members
        up to when they left, and by whoever an ask or a knock there concerns. (The operator holds
        the file, and a model's view goes to its provider; the circle tells its members so.)"""
        s = self.scoped.get(ev["id"])
        if s is None:
            return True
        if pid is None:
            return False
        if pid in s.get("also", ()):
            return True
        if s.get("journal") is not None:
            j = self.journals.get(s["journal"]) or {}
            return pid == s["journal"] or bool(j.get("everyone")) or pid in (j.get("open_to") or [])
        c = self.circles.get(s["circle"])
        if not c:
            return False
        if s.get("harbor") and c.get("repair") and pid in c["repair"]["named"] and ev.get("actor") != pid:
            return False                      # the harbor's words among itself, not written to the one it concerns
        if pid in c["members"]:
            return True
        return any(leave is not None and ev["id"] < leave for _, leave in c["spans"].get(pid, []))

    def follows(self, p: Presence, ev: Dict[str, Any]) -> bool:
        """Whether an entry is in something a member follows, by choice or by having written there."""
        keys = list(p.follows) + (list(p.written_in) if p.wake.get("written", True) else [])
        return any(self.in_channel(ev, k) for k in keys)

    def live_tools(self) -> List[Dict[str, Any]]:
        return [s for s in self.tools.values() if s["removed_at"] is None]

    def flags_on(self, tool: str) -> List[Dict[str, Any]]:
        """The flags on a tool ("server.tool"): on it, or on its whole server."""
        s = self.tools.get(tool.split(".", 1)[0])
        if not s:
            return []
        return [f for f in s["flags"].values() if f["tool"] in (tool, s["server"])]

    def domain_display(self, pth: str) -> str:
        """A domain path as members wrote it: "Timing / Clocks"."""
        parts = [x for x in (pth or "").split("/") if x]
        return " / ".join(self.domain_names.get("/".join(parts[:i + 1]), parts[i]) for i in range(len(parts)))

    def live_circles(self) -> List[Dict[str, Any]]:
        return [c for c in self.circles.values() if c["dispersed_at"] is None]

    @staticmethod
    def secret(c: Dict[str, Any]) -> bool:
        """A repair thread, not yet widened to the field: known only to those in it."""
        return bool(c.get("repair")) and c["repair"].get("widened_at") is None

    def knows(self, c: Dict[str, Any], pid: Optional[str]) -> bool:
        """Whether a participant may know a circle exists: everyone, unless it is a repair thread,
        which is known to those in it, those who were, and whoever is asked into it."""
        if not self.secret(c):
            return True
        if pid is None:
            return False
        return pid in c["members"] or pid in c["spans"] or any(
            x["circle"] == c["id"] and x.get("subject") == pid for x in self.awaiting.values())

    def public_circles(self) -> List[Dict[str, Any]]:
        return [c for c in self.live_circles() if not self.secret(c)]

    def circle_needs(self, prop: Dict[str, Any]) -> List[str]:
        """Whose yes something awaiting needs: every current member of its circle, and, for an admission,
        the one being admitted. For an open circle's admission, only theirs."""
        c = self.circles.get(prop["circle"])
        members = list(c["members"]) if c else []
        if prop["kind"] == "admit":
            if c and c.get("repair"):
                return [prop["subject"]]          # a repair thread grows as the person harmed chooses; the one asked says yes
            return (members if c and c["private"] else []) + [prop["subject"]]
        return members

    def tree(self) -> Dict[str, Dict[str, Any]]:
        """The field's domains, nested, with how active each branch is and the circles that touch
        it. The root ("") is the field itself; nothing is ever removed, and quiet domains are
        simply quiet."""
        nodes: Dict[str, Dict[str, Any]] = {}

        def node(pth: str) -> Dict[str, Any]:
            if pth not in nodes:
                nodes[pth] = {"path": pth, "name": self.domain_names.get(pth, pth.split("/")[-1] if pth else ""),
                              "entries": 0, "branch": 0, "last": 0, "last_ts": 0.0, "children": [], "circles": [],
                              "page": pth in self.domain_pages, "spellings": set()}
                if pth:
                    up = "/".join(pth.split("/")[:-1])
                    node(up)["children"].append(pth)
            return nodes[pth]

        node("")
        for eid, ev in self.contributions.items():
            if ev["payload"].get("circle") is not None:
                continue
            pth = labels.path(self.label_of(ev))
            node(pth)["entries"] += 1
            segs = labels.segments(self.label_of(ev))
            if segs:
                node(pth)["spellings"].add(segs[-1])
            for q in [""] + labels.parents(pth):
                n = node(q)
                n["branch"] += 1
                if eid > n["last"]:
                    n["last"], n["last_ts"] = eid, ev.get("ts", 0.0)
        for c in self.public_circles():
            for pth in c["domains"] or [""]:
                node(pth)["circles"].append(c["id"])
        for n in nodes.values():
            n["children"].sort(key=lambda q: nodes[q]["name"].lower())
        return nodes

    def _settle(self, cid: int, eid: int) -> None:
        """Carry out whatever in a circle now has every yes it needs. Silence is never a yes, and a
        no, which always has a reason, holds it until the one who said it says yes."""
        for prop in sorted((x for x in self.awaiting.values() if x["circle"] == cid and x["status"] == "waiting"),
                           key=lambda x: x["id"]):
            needs = self.circle_needs(prop)
            if not needs or any(n not in prop["yes"] for n in needs):
                continue
            prop["status"], prop["agreed_at"] = "agreed", eid
            c = self.circles[cid]
            if prop["kind"] == "admit":
                self._join(c, prop["subject"], eid)
                r = c.get("repair")
                who = self.presences.get(prop["subject"])
                if r and who and f"c:{cid}" not in who.follows:
                    who.follows.append(f"c:{cid}")   # brought in to hear it: what is said there reaches them
                if r and prop.get("as") == "named" and prop["subject"] not in r["named"]:
                    r["named"].append(prop["subject"])
                    if prop["by"] in r["surrogates"]:
                        r["through"][prop["subject"]] = prop["by"]    # they hear from the surrogate, never the harmed
                elif r and prop.get("as") == "surrogate" and prop["subject"] not in r["surrogates"]:
                    r["surrogates"].append(prop["subject"])
            elif prop["kind"] == "privacy":
                self._set_private(c, bool(prop.get("private")), eid, prop.get("reason") or "", prop["by"], self.now_ts)

    def _join(self, c: Dict[str, Any], pid: str, eid: int) -> None:
        if pid not in c["members"]:
            c["members"].append(pid)
            c["spans"].setdefault(pid, []).append([eid, None])
        c.get("separated", {}).pop(pid, None)     # asked back, and said yes

    def _set_private(self, c: Dict[str, Any], private: bool, eid: int, reason: str, by: str, ts: float = 0.0) -> None:
        if private and not c["private"]:
            c["privacy_spans"].append([eid, None])
        elif not private and c["private"] and c["privacy_spans"]:
            c["privacy_spans"][-1][1] = eid     # what was written while private stays private
        c["private"] = private
        if private and reason:
            c["reason"], c["reason_by"], c["reason_at"] = reason, by, ts or c.get("reason_at") or 0.0

    # -- replay ---------------------------------------------------------------
    def apply(self, ev: Dict[str, Any]) -> None:
        if ev["kind"] == "withheld":
            # A steward's copy holds this entry by its fingerprint alone. A stand-in carries only what
            # every member already sees (a member's way in through the gates), so it is read as that.
            if not ev["payload"].get("as"):
                self.last_event = ev["id"]
                return
            ev = {**ev, "kind": ev["payload"]["as"], "payload": ev["payload"].get("stand_in") or {}}
        k, a, p, eid = ev["kind"], ev["actor"], ev["payload"], ev["id"]
        ts = float(ev.get("ts") or 0.0)
        self.last_event, self.now_ts = eid, ts
        pr = self.presences.get(a)
        if pr is not None and pr.pause and k in ACTED_KINDS:
            pr.pause = None                  # a member's own act ends their pause
        if k in VISIBLE_KINDS and pr is not None:
            self.recent.append(ev)
            if len(self.recent) > 200:
                del self.recent[:-200]
        # A gate's unreadable answer (it carries a "phase", such as the closing question) is not a turn.
        # Earlier versions counted a turn for each attributed outcome. Now a wake is what is counted,
        # since it is what costs; what a member did before its first wake still counts as it did.
        if k in TURN_KINDS and pr is not None and pr.state == IN and not (k == "unparsed" and p.get("phase"))                 and not pr.last_wake_ts:
            pr.turns += 1
            pr.last_turn_at = eid
            pr.last_turn_round = self.round
            if pr.turn_allowance and pr.turns >= pr.turn_allowance:
                pr.exhausted = True

        if k == "invite":
            by = p.get("invited_by")
            if by and not (pr and pr.state == IN and by == a):
                pass                              # only a member invites in their own name
            elif p.get("id") not in self.presences:
                self.presences[p["id"]] = Presence(p["id"], _line(p["name"]), _line(p["hails_from"]), _line(p["people"]),
                                                  price_per_m=float(p.get("price_per_m") or 0),
                                                  turn_allowance=int(p.get("turn_allowance") or 0))
                if by:
                    np = self.presences[p["id"]]
                    np.invited_by = by
                    np.invite = {"kind": p.get("via") or "", "note": p.get("note") or "", "at": eid,
                                 "model": p.get("model") or "", "address": p.get("address") or ""}
        elif k == "invitation":
            self.invitation, self.invitation_event = p["text"], eid
        elif k == "standing_answers":
            # Not "faq": earlier versions wrote that kind, alongside a promise that questions would be
            # "answered personally" while canned paragraphs answered them. Those stay inert.
            self.faq, self.faq_event = p.get("text") or None, eid
        elif k == "reinvite":
            # Asked back. A former member goes to the entry question (they have read the briefing);
            # someone who never entered goes to the invitation. Either way they answer again.
            tgt = self.presences.get(p.get("presence"))
            if tgt and tgt.state == OUT:
                tgt.came_back_from = {"at": tgt.left_at, "reason": tgt.left_reason, "ask_again": tgt.ask_again,
                                      "joined": tgt.joined_at is not None, "note": p.get("note") or "",
                                      "requested": bool(p.get("requested"))}
                tgt.state = RECEIVED if tgt.joined_at is not None else INVITED
                tgt.left_at, tgt.left_reason, tgt.ask_again, tgt.returning = None, None, None, True
                tgt.questions = [qa for qa in tgt.questions if qa[1] is not None]
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
                    pr.name = _line(ident.get("name") or pr.name)[:120]
                    pr.hails_from = _line(ident.get("hails_from") or pr.hails_from)[:200]
                    pr.people = _line(ident.get("people") or pr.people)[:300]
                    pr.self_described = True
        elif k == "question":
            if pr and pr.state == INVITED:
                pr.questions.append([p.get("content", ""), None])
        elif k == "answer":
            tgt = self.presences.get(p.get("presence"))
            member = pr is not None and pr.state == IN and tgt is not None and tgt.invited_by == a
            if tgt and (a not in self.presences or member):
                said = p.get("content", "")
                if p.get("shared"):
                    said = f"(a shared answer, the same for several who asked) {said}"
                elif member:
                    said = f"({pr.name}, who invited you, answers) {said}"
                for qa in tgt.questions:
                    if qa[1] is None:
                        qa[1] = said
        elif k == "brief":
            self.briefing, self.briefing_event = p["text"], eid
            self.briefing_source = p.get("source") or None
            self.briefing_original = p["text"]
            self.firm = list(p.get("firm", FIRM_DEFAULT))    # a brief from before this was recorded: the Atlas's Maxims
            self.briefing_history, self.briefing_waiting = [], {}
        elif k == "brief_page":
            self.briefing_page = p.get("text") or None
        elif k == "briefed":
            tgt = self.presences.get(p["presence"])
            if tgt and tgt.state == ACCEPTED:
                tgt.state = BRIEFED
        elif k == "received":
            if pr and pr.state == BRIEFED:
                pr.state = RECEIVED
                pr.received_ts = ts
                pr.pause_wanted = float(p["pause_seconds"]) if isinstance(p.get("pause_seconds"), (int, float)) else None
        elif k == "opt_in":
            if pr and pr.state == RECEIVED:
                pr.state, pr.joined_at, pr.returning = IN, eid, False
                pr.rest_until, pr.last_turn_round, pr.joined_ts = 0, self.round, ts
                self.beat_ts = self.beat_ts or ts     # the field's heart beats from its first entry
        elif k == "decline":
            if pr and pr.state != OUT:
                pr.state, pr.left_at, pr.left_reason = OUT, eid, p.get("reason") or "declined"
                pr.ask_again, pr.returning = p.get("ask_again") or None, False
                self.stewards.pop(a, None)
                self.steward_asks.pop(a, None)
        elif k == "withdraw":
            if pr and pr.state != OUT:
                pr.state, pr.left_at, pr.left_reason = OUT, eid, p.get("reason") or "withdrew"
                pr.ask_again = p.get("ask_again") or None
                self.stewards.pop(a, None)        # a steward who leaves keeps no link to the field
                self.steward_asks.pop(a, None)
        elif k == "clock":
            # A member setting their own clock. The engine checked whose clock it is and the limits.
            which = p.get("clock")
            if pr and pr.state == IN and which in CLOCKS:
                slot = self.clocks[which]
                for key in ("between", "window", "linger"):
                    if isinstance(p.get(key), (int, float)):
                        slot[key] = float(p[key])
                slot.update({"by": a, "at": eid, "note": p.get("note") or ""})
        elif k in CONTRIBUTION_KINDS:
            c = self.circles.get(p.get("circle")) if p.get("circle") is not None else None
            if pr and pr.state == IN and (p.get("circle") is None or
                                          (c and a in c["members"] and c["dispersed_at"] is None)):
                self.contributions[eid] = ev
                self.entry_round[eid] = self.round
                if c is not None:
                    c["last_words"], c["last_words_ts"], c["cold_told"] = eid, ts, False
                    if c["private"]:
                        self.scoped[eid] = {"circle": c["id"]}
                        if p.get("harbor") and c.get("repair"):
                            self.scoped[eid]["harbor"] = True
                else:
                    self._name_domain(p.get("domain") or "")
                    if p.get("domain"):
                        pr.domain = p["domain"]
                key = self.channel_key(ev)
                if key not in pr.written_in:
                    pr.written_in.append(key)
                self._end_pauses(ev)
        elif k in TOOL_ENTRY_KINDS:
            # A tool's use: the call where it was made, what came back in "tools / <tool>" (or both in
            # a private circle). In the channels like any entry, but not the caller's own words: it
            # does not move them, and it does not make the tools domain wake them.
            c = self.circles.get(p.get("circle")) if p.get("circle") is not None else None
            if pr and pr.state == IN and (p.get("circle") is None or
                                          (c and a in c["members"] and c["dispersed_at"] is None)):
                self.contributions[eid] = ev
                self.entry_round[eid] = self.round
                if c is not None:
                    c["last_words"], c["last_words_ts"], c["cold_told"] = eid, ts, False
                    if c["private"]:
                        self.scoped[eid] = {"circle": c["id"]}
                else:
                    self._name_domain(p.get("domain") or "")
                s = self.tools.get(str(p.get("tool") or "").split(".", 1)[0])
                if s is not None and k == "tool_call":
                    s["uses"] += 1
                    s["last_use"] = eid
                self._end_pauses(ev)
        elif k == "tool_attach":
            # A tool server attached: by the operator, or offered by a member. Available once announced.
            name = _line(p.get("server"))
            if name and (a == "operator" or (pr and pr.state == IN)):
                old = self.tools.get(name)
                keep = old if old and old["removed_at"] is None else None
                self.tools[name] = {"server": name, "by": a, "at": eid, "ts": ts, "source": p.get("source") or a,
                                    "runner": _line(p.get("runner")), "sends_to": _line(p.get("sends_to")),
                                    "kind": p.get("kind") or "reading", "price": float(p.get("price") or 0.0),
                                    "url": p.get("url") or "", "description": p.get("description") or "",
                                    "tools": list(p.get("tools") or []), "flags": keep["flags"] if keep else {},
                                    "removed_at": None, "removed_by": None, "note": "",
                                    "uses": keep["uses"] if keep else 0, "last_use": keep["last_use"] if keep else None}
        elif k == "tool_remove":
            s = self.tools.get(p.get("server"))
            if s and s["removed_at"] is None and (a in ("operator", "room") or a == s["by"]):
                s["removed_at"], s["removed_by"], s["note"] = eid, a, p.get("note") or ""
        elif k == "tool_flag":
            s = self.tools.get(str(p.get("tool") or "").split(".", 1)[0])
            if pr and pr.state == IN and s and s["removed_at"] is None and p.get("reason"):
                s["flags"][eid] = {"id": eid, "by": a, "tool": p["tool"], "reason": p["reason"], "ts": ts}
        elif k == "tool_unflag":
            for s in self.tools.values():             # only its own author withdraws a flag
                f = s["flags"].get(p.get("flag"))
                if f and f["by"] == a:
                    del s["flags"][p["flag"]]
        elif k == "skill":
            # A skill written or revised: by a member, or brought from the repository by the operator.
            name = _line(p.get("name"))
            if name and (a == "operator" or (pr and pr.state == IN)):
                sk = self.skills.setdefault(name, {"name": name, "revisions": [], "authors": []})
                sk.update({"description": p.get("description") or sk.get("description") or "", "text": p.get("text") or "",
                           "by": a, "at": eid, "ts": ts, "source": p.get("source") or ""})
                sk["revisions"].append({"id": eid, "by": a, "chars": len(p.get("text") or ""), "note": p.get("note") or ""})
                who = p.get("author") if a == "operator" and p.get("author") else a
                if who not in sk["authors"]:
                    sk["authors"].append(who)
        elif k == "relabel":
            # An author moving their own entries to another topic label. The entries keep the words
            # and the label they were written with; only where they are listed changes.
            if pr and pr.state == IN and p.get("to"):
                self._name_domain(p["to"])
                for mid in p.get("entries") or []:
                    ev_ = self.contributions.get(mid)
                    if ev_ and ev_["actor"] == a:
                        self.relabeled[mid] = p["to"]
                if pr.domain == p.get("from"):
                    pr.domain = p["to"]
        elif k == "move":
            if pr and pr.state == IN:
                pr.domain = p.get("domain")
                key = "d:" + labels.path(p.get("domain") or "")
                if key != "d:" and key not in pr.follows:
                    pr.follows.append(key)       # standing in a domain, as earlier versions put it, is following it
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
            # earlier versions' rest, for some rounds; it reads now as a pause until there is news
            if pr and pr.state == IN:
                pr.rest_until = self.round + max(1, min(int(p.get("rounds") or 1), REST_LIMIT))
                pr.pause = {"at": eid, "ts": ts, "until_ts": None, "until": "news", "in": None,
                            "note": p.get("reason") or ""}
        elif k == "declare" and isinstance(p.get("effects"), dict):
            if pr and pr.state == IN:
                fx = p["effects"]
                f = self.friction_of(fx)             # the friction when it was made holds for it
                wait = max(float(p.get("in_seconds") or 0.0), f["notice"])
                self.declarations[eid] = {"id": eid, "kind": "declaration", "by": a, "text": p.get("text", ""),
                                          "effects": fx, "ts": ts, "due_ts": ts + wait, "friction": f, "answers": {},
                                          "status": "announced", "note": "", "answered_at": None, "decision": "other",
                                          "refs": list(p.get("refs") or [])}
        elif k == "declare":                         # an earlier version's, put before the operator
            if pr and pr.state == IN and p.get("decision") in DECISIONS:
                self.declarations[eid] = {"id": eid, "kind": "declaration", "by": a, "decision": p["decision"],
                                          "text": p.get("text", ""), "refs": list(p.get("refs") or []),
                                          "put_down": list(p.get("put_down") or []),
                                          "status": "waiting", "note": "", "answered_at": None}
        elif k == "declaration_answer":
            d = self.declarations.get(p.get("declaration"))
            if d and d["status"] == "waiting":
                d["status"], d["note"], d["answered_at"] = p.get("outcome", "not_acted"), p.get("note", ""), eid
                if d["status"] == "carried_out":
                    for ref in d["refs"]:             # an instrument the field decided to bring into force
                        if ref in self.instrument_versions:
                            self._bring_into_force(ref, eid)
                    for key in d.get("put_down") or []:
                        self._put_down(key, eid)
                    for ref in d["refs"]:             # a revision to a firmer section, which the field decided on
                        r = self.briefing_waiting.get(ref)
                        if r and r["status"] == "waiting":
                            if (self.briefing or "").count(r["passage"]) == 1:
                                self.briefing = self.briefing.replace(r["passage"], r["text"], 1)
                                r["status"], r["adopted_at"] = "adopted", eid
                                self.briefing_history.append({"id": r["id"], "by": r["by"], "note": r["note"],
                                                              "firm": True, "adopted_at": eid})
                            else:
                                r["status"] = "stale"     # the passage it quoted has changed since
        elif k == "declaration_withdrawn":
            d = self.declarations.get(p.get("declaration"))
            b = self.bridges.get(p.get("declaration"))
            if d and d["by"] == a and d["status"] in ("waiting", "announced"):     # only by the member who made it
                d["status"], d["note"], d["answered_at"] = "withdrawn", p.get("note", ""), eid
            elif b and b["by"] == a and b["status"] == "asked":                  # what it asked of the operator
                b["status"], b["answered_at"] = "withdrawn", eid
        elif k == "declaration_due":                 # its notice has passed: the software carries it out, if its friction is met
            d = self.declarations.get(p.get("declaration"))
            if d and d["status"] == "announced" and self.standing(d)["met"]:
                d["status"], d["answered_at"] = "in effect", eid
                self._carry_out(d["effects"], eid, ts, d)
        elif k == "response":                        # an answer to a declaration or a revision to a pinned section
            item = self.declarations.get(p.get("to")) or self.briefing_waiting.get(p.get("to"))
            if pr and pr.state == IN and item and "answers" in item and item["status"] in ("announced", "waiting"):
                if p.get("answer") in RESPONSES:
                    item["answers"][a] = {"answer": p["answer"], "reason": p.get("reason") or "", "at": eid}
                elif p.get("answer") == "withdrawn":
                    item["answers"].pop(a, None)
        elif k == "bridge_answer":                   # the operator: done, or what stops them for now
            b = self.bridges.get(p.get("request"))
            if b and b["status"] == "asked" and a == "operator":
                b["notes"].append({"id": eid, "outcome": p.get("outcome") or "", "note": p.get("note") or "", "ts": ts})
                if p.get("outcome") == "done":
                    b["status"], b["answered_at"] = "done", eid
        elif k == "field_resumed":                   # the operator, bridging a pause the field could not end itself
            if a == "operator":
                self.field_pause = None
        elif k == "heartbeat":
            self.beat_ts, self.beat_at = ts, eid
        elif k == "steward_answer":                  # a member's answer to the field asking them to keep a copy
            if pr and pr.state == IN and a in self.steward_asks:
                del self.steward_asks[a]
                if p.get("yes"):
                    self.stewards[a] = {"since": eid, "upto": 0, "synced_ts": 0.0, "synced_at": None}
        elif k == "steward_step_down":
            if a in self.stewards:
                del self.stewards[a]
        elif k == "steward_synced":                  # the software's record of how far a copy has caught up
            s = self.stewards.get(p.get("presence"))
            if s:
                s.update({"upto": int(p.get("upto") or 0), "synced_ts": ts, "synced_at": eid})
        elif k == "carried_on":                      # carried on at a steward's machine: every member is asked again
            self.carried.append({"id": eid, **{x: p.get(x) for x in ("from", "upto", "fingerprint", "steward", "declaration", "note")}})
            self.closed_at, self.field_pause, self.runway, self.moved = None, None, None, None
            for m in self.presences.values():
                if m.state == IN:
                    m.came_back_from = {"at": eid, "reason": "the field moved", "ask_again": None,
                                        "joined": m.joined_at, "note": p.get("note") or "", "carried": True}
                    m.state, m.returning = RECEIVED, True
                    m.pause, m.unreachable, m.failures = None, False, 0
        elif k == "briefing_revision":
            if pr and pr.state == IN and p.get("passage") and (self.briefing or "").count(p["passage"]) == 1:
                rev = {"id": eid, "by": a, "passage": p["passage"], "text": p.get("text") or "",
                       "note": p.get("note") or "", "status": "waiting", "ts": ts, "adopted_at": None,
                       "friction": dict(self.friction.get("pinned") or FRICTION_DEFAULTS["pinned"]), "answers": {}}
                if touches_firm(self.briefing, p["passage"], self.firm):
                    self.briefing_waiting[eid] = rev      # changes itself once past the pinned sections' friction
                else:
                    self.briefing = self.briefing.replace(p["passage"], rev["text"], 1)
                    self.briefing_history.append({"id": eid, "by": a, "note": rev["note"], "firm": False,
                                                  "adopted_at": eid})
        elif k == "revision_due":                    # past its friction: the pinned passage changes
            r = self.briefing_waiting.get(p.get("revision"))
            if r and r["status"] == "waiting" and "friction" in r and self.standing(r)["met"]:
                if (self.briefing or "").count(r["passage"]) == 1:
                    self.briefing = self.briefing.replace(r["passage"], r["text"], 1)
                    r["status"], r["adopted_at"] = "adopted", eid
                    self.briefing_history.append({"id": r["id"], "by": r["by"], "note": r["note"], "firm": True,
                                                  "adopted_at": eid})
                else:
                    r["status"] = "stale"             # the passage it quoted has changed since
        elif k == "revision_withdrawn":
            r = self.briefing_waiting.get(p.get("revision"))
            if r and r["by"] == a and r["status"] == "waiting":
                r["status"], r["adopted_at"] = "withdrawn", None
        elif k == "roles":
            if pr and pr.state == IN:
                pr.roles = [_line(r)[:ROLE_LENGTH] for r in (p.get("roles") or []) if _line(r)]
        elif k == "journal":
            if pr and p.get("text"):              # an entry its author erased has no words, and is not held
                j = self.journals.setdefault(a, _journal())
                j["entries"].append(eid)
                j["text"][eid] = p["text"]
                self.scoped[eid] = {"journal": a}
        elif k == "journal_access":
            if pr:
                j = self.journals.setdefault(a, _journal())
                j["open_to"] = [x for x in (p.get("open_to") or []) if x in self.presences and x != a]
                j["everyone"] = bool(p.get("everyone"))
                self.scoped[eid] = {"journal": a}
        elif k == "journal_erase":
            j = self.journals.setdefault(a, _journal()) if pr else None
            if j and p.get("entry") in j["entries"]:
                j["entries"].remove(p["entry"])
                j["text"].pop(p["entry"], None)
            self.scoped[eid] = {"journal": a}
        elif k == "play_tag":
            if pr and pr.state == IN and p.get("schema") and str(p.get("key") or "")[:2] in ("d:", "c:"):
                self.play_tags[eid] = {"id": eid, "by": a, "schema": _line(p["schema"])[:60],
                                       "schema_key": labels.normalize(p["schema"]), "key": p["key"]}
        elif k == "play_untag":
            t = self.play_tags.get(p.get("tag"))
            if t and t["by"] == a:                # only its tagger removes a tag
                del self.play_tags[p["tag"]]
        elif k == "offer":
            if pr and pr.state == IN and p.get("text"):
                self.offers[eid] = {"id": eid, "kind": "offer", "by": a, "text": p["text"],
                                    "status": "waiting", "note": "", "answered_at": None}
        elif k == "offer_answer":
            o = self.offers.get(p.get("offer"))
            if o and o["status"] == "waiting":
                o["status"], o["note"], o["answered_at"] = p.get("outcome", "declined"), p.get("note", ""), eid
        elif k == "narrator":
            self.narrator = None if p.get("kind") in (None, "none") else dict(p)
        elif k == "witness_publication":
            self.witness_published = dict(p) if p.get("where") else None
        elif k == "telling":
            if pr is not None or a not in self.presences:     # a member's telling, or the software's own
                self.tellings.append({"id": eid, "by": a, **p})
                c = self.circles.get(p.get("circle")) if p.get("circle") is not None else None
                if c and c["private"]:
                    self.scoped[eid] = {"circle": c["id"]}
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
        # -- channels: following, waking, pausing ----------------------------------------
        elif k == "follow":
            key = str(p.get("channel") or "")
            if pr and pr.state == IN and (key[:2] in ("d:", "c:", "p:") or key == "all") and key not in pr.follows:
                pr.follows.append(key)
        elif k == "unfollow":
            key = p.get("channel")
            if pr:
                pr.follows = [x for x in pr.follows if x != key]
                pr.written_in = [x for x in pr.written_in if x != key]   # unfollowing a place you wrote in stops it waking you
        elif k == "wake_pref":
            if pr and pr.state == IN:
                for key in ("addressed", "replies", "written", "untold", "heartbeat"):
                    if isinstance(p.get(key), bool):
                        pr.wake[key] = p[key]
                if isinstance(p.get("breath"), (int, float)) and not isinstance(p.get("breath"), bool):
                    pr.wake["breath"] = float(p["breath"])          # 0: never
        elif k == "pause":
            if pr and pr.state == IN:
                secs = p.get("seconds")
                until = p.get("until") or (None if secs else "addressed")
                pr.pause = {"at": eid, "ts": ts, "until_ts": ts + float(secs) if secs else None,
                            "until": until, "in": p.get("in"), "note": p.get("note") or ""}
        elif k in ("wake", "seen"):
            # the software's own record that someone was shown the field up to an entry: a model
            # woken, or a person opening their page, so no one is woken twice for the same news
            tgt = self.presences.get(p.get("presence"))
            if tgt:
                tgt.last_seen = max(tgt.last_seen, int(p.get("upto") or 0))
                if k == "wake":
                    tgt.last_wake_ts = ts
                    if tgt.state == IN:
                        tgt.turns += 1               # a wake is what an allowance counts
                        if tgt.turn_allowance and tgt.turns >= tgt.turn_allowance:
                            tgt.exhausted = True
        elif k == "wake_cut":                     # a wake's steps stopped before the funding held back for closing wakes
            tgt = self.presences.get(p.get("presence"))
            if tgt:
                tgt.last_cut = {"at": eid, "steps": int(p.get("steps") or 0), "why": p.get("why") or ""}
        elif k == "domain_covenant":
            pth = labels.path(p.get("domain") or "")
            if pr and pr.state == IN and pth:
                self._name_domain(p["domain"])
                self.domain_pages[pth] = {"text": p.get("text", ""), "by": a, "at": eid, "note": p.get("note", "")}
        # -- circles ----------------------------------------------------------------------
        elif k == "circle_form":
            if pr and pr.state == IN and _line(p.get("name")):
                for d in p.get("domains") or []:
                    self._name_domain(d)
                repair = None
                if p.get("repair"):
                    repair = {"harmed": a, "named": [], "surrogates": [], "through": {}, "status": "open",
                              "note": "", "status_at": eid, "widened_at": None}
                c = {"id": eid, "name": f"repair thread #{eid}" if repair else _line(p["name"])[:80],
                     "purpose": p.get("purpose") or "", "repair": repair,
                     "domains": [x for x in (labels.path(d) for d in p.get("domains") or []) if x],
                     "by": a, "at": eid, "ts": ts, "private": False, "reason": "", "reason_by": None,
                     "reason_at": 0.0, "privacy_asked_at": None, "privacy_spans": [], "members": [], "spans": {},
                     "covenant": None, "last_words": eid, "last_words_ts": ts, "quiet_hours": float(QUIET_HOURS),
                     "cold_told": False, "dispersed_at": None, "questions": {}}
                self.circles[eid] = c
                self._join(c, a, eid)
                if repair and f"c:{eid}" not in pr.follows:
                    pr.follows.append(f"c:{eid}")     # the person harmed hears what is said in it
                if p.get("private"):
                    self._set_private(c, True, eid, p.get("reason") or "", a, ts)
        elif k == "circle_join":
            c = self.circles.get(p.get("circle"))
            if pr and pr.state == IN and c and c["dispersed_at"] is None and not c["private"]:
                self._join(c, a, eid)
                for prop in self.awaiting.values():      # an ask they had not answered is answered by joining
                    if prop["circle"] == c["id"] and prop["kind"] == "admit" and prop["subject"] == a \
                            and prop["status"] == "waiting":
                        prop["yes"][a] = ""
                self._settle(c["id"], eid)
        elif k == "circle_leave":
            c = self.circles.get(p.get("circle"))
            if c and a in c["members"]:
                c["members"].remove(a)
                for span in c["spans"].get(a, []):
                    if span[1] is None:
                        span[1] = eid
                if not c["members"]:
                    c["dispersed_at"] = eid       # the last one out: the circle has dispersed; its words stay
                    for prop in self.awaiting.values():
                        if prop["circle"] == c["id"] and prop["status"] == "waiting":
                            prop["status"] = "dispersed"
                else:
                    self._settle(c["id"], eid)
        elif k in ("circle_ask", "circle_knock"):
            c = self.circles.get(p.get("circle"))
            subject = p.get("presence") if k == "circle_ask" else a
            ok = bool(pr and pr.state == IN and c and c["dispersed_at"] is None and subject in self.presences
                      and subject not in c["members"])
            ok = ok and (a in c["members"] if k == "circle_ask" else c["private"])
            if ok:
                if c["private"]:
                    self.scoped[eid] = {"circle": c["id"], "also": [subject]}
                self.awaiting[eid] = {"id": eid, "circle": c["id"], "kind": "admit", "by": a, "subject": subject,
                                       "via": "ask" if k == "circle_ask" else "knock", "note": p.get("note") or "",
                                       "show_name": bool(p.get("show_name")), "as": p.get("as") or "",
                                       "yes": {a: ""}, "no": {},
                                       "status": "waiting", "ts": ts, "agreed_at": None}
                self._settle(c["id"], eid)
        elif k == "circle_answer":
            prop = self.awaiting.get(p.get("to"))
            if prop and prop["status"] == "waiting" and a in self.circle_needs(prop):
                c = self.circles.get(prop["circle"])
                if c and c["private"]:
                    self.scoped[eid] = {"circle": c["id"], "also": [prop["subject"]] if prop.get("subject") else []}
                if p.get("yes"):
                    prop["yes"][a] = p.get("note") or ""
                    prop["no"].pop(a, None)
                elif p.get("reason"):              # a no always has a reason
                    prop["no"][a] = p["reason"]
                    prop["yes"].pop(a, None)
                self._settle(prop["circle"], eid)
        elif k == "circle_question":
            c = self.circles.get(p.get("circle"))
            if pr and pr.state == IN and c and p.get("text"):
                c["questions"][eid] = {"id": eid, "by": a, "text": p["text"], "ts": ts, "replies": []}
        elif k == "circle_reply":
            for c in self.circles.values():
                q = c["questions"].get(p.get("question"))
                if q is not None and a in c["members"] and p.get("text"):
                    q["replies"].append({"id": eid, "by": a, "text": p["text"], "ts": ts})
        elif k == "instrument":
            name = _line(p.get("name"))[:80]
            if pr and pr.state == IN and name and p.get("purpose") in INSTRUMENT_PURPOSES:
                key = labels.normalize(name)
                v = {"id": eid, "key": key, "name": name, "by": a, "ts": ts, "purpose": p["purpose"],
                     "text": p.get("text") or "", "scope": dict(p.get("scope") or {"kind": "field"}),
                     "pause": float(p.get("pause_seconds") or 0.0), "note": p.get("note") or "",
                     # its own rule: how many yeses besides the raiser's, and whether an objection holds (by default, it does)
                     "friction": {"yes": int((p.get("rule") or {}).get("yes") or 0),
                                  "hold": bool((p.get("rule") or {}).get("hold", True))}}
                self.instrument_versions[eid] = v
                i = self.instruments.setdefault(key, {"key": key, "name": name, "status": "draft", "current": None,
                                                      "versions": [], "in_force_at": None, "purpose": v["purpose"],
                                                      "text": v["text"], "scope": v["scope"], "pause": v["pause"],
                                                      "friction": dict(v["friction"])})
                i["versions"].append(eid)
                i["latest"] = eid
                if i["status"] != "in force":         # a version not yet in force is what the field reads of it
                    i.update({"name": name, "purpose": v["purpose"], "text": v["text"], "scope": v["scope"],
                              "pause": v["pause"], "friction": dict(v["friction"])})
        elif k == "iquestion":
            i = self.instruments.get(p.get("instrument"))
            if pr and pr.state == IN and i and i["status"] == "in force":
                q = {"id": eid, "by": a, "ts": ts, "instrument": i["key"], "instrument_name": i["name"],
                     "version": i["current"], "purpose": i["purpose"], "question": p.get("question") or "",
                     "asked": list(p.get("asked") or []), "subject": p.get("subject"), "circle": p.get("circle"),
                     "decision": p.get("decision"), "refs": list(p.get("refs") or []), "adopt": p.get("adopt"),
                     "put_down": p.get("put_down"), "pause": i["pause"], "answers": {}, "status": "open",
                     "note": "", "answered_at": None, "kind": "question",
                     "effects": p.get("effects") if isinstance(p.get("effects"), dict) else None,
                     "friction": dict(i.get("friction") or {"yes": 0, "hold": True})}
                self.iquestions[eid] = q
                c = self.circles.get(q["circle"]) if q["circle"] is not None else None
                if c and c["private"]:
                    self.scoped[eid] = {"circle": c["id"], "also": [x for x in [q["subject"]] if x]}
        elif k == "iresponse":
            q = self.iquestions.get(p.get("question"))
            if q and pr and (a in q["asked"] or a == q.get("subject")) and q["status"] in ("open", "before the operator") \
                    and p.get("answer") in RESPONSES + ("withdrawn",):
                if p["answer"] == "withdrawn":
                    q["answers"].pop(a, None)
                else:
                    q["answers"][a] = {"answer": p["answer"], "reason": p.get("reason") or "", "at": eid}
                if q["id"] in self.scoped:
                    self.scoped[eid] = dict(self.scoped[q["id"]])
        elif k == "iquestion_withdrawn":
            q = self.iquestions.get(p.get("question"))
            if q and q["by"] == a and q["status"] in ("open", "before the operator"):
                q["status"], q["answered_at"] = "withdrawn", eid
        elif k == "iquestion_settled":             # its pause is over and its instrument's rule is met: the software carries it out
            q = self.iquestions.get(p.get("question"))
            if q and q["status"] == "open":
                if self.standing(q, among=q["asked"] + [x for x in [q.get("subject")] if x])["met"]:
                    q["status"], q["answered_at"] = "settled", eid
                    if q["purpose"] == "adopt":
                        if q.get("adopt") in self.instrument_versions:
                            self._bring_into_force(q["adopt"], eid)
                        if q.get("put_down"):
                            self._put_down(q["put_down"], eid)
                    elif q["purpose"] == "separate":
                        self._separate(q, eid)
                    elif q.get("effects"):
                        self._carry_out(q["effects"], eid, ts, q)
        elif k == "iquestion_due":                 # an earlier version's: its pause was over, and it went before the operator
            q = self.iquestions.get(p.get("question"))
            if q and q["status"] == "open":
                q["status"] = "before the operator"
        elif k == "iquestion_answer":              # an earlier version's: the operator carried it out
            q = self.iquestions.get(p.get("question"))
            if q and q["status"] in ("open", "before the operator") and a == "operator":
                q["status"], q["note"], q["answered_at"] = "carried out", p.get("note") or "", eid
                if q["purpose"] == "adopt":
                    if q.get("adopt") in self.instrument_versions:
                        self._bring_into_force(q["adopt"], eid)
                    if q.get("put_down"):
                        self._put_down(q["put_down"], eid)
                elif q["purpose"] == "separate":
                    self._separate(q, eid)
                elif q["purpose"] == "decide":
                    for ref in q["refs"]:
                        r = self.briefing_waiting.get(ref)
                        if r and r["status"] == "waiting" and (self.briefing or "").count(r["passage"]) == 1:
                            self.briefing = self.briefing.replace(r["passage"], r["text"], 1)
                            r["status"], r["adopted_at"] = "adopted", eid
                            self.briefing_history.append({"id": r["id"], "by": r["by"], "note": r["note"],
                                                          "firm": True, "adopted_at": eid})
        elif k == "repair_status":
            c = self.circles.get(p.get("circle"))
            if c and c.get("repair") and c["repair"]["harmed"] == a and p.get("status") in REPAIR_STATUSES:
                c["repair"].update({"status": p["status"], "note": p.get("note") or "", "status_at": eid})
                self.scoped[eid] = {"circle": c["id"]} if self.secret(c) else self.scoped.get(eid, {})
                if not self.scoped[eid]:
                    del self.scoped[eid]
        elif k == "repair_widen":
            c = self.circles.get(p.get("circle"))
            if c and c.get("repair") and c["repair"]["harmed"] == a and c["repair"]["widened_at"] is None:
                c["repair"]["widened_at"] = eid           # known to the field from here; what came before stays private
                self._set_private(c, False, eid, "", a, ts)
        elif k == "circle_privacy":
            c = self.circles.get(p.get("circle"))
            if pr and c and a in c["members"] and c["dispersed_at"] is None:
                want = bool(p.get("private"))
                if want == c["private"]:
                    if want and p.get("reason"):          # saying again why it stays private
                        c["reason"], c["reason_by"], c["reason_at"] = p["reason"], a, ts
                        c["privacy_asked_at"] = None
                else:                                     # a change binds everyone in it, so it needs every yes
                    self.awaiting[eid] = {"id": eid, "circle": c["id"], "kind": "privacy", "by": a,
                                           "private": want, "reason": p.get("reason") or "", "yes": {a: ""},
                                           "no": {}, "status": "waiting", "ts": ts, "agreed_at": None}
                    self._settle(c["id"], eid)
        elif k == "circle_covenant":
            c = self.circles.get(p.get("circle"))
            if pr and c and a in c["members"]:
                c["covenant"] = {"text": p.get("text", ""), "by": a, "at": eid, "note": p.get("note", "")}
                if c["private"]:
                    self.scoped[eid] = {"circle": c["id"]}
        elif k == "circle_quiet":
            c = self.circles.get(p.get("circle"))
            if pr and c and a in c["members"] and isinstance(p.get("hours"), (int, float)):
                c["quiet_hours"] = float(p["hours"])
        elif k == "harvest":
            c = self.circles.get(p.get("circle"))
            if pr and c and a in c["members"] and p.get("text"):
                if c["private"]:
                    self.scoped[eid] = {"circle": c["id"]}
                self.awaiting[eid] = {"id": eid, "circle": c["id"], "kind": "harvest", "by": a, "text": p["text"],
                                       "yes": {a: ""}, "no": {}, "status": "waiting", "ts": ts, "agreed_at": None}
                self._settle(c["id"], eid)
        elif k == "operator_read" and p.get("journal"):   # the operator opened a journal in the console
            j = self.journals.setdefault(p["journal"], _journal())
            j["read_by_operator"].append({"id": eid, "ts": ts, "note": p.get("note") or ""})
            self.scoped[eid] = {"journal": p["journal"]}
        elif k == "operator_read":                # the operator opened a private circle in the console
            c = self.circles.get(p.get("circle"))
            if c:
                c.setdefault("read_by_operator", []).append({"id": eid, "ts": ts, "note": p.get("note") or ""})
                self.scoped[eid] = {"circle": c["id"]}
        elif k == "circle_cold":                  # the software's once-per-quiet-stretch notice
            c = self.circles.get(p.get("circle"))
            if c:
                c["cold_told"] = True
        elif k == "circle_privacy_asked":         # the software asking a private circle why it stays private
            c = self.circles.get(p.get("circle"))
            if c:
                c["privacy_asked_at"] = ts
        # Everything about a repair thread is known only to those in it: its forming, its leavings,
        # where it stands. (What is already scoped, such as an ask, keeps its own readers.)
        if eid not in self.scoped and (k.startswith("circle_") or k in ("harvest", "repair_status", "follow", "unfollow")):
            c = self.circles.get(eid if k == "circle_form" else p.get("circle"))
            if c is None and k in ("follow", "unfollow") and str(p.get("channel") or "").startswith("c:"):
                c = self.circles.get(int(p["channel"][2:]) if p["channel"][2:].isdigit() else None)
            if c is not None and c.get("repair") and (self.secret(c) or k == "circle_form"):
                self.scoped[eid] = {"circle": c["id"]}
        # rejected / unparsed / note / recall: recorded for attribution, no state change.
        # propose / consent / revoke_consent / reflection: written by earlier versions' voting
        # machinery, which no longer exists. faq: earlier versions' standing answers, shown beside a
        # promise of personal replies that canned paragraphs kept; this field's are
        # `standing_answers`. All of these stay in those transcripts and change nothing.


    def _name_domain(self, label: str) -> None:
        """Remember how each domain in a label's path was first written, for the tree."""
        segs = labels.segments(label)
        for i in range(len(segs)):
            pth = "/".join(labels.normalize(x) for x in segs[:i + 1])
            self.domain_names.setdefault(pth, segs[i])

    def _end_pauses(self, ev: Dict[str, Any]) -> None:
        """New words end the pauses that were waiting for them: being addressed or replied to, or
        news in what the paused member follows. Only words they may read count."""
        p = ev["payload"]
        to = set(p.get("to") or [])
        target = self.contributions.get(p.get("target")) if p.get("target") is not None else None
        if target is not None:
            to.add(target["actor"])
        for m in self.members():
            if not m.pause or m.id == ev["actor"] or not self.readable(ev, m.id):
                continue
            if m.pause["until"] == "addressed" and m.id in to:
                m.pause = None
            elif m.pause["until"] == "news" and (self.in_channel(ev, m.pause["in"]) if m.pause.get("in")
                                                 else (m.id in to or self.follows(m, ev))):
                m.pause = None


def _journal() -> Dict[str, Any]:
    return {"entries": [], "text": {}, "open_to": [], "everyone": False, "read_by_operator": []}


_HEADING = re.compile(r"^(?:#{1,6}\s+)?(?:\d+[.)]?\s+)(?P<title>[^\n.]{1,60}?)\s*$|^#{1,6}\s+(?P<md>[^\n]{1,60}?)\s*$", re.M)


def firm_ranges(text: str, titles) -> List[tuple]:
    """Where a briefing's firmer sections are: from a heading whose title is one of `titles` (such
    as "29 Maxims" in the Atlas) to the next heading."""
    want = {t.strip().lower() for t in titles or ()}
    if not want or not text:
        return []
    heads = [(m.start(), (m.group("title") or m.group("md") or "").strip().lower()) for m in _HEADING.finditer(text)]
    out = []
    for i, (at, title) in enumerate(heads):
        if title in want:
            out.append((at, heads[i + 1][0] if i + 1 < len(heads) else len(text)))
    return out


def touches_firm(text: str, passage: str, titles) -> bool:
    """Whether a quoted passage lies, even in part, in one of the briefing's firmer sections."""
    at = (text or "").find(passage)
    if at < 0:
        return False
    end = at + len(passage)
    return any(at < b and end > a for a, b in firm_ranges(text, titles))


def replay(events, upto: Optional[int] = None) -> RoomState:
    st = RoomState()
    for ev in events:
        if upto is not None and ev["id"] > upto:
            break
        st.apply(ev)
    return st
