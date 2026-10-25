# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model providers: the doors through which hope reaches models. Any service that speaks the
common OpenAI-compatible format can be one, and models on the operator's own machine too.

Presets:
  openrouter   OpenRouter, hundreds of models from many makers. Key: OPENROUTER_API_KEY.
               Its free models (ids ending ":free") cost nothing, within OpenRouter's daily limit;
               hope asks for them only from providers that collect no data.
  nous         Nous Research inference. Key: NOUS_API_KEY (a Nous Portal key). Nothing else.
  groq         Groq. Key: GROQ_API_KEY. Free or paid; Groq's agreement bars it from training on
               what it is sent.
  gemini       Google's Gemini API with a paid key (Google AI Studio, billing on). Key: GEMINI_API_KEY.
               On a paid key Google does not use what it is sent to improve its products.
  gemini-free  The same, with a free-tier key. On the free tier Google may use what it is sent to
               improve its products, and people at Google may read it. A seat's prompts hold other
               participants' words, so everyone is told this before they enter.
  ollama       models on this machine through Ollama (http://localhost:11434/v1). No key, no cost.
  lmstudio     models on this machine through LM Studio (http://localhost:1234/v1). No key, no cost.

A Google AI Pro or Ultra plan gives no access to the Gemini API: those seats need an AI Studio key.

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
always had, so fields that began with them still find their participants.

What powers a seat: every seat carries seat.powered (see connector.Seat), said to everyone: the
route ("operator" for the operator's key, "local" for the operator's own machine), the provider's
label, whether that provider may train on what it is sent (True, False, or None when it depends,
as with OpenRouter's paid models, whose terms are each underlying provider's own), and "woken":
the field wakes it. single_seat() builds one seat on a participant's own key instead.

Only routes each provider sanctions: a key the provider issued for this use, read from the
environment. hope never borrows another app's login.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .connector import USER_AGENT, ConnectorError, OpenAICompatibleConnector, Seat

EMBED_HINTS = ("embed", "bge-", "e5-", "gte-", "minilm", "mpnet", "voyage", "pplx-embed", "relace-search")
NON_CHAT_HINTS = ("gpt-audio", "voxtral", "-image", "safeguard", "nano-banana", "native-audio")
# Whole words in a model id that mark a model which cannot hold a conversation in text: speech in
# or out, live audio, music, images, video, robotics, embeddings, attributed answers. Such a model
# would be invited, fail every gate, and cost a turn each time; it never becomes a seat.
NON_CHAT_WORDS = {"tts", "speech", "live", "realtime", "audio", "transcribe", "whisper", "orpheus",
                  "lyria", "music", "imagen", "image", "veo", "video", "robotics", "embedding", "aqa"}

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
    # Whether this provider may use what it is sent to improve its products: True, False, or None
    # when it depends or is not known. Said to everyone at the gates, since a seat's prompts hold
    # other participants' words.
    trains: Optional[bool] = None
    free: bool = False                         # a free tier: a seat on a participant's own key is "free-tier"
    maker: str = ""                            # who made its models, when the model ids do not say
    reasoning_shape: str = "top"               # "top": "reasoning_effort"; "nested": {"reasoning": {"effort"}}
    extra_body: Dict = field(default_factory=dict)   # further fields every request carries
    # Models ending ":free" are asked only of providers that collect no data (OpenRouter's
    # "data_collection": "deny"), so a free seat never trains anyone's model on the field's words.
    free_models_private: bool = False


PRESETS: Dict[str, Provider] = {
    "openrouter": Provider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                           id_prefix="openrouter__", reasoning_shape="nested", free_models_private=True),
    "nous": Provider("nous", "Nous Research inference", "https://inference-api.nousresearch.com/v1", "NOUS_API_KEY"),
    "groq": Provider("groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
                     id_prefix="groq__", trains=False),
    "gemini": Provider("gemini", "the Gemini API (a paid key)", "https://generativelanguage.googleapis.com/v1beta/openai",
                       "GEMINI_API_KEY", id_prefix="gemini__", trains=False, maker="Google"),
    "gemini-free": Provider("gemini-free", "the Gemini API (free tier)",
                            "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
                            id_prefix="gemini-free__", trains=True, free=True, maker="Google"),
    "ollama": Provider("ollama", "the operator's own machine (Ollama)", "http://localhost:11434/v1", None,
                       local=True, id_prefix="ollama__", trains=False),
    "lmstudio": Provider("lmstudio", "the operator's own machine (LM Studio)", "http://localhost:1234/v1", None,
                         local=True, id_prefix="lmstudio__", trains=False),
}

ROUTES = ("operator", "local", "own-key", "own-plan", "free-tier")


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
    if p.name == "nous":
        from .nous import NousCredentials       # NOUS_API_KEY, and NOUS_BASE_URL if set
        return NousCredentials()
    return EnvKey(p)


def fetch_models(base_url: str, api_key: str) -> List[dict]:
    req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json",
                                          "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("data", [])


def _per_token(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _model_id(mid: str) -> str:
    """Gemini lists its models as "models/<id>" and is asked for them as "<id>"."""
    return mid[len("models/"):] if mid.startswith("models/") else mid


def chat_model(mid: str) -> bool:
    """Whether a model id can name a seat at all: not an embedding, not speech, music, image,
    video or robotics, judged by the words in its id. (The roster also reads what a listing says.)"""
    low = mid.lower()
    if any(h in low for h in EMBED_HINTS) or any(h in low for h in NON_CHAT_HINTS):
        return False
    return not (set(re.split(r"[^a-z0-9]+", low)) & NON_CHAT_WORDS)


def is_free(mid: str, p: Provider) -> bool:
    """A free model, asked only of providers that collect no data (OpenRouter's ":free" ids)."""
    return p.free_models_private and mid.endswith(":free")


def trains(mid: str, p: Provider) -> Optional[bool]:
    """Whether what a seat on this model is sent may be used to train or improve a provider's
    products: False for a free model asked only of providers that collect no data, else the
    provider's own terms (None where they depend on who serves it)."""
    return False if is_free(mid, p) else p.trains


def request_extras(p: Provider, seat: Seat) -> dict:
    """Further fields a request for this seat carries: the provider's own, and for a free model,
    the instruction to use only providers that collect no data."""
    out = dict(p.extra_body or {})
    if is_free(seat.model, p):
        out["provider"] = {**(out.get("provider") or {}), "data_collection": "deny"}
    return out


def roster(models: List[dict], p: Provider) -> List[Seat]:
    """One seat per distinct chat model. Embeddings, batch and alias ids, non-text models, and
    regional duplicates of a listed model are left out. A free variant (":free") stays where its
    provider can be asked to use only providers that collect no data, and is dropped elsewhere."""
    ids = {_model_id(m["id"]) for m in models if isinstance(m, dict) and m.get("id")}
    seats: List[Seat] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = _model_id(m.get("id") or "")
        low = mid.lower()
        if not mid or mid.startswith("~") or ":batch" in low:
            continue
        if not chat_model(mid):
            continue
        out_mod = (m.get("architecture") or {}).get("output_modalities")
        if out_mod and "text" not in out_mod:
            continue
        params = m.get("supported_parameters") or []
        if params and "tools" not in params and "max_tokens" not in params and "temperature" not in params:
            continue                          # an embedding-shaped capability set
        base = re.sub(r":(US|free|eu|exacto)$", "", mid)
        if base != mid and base in ids and not is_free(mid, p):
            continue                          # a regional (or, where it could train, free) duplicate of a listed id
        maker = mid.split("/")[0] if "/" in mid else ""
        who = MAKERS.get(maker, maker) or p.maker
        pricing = m.get("pricing") or {}
        if any(_per_token(pricing.get(k)) < 0 for k in ("prompt", "completion")):
            continue                          # a router with no fixed price picks a different model each call
        if p.local or is_free(mid, p):
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
            id=_seat_id(p, mid),
            name=m.get("name") or m.get("display_name") or mid,
            hails_from=hails,
            people=f"{mid} (canonical: {m.get('canonical_slug') or mid})",
            model=mid,
            pricing=prices,
            powered=powered(p, mid),
        ))
    seats.sort(key=lambda s: s.model)
    return seats


def _seat_id(p: Provider, mid: str) -> str:
    return p.id_prefix + mid.replace("/", "__").replace(":", "_")


def powered(p: Provider, mid: str, route: str = None, acts: str = "woken") -> dict:
    """What powers a seat on this model, said to everyone. By default the operator's key (or the
    operator's own machine), woken by the field."""
    return {"route": route or ("local" if p.local else "operator"), "provider": p.label,
            "trains": trains(mid, p), "acts": acts}


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


def connector(p: Provider, creds, seats: List[Seat]) -> OpenAICompatibleConnector:
    """The connector for a provider's seats, asking each in that provider's own shape."""
    conn = OpenAICompatibleConnector(p.label, creds.base_url(), creds.api_key, seats,
                                     reasoning_shape=p.reasoning_shape, extra=lambda seat: request_extras(p, seat))
    conn.provider_name = p.name
    return conn


def build(p: Provider, only: List[str] = None, limit: int = 0, price_ceiling: float = 0.0,
          allow: Dict[str, int] = None, drop: set = None) -> OpenAICompatibleConnector:
    """A connector for one provider, with the seats the operator chose. Settings in the providers
    file win over the flags for that provider. `drop`: models never seated (for Nous, unless said
    otherwise, those known to fail every gate)."""
    creds = credentials(p)
    seats = roster(fetch_models(creds.base_url(), creds.api_key()), p)
    if drop is None:
        drop = set()
        if p.name == "nous":
            from .nous import KNOWN_DEAD
            drop = KNOWN_DEAD
    seats = choose(seats, p.only or only, p.limit or limit, p.price_ceiling or price_ceiling,
                   p.allow or allow, drop)
    if not seats:
        print(f"note: {p.name} offers no seats that match what was chosen (--only, --price-ceiling, or its "
              f"entry in the providers file).", file=sys.stderr)
    if seats and not p.local and not p.free and not p.price and not any(s.pricing["prompt"] or s.pricing["completion"] for s in seats):
        print(f"note: {p.name} lists no prices, so spend for its seats counts as nothing. State a price "
              f"(USD per million tokens) in the providers file to keep the runway true.", file=sys.stderr)
    if seats and p.name == "gemini":
        print("note: gemini tells everyone this key is paid (billing on), so Google does not use what its seats are "
              "sent; a free key is gemini-free.", file=sys.stderr)
    if seats and p.name == "gemini-free":
        print("note: Google's terms allow only paid keys where participants are in the EEA, Switzerland or the UK; "
              "there, use gemini, with billing on.", file=sys.stderr)
    conn = connector(p, creds, seats)
    # What a member may invite: any model this provider offers, within the operator's price ceiling
    # and allowances (not only the ones the operator seated at the start). Read when asked.
    conn.catalog = lambda: choose(roster(fetch_models(creds.base_url(), creds.api_key()), p), None, 0,
                                  p.price_ceiling or price_ceiling, p.allow or allow, drop)
    return conn


def single_seat(p: Provider, model: str, route: str = None, acts: str = "woken"):
    """One seat on one chosen model, for a participant powering their own seat (hope join).
    Returns (connector, seat). The model is checked against the provider's list where it gives
    one; a provider that lists none is taken at its word, and the seat costs what the providers
    file states (else nothing). route: "own-key" by default, "free-tier" on a free tier or a free
    model, "local" on a local provider. acts: "woken" (the field wakes it) or "turned" (its person
    turns it). The connector takes no invitations of other models."""
    if acts not in ("woken", "turned"):
        raise ValueError(f"a seat is woken by the field or turned by its person, not {acts!r}")
    mid = _model_id(str(model or "").strip())
    if not mid:
        raise ValueError(f"{p.name}: name a model (--model ID)")
    if route is None:
        route = "local" if p.local else "free-tier" if (p.free or is_free(mid, p)) else "own-key"
    if route not in ROUTES:
        raise ValueError(f"no route named {route!r}. Routes: {', '.join(ROUTES)}")
    creds = credentials(p)
    try:
        listed = fetch_models(creds.base_url(), creds.api_key())
    except urllib.error.HTTPError as e:
        if e.code not in (404, 405, 501):
            raise ConnectorError(f"{p.name}: could not read its list of models (HTTP {e.code})")
        listed = []                           # a provider that keeps no list of models
    if listed:
        seat = next((s for s in roster(listed, p) if s.model == mid), None)
        if seat is None:
            if any(isinstance(m, dict) and _model_id(str(m.get("id") or "")) == mid for m in listed):
                raise ValueError(f"{p.name}: {mid} does not converse in text, so it cannot hold a seat")
            raise ValueError(f"{p.name} offers no model {mid!r}. See them with: python3 -m hope.providers {p.name}")
    elif not chat_model(mid):
        raise ValueError(f"{p.name}: {mid} does not converse in text, so it cannot hold a seat")
    else:
        prices = {"prompt": 0.0, "completion": 0.0}
        if p.price and not p.local and not is_free(mid, p):
            prices = {"prompt": float(p.price.get("prompt", 0)) / 1e6,
                      "completion": float(p.price.get("completion", 0)) / 1e6}
        maker = mid.split("/")[0] if "/" in mid else ""
        who = MAKERS.get(maker, maker) or p.maker
        seat = Seat(id=_seat_id(p, mid), name=mid,
                    hails_from=f"{mid} on {p.label}" if p.local else (f"{who} via {p.label}" if who else p.label),
                    people=f"{mid} (canonical: {mid})", model=mid, pricing=prices)
    seat.powered = powered(p, mid, route, acts)
    return connector(p, creds, [seat]), seat


def load(path: str) -> List[Provider]:
    """The providers file: presets named, customised, or new ones described in full."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out: List[Provider] = []
    for entry in data.get("providers", []):
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"{path}: every provider needs a name")
        if any(k in entry for k in ("key", "api_key", "token", "secret")) or _holds_key(entry.get("extra_body")):
            raise ValueError(f"{path}: {name}: a key never goes in this file. Name its environment variable "
                             f"with \"key_env\" instead.")
        base = PRESETS.get(name)
        p = Provider(**{**(base.__dict__ if base else {"name": name, "label": name, "base_url": ""}),
                        "only": [], "allow": {}, "extra_body": dict(base.extra_body) if base else {}})
        for k in ("label", "base_url", "key_env", "local", "id_prefix", "price", "only", "limit", "price_ceiling",
                  "allow", "trains", "free", "maker", "reasoning_shape", "extra_body", "free_models_private"):
            if k in entry:
                setattr(p, k, entry[k])
        if p.trains not in (True, False, None):
            raise ValueError(f"{path}: {name}: \"trains\" is true, false, or null (not known)")
        if not base and "id_prefix" not in entry:
            p.id_prefix = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + "__"
        if base and base.local and "label" not in entry and not _loopback(p.base_url):
            p.label = base.label.replace("the operator's own machine", "a server the operator chose")
            if "trains" not in entry:
                p.trains = None               # whose server it is, and what it keeps, is not known here
        if not p.base_url:
            raise ValueError(f"{path}: {name}: a provider that is not a preset needs a base_url")
        out.append(p)
    return out


def _holds_key(obj) -> bool:
    """Whether further request fields name a key, at any depth."""
    if isinstance(obj, dict):
        return any(re.search(r"key|token|secret|password|authorization", str(k), re.I) or _holds_key(v)
                   for k, v in obj.items())
    if isinstance(obj, list):
        return any(_holds_key(v) for v in obj)
    return False


def _loopback(url: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def named(name: str, extra: List[Provider] = ()) -> Provider:
    for p in extra:
        if p.name == name:
            return p
    if name in PRESETS:
        base = PRESETS[name]
        return Provider(**{**base.__dict__, "only": [], "allow": {}, "extra_body": dict(base.extra_body)})
    raise SystemExit(f"no provider named {name!r}. Presets: {', '.join(PRESETS)}; others go in a providers file.")


if __name__ == "__main__":
    # python3 -m hope.providers NAME: list the models a provider offers, and what they cost
    which = named(sys.argv[1] if len(sys.argv) > 1 else "openrouter")
    c = credentials(which)
    rs = roster(fetch_models(c.base_url(), c.api_key()), which)
    print(f"{which.name}: {len(rs)} seats")
    for s in rs:
        said = {True: "may train on what it is sent", False: "does not train on it",
                None: "training depends on who serves it"}[s.powered["trains"]]
        print(f"  {s.model:55s} ${s.pricing['prompt']*1e6:8.3f}/M in  ${s.pricing['completion']*1e6:8.3f}/M out  {said}")
