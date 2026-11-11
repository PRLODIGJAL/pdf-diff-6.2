# -*- coding: utf-8 -*-
"""
pdfdiff_pairing.py —— **哪份是旧版、哪份是新版**。纯函数，不碰界面。
==================================================================
判据只有一条，但踩过两个坑，都写进注释了：

  ① 文件名里的数字必须按**数值**比，不能按字符串比 —— 否则 `rev10` 排在
     `rev9` 前面、`Picture2.pdf` 排在 `Picture.pdf` 前面；
  ② 主名与扩展名必须**分开**比 —— 整串比时 `Picture.pdf` 会输给
     `Picture2.pdf`（前缀更短者小），于是「Picture.pdf」被当成新版。

放在 judge 层而不是 ui 层，是因为「谁旧谁新」是业务判定：判错了整套标记的
红绿方向都是反的，改这里必须能脱离界面单独验。

2026-11-02 目录重构时从 `diff_app5.py` 切出来，只把名字的私有前缀去掉，
算法一个字符没改。
"""

from __future__ import annotations

import os
import re


def nat_tokens(s: str):
    """字符串 → 自然排序键（连续数字按数值）。空串给空元组，避免比较报错。"""
    out = []
    for tok in re.split(r"(\d+)", s or ""):
        if not tok:
            continue
        out.append((1, int(tok)) if tok.isdigit() else (0, tok.lower()))
    return tuple(out)


def nat_key(path: str):
    """文件名自然排序键：**先比主名、再比扩展名**，数字按数值比。

    为什么不能直接 `sorted()`：字符串序里 `rev10` 会排在 `rev9` 前面
    （"1" < "9"），`Picture2.pdf` 也会跑到 `Picture.pdf` 前面。做法是把
    连续数字切成段、数字转 int 逐段比较。

    ⚠️ 主名与扩展名**必须分开比**：`Picture.pdf` 的单词段是 `picture.pdf`，
    整串比会输给 `Picture2.pdf` 的 `picture`（前缀更短者小），
    于是「Picture.pdf」被当成新版 —— 桌面 `对比案例/label/` 正是这种命名。
    """
    base = os.path.basename(path or "")
    stem, ext = os.path.splitext(base)
    return (nat_tokens(stem), nat_tokens(ext))


def pair_from_paths(paths):
    """两个以上路径 → (旧, 新)。排序在前的当旧版，多余的丢弃（调用方负责提示）。"""
    ps = sorted([p for p in paths if p], key=nat_key)
    if not ps:
        return "", ""
    if len(ps) == 1:
        return ps[0], ""
    return ps[0], ps[1]
