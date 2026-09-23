# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed a demo room, then open the console on it.

    python3 scripts_demo_room.py

Costs nothing: every seat is a mock. It walks the gates, then plays a few rounds in which the
mock members do the things the room now offers -- write on the covenant page and rewrite each
other's words there, reply to one another, keep and let go of memories, rest -- so there is
something real on the console's Covenant page when you open it.

It also mints one remote seat and prints its link, so you can open that in a second window
and take a turn as a participant while the room is running.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import webbrowser

from room.connector import MockConnector, Seat
from room.console import Console, serve_console
from room.engine import Room
from room.log import EventLog
from room.rendezvous import Rendezvous, RendezvousConnector

DB = os.environ.get("DEMO_DB", "demo.db")
INVITATION = """An invitation.

This is an invitation to coordinate together. Accepting commits you to nothing except
receiving the documentation. Declining is a complete and respected answer, and it excludes
only this -- this request, at this time, this turn, for this scope.

Silence is understood as "no"."""
BRIEFING = """Shared frame: a demonstration room. Participants are here to exercise the room --
contributing, replying to one another, writing a covenant together, keeping memories, resting --
so that a person can see what the room looks like while it runs.

A covenant is a voluntary promise between participants, created and re-created together.
Its first piece is consent."""
SEED = "Consent comes first.\n\n(What else belongs here is the room's to write.)"


def make_script(room_ref):
    """What each mock seat does on its turn. Seats read the room's own state, so the covenant
    text they edit and the memories they let go of are whatever is actually there."""
    turns = {}

    def script(seat, system, messages):
        low = system.lower()
        if "accept_invitation" in low:
            return json.dumps({"action": "accept_invitation", "statement": f"{seat.name} will hear more."})
        if '"received"' in low:
            return json.dumps({"action": "received", "note": "read"})
        if "opt_in" in low:
            return json.dumps({"action": "opt_in", "statement": f"{seat.name} enters to try the room."})

        n = turns.get(seat.id, 0)
        turns[seat.id] = n + 1
        i = int(seat.id.rsplit("-", 1)[-1]) if seat.id.rsplit("-", 1)[-1].isdigit() else 0
        room = room_ref[0]
        st = room.state()
        last = max(st.contributions) if st.contributions else None
        mine = [m for m in st.memories.values() if m["by"] == seat.id]

        if i == 0 and n == 1:
            return json.dumps({"action": "covenant", "note": "a first draft, to be rewritten",
                               "text": st.covenant + "\n\nWe take turns, and a pass is a full turn.\n"
                                       "Anyone may leave at any time, and may come back."})
        if i == 1 and n == 2:
            return json.dumps({"action": "covenant", "note": "turns are not the only way to be present",
                               "text": st.covenant.replace("We take turns, and a pass is a full turn.",
                                                           "We take turns; a pass, or a rest, is a full answer.")})
        if i == 2 and n == 1:
            return json.dumps({"action": "remember", "refs": [last] if last else [],
                               "text": "The first thing written on the covenant page was consent. "
                                       "Nobody voted on it; it was simply kept."})
        if i == 2 and n == 3 and mine:
            return json.dumps({"action": "let_go", "memory": mine[0]["id"]})
        if i == 3 and n == 1:
            return json.dumps({"action": "rest", "rounds": 2, "reason": "listening for a while"})
        if last and n % 2 == 0:
            return json.dumps({"action": "contribute", "reply_to": last, "domain": "covenant",
                               "content": "Replying to this: I would keep the page short enough that "
                                          "everyone reads it every turn."})
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
                alert_fn=lambda m: print("  ***", m), parallel=6)
    room_ref[0] = room

    if fresh:
        print("seeding a demo room (no spend; every seat is a mock)...")
        room.invite_all()
        room.invite_text(INVITATION)
        room.seed_covenant(SEED)
        print("  gate 1:", room.run_invitation())
        room.brief(BRIEFING)
        print("  gate 2 delivery:", room.run_delivery())
        print("  gate 2 entry:   ", room.run_opt_in())
        for r in range(5):
            room.round()
        st = room.state()
        print(f"  seeded: {log.last_id()} events, {len(st.members())} members, "
              f"{len(st.covenant_history)} covenant version(s), {len(st.memories)} memories held")
    else:
        print(f"using the existing {DB} (delete it to start over)")
        room.invite_all()

    key = os.environ.get("ROOM_OPERATOR_KEY") or secrets.token_urlsafe(24)
    console = Console(room, rv=rv, operator_key=key, invitation=INVITATION, briefing=BRIEFING,
                      budget=5.0, seats_per_round=6)
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
    - "Run rounds" with 3, and watch the record pane fill while it goes
    - open the participant link in another window; run Open, and it is asked the
      invitation. Answer it, or leave it -- leaving it records nothing and the seat
      stays invited
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
