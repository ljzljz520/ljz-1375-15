"""演示数据：覆盖同音异义、异体字、地区变体、简繁、标点、
音标组合字符、音频、引用文章等场景。"""
from . import services as S


def seed(conn):
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        return False
    ed = {"id": "u_editor", "role": "editor"}
    ap = {"id": "u_approver", "role": "approver"}
    conn.execute("INSERT INTO users VALUES ('u_editor','阿珍(编辑)','editor','tok-editor')")
    conn.execute("INSERT INTO users VALUES ('u_approver','阿强(审校)','approver','tok-approver')")

    rg_gz = S.add_region(conn, ed, "广州")
    rg_hk = S.add_region(conn, ed, "香港")
    src1 = S.add_source(conn, ed, "《广州话正音字典》", "詹伯慧 等", 2002)
    src2 = S.add_source(conn, ed, "田野调查笔记", "本课题组", 2024)

    def publish_all(ids):
        for eid in ids:
            S.submit_entry(conn, ed, eid)
            S.approve_entry(conn, ap, eid)
            S.publish_entry(conn, ap, eid)

    # 1) 佢 —— 第三人称代词（带音频、语境说明）
    e_keoi = S.create_entry(conn, ed, "佢", "粤语第三人称代词。")
    S.add_pronunciation(conn, ed, e_keoi, "jyutping", "keoi5", rg_gz)
    s1 = S.add_sense(conn, ed, e_keoi,
                     "第三人称单数代词，相当于普通话的“他/她/它”。",
                     "日常通用，无敬谦色彩；对长辈宜改用称呼以示尊重。", rg_gz)
    x1 = S.add_example(conn, ed, s1, "佢今日唔嚟。", "他今天不来。", src2, rg_gz)
    S.attach_audio(conn, ed, x1, "/static/audio/silence.wav",
                   "佢今日唔嚟（keoi5 gam1 jat6 m4 lai4）")
    S.convert_pronunciations(conn, ed, e_keoi, "ipa")   # keoi5 在表中 -> ok

    # 2) 渠 —— 佢 的异体字
    e_qu = S.create_entry(conn, ed, "渠", "“佢”的异体写法，见于早期文献。")
    S.add_pronunciation(conn, ed, e_qu, "jyutping", "keoi5")
    S.add_sense(conn, ed, e_qu, "同“佢”，第三人称代词（异体写法）。")
    S.add_relation(conn, ed, e_qu, e_keoi, "variant_char", "异体字，见主词条")

    # 3) 芳 / 方 —— 同音异义（身份各自独立，关系边互链）
    e_fang = S.create_entry(conn, ed, "芳")
    S.add_pronunciation(conn, ed, e_fang, "jyutping", "fong1")
    S.add_sense(conn, ed, e_fang, "香，花草的香气。")
    e_fong = S.create_entry(conn, ed, "方")
    S.add_pronunciation(conn, ed, e_fong, "jyutping", "fong1")
    S.add_sense(conn, ed, e_fong, "方向；方法。")
    S.add_relation(conn, ed, e_fang, e_fong, "homophone", "同音（fong1）异义")

    # 4) 雪櫃 / 冰箱 —— 地区变体
    e_syut = S.create_entry(conn, ed, "雪櫃", "香港地区说法。")
    S.add_pronunciation(conn, ed, e_syut, "jyutping", "syut3 gwai6", rg_hk)
    S.add_sense(conn, ed, e_syut, "冷藏食物的电器（香港说法）。", "", rg_hk)
    S.convert_pronunciations(conn, ed, e_syut, "yale")  # gwai6 无映射 -> 未转换
    e_bing = S.create_entry(conn, ed, "冰箱")
    S.add_pronunciation(conn, ed, e_bing, "jyutping", "bing1 syut3")
    S.add_sense(conn, ed, e_bing, "冷藏食物的电器（通用说法）。", "", rg_gz)
    S.add_relation(conn, ed, e_syut, e_bing, "regional_variant", "香港/通用对应")

    # 5) 龍舟 —— 繁体词形 + 带标点例句
    e_long = S.create_entry(conn, ed, "龍舟")
    S.add_pronunciation(conn, ed, e_long, "jyutping", "lung4 zau1")
    s5 = S.add_sense(conn, ed, e_long, "端午节竞渡用的长形木船。")
    S.add_example(conn, ed, s5, "龍舟一過，鞭炮齊鳴！", "龙舟一过，鞭炮齐鸣。", src1)

    # 6) 食饭 —— 带尊重性语境说明
    e_sik = S.create_entry(conn, ed, "食饭")
    S.add_pronunciation(conn, ed, e_sik, "jyutping", "sik6 faan6")
    S.add_sense(conn, ed, e_sik, "吃饭。",
                "熟人之间直说“食饭未？”是亲切问候；正式场合宜用“用膳”。", rg_gz)

    # 7) 唔該 —— 礼貌用语，例句含标点
    e_mgoi = S.create_entry(conn, ed, "唔該")
    S.add_pronunciation(conn, ed, e_mgoi, "jyutping", "m4 goi1")
    s7 = S.add_sense(conn, ed, e_mgoi, "谢谢；劳驾。",
                     "礼貌用语；对服务者道“唔該”是基本尊重。")
    S.add_example(conn, ed, s7, "唔該，唔該！", "谢谢，谢谢！", src2)

    # 8) 妈 —— 普通话对照，拼音含预组合声调字符 mā
    e_ma = S.create_entry(conn, ed, "妈", "普通话对照词条。")
    S.add_pronunciation(conn, ed, e_ma, "pinyin", "mā")
    S.add_sense(conn, ed, e_ma, "母亲（普通话对照）。")

    publish_all([e_keoi, e_qu, e_fang, e_fong, e_syut, e_bing, e_long,
                 e_sik, e_mgoi, e_ma])

    # 引用文章：固定引用 佢 的义项 v1
    S.create_article(conn, ed, "粤语代词小考",
                     "粤语第三人称“佢”源流久远……",
                     [{"sense_id": s1, "sense_version": 1}])
    conn.commit()
    return True
