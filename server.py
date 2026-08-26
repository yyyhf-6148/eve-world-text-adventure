"""EVE World 独立程序：WebSocket 服务端

协议（JSON over WS）：
- 连接建立后，服务端主动发送: {"type":"register", "commands":[{name,aliases,description}]}
- 客户端发送:      {"type":"handle", "id":"<uuid>", "command":"start", "args":"", "context":{...}}
- 服务端回复:      {"type":"reply", "id":"<uuid>", "replies":[{"format":"text|markdown","content":"..."}]}
- 心跳:           {"type":"ping"} -> {"type":"pong"}
"""

import asyncio
import json

import websockets

import game
from config import WS_HOST, WS_PORT
from logger import logger


async def _send(ws, payload: dict):
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def handler(ws):
    """处理单个 WS 连接"""
    client_ip = ws.remote_address[0] if ws.remote_address else "?"
    logger.info(f"客户端连接: {client_ip}")

    try:
        # 连接建立后发送指令注册列表
        await _send(ws, {
            "type": "register",
            "commands": game.command_list(),
        })

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send(ws, {"type": "error", "message": "invalid json"})
                continue

            mtype = msg.get("type")
            if mtype == "ping":
                await _send(ws, {"type": "pong"})
            elif mtype == "handle":
                req_id = msg.get("id", "")
                command = msg.get("command", "")
                args_text = msg.get("args", "")
                context = msg.get("context", {})
                try:
                    replies = await game.handle_command(command, args_text, context)
                except Exception as e:
                    logger.exception(f"处理指令异常: {e}")
                    replies = [{"format": "text", "content": "服务端处理异常"}]
                await _send(ws, {
                    "type": "reply",
                    "id": req_id,
                    "replies": replies,
                })
            else:
                await _send(ws, {"type": "error", "message": f"unknown type: {mtype}"})
    except websockets.exceptions.ConnectionClosed:
        logger.info(f"客户端断开: {client_ip}")
    except Exception as e:
        logger.exception(f"连接异常 {client_ip}: {e}")


async def start_server():
    await game.init_world()
    async with websockets.serve(handler, WS_HOST, WS_PORT):
        logger.info(f"EVE World WS 服务已启动: ws://{WS_HOST}:{WS_PORT}")
        await asyncio.Future()  # 永久运行