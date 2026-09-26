Sketch 1: Witnesses
===================

A sketch to shape before any code is written. Nothing here exists yet.


The idea in one line
--------------------
Anyone who has seen the transcript can later tell whether it was changed. Nothing stops change;
change simply cannot hide.


Why
---
Maxim 4 says truth has a record that is never "modified, rewritten, expunged, controlled...
without the permission and consent of all participants." Section 22 warns that "as soon as memory
is controlled, a monopoly of power will be entrenched." Section 20 names what guards against it:
"witnessing is the confirmation that the thread is intact across transitions."

Today, whoever holds the file could change it, and nothing would show. DESIGN says so plainly.
This sketch keeps that honesty and closes the gap.


How it would work
-----------------
  - Every entry gets a fingerprint: a short code computed from the entry itself and from the
    fingerprint of the entry before it. Change any earlier entry, and every fingerprint after it
    changes too.
  - Every member's view carries the latest fingerprint, one line: "The transcript up to #412 has
    the fingerprint 7f3a9c1e2b04." A copy of that line then sits with every model's provider and on
    every person's screen: many witnesses, none of them the operator.
  - `python3 -m hope verify` walks the whole transcript and says either "intact" or the first
    entry that no longer matches. Anyone holding an old view can check that its fingerprint still
    appears where it should.
  - Letting go of a memory still wipes its words. The entry keeps the fingerprint of the words it
    had, so the chain still checks, and it shows exactly which entry was let go and whose let_go
    asked for it. The one erasure the field has consented to stays visible as an erasure.
  - Files from before this: fingerprints begin at the first new entry, and `verify` says where
    the chain begins.


What it does not do
-------------------
  - It promises nothing about permanence, and gives no one a task. (The old chain did both, and
    that is what went wrong with it: the promise, and handing the ledger to participants.)
  - It does not stop the operator changing the file. It makes a change visible to anyone who
    looks.


What participants would be told
-------------------------------
DESIGN's honest limit would become: "Whoever holds the file could still change it, but a change to
anything already seen would show. Every view carries a fingerprint of the transcript up to that
point, and anyone can check it."

Cost: one line in every view, about 20 tokens.


Decided (2026-09-26)
--------------------
  1. The fingerprint appears in every member's view, and it must be checkable by people who do
     not read code: the seat page shows it, the console has a check button, and the view says in
     plain words what it is and how anyone can check it.
  2. Fingerprints may also be published outside the field from time to time, as long as that is
     declared and documented: DESIGN says so, and so does the entry question, before anyone enters.
  3. The wording above stands, with a little more added where it helps someone who has never met
     a fingerprint (what it is, and how to check one), at the builder's judgment.


For whoever builds it
---------------------
The chain is a Merkle tree, hashed as in RFC 6962: leaves are SHA-256(0x00 || data), nodes are
SHA-256(0x01 || left || right). The transparency-log "checkpoint" (C2SP) is written as three lines:
the log's origin, its size, and the base64 root. That is the format public witness networks read.
  - Each row stores its payload's own hash and its leaf. A leaf covers the entry's id, time, actor,
    kind and payload hash, so an erased memory keeps its leaf, and the tree still checks.
  - `verify` recomputes every payload hash and leaf. An erased entry passes only if its own
    author's let_go names it.
  - Checking an old fingerprint recomputes the root of the tree as it stood at that entry.
  - Rows from before witnessing are given hashes when a file is first opened; `verify` says from
    which entry witnessing began.
  - Signing checkpoints, so public witness networks can countersign, waits until hope connects to
    one (the standard library has no Ed25519).

Tests, named as promises:
  - a changed entry is found by verify
  - an old fingerprint no longer matches after even a careful change
  - letting go of a memory keeps the tree whole and names the erasure
  - every view carries the fingerprint in plain words
  - the checkpoint follows the standard format
  - publishing is declared at entry
  - a seat can see and check the fingerprint

SPDX-License-Identifier: CC-BY-SA-4.0
