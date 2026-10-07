"""版本化关系闭包发布与缓存。

- 发布时把 词条+标音+义项+例句+关系边(一跳) 冻结为不可变闭包，
  按 (entry_id, version) 持久化，旧深链接 ?v=N 永远可取到旧版。
- 缓存键 = (entry_id, version)：不同版本的解释与例句不可能交叉，
  从结构上杜绝"旧解释配新例句"。
- 音频授权属于实时策略而非版本化内容：serve 时在闭包副本上叠加
  当前授权状态，撤权立即生效且不污染缓存原件。
"""
import copy
import json

from .db import NotFound, now

_CACHE = {}

RESPECT_NOTICE = (
    "方言是活态的社区文化遗产。本词典所收词条的语境说明旨在帮助读者"
    "得体使用；请尊重语言社区与使用者，勿将词条用于嘲讽或污名。"
)


def build_closure(conn, entry_id):
    """从当前库内容构建关系闭包（发布瞬间的快照）。"""
    e = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if not e:
        raise NotFound("词条不存在: %s" % entry_id)
    prons = [dict(r) for r in conn.execute(
        "SELECT id, scheme, text, origin, converted_from, status, region_id"
        " FROM pronunciations WHERE entry_id=? ORDER BY created_at", (entry_id,))]
    senses = []
    for s in conn.execute(
            "SELECT * FROM senses WHERE entry_id=? AND status='active' ORDER BY idx",
            (entry_id,)):
        sv = conn.execute(
            "SELECT * FROM sense_versions WHERE sense_id=? AND version=?",
            (s["id"], s["version"])).fetchone()
        examples = []
        for x in conn.execute(
                "SELECT * FROM examples WHERE sense_id=? AND status='active'",
                (s["id"],)):
            xv = conn.execute(
                "SELECT * FROM example_versions WHERE example_id=? AND version=?",
                (x["id"], x["version"])).fetchone()
            au = conn.execute(
                "SELECT id, url, transcript, status FROM audio"
                " WHERE example_id=?", (x["id"],)).fetchone()
            src = None
            if xv["source_id"]:
                src = conn.execute(
                    "SELECT id, title, author, year FROM sources WHERE id=?",
                    (xv["source_id"],)).fetchone()
            examples.append({
                "id": x["id"], "version": x["version"],
                "text": xv["text"], "translation": xv["translation"],
                "source": dict(src) if src else None,
                "audio": dict(au) if au else None,
            })
        region = None
        if sv["region_id"]:
            rg = conn.execute("SELECT name FROM regions WHERE id=?",
                              (sv["region_id"],)).fetchone()
            region = rg["name"] if rg else None
        senses.append({
            "id": s["id"], "version": s["version"], "idx": s["idx"],
            "definition": sv["definition"], "usage_note": sv["usage_note"],
            "region_id": sv["region_id"], "region": region,
            "examples": examples,
        })
    relations = []
    for r in conn.execute(
            "SELECT * FROM relations WHERE from_entry=? OR to_entry=?",
            (entry_id, entry_id)):
        other_id = r["to_entry"] if r["from_entry"] == entry_id else r["from_entry"]
        o = conn.execute(
            "SELECT id, headword, status FROM entries WHERE id=?",
            (other_id,)).fetchone()
        if not o:
            continue
        relations.append({
            "id": r["id"], "type": r["type"],
            "direction": "out" if r["from_entry"] == entry_id else "in",
            "other": dict(o), "note": r["note"],
        })
    return {
        "entry": {"id": e["id"], "headword": e["headword"], "note": e["note"],
                  "version": e["current_version"], "status": e["status"]},
        "pronunciations": prons,
        "senses": senses,
        "relations": relations,
    }


def store_publication(conn, entry_id, version, closure, actor_id):
    conn.execute(
        "INSERT OR REPLACE INTO publications"
        "(entry_id, version, closure_json, published_by, published_at)"
        " VALUES (?,?,?,?,?)",
        (entry_id, version, json.dumps(closure, ensure_ascii=False),
         actor_id, now()))
    _CACHE.pop((entry_id, version), None)


def get_publication(conn, entry_id, version=None):
    """取发布物；version=None 取最新。带 (entry,version) 键缓存。"""
    if version is None:
        row = conn.execute(
            "SELECT * FROM publications WHERE entry_id=?"
            " ORDER BY version DESC LIMIT 1", (entry_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM publications WHERE entry_id=? AND version=?",
            (entry_id, version)).fetchone()
    if not row:
        return None
    key = (entry_id, row["version"])
    if key not in _CACHE:
        _CACHE[key] = {
            "entry_id": entry_id,
            "version": row["version"],
            "closure": json.loads(row["closure_json"]),
            "published_by": row["published_by"],
            "published_at": row["published_at"],
        }
    return _CACHE[key]


def list_published_versions(conn, entry_id):
    return [r["version"] for r in conn.execute(
        "SELECT version FROM publications WHERE entry_id=? ORDER BY version",
        (entry_id,))]


def serve_public(conn, entry_id, version=None):
    """公开页读取：版本化闭包 + 实时音频授权叠加。

    返回 None 表示未发布；词条已合并且未指定版本时返回重定向。
    """
    e = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if not e:
        raise NotFound("词条不存在: %s" % entry_id)
    if e["status"] == "merged" and version is None:
        tgt = conn.execute("SELECT headword FROM entries WHERE id=?",
                           (e["merged_into"],)).fetchone()
        return {"redirect": e["merged_into"],
                "message": "词条已合并至《%s》" % (tgt["headword"] if tgt else "?"),
                "entry_id": entry_id}
    pub = get_publication(conn, entry_id, version)
    if not pub:
        return None
    closure = copy.deepcopy(pub["closure"])  # 不污染缓存原件
    for sense in closure["senses"]:
        for ex in sense["examples"]:
            au = ex.get("audio")
            if not au:
                continue
            st = conn.execute("SELECT status FROM audio WHERE id=?",
                              (au["id"],)).fetchone()
            if not st or st["status"] != "authorized":
                # 撤权：摘掉音频地址，保留文本转写，标注原因
                ex["audio"] = {"id": au["id"], "url": None,
                               "transcript": au.get("transcript", ""),
                               "status": "revoked"}
                ex["audio_revoked"] = True
    return {"entry_id": entry_id, "version": pub["version"],
            "published_at": pub["published_at"],
            "respect_notice": RESPECT_NOTICE, "closure": closure}


def clear_cache():
    _CACHE.clear()
