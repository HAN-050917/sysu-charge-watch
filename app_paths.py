"""路径兼容层：源码运行与 PyInstaller 打包运行共用同一套目录约定。

源码模式：所有路径就是脚本所在目录（行为与打包前完全一致）。
打包模式：APP_ROOT = exe 所在目录，web/ 与 data/ 都放在 exe 旁边，
         这样配置与历史数据持久化、可见、可备份，不写进临时解压目录。
"""
from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    APP_ROOT = Path(__file__).resolve().parent

WEB_DIR = APP_ROOT / "web"
DATA_DIR = APP_ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
PROFILE_CACHE = DATA_DIR / "profiles.json"
SITES_PATH = DATA_DIR / "sites.json"
DB_PATH = DATA_DIR / "watch.db"

__all__ = [
    "FROZEN", "APP_ROOT", "WEB_DIR", "DATA_DIR", "CONFIG_PATH",
    "PROFILE_CACHE", "SITES_PATH", "DB_PATH",
]
