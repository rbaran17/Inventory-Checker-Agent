"""Audit knowledge sources behind one small interface."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class GuidanceSnippet:
    id: str
    title: str
    text: str
    source: str  # where it came from, e.g. "standin/references/test-counts.md" or the service's doc id
    refs: list[str] = field(default_factory=list)  # standard references, e.g. ["ISA 501.4", "AU-C 501.11"]
    topics: list[str] = field(default_factory=list)
    url: str | None = None
    score: float = 0.0
    authoritative: bool = True  # False for the bundled stand-in

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "text": self.text, "source": self.source, "refs": self.refs,
                "topics": self.topics, "url": self.url, "score": round(self.score, 3),
                "authoritative": self.authoritative}


@runtime_checkable
class AuditKnowledgeSource(Protocol):
    name: str

    def search(self, query: str, *, topic: str | None = None, top_k: int = 5) -> list[GuidanceSnippet]: ...


# ----------------------------------------------------------------------------- skill folder

_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_STOP = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were", "will", "with", "should", "must", "may", "can", "not", "no", "do", "does", "how", "what", "when", "which", "who"]
)
_META = re.compile(r"^>\s*(refs|topics)\s*:\s*(.+)$", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(text.lower()):
        if t in _STOP:
            continue
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


class SkillFolderSource:
    """Searches a skill folder: ``SKILL.md`` and ``references/**/*.md``, split into sections by heading.

    A section may carry metadata lines right under its heading::

        > refs: ISA 501.4(a); AU-C 501.11
        > topics: test_counts, planning
    """

    def __init__(self, root: str | Path, *, name: str | None = None, authoritative: bool = True):
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"Audit knowledge folder not found: {self.root}")
        self.name = name or self.root.name
        self.authoritative = authoritative
        self.snippets = self._load()
        self._index()

    def _load(self) -> list[GuidanceSnippet]:
        snippets: list[GuidanceSnippet] = []
        for path in sorted(p for p in self.root.rglob("*.md") if p.is_file()):
            rel = path.relative_to(self.root).as_posix()
            text = path.read_text(encoding="utf-8")
            if text.startswith("---"):  # skip YAML front matter
                end = text.find("\n---", 3)
                text = text[end + 4:] if end != -1 else text
            sections: list[dict[str, Any]] = []
            for line in text.splitlines():
                h = re.match(r"^#{1,4}\s+(.*)$", line)
                if h:
                    sections.append({"title": h.group(1).strip(), "refs": [], "topics": [], "body": []})
                    continue
                if not sections:
                    continue
                cur = sections[-1]
                m = _META.match(line.strip())
                if m and not any(b.strip() for b in cur["body"]):
                    vals = [v.strip() for v in re.split(r"[;,]", m.group(2)) if v.strip()]
                    cur["refs" if m.group(1).lower() == "refs" else "topics"].extend(vals)
                    continue
                cur["body"].append(line)
            for sec in sections:
                content = "\n".join(sec["body"]).strip()
                if content:
                    snippets.append(GuidanceSnippet(
                        id=f"{path.stem}#{_slug(sec['title'])}", title=sec["title"], text=content, source=rel,
                        refs=sec["refs"], topics=sec["topics"], authoritative=self.authoritative))
        return snippets

    def _index(self) -> None:
        self._docs = [Counter(_tokens(f"{s.title} {s.title} {' '.join(s.refs)} {' '.join(s.topics)} {s.text}"))
                      for s in self.snippets]
        self._avg = sum(sum(d.values()) for d in self._docs) / max(1, len(self._docs))
        df: Counter[str] = Counter()
        for d in self._docs:
            df.update(d.keys())
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, *, topic: str | None = None, top_k: int = 5) -> list[GuidanceSnippet]:
        q = _tokens(query)
        k1, b = 1.5, 0.75
        scored = []
        for s, d in zip(self.snippets, self._docs):
            if topic and topic not in s.topics:
                continue
            length = sum(d.values())
            score = sum(self._idf.get(t, 0) * d[t] * (k1 + 1) / (d[t] + k1 * (1 - b + b * length / self._avg))
                        for t in q if t in d)
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, s in scored[:top_k]:
            snip = GuidanceSnippet(**{**s.__dict__, "score": score})
            out.append(snip)
        return out


def standin_source() -> SkillFolderSource:
    root = resources.files("inventory_checker").joinpath("knowledge", "standin")
    return SkillFolderSource(Path(str(root)), name="standin", authoritative=False)


# ----------------------------------------------------------------------------- HTTP service


class HttpKnowledgeSource:
    """Calls a search service.

    Request:  ``POST <url>`` with ``{"query": str, "top_k": int, "topic": str | null, "domain": "inventory"}``
    Response: ``{"results": [{"id", "title", "text", "source", "refs"?, "topics"?, "url"?, "score"?}]}``
    """

    def __init__(self, url: str, *, token: str | None = None, timeout: float = 10.0, name: str = "http"):
        self.url, self.token, self.timeout, self.name = url, token, timeout, name

    def search(self, query: str, *, topic: str | None = None, top_k: int = 5) -> list[GuidanceSnippet]:
        body = json.dumps({"query": query, "top_k": top_k, "topic": topic, "domain": "inventory"}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        out = []
        for r in data.get("results", [])[:top_k]:
            out.append(GuidanceSnippet(
                id=str(r["id"]), title=r.get("title", ""), text=r.get("text", ""), source=r.get("source", self.name),
                refs=list(r.get("refs") or []), topics=list(r.get("topics") or []), url=r.get("url"),
                score=float(r.get("score") or 0)))
        return out


# ----------------------------------------------------------------------------- configuration


def knowledge_from_env() -> AuditKnowledgeSource | None:
    spec = os.getenv("AUDIT_KNOWLEDGE_SOURCE", "standin").strip()
    if spec in ("", "standin"):
        return standin_source()
    if spec == "none":
        return None
    if spec.startswith("dir:"):
        return SkillFolderSource(spec[4:])
    if spec.startswith(("http:", "https:")):
        url = spec[5:] if spec.startswith("http:http") else spec
        return HttpKnowledgeSource(url, token=os.getenv("AUDIT_KNOWLEDGE_TOKEN"))
    raise ValueError(f"Unknown AUDIT_KNOWLEDGE_SOURCE {spec!r}")
