import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "messenger.db")

app = FastAPI(title="Messenger")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# user_id -> активное WebSocket-соединение
connections: dict = {}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_by INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (group_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL,
        chat_type TEXT NOT NULL,
        target_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()


init_db()


class RegisterIn(BaseModel):
    name: str


class LoginIn(BaseModel):
    id: int
    name: str


class GroupIn(BaseModel):
    name: str
    created_by: int
    members: List[int] = []


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.post("/api/register")
def register(data: RegisterIn):
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")
    conn = db()
    cur = conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return {"id": user_id, "name": name}


@app.post("/api/login")
def login(data: LoginIn):
    conn = db()
    row = conn.execute(
        "SELECT id, name FROM users WHERE id = ? AND name = ?",
        (data.id, data.name.strip()),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(401, "Неверный ID или имя")
    return {"id": row["id"], "name": row["name"]}


@app.get("/api/users")
def list_users():
    conn = db()
    rows = conn.execute("SELECT id, name FROM users ORDER BY name").fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@app.get("/api/groups/{user_id}")
def user_groups(user_id: int):
    conn = db()
    rows = conn.execute(
        """SELECT g.id, g.name FROM groups g
           JOIN group_members gm ON gm.group_id = g.id
           WHERE gm.user_id = ? ORDER BY g.name""",
        (user_id,),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@app.post("/api/groups")
def create_group(data: GroupIn):
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Название группы не может быть пустым")
    members = set(data.members) | {data.created_by}
    conn = db()
    cur = conn.execute(
        "INSERT INTO groups (name, created_by) VALUES (?, ?)",
        (name, data.created_by),
    )
    group_id = cur.lastrowid
    for uid in members:
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, uid),
        )
    conn.commit()
    conn.close()
    return {"id": group_id, "name": name}


@app.get("/api/messages")
def history(user_id: int, chat_type: str, target_id: int):
    conn = db()
    if chat_type == "group":
        rows = conn.execute(
            """SELECT m.id, m.sender_id, m.chat_type, m.target_id, m.text,
                      m.created_at, u.name AS sender_name
               FROM messages m JOIN users u ON u.id = m.sender_id
               WHERE m.chat_type = 'group' AND m.target_id = ?
               ORDER BY m.id DESC LIMIT 100""",
            (target_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT m.id, m.sender_id, m.chat_type, m.target_id, m.text,
                      m.created_at, u.name AS sender_name
               FROM messages m JOIN users u ON u.id = m.sender_id
               WHERE m.chat_type = 'private' AND (
                   (m.sender_id = ? AND m.target_id = ?) OR
                   (m.sender_id = ? AND m.target_id = ?))
               ORDER BY m.id DESC LIMIT 100""",
            (user_id, target_id, target_id, user_id),
        ).fetchall()
    conn.close()
    result = [dict(r) for r in reversed(rows)]
    return result


# NEW: Delete account endpoint
@app.delete("/api/users/{user_id}")
def delete_user(user_id: int):
    conn = db()
    # Check user exists
    user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(404, "Пользователь не найден")
    
    # Delete user's messages
    conn.execute("DELETE FROM messages WHERE sender_id = ?", (user_id,))
    # Remove from group memberships
    conn.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
    # Delete groups created by user (and their memberships)
    created_groups = conn.execute("SELECT id FROM groups WHERE created_by = ?", (user_id,)).fetchall()
    for g in created_groups:
        conn.execute("DELETE FROM group_members WHERE group_id = ?", (g["id"],))
        conn.execute("DELETE FROM messages WHERE chat_type = 'group' AND target_id = ?", (g["id"],))
    conn.execute("DELETE FROM groups WHERE created_by = ?", (user_id,))
    # Finally delete the user
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    
    conn.commit()
    conn.close()
    return {"ok": True}


def get_recipients(sender_id: int, chat_type: str, target_id: int) -> list:
    conn = db()
    if chat_type == "private":
        exists = conn.execute("SELECT 1 FROM users WHERE id = ?", (target_id,)).fetchone()
        conn.close()
        return [sender_id, target_id] if exists else []
    rows = conn.execute(
        "SELECT user_id FROM group_members WHERE group_id = ?", (target_id,)
    ).fetchall()
    conn.close()
    ids = [r["user_id"] for r in rows]
    return ids if sender_id in ids else []


def save_message(sender_id: int, chat_type: str, target_id: int, text: str) -> dict:
    created = datetime.now(timezone.utc).isoformat()
    conn = db()
    cur = conn.execute(
        """INSERT INTO messages (sender_id, chat_type, target_id, text, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (sender_id, chat_type, target_id, text, created),
    )
    conn.commit()
    msg_id = cur.lastrowid
    sender_name = conn.execute(
        "SELECT name FROM users WHERE id = ?", (sender_id,)
    ).fetchone()["name"]
    conn.close()
    return {
        "type": "message",
        "id": msg_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "chat_type": chat_type,
        "target_id": target_id,
        "text": text,
        "created_at": created,
    }


@app.websocket("/ws/{user_id}")
async def ws_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    connections[user_id] = websocket
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("type") != "message":
                continue

            chat_type = data.get("chat_type")
            target_id = data.get("target_id")
            text = (data.get("text") or "").strip()
            if chat_type not in ("private", "group"):
                continue
            if not isinstance(target_id, int) or not text or len(text) > 2000:
                continue

            recipients = get_recipients(user_id, chat_type, target_id)
            if not recipients:
                continue

            payload = save_message(user_id, chat_type, target_id, text)
            for uid in set(recipients):
                target_ws = connections.get(uid)
                if target_ws:
                    try:
                        await target_ws.send_text(json.dumps(payload))
                    except Exception:
                        connections.pop(uid, None)
    except WebSocketDisconnect:
        pass
    finally:
        if connections.get(user_id) is websocket:
            connections.pop(user_id, None)
