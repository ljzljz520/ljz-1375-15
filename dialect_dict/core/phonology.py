# -*- coding: utf-8 -*-
"""标音方案注册与无损转换。

原则：
1. 原始记录（prons 表）永远不被修改；切换方案只是选择显示哪条记录/哪条转换结果。
2. 转换必须无损：音节切分失败、韵母/声母/调类在目标方案中无对应、目标方案
   无法表达某对立（如简式罗马字无入声尾）→ 一律返回 status="unconvertible"，
   由前端显示「未转换」，绝不猜读。
3. 转换结果按 (pron_id, target_scheme, rule_version) 缓存于 conversions 表；
   规则升级（RULE_VERSION 递增）后自动重算。
"""
import re

RULE_VERSION = 3

SCHEMES = {
    "yd":   {"name": "本地拼音（六调数字）", "desc": "本词典工作标音"},
    "ipa":  {"name": "国际音标 IPA",        "desc": "严式记音"},
    "plain": {"name": "简式罗马字",          "desc": "无入声尾、上标调号，仅开音节"},
}

# 本地拼音 -> IPA 映射表（显式枚举；不在表内即不可转换）
INITIALS_IPA = {
    "b": "p", "p": "pʰ", "m": "m", "f": "f",
    "d": "t", "t": "tʰ", "n": "n", "l": "l",
    "g": "k", "k": "kʰ", "ng": "ŋ", "h": "h",
    "gw": "kʷ", "kw": "kʷʰ", "w": "w",
    "z": "ts", "c": "tsʰ", "s": "s", "j": "j",
    "": "ʔ",
}
FINALS_IPA = {
    "aa": "aː", "aai": "aːi̯", "aau": "aːu̯", "aam": "aːm", "aan": "aːn",
    "aang": "aːŋ", "aap": "aːp̚", "aat": "aːt̚", "aak": "aːk̚",
    "ai": "ɐi̯", "au": "ɐu̯", "am": "ɐm", "an": "ɐn", "ang": "ɐŋ",
    "ap": "ɐp̚", "at": "ɐt̚", "ak": "ɐk̚",
    "e": "ɛː", "ei": "ei̯", "eng": "ɛːŋ", "ek": "ɛːk̚",
    "i": "iː", "iu": "iːu̯", "im": "iːm", "in": "iːn", "ing": "ɪŋ",
    "ip": "iːp̚", "it": "iːt̚", "ik": "ɪk̚",
    "o": "ɔː", "oi": "ɔːi̯", "ou": "ou̯", "on": "ɔːn", "ong": "ɔːŋ",
    "ot": "ɔːt̚", "ok": "ɔːk̚",
    "u": "uː", "ui": "uːi̯", "un": "uːn", "ung": "ʊŋ", "ut": "uːt̚", "uk": "ʊk̚",
    "eoi": "ɵy̯", "eon": "ɵn", "eot": "ɵt̚",
    "ng": "ŋ̍", "m": "m̩",
}
TONES_IPA = {"1": "˥", "2": "˧˥", "3": "˧", "4": "˨˩", "5": "˩˧", "6": "˨"}
SUP = {"1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶"}
CHECKED_CODAS = ("p", "t", "k")   # 入声尾：简式罗马字无法表达 → 不可无损转换

# 故意不收录的韵母（用于演示「无法转换」）：eo, eu, oeng 等
_INITIALS_SORTED = sorted(INITIALS_IPA, key=len, reverse=True)

def parse_yd(syl: str):
    """本地拼音音节 -> (initial, final, tone)；无法切分返回 None。"""
    m = re.fullmatch(r"([a-z]+?)([1-6])?", syl.strip())
    if not m:
        return None
    body, tone = m.group(1), m.group(2)
    if tone is None:
        return None                      # 缺调号：本方案要求每音节标调
    for ini in _INITIALS_SORTED:
        if ini and body.startswith(ini) and body[len(ini):] in FINALS_IPA:
            return ini, body[len(ini):], tone
    if body in FINALS_IPA:               # 零声母
        return "", body, tone
    return None

# IPA -> 本地拼音：仅承认映射表能精确生成的「规范形」，其余一律不可转换
def _build_reverse():
    rev = {}
    for ini, fi in INITIALS_IPA.items():
        for fin, ff in FINALS_IPA.items():
            for t, tf in TONES_IPA.items():
                rev[fi + ff + tf] = (ini + fin if ini else fin) + t
    return rev
_REV_IPA = _build_reverse()

def convert(value: str, src: str, dst: str):
    """返回 dict(status, value|None, detail)。status: ok | unconvertible。"""
    if src == dst:
        return {"status": "ok", "value": value, "detail": "同一方案，原样显示"}
    if src not in SCHEMES or dst not in SCHEMES:
        return {"status": "unconvertible", "value": None,
                "detail": f"无转换规则：{src}→{dst}"}
    out, problems = [], []
    for syl in value.split():
        if src == "yd" and dst == "ipa":
            p = parse_yd(syl)
            if not p:
                problems.append(f"音节「{syl}」无法切分或韵母无 IPA 对应")
                continue
            ini, fin, tone = p
            out.append(INITIALS_IPA[ini] + FINALS_IPA[fin] + TONES_IPA[tone])
        elif src == "yd" and dst == "plain":
            p = parse_yd(syl)
            if not p:
                problems.append(f"音节「{syl}」无法切分")
                continue
            ini, fin, tone = p
            if fin.endswith(CHECKED_CODAS):
                problems.append(f"音节「{syl}」含入声尾 -{fin[-1]}，简式罗马字无对应，不能无损转换")
                continue
            out.append((ini + fin) + SUP[tone])
        elif src == "ipa" and dst == "yd":
            if syl in _REV_IPA:
                out.append(_REV_IPA[syl])
            else:
                problems.append(f"IPA「{syl}」不在可逆映射表内，拒绝猜读")
        else:
            return {"status": "unconvertible", "value": None,
                    "detail": f"无转换规则：{src}→{dst}"}
    if problems:
        return {"status": "unconvertible", "value": None,
                "detail": "；".join(problems)}
    return {"status": "ok", "value": " ".join(out), "detail": ""}
