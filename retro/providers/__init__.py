"""Image-style providers. Add a new one by subclassing StyleProvider and registering it here."""
from __future__ import annotations

from .base import ProviderError, ProviderRefused, StyleProvider


def get_provider(cfg) -> StyleProvider:
    name = cfg.get("style.provider", "openai")
    pcfg = cfg.get(f"style.providers.{name}", {}) or {}
    if name == "openai":
        from .openai_images import OpenAIImages
        return OpenAIImages(pcfg)
    if name == "gemini":
        from .gemini import GeminiImages
        return GeminiImages(pcfg)
    if name == "local":
        from .local import LocalPoster
        return LocalPoster(pcfg)
    raise ValueError(f"unknown style provider: {name}")


__all__ = ["get_provider", "StyleProvider", "ProviderError", "ProviderRefused"]
