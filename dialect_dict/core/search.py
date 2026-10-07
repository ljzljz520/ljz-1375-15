# -*- coding: utf-8 -*-
"""检索：规范化检索键 + 原文分字段索引 双路召回，可解释高亮。

- 规范键索引 idx_norm：折叠简繁/标点/大小写/音标组合字符后做子串匹配，
  命中后通过偏移映射还原原文区间 → 高亮。explain 列出实际触发的折叠规则。
- 原文索引 idx_tokens：不折叠，查询与文档 token 精确相等才命中（AND 语义）。
  优点：零误伤、偏移天然精确；缺点：繁简、标点、组合符差异都会漏召回。
- compare() 并排给出两路结果差集，用于评估与演示。
"""
import json
from .normalize import normalize, tokenize, spans_to_original

FIELD_LABEL = {"headword": "词目", "form": "词形", "pron": "标音",
               "sense": "义项", "example": "例句"}

def _norm_hits(db, vid, qnorm, qrules):
    if not qnorm:
        return {}
    hits = {}
    for row in db.query("SELECT * FROM idx_norm WHERE version_id=? AND instr(norm_text, ?) > 0",
                        (vid, qnorm)):
        key = (row["entry_id"], row["field"], row["sub_id"])
        offs = json.loads(row["offsets"]); rules = json.loads(row["rules"])
        spans, start = [], 0
        while True:
            i = row["norm_text"].find(qnorm, start)
            if i < 0:
                break
            spans.append(spans_to_original(i, i + len(qnorm), offs, row["text"]))
            start = i + 1
        applied = sorted(set(qrules) | set(rules))
        hits[key] = {"entry_id": row["entry_id"], "field": row["field"],
                     "sub_id": row["sub_id"], "text": row["text"], "spans": spans,
                     "matched_via": "norm",
                     "explain": f"规范化键匹配：{FIELD_LABEL[row['field']]}字段原文经折叠后命中；"
                                f"触发规则：{'; '.join(applied) if applied else '无需折叠'}"}
    return hits

def _token_hits(db, vid, qtokens):
    if not qtokens:
        return {}
    need = {t for t, _, _ in qtokens}
    cand = {}
    for tok in need:
        for row in db.query("SELECT * FROM idx_tokens WHERE version_id=? AND token=?",
                            (vid, tok)):
            key = (row["entry_id"], row["field"], row["sub_id"])
            cand.setdefault(key, {}).setdefault(tok, []).append([row["start"], row["end"]])
    hits = {}
    for key, got in cand.items():
        if set(got) != need:
            continue                                   # AND：查询 token 必须全部精确出现
        spans = [sp for tok in need for sp in got[tok]]
        eid, field, sub_id = key
        row = db.one("SELECT text FROM idx_norm WHERE entry_id=? AND version_id=? AND field=? "
                     "AND sub_id=?", (eid, vid, field, sub_id))
        hits[key] = {"entry_id": eid, "field": field, "sub_id": sub_id,
                     "text": row["text"] if row else "", "spans": sorted(spans),
                     "matched_via": "field",
                     "explain": f"原文分字段精确匹配：{FIELD_LABEL[field]}字段 token "
                                f"{'、'.join(sorted(need))} 逐字命中（未做任何折叠）"}
    return hits

def search(db, service, q, version_id=None):
    vid = version_id or db.get_pointer("indexed")
    if not vid:
        return {"version_id": 0, "hits": [], "note": "索引尚未就绪"}
    qnorm, _, qrules = normalize(q)
    norm_hits = _norm_hits(db, vid, qnorm, qrules)
    tok_hits = _token_hits(db, vid, tokenize(q))
    merged = {}
    for src in (norm_hits, tok_hits):
        for key, h in src.items():
            if key in merged:          # 双路同时命中：保留双解释，标记 both
                m = merged[key]
                m["matched_via"] = "both"
                m["explain"] += "｜" + h["explain"]
                m["spans"] = sorted(m["spans"] + [s for s in h["spans"] if s not in m["spans"]])
            else:
                merged[key] = dict(h)
    heads = {}
    clo = service.closure_map(vid)
    for eid in {k[0] for k in merged}:
        ev = clo.get("entries", {}).get(eid)
        row = service._row_at("entries", eid, ev) if ev else None
        heads[eid] = row["headword"] if row else "?"
    rank = {"both": 0, "field": 1, "norm": 2}
    hits = sorted(merged.values(), key=lambda h: (rank[h["matched_via"]], h["entry_id"]))
    for h in hits:
        h["headword"] = heads[h["entry_id"]]
        h["field_label"] = FIELD_LABEL[h["field"]]
    return {"version_id": vid, "query_norm": qnorm, "query_rules": qrules, "hits": hits}

def compare(db, service, q):
    """并排比较两种索引：返回各自命中与差集，用于「规范化键 vs 原文索引」评估。"""
    vid = db.get_pointer("indexed")
    if not vid:
        return {"version_id": 0}
    qnorm, _, qrules = normalize(q)
    n = _norm_hits(db, vid, qnorm, qrules)
    t = _token_hits(db, vid, tokenize(q))
    nk, tk = set(n), set(t)
    def brief(keys, src):
        return [{"entry_id": k[0], "field": k[1], "text": src[k]["text"],
                 "explain": src[k]["explain"]} for k in sorted(keys)]
    return {"version_id": vid, "query": q, "query_norm": qnorm,
            "both": brief(nk & tk, n), "only_norm": brief(nk - tk, n),
            "only_field": brief(tk - nk, t),
            "note": "only_norm = 依赖折叠才能命中（繁简/标点/组合符差异）；"
                    "only_field = 精确命中但被规范键漏掉（罕见，如查询含被剥离字符）"}
