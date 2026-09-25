"""Telegram bot: review queue (Post / Redo / Skip), inbox for your own picks, and a few commands.

No server needed: every scheduled run long-polls getUpdates and handles whatever arrived.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import env
from .http import session

log = logging.getLogger(__name__)
API = "https://api.telegram.org"


class Telegram:
    def __init__(self):
        self.token = env("TELEGRAM_BOT_TOKEN")
        self.owner = env("TELEGRAM_OWNER_ID")

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, files=None, **params):
        if not self.token:
            return None
        url = f"{API}/bot{self.token}/{method}"
        try:
            if files:
                data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
                r = session().post(url, data=data, files=files, timeout=120)
            else:
                r = session().post(url, json=params, timeout=60)
            j = r.json() if r.content else {}
        except Exception as e:   # Telegram hiccups must never break a tick
            log.warning("telegram %s error: %s", method, e)
            return None
        if not j.get("ok"):
            log.warning("telegram %s failed: %s", method, j.get("description") or r.text[:200])
            return None
        return j["result"]

    # --- outgoing ------------------------------------------------------
    def text(self, text: str, reply_to: int | None = None):
        if not self.owner:
            return None
        params = {"chat_id": self.owner, "text": text[:4000], "disable_web_page_preview": True}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to}
        return self._call("sendMessage", **params)

    @staticmethod
    def keyboard(item_id: str, gen: int | str = "") -> dict:
        # gen ties a tap to the exact version you saw (a redo invalidates old buttons)
        return {"inline_keyboard": [[
            {"text": "✅ Post", "callback_data": f"a:{item_id}:{gen}"},
            {"text": "🔁 Redo", "callback_data": f"r:{item_id}:{gen}"},
            {"text": "✖ Skip", "callback_data": f"s:{item_id}:{gen}"},
        ]]}

    def send_preview(self, item: dict, preview: Path, caption: str) -> int | None:
        if not self.owner:
            return None
        with open(preview, "rb") as fh:
            res = self._call("sendPhoto", files={"photo": ("preview.jpg", fh, "image/jpeg")},
                             chat_id=self.owner, caption=caption[:1000],
                             reply_markup=self.keyboard(item["id"], item.get("gen", "")))
        return res["message_id"] if res else None

    def edit_caption(self, message_id: int | None, caption: str, keep_buttons_for: str | None = None):
        if not (self.owner and message_id):
            return
        params = {"chat_id": self.owner, "message_id": message_id, "caption": caption[:1000]}
        if not keep_buttons_for:
            params["reply_markup"] = {"inline_keyboard": []}
        self._call("editMessageCaption", **params)

    def answer(self, callback_id: str, text: str = ""):
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

    # --- incoming ------------------------------------------------------
    def updates(self, offset: int) -> list[dict]:
        res = self._call("getUpdates", offset=offset, timeout=0, limit=100,
                         allowed_updates=["message", "callback_query"])
        return res or []

    def download_file(self, file_id: str) -> bytes | None:
        f = self._call("getFile", file_id=file_id)
        if not f:
            return None
        r = session().get(f"{API}/file/bot{self.token}/{f['file_path']}", timeout=120)
        return r.content if r.ok else None

    def is_owner(self, chat_id) -> bool:
        return bool(self.owner) and str(chat_id) == str(self.owner)
