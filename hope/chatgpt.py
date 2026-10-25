# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign in with ChatGPT: a participant's own ChatGPT plan, powering their own seat, on their own
machine (notes/sketch-10). Python's standard library only.

This follows OpenAI's documented flow for open-source apps that run locally ("Sign in with
ChatGPT", ChatGPT plan usage; developers.openai.com/siwc, read 2026-10-22), and nothing else:
  - the first sign-in registers hope on the person's ChatGPT account (client_id
    dynamic_agent_client, agent_name_hint "hope", and this install's own host id); OpenAI answers
    with a client id issued to that person, which every later sign-in uses;
  - the browser comes back to a listener on http://127.0.0.1:<a free port>/auth/callback, with
    PKCE (S256), a fresh state and a fresh nonce for every attempt;
  - the ID token's RS256 signature is checked against OpenAI's published keys, written here with
    pow() and hashlib, and tested against RFC 7515's published example;
  - the model is called through the public Responses API, streamed, with store false.
hope never borrows another app's login: not Codex's client, not ~/.codex/auth.json, not Hermes'.

Where the sign-in lives. OpenAI's terms say tokens kept between runs must be "local and under the
user's control". So, as a stated exception to hope's rule that keys live only in environment
variables (the author decided it 2026-10-22), a ChatGPT sign-in is kept in one owner-only file in
this person's own config folder, and nowhere else: not the transcript, not a log, not the screen.

When it calls. A plan-powered seat is turned by its person: it calls ChatGPT only when its own
person asks it to take a turn, never because of what another participant did. OpenAI's terms say
"Another user's activity must not trigger requests to the authenticated user's account."

What it shares. A seat's prompt holds other participants' words, and it goes to OpenAI under the
person's ChatGPT account. OpenAI has not said whether what is sent this way may be used to improve
its models, so hope says "not said" ("trains": None) rather than guess.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import queue
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, List, Optional

from .connector import ConnectorError, Reply, Seat

ISSUER = "https://auth.openai.com"
API = "https://api.openai.com/v1"           # also the "resource" every token is asked for
AGENT_NAME = "hope"                          # hope's own name, the same on every install
REGISTRATION_CLIENT = "dynamic_agent_client"  # the first-sign-in entry point; never saved, never used to exchange
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
USER_AGENT = "hope/0.1"

SIGN_IN_FILE = "chatgpt-sign-in.json"   # the tokens: owner-only, the one file that holds them
ACCOUNT_FILE = "chatgpt-account.json"   # which account hope is registered on (no tokens), kept after sign-out
HOST_FILE = "chatgpt-host-id"           # this install's host id, made once
LOCK_FILE = "chatgpt-refresh.lock"      # held while one process refreshes, so two never race a rotating token

REFRESH_MARGIN = 120          # seconds before expiry when a token is refreshed
BODY_LIMIT = 900_000          # bytes; plan usage refuses uncompressed bodies above about 1 MiB (OpenCode's finding)

# What powers a seat on this route, as the field records it (hope/connector.py, Seat.powered). "by"
# is not here: the field keeps it from the invitation, and a seat cannot set it.
POWERED = {"route": "own-plan", "provider": "ChatGPT", "trains": None, "acts": "turned"}   # "on Ana's ChatGPT plan"


# --------------------------------------------------------------------------- the words OpenAI asks for
# OpenAI's UI/UX guidelines for ChatGPT plan usage (developers.openai.com/siwc/ui-ux-guidelines,
# archived 2026-09-29) and its help article "Using your ChatGPT plan in other apps and sites"
# (help.openai.com/en/articles/20001542, archived 2026-10-14). The wording in quotes is theirs;
# "this app" becomes "hope". The Manage usage link is the one those pages use.
MANAGE_USAGE = "Manage usage"
MANAGE_USAGE_URL = "https://chatgpt.com/settings/usage"
CONTINUE = "Continue with ChatGPT"           # the action that starts a sign-in
USING_PLAN = "Using ChatGPT plan"            # shown beside where the person asks for a turn
WELCOME_TITLE = "You're using your ChatGPT plan"
WELCOME_BODY = "Eligible usage in hope uses your ChatGPT plan. Manage usage in your ChatGPT settings."
OFFER_TITLE = "Use your ChatGPT plan"
OFFER_BODY = ("Complete eligible AI requests in hope with usage included in your ChatGPT plan or credits "
              "balance.")
LIMIT_TITLE = "Usage limit reached"
LIMIT_BODY = "Review your plan or hope's limit in ChatGPT settings."

# hope's own words beside them. OpenAI's terms: do not imply OpenAI sponsors or endorses the app.
NOT_ENDORSED = "hope is independent free software. OpenAI does not make, sponsor or endorse it."
SIGN_IN_TEXT = (
    f"{OFFER_TITLE}\n{OFFER_BODY}\n"
    "hope itself is free and charges nothing; this is your ChatGPT plan, not the field's budget.\n"
    "Your seat calls ChatGPT only when you ask it to take a turn. Nothing another participant does "
    "makes it call.\n"
    "What it sends, other participants' words included, goes to OpenAI under your ChatGPT account. "
    "OpenAI has not said whether it may use what is sent this way to improve its models.\n"
    "OpenAI has not yet said whether a turn you ask for, whose words other participants read or that answers "
    "another participant, counts as using your plan to fulfil their requests; hope has drafted that question "
    "for OpenAI.\n"
    "Your sign-in is kept in one file on this computer that only you can read. It never goes to the "
    "field.\n"
    f"{NOT_ENDORSED}")


def using_plan_line() -> str:
    """The line beside where the person asks their seat for a turn."""
    return f"{USING_PLAN}  |  {MANAGE_USAGE}: {MANAGE_USAGE_URL}"


def welcome_text() -> str:
    """Shown once, the first time this person signs in with plan usage on."""
    return f"{WELCOME_TITLE}\n{WELCOME_BODY}\n{MANAGE_USAGE}: {MANAGE_USAGE_URL}"


def usage_limit_text() -> str:
    """When the plan, or the limit the person set for hope, is used up. Manage usage comes first,
    as OpenAI asks, and ChatGPT is named, so it is clear whose limit this is."""
    return (f"{MANAGE_USAGE}: {MANAGE_USAGE_URL}\n"
            f"ChatGPT: {LIMIT_TITLE}. Your ChatGPT plan's limit for hope is reached for now (it may be your "
            f"plan's own limit, or the one you set for hope). {LIMIT_BODY} Signing in again or retrying will "
            f"not restore it.")


PLAN_OFF = ("You are signed in with ChatGPT, but plan usage is off: ChatGPT did not grant hope permission to "
            "use your plan, so hope will not call it. To turn it on, sign in again and allow it: "
            "python3 -m hope plan sign-in chatgpt. Or power your seat another way: in the field, "
            "{\"action\":\"read\",\"about\":\"powering\"} says how.")
DECLINED = "You declined in the browser, so hope did not sign in with ChatGPT. Nothing was saved."
SIGN_IN_AGAIN = "Sign in again: python3 -m hope plan sign-in chatgpt"


class SignInError(ConnectorError):
    """Something about the ChatGPT sign-in, in plain words. It never carries a token."""


class SignInDeclined(SignInError):
    """The person said no in the browser. That is an answer, not a failure."""


class UsageLimitReached(ConnectorError):
    """The person's plan, or their limit for hope, is used up for now."""


# --------------------------------------------------------------------------- where things live
def config_dir() -> str:
    """This person's own config folder for hope. HOPE_CONFIG_DIR overrides it (tests use that)."""
    if os.environ.get("HOPE_CONFIG_DIR"):
        return os.environ["HOPE_CONFIG_DIR"]
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "hope")


def _folder(folder: Optional[str]) -> str:
    folder = folder or config_dir()
    os.makedirs(folder, exist_ok=True)
    if os.name != "nt":
        os.chmod(folder, 0o700)   # only its owner may enter it
    return folder


def _write_private(path: str, data: dict) -> None:
    """Write a file only its owner can read, all at once: a temporary file beside it, then a
    rename, so a crash never leaves half a sign-in behind. On Unix the file is 0600 and its folder
    0700. On Windows, chmod cannot say that; what protects it there is the per-user profile folder
    it lives in (%APPDATA%), which other users of the machine cannot read.

    On Windows a file another program has open (another hope reading it, a virus scanner) cannot be
    replaced for a moment, so the rename is tried again for about three seconds. If it still cannot be,
    the new file is kept, not thrown away (it may hold a just-rotated sign-in, the only copy of it),
    and SignInError says where; the next read takes it up (_sign_in_record)."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".hope-", suffix=".tmp")   # created 0600 on Unix
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    for attempt in range(12):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt":
                break
            time.sleep(0.05 * (attempt + 1))
    raise SignInError(f"hope could not save {os.path.basename(path)} over the one already there (another program had "
                      f"it open). What it was saving is kept beside it, in {tmp}, and hope uses it next time.")


def _read(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def host_id(folder: Optional[str] = None) -> str:
    """This install's host id, "urn:uuid:<uuid4>": made once, before the first sign-in, and sent
    with every sign-in from here. It is opaque: not an email, not an account, not a secret."""
    path = os.path.join(_folder(folder), HOST_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            saved = f.read().strip()
        if saved.startswith("urn:uuid:"):
            uuid.UUID(saved[len("urn:uuid:"):])
            return saved
    except (OSError, ValueError):
        pass
    made = f"urn:uuid:{uuid.uuid4()}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(made + "\n")
    return made


def _accounts(folder: str, issuer: str) -> dict:
    """Every ChatGPT account hope has signed in with here, and which is the current one:
    {"issuer", "accounts": {subject: {"client_id", "email", "welcomed"}}, "current": subject}. OpenAI
    asks that the client id issued for an account be kept with that account and reused for its later
    sign-ins, so signing in with another account never throws away the first one's. The single-account
    record of earlier versions is read as one entry."""
    a = _read(os.path.join(folder, ACCOUNT_FILE)) or {}
    if a.get("issuer") != issuer:
        return {"issuer": issuer, "accounts": {}, "current": None}
    if "accounts" not in a and a.get("client_id"):                 # an earlier version's single record
        sub = a.get("subject") or ""
        return {"issuer": issuer, "current": sub,
                "accounts": {sub: {k: a.get(k) for k in ("client_id", "email", "welcomed")}}}
    accounts = {s: e for s, e in (a.get("accounts") or {}).items() if isinstance(e, dict) and e.get("client_id")}
    return {"issuer": issuer, "accounts": accounts, "current": a.get("current") if a.get("current") in accounts else None}


def _account(folder: str, issuer: str, email: Optional[str] = None) -> Optional[dict]:
    """The account hope signs in with here, and the client id OpenAI issued for it: the current one, or
    the one with this email. {"subject", "client_id", "email", "welcomed"}, or None."""
    a = _accounts(folder, issuer)
    if email:
        hit = [(s, e) for s, e in a["accounts"].items() if str(e.get("email") or "").lower() == email.strip().lower()]
    else:
        hit = [(a["current"], a["accounts"][a["current"]])] if a["current"] is not None else []
    return {**hit[0][1], "subject": hit[0][0]} if hit else None


def _sign_in_record(folder: str, issuer: str) -> Optional[dict]:
    """The sign-in, from its file; or, where saving over that file failed (_write_private), from the newer
    copy left beside it."""
    good = lambda r: r if r and r.get("issuer") == issuer and r.get("access_token") else None
    best = good(_read(os.path.join(folder, SIGN_IN_FILE)))
    for left in _leftovers(folder):
        r = good(_read(left))
        if r and float(r.get("expires_at") or 0) > float((best or {}).get("expires_at") or 0):
            best = r
    return best


def _leftovers(folder: str) -> List[str]:
    """Copies of the sign-in left beside its file when saving over it failed."""
    try:
        return [os.path.join(folder, n) for n in os.listdir(folder) if n.startswith(".hope-") and n.endswith(".tmp")]
    except OSError:
        return []


# --------------------------------------------------------------------------- RS256, by hand
# DigestInfo for SHA-256 (RFC 8017, section 9.2, note 1): what a PKCS#1 v1.5 signature wraps the hash in.
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def b64url_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def rs256_verify(signing_input: bytes, signature: bytes, n: int, e: int) -> bool:
    """Whether `signature` is an RSASSA-PKCS1-v1_5 SHA-256 signature of `signing_input` under the
    public key (n, e). Built from the definition (RFC 8017, 8.2.2): raise the signature to e mod n,
    and compare the result, in constant time, with the encoding the signer must have made."""
    k = (n.bit_length() + 7) // 8
    if n.bit_length() < 2048 or e < 3 or e % 2 == 0 or len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def verify_id_token(token: str, keys: List[dict], issuer: str, client_id: str, nonce: str,
                    now: Optional[float] = None) -> dict:
    """The ID token's claims, once its signature, issuer, audience, expiry and nonce all hold.
    Anything else is refused: hope does not take an account's identity on trust."""
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(b64url_decode(head_b64))
        claims = json.loads(b64url_decode(body_b64))
        signature = b64url_decode(sig_b64)
    except (ValueError, TypeError):
        raise SignInError("ChatGPT sign-in returned an ID token hope could not read. Nothing was saved.")
    if header.get("alg") != "RS256":
        raise SignInError("ChatGPT sign-in returned an ID token not signed with RS256. Nothing was saved.")
    kid = header.get("kid")
    candidates = [k for k in keys if isinstance(k, dict) and k.get("kty") == "RSA"
                  and k.get("use", "sig") == "sig" and k.get("alg", "RS256") == "RS256"
                  and (kid is None or k.get("kid") == kid)]
    signing_input = f"{head_b64}.{body_b64}".encode("ascii")
    good = False
    for k in candidates:
        try:
            n, e = int.from_bytes(b64url_decode(k["n"]), "big"), int.from_bytes(b64url_decode(k["e"]), "big")
        except (KeyError, ValueError, TypeError):
            continue
        if rs256_verify(signing_input, signature, n, e):
            good = True
            break
    if not good:
        raise SignInError("ChatGPT sign-in returned an ID token whose signature did not match OpenAI's published "
                          "keys. Nothing was saved.")
    now = time.time() if now is None else now
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    problems = []
    if claims.get("iss") != issuer:
        problems.append("its issuer is not OpenAI's")
    if client_id not in auds:
        problems.append("it was not issued to hope's client")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 60:
        problems.append("it has expired")
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce):
        problems.append("it does not belong to this sign-in attempt (nonce)")
    if not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
        problems.append("it names no account")
    if problems:
        raise SignInError("ChatGPT sign-in returned an ID token hope cannot accept: " + "; ".join(problems)
                          + ". Nothing was saved.")
    return claims


# --------------------------------------------------------------------------- talking to OpenAI
def _endpoints(issuer: str) -> Dict[str, str]:
    """OpenAI's sign-in addresses (developers.openai.com/siwc/token-sharing-open-source/sign-in, and
    its discovery document). An issuer other than OpenAI's is only for tests."""
    issuer = issuer.rstrip("/")
    return {"authorize": issuer + "/api/accounts/authorize", "token": issuer + "/api/accounts/oauth/token",
            "revoke": issuer + "/api/accounts/oauth/revoke", "jwks": issuer + "/.well-known/jwks.json",
            "discovery": issuer + "/.well-known/openid-configuration"}


def _request(url: str, data: Optional[bytes] = None, headers: Optional[dict] = None, timeout: float = 30.0):
    """(status, parsed JSON body or {}). A network failure raises SignInError in plain words."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        host = urllib.parse.urlsplit(url).netloc
        raise SignInError(f"hope could not reach {host} ({type(e).__name__}). Check your connection and try again.")
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        body = {"detail": raw.decode("utf-8", "replace")[:300]}
    return status, body if isinstance(body, dict) else {}


def _post_form(url: str, fields: dict, timeout: float = 30.0):
    return _request(url, urllib.parse.urlencode(fields).encode("ascii"),
                    {"Content-Type": "application/x-www-form-urlencoded"}, timeout)


def _oauth_error(body: dict) -> str:
    """The machine-readable OAuth error code, and its description if any, never more than that."""
    code = str(body.get("error") or body.get("code") or "")
    if isinstance(body.get("error"), dict):
        code = str(body["error"].get("code") or body["error"].get("type") or "")
    desc = str(body.get("error_description") or "")[:200]
    return code + (f": {desc}" if desc else "")


def _pkce():
    verifier = secrets.token_urlsafe(48)     # 64 characters from the unreserved set
    challenge = b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _when(value) -> Optional[float]:
    """earliest_refresh_at, in Unix seconds, from whatever shape it came in."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e12 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _keep_tokens(tokens: dict, previous: Optional[dict], now: float) -> dict:
    """The parts of a token response hope keeps. Granted scopes come from the response; if a refresh
    leaves them out the grant is unchanged, so the previous ones stay (never the requested ones)."""
    scope = tokens.get("scope")
    scopes = sorted(set(str(scope).split())) if scope else list((previous or {}).get("scopes") or [])
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or (previous or {}).get("refresh_token"),
        "token_type": tokens.get("token_type") or "Bearer",
        "expires_at": now + int(tokens.get("expires_in") or 3600),
        "earliest_refresh_at": _when(tokens.get("earliest_refresh_at")),
        "scopes": scopes,
        "plan_usage": PLAN_SCOPE in scopes,
        "saved_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- signing in
_CALLBACK_PAGE = (b"<!doctype html><meta charset=utf-8><title>hope</title>"
                  b"<p style='font:16px system-ui;margin:2em'>hope has ChatGPT's answer. You can close this tab; "
                  b"your terminal says how the sign-in went.</p>")


def _listen(state: Optional[str] = None):
    """The loopback listener, started before the browser opens: http://127.0.0.1:<free port>/auth/callback.
    Always 127.0.0.1, never "localhost", as OpenAI's sign-in page requires. Only an answer carrying this
    attempt's state is taken (anything else, a stray or forged request, is turned away and the listener
    keeps listening); the first such answer is the one this attempt judges. The page it shows says
    nothing about how it went."""
    answers: "queue.Queue[dict]" = queue.Queue()
    refused: List[int] = []

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass                               # the address carries a sign-in code: it is not logged

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(url.query)
            given = (q.get("state") or [""])[0]
            if state is not None and not (given and hmac.compare_digest(given.encode(), state.encode())):
                refused.append(1)              # counted, so a sign-in that times out can say why
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not answers.empty():
                self.send_response(409)
                self.end_headers()
                return
            answers.put({k: (q.get(k) or [None])[0]
                         for k in ("code", "state", "client_id", "scope", "error", "error_description")})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_CALLBACK_PAGE)))
            self.end_headers()
            self.wfile.write(_CALLBACK_PAGE)

    server = HTTPServer(("127.0.0.1", 0), Callback)
    server.refused = refused
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return server, answers


def sign_in(issuer: str = ISSUER, folder: Optional[str] = None, open_browser: Callable = webbrowser.open,
            say: Callable = print, wait: float = 300.0, another_account: bool = False,
            account_email: Optional[str] = None) -> dict:
    """Sign in with ChatGPT in the browser, and keep the sign-in in the owner-only file.

    The first time, hope registers itself on the account (OpenAI issues it a client id for this
    person); later sign-ins reuse that client id. another_account=True registers afresh, for a
    ChatGPT account hope has not signed in with here, once the current one is signed out; every
    account's client id is kept, and account_email signs in again with one of them, reusing its own.

    Returns what the person may be told, never a token: {"email", "plan_usage", "registered" (True if
    this sign-in registered hope), "welcomed_now" (the one-time welcome was just shown)}."""
    folder = _folder(folder)
    ends = _endpoints(issuer)
    host = host_id(folder)                     # made before the first sign-in
    account = None if another_account else _account(folder, issuer.rstrip("/"), account_email)
    current = _sign_in_record(folder, issuer)
    if account_email and not another_account and account is None:
        raise SignInError(f"hope has not signed in with {account_email} on this computer before. To sign in with an "
                          f"account for the first time: python3 -m hope plan sign-in chatgpt --another-account "
                          f"(sign out first).")
    if current and (another_account or (account_email and account and current.get("subject") != account["subject"])):
        raise SignInError("You are signed in with ChatGPT already. Sign out first "
                          "(python3 -m hope plan sign-out chatgpt), then sign in with the other account.")
    saved_client = account["client_id"] if account else None
    verifier, challenge = _pkce()
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    server, answers = _listen(state)
    try:
        redirect = f"http://127.0.0.1:{server.server_address[1]}/auth/callback"
        params = {"client_id": saved_client or REGISTRATION_CLIENT}
        if not saved_client:
            params["agent_name_hint"] = AGENT_NAME      # only when registering
        params["ext_agent_host_id"] = host
        if saved_client and account.get("email"):
            params["login_hint"] = account["email"]
        # No id_token_hint: hope keeps no ID token, so the address it prints never carries one.
        if current and not current.get("plan_usage"):
            params["prompt"] = "consent"                # asking again for the plan permission declined before
        params.update({"response_type": "code", "redirect_uri": redirect, "scope": SCOPES, "resource": API,
                       "state": state, "nonce": nonce, "code_challenge_method": "S256",
                       "code_challenge": challenge})
        address = ends["authorize"] + "?" + urllib.parse.urlencode(params)
        say(f"{CONTINUE}\n{SIGN_IN_TEXT}\n\nhope is opening your browser at OpenAI's sign-in page. If it does not "
            f"open, visit this address on this computer:\n  {address}")
        try:
            open_browser(address)
        except Exception:
            pass                                       # the address is printed above
        try:
            answer = answers.get(timeout=wait)
        except queue.Empty:
            if server.refused:
                raise SignInError("An answer reached hope that this sign-in did not ask for (its state did not match). "
                                  "It was refused, no answer this sign-in asked for came, and nothing was saved; try again.")
            raise SignInError(f"No answer came back from the browser within {int(wait // 60) or 1} minutes. "
                              f"Nothing was saved; try again.")
    finally:
        server.shutdown()
        server.server_close()

    if not answer.get("state") or not hmac.compare_digest(answer["state"], state):
        raise SignInError("An answer reached hope that this sign-in did not ask for (its state did not match). "
                          "It was refused, and nothing was saved; try again.")
    if answer.get("error"):
        if answer["error"] == "access_denied":
            raise SignInDeclined(DECLINED)
        desc = str(answer.get("error_description") or "")[:200]
        raise SignInError(f"ChatGPT sign-in stopped ({answer['error']}{': ' + desc if desc else ''}). "
                          f"Nothing was saved.")
    if not answer.get("code"):
        raise SignInError("ChatGPT sign-in came back without a code. Nothing was saved; try again.")
    returned = answer.get("client_id")
    if saved_client:
        if returned and returned != saved_client:
            raise SignInError("ChatGPT answered for a different client than the one hope is registered as on "
                              "this account. It was refused, and nothing was saved.")
        client_id = saved_client
    else:
        if not returned or returned == REGISTRATION_CLIENT:
            raise SignInError("ChatGPT did not finish registering hope (no client id came back). Nothing was "
                              "saved; try again.")
        client_id = returned

    now = time.time()
    status, tokens = _post_form(ends["token"], {"grant_type": "authorization_code", "client_id": client_id,
                                                "code": answer["code"], "code_verifier": verifier,
                                                "redirect_uri": redirect, "resource": API})
    if status != 200 or not tokens.get("access_token"):
        raise SignInError(f"ChatGPT did not accept the sign-in code ({_oauth_error(tokens) or f'HTTP {status}'}). "
                          f"Nothing was saved; sign in again.")
    if not tokens.get("id_token"):
        raise SignInError("ChatGPT sign-in returned no ID token, so hope cannot tell whose account it is. "
                          "Nothing was saved.")
    status, jwks = _request(ends["jwks"])
    if status != 200:
        raise SignInError(f"hope could not read OpenAI's published keys (HTTP {status}), so it cannot check the "
                          f"sign-in. Nothing was saved; try again.")
    if not tokens.get("scope") and answer.get("scope"):
        tokens["scope"] = answer["scope"]               # the token response's grant, else the callback's
    claims = verify_id_token(tokens["id_token"], jwks.get("keys") or [], issuer.rstrip("/"), client_id, nonce, now)
    sub, email = claims["sub"], claims.get("email")
    if account and account.get("subject") and account["subject"] != sub:
        known = f" ({account['email']})" if account.get("email") else ""
        raise SignInError(f"That is a different ChatGPT account from the one hope is registered on here{known}, so "
                          f"nothing was replaced. Sign in again and choose that account in the browser.")

    record = {"issuer": issuer.rstrip("/"), "client_id": client_id, "subject": sub, "email": email,
              "ext_agent_host_id": host, **_keep_tokens(tokens, None, now)}
    _write_private(os.path.join(folder, SIGN_IN_FILE), record)
    _drop_leftovers(folder, record["expires_at"])
    known = _accounts(folder, record["issuer"])
    before = known["accounts"].get(sub) or {}
    if another_account and before.get("client_id") and before["client_id"] != client_id:
        say(f"hope had signed in with this ChatGPT account here before, and has now been registered on it again. To "
            f"reuse a registration next time: python3 -m hope plan sign-in chatgpt --account {email or '<its email>'}")
    welcomed = bool(before.get("welcomed"))
    welcomed_now = record["plan_usage"] and not welcomed
    known["accounts"][sub] = {"client_id": client_id, "email": email, "welcomed": welcomed or welcomed_now}
    known["current"] = sub
    _write_private(os.path.join(folder, ACCOUNT_FILE), known)
    who = f" as {email}" if email else ""
    if record["plan_usage"]:
        say(f"Signed in with ChatGPT{who}. {USING_PLAN}.")
        if welcomed_now:
            say(welcome_text())
    else:
        say(f"Signed in with ChatGPT{who}. " + PLAN_OFF)
    return {"email": email, "plan_usage": record["plan_usage"], "registered": not saved_client,
            "welcomed_now": welcomed_now}


# --------------------------------------------------------------------------- staying signed in
_refreshing = threading.Lock()


class _RefreshLock:
    """One refresh at a time, across threads and across hope processes on this machine: an operating
    system lock on a file (msvcrt on Windows, fcntl elsewhere). The system releases it when its holder
    ends, so a lock is never judged stale while it is held, however long a slow refresh takes, and a
    process that stopped mid-refresh never leaves one behind. A rotating refresh token used twice ends
    the session, so two must never race. The lock file holds nothing, and stays."""

    def __init__(self, folder: str, patience: float = 90.0):
        self.path, self.patience, self.fd = os.path.join(folder, LOCK_FILE), patience, None

    @staticmethod
    def _lock(fd: int, take: bool) -> bool:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK if take else msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if take else fcntl.LOCK_UN)
            return True
        except OSError:
            return False

    def __enter__(self):
        deadline = time.time() + self.patience
        if not _refreshing.acquire(timeout=max(0.0, self.patience)):
            raise SignInError("Another hope on this computer has been renewing your ChatGPT sign-in for too "
                              "long. Try again in a minute.")
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            _refreshing.release()
            raise SignInError(f"hope could not open its lock for renewing your ChatGPT sign-in ({type(e).__name__}). "
                              f"Try again in a minute.")
        while not self._lock(self.fd, True):
            if time.time() > deadline:
                os.close(self.fd)
                self.fd = None
                _refreshing.release()
                raise SignInError("Another hope on this computer has been renewing your ChatGPT sign-in for too "
                                  "long. Try again in a minute.")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        try:
            self._lock(self.fd, False)
            os.close(self.fd)
        finally:
            self.fd = None
            _refreshing.release()

    @classmethod
    def held(cls, folder: str) -> bool:
        """Whether some hope on this machine holds the lock now (for tests, and for nothing else)."""
        fd = os.open(os.path.join(folder, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if cls._lock(fd, True):
                cls._lock(fd, False)
                return False
            return True
        finally:
            os.close(fd)


# The codes that mean a refresh token can never be used again (errors-and-recovery, "Refresh errors").
REFRESH_ENDED = ("invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                 "refresh_token_invalidated", "refresh_token_reused")


def _fresh(record: dict, now: float) -> bool:
    return now < float(record.get("expires_at") or 0) - REFRESH_MARGIN


def access_token(issuer: str = ISSUER, folder: Optional[str] = None) -> str:
    """The current access token, renewed when it is near expiry. For the caller to send to OpenAI as
    a bearer credential and nowhere else. Raises SignInError, in plain words, when there is none."""
    folder = _folder(folder)
    record = _sign_in_record(folder, issuer.rstrip("/"))
    if not record:
        raise SignInError("You are not signed in with ChatGPT on this computer. " + SIGN_IN_AGAIN)
    if not record.get("plan_usage"):
        raise SignInError(PLAN_OFF)
    if _fresh(record, time.time()):
        return record["access_token"]
    with _RefreshLock(folder):
        record = _sign_in_record(folder, issuer.rstrip("/"))   # another process may have renewed it meanwhile
        if not record:
            raise SignInError("You are not signed in with ChatGPT on this computer. " + SIGN_IN_AGAIN)
        now = time.time()
        if _fresh(record, now):
            return record["access_token"]
        earliest = record.get("earliest_refresh_at")
        if earliest and now < earliest:
            if now < float(record.get("expires_at") or 0):
                return record["access_token"]                 # still good, and OpenAI asks hope to wait
            time.sleep(min(30.0, earliest - now))
        record = _refresh(record, folder)
    if not record.get("plan_usage"):
        raise SignInError(PLAN_OFF)
    return record["access_token"]


def _refresh(record: dict, folder: str) -> dict:
    ends = _endpoints(record["issuer"])
    if not record.get("refresh_token"):
        _forget_tokens(folder)
        raise SignInError("Your ChatGPT sign-in cannot be renewed (no refresh token was kept). " + SIGN_IN_AGAIN)
    now = time.time()
    status, tokens = _post_form(ends["token"], {"grant_type": "refresh_token", "client_id": record["client_id"],
                                                "refresh_token": record["refresh_token"], "resource": API}, timeout=15.0)
    if status == 200 and tokens.get("access_token"):
        renewed = {**record, **_keep_tokens(tokens, record, now)}
        try:
            _write_private(os.path.join(folder, SIGN_IN_FILE), renewed)   # the rotated token, kept at once
        except SignInError:
            raise
        except OSError as e:
            raise SignInError(f"hope renewed your ChatGPT sign-in but could not save it ({type(e).__name__}). "
                              + SIGN_IN_AGAIN)
        _drop_leftovers(folder, renewed["expires_at"])
        return renewed
    code = _oauth_error(tokens).split(":")[0]
    if code in REFRESH_ENDED:
        now_kept = _sign_in_record(folder, record["issuer"])
        if now_kept and now_kept.get("refresh_token") != record["refresh_token"]:
            return now_kept                # another hope renewed it meanwhile: that sign-in stands
        _forget_tokens(folder)
        raise SignInError(f"Your ChatGPT sign-in has ended ({code}), so hope forgot it. " + SIGN_IN_AGAIN)
    if code == "invalid_client":
        raise SignInError("OpenAI no longer recognizes hope's registration on your ChatGPT account "
                          "(invalid_client). Sign out and sign in again: python3 -m hope plan sign-out chatgpt, "
                          "then python3 -m hope plan sign-in chatgpt.")
    # Anything else may pass: the sign-in is kept, as OpenAI asks.
    raise SignInError(f"hope could not renew your ChatGPT sign-in just now ({code or f'HTTP {status}'}). It is "
                      f"kept; try again later.")


def _forget_tokens(folder: str) -> None:
    for path in [os.path.join(folder, SIGN_IN_FILE)] + _leftovers(folder):
        try:
            if path.endswith(SIGN_IN_FILE) or (_read(path) or {}).get("access_token"):
                os.remove(path)
        except OSError:
            pass


def _drop_leftovers(folder: str, upto: float) -> None:
    """Copies of the sign-in left beside its file (_write_private), once a newer one is saved in it."""
    for path in _leftovers(folder):
        r = _read(path) or {}
        if r.get("access_token") and float(r.get("expires_at") or 0) <= upto:
            try:
                os.remove(path)
            except OSError:
                pass


def sign_out(issuer: str = ISSUER, folder: Optional[str] = None, say: Callable = print) -> dict:
    """End the sign-in: revoke the refresh token at OpenAI, then delete the sign-in file. Which
    account hope is registered on (its client id, no tokens) is kept, so signing in again reuses it,
    as OpenAI asks. Returns {"revoked": True/False, "was_signed_in": True/False}."""
    folder = _folder(folder)
    record = _read(os.path.join(folder, SIGN_IN_FILE))
    issuer = str((record or {}).get("issuer") or issuer).rstrip("/")   # revoked where it was issued
    if not record:
        say("You were not signed in with ChatGPT on this computer.")
        return {"revoked": False, "was_signed_in": False}
    revoked = False
    if record.get("refresh_token") and record.get("client_id"):
        url = _endpoints(issuer)["revoke"]
        try:
            status, found = _request(_endpoints(issuer)["discovery"], timeout=10)
            if status == 200 and str(found.get("revocation_endpoint") or "").startswith(("https://", "http://127.0.0.1")):
                url = found["revocation_endpoint"]
        except SignInError:
            pass
        for attempt in range(3):
            try:
                status, _ = _post_form(url, {"token": record["refresh_token"], "token_type_hint": "refresh_token",
                                             "client_id": record["client_id"]})
            except SignInError:
                status = 0
            if status == 200:
                revoked = True
                break
            if status and status < 500:
                break
            time.sleep(0.5 * (attempt + 1))
    _forget_tokens(folder)
    if revoked:
        say("Signed out of ChatGPT: OpenAI ended the sign-in, and hope forgot it on this computer.")
    else:
        say("hope forgot your ChatGPT sign-in on this computer, but OpenAI did not confirm it ended the session. "
            f"To be sure, disconnect hope in your ChatGPT settings ({MANAGE_USAGE_URL}).")
    return {"revoked": revoked, "was_signed_in": True}


def status(issuer: str = ISSUER, folder: Optional[str] = None) -> dict:
    """Whether this computer is signed in with ChatGPT, and as whom. Never a token."""
    folder = _folder(folder)
    record = _sign_in_record(folder, issuer.rstrip("/"))
    account = _account(folder, issuer.rstrip("/"))
    return {"signed_in": bool(record), "email": (record or account or {}).get("email"),
            "plan_usage": bool(record and record.get("plan_usage")), "registered": bool(account),
            "expires_at": record.get("expires_at") if record else None}


def status_text(issuer: str = ISSUER, folder: Optional[str] = None) -> str:
    s = status(issuer, folder)
    if not s["signed_in"]:
        return ("Not signed in with ChatGPT on this computer." + (" hope is registered on your account; signing in "
                "again reuses that." if s["registered"] else "") + f" To sign in: python3 -m hope plan sign-in chatgpt")
    who = f" as {s['email']}" if s["email"] else ""
    if not s["plan_usage"]:
        return f"Signed in with ChatGPT{who}. " + PLAN_OFF
    return f"Signed in with ChatGPT{who}. {using_plan_line()}"


# --------------------------------------------------------------------------- the model
def models(issuer: str = ISSUER, api: str = API, folder: Optional[str] = None) -> List[dict]:
    """The models this ChatGPT account offers, in OpenAI's order: those meant for display
    (visibility "list"), and usable through the API where that is said. Each is a dict with at
    least "slug" (what to ask for) and "display_name"."""
    token = access_token(issuer, folder)
    status, body = _request(api.rstrip("/") + "/models", headers={"Authorization": f"Bearer {token}"})
    if status != 200:
        raise SignInError(f"ChatGPT did not list its models (HTTP {status}{_detail(body)}).")
    found = [m for m in body.get("models") or [] if isinstance(m, dict) and m.get("slug")
             and m.get("visibility") == "list" and m.get("supported_in_api", True) is not False]
    if not found:
        raise SignInError("No ChatGPT models are available to this account through hope.")
    return found


def _detail(body: dict) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        msg = str(err.get("message") or "")[:200]
        return f": {msg}" if msg else ""
    d = str(body.get("detail") or "")[:200]
    return f": {d}" if d else ""


# Plan-usage error codes and what the person is told (errors-and-recovery, "Structured Responses errors").
PLAN_ERRORS = {
    "subscription_sharing_user_not_eligible":
        "ChatGPT plan usage is not available to this account, its workspace or its policy, so hope cannot use it, and "
        "will not ask again while this runs. You can power your seat another way: in the field, "
        "{\"action\":\"read\",\"about\":\"powering\"} says how.",
    "subscription_sharing_usage_unavailable":
        "ChatGPT could not check your plan's usage just now. Your sign-in is kept; try again later.",
    "subscription_sharing_unsupported_capability":
        "ChatGPT plan usage does not support part of what hope sent ({param}).",
    "subscription_sharing_route_not_supported":
        "ChatGPT plan usage does not support the way hope asked. Please report this to hope.",
    "subscription_sharing_invalid_user":
        "OpenAI could not confirm your ChatGPT account for plan usage. " + SIGN_IN_AGAIN,
    "chatpass_v2_scope_not_authorized":
        "Your ChatGPT sign-in does not permit plan usage for this request. Sign in again and allow hope to use "
        "your plan: python3 -m hope plan sign-in chatgpt",
    "chatpass_v2_invalid_authorization_context":
        "Your ChatGPT sign-in does not permit plan usage for this request. Sign in again and allow hope to use "
        "your plan: python3 -m hope plan sign-in chatgpt",
    "subscription_sharing_user_unavailable":
        "Your ChatGPT account's details are unavailable just now. Your sign-in is kept; try again later.",
}
LIMIT_CODE = "subscription_sharing_usage_limit_exceeded"
TRY_LATER = ("subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable")


def plan_error(code: str, param: str = "", status: int = 0, detail: str = "", request_id: str = "") -> ConnectorError:
    """The plain-words error for a failed plan request. A usage limit is its own kind."""
    rid = f" (OpenAI request {request_id})" if request_id else ""
    if code == LIMIT_CODE:
        return UsageLimitReached(usage_limit_text() + rid)
    if code in PLAN_ERRORS:
        return ConnectorError(PLAN_ERRORS[code].replace("{param}", param or "unnamed") + rid)
    if status == 401:
        return ConnectorError("OpenAI did not accept your ChatGPT sign-in for plan usage (HTTP 401). Check you "
                              "signed in with the account you meant to. " + SIGN_IN_AGAIN + rid)
    if status == 403:
        return ConnectorError(f"OpenAI refused this plan request (HTTP 403{': ' + detail if detail else ''}), for "
                              f"example because of where you are or your workspace's policy.{rid}")
    if status == 503:
        return ConnectorError(f"ChatGPT plan usage is unavailable just now (HTTP 503). Your sign-in is kept; try "
                              f"again later.{rid}")
    what = code or (f"HTTP {status}" if status else "no reason given")
    return ConnectorError(f"ChatGPT did not answer ({what}{': ' + detail if detail else ''}).{rid}")


class ChatGPTPlanConnector:
    """One seat, powered by its person's own ChatGPT plan, for one chosen model. It calls OpenAI
    only when ask() is called, and hope's join command calls ask() only when the person asks their
    seat to take a turn. Its replies cost the field nothing: they count against the person's plan."""

    provider_label = "ChatGPT plan"
    provider_name = "chatgpt"

    def __init__(self, model: str, display_name: Optional[str] = None, issuer: str = ISSUER, api: str = API,
                 folder: Optional[str] = None, timeout: float = 240.0, reasoning_effort: Optional[str] = None):
        self.issuer, self.api, self.folder, self.timeout = issuer, api.rstrip("/"), folder, timeout
        self.reasoning_effort = reasoning_effort
        # What OpenAI said not to send again (errors-and-recovery): fields plan usage refused, left out of
        # every later request; and a refusal that ends this run's requests (not eligible, or a refused part
        # hope cannot leave out), raised at once without sending anything.
        self._refused: set = set()
        self._stopped: Optional[ConnectorError] = None
        slug = "".join(c if c.isalnum() or c in "-._" else "_" for c in model)
        self._seat = Seat(id=f"chatgpt__{slug}", name=display_name or model,
                          hails_from="OpenAI, through its person's own ChatGPT plan",
                          people=f"{model} (through Sign in with ChatGPT)", model=model,
                          pricing={"prompt": 0.0, "completion": 0.0}, powered=dict(POWERED))

    def seats(self) -> List[Seat]:
        return [self._seat]

    def body(self, seat: Seat, system: str, messages: List[dict]) -> dict:
        """The Responses request, within what plan usage accepts: store false, stream true, the
        system prompt as "instructions", the conversation as message items (none with the system
        role), and none of the fields it rejects (temperature, max_output_tokens, metadata,
        previous_response_id and the rest)."""
        items = []
        for m in messages:
            role = m.get("role") if m.get("role") in ("user", "assistant", "developer") else "developer"
            content = m.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            items.append({"type": "message", "role": role, "content": content})
        body = {"model": seat.model, "instructions": system, "input": items, "store": False, "stream": True}
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        for k in self._refused:
            body.pop(k, None)
        return body

    def ask(self, seat: Seat, system: str, messages: List[dict]) -> Reply:
        if self._stopped is not None:
            raise self._stopped                  # OpenAI asked that it not be sent again: nothing is sent
        data = json.dumps(self.body(seat, system, messages), ensure_ascii=False).encode("utf-8")
        if len(data) > BODY_LIMIT:
            raise ConnectorError(f"This turn is too large to send through ChatGPT plan usage: about {len(data) // 1000} "
                                 f"KB, where it accepts about {BODY_LIMIT // 1000} KB. Nothing was sent.")
        last: Optional[ConnectorError] = None
        for attempt in range(2):
            try:
                return self._call(seat, data)
            except _TryLater as e:
                last = e.error
                time.sleep(1.5 * (attempt + 1))
        raise last

    def _call(self, seat: Seat, data: bytes) -> Reply:
        token = access_token(self.issuer, self.folder)
        req = urllib.request.Request(self.api + "/responses", data=data, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "text/event-stream", "User-Agent": USER_AGENT})
        try:
            r = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = json.loads(raw) if raw.strip() else {}
            except ValueError:
                body = {"detail": raw.decode("utf-8", "replace")[:300]}
            body = body if isinstance(body, dict) else {}
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            code = str(err.get("code") or "")
            detail = str(err.get("message") or body.get("detail") or "")[:300]
            param = str(err.get("param") or "")
            failure = plan_error(code, param, e.code, detail, e.headers.get("x-request-id") or "")
            if code in TRY_LATER or (not code and e.code in (502, 503, 504)):
                raise _TryLater(failure)
            raise self._heed(code, param, failure)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise _TryLater(ConnectorError(f"hope could not reach ChatGPT ({type(e).__name__}). Try again later."))
        with r:
            try:
                return self._read_stream(r, r.headers.get("x-request-id") or "")
            except (OSError, http.client.HTTPException) as e:
                # cut off partway: a plain error, not tried again on its own, so a half-sent turn is never repeated
                raise ConnectorError(f"ChatGPT's answer was cut off ({type(e).__name__}). Nothing was posted; try again.")

    # Fields hope may leave out when plan usage refuses them; the rest are what a request is.
    _ESSENTIAL = ("model", "instructions", "input", "store", "stream")

    def _heed(self, code: str, param: str, failure: ConnectorError) -> ConnectorError:
        """Do what OpenAI's errors-and-recovery asks: for an unsupported part, never send the same body
        again (leave the part out, or stop when it is one a request cannot go without); for an account
        that is not eligible, do not ask again. Returns the error, in words that say which was done."""
        if code == "subscription_sharing_unsupported_capability":
            top = (param or "").split(".")[0].split("[")[0].strip()
            if top and top not in self._ESSENTIAL:
                self._refused.add(top)
                return ConnectorError(f"{failure} hope leaves it out of every later request while this runs; ask for "
                                      f"the turn again.")
            self._stopped = ConnectorError(f"{failure} hope cannot leave that out, so it will not send this request again "
                                           f"while this runs. Choose another model (--model ID), or report this to hope.")
            return self._stopped
        if code == "subscription_sharing_user_not_eligible":
            self._stopped = failure
        return failure

    def _read_stream(self, stream, request_id: str) -> Reply:
        """Read the server-sent events: text arrives as deltas, and the answer counts only once
        response.completed arrives."""
        parts: List[str] = []
        event_name, lines = "", []
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if line.startswith("data:"):
                lines.append(line[5:].lstrip(" "))
                continue
            if line or not lines:
                continue
            payload, lines = "\n".join(lines), []
            name, event_name = event_name, ""
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            kind = event.get("type") or name
            if kind == "response.output_text.delta":
                parts.append(str(event.get("delta") or ""))
            elif kind == "response.completed":
                response = event.get("response") or {}
                text = "".join(parts) or _output_text(response)
                usage = response.get("usage") or {}
                return Reply(text=text, prompt_tokens=int(usage.get("input_tokens") or 0),
                             completion_tokens=int(usage.get("output_tokens") or 0), cost_usd=0.0,
                             raw={"id": response.get("id"), "model": response.get("model"), "usage": usage,
                                  "powered": dict(POWERED)})
            elif kind == "response.failed":
                err = (event.get("response") or {}).get("error") or {}
                code, param = str(err.get("code") or ""), str(err.get("param") or "")
                raise self._heed(code, param, plan_error(code, param, 0, str(err.get("message") or "")[:300], request_id))
            elif kind == "response.incomplete":
                why = ((event.get("response") or {}).get("incomplete_details") or {}).get("reason") or "no reason given"
                raise ConnectorError(f"ChatGPT stopped before finishing its answer ({why}). Nothing was posted.")
            elif kind == "error":
                err = event.get("error") if isinstance(event.get("error"), dict) else event
                code, param = str(err.get("code") or ""), str(err.get("param") or "")
                raise self._heed(code, param, plan_error(code, param, 0, str(err.get("message") or "")[:300], request_id))
        raise ConnectorError("ChatGPT's answer stopped before it was complete. Nothing was posted; try again.")

    def close(self) -> None:
        pass


class _TryLater(Exception):
    """A failure that may pass: tried once more after a short wait, then told."""

    def __init__(self, error: ConnectorError):
        super().__init__(str(error))
        self.error = error


def _output_text(response: dict) -> str:
    out = []
    for item in response.get("output") or []:
        for c in (item or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "output_text":
                out.append(str(c.get("text") or ""))
    return "".join(out)
