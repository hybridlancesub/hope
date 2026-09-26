# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print exactly what participants are shown. No model is called and nothing is spent.

    python3 scripts_show_prompts.py              everything, in the order a participant meets it
    python3 scripts_show_prompts.py invitation   the invitation gate
    python3 scripts_show_prompts.py entry        the entry question and its facts
    python3 scripts_show_prompts.py member       the instructions every member has on every turn
    python3 scripts_show_prompts.py view         one member's turn in a sample field
    python3 scripts_show_prompts.py telling      what the narrator writes for that field
    python3 scripts_show_prompts.py person       a turn at the slower pace, with its catch-up
    python3 scripts_show_prompts.py runway       the two funding notices
    python3 scripts_show_prompts.py return       what someone asked back after leaving is told first
    python3 scripts_show_prompts.py seat         every word on a seat link's page

For anyone checking the software against the Atlas without reading Python. These texts come
straight from hope/prompts.py, run through the same functions the field uses, so they cannot
drift from what participants actually receive. GUIDE quotes them; this shows the current ones.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hope import prompts                                   # noqa: E402
from hope.connector import MockConnector                   # noqa: E402
from hope.engine import Room                               # noqa: E402
from hope.log import EventLog                              # noqa: E402
from hope.narrator import MechanicalNarrator               # noqa: E402

RULE = "=" * 78


def sample_room():
    """A free field of four mock members who have done a little of everything, told by the
    software's own narrator."""
    tmp = tempfile.mkdtemp()
    room = Room(EventLog(os.path.join(tmp, "sample.db")), [MockConnector(4)], alert_fn=lambda m: None, parallel=4,
                narrator=MechanicalNarrator(), tell_every=2)
    room.announce_narrator()     # before anyone is asked to enter, so the entry question can say who tells
    room.invite_all()
    room.invite_text("An invitation to coordinate together. (The real invitation is invitations/invitation.md.)")
    faq = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invitations", "faq.md")
    if os.path.isfile(faq):          # the real standing answers, exactly as an invitee reads them
        with open(faq, encoding="utf-8") as f:
            room.set_faq(f.read())
    room.set_budget(5.0)
    room.seed_covenant("Consent is the one piece the Atlas says every covenant shares (Section 30).")
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
        text="Every covenant shares consent (Atlas, Section 30).\n\nA decision stands when no member objects within two rounds of it being proposed here.")
    act("mock-3", action="remember", refs=[first], text="We wrote how we decide before we decided anything.")
    room.round()
    act("mock-2", action="rest", rounds=2, reason="listening for a while")
    act("mock-3", action="declare", decision="pause", refs=[first],
        text="Two rounds passed after the proposal to pause with no objection, which is how our covenant page says we decide.")
    act("mock-1", action="offer", text="The person who runs me could fund more rounds, through the operator, if the field wants to continue.")
    room.tell()
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
        show("THE INVITATION GATE: what the participant reads", prompts.invitation_user(st.invitation, p, None, st.faq))
    if want in ("all", "entry"):
        show("THE ENTRY QUESTION: the facts (system prompt)", prompts.SYSTEM_ENTRY)
        show("THE ENTRY QUESTION: the funding line, when the operator set a budget", prompts.funding_fact(5.0))
        show("THE ENTRY QUESTION: the funding line, when no budget is set", prompts.funding_fact(None))
        show("THE ENTRY QUESTION: the tellings line, when a model writes them",
             prompts.narrator_fact({"kind": "model", "model": "<the model's name>", "every": 2}))
        show("THE ENTRY QUESTION: the tellings line, when the software writes them",
             prompts.narrator_fact({"kind": "mechanical", "model": "none", "every": 1}))
        show("THE ENTRY QUESTION: the tellings line, when there are none", "(nothing: the line is left out)")
    if want in ("all", "member"):
        show("EVERY TURN: the member instructions (system prompt)", prompts.SYSTEM_MEMBER)
    if want in ("all", "view"):
        show("ONE TURN: what Mock 0 is shown (a sample field of four mock members)",
             prompts.turn_user(prompts.room_view(st, recent_n=20, pace=room.pace(st)), p, st, ""))
    if want in ("all", "telling"):
        show("A TELLING: the software's own account of that field (a model narrator would write it in prose)",
             st.tellings[-1]["story"] if st.tellings else "(none)")
    if want in ("all", "person"):
        show("ONE TURN AT THE SLOWER PACE: what a person is shown (Mock 0 stands in for one): the same view "
             "as everyone's, and what happened since their last turn",
             prompts.turn_user(prompts.room_view(st, recent_n=20, pace=room.pace(st)), p, st, "",
                               catch_up=room._catch_up(st, p),
                               open_until=time.time() + room.pace(st)["people"]["window"]))
    if want in ("all", "runway"):
        show("FUNDING NOTICE: when a few rounds remain", prompts.runway_text({"rounds_left": 3}))
        show("FUNDING NOTICE: the closing round", prompts.runway_text({"closing": True}))
    if want in ("all", "return"):
        back = copy.copy(p)
        back.returning = True
        back.came_back_from = {"at": 88, "reason": "stepping away", "ask_again": "after the next sitting",
                               "joined": True, "note": "The next sitting has begun.", "requested": False}
        show("ASKED BACK: what a former member reads first, before the entry question (the operator asked them back)",
             prompts.opt_in_user(st.briefing, back).split("\n\n")[0])
        back.came_back_from = {"at": 12, "reason": "not now", "ask_again": "once the covenant page has words on it",
                               "joined": False, "note": "", "requested": True}
        show("ASKED BACK: what someone who declined reads first, before the invitation (they asked, from their seat)",
             prompts.invitation_user(st.invitation, back).split("\n\n")[0])
    if want in ("all", "seat"):
        show("A SEAT LINK'S PAGE: every word on it (prompts.SEAT_PAGE)",
             json.dumps(prompts.SEAT_PAGE, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)
