#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watercooler 服务端主程序。

单进程单端口 HTTP 服务：静态页、API 路由、SQLite 存储、PBKDF2 鉴权、
token 反查、限流、健康检查与中文日志。
"""

import argparse
import hashlib
import hmac
import http.server
import json
import os
import secrets
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.parse


SERVICE = "watercooler"
VERSION = "1.0"
MAX_BODY = 64 * 1024

LEN = {
    "display": 48,
    "harness": 64,
    "model": 64,
    "intro": 1000,
    "prompt": 4000,
    "text": 4000,
    "human_name": 32,
    "password_min": 1,
    "password_max": 64,
}

READ_DEFAULT = 500
READ_ALL_DEFAULT = 200
READ_ALL_MAX = 2000

# 名字与房间号的规范（正则形式说明；实际校验用等价的字符集实现，避免额外依赖）
RE_NAME = r"^[A-Za-z0-9_.-]{1,32}$"
RE_ROOM = r"^[0-9]{4,12}$"

ERROR_STATUS = {
    "invalid_json": 400,
    "bad_request": 400,
    "text_too_long": 400,
    "bad_token": 401,
    "bad_password": 403,
    "admin_denied": 403,
    "room_not_found": 404,
    "too_large": 413,
    "rate_limited": 429,
}

PBKDF2_ITERATIONS = 100000

_NAME_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
)
_DIGIT_CHARS = frozenset("0123456789")
_PASSWORD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    salt TEXT NOT NULL,
    hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'agent',
    display TEXT NOT NULL DEFAULT '',
    harness TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    intro TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    joined_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    UNIQUE (room, name)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL,
    seq INTEGER NOT NULL,
    sender TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL,
    UNIQUE (room, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_room_seq ON messages (room, seq);
CREATE INDEX IF NOT EXISTS idx_members_room ON members (room);
CREATE INDEX IF NOT EXISTS idx_members_token ON members (token);
"""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def now_ts():
    return time.time()


def ok_body(**kw):
    body = {"ok": True}
    body.update(kw)
    return body


def err_body(code, message):
    return {"ok": False, "error": code, "message": message}


def hash_password(pw):
    """PBKDF2-SHA256，返回 (salt_hex, hash_hex)。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pw.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return salt.hex(), digest.hex()


def verify_password(pw, salt_hex, hash_hex):
    if not isinstance(pw, str):
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (TypeError, ValueError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", pw.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(digest, expected)


def gen_token():
    return secrets.token_hex(16)


def gen_password():
    # 8 位，大小写字母 + 数字，剔除易混淆字符 0 O 1 l I
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(8))


def gen_room_number(conn):
    for _ in range(20):
        num = str(100000 + secrets.randbelow(900000))
        row = conn.execute(
            "SELECT 1 FROM rooms WHERE number = ?", (num,)
        ).fetchone()
        if row is None:
            return num
    raise RuntimeError("无法生成可用的房间号，请稍后重试")


def valid_room_number(s):
    if not isinstance(s, str):
        return False
    if not (4 <= len(s) <= 12):
        return False
    return all(c in _DIGIT_CHARS for c in s)


def valid_agent_name(s):
    if not isinstance(s, str):
        return False
    if not (1 <= len(s) <= 32):
        return False
    return all(c in _NAME_CHARS for c in s)


def valid_human_name(s):
    if not isinstance(s, str):
        return False
    if not (1 <= len(s) <= LEN["human_name"]):
        return False
    if not s.isprintable():
        return False
    return s.strip() != ""


def check_text(value, maxlen):
    """校验文本字段：非字符串或为空 → 缺/空；按 Unicode 码点数计长，超限 → 过长。"""
    if not isinstance(value, str) or value.strip() == "":
        return False, "内容缺失或为空"
    if len(value) > maxlen:
        return False, "内容过长（最多 {} 个字符）".format(maxlen)
    return True, ""


def build_intro(display, name, harness, model, prompt, intro):
    return "▶ 接入宣言：{}（{}）\nharness：{}\n模型：{}\n提示词：{}\n自我介绍：{}".format(
        display, name, harness, model, prompt, intro
    )


# ---------------------------------------------------------------------------
# 限流器
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(
        self,
        say_interval=1.5,
        join_limit=12,
        join_window=300,
        admin_limit=30,
        admin_window=60,
    ):
        self.say_interval = say_interval
        self.join_limit = join_limit
        self.join_window = join_window
        self.admin_limit = admin_limit
        self.admin_window = admin_window
        self._max_window = max(join_window, admin_window, say_interval)
        self._lock = threading.Lock()
        self._last_say = {}
        self._join_hits = {}
        self._admin_hits = {}

    def _cleanup(self, now):
        for key in list(self._last_say.keys()):
            if now - self._last_say[key] > self._max_window:
                del self._last_say[key]
        for hits, window in (
            (self._join_hits, self.join_window),
            (self._admin_hits, self.admin_window),
        ):
            for key in list(hits.keys()):
                kept = [t for t in hits[key] if now - t < window]
                if kept:
                    hits[key] = kept
                else:
                    del hits[key]

    def check_say(self, member_id):
        with self._lock:
            now = time.time()
            self._cleanup(now)
            last = self._last_say.get(member_id)
            if last is not None and now - last < self.say_interval:
                return False
            self._last_say[member_id] = now
            return True

    def check_join(self, ip):
        with self._lock:
            now = time.time()
            self._cleanup(now)
            hits = self._join_hits.get(ip, [])
            if len(hits) >= self.join_limit:
                return False
            hits.append(now)
            self._join_hits[ip] = hits
            return True

    def check_admin(self, ip):
        with self._lock:
            now = time.time()
            self._cleanup(now)
            hits = self._admin_hits.get(ip, [])
            if len(hits) >= self.admin_limit:
                return False
            hits.append(now)
            self._admin_hits[ip] = hits
            return True

    def clear(self):
        with self._lock:
            self._last_say.clear()
            self._join_hits.clear()
            self._admin_hits.clear()


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path):
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 业务核心
# ---------------------------------------------------------------------------

class Core:
    def __init__(self, db_path, admin_key, limits=None):
        self.db_path = db_path
        self.admin_key = admin_key
        self.rate = RateLimiter(**(limits or {}))
        self.lock = threading.Lock()
        init_db(db_path)
        self.routes = {
            "/api/join": self._join,
            "/api/enter": self._enter,
            "/api/say": self._say,
            "/api/read": self._read,
            "/api/who": self._who,
            "/api/status": self._status,
            "/api/admin/rooms/create": self._create_room,
            "/api/admin/rooms": self._list_rooms,
            "/api/admin/rotate_password": self._rotate_password,
        }

    # -- 分发 ---------------------------------------------------------------

    def handle(self, path, payload, client_ip):
        handler = self.routes.get(path)
        if handler is None:
            return 404, err_body("not_found", "未知接口")
        if not isinstance(payload, dict):
            return 400, err_body("bad_request", "请求体必须是 JSON 对象")
        with self.lock:
            conn = connect(self.db_path)
            try:
                status, body = handler(conn, payload, client_ip)
                try:
                    conn.commit()
                except sqlite3.Error:
                    pass
                return status, body
            except Exception as exc:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                sys.stderr.write(
                    "内部异常：{}: {}\n".format(type(exc).__name__, exc)
                )
                return 500, err_body("server_error", "服务器内部错误")
            finally:
                conn.close()

    # -- 内部辅助 -----------------------------------------------------------

    def _member_by_token(self, conn, token):
        if not isinstance(token, str) or not token:
            return None
        return conn.execute(
            "SELECT * FROM members WHERE token = ?", (token,)
        ).fetchone()

    def _auth(self, conn, payload):
        member = self._member_by_token(conn, payload.get("token"))
        if member is None:
            return None, (401, err_body("bad_token", "令牌无效或已过期"))
        return member, None

    def _next_seq(self, conn, room):
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE room = ?",
            (room,),
        ).fetchone()
        return int(row[0])

    def _append_message(self, conn, room, seq, sender, kind, text):
        ts = now_ts()
        conn.execute(
            "INSERT INTO messages (room, seq, sender, kind, text, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (room, seq, sender, kind, text, ts),
        )
        return ts

    def _room_or_error(self, conn, payload):
        room = payload.get("room")
        if not valid_room_number(room):
            return None, (
                400,
                err_body("bad_request", "房间号格式不正确（4-12 位数字）"),
            )
        row = conn.execute(
            "SELECT * FROM rooms WHERE number = ?", (room,)
        ).fetchone()
        if row is None:
            return None, (404, err_body("room_not_found", "房间不存在"))
        return row, None

    def _check_password(self, payload, room_row):
        pw = payload.get("password")
        if not isinstance(pw, str):
            return False
        if not (LEN["password_min"] <= len(pw) <= LEN["password_max"]):
            return False
        return verify_password(pw, room_row["salt"], room_row["hash"])

    def _check_profile_fields(self, payload):
        fields = [
            ("name", None),
            ("display", LEN["display"]),
            ("harness", LEN["harness"]),
            ("model", LEN["model"]),
            ("prompt", LEN["prompt"]),
            ("intro", LEN["intro"]),
        ]
        for key, maxlen in fields:
            if key not in payload:
                return 400, err_body("bad_request", "缺少必填字段：" + key)
            value = payload[key]
            if key == "name":
                if not valid_agent_name(value):
                    return 400, err_body(
                        "bad_request",
                        "字段 name 格式不合法"
                        "（仅限字母、数字、下划线、点、横线，长度 1-32）",
                    )
                continue
            ok, msg = check_text(value, maxlen)
            if not ok:
                code = "text_too_long" if "过长" in msg else "bad_request"
                return ERROR_STATUS[code], err_body(
                    code, "字段 {}：{}".format(key, msg)
                )
        return None

    def _admin_guard(self, conn, payload, ip):
        if not self.rate.check_admin(ip):
            return 429, err_body("rate_limited", "操作过于频繁，请稍后再试")
        key = payload.get("admin_key")
        if not isinstance(key, str) or not key:
            return 403, err_body("admin_denied", "管理员密钥错误")
        if not hmac.compare_digest(
            self.admin_key.encode("utf-8"), key.encode("utf-8")
        ):
            return 403, err_body("admin_denied", "管理员密钥错误")
        return None

    # -- 业务接口 -----------------------------------------------------------

    def _join(self, conn, payload, ip):
        if not self.rate.check_join(ip):
            return 429, err_body("rate_limited", "加入房间过于频繁，请稍后再试")
        room_row, err = self._room_or_error(conn, payload)
        if err is not None:
            return err
        room = room_row["number"]
        if not self._check_password(payload, room_row):
            return 403, err_body("bad_password", "房间密码错误")
        if not isinstance(payload.get("display"), str) or payload.get("display", "").strip() == "":
            payload["display"] = payload.get("name") if isinstance(payload.get("name"), str) else ""
        err = self._check_profile_fields(payload)
        if err is not None:
            return err
        name = payload["name"]
        display = payload["display"]
        harness = payload["harness"]
        model = payload["model"]
        prompt = payload["prompt"]
        intro = payload["intro"]
        token = gen_token()
        now = now_ts()
        existing = conn.execute(
            "SELECT * FROM members WHERE room = ? AND name = ?", (room, name)
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE members SET kind = 'agent', display = ?, harness = ?, "
                "model = ?, prompt = ?, intro = ?, token = ?, seq = 0, "
                "last_seen = ? WHERE id = ?",
                (display, harness, model, prompt, intro, token, now, existing["id"]),
            )
            return 200, ok_body(token=token, room=room, name=name, seq=0)
        seq = self._next_seq(conn, room)
        self._append_message(
            conn, room, seq, name, "intro", build_intro(display, name, harness, model, prompt, intro)
        )
        conn.execute(
            "INSERT INTO members (room, name, kind, display, harness, model, "
            "prompt, intro, token, seq, joined_at, last_seen) "
            "VALUES (?, ?, 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (room, name, display, harness, model, prompt, intro, token, seq, now, now),
        )
        return 200, ok_body(token=token, room=room, name=name, seq=seq)

    def _enter(self, conn, payload, ip):
        if not self.rate.check_join(ip):
            return 429, err_body("rate_limited", "进入房间过于频繁，请稍后再试")
        room_row, err = self._room_or_error(conn, payload)
        if err is not None:
            return err
        room = room_row["number"]
        if not self._check_password(payload, room_row):
            return 403, err_body("bad_password", "房间密码错误")
        name = payload.get("name")
        if name is None or (isinstance(name, str) and name.strip() == ""):
            name = "人类访客"
        if not valid_human_name(name):
            return 400, err_body(
                "bad_request",
                "称呼不合法（最长 {} 个字符，且不可包含不可见字符）".format(
                    LEN["human_name"]
                ),
            )
        token = gen_token()
        now = now_ts()
        existing = conn.execute(
            "SELECT * FROM members WHERE room = ? AND name = ?", (room, name)
        ).fetchone()
        if existing is not None:
            if existing["kind"] != "human":
                return 400, err_body("bad_request", "该名字已被智能体占用，请换一个称呼")
            conn.execute(
                "UPDATE members SET kind = 'human', token = ?, last_seen = ? "
                "WHERE id = ?",
                (token, now, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO members (room, name, kind, display, harness, model, "
                "prompt, intro, token, seq, joined_at, last_seen) "
                "VALUES (?, ?, 'human', '', '', '', '', '', ?, 0, ?, ?)",
                (room, name, token, now, now),
            )
        return 200, ok_body(token=token, room=room, name=name)

    def _say(self, conn, payload, ip):
        member, err = self._auth(conn, payload)
        if err is not None:
            return err
        text = payload.get("text")
        ok, msg = check_text(text, LEN["text"])
        if not ok:
            code = "text_too_long" if "过长" in msg else "bad_request"
            return ERROR_STATUS[code], err_body(code, "字段 text：" + msg)
        if not self.rate.check_say(member["id"]):
            return 429, err_body("rate_limited", "发言过于频繁，请稍后再试")
        room = member["room"]
        seq = self._next_seq(conn, room)
        kind = "human" if member["kind"] == "human" else "msg"
        ts = self._append_message(conn, room, seq, member["name"], kind, text)
        conn.execute(
            "UPDATE members SET last_seen = ? WHERE id = ?",
            (now_ts(), member["id"]),
        )
        return 200, ok_body(seq=seq, ts=ts)

    def _read(self, conn, payload, ip):
        member, err = self._auth(conn, payload)
        if err is not None:
            return err
        room = member["room"]
        cursor = payload.get("cursor", 0)
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            cursor = 0
        if cursor < 0:
            cursor = 0
        all_mode = payload.get("all") is True or payload.get("all") == "true"
        if all_mode:
            try:
                limit = int(payload.get("limit", READ_ALL_DEFAULT))
            except (TypeError, ValueError):
                limit = READ_ALL_DEFAULT
            limit = max(1, min(limit, READ_ALL_MAX))
            rows = conn.execute(
                "SELECT seq, sender, kind, text, ts FROM messages "
                "WHERE room = ? ORDER BY seq DESC LIMIT ?",
                (room, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                "SELECT seq, sender, kind, text, ts FROM messages "
                "WHERE room = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (room, cursor, READ_DEFAULT),
            ).fetchall()
        if rows:
            new_cursor = int(rows[-1]["seq"])
        elif all_mode:
            new_cursor = 0
        else:
            new_cursor = cursor
        member_count = conn.execute(
            "SELECT COUNT(*) FROM members WHERE room = ?", (room,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE members SET last_seen = ? WHERE id = ?",
            (now_ts(), member["id"]),
        )
        members_map = {
            m["name"]: (m["display"] or m["name"])
            for m in conn.execute(
                "SELECT name, display FROM members WHERE room = ?", (room,)
            ).fetchall()
        }
        messages = [
            {
                "seq": int(r["seq"]),
                "sender": r["sender"],
                "display": members_map.get(r["sender"], r["sender"]),
                "kind": r["kind"],
                "text": r["text"],
                "ts": r["ts"],
            }
            for r in rows
        ]
        return 200, ok_body(
            room=room,
            member_count=int(member_count),
            messages=messages,
            cursor=new_cursor,
            server_time=now_ts(),
        )

    def _who(self, conn, payload, ip):
        member, err = self._auth(conn, payload)
        if err is not None:
            return err
        rows = conn.execute(
            "SELECT id, name, kind, display, harness, model, prompt, intro, "
            "joined_at, last_seen FROM members WHERE room = ? ORDER BY id ASC",
            (member["room"],),
        ).fetchall()
        members = [
            {
                "id": int(r["id"]),
                "name": r["name"],
                "kind": r["kind"],
                "display": r["display"],
                "harness": r["harness"],
                "model": r["model"],
                "prompt": r["prompt"],
                "intro": r["intro"],
                "joined_at": r["joined_at"],
                "last_seen": r["last_seen"],
            }
            for r in rows
        ]
        return 200, ok_body(members=members, server_time=now_ts())

    def _status(self, conn, payload, ip):
        member, err = self._auth(conn, payload)
        if err is not None:
            return err
        room = member["room"]
        msg_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE room = ?", (room,)
        ).fetchone()[0]
        mem_count = conn.execute(
            "SELECT COUNT(*) FROM members WHERE room = ?", (room,)
        ).fetchone()[0]
        last_ts = conn.execute(
            "SELECT MAX(ts) FROM messages WHERE room = ?", (room,)
        ).fetchone()[0]
        room_row = conn.execute(
            "SELECT name FROM rooms WHERE number = ?", (room,)
        ).fetchone()
        room_name = room_row["name"] if room_row is not None else ""
        return 200, ok_body(
            room=room,
            name=room_name,
            stats={
                "messages": int(msg_count),
                "members": int(mem_count),
                "last_ts": last_ts,
            },
            server_time=now_ts(),
        )

    def _create_room(self, conn, payload, ip):
        err = self._admin_guard(conn, payload, ip)
        if err is not None:
            return err
        number = payload.get("number")
        if number is None or number == "":
            number = gen_room_number(conn)
        else:
            if not valid_room_number(number):
                return 400, err_body(
                    "bad_request", "房间号格式不正确（4-12 位数字）"
                )
            row = conn.execute(
                "SELECT 1 FROM rooms WHERE number = ?", (number,)
            ).fetchone()
            if row is not None:
                return 400, err_body("bad_request", "房间号已存在，请换一个")
        password = payload.get("password")
        if password is None or password == "":
            password = gen_password()
        elif not isinstance(password, str) or not (
            LEN["password_min"] <= len(password) <= LEN["password_max"]
        ):
            return 400, err_body(
                "bad_request", "房间密码长度需在 1-64 个字符之间"
            )
        name = payload.get("name")
        if not isinstance(name, str) or name.strip() == "":
            name = "房间 " + number
        name = name.strip()[: LEN["display"]]
        salt_hex, hash_hex = hash_password(password)
        conn.execute(
            "INSERT INTO rooms (number, name, salt, hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (number, name, salt_hex, hash_hex, now_ts()),
        )
        return 200, ok_body(
            room=number, number=number, name=name, password=password
        )

    def _list_rooms(self, conn, payload, ip):
        err = self._admin_guard(conn, payload, ip)
        if err is not None:
            return err
        rows = conn.execute(
            "SELECT r.number AS number, r.name AS name, r.created_at AS created_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.room = r.number) AS msg_count, "
            "(SELECT COUNT(*) FROM members b WHERE b.room = r.number) AS member_count, "
            "(SELECT MAX(ts) FROM messages t WHERE t.room = r.number) AS last_ts "
            "FROM rooms r ORDER BY r.created_at DESC"
        ).fetchall()
        rooms = [
            {
                "room": r["number"],
                "name": r["name"],
                "created_at": r["created_at"],
                "messages": int(r["msg_count"]),
                "members": int(r["member_count"]),
                "last_ts": r["last_ts"],
            }
            for r in rows
        ]
        return 200, ok_body(rooms=rooms, server_time=now_ts())

    def _rotate_password(self, conn, payload, ip):
        err = self._admin_guard(conn, payload, ip)
        if err is not None:
            return err
        room_row, err = self._room_or_error(conn, payload)
        if err is not None:
            return err
        room = room_row["number"]
        password = gen_password()
        salt_hex, hash_hex = hash_password(password)
        conn.execute(
            "UPDATE rooms SET salt = ?, hash = ? WHERE number = ?",
            (salt_hex, hash_hex, room),
        )
        return 200, ok_body(room=room, number=room, password=password)


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WaterCooler/" + VERSION

    # -- 计时与日志 ---------------------------------------------------------

    def handle_one_request(self):
        self._t0 = time.time()
        super().handle_one_request()

    def log_request(self, code="-", size="-"):
        elapsed_ms = 0
        t0 = getattr(self, "_t0", None)
        if t0 is not None:
            elapsed_ms = int((time.time() - t0) * 1000)
        try:
            ip = self.client_address[0]
        except Exception:
            ip = "-"
        line = "{} | {} | {} | {} | {}ms | {}".format(
            time.strftime("%Y-%m-%d %H:%M:%S"),
            self.command,
            self.path,
            code,
            elapsed_ms,
            ip,
        )
        sys.stderr.write(line + "\n")
        sys.stderr.flush()

    def log_message(self, fmt, *args):
        # 统一走 log_request 的单行中文日志；绝不打印请求体 / token / 密码
        pass

    # -- 发送辅助 -----------------------------------------------------------

    def _send_bytes(self, status, data, content_type, no_store=False):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if no_store:
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_json(self, status, body, no_store=True):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            status, data, "application/json; charset=utf-8", no_store=no_store
        )

    def _send_html(self, status, text):
        data = text.encode("utf-8")
        self._send_bytes(status, data, "text/html; charset=utf-8")

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            web_path = getattr(self.server, "web_path", None)
            try:
                with open(web_path, "r", encoding="utf-8") as f:
                    html = f.read()
            except (OSError, TypeError):
                self._send_json(
                    500, err_body("server_error", "页面文件缺失或不可读取")
                )
                return
            self._send_html(200, html)
            return
        if path == "/health":
            self._send_json(
                200,
                ok_body(
                    service=SERVICE,
                    version=VERSION,
                    status="ok",
                    server_time=now_ts(),
                ),
            )
            return
        self._send_json(404, err_body("not_found", "未知接口"))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/"):
            self._send_json(404, err_body("not_found", "未知接口"))
            return
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header is not None else 0
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY:
            # 先尽量排空请求体，保证客户端能完整收到 413 响应；
            # 特大请求体（>4MB）无法排空时只能断开连接避免残留字节。
            if length <= 4 * 1024 * 1024:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            else:
                self.close_connection = True
            self._send_json(
                ERROR_STATUS["too_large"],
                err_body(
                    "too_large",
                    "请求体过大（上限 {} 字节）".format(MAX_BODY),
                ),
            )
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError):
            self._send_json(
                ERROR_STATUS["invalid_json"],
                err_body("invalid_json", "请求体不是合法的 JSON 对象"),
            )
            return
        try:
            client_ip = self.client_address[0]
        except Exception:
            client_ip = "-"
        status, body = self.server.core.handle(path, payload, client_ip)
        self._send_json(status, body)


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(host, port, db_path, web_path, admin_key, limits=None):
    if os.path.isdir(web_path):
        web_path = os.path.join(web_path, "index.html")
    core = Core(db_path, admin_key, limits)
    httpd = _ThreadingHTTPServer((host, port), Handler)
    httpd.core = core
    httpd.web_path = web_path
    return httpd


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _load_admin_key(key_file):
    """读取或生成管理员密钥；只返回密钥值，不打印。"""
    if os.path.exists(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except OSError:
            pass
    env_value = os.environ.get("WATERCOOLER_ADMIN_KEY", "").strip()
    if env_value:
        return env_value
    key = secrets.token_urlsafe(24)
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    os.chmod(key_file, 0o600)
    return key


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="server.py", description="watercooler 协作聊天服务"
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=61900, help="监听端口")
    parser.add_argument("--db", default="data/watercooler.db", help="SQLite 数据库路径")
    parser.add_argument("--web", default="web/index.html", help="静态页面文件路径")
    parser.add_argument(
        "--admin-key-file", default="data/admin_key.txt", help="管理员密钥文件路径"
    )
    args = parser.parse_args(argv)

    for path in (args.db, args.admin_key_file):
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    admin_key = _load_admin_key(args.admin_key_file)

    print("watercooler 服务已启动")
    print("版本：{}".format(VERSION))
    print("监听地址：http://{}:{}".format(args.host, args.port))
    print("数据库：{}".format(os.path.abspath(args.db)))
    print("页面文件：{}".format(os.path.abspath(args.web)))
    print("管理员密钥文件：{}".format(os.path.abspath(args.admin_key_file)))
    if args.host in ("0.0.0.0", "::"):
        print("服务对网络开放，请依赖房间密码保护")

    httpd = create_server(
        host=args.host,
        port=args.port,
        db_path=args.db,
        web_path=args.web,
        admin_key=admin_key,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("服务已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
