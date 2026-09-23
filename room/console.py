# SPDX-License-Identifier: AGPL-3.0-or-later
"""One command, a browser, and no flags: the room for people who do not live in a terminal.

    python3 -m room console --db R.db --invitation FILE --briefing FILE

It runs the engine in this process and serves two different surfaces from it:

  the OPERATOR console   /?k=<operator key>     what you are paying, who is at which gate,
                                                the raw stream, the covenant page and memories
  a SEAT                 /seat/<seat token>/    one participant's own gate questions and turns

They are not the same surface and must never become so. A seat token shows that seat's own
parked question and nothing else -- not the record, not the roster, not the spend. DESIGN
Sec. 6 admits no "watching from outside" state, and a link that let someone watch would be
one. The operator key is the only thing that opens the record, and it is required even on
localhost so that putting this behind a public address changes nothing about who can read.

What the operator can do here is deliberately smaller than what a terminal can do:

    note            an operator notice, recorded, shown to members in their next view
    answer          answer a question someone asked at the invitation gate
    seat            mint an invitation link for a person or an agent on another machine
    open/enter/run  the phases, exactly as room/__main__.py runs them
    declaration     answer a member's declaration of something the room decided (to pause, to
                    close, or anything else it asks for): carry it out, or say why not, or say
                    you will answer later. Each takes an optional message the room reads.
    offer           answer a member's offer of resources: accept or decline, with a note
    budget          change what the room may spend; members are told in rounds, not dollars
    reopen          undo a close carried out by mistake (needs words the room will read)
    stop            pause the software -- which decides nothing in the room

There is no halt, no resume, and no restore here, and none anywhere else either: the room has
no voting machinery at all, and whether it pauses or ends is its members' to decide. `stop` is
the operator pausing the software (to fix a fault, say), with the obligation the pilot taught:
it will not stop the turns until you have written what the room should be told, and that
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
MAX_BODY = 1 << 20

# A browser that arrives without a key gets a page, not a JSON error. Dropping the ?k= part of
# a pasted link is the ordinary way to arrive here, and {"error": ...} tells a reader nothing
# about what went wrong or what to do -- it reads like the room is broken.
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

_NEED_KEY = _PAGE.format(title="Room console", body="""
<p>This page needs the operator key, and the address you used does not carry one.</p>
<p class="d">The key travels in the link, after <code>?k=</code>. A link copied without that
part will land here. Open the link the room printed when it started, or paste the key below.</p>
<input id="k" placeholder="operator key" autofocus>
<button onclick="if(k.value.trim())location='/?k='+encodeURIComponent(k.value.trim())">Open the console</button>
<p class="d">If you do not have it, the key is printed in the terminal where the room is
running, and can be set before starting with <code>ROOM_OPERATOR_KEY</code>.</p>""")

_NOT_A_SEAT = _PAGE.format(title="Not a seat", body="""
<p>This link does not open a seat in the room.</p>
<p class="d">A seat link is long and ends in a slash. If yours was split across lines by mail or
chat, part of it may be missing; paste the whole thing in one piece. If it was replaced, the
old one stopped working the moment the new one was made &mdash; ask whoever invited you for
the current link.</p>
<p class="d">Nothing you have said in the room is affected by this.</p>""")


class Console:
    """Owns the room and the phase worker. One phase runs at a time; the browser polls."""

    def __init__(self, room, rv=None, operator_key: str = "", invitation: str = "",
                 briefing: str = "", documentation: str = "",
                 briefing_source: str = "", budget: Optional[float] = None,
                 seats_per_round: Optional[int] = None, covenant_seed: str = "",
                 briefing_page: str = ""):
        self.room = room
        self.rv = rv
        self.key = operator_key or secrets.token_urlsafe(24)
        self.invitation, self.briefing = invitation, briefing
        self.documentation = documentation
        self.briefing_source = briefing_source
        self.budget, self.seats_per_round = budget, seats_per_round
        self.covenant_seed, self.briefing_page = covenant_seed, briefing_page
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

    def start_phase(self, name: str, rounds: int = 0, pause: float = 0.0) -> Dict[str, Any]:
        with self._lock:
            if self.busy():
                return {"ok": False, "error": f"{self.phase} is already running"}
            fn = {"open": self._open, "enter": self._enter, "run": self._run}.get(name)
            if fn is None:
                return {"ok": False, "error": f"unknown phase {name!r}"}
            self.phase = name

            def body():
                try:
                    fn(rounds=rounds, pause=pause)
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
        if self.documentation and st.documentation is None:
            room.set_documentation(self.documentation)
        room.set_budget(self.budget)
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
        self.room.invite_all()
        self._say(f"gate 2 (opt-in): {self.room.run_opt_in()}")
        self._say(f"members in: {len(self.room.state().members())}")

    def _run(self, rounds: int = 0, pause: float = 0.0, **_):
        self.room._stop.clear()      # a previous stop ended the turns, it did not end the room
        self.room.set_budget(self.budget)
        self._say(f"running {rounds or 'until stopped'} round(s)")
        self.room.run(rounds=rounds, pause=pause)
        self._say("turns stopped")

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
                return {"ok": False, "error": "this room was started without remote seats"}
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
                return {"ok": False, "error": "this room was started without remote seats"}
            sid = payload.get("seat")
            token = self.rv.rotate_token(sid)
            if token is None:
                return {"ok": False, "error": f"unknown seat {sid!r}"}
            self._say(f"replaced the link for {sid}; the old one no longer works")
            return {"ok": True, "seat": sid, "path": f"/seat/{token}/",
                    "note": "the old link stopped working. Nothing they have said is affected."}

        if action == "declaration":
            did, outcome = _int(payload.get("id")), (payload.get("outcome") or "").strip()
            if outcome not in ("carry_out", "ignore", "later"):
                return {"ok": False, "error": "outcome must be carry_out, ignore or later"}
            if outcome == "later":
                out = self.room.acknowledge_declaration(did, payload.get("note") or "")
                if out.get("ok"):
                    self._say(f"declaration #{did}: the room is told you will answer later")
                return out
            out = self.room.answer_declaration(did, outcome == "carry_out", payload.get("note") or "")
            if out.get("ok"):
                self._say(f"declaration #{did}: " + ("carried out; turns will cease" if outcome == "carry_out" else "not acted on; the room is told"))
            return out

        if action == "offer":
            oid, outcome = _int(payload.get("id")), (payload.get("outcome") or "").strip()
            if outcome not in ("accept", "decline"):
                return {"ok": False, "error": "outcome must be accept or decline"}
            out = self.room.answer_offer(oid, outcome == "accept", payload.get("note") or "")
            if out.get("ok"):
                self._say(f"offer #{oid}: {outcome}ed; the room is told")
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
                self._say("the room was reopened; the room is told why")
            return out

        if action == "stop":
            note = (payload.get("note") or "").strip()
            if not note:
                return {"ok": False, "error": "say what the room should be told. Stopping the process "
                                              "records nothing by itself, and the pilot halted a room over "
                                              "an unannounced change. The notice is recorded before turns cease."}
            self.room.emit("operator", "operator_note", {"content": note})
            self.room.request_stop()
            return {"ok": True, "note": "notice recorded, turns will cease. Nothing was decided in the room."}

        return {"ok": False, "error": f"unknown action {action!r}"}

    # -- what the operator's page reads -----------------------------------------
    def op_state(self) -> Dict[str, Any]:
        log = self.room.log
        st = state_json(log, budget=self.budget, seats_per_round=self.seats_per_round)
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
        server_version = "room-console"

        # -- plumbing ----------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")          # keep tokens out of referers
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _static(self, name: str):
            path = os.path.join(STATIC, name)
            if not os.path.isfile(path):
                return self._json({"error": f"{name} is missing"}, 404)
            with open(path, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")

        def _key(self) -> str:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            return (self.headers.get("X-Room-Key") or (q.get("k") or [""])[0] or "").strip()

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

            # a seat: its own question, and nothing else in the room
            token = self._seat_token(route)
            if token is not None:
                if rv is None or rv.seat_for_token(token) is None:
                    return self._refuse(404, "this link is not a seat", _NOT_A_SEAT)
                tail = route.split(token, 1)[1]
                if tail in ("", "/"):
                    return self._static("seat.html")
                if tail == "/turn.json":
                    return self._json(rv.peek(token))
                return self._json({"error": "a seat link addresses only that seat"}, 404)

            # everything else is the operator's, key or no answer
            if not console.authorized(self._key()):
                return self._refuse(401, "the operator key is required", _NEED_KEY)
            if route in ("/", "/index.html"):
                return self._static("console.html")
            if route == "/op/state.json":
                return self._json(console.op_state())
            if route == "/state.json":
                return self._json(state_json(console.room.log, budget=console.budget,
                                             seats_per_round=console.seats_per_round))
            if route == "/spend.json":
                return self._json(spend_json(console.room.log, budget=console.budget,
                                             seats_per_round=console.seats_per_round))
            if route == "/admission.json":
                return self._json(admission_json(console.room.state()))
            if route == "/record.txt":
                everything = "everything" in (urlparse(self.path).query or "")
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
                if route.split(token, 1)[1] != "/action":
                    return self._json({"error": "a seat may only answer its own turn"}, 404)
                out = rv.answer(token, payload)
                return self._json(out, 200 if out.get("ok") else 409)

            if not console.authorized(self._key()):
                return self._json({"error": "the operator key is required"}, 401)
            if route == "/op/phase":
                out = console.start_phase((payload.get("phase") or "").strip(),
                                          rounds=int(payload.get("rounds") or 0),
                                          pause=float(payload.get("pause") or 0.0))
                return self._json(out, 200 if out.get("ok") else 409)
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
