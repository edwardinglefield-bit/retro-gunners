"""Public URLs for generated images (Instagram and Threads fetch media from a URL)."""
from __future__ import annotations

import logging
from pathlib import Path

from .config import env
from .http import session

log = logging.getLogger(__name__)


class Hosting:
    def __init__(self, cfg):
        self.cfg = cfg
        self.backend = cfg.get("hosting.backend", "github")
        self.data_dir: Path = cfg.data_dir

    def publish_files(self, folder: Path):
        """Called right after generation. github backend: nothing to do, the workflow pushes the data branch."""
        if self.backend != "r2":
            return
        import boto3  # optional dependency, only for the r2 backend
        s3 = boto3.client("s3", endpoint_url=env("R2_ENDPOINT"),
                          aws_access_key_id=env("R2_ACCESS_KEY_ID"),
                          aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"))
        for p in folder.glob("*.jpg"):
            key = str(p.relative_to(self.data_dir))
            s3.upload_file(str(p), env("R2_BUCKET"), key, ExtraArgs={"ContentType": "image/jpeg"})

    def url(self, rel_path: str) -> str:
        if self.backend == "r2":
            return f"{env('R2_PUBLIC_BASE', '').rstrip('/')}/{rel_path}"
        repo = env("GITHUB_REPOSITORY")
        branch = env("DATA_BRANCH", "data")
        if not repo:
            raise RuntimeError("GITHUB_REPOSITORY not set (needed for github hosting)")
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{rel_path}"

    def is_live(self, rel_path: str) -> bool:
        try:
            r = session().head(self.url(rel_path), timeout=20, allow_redirects=True)
            return r.status_code == 200
        except Exception:
            return False
