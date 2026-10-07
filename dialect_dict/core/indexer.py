# -*- coding: utf-8 -*-
"""异步索引服务。

乱序完成安全的设计：
1. 索引文档按 (entry_id, version_id) 命名空间隔离 —— 旧版本的迟到任务写旧命名空间，
   永远不会覆盖新版本文档；
2. 检索可见性由「版本水位线」pointers.indexed 控制：indexed = 所有 ≤v 版本任务
   全部完成的最大 v。同版本内任务乱序完成无所谓，跨版本乱序也只会推迟水位线，
   不会造成半新半旧的可见状态；
3. 同一 (entry, version) 任务重复执行是幂等的（先删后插）。
"""
import json, random, threading, time
from .normalize import normalize, tokenize

class Indexer:
    def __init__(self, db, service):
        self.db, self.service = db, service
        self._stop = threading.Event()
        self._thread = None

    # ---- 任务执行 ----
    def run_task(self, task_id) -> bool:
        t = self.db.one("SELECT * FROM index_tasks WHERE id=?", (task_id,))
        if not t or t["status"] != "pending":
            return False
        docs = self.service.doc_fields(t["entry_id"], t["version_id"])
        with self.db.tx() as c:
            c.execute("DELETE FROM idx_norm WHERE entry_id=? AND version_id=?",
                      (t["entry_id"], t["version_id"]))
            c.execute("DELETE FROM idx_tokens WHERE entry_id=? AND version_id=?",
                      (t["entry_id"], t["version_id"]))
            for field, sub_id, text in docs:
                norm, offs, rules = normalize(text)
                c.execute("INSERT INTO idx_norm(entry_id,version_id,field,sub_id,text,norm_text,"
                          "offsets,rules) VALUES(?,?,?,?,?,?,?,?)",
                          (t["entry_id"], t["version_id"], field, sub_id, text, norm,
                           json.dumps(offs), json.dumps(rules, ensure_ascii=False)))
                for tok, a, b in tokenize(text):
                    c.execute("INSERT INTO idx_tokens(token,entry_id,version_id,field,sub_id,"
                              "start,end) VALUES(?,?,?,?,?,?,?)",
                              (tok, t["entry_id"], t["version_id"], field, sub_id, a, b))
            c.execute("UPDATE index_tasks SET status='done', done_at=? WHERE id=?",
                      (time.time(), task_id))
        self.advance_pointer()
        return True

    def advance_pointer(self):
        """水位线 = 不存在未完成任务的最大版本前缀。"""
        minp = self.db.one("SELECT MIN(version_id) m FROM index_tasks WHERE status='pending'")["m"]
        maxv = self.db.one("SELECT MAX(version_id) m FROM index_tasks")["m"] or 0
        target = maxv if minp is None else minp - 1
        target = min(target, self.db.get_pointer("published"))
        if target != self.db.get_pointer("indexed"):
            self.db.set_pointer("indexed", target)

    def pending(self):
        return self.db.query("SELECT * FROM index_tasks WHERE status='pending' ORDER BY id")

    # ---- 后台模式：随机取任务，天然乱序完成 ----
    def start_background(self, interval=0.05):
        def loop():
            while not self._stop.is_set():
                rows = self.pending()
                if rows:
                    self.run_task(random.choice(rows)["id"])   # 随机选择 → 完成顺序乱序
                else:
                    time.sleep(interval)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
