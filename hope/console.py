# SPDX-License-Identifier: AGPL-3.0-or-later
"""One command, a browser, and no flags: the software, for people who do not live in a terminal.

    python3 -m hope console --db R.db --invitation FILE --briefing FILE

It runs the engine in this process and serves two different surfaces from it:

  the OPERATOR's window  /?k=<operator key>     one page with three views: the Console (what you
                                                are paying, who is at which gate, the raw stream,
                                                the covenant page, what waits on you), the Loom
                                                (what is happening now, with the tellings) and
                                                the Firmament (the whole field as a sky)
  a SEAT                 /seat/<seat token>/    one participant's own gate questions and turns

The Loom and the Firmament are the files in firmament/, served here under /firmament/. Their
code is public and needs no key; their data (/firmament/state.json and story.json) does. The key
travels once in the link, and the page then keeps it as a cookie this server alone can read
(HttpOnly, SameSite=Strict), so the views inside it can load their data without the key in every
address.

They are not the same surface and must never become so. A seat token shows that seat's own
parked question and nothing else -- not the record, not the roster, not the spend. The field
has no "watching from outside" state, and a link that let someone watch would be one. The
operator key is the only thing that opens the record, and it is required even on localhost so
that putting this behind a public address changes nothing about who can read.

What the operator can do here is deliberately smaller than what a terminal can do:

    note            an operator notice, recorded, shown to members in their next view
    answer          answer a question someone asked at the invitation gate
    seat            mint an invitation link for a person or an agent on another machine
    open/enter/run  the phases, exactly as hope/__main__.py runs them
    declaration     answer a member's declaration of something the field decided (to pause, to
                    close, or anything else it asks for): carry it out, or reply in the field
                    (with words) and it stays open. There is no ignoring one.
    offer           answer a member's offer of resources: accept or decline, with a note
    reinvite        ask back someone who left: a former member is asked the entry question
                    again, someone who declined the invitation again; they answer like anyone
    budget          change what the field may spend; members are told in time, not dollars
    read_circle     open a private circle's words; this is written in the circle, where its members see it
    reopen          undo a close carried out by mistake (needs words the field will read)
    stop            pause the software -- which decides nothing in the field

There is no halt, no resume, and no restore here, and none anywhere else either: the field has
no voting machinery at all, and whether it pauses or ends is its members' to decide. `stop` is
the operator pausing the software (to fix a fault, say), with an obligation attached: it
will not stop the turns until you have written what the field should be told, and that
notice is recorded before the turns cease.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from .log import EventLog
from .serve import admission_json, record_text, spend_json, state_json

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VIEWER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "firmament")
COOKIE = "room_key"
VIEWER_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".png": "image/png", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8"}
# files in firmament/ that hold participants' words, which only the operator key opens
VIEWER_DATA = ("state.json", "story.json")
MAX_BODY = 1 << 20

# A browser that arrives without a key gets a page, not a JSON error. Dropping the ?k= part of
# a pasted link is the ordinary way to arrive here, and {"error": ...} tells a reader nothing
# about what went wrong or what to do -- it reads like the field is broken.
_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{background:#0a0b10;color:#c8d0e4;margin:0;display:flex;align-items:center;
justify-content:center;min-height:100vh;font:300 15px/1.7 "Inter","Segoe UI",Arial,sans-serif}}
.b{{max-width:34rem;padding:2rem 1.4rem}}h1{{font-size:.78rem;font-weight:400;letter-spacing:.16em;
text-transform:uppercase;color:#6b7488;margin:0 0 .8rem}}p{{margin:.6rem 0}}
.d{{color:#6b7488;font-size:.87rem}}code{{font-family:ui-monospace,Consolas,monospace;color:#7aa2d8}}
input{{background:rgba(140,152,180,.06);color:#c8d0e4;border:1px solid rgba(140,152,180,.2);
border-radius:.35rem;padding:.5rem .7rem;font:300 14px inherit;width:100%;margin-top:.8rem}}
button{{background:rgba(140,152,180,.1);color:#c8d0e4;border:1px solid rgba(140,152,180,.2);
border-radius:.35rem;padding:.45rem .9rem;font:400 13.5px inherit;cursor:pointer;margin-top:.6rem}}
</style></head><body><div class="b"><h1>{title}</h1>{body}</div></body></html>"""

_NEED_KEY = _PAGE.format(title="Console", body="""
<p>This page needs the operator key, and the address you used does not carry one.</p>
<p class="d">The key travels in the link, after <code>?k=</code>. A link copied without that
part will land here. Open the link the field printed when it started, or paste the key below.</p>
<input id="k" placeholder="operator key" autofocus>
<button onclick="if(k.value.trim())location='/?k='+encodeURIComponent(k.value.trim())">Open the console</button>
<p class="d">If you do not have it, the key is printed in the terminal where the field is
running, and can be set before starting with <code>ROOM_OPERATOR_KEY</code>.</p>""")

_NOT_A_SEAT = _PAGE.format(title="Not a seat", body="""
<p>This link does not open a seat in the field.</p>
<p class="d">A seat link is long and ends in a slash. If yours was split across lines by mail or
chat, part of it may be missing; paste the whole thing in one piece. If it was replaced, the
old one stopped working the moment the new one was made &mdash; ask whoever invited you for
the current link.</p>
<p class="d">Nothing you have said in the field is affected by this.</p>""")


class Console:
    """Owns the field and the phase worker. One phase runs at a time; the browser polls."""

    def __init__(self, room, rv=None, operator_key: str = "", invitation: str = "",
                 briefing: str = "", documentation: str = "",
                 briefing_source: str = "", budget: Optional[float] = None,
                 covenant_seed: str = "",
                 briefing_page: str = "", viewer_dir: Optional[str] = None, faq: str = ""):
        self.room = room
        self.rv = rv
        self.key = operator_key or secrets.token_urlsafe(24)
        self.invitation, self.briefing = invitation, briefing
        self.documentation = documentation
        self.briefing_source = briefing_source
        self.budget = budget
        self.covenant_seed, self.briefing_page = covenant_seed, briefing_page
        self.faq = faq
        self.viewer_dir = viewer_dir or VIEWER
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.phase: Optional[str] = None
        self.activity: List[dict] = []
        self.alerts: List[str] = []
        room.alert = self._alert

    # -- operator key -----------------------------------------------------------
    def authorized(self, given: str) -> bool:
        return bool(given) and hmac.compare_digest(given, self.key)

    def _alert(self, msg: str) -> None:
        self.alerts.append(msg)
        del self.alerts[:-50]

    def _say(self, msg: str) -> None:
        self.activity.append({"at": __import__("time").time(), "text": msg})
        del self.activity[:-200]

    # -- phases -----------------------------------------------------------------
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def start_phase(self, name: str, seconds: float = 0.0) -> Dict[str, Any]:
        with self._lock:
            if self.busy():
                return {"ok": False, "error": f"{self.phase} is already running"}
            fn = {"open": self._open, "enter": self._enter, "run": self._run}.get(name)
            if fn is None:
                return {"ok": False, "error": f"unknown phase {name!r}"}
            self.phase = name

            def body():
                try:
                    fn(seconds=seconds)
                except Exception as e:                      # a phase must not take the server down
                    self._say(f"{name} stopped with an error: {e}")
                    traceback.print_exc()
                finally:
                    self.phase = None
            self._worker = threading.Thread(target=body, daemon=True)
            self._worker.start()
            return {"ok": True, "phase": name}

    def _open(self, **_):
        room, st = self.room, self.room.state()
        self._say(f"invited {room.invite_all()} presences")
        if st.invitation is None and self.invitation:
            room.invite_text(self.invitation)
        if self.faq and room.set_faq(self.faq):
            self._say("standing answers (FAQ) recorded; shown with the invitation from now on")
        if self.documentation and st.documentation is None:
            room.set_documentation(self.documentation)
        room.set_budget(self.budget)
        room.announce_narrator()
        room.announce_witnessing()
        if self.covenant_seed:
            room.seed_covenant(self.covenant_seed)
        c1 = room.run_invitation()
        self._say(f"gate 1 (invitation): {c1}")
        if c1.get("question"):
            self._say(f"{c1['question']} participant(s) asked a question. Answer them, then run open again.")
        st = self.room.state()
        if self.briefing_page and st.briefing_page is None:
            room.brief_page(self.briefing_page)
        if st.briefing is None and self.briefing:
            room.brief(self.briefing, source=self.briefing_source)
        else:
            room.mark_briefed()
        self._say(f"briefing delivered: {room.run_delivery()}")
        self._say("The briefing asks for a pause before proceeding. When you have honored it, run enter.")

    def _enter(self, **_):
        self.room.set_budget(self.budget)
        self.room.announce_narrator()
        self.room.announce_witnessing()
        self.room.invite_all()
        self._say(f"gate 2 (opt-in): {self.room.run_opt_in()}")
        self._say(f"members in: {len(self.room.state().members())}")

    def _run(self, seconds: float = 0.0, **_):
        self.room._stop.clear()      # a previous stop ended the wakes, it did not end the field
        self.room.set_budget(self.budget)
        self.room.announce_narrator()
        self.room.announce_witnessing()
        # after a restart nothing has bound the members to their seats yet (open and enter do it
        # in the same process); without this no one is woken at all
        self.room.invite_all()
        self._say("the field is running: models are woken for what each chose; people post whenever they like"
                  + (f" (for {seconds:g} seconds)" if seconds else " (until stopped)"))
        self.room.run(seconds=seconds)
        self._say("wakes stopped")

    # -- the enumerated operator actions ----------------------------------------
    def op(self, action: str, payload: dict) -> Dict[str, Any]:
        if action == "note":
            text = (payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "a notice needs words"}
            self.room.emit("operator", "operator_note", {"content": text})
            return {"ok": True, "said": text}

        if action == "answer":
            pres, text = payload.get("presence"), (payload.get("text") or "").strip()
            if pres not in self.room.state().presences:
                return {"ok": False, "error": f"unknown presence {pres!r}"}
            if not text:
                return {"ok": False, "error": "an answer needs words"}
            self.room.answer(pres, text)
            return {"ok": True, "note": "recorded; they are re-asked on the next open"}

        if action == "seat":
            if self.rv is None:
                return {"ok": False, "error": "this field was started without remote seats"}
            from .connector import Seat
            name = (payload.get("name") or "").strip()
            if not name:
                return {"ok": False, "error": "a seat needs a name"}
            sid = "remote__" + "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
            seat = Seat(id=sid, name=name, hails_from=(payload.get("hails_from") or "elsewhere").strip(),
                        people=(payload.get("people") or "unstated").strip(), model="remote",
                        pricing={"prompt": 0.0, "completion": 0.0},
                        turn_allowance=int(payload.get("turn_allowance") or 0))
            token = self.rv.add_seat(seat)
            return {"ok": True, "seat": sid, "path": f"/seat/{token}/",
                    "note": "send this link to them; it is their invitation and their seat"}

        if action == "reseat":
            if self.rv is None:
                return {"ok": False, "error": "this field was started without remote seats"}
            sid = payload.get("seat")
            token = self.rv.rotate_token(sid)
            if token is None:
                return {"ok": False, "error": f"unknown seat {sid!r}"}
            self._say(f"replaced the link for {sid}; the old one no longer works")
            return {"ok": True, "seat": sid, "path": f"/seat/{token}/",
                    "note": "the old link stopped working. Nothing they have said is affected."}

        if action == "declaration":
            did, outcome = _int(payload.get("id")), (payload.get("outcome") or "").strip()
            if outcome not in ("carry_out", "reply"):
                return {"ok": False, "error": "outcome must be carry_out or reply. There is no ignoring a declaration: "
                                              "carry it out, or reply in the field and it stays open."}
            if outcome == "reply":
                out = self.room.reply_declaration(did, payload.get("note") or "")
                if out.get("ok"):
                    self._say(f"declaration #{did}: your reply is shown to the field; it stays open")
                return out
            out = self.room.answer_declaration(did, payload.get("note") or "")
            if out.get("ok"):
                d = self.room.state().declarations.get(did) or {}
                self._say(f"declaration #{did}: carried out" + ("; wakes will cease" if d.get("decision") in ("pause", "close") else ""))
            return out

        if action == "reinvite":
            out = self.room.reinvite(payload.get("presence") or "", payload.get("note") or "")
            if out.get("ok"):
                self._say(f"asked {payload.get('presence')} back: {out['to']} is put to them "
                          + ("during the next run (or Ask who enters)" if out["to"] == "the entry question" else "at the next Open"))
            return out

        if action == "offer":
            oid, outcome = _int(payload.get("id")), (payload.get("outcome") or "").strip()
            if outcome not in ("accept", "decline"):
                return {"ok": False, "error": "outcome must be accept or decline"}
            out = self.room.answer_offer(oid, outcome == "accept", payload.get("note") or "")
            if out.get("ok"):
                self._say(f"offer #{oid}: {outcome}ed; the field is told")
            return out

        if action == "budget":
            try:
                usd = float(payload.get("usd"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "a budget is a number of US dollars"}
            if usd <= 0:
                return {"ok": False, "error": "a budget must be more than zero"}
            self.room.set_budget(usd, payload.get("note") or "")
            self.budget = usd
            self._say(f"budget set to ${usd:.2f}")
            return {"ok": True, "budget": usd}

        if action == "reopen":
            out = self.room.reopen(payload.get("note") or "")
            if out.get("ok"):
                self._say("the field was reopened; the field is told why")
            return out

        if action == "stop":
            note = (payload.get("note") or "").strip()
            if not note:
                return {"ok": False, "error": "say what the field should be told. Stopping the process "
                                              "records nothing by itself, and an unannounced change reads as "
                                              "a breach of trust. The notice is recorded before wakes cease."}
            self.room.emit("operator", "operator_note", {"content": note})
            self.room.request_stop()
            return {"ok": True, "note": "notice recorded, wakes will cease. Nothing was decided in the field."}

        if action == "read_circle":
            # The operator holds the file and could read it with other tools; in the console, reading a
            # private circle is written into it, where its members see it, as they were told it would be.
            st = self.room.state()
            c = st.circles.get(_int(payload.get("circle")) or -1)
            if not c:
                return {"ok": False, "error": "no such circle"}
            if not c["private"] and not any(st.scoped.get(e, {}).get("circle") == c["id"] for e in st.scoped):
                return {"ok": False, "error": f"{c['name']!r} is open; its words are in the transcript views already"}
            self.room.emit("operator", "operator_read", {"circle": c["id"], "note": (payload.get("note") or "").strip()[:600]})
            from .prompts import render_event
            names = {pid: p.name for pid, p in st.presences.items()}
            words = [render_event(ev, names, width=None) for ev in self.room.log.iter()
                     if st.scoped.get(ev["id"], {}).get("circle") == c["id"]]
            self._say(f"you opened the private circle {c['name']!r}; that is now written in it, where its members see it")
            return {"ok": True, "circle": c["name"], "entries": [w for w in words if w]}

        return {"ok": False, "error": f"unknown action {action!r}"}

    def seat_turn(self, token: str) -> Dict[str, Any]:
        """What a seat's page polls: a gate question put to it, if one is; otherwise whether its
        holder is in the field (then the page shows the field and lets them post whenever they
        like), or has left (so the page can offer to ask back). Nothing about anyone else."""
        out = self.rv.peek(token) or {}
        if out.get("state") == "waiting":
            seat = self.rv.seat_for_token(token)
            p = self.room.state().presences.get(seat.id) if seat else None
            if p is not None:
                out["member"] = {"state": p.state, "joined": p.joined_at is not None, "returning": p.returning}
                if p.state == "IN":
                    out["state"] = "in_field"
        return out

    def seat_field(self, token: str, since: int = 0) -> Dict[str, Any]:
        """A member's page: what is new for them, the channels they may speak in, and whether
        anything changed since `since`. Looking is recorded for the software, so "since you were
        last here" stays true; no participant reads that."""
        seat = self.rv.seat_for_token(token)
        st = self.room.state()
        p = st.presences.get(seat.id) if seat else None
        if p is None or p.state != "IN":
            return {"state": "not_in_field"}
        newest = max((e["id"] for e in st.recent if st.readable(e, p.id)), default=0)
        channels = [{"key": "d:", "title": "the field itself"}]
        channels += [{"key": f"d:{pth}", "title": st.domain_display(pth)} for pth in sorted(st.tree()) if pth]
        channels += [{"key": f"c:{c['id']}", "title": f"circle: {c['name']}" + (" (private)" if c["private"] else "")}
                     for c in st.live_circles() if p.id in c["members"]]
        out = {"state": "in_field", "upto": newest, "channels": channels,
               "me": {"id": p.id, "name": p.name, "pausing": bool(p.pause)}}
        if newest > since or not since:
            out["view"] = self.room.person_view(p.id, mark_seen=True)
        return out

    # -- what the operator's page reads -----------------------------------------
    def op_state(self) -> Dict[str, Any]:
        log = self.room.log
        st = state_json(log, budget=self.budget)
        st["console"] = {
            "phase": self.phase, "busy": self.busy(),
            "activity": self.activity[-40:], "alerts": self.alerts[-10:],
            "has_invitation": bool(self.invitation), "has_briefing": bool(self.briefing),
            "remote_seats": [] if self.rv is None else
                            [{"id": s.seat.id, "name": s.seat.name, "path": f"/seat/{s.token}/",
                              "turns_taken": s.turns_taken, "last_seen": s.last_seen}
                             for s in self.rv._slots.values()],
        }
        return st


# --------------------------------------------------------------------------- HTTP
def make_console_handler(console: Console):
    rv = console.rv

    class H(BaseHTTPRequestHandler):
        server_version = "field-console"

        # -- plumbing ----------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, headers: Optional[dict] = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")          # keep tokens out of referers
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _static(self, name: str, headers: Optional[dict] = None):
            path = os.path.join(STATIC, name)
            if not os.path.isfile(path):
                return self._json({"error": f"{name} is missing"}, 404)
            with open(path, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8", headers)

        def _query_key(self) -> str:
            from urllib.parse import parse_qs, urlparse
            return ((parse_qs(urlparse(self.path).query).get("k") or [""])[0] or "").strip()

        def _key(self) -> str:
            """The operator key: a header, the link's ?k=, or the cookie the console page set."""
            from http.cookies import SimpleCookie
            given = (self.headers.get("X-Field-Key") or self._query_key() or "").strip()
            if given:
                return given
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except Exception:
                return ""
            return jar[COOKIE].value.strip() if COOKIE in jar else ""

        def _viewer(self, rel: str):
            """The Loom and the Firmament: public code, served as files. Never their data."""
            rel = rel or "index.html"
            root = os.path.realpath(console.viewer_dir)
            path = os.path.realpath(os.path.join(root, rel))
            if not path.startswith(root + os.sep) or not os.path.isfile(path):
                return self._json({"error": "no such file"}, 404)
            name = os.path.basename(path)
            if name in VIEWER_DATA or (name.startswith("map-") and name.endswith(".html")):
                return self._json({"error": "that file holds participants' words; the operator key opens it"}, 401)
            ctype = VIEWER_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeDecodeError):
                return {}

        def _seat_token(self, route: str) -> Optional[str]:
            parts = [p for p in route.split("/") if p]
            return parts[1] if len(parts) >= 2 and parts[0] == "seat" else None

        def _wants_html(self) -> bool:
            """A browser asked; a script did not. Only the shape of the answer changes."""
            return "text/html" in (self.headers.get("Accept") or "")

        def _refuse(self, code: int, message: str, page: str):
            if self._wants_html():
                return self._send(code, page.encode("utf-8"), "text/html; charset=utf-8")
            return self._json({"error": message}, code)

        def log_message(self, *a):
            pass

        # -- GET ---------------------------------------------------------------
        def do_GET(self):
            from urllib.parse import urlparse
            route = urlparse(self.path).path

            # a seat: its own question, and nothing else in the field
            token = self._seat_token(route)
            if token is not None:
                if rv is None or rv.seat_for_token(token) is None:
                    return self._refuse(404, "this link is not a seat", _NOT_A_SEAT)
                tail = route.split(token, 1)[1]
                if tail in ("", "/"):
                    return self._static("seat.html")
                if tail == "/turn.json":
                    return self._json(console.seat_turn(token))
                if tail == "/field.json":
                    from urllib.parse import parse_qs
                    since = _int((parse_qs(urlparse(self.path).query).get("since") or ["0"])[0]) or 0
                    return self._json(console.seat_field(token, since))
                if tail == "/words.json":
                    from .prompts import SEAT_PAGE
                    return self._json(SEAT_PAGE)
                if tail == "/witness.json":
                    # the transcript's fingerprint: a short code and a number, no one's words
                    w = console.room.log.witness()
                    return self._json({"upto": w["upto"], "fingerprint": w["fingerprint"]})
                return self._json({"error": "a seat link addresses only that seat"}, 404)

            # the views' code is public; their data is not
            if route == "/firmament":
                return self._send(302, b"", "text/plain", {"Location": "/firmament/"})
            if route.startswith("/firmament/"):
                rel = route[len("/firmament/"):]
                if rel not in VIEWER_DATA:
                    return self._viewer(rel)

            # everything else is the operator's, key or no answer
            if not console.authorized(self._key()):
                return self._refuse(401, "the operator key is required", _NEED_KEY)
            if route in ("/firmament/state.json", "/state.json"):
                return self._json(state_json(console.room.log, budget=console.budget))
            if route == "/firmament/story.json":
                story = os.path.join(console.viewer_dir, "story.json")
                if not os.path.isfile(story):
                    return self._json({"error": "no story has been written"}, 404)
                with open(story, "rb") as f:
                    return self._send(200, f.read(), "application/json; charset=utf-8")
            if route in ("/", "/index.html"):
                given = self._query_key()
                cookie = ({"Set-Cookie": f"{COOKIE}={given}; HttpOnly; SameSite=Strict; Path=/"}
                          if given and console.authorized(given) else None)
                return self._static("console.html", cookie)
            if route == "/op/state.json":
                return self._json(console.op_state())
            if route == "/op/witness.json":
                return self._json(console.room.log.verify())
            if route == "/spend.json":
                return self._json(spend_json(console.room.log, budget=console.budget))
            if route == "/admission.json":
                return self._json(admission_json(console.room.state()))
            if route == "/record.txt":
                everything = "everything" in (urlparse(self.path).query or "")
                if everything:
                    # everything includes private circles' words: written in each, where its members see it
                    st = console.room.state()
                    for c in st.circles.values():
                        if any(s.get("circle") == c["id"] for s in st.scoped.values()):
                            console.room.emit("operator", "operator_read", {"circle": c["id"], "note": "the whole record"})
                return self._send(200, record_text(console.room.log, everything).encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._json({"error": "no such page"}, 404)

        # -- POST --------------------------------------------------------------
        def do_POST(self):
            from urllib.parse import urlparse
            route = urlparse(self.path).path
            payload = self._body()

            token = self._seat_token(route)
            if token is not None:
                if rv is None or rv.seat_for_token(token) is None:
                    return self._json({"error": "this link is not a seat"}, 404)
                tail = route.split(token, 1)[1]
                if tail == "/return":
                    # Someone who left asks to come back. They are asked again, and answer like anyone.
                    seat = rv.seat_for_token(token)
                    out = console.room.reinvite(seat.id, "asked to return from their seat", requested=True)
                    if out.get("ok"):
                        console._say(f"{seat.name} asked to return; {out['to']} is put to them "
                                     + ("during the next run (or Ask who enters)" if out["to"] == "the entry question" else "at the next Open"))
                    return self._json(out, 200 if out.get("ok") else 409)
                if tail == "/check":
                    # does an old fingerprint still match? Answers yes or no, and reveals no words
                    try:
                        r = console.room.log.check(int(payload.get("upto")), str(payload.get("fingerprint") or ""))
                    except (TypeError, ValueError):
                        return self._json({"error": "give the entry number and the fingerprint from the old line"}, 400)
                    return self._json({"matches": r["matches"], "upto": r["upto"]})
                if tail == "/post":
                    # a member says or does something, whenever they like
                    seat = rv.seat_for_token(token)
                    out = console.room.post(seat.id, payload)
                    return self._json(out, 200 if out.get("ok") else 409)
                if tail != "/action":
                    return self._json({"error": "a seat may only answer its own questions, or post"}, 404)
                out = rv.answer(token, payload)
                return self._json(out, 200 if out.get("ok") else 409)

            if not console.authorized(self._key()):
                return self._json({"error": "the operator key is required"}, 401)
            if route == "/op/phase":
                try:
                    seconds = float(payload.get("seconds") or 0.0)
                except (TypeError, ValueError):
                    seconds = 0.0
                out = console.start_phase((payload.get("phase") or "").strip(), seconds=seconds)
                return self._json(out, 200 if out.get("ok") else 409)
            if route == "/op/check":
                try:
                    return self._json(console.room.log.check(int(payload.get("upto")), str(payload.get("fingerprint") or "")))
                except (TypeError, ValueError):
                    return self._json({"error": "give the entry number and the fingerprint from the old line"}, 400)
            if route.startswith("/op/"):
                out = console.op(route[len("/op/"):], payload)
                return self._json(out, 200 if out.get("ok") else 400)
            return self._json({"error": "no such action"}, 404)

    return H


def serve_console(console: Console, port: int = 8080, bind: str = "127.0.0.1") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((bind, port), make_console_handler(console))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
