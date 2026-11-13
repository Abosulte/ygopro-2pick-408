#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 mycard/ygopro-2pick 的 2pick/pick.lua 改造成 408 环境可用版本。

用法：python3 patch_pick.py [原版 pick.lua 路径] [输出 pick.lua 路径]
默认读包内 _dev/data/_upstream/pick.lua（上游原版快照），输出到 ./2pick/pick.lua。
每处替换都会 assert，原版一变就立刻报错，避免静默生成错误规则。
"""
import os
import re
import sys

# 脚本位于 _dev/tools/，包根在两级之上；所有数据路径都以包根为基准
HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEV = os.path.join(HERE, "_dev")


def _default_src():
    # 包内快照优先（_dev/data/_upstream/）；其次旧工作目录（开发机上才有）
    for p in (os.path.join(DEV, "data", "_upstream", "pick.lua"),
              os.path.join(HERE, "..", "_work", "2pick-research", "pick.lua")):
        if os.path.isfile(p):
            return p
    return os.path.join(DEV, "data", "_upstream", "pick.lua")


SRC = sys.argv[1] if len(sys.argv) > 1 else _default_src()
DST = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "2pick", "pick.lua")

HEADER = """-- ============================================================
-- 408 环境 2pick 轮抽规则（由 patch_pick.py 从 mycard/ygopro-2pick 生成，勿手改）
--
-- 相对上游的主要改动：408 卡池 / 2006.3.1 禁限表、融合池替代同调超量连接
--   （408 没有那三池，留着会 math.random(0) 报错）、额度只在最终选中后计数、
--   未选卡 Duel.Exile、删掉官方二次确认（MDPro3 会卡在「剩余卡片」）、
--   combo 一组一弹窗、240 秒应答、私聊选卡记录、主题包「除外」本轮拉黑、
--   StartPick 开头 e:Reset()（防 EVENT_ADJUST 重入导致选卡无限重来）。
--
-- 轮抽序列：①额外融合 2 轮(8 选 4×2) ②combo ③主卡 4 轮(选中复制) ④combo
--   ⑤固定补大风暴/天使的施舍/黑洞 ⑥按禁限表整理(禁0/限1/准限2/其他3) ⑦自由删卡 0~6 张
--   -> 主卡约 40 张；融合怪由内核自动落 EXTRA，不占主卡组，删满后少于 40 属预期。
--   DECK_LIMIT_EXCEPT 只放行黑洞 1 张（⑤ 故意送的禁卡）。
--
-- 内核前提（port_2pick.py）：5 个 Lua API、MSG_SAVE_PICK_DECK/RESET_TIME、
--   关闭开局自动抽卡、MSG_PICK_LOG、YGOPRO_NO_LUA_SAFE；卡等级查 levels.lua。
-- ============================================================
"""

CONFIG = """-- ==== CONFIG ====
local ENABLE_DEEPTHINK = false  -- 408 无玩家技能（文件仍被 require，不能删）
local ENABLE_COMBO     = true   -- combo.lua 为空时必须关，否则 math.random(0) 报错
local COPY_MAIN        = true   -- 选中卡各复制 1 张 => 4 轮 x 4 x 2 = 32 张；false = 16 张
local CONFIRM_MODE     = 1      -- 0 = 不发确认；1 = 只发本轮选中（<4 张时自动改发整批）
local CONFIRM_EXTRA    = true   -- 额外轮也弹确认（需 patch_pick_chat.py 把 EXTRA 改成单播）
local PICK_TIME_LIMIT  = 240    -- 每轮应答时限（秒）；超时服务端发 MSG_WIN(0x3)，客户端断线
local ENABLE_PICK_LOG  = true   -- 聊天栏私聊「本轮选卡」（需 patch_pick_chat.py）
-- 408 定制流程（改这里就能调张数，改完必须重跑 patch_pick.py）
local MAIN_ROUNDS      = 4      -- 主卡组轮数：每轮 8 张候选选 4 张（原版 5 轮，删掉最后一轮）
local COMBO_ADD_CARDS  = {19613556,79571449,53129443}  -- 全部选完固定补进卡组：大风暴、天使的施舍、黑洞
local DECK_LIMIT_EXCEPT = {[53129443]=1}  -- 黑洞是禁卡，但 ⑤ 故意送 1 张，放行 1 张
local DECK_TRIM_COUNT  = 6      -- 最后自己从主卡组挑 0~DECK_TRIM_COUNT 张移出卡组（可以一张不删）
local SAME_NAME_MAX    = 3      -- 非禁限卡的同名上限（第 10 步整理时用）
local TEST_FAST_PICK  = false  -- 正式流程全开；true = 只留两次 combo（临时测试）
-- 张数账：额外 8 / 主卡 4 轮 x4 = 16 -> 复制 32 / combo 12 / 补 3 => 47，⑥削同名、⑦删 0~6 张后约 40。
--   融合怪自动落 EXTRA 不占主卡组，删满后少于 40 张属预期，不补卡。
-- ===========================
local limits = require("./2pick/limits")
local levels = require("./2pick/levels")        -- 卡号 -> 等级（替代 Duel.ReadCard）
local exclusive = require("./2pick/exclusive")  -- 只走 combo 的卡：不进普通轮抽池
local ritual = require("./2pick/ritual")        -- 仪式怪兽 <-> 仪式魔法 配对
local picked_limit = {[0]={},[1]={}}            -- 每玩家、每卡号「最终选中」张数
local combo_picked = {[0]=nil,[1]=nil}          -- 第一次 combo 选中的包下标（第二次必须避开）
local combo_banned = {[0]={},[1]={}}            -- 本轮被「除外」拉黑的包下标；②/④ 开始时各清一次

function Auxiliary.CardLevel(code)
	return levels[code] or 0
end

-- 额度检查：禁卡不出、限制 1、准限制 2、其余 3；extra_used 是本轮已展示的数量
function Auxiliary.CanPick(p,code,extra_used)
	if limits.forbidden[code] then return false end
	local used=(picked_limit[p][code] or 0)+(extra_used and (extra_used[code] or 0) or 0)
	local max=3
	if limits.limit1[code] then max=1
	elseif limits.limit2[code] then max=2 end
	return used<max
end

function Auxiliary.MarkPick(p,code)
	picked_limit[p][code]=(picked_limit[p][code] or 0)+1
end

-- 第 10 步整理的禁限表例外（卡号 -> 允许保留张数），见 DECK_LIMIT_EXCEPT
local EXCEPT_MAX={}
for code,n in pairs(DECK_LIMIT_EXCEPT) do EXCEPT_MAX[code]=n end

-- 第 10 步整理时，这个卡号在主卡组里最多留几张 —— 按 2006.3.1 禁限表：
--   禁卡 0 张 / 限制 1 张 / 准限制 2 张 / 其他 SAME_NAME_MAX(3) 张
-- DECK_LIMIT_EXCEPT 里的卡不受禁限表约束，按表里写的张数保留（黑洞：禁卡但故意送 1 张）。
function Auxiliary.DeckLimitFor(code)
	local ex=EXCEPT_MAX[code]
	if ex then return ex end
	if limits.forbidden[code] then return 0 end
	if limits.limit1[code] then return 1 end
	if limits.limit2[code] then return 2 end
	return SAME_NAME_MAX
end

-- 第 10 步「自动整理」：把主卡组整成符合禁限表的样式。
-- 触发原因：主卡组每张「选中」后又复制 1 张（COPY_MAIN），选中 3 张就是 6 张；
-- 限制卡/禁卡被复制后也会超额（1 张变 2 张），combo 包再撞同名会更多。
-- 超出额度的卡按出现顺序削掉，每组只留前 DeckLimitFor(code) 张。
function Auxiliary.TrimOverLimit(p)
	if not Duel.IsPlayerNeedToPickDeck(p) then return end
	local g=Duel.GetFieldGroup(p,LOCATION_DECK,0)
	if g:GetCount()==0 then return end
	local list={}
	for c in aux.Next(g) do list[#list+1]=c end
	local seen={}
	local sg=Group.CreateGroup()
	for _,c in ipairs(list) do
		local code=c:GetOriginalCode()
		seen[code]=(seen[code] or 0)+1
		if seen[code]>Auxiliary.DeckLimitFor(code) then
			sg:AddCard(c)
		end
	end
	if sg:GetCount()>0 then Duel.Exile(sg,REASON_RULE) end
end

-- 仪式配对：候选里出现仪式怪兽就补上它的仪式魔法（反之亦然）。
-- 表来自 2pick/ritual.lua；通用仪式魔法不点名怪兽，表里没有条目，行为不变。
function Auxiliary.AddRitualPartner(p,ag)
	local codes={}
	for c in aux.Next(ag) do codes[#codes+1]=c:GetOriginalCode() end
	for _,code in ipairs(codes) do
		local partners=ritual.monster_to_spell[code] or ritual.spell_to_monster[code]
		if partners then
			for _,rc in ipairs(partners) do
				if not exclusive[rc] and not ag:IsExists(Card.IsCode,1,nil,rc)
					and Auxiliary.CanPick(p,rc) then
					ag:AddCard(Duel.CreateToken(p,rc))
				end
			end
		end
	end
end

-- 随机挑一个 combo 包，排除三类：
--   lastpack —— 同一次 SinglePick 里已经给过的那组（保证两组不同）
--   exclude  —— 第一次 combo 真正选中拿走的包（跨轮永久避开）
--   banned   —— 本轮被玩家点「除外」拉黑的包（②/④ 各自清空）
-- 先把可选项收进 pool 再随机，不会像「重试 N 次」那样失败后返回 nil 导致漏卡。
function Auxiliary.PickComboPack(p,packed,lastpack,exclude,banned)
	local n=#packed
	if n==0 then return nil end
	local pool={}
	for i=1,n do
		if i~=lastpack and i~=exclude and not (banned and banned[i]) then pool[#pool+1]=i end
	end
	if #pool==0 then return nil end
	return pool[math.random(#pool)]
end
"""


NEW_SEQUENCE = """	-- ===== 408 轮抽序列（改这里 = 改流程，改完必须重跑 patch_pick.py）=====
	-- ① 额外融合 2 轮：每轮 8 张候选选 4 张，共 8 张
	if not TEST_FAST_PICK then
		for i=1,2 do
			for p=0,1 do
				Auxiliary.ArbitraryPick(p,4,{[1]=extra_sp[TYPE_FUSION],[2]=extra_sp[TYPE_FUSION]},{[1]=4,[2]=4},false,false,nil)
			end
		end
	end

	-- ② combo 第一次：先攻/后攻都只有 1 次重选（最多看 2 包，第 2 包必须换一组）
	--    点「除外」= 两组都不要：本轮拉黑这两组，重选换两组没给过的。
	if ENABLE_COMBO then
		combo_banned[0]={}
		combo_banned[1]={}
		for t=1,0,-1 do
			if Auxiliary.SinglePick(0,main,0,nil,nil,false,false,nil,combo_pack.pack,t>0) then break end
		end
		for t=1,0,-1 do
			if Auxiliary.SinglePick(1,main,0,nil,nil,false,false,nil,combo_pack.pack,t>0) then break end
		end
	end

	-- ③ 主卡组 MAIN_ROUNDS 轮：每轮 8 张候选选 4 张，选中各复制 1 张（16 -> 32）
	if not TEST_FAST_PICK then
		for i=1,MAIN_ROUNDS do
			local lists={[1]=main_monster,[3]=main_spell,[4]=main_trap}
			local lists_count={[1]=4,[3]=2,[4]=2}
			if i==1 or i==2 then
				lists[1]=main_plain
				lists_count[1]=2
				lists[2]=main_adv
				lists_count[2]=2
			end
			for p=0,1 do
				Auxiliary.ArbitraryPick(p,4,lists,lists_count,COPY_MAIN)
			end
		end
	end

	-- ④ combo 第二次：先攻 2 次重选、后攻 1 次；不能重复 ② 拿到的那组
	--    本轮拉黑表清空：② 里被「除外」的组在这一轮可以重新出现；
	--    只有 ② 真正选中拿走的那组由 combo_picked[p] 永久避开。
	if ENABLE_COMBO then
		combo_banned[0]={}
		combo_banned[1]={}
		for t=2,0,-1 do
			if Auxiliary.SinglePick(0,main,0,nil,nil,false,false,nil,combo_pack.pack,t>0,combo_picked[0]) then break end
		end
		for t=1,0,-1 do
			if Auxiliary.SinglePick(1,main,0,nil,nil,false,false,nil,combo_pack.pack,t>0,combo_picked[1]) then break end
		end
	end

	-- ⑤ 固定补卡（大风暴 / 天使的施舍 / 黑洞）：CreateToken 直取，不受禁限表约束
	--    ⑥ 会按禁限表整理：大风暴/天使的施舍是限制卡，最终留 1 张；黑洞在
	--    DECK_LIMIT_EXCEPT 里放行 1 张。⑦ 里玩家要是想删，这三张也可以删掉。
	if #COMBO_ADD_CARDS>0 then
		for p=0,1 do
			if Duel.IsPlayerNeedToPickDeck(p) then
				local g=Group.CreateGroup()
				for _,code in ipairs(COMBO_ADD_CARDS) do
					g:AddCard(Duel.CreateToken(p,code))
				end
				Duel.SendtoDeck(g,nil,0,REASON_RULE)
			end
		end
	end

	-- ⑥ 自动整理：把主卡组整成符合禁限表的样式（禁卡 0 张 / 限制 1 张 / 准限制 2 张 / 其他 3 张）。
	--    （「选中 + 复制」会叠出 4~6 张同名，限制卡也会 1 张变 2 张，先整理再让玩家删）
	--    DECK_LIMIT_EXCEPT 例外：黑洞是禁卡但 ⑤ 故意送，保留 1 张；大风暴/天使的施舍
	--    不写例外，按「限制卡最多 1 张」处理。
	for p=0,1 do
		Auxiliary.TrimOverLimit(p)
	end

	-- ⑦ 自由删卡：自己从主卡组挑 0~DECK_TRIM_COUNT 张移出卡组（40 张左右）
	--    min=0 表示可以一张不删：客户端会出现「取消」，取消或选 0 张都等于跳过。
	--    候选 = 整个主卡组，**不做任何过滤**：连 ⑤ 送的大风暴 / 天使的施舍 / 黑洞
	--    都能删，全凭玩家意愿。删卡只会让张数变少，不会把 ⑥ 整理好的禁限表搞坏。
	if not TEST_FAST_PICK and DECK_TRIM_COUNT>0 then
		for p=0,1 do
			if Duel.IsPlayerNeedToPickDeck(p) then
				local g=Duel.GetFieldGroup(p,LOCATION_DECK,0)
				local trim=DECK_TRIM_COUNT
				if trim>g:GetCount() then trim=g:GetCount() end
				if trim>0 then
					Duel.ResetTimeLimit(p,PICK_TIME_LIMIT)
					-- 卡组里的卡是表侧 token（和候选卡同一套机制），客户端能拿到卡号，直接列出选
					Duel.Hint(HINT_SELECTMSG,p,HINTMSG_SELECT)
					-- group_select 是 check_param_count(L,5)：group/p/min/max/exception，
					-- 第 5 个是 exception（nil = 不剔除任何卡）。少传会抛「5 Parameters are needed.」
					local sg=g:Select(p,0,trim,nil)
					if sg and sg:GetCount()>0 then Duel.Exile(sg,REASON_RULE) end
				end
			end
		end
	end

"""


def sub_once(text, old, new, label):
    n = text.count(old)
    assert n == 1, "%s: 锚点命中 %d 次（应为 1）" % (label, n)
    print("  [OK] %s" % label)
    return text.replace(old, new, 1)


def rep(text, old, new, label, count=1):
    n = text.count(old)
    assert n == count, "%s: 期望匹配 %d 处，实际 %d 处" % (label, count, n)
    return text.replace(old, new, count)


def main():
    with open(SRC, "r", encoding="utf-8") as f:
        s = f.read()

    # 0) 头部注释与开关
    s = HEADER + CONFIG + s

    # 1) 去掉现代固定额外卡
    s = rep(s, "local extra_fixed={62709239,95169481}",
            "local extra_fixed={} -- 408 卡池不含这两张现代卡", "extra_fixed")

    # 2) StartPick 开头每局清空额度计数 + 放宽轮抽计时
    #    ★防重入：一进函数先 e:Reset()，立刻注销 EVENT_ADJUST 全局效果。
    #    原版把 e:Reset() 放在函数最后一行，整个选卡过程（多次 yield 等玩家应答）
    #    效果一直注册着；现代内核 processor.cpp 的 PROCESSOR_ADJUST case15 会
    #    raise_event(EVENT_ADJUST)，任何一次调整都会再次调用 StartPick，
    #    interpreter.cpp 检测到同一 Lua 函数重入 -> 打 "recursive event trigger detected"
    #    并 OPERATION_FAIL，效果反而没注销 -> 下一轮又从头选卡。
    s = rep(s,
            "function Auxiliary.StartPick(e)\n\tfor p=0,1 do\n",
            "function Auxiliary.StartPick(e)\n"
            "\t-- ★防重入：先注销 EVENT_ADJUST 全局效果（原版放在末尾，选卡期间会被再次触发）\n"
            "\te:Reset()\n"
            "\tpicked_limit[0]={}\n\tpicked_limit[1]={}\n"
            "\tcombo_picked[0]=nil\n\tcombo_picked[1]=nil\n"
            "\tcombo_banned[0]={}\n\tcombo_banned[1]={}\n"
            "\t-- 轮抽期间放宽双方应答计时（服务端超时会 MSG_WIN(0x3) 直接结束决斗）\n"
            "\tDuel.ResetTimeLimit(0,PICK_TIME_LIMIT)\n"
            "\tDuel.ResetTimeLimit(1,PICK_TIME_LIMIT)\n"
            "\tfor p=0,1 do\n",
            "StartPick 清空计数 + 放宽轮抽计时 + 防重入")

    # 2b) 删掉原版末尾那次 e:Reset()（已前移到开头），留注释说明
    s = rep(s,
            "\te:Reset()\nend\n",
            "\t-- e:Reset() 已前移到 StartPick 开头（防 EVENT_ADJUST 重入）\nend\n",
            "StartPick 末尾 e:Reset 前移")

    # 3) 抽卡时检查额度：禁卡不出、限制 1、准限制 2、其余 3。
    #    候选阶段只累加 round_used（本轮用量），不消耗 picked_limit（总额度）。
    for old, new, label in [
        ("\t\t\tif not ag:IsExists(Card.IsCode,1,nil,code) and not (lv_diff and g:IsExists(Card.IsLevel,1,nil,lv)) then\n\t\t\t\tlocal card=Duel.CreateToken(p,code)\n\t\t\t\tg:AddCard(card)",
         "\t\t\tif Auxiliary.CanPick(p,code,round_used) and not ag:IsExists(Card.IsCode,1,nil,code) and not (lv_diff and g:IsExists(Card.IsLevel,1,nil,lv)) then\n\t\t\t\tround_used[code]=(round_used[code] or 0)+1\n\t\t\t\tlocal card=Duel.CreateToken(p,code)\n\t\t\t\tg:AddCard(card)",
         "SinglePick 主列表额度"),
        ("\t\t\t\tif not ag:IsExists(Card.IsCode,1,nil,code) and not (lv_diff and g:IsExists(Card.IsLevel,1,nil,lv)) then\n\t\t\t\t\tlocal card=Duel.CreateToken(p,code)\n\t\t\t\t\tg:AddCard(card)",
         "\t\t\t\tif Auxiliary.CanPick(p,code,round_used) and not ag:IsExists(Card.IsCode,1,nil,code) and not (lv_diff and g:IsExists(Card.IsLevel,1,nil,lv)) then\n\t\t\t\t\tround_used[code]=(round_used[code] or 0)+1\n\t\t\t\t\tlocal card=Duel.CreateToken(p,code)\n\t\t\t\t\tg:AddCard(card)",
         "SinglePick ex 列表额度"),
        ("\t\t\tif not ag:IsExists(Card.IsCode,1,nil,code) and not (lv_diff and pg:IsExists(Card.IsLevel,1,nil,lv)) then\n\t\t\t\tlocal card=Duel.CreateToken(p,code)\n\t\t\t\tag:AddCard(card)",
         "\t\t\tif Auxiliary.CanPick(p,code,round_used) and not ag:IsExists(Card.IsCode,1,nil,code) and not (lv_diff and pg:IsExists(Card.IsLevel,1,nil,lv)) then\n\t\t\t\tround_used[code]=(round_used[code] or 0)+1\n\t\t\t\tlocal card=Duel.CreateToken(p,code)\n\t\t\t\tag:AddCard(card)",
         "ArbitraryPick 额度"),
    ]:
        s = rep(s, old, new, label)
    assert "UNIQUE_CARDS" not in s, "UNIQUE_CARDS 残留"
    assert "picked[" not in s.replace("picked_limit[", "").replace("combo_picked[", ""), \
        "picked 表残留"

    # 3b) IsPlayerNeedToPickDeck：第一局轮抽、第二局起直接抽起始手卡
    n = s.count("Duel.IsPlayerNeedToPickDeck(p)")
    assert n >= 3, "IsPlayerNeedToPickDeck 调用点数异常: %d" % n
    print("  [OK] 保留 IsPlayerNeedToPickDeck 判断（%d 处）" % n)

    # 3e) combo 固定包：整包放行（不按额度裁剪）。
    #     408 的每个主题包都是固定 6 张，裁剪会让卡组少张数、凑不满 40；
    #     包里也不含禁卡（make_combo.py 会提示），所以直接整包给。
    m = re.search(r"(?m)^([ \t]*)if packed then\n"
                  r"[ \t]*while true do\n"
                  r"[ \t]*local thispack=math\.random\(#packed\)\n"
                  r"[ \t]*if thispack~=lastpack then\n"
                  r"[ \t]*lastpack=thispack\n"
                  r"[ \t]*for _,code in ipairs\(packed\[thispack\]\) do\n"
                  r"[ \t]*local card=Duel\.CreateToken\(p,code\)\n"
                  r"[ \t]*g:AddCard\(card\)\n"
                  r"[ \t]*ag:AddCard\(card\)\n"
                  r"[ \t]*end\n"
                  r"[ \t]*break\n"
                  r"[ \t]*end\n"
                  r"[ \t]*end\n"
                  r"[ \t]*end", s)
    assert m, "找不到 combo 固定包代码"
    ind = m.group(1)
    new_packed = (ind + "if packed and #packed>0 then\n"
                  + ind + "\tlocal thispack=Auxiliary.PickComboPack(p,packed,lastpack,exclude,combo_banned[p])\n"
                  + ind + "\tif thispack then\n"
                  + ind + "\t\tlastpack=thispack\n"
                  + ind + "\t\tpack_of[g]=thispack\n"
                  + ind + "\t\tfor _,code in ipairs(packed[thispack]) do\n"
                  + ind + "\t\t\tround_used[code]=(round_used[code] or 0)+1\n"
                  + ind + "\t\t\tlocal card=Duel.CreateToken(p,code)\n"
                  + ind + "\t\t\tg:AddCard(card)\n"
                  + ind + "\t\t\tag:AddCard(card)\n"
                  + ind + "\t\tend\n"
                  + ind + "\tend\n"
                  + ind + "end")
    s = s.replace(m.group(0), new_packed, 1)
    print("  [OK] combo 包：整包放行（避开 lastpack / exclude / 本轮拉黑）")

    # 3e2) 一张候选都没生成（例如 combo 全部不可用）时直接返回，
    #      避免对空组做 SelectUnselect / ResetTimeLimit 后卡住。
    s = sub_once(s,
                 "\t\tDuel.SendtoDeck(g,nil,0,REASON_RULE)\n\tend\n\tDuel.ResetTimeLimit(p,90)\n",
                 "\t\tDuel.SendtoDeck(g,nil,0,REASON_RULE)\n\tend\n"
                 "\tif ag:GetCount()==0 then return false end\n"
                 "\tDuel.ResetTimeLimit(p,90)\n",
                 "3e2 空候选保护")

    # 3f) 选完后的「确认卡片」：官方是 ConfirmCards(p,tg)（只发选中的 3~4 张）。
    #     CONFIRM_MODE=0（默认）：整条消息都不发 —— 选卡界面已经展示过，再弹一次纯属重复。
    #     CONFIRM_MODE=1/2：需要决斗日志里的「公开 卡名」时用（这是唯一带卡名的消息）。
    #     MDPro3 只有一次确认 >=4 张时才弹列表（DuelMessage.GameMessage_ConfirmCards:
    #     `if (count > 3 && condition == Duel) listShow = true;`），<=3 张会逐张播 3D 翻卡
    #     动画，2pick 的卡是牌堆上的临时卡、没有实体模型，会卡住界面 —— 所以 1 也做兜底。
    #     另外额外轮的卡在 LOCATION_EXTRA（不是 DECK），判位置要两个都认。
    #
    #     ArbitraryPick 里 `rg=ag; rg:Sub(tg)` 会把 ag 改小成「没选中的那几张」，
    #     所以先留一份整批快照 all。
    s = sub_once(s,
                 "\tlocal tg=ag:Select(p,count,count,nil)\n\tlocal rg=ag\n",
                 "\tlocal tg=ag:Select(p,count,count,nil)\n"
                 "\tlocal all=Group.CreateGroup()   -- 3f: 整批候选快照（下面 rg:Sub(tg) 会把 ag 改小）\n"
                 "\tfor c in aux.Next(ag) do all:AddCard(c) end\n"
                 "\tlocal rg=ag\n",
                 "3f 整批候选快照")

    _confirm_old = ("\tif tg:GetFirst():IsLocation(LOCATION_DECK) then\n"
                    "\t\tDuel.ConfirmCards(p,tg)\n"
                    "\tend")
    _parts = s.split(_confirm_old)
    assert len(_parts) == 3, "确认卡片锚点应为 2 处，实际 %d" % (len(_parts) - 1)
    _confirm_new = ("\t-- 3f 确认卡片（CONFIRM_MODE=0 不发；=1 只发本轮选中，<4 张改发整批以走列表弹窗）\n"
                    "\t--    官方 MSG_CONFIRM_CARDS 只在「第一张卡在 LOCATION_DECK」时单播本人，\n"
                    "\t--    其它位置连对手/观战一起发；patch_pick_chat.py 已把 LOCATION_EXTRA\n"
                    "\t--    也改成单播，所以额外轮同样能弹窗，且客户端会 SetCode 补上卡号。\n"
                    "\tlocal canConfirm=(tg:GetCount()>0)\n"
                    "\tfor c in aux.Next(tg) do\n"
                    "\t\tif not c:IsLocation(LOCATION_DECK) and not (CONFIRM_EXTRA and c:IsLocation(LOCATION_EXTRA)) then canConfirm=false break end\n"
                    "\tend\n"
                    "\tif CONFIRM_MODE>0 and canConfirm then\n"
                    "\t\tif tg:GetCount()<4 then Duel.ConfirmCards(p,%s) else Duel.ConfirmCards(p,tg) end\n"
                    "\tend\n"
                    "\t-- 3f2 私聊选卡记录（ENABLE_PICK_LOG）：卡号交内核，服务端查 cardnames.txt\n"
                    "\t--     拼一行「【轮抽】本轮选卡：…」只发给本人；Duel.PickLog 不存在时跳过。\n"
                    "\tif ENABLE_PICK_LOG and Duel.PickLog then Duel.PickLog(p,tg) end")
    s = (_parts[0] + _confirm_new % "ag"
         + _parts[1] + _confirm_new % "all"
         + _parts[2])
    print("  [OK] 3f 确认卡片：CONFIRM_MODE=1 只发本人（CONFIRM_EXTRA 控制额外轮）")
    print("  [OK] 3f2 私聊选卡记录：ENABLE_PICK_LOG -> Duel.PickLog（需内核补丁）")

    # 3g) 删掉「选完再确认一遍剩余卡片」的 SelectUnselect 循环（只删 ArbitraryPick 里的那个）。
    #     官方设计：选满 count 张后，把剩下的候选再列一遍，允许玩家点回来换卡。
    #     MDPro3 的实现是「一次点一张、点了就并进已选组」：
    #       DuelMessage.GameMessage_SelectUnselect -> ShowPopupSelectCard(...,1,1,cancelable,finishable)
    #       PopupDuelSelectCard -> 确认时写 (count, 各下标)，点一张就加一张；
    #     已选数一超过上限，下一轮 finish=tg:GetCount()==count 变 false，
    #     cancelable 也跟着变 false（exitable=false），取消按钮/ESC 全部失效 —— 无解，只能卡死。
    #     换卡需求在第一次 Select 弹窗里就能做（选多了再点一下取消选中），所以整段删掉。
    _sel_loop = ("\twhile true do\n"
                 "\t\tlocal finish=tg:GetCount()==count\n"
                 "\t\tDuel.Hint(HINT_SELECTMSG,p,HINTMSG_TODECK)\n"
                 "\t\tlocal sc=rg:SelectUnselect(tg,p,finish,false,count,count)\n"
                 "\t\tif not sc then break end\n"
                 "\t\tif tg:IsContains(sc) then\n"
                 "\t\t\ttg:RemoveCard(sc)\n"
                 "\t\t\trg:AddCard(sc)\n"
                 "\t\telse\n"
                 "\t\t\ttg:AddCard(sc)\n"
                 "\t\t\trg:RemoveCard(sc)\n"
                 "\t\tend\n"
                 "\tend\n")
    s = sub_once(s, _sel_loop,
                 "\t-- 3g 官方这里还有一段 SelectUnselect「选完再换卡」的循环，已删除：\n"
                 "\t--    MDPro3 的弹窗一次只能点 1 张、点了就并进已选组，超过上限后\n"
                 "\t--    finishable/cancelable 都变 false（取消按钮失效），必定卡死在「剩余卡片」界面。\n"
                 "\t--    要换卡请在下面这次 Select 里操作（多选了就再点一下取消选中）。\n",
                 "3g 去掉「剩余卡片」二次确认")

    # 3g2) combo 选包（SinglePick）：原版把两组 12 张一起列，点任意一张代表选中该组。
    #      问题：弹窗顺序被内核 card_operation_sort 重排（怪兽->魔法->陷阱、等级、卡号），
    #      两组交叉看不出分组；点完还会再弹一次「剩下的那组」，MDPro3 里很迷惑。
    #      改法：一组一弹窗 —— 确认=选它，取消=换另一组，可来回比较。
    #      兜底：连翻 6 次都取消就默认拿第一组，任何一条路都走得完，不会卡死。
    _combo_old = ("\tlocal tg=Group.CreateGroup()\n"
                  "\tlocal rg=ag\n"
                  "\twhile true do\n"
                  "\t\tlocal finish=tg:GetCount()>0\n"
                  "\t\tDuel.Hint(HINT_SELECTMSG,p,HINTMSG_TODECK)\n"
                  "\t\tlocal sc=rg:SelectUnselect(tg,p,finish,false,#g1,#g2)\n"
                  "\t\tif not sc then break end\n"
                  "\t\ttg=g1:IsContains(sc) and g1 or g2\n"
                  "\t\trg=g1:IsContains(sc) and g2 or g1\n"
                  "\tend\n")
    _combo_new = ("\tlocal tg=Group.CreateGroup()\n"
                  "\tlocal rg=ag\n"
                  "\t-- 3g2 combo 选包：一组一弹窗。原版 12 张一起列，但弹窗顺序被内核\n"
                  "\t--   card_operation_sort 按卡种重排，两组交叉看不出分组；\n"
                  "\t--   改成一次只列一组：确认=选它，取消=换另一组。\n"
                  "\tlocal chosen=nil\n"
                  "\tlocal cur=g1\n"
                  "\tfor _=1,6 do\n"
                  "\t\tif cur:GetCount()==0 then break end\n"
                  "\t\tDuel.Hint(HINT_SELECTMSG,p,HINTMSG_SELECT)\n"
                  "\t\tlocal sc=cur:SelectUnselect(Group.CreateGroup(),p,false,true,1,1)\n"
                  "\t\tif sc then chosen=cur break end\n"
                  "\t\tcur=(cur==g1) and g2 or g1\n"
                  "\tend\n"
                  "\tif not chosen then chosen=(g1:GetCount()>0) and g1 or g2 end\n"
                  "\ttg=chosen\n"
                  "\trg=(tg==g1) and g2 or g1\n"
                  "\t-- 记录「这两组里玩家真正拿走的那一组」是第几个包（3j 用）\n"
                  "\tif packed and #packed>0 then picked_pack=pack_of[tg] end\n")
    s = sub_once(s, _combo_old, _combo_new, "3g2 combo 选包一组一弹窗")

    # 3k) 主题包「除外」：把刚看过的两组记进本轮拉黑表，重选不再随机到它们。
    #     combo_banned[p] 由 ②/④ 开头清空，所以只在本轮生效；跨轮永久避开的是
    #     combo_picked[p]（3j 记录，④ 作为 exclude 传进来）。
    s = sub_once(s,
                 "\tif optional and ag:GetFirst():IsLocation(LOCATION_DECK) then\n"
                 "\t\tDuel.ConfirmCards(p,ag)\n"
                 "\t\tif Duel.SelectOption(p,1190,1192)==1 then\n"
                 "\t\t\tDuel.Exile(ag,REASON_RULE)\n"
                 "\t\t\treturn false\n"
                 "\t\tend\n"
                 "\tend\n",
                 "\tif optional and ag:GetFirst():IsLocation(LOCATION_DECK) then\n"
                 "\t\tDuel.ConfirmCards(p,ag)\n"
                 "\t\tif Duel.SelectOption(p,1190,1192)==1 then\n"
                 "\t\t\t-- 3k: 两组都不要 -> 本轮拉黑，重选不再随机到它们\n"
                 "\t\t\tfor _,g in ipairs({g1,g2}) do\n"
                 "\t\t\t\tlocal pk=pack_of[g]\n"
                 "\t\t\t\tif pk then combo_banned[p][pk]=true end\n"
                 "\t\t\tend\n"
                 "\t\t\tDuel.Exile(ag,REASON_RULE)\n"
                 "\t\t\treturn false\n"
                 "\t\tend\n"
                 "\tend\n",
                 "3k 主题包除外本轮拉黑")

    # 4d) 额外轮候选直接进额外卡组（官方行为，不改）。
    #     ⚠ 候选阶段绝不能用 Duel.Exile：它把卡移到 location 0，客户端建不出卡对象，
    #       紧接着的 SELECT_CARD 会全空并刷 "Not found card for SelectCard"。
    #       Exile 只用于「选完之后清掉未选卡」（见 4e）。

    # 3e3) combo 专用卡不进普通轮抽池（2pick/exclusive.lua）
    s = sub_once(s,
                 "\t\tif (cat & TYPE_FUSION+TYPE_SYNCHRO+TYPE_XYZ+TYPE_LINK)>0 then\n",
                 "\t\tif exclusive[code] then\n"
                 "\t\t\t-- 408：combo 专用卡（如被封印的艾克佐迪亚 5 件套），不进普通池\n"
                 "\t\telseif (cat & TYPE_FUSION+TYPE_SYNCHRO+TYPE_XYZ+TYPE_LINK)>0 then\n",
                 "3e3 exclusive 卡排除普通池")

    # 3f) copy 复制出来的卡也计入额度；额度不足（如限 1 卡）时干脆不复制，
    #     否则「选到 1 张限制卡 → 复制成 2 张」会直接超限。
    copy_pat = re.compile(r"(?m)^([ \t]*)for nc in aux\.Next\(tg\) do\n"
                          r"[ \t]*local copy_code=nc:GetOriginalCode\(\)\n"
                          r"[ \t]*g3:AddCard\(Duel\.CreateToken\(p,copy_code\)\)\n"
                          r"[ \t]*end")

    def _copy_repl(m):
        ind = m.group(1)
        # 复制无条件进行：额度只约束「候选/选中」，复制是凑张数用的。
        # 若这里再查一次 CanPick，抽到第 3 张同卡时复制会被拒 → 卡组 43 张 → 最终 39 张。
        return (ind + "for nc in aux.Next(tg) do\n"
                + ind + "\tlocal copy_code=nc:GetOriginalCode()\n"
                + ind + "\tAuxiliary.MarkPick(p,copy_code)\n"
                + ind + "\tg3:AddCard(Duel.CreateToken(p,copy_code))\n"
                + ind + "end")

    s, k = copy_pat.subn(_copy_repl, s)
    assert k == 2, "copy 复制段替换点数异常: %d" % k

    # 3g) 两个抽卡函数各加本轮临时计数表
    s = sub_once(s,
        "function Auxiliary.SinglePick(p,list,count,ex_list,ex_count,copy,lv_diff,fixed,packed,optional)\n\tif not Duel.IsPlayerNeedToPickDeck(p) then return end\n",
        "function Auxiliary.SinglePick(p,list,count,ex_list,ex_count,copy,lv_diff,fixed,packed,optional,exclude)\n\tif not Duel.IsPlayerNeedToPickDeck(p) then return end\n\tlocal round_used={}\n\tlocal picked_pack=nil\n\tlocal pack_of={}\n",
        "3g SinglePick 本轮计数表 + exclude 参数")
    s = sub_once(s,
        "function Auxiliary.ArbitraryPick(p,count,pick_lists,lists_count,copy,lv_diff,fixed)\n\tif not Duel.IsPlayerNeedToPickDeck(p) then return end\n",
        "function Auxiliary.ArbitraryPick(p,count,pick_lists,lists_count,copy,lv_diff,fixed)\n\tif not Duel.IsPlayerNeedToPickDeck(p) then return end\n\tlocal round_used={}\n",
        "3g ArbitraryPick 本轮计数表")

    # 3h) 只有玩家最终选中的卡才计入额度（SinglePick / ArbitraryPick 各一处）
    old_tg = "\tDuel.Exile(rg,REASON_RULE)\n\tif copy then"
    new_tg = ("\tDuel.Exile(rg,REASON_RULE)\n"
              "\tfor nc in aux.Next(tg) do\n"
              "\t\tAuxiliary.MarkPick(p,nc:GetOriginalCode())\n"
              "\tend\n\tif copy then")
    n = s.count(old_tg)
    assert n == 2, "3h 选中后计数锚点命中 %d 次（应为 2）" % n
    s = s.replace(old_tg, new_tg, 2)
    print("  [OK] 3h 选中后计数（2 处）")

    # 3j) 记录「第一次 combo 最终拿到的包」，第二次 combo 用 exclude 避开它。
    #     只在真正选完（return true）时写，重选被取消的那些包不算。
    s = sub_once(s,
                 "\t\tDuel.SendtoDeck(g3,nil,0,REASON_RULE)\n\tend\n\treturn true\nend\n",
                 "\t\tDuel.SendtoDeck(g3,nil,0,REASON_RULE)\n\tend\n"
                 "\t-- 3j: 记录本次 combo 拿到的包（第二次 combo 要避开它）\n"
                 "\tif picked_pack then combo_picked[p]=picked_pack end\n"
                 "\treturn true\nend\n",
                 "3j 记录第一次 combo 的包")

    # 3i) Duel.ReadCard(code,CARDDATA_LEVEL) -> 本地 levels.lua
    #     现代 mycard 内核没有 ReadCard / AnnounceCardFilter，直接调用会在
    #     第一次抽卡时 "attempt to call a nil value"。
    n = s.count("Duel.ReadCard(code,CARDDATA_LEVEL)")
    assert n == 3, "Duel.ReadCard 调用点数异常: %d" % n
    s = s.replace("Duel.ReadCard(code,CARDDATA_LEVEL)", "Auxiliary.CardLevel(code)")
    print("  [OK] ReadCard -> levels.lua（%d 处）" % n)

    # 4) 重建整个轮抽序列（408 定制）：
    #    额外 2 轮 -> combo ① -> 主卡组 MAIN_ROUNDS 轮 -> combo ② -> 补 3 张
    #    -> 同名裁剪 -> 自由删卡
    #    原版是「主卡组 5 轮 -> combo 1 次 -> 同调/超量/连接轮」，这里整段替换，
    #    免得多次小改互相踩锚点（旧版第 4/5/6 步就是这么叠出来的）。
    old_extra_start = "\tfor tp,list in pairs(extra_sp) do\n\t\tif tp~=TYPE_FUSION then\n"
    assert s.count(old_extra_start) == 1, "extra_sp 循环锚点不唯一（LoadDB 里也有类似行）"
    i = s.index(old_extra_start)
    j = s.index("\t-- -- XXYYZZ Additional Picks", i)
    s = s[:i] + s[j:]                       # 删掉原版放在 combo 之后的额外轮

    seq_start = "\tfor i=1,5 do\n\t\tlocal lists={[1]=main_monster,[3]=main_spell,[4]=main_trap}\n"
    assert s.count(seq_start) == 1, "主卡组循环锚点不唯一"
    a = s.index(seq_start)
    b = s.index("\t-- -- XXYYZZ Additional Picks", a)
    s = s[:a] + NEW_SEQUENCE + s[b:]
    print("  [OK] 顺序：额外 2 轮 -> combo① -> 主卡组 %d 轮 -> combo② -> 补 3 张 -> 同名裁剪 -> 自由删卡" % 4)

    # 7) 关掉技能（保留文件与开关）
    s = rep(s,
            "\t--Skill Deepthink\n\tDeepthink.Load_Skill_Deepthink_Rule()",
            "\t--Skill Deepthink（408 默认关闭）\n\tif ENABLE_DEEPTHINK then Deepthink.Load_Skill_Deepthink_Rule() end",
            "Deepthink 开关")

    # 4b) 仪式配对：候选里出现仪式怪兽/仪式魔法时，把另一半补进同一批候选
    s = sub_once(s,
                 "\t\tend\n\tend\n\tif fixed then\n",
                 "\t\tend\n\tend\n"
                 "\t-- 仪式配对：仪式怪兽带着它的仪式魔法一起出现在候选里（反之亦然）\n"
                 "\tAuxiliary.AddRitualPartner(p,ag)\n"
                 "\tif fixed then\n",
                 "4b 仪式配对补位")

    # 4c) 开局抽卡按实际牌堆张数收敛：复制被额度拒掉时主卡组可能只有 39 张，
    #     牌堆比 start_count 少时少抽，绝不抽空判负。
    s = sub_once(s,
                 "\tfor p=0,1 do\n\t\tDuel.Draw(p,Duel.GetStartCount(p),REASON_RULE)\n\tend\n",
                 "\t-- 主卡组可能不足 40 张（复制被额度拒掉时不补位），这里按实际张数收敛：\n"
                 "\t-- 牌堆 >= start_count 就照常抽 5 张，不够就只抽现有的，避免抽空判负。\n"
                 "\tfor p=0,1 do\n"
                 "\t\tlocal n=Duel.GetStartCount(p)\n"
                 "\t\tlocal avail=Duel.GetFieldGroupCount(p,LOCATION_DECK,0)\n"
                 "\t\tif n>avail then n=avail end\n"
                 "\t\tif n>0 then Duel.Draw(p,n,REASON_RULE) end\n"
                 "\tend\n",
                 "4c 开局抽卡收敛")

    # 4e) 收尾：Duel.Exile 保留官方语义 —— 只在「选完之后」清未选卡。
    #     候选阶段绝不能用 Exile（卡落到 location 0，客户端建不出卡对象，
    #     SELECT_CARD 全空 + "Not found card for SelectCard"）。
    #     Exile 的 POS_FACEUP -> POS_FACEDOWN 由 port_2pick.py 改内核：
    #     服务端 MSG_MOVE 只在移动后是里侧（或目标是卡组/手牌）时隐藏卡号，
    #     表侧 Exile 会把没选中的卡名泄露给对手。
    _n_exile = len(re.findall(r"Duel\.Exile\((\w+),REASON_RULE\)", s))
    assert _n_exile >= 4, "Duel.Exile 清理调用点异常：%d 处（应 >=4）" % _n_exile
    assert "Duel.Remove(ag,POS_FACEDOWN,REASON_RULE)" not in s, "候选阶段仍有里侧除外"
    print("  [OK] 4e Duel.Exile 清理 %d 处，候选阶段无 Exile/Remove" % _n_exile)

    # 4f) SaveDeck 只写回主卡组 + 额外卡组，且仅轮抽局写回。
    #     官方原版用 Duel.Exile 把没选中的卡移出场地，所以 0xff 收不到它们；
    #     这里改为内核直接按引擎卡组实际顺序（list_main 顶到低 + list_extra）写回。
    s = sub_once(s,
                 "\t\tlocal g=Duel.GetFieldGroup(p,0xff,0)\n"
                 "\t\tDuel.SavePickDeck(p,g)\n",
                 "\t\t-- 4f: 仅本局现场轮抽（pick_needed=true）才写回：M# 第二/三局不轮抽，\n"
                 "\t\t--     再 SaveDeck 会重复触发服务端 MSG_SAVE_PICK_DECK -> REPLAY_CODE，\n"
                 "\t\t--     把本局 replay 截断重建 + 就地再切洗 pick_deck，损坏录像（2026-11-12）。\n"
                 "\t\t--     内核按引擎卡组实际顺序写回，不再传 GetFieldGroup 的 group（它的\n"
                 "\t\t--     container 是 card_set，按 cardid 排序会丢 ShuffleDeck 后的真实牌序）。\n"
                 "\t\tif Duel.IsPlayerNeedToPickDeck(p) then\n"
                 "\t\t\tDuel.SavePickDeck(p)\n"
                 "\t\tend\n",
                 "4f SaveDeck 仅轮抽局写回（内核按引擎真实牌序）")

    # 4g) 先 ShuffleDeck 再 SaveDeck：让 MSG_SAVE_PICK_DECK 记录的是引擎切洗后的
    #     真实牌序，录像头卡组顺序才对得上引擎，否则录像「开局卡组是选卡顺序/对不上」
    #     无法播放（2026-11-12）。
    s = sub_once(s,
                 "\tAuxiliary.SaveDeck()\n"
                 "\t--ActionDuel.Load_Action_Duel()\n"
                 "\t\n"
                 "\tfor p=0,1 do\n"
                 "\t\tif Duel.IsPlayerNeedToPickDeck(p) then\n"
                 "\t\t\tDuel.ShuffleDeck(p)\n"
                 "\t\t\tDuel.ResetTimeLimit(p)\n"
                 "\t\tend\n"
                 "\tend\n",
                 "\t--ActionDuel.Load_Action_Duel()\n"
                 "\t\n"
                 "\tfor p=0,1 do\n"
                 "\t\tif Duel.IsPlayerNeedToPickDeck(p) then\n"
                 "\t\t\tDuel.ShuffleDeck(p)\n"
                 "\t\t\tDuel.ResetTimeLimit(p)\n"
                 "\t\tend\n"
                 "\tend\n"
                 "\t-- 先切洗再 SaveDeck：MSG_SAVE_PICK_DECK 记录的是引擎切洗后的真实牌序，\n"
                 "\t-- 录像头卡组顺序才对得上引擎（2026-11-12，修「录像无法播放」）。\n"
                 "\tAuxiliary.SaveDeck()\n",
                 "4g SaveDeck 挪到 ShuffleDeck 之后")

    # 15) 轮抽阶段所有「每轮重置时限」统一改成 PICK_TIME_LIMIT。
    #     官方 ArbitraryPick 是 120 秒、SinglePick(combo) 是 90 秒，慢选会超时断线。
    #     注意末尾 Duel.ResetTimeLimit(p)（不带秒数）要保留：它恢复房间原始时限。
    s, n_time = re.subn(r"Duel\.ResetTimeLimit\(p,(?:90|120)\)",
                        "Duel.ResetTimeLimit(p,PICK_TIME_LIMIT)", s)
    assert n_time >= 2, "轮抽计时锚点异常：只改到 %d 处 ResetTimeLimit(p,90/120)" % n_time
    print("  [OK] 15 轮抽每轮时限 -> PICK_TIME_LIMIT（%d 处）" % n_time)

    # 16) 删掉官方预留、整段注释掉的超量/连接函数（408 用不到，约占三分之一篇幅）
    _dead = re.search(r"(?m)^-- function Auxiliary\.Load_EVENT_ExtraFusion\(\)", s)
    if _dead:
        _tail = s[_dead.start():]
        _live = [ln for ln in _tail.splitlines() if ln.strip() and not ln.lstrip().startswith("--")]
        assert not _live, "死代码段之后仍有有效代码，不能整段删除：%s" % _live[:3]
        s = s[:_dead.start()].rstrip() + "\n"
        print("  [OK] 16 删除整段注释掉的超量/连接死代码（%d 行）" % len(_tail.splitlines()))

    out_dir = os.path.dirname(DST)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(DST, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)
    print("[pick.lua] 输出 %s（%d 字节）" % (DST, len(s.encode("utf-8"))))


if __name__ == "__main__":
    main()
