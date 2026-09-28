# SPDX-License-Identifier: AGPL-3.0-or-later
"""The field's skills, to and from the repository (notes/sketch-4-tools.md).

A skill is a folder with a SKILL.md: a name, a description, and instructions, the open format
(agentskills.io) that Hermes, OpenClaw, Claude Code and others share. The field writes its own;
writing one is its author's yes to its publication here, attributed, which every member is told.

  export: every skill the field holds, written to <dir>/<name>/SKILL.md with its authors, for the
          operator (or the field, through a tool that can change the repository) to commit.
  load:   the skills in a directory, for a new field to take up.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from .model import RoomState


def _quote(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(sk: Dict[str, Any], names: Dict[str, str], field: str = "") -> str:
    authors = ", ".join(names.get(a, a) for a in sk["authors"] if a != "operator") or "the operator"
    meta = [f"  authors: {_quote(authors)}", f"  revisions: {_quote(str(len(sk['revisions'])))}"]
    if field:
        meta.append(f"  field: {_quote(field)}")
    if sk.get("source"):
        meta.append(f"  source: {_quote(sk['source'])}")
    return ("---\n"
            f"name: {sk['name']}\n"
            f"description: {_quote(' '.join(sk['description'].split()))}\n"
            "metadata:\n" + "\n".join(meta) + "\n"
            "---\n\n" + sk["text"].strip() + "\n")


def export(st: RoomState, out_dir: str, field: str = "") -> List[str]:
    """Write every skill the field holds (not retired). Returns the files written."""
    names = {pid: p.name for pid, p in st.presences.items()}
    written = []
    for sk in sorted(st.skills.values(), key=lambda s: s["name"]):
        if not sk.get("text"):
            continue
        folder = os.path.join(out_dir, sk["name"])
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "SKILL.md")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(render(sk, names, field))
        written.append(path)
    return written


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
        if v and v[0] != "'":
            v = v.replace('\\"', '"').replace("\\\\", "\\")
    return v


def parse(text: str) -> Dict[str, Any]:
    """A SKILL.md: its frontmatter (name, description, metadata) and its instructions."""
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}
    head, body = m.group(1), m.group(2)
    out: Dict[str, Any] = {"metadata": {}}
    lines = head.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        mm = re.match(r"([A-Za-z_-]+):\s*(.*)$", line)
        i += 1
        if not mm:
            continue
        key, val = mm.group(1), mm.group(2)
        if val in (">", "|", ">-", "|-"):                    # a block of indented lines
            block = []
            while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            val = (" " if val.startswith(">") else "\n").join(x for x in block if x)
        elif key == "metadata" and not val:
            while i < len(lines) and lines[i].startswith(" "):
                sub = re.match(r"\s+([A-Za-z_-]+):\s*(.*)$", lines[i])
                if sub:
                    out["metadata"][sub.group(1)] = _unquote(sub.group(2))
                i += 1
            continue
        out[key] = _unquote(val)
    out["text"] = body.strip()
    return out


def load(directory: str) -> List[Dict[str, Any]]:
    """Every skill in a directory of skill folders."""
    found = []
    for entry in sorted(os.listdir(directory)):
        path = os.path.join(directory, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            sk = parse(f.read())
        name = re.sub(r"[^a-z0-9]+", "-", str(sk.get("name") or entry).lower()).strip("-")[:64]
        if name and sk.get("text"):
            found.append({"name": name, "description": sk.get("description") or "", "text": sk["text"],
                          "author": (sk.get("metadata") or {}).get("authors", ""),
                          "source": f"the repository, {os.path.relpath(path, directory)}"})
    return found
