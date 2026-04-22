from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def _default_path(project_root: Path) -> Path:
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "telegram_acl.json"


def _normalize_ids(values: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        stripped = values.strip()
        if not stripped:
            return []
        return [part.strip() for part in stripped.split(",") if part.strip()]
    normalized: list[str] = []
    for value in values:
        rendered = str(value).strip()
        if rendered:
            normalized.append(rendered)
    return normalized


class TelegramACL:
    """
    If admin list is empty, single-user mode is used and only primary_chat_id is allowed.
    If admin list is provided, access is admin-only and ACL persistence is ignored.
    """

    def __init__(
        self,
        project_root: Path,
        primary_chat_id: str,
        admin_chat_id: str | list[str] | tuple[str, ...],
        path: Path | None = None,
    ):
        self._path = path or _default_path(project_root)
        self._primary = (primary_chat_id or "").strip()
        self._admins = list(dict.fromkeys(_normalize_ids(admin_chat_id)))
        self._data: dict[str, Any] = {"allowed_ids": [], "pending": {}}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            if self._primary:
                self._data = {"allowed_ids": [self._primary], "pending": {}}
                self._save()
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = {
                "allowed_ids": [str(item) for item in raw.get("allowed_ids", [])],
                "pending": dict(raw.get("pending", {})),
            }
        except Exception:
            self._data = {"allowed_ids": [self._primary] if self._primary else [], "pending": {}}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    @property
    def multi_user_mode(self) -> bool:
        return bool(self._admins)

    @property
    def admin_ids(self) -> list[str]:
        return list(self._admins)

    def is_admin(self, user_id: str) -> bool:
        return str(user_id) in self._admins

    def is_allowed(self, user_id: str) -> bool:
        uid = str(user_id)
        if not self.multi_user_mode:
            return bool(self._primary) and uid == self._primary
        return self.is_admin(uid)

    def is_pending(self, user_id: str) -> bool:
        if self.multi_user_mode:
            return False
        return str(user_id) in self._data["pending"]

    def add_pending(self, user_id: str, username: str | None) -> None:
        if self.multi_user_mode:
            return
        self._data["pending"][str(user_id)] = {
            "username": username,
            "ts": time.time(),
        }
        self._save()

    def approve(self, user_id: str) -> None:
        if self.multi_user_mode:
            return
        uid = str(user_id)
        if uid not in self._data["allowed_ids"]:
            self._data["allowed_ids"].append(uid)
        self._data["pending"].pop(uid, None)
        self._save()

    def reject(self, user_id: str) -> None:
        if self.multi_user_mode:
            return
        self._data["pending"].pop(str(user_id), None)
        self._save()

    def remove_user(self, user_id: str) -> None:
        if self.multi_user_mode:
            return
        uid = str(user_id)
        if uid in self._data["allowed_ids"]:
            self._data["allowed_ids"] = [item for item in self._data["allowed_ids"] if item != uid]
        self._save()

    def add_user_manual(self, user_id: str) -> None:
        if self.multi_user_mode:
            return
        uid = str(user_id)
        if uid not in self._data["allowed_ids"]:
            self._data["allowed_ids"].append(uid)
        self._save()

    def list_allowed(self) -> list[str]:
        if self.multi_user_mode:
            return []
        return list(self._data["allowed_ids"])
