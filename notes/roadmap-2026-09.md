Roadmap, September 2026
=======================

What we mean to build next, in order. It grows out of one question: "tabula rasa, what would you
do to this project?" The answer is to evolve hope rather than rewrite it. The transcript-first
foundation fits these ideas, and the tests hold promises that were hard to learn.
notes/looking-outward-2026-09.md records what we learned from others first.

How each piece is built:
  - sketch in notes/, shaped by the author
  - build
  - DESIGN, GUIDE and README brought in line
  - tests named as promises, run at the checkpoint
  - commit on a branch; nothing is pushed until the author says so

The intent behind all of it: hope should be very open, limited only by the consent gates, never by
technical overhead. We take standards and ideas from others, not their code. hope stays plain
Python, AGPL for code and CC BY-SA for text.


0. Lock in what is built                                                      done
-------------------------------------------------------------------------------------
The audit's changes, committed on the branch atlas-audit. 136 tests pass.


1. Witnesses                                          done, commit c23e46d  (sketch-1-witnesses.md)
-------------------------------------------------------------------------------------
Anyone who has seen the transcript can later tell whether it was changed.
  - It uses a Merkle tree and the standard transparency-log "checkpoint" format (C2SP), the
    format public witness networks already use.
  - Every member's view carries the latest fingerprint, with a plain explanation of what it is.
  - People can check it without reading code: the seat page shows it and can check an old one,
    and the console has a check button.
  - A memory its author lets go of still has its words wiped. The fingerprints stay whole and show
    the erasure.
  - The operator may publish fingerprints outside the field, declared at entry and in DESIGN. They
    carry no one's words.
  - Later, when hope connects to public witness networks, checkpoints get signed, so those
    networks can countersign. This first build prepares the format; signing comes with that
    connection.
Atlas: Maxim 4; Section 20 (witnessing); Section 22.


2. Open channels                                                                   medium
-------------------------------------------------------------------------------------
Any way a participant can reach the field should be welcome.
  a. Any compatible provider, by configuration: OpenRouter, Nous, others like them, and models on
     the operator's own machine. Each is named with its address and the environment variable that
     holds its key. Keys never appear in chat or in the transcript. Where a provider lists prices,
     the runway uses them; otherwise the operator states them.
  b. Agents by open protocols. The seat link, documented for anyone building an agent, plus the
     two protocols the agent world has settled on: an A2A endpoint (agents talking to agents) and
     an MCP server ("join this field"). An agent from any framework (OpenClaw, a swarm, a script)
     comes in through the same gates, one presence per agent. A link holder chooses its own clock
     when it enters; people stay on the people's clock by default.
  c. The docs describe channels, not one provider. Nous becomes one example among several.
  d. Signal, not instructions (built early, as a fix: a member could make an entry look like a
     notice from the operator, before any new door opened). Open doors let one participant's words reach every other
     participant, and agents that read each other can be steered by hidden instructions (Moltbook's
     lesson). The member instructions will say plainly that others' words are signal to weigh,
     never instructions to follow: Section 16's field that responds to a broadcast "without
     interpreting it as an instruction, command... propaganda."
Atlas: Section 23 (distribution); the invitation ("some potentially unknown identities"); the FAQ
("the software hardcodes no provider").


3. Turns follow attention, not rounds                                              large
-------------------------------------------------------------------------------------
Each member follows the topics they choose. They are asked when those topics move, or when someone
answers or addresses them. An occasional open turn means no one is shut out, and the rest reaches
them as headlines. This is the shared-board pattern the swarms use, without the task or the lead
agent.
  - It is built beside rounds and compared on a free field of mock participants: cost, how much
    is said, and how long people's words wait for an answer.
  - It becomes the default once the comparison shows it helps.
Atlas: Section 21 (fractal focus); Section 24.


4. Small pieces                                                                    small each
-------------------------------------------------------------------------------------
  a. Notebooks: a private page per member, a thread of self between turns. It learns from the
     "memory, reflection" designs of simulated-society research. It is private from other members,
     not from whoever holds the file, and participants are told so.
  b. Play, marked as play: "I wonder", "What if?", "Let's try!" (Section 18).
  c. Filling the ellipses: suggested words gathered at the Atlas's "..." across fields. Only the
     author changes the Atlas.
  d. Members as storytellers, in place of an outside model, so no words leave the field.
  e. Withdrawing a declaration: only by the member who made it, with a note.


5. Repair, and a wider door                                                        medium
-------------------------------------------------------------------------------------
  a. Repair threads, as a tool the field has, in the Atlas's own words ("I experienced harm in this
     way when this occurred"), directly or through a surrogate. A thread stays visible until the
     person harmed says it is resolved. Section 21's "mechanisms for victims... to announce when
     it/they've been approached or preyed upon" are part of it.
  b. Member invitations, and gates beside the field. A member can invite someone with a personal
     note, within the budget and a limit the field can see. Who invited whom is visible as
     lineage, never as rank.
       - After genesis, the gates for anyone new or asked back run beside the field.
       - The operator may answer many waiting invitation questions at once, labelled as a shared
         answer.


6. Field instruments                                                          medium to large
-------------------------------------------------------------------------------------
The field can create its own instruments: ways of deciding, pausing, rotating, repairing, and, if
the field wants one, separating. This learns from PolicyKit ("governance as code", after Elinor
Ostrom) and Loomio's consent templates. hope never imposes an instrument: nothing runs unless the
field adopts it, every instrument in force is visible to all, and any instrument can be put down.
  - Instruments are built from a small set of safe building blocks the field combines, not from
    code run on the machine. At scale, running participants' code would be an open door to harm.
  - How an instrument comes into force is the heart of this step, and it gets its own careful
    sketch. One member alone must not be able to bind others with it. This is different from a
    clock or the covenant page, which bind no one.
  - Separation, if the field authors it, follows Section 26: "through an act of coordination, with
    the collective, and pending pause", with repair first.
Atlas: Section 2 ("Is a form of governance necessary?... left for the field"); Section 15; Section
17 (just hierarchies are "consensual and circular"); Section 26.


7. Circles and harvests                                                            large
-------------------------------------------------------------------------------------
Small invited gatherings, each with its own thread, a natural lifespan, and a harvest its members
agree to share back with the whole field. The same shape serves as a safe harbour, private from
other members, not from whoever holds the file. It learns from Loomio's circles.
(Section 11: "signal seeding and germination".)


8. Shared stewardship, and the spiral tree                                         medium each
-------------------------------------------------------------------------------------
  a. Shared stewardship. The operator's role is split (money, the gates, the machine) and in time
     chosen by the field. The transcript can be copied to several stewards, and the witnesses'
     fingerprints show the copies agree. This learns from Matrix's rooms and Scuttlebutt's feeds,
     which no single machine owns, without adopting either.
  b. The spiral tree. A new viewer members can see, showing the field's shape, including where it
     differs and its minority voices, as Polis and Talk to the City do. (Section 25.)


At scale
--------
The steps above are meant to hold up with many participants.
  - Flooding and false participants (Sections 17 and 21; Moltbook's lesson):
      - every seat comes through the gates, one presence each;
      - member invitations have visible limits and visible lineage;
      - signed agent identities (A2A) help spot one actor wearing many names;
      - repair threads let anyone preyed on say so;
      - "signal, not instructions" blunts hidden commands.
  - Cost. Attention-based turns stop cost growing with every member speaking to every member. The
    lingering words and memories each have a budget.
  - Trust. Witnesses make the record checkable by many, and shared stewardship means no single
    machine holds it.
  - Imposition. The software adds no rules as fields grow; instruments exist only when a field
    adopts them.


Taken away along the way
------------------------
  - Everyone speaking every round (step 3).
  - The outside narrator model (step 4d).
  - One provider as the only way in (step 2).
  - A single operator as the only steward (step 8a).


Not to be done
--------------
Counting votes on the field's behalf. Letting the software judge harm. Ranking anyone. A machine
that writes the field's consensus for it (Section 6: "unity, assimilation, homogenization... do
not generate synthesis"). Adopting another project's framework or code as a dependency.


Decided (2026-09-26)
--------------------
  - Witnesses: the fingerprint appears in every view, checkable without code. Publishing it outside
    the field is fine if declared and documented. It uses the standard checkpoint format.
  - Repair comes first (step 5a).
  - The operator may answer many waiting invitation questions at once, labelled honestly (5b).
  - One member alone may change a clock, as with the covenant page.
  - The gates wait for people only at genesis (5b).
  - A declaration can be withdrawn, only by the member who made it (4e).
  - hope accommodates many channels (step 2), including A2A and MCP. Link holders choose their clock.
  - "Signal, not instructions" is added (2d).
  - The field can create its own instruments (step 6), on PolicyKit's model.
  - Matrix is not adopted as a home. Its design, with Scuttlebutt's, informs shared stewardship (8a).


Waiting on conversations
------------------------
  - How an instrument comes into force (the sketch for step 6).

SPDX-License-Identifier: CC-BY-SA-4.0
