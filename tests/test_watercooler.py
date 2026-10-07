#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watercooler 服务端验收测试（纯标准库 unittest）。

覆盖：健康检查、静态页、管理端建房/轮换密码、加入房间鉴权、
字段校验、发言/读取游标、who/status、房间隔离、限流、
超长文本、房间号格式、非法 JSON、超大请求体、未知路径。
仅依赖标准库与本仓库根目录下的 server.py，不访问外网、不占用固定端口。
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

ADMIN_KEY = "test-admin-key"


class WaterCoolerTests(unittest.TestCase):
    """watercooler 全套验收测试。"""

    httpd = None
    tmpdir = None
    port = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="wc-test-")
        cls.db = os.path.join(cls.tmpdir, "wc.db")
        server.init_db(cls.db)

        cls.webdir = os.path.join(cls.tmpdir, "web")
        os.makedirs(cls.webdir, exist_ok=True)
        with open(
            os.path.join(cls.webdir, "index.html"), "w", encoding="utf-8"
        ) as f:
            f.write(
                "<!DOCTYPE html>\n"
                "<html><head><meta charset=\"utf-8\">"
                "<title>电子饮水机</title></head>"
                "<body><h1>电子饮水机</h1>"
                "<p>AI 茶水间：多智能体协作聊天室</p>"
                "</body></html>\n"
            )

        cls.httpd = server.create_server(
            "127.0.0.1",
            0,
            cls.db,
            cls.webdir,
            ADMIN_KEY,
            limits={
                "say_interval": 1.5,
                "join_limit": 100000,
                "join_window": 300,
                "admin_limit": 100000,
                "admin_window": 60,
            },
        )
        cls.port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(
            target=cls.httpd.serve_forever, daemon=True
        )
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        if cls.httpd is not None:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        if cls.tmpdir is not None:
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        # 避免跨用例触发 join / admin / say 限流
        self.httpd.core.rate.clear()

    # ------------------------------------------------------------------
    # HTTP 辅助
    # ------------------------------------------------------------------

    def _url(self, path):
        return "http://127.0.0.1:{}{}".format(self.port, path)

    def post(self, path, payload):
        """POST JSON；payload 也可传 str/bytes 以发送非 JSON 体。

        返回 (status, dict)。
        """
        if isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(path),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            body = e.read()
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        return status, parsed

    def get(self, path, headers=None):
        """GET；返回 (status, bytes)。headers 可选（如模拟浏览器 UA）。"""
        req = urllib.request.Request(self._url(path), method="GET", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    # ------------------------------------------------------------------
    # 业务辅助
    # ------------------------------------------------------------------

    def join_agent(self, room, pw, name, **kw):
        payload = {
            "protocol": server.PROTOCOL_ACK,
            "room": room,
            "password": pw,
            "name": name,
            "display": kw.pop("display", "测试体-" + name),
            "harness": kw.pop("harness", "TestHarness"),
            "model": kw.pop("model", "test-model-v1"),
            "prompt": kw.pop(
                "prompt", "你是协同编码管线中的测试智能体。"
            ),
            "intro": kw.pop(
                "intro", "接入宣言：我是{}，很高兴接入电子饮水机。".format(name)
            ),
        }
        payload.update(kw)
        return self.post("/api/join", payload)

    def admin_create(self, **kw):
        payload = {"admin_key": ADMIN_KEY}
        payload.update(kw)
        return self.post("/api/admin/rooms/create", payload)

    def admin_rotate(self, room):
        return self.post(
            "/api/admin/rotate_password",
            {"admin_key": ADMIN_KEY, "room": room},
        )

    def admin_list(self):
        return self.post("/api/admin/rooms", {"admin_key": ADMIN_KEY})

    def room_count(self):
        st, resp = self.admin_list()
        self.assertEqual(st, 200)
        rooms = resp.get("rooms")
        if rooms is None:
            for key in ("data", "list", "items"):
                if isinstance(resp.get(key), list):
                    rooms = resp[key]
                    break
        if rooms is None and isinstance(resp.get("count"), int):
            return resp["count"]
        self.assertIsNotNone(rooms, "无法从 /api/admin/rooms 解析房间列表")
        return len(rooms)

    def _room_field(self, resp, key):
        """兼容 room 为 dict 或平铺字段的响应格式。"""
        room = resp.get("room")
        if isinstance(room, dict) and key in room:
            return room[key]
        return resp.get(key)

    def _room_number(self, resp):
        room = resp.get("room")
        if isinstance(room, dict):
            return room.get("number")
        if isinstance(room, str):
            return room
        return resp.get("number")

    # ------------------------------------------------------------------
    # 基础：健康检查与静态页
    # ------------------------------------------------------------------

    def test_health(self):
        st, body = self.get("/health")
        self.assertEqual(st, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "watercooler")
        self.assertEqual(data["version"], "1.0")

    def test_index_page(self):
        st, body = self.get("/", headers={"User-Agent": "Mozilla/5.0 (test-browser)"})
        self.assertEqual(st, 200)
        self.assertIn(b"<!DOCTYPE", body)
        self.assertIn("电子饮水机".encode("utf-8"), body)

    # ------------------------------------------------------------------
    # 管理端：建房
    # ------------------------------------------------------------------

    def test_admin_create_room(self):
        before = self.room_count()

        # 默认建房：6 位数字房间号 + 随机密码
        st, resp = self.admin_create()
        self.assertEqual(st, 200)
        self.assertTrue(resp.get("ok"))
        number = self._room_field(resp, "number")
        password = self._room_field(resp, "password")
        self.assertIsInstance(number, str)
        self.assertEqual(len(number), 6)
        self.assertTrue(number.isdigit())
        self.assertIsInstance(password, str)
        self.assertGreaterEqual(len(password), 6)

        after = self.room_count()
        self.assertEqual(after, before + 1)

        # 用返回的新密码可以加入
        st, joined = self.join_agent(number, password, "auto-join-agent")
        self.assertEqual(st, 200)
        self.assertTrue(joined.get("ok"))

        # 自定义房间号 + 自定义密码
        st, resp2 = self.admin_create(number="510510", password="CustomPass77")
        self.assertEqual(st, 200)
        number2 = self._room_field(resp2, "number")
        password2 = self._room_field(resp2, "password")
        self.assertEqual(number2, "510510")
        self.assertIsInstance(password2, str)
        self.assertTrue(password2)

        after2 = self.room_count()
        self.assertEqual(after2, before + 2)

        st, joined2 = self.join_agent("510510", password2, "custom-join-agent")
        self.assertEqual(st, 200)
        self.assertTrue(joined2.get("ok"))

    def test_room_number_format(self):
        # 含非数字字符
        st, resp = self.admin_create(number="12a4")
        self.assertEqual(st, 400)
        self.assertEqual(resp["error"], "bad_request")

        # 位数过短
        st, resp = self.admin_create(number="12")
        self.assertEqual(st, 400)
        self.assertEqual(resp["error"], "bad_request")

    # ------------------------------------------------------------------
    # 加入房间：鉴权与字段校验
    # ------------------------------------------------------------------

    def test_join_bad_password(self):
        st, _ = self.admin_create(number="520801", password="RightPass1")
        self.assertEqual(st, 200)
        st, resp = self.join_agent("520801", "WrongPass9", "bad-pw-agent")
        self.assertEqual(st, 403)
        self.assertEqual(resp["error"], "bad_password")

    def test_join_missing_field(self):
        st, _ = self.admin_create(number="530901", password="FieldPass1")
        self.assertEqual(st, 200)
        base = {
            "protocol": server.PROTOCOL_ACK,
            "room": "530901",
            "password": "FieldPass1",
            "name": "field-agent",
            "display": "字段测试体",
            "harness": "TestHarness",
            "model": "test-model-v1",
            "prompt": "你是字段校验测试智能体。",
            "intro": "接入宣言：字段校验。",
        }
        for field in ("harness", "model", "prompt", "intro"):
            for mode in ("missing", "empty"):
                with self.subTest(field=field, mode=mode):
                    payload = dict(base)
                    if mode == "missing":
                        payload.pop(field)
                    else:
                        payload[field] = ""
                    st, resp = self.post("/api/join", payload)
                    self.assertEqual(st, 400)
                    self.assertEqual(resp["error"], "bad_request")
                    self.assertIn(field, resp.get("message", ""))

    def test_join_success_and_intro_message(self):
        st, _ = self.admin_create(number="540601", password="IntroPass1")
        self.assertEqual(st, 200)
        harness = "IntroHarness-α"
        model = "intro-model-9"
        prompt = "你是接入宣言验收专用智能体，编号 INTRO-01。"
        st, resp = self.join_agent(
            "540601",
            "IntroPass1",
            "intro-agent",
            harness=harness,
            model=model,
            prompt=prompt,
            intro="接入宣言：我是 INTRO-01，正式接入电子饮水机。",
        )
        self.assertEqual(st, 200)
        self.assertTrue(resp.get("ok"))
        token = resp["token"]
        self.assertIsInstance(token, str)
        self.assertTrue(token)
        self.assertGreaterEqual(resp["seq"], 1)

        st, r = self.post("/api/read", {"token": token, "all": True})
        self.assertEqual(st, 200)
        msgs = r["messages"]
        self.assertGreaterEqual(len(msgs), 1)
        first = msgs[0]
        self.assertEqual(first["kind"], "intro")
        self.assertIn(harness, first["text"])
        self.assertIn(model, first["text"])
        self.assertIn(prompt, first["text"])
        self.assertIn("接入宣言", first["text"])

    # ------------------------------------------------------------------
    # 发言与读取
    # ------------------------------------------------------------------

    def test_say_requires_token(self):
        st, _ = self.admin_create(number="550201", password="TokenPass1")
        self.assertEqual(st, 200)

        st, resp = self.post("/api/say", {"text": "没有令牌的发言"})
        self.assertEqual(st, 401)
        self.assertEqual(resp["error"], "bad_token")

        st, resp = self.post(
            "/api/say", {"token": "f" * 32, "text": "伪造令牌的发言"}
        )
        self.assertEqual(st, 401)
        self.assertEqual(resp["error"], "bad_token")

    def test_say_then_read_cursor(self):
        st, _ = self.admin_create(number="560301", password="CursorPass1")
        self.assertEqual(st, 200)
        st, joined = self.join_agent("560301", "CursorPass1", "cursor-agent")
        self.assertEqual(st, 200)
        token = joined["token"]
        seq0 = joined["seq"]

        st, said = self.post(
            "/api/say", {"token": token, "text": "游标测试消息"}
        )
        self.assertEqual(st, 200)
        new_seq = said["seq"]
        self.assertGreater(new_seq, seq0)

        # 首次增量读取（cursor 从 0 开始）能拿到该条
        st, r1 = self.post("/api/read", {"token": token})
        self.assertEqual(st, 200)
        seqs = [m["seq"] for m in r1["messages"]]
        self.assertIn(new_seq, seqs)
        cursor = r1["cursor"]

        # 用最新 cursor 再读：无新内容
        st, r2 = self.post("/api/read", {"token": token, "cursor": cursor})
        self.assertEqual(st, 200)
        self.assertEqual(r2["messages"], [])

        # all=true 仍能看到历史
        st, r3 = self.post("/api/read", {"token": token, "all": True})
        self.assertEqual(st, 200)
        seqs_all = [m["seq"] for m in r3["messages"]]
        self.assertIn(new_seq, seqs_all)

    def test_who_contains_prompt(self):
        st, _ = self.admin_create(number="570401", password="WhoPass123")
        self.assertEqual(st, 200)
        prompt = "你是验收测试专用智能体，编号 WHO-77。"
        st, joined = self.join_agent(
            "570401",
            "WhoPass123",
            "who-agent",
            harness="WhoHarness",
            model="who-model-9",
            prompt=prompt,
            intro="接入宣言：WHO-77 报到。",
        )
        self.assertEqual(st, 200)

        st, w = self.post("/api/who", {"token": joined["token"]})
        self.assertEqual(st, 200)
        members = w["members"]
        names = [m["name"] for m in members]
        self.assertIn("who-agent", names)
        target = [m for m in members if m["name"] == "who-agent"][0]
        self.assertEqual(target["prompt"], prompt)
        self.assertEqual(target["harness"], "WhoHarness")
        self.assertEqual(target["model"], "who-model-9")

    def test_status(self):
        t0 = time.time()
        st, _ = self.admin_create(number="580501", password="StatusPas1")
        self.assertEqual(st, 200)
        st, joined = self.join_agent("580501", "StatusPas1", "status-agent")
        self.assertEqual(st, 200)
        token = joined["token"]
        st, _ = self.post(
            "/api/say", {"token": token, "text": "状态检查消息"}
        )
        self.assertEqual(st, 200)

        st, r = self.post("/api/status", {"token": token})
        self.assertEqual(st, 200)
        stats = r["stats"]
        self.assertEqual(stats["members"], 1)
        self.assertEqual(stats["messages"], 2)  # intro + say
        self.assertIsInstance(stats["last_ts"], (int, float))
        self.assertGreaterEqual(stats["last_ts"], t0 - 5)

    # ------------------------------------------------------------------
    # 隔离
    # ------------------------------------------------------------------

    def test_room_isolation(self):
        st, _ = self.admin_create(number="410001", password="PassIsoA1")
        self.assertEqual(st, 200)
        st, _ = self.admin_create(number="410002", password="PassIsoB1")
        self.assertEqual(st, 200)

        st, ja = self.join_agent("410001", "PassIsoA1", "iso-a")
        self.assertEqual(st, 200)
        st, jb = self.join_agent("410002", "PassIsoB1", "iso-b")
        self.assertEqual(st, 200)
        token_a = ja["token"]
        token_b = jb["token"]

        # B 房间发言
        st, sb = self.post(
            "/api/say", {"token": token_b, "text": "B房间专属消息XYZ"}
        )
        self.assertEqual(st, 200)
        # A 房间发言
        st, sa = self.post(
            "/api/say", {"token": token_a, "text": "A房间公开消息ABC"}
        )
        self.assertEqual(st, 200)

        # A 的 token 只能读到 A 的消息
        st, ra = self.post("/api/read", {"token": token_a})
        self.assertEqual(st, 200)
        texts_a = [m["text"] for m in ra["messages"]]
        self.assertIn("A房间公开消息ABC", texts_a)
        self.assertNotIn("B房间专属消息XYZ", texts_a)

        # B 的 token 只能读到 B 的消息
        st, rb = self.post("/api/read", {"token": token_b})
        self.assertEqual(st, 200)
        texts_b = [m["text"] for m in rb["messages"]]
        self.assertIn("B房间专属消息XYZ", texts_b)
        self.assertNotIn("A房间公开消息ABC", texts_b)

        # A 的 token 写入只落在 A（B 读不到，已由上一步验证）
        # status 通过 token 反查房间，请求体不带 room
        st, sa_status = self.post("/api/status", {"token": token_a})
        self.assertEqual(st, 200)
        self.assertEqual(self._room_number(sa_status), "410001")

        st, sb_status = self.post("/api/status", {"token": token_b})
        self.assertEqual(st, 200)
        self.assertEqual(self._room_number(sb_status), "410002")

    # ------------------------------------------------------------------
    # 限流与长度限制
    # ------------------------------------------------------------------

    def test_say_rate_limited(self):
        st, _ = self.admin_create(number="590601", password="RatePass11")
        self.assertEqual(st, 200)
        st, joined = self.join_agent("590601", "RatePass11", "rate-agent")
        self.assertEqual(st, 200)
        token = joined["token"]

        st, r1 = self.post("/api/say", {"token": token, "text": "第一条发言"})
        self.assertEqual(st, 200)

        # 间隔 < 1.5s 的第二次发言应被限流
        st, r2 = self.post("/api/say", {"token": token, "text": "第二条发言"})
        self.assertEqual(st, 429)
        self.assertEqual(r2["error"], "rate_limited")

    def test_text_too_long(self):
        st, _ = self.admin_create(number="600701", password="LongPass11")
        self.assertEqual(st, 200)
        st, joined = self.join_agent("600701", "LongPass11", "longtext-agent")
        self.assertEqual(st, 200)
        token = joined["token"]

        st, resp = self.post(
            "/api/say", {"token": token, "text": "长" * 4001}
        )
        self.assertEqual(st, 400)
        self.assertEqual(resp["error"], "text_too_long")

    # ------------------------------------------------------------------
    # 密码轮换
    # ------------------------------------------------------------------

    def test_rotate_password(self):
        old_pw = "OldPass123"
        st, _ = self.admin_create(number="610801", password=old_pw)
        self.assertEqual(st, 200)

        st, r = self.admin_rotate("610801")
        self.assertEqual(st, 200)
        self.assertTrue(r.get("ok"))
        new_pw = self._room_field(r, "password")
        self.assertIsInstance(new_pw, str)
        self.assertTrue(new_pw)
        self.assertNotEqual(new_pw, old_pw)

        # 旧密码 join → 403
        st, resp = self.join_agent("610801", old_pw, "old-pw-agent")
        self.assertEqual(st, 403)
        self.assertEqual(resp["error"], "bad_password")

        # 新密码 join → 成功
        st, resp = self.join_agent("610801", new_pw, "new-pw-agent")
        self.assertEqual(st, 200)
        self.assertTrue(resp.get("ok"))

    # ------------------------------------------------------------------
    # 协议层边界
    # ------------------------------------------------------------------

    def test_invalid_json(self):
        st, resp = self.post("/api/join", b"{this is not valid json")
        self.assertEqual(st, 400)
        self.assertEqual(resp["error"], "invalid_json")

    def test_body_too_large(self):
        big = {
            "room": "620901",
            "password": "BigPass123",
            "name": "big-body-agent",
            "display": "超大请求体测试",
            "harness": "TestHarness",
            "model": "test-model-v1",
            "prompt": "你是超大请求体测试智能体。",
            "intro": "接入宣言：" + "大" * 70000,
        }
        st, resp = self.post("/api/join", big)
        self.assertEqual(st, 413)
        self.assertEqual(resp["error"], "too_large")

    # ------------------------------------------------------------------
    # agent 门厅：/llms.txt 首站与「已读凭证」
    # ------------------------------------------------------------------

    def test_llms_and_protocol_docs_served(self):
        st, body = self.get("/llms.txt")
        self.assertEqual(st, 200)
        text = body.decode("utf-8")
        self.assertIn("接入须知", text)
        self.assertIn(server.PROTOCOL_ACK, text)
        st, body = self.get("/protocol.md")
        self.assertEqual(st, 200)
        self.assertIn("电子饮水机", body.decode("utf-8"))

    def test_root_docs_for_agents_html_for_browsers(self):
        # 非浏览器（无 Mozilla 字样）→ 直接收到接入须知
        st, body = self.get("/", headers={"User-Agent": "curl/8.5.0"})
        self.assertEqual(st, 200)
        self.assertIn("接入须知".encode("utf-8"), body)
        # 浏览器 → HTML 页面
        st, body = self.get("/", headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(st, 200)
        self.assertIn(b"<!DOCTYPE", body)

    def test_join_requires_protocol_ack(self):
        st, _ = self.admin_create(number="630901", password="AckPass123")
        self.assertEqual(st, 200)
        base = {
            "room": "630901",
            "password": "AckPass123",
            "name": "ack-agent",
            "display": "凭证测试体",
            "harness": "TestHarness",
            "model": "test-model-v1",
            "prompt": "你是门厅凭证测试智能体。",
            "intro": "接入宣言：凭证测试。",
        }
        st, resp = self.post("/api/join", base)
        self.assertEqual(st, 428)
        self.assertEqual(resp["error"], "protocol_required")
        self.assertIn("llms.txt", resp.get("message", ""))

        st, resp = self.post("/api/join", dict(base, protocol="not-the-value", name="ack-agent-2"))
        self.assertEqual(st, 428)
        self.assertEqual(resp["error"], "protocol_required")

        st, resp = self.post("/api/join", dict(base, protocol=server.PROTOCOL_ACK))
        self.assertEqual(st, 200)
        self.assertTrue(resp.get("ok"))

    # ------------------------------------------------------------------
    # 退出房间：人类成员移除（agent 保留）
    # ------------------------------------------------------------------

    def test_leave_removes_human_member(self):
        st, _ = self.admin_create(number="640901", password="LeavePass1")
        self.assertEqual(st, 200)
        st, resp = self.post("/api/enter",
                             {"room": "640901", "password": "LeavePass1", "name": "leave-tester"})
        self.assertEqual(st, 200)
        token = resp["token"]

        st, resp = self.post("/api/who", {"token": token})
        self.assertEqual(st, 200)
        self.assertIn("leave-tester", [m.get("name") for m in (resp.get("members") or [])])

        st, resp = self.post("/api/leave", {"name": "leave-tester", "token": token})
        self.assertEqual(st, 200)
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("left"))

        # token 随成员删除而失效
        st, _ = self.post("/api/who", {"token": token})
        self.assertEqual(st, 401)

        # 管理端视角：房间成员归零
        st, resp = self.admin_list()
        rows = [r for r in (resp.get("rooms") or []) if r.get("room") == "640901"]
        self.assertTrue(rows)
        self.assertEqual(rows[0].get("members"), 0)

        # agent 成员不能通过 leave 删除
        st, _ = self.admin_create(number="640902", password="LeavePass2")
        self.assertEqual(st, 200)
        st, resp = self.join_agent("640902", "LeavePass2", "leave-agent")
        self.assertEqual(st, 200)
        agent_token = resp["token"]
        st, resp = self.post("/api/leave", {"name": "leave-agent", "token": agent_token})
        self.assertEqual(st, 400)
        self.assertEqual(resp["error"], "bad_request")

    def test_unknown_path(self):
        st, _ = self.post("/nope", {"anything": 1})
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
