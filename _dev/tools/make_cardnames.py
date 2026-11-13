#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 2pick/cardnames.txt（卡号 + 卡名，UTF-8 纯文本）

给谁用：内核补丁 patch_pick_chat.py 的「选卡私聊」（Duel.PickLog）。
  服务端要把「【轮抽】本轮选卡：青眼白龙、…」发给玩家，就得自己查卡名。
  ygopro 的 strings.conf 里只有 !system / !victory / !counter / !setname，
  没有卡名（卡名在 cards.cdb 的 texts 表里）；服务端又不方便查 SQLite，
  所以本地把卡名导成一行一条的文本，随 push-rules.sh 一起拷过去。

格式（一行一条，空格分隔；卡名允许含空格，服务端取第一个空格之后的全部内容）：
  89631139 青眼白龙

卡名来源（按顺序，先到先得，408.cdb 优先）：
  2pick/408.cdb                     —— 当前卡池（674 张）
  2pick/_pool_backup/*.cdb          —— 历次全量池备份（2386 张）
  这样 combo.lua 里引用、但被「待删除.ydk」剔出当前卡池的卡（例如扰乱三兄弟、
  鹰身女郎 2/3）也仍然有中文名，不会在私聊里退化成 #卡号。

卡池变动后重跑一次（一键更新全部.bat 已经串进去了）。
"""
import glob
import io
import os
import sqlite3
import sys

# 脚本位于 _dev/tools/，包根在两级之上；所有数据路径都以包根为基准
HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEV = os.path.join(HERE, "_dev")
CDB = os.path.join(HERE, "2pick", "408.cdb")
OUT = os.path.join(HERE, "2pick", "cardnames.txt")


def collect(path, names):
    if not os.path.isfile(path):
        return 0
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("?", "%3f"), uri=True)
    except Exception:
        return 0
    n = 0
    try:
        for code, name in con.execute(
                "select id, name from texts where name is not null and name <> ''"):
            name = " ".join(str(name).split())   # 去掉换行 / 多余空格
            if not name:
                continue
            code = int(code)
            if code not in names:                # 408.cdb 的名字优先
                names[code] = name
                n += 1
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return n


def main():
    if not os.path.isfile(CDB):
        raise SystemExit("找不到 %s" % CDB)
    names = {}
    total = collect(CDB, names)
    print("[cardnames] 408.cdb         新增 %4d 条" % total)
    for path in sorted(glob.glob(os.path.join(DEV, "backup", "_pool_backup", "*.cdb"))):
        n = collect(path, names)
        if n:
            print("[cardnames] %-20s 新增 %4d 条" % (os.path.basename(path), n))
    lines = ["%d %s" % (c, names[c]) for c in sorted(names)]
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("[cardnames] 合计 %d 条 -> %s（%d 字节）"
          % (len(lines), OUT, os.path.getsize(OUT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
