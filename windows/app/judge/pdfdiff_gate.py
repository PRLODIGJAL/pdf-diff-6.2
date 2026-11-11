# -*- coding: utf-8 -*-
"""
pdfdiff_gate.py —— 开跑前的**准入与提醒**：能不能比、要不要先问一句。
==================================================================
这里**只做决定，不弹窗、不写状态栏**。返回一个动作，由界面去执行：

    ACT_BLOCK  不许比 —— 界面弹错误框，不给「仍然继续」
    ACT_ASK    能比，但这份读不出文字 —— 界面弹确认框问一句
    ACT_GO     直接跑（`note` 是这一轮要一直摆在状态栏的那句话）

为什么要把这十几行单独拎出来：这三条是**用户定的硬规则**（同格式、同页数、
扫描件不比），判据在 `pdfdiff_preflight.assess()`。以前它和弹窗、状态栏文案
搅在一个 70 行的界面方法里，「到底什么情况会拦」只能靠读界面代码推。现在
`decide()` 是纯函数：输入一份体检结论、输出一个动作，脱离 Tk 就能验。

⚠️ 文案与 `pdfdiff_preflight.block_text()` **同源**：界面上写着允许、点了却被
   拦，就是这两处走散了（顶部 `SUPPORT_HINT` 那三条硬规则也指着这对）。
⚠️ 2026-10-30 口径：丢文件时只判不问（可能只是想当查看器翻一翻），真正的
   拦截与提醒都在**点「开始对比」那一刻** —— 也就是走到这里。
"""

from __future__ import annotations

import pdfdiff_preflight as pre

ACT_BLOCK = "block"
ACT_GO = "go"
ACT_ASK = "ask"


def block_reason(rep) -> str:
    """这份「为什么不能比」的第一条原因；能比就给空串。

    状态栏、按钮、弹窗三处说的是同一件事，所以都从这里取，不各写一遍。
    """
    if not rep or rep.get("level") != "block":
        return ""
    try:
        t, reasons, _tip = pre.block_text(rep)
        return reasons[0] if reasons else t
    except Exception:
        return rep.get("title") or "这份不能对比"


def block_payload(rep) -> dict:
    """不能比时，弹窗要的三段：标题 / 逐条原因 / 结尾提示。"""
    t, reasons, tip = pre.block_text(rep)
    return {
        "head": reasons[0] if reasons else t,
        "body": "%s\n\n%s\n\n%s"
                % (t, "\n".join("· " + r for r in reasons), tip),
        "title": t,
        "reasons": reasons,
        "tip": tip,
    }


def decide(rep, confirmed: bool = False) -> dict:
    """体检结论 → 该拦、该问、还是该直接跑。

    `confirmed` = 这份文件的提醒用户**已经点过「继续对比」**（丢文件那会儿
    问过并确认了）。同一个文件问两遍很烦，所以确认过就不再问 —— 仍然会跑，
    只是动作从 `ACT_ASK` 变成带 `note` 的 `ACT_GO`。

    `rep` 为 None（体检自己出错）一律放行：体检是辅助，不该成为拦路石，
    真有问题交给引擎去暴露。
    """
    if not rep:
        return {"action": ACT_GO, "note": "", "confirmed": False}
    if rep.get("level") == "block":
        d = block_payload(rep)
        d["action"] = ACT_BLOCK
        return d
    if rep.get("level") != "warn":
        return {"action": ACT_GO, "note": "", "confirmed": False}
    note = pre.pixel_hint(rep)
    if confirmed:
        return {"action": ACT_GO, "note": note, "confirmed": True}
    return {"action": ACT_ASK, "note": note, "confirmed": False}
