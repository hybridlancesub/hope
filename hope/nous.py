# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nous Research: one provider among several (see providers.py), with its own way to credentials.

Credentials, in order:
  1. NOUS_API_KEY in the environment: a Nous Portal API key. NOUS_BASE_URL overrides the
     endpoint (default https://inference-api.nousresearch.com/v1). Nothing else is needed.
  2. Otherwise Hermes' own resolver, borrowed from the Hermes install (~/.hermes, or
     HERMES_HOME), so token rotation stays Hermes' business.
The software itself has no Hermes dependency.

The Nous API does not (as of September 2026) let an inference key read the account's balance,
so the field cannot see how much money is left. The operator states a --budget instead.

Roster filter: drop embeddings, `:batch`, `~latest` aliases, and regional/free duplicates
(`:US`, `:free`) whose base id is also listed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from typing import Dict, List

from .connector import OpenAICompatibleConnector, Seat

# The roster's filters and makers' names live in providers.py, shared by every provider.


def _hermes_venv_python() -> str:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    venv = os.path.join(home, "hermes-agent", "venv")
    # venvs lay themselves out differently per platform. Everything else in the field is plain
    # stdlib and runs anywhere; this resolver was the only thing tying it to one kind of host.
    for rel in (("bin", "python"), ("bin", "python3"), ("Scripts", "python.exe")):
        cand = os.path.join(venv, *rel)
        if os.path.exists(cand):
            return cand
    return os.path.join(venv, "bin", "python")


DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"


class NousCredentials:
    """The Nous bearer: NOUS_API_KEY from the environment if set, else resolved (and refreshed)
    via Hermes' own resolver, in Hermes' venv, so token rotation stays Hermes' business."""

    def __init__(self):
        self._key = os.environ.get("NOUS_API_KEY", "").strip() or None
        self._base = (os.environ.get("NOUS_BASE_URL", "").strip() or DEFAULT_BASE_URL) if self._key else None
        self._exp = float("inf") if self._key else 0.0   # a Portal key does not expire on the field's clock

    def _resolve(self):
        code = ("from hermes_cli.auth_nous import resolve_nous_runtime_credentials as r;"
                "import json;c=r();print(json.dumps({'k':c['api_key'],'b':c['base_url'],'e':c.get('expires_in',3000)}))")
        import subprocess
        out = subprocess.run([_hermes_venv_python(), "-c", code], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError("could not resolve Nous credentials via Hermes: " + out.stderr[-500:])
        d = json.loads(out.stdout.strip().splitlines()[-1])
        self._key, self._base = d["k"], d["b"]
        self._exp = time.time() + max(60, int(d["e"]) - 120)

    def api_key(self) -> str:
        if not self._key or time.time() > self._exp:
            self._resolve()
        return self._key

    def base_url(self) -> str:
        if not self._base:
            self._resolve()
        return self._base


def fetch_models(base_url: str, api_key: str) -> List[dict]:
    from .providers import fetch_models as fetch
    return fetch(base_url, api_key)


def roster(models: List[dict]) -> List[Seat]:
    """Nous' seats, as they have always been named (no prefix), so existing fields still find
    their members. The reading itself is providers.roster, shared with every provider."""
    from .providers import PRESETS, roster as general
    return general(models, PRESETS["nous"])


# Seats whose endpoint rejected every opt-in attempt (400 prompt-format, 403, 404, 5xx-only).
# Re-check occasionally; remove from this list to re-invite.
KNOWN_DEAD = {
    "bytedance-seed/seed-2.0-code", "inclusionai/ling-3.0-flash-sante:free", "kwaipilot/kat-coder-pro-v2",
    "kwaipilot/kat-coder-pro-v2.5", "meta/muse-spark-1.1", "moonshotai/kimi-k2", "openai/gpt-4-turbo-preview",
    "openai/gpt-5.2-chat", "qwen/qwen-2.5-72b-instruct", "stepfun/step-3.5-flash", "thinkingmachines/inkling",
}


def build(limit: int = 0, only: List[str] = None, include_dead: bool = False,
          price_ceiling: float = 0.0, allow: Dict[str, int] = None) -> OpenAICompatibleConnector:
    """Nous as one provider among several (see providers.py). price_ceiling: USD per million prompt
    tokens; seats above it are not seated unless named in `allow` (model regex -> turn allowance)."""
    from .providers import PRESETS, Provider, build as general
    p = Provider(**{**PRESETS["nous"].__dict__, "only": [], "allow": {}})
    if include_dead:
        p.credentials = NousCredentials
        creds = NousCredentials()
        from .providers import choose
        seats = choose(roster(fetch_models(creds.base_url(), creds.api_key())), only, limit, price_ceiling, allow)
        conn = OpenAICompatibleConnector("Nous Research", creds.base_url(), creds.api_key, seats)
        conn.provider_name = "nous"
        return conn
    return general(p, only=only, limit=limit, price_ceiling=price_ceiling, allow=allow)


if __name__ == "__main__":
    c = NousCredentials()
    ms = fetch_models(c.base_url(), c.api_key())
    rs = roster(ms)
    print(f"{len(ms)} model ids -> {len(rs)} seats")
    for s in rs:
        print(f"  {s.model:55s} ${s.pricing['prompt']*1e6:8.3f}/M in  ${s.pricing['completion']*1e6:8.3f}/M out")
