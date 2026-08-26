"""EVE World 独立游戏程序配置"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# WS 服务监听地址与端口
WS_HOST = os.getenv("EVE_WORLD_WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("EVE_WORLD_WS_PORT", "8765"))

# 数据库（默认独立 SQLite，可改用 MySQL）
DATABASE_URL = os.getenv(
    "EVE_WORLD_DATABASE_URL",
    f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'game.db'}",
)

# 日志级别
LOG_LEVEL = os.getenv("EVE_WORLD_LOG_LEVEL", "INFO")

# 星系 CSV 路径
SYSTEMS_CSV = BASE_DIR / "sde" / "universe" / "mapSolarSystems.csv"
JUMPS_CSV = BASE_DIR / "sde" / "universe" / "mapSolarSystemJumps.csv"

# 星系 CSV 下载地址（首次启动自动下载）
FUZZWORK_BASE = "https://www.fuzzwork.co.uk/dump/latest/csv"