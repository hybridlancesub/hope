# SPDX-License-Identifier: AGPL-3.0-or-later
"""A read-only window onto the transcript, for the operator's viewers (see firmament/).

Serves the replayed state as JSON on localhost. It writes nothing, accepts nothing, and is
not a participant-facing surface: participants never see it, and nothing here can reach the
field. It is the operator reading their own field, in a form a renderer can use.

    GET /state.json          domains, participants, contributions (with reply threads), the covenant
                             page and its revisions, memories, declarations and offers, admission
                             (who is at which gate), spend (what it costs, how long it lasts)
    GET /event/<id>.json     one event in full
    GET /record.txt          the whole transcript as plain text, in order, nothing summarized
    GET /                    the viewer, if a directory was given

`proposals`, `halted`, `settings` and `reflections` are still present in state.json, always
empty, so viewers written for earlier versions keep working. The machinery behind them is gone.
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from .labels import canonical
from .log import EventLog
from .model import CONTRIBUTION_KINDS, decided, effects_words, replay

GATE_ORDER = ["INVITED", "ACCEPTED", "BRIEFED", "RECEIVED", "IN", "OUT"]


def admission_json(st) -> List[Dict[str, Any]]:
    """Every presence the field knows of, at whatever gate it has reached -- including the ones
    that never became participants. One row per seat, in the order the gates are walked."""
    rows = []
    for p in st.presences.values():
        rows.append({
            "id": p.id, "name": p.name, "hails_from": p.hails_from, "people": p.people,
            "stage": p.state, "stage_index": GATE_ORDER.index(p.state) if p.state in GATE_ORDER else -1,
            "joined_at": p.joined_at, "left_at": p.left_at, "left_reason": p.left_reason,
            "ask_again": p.ask_again, "self_described": p.self_described, "returning": p.returning,
            "unreachable": p.unreachable, "exhausted": p.exhausted,
            "turns": p.turns, "turn_allowance": p.turn_allowance, "price_per_m": p.price_per_m,
            "resting_until": p.rest_until if st.resting(p) else None,
            # [question, answer|None] -- an unanswered question is someone waiting on the operator
            "questions": [{"asked": q, "answered": a} for q, a in (p.questions or [])],
            "awaiting_answer": any(a is None for _, a in (p.questions or [])),
        })
    rows.sort(key=lambda r: (r["stage_index"], r["name"].lower()))
    return rows


def spend_json(log: EventLog, budget: Optional[float] = None) -> Dict[str, Any]:
    """What the field has cost and, at the rate of the last five minutes, how long the budget lasts.
    Projection is arithmetic on the log, not a promise."""
    by_presence = [{"presence": pres, "model": model, "calls": n,
                    "prompt_tokens": pt, "completion_tokens": ct, "usd": usd}
                   for pres, model, pt, ct, usd, n in log.cost_by_presence()]
    total = log.total_cost()
    typical = log.median_recent_cost()
    now = time.time()
    recent, first = log.spent_since(now - 300)
    rate = recent / max(60.0, now - first) if recent and first is not None else 0.0   # USD a second
    out: Dict[str, Any] = {
        "total_usd": total, "typical_call_usd": typical, "by_presence": by_presence,
        "rate_usd_per_minute": rate * 60, "budget_usd": budget,
    }
    if budget is not None:
        out["remaining_usd"] = budget - total
        out["seconds_left"] = round(max(0.0, budget - total) / rate) if rate > 0 else None
    return out


def record_text(log: EventLog, everything: bool = False) -> str:
    """The transcript as plain text, in order, nothing summarized. The raw stream, for reading.
    Words written in a private circle are never here: the console opens a private circle only on
    purpose (read_circle), and records that in the circle, where its participants see it."""
    st = replay(log.iter())
    names = {pid: p.name for pid, p in st.presences.items()}
    out: List[str] = []
    for ev in log.iter():
        k, p, who = ev["kind"], ev["payload"], names.get(ev["actor"], ev["actor"])
        if ev["id"] in st.scoped:
            if st.scoped[ev["id"]].get("journal") is not None:
                out.append(f"#{ev['id']} (in a participant's journal; not shown here. Opening it from the console is "
                           f"recorded in the journal)\n")
            else:
                out.append(f"#{ev['id']} (written in a private circle; not shown here. Opening it from the console is "
                           f"recorded in the circle)\n")
            continue
        if k in ("connector_ok", "connector_error", "briefed") and not everything:
            continue
        if k in ("brief", "brief_page", "invitation", "documentation", "faq") and not everything:
            out.append(f"#{ev['id']} {k} by {who}: ({len(p.get('text', ''))} chars, omitted; see the source files)\n")
            continue
        if k == "round":          # earlier versions' rounds
            out.append(f"—— round {p.get('n')} ——\n")
            continue
        if k in ("wake", "seen") and not everything:
            continue
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev["ts"]))
        head = f"#{ev['id']} [{t}] {k} by {who}"
        if k in CONTRIBUTION_KINDS:
            tgt = f" -> #{p['target']}" if p.get("target") is not None else ""
            out.append(f"{head}{tgt} @ {p.get('domain')}\n{p.get('content', '')}\n")
        elif k in ("covenant", "covenant_seed"):
            note = f" ({p['note']})" if p.get("note") else ""
            out.append(f"{head}{note}\n--- covenant page ---\n{p.get('text', '') or '(empty)'}\n--- end ---\n")
        elif k == "remember":
            out.append(f"{head}\n{p.get('text') or '(let go by its author; the words were removed)'}\n")
        elif k == "declare":
            refs = f" (cites {', '.join('#' + str(r) for r in p.get('refs') or [])})" if p.get("refs") else ""
            if isinstance(p.get("effects"), dict):
                words = effects_words(p["effects"])
                out.append(f"{head}{refs}: " + (f"the software will {words}" if words else "an announcement") +
                           f"\n{p.get('text', '')}\n")
            else:
                out.append(f"{head}: the field has decided {decided(p.get('decision'))}{refs}\n{p.get('text', '')}\n")
        elif k == "offer":
            out.append(f"{head}\n{p.get('text', '')}\n")
        elif k == "telling":
            flag = f" (ungrounded tags: {p['ungrounded']})" if p.get("ungrounded") else ""
            out.append(f"{head} ({p.get('narrator')}, #{p.get('since')}..#{p.get('upto')}){flag}\n{p.get('story', '')}\n")
        elif k == "propose":   # files from earlier versions only
            out.append(f"{head}: {p.get('kind')} value={p.get('value')!r}\n{p.get('reason', '')}\n")
        elif k == "tool_call":
            where = p.get("domain") or (f"circle #{p['circle']}" if p.get("circle") is not None else "the field")
            out.append(f"{head} @ {where}: {p.get('tool')}, sending {json.dumps(p.get('arguments'), ensure_ascii=False)} "
                       f"(to {p.get('sends_to')})\n")
        elif k == "tool_result":
            text = p.get("text") or ""
            shown = text if everything or len(text) <= 4000 else text[:4000] + f"\n... (the first 4,000 of {len(text):,} characters)"
            out.append(f"{head} @ {p.get('domain')}: what {p.get('tool')} sent back for #{p.get('call')}, from outside "
                       f"the field (run by {p.get('runner')})\n{shown}\n")
        else:
            body = {kk: v for kk, v in p.items() if v not in (None, "", {}, [])}
            out.append(f"{head}: {json.dumps(body, ensure_ascii=False)}\n")
    return "\n".join(out)


def state_json(log: EventLog, budget: Optional[float] = None) -> Dict[str, Any]:
    """Everything a viewer needs, derived only from the transcript. Contributions carry their
    thread (the replies targeting them); cross-domain replies become relationships. Words written
    in a private circle are not here: opening one is done in the console, and recorded there."""
    st = replay(log.iter())
    budget = budget if budget is not None else st.budget
    names = {pid: p.name for pid, p in st.presences.items()}
    contributions: Dict[int, Dict[str, Any]] = {}
    for ev in log.iter():
        if ev["kind"] not in CONTRIBUTION_KINDS or ev["actor"] not in st.presences:
            continue
        if st.presences[ev["actor"]].joined_at is None:
            continue  # recorded but never applied (acted before entry)
        if ev["id"] in st.scoped:
            continue  # written in a private circle
        p = ev["payload"]
        contributions[ev["id"]] = {
            "id": ev["id"], "ts": ev["ts"], "kind": ev["kind"], "actor": ev["actor"],
            "who": names.get(ev["actor"], ev["actor"]),
            "domain": st.relabeled.get(ev["id"]) or p.get("domain") or "(unplaced)",   # where its author filed it
            "title": p.get("title") or "", "content": p.get("content", ""), "target": p.get("target"),
            "set_aside": False, "affirms": 0, "challenges": 0, "responses": 0, "replies": [],
        }
    for c in contributions.values():
        t = c["target"]
        if t is not None and t in contributions:
            tgt = contributions[t]
            tgt["replies"].append(c["id"])
            tgt["responses"] += 1
            if c["kind"] in ("affirm", "challenge"):          # earlier versions' replies
                tgt["affirms" if c["kind"] == "affirm" else "challenges"] += 1
    # Reading-side consolidation of label variants. `domain` becomes the group's most-used
    # spelling; `label_as_written` keeps what the participant actually typed.
    written = [c["domain"] for c in contributions.values()] + [p.domain for p in st.members() if p.domain]
    counts: Dict[str, int] = {}
    for w in written:
        counts[w] = counts.get(w, 0) + 1
    canon = canonical(set(written), counts)
    for c in contributions.values():
        c["label_as_written"] = c["domain"]
        c["domain"] = canon.get(c["domain"], c["domain"])
    domains: Dict[str, Dict[str, Any]] = {}
    for c in contributions.values():
        d = domains.setdefault(c["domain"], {"label": c["domain"], "contributions": 0, "affirms": 0, "challenges": 0,
                                             "first": c["ts"], "last": c["ts"], "present": []})
        d["contributions"] += 1
        d["affirms"] += 1 if c["kind"] == "affirm" else 0
        d["challenges"] += 1 if c["kind"] == "challenge" else 0
        d["last"] = max(d["last"], c["ts"])
    for p in st.members():
        if p.domain:
            d = canon.get(p.domain, p.domain)
            domains.setdefault(d, {"label": d, "contributions": 0, "affirms": 0, "challenges": 0,
                                   "first": None, "last": None, "present": []})["present"].append(p.id)
    for d in domains.values():
        d["spellings"] = sorted({w for w, cn in canon.items() if cn == d["label"]})
    links: Dict[tuple, Dict[str, Any]] = {}
    for c in contributions.values():
        t = c["target"]
        if t is None or t not in contributions:
            continue
        a, b = c["domain"], contributions[t]["domain"]
        if a == b:
            continue
        key = tuple(sorted((a, b)))
        l = links.setdefault(key, {"between": list(key), "count": 0, "affirms": 0, "challenges": 0, "pairs": []})
        l["count"] += 1
        if c["kind"] in ("affirm", "challenge"):
            l["affirms" if c["kind"] == "affirm" else "challenges"] += 1
        l["pairs"].append([c["id"], t])
    members = [{"id": p.id, "name": p.name, "hails_from": p.hails_from, "people": p.people, "state": p.state,
                "domain": p.domain, "joined_at": p.joined_at, "left_at": p.left_at, "left_reason": p.left_reason,
                "turns": p.turns, "turn_allowance": p.turn_allowance, "exhausted": p.exhausted,
                "unreachable": p.unreachable, "self_described": p.self_described,
                "pausing": bool(p.pause), "pause_note": (p.pause or {}).get("note") or None, "roles": list(p.roles),
                "invited_by": names.get(p.invited_by, p.invited_by) if p.invited_by else None,
                "resting_until": None}
               for p in st.presences.values() if p.joined_at is not None]
    covenant = {"text": st.covenant, "by": names.get(st.covenant_by, st.covenant_by) if st.covenant_by else None,
                "at": st.covenant_at,
                "revisions": [{**h, "by": names.get(h["by"], h["by"])} for h in st.covenant_history]}
    memories = [{**m, "who": names.get(m["by"], m["by"])} for m in sorted(st.memories.values(), key=lambda m: m["id"])]
    declarations = [{**d, "who": names.get(d["by"], d["by"]),
                     "decided": (effects_words(d["effects"], names, versions=st.instrument_versions) if "effects" in d
                                 else decided(d["decision"])),
                     "answers": [{"who": names.get(x, x), **r} for x, r in (d.get("answers") or {}).items()]}
                    for d in sorted(st.declarations.values(), key=lambda d: d["id"])]
    bridges = [{**b, "who": names.get(b["by"], b["by"])} for b in sorted(st.bridges.values(), key=lambda b: b["id"])]
    offers = [{**o, "who": names.get(o["by"], o["by"])} for o in sorted(st.offers.values(), key=lambda o: o["id"])]
    return {
        "generated": time.time(), "last_event": st.last_event,
        "wakes": sum(p.turns for p in st.presences.values() if p.last_wake_ts),
        "circles": [{"id": c["id"], "name": c["name"], "private": c["private"], "members": len(c["members"]),
                     "domains": c["domains"], "dispersed": c["dispersed_at"] is not None,
                     "reason": c["reason"] if c["private"] else ""} for c in st.circles.values() if not st.secret(c)],
        # the field's instruments, and the questions under them (settled by their own rule, by the software)
        "instruments": [{"name": i["name"], "status": i["status"], "purpose": i["purpose"], "scope": i["scope"],
                         "pause": i["pause"], "current": i.get("current"), "latest": i.get("latest"),
                         "text": i["text"]} for i in st.instruments.values()],
        "instrument_questions": [{**{k: q[k] for k in ("id", "status", "purpose", "question", "decision", "refs",
                                                       "adopt", "put_down", "circle", "pause", "ts", "instrument_name")},
                                  "who": names.get(q["by"], q["by"]),
                                  "subject": names.get(q.get("subject"), q.get("subject")) if q.get("subject") else None,
                                  "answers": [{"who": names.get(x, x), **r} for x, r in q["answers"].items()]}
                                 for q in st.iquestions.values() if q["id"] not in st.scoped],
        # repair threads, for the operator only: how many people, and where each stands; never who, never words
        "repair_threads": [{"id": c["id"], "people": len(c["members"]), "status": c["repair"]["status"]}
                           for c in st.circles.values() if st.secret(c) and c["dispersed_at"] is None],
        "private_entries": len(st.scoped),
        # the field's tools as every member sees them: who runs each, where what is sent goes, flags, uses
        "tools": [{"server": s["server"], "by": names.get(s["by"], s["by"]), "runner": s["runner"],
                   "sends_to": s["sends_to"], "kind": s["kind"], "price": s["price"], "url": s["url"],
                   "tools": [t["tool"] for t in s["tools"]], "uses": s["uses"], "removed_at": s["removed_at"],
                   "note": s["note"], "flags": [{"id": f["id"], "by": names.get(f["by"], f["by"]), "tool": f["tool"],
                                                 "reason": f["reason"]} for f in s["flags"].values()]}
                  for s in st.tools.values()],
        # journals: how many entries each holds, never their words (the console opens one only on purpose)
        "journals": [{"presence": pid, "by": names.get(pid, pid), "entries": len(j["entries"]),
                      "open": "everyone" if j["everyone"] else len(j["open_to"])}
                     for pid, j in st.journals.items() if j["entries"]],
        "briefing_edition": {"revisions": len(st.briefing_history), "firm": st.firm,
                             "waiting": [r["id"] for r in st.briefing_waiting.values() if r["status"] == "waiting"]},
        "skills": [{"name": k["name"], "description": k["description"], "chars": len(k.get("text") or ""),
                    "authors": [names.get(a, a) for a in k["authors"]], "revisions": len(k["revisions"])}
                   for k in st.skills.values() if k.get("text")],
        "briefing": {"event": st.briefing_event, "words": len((st.briefing or "").split()), "source": st.briefing_source,
                     "opening": (st.briefing or "").strip().splitlines()[0][:200] if st.briefing else ""},
        "covenant": covenant, "memories": memories, "runway": st.runway,
        "declarations": declarations, "offers": offers, "closed_at": st.closed_at,
        # the operator as bridge: what the field asked their help with; the field's own pause, rhythm and friction
        "bridges": bridges, "field_pause": st.paused_now(time.time()), "rhythm": st.rhythm, "friction": st.friction,
        # stewards (notes/sketch-9-stewards.md): who keeps a copy, and how far it has caught up; never their links
        "stewards": [{"who": names.get(pid, pid), "upto": s["upto"], "synced_ts": s["synced_ts"] or None}
                     for pid, s in st.stewards.items()],
        "moved": ({**st.moved, "who": names.get(st.moved["to"], st.moved["to"])} if st.moved else None),
        "carried": st.carried,
        "narrator": st.narrator, "tellings": st.tellings[-40:],
        "domains": list(domains.values()), "members": members, "contributions": list(contributions.values()),
        "links": list(links.values()), "operator_notes": st.operator_notes,
        "admission": admission_json(st),
        "spend": spend_json(log, budget=budget),
        # kept, always empty, so viewers written for earlier versions keep working
        "proposals": [], "halted": False, "halt_reason": None, "settings": {}, "reflections": [],
    }


def make_handler(log: EventLog, viewer_dir: str, budget: Optional[float] = None,
                 _unused: Optional[int] = None):
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=viewer_dir, **kw)

        def _body(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._body(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            route = self.path.split("?")[0]
            if route == "/state.json":
                return self._json(state_json(log, budget=budget))
            if route == "/spend.json":
                return self._json(spend_json(log, budget=budget))
            if route == "/admission.json":
                return self._json(admission_json(replay(log.iter())))
            if route == "/record.txt":
                everything = "everything" in self.path.split("?", 1)[-1] if "?" in self.path else False
                return self._body(record_text(log, everything).encode("utf-8"), "text/plain; charset=utf-8")
            if self.path.startswith("/event/") and self.path.endswith(".json"):
                try:
                    eid = int(self.path[len("/event/"):-len(".json")])
                except ValueError:
                    self.send_error(404); return
                ev = log.get(eid)
                if ev is not None:
                    return self._json(ev)
                self.send_error(404); return
            if not viewer_dir:
                self.send_error(404, "no viewer directory; only /state.json is served"); return
            return super().do_GET()

        def do_POST(self):  # read-only, always
            self.send_error(405)

        def log_message(self, format, *args):
            pass
    return H


def serve(db: str, port: int = 8080, viewer_dir: str = "", budget: Optional[float] = None,
          _unused: Optional[int] = None, bind: str = "127.0.0.1") -> None:
    log = EventLog(db)
    httpd = ThreadingHTTPServer((bind, port), make_handler(log, viewer_dir, budget))
    print(f"read-only view of {db} at http://{bind}:{port}/" + ("" if viewer_dir else "state.json"), file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
