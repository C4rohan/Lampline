"""The engine's own data: records agents look up and save, and knowledge they search.

Both live in one SQLite file in the engine's data folder (VPE_DATA_DIR, default
~/.local/share/vpengine), never in an agent folder, so they stay out of git.

Records are JSON documents in named collections, a small document store:

    collections   name, description, key (a field that identifies a record, for "save" to update)
    records       collection, id, data (JSON), created, updated

Knowledge bases hold documents split into passages. Search ranks passages by
keywords (SQLite FTS5, BM25) and, when an embedding model is available, by
meaning too, and merges the two (reciprocal rank fusion).
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import numpy as np

from .embed import EmbedError, Embedder

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
MAX_RECORD = 64 * 1024
MAX_FILE = 20 * 1024 * 1024
MAX_TEXT = 2_000_000
CHUNK, CHUNK_MAX = 700, 1100
KINDS = (".md", ".markdown", ".txt", ".html", ".htm", ".pdf", ".docx", ".csv", ".json")
_STOP = set("a an and are as at be by can do does for from how i if in is it me my of on or our please the to was "
            "we what when where which who why will with you your".split())


class LibraryError(ValueError):
    pass


def default_dir() -> Path:
    custom = os.environ.get("VPE_DATA_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    base = os.environ.get("XDG_DATA_HOME", "").strip() or str(Path.home() / ".local" / "share")
    return Path(base) / "vpengine"


def check_name(name: str, what: str) -> str:
    name = str(name or "").strip()
    if not NAME.match(name):
        raise LibraryError(f"a {what} name is lowercase letters, digits, - and _, starting with a letter (got {name!r})")
    return name


def _norm(v: Any) -> str:
    """How values compare in a lookup: case, spacing and punctuation don't matter ("A-12 7" finds "a127")."""
    return re.sub(r"[\W_]+", "", str(v)).casefold()


def _field_name(header: Any) -> str:
    """A CSV header as a field name: "Member ID" -> member_id."""
    name = re.sub(r"\W+", "_", str(header).strip()).strip("_").lower() or "field"
    return name if name[0].isalpha() or name[0] == "_" else f"f_{name}"


def _dig(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


class Library:
    def __init__(self, folder: Path | None = None, embedder: Embedder | None = None):
        self.folder = Path(folder) if folder else default_dir()
        self.embedder = embedder or Embedder("off")
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ storage
    @property
    def path(self) -> Path:
        return self.folder / "library.sqlite3"

    def db(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is None:
                self.folder.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(self.folder, 0o700)
                except OSError:
                    pass
                db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA foreign_keys=ON")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS collections (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
                        key TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS records (collection TEXT NOT NULL REFERENCES collections(name) ON DELETE CASCADE,
                        id TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                        PRIMARY KEY (collection, id));
                    CREATE TABLE IF NOT EXISTS bases (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, base TEXT NOT NULL REFERENCES bases(name) ON DELETE CASCADE,
                        title TEXT NOT NULL, chars INTEGER NOT NULL, added REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, base TEXT NOT NULL,
                        doc INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, idx INTEGER NOT NULL,
                        text TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', vec BLOB);
                    CREATE INDEX IF NOT EXISTS chunks_base ON chunks(base);
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id',
                        tokenize='porter unicode61');
                    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                        INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text); END;
                    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                        INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
                """)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                self._db = db
            return self._db

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db().execute(sql, args).fetchall()

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ------------------------------------------------------------------ collections and records
    def collections(self) -> list[dict]:
        rows = self._q("SELECT c.name, c.description, c.key, c.created, COUNT(r.id) AS n FROM collections c "
                       "LEFT JOIN records r ON r.collection = c.name GROUP BY c.name ORDER BY c.name")
        out = []
        for r in rows:
            fields: dict[str, int] = {}
            for (data,) in self._q("SELECT data FROM records WHERE collection = ? ORDER BY updated DESC LIMIT 50", (r["name"],)):
                for k in json.loads(data):
                    fields[k] = fields.get(k, 0) + 1
            out.append({"name": r["name"], "description": r["description"], "key": r["key"], "count": r["n"],
                        "fields": sorted(fields, key=lambda k: -fields[k])[:30]})
        return out

    def collection(self, name: str) -> dict:
        rows = self._q("SELECT name, description, key FROM collections WHERE name = ?", (name,))
        if not rows:
            raise LibraryError(f"there's no collection named {name}")
        return dict(rows[0])

    def create_collection(self, name: str, description: str = "", key: str = "") -> dict:
        name = check_name(name, "collection")
        if key and not FIELD.match(key):
            raise LibraryError("the key must be a field name like member_id")
        with self._lock:
            if self._q("SELECT 1 FROM collections WHERE name = ?", (name,)):
                self._q("UPDATE collections SET description = ?, key = ? WHERE name = ?", (description[:500], key, name))
            else:
                self._q("INSERT INTO collections (name, description, key, created) VALUES (?, ?, ?, ?)",
                        (name, description[:500], key, time.time()))
        return self.collection(name)

    def delete_collection(self, name: str) -> None:
        self.collection(name)
        self._q("DELETE FROM collections WHERE name = ?", (name,))

    def _check(self, data: Any) -> str:
        if not isinstance(data, dict):
            raise LibraryError("a record is a JSON object")
        for k in data:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k)):
                raise LibraryError(f"field names are letters, digits and _ (got {k!r})")
        text = json.dumps(data, ensure_ascii=False, default=str)
        if len(text.encode()) > MAX_RECORD:
            raise LibraryError("a record can be at most 64 KB")
        return text

    def records(self, collection: str, *, limit: int = 50, offset: int = 0, search: str = "") -> dict:
        self.collection(collection)
        where, args = "collection = ?", [collection]
        if search.strip():
            where += " AND (id LIKE ? OR data LIKE ?)"
            args += [f"%{search.strip()}%"] * 2
        total = self._q(f"SELECT COUNT(*) FROM records WHERE {where}", tuple(args))[0][0]
        rows = self._q(f"SELECT id, data, updated FROM records WHERE {where} ORDER BY updated DESC LIMIT ? OFFSET ?",
                       (*args, max(1, min(limit, 500)), max(0, offset)))
        return {"total": total, "records": [{"id": r["id"], "data": json.loads(r["data"]), "updated": r["updated"]} for r in rows]}

    def get(self, collection: str, rid: str) -> dict | None:
        rows = self._q("SELECT data FROM records WHERE collection = ? AND id = ?", (collection, str(rid)))
        return {"id": str(rid), **json.loads(rows[0]["data"])} if rows else None

    def put(self, collection: str, data: dict, rid: str | None = None, *, merge: bool = False, by_key: bool = True) -> dict:
        """Add a record, or replace (merge=True: update) the one with this id, or with the same key value."""
        col = self.collection(collection)
        data = {k: v for k, v in data.items() if k != "id"}
        with self._lock:
            if rid is None and by_key and col["key"] and _dig(data, col["key"]) not in (None, ""):
                want = _norm(_dig(data, col["key"]))
                rid = next((r["id"] for r in self._q("SELECT id, data FROM records WHERE collection = ?", (collection,))
                            if _norm(_dig(json.loads(r["data"]), col["key"])) == want), None)
            now = time.time()
            old = self.get(collection, rid) if rid else None
            if old is not None:
                old.pop("id")
                new = {**old, **data} if merge else data
                self._q("UPDATE records SET data = ?, updated = ? WHERE collection = ? AND id = ?",
                        (self._check(new), now, collection, str(rid)))
            else:
                new = data
                rid = str(rid or uuid.uuid4().hex[:12])
                if not re.match(r"^[\w.-]{1,80}$", rid):
                    raise LibraryError("a record id is up to 80 letters, digits, ., - and _")
                self._q("INSERT INTO records (collection, id, data, created, updated) VALUES (?, ?, ?, ?, ?)",
                        (collection, rid, self._check(new), now, now))
        return {"id": rid, **new}

    def delete(self, collection: str, rid: str) -> bool:
        with self._lock:
            n = self.db().execute("DELETE FROM records WHERE collection = ? AND id = ?", (collection, str(rid))).rowcount
        return n > 0

    def find(self, collection: str, where: dict[str, Any], *, limit: int = 5) -> list[dict]:
        """Records whose fields equal these values, ignoring case, spaces and punctuation."""
        self.collection(collection)
        for f in where:
            if not FIELD.match(str(f)) and f != "id":
                raise LibraryError(f"can't look up by {f!r}")
        want = {f: _norm(v) for f, v in where.items()}
        if any(v == "" for v in want.values()):
            return []
        out = []
        for r in self._q("SELECT id, data FROM records WHERE collection = ? ORDER BY updated DESC", (collection,)):
            data = {"id": r["id"], **json.loads(r["data"])}
            if all(_norm(_dig(data, f)) == v for f, v in want.items()):
                out.append(data)
                if len(out) >= limit:
                    break
        return out

    def import_rows(self, collection: str, raw: bytes, filename: str) -> dict:
        """CSV (a header row) or JSON (a list of objects) into a collection; returns counts."""
        text = raw.decode("utf-8-sig", errors="replace")
        if filename.lower().endswith(".json"):
            try:
                rows = json.loads(text)
            except ValueError as exc:
                raise LibraryError(f"that isn't valid JSON: {exc}") from exc
            if isinstance(rows, dict):
                rows = rows.get("records") or rows.get("items") or [rows]
        else:
            rows = list(csv.DictReader(io.StringIO(text)))
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise LibraryError("expected a list of records")
        if len(rows) > 50_000:
            raise LibraryError("at most 50,000 records at a time")
        key = self.collection(collection)["key"]
        index: dict[str, str] = {}                   # key value -> record id, read once (not once per row)
        if key:
            for r in self._q("SELECT id, data FROM records WHERE collection = ?", (collection,)):
                index.setdefault(_norm(_dig(json.loads(r["data"]), key)), r["id"])
        added = 0
        with self._lock:
            self._q("BEGIN")
            try:
                for r in rows:
                    clean = {_field_name(k): v for k, v in r.items() if k is not None}
                    rid = clean.pop("id", None)
                    rid = str(rid) if rid not in (None, "") else None
                    k = _norm(_dig(clean, key)) if key and _dig(clean, key) not in (None, "") else ""
                    if rid is None and k:
                        rid = index.get(k)
                    rec = self.put(collection, clean, rid, by_key=False)
                    if k:
                        index[k] = rec["id"]
                    added += 1
                self._q("COMMIT")
            except Exception:
                self._q("ROLLBACK")
                raise
        return {"imported": added}

    # ------------------------------------------------------------------ knowledge
    def bases(self) -> list[dict]:
        rows = self._q("SELECT b.name, b.description, (SELECT COUNT(*) FROM documents d WHERE d.base = b.name) AS docs, "
                       "(SELECT COUNT(*) FROM chunks c WHERE c.base = b.name) AS passages, "
                       "(SELECT COUNT(*) FROM chunks c WHERE c.base = b.name AND c.model = ? AND c.vec IS NOT NULL) AS embedded "
                       "FROM bases b ORDER BY b.name", (self.embedder.name,))
        return [dict(r) for r in rows]

    def base(self, name: str) -> dict:
        rows = self._q("SELECT name, description FROM bases WHERE name = ?", (name,))
        if not rows:
            raise LibraryError(f"there's no knowledge base named {name}")
        out = dict(rows[0])
        out["documents"] = [dict(r) for r in self._q(
            "SELECT d.id, d.title, d.chars, d.added, COUNT(c.id) AS passages FROM documents d LEFT JOIN chunks c ON c.doc = d.id "
            "WHERE d.base = ? GROUP BY d.id ORDER BY d.added DESC", (name,))]
        return out

    def create_base(self, name: str, description: str = "") -> dict:
        name = check_name(name, "knowledge base")
        with self._lock:
            if self._q("SELECT 1 FROM bases WHERE name = ?", (name,)):
                self._q("UPDATE bases SET description = ? WHERE name = ?", (description[:500], name))
            else:
                self._q("INSERT INTO bases (name, description, created) VALUES (?, ?, ?)", (name, description[:500], time.time()))
        return self.base(name)

    def delete_base(self, name: str) -> None:
        self.base(name)
        with self._lock:
            self._q("DELETE FROM chunks WHERE base = ?", (name,))
            self._q("DELETE FROM bases WHERE name = ?", (name,))

    def delete_document(self, base: str, doc: int) -> bool:
        with self._lock:
            self._q("DELETE FROM chunks WHERE doc = ? AND base = ?", (int(doc), base))
            return self.db().execute("DELETE FROM documents WHERE id = ? AND base = ?", (int(doc), base)).rowcount > 0

    async def add_document(self, base: str, filename: str, raw: bytes) -> dict:
        self.base(base)
        if len(raw) > MAX_FILE:
            raise LibraryError("a file can be at most 20 MB")
        title = Path(filename).name[:200] or "document"
        text = await asyncio.to_thread(extract_text, title, raw)      # a big PDF mustn't stall live calls
        if not text.strip():
            raise LibraryError(f"no text found in {title}")
        passages = chunk(text)
        with self._lock:
            cur = self.db().execute("INSERT INTO documents (base, title, chars, added) VALUES (?, ?, ?, ?)",
                                    (base, title, len(text), time.time()))
            doc = cur.lastrowid
            for i, p in enumerate(passages):
                self.db().execute("INSERT INTO chunks (base, doc, idx, text) VALUES (?, ?, ?, ?)", (base, doc, i, p))
        embedded = await self.embed_missing(base)
        return {"id": doc, "title": title, "passages": len(passages), "embedded": embedded}

    async def embed_missing(self, base: str) -> int:
        """Vectors for passages that don't have one from the current model. 0 if embeddings aren't available."""
        if not self.embedder.ready:
            return 0
        rows = self._q("SELECT id, text FROM chunks WHERE base = ? AND model != ?", (base, self.embedder.name))
        done = 0
        for i in range(0, len(rows), 64):
            part = rows[i:i + 64]
            try:
                vecs = await self.embedder.embed([r["text"] for r in part])
            except EmbedError:
                return done
            with self._lock:
                for r, v in zip(part, vecs):
                    self.db().execute("UPDATE chunks SET model = ?, vec = ? WHERE id = ?", (self.embedder.name, v.tobytes(), r["id"]))
            done += len(part)
        return done

    async def search(self, base: str, query: str, limit: int = 3) -> list[dict]:
        self.base(base)
        limit = max(1, min(int(limit or 3), 10))
        words = [w for w in re.findall(r"\w+", query.lower()) if w not in _STOP][:24]
        ranked: dict[int, float] = {}
        keyword: set[int] = set()
        if words:
            fts = " OR ".join(f'"{w}"' for w in words)
            rows = self._q("SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid WHERE chunks_fts MATCH ? "
                           "AND c.base = ? ORDER BY bm25(chunks_fts) LIMIT 20", (fts, base))
            for rank, r in enumerate(rows):
                ranked[r["id"]] = ranked.get(r["id"], 0) + 1 / (60 + rank)
                keyword.add(r["id"])
        meaning: dict[int, float] = {}
        if self.embedder.ready and query.strip():
            rows = self._q("SELECT id, vec FROM chunks WHERE base = ? AND model = ?", (base, self.embedder.name))
            if rows:
                try:
                    q = (await self.embedder.embed([query]))[0]
                except EmbedError:
                    q = None
                if q is not None:
                    mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
                    scores = mat @ q
                    for rank, i in enumerate(np.argsort(-scores)[:20]):
                        if scores[i] < 0.3 and rows[i]["id"] not in keyword:
                            break                         # not close enough in meaning, and no shared words
                        meaning[rows[i]["id"]] = float(scores[i])
                        ranked[rows[i]["id"]] = ranked.get(rows[i]["id"], 0) + 1 / (60 + rank)
        best = sorted(ranked, key=lambda k: -ranked[k])[:limit]
        if not best:
            return []
        marks = ",".join("?" * len(best))
        info = {r["id"]: r for r in self._q(f"SELECT c.id, c.text, d.title FROM chunks c JOIN documents d ON d.id = c.doc "
                                            f"WHERE c.id IN ({marks})", tuple(best))}
        return [{"text": info[k]["text"], "source": info[k]["title"], "score": round(ranked[k] * 1000, 2),
                 **({"similarity": round(meaning[k], 3)} if k in meaning else {})} for k in best if k in info]


# ---------------------------------------------------------------------- reading files

class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n\n" if tag.startswith("h") or tag == "p" else "\n")
            if tag.startswith("h") and len(tag) == 2:
                self.out.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n\n" if tag.startswith("h") or tag == "p" else "\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def extract_text(filename: str, raw: bytes) -> str:
    """Plain text from a document. Nothing in it is run: no macros, no scripts, no links followed."""
    ext = Path(filename.lower()).suffix
    if ext not in KINDS:
        raise LibraryError(f"can't read {ext or 'that'} files; use {', '.join(KINDS)}")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise LibraryError("reading PDFs needs pypdf (pip install pypdf)") from exc
        try:
            reader = PdfReader(io.BytesIO(raw))
            text = "\n\n".join((p.extract_text() or "") for p in reader.pages[:2000])
        except Exception as exc:  # noqa: BLE001 - a broken PDF is the user's to fix
            raise LibraryError(f"couldn't read that PDF: {exc}") from exc
    elif ext == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                info = z.getinfo("word/document.xml")
                if info.file_size > 50 * 1024 * 1024:
                    raise LibraryError("that Word file is too large once unpacked")
                xml = z.read(info).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, KeyError) as exc:
            raise LibraryError("that isn't a Word (.docx) file") from exc
        paras = []
        for p in re.split(r"</w:p>", xml):
            words = "".join(html.unescape(t) for t in re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", p))
            style = re.search(r'<w:pStyle w:val="Heading(\d)"', p)
            if words.strip():
                paras.append(("#" * int(style.group(1)) + " " if style else "") + words)
        text = "\n\n".join(paras)
    else:
        text = raw.decode("utf-8-sig", errors="replace")
        if ext in (".html", ".htm"):
            parser = _Text()
            parser.feed(text)
            text = re.sub(r"[ \t]+", " ", "".join(parser.out))
        elif ext == ".csv":
            rows = list(csv.DictReader(io.StringIO(text)))
            text = "\n\n".join("; ".join(f"{k}: {v}" for k, v in r.items() if k and v not in (None, "")) for r in rows)
        elif ext == ".json":
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise LibraryError(f"that isn't valid JSON: {exc}") from exc
            items = data if isinstance(data, list) else [data]
            text = "\n\n".join("; ".join(f"{k}: {v}" for k, v in x.items()) if isinstance(x, dict) else str(x) for x in items)
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    return text[:MAX_TEXT]


def chunk(text: str) -> list[str]:
    """Passages of about CHUNK characters along paragraph and sentence lines, each led by its section heading."""
    out: list[str] = []
    heading, buf = "", ""

    def flush():
        nonlocal buf
        if buf.strip():
            out.append((heading + "\n" if heading and not buf.startswith(heading) else "") + buf.strip())
        buf = ""

    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", para)
        if m and "\n" not in para:
            flush()
            heading = m.group(2).strip()[:120]
            continue
        pieces = [para] if len(para) <= CHUNK_MAX else re.split(r"(?<=[.!?])\s+", para)
        for piece in pieces:
            while len(piece) > CHUNK_MAX:                 # one enormous sentence
                cut = piece.rfind(" ", 0, CHUNK_MAX)
                cut = cut if cut > CHUNK // 2 else CHUNK_MAX
                if buf:
                    flush()
                buf = piece[:cut]
                flush()
                piece = piece[cut:].strip()
            if buf and len(buf) + len(piece) + 2 > CHUNK:
                flush()
            buf = f"{buf}\n\n{piece}" if buf else piece
    flush()
    return out
