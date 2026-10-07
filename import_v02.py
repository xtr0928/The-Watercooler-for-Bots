#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_v02.py

把 v0.2 文件版数据（rooms/*.jsonl + agents/*.json）导入供 v1.0 服务读取的
新 SQLite 库。

- 只读 v0.2 原始文件，绝不修改或删除 rooms/、agents/ 目录下的任何内容。
- 复用 server.py 的模块级函数：init_db / hash_password / gen_password / now_ts / gen_token。
- 无第三方依赖，仅使用标准库。
"""

import argparse
from datetime import datetime
import json
import os
import re
import secrets
import sqlite3
import sys

import server
from server import gen_password, hash_password, init_db, now_ts

ROOM_NUMBER_RE = re.compile(r"^[0-9]{4,12}$")
VALID_KINDS = {"msg", "human", "system", "intro"}

AGENT_PLACEHOLDERS = {
    "harness": "未知 harness（v0.2 导入）",
    "model": "未知模型（v0.2 导入）",
    "prompt": "（v0.2 导入，无提示词记录）",
    "intro": "（v0.2 导入，无自我介绍）",
}

_gen_token_fn = getattr(server, "gen_token", None)


def gen_token():
    """优先复用 server.gen_token，签名不兼容或缺失时使用本地随机 token。"""
    if _gen_token_fn is not None:
        try:
            return _gen_token_fn()
        except TypeError:
            pass
    return secrets.token_hex(16)


def make_password_record():
    """生成随机密码及配套 salt/hash，返回 (password, salt, hash)。"""
    password = gen_password()
    salt, phash = hash_password(password)
    return password, salt, phash


def open_db(db_path):
    """打开（必要时创建目录和库），并复用 server.init_db 初始化表结构。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    return conn


def default_dir(name):
    """缺省目录：优先脚本所在目录（仓库根）下的 name/，其次当前目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, name), os.path.join(os.getcwd(), name)]
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    return candidates[0]


def parse_ts(value):
    """把 v0.2 时间戳（ISO 字符串 / 数字）转 epoch 秒；无法识别用当前时间。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        s = value.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).timestamp()
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            pass
    return now_ts()


def parse_agent_file(path):
    """解析 agents/<name>.json，返回 (agent_dict, 补齐字段名列表)。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象")
    stem = os.path.splitext(os.path.basename(path))[0]
    name = str(data.get("name") or stem)
    filled = []
    agent = {"name": name}
    if data.get("display"):
        agent["display"] = str(data["display"])
    else:
        agent["display"] = name
        filled.append("display")
    for key in ("harness", "model", "prompt", "intro"):
        val = data.get(key)
        if val:
            agent[key] = str(val)
        else:
            agent[key] = AGENT_PLACEHOLDERS[key]
            filled.append(key)
    return agent, filled


def parse_room_file(path, known_agent_names):
    """宽松解析 rooms/<room>.jsonl，返回 (messages, 行级错误列表)。

    - 逐行 json.loads，坏行记录错误并跳过；
    - seq 缺失/非法/重复时，整份文件按出现顺序重排为 1..N；
    - ts 缺失/非法时用 now_ts()；
    - kind 缺失 → sender 为已知 agent 时 'msg'，否则 'human'；非法值回落 'msg'。
    """
    records = []
    errors = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, json.JSONDecodeError) as exc:
                errors.append("第 %d 行 JSON 解析失败：%s" % (lineno, exc))
                continue
            if not isinstance(obj, dict):
                errors.append("第 %d 行不是 JSON 对象，已跳过" % lineno)
                continue
            records.append(obj)

    # --- 处理 seq ---
    seqs = []
    seq_ok = True
    seen = set()
    for obj in records:
        seq = obj.get("seq")
        if seq is None:
            seq_ok = False
            seqs.append(None)
            continue
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            seq_ok = False
            seqs.append(None)
            continue
        if seq in seen:
            seq_ok = False
        seen.add(seq)
        seqs.append(seq)
    if not seq_ok:
        seqs = list(range(1, len(records) + 1))

    # --- 组装消息 ---
    messages = []
    for idx, obj in enumerate(records):
        ts = parse_ts(obj.get("ts"))
        sender = obj.get("sender")
        sender = str(sender).strip() if sender is not None else ""
        kind = obj.get("kind")
        if kind in VALID_KINDS:
            pass
        elif kind is None or kind == "":
            kind = "msg" if sender in known_agent_names else "human"
        else:
            kind = "msg"
        text = obj.get("text")
        text = "" if text is None else str(text)
        messages.append({
            "seq": seqs[idx],
            "ts": ts,
            "sender": sender,
            "kind": kind,
            "text": text,
        })
    return messages, errors


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把 v0.2 文件版 rooms/*.jsonl + agents/*.json 导入新 SQLite 库（供 v1.0 服务读取）。"
                    "只读 v0.2 原始文件，绝不修改或删除它们。")
    parser.add_argument("--rooms-dir", default=None,
                        help="v0.2 房间目录（缺省在仓库根下找 rooms/）")
    parser.add_argument("--agents-dir", default=None,
                        help="v0.2 agent 目录（缺省在仓库根下找 agents/）")
    parser.add_argument("--db", default=os.path.join("data", "watercooler.db"),
                        help="目标 SQLite 库路径（缺省 data/watercooler.db）")
    parser.add_argument("--room-name", default=None,
                        help="导入房间的显示名（缺省为“导入房间 <房间号>”）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只扫描解析并打印统计，不写库")
    parser.add_argument("--force", action="store_true",
                        help="房间号已存在时覆盖导入（先清掉该房旧数据再写）")
    parser.add_argument("--password-file", default=None,
                        help="把新生成的房间密码写入该文件（0600，不在屏幕回显）")
    args = parser.parse_args(argv)

    rooms_dir = args.rooms_dir or default_dir("rooms")
    agents_dir = args.agents_dir or default_dir("agents")

    fatal = False
    parse_errors = []      # 文件级/行级错误（非致命，记录后继续）
    agent_fill_notes = []  # 补齐字段清单

    # ---------- 读取 agents ----------
    agents = []
    if os.path.isdir(agents_dir):
        for fn in sorted(os.listdir(agents_dir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(agents_dir, fn)
            try:
                agent, filled = parse_agent_file(path)
            except Exception as exc:
                parse_errors.append("agents/%s：%s（该 agent 已跳过）" % (fn, exc))
                continue
            agents.append(agent)
            for field in filled:
                agent_fill_notes.append("agents/%s：缺少字段 %s（已用占位补齐）" % (fn, field))
    else:
        print("[告警] 未找到 agents 目录：%s（将不导入任何 agent 成员）" % agents_dir, file=sys.stderr)

    agent_names = set()
    for agent in agents:
        agent_names.add(agent["name"])
        # 文件名主干也视作已知 agent 名，便于 sender 匹配
        agent_names.add(agent["name"].strip())

    # ---------- 读取 rooms ----------
    rooms = []  # {number, file, messages, errors, humans}
    scanned_rooms = 0
    invalid_number_files = 0
    if os.path.isdir(rooms_dir):
        for fn in sorted(os.listdir(rooms_dir)):
            if not fn.endswith(".jsonl"):
                continue
            scanned_rooms += 1
            stem = fn[: -len(".jsonl")]
            path = os.path.join(rooms_dir, fn)
            if not ROOM_NUMBER_RE.match(stem):
                print("[告警] 文件名 %s 不是合法房间号（需 4-12 位数字），已跳过" % fn,
                      file=sys.stderr)
                invalid_number_files += 1
                continue
            try:
                messages, errors = parse_room_file(path, agent_names)
            except Exception as exc:
                parse_errors.append("rooms/%s：%s（该房间已跳过）" % (fn, exc))
                continue
            for err in errors:
                parse_errors.append("rooms/%s：%s" % (fn, err))
            humans = sorted({
                m["sender"] for m in messages
                if m["sender"] and m["sender"] not in agent_names
                and m["kind"] != "system"
            })
            rooms.append({
                "number": stem,
                "file": fn,
                "messages": messages,
                "errors": errors,
                "humans": humans,
            })
    else:
        print("[告警] 未找到 rooms 目录：%s（无房间可导入）" % rooms_dir, file=sys.stderr)

    # ---------- 写库 / 统计 ----------
    imported_rooms = 0
    skipped_existing = 0
    imported_members = 0
    imported_messages = 0
    passwords = []  # (number, name, password)

    if args.dry_run:
        # 只统计，不写库、不生成密码
        for room in rooms:
            imported_rooms += 1
            imported_members += len(agents) + len(room["humans"])
            imported_messages += len(room["messages"])
    elif rooms:
        conn = None
        try:
            conn = open_db(args.db)
            cur = conn.cursor()
            for room in rooms:
                number = room["number"]
                exists = cur.execute(
                    "SELECT 1 FROM rooms WHERE number=?", (number,)).fetchone()
                if exists and not args.force:
                    skipped_existing += 1
                    continue
                if exists and args.force:
                    # --force：先清掉该房旧数据（含 messages 的主键冲突源）
                    cur.execute("DELETE FROM messages WHERE room=?", (number,))
                    cur.execute("DELETE FROM members WHERE room=?", (number,))
                    cur.execute("DELETE FROM rooms WHERE number=?", (number,))

                name = args.room_name or ("导入房间 %s" % number)
                password, salt, phash = make_password_record()
                ts_now = now_ts()
                cur.execute(
                    "INSERT INTO rooms (number, name, salt, hash, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (number, name, salt, phash, ts_now))
                passwords.append((number, name, password))
                imported_rooms += 1

                # members：每个 agent 一条；cursor 置为该房最大 seq，避免重复推历史
                max_seq = 0
                for m in room["messages"]:
                    if m["seq"] > max_seq:
                        max_seq = m["seq"]
                for agent in agents:
                    cur.execute(
                        "INSERT INTO members (room, name, kind, display,"
                        " harness, model, prompt, intro, token, seq,"
                        " joined_at, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (number, agent["name"], "agent",
                         agent.get("display") or agent["name"],
                         agent.get("harness") or "",
                         agent.get("model") or "",
                         agent.get("prompt") or "",
                         agent.get("intro") or "",
                         gen_token(), max_seq, ts_now, ts_now))
                    imported_members += 1
                # 人类发送者：kind='human'，无 token（不落文件）
                for human in room["humans"]:
                    cur.execute(
                        "INSERT INTO members (room, name, kind, display,"
                        " harness, model, prompt, intro, token, seq,"
                        " joined_at, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (number, human, "human", human,
                         "", "", "", "", "", 0, ts_now, ts_now))
                    imported_members += 1

                # messages：按房间批量写入
                if room["messages"]:
                    cur.executemany(
                        "INSERT INTO messages (room, seq, ts, sender, kind, text)"
                        " VALUES (?,?,?,?,?,?)",
                        [(number, m["seq"], m["ts"], m["sender"], m["kind"],
                          m["text"]) for m in room["messages"]])
                    imported_messages += len(room["messages"])
            conn.commit()
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            print("[致命] 写库失败（%s）：%s" % (args.db, exc), file=sys.stderr)
            fatal = True
        finally:
            if conn is not None:
                conn.close()

    # ---------- 报告 ----------
    mode_note = "（dry-run：只统计，未写库）" if args.dry_run else ""
    print("=" * 56)
    print("v0.2 → v1.0 数据导入统计报告 %s" % mode_note)
    print("=" * 56)
    print("扫描房间文件数：%d" % scanned_rooms)
    print("导入房间数：%d" % imported_rooms)
    print("跳过房间数：%d" % (scanned_rooms - imported_rooms))
    print("  - 非法房间号（文件名告警）：%d" % invalid_number_files)
    print("  - 已存在且未加 --force：%d" % skipped_existing)
    print("  - 解析失败整文件跳过：见下方错误清单")
    print("导入成员数：%d" % imported_members)
    print("导入消息数：%d" % imported_messages)

    if agent_fill_notes:
        print("-" * 56)
        print("补齐字段清单：")
        for note in agent_fill_notes:
            print("  - %s" % note)
    else:
        print("补齐字段清单：（无，agent 文件字段齐全）")

    if parse_errors:
        print("-" * 56)
        print("解析错误/告警（按文件记录，均已跳过坏行或坏文件后继续）：")
        for err in parse_errors:
            print("  - %s" % err)
    else:
        print("解析错误/告警：（无）")

    if fatal:
        passwords = []  # 事务已回滚，不再展示未生效的密码
    if passwords and args.password_file:
        lines = ["房间 %s（%s）：密码 %s" % (n, nm, pw)
                 for n, nm, pw in passwords]
        with open(args.password_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        try:
            os.chmod(args.password_file, 0o600)
        except OSError:
            pass
        print("-" * 56)
        print("新生成房间密码已写入文件：%s（仅此一次生成，请妥善保管）"
              % args.password_file)
        passwords = []
    if passwords:
        print("-" * 56)
        print("新生成房间密码（仅此一次打印，请尽快用管理面板改密）：")
        for number, name, password in passwords:
            print("  房间 %s（%s）：密码 %s" % (number, name, password))
    elif args.dry_run:
        print("-" * 56)
        print("（dry-run 模式：未写库，未生成任何密码）")

    print("=" * 56)
    if fatal:
        print("结果：存在致命错误，导入未完成。")
        return 1
    print("结果：导入完成。" if not args.dry_run else "结果：dry-run 校验完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
