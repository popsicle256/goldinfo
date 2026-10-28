# -*- coding: utf-8 -*-
"""配置读写。配置与程序放在同一目录，便于绿色迁移。"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "goldinfo.log"

APPDATA = Path(os.environ.get("APPDATA") or Path.home())
STARTUP_FILE = (
    APPDATA
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / "Startup"
    / "GoldInfoMonitor.bat"
)

DEFAULTS = {
    "symbols": ["sh000001", "sh600362", "hf_XAU", "hf_CAD"],
    "names": {},
    "interval": 3,
    "font_size": 10,
    "topmost": True,
    "locked": False,
    "x": None,
    "y": None,
}


def _repair_text(value: str) -> str:
    """修复被错误编码保存的中文（UTF-8 字节按 GBK 解读的情况）。"""
    if not isinstance(value, str) or not value:
        return value
    try:
        fixed = value.encode("gb18030").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return fixed if fixed else value


def _read_json() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    raw = CONFIG_FILE.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            data = json.loads(raw.decode(encoding))
            if isinstance(data, dict):
                return data
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return {}


def load_config() -> dict:
    cfg = deepcopy(DEFAULTS)
    data = _read_json()
    for key in DEFAULTS:
        if key in data:
            cfg[key] = data[key]

    symbols = cfg.get("symbols")
    if not isinstance(symbols, list):
        symbols = []
    cfg["symbols"] = [s for s in symbols if isinstance(s, str) and s.strip()] or list(DEFAULTS["symbols"])

    names = cfg.get("names")
    if not isinstance(names, dict):
        names = {}
    current = set(cfg["symbols"])
    cfg["names"] = {
        str(k): _repair_text(str(v))
        for k, v in names.items()
        if isinstance(k, str) and k in current
    }

    try:
        cfg["interval"] = max(2, min(300, int(cfg.get("interval", 3))))
    except Exception:
        cfg["interval"] = 3
    try:
        cfg["font_size"] = max(8, min(16, int(cfg.get("font_size", 10))))
    except Exception:
        cfg["font_size"] = 10

    for key in ("topmost", "locked"):
        cfg[key] = bool(cfg.get(key))

    for key in ("x", "y"):
        try:
            cfg[key] = int(cfg[key]) if cfg[key] is not None else None
        except Exception:
            cfg[key] = None

    return cfg


def save_config(cfg: dict) -> None:
    try:
        text = json.dumps(cfg, ensure_ascii=False, indent=2)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        pass
