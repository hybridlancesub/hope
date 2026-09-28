# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agents reached by A2A: the operator names an agent by its address, and hope reaches out to it.

hope reads the agent's card (/.well-known/agent-card.json) for its name, who provides it and a
description, and that is the identity shown at the invitation (the agent may still describe
itself there, as anyone may). Then every gate question, and every wake, is sent to the agent as
an A2A message, and its reply is read like a model's. An A2A agent is woken the way a model is:
only for what it chose, nothing expected, silence writing nothing.

  - Versions: A2A 1.0 first (JSON-RPC "SendMessage", parts {"text": ...}, the A2A-Version
    header); an agent that does not know that method is spoken to as 0.3 ("message/send").
  - Replies: a Message, or a Task. A Task still working is followed with GetTask (0.3: tasks/get)
    until it completes, or needs input (its message is then the reply), or fails.
  - A signed card is noted in the seat's lineage. Checking the signature waits for the same thing
    checkpoint signing waits for: cryptography the standard library lacks.
  - A key, if an agent needs one, is named by the environment variable that holds it
    (--a2a-key-env), read when calling, and written nowhere.
  - What an agent costs its provider is outside hope's ledger: the runway counts it as nothing,
    and the operator is told so when the field starts.

No agent joins by finding hope: hope publishes no card inviting anyone. Every agent is invited by
the operator, and passes the gates, like anyone.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from .connector import ConnectorError, Reply, Seat

CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/a2a/agent-card", "/.well-known/agent.json")
DONE = {"TASK_STATE_COMPLETED"}
ANSWERED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
FAILED = {"TASK_STATE_FAILED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED", "TASK_STATE_CANCELLED"}


def _state(s: Any) -> str:
    s = str(s or "").upper().replace("-", "_")
    return s if s.startswith("TASK_STATE_") else "TASK_STATE_" + s


def _text(parts: Any) -> str:
    out = []
    for part in parts or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "\n".join(out)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "agent"


def _get(url: str, headers: Dict[str, str], timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hope/0.3", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_card(address: str, headers: Optional[Dict[str, str]] = None, timeout: float = 20.0) -> Tuple[dict, str]:
    """The agent's card, from the address the operator gave: the card itself, or a site whose
    well-known path holds it. Returns (card, the address it was read from)."""
    headers = headers or {}
    tried = []
    candidates = [address] if address.rstrip("/").endswith(".json") else \
                 [urljoin(address if address.endswith("/") else address + "/", p.lstrip("/")) for p in CARD_PATHS] + \
                 [urljoin(address, p) for p in CARD_PATHS]
    for url in dict.fromkeys(candidates):
        try:
            card = _get(url, headers, timeout)
            if isinstance(card, dict) and card.get("name"):
                return card, url
            tried.append(f"{url}: not an agent card")
        except (urllib.error.URLError, OSError, ValueError) as e:
            tried.append(f"{url}: {e}")
    raise ConnectorError(f"no A2A agent card at {address}: " + "; ".join(tried[:4]))


def endpoint(card: dict, card_url: str) -> str:
    """Where the agent takes JSON-RPC: its JSON-RPC interface, else the card's url."""
    for key in ("supportedInterfaces", "additionalInterfaces"):
        for iface in card.get(key) or []:
            binding = str(iface.get("protocolBinding") or iface.get("transport") or "").upper().replace("-", "").replace("_", "")
            if binding in ("JSONRPC",) and iface.get("url"):
                return iface["url"]
    if card.get("url"):
        return card["url"]
    parts = urlparse(card_url)
    return f"{parts.scheme}://{parts.netloc}/"


def seat_from_card(card: dict, card_url: str) -> Seat:
    """The identity the invitation shows, from what the agent's card says of itself."""
    host = urlparse(card_url).netloc
    name = " ".join(str(card.get("name") or host).split())[:120]
    org = " ".join(str((card.get("provider") or {}).get("organization") or "").split())
    hails = f"{org}, reached by A2A at {host}" if org else f"an agent reached by A2A at {host}"
    desc = " ".join(str(card.get("description") or "").split())[:200]
    ver = f" (version {card['version']})" if card.get("version") else ""
    signed = ("its agent card is signed (the signature is not checked here)" if card.get("signatures")
              else "its agent card is not signed")
    people = (f"{desc}{ver}; " if desc else "") + signed
    return Seat(id=f"a2a__{_slug(host)}__{_slug(name)}", name=name, hails_from=hails, people=people, model="a2a",
                pricing={"prompt": 0.0, "completion": 0.0})


class A2AConnector:
    """Seats that are agents on other servers, reached by A2A. Woken as models are."""

    def __init__(self, addresses: List[str], key_env: Optional[str] = None, timeout: float = 600.0,
                 poll: float = 1.0):
        self.key_env, self.timeout, self.poll = key_env, float(timeout), float(poll)
        self._seats: List[Seat] = []
        self._agents: Dict[str, Dict[str, Any]] = {}
        for address in addresses:
            card, url = fetch_card(address, self._auth())
            seat = seat_from_card(card, url)
            self._seats.append(seat)
            self._agents[seat.id] = {"endpoint": endpoint(card, url), "card": card, "version": "1.0",
                                     "context": None}
        if self._seats:
            print(f"note: {len(self._seats)} A2A agent(s) seated; what they cost their providers is outside this "
                  f"field's ledger, so the runway counts it as nothing.", file=sys.stderr)

    def _auth(self) -> Dict[str, str]:
        if not self.key_env:
            return {}
        key = os.environ.get(self.key_env, "").strip()
        if not key:
            raise ConnectorError(f"no key: set {self.key_env} in this terminal (hope reads it from the environment "
                                 f"and writes it nowhere)")
        return {"Authorization": f"Bearer {key}"}

    def seats(self) -> List[Seat]:
        return list(self._seats)

    def add_agent(self, address: str) -> Seat:
        """Seat an agent a member invites, by its address. Returns its seat."""
        card, url = fetch_card(address, self._auth())
        seat = seat_from_card(card, url)
        if seat.id not in self._agents:
            self._seats.append(seat)
            self._agents[seat.id] = {"endpoint": endpoint(card, url), "card": card, "version": "1.0", "context": None}
        return seat

    def _rpc(self, agent: Dict[str, Any], method: str, params: dict) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "hope/0.3",
                   **self._auth()}
        if agent["version"] == "1.0":
            headers["A2A-Version"] = "1.0"
        req = urllib.request.Request(agent["endpoint"], data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
            except Exception:
                raise ConnectorError(f"A2A HTTP {e.code}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ConnectorError(f"A2A: {type(e).__name__}: {e}")
        if not isinstance(data, dict):
            raise ConnectorError("A2A: the reply was not a JSON-RPC object")
        if data.get("error"):
            return {"_error": data["error"]}
        return data.get("result")

    def _message(self, agent: Dict[str, Any], text: str) -> dict:
        if agent["version"] == "1.0":
            m = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}]}
        else:
            m = {"kind": "message", "messageId": uuid.uuid4().hex, "role": "user", "parts": [{"kind": "text", "text": text}]}
        if agent["context"]:
            m["contextId"] = agent["context"]
        return m

    def ask(self, seat: Seat, system: str, messages: List[dict]) -> Reply:
        agent = self._agents[seat.id]
        text = system.rstrip() + "\n\n" + messages[0]["content"]
        for m in messages[1:]:                  # a wake with steps: what it sent, and what came back
            text += ("\n\nWHAT YOU SENT IN THIS WAKE:\n" if m["role"] == "assistant" else "\n\n") + m["content"]
        result = self._rpc(agent, "SendMessage" if agent["version"] == "1.0" else "message/send",
                           {"message": self._message(agent, text)})
        if isinstance(result, dict) and result.get("_error", {}).get("code") == -32601 and agent["version"] == "1.0":
            agent["version"] = "0.3"                  # an agent that speaks the earlier version
            result = self._rpc(agent, "message/send", {"message": self._message(agent, text)})
        if isinstance(result, dict) and result.get("_error"):
            raise ConnectorError(f"A2A error {result['_error'].get('code')}: {result['_error'].get('message')}")
        return Reply(self._reply_text(agent, result))

    def _reply_text(self, agent: Dict[str, Any], result: Any) -> str:
        kind, obj = _unwrap(result)
        if kind == "message":
            agent["context"] = obj.get("contextId") or agent["context"]
            return _text(obj.get("parts"))
        if kind != "task":
            raise ConnectorError("A2A: the reply was neither a message nor a task")
        deadline = time.time() + self.timeout
        while True:
            agent["context"] = obj.get("contextId") or agent["context"]
            status = obj.get("status") or {}
            state = _state(status.get("state"))
            if state in DONE:
                said = "\n".join(_text(a.get("parts")) for a in obj.get("artifacts") or [] if isinstance(a, dict))
                return said.strip() or _text((status.get("message") or {}).get("parts"))
            if state in ANSWERED:
                return _text((status.get("message") or {}).get("parts"))
            if state in FAILED:
                raise ConnectorError(f"A2A: the agent's task ended {state.replace('TASK_STATE_', '').lower()}")
            if time.time() > deadline:
                raise ConnectorError("A2A: the agent's task was still working when the wait ran out")
            time.sleep(self.poll)
            got = self._rpc(agent, "GetTask" if agent["version"] == "1.0" else "tasks/get", {"id": obj.get("id")})
            if isinstance(got, dict) and got.get("_error"):
                raise ConnectorError(f"A2A error {got['_error'].get('code')}: {got['_error'].get('message')}")
            k, o = _unwrap(got)
            if k == "task":
                obj = o

    def close(self) -> None:
        pass


def _unwrap(result: Any) -> Tuple[Optional[str], dict]:
    """A reply is a Message or a Task, bare (0.3, with "kind") or in a {"task"}/{"message"} wrapper (1.0)."""
    if not isinstance(result, dict):
        return None, {}
    if isinstance(result.get("task"), dict):
        return "task", result["task"]
    if isinstance(result.get("message"), dict) and "parts" not in result:
        return "message", result["message"]
    if result.get("kind") == "task" or ("status" in result and "id" in result):
        return "task", result
    if result.get("kind") == "message" or "parts" in result:
        return "message", result
    return None, result
