-- ============================================================
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
-- ==== CONFIG ====
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
os=require("os")
io=require("io")
--globals
local main={[0]={},[1]={}}
local extra={[0]={},[1]={}}

local main_nonadv={[0]={},[1]={}}

main_monster={[0]={},[1]={}}
main_spell={[0]={},[1]={}}
main_trap={[0]={},[1]={}}

main_plain={[0]={},[1]={}}
local main_adv={[0]={},[1]={}}

local main_new={[0]={},[1]={}}

local extra_sp={
	[TYPE_FUSION]={[0]={},[1]={}},
	[TYPE_SYNCHRO]={[0]={},[1]={}},
	[TYPE_XYZ]={[0]={},[1]={}},
	[TYPE_LINK]={[0]={},[1]={}},
}

local xyz_plain={[0]={},[1]={}}
local xyz_adv={[0]={},[1]={}}

local extra_fixed={} -- 408 卡池不含这两张现代卡
local combo_pack=require("./2pick/combo")
local Deepthink=require("./2pick/deepthink")
--local ActionDuel=require("./2pick/actionduel")

function Auxiliary.SplitData(inputstr)
	local t={}
	for str in string.gmatch(inputstr,"([^|]+)") do
		table.insert(t,tonumber(str))
	end
	return t
end
function Auxiliary.LoadDB(p,pool)
	local file=io.popen("echo .exit | sqlite3 "..pool.." -cmd \"select * from datas;\"")
	for line in file:lines() do
		local data=Auxiliary.SplitData(line)
		if #data<2 then break end
		local code=data[1]
		local ot=data[2]
		local cat=data[5]
		local lv=data[8] & 0xff
		if exclusive[code] then
			-- 408：combo 专用卡（如被封印的艾克佐迪亚 5 件套），不进普通池
		elseif (cat & TYPE_FUSION+TYPE_SYNCHRO+TYPE_XYZ+TYPE_LINK)>0 then
			table.insert(extra[p],code)
			for tp,list in pairs(extra_sp) do
				if (cat & tp)>0 then
					table.insert(list[p],code)
				end
			end
			if (cat & TYPE_XYZ)>0 then
				if lv>4 then
					table.insert(xyz_adv[p],code)
				else
					table.insert(xyz_plain[p],code)
				end
			end
		elseif (cat & TYPE_TOKEN)==0 then
			if (ot==4) then
				table.insert(main_new[p],code)
			end
			if (cat & TYPE_MONSTER)>0 then
				table.insert(main_monster[p],code)
				if lv>4 then
					table.insert(main_adv[p],code)
				else
					table.insert(main_plain[p],code)
					table.insert(main_nonadv[p],code)
				end
			elseif (cat & TYPE_SPELL)>0 then
				table.insert(main_nonadv[p],code)
				table.insert(main_spell[p],code)
			elseif (cat & TYPE_TRAP)>0 then
				table.insert(main_nonadv[p],code)
				table.insert(main_trap[p],code)
			end
			table.insert(main[p],code)
		end
	end
	file:close()
end
--to do: multi card pools
function Auxiliary.LoadCardPools()
	local pool_list={}
	local file=io.popen("ls 2pick/*.cdb")
	for pool in file:lines() do
		table.insert(pool_list,pool)
	end
	file:close()
	for p=0,1 do
		Auxiliary.LoadDB(p,pool_list[math.random(#pool_list)])
	end
end

function Auxiliary.SaveDeck()
	for p=0,1 do
		-- 4f: 仅本局现场轮抽（pick_needed=true）才写回：M# 第二/三局不轮抽，
		--     再 SaveDeck 会重复触发服务端 MSG_SAVE_PICK_DECK -> REPLAY_CODE，
		--     把本局 replay 截断重建 + 就地再切洗 pick_deck，损坏录像（2026-11-12）。
		--     内核按引擎卡组实际顺序写回，不再传 GetFieldGroup 的 group（它的
		--     container 是 card_set，按 cardid 排序会丢 ShuffleDeck 后的真实牌序）。
		if Duel.IsPlayerNeedToPickDeck(p) then
			Duel.SavePickDeck(p)
		end
	end
end
function Auxiliary.SinglePick(p,list,count,ex_list,ex_count,copy,lv_diff,fixed,packed,optional,exclude)
	if not Duel.IsPlayerNeedToPickDeck(p) then return end
	local round_used={}
	local picked_pack=nil
	local pack_of={}
	local g1=Group.CreateGroup()
	local g2=Group.CreateGroup()
	local ag=Group.CreateGroup()
	local plist=list[p]
	local lastpack=-1
	for _,g in ipairs({g1,g2}) do
		--for i=1,count do
		--	local code=plist[math.random(#plist)]
		--	g:AddCard(Duel.CreateToken(p,code))
		--end
		local pick_count=0
		if packed and #packed>0 then
			local thispack=Auxiliary.PickComboPack(p,packed,lastpack,exclude,combo_banned[p])
			if thispack then
				lastpack=thispack
				pack_of[g]=thispack
				for _,code in ipairs(packed[thispack]) do
					round_used[code]=(round_used[code] or 0)+1
					local card=Duel.CreateToken(p,code)
					g:AddCard(card)
					ag:AddCard(card)
				end
			end
		end
		while pick_count<count do
			local code=plist[math.random(#plist)]
			local lv=Auxiliary.CardLevel(code)
			if Auxiliary.CanPick(p,code,round_used) and not ag:IsExists(Card.IsCode,1,nil,code) and not (lv_diff and g:IsExists(Card.IsLevel,1,nil,lv)) then
				round_used[code]=(round_used[code] or 0)+1
				local card=Duel.CreateToken(p,code)
				g:AddCard(card)
				ag:AddCard(card)
				pick_count=pick_count+1
			end
		end
		if ex_list and ex_count then
			--for i=1,ex_count do
			--	local code=ex_plist[math.random(#ex_plist)]
			--	g:AddCard(Duel.CreateToken(p,code))
			--end
			local ex_plist=ex_list[p]
			local ex_pick_count=0
			while ex_pick_count<ex_count do
				local code=ex_plist[math.random(#ex_plist)]
				local lv=Auxiliary.CardLevel(code)
				if Auxiliary.CanPick(p,code,round_used) and not ag:IsExists(Card.IsCode,1,nil,code) and not (lv_diff and g:IsExists(Card.IsLevel,1,nil,lv)) then
					round_used[code]=(round_used[code] or 0)+1
					local card=Duel.CreateToken(p,code)
					g:AddCard(card)
					ag:AddCard(card)
					ex_pick_count=ex_pick_count+1
				end
			end
		end
		if fixed then
			for _,code in ipairs(fixed) do
				local card=Duel.CreateToken(p,code)
				g:AddCard(card)
				ag:AddCard(card)
			end
		end
		Duel.SendtoDeck(g,nil,0,REASON_RULE)
	end
	if ag:GetCount()==0 then return false end
	Duel.ResetTimeLimit(p,PICK_TIME_LIMIT)
	
	if optional and ag:GetFirst():IsLocation(LOCATION_DECK) then
		Duel.ConfirmCards(p,ag)
		if Duel.SelectOption(p,1190,1192)==1 then
			-- 3k: 两组都不要 -> 本轮拉黑，重选不再随机到它们
			for _,g in ipairs({g1,g2}) do
				local pk=pack_of[g]
				if pk then combo_banned[p][pk]=true end
			end
			Duel.Exile(ag,REASON_RULE)
			return false
		end
	end
	
	local tg=Group.CreateGroup()
	local rg=ag
	-- 3g2 combo 选包：一组一弹窗。原版 12 张一起列，但弹窗顺序被内核
	--   card_operation_sort 按卡种重排，两组交叉看不出分组；
	--   改成一次只列一组：确认=选它，取消=换另一组。
	local chosen=nil
	local cur=g1
	for _=1,6 do
		if cur:GetCount()==0 then break end
		Duel.Hint(HINT_SELECTMSG,p,HINTMSG_SELECT)
		local sc=cur:SelectUnselect(Group.CreateGroup(),p,false,true,1,1)
		if sc then chosen=cur break end
		cur=(cur==g1) and g2 or g1
	end
	if not chosen then chosen=(g1:GetCount()>0) and g1 or g2 end
	tg=chosen
	rg=(tg==g1) and g2 or g1
	-- 记录「这两组里玩家真正拿走的那一组」是第几个包（3j 用）
	if packed and #packed>0 then picked_pack=pack_of[tg] end
	
	-- 3f 确认卡片（CONFIRM_MODE=0 不发；=1 只发本轮选中，<4 张改发整批以走列表弹窗）
	--    官方 MSG_CONFIRM_CARDS 只在「第一张卡在 LOCATION_DECK」时单播本人，
	--    其它位置连对手/观战一起发；patch_pick_chat.py 已把 LOCATION_EXTRA
	--    也改成单播，所以额外轮同样能弹窗，且客户端会 SetCode 补上卡号。
	local canConfirm=(tg:GetCount()>0)
	for c in aux.Next(tg) do
		if not c:IsLocation(LOCATION_DECK) and not (CONFIRM_EXTRA and c:IsLocation(LOCATION_EXTRA)) then canConfirm=false break end
	end
	if CONFIRM_MODE>0 and canConfirm then
		if tg:GetCount()<4 then Duel.ConfirmCards(p,ag) else Duel.ConfirmCards(p,tg) end
	end
	-- 3f2 私聊选卡记录（ENABLE_PICK_LOG）：卡号交内核，服务端查 cardnames.txt
	--     拼一行「【轮抽】本轮选卡：…」只发给本人；Duel.PickLog 不存在时跳过。
	if ENABLE_PICK_LOG and Duel.PickLog then Duel.PickLog(p,tg) end
	Duel.Exile(rg,REASON_RULE)
	for nc in aux.Next(tg) do
		Auxiliary.MarkPick(p,nc:GetOriginalCode())
	end
	if copy then
		local g3=Group.CreateGroup()
		for nc in aux.Next(tg) do
			local copy_code=nc:GetOriginalCode()
			Auxiliary.MarkPick(p,copy_code)
			g3:AddCard(Duel.CreateToken(p,copy_code))
		end
		Duel.SendtoDeck(g3,nil,0,REASON_RULE)
	end
	-- 3j: 记录本次 combo 拿到的包（第二次 combo 要避开它）
	if picked_pack then combo_picked[p]=picked_pack end
	return true
end
function Auxiliary.ArbitraryPick(p,count,pick_lists,lists_count,copy,lv_diff,fixed)
	if not Duel.IsPlayerNeedToPickDeck(p) then return end
	local round_used={}
	local ag=Group.CreateGroup()
	local eg=Group.CreateGroup()
	for index,list in pairs(pick_lists) do
		local plist=list[p]
		local pg=Group.CreateGroup()
		local pick_count=lists_count[index]
		while pick_count>0 do
			local code=plist[math.random(#plist)]
			local lv=Auxiliary.CardLevel(code)
			if Auxiliary.CanPick(p,code,round_used) and not ag:IsExists(Card.IsCode,1,nil,code) and not (lv_diff and pg:IsExists(Card.IsLevel,1,nil,lv)) then
				round_used[code]=(round_used[code] or 0)+1
				local card=Duel.CreateToken(p,code)
				ag:AddCard(card)
				pg:AddCard(card)
				pick_count=pick_count-1
			end
		end
	end
	-- 仪式配对：仪式怪兽带着它的仪式魔法一起出现在候选里（反之亦然）
	Auxiliary.AddRitualPartner(p,ag)
	if fixed then
		for _,code in ipairs(fixed) do
			local card=Duel.CreateToken(p,code)
			ag:AddCard(card)
		end
	end
	Duel.SendtoDeck(ag,nil,0,REASON_RULE)
	Duel.ResetTimeLimit(p,PICK_TIME_LIMIT)
	
	Duel.Hint(HINT_SELECTMSG,p,0)
	local tg=ag:Select(p,count,count,nil)
	local all=Group.CreateGroup()   -- 3f: 整批候选快照（下面 rg:Sub(tg) 会把 ag 改小）
	for c in aux.Next(ag) do all:AddCard(c) end
	local rg=ag
	rg:Sub(tg)
	-- 3g 官方这里还有一段 SelectUnselect「选完再换卡」的循环，已删除：
	--    MDPro3 的弹窗一次只能点 1 张、点了就并进已选组，超过上限后
	--    finishable/cancelable 都变 false（取消按钮失效），必定卡死在「剩余卡片」界面。
	--    要换卡请在下面这次 Select 里操作（多选了就再点一下取消选中）。
	
	-- 3f 确认卡片（CONFIRM_MODE=0 不发；=1 只发本轮选中，<4 张改发整批以走列表弹窗）
	--    官方 MSG_CONFIRM_CARDS 只在「第一张卡在 LOCATION_DECK」时单播本人，
	--    其它位置连对手/观战一起发；patch_pick_chat.py 已把 LOCATION_EXTRA
	--    也改成单播，所以额外轮同样能弹窗，且客户端会 SetCode 补上卡号。
	local canConfirm=(tg:GetCount()>0)
	for c in aux.Next(tg) do
		if not c:IsLocation(LOCATION_DECK) and not (CONFIRM_EXTRA and c:IsLocation(LOCATION_EXTRA)) then canConfirm=false break end
	end
	if CONFIRM_MODE>0 and canConfirm then
		if tg:GetCount()<4 then Duel.ConfirmCards(p,all) else Duel.ConfirmCards(p,tg) end
	end
	-- 3f2 私聊选卡记录（ENABLE_PICK_LOG）：卡号交内核，服务端查 cardnames.txt
	--     拼一行「【轮抽】本轮选卡：…」只发给本人；Duel.PickLog 不存在时跳过。
	if ENABLE_PICK_LOG and Duel.PickLog then Duel.PickLog(p,tg) end
	Duel.Exile(rg,REASON_RULE)
	for nc in aux.Next(tg) do
		Auxiliary.MarkPick(p,nc:GetOriginalCode())
	end
	if copy then
		local g3=Group.CreateGroup()
		for nc in aux.Next(tg) do
			local copy_code=nc:GetOriginalCode()
			Auxiliary.MarkPick(p,copy_code)
			g3:AddCard(Duel.CreateToken(p,copy_code))
		end
		Duel.SendtoDeck(g3,nil,0,REASON_RULE)
	end
end
function Auxiliary.StartPick(e)
	-- ★防重入：先注销 EVENT_ADJUST 全局效果（原版放在末尾，选卡期间会被再次触发）
	e:Reset()
	picked_limit[0]={}
	picked_limit[1]={}
	combo_picked[0]=nil
	combo_picked[1]=nil
	combo_banned[0]={}
	combo_banned[1]={}
	-- 轮抽期间放宽双方应答计时（服务端超时会 MSG_WIN(0x3) 直接结束决斗）
	Duel.ResetTimeLimit(0,PICK_TIME_LIMIT)
	Duel.ResetTimeLimit(1,PICK_TIME_LIMIT)
	for p=0,1 do
		if Duel.IsPlayerNeedToPickDeck(p) then
			local g=Duel.GetFieldGroup(p,0xff,0)
			Duel.Exile(g,REASON_RULE)
		end
	end
--[[
	for i=1,5 do
		local list=main
		local count=4
		local ex_list=nil
		local ex_count=nil
		if i==1 or i==2 then
			list=main_plain
			count=3
			ex_list=main_adv
			ex_count=1
		elseif i==3 then
			list=main_plain
			--Adding New Cards
			count=3
			ex_list=main_new
			ex_count=1
		elseif i==4 then
			list=main_spell
		elseif i==5 then
			list=main_trap
		end
		for p=0,1 do
			Auxiliary.SinglePick(p,list,count,ex_list,ex_count,false)
		end
	end
]]--
	-- ===== 408 轮抽序列（改这里 = 改流程，改完必须重跑 patch_pick.py）=====
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

	-- -- XXYYZZ Additional Picks
	-- xyz_list={91998119,91998120,91998121}
	-- for p=0,1 do
	-- 	if Duel.IsPlayerNeedToPickDeck(p) then
	-- 		local ng=Group.CreateGroup()
	-- 		local card1=Duel.CreateToken(p,2111707)
	-- 		local card2=Duel.CreateToken(p,25119460)
	-- 		local card3=Duel.CreateToken(p,99724761)
	-- 		local card4=Duel.CreateToken(p,xyz_list[math.random(#xyz_list)])
	-- 		ng:AddCard(card1)
	-- 		ng:AddCard(card2)
	-- 		ng:AddCard(card3)
	-- 		ng:AddCard(card4)
	-- 		Duel.SendtoDeck(ng,nil,0,REASON_RULE)
	-- 	end
	-- end
	
	--ActionDuel.Load_Action_Duel()
	
	for p=0,1 do
		if Duel.IsPlayerNeedToPickDeck(p) then
			Duel.ShuffleDeck(p)
			Duel.ResetTimeLimit(p)
		end
	end
	-- 先切洗再 SaveDeck：MSG_SAVE_PICK_DECK 记录的是引擎切洗后的真实牌序，
	-- 录像头卡组顺序才对得上引擎（2026-11-12，修「录像无法播放」）。
	Auxiliary.SaveDeck()
	-- 主卡组可能不足 40 张（复制被额度拒掉时不补位），这里按实际张数收敛：
	-- 牌堆 >= start_count 就照常抽 5 张，不够就只抽现有的，避免抽空判负。
	for p=0,1 do
		local n=Duel.GetStartCount(p)
		local avail=Duel.GetFieldGroupCount(p,LOCATION_DECK,0)
		if n>avail then n=avail end
		if n>0 then Duel.Draw(p,n,REASON_RULE) end
	end
	-- e:Reset() 已前移到 StartPick 开头（防 EVENT_ADJUST 重入）
end

function Auxiliary.Load2PickRule()
	math.randomseed(os.time())
	Auxiliary.LoadCardPools()
	local e1=Effect.GlobalEffect()
	e1:SetType(EFFECT_TYPE_FIELD | EFFECT_TYPE_CONTINUOUS)
	e1:SetCode(EVENT_ADJUST)
	e1:SetProperty(EFFECT_FLAG_IGNORE_IMMUNE)
	e1:SetOperation(Auxiliary.StartPick)
	Duel.RegisterEffect(e1,0)

	--Skill Deepthink（408 默认关闭）
	if ENABLE_DEEPTHINK then Deepthink.Load_Skill_Deepthink_Rule() end

	--Skill DrawSense Specials
	--Auxiliary.Load_Skill_DrawSense_Rule()
	
	--Chicken_Game_Rule
	--Auxiliary.Load_Chicken_Game_Rule()
end

	--Skill_DrawSense_Rule

function Auxiliary.Load_Skill_DrawSense_Rule()
	local e1=Effect.GlobalEffect()
	e1:SetType(EFFECT_TYPE_FIELD+EFFECT_TYPE_CONTINUOUS)
	e1:SetProperty(EFFECT_FLAG_PLAYER_TARGET)
	e1:SetTargetRange(1,1)
	--e1:SetCode(PHASE_DRAW+EVENT_PHASE_START)
	e1:SetCode(EVENT_PREDRAW)
	e1:SetCondition(Auxiliary.Skill_DrawSense_Condition)
	e1:SetOperation(Auxiliary.Skill_DrawSense_Operation)
	Duel.RegisterEffect(e1,0)
end

function Auxiliary.Skill_DrawSense_Condition(e,tp,eg,ep,ev,re,r,rp)
	local tp=Duel.GetTurnPlayer()
	return (Duel.GetLP(1-tp))-(Duel.GetLP(tp))>2499
		and Duel.GetFieldGroupCount(tp,LOCATION_DECK,0)>1
		and Duel.GetDrawCount(tp)>0
		and Duel.GetFieldGroupCount(tp,LOCATION_MZONE,0)==0
		--and Duel.IsExistingMatchingCard(Auxiliary.Skill_DestinyDraw_SearchFilter,tp,LOCATION_DECK,0,1,nil)
end

function Auxiliary.Skill_DrawSense_Operation(e,tp,eg,ep,ev,re,r,rp)
	local tp=Duel.GetTurnPlayer()
	local dt=Duel.GetDrawCount(tp)
	if dt~=0 then
		_replace_count=0
		_replace_max=dt
		-- local e1=Effect.CreateEffect(e:GetHandler())
		-- e1:SetType(EFFECT_TYPE_FIELD)
		-- e1:SetProperty(EFFECT_FLAG_PLAYER_TARGET)
		-- e1:SetCode(EFFECT_DRAW_COUNT)
		-- e1:SetTargetRange(1,0)
		-- e1:SetReset(RESET_PHASE+PHASE_DRAW)
		-- e1:SetValue(0)
		-- Duel.RegisterEffect(e1,tp)

		Duel.Hint(HINT_SELECTMSG,tp,HINTMSG_CARDTYPE)
		local SenseType=(Duel.AnnounceType(tp))

		if (SenseType==0 and Duel.IsExistingMatchingCard(Card.IsType,tp,LOCATION_DECK,0,1,nil,TYPE_MONSTER)) then
			g=Duel.GetMatchingGroup(Card.IsType,tp,LOCATION_DECK,0,nil,TYPE_MONSTER)
			local SenseCard=g:RandomSelect(tp,1)
			local tc=SenseCard:GetFirst()
			if tc then
				Duel.ShuffleDeck(tp)
				Duel.MoveSequence(tc,0)
			end
			--Duel.Draw(tp,1,REASON_RULE)
		elseif (SenseType==1 and Duel.IsExistingMatchingCard(Card.IsType,tp,LOCATION_DECK,0,1,nil,TYPE_SPELL)) then
			g=Duel.GetMatchingGroup(Card.IsType,tp,LOCATION_DECK,0,nil,TYPE_SPELL)
			local SenseCard=g:RandomSelect(tp,1)
			local tc=SenseCard:GetFirst()
			if tc then
				Duel.ShuffleDeck(tp)
				Duel.MoveSequence(tc,0)
			end
			--Duel.Draw(tp,1,REASON_RULE)
		elseif (SenseType==2 and Duel.IsExistingMatchingCard(Card.IsType,tp,LOCATION_DECK,0,1,nil,TYPE_TRAP)) then
			g=Duel.GetMatchingGroup(Card.IsType,tp,LOCATION_DECK,0,nil,TYPE_TRAP)
			local SenseCard=g:RandomSelect(tp,1)
			local tc=SenseCard:GetFirst()
			if tc then
				Duel.ShuffleDeck(tp)
				Duel.MoveSequence(tc,0)
			end
			--Duel.Draw(tp,1,REASON_RULE)
		else 
			Duel.ShuffleDeck(tp)
			--Duel.Draw(tp,1,REASON_RULE)
		end
	end
end

--Chicken_Game_Rule
function Auxiliary.Load_Chicken_Game_Rule()
	local e1=Effect.GlobalEffect()
	e1:SetType(EFFECT_TYPE_FIELD+EFFECT_TYPE_CONTINUOUS)
	e1:SetProperty(EFFECT_FLAG_PLAYER_TARGET+EFFECT_FLAG_IGNORE_IMMUNE)
	e1:SetTargetRange(1,1)
	e1:SetCountLimit(1)
	e1:SetCode(EVENT_PHASE_START+PHASE_STANDBY)
	e1:SetOperation(Auxiliary.Chicken_Game_Operation)
	Duel.RegisterEffect(e1,0)
end
function Auxiliary.Chicken_Game_Operation(e,tp,eg,ep,ev,re,r,rp)
	local tp=Duel.GetTurnPlayer()
	Duel.Hint(HINT_SELECTMSG,tp,HINTMSG_EFFECT)
	local hintlist=(Duel.GetFieldGroupCount(tp,LOCATION_HAND,0)>0 and Duel.GetFieldGroupCount(tp,LOCATION_DECK,0)>0) and {aux.Stringid(64306248,0),aux.Stringid(42541548,0)} or {aux.Stringid(64306248,0)}
    local op=Duel.SelectOption(tp,table.unpack(hintlist))
	-- heal
	if op==0 then
		Duel.Recover(tp,1000,REASON_EFFECT)
	-- sendback and draw
	else 
		local g=Duel.SelectMatchingCard(tp,aux.TRUE,tp,LOCATION_HAND,0,1,1,nil)
		if g:GetCount()>0 and Duel.SendtoDeck(g,nil,1,REASON_EFFECT)>0 then
			Duel.Draw(tp,1,REASON_EFFECT)
		end
	end
end

--EVENT ExtraFusion
