"""``images/.state.json``：最近一次成功下载的元数据。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE_FILENAME = ".state.json"


def load_state(image_dir: Path) -> dict[str, dict[str, object]]:
    """读取下载状态；文件缺失或损坏时返回空表并给出警告。"""
    path = image_dir / STATE_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(
            f"Warning: ignoring unreadable image state file {path}",
            file=sys.stderr,
        )
        return {}
    if not isinstance(raw, dict):
        print(
            f"Warning: ignoring invalid image state file {path}",
            file=sys.stderr,
        )
        return {}
    state: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            state[key] = value
    return state


def save_state(image_dir: Path, state: dict[str, dict[str, object]]) -> None:
    """原子写入状态文件（先写临时文件再 os.replace）。"""
    image_dir.mkdir(parents=True, exist_ok=True)
    path = image_dir / STATE_FILENAME
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
