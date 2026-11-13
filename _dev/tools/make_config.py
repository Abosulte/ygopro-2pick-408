#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【历史入口，已由 config/generate-config.py 取代】

早期这个脚本从「本机 4088 服的 config.json」派生 4082 实例配置；那份模板含
真实环境配置和本机绝对路径，不适合进公开仓库。现在统一改为：

  * 仓库只放 config/config.json.example（脱敏模板）
  * 部署时由 config/generate-config.py 按 deploy.env / 环境变量生成 config.json
  * 端口、信任 IP、随机凭据都在部署期决定，任何环境都无需改仓库文件

保留本文件只是给旧笔记 / 旧 .bat 一个不至于报 ImportError 的入口：
    python3 _dev/tools/make_config.py            # 等价于 generate-config.py --print
    python3 _dev/tools/make_config.py --write    # 生成到 包根/config/config.json（本地调试用）
"""
import os
import subprocess
import sys

HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
GEN = os.path.join(HERE, "config", "generate-config.py")
TPL = os.path.join(HERE, "config", "config.json.example")


def main():
    if not os.path.isfile(GEN):
        raise SystemExit("[失败] 找不到 %s" % GEN)
    args = [sys.executable, GEN, "--template", TPL]
    if "--write" in sys.argv:
        args += ["--out", os.path.join(HERE, "config", "config.json")]
    else:
        args += ["--print"]
    raise SystemExit(subprocess.call(args))


if __name__ == "__main__":
    main()
