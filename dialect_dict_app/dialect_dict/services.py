"""业务逻辑层：词条身份、标音、义项、例句、关系边、合并/拆分、
工作流（编辑/批准分离）、引用文章、音频授权。

词条身份 = 不透明 id（e_xxx），绝不以拼音/词形作为身份；
同音多义、异体字、地区异义通过关系边分别关联。
"""
import json
import sqlite3

from .db import NotFound, Conflict, new_id, now
from . import romanization
from . import publish as publish_mod

RELATION_TYPES = ("homophone", "variant_char", "regional_variant", "see_also")
RELATION_LABELS = {
    "homophone": "同音异义",
    "variant_char": "异体字",
    "regional_variant": "地区变体",
    "see_also": "参见",
}


# ---------------------------------------------------------------- 基础

def audit(conn, actor, action, target="", detail=""):
    conn.execute(
        "INSERT INTO audit_log(actor, action, target, detail, at)"
        " VALUES (?,?,?,?,?)",
        (actor["id"] if actor else None, action, target, detail, now()))


def get_user_by_token(conn, token):
    r = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    return dict(r) if r else None


def require_role(actor, *roles):
    if actor is None:
        raise PermissionError("需要登录")
    if actor["role"] not in roles:
        raise PermissionError("需要角色: %s（当前: %s）"
                              % ("/".join(roles), actor["role"]))


def _get(conn, table, rid, label):
    r = conn.execute("SELECT * FROM %s WHERE id=?" % table, (rid,)).fetchone()
    if not r:
        raise NotFound("%s不存在: %s" % (label, rid))
    return r


def get_entry(conn, entry_id):
    return _get(conn, "entries", entry_id, "词条")


def bump(conn, entry_id, op, actor):
    """词条版本号 +1 并记录版本快照（词条级时钟）。"""
    e = get_entry(conn, entry_id)
    v = e["current_version"] + 1
    conn.execute("UPDATE entries SET current_version=?, updated_at=? WHERE id=?",
                 (v, now(), entry_id))
    conn.execute(
        "INSERT INTO entry_versions(entry_id, version, headword, note, op,"
        " actor, created_at) VALUES (?,?,?,?,?,?,?)",
        (entry_id, v, e["headword"], e["note"], op,
         actor["id"] if actor else None, now()))
    return v


# ---------------------------------------------------------------- 词条

def create_entry(conn, actor, headword, note=""):
    require_role(actor, "editor")
    if not headword or not headword.strip():
        raise ValueError("词形不能为空")
    eid = new_id("e")
    conn.execute(
        "INSERT INTO entries(id, headword, note, status, current_version,"
        " created_by, created_at, updated_at) VALUES (?,?,?,'draft',0,?,?,?)",
        (eid, headword.strip(), note, actor["id"], now(), now()))
    bump(conn, eid, "create", actor)
    audit(conn, actor, "entry.create", eid, headword)
    return eid


def update_entry(conn, actor, entry_id, headword=None, note=None):
    require_role(actor, "editor")
    e = get_entry(conn, entry_id)
    if e["status"] == "merged":
        raise Conflict("词条已合并，请先拆分恢复")
    conn.execute("UPDATE entries SET headword=?, note=? WHERE id=?",
                 (headword if headword is not None else e["headword"],
                  note if note is not None else e["note"], entry_id))
    bump(conn, entry_id, "edit", actor)
    audit(conn, actor, "entry.update", entry_id, "")


def list_entries(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, headword, status, current_version, merged_into, updated_at"
        " FROM entries ORDER BY updated_at DESC")]


def get_entry_detail(conn, entry_id):
    e = get_entry(conn, entry_id)
    prons = [dict(r) for r in conn.execute(
        "SELECT * FROM pronunciations WHERE entry_id=? ORDER BY created_at",
        (entry_id,))]
    senses = []
    for s in conn.execute(
            "SELECT * FROM senses WHERE entry_id=? ORDER BY idx", (entry_id,)):
        sv = conn.execute(
            "SELECT * FROM sense_versions WHERE sense_id=? AND version=?",
            (s["id"], s["version"])).fetchone()
        examples = []
        for x in conn.execute(
                "SELECT * FROM examples WHERE sense_id=?", (s["id"],)):
            xv = conn.execute(
                "SELECT * FROM example_versions WHERE example_id=? AND version=?",
                (x["id"], x["version"])).fetchone()
            au = conn.execute(
                "SELECT * FROM audio WHERE example_id=?", (x["id"],)).fetchone()
            examples.append({"id": x["id"], "version": x["version"],
                             "status": x["status"], "text": xv["text"],
                             "translation": xv["translation"],
                             "source_id": xv["source_id"],
                             "audio": dict(au) if au else None})
        senses.append({"id": s["id"], "idx": s["idx"], "version": s["version"],
                       "status": s["status"], "definition": sv["definition"],
                       "usage_note": sv["usage_note"],
                       "region_id": sv["region_id"], "examples": examples})
    relations = list_relations(conn, entry_id)
    versions = [dict(r) for r in conn.execute(
        "SELECT version, op, actor, created_at FROM entry_versions"
        " WHERE entry_id=? ORDER BY version", (entry_id,))]
    return {"entry": dict(e), "pronunciations": prons, "senses": senses,
            "relations": relations, "versions": versions}


# ---------------------------------------------------------------- 标音

def add_pronunciation(conn, actor, entry_id, scheme, text, region_id=None):
    require_role(actor, "editor")
    get_entry(conn, entry_id)
    if scheme not in romanization.SCHEMES:
        raise ValueError("未知标音方案: %s" % scheme)
    if not text or not text.strip():
        raise ValueError("标音文本不能为空")
    pid = new_id("p")
    conn.execute(
        "INSERT INTO pronunciations(id, entry_id, scheme, text, origin,"
        " status, region_id, created_by, created_at)"
        " VALUES (?,?,?,?,'recorded','ok',?,?,?)",
        (pid, entry_id, scheme, text.strip(), region_id, actor["id"], now()))
    bump(conn, entry_id, "pron_add", actor)
    audit(conn, actor, "pron.add", entry_id, "%s: %s" % (scheme, text))
    return pid


def convert_pronunciations(conn, actor, entry_id, to_scheme):
    """把已记录的标音转换到目标方案。

    原始记录保持不变；无法无损转换的音节 => 存 status='unconverted'
    且 text=NULL 的占位行，展示层回退到原始记录并标注"未转换"。
    """
    require_role(actor, "editor")
    get_entry(conn, entry_id)
    if to_scheme not in romanization.SCHEMES:
        raise ValueError("未知标音方案: %s" % to_scheme)
    made = []
    rows = conn.execute(
        "SELECT * FROM pronunciations WHERE entry_id=? AND origin='recorded'"
        " AND scheme!=?", (entry_id, to_scheme)).fetchall()
    for r in rows:
        dup = conn.execute(
            "SELECT id FROM pronunciations WHERE converted_from=? AND scheme=?",
            (r["id"], to_scheme)).fetchone()
        if dup:
            continue
        text, missing = romanization.convert_text(r["text"], r["scheme"], to_scheme)
        pid = new_id("p")
        if text is None:
            conn.execute(
                "INSERT INTO pronunciations(id, entry_id, scheme, text, origin,"
                " converted_from, status, created_by, created_at)"
                " VALUES (?,?,?,NULL,'converted',?,'unconverted',?,?)",
                (pid, entry_id, to_scheme, r["id"], actor["id"], now()))
            made.append({"id": pid, "status": "unconverted",
                         "missing": missing, "from": r["text"]})
        else:
            conn.execute(
                "INSERT INTO pronunciations(id, entry_id, scheme, text, origin,"
                " converted_from, status, created_by, created_at)"
                " VALUES (?,?,?,?,'converted',?,'ok',?,?)",
                (pid, entry_id, to_scheme, text, r["id"], actor["id"], now()))
            made.append({"id": pid, "status": "ok", "text": text,
                         "from": r["text"]})
    if made:
        bump(conn, entry_id, "pron_convert", actor)
        audit(conn, actor, "pron.convert", entry_id, "-> %s" % to_scheme)
    return made


def pronunciation_display(conn, entry_id, scheme):
    """按方案取展示用标音：有无损转换用转换值，否则回退原始记录并
    标注 unconverted —— 绝不猜读。"""
    get_entry(conn, entry_id)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM pronunciations WHERE entry_id=? ORDER BY created_at",
        (entry_id,))]
    ok = [r for r in rows if r["scheme"] == scheme and r["status"] == "ok"]
    if ok:
        return [{"text": r["text"], "scheme": scheme, "origin": r["origin"],
                 "unconverted": False} for r in ok]
    recorded = [r for r in rows if r["origin"] == "recorded"]
    return [{"text": r["text"], "scheme": r["scheme"],
             "requested_scheme": scheme, "origin": "recorded",
             "unconverted": True,
             "note": "无法无损转换为 %s，显示原始记录" % scheme}
            for r in recorded]


# ---------------------------------------------------------------- 义项

def add_sense(conn, actor, entry_id, definition, usage_note="", region_id=None):
    require_role(actor, "editor")
    get_entry(conn, entry_id)
    if not definition or not definition.strip():
        raise ValueError("义项定义不能为空")
    row = conn.execute(
        "SELECT COALESCE(MAX(idx),0) m FROM senses WHERE entry_id=?",
        (entry_id,)).fetchone()
    sid = new_id("s")
    conn.execute(
        "INSERT INTO senses(id, entry_id, idx, version, status, region_id,"
        " created_by, created_at) VALUES (?,?,?,1,'active',?,?,?)",
        (sid, entry_id, row["m"] + 1, region_id, actor["id"], now()))
    conn.execute(
        "INSERT INTO sense_versions(sense_id, version, definition, usage_note,"
        " region_id, op, actor, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (sid, 1, definition.strip(), usage_note, region_id, "create",
         actor["id"], now()))
    bump(conn, entry_id, "sense_add", actor)
    audit(conn, actor, "sense.add", entry_id, sid)
    return sid


def _latest_sense_version(conn, sid):
    return conn.execute(
        "SELECT * FROM sense_versions WHERE sense_id=?"
        " ORDER BY version DESC LIMIT 1", (sid,)).fetchone()


def update_sense(conn, actor, sense_id, definition=None, usage_note=None,
                 region_id="__keep__"):
    require_role(actor, "editor")
    s = _get(conn, "senses", sense_id, "义项")
    if s["status"] != "active":
        raise Conflict("义项已删除，不能编辑")
    cur = _latest_sense_version(conn, sense_id)
    newv = s["version"] + 1
    d = cur["definition"] if definition is None else definition
    n = cur["usage_note"] if usage_note is None else usage_note
    rg = cur["region_id"] if region_id == "__keep__" else region_id
    conn.execute("UPDATE senses SET version=?, region_id=? WHERE id=?",
                 (newv, rg, sense_id))
    conn.execute(
        "INSERT INTO sense_versions(sense_id, version, definition, usage_note,"
        " region_id, op, actor, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (sense_id, newv, d, n, rg, "edit", actor["id"], now()))
    bump(conn, s["entry_id"], "sense_edit", actor)
    audit(conn, actor, "sense.update", s["entry_id"], sense_id)
    return newv


def delete_sense(conn, actor, sense_id):
    """软删除：被文章引用的义项仍可删除，但历史版本保留，
    引用方按 (sense_id, version) 取到存档内容。"""
    require_role(actor, "editor")
    s = _get(conn, "senses", sense_id, "义项")
    if s["status"] != "active":
        raise Conflict("义项已删除")
    cited = conn.execute(
        "SELECT COUNT(*) c FROM article_citations WHERE sense_id=?",
        (sense_id,)).fetchone()["c"]
    conn.execute(
        "UPDATE senses SET status='deleted', deleted_by=?, deleted_at=?"
        " WHERE id=?", (actor["id"], now(), sense_id))
    cur = _latest_sense_version(conn, sense_id)
    conn.execute(
        "INSERT INTO sense_versions(sense_id, version, definition, usage_note,"
        " region_id, op, actor, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (sense_id, s["version"] + 1, cur["definition"], cur["usage_note"],
         cur["region_id"], "delete", actor["id"], now()))
    conn.execute("UPDATE senses SET version=? WHERE id=?",
                 (s["version"] + 1, sense_id))
    bump(conn, s["entry_id"], "sense_delete", actor)
    audit(conn, actor, "sense.delete", s["entry_id"],
          "%s（被 %d 篇文章引用）" % (sense_id, cited))
    return {"sense_id": sense_id, "cited_by": cited}


def get_sense_at(conn, sense_id, version=None):
    s = _get(conn, "senses", sense_id, "义项")
    if version is None:
        sv = _latest_sense_version(conn, sense_id)
    else:
        sv = conn.execute(
            "SELECT * FROM sense_versions WHERE sense_id=? AND version=?",
            (sense_id, version)).fetchone()
        if not sv:
            raise NotFound("义项版本不存在: %s v%s" % (sense_id, version))
    return {"sense": dict(s), "content": dict(sv)}


def public_sense_view(conn, sense_id):
    """旧深链接视图：已删除义项返回存档内容 + 删除说明。"""
    s = _get(conn, "senses", sense_id, "义项")
    sv = _latest_sense_version(conn, sense_id)
    history = [dict(r) for r in conn.execute(
        "SELECT version, op, actor, created_at FROM sense_versions"
        " WHERE sense_id=? ORDER BY version", (sense_id,))]
    return {
        "sense_id": sense_id, "entry_id": s["entry_id"],
        "status": s["status"], "version": sv["version"],
        "definition": sv["definition"], "usage_note": sv["usage_note"],
        "deleted_at": s["deleted_at"],
        "message": ("该义项已删除（删除时间 %s）；以下为存档版本，引用文章不受影响。"
                    % s["deleted_at"]) if s["status"] == "deleted" else None,
        "history": history,
    }


# ---------------------------------------------------------------- 例句

def add_example(conn, actor, sense_id, text, translation="", source_id=None,
                region_id=None):
    require_role(actor, "editor")
    s = _get(conn, "senses", sense_id, "义项")
    if s["status"] != "active":
        raise Conflict("义项已删除，不能添加例句")
    if not text or not text.strip():
        raise ValueError("例句不能为空")
    xid = new_id("x")
    conn.execute(
        "INSERT INTO examples(id, sense_id, version, status)"
        " VALUES (?,?,1,'active')", (xid, sense_id))
    conn.execute(
        "INSERT INTO example_versions(example_id, version, text, translation,"
        " source_id, region_id, op, actor, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (xid, 1, text.strip(), translation, source_id, region_id, "create",
         actor["id"], now()))
    bump(conn, s["entry_id"], "example_add", actor)
    audit(conn, actor, "example.add", s["entry_id"], xid)
    return xid


def update_example(conn, actor, example_id, text=None, translation=None,
                   source_id="__keep__", region_id="__keep__"):
    require_role(actor, "editor")
    x = _get(conn, "examples", example_id, "例句")
    cur = conn.execute(
        "SELECT * FROM example_versions WHERE example_id=?"
        " ORDER BY version DESC LIMIT 1", (example_id,)).fetchone()
    newv = x["version"] + 1
    conn.execute("UPDATE examples SET version=? WHERE id=?", (newv, example_id))
    conn.execute(
        "INSERT INTO example_versions(example_id, version, text, translation,"
        " source_id, region_id, op, actor, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (example_id, newv,
         cur["text"] if text is None else text,
         cur["translation"] if translation is None else translation,
         cur["source_id"] if source_id == "__keep__" else source_id,
         cur["region_id"] if region_id == "__keep__" else region_id,
         "edit", actor["id"], now()))
    s = _get(conn, "senses", x["sense_id"], "义项")
    bump(conn, s["entry_id"], "example_edit", actor)
    audit(conn, actor, "example.update", s["entry_id"], example_id)
    return newv


# ---------------------------------------------------------------- 音频

def attach_audio(conn, actor, example_id, url, transcript=""):
    require_role(actor, "editor")
    x = _get(conn, "examples", example_id, "例句")
    conn.execute("DELETE FROM audio WHERE example_id=?", (example_id,))
    auid = new_id("au")
    conn.execute(
        "INSERT INTO audio(id, example_id, url, transcript, status)"
        " VALUES (?,?,?,?,'authorized')", (auid, example_id, url, transcript))
    s = _get(conn, "senses", x["sense_id"], "义项")
    bump(conn, s["entry_id"], "audio_attach", actor)
    audit(conn, actor, "audio.attach", example_id, auid)
    return auid


def revoke_audio(conn, actor, audio_id, reason=""):
    """音频撤权：实时策略，公开页立即隐藏音频地址、保留文本。"""
    require_role(actor, "approver")
    _get(conn, "audio", audio_id, "音频")
    conn.execute(
        "UPDATE audio SET status='revoked', revoked_by=?, revoked_at=?,"
        " revoked_reason=? WHERE id=?",
        (actor["id"], now(), reason, audio_id))
    audit(conn, actor, "audio.revoke", audio_id, reason)


# ---------------------------------------------------------------- 关系边

def add_relation(conn, actor, from_entry, to_entry, rtype, note=""):
    require_role(actor, "editor")
    if rtype not in RELATION_TYPES:
        raise ValueError("未知关系类型: %s" % rtype)
    if from_entry == to_entry:
        raise Conflict("不能建立自指关系")
    get_entry(conn, from_entry)
    get_entry(conn, to_entry)
    rid = new_id("r")
    try:
        conn.execute(
            "INSERT INTO relations(id, type, from_entry, to_entry, note,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (rid, rtype, from_entry, to_entry, note, actor["id"], now()))
    except sqlite3.IntegrityError:
        raise Conflict("关系已存在")
    bump(conn, from_entry, "relation_add", actor)
    audit(conn, actor, "relation.add", from_entry,
          "%s -> %s (%s)" % (from_entry, to_entry, rtype))
    return rid


def remove_relation(conn, actor, relation_id):
    require_role(actor, "editor")
    r = _get(conn, "relations", relation_id, "关系")
    conn.execute("DELETE FROM relations WHERE id=?", (relation_id,))
    bump(conn, r["from_entry"], "relation_remove", actor)
    audit(conn, actor, "relation.remove", r["from_entry"], relation_id)


def list_relations(conn, entry_id):
    out = []
    for r in conn.execute(
            "SELECT * FROM relations WHERE from_entry=? OR to_entry=?",
            (entry_id, entry_id)):
        other_id = r["to_entry"] if r["from_entry"] == entry_id else r["from_entry"]
        o = conn.execute("SELECT id, headword, status FROM entries WHERE id=?",
                         (other_id,)).fetchone()
        out.append({"id": r["id"], "type": r["type"],
                    "type_label": RELATION_LABELS[r["type"]],
                    "direction": "out" if r["from_entry"] == entry_id else "in",
                    "other": dict(o) if o else None, "note": r["note"]})
    return out


def homophone_candidates(conn, entry_id):
    """同音候选：共享相同标音文本的其他词条。

    拼音只用于"提示候选"，词条身份与关联均由人工确认的关系边决定 —
    同音多义各自成条，绝不自动合并。
    """
    get_entry(conn, entry_id)
    mine = {(r["scheme"], r["text"]) for r in conn.execute(
        "SELECT scheme, text FROM pronunciations WHERE entry_id=?"
        " AND text IS NOT NULL", (entry_id,))}
    if not mine:
        return []
    linked = {r["to_entry"] for r in conn.execute(
        "SELECT to_entry FROM relations WHERE from_entry=? AND type='homophone'",
        (entry_id,))} | {r["from_entry"] for r in conn.execute(
        "SELECT from_entry FROM relations WHERE to_entry=? AND type='homophone'",
        (entry_id,))}
    found = {}
    for r in conn.execute(
            "SELECT p.entry_id, e.headword, p.scheme, p.text"
            " FROM pronunciations p JOIN entries e ON e.id=p.entry_id"
            " WHERE p.text IS NOT NULL AND e.status!='merged'"
            " AND p.entry_id!=?", (entry_id,)):
        if (r["scheme"], r["text"]) in mine and r["entry_id"] not in linked:
            found[r["entry_id"]] = {"entry_id": r["entry_id"],
                                    "headword": r["headword"],
                                    "shared": "%s:%s" % (r["scheme"], r["text"])}
    return list(found.values())


# ---------------------------------------------------------------- 合并 / 拆分

def _move(conn, op_id, kind, obj_id, frm, to):
    conn.execute(
        "INSERT INTO move_events(op_id, kind, obj_id, from_entry, to_entry, at)"
        " VALUES (?,?,?,?,?,?)", (op_id, kind, obj_id, frm, to, now()))


def merge_entries(conn, actor, source_id, target_id):
    """source 并入 target：义项/标音/关系边整体迁移，source 留墓碑，
    旧深链接重定向到 target。全部移动记入 move_events 供拆分还原。"""
    require_role(actor, "editor")
    if source_id == target_id:
        raise Conflict("不能合并到自身")
    s = get_entry(conn, source_id)
    get_entry(conn, target_id)
    if s["status"] == "merged":
        raise Conflict("源词条已是合并状态")
    op_id = new_id("op")
    for r in conn.execute("SELECT id FROM senses WHERE entry_id=?",
                          (source_id,)):
        conn.execute("UPDATE senses SET entry_id=? WHERE id=?",
                     (target_id, r["id"]))
        _move(conn, op_id, "sense", r["id"], source_id, target_id)
    for r in conn.execute("SELECT id FROM pronunciations WHERE entry_id=?",
                          (source_id,)):
        conn.execute("UPDATE pronunciations SET entry_id=? WHERE id=?",
                     (target_id, r["id"]))
        _move(conn, op_id, "pron", r["id"], source_id, target_id)
    for r in conn.execute("SELECT * FROM relations WHERE from_entry=?",
                          (source_id,)).fetchall():
        try:
            conn.execute("UPDATE relations SET from_entry=? WHERE id=?",
                         (target_id, r["id"]))
            _move(conn, op_id, "relation_from", r["id"], source_id, target_id)
        except sqlite3.IntegrityError:
            conn.execute("DELETE FROM relations WHERE id=?", (r["id"],))
    for r in conn.execute("SELECT * FROM relations WHERE to_entry=?",
                          (source_id,)).fetchall():
        try:
            conn.execute("UPDATE relations SET to_entry=? WHERE id=?",
                         (target_id, r["id"]))
            _move(conn, op_id, "relation_to", r["id"], source_id, target_id)
        except sqlite3.IntegrityError:
            conn.execute("DELETE FROM relations WHERE id=?", (r["id"],))
    conn.execute("UPDATE entries SET status='merged', merged_into=? WHERE id=?",
                 (target_id, source_id))
    bump(conn, source_id, "merge_into:%s" % target_id, actor)
    bump(conn, target_id, "merge_absorb:%s" % source_id, actor)
    audit(conn, actor, "entry.merge", source_id, "-> %s" % target_id)
    return {"merged": source_id, "into": target_id, "op_id": op_id}


def split_entry(conn, actor, entry_id, sense_ids, restore_entry_id=None,
                new_headword=None):
    """从 entry_id 拆出指定义项。

    restore_entry_id 指向曾并入本词条的墓碑时：复活该词条并按
    move_events 精确还原其标音与关系边；否则新建词条。
    """
    require_role(actor, "editor")
    src = get_entry(conn, entry_id)
    if src["status"] == "merged":
        raise Conflict("已合并词条不能再拆分")
    op_id = new_id("op")
    if restore_entry_id:
        r = get_entry(conn, restore_entry_id)
        if not (r["status"] == "merged" and r["merged_into"] == entry_id):
            raise Conflict("restore 目标不是并入本词条的墓碑")
        nid = restore_entry_id
        conn.execute(
            "UPDATE entries SET status='draft', merged_into=NULL WHERE id=?",
            (nid,))
        bump(conn, nid, "split_restore", actor)
    else:
        nid = new_id("e")
        conn.execute(
            "INSERT INTO entries(id, headword, note, status, current_version,"
            " created_by, created_at, updated_at) VALUES (?,?,?,'draft',0,?,?,?)",
            (nid, new_headword or (src["headword"] + "（拆分）"),
             src["note"], actor["id"], now(), now()))
        bump(conn, nid, "split_create", actor)
    for sid in sense_ids:
        s = _get(conn, "senses", sid, "义项")
        if s["entry_id"] != entry_id:
            raise Conflict("义项 %s 不属于词条 %s" % (sid, entry_id))
        conn.execute("UPDATE senses SET entry_id=? WHERE id=?", (nid, sid))
        _move(conn, op_id, "sense", sid, entry_id, nid)
    if restore_entry_id:
        # 还原合并时从 restore 词条迁出的标音与关系边（仍落在 entry_id 上的）
        moved = conn.execute(
            "SELECT kind, obj_id FROM move_events WHERE from_entry=?"
            " AND to_entry=? AND kind IN ('pron','relation_from','relation_to')",
            (restore_entry_id, entry_id)).fetchall()
        for m in moved:
            if m["kind"] == "pron":
                cur = conn.execute(
                    "SELECT entry_id FROM pronunciations WHERE id=?",
                    (m["obj_id"],)).fetchone()
                if cur and cur["entry_id"] == entry_id:
                    conn.execute(
                        "UPDATE pronunciations SET entry_id=? WHERE id=?",
                        (nid, m["obj_id"]))
                    _move(conn, op_id, "pron", m["obj_id"], entry_id, nid)
            elif m["kind"] == "relation_from":
                cur = conn.execute(
                    "SELECT from_entry FROM relations WHERE id=?",
                    (m["obj_id"],)).fetchone()
                if cur and cur["from_entry"] == entry_id:
                    conn.execute(
                        "UPDATE relations SET from_entry=? WHERE id=?",
                        (nid, m["obj_id"]))
                    _move(conn, op_id, "relation_from", m["obj_id"], entry_id, nid)
            else:
                cur = conn.execute(
                    "SELECT to_entry FROM relations WHERE id=?",
                    (m["obj_id"],)).fetchone()
                if cur and cur["to_entry"] == entry_id:
                    conn.execute(
                        "UPDATE relations SET to_entry=? WHERE id=?",
                        (nid, m["obj_id"]))
                    _move(conn, op_id, "relation_to", m["obj_id"], entry_id, nid)
    bump(conn, entry_id, "split_out", actor)
    audit(conn, actor, "entry.split", entry_id,
          "-> %s senses=%s" % (nid, ",".join(sense_ids)))
    return {"entry_id": nid, "restored": bool(restore_entry_id)}


# ---------------------------------------------------------------- 引用文章

def create_article(conn, actor, title, body, citations):
    """citations: [{"sense_id":..,"sense_version":..}]，按版本固化引用。"""
    require_role(actor, "editor")
    aid = new_id("art")
    conn.execute(
        "INSERT INTO articles(id, title, body, created_by, created_at)"
        " VALUES (?,?,?,?,?)", (aid, title, body, actor["id"], now()))
    for c in citations:
        sv = conn.execute(
            "SELECT 1 x FROM sense_versions WHERE sense_id=? AND version=?",
            (c["sense_id"], c["sense_version"])).fetchone()
        if not sv:
            raise NotFound("引用版本不存在: %s v%s"
                           % (c["sense_id"], c["sense_version"]))
        conn.execute(
            "INSERT INTO article_citations(article_id, sense_id, sense_version)"
            " VALUES (?,?,?)", (aid, c["sense_id"], c["sense_version"]))
    audit(conn, actor, "article.create", aid, title)
    return aid


def get_article(conn, article_id):
    a = _get(conn, "articles", article_id, "文章")
    cits = []
    for c in conn.execute(
            "SELECT * FROM article_citations WHERE article_id=?", (article_id,)):
        sv = conn.execute(
            "SELECT * FROM sense_versions WHERE sense_id=? AND version=?",
            (c["sense_id"], c["sense_version"])).fetchone()
        s = conn.execute("SELECT * FROM senses WHERE id=?",
                         (c["sense_id"],)).fetchone()
        e = conn.execute("SELECT id, headword FROM entries WHERE id=?",
                         (s["entry_id"],)).fetchone()
        cits.append({
            "sense_id": c["sense_id"], "sense_version": c["sense_version"],
            "entry_id": e["id"], "headword": e["headword"],
            "definition": sv["definition"], "usage_note": sv["usage_note"],
            "sense_status": s["status"],
            "note": ("该义项后续已被删除，此处为引用时存档版本"
                     if s["status"] == "deleted"
                     else "引用固定于 v%d" % c["sense_version"]),
        })
    return {"id": a["id"], "title": a["title"], "body": a["body"],
            "citations": cits}


# ---------------------------------------------------------------- 工作流

def submit_entry(conn, actor, entry_id):
    require_role(actor, "editor")
    e = get_entry(conn, entry_id)
    if e["status"] != "draft":
        raise Conflict("仅草稿可提交审核（当前: %s）" % e["status"])
    conn.execute("UPDATE entries SET status='review' WHERE id=?", (entry_id,))
    bump(conn, entry_id, "submit", actor)
    audit(conn, actor, "entry.submit", entry_id, "")


def approve_entry(conn, actor, entry_id):
    require_role(actor, "approver")
    e = get_entry(conn, entry_id)
    if e["status"] != "review":
        raise Conflict("仅待审词条可批准（当前: %s）" % e["status"])
    conn.execute("UPDATE entries SET status='approved' WHERE id=?", (entry_id,))
    bump(conn, entry_id, "approve", actor)
    audit(conn, actor, "entry.approve", entry_id, "")


def publish_entry(conn, actor, entry_id):
    """批准人发布：冻结版本化关系闭包，并投递异步索引任务。"""
    require_role(actor, "approver")
    e = get_entry(conn, entry_id)
    if e["status"] not in ("approved", "published"):
        raise Conflict("仅已批准词条可发布（当前: %s）" % e["status"])
    version = e["current_version"]
    closure = publish_mod.build_closure(conn, entry_id)
    publish_mod.store_publication(conn, entry_id, version, closure, actor["id"])
    conn.execute("UPDATE entries SET status='published' WHERE id=?", (entry_id,))
    conn.execute(
        "INSERT INTO index_tasks(entry_id, version, status, created_at)"
        " VALUES (?,?, 'pending', ?)", (entry_id, version, now()))
    audit(conn, actor, "entry.publish", entry_id, "v%d" % version)
    return version


# ---------------------------------------------------------------- 元数据

def add_region(conn, actor, name, note=""):
    require_role(actor, "editor")
    rid = new_id("rg")
    conn.execute("INSERT INTO regions(id, name, note) VALUES (?,?,?)",
                 (rid, name, note))
    return rid


def add_source(conn, actor, title, author="", year=None, note=""):
    require_role(actor, "editor")
    sid = new_id("src")
    conn.execute(
        "INSERT INTO sources(id, title, author, year, note) VALUES (?,?,?,?,?)",
        (sid, title, author, year, note))
    return sid


def meta(conn):
    return {
        "schemes": romanization.SCHEMES,
        "scheme_labels": romanization.SCHEME_LABELS,
        "relation_types": RELATION_TYPES,
        "relation_labels": RELATION_LABELS,
        "regions": [dict(r) for r in conn.execute("SELECT * FROM regions")],
        "sources": [dict(r) for r in conn.execute("SELECT * FROM sources")],
        "respect_notice": publish_mod.RESPECT_NOTICE,
    }
