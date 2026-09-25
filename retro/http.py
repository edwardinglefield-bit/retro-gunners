"""Shared HTTP session with sane retries and a browser-like UA."""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

_s: requests.Session | None = None


def session() -> requests.Session:
    global _s
    if _s is None:
        s = requests.Session()
        retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET", "HEAD"}))  # never auto-resend POSTs (double posts / double billing)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
        _s = s
    return _s


def download(url: str, fallback: str | None = None, timeout: int = 60) -> bytes:
    for u in [url] + ([fallback] if fallback else []):
        try:
            r = session().get(u, timeout=timeout)
            r.raise_for_status()
            if r.content:
                return r.content
        except requests.RequestException:
            if u == (fallback or url):
                raise
    raise RuntimeError(f"could not download {url}")
