# imports and setup start here
import json
import os
import random
import re
import sqlite3
import string
import time
import uuid

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

app = FastAPI()
DB_URL = os.environ.get("DATABASE_URL")  # railway gives this automatically
MAX_IMG = 2 * 1024 * 1024  # two megabyte image limit
MAX_GIF = 10 * 1024 * 1024  # gifs get ten megabytes
OK_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
HEX = re.compile(r"^[0-9a-fA-F]{6}$")
HERE = os.path.dirname(os.path.abspath(__file__))  # paths work from anywhere
STATIC = os.path.join(HERE, "static")


# database layer starts here
pool = None  # postgres pool lives here
lite = None  # sqlite fallback for local


@app.on_event("startup")
async def init_db():
    global pool, lite
    if DB_URL:  # use postgres on railway
        import asyncpg
        pool = await asyncpg.create_pool(DB_URL)
        async with pool.acquire() as c:
            await c.execute("CREATE TABLE IF NOT EXISTS images (id TEXT PRIMARY KEY, mime TEXT, data BYTEA)")
            await c.execute("CREATE TABLE IF NOT EXISTS messages (id SERIAL PRIMARY KEY, room TEXT, body TEXT)")
    else:  # local testing uses sqlite
        lite = sqlite3.connect(os.path.join(HERE, "local.db"), check_same_thread=False)
        lite.execute("CREATE TABLE IF NOT EXISTS images (id TEXT PRIMARY KEY, mime TEXT, data BLOB)")
        lite.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, room TEXT, body TEXT)")
        lite.commit()


async def save_image(img_id, mime, data):
    if pool:
        await pool.execute("INSERT INTO images VALUES ($1,$2,$3)", img_id, mime, data)
    else:
        lite.execute("INSERT INTO images VALUES (?,?,?)", (img_id, mime, data))
        lite.commit()


async def load_image(img_id):
    if pool:
        row = await pool.fetchrow("SELECT mime, data FROM images WHERE id=$1", img_id)
        return (row["mime"], row["data"]) if row else None
    return lite.execute("SELECT mime, data FROM images WHERE id=?", (img_id,)).fetchone()


async def save_msg(room, msg):
    body = json.dumps(msg)
    if pool:
        await pool.execute("INSERT INTO messages (room, body) VALUES ($1,$2)", room, body)
    else:
        lite.execute("INSERT INTO messages (room, body) VALUES (?,?)", (room, body))
        lite.commit()


async def recent_msgs(room, n=50):
    if pool:
        rows = await pool.fetch("SELECT body FROM messages WHERE room=$1 ORDER BY id DESC LIMIT $2", room, n)
        rows = [r["body"] for r in rows]
    else:
        rows = [r[0] for r in lite.execute("SELECT body FROM messages WHERE room=? ORDER BY id DESC LIMIT ?", (room, n))]
    return [json.loads(b) for b in reversed(rows)]
# database layer ends here


# room state lives in memory
rooms = {}  # room code to sockets


def new_code():
    while True:  # keep trying until unique
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if code not in rooms:
            return code


async def send_room(code, data):
    dead = []
    for ws in rooms.get(code, set()):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)  # remember broken sockets
    for ws in dead:
        rooms[code].discard(ws)


async def send_count(code):
    await send_room(code, {"type": "count", "n": len(rooms.get(code, ()))})


async def leave(ws, code):
    if code and code in rooms:
        rooms[code].discard(ws)
        if rooms[code]:
            await send_count(code)
        else:
            del rooms[code]  # empty rooms get removed
# room state ends here


# http routes start here
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if file.content_type not in OK_TYPES:
        return JSONResponse({"error": "images only please"}, 400)
    limit = MAX_GIF if file.content_type == "image/gif" else MAX_IMG
    data = await file.read(limit + 1)
    if len(data) > limit:
        mb = limit // (1024 * 1024)
        return JSONResponse({"error": f"image too big ({mb}mb max)"}, 400)
    img_id = uuid.uuid4().hex
    await save_image(img_id, file.content_type, data)
    return {"id": img_id}


@app.get("/img/{img_id}")
async def get_img(img_id: str):
    row = await load_image(img_id)
    if not row:
        return Response(status_code=404)
    return Response(bytes(row[1]), media_type=row[0],
                    headers={"Cache-Control": "public, max-age=31536000"})


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
# http routes end here


# websocket message handling starts here
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    room = None  # this socket's current room
    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")

            if kind in ("create", "join"):  # create or join room
                await leave(ws, room)
                code = new_code() if kind == "create" else str(data.get("room", "")).upper().strip()
                if kind == "join" and code not in rooms:
                    await ws.send_json({"type": "system", "text": f"room {code} not found"})
                    continue
                room = code
                rooms.setdefault(room, set()).add(ws)
                await ws.send_json({"type": "joined", "room": room, "created": kind == "create",
                                    "history": await recent_msgs(room)})
                await send_count(room)

            elif kind == "leave":
                await leave(ws, room)
                room = None
                await ws.send_json({"type": "left"})

            elif kind == "list":  # room browser asks here
                await ws.send_json({"type": "rooms",
                                    "rooms": [{"code": c, "n": len(s)} for c, s in rooms.items()]})

            elif kind == "msg" and room:  # chat messages handled here
                color = str(data.get("color", "000000")).strip().lstrip("#")
                if len(color) == 3:  # f0a becomes ff00aa
                    color = "".join(c * 2 for c in color)
                text = str(data.get("text", "")).strip()[:1000]
                img_id = str(data.get("id", ""))
                if not re.fullmatch(r"[0-9a-f]{32}", img_id):
                    img_id = ""  # ignore bad image ids
                if not text and not img_id:
                    continue  # nothing to send
                msg = {
                    "type": "msg",
                    "name": str(data.get("name") or "Anonymous")[:20],
                    "color": color.lower() if HEX.match(color) else "000000",  # reject bad colour values
                    "time": time.time(),
                    "text": text,
                    "id": img_id,
                }
                await save_msg(room, msg)
                await send_room(room, msg)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await leave(ws, room)  # cleanup when tab closes
# websocket message handling ends here


app.mount("/static", StaticFiles(directory=STATIC), name="static")
