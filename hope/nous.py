# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nous Research: one provider among several (see providers.py), reached like any other.

Credentials: NOUS_API_KEY in the environment, a Nous Portal API key, read each time it is needed
and kept nowhere else. NOUS_BASE_URL overrides the endpoint (default
https://inference-api.nousresearch.com/v1). Nothing else is needed, and nothing else is used:
hope does not borrow another app's login (earlier versions fell back to the Hermes app's own
credential resolver; that route is gone, because a login one app was given is not another's to use).

The Nous API does not (as of September 2026) let an inference key read the account's balance,
so the field cannot see how much money is left. The operator states a --budget instead.

Roster filter: drop embeddings, `:batch`, `~latest` aliases, and regional/free duplicates
(`:US`, `:free`) whose base id is also listed.
"""
from __future__ import annotations

import os
from typing import Dict, List

from .connector import OpenAICompatibleConnector, Seat

# The roster's filters and makers' names live in providers.py, shared by every provider.

DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"


class NousCredentials:
    """The Nous bearer: NOUS_API_KEY, read from the environment each time it is needed."""

    def api_key(self) -> str:
        key = os.environ.get("NOUS_API_KEY", "").strip()
        if not key:
            raise RuntimeError("nous: no key. Set NOUS_API_KEY to a Nous Portal API key in this terminal (hope "
                               "reads it from the environment and writes it nowhere). hope never borrows another "
                               "app's login, so a Hermes sign-in is not used.")
        return key

    def base_url(self) -> str:
        return os.environ.get("NOUS_BASE_URL", "").strip() or DEFAULT_BASE_URL


def fetch_models(base_url: str, api_key: str) -> List[dict]:
    from .providers import fetch_models as fetch
    return fetch(base_url, api_key)


def roster(models: List[dict]) -> List[Seat]:
    """Nous' seats, as they have always been named (no prefix), so existing fields still find
    their participants. The reading itself is providers.roster, shared with every provider."""
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
    from .providers import build as general, named
    return general(named("nous"), only=only, limit=limit, price_ceiling=price_ceiling, allow=allow,
                   drop=set() if include_dead else None)


if __name__ == "__main__":
    c = NousCredentials()
    ms = fetch_models(c.base_url(), c.api_key())
    rs = roster(ms)
    print(f"{len(ms)} model ids -> {len(rs)} seats")
    for s in rs:
        print(f"  {s.model:55s} ${s.pricing['prompt']*1e6:8.3f}/M in  ${s.pricing['completion']*1e6:8.3f}/M out")
