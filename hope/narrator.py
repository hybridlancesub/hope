# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tellings: short accounts of what the field said, for the people who follow it at a slower pace.

A telling covers one stretch of the transcript: everything since the last telling. Every event
it mentions carries its [#id], and every tag is checked against the transcript before the
telling is kept; a narrator that cites something that does not exist gets one correction pass,
and whatever is still ungrounded is reported beside the telling, never hidden.

Two narrators:
  ModelNarrator       a model reads the stretch and writes the account. Participants' words go to
                      the service that runs that model, so the entry question says so whenever a
                      model narrator is on (prompts.narrator_fact).
  MechanicalNarrator  the software writes a plain account from the same digest. No model, no cost,
                      nothing leaves the field. Used for demos, and for catching a person up when no
                      telling covers the time since their last turn.

Tellings live in the transcript (kind "telling"), not in any file. Members who take a turn every
round do not see them; people at the slower pace get the ones written since their last turn.
"""
from __future__ import annotations

from typing import Dict

from .map import TAG_RE, check_story, digest_text
from .model import decided

TELL_SYSTEM = """You are the field's narrator. You will receive a digest of what a coordination field said and did since the last telling: who spoke, what they said (with event ids), the threads, what was remembered, how the covenant page changed, what was put before the operator, who arrived or left.

Write a short account of it for a person who follows the field at a slower pace and has not read the transcript. Rules:
- Every event you refer to MUST carry its tag, exactly like [#142]. Tags are how a reader checks you against the transcript.
- Invent nothing: no speech, no motive, no event that is not in the digest. Choose your words freely; do not choose facts.
- Name participants as the digest names them.
- Plain prose, under about 150 words: what moved, who answered whom, what changed on the covenant page, and anything put before the operator."""


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
        if s["kind"] == "declare":
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


class ModelNarrator:
    kind = "model"

    def __init__(self, connector, seat):
        self.connector, self.seat = connector, seat
        self.name, self.model = seat.name, seat.model
        self.provider = getattr(connector, "provider_name", None)       # named at entry, with the model
        self.through = getattr(connector, "provider_label", None) or self.provider

    def tell(self, d: Dict, log, upto: int) -> Dict:
        """One model call, plus at most one correction pass. Both calls are counted."""
        msgs = [{"role": "user", "content": digest_text(d) + "\n\nTell what happened in this stretch."}]
        spent = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

        def ask():
            saved = getattr(self.connector, "json_mode", True)
            self.connector.json_mode = False        # a telling is prose, not an action
            try:
                reply = self.connector.ask(self.seat, TELL_SYSTEM, msgs)
            finally:
                self.connector.json_mode = saved
            spent["prompt_tokens"] += reply.prompt_tokens
            spent["completion_tokens"] += reply.completion_tokens
            spent["cost_usd"] += reply.cost_usd
            text = (reply.text or "").strip()
            return text if TAG_RE.search(text) else ""   # a telling that cites nothing cannot be checked

        story = ask()
        bad = check_story(story, log, upto) if story else []
        tries = 1
        if bad or not story:
            msgs += [{"role": "assistant", "content": story or "(no usable telling was produced)"},
                     {"role": "user", "content": ("These tags do not exist in the transcript: " + str(bad) + ". " if bad else "")
                      + "Tell it again as prose, citing only real event ids as [#id]; where you cannot cite, do not claim."}]
            story = ask()
            bad = check_story(story, log, upto) if story else []
            tries = 2
        return {"story": story, "ungrounded": bad, "tries": tries, "narrator": self.name, "model": self.model, **spent}
