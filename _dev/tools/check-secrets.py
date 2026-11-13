#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开源前脱敏自检：扫描「将被 git 提交的文件」，发现真实公网 IP、开发机绝对路径、
被误加入的私有配置文件就报错退出（退出码 1）。

    python3 _dev/tools/check-secrets.py           # git 仓库里：只查会被提交的文件
    python3 _dev/tools/check-secrets.py --all    # 不依赖 git，遍历整个包（按排除表）

设计原则：仓库里**根本不存在** config.json / deploy.env / local-paths.json，
所以「忘了脱敏」这件事不会发生；本脚本只是最后一道保险。
"""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

# 遍历模式下跳过的目录（与 .gitignore 对齐）
SKIP_DIRS = {".git", "__pycache__", "_dev/backup", "node_modules", ".idea", ".vscode"}
# 私有文件：一旦出现在待提交列表里就是错误
PRIVATE_FILES = {
    "deploy.env",
    "config/config.json",
    "_dev/tools/local-paths.json",
    ".deploy-env",
}
# 二进制 / 大资源，不该进 git
BAD_SUFFIX = (".ypk", ".tar.gz", ".tgz", ".pyc", ".pyo", ".log", ".bak")

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
WINPATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"']*")
# C++ 源码字符串里的 "protected:\n" 之类会误命 WINPATH_RE 的变体，这里只找盘符+反斜杠
WINPATH_STRICT = re.compile(r"\b[A-Za-z]:\\")
CRED_KEYS = ("accesskey", "api_key", "auth_key", "password", "passwd", "secret", "token")

TEXT_EXT = {".sh", ".py", ".lua", ".json", ".txt", ".conf", ".md", ".env",
            ".example", ".bat", ".yml", ".yaml", ".js", ".cpp", ".h", ".c"}


def tracked_files():
    """优先用 git 的候选列表；不是仓库时按排除表遍历。"""
    try:
        out = subprocess.check_output(
            ["git", "-C", ROOT, "ls-files", "--cached", "--others", "--exclude-standard"],
            stderr=subprocess.DEVNULL, text=True)
        return sorted({x.strip().replace("\\", "/") for x in out.splitlines() if x.strip()}), "git"
    except Exception:
        pass
    result = []
    for base, dirs, files in os.walk(ROOT):
        rel_base = os.path.relpath(base, ROOT).replace("\\", "/")
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS
                   and (rel_base + "/" + d).lstrip("./") not in SKIP_DIRS]
        if rel_base.startswith("_dev/backup") or rel_base == "__pycache__":
            continue
        for name in files:
            rel = os.path.relpath(os.path.join(base, name), ROOT).replace("\\", "/")
            if rel.lstrip("./") in PRIVATE_FILES or rel.endswith(BAD_SUFFIX):
                continue
            if name == "local-paths.json":
                continue
            result.append(rel)
    return sorted(result), "walk"


def public_ips(line):
    hits = []
    for m in IP_RE.finditer(line):
        raw = m.group(0)
        try:
            ip = ipaddress.IPv4Address(raw)
        except ValueError:
            continue
        # is_global 会排除 RFC1918 私有段、回环、链路本地、组播、
        # 以及 203.0.113.0/24 等 RFC5737 文档示例网段
        if ip.is_global:
            hits.append(raw)
    return hits


def check_template_example(rel, text, issues):
    """模板里的凭据必须保持占位符，不能被回填成真实值。"""
    if rel != "config/config.json.example":
        return
    try:
        d = json.loads(text)
    except Exception as e:
        issues.append((rel, 0, "模板不是合法 JSON：%s" % e))
        return
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                p = path + "." + k if path else k
                if any(key in k.lower() for key in CRED_KEYS) and isinstance(v, str):
                    if v != "__GENERATE__":
                        issues.append((rel, 0, "模板凭据字段 %s 不是占位符：%r" % (p, v)))
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))
    walk(d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="强制遍历模式（忽略 git 状态）")
    args = ap.parse_args()

    if args.all:
        files, mode = [], "walk"
        for base, dirs, fs in os.walk(ROOT):
            rel_base = os.path.relpath(base, ROOT).replace("\\", "/")
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and
                       not (rel_base + "/" + d).lstrip("./").startswith("_dev/backup")]
            if rel_base.startswith("_dev/backup"):
                continue
            for f in fs:
                rel = os.path.relpath(os.path.join(base, f), ROOT).replace("\\", "/")
                if rel.lstrip("./") in PRIVATE_FILES or rel.endswith(BAD_SUFFIX) or f == "local-paths.json":
                    continue
                files.append(rel)
        files = sorted(files)
    else:
        files, mode = tracked_files()

    issues = []
    for rel in files:
        rel = rel.lstrip("./")
        if rel in PRIVATE_FILES:
            issues.append((rel, 0, "私有配置文件不应被提交（检查 .gitignore）"))
            continue
        if rel.endswith(BAD_SUFFIX):
            issues.append((rel, 0, "二进制/临时文件不应被提交"))
            continue
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path) or os.path.getsize(path) > 2 * 1024 * 1024:
            continue
        if os.path.splitext(rel)[1].lower() not in TEXT_EXT and not rel.endswith(".example"):
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for ip in public_ips(line):
                issues.append((rel, i, "发现公网 IPv4 %s：%s" % (ip, line.strip()[:100])))
            if WINPATH_STRICT.search(line):
                issues.append((rel, i, "发现开发机盘符绝对路径：%s" % line.strip()[:100]))
        check_template_example(rel, text, issues)

    print("脱敏自检：模式=%s，检查 %d 个文件" % (mode, len(files)))
    if issues:
        print("❌ 发现 %d 处需要处理的敏感内容：" % len(issues))
        for rel, line, msg in issues:
            print("  %s%s  %s" % (rel, (":%d" % line) if line else "", msg))
        return 1
    print("✅ 通过：无真实公网 IP、无开发机路径、无私有配置、模板凭据均为占位符")
    return 0


if __name__ == "__main__":
    sys.exit(main())
