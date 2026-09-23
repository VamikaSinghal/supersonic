"""Context graph: the harness's second brain. [Owner: context agent]

A typed, persistent graph of what you and the harness have touched and said.
Stored locally at <ws.root>/.sonic/context/graph.json (nothing leaves the machine).

Node: {"id": f"{type}:{key}", "type", "key", "label", "summary", "tags": [..],
       "created", "updated" (ISO-8601), "hits": int, "archived": bool, "data": {..}}
  types: task, file, symbol, command, note, topic
Edge: {"src", "dst", "rel", "weight": int}   (deduped on (src, dst, rel); repeats bump weight)
  rels: read, wrote, edited, ran, uses, defines, imports, mentions, tagged
"""
from sonic.planner import Step
from sonic.workspace import Workspace

GRAPH_PATH = ".sonic/context/graph.json"


class ContextGraph:
    def __init__(self, ws: Workspace):
        """Load GRAPH_PATH if it exists, else start empty."""
        raise NotImplementedError

    def add_node(self, type: str, key: str, label: str | None = None, summary: str = "",
                 tags: list[str] | None = None, **data) -> str:
        """Upsert; returns the id. Re-adding bumps hits/updated and merges summary/tags/data."""
        raise NotImplementedError

    def add_edge(self, src: str, dst: str, rel: str) -> None:
        raise NotImplementedError

    def node(self, id: str) -> dict | None:
        raise NotImplementedError

    def neighbors(self, id: str, depth: int = 1) -> list[dict]:
        """Nodes within `depth` hops (either direction), excluding `id` itself."""
        raise NotImplementedError

    def start_task(self, instruction: str) -> str:
        """Create a new task node (key = running counter) for one instruction; returns its id."""
        raise NotImplementedError

    def record_step(self, task_id: str, step: Step) -> None:
        """Capture a finished step. Successful fs steps: task -read/wrote/edited-> file.
        run_shell: task -ran-> command, command -uses-> any workspace file named in it.
        Successful write/edit of a .py file: parse it (ast) -> file -defines-> symbol for
        top-level functions/classes, and file -imports-> file for imports resolving to workspace files.
        """
        raise NotImplementedError

    def remember(self, text: str) -> str:
        """Store a user note; link note -mentions-> file for workspace paths in the text
        and note -tagged-> topic for #hashtags. Returns the note id."""
        raise NotImplementedError

    def forget(self, id: str) -> bool:
        """Archive a node (never erased): excluded from relevant(), still in to_json()."""
        raise NotImplementedError

    def relevant(self, query: str, k: int = 8) -> list[dict]:
        """Rank non-archived nodes: keyword overlap with label/key/summary/tags, then spread
        a share of each hit's score to its neighbours, plus small recency and hits bonuses."""
        raise NotImplementedError

    def render_context(self, query: str, budget_chars: int = 2000) -> str:
        """Plain-text block of relevant nodes (and their key edges) for a model prompt, within budget."""
        raise NotImplementedError

    def to_json(self) -> dict:
        """{"nodes": [...], "edges": [...]} for the UI."""
        raise NotImplementedError

    def save(self) -> None:
        """Atomic write (temp file in the same dir + os.replace)."""
        raise NotImplementedError
