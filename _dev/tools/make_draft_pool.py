#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用实战卡组 ydk 重建 2pick 卡池 408.cdb。

步骤：收集 .ydk 卡号 -> 异画/复刻按 alias 或同名最小卡号映射到 408 基准 -> 去重 -> 写 cdb。
用法：python make_draft_pool.py [--deck-dir <目录>] [--stats] [--use-exclude] [--dry-run]
注意：旧 cdb 备份到 2pick/_pool_backup/；levels.lua / limits.lua 以全卡池为超集，
      卡池变小不用重生成；重跑 make_408_pool.py 会覆盖本脚本成果。
"""

import argparse
import collections
import glob
import io
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
from _localenv import local_path

# 脚本位于 _dev/tools/，包根在两级之上；所有数据路径都以包根为基准
HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEV = os.path.join(HERE, "_dev")
PICK_DIR = os.path.join(HERE, "2pick")
CDB = os.path.join(PICK_DIR, "408.cdb")
LIMITS = os.path.join(PICK_DIR, "limits.json")
BACKUP_DIR = os.path.join(DEV, "backup", "_pool_backup")
# 全量 408 白名单基准（2386 张）。卡池会被本脚本缩小，所以基准必须独立保存，
# 否则第二次运行会「以缩小后的池为准」，越跑越小。
BASE_CDB = os.path.join(BACKUP_DIR, "408-full-2386.cdb")

DEFAULT_DECK_DIR = local_path("YDK_DECK_DIR")
# combo 主题包源目录。这里的卡**也全部并入卡池**（用户要求）：
#   ① combo 阶段 Duel.CreateToken 按卡号查 DataManager，池里没有就是空卡，
#      所以 combo 用到的卡必须有数据；
#   ② 与其只补数据再把它们从普通池排除，不如直接让它们可被普通轮抽抽到。
# 仍然受 exclusive / 限 1 / 禁卡表约束（那几类本来就不进普通池）。
DEFAULT_COMBO_DIR = local_path("YDK_COMBO_DIR")
# 黑名单：--use-exclude 时才会读，里面的卡号一律不进卡池
# 永久排除（不依赖 --use-exclude，空集=不使用）。**当前为空，不是遗漏**：
#   2026-11-11 曾在此硬编码 {90660762} 流星黑龙，2026-11-13 用户澄清「只是不想让
#   408.cdb 里有流星黑龙」，并非要一个代码级黑名单 —— 而该卡早已从源卡组
#   轮抽服务器/408 暗黑界.ydk 的 #extra 段删除（76 个 ydk 全量扫描确认 0 命中），
#   所以移除硬编码后重跑，408.cdb 依然不含它。改由源数据（ydk）决定卡池内容。
# 若以后要「临时整批剔卡」，用 DEFAULT_EXCLUDE_YDK 指向的待删除.ydk，别改这里。
FORCE_EXCLUDE_CODES = set()
DEFAULT_EXCLUDE_YDK = local_path("YDK_EXCLUDE_FILE")
# 408 禁卡（limits.json 的 forbidden，2006.3 表共 28 张）是否留在 408.cdb 里。
# True （当前）：28 张禁卡的数据进 cdb，但 2pick/exclusive.lua 会把它们标成
#   「只走 combo」—— pick.lua 的 LoadDB 见到 exclusive[code] 直接跳过，
#   所以普通轮抽池的实际张数不变，禁卡只能通过 combo 主题包定向发放。
#   好处：客户端/服务端能查到卡名、卡图、等级，cardnames.txt 也完整。
# False：老的建法，禁卡连数据都不进 cdb。
KEEP_FORBIDDEN_IN_CDB = True
DEFAULT_STATS_URL = local_path("CARD_STATS_URL")   # 留空则用本地快照
STATS_CACHE = os.path.join(DEV, "data", "_sources", "card-stats-408.json")
LOCALE_DIR = local_path("MDPro3_LOCALES_DIR")
LOCALE_CDBS = [os.path.join(LOCALE_DIR, loc, "cards.cdb") for loc in
               ("zh-CN", "zh-TW", "ja-JP", "en-US")]

TYPE_MONSTER = 0x1
TYPE_SPELL = 0x2
TYPE_TRAP = 0x4
TYPE_FUSION = 0x40
TYPE_SYNCHRO = 0x2000
TYPE_XYZ = 0x800000
TYPE_LINK = 0x4000000
TYPE_TOKEN = 0x4000


def parse_ydk(path):
    """返回文件里所有卡号（含 main/extra/side）。"""
    out = []
    with io.open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if s.isdigit():
                out.append(int(s))
    return out


def find_locale_cdb():
    for p in LOCALE_CDBS:
        if os.path.exists(p):
            return p
    raise SystemExit("[失败] 找不到本地化 cards.cdb（用来查异画卡名），"
                     "请用 --locale-cdb 指定")


def load_full_names(path, ids):
    """从完整 cards.cdb 里查这些卡号的卡名。"""
    con = sqlite3.connect(path)
    try:
        names = {}
        ids = list(ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = "select id,name from texts where id in (%s)" % ",".join("?" * len(chunk))
            names.update(dict(con.execute(q, chunk)))
        return names
    finally:
        con.close()


def fetch_stats(url_tpl, timeout=40):
    """联网抓 408 天梯单卡使用率（接口 pageSize 上限 100，必须分页）。"""
    cards = {}
    total = None
    for page in range(1, 60):
        url = url_tpl.format(page=page)
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                  "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        total = d.get("total", total)
        batch = d.get("cards") or []
        if not batch:
            break
        for c in batch:
            cards[int(c["id"])] = c.get("name", "")
        if total is not None and len(cards) >= total:
            break
    if not cards:
        raise RuntimeError("接口没返回任何卡片")
    return cards, total


def load_stats(url_tpl, refresh):
    """返回 ({卡号: 卡名}, 来源说明)。默认用本地快照，--refresh-stats 才联网。"""
    if refresh or not os.path.exists(STATS_CACHE):
        try:
            cards, total = fetch_stats(url_tpl)
        except Exception as e:
            if os.path.exists(STATS_CACHE):
                print("  [警告] 联网抓取失败（%s），改用本地快照" % e)
                data = json.load(io.open(STATS_CACHE, encoding="utf-8"))
                return {int(c["id"]): c.get("name", "") for c in data["cards"]}, "本地快照（联网失败）"
            raise SystemExit("[失败] 抓取使用率榜单失败：%s\n"
                             "（可以用 --no-stats 跳过，或检查网络/代理）" % e)
        os.makedirs(os.path.dirname(STATS_CACHE), exist_ok=True)
        data = {"total": total, "cards": [{"id": k, "name": v} for k, v in cards.items()],
                "fetched": time.strftime("%Y-%m-%d %H:%M:%S")}
        with io.open(STATS_CACHE, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
        return cards, "联网抓取 %d 张" % len(cards)
    data = json.load(io.open(STATS_CACHE, encoding="utf-8"))
    return ({int(c["id"]): c.get("name", "") for c in data["cards"]},
            "本地快照 %s（%d 张）" % (data.get("fetched", "?"), len(data["cards"])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck-dir", default=DEFAULT_DECK_DIR)
    ap.add_argument("--locale-cdb", default=None)
    ap.add_argument("--out", default=CDB)
    ap.add_argument("--base-cdb", default=None,
                    help="408 全量白名单 cdb（默认 2pick/_pool_backup/408-full-2386.cdb）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats-url", default=DEFAULT_STATS_URL)
    ap.add_argument("--stats", dest="no_stats", action="store_false",
                    help="并入 408 天梯单卡使用率榜单（默认不并，只用 ydk 里的卡）")
    ap.set_defaults(no_stats=True)
    ap.add_argument("--refresh-stats", action="store_true", help="强制重新联网抓取榜单")
    ap.add_argument("--combo-dir", default=DEFAULT_COMBO_DIR,
                    help="combo 主题包源目录（里面的卡只保证数据存在，不进普通池）")
    ap.add_argument("--no-combo", action="store_true",
                    help="不把 combo 目录的卡并入 408.cdb 数据")
    ap.add_argument("--exclude-ydk", action="append", default=None,
                    help="剔除名单 ydk（可多次指定，需配合 --use-exclude）")
    ap.add_argument("--use-exclude", dest="no_exclude", action="store_false",
                    help="启用黑名单（默认不启用，ydk 里出现的卡全都要）")
    ap.set_defaults(no_exclude=True)
    a = ap.parse_args()

    deck_dir = a.deck_dir
    if not os.path.isdir(deck_dir):
        raise SystemExit("[失败] 卡组目录不存在：%s" % deck_dir)
    files = sorted(glob.glob(os.path.join(deck_dir, "**", "*.ydk"), recursive=True))
    if not files:
        raise SystemExit("[失败] %s 下没有 .ydk" % deck_dir)

    raw = collections.Counter()
    for f in files:
        for c in parse_ydk(f):
            raw[c] += 1
    deck_ids = set(raw)
    print("=" * 66)
    print("卡组目录：%s" % deck_dir)
    print("ydk 文件：%d 个，卡号条目 %d，去重卡号 %d"
          % (len(files), sum(raw.values()), len(deck_ids)))

    stats_ids = {}
    if not a.no_stats:
        stats_ids, src = load_stats(a.stats_url, a.refresh_stats)
        print("天梯使用率榜单：%s，去重卡号 %d（其中卡组里没有的 %d 张）"
              % (src, len(stats_ids), len(set(stats_ids) - deck_ids)))
    # ---- combo 目录：卡的数据必须进 cdb，否则 combo 阶段 CreateToken 拿不到卡 ----
    combo_ids = set()
    if not a.no_combo and os.path.isdir(a.combo_dir):
        cfiles = sorted(glob.glob(os.path.join(a.combo_dir, "**", "*.ydk"), recursive=True))
        craw = collections.Counter()
        for f in cfiles:
            for c in parse_ydk(f):
                craw[c] += 1
        combo_ids = set(craw)
        print("combo 目录：%s（%d 个 ydk，去重卡号 %d；其中 %d 张不在卡组目录内）"
              % (a.combo_dir, len(cfiles), len(combo_ids), len(combo_ids - deck_ids)))
    elif not a.no_combo:
        print("  [提示] combo 目录不存在，跳过：%s" % a.combo_dir)

    all_ids = deck_ids | set(stats_ids) | combo_ids

    # 408 白名单基准（全量），不是当前已缩小的 408.cdb
    base = a.base_cdb or (BASE_CDB if os.path.exists(BASE_CDB) else CDB)
    if base == CDB:
        print("  [警告] 找不到全量基准 %s，退回用当前 408.cdb（只能越缩越小）" % BASE_CDB)
    src = sqlite3.connect(base)
    pool = {r[0]: r for r in src.execute("select id,type,level from datas")}
    pname = dict(src.execute("select id,name from texts"))
    by_name = collections.defaultdict(list)
    for cid, n in pname.items():
        by_name[n].append(cid)
    for n in by_name:
        by_name[n].sort()
    print("408 白名单基准：%s（%d 张）" % (os.path.basename(base), len(pool)))

    # ---- 黑名单（--exclude-ydk）----
    exclude_files = [] if a.no_exclude else (a.exclude_ydk or [DEFAULT_EXCLUDE_YDK])
    excluded = set(FORCE_EXCLUDE_CODES)
    if FORCE_EXCLUDE_CODES:
        print("永久排除 %d 张：%s" % (len(FORCE_EXCLUDE_CODES),
              "、".join(pname.get(c, str(c)) for c in sorted(FORCE_EXCLUDE_CODES))))
    for ef in exclude_files:
        if not os.path.exists(ef):
            print("  [提示] 剔除名单不存在，跳过：%s" % ef)
            continue
        ids = set(parse_ydk(ef))
        excluded |= ids
        print("剔除名单：%s（%d 张）" % (os.path.basename(ef), len(ids)))

    # ---- 异画 / 复刻 -> 原版 ----
    # 判定顺序：① alias（异画卡在 cards.cdb 里 alias 指向原版）
    #           ② 同名卡里「408 全量基准内」最小的卡号
    #           ③ 都不行就丢弃并打印，方便手工核对
    missing = sorted(c for c in all_ids if c not in pool)
    mapped, dropped, via_alias = {}, [], []
    if missing:
        locale = a.locale_cdb or find_locale_cdb()
        loc = sqlite3.connect(locale)
        try:
            for c in missing:
                target = None
                row = loc.execute("select alias from datas where id=?", (c,)).fetchone()
                if row and row[0] and int(row[0]) in pool:
                    target = int(row[0])
                    via_alias.append((c, target))
                else:
                    row = loc.execute("select name from texts where id=?", (c,)).fetchone()
                    hits = by_name.get(row[0]) if row and row[0] else None
                    if hits:
                        target = hits[0]
                if target:
                    mapped[c] = target
                else:
                    row = loc.execute("select name from texts where id=?", (c,)).fetchone()
                    dropped.append((c, row[0] if row else None))
        finally:
            loc.close()

    print("池外卡号：%d 张 -> 映射回原版 %d 张（其中按 alias %d 张），丢弃 %d 张"
          % (len(missing), len(mapped), len(via_alias), len(dropped)))
    for c in sorted(mapped):
        print("    %d -> %d  %s" % (c, mapped[c], pname.get(mapped[c], "")))
    if dropped:
        print("  无法映射（已丢弃）：")
        for c, n in dropped:
            print("    %d %s" % (c, n or "（本地卡库查不到）"))

    # ---- 合并 ----
    final = set()
    for c in all_ids:
        if c in pool:
            final.add(c)
        elif c in mapped:
            final.add(mapped[c])

    # ---- 剔除黑名单（异画卡先映射成原版再比对，否则漏删）----
    if excluded:
        drop_ids = set(excluded) | {mapped[c] for c in excluded if c in mapped}
        hit = sorted(final & drop_ids)
        print("从卡池剔除 %d 张：%s" % (len(hit), [pname.get(c, c) for c in hit]))
        for c in sorted(drop_ids - final):
            print("  [提示] %d %s 本来就不在卡池，忽略" % (c, pname.get(c, "?")))
        final -= drop_ids

    # ---- 禁卡：KEEP_FORBIDDEN_IN_CDB=True 时数据进 cdb，但由 exclusive.lua 拦截 ----
    limits = json.loads(io.open(LIMITS, encoding="utf-8").read()) if os.path.exists(LIMITS) else {}
    forbidden = {int(x) for x in limits.get("forbidden", [])}
    dropped = sorted(final & forbidden)
    if KEEP_FORBIDDEN_IN_CDB:
        # 强制把 28 张禁卡都放进来（禁卡不在任何实战卡组/榜单里，靠收集是收不到的）。
        # 黑名单优先级更高：黑名单里若也有，就不加回去。
        add_forbidden = (forbidden & set(pool)) - excluded - {mapped[c] for c in excluded if c in mapped}
        final |= add_forbidden
        print("保留禁卡 %d 张进 408.cdb（exclusive.lua 拦住，不进普通轮抽池）" % len(add_forbidden))
    else:
        final -= forbidden

    # ---- 统计 ----
    cnt = collections.Counter()
    for c in final:
        t, lv = pool[c][1], pool[c][2]
        if t & TYPE_TOKEN:
            cnt["token"] += 1
        if t & (TYPE_FUSION | TYPE_SYNCHRO | TYPE_XYZ | TYPE_LINK):
            cnt["extra"] += 1
        elif t & TYPE_MONSTER:
            cnt["monster"] += 1
            cnt["lv>=5" if lv > 4 else "lv<=4"] += 1
        elif t & TYPE_SPELL:
            cnt["spell"] += 1
        elif t & TYPE_TRAP:
            cnt["trap"] += 1

    print("-" * 66)
    if not KEEP_FORBIDDEN_IN_CDB:
        print("去掉禁卡 %d 张：%s" % (len(dropped), [pname.get(c, c) for c in dropped]))
    print("最终卡池：%d 张（其中禁卡 %d 张，由 exclusive.lua 排除）"
          % (len(final), len(final & forbidden)))

    print("  主卡组怪兽 %d（LV<=4 %d / LV>=5 %d）"
          % (cnt["monster"], cnt["lv<=4"], cnt["lv>=5"]))
    print("  魔法 %d  陷阱 %d  额外（融合/同调/超量/连接）%d"
          % (cnt["spell"], cnt["trap"], cnt["extra"]))

    # 各轮候选够不够（每轮最多要 8 张不重复）
    need = {
        "轮1/2 低星怪": cnt["lv<=4"], "轮1/2 高星怪": cnt["lv>=5"],
        "轮1/2 魔法": cnt["spell"], "轮1/2 陷阱": cnt["trap"],
        "轮3-5 怪兽": cnt["monster"], "额外轮 融合": cnt["extra"],
    }
    warn = [k for k, v in need.items() if v < 8]
    if warn:
        print("  [警告] 下列分类不足 8 张，可能抽不满：%s" % warn)
    else:
        print("  各轮候选充足（每类 >= 8 张）")

    # ---- combo 卡完整性：不在 final 里说明数据缺失，combo 阶段会拿到空卡 ----
    combo_all = {mapped.get(c, c) for c in combo_ids}
    combo_bad = sorted(c for c in combo_all if c not in final)
    if combo_bad:
        print("  [警告] 下列卡在 combo 里出现，但数据进不了 408.cdb（combo 会拿到空卡）：")
        for c in combo_bad:
            print("    %d %s" % (c, pname.get(c) or "（基准池也查不到）"))
    # combo 目录里、原本不在卡组目录的卡，现在也进了普通池
    combo_new = sorted({mapped.get(c, c) for c in (combo_ids - deck_ids)} & final)
    print("combo 目录额外并入卡池：%d 张%s"
          % (len(combo_new),
             (" " + "、".join(pname.get(c, str(c)) for c in combo_new)) if combo_new else ""))

    if a.dry_run:
        print("\n（--dry-run，未写文件）")
        return 0

    # ---- 写新 cdb ----
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        old_count = sqlite3.connect(CDB).execute("select count(*) from datas").fetchone()[0]
    except sqlite3.Error:
        old_count = -1
    bak = os.path.join(BACKUP_DIR, "408-%s-%d张.cdb" % (stamp, old_count))
    shutil.copy2(CDB, bak)
    print("-" * 66)
    print("已备份旧卡池 -> %s" % bak)

    tmp = CDB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    try:
        for t in ("datas", "texts"):
            ddl = src.execute(
                "select sql from sqlite_master where type='table' and name=?", (t,)).fetchone()
            if ddl:
                dst.execute(ddl[0])
        for t in ("datas", "texts"):
            cols = [r[1] for r in src.execute("pragma table_info(%s)" % t)]
            if not cols:
                continue
            ids = sorted(final)
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                q = "select * from %s where id in (%s)" % (t, ",".join("?" * len(chunk)))
                rows = src.execute(q, chunk).fetchall()
                dst.executemany(
                    "insert into %s values (%s)" % (t, ",".join("?" * len(cols))), rows)
        try:
            dst.execute("create unique index datas_id on datas(id)")
            dst.execute("create unique index texts_id on texts(id)")
        except sqlite3.OperationalError:
            pass
        dst.commit()
        got = dst.execute("select count(*) from datas").fetchone()[0]
    finally:
        dst.close()
        src.close()

    if got != len(final):
        os.remove(tmp)
        raise SystemExit("[失败] 写入条数不符：期望 %d，实际 %d" % (len(final), got))
    try:
        os.replace(tmp, a.out)
    except OSError as e:
        # 本机（Windows + 安全软件）偶尔不允许「rename 覆盖已有文件」，
        # 报 WinError 5 拒绝访问；退化成先写内容、再删临时文件。
        print("  [提示] os.replace 失败（%s），改用直接覆盖写入" % e)
        shutil.copyfile(tmp, a.out)
        os.remove(tmp)
    print("已写入 %s（%d 张，%d 字节）" % (a.out, got, os.path.getsize(a.out)))
    print("\n下一步：跑 更新-仪式.bat（重新生成 ritual.lua）和 更新-exclusive.bat，"
          "再上传 2pick-408 执行 push-rules.sh。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
