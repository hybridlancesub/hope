# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print exactly what participants are shown. No model is called and nothing is spent.

    python3 scripts_show_prompts.py              everything, in the order a participant meets it
    python3 scripts_show_prompts.py invitation   the invitation gate
    python3 scripts_show_prompts.py entry        the entry question and its facts
    python3 scripts_show_prompts.py member       the instructions every member has on every turn
    python3 scripts_show_prompts.py view         one member's turn in a sample room
    python3 scripts_show_prompts.py runway       the two funding notices

For anyone checking the software against the Atlas without reading Python. These texts come
straight from room/prompts.py, run through the same functions the room uses, so they cannot
drift from what participants actually receive. GUIDE quotes them; this shows the current ones.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from room import prompts                                   # noqa: E402
from room.connector import MockConnector                   # noqa: E402
from room.engine import Room                               # noqa: E402
from room.log import EventLog                              # noqa: E402

RULE = "=" * 78


def sample_room():
    """A free room of four mock members who have done a little of everything."""
    tmp = tempfile.mkdtemp()
    room = Room(EventLog(os.path.join(tmp, "sample.db")), [MockConnector(4)], alert_fn=lambda m: None, parallel=4)
    room.invite_all()
    room.invite_text("An invitation to coordinate together. (The real invitation is invitations/invitation.md.)")
    room.set_budget(5.0)
    room.seed_covenant("Consent comes first. It is the one piece the Atlas says every covenant shares (Section 30).")
    room.run_invitation()
    room.brief("Shared frame: the Atlas of Coordination. (A short stand-in here; the real briefing is "
               "briefings/atlas-of-coordination.md, which rides in each view by reference because it is long.)")
    room.run_delivery()
    room.run_opt_in()
    room.round()
    act = lambda pid, **a: room._apply_action(pid, json.dumps(a))
    first = min(room.state().contributions)
    act("mock-1", action="contribute", reply_to=first, domain="covenant",
        content="Answering Mock 0: I would keep the covenant page short enough that everyone reads it every turn.")
    act("mock-2", action="covenant", note="how we decide, in our own words",
        text="Consent comes first.\n\nA decision stands when no member objects within two rounds of it being proposed here.")
    act("mock-3", action="remember", refs=[first], text="We wrote how we decide before we decided anything.")
    room.round()
    act("mock-2", action="rest", rounds=2, reason="listening for a while")
    act("mock-3", action="declare", decision="pause", refs=[first],
        text="Two rounds passed after the proposal to pause with no objection, which is how our covenant page says we decide.")
    act("mock-1", action="offer", text="The person who runs me could fund more rounds, through the operator, if the room wants to continue.")
    return room


def show(title, text):
    print(RULE)
    print(title)
    print(RULE)
    print(text.rstrip())
    print()


def main(argv):
    want = argv[1] if len(argv) > 1 else "all"
    room = sample_room()
    st = room.state()
    p = st.presences["mock-0"]
    if want in ("all", "invitation"):
        show("THE INVITATION GATE: the instructions (system prompt)", prompts.SYSTEM_INVITATION)
        show("THE INVITATION GATE: what the participant reads", prompts.invitation_user(st.invitation, p))
    if want in ("all", "entry"):
        show("THE ENTRY QUESTION: the facts (system prompt)", prompts.SYSTEM_ENTRY)
        show("THE ENTRY QUESTION: the funding line, when the operator set a budget", prompts.funding_fact(5.0))
        show("THE ENTRY QUESTION: the funding line, when no budget is set", prompts.funding_fact(None))
    if want in ("all", "member"):
        show("EVERY TURN: the member instructions (system prompt)", prompts.SYSTEM_MEMBER)
    if want in ("all", "view"):
        show("ONE TURN: what Mock 0 is shown (a sample room of four mock members)",
             prompts.turn_user(prompts.room_view(st, recent_n=20), p, st, ""))
    if want in ("all", "runway"):
        show("FUNDING NOTICE: when a few rounds remain", prompts.runway_text({"rounds_left": 3}))
        show("FUNDING NOTICE: the closing round", prompts.runway_text({"closing": True}))


if __name__ == "__main__":
    main(sys.argv)
