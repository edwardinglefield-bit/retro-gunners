"""Config + environment loading."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Config:
    raw: dict
    root: Path
    data_dir: Path

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def read_text(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8").strip()


def load(path: str | None = None) -> Config:
    cfg_path = Path(path or env("RETRO_CONFIG") or ROOT / "config.yaml")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    data_dir = Path(env("RETRO_DATA_DIR") or ROOT / "data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(raw=raw, root=cfg_path.parent.resolve(), data_dir=data_dir)
