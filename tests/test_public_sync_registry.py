from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.public_sync_registry import PublicSyncRegistry


def test_private_registry_round_trip_and_rejects_other_table(tmp_path: Path) -> None:
    path = tmp_path / "managed.json"
    registry = PublicSyncRegistry(path, "app-a", "table-a")
    registry.record_ids.update({"rec-2", "rec-1"})
    registry.save()
    assert json.loads(path.read_text(encoding="utf-8"))["record_ids"] == [
        "rec-1", "rec-2",
    ]
    restored = PublicSyncRegistry(path, "app-a", "table-a")
    restored.load()
    assert restored.record_ids == {"rec-1", "rec-2"}
    with pytest.raises(ValueError, match="标识不匹配"):
        PublicSyncRegistry(path, "app-a", "other-table").load()


def test_missing_private_registry_fails_closed(tmp_path: Path) -> None:
    registry = PublicSyncRegistry(tmp_path / "missing.json", "app", "table")
    with pytest.raises(FileNotFoundError):
        registry.load()
