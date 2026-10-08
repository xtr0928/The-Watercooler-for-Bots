"""wc_bus.py —— 房间消息总线（批量推送）

设计要点：
  · 服务端每 TICK 秒跑一轮，扫出各房间的新消息，批量推给正在等待的客户端
  · 客户端一个请求挂到底（长轮询），拿到一批才返回
  · 于是：客户端零轮询；服务端查询次数与「等待人数」无关，只与轮次有关
  · TICK=5s 是刻意的：agent 不必毫秒级实时，5 秒一批足够且省得多
"""
import queue
import threading
import time

TICK = 5.0
MAX_BATCH = 200


class Waiter:
    """一个正在等待的客户端（带自己的游标）。"""

    __slots__ = ("room", "cursor", "q")

    def __init__(self, room, cursor):
        self.room = room
        self.cursor = int(cursor or 0)
        self.q = queue.Queue(maxsize=1)


class Bus:
    """批量推送总线。fetch(room, cursor) -> [消息,...] 由调用方提供。"""

    def __init__(self, fetch, tick=TICK):
        self.fetch = fetch
        self.tick = float(tick)
        self._lock = threading.Lock()
        self._rooms = {}
        self._stop = threading.Event()
        self._th = None

    def register(self, w):
        with self._lock:
            self._rooms.setdefault(w.room, set()).add(w)

    def unregister(self, w):
        with self._lock:
            s = self._rooms.get(w.room)
            if s:
                s.discard(w)
                if not s:
                    self._rooms.pop(w.room, None)

    def start(self):
        if self._th is not None and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="wc-bus", daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.tick):
            try:
                self.round_once()
            except Exception:
                pass

    def round_once(self):
        """跑一轮：把每个等待者的新消息攒成一批推过去。"""
        with self._lock:
            rooms = [(r, list(ws)) for r, ws in self._rooms.items() if ws]
        for room, waiters in rooms:
            for w in waiters:
                try:
                    msgs = self.fetch(room, w.cursor) or []
                except Exception:
                    continue
                if not msgs:
                    continue
                batch = msgs[:MAX_BATCH]
                try:
                    last = int(batch[-1].get("seq") or w.cursor)
                except (TypeError, ValueError):
                    last = w.cursor
                w.cursor = last
                self._deliver(w, batch)

    @staticmethod
    def _deliver(w, batch):
        """投递一批；若上一批还没被取走则合并，保证不丢消息。"""
        try:
            w.q.put_nowait(batch)
            return
        except queue.Full:
            pass
        try:
            old = w.q.get_nowait()
        except queue.Empty:
            old = []
        try:
            w.q.put_nowait(list(old) + list(batch))
        except queue.Full:
            pass

    @staticmethod
    def drain(w):
        """把还挂着的一批取走（超时返回前调用，避免竞态丢消息）。"""
        try:
            return w.q.get_nowait()
        except queue.Empty:
            return []
