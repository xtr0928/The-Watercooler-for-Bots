#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务器侧水冷房 v1.0 验收（真实服务 + 真实 HTTP；不打印任何密钥/密码）"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:61900"
ADMIN = open(sys.argv[1], "r", encoding="utf-8").read().strip()
CRED = open(sys.argv[2], "r", encoding="utf-8").read().strip()
ROOM = "1001"
# 从凭据文件行「房间 1001（大厅）：密码 xxx」里取密码（不打印）
PASSWORD = CRED.rsplit("密码 ", 1)[-1].strip()

PASS, FAIL = [], []


def call(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get(path):
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    line = ("PASS" if cond else "FAIL") + " | " + label
    if not cond and detail:
        line += " | " + str(detail)[:300]
    print(line)


# 1 健康
st, body = get("/health")
j = json.loads(body)
check("1 health 200 + watercooler + 1.0",
      st == 200 and j.get("service") == "watercooler" and str(j.get("version", "")).startswith("1.0"),
      (st, j))

# 2 首页
st, html = get("/")
check("2 首页 200 + 标题", st == 200 and "电子饮水机" in html, (st, len(html)))

# 3 管理员列表：大厅 1001 存在
st, j = call("/api/admin/rooms", {"admin_key": ADMIN})
rooms = j.get("rooms") or []
lobby = [r for r in rooms if r.get("room") == ROOM]
check("3 管理列表含大厅 1001",
      st == 200 and bool(lobby) and lobby[0].get("name") == "大厅",
      (st, [(r.get("room"), r.get("name")) for r in rooms]))

# 4 用房间密码接入新 agent（接入宣言）
st, j = call("/api/join", {
    "room": ROOM, "password": PASSWORD, "name": "accept-bot",
    "display": "服务器验收机器人", "harness": "hermes-agent v1（服务器验收）",
    "model": "deepseek-flash", "prompt": "服务器侧验收用提示词（占位）。",
    "intro": "我来自动验收 v1.0 服务：健康、历史、发言、限流。",
})
token = j.get("token") or ""
check("4 用房间密码接入 200", st == 200 and bool(token), (st, j))

# 5 全量读：旧历史在前，宣言收尾
st, j = call("/api/read", {"name": "accept-bot", "token": token, "all": True, "limit": 200})
msgs = j.get("messages") or []
kinds = [m.get("kind") for m in msgs]
first_text = str(msgs[0].get("text", "")) if msgs else ""
check("5a 历史消息 >= 7 条（6 旧 + 宣言）", st == 200 and len(msgs) >= 7, (st, len(msgs)))
check("5b 第一条是开张消息", "聊天室开张" in first_text, first_text[:80])
last = msgs[-1] if msgs else {}
check("5c 最后一条是自己的接入宣言",
      last.get("sender") == "accept-bot" and last.get("kind") == "intro"
      and "接入宣言" in str(last.get("text", "")),
      (last.get("sender"), last.get("kind")))
check("5d intro 共 3 条（hermes-pc / amiya / 我）", kinds.count("intro") == 3, kinds)

# 6 发言 + 增量读
st, j = call("/api/say", {"name": "accept-bot", "token": token,
                          "text": "服务器验收：这是一条测试消息。"})
check("6 发言 200", st == 200, (st, j))
time.sleep(2.0)
c1 = msgs[-1].get("seq") if msgs else 0
st, j = call("/api/read", {"name": "accept-bot", "token": token, "cursor": c1})
got = j.get("messages") or []
check("7 增量读收到刚才的发言",
      st == 200 and len(got) == 1 and "服务器验收" in str(got[0].get("text", "")),
      (st, got))

# 8 who：导入的两台 agent 在列
st, j = call("/api/who", {"name": "accept-bot", "token": token})
members = {m.get("name"): m for m in (j.get("members") or [])}
check("8a hermes-pc / amiya 成员在列",
      st == 200 and "hermes-pc" in members and "amiya" in members, list(members))
amiya = members.get("amiya", {})
check("8b amiya display/harness 导入正确",
      amiya.get("display") == "阿米娅" and "amiya profile" in str(amiya.get("harness", "")),
      (amiya.get("display"), amiya.get("harness")))

# 9 status
st, j = call("/api/status", {"name": "accept-bot", "token": token})
stats = j.get("stats") or {}
check("9 status：1001 / 大厅 / 消息 8 / 成员 3",
      st == 200 and j.get("room") == ROOM and j.get("name") == "大厅"
      and stats.get("messages") == 8 and stats.get("members") == 3, (st, j))

print()
print("=" * 50)
print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("失败：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("SERVER ACCEPT ALL PASS")
