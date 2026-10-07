# -*- coding: utf-8 -*-
"""演示种子数据：同音异义、异体字、地区变体、尊重性语境说明、媒体、不可转换标音。"""
from .db import DB
from .service import Service

def seed(db: DB, svc: Service):
    if db.one("SELECT * FROM users LIMIT 1"):
        return
    db.execute("INSERT INTO users VALUES('editor1','ed-token','editor')")
    db.execute("INSERT INTO users VALUES('approver1','ap-token','approver')")

    src1 = svc.add_source("editor1", "《岭南方言田野调查卷三》", "陈记语言工作室", 1987, "CC BY-NC")
    src2 = svc.add_source("editor1", "《城关老话口述史》", "县文化馆", 2003, "内部资料")

    # 1) 同音异义：生 / 甥 同读 saang1，身份各自独立，用 homophone 边关联
    e_sang = svc.create_entry("editor1", "生")
    svc.add_pron("editor1", e_sang, "yd", "saang1", region="城关", source_id=src1)
    s1 = svc.add_sense("editor1", e_sang, 1, "出生；生长", pos="动", region="城关")
    svc.add_example("editor1", s1, "佢孫女上個月出世，好開心！", "他孙女上个月出生，很开心。",
                    region="城关", source_id=src2)
    e_sang2 = svc.create_entry("editor1", "甥")
    svc.add_pron("editor1", e_sang2, "yd", "saang1", region="城关", source_id=src1)
    svc.add_sense("editor1", e_sang2, 1, "外甥；姐妹之子", pos="名", region="城关")
    svc.add_relation("editor1", e_sang, e_sang2, "homophone", "同音 saang1，义项无涉")

    # 2) 异体字：臺 下挂 台（简）、枱（异体）
    e_tai = svc.create_entry("editor1", "臺")
    svc.add_form("editor1", e_tai, "臺", script="trad", is_standard=1)
    svc.add_form("editor1", e_tai, "台", script="simp")
    svc.add_form("editor1", e_tai, "枱", script="variant", note="俗写，多见于家具义")
    svc.add_pron("editor1", e_tai, "yd", "toi4", region="城关", source_id=src1)
    s_tai = svc.add_sense("editor1", e_tai, 1, "高而平的建筑物；臺灣亦省作台", pos="名")
    svc.add_example("editor1", s_tai, "戲臺前擺滿長凳。", "戏台前摆满长凳。", source_id=src2)

    # 3) 地区变体：玉米（北）/ 粟米（粤） regional_variant 边
    e_ym = svc.create_entry("editor1", "玉米")
    svc.add_pron("editor1", e_ym, "yd", "nguk6 mai5", region="北片")
    svc.add_sense("editor1", e_ym, 1, "玉蜀黍，粮食作物", pos="名", region="北片")
    e_sm = svc.create_entry("editor1", "粟米")
    svc.add_pron("editor1", e_sm, "yd", "suk1 mai5", region="粤海片")
    svc.add_sense("editor1", e_sm, 1, "即玉米；粤海片说法", pos="名", region="粤海片")
    svc.add_relation("editor1", e_ym, e_sm, "regional_variant", "北片/粤海片异名同物")

    # 4) 尊重性语境说明 + 无音频文本（transcript）
    e_qd = svc.create_entry("editor1", "契弟",
        context_note="旧时詈语，今用于熟人戏谑或詈骂，语境敏感；本词典仅作语言文献记录，"
                     "引用时请说明时代与语域，避免对特定群体的不尊重。")
    svc.add_pron("editor1", e_qd, "yd", "kai3 dai6", region="城关", source_id=src1)
    s_qd = svc.add_sense("editor1", e_qd, 1, "詈语；亦作熟人戏称（二十世纪中叶城关用法）",
                         pos="名", region="城关")
    m1 = svc.add_media("editor1", "/media/kai3dai6_1962.wav",
                       transcript="录音（1962，城关，男，62岁）：「你個契弟，又偷我隻雞！」")
    svc.add_example("editor1", s_qd, "你個契弟，又偷我隻雞！", "你这个家伙，又偷我的鸡！",
                    region="城关", source_id=src2, media_id=m1)

    # 5) 可转换与不可转换标音对照
    e_baak = svc.create_entry("editor1", "白")
    p_baak = svc.add_pron("editor1", e_baak, "yd", "baak6", region="城关", source_id=src1)
    svc.add_sense("editor1", e_baak, 1, "颜色白；明白", pos="形")
    svc.convert_pron(p_baak, "ipa")      # ok：paːk̚˨
    svc.convert_pron(p_baak, "plain")    # unconvertible：入声尾
    e_sit = svc.create_entry("editor1", "屑")
    p_sit = svc.add_pron("editor1", e_sit, "yd", "xeo3", region="山乡", source_id=src2)
    svc.add_sense("editor1", e_sit, 1, "碎末；不屑", pos="名", region="山乡")
    svc.convert_pron(p_sit, "ipa")       # unconvertible：韵母 eo 未收录，拒绝猜读

    # 6) 引用：外部文章引用「生」义项
    svc.create_citation("editor1", "https://example.org/articles/chengguan-phonology#s3", s1)
