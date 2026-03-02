from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _default_config() -> dict[str, Any]:
    return {
        "recent_bundle_paths": [],
        "last_extract_dir": "",
        "last_import_image": "",
        "last_import_dir": "",
        "last_output_bundle": "",
        "global_preview_settings": {
            "swizzle_enabled": True,
            "swap_rb": True,
            "flip_tb": True,
            "bc_mode": "auto",
            "pipe_log2": 2,
            "pipe_bank_xor": 0,
            "mip_offset_override": None,
            "use_mip_count": True,
            "roughness_guard": True,
        },
        "texture_settings_overrides": {},
        "operation_history": [],
    }


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = _default_config()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                base = _default_config()
                base.update(loaded)
                self.data = base
            else:
                self.data = _default_config()
        except Exception:
            self.data = _default_config()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_global_settings(self) -> dict[str, Any]:
        value = self.data.get("global_preview_settings", {})
        return value if isinstance(value, dict) else {}

    def set_global_settings(self, settings: dict[str, Any]) -> None:
        self.data["global_preview_settings"] = dict(settings)
        self.save()

    def add_recent_bundle(self, bundle_path: str, max_items: int = 20) -> None:
        path_str = str(bundle_path)
        recent = self.data.get("recent_bundle_paths", [])
        if not isinstance(recent, list):
            recent = []
        recent = [x for x in recent if x != path_str]
        recent.insert(0, path_str)
        self.data["recent_bundle_paths"] = recent[:max_items]
        self.save()

    def get_override(self, bundle_path: str, path_id: int) -> dict[str, Any] | None:
        key = f"{bundle_path}|{int(path_id)}"
        node = self.data.get("texture_settings_overrides", {})
        if isinstance(node, dict):
            value = node.get(key)
            if isinstance(value, dict):
                return value
        return None

    def set_override(self, bundle_path: str, path_id: int, settings: dict[str, Any]) -> None:
        key = f"{bundle_path}|{int(path_id)}"
        node = self.data.get("texture_settings_overrides", {})
        if not isinstance(node, dict):
            node = {}
        node[key] = dict(settings)
        self.data["texture_settings_overrides"] = node
        self.save()

    def clear_override(self, bundle_path: str, path_id: int) -> None:
        key = f"{bundle_path}|{int(path_id)}"
        node = self.data.get("texture_settings_overrides", {})
        if isinstance(node, dict) and key in node:
            node.pop(key, None)
            self.data["texture_settings_overrides"] = node
            self.save()

    def append_history(self, action: str, payload: dict[str, Any], max_items: int = 300) -> None:
        history = self.data.get("operation_history", [])
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "action": action,
                "payload": payload,
            }
        )
        self.data["operation_history"] = history[-max_items:]
        self.save()

