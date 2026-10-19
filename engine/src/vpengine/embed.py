"""Text embeddings for knowledge search, on your computer or from an Ollama-style server.

    local    all-MiniLM-L6-v2 (Apache-2.0, 384 numbers per passage) run with onnxruntime,
             which the engine already has for Kokoro. One 90 MB download, then offline.
    ollama   any OpenAI-compatible /embeddings endpoint (Ollama's nomic-embed-text, say).
    off      keyword search only.

Keyword search (SQLite FTS5, BM25) works without any of these; embeddings add
"same meaning, different words" matches on top.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path

import httpx
import numpy as np

from .builtin_speech import CACHE

MINILM = "all-MiniLM-L6-v2"
_REV = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"        # pinned so the vectors don't change under you
MINILM_FILES = {
    "model.onnx": f"https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/{_REV}/onnx/model.onnx",
    "tokenizer.json": f"https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/{_REV}/tokenizer.json",
}
MINILM_SHA256 = {
    "model.onnx": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
}
MINILM_MB = 90


class EmbedError(RuntimeError):
    pass


def minilm_dir() -> Path:
    return CACHE / "minilm"


def minilm_ready() -> bool:
    return all((minilm_dir() / n).is_file() for n in MINILM_FILES)


_lock = threading.Lock()
_model: tuple | None = None


def _load():
    global _model
    with _lock:
        if _model is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            tok = Tokenizer.from_file(str(minilm_dir() / "tokenizer.json"))
            tok.enable_truncation(256)
            tok.enable_padding()
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
            sess = ort.InferenceSession(str(minilm_dir() / "model.onnx"), opts, providers=["CPUExecutionProvider"])
            _model = (tok, sess)
        return _model


def _minilm(texts: list[str]) -> np.ndarray:
    tok, sess = _load()
    out = []
    for i in range(0, len(texts), 32):
        enc = tok.encode_batch(texts[i:i + 32])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        hidden = sess.run(None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)})[0]
        out.append((hidden * mask[..., None]).sum(1) / np.maximum(mask.sum(1, keepdims=True), 1))
    return np.vstack(out).astype(np.float32)


class Embedder:
    """Turns passages into unit vectors. `name` identifies the model, so vectors from different models never mix."""

    def __init__(self, mode: str = "local", url: str = "", model: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.mode, self.url, self.model, self.transport = mode, url.rstrip("/"), model, transport

    @property
    def name(self) -> str:
        return {"local": MINILM, "ollama": f"ollama:{self.model}"}.get(self.mode, "")

    @property
    def ready(self) -> bool:
        if self.mode == "local":
            return minilm_ready()
        return self.mode == "ollama" and bool(self.url and self.model)

    async def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        if self.mode == "local":
            if not minilm_ready():
                raise EmbedError("the local embedding model isn't downloaded yet (Knowledge → Download)")
            vecs = await asyncio.to_thread(_minilm, texts)
        elif self.mode == "ollama":
            vecs = await self._remote(texts)
        else:
            raise EmbedError("embeddings are off")
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return (vecs / np.maximum(norms, 1e-9)).astype(np.float32)

    async def _remote(self, texts: list[str]) -> np.ndarray:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60, transport=self.transport) as http:
            for i in range(0, len(texts), 32):
                try:
                    r = await http.post(f"{self.url}/embeddings", json={"model": self.model, "input": texts[i:i + 32]})
                    r.raise_for_status()
                    data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    raise EmbedError(f"the embeddings server at {self.url} didn't answer: {exc}") from exc
                out += [d["embedding"] for d in data]
        return np.array(out, dtype=np.float32)


class Download:
    """Fetches the local model once, with progress for the Knowledge page."""

    def __init__(self):
        self.state, self.message, self.done_bytes, self.total_bytes = "idle", "", 0, 0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.state, self.message, self.done_bytes, self.total_bytes = "downloading", "Starting the download…", 0, MINILM_MB << 20
        self._task = asyncio.create_task(self._run())

    def status(self) -> dict:
        return {"state": self.state, "message": self.message, "done_bytes": self.done_bytes,
                "total_bytes": self.total_bytes, "ready": minilm_ready(), "model": MINILM, "size_mb": MINILM_MB}

    async def _run(self) -> None:
        try:
            minilm_dir().mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=10.0)) as http:
                for name, url in MINILM_FILES.items():
                    dest = minilm_dir() / name
                    if dest.is_file():
                        continue
                    self.message = f"Downloading {name}…"
                    part = dest.with_suffix(dest.suffix + ".part")
                    digest = hashlib.sha256()
                    async with http.stream("GET", url) as r:
                        r.raise_for_status()
                        with open(part, "wb") as f:
                            async for chunk in r.aiter_bytes(1 << 16):
                                f.write(chunk)
                                digest.update(chunk)
                                self.done_bytes += len(chunk)
                    if digest.hexdigest() != MINILM_SHA256[name]:
                        part.unlink(missing_ok=True)
                        raise EmbedError(f"{name} didn't match its expected checksum; not using it")
                    os.replace(part, dest)
            self.message = "Loading the model…"
            await asyncio.to_thread(_load)
            self.state, self.message = "idle", "Ready"
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            self.state, self.message = "error", f"Download failed: {exc}"
