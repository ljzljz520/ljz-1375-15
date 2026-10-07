# -*- coding: utf-8 -*-
"""验收测试：
1. 词条合并再拆分      2. 同音异义身份独立    3. 删除被引用义项
4. 索引任务乱序完成    5. 音频撤权            6. 标音不可转换→未转换
7. 规范化匹配与可解释高亮  8. 缓存版本隔离（旧解释不配新例句）  9. 角色分离
"""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import DB
from core.service import Service, Forbidden
from core.indexer import Indexer
from core import search as search_mod

class Base(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.svc = Service(self.db)
        self.idx = Indexer(self.db, self.svc)
        self.db.execute("INSERT INTO users VALUES('ed','ed-token','editor')")
        self.db.execute("INSERT INTO users VALUES('ap','ap-token','approver')")

    def mk_entry(self, hw, pron=None, senses=()):
        eid = self.svc.create_entry("ed", hw)
        if pron:
            self.svc.add_pron("ed", eid, "yd", pron)
        sids = [self.svc.add_sense("ed", eid, i + 1, d) for i, d in enumerate(senses)]
        return eid, sids

    def publish_and_index(self, note=""):
        vid = self.svc.publish("ap", note)
        for t in list(self.idx.pending()):
            self.idx.run_task(t["id"])
        return vid

# 1 ---------------------------------------------------------------
class TestMergeThenSplit(Base):
    def test_merge_then_split(self):
        a, (sa,) = self.mk_entry("甲", senses=["甲义"])
        b, (sb,) = self.mk_entry("乙", senses=["乙义"])
        v1 = self.publish_and_index("v1 两词独立")

        self.svc.merge_entries("ed", b, a)          # 乙并入甲
        v2 = self.publish_and_index("v2 合并")
        va = self.svc.entry_view(a, v2)
        self.assertCountEqual([s["id"] for s in va["senses"]], [sa, sb])  # 义项迁入，id 不变
        vb = self.svc.entry_view(b, v2)
        self.assertEqual(vb["status"], "merged")
        self.assertEqual(vb["merged_into"], a)

        self.svc.split_entry("ed", b, [sb])          # 再拆分：乙恢复，义项迁回
        v3 = self.publish_and_index("v3 拆分")
        vb3 = self.svc.entry_view(b, v3)
        self.assertEqual(vb3["status"], "active")
        self.assertEqual(vb3["split_from"], a)
        self.assertEqual([s["id"] for s in vb3["senses"]], [sb])
        va3 = self.svc.entry_view(a, v3)
        self.assertEqual([s["id"] for s in va3["senses"]], [sa])

        # 旧深链接仍然有效且内容不变（版本化闭包）
        va_v2 = self.svc.entry_view(a, v2)
        self.assertCountEqual([s["id"] for s in va_v2["senses"]], [sa, sb])
        va_v1 = self.svc.entry_view(a, v1)
        self.assertEqual([s["id"] for s in va_v1["senses"]], [sa])
        # 关系边在各自版本中可见
        self.assertIn("split_from", vb3["relations"])

# 2 ---------------------------------------------------------------
class TestHomophonePolysemy(Base):
    def test_homophone_identity_not_pinyin(self):
        e1, _ = self.mk_entry("生", pron="saang1", senses=["出生"])
        e2, _ = self.mk_entry("甥", pron="saang1", senses=["外甥"])
        self.svc.add_relation("ed", e1, e2, "homophone", "同音异义")
        self.publish_and_index()
        # 拼音相同但身份独立：检索 saang1 命中两个不同词条
        r = search_mod.search(self.db, self.svc, "saang1")
        ids = {h["entry_id"] for h in r["hits"]}
        self.assertEqual(ids, {e1, e2})
        # 修改其一标音不影响另一个：身份从不靠拼音
        pid = self.db.one("SELECT id FROM prons WHERE entry_id=?", (e1,))["id"]
        self.svc._bump("prons", pid, value="sang1")
        self.assertEqual(self.svc._cur("entries", e1)["id"], e1)
        self.assertEqual(self.svc._cur("entries", e2)["id"], e2)
        v = self.svc.entry_view(e1, self.publish_and_index("改标音"))
        self.assertIn("homophone", v["relations"])
        self.assertEqual(v["relations"]["homophone"][0]["headword"], "甥")

# 3 ---------------------------------------------------------------
class TestDeleteCitedSense(Base):
    def test_delete_cited_sense(self):
        e, (s1, s2) = self.mk_entry("头", senses=["脑袋（旧释）", "首领"])
        cid = self.svc.create_citation("ed", "https://example.org/paper#2.1", s1)
        v1 = self.publish_and_index("v1")
        # 编辑删除被引用义项并发布
        self.svc.delete_sense("ed", s1)
        v2 = self.publish_and_index("v2 删除义项1")
        # 当前词条视图不再含该义项
        view = self.svc.entry_view(e, v2)
        self.assertEqual([s["id"] for s in view["senses"]], [s2])
        # 引用解析：不 404、不张冠李戴，返回存档 + removed 状态
        r = self.svc.resolve_citation(cid)
        self.assertEqual(r["status"], "removed")
        self.assertEqual(r["archived_sense"]["definition"], "脑袋（旧释）")
        self.assertIn("移除", r["note"])
        # 旧版本深链接仍能看到被删前的义项
        old = self.svc.entry_view(e, v1)
        self.assertEqual(len(old["senses"]), 2)
        # 修改（非删除）被引用义项 → updated 状态，存档与现行并存
        cid2 = self.svc.create_citation("ed", "https://example.org/paper#2.2", s2)
        self.svc.update_sense("ed", s2, definition="首领（修订：兼指带头人）")
        self.publish_and_index("v3 修订义项2")
        r2 = self.svc.resolve_citation(cid2)
        self.assertEqual(r2["status"], "updated")
        self.assertEqual(r2["archived_sense"]["definition"], "首领")
        self.assertEqual(r2["current_sense"]["definition"], "首领（修订：兼指带头人）")

# 4 ---------------------------------------------------------------
class TestOutOfOrderIndexing(Base):
    def test_out_of_order_and_stale_tasks(self):
        a, _ = self.mk_entry("阿大", senses=["老大"])
        b, _ = self.mk_entry("阿二", senses=["老二"])
        v1 = self.svc.publish("ap", "v1")
        tasks_v1 = [t["id"] for t in self.idx.pending()]
        self.assertEqual(len(tasks_v1), 2)
        # 乱序完成：先跑后派发的任务
        self.idx.run_task(tasks_v1[1])
        self.assertEqual(self.db.get_pointer("indexed"), 0)   # 水位线不动
        self.idx.run_task(tasks_v1[0])
        self.assertEqual(self.db.get_pointer("indexed"), v1)  # 齐才推进
        # v2 修改两词并发布；跨版本乱序：先完成 v2 全部任务
        self.svc.update_sense("ed", self.db.one(
            "SELECT id FROM senses WHERE entry_id=?", (a,))["id"], definition="老大（修订）")
        v2 = self.svc.publish("ap", "v2")
        tasks_v2 = [t["id"] for t in self.idx.pending()]
        for tid in tasks_v2:
            self.idx.run_task(tid)
        # v1 已全部完成，v2 也完成 → 水位线直接到 v2
        self.assertEqual(self.db.get_pointer("indexed"), v2)
        r = search_mod.search(self.db, self.svc, "修订")
        self.assertEqual(len(r["hits"]), 1)
        # 模拟 v1 的迟到任务（乱序+过期）：重复执行旧任务不得污染当前检索
        stale = self.db.one("SELECT id FROM index_tasks WHERE version_id=? LIMIT 1", (v1,))
        self.db.execute("UPDATE index_tasks SET status='pending' WHERE id=?", (stale["id"],))
        self.idx.run_task(stale["id"])
        self.assertEqual(self.db.get_pointer("indexed"), v2)  # 水位线不回退
        r2 = search_mod.search(self.db, self.svc, "修订")
        self.assertEqual(len(r2["hits"]), 1)                  # 仍是 v2 内容
        r_old = search_mod.search(self.db, self.svc, "老大", version_id=v1)
        self.assertEqual(r_old["hits"][0]["text"], "老大")     # 旧版本文档仍可按需显式查询

    def test_watermark_waits_for_earlier_version(self):
        a, _ = self.mk_entry("先", senses=["一"])
        v1 = self.svc.publish("ap", "v1")
        self.svc.update_entry("ed", a, headword="先（改）")
        v2 = self.svc.publish("ap", "v2")
        # 先完成 v2 任务，v1 任务压着不动 → 水位线停在 0
        for t in [t for t in self.idx.pending() if t["version_id"] == v2]:
            self.idx.run_task(t["id"])
        self.assertEqual(self.db.get_pointer("indexed"), 0)
        for t in list(self.idx.pending()):
            self.idx.run_task(t["id"])
        self.assertEqual(self.db.get_pointer("indexed"), v2)  # 补齐后跳到 v2

# 5 ---------------------------------------------------------------
class TestAudioRevocation(Base):
    def test_revocation_effective_immediately_everywhere(self):
        e, (s,) = self.mk_entry("鸡", senses=["家禽"])
        m = self.svc.add_media("ed", "/media/gai1.wav", transcript="录音文本：鸡乸啼。")
        self.svc.add_example("ed", s, "鸡乸啼咯！", "母鸡叫了！", media_id=m)
        v1 = self.publish_and_index("v1")
        view = self.svc.entry_view(e)
        ex = view["senses"][0]["examples"][0]
        self.assertEqual(ex["media"]["url"], "/media/gai1.wav")
        self.assertEqual(ex["media_status"], "ok")
        # 撤权（approver 职权）
        self.svc.revoke_media("ap", m)
        for vid in (v1, None):                      # 历史版本视图同样即时生效
            v = self.svc.entry_view(e, vid)
            ex2 = v["senses"][0]["examples"][0]
            self.assertIsNone(ex2["media"])                  # 不再给出音频地址
            self.assertEqual(ex2["media_status"], "revoked")
            self.assertEqual(ex2["transcript"], "录音文本：鸡乸啼。")  # 无音频文本兜底
        # 缓存不得继续提供旧音频链接：连续读取结果一致
        again = self.svc.entry_view(e, v1)
        self.assertIsNone(again["senses"][0]["examples"][0]["media"])

# 6 ---------------------------------------------------------------
class TestUnconvertible(Base):
    def test_unconvertible_shows_not_converted(self):
        e, _ = self.mk_entry("白", pron="baak6", senses=["白色"])
        pid = self.db.one("SELECT id FROM prons WHERE entry_id=?", (e,))["id"]
        r1 = self.svc.convert_pron(pid, "ipa")
        self.assertEqual(r1["status"], "ok")
        self.assertEqual(r1["value"], "paːk̚˨")
        r2 = self.svc.convert_pron(pid, "plain")     # 入声尾 → 不可无损转换
        self.assertEqual(r2["status"], "unconvertible")
        self.assertIsNone(r2["value"])               # 不猜读
        self.assertIn("入声", r2["detail"])
        # 原始记录不变
        self.assertEqual(self.svc._cur("prons", pid)["value"], "baak6")
        # 未收录韵母
        e2, _ = self.mk_entry("屑", pron="xeo3", senses=["碎末"])
        pid2 = self.db.one("SELECT id FROM prons WHERE entry_id=?", (e2,))["id"]
        r3 = self.svc.convert_pron(pid2, "ipa")
        self.assertEqual(r3["status"], "unconvertible")
        # 词条视图携带转换状态，前端据以显示「未转换」
        self.publish_and_index()
        v = self.svc.entry_view(e2)
        conv = v["prons"][0]["conversions"][0]
        self.assertEqual(conv["status"], "unconvertible")

# 7 ---------------------------------------------------------------
class TestNormalizationAndHighlight(Base):
    def test_simp_trad_punct_combining(self):
        e, (s,) = self.mk_entry("臺", senses=["臺灣亦省作台"])
        self.svc.add_example("ed", s, "佢今日去咗街市買餸，好開心！")
        p = self.svc.add_pron("ed", e, "ipa", "kʰɛ́˥")
        self.publish_and_index()
        # 简体查繁体
        r = search_mod.search(self.db, self.svc, "台湾")
        self.assertTrue(any(h["field"] == "sense" for h in r["hits"]))
        hit = [h for h in r["hits"] if h["field"] == "sense"][0]
        self.assertIn("trad2simp", hit["explain"])           # 可解释
        a, b = hit["spans"][0]
        self.assertEqual(hit["text"][a:b], "臺灣")            # 高亮落在原文坐标
        # 跨标点
        r2 = search_mod.search(self.db, self.svc, "买餸好开心")
        self.assertTrue(any(h["field"] == "example" for h in r2["hits"]))
        # 音标组合字符：khe 命中 kʰɛ́˥
        r3 = search_mod.search(self.db, self.svc, "khe")
        ph = [h for h in r3["hits"] if h["field"] == "pron"][0]
        a, b = ph["spans"][0]
        self.assertEqual(ph["text"][a:b], "kʰɛ́")
        self.assertIn("strip_combining", ph["explain"])
        # 原文索引查不到繁简差异 → compare 展示差集
        cmp = search_mod.compare(self.db, self.svc, "台湾")
        self.assertEqual(cmp["only_field"], [])
        self.assertTrue(cmp["only_norm"])
        # 原文索引精确命中仍可用
        cmp2 = search_mod.compare(self.db, self.svc, "臺灣")
        self.assertTrue(cmp2["both"])

# 8 ---------------------------------------------------------------
class TestCacheVersionIsolation(Base):
    def test_old_def_never_pairs_new_example(self):
        e, (s,) = self.mk_entry("灯", senses=["旧释：油灯"])
        self.svc.add_example("ed", s, "旧例：点灯读书。")
        v1 = self.publish_and_index("v1")
        view1a = self.svc.entry_view(e, v1)          # 填充缓存
        # 修改义项与例句，发布 v2
        self.svc.update_sense("ed", s, definition="新释：电灯")
        xid = self.db.one("SELECT id FROM examples")["id"]
        self.svc.update_example("ed", xid, text="新例：开灯。")
        v2 = self.publish_and_index("v2")
        view2 = self.svc.entry_view(e)               # 当前 = v2
        self.assertEqual(view2["senses"][0]["definition"], "新释：电灯")
        self.assertEqual(view2["senses"][0]["examples"][0]["text"], "新例：开灯。")
        view1b = self.svc.entry_view(e, v1)          # 旧深链接：必须旧释配旧例
        self.assertEqual(view1b["senses"][0]["definition"], "旧释：油灯")
        self.assertEqual(view1b["senses"][0]["examples"][0]["text"], "旧例：点灯读书。")
        self.assertIsNot(view1a, view2)              # 缓存键含版本号

# 9 ---------------------------------------------------------------
class TestRoles(Base):
    def test_role_separation(self):
        with self.assertRaises(Forbidden):
            self.svc.require("bad-token", ("editor",))
        ed = self.svc.require("ed-token", ("editor", "approver"))
        self.assertEqual(ed["role"], "editor")
        with self.assertRaises(Forbidden):           # 编辑不能充当批准
            self.svc.require("ed-token", ("approver",))
        ap = self.svc.require("ap-token", ("approver",))
        self.assertEqual(ap["role"], "approver")

if __name__ == "__main__":
    unittest.main(verbosity=2)
