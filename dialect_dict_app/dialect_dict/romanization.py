"""标音方案转换。

原则：只按显式转换表逐音节映射；任一音节无映射即整条标记
unconverted，绝不猜读。原始记录（origin='recorded'）永不被修改，
转换结果另存新行并通过 converted_from 指回原记录。
"""

SCHEMES = ["jyutping", "yale", "ipa", "pinyin"]

SCHEME_LABELS = {
    "jyutping": "粤拼",
    "yale": "耶鲁拼音",
    "ipa": "国际音标",
    "pinyin": "汉语拼音",
}

# 演示用部分转换表（故意不完整，用于验证“未转换”路径）
_JYP_IPA = {
    "keoi5": "kʰɵy˩˧", "sik6": "sɪk̚˨", "faan6": "faːn˨",
    "fong1": "fɔːŋ˥", "m4": "m̩˩", "goi1": "kɔːy˥",
    "lung4": "lʊŋ˩", "zau1": "tsɐu˥", "syut3": "syːt̚˧",
    "jan4": "jɐn˩", "hou2": "hou˧˥", "gam1": "kɐm˥",
    "jat6": "jɐt̚˨", "lai4": "lɐi˩",
}
_JYP_YALE = {   # 注意：故意缺少 gwai6 / faan6 等，制造不可无损转换的情形
    "keoi5": "kéuih", "sik6": "sihk", "fong1": "fōng",
    "m4": "m̀h", "goi1": "gōi", "lung4": "lùhng", "zau1": "jāu",
    "syut3": "syut", "jan4": "yàhn", "hou2": "hóu",
}
_PINYIN_IPA = {"ma1": "ma˥", "ma": "ma", "ta1": "tʰa˥", "ni3": "ni˨˩˦"}

TABLES = {
    ("jyutping", "ipa"): _JYP_IPA,
    ("jyutping", "yale"): _JYP_YALE,
    ("pinyin", "ipa"): _PINYIN_IPA,
}


def convert_text(text, from_scheme, to_scheme):
    """返回 (转换结果或 None, 缺失音节列表)。None 表示无法无损转换。"""
    if from_scheme == to_scheme:
        return text, []
    table = TABLES.get((from_scheme, to_scheme))
    if table is None:
        return None, ["<无转换表:%s→%s>" % (from_scheme, to_scheme)]
    out, missing = [], []
    for tok in text.split():
        if tok in table:
            out.append(table[tok])
        else:
            missing.append(tok)
    if missing:
        return None, missing
    return " ".join(out), []
