"""EVE World 独立游戏程序：多人共享宇宙游戏逻辑

通过 WebSocket 对外提供服务：接收指令消息，返回文本/markdown 回复。
"""

import asyncio
import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import func, select

from db import get_session
from logger import logger
from models import (
    GameBattleLog,
    GameCargo,
    GameCombat,
    GameEquipment,
    GameInstalled,
    GameItem,
    GameJump,
    GameMarket,
    GameMission,
    GameNpc,
    GameOre,
    GamePlayer,
    GamePlayerMission,
    GamePlayerShip,
    GameShip,
    GameSystem,
)


class Finish(Exception):
    """指令处理结束信号（捕获后返回已收集的回复）"""


# 指令注册表：name -> {"aliases": [...], "handler": async(ctx)}
COMMANDS: dict[str, dict] = {}


def on_command(name, aliases=None, permission=None, description=""):
    """指令装饰器：注册到本程序的指令表"""
    def decorator(handler):
        COMMANDS[name] = {
            "name": name,
            "aliases": [a.lower() for a in (aliases or [])],
            "handler": handler,
            "description": description,
        }
        return handler
    return decorator


def command_list() -> list[dict]:
    """返回指令列表（供 WS 服务端发给客户端注册）"""
    return [
        {"name": c["name"], "aliases": c["aliases"], "description": c["description"]}
        for c in COMMANDS.values()
    ]

# 初始出生星系：Jita (30000142)
START_SYSTEM_ID = 30000142

# 行动冷却（秒）：两次行动之间需等待
ACTION_COOLDOWN = 2

# 移动/跃迁冷却（秒）
MOVE_COOLDOWN = 5

# ==================== 技能系统（EVE 式多分支） ====================
# 经验获取：随时间增长 + PvP 胜利
XP_PER_MINUTE = 0.2          # 每分钟获得经验（每 5 分钟 1 点）
XP_PVP_WIN = 50.0            # PvP 胜利获得经验
SKILL_XP_BASE = 100.0        # 技能 0→1 级所需经验（每级为前一级 10 倍）

# 技能定义：key -> (中文名, 说明)
SKILLS: dict[str, tuple[str, str]] = {
    "ship": ("舰船操控", "决定能驾驶的舰船等级"),
    "gunnery": ("炮术", "提高武器伤害"),
    "shield": ("护盾", "提高护盾量与护盾装备"),
    "armor": ("装甲", "提高装甲量与装甲装备"),
    "engineering": ("工程", "提高装配能力与结构/推进装备"),
}

# 技能属性字段映射
SKILL_FIELD = {
    "ship": "ship_skill",
    "gunnery": "gunnery_skill",
    "shield": "shield_skill",
    "armor": "armor_skill",
    "engineering": "engineering_skill",
}

# 装备效果类型 -> 所需技能
EQUIP_SKILL_BY_EFFECT = {
    "damage": "gunnery",
    "shield": "shield",
    "armor": "armor",
    "hull": "engineering",
    "escape": "engineering",
    "resist": "engineering",
}

# 行动点：每行动扣 1，每 60 秒恢复 1，上限 MAX_ACTION
MAX_ACTION = 10
ACTION_RECOVER_SECONDS = 60

# 舰船模板（真实 EVE 舰船数据：槽位/血量/栅格/CPU 参考官方 SDE）
SHIP_TEMPLATES = [
    {
        "id": 1,
        "name": "Kestrel",
        "hull": 400.0,
        "armor": 350.0,
        "shield": 500.0,
        "damage": 40.0,
        "cargo": 160.0,
        "speed": 1.0,
        "high_slots": 4,
        "medium_slots": 4,
        "low_slots": 2,
        "cpu": 180.0,
        "powergrid": 45.0,
        "cost": 0.0,
        "min_skill": 0,
        "description": "加达里 T1 导弹护卫舰（4高/4中/2低），新手入门船",
    },
    {
        "id": 2,
        "name": "Vexor",
        "hull": 2000.0,
        "armor": 2000.0,
        "shield": 1100.0,
        "damage": 100.0,
        "cargo": 480.0,
        "speed": 0.8,
        "high_slots": 4,
        "medium_slots": 4,
        "low_slots": 5,
        "cpu": 300.0,
        "powergrid": 700.0,
        "cost": 8000000.0,
        "min_skill": 2,
        "description": "盖伦特 T1 无人机巡洋舰（4高/4中/5低），高甲抗",
    },
    {
        "id": 3,
        "name": "Dominix",
        "hull": 9350.0,
        "armor": 8800.0,
        "shield": 7920.0,
        "damage": 220.0,
        "cargo": 750.0,
        "speed": 0.5,
        "high_slots": 6,
        "medium_slots": 5,
        "low_slots": 7,
        "cpu": 600.0,
        "powergrid": 10000.0,
        "cost": 100000000.0,
        "min_skill": 4,
        "description": "盖伦特无人机战列舰（6高/5中/7低），00 区主力",
    },
]

# 装备模板（真实 EVE 装备，效果数值参考游戏内设定）
# 武器伤害参考弹药伤害值；护盾/装甲/结构加成参考扩展装置/附甲板；CPU/栅格参考官方数据
EQUIPMENT_TEMPLATES = [
    # ===== 高槽：武器（伤害参考弹药） =====
    {"name": "轻型导弹发射器", "slot_type": "high", "category": "武器", "effect_type": "damage", "effect_value": 50.0, "cpu_use": 26.0, "pg_use": 2.0, "base_price": 50000.0, "min_skill": 0, "description": "高槽导弹发射器，轻型导弹伤害约 50"},
    {"name": "150mm高斯炮", "slot_type": "high", "category": "武器", "effect_type": "damage", "effect_value": 40.0, "cpu_use": 15.0, "pg_use": 5.0, "base_price": 60000.0, "min_skill": 1, "description": "高槽小型混合炮，近距离高射速"},
    {"name": "重型导弹发射器", "slot_type": "high", "category": "武器", "effect_type": "damage", "effect_value": 150.0, "cpu_use": 50.0, "pg_use": 20.0, "base_price": 300000.0, "min_skill": 2, "description": "高槽导弹发射器，重型导弹伤害约 150"},
    {"name": "425mm磁轨炮", "slot_type": "high", "category": "武器", "effect_type": "damage", "effect_value": 200.0, "cpu_use": 30.0, "pg_use": 100.0, "base_price": 800000.0, "min_skill": 3, "description": "战列舰级混合炮，远程高伤害"},
    # ===== 中槽：护盾 / 推进 =====
    {"name": "小型护盾扩展装置", "slot_type": "medium", "category": "护盾", "effect_type": "shield", "effect_value": 400.0, "cpu_use": 15.0, "pg_use": 10.0, "base_price": 40000.0, "min_skill": 0, "description": "中槽被动护盾，增加护盾量 400"},
    {"name": "中型护盾扩展装置", "slot_type": "medium", "category": "护盾", "effect_type": "shield", "effect_value": 1000.0, "cpu_use": 30.0, "pg_use": 30.0, "base_price": 150000.0, "min_skill": 1, "description": "中槽被动护盾，增加护盾量 1000"},
    {"name": "大型护盾扩展装置", "slot_type": "medium", "category": "护盾", "effect_type": "shield", "effect_value": 2500.0, "cpu_use": 50.0, "pg_use": 100.0, "base_price": 500000.0, "min_skill": 2, "description": "中槽被动护盾，增加护盾量 2500"},
    {"name": "加力燃烧器", "slot_type": "medium", "category": "推进", "effect_type": "escape", "effect_value": 0.15, "cpu_use": 20.0, "pg_use": 5.0, "base_price": 30000.0, "min_skill": 0, "description": "中槽推进，提升逃跑成功率 15%"},
    {"name": "微型跃迁推进器", "slot_type": "medium", "category": "推进", "effect_type": "escape", "effect_value": 0.30, "cpu_use": 30.0, "pg_use": 20.0, "base_price": 120000.0, "min_skill": 2, "description": "中槽推进，大幅提升逃跑成功率 30%"},
    {"name": "停滞缠绕光束", "slot_type": "medium", "category": "电子战", "effect_type": "hull", "effect_value": 300.0, "cpu_use": 40.0, "pg_use": 5.0, "base_price": 200000.0, "min_skill": 2, "description": "中槽电子战（网子），减速敌人并附带结构加成"},
    # ===== 低槽：装甲 / 结构 / 武器升级 =====
    {"name": "小型装甲维修器", "slot_type": "low", "category": "装甲", "effect_type": "armor", "effect_value": 150.0, "cpu_use": 20.0, "pg_use": 5.0, "base_price": 40000.0, "min_skill": 0, "description": "低槽主动维修，增加装甲 150"},
    {"name": "中型装甲维修器", "slot_type": "low", "category": "装甲", "effect_type": "armor", "effect_value": 400.0, "cpu_use": 30.0, "pg_use": 20.0, "base_price": 150000.0, "min_skill": 1, "description": "低槽主动维修，增加装甲 400"},
    {"name": "800mm附带甲板", "slot_type": "low", "category": "装甲", "effect_type": "armor", "effect_value": 2000.0, "cpu_use": 30.0, "pg_use": 100.0, "base_price": 400000.0, "min_skill": 2, "description": "低槽装甲板，大幅增加装甲 2000"},
    {"name": "强化舱隔壁", "slot_type": "low", "category": "结构", "effect_type": "hull", "effect_value": 2000.0, "cpu_use": 20.0, "pg_use": 20.0, "base_price": 300000.0, "min_skill": 1, "description": "低槽结构，增加结构上限 2000"},
    {"name": "损伤控制", "slot_type": "low", "category": "结构", "effect_type": "resist", "effect_value": 0.10, "cpu_use": 25.0, "pg_use": 1.0, "base_price": 150000.0, "min_skill": 1, "description": "低槽核心装备，全伤害减免 10%"},
    {"name": "弹道控制系统", "slot_type": "low", "category": "武器升级", "effect_type": "damage", "effect_value": 50.0, "cpu_use": 30.0, "pg_use": 5.0, "base_price": 200000.0, "min_skill": 2, "description": "低槽导弹伤害升级，+50 伤害"},
]

# 区域/星座名称缓存（可选）
_REGION_NAMES = {
    10000001: "德里克",
    10000002: "断裂",
    # 更多可后续补充；缺失时显示区域ID
}

SYSTEMS_CSV = Path(__file__).resolve().parent / "sde" / "universe" / "mapSolarSystems.csv"
JUMPS_CSV = Path(__file__).resolve().parent / "sde" / "universe" / "mapSolarSystemJumps.csv"


# ==================== 数据导入 ====================

async def _system_table_empty() -> bool:
    async with get_session() as session:
        result = await session.execute(select(func.count()).select_from(GameSystem))
        return result.scalar_one() == 0


async def import_universe_csv() -> None:
    """将星系 CSV 导入数据库（幂等：表空时才导入）"""
    if await _system_table_empty():
        logger.info("[world] game_systems 为空，开始导入星系数据...")
    else:
        logger.info("[world] game_systems 已有数据，跳过星系导入")
        return

    if not SYSTEMS_CSV.exists() or not JUMPS_CSV.exists():
        logger.warning(f"[world] 星系 CSV 缺失：{SYSTEMS_CSV} / {JUMPS_CSV}。"
                       f"请运行 python sde/download_universe.py")
        return

    async with get_session() as session:
        # 导入星系
        systems = []
        with open(SYSTEMS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    sys_id = int(row["solarSystemID"])
                    security = float(row["security"]) if row.get("security") else 1.0
                    systems.append(
                        GameSystem(
                            id=sys_id,
                            name=row.get("solarSystemName", "")[:50],
                            security=security,
                            region_id=int(row["regionID"]) if row.get("regionID") else None,
                            constellation_id=int(row["constellationID"]) if row.get("constellationID") else None,
                        )
                    )
                except (ValueError, KeyError) as e:
                    continue
        session.add_all(systems)
        await session.commit()
        logger.info(f"[world] 星系导入完成：{len(systems)} 个")

        # 导入星门连接
        jumps = []
        seen = set()
        with open(JUMPS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    frm = int(row["fromSolarSystemID"])
                    to = int(row["toSolarSystemID"])
                    key = (min(frm, to), max(frm, to))
                    if key in seen:
                        continue
                    seen.add(key)
                    jumps.append(GameJump(from_system_id=frm, to_system_id=to))
                except (ValueError, KeyError):
                    continue
        session.add_all(jumps)
        await session.commit()
        logger.info(f"[world] 星门连接导入完成：{len(jumps)} 条")


async def _ensure_ships() -> None:
    """确保舰船模板存在"""
    async with get_session() as session:
        count = (await session.execute(select(func.count()).select_from(GameShip))).scalar_one()
        if count > 0:
            return
        for s in SHIP_TEMPLATES:
            session.add(GameShip(**{k: v for k, v in s.items() if k != "id"}))
        await session.commit()
        logger.info("[world] 舰船模板初始化完成")


# 矿石模板
ORE_TEMPLATES = [
    {"name": "凡晶石", "base_value": 25.0, "volume": 0.1, "min_security": 0.5, "rarity": 1.0, "yield_per_mine": 50.0, "description": "常见矿石，适合新手"},
    {"name": "灼烧岩", "base_value": 60.0, "volume": 0.15, "min_security": 0.5, "rarity": 0.8, "yield_per_mine": 35.0, "description": "普通矿石，价值较高"},
    {"name": "灰岩", "base_value": 120.0, "volume": 0.25, "min_security": 0.5, "rarity": 0.6, "yield_per_mine": 25.0, "description": "较值钱的矿石"},
    {"name": "富凡晶石", "base_value": 250.0, "volume": 0.3, "min_security": 0.1, "rarity": 0.4, "yield_per_mine": 18.0, "description": "低安才有的稀有矿石"},
    {"name": "暗曜石", "base_value": 500.0, "volume": 0.4, "min_security": 0.0, "rarity": 0.25, "yield_per_mine": 12.0, "description": "00区高价值矿石"},
    {"name": "黎明石", "base_value": 900.0, "volume": 0.5, "min_security": 0.0, "rarity": 0.12, "yield_per_mine": 8.0, "description": "极其稀有的00区矿石"},
]

# 可购买物品模板（非矿石）
ITEM_TEMPLATES = [
    {"name": "轻型导弹", "base_price": 500.0, "volume": 0.05, "category": "消耗品", "effect": 10.0, "description": "战斗用弹药，增加伤害"},
    {"name": "重型导弹", "base_price": 2000.0, "volume": 0.15, "category": "消耗品", "effect": 30.0, "description": "战斗用弹药，大幅增加伤害"},
    {"name": "护盾增强器", "base_price": 50000.0, "volume": 5.0, "category": "升级", "effect": 200.0, "description": "一次性升级，增加护盾上限"},
    {"name": "装甲维修器", "base_price": 50000.0, "volume": 5.0, "category": "升级", "effect": 200.0, "description": "一次性升级，增加装甲上限"},
    {"name": "跃迁核心稳定器", "base_price": 100000.0, "volume": 5.0, "category": "升级", "effect": 1.0, "description": "降低被跃迁扰断的概率"},
]

# 任务模板
MISSION_TEMPLATES = [
    {"name": "采集凡晶石", "mission_type": "mine", "target_item": "凡晶石", "target_amount": 200.0, "reward_isk": 20000.0, "reward_xp": 30.0, "min_skill": 0, "description": "在任意高安星系采集 200 单位凡晶石"},
    {"name": "采集灰岩", "mission_type": "mine", "target_item": "灰岩", "target_amount": 100.0, "reward_isk": 40000.0, "reward_xp": 60.0, "min_skill": 1, "description": "采集 100 单位灰岩"},
    {"name": "采集暗曜石", "mission_type": "mine", "target_item": "暗曜石", "target_amount": 50.0, "reward_isk": 100000.0, "reward_xp": 120.0, "min_skill": 2, "description": "前往 00 区采集 50 单位暗曜石（高风险高回报）"},
    {"name": "清剿流浪海盗", "mission_type": "kill", "target_item": "流浪海盗", "target_amount": 3.0, "reward_isk": 30000.0, "reward_xp": 50.0, "min_skill": 0, "description": "消灭 3 个流浪海盗"},
    {"name": "猎杀血袭者", "mission_type": "kill", "target_item": "血袭者劫掠者", "target_amount": 2.0, "reward_isk": 60000.0, "reward_xp": 80.0, "min_skill": 1, "description": "消灭 2 个血袭者劫掠者"},
    {"name": "猎杀00区无畏舰", "mission_type": "kill", "target_item": "00区无畏舰", "target_amount": 1.0, "reward_isk": 200000.0, "reward_xp": 200.0, "min_skill": 3, "description": "消灭 1 艘 00区无畏舰"},
    {"name": "送货到佩里姆特", "mission_type": "deliver", "target_item": "Perimeter", "target_amount": 1.0, "reward_isk": 15000.0, "reward_xp": 20.0, "min_skill": 0, "description": "前往 Perimeter 星系"},
    {"name": "送货到低安", "mission_type": "deliver", "target_item": "低安", "target_amount": 1.0, "reward_isk": 50000.0, "reward_xp": 60.0, "min_skill": 1, "description": "前往任意低安星系（0.1-0.4）"},
]

# NPC 海盗模板
NPC_TEMPLATES = [
    {"name": "流浪海盗", "hull": 120.0, "damage": 12.0, "reward": 5000.0, "xp_reward": 15.0, "min_security": 0.5, "rarity": 1.0, "description": "高安偶尔出没的流寇"},
    {"name": "血袭者劫掠者", "hull": 200.0, "damage": 20.0, "reward": 12000.0, "xp_reward": 30.0, "min_security": 0.5, "rarity": 0.6, "description": "有组织的海盗团伙"},
    {"name": "天使企业精英", "hull": 400.0, "damage": 35.0, "reward": 35000.0, "xp_reward": 60.0, "min_security": 0.1, "rarity": 0.5, "description": "低安活跃的精英海盗"},
    {"name": "萨沙超级航母", "hull": 800.0, "damage": 60.0, "reward": 100000.0, "xp_reward": 120.0, "min_security": 0.1, "rarity": 0.3, "description": "低安深处的强大敌舰"},
    {"name": "00区无畏舰", "hull": 1500.0, "damage": 100.0, "reward": 300000.0, "xp_reward": 250.0, "min_security": -1.0, "rarity": 0.25, "description": "00 区盘踞的重型敌舰，极度危险"},
    {"name": "虫洞至高母舰", "hull": 3000.0, "damage": 180.0, "reward": 800000.0, "xp_reward": 500.0, "min_security": -1.0, "rarity": 0.1, "description": "虫洞深处的最强存在"},
]


async def _ensure_ores_and_items() -> None:
    """确保矿石和物品模板存在"""
    async with get_session() as session:
        n_ore = (await session.execute(select(func.count()).select_from(GameOre))).scalar_one()
        if n_ore == 0:
            for o in ORE_TEMPLATES:
                session.add(GameOre(**o))
            logger.info("[world] 矿石模板初始化完成")

        n_item = (await session.execute(select(func.count()).select_from(GameItem))).scalar_one()
        if n_item == 0:
            for it in ITEM_TEMPLATES:
                session.add(GameItem(**it))
            logger.info("[world] 物品模板初始化完成")

        n_npc = (await session.execute(select(func.count()).select_from(GameNpc))).scalar_one()
        if n_npc == 0:
            for npc in NPC_TEMPLATES:
                session.add(GameNpc(**npc))
            logger.info("[world] NPC 模板初始化完成")

        n_mission = (await session.execute(select(func.count()).select_from(GameMission))).scalar_one()
        if n_mission == 0:
            for m in MISSION_TEMPLATES:
                session.add(GameMission(**m))
            logger.info("[world] 任务模板初始化完成")

        n_equip = (await session.execute(select(func.count()).select_from(GameEquipment))).scalar_one()
        if n_equip == 0:
            for e in EQUIPMENT_TEMPLATES:
                session.add(GameEquipment(**e))
            logger.info("[world] 装备模板初始化完成")

        await session.commit()


async def _init_world():
    await import_universe_csv()
    await _ensure_ships()
    await _ensure_ores_and_items()
    await _build_volume_cache()


# ==================== 辅助函数 ====================


# ==================== 辅助函数 ====================

async def _get_player(user_openid: str) -> GamePlayer | None:
    async with get_session() as session:
        result = await session.execute(
            select(GamePlayer).where(GamePlayer.user_openid == user_openid)
        )
        return result.scalar_one_or_none()


async def _get_system(system_id: int) -> GameSystem | None:
    async with get_session() as session:
        result = await session.execute(select(GameSystem).where(GameSystem.id == system_id))
        return result.scalar_one_or_none()


async def _get_system_by_name(name: str) -> GameSystem | None:
    async with get_session() as session:
        result = await session.execute(
            select(GameSystem).where(func.lower(GameSystem.name) == name.lower())
        )
        return result.scalar_one_or_none()


async def _get_ship(ship_id: int) -> GameShip | None:
    async with get_session() as session:
        result = await session.execute(select(GameShip).where(GameShip.id == ship_id))
        return result.scalar_one_or_none()


def _sec_label(security: float) -> str:
    """安全等级文本"""
    if security >= 0.5:
        return "高安"
    elif security >= 0.1:
        return "低安"
    elif security > 0.0:
        return "零安"
    else:
        return "虫洞/未知"


def _security_color(security: float) -> str:
    if security >= 0.5:
        return "🟢"
    elif security >= 0.1:
        return "🟡"
    elif security > 0.0:
        return "🟠"
    else:
        return "🔴"


# ==================== 菜单系统（文字游戏界面） ====================

def _cmd(text: str, show: str = "") -> str:
    """生成点击后填入输入框的交互标签（qqbot-cmd-input，支持群聊）
    text 为点击后填入输入框的命令，show 为消息内显示的文字"""
    show = show or text
    return f'<qqbot-cmd-input text="{quote(text)}" show="{quote(show)}" />'


def _cmd_menu(cmd: str, desc: str) -> str:
    """菜单项：无参指令可直接点击；带参指令填入"指令 "供补全"""
    if " " in cmd:
        base = cmd.split()[0]
        return f"· {_cmd(base + ' ', cmd)} {desc}"
    return f"· {_cmd(cmd, cmd)} {desc}"


# 功能分区：key -> (标题, [(指令, 说明)])
MENU_SECTIONS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "explore": ("探索", [("scan", "扫描当前星系"), ("move 星系名", "通过星门跃迁")]),
    "mine": ("采矿", [("mine", "采集矿石"), ("cargo", "查看货舱")]),
    "market": ("市场", [("market", "查看行情"), ("sell 物品", "出售"), ("buy 物品", "购买")]),
    "combat": ("战斗", [("hunt", "巡逻遇敌"), ("fight", "开火"), ("flee", "逃跑")]),
    "fleet": ("舰队", [("fleet", "我的舰队"), ("upgrade 船名", "购买舰船"), ("switch 船名", "切换驾驶"), ("fittings", "配装"), ("install 装备", "安装"), ("uninstall 装备", "卸下")]),
    "mission": ("任务", [("mission", "查看任务"), ("mission new", "接取新任务")]),
    "skill": ("技能", [("skills", "查看技能与经验"), ("train 技能名", "分配经验到技能")]),
    "status": ("状态", [("status", "我的状态"), ("attack 玩家", "PvP 攻击"), ("report", "PvP 战报")]),
}


def _menu_block(title: str, cmds: list[tuple[str, str]]) -> str:
    lines = [f"━━━ {title} ━━━"]
    for cmd, desc in cmds:
        lines.append(_cmd_menu(cmd, desc))
    return "\n".join(lines)


def _main_menu(name: str, sys_name: str) -> str:
    """主菜单 markdown（文字游戏控制台风格）"""
    lines = [
        f"🚀 **EVE 新伊甸 · 星海航行**",
        f"════════════════════",
        f"👤 飞行员：`{name}`",
        f"📍 位置：`{sys_name}`",
        f"",
    ]
    for title, cmds in MENU_SECTIONS.values():
        lines.append(_menu_block(title, cmds))
        lines.append("")
    lines.append("─────────────")
    lines.append(f"💡 点击指令标签即可快速输入")
    lines.append(f"🔄 发送 {_cmd('start', '主菜单')} 随时返回本页")
    return "\n".join(lines)


def _page_hint(key: str) -> str:
    """功能页操作提示页脚（markdown，指令可点击）"""
    entry = MENU_SECTIONS.get(key)
    if not entry:
        return ""
    title, cmds = entry
    lines = ["", f"━━━ {title}页操作 ━━━"]
    for cmd, desc in cmds:
        lines.append(_cmd_menu(cmd, desc))
    lines.append(f"· {_cmd('start', '主菜单')} 返回主菜单")
    return "\n".join(lines)


def _cooldown_ok(player: GamePlayer, cooldown: float = ACTION_COOLDOWN) -> bool:
    """检查行动冷却是否就绪（cooldown 秒内不可再次行动）"""
    if player.last_action_at is None:
        return True
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last = player.last_action_at
    if last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    return (now - last).total_seconds() >= cooldown


async def _update_cooldown(user_openid: str) -> None:
    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.user_openid == user_openid))
        player = result.scalar_one_or_none()
        if player:
            player.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()


# ==================== Phase 2: 采矿 / 市场 / 货舱 ====================

async def _get_ore_by_name(name: str) -> GameOre | None:
    async with get_session() as session:
        result = await session.execute(
            select(GameOre).where(func.lower(GameOre.name) == name.lower())
        )
        return result.scalar_one_or_none()


async def _get_item_by_name(name: str) -> GameItem | None:
    async with get_session() as session:
        result = await session.execute(
            select(GameItem).where(func.lower(GameItem.name) == name.lower())
        )
        return result.scalar_one_or_none()


async def _get_cargo(player_id: int) -> list[GameCargo]:
    async with get_session() as session:
        result = await session.execute(select(GameCargo).where(GameCargo.player_id == player_id))
        return list(result.scalars().all())


async def _cargo_total_volume(player_id: int) -> float:
    """计算货舱已用体积"""
    items = await _get_cargo(player_id)
    total = 0.0
    for c in items:
        if c.item_name in _ORE_NAME_TO_VOLUME:
            total += c.quantity * _ORE_NAME_TO_VOLUME[c.item_name]
        elif c.item_name in _ITEM_NAME_TO_VOLUME:
            total += c.quantity * _ITEM_NAME_TO_VOLUME[c.item_name]
        else:
            total += c.quantity
    return total


# 名称 -> 体积 缓存（避免频繁查库）
_ORE_NAME_TO_VOLUME: dict[str, float] = {}
_ITEM_NAME_TO_VOLUME: dict[str, float] = {}


async def _build_volume_cache() -> None:
    """构建矿石/物品体积缓存"""
    async with get_session() as session:
        for o in (await session.execute(select(GameOre))).scalars().all():
            _ORE_NAME_TO_VOLUME[o.name] = o.volume
        for it in (await session.execute(select(GameItem))).scalars().all():
            _ITEM_NAME_TO_VOLUME[it.name] = it.volume


async def _get_or_create_market(system_id: int, item_id: int, item_name: str) -> GameMarket:
    """获取或创建星系市场价格记录"""
    async with get_session() as session:
        result = await session.execute(
            select(GameMarket).where(
                GameMarket.system_id == system_id,
                GameMarket.item_id == item_id,
            )
        )
        market = result.scalar_one_or_none()
        if market is not None:
            return market
        # 默认价格：矿石按基准价，物品按基准价
        price = 0.0
        o = await _get_ore_by_name(item_name)
        it = await _get_item_by_name(item_name)
        if o:
            price = o.base_value
        elif it:
            price = it.base_price
        market = GameMarket(
            system_id=system_id,
            item_id=item_id,
            item_name=item_name,
            buy_price=price,
            sell_price=price,
        )
        session.add(market)
        await session.commit()
        return market


def _market_price_multiplier(security: float, item_name: str, ore: bool = True) -> float:
    """市场价格乘数：安全等级影响供需
    - 高安(0.5+)：矿石收购价低(0.7x)，物品售价低(0.8x) —— 物品丰富
    - 低安(0.1-0.5)：中等
    - 00(<0.1)：矿石收购价高(1.5x)，物品售价高(1.2x) —— 风险溢价
    """
    if ore:
        if security >= 0.5:
            return 0.7
        elif security >= 0.1:
            return 1.0
        else:
            return 1.5
    else:
        if security >= 0.5:
            return 0.8
        elif security >= 0.1:
            return 1.0
        else:
            return 1.2


async def _market_for_system(system_id: int) -> list[GameMarket]:
    async with get_session() as session:
        result = await session.execute(select(GameMarket).where(GameMarket.system_id == system_id))
        return list(result.scalars().all())


async def _add_to_cargo(player_id: int, item_id: int, item_name: str, quantity: float) -> bool:
    """添加到货舱（调用方需先校验体积）"""
    async with get_session() as session:
        result = await session.execute(
            select(GameCargo).where(
                GameCargo.player_id == player_id,
                GameCargo.item_id == item_id,
            )
        )
        cargo = result.scalar_one_or_none()
        if cargo:
            cargo.quantity += quantity
        else:
            session.add(GameCargo(player_id=player_id, item_id=item_id, item_name=item_name, quantity=quantity))
        await session.commit()
    return True


async def _player_ship(player: GamePlayer) -> GameShip | None:
    return await _get_ship(player.ship_id)


# ==================== 技能系统辅助 ====================

def _skill_level_of(player: GamePlayer, skill: str) -> int:
    """获取玩家指定技能等级"""
    return getattr(player, SKILL_FIELD.get(skill, "ship_skill"), 0)


def _skill_cost(level: int) -> float:
    """从 level 升到 level+1 所需经验（每级为前一级 10 倍）"""
    return SKILL_XP_BASE * (10 ** level)


async def _settle_xp(player: GamePlayer) -> float:
    """结算时间累积的经验到经验池，返回本次新增经验"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last = player.last_xp_time
    if last is None:
        player.last_xp_time = now
        return 0.0
    if last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    elapsed_min = max(0.0, (now - last).total_seconds() / 60.0)
    gained = elapsed_min * XP_PER_MINUTE
    player.unallocated_xp += gained
    player.last_xp_time = now
    return gained


async def _ensure_xp(player: GamePlayer) -> None:
    """确保玩家时间经验已结算（在指令处理前调用，落库）"""
    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
        p = result.scalar_one_or_none()
        if p:
            await _settle_xp(p)
            await session.commit()
            player.unallocated_xp = p.unallocated_xp
            player.last_xp_time = p.last_xp_time


def _max_skill(player: GamePlayer) -> int:
    """最高技能等级（用于任务门槛等）"""
    return max(
        player.ship_skill, player.gunnery_skill,
        player.shield_skill, player.armor_skill, player.engineering_skill,
    )


# ==================== 指令 ====================

@on_command("start", aliases=["注册", "开始游戏", "菜单", "主菜单"])
async def handle_start(ctx):
    """注册角色并显示主菜单"""
    user_openid = ctx.user_openid
    if not user_openid:
        await ctx.finish_markdown("无法获取你的身份信息")

    display_name = ctx.member_username or "新飞行员"
    is_new = False

    existing = await _get_player(user_openid)
    if not existing:
        is_new = True
        async with get_session() as session:
            player = GamePlayer(
                user_openid=user_openid,
                name=display_name,
                system_id=START_SYSTEM_ID,
                ship_id=1,
                isk=100000.0,
            )
            # 初始船只完整状态
            ship = await _get_ship(1)
            if ship:
                player.hull = ship.hull
                player.armor = ship.armor
                player.shield = ship.shield
            session.add(player)
            await session.flush()
            # 初始舰船入仓（舰船仓库）
            session.add(
                GamePlayerShip(
                    player_id=player.id,
                    ship_id=1,
                    hull=ship.hull if ship else 150.0,
                    armor=ship.armor if ship else 120.0,
                    shield=ship.shield if ship else 150.0,
                )
            )
            await session.commit()

    start_sys = await _get_system(START_SYSTEM_ID)
    sys_name = start_sys.name if start_sys else "Jita"

    header = f"🚀 **欢迎加入新伊甸，飞行员 {display_name}！**\n\n" if is_new else f"🚀 **欢迎回来，飞行员 {display_name}！**\n\n"

    await ctx.finish_markdown(
        header + _main_menu(display_name, sys_name)
    )


@on_command("status", aliases=["状态", "我的状态"])
async def handle_status(ctx):
    """查看自身状态"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    await _ensure_xp(player)

    ship = await _get_ship(player.ship_id)
    system = await _get_system(player.system_id)

    ship_name = ship.name if ship else "未知"
    sys_name = f"{system.name} ({_sec_label(system.security)})" if system else "未知星系"

    # 配船综合属性
    stats = await _fleet_stats(player, ship) if ship else None
    if stats:
        hp_line = (
            f"· 结构：`{player.hull:.0f}/{stats['hull']:.0f}` "
            f"装甲：`{player.armor:.0f}/{stats['armor']:.0f}` "
            f"护盾：`{player.shield:.0f}/{stats['shield']:.0f}`"
        )
        combat_line = f"· 综合火力：`{stats['damage']:.0f}` 抗性：`{stats['resist']*100:.0f}%` 逃跑率：`{stats['escape']*100:.0f}%`"
    else:
        hp_line = f"· 结构：`{player.hull:.0f}` / 装甲：`{player.armor:.0f}` / 护盾：`{player.shield:.0f}`"
        combat_line = ""

    lines = [
        f"🛰 **{player.name} 的状态**",
        f"· 舰船：`{ship_name}`",
        f"· 位置：`{sys_name}`",
        f"· 资金：`{player.isk:,.0f} ISK`",
        hp_line,
    ]
    if combat_line:
        lines.append(combat_line)
    lines.append(f"· 经验池：`{player.unallocated_xp:.0f} XP`")
    lines.append(
        f"· 技能：舰船{player.ship_skill} 炮术{player.gunnery_skill} "
        f"护盾{player.shield_skill} 装甲{player.armor_skill} 工程{player.engineering_skill}"
    )

    # 货舱
    async with get_session() as session:
        result = await session.execute(
            select(GameCargo).where(GameCargo.player_id == player.id)
        )
        cargo_items = result.scalars().all()
    if cargo_items:
        lines.append(f"· 货舱：`{sum(c.quantity for c in cargo_items):.0f}/{ship.cargo:.0f}`")
    else:
        lines.append(f"· 货舱：`0/{ship.cargo:.0f}`")

    lines.append(_page_hint("status"))
    await ctx.finish_markdown("\n".join(lines))


@on_command("scan", aliases=["扫描"])
async def handle_scan(ctx):
    """扫描当前星系"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    system = await _get_system(player.system_id)
    if not system:
        await ctx.finish("当前位置数据异常")

    # 邻接星系
    async with get_session() as session:
        result = await session.execute(
            select(GameJump).where(
                (GameJump.from_system_id == player.system_id) | (GameJump.to_system_id == player.system_id)
            )
        )
        jumps = result.scalars().all()
        neighbor_ids = set()
        for j in jumps:
            neighbor_ids.add(j.from_system_id if j.from_system_id != player.system_id else j.to_system_id)

    neighbor_systems = []
    if neighbor_ids:
        async with get_session() as session:
            result = await session.execute(
                select(GameSystem).where(GameSystem.id.in_(neighbor_ids))
            )
            neighbor_systems = list(result.scalars().all())

    # 在线玩家（同星系的其他玩家）
    async with get_session() as session:
        result = await session.execute(
            select(GamePlayer).where(GamePlayer.system_id == player.system_id)
        )
        players_here = [p for p in result.scalars().all() if p.user_openid != ctx.user_openid]

    lines = [
        f"🔭 **扫描结果：{system.name}**",
        f"· 安全等级：`{system.security:.2f}` {_security_color(system.security)}{_sec_label(system.security)}",
        f"· 星门连接：`{len(neighbor_ids)}` 条",
    ]

    if neighbor_systems:
        lines.append("· 可前往星系（点击跃迁）：")
        for ns in sorted(neighbor_systems, key=lambda s: s.security, reverse=True)[:10]:
            lines.append(
                f"  {_security_color(ns.security)}{_cmd(f'move {ns.name}', ns.name)}（{_sec_label(ns.security)}）"
            )
    else:
        lines.append("· 无可前往星系")

    if players_here:
        lines.append(f"· 在线飞行员：{len(players_here)} 人")
    else:
        lines.append("· 附近没有其他飞行员")

    lines.append(_page_hint("explore"))
    await ctx.finish_markdown("\n".join(lines))


@on_command("move", aliases=["移动", "跃迁", "前往"])
async def handle_move(ctx):
    """通过星门移动"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    if not _cooldown_ok(player, MOVE_COOLDOWN):
        await ctx.finish("跃迁冷却中，请稍候片刻")

    target_name = ctx.args_text.strip()
    if not target_name:
        await ctx.finish("用法：move 星系名，例：move Jita")

    target = await _get_system_by_name(target_name)
    if not target:
        await ctx.finish(f"未找到星系「{target_name}」，请检查拼写")

    # 检查是否相邻
    async with get_session() as session:
        result = await session.execute(
            select(GameJump).where(
                (
                    (GameJump.from_system_id == player.system_id) & (GameJump.to_system_id == target.id)
                )
                | (
                    (GameJump.from_system_id == target.id) & (GameJump.to_system_id == player.system_id)
                )
            )
        )
        is_connected = result.scalar_one_or_none() is not None

    current_sys = await _get_system(player.system_id)

    if target.id == player.system_id:
        await ctx.finish(f"你已经在这个星系了（{current_sys.name}）")

    if not is_connected:
        await ctx.finish(
            f"没有从 `{current_sys.name}` 到 `{target.name}` 的星门连接。"
            f"请先 `scan` 查看可前往的星系"
        )

    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.user_openid == ctx.user_openid))
        p = result.scalar_one_or_none()
        if p:
            p.system_id = target.id
            p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()

    # 送货任务进度更新
    mission_note = ""
    pm = await _get_player_mission(player.id)
    if pm:
        mission = await _get_mission(pm.mission_id)
        if mission and mission.mission_type == "deliver":
            # 目标：具体星系名，或 "低安"（任意低安星系）
            deliver_target = mission.target_item
            match = False
            if deliver_target == "低安":
                match = 0.1 <= target.security < 0.5
            else:
                match = target.name.lower() == deliver_target.lower()
            if match:
                _m, completed = await _mission_progress(player.id, "deliver", deliver_target, 1.0)
                if completed and _m:
                    mission_note = f"\n✅ **任务完成！** `{_m.name}` 奖励 `+{_m.reward_isk:,.0f} ISK`"

    await ctx.finish_markdown(
        f"🚀 **跃迁完成**\n"
        f"· 从 `{current_sys.name}` 通过星门到达 `{target.name}`\n"
        f"· 安全等级：`{target.security:.2f}` {_security_color(target.security)}{_sec_label(target.security)}"
        f"{mission_note}\n"
        f"\n"
        f"点击 {_cmd('scan', '扫描新星系')} 查看周围环境"
        + _page_hint("explore")
    )


@on_command("mine", aliases=["采矿", "采集"])
async def handle_mine(ctx):
    """采矿"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    if not _cooldown_ok(player):
        await ctx.finish("行动冷却中，请稍候片刻")

    system = await _get_system(player.system_id)
    if not system:
        await ctx.finish("当前位置数据异常")

    # 矿石选择：参数指定，否则自动选当前星系可采的最常见矿石
    ore_name = ctx.args_text.strip()
    ore = None
    if ore_name:
        ore = await _get_ore_by_name(ore_name)
        if not ore:
            await ctx.finish(f"未找到矿石「{ore_name}」。发送 `market` 查看本星系矿石")
        # 安全等级限制
        if system.security < ore.min_security:
            await ctx.finish(
                f"当前星系安全等级过低，无法开采 `{ore.name}`（需要 ≥ {ore.min_security}）"
            )
    else:
        # 自动选择：可采矿石中稀有度最高（最常见）的
        async with get_session() as session:
            result = await session.execute(select(GameOre))
            all_ores = list(result.scalars().all())
        candidates = [o for o in all_ores if system.security >= o.min_security]
        if not candidates:
            await ctx.finish("当前星系没有可开采的矿石，请前往其他星系")
        ore = max(candidates, key=lambda o: o.rarity)

    ship = await _get_ship(player.ship_id)
    cargo_used = await _cargo_total_volume(player.id)

    # 计算可采数量（受货舱限制）
    amount = ore.yield_per_mine
    volume = amount * ore.volume
    if cargo_used + volume > ship.cargo:
        # 装满为止
        remaining = ship.cargo - cargo_used
        if remaining <= 0:
            await ctx.finish("货舱已满，请先 `sell` 出售或 `cargo` 查看")
        amount = remaining / ore.volume
        volume = remaining

    # 随机性：产量波动 ±20%
    amount *= random.uniform(0.8, 1.2)
    amount = round(amount, 1)
    volume = round(amount * ore.volume, 1)

    await _add_to_cargo(player.id, ore.id, ore.name, amount)
    await _update_cooldown(ctx.user_openid)

    # 任务进度更新（采矿任务）
    mission_done = ""
    mission, completed = await _mission_progress(player.id, "mine", ore.name, amount)
    if completed and mission:
        mission_done = f"\n✅ **任务完成！** `{mission.name}` 奖励 `+{mission.reward_isk:,.0f} ISK`"

    await ctx.finish_markdown(
        f"⛏ **采矿完成**\n"
        f"· 在 `{system.name}` 采集了 `{amount:.0f} 单位` {ore.name}\n"
        f"· 占用货舱：`{volume:.1f} m³`（{cargo_used:.1f}/{ship.cargo:.1f}）"
        f"{mission_done}\n"
        f"\n"
        f"点击 {_cmd(f'sell {ore.name}', f'出售{ore.name}')} 或 {_cmd('market', '查看价格')}"
        + _page_hint("mine")
    )


@on_command("cargo", aliases=["货舱", "背包"])
async def handle_cargo(ctx):
    """查看货舱"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    ship = await _get_ship(player.ship_id)
    items = await _get_cargo(player.id)
    cargo_used = await _cargo_total_volume(player.id)

    lines = [
        f"📦 **货舱（{cargo_used:.1f}/{ship.cargo:.1f} m³）**",
    ]

    if not items:
        lines.append(f"· 空空如也，点击 {_cmd('mine', '开始采矿')}")
    else:
        for c in items:
            unit_vol = _ORE_NAME_TO_VOLUME.get(c.item_name) or _ITEM_NAME_TO_VOLUME.get(c.item_name) or 1.0
            lines.append(
                f"· `{c.item_name}` × {c.quantity:.0f}（{c.quantity * unit_vol:.1f} m³）"
            )

    lines.append(_page_hint("mine"))
    await ctx.finish_markdown("\n".join(lines))


@on_command("market", aliases=["市场", "价格"])
async def handle_market(ctx):
    """查看当前星系市场价格"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    system = await _get_system(player.system_id)
    if not system:
        await ctx.finish("当前位置数据异常")

    # 获取本星系已有市场记录；没有则用模板基准价计算
    markets = await _market_for_system(player.system_id)
    ore_mult = _market_price_multiplier(system.security, "", ore=True)
    item_mult = _market_price_multiplier(system.security, "", ore=False)

    # 矿石列表
    async with get_session() as session:
        ores = list((await session.execute(select(GameOre))).scalars().all())
        items = list((await session.execute(select(GameItem))).scalars().all())
        equipments = list((await session.execute(select(GameEquipment))).scalars().all())

    lines = [
        f"💰 **市场行情：{system.name}**",
        f"· 安全等级：`{system.security:.2f}`（{_sec_label(system.security)}）",
        f"",
        f"**矿石（收购价）**",
    ]

    for o in ores:
        # 不可采的矿石显示灰色
        available = system.security >= o.min_security
        price = o.base_value * ore_mult
        mark = "" if available else "（不可采）"
        lines.append(f"· `{o.name}`：`{price:,.0f}` ISK/单位{mark}")

    lines.append("")
    lines.append("**消耗品（售价）**")
    for it in items:
        price = it.base_price * item_mult
        lines.append(f"· `{it.name}`：`{price:,.0f}` ISK")

    # 装备按槽位分类显示
    slot_labels = {"high": "高槽·武器", "medium": "中槽·护盾/推进/电子战", "low": "低槽·装甲/结构/武器升级"}
    for slot_key, label in slot_labels.items():
        eqs = [e for e in equipments if e.slot_type == slot_key]
        if eqs:
            lines.append("")
            lines.append(f"**{label}（售价）**")
            for e in eqs:
                price = e.base_price * item_mult
                lines.append(f"· `{e.name}`：`{price:,.0f}` ISK（CPU {e.cpu_use:.0f}/栅格 {e.pg_use:.0f}）")

    lines.append("")
    lines.append("💡 提示：装备用 `buy` 购买，`install` 安装，受槽位和 CPU/栅格限制")
    lines.append("💡 00 区矿石收购价更高，物品售价也更贵")

    lines.append(_page_hint("market"))
    await ctx.finish_markdown("\n".join(lines))


@on_command("sell", aliases=["出售", "卖"])
async def handle_sell(ctx):
    """出售矿石/物品"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    if not _cooldown_ok(player):
        await ctx.finish("行动冷却中，请稍候片刻")

    args = ctx.args_text.strip().split()
    if not args:
        await ctx.finish("用法：sell 物品名 [数量]，例：sell 凡晶石")

    item_name = args[0]
    quantity = None
    if len(args) > 1:
        try:
            quantity = float(args[1])
        except ValueError:
            await ctx.finish("数量格式错误")

    # 查找货舱中的物品
    items = await _get_cargo(player.id)
    cargo_item = next((c for c in items if c.item_name.lower() == item_name.lower()), None)
    if not cargo_item:
        await ctx.finish(f"货舱中没有「{item_name}」")

    sell_qty = min(quantity or cargo_item.quantity, cargo_item.quantity)

    system = await _get_system(player.system_id)

    # 计算价格（矿石 / 普通物品 / 装备）
    ore = await _get_ore_by_name(cargo_item.item_name)
    item_t = await _get_item_by_name(cargo_item.item_name)
    equip_t = await _get_equipment_by_name(cargo_item.item_name)
    if ore:
        mult = _market_price_multiplier(system.security, ore.name, ore=True)
        unit_price = ore.base_value * mult
    elif item_t:
        mult = _market_price_multiplier(system.security, item_t.name, ore=False)
        unit_price = item_t.base_price * mult
    elif equip_t:
        mult = _market_price_multiplier(system.security, equip_t.name, ore=False)
        unit_price = equip_t.base_price * mult
    else:
        await ctx.finish(f"无法确定「{cargo_item.item_name}」的价值")

    total_income = unit_price * sell_qty

    # 扣减货舱，增加 ISK
    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.user_openid == ctx.user_openid))
        p = result.scalar_one_or_none()
        if p:
            p.isk += total_income
            p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)

        cargo_result = await session.execute(
            select(GameCargo).where(
                GameCargo.player_id == p.id,
                GameCargo.item_id == cargo_item.item_id,
            )
        )
        c = cargo_result.scalar_one_or_none()
        if c:
            c.quantity -= sell_qty
            if c.quantity <= 0.001:
                await session.delete(c)
        await session.commit()

    await ctx.finish_markdown(
        f"💰 **出售成功**\n"
        f"· 卖出 `{sell_qty:.0f} 单位` {cargo_item.item_name} @ `{unit_price:,.0f}` ISK\n"
        f"· 收入：`+{total_income:,.0f} ISK`\n"
        f"· 当前资金：`{player.isk + total_income:,.0f} ISK`"
        + _page_hint("market")
    )


@on_command("buy", aliases=["购买", "买"])
async def handle_buy(ctx):
    """购买物品/装备"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    if not _cooldown_ok(player):
        await ctx.finish("行动冷却中，请稍候片刻")

    args = ctx.args_text.strip().split()
    if not args:
        await ctx.finish("用法：buy 物品名 [数量]，例：buy 轻型导弹 5")

    item_name = args[0]
    quantity = 1.0
    if len(args) > 1:
        try:
            quantity = float(args[1])
        except ValueError:
            await ctx.finish("数量格式错误")

    # 支持购买普通物品（弹药/消耗品）和装备
    item_t = await _get_item_by_name(item_name)
    equip_t = None
    if not item_t:
        equip_t = await _get_equipment_by_name(item_name)
    if not item_t and not equip_t:
        await ctx.finish(f"未找到可购买的物品「{item_name}」。发送 `market` 查看可购物品")

    if item_t:
        buy_id, buy_name, base_price = item_t.id, item_t.name, item_t.base_price
        volume = item_t.volume
    else:
        buy_id, buy_name, base_price = equip_t.id, equip_t.name, equip_t.base_price
        volume = 1.0  # 装备体积按 1 m³

    system = await _get_system(player.system_id)
    mult = _market_price_multiplier(system.security, buy_name, ore=False)
    unit_price = base_price * mult
    total_cost = unit_price * quantity

    ship = await _get_ship(player.ship_id)
    cargo_used = await _cargo_total_volume(player.id)
    add_volume = quantity * volume

    if cargo_used + add_volume > ship.cargo:
        await ctx.finish(f"货舱空间不足（需要 {add_volume:.1f} m³，剩余 {ship.cargo - cargo_used:.1f} m³）")

    if player.isk < total_cost:
        await ctx.finish(
            f"资金不足：需要 `{total_cost:,.0f} ISK`，你只有 `{player.isk:,.0f} ISK`"
        )

    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.user_openid == ctx.user_openid))
        p = result.scalar_one_or_none()
        if p:
            p.isk -= total_cost
            p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()

    await _add_to_cargo(player.id, buy_id, buy_name, quantity)

    await ctx.finish_markdown(
        f"🛒 **购买成功**\n"
        f"· 购买 `{quantity:.0f} × {buy_name}` @ `{unit_price:,.0f}` ISK\n"
        f"· 花费：`-{total_cost:,.0f} ISK`\n"
        f"· 当前资金：`{player.isk - total_cost:,.0f} ISK`\n"
        f"\n"
        f"点击 {_cmd('cargo', '查看货舱')} 装备可用 {_cmd('install', '安装')}"
        + _page_hint("market")
    )


# ==================== Phase 3: PvE 战斗 ====================

def _player_damage(player: GamePlayer, ship: GameShip) -> float:
    """玩家基础伤害 = 舰船火力 + 炮术技能加成"""
    return ship.damage * (1 + _skill_level_of(player, "gunnery") * 0.1)


async def _player_ammo_bonus(player_id: int) -> float:
    """根据货舱中的弹药计算伤害加成（使用轻型/重型导弹）"""
    cargo = await _get_cargo(player_id)
    bonus = 0.0
    for c in cargo:
        if c.item_name == "轻型导弹":
            bonus += c.quantity * 10.0 * 0.1  # 每枚导弹按 10 伤害，10% 生效
        elif c.item_name == "重型导弹":
            bonus += c.quantity * 30.0 * 0.15
    return min(bonus, 100.0)  # 弹药加成上限 100


async def _get_active_combat(player_id: int) -> GameCombat | None:
    async with get_session() as session:
        result = await session.execute(
            select(GameCombat).where(GameCombat.player_id == player_id)
        )
        return result.scalar_one_or_none()


async def _remove_combat(player_id: int) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(GameCombat).where(GameCombat.player_id == player_id)
        )
        combat = result.scalar_one_or_none()
        if combat:
            await session.delete(combat)
            await session.commit()


@on_command("hunt", aliases=["狩猎", "遇敌", "巡逻"])
async def handle_hunt(ctx):
    """在星系中遭遇 NPC 海盗"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    active = await _get_active_combat(player.id)
    if active:
        await ctx.finish(f"你正在与 `{active.npc_name}` 交战中，发送 `fight` 继续战斗")

    if not _cooldown_ok(player):
        await ctx.finish("行动冷却中，请稍候片刻")

    system = await _get_system(player.system_id)
    if not system:
        await ctx.finish("当前位置数据异常")

    # 根据安全等级选 NPC：高安偏向弱 NPC，00 区偏向强 NPC
    async with get_session() as session:
        result = await session.execute(select(GameNpc))
        all_npcs = list(result.scalars().all())

    candidates = [n for n in all_npcs if system.security >= n.min_security]
    if not candidates:
        await ctx.finish("这个星系很平静，没有发现任何海盗")

    # 权重 = 基础稀有度 × 安全等级匹配度
    # 星系越安全，低威胁(低伤害) NPC 越常见；越危险，高威胁 NPC 越常见
    def _npc_weight(npc):
        threat = npc.damage  # 用伤害代表威胁等级
        # 期望威胁：安全等级越高期望威胁越低（0.5 高安 → 期望 15；0.0 00区 → 期望 100）
        expected_threat = 15 + (1 - max(system.security, 0.0)) * 85
        # 权重 = 基础稀有度 × 高斯衰减（越接近期望威胁权重越高）
        spread = max(20.0, expected_threat * 0.4)
        match = max(0.01, 1.0 - abs(threat - expected_threat) / spread)
        return npc.rarity * match

    weights = [_npc_weight(n) for n in candidates]
    npc = random.choices(candidates, weights=weights, k=1)[0]

    # 遭遇概率：安全等级越低遭遇概率越高
    encounter_chance = 0.5 if system.security >= 0.5 else (0.7 if system.security >= 0.1 else 0.9)
    if random.random() > encounter_chance:
        await _update_cooldown(ctx.user_openid)
        await ctx.finish_markdown(
            f"🔭 **侦查结果**\n"
            f"· 在 `{system.name}` 巡逻了一圈，没有发现敌情\n"
            f"· 安全等级：`{system.security:.2f}`（{_sec_label(system.security)}）"
        )

    # 遭遇 NPC，创建战斗
    async with get_session() as session:
        combat = GameCombat(
            player_id=player.id,
            npc_id=npc.id,
            npc_name=npc.name,
            npc_hull=npc.hull,
            npc_damage=npc.damage,
            npc_reward=npc.reward,
            npc_xp_reward=npc.xp_reward,
            turn=1,
        )
        session.add(combat)
        player_result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
        p = player_result.scalar_one_or_none()
        if p:
            p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()

    ship = await _get_ship(player.ship_id)
    danger = "极高" if npc.damage >= ship.damage else ("较高" if npc.damage >= ship.damage * 0.6 else "中等")

    await ctx.finish_markdown(
        f"⚔️ **遭遇敌人！**\n"
        f"· 在 `{system.name}` 遭遇 `{npc.name}`\n"
        f"· 敌舰结构：`{npc.hull:.0f}` / 火力：`{npc.damage:.0f}`\n"
        f"· 威胁程度：`{danger}`\n"
        f"· 击杀奖励：`{npc.reward:,.0f} ISK` + `{npc.xp_reward:.0f}` 经验\n"
        f"\n"
        f"点击 {_cmd('fight', '⚔️ 开火')} 或 {_cmd('flee', '🏃 逃跑')}"
        + _page_hint("combat")
    )


@on_command("fight", aliases=["开火", "攻击", "战斗"])
async def handle_fight(ctx):
    """与当前 NPC 战斗（回合制）"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    if not _cooldown_ok(player):
        await ctx.finish("行动冷却中，请稍候片刻")

    combat = await _get_active_combat(player.id)
    if not combat:
        await ctx.finish("当前没有战斗，发送 `hunt` 巡逻搜索敌人")

    ship = await _get_ship(player.ship_id)
    stats = await _fleet_stats(player, ship)
    ammo_bonus = await _player_ammo_bonus(player.id)

    # 玩家伤害（配船火力 + 弹药加成，含随机波动）
    base_damage = stats["damage"] * (1 + _skill_level_of(player, "gunnery") * 0.1)
    player_dmg = (base_damage + ammo_bonus) * random.uniform(0.8, 1.2)
    player_dmg = max(1.0, player_dmg)

    # NPC 伤害（玩家的损伤控制抗性减伤）
    npc_dmg = combat.npc_damage * random.uniform(0.8, 1.2)
    npc_dmg = max(1.0, npc_dmg * (1 - stats["resist"]))

    # 结算：NPC 先受伤害，再反击
    combat.npc_hull -= player_dmg
    player_result = None

    if combat.npc_hull <= 0:
        # 玩家胜利
        await _remove_combat(player.id)
        async with get_session() as session:
            result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
            p = result.scalar_one_or_none()
            if p:
                p.isk += combat.npc_reward
                p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
            player_result = p

        # 消耗部分弹药
        await _consume_ammo(player.id)

        # 击杀任务进度更新
        mission, mission_completed = await _mission_progress(player.id, "kill", combat.npc_name, 1.0)

        lines = [
            f"🎉 **击杀成功！**",
            f"· `{combat.npc_name}` 已被摧毁",
            f"· 你造成了 `{player_dmg:.0f}` 点伤害",
            f"· 获得：`+{combat.npc_reward:,.0f} ISK`",
        ]
        if mission_completed and mission:
            lines.append(f"✅ **任务完成！** `{mission.name}` 奖励 `+{mission.reward_isk:,.0f} ISK`")
        lines.append(f"· 当前资金：`{player_result.isk:,.0f} ISK`" if player_result else "")
        lines.append(f"\n点击 {_cmd('hunt', '继续巡逻')}")
        lines.append(_page_hint("combat"))
        await ctx.finish_markdown("\n".join(lines))

    # NPC 反击
    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
        p = result.scalar_one_or_none()
        if p:
            # 伤害优先扣护盾，再装甲，最后结构
            shield_dmg = min(p.shield, npc_dmg)
            p.shield -= shield_dmg
            remain = npc_dmg - shield_dmg
            if remain > 0:
                armor_dmg = min(p.armor, remain)
                p.armor -= armor_dmg
                remain -= armor_dmg
                if remain > 0:
                    p.hull -= remain
            p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        player_after = p

    # 保存战斗剩余血量
    async with get_session() as session:
        result = await session.execute(select(GameCombat).where(GameCombat.player_id == player.id))
        c = result.scalar_one_or_none()
        if c:
            c.npc_hull = max(0.1, combat.npc_hull)
            c.turn += 1
            await session.commit()

    if player_after and player_after.hull <= 0:
        # 玩家被击毁
        await _remove_combat(player.id)
        async with get_session() as session:
            result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
            p = result.scalar_one_or_none()
            if p:
                # 掉落 20% 资金，护盾装甲恢复，结构回 50%，回到 Jita
                lost = p.isk * 0.2
                p.isk -= lost
                p.system_id = START_SYSTEM_ID
                p.shield = 100.0
                p.armor = 100.0
                p.hull = 100.0
                p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
            lost_amt = lost if p else 0
        await ctx.finish_markdown(
            f"💥 **舰船被击毁！**\n"
            f"· `{combat.npc_name}` 的致命一击打穿了你的护盾\n"
            f"· 逃生舱弹射，丢失 `{lost_amt:,.0f} ISK`（20%）\n"
            f"· 已被送回 `Jita` 空间站\n"
            f"\n"
            f"发送 `status` 查看状态"
        )

    shield_txt = f"{player_after.shield:.0f}" if player_after else "0"
    armor_txt = f"{player_after.armor:.0f}" if player_after else "0"
    hull_txt = f"{player_after.hull:.0f}" if player_after else "0"

    await ctx.finish_markdown(
        f"⚔️ **战斗回合 {combat.turn}**\n"
        f"· 你对 `{combat.npc_name}` 造成 `{player_dmg:.0f}` 伤害（剩余 `{max(0.1, combat.npc_hull):.0f}`）\n"
        f"· `{combat.npc_name}` 反击造成 `{npc_dmg:.0f}` 伤害\n"
        f"· 你的护盾：`{shield_txt}` / 装甲：`{armor_txt}` / 结构：`{hull_txt}`\n"
        f"\n"
        f"点击 {_cmd('fight', '⚔️ 继续开火')} 或 {_cmd('flee', '🏃 逃跑')}"
        + _page_hint("combat")
    )


async def _consume_ammo(player_id: int) -> None:
    """战斗后消耗弹药（每场消耗 1 枚轻型/重型导弹）"""
    async with get_session() as session:
        result = await session.execute(
            select(GameCargo).where(
                GameCargo.player_id == player_id,
                GameCargo.item_id.in_([0, 1]),  # 轻型=0, 重型=1
            )
        )
        ammo_items = result.scalars().all()
        for a in ammo_items:
            if a.item_name in ("轻型导弹", "重型导弹") and a.quantity > 0:
                a.quantity -= 1
                if a.quantity <= 0.001:
                    await session.delete(a)
                break
        await session.commit()


@on_command("flee", aliases=["逃跑", "撤退"])
async def handle_flee(ctx):
    """逃跑（有概率失败受反击）"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    combat = await _get_active_combat(player.id)
    if not combat:
        await ctx.finish("当前没有战斗")

    if not _cooldown_ok(player):
        await ctx.finish("行动冷却中，请稍候片刻")

    # 逃跑成功率：基础 60% + 推进器加成（配船效果），上限 90%
    ship = await _get_ship(player.ship_id)
    stats = await _fleet_stats(player, ship)
    escape_chance = stats["escape"]
    if random.random() < escape_chance:
        await _remove_combat(player.id)
        async with get_session() as session:
            result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
            p = result.scalar_one_or_none()
            if p:
                p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
        await ctx.finish_markdown(
            f"🏃 **成功逃跑**\n"
            f"· 你脱离了与 `{combat.npc_name}` 的战斗\n"
            f"· 安全返回当前星系"
        )

    # 逃跑失败，受反击
    npc_dmg = combat.npc_damage * random.uniform(0.8, 1.2)
    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
        p = result.scalar_one_or_none()
        if p:
            shield_dmg = min(p.shield, npc_dmg)
            p.shield -= shield_dmg
            remain = npc_dmg - shield_dmg
            if remain > 0:
                armor_dmg = min(p.armor, remain)
                p.armor -= armor_dmg
                remain -= armor_dmg
                if remain > 0:
                    p.hull -= remain
            p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        after = p

    if after and after.hull <= 0:
        await _remove_combat(player.id)
        async with get_session() as session:
            result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
            q = result.scalar_one_or_none()
            if q:
                lost = q.isk * 0.2
                q.isk -= lost
                q.system_id = START_SYSTEM_ID
                q.shield = 100.0
                q.armor = 100.0
                q.hull = 100.0
                q.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
            lost_amt = lost if q else 0
        await ctx.finish_markdown(
            f"💥 **逃跑失败，舰船被击毁！**\n"
            f"· 丢失 `{lost_amt:,.0f} ISK`（20%）\n"
            f"· 已被送回 `Jita` 空间站"
        )

    await ctx.finish_markdown(
        f"🏃 **逃跑失败！**\n"
        f"· `{combat.npc_name}` 对你造成 `{npc_dmg:.0f}` 伤害\n"
        f"· 你的护盾：`{after.shield:.0f}` / 装甲：`{after.armor:.0f}` / 结构：`{after.hull:.0f}`\n"
        f"\n"
        f"发送 `fight` 继续战斗，或再试 `flee`"
    )


# ==================== Phase 4: PvP 玩家对战 ====================

async def _pvp_player_power(player: GamePlayer, ship: GameShip) -> float:
    """PvP 战斗力 = 配船火力 × 配船血量系数 × 炮术技能"""
    stats = await _fleet_stats(player, ship)
    total_hp = stats["hull"] + stats["armor"] + stats["shield"]
    hp_factor = total_hp / 100.0
    return stats["damage"] * (1 + _skill_level_of(player, "gunnery") * 0.1) * hp_factor * 0.01


@on_command("attack", aliases=["攻击", "宣战", "pvp"])
async def handle_attack(ctx):
    """攻击同星系的玩家（PvP）"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    if not _cooldown_ok(player):
        await ctx.finish("行动冷却中，请稍候片刻")

    # 解析被攻击目标
    mentioned = None
    if ctx.event.mentions:
        for m in ctx.event.mentions:
            uid = getattr(m, "member_openid", None) or getattr(m, "id", None)
            if uid:
                mentioned = str(uid)
                break
    if not mentioned:
        # 尝试从参数解析（攻击 玩家名）
        target_name = ctx.args_text.strip()
        if not target_name:
            await ctx.finish("用法：attack @玩家，例：attack @某人")

        async with get_session() as session:
            result = await session.execute(
                select(GamePlayer).where(func.lower(GamePlayer.name) == target_name.lower())
            )
            target = result.scalar_one_or_none()
    else:
        async with get_session() as session:
            result = await session.execute(
                select(GamePlayer).where(GamePlayer.user_openid == mentioned)
            )
            target = result.scalar_one_or_none()

    if not target:
        await ctx.finish("未找到目标玩家")
    if target.user_openid == ctx.user_openid:
        await ctx.finish("不能攻击自己！")

    # 目标必须在同一星系
    if target.system_id != player.system_id:
        await ctx.finish(f"`{target.name}` 不在当前星系，无法攻击")

    system = await _get_system(player.system_id)

    # 高安（>=0.5）攻击 → CONCORD 惩罚
    if system.security >= 0.5:
        await _concord_punish(ctx, player, target, system)
        return

    # 低安/00 区：正常 PvP 结算
    await _pvp_resolve(ctx, player, target, system)


async def _concord_punish(ctx, attacker: GamePlayer, defender: GamePlayer, system: GameSystem):
    """高安攻击触发 CONCORD：攻击者舰船被摧毁，掉落 30% 资金"""
    lost = attacker.isk * 0.3
    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.id == attacker.id))
        a = result.scalar_one_or_none()
        if a:
            a.isk -= lost
            a.system_id = START_SYSTEM_ID
            a.shield = 100.0
            a.armor = 100.0
            a.hull = 100.0
            a.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)

        result2 = await session.execute(select(GamePlayer).where(GamePlayer.id == defender.id))
        d = result2.scalar_one_or_none()
        if d:
            d.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)

        log = GameBattleLog(
            attacker_openid=attacker.user_openid,
            defender_openid=defender.user_openid,
            attacker_name=attacker.name,
            defender_name=defender.name,
            system_id=system.id,
            system_name=system.name,
            security=system.security,
            winner_openid="",  # 无人获胜（CONCORD）
            concord=True,
            attacker_isk_dropped=lost,
            defender_isk_dropped=0.0,
            log_text=(
                f"{attacker.name} 在 {system.name}（高安 {system.security:.2f}）攻击 {defender.name}，"
                f"被 CONCORD 执法队击毁，掉落 {lost:,.0f} ISK"
            ),
        )
        session.add(log)
        await session.commit()

    await ctx.finish_markdown(
        f"🚨 **CONCORD 执法！**\n"
        f"· 你竟敢在高安 `{system.name}`（{system.security:.2f}）攻击 `{defender.name}`\n"
        f"· CONCORD 执法队瞬间将你摧毁！\n"
        f"· 你掉落了 `{lost:,.0f} ISK`（30%）\n"
        f"· 已被送回 `Jita` 空间站\n"
        f"\n"
        f"💡 高安区受警察保护，去低安或 00 区才能 PvP"
    )


async def _pvp_resolve(ctx, attacker: GamePlayer, defender: GamePlayer, system: GameSystem):
    """低安/00区 PvP 自动结算"""
    atk_ship = await _get_ship(attacker.ship_id)
    def_ship = await _get_ship(defender.ship_id)

    atk_power = (await _pvp_player_power(attacker, atk_ship)) * random.uniform(0.8, 1.2)
    def_power = (await _pvp_player_power(defender, def_ship)) * random.uniform(0.8, 1.2)

    # 胜者
    attacker_wins = atk_power >= def_power
    winner = attacker if attacker_wins else defender
    loser = defender if attacker_wins else attacker

    # 掉落 20% 资金
    loser_drop = loser.isk * 0.2

    async with get_session() as session:
        # 更新双方状态
        result = await session.execute(select(GamePlayer).where(GamePlayer.id == winner.id))
        w = result.scalar_one_or_none()
        if w:
            w.isk += loser_drop
            w.unallocated_xp += XP_PVP_WIN  # PvP 胜利经验进经验池
            w.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)

        result2 = await session.execute(select(GamePlayer).where(GamePlayer.id == loser.id))
        l = result2.scalar_one_or_none()
        if l:
            l.isk -= loser_drop
            l.system_id = START_SYSTEM_ID
            l.shield = 100.0
            l.armor = 100.0
            l.hull = 100.0
            l.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)

        # 记录战报
        log_lines = [
            f"⚔️ {attacker.name} 与 {defender.name} 在 {system.name}（{system.security:.2f}）发生 PvP",
            f"· 双方战力：{attacker.name} {atk_power:.1f} vs {defender.name} {def_power:.1f}",
            f"· 胜者：{winner.name}",
            f"· {loser.name} 被击毁，掉落 {loser_drop:,.0f} ISK",
        ]
        log = GameBattleLog(
            attacker_openid=attacker.user_openid,
            defender_openid=defender.user_openid,
            attacker_name=attacker.name,
            defender_name=defender.name,
            system_id=system.id,
            system_name=system.name,
            security=system.security,
            winner_openid=winner.user_openid,
            concord=False,
            attacker_isk_dropped=loser_drop if attacker == loser else 0.0,
            defender_isk_dropped=loser_drop if defender == loser else 0.0,
            log_text="\n".join(log_lines),
        )
        session.add(log)
        await session.commit()

    result_lines = [
        f"⚔️ **PvP 战斗结束**",
        f"· 地点：`{system.name}`（{_sec_label(system.security)}）",
        f"· `{attacker.name}` vs `{defender.name}`",
        f"· 胜者：`{winner.name}`",
    ]
    if attacker_wins:
        result_lines.append(f"· 🏆 你赢得了战斗！获得 `{loser_drop:,.0f} ISK` 战利品")
        result_lines.append(f"· 目标已送回 Jita")
    else:
        result_lines.append(f"· 💥 你被 `{defender.name}` 击毁了！")
        result_lines.append(f"· 你掉落了 `{loser_drop:,.0f} ISK`（20%），已被送回 Jita")
    result_lines.append(f"\n发送 `report` 查看你的战报")

    await ctx.finish_markdown("\n".join(result_lines))


@on_command("report", aliases=["战报", "我的战报", "pvp战报"])
async def handle_report(ctx):
    """查看最近的 PvP 战报"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    async with get_session() as session:
        result = await session.execute(
            select(GameBattleLog)
            .where(
                (GameBattleLog.attacker_openid == ctx.user_openid)
                | (GameBattleLog.defender_openid == ctx.user_openid)
            )
            .order_by(GameBattleLog.id.desc())
            .limit(5)
        )
        logs = list(result.scalars().all())

    if not logs:
        await ctx.finish("你还没有参与过任何 PvP 战斗")

    lines = ["📜 **最近的 PvP 战报**"]
    for log in logs:
        marker = "🏆" if log.winner_openid == ctx.user_openid else ("💥" if not log.concord else "🚨")
        lines.append(
            f"{marker} `{log.attacker_name}` vs `{log.defender_name}` @ {log.system_name}\n"
            f"  · {log.log_text.split(chr(10))[-1]}"
        )

    await ctx.finish_markdown("\n".join(lines))


# ==================== Phase 5: 任务 / 升级 / 维修 ====================

async def _get_player_mission(player_id: int) -> GamePlayerMission | None:
    async with get_session() as session:
        result = await session.execute(
            select(GamePlayerMission).where(
                GamePlayerMission.player_id == player_id,
                GamePlayerMission.completed == False,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()


async def _get_mission(mission_id: int) -> GameMission | None:
    async with get_session() as session:
        result = await session.execute(select(GameMission).where(GameMission.id == mission_id))
        return result.scalar_one_or_none()


async def _accept_mission(player_id: int) -> GameMission | None:
    """接取一个可用的任务（按玩家技能等级过滤）"""
    player = None
    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.id == player_id))
        player = result.scalar_one_or_none()

    async with get_session() as session:
        result = await session.execute(select(GameMission))
        all_missions = list(result.scalars().all())

    # 过滤技能等级够的任务（按最高技能等级）
    eligible = [m for m in all_missions if player and _max_skill(player) >= m.min_skill]
    if not eligible:
        return None

    mission = random.choice(eligible)
    session_pm = GamePlayerMission(player_id=player_id, mission_id=mission.id)
    async with get_session() as session:
        session.add(session_pm)
        await session.commit()
    return mission


async def _mission_progress(player_id: int, mission_type: str, target_item: str, amount: float = 1.0):
    """更新任务进度；完成后自动结算。返回 (mission, completed)"""
    pm = await _get_player_mission(player_id)
    if not pm:
        return None, False
    mission = await _get_mission(pm.mission_id)
    if not mission or mission.mission_type != mission_type:
        return None, False
    if mission.target_item != target_item:
        return None, False

    async with get_session() as session:
        result = await session.execute(select(GamePlayerMission).where(GamePlayerMission.id == pm.id))
        p = result.scalar_one_or_none()
        if p:
            p.progress += amount
            await session.commit()

    completed = p.progress >= mission.target_amount
    if completed:
        # 结算奖励
        async with get_session() as session:
            result = await session.execute(select(GamePlayerMission).where(GamePlayerMission.id == pm.id))
            p2 = result.scalar_one_or_none()
            if p2:
                p2.completed = True
                from datetime import datetime as _dt
                p2.completed_at = _dt.now()
            result2 = await session.execute(select(GamePlayer).where(GamePlayer.id == player_id))
            pl = result2.scalar_one_or_none()
            if pl:
                pl.isk += mission.reward_isk
                pl.unallocated_xp += mission.reward_xp  # 任务奖励经验进经验池
            await session.commit()
    return mission, completed


@on_command("mission", aliases=["任务", "接任务"])
async def handle_mission(ctx):
    """查看/接取任务"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    arg = ctx.args_text.strip().lower()
    if arg in ("new", "接", "接取", "刷新"):
        # 检查是否已有进行中的任务
        active = await _get_player_mission(player.id)
        if active:
            mission = await _get_mission(active.mission_id)
            await ctx.finish(
                f"你还有进行中的任务：`{mission.name}`\n"
                f"进度：`{active.progress:.0f}/{mission.target_amount:.0f}`"
            )
        mission = await _accept_mission(player.id)
        if not mission:
            await ctx.finish("暂时没有适合你技能等级的任务，先去升级吧")
        await ctx.finish_markdown(
            f"📋 **接受任务**\n"
            f"· 任务：`{mission.name}`\n"
            f"· 目标：{mission.description}\n"
            f"· 奖励：`{mission.reward_isk:,.0f} ISK` + `{mission.reward_xp:.0f}` 经验\n"
            f"\n"
            f"发送 `mission` 查看进度"
            + _page_hint("mission")
        )

    active = await _get_player_mission(player.id)
    if not active:
        await ctx.finish_markdown(
            f"📋 **任务大厅**\n"
            f"· 当前没有进行中的任务\n"
            f"· 点击 {_cmd('mission new', '接取新任务')} 领取任务"
            + _page_hint("mission")
        )
    mission = await _get_mission(active.mission_id)
    if not mission:
        await ctx.finish("任务数据异常")

    await ctx.finish_markdown(
        f"📋 **当前任务**\n"
        f"· 任务：`{mission.name}`\n"
        f"· 目标：{mission.description}\n"
        f"· 进度：`{active.progress:.0f}/{mission.target_amount:.0f}`\n"
        f"· 奖励：`{mission.reward_isk:,.0f} ISK` + `{mission.reward_xp:.0f}` 经验\n"
        f"\n"
        f"💡 完成采矿/击杀/送货后自动结算"
        + _page_hint("mission")
    )


@on_command("upgrade", aliases=["换船", "升级舰船", "买船", "购船"])
async def handle_upgrade(ctx):
    """购买舰船（入仓，不覆盖当前驾驶的船）"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    async with get_session() as session:
        result = await session.execute(select(GameShip))
        ships = list(result.scalars().all())

    # 玩家已拥有的船
    owned_ships = await _get_player_ships(player.id)
    owned_ids = {ps.ship_id for ps in owned_ships}

    ship_name = ctx.args_text.strip()

    lines = [f"🛸 **舰船商店**\n"]
    ship_skill = _skill_level_of(player, "ship")
    for s in ships:
        can_buy = player.isk >= s.cost and ship_skill >= s.min_skill
        if s.id in owned_ids:
            mark = "（当前）" if s.id == player.ship_id else "（已拥有）"
        else:
            mark = ""
        lines.append(
            f"· `{s.name}`{mark}\n"
            f"  结构{s.hull:.0f}/装甲{s.armor:.0f}/护盾{s.shield:.0f} 火力{s.damage:.0f} 货舱{s.cargo:.0f}\n"
            f"  价格：`{s.cost:,.0f} ISK`，需求技能：{s.min_skill}"
        )

    lines.append("\n用法：upgrade 舰船名（购买后入仓，发送 `switch 船名` 更换驾驶）")
    lines.append("`fleet` 查看拥有的舰船")
    if not ship_name:
        await ctx.finish_markdown("\n".join(lines))

    target = None
    for s in ships:
        if s.name.lower() == ship_name.lower():
            target = s
            break
    if not target:
        await ctx.finish(f"未找到舰船「{ship_name}」")

    if target.id in owned_ids:
        await ctx.finish(f"你已经拥有 `{target.name}` 了，发送 `switch {target.name}` 更换驾驶")

    if player.isk < target.cost:
        await ctx.finish(
            f"资金不足：`{target.name}` 需要 `{target.cost:,.0f} ISK`，你只有 `{player.isk:,.0f} ISK`"
        )
    ship_skill = _skill_level_of(player, "ship")
    if ship_skill < target.min_skill:
        await ctx.finish(
            f"舰船操控等级不足：驾驶 `{target.name}` 需要 {target.min_skill} 级，"
            f"你只有 {ship_skill} 级。发送 `train 舰船操控` 提升"
        )

    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
        p = result.scalar_one_or_none()
        if p:
            p.isk -= target.cost
        session.add(
            GamePlayerShip(
                player_id=player.id,
                ship_id=target.id,
                hull=target.hull,
                armor=target.armor,
                shield=target.shield,
            )
        )
        await session.commit()

    await ctx.finish_markdown(
        f"🛸 **购买成功（已入仓）**\n"
        f"· `{target.name}` 已加入你的舰队\n"
        f"· 花费：`-{target.cost:,.0f} ISK`\n"
        f"· 当前资金：`{player.isk - target.cost:,.0f} ISK`\n"
        f"\n"
        f"发送 `switch {target.name}` 更换驾驶，`fleet` 查看舰队"
        + _page_hint("fleet")
    )


@on_command("fleet", aliases=["舰队", "我的舰队", "船坞", "仓库"])
async def handle_fleet(ctx):
    """查看自己拥有的所有舰船"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    owned_ships = await _get_player_ships(player.id)
    if not owned_ships:
        await ctx.finish("你没有舰船，发送 `start` 获得初始 Kestrel 或 `upgrade` 购买")

    lines = [f"🚀 **我的舰队**\n"]
    for ps in owned_ships:
        ship = await _get_ship(ps.ship_id)
        if not ship:
            continue
        mark = "（当前驾驶）" if ps.ship_id == player.ship_id else ""
        installed = await _get_installed(player.id, ps.ship_id)
        equip_names = []
        for inst in installed:
            eq = await _get_equipment_by_id(inst.equipment_id)
            if eq:
                equip_names.append(eq.name)
        equip_txt = f" 装备：{len(equip_names)} 件" if equip_names else ""
        lines.append(
            f"· `{ship.name}`{mark} {equip_txt}\n"
            f"  结构{ps.hull:.0f}/{ship.hull:.0f} 装甲{ps.armor:.0f}/{ship.armor:.0f} 护盾{ps.shield:.0f}/{ship.shield:.0f}"
        )

    lines.append("\n💡 发送 `switch 船名` 更换驾驶")
    lines.append(_page_hint("fleet"))
    await ctx.finish_markdown("\n".join(lines))


@on_command("switch", aliases=["换船", "驾驶", "切换"])
async def handle_switch(ctx):
    """在拥有的舰船之间切换驾驶（装备绑定原船，切换后新船配装独立）"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    ship_name = ctx.args_text.strip()
    if not ship_name:
        await ctx.finish("用法：switch 船名，例：switch Vexor。发送 `fleet` 查看舰队")

    owned_ships = await _get_player_ships(player.id)
    target = None
    for ps in owned_ships:
        ship = await _get_ship(ps.ship_id)
        if ship and ship.name.lower() == ship_name.lower():
            target = (ps, ship)
            break
    if not target:
        await ctx.finish(f"你没有「{ship_name}」，发送 `fleet` 查看舰队或 `upgrade` 购买")

    target_ps, target_ship = target
    if target_ship.id == player.ship_id:
        await ctx.finish(f"你已经在驾驶 `{target_ship.name}` 了")

    async with get_session() as session:
        # 保存当前船血量
        result = await session.execute(
            select(GamePlayerShip).where(
                GamePlayerShip.player_id == player.id,
                GamePlayerShip.ship_id == player.ship_id,
            )
        )
        cur_ps = result.scalar_one_or_none()
        if cur_ps:
            cur_ps.hull = player.hull
            cur_ps.armor = player.armor
            cur_ps.shield = player.shield

        # 切换
        p_result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
        p = p_result.scalar_one_or_none()
        if p:
            p.ship_id = target_ship.id
            p.hull = target_ps.hull
            p.armor = target_ps.armor
            p.shield = target_ps.shield
        await session.commit()

    # 新船已安装装备（若有）
    new_installed = await _get_installed(player.id, target_ship.id)
    equip_names = []
    for inst in new_installed:
        eq = await _get_equipment_by_id(inst.equipment_id)
        if eq:
            equip_names.append(eq.name)

    equip_txt = f" 已装装备：{'、'.join(equip_names)}" if equip_names else "（无装备）"
    await ctx.finish_markdown(
        f"🚀 **切换驾驶完成**\n"
        f"· 已切换到 `{target_ship.name}`\n"
        f"· 结构/装甲/护盾：`{target_ps.hull:.0f}/{target_ps.armor:.0f}/{target_ps.shield:.0f}`\n"
        f"· 配装：{equip_txt}\n"
        f"\n"
        f"发送 `fittings` 查看配装，`install` 安装装备"
        + _page_hint("fleet")
    )


@on_command("repair", aliases=["维修", "修复"])
async def handle_repair(ctx):
    """维修舰船（用 ISK 恢复护盾/装甲/结构到配船后的上限）"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    ship = await _get_ship(player.ship_id)
    if not ship:
        await ctx.finish("舰船数据异常")

    stats = await _fleet_stats(player, ship)

    # 计算损伤（配船后上限）
    max_hp = stats["hull"] + stats["armor"] + stats["shield"]
    cur_hp = player.hull + player.armor + player.shield
    damage = max(0.0, max_hp - cur_hp)
    if damage <= 0:
        await ctx.finish("你的舰船状态完好，无需维修")

    # 维修费用：每点损伤 5 ISK
    cost = damage * 5.0
    if player.isk < cost:
        await ctx.finish(
            f"资金不足：维修需要 `{cost:,.0f} ISK`，你只有 `{player.isk:,.0f} ISK`"
        )

    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
        p = result.scalar_one_or_none()
        if p:
            p.isk -= cost
            p.hull = stats["hull"]
            p.armor = stats["armor"]
            p.shield = stats["shield"]
            await session.commit()

    await ctx.finish_markdown(
        f"🔧 **维修完成**\n"
        f"· 恢复了 `{damage:.0f}` 点损伤\n"
        f"· 花费：`-{cost:,.0f} ISK`\n"
        f"· 护盾/装甲/结构已完全修复\n"
        f"· 当前资金：`{player.isk - cost:,.0f} ISK`"
    )


# ==================== Phase 6: 配船系统（参考 EVE 槽位） ====================

async def _get_equipment_by_name(name: str) -> GameEquipment | None:
    async with get_session() as session:
        result = await session.execute(
            select(GameEquipment).where(func.lower(GameEquipment.name) == name.lower())
        )
        return result.scalar_one_or_none()


async def _get_installed(player_id: int, ship_id: int | None = None) -> list[GameInstalled]:
    """获取玩家在当前舰船上已安装的装备（装备绑定舰船）"""
    async with get_session() as session:
        stmt = select(GameInstalled).where(GameInstalled.player_id == player_id)
        if ship_id is not None:
            stmt = stmt.where(GameInstalled.ship_id == ship_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def _get_player_ships(player_id: int) -> list[GamePlayerShip]:
    """获取玩家拥有的所有舰船"""
    async with get_session() as session:
        result = await session.execute(
            select(GamePlayerShip).where(GamePlayerShip.player_id == player_id)
        )
        return list(result.scalars().all())


async def _save_current_ship_hp(player: GamePlayer) -> None:
    """保存当前驾驶舰船的血量到仓库记录"""
    async with get_session() as session:
        result = await session.execute(
            select(GamePlayerShip).where(
                GamePlayerShip.player_id == player.id,
                GamePlayerShip.ship_id == player.ship_id,
            )
        )
        ps = result.scalar_one_or_none()
        if ps:
            ps.hull = player.hull
            ps.armor = player.armor
            ps.shield = player.shield
            await session.commit()


async def _load_ship_hp(player: GamePlayer, ship_id: int, ship: GameShip) -> None:
    """切换舰船时，把玩家血量设为目标船的血量（优先仓库记录，否则满血）"""
    async with get_session() as session:
        result = await session.execute(
            select(GamePlayerShip).where(
                GamePlayerShip.player_id == player.id,
                GamePlayerShip.ship_id == ship_id,
            )
        )
        ps = result.scalar_one_or_none()
        if ps:
            player.hull = ps.hull
            player.armor = ps.armor
            player.shield = ps.shield
        else:
            player.hull = ship.hull
            player.armor = ship.armor
            player.shield = ship.shield


async def _fleet_stats(player: GamePlayer, ship: GameShip) -> dict:
    """计算配船后的综合属性（装备效果汇总）"""
    installed = await _get_installed(player.id, player.ship_id)
    equipments = []
    for inst in installed:
        eq = await _get_equipment_by_id(inst.equipment_id)
        if eq:
            equipments.append(eq)

    total_damage = ship.damage
    total_shield = ship.shield
    total_armor = ship.armor
    total_hull = ship.hull
    escape_bonus = 0.0
    resist = 0.0
    cpu_used = 0.0
    pg_used = 0.0

    for eq in equipments:
        cpu_used += eq.cpu_use
        pg_used += eq.pg_use
        if eq.effect_type == "damage":
            total_damage += eq.effect_value
        elif eq.effect_type == "shield":
            total_shield += eq.effect_value
        elif eq.effect_type == "armor":
            total_armor += eq.effect_value
        elif eq.effect_type == "hull":
            total_hull += eq.effect_value
        elif eq.effect_type == "escape":
            escape_bonus += eq.effect_value
        elif eq.effect_type == "resist":
            resist += eq.effect_value

    # 技能加成：炮术→伤害、护盾→护盾量、装甲→装甲量（每级 +5%）
    total_damage *= (1 + _skill_level_of(player, "gunnery") * 0.05)
    total_shield *= (1 + _skill_level_of(player, "shield") * 0.05)
    total_armor *= (1 + _skill_level_of(player, "armor") * 0.05)

    return {
        "damage": total_damage,
        "shield": total_shield,
        "armor": total_armor,
        "hull": total_hull,
        "escape": min(0.9, 0.6 + escape_bonus),  # 逃跑基础 60% + 推进加成，上限 90%
        "resist": min(0.5, resist),  # 抗性上限 50%
        "cpu_used": cpu_used,
        "pg_used": pg_used,
        "equipments": equipments,
    }


async def _get_equipment_by_id(equipment_id: int) -> GameEquipment | None:
    async with get_session() as session:
        result = await session.execute(
            select(GameEquipment).where(GameEquipment.id == equipment_id)
        )
        return result.scalar_one_or_none()


@on_command("fittings", aliases=["配装", "装备", "我的配装"])
async def handle_fittings(ctx):
    """查看当前配装"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    ship = await _get_ship(player.ship_id)
    stats = await _fleet_stats(player, ship)
    installed = await _get_installed(player.id, player.ship_id)

    # 按槽位分组
    by_slot = {"high": [], "medium": [], "low": []}
    for inst in installed:
        eq = await _get_equipment_by_id(inst.equipment_id)
        if eq:
            by_slot[eq.slot_type].append(eq)

    slot_labels = {"high": "高能量槽", "medium": "中能量槽", "low": "低能量槽"}

    lines = [
        f"🛸 **配装：{ship.name}**",
        f"· 综合火力：`{stats['damage']:.0f}`",
        f"· 护盾：`{stats['shield']:.0f}` / 装甲：`{stats['armor']:.0f}` / 结构：`{stats['hull']:.0f}`",
        f"· 抗性：`{stats['resist']*100:.0f}%` 逃跑率：`{stats['escape']*100:.0f}%`",
        f"· CPU：`{stats['cpu_used']:.0f}/{ship.cpu:.0f}` 能量栅格：`{stats['pg_used']:.0f}/{ship.powergrid:.0f}`",
        f"",
    ]

    for slot_key, label in slot_labels.items():
        max_slots = {
            "high": ship.high_slots,
            "medium": ship.medium_slots,
            "low": ship.low_slots,
        }[slot_key]
        eqs = by_slot[slot_key]
        lines.append(f"**{label}（{len(eqs)}/{max_slots}）**")
        if eqs:
            for eq in eqs:
                lines.append(f"· `{eq.name}`（CPU {eq.cpu_use:.0f}/栅格 {eq.pg_use:.0f}）")
        else:
            lines.append("· 空")
        lines.append("")

    lines.append("💡 用法：`install 装备名` 安装 / `uninstall 装备名` 卸下")
    lines.append("`buy 装备名` 可从市场购买装备")

    lines.append(_page_hint("fleet"))
    await ctx.finish_markdown("\n".join(lines))


@on_command("install", aliases=["安装", "装配"])
async def handle_install(ctx):
    """安装装备（受槽位 + CPU/能量栅格限制）"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    if not _cooldown_ok(player):
        await ctx.finish("行动冷却中，请稍候片刻")

    equip_name = ctx.args_text.strip()
    if not equip_name:
        await ctx.finish("用法：install 装备名，例：install 轻型导弹发射器")

    # 装备必须在货舱中
    cargo = await _get_cargo(player.id)
    cargo_item = next((c for c in cargo if c.item_name.lower() == equip_name.lower()), None)
    if not cargo_item:
        await ctx.finish(f"货舱中没有「{equip_name}」，先用 `buy` 购买")

    eq = await _get_equipment_by_name(equip_name)
    if not eq:
        await ctx.finish(f"「{equip_name}」不是可安装的装备")

    # 技能等级要求（按装备效果类型推断所需技能）
    eq_skill = EQUIP_SKILL_BY_EFFECT.get(eq.effect_type) or eq.skill_type or "gunnery"
    skill_level = _skill_level_of(player, eq_skill)
    skill_name = SKILLS.get(eq_skill, ("未知", ""))[0]
    if skill_level < eq.min_skill:
        await ctx.finish(
            f"技能不足：`{eq.name}` 需要「{skill_name}」{eq.min_skill} 级，"
            f"你只有 {skill_level} 级。发送 `train {skill_name}` 提升"
        )

    ship = await _get_ship(player.ship_id)
    installed = await _get_installed(player.id, player.ship_id)

    # 检查槽位
    slot_capacity = {
        "high": ship.high_slots,
        "medium": ship.medium_slots,
        "low": ship.low_slots,
    }
    used_in_slot = 0
    for i in installed:
        if await _slot_of(i.equipment_id) == eq.slot_type:
            used_in_slot += 1
    if used_in_slot >= slot_capacity[eq.slot_type]:
        await ctx.finish(f"{eq.slot_type}槽位已满（{slot_capacity[eq.slot_type]}/{slot_capacity[eq.slot_type]}）")

    # 检查 CPU/能量栅格
    stats = await _fleet_stats(player, ship)
    if stats["cpu_used"] + eq.cpu_use > ship.cpu:
        await ctx.finish(f"CPU 不足：需要 `{eq.cpu_use:.0f}`，剩余 `{ship.cpu - stats['cpu_used']:.0f}`")
    if stats["pg_used"] + eq.pg_use > ship.powergrid:
        await ctx.finish(f"能量栅格不足：需要 `{eq.pg_use:.0f}`，剩余 `{ship.powergrid - stats['pg_used']:.0f}`")

    # 安装：扣货舱，加装备，并把对应防御值补到新上限
    async with get_session() as session:
        result = await session.execute(
            select(GameCargo).where(
                GameCargo.player_id == player.id,
                GameCargo.item_id == eq.id,
            )
        )
        c = result.scalar_one_or_none()
        if c:
            c.quantity -= 1
            if c.quantity <= 0.001:
                await session.delete(c)
        session.add(GameInstalled(player_id=player.id, ship_id=player.ship_id, equipment_id=eq.id))

        # 补满对应防御到配船后新上限
        new_stats = await _fleet_stats(player, ship)
        p_result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
        p = p_result.scalar_one_or_none()
        if p:
            if eq.effect_type == "shield":
                p.shield = new_stats["shield"]
            elif eq.effect_type == "armor":
                p.armor = new_stats["armor"]
            elif eq.effect_type == "hull":
                p.hull = new_stats["hull"]
            p.last_action_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()

    await ctx.finish_markdown(
        f"🔩 **安装完成**\n"
        f"· 已安装 `{eq.name}` 到{ {'high':'高','medium':'中','low':'低'}[eq.slot_type] }槽\n"
        f"· 发送 `fittings` 查看配装效果"
    )


async def _slot_of(equipment_id: int) -> str:
    eq = await _get_equipment_by_id(equipment_id)
    return eq.slot_type if eq else ""


@on_command("uninstall", aliases=["卸下", "拆卸", "拆除"])
async def handle_uninstall(ctx):
    """卸下装备（回到货舱）"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    if not _cooldown_ok(player):
        await ctx.finish("行动冷却中，请稍候片刻")

    equip_name = ctx.args_text.strip()
    if not equip_name:
        await ctx.finish("用法：uninstall 装备名")

    installed = await _get_installed(player.id, player.ship_id)
    target_inst = None
    target_eq = None
    for inst in installed:
        eq = await _get_equipment_by_id(inst.equipment_id)
        if eq and eq.name.lower() == equip_name.lower():
            target_inst = inst
            target_eq = eq
            break

    if not target_inst:
        await ctx.finish(f"未安装「{equip_name}」，发送 `fittings` 查看配装")

    async with get_session() as session:
        result = await session.execute(select(GameInstalled).where(GameInstalled.id == target_inst.id))
        inst = result.scalar_one_or_none()
        if inst:
            await session.delete(inst)
        session.add(GameCargo(player_id=player.id, item_id=target_eq.id, item_name=target_eq.name, quantity=1))
        await session.commit()

    await ctx.finish_markdown(
        f"🔧 **卸下完成**\n"
        f"· 已卸下 `{target_eq.name}`，放回货舱"
    )


# ==================== 技能指令 ====================

@on_command("skills", aliases=["技能", "我的技能"])
async def handle_skills(ctx):
    """查看技能与经验池"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    await _ensure_xp(player)

    lines = [
        f"📚 **技能与经验**",
        f"· 经验池：`{player.unallocated_xp:.0f} XP`（随时间增长，发送 `train 技能名` 分配）",
        f"",
    ]
    for key, (name, desc) in SKILLS.items():
        level = _skill_level_of(player, key)
        next_cost = _skill_cost(level)
        lines.append(
            f"━━━ {name} ━━━\n"
            f"· 等级：`{level}` ｜ 描述：{desc}\n"
            f"· 升到 {level + 1} 级需：`{next_cost:,.0f} XP`"
        )
    lines.append("")
    lines.append(f"💡 发送 {_cmd('train 舰船操控', 'train 舰船操控')} 将经验投入技能")
    lines.append(f"🔬 技能每升一级所需经验为上一级的 10 倍")

    await ctx.finish_markdown("\n".join(lines))


@on_command("train", aliases=["学习", "训练", "加点"])
async def handle_train(ctx):
    """把经验池经验投入指定技能"""
    player = await _get_player(ctx.user_openid)
    if not player:
        await ctx.finish("你还没有注册角色，发送 `start` 开始游戏")

    await _ensure_xp(player)

    arg = ctx.args_text.strip().lower()
    if not arg:
        await ctx.finish_markdown(
            f"📚 **训练技能**\n"
            f"· 经验池：`{player.unallocated_xp:.0f} XP`\n"
            f"· 用法：`train 技能名`\n"
            f"· 可选技能：{'、'.join(f'`{n}`' for n, _ in SKILLS.values())}"
        )

    # 匹配技能名（支持中文名/key）
    skill_key = None
    for key, (name, _) in SKILLS.items():
        if arg == name.lower() or arg == key:
            skill_key = key
            break
    if not skill_key:
        await ctx.finish(f"未知技能「{arg}」。可用：{'、'.join(f'`{n}`' for n, _ in SKILLS.values())}")

    current = _skill_level_of(player, skill_key)
    next_cost = _skill_cost(current)

    if player.unallocated_xp < next_cost:
        await ctx.finish(
            f"经验不足：`{SKILLS[skill_key][0]}` 升到 {current + 1} 级需要 `{next_cost:,.0f} XP`，"
            f"你只有 `{player.unallocated_xp:.0f} XP`。经验随时间增长或 PvP 胜利获得"
        )

    # 一次可连升多级（若经验足够）
    spent = 0.0
    levels_up = 0
    while player.unallocated_xp >= _skill_cost(current + levels_up):
        cost = _skill_cost(current + levels_up)
        spent += cost
        player.unallocated_xp -= cost
        levels_up += 1

    async with get_session() as session:
        result = await session.execute(select(GamePlayer).where(GamePlayer.id == player.id))
        p = result.scalar_one_or_none()
        if p:
            setattr(p, SKILL_FIELD[skill_key], current + levels_up)
            p.unallocated_xp = player.unallocated_xp
            await session.commit()

    await ctx.finish_markdown(
        f"📈 **训练完成**\n"
        f"· `{SKILLS[skill_key][0]}` 提升 {levels_up} 级：`{current}` → `{current + levels_up}`\n"
        f"· 消耗经验：`-{spent:,.0f} XP`\n"
        f"· 剩余经验池：`{player.unallocated_xp:.0f} XP`\n"
        f"\n"
        f"发送 {_cmd('skills', '查看技能')} 查看技能列表"
    )


# ==================== WS 服务入口 ====================

class FakeEvent:
    """模拟群消息事件（提供 mentions / member 等字段，供游戏逻辑读取）"""

    def __init__(self, user_openid: str, member_username: str, group_openid: str, is_private: bool):
        self.user_openid = user_openid
        self.member_username = member_username
        self.group_openid = group_openid
        self.is_private = is_private
        self.chat_openid = user_openid if is_private else group_openid
        self.msg_id = ""
        self.api = None
        self.member_role = "member"
        self.mentions = []


class GameContext:
    """WS 请求上下文：收集游戏逻辑的回复，最终由服务端发回客户端"""

    def __init__(self, user_openid: str, member_username: str, group_openid: str,
                 is_private: bool, args_text: str, mentions: list = None):
        self.user_openid = user_openid
        self.member_username = member_username
        self.group_openid = group_openid
        self.is_private = is_private
        self.args_text = args_text
        self.event = FakeEvent(user_openid, member_username, group_openid, is_private)
        self.event.mentions = mentions or []
        self.replies: list[dict] = []  # [{"format": "text"|"markdown", "content": ...}]

    def _add(self, fmt: str, content: str):
        if content:
            self.replies.append({"format": fmt, "content": content})

    async def send(self, text: str = ""):
        self._add("text", text)
        return None

    async def finish(self, text: str = ""):
        self._add("text", text)
        raise Finish(text)

    async def send_markdown(self, content: str = ""):
        self._add("markdown", content)
        return None

    async def finish_markdown(self, content: str = ""):
        self._add("markdown", content)
        raise Finish(content)

    async def send_image(self, image_bytes: bytes, filename: str = "image.png"):
        self._add("text", "[图片消息暂不支持]")
        return None

    async def finish_image(self, image_bytes: bytes, filename: str = "image.png"):
        self._add("text", "[图片消息暂不支持]")
        raise Finish("")


async def init_world():
    """初始化游戏世界（导入星系/模板）。幂等。"""
    await import_universe_csv()
    await _ensure_ships()
    await _ensure_ores_and_items()
    await _build_volume_cache()
    logger.info(f"[world] 游戏世界初始化完成，注册指令 {len(COMMANDS)} 个")


async def handle_command(command: str, args_text: str, context: dict) -> list[dict]:
    """处理一条指令，返回回复列表 [{"format", "content"}, ...]

    context: {user_openid, member_username, group_openid, is_private, mentions}
    """
    cmd = COMMANDS.get(command.lower())
    if cmd is None:
        return [{"format": "text", "content": f"未知指令：{command}"}]

    ctx = GameContext(
        user_openid=context.get("user_openid", ""),
        member_username=context.get("member_username", ""),
        group_openid=context.get("group_openid", ""),
        is_private=bool(context.get("is_private", False)),
        args_text=args_text,
        mentions=context.get("mentions"),
    )

    try:
        await cmd["handler"](ctx)
    except Finish:
        pass
    except Exception as e:
        logger.exception(f"[world] 指令 {command} 处理异常: {e}")
        ctx.replies.append({"format": "text", "content": "命令执行出错，请稍后重试"})

    if not ctx.replies:
        ctx.replies.append({"format": "text", "content": "（无回复）"})
    return ctx.replies
