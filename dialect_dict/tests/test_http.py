# -*- coding: utf-8 -*-
"""HTTP 集成：真实起服务，验证角色强制、公开端点、深链接、撤权即时生效。"""
import json, os, sys, threading, time, unittest, http.client, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import DB
from core.service import Service
from core.indexer import Indexer
from core.seed import seed
from core.api import serve

class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = DB(":memory:")
        cls.svc = Service(cls.db)
        seed(cls.db, cls.svc)
        cls.svc.publish("approver1", "v1")
        cls.idx = Indexer(cls.db, cls.svc)
        for t in list(cls.idx.pending()):
            cls.idx.run_task(t["id"])
        cls.srv = serve(cls.svc, cls.idx, port=0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, method, path, body=None, token=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Token"] = token
        c.request(method, urllib.parse.quote(path, safe="/?=&"), json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = json.loads(r.read().decode("utf-8"))
        c.close()
        return r.status, data

    def test_public_search_and_entry(self):
        st, r = self.call("GET", "/api/search?q=" + "saang1")
        self.assertEqual(st, 200)
        heads = {h["headword"] for h in r["hits"]}
        self.assertEqual(heads, {"生", "甥"})          # 同音异义分别命中
        eid = r["hits"][0]["entry_id"]
        st, v = self.call("GET", f"/api/entry/{eid}")
        self.assertEqual(st, 200)
        self.assertIn("homophone", v["relations"])
        # 尊重性语境说明在公开词条可见
        st, r2 = self.call("GET", "/api/search?q=" + "契弟")
        qd = r2["hits"][0]["entry_id"]
        st, v2 = self.call("GET", f"/api/entry/{qd}")
        self.assertIn("不尊重", v2["context_note"])
        ex = v2["senses"][0]["examples"][0]
        self.assertEqual(ex["media_status"], "ok")
        self.assertTrue(ex["transcript"])            # 无音频文本始终可得

    def test_deep_link_and_unconvertible(self):
        st, r = self.call("GET", "/api/search?q=" + "白色")
        # 「白」在种子里定义是"颜色白；明白" —— 用繁体检索验证折叠
        st, r = self.call("GET", "/api/search?q=" + "臺灣")
        eid = r["hits"][0]["entry_id"]
        st, v1 = self.call("GET", f"/api/entry/{eid}?v=1")
        st, v2 = self.call("GET", f"/api/entry/{eid}")
        self.assertEqual(v1["version_id"], v2["version_id"])  # 目前只有 v1
        # 未转换：屑 的 IPA 转换
        st, r = self.call("GET", "/api/search?q=" + "碎末")
        eid = r["hits"][0]["entry_id"]
        st, v = self.call("GET", f"/api/entry/{eid}")
        conv = v["prons"][0]["conversions"][0]
        self.assertEqual(conv["status"], "unconvertible")
        self.assertIsNone(conv["value"])             # 前端据此显示「未转换」

    def test_role_enforcement_over_http(self):
        st, _ = self.call("POST", "/api/entries", {"headword": "X"})
        self.assertEqual(st, 403)                    # 未登录
        st, _ = self.call("POST", "/api/publish", {"note": "x"}, token="ed-token")
        self.assertEqual(st, 403)                    # 编辑不能发布
        st, r = self.call("POST", "/api/entries", {"headword": "测试词"}, token="ed-token")
        self.assertEqual(st, 200)
        st, r = self.call("POST", "/api/publish", {"note": "v2"}, token="ap-token")
        self.assertEqual(st, 200)
        self.assertEqual(r["version_id"], 2)

    def test_revocation_over_http_immediate(self):
        st, r = self.call("GET", "/api/search?q=" + "契弟")
        qd = r["hits"][0]["entry_id"]
        st, v = self.call("GET", f"/api/entry/{qd}")
        mid_url = v["senses"][0]["examples"][0]["media"]
        # 找到 media id：通过 draft 端点（编辑视角）
        st, d = self.call("GET", f"/api/draft/entry/{qd}", token="ed-token")
        mid = d["examples"][0]["media_id"]
        st, _ = self.call("POST", f"/api/media/{mid}/revoke", {}, token="ed-token")
        self.assertEqual(st, 403)                    # 撤权是审校职权
        st, _ = self.call("POST", f"/api/media/{mid}/revoke", {}, token="ap-token")
        self.assertEqual(st, 200)
        st, v2 = self.call("GET", f"/api/entry/{qd}")
        ex = v2["senses"][0]["examples"][0]
        self.assertIsNone(ex["media"])
        self.assertEqual(ex["media_status"], "revoked")
        self.assertTrue(ex["transcript"])            # 无音频文本兜底

    def test_compare_endpoint(self):
        st, r = self.call("GET", "/api/compare?q=" + "台湾")
        self.assertEqual(st, 200)
        self.assertTrue(r["only_norm"])              # 只有规范键能跨繁简命中
        self.assertEqual(r["only_field"], [])

if __name__ == "__main__":
    unittest.main(verbosity=2)
