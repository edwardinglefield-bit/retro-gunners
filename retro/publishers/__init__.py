"""Social publishers. Each one switches itself on only when its secrets are present."""
from __future__ import annotations

from .base import Publisher, PublishError
from .instagram import Instagram
from .threads import Threads
from .x import X


def enabled_publishers(cfg, vault) -> list[Publisher]:
    pubs = [X(cfg, vault), Threads(cfg, vault), Instagram(cfg, vault)]
    return [p for p in pubs if p.configured() and p.switched_on()]


__all__ = ["enabled_publishers", "Publisher", "PublishError"]
