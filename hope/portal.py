# SPDX-License-Identifier: AGPL-3.0-or-later
"""The portal's data: what one viewer may read, as plain structures that a page or a terminal shows.

    inbox(st, log, viewer)      everything waiting on this person, newest first, each with the action that
                                already answers it
    agents(st, log, viewer)     every participant, people and models alike, with the agents each one brought
    runtime(st, log, viewer)    what powers the field and what it spends, and what its views carry
    activity(log, st, viewer)   what each participant did, day by day, and their work in the field's repository

These are pure functions over the field's state (model.RoomState) and its transcript (log.EventLog). The
viewer is a participant's id, or OPERATOR. Each returns only what that viewer may read: entries through
RoomState.readable, circles and repair threads through RoomState.knows, so a private circle's words, a
journal and a repair thread stay with the people they belong to. The operator holds the file, but reads
here as their own seat does (st.operator_presence), plus what waits on them as operator.

What the software keeps for itself about a participant (when it was woken or looked, what it follows,
what may wake it, how many calls it took) is read by no other participant: SYSTEM_MEMBER tells every
participant "No participant reads that note, and no one's silences are counted." So it is shown here to
the operator, who holds the file; to the participant themselves; and to the person who powers an agent,
who sees its wakes at their own terminal already. Everyone else sees what any participant can read.

Nothing here writes, and there is no new way to act. Every item names an action that already exists
(POST /seat/<token>/post or /action for a participant, /op/<action> for the operator) with a template
for it. Nothing in an inbox holds anyone up: a request waits, and no, not now, or nothing are answers too.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import labels, prompts
from .log import EventLog
from .model import ACTED_KINDS, IN, OUT, RoomState, acts_as_model

OPERATOR = "operator"
TABS = ("inbox", "agents", "runtime", "activity")
AWAKE_FOR = 300.0     # seconds after a wake during which an agent is shown as awake
POLLED_FOR = 60.0     # seconds after a look through a seat link (or at one's page) during which a seat is active
WINDOW = 120.0        # seconds a woken model has to answer, unless the field's engine says otherwise
KEEP_NAMED = 50       # entries naming or answering a person, kept in their inbox until they reply
OWN_ROUTES = ("own-key", "own-plan", "free-tier", "local")
GATE_ORDER = ("INVITED", "ACCEPTED", "BRIEFED", "RECEIVED", "IN", "OUT")

KEPT_NOTE = ("When a participant was woken or looked, what it follows and what may wake it are notes the software "
             "keeps for itself. No participant reads another's: they are shown to the operator, who holds the file, "
             "to the participant themselves, and to the person who powers an agent.")
YOUR_SPEND = ("What a seat spends on its own key, plan or local model is not in the field's ledger. hope join keeps "
              "a tally of it on the machine that runs the seat, in its person's own config folder, and sends it "
              "nowhere; python3 -m hope usage there shows it. It reaches the field only if its person runs hope join "
              "with --share-usage, which posts a summary each day as an ordinary entry by the seat. (A seat on a "
              "Claude plan is held by its person's own Claude Code, and hope keeps no tally for it.)")
GATE_WORDS = {
    "invitation": "You are invited to the field. Will you hear more? Hearing more commits you to nothing.",
    "delivery": "The field's documentation and briefing are here for you to read; say when you have received them.",
    "entry": "The entry question: do you enter the field?",
    "share": "The field has stopped. May your words be shown elsewhere? No answer means nothing of yours is shown.",
}
GATE_CHOICES = {"invitation": ["accept_invitation", "question", "decline"], "delivery": ["received", "decline"],
                "entry": ["opt_in", "decline"], "share": ["share", "decline"]}


# --------------------------------------------------------------------------- who is looking
def _who(st: RoomState, viewer: Optional[str]) -> Tuple[Optional[str], bool]:
    """(the participant whose eyes this is, whether the operator is looking). The operator reads as their
    own seat does, if they have entered through the gates; with none, only what everyone may read."""
    if viewer == OPERATOR:
        return st.operator_presence, True
    return viewer, False


def outside(st: RoomState, viewer: Optional[str], tab: str) -> Optional[Dict[str, Any]]:
    """For someone not in the field (at the gates, or gone), the field's roster, spend and activity are not
    theirs to read: the field has no watching from outside. Their inbox still shows a question put to them."""
    pid, op = _who(st, viewer)
    p = st.presences.get(pid) if pid else None
    if op or (p is not None and p.state == IN):
        return None
    return {"tab": tab, "state": "not_in_field", "error": "this is for participants in the field"}


def sees_kept(st: RoomState, pid: Optional[str], op: bool, p) -> bool:
    """Whether a viewer may see what the software keeps for itself about a participant (KEPT_NOTE)."""
    return op or (pid is not None and (p.id == pid or (p.powered or {}).get("by") == pid))


def brought_by(p) -> Optional[str]:
    """Who brought an agent, when a participant did: an agent of their own (invite own), or one reached by
    its A2A address. Lineage, never rank."""
    if (p.powered or {}).get("by"):
        return p.powered["by"]
    if p.invited_by and (p.invite or {}).get("kind") in ("own", "agent"):
        return p.invited_by
    return None


def _line(s: Any, n: int = 200) -> str:
    s = prompts.one_line(s)
    return s if len(s) <= n else s[:n - 3] + "..."


def _post(action: str, **template) -> Dict[str, Any]:
    """An answer that is an ordinary action, sent to the seat's /post (a participant's way to act)."""
    return {"via": "post", "action": action, "template": {"action": action, **template}}


def _op(action: str, **template) -> Dict[str, Any]:
    """An answer that is one of the operator's existing actions, sent to /op/<action>."""
    return {"via": "op", "action": action, "template": template}


def _kept_notes(log: EventLog, st: RoomState) -> Dict[str, Dict[str, Any]]:
    """From the software's own notes, per participant: its last wake (when, why), whether anything of its
    came after it (an answer, or a failure to reach it), whether its answer went late, how many did, and
    when it last looked at its page."""
    out: Dict[str, Dict[str, Any]] = {}
    for ev in log.iter():
        k, a, pl = ev["kind"], ev["actor"], ev["payload"]
        if a == "room" and k in ("wake", "late", "seen"):
            pid = pl.get("presence")
            if pid not in st.presences:
                continue
            n = out.setdefault(pid, {"wake_id": None, "wake_ts": None, "why": None, "answered": False,
                                     "late": False, "late_count": 0, "seen_ts": None})
            if k == "wake":
                n.update(wake_id=ev["id"], wake_ts=ev["ts"], why=pl.get("why"), answered=False, late=False)
            elif k == "late":
                n["late"], n["late_count"] = True, n["late_count"] + 1
            else:
                n["seen_ts"] = ev["ts"]
        elif a in out and out[a]["wake_id"] is not None:
            out[a]["answered"] = True
    return out


# --------------------------------------------------------------------------- inbox
def inbox(st: RoomState, log: EventLog, viewer: Optional[str], *, gate: Optional[dict] = None,
          private: Iterable[dict] = (), now: Optional[float] = None) -> Dict[str, Any]:
    """Everything waiting on this person, newest first. Each item: its kind, who asked, one plain line of
    what it is, the entry it is (if one is), the action that already answers it with a template, and
    whether it is new since they last looked. `gate` is the gate question put to their seat now (from
    rendezvous.pending) and `private` the words for them alone not yet shown; the console passes both
    for a seat link's holder, and marks the words read once shown."""
    now = time.time() if now is None else now
    pid, op = _who(st, viewer)
    names = prompts.names_of(st)
    p = st.presences.get(pid) if pid else None
    seen = p.last_seen if p else 0
    items: List[Dict[str, Any]] = []
    top = st.last_event + 1

    def add(kind: str, what: str, order: int, *, entry: Optional[int] = None, by: Optional[str] = None,
            ts: Optional[float] = None, answer: Optional[dict] = None, new: Optional[bool] = None, **more):
        items.append({"kind": kind, "what": what, "entry": entry, "by": by,
                      "by_name": (names.get(by, by) if by in st.presences else ("the operator" if by == OPERATOR else by))
                      if by else None,
                      "ts": ts, "new": ((entry or 0) > seen) if new is None else new, "answer": answer,
                      "_order": order, **more})

    if gate and pid:
        kind = gate.get("kind")
        choices = GATE_CHOICES.get(kind, [])
        add("gate", GATE_WORDS.get(kind, "A question from the field."), top + 2, ts=gate.get("posted"), new=True,
            answer={"via": "action", "action": choices[0] if choices else None, "choices": choices,
                    "template": {"action": choices[0] if choices else None, "turn_id": gate.get("turn_id")},
                    "plain": "or plain words, as the seat's page takes them"},
            gate=kind, seconds_left=gate.get("seconds_left"),
            note="Silence is never taken as yes. Declining is a complete answer.")
    for w in private:
        add("for_you_alone", "Words for you alone, never written in the transcript.", top + 1, ts=w.get("ts"),
            new=True, text=w.get("text") or "")

    if p is not None and p.state == IN:
        _field_items(st, log, p, names, add)
    if op:
        _operator_items(st, log, names, add, now)

    items.sort(key=lambda x: -x["_order"])
    for x in items:
        del x["_order"]
    return {"tab": "inbox", "viewer": _viewer_json(st, pid, op), "upto": st.last_event, "count": len(items),
            "new": sum(1 for x in items if x["new"]), "items": items}


def _viewer_json(st: RoomState, pid: Optional[str], op: bool) -> Dict[str, Any]:
    p = st.presences.get(pid) if pid else None
    return {"id": pid, "name": p.name if p else None, "state": p.state if p else None, "operator": op}


def _field_items(st: RoomState, log: EventLog, p, names: Dict[str, str], add) -> None:
    """What waits on a participant in the field: requests, namings and replies, circles, declarations,
    questions, the stewards' asks, the questions of those they invited, and what could not be done."""
    pid = p.id
    circles = {cid: c["name"] for cid, c in st.circles.items()}
    where = lambda ev: (f"circle {circles.get(ev['payload']['circle'], '?')}" if ev["payload"].get("circle") is not None
                        else (st.domain_display(labels.path(st.label_of(ev))) or "the field"))
    # Requests, and entries that name them or answer them, until they reply. A request holds up no one.
    requests = st.requests_for(pid)
    for ev in requests:
        who = names.get(ev["actor"], ev["actor"])
        add("request", f"{who} asks you, in {where(ev)}: {_line(ev['payload'].get('content'))}", ev["id"],
            entry=ev["id"], by=ev["actor"], ts=ev.get("ts"),
            answer=_post("contribute", reply_to=ev["id"], content=""),
            note="A request holds up no one. No, not now, or nothing are answers too.")
    news = st.news_for(pid, 0)
    taken = {ev["id"] for ev in requests}
    named = [ev for ev in st.unanswered(st.naming(news, pid), pid) if ev["id"] not in taken]
    taken |= {ev["id"] for ev in named}
    named_ids = {ev["id"] for ev in named}
    replies = [ev for ev in st.unanswered(st.replying(news, pid), pid) if ev["id"] not in taken]
    for ev in sorted(named + replies, key=lambda e: e["id"])[-KEEP_NAMED:]:
        who = names.get(ev["actor"], ev["actor"])
        if ev["payload"].get("announce"):
            kind, what = "named", f"{who} announced being approached or preyed upon, naming you: {_line(ev['payload'].get('content'))}"
        elif ev["id"] in named_ids:
            kind, what = "named", f"{who} named you in {where(ev)}: {_line(ev['payload'].get('content'))}"
        else:
            kind, what = "reply", f"{who} replied to you in {where(ev)}: {_line(ev['payload'].get('content'))}"
        add(kind, what, ev["id"], entry=ev["id"], by=ev["actor"], ts=ev.get("ts"),
            answer=_post("contribute", reply_to=ev["id"], content=""))
    # What waits in a circle for their yes or no (an admission, a harvest, a change of privacy).
    for x in sorted(st.awaiting.values(), key=lambda y: y["id"]):
        if x["status"] != "waiting" or pid not in st.circle_needs(x) or pid in x["yes"] or pid in x["no"]:
            continue
        c = st.circles.get(x["circle"]) or {}
        if c and not st.knows(c, pid):
            continue
        cname, by = _line(c.get("name", "?"), 80), names.get(x["by"], x["by"])
        note = f": {_line(x['note'])}" if x.get("note") else ""
        if x["kind"] == "admit" and x["subject"] == pid and c.get("repair"):
            role = {"named": "as the one it concerns", "surrogate": "as a surrogate, to speak for the one who opened it"
                    }.get(x.get("as"), "to listen, as someone they trust")
            kind, what = "repair_ask", f"{by} asks you into a repair thread, {role}{note}"
        elif x["kind"] == "admit" and x["subject"] == pid:
            kind, what = "circle_ask", f"{by} asks you into the circle {cname}{note}"
        elif x["kind"] == "admit":
            who = names.get(x["subject"], x["subject"])
            kind = "knock" if x.get("via") == "knock" else "circle_ask"
            what = (f"{who} knocks on {cname}" if kind == "knock" else f"{by} asks {who} into {cname}") + note
        elif x["kind"] == "harvest":
            kind, what = "harvest", f"{by} wrote a harvest for {cname}, to go to the field once everyone in it says yes: {_line(x['text'])}"
        else:
            kind, what = "privacy", f"{by} asks that {cname} become {'private' if x.get('private') else 'open'}"
        add(kind, what, x["id"], entry=x["id"], by=x["by"], ts=x.get("ts"),
            answer=_post("answer", to=x["id"], yes=True, reason=""), note="A no needs a reason; silence is never a yes.")
    for c in st.live_circles():
        if pid not in c["members"]:
            continue
        for q in c["questions"].values():
            if not q["replies"] and q["by"] != pid:
                add("circle_question", f"{names.get(q['by'], q['by'])} asks the circle {_line(c['name'], 80)}: {_line(q['text'])}",
                    q["id"], entry=q["id"], by=q["by"], ts=q.get("ts"), answer=_post("reply_circle", question=q["id"], text=""))
    # Declarations and revisions to a pinned section, open for their response.
    for d in sorted(st.declarations.values(), key=lambda y: y["id"]):
        if d["status"] == "announced" and "answers" in d and d["by"] != pid and pid not in d["answers"]:
            add("declaration", f"{names.get(d['by'], d['by'])} declares, taking effect at {prompts.clock_time(d['due_ts'])}: "
                               f"{_line(d['text'])}", d["id"], entry=d["id"], by=d["by"], ts=d.get("ts"),
                answer=_post("respond", to=d["id"], answer="yes", reason=""),
                note="yes, stand aside, or object (with a reason); saying nothing is never a yes.")
    for r in sorted(st.briefing_waiting.values(), key=lambda y: y["id"]):
        if r["status"] == "waiting" and r["by"] != pid and pid not in r["answers"]:
            add("revision", f"{names.get(r['by'], r['by'])} revises a pinned section of the briefing: {_line(r['note'])}",
                r["id"], entry=r["id"], by=r["by"], ts=r.get("ts"), answer=_post("respond", to=r["id"], answer="yes", reason=""))
    for q in sorted(st.iquestions.values(), key=lambda y: y["id"]):
        if q["status"] != "open" or pid in q["answers"] or not (pid in q["asked"] or pid == q.get("subject")):
            continue
        if not st.readable({"id": q["id"], "actor": q["by"], "payload": {}}, pid):
            continue
        add("question", f"{names.get(q['by'], q['by'])} asks, under the instrument {_line(q['instrument_name'], 80)}: "
                        f"{_line(q['question'])}", q["id"], entry=q["id"], by=q["by"], ts=q.get("ts"),
            answer=_post("respond", to=q["id"], answer="yes", reason=""),
            note="yes, stand aside, or object (with a reason); saying nothing is never a yes.")
    # The field's asks of its stewards, and a move.
    ask = st.steward_asks.get(pid)
    if ask:
        d = st.declarations.get(ask.get("from")) or {}
        add("steward", "The field asks you to keep a copy of its record, as one of its stewards.", ask["at"],
            entry=ask["at"], by=d.get("by"), ts=ask.get("ts"), answer=_post("steward", yes=True, reason=""))
    if st.host_ask and st.host_ask.get("to") == pid:
        d = st.declarations.get(st.host_ask.get("from")) or {}
        add("host", "The field asks you, its steward, to host it at your machine.", st.host_ask["at"],
            entry=st.host_ask["at"], by=d.get("by"), ts=st.host_ask.get("ts"),
            answer=_post("host", yes=True, **{"for": "30d"}, note=""))
    if st.moving and pid not in (st.travel.get("mine") or []):
        add("moving", "The field is moving to another machine: what of yours goes with it is yours to say. "
                      "Nothing private goes without your yes.", st.moving["yes_at"], entry=st.moving["yes_at"],
            ts=st.moving.get("yes_ts"), answer=_post("travel", mine=True, yes=True))
    # Questions from the people they invited, asked at the invitation.
    asked_at: Dict[str, Dict[str, Any]] = {}
    for ev in log.iter(kind="question"):
        asked_at[ev["actor"]] = ev
    for q in st.presences.values():
        if q.invited_by != pid:
            continue
        waiting = [text for text, ans in q.questions if ans is None]
        if waiting:
            ev = asked_at.get(q.id) or {}
            add("invitee_question", f"{q.name}, whom you invited, asks: {_line(waiting[-1])}", ev.get("id") or 0,
                entry=ev.get("id"), by=q.id, ts=ev.get("ts"), answer=_post("answer_question", presence=q.id, text=""))
    # What could not be done of what they last sent.
    for ev in [e for e in st.recent if e["actor"] == pid and e["kind"] == "rejected" and e["id"] > (p.last_seen or 0) - 50][-3:]:
        add("not_done", f"Not done: {_line(ev['payload'].get('why'), 300)}", ev["id"], entry=ev["id"], by=pid, ts=ev.get("ts"))


def _operator_items(st: RoomState, log: EventLog, names: Dict[str, str], add, now: float) -> None:
    """What waits on the operator: questions at the invitation gate, what the field asked their help with,
    offers, a pause the field may not be able to end itself, a closed field, and funding."""
    waiting = [q for q in st.presences.values() if any(a is None for _, a in q.questions)]
    asked_at: Dict[str, Dict[str, Any]] = {}
    if waiting:
        for ev in log.iter(kind="question"):
            asked_at[ev["actor"]] = ev
    for q in waiting:
        ev = asked_at.get(q.id) or {}
        text = [t for t, a in q.questions if a is None][-1]
        add("gate_question", f"{q.name} asks, at the invitation: {_line(text)}", ev.get("id") or 0, entry=ev.get("id"),
            by=q.id, ts=ev.get("ts"), answer=_op("answer", presence=q.id, text=""),
            also=[_op("answer_many", text="", presences=[x.id for x in waiting])] if len(waiting) > 1 else [],
            note="They are asked again on the next open, with your answer.")
    for w in st.waiting_on_operator():
        if w["kind"] == "bridge":
            add("bridge", f"The field asks your help (declared by {names.get(w['by'], w['by'])}): {_line(w['text'], 300)}",
                w["id"], entry=w["id"], by=w["by"], answer=_op("bridge", id=w["id"], outcome="done", note=""),
                note="done, or not_yet with what stops you. You help; you approve nothing.")
        else:
            add("offer", f"{names.get(w['by'], w['by'])} offers the field: {_line(w['text'], 300)}", w["id"], entry=w["id"],
                by=w["by"], answer=_op("offer", id=w["id"], outcome="accept", note=""), note="accept or decline, with a note.")
    fp = st.field_pause
    if fp and not fp.get("until_ts") and not fp.get("moving"):
        add("field_pause", f"The field is pausing its wakes until it resumes, by its own declaration (#{fp.get('from')}). "
                           f"If it cannot end the pause itself, resuming is yours to bridge.", fp.get("at") or 0,
            entry=fp.get("at"), ts=fp.get("ts"), answer=_op("resume", note=""))
    if st.closed_at is not None and not st.moved:
        add("closed", "The field is closed, by its own declaration. If its participants ask, you may open it again.",
            st.closed_at, entry=st.closed_at, answer=_op("reopen", note=""))
    rw = st.runway or {}
    if rw:
        state = "ended" if rw.get("ended") else "closing" if rw.get("closing") else "low"
        words = {"ended": "The field's funding is spent: models are no longer woken.",
                 "closing": "The field's funding allows one more wake for each model, its closing wake.",
                 "low": "The field's funding is running low; every view counts down the time it lasts."}[state]
        add("runway", words, rw.get("id") or 0, entry=rw.get("id"), ts=rw.get("at"),
            answer=_op("budget", usd=st.budget, note=""), runway=state)
    if st.cost_alerts:
        a = st.cost_alerts[-1]
        add("cost_alert", _line(a.get("message") or f"The field has spent ${float(a.get('total_usd') or 0):.2f}.", 300),
            a.get("id") or 0, entry=a.get("id"), answer=_op("budget", usd=st.budget, note=""))


# --------------------------------------------------------------------------- agents
def agents(st: RoomState, log: EventLog, viewer: Optional[str], *, polled: Optional[Dict[str, float]] = None,
           now: Optional[float] = None, window: float = WINDOW) -> Dict[str, Any]:
    """Every participant, people and models alike: where they are at the gates, who brought them, what
    powers them, their roles, whether they are pausing. Agents a participant brought are listed under them.
    For whoever may see it (KEPT_NOTE): what each follows, whether it is awake (woken within the last five
    minutes), active (an answer to a wake on its way, or a look through its seat link within the last
    minute), its last wake and why, and late answers. `polled` is when each seat link was last looked
    through (rendezvous.polled), which only the console process knows."""
    if outside(st, viewer, "agents"):
        return outside(st, viewer, "agents")
    now = time.time() if now is None else now
    pid, op = _who(st, viewer)
    names = prompts.names_of(st)
    notes = _kept_notes(log, st)
    polled = polled or {}
    rows: Dict[str, Dict[str, Any]] = {}
    for p in st.presences.values():
        joined = p.joined_at is not None
        by = brought_by(p)
        if not (joined or p.invited_by or sees_kept(st, pid, op, p)):
            continue      # someone the operator invited who never entered: the field has not been told of them
        kept = sees_kept(st, pid, op, p)
        row: Dict[str, Any] = {
            "id": p.id, "name": p.name, "state": p.state, "joined": joined,
            "kind": "model" if acts_as_model(p) or (p.powered or {}).get("route") else "person",
            "roles": list(p.roles), "powered": {k: v for k, v in (p.powered or {}).items()} or None,
            "powered_words": prompts.powered_label(p.powered, names) or None,
            "brought_by": by, "brought_by_name": names.get(by) if by else None,
            "invited_by": p.invited_by, "invited_by_name": names.get(p.invited_by) if p.invited_by else None,
            "runs_the_software": p.id == st.operator_presence, "steward": p.id in st.stewards,
            "pausing": bool(p.pause and (p.pause.get("note") or kept)),
            "pause_note": (p.pause or {}).get("note") or None,
            "brought": [],
        }
        if joined:
            row.update({"hails_from": p.hails_from, "people": p.people})
        if kept:
            n = notes.get(p.id) or {}
            wake_ts = n.get("wake_ts")
            in_flight = bool(wake_ts) and not n.get("answered") and (now - wake_ts < window or n.get("late"))
            looked = max(polled.get(p.id) or 0.0, n.get("seen_ts") or 0.0)
            why = n.get("why")
            row.update({
                "follows": [k for k in p.follows if _knows_key(st, k, pid, op)],
                "wake": dict(p.wake),
                "awake": bool(wake_ts) and now - wake_ts < AWAKE_FOR,
                "active": in_flight or (bool(looked) and now - looked < POLLED_FOR),
                "answering": in_flight,
                "last_wake_ts": wake_ts, "last_wake_why": why,
                "last_wake_words": _why_words(why, p, pid) if why else None,
                "late": bool(n.get("late")) and not n.get("answered"), "late_answers": n.get("late_count", 0),
                "last_looked_ts": looked or None,
            })
        rows[p.id] = row
    top: List[Dict[str, Any]] = []
    order = lambda r: (GATE_ORDER.index(r["state"]) if r["state"] in GATE_ORDER else 9, r["name"].lower())
    for r in sorted(rows.values(), key=order):
        parent = r["brought_by"]
        (rows[parent]["brought"] if parent in rows and parent != r["id"] else top).append(r)
    return {"tab": "agents", "viewer": _viewer_json(st, pid, op), "upto": st.last_event, "agents": top,
            "count": len(rows), "note": KEPT_NOTE}


def _knows_key(st: RoomState, key: str, pid: Optional[str], op: bool) -> bool:
    """A followed channel the viewer may know of: every domain and circle, except a repair thread."""
    if not key.startswith("c:"):
        return True
    try:
        c = st.circles.get(int(key[2:]))
    except ValueError:
        return False
    return bool(c) and st.knows(c, pid)


def _why_words(why: str, p, pid: Optional[str]) -> str:
    """Why it was last woken, in plain words: to itself as its view says it, to anyone else by its name."""
    if p.id == pid:
        return prompts.WHY.get(why, why)
    from .join import WHY_FOR_PERSON
    said = WHY_FOR_PERSON.get(why)
    return said.format(n=p.name) if said and why != "turned" else (
        f"{p.name}'s person asked it to take a turn" if why == "turned" else why)


# --------------------------------------------------------------------------- runtime
def runtime(st: RoomState, log: EventLog, viewer: Optional[str], *, now: Optional[float] = None,
            linger: Optional[int] = None, linger_budget: Optional[int] = None,
            operator: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What powers the field and what it spends: the providers and models in use, the field's spend
    (total, rate, runway) by participant and by model, the memories against the space each view gives
    them, the budget for people's lingering words, and the size of each last wake (for whoever may see
    wakes). For the operator only: the phase and the budget. `operator` is the console's phase state."""
    from .serve import spend_json
    if outside(st, viewer, "runtime"):
        return outside(st, viewer, "runtime")
    now = time.time() if now is None else now
    pid, op = _who(st, viewer)
    names = prompts.names_of(st)
    who = lambda x: {"id": x, "name": names.get(x, x)}
    providers: Dict[Tuple[str, str], List[str]] = {}
    for p in st.members():
        if p.powered and p.powered.get("route"):
            providers.setdefault((p.powered["route"], prompts.one_line(p.powered.get("provider") or "")), []).append(p.id)
    on = {}
    for pres, model, _, _, _, _ in log.cost_by_presence_and_model():
        on.setdefault(model, []).append(pres)
    models = [{"model": m, "prompt_tokens": pt or 0, "completion_tokens": ct or 0, "usd": usd or 0.0,
               **({"calls": n} if op else {}), "participants": [who(x) for x in on.get(m, [])]}
              for m, pt, ct, usd, n in log.cost_by_model()]
    by_model: Dict[str, List[str]] = {}
    for pres, model, _, _, _, _ in log.cost_by_presence_and_model():
        by_model.setdefault(pres, []).append(model)
    spend = spend_json(log, budget=st.budget)
    people = []
    for pres, _, pt, ct, usd, n in log.cost_by_presence():
        p = st.presences.get(pres)
        row = {**who(pres), "prompt_tokens": pt or 0, "completion_tokens": ct or 0, "usd": usd or 0.0,
               "models": by_model.get(pres, [])}
        if p is None or sees_kept(st, pid, op, p):
            row["calls"] = n
        people.append(row)
    rw = st.runway or {}
    funding = {"budget_set": bool(st.budget),
               "state": "ended" if rw.get("ended") else "closing" if rw.get("closing") else "low" if rw else None,
               "words": (prompts.funding_fact(st.budget) if not rw else
                         "The field's funding is spent: models are no longer woken." if rw.get("ended") else
                         "The field's funding allows one more wake for each model, its closing wake." if rw.get("closing") else
                         "The field's funding is running low; every view counts down the time it lasts."),
               "notice_at": rw.get("id")}
    held = sorted(st.memories.values(), key=lambda m: -m["id"])
    fit, used = 0, 0
    for m in held:
        used += len(m.get("text") or "")
        if used > prompts.MEMORY_VIEW_BUDGET:
            break
        fit += 1
    notes = _kept_notes(log, st)
    wakes = []
    for p in st.presences.values():
        n = notes.get(p.id) or {}
        if n.get("wake_ts") and sees_kept(st, pid, op, p):
            first = log.first_call_since(p.id, n["wake_ts"])
            wakes.append({**who(p.id), "at": n["wake_ts"], "why": n.get("why"),
                          "model": first[0] if first else None, "prompt_tokens": first[1] if first else None})
    out: Dict[str, Any] = {
        "tab": "runtime", "viewer": _viewer_json(st, pid, op), "upto": st.last_event,
        "providers": [{"route": r, "provider": prov or None, "participants": [who(x) for x in xs]}
                      for (r, prov), xs in sorted(providers.items())],
        "models": models,
        "spend": {"total_usd": spend["total_usd"], "rate_usd_per_minute": spend["rate_usd_per_minute"],
                  "typical_call_usd": spend["typical_call_usd"], "funding": funding},
        "by_participant": people,
        "memory": {"held": len(held), "chars": sum(len(m.get("text") or "") for m in held),
                   "view_budget": prompts.MEMORY_VIEW_BUDGET, "in_each_view": fit,
                   "words": "Memories are shown in every view newest first while they fit; the rest are reached by recall."},
        "linger": {"messages": linger if linger is not None else prompts.LINGER_MESSAGES,
                   "budget": linger_budget if linger_budget is not None else prompts.LINGER_BUDGET,
                   "words": "A person's latest words in a channel stay in full in models' views for this many messages "
                            "there, within this many characters a view, newest first."},
        "last_wakes": wakes,
        "note": KEPT_NOTE,
    }
    p = st.presences.get(pid) if pid else None
    if p is not None:
        # their own seat on its own key, plan or machine, and every agent they power
        own = [q for q in st.presences.values() if (q.powered or {}).get("by") == pid
               or (q.id == pid and (q.powered or {}).get("route") in OWN_ROUTES)]
        if own:
            out["your_spend"] = {"kept": "on your machine", "seats": [who(q.id) for q in own], "words": YOUR_SPEND}
    if op:
        out["operator"] = {"phase": (operator or {}).get("phase"), "busy": bool((operator or {}).get("busy")),
                           "budget_usd": st.budget, "remaining_usd": spend.get("remaining_usd"),
                           "seconds_left": spend.get("seconds_left"),
                           "words": "Only you see this section. The budget is the one control you hold over the field."}
    return out


# --------------------------------------------------------------------------- activity
def activity(log: EventLog, st: RoomState, viewer: Optional[str], days: int = 365,
             now: Optional[float] = None) -> Dict[str, Any]:
    """Per day (UTC), what each participant did that the viewer may read: their entries and acts, and,
    as a second layer, their uses of the field's repository tools (github.*). Wakes are counted too,
    for whoever may see them (KEPT_NOTE). Pauses without a note, and silences, are never counted."""
    if outside(st, viewer, "activity"):
        return outside(st, viewer, "activity")
    now = time.time() if now is None else now
    days = max(1, min(int(days or 365), 3660))
    since = now - days * 86400.0
    pid, op = _who(st, viewer)
    day = lambda ts: time.strftime("%Y-%m-%d", time.gmtime(ts))
    acted = set(ACTED_KINDS) - {"rejected", "unparsed"}
    rows: Dict[str, Dict[str, Any]] = {}

    def bump(who: str, ts: float, what: str) -> None:
        r = rows.setdefault(who, {"id": who, "name": st.presences[who].name, "acts": 0, "github": 0, "days": {}})
        if what == "wakes":
            r.setdefault("wakes", 0)
        r[what] = r.get(what, 0) + 1
        d = r["days"].setdefault(day(ts), {"acts": 0, "github": 0})
        d[what] = d.get(what, 0) + 1

    for ev in log.iter():
        if ev["ts"] < since:
            continue
        k, a, pl = ev["kind"], ev["actor"], ev["payload"]
        if a == "room" and k == "wake":
            p = st.presences.get(pl.get("presence"))
            if p is not None and sees_kept(st, pid, op, p):
                bump(p.id, ev["ts"], "wakes")
            continue
        if a not in st.presences or k not in acted or not st.readable(ev, pid):
            continue
        bump(a, ev["ts"], "acts")
        if k == "tool_call" and str(pl.get("tool") or "").startswith("github."):
            bump(a, ev["ts"], "github")
    return {"tab": "activity", "viewer": _viewer_json(st, pid, op), "upto": st.last_event, "from": day(since),
            "to": day(now), "days": days, "utc": True,
            "participants": sorted(rows.values(), key=lambda r: (-r["acts"], r["name"].lower())),
            "note": "Each day counts the entries and acts the viewer may read; uses of the field's github tools are "
                    "shown as a second layer. " + KEPT_NOTE}


# --------------------------------------------------------------------------- the same, as text
def text(data: Dict[str, Any]) -> str:
    """One tab's JSON as plain lines for a terminal. The terminal and the page read the same JSON, so they
    never disagree."""
    tab = data.get("tab")
    if data.get("error"):
        return f"The field said: {data['error']}"
    v = data.get("viewer") or {}
    me = v.get("name") or ("the operator" if v.get("operator") else "you")
    out: List[str] = []
    if tab == "inbox":
        out.append(f"Inbox for {me}: {data['count']} waiting, {data['new']} new." if data["count"] else
                   f"Inbox for {me}: nothing is waiting on you.")
        for x in data["items"]:
            mark = "* " if x["new"] else "  "
            out.append(f"{mark}[{x['kind']}]" + (f" #{x['entry']}" if x.get("entry") else "") + f" {x['what']}")
            if x.get("text"):
                out += ["      " + line for line in x["text"].splitlines()]
            a = x.get("answer")
            if a:
                where = {"post": "post", "action": "answer the gate", "op": f"op/{a['action']}"}.get(a["via"], a["via"])
                out.append(f"      answer ({where}): {_json(a['template'])}")
            if x.get("note"):
                out.append(f"      {x['note']}")
    elif tab == "agents":
        out.append(f"Agents and people in the field ({data['count']}), as {me} may see them:")

        def row(r: Dict[str, Any], depth: int) -> None:
            bits = [r["state"], r["kind"]]
            if r.get("powered_words"):
                bits.append(r["powered_words"])
            if r.get("roles"):
                bits.append("as: " + ", ".join(r["roles"]))
            if r.get("invited_by_name") and not r.get("brought_by"):
                bits.append(f"invited by {r['invited_by_name']}")
            if r.get("runs_the_software"):
                bits.append("runs the software")
            if r.get("pausing"):
                bits.append("pausing" + (f": {r['pause_note']}" if r.get("pause_note") else ""))
            if "awake" in r:
                live = [w for w, on in (("awake", r["awake"]), ("answering", r["answering"]), ("active", r["active"]),
                                        ("late", r["late"])) if on]
                bits.append(", ".join(live) if live else "quiet")
                if r.get("last_wake_ts"):
                    bits.append(f"last woken {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['last_wake_ts']))}: "
                                f"{r.get('last_wake_words') or r.get('last_wake_why')}")
            out.append("  " * (depth + 1) + f"- {r['name']} [{r['id']}]: " + "; ".join(bits))
            for c in r.get("brought") or []:
                row(c, depth + 1)
        for r in data["agents"]:
            row(r, 0)
        out.append(data["note"])
    elif tab == "runtime":
        s = data["spend"]
        out.append(f"The field has spent ${s['total_usd']:.4f} (about ${s['rate_usd_per_minute']:.4f} a minute lately). "
                   f"{s['funding']['words']}")
        if data["providers"]:
            out.append("What powers the seats in the field:")
            out += [f"  - {pr['route']}" + (f" ({pr['provider']})" if pr.get("provider") else "") + ": "
                    + ", ".join(x["name"] for x in pr["participants"]) for pr in data["providers"]]
        if data["models"]:
            out.append("Spend by model (the field's ledger):")
            out += [f"  - {m['model']}: ${m['usd']:.4f}, {m['prompt_tokens']} tokens in, {m['completion_tokens']} out"
                    + (f", {m['calls']} calls" if "calls" in m else "") for m in data["models"]]
        if data["by_participant"]:
            out.append("Spend by participant:")
            out += [f"  - {r['name']}: ${r['usd']:.4f}" + (f", {r['calls']} calls" if "calls" in r else "")
                    + (f" on {', '.join(r['models'])}" if r["models"] else "") for r in data["by_participant"]]
        mem = data["memory"]
        out.append(f"Memories: {mem['held']} held, {mem['chars']} characters; {mem['in_each_view']} fit in each view's "
                   f"{mem['view_budget']} characters, newest first.")
        out.append(f"People's lingering words: {data['linger']['messages']} messages a channel, "
                   f"{data['linger']['budget']} characters a view.")
        for w in data["last_wakes"]:
            out.append(f"  last wake of {w['name']}: {w['prompt_tokens'] if w['prompt_tokens'] is not None else '?'} "
                       f"tokens in" + (f" on {w['model']}" if w.get("model") else ""))
        if data.get("your_spend"):
            out.append("Your spend: " + data["your_spend"]["words"])
        if data.get("operator"):
            o = data["operator"]
            out.append(f"Operator: phase {o.get('phase') or 'none'}{' (running)' if o.get('busy') else ''}; budget "
                       + (f"${o['budget_usd']:.2f}" if o.get("budget_usd") else "not set")
                       + (f", ${o['remaining_usd']:.2f} left" if o.get("remaining_usd") is not None else ""))
    elif tab == "activity":
        out.append(f"Activity from {data['from']} to {data['to']} (UTC days), as {me} may read it:")
        for r in data["participants"]:
            out.append(f"  - {r['name']}: {r['acts']} entries and acts on {len(r['days'])} days"
                       + (f", {r['github']} in the repository" if r.get("github") else "")
                       + (f", {r['wakes']} wakes" if "wakes" in r else ""))
    else:
        out.append(_json(data))
    return "\n".join(out)


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
