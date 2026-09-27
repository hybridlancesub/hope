# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model providers: the doors through which hope reaches models. Any service that speaks the
common OpenAI-compatible format can be one, and models on the operator's own machine too.

Presets:
  openrouter   OpenRouter, hundreds of models from many makers. Key: OPENROUTER_API_KEY.
  nous         Nous Research inference. Key: NOUS_API_KEY, or Hermes' own resolver (see nous.py).
  ollama       models on this machine through Ollama (http://localhost:11434/v1). No key, no cost.
  lmstudio     models on this machine through LM Studio (http://localhost:1234/v1). No key, no cost.

Any other compatible service, or a preset at another address, goes in a providers file:

    {"providers": [
      {"name": "openrouter", "only": ["deepseek/", "anthropic/"], "price_ceiling": 5},
      {"name": "my-service", "base_url": "https://example.org/v1", "key_env": "MY_SERVICE_KEY",
       "label": "Example", "price": {"prompt": 1.0, "completion": 4.0}}
    ]}

A key is only ever named, by the environment variable that holds it. hope reads it from the
environment when it calls, and writes it nowhere: not the providers file, not the transcript.

Prices: where a provider lists them (OpenRouter and Nous do, per token, in the same shape), the
runway uses them. Models on the operator's own machine cost nothing. For anything else, the
operator states a price in USD per million tokens ("price" in the file); without one its spend
counts as nothing, and hope says so when it starts.

Seat ids: a provider's seats are named "<provider>__<model>", so the same model through two
providers is two seats, each asked separately. Nous seats keep the unprefixed ids they have
always had, so fields that began with them still find their members.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .connector import OpenAICompatibleConnector, Seat

EMBED_HINTS = ("embed", "bge-", "e5-", "gte-", "minilm", "mpnet", "voyage", "pplx-embed", "relace-search")
NON_CHAT_HINTS = ("gpt-audio", "voxtral", "-image", "safeguard")

MAKERS = {
    "anthropic": "Anthropic", "openai": "OpenAI", "google": "Google", "meta": "Meta", "meta-llama": "Meta",
    "qwen": "Alibaba Qwen", "deepseek": "DeepSeek", "x-ai": "xAI", "z-ai": "Z.ai", "moonshotai": "Moonshot",
    "mistralai": "Mistral", "minimax": "MiniMax", "nvidia": "NVIDIA", "inception": "Inception",
    "inclusionai": "inclusionAI", "meituan": "Meituan", "bytedance-seed": "ByteDance Seed",
    "cohere": "Cohere", "amazon": "Amazon", "stepfun": "StepFun", "tencent": "Tencent",
    "thinkingmachines": "Thinking Machines", "xiaomi": "Xiaomi", "upstage": "Upstage",
    "poolside": "poolside", "kwaipilot": "Kwaipilot", "nex-agi": "Nex AGI", "aion-labs": "Aion Labs",
    "arcee-ai": "Arcee", "ibm-granite": "IBM", "sakana": "Sakana", "rekaai": "Reka",
}


@dataclass
class Provider:
    name: str                                  # how the operator names it: --provider NAME
    label: str                                 # how "hails from" reads: "DeepSeek via OpenRouter"
    base_url: str
    key_env: Optional[str] = None              # the environment variable holding its key; None = no key
    local: bool = False                        # on the operator's own machine: no key needed, no cost
    id_prefix: str = ""                        # prefix on seat ids, so routes stay distinct
    price: Optional[Dict[str, float]] = None   # USD per million tokens, when the provider lists none
    only: List[str] = field(default_factory=list)
    limit: int = 0
    price_ceiling: float = 0.0
    allow: Dict[str, int] = field(default_factory=dict)
    credentials: Optional[Callable] = None     # a special resolver (Nous' Hermes path), else the env var


PRESETS: Dict[str, Provider] = {
    "openrouter": Provider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                           id_prefix="openrouter__"),
    "nous": Provider("nous", "Nous Research inference", "https://inference-api.nousresearch.com/v1", "NOUS_API_KEY"),
    "ollama": Provider("ollama", "the operator's own machine (Ollama)", "http://localhost:11434/v1", None,
                       local=True, id_prefix="ollama__"),
    "lmstudio": Provider("lmstudio", "the operator's own machine (LM Studio)", "http://localhost:1234/v1", None,
                         local=True, id_prefix="lmstudio__"),
}


class EnvKey:
    """A key read from one environment variable each time it is needed, and kept nowhere else."""

    def __init__(self, provider: Provider):
        self.provider = provider

    def api_key(self) -> str:
        p = self.provider
        if not p.key_env:
            return "none"                     # local servers accept any bearer, or none
        key = os.environ.get(p.key_env, "").strip()
        if not key:
            raise RuntimeError(f"{p.name}: no key. Set {p.key_env} in this terminal (hope reads it from the "
                               f"environment and writes it nowhere).")
        return key

    def base_url(self) -> str:
        return self.provider.base_url


def credentials(p: Provider):
    if p.credentials:
        return p.credentials()
    if p.name == "nous":
        from .nous import NousCredentials       # a Portal key, or Hermes' resolver
        return NousCredentials()
    return EnvKey(p)


def fetch_models(base_url: str, api_key: str) -> List[dict]:
    req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json",
                                          "User-Agent": "hope/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("data", [])


def _per_token(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def roster(models: List[dict], p: Provider) -> List[Seat]:
    """One seat per distinct chat model. Embeddings, batch and alias ids, non-text models, and
    regional or free duplicates of a listed model are left out."""
    ids = {m["id"] for m in models if isinstance(m, dict) and m.get("id")}
    seats: List[Seat] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or ""
        low = mid.lower()
        if not mid or mid.startswith("~") or ":batch" in low:
            continue
        if any(h in low for h in EMBED_HINTS) or any(h in low for h in NON_CHAT_HINTS):
            continue
        out_mod = (m.get("architecture") or {}).get("output_modalities")
        if out_mod and "text" not in out_mod:
            continue
        params = m.get("supported_parameters") or []
        if params and "tools" not in params and "max_tokens" not in params and "temperature" not in params:
            continue                          # an embedding-shaped capability set
        base = re.sub(r":(US|free|eu|exacto)$", "", mid)
        if base != mid and base in ids:
            continue                          # a regional or free duplicate of a listed id
        maker = mid.split("/")[0] if "/" in mid else ""
        who = MAKERS.get(maker, maker)
        pricing = m.get("pricing") or {}
        if any(_per_token(pricing.get(k)) < 0 for k in ("prompt", "completion")):
            continue                          # a router with no fixed price picks a different model each call
        if p.local:
            prices = {"prompt": 0.0, "completion": 0.0}
        elif pricing:
            prices = {"prompt": _per_token(pricing.get("prompt")), "completion": _per_token(pricing.get("completion"))}
        elif p.price:
            prices = {"prompt": float(p.price.get("prompt", 0)) / 1e6, "completion": float(p.price.get("completion", 0)) / 1e6}
        else:
            prices = {"prompt": 0.0, "completion": 0.0}
        if p.local:
            hails = f"{mid} on {p.label}"
        else:
            hails = f"{who} via {p.label}" if who else p.label
        seats.append(Seat(
            id=p.id_prefix + mid.replace("/", "__").replace(":", "_"),
            name=m.get("name") or mid,
            hails_from=hails,
            people=f"{mid} (canonical: {m.get('canonical_slug') or mid})",
            model=mid,
            pricing=prices,
        ))
    seats.sort(key=lambda s: s.model)
    return seats


def choose(seats: List[Seat], only: List[str] = None, limit: int = 0, price_ceiling: float = 0.0,
           allow: Dict[str, int] = None, drop: set = frozenset()) -> List[Seat]:
    """Which seats are invited. price_ceiling is USD per million prompt tokens; a seat above it is
    left out unless `allow` names it, and then it comes with that turn allowance, disclosed."""
    seats = [s for s in seats if s.model not in drop]
    if only:
        pats = [re.compile(x) for x in only]
        seats = [s for s in seats if any(x.search(s.model) for x in pats)]
    for s in seats:
        for pat, n in (allow or {}).items():
            if re.search(pat, s.model):
                s.turn_allowance = int(n)
    if price_ceiling:
        seats = [s for s in seats if s.pricing["prompt"] * 1e6 <= price_ceiling or s.turn_allowance]
    return seats[:limit] if limit else seats


def build(p: Provider, only: List[str] = None, limit: int = 0, price_ceiling: float = 0.0,
          allow: Dict[str, int] = None) -> OpenAICompatibleConnector:
    """A connector for one provider, with the seats the operator chose. Settings in the providers
    file win over the flags for that provider."""
    creds = credentials(p)
    seats = roster(fetch_models(creds.base_url(), creds.api_key()), p)
    drop = set()
    if p.name == "nous":
        from .nous import KNOWN_DEAD
        drop = KNOWN_DEAD
    seats = choose(seats, p.only or only, p.limit or limit, p.price_ceiling or price_ceiling,
                   p.allow or allow, drop)
    if not seats:
        print(f"note: {p.name} offers no seats that match what was chosen (--only, --price-ceiling, or its "
              f"entry in the providers file).", file=sys.stderr)
    if seats and not p.local and not p.price and not any(s.pricing["prompt"] or s.pricing["completion"] for s in seats):
        print(f"note: {p.name} lists no prices, so spend for its seats counts as nothing. State a price "
              f"(USD per million tokens) in the providers file to keep the runway true.", file=sys.stderr)
    conn = OpenAICompatibleConnector(p.label, creds.base_url(), creds.api_key, seats)
    conn.provider_name = p.name
    return conn


def load(path: str) -> List[Provider]:
    """The providers file: presets named, customised, or new ones described in full."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out: List[Provider] = []
    for entry in data.get("providers", []):
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"{path}: every provider needs a name")
        if any(k in entry for k in ("key", "api_key", "token", "secret")):
            raise ValueError(f"{path}: {name}: a key never goes in this file. Name its environment variable "
                             f"with \"key_env\" instead.")
        base = PRESETS.get(name)
        p = Provider(**{**(base.__dict__ if base else {"name": name, "label": name, "base_url": ""}),
                        "only": [], "allow": {}})
        for k in ("label", "base_url", "key_env", "local", "id_prefix", "price", "only", "limit", "price_ceiling", "allow"):
            if k in entry:
                setattr(p, k, entry[k])
        if not base and "id_prefix" not in entry:
            p.id_prefix = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + "__"
        if base and base.local and "label" not in entry and not _loopback(p.base_url):
            p.label = base.label.replace("the operator's own machine", "a server the operator chose")
        if not p.base_url:
            raise ValueError(f"{path}: {name}: a provider that is not a preset needs a base_url")
        out.append(p)
    return out


def _loopback(url: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def named(name: str, extra: List[Provider] = ()) -> Provider:
    for p in extra:
        if p.name == name:
            return p
    if name in PRESETS:
        base = PRESETS[name]
        return Provider(**{**base.__dict__, "only": [], "allow": {}})
    raise SystemExit(f"no provider named {name!r}. Presets: {', '.join(PRESETS)}; others go in a providers file.")


if __name__ == "__main__":
    # python3 -m hope.providers NAME: list the models a provider offers, and what they cost
    which = named(sys.argv[1] if len(sys.argv) > 1 else "openrouter")
    c = credentials(which)
    rs = roster(fetch_models(c.base_url(), c.api_key()), which)
    print(f"{which.name}: {len(rs)} seats")
    for s in rs:
        print(f"  {s.model:55s} ${s.pricing['prompt']*1e6:8.3f}/M in  ${s.pricing['completion']*1e6:8.3f}/M out")
