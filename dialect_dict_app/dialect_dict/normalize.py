"""规范化检索键与可解释高亮的偏移映射。

规范化流水线（逐字符进行，并记录每个输出字符对应的原文区间）：
1. 简繁折叠   TRAD2SIM（演示用内置小表；生产应替换为 OpenCC 全量表）
2. NFKD 分解  预组合字符（如 ā）分解为 基字符 + 组合附加符号
3. 音标组合字符折叠  丢弃 Mn 类（组合变音/声调符号），如 a+U+0304 -> a
4. 标点忽略   丢弃所有 P* 类字符
5. 空白忽略   丢弃 Zs/Cc 等
6. 大小写折叠 casefold

每一步若实际改变了文本，就在 flags 中记录，供检索端生成
"为什么命中" 的可解释说明。offsets[i] = (原文起点, 原文终点)，
把规范化串上的命中区间映射回原文，实现精确高亮。
"""
import unicodedata

# 演示用简繁对照子集（覆盖种子数据与常见字；生产环境替换为 OpenCC）
_TRAD = "龍齊鳴該飯詞語學愛見書東廣話來開門車馬魚鳥貓長櫃個頭對聽說讀寫們這時過還進點發當後會電腦細緊錢飲館樂氣無與國內線場聲員風雲麵飛靚係為問幾機難雙雞鹽買賣讓講親舊歡觀"
_SIMP = "龙齐鸣该饭词语学爱见书东广话来开门车马鱼鸟猫长柜个头对听说读写们这时过还进点发当后会电脑细紧钱饮馆乐气无与国内线场声员风云面飞靓系为问几机难双鸡盐买卖让讲亲旧欢观"
TRAD2SIM = dict(zip(_TRAD, _SIMP))

FOLD_LABELS = {
    "trad": "简繁折叠",
    "dia": "音标/声调组合字符折叠",
    "punct": "标点忽略",
    "ws": "空白忽略",
    "case": "大小写折叠",
}


def normalize_with_map(text, keep_diacritics=False):
    """返回 (规范化串, 偏移映射[(起,止)...], 折叠标记集合)。"""
    out, offsets, flags = [], [], set()
    for i, ch in enumerate(text):
        simp = TRAD2SIM.get(ch)
        if simp is not None and simp != ch:
            flags.add("trad")
            ch = simp
        for d in unicodedata.normalize("NFKD", ch):
            cat = unicodedata.category(d)
            if cat == "Mn" and not keep_diacritics:
                flags.add("dia")          # 音标组合字符（如 ◌̄ ◌̚）被折叠
                continue
            if cat.startswith("P"):
                flags.add("punct")        # 标点被忽略
                continue
            if cat in ("Zs", "Zl", "Zp", "Cc", "Cf"):
                flags.add("ws")           # 空白被忽略
                continue
            folded = d.casefold()
            if folded != d:
                flags.add("case")
            for fc in folded:             # casefold 可能一变多（ß->ss）
                out.append(fc)
                offsets.append((i, i + 1))
    return "".join(out), offsets, flags


def normalize(text):
    return normalize_with_map(text)[0]


def find_spans(haystack, needle):
    """在规范化串中找全部命中区间（左闭右开）。"""
    spans = []
    if not needle:
        return spans
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            break
        spans.append((i, i + len(needle)))
        start = i + 1
    return spans


def span_to_original(offsets, span):
    """规范化区间 -> 原文区间（用于高亮）。"""
    i, j = span
    return (offsets[i][0], offsets[j - 1][1])
