from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path


class PublishError(RuntimeError):
    pass


@dataclass
class Media:
    """Everything a publisher might need for one item."""
    paths: dict[str, Path]     # name -> local file (art, wallpaper, story, feed)
    urls: dict[str, str]       # name -> public URL


class Publisher:
    name = "base"

    def __init__(self, cfg, vault):
        self.cfg = cfg
        self.vault = vault

    def configured(self) -> bool:
        raise NotImplementedError

    def switched_on(self) -> bool:
        return True

    def needs_public_urls(self) -> bool:
        return False

    def publish(self, media: Media, caption: str, progress: dict | None = None) -> dict:
        """Return {"id": ..., "url": ...}. `progress` is persisted between attempts so
        multi-step publishers can skip steps that already went live."""
        raise NotImplementedError

    def refresh_token(self):
        """Optional: rotate long-lived tokens."""

    def check(self) -> str:
        """Light connectivity check for `doctor`."""
        return "ok"

    @staticmethod
    def wait(seconds: float):
        time.sleep(seconds)
