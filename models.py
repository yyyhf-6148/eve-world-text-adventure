"""EVE World 独立程序：数据库模型"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db import Base

# 自增主键类型：MySQL 用 BIGINT，SQLite 需 INTEGER 才能成为 rowid 别名（自动增长）
AutoId = BigInteger().with_variant(Integer, "sqlite")


class GameSystem(Base):
    """星系表（真实 EVE 星系数据）"""

    __tablename__ = "game_systems"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    security: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    region_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    constellation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class GameJump(Base):
    """星门连接表"""

    __tablename__ = "game_jumps"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    from_system_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    to_system_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class GameShip(Base):
    """舰船模板表"""

    __tablename__ = "game_ships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    ship_class: Mapped[str] = mapped_column(String(20), nullable=False, default="frigate")  # frigate/destroyer/cruiser/battleship
    hull: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    armor: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    shield: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    damage: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    cargo: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    speed: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    high_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    medium_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    low_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    cpu: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    powergrid: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    min_skill: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class GameEquipment(Base):
    """装备模板表（参考 EVE 槽位设定）"""

    __tablename__ = "game_equipments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    slot_type: Mapped[str] = mapped_column(String(10), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    effect_type: Mapped[str] = mapped_column(String(20), nullable=False)
    effect_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cpu_use: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    pg_use: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    base_price: Mapped[float] = mapped_column(Float, nullable=False, default=5000.0)
    skill_type: Mapped[str] = mapped_column(String(20), nullable=False, default="gunnery")  # 需要的技能类型
    min_skill: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class GameInstalled(Base):
    """已安装装备表（装备绑定具体舰船）"""

    __tablename__ = "game_installed"
    __table_args__ = (
        UniqueConstraint("player_id", "ship_id", "equipment_id", name="uq_installed"),
    )

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    ship_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    equipment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class GamePlayerShip(Base):
    """玩家拥有的舰船表（舰船仓库）"""

    __tablename__ = "game_player_ships"
    __table_args__ = (
        UniqueConstraint("player_id", "ship_id", name="uq_player_ship"),
    )

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    ship_id: Mapped[int] = mapped_column(Integer, nullable=False)
    hull: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    armor: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    shield: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)


class GamePlayer(Base):
    """玩家表"""

    __tablename__ = "game_players"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    user_openid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    system_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=30000142)
    ship_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    isk: Mapped[float] = mapped_column(Float, nullable=False, default=100000.0)
    hull: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    armor: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    shield: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    # 技能系统：经验池 + 各技能等级
    unallocated_xp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 未分配经验
    last_xp_time: Mapped[datetime] = mapped_column(DateTime, nullable=True)             # 上次结算时间经验
    ship_skill: Mapped[int] = mapped_column(Integer, nullable=False, default=0)          # 舰船操控
    gunnery_skill: Mapped[int] = mapped_column(Integer, nullable=False, default=0)       # 炮术
    shield_skill: Mapped[int] = mapped_column(Integer, nullable=False, default=0)        # 护盾
    armor_skill: Mapped[int] = mapped_column(Integer, nullable=False, default=0)         # 装甲
    engineering_skill: Mapped[int] = mapped_column(Integer, nullable=False, default=0)   # 工程
    last_action_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class GameCargo(Base):
    """玩家货舱表"""

    __tablename__ = "game_cargo"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_name: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class GameOre(Base):
    """矿石/资源模板表"""

    __tablename__ = "game_ores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    base_value: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    min_security: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    rarity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    yield_per_mine: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class GameMarket(Base):
    """星系市场价格表"""

    __tablename__ = "game_market"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    system_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    item_name: Mapped[str] = mapped_column(String(100), nullable=False)
    buy_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sell_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class GameItem(Base):
    """可购买物品模板表"""

    __tablename__ = "game_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    base_price: Mapped[float] = mapped_column(Float, nullable=False, default=1000.0)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="消耗品")
    effect: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class GameNpc(Base):
    """NPC 海盗模板表"""

    __tablename__ = "game_npcs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    hull: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    damage: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    reward: Mapped[float] = mapped_column(Float, nullable=False, default=1000.0)
    xp_reward: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    min_security: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    rarity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class GameCombat(Base):
    """进行中的战斗状态表"""

    __tablename__ = "game_combats"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    npc_id: Mapped[int] = mapped_column(Integer, nullable=False)
    npc_name: Mapped[str] = mapped_column(String(50), nullable=False)
    npc_hull: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    npc_damage: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    npc_reward: Mapped[float] = mapped_column(Float, nullable=False, default=1000.0)
    npc_xp_reward: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    turn: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class GameBattleLog(Base):
    """PvP 战报表"""

    __tablename__ = "game_battle_logs"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    attacker_openid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    defender_openid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attacker_name: Mapped[str] = mapped_column(String(50), nullable=False)
    defender_name: Mapped[str] = mapped_column(String(50), nullable=False)
    system_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    system_name: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    security: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    winner_openid: Mapped[str] = mapped_column(String(64), nullable=False)
    concord: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attacker_isk_dropped: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    defender_isk_dropped: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    log_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class GameMission(Base):
    """任务模板表"""

    __tablename__ = "game_missions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    mission_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_item: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_amount: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    reward_isk: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0)
    reward_xp: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    min_skill: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class GamePlayerMission(Base):
    """玩家进行中的任务表"""

    __tablename__ = "game_player_missions"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    mission_id: Mapped[int] = mapped_column(Integer, nullable=False)
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)