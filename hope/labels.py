# SPDX-License-Identifier: AGPL-3.0-or-later
"""Domain labels, normalised for READING only. The record keeps every label exactly as a
participant wrote it; this collapses the mechanical variants (case, separators, plural) so a
reader sees one region where the field meant one topic. Near-synonyms are never merged: whether
"purpose of this field" and "field-purpose" are one domain is the participants' call. `near`
only lets the view point one out, and an author may move their own entries (the relabel action)."""
from __future__ import annotations

import re
from typing import Dict, Iterable, List

STOP = {"the", "of", "and", "a", "an", "this", "for", "in", "on", "vs", "versus"}


def normalize(label: str) -> str:
    s = (label or "").lower().strip()
    s = re.sub(r"[\s_./\\-]+", "-", s)
    s = re.sub(r"[^a-z0-9\-\u4e00-\u9fff]", "", s)
    parts = [p for p in s.split("-") if p]
    parts = [p[:-1] if len(p) > 3 and p.endswith("s") and not p.endswith("ss") else p for p in parts]
    kept = [p for p in parts if p not in STOP] or parts
    return "-".join(kept) or s or "(unplaced)"


def words(label: str) -> set:
    """The words a label is made of, once spelling, plurals and small words are set aside."""
    return {w for w in normalize(label).split("-") if w and w != "unplaced"}


def near(a: str, b: str) -> bool:
    """Two labels that are not one topic by spelling, but share most of their words, such as
    "purpose of this field" and "field purpose". Only ever a suggestion: nothing is merged, and
    each label stays as its author wrote it unless its author moves their own entries."""
    if normalize(a) == normalize(b):
        return False
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.5


# -- domains nest, by name ---------------------------------------------------------------
# "timing / clocks" is the domain "clocks" inside "timing". A path is its normalised segments
# joined by "/", so "Timing/Clocks" and "timing / clock" are one place; the root, the field
# itself, is "". Domains flow down, as folders do: what is in "timing / clocks" is also in
# "timing", and what belongs to "timing" (its covenant page, following it) reaches all of it.

def segments(label: str) -> List[str]:
    """The label's parts, each on one line, as its author wrote them."""
    return [" ".join(s.split()) for s in str(label or "").split("/") if s.strip()]


def display(label: str) -> str:
    return " / ".join(segments(label))


def path(label: str) -> str:
    return "/".join(normalize(s) for s in segments(label))


def parents(p: str) -> List[str]:
    """A path and every domain above it, outermost first; the root is not among them."""
    parts = [x for x in (p or "").split("/") if x]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def under(p: str, q: str) -> bool:
    """Whether path p is q or nested inside it. Everything is under the root only in the tree:
    the root's channel holds what was written without a domain."""
    if not q:
        return not p
    return p == q or p.startswith(q + "/")


def canonical(labels: Iterable[str], counts: Dict[str, int]) -> Dict[str, str]:
    """label -> the most-used spelling in its normalised group. Reading names stay the
    participants' own words; the group is just spelled the way most of them spelled it."""
    groups: Dict[str, list] = {}
    for l in labels:
        groups.setdefault(normalize(l), []).append(l)
    out = {}
    for key, members in groups.items():
        best = max(members, key=lambda m: (counts.get(m, 0), -len(m)))
        for m in members:
            out[m] = best
    return out
