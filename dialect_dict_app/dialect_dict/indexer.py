"""异步索引服务。

- 发布时向 index_tasks 投递 (entry_id, version) 任务；
- 工作线程/测试可乱序完成任务，版本守卫保证：
  仅当 task.version > 该词条已应用版本时才应用，否则标记 superseded。
  因此无论完成顺序如何，索引最终收敛到最高版本。
- 索引内容来自发布闭包（不可变快照），与在线编辑解耦。
"""
import json
import threading
import time

from .db import now
from .normalize import normalize_with_map
from .publish import get_publication


def docs_from_closure(closure):
    """把发布闭包展开为分字段文档： (kind, oid, sub, raw)。"""
    docs = [("headword", "", "", closure["entry"]["headword"])]
    if closure["entry"].get("note"):
        docs.append(("note", "", "", closure["entry"]["note"]))
    for p in closure["pronunciations"]:
        if p.get("text"):
            docs.append(("pron", p["id"], p["scheme"], p["text"]))
    for s in closure["senses"]:
        docs.append(("sense_def", s["id"], "", s["definition"]))
        if s.get("usage_note"):
            docs.append(("sense_note", s["id"], "", s["usage_note"]))
        for ex in s["examples"]:
            docs.append(("example", ex["id"], "", ex["text"]))
            if ex.get("translation"):
                docs.append(("translation", ex["id"], "", ex["translation"]))
    return docs


class Indexer:
    def __init__(self, connect, shared=False):
        # connect: 连接工厂；shared=True 表示工厂返回共享连接，用后不得关闭
        self._connect = connect
        self._shared = shared
        self._stop = threading.Event()
        self._thread = None

    def _release(self, conn):
        if not self._shared:
            conn.close()

    # ---------------------------------------------------------- 任务
    def submit(self, entry_id, version):
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO index_tasks(entry_id, version, status, created_at)"
                " VALUES (?,?,'pending',?)", (entry_id, version, now()))
            conn.commit()
            return cur.lastrowid
        finally:
            self._release(conn)

    def pending(self):
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM index_tasks WHERE status='pending' ORDER BY id")]
        finally:
            self._release(conn)

    def tasks(self):
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM index_tasks ORDER BY id")]
        finally:
            self._release(conn)

    # ---------------------------------------------------------- 应用
    def complete(self, task_id):
        """完成指定任务（模拟某个 worker 完成）。乱序安全。"""
        conn = self._connect()
        try:
            t = conn.execute("SELECT * FROM index_tasks WHERE id=?",
                             (task_id,)).fetchone()
            if not t or t["status"] != "pending":
                return None
            st = conn.execute(
                "SELECT applied_version FROM index_state WHERE entry_id=?",
                (t["entry_id"],)).fetchone()
            applied = st["applied_version"] if st else 0
            if t["version"] <= applied:
                conn.execute(
                    "UPDATE index_tasks SET status='superseded',"
                    " completed_at=?, detail=? WHERE id=?",
                    (now(), "已有更新版本 v%d 被应用，跳过 v%d"
                     % (applied, t["version"]), task_id))
                conn.commit()
                return "superseded"
            pub = get_publication(conn, t["entry_id"], t["version"])
            if not pub:
                conn.execute(
                    "UPDATE index_tasks SET status='failed', completed_at=?,"
                    " detail='发布物不存在' WHERE id=?", (now(), task_id))
                conn.commit()
                return "failed"
            docs = docs_from_closure(pub["closure"])
            conn.execute("DELETE FROM index_docs WHERE entry_id=?",
                         (t["entry_id"],))
            for kind, oid, sub, raw in docs:
                norm, offsets, flags = normalize_with_map(raw)
                conn.execute(
                    "INSERT OR REPLACE INTO index_docs(entry_id, kind, oid,"
                    " sub, raw, norm, offsets, flags, version)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (t["entry_id"], kind, oid, sub, raw, norm,
                     json.dumps(offsets, ensure_ascii=False),
                     json.dumps(sorted(flags)), t["version"]))
            conn.execute(
                "INSERT INTO index_state(entry_id, applied_version)"
                " VALUES (?,?) ON CONFLICT(entry_id)"
                " DO UPDATE SET applied_version=excluded.applied_version",
                (t["entry_id"], t["version"]))
            conn.execute(
                "UPDATE index_tasks SET status='applied', completed_at=?,"
                " detail=? WHERE id=?",
                (now(), "应用 v%d（%d 个字段文档）" % (t["version"], len(docs)),
                 task_id))
            conn.commit()
            return "applied"
        finally:
            self._release(conn)

    def complete_all(self):
        for t in self.pending():
            self.complete(t["id"])

    def applied_version(self, entry_id):
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT applied_version FROM index_state WHERE entry_id=?",
                (entry_id,)).fetchone()
            return r["applied_version"] if r else 0
        finally:
            self._release(conn)

    # ---------------------------------------------------------- 后台线程
    def start_background(self, interval=0.3):
        def loop():
            while not self._stop.is_set():
                try:
                    self.complete_all()
                except Exception:
                    pass
                time.sleep(interval)
        self._thread = threading.Thread(target=loop, daemon=True,
                                        name="indexer")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
