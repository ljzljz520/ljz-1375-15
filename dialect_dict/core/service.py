# -*- coding: utf-8 -*-
"""领域服务层：词条/词形/标音/义项/例句/关系边/来源/媒体/引用/发布。

关键不变式：
- 词条身份 = entries.id（代理键），与拼音、词形无关。同音异义是两个词条 + homophone 边；
  异体字是同一词条下的多个 form（或跨词条 variant_char 边）；地区异义由 sense.region
  与 regional_variant 边表达。
- 一切修改产生新版本行（追加式），草稿态=各实体最大 version 行；发布后公开页只看闭包。
- 缓存键 = (entry_id, 发布版本号, rights_epoch)：旧解释与新例句永远不会拼进同一缓存项。
"""
import time, json, threading
from .db import DB, new_id
from . import phonology

KINDS = ["entries", "forms", "prons", "senses", "examples", "relations", "media"]

class Forbidden(Exception): pass
class NotFound(Exception): pass
class BadRequest(Exception): pass

class Service:
    def __init__(self, db: DB):
        self.db = db
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.rights_epoch = 0   # 音频撤权即递增 → 所有含媒体授权的缓存键失效

    # ---------- 认证 / 授权 ----------
    def user(self, token):
        if not token:
            return None
        return self.db.one("SELECT * FROM users WHERE token=?", (token,))

    def require(self, token, roles):
        u = self.user(token)
        if u is None:
            raise Forbidden("未登录或令牌无效")
        if u["role"] not in roles:
            raise Forbidden(f"需要角色 {roles}，当前为 {u['role']}")
        return u

    # ---------- 工作副本（草稿）读写 ----------
    def _cur(self, table, _id):
        return self.db.one(f"SELECT * FROM {table} WHERE id=? ORDER BY version DESC LIMIT 1", (_id,))

    def _bump(self, table, _id, **changes):
        old = self._cur(table, _id)
        if old is None:
            raise NotFound(f"{table}/{_id} 不存在")
        row = dict(old); row.update(changes); row["version"] = old["version"] + 1
        cols = ",".join(row.keys()); qs = ",".join("?" for _ in row)
        self.db.execute(f"INSERT INTO {table}({cols}) VALUES({qs})", tuple(row.values()))
        return self._cur(table, _id)

    def _insert(self, table, row: dict):
        cols = ",".join(row.keys()); qs = ",".join("?" for _ in row)
        self.db.execute(f"INSERT INTO {table}({cols}) VALUES({qs})", tuple(row.values()))
        return row

    # ---------- 词条 ----------
    def create_entry(self, actor, headword, context_note=""):
        eid = new_id("e")
        self._insert("entries", dict(id=eid, version=1, headword=headword,
            context_note=context_note, status="active", merged_into=None,
            split_from=None, updated_by=actor, updated_at=time.time()))
        self.db.audit(actor, "create_entry", eid)
        return eid

    def update_entry(self, actor, entry_id, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("headword", "context_note")}
        allowed.update(updated_by=actor, updated_at=time.time())
        self._bump("entries", entry_id, **allowed)
        self.db.audit(actor, "update_entry", f"{entry_id} {allowed}")

    def add_form(self, actor, entry_id, text, script="simp", is_standard=0, note=""):
        self._must_be_active_entry(entry_id)
        fid = new_id("f")
        self._insert("forms", dict(id=fid, version=1, entry_id=entry_id, text=text,
            script=script, is_standard=is_standard, note=note, deleted=0))
        return fid

    def add_pron(self, actor, entry_id, scheme, value, region="", source_id=None):
        self._must_be_active_entry(entry_id)
        if scheme not in phonology.SCHEMES:
            raise BadRequest(f"未知标音方案 {scheme}")
        pid = new_id("p")
        self._insert("prons", dict(id=pid, version=1, entry_id=entry_id, scheme=scheme,
            value=value, region=region, source_id=source_id, deleted=0))
        return pid

    def add_sense(self, actor, entry_id, num, definition, pos="", region=""):
        self._must_be_active_entry(entry_id)
        sid = new_id("s")
        self._insert("senses", dict(id=sid, version=1, entry_id=entry_id, num=num,
            pos=pos, definition=definition, region=region, deleted=0))
        return sid

    def update_sense(self, actor, sense_id, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("definition", "pos", "region", "num")}
        self._bump("senses", sense_id, **allowed)   # 版本+1；旧版本仍被引用/闭包钉住
        self.db.audit(actor, "update_sense", sense_id)

    def delete_sense(self, actor, sense_id):
        self._bump("senses", sense_id, deleted=1)   # 软删除：存档版本仍可被引用解析
        self.db.audit(actor, "delete_sense", sense_id)

    def add_example(self, actor, sense_id, text, translation="", region="", source_id=None, media_id=None):
        if self._cur("senses", sense_id) is None:
            raise NotFound("义项不存在")
        xid = new_id("x")
        self._insert("examples", dict(id=xid, version=1, sense_id=sense_id, text=text,
            translation=translation, region=region, source_id=source_id,
            media_id=media_id, deleted=0))
        return xid

    def update_example(self, actor, example_id, **fields):
        allowed = {k: v for k, v in fields.items()
                   if k in ("text", "translation", "region", "source_id", "media_id")}
        self._bump("examples", example_id, **allowed)

    def add_relation(self, actor, src, dst, rtype, note=""):
        rid = new_id("r")
        self._insert("relations", dict(id=rid, version=1, src=src, dst=dst,
            type=rtype, note=note, deleted=0))
        return rid

    def add_source(self, actor, title, author="", year=None, license=""):
        sid = new_id("src")
        self._insert("sources", dict(id=sid, title=title, author=author, year=year, license=license))
        return sid

    def add_media(self, actor, path, transcript=""):
        mid = new_id("m")
        self._insert("media", dict(id=mid, version=1, path=path, transcript=transcript, deleted=0))
        self.db.execute("INSERT OR IGNORE INTO media_rights(media_id,revoked,updated_at) VALUES(?,0,?)",
                        (mid, time.time()))
        return mid

    def _must_be_active_entry(self, entry_id):
        e = self._cur("entries", entry_id)
        if e is None:
            raise NotFound("词条不存在")
        if e["status"] != "active":
            raise BadRequest(f"词条状态为 {e['status']}，不可编辑子项")

    # ---------- 合并 / 拆分 ----------
    def merge_entries(self, actor, src_id, dst_id):
        """src 并入 dst：src 置 merged；src 的义项整体迁到 dst（义项 id 不变，引用不断）。"""
        src, dst = self._cur("entries", src_id), self._cur("entries", dst_id)
        if not src or not dst:
            raise NotFound("词条不存在")
        if src["status"] != "active" or dst["status"] != "active":
            raise BadRequest("仅 active 词条可合并")
        self._bump("entries", src_id, status="merged", merged_into=dst_id,
                   updated_by=actor, updated_at=time.time())
        for s in self.db.query("SELECT * FROM senses WHERE entry_id=? AND deleted=0", (src_id,)):
            if self._cur("senses", s["id"])["entry_id"] == src_id:
                self._bump("senses", s["id"], entry_id=dst_id)
        self.add_relation(actor, src_id, dst_id, "merged_into", f"由 {actor} 合并")
        self.db.audit(actor, "merge", f"{src_id} -> {dst_id}")

    def split_entry(self, actor, merged_id, sense_ids):
        """把已合并词条重新拆出：恢复 active，指定义项迁回；旧发布版本不受影响。"""
        e = self._cur("entries", merged_id)
        if not e or e["status"] != "merged":
            raise BadRequest("仅 merged 词条可拆分")
        former = e["merged_into"]
        self._bump("entries", merged_id, status="active", merged_into=None,
                   split_from=former, updated_by=actor, updated_at=time.time())
        for sid in sense_ids:
            self._bump("senses", sid, entry_id=merged_id)
        self.add_relation(actor, merged_id, former, "split_from", f"由 {actor} 拆分")
        self.db.audit(actor, "split", f"{merged_id} <- {former}")

    # ---------- 标音转换 ----------
    def convert_pron(self, pron_id, target_scheme):
        p = self._cur("prons", pron_id)
        if not p:
            raise NotFound("标音不存在")
        row = self.db.one(
            "SELECT * FROM conversions WHERE pron_id=? AND target_scheme=? AND rule_version=?",
            (pron_id, target_scheme, phonology.RULE_VERSION))
        if row is None:
            r = phonology.convert(p["value"], p["scheme"], target_scheme)
            self._insert("conversions", dict(id=new_id("cv"), pron_id=pron_id,
                target_scheme=target_scheme, value=r["value"], status=r["status"],
                detail=r["detail"], rule_version=phonology.RULE_VERSION,
                created_at=time.time()))
            row = self.db.one("SELECT * FROM conversions WHERE pron_id=? AND target_scheme=? "
                              "AND rule_version=?", (pron_id, target_scheme, phonology.RULE_VERSION))
        return {"pron_id": pron_id, "original_scheme": p["scheme"], "original": p["value"],
                "target_scheme": target_scheme, "status": row["status"],
                "value": row["value"], "detail": row["detail"]}

    # ---------- 引用 ----------
    def create_citation(self, actor, article_uri, sense_id):
        s = self._cur("senses", sense_id)
        if not s:
            raise NotFound("义项不存在")
        cid = new_id("c")
        self._insert("citations", dict(id=cid, article_uri=article_uri, sense_id=sense_id,
            sense_version=s["version"], created_at=time.time()))
        return cid

    def resolve_citation(self, citation_id):
        c = self.db.one("SELECT * FROM citations WHERE id=?", (citation_id,))
        if not c:
            raise NotFound("引用不存在")
        archived = self.db.one("SELECT * FROM senses WHERE id=? AND version=?",
                               (c["sense_id"], c["sense_version"]))
        pub = self.db.get_pointer("published")
        clo_v = self.db.one("SELECT entity_version FROM closure WHERE version_id=? AND kind='senses' "
                            "AND entity_id=?", (pub, c["sense_id"]))
        result = {"citation_id": citation_id, "article_uri": c["article_uri"],
                  "cited_version": c["sense_version"],
                  "archived_sense": self._sense_payload(archived) if archived else None}
        if clo_v is None:
            result["status"] = "removed"
            result["note"] = "该义项已在当前发布版本中移除；以下为引用时的存档内容"
        else:
            cur = self.db.one("SELECT * FROM senses WHERE id=? AND version=?",
                              (c["sense_id"], clo_v["entity_version"]))
            if cur["deleted"]:
                result["status"] = "removed"
                result["note"] = "该义项已在当前发布版本中移除；以下为引用时的存档内容"
            elif clo_v["entity_version"] != c["sense_version"]:
                result["status"] = "updated"
                result["note"] = "该义项在现行版本中已修改；同时给出引用时存档与现行内容"
                result["current_sense"] = self._sense_payload(cur)
            else:
                result["status"] = "ok"
                result["current_sense"] = self._sense_payload(cur)
        return result

    def _sense_payload(self, row):
        return {"id": row["id"], "version": row["version"], "entry_id": row["entry_id"],
                "num": row["num"], "pos": row["pos"], "definition": row["definition"],
                "region": row["region"], "deleted": row["deleted"]}

    # ---------- 发布：版本化关系闭包 ----------
    def publish(self, actor, note=""):
        with self.db.tx() as c:
            vid = c.execute("INSERT INTO versions(note,published_by,published_at) VALUES(?,?,?)",
                            (note, actor, time.time())).lastrowid
            for kind in KINDS:
                for r in c.execute(f"SELECT id, MAX(version) v FROM {kind} GROUP BY id"):
                    c.execute("INSERT INTO closure(version_id,kind,entity_id,entity_version) "
                              "VALUES(?,?,?,?)", (vid, kind, r["id"], r["v"]))
            c.execute("INSERT INTO pointers(key,value) VALUES('published',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (vid,))
            # 为每个 active 词条派发异步索引任务（完成顺序不限）
            for r in c.execute("SELECT id FROM entries e WHERE status='active' "
                               "AND version=(SELECT MAX(version) FROM entries WHERE id=e.id)"):
                c.execute("INSERT INTO index_tasks(version_id,entry_id) VALUES(?,?)", (vid, r["id"]))
        self.db.audit(actor, "publish", f"v{vid} {note}")
        return vid

    # ---------- 音频撤权（即时生效，含历史版本视图） ----------
    def revoke_media(self, actor, media_id):
        if self.db.one("SELECT * FROM media WHERE id=?", (media_id,)) is None:
            raise NotFound("媒体不存在")
        self.db.execute("INSERT INTO media_rights(media_id,revoked,updated_at) VALUES(?,1,?) "
                        "ON CONFLICT(media_id) DO UPDATE SET revoked=1, updated_at=excluded.updated_at",
                        (media_id, time.time()))
        with self.cache_lock:
            self.rights_epoch += 1     # 缓存键含 rights_epoch → 旧音频链接立即不可再被缓存提供
        self.db.audit(actor, "revoke_media", media_id)

    # ---------- 公开视图（按发布闭包组装；媒体权利实时判定） ----------
    def closure_map(self, vid):
        m = {}
        for r in self.db.query("SELECT kind,entity_id,entity_version FROM closure WHERE version_id=?", (vid,)):
            m.setdefault(r["kind"], {})[r["entity_id"]] = r["entity_version"]
        return m

    def _row_at(self, table, _id, version):
        return self.db.one(f"SELECT * FROM {table} WHERE id=? AND version=?", (_id, version))

    def entry_view(self, entry_id, version_id=None):
        vid = version_id or self.db.get_pointer("published")
        if not vid:
            raise NotFound("尚无发布版本")
        key = ("entry", entry_id, vid, self.rights_epoch)
        with self.cache_lock:
            if key in self.cache:
                return self.cache[key]
        clo = self.closure_map(vid)
        ev = clo.get("entries", {}).get(entry_id)
        if ev is None:
            raise NotFound("词条不在该发布版本中")
        e = self._row_at("entries", entry_id, ev)
        view = {"id": entry_id, "version_id": vid, "headword": e["headword"],
                "status": e["status"], "context_note": e["context_note"],
                "merged_into": e["merged_into"], "split_from": e["split_from"],
                "forms": [], "prons": [], "senses": [], "relations": {}}
        for fid, fv in clo.get("forms", {}).items():
            f = self._row_at("forms", fid, fv)
            if f["entry_id"] == entry_id and not f["deleted"]:
                view["forms"].append({"id": fid, "text": f["text"], "script": f["script"],
                                      "is_standard": f["is_standard"], "note": f["note"]})
        for pid, pv in clo.get("prons", {}).items():
            p = self._row_at("prons", pid, pv)
            if p["entry_id"] == entry_id and not p["deleted"]:
                convs = [dict(id=c["id"], target_scheme=c["target_scheme"], value=c["value"],
                              status=c["status"], detail=c["detail"])
                         for c in self.db.query("SELECT * FROM conversions WHERE pron_id=?", (pid,))]
                view["prons"].append({"id": pid, "scheme": p["scheme"], "value": p["value"],
                                      "region": p["region"], "source_id": p["source_id"],
                                      "conversions": convs})
        ex_by_sense = {}
        for xid, xv in clo.get("examples", {}).items():
            x = self._row_at("examples", xid, xv)
            if not x["deleted"]:
                ex_by_sense.setdefault(x["sense_id"], []).append((x, xv))
        for sid, sv in sorted(clo.get("senses", {}).items(),
                              key=lambda kv: (self._row_at("senses", kv[0], kv[1])["num"], kv[0])):
            s = self._row_at("senses", sid, sv)
            if s["entry_id"] != entry_id or s["deleted"]:
                continue
            sd = self._sense_payload(s)
            sd["examples"] = []
            for x, xv in ex_by_sense.get(sid, []):
                xd = {"id": x["id"], "text": x["text"], "translation": x["translation"],
                      "region": x["region"], "source_id": x["source_id"]}
                if x["media_id"]:
                    mver = clo.get("media", {}).get(x["media_id"])
                    m = self._row_at("media", x["media_id"], mver) if mver else None
                    rights = self.db.one("SELECT revoked FROM media_rights WHERE media_id=?",
                                         (x["media_id"],))
                    revoked = bool(rights and rights["revoked"])
                    # 内容来自闭包版本；权利实时判定 —— 撤权后任何版本视图都不再给出音频地址
                    xd["media"] = None if revoked or not m else {"id": m["id"], "url": m["path"]}
                    xd["media_status"] = "revoked" if revoked else ("ok" if m else "missing")
                    xd["transcript"] = m["transcript"] if m else ""
                sd["examples"].append(xd)
            view["senses"].append(sd)
        for rid, rv in clo.get("relations", {}).items():
            r = self._row_at("relations", rid, rv)
            if r["deleted"] or (r["src"] != entry_id and r["dst"] != entry_id):
                continue
            other = r["dst"] if r["src"] == entry_id else r["src"]
            ov = clo.get("entries", {}).get(other)
            o = self._row_at("entries", other, ov) if ov else None
            view["relations"].setdefault(r["type"], []).append(
                {"id": other, "headword": o["headword"] if o else "?", "note": r["note"]})
        with self.cache_lock:
            self.cache[key] = view
        return view

    # ---------- 索引文档源 ----------
    def doc_fields(self, entry_id, vid):
        clo = self.closure_map(vid)
        ev = clo.get("entries", {}).get(entry_id)
        if ev is None:
            return []
        e = self._row_at("entries", entry_id, ev)
        if e["status"] != "active":
            return []                      # 已合并词条不建检索文档（页面仍可按 id 访问）
        docs = [("headword", entry_id, e["headword"])]
        for fid, fv in clo.get("forms", {}).items():
            f = self._row_at("forms", fid, fv)
            if f["entry_id"] == entry_id and not f["deleted"]:
                docs.append(("form", fid, f["text"]))
        for pid, pv in clo.get("prons", {}).items():
            p = self._row_at("prons", pid, pv)
            if p["entry_id"] == entry_id and not p["deleted"]:
                docs.append(("pron", pid, p["value"]))
        for sid, sv in clo.get("senses", {}).items():
            s = self._row_at("senses", sid, sv)
            if s["entry_id"] == entry_id and not s["deleted"]:
                docs.append(("sense", sid, s["definition"]))
        for xid, xv in clo.get("examples", {}).items():
            x = self._row_at("examples", xid, xv)
            if not x["deleted"]:
                sv = clo.get("senses", {}).get(x["sense_id"])   # 归属判定必须用闭包版本
                s = self._row_at("senses", x["sense_id"], sv) if sv else None
                if s and s["entry_id"] == entry_id and not s["deleted"]:
                    docs.append(("example", xid, x["text"]))
        return docs
