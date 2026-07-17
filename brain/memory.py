"""
memory.py - local semantic memory for the AI mini bot, with management commands.

Fully local embeddings via fastembed (ONNX, no torch, no API). Saves every
exchange to disk and retrieves by meaning across sessions.

Store lives in ./bot_memory/ :
  memory_store.json  - readable records
  memory_vecs.npy    - embedding matrix (search index)

Install:  pip install fastembed numpy

--- Terminal management (run directly) ---
  python3 memory.py stats            # how many memories
  python3 memory.py list             # list all, with ids + timestamps
  python3 memory.py search "coffee"  # semantic search
  python3 memory.py delete 5         # delete memory id 5
  python3 memory.py clear            # wipe all memory
  python3 memory.py export           # write a readable memory.md to browse
"""

import json
import os
import time

import numpy as np
from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-small-en-v1.5"   # 384-dim, small, fast
DIM = 384


class Memory:
    def __init__(self, folder="bot_memory", model_name=MODEL_NAME):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.json_path = os.path.join(folder, "memory_store.json")
        self.vecs_path = os.path.join(folder, "memory_vecs.npy")
        self.embedder = TextEmbedding(model_name=model_name)
        self.records = []
        self.vecs = np.zeros((0, DIM), dtype=np.float32)
        self._load()
        self.next_id = (max([r["id"] for r in self.records], default=-1) + 1)

    # ---------- embedding ----------
    def _embed(self, text):
        v = np.asarray(list(self.embedder.embed([text]))[0], dtype=np.float32)
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    # ---------- persistence ----------
    def _load(self):
        if os.path.exists(self.json_path):
            with open(self.json_path, encoding="utf-8") as f:
                self.records = json.load(f)
        if os.path.exists(self.vecs_path):
            self.vecs = np.load(self.vecs_path)
        n = min(len(self.records), len(self.vecs))
        self.records, self.vecs = self.records[:n], self.vecs[:n]

    def _save(self):
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(self.records, f, ensure_ascii=False, indent=2)
        np.save(self.vecs_path, self.vecs)

    # ---------- writing ----------
    def add(self, text, meta=None):
        text = (text or "").strip()
        if not text:
            return None
        v = self._embed(text)
        rec = {"id": self.next_id, "ts": time.time(), "text": text, "meta": meta or {}}
        self.next_id += 1
        self.records.append(rec)
        self.vecs = v[None, :] if self.vecs.size == 0 else np.vstack([self.vecs, v[None, :]])
        self._save()
        return rec["id"]

    def add_exchange(self, user_text, bot_text):
        user_text = (user_text or "").strip()
        bot_text = (bot_text or "").strip()
        if not (user_text or bot_text):
            return None
        combined = f"User: {user_text}\nBot: {bot_text}".strip()
        return self.add(combined, meta={"type": "exchange"})

    # ---------- reading ----------
    def search(self, query, k=3, min_score=0.30):
        query = (query or "").strip()
        if not query or len(self.records) == 0:
            return []
        q = self._embed(query)
        scores = self.vecs @ q
        order = np.argsort(-scores)[:k]
        out = []
        for i in order:
            if scores[int(i)] >= min_score:
                r = dict(self.records[int(i)])
                r["score"] = round(float(scores[int(i)]), 3)
                out.append(r)
        return out

    def recall_text(self, query, k=3, min_score=0.30):
        hits = self.search(query, k=k, min_score=min_score)
        if not hits:
            return ""
        lines = [f"- {h['text']} (relevance {h['score']})" for h in hits]
        return "Things you remember that may be relevant:\n" + "\n".join(lines)

    # ---------- management ----------
    def delete(self, target_id):
        for i, r in enumerate(self.records):
            if r["id"] == target_id:
                self.records.pop(i)
                self.vecs = np.delete(self.vecs, i, axis=0)
                self._save()
                return True
        return False

    def clear(self):
        self.records = []
        self.vecs = np.zeros((0, DIM), dtype=np.float32)
        self._save()

    def export_markdown(self, path=None):
        path = path or os.path.join(self.folder, "memory.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Robot memory\n\n")
            for r in self.records:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
                f.write(f"- **[{r['id']}] {ts}** — {r['text'].strip()}\n\n")
        return path

    def stats(self):
        return {"count": len(self.records)}


if __name__ == "__main__":
    import sys
    m = Memory()
    args = sys.argv[1:]
    cmd = args[0] if args else "stats"

    if cmd == "stats":
        print(m.stats())
    elif cmd == "list":
        for r in m.records:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
            print(f"[{r['id']}] {ts}  {r['text'].replace(chr(10), ' | ')[:100]}")
        print(f"\n({m.stats()['count']} total)")
    elif cmd == "search":
        for h in m.search(" ".join(args[1:]), k=5):
            print(h["score"], "-", h["text"].replace(chr(10), " | "))
    elif cmd == "delete":
        print("deleted" if m.delete(int(args[1])) else "id not found")
    elif cmd == "clear":
        m.clear(); print("memory cleared")
    elif cmd == "export":
        print("wrote", m.export_markdown())
    else:
        print("commands: stats | list | search <q> | delete <id> | clear | export")
