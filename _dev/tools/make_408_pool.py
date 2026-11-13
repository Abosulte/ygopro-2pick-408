#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 408 环境 2pick 卡池 (2pick/408.cdb)。

卡池唯一依据：官方白名单 408-whitelist.txt（ygobbs2 帖子 551010，12 段共 2386 条）。
    python3 make_408_pool.py
    python3 make_408_pool.py --exclude-forbidden      # 可选：额外剔除 2006.3.1 禁止卡

规则：
  1. 严格按白名单卡号取卡（白名单本身已排除异画：同卡的多个 ID 只列一个）。
     注意 datas.alias != 0 不等于异画——「鹰身女郎1/2/3」「传说之都 亚特兰蒂斯」
     是规则上视为同名的独立卡，官方白名单里都有，不能按 alias 删。
  2. 排除 TYPE_TOKEN (0x4000)（官方白名单本身不含任何 token / 衍生物）。
  3. 默认保留 2006.3.1 禁止卡（黑洞/死者苏生/雷击等），因为官方白名单包含它们；
     2pick 服务端把 CheckDeck 注释掉了，若不想要禁止卡进轮抽，加 --exclude-forbidden。
  4. texts 只保留卡池内 ID，便于人工核对卡名（pick.lua 不读 texts）。
  5. 默认不写 combo.lua：主题包请用 add_combo_from_ypk.py 逐包添加，
     重跑本脚本不会清空你已添加的包。只有显式传 --combo 才生成。
"""
import argparse
import json
import os
import re
import sqlite3
from _localenv import local_path

# 脚本位于 _dev/tools/，包根在两级之上；所有数据路径都以包根为基准
HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEV = os.path.join(HERE, "_dev")
# 两个「自备输入」不随项目分发（体积大 / 涉及卡数据授权），可用 --src / --lflist 指定
DEFAULT_SRC = local_path("SRC_408_CDB", os.path.join(DEV, "data", "1_408_TTT.cdb"))
DEFAULT_LFLIST = local_path("LFLIST_CONF", os.path.join(DEV, "data", "lflist.conf"))
DEFAULT_OUT = os.path.join(HERE, "2pick", "408.cdb")
DEFAULT_COMBO = os.path.join(HERE, "2pick", "combo.lua")
DEFAULT_THEMES = os.path.join(DEV, "data", "combo-themes.json")
DEFAULT_WHITELIST = os.path.join(DEV, "data", "408-whitelist.txt")

TYPE_TOKEN = 0x4000

# 408 主题包：每张尽量同主题，缺失/禁止卡由脚本从 FILLER 自动补足到 6 张。
THEMES = [
    ("青眼", ["青眼白龙", "青眼究极龙", "白龙之圣骑士", "龙之支配者", "呼龙笛", "融合"]),
    ("黑魔导", ["黑魔术师", "黑魔术少女", "黑魔族的幕帘", "千把刀", "光与暗之洗礼", "圣魔术师"]),
    ("零件", ["黄色零件", "绿色零件", "红色零件", "血之代偿", "限制解除", "地碎"]),
    ("帝王", ["黄泉青蛙", "雷帝 扎博尔格", "冰帝 美比乌斯", "炎帝 泰斯塔罗斯", "地帝 格兰玛格", "灵魂交换"]),
    ("不死", ["金字塔龟", "龙骨鬼", "削魂的死灵", "生者之书-禁断的咒术-", "不死王 巫妖", "抹杀之使徒"]),
    ("混沌光暗", ["混沌巫师", "异次元的女战士", "流氓佣兵部队", "圣魔术师", "电子龙", "旋风"]),
    ("均卡 Good Stuff", ["旋风", "地碎", "抹杀之使徒", "奈落的落穴", "炸裂装甲", "魔法筒"]),
    ("电子龙", ["电子龙", "原始电子龙", "限制解除", "融合", "融合回收", "闪电旋涡"]),
    ("守墓", ["守墓的侦察者", "守墓的咒术师", "守墓的使魔", "守墓的暗杀者", "守墓的监视者", "守墓的番兵"]),
    ("卡通", ["卡通世界", "青眼卡通龙", "卡通双生精灵", "卡通蛋龙", "卡通假面魔道士", "卡通哥布林突击部队"]),
    ("暗黑界", ["暗黑界的军神 希尔瓦", "暗黑界的武神 高尔德", "暗黑界的骑士 祖尔", "暗黑界的狂王 布隆", "暗黑界的斥候 斯卡", "暗黑界的尖兵 贝基"]),
    ("融合向", ["元素英雄 电光侠", "融合", "融合回收", "青眼白龙", "黑魔术师", "过早的埋葬"]),
    ("坑 BEAT", ["神之宣告", "王宫的通告", "技能抽取", "激流葬", "奈落的落穴", "炸裂装甲"]),
    ("亚马逊", ["亚马逊格斗战士", "亚马逊锁链使", "亚马逊圣战士", "亚马逊弩弓队", "亚马逊吹箭兵", "亚马逊宠物虎"]),
]

FILLER = [
    "旋风", "地碎", "抹杀之使徒", "奈落的落穴", "炸裂装甲", "魔法筒",
    "流氓佣兵部队", "异次元的女战士", "圣魔术师", "电子龙", "闪电旋涡",
    "神之宣告", "激流葬", "过早的埋葬", "融合", "黄泉青蛙",
]


def parse_lflist(path):
    """返回 forbidden 卡号集合（2006.3.1 表）。"""
    forbidden = set()
    section = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            low = line.lower()
            if low.startswith("#forbidden"):
                section = "forbidden"
                continue
            if low.startswith("#semi limit"):
                section = "semi"
                continue
            if low.startswith("#limit"):
                section = "limit"
                continue
            if line.startswith("!") or line.startswith("#") or not line:
                continue
            m = re.match(r"^(\d+)\s+(\d+)", line)
            if not m:
                continue
            code = int(m.group(1))
            if section == "forbidden":
                forbidden.add(code)
    return forbidden


def parse_whitelist(path):
    codes = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line and line.isdigit():
                codes.append(int(line))
    return list(dict.fromkeys(codes))


def build_pool(src, out, whitelist_path, lflist=None, exclude_forbidden=False):
    wl = parse_whitelist(whitelist_path)
    forbidden = parse_lflist(lflist) if (exclude_forbidden and lflist) else set()
    s = sqlite3.connect(src)
    meta = {cid: (ctype, alias) for cid, ctype, alias in s.execute("SELECT id, type, alias FROM datas")}
    names = dict(s.execute("SELECT id, name FROM texts"))
    s.close()

    final, dropped_token, dropped_forbidden, missing, alias_kept = [], [], [], [], []
    for code in wl:
        if code not in meta:
            missing.append(code)
            continue
        ctype, alias = meta[code]
        # 只按 TYPE_TOKEN 判定：名字含「衍生物」的「衍生物收获祭 / 谢肉祭」
        # 是正常陷阱卡，在官方白名单内，不能按名字误删。
        if ctype & TYPE_TOKEN:
            dropped_token.append(code)
            continue
        if exclude_forbidden and code in forbidden:
            dropped_forbidden.append(code)
            continue
        final.append(code)
        if alias:
            alias_kept.append(code)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    dst = sqlite3.connect(out)
    dst.execute("ATTACH DATABASE ? AS src", (src,))
    ph = ",".join("?" * len(final))
    dst.execute("CREATE TABLE datas AS SELECT * FROM src.datas WHERE id IN (%s)" % ph, tuple(final))
    dst.execute("CREATE TABLE texts AS SELECT * FROM src.texts WHERE id IN (SELECT id FROM datas)")
    dst.execute("CREATE INDEX idx_datas_id ON datas(id)")
    dst.execute("CREATE INDEX idx_texts_id ON texts(id)")
    dst.commit()
    total = dst.execute("SELECT COUNT(*) FROM datas").fetchone()[0]
    kind = {
        "怪兽": dst.execute("SELECT COUNT(*) FROM datas WHERE (type & 1) != 0").fetchone()[0],
        "魔法": dst.execute("SELECT COUNT(*) FROM datas WHERE (type & 2) != 0").fetchone()[0],
        "陷阱": dst.execute("SELECT COUNT(*) FROM datas WHERE (type & 4) != 0").fetchone()[0],
        "融合": dst.execute("SELECT COUNT(*) FROM datas WHERE (type & 0x40) != 0").fetchone()[0],
        "上级怪": dst.execute("SELECT COUNT(*) FROM datas WHERE (type & 1) != 0 AND (level & 0xff) >= 5").fetchone()[0],
        "下级怪": dst.execute("SELECT COUNT(*) FROM datas WHERE (type & 1) != 0 AND (level & 0xff) <= 4").fetchone()[0],
    }
    print("[卡池] 白名单 %s：%d 条" % (whitelist_path, len(wl)))
    print("[卡池] 排除 token %d，排除禁止卡 %d，源库缺失 %d" %
          (len(dropped_token), len(dropped_forbidden), len(missing)))
    print("[卡池] 保留规则同名卡(alias!=0) %d 张（鹰身女郎1/2/3 等，不是异画）" % len(alias_kept))
    print("[卡池] 输出 %s，共 %d 张" % (out, total))
    print("[卡池] 构成 %s" % kind)
    if missing:
        print("[卡池] 警告：白名单中源库不存在的卡号：%s" % missing[:20])
    build_levels(dst, os.path.join(os.path.dirname(out), "levels.lua"))
    return dst


DEFAULT_LEVELS = os.path.join(HERE, "2pick", "levels.lua")


def build_levels(conn, out):
    """导出 卡号->等级 表，替代现代内核没有的 Duel.ReadCard(code,CARDDATA_LEVEL)。

    pick.lua 只用等级避免同一轮出现同等级怪；表体积小（2386 条约 30 KB），
    加载一次即可，比逐张调内核便宜。
    """
    rows = conn.execute("SELECT id, (level & 0xff) FROM datas WHERE (type & 1) != 0").fetchall()
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("-- 408 卡池：卡号 -> 等级（由 make_408_pool.py 生成，勿手改）\n")
        f.write("-- 供 pick.lua 的 Auxiliary.CardLevel(code) 使用\nreturn {\n")
        for code, lv in sorted(rows):
            f.write("\t[%d]=%d,\n" % (code, lv))
        f.write("}\n")
    print("[levels] 输出 %s，%d 张怪兽" % (out, len(rows)))


def name_to_code(conn, name):
    r = conn.execute(
        "SELECT d.id FROM datas d JOIN texts t ON t.id = d.id WHERE t.name = ? ORDER BY d.id LIMIT 1",
        (name,),
    ).fetchone()
    return r[0] if r else None


def load_themes_from_json(conn, path):
    """从可编辑的 combo-themes.json 读主题；卡名优先，改卡名即换卡。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    themes = []
    for t in data["themes"]:
        codes = []
        for card in t.get("cards", []):
            code = name_to_code(conn, card.get("name", "")) or card.get("code")
            if code and conn.execute("SELECT 1 FROM datas WHERE id=?", (code,)).fetchone():
                if code not in codes:
                    codes.append(code)
        themes.append((t.get("name", "未命名"), codes))
    return themes


def build_combo(conn, out, themes_path=None):
    filler_codes = [c for c in (name_to_code(conn, n) for n in FILLER) if c]
    if themes_path and os.path.exists(themes_path):
        themes = load_themes_from_json(conn, themes_path)
        print("[combo] 主题来源：%s（%d 组）" % (themes_path, len(themes)))
    else:
        themes = [
            (n, [c for c in (name_to_code(conn, x) for x in names) if c])
            for n, names in THEMES
        ]
        print("[combo] 主题来源：脚本内置 THEMES（%d 组）" % len(themes))
    lines = [
        "-- 408 环境 2pick 主题卡包，由 make_408_pool.py 生成。",
        "-- 每包固定 6 张，全部卡号都存在于同目录 408.cdb，且已剔除 2006.3.1 禁止卡。",
        "combo_pack = {}",
        "",
        "combo_pack.pack = {",
    ]
    used_all = set()
    for theme, codes in themes:
        codes = list(codes)
        for c in filler_codes:
            if len(codes) >= 6:
                break
            if c not in codes:
                codes.append(c)
        codes = codes[:6]
        used_all.update(codes)
        lines.append("\t-- %s" % theme)
        lines.append("\t{%s}," % ",".join(str(c) for c in codes))
    lines.append("}")
    lines.append("")
    lines.append("return combo_pack")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("[combo] 输出 %s，%d 组，涉及 %d 张不同卡" % (out, len(themes), len(used_all)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC, help="源卡库 cdb（含 datas/texts）")
    ap.add_argument("--whitelist", default=DEFAULT_WHITELIST, help="官方 408 白名单卡号，每行一个")
    ap.add_argument("--lflist", default=DEFAULT_LFLIST, help="2006.3.1 禁限表 lflist.conf")
    ap.add_argument("--exclude-forbidden", action="store_true",
                    help="额外剔除 2006.3.1 禁止卡（默认保留，因官方白名单包含它们）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 2pick/408.cdb")
    ap.add_argument("--combo", default=None,
                    help="可选：同时重新生成 combo.lua；默认不写，避免清空已添加的主题包")
    ap.add_argument("--themes", default=DEFAULT_THEMES,
                    help="配合 --combo 使用；传 none 则用脚本内置 THEMES")
    a = ap.parse_args()
    for p in (a.src, a.whitelist):
        if not os.path.exists(p):
            raise SystemExit("找不到输入文件：%s" % p)
    conn = build_pool(a.src, a.out, a.whitelist, a.lflist, a.exclude_forbidden)
    if a.combo:
        themes_path = None if a.themes.lower() == "none" else a.themes
        build_combo(conn, a.combo, themes_path)
    else:
        print("[combo] 未指定 --combo，保持现有 combo.lua 不变；"
              "主题包请用 add_combo_from_ypk.py 添加")
    conn.close()


if __name__ == "__main__":
    main()
