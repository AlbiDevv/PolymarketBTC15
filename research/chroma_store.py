from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


class MotifStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)
        try:
            import chromadb  # type: ignore

            self._client = chromadb.PersistentClient(path=str(self._path))
        except Exception:
            self._client = None

    def upsert(self, collection_name: str, rows: Iterable[dict]):
        items = list(rows)
        if not items:
            return
        if self._client is not None:
            collection = self._client.get_or_create_collection(collection_name)
            collection.upsert(
                ids=[str(item["id"]) for item in items],
                documents=[json.dumps(item, ensure_ascii=False, sort_keys=True) for item in items],
                metadatas=[{k: v for k, v in item.items() if isinstance(v, (str, int, float, bool))} for item in items],
            )
            return

        out_path = self._path / f"{collection_name}.jsonl"
        with open(out_path, "a", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
