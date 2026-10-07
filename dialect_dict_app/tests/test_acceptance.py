# -*- coding: utf-8 -*-
"""验收测试（stdlib unittest）：

1. 词条合并再拆分（含关系边/标音还原）
2. 同音异义：身份不靠拼音，候选提示 + 人工关系边
3. 删除被引用义项：文章按版本取存档，旧深链接可用
4. 索引任务乱序完成：版本守卫收敛到最新版
5. 音频撤权：公开页即时隐藏音频、保留文本
6. 缓存版本隔离：旧解释绝不配新例句
7. 标音方案切换：保留原始记录，不可无损转换时显示未转换
8. 规范化检索 vs 原文索引：简繁/标点/音标组合字符 + 可解释高亮
9. 角色分离：编辑与批准互相不能越权
10. 词库持久化：重启（重开库）后数据仍在
11. 地区变体与异体字关系边进入发布闭包
12. HTTP 冒烟：公开检索/词条/权限
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dialect_dict import db, services as S, seed as seed_mod
from dialect_dict import publish as publish_mod
from dialect_dict.indexer import Indexer
from dialect_dict import search as search_mod
from dialect_dict.api import create_server

ED = {"id": "u_ed", "role": "editor", "name": "ed"}
AP = {"id": "u_ap", "role": "approver", "name": "ap"}


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        publish_mod.clear_cache()

    def tearDown(self):
        self.conn.close()

    # -- helpers ---------------------------------------------------------
    def make_entry(self, headword, prons=(), senses=()):
        eid = S.create_entry(self.conn, ED, headword)
        for scheme, text in prons:
            S.add_pronunciation(self.conn, ED, eid, scheme, text)
        sids = []
        for d in senses:
            sids.append(S.add_sense(self.conn, ED, eid, d))
        return eid, sids

    def publish(self, eid):
        e = S.get_entry(self.conn, eid)
        if e["status"] == "draft":
            S.submit_entry(self.conn, ED, eid)
            S.approve_entry(self.conn, AP, eid)
        elif e["status"] == "review":
            S.approve_entry(self.conn, AP, eid)
        return S.publish_entry(self.conn, AP, eid)

    def indexer(self):
        return Indexer(lambda: self.conn, shared=True)


class TestMergeSplit(Base):
    def test_01_merge_then_split_restores(self):
        ea, (sa,) = self.make_entry("甲", [("jyutping", "aa1")], ["甲义"])
        pa = self.conn.execute(
            "SELECT id FROM pronunciations WHERE entry_id=?", (ea,)).fetchone()
        ec, _ = self.make_entry("丙")
        rid = S.add_relation(self.conn, ED, ea, ec, "homophone", "同音")
        eb, (sb,) = self.make_entry("乙", senses=["乙义"])

        # 合并 甲 -> 乙
        S.merge_entries(self.conn, ED, ea, eb)
        a = S.get_entry(self.conn, ea)
        self.assertEqual(a["status"], "merged")
        self.assertEqual(a["merged_into"], eb)
        self.assertEqual(self.conn.execute(
            "SELECT entry_id FROM senses WHERE id=?", (sa,)).fetchone()[0], eb)
        self.assertEqual(self.conn.execute(
            "SELECT entry_id FROM pronunciations WHERE id=?",
            (pa[0],)).fetchone()[0], eb)
        self.assertEqual(self.conn.execute(
            "SELECT from_entry FROM relations WHERE id=?",
            (rid,)).fetchone()[0], eb)
        # 旧深链接 -> 重定向
        redir = publish_mod.serve_public(self.conn, ea)
        self.assertEqual(redir["redirect"], eb)

        # 再拆分：复活 甲 并还原义项/标音/关系边
        out = S.split_entry(self.conn, ED, eb, [sa], restore_entry_id=ea)
        self.assertEqual(out["entry_id"], ea)
        a = S.get_entry(self.conn, ea)
        self.assertEqual(a["status"], "draft")
        self.assertIsNone(a["merged_into"])
        self.assertEqual(self.conn.execute(
            "SELECT entry_id FROM senses WHERE id=?", (sa,)).fetchone()[0], ea)
        self.assertEqual(self.conn.execute(
            "SELECT entry_id FROM pronunciations WHERE id=?",
            (pa[0],)).fetchone()[0], ea)
        self.assertEqual(self.conn.execute(
            "SELECT from_entry FROM relations WHERE id=?",
            (rid,)).fetchone()[0], ea)
        # 乙 保留自己的义项
        self.assertEqual(self.conn.execute(
            "SELECT entry_id FROM senses WHERE id=?", (sb,)).fetchone()[0], eb)


class TestHomophone(Base):
    def test_02_homophone_identity_not_pinyin(self):
        e1, _ = self.make_entry("芳", [("jyutping", "fong1")], ["香"])
        e2, _ = self.make_entry("方", [("jyutping", "fong1")], ["方向"])
        # 同音但身份独立，绝不自动合并
        self.assertNotEqual(e1, e2)
        self.assertEqual(S.get_entry(self.conn, e1)["status"], "draft")
        self.assertEqual(S.get_entry(self.conn, e2)["status"], "draft")
        # 拼音仅用于提示候选
        cands = S.homophone_candidates(self.conn, e1)
        self.assertEqual([c["entry_id"] for c in cands], [e2])
        # 人工确认关系边
        S.add_relation(self.conn, ED, e1, e2, "homophone", "fong1 同音异义")
        rels = S.list_relations(self.conn, e1)
        self.assertEqual(rels[0]["type"], "homophone")
        self.assertEqual(rels[0]["other"]["id"], e2)
        # 建边后不再是"候选"
        self.assertEqual(S.homophone_candidates(self.conn, e1), [])


class TestDeleteReferencedSense(Base):
    def test_03_delete_referenced_sense(self):
        eid, (sid,) = self.make_entry("佢", senses=["第三人称代词（v1 释义）"])
        aid = S.create_article(self.conn, ED, "代词考", "正文",
                               [{"sense_id": sid, "sense_version": 1}])
        S.update_sense(self.conn, ED, sid, definition="第三人称代词（v2 修订）")
        v1 = self.publish(eid)                       # 发布含 v2 的闭包
        res = S.delete_sense(self.conn, ED, sid)
        self.assertEqual(res["cited_by"], 1)
        # 文章仍解析到引用时的 v1
        art = S.get_article(self.conn, aid)
        self.assertEqual(art["citations"][0]["definition"], "第三人称代词（v1 释义）")
        self.assertEqual(art["citations"][0]["sense_status"], "deleted")
        self.assertIn("存档", art["citations"][0]["note"])
        # 旧深链接：存档视图 + 删除说明
        view = S.public_sense_view(self.conn, sid)
        self.assertEqual(view["status"], "deleted")
        self.assertIn("已删除", view["message"])
        # 再发布：新闭包不含已删义项；旧版闭包仍含
        v2 = self.publish(eid)
        newc = publish_mod.serve_public(self.conn, eid)["closure"]
        self.assertEqual(newc["senses"], [])
        oldc = publish_mod.serve_public(self.conn, eid, v1)["closure"]
        self.assertEqual(oldc["senses"][0]["id"], sid)
        self.assertNotEqual(v1, v2)


class TestIndexerOutOfOrder(Base):
    def test_04_out_of_order_completion(self):
        eid, _ = self.make_entry("初版词形")
        self.publish(eid)                                    # task v_a
        S.update_entry(self.conn, ED, eid, headword="二版词形")
        self.publish(eid)                                    # task v_b
        S.update_entry(self.conn, ED, eid, headword="最终词形")
        self.publish(eid)                                    # task v_c
        idx = self.indexer()
        tasks = idx.pending()
        self.assertEqual(len(tasks), 3)
        t1, t2, t3 = [t["id"] for t in tasks]
        # 乱序完成：t2 -> t3 -> t1
        self.assertEqual(idx.complete(t2), "applied")
        self.assertEqual(idx.complete(t3), "applied")
        self.assertEqual(idx.complete(t1), "superseded")
        # 索引收敛到最高版本
        self.assertEqual(idx.applied_version(eid), tasks[2]["version"])
        raw = self.conn.execute(
            "SELECT raw FROM index_docs WHERE entry_id=? AND kind='headword'",
            (eid,)).fetchone()[0]
        self.assertEqual(raw, "最终词形")
        # 场景二：最高版本先完成，其余全部作废
        e2, _ = self.make_entry("甲一")
        self.publish(e2)
        S.update_entry(self.conn, ED, e2, headword="甲二")
        self.publish(e2)
        S.update_entry(self.conn, ED, e2, headword="甲三")
        self.publish(e2)
        ts = [t["id"] for t in idx.pending()]
        self.assertEqual(idx.complete(ts[2]), "applied")
        self.assertEqual(idx.complete(ts[0]), "superseded")
        self.assertEqual(idx.complete(ts[1]), "superseded")
        raw = self.conn.execute(
            "SELECT raw FROM index_docs WHERE entry_id=? AND kind='headword'",
            (e2,)).fetchone()[0]
        self.assertEqual(raw, "甲三")


class TestAudioRevoke(Base):
    def test_05_audio_revocation(self):
        eid, (sid,) = self.make_entry("佢", senses=["他"])
        xid = S.add_example(self.conn, ED, sid, "佢唔嚟。", "他不来。")
        auid = S.attach_audio(self.conn, ED, xid, "/static/audio/a.wav",
                              "佢唔嚟（keoi5 m4 lai4）")
        self.publish(eid)
        served = publish_mod.serve_public(self.conn, eid)
        au = served["closure"]["senses"][0]["examples"][0]["audio"]
        self.assertEqual(au["url"], "/static/audio/a.wav")
        # 撤权后立即生效（实时策略叠加，无需重新发布）
        S.revoke_audio(self.conn, AP, auid, "授权到期")
        served = publish_mod.serve_public(self.conn, eid)
        ex = served["closure"]["senses"][0]["examples"][0]
        self.assertIsNone(ex["audio"]["url"])
        self.assertTrue(ex["audio_revoked"])
        self.assertEqual(ex["audio"]["transcript"], "佢唔嚟（keoi5 m4 lai4）")
        # 缓存原件未被污染：再次读取结果一致
        again = publish_mod.serve_public(self.conn, eid)
        self.assertIsNone(again["closure"]["senses"][0]["examples"][0]["audio"]["url"])
        # 编辑不能撤权
        with self.assertRaises(PermissionError):
            S.revoke_audio(self.conn, ED, auid, "越权")


class TestCacheVersionIsolation(Base):
    def test_06_old_definition_never_pairs_new_example(self):
        eid, (sid,) = self.make_entry("食饭", senses=["旧解释：吃饭"])
        xid = S.add_example(self.conn, ED, sid, "旧例句。")
        v1 = self.publish(eid)
        S.update_sense(self.conn, ED, sid, definition="新解释：进餐")
        S.update_example(self.conn, ED, xid, text="新例句。")
        v2 = self.publish(eid)
        c1 = publish_mod.serve_public(self.conn, eid, v1)["closure"]
        c2 = publish_mod.serve_public(self.conn, eid, v2)["closure"]
        self.assertEqual(c1["senses"][0]["definition"], "旧解释：吃饭")
        self.assertEqual(c1["senses"][0]["examples"][0]["text"], "旧例句。")
        self.assertEqual(c2["senses"][0]["definition"], "新解释：进餐")
        self.assertEqual(c2["senses"][0]["examples"][0]["text"], "新例句。")
        # 任意版本内部不得交叉
        for c in (c1, c2):
            d = c["senses"][0]["definition"]
            x = c["senses"][0]["examples"][0]["text"]
            self.assertEqual(d.startswith("旧"), x.startswith("旧"))


class TestRomanization(Base):
    def test_07_lossless_or_unconverted_never_guess(self):
        eid, _ = self.make_entry("食饭", [("jyutping", "sik6 faan6")])
        # yale 表缺 faan6 -> 整条未转换
        made = S.convert_pronunciations(self.conn, ED, eid, "yale")
        self.assertEqual(made[0]["status"], "unconverted")
        self.assertIn("faan6", made[0]["missing"])
        row = self.conn.execute(
            "SELECT text, status FROM pronunciations WHERE scheme='yale'").fetchone()
        self.assertIsNone(row["text"])
        self.assertEqual(row["status"], "unconverted")
        # 原始记录仍在，展示回退原始记录并标注未转换
        disp = S.pronunciation_display(self.conn, eid, "yale")
        self.assertTrue(disp[0]["unconverted"])
        self.assertEqual(disp[0]["text"], "sik6 faan6")
        # ipa 表完整 -> 无损转换成功
        made = S.convert_pronunciations(self.conn, ED, eid, "ipa")
        self.assertEqual(made[0]["status"], "ok")
        disp = S.pronunciation_display(self.conn, eid, "ipa")
        self.assertFalse(disp[0]["unconverted"])
        self.assertEqual(disp[0]["text"], "sɪk̚˨ faːn˨")
        # 原始记录未被修改
        rec = self.conn.execute(
            "SELECT text FROM pronunciations WHERE origin='recorded'").fetchone()
        self.assertEqual(rec["text"], "sik6 faan6")


class TestSearch(Base):
    def _seeded(self):
        seed_mod.seed(self.conn)
        idx = self.indexer()
        idx.complete_all()
        return idx

    def test_08_normalization_and_explainable_highlight(self):
        self._seeded()
        # 简繁：简体查询命中繁体词形
        r = search_mod.search(self.conn, "龙舟")
        self.assertEqual(r[0]["headword"], "龍舟")
        m = [m for m in r[0]["matches"] if m["kind"] == "headword"][0]
        self.assertEqual(m["via"], "normalized")
        self.assertIn("trad", m["folds"])
        self.assertEqual(m["spans"], [[0, 2]])
        self.assertEqual(m["raw"][0:2], "龍舟")
        self.assertIn("简繁", m["explanation"])
        # 标点：无标点查询命中带标点例句
        r = search_mod.search(self.conn, "唔该唔该")
        hit = [x for x in r if x["headword"] == "唔該"][0]
        m = [m for m in hit["matches"] if m["kind"] == "example"][0]
        self.assertIn("punct", m["folds"])
        self.assertIn("trad", m["folds"])
        s, e = m["spans"][0]
        self.assertEqual(m["raw"][s:e], "唔該，唔該")
        # 音标组合字符：'ma' 命中预组合 'mā'
        r = search_mod.search(self.conn, "ma")
        hit = [x for x in r if x["headword"] == "妈"][0]
        m = hit["matches"][0]
        self.assertEqual(m["raw"], "mā")
        self.assertIn("dia", m["folds"])
        self.assertIn("组合字符", m["explanation"])
        # 原文索引：繁体查询精确命中词形
        r = search_mod.search(self.conn, "龍舟", mode="raw")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["matches"][0]["via"], "exact")
        # 简体查询在原文模式下打不到繁体词形，只能命中文中的简体译文
        r = search_mod.search(self.conn, "龙舟", mode="raw")
        kinds = {m["kind"] for x in r for m in x["matches"]}
        self.assertNotIn("headword", kinds)
        self.assertIn("translation", kinds)
        # 规范化模式才能命中繁体词形
        r = search_mod.search(self.conn, "龙舟", mode="norm")
        kinds = {m["kind"] for x in r for m in x["matches"]}
        self.assertIn("headword", kinds)
        # 双索引对比：'雪柜' 原文模式零命中，规范化命中繁体词形'雪櫃'
        cmp = search_mod.compare(self.conn, "雪柜")
        self.assertEqual(cmp["raw"]["count"], 0)
        self.assertGreaterEqual(cmp["norm"]["count"], 1)
        self.assertIn("雪櫃", cmp["only_in_norm"])

    def test_11_regional_and_variant_edges_in_closure(self):
        self._seeded()
        entries = {e["headword"]: e["id"] for e in S.list_entries(self.conn)}
        c = publish_mod.serve_public(self.conn, entries["佢"])["closure"]
        incoming = [r for r in c["relations"] if r["direction"] == "in"]
        self.assertEqual(incoming[0]["type"], "variant_char")
        self.assertEqual(incoming[0]["other"]["headword"], "渠")
        c = publish_mod.serve_public(self.conn, entries["雪櫃"])["closure"]
        rv = [r for r in c["relations"] if r["type"] == "regional_variant"]
        self.assertEqual(rv[0]["other"]["headword"], "冰箱")
        self.assertEqual(c["senses"][0]["region"], "香港")
        # 同音词互链
        c = publish_mod.serve_public(self.conn, entries["芳"])["closure"]
        hp = [r for r in c["relations"] if r["type"] == "homophone"]
        self.assertEqual(hp[0]["other"]["headword"], "方")


class TestRoles(Base):
    def test_09_role_separation(self):
        eid, (sid,) = self.make_entry("测试", senses=["义"])
        with self.assertRaises(PermissionError):
            S.create_entry(self.conn, AP, "越权词条")
        with self.assertRaises(PermissionError):
            S.update_sense(self.conn, AP, sid, definition="越权改")
        with self.assertRaises(PermissionError):
            S.publish_entry(self.conn, ED, eid)      # 编辑不能发布
        with self.assertRaises(PermissionError):
            S.approve_entry(self.conn, ED, eid)      # 编辑不能批准
        # 正常流程：编辑提交 -> 审校批准 -> 审校发布
        S.submit_entry(self.conn, ED, eid)
        S.approve_entry(self.conn, AP, eid)
        v = S.publish_entry(self.conn, AP, eid)
        self.assertGreater(v, 0)
        self.assertEqual(S.get_entry(self.conn, eid)["status"], "published")


class TestPersistence(Base):
    def test_10_lexicon_lives_in_server_db(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "d.db")
            c1 = db.connect(path)
            db.init_db(c1)
            eid = S.create_entry(c1, ED, "持久词")
            S.add_sense(c1, ED, eid, "重启后仍在")
            c1.commit()
            c1.close()
            # 模拟重启：全新连接
            c2 = db.connect(path)
            e = S.get_entry(c2, eid)
            self.assertEqual(e["headword"], "持久词")
            d = S.get_entry_detail(c2, eid)
            self.assertEqual(d["senses"][0]["definition"], "重启后仍在")
            c2.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestHttpSmoke(Base):
    def test_12_http_end_to_end(self):
        tmp = tempfile.mkdtemp()
        static = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static")
        try:
            path = os.path.join(tmp, "d.db")
            conn = db.connect(path)
            db.init_db(conn)
            seed_mod.seed(conn)
            conn.close()
            Indexer(lambda: db.connect(path)).complete_all()
            srv = create_server(path, static, port=0)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def req(method, url, token=None, body=None):
                    c = http.client.HTTPConnection("127.0.0.1", port)
                    headers = {"Content-Type": "application/json"}
                    if token:
                        headers["Authorization"] = "Bearer " + token
                    c.request(method, url,
                              json.dumps(body) if body is not None else None,
                              headers)
                    r = c.getresponse()
                    data = json.loads(r.read() or b"{}")
                    c.close()
                    return r.status, data

                st, res = req("GET", "/api/public/search?q=%E9%BE%99%E8%88%9F")
                self.assertEqual(st, 200)
                self.assertEqual(res["results"][0]["headword"], "龍舟")
                eid = res["results"][0]["entry_id"]
                st, res = req("GET", "/api/public/entries/" + eid)
                self.assertEqual(st, 200)
                self.assertIn("respect_notice", res)
                st, res = req("GET", "/api/public/entries/%s/versions" % eid)
                self.assertTrue(res["versions"])
                # 未登录 / 越权
                st, _ = req("POST", "/api/entries", body={"headword": "x"})
                self.assertEqual(st, 401)
                st, _ = req("POST", "/api/entries", token="tok-approver",
                            body={"headword": "x"})
                self.assertEqual(st, 403)
                # 编辑建词条 -> 提交；审校批准 -> 发布
                st, res = req("POST", "/api/entries", token="tok-editor",
                              body={"headword": "新词"})
                self.assertEqual(st, 200)
                nid = res["id"]
                st, _ = req("POST", "/api/entries/%s/submit" % nid,
                            token="tok-editor")
                self.assertEqual(st, 200)
                st, _ = req("POST", "/api/entries/%s/publish" % nid,
                            token="tok-editor")
                self.assertEqual(st, 403)            # 编辑不能发布
                req("POST", "/api/entries/%s/approve" % nid, token="tok-approver")
                st, res = req("POST", "/api/entries/%s/publish" % nid,
                              token="tok-approver")
                self.assertEqual(st, 200)
            finally:
                srv.shutdown()
                srv.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
