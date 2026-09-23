"""Context graph: the harness's second brain. [Owner: context agent]

A typed, persistent graph of what you and the harness have touched and said.
Stored locally at <ws.root>/.sonic/context/graph.json (nothing leaves the machine).

Node: {"id": f"{type}:{key}", "type", "key", "label", "summary", "tags": [..],
       "created", "updated" (ISO-8601), "hits": int, "archived": bool, "data": {..}}
  types: task, file, symbol, command, note, topic
Edge: {"src", "dst", "rel", "weight": int}   (deduped on (src, dst, rel); repeats bump weight)
  rels: read, wrote, edited, ran, uses, defines, imports, mentions, tagged

Persistence is best-effort and never destructive:
- a graph.json that can't be parsed is renamed to graph.json.corrupt-<stamp> (never
  overwritten or deleted) and the graph starts empty; loadable-but-damaged nodes/edges
  are repaired or skipped;
- a graph.json from a newer VERSION is copied to graph.json.v<N>-<stamp> and loaded
  as far as possible;
- save() is atomic (temp file + os.replace) and never raises: on failure it warns once
  on stderr. Several ContextGraph instances on one workspace do not merge: the last
  save() wins.
"""
import ast
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sonic.errors import SandboxError
from sonic.planner import Step
from sonic.workspace import Workspace

GRAPH_PATH = ".sonic/context/graph.json"
VERSION = 1
MAX_PARSE_BYTES = 1_000_000   # .py files larger than this are not parsed for symbols
_MAX_LINK_CANDIDATES = 200    # path-like tokens checked per note

_FS_RELS = {"read_file": "read", "write_file": "wrote", "edit_file": "edited"}
_FIELD_WEIGHTS = (("label", 3), ("tags", 3), ("key", 2), ("summary", 1))
_STOPWORDS = frozenset(
    "the and for are but not you all any can had her was one our out has have his how its "
    "may new now see two who did does get got let say she too use via with this that from "
    "what when where which while will would should could into onto than then them they "
    "their there these those about after before being been also just only some such very "
    "your yours here were why is it of to in on at by an or".split()
)
_WORD = re.compile(r"\w+")
_HASHTAG = re.compile(r"(?<![\w#])#(\w[\w-]*)")
_QUOTED = re.compile(r"\"([^\"\n]+)\"|'([^'\n]+)'|`([^`\n]+)`")
_PATHISH = re.compile(r"[\w./-]+")
_SPREAD = 0.5
_TYPE_BOOST = {"note": 2.0, "task": 1.0, "topic": 1.0, "file": 0.6, "symbol": 0.6, "command": 0.6}
_INVERSE = {"read": "read by", "wrote": "written by", "edited": "edited by", "ran": "run by",
            "uses": "used by", "defines": "defined in", "imports": "imported by",
            "mentions": "mentioned in", "tagged": "tagged on"}
_RECENCY_MAX = 0.5
_RECENCY_DAYS = 7.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _terms(text: str) -> set[str]:
    """Lowercase words of >= 3 chars (>= 2 if non-ASCII), minus stopwords, naive plural strip."""
    out = set()
    for w in _WORD.findall(text.lower()):
        if len(w) < (3 if w.isascii() else 2) or w in _STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def _trim(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class ContextGraph:
    def __init__(self, ws: Workspace):
        """Load GRAPH_PATH if it exists, else start empty."""
        self.ws = ws
        self.path = ws.root / GRAPH_PATH
        self.task_counter = 0
        self._nodes: dict[str, dict] = {}
        self._edges: dict[tuple[str, str, str], dict] = {}
        self._adj: dict[str, set[str]] = {}
        self._incident: dict[str, set[tuple[str, str, str]]] = {}
        self._index: dict[str, set[str]] = {}
        self._node_terms: dict[str, dict[str, int]] = {}
        self._pending: dict[str, set[str]] = {}
        self._save_disabled: str | None = None
        self._save_warned = False
        self._load()

    # --- persistence ---

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = self.path.read_bytes()
        except OSError as e:
            # Can't read it, so don't overwrite it either.
            self._save_disabled = f"could not read {self.path}: {e}"
            self._warn(f"context graph not loaded ({e}); changes this session won't be saved")
            return
        try:
            raw = json.loads(data.decode("utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), list):
                raise ValueError("expected an object with a 'nodes' list")
            edges = raw.get("edges", [])
            if not isinstance(edges, list):
                raise ValueError("'edges' is not a list")
        except (ValueError, UnicodeDecodeError) as e:
            kept = self._backup("corrupt", move=True)
            where = f"moved to {kept.name}" if kept else "left in place"
            self._warn(f"context graph {self.path} is unreadable ({e}); {where}, starting empty")
            if kept is None:
                self._save_disabled = "the unreadable graph file could not be moved aside"
            return
        version = raw.get("version", VERSION)
        if not isinstance(version, int) or version > VERSION:
            kept = self._backup(f"v{version}", move=False)
            self._warn(f"context graph was written by a newer version ({version!r}); loading what "
                       f"is compatible" + (f", original kept as {kept.name}" if kept else ""))
            if kept is None:
                self._save_disabled = "could not back up a newer-version graph"
        try:
            self.task_counter = max(int(raw.get("task_counter", 0)), 0)
        except (TypeError, ValueError):
            self.task_counter = 0
        for n in raw["nodes"]:
            if (n := self._repair_node(n)) is None:
                continue
            self._nodes[n["id"]] = n
            self._reindex(n["id"])
            for group in n["data"].get("pending_imports", []):
                for c in group:
                    self._pending.setdefault(c, set()).add(n["id"])
        for e in edges:
            if not isinstance(e, dict):
                continue
            src, dst, rel = e.get("src"), e.get("dst"), e.get("rel")
            if not all(isinstance(x, str) for x in (src, dst, rel)) or not rel:
                continue
            if src in self._nodes and dst in self._nodes and src != dst:
                try:
                    weight = max(int(e.get("weight", 1)), 1)
                except (TypeError, ValueError):
                    weight = 1
                self._link(src, dst, rel, weight)

    @staticmethod
    def _repair_node(n: object) -> dict | None:
        """Fill in / coerce a loaded node's fields; None if it has no usable id."""
        if not isinstance(n, dict) or not isinstance(n.get("id"), str) or not n["id"]:
            return None
        type_, _, key = n["id"].partition(":")
        if not isinstance(n.get("type"), str):
            n["type"] = type_ if key else "note"
        if not isinstance(n.get("key"), str):
            n["key"] = key or n["id"]
        if not isinstance(n.get("label"), str) or not n["label"]:
            n["label"] = n["key"]
        if not isinstance(n.get("summary"), str):
            n["summary"] = ""
        tags = n.get("tags")
        n["tags"] = [t for t in tags if isinstance(t, str) and t] if isinstance(tags, list) else []
        if not isinstance(n.get("data"), dict):
            n["data"] = {}
        pending = n["data"].get("pending_imports")
        if pending is not None:
            ok = isinstance(pending, list) and all(
                isinstance(g, list) and all(isinstance(c, str) for c in g) for g in pending)
            if not ok:
                n["data"].pop("pending_imports")
        if not isinstance(n.get("hits"), int) or isinstance(n["hits"], bool):
            n["hits"] = 1
        n["archived"] = n.get("archived") is True
        now = _now()
        for field in ("created", "updated"):
            if not isinstance(n.get(field), str):
                n[field] = now
        return n

    def _backup(self, tag: str, move: bool) -> Path | None:
        """Rename (move=True) or copy graph.json to a fresh graph.json.<tag>-<stamp>[-N].
        Never overwrites an existing file and never deletes anything."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"{self.path.name}.{re.sub(r'[^A-Za-z0-9_.-]', '_', tag)[:40]}-{stamp}"
        for i in range(1000):
            dest = self.path.with_name(base if i == 0 else f"{base}-{i}")
            try:
                if move:
                    if os.path.lexists(dest):
                        continue
                    os.rename(self.path, dest)
                else:
                    with open(dest, "xb") as f:     # exclusive create: fails if dest exists
                        f.write(self.path.read_bytes())
                    shutil.copystat(self.path, dest)
                return dest
            except FileExistsError:
                continue
            except OSError:
                return None
        return None

    def _warn(self, message: str) -> None:
        print(f"warning: {message}", file=sys.stderr)

    def to_json(self) -> dict:
        """{"nodes": [...], "edges": [...]} for the UI."""
        return {"nodes": [dict(n, tags=list(n["tags"]), data=dict(n["data"]))
                          for n in self._nodes.values()],
                "edges": [dict(e) for e in self._edges.values()]}

    def save(self) -> bool:
        """Atomic write (temp file in the same dir + os.replace); last writer wins.
        Never raises: on failure warns once on stderr and returns False."""
        if self._save_disabled:
            return False
        try:
            payload = {"version": VERSION, "task_counter": self.task_counter, **self.to_json()}
            text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".graph-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
        except (OSError, ValueError, TypeError) as e:
            if not self._save_warned:
                self._save_warned = True
                self._warn(f"context graph not saved ({e}); will keep trying silently")
            return False
        return True

    # --- core graph ---

    def _reindex(self, id: str) -> None:
        n = self._nodes[id]
        for t in self._node_terms.get(id, {}):
            self._index.get(t, set()).discard(id)
        weights: dict[str, int] = {}
        for field, w in _FIELD_WEIGHTS:
            if field == "key" and n.get("key") == n.get("label"):
                continue
            value = n.get(field) or ""
            text = " ".join(value) if isinstance(value, list) else str(value)
            for t in _terms(text):
                weights[t] = weights.get(t, 0) + w
        self._node_terms[id] = weights
        for t in weights:
            self._index.setdefault(t, set()).add(id)

    def add_node(self, type: str, key: str, label: str | None = None, summary: str = "",
                 tags: list[str] | None = None, **data) -> str:
        """Upsert; returns the id. Re-adding bumps hits/updated and merges summary/tags/data."""
        id = f"{type}:{key}"
        now = _now()
        n = self._nodes.get(id)
        if n is None:
            n = {"id": id, "type": type, "key": key, "label": label or key, "summary": summary,
                 "tags": [], "created": now, "updated": now, "hits": 1, "archived": False,
                 "data": {}}
            self._nodes[id] = n
        else:
            n["hits"] += 1
            n["updated"] = now
            if label:
                n["label"] = label
            if summary:
                n["summary"] = summary
        for t in tags or []:
            if t and t not in n["tags"]:
                n["tags"].append(t)
        n["data"].update(data)
        self._reindex(id)
        if type == "file" and key in self._pending:
            self._resolve_pending(id, key)
        return id

    def add_edge(self, src: str, dst: str, rel: str) -> None:
        """Link two existing nodes; a repeat bumps the edge's weight."""
        if src == dst or src not in self._nodes or dst not in self._nodes:
            return
        e = self._edges.get((src, dst, rel))
        if e:
            e["weight"] += 1
        else:
            self._link(src, dst, rel, 1)

    def _link(self, src: str, dst: str, rel: str, weight: int) -> None:
        key = (src, dst, rel)
        self._edges[key] = {"src": src, "dst": dst, "rel": rel, "weight": weight}
        for a, b in ((src, dst), (dst, src)):
            self._adj.setdefault(a, set()).add(b)
            self._incident.setdefault(a, set()).add(key)

    def node(self, id: str) -> dict | None:
        n = self._nodes.get(id)
        return dict(n, tags=list(n["tags"]), data=dict(n["data"])) if n else None

    def neighbors(self, id: str, depth: int = 1) -> list[dict]:
        """Nodes within `depth` hops (either direction), excluding `id` itself."""
        seen, frontier, out = {id}, [id], []
        for _ in range(max(depth, 0)):
            nxt = sorted({m for f in frontier for m in self._adj.get(f, ())} - seen)
            seen.update(nxt)
            out.extend(nxt)
            frontier = nxt
        return [self.node(i) for i in out]

    def forget(self, id: str) -> bool:
        """Archive a node (never erased): excluded from relevant(), still in to_json()."""
        n = self._nodes.get(id)
        if n is None or n["archived"]:
            return False
        n["archived"] = True
        n["updated"] = _now()
        return True

    # --- capture ---

    def _rel(self, path: str) -> str | None:
        """Workspace-relative posix path, or None if it escapes or is harness state."""
        try:
            p = self.ws.resolve(path)
        except (SandboxError, OSError, ValueError):
            return None
        if p == self.ws.root:
            return None
        rel = p.relative_to(self.ws.root).as_posix()
        return None if rel.split("/", 1)[0] == ".sonic" else rel

    def _existing_file(self, token: str) -> str | None:
        """Relative path if `token` names a workspace file on disk or a known file node."""
        token = token.strip("\"'`()[]{}<>,;:!?").rstrip(".")
        if not token or len(token) > 255 or token.startswith("-"):
            return None
        rel = self._rel(token)
        if rel is None:
            return None
        if f"file:{rel}" in self._nodes or (self.ws.root / rel).is_file():
            return rel
        return None

    def start_task(self, instruction: str) -> str:
        """Create a new task node (key = running counter) for one instruction; returns its id."""
        self.task_counter += 1
        while f"task:{self.task_counter}" in self._nodes:
            self.task_counter += 1
        return self.add_node("task", str(self.task_counter), label=_trim(instruction, 120),
                             summary="0 ok", instruction=instruction, ok=0, failed=0)

    def record_step(self, task_id: str, step: Step) -> None:
        """Capture a finished step. Successful fs steps: task -read/wrote/edited-> file.
        run_shell: task -ran-> command, command -uses-> any workspace file named in it.
        Successful write/edit of a .py file: parse it (ast) -> file -defines-> symbol for
        top-level functions/classes, and file -imports-> file for imports resolving to workspace files.
        """
        task = self._nodes.get(task_id)
        if task is not None:
            d = task["data"]
            d["ok" if step.ok else "failed"] = d.get("ok" if step.ok else "failed", 0) + 1
            tally = f"{d.get('ok', 0)} ok" + (f", {d['failed']} failed" if d.get("failed") else "")
            task["summary"] = tally
            task["updated"] = _now()
            self._reindex(task_id)
        tool, args = step.action.tool, step.action.args
        if not isinstance(args, dict):
            args = {}
        if tool in _FS_RELS and step.ok:
            path = args.get("path")
            rel = self._rel(path) if isinstance(path, str) and path else None
            if rel is None:
                return
            fid = self.add_node("file", rel)
            self.add_edge(task_id, fid, _FS_RELS[tool])
            if tool != "read_file" and rel.endswith(".py"):
                self._index_python(rel)
        elif tool == "run_shell":
            command = str(args.get("command", "")).strip()
            if not command:
                return
            cid = self.add_node("command", command, label=_trim(command, 80), last_ok=step.ok)
            self.add_edge(task_id, cid, "ran")
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = command.split()
            for tok in tokens:
                for part in {tok, tok.split("=", 1)[-1]}:
                    if rel := self._existing_file(part):
                        self.add_edge(cid, self.add_node("file", rel), "uses")

    def _index_python(self, rel: str) -> None:
        path = self.ws.root / rel
        try:
            if path.stat().st_size > MAX_PARSE_BYTES:
                return
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError, RecursionError, MemoryError):
            return
        fid = f"file:{rel}"
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                sid = self.add_node("symbol", f"{rel}::{node.name}", label=node.name,
                                    kind=kind, line=node.lineno)
                self.add_edge(fid, sid, "defines")
        self._set_pending(fid, [])
        pending = []
        for group in self._imports(tree, rel):
            target = next((c for c in group if (self.ws.root / c).is_file()), None)
            if target:
                self.add_edge(fid, self.add_node("file", target), "imports")
            else:
                pending.append(group)
        self._set_pending(fid, pending)

    def _set_pending(self, fid: str, groups: list[list[str]]) -> None:
        """Replace a file's unresolved imports (candidate-path groups) and their index entries."""
        for group in self._nodes[fid]["data"].get("pending_imports", []):
            for c in group:
                self._pending.get(c, set()).discard(fid)
        if groups:
            self._nodes[fid]["data"]["pending_imports"] = groups
        else:
            self._nodes[fid]["data"].pop("pending_imports", None)
        for group in groups:
            for c in group:
                self._pending.setdefault(c, set()).add(fid)

    def _resolve_pending(self, fid: str, rel: str) -> None:
        """Link files whose imports were waiting on `rel`."""
        for importer in sorted(self._pending.get(rel, ())):
            groups = self._nodes[importer]["data"].get("pending_imports", [])
            self._set_pending(importer, [g for g in groups if rel not in g])
            self.add_edge(importer, fid, "imports")

    def _imports(self, tree: ast.Module, rel: str) -> list[list[str]]:
        """Candidate-path groups for `rel`'s imports; any existing member resolves a group."""
        here = Path(rel).parent
        groups: list[list[str]] = []

        def candidates(bases: list[Path], parts: list[str]) -> list[str]:
            out: list[str] = []
            for base in bases:
                stem = base.joinpath(*parts) if parts else base
                for cand in ([stem.with_suffix(".py")] if parts else []) + [stem / "__init__.py"]:
                    r = cand.as_posix()
                    if r != rel and r not in out and self._rel(r) == r:
                        out.append(r)
            return out

        for node in ast.walk(tree):
            specs: list[tuple[list[Path], list[str]]] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    specs.append(([Path("."), here], alias.name.split(".")))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module.split(".") if node.module else []
                if node.level:
                    base = here
                    for _ in range(node.level - 1):
                        base = base.parent
                    bases = [base]
                else:
                    bases = [Path("."), here]
                if mod or node.level:
                    specs.append((bases, mod))
                for alias in node.names:
                    if alias.name != "*":
                        specs.append((bases, mod + [alias.name]))
            for bases, parts in specs:
                if (group := candidates(bases, parts)) and group not in groups:
                    groups.append(group)
        return groups

    def remember(self, text: str) -> str:
        """Store a user note; link note -mentions-> file for workspace paths in the text
        and note -tagged-> topic for #hashtags. Quoted paths may contain spaces.
        Returns the note id; raises ValueError for empty/blank text."""
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            raise ValueError("nothing to remember: empty note")
        tags = list(dict.fromkeys(t.rstrip("-").lower() for t in _HASHTAG.findall(text)))
        tags = [t for t in tags if t]
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        nid = self.add_node("note", key, label=_trim(text, 60), summary=text, tags=tags)
        self._nodes[nid]["archived"] = False
        quoted = [next(g for g in m if g) for m in _QUOTED.findall(text)]
        candidates = [tok for tok in dict.fromkeys(quoted + _PATHISH.findall(text))
                      if "." in tok or "/" in tok or f"file:{tok}" in self._nodes]
        for tok in candidates[:_MAX_LINK_CANDIDATES]:
            if rel := self._existing_file(tok):
                self.add_edge(nid, self.add_node("file", rel), "mentions")
        for tag in tags:
            self.add_edge(nid, self.add_node("topic", tag, label=f"#{tag}"), "tagged")
        return nid

    # --- retrieval ---

    def relevant(self, query: str, k: int = 8) -> list[dict]:
        """Rank non-archived nodes: keyword overlap with label/key/summary/tags, then spread
        a share of each hit's score to its neighbours, plus small recency and hits bonuses."""
        base: dict[str, float] = {}
        for t in _terms(query):
            for id in self._index.get(t, ()):
                n = self._nodes[id]
                if not n["archived"]:
                    boost = _TYPE_BOOST.get(n["type"], 1.0)
                    base[id] = base.get(id, 0.0) + self._node_terms[id][t] * boost
        spread: dict[str, float] = {}
        for id, s in base.items():
            for m in self._adj.get(id, ()):
                if not self._nodes[m]["archived"]:
                    spread[m] = max(spread.get(m, 0.0), _SPREAD * s)
        scores = {id: base.get(id, 0.0) + spread.get(id, 0.0) for id in base.keys() | spread.keys()}
        now = datetime.now(timezone.utc)
        ranked = []
        for id, s in scores.items():
            n = self._nodes[id]
            try:
                age = (now - datetime.fromisoformat(n["updated"])).total_seconds() / 86400
            except (TypeError, ValueError):
                age = float("inf")
            s += _RECENCY_MAX * math.exp(-max(age, 0.0) / _RECENCY_DAYS)
            s += math.log1p(max(n.get("hits", 0), 0)) * 0.1
            ranked.append((-round(s, 6), id))
        ranked.sort()
        return [dict(self.node(id), score=-neg) for neg, id in ranked[:max(k, 0)]]

    def _edge_hint(self, id: str, limit: int = 4) -> str:
        """Short "(defines id, ...; mentioned in id)" summary of a node's strongest links."""
        links = []
        for src, dst, rel in self._incident.get(id, ()):
            other = dst if src == id else src
            if not self._nodes[other]["archived"]:
                label = rel if src == id else _INVERSE.get(rel, f"{rel} (from)")
                links.append((-self._edges[(src, dst, rel)]["weight"], label, other))
        links.sort()
        groups: dict[str, list[str]] = {}
        for _, label, m in links[:limit]:
            groups.setdefault(label, []).append(_trim(m, 48))
        if not groups:
            return ""
        return "  (" + "; ".join(f"{r} {', '.join(ms)}" for r, ms in groups.items()) + ")"

    def render_context(self, query: str, budget_chars: int = 2000) -> str:
        """Plain-text block of relevant nodes (and their key edges) for a model prompt, within budget."""
        lines: list[str] = []
        used = 0
        for n in self.relevant(query, k=max(8, budget_chars // 40)):
            text = n["summary"] if n["type"] == "note" and n["summary"] else n["label"]
            if n["type"] not in ("note", "topic") and n["summary"] and n["summary"] != text:
                text = f"{text}: {n['summary']}"
            line = f"- [{n['type']}] {_trim(text, 200)}{self._edge_hint(n['id'])}"
            cost = len(line) + (1 if lines else 0)
            if used + cost > budget_chars:
                if not lines and budget_chars > 0:
                    lines.append(line[:budget_chars])
                break
            lines.append(line)
            used += cost
        return "\n".join(lines)
