#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""408 2pick 轮抽服内核补丁：把全部 C++ 改动打到 virgin ygopro 源码上。

基线：mycard/ygopro server@176fc60c + 子模块 ocgcore@3209cd86
用法：python3 port_2pick.py --root /root/ygopro-2pick/src --pack /root/2pick-408
只支持全新源码树：每个锚点必须恰好命中 1 次，否则立刻停；改前生成 .bak-<TS>。

改动清单：
  1. 开局不自动抽卡，改由 pick.lua 轮抽完自己 Duel.Draw（占位卡组只有 1 张）。
  2. player_info.pick_needed：区分「本局要轮抽」与「第二局沿用已抽卡组」。
  3. SingleDuel 持有占位/轮抽卡组；MSG_SAVE_PICK_DECK 覆盖 pdeck；第二局跳换 side。
  4. Duel.Exile：写内部消息 MSG_PICK_EXILE + 引擎静默 remove_card，Analyze 转成
     「目的地=位置0」的 MSG_MOVE 直发双方。不能用 send_to(0)（PLAYER_NONE 使
     players[cc] 越界）、也不能真送除外区（污染决斗）。
  5. Duel.PickLog 私聊 + 逐卡 MSG_UPDATE_CARD 补发 DECK/EXTRA 真卡号。
  6. MSG_CONFIRM_CARDS 对额外卡组只单播本人（防泄露 / 阻塞对手）。
  7. 轮抽录像重建：截断占位卡组与选卡响应、按引擎真实牌序重写卡组段。
  8. interpreter.cpp 定义 YGOPRO_NO_LUA_SAFE（pick.lua 需要 require/package/io/os）。
  9. reset_random()：MSG_SAVE_PICK_DECK 后复位内核 RNG。回放客户端不重演选卡，
     而选卡期 ShuffleDeck 消耗过随机数，不复位会与回放逐位错开。
"""
import argparse
import datetime
import os
import shutil
import sys

STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def backup(path):
    dst = path + ".bak-" + STAMP
    if not os.path.exists(dst):
        shutil.copy2(path, dst)
    return dst


def sub_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit("[失败] %s：锚点命中 %d 次（应为 1）\n--- 锚点 ---\n%s" %
                         (label, n, old[:300]))
    print("  [OK] %s" % label)
    return text.replace(old, new, 1)


def edit_file(path, transform, label):
    if not os.path.exists(path):
        raise SystemExit("[失败] 找不到 %s（%s）" % (path, label))
    backup(path)
    write(path, transform(read(path)))
    print("[改] %s" % path)


# ---------------------------------------------------------------------------
# ocgcore/common.h：内部消息号
# ---------------------------------------------------------------------------
def patch_common_h(path):
    def go(s):
        import re
        m = re.search(r"(?m)^#define MSG_CUSTOM_MSG[ \t]+180[ \t]*$", s)
        if not m:
            raise SystemExit("[失败] common.h 找不到 MSG_CUSTOM_MSG 180")
        anchor = s[m.start():m.end() + 1]
        add = ("#define MSG_SAVE_PICK_DECK\t\t220\n"
               "#define MSG_RESET_TIME\t\t\t221\n"
               "#define MSG_PICK_LOG\t\t\t222\n"
               "#define MSG_PICK_EXILE\t\t\t223\n")
        return sub_once(s, anchor, anchor + add, "common.h: 内部消息 220~223")
    edit_file(path, go, "common.h")


# ---------------------------------------------------------------------------
# ocgcore/field.h：player_info.pick_needed
# ---------------------------------------------------------------------------
FIELD_H_ANCHOR = ("struct player_info {\n"
                  "\tint32_t lp{ 8000 };\n"
                  "\tint32_t start_count{ 5 };\n"
                  "\tint32_t draw_count{ 1 };\n")


def patch_field_h(path):
    def go(s):
        add = ("\t// 2pick：本局该玩家是否还要现场轮抽（true 时引擎跳过开局抽卡，交给 pick.lua）\n"
               "\tbool pick_needed{ false };\n")
        return sub_once(s, FIELD_H_ANCHOR, FIELD_H_ANCHOR + add,
                        "field.h: player_info::pick_needed")
    edit_file(path, go, "field.h")


# ---------------------------------------------------------------------------
# ocgcore/duel.h：保存建局种子，供轮抽结束后复位内核 RNG
# 客户端回放从 create_duel_v2(seed_sequence) 起步且不重演选卡流程，
# 而服务器选卡期的 Duel.ShuffleDeck 会消耗内核随机数；必须能把 RNG 拨回起点。
# ---------------------------------------------------------------------------
DUEL_H_ANCHOR = "\tmtrandom random;\n"
DUEL_H_ADD = ("\t// 2pick: 建局种子，reset_random() 用它把内核 RNG 复位到对局起点\n"
              "\tuint32_t seed_sequence[SEED_COUNT]{};\n")


def patch_duel_h(path):
    def go(s):
        return sub_once(s, DUEL_H_ANCHOR, DUEL_H_ANCHOR + DUEL_H_ADD,
                        "duel.h: 保存 seed_sequence")
    edit_file(path, go, "duel.h")


# ---------------------------------------------------------------------------
# ocgcore/ocgapi.h / .cpp：set_player_info 第 5 参数 + 关掉开局抽卡
#   + 保存种子 / 新增 reset_random()（轮抽录像回放对齐）
# ---------------------------------------------------------------------------
OCGAPI_H_OLD = ("void set_player_info(intptr_t pduel, int32_t playerid, int32_t lp, "
                "int32_t startcount, int32_t drawcount);")
OCGAPI_H_NEW = ("void set_player_info(intptr_t pduel, int32_t playerid, int32_t lp, "
                "int32_t startcount, int32_t drawcount, bool pick_needed = false);")

OCGAPI_CPP_SIG_OLD = ("void set_player_info(intptr_t pduel, int32_t playerid, int32_t lp, "
                      "int32_t startcount, int32_t drawcount) {")
OCGAPI_CPP_SIG_NEW = ("void set_player_info(intptr_t pduel, int32_t playerid, int32_t lp, "
                      "int32_t startcount, int32_t drawcount, bool pick_needed) {")

OCGAPI_CPP_BODY_OLD = ("\tif(drawcount >= 0)\n"
                       "\t\tpd->game_field->player[playerid].draw_count = drawcount;\n"
                       "}")
OCGAPI_CPP_BODY_NEW = ("\tif(drawcount >= 0)\n"
                       "\t\tpd->game_field->player[playerid].draw_count = drawcount;\n"
                       "\tpd->game_field->player[playerid].pick_needed = pick_needed;\n"
                       "}")

START_DUEL_DRAW_OLD = (
    "\tif(pd->game_field->player[0].start_count > 0)\n"
    "\t\tpd->game_field->draw(0, REASON_RULE, PLAYER_NONE, 0, pd->game_field->player[0].start_count);\n"
    "\tif(pd->game_field->player[1].start_count > 0)\n"
    "\t\tpd->game_field->draw(0, REASON_RULE, PLAYER_NONE, 1, pd->game_field->player[1].start_count);\n")

START_DUEL_DRAW_NEW = (
    "\t// 2pick: 开局抽卡交由 pick.lua（轮抽结束后 Duel.Draw(p, GetStartCount(p))）。\n"
    "\t// 轮抽局此时牌堆里只有 1 张占位卡，照常抽会抽空 -> 无卡可抽 -> 一开场就结束。\n"
    "\t/*\n" + START_DUEL_DRAW_OLD + "\t*/\n")

# 轮抽录像回放对齐：create_duel_v2 把种子存进 duel，reset_random 用它重新播种。
# 不用 gframe 层传参（每局 seed 不同、单局内还可能跨函数），duel 自持最稳。
RESEED_H_ANCHOR = "OCGCORE_API intptr_t create_duel_v2(uint32_t seed_sequence[]);\n"
RESEED_H_ADD = RESEED_H_ANCHOR + "OCGCORE_API void reset_random(intptr_t pduel);\n"

CREATE_V2_OLD = ("intptr_t create_duel_v2(uint32_t seed_sequence[]) {\n"
                 "\tduel* pduel = new duel();\n"
                 "\tduel_set.insert(pduel);\n"
                 "\tpduel->random.seed(seed_sequence, SEED_COUNT);\n"
                 "\tpduel->rng_version = 2;\n"
                 "\treturn (intptr_t)pduel;\n"
                 "}\n")
CREATE_V2_NEW = ("intptr_t create_duel_v2(uint32_t seed_sequence[]) {\n"
                 "\tduel* pduel = new duel();\n"
                 "\tduel_set.insert(pduel);\n"
                 "\t// 2pick: 留存种子，供轮抽结束后 reset_random() 复位 RNG\n"
                 "\tfor(int i = 0; i < SEED_COUNT; ++i)\n"
                 "\t\tpduel->seed_sequence[i] = seed_sequence[i];\n"
                 "\tpduel->random.seed(seed_sequence, SEED_COUNT);\n"
                 "\tpduel->rng_version = 2;\n"
                 "\treturn (intptr_t)pduel;\n"
                 "}\n"
                 "void reset_random(intptr_t pduel) {\n"
                 "\tduel* pd = (duel*)pduel;\n"
                 "\t// 2pick: 选卡流程(Duel.ShuffleDeck 等)消耗过内核随机数，\n"
                 "\t// 但录像回放不重演选卡；复位到建局起点，后续所有随机事件\n"
                 "\t// (洗手牌/掷币/随机丢弃)才与回放客户端逐位一致。\n"
                 "\tpd->random.seed(pd->seed_sequence, SEED_COUNT);\n"
                 "}\n")


def patch_ocgapi_h(path):
    def go(s):
        s = sub_once(s, OCGAPI_H_OLD, OCGAPI_H_NEW,
                     "ocgapi.h: set_player_info 第 5 参数")
        return sub_once(s, RESEED_H_ANCHOR, RESEED_H_ADD,
                        "ocgapi.h: 声明 reset_random")
    edit_file(path, go, "ocgapi.h")


def patch_ocgapi_cpp(path):
    def go(s):
        s = sub_once(s, OCGAPI_CPP_SIG_OLD, OCGAPI_CPP_SIG_NEW,
                     "ocgapi.cpp: set_player_info 签名")
        s = sub_once(s, OCGAPI_CPP_BODY_OLD, OCGAPI_CPP_BODY_NEW,
                     "ocgapi.cpp: 写入 pick_needed")
        s = sub_once(s, CREATE_V2_OLD, CREATE_V2_NEW,
                     "ocgapi.cpp: 保存种子 + 实现 reset_random")
        return sub_once(s, START_DUEL_DRAW_OLD, START_DUEL_DRAW_NEW,
                        "ocgapi.cpp: 关掉 start_duel 开局抽卡")
    edit_file(path, go, "ocgapi.cpp")


# ---------------------------------------------------------------------------
# ocgcore/interpreter.cpp：定义 YGOPRO_NO_LUA_SAFE，走 luaL_openlibs 全量开库。
# mycard 内核的 interpreter 默认只开 base/string/utf8/table/math，
# 而 pick.lua 需要 require/package 加载 2pick/*.lua、io.popen 读卡池、os.time 做种子。
# 该宏只在 interpreter.cpp 里被 #ifdef 使用，在 .cpp 顶部单独定义是安全的。
# ---------------------------------------------------------------------------
INTERPRETER_GUARD = ("#ifndef YGOPRO_NO_LUA_SAFE\n"
                     "#define YGOPRO_NO_LUA_SAFE\n"
                     "#endif\n")


def patch_interpreter_cpp(path):
    def go(s):
        if "#define YGOPRO_NO_LUA_SAFE" in s:
            raise SystemExit("[失败] interpreter.cpp 已定义 YGOPRO_NO_LUA_SAFE（应只在全新源码上运行）")
        print("  [OK] interpreter.cpp: 定义 YGOPRO_NO_LUA_SAFE（luaL_openlibs 全量开库）")
        return INTERPRETER_GUARD + s
    edit_file(path, go, "interpreter.cpp")


# ---------------------------------------------------------------------------
# ocgcore/scriptlib.h：成员函数声明
# ---------------------------------------------------------------------------
SCRIPTLIB_DECL = ("\t// 2pick\n"
                  "\tstatic int32_t duel_save_pick_deck(lua_State *L);\n"
                  "\tstatic int32_t duel_get_start_count(lua_State *L);\n"
                  "\tstatic int32_t duel_reset_time_limit(lua_State *L);\n"
                  "\tstatic int32_t duel_is_player_need_to_pick_deck(lua_State *L);\n"
                  "\tstatic int32_t duel_exile(lua_State *L);\n"
                  "\tstatic int32_t duel_pick_log(lua_State *L);\n")
SCRIPTLIB_ANCHOR = "\tstatic int32_t duel_enable_global_flag(lua_State *L);\n"


def patch_scriptlib_h(path):
    def go(s):
        return sub_once(s, SCRIPTLIB_ANCHOR, SCRIPTLIB_DECL + SCRIPTLIB_ANCHOR,
                        "scriptlib.h: 声明 6 个成员函数")
    edit_file(path, go, "scriptlib.h")


# ---------------------------------------------------------------------------
# ocgcore/libduel.cpp：6 个 Lua API + 注册
# ---------------------------------------------------------------------------
LIBDUEL_FUNCS = r"""// 2pick: 轮抽卡组落盘。牌序必须用 list_main 的真实顺序（rbegin->rend，
// 卡组顶到低），不能收 GetFieldGroup 的 group——其 container 按 cardid 排序，
// 丢失引擎切洗后的牌序，录像与手牌顺序会错。
int32_t scriptlib::duel_save_pick_deck(lua_State *L) {
	check_param_count(L, 1);
	int32_t playerid = (int32_t)lua_tointeger(L, 1);
	if(playerid != 0 && playerid != 1)
		luaL_error(L, "Parameter 1 should be 0 or 1.", 2);
	duel* pduel = interpreter::get_duel_info(L);
	auto& pl = pduel->game_field->player[playerid];
	pduel->write_buffer8(MSG_SAVE_PICK_DECK);
	pduel->write_buffer8((uint8_t)playerid);
	pduel->write_buffer8((uint8_t)(pl.list_main.size() + pl.list_extra.size()));
	for(auto cit = pl.list_main.rbegin(); cit != pl.list_main.rend(); ++cit)
		pduel->write_buffer32((*cit)->data.code);
	for(auto cit = pl.list_extra.rbegin(); cit != pl.list_extra.rend(); ++cit)
		pduel->write_buffer32((*cit)->data.code);
	return 0;
}
int32_t scriptlib::duel_get_start_count(lua_State *L) {
	check_param_count(L, 1);
	int32_t p = (int32_t)lua_tointeger(L, 1);
	if(p != 0 && p != 1)
		luaL_error(L, "Parameter 1 should be 0 or 1.", 2);
	duel* pduel = interpreter::get_duel_info(L);
	lua_pushinteger(L, pduel->game_field->player[p].start_count);
	return 1;
}
int32_t scriptlib::duel_reset_time_limit(lua_State *L) {
	check_param_count(L, 1);
	int32_t p = (int32_t)lua_tointeger(L, 1);
	int32_t time = 0;
	if(p != 0 && p != 1)
		luaL_error(L, "Parameter 1 should be 0 or 1.", 2);
	if(lua_gettop(L) >= 2)
		time = (int32_t)lua_tointeger(L, 2);
	if(time < 0 || time > 0x7fff)
		luaL_error(L, "Invalid time value.", 2);
	duel* pduel = interpreter::get_duel_info(L);
	pduel->write_buffer8(MSG_RESET_TIME);
	pduel->write_buffer8((uint8_t)p);
	pduel->write_buffer16((uint16_t)time);
	return 0;
}
int32_t scriptlib::duel_is_player_need_to_pick_deck(lua_State *L) {
	check_param_count(L, 1);
	int32_t p = (int32_t)lua_tointeger(L, 1);
	if(p != 0 && p != 1)
		luaL_error(L, "Parameter 1 should be 0 or 1.", 2);
	duel* pduel = interpreter::get_duel_info(L);
	lua_pushboolean(L, pduel->game_field->player[p].pick_needed);
	return 1;
}
int32_t scriptlib::duel_exile(lua_State *L) {
	check_action_permission(L);
	check_param_count(L, 2);
	card* pcard = nullptr;
	group* pgroup = nullptr;
	duel* pduel = nullptr;
	if(check_param(L, PARAM_TYPE_CARD, 1, TRUE)) {
		pcard = *(card**) lua_touserdata(L, 1);
		pduel = pcard->pduel;
	} else if(check_param(L, PARAM_TYPE_GROUP, 1, TRUE)) {
		pgroup = *(group**) lua_touserdata(L, 1);
		pduel = pgroup->pduel;
	} else
		return luaL_error(L, "Parameter 1 should be \"Card\" or \"Group\".");
	// 2pick: 未选卡「从游戏消失」（照抄 mycard/ygopro-2pick 的视觉行为）。
	// 候选卡是 CreateToken 建的真实卡（data.type 不带 TYPE_TOKEN），send_to
	// 在现代内核里不能用：LOCATION_REMOVED -> 真进除外区污染决斗；destination=0
	// -> MSG_MOVE 新控制者是 PLAYER_NONE，服务端 players[cc] 越界闪退；
	// 纯静默 remove_card -> 客户端看不到卡减少（旧内核的行为）。
	// 这里写内部消息 MSG_PICK_EXILE（single_duel.cpp 转成「目的地=位置0」的
	// MSG_MOVE 直发双方，客户端把卡从卡组视图渐隐移除），再静默 remove_card：
	// 卡不进除外区/墓地，引擎与对局零影响。
	card_set cs;
	if(pcard)
		cs.insert(pcard);
	else
		cs = pgroup->container;
	for(auto cit = cs.begin(); cit != cs.end(); ++cit) {
		card* pc = *cit;
		if(pc->current.location == 0)
			continue;
		pduel->write_buffer8(MSG_PICK_EXILE);
		pduel->write_buffer32(pc->data.code);
		pduel->write_buffer8(pc->current.controler);
		pduel->write_buffer8(pc->current.location);
		pduel->write_buffer8(pc->current.sequence);
		pduel->game_field->remove_card(pc);
	}
	return 0;
}
int32_t scriptlib::duel_pick_log(lua_State *L) {
	check_param_count(L, 2);
	check_param(L, PARAM_TYPE_GROUP, 2);
	int32_t playerid = (int32_t)lua_tointeger(L, 1);
	if(playerid != 0 && playerid != 1)
		luaL_error(L, "Parameter 1 should be 0 or 1.", 2);
	group* pgroup = *(group**)lua_touserdata(L, 2);
	duel* pduel = pgroup->pduel;
	if(pgroup->container.empty())
		return 0;
	// 2pick: 选卡私聊。只把卡号抛给 gframe，卡名由服务端查 cardnames.txt
	pduel->write_buffer8(MSG_PICK_LOG);
	pduel->write_buffer8((uint8_t)playerid);
	pduel->write_buffer8((uint8_t)pgroup->container.size());
	for(auto cit = pgroup->container.begin(); cit != pgroup->container.end(); ++cit)
		pduel->write_buffer32((*cit)->data.code);
	return 0;
}
"""

LIBDUEL_ANCHOR = "int32_t scriptlib::duel_enable_global_flag(lua_State *L) {"
LIBDUEL_REG = [("SavePickDeck", "duel_save_pick_deck"),
               ("GetStartCount", "duel_get_start_count"),
               ("ResetTimeLimit", "duel_reset_time_limit"),
               ("IsPlayerNeedToPickDeck", "duel_is_player_need_to_pick_deck"),
               ("Exile", "duel_exile"),
               ("PickLog", "duel_pick_log")]


def patch_libduel_cpp(path):
    def go(s):
        s = sub_once(s, LIBDUEL_ANCHOR, LIBDUEL_FUNCS + LIBDUEL_ANCHOR,
                     "libduel.cpp: 插入 6 个 Lua 函数")
        missing = []
        for name, fn in LIBDUEL_REG:
            good = '\t{ "%s", scriptlib::%s },\n' % (name, fn)
            if good in s:
                raise SystemExit("[失败] libduel.cpp 已存在注册项 %s（源码不是 virgin？）" % name)
            missing.append(good)
        reg = "static const struct luaL_Reg duellib[] = {\n"
        s = sub_once(s, reg, reg + "".join(missing),
                     "libduel.cpp: 注册 6 个 Lua API")
        return s
    edit_file(path, go, "libduel.cpp")


# ---------------------------------------------------------------------------
# gframe/single_duel.h：成员
# ---------------------------------------------------------------------------
def patch_single_duel_h(path):
    def go(s):
        anchor = "protected:\n"
        add = ("\t// 2pick\n"
               "\tDeck default_deck;\n"
               "\tDeck pick_deck[2];\n"
               "\tbool pick_deck_saved[2]{};\n"
               "\tvoid SwapPickDeck() {\n"
               "\t\tDeck d = pick_deck[0]; pick_deck[0] = pick_deck[1]; pick_deck[1] = d;\n"
               "\t\tbool b = pick_deck_saved[0]; pick_deck_saved[0] = pick_deck_saved[1]; pick_deck_saved[1] = b;\n"
               "\t}\n"
               "\tsize_t pick_replay_deck_pos{ 0 };\n")
        return sub_once(s, anchor, anchor + add, "single_duel.h: pick_deck 成员")
    edit_file(path, go, "single_duel.h")


# ---------------------------------------------------------------------------
# gframe/replay.h：授权 SingleDuel 读写录像缓冲（轮抽录像重建用）
# ---------------------------------------------------------------------------
def patch_replay_h(path):
    def go(s):
        return sub_once(s, "class Replay {\npublic:\n",
                        "class Replay {\n\tfriend class SingleDuel;\npublic:\n",
                        "replay.h: friend class SingleDuel")
    edit_file(path, go, "replay.h")


# ---------------------------------------------------------------------------
# gframe/single_duel.cpp
# ---------------------------------------------------------------------------
INCLUDE_BLOCK = ("// ==== 2pick: 辅助头文件（必须在 namespace ygo 之前）====\n"
                 "#include <fstream>\n"
                 "#include <map>\n"
                 "#include <string>\n"
                 "#include <cstdlib>\n\n")

# 卡名表 + 私聊发送 + 补发诊断（放在 namespace ygo 内、SingleDuel::Chat 之前）
HELPER_BLOCK = """// ==== 2pick: 选卡私聊（MSG_PICK_LOG -> 只发给该玩家的 STOC_CHAT）====
namespace {

// 卡号 -> UTF-8 卡名，来源 2pick/cardnames.txt（make_cardnames.py 从 408.cdb
// 的 texts 表导出）。ygopro 的 strings.conf 不存卡名（在 cards.cdb texts 表）。
const std::map<uint32_t, std::string>& PickChatNames() {
	static std::map<uint32_t, std::string> names;
	static bool loaded = false;
	if(loaded)
		return names;
	loaded = true;
	// ygopro 子进程的 cwd 是 .../ygopro（srvpro 用 spawn('./ygopro', {cwd:'ygopro'})），
	// 其它几项只是兜底。
	const char* const candidates[] = {
		"./2pick/cardnames.txt",
		"2pick/cardnames.txt",
		"./cardnames.txt",
		"cardnames.txt",
		"../2pick/cardnames.txt",
		"../ygopro/2pick/cardnames.txt",
		"/root/ygopro-2pick/ygopro/2pick/cardnames.txt",
		nullptr
	};
	std::ifstream fin;
	for(int i = 0; candidates[i]; ++i) {
		fin.open(candidates[i], std::ios::binary);
		if(fin.is_open())
			break;
	}
	if(!fin.is_open())
		return names;
	std::string line;
	while(std::getline(fin, line)) {
		if(line.empty() || line[0] == '#')
			continue;
		size_t p = 0;
		if(line.compare(0, 5, "!name") == 0)
			p = 5;
		while(p < line.size() && (line[p] == ' ' || line[p] == '\\t'))
			++p;
		size_t q = p;
		while(q < line.size() && line[q] >= '0' && line[q] <= '9')
			++q;
		if(q == p)
			continue;
		const uint32_t code = (uint32_t)std::strtoul(line.substr(p, q - p).c_str(), nullptr, 10);
		while(q < line.size() && (line[q] == ' ' || line[q] == '\\t'))
			++q;
		if(code == 0 || q >= line.size())
			continue;
		std::string name = line.substr(q);
		while(!name.empty() && (name.back() == '\\r' || name.back() == '\\n'))
			name.pop_back();
		if(!name.empty())
			names[code] = name;
	}
	return names;
}

// UTF-8 -> UTF-16（只认合法的 1~4 字节序列，坏字节直接跳过）
std::u16string PickChatToUtf16(const std::string& src) {
	std::u16string out;
	size_t i = 0;
	const size_t n = src.size();
	while(i < n) {
		const unsigned char c = (unsigned char)src[i];
		uint32_t cp = 0;
		size_t extra = 0;
		if(c < 0x80) {
			cp = c;
		} else if((c & 0xE0) == 0xC0) {
			cp = c & 0x1F;
			extra = 1;
		} else if((c & 0xF0) == 0xE0) {
			cp = c & 0x0F;
			extra = 2;
		} else if((c & 0xF8) == 0xF0) {
			cp = c & 0x07;
			extra = 3;
		} else {
			++i;
			continue;
		}
		if(i + extra >= n)
			break;
		for(size_t k = 1; k <= extra; ++k)
			cp = (cp << 6) | ((unsigned char)src[i + k] & 0x3F);
		i += extra + 1;
		if(cp > 0xFFFF) {
			cp -= 0x10000;
			out.push_back((char16_t)(0xD800 + (cp >> 10)));
			out.push_back((char16_t)(0xDC00 + (cp & 0x3FF)));
		} else {
			out.push_back((char16_t)cp);
		}
	}
	return out;
}

// STOC_CHAT 必须走 NetServer::CreateChatPacket（与 SingleDuel::Chat 同源）：
// 它写 uint16 player、复制完整 UTF-16 文本（含结尾 0）并返回准确包长；
// 手写变长包缺结尾 0 会被 srvpro 当不完整包丢弃。
void PickChatSend(DuelPlayer* dp, uint16_t seat, const std::u16string& text) {
	if(!dp)
		return;
	size_t n = text.size();
	if(n > (size_t)LEN_CHAT_MSG - 1)
		n = (size_t)LEN_CHAT_MSG - 1;
	uint16_t msg[LEN_CHAT_MSG]{};
	for(size_t i = 0; i < n; ++i)
		msg[i] = (uint16_t)text[i];
	msg[n] = 0;
	const int src_size = (int)((n + 1) * sizeof(uint16_t));
	unsigned char scc[SIZE_STOC_CHAT]{};
	const auto scc_size = NetServer::CreateChatPacket((unsigned char*)msg, src_size, scc, seat);
	if(!scc_size)
		return;
	NetServer::SendBufferToPlayer(dp, STOC_CHAT, scc, (int)scc_size);
}

// 卡号补发探针：只写 /tmp/2pick-sync.log，验证补发分支确实在跑。
// 绝不碰 stderr——srvpro 会把 ygopro 子进程的 stderr 广播成聊天消息。
static const char k2pickSyncLogPath[] __attribute__((used)) = "/tmp/2pick-sync.log";
static void PickSyncTrace(int pp, int loc, int qlen) __attribute__((used));
static void PickSyncTrace(int pp, int loc, int qlen) {
	static bool done = false;
	if(done)
		return;
	done = true;
	std::ofstream fout(k2pickSyncLogPath, std::ios::app);
	if(fout)
		fout << "2pick sync active pp=" << pp << " loc=" << loc << " bytes=" << qlen << "\\n";
}

}  // namespace

"""

# Analyze 的新增 case：依次是 未选卡离场 / 轮抽卡组落盘(+录像重建) / 时限 / 私聊+卡号补发
ANALYZE_CASES = r"""		case MSG_PICK_EXILE: {
			// 2pick: 未选卡离场 -> 复刻上游视觉「卡从卡组直接消失」：
			// 发目的地=位置0 的 MSG_MOVE，客户端走「渐隐+移除」分支
			//（与场上衍生物消失同一代码路径）。引擎侧已静默 remove_card，
			// 卡不进除外区/墓地。目的地控制者必须写 0/1（直发按它索引 players），
			// 新姿态写 POS_FACEDOWN，对手侧卡号写 0 防泄露。
			uint32_t exile_code = BufferIO::Read<uint32_t>(pbuf);
			uint8_t epc = BufferIO::Read<uint8_t>(pbuf);
			uint8_t epl = BufferIO::Read<uint8_t>(pbuf);
			uint8_t eps = BufferIO::Read<uint8_t>(pbuf);
			unsigned char movebuf[17];
			unsigned char* mbuf = movebuf;
			BufferIO::Write<uint8_t>(mbuf, MSG_MOVE);
			BufferIO::Write<uint32_t>(mbuf, exile_code);
			BufferIO::Write<uint8_t>(mbuf, epc);
			BufferIO::Write<uint8_t>(mbuf, epl);
			BufferIO::Write<uint8_t>(mbuf, eps);
			BufferIO::Write<uint8_t>(mbuf, POS_FACEDOWN);
			BufferIO::Write<uint8_t>(mbuf, epc);
			BufferIO::Write<uint8_t>(mbuf, 0);
			BufferIO::Write<uint8_t>(mbuf, 0);
			BufferIO::Write<uint8_t>(mbuf, POS_FACEDOWN);
			BufferIO::Write<uint32_t>(mbuf, REASON_RULE);
			NetServer::SendBufferToPlayer(players[epc], STOC_GAME_MSG, movebuf, 17);
			unsigned char* hbuf = movebuf + 1;
			BufferIO::Write<uint32_t>(hbuf, 0);
			NetServer::SendBufferToPlayer(players[1 - epc], STOC_GAME_MSG, movebuf, 17);
			for(auto oit = observers.begin(); oit != observers.end(); ++oit)
				NetServer::ReSendToPlayer(*oit);
#ifdef YGOPRO_SERVER_MODE
			NetServer::ReSendToPlayers(cache_recorder, replay_recorder);
#endif
			break;
		}
		case MSG_SAVE_PICK_DECK: {
			player = BufferIO::Read<uint8_t>(pbuf);
			count = BufferIO::Read<uint8_t>(pbuf);
			uint32_t cardlist[128]{};
			for(int i = 0; i < count && i < 128; ++i)
				cardlist[i] = BufferIO::Read<uint32_t>(pbuf);
			Deck tdeck;
			deckManager.LoadDeck(tdeck, cardlist, count, 0);
			pick_deck[player] = tdeck;
			pick_deck_saved[player] = true;
			// 2pick: 轮抽录像重建。官方 TPResult 在 start_duel 前就把 pdeck 写进
			// last_replay，第一局那时还是占位卡组，且缓冲里已堆满轮抽选卡响应。
			// 双方到齐后把缓冲截断回写卡组前的偏移（丢弃占位卡组+选卡响应），
			// 再按真实牌序（pick.lua 先 ShuffleDeck 再 SaveDeck）重写卡组段。
			if(pick_deck_saved[0] && pick_deck_saved[1] && pick_replay_deck_pos > 0) {
				last_replay.replay_size = pick_replay_deck_pos;
				for(int i = 0; i < 2; ++i) {
					last_replay.WriteInt32((int32_t)pick_deck[i].main.size(), false);
					for(auto cit = pick_deck[i].main.rbegin(); cit != pick_deck[i].main.rend(); ++cit)
						last_replay.WriteInt32((*cit)->code, false);
					last_replay.WriteInt32((int32_t)pick_deck[i].extra.size(), false);
					for(auto cit = pick_deck[i].extra.rbegin(); cit != pick_deck[i].extra.rend(); ++cit)
						last_replay.WriteInt32((*cit)->code, false);
				}
				last_replay.Flush();
				pick_replay_deck_pos = 0;
				// 2pick: 录像卡组段已固化为引擎真实牌序，把内核 RNG 复位到建局起点。
				// 回放客户端从 create_duel_v2(seed) 起步且不重演选卡（选卡响应已截断），
				// 而本局选卡期的 Duel.ShuffleDeck 消耗过内核随机数；不复位会让之后
				// 洗手牌/掷币/随机丢弃等随机事件与回放逐位错开（丢错牌、卡片串位）。
				// 只在本局双方卡组都到齐、录像重建完成时执行一次；M# 后续局不轮抽，
				// SaveDeck 有守卫不发本消息，其 RNG 本来就从起点开始，无需复位。
				reset_random(pduel);
			}
			break;
		}
		case MSG_RESET_TIME: {
			player = BufferIO::Read<uint8_t>(pbuf);
			int16_t time = BufferIO::Read<int16_t>(pbuf);
			if(host_info.time_limit) {
				if(time) time_limit[player] = time;
				else time_limit[player] = host_info.time_limit;
			}
			break;
		}
		case MSG_PICK_LOG: {
			// 2pick: 选卡私聊 -> 拼一行卡名 STOC_CHAT 只发本人
			player = BufferIO::Read<uint8_t>(pbuf);
			count = BufferIO::Read<uint8_t>(pbuf);
			if(player != 0 && player != 1)
				player = 0;
			if(count > 0 && count <= 127) {
				const std::map<uint32_t, std::string>& names = PickChatNames();
				std::u16string msg = PickChatToUtf16(names.empty()
					? "【轮抽】本轮选卡（没找到 cardnames.txt，下面显示卡号）："
					: "【轮抽】本轮选卡：");
				for(int i = 0; i < count; ++i) {
					const uint32_t code = BufferIO::Read<uint32_t>(pbuf);
					if(i > 0)
						msg += PickChatToUtf16("、");
					const auto it = names.find(code);
					if(it != names.end())
						msg += PickChatToUtf16(it->second);
					else
						msg += PickChatToUtf16("#" + std::to_string((unsigned long long)code));
				}
				PickChatSend(players[player], player, msg);
			} else {
				pbuf += count * 4;
			}
			// 2pick: 卡号补发。MDPro3 卡组列表按本地 GameCard 的卡号画图，0 = 卡背；
			// 候选卡的 MSG_MOVE/MSG_SHUFFLE_DECK 会清 code 且官方从不刷新主卡组。
			// 逐卡 MSG_UPDATE_CARD（带 GPS）：客户端按 GPS 定位、卡不存在就跳过；
			// 绝不用批量 MSG_UPDATE_DATA——客户端 UpdateFieldCard 无容错会闪退。
			for(int pp = 0; pp < 2; ++pp) {
				if(!players[pp])
					continue;
				const uint8_t pick_locs[2] = { LOCATION_DECK, LOCATION_EXTRA };
				const int pick_max[2] = { 60, 15 };
				for(int li = 0; li < 2; ++li) {
					for(int seq = 0; seq < pick_max[li]; ++seq) {
						unsigned char qbuf[SIZE_QUERY_BUFFER]{};
						unsigned char* q = qbuf;
						BufferIO::Write<uint8_t>(q, MSG_UPDATE_CARD);
						BufferIO::Write<uint8_t>(q, (uint8_t)pp);
						BufferIO::Write<uint8_t>(q, pick_locs[li]);
						BufferIO::Write<uint8_t>(q, (uint8_t)seq);
						const int len = query_card(pduel, pp, pick_locs[li], (uint8_t)seq, QUERY_CODE | QUERY_POSITION, q, 0);
						if(len > LEN_HEADER)
							NetServer::SendBufferToPlayer(players[pp], STOC_GAME_MSG, qbuf, len + 4);
					}
				}
				PickSyncTrace(pp, pick_locs[0], 0);
			}
			break;
		}
"""

ANALYZE_ANCHOR = "\t\tswitch (engType) {\n\t\tcase MSG_RETRY: {\n"
ANALYZE_NEW = ("\t\tswitch (engType) {\n" + ANALYZE_CASES + "\t\tcase MSG_RETRY: {\n")

# 第二局跳过换 side：换 side 界面显示进房时提交的卡组（非轮抽结果），
# 必然 SIDEERROR。照抄上游 DuelEndProc：直接 STOC_DUEL_START + 给败者发 STOC_SELECT_TP。
SIDING_OLD = (
    "\t\t\tif(players[0] != pplayer[0]) {\n"
    "\t\t\t\tplayers[0] = pplayer[0];\n"
    "\t\t\t\tplayers[1] = pplayer[1];\n"
    "\t\t\t\tplayers[0]->type = 0;\n"
    "\t\t\t\tplayers[1]->type = 1;\n"
    "\t\t\t\tDeck d = pdeck[0];\n"
    "\t\t\t\tpdeck[0] = pdeck[1];\n"
    "\t\t\t\tpdeck[1] = d;\n"
    "\t\t\t}\n"
    "\t\t\tready[0] = false;\n"
    "\t\t\tready[1] = false;\n"
    "\t\t\tplayers[0]->state = CTOS_UPDATE_DECK;\n"
    "\t\t\tplayers[1]->state = CTOS_UPDATE_DECK;\n"
    "\t\t\tNetServer::SendPacketToPlayer(players[0], STOC_CHANGE_SIDE);\n"
    "\t\t\tNetServer::SendPacketToPlayer(players[1], STOC_CHANGE_SIDE);\n"
    "\t\t\tfor(auto oit = observers.begin(); oit != observers.end(); ++oit)\n"
    "\t\t\t\tNetServer::SendPacketToPlayer(*oit, STOC_WAITING_SIDE);\n"
    "#ifdef YGOPRO_SERVER_MODE\n"
    "\t\t\tif(cache_recorder)\n"
    "\t\t\t\tNetServer::SendPacketToPlayer(cache_recorder, STOC_WAITING_SIDE);\n"
    "\t\t\tif(replay_recorder)\n"
    "\t\t\t\tNetServer::SendPacketToPlayer(replay_recorder, STOC_WAITING_SIDE);\n"
    "#endif\n"
    "\t\t\tduel_stage = DUEL_STAGE_SIDING;\n")
SIDING_NEW = (
    "\t\t\t// 2pick: 第二局跳过换 side（照抄 mycard/ygopro-2pick 的 DuelEndProc）。\n"
    "\t\t\t// 换 side 界面显示的是进房时提交的卡组而非轮抽结果，点完成必然 SIDEERROR。\n"
    "\t\t\t// 交换 players/pdeck 时轮抽卡组也要一起换（SwapPickDeck）。\n"
    "\t\t\tif(players[0] != pplayer[0]) {\n"
    "\t\t\t\tplayers[0] = pplayer[0];\n"
    "\t\t\t\tplayers[1] = pplayer[1];\n"
    "\t\t\t\tplayers[0]->type = 0;\n"
    "\t\t\t\tplayers[1]->type = 1;\n"
    "\t\t\t\tDeck d = pdeck[0];\n"
    "\t\t\t\tpdeck[0] = pdeck[1];\n"
    "\t\t\t\tpdeck[1] = d;\n"
    "\t\t\t\tSwapPickDeck();\n"
    "\t\t\t}\n"
    "\t\t\tready[0] = false;\n"
    "\t\t\tready[1] = false;\n"
    "\t\t\tfor(auto oit = observers.begin(); oit != observers.end(); ++oit)\n"
    "\t\t\t\tNetServer::SendPacketToPlayer(*oit, STOC_WAITING_SIDE);\n"
    "#ifdef YGOPRO_SERVER_MODE\n"
    "\t\t\tif(cache_recorder)\n"
    "\t\t\t\tNetServer::SendPacketToPlayer(cache_recorder, STOC_WAITING_SIDE);\n"
    "\t\t\tif(replay_recorder)\n"
    "\t\t\t\tNetServer::SendPacketToPlayer(replay_recorder, STOC_WAITING_SIDE);\n"
    "#endif\n"
    "\t\t\tNetServer::SendPacketToPlayer(players[0], STOC_DUEL_START);\n"
    "\t\t\tNetServer::ReSendToPlayer(players[1]);\n"
    "\t\t\t// tp_player = 上一局败者；语义与 UpdateDeck() 第二局分支完全相同。\n"
    "\t\t\tNetServer::SendPacketToPlayer(players[tp_player], STOC_SELECT_TP);\n"
    "\t\t\tplayers[1 - tp_player]->state = 0xff;\n"
    "\t\t\tplayers[tp_player]->state = CTOS_TP_RESULT;\n"
    "\t\t\tduel_stage = DUEL_STAGE_FIRSTGO;\n")

# TPResult：写录像卡组段前记偏移（避免硬编码 96 字节头长度）
REPLAY_POS_ANCHOR = "\tauto load = [&](const std::vector<const CardDataC*>& deck_container, uint8_t p, uint8_t location) {\n"

CTOR_OLD = ("SingleDuel::SingleDuel(bool is_match) {\n"
            "\tmatch_mode = is_match;\n"
            "}")
CTOR_NEW = ("SingleDuel::SingleDuel(bool is_match) {\n"
            "\tmatch_mode = is_match;\n"
            "\t// 2pick: 占位卡组，真正的卡组由 Lua 轮抽后写回\n"
            "\tuint32_t cardlist[128]{};\n"
            "\tcardlist[0] = 89631139;\n"
            "\tdeckManager.LoadDeck(default_deck, cardlist, 1, 0);\n"
            "}")

FINGER_ANCHOR = "\tduel_stage = DUEL_STAGE_FINGER;\n"

SWAP_OLD = "\t\tstd::swap(pdeck[0], pdeck[1]);\n\t\tswapped = true;\n"
SWAP_NEW = "\t\tstd::swap(pdeck[0], pdeck[1]);\n\t\tSwapPickDeck();\n\t\tswapped = true;\n"

PDECK_ANCHOR = "\tdp->state = CTOS_RESPONSE;\n\tstd::random_device rd;\n"
PDECK_NEW = ("\tdp->state = CTOS_RESPONSE;\n"
             "\t// 2pick: MATCH 第二局起用上一局轮抽出的卡组\n"
             "\tfor(int i = 0; i < 2; ++i) {\n"
             "\t\tif(pick_deck_saved[i]) pdeck[i] = pick_deck[i];\n"
             "\t\telse pdeck[i] = default_deck;\n"
             "\t}\n"
             "\tstd::random_device rd;\n")

SPI_OLD = ("\tset_player_info(pduel, 0, host_info.start_lp, host_info.start_hand, host_info.draw_count);\n"
           "\tset_player_info(pduel, 1, host_info.start_lp, host_info.start_hand, host_info.draw_count);\n")
SPI_NEW = ("\t// 2pick：第 5 个参数 = 本局该玩家是否要现场轮抽（false = 用上一局抽好的卡组）\n"
           "\tset_player_info(pduel, 0, host_info.start_lp, host_info.start_hand, host_info.draw_count, !pick_deck_saved[0]);\n"
           "\tset_player_info(pduel, 1, host_info.start_lp, host_info.start_hand, host_info.draw_count, !pick_deck_saved[1]);\n")

CONFIRM_OLD = "if(pbuf[5] != LOCATION_DECK) {"
CONFIRM_NEW = "if(pbuf[5] != LOCATION_DECK && pbuf[5] != LOCATION_EXTRA) {"
CONFIRM_NOTE = ("// 2pick: 额外卡组的确认卡片也只单播本人（官方对非 DECK 位置广播，\n"
                "// 会让对手收到没人等待的阻塞弹窗并泄露选卡）\n")


def patch_single_duel_cpp(path):
    def go(s):
        # 辅助头文件放文件最开头（放 namespace 内会产生 ygo::std，全文件编译失败）
        s = INCLUDE_BLOCK + s
        # helper（卡名表/私聊发送/补发探针）
        chat_anchor = "void SingleDuel::Chat(DuelPlayer* dp, unsigned char* pdata, int len) {"
        s = sub_once(s, chat_anchor, HELPER_BLOCK + chat_anchor,
                     "single_duel.cpp: 卡名表 + 私聊发送 helper")
        # 占位卡组
        s = sub_once(s, CTOR_OLD, CTOR_NEW, "single_duel.cpp: 占位卡组")
        # 每局猜拳后重置轮抽状态
        s = sub_once(s, FINGER_ANCHOR,
                     "\tpick_deck_saved[0] = false;\n\tpick_deck_saved[1] = false;\n" + FINGER_ANCHOR,
                     "single_duel.cpp: 每局重置轮抽状态")
        # 交换先后攻时同步交换轮抽卡组
        s = sub_once(s, SWAP_OLD, SWAP_NEW, "single_duel.cpp: SwapPickDeck")
        # 开局前用轮抽结果覆盖 pdeck（必须在 shuffle 之前）
        s = sub_once(s, PDECK_ANCHOR, PDECK_NEW, "single_duel.cpp: 覆盖 pdeck")
        # TPResult 记录像卡组段偏移
        s = sub_once(s, REPLAY_POS_ANCHOR,
                     "\tpick_replay_deck_pos = last_replay.replay_size;\n" + REPLAY_POS_ANCHOR,
                     "single_duel.cpp: TPResult 记录 replay 卡组段偏移")
        # 第二局跳过换 side
        s = sub_once(s, SIDING_OLD, SIDING_NEW, "single_duel.cpp: 第二局跳过换 side")
        # set_player_info 第 5 参数
        s = sub_once(s, SPI_OLD, SPI_NEW, "single_duel.cpp: set_player_info 第 5 参数")
        # Analyze 新增 4 个 case
        s = sub_once(s, ANALYZE_ANCHOR, ANALYZE_NEW, "single_duel.cpp: Analyze 新增 4 个 case")
        # MSG_CONFIRM_CARDS 额外卡组单播
        if s.count(CONFIRM_OLD) != 1:
            raise SystemExit("[失败] MSG_CONFIRM_CARDS 锚点命中 %d 次（应为 1）" % s.count(CONFIRM_OLD))
        s = s.replace(CONFIRM_OLD, CONFIRM_NOTE + CONFIRM_NEW, 1)
        print("  [OK] single_duel.cpp: MSG_CONFIRM_CARDS 对额外卡组只单播本人")
        # 自检：include 必须在 namespace ygo 之前
        import re
        m_ns = re.search(r"(?m)^[ \t]*namespace[ \t]+ygo\b", s)
        if m_ns and s.index("#include <fstream>") > m_ns.start():
            raise SystemExit("[失败] 辅助 #include 落在 namespace ygo 之后（会产生 ygo::std）")
        return s
    edit_file(path, go, "single_duel.cpp")


# ---------------------------------------------------------------------------
# special.lua：放 expansions/script/ 下（内核 ScriptReaderEx 服务器模式"expansions 优先"，
# 放这里才会被 preload_script 生效；放 script/ 会被 4088 遗留的 expansions 版遮蔽）
# ---------------------------------------------------------------------------
SPECIAL_EXTRA_ZONE = """
-- ==== 408 环境全局规则：禁用额外怪兽区（0x600060 = 双方两个 EMZ 格子，
-- 让融合/同调/超量怪由内核直接落到主怪兽区）====
local e1=Effect.GlobalEffect()
e1:SetType(EFFECT_TYPE_FIELD)
e1:SetCode(EFFECT_DISABLE_FIELD)
e1:SetValue(0x600060)
Duel.RegisterEffect(e1,0)
"""

SPECIAL_PICK_RULE = """
-- ==== 2pick 轮抽规则挂载 ====
do
	local ok, err = pcall(function()
		local old_path = package.path
		package.path = "./?.lua;" .. package.path
		require("./2pick/pick")
		Auxiliary.Load2PickRule()
		package.path = old_path
	end)
	if not ok then
		local lg = io.open("/tmp/2pick-load.log", "a")
		if lg then
			lg:write(os.date("%Y-%m-%d %H:%M:%S") .. " 2pick mount failed: " .. tostring(err) .. "\\n")
			lg:close()
		end
	end
end
"""


def patch_special_lua(root):
    # 内核 ScriptReaderEx 在服务器模式下读 ./script/xxx.lua 时"expansions 优先"：
    # ./expansions/script/special.lua 存在就会遮蔽 ./script/special.lua。
    # 因此统一把 special.lua 放 expansions/script/ 下（4088 遗留的同名文件也会被覆盖）。
    path = os.path.join(root, "expansions", "script", "special.lua")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and "Auxiliary.Load2PickRule()" in read(path):
        print("  [跳过] expansions/script/special.lua 规则已齐全")
        print("[改] %s" % path)
        return
    s = "-- created by port_2pick.py\n\n" + SPECIAL_EXTRA_ZONE + "\n" + SPECIAL_PICK_RULE
    write(path, s)
    print("  [OK] special.lua: 禁用额外怪兽区 + 2pick 规则挂载（expansions/script/）")
    print("[改] %s" % path)


# ---------------------------------------------------------------------------
def copy_resources(root, pack):
    src = os.path.join(pack, "2pick")
    dst = os.path.join(root, "2pick")
    if not os.path.isdir(src):
        raise SystemExit("[失败] 部署包里没有 2pick/：%s" % src)
    os.makedirs(dst, exist_ok=True)
    for name in sorted(os.listdir(src)):
        if ".bak" in name or name.startswith("backup-"):
            continue
        p = os.path.join(src, name)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dst, name))
            print("[资源] 2pick/%s" % name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="ygopro server 分支源码根目录（例如 /root/ygopro-2pick/src）")
    ap.add_argument("--pack", default=os.path.dirname(os.path.abspath(__file__)),
                    help="部署包目录（含 2pick/ 资源）")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    pack = os.path.abspath(a.pack)
    print("目标源码：%s" % root)
    for p in ("ocgcore/common.h", "ocgcore/libduel.cpp", "ocgcore/scriptlib.h",
              "ocgcore/interpreter.cpp", "ocgcore/field.h", "ocgcore/duel.h",
              "ocgcore/duel.cpp", "ocgcore/ocgapi.h", "ocgcore/ocgapi.cpp",
              "gframe/single_duel.h", "gframe/single_duel.cpp", "gframe/replay.h"):
        if not os.path.exists(os.path.join(root, p)):
            raise SystemExit("[失败] 缺少 %s，--root 指错了？或子模块没拉全？" % p)

    patch_common_h(os.path.join(root, "ocgcore", "common.h"))
    patch_field_h(os.path.join(root, "ocgcore", "field.h"))
    patch_duel_h(os.path.join(root, "ocgcore", "duel.h"))
    patch_ocgapi_h(os.path.join(root, "ocgcore", "ocgapi.h"))
    patch_ocgapi_cpp(os.path.join(root, "ocgcore", "ocgapi.cpp"))
    patch_interpreter_cpp(os.path.join(root, "ocgcore", "interpreter.cpp"))
    patch_scriptlib_h(os.path.join(root, "ocgcore", "scriptlib.h"))
    patch_libduel_cpp(os.path.join(root, "ocgcore", "libduel.cpp"))
    patch_single_duel_h(os.path.join(root, "gframe", "single_duel.h"))
    patch_replay_h(os.path.join(root, "gframe", "replay.h"))
    patch_single_duel_cpp(os.path.join(root, "gframe", "single_duel.cpp"))
    patch_special_lua(root)
    copy_resources(root, pack)

    print("\n全部补丁应用完成。备份后缀：.bak-%s" % STAMP)
    print("接下来编译（在 %s 下）：" % root)
    print("  ./premake5 gmake --lua-deb")
    print('  cd build && make config=release -j$(nproc) CXXFLAGS="-std=c++14" && cd ..')
    print("  strip ygopro")


if __name__ == "__main__":
    main()
