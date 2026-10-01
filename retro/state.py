"""Tiny JSON state store kept on the `data` branch alongside generated media."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Item lifecycle:
#   candidate -> (chosen) generating -> pending (awaiting your tap) -> approved -> posted
#                                            \-> skipped / failed      \-> failed (after retries)
#   candidate -> rejected (low score / duplicate)
OPEN_STATUSES = {"generating", "pending", "approved"}


def now_ts() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_ts(), tz=timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path: Path):
        self.path = path
        self.dirty = False
        if path.exists():
            self.d: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.d = {}
        self.d.setdefault("items", {})
        self.d.setdefault("telegram_offset", 0)
        self.d.setdefault("generations", [])   # timestamps of image generations
        self.d.setdefault("posts", [])         # timestamps of published posts
        self.d.setdefault("last_discovery", 0)
        self.d.setdefault("paused", False)
        self.d.setdefault("vault", "")         # encrypted refreshed tokens
        self.d.setdefault("seen", [])          # source image keys already considered

    # --- generic -------------------------------------------------------
    def __getitem__(self, k):
        return self.d[k]

    def set(self, k, v):
        self.d[k] = v
        self.dirty = True

    def save(self) -> bool:
        if not self.dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False
        return True

    # --- items ---------------------------------------------------------
    @property
    def items(self) -> dict[str, dict]:
        return self.d["items"]

    def put(self, item: dict) -> dict:
        item.setdefault("created_at", now_ts())
        item.setdefault("status", "candidate")
        item.setdefault("errors", [])
        item.setdefault("posted", {})
        self.items[item["id"]] = item
        self.dirty = True
        return item

    def update(self, item_id: str, **fields) -> dict:
        it = self.items[item_id]
        it.update(fields)
        it["updated_at"] = now_ts()
        self.dirty = True
        return it

    def by_status(self, *statuses: str) -> list[dict]:
        return sorted((i for i in self.items.values() if i["status"] in statuses),
                      key=lambda i: i.get("created_at", 0))

    def mark_seen(self, key: str):
        if key not in self.d["seen"]:
            self.d["seen"].append(key)
            self.d["seen"] = self.d["seen"][-5000:]
            self.dirty = True

    def is_seen(self, key: str) -> bool:
        return key in self.d["seen"]

    # --- counters ------------------------------------------------------
    def count_since(self, key: str, seconds: float) -> int:
        cut = now_ts() - seconds
        return sum(1 for t in self.d[key] if t >= cut)

    def log(self, key: str):
        self.d[key].append(now_ts())
        self.d[key] = [t for t in self.d[key] if t >= now_ts() - 7 * 86400]
        self.dirty = True

    def recent_subjects(self, n: int) -> list[str]:
        done = [i for i in self.items.values()
                if i["status"] in ("pending", "approved", "posted") and i.get("subject")]
        done.sort(key=lambda i: i.get("generated_at", 0), reverse=True)
        return [i["subject"].lower() for i in done[:n]]

    def recent_generated(self, n: int) -> list[dict]:
        """Newest first: items picked from the sources (not your own photos) that reached the review stage."""
        done = [i for i in self.items.values()
                if i.get("generated_at") and not i.get("priority")
                and i["status"] in ("pending", "approved", "posted", "skipped", "redo", "generating")]
        done.sort(key=lambda i: i["generated_at"], reverse=True)
        return done[:n]

    def prune(self, keep_days: int):
        """Drop old finished items so state.json stays small."""
        cut = now_ts() - keep_days * 86400
        drop = [k for k, i in self.items.items()
                if i["status"] not in OPEN_STATUSES and i.get("updated_at", i["created_at"]) < cut]
        for k in drop:
            del self.items[k]
        if drop:
            self.dirty = True
        return drop
