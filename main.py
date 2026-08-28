"""EVE World 独立游戏程序入口

用法:
    python main.py

启动 WS 服务，等待 bot 客户端连接（默认 ws://0.0.0.0:8765）。
"""

import asyncio

from db import create_tables, dispose_engine, init_db, run_migrations
from logger import logger


async def main():
    init_db()
    await create_tables()
    await run_migrations()  # 已有库结构升级（补新增列）
    logger.info("数据库就绪")

    from server import start_server
    await start_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")
    finally:
        asyncio.run(dispose_engine())