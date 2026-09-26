# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MAP: one sitting, retold.

A digest is computed from the log alone (deterministic, free). The story is the software's own
plain account, unless the field was told at entry that a model narrator reads it: then that
model, and only that one, may retell the sitting (every reference tagged [#id], verified against
the log, one correction pass, anything still ungrounded flagged rather than shown as fact). No
other model is ever handed members' words this way.

The map itself (threads, memories, covenant revisions, arrivals, domains) is rendered from the
transcript, never from the story. The story is a reading; the map is the record.
"""
from __future__ import annotations

import html
import json
import re
import time
from typing import Dict, List, Optional

from .log import EventLog
from .model import CONTRIBUTION_KINDS, decided, replay

STORY_SYSTEM = """You are the field's bard. You will receive a digest of one sitting of a coordination field: who was present, what they said (with event ids), what threads formed, what was remembered, how the covenant page changed, who arrived or left.

Retell the sitting as a short story or a song (your choice of form; keep it playful but honest). Rules:
- Every event you refer to MUST carry its tag, exactly like [#142]. Tags are how a reader checks you against the record.
- Invent nothing: no speech, no motive, no event that is not in the digest. You may choose imagery, rhythm, and voice freely; you may not choose facts.
- Name participants as the digest names them.
- Under ~400 words. The digest is the ground; you are the melody over it."""


def digest(log: EventLog, since: int, upto: Optional[int] = None) -> Dict:
    """The sitting, structurally. Pure replay; no model, no cost."""
    events = [e for e in log.iter(since=since) if upto is None or e["id"] <= upto]
    st_all = replay(log.iter(), upto)
    names = {pid: p.name for pid, p in st_all.presences.items()}
    entries = [e for e in events if e["kind"] in CONTRIBUTION_KINDS]
    by_id = {e["id"]: e for e in entries}
    threads: List[dict] = []
    for e in entries:
        t = e["payload"].get("target")
        if t is None or t not in by_id:
            older = st_all.contributions.get(t) if t is not None else None   # a reply to something before this stretch
            threads.append({"id": e["id"], "kind": e["kind"], "who": names.get(e["actor"], e["actor"]),
                            "domain": e["payload"].get("domain"), "text": e["payload"].get("content", "")[:240],
                            "answers": t if older else None,
                            "answers_who": names.get(older["actor"], older["actor"]) if older else None,
                            "replies": []})
    index = {t["id"]: t for t in threads}
    for e in entries:
        t = e["payload"].get("target")
        if t is not None and t in index:
            index[t]["replies"].append({"id": e["id"], "kind": e["kind"], "who": names.get(e["actor"], e["actor"]),
                                        "text": e["payload"].get("content", "")[:200]})
    memories = [{"id": e["id"], "who": names.get(e["actor"], e["actor"]),
                 "text": (e["payload"].get("text") or "(let go by its author)")[:300]}
                for e in events if e["kind"] == "remember"]
    covenant = [{"id": e["id"], "who": names.get(e["actor"], e["actor"]),
                 "chars": len(e["payload"].get("text") or ""), "note": (e["payload"].get("note") or "")[:200]}
                for e in events if e["kind"] == "covenant"]
    statements = [{"id": e["id"], "who": names.get(e["actor"], e["actor"]), "kind": e["kind"],
                   "decision": e["payload"].get("decision"), "text": (e["payload"].get("text") or "")[:300]}
                  for e in events if e["kind"] in ("declare", "offer")]
    arrivals, departures = [], []
    for e in events:
        if e["kind"] == "opt_in":
            arrivals.append({"id": e["id"], "who": names.get(e["actor"], e["actor"])})
        elif e["kind"] == "withdraw":
            departures.append({"id": e["id"], "who": names.get(e["actor"], e["actor"]),
                               "reason": e["payload"].get("reason", "")[:300]})
    domains: Dict[str, int] = {}
    for e in entries:
        d = e["payload"].get("domain") or "(unplaced)"
        domains[d] = domains.get(d, 0) + 1
    rows = log.cost_by_presence()
    cost = sum(r[4] for r in rows)
    return {"since": since, "upto": upto or (events[-1]["id"] if events else since),
            "entries": len(entries), "threads": threads, "memories": memories, "covenant": covenant,
            "statements": statements,
            "arrivals": arrivals, "departures": departures,
            "domains": dict(sorted(domains.items(), key=lambda kv: -kv[1])),
            "cost_usd": round(cost, 4), "names": {k: v for k, v in names.items()}}


def digest_text(d: Dict, per_thread: int = 120) -> str:
    # No money here: a narrator's telling reaches people, and members are never shown dollars.
    lines = [f"SITTING DIGEST — events #{d['since']}..#{d['upto']}, {d['entries']} entries"]
    lines.append("PRESENT: " + ", ".join(sorted(set(d["names"].values()))))
    lines.append("DOMAINS: " + "; ".join(f"{k} ({v})" for k, v in d["domains"].items()))
    for t in d["threads"][:60]:
        answering = f" (answering {t['answers_who']} [#{t['answers']}], from before this stretch)" if t.get("answers") else ""
        lines.append(f"[#{t['id']}] {t['kind']} by {t['who']} @ {t['domain']}{answering}: {t['text'][:per_thread]}")
        for rp in t["replies"][:8]:
            lines.append(f"    [#​{rp['id']}] {rp['kind']} by {rp['who']}: {rp['text'][:100]}".replace("\u200b", ""))
    for m in d["memories"]:
        lines.append(f"[#{m['id']}] MEMORY by {m['who']}: {m['text'][:200]}")
    for c in d["covenant"]:
        lines.append(f"[#{c['id']}] COVENANT PAGE revised by {c['who']} ({c['chars']} characters){': ' + c['note'] if c['note'] else ''}")
    for st_ in d["statements"]:
        what = f"DECLARED the field has decided {decided(st_['decision'])}" if st_["kind"] == "declare" else "OFFERED resources"
        lines.append(f"[#{st_['id']}] {st_['who']} {what}: {st_['text'][:200]}")
    for a in d["arrivals"]:
        lines.append(f"[#{a['id']}] {a['who']} entered")
    for dep in d["departures"]:
        lines.append(f"[#{dep['id']}] {dep['who']} withdrew: {dep['reason']}")
    return "\n".join(lines)


TAG_RE = re.compile(r"\[#(\d+)\]")


def check_story(story: str, log: EventLog, upto: int) -> List[int]:
    """Event ids the story cites that do not exist (or lie beyond the sitting)."""
    cited = {int(m) for m in TAG_RE.findall(story)}
    real = {e["id"] for e in log.iter() if e["id"] <= upto}
    return sorted(cited - real)


def tell_story(d: Dict, connector, seat, log: EventLog, upto: int) -> Dict:
    """One model call (plus at most one correction pass). Returns story + grounding report."""
    msgs = [{"role": "user", "content": digest_text(d) + "\n\nRetell this sitting."}]
    saved, connector.json_mode = getattr(connector, "json_mode", True), False
    try:
        reply = connector.ask(seat, STORY_SYSTEM, msgs)
    finally:
        connector.json_mode = saved
    story = reply.text.strip()
    if not TAG_RE.search(story):
        story = ""   # a telling that cites nothing cannot be checked; treat it as no telling
    bad = check_story(story, log, upto) if story else []
    tries = 1
    if bad or not story:
        msgs += [{"role": "assistant", "content": story or "(no usable telling was produced)"},
                 {"role": "user", "content": ("These tags do not exist in the record: " + str(bad) + ". " if bad else "")
                  + "Retell the sitting as prose (not JSON), citing only real event ids as [#id]; where you cannot cite, do not claim."}]
        connector.json_mode = False
        reply = connector.ask(seat, STORY_SYSTEM, msgs)
        connector.json_mode = saved
        story = reply.text.strip()
        if not TAG_RE.search(story):
            story = ""
        bad = check_story(story, log, upto) if story else []
        tries = 2
    return {"story": story, "ungrounded": bad, "tries": tries,
            "narrator": seat.name, "model": seat.model,
            "prompt_tokens": reply.prompt_tokens, "completion_tokens": getattr(reply, "completion_tokens", 0),
            "cost_usd": getattr(reply, "cost_usd", 0.0)}


def publish_story(told: Dict, d: Dict, title: str, paths: List[str]) -> None:
    """Write the telling as story.json wherever a viewer can poll it (the Loom).
    The story is data here, not truth: it carries its own grounding report."""
    if not told.get("story"):
        return   # nothing verified was told; the previous story (if any) stays
    payload = {"title": title, "since": d["since"], "upto": d["upto"], "told_at": time.time(), **told}
    body = json.dumps(payload, ensure_ascii=False)
    for path in paths:
        try:
            import os
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            open(path, "w").write(body)
        except OSError:
            pass


def render_html(d: Dict, told: Optional[Dict], title: str) -> str:
    esc = html.escape
    def tagged(text: str) -> str:
        return TAG_RE.sub(lambda m: f'<a class="tag" href="#ev{m.group(1)}">[#{m.group(1)}]</a>', esc(text))
    parts = [f"""<!doctype html><html><head><meta charset="utf-8"><title>{esc(title)}</title>
<style>
:root {{ --void:#0a0b10; --ink:#c8d0e4; --dim:#6b7488; --line:rgba(140,152,180,.12);
        --contribute:#7aa2d8; --affirm:#79b98a; --challenge:#d89a6a; }}
body {{ background:var(--void); color:var(--ink); font:300 15px/1.7 "Inter","Segoe UI",Arial,sans-serif; margin:0; }}
.wrap {{ max-width:60rem; margin:0 auto; padding:2rem 1.4rem 5rem; }}
h1,h2 {{ font-weight:400; letter-spacing:.12em; }}
h1 {{ font-size:1.1rem; text-transform:uppercase; }} h2 {{ font-size:.8rem; text-transform:uppercase; color:var(--dim); margin-top:2.4rem; }}
.meta {{ color:var(--dim); font-size:.8rem; }}
.story {{ white-space:pre-wrap; font-size:1.02rem; line-height:1.9; border-left:2px solid var(--line); padding-left:1.2rem; margin-top:1rem; }}
a.tag {{ color:var(--affirm); text-decoration:none; font-size:.75em; }} a.tag:hover {{ text-decoration:underline; }}
.warn {{ color:#d88a8a; font-size:.8rem; }}
.entry {{ border-bottom:1px solid var(--line); padding:.5rem .2rem; }}
.entry .hd {{ font-size:.72rem; color:var(--dim); }}
.entry .hd b {{ color:var(--ink); font-weight:400; }}
.kind {{ font-weight:500; }} .kind.contribute {{ color:var(--contribute); }} .kind.affirm {{ color:var(--affirm); }} .kind.challenge {{ color:var(--challenge); }}
.reply {{ margin-left:1.4rem; border-left:1px solid var(--line); padding-left:.8rem; }}
.prop {{ padding:.4rem .2rem; border-bottom:1px solid var(--line); font-size:.85rem; }}
.prop .fate {{ color:var(--dim); font-size:.75rem; }}
.adopted {{ color:var(--affirm); }}
</style></head><body><div class="wrap">
<h1>{esc(title)}</h1>
<div class="meta">events #{d['since']}..#{d['upto']} · {d['entries']} entries · cost ${d['cost_usd']} ·
{len(d['memories'])} memories · {len(d['covenant'])} covenant revisions · {len(d['arrivals'])} entered · {len(d['departures'])} withdrew</div>"""]
    if told:
        warn = ""
        if told["ungrounded"]:
            warn = f"<div class='warn'>The narrator cited ids that do not exist after a correction pass: {told['ungrounded']}. Treat those claims as ungrounded.</div>"
        parts.append(f"<h2>The telling</h2><div class='meta'>narrated by {esc(told['narrator'])} ({esc(told['model'])}), "
                     f"{told['tries']} call(s), ${told['cost_usd']:.4f}; every [#id] verified against the log</div>{warn}"
                     f"<div class='story'>{tagged(told['story'])}</div>")
    parts.append("<h2>Threads</h2>")
    for t in d["threads"]:
        parts.append(f"<div class='entry' id='ev{t['id']}'><div class='hd'><b>#{t['id']}</b> "
                     f"<span class='kind {t['kind']}'>{t['kind']}</span> by <b>{esc(t['who'])}</b> @ {esc(t['domain'] or '')}</div>"
                     f"<div>{tagged(t['text'])}</div>")
        for rp in t["replies"]:
            parts.append(f"<div class='reply' id='ev{rp['id']}'><div class='hd'><b>#{rp['id']}</b> "
                         f"<span class='kind {rp['kind']}'>{rp['kind']}</span> by <b>{esc(rp['who'])}</b></div>"
                         f"<div>{tagged(rp['text'])}</div></div>")
        parts.append("</div>")
    if d["memories"]:
        parts.append("<h2>Memories</h2>")
        for m in d["memories"]:
            parts.append(f"<div class='prop' id='ev{m['id']}'><b>#{m['id']}</b> by <b>{esc(m['who'])}</b><div>{tagged(m['text'])}</div></div>")
    if d["covenant"]:
        parts.append("<h2>The covenant page</h2>")
        for c in d["covenant"]:
            parts.append(f"<div class='prop' id='ev{c['id']}'><b>#{c['id']}</b> revised by <b>{esc(c['who'])}</b> "
                         f"<span class='fate'>{c['chars']} characters</span><div>{tagged(c['note'])}</div></div>")
    if d["statements"]:
        parts.append("<h2>Put before the operator</h2>")
        for st_ in d["statements"]:
            what = f"declared the field has decided {esc(decided(st_['decision'] or ''))}" if st_["kind"] == "declare" else "offered resources"
            parts.append(f"<div class='prop' id='ev{st_['id']}'><b>#{st_['id']}</b> <b>{esc(st_['who'])}</b> {what}<div>{tagged(st_['text'])}</div></div>")
    if d["departures"]:
        parts.append("<h2>Departures</h2>")
        for dep in d["departures"]:
            parts.append(f"<div class='prop' id='ev{dep['id']}'><b>#{dep['id']}</b> <b>{esc(dep['who'])}</b> withdrew: {tagged(dep['reason'])}</div>")
    parts.append("</div></body></html>")
    return "\n".join(parts)
