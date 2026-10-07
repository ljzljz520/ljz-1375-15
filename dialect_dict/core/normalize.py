# -*- coding: utf-8 -*-
"""规范化检索键 vs 原文分字段索引 的基础组件。

normalize():  原文 -> (规范化键, 偏移映射, 命中规则)。偏移映射使规范键上的命中
              可以映射回原文坐标，实现「可解释高亮」。
tokenize():   原文分词（CJK 单字+二元、拉丁/数字整词），保留原文偏移，
              不做任何折叠 —— 这是「原文分字段索引」的精确但脆弱的一面。

折叠规则（每条都会记录进 rules，用于向用户解释为何命中）：
  nfkc            兼容字符规范化（全角→半角、ﬀ→ff、修饰字母ʰ→h）
  casefold        大小写折叠
  trad2simp       繁体→简体（演示用内置小表；生产可替换 OpenCC，接口不变）
  strip_combining 音标组合附加符号（U+0300–036F 等 Mn/Me）剥离：é 与 e+́ 归一
  ipa_fold        常见 IPA 字母折叠到近位 ASCII（ɛ→e, ɔ→o, ŋ→ng, ʔ→'）
  strip_tone      IPA 调值字母(˥˦˧˨˩)与上标调号(¹²³⁴⁵)剥离；调类数字(1-6)保留
  strip_punct     标点/空白剥离
"""
import unicodedata

# 演示用繁→简小表（覆盖种子数据与常见字；生产环境替换为完整 OpenCC 表即可）
TRAD2SIMP = dict(zip(
    "臺灣頭髮雞鴨學校廣東話語時間開關買賣長場車門問聞見觀點讀寫聽說紅綠藍黃白黑貓狗魚鳥馬牛羊豬龍鳳龜書畫樂醫藥飯飲餸錢銀鐵電腦機會義議國圖園圓燈火煙氣風雲雨雪靈麗聲音樂愛親新舊兒孫後來東西兩個們這裡那麼為甚麼現發達過還進遠近邊鄉郵醫鹽麵包飽養驚體臉腳肩背齒舌頭條張隻對雙號種類樣歲數無沒與於為從將被讓給",
    "台湾头发鸡鸭学校广东话语时间开关买卖长场车门闻闻见观点读写听说红绿蓝黄白黑猫狗鱼鸟马牛羊猪龙凤龟书画乐医药饭饮餸钱银铁电脑机会义议国图园圆灯火烟氣风云雨雪灵丽声音乐爱亲新旧儿孙后来东西两个们这里那么为什么现发达过还进远近边乡邮医盐面包饱养惊体脸脚肩背齿舌头条张只对双号种类样岁数无没与于为从将被让给"
))
# 修正逐字映射中长度不一致的风险：显式校验
assert all(len(k) == 1 and len(v) == 1 for k, v in TRAD2SIMP.items())

TRAD2SIMP["氣"] = "气"  # 修正 zip 对齐

# 补充常用字（显式对照）
TRAD2SIMP.update({
    "箏":"筝","鷂":"鹞","紙":"纸","線":"线","繩":"绳","飛":"飞","鳴":"鸣","響":"响",
    "聲":"声","調":"调","詞":"词","典":"典","記":"记","錄":"录","載":"载","傳":"传",
    "統":"统","計":"计","較":"较","準":"准","確":"确","認":"认","識":"识","變":"变",
    "換":"换","標":"标","註":"注","釋":"释","義":"义","項":"项","條":"条","目":"目",
    "錯":"错","誤":"误","對":"对","齊":"齐","備":"备","註":"注","冊":"册","頁":"页",
    "內":"内","容":"容","資":"资","料":"料","庫":"库","檔":"档","案":"案","捨":"舍",
    "棄":"弃","權":"权","限":"限","授":"授","許":"许","證":"证","書":"书","簽":"签",
    "發":"发","佈":"布","審":"审","校":"校","批":"批","編":"编","輯":"辑","員":"员",
    "戲":"戏","劇":"剧","臺":"台","凳":"凳","擺":"摆","滿":"满","長":"长","戲":"戏",
    "雞":"鸡","鴨":"鸭","鵝":"鹅","乸":"乸","啼":"啼","咯":"咯","佢":"佢","咗":"咗",
    "個":"个","隻":"只","偷":"偷","舊":"旧","詈":"詈","語":"语","戲":"戏","謔":"谑",
    "駡":"骂","罵":"骂","記":"记","錄":"录","時":"时","代":"代","語":"语","域":"域",
})


IPA_FOLD = {
    "ɛ": "e", "ɔ": "o", "ɐ": "a", "ə": "e", "œ": "oe", "ø": "o", "ɶ": "o",
    "ɪ": "i", "ʊ": "u", "ʏ": "y", "ɯ": "u", "ɤ": "o", "ɑ": "a", "æ": "ae",
    "ŋ": "ng", "ɲ": "ny", "ɳ": "n", "ɬ": "sl", "ɡ": "g", "ɣ": "g",
    "ʔ": "'", "ʰ": "h", "ʷ": "w", "ː": ":", "̍": "", "ɕ": "s", "ʑ": "z",
}
TONE_LETTERS = set("˥˦˧˨˩↑↓↗↘˹˺˻") | {chr(c) for c in range(0x02E5, 0x02EA)}
SUP_TONES = set("¹²³⁴⁵⁰ˊˋˇˉ")

def _is_punct(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "PZC" and ch != "'"

def normalize(text: str):
    """返回 (norm_text, offsets, rules)。offsets[i] 是 norm_text[i] 在原文中的下标。"""
    out, offs, rules = [], [], set()
    for i, ch in enumerate(text):
        s = unicodedata.normalize("NFKC", ch)
        if s != ch:
            rules.add(f"nfkc:{ch}→{s}")
        for c in s:
            cf = c.casefold()
            if cf != c:
                rules.add(f"casefold:{c}→{cf}")
            for c2 in cf:
                if c2 in TRAD2SIMP:
                    rules.add(f"trad2simp:{c2}→{TRAD2SIMP[c2]}")
                    c2 = TRAD2SIMP[c2]
                base = ""
                for d in unicodedata.normalize("NFD", c2):
                    if unicodedata.category(d) in ("Mn", "Me"):
                        rules.add(f"strip_combining:U+{ord(d):04X}")
                    else:
                        base += d
                if base in IPA_FOLD:
                    rules.add(f"ipa_fold:{base}→{IPA_FOLD[base]}")
                    base = IPA_FOLD[base]
                if base and all(b in TONE_LETTERS or b in SUP_TONES for b in base):
                    rules.add(f"strip_tone:{base}")
                    continue
                for b in base:
                    if _is_punct(b):
                        rules.add("strip_punct")
                        continue
                    out.append(b)
                    offs.append(i)
    return "".join(out), offs, sorted(rules)

def _is_cjk(c: str) -> bool:
    return "一" <= c <= "鿿" or "㐀" <= c <= "䶿"

def tokenize(text: str):
    """原文分词，保留原文偏移；不做任何折叠（大小写、繁简、标点全部保留原样）。"""
    toks, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if _is_cjk(c):
            toks.append((c, i, i + 1))
            if i + 1 < n and _is_cjk(text[i + 1]):
                toks.append((text[i:i + 2], i, i + 2))
            i += 1
        elif c.isalnum():
            j = i
            while j < n and text[j].isalnum():
                j += 1
            toks.append((text[i:j], i, j))
            i = j
        else:
            i += 1
    return toks

def spans_to_original(norm_start, norm_end, offsets, orig_text):
    """把规范键上的命中区间映射回原文区间；尾部组合符并入高亮。"""
    a = offsets[norm_start]
    b = offsets[norm_end - 1] + 1
    while b < len(orig_text) and unicodedata.category(orig_text[b]) in ("Mn", "Me"):
        b += 1
    return [a, b]
