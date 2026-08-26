"""EVE World 独立程序：异步数据库（SQLAlchemy 2.0 async）"""

import os
import re
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config import DATABASE_URL
from logger import logger

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    pass


def init_db():
    global _engine, _session_factory
    connect_args = {}
    if DATABASE_URL.startswith("sqlite"):
        path_part = re.sub(r"^sqlite\+aiosqlite:///", "", DATABASE_URL)
        if path_part and path_part != ":memory:":
            db_dir = os.path.dirname(os.path.abspath(path_part))
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        connect_args["check_same_thread"] = False

    logger.info(f"初始化游戏数据库: {DATABASE_URL}")
    _engine = create_async_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def create_tables():
    import models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _session_factory() as session:
        yield session


async def dispose_engine():
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None