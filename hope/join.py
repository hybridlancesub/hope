# SPDX-License-Identifier: AGPL-3.0-or-later
"""hope join: a participant powers a seat of their own, on their own machine (notes/sketch-10).

    python3 -m hope join SEAT_LINK --provider NAME [--model ID] [--turned] [--no-heartbeat] [--billing-on]
                                   [--providers FILE]
    python3 -m hope join SEAT_LINK --plan chatgpt [--model ID]     (either may add --share-usage)
    python3 -m hope plan status|sign-in|sign-out chatgpt [--another-account | --account EMAIL]
    python3 -m hope usage

A participant who brings an agent of their own ({"action":"invite","own":"<a name>"}) is given a
seat link, shown to them alone. This is the other end of that link: it holds the seat, and asks
the participant's own model what to say, through the participant's own key, plan or machine.

It answers the gates as a seat the software runs answers them: the same system prompt and the same
view (from turn.json), and the same one retry with prompts.RETRY when an answer cannot be read. What
powers the seat is said by this runner when it says yes to the invitation, never left to the model.
Then it takes part in one of two ways, and the person chose which:

  woken by the field   (own key, free tier, local model) It asks the field, through field.json,
                       whether a model the software runs would be woken now, and calls its model
                       only then: when it is named, replied to, or there is news where it follows,
                       on the field's heartbeat (on by default; --no-heartbeat asks the field to
                       leave it out), and on a breath after a day with nothing new.
  turned by its person (a plan, or --turned) It never calls its model because of anything another
                       participant did, the operator's invitation included. It tells its person what
                       is new without calling the model, and calls it only when they press Enter.
                       A plan is lent to one person: OpenAI's terms say "Another user's activity
                       must not trigger requests to the authenticated user's account."

A Claude plan needs none of this: the person's own Claude Code holds the seat by MCP (see
POWERING). hope never asks for, keeps or passes a Claude login.

Secrets. Keys come from environment variables, as everywhere in hope; a ChatGPT sign-in stays in
its owner-only file (hope/chatgpt.py). Neither, and not the seat's token either, is ever printed:
the seat link is a key, so this never shows it except as the person typed it.

Your own usage. What the seat spends is yours, not the field's: the field's ledger counts only what the
operator's providers spend. So this keeps a tally of it here, in your own config folder beside the
ChatGPT sign-in (usage.json: tokens in and out per model per day, and the cost where the provider reports
one), shows it at the end of each turn's line, and sends it nowhere. python3 -m hope usage shows it. With
--share-usage, once a day has ended, its summary is posted to the field as an ordinary entry by the seat.

Ctrl-C leaves quietly: nothing is recorded, and the seat stays in the field until its person
returns with the same command.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import prompts
from .connector import ConnectorError, USER_AGENT
from .engine import DELIVERY_ACTIONS, ENTRY_ACTIONS, INVITATION_ACTIONS, STEP_ACTIONS, _parse, _parse_many
from .human import visible_text
from .model import WAKE_ACTIONS

MAX_STEPS = 8          # times one wake may be asked again with what a tool sent back, or what it read on
WAIT = 60.0            # seconds the field may hold a look open, waiting for something to wake the seat
POLL = 2.0             # seconds between looks at the gates

# The answers each gate takes, as the engine reads them (engine._gate, engine.closing).
GATE_ANSWERS = {"invitation": INVITATION_ACTIONS, "delivery": DELIVERY_ACTIONS, "entry": ENTRY_ACTIONS,
                "share": {"share", "decline"}}
GATE_WORDS = {
    "invitation": "whether it will hear more: the invitation",
    "delivery": "to acknowledge the documentation and briefing it has been given",
    "entry": "whether it enters the field: the entry question",
    "share": "whether its words may be shown elsewhere, now that the field has stopped",
}
ANSWER_WORDS = {"accept_invitation": "yes, it will hear more", "received": "it received them", "opt_in": "it enters",
                "decline": "no", "question": "a question first", "share": "yes, it shares"}

# Why a seat would be woken (prompts.WHY), as its person reads it. {n} is the agent's name.
WHY_FOR_PERSON = {
    "entered": "{n} has just entered the field",
    "addressed": "someone named {n}",
    "reply": "someone replied to {n}",
    "awaiting": "something in a circle waits for {n}'s answer",
    "news": "there are new words where {n} follows",
    "breath": "{n} asked to be woken after a quiet stretch",
    "closing": "the field's funding is ending, and this is {n}'s closing wake",
    "recalled": "{n} asked to recall something",
    "untold": "a stretch of the field has gone untold, and {n} asked to hear of it",
    "asked": "a question under the field's instruments asks {n}",
    "heartbeat": "the field's heartbeat",
    "steward": "the field asks {n} to keep a copy of its record",
    "host": "the field asks {n} to host it",
    "moving": "the field is moving, and what of {n}'s goes with it is {n}'s to say",
    "turned": "you asked",
}

LEFT = ("Left quietly. Nothing was recorded, and the seat stays in the field: run the same command to come back. "
        "To leave the field itself, your agent says so there.")
NOT_ASKED = ("Your agent was not asked, so nothing was sent and no answer is recorded. The question stays open "
             "until its window closes, and the field asks again later; run this command again to be asked.")


class JoinError(Exception):
    """Something the person must hear in plain words, and that stops the runner. Never holds a secret."""


# --------------------------------------------------------------------------- your own usage, on your machine
USAGE_FILE = "usage.json"   # beside the ChatGPT sign-in, in this person's own config folder; sent nowhere


def read_usage(folder: Optional[str] = None) -> Dict[str, Any]:
    """This machine's tally of its person's own usage: {"days": {day: {model: {calls, prompt_tokens,
    completion_tokens, cost_usd?}}}, "shared": [days posted to the field]}. Empty when there is none."""
    from . import chatgpt
    try:
        with open(os.path.join(folder or chatgpt.config_dir(), USAGE_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def count_usage(model: str, prompt_tokens: int, completion_tokens: int, cost: Any = None,
                folder: Optional[str] = None, day: Optional[str] = None) -> Dict[str, Any]:
    """Add one call to this machine's tally, under the person's own day; the cost only where the provider
    reported one. Written only to the owner's own file, never sent. Returns the whole tally."""
    from . import chatgpt
    data = read_usage(folder)
    day = day or time.strftime("%Y-%m-%d")
    m = data.setdefault("days", {}).setdefault(day, {}).setdefault(model or "its model",
                                                                    {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    m["calls"] += 1
    m["prompt_tokens"] += int(prompt_tokens or 0)
    m["completion_tokens"] += int(completion_tokens or 0)
    try:
        if cost is not None:
            m["cost_usd"] = round(float(m.get("cost_usd") or 0.0) + float(cost), 6)
    except (TypeError, ValueError):
        pass
    chatgpt._write_private(os.path.join(chatgpt._folder(folder), USAGE_FILE), data)
    return data


def day_words(models: Dict[str, Any]) -> str:
    """One day's usage in plain words: calls, tokens in and out, per model, and what the provider reported it cost."""
    parts = []
    for model, m in sorted(models.items()):
        cost = f", ${m['cost_usd']:.4f} as its provider reported" if m.get("cost_usd") is not None else ""
        parts.append(f"{m['calls']} call{'s' if m['calls'] != 1 else ''} to {model}, {m['prompt_tokens']:,} tokens in "
                     f"and {m['completion_tokens']:,} out{cost}")
    return "; ".join(parts) or "nothing"


def usage_text(folder: Optional[str] = None) -> str:
    """python3 -m hope usage: the tally kept on this machine, newest day first."""
    data = read_usage(folder)
    days = data.get("days") or {}
    if not days:
        return ("No usage is kept on this machine yet. hope join counts what your own seat spends here, and sends "
                "it nowhere.")
    shared = set(data.get("shared") or [])
    lines = ["Your own usage, as hope join counts it on this machine (it is sent nowhere unless you share it):"]
    for day in sorted(days, reverse=True)[:60]:
        lines.append(f"  {day}: {day_words(days[day])}" + (" (shared with the field)" if day in shared else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- the seat link
def seat_address(link: str) -> Tuple[str, str]:
    """The seat's address (ending in /seat/TOKEN/) and its token, from the link as the person typed it."""
    s = (link or "").strip()
    if "<the field's address>" in s or s.startswith("<"):
        raise JoinError("That link still starts with \"<the field's address>\". Put the field's own address in its "
                        "place: it is the one your own seat link starts with, and the operator can tell you.")
    m = re.match(r"^(https?://[^\s?#]+?/seat/)([A-Za-z0-9_-]{16,})/?(?:mcp/?)?$", s)
    if not m:
        raise JoinError("That is not a seat link. A seat link looks like https://<the field's address>/seat/<its key>/ "
                        "and is shown, to you alone, when you bring an agent of your own: "
                        "{\"action\":\"invite\",\"own\":\"<a name for it>\"}.")
    return m.group(1) + m.group(2) + "/", m.group(2)


class Field:
    """The seat's own pages on the field, over HTTP: turn.json and action (the gates), field.json and
    post (the field), tree.json (what is there, without marking anything seen). Nothing it raises or
    returns holds the seat's token."""

    def __init__(self, link: str, timeout: float = 30.0):
        self.base, self._token = seat_address(link)
        self.timeout = timeout

    def _hide(self, text: Any) -> str:
        return str(text).replace(self._token, "<seat key>")

    def get(self, page: str, query: Optional[dict] = None, wait: float = 0.0) -> Tuple[int, dict]:
        url = self.base + page + (("?" + urllib.parse.urlencode(query)) if query else "")
        return self._send(urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}),
                          self.timeout + wait)

    def post(self, page: str, obj: dict) -> Tuple[int, dict]:
        req = urllib.request.Request(self.base + page, data=json.dumps(obj).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": USER_AGENT})
        return self._send(req, self.timeout)

    def _send(self, req, timeout: float) -> Tuple[int, dict]:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", None) or e
            raise ConnectorError(self._hide(f"could not reach the field ({reason})"))
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            body = {}
        if status == 404 and "not a seat" in str(body.get("error", "")):
            raise JoinError("The field says this link is not a seat. It may have been replaced by a new one; ask "
                            "whoever brought this agent for its current link.")
        return status, body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------- what a model said, as a post
def to_post(text: str) -> Optional[dict]:
    """What a model said, as the post the engine would have applied from a wake: nothing (saying nothing
    writes nothing), plain words as a contribution, or up to WAKE_ACTIONS actions (using a tool and
    reading on are not counted among them), with "next" if it gave one. None when there is nothing to send."""
    acts = _parse_many(text)
    if acts is None:
        plain = visible_text(text or "")
        if plain and not plain.startswith(("{", "[", "`")) and re.search(r"[^\W\d_]", plain):
            return {"action": "contribute", "content": plain}
        return None
    nxt, keep, n = None, [], 0
    for a in acts:
        nxt = a.pop("next", None) or nxt
    for a in acts:
        if a.get("action") in ("quiet", "pass"):
            continue
        if a.get("action") not in STEP_ACTIONS:
            if n >= WAKE_ACTIONS:
                continue                    # one wake carries at most a few actions; the rest are not sent
            n += 1
        keep.append({k: v for k, v in a.items() if k not in ("token", "turn_id")})
    if not keep and not nxt:
        return None
    out: Dict[str, Any] = {"actions": keep}
    if nxt:
        out["next"] = nxt
    return out


def _gist(payload: dict) -> str:
    """A line or two of what was sent, for the person."""
    acts = payload.get("actions") if isinstance(payload.get("actions"), list) else [payload]
    parts = []
    for a in acts:
        words = " ".join(str(a.get("content") or a.get("about") or a.get("tool") or "").split())
        parts.append(a.get("action", "?") + (f": {words[:160]}" + ("..." if len(words) > 160 else "") if words else ""))
    if payload.get("next"):
        parts.append(f"next: {payload['next']}")
    return "; ".join(parts) or "nothing"


def _trains_words(powered: dict) -> str:
    prov = powered.get("provider") or "its provider"
    t = powered.get("trains")
    if t is True:
        return f"{prov} may use what it is sent, other participants' words included, to improve its products"
    if t is False:
        return f"{prov} does not use what it is sent to train its models"
    return f"whether {prov} may use what it is sent to improve its products is not said"


# --------------------------------------------------------------------------- the runner
class Runner:
    """Holds one seat link for its person, with their own model behind it.

    conn and seat are a connector and its one seat (an OpenAICompatibleConnector from
    providers.single_seat, or a chatgpt.ChatGPTPlanConnector). powered is what the seat says powers it:
    route, provider, trains, acts. inp and out are how it talks with its person (input and print by
    default; tests pass their own), and sleep how it waits. Its person's own usage is tallied in
    usage_folder (their config folder by default), and posted once a day only with share_usage."""

    def __init__(self, link: str, conn, seat, powered: dict, *, plan: Optional[str] = None,
                 inp: Callable[[str], str] = input, out: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, wait: float = WAIT, poll: float = POLL,
                 steps: int = MAX_STEPS, heartbeat: bool = True, share_usage: bool = False,
                 usage_folder: Optional[str] = None):
        self.field = Field(link)
        self.conn, self.seat = conn, seat
        self.powered = {k: powered.get(k) for k in ("route", "provider", "trains", "acts")}   # never "by"
        self.turned = self.powered["acts"] == "turned"
        self.plan = plan
        self.inp, self._out, self.sleep = inp, out, sleep
        self.wait, self.poll, self.steps = wait, poll, steps
        self.heartbeat = heartbeat           # False: once in the field, ask not to be woken on its heartbeat
        self.share_usage = share_usage       # True: post a summary of each ended day's usage, by the seat
        self.usage_folder = usage_folder     # where the tally is kept (the person's config folder by default)
        self.stop = threading.Event()
        self.name, self.pid = "your agent", None
        self.answered: set = set()           # gate questions answered (by turn id)
        self.skipped: set = set()            # gate questions its person said no to
        self._said: Dict[str, str] = {}      # the last line said about each thing, so it is not repeated
        self._lock = threading.Lock()

    def out(self, text: str) -> None:
        with self._lock:
            self._out(self.field._hide(text))

    def _once(self, key: str, text: str) -> None:
        if self._said.get(key) != text:
            self._said[key] = text
            self.out(text)

    # -- the person ----------------------------------------------------------------------------------
    def _beside_prompt(self) -> None:
        """OpenAI's words, beside where the person lets a plan-powered seat call (hope/chatgpt.py)."""
        if self.plan == "chatgpt":
            from . import chatgpt
            self.out(chatgpt.using_plan_line())

    def _confirm(self, prompt: str) -> bool:
        """Its person says yes (Enter) or no. Nothing else is taken as either."""
        while not self.stop.is_set():
            self._beside_prompt()
            try:
                said = self.inp(prompt)
            except EOFError:
                return False
            s = (said or "").strip().lower()
            if s == "":
                return True
            if s in ("no", "n", "q", "quit"):
                return False
            self.out("Press Enter to let it go ahead, or type no.")
        return False

    # -- the whole of it -----------------------------------------------------------------------------
    def run(self) -> None:
        try:
            self._tell_what_happens()
            if not self._gates():
                return
            self.out(f"{self.name} is in the field, {prompts.powered_label(self.powered)}.")
            if not self.turned and not self.heartbeat:
                self._no_heartbeat()
            if self.turned:
                self._turned()
            else:
                self._woken()
        except KeyboardInterrupt:
            self.out(LEFT)
        finally:
            self.stop.set()

    def _tell_what_happens(self) -> None:
        if self.turned:
            self.out("Your agent's model is called only when you ask: at each of the field's questions, and whenever "
                     "you press Enter for a turn. Nothing another participant does makes it call.")
        else:
            self.out("Your agent's model is called when the field would wake a model: at the field's questions; "
                     "when it is named, replied to, or there is news where it follows; on the field's heartbeat, "
                     "which is on by default (every 15 minutes unless the field sets another rhythm), "
                     + ("which hope asks the field to leave out for it once it is in; "
                        if not self.heartbeat else "unless you add --no-heartbeat; ")
                     + "and on a breath after a day with nothing new. Each call spends your key. Ctrl-C stops it.")
        if self.powered.get("route") == "local":
            self.out(f"Each call sends what it is shown, other participants' words included, to your local model "
                     f"({self.powered.get('provider') or 'on your machine'}); {_trains_words(self.powered)}.")
        else:
            self.out(f"Each call sends what it is shown, other participants' words included, to "
                     f"{self.powered.get('provider') or 'its provider'} under your own account; "
                     f"{_trains_words(self.powered)}.")

    def _ask(self, system: str, messages: List[dict]):
        """Its model, once. None (and its person told why) when it could not answer."""
        try:
            reply = self.conn.ask(self.seat, system, messages)
            self._tally(reply)
            return reply
        except (OSError, ValueError, http.client.HTTPException) as e:
            self.out(f"Its model could not answer: {type(e).__name__}; nothing was posted.")
            return None
        except ConnectorError as e:
            from .chatgpt import UsageLimitReached
            if isinstance(e, UsageLimitReached):
                self.out(str(e))                 # "Manage usage" first, as OpenAI asks
                self.out("Turns are paused for now. Press Enter for a turn later to try again.")
            else:
                self.out(f"Its model could not answer: {e}")
            return None

    # -- its person's own usage, on this machine -----------------------------------------------------
    def _tally(self, reply) -> None:
        """Count one call in its person's own tally, here; never sent. The cost only where the provider reported one."""
        raw = reply.raw if isinstance(getattr(reply, "raw", None), dict) else {}
        cost = (raw.get("usage") or {}).get("cost") if isinstance(raw.get("usage"), dict) else None
        try:
            count_usage(getattr(self.seat, "model", "") or "its model", getattr(reply, "prompt_tokens", 0),
                        getattr(reply, "completion_tokens", 0), cost, self.usage_folder)
        except OSError as e:
            self._once("tally", f"Your usage could not be kept on this machine ({type(e).__name__}); nothing else changes.")

    def _today(self) -> str:
        """Today's own usage, said at the end of a turn's line."""
        day = (read_usage(self.usage_folder).get("days") or {}).get(time.strftime("%Y-%m-%d"))
        if not day:
            return ""
        ins = sum(m.get("prompt_tokens", 0) for m in day.values())
        outs = sum(m.get("completion_tokens", 0) for m in day.values())
        costs = [m["cost_usd"] for m in day.values() if m.get("cost_usd") is not None]
        return (f" [today, on your own account: {ins:,} tokens in, {outs:,} out"
                + (f", ${sum(costs):.4f} as reported" if costs else "") + "; kept on this machine]")

    def _share(self) -> None:
        """With --share-usage: each ended day's usage not yet shared, posted to the field as an ordinary entry
        by the seat. Without it, nothing of the tally leaves this machine."""
        if not self.share_usage:
            return
        data = read_usage(self.usage_folder)
        today, shared = time.strftime("%Y-%m-%d"), set(data.get("shared") or [])
        for day in sorted(d for d in (data.get("days") or {}) if d < today and d not in shared)[-7:]:
            text = (f"{self.name}'s own usage on {day}, on its person's own account, shared by its person: "
                    f"{day_words(data['days'][day])}.")
            res = self._post({"action": "contribute", "content": text, "domain": "usage", "title": f"usage on {day}"})
            if not (res and res.get("ok")):
                return
            from . import chatgpt
            data = read_usage(self.usage_folder)
            data["shared"] = sorted(set(data.get("shared") or []) | {day})
            try:
                chatgpt._write_private(os.path.join(chatgpt._folder(self.usage_folder), USAGE_FILE), data)
            except OSError:
                return

    # -- the gates -----------------------------------------------------------------------------------
    def _look_at_gates(self) -> dict:
        _, t = self.field.get("turn.json")
        seat = t.get("seat") or {}
        if seat.get("name"):
            self.name, self.pid = " ".join(str(seat["name"]).split()), seat.get("id")
        return t

    def _gates(self) -> bool:
        """Answer the field's questions until the seat is in the field. False if stopped first."""
        while not self.stop.is_set():
            try:
                t = self._look_at_gates()
            except ConnectorError as e:
                self._once("reach", f"{e}; trying again.")
                self.sleep(max(self.poll, 5.0))
                continue
            state = t.get("state")
            if state == "in_field":
                return True
            if state == "your_turn" and t.get("turn_id") not in self.answered | self.skipped:
                self._gate(t)
                continue
            if state == "waiting":
                m = t.get("member") or {}
                where = {"INVITED": "is invited, and waits for the field's next question",
                         "ACCEPTED": "waits for the field's documentation and briefing",
                         "BRIEFED": "waits for the field's documentation and briefing",
                         "RECEIVED": "is pausing before the entry question, as it chose",
                         "OUT": "is not in the field (it declined or left); the field may ask it again"}.get(
                             m.get("state"), "waits for the field's first question")
                self._once("gates", f"{self.name} {where}.")
            self.sleep(self.poll)
        return False

    def _gate(self, t: dict) -> None:
        """One gate question, answered by its own model as a seat the software runs answers it."""
        kind, system, view, tid = t.get("kind"), t.get("system") or "", t.get("view") or "", t.get("turn_id")
        allowed = GATE_ANSWERS.get(kind)
        if allowed is None:
            self.answered.add(tid)          # not a gate; a seat link is not asked to take turns this way
            return
        self.out(f"The field asks {self.name} {GATE_WORDS[kind]}.")
        if self.turned:
            shown = view if len(view) <= 1500 else view[:1500] + f"\n... ({len(view) - 1500} more characters; your agent reads it all)"
            self.out(shown)
            if not self._confirm("Press Enter to let your agent answer, or type no "):
                self.skipped.add(tid)
                self.out(NOT_ASKED)
                return
        msgs = [{"role": "user", "content": view}]
        reply = self._ask(system, msgs)
        if reply is None:
            self.sleep(max(self.poll, 15.0))
            return
        act = _parse(reply.text)
        if not (act and act.get("action") in allowed):
            if self.turned and not self._confirm("Its answer could not be read. Press Enter to let it try once more, "
                                                 "or type no "):
                self.skipped.add(tid)
                self.out(NOT_ASKED)
                return
            msgs += [{"role": "assistant", "content": reply.text or "(empty)"}, {"role": "user", "content": prompts.RETRY}]
            reply = self._ask(system, msgs)
            if reply is None:
                self.sleep(max(self.poll, 15.0))
                return
            act = _parse(reply.text)
        if not (act and act.get("action") in allowed):
            # consent is never assumed: an answer that cannot be read twice is a no, as the engine takes it
            act = {"action": "decline", "reason": "its model's answer could not be read, so it is taken as no"}
        act = {k: v for k, v in act.items() if k not in ("powered", "turn_id", "token")}
        if act["action"] == "accept_invitation":
            act["powered"] = dict(self.powered)      # said by this runner, never left to the model
        try:
            status, res = self.field.post("action", {**act, "turn_id": tid})
        except ConnectorError as e:
            # not answered: the question stays open and is asked again (its model is asked again too)
            self.out(f"{e}; the answer was not sent, and the question will be asked again"
                     + (", by you first." if self.turned else "."))
            self.sleep(max(self.poll, 5.0))
            return
        self.answered.add(tid)
        if res.get("ok"):
            self.out(f"{self.name} answered: {ANSWER_WORDS.get(act['action'], act['action'])}.")
        else:
            self.out(f"The field did not take that answer: {res.get('error') or status}.")

    # -- woken by the field --------------------------------------------------------------------------
    def _woken(self) -> None:
        upto = 0
        while not self.stop.is_set():
            try:
                _, f = self.field.get("field.json", {"as": "model", "since": upto, "wait": self.wait}, wait=self.wait)
            except ConnectorError as e:
                self._once("reach", f"{e}; trying again.")
                self.sleep(max(self.poll, 5.0))
                continue
            if f.get("state") != "in_field":
                self._outside()
                continue
            upto = f.get("upto") or upto
            w = f.get("wake") or {}
            if w.get("due") and f.get("view"):
                self.out(f"Woken: {self._why(w.get('why'))}.")
                self._act(f.get("system") or prompts.SYSTEM_MEMBER, f["view"])
                self._share()

    def _outside(self) -> None:
        """Not in the field just now: a question may be waiting for it (at the field's closing, or asked back)."""
        try:
            t = self._look_at_gates()
        except ConnectorError as e:
            self._once("reach", f"{e}; trying again.")
            t = {}
        if t.get("state") == "your_turn" and t.get("turn_id") not in self.answered | self.skipped:
            self._gate(t)
            return
        self._once("outside", f"{self.name} is not in the field just now.")
        self.sleep(self.poll)

    def _why(self, why: Optional[str]) -> str:
        return WHY_FOR_PERSON.get(why or "", why or "the field").format(n=self.name)

    def _act(self, system: str, view: str) -> None:
        """Ask its model with what it was shown, send what it said, and, when that used a tool or read on,
        ask again with what came back (the step a woken model is shown), at most MAX_STEPS times."""
        msgs = [{"role": "user", "content": view}]
        for step in range(self.steps + 1):
            if self.stop.is_set():
                return
            reply = self._ask(system, msgs)
            if reply is None:
                return
            payload = to_post(reply.text)
            if payload is None:
                self.out(f"{self.name} said nothing this time." + self._today())
                return
            res = self._post(payload)
            if not res or not res.get("next"):
                return
            if step == self.steps:
                self.out(f"{self.name} has used its {self.steps} steps for this wake.")
                return
            if self.turned and not self._confirm(f"{self.name} used a tool or read on, and what came back is ready. "
                                                 f"Press Enter to let it go on, or type no "):
                return
            msgs += [{"role": "assistant", "content": reply.text}, {"role": "user", "content": res["next"]}]

    def _post(self, payload: dict) -> Optional[dict]:
        """Send it, waiting out the field's floor when the field says so, as every model waits."""
        for _ in range(6):
            try:
                status, res = self.field.post("post", payload)
            except ConnectorError as e:
                self.out(f"{e}. What it said was not sent.")
                return None
            if status == 429 and res.get("error") == "floor":
                wait = max(0.0, float(res.get("retry_after") or 1.0))
                self.out(f"The field's floor: {self.name} waits {wait:.0f}s before it acts again, as every model does.")
                self.sleep(wait)
                continue
            if res.get("ok") or res.get("recorded"):
                self.out(f"{self.name}: {_gist(payload)}" + self._today())
            if res.get("error") and res.get("error") != "nothing to record":
                self.out(f"The field said: {res['error']}")
            if res.get("private"):
                # words for this seat's person alone: shown here, in their own terminal, and never to the model
                self.out("For you alone, not for your agent:\n" + str(res["private"]))
            return res

    def _no_heartbeat(self) -> None:
        """Ask the field not to wake this seat on its heartbeat (--no-heartbeat): each wake spends its person's key."""
        try:
            _, res = self.field.post("post", {"action": "wake", "heartbeat": False})
        except ConnectorError as e:
            self.out(f"{e}; the field was not asked to leave out its heartbeat. Run again to ask.")
            return
        if res.get("ok"):
            self.out(f"The field will not wake {self.name} on its heartbeat; it still wakes it when it is named, "
                     f"replied to, or for news where it follows.")
        else:
            self.out(f"The field did not take that: {res.get('error')}")
        return None

    # -- turned by its person ------------------------------------------------------------------------
    def _turned(self) -> None:
        threading.Thread(target=self._watch, daemon=True, name="hope-join-watch").start()
        try:
            while not self.stop.is_set():
                self._beside_prompt()
                try:
                    said = self.inp(f"Enter: let {self.name} take a turn . q: quit ")
                except EOFError:
                    break
                s = (said or "").strip().lower()
                if s in ("q", "quit", "exit"):
                    self.out(LEFT)
                    break
                if s:
                    self.out(f"Press Enter to let {self.name} take a turn, or q to quit.")
                    continue
                self._turn()
        finally:
            self.stop.set()

    def _turn(self) -> None:
        try:
            t = self._look_at_gates()
            if t.get("state") == "your_turn" and t.get("turn_id") not in self.answered:
                self._gate(t)                # the field has a question for it first
                return
            _, f = self.field.get("field.json", {"as": "model", "turn": 1})
            for _ in range(6):
                wait = (f.get("wake") or {}).get("retry_after")
                if not wait or self.stop.is_set():
                    break
                # sooner than the floor after its last wake: it waits it out, as every model does
                self.out(f"The field's floor: {self.name} waits {float(wait):.0f}s before it acts again, as every "
                         f"model does.")
                self.sleep(float(wait) + 0.05)
                _, f = self.field.get("field.json", {"as": "model", "turn": 1})
        except ConnectorError as e:
            self.out(f"{e}. Press Enter to try again.")
            return
        if f.get("state") != "in_field":
            self.out(f"{self.name} is not in the field just now, so it takes no turn.")
            return
        if not (f.get("wake") or {}).get("due") or not f.get("view"):
            self.out(str((f.get("wake") or {}).get("says") or "The field gave no view just now.")
                     + " Press Enter to try again.")
            return
        self.out(f"{self.name} is taking a turn.")
        self._act(f.get("system") or prompts.SYSTEM_MEMBER, f.get("view") or "")
        self._share()

    def _watch(self) -> None:
        """What is new, in a line or two, without calling the model: who wrote, and whether anyone named or
        answered the agent. A look as a model, without a turn, gives no view and records nothing."""
        upto: Optional[int] = None
        while not self.stop.is_set():
            try:
                _, f = self.field.get("field.json", {"as": "model", "since": upto or 0, "wait": self.wait}, wait=self.wait)
            except ConnectorError:
                self.sleep(max(self.poll, 5.0))
                continue
            if self.stop.is_set():
                return
            if f.get("state") != "in_field":
                self._once("outside", f"{self.name} is not in the field just now (the field may have a question for "
                                      f"it: press Enter to see).")
                self.sleep(self.poll)
                continue
            self._said.pop("outside", None)
            newest = int(f.get("upto") or 0)
            why = next((k for k, v in prompts.WHY.items() if v in str((f.get("wake") or {}).get("says") or "")), None)
            news = self._news(upto or 0, first=upto is None) if newest > (upto or 0) else ""
            lines = [news]
            # "entered" is what the field says of a seat that has not yet taken a turn (its person knows that),
            # and a naming already said in the news is not said twice
            if why and why not in ("turned", "entered") and not (why == "addressed" and f"named {self.name}" in news):
                said = self._why(why)
                lines.append(said[0].upper() + said[1:] + ".")
            upto = newest
            text = " ".join(x for x in lines if x)
            if text and text != self._said.get("news"):
                self._said["news"] = text
                self.out(text + f" ({self.name} acts only when you press Enter.)")

    def _news(self, since: int, first: bool = False) -> str:
        try:
            _, tree = self.field.get("tree.json")
        except ConnectorError:
            return ""
        branches = tree.get("branches") or []
        leaves = [x for b in branches for x in b.get("leaves") or []]
        mine = {x["id"] for x in leaves if x.get("by") == self.pid}
        new = [x for x in leaves if x.get("id", 0) > since and x.get("by") != self.pid]
        if not new:
            return ""
        # the tree shows each branch's latest entries only; where all it shows are new, there may be more
        more = any(b.get("entries", 0) > len(b.get("leaves") or []) and
                   min((x.get("id", 0) for x in b.get("leaves") or []), default=0) > since for b in branches)
        who = sorted({x.get("who") or "someone" for x in new})
        s = (f"{'In the field so far' if first else 'New in the field'}: {'at least ' if more else ''}{len(new)} "
             f"entr{'y' if len(new) == 1 else 'ies'}, from {', '.join(who)}.")
        named = sorted({x.get("who") or "someone" for x in new
                        if re.search(rf"(?<!\w){re.escape(self.name)}(?!\w)", str(x.get("title") or ""), re.I)})
        if named:
            s += f" {', '.join(named)} named {self.name}."      # the name is in the opening words of what they wrote
        answered = sorted({x.get("who") or "someone" for x in new if x.get("reply_to") in mine})
        if answered:
            s += f" {', '.join(answered)} answered {self.name}."
        return s


# --------------------------------------------------------------------------- building the seat
def choose(options: List[Tuple[str, str]], inp: Callable[[str], str], out: Callable[[str], None], what: str) -> str:
    """Let the person pick one of (id, label), by number; Enter takes the first."""
    if not options:
        raise JoinError(f"There are no {what} to choose from.")
    out(f"Choose {what}:")
    for i, (_, label) in enumerate(options, 1):
        out(f"  {i}. {label}")
    while True:
        try:
            said = (inp(f"Number (Enter for 1): ") or "").strip()
        except EOFError:
            raise JoinError(f"No choice was made. Name one with --model ID.")
        if not said:
            return options[0][0]
        if said.isdigit() and 1 <= int(said) <= len(options):
            return options[int(said) - 1][0]
        out(f"Type a number from 1 to {len(options)}.")


def _local_name(label: str, name: str) -> str:
    """A local preset's label names the operator's machine; on a participant's machine it is just the app."""
    m = re.search(r"\(([^()]+)\)\s*$", label or "")
    return m.group(1) if m else (name or label)


GEMINI_BILLING = ("Is billing on for this key's Google Cloud project? Without billing, Google may use what your seat "
                  "is sent, other participants' words included, to improve its products. [y/N] ")
GEMINI_EEA = ("Google's terms allow only paid keys where participants are in the EEA, Switzerland or the UK; there, "
              "use gemini, with billing on.")


def provider_seat(p, model: Optional[str], turned: bool, inp=input, out=print, billing_on: bool = False):
    """A seat on the participant's own key, a free tier, or a local model (providers.single_seat).
    Returns (connector, seat, powered). Route: "local" on a local provider; "free-tier" on a free tier
    (or where the provider may train on what it is sent); otherwise "own-key".

    gemini and gemini-free read the same key, and hope cannot tell whether billing is on, which is what
    "does not train" rests on. So on gemini the person says so (or --billing-on); anything but yes,
    including no terminal to ask at, is taken as the free tier, the cautious answer, and said."""
    from . import providers
    if p.name == "gemini" and not billing_on:
        try:
            said = (inp(GEMINI_BILLING) or "").strip().lower()
        except EOFError:
            said = ""
        if said not in ("y", "yes"):
            import dataclasses
            free = providers.PRESETS["gemini-free"]       # the same key and address, said as the free tier
            p = dataclasses.replace(p, name=free.name, label=free.label, id_prefix=free.id_prefix, trains=True, free=True)
            out("Taken as a free key: the seat is on gemini-free, and says to everyone that Google may use what it is "
                "sent. With billing on, run again and answer yes, or add --billing-on.")
    if p.name == "gemini-free":
        out(GEMINI_EEA)
    if not model:
        try:
            creds = providers.credentials(p)
            listed = providers.roster(providers.fetch_models(creds.base_url(), creds.api_key()), p)
        except (RuntimeError, ConnectorError) as e:
            raise JoinError(str(e))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise JoinError(f"{p.name}: could not read its list of models ({getattr(e, 'reason', e)}). "
                            f"Name one with --model ID.")
        model = choose([(s.model, s.model + ("" if s.powered.get("trains") is not True
                                             else "  (its provider may train on what it is sent)")) for s in listed],
                       inp, out, f"a model from {p.label}")
    route = "free-tier" if (not p.local and providers.trains(model, p) is True) else None
    try:
        conn, seat = providers.single_seat(p, model, route=route, acts="turned" if turned else "woken")
    except (ValueError, RuntimeError, ConnectorError) as e:
        raise JoinError(str(e))
    powered = dict(seat.powered)
    if powered.get("route") == "local":
        powered["provider"] = _local_name(p.label, p.name)
    return conn, seat, powered


def chatgpt_seat(model: Optional[str], inp=input, out=print, issuer: Optional[str] = None, api: Optional[str] = None,
                 folder: Optional[str] = None, open_browser: Optional[Callable] = None):
    """A seat on the person's own ChatGPT plan, turned by them. Signs in first when needed (the sign-in
    prints OpenAI's one-time welcome itself), and lets them choose a model. Returns (connector, seat, powered)."""
    from . import chatgpt
    import webbrowser
    where = {"issuer": issuer or chatgpt.ISSUER, "folder": folder}
    try:
        s = chatgpt.status(**where)
        if not (s["signed_in"] and s["plan_usage"]):
            r = chatgpt.sign_in(**where, open_browser=open_browser or webbrowser.open, say=out)
            if not r["plan_usage"]:
                raise JoinError("Plan usage is off, so your seat cannot use your ChatGPT plan.")
        display = model
        if not model:
            found = chatgpt.models(where["issuer"], api or chatgpt.API, folder)
            model = choose([(m["slug"], m.get("display_name") or m["slug"]) for m in found], inp, out, "a ChatGPT model")
            display = next((m.get("display_name") for m in found if m["slug"] == model), None) or model
    except ConnectorError as e:
        raise JoinError(str(e))
    conn = chatgpt.ChatGPTPlanConnector(model, display, issuer=where["issuer"], api=api or chatgpt.API, folder=folder)
    return conn, conn.seats()[0], dict(chatgpt.POWERED)       # the field says "on Ana's ChatGPT plan"


def claude_words(link: str) -> str:
    """A Claude plan powers a seat through the person's own Claude Code, not through hope."""
    base, _ = seat_address(link)
    return ("A Claude plan powers a seat through your own Claude Code, connected to the seat; hope runs nothing for it "
            "and never asks for, keeps or passes a Claude login. In a terminal where Claude Code is signed in to your "
            "plan:\n"
            f"  claude mcp add --transport http hope {base}mcp\n"
            "Then ask it to look at the field and post when you like. It answers the field's questions through the same "
            "seat, and says what powers it when it says yes to the invitation: "
            "\"powered\": {\"route\": \"own-plan\", \"provider\": \"Claude\", \"trains\": null, \"acts\": \"turned\"}.")


# --------------------------------------------------------------------------- the commands
def cli(args, inp=input, out=print) -> int:
    """python3 -m hope join SEAT_LINK ... (the flags in hope/__main__.py)."""
    link = args.link
    plan = (args.plan or "").lower() or None
    try:
        Field(link)                          # a seat link, or plain words saying what one looks like
        if plan == "claude":
            out(claude_words(link))
            return 0
        if plan and args.join_provider:
            raise JoinError("Choose one: --plan (your own plan) or --provider (your own key or local model).")
        if not plan and not args.join_provider:
            raise JoinError("Say what powers your seat: --plan chatgpt, or --provider NAME [--model ID] (such as groq, "
                            "gemini, gemini-free, openrouter, ollama or lmstudio). For a Claude plan: --plan claude. "
                            "Anyone in the field can read how each works: {\"action\":\"read\",\"about\":\"powering\"}.")
        if plan and plan != "chatgpt":
            raise JoinError(f"hope knows no plan named {plan!r}. ChatGPT plans sign in here (--plan chatgpt); a Claude "
                            f"plan uses your own Claude Code (--plan claude says how).")
        field = Field(link)
        field.get("turn.json")                   # the link is a seat before anyone signs in or spends anything
        host = urllib.parse.urlsplit(field.base).hostname or ""
        if field.base.startswith("http://") and host not in ("127.0.0.1", "localhost", "::1"):
            out("Note: this link is not https, so the seat's key travels unencrypted. Ask the operator for the "
                "field's https address.")
        if plan == "chatgpt":
            conn, seat, powered = chatgpt_seat(args.join_model, inp, out)
        else:
            from . import providers
            files = args.join_providers or getattr(args, "providers", None)
            listed = providers.load(files) if files else []
            try:
                p = providers.named(args.join_provider, listed)
            except SystemExit as e:
                raise JoinError(str(e))
            conn, seat, powered = provider_seat(p, args.join_model, args.turned, inp, out,
                                                billing_on=bool(getattr(args, "billing_on", False)))
    except (JoinError, ConnectorError, ValueError, OSError) as e:
        out(str(e))
        return 2
    except KeyboardInterrupt:
        out(LEFT)
        return 0
    try:
        Runner(link, conn, seat, powered, plan=plan, inp=inp, out=out,
               heartbeat=not getattr(args, "no_heartbeat", False),
               share_usage=bool(getattr(args, "share_usage", False))).run()
    except (JoinError, ConnectorError) as e:
        out(str(e))
        return 2
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


def plan_cli(action: str, which: str, another_account: bool = False, out=print, account: Optional[str] = None) -> int:
    """python3 -m hope plan status|sign-in|sign-out chatgpt."""
    from . import chatgpt
    if which != "chatgpt":
        out("hope signs in to ChatGPT plans only. A Claude plan powers a seat through your own Claude Code: "
            "python3 -m hope join <seat link> --plan claude says how.")
        return 2
    try:
        if action == "status":
            out(chatgpt.status_text())
        elif action == "sign-in":
            chatgpt.sign_in(say=out, another_account=another_account, account_email=account)
        elif action == "sign-out":
            chatgpt.sign_out(say=out)
    except ConnectorError as e:
        out(str(e))
        return 1
    except KeyboardInterrupt:
        out("Stopped. Nothing was saved.")
        return 1
    return 0
