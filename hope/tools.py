# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tools for the field (notes/sketch-4-tools.md).

Tools reach the field from everyone. Participants bring their own. The operator attaches tool
servers in a tools file (a command run on the operator's machine, or an address). Any member
offers one by its address: a server they run on a machine they lend, or one they know of. The
door is MCP, the one Hermes and OpenClaw use, so the same servers serve here.

What this holds to:
  - hope's own process never runs a participant's code. A member offers a server by its address
    only, and it must be a public HTTPS address: nothing on loopback, a private network, or
    link-local addresses, so no one can point the field at the operator's own machine or
    network. The built-in fetch refuses those too. The operator may attach local servers of
    their own; that is their consent to their own machine.
  - At most MAX_READ bytes are read from any server or page, to protect the machine. A result is
    otherwise kept whole; the views carry it within their budget, as they do everything.
  - Keys are named by environment variable (key_env for an address; env_pass or env_from for a
    command), read from the operator's terminal when needed, and written nowhere. A command gets
    only the environment it needs to start, and what its entry names (child_env).
  - Every tool says who runs it and where what it is given goes, and whoever attached it says
    its kind (reading, working, acting). MCP's own hints are kept beside it.
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

MAX_READ = 5 * 1024 * 1024          # bytes read from one server or page, at most
KINDS = ("reading", "working", "acting")
PROTOCOL = "2025-11-25"             # what most servers speak today; the 2026-07-28 form is tried after


class ToolError(Exception):
    pass


# -- where a member may point the field -------------------------------------------------------------
def public_https(url: str) -> Optional[str]:
    """Why a member may not offer this address, or None if it is a public HTTPS address."""
    u = urlparse(url or "")
    if u.scheme != "https" or not u.hostname:
        return "a tool server offered by a member must be at an https:// address"
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return f"cannot find {u.hostname}: {e}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return (f"{u.hostname} is at {ip}, which is not a public address; a member cannot point the field at "
                    f"the operator's machine or a private network")
    return None


def public_web(url: str) -> Optional[str]:
    """Why fetch may not read this address, or None: http or https, and public."""
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        return "fetch reads http:// and https:// addresses"
    return public_https("https://" + u.netloc) if u.scheme == "http" else public_https(url)


def _read(resp, limit: int = MAX_READ) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise ToolError(f"the answer was larger than {limit // (1024 * 1024)} MB; nothing was kept")
    return data


# -- MCP clients ---------------------------------------------------------------------------------------
# What a command the operator attaches is given of the operator's environment: what programs need
# to start (paths, the temporary folder, the home folder), and nothing else unless the tools file
# names it (env_pass, env_from). So no key in the operator's terminal reaches a tool it was not meant for.
BASE_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME",
            "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
            "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG",
            "LC_ALL", "TERM", "USER", "LOGNAME", "SHELL", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR", "NVM_DIR", "DENO_DIR", "UV_CACHE_DIR", "DOCKER_HOST")
SECRET_NAME = re.compile(r"(?i)(key|token|secret|password|passwd|credential|auth)")


def child_env(literal: Optional[Dict[str, str]] = None, passed: Optional[List[str]] = None,
              renamed: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment a tool command gets: BASE_ENV, the names passed through, the ones renamed
    ({"NAME_IT_WANTS": "NAME_IN_YOUR_TERMINAL"}), and plain settings that are not secrets."""
    have = {k.upper(): v for k, v in os.environ.items()}
    env = {k: have[k] for k in BASE_ENV if k in have}
    for name in passed or []:
        if name.upper() in have:
            env[name] = have[name.upper()]
    for child, mine in (renamed or {}).items():
        if mine.upper() in have:
            env[child] = have[mine.upper()]
    for k, v in (literal or {}).items():
        if SECRET_NAME.search(k):
            raise ToolError(f"{k} looks like a key; a key never goes in the tools file. Name it in \"env_pass\" "
                            f"(or \"env_from\") and set it in the terminal that starts hope")
        env[k] = str(v)
    return env


class StdioServer:
    """An MCP server run as a command on the operator's machine (the operator's to attach)."""

    def __init__(self, command: List[str], env: Optional[Dict[str, str]] = None, timeout: float = 60.0):
        exe = shutil.which(command[0]) if command else None
        self.command, self.timeout = ([exe] + list(command[1:])) if exe else list(command), timeout
        self._env = dict(env) if env is not None else child_env()
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._id = 0

    def _start(self) -> None:
        try:
            self._proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, env=self._env, text=True, encoding="utf-8",
                                          errors="replace", bufsize=1)
        except OSError as e:
            raise ToolError(f"could not start {self.command[0]}: {e}")
        lines = self._lines = queue.Queue()             # a new queue for each start: an old one ends with a stop

        def pump(out):
            try:
                for line in out:
                    lines.put(line)
            except (OSError, ValueError):
                pass
            lines.put(None)
        threading.Thread(target=pump, args=(self._proc.stdout,), daemon=True).start()
        # A first start may download the program (npx, uvx), so it is given longer than a call.
        self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "hope", "version": "0.3"}}, timeout=max(self.timeout, 300.0))
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise ToolError(f"the tool server stopped: {e}")

    def _rpc(self, method: str, params: dict, timeout: Optional[float] = None) -> dict:
        self._id += 1
        mid = self._id
        wait = timeout or self.timeout
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        got = 0
        while True:
            try:
                line = self._lines.get(timeout=wait)
            except queue.Empty:
                raise ToolError(f"the tool server did not answer {method} in {wait:g} seconds")
            if line is None:
                raise ToolError("the tool server stopped")
            got += len(line)
            if got > MAX_READ:
                raise ToolError("the tool server's answer was too large")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != mid:
                continue                     # a notification, or a log line
            if msg.get("error"):
                raise ToolError(f"{msg['error'].get('code')}: {msg['error'].get('message')}")
            return msg.get("result") or {}

    def request(self, method: str, params: dict) -> dict:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            return self._rpc(method, params)

    def close(self) -> None:
        p, self._proc = self._proc, None
        if p is None:
            return
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except (OSError, ValueError):
                pass
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


class HttpServer:
    """An MCP server at an address, over Streamable HTTP: initialize first, as most servers expect,
    keeping any session id it gives; the 2026-07-28 form if the server wants that instead."""

    def __init__(self, url: str, key_env: Optional[str] = None, timeout: float = 60.0,
                 check: Optional[Callable[[str], Optional[str]]] = None):
        self.url, self.key_env, self.timeout, self.check = url, key_env, timeout, check
        self._lock = threading.Lock()
        self._session: Optional[str] = None
        self._ready = False
        self._modern = False
        self._id = 0

    def _headers(self, method: str, name: Optional[str]) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": "hope/0.3", "MCP-Protocol-Version": "2026-07-28" if self._modern else PROTOCOL}
        if self._modern:
            h["Mcp-Method"] = method
            if name:
                h["Mcp-Name"] = name
        if self._session:
            h["Mcp-Session-Id"] = self._session
        if self.key_env:
            key = os.environ.get(self.key_env, "").strip()
            if not key:
                raise ToolError(f"no key: set {self.key_env} in the operator's terminal (it is read from the "
                                f"environment and written nowhere)")
            h["Authorization"] = f"Bearer {key}"
        return h

    def _post(self, msg: dict) -> Optional[dict]:
        if self.check:
            why = self.check(self.url)             # checked at every call, so a name cannot move later
            if why:
                raise ToolError(why)
        params = msg.get("params") or {}
        if self._modern and "id" in msg:
            params = {**params, "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                          "io.modelcontextprotocol/clientInfo": {"name": "hope", "version": "0.3"},
                                          "io.modelcontextprotocol/clientCapabilities": {}}}
            msg = {**msg, "params": params}
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(),
                                     headers=self._headers(msg["method"], params.get("name")))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self._session = r.headers.get("Mcp-Session-Id") or self._session
                body = _read(r)
                ctype = r.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as e:
            try:
                return json.loads(_read(e) or b"null")
            except ValueError:
                raise ToolError(f"HTTP {e.code} from the tool server")
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"the tool server could not be reached: {e}")
        if not body:
            return None
        text = body.decode("utf-8", "replace")
        if "text/event-stream" in ctype:
            for line in text.splitlines():             # the response is the last data line that is ours
                if line.startswith("data:"):
                    try:
                        m = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(m, dict) and m.get("id") == msg.get("id"):
                        return m
            return None
        return json.loads(text)

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        out = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if not isinstance(out, dict):
            raise ToolError(f"the tool server gave no answer to {method}")
        if out.get("error"):
            raise ToolError(f"{out['error'].get('code')}: {out['error'].get('message')}")
        return out.get("result") or {}

    def _open(self) -> None:
        try:
            self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                     "clientInfo": {"name": "hope", "version": "0.3"}})
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except ToolError as e:
            if "-32020" not in str(e) and "-32601" not in str(e) and "-32022" not in str(e):
                raise
            self._modern = True                        # a server of the 2026-07-28 revision: no handshake
        self._ready = True

    def request(self, method: str, params: dict) -> dict:
        with self._lock:
            if not self._ready:
                self._open()
            return self._rpc(method, params)

    def close(self) -> None:
        pass


# -- the built-in tool: fetch --------------------------------------------------------------------------
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre"}

    def __init__(self):
        super().__init__()
        self.out, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith("http"):
                self.out.append(f" [{href}] ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def fetch(url: str, allow_local: bool = False) -> str:
    """Read a web page as text. A reading tool; it sends the address to that site."""
    if not allow_local:
        why = public_web(url)
        if why:
            raise ToolError(why)
    req = urllib.request.Request(url, headers={"User-Agent": "hope/0.3 (a coordination field reading a page)",
                                               "Accept": "text/html, text/plain, application/json;q=0.9, */*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = _read(r)
            ctype = r.headers.get("Content-Type") or ""
    except urllib.error.HTTPError as e:
        raise ToolError(f"{url} answered HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise ToolError(f"{url} could not be read: {e}")
    charset = re.search(r"charset=([\w-]+)", ctype)
    text = body.decode(charset.group(1) if charset else "utf-8", "replace")
    if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
        p = _Text()
        p.feed(text)
        text = html.unescape("".join(p.out))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


# -- the hub: every tool the field has --------------------------------------------------------------------
@dataclass
class Server:
    name: str
    runner: str                                   # who runs it, as the field is told
    sends_to: str                                 # where what it is given goes
    kind: str = "reading"
    price: float = 0.0                            # USD a call, as whoever attached it states
    source: str = "operator"                      # "operator", or the member who offered it
    client: Any = None                            # StdioServer | HttpServer | None (built in)
    url: str = ""
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    description: str = ""


class ToolHub:
    """Every tool the field has: the built-in fetch, the operator's servers, and members' offers."""

    def __init__(self, builtin_fetch: bool = True, allow_local: bool = False):
        self.allow_local = allow_local
        self.servers: Dict[str, Server] = {}
        self.failed: List[str] = []                   # servers in the tools file that could not be started
        self.tools: Dict[str, Dict[str, Any]] = {}   # "server.tool" -> {server, name, description, schema, hints}
        if builtin_fetch:
            self.servers["web"] = Server("web", "the operator's machine (hope's own fetch)",
                                         "the site at the address given", "reading")
            self.tools["web.fetch"] = {
                "server": "web", "name": "fetch",
                "description": "Read a web page as text, links kept as [address]. It sends the address to that site.",
                "schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                "hints": {"readOnlyHint": True, "openWorldHint": True}}

    # attaching ------------------------------------------------------------------------------------------
    def load(self, path: str) -> List[str]:
        """The operator's tools file: {"servers": [{"name", "command": [...] | "url", "kind", "runner",
        "sends_to", "key_env", "env": {...}, "env_pass": [...], "env_from": {...}, "price_per_call",
        "include", "exclude", "enabled"}]}. A key is only ever named (key_env, env_pass, env_from) and
        read from the operator's terminal; a file with a key in it is refused. A server that cannot be
        started is left out, and said so in `failed`, so one broken tool does not stop the field."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        names = []
        self.failed: List[str] = []
        for spec in data.get("servers", []):
            if spec.get("enabled") is False:
                continue
            if any(k in spec for k in ("key", "api_key", "apikey", "token", "secret", "password")):
                raise ToolError(f"{path}: {spec.get('name')}: a key never goes in this file; name its environment "
                                f"variable with \"key_env\" or \"env_pass\"")
            try:
                names.append(self.attach(spec, source="operator"))
            except ToolError as e:
                self.failed.append(f"{spec.get('name')}: {e}")
        return names

    def attach(self, spec: Dict[str, Any], source: str = "operator") -> str:
        name = re.sub(r"[^a-z0-9_-]+", "-", str(spec.get("name") or "").lower()).strip("-")
        if not name:
            raise ToolError("a tool server needs a name")
        if name in self.servers:
            raise ToolError(f"there is already a tool server named {name!r}")
        kind = str(spec.get("kind") or "reading").lower()
        if kind not in KINDS:
            raise ToolError(f"a tool's kind is one of {', '.join(KINDS)}")
        if spec.get("command"):
            if source != "operator":
                raise ToolError("a member offers a tool server by its address; hope never runs a participant's code")
            env = child_env(spec.get("env"), spec.get("env_pass"), spec.get("env_from"))
            client = StdioServer(list(spec["command"]), env=env)
            where = spec.get("sends_to") or "a program on the operator's machine"
        elif spec.get("url"):
            check = None if (source == "operator" or self.allow_local) else public_https
            if check:
                why = check(spec["url"])
                if why:
                    raise ToolError(why)
            client = HttpServer(spec["url"], key_env=spec.get("key_env"), check=check)
            where = spec.get("sends_to") or f"the server at {urlparse(spec['url']).netloc}"
        else:
            raise ToolError("a tool server needs a command (the operator's) or a url")
        server = Server(name=name, runner=str(spec.get("runner") or ("the operator" if source == "operator" else source)),
                        sends_to=str(where), kind=kind, price=float(spec.get("price_per_call") or 0.0), source=source,
                        client=client, url=str(spec.get("url") or ""), include=list(spec.get("include") or []),
                        exclude=list(spec.get("exclude") or []), description=str(spec.get("description") or ""))
        try:
            listed = self._list(server)
        except ToolError:
            client.close()
            raise
        self.servers[name] = server
        for t in listed:
            self.tools[f"{name}.{t['name']}"] = {"server": name, "name": t["name"],
                                                  "description": str(t.get("description") or "")[:1000],
                                                  "schema": t.get("inputSchema") or {"type": "object"},
                                                  "hints": t.get("annotations") or {}}
        return name

    def _list(self, server: Server) -> List[dict]:
        out, cursor = [], None
        for _ in range(20):
            res = server.client.request("tools/list", {"cursor": cursor} if cursor else {})
            out += [t for t in res.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        if server.include:
            out = [t for t in out if t["name"] in server.include]
        if server.exclude:
            out = [t for t in out if t["name"] not in server.exclude]
        return out

    def remove(self, name: str) -> None:
        s = self.servers.pop(name, None)
        if s and s.client:
            s.client.close()
        for t in [t for t, v in self.tools.items() if v["server"] == name]:
            del self.tools[t]

    # describing ---------------------------------------------------------------------------------------------
    def kind_of(self, full: str) -> str:
        """The kind whoever attached it said, unless the tool's own hints say it changes things."""
        t = self.tools[full]
        s = self.servers[t["server"]]
        h = t.get("hints") or {}
        if s.kind != "acting" and (h.get("destructiveHint") is True or h.get("readOnlyHint") is False and s.kind == "reading"):
            return "acting"
        return s.kind

    def catalog(self) -> List[Dict[str, Any]]:
        """What the transcript records of each tool, so every view can say it (never a key)."""
        out = []
        for full in sorted(self.tools):
            t = self.tools[full]
            s = self.servers[t["server"]]
            required = [str(a) for a in t["schema"].get("required") or []]
            props = [str(a) for a in (t["schema"].get("properties") or {}).keys()]
            out.append({"tool": full, "description": t["description"], "kind": self.kind_of(full), "runner": s.runner,
                        "sends_to": s.sends_to, "price": s.price, "source": s.source,
                        "arguments": required + [a for a in props if a not in required],   # what it needs first
                        "required": required})
        return out

    # calling ---------------------------------------------------------------------------------------------------
    def call(self, full: str, arguments: Any) -> Tuple[str, bool, float]:
        """(the result as text, whether it is an error, what it cost)."""
        t = self.tools.get(full)
        if t is None:
            raise ToolError(f"there is no tool {full!r}")
        s = self.servers[t["server"]]
        if isinstance(arguments, str):                   # plain words: the tool's first required text argument
            props = t["schema"].get("properties") or {}
            first = next((k for k in (t["schema"].get("required") or list(props)) if
                          (props.get(k) or {}).get("type", "string") == "string"), None)
            arguments = {first: arguments} if first else {}
        if not isinstance(arguments, dict):
            raise ToolError("arguments are an object, as the tool describes, or plain words")
        if full == "web.fetch" and s.client is None:
            return fetch(str(arguments.get("url") or ""), self.allow_local), False, 0.0
        res = s.client.request("tools/call", {"name": t["name"], "arguments": arguments})
        parts = []
        for c in res.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text") or ""))
            elif isinstance(c, dict) and c.get("type") in ("resource_link", "resource"):
                parts.append(f"[{c.get('uri') or (c.get('resource') or {}).get('uri')}]")
            elif isinstance(c, dict):
                parts.append(f"[{c.get('type')} content, not shown as text]")
        if not parts and res.get("structuredContent") is not None:
            parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
        return "\n".join(parts), bool(res.get("isError")), s.price

    def close(self) -> None:
        for s in self.servers.values():
            if s.client:
                s.client.close()
