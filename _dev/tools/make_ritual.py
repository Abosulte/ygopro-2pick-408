#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 2pick/ritual.lua：仪式怪兽 <-> 仪式魔法 配对表。

为什么需要：
    轮抽时仪式怪兽单张没用，必须有对应仪式魔法才能出场。pick.lua 靠这张表，
    只要有仪式怪兽进候选，就同时把它的仪式魔法塞进同一批候选（反之亦然）。

数据来源：
    408.cdb 的卡文（texts.desc）里的「卡名」引用，不需要联网也不需要卡包脚本。
      * 仪式怪兽——「混沌的仪式」降临。            -> 引用的是仪式魔法
      * 仪式魔法——「混沌战士」的降临必需 / ...仪式召唤 -> 引用的是仪式怪兽
    只认「当前卡池里真实存在的」仪式怪兽/仪式魔法，其它引用（如效果里提到的
    青眼白龙）自动忽略。

通用仪式魔法：
    与奈落的契约、与暗之支配者的契约（点名怪兽）之外，像「与奈落的契约」这种
    按属性/等级召唤、文字里不点名怪兽的，会被判定为 generic，不参与配对，
    单独出现在普通池里就行。

用法：
    python make_ritual.py            # 生成 2pick/ritual.lua 并打印校验报告
    python make_ritual.py --check    # 只报告，不写文件
    python make_ritual.py --cdb x.cdb --out y.lua
"""
import argparse
import io
import os
import re
import sqlite3
import sys

# 脚本位于 _dev/tools/，包根在两级之上；所有数据路径都以包根为基准
HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEV = os.path.join(HERE, "_dev")
PICK_DIR = os.path.join(HERE, "2pick")
CDB = os.path.join(PICK_DIR, "408.cdb")
OUT = os.path.join(PICK_DIR, "ritual.lua")

TYPE_MONSTER = 0x1
TYPE_SPELL = 0x2
TYPE_RITUAL = 0x80

QUOTE = re.compile(u"「([^」]+)」")


def load(cdb):
    con = sqlite3.connect(cdb)
    try:
        rows = {r[0]: r[1:] for r in con.execute(
            "select d.id, t.name, d.type, t.desc from datas d join texts t on t.id = d.id")}
    finally:
        con.close()
    names = {i: v[0] for i, v in rows.items()}
    return rows, names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdb", default=CDB)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--check", action="store_true", help="只报告，不写文件")
    a = ap.parse_args()

    if not os.path.exists(a.cdb):
        raise SystemExit("[失败] 找不到 %s" % a.cdb)
    rows, names = load(a.cdb)
    by_name = {}
    for i, n in names.items():
        by_name.setdefault(n, []).append(i)

    monsters = sorted(i for i, (n, t, d) in rows.items()
                      if (t & TYPE_RITUAL) and (t & TYPE_MONSTER))
    spells = sorted(i for i, (n, t, d) in rows.items()
                    if (t & TYPE_RITUAL) and (t & TYPE_SPELL))

    print("=" * 66)
    print("卡池：%s（%d 张）" % (os.path.basename(a.cdb), len(rows)))
    print("仪式怪兽 %d 张，仪式魔法 %d 张" % (len(monsters), len(spells)))

    m2s = {m: set() for m in monsters}
    s2m = {s: set() for s in spells}
    ms, ss = set(monsters), set(spells)
    for i, (n, t, d) in rows.items():
        if i not in ms and i not in ss:
            continue
        for quoted in QUOTE.findall(d or ""):
            for j in by_name.get(quoted, []):
                if i in ms and j in ss:
                    m2s[i].add(j)
                elif i in ss and j in ms:
                    s2m[i].add(j)

    # 双向补齐：文字只写了一边的，另一边也补上，保证「一起出现」是对称的
    for m in list(m2s):
        for s in list(m2s[m]):
            s2m[s].add(m)
    for s in list(s2m):
        for m in list(s2m[s]):
            m2s[m].add(s)

    generic = [s for s in spells if not s2m[s]]
    bad_monster = [m for m in monsters if not m2s[m]]

    print("-" * 66)
    print("配对成功 %d 对：" % sum(len(v) for v in m2s.values()))
    for m in monsters:
        if m2s[m]:
            print("  %-22s %-10d <- %s" % (
                names[m], m, "、".join("%s(%d)" % (names[s], s) for s in sorted(m2s[m]))))
    if generic:
        print("-" * 66)
        print("通用仪式魔法 %d 张（不点名怪兽，不配对，单独出现）：" % len(generic))
        for s in generic:
            print("  %-22s %-10d %s" % (names[s], s, (rows[s][2] or "").split("\n")[0][:40]))

    rc = 0
    if bad_monster:
        rc = 1
        print("-" * 66)
        print("[问题] %d 张仪式怪兽在当前卡池里找不到对应仪式魔法，抽到就是废卡：" % len(bad_monster))
        for m in bad_monster:
            print("  %-22s %d" % (names[m], m))

    # ---- 写 ritual.lua ----
    lines = []
    lines.append("\tmonster_to_spell = {")
    for m in monsters:
        if not m2s[m]:
            continue
        body = ",".join(str(s) for s in sorted(m2s[m]))
        note = "、".join(names[s] for s in sorted(m2s[m]))
        lines.append("\t\t[%d]={%s},\t-- %s -> %s" % (m, body, names[m], note))
    lines.append("\t},")
    lines.append("\tspell_to_monster = {")
    for s in spells:
        if not s2m[s]:
            continue
        body = ",".join(str(m) for m in sorted(s2m[s]))
        note = "、".join(names[m] for m in sorted(s2m[s]))
        lines.append("\t\t[%d]={%s},\t-- %s -> %s" % (s, body, names[s], note))
    lines.append("\t},")
    lines.append("\tgeneric_spell = {")
    for s in generic:
        lines.append("\t\t[%d]=true,\t-- %s" % (s, names[s]))
    lines.append("\t},")

    body = ("-- 由 make_ritual.py 生成，不要手改这个文件\n"
            "-- 仪式怪兽 <-> 仪式魔法 配对表（来自 408.cdb 卡文里的「卡名」引用）\n"
            "-- pick.lua 用它保证：仪式怪兽进候选时，对应仪式魔法一起进候选（反之亦然）\n"
            "return {\n" + "\n".join(lines) + "\n}\n")

    print("-" * 66)
    if a.check:
        print("（--check 模式，未写文件）")
    else:
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        print("已生成 %s（%d 字节）" % (a.out, len(body.encode("utf-8"))))
        print("改动后跑 更新-卡池.bat / push-rules.sh 上传即可（不用重编）。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
