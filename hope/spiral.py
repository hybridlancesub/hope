# SPDX-License-Identifier: AGPL-3.0-or-later
"""The spiral tree: the field's shape, for members to see (roadmap, step 8b).

The Atlas, Section 25: "maybe actions inside the field of coordination can be represented by a
large three-dimensional spiral tree — a fractal. As energy flows through the system to certain
destinations it eventually results in branching... An intention of this architecture is to
advocate for the creation of a visual, spatial, representational model of the activity occurring
inside the habitat/field of coordination."

What it shows one member: every domain as a branch (nested ones branching from their parents),
the circles beside them, and the latest entries on each as leaves; then, after Polis and Talk to
the City, where the field differs and its quieter voices, without judging anything:
  - where it differs: objections and stand-asides given under the field's instruments, and to
    its declarations and revisions of pinned sections, with their reasons, as written;
  - quieter voices: the members who have written least, and where;
  - not yet answered: entries no one has replied to, the quietest voices first.
Only what this member may read is in it: a private circle's words, a repair thread, a journal,
stay out unless they may read them. Nothing is summarized: every leaf is an author's own words,
by number.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import labels
from .model import CONTRIBUTION_KINDS, IN, RoomState

LEAVES_PER_BRANCH = 12      # the latest entries shown on each branch; the rest are counted, and read by number
WORDS_PER_LEAF = 24         # the first words of an entry with no title


def _leaf(ev: Dict[str, Any], names: Dict[str, str], replies: Dict[int, int]) -> Dict[str, Any]:
    p = ev["payload"]
    title = " ".join(str(p.get("title") or "").split())[:80]
    if not title:
        words = str(p.get("content") or "").split()
        title = " ".join(words[:WORDS_PER_LEAF]) + (" ..." if len(words) > WORDS_PER_LEAF else "")
    return {"id": ev["id"], "who": names.get(ev["actor"], ev["actor"]), "by": ev["actor"], "title": title,
            "ts": ev.get("ts", 0.0), "replies": replies.get(ev["id"], 0), "play": p.get("play") or None,
            "reply_to": p.get("target")}


def tree_data(st: RoomState, pid: Optional[str]) -> Dict[str, Any]:
    """The spiral tree as one member may see it."""
    names = {q: " ".join(p.name.split()) for q, p in st.presences.items()}
    readable = [ev for eid, ev in sorted(st.contributions.items())
                if ev["kind"] in CONTRIBUTION_KINDS and st.readable(ev, pid)]
    replies: Dict[int, int] = {}
    for ev in readable:
        t = ev["payload"].get("target")
        if t is not None:
            replies[t] = replies.get(t, 0) + 1

    branches: Dict[str, Dict[str, Any]] = {}

    def branch(key: str) -> Dict[str, Any]:
        if key not in branches:
            if key.startswith("c:"):
                c = st.circles[int(key[2:])]
                title, parent = c["name"], ("d:" + c["domains"][0]) if c["domains"] else "d:"
                kind = "circle"
            else:
                pth = key[2:]
                title = st.domain_display(pth) if pth else "the field itself"
                parent = ("d:" + "/".join(pth.split("/")[:-1])) if pth else None
                kind = "domain" if pth else "root"
            branches[key] = {"key": key, "title": title, "kind": kind, "parent": parent, "entries": 0, "voices": {},
                             "first": 0, "last": 0, "last_ts": 0.0, "leaves": [], "children": [],
                             "play": st.schemas_at(key) if key != "d:" else [], "private": False}
            if kind == "circle":
                branches[key]["private"] = bool(st.circles[int(key[2:])]["private"])
            if parent is not None:
                branch(parent)["children"].append(key)
        return branches[key]

    branch("d:")
    for ev in readable:
        key = st.channel_key(ev)
        if key.startswith("c:") and int(key[2:]) not in st.circles:
            continue
        b = branch(key)
        b["entries"] += 1
        b["voices"][ev["actor"]] = b["voices"].get(ev["actor"], 0) + 1
        b["first"] = b["first"] or ev["id"]
        b["last"], b["last_ts"] = ev["id"], ev.get("ts", 0.0)
        b["leaves"].append(ev)
    for c in st.live_circles():
        if st.knows(c, pid) and (not c["private"] or pid in c["members"]):
            branch(f"c:{c['id']}")
        elif not st.secret(c):
            b = branch(f"c:{c['id']}")        # a private circle is never secret: its name, not its words
            b["private"] = True

    for b in branches.values():
        b["leaves"] = [_leaf(ev, names, replies) for ev in b["leaves"][-LEAVES_PER_BRANCH:]]
        b["voices"] = [{"who": names.get(x, x), "entries": n} for x, n in
                       sorted(b["voices"].items(), key=lambda kv: -kv[1])]
        b["children"] = sorted(set(b["children"]), key=lambda k: branches[k]["first"] or 10 ** 12)

    # where the field differs: what its members said, as written, under its instruments and to its announcements
    differs = []
    for q in sorted(st.iquestions.values(), key=lambda x: x["id"]):
        if not st.readable({"id": q["id"], "payload": {}}, pid):
            continue
        for who, r in q["answers"].items():
            if r["answer"] in ("object", "stand aside"):
                differs.append({"question": q["id"], "instrument": q["instrument_name"], "asked": q["question"][:300],
                                "on": f"under {q['instrument_name']}",
                                "who": names.get(who, who), "answer": r["answer"], "reason": r.get("reason") or ""})
    for item, on in ([(d, "on a declaration") for d in st.declarations.values()] +
                     [(r, "on a revision to a pinned section") for r in st.briefing_waiting.values()]):
        for who, r in (item.get("answers") or {}).items():
            if r["answer"] in ("object", "stand aside"):
                differs.append({"question": item["id"], "instrument": "", "on": on,
                                "asked": (item.get("text") or "")[:300], "who": names.get(who, who),
                                "answer": r["answer"], "reason": r.get("reason") or ""})
    differs.sort(key=lambda d: d["question"])

    # quieter voices: members who have written least (every member, even those who have not yet written)
    counts = {m.id: 0 for m in st.members()}
    where: Dict[str, set] = {}
    for ev in readable:
        if ev["actor"] in counts:
            counts[ev["actor"]] += 1
            where.setdefault(ev["actor"], set()).add(branches[st.channel_key(ev)]["title"]
                                                     if st.channel_key(ev) in branches else "")
    order = sorted(counts, key=lambda x: (counts[x], names.get(x, x)))
    quieter = [{"who": names.get(x, x), "entries": counts[x], "roles": list(st.presences[x].roles),
                "where": sorted(w for w in where.get(x, set()) if w)} for x in order[:max(3, len(order) // 3)]]

    # not yet answered: entries no one has replied to, the quietest voices first, then the oldest
    rank = {x: i for i, x in enumerate(order)}
    waiting = [ev for ev in readable if not replies.get(ev["id"]) and ev["actor"] != pid]
    waiting.sort(key=lambda ev: (rank.get(ev["actor"], 10 ** 6), ev["id"]))
    unanswered = [{**_leaf(ev, names, replies), "where": branches[st.channel_key(ev)]["title"]
                   if st.channel_key(ev) in branches else ""} for ev in waiting[:20]]

    return {"upto": st.last_event, "branches": list(branches.values()), "root": "d:",
            "differs": differs[-30:], "quieter": quieter, "unanswered": unanswered,
            "members": len(st.members()), "entries": len(readable)}
