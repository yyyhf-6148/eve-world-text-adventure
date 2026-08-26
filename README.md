# EVE World 独立游戏程序

EVE Online 文字游戏（多人共享宇宙），作为**独立程序**运行，通过 WebSocket 提供服务。

bot 客户端连接后，服务端返回指令列表，客户端动态注册这些指令供群内玩家使用。

## 架构

```
┌─────────────┐   WebSocket    ┌──────────────────┐
│ QQ bot 客户端 │ ─────────────► │ EVE World 服务端 │
│ (eve_world_ │  register/      │ (本仓库)         │
│  client 插件)│  handle/reply   │ 独立数据库/逻辑   │
└─────────────┘                └──────────────────┘
```

## 启动

```bash
pip install -r requirements.txt
python download_universe.py   # 首次下载星系数据（Fuzzwork CSV）
python main.py                # 启动 WS 服务（默认 ws://0.0.0.0:8765）
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EVE_WORLD_WS_HOST` | `0.0.0.0` | WS 监听地址 |
| `EVE_WORLD_WS_PORT` | `8765` | WS 端口 |
| `EVE_WORLD_DATABASE_URL` | `sqlite+aiosqlite:///./data/game.db` | 独立数据库（可改 MySQL） |
| `EVE_WORLD_LOG_LEVEL` | `INFO` | 日志级别 |

## WS 协议（JSON）

连接建立后，服务端主动发送：

```json
{"type": "register", "commands": [{"name": "start", "aliases": ["注册"], "description": "..."}]}
```

客户端发送指令：

```json
{"type": "handle", "id": "uuid", "command": "start", "args": "",
 "context": {"user_openid": "...", "member_username": "...", "group_openid": "...",
             "is_private": false, "mentions": []}}
```

服务端回复：

```json
{"type": "reply", "id": "uuid", "replies": [{"format": "text|markdown", "content": "..."}]}
```

心跳：`{"type": "ping"}` → `{"type": "pong"}`

## 游戏指令

`start` `status` `scan` `move` `mine` `cargo` `market` `sell` `buy` `hunt` `fight`
`flee` `attack` `report` `mission` `upgrade` `fleet` `switch` `repair` `fittings` `install` `uninstall`

## 数据库

独立数据库（默认 SQLite，可换 MySQL）。表：星系/星门/舰船/装备/玩家/货舱/矿石/市场/NPC/战斗/战报/任务等。

## 部署

```bash
# 服务器上
docker build -t eve-world-game .   # 或直接 python main.py
```

需要开放 WS 端口供 bot 连接。