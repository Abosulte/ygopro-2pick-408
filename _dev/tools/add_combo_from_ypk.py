#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 ypk 里读出卡号，追加成一组 2pick 主题包到 combo.lua。

双击同目录的 add-combo.bat 运行（打开即弹文件选择窗口，无需命令行）：
    ① 选 ypk    ② 选 combo.lua    ③ 确认后写入

ypk 里识别卡号的来源（按优先级）：
    *.ydk 卡组文件  >  *.cdb 的 datas 表（超过 50 张卡的环境数据库会被忽略）
    >  pics/12345.jpg 或 script/c12345.lua 这类文件名
一包 3~6 张都可以（官方主题包是 6 张，艾克佐迪亚五件套这类俗称「5 张套」）；
卡数在 3~6 之间直接写入，超出范围会弹列表窗口让你挑选。

安全：写入前自动备份 combo.lua 与（需要改动时）pick.lua；
空 combo.lua 时 pick.lua 的 ENABLE_COMBO 是 false，添加第一组后会自动改成 true。
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections import Counter

MIN_PACK, MAX_PACK = 3, 6   # 一组主题包的卡数范围（官方是 6 张；5 张套也支持）


def unique(pairs):
    """(卡号, 来源) 列表 -> 保序去重的卡号列表。"""
    seen, out = set(), []
    for code, _ in pairs:
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def collect_from_ypk(path):
    """返回 (卡号列表, {卡号: 卡名}, 说明文字)。"""
    with zipfile.ZipFile(path) as z:
        ydk, cdb, fnames, cdb_names = [], [], [], {}
        for n in z.namelist():
            low = n.lower()
            if low.endswith(".ydk"):
                text = z.read(n).decode("utf-8", "replace")
                for line in text.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        ydk.append((int(line), n))
            elif low.endswith(".cdb"):
                fd, tmp = tempfile.mkstemp(suffix=".cdb")
                os.close(fd)
                try:
                    with open(tmp, "wb") as f:
                        f.write(z.read(n))
                    conn = sqlite3.connect(tmp)
                    try:
                        for (code,) in conn.execute("SELECT id FROM datas"):
                            cdb.append((code, n))
                        cdb_names.update(dict(conn.execute("SELECT id, name FROM texts")))
                    except sqlite3.Error:
                        pass
                    finally:
                        conn.close()
                finally:
                    os.remove(tmp)
            else:
                m = re.match(r"^c?(\d{1,12})\.(lua|jpg|jpeg|png|webp)$",
                             os.path.basename(n), re.I)
                if m:
                    fnames.append((int(m.group(1)), n))

        buckets = []
        if ydk:
            buckets.append(("ydk 卡组文件", ydk))
        if cdb and len(cdb) <= 50:
            buckets.append(("cdb 卡库", cdb))
        if fnames:
            buckets.append(("卡图/脚本文件名", fnames))
        if not buckets:
            return [], {}, "ypk 里没找到 ydk / cdb / 卡图 / 脚本文件名"

        # 不去重：一个主题包允许放 2~3 张同名卡（例如 3 张「青眼白龙」），
        # 按卡号去重会把它们误吞掉，导致卡数不足。
        for label, pairs in buckets:
            codes = [c for c, _ in pairs]
            if MIN_PACK <= len(codes) <= MAX_PACK:
                return codes, cdb_names, "%s（%d 张，允许同名重复）" % (label, len(codes))
        merged = [c for _, pairs in buckets for c, _ in pairs]
        return merged, cdb_names, "合并全部来源（%d 张，需手动挑 %d~%d 张）" % (
            len(merged), MIN_PACK, MAX_PACK)


def load_names(combo_path, cdb_names):
    """优先用 combo.lua 同目录的 408.cdb / cards.cdb 查中文卡名。"""
    d = os.path.dirname(os.path.abspath(combo_path))
    for fn in ("408.cdb", "cards.cdb"):
        p = os.path.join(d, fn)
        if os.path.exists(p):
            try:
                conn = sqlite3.connect(p)
                names = dict(conn.execute("SELECT id, name FROM texts"))
                conn.close()
                if names:
                    return names
            except sqlite3.Error:
                pass
    return cdb_names


def load_limits(combo_path):
    """读同目录 limits.json（由 make_limits.py 生成）。"""
    p = os.path.join(os.path.dirname(os.path.abspath(combo_path)), "limits.json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def check_limits(codes, limits, names):
    """返回 (禁止卡文本, 超额文本)。按卡号独立计数（不做 alias 合并）。"""
    if not limits:
        return "", ""
    forbidden = set(limits.get("forbidden", []))
    limit1 = set(limits.get("limit1", []))
    limit2 = set(limits.get("limit2", []))
    cnt = Counter(codes)
    banned, over = [], []
    for code, n in cnt.items():
        label = "%s(%s)" % (names.get(code, code), code)
        if code in forbidden:
            banned.append(label)
            continue
        cap = 1 if code in limit1 else 2 if code in limit2 else 3
        if n > cap:
            over.append("%s x%d（上限 %d）" % (label, n, cap))
    return ("、".join(banned), "；".join(over))


def append_pack(combo_path, ypk_name, codes, stamp=None):
    """把一组卡追加进 combo.lua；返回 (备份文件, 是否顺带开启了 ENABLE_COMBO)。"""
    stamp = stamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(combo_path, "r", encoding="utf-8") as f:
        s = f.read()
    marker = "combo_pack.pack = {"
    i = s.find(marker)
    j = s.find("}", i) if i >= 0 else -1
    if i < 0 or j < 0:
        raise ValueError("combo.lua 里找不到 'combo_pack.pack = { ... }' 结构")

    backup = combo_path + ".bak-" + stamp
    shutil.copy2(combo_path, backup)
    insert = "\t-- %s\n\t{%s},\n" % (ypk_name, ",".join(str(c) for c in codes))
    new = s[:j].rstrip("\n") + "\n" + insert + "}\n\n" + s[j + 1:].lstrip("\n")
    with open(combo_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)

    enabled = False
    pick = os.path.join(os.path.dirname(os.path.abspath(combo_path)), "pick.lua")
    if os.path.exists(pick):
        with open(pick, "r", encoding="utf-8") as f:
            t = f.read()
        t2 = re.sub(r"(local ENABLE_COMBO\s*=\s*)false", r"\1true", t, count=1)
        if t2 != t:
            shutil.copy2(pick, pick + ".bak-" + stamp)
            with open(pick, "w", encoding="utf-8", newline="\n") as f:
                f.write(t2)
            enabled = True
    return backup, enabled


def choose_cards(root, codes, names):
    """卡数超出 3~6 张时，弹列表窗口让用户勾选。"""
    import tkinter as tk
    from tkinter import messagebox, ttk

    win = tk.Toplevel(root)
    win.title("选择 %d~%d 张卡" % (MIN_PACK, MAX_PACK))
    win.geometry("460x420")
    ttk.Label(win, text="检测到 %d 张卡，请勾选 %d~%d 张：" %
              (len(codes), MIN_PACK, MAX_PACK)).pack(pady=6)
    lb = tk.Listbox(win, selectmode=tk.EXTENDED, width=56, height=18)
    lb.pack(fill=tk.BOTH, expand=True, padx=10)
    for c in codes:
        lb.insert(tk.END, "%s  %s" % (c, names.get(c, "(未知卡名)")))
    for k in range(min(MAX_PACK, len(codes))):
        lb.selection_set(k)
    result = []

    def confirm():
        sel = [int(lb.get(k).split()[0]) for k in lb.curselection()]
        if not (MIN_PACK <= len(sel) <= MAX_PACK):
            messagebox.showwarning(
                "数量不对", "必须选 %d~%d 张，当前选了 %d 张" % (MIN_PACK, MAX_PACK, len(sel)),
                parent=win)
            return
        result.extend(sel)
        win.destroy()

    ttk.Button(win, text="确定", command=confirm).pack(pady=8)
    win.transient(root)
    win.grab_set()
    root.wait_window(win)
    return result


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        ypk = filedialog.askopenfilename(
            title="① 选择包含 3~6 张卡的 ypk",
            filetypes=[("YPK 扩展包", "*.ypk"), ("ZIP", "*.zip"), ("所有文件", "*.*")])
        if not ypk:
            return
        try:
            codes, cdb_names, info = collect_from_ypk(ypk)
        except zipfile.BadZipFile:
            messagebox.showerror("打不开", "这不是 zip 格式的 ypk：\n%s" % ypk, parent=root)
            return
        if not codes:
            messagebox.showerror("没找到卡", info, parent=root)
            return

        combo = filedialog.askopenfilename(
            title="② 选择要追加的 combo.lua",
            filetypes=[("Lua 脚本", "*.lua"), ("所有文件", "*.*")])
        if not combo:
            return
        names = load_names(combo, cdb_names)
        if not (MIN_PACK <= len(codes) <= MAX_PACK):
            codes = choose_cards(root, codes, names)
            if not codes:
                return

        cardlist = "\n".join("  %s  %s" % (c, names.get(c, "(未知卡名)")) for c in codes)
        dup = {c: n for c, n in Counter(codes).items() if n > 1}
        dup_text = ("\n\n同名卡：" + "、".join(
            "%s x%d" % (names.get(c, c), n) for c, n in dup.items())) if dup else ""
        warning = ""
        pool = os.path.join(os.path.dirname(os.path.abspath(combo)), "408.cdb")
        if os.path.exists(pool):
            try:
                conn = sqlite3.connect(pool)
                have = {i for i, in conn.execute("SELECT id FROM datas")}
                conn.close()
                miss = [c for c in codes if c not in have]
                if miss:
                    warning = ("\n\n提示：这些卡不在当前轮抽卡池 408.cdb 里：%s\n"
                               "（combo 走卡号直取，不依赖卡池，仍能正常给出；"
                               "只要卡号没写错就不用管）" % miss)
            except sqlite3.Error:
                pass

        limits = load_limits(combo)
        banned, over = check_limits(codes, limits, names)
        if banned:
            messagebox.showerror(
                "含禁止卡", "这个包里有 2006.3.1 禁止卡，不能添加：\n%s" % banned, parent=root)
            return
        if over:
            warning += "\n\n注意：超过禁限表额度：%s" % over
        if not messagebox.askokcancel(
                "确认添加",
                "来源：%s\n识别方式：%s\n\n将追加到：\n%s\n\n%s%s%s" %
                (os.path.basename(ypk), info, combo, cardlist, dup_text, warning),
                parent=root):
            return

        backup, enabled = append_pack(combo, os.path.basename(ypk), codes)

        # combo 专用卡（2pick/exclusive.txt）提示：这些卡只走 combo，包里用到了才拿得到
        excl_note = ""
        excl_txt = os.path.join(os.path.dirname(os.path.abspath(combo)), "exclusive.txt")
        if os.path.exists(excl_txt):
            excl_ids = set()
            with open(excl_txt, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    tok = line.split(None, 1)[0]
                    if tok.isdigit():
                        excl_ids.add(int(tok))
            hit = [c for c in codes if c in excl_ids]
            if hit:
                excl_note = "\n\n本包用到了 combo 专用卡：%s\n（它们在普通轮抽里不会出现，符合预期）" % hit
            else:
                excl_note = ("\n\n提示：这包没有用到 2pick/exclusive.txt 里的卡。"
                             "若某张卡只有成套才有用，记得把它加进 exclusive.txt 并跑 更新-exclusive.bat。")

        messagebox.showinfo(
            "完成",
            "已追加一组 %d 张卡到：\n%s\n\n%s\n\n备份：%s\n%s%s" %
            (len(codes), combo, cardlist, os.path.basename(backup),
             "已自动把 pick.lua 的 ENABLE_COMBO 打开" if enabled else "ENABLE_COMBO 无需改动",
             excl_note),
            parent=root)
    except Exception as e:
        messagebox.showerror("出错", "%s: %s" % (type(e).__name__, e), parent=root)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
