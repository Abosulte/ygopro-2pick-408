#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 2pick/exclusive.lua：普通轮抽阶段永远不出现的卡（只走 combo 拿）。

维护方式（这是唯一需要你手改的文件）：
    2pick/exclusive.txt
        每行一张卡：  <卡号>  <卡名（可省，只用来核对）>
        以 # 开头是注释；空行忽略。

    python make_exclusive.py          # 生成 exclusive.lua 并做校验
    python make_exclusive.py --check  # 只校验，不写文件

自动部分（不用手写）：
    AUTO_LIMIT1 = True  -> limits.json 里所有「限制卡（限 1）」自动并入
    AUTO_LIMIT2 = False -> 需要连「准限制卡（限 2）」一起排除时改成 True

说明：
    * 卡号不在 2pick/408.cdb 里不会报错。exclusive 只是一张查表，
      卡池装载时逐行查 `exclusive[code]`，池里没有的卡号永远不会被查到；
      combo 走 Duel.CreateToken 直取卡号，也不依赖 408.cdb，所以依然能出。
    * 禁卡写在这里也无害（禁卡本来就不进任何池）。
"""
import io
import json
import os
import re
import sqlite3
import sys

# 脚本位于 _dev/tools/，包根在两级之上；所有数据路径都以包根为基准
HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEV = os.path.join(HERE, "_dev")
PICK_DIR = os.path.join(HERE, "2pick")
TXT = os.path.join(PICK_DIR, "exclusive.txt")
OUT = os.path.join(PICK_DIR, "exclusive.lua")
CDB = os.path.join(PICK_DIR, "408.cdb")
LIMITS = os.path.join(PICK_DIR, "limits.json")
COMBO = os.path.join(PICK_DIR, "combo.lua")

CHECK_ONLY = "--check" in sys.argv

# 额外来源：--ydk <file>，把 ydk 里出现的卡号也并入 exclusive（可多次指定）。
# 例：python make_exclusive.py --ydk "<本机卡组目录>/exclusive.ydk"
YDK_FILES = []
for _i, _a in enumerate(sys.argv):
    if _a == "--ydk" and _i + 1 < len(sys.argv):
        YDK_FILES.append(sys.argv[_i + 1])

# 自动把限制卡并入 exclusive（限制卡在轮抽里只允许 1 张，复制会被额度拒掉，
# 抽到就会让卡组少 1 张；直接不让它们出现最省事）
AUTO_LIMIT1 = True    # 限制卡（限 1）
AUTO_LIMIT2 = False   # 准限制卡（限 2）


def read(path):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def parse_txt(text):
    """返回 [(code, name_or_None)]，保持文件顺序，去重。"""
    out = []
    seen = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        token = parts[0]
        name = parts[1].strip() if len(parts) > 1 else None
        if not token.isdigit():
            print("  [跳过] 第 %d 行不是卡号：%s" % (lineno, raw.strip()))
            continue
        code = int(token)
        if code in seen:
            print("  [重复] %d 出现多次，只保留一次" % code)
            continue
        seen.add(code)
        out.append((code, name))
    return out


def load_cdb_names():
    con = sqlite3.connect(CDB)
    try:
        return dict(con.execute("select id, name from texts"))
    finally:
        con.close()


def cdb_ids():
    con = sqlite3.connect(CDB)
    try:
        return {r[0] for r in con.execute("select id from datas")}
    finally:
        con.close()


def parse_ydk_codes(path):
    """ydk 里出现的所有卡号（main/extra/side 都算）。"""
    out = []
    with io.open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if s.isdigit():
                out.append(int(s))
    return out


def combo_packs():
    """从 combo.lua 里读回每组包（只认 combo_pack.pack = { ... } 里的花括号）。"""
    if not os.path.exists(COMBO):
        return []
    s = read(COMBO)
    m = re.search(r"combo_pack\.pack\s*=\s*\{(.*?)\n\}", s, re.S)
    if not m:
        return []
    packs = []
    for body in re.findall(r"\{([^{}]*)\}", m.group(1)):
        ids = [int(x) for x in re.findall(r"\d+", body)]
        if ids:
            packs.append(ids)
    return packs


def main():
    if not os.path.exists(TXT):
        print("[失败] 找不到 %s" % TXT)
        return 1
    manual = parse_txt(read(TXT))
    names = load_cdb_names()
    pool = cdb_ids()

    # ---- 从 --ydk 追加：ydk 里出现的卡号直接并入 exclusive ----
    if YDK_FILES:
        have = {c for c, _ in manual}
        for p in YDK_FILES:
            if not os.path.exists(p):
                print("[提示] --ydk 指定的文件不存在，跳过：%s" % p)
                continue
            added = 0
            for c in parse_ydk_codes(p):
                if c in have:
                    continue
                have.add(c)
                manual.append((c, None))
                added += 1
            print("--ydk 追加：%s（新增 %d 张）" % (os.path.basename(p), added))
    limits = json.loads(read(LIMITS)) if os.path.exists(LIMITS) else {}
    forbidden = {int(x) for x in limits.get("forbidden", [])}
    limit1 = {int(x) for x in limits.get("limit1", [])}
    limit2 = {int(x) for x in limits.get("limit2", {})}

    auto = []
    if AUTO_LIMIT2:
        auto += [(c, names.get(c, ""), "准限制卡（限 2，自动）") for c in sorted(limit2)]
    if AUTO_LIMIT1:
        auto += [(c, names.get(c, ""), "限制卡（限 1，自动）") for c in sorted(limit1)]
    auto_codes = {c for c, _, _ in auto}
    auto = [x for x in auto if x[0] not in {c for c, _ in manual}]

    packs = combo_packs()
    in_combo = set()
    for p in packs:
        in_combo.update(p)

    print("=" * 62)
    print("exclusive.txt 手写：%d 张" % len(manual))
    print("自动并入限制卡：%d 张（限制1=%s，准限制2=%s）"
          % (len(auto), AUTO_LIMIT1, AUTO_LIMIT2))
    print("合计：%d 张不会出现在普通轮抽池" % (len(manual) + len(auto)))
    print("combo.lua：%d 组，共 %d 张卡号" % (len(packs), sum(len(p) for p in packs)))
    print("-" * 62)

    # ---- 校验（全部为提示级，不影响生成）----
    not_in_pool = [(c, n, src) for c, n, src in
                   [(c, names.get(c) or (n or ""), "手写") for c, n in manual] +
                   [(c, n, t) for c, n, t in auto] if c not in pool]
    if not_in_pool:
        print("提示：%d 张不在当前 408.cdb 卡池里（无害，只是本来也不会出现）：" % len(not_in_pool))
        for c, n, src in not_in_pool[:50]:
            print("    %d %s（%s）" % (c, n, src))
        if len(not_in_pool) > 50:
            print("    ... 其余 %d 张省略" % (len(not_in_pool) - 50))

    forb_in = [(c, names.get(c, "")) for c, _ in manual if c in forbidden]
    if forb_in:
        in_pool = sum(1 for c, _ in forb_in if c in pool)
        print("禁卡 %d 张：已按要求同时写入 408.cdb（有数据 %d 张）与 exclusive.lua；"
              % (len(forb_in), in_pool))
        print("  LoadDB 会跳过它们，普通轮抽不出现；只有 combo 主题包能发，"
              "且需先清掉 limits.lua 的 forbidden 才拿得动。")
    missing_forb = sorted(forbidden - {c for c, _ in manual} - auto_codes)
    if missing_forb:
        print("提示：limits.json 里有 %d 张禁卡没写进 exclusive.txt：%s"
              % (len(missing_forb), missing_forb[:20]))

    # 手写项才检查「有没有 combo 用」；自动的限制卡是故意拿不到的
    dead = [(c, names.get(c) or (n or "")) for c, n in manual
            if c not in in_combo and c not in forbidden]
    if dead:
        print("-" * 62)
        print("提示：exclusive.txt 里 %d 张既不在任何 combo、又不是禁卡，"
              "整局都拿不到（确认是故意的就忽略）：" % len(dead))
        for c, n in dead:
            print("    %d %s" % (c, n))

    bad_combo = []
    for i, p in enumerate(packs, 1):
        for c in p:
            if c in forbidden:
                bad_combo.append((i, c, "禁卡"))
            elif c not in pool:
                bad_combo.append((i, c, "不在当前卡池（CreateToken 仍可出，确认卡号正确即可）"))
    if bad_combo:
        print("-" * 62)
        print("提示：combo.lua 里有下列卡：")
        for i, c, why in bad_combo:
            print("   第 %d 组 卡 %d %s：%s" % (i, c, names.get(c, ""), why))

    # ---- 生成 ----
    lines = []
    if manual:
        lines.append("\t-- ===== 手写：2pick/exclusive.txt =====")
        for code, name in manual:
            note = names.get(code) or (name or "")
            lines.append("\t[%d]=true,\t-- %s" % (code, note))
    if auto:
        lines.append("\t-- ===== 自动：禁限表里的限制卡 =====")
        for code, name, _ in auto:
            lines.append("\t[%d]=true,\t-- %s" % (code, name or ""))

    body = ("-- 由 make_exclusive.py 生成，不要手改这个文件\n"
            "-- 这些卡在普通轮抽阶段永远不出现；combo 走卡号直取，照样能出\n"
            "return {\n" + "\n".join(lines) + ("\n" if lines else "") + "}\n")

    print("-" * 62)
    if CHECK_ONLY:
        print("（--check 模式，未写文件）")
    else:
        with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        print("已生成 %s（%d 张，%d 字节）"
              % (OUT, len(manual) + len(auto), len(body.encode("utf-8"))))

    print("上传 2pick/exclusive.lua 后重开房间即生效（无需重编）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
