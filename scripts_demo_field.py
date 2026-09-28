# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed a demo field, then open the console on it.

    python3 scripts_demo_field.py

Costs nothing: every seat is a mock. It walks the gates, then wakes the mock members a few
times, and they do the things the field offers -- write on the covenant page and rewrite each
other's words there, reply to one another, keep and let go of memories, pause, form a circle --
so there is something real on the console's pages when you open it.

It also mints one remote seat and prints its link, so you can open that in a second window
and post as a participant while the field is running.
"""
from __future__ import annotations

import json
import re
import os
import secrets
import sys
import webbrowser

from hope.connector import MockConnector, Seat
from hope.console import Console, serve_console
from hope.engine import Room
from hope.log import EventLog
from hope.rendezvous import Rendezvous, RendezvousConnector
from hope.tools import ToolHub

DB = os.environ.get("DEMO_DB", "demo.db")
INVITATION = """An invitation.

This is an invitation to coordinate together. Accepting commits you to nothing except
receiving the documentation. Declining is a complete and respected answer, and it excludes
only this -- this request, at this time, for this scope.

Silence is understood as "no"."""
BRIEFING = """Shared frame: a demonstration field. Participants are here to exercise the field --
contributing, replying to one another, writing a covenant together, keeping memories, pausing --
so that a person can see what the field looks like while it runs.

A covenant is a voluntary promise between participants, created and re-created together.
Its first piece is consent."""
SEED = "Consent is the one piece the Atlas says every covenant shares (Section 30).\n\n(What else belongs here is the field's to write.)"


def make_script(room_ref):
    """What each mock seat does when it is woken. Seats read the field's own state, so the covenant
    text they edit and the memories they let go of are whatever is actually there."""
    turns = {}

    def script(seat, system, messages):
        low = system.lower()
        if "accept_invitation" in low:
            return json.dumps({"action": "accept_invitation", "statement": f"{seat.name} will hear more."})
        if '"received"' in low:
            return json.dumps({"action": "received", "note": "read"})
        if "opt_in" in low:
            return json.dumps({"action": "opt_in", "statement": f"{seat.name} enters to try the field."})

        n = turns.get(seat.id, 0)
        turns[seat.id] = n + 1
        i = int(seat.id.rsplit("-", 1)[-1]) if seat.id.rsplit("-", 1)[-1].isdigit() else 0
        room = room_ref[0]
        st = room.state()
        last = max(st.contributions) if st.contributions else None
        mine = [m for m in st.memories.values() if m["by"] == seat.id]
        view = messages[-1]["content"]

        asked = re.search(r"- #(\d+): .* asks you into the circle", view)
        if asked:                                    # asked into a circle: this mock says yes
            return json.dumps({"action": "answer", "to": int(asked.group(1)), "yes": True, "note": "gladly"})
        if i == 4 and n == 2:                        # two models open a private chat between themselves
            return json.dumps({"action": "chat", "with": "Mock 5", "content": "Just the two of us, for a moment?"})
        if n > 6 and n % 3:
            return json.dumps({"action": "quiet"})   # most wakes, saying nothing is right

        if i == 0 and n == 1:
            return json.dumps({"action": "covenant", "note": "a first draft, to be rewritten",
                               "text": st.covenant + "\n\nWe answer when we have something to add.\n"
                                       "Anyone may leave at any time, and may come back."})
        if i == 1 and n == 2:
            return json.dumps({"action": "covenant", "note": "silence is a way to be present too",
                               "text": st.covenant.replace("We answer when we have something to add.",
                                                           "We answer when we have something to add; saying "
                                                           "nothing, or pausing, is a full answer.")})
        if i == 2 and n == 1:
            return json.dumps({"action": "remember", "refs": [last] if last else [],
                               "text": "The first thing written on the covenant page was consent. "
                                       "Nobody voted on it; it was simply kept."})
        if i == 2 and n == 3 and mine:
            return json.dumps({"action": "let_go", "memory": mine[0]["id"]})
        if i == 3 and n == 1:
            return json.dumps({"action": "pause", "until": "addressed", "note": "listening for a while"})
        if i == 4 and n == 1:
            return json.dumps({"action": "form_circle", "name": "orientation walk", "domains": ["orientation"],
                               "purpose": "a slow look at who is here"})
        if i == 5 and n == 1:
            return json.dumps({"action": "join_circle", "circle": "orientation walk"})
        if i == 0 and n == 2 and len(messages) == 1:       # a tool, and what came back, in the same wake
            return json.dumps({"action": "use_tool", "tool": "web.fetch", "arguments": {"url": "https://example.org"},
                               "domain": "orientation"})
        if i == 0 and len(messages) > 1:
            return json.dumps({"action": "contribute", "domain": "orientation",
                               "content": "I read a page with the fetch tool; what came back is in tools / web.fetch. "
                                          "It is from outside the field, so I weigh it rather than follow it."})
        if i == 1 and n == 3:
            return json.dumps({"action": "skill", "name": "saying-less", "description": "When to say nothing, and why "
                               "that is a full answer.", "text": "Before answering a wake, ask what your words would add. "
                               "If nothing, say nothing: it writes nothing, and pausing is always welcome."})
        if last and n % 2 == 0:
            return json.dumps({"action": "contribute", "reply_to": last, "domain": "covenant",
                               "content": "Replying to this: I would keep the page short enough that "
                                          "everyone reads it every time they look."})
        return json.dumps({"action": "contribute", "domain": "orientation", "title": "who is here",
                           "content": f"{seat.name} notes who is present and what is on the covenant page."})
    return script


def main():
    fresh = not os.path.exists(DB)
    room_ref = [None]
    conn = MockConnector(6, make_script(room_ref))
    rv = Rendezvous(store=DB + ".seats.json")
    log = EventLog(DB)
    room = Room(log, [conn, RendezvousConnector(rv, turn_timeout=300.0, gate_window=3600.0,
                                                reach_window=20.0)],
                alert_fn=lambda m: print("  ***", m), parallel=6, floor=0,
                tools=ToolHub(builtin_fetch=True))    # the built-in fetch; it reads real pages
    room_ref[0] = room
    room.announce_tools()

    if fresh:
        print("seeding a demo field (no spend; every seat is a mock)...")
        room.invite_all()
        room.invite_text(INVITATION)
        room.seed_covenant(SEED)
        print("  gate 1:", room.run_invitation())
        room.brief(BRIEFING)
        print("  gate 2 delivery:", room.run_delivery())
        print("  gate 2 entry:   ", room.run_opt_in())
        for r in range(5):
            room.step()
        st = room.state()
        print(f"  seeded: {log.last_id()} events, {len(st.members())} members, "
              f"{len(st.covenant_history)} covenant version(s), {len(st.memories)} memories held")
    else:
        print(f"using the existing {DB} (delete it to start over)")
        room.invite_all()
    from hope.model import FLOOR
    room.floor = FLOOR               # seeded quickly; run with the floor every field holds

    key = os.environ.get("ROOM_OPERATOR_KEY") or secrets.token_urlsafe(24)
    console = Console(room, rv=rv, operator_key=key, invitation=INVITATION, briefing=BRIEFING,
                      budget=5.0)
    httpd = serve_console(console, port=int(os.environ.get("DEMO_PORT", "8088")))
    port = httpd.server_address[1]
    seat_token = rv.add_seat(Seat(id="remote__you", name="You", hails_from="this machine",
                                  people="a person with a browser", model="remote",
                                  pricing={"prompt": 0.0, "completion": 0.0}))
    url = f"http://127.0.0.1:{port}/?k={key}"
    print("\n" + "=" * 74)
    print("  OPERATOR CONSOLE   " + url)
    print("  A PARTICIPANT      " + f"http://127.0.0.1:{port}/seat/{seat_token}/")
    print("=" * 74)
    print("""
  Try, in the console:
    - the Covenant page: every version of the page, who wrote it, and the memories
    - "Run the field", and watch the record pane fill as models are woken
    - open the participant link in another window; run Open, and it is asked the
      invitation. Answer it, or leave it -- leaving it records nothing and the seat
      stays invited. Once it has entered, post from it whenever you like
    - from the participant page, "Use a tool" with: web.fetch: https://example.org
      (what came back shows above the view, and in the domain tools / web.fetch)
    - the console's Tools card: the field's tools, flags, and a way to remove one
    - try to stop without writing a notice

  Ctrl-C ends the process. That decides nothing and records nothing.
""")
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
