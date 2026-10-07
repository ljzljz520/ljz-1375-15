"""检索：规范化检索键 + 原文分字段双索引，可解释高亮。

两种索引键的对比：
- 原文分字段索引 (mode=raw)：对 raw 文本做精确子串匹配。
  召回低但零歧义，适合引文核对。
- 规范化检索键 (mode=norm)：查询与文档都经 normalize 折叠
  （简繁/标点/音标组合字符/大小写/空白），召回高；
  每个命中携带 folds 说明"为什么命中"，offsets 把规范化串上的
  命中区间映射回原文坐标，实现可解释高亮。
- mode=auto：先原文后规范化，并在结果中标注实际命中途径。
"""
import json

from .db import d
from .normalize import (FOLD_LABELS, find_spans, normalize_with_map,
                        span_to_original)

KIND_LABELS = {
    "headword": "词形", "note": "备注", "pron": "标音",
    "sense_def": "义项", "sense_note": "语境说明",
    "example": "例句", "translation": "例句译文",
}


def _explain(via, folds):
    if via == "exact":
        return "原文精确匹配"
    if not folds:
        return "规范化匹配"
    return "规范化匹配（%s）" % "、".join(
        FOLD_LABELS.get(f, f) for f in folds)


def search(conn, query, mode="auto", limit=50):
    query = (query or "").strip()
    if not query:
        return []
    q_norm, _, q_flags = normalize_with_map(query)
    hits = {}
    for row in conn.execute("SELECT * FROM index_docs"):
        raw = row["raw"]
        match = None
        if mode in ("raw", "auto"):
            spans = find_spans(raw, query)
            if spans:
                match = {"via": "exact", "spans": [list(s) for s in spans],
                         "folds": []}
        if match is None and mode in ("norm", "auto") and q_norm:
            nspans = find_spans(row["norm"], q_norm)
            if nspans:
                offsets = json.loads(row["offsets"])
                spans = [list(span_to_original(offsets, s)) for s in nspans]
                folds = sorted(set(json.loads(row["flags"])) | q_flags)
                match = {"via": "normalized", "spans": spans, "folds": folds}
        if match:
            hits.setdefault(row["entry_id"], []).append({
                "kind": row["kind"],
                "kind_label": KIND_LABELS.get(row["kind"], row["kind"]),
                "oid": row["oid"], "sub": row["sub"], "raw": raw,
                "spans": match["spans"], "via": match["via"],
                "folds": match["folds"],
                "explanation": _explain(match["via"], match["folds"]),
            })
    results = []
    for eid, matches in hits.items():
        e = conn.execute(
            "SELECT id, headword, status FROM entries WHERE id=?",
            (eid,)).fetchone()
        if not e:
            continue
        results.append({"entry_id": eid, "headword": e["headword"],
                        "matches": matches})
    results.sort(key=lambda r: (
        0 if any(m["via"] == "exact" for m in r["matches"]) else 1,
        r["headword"]))
    return results[:limit]


def compare(conn, query):
    """对比两种索引键：各自命中数与各自独有的词条。"""
    raw_res = search(conn, query, mode="raw")
    norm_res = search(conn, query, mode="norm")
    raw_ids = {r["entry_id"] for r in raw_res}
    norm_ids = {r["entry_id"] for r in norm_res}
    return {
        "query": query,
        "raw": {"count": len(raw_res),
                "entries": [r["headword"] for r in raw_res]},
        "norm": {"count": len(norm_res),
                 "entries": [r["headword"] for r in norm_res]},
        "only_in_norm": [r["headword"] for r in norm_res
                         if r["entry_id"] not in raw_ids],
        "only_in_raw": [r["headword"] for r in raw_res
                        if r["entry_id"] not in norm_ids],
        "note": ("规范化检索键通过简繁折叠、标点忽略、音标组合字符折叠"
                 "扩大召回；原文分字段索引保持逐字精确匹配，两者互补。"),
    }
