#!/usr/bin/env bash
# ============================================================
# 【修订 2 后已非必需，仅保留作高级用法】
# 在「现役 408 服」上执行：把 408 基础资源打成一个 tar.gz，供 RES_408 指定使用。
#
# 默认部署**完全不需要这一步**：cards.cdb / lflist.conf / strings.conf / script/
# 由 deploy.sh 从自己 clone 的 mycard/ygopro@176fc60c 仓库根与子模块取得；
# 408 老效果脚本由 _dev/tools/extract-408-scripts.py 从 TTT-MIX.ypk 提取到 expansions/。
# 只有想强制用某台现成 408 服的完整资源树时，才需要本脚本：
#   RES_408=/path/to/extracted-tree bash deploy.sh
#
# 只读，绝不修改服务器上的任何文件、不碰 pm2 进程。
#
# 用法：
#   bash pack-base-408.sh                       # 默认 /root/ygopro -> /root/base-408.tar.gz
#   bash pack-base-408.sh /opt/408 /tmp/b.tgz  # 自定义源目录 / 产物
#
# 新服务器上（仅高级用法）：
#   mkdir -p /opt/408-base && tar xzf base-408.tar.gz -C /opt/408-base
#   RES_408=/opt/408-base bash <部署包>/deploy.sh
# ============================================================
set -euo pipefail

RES=${1:-/root/ygopro}
OUT=${2:-/root/base-408.tar.gz}

echo "源资源目录：$RES"
echo "产物：$OUT"

[ -f "$RES/cards.cdb" ] || { echo "❌ 缺 $RES/cards.cdb"; exit 1; }
[ -d "$RES/script" ]   || { echo "❌ 缺 $RES/script/"; exit 1; }
if [ ! -d "$RES/expansions" ]; then
    echo "⚠ 没有 $RES/expansions/ —— 408 自定义卡脚本会缺失，部署后部分卡可能无效果"
fi

FILES=(cards.cdb)
for f in lflist.conf strings.conf; do
    [ -f "$RES/$f" ] && FILES+=("$f") || echo "⚠ 没有 $f，deploy.sh 会从官方仓库根补齐"
done
FILES+=(script)
[ -d "$RES/expansions" ] && FILES+=(expansions)

echo "将打包：${FILES[*]}"
tar czf "$OUT" -C "$RES" "${FILES[@]}"
echo "完成：$(ls -lh "$OUT" | awk '{print $5}')  $OUT"

cat <<'EOF'

⚠ 授权提示：cards.cdb（卡片数据）、script/（社区效果脚本）、expansions/（自定义脚本）
   属于第三方内容，授权由提供者负责。不要把这份 tar.gz 直接传进公开 GitHub 仓库，
   建议放在私有位置 / Release 附件 / 由使用者自行从自己的 408 客户端提取。
   本项目仓库只分发补丁脚本与规则文件，不分发这些卡数据。
EOF
