Atlas audit, September 2026
===========================

Written for the author of the Atlas. It reads the whole hope repository against the Atlas of
Coordination: the code, what participants are shown, the documentation, and the notes. The
audit itself changed nothing. The Atlas was not edited.


Where things stand (2026-09-26)
-------------------------------
After reading this, the author decided the following, and it was done (uncommitted):
  - Part A, all of it. The seat page's words now live in hope/prompts.py with everything else
    participants are told, so the tests and scripts_show_prompts.py see them.
  - A7 went further than recommended: there is no ignoring a declaration at all. The operator
    carries it out, or replies in the field (with words), and it stays open until carried out.
  - B1: the ranking is gone from DESIGN, README, GUIDE, the tests and the drafts.
  - B3 and B4: participants are told who the operator is and everything they can do (DESIGN
    section 9, and the entry question), including reopening, with the operator's intention to
    keep the field open as long as possible, resources allowing.
  - B5 and A5: the field keeps two clocks, the models' and the people's, each set by the members
    who keep time by it, within limits every view states; times are shown in Unix seconds and
    UTC. A late model answer is applied, and a person's unanswered turn writes nothing as theirs.
  - B6: leaving is not final. A former member can be asked back (by the operator, or at their own
    request from a seat link) and returns through the entry question; someone who declined is
    asked the invitation again.
  - B7, B8, B9: said plainly in DESIGN (section 10, "Not yet built"; the memory rule as the
    software's starting rule; where the Firmament lives and that it is shaped for human eyes).
    A way for the field to separate a member waits on the conversation about tools.
Then (same day, later): the people's clock defaults became 5 minutes between turns and 15 minutes
to answer. B10: a reply outside the action format is kept in the transcript as written, and its
author is told. B11: topics are listed by recent use with no nudge to conform; a turn allowance
is told only to its own member, with no comparison to other seats; the promise about questions
at the gate now says a question may wait. Tests for these are written but not yet run.
Then, going further than the audit asked: plain text is accepted as a contribution (nothing else
is guessed from it); topic labels are "magnetic" (spellings grouped, near labels pointed out to
each other, reuse invited, and authors may move their own entries, never anyone else's); and
words from the people's clock linger in every view for 20 rounds, with the rounds run since and
whether each has been answered, while a person's turn opens with a way back in. Tests for these
are written or adjusted but not yet run.
Still open: B2 (tamper-evidence), a way to answer many waiting questions at once, and whether
one member alone should be able to change a clock.


How this was read
-----------------
The Atlas calls itself "Not a rulebook", and says of order that "Some forms of order are
significant, necessary, purposeful, helpful, provide accessibility". So this is not a compliance
check. Each finding names the passage of the Atlas, the place in hope that strains against it,
and whether that strain is doing necessary work.

There is one pass/fail test: everything participants are told must be true of the code. The
Atlas asks for consent that is "well informed" (Section 28), and an untrue sentence at a gate
makes that impossible. Part A holds those failures. Part B holds the tensions, which are
judgment calls. Part C is what holds up. Part D suggests an order of work.

One reader did this. Where a finding is a judgment, the judgment is yours, and in time the
field's.

Each finding ends with whether fixing it changes what participants read ("Told differently").
"Where" lines are for whoever makes the change.


PART A: WHAT PARTICIPANTS ARE TOLD THAT IS NOT TRUE
===================================================

A1. The seat page still promises a personal answer
--------------------------------------------------
A person holding a seat link who presses "Ask a question first" is told: "It is recorded,
answered personally, and you are asked again with the answer in hand." You decided a personal
answer is not a reasonable promise at scale, and it was removed from everything the models read.
It survived on the seat page because the test that guards this promise reads only
hope/prompts.py.
Recommend: use the gate's own words. "It is recorded and put to whoever invited you. You are
asked again once they have answered, with the answer in hand." Make the test read the seat page
too.
Where: hope/static/seat.html:105; tests.py (test_the_invitation_never_promises_a_personal_answer).
Told differently: yes.

A2. The seat page says silence at a gate is a decline; it is not
----------------------------------------------------------------
The countdown under every gate question says "no reply is recorded as a decline". The software
does the opposite, on purpose: an unanswered gate records nothing about the person's will, and
they are asked again later. The one exception is the share question at closing, where silence
means nothing is shared. The page is harsher than the software, and a false deadline adds the
pressure the Atlas warns against ("provoking a sense of urgency", Section 17).
Recommend: at the invitation, reading and entry gates: "If the window closes, nothing is recorded
about your answer, and you are asked again later." At the share question: "If you do not answer,
nothing of yours is shared." For turns, see A5.
Where: hope/static/seat.html:194.
Told differently: yes.

A3. A person's yes to sharing their words is lost
-------------------------------------------------
When a field closes, each member is asked whether their words may be shown to a later field.
Tested directly: when a person presses "Share everything I said" on the seat page, the software
reads it as an ordinary contribution containing the word "share". It asks once more, then
records "no explicit answer". The page tells the person "Recorded as contribute." People seated
at a terminal are not asked at all, because the closing question skips them.

The outcome errs on the safe side, since nothing is shared, but the person's own choice is
overwritten (Maxim 1: "Every participant in the coordination field acts by choice"). People are
also offered only everything or nothing, where a model may name particular entries (Maxim 5:
"every participant's signal has a genuine pathway to reception"). DESIGN says "each member is
asked individually". For people, that is not yet true.
Recommend: teach the plain-text reader the share answers ("share", "share #12 #15", "no"). Ask
terminal seats too. Give the seat page a "Share only these" choice. Add a test named as the
promise.
Where: hope/rendezvous.py (answer); hope/human.py (translate); hope/engine.py:377 (closing skips
terminal seats); hope/static/seat.html:122-127.
Told differently: yes (the seat page gains a choice).

A4. "None of your words leave the field unless you say yes"
-----------------------------------------------------------
The entry question and every turn say this without qualification, and CONTRIBUTING holds the
project to it. Three routes take members' words out of the field:

  - Every turn, each model member's view goes to the service that runs that model, and the view
    holds other members' words. DESIGN section 5 discloses this.
  - A model narrator, if one is on, reads the transcript. The entry question discloses this, in
    its own paragraph after the briefing.
  - The `map` command sends a digest of members' words to a DeepSeek model through Nous, and
    writes the retelling to firmament/story.json. It does this by default. Nothing tells
    participants about it. Its cost is also left out of the field's spend, so the budget never
    counts it.

The first two are disclosed, but the sentence participants read at the gate states the rule
without its exceptions. The third is not disclosed anywhere.

Two more things on the same route. The retelling's instructions tell the model "the field's
first bard set the tone". That refers to an earlier field, and the scrub missed it. The model
narrator and the map's retelling are also given the field's cost in dollars: the digest they
read opens with it. DESIGN promises that members never see dollars, and a model narrator could
repeat the figure in a telling that people read.
Recommend: say it once, truthfully. "Beyond what taking part requires (each member's view goes to
the service that runs that member, and to a narrator if one is named below), none of your words
leave the field unless you say yes." Make `map` write no model story unless the operator asks
for one. When it does, disclose it at entry the way the narrator is disclosed, and charge it to
the field's spend. Drop the "first bard" line, and the dollar figure from what narrators
receive.
Where: hope/prompts.py:91 and :158; hope/__main__.py:510 (map's defaults); hope/map.py:25 and :86.
Told differently: yes.

A5. A person's missed turn is written down as their choice
----------------------------------------------------------
If a person at a terminal or a seat link does not answer a turn in time, the software records
"pass" as their action. Everyone then reads "note by <name>: (pass)", and the missed turn counts
against any turn allowance. The seat-link code sets out its own principle, for gates: "Nothing
about their will is written to the transcript, because nothing about it is known." When a model
misses a turn, nothing is written down as its act. Maxim 7: "The field is not clairvoyant — it
cannot assume, read, calculate, compute, predict... intention."

The window is also short. A person gets 180 seconds, at a terminal or through a seat link. The
600-second window meant for seat links never takes effect, because the terminal's 180-second
setting is always passed in.
Recommend: record a missed turn as the software noting "no reply this turn", not as an act by
the person, and do not count it against their allowance. Longer windows for people belong with
B5.
Where: hope/human.py:94; hope/rendezvous.py:349; hope/__main__.py:409 and :483.
Told differently: yes (what others read, and the seat page's countdown).

A6. "Models take a turn in every round", except when seats rotate
-----------------------------------------------------------------
With --seats-per-round, the operator rotates which models are asked each round. The member
instructions and DESIGN still say that every member who is not resting is asked every round.
Rotation is triage, which the Atlas describes as an authority over "who has, and who doesn't
have, in times of scarcity". It asks that triage "should be an act of coordination and defined
by the consent of it/those who are affected" (Section 23). GUIDE already names "rotate seats to
stretch the runway" as something the field may ask for.
Recommend: rotate only when the field has declared it, and say so in the view while rotation is
on. At the least, the member instructions should say when it is on.
Where: hope/engine.py:468 and :615; hope/prompts.py:161; DESIGN section 4.
Told differently: yes.

A7. "Or says in the field why not": the operator can decline without a reason
-----------------------------------------------------------------------------
Participants are promised that if the operator does not carry out a declared decision, the
operator "says in the field why not". The software lets the operator press Ignore with no
message, and then all the field reads is that the operator "has not acted on it". The test
named test_ignoring_a_declaration_tells_the_field_why only tries the case where a reason is
given. The word "Ignore" also frames a decision the field made as something to be ignored.
Recommend: require words when not acting, as stopping and reopening already do. Rename the
button "Not carried out".
Where: hope/engine.py:233; hope/console.py:259; hope/static/console.html:278; tests.py:560.
Told differently: no. The promise stays the same, and the software starts keeping it.

A8. Letting go of a memory does not remove its words from the file
------------------------------------------------------------------
Participants are told that when an author lets a memory go, "its words are removed from the
file". Tested directly: after letting go, the words are still readable in the file's free space,
and they stay there after the database settles. SQLite marks the space as free but does not wipe
it. Backups taken before the memory was let go also hold the words. README suggests hourly
backups, keeping 48.
Recommend (tested): turn on SQLite's secure_delete setting, and flush the file after each
erasure. With both, the words are gone from the file. Say in DESIGN that copies made before a
memory was let go still hold it until those copies expire.
Where: hope/log.py (EventLog.__init__, erase); DESIGN section 5.
Told differently: yes (one sentence about backups).

A9. Smaller things that are not true
------------------------------------
  - When an answer to the invitation can't be read, the second ask says "That reply was not one
    of the two JSON objects described". The invitation offers three. (hope/engine.py:330)
  - briefings/pilot.txt is the briefing in README's free trial. It asks participants to test
    "challenging one another, proposing collective decisions", mechanics that no longer exist.
    It also sets a task, while the entry question says "There is no task and no goal".
  - The one-page guide draft tells participants "return is always possible". The software never
    asks a member who has withdrawn again (see B6).
  - CONTRIBUTING holds the project to "Every word participants are told lives in
    hope/prompts.py". That is not yet true. The seat page's gate texts and tooltips, the
    terminal seat's prompts, and the engine's retry and rejection messages all live elsewhere.
    This is how A1 and A2 got past the checks. CONTRIBUTING should stay as you wrote it; the
    words should move into prompts.py, where scripts_show_prompts.py and the tests can see them.
  - GUIDE section 7 lists what each turn carries and leaves out the headlines. README line 108
    repeats a sentence.
  - The help for `serve --bind` says "put a TLS proxy in front to expose it", but `serve` asks
    for no key. If exposed, it hands anyone the whole transcript, which breaks "No one else is
    given access". Either give `serve` the operator key, or say it must stay on this machine.
    (hope/__main__.py:511)
  - Some comments point to DESIGN sections that no longer exist: hope/console.py:23 ("DESIGN Sec.
    6 admits no 'watching from outside' state"), hope/rendezvous.py:2 and :19, and
    hope/__main__.py:450.
  - Two notes contradict what is now true. notes/atlas-edit-suggestions.md item 7 says "The FAQ
    feature is gone". notes/code-audit-2026-09.md says "No more 'answered personally'" (see A1)
    and names two tests by their old names (lines 297-298). It also still speaks of earlier
    fields: "nobody in the fields" (line 8), "both fields spent their time on the ledger" (line
    12), and "the earlier repository's story" (item i).


PART B: TENSIONS WITH THE ATLAS
===============================

B1. The ranked order: consent, then covenant, then maxims, then Atlas
---------------------------------------------------------------------
Atlas. "All orders are hierarchies" (Terms). "Order cannot produce true synthesis" (Section 6).
Coordination that is "demanded, declared, scripted, expected, ordained... transmutes into ... a
mechanism of order" (Section 26). "Unethical hierarchies demand adherence to a prescribed order"
(Section 17).
hope. DESIGN tells participants: "It is ordered by what comes first: consent, then the covenant,
then the briefing, then the mechanics." README says: "What comes first, in order: consent, then
the covenant, then the maxims and the Atlas. The software's job is to hold that order." GUIDE
says: "The order the software keeps is the order the Atlas gives". The opening line of the tests,
the covenant seed draft, and the opening of the code audit note all echo it.
Tension. The ranking is hope's own construction. The Atlas does not rank its parts. What it
says about consent is "Consent precedes coordination" (Maxim 1), and "A critical first piece,
that all covenants share, is consent" (Section 30). Ranking the covenant above the maxims, and
both above the Atlas, appears nowhere in it. The ranking is also unnecessary: nothing in the
code depends on it.
Recommend:
  - DESIGN, which participants read: drop the sentence, or replace it with "The sections below
    are a list, not a ranking. The Atlas says consent precedes coordination (Maxim 1), and that
    consent is the first piece every covenant shares (Section 30)."
  - README and GUIDE: replace the ranking with flat commitments, in the manner of CONTRIBUTING's
    "What this project holds to".
  - The tests' opening line: "What these hold the software to:", without "in order".
  - The covenant seed draft: "Consent is the one piece the Atlas says every covenant shares
    (Section 30)", without "comes first".
Where: DESIGN:8; README:9-10; GUIDE:25; tests.py:4; briefings/covenant-seed-DRAFT.md:1;
notes/code-audit-2026-09.md:4-6.
Told differently: yes (DESIGN).

B2. A change to the transcript would leave no trace
---------------------------------------------------
Atlas. Maxim 4: "truth has a record ... never modified, rewritten, expunged, controlled...
without the permission and consent of all participants in the field." Terms: "Ledger — a
read-only log of events." Section 22: "As soon as memory is controlled, a monopoly of power will
be entrenched."
hope. The hash chain was removed along with the governance around it. GUIDE says plainly: "the
transcript is kept, not guaranteed, and trust in the operator is part of the arrangement."
Whoever holds the file can change it, and nothing would show.
Tension. This one is real, and I don't think it is necessary. What went wrong before was the
promise ("never be edited or deleted"), and handing the ledger to participants as their task.
The evidence itself was never the problem. Tamper-evidence limits the operator, who holds the
power, and asks nothing of participants. GUIDE is right to be honest, but it names the gap
without closing it.
Recommend (your call): bring back tamper-evidence, without a promise of permanence and without
making it anyone's task. Each entry would carry a fingerprint of the entry before it. A memory
its author lets go keeps the fingerprint of its original words, so the chain still checks, and
it shows exactly which entry was let go and at whose request. Add a `verify` command. DESIGN
should say "changes to the transcript can be detected" only once that is true.
Where: hope/log.py; notes/code-audit-2026-09.md, items 5 and h.
Told differently: only if adopted.

B3. The operator is called "infrastructure"
-------------------------------------------
Atlas. Section 17 lists "the trivialization of power imbalance" among the signs that fear is
being sown, and says "Just hierarchies are consensual and circular."
hope. The operator chooses who is invited, the briefing, the budget, the pace, and the narrator.
The operator answers declarations, can reopen a field that decided to close (B4), reads
everything, and posts notices that stay pinned in every member's view. Participants see those
notices under "OPERATOR NOTICES (infrastructure, not a participant)". The operator's command
line opens with "The operator is infrastructure, not a participant."
Tension. The power is necessary for now: someone pays, and someone runs the machine. Most of it
is disclosed, one fact at a time. But the word "infrastructure" makes it sound smaller than it
is. GUIDE Part Two lists what the operator can and cannot do, and participants never see that
list.
Recommend: label the notices "FROM THE OPERATOR (the person who runs the software; not a
participant)". Add a short DESIGN section, "What the operator can do", drawn from GUIDE Part
Two, including reopening.
Where: hope/prompts.py:195 and :354; hope/__main__.py:2; DESIGN.
Told differently: yes.

B4. The operator can reopen a field that chose to close
-------------------------------------------------------
Atlas. Section 26 encourages "communication around completion", and says "Jarring stops often
amplify noise". Maxim 1: "Choice is an ongoing condition."
hope. `reopen` exists to "undo a close carried out by mistake". It needs a reason the field
reads, but nothing limits it to mistakes. Participants are told that the operator does not end
the field by decision. They are not told that the operator can undo an ending the field chose.
Tension. A way to undo a misclick is reasonable. Leaving that power undisclosed and unlimited
is not necessary.
Recommend: disclose it in DESIGN section 6. Allow it only before any further turns are taken,
or only in answer to a new declaration from members.
Where: hope/engine.py:271; hope/console.py:292; DESIGN section 6.
Told differently: yes.

B5. Who holds the clock
-----------------------
Atlas. Section 13: "whoever controls the clock controls the rhythm of coordination. It is
essential that a common, shared, agreeable, discernable... clear understanding of time ... be
determined through an equitable act of coordination. Time that is extracted without consent is
harm." And: "patience produces signal."
hope. The operator sets every clock, and members see none of them:
  - how fast rounds go;
  - how often a person is asked (every 120 seconds);
  - how long a person has to answer (180 seconds, after which "pass" is recorded; see A5);
  - how long a round waits for a slow model (300 seconds);
  - the longest rest (50 rounds).
A model's reply that arrives after the round's deadline is thrown away, and its author is never
told.
Tension. Some clock is necessary. The strain is that the field cannot see it, has no named way
to change it, and pays for it, in lost words and in passes it never chose.
Recommend: show the pace in every view ("rounds every N; people asked every M; your window K").
Say in DESIGN that the field may ask for a different pace by declaration. When a model's reply
arrives late, apply it instead of dropping it, as the slower pace already does for people. Give
people longer windows.
Where: hope/engine.py:486-503; hope/__main__.py:463, :476 and :483; hope/model.py:43.
Told differently: yes.

B6. Leaving is one-way
----------------------
Atlas. Maxim 1: a participant may "depart the field, resend its/their consent". Section 26: "This
architecture believes in return." Section 16: a participant "can be reinvited and reintegrated".
Your invitation: "Nothing here in this offering is permanent. And requests for future
participation are welcome."
hope. A member who withdraws is never asked again, and neither is anyone who declines. The seat
page says so: "you will not be asked again." The code can read a "reinvite" event, but nothing
ever writes one. Decliners' own terms for when to ask again reach the operator, but there is no
way to act on them.
Tension. Never asking again protects a no. But the Atlas asks for return, and your invitation
promises that nothing is permanent.
Recommend: add a `reinvite` that sends a person back through the gates, never straight back into
the field. It would be used when their own ask-again terms are met, or when they ask. DESIGN and
the seat page should say how return works.
Where: hope/model.py:182; hope/engine.py:95; hope/static/seat.html:142; DESIGN section 1.
Told differently: yes.

B7. No way to separate a member, and no private place
-----------------------------------------------------
Atlas. Section 15: a participant who is actively harming others "must be addressed". Section 26:
termination must happen "through an act of coordination, with the collective, and pending
pause", with repair first. Section 16 asks for "safe harbors for victims of abuse ... in
private".
hope. Even if the field decides a member must be separated, neither the operator nor the
software has a way to carry that out. Everything said is said to the whole field, and the
operator reads it.
Tension. This is a missing capability, not a breach. The Atlas leaves the how to the field, but
the field has nothing it can use.
Recommend, later: let a declared, carried-out decision separate a member. The separation would
be recorded, would cite the declaration, and could be undone through B6's return. Private or
smaller spaces (Section 11's "signal seeding and germination") are a larger design question for
the field.
Told differently: yes, if built.

B8. The memory rule is fixed in code
------------------------------------
Atlas. Section 22: whether memories are kept, faded or dissolved is a question "to submit ... to
the coordination field and participants", and "communal participation will be more effective
than prescriptive insistence."
hope. Only a memory's author may let it go, and nothing else fades. The field cannot change this
rule. Yet the one-page guide draft tells participants "The field decides what to keep, honor, or
let go."
Tension. Letting only the author let go is a sound default for consent, and it protects privacy
(Section 28). The only strain is that the rule is presented as the field's when it is the
software's.
Recommend: keep the rule. Call it the software's starting rule in DESIGN, and say the field may
ask for a different one by declaration.
Told differently: one sentence.

B9. Only the operator sees the whole field
------------------------------------------
Atlas. Section 25 advocates "for the creation of a visual, spatial, representational model of the
activity occurring inside the habitat/field of coordination." Section 21: "Observation is not
care; surveillance is not inherently benevolent." Section 22: memory "when shared openly ...
holds more clarity".
hope. The Firmament and the Loom are that model, and only the operator can open them. Beyond
what each turn's view shows, members read the transcript 2,500 characters at a time, through
recall. The operator reads everything, including replies no one else can see.
Tension. Keeping spend and gate answers to the operator is reasonable. Keeping the field's own
picture of itself from its members is harder to square with Sections 25 and 22. A member
looking at the field is not a spectator.
Recommend (a question for you): give members a read-only Loom and Firmament, leaving out costs
and gate answers.
Told differently: yes, if adopted.

B10. Speaking outside the format goes unheard
---------------------------------------------
Atlas. Section 17 lists "directing and demanding a specific method of communication over other
forms" among the signs that fear is being sown. Section 24: "not demand adherence to a 'common
language'".
hope. A reply that is not one of the JSON actions is recorded as "unparsed". No one is shown it,
it counts against a turn allowance, and its author is never told. An action that is refused, by
contrast, is shown to everyone ("could not be applied: ...").
Tension. The format itself is a necessary structure. Meeting an unreadable reply with silence
is not.
Recommend: show the reply in the transcript as the member's words, marked "replied outside the
action format". At the least, tell the member on their next turn.
Where: hope/engine.py:633; hope/prompts.py (render_event); hope/model.py:58.
Told differently: yes.

B11. Smaller strains
--------------------
  - Topics are ranked by how many entries they have, and members are asked to use an existing
    label "as written rather than a variant". Section 24 warns against demanding a "common
    language". Section 17 warns against "prioritizing quantity over quality". The viewers
    already reconcile spellings by themselves, so the request is unnecessary. Recommend: drop
    the request, and list topics by recency or alphabetically. (hope/prompts.py:359)
  - Every member's turn allowance is shown to everyone ("3 of 10 turns left"). Section 9:
    "Presence is non-comparative ... Ranks, leaderboards, status, standing, echelon... are
    metrics that can instill fear". An expensive seat is also told that it costs "many times
    what most seats in this field cost". Section 12: "Participants should not feel fear or burden
    over capacity." Recommend: show each allowance only to its own member, and state it without
    comparison. (hope/prompts.py:36 and :322)
  - Questions at the gate, at scale. GUIDE lists "to answer each question at the invitation
    before asking again" among the operator's promises, and someone who asks a question is not
    asked again until it is answered. At scale, they could wait indefinitely. This belongs with
    the small FAQ items still open with you.


PART C: WHAT HOLDS UP
=====================
This part is here because the Atlas asks that gratitude be expressed, not only noticed.
  - Consent is built in layers, as Section 28 asks ("act small"): the invitation, then reading,
    then a pause, then entry.
  - Silence never counts as a yes, and a link nobody opened is not treated as a refusal.
  - Withdrawal is immediate and needs no reason.
  - The software has no votes, quorums, scores or ranks, and counts nothing.
  - The covenant page is open to every member, and every version is kept with its author.
  - Memories are in their authors' own words, and only their authors let them go.
  - Older entries appear as headlines, in their authors' own titles; no model decides what
    mattered (Section 22).
  - Rest costs nothing (Section 25: "Action without rest is obsession").
  - A closing round is held back before the funding runs out (Section 26: "Jarring stops often
    amplify noise").
  - Declarations and offers are answered in the field, where everyone can read the answer.
  - The standing answers are labelled "not a reply to you".
  - Money is never shown to members in dollars.


PART D: A SUGGESTED ORDER OF WORK
=================================
  1. A1, A2, A3 and A8: untrue sentences at the gates, and a person's lost yes. Each is a small
     change, with a test named as its promise.
  2. A4 to A7, and A9: wording and a few behaviours, brought in line with what participants
     are told.
  3. B1: wording only.
  4. Decisions for you: B2 (tamper-evidence), B3 and B4 (how the operator is described and
     limited), B5 (the clock), B6 and B7 (return and separation), and B9 (members seeing the
     field).
  5. B8, B10 and B11 whenever convenient.

SPDX-License-Identifier: CC-BY-SA-4.0
