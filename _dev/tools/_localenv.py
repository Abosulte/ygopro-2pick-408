#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地生成器专用的「本机路径」解析。

仓库里**不放**任何开发机盘符绝对路径（会暴露用户名与目录结构）。
需要本机资源的生成器统一走这里，取值优先级：

    1. 环境变量（例如 YDK_DECK_DIR、TTT_MIX_YPK）
    2. _dev/tools/local-paths.json   <- 本机私有，已被 .gitignore 排除
    3. fallback（一般是空串，由调用方给出友好报错）

使用者：复制 local-paths.example.json -> local-paths.json，按自己机器填。
部署服上完全用不到这些生成器，deploy.sh 不依赖本模块。
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = os.path.join(_HERE, "local-paths.json")
_CACHE = None


def local_path(key, fallback=""):
    global _CACHE
    v = os.environ.get(key, "").strip()
    if v:
        return v
    if _CACHE is None:
        _CACHE = {}
        if os.path.isfile(_CFG):
            with open(_CFG, encoding="utf-8") as f:
                _CACHE = json.load(f)
    v = str(_CACHE.get(key, "") or "").strip()
    return v if v else fallback


def require(key, what):
    """取路径并在缺失时给出可操作的提示，而不是抛一堆 FileNotFoundError。"""
    v = local_path(key)
    if not v:
        raise SystemExit(
            "[失败] 未配置 %s（%s）。\n"
            "  方式一：cp _dev/tools/local-paths.example.json _dev/tools/local-paths.json 后填写\n"
            "  方式二：设置环境变量 %s=<路径>\n"
            "  注：这些只影响本地生成器，服务器部署 deploy.sh 完全不需要。" % (key, what, key))
    return v
