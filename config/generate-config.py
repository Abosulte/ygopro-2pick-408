#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署期生成 srvpro 的 config.json（仓库只放脱敏模板 config.json.example）。

端口 / 欢迎语 / 客户端版本取自 deploy.env 或环境变量；9 个 accesskey / password
每次随机生成；轮抽服必需的固定项强制写入，不随模板漂移。
trusted_proxies 只含本机回环，本脚本不改动。
用法：python3 config/generate-config.py --out <路径> [--force] [--print]
环境变量：PORT_4082(4082) / CLIENT_VERSION(4962) / WELCOME(可选)
"""
import argparse
import json
import os
import secrets
import sys

PLACEHOLDER = "__GENERATE__"
BASE_PORT = 4082  # 模板里的基准主端口，用于整体平移内部端口

# 模板里所有内部端口；换主端口时按 delta 一起平移，避免与别的实例撞端口
INTERNAL_PORT_PATHS = [
    ("modules", "http", "port"),
    ("modules", "http", "ssl", "port"),
    ("modules", "tournament_mode", "port"),
    ("modules", "neos", "port"),
    ("modules", "webhook", "port"),
    ("modules", "pre_util", "port"),
    ("modules", "update_util", "port"),
    ("modules", "windbot", "port"),
]


def env(name, default=""):
    return str(os.environ.get(name, default)).strip()


def walk_replace(node, on_leaf):
    """递归遍历，把模板里的 __GENERATE__ 换成随机串。"""
    if isinstance(node, dict):
        return {k: walk_replace(v, on_leaf) for k, v in node.items()}
    if isinstance(node, list):
        return [walk_replace(v, on_leaf) for v in node]
    return on_leaf(node)


def get_path(obj, path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def set_path(obj, path, value):
    cur = obj
    for key in path[:-1]:
        cur = cur.setdefault(key, {})
    cur[path[-1]] = value


def build(template_path):
    with open(template_path, encoding="utf-8") as f:
        cfg = json.load(f)

    # --- 端口 -------------------------------------------------------------
    try:
        port = int(env("PORT_4082", cfg.get("port", BASE_PORT)))
    except ValueError:
        sys.exit("[失败] PORT_4082 不是整数：%s" % env("PORT_4082"))
    if not (1 <= port <= 65535):
        sys.exit("[失败] PORT_4082 超出 1~65535：%s" % port)
    delta = port - BASE_PORT
    cfg["port"] = port
    if delta:
        for path in INTERNAL_PORT_PATHS:
            old = get_path(cfg, path)
            # 模板没有的内部端口不要凭空造出来
            if isinstance(old, int):
                set_path(cfg, path, old + delta)

    # --- 客户端版本 / 欢迎语 ----------------------------------------------
    try:
        cfg["version"] = int(env("CLIENT_VERSION", cfg.get("version", 4962)))
    except ValueError:
        sys.exit("[失败] CLIENT_VERSION 不是整数：%s" % env("CLIENT_VERSION"))
    # 欢迎语与端口解耦：模板与 deploy.env 都不带端口号，换端口时无需同步改文案。
    welcome = env("WELCOME")
    if welcome:
        cfg["modules"]["welcome"] = welcome

    # --- 2pick 服必需的固定项（防止模板被改坏）----------------------------
    cfg.setdefault("hostinfo", {})["no_check_deck"] = True
    cfg.setdefault("modules", {})["replay_delay"] = False

    # --- 随机凭据 ---------------------------------------------------------
    n_gen = [0]

    def on_leaf(v):
        if v == PLACEHOLDER:
            n_gen[0] += 1
            return secrets.token_hex(16)
        return v

    cfg = walk_replace(cfg, on_leaf)

    return cfg, {
        "port": port,
        "delta": delta,
        "version": cfg["version"],
        "generated": n_gen[0],
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="部署期生成 srvpro config.json（脱敏）")
    ap.add_argument("--template", default=os.path.join(here, "config.json.example"))
    ap.add_argument("--out", default=os.path.join(here, "config.json"))
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的 config.json")
    ap.add_argument("--print", dest="print_only", action="store_true", help="只打印摘要")
    args = ap.parse_args()

    if not os.path.isfile(args.template):
        sys.exit("[失败] 找不到模板：%s" % args.template)
    cfg, info = build(args.template)

    print("  主端口        : %d（内部端口平移 %+d）" % (info["port"], info["delta"]))
    print("  客户端版本    : %s" % info["version"])
    print("  随机凭据      : %d 个（每次部署都不同，不写日志）" % info["generated"])

    if args.print_only:
        return
    if os.path.exists(args.out) and not args.force:
        print("  [跳过] 已存在且未指定 --force：%s" % args.out)
        return
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(args.out, 0o600)
    print("  [OK] 已生成 %s（权限 600，不进版本库）" % args.out)


if __name__ == "__main__":
    main()
