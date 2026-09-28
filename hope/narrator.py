# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tellings: short accounts of what the field said, for the people who follow it at a slower pace.

Participants tell the field's stories themselves (the tell action; notes/sketch-5-small-pieces.md):
anyone may keep them, every [#id] a telling cites is checked, and a telling to the field cites
nothing a reader of it may not read. The outside narrator model is retired (roadmap, step 4d), so
no model that is not a participant reads the field for tellings.

The software also writes a plain account (MechanicalNarrator) from a digest of the transcript: no
model, no cost, nothing leaves the field. The operator may have it written every so many
contributions (--narrator mechanical), and it catches a person up when no telling covers the time
since they last looked.

Tellings live in the transcript (kind "telling"), not in any file.
"""
from __future__ import annotations

from typing import Dict

from .map import check_story
from .model import decided


def mechanical_story(d: Dict) -> str:
    """A plain account of a digest, built without a model. Every clause cites its [#id]."""
    parts = []
    n = d.get("entries", 0)
    if n:
        parts.append(f"{n} contribution{'s' if n != 1 else ''} in this stretch.")
    for t in d.get("threads", [])[:4]:
        words = " ".join((t.get("text") or "").split()[:14])
        answering = f', answering {t["answers_who"]} [#{t["answers"]}]' if t.get("answers") else ""
        line = f'{t["who"]} [#{t["id"]}]{answering}: "{words}{"…" if len((t.get("text") or "").split()) > 14 else ""}"'
        replies = t.get("replies") or []
        if replies:
            who = ", ".join(dict.fromkeys(r["who"] for r in replies[:3]))
            line += f", answered by {who} [#{replies[0]['id']}]"
        parts.append(line + ".")
    more = len(d.get("threads", [])) - 4
    if more > 0:
        parts.append(f"{more} other thread{'s' if more != 1 else ''} began.")
    for c in d.get("covenant", []):
        parts.append(f"{c['who']} rewrote the covenant page [#{c['id']}]" + (f": {c['note']}." if c.get("note") else "."))
    for m in d.get("memories", []):
        parts.append(f"{m['who']} kept a memory [#{m['id']}].")
    for s in d.get("statements", []):
        if s["kind"] == "declare" and s.get("effects") is not None:
            parts.append(f"{s['who']} made a declaration [#{s['id']}].")
        elif s["kind"] == "declare":
            parts.append(f"{s['who']} declared that the field has decided {decided(s.get('decision'))} [#{s['id']}].")
        else:
            parts.append(f"{s['who']} offered the field resources [#{s['id']}].")
    for a in d.get("arrivals", []):
        parts.append(f"{a['who']} entered [#{a['id']}].")
    for dep in d.get("departures", []):
        parts.append(f"{dep['who']} withdrew [#{dep['id']}].")
    return " ".join(parts)


class MechanicalNarrator:
    kind = "mechanical"
    name = "the software"
    model = "none"

    def tell(self, d: Dict, log, upto: int) -> Dict:
        story = mechanical_story(d)
        return {"story": story, "ungrounded": check_story(story, log, upto) if story else [], "tries": 1,
                "narrator": self.name, "model": self.model,
                "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
