#!/usr/bin/env bash
# ============================================================
# 只同步规则数据（不重编）：pick.lua / combo.lua / exclusive.lua /
# limits.lua / levels.lua / ritual.lua / 408.cdb / cardnames.txt
#
# 什么时候用：只改了 Lua 或卡池数据，几秒钟生效。
# 什么时候不能用：改过 C++ 补丁（port_2pick.py）→ 重跑 deploy.sh。
#
# 用法：上传新 2pick-408 后 bash /root/2pick-408/push-rules.sh
# ============================================================
set -euo pipefail

# 路径可用环境变量覆盖：PACK_4082=/opt/2pick-408 RUN_4082=/opt/ygopro-2pick/ygopro bash push-rules.sh
PACK=${PACK_4082:-$(cd "$(dirname "$0")" 2>/dev/null && pwd)}
# 与 deploy.sh 共用同一份部署期参数（deploy.env 含真实 IP，不进 git）
ENV_FILE=${DEPLOY_ENV:-$PACK/deploy.env}
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi
NAME=${NAME_4082:-srvpro-2pick}
RUN=${RUN_4082:-${WORK_4082:-${HOME:-/root}/ygopro-2pick}/ygopro}

[ -d "$RUN/2pick" ] || { echo "找不到 $RUN/2pick，先跑一次 deploy.sh"; exit 1; }

echo "==================== 备份现有规则 ===================="
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$RUN/2pick/backup-$STAMP"
for f in combo.lua exclusive.lua limits.lua pick.lua levels.lua ritual.lua 408.cdb cardnames.txt; do
    [ -f "$RUN/2pick/$f" ] && cp -a "$RUN/2pick/$f" "$RUN/2pick/backup-$STAMP/" || true
done
echo "备份目录：$RUN/2pick/backup-$STAMP"

echo "==================== 覆盖规则文件 ===================="
for f in combo.lua exclusive.lua limits.lua pick.lua levels.lua ritual.lua 408.cdb cardnames.txt; do
    if [ -f "$PACK/2pick/$f" ]; then
        cp -a "$PACK/2pick/$f" "$RUN/2pick/$f"
        echo "  $f  <- $(stat -c %s "$PACK/2pick/$f") bytes"
    fi
done

echo "==================== 全局规则 / 录像配置 ===================="
SP="$RUN/expansions/script/special.lua"
if [ -f "$SP" ]; then
    if ! grep -F "EFFECT_DISABLE_FIELD" "$SP" >/dev/null; then
        cat >> "$SP" <<'LUA'

-- ==== 408 环境全局规则：禁用额外怪兽区（push-rules.sh 兜底追加）====
local e1=Effect.GlobalEffect()
e1:SetType(EFFECT_TYPE_FIELD)
e1:SetCode(EFFECT_DISABLE_FIELD)
e1:SetValue(0x600060)
Duel.RegisterEffect(e1,0)
LUA
        echo "  已给 expansions/script/special.lua 追加禁用额外怪兽区"
    else
        echo "  expansions/script/special.lua 已禁用额外怪兽区"
    fi
else
    echo "  ⚠ 找不到 $SP —— special.lua 由 port_2pick.py 生成、deploy.sh 同步，请重跑 deploy.sh"
fi

CFG="${RUN%/ygopro}/config/config.json"
if [ -f "$CFG" ]; then
    python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding='utf-8'))
m = d.setdefault('modules', {})
if m.get('replay_delay') is not False:
    m['replay_delay'] = False
    json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print('  config.json: replay_delay -> false（每小局立即发本局录像）')
else:
    print('  config.json: replay_delay 已是 false')
PY
else
    echo "  ⚠ 找不到 $CFG"
fi

echo "==================== 自检 ===================="
echo "combo 组数：$(grep -c '^[[:space:]]*{' "$RUN/2pick/combo.lua" || true)"
echo "exclusive 卡数：$(grep -c '=true' "$RUN/2pick/exclusive.lua" || true)"
echo "ENABLE_COMBO：$(grep -m1 'local ENABLE_COMBO' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "COPY_MAIN   ：$(grep -m1 'local COPY_MAIN' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "MAIN_ROUNDS ：$(grep -m1 'local MAIN_ROUNDS' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "COMBO_ADD   ：$(grep -m1 'local COMBO_ADD_CARDS' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "DECK_TRIM   ：$(grep -m1 'local DECK_TRIM_COUNT' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "SAME_NAME_MAX：$(grep -m1 'local SAME_NAME_MAX' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "DECK_LIMIT_EXCEPT：$(grep -m1 'local DECK_LIMIT_EXCEPT' "$RUN/2pick/pick.lua" | sed 's/--.*//')（禁限表例外，黑洞=1）"
echo "整理规则    ：$(grep -F 'if limits.forbidden[code] then return 0 end' "$RUN/2pick/pick.lua" >/dev/null && echo '按禁限表(禁0/限1/准限2/其他3)' || echo '⚠ 未找到 DeckLimitFor，是旧规则')"
echo "CONFIRM_MODE：$(grep -m1 'local CONFIRM_MODE' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "PICK_LOG    ：$(grep -m1 'local ENABLE_PICK_LOG' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "PICK_TIME_LIMIT：$(grep -m1 'local PICK_TIME_LIMIT' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "CONFIRM_EXTRA  ：$(grep -m1 'local CONFIRM_EXTRA' "$RUN/2pick/pick.lua" | sed 's/--.*//')"
echo "轮抽序列    ：$(awk '/^\t\tcombo_banned\[0\]=\{\}$/{c++} END{print c+0}' "$RUN/2pick/pick.lua") 段 combo（应为 2）"
# CONFIRM_EXTRA=true 需要服务端二进制打过 MSG_CONFIRM_CARDS 单播补丁；
# 只改规则时也能顺手从源码树确认一下，避免「弹窗广播给对手」。
if grep -qE '^local CONFIRM_EXTRA[[:space:]]*=[[:space:]]*true' "$RUN/2pick/pick.lua"; then
    SRC="${RUN%/ygopro}/src/gframe/single_duel.cpp"
    if [ -f "$SRC" ] && ! grep -q 'pbuf\[5\] != LOCATION_DECK && pbuf\[5\] != LOCATION_EXTRA' "$SRC"; then
        echo "⚠ CONFIRM_EXTRA=true，但 $SRC 里没有额外卡组单播补丁。"
        echo "  请先跑一次 bash ${PACK}/deploy.sh 全新重编，或把 CONFIRM_EXTRA 改回 false。"
    fi
fi
echo "  Duel.Exile 清理调用：$(grep -cE '^[[:space:]]*Duel\.Exile' "$RUN/2pick/pick.lua" || true) 处（应 ≥4；=0 说明是旧规则，未选卡会一直留在除外区）"
echo "  内核 PickLog：$(strings -a "$RUN/ygopro" 2>/dev/null | grep -cx PickLog || echo 0)（0 = 二进制没有补丁，pick.lua 会自动跳过）"
echo "  卡名表 cardnames.txt：$(grep -c . "$RUN/2pick/cardnames.txt" 2>/dev/null || echo '（缺失，选卡记录会显示卡号）')"
echo "卡池张数    ：$(sqlite3 "$RUN/2pick/408.cdb" 'select count(*) from datas' 2>/dev/null || echo '（无 sqlite3，跳过）')"
echo "主题包除外  ：$(grep -Fc 'combo_banned[p][pk]=true' "$RUN/2pick/pick.lua" || true) 处（本轮拉黑，应为 1）"
echo "仪式配对    ：$(grep -c '\]=' "$RUN/2pick/ritual.lua" || true) 条（怪兽+魔法）"
# 三局两胜第二局跳换 side（v3.7，回到 v3.2 写法）。2pick 没有 side deck，
# 不弹「更换副卡组」界面、第二局 BP/M2/EP 按钮正常、不会双弹保存录像：
# 直接 STOC_DUEL_START + STOC_SELECT_TP 给上一局败者（tp_player）。
SRC_SD="${RUN%/ygopro}/src/gframe/single_duel.cpp"
[ -f "$SRC_SD" ] || SRC_SD="${WORK_4082:-/root/ygopro-2pick}/src/gframe/single_duel.cpp"
if [ -f "$SRC_SD" ]; then
    if grep -q '第二局跳换 side' "$SRC_SD"; then
        echo "  比赛第二局：跳换 side（v3.7，第二局由败者选先后攻，源码补丁在）"
    elif grep -q '恢复官方换 side 流程' "$SRC_SD"; then
        echo "⚠ 比赛第二局：还是 v3.6 恢复换 side 补丁（会弹「更换副卡组」界面）。"
        echo "  请跑一次 bash ${PACK}/deploy.sh 全新重编（会得到 v3.7 跳换 side 定稿版）。"
    elif grep -q '第二局由上一局败者选先后攻' "$SRC_SD"; then
        echo "⚠ 比赛第二局：还是 v3.0 跳过换 side 补丁（会 BP/M2/EP 按钮消失 + 双弹窗）。"
        echo "  请跑一次 bash ${PACK}/deploy.sh 全新重编（会得到 v3.7 跳换 side 定稿版）。"
    elif grep -q '跳过更换副卡组' "$SRC_SD"; then
        echo "⚠ 比赛第二局：是更旧补丁（第二局会重新猜拳）。"
        echo "  请跑一次 bash ${PACK}/deploy.sh 全新重编（会得到 v3.7 跳换 side 定稿版）。"
    else
        echo "⚠ 比赛第二局：源码里没有换 side 补丁，三局两胜第二局会卡住。"
        echo "  请跑一次 bash ${PACK}/deploy.sh 全新重编后再开比赛。"
    fi
fi

echo "额外怪兽区  ：$(grep -Fc 'EFFECT_DISABLE_FIELD' "$RUN/expansions/script/special.lua" 2>/dev/null || true) 处（应为 1）"
echo "每局录像    ：replay_delay=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8')).get('modules',{}).get('replay_delay'))" "$CFG" 2>/dev/null || echo '（读取失败）')（应为 False）"
echo "录像卡组修复：$(grep -Fc '2pick: 轮抽录像重建' "${RUN%/ygopro}/src/gframe/single_duel.cpp" 2>/dev/null || true) 处（应为 1，需 deploy.sh 全新重编）"
echo "录像RNG复位 ：$(grep -Fc 'reset_random(pduel);' "${RUN%/ygopro}/src/gframe/single_duel.cpp" 2>/dev/null || true) 处（应为 1；修复回放洗手牌/掷币错位，需 deploy.sh 全新重编）"
echo "录像缓冲授权：$(grep -Fc 'friend class SingleDuel' "${RUN%/ygopro}/src/gframe/replay.h" 2>/dev/null || true) 处（应为 1）"
echo "==================== 重启 $NAME ===================="
pm2 restart "$NAME" --update-env
sleep 2
pm2 list | grep "$NAME" || true
echo
echo "完成。已开的房间要重开才用新规则（新房间直接生效）。"
echo "回滚：cp -a $RUN/2pick/backup-$STAMP/. $RUN/2pick/ && pm2 restart $NAME"
