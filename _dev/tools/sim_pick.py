#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用假 Duel/Group 接口在 Lua 里真跑一遍 2pick/pick.lua，做整局轮抽模拟。

不是重写一遍逻辑，而是 dofile 真实的 pick.lua，只把核心 API 换成假的：
  Duel.CreateToken / SendtoDeck / Exile / SavePickDeck / Draw / SelectOption ...
  Group 的 AddCard / IsExists / Select / SelectUnselect ...
这样验的是「服务器真正会跑的代码」，改完规则跑一遍就知道对不对。

跑完会打印：
  * 每种轮次的候选张数（额外轮是不是 8 张候选选 4 张、共 2 轮）
  * 主卡组 / 额外卡组张数分布
  * 仪式配对是否 100% 同时出现在候选里
  * combo 包是否按额度裁剪、没有超额
  * 同卡号最终张数是否都守禁限表

用法：
    python sim_pick.py                # 300 局
    python sim_pick.py -n 1000
    python sim_pick.py --seed 42
    python sim_pick.py --force-exile   # 每次都点「除外」，验证本轮拉黑 + 跨轮恢复
"""
import argparse
import collections
import io
import os
import re
import sqlite3
import sys

import lupa

# 脚本位于 _dev/tools/，包根在两级之上；所有数据路径都以包根为基准
HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEV = os.path.join(HERE, "_dev")
PICK_DIR = os.path.join(HERE, "2pick")
CDB = os.path.join(PICK_DIR, "408.cdb")

MOCK = r"""
-- ===== 假核心：只实现 pick.lua 用到的那点 API =====
Auxiliary = Auxiliary or {}
LOCATION_DECK   = 1
LOCATION_HAND   = 2
LOCATION_EXTRA  = 0x40
LOCATION_ONFIELD= 4
LOCATION_REMOVED= 0x20
POS_FACEUP      = 5
POS_FACEDOWN    = 0xa
REASON_RULE     = 0
TYPE_MONSTER    = 0x1
TYPE_SPELL      = 0x2
TYPE_TRAP       = 0x4
TYPE_TOKEN      = 0x4000
TYPE_FUSION     = 0x40
TYPE_SYNCHRO    = 0x2000
TYPE_XYZ        = 0x800000
TYPE_LINK       = 0x4000000
HINT_SELECTMSG  = 0
HINTMSG_TODECK  = 1
HINTMSG_SELECT  = 560
EFFECT_TYPE_FIELD      = 1
EFFECT_TYPE_CONTINUOUS = 2
EVENT_ADJUST           = 2
EFFECT_FLAG_IGNORE_IMMUNE = 4

PAIR_LOG = {}

local card_mt = {}
card_mt.__index = card_mt
function card_mt:GetOriginalCode() return self.code end
function card_mt:GetCode() return self.code end
function card_mt:IsLocation(loc) return self.loc == loc end
function card_mt:IsCode(c) return self.code == c end
function card_mt:IsLevel(lv) return self.level == lv end
function card_mt:IsType(t) return (self.ctype & t) ~= 0 end

local function new_card(p, code)
	return setmetatable({code = code, owner = p, loc = LOCATION_DECK,
		level = levels[code] or 0, ctype = TYPES[code] or 0}, card_mt)
end

Card = setmetatable({}, {__index = function(_, k) return card_mt[k] end})
Card.IsCode  = function(c, code) return c.code == code end
Card.IsLevel = function(c, lv) return c.level == lv end
Card.IsType  = function(c, t) return (c.ctype & t) ~= 0 end

local group_mt = {}
group_mt.__index = group_mt
Group = {}
function Group.CreateGroup() return setmetatable({cards = {}}, group_mt) end
function Group.FromCards(...) local g = Group.CreateGroup() for _, c in ipairs({...}) do g:AddCard(c) end return g end
function group_mt:AddCard(c) table.insert(self.cards, c) end
function group_mt:RemoveCard(c)
	for i, v in ipairs(self.cards) do if v == c then table.remove(self.cards, i) return end end
end
function group_mt:GetCount() return #self.cards end
function group_mt:GetFirst() return self.cards[1] end
function group_mt:IsContains(c)
	for _, v in ipairs(self.cards) do if v == c then return true end end
	return false
end
function group_mt:IsExists(f, cnt, excl, ...)
	local n = 0
	for _, c in ipairs(self.cards) do
		if c ~= excl and f(c, ...) then n = n + 1 end
	end
	return n >= cnt, n
end
function group_mt:Sub(other)
	local rm = {}
	for _, c in ipairs(other.cards) do rm[c] = true end
	local keep = {}
	for _, c in ipairs(self.cards) do if not rm[c] then keep[#keep + 1] = c end end
	self.cards = keep
end
-- 真实内核的 Group.Filter(f, ...)：返回包含所有 satisfy 的卡的新 group
-- 内核要求至少 3 个参数（group + func + 附加参数），少一个直接抛
-- 「3 Parameters are needed.」并中断整个 StartPick。这里照抄这个校验，
-- 免得本地模拟全绿、上服务器才炸。（2026-11-11 真出过这一次）
function group_mt:Filter(f, ...)
	if select('#', ...) < 1 then
		error("3 Parameters are needed.", 2)
	end
	local g = Group.CreateGroup()
	for _, c in ipairs(self.cards) do
		if f(c, ...) then g:AddCard(c) end
	end
	return g
end
function group_mt:Merge(other)
	for _, c in ipairs(other.cards) do self:AddCard(c) end
end
-- 玩家「随机选 count 张」
-- 真实内核 Group:Select(playerid, min, max, exception)（libgroup.cpp group_select）：
--   check_param_count(L, 5) —— 含 self 必须传满 5 个参数，第 5 个是 exception
--   （一张卡或一个 group，从候选里剔除；传 nil = 不剔除）。
--   Select **没有** filter 能力，要过滤就用 FilterSelect(playerid, func, min, max, exception)。
--   少传 exception 会抛「5 Parameters are needed.」，和 Filter 一样会中断 StartPick。
function group_mt:Select(...)
	local p, minc, maxc, exception = ...
	if select('#', ...) < 4 then
		error("5 Parameters are needed.", 2)
	end
	local cards = self.cards
	if exception then
		local rm = {}
		if exception.cards then
			for _, c in ipairs(exception.cards) do rm[c] = true end
		else
			rm[exception] = true
		end
		local keep = {}
		for _, c in ipairs(cards) do if not rm[c] then keep[#keep + 1] = c end end
		cards = keep
	end
	local n = #cards
	local want = maxc or minc
	if want > n then want = n end
	local idx, picked = {}, {}
	for i = 1, n do idx[i] = i end
	for i = 1, want do
		local j = math.random(i, n)
		idx[i], idx[j] = idx[j], idx[i]
		picked[#picked + 1] = cards[idx[i]]
	end
	local g = Group.CreateGroup()
	for _, c in ipairs(picked) do g:AddCard(c) end
	return g
end
-- 玩家操作：tg 为空时点第一张（= 选中它所在的那一组），否则确认
function group_mt:SelectUnselect(tg, p, finish, cancelable, minc, maxc)
	if self:GetCount() == 0 then return nil end
	if cancelable and math.random() < 0.3 then return nil end
	return self:GetFirst()
end

aux = {}
function aux.Next(g) return coroutine.wrap(function()
	for _, c in ipairs(g.cards) do coroutine.yield(c) end
end) end
aux.TRUE = function() return true end

local effect_mt = {}
effect_mt.__index = effect_mt
for _, k in ipairs({"SetType", "SetCode", "SetProperty", "SetTargetRange", "SetOperation",
	"SetCondition", "SetTarget", "SetValue", "SetReset", "SetCountLimit", "SetLabel"}) do
	effect_mt[k] = function(self) return self end
end
function effect_mt:Reset() end
Effect = {GlobalEffect = function() return setmetatable({}, effect_mt) end}
function effect_mt:Clone() return setmetatable({}, effect_mt) end

-- ===== 每个玩家的状态 =====
DECK = {[0] = {}, [1] = {}}
EXTRA = {[0] = {}, [1] = {}}
HAND = {[0] = {}, [1] = {}}
REMOVED = {[0] = {}, [1] = {}}   -- Duel.Remove 的卡仍留在除外区（真实内核行为）
SAVED = {[0] = {}, [1] = {}}
DRAWN = {[0] = 0, [1] = 0}
PICKLOG = {[0] = 0, [1] = 0}   -- Duel.PickLog 被调用次数（私聊选卡记录）
TOKENS = {[0] = {}, [1] = {}}

local function is_extra_type(t)
	return (t & (TYPE_FUSION | TYPE_SYNCHRO | TYPE_XYZ | TYPE_LINK)) ~= 0
end

local function remove_from_all(p, c)
	local function rm(list)
		for i, v in ipairs(list) do if v == c then table.remove(list, i) return true end end
		return false
	end
	local ok = rm(DECK[p])
	if not ok then ok = rm(EXTRA[p]) end
	if not ok then ok = rm(HAND[p]) end
	if not ok then rm(REMOVED[p]) end
end

Duel = {}
function Duel.CreateToken(p, code)
	local c = new_card(p, code)
	TOKENS[p][#TOKENS[p] + 1] = c
	return c
end
function Duel.SendtoDeck(g, who, seq, reason)
	for _, c in ipairs(g.cards) do
		remove_from_all(c.owner, c)   -- 额外轮：卡从除外区回额外卡组，不能两边都留
		if is_extra_type(c.ctype) then
			c.loc = LOCATION_EXTRA
			EXTRA[c.owner][#EXTRA[c.owner] + 1] = c
		else
			c.loc = LOCATION_DECK
			DECK[c.owner][#DECK[c.owner] + 1] = c
		end
	end
end
function Duel.Exile(g, reason)
	-- 真实内核（send_to PLAYER_NONE, location=0）：卡从所有区域摘掉，
	-- location=0 不属于任何区域，GetFieldGroup 收不到、也不参与决斗。
	-- 这正是「清掉未选卡」该用的 API，不会像 Remove 那样留在除外区。
	for _, c in ipairs(g.cards) do remove_from_all(c.owner, c) c.loc = 0 end
end
function Duel.Remove(g, pos, reason)
	-- 真实内核：卡移到 LOCATION_REMOVED，仍然存在（GetFieldGroup 0xff 能收到）
	for _, c in ipairs(g.cards) do
		remove_from_all(c.owner, c)
		c.loc = LOCATION_REMOVED
		REMOVED[c.owner][#REMOVED[c.owner] + 1] = c
	end
	return #g.cards
end
function Duel.GetFieldGroup(p, loc, seq)
	local g = Group.CreateGroup()
	if (loc & LOCATION_DECK) ~= 0 then
		for _, c in ipairs(DECK[p]) do g:AddCard(c) end
	end
	if (loc & LOCATION_EXTRA) ~= 0 then
		for _, c in ipairs(EXTRA[p]) do g:AddCard(c) end
	end
	if (loc & LOCATION_HAND) ~= 0 then
		for _, c in ipairs(HAND[p]) do g:AddCard(c) end
	end
	if (loc & LOCATION_REMOVED) ~= 0 then
		for _, c in ipairs(REMOVED[p]) do g:AddCard(c) end
	end
	return g
end
function Duel.GetFieldGroupCount(p, loc, seq) return #DECK[p] end
function Duel.SavePickDeck(p)
	-- 内核（port_2pick.py v3.8 起）按引擎卡组实际顺序写回，不再传 group；
	-- 模拟器直接用当前 DECK[p] 顺序，SAVED 的校验语义保持不变。
	local t = {}
	for _, c in ipairs(DECK[p]) do t[#t + 1] = c.code end
	SAVED[p] = t
end
function Duel.Draw(p, n)
	local took = 0
	for i = 1, n do
		local c = table.remove(DECK[p], 1)
		if not c then break end
		c.loc = LOCATION_HAND
		HAND[p][#HAND[p] + 1] = c
		took = took + 1
	end
	DRAWN[p] = DRAWN[p] + took
	return took
end
function Duel.GetStartCount(p) return 5 end
function Duel.ShuffleDeck(p) end
function Duel.ResetTimeLimit(p, t) end
function Duel.Hint(...) end
function Duel.ConfirmCards(a, b) end
function Duel.PickLog(p, g) PICKLOG[p] = PICKLOG[p] + 1 end   -- 内核 patch_pick_chat.py
function Duel.SelectOption(p, ...) return FORCE_EXILE and 1 or 0 end
function Duel.RegisterEffect(e, p) end
function Duel.IsPlayerNeedToPickDeck(p) return true end
function Duel.GetLP(p) return 8000 end
function Duel.GetTurnPlayer() return 0 end
function Duel.GetDrawCount(p) return 1 end
function Duel.AnnounceType(p) return 0 end

-- 假 io / os：只骗 LoadCardPools / LoadDB（lupa 不带 io/os 库）
FAKE_IO = {}
FAKE_OS = {time = function() return 1762531200 end}
-- Python 传过来的 cdb 行先切成 Lua 表（Lua 下标 1 开始，直接用 py 列表会错位）
CDB_ROWS = {}
do
	for line in CDB_TEXT:gmatch("[^\n]+") do CDB_ROWS[#CDB_ROWS + 1] = line end
end
TYPES = {}
for code, tp in TYPES_TEXT:gmatch("(%-?%d+) (%-?%d+)") do
	TYPES[tonumber(code)] = tonumber(tp)
end
function FAKE_IO.popen(cmd)
	if cmd:find("sqlite3") then
		local i = 0
		return {lines = function() return function()
				i = i + 1
				return CDB_ROWS[i]
			end end, close = function() end}
	end
	local done = false
	return {lines = function() return function()
			if done then return nil end
			done = true
			return "2pick/408.cdb"
		end end, close = function() end}
end

-- ===== 重置到「新的一局」 =====
function RESET()
	for p = 0, 1 do
		DECK[p], EXTRA[p], HAND[p], REMOVED[p], SAVED[p], TOKENS[p] = {}, {}, {}, {}, {}, {}
		DRAWN[p] = 0
		PICKLOG[p] = 0
		-- 开局占位卡组（服务器给的是 1 张青眼白龙）
		local c = new_card(p, 89631139)
		DECK[p][1] = c
	end
	PAIR_LOG = {}
	SEEN = {}
	PRE_COMBO = {}
	TRIM_LOG = {}
	PCP_CALL = {[0] = 0, [1] = 0}
	SEEN_EXCLUDE = {[0]=false,[1]=false}
	BAN_PREV = {[0]={},[1]={}}
end
"""


def load_rows():
    con = sqlite3.connect(CDB)
    try:
        rows = con.execute(
            "select id,ot,alias,setcode,type,atk,def,level,race,attribute,category from datas").fetchall()
    finally:
        con.close()
    return ["|".join(str(x) for x in r) for r in rows], {r[0]: r[4] for r in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--runs", type=int, default=300)
    ap.add_argument("--seed", type=int, default=408)
    ap.add_argument("--force-exile", action="store_true",
                    help="模拟玩家每次都点「除外」，验证本轮拉黑 + 跨轮恢复")
    a = ap.parse_args()

    rows, types = load_rows()
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    g.CDB_TEXT = "\n".join(rows)
    g.TYPES_TEXT = "\n".join("%d %d" % (k, v) for k, v in types.items())
    lua.execute(MOCK)

    # 预加载 require 的模块（服务器上是 ./2pick/xxx，这里直接塞 package.loaded）
    lua.execute(r"""
	local pl = package.loaded
	for _, name in ipairs({"limits", "levels", "exclusive", "ritual", "combo", "deepthink"}) do
		pl["./2pick/" .. name] = dofile("2pick/" .. name .. ".lua")
	end
	package.loaded["io"] = FAKE_IO
	package.loaded["os"] = FAKE_OS
	levels = pl["./2pick/levels"]
	dofile("2pick/pick.lua")
	-- 主卡组 5 轮选完、还没进 combo 时的张数（旧流程用，保留但不参与断言）
	PRE_COMBO = {}
	SEEN = {}
	local ORIG_SP = Auxiliary.SinglePick
	Auxiliary.SinglePick = function(p, list, count, ...)
		if count == 0 and not SEEN[p] then
			SEEN[p] = true
			PRE_COMBO[#PRE_COMBO + 1] = #DECK[p]
		end
		return ORIG_SP(p, list, count, ...)
	end
	-- combo 选包校验：① 永不返回 lastpack / exclude / 本轮拉黑包
	--                ② 进入第二次 combo（见过 exclude）后每次都要带 exclude
	local ORIG_PCP = Auxiliary.PickComboPack
	Auxiliary.PickComboPack = function(p, packed, lastpack, exclude, banned)
		PCP_CALL[p] = PCP_CALL[p] + 1
		local idx = ORIG_PCP(p, packed, lastpack, exclude, banned)
		if idx and (idx == lastpack or idx == exclude) then
			PCP_VIOL = PCP_VIOL + 1
		end
		if banned then
			local n = 0
			for _ in pairs(banned) do n = n + 1 end
			if n > 0 then BAN_SEEN = BAN_SEEN + 1 end
		end
		-- ② 阶段被拉黑的包记进 BAN_PREV；进入 ④ 后看它们能否重新出现
		if not SEEN_EXCLUDE[p] and banned then
			for k in pairs(banned) do BAN_PREV[p][k] = true end
		end
		if SEEN_EXCLUDE[p] and idx and BAN_PREV[p][idx] then
			BAN_RECOVER = BAN_RECOVER + 1
		end
		if idx and banned and banned[idx] then BAN_VIOL = BAN_VIOL + 1 end
		if SEEN_EXCLUDE[p] and exclude == nil then
			PCP_EXCL_NIL = PCP_EXCL_NIL + 1
		end
		if exclude ~= nil then SEEN_EXCLUDE[p] = true end
		return idx
	end
	ORIG_PAIR = Auxiliary.AddRitualPartner
	Auxiliary.AddRitualPartner = function(p, ag)
		local before = {}
		for c in aux.Next(ag) do before[#before + 1] = c:GetOriginalCode() end
		ORIG_PAIR(p, ag)
		local after = {}
		for c in aux.Next(ag) do after[#after + 1] = c:GetOriginalCode() end
		-- 诊断：复刻一遍补卡判断，记下每个 partner 没进候选的原因。
		-- exclusive 是 pick.lua 的 local，这里从 package.loaded 拿同一张表。
		local diag = {}
		local rt = package.loaded["./2pick/ritual"]
		local ex = package.loaded["./2pick/exclusive"]
		if rt then
			for _, code in ipairs(before) do
				local partners = rt.monster_to_spell[code] or rt.spell_to_monster[code]
				if partners then
					for _, rc in ipairs(partners) do
						local inafter = false
						for _, x in ipairs(after) do if x == rc then inafter = true end end
						local why
						if inafter then why = "ok"
						elseif ex and ex[rc] then why = "exclusive"
						elseif ag:IsExists(Card.IsCode, 1, nil, rc) then why = "already-in-ag"
						elseif not Auxiliary.CanPick(p, rc) then why = "quota"
						else why = "token-created-but-lost" end
						diag[tostring(rc)] = why
					end
				end
			end
		end
		table.insert(PAIR_LOG, {p = p, before = before, after = after, diag = diag})
	end
	-- 同名裁剪统计：记「这一次裁掉了几张」，0 = 没触发
	local ORIG_TRIM = Auxiliary.TrimOverLimit
	Auxiliary.TrimOverLimit = function(p)
		local before = #DECK[p]
		ORIG_TRIM(p)
		TRIM_LOG[#TRIM_LOG + 1] = before - #DECK[p]
	end
	""")

    lua.execute("math.randomseed(%d)" % a.seed)
    # 这两个计数器跨局累计（RESET 里不重置），用来统计整轮模拟的违规次数
    lua.execute("PCP_VIOL = 0\nPCP_EXCL_NIL = 0\nBAN_SEEN = 0\nBAN_VIOL = 0"
                "\nBAN_RECOVER = 0"
                "\nFORCE_EXILE = %s" % ("true" if a.force_exile else "false"))
    # 真实服务器是 Load2PickRule 里调一次 LoadCardPools；这里手动调一次就够（列表常驻）
    lua.execute("Auxiliary.LoadCardPools()")
    lua.execute("RESET()")
    # 每局的入口：StartPick 会跑两个玩家
    start = lua.eval("Auxiliary.StartPick")
    fake_e = lua.eval("{Reset = function() end}")

    limit1, limit2, forbidden, pairs, spell2mon, exclusive = load_limits_and_pairs(lua)
    combo_add = load_combo_add_cards()
    except_max = load_deck_limit_except()

    # 第 10 步整理后的额度上限，与 pick.lua 的 Auxiliary.DeckLimitFor 同口径：
    #   例外表优先 -> 禁卡 0 -> 限制 1 -> 准限制 2 -> 其他 3
    def deck_allowed(code):
        if code in except_max:
            return except_max[code]
        if code in forbidden:
            return 0
        if code in limit1:
            return 1
        if code in limit2:
            return 2
        return 3

    # 普通池里能抽到的限制/准限制卡（信息统计：这些卡现在会被裁到额度内）
    pool_limited = (limit1 | limit2) - exclusive - forbidden

    main_sizes = collections.Counter()
    extra_sizes = collections.Counter()
    extra_rounds = collections.Counter()   # 额外轮每轮候选张数
    pair_checked = pair_ok = pair_quota = 0
    bad_pairs = []
    over_limit = []                        # 整理后仍超禁限表额度的卡 -> ⑥ 失效（必须为 0）
    add_seen = collections.Counter()       # ⑤ 补的三张卡最终张数分布（0 = 玩家第 11 步删了）
    trim_amount = collections.Counter()    # ⑥ 每次裁掉几张（0 = 这次没超限）
    drawn = collections.Counter()
    pre_combo = collections.Counter()
    picklog = collections.Counter()   # 每人被发的私聊条数（应 = 8：2 额外轮 + 5 主卡组 + 1 combo）
    removed_left = collections.Counter()  # 决斗开始时还留在 LOCATION_REMOVED 的卡（应为 0）
    lim_in_deck = 0                       # 卡组里带「普通池限制/准限制卡」的人次（现在会被裁到额度内）

    for it in range(a.runs):
        lua.execute("RESET()")
        start(fake_e)

        for v in g.PRE_COMBO.values():
            pre_combo[int(v)] += 1

        for v in g.TRIM_LOG.values():
            trim_amount[int(v)] += 1

        for p in (0, 1):
            codes = list(g.SAVED[p].values())
            nmain = sum(1 for c in codes if not (
                types.get(c, 0) & (0x40 | 0x2000 | 0x800000 | 0x4000000)))
            nextra = len(codes) - nmain
            main_sizes[nmain] += 1
            extra_sizes[nextra] += 1
            drawn[int(g.DRAWN[p])] += 1
            picklog[int(g.PICKLOG[p])] += 1
            removed_left[len(list(g.REMOVED[p].values()))] += 1

            if any(c in pool_limited for c in codes):
                lim_in_deck += 1

            cnt = collections.Counter(codes)
            for c, n in cnt.items():
                lim = deck_allowed(c)
                if n > lim:
                    # 第 10 步「自动整理」没把这张卡裁到禁限表额度内
                    over_limit.append((it, p, c, n, lim))
            for c in combo_add:
                add_seen[(c, cnt.get(c, 0))] += 1

        # 仪式配对：每轮候选里，出现仪式怪兽就必须有对应仪式魔法
        for rec in g.PAIR_LOG.values():
            before = [int(x) for x in rec.before.values()]
            after = [int(x) for x in rec.after.values()]
            extra_rounds[len(before)] += 1
            for c in before:
                if c in pairs:
                    pair_checked += 1
                    missing = [s for s in pairs[c] if s not in after]
                    if not missing:
                        pair_ok += 1
                    else:
                        # diag: partner 卡号 -> 为什么没进候选
                        diag = {str(k): str(v) for k, v in (rec.diag or {}).items()}
                        if all(diag.get(str(s)) == "quota" for s in missing):
                            # 该卡同名额度已经拿满（3 张），系统不再补第 4 张。
                            # 这是禁限表的正确行为，不是配对失效，单列统计。
                            pair_quota += 1
                        else:
                            pwr = int(rec.p)
                            bad_pairs.append((it, pwr, c, pairs[c], after,
                                              dict(g.SAVED[pwr] or {}), diag))

    print("=" * 66)
    print("模拟 %d 局（每局 2 个玩家）" % a.runs)
    print("-" * 66)
    print("主卡组张数分布：%s（上限 47；combo 含融合怪时会少几张，属预期）"
          % dict(sorted(main_sizes.items())))
    print("额外卡组张数分布：%s（8 = 2 轮额外；多出来的是 combo 包里的融合怪）"
          % dict(sorted(extra_sizes.items())))
    print("开局抽到的手卡张数：%s" % dict(sorted(drawn.items())))
    print("额外轮每轮候选张数分布：%s" % dict(sorted(extra_rounds.items())))
    print("私聊选卡记录（Duel.PickLog）每人条数分布：%s（应为 8 = 2 额外 + 4 主卡 + 2 combo）"
          % dict(sorted(picklog.items())))
    print("决斗开始时除外区残留卡数：%s（应全为 0）" % dict(sorted(removed_left.items())))
    print("-" * 66)
    print("combo 选包违规（返回 lastpack 或 exclude）：%d 次（应为 0）" % int(g.PCP_VIOL))
    print("主题包除外拉黑：命中 %d 次调用，重选又抽到被拉黑包 %d 次（应为 0）"
          % (int(g.BAN_SEEN), int(g.BAN_VIOL)))
    print("跨轮恢复    ：④ 里重新出现 ② 被除外包 %d 次（强制除外时应 > 0）"
          % int(g.BAN_RECOVER))
    print("第二次 combo 没带 exclude 的次数：%d 次（应为 0）" % int(g.PCP_EXCL_NIL))
    print("仪式配对检查：%d 次机会，成功 %d 次，因额度已满未补 %d 次，失败 %d 次"
          % (pair_checked, pair_ok, pair_quota, len(bad_pairs)))
    if bad_pairs:
        for b in bad_pairs[:5]:
            print("  失败样本：第 %d 局 玩家 %d 怪兽 %d 需要 %s，候选里只有 %s"
                  % (b[0], b[1], b[2], b[3], b[4]))
            print("    该玩家最终卡组里这些卡的张数 %s；补卡判断 %s"
                  % ({k: v for k, v in b[5].items() if k in b[3]}, b[6]))
    # 第 10 步「自动整理」：卡组里每张卡的张数必须 ≤ 禁限表额度（例外表里的按例外）
    print("⑥ 自动整理：每次裁掉张数分布 %s（0 = 本次没超额度）"
          % dict(sorted(trim_amount.items())))
    print("整理后仍超禁限表额度的次数：%d 次（应为 0）" % len(over_limit))
    if over_limit:
        for o in over_limit[:5]:
            print("  第 %d 局 玩家 %d 卡 %d 有 %d 张（额度 %d）" % o)
    if add_seen:
        print("⑤ 补的那三张最终张数分布：%s（1 = 留在卡组里；0 = 玩家在第 11 步自己删了）"
              % dict(sorted(add_seen.items())))
    print("卡组里带「普通池限制/准限制卡」的人次：%d（这类卡现在会被裁到额度内）" % lim_in_deck)

    bad_main = [k for k in main_sizes if k > 47 or k < 28]
    ok = (not bad_pairs and not bad_main and not over_limit
          and int(g.PCP_VIOL) == 0 and int(g.PCP_EXCL_NIL) == 0
          and int(g.BAN_VIOL) == 0
          and (not a.force_exile or (int(g.BAN_SEEN) > 0
                                    and int(g.BAN_RECOVER) > 0)))
    print("-" * 66)
    print("结论：%s" % ("通过" if ok else "有问题，见上"))
    return 0 if ok else 1


def load_limits_and_pairs(lua):
    import json
    d = json.load(io.open(os.path.join(PICK_DIR, "limits.json"), encoding="utf-8"))
    limit1 = set(int(x) for x in d.get("limit1", []))
    limit2 = set(int(x) for x in d.get("limit2", {}))
    forbidden = set(int(x) for x in d.get("forbidden", []))
    # ritual.lua 的 monster_to_spell：复用同一个运行时已加载的表
    r = lua.eval('package.loaded["./2pick/ritual"]')
    m2s, s2m = {}, {}
    for k in r.monster_to_spell.keys():
        m2s[int(k)] = [int(x) for x in r.monster_to_spell[k].values()]
    for k in r.spell_to_monster.keys():
        s2m[int(k)] = [int(x) for x in r.spell_to_monster[k].values()]
    # exclusive 表：普通轮抽抽不到的卡（pick.lua 里是 local，这里拿同一个表）
    ex = lua.eval('package.loaded["./2pick/exclusive"]')
    exclusive = set(int(k) for k in ex.keys())
    return limit1, limit2, forbidden, m2s, s2m, exclusive


def load_combo_add_cards():
    """从 patch_pick 模板里取 COMBO_ADD_CARDS（pick.lua 里是 local，正则读源更省事）"""
    src = io.open(os.path.join(DEV, "tools", "patch_pick.py"), encoding="utf-8").read()
    m = re.search(r"COMBO_ADD_CARDS\s*=\s*\{([^}]*)\}", src)
    return set(int(x) for x in re.findall(r"\d+", m.group(1))) if m else set()


def load_deck_limit_except():
    """从 patch_pick 模板里取 DECK_LIMIT_EXCEPT（卡号 -> 允许保留张数）"""
    src = io.open(os.path.join(DEV, "tools", "patch_pick.py"), encoding="utf-8").read()
    m = re.search(r"DECK_LIMIT_EXCEPT\s*=\s*\{([^}]*)\}", src)
    out = {}
    if m:
        for code, n in re.findall(r"\[(\d+)\]\s*=\s*(\d+)", m.group(1)):
            out[int(code)] = int(n)
    return out


if __name__ == "__main__":
    sys.exit(main())
