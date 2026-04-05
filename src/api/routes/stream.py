"""
Nexus AI — WebSocket real-time streaming endpoint
Provides live pipeline events via WebSocket (in addition to SSE).
"""
from __future__ import annotations
import asyncio, json, time
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from src.security.auth import decode_token
import structlog

log = structlog.get_logger(__name__)
router = APIRouter()
_connections: dict[str, list[WebSocket]] = {}

@router.websocket("/ws/{task_id}")
async def websocket_stream(websocket: WebSocket, task_id: str):
    token = websocket.query_params.get("token", "")
    try:
        token_data = decode_token(token)
    except Exception:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    if task_id not in _connections:
        _connections[task_id] = []
    _connections[task_id].append(websocket)
    log.info("ws_connected", task_id=task_id[:8], user=token_data.sub[:8])

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if data == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "ts": time.time()}))
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "heartbeat", "ts": time.time()}))
    except WebSocketDisconnect:
        pass
    finally:
        if task_id in _connections:
            _connections[task_id] = [ws for ws in _connections[task_id] if ws != websocket]

async def broadcast_event(task_id: str, event: dict) -> None:
    if task_id not in _connections:
        return
    dead = []
    for ws in _connections[task_id]:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            dead.append(ws)
    _connections[task_id] = [ws for ws in _connections[task_id] if ws not in dead]
