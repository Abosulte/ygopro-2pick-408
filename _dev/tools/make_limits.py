#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 2006.3.1 禁限表 + 408 卡池生成轮抽用的额度表。

输出：
  2pick/limits.lua   —— pick.lua 读取（禁卡不出现、限制 1 张、准限制 2 张）
  2pick/limits.json  —— add_combo_from_ypk.py 读取（导入主题包时校验）

用法：python3 make_limits.py [--lflist ...] [--pool ...] [--out-dir ...]
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
# 自备输入：2006.3.1 禁限表，可用 --lflist 指定（不随项目分发）
DEFAULT_LFLIST = local_path("LFLIST_CONF", os.path.join(DEV, "data", "lflist.conf"))
DEFAULT_POOL = os.path.join(HERE, "2pick", "408.cdb")
DEFAULT_OUT = os.path.join(HERE, "2pick")


def parse_lflist(path):
    """返回 {'forbidden': set, 'limit': set, 'semi': set}。"""
    out = {"forbidden": set(), "limit": set(), "semi": set()}
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
            m = re.match(r"^(\d+)\s+(\d+)", line)
            if m and section:
                out[section].add(int(m.group(1)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lflist", default=DEFAULT_LFLIST)
    ap.add_argument("--pool", default=DEFAULT_POOL)
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    a = ap.parse_args()

    conn = sqlite3.connect(a.pool)
    pool = {i for i, in conn.execute("SELECT id FROM datas")}
    alias_of = {i: al for i, al in conn.execute("SELECT id, alias FROM datas WHERE alias != 0")}
    names = dict(conn.execute("SELECT id, name FROM texts"))
    conn.close()

    lf = parse_lflist(a.lflist)
    # 额度严格按卡号独立计算，不做 alias 归一化：
    # 鹰身女郎1/2/3 是 3 张不同的卡，每张各自最多 3 张。
    # alias_of 只作为信息保留在输出里，轮抽逻辑不读取它。
    result = {}
    for key in ("forbidden", "limit", "semi"):
        raw = lf[key]
        in_pool = sorted(c for c in raw if c in pool)
        result[key] = in_pool
        print("[limits] %-9s 表内 %5d 条，卡池内 %3d 条" % (key, len(raw), len(in_pool)))

    os.makedirs(a.out_dir, exist_ok=True)
    lua_path = os.path.join(a.out_dir, "limits.lua")
    with open(lua_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("-- 由 make_limits.py 生成：2006.3.1 禁限表（只保留 408 卡池内卡号）\n")
        f.write("-- forbidden=禁止出现 / limit1=最多 1 张 / limit2=最多 2 张 / 其余最多 3 张\n")
        f.write("-- 额度按卡号独立；alias_of 仅供参考，不参与计数\n")
        f.write("local L = {}\n")
        for lua_key, key in (("forbidden", "forbidden"), ("limit1", "limit"), ("limit2", "semi")):
            f.write("L.%s = {\n\t" % lua_key)
            f.write(",".join("[%d]=true" % c for c in result[key]))
            f.write("\n}\n")
        f.write("L.alias_of = {\n\t")
        f.write(",".join("[%d]=%d" % (k, v) for k, v in sorted(alias_of.items())))
        f.write("\n}\nreturn L\n")
    print("[limits] 输出 %s" % lua_path)

    json_path = os.path.join(a.out_dir, "limits.json")
    with open(json_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({
            "forbidden": result["forbidden"],
            "limit1": result["limit"],
            "limit2": result["semi"],
            "alias_of": alias_of,
            "names": {str(k): names.get(k, "") for k in
                      set(result["forbidden"]) | set(result["limit"]) | set(result["semi"])},
        }, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("[limits] 输出 %s" % json_path)


if __name__ == "__main__":
    main()
