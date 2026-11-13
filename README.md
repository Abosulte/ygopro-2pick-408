# 408 环境 · 2pick 轮抽服

玩家建房后现场轮抽组卡、再进入决斗的独立服务端。本项目不是任何上游仓库的副本，而是一套「补丁生成器 + 部署编排 + 规则数据」：部署时按固定版本拉取开源项目、打内核补丁并编译，再铺上 408 环境的规则数据，并启动一个独立的 srvpro 实例。

## 引用 / 参考的开源项目

全部按固定 commit 拉取，不跟随上游更新：

| 项目 | 版本 | 用途 | 许可证 |
|---|---|---|---|
| `mycard/srvpro` | `193ab4c` | 房间 / 大厅服务（Node.js） | AGPL-3.0 |
| `mycard/ygopro`（`server` 分支） | `176fc60c` | 服务端源码；仓库根自带 `cards.cdb` / `lflist.conf` / `strings.conf` | GPL-2.0 |
| └ 子模块 `mycard/ygopro-core` | `3209cd86` | 决斗内核 | MIT |
| └ 子模块 `mycard/ygopro-scripts` | `b83db1cc` | 13575 个现代卡片效果脚本 | GPL-2.0 |
| `mycard/ygopro-2pick` | 快照见 `_dev/data/_upstream/pick.lua` | 轮抽规则 `2pick/pick.lua` 的原始基线（参考，非 fork） | GPL-2.0 |
| `premake/premake-core` | `5.0.0` | 编译用 premake5 | BSD-3-Clause |

许可证全文见 `LICENSE`。

## 部署

### 环境

Linux x86_64；`git` `g++` `make` `python3` `curl`、Lua 5.3 / 5.4 开发头文件、Node.js ≥ 16 与 `npm` / `pm2`；能访问 github.com；主端口 4082 及内部端口空闲；408 环境客户端版本 4962。**无需自备卡库或脚本资源**。

依赖默认只检查不安装；root 下 `INSTALL_DEPS=1 bash deploy.sh` 可自动装系统依赖与 pm2，Node.js / npm 需自备。

### 步骤

```bash
git clone https://github.com/Abosulte/ygopro-2pick-408.git /root/2pick-408
cd /root/2pick-408
cp deploy.env.example deploy.env      # 可选：改端口 / 欢迎语 / 编译并行度；不填用默认值
CHECK_ONLY=1 bash deploy.sh           # 只读预检，不改任何东西
bash deploy.sh                        # 一键部署
```

`deploy.sh` 依次执行：停旧实例并归档 → clone srvpro + `npm ci` → 部署期生成 `config.json`（凭据随机、不进仓库）→ 铺 `2pick/` 规则与 `expansions/` 覆写层 → clone ygopro 及子模块 → `port_2pick.py` 打内核补丁并生成 `special.lua` → premake + make 编译 → badip 补丁 → `pm2 start` + 端口验收。

脚本只操作 `srvpro-2pick` 进程与 `$HOME/ygopro-2pick`，不影响其它已存在的服；不会自动改防火墙，需自行放行 TCP 4082。中途失败可直接重跑（fresh-only）。

### 验收

客户端连 4082 建房，完整走 ①额外融合 2 轮 → ②combo → ③主卡 4 轮 → ④combo → ⑤补卡 → ⑥整理 → ⑦删卡 → 开局抽 5 张。`pm2 logs srvpro-2pick --lines 120 --nostream` 中不应出现 `YGOPRO Debug:`。

只改 Lua / 卡池数据时 `bash push-rules.sh` 热更新（不重编）；改过 C++ 补丁或部署参数则重跑 `deploy.sh`。
