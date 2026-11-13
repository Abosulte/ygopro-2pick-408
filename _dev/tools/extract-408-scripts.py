#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 MDPro3 的 TTT-MIX.ypk 里提取「408 老效果脚本」，生成部署包的 expansions/ 覆写层。

原理（data_manager.cpp:521 ScriptReaderEx，YGOPRO_SERVER_MODE）：
    请求 ./script/c12345.lua
      -> ./specials/<...>              （本服不用）
      -> ./expansions/script/c12345.lua  ← 本脚本生成的就是这一层，always read expansions first
      -> ./script/c12345.lua          （官方子模块的现代脚本）
所以 ypk 里的 script/ 目录要整包放到运行目录的 expansions/ 下，形成 expansions/script/。
special.lua 也放同一个目录，由 port_2pick.py 生成、deploy.sh 同步。

【只保留「和官方现代脚本不同」的脚本】
ypk 里共 540 个 c<卡号>.lua，逐字节对照官方 script 子模块
（mycard/ygopro-scripts@b83db1cc，deploy.sh 会 clone 到 $BASE/src/script）后：
  - 468 个规范化后完全一致（仅官方 CRLF / ypk LF 的换行差异），放进去毫无作用；
  - 40 个只差官方新增的 c<卡号>.mentioned_counter={...} 元数据表 —— 它不注册 Effect、
    不参与结算（Auxiliary.HasMentionedCounter 全库仅 1 张卡用，408 池 0 张调用），
    效果代码逐行相同，也不算老效果；
  - 32 个效果逻辑真的不同 —— 这才是需要覆写的 408 环境老脚本。
过滤结果固化在 _dev/data/408-old-scripts.txt（32 个卡号），默认按它提取，
这样重跑生成器不会把 540 个又铺回来，且不依赖本机是否有官方 script 树。

ypk 里的 6 个 cdb、lflist.conf、pics/ 一概不取：
  - 卡数据用官方 mycard/ygopro 仓库根的 cards.cdb（408 卡号全都在里面）
  - lflist.conf 用官方仓库根；轮抽禁限走 2pick/limits.lua，引擎 lflist 不参与
  - pics/ 是客户端卡图，服务器不需要

用法：
    python3 _dev/tools/extract-408-scripts.py                       # 默认：按清单提取 32 个
    python3 _dev/tools/extract-408-scripts.py --ypk <TTT-MIX.ypk 路径>
    python3 _dev/tools/extract-408-scripts.py --clean               # 先清空输出目录里的 c*.lua
    python3 _dev/tools/extract-408-scripts.py --all                 # 诊断：提取全部 540 个
    python3 _dev/tools/extract-408-scripts.py --only-pool           # 只留 408.cdb 池内卡（16 张）
    python3 _dev/tools/extract-408-scripts.py --refresh-list \
        --official-script <官方 script 目录>                        # 重新比对并刷新清单
"""
import argparse
import io
import os
import re
import shutil
import sqlite3
import sys
import zipfile
from _localenv import local_path

HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEV = os.path.join(HERE, "_dev")

DEFAULT_YPK_CANDIDATES = [
    local_path("TTT_MIX_YPK"),
    os.path.join(HERE, "TTT-MIX.ypk"),
    os.path.join(DEV, "data", "TTT-MIX.ypk"),
]

# 「与官方现代脚本不同」的卡号清单，由 --refresh-list 生成，随仓库分发。
DIFF_LIST = os.path.join(DEV, "data", "408-old-scripts.txt")

CARD_SCRIPT = re.compile(r"^script/c(\d+)\.lua$")

LIST_HEADER = """# 408 老效果脚本清单：与官方 mycard/ygopro-scripts@b83db1cc 效果代码不同的卡号。
# 由 extract-408-scripts.py --refresh-list 生成。判定顺序：
#   ① 规范化（去 BOM / CRLF->LF / 去行尾空白 / 去首尾空行）后相同 -> 不覆写；
#   ② 去掉官方独有的 c<卡号>.mentioned_counter={...} 元数据表后相同 -> 不覆写
#      （该表只供卡查辅助函数读取，不注册 Effect、不参与结算）；
#   ③ 其余才是真正需要覆写的老效果脚本。
# 默认提取只按此清单；--all 可提取 ypk 里全部脚本。
# 官方 script 树：deploy.sh clone 的 $BASE/src/script（本地比对用 _compare/ygopro-virgin/script）。
"""


def find_ypk():
    for p in DEFAULT_YPK_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def normalize(data):
    """比较用的规范化：消除换行/空白层面的非实质差异。"""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = [ln.rstrip(b" \t") for ln in data.split(b"\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return b"\n".join(lines)


# 现代 ygopro-scripts 给部分卡加的「元数据声明」：c<卡号>.mentioned_counter={ [0x100e]=true, }
# 只供 Auxiliary.HasMentionedCounter 这类卡查/检索辅助函数读取，不注册任何 Effect，
# 不参与效果结算。老脚本没有这张表，但效果代码逐行相同 —— 不应因此判成「老效果覆写」。
METADATA_BLOCK = re.compile(rb"c\d+\.mentioned_counter\s*=\s*\{[^}]*\}")


def strip_metadata(data):
    d = METADATA_BLOCK.sub(b"", data)
    return b"\n".join(ln for ln in d.split(b"\n") if ln.strip())


def is_effectively_same(old_bytes, official_bytes):
    a, b = normalize(old_bytes), normalize(official_bytes)
    if a == b:
        return True
    return strip_metadata(a) == strip_metadata(b)


def load_diff_list():
    """读卡号清单；不存在返回 None（此时退回全量提取并给出提示）。"""
    if not os.path.isfile(DIFF_LIST):
        return None
    codes = set()
    with io.open(DIFF_LIST, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                codes.add(int(ln))
    return codes


def refresh_diff_list(ypk, official):
    """用官方 script 树逐字节比对 ypk 里的全部卡脚本，重写清单并返回统计。"""
    if not os.path.isdir(official):
        raise SystemExit("❌ 官方 script 目录不存在：%s" % official)
    z = zipfile.ZipFile(ypk)
    entries = [(n, m) for n, m in
               ((n, CARD_SCRIPT.match(n)) for n in z.namelist()) if m]
    same, different, missing = [], [], []
    for name, m in entries:
        code = int(m.group(1))
        np = os.path.join(official, os.path.basename(name))
        if not os.path.isfile(np):
            missing.append(code)
            continue
        a = z.read(name)
        b = open(np, "rb").read()
        (same if is_effectively_same(a, b) else different).append(code)
    # 官方没有同名的也保留（否则该卡在 expansions 层就完全没脚本了）
    keep = sorted(set(different) | set(missing))
    with io.open(DIFF_LIST, "w", encoding="utf-8", newline="\n") as f:
        f.write(LIST_HEADER)
        for c in keep:
            f.write("%d\n" % c)
    print("清单已更新：%s" % DIFF_LIST)
    print("  规范化后相同（不覆写）: %d" % len(same))
    print("  内容不同（覆写）      : %d" % len(different))
    print("  官方无同名（覆写）    : %d" % len(missing))
    print("  最终保留              : %d" % len(keep))
    return set(keep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ypk", default=None, help="TTT-MIX.ypk 路径（不传就自动找）")
    ap.add_argument("--out", default=os.path.join(HERE, "expansions"),
                    help="输出目录，默认 <包>/expansions/")
    ap.add_argument("--only-pool", action="store_true",
                    help="只提取 2pick/408.cdb 池内的卡脚本（与清单取交集，16 张）")
    ap.add_argument("--all", action="store_true",
                    help="忽略清单，提取 ypk 里全部 c<卡号>.lua（诊断用，540 个）")
    ap.add_argument("--clean", action="store_true", help="先清空输出目录里的 c*.lua")
    ap.add_argument("--refresh-list", action="store_true",
                    help="用 --official-script 重新比对并刷新 _dev/data/408-old-scripts.txt")
    ap.add_argument("--official-script", default=None,
                    help="官方 script 子模块目录（--refresh-list 时用，如 $BASE/src/script）")
    a = ap.parse_args()

    ypk = a.ypk or find_ypk()
    if not ypk or not os.path.isfile(ypk):
        raise SystemExit("找不到 TTT-MIX.ypk，请用 --ypk 指定路径。\n候选：\n  " +
                         "\n  ".join(DEFAULT_YPK_CANDIDATES))

    if a.refresh_list:
        if not a.official_script:
            raise SystemExit("❌ --refresh-list 需要 --official-script <官方 script 目录>")
        refresh_diff_list(ypk, a.official_script)
        if not (a.clean or a.all or a.only_pool):
            return

    z = zipfile.ZipFile(ypk)
    entries = [(n, m) for n, m in
               ((n, CARD_SCRIPT.match(n)) for n in z.namelist()) if m]
    if not entries:
        raise SystemExit("ypk 里没有 script/c<卡号>.lua，格式不对？")

    # 过滤优先级：--all 全量 > 清单 ∩ 池内 > 清单
    keep = None
    mode = "全部（--all）"
    if not a.all:
        keep = load_diff_list()
        if keep is None:
            print("  ⚠ 没有清单 %s，退回全量提取；" % DIFF_LIST)
            print("     建议先跑 --refresh-list --official-script <官方 script 目录>")
            mode = "全部（无清单）"
        else:
            mode = "清单（与官方不同，%d 个）" % len(keep)
        if a.only_pool:
            pool = os.path.join(HERE, "2pick", "408.cdb")
            con = sqlite3.connect(pool)
            pool_ids = {r[0] for r in con.execute("select id from datas")}
            con.close()
            keep = pool_ids if keep is None else (keep & pool_ids)
            mode += " ∩ 408 卡池"

    outdir = os.path.join(a.out, "script")
    os.makedirs(outdir, exist_ok=True)
    if a.clean:
        for fn in os.listdir(outdir):
            if re.match(r"c\d+\.lua$", fn):
                os.remove(os.path.join(outdir, fn))

    total, written, skipped = 0, 0, 0
    for name, m in entries:
        code = int(m.group(1))
        if keep is not None and code not in keep:
            skipped += 1
            continue
        data = z.read(name)
        with open(os.path.join(outdir, os.path.basename(name)), "wb") as f:
            f.write(data)
        total += len(data)
        written += 1

    # 安全校验：不能把 special.lua / 公共脚本带进来
    extra = [n for n in z.namelist()
             if n.startswith("script/") and n.endswith(".lua")
             and not CARD_SCRIPT.match(n)]
    print("ypk            : %s (%.1f MB)" % (ypk, os.path.getsize(ypk) / 1024 / 1024))
    print("过滤方式        : %s" % mode)
    print("写入脚本        : %d 个 / %.1f KB -> %s（跳过 %d 个）" %
          (written, total / 1024, outdir, skipped))
    if a.clean:
        print("当前目录文件数  : %d" %
              sum(1 for f in os.listdir(outdir) if re.match(r"c\d+\.lua$", f)))
    print("ypk 里的其它脚本: %s" % (extra[:10] if extra else "无（全是 c<卡号>.lua）"))
    print("未取内容        : 6 个 cdb / lflist.conf / %d 张 pics（均不需要）" %
          sum(1 for n in z.namelist() if n.startswith("pics/") and n.endswith(".jpg")))

    # 与轮抽卡池对照
    pool = os.path.join(HERE, "2pick", "408.cdb")
    if os.path.isfile(pool):
        con = sqlite3.connect(pool)
        ids = {r[0] for r in con.execute("select id from datas")}
        con.close()
        codes = {int(re.search(r"c(\d+)\.lua$", f).group(1))
                 for f in os.listdir(outdir) if re.match(r"c\d+\.lua$", f)}
        print("408 卡池        : %d 张，其中被老脚本覆盖 %d 张" % (len(ids), len(ids & codes)))

    print("\n下一步：把整个 2pick-408/ 上传，deploy.sh 会自动把 expansions/ 铺到运行目录；")
    print("      special.lua 由 port_2pick.py 生成后同步到同一个 expansions/script/ 下。")


if __name__ == "__main__":
    main()
