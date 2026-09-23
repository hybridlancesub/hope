# SPDX-License-Identifier: AGPL-3.0-or-later
"""A human seat. One person, one presence, the same gates and the same actions as every
other participant. The connector prints the room's view to the terminal and reads a reply
from stdin; if no reply arrives within the turn timeout, the turn is recorded as pass.

The reply format is plain text, translated to the same JSON actions models send:

    <text>                          contribute  (domain = your current one, or "unplaced")
    @domain <text>                  contribute in a domain
    [handle] <text>                 a short title first, in square brackets, works with the two above
    #123 <text>                     reply to entry 123 (say in your own words how)
    remember <text>                 add a memory for the room to carry forward (#ids in it become refs)
    let go 123                      let go of a memory you added; its words are removed
    covenant <text>                 replace the covenant page with <text> (the whole page)
    rest 3 [reason]                 step out for 3 rounds; you are not asked until they pass
    declare close <how> [#ids]      tell the operator the room has decided to close (or: declare pause ...,
                                    declare other ... for anything else it asks the operator to carry out),
                                    saying how, in the way its covenant describes; #ids become citations
    offer <text>                    put an offer of resources before the operator and everyone
    recall [briefing|transcript|memory|covenant|prior] <words>
                                    re-read matching passages (shown next turn; briefing if unnamed)
    pass
    withdraw [reason]
    question <text>                 (invitation gate only)
    yes [statement] | no [reason]   (at either gate; "no ... / ask again when ..." records terms)

The room has no voting commands. To ask the room to decide something, say so in words.
"""
from __future__ import annotations

import json
import os
import re
import select
import sys
import threading
import time
from typing import List, Optional

from .connector import Reply, Seat

_print_lock = threading.Lock()


class HumanConnector:
    """A person's seat. Two modes:

    - terminal (default): the prompt and the view print here; you answer on stdin.
    - inbox (inbox=path): the room runs in the background; when it is your turn it waits
      for a line to appear in the inbox file. You speak from ANY terminal with the `say`
      command (or by appending a line yourself). No tmux, no attached session; if no line
      arrives within the turn timeout, the turn is recorded as pass, exactly as before.
    """

    def __init__(self, name: str, hails_from: str, people: str = "human",
                 turn_timeout: float = 180.0, infile=None, outfile=None, inbox: str = None):
        self.seat = Seat(id="human__" + _slug(name), name=name, hails_from=hails_from, people=people,
                         model="human", pricing={"prompt": 0.0, "completion": 0.0})
        self.turn_timeout = turn_timeout
        self.infile = infile or sys.stdin
        self.outfile = outfile or sys.stdout
        self.inbox = inbox
        self._inbox_pos = 0
        if inbox:
            with open(inbox, "a"):   # exists, and the handle does not outlive this line
                pass
            self._inbox_pos = os.path.getsize(inbox)   # only lines written from now on count

    def seats(self) -> List[Seat]:
        return [self.seat]

    def ask(self, seat: Seat, system: str, messages: List[dict]) -> Reply:
        gate = "accept_invitation" in system.lower()
        delivery = '"received"' in system.lower()
        entry = ("opt_in" in system.lower()) and not gate and not delivery
        with _print_lock:
            self._say("\n" + "=" * 78)
            self._say(system.strip())
            self._say("-" * 78)
            self._say(messages[-1]["content"])
            self._say("-" * 78)
            if gate:
                self._say("Your answer (yes [statement] | no [reason] [/ ask again when ...] | question <text>):")
            elif delivery:
                self._say("Acknowledge receipt (received [note] | no [reason]). You are not being asked to enter yet:")
            elif entry:
                self._say("Your answer (yes [statement] | no [reason]):")
            else:
                limit = f"{self.turn_timeout:.0f}s, " if self.turn_timeout else ""
                self._say(f"Your action ({limit}blank or timeout = pass). Type 'help' for the format:")
            self._say("> ", end="")
            line = self._read_line(self.turn_timeout if not (gate or entry or delivery) else None)
        if line is None:
            self._say("\n(no reply; recorded as pass)")
            return Reply(json.dumps({"action": "pass"}))
        if line.strip().lower() == "help":
            self._say(__doc__)
            return self.ask(seat, system, messages)
        return Reply(json.dumps(translate(line, gate=gate, entry=entry, delivery=delivery)))

    def close(self) -> None:
        pass

    def _say(self, s: str, end: str = "\n") -> None:
        self.outfile.write(s + end)
        self.outfile.flush()

    def _read_line(self, timeout: Optional[float]) -> Optional[str]:
        if self.inbox:
            return self._read_inbox(timeout)
        if timeout is None:
            line = self.infile.readline()
            return line if line else None
        r, _, _ = select.select([self.infile], [], [], timeout)
        if not r:
            return None
        line = self.infile.readline()
        return line if line else None

    def _read_inbox(self, timeout: Optional[float]) -> Optional[str]:
        deadline = None if timeout is None else time.time() + timeout
        while True:
            with open(self.inbox, "r") as f:
                f.seek(self._inbox_pos)
                line = f.readline()
                if line:
                    self._inbox_pos = f.tell()
                    line = line.strip()
                    if line:
                        return line
                    continue    # blank lines are ignored, not passes, in inbox mode
            if deadline is not None and time.time() >= deadline:
                return None
            time.sleep(0.5)


def translate(line: str, *, gate: bool = False, entry: bool = False, delivery: bool = False) -> dict:
    s = line.strip()
    low = s.lower()
    if delivery:
        if low.startswith("no"):
            return {"action": "decline", "reason": s[2:].strip()}
        note = s[8:].strip() if low.startswith("received") else s
        return {"action": "received", "note": note}
    if gate or entry:
        if low.startswith("yes"):
            rest = s[3:].strip()
            d = {"action": "accept_invitation" if gate else "opt_in", "statement": rest}
            if gate and rest.lower().startswith("as "):
                parts = [x.strip() for x in rest[3:].split("/")]
                d["identity"] = {k: v for k, v in zip(("name", "hails_from", "people"), parts) if v}
                d["statement"] = ""
            return d
        if low.startswith("no"):
            rest = s[2:].strip()
            reason, _, again = rest.partition("/")
            d = {"action": "decline", "reason": reason.strip()}
            if gate and again.strip():
                d["ask_again"] = again.strip()
            return d
        if gate and low.startswith("question"):
            return {"action": "question", "content": s[8:].strip()}
        return {"action": "decline", "reason": s or "no answer"}
    if not s or low == "pass":
        return {"action": "pass"}
    if low.startswith("withdraw"):
        return {"action": "withdraw", "reason": s[8:].strip()}
    if low.startswith("remember "):
        text = s[9:].strip()
        return {"action": "remember", "text": text, "refs": [int(x) for x in re.findall(r"#(\d+)", text)]}
    if low.startswith("let go "):
        return {"action": "let_go", "memory": _int(s[7:].strip().lstrip("#"))}
    if low.startswith("covenant "):
        return {"action": "covenant", "text": s[9:].strip()}
    if low.startswith("declare "):
        decision, _, text = s[8:].strip().partition(" ")
        return {"action": "declare", "decision": decision.lower(), "text": text.strip(),
                "refs": [int(x) for x in re.findall(r"#(\d+)", text)]}
    if low.startswith("offer "):
        return {"action": "offer", "text": s[6:].strip()}
    if low.startswith("rest "):
        n, _, reason = s[5:].strip().partition(" ")
        return {"action": "rest", "rounds": _int(n), "reason": reason.strip()}
    if low.startswith("recall "):
        rest = s[7:].strip()
        first, _, words = rest.partition(" ")
        if first.lower() in ("briefing", "transcript", "memory", "covenant", "prior") and words.strip():
            return {"action": "recall", "query": words.strip(), "from": first.lower()}
        return {"action": "recall", "query": rest}
    if low.startswith("note "):
        return {"action": "note", "content": s[5:].strip()}
    if low.startswith("move "):
        return {"action": "move", "domain": s[5:].strip()}
    if s[0] in "#+-" and len(s) > 1 and s[1].isdigit():
        # "#123 text" is a reply; "+123"/"-123" were earlier rooms' affirm/challenge and are replies now too
        num, _, text = s[1:].partition(" ")
        domain, text = _domain_prefix(text)
        title, text = _title_prefix(text)
        d = {"action": "contribute", "reply_to": _int(num), "domain": domain, "content": text.strip()}
        return {**d, "title": title} if title else d
    domain, text = _domain_prefix(s)
    title, text = _title_prefix(text)
    d = {"action": "contribute", "domain": domain, "content": text.strip()}
    return {**d, "title": title} if title else d


def _title_prefix(text: str):
    text = text.strip()
    if text.startswith("[") and "]" in text:
        t, _, rest = text[1:].partition("]")
        return t.strip(), rest
    return None, text


def _domain_prefix(text: str):
    text = text.strip()
    if text.startswith("@"):
        d, _, rest = text[1:].partition(" ")
        return d, rest
    return None, text


def _int(s: str):
    try:
        return int(s.strip())
    except ValueError:
        return None


def _slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
