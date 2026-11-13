#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把目录里的 .ydk 生成 2pick 主题包 combo.lua（以目录为准，整份重写）。

用法：python make_combo.py [--dir <目录>] [--check]   # 默认读 local-paths.json
规则：一个 ydk = 一组；#main + #extra 卡号全收（保留同名重复），取前 6 张，
      少于 3 张只提示；禁卡 / 超额只提示，由 pick.lua 的 CanPick 裁掉。
注意：手工加包要改 ydk 目录，不要直接改 combo.lua（会被下次重跑覆盖）。
"""
import argparse
import io
import json
import os
import sqlite3
import sys
from _localenv import local_path

# 脚本位于 _dev/tools/，包根在两级之上；所有数据路径都以包根为基准
HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEV = os.path.join(HERE, "_dev")
PICK_DIR = os.path.join(HERE, "2pick")
OUT = os.path.join(PICK_DIR, "combo.lua")
CDB = os.path.join(PICK_DIR, "408.cdb")
LIMITS = os.path.join(PICK_DIR, "limits.json")
DEFAULT_DIR = local_path("YDK_COMBO_DIR")

MIN_PACK, MAX_PACK = 3, 6


def parse_ydk(path):
    """返回 [(code, 段名)]，保留重复与顺序（main 在前、extra 在后）。"""
    out = []
    section = "main"
    with io.open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                if s[1:].startswith("extra"):
                    section = "extra"
                elif s[1:].startswith("main"):
                    section = "main"
                elif s.startswith("!side"):
                    section = "side"
                continue
            if s.isdigit():
                out.append((int(s), section))
    return out


def load_names():
    """卡名优先用当前卡池，池外补 408 全量池（主题包里常有池外卡）。"""
    names = {}
    base = os.path.join(DEV, "backup", "_pool_backup", "408-full-2386.cdb")
    for p in (base, CDB):
        if not os.path.exists(p):
            continue
        con = sqlite3.connect(p)
        try:
            names.update(dict(con.execute("select id, name from texts")))
        finally:
            con.close()
    return names


def full_pool_ids():
    """408 全量池（2386 张）的卡号，用来判断主题包里的卡是不是 408 卡。"""
    p = os.path.join(DEV, "backup", "_pool_backup", "408-full-2386.cdb")
    if not os.path.exists(p):
        return None
    con = sqlite3.connect(p)
    try:
        return {r[0] for r in con.execute("select id from datas")}
    finally:
        con.close()


def load_limits():
    if not os.path.exists(LIMITS):
        return {}, {}, {}
    with io.open(LIMITS, encoding="utf-8") as f:
        d = json.load(f)
    return (set(int(x) for x in d.get("forbidden", [])),
            set(int(x) for x in d.get("limit1", [])),
            set(int(x) for x in d.get("limit2", [])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if not os.path.isdir(a.dir):
        raise SystemExit("[失败] 目录不存在：%s" % a.dir)
    files = sorted(f for f in os.listdir(a.dir) if f.lower().endswith(".ydk"))
    if not files:
        raise SystemExit("[失败] %s 里没有 .ydk" % a.dir)

    names = load_names()
    forbidden, limit1, limit2 = load_limits()
    full = full_pool_ids()

    print("=" * 66)
    print("主题包目录：%s（%d 个 ydk）" % (a.dir, len(files)))

    packs = []
    warnings = []
    for fn in files:
        pairs = parse_ydk(os.path.join(a.dir, fn))
        codes = [c for c, _ in pairs]
        title = os.path.splitext(fn)[0]
        if not codes:
            warnings.append("%s：没读到卡号，已跳过" % fn)
            continue
        if len(codes) > MAX_PACK:
            warnings.append("%s：%d 张，超过 %d 张，只取前 %d 张"
                            % (fn, len(codes), MAX_PACK, MAX_PACK))
            codes = codes[:MAX_PACK]
        if len(codes) < MIN_PACK:
            warnings.append("%s：只有 %d 张（少于 %d 张，combo 阶段会要求整组拿走）"
                            % (fn, len(codes), MIN_PACK))

        banned = [c for c in codes if c in forbidden]
        if banned:
            warnings.append("%s：含禁卡 %s（会被 CanPick 自动裁掉）"
                            % (fn, "、".join("%s(%d)" % (names.get(c, "?"), c) for c in banned)))

        outside = [c for c in codes if full is not None and c not in full]
        if outside:
            warnings.append("%s：%s 不在 408 全量卡池里（卡号写错？会 CreateToken 出空白卡）"
                            % (fn, "、".join(str(c) for c in outside)))

        # 同卡号在包内的重复数 vs 禁限额
        cnt = {}
        for c in codes:
            cnt[c] = cnt.get(c, 0) + 1
        for c, n in sorted(cnt.items()):
            cap = 1 if c in limit1 else 2 if c in limit2 else 3
            if n > cap:
                warnings.append("%s：%s x%d 超过额度 %d（COMBO_STRICT=false 会裁掉多余的）"
                                % (fn, names.get(c, c), n, cap))

        packs.append((title, codes))
        print("  %-26s %d 张  %s" % (title, len(codes),
                                     " ".join("%s(%d)" % (names.get(c, c), c) for c in codes)))

    if warnings:
        print("-" * 66)
        print("提示（%d 条，不影响生成）：" % len(warnings))
        for w in warnings:
            print("  " + w)

    lines = []
    for title, codes in packs:
        lines.append("\t-- %s（%d 张）" % (title, len(codes)))
        lines.append("\t{%s}," % ",".join(str(c) for c in codes))

    # 不要把本机 ydk 绝对路径写进生成物（会随项目公开）；改目录用 local-paths.json / --dir。
    body = ("-- 408 环境 2pick 主题卡包（由 make_combo.py 生成，勿手改）\n"
            "-- 每组 6 张卡号整组给出，整包放行；加包请改 ydk 目录后重跑生成器。\n"
            "combo_pack = {}\n\n"
            "combo_pack.pack = {\n%s\n}\n\nreturn combo_pack\n" % ("\n".join(lines),))

    print("-" * 66)
    if a.check:
        print("（--check 模式，未写文件）")
        return 0
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    print("已生成 %s（%d 组，%d 字节）" % (a.out, len(packs), len(body.encode("utf-8"))))

    # combo 非空时确保 pick.lua 的 ENABLE_COMBO 是 true
    pick = os.path.join(PICK_DIR, "pick.lua")
    if packs and os.path.exists(pick):
        s = io.open(pick, encoding="utf-8").read()
        s2 = s.replace("local ENABLE_COMBO     = false", "local ENABLE_COMBO     = true", 1)
        if s2 != s:
            io.open(pick, "w", encoding="utf-8", newline="\n").write(s2)
            print("已把 pick.lua 的 ENABLE_COMBO 改成 true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
