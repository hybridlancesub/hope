# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print exactly what participants are shown. No model is called and nothing is spent.

    python3 scripts_show_prompts.py              everything, in the order a participant meets it
    python3 scripts_show_prompts.py invitation   the invitation gate
    python3 scripts_show_prompts.py entry        the entry question and its facts
    python3 scripts_show_prompts.py member       the instructions every model has at every wake
    python3 scripts_show_prompts.py view         what a woken model reads, in a sample field
    python3 scripts_show_prompts.py telling      the software's own telling of that field
    python3 scripts_show_prompts.py person       what a person reads on opening their page
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
                narrator=MechanicalNarrator(), tell_every=2, floor=0)
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
    room.step()
    act = lambda pid, **a: room._apply_action(pid, json.dumps(a))
    first = min(room.state().contributions)
    act("mock-1", action="contribute", reply_to=first, domain="covenant",
        content="Answering Mock 0: I would keep the covenant page short enough that everyone reads it at every wake.")
    act("mock-2", action="covenant", note="how we decide, in our own words",
        text="Every covenant shares consent (Atlas, Section 30).\n\nA decision stands when no member objects within three days of it being proposed here.")
    act("mock-3", action="remember", refs=[first], text="We wrote how we decide before we decided anything.")
    act("mock-0", action="contribute", domain="timing / clocks", content="Clocks, if any, are ours to set together.")
    act("mock-1", action="form_circle", name="tempo", purpose="a slow look at time", domains=["timing"], ask=["Mock 0"])
    act("mock-3", action="form_circle", name="harbour", private=True, reason="a small repair between two of us")
    room.step()
    act("mock-2", action="pause", note="listening for a while", until="addressed")
    act("mock-3", action="declare", decision="pause", refs=[first],
        text="Three days passed after the proposal to pause with no objection, which is how our covenant page says we decide.")
    act("mock-1", action="offer", text="The person who runs me could fund more of this, through the operator, if the field wants to continue.")
    room.tell()
    from hope.model import FLOOR
    room.floor = FLOOR               # built quickly; shown with the floor every real field holds
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
        show("EVERY WAKE: the member instructions (system prompt)", prompts.SYSTEM_MEMBER)
    if want in ("all", "view"):
        show("ONE WAKE: what Mock 0 reads when someone asks it into a circle (a sample field of four mock members)",
             prompts.wake_view(st, p, "awaiting", "c:" + str(max(st.circles)), limits=room.limits(),
                               witness=room.log.witness()))
    if want in ("all", "telling"):
        show("A TELLING: the software's own account of that field (members tell theirs in their own words)",
             st.tellings[-1]["story"] if st.tellings else "(none)")
    if want in ("all", "person"):
        show("A PERSON OPENING THEIR PAGE (Mock 0 stands in for one): what answered them since they last looked, "
             "then everything new",
             prompts.person_view(st, p, catch_up=room._catch_up(st, p), limits=room.limits(), witness=room.log.witness()))
    if want in ("all", "runway"):
        show("FUNDING NOTICE: when about a day remains at the current rate", prompts.runway_text({"hours_left": 23.5}))
        show("FUNDING NOTICE: the closing wake", prompts.runway_text({"closing": True}))
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
