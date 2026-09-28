# SPDX-License-Identifier: AGPL-3.0-or-later
"""A human seat. One person, one presence, the same gates and the same actions as every
other participant. At the gates the connector prints the question and reads the answer from
stdin. After entry nothing is asked of them: they post whenever they like (listen), and "look"
shows what is new since they last looked.

The format is plain text, translated to the same JSON actions models send:

    <text>                          say something (in the domain you last wrote in, or the field itself)
    @domain <text>                  say it in a domain; nest with /, as in @timing/clocks
    in <circle>: <text>             say it in a circle you are in
    to <name>: <text>               name someone (it wakes them, if they allow it)
    [handle] <text>                 a short title first, in square brackets, works with the ones above
    #123 <text>                     reply to entry 123 (say in your own words how)
    look                            what is new since you last looked (terminal only)
    pause [for 3h | until addressed | until news] [/ words for the field]
                                    step back; anything you do ends it
    follow <domain> | follow circle <name> | unfollow ...
    chat with <name> [: first words]              a private circle of two; they are asked in
    form <name> [/ purpose] [/ private: why]      form a circle (open unless you say private)
    join <circle> | leave <circle> | knock <circle> [: note]
    ask <name> into <circle> [: note]
    yes #12 [note] | no #12 <reason>                answer something waiting for you (a no has a reason)
    question <circle>: <text>       put a question to a circle; respond #12 <text> answers one put to yours
    privacy <circle> private <why> | privacy <circle> open
    harvest <circle>: <text>        what a circle learned, for the field (every member's yes sends it)
    remember <text>                 add a memory for the field to carry forward (#ids in it become refs)
    let go 123                      let go of a memory you added; its words are removed
    covenant <text>                 replace the covenant page with <text> (the whole page)
    relabel <label> -> <label>      move your own entries from one domain to another
    declare <what, why, how> [/ in 1h] [/ pause 2h | / pause | / resume | / close | / bring <instrument> |
            / put down <instrument> | / pin <section> | / unpin <section> | / rhythm 15m |
            / friction <declarations|pinned|an act>: notice 1h, yes 1, objections hold | / ask <help> |
            / steward <names> | / unsteward <names> | / move to <a steward>]
                                    announce what will happen; after its notice the software carries out what
                                    you name, and asks the operator's help with "ask"; #ids become citations
    steward yes | steward no [why] | steward step down [note]
                                    answer the field asking you to keep a copy of its record, or step down
    offer <text>                    put an offer of resources before the operator and everyone
    tool <tool>: <words>            use one of the field's tools (the words go to its first text argument;
                                    or give its arguments as JSON after the colon); what came back is shown
    read #123 [part 2] | read tool <tool> | read skill <name>
                                    read on in something long, a tool's full description, or a skill
    offer tool <name> <https address> [reading|working|acting] [: what it does]
                                    offer the field an MCP tool server (put no key in the address)
    remove tool <name>              remove a tool server you offered
    flag tool <tool>: <why>         flag a tool that could change the field in ways no one intended
    unflag #12                      withdraw a flag you put on a tool
    skill <name>: <what it does, and when to use it>
    <the instructions, on the lines after>
                                    write or revise one of the field's skills (your yes to its publication
                                    in hope's repository, attributed to you); retire skill <name> retires it
    recall [briefing|original|transcript|memory|covenant|prior] <words>
                                    re-read matching passages (shown the next time you look; briefing if unnamed)
    journal <text>                  an entry in your own journal (journal open to <names> | journal open to everyone |
                                    journal close | journal erase #12)
    role add <words> | role remove <words> | roles <a>, <b> | roles none
                                    words that say how you take part, shown beside your name; they grant nothing
    tell <story>                    tell the stretch since the last telling, citing entries as [#12]
    play: <text> | play what if: <text> | play wonder: <text> | play try: <text>
                                    offer something as play (Atlas Section 18)
    tag @<domain> <schema> | tag circle <name>: <schema>   (untag the same way, for your own tags)
                                    a schema of play, such as positioning; follow play <schema> follows it
    follow everything               every channel you may read (for storytellers)
    revise briefing: <the words as they stand> ==> <the new words> [// why]
                                    revise the field's edition of the briefing
    withdraw declaration #12 [note] take back a declaration you made, until it takes effect
    withdraw revision #12           take back a revision you proposed to a pinned section, while it waits
    repair: <what happened> [/ with <names>] [/ surrogate <name>] [/ naming <name>]
                                    open a repair thread, known only to those you bring in
    repair #12 with <names> | repair #12 surrogate <name> | repair #12 naming <name>
    repair #12 status resolved|partly resolved|stepping back|open [/ note] | repair #12 widen
    in #12, to them: <text>         in a repair thread, words for the one it concerns (otherwise they stay
                                    with those you brought in to hear it)
    announce: <text> [/ naming <names>]   tell the field you were approached or preyed upon
    invite person <name> [: note] | invite model <id> [: note] | invite agent <address> [: note]
    answer invitee <name>: <text>   answer a question from someone you invited
    instrument <name>: <its words> [/ for decide|adopt|separate] [/ asks circle <name> | / asks <names>] [/ pause 3d]
               [/ yes 2 | / yes everyone] [/ objections hold|heard]
                                    write down one of the field's instruments (in force once a declaration brings it)
    raise <instrument>: <question> [/ about <member>] [/ adopt <instrument>] [/ put down <instrument>]
                                    [/ pause 2h | / close | / rhythm 30m | / ask <help> ..., as a declaration would]
    answer #12 yes | answer #12 stand aside | answer #12 object <why> | answer #12 withdraw
                                    answer a declaration, a revision to a pinned section, or a question under an
                                    instrument (silence is never a yes)
    withdraw question #12           withdraw a question you raised
    read journal <name>             a journal opened to you
    withdraw [reason] [/ when it would be fair to ask you back]
    question <text>                 (invitation gate only)
    yes [statement] | no [reason]   (at either gate; "no ... / ask again when ..." records terms)
    share | share #12 #15 | no      (the closing question: everything, only those entries, or nothing)

The field has no voting commands. To ask the field to decide something, say so in words.
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

from .connector import Reply, Seat, no_reply

_print_lock = threading.Lock()


class HumanConnector:
    """A person's seat. Two modes:

    - terminal (default): the gates and the field print here; you answer and post on stdin.
    - inbox (inbox=path): the field runs in the background, and every line that appears in the
      inbox file is read as something you said. You speak from ANY terminal with the `say`
      command (or by appending a line yourself). No tmux, no attached session.

    Gates have no window here, since the person is at this terminal. After entry nothing is
    asked of them at all: the engine starts `listen`, and they post whenever they like.
    """

    def __init__(self, name: str, hails_from: str, people: str = "human",
                 turn_timeout: float = 900.0, infile=None, outfile=None, inbox: str = None):
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
        low = system.lower()
        gate = "accept_invitation" in low
        delivery = '"received"' in low
        share = '"share"' in low
        entry = ("opt_in" in low) and not gate and not delivery
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
            elif share:
                self._say("Your answer (share | share #12 #15 | no [reason]). No answer shares nothing:")
            else:
                limit = f"window {self.turn_timeout:.0f}s; " if self.turn_timeout else ""
                self._say(f"Your action ({limit}blank = pass; no answer writes nothing as yours). Type 'help' for the format:")
            self._say("> ", end="")
            line = self._read_line(None if (gate or entry or delivery or share) else self.turn_timeout)
        if line is None:
            if share:
                return Reply(json.dumps({"action": "decline", "reason": "no answer, which this question treats as declining to share"}))
            self._say("\n(no reply in time; nothing is written as yours)")
            return no_reply()
        if line.strip().lower() == "help":
            self._say(__doc__)
            return self.ask(seat, system, messages)
        return Reply(json.dumps(translate(line, gate=gate, entry=entry, delivery=delivery, share=share)))

    def close(self) -> None:
        pass

    def listen(self, room, pid: str) -> None:
        """After entry: whenever a line comes, it is posted as theirs. "look" shows what is new,
        "help" the format. Nothing is ever asked of them here."""
        with _print_lock:
            self._say(room.person_view(pid))
            self._say("You are in the field. Nothing is asked of you: type whenever you like. "
                      "'look' shows what is new, 'help' the format.")
        while True:
            line = self._read_line(None)
            if line is None:
                return
            t = visible_text(line)
            if not t:
                continue
            p = room.state().presences.get(pid)
            if not p or p.state != "IN":
                return
            if t.lower() == "help":
                self._say(__doc__)
                continue
            if t.lower() == "look":
                self._say(room.person_view(pid))
                continue
            out = room.post(pid, {"text": t})
            self._say("(kept)" if out.get("ok") else f"(not kept: {out.get('error')})")
            for r in out.get("results") or []:
                self._say(r)

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
            with open(self.inbox, "r", encoding="utf-8") as f:
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


_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-_]")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def visible_text(text: str) -> str:
    """What a person (or a model) actually wrote, without terminal escape codes and control
    characters, so noise pasted from a terminal never becomes words anyone is said to have said."""
    return _CONTROLS.sub("", _ESCAPES.sub("", text or "")).strip()


def translate(line: str, *, gate: bool = False, entry: bool = False, delivery: bool = False,
              share: bool = False) -> dict:
    s = visible_text(line)
    low = s.lower()
    if share:
        # The closing question: everything, only the entries named, or nothing. Anything else is
        # not an answer to it, so the engine asks once more and then takes it as nothing shared.
        if low.startswith("no"):
            return {"action": "decline", "reason": s[2:].strip()}
        if low.startswith("share"):
            ids = [int(x) for x in re.findall(r"#?(\d+)", s[5:])]
            return {"action": "share", "scope": "some", "events": ids} if ids else {"action": "share", "scope": "all"}
        return {"action": "unreadable", "text": s}
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
    if not s or low in ("pass", "quiet"):
        return {"action": "quiet"}
    if low == "pause" or low.startswith("pause "):
        rest, _, note = s[5:].partition("/")
        words = rest.strip().lower()
        d = {"action": "pause", "note": note.strip()}
        m = re.search(r"\bfor\s+(\S+)", words)
        if m:
            d["for"] = m.group(1)
        m = re.search(r"\buntil\s+(addressed|news)\b", words)
        if m:
            d["until"] = m.group(1)
        return d
    for verb in ("follow", "unfollow"):
        if low.startswith(verb + " "):
            rest = s[len(verb) + 1:].strip()
            if rest.lower() == "everything":
                return {"action": verb, "everything": True}
            if rest.lower().startswith("play "):
                return {"action": verb, "play": rest[5:].strip()}
            if rest.lower().startswith("circle "):
                return {"action": verb, "circle": rest[7:].strip()}
            return {"action": verb, "domain": "" if rest.lower() in ("the field", "field") else rest}
    if low.startswith("wake "):
        d = {"action": "wake"}
        words = s[5:].split()
        for i, w in enumerate(words):
            nxt = words[i + 1].lower() if i + 1 < len(words) else ""
            if w.lower() in ("addressed", "replies", "written", "untold", "heartbeat") and nxt in ("on", "off", "yes", "no"):
                d[w.lower()] = nxt in ("on", "yes")
            if w.lower() == "breath" and nxt:
                d["breath"] = nxt
        return d
    if low.startswith("form "):
        parts = [x.strip() for x in s[5:].split("/")]
        d = {"action": "form_circle", "name": parts[0]}
        for extra in parts[1:]:
            if extra.lower().startswith("private"):
                d["private"], d["reason"] = True, extra[7:].lstrip(" :")
            elif extra:
                d["purpose"] = extra
        return d
    for verb, act in (("join ", "join_circle"), ("leave ", "leave_circle")):
        if low.startswith(verb):
            return {"action": act, "circle": s[len(verb):].strip()}
    if low.startswith("knock "):
        circle, _, note = s[6:].partition(":")
        return {"action": "knock", "circle": circle.strip(), "note": note.strip()}
    m = re.match(r"chat\s+with\s+([^:]+)(?::(.*))?$", s, re.I | re.S)
    if m:
        return {"action": "chat", "with": m.group(1).strip(), "content": (m.group(2) or "").strip()}
    m = re.match(r"ask\s+(.+?)\s+into\s+([^:]+)(?::(.*))?$", s, re.I | re.S)
    if m:
        return {"action": "ask", "who": m.group(1).strip(), "circle": m.group(2).strip(), "note": (m.group(3) or "").strip()}
    m = re.match(r"(yes|no)\s+#(\d+)\s*(.*)$", s, re.I | re.S)
    if m:
        yes = m.group(1).lower() == "yes"
        return {"action": "answer", "to": int(m.group(2)), "yes": yes,
                **({"note": m.group(3).strip()} if yes else {"reason": m.group(3).strip()})}
    if low.startswith("question "):
        circle, _, text = s[9:].partition(":")
        return {"action": "ask_circle", "circle": circle.strip(), "question": text.strip()}
    m = re.match(r"respond\s+#?(\d+)\s+(.*)$", s, re.I | re.S)
    if m:
        return {"action": "reply_circle", "question": int(m.group(1)), "text": m.group(2).strip()}
    m = re.match(r"privacy\s+(.+?)\s+(private|open)\b\s*(.*)$", s, re.I | re.S)
    if m:
        return {"action": "privacy", "circle": m.group(1).strip(), "private": m.group(2).lower() == "private",
                "reason": m.group(3).strip()}
    if low.startswith("harvest "):
        circle, _, text = s[8:].partition(":")
        return {"action": "harvest", "circle": circle.strip(), "text": text.strip()}
    m = re.match(r"repair\s*:\s*(.*)$", s, re.I | re.S)
    if m:
        parts = [x.strip() for x in m.group(1).split(" / ")]
        d = {"action": "repair", "account": parts[0]}
        for extra in parts[1:]:
            low_ = extra.lower()
            for key, word in (("ask", "with "), ("surrogate", "surrogate "), ("name", "naming ")):
                if low_.startswith(word):
                    d[key] = [x.strip() for x in extra[len(word):].split(",") if x.strip()]
        return d
    m = re.match(r"repair\s+#?(\d+)\s+(with|surrogate|naming|status|widen)\b\s*(.*)$", s, re.I | re.S)
    if m:
        d = {"action": "repair", "thread": int(m.group(1))}
        verb, rest = m.group(2).lower(), m.group(3).strip()
        if verb == "widen":
            d["widen"] = True
        elif verb == "status":
            status, _, note = rest.partition("/")
            d["status"], d["note"] = status.strip(), note.strip()
        else:
            d[{"with": "ask", "surrogate": "surrogate", "naming": "name"}[verb]] = [x.strip() for x in rest.split(",") if x.strip()]
        return d
    m = re.match(r"in\s+#?(\d+)\s*,?\s*to them\s*:\s*(.*)$", s, re.I | re.S)
    if m:
        return {"action": "contribute", "circle": int(m.group(1)), "content": m.group(2).strip(), "to_named": True}
    m = re.match(r"announce\s*:\s*(.*?)(?:\s+/\s*naming\s+(.+))?$", s, re.I | re.S)
    if m:
        d = {"action": "announce", "text": m.group(1).strip()}
        if m.group(2):
            d["name"] = [x.strip() for x in m.group(2).split(",") if x.strip()]
        return d
    m = re.match(r"invite\s+(person|model|agent)\s+(.+?)(?::\s+(.*))?$", s, re.I | re.S)
    if m:
        return {"action": "invite", m.group(1).lower(): m.group(2).strip(), "note": (m.group(3) or "").strip()}
    m = re.match(r"answer\s+invitee\s+([^:]+):\s*(.*)$", s, re.I | re.S)
    if m:
        return {"action": "answer_question", "presence": m.group(1).strip(), "text": m.group(2).strip()}
    m = re.match(r"instrument\s+([^:]+):\s*(.*)$", s, re.I | re.S)
    if m:
        parts = [x.strip() for x in m.group(2).split(" / ")]
        d = {"action": "instrument", "name": m.group(1).strip(), "text": parts[0]}
        for extra in parts[1:]:
            low_ = extra.lower()
            if low_.startswith("for "):
                d["for"] = extra[4:].strip()
            elif low_.startswith("asks circle "):
                d["circle"] = extra[12:].strip()
            elif low_.startswith("asks ") and low_[5:].strip() not in ("field", "the field", "everyone"):
                d["named"] = [x.strip() for x in extra[5:].split(",") if x.strip()]
            elif low_.startswith("pause "):
                d["pause"] = extra[6:].strip()
            elif low_.startswith("yes "):
                d["yes"] = extra[4:].strip()
            elif low_.startswith("objections "):
                d["objections"] = extra[11:].strip().lower()
        return d
    m = re.match(r"raise\s+([^:]+):\s*(.*)$", s, re.I | re.S)
    if m:
        parts = [x.strip() for x in m.group(2).split(" / ")]
        d = {"action": "raise", "instrument": m.group(1).strip(), "question": parts[0]}
        for extra in parts[1:]:
            low_ = extra.lower()
            for key, word in (("about", "about "), ("adopt", "adopt "), ("decision", "decision ")):
                if low_.startswith(word):
                    d[key] = extra[len(word):].strip()
                    break
            else:
                _effect_words(d, extra)
        return d
    m = re.match(r"steward\s+(yes|no|step\s+down)\b\s*(.*)$", s, re.I | re.S)
    if m:
        verb, rest = " ".join(m.group(1).lower().split()), m.group(2).strip()
        if verb == "step down":
            return {"action": "steward", "step_down": True, "note": rest}
        return {"action": "steward", "yes": verb == "yes", "reason": rest}
    m = re.match(r"withdraw\s+revision\s+#?(\d+)\s*(.*)$", s, re.I | re.S)
    if m:
        return {"action": "withdraw_revision", "revision": int(m.group(1)), "note": m.group(2).strip()}
    m = re.match(r"answer\s+#?(\d+)\s+(yes|stand\s+aside|object|withdraw)\b\s*(.*)$", s, re.I | re.S)
    if m:
        return {"action": "respond", "question": int(m.group(1)), "answer": " ".join(m.group(2).lower().split()),
                "reason": m.group(3).strip()}
    m = re.match(r"withdraw\s+question\s+#?(\d+)\s*(.*)$", s, re.I | re.S)
    if m:
        return {"action": "withdraw_question", "question": int(m.group(1)), "note": m.group(2).strip()}
    m = re.match(r"withdraw\s+declaration\s+#?(\d+)\s*(.*)$", s, re.I | re.S)
    if m:                          # before withdrawing from the field: this takes back a declaration, nothing more
        return {"action": "withdraw_declaration", "declaration": int(m.group(1)), "note": m.group(2).strip()}
    if low.startswith("withdraw"):
        reason, _, again = s[8:].partition("/")
        d = {"action": "withdraw", "reason": reason.strip()}
        if again.strip():
            d["ask_again"] = again.strip()
        return d
    if low.startswith("relabel "):
        rest = s[8:]
        src, sep, dst = rest.partition("->")
        if not sep:
            src, sep, dst = rest.rpartition(" to ")
        return {"action": "relabel", "from": src.strip(), "to": dst.strip()}
    if low == "clock" or low.startswith("clock "):
        words = s[5:].split()
        d, note = {"action": "clock"}, []
        i = 0
        while i < len(words):
            w = words[i].lower().rstrip(":")
            if w in ("between", "every", "window", "linger") and i + 1 < len(words):
                d[w if w in ("window", "linger") else "between"] = words[i + 1]
                i += 2
            else:
                note.append(words[i])
                i += 1
        if note:
            d["note"] = " ".join(note)
        return d
    if low.startswith("remember "):
        text = s[9:].strip()
        return {"action": "remember", "text": text, "refs": [int(x) for x in re.findall(r"#(\d+)", text)]}
    if low.startswith("let go "):
        return {"action": "let_go", "memory": _int(s[7:].strip().lstrip("#"))}
    if low.startswith("covenant "):
        return {"action": "covenant", "text": s[9:].strip()}
    if low.startswith("declare "):
        parts = [x.strip() for x in s[8:].strip().split(" / ")]
        first, _, rest = parts[0].partition(" ")
        if first.lower() in ("close", "pause", "other") and len(parts) == 1:     # earlier versions' form
            return {"action": "declare", "decision": first.lower(), "text": rest.strip(),
                    "refs": [int(x) for x in re.findall(r"#(\d+)", rest)]}
        d = {"action": "declare", "text": parts[0], "refs": [int(x) for x in re.findall(r"#(\d+)", parts[0])]}
        for extra in parts[1:]:
            _effect_words(d, extra)
        return d
    m = re.match(r"journal\s+(open\s+to|close|erase)\b\s*(.*)$", s, re.I | re.S)
    if m:
        verb, rest = m.group(1).lower(), m.group(2).strip()
        if verb == "close":
            return {"action": "journal", "close": True}
        if verb == "erase":
            return {"action": "journal", "erase": _int(rest.lstrip("#"))}
        if rest.lower() == "everyone":
            return {"action": "journal", "open_to": "everyone"}
        return {"action": "journal", "open_to": [x.strip() for x in rest.split(",") if x.strip()]}
    if low.startswith("journal "):
        return {"action": "journal", "text": s[8:].strip()}
    m = re.match(r"role\s+(add|remove)\s+(.+)$", s, re.I | re.S)
    if m:
        return {"action": "role", m.group(1).lower(): m.group(2).strip()}
    m = re.match(r"roles\s+(.+)$", s, re.I | re.S)
    if m:
        rest = m.group(1).strip()
        return {"action": "role", "set": [] if rest.lower() == "none" else [x.strip() for x in rest.split(",") if x.strip()]}
    if low.startswith("tell "):
        return {"action": "tell", "story": s[5:].strip()}
    m = re.match(r"play(?:\s+(what\s*if|wonder|try))?\s*:\s*(.*)$", s, re.I | re.S)
    if m:
        kind = (m.group(1) or "").lower().replace(" ", "")
        domain, text = _domain_prefix(m.group(2))
        return {"action": "contribute", "domain": domain, "content": text.strip(),
                "play": {"whatif": "what-if", "wonder": "wonder", "try": "try"}.get(kind, True)}
    m = re.match(r"(tag|untag)\s+@(\S+)\s+(.+)$", s, re.I | re.S)
    if m:
        return {"action": m.group(1).lower(), "domain": m.group(2), "play": m.group(3).strip()}
    m = re.match(r"(tag|untag)\s+circle\s+([^:]+):\s*(.+)$", s, re.I | re.S)
    if m:
        return {"action": m.group(1).lower(), "circle": m.group(2).strip(), "play": m.group(3).strip()}
    m = re.match(r"revise\s+briefing\s*:\s*(.+?)\s*==>\s*(.*?)(?:\s*//\s*(.*))?$", s, re.I | re.S)
    if m:
        return {"action": "revise_briefing", "passage": m.group(1), "text": m.group(2), "note": (m.group(3) or "").strip()}
    m = re.match(r"read\s+journal\s+(.+)$", s, re.I)
    if m:
        return {"action": "read", "journal": m.group(1).strip()}
    m = re.match(r"tool\s+([\w.-]+)\s*:\s*(.*)$", s, re.I | re.S)
    if m:
        rest = m.group(2).strip()
        try:
            args = json.loads(rest) if rest.startswith("{") else rest
        except ValueError:
            args = rest
        return {"action": "use_tool", "tool": m.group(1), "arguments": args}
    m = re.match(r"read\s+#(\d+)(?:\s+part\s+(\d+))?\s*$", s, re.I)
    if m:
        return {"action": "read", "entry": int(m.group(1)), "part": int(m.group(2) or 1)}
    m = re.match(r"read\s+(tool|skill)\s+(\S+)\s*$", s, re.I)
    if m:
        return {"action": "read", m.group(1).lower(): m.group(2)}
    m = re.match(r"offer\s+tool\s+([\w-]+)\s+(\S+)(?:\s+(reading|working|acting))?\s*(?::\s*(.*))?$", s, re.I | re.S)
    if m:
        return {"action": "offer_tool", "name": m.group(1), "url": m.group(2), "kind": (m.group(3) or "reading").lower(),
                "description": (m.group(4) or "").strip()}
    m = re.match(r"remove\s+tool\s+(\S+)\s*$", s, re.I)
    if m:
        return {"action": "remove_tool", "server": m.group(1)}
    m = re.match(r"flag\s+tool\s+([^:]+):\s*(.*)$", s, re.I | re.S)
    if m:
        return {"action": "flag_tool", "tool": m.group(1).strip(), "reason": m.group(2).strip()}
    m = re.match(r"unflag\s+#?(\d+)\s*$", s, re.I)
    if m:
        return {"action": "unflag_tool", "flag": int(m.group(1))}
    m = re.match(r"retire\s+skill\s+(\S+)\s*$", s, re.I)
    if m:
        return {"action": "skill", "name": m.group(1), "retire": True}
    m = re.match(r"skill\s+([\w-]+)\s*:\s*([^\n]*)\n(.*)$", s, re.I | re.S)
    if m:
        return {"action": "skill", "name": m.group(1), "description": m.group(2).strip(), "text": m.group(3).strip()}
    if low.startswith("offer "):
        return {"action": "offer", "text": s[6:].strip()}
    if low.startswith("rest "):
        n, _, reason = s[5:].strip().partition(" ")
        return {"action": "pause", "until": "news", "note": reason.strip()}
    if low.startswith("recall "):
        rest = s[7:].strip()
        first, _, words = rest.partition(" ")
        if first.lower() in ("briefing", "original", "transcript", "memory", "covenant", "prior") and words.strip():
            return {"action": "recall", "query": words.strip(), "from": first.lower()}
        return {"action": "recall", "query": rest}
    if low.startswith("note "):
        return {"action": "note", "content": s[5:].strip()}
    if low.startswith("move "):
        return {"action": "move", "domain": s[5:].strip()}
    if s[0] in "#+-" and len(s) > 1 and s[1].isdigit():
        # "#123 text" is a reply; "+123"/"-123" were earlier versions' affirm/challenge and are replies now too
        num, _, text = s[1:].partition(" ")
        domain, text = _domain_prefix(text)
        title, text = _title_prefix(text)
        d = {"action": "contribute", "reply_to": _int(num), "domain": domain, "content": text.strip()}
        return {**d, "title": title} if title else d
    extra = {}
    said = s                       # the words as written, if "in ...:" or "to ...:" turns out to name no circle or member
    m = re.match(r"in\s+([^:\n]{1,80}):\s*(.*)$", s, re.I | re.S)
    if m:
        extra["circle"], extra["said"], s = m.group(1).strip(), said, m.group(2)
    m = re.match(r"to\s+([^:\n]{1,120}):\s*(.*)$", s, re.I | re.S)
    if m:
        extra["to"] = [x.strip() for x in m.group(1).split(",") if x.strip()]
        extra["said"], s = said, m.group(2)
    domain, text = _domain_prefix(s)
    title, text = _title_prefix(text)
    # plain words: kept as said, and if they read like another action, a hint follows when they next look
    d = {"action": "contribute", "domain": domain, "content": text.strip(), "plain": True, **extra}
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


def _effect_words(d: dict, extra: str) -> None:
    """One "/ ..." part of a declaration (or of a question for deciding): what the software is to do.
    Anything it does not recognise stays part of the words."""
    low_ = extra.lower().strip()
    if low_ in ("pause", "resume", "close"):
        d[low_] = True
    elif low_.startswith("pause "):
        d["pause"] = extra[6:].strip()
    elif low_.startswith("in "):
        d["when"] = extra[3:].strip()
    elif low_.startswith("bring "):
        d.setdefault("bring", []).append(extra[6:].strip())
    elif low_.startswith("put down "):
        d.setdefault("put_down", []).append(extra[9:].strip())
    elif low_.startswith("pin "):
        d.setdefault("pin", []).append(extra[4:].strip())
    elif low_.startswith("unpin "):
        d.setdefault("unpin", []).append(extra[6:].strip())
    elif low_.startswith("rhythm "):
        d["rhythm"] = extra[7:].strip()
    elif low_.startswith("ask "):
        d["ask"] = extra[4:].strip()
    elif low_.startswith("steward "):
        d.setdefault("steward", []).extend(x.strip() for x in extra[8:].split(",") if x.strip())
    elif low_.startswith("unsteward "):
        d.setdefault("unsteward", []).extend(x.strip() for x in extra[10:].split(",") if x.strip())
    elif low_.startswith("move to "):
        d["move"] = extra[8:].strip()
    elif low_.startswith("move "):
        d["move"] = extra[5:].strip()
    elif low_.startswith("friction "):
        target, _, rest = extra[9:].partition(":")
        f = {"for": target.strip()}
        for bit in rest.split(","):
            word, _, val = bit.strip().partition(" ")
            if word.lower() in ("notice", "yes", "objections") and val.strip():
                f[word.lower()] = val.strip().lower() if word.lower() == "objections" else val.strip()
        d.setdefault("friction", []).append(f)
    else:
        key = "question" if "question" in d else "text"
        d[key] = (d.get(key) or "") + " / " + extra
