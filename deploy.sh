#!/usr/bin/env bash
# ============================================================
# 408 环境 2pick 轮抽服：全新部署（删除旧实例 -> 重建）。
# 用法：上传部署包后 bash <包目录>/deploy.sh
#
# 上游固定 commit：srvpro@193ab4c、ygopro@176fc60c（子模块由 gitlink 钉死）。
# 资源零自备：cards.cdb / lflist.conf / strings.conf / script/ 取自 ygopro 仓库根
#   与 script 子模块；408 老效果脚本取自部署包 expansions/（server 模式
#   ScriptReaderEx 优先读 expansions/script/）；special.lua 由 port_2pick.py
#   部署期生成，不进仓库。ypk 里的 cdb / lflist / pics 一概不用。
# 配置零敏感数据：config.json 由 config/generate-config.py 部署期生成，凭据随机。
# 隔离：只操作 pm2 进程 srvpro-2pick 与目录 $BASE，不动其它已存在的服。
#
# 流程：deploy.env -> CRLF 归一化 -> 预检 -> 停旧实例并归档 -> clone srvpro + npm ci
#   -> 生成 config -> 铺规则/老效果层 -> clone ygopro + 子模块 -> 打内核补丁
#   -> 编译 -> badip 补丁 -> pm2 启动 -> 验收
# CHECK_ONLY=1 只读预检；INSTALL_DEPS=1 可自动装系统依赖。
# ============================================================
set -euo pipefail

# ---- 可覆盖参数（deploy.env / 环境变量优先；不设就用默认值）---------------
PACK=${PACK_4082:-$(cd "$(dirname "$0")" 2>/dev/null && pwd)}
BASE=${WORK_4082:-${HOME:-/root}/ygopro-2pick}
NAME=${NAME_4082:-srvpro-2pick}
SRVPRO_COMMIT=${SRVPRO_COMMIT:-193ab4c}       # mycard/srvpro，与现役部署一致
YGOPRO_SHA=${YGOPRO_SHA:-176fc60c}             # mycard/ygopro server 分支
PREMAKE_URL=${PREMAKE_URL:-https://github.com/premake/premake-core/releases/download/v5.0.0/premake-5.0.0-linux.tar.gz}

# 可选：用一份现成的 408 资源树覆盖「基础资源」来源（高级用法，默认不用）。
RES_408=${RES_408:-}
# 408 老效果脚本覆写层，默认 <部署包>/expansions/
EXPANSIONS=${EXPANSIONS_408:-$PACK/expansions}

# ---- 0a. 读取部署期环境（deploy.env 本机私有，不进 git）--------------------
ENV_FILE=${DEPLOY_ENV:-$PACK/deploy.env}
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    echo "  部署参数：$ENV_FILE"
else
    echo "  部署参数：无 deploy.env，使用默认值 / 命令行环境变量"
    echo "            （模板：deploy.env.example）"
fi
# 这些变量可能没在 env 文件里定义，set -u 下先给默认
PORT_4082=${PORT_4082:-4082}
JOBS=${JOBS:-}

echo "==================== 0b. 文本文件行尾归一化 ===================="
# Windows 写出的 .sh/.py/.lua/.json 常带 CRLF，Linux 下 bash 会报
# "\r: command not found"、Lua 也可能解析失败。部署前统一去掉，幂等。
if [ "${KEEP_CRLF:-0}" = 1 ]; then
    echo "  KEEP_CRLF=1，跳过"
elif command -v python3 >/dev/null 2>&1; then
    python3 - "$PACK" "$0" "$PACK/deploy.sh" <<'PY'
import os, sys
root = sys.argv[1]
# 跳过正在执行的脚本自身（相对路径 / 绝对路径两种调用方式都覆盖）
skip = {os.path.abspath(x) for x in sys.argv[2:] if x}
exts = ('.sh', '.py', '.lua', '.json', '.txt', '.conf', '.env', '.example')
n = 0
for base, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if d != '_dev']
    for name in files:
        if not name.endswith(exts):
            continue
        p = os.path.join(base, name)
        # 绝不改写正在执行的脚本自身：bash 可能还没把它读完，会截断后续命令
        if os.path.abspath(p) in skip:
            continue
        with open(p, 'rb') as f:
            b = f.read()
        if b'\r\n' in b:
            with open(p, 'wb') as f:
                f.write(b.replace(b'\r\n', b'\n'))
            n += 1
print('  归一化 %d 个文件（CRLF -> LF；已跳过运行中的脚本自身）' % n)
PY
else
    echo "  ⚠ 没有 python3，跳过行尾归一化；若报错请手工执行 sed -i 去掉行尾 CR"
fi

echo "==================== 0c. 前置检查 ===================="
pkg_hint() {
    if command -v apt-get >/dev/null 2>&1; then
        echo "apt update && apt install -y build-essential git python3 liblua5.4-dev libsqlite3-dev curl ca-certificates"
    elif command -v dnf >/dev/null 2>&1; then
        echo "dnf install -y gcc-c++ make git python3 lua-devel sqlite-devel curl ca-certificates"
    elif command -v yum >/dev/null 2>&1; then
        echo "yum install -y gcc-c++ make git python3 lua-devel sqlite-devel curl ca-certificates"
    elif command -v apk >/dev/null 2>&1; then
        echo "apk add --no-cache build-base git python3 lua5.4-dev sqlite-dev curl ca-certificates"
    elif command -v pacman >/dev/null 2>&1; then
        echo "pacman -S --needed base-devel git python lua sqlite curl ca-certificates"
    else
        echo "请自行安装：C++ 编译器 / make / git / python3 / lua 开发头文件 / curl"
    fi
}
HINT=$(pkg_hint)
if [ "${INSTALL_DEPS:-0}" = 1 ]; then
    if [ "$(id -u 2>/dev/null || echo 1)" = 0 ]; then
        echo "  INSTALL_DEPS=1，安装系统依赖：$HINT"
        sh -c "$HINT"
        # pm2 属于 Node 生态，不在系统包管理器里；顺手装上，失败不阻断（下面会统一检查）
        if command -v npm >/dev/null 2>&1 && ! command -v pm2 >/dev/null 2>&1; then
            echo "  INSTALL_DEPS=1，安装 pm2"
            npm install -g pm2 || echo "  ⚠ pm2 安装失败，请手工执行 npm install -g pm2"
        fi
    else
        echo "  ⚠ INSTALL_DEPS=1 需要 root，跳过自动安装"
    fi
fi
MISSING=0
for c in git node npm pm2 g++ make python3 curl; do
    if ! command -v "$c" >/dev/null 2>&1; then
        echo "  ❌ 缺 $c"; MISSING=1
    fi
done
if [ "$MISSING" = 1 ]; then
    echo "  安装建议：$HINT"
    echo "            npm install -g pm2"
    exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "  ℹ 没有 sqlite3 命令行，只影响卡池张数自检（可选）"
fi
if ! command -v strip >/dev/null 2>&1; then
    echo "  ℹ 没有 strip，编译后跳过精简二进制（不影响运行）"
fi

LUA_OK=0
for d in /usr/include/lua5.4 /usr/include/lua5.3 /usr/include/lua /usr/local/include/lua5.4 /usr/local/include; do
    if [ -f "$d/lua.h" ]; then LUA_OK=1; break; fi
done
if [ "$LUA_OK" != 1 ]; then
    echo "  ⚠ 没找到 lua 开发头文件，编译会失败。安装建议：$HINT"
fi
NODE_MAJ=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
if [ "$NODE_MAJ" -lt 16 ] 2>/dev/null; then
    echo "  ⚠ node $NODE_MAJ 偏旧，srvpro 建议 Node >= 16（官方镜像用 24）"
fi

[ -f "$PACK/port_2pick.py" ]             || { echo "❌ 部署包不完整：缺 port_2pick.py"; exit 1; }
[ -f "$PACK/config/generate-config.py" ] || { echo "❌ 部署包不完整：缺 config/generate-config.py"; exit 1; }
[ -f "$PACK/config/config.json.example" ]|| { echo "❌ 部署包不完整：缺 config/config.json.example"; exit 1; }
[ -d "$PACK/2pick" ]                      || { echo "❌ 部署包不完整：缺 2pick/"; exit 1; }

# 配置来源：部署者若放了自己的完整 config.json 就直接沿用；否则部署期生成
CFG_SOURCE=generated
if [ -f "$PACK/config/config.json" ]; then
    CFG_SOURCE=local
    echo "  ⚠ 检测到 $PACK/config/config.json，将直接沿用（不重新生成）"
    echo "     这是给高级用法留的兼容路径；走新流程请删掉它，改用 deploy.env"
    PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["port"])' "$PACK/config/config.json")
else
    PORT=$PORT_4082
fi
echo "  配置来源：$CFG_SOURCE（$([ "$CFG_SOURCE" = generated ] && echo 部署期生成 || echo 本地文件)）"

# 408 老效果覆写层：缺失不阻断部署（规则仍能跑），但要明确警告
if [ -d "$EXPANSIONS/script" ]; then
    NSCR=$(find "$EXPANSIONS/script" -maxdepth 1 -type f -name 'c*.lua' 2>/dev/null | wc -l || true)
    echo "  408 老效果脚本：$EXPANSIONS/script（$NSCR 个）"
    if [ "$NSCR" -lt 1 ]; then echo "  ⚠ 老效果层是空的，408 老效果不会生效"; fi
else
    echo "  ⚠ 没有 408 老效果覆写层（$EXPANSIONS）：部署仍会完成，但会全用现代效果脚本"
    echo "     生成方式：python3 _dev/tools/extract-408-scripts.py --ypk <TTT-MIX.ypk>"
fi
if [ -n "$RES_408" ]; then
    if [ ! -f "$RES_408/cards.cdb" ] || [ ! -d "$RES_408/script" ]; then
        echo "❌ RES_408 指定的资源树不完整（需要 cards.cdb + script/）：$RES_408"; exit 1
    fi
    echo "  ⚠ 已用 RES_408 覆盖基础资源来源：$RES_408"
fi

echo "  部署包：$PACK"
echo "  实例目录：$BASE    主端口：$PORT    pm2 名：$NAME"
echo "  基础资源：$([ -n "$RES_408" ] && echo "$RES_408" || echo "\$BASE/src（mycard/ygopro@$YGOPRO_SHA，官方开源）")"

# 只读端口提示（不阻断：旧实例正在跑时端口当然被占，第 1 步会先停它）
if command -v ss >/dev/null 2>&1; then
    BUSY=$(ss -tln 2>/dev/null | grep -c ":${PORT} " || true)
    if [ "$BUSY" != 0 ]; then echo "  ℹ 端口 $PORT 当前有监听（若是本服旧实例，属正常）"; fi
fi

echo "现状（下面这些进程不会被本脚本改动）："
pm2 list || true

# 配置预览：只打印摘要，不写文件、不打印随机密钥
if [ "$CFG_SOURCE" = generated ]; then
    echo "---------------- 部署期配置预览 ----------------"
    python3 "$PACK/config/generate-config.py" \
        --template "$PACK/config/config.json.example" --print
    echo "----------------------------------------------"
fi

# 只读预检：CHECK_ONLY=1 bash deploy.sh —— 只跑第 0 步，绝不改任何东西
if [ "${CHECK_ONLY:-0}" = 1 ]; then
    echo "✅ 前置检查通过（CHECK_ONLY=1，未做任何改动）"
    echo "   依赖 / 端口 / 配置来源 / 资源来源都已确认；去掉 CHECK_ONLY 即开始部署。"
    exit 0
fi

echo "==================== 1. 停旧实例 + 归档 ===================="
TS=$(date +%Y%m%d-%H%M%S)
ARCHIVED=0
pm2 delete "$NAME" 2>/dev/null || echo "  pm2 里没有 $NAME（首次部署）"
if [ -e "$BASE" ]; then
    mv "$BASE" "$BASE.bak-$TS"
    echo "  旧目录已归档：$BASE.bak-$TS"
    ARCHIVED=1
fi
# premake5 优先从旧归档找（省一次下载）
PREMAKE_FROM=""
if [ -x "$BASE.bak-$TS/src/premake5" ]; then
    PREMAKE_FROM="$BASE.bak-$TS/src/premake5"
fi

echo "==================== 2. 克隆 srvpro + 生产依赖 ===================="
GIT_TERMINAL_PROMPT=0 git clone https://github.com/mycard/srvpro "$BASE"
git -C "$BASE" checkout -q "$SRVPRO_COMMIT"
cd "$BASE"
# 官方 Dockerfile.lite 用 NODE_ENV=production npm ci；lock 文件过旧时才退回 install
if ! npm ci --omit=dev --no-audit --no-fund; then
    echo "  npm ci 失败（老 lockfile 与新 npm 不兼容时常见），改用 npm install"
    npm install --omit=dev --no-audit --no-fund
fi
# 与官方镜像一致：config / decks / replays / logs 四个目录都要存在
mkdir -p "$BASE/config" "$BASE/decks" "$BASE/replays" "$BASE/logs" \
         "$BASE/ygopro/2pick" "$BASE/ygopro/expansions/script"

echo "==================== 3. 生成 srvpro 配置（部署期，零敏感数据进仓库）===================="
if [ "$CFG_SOURCE" = local ]; then
    cp "$PACK/config/config.json" "$BASE/config/config.json"
    echo "  已复制本地提供的 config.json"
else
    python3 "$PACK/config/generate-config.py" \
        --template "$PACK/config/config.json.example" \
        --out "$BASE/config/config.json" --force
fi
# 验收端口一律以「最终生效的配置」为准
PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["port"])' "$BASE/config/config.json")

echo "==================== 4. 运行目录：2pick 规则 + 408 老效果覆写层 ===================="
cp -a "$PACK/2pick/." "$BASE/ygopro/2pick/"
if [ -d "$EXPANSIONS" ]; then
    cp -a "$EXPANSIONS/." "$BASE/ygopro/expansions/"
    echo "  已铺 408 老效果脚本 -> ygopro/expansions/script/"
fi
if command -v sqlite3 >/dev/null 2>&1; then
    echo "卡池张数：$(sqlite3 "$BASE/ygopro/2pick/408.cdb" 'select count(*) from datas;' 2>/dev/null || echo '读取失败')"
fi

echo "==================== 5. 克隆 ygopro server 分支源码 ===================="
GIT_TERMINAL_PROMPT=0 git clone --no-checkout https://github.com/mycard/ygopro "$BASE/src"
git -C "$BASE/src" checkout -q "$YGOPRO_SHA"
git -C "$BASE/src" submodule update --init --recursive --depth 1
# 部分 Git / 服务端不允许按 SHA 浅 fetch，会导致子模块目录是空的；去掉 depth 重试一次
NSUB=$(find "$BASE/src/script" -type f -name '*.lua' 2>/dev/null | wc -l || true)
if [ "$NSUB" -lt 100 ]; then
    echo "  ⚠ 子模块浅克隆只拿到 $NSUB 个 lua，改为完整 fetch 重试"
    git -C "$BASE/src" submodule update --init --recursive
    NSUB=$(find "$BASE/src/script" -type f -name '*.lua' 2>/dev/null | wc -l || true)
fi
echo "  script/ 子模块 lua 数：$NSUB"
[ "$NSUB" -ge 100 ] || { echo "❌ script 子模块没拉全，检查网络 / Git 版本"; exit 1; }
echo "ocgcore = $(git -C "$BASE/src/ocgcore" log --oneline -1)"
echo "script  = $(git -C "$BASE/src/script" log --oneline -1)"

echo "==================== 5b. 基础资源（官方仓库自带，零自备）===================="
SRCRES="$BASE/src"
if [ -n "$RES_408" ]; then SRCRES="$RES_408"; fi
for f in cards.cdb lflist.conf strings.conf; do
    if [ -f "$SRCRES/$f" ]; then
        cp -a "$SRCRES/$f" "$BASE/ygopro/"
        echo "  $f  <- $SRCRES ($(wc -c < "$SRCRES/$f") bytes)"
    else
        echo "  ⚠ 缺 $SRCRES/$f"
    fi
done
[ -f "$BASE/ygopro/cards.cdb" ] || { echo "❌ 没拿到 cards.cdb，无法启动"; exit 1; }
if [ -d "$SRCRES/script" ]; then
    cp -a "$SRCRES/script" "$BASE/ygopro/"
    echo "  script/  <- $SRCRES/script ($(find "$BASE/ygopro/script" -type f -name '*.lua' 2>/dev/null | wc -l || true) 个 lua)"
else
    echo "❌ 缺 $SRCRES/script/（子模块没拉全？）"; exit 1
fi
# 官方 script 子模块本来就不含 special.lua。即使旧资源树里带了一份也不用删：
# ScriptReaderEx 的固定查找序是 ./specials/ -> ./expansions/script/ -> ./script/，
# expansions 版存在时 script 版永远读不到（"always read expansions first"）。
# 这里只提示，避免有人误以为两份都在生效。
if [ -f "$BASE/ygopro/script/special.lua" ]; then
    echo "  ℹ 检测到 ygopro/script/special.lua：expansions 版优先，这份不会被加载（保留不删）"
fi

echo "==================== 6. 打内核补丁 + 生成 special.lua ===================="
python3 "$PACK/port_2pick.py" --root "$BASE/src" --pack "$PACK"
SP="$BASE/src/expansions/script/special.lua"
[ -f "$SP" ] || { echo "❌ port_2pick.py 没生成 $SP"; exit 1; }
grep -q "Auxiliary.Load2PickRule" "$SP" || { echo "❌ special.lua 缺 2pick 规则挂载"; exit 1; }
grep -q "EFFECT_DISABLE_FIELD" "$SP"   || { echo "❌ special.lua 缺禁用额外怪兽区"; exit 1; }
mkdir -p "$BASE/ygopro/expansions/script"
cp -f "$SP" "$BASE/ygopro/expansions/script/special.lua"
echo "  已同步 expansions/script/special.lua（$(wc -c < "$SP") bytes）"

echo "==================== 7. 编译 ===================="
is_elf() { [ "$(head -c 4 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "7f454c46" ]; }
premake_usable() {
    local b="$1" v
    [ -x "$b" ] || return 1
    v=$(cd /tmp && "$b" --version 2>/dev/null | head -1 || true)
    [ -n "$v" ] || return 1
    case "$v" in *beta1*|*beta2*) return 1 ;; esac
    return 0
}
PM=""
for c in "$PREMAKE_FROM" "$(command -v premake5 2>/dev/null || true)"; do
    if [ -n "$c" ] && premake_usable "$c"; then PM="$c"; break; fi
done
if [ -z "$PM" ]; then
    echo "  本机没有可用的 premake5，下载官方 5.0.0 Linux 版..."
    echo "  URL=$PREMAKE_URL（可用 PREMAKE_URL 换成镜像）"
    TMPD=$(mktemp -d)
    if ! curl -fsSL --retry 3 --retry-delay 2 -o "$TMPD/pm.tar.gz" "$PREMAKE_URL"; then
        echo "❌ premake5 下载失败。请手动放一个 Linux x64 版到 $BASE/src/premake5 再重跑"; exit 1
    fi
    tar -xzf "$TMPD/pm.tar.gz" -C "$TMPD" 2>/dev/null || true
    BIN=$(find "$TMPD" -type f -name '*premake*' 2>/dev/null | head -1 || true)
    if [ -z "$BIN" ]; then
        for f in $(find "$TMPD" -type f 2>/dev/null); do
            if is_elf "$f"; then BIN="$f"; break; fi
        done
    fi
    if [ -n "$BIN" ] && is_elf "$BIN"; then
        cp "$BIN" "$BASE/src/premake5" && chmod +x "$BASE/src/premake5"
    else
        echo "❌ 解出的 premake5 不是可执行文件，请手动放置到 $BASE/src/premake5"; exit 1
    fi
    # ELF 头对了不等于能跑：官方预编译版是 glibc 构建，Alpine/musl 上会加载失败。
    # 这里实跑一次 --version，失败就给可操作的提示，别等到 gmake 阶段才报一堆看不懂的错。
    if ! premake_usable "$BASE/src/premake5"; then
        echo "❌ 下载的 premake5 在本机无法执行（常见于 Alpine/musl；官方预编译版按 glibc 构建）。"
        echo "   请自行编译一个放到 $BASE/src/premake5（官方 Dockerfile.lite 是 ./Bootstrap.sh），"
        echo "   或改用 glibc 发行版后重跑。"
        exit 1
    fi
    rm -rf "$TMPD"
    PM="$BASE/src/premake5"
fi
echo "premake5 = $PM ($((cd /tmp && "$PM" --version) 2>/dev/null | head -1))"
# 小内存机器 make -j$(nproc) 会 OOM；默认按 CPU / 可用内存取小值，上限 4
if [ -z "$JOBS" ]; then
    CPU=$(nproc 2>/dev/null || echo 2)
    MEM_GB=$(awk '/MemAvailable/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 2)
    if [ -z "$MEM_GB" ]; then MEM_GB=2; fi
    if [ "$MEM_GB" -lt 1 ]; then MEM_GB=1; fi
    JOBS=$CPU
    if [ "$JOBS" -gt "$MEM_GB" ]; then JOBS=$MEM_GB; fi
    if [ "$JOBS" -gt 4 ]; then JOBS=4; fi
    if [ "$JOBS" -lt 1 ]; then JOBS=1; fi
fi
echo "编译并行度 -j$JOBS（可用 JOBS= 或 deploy.env 覆盖）"
cd "$BASE/src"
"$PM" gmake --lua-deb
cd build && make config=release -j"$JOBS" CXXFLAGS="-std=c++14" && cd ..
BIN_NEW="$BASE/src/bin/release/ygopro"
[ -f "$BIN_NEW" ] || { echo "❌ 编译产物不存在：$BIN_NEW"; exit 1; }
echo "---- 二进制自检 ----"
for sym in SavePickDeck GetStartCount ResetTimeLimit IsPlayerNeedToPickDeck Exile PickLog; do
    n=$(grep -ac "$sym" "$BIN_NEW" || true)
    echo "  $sym => $n"
    if [ "$n" -lt 1 ]; then echo "❌ 二进制缺 $sym，补丁没进构建"; exit 1; fi
done
grep -aq "2pick-sync.log" "$BIN_NEW" || { echo "❌ 卡号补发标记缺失"; exit 1; }
if command -v strip >/dev/null 2>&1; then strip "$BIN_NEW"; fi
cp -f "$BIN_NEW" "$BASE/ygopro/ygopro"
chmod +x "$BASE/ygopro/ygopro"
echo "  已安装 $BASE/ygopro/ygopro ($(wc -c < "$BASE/ygopro/ygopro") bytes)"

echo "==================== 8. srvpro badip 补丁（可信代理不计入连接数）===================="
python3 - "$BASE/ygopro-server.js" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "isTrustedProxy(client.ip) && (ROOM_bad_ip[client.ip]" in s:
    print("  [跳过] badip 补丁已在")
    sys.exit(0)
pairs = [
    ("    if (ROOM_bad_ip[client.ip] > 5 || ROOM_connected_ip[client.ip] > 10) {",
     "    if (!isTrustedProxy(client.ip) && (ROOM_bad_ip[client.ip] > 5 || ROOM_connected_ip[client.ip] > 10)) {"),
    ('            if (handle_data.feedback.type === "OVERSIZE" || ROOM_bad_ip[client.ip] > 5) {',
     '            if (handle_data.feedback.type === "OVERSIZE" || handle_data.feedback.type === "INVALID_PACKET") { log.warn("BADPACKET type=" + handle_data.feedback.type + " ip=" + client.ip); }\n'
     '            if (!isTrustedProxy(client.ip) && (handle_data.feedback.type === "OVERSIZE" || ROOM_bad_ip[client.ip] > 5)) {'),
    ('          if (handle_data.feedback.type === "OVERSIZE" || handle_data.feedback.type === "INVALID_PACKET" || ROOM_bad_ip[client.ip] > 5) {',
     '          if (handle_data.feedback.type === "OVERSIZE" || handle_data.feedback.type === "INVALID_PACKET") { log.warn("BADPACKET type=" + handle_data.feedback.type + " ip=" + client.ip); }\n'
     '          if (!isTrustedProxy(client.ip) && (handle_data.feedback.type === "OVERSIZE" || handle_data.feedback.type === "INVALID_PACKET" || ROOM_bad_ip[client.ip] > 5)) {'),
]
for old, new in pairs:
    n = s.count(old)
    if n != 1:
        sys.exit("[失败] srvpro 锚点命中 %d 次（应为 1）：%s" % (n, old[:80]))
    s = s.replace(old, new, 1)
open(p, "w", encoding="utf-8").write(s)
print("  [OK] 3 处 badip 判定加 !isTrustedProxy 守卫")
PY

echo "==================== 9. 启动 ===================="
cd "$BASE"
pm2 start ygopro-server.js --name "$NAME"
pm2 save

echo "==================== 10. 验收 ===================="
sleep 2
# 用 python socket 探测监听，比 grep ss 输出更可移植（ss / netstat 可能没装）
LISTENING=0
if python3 - "$PORT" <<'PY'
import socket, sys
p = int(sys.argv[1]); s = socket.socket(); s.settimeout(1)
try:
    r = s.connect_ex(('127.0.0.1', p))
finally:
    s.close()
sys.exit(0 if r == 0 else 1)
PY
then LISTENING=1; fi
if [ "$LISTENING" != 1 ]; then
    echo "❌ ${PORT} 未监听，看日志：pm2 logs $NAME --lines 50 --nostream"; exit 1
fi
pm2 logs "$NAME" --lines 10 --nostream || true

# 只有真的归档过旧实例，回滚命令才成立；全新部署时不要打印一个不存在的 .bak 路径
if [ "$ARCHIVED" = 1 ]; then
    ROLLBACK="pm2 delete $NAME && rm -rf $BASE && mv $BASE.bak-$TS $BASE && cd $BASE && pm2 start ygopro-server.js --name $NAME && pm2 save"
else
    ROLLBACK="本次为全新部署，无旧归档可回滚；直接重跑 bash $PACK/deploy.sh 即可（会归档半残目录重来）"
fi

cat <<EOF

部署完成。
  实例目录：$BASE    pm2 进程：$NAME    主端口：$PORT
  配置来源：$CFG_SOURCE -> $BASE/config/config.json（权限 600，不进版本库）
  基础资源：官方 mycard/ygopro@$YGOPRO_SHA 仓库根 + script 子模块
  老效果层：$BASE/ygopro/expansions/script/（$(find "$BASE/ygopro/expansions/script" -type f -name 'c*.lua' 2>/dev/null | wc -l || true) 个 + special.lua）
  回滚：$ROLLBACK
  客户端连本服 $PORT 端口建房测试。
  验收：cat /tmp/2pick-sync.log（卡号补发）、cat /tmp/2pick-load.log（规则挂载）
  ⚠ 若某张卡效果报错，看 pm2 logs 里的 "YGOPRO Debug:" —— 多半是老脚本与内核 API 不匹配。
EOF
