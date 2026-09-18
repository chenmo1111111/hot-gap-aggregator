"""Private ownership registry for public Feishu rows.

The public Base must not expose provenance or technical IDs.  Feishu's own
record IDs stay in this root-only local file so sync can still distinguish its
managed rows from rows a person added in the Base.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class PublicSyncRegistry:
    def __init__(self, path: str | Path, app_token: str, table_id: str) -> None:
        self.path = Path(path)
        self.app_token = app_token
        self.table_id = table_id
        self.record_ids: set[str] = set()

    def load(self) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or payload.get("app_token") != self.app_token
            or payload.get("table_id") != self.table_id
            or not isinstance(payload.get("record_ids"), list)
        ):
            raise ValueError("公考公开表私有同步台账格式或表格标识不匹配")
        values = payload["record_ids"]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("公考公开表私有同步台账含无效记录 ID")
        self.record_ids = set(values)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "app_token": self.app_token,
            "table_id": self.table_id,
            "record_ids": sorted(self.record_ids),
        }
        fd, temporary_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent,
        )
        try:
            os.chmod(temporary_name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
