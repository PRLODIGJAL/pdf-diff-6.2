#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bootui.py —— 所有工具「Windows 启动/自检」的统一输出模板（零依赖，只用标准库）。

为什么单独抽出这一个文件
------------------------
2026-10-23 用户原话：

    「这么多的项目，加上还有那个之前的对比，然后还有 WI 一致性检验这些，
      其实它 Windows 启动的那个界面都是不统一的现在。文字部分千奇百怪，
      不方便去识别。」

于是把「怎么打印」和「打印什么」拆开：**本文件只管版式**，各项目只管自己的检查项。
同一个 bootui.py 逐字节拷进每个交付包（md5 可比），谁的启动界面都长一个样。

版式（固定 62 列，80 列宽的 cmd 窗口不会折行）
----------------------------------------------
    ==============================================================
      PDF 转换拼接工具  4.0   环境自检
    ==============================================================
      系统    Windows 10 (10.0.19045)
      Python  3.12.4
      位置    D:\\工具\\PDF转换拼接4.0

    -- 1  运行环境 ------------------------------------------------
      [ OK ]  虚拟环境    .venv
      [ OK ]  依赖        pypdf 6.0.0 · tkinterdnd2 0.4.2
      [ OK ]  界面库      Tk 8.6

    ==============================================================
      [ OK ]  环境就绪，正在打开界面…
    ==============================================================

四条必须守住的规矩
------------------
1. **状态标记只有四种**，全是 ASCII，6 列等宽，任何控制台代码页都显示得出来：
       [ OK ] 通过    [ !! ] 不通过    [ -- ] 跳过/不适用    [ .. ] 进行中
   不许再出现 ✓ ✗ ✅ ❌ ⚠️ ★ 之类 —— 它们正是「千奇百怪」的来源
   （这些符号不在 GBK 里：一旦输出被重定向到文件就是 ?，还有人看成乱码）。
2. **标签只用 LABELS 里那几个词**。同一件事在每个项目里必须叫同一个名字，
   这样眼睛扫左列就能认，不用重新读一遍句子。
3. **一屏看完**：正常路径 ≤ 18 行；出错时先给结论、再给「下一步」，
   细节留给 `--check` / `--verbose` 那些入口，不往启动器里塞。
4. **不打印路径以外的实现细节**（版本号、行号、内部变量）——使用者不需要。

怎么用（所有项目一模一样）
--------------------------
    import bootui as B
    B.head("PDF 转换拼接工具", "4.0", "环境自检",
           [("系统", B.os_line()), ("位置", HERE)])
    B.section(1, "运行环境")
    B.item(B.OK, "虚拟环境", ".venv")
    B.end(B.OK, "环境就绪，正在打开界面…")

    # 出错时：
    B.end(B.FAIL, "没能准备好环境",
          steps=["重新双击「启动PDF差异对比工具」补一次环境",
                 "还不行就把这一屏截图发给我"])
"""

import os
import platform
import unicodedata

# ── 版式常量 ────────────────────────────────────────────────────────────────
WIDTH = 62          # 整屏宽度（显示列，中文算 2 列）—— 80 列窗口留 18 列余量
LABEL_W = 10        # 「标签」这一列的宽度，短了会把 detail 挤歪，长了白占地方
INDENT = 20         # detail 的起点 = 2 + 6 + 2 + 10（note/raw 跟它对齐）

# ── 四种状态 ────────────────────────────────────────────────────────────────
OK = "OK"           # 通过
FAIL = "!!"         # 不通过 / 需要注意
SKIP = "--"         # 跳过、不适用（在某些机器上是**正常**的，别让人误以为坏了）
RUN = ".."          # 正在做（装依赖这类要跑几秒的事）

_MARK = {OK: "[ OK ]", FAIL: "[ !! ]", SKIP: "[ -- ]", RUN: "[ .. ]"}

# 不许出现在行首的标点（避头尾）—— 折行时让它们跟着上一行走。
_NO_LINE_START = "、，。；：！？）〕】》」』’”"

# ── 固定标签词表（不许临时造词）────────────────────────────────────────────
LABELS = (
    "虚拟环境",      # 工具自带的 .venv
    "依赖",          # pip 装的包（带版本）
    "运行库",        # 系统级运行库（VC++ 这类，不属于 pip）
    "界面库",        # Tk 能不能真的起起来
    "可用性",        # 合起来这一句结论
    "程序",          # 外部程序（Word / Visio / CAD / BarTender）找到没
    "PDF 打印机",    # CAD 出图要挑的那台设备
    "结论",          # 收尾那一句
)

_print = print


def _w(text):
    """显示宽度：东亚宽字符按 2 列算。对齐全靠它，用 len() 会歪。

    ⚠️ 只认 `WF`（宽 / 全角），**不要**把 `A`（歧义宽度）也算成 2 ——
       `·` `…` 这类字符在 cmd 里其实只占 1 列，算成 2 就会整列右移。
       模板文本里也尽量少用它。
    """
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def _pad(text, width):
    """按显示宽度右补空格。"""
    text = str(text)
    return text + " " * max(0, width - _w(text))


def _fill(left):
    """左半句 + 用 - 填满整行（分节标题就长这样，一行就够，不占三行）。"""
    left = str(left)
    return left + "-" * max(0, WIDTH - _w(left))


def pad(text, width):
    """按显示宽度右补空格 —— 给「清单式」的行自己对齐用。

    检查项的标签列固定 LABEL_W（10）；而「每族一条」这种清单的标签是内容
    （`Word / Excel 这类 Office 文档`），长度不一，所以让它自己给宽度：
        B.item(B.OK, B.pad(family_label(fam), 20), eng.label)
    同一段清单里用同一个宽度，看着就是一列。
    """
    return _pad(text, width)


def _clip(text, width):
    """超宽就截断加…（只给「不该太长」的字段用，比如理由摘要）。"""
    text = str(text)
    if _w(text) <= width:
        return text
    out = ""
    for ch in text:
        if _w(out) + _w(ch) > width - 1:
            break
        out += ch
    return out + "…"


def _wrap(text, width):
    """按显示宽度折行，返回若干行。

    ⚠️ 检查项宁可折行也**不许截断** —— 被截掉的往往正是关键信息
       （依赖版本、报错原文、哪个程序没找到）。
    ⚠️ 英文报错按单词折（别把 `following` 劈成 `follo` / `wing`）；
       超过半行还没空格（长路径、长包名）才逐字折。
    ⚠️ 顿号 / 逗号 / 句号这类**不许出现在行首**：宁可这一行多占 2 列，
       也不让下一行以「、」开头（看着像排版坏了）。
    """
    lines = []
    for para in str(text).splitlines() or [""]:
        cur, curw = "", 0
        for ch in para:
            cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            if curw + cw > width:
                if ch in _NO_LINE_START:      # 标点不许放行首，跟着上一行走
                    lines.append(cur + ch)
                    cur, curw = "", 0
                    continue
                cut = cur.rfind(" ")
                if cut >= width // 2:          # 有像样的断点就按词断
                    lines.append(cur[:cut])
                    cur, curw = cur[cut + 1:], _w(cur[cut + 1:])
                else:
                    lines.append(cur)
                    cur, curw = "", 0
            cur += ch
            curw += cw
        lines.append(cur)
    return lines


def os_line():
    """「系统」那一行的值：Windows 10 (10.0.19045) / macOS 14.5 这种。"""
    if os.name == "nt":
        ver = platform.version() or "?"
        rel = platform.release() or "Windows"
        arch = "64 位" if platform.machine().endswith("64") else "32 位"
        return "%s (%s) %s" % (rel, ver, arch)
    return "%s %s" % (platform.system() or "?", platform.release() or "")


def head(tool, version, mode="", pairs=()):
    """抬头：两行等号线夹一个「工具名 版本 模式」，下面是固定的几行键值。

    pairs 只用这几个键：系统 / Python / 位置 —— 别的都别加，
    加得越多越像日志，越难扫。
    """
    _print("=" * WIDTH, flush=True)
    title = "%s  %s" % (tool, version)
    if mode:
        title += "   " + mode
    _print("  " + title, flush=True)
    _print("=" * WIDTH, flush=True)
    for label, value in pairs:
        lines = _wrap(value, WIDTH - 10)
        _print("  " + _pad(label, 8) + lines[0].rstrip(), flush=True)
        for extra in lines[1:]:
            _print(" " * 10 + extra.rstrip(), flush=True)


def section(no, name):
    """分节：`-- 1  运行环境 ---------...`。no 给空就只写名字。"""
    _print("", flush=True)
    _print(_fill("-- %s  %s " % (no, name) if no else "-- %s " % name), flush=True)


def item(state, label, detail=""):
    """一行检查项：`  [ OK ]  虚拟环境    .venv`。

    detail 太长就**折到下一行**（缩进仍是 detail 那一列），不截断 —— 见 _wrap。
    """
    mark = _MARK.get(state, _MARK[FAIL])
    left = "  %s  %s" % (mark, _pad(label, LABEL_W))
    room = WIDTH - _w(left)
    lines = _wrap(detail, max(room, 20))
    _print((left + lines[0]).rstrip(), flush=True)
    for extra in lines[1:]:
        _print(" " * INDENT + extra.rstrip(), flush=True)


def step(text):
    """进行中的一行（装依赖、重建环境这种会跑几秒的事）。"""
    _print("  [ .. ]  " + _clip(text, WIDTH - 10).rstrip(), flush=True)


def note(text):
    """补充说明：没有标记，缩进对齐到 detail 那一列。多行会逐行缩进。"""
    for ln in _wrap(text, WIDTH - INDENT):
        _print(" " * INDENT + ln.rstrip(), flush=True)


def raw(text, indent=0):
    """把子进程的输出原样嵌进来。

    ⚠️ 默认**不加缩进**：子进程本来就是按本模板打的（`--no-head`），
       再加一层就变成 4 列，跟外面这一层的 2 列对不上。
    ⚠️ 不折行也不截断：那是别的程序的原文，改了就失真了。
    """
    for ln in str(text).splitlines():
        if ln.strip():
            _print(" " * indent + ln.rstrip(), flush=True)


def para(text, indent=2):
    """整段话按宽度折行（左缩进 indent）—— 给「一长串格式名」这类内容用。

    检查项走 item()；一行到底的长句走这里，**别让它超过 WIDTH**，
    否则 80 列的 cmd 窗口会自己折，整片对齐全废。
    """
    for ln in _wrap(text, WIDTH - indent):
        _print(" " * indent + ln.rstrip(), flush=True)


def end(state, text, steps=()):
    """收尾：两行等号线夹一句结论；steps 非空时再给一个「下一步」清单。

    ⚠️ steps 最多 3 条、每条一句话 —— 这里是给人照着做的，
       写成段落就等于没写。
    """
    _print("", flush=True)
    _print("=" * WIDTH, flush=True)
    _print("  %s  %s" % (_MARK.get(state, _MARK[FAIL]), text), flush=True)
    _print("=" * WIDTH, flush=True)
    if steps:
        _print("", flush=True)
        _print("  下一步", flush=True)
        for i, s in enumerate(steps, 1):
            _print("    %d. %s" % (i, s), flush=True)
        _print("", flush=True)
