# -*- coding: utf-8 -*-
"""
pdfdiff_locate.py —— **坐标定位型**比对（给「标注版 PDF」用）
==================================================================
为什么要有第二个引擎
--------------------
标注要画准，就必须知道「改动在页面上的哪个矩形里」。
`pdfdiff_core` 走 pdfminer，只给出文字内容（行/表格格），
于是标注只能拿着文字去 `page.search_for()` 搜位置 ——
这一步是误标的万恶之源：

  · 差异块常是跨行拼出来的**伪文本**，PDF 里压根没有这段连续文字 → 搜不到；
  · 搜不到就砍短再试 → 砍成「公司」「批号」「标识」→ 满页命中无辜文字；
  · 就算搜到了，同名的文字在页里出现几次就标几处。

本模块换一条路：**比对和标注用同一个抽取引擎（PyMuPDF）**。
PyMuPDF 的 `get_text("rawdict")` 直接给到**每一个字符的 bbox**，
所以差异项天生自带坐标 —— 标注时一个字都不用搜，直接画。

用户要的效果（2026-09-29 原话）：
  「比方一二三四五六七八九十是旧表，一二三四五六七八十一十是新表，
    识别到 9 和 11 有差异，就在旧表标出 9、在新表标出 11。」
→ 本模块正是这么做的：**逐字符对比（真正的一对一坐标反推）**。

比对三步走（这是根除「瞎标」的关键）
------------------------------------
  1. **抽坐标**：用 rawdict 把每页每个字连同它自己的 bbox 平铺出来（带页码）。
  2. **行级精确抵消**：把两份文档里**逐字完全相同、只是位置变了**的行先抵消掉
     （跨页/换行重排、整段换位都不会刷出假差异）。这一步是位置无关的。
  3. **字符流逐字 diff**：把剩下没抵消的行摊成**一条字符流**（空白换行不进流），
     整份文档一次 `SequenceMatcher` 比完 —— 变了的那些字，**直接拿它们自己的
     bbox** 去标红/标绿。

⚠️ 为什么第 3 步**不能**按「整行配对」做（2026-09-29 用户实测打回来的教训）
--------------------------------------------------------------------------
改版时文字会**重新折行**（旧版「A、B」两行 → 新版并成一行，或反过来）。
按「整行」配对的引擎遇到这种情况，**整行配不上对**，于是：
  · 旧版那半行被判成「删除」→ 标红；
  · 新版那半行被判成「新增」→ 标绿；
  · 可这些字**两边都有**，只是换行位置变了 —— 用户一眼就看出来是「瞎标」。
实测 DOC-1007 rev06→rev07：`示例医疗`、`101室，1栋，2栋`、
`• 本试验主要有效性指标是手…` 全被这么误标。

换成字符流后，空白（含换行）根本不进比对串，折行差异天然消失，
只有**字本身变了**才会落进 diff 的 replace/delete/insert 里。

整个过程中 bbox 从一开始就挂在每个字上，**中间没有任何「归一化序号 →
真实字符序号」的重映射**，所以标出来的矩形 100% 就是 diff 算出的那几个字。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import atexit
from collections import Counter, deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:                       # 1.28 起推荐用 pymupdf，fitz 只是兼容别名
    import pymupdf as fitz
except ImportError:        # pragma: no cover
    import fitz            # type: ignore


_log = logging.getLogger("pdfdiff_locate")


# ---------------------------------------------------------------------------
# 协作式「停止对比」（2026-10-20，用户：开始对比右边加「停止对比」）
#
# 为什么是**协作式**、不是直接杀线程
# ----------------------------------
# Python 没有安全的「强杀线程」：`PyThreadState_SetAsyncExc` 能把异常塞进目标
# 线程，但若那一刻它正停在 onnxruntime / PyMuPDF 的 C 栈里，异常会在**任意的
# 后续字节码位置**爆出来，留下半初始化的 OCR 引擎、半写出的缓存文件。
# 做法改成：在**耗时单元的边界**查一次标志，命中就抛 `Cancelled` 让调用栈
# 自然退栈 —— 任何一次退出都发生在「上一件事已经做完、下一件事还没开始」。
#
# ⚠️ `Cancelled` 必须继承 `BaseException`，**绝不能**继承 `Exception`
# -------------------------------------------------------------------
# 本模块（和 pdf_annotate / pdfdiff_embed）里到处是
#     try: … except Exception: return "" / return [] / 走降级分支
# 的兜底 —— OCR 认不出、缓存读坏、引擎缺失都靠它们。如果取消信号是
# `Exception` 的子类，它会被这些兜底**当场吞掉**，程序继续往下跑，只是
# 数据全变成空：用户看到的是「停不下来」+「结果全空」。
# 那就是**静默改结果**，比不能停更糟。用 `BaseException` 才能一路穿到线程顶层。
#
# ⚠️ 未触发取消时，只多一次 `Event.is_set()`（纳秒级，且在 OCR/区域这种
#    毫秒~秒级单元之前），**判定逐字段一致** —— 见 _regress 的取消专项回归。
# ---------------------------------------------------------------------------
class Cancelled(BaseException):
    """用户点了「停止对比」。**不是错误**，是主动中止，调用方单独处理。"""


_CANCEL = threading.Event()


def request_cancel() -> None:
    """请求中止当前比对（可在任意线程调用）。"""
    _CANCEL.set()


def clear_cancel() -> None:
    """开始新一轮比对前清标志（否则上一轮的取消会立刻打死新一轮）。"""
    _CANCEL.clear()


def cancel_requested() -> bool:
    return _CANCEL.is_set()


def checkpoint() -> None:
    """耗时单元边界上的检查点：已请求中止就抛 `Cancelled`。

    只放「单元之间」，**不要**放进正在算某个结果的函数中间 —— 放中间会让
    中止把一个算了一半的结果留在缓存里（本模块的缓存都是整块写，边界上抛
    不会产生半截条目）。
    """
    if _CANCEL.is_set():
        raise Cancelled("用户中止比对")



def _guard_pages(items: List[Any], old_pages: int, new_pages: int,
                 where: str = "") -> None:
    """页码守卫（指令F 一-2）：所有输出的 page_old/page_new 必须落在
    `[0, 文档页数)` 内，越界即抛 `ValueError` 并写日志。

    在 `locate()` / `graphic_regions()` 出口统一调用，把「幽灵页码」从静默
    越界变成显式失败——宁可让一次比对报错，也不能让界面画到不存在的页上。
    """
    for it in items:
        po = getattr(it, "page_old", None)
        pn = getattr(it, "page_new", None)
        if po is not None and not (0 <= po < old_pages):
            _log.error("页码越界 %s: page_old=%s >= old_pages=%s (%s)",
                       where, po, old_pages, getattr(it, "kind", "?"))
            raise ValueError(
                f"page_old={po} 越界（old_pages={old_pages}） @ {where}")
        if pn is not None and not (0 <= pn < new_pages):
            _log.error("页码越界 %s: page_new=%s >= new_pages=%s (%s)",
                       where, pn, new_pages, getattr(it, "kind", "?"))
            raise ValueError(
                f"page_new={pn} 越界（new_pages={new_pages}） @ {where}")


@dataclass
class Config:
    """可注入的判定参数（2026-10-15 按外部评审建议：常量 → 可扫描配置对象）。

    模块级常量改成实例属性后，阈值标定脚本（_regress/calibrate_threshold.py）
    能批量扫描不同阈值、复用同一份样本打标签，而不用改代码。
    默认值即原先的硬编码常量；exe 同目录若有 config.json 会覆盖（标准库 json，
    不引入 pyyaml）。
    """
    # ---- 位移判定 ----
    move_ratio_max: float = 5.0
    move_min_dist: float = 2.0
    block_dx_tol: float = 8.0
    block_dy_tol: float = 20.0
    # 切片长度算法：projection（外接矩形在位移方向投影 w|ux|+h|uy|，默认）
    #            chord（过中心的弦长 min(w/|ux|, h/|uy|)）。
    # 实测 11vs12 两种模式对真实样本几乎一致（真实移动近乎纯垂直/水平），
    # 保留双模式供标定脚本并排比较（calibrate_ratio.py）。
    slice_mode: str = "projection"
    # 低置信度阈值：ratio 落在 (move_ratio_max, low_conf_ratio] 的块仍判移动（标蓝），
    # 但写入报告「低置信度清单」（2026-10-15 用户拍板：不做灰框，4~6 仍标蓝）。
    low_conf_ratio: float = 6.0
    # 格式变化（文字相同、渲染不同）判定：文字层判相同的行，按其位移补偿后做
    # 行块级模板匹配取最佳位移 + NCC，并算墨量比 min/max。
    # NCC < format_ncc_min 或 墨量比 < format_ink_min → 旧红 + 新绿（加粗/换字体）。
    # 2026-10-16 指令D：替换上一轮的「像素残差占比」方案（format_resid_min），
    # 实测 11vs12：加粗混排行 NCC≈0.6~0.76，未变/仅推移行 ≥0.93。
    format_ncc_min: float = 0.85
    format_ink_min: float = 0.88
    # 行块级模板匹配（转曲区/格式变化/图形层召回共用）的通用阈值。
    tm_ncc_move_min: float = 0.50   # 转曲区位移检测：NCC≥此值判「移动」（蓝）。
    # ⚠️ 2026-10-16 实测标定（11vs12 二-2）：指令D 建议初值 0.93 对**同一嵌入图
    # 像的纯平移**可达，但对**矢量重排/重新转曲的文字**不可达 —— 重排后字形
    # 渲染有像素级差异（重绘曲线），实测真位移簇 NCC≈0.53~0.78（300dpi）、
    # 错位误匹配 0.2~0.4、未变行 ≥0.99。阈值取 0.50 落在分离带内，config.json
    # 可覆盖（需要更严就调回 0.93）。
    tm_shift_min_pt: float = 2.0    # 位移>此值才判「移动」（噪声下限）
    tm_ink_min: float = 0.85        # 墨量比≥此值（真位移两侧墨量基本一致）
    # ---- 标注样式（2026-10-15 定稿：紧贴内容的直角矩形）----
    annot_pad: float = 1.0          # 文字框外扩上限（pt）：≤1pt
    graphic_pad: float = 1.0        # 像素残差框外扩（pt）：≤1pt
    annot_border: float = 1.0       # 框线宽（pt）：0.8~1
    annot_opacity: float = 0.15     # 内部填充不透明度：0.12~0.18
    annot_label: bool = True        # 框左上角外侧画编号小标签
    annot_label_size: float = 6.0   # 编号字号（pt）
    hl_min_h: float = 0.0           # 矮框补到最小高（pt）：0 = 关闭（紧贴不扩）


def _load_config() -> Config:
    """从 exe/脚本同目录的 config.json 读覆盖项（可选）。仅标准库。"""
    cfg = Config()
    for base in (os.path.dirname(os.path.abspath(__file__)),
                 os.getcwd(),
                 getattr(sys, "_MEIPASS", "")):
        if not base:
            continue
        p = os.path.join(base, "config.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    over = json.load(f)
                for k, v in over.items():
                    if hasattr(cfg, k):
                        if isinstance(v, bool) or isinstance(v, (int, float, str)):
                            setattr(cfg, k, v)
            except Exception:
                pass  # 配置坏了就回落默认，绝不影响运行
    return cfg


#: 全局配置单例。旧代码里的 MOVE_RATIO_MAX 等模块常量改由它承接。
_CFG = _load_config()

# ---------------------------------------------------------------- 参数
HF_TOP = 0.055              # 页顶/页底这个比例内的行 = 页眉页脚（可忽略）
HF_BOTTOM = 0.945
# 高亮矩形上下各留一点。2026-10-15 改定：外收进 Config.annot_pad（默认 1.0pt，
# 紧贴内容 —— 旧值 1.5pt 会在渲染图上外扩超过 2px）。
RECT_PAD = _CFG.annot_pad
MIN_ROW_LEN = 2            # 归一化后不足这个长度的行直接忽略（见下）
_GROUP_GAP = 4.0           # 同一处改动里相邻的字，纵向差 ≤ 此值 (pt) 就算同一行
_TINY_ANCHOR = 4           # 字符流里短于这个长度的「相等段」不算锚点，并入改动（见 _opcodes）

# ⚠️ 反面教材：**不要按「在页面顶部/底部」来过滤页眉页脚**（曾这么做过，已废弃）。
# 说明书的文件号、修订日期、版本号**就印在页脚**，而它们恰恰是改版时最该标出来的东西
# （DOC-1007 实测：Rev06/DCR 0000-0000 → Rev07、修订日期 2025年 → 2026年）。
# 按位置一刀切会把它们整段滤掉，用户看到的标注就"莫名其妙少了"一大截。
# 真正该忽略的只有页码这种纯编号行（页数一变会满篇刷差异），见 _is_page_number。
_PAGE_NUM_RE = re.compile(r"^\s*\d{1,3}\s*([/／\-]\s*\d{1,3})?\s*$")
# 行尾的页码 token（如 "1/2"）。改版时页脚常被重排：旧版页码单独一行（已被上面忽略），
# 新版并进页脚那一行 → 字符流里就凭空多出「1/2」被标绿。见 _strip_page_token。
_TAIL_PAGE_RE = re.compile(r"\s+(\d{1,3}\s*[/／]\s*\d{1,3})\s*$")


@dataclass(frozen=True)
class Row:
    """页面上的一行文字，带它每个字的坐标。

    v67（指令D 六-3）：改为 `frozen=True` —— 实例不可变、**可哈希**，从类型
    层面杜绝「Row 塞进 set/dict」时的 `TypeError: unhashable type: 'Row'`
    （混合版 `_extract_moved_rows` 第 1.5 步实锤过）。`chars` 是 list（不可
    哈希），故显式定义 `__hash__` 只哈希身份字段；`eq` 语义不变（仍按全字段
    比值）。补查路径的 `_bad_pairs` 继续用 `id()` 追踪对象同一性（两个值相等
    但对象不同的行不应互相误伤），两者并存、各有测试。
    """
    text: str
    norm: str
    chars: List[Tuple[str, Any]] = field(default_factory=list)
    rect: Optional[Tuple[float, float, float, float]] = None
    page: int = 0
    #: 主 span 的字体签名 `"字体名|字号"`（字数最多的那个 span）。
    #: 仅文字层有；OCR 补的行留空（空签名不参与字体变动判定）。
    font: str = ""
    #: 主 span 的**渲染字号**（pt，round 到 0.1）。单独存一份，供字体变动判定
    #: 精确比较字号差（`_extract_font_changes` 里与字体名分开判，阈值 FONT_MIN_DSIZE）。
    size: float = 0.0

    def __hash__(self) -> int:
        # chars 为 list 不可哈希 → 只取身份字段；与 eq（全字段比值）满足
        # 「相等对象必等哈希」契约，不等对象允许同哈希。
        return hash((self.text, self.norm, self.page, self.rect,
                     self.font, self.size))


@dataclass
class LocDiff:
    """一处改动，两侧各自带矩形（画标注时直接用，不再搜索）。"""
    kind: str                       # added / removed / page_added / page_removed / moved / font
    page_old: Optional[int] = None
    page_new: Optional[int] = None
    old_text: str = ""
    new_text: str = ""
    rects_old: List[Tuple[float, float, float, float]] = field(default_factory=list)
    rects_new: List[Tuple[float, float, float, float]] = field(default_factory=list)


# ---------------------------------------------------------------- 抽取
def _norm(text: str, ignore_case: bool, ignore_punct: bool) -> str:
    out: List[str] = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
            continue
        c = ch.lower() if ignore_case else ch
        if ignore_punct and not c.isalnum():
            continue
        out.append(c)
    return "".join(out).strip()


def _is_page_number(s: str) -> bool:
    return bool(_PAGE_NUM_RE.match(s or ""))


def _strip_page_token(raw: str, min_keep: int = 8) -> str:
    """剥掉行尾的页码 token（`1/2` 这种）。

    ⚠️ 只在「行尾 + 前面还有 ≥ min_keep 个非空白字」时才剥：
    页码是随翻页/重排飘的东西，不该算改动；但表格里真正的「1/2」类数值
    往往就是整格内容（前面没什么字），那就不剥。
    """
    m = _TAIL_PAGE_RE.search(raw)
    if not m:
        return raw
    if len(re.sub(r"\s+", "", raw[:m.start()])) < min_keep:
        return raw
    return raw[:m.start()]


def extract_rows(page, page_idx: int, ignore_case: bool = False,
                 ignore_punct: bool = False,
                 drop_header_footer: bool = False,
                 ignore_page_number: bool = True,
                 min_len: int = MIN_ROW_LEN) -> List[Row]:
    """用 PyMuPDF 抽一页的**行**，每行带上它每个字的 bbox。

    用 rawdict 而不是 get_text()：rawdict 给到字符级坐标，
    这正是「改动标在哪」所需要的精度。

    ⚠️ `min_len`：只含一两个符号的行（"•"、"♦"、单独的页码）在改版时数量一变，
    就会刷出成片的"删+增"噪音。这类行本身也不承载信息，直接不参与比对。

    ⚠️ `drop_header_footer`（按位置砍页眉页脚）**默认关闭且不建议开**：
    文件号/修订日期/版本号常印在页脚，开了会把真改动整段滤掉。
    """
    rows: List[Row] = []
    try:
        data = page.get_text("rawdict")
    except Exception:
        return rows
    h = float(page.rect.height) or 1.0
    # 页面真实宽高（渲染坐标系，已应用 /Rotate）。
    pw = float(page.rect.width) or 1.0
    ph = float(page.rect.height) or 1.0
    # 越界容差：字符 bbox 允许略微超出裁剪框（出血/负坐标），超出它即判幽灵内容。
    _EDGE_TOL = 2.0
    # 旋转归一化（指令F 一-4 重做）：`rawdict` 给的字符 bbox 是**未应用 /Rotate 的
    # 原始坐标**（DOC-1004 rev01 的 /Rotate 90 竖版页，`0.018` 的 bbox 落在
    # y=627~652，是竖版 595×842 坐标系下的位置，与横版 rev04 的 y=20~35 对不上）。
    # 之前的修法是把这些「看起来越界」的字符当幽灵内容**过滤掉**——方向错了：它们
    # 是真实内容，只是没被旋转到显示坐标，过滤反而让 rev01 侧文字消失、刷出假差异。
    # 正确做法：用 `page.rotation_matrix`（rotation=0 时是单位阵、零副作用）把每个
    # 字符 bbox 变换到显示坐标系，让新旧两版坐标对齐。实测 rev01 `0.018` 旋转后
    # bbox=(189.5,20.8,214.5,34.8)，与 rev04 的 (189.3,20.5,214.3,34.8) 几乎重合。
    rot_m = page.rotation_matrix if getattr(page, "rotation", 0) != 0 else None
    for blk in data.get("blocks") or []:
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines") or []:
            chars: List[Tuple[str, Any]] = []
            texts: List[str] = []
            for sp in ln.get("spans") or []:
                for ch in sp.get("chars") or []:
                    c = ch.get("c", "")
                    if not c:
                        continue
                    b = ch.get("bbox")
                    if not b:
                        continue
                    # 旋转归一化：把字符 bbox 映射到显示坐标系（rotation=0 时跳过）。
                    if rot_m is not None:
                        r = fitz.Rect(b) * rot_m
                        b = (r.x0, r.y0, r.x1, r.y1)
                    # 过滤越界「幽灵内容」：字符 bbox 完全落在页面裁剪框之外。
                    # 仅当 bbox 与页面矩形**零交集**且整体越界时才丢弃；略微出血
                    # 的正常字符（如斜体伸出的笔画）不受影响。此判断在**旋转后**
                    # 做——旋转前竖版页的正常字符坐标天然「越界」，不能据此丢弃。
                    bx0, by0, bx1, by1 = (float(v) for v in b)
                    if (bx1 < -_EDGE_TOL or bx0 > pw + _EDGE_TOL
                            or by1 < -_EDGE_TOL or by0 > ph + _EDGE_TOL):
                        continue
                    chars.append((c, tuple(float(v) for v in b)))
                    texts.append(c)
            if not chars:
                continue
            raw = "".join(texts)
            # 行尾页码 token 先剥掉（它随重排飘，不算改动；见 _strip_page_token）
            stripped = _strip_page_token(raw)
            if stripped != raw:
                raw = stripped
                chars = chars[:len(stripped)]
                texts = texts[:len(stripped)]
            if not raw.strip():
                continue
            lb = ln.get("bbox") or [0, 0, 0, 0]
            y0 = float(lb[1]) / h
            y1 = float(lb[3]) / h
            in_edge = (y1 <= HF_TOP or y0 >= HF_BOTTOM)
            if drop_header_footer and in_edge:
                continue
            # ⚠️ 页码只在**页边带**里才当页码（2026-10-11 修，合成留出集 S4 抓到）
            #    原来是无条件按内容判：正文 / 表格里整格就是「150」「20」这种纯数字，
            #    被 `_PAGE_NUM_RE` 当成页码**整行滤掉** → 那些格子根本不进字符流，
            #    结果是「设定值 150→180」在清单里报了、标注版上一处都没标
            #    （两条链路口径不一致；`-80` 因带负号不匹配才侥幸躲过，故现象不对称）。
            #    页码是**页边**的东西，加位置约束后表格数字照常参与比对。
            if ignore_page_number and in_edge and _is_page_number(raw.strip()):
                continue
            norm = _norm(raw, ignore_case, ignore_punct)
            _eff = norm.replace(" ", "")
            if len(_eff) < min_len:
                # ⚠️ 2026-10-28（DHR-1245 rev15→rev16 第 3 页实测）：**单个正文汉字不能丢**。
                #    正文被挤压重排时，行末最后一个字会单独掉到下一行 ——
                #    旧版 `…box /PE袋装产品入运输箱`（一行），新版拆成
                #    `…into shipping box /PE袋装产品入运输` + `箱`（独立一行）。
                #    这里把单字行当「装饰符号行」滤掉，字符流里新版就少了这个 `箱`，
                #    旧版那行反而对不上 → 报成「`箱` 被删除」。实际一个字都没删，
                #    只是被挤到了下一行（用户看到的就是这种假删除）。
                # 判据只放行**单个字母/汉字**（`str.isalpha()`：汉字属 Lo，为 True）；
                # 单个数字、标点、符号（`•` `♦` `√` `□` 页码）继续按原样滤掉 ——
                # 它们数量一变就刷成片噪音，本身也不承载信息。
                if not (len(_eff) == 1 and _eff.isalpha()):
                    continue
            xs = [c[1][0] for c in chars] + [c[1][2] for c in chars]
            ys = [c[1][1] for c in chars] + [c[1][3] for c in chars]
            rows.append(Row(text=raw, norm=norm, chars=chars,
                            rect=(min(xs), min(ys), max(xs), max(ys)),
                            page=page_idx,
                            font=_dominant_font(ln.get("spans") or []),
                            size=_dominant_size(ln.get("spans") or [])))
    return rows


def _dominant_font(spans: List[dict]) -> str:
    """一行里**字数最多**的 span 的字体签名 `"字体名|字号"`。

    ⚠️ 字体名先剥掉子集前缀（`ABCDEE+SimSun` → `SimSun`）：嵌入子集的前缀是
    产出器随机生成的，同一台机器两次导出都会变 —— 拿它当判据会满篇假阳性。
    """
    best, best_n = "", -1
    for sp in spans:
        n = len(sp.get("chars") or [])
        if n > best_n:
            best_n = n
            name = str(sp.get("font") or "")
            if "+" in name:
                name = name.split("+", 1)[1]
            try:
                size = round(float(sp.get("size") or 0.0), 1)
            except Exception:
                size = 0.0
            best = "%s|%.1f" % (name, size)
    return best if best_n > 0 else ""


def _dominant_size(spans: List[dict]) -> float:
    """一行里**字数最多**的 span 的**渲染字号**（pt，round 到 0.1）。

    与 `_dominant_font` 同源（同一个「字数最多 span」），只是单独把字号抠出来，
    供 `_extract_font_changes` 精确比较字号差（阈值 FONT_MIN_DSIZE）。
    """
    best, best_n = 0.0, -1
    for sp in spans:
        n = len(sp.get("chars") or [])
        if n > best_n:
            best_n = n
            try:
                best = round(float(sp.get("size") or 0.0), 1)
            except Exception:
                best = 0.0
    return best if best_n > 0 else 0.0


# ---------------- 字体变动检测（已废除，2026-10-14 / 2026-10-15 指令C清理）-------
# 2026-10-14：删除「字体」颜色桶（4.0 分类二维化）——文字相同、字体/字号变了，
# 不再单独报「字体」差异，交行级抵消按「文字相同」直接抵消，渲染层差异由像素层兜底。
# 2026-10-15（指令C）：彻底清理 `_font_pair_changed` / `_extract_font_changes` /
# `FONT_MIN_DSIZE` / `FONT_CHANGE_MAX_FRAC` / `FONT_GUARD_MIN_ROWS` 死代码。
# 保留 `_dominant_font` / `_dominant_size`（仍被 `extract_rows` 填充 `Row.font/size`
# 字段使用，见 288-289 行），但 Row.font/size 字段目前无判定消费者。


# ---------------------------------------------------------------- 移动判据（5 倍比例）
# 2026-10-15 用户定稿：判「移动」还是「删+增」，不看绝对距离，看**比例** ——
#   移动距离（区域中心点位移）÷ 区域沿位移方向的"切片长度"。
#   ≤ MOVE_RATIO_MAX 倍 → 移动；> 它 → 删+增。
# 分母不是整个面积，而是「沿位移方向砍一刀的切片长度」：
#   · 很长的文字从上往下移 → 只看它垂直方向的厚度（一行高），不看横向宽度；
#   · 正方形区域从左下移到右上 → 沿位移矢量方向量那条斜切线。
# MOVE_RATIO_MAX 是估的，用真实样本标定后可调（AI 建议 3~8，模糊带见 Config）。
# 2026-10-15：常量上收至 Config（见文件头 _CFG），此处仅承接为模块级同名常量。
MOVE_RATIO_MAX = _CFG.move_ratio_max
MOVE_MIN_DIST = _CFG.move_min_dist
BLOCK_DX_TOL = _CFG.block_dx_tol
BLOCK_DY_TOL = _CFG.block_dy_tol
SLICE_MODE = _CFG.slice_mode
LOW_CONF_RATIO = _CFG.low_conf_ratio
FORMAT_RESID_MIN = getattr(_CFG, "format_resid_min", 0.02)  # 2026-10-16 已废弃，保留承接以免旧引用报错
FORMAT_NCC_MIN = _CFG.format_ncc_min
FORMAT_INK_MIN = _CFG.format_ink_min
TM_NCC_MOVE_MIN = _CFG.tm_ncc_move_min
TM_SHIFT_MIN_PT = _CFG.tm_shift_min_pt
TM_INK_MIN = getattr(_CFG, "tm_ink_min", 0.85)


def _center(rect: Tuple[float, float, float, float]) -> Tuple[float, float]:
    """矩形中心点 (cx, cy)。rect = (x0, y0, x1, y1)。"""
    return ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)


def _slice_len_along(rect: Tuple[float, float, float, float],
                     dx: float, dy: float,
                     mode: Optional[str] = None) -> float:
    """区域「沿位移方向 (dx,dy) 的切片长度」。

    两种算法（Config.slice_mode 切换，默认 projection）：
    - projection：外接矩形在位移单位向量上的投影长度 = |dx|/|d|*w + |dy|/|d|*h。
      物理直觉：物体沿某方向移动，量它沿这个方向的"身位长度"。
    - chord：过中心的弦长 = min(w/|ux|, h/|uy|)。
      两种模式对真实样本（移动几乎纯垂直/水平）结果一致，标定脚本可并排比较。

    公式（projection）：|dx|/|d| * w + |dy|/|d| * h，其中 d=(dx,dy)、w=宽、h=高。
    """
    w = abs(rect[2] - rect[0])
    h = abs(rect[3] - rect[1])
    norm = (dx * dx + dy * dy) ** 0.5
    if norm < 1e-9:
        # 位移为零：无所谓方向，用对角线作基准（其实这种情况不会走到这，防除零）
        return (w * w + h * h) ** 0.5
    m = mode or SLICE_MODE
    if m == "chord":
        ux = abs(dx) / norm
        uy = abs(dy) / norm
        if ux < 1e-9:
            return h
        if uy < 1e-9:
            return w
        return min(w / ux, h / uy)
    return (abs(dx) / norm) * w + (abs(dy) / norm) * h


# 位移判定的三态结果（2026-10-15 定稿，撤掉模糊带/灰框）：
#   0 = 未移动（位移小于噪声下限，内容相同位置相同 → 交给抵消）
#   1 = 移动（位移 > 噪声下限 且 比例 ≤ MOVE_RATIO_MAX → 标蓝）
#   2 = 删+增（位移比例 > MOVE_RATIO_MAX → 旧位红、新位绿）
#   比例落在 (MOVE_RATIO_MAX, LOW_CONF_RATIO] 的块仍判移动（标蓝），
#   但 `_move_state` 会额外返回「低置信」标记，由调用方写入低置信度清单。
_MOVE_NONE, _MOVE_YES, _MOVE_FAR = 0, 1, 2

# 位移最小距离 / 块聚类容差已上收至 Config（见上方 _CFG 与同名模块常量）。


def _move_state(rect_a: Tuple[float, float, float, float],
                rect_b: Tuple[float, float, float, float]
                ) -> Tuple[int, float, bool]:
    """按「5 倍比例」判据，返回 `(状态, 比值, 是否低置信)`。

    - _MOVE_NONE ：位移 < MOVE_MIN_DIST（噪声），内容相同位置相同，交抵消；
    - _MOVE_YES  ：位移 ≥ 下限 且 比例 ≤ MOVE_RATIO_MAX → 移动（蓝）；
    - _MOVE_FAR  ：比例 > MOVE_RATIO_MAX → 删+增（红+绿）。
    比例落在 (MOVE_RATIO_MAX, LOW_CONF_RATIO] 的块仍判 _MOVE_YES（标蓝），
    但 `low_conf=True` —— 调用方据此把该块写进「低置信度清单」，不做灰框
    （2026-10-15 用户拍板：4~6 仍标蓝，撤销 _MOVE_REVIEW 灰框）。
    """
    ax, ay = _center(rect_a)
    bx, by = _center(rect_b)
    dx, dy = bx - ax, by - ay
    dist = (dx * dx + dy * dy) ** 0.5
    if dist < MOVE_MIN_DIST:
        return _MOVE_NONE, 0.0, False
    base = _slice_len_along(rect_a, dx, dy)
    if base < 1e-6:
        if dist >= 1.0:
            return _MOVE_FAR, float("inf"), False
        return _MOVE_NONE, 0.0, False
    ratio = dist / base
    if ratio <= MOVE_RATIO_MAX:
        return _MOVE_YES, ratio, False
    if ratio <= LOW_CONF_RATIO:
        return _MOVE_YES, ratio, True
    return _MOVE_FAR, ratio, False


def _extract_moved_rows(pa: List[List[Row]], pb: List[List[Row]],
                        pages_equal: bool = True) -> List[LocDiff]:
    """把「文字一样、位置变了」的行配成对，**聚类成块**后按定稿规则三态分流。

    在 `_cancel_identical` **之前**跑（对称于 `_extract_font_changes`）：抵消按
    文本做、位置无关，「整块下移」的行会被它当无差异抵消掉、再也标不出浅蓝。

    ⚠️ 2026-10-15 重构2 关键：位移判据是**按块**（用户拍板），不是按行。
    一整段文字（连续多行）被整体推下去，要用**整块的高度**做分母 ——
    单行高 9pt、整段被推 100pt，若按行算是 11 倍（误判删增），按块算
    （整段几十行高）则远小于 5 倍 → 正确判「移动」。

    ⚠️ 2026-10-28 两条边界（用户定稿，实测数据见修复报告第五节）：
      · **一页只配一页**。第 N 页只跟第 N 页的行配，跨页配对一律不做；
      · **页数不等就不配**（`pages_equal=False` 直接返回空）。

    流程：
      1. 同页逐行配对（文本相同、中心点位移最近）；短行另走「同页唯一」窄门；
      2. 把「旧版里垂直连续 + 新版里也垂直连续 + 位移方向一致」的配对聚成块；
      3. 对每块用**块整体外接矩形**算位移比例（_move_state）：
         · < MOVE_MIN_DIST → 噪声，交抵消；
         · ≤ 5 倍 → 整块 moved（浅蓝）；
         · > 5 倍 → 整块 removed + added（删+增，旧红新绿）；
         · **短行块例外**：只许标蓝，判到删增就整块放弃 —— 摘走短行是
           「漏标」的唯一入口（DHR-1245 就是这么丢的），宁可交字符流照实报。
      4. 判定后的行从 pa/pb 摘掉，避免字符流 diff 重复报。
    """
    # ---- 第 0 步：页数不等直接不配（2026-10-28 用户定稿）----
    # 页数对不上时「第 N 页对第 N 页」这个前提本身就失效了（中间插了一页，后面
    # 全部错位）。任何按页位置做的配对都不可信，与其配出一堆错位蓝框/红绿框，
    # 不如不做：整页增删由 `page_diffs` 报，正文改动由**全局字符流**照实报
    # （字符流是把全文摊平后一次 diff 的，中间插页它天然对得齐）。
    if not pages_equal:
        return []

    # ---- 第 1 步：**同页**逐行配对，收集 (旧行, 新行) 对 ----
    # ⚠️ 2026-10-28 用户定稿：一页只跟一页配，不许拿第 1 页的行去配第 3 页。
    #   实测（9 个真实样本、1778 对配对）跨页配对只有 3 对，且 3 对全是错的
    #   ——「页眉的版本号配上了另一页正文的同形数字」这种。零价值、纯噪声，
    #   DHR-1245 的页眉漏标就是它把旧版那行摘走造成的。
    #   跨页整段搬运**不需要**蓝框：长行由 `_cancel_identical` 位置无关地静默
    #   抵消（内容没变就不算差异），硬报一个跨页蓝框反而是噪声。
    pairs: List[Tuple[Row, Row]] = []
    short_pairs: List[Tuple[Row, Row]] = []
    for pi in range(min(len(pa), len(pb))):
        ba: Dict[str, List[Row]] = {}
        bb: Dict[str, List[Row]] = {}
        for r in pa[pi]:
            if r.rect is not None:
                ba.setdefault(r.norm, []).append(r)
        for r in pb[pi]:
            if r.rect is not None:
                bb.setdefault(r.norm, []).append(r)

        page_pairs: List[Tuple[Row, Row]] = []
        for norm, la in ba.items():
            lb = bb.get(norm)
            if not lb:
                continue
            # 长度按**有效字数**（去掉空白）算：`_norm` 保留空白，页眉里单独占一行
            # 的 `15 `（数字 + 尾随空格）会算成 3 字，把闸门骗过去。
            n_eff = len(norm.replace(" ", ""))
            if n_eff < _MOVED_MIN_CHARS:
                # 短行（≤2 有效字）：**只有「这一页两边各只出现一次」才配**。
                # 此时它必然是同一处内容，没有「配错对象」的余地 —— 页眉唯一一个
                # `15` 移到别处，本来就该标蓝而不是报一对红绿。
                # 同页出现多处同形短串就不配：页眉的 15、表格里的 15、页码 15
                # 长得一模一样，挑谁都是猜。这些交给短行抵消失效后的字符流照实报。
                if len(la) == 1 and len(lb) == 1:
                    short_pairs.append((la[0], lb[0]))
                continue
            used_b: List[bool] = [False] * len(lb)
            for x in la:
                best_j, best_dist = None, None
                for j, y in enumerate(lb):
                    if used_b[j]:
                        continue
                    ax, ay = _center(x.rect)
                    bx, by = _center(y.rect)
                    d = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
                    if best_dist is None or d < best_dist:
                        best_dist, best_j = d, j
                if best_j is None:
                    continue
                used_b[best_j] = True
                page_pairs.append((x, lb[best_j]))

        # ---- 第 1.5 步：行配对顺序校验（2026-10-15 指令C）----
        # 同栏内新旧配对必须保持**阅读顺序单调**（重复文字「名称：示例…」出现
        # 多次时，贪心最近距离可能配交叉 → 位移方向错 → 整块误判）。这里对每个 norm
        # 的多对，按旧版 y 排序后检查新版 y 是否也单调递增；出现逆序（配交叉）说明是
        # 「互换/顺序颠倒」，按规则应判删增而非移动 —— 摘掉，交字符流 diff 报删增。
        # ⚠️ 用 id() 做 key：Row 是 @dataclass（eq=True 默认 → __hash__=None，不可
        # 哈希），不能直接塞进 set；而且两行 norm 相同但对象不同，值相等判定会误伤。
        # ⚠️ 2026-10-28：必须在**页内**做。以前拿全文档的 pairs 按 norm 分组，同一个
        # `15` 在第 1/2/3 页各有一对时会被混成一组，按旧版 y 排序后新版 y 跨页归零，
        # 看着像「全部逆序」→ 一律摘掉（同页配对的成果被自己吃掉）。
        by_norm: Dict[str, List[Tuple[Row, Row]]] = {}
        for x, y in page_pairs:
            by_norm.setdefault(x.norm, []).append((x, y))
        _bad_pairs = set()
        for norm, group in by_norm.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda p: _center(p[0].rect)[1])
            _new_ys = [_center(p[1].rect)[1] for p in group]
            _inv = sum(1 for i in range(len(_new_ys) - 1)
                       if _new_ys[i] > _new_ys[i + 1] + 1.0)
            if _inv > 0:
                _bad_pairs.update((id(x), id(y)) for x, y in group)
        if _bad_pairs:
            page_pairs = [p for p in page_pairs
                          if (id(p[0]), id(p[1])) not in _bad_pairs]
        pairs.extend(page_pairs)

    # ---- 第 2 步：把配对聚成「块」----
    # 块 = 「被**同一个整体平移**带走的、内容连贯的一批行」。
    # 关键（2026-10-15 用户定稿）：判据是「位移向量一致」，**不是**物理相邻。
    # ⚠️ 多栏排版下，同一段被平移的行，会跟「没动/微动」的地址行、注册证号行
    # 在 y 坐标上**交错**。若按 y 顺序线性扫描聚类，会被这些行切断（实测
    # 「上市前研究」整段被地址行 dy=8 切成 7 块碎片）。
    # 所以这里用**全局位移向量分组**（不看物理相邻）：dy 落在同一区间 → 同一块。
    def _cluster(pairs: List[Tuple[Row, Row]]) -> List[List[Tuple[Row, Row]]]:
        # 按 (page, 位移向量量化) 分组：同页 + dy 落在同一「梯度带」的行归一块。
        # 梯度带宽度 BLOCK_DY_TOL，dy 从 116→75 渐变也能被几条相邻带覆盖；
        # 但 dy≈8（地址行）和 dy≈101（正文）会落进不同带 → 正确分开。
        groups: Dict[Tuple[int, int], List[Tuple[Row, Row]]] = {}
        order: List[Tuple[int, int]] = []
        for x, y in pairs:
            dx = dy = 0.0
            if x.rect and y.rect:
                dx = _center(y.rect)[0] - _center(x.rect)[0]
                dy = _center(y.rect)[1] - _center(x.rect)[1]
            if (dx * dx + dy * dy) ** 0.5 < MOVE_MIN_DIST:
                continue
            # 量化位移向量：dy 按 BLOCK_DY_TOL 取整、dx 按 BLOCK_DX_TOL 取整
            key = (x.page, int(round(dy / BLOCK_DY_TOL)), int(round(dx / BLOCK_DX_TOL)))
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((x, y))
        # 相邻梯度带合并：dy 渐变的段（如 116→104→101→97→89→84→75）
        # 落在相邻几个带，需要并成一块。做法：按 dy 带号排序，相邻带且 dx 一致则合并。
        blocks: List[List[Tuple[Row, Row]]] = []
        # 简化：先按 (page, dy带, dx带) 排序，把「dy 带号连续」的合并
        sorted_keys = sorted(order, key=lambda k: (k[0], k[1], k[2]))
        cur: List[Tuple[Row, Row]] = []
        last_page = last_dy_band = last_dx_band = None
        for key in sorted_keys:
            page, dy_band, dx_band = key
            if last_page is None:
                cur = list(groups[key])
                last_page, last_dy_band, last_dx_band = page, dy_band, dx_band
                continue
            # 合并条件：同页 + dx 带一致 + dy 带号相邻（差 1）或相同
            if (page == last_page and dx_band == last_dx_band
                    and abs(dy_band - last_dy_band) <= 1):
                cur.extend(groups[key])
                last_dy_band = dy_band
            else:
                blocks.append(cur)
                cur = list(groups[key])
                last_page, last_dy_band, last_dx_band = page, dy_band, dx_band
        if cur:
            blocks.append(cur)
        return blocks

    blocks = _cluster(pairs)

    # ---- 第 2.5 步：块内位移一致性二次切分（2026-10-15 指令C）----
    # `_cluster` 用 dy 带量化分组，dy 带号太粗时（BLOCK_DY_TOL=20 下 dy=1 与 dy=7.9
    # 同落 band 0），会把「位移明显不同」的行混进同一块（实测 moved[0] 混入
    # dy=1.0 的页眉标题行，主群 dy=7.9）。
    # ⚠️ 但实测发现：14.0~15.0 段是**复杂被动位移**——12.0/13.0/15.0 标题 dy
    # 分别 116/110/75，正文 dy 84~116，位移量不是物理 y 的单调函数（15.0 标题
    # 在 14.0 正文之后、位移反而更小）。按 dy 排序切分会把这段「同一整体平移」
    # 误切成多个子块，单行块 ratio 巨大 → 误判删增（实测 `15.0` 被报删增）。
    # 结论：**不做 dy 排序切分**。位移向量分组 + 相邻带合并已能正确把「地址行
    # dy=8 与正文 dy=101」分开（band 0 vs band 5），页眉 dy=1.0 与正文 dy=7.9
    # 同 band 0 的混块是**低风险**（整体仍判 moved，不误删增），留待标定脚本
    # 导出后人工判定，不在引擎里贸然切分。
    blocks = _cluster(pairs)            # 长行块：允许三态（含删+增）
    # ⚠️ 2026-10-28：短行块**只许标蓝**。短串一旦被判「删+增」，就要把这行从
    #   字符流里摘走 —— 那是「漏标」的唯一入口（DHR-1245 页眉 15 就这么丢的）。
    #   判到删增就整块放弃，行留在原地，让字符流照实报（顶多报成红+绿，不会漏）。
    sblocks = _cluster(short_pairs)

    # ---- 第 3 步：按块判定 ----
    moved: List[LocDiff] = []
    drop_a: set = set()
    drop_b: set = set()
    LAST_LOW_CONF.clear()
    for blk, allow_far in ([(b, True) for b in blocks]
                           + [(b, False) for b in sblocks]):
        # 块的整体外接矩形（旧版侧 + 新版侧）
        xs = [p[0] for p in blk]
        ys = [p[1] for p in blk]
        ra_rects = [x.rect for x in xs if x.rect]
        rb_rects = [y.rect for y in ys if y.rect]
        if not ra_rects or not rb_rects:
            continue
        box_a = (_min0(ra_rects), _min1(ra_rects), _max2(ra_rects), _max3(ra_rects))
        box_b = (_min0(rb_rects), _min1(rb_rects), _max2(rb_rects), _max3(rb_rects))
        st, ratio, low_conf = _move_state(box_a, box_b)
        # 被动位移判据（2026-10-15 指令C）：块位移 ≈ 同栏上方净插入/删除行高之和
        # → 一律判移动，不看比值（被上方内容顶下来/抽上去）。
        if st == _MOVE_FAR and _passive_shift_explains(pa, pb, xs, ys, box_a, box_b):
            st, low_conf = _MOVE_YES, False
        if st == _MOVE_FAR and not allow_far:
            # 短行块判到删+增：说明「这一页两处同形短串」到底是不是同一处，
            # 光凭位移比值说不清。不摘行、不发框，交字符流照实报（宁多标不漏标）。
            continue
        if st == _MOVE_NONE:
            continue                      # 整块基本没动 → 交抵消
        if st == _MOVE_YES:
            # 整块移动（标蓝）。比值落低置信带 → 记入 LAST_LOW_CONF，仍标蓝。
            if low_conf:
                LAST_LOW_CONF.append({
                    "page": xs[0].page, "ratio": round(ratio, 3),
                    "dist": round((_center(box_b)[0] - _center(box_a)[0]) ** 2
                                  + (_center(box_b)[1] - _center(box_a)[1]) ** 2, 3),
                    "text": " ".join(x.text for x in xs)[:60],
                })
            # 2026-10-15 标注定稿：**整块只画一个框**（旧版一个、新版一个）——
            # 块内各行的矩形并集作为这一条的 rects，不再逐行发 LocDiff。
            moved.append(LocDiff(
                kind="moved",
                page_old=xs[0].page, page_new=ys[0].page,
                old_text=" ".join(x.text for x in xs), new_text=" ".join(y.text for y in ys),
                rects_old=_pad([r for x in xs if x.rect for r in [x.rect]]),
                rects_new=_pad([r for y in ys if y.rect for r in [y.rect]])))
        else:
            # 整块删+增
            for x, y in blk:
                moved.append(LocDiff(kind="removed",
                                     page_old=x.page, old_text=x.text,
                                     rects_old=_pad([x.rect]) if x.rect else []))
                moved.append(LocDiff(kind="added",
                                     page_new=y.page, new_text=y.text,
                                     rects_new=_pad([y.rect]) if y.rect else []))
        for x in xs:
            drop_a.add(id(x))
        for y in ys:
            drop_b.add(id(y))

    for rows in pa:
        rows[:] = [r for r in rows if id(r) not in drop_a]
    for rows in pb:
        rows[:] = [r for r in rows if id(r) not in drop_b]
    return moved


def _passive_shift_explains(pa: List[List[Row]], pb: List[List[Row]],
                            xs: List[Row], ys: List[Row],
                            box_a: Tuple[float, float, float, float],
                            box_b: Tuple[float, float, float, float]) -> bool:
    """被动位移判据（2026-10-15 指令C）：块被上方内容净插入/删除「顶下去/抽上来」。

    判据（保守，只在 ratio 已判 _MOVE_FAR 时兜底）：
      · 位移基本纯纵向（横向分量 ≤ BLOCK_DX_TOL）；
      · 块在旧版上方存在「位移 ≈ 0 的锚点行」（说明上方不是整栏一起被顶，锚点还在）；
      · 块位移量 ≈ 上方净插入/删除的行高之和（容差 ±1 行高）。
    满足 → 判被动位移（移动），不看比值。块越界（移出页）或与邻块先后顺序变了
    → 不满足（返回 False，交删增）。
    """
    # 纵向位移分量
    dy = (_center(box_b)[1] - _center(box_a)[1])
    dx = (_center(box_b)[0] - _center(box_a)[0])
    if abs(dx) > BLOCK_DX_TOL:
        return False                         # 不是纯纵向 → 不是被顶下去
    if abs(dy) < MOVE_MIN_DIST:
        return False
    # 块的行高估算（中位数）
    hs = sorted((r[3] - r[1]) for r in (x.rect for x in xs if x.rect))
    if not hs:
        return False
    row_h = hs[len(hs) // 2]
    if row_h < 1e-3:
        return False
    # 块越界检查：新位置是否仍在页内（用**新版**该页行范围估页高 —— 不能用旧版，
    # 否则「上方插内容把块顶下」的新位置会被误判越界，S10 实测踩过）。
    page = xs[0].page
    if page < len(pa) and page < len(pb):
        pa_rows = pa[page]
        pb_rows = pb[page]
        if pb_rows:
            page_h = max((r.rect[3] for r in pb_rows if r.rect), default=0.0)
            if page_h > 0:
                if box_b[3] > page_h * 1.05 or box_b[1] < 0:
                    return False             # 越界 → 不判被动位移
    # 锚点：旧版该页上、同 x 带内、位移≈0 的行（位移 < MOVE_MIN_DIST）
    x0, x1 = box_a[0], box_a[2]
    anchor = False
    for r in pa_rows if page < len(pa) else []:
        if r.rect is None:
            continue
        # 上方且 x 重叠
        if r.rect[3] <= box_a[1] and r.rect[2] > x0 and r.rect[0] < x1:
            # 该行在新版里位移≈0？
            for r2 in pb_rows:
                if r2.norm == r.norm and r2.rect is not None:
                    d = abs(_center(r2.rect)[1] - _center(r.rect)[1])
                    if d < MOVE_MIN_DIST:
                        anchor = True
                        break
            if anchor:
                break
    if not anchor:
        return False                         # 上方无锚点 → 无法佐证是被顶，保守不判
    # 位移量需能被「若干行高」解释（容差 ±1 行高）
    n = abs(dy) / row_h
    frac = n - round(n)
    return abs(frac) * row_h <= row_h       # 落在某个整数行高 ±1 行内


def _min0(rects):
    return min(r[0] for r in rects)


def _min1(rects):
    return min(r[1] for r in rects)


def _max2(rects):
    return max(r[2] for r in rects)


def _max3(rects):
    return max(r[3] for r in rects)


def _extract_format_changes(pa: List[List[Row]], pb: List[List[Row]],
                            old_path: str, new_path: str) -> List[LocDiff]:
    """格式变化检测（2026-10-16 指令D 重写）：文字相同、位置基本相同、但**渲染不同**
    （加粗/换字体/换字号）的行 → 旧红 + 新绿。

    在 `_cancel_identical` 之前调用（此时文字相同的行还在 pa/pb）。判据改为
    **行块级模板匹配 + NCC + 墨量比**（替换上一轮的像素残差占比方案）：
      1. 初筛：文字 norm 相同 + 位置位移 < MOVE_MIN_DIST（原地行）；
      2. 字体签名初筛：`Row.font` 不同 或 `Row.size` 差 ≥ 0.5pt（缩小像素比对范围）；
      3. 模板匹配确认：按文字层配对位移在 ±15pt 窗口内做 NCC 模板匹配，
         取最佳位移与 NCC，并算墨量比 min/max。
         · NCC < FORMAT_NCC_MIN 或 墨量比 < FORMAT_INK_MIN → 报旧红+新绿；
         · 否则忽略（仅位移/重排，NCC≥阈值且墨量比≥阈值）。
      （实测 11vs12：加粗混排行 NCC≈0.6~0.76、墨量比<0.88；未变/仅推移行 ≥0.93）

    返回 `kind="removed"`(旧红) + `kind="added"`(新绿) 各一条（两侧同一段字）。
    """
    def _rows(pages):
        return [r for rows in pages for r in rows]

    ra, rb = _rows(pa), _rows(pb)
    cb: Dict[str, List[Row]] = {}
    for r in rb:
        cb.setdefault(r.norm, []).append(r)

    # 候选：文字相同 + 位移 < MOVE_MIN_DIST（原地）+ 字体签名不同
    cand: List[Tuple[Row, Row]] = []
    for x in ra:
        lb = cb.get(x.norm)
        if not lb:
            continue
        for y in lb:
            if x.rect and y.rect:
                d = ((_center(y.rect)[0] - _center(x.rect)[0]) ** 2
                     + (_center(y.rect)[1] - _center(x.rect)[1]) ** 2) ** 0.5
                if d >= MOVE_MIN_DIST:
                    continue
            # 字体签名初筛
            # 字体比较只看字体名（Row.font 形如 'SimSun|7.5'，含字号）；
            # 字号差异由后面的 size 阈值单独判，0.1pt 微调不该触发格式变化。
            _fn = lambda f: (f or "").split("|")[0]
            if _fn(x.font) != _fn(y.font) or (x.size and y.size and abs(y.size - x.size) >= 0.5):
                cand.append((x, y))
                break

    if not cand:
        return []

    # 模板匹配确认（NCC + 墨量比）
    da = fitz.open(old_path)
    db = fitz.open(new_path)
    try:
        out: List[LocDiff] = []
        drop_a: set = set()
        drop_b: set = set()
        # 窗口 ±15pt（覆盖文字层 bbox 与墨迹的系统性偏移，二-1 实测约 10~15pt）
        win = (15.0 * 1.0, 15.0 * 1.0)  # (px) —— 这里按 pt 传给模板匹配的下层换算
        for x, y in cand:
            if not x.rect or not y.rect:
                continue
            if x.page >= da.page_count or y.page >= db.page_count:
                continue
            r = _row_template_match(da[x.page], db[y.page], x.rect, y.rect,
                                    win_px=(15.0 * 150 / 72.0, 15.0 * 150 / 72.0),
                                    dpi=150)
            if r is None:
                continue
            ncc_val, _dx, _dy, ink_ratio, _io, _in = r
            if ncc_val >= FORMAT_NCC_MIN and ink_ratio >= FORMAT_INK_MIN:
                continue          # 渲染一致（仅位移/重排），忽略
            out.append(LocDiff(kind="removed", page_old=x.page, old_text=x.text,
                               rects_old=_pad([x.rect])))
            out.append(LocDiff(kind="added", page_new=y.page, new_text=y.text,
                               rects_new=_pad([y.rect])))
            drop_a.add(id(x))
            drop_b.add(id(y))
    finally:
        da.close()
        db.close()

    # 摘除已报的行，避免字符流 diff 重复报
    for rows in pa:
        rows[:] = [r for r in rows if id(r) not in drop_a]
    for rows in pb:
        rows[:] = [r for r in rows if id(r) not in drop_b]
    return out




def _row_template_match(old_page, new_page,
                        rect_old: Tuple[float, float, float, float],
                        rect_new: Tuple[float, float, float, float],
                        win_px: Tuple[float, float] = (150.0, 420.0),
                        dpi: int = 150, pad_pt: float = 4.0,
                        ) -> Optional[Tuple[float, float, float, float, float, float]]:
    """行块级模板匹配（2026-10-16 指令D）：指令二/三/四共用的核心。

    以旧页 `rect_old` 渲染的墨迹块为模板，在新页 `rect_new` 的 ±win_px 窗口内
    做归一化互相关（NCC）模板匹配，返回最佳位移与相似度。

    返回 `(ncc, dx_pt, dy_pt, ink_ratio, ink_old, ink_new)`：
      · ncc        —— 归一化互相关系数（-1~1，1=完全一致）
      · dx_pt/dy_pt —— 最佳位移（pt，新相对旧的偏移）
      · ink_ratio  —— 墨量比 min/max（0~1，1=两侧墨量相同；加粗会显著 <1）
      · ink_old/ink_new —— 两侧墨量（深色像素占比 0~1）
    返回 None = 无法比较（页面缺失/窗口太小/渲染失败）。

    ⚠️ 性能：搜索窗可能较大（±420px@150dpi 纵向），用 numpy FFT 相关加速，
    不引 scipy。粗到细两阶段：FFT 全窗粗定位 → 亚像素级局部精搜。
    """
    try:
        import numpy as _np
        import cv2 as _cv2
        scale = dpi / 72.0
        # 模板 = 旧页 rect_old（外扩 pad 含字形渲染）
        r0 = fitz.Rect(rect_old[0] - pad_pt, rect_old[1] - pad_pt,
                       rect_old[2] + pad_pt, rect_old[3] + pad_pt)
        pm_t = old_page.get_pixmap(dpi=dpi, clip=r0, colorspace=fitz.csGRAY,
                                   annots=False)
        T = _np.frombuffer(pm_t.samples, dtype=_np.uint8).reshape(
            pm_t.height, pm_t.width).astype(_np.float64)
        th, tw = T.shape
        if th < 4 or tw < 4:
            return None
        Tf = T.astype(_np.float32)
        # 纯色模板：NCC 分母为 0，无匹配可言（与 5.0 原判据同口径，提前到外面只算一次）
        if float(Tf.std()) < 1e-9:
            return None
        pw, ph = new_page.rect.width, new_page.rect.height

        def _match(wx_pt: float, wy_pt: float):
            """在「rect_new ± (wx_pt, wy_pt)」的窗里渲染并匹配一次。

            返回 `(ncc, bdx, bdy, S, rn)`；窗口退化/渲染失败返回 None。
            `bdx/bdy` 是匹配位置在该窗内的像素坐标，`rn` 是窗矩形（pt）。

            ⚠️ 提速A（2026-10-26，6.0）落在这里：模板与窗内「同位置子块」
            逐像素完全相等时，直接给 `(1.0, bdx0, bdy0)` 并跳过 matchTemplate。
            """
            rn = fitz.Rect(rect_new[0] - wx_pt, rect_new[1] - wy_pt,
                           rect_new[2] + wx_pt, rect_new[3] + wy_pt)
            rn.x0 = max(0.0, rn.x0); rn.y0 = max(0.0, rn.y0)
            rn.x1 = min(pw, rn.x1); rn.y1 = min(ph, rn.y1)
            if rn.width <= 0 or rn.height <= 0:
                return None
            pm_s = new_page.get_pixmap(dpi=dpi, clip=rn, colorspace=fitz.csGRAY,
                                       annots=False)
            S = _np.frombuffer(pm_s.samples, dtype=_np.uint8).reshape(
                pm_s.height, pm_s.width).astype(_np.float64)
            sh, sw = S.shape
            if sh < th or sw < tw:
                return None

            # ---- 提速A：同位置逐像素完全一致 → NCC 上界 1.0、位移 0 ----
            if FAST_MATCH:
                _bx0 = int(round((r0.x0 - rn.x0) * scale))
                _by0 = int(round((r0.y0 - rn.y0) * scale))
                if (0 <= _bx0 <= sw - tw and 0 <= _by0 <= sh - th
                        and _np.array_equal(T, S[_by0:_by0 + th,
                                                 _bx0:_bx0 + tw])):
                    return (1.0, _bx0, _by0, S, rn)

            # ---- 归一化互相关（NCC）模板匹配 ----
            # 分子 = Σ(T-T̄)(S-Ś)，分母 = sqrt(Σ(T-T̄)² · Σ(S-Ś)²)。
            #
            # ⚠️ 2026-10-19 性能改写（**判定公式一字未改，只换实现**）：
            #   原实现是 Python 双层循环逐位置手算 NCC，每个位置 3 次 numpy reduce。
            #   DOC-1009(rev03↔rev04) 剖析：183 次调用累计 **79.6s**，是整份文件最大
            #   的一块（比 OCR 的 84s 还硬）。现改走 cv2.matchTemplate 的
            #   TM_CCOEFF_NORMED —— OpenCV 定义与此处公式**完全一致**
            #   （T'=T-T̄、I'=I-Í，R=ΣT'I'/sqrt(ΣT'²·ΣI'²)），C++ 全窗一次算完。
            #   原先「粗扫步长 3 → ±3 精扫」只是为省 CPU 做的近似，会漏掉真正的最优
            #   位置；现在全窗精扫，只会更准、不会更差。
            #   唯一必须补的差异：原逻辑 denom<1e-9 时 `continue`（纯色窗口不参与），
            #   而 cv2 对常数窗口会返回 1.0（假满分）—— 用积分图算局部方差把它屏蔽掉。
            Sf = S.astype(_np.float32)
            res = _cv2.matchTemplate(Sf, Tf, _cv2.TM_CCOEFF_NORMED)
            _np.nan_to_num(res, copy=False, nan=-2.0, posinf=-2.0, neginf=-2.0)
            # 局部方差（积分图 O(n)）：窗口内几乎无墨 → 与原逻辑同口径判无效
            _Sd = S
            _ii1 = _cv2.integral(_Sd)
            _ii2 = _cv2.integral(_Sd * _Sd)
            _npx = float(th * tw)

            def _box_sum(I):
                return (I[th:sh + 1, tw:sw + 1] - I[0:sh - th + 1, tw:sw + 1]
                        - I[th:sh + 1, 0:sw - tw + 1] + I[0:sh - th + 1, 0:sw - tw + 1])

            _s1, _s2 = _box_sum(_ii1), _box_sum(_ii2)
            res[_s2 - _s1 * _s1 / _npx < 1e-6] = -2.0
            # ⚠️ **搜索集合必须与原实现逐点一致**，否则区域数会变（实测全窗精扫会让
            #    DOC-1009 从 58 处变 65 处 —— 多出的 7 处是「更优位置」带来的新位移，
            #    属于擅自改判定，不是提速）。原实现是：
            #      ① 粗扫：只考察 dy%3==0 且 dx%3==0 的网格点，取最优；
            #      ② 精扫：在粗最优 ±3px 的方窗内逐点，取代价更小者。
            #    res 的每个元素本来就是该位置的 NCC，所以「按网格挑最优」等价于
            #    在 res 上做掩码取 max，不需要重算。
            vy, vx = sh - th + 1, sw - tw + 1
            coarse = _np.full_like(res, -2.0)
            coarse[::3, ::3] = res[::3, ::3]
            _, _c0, _, (_cx0, _cy0) = _cv2.minMaxLoc(coarse)
            _y0 = max(0, _cy0 - 3); _y1 = min(vy, _cy0 + 4)
            _x0 = max(0, _cx0 - 3); _x1 = min(vx, _cx0 + 4)
            if _y1 <= _y0 or _x1 <= _x0:            # 窗口退化（极小模板）
                return (float(_c0), int(_cx0), int(_cy0), S, rn)
            _win = res[_y0:_y1, _x0:_x1]
            _, ncc_val, _, (_ox, _oy) = _cv2.minMaxLoc(_win)
            return (float(ncc_val), int(_x0 + _ox), int(_y0 + _oy), S, rn)

        # 搜索窗（px 转 pt）
        wx_pt, wy_pt = win_px[0] / scale, win_px[1] / scale
        hit = None
        # ---- 提速B（2026-10-26，6.0）：先小窗试，贴边/低分/满分都回退大窗 ----
        # 绝大多块「本来就在原位」：小窗即可定案，省下大窗的渲染与相关运算。
        # 三道门都过才采用小窗结果，否则一律回退原大窗（兜底不漏）：
        #   ① 不贴边 —— 命中落在小窗边界说明真实最优可能在窗外；
        #   ② NCC ≥ MATCH_HINT_NCC_MIN —— 小窗只看得见 ±14pt，若附近有**长得像**
        #      的别的行（实测「旋转12RPM」附近几行文字相似），小窗会锁在近处，
        #      而原全窗逻辑可能选到更远处那个真最优。分数不够就是证据不足，
        #      必须让大窗重新裁决 —— 否则等于改变 5.0 的判定。
        #   ③ NCC < 1.0 —— 小窗出现「满分」却让提速A没命中，说明完全匹配发生在
        #      **别的位置**（位移正好等于整数像素），此时全窗里往往还有别的 1.0
        #      位置，选哪一个由 minMaxLoc 的扫描顺序决定。多解选位必须交回大窗，
        #      才能复现 5.0 的原选择（实测否则会有 0.16~0.32pt 的选位差）。
        if FAST_MATCH_HINT:
            hx = min(wx_pt, MATCH_HINT_WIN_PX / scale)
            hy = min(wy_pt, MATCH_HINT_WIN_PX / scale)
            if hx < wx_pt or hy < wy_pt:
                first = _match(hx, hy)
                if first is not None:
                    _vx1 = first[3].shape[1] - tw
                    _vy1 = first[3].shape[0] - th
                    _bx1, _by1 = first[1], first[2]
                    _edge = (_bx1 <= 0 or _by1 <= 0
                             or _bx1 >= _vx1 or _by1 >= _vy1)
                    if (not _edge
                            and MATCH_HINT_NCC_MIN <= first[0] < 1.0):
                        hit = first
        if hit is None:
            hit = _match(wx_pt, wy_pt)
        if hit is None:
            return None
        ncc_val, bdx, bdy, S, rn = hit

        # ---- 墨量比：模板墨量 vs 匹配处墨量 ----
        sub = S[bdy:bdy + th, bdx:bdx + tw]
        ink_old = float((T < 128).mean())
        ink_new = float((sub < 128).mean())
        ink_ratio = (min(ink_old, ink_new) / max(ink_old, ink_new)
                     if max(ink_old, ink_new) > 1e-6 else 1.0)

        # 位移（pt）= (搜索窗原点 - 模板原点) + (匹配像素偏移)/scale
        dx_pt = (rn.x0 - r0.x0) + bdx / scale
        dy_pt = (rn.y0 - r0.y0) + bdy / scale
        return (float(ncc_val), float(dx_pt), float(dy_pt),
                float(ink_ratio), ink_old, ink_new)
    except Exception:
        return None




def _cluster_shift_in_rect(src_page, dst_page, rect, dpi: int = 300,
                           win_pt: Tuple[float, float] = (60.0, 40.0),
                           ) -> List[Tuple[Tuple[float, float, float, float],
                                           Tuple[float, float, float, float]]]:
    """对一个矩形内的墨迹做**簇级位移检测**（2026-10-16 指令D 二-2 核心）。

    矩形内按行切分（水平投影+谷底）→ 行内按垂直投影切墨迹簇 → 每簇在新页
    ±win_pt 窗口做 `_row_template_match`。返回 `[(旧簇rect, 新簇rect), ...]`
    （NCC ≥ TM_NCC_MOVE_MIN 且位移 > TM_SHIFT_MIN_PT 的命中对）。

    为什么必须簇级（11vs12 二-2 实测）：2.0 供应方式区是「标签不动、正文右移
    5.5~14.4pt」的行内相对位移 —— 整块匹配无法对齐（ov=0.587 不达标、NCC 被
    错位拉低），只有对**位移的那部分墨迹**（正文簇）单独匹配才能同时拿到
    高 NCC 与 >2pt 位移。
    """
    import numpy as _np
    hits: List[Tuple[Tuple[float, float, float, float],
                     Tuple[float, float, float, float]]] = []
    try:
        scale = dpi / 72.0
        pm = src_page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)
        arr = _np.frombuffer(pm.samples, dtype=_np.uint8).reshape(
            pm.height, pm.width)
        ink_all = arr < 128
        rx0, ry0, rx1, ry1 = rect
        r0 = max(0, int(ry0 * scale)); r1 = min(pm.height, int(ry1 * scale))
        c0 = max(0, int(rx0 * scale)); c1 = min(pm.width, int(rx1 * scale))
        if r1 - r0 < 4 or c1 - c0 < 8:
            return hits
        seg = ink_all[r0:r1, c0:c1]
        row_ink = seg.sum(axis=1)
        peak = int(row_ink.max()) if len(row_ink) else 0
        if peak <= 0:
            return hits
        thr = max(1, int(peak * 0.08))
        # 行切分（谷底）
        bands: List[Tuple[int, int]] = []
        start = None
        for y in range(seg.shape[0]):
            has = row_ink[y] >= thr
            if has and start is None:
                start = y
            elif not has and start is not None:
                bands.append((start, y)); start = None
        if start is not None:
            bands.append((start, seg.shape[0]))
        for (bs, be) in bands:
            # v67：最小行高 4.0→3.0pt —— 补查/热瓦片矩形按瓦片边界裁行，行被
            # 裁剩 3~4pt 时（11vs12 运输条件行实测）不该整行放弃。
            if (be - bs) / scale < 3.0 or (be - bs) / scale > 24.0:
                continue
            bseg = seg[bs:be, :]
            col_ink = bseg.sum(axis=0)
            clusters: List[Tuple[int, int]] = []
            cst = None
            gap = 0
            for x in range(len(col_ink)):
                if col_ink[x] >= 1:
                    if cst is None:
                        cst = x
                    gap = 0
                elif cst is not None:
                    gap += 1
                    if gap > 8:
                        clusters.append((cst, x - gap)); cst = None; gap = 0
            if cst is not None:
                clusters.append((cst, len(col_ink) - 1))
            merged: List[List[int]] = []
            for cl in clusters:
                if merged and (cl[0] - merged[-1][1]) / scale < 6.0:
                    merged[-1][1] = cl[1]
                else:
                    merged.append([cl[0], cl[1]])
            for (cs, ce) in merged:
                w_pt = (ce - cs + 1) / scale
                # v67：最小簇宽 8.0→6.0pt —— 「有效期 三年」的正文簇只有
                # 7.2pt 宽（两个汉字），8.0 门槛把它整簇滤掉 → 有效期行漏报。
                # 0.5/1.4pt 的竖线噪点簇仍被滤住。
                if w_pt < 6.0 or w_pt > 300.0:
                    continue
                crect = (rx0 + cs / scale, ry0 + bs / scale,
                         rx0 + (ce + 1) / scale, ry0 + be / scale)
                m = _row_template_match(src_page, dst_page, crect, crect,
                                        win_px=(win_pt[0] * scale,
                                                win_pt[1] * scale), dpi=dpi)
                if m is None:
                    continue
                ncc_val, dx_pt, dy_pt, ink_ratio, _io, _in = m
                shift = (dx_pt * dx_pt + dy_pt * dy_pt) ** 0.5
                # ⚠️ 墨量比不作硬门槛（11vs12 实测：行内相对位移下模板边界含
                # 相邻内容差异，墨量比被拉到 0.5~0.6，设门槛会漏）。判据 =
                # 位移>2pt 且 NCC≥TM_NCC_MOVE_MIN（0.50，实测分离带内）。
                if shift > TM_SHIFT_MIN_PT and ncc_val >= TM_NCC_MOVE_MIN:
                    nrect = (crect[0] + dx_pt, crect[1] + dy_pt,
                             crect[2] + dx_pt, crect[3] + dy_pt)
                    hits.append((crect, nrect))
    except Exception:
        pass
    return hits




# ---------------------------------------------------------------- 全局抵消
def _cancel_identical(pa: List[List[Row]], pb: List[List[Row]],
                      pages_equal: bool = True) -> None:
    """把两份文档里**逐字完全相同（只是位置可能变了）**的行全局抵消掉。

    位置无关：不管这行在旧版第几页、新版第几页，只要内容一字不差就抵消。
    抵消后剩下的行才是「真新增 / 真删除 / 真改写」，后续比对不会再被换位重排干扰。
    直接就地修改两个列表。

    ⚠️ 2026-10-28 两处修正（DHR-1245 页眉版本号 `15→16` 三页全漏的真凶）
    ------------------------------------------------------------------
    ① **预算是全局的，不能每页重置。**
       原实现 `for rows in pa: _strip(rows, dict(common))` 每页都新建一份预算，
       于是「旧版某行出现 4 次、新版只出现 1 次」时，两边各页都能抵一次 ——
       实际抵消量可以远超 `min(总数)`。实测：版本号在旧版 3 页页眉 + 正文共 4 处，
       新版只有正文 1 处，本该只抵 1 对，结果旧版被抵掉 3 行、新版页眉的 3 个
       `16` 也被抵掉 3 行 —— 页眉改动直接蒸发。

    ② **短行必须「同页就近」配对才能抵，不许按文本计数抓。**
       「这几处都有 `15`」不代表是同一段内容：页眉右上角的版本号、正文表格里的
       数值、页码、年份，长得一模一样但彼此无关。原来按文本计数取 min、遇到就抵，
       于是「页眉的 `15`」被拿去跟「另一页正文的 `15`」抵掉（实测第 3 页页眉
       `15→16` 就是被同页正文那个 `15` 抵掉的），剩下的错位配对再把标记画到
       别的坐标上。现在短行要在**同一页上、位置最近、且位移 ≤ `_CANCEL_NEAR_PT`**
       才能抵 —— 这就等价于「同一处内容没动」。位置差得远（或者对面这页压根
       没有）→ 不抵，交给位移判定 / 字符流照实报。
       长行仍走位置无关的计数抵消：整段重排、跨页搬运是它的主战场，按位置判反而
       会失效。
    """
    ca = Counter(r.norm for rows in pa for r in rows)
    cb = Counter(r.norm for rows in pb for r in rows)

    def _eff(n: str) -> int:
        return len(n.replace(" ", ""))

    def _budget() -> Dict[str, int]:
        """长行预算：位置无关，抵消量 = 两侧条数取小，**两侧各一份、跨全部页共享**。"""
        return {n: min(ca[n], cb[n]) for n in ca
                if n in cb and min(ca[n], cb[n]) > 0 and _eff(n) >= _CANCEL_NEAR_MIN}

    # ---- 短行：同页就近配对（贪心取最近，一对一，位移超限不配）----
    # ⚠️ 2026-10-28：页数不等时这一步**整体跳过**。它靠「第 N 页对第 N 页」，
    #   页数对不上（中间插页/删页）时第 N 页根本对不上第 N 页，配出来的对是
    #   不相干的两处内容 —— 抵掉了真改动、还把标记画到别的坐标上。
    #   跳过后的后果仅是「短行不再抵消」：相同文字两边都有，全局字符流照样
    #   对得齐，不会冒出假差异。长行抵消与页无关，照常执行。
    short_drop_a: set = set()
    short_drop_b: set = set()
    for i in (range(min(len(pa), len(pb))) if pages_equal else ()):
        ba: Dict[str, List[Row]] = {}
        bb: Dict[str, List[Row]] = {}
        for r in pa[i]:
            if r.rect and _eff(r.norm) < _CANCEL_NEAR_MIN:
                ba.setdefault(r.norm, []).append(r)
        for r in pb[i]:
            if r.rect and _eff(r.norm) < _CANCEL_NEAR_MIN:
                bb.setdefault(r.norm, []).append(r)
        for norm, la in ba.items():
            lb = bb.get(norm)
            if not lb:
                continue
            used = [False] * len(lb)
            for x in la:
                ax, ay = _center(x.rect)
                best, best_d = None, None
                for j, y in enumerate(lb):
                    if used[j]:
                        continue
                    bx, by = _center(y.rect)
                    d = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
                    if best_d is None or d < best_d:
                        best, best_d = j, d
                if best is None or best_d is None or best_d > _CANCEL_NEAR_PT:
                    continue
                used[best] = True
                short_drop_a.add(id(x))
                short_drop_b.add(id(lb[best]))

    def _strip(rows: List[Row], budget: Dict[str, int], drop: set) -> List[Row]:
        kept: List[Row] = []
        for r in rows:
            if id(r) in drop:
                continue                # 短行：已就近配到同一处（见上面 ②）
            if _eff(r.norm) >= _CANCEL_NEAR_MIN and budget.get(r.norm, 0) > 0:
                budget[r.norm] -= 1
                continue
            kept.append(r)
        return kept

    # 预算按侧各一份、跨全部页共享（见上面 ①）
    bud_a = _budget()
    for rows in pa:
        rows[:] = _strip(rows, bud_a, short_drop_a)
    bud_b = _budget()
    for rows in pb:
        rows[:] = _strip(rows, bud_b, short_drop_b)


# ---------------------------------------------------------------- 字符流比对
# 2026-10-27：原 _substantial 闸门（「两侧都无实义字 → 丢」）会把纯标点替换
# （如半角逗号→全角逗号）整条吞掉，与 pdfdiff_noise 规则表「逗号动了必须报」矛盾。
# 已改用 noise.noise_only（剥装饰符后相同才丢）+ noise.strip_noise（碎块纯装饰才丢），
# 语义标点改动（,→， ：→： 括号）现在正确报出。装饰符改动（•→◦ 删•）照旧不报。


def _pad(rects: Sequence[Tuple[float, float, float, float]]
         ) -> List[Tuple[float, float, float, float]]:
    return [(x0, y0 - RECT_PAD, x1, y1 + RECT_PAD) for x0, y0, x1, y1 in rects]


def _page_w(rows: List[Row]) -> float:
    """从一页的行坐标推断页面宽度（pt）。无行时回落 A4 宽度 595。"""
    xs = [r.rect[2] for r in rows if r.rect]
    return max(xs) if xs else 595.0


def _page_h(rows: List[Row]) -> float:
    """从一页的行坐标推断页面高度（pt）。无行时回落 A4 高度 842。"""
    ys = [r.rect[3] for r in rows if r.rect]
    return max(ys) if ys else 842.0


def _visual_order(rows: List[Row]) -> List[Row]:
    """把一页的行按**人眼阅读顺序**排：先上后下，再左到右。

    rawdict 吐出来的行序是 PDF 内部的块顺序，改版后这个顺序可能变
    （实测 DOC-1007 新版就把两处「生理」的块顺序挪了位）。
    不先归一成视觉顺序，同一条文字在两份文件里的先后不同，
    字符流一比就会整段错位 → 又是假差异。
    """
    return sorted(rows, key=lambda r: (round(r.rect[1], 1) if r.rect else 0.0,
                                       round(r.rect[0], 1) if r.rect else 0.0))


def _stream(rows: Sequence[Row], ignore_case: bool, ignore_punct: bool
            ) -> List[Tuple[str, str, Any, int]]:
    """把行摊成**一条字符流**：每项 = (比对用的字, 原文的字, 自己的 bbox, 页码)。

    ⚠️ 空白（含换行）**不进流**。这正是「重新折行不算改动」的立足点：
    旧版两行、新版一行，摊成字符流后完全一样，diff 自然报 0 处。
    """
    out: List[Tuple[str, str, Any, int]] = []
    for r in rows:
        for c, bb in r.chars:
            if c.isspace():
                continue
            if ignore_punct and not c.isalnum():
                continue
            out.append((c.lower() if ignore_case else c, c, bb, r.page))
    return out


def _group(entries: Sequence[Tuple[str, str, Any, int]]
           ) -> List[Dict[str, Any]]:
    """把一段改动里的一串字按「同一行」归堆，方便报成一条差异。

    不归堆的话，删掉一整段免责声明会报成 200 条一条一个字的差异，
    列表没法看、标注也会画 200 个小方块。
    """
    groups: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    prev_y: Optional[float] = None
    for _key, c, bb, pg in entries:
        y = round(float(bb[1]), 1)
        if (cur is not None and cur["page"] == pg and prev_y is not None
                and abs(y - prev_y) <= _GROUP_GAP):
            cur["rects"].append(tuple(bb))
            cur["chars"].append(c)
        else:
            cur = {"page": pg, "rects": [tuple(bb)], "chars": [c]}
            groups.append(cur)
        prev_y = y
    return groups


def _opcodes(ta: str, tb: str) -> List[Tuple[str, int, int, int, int]]:
    """逐字 diff 的操作码，并把**太短的「相等段」并入改动**。

    为什么要多这一步（2026-09-29 实测的坑）：
    整份文档摊成一条长字符流后，diff 会把**两个不相干的相同字符**当成「相等」——
    实测 DOC-1007：旧版 `Rev06` 里的 `6` 被对到了新版修订日期 `2026` 里的 `6`，
    结果「版本号 6→7」这处真改动整个漏标，标记反而跑到日期上去了。

    短相等段（< _TINY_ANCHOR 个字）本来就不可能是可靠锚点，
    一律当改动处理，这类「巧合配对」就没了；相邻的改动再合并成一块，
    免得同一处改动被切成「删 + 增 + 删」一堆碎块。
    """
    raw = SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes()
    out: List[Tuple[str, int, int, int, int]] = []
    for op, i1, i2, j1, j2 in raw:
        if op == "equal" and (i2 - i1) < _TINY_ANCHOR:
            out.append(("replace", i1, i2, j1, j2))
        else:
            out.append((op, i1, i2, j1, j2))
    merged: List[Tuple[str, int, int, int, int]] = []
    for op, i1, i2, j1, j2 in out:
        if merged and op != "equal" and merged[-1][0] != "equal":
            _, pi1, _pi2, pj1, _pj2 = merged[-1]
            merged[-1] = ("replace", pi1, i2, pj1, j2)
        else:
            merged.append((op, i1, i2, j1, j2))
    return _snap_windows(ta, tb, merged)


_SNAP_MAX = 2              # 改动窗口最多往左挪这么多字（见 _snap_windows）


def _alnum_cut(t: str, p: int) -> int:
    """位置 p 是不是把一段「ASCII 字母数字串」拦腰切开了（1 = 是）。

    只认 ASCII 字母数字：汉字一个字就是一个词，切开不算切错。
    """
    if p <= 0 or p >= len(t):
        return 0
    a, b = t[p - 1], t[p]
    return 1 if (a.isascii() and b.isascii() and a.isalnum() and b.isalnum()) else 0


def _window_cuts(ta: str, tb: str, i1: int, i2: int, j1: int, j2: int) -> int:
    """一处改动窗口两端「切开字母数字串」的切口数（0~4）。"""
    n = 0
    if i1 == i2:
        n += _alnum_cut(ta, i1)               # 纯增删：a 侧只有一个切口
    else:
        n += _alnum_cut(ta, i1) + _alnum_cut(ta, i2)
    if j1 == j2:
        n += _alnum_cut(tb, j1)
    else:
        n += _alnum_cut(tb, j1) + _alnum_cut(tb, j2)
    return n


def _snap_windows(ta: str, tb: str,
                  ops: List[Tuple[str, int, int, int, int]]
                  ) -> List[Tuple[str, int, int, int, int]]:
    """把改动窗口整体**往左**挪一挪，让切口不落在「字母数字串」中间。

    为什么需要它（2026-09-29 用户实测打回来的）
    ------------------------------------------
    DOC-1007 地址行的真改动是：`…示例路1号` 后面插入 `1栋101室，`，
    再在 `1栋101室` **前面**插入 `2栋101室，3栋101室，`。
    但 `SequenceMatcher` 取的是「最长匹配块」，它把 `1栋101室` 的 `1`
    跟 `1栋101室` 的 `1` 对上了（这样相等段能多一个字），于是报出来的新增文字
    变成了 `1栋101室，2栋101室，1` —— 开头少一个字、末尾多卷进 `13` 的 `1`，
    整条高亮**右移了一个字**。用户原话：「他把多标的地方插到原本的 13 中间去了，
    但他其实是加在 13 的前面」。

    两种切法的编辑距离**完全一样**（相等段总长相同），所以不是「谁更优」的问题，
    只能靠「切口落点」判。两条实测定下来的规矩：

      · **切口不许切开字母数字串** —— 挪完必须严格变好（切口数变少），否则一个字不动；
      · **只往左挪** —— `SequenceMatcher` 的偏差方向是「把相等块尽量往右伸」，
        反向挪没有依据。实测日期 `2025年12月19日→2026年05月27日` 那处，
        往右挪能让两个切口都变成「类转换」，看着像变好了，实际会把
        `年`、`日` 两个没变的字卷进标记里（标记变粗）—— 禁止右挪正好挡住它。
      · **挪完不能把操作码的划分弄坏** —— 窗口里两侧要**仍然不同**，划给相邻
        `equal` 段的字要**两侧一致**。这两条是 2026-09-29 补的，踩过的坑：
        汉字不算「切口」，于是把窗口挪到一对**相同**的汉字上时切口数会「免费」
        降到 0 —— 看着变好了，实际标的是没变的字。实测
        `FAS 成功率 98.99% → 99.99%` 就栽在这：窗口从 `8` 挪到 `率`，
        报出来的改动成了「率 → 率」，右边那个 equal 段也变成
        `98.99%`/`99.99%` 这种「标着相等却两边不同」的坏划分。

    挪窗口时相邻的 equal 段要跟着伸缩，保证操作码仍然是对两个字符串的一个划分。
    """
    n = len(ops)
    for k, (op, i1, i2, j1, j2) in enumerate(ops):
        if op == "equal":
            continue
        base = _window_cuts(ta, tb, i1, i2, j1, j2)
        if base == 0:
            continue
        best = None
        # ⚠️ 顺序必须是「由近到远」：挪得越少越好。写成 range(-N,0) 会让 -2 先试，
        #    两个距离同样干净时反而选了挪得更远的那个（实测地址那处会多卷进一个「，」）。
        for d in sorted(range(-_SNAP_MAX, 0), key=abs):
            if i1 + d < 0 or j1 + d < 0:
                continue
            if ta[i1 + d:i1] != tb[j1 + d:j1]:
                continue                      # 挪进来的字两侧必须一致（等长段天然满足）
            if i2 != i1 and (i2 + d) - (i1 + d) == 0:
                continue                      # 别把 replace/delete 的窗口挪空
            if j2 != j1 and (j2 + d) - (j1 + d) == 0:
                continue
            # ⚠️ 挪完窗口里两侧内容必须**仍然不同**。不查这条会出事：汉字不算「切口」
            #    （`_alnum_cut` 只认 ASCII），所以把窗口挪到一对相同的汉字上，
            #    切口数会「免费」降到 0 —— 看着变好了，其实标的是没变的字。
            #    实测 `FAS 成功率 98.99% → 99.99%` 就栽在这：窗口从 `8` 挪到 `率`，
            #    报出来的改动成了「率 → 率」。
            if ta[i1 + d:i2 + d] == tb[j1 + d:j2 + d]:
                continue
            # 划给右邻 equal 段的字必须两侧一致，否则那个 equal 段就成了
            # 「标着 equal 却两边不同」——上面的 98.99% vs 99.99% 就是这么来的。
            if k + 1 < n and ops[k + 1][0] == "equal":
                a1, b1 = ops[k + 1][1], ops[k + 1][3]
                if ta[a1 + d:a1] != tb[b1 + d:b1]:
                    continue
            # 左邻 equal 段会被缩短 |d|，别缩成负长度
            if k > 0 and ops[k - 1][0] == "equal" and ops[k - 1][2] + d < ops[k - 1][1]:
                continue
            c = _window_cuts(ta, tb, i1 + d, i2 + d, j1 + d, j2 + d)
            if c < base and (best is None or c < best[0]):
                best = (c, d)
        if best is None:
            continue
        d = best[1]
        ops[k] = (op, i1 + d, i2 + d, j1 + d, j2 + d)
        if k > 0 and ops[k - 1][0] == "equal":
            o, a1, a2, b1, b2 = ops[k - 1]
            ops[k - 1] = (o, a1, a2 + d, b1, b2 + d)
        if k + 1 < n and ops[k + 1][0] == "equal":
            o, a1, a2, b1, b2 = ops[k + 1]
            ops[k + 1] = (o, a1 + d, a2, b1 + d, b2)
    return ops


def locate(old_path: str, new_path: str, ignore_case: bool = False,
           ignore_punct: bool = False, drop_header_footer: bool = False,
           prog=None, skip_pages=None) -> List[LocDiff]:
    """比对两份 PDF，返回**带坐标**的差异列表。

    先做行级全局抵消（去重排/换位的假差异），再把两份文档摊成字符流做一次
    逐字 diff；每条 LocDiff 的 rects 已是页面坐标系下的矩形，标注时直接画。

    `skip_pages`（2026-10-29）：**两侧都不参与比对**的页号集合（0 基）。
    拼接 PDF 里那些扫描页由 `pdfdiff_preflight.assess()["skip_pages"]` 给出 ——
    扫描页没有文字层、纸上还有噪声和倾斜，逐像素比出来全是假差异。
    做法是让这些页「两侧都抽不出行」，于是它们在字符流里是空的：
      · 不产生任何删/增（两侧都空，没有可比的东西）；
      · `_mixed_ocr_fill` 也不会给它们补 OCR 行（那一步只处理「恰好一侧为空」）；
      · 页数不变，整页增删那条分支不受影响。
    ⚠️ 页数不同的两个文件本来就不许比（预检会拦），所以「同号页」就是对得上的。

    ⚠️ 字符流里的空白（含换行）不参与比对 —— 重新折行不再算改动（见模块说明）。
    """
    _skip = set(int(i) for i in (skip_pages or ()))
    _reset_ocr_stats()      # 6.1：每次对比重置兜底统计
    # 图片输入（PNG/JPG/TIFF…）先折成单页 PDF、Word/Excel 先转成 PDF ——
    # 与扫描件 PDF 同路，见 `pdfdiff_img` / `pdfdiff_office` 模块说明
    # （不为图片或 Office 各开一条比对分支）。
    import pdfdiff_office as _off
    import pdfdiff_noise as _noise
    # ⚠️ 这里**只用图片转换 / Office 转 PDF，不做 /Rotate 归一化**（指令F 一-4 重做）。此前用
    # `_img.ensure_pdf`（内部含 `normalize_rotation`）把 `/Rotate` 页 `set_rotation(0)`，
    # 但内容层坐标一个字节没动 —— 对 PyMuPDF 而言这是**有害**的：PyMuPDF 本会按
    # /Rotate 自动把页面显示成横版、`page.rect` 也给横版尺寸，归零后反而退成竖版
    # 595×842、字符 bbox 仍是竖版原始坐标，与横版对方页对不上 → 满页假「删+增」。
    # 旋转交由 `extract_rows` 用 `page.rotation_matrix` 正确处理（见该函数内注释）。
    old_path = _off.ensure_input(old_path)
    new_path = _off.ensure_input(new_path)
    da = fitz.open(old_path)
    db = fitz.open(new_path)
    # 每页嵌入图片（位图）的 rect 列表，供「删行连图标一起标红」用（指令C 修改项6）。
    img_rects_old: List[List[Tuple[float, float, float, float]]] = []
    img_rects_new: List[List[Tuple[float, float, float, float]]] = []
    # 每页**真实渲染尺寸**（page.rect，已应用 /Rotate）。指令F 一-4：整页增删
    # 画整页框要用真实尺寸，不能靠字符 bbox 推断（竖排/越界字符会把它带偏）。
    page_sizes_old: List[Tuple[float, float]] = []
    page_sizes_new: List[Tuple[float, float]] = []
    try:
        # `skip_pages` 里的页两侧都抽空 → 不进字符流（见函数说明）
        pa = [[] if i in _skip else
              extract_rows(da[i], i, ignore_case, ignore_punct, drop_header_footer)
              for i in range(da.page_count)]
        pb = [[] if i in _skip else
              extract_rows(db[i], i, ignore_case, ignore_punct, drop_header_footer)
              for i in range(db.page_count)]
        page_sizes_old = [(float(da[i].rect.width), float(da[i].rect.height))
                          for i in range(da.page_count)]
        page_sizes_new = [(float(db[i].rect.width), float(db[i].rect.height))
                          for i in range(db.page_count)]
        for i in range(da.page_count):
            _rs = []
            for _img_info in da[i].get_images(full=True):
                for _r in da[i].get_image_rects(_img_info[0]):
                    _rs.append((_r.x0, _r.y0, _r.x1, _r.y1))
            img_rects_old.append(_rs)
        for i in range(db.page_count):
            _rs = []
            for _img_info in db[i].get_images(full=True):
                for _r in db[i].get_image_rects(_img_info[0]):
                    _rs.append((_r.x0, _r.y0, _r.x1, _r.y1))
            img_rects_new.append(_rs)
    finally:
        da.close()
        db.close()

    # ⓪ 混合输入补行：恰好一侧没有文字层时，给那一侧补 OCR 行（见 `_mixed_ocr_fill`）。
    #    不做这一步的话，扫描那侧在字符流里是空的 → 整份文档被报成「全部删除/全部新增」。
    checkpoint()          # 中止检查点：整页 OCR 补行是这一步里最贵的（扫件可达分钟级）
    _mixed_ocr_fill(pa, pb, old_path, new_path,
                    ignore_case=ignore_case, ignore_punct=ignore_punct)

    # ⓪-bis 整页增删（2026-10-15 指令C）：页数不同时，对多出的页报**整页一条**
    # `page_removed` / `page_added`（整页一个框），**不刷满页逐字框**。这些页的
    # 行从 pa/pb 摘除，不再进字符流 diff。
    page_diffs: List[LocDiff] = []
    # 页数是否相等：决定后面「按页位置」的判定（位移配对、短行就近抵消）能不能用。
    # ⚠️ 必须在截断**之前**取，截断之后 pa/pb 一定等长，信息就没了。
    pages_equal = (len(pa) == len(pb))
    if len(pa) > len(pb):
        for pi in range(len(pb), len(pa)):
            _pw, _ph = (page_sizes_old[pi]
                        if pi < len(page_sizes_old) else (_page_w(pa[pi]), _page_h(pa[pi])))
            page_diffs.append(LocDiff(
                kind="page_removed", page_old=pi,
                old_text="", rects_old=[(0, 0, _pw, _ph)]))
        pa = pa[:len(pb)]
    elif len(pb) > len(pa):
        for pi in range(len(pa), len(pb)):
            _pw, _ph = (page_sizes_new[pi]
                        if pi < len(page_sizes_new) else (_page_w(pb[pi]), _page_h(pb[pi])))
            page_diffs.append(LocDiff(
                kind="page_added", page_new=pi,
                new_text="", rects_new=[(0, 0, _pw, _ph)]))
        pb = pb[:len(pa)]

    # ① 先把两份文档的**全文**字符摊出来（判「是不是只是挪了位置」要用全文，
    #    因为一行可能已被第 ② 步抵消掉，但它的文字在对面文档里确实存在）
    fa = "".join(e[1] for e in
                 _stream([r for rows in pa for r in _visual_order(rows)],
                         ignore_case, ignore_punct))
    fb = "".join(e[1] for e in
                 _stream([r for rows in pb for r in _visual_order(rows)],
                         ignore_case, ignore_punct))

    # ②-bis 字体变动（2026-10-12）：文字一样、字体/字号变了的行，必须在
    #    行级抵消**之前**摘出来 —— 抵消按文本做，会把它们直接抵消掉。
    #    ⚠️ 2026-10-14（4.0 分类二维化）：已废除「字体」桶。字体/字号变了但
    #    文字相同的行，不再单独报「字体」差异，而是交给行级抵消按「文字相同」
    #    直接抵消掉（内容没变就不算差异）；其渲染层差异由像素层兜底标注。
    #    这里不再调用 `_extract_font_changes`。

    # ②-ter 位移（2026-10-12）：文字一样、位置变了的行，同样必须在行级抵消
    #    **之前**摘出来转 moved（浅蓝）—— 抵消位置无关，会把「整块下移」当
    #    无差异吃掉。
    if not pages_equal:
        _log.info("页数不等（旧 %d 页 / 新 %d 页）：跳过位移配对与短行就近抵消 "
                  "—— 按页位置判断的前提已失效，正文改动交全局字符流",
                  len(page_sizes_old), len(page_sizes_new))
    moved_diffs = _extract_moved_rows(pa, pb, pages_equal=pages_equal)

    # ②-quinquies 转曲区行块位移检测（2026-10-16 指令D 二-2）：
    #    ⚠️ 2026-10-16 定稿：**不在 locate() 做全页扫描**。实测（引擎自检 8 项
    #    失败 + 46s 耗时 + 大窗口假阳性）证明：无差别全页簇匹配把「纯插入」
    #    「远距搬移」都配成 moved。位移检测收敛到**graphic 补查路径**（v66）：
    #    只对「像素确有差异且文字层盖住」的候选矩形做簇级匹配（≥2 簇位移
    #    一致才报），那里有 hot 瓦片这个高置信前提，噪声可控。见
    #    `_cluster_shift_in_rect` 与 graphic_regions 补查段。

    # ②-quater 格式变化（2026-10-15 指令C）：文字相同、位置也基本相同、但**渲染
    #    不同**（加粗/换字体/换字号）的行，行级抵消会把它们当「文字相同」吃掉。
    #    这里在抵消**之前**，用字体签名初筛 + 像素残差确认，报成 removed(旧红)
    #    + added(新绿) 两条（旧值/新值都是同一段字，但标注在旧/新两侧）。
    checkpoint()          # 中止检查点：格式变化检测逐行算像素残差，长文档可达数十秒
    format_diffs = _extract_format_changes(pa, pb, old_path, new_path)

    # ② 行级全局抵消：内容一字不差、只是挪了位置的行先去掉（换位/重排不刷假差异）
    _cancel_identical(pa, pb, pages_equal=pages_equal)

    # ③ 摊成字符流（空白不进流 → 折行无关）
    sa = _stream([r for rows in pa for r in _visual_order(rows)], ignore_case, ignore_punct)
    sb = _stream([r for rows in pb for r in _visual_order(rows)], ignore_case, ignore_punct)
    ta = "".join(e[0] for e in sa)
    tb = "".join(e[0] for e in sb)
    if ta == tb:
        # 文字全同：只剩位移差异 + 格式变化也要报（2026-10-15 指令C）
        return page_diffs + moved_diffs + format_diffs

    # ④ 整份文档一次逐字 diff；变了的字，直接拿它自己的 bbox
    ops = _opcodes(ta, tb)
    out: List[LocDiff] = []
    n_total = max(1, len(ops))
    for k, (op, i1, i2, j1, j2) in enumerate(ops):
        checkpoint()      # 中止检查点：逐 opcode 走，一页上千条也停得住
        if prog:
            prog(k, n_total)
        if op == "equal":
            continue
        if _noise.noise_only(ta[i1:i2], tb[j1:j2]):
            continue   # 只差装饰符号（• ◦ 零宽符等）→ 不算改动；语义标点（,→，）必须报
        go = _group(sa[i1:i2])
        gn = _group(sb[j1:j2])
        if len(go) == 1 and len(gn) == 1:
            # 两侧各只落在同一行 → 就是「这行这几个字改了」。
            # 2026-10-13 删「变动」档：拆成删（旧值标红）+ 增（新值标绿）两条。
            out.append(LocDiff(kind="removed", page_old=go[0]["page"],
                               old_text="".join(go[0]["chars"]),
                               rects_old=_pad(go[0]["rects"])))
            out.append(LocDiff(kind="added", page_new=gn[0]["page"],
                               new_text="".join(gn[0]["chars"]),
                               rects_new=_pad(gn[0]["rects"])))
        else:
            # 跨行/只在一侧（整段增删、跨行重写）→ 逐行分开报，别糊成一大块
            for g in go:
                t = "".join(g["chars"])
                if not _noise.strip_noise(t).strip():
                    continue   # 碎块里掉出来的孤零零装饰符号（如「•」）不算改动
                out.append(LocDiff(kind="removed", page_old=g["page"],
                                   old_text=t, rects_old=_pad(g["rects"])))
            for g in gn:
                t = "".join(g["chars"])
                if not _noise.strip_noise(t).strip():
                    continue   # 同上
                out.append(LocDiff(kind="added", page_new=g["page"],
                                   new_text=t, rects_new=_pad(g["rects"])))

    # 修改项6（2026-10-15 指令C）：删行时把「同行嵌入图标（位图）」一起标红。
    # 「生产日期」整行删除时，同行的工厂图标（嵌入位图，无文字层）也要进红框。
    for d in out:
        if d.kind != "removed" or d.page_old is None:
            continue
        if d.page_old >= len(img_rects_old):
            continue
        _rs = img_rects_old[d.page_old]
        if not _rs or not d.rects_old:
            continue
        # removed 行矩形并集（去 pad 的影响：取原字符 rect 的并集）
        x0 = min(r[0] for r in d.rects_old)
        y0 = min(r[1] for r in d.rects_old)
        x1 = max(r[2] for r in d.rects_old)
        y1 = max(r[3] for r in d.rects_old)
        # 同行图标：y 中心落在行纵向范围内（外扩一个行高），且图标在行右侧或同列附近
        _row_h = (y1 - y0) or 8.0
        extra = []
        for _ir in _rs:
            _cy = (_ir[1] + _ir[3]) / 2.0
            if abs(_cy - (y0 + y1) / 2.0) <= _row_h:
                # 图标与行横向不重叠（在行的右侧/左侧），视为「同行附属图标」
                if _ir[2] >= x0 - _row_h or _ir[0] <= x1 + _row_h:
                    extra.append(_ir)
        if extra:
            d.rects_old.extend(_pad(extra))

    result = (page_diffs
              + moved_diffs
              + format_diffs
              + _ocr_rescue(_drop_moved(out, fa, fb), old_path, new_path))
    # 页码守卫（指令F 一-2）：越界即抛异常写日志，不把幽灵页码带出去。
    _guard_pages(result, len(page_sizes_old), len(page_sizes_new), "locate")
    return result


_MOVED_MIN = 2             # 少于这么多字的改动不去对面找「挪位」证据（免得把「8」这类真改动抹了）
#: 纯数字短于这么多位就**不拿「对面也有」当免责证据**（见 `_moved_evidence`）。
_MOVED_NUM_MIN = 5

# 位移标蓝（2026-10-15 重构2）：`_extract_moved_rows` 的判据已升级为
# `_move_state` 的「5 倍比例」（见 MOVE_RATIO_MAX / MOVE_MIN_DIST）。
# `_MOVED_MIN_CHARS` 仍在用（过短行不判位移）；`_MOVED_MIN_DY` 已被
# MOVE_MIN_DIST（中心点总位移下限）取代，仅保留常量以免 core 老链路引用报错。
_MOVED_MIN_DY = 3.0        # ⚠️ 已废弃：改用 MOVE_MIN_DIST（中心点总位移下限）
_MOVED_MIN_CHARS = 3       # 有效字数（去掉空白）< 它 = 「短行」：不参与贪心最近配对，
                           # 只在**同页两边各只出现一次**时配对，且判到删增就整块放弃。
                           # 目的：短串（修订号/表格值/页码）长得一样但互不相干，
                           # 一旦被摘进「删+增」就再也不会出现在字符流里 → 漏标。
#: 行级抵消里「短行」的门槛（有效字数）。短于此的行**必须同页就近配对**才抵 ——
#: 页眉的版本号、正文的表格值、页码长得一样但毫无关系，按文本计数去抵一定出错。
_CANCEL_NEAR_MIN = 8
#: 短行「就近」的位移上限（pt）。超过就不算「同一处内容没动」，交位移判定/字符流。
_CANCEL_NEAR_PT = 24.0



def _moved_evidence(t: str) -> bool:
    """`t` 够不够格把「对面文档里也有这段字」当成免责证据。

    两道闸（2026-10-28 补，DHR-1245 页眉版本号漏标的第二处病根）：
      · 至少 `_MOVED_MIN` 个字 —— 单字（`8`、`1`）在任何长文档里都有；
      · **短纯数字一律不认** —— 修订号、日期、温度、数量、批号正是核对的主角，
        「别处也有这个数」根本说明不了这处没改。
    """
    if len(t) < _MOVED_MIN:
        return False
    if t.isdigit() and len(t) < _MOVED_NUM_MIN:
        return False
    return True


def _cited_in(t: str, hay: str) -> bool:
    """`t` 在 `hay` 里是否有一处**站得住的**出现。

    为什么数字要整串判（2026-10-28 DHR-1245 实测）
    ----------------------------------------------
    页眉右上角版本号 `15 → 16` 这处真改动被整条抹掉，靠的就是两处假证据：
      · 判「新增 `16`」时去旧版全文找 —— 命中的是表格序号串 `16171819` 里的片段；
      · 判「删除 `15`」时去新版全文找 —— 命中的是 `WI-1015`、`FM-1715` 里的片段。
    纯子串匹配分不清「`15`」和「`1015` 里的 15」，两处巧合就凑成了一张假证据。
    规矩：**数字必须整串出现**（前后都不再挨着数字）才算证据；文字沿用原子串判据
    （汉字没有词边界，按整串判反而会漏掉「行被拆开」这种真实情况）。
    """
    if not t:
        return False
    if not t.isdigit():
        return t in hay
    i = hay.find(t)
    while i >= 0:
        j = i + len(t)
        left = hay[i - 1] if i > 0 else ""
        right = hay[j] if j < len(hay) else ""
        if not (left.isdigit() or right.isdigit()):
            return True
        i = hay.find(t, i + 1)
    return False


def _count_cited(t: str, hay: str) -> int:
    """`t` 在 `hay` 里按 `_cited_in` **同一口径**出现的次数。

    与 `_cited_in` 配对使用：`_cited_in` 判「有没有」，这里判「有几处」。
    `_drop_moved` 的净额约束要用这个数 —— 对面能承接几处，就只能免责几处
    （见该函数 ② 段说明）。
    """
    if not t:
        return 0
    if not t.isdigit():
        return hay.count(t)
    n = 0
    i = hay.find(t)
    while i >= 0:
        j = i + len(t)
        left = hay[i - 1] if i > 0 else ""
        right = hay[j] if j < len(hay) else ""
        if not (left.isdigit() or right.isdigit()):
            n += 1
        i = hay.find(t, i + 1)
    return n


def _drop_moved(items: List[LocDiff], flat_a: str, flat_b: str) -> List[LocDiff]:
    """残余清理：**一段字在对面文档里也能找到 = 内容只是挪了位置 → 不算改动**。

    行级抵消（第 ① 步）只认「整行一字不差」，抓不住这些情况：
      · 两栏排版下同一行的**视觉顺序**两份文件不同（实测「有效期三年」）；
      · 旧版的孤立小行被新版并进长行（实测页脚「示例医疗」）；
      · 一行被拆进不同的行、又被别的内容隔开。
    它们在字符流里表现成「这串字删了 + 同一串字又加了」——
    用户看到的就是「两边都有的文字却被标了」。

    与既有策略一致：**内容挪了位置不算改动**（第 ① 步就是这么定的）。

    ⚠️ 但「找得到」必须是真的找得到（2026-10-28 DHR-1245 页眉 15→16 漏标）：
    证据的门槛见 `_moved_evidence`（短纯数字不算），命中的判据见 `_cited_in`
    （数字必须整串出现）。原先是「2 个字以上 + 是子串就免责」，
    一个 3 页文件就能凑出假证据把真改动抹掉。

    判据分两级（2026-10-28 补第一级）：
      ① **同文本成对抵消**：这一侧「删掉的这段字」在对面**也是被当成新增报出来的**
         → 它就是同一段字挪了位置，两条一起撤。这是最强的证据（不需要去全文里找），
         也是唯一能给短内容用的证据 —— `050` 这种表格序号行上下挪一行，
         删/增两条文本一模一样，靠它撤掉，不会变成「删了 050 又加了 050」。
      ② 残余再走全文证据：`_moved_evidence` 过关（够长、不是短数字）且
         `_cited_in` 命中。短数字（`15`、`16`、`050`）**不进这一级** ——
         「别处也有这个数」说明不了这里没改，而这正是漏标的来源。
    """
    # ① 同文本成对抵消（removed 与 added 各取 min 条数互抵）
    rem_idx: Dict[str, List[int]] = {}
    add_idx: Dict[str, List[int]] = {}
    for k, d in enumerate(items):
        if d.kind == "removed":
            t = (d.old_text or "").strip()
            if t:
                rem_idx.setdefault(t, []).append(k)
        elif d.kind == "added":
            t = (d.new_text or "").strip()
            if t:
                add_idx.setdefault(t, []).append(k)
    gone: set = set()
    for t, ks in rem_idx.items():
        other = add_idx.get(t)
        if not other:
            continue
        n = min(len(ks), len(other))
        gone.update(ks[:n])
        gone.update(other[:n])

    # ② 残余：全文证据（长文本 / 非短数字）+ **净额约束**
    #    ⚠️ 2026-10-28（DHR-1245 rev15→rev16 第 3 页实测）：「对面也有这段字」只能
    #    证明这段**文字**在对面出现过，证明不了**这一处**没动。文本在文档里出现
    #    多次时（`【大灭菌柜】` 这种标签），这句话就完全不成立：
    #      旧版全文 6 处 / 新版 10 处（净增 4），diff 已报出的那 1 条 added
    #    `【大灭菌柜】` 却因为「旧版也有」被整条撤掉 → 用户看不到「这里新加了
    #    【大灭菌柜】」。这是漏标，方向必须反过来（宁可多标）。
    #    规矩：对面出现几次，最多只能承接几处；本侧比对面**多出来的**那几处
    #    一定是真增真删，不许免责。净额一致（纯挪位）才允许整段免责。
    rem_groups: Dict[str, List[int]] = {}
    add_groups: Dict[str, List[int]] = {}
    for k, d in enumerate(items):
        if k in gone:
            continue
        if d.kind == "removed":
            t = (d.old_text or "").strip()
            if t:
                rem_groups.setdefault(t, []).append(k)
        elif d.kind == "added":
            t = (d.new_text or "").strip()
            if t:
                add_groups.setdefault(t, []).append(k)

    def _excusable(t: str, ks: List[int], own: str, other: str) -> int:
        """这段字最多能免责几条：本侧实例数不得多于对面，多出来的必须报。"""
        if not _moved_evidence(t):
            return 0
        n_own = _count_cited(t, own)
        n_other = _count_cited(t, other)
        # 本侧比对面多出来的 → 真增/真删，必须报出来
        must_keep = max(0, n_own - n_other)
        return max(0, min(len(ks), n_other) - must_keep)

    excused: set = set()
    for t, ks in rem_groups.items():
        # removed 在旧版全文（flat_a）里，对面是新版（flat_b）
        n = _excusable(t, ks, flat_a, flat_b)
        excused.update(ks[:n])
    for t, ks in add_groups.items():
        n = _excusable(t, ks, flat_b, flat_a)
        excused.update(ks[:n])

    return [d for k, d in enumerate(items) if k not in gone and k not in excused]


# ---------------------------------------------------------------- 图形化文字（转曲）兜底
# 这一节解决的是「读文字」这条路**原理上够不到**的地方：文件里有些正文是**矢量曲线**
# （转曲文字），没有文字层可读 —— 见 graphic_regions 的说明。
GRAPHIC_DPI = 120          # 渲染分辨率（120dpi 够看清「这块内容变了没有」，也不慢）
GRAPHIC_TILE = 24          # 瓦片边长 (px)，120dpi 下 ≈ 14.4pt ≈ 一行字高
GRAPHIC_DARK = 60          # 单个像素灰度差超过它才算「这两个像素不一样」(0~255)
GRAPHIC_FRAC = 0.055       # 一块瓦片里「不一样」的像素占比达到它才算这块变了
GRAPHIC_MIN_TILES = 2      # 合并后的区域小于这么多瓦片就不报（去碎点）

# ---------------------------------------------------------------- 6.0 提速开关
# ⚠️ 两个开关都只影响**速度**，设计上不改变判定；出问题按下面说明单独关掉即可
#    （关掉 = 退回 5.0 的算法路径，行为逐字节一致）。
#
# FAST_MATCH（提速A）：同位置逐像素完全一致时，短路掉 cv2.matchTemplate。
#   原理：模板与搜索窗内「同位置子块」逐像素相等 ⇒ 该处 NCC 必为 1.0 且位移为 0，
#   而 1.0 已是 NCC 上界，全局最优不可能更好 —— 可直接返回。
#   ⚠️ 唯一已知边界：块内容在搜索窗内**周期重复**时（同样一行字出现多次），
#   原逻辑可能返回「另一个也是 1.0 的位置」；本短路返回「同位置」。这类情形
#   属于原逻辑的假位移误报，且极罕见（回归已逐字段核对）。
#   关：export PDFDIFF_FAST_MATCH=0
FAST_MATCH = os.environ.get("PDFDIFF_FAST_MATCH", "1") != "0"
#
# FAST_MATCH_HINT（提速B）：先用「同位置」小窗搜索，命中不在窗口边缘就直接采用；
#   落在边缘说明真实位移可能更大，回退大窗重搜（兜底不漏）。
#   原理：NCC 在任一位置的取值只取决于该位置的子图与模板，与搜索窗大小无关，
#   因此小窗内的最大值 = 大窗在同一范围内的最大值。
#   ⚠️ **默认关闭**（2026-10-26 实测结论）：小窗只有 ±14pt，而真实位移常达
#   15~50pt（整块下移一行）。此时小窗会「锁在最近的相似内容上」并给出高分，
#   于是把一个真实 15pt 位移判成「原位未动」—— 回归语料 S11 实测漏报 2 条
#   moved。这类错误是「漏报」，比慢严重，所以在证明安全之前不默认开。
#   仅作实验开关保留：用 PDFDIFF_FAST_MATCH_HINT=1 手动开启。
#   关：默认即关
FAST_MATCH_HINT = os.environ.get("PDFDIFF_FAST_MATCH_HINT", "0") != "0"
MATCH_HINT_WIN_PX = 28     # 小窗半径 (px)：28px@150dpi ≈ 13.4pt，足够覆盖行内小位移
MATCH_HINT_NCC_MIN = 0.98  # 小窗结果必须「近乎完全匹配」才采用，否则回退大窗

# FAST_TILE_FRAC（快速定位模式的瓦片阈值）：比完整模式的 `GRAPHIC_FRAC`(0.055) 低一档。
#   为什么单独给一个更低的阈值：完整模式靠「热点瓦片 + OCR 判定」两段，
#   阈值定得高一点（少起 OCR）也不会漏报；快速模式**没有 OCR 兜底**，
#   热点瓦片漏了就是真漏。2026-10-26 实测（转曲/图片样本）：
#     · curve_chg 与旧版只差 **50 个像素**（0.009%），热点瓦片最大占比 0.042
#       → 用 0.055 判 **0 处**（整份漏报），用 0.03 判 2 处（位置正确）。
#     · 同一文件整体平移 0.6pt 的抖动对照对：两种阈值都是 0 处，
#       说明降这一档没有把「细线位移噪声」放进来。
#   关/调：export PDFDIFF_FAST_FRAC=0.02（或 0.055 退回旧口径）
FAST_TILE_FRAC = float(os.environ.get("PDFDIFF_FAST_FRAC", "0.03"))

# ---- 快速定位的「像素级紧框」参数（6.0，2026-10-26 用户反馈「紫框面积太大」）----
# 病根：`_tiles_to_rects` 给的是**瓦片边界**（24px @120dpi ≈ 14.4pt）。一行字里只有
#   一个词变了，框也至少铺满整格、并向左上各多出半格；相邻瓦片再按 4 邻接并成一个
#   连通域，一处行内改动就外扩成十几个字宽 —— 用户看到的「一大片」是这么来的。
# 做法：瓦片只当**噪声门**（占比 ≥ FAST_TILE_FRAC 才继续查），真正的框改由像素级
#   diff 求**紧包围盒**。召回范围完全不变（还是原来那些热瓦片），只是框变小、变准。
#   `FAST_MERGE_GAP_PX` 只做**横向**膨胀：把同一行里相邻的字连成一句，不跨列合并。
FAST_MIN_PX = int(os.environ.get("PDFDIFF_FAST_MIN_PX", "6"))
FAST_MERGE_GAP_PX = int(os.environ.get("PDFDIFF_FAST_GAP_PX", "10"))
# 一页紧框数超过它 = 整页性差异（重扫/重绘/压缩噪声），按行带合并，免得满屏碎紫框
FAST_MAX_RECTS = int(os.environ.get("PDFDIFF_FAST_MAX_RECTS", "60"))
# 紧框的最小可见边长（pt）。逐像素收紧之后，一个字的改动只有 5~8pt 宽，
# 在 71dpi 的预览里就是四五个像素 —— 框收得太狠反而看不见（2026-10-26 实测）。
# 所以取「实际差异框」与「9pt 方框」的并集：位置还是那个位置，只是不至于看不见。
FAST_MIN_BOX_PT = float(os.environ.get("PDFDIFF_FAST_MIN_BOX", "9"))

# ---- 「大块局部配准」（6.0 第五轮，2026-10-26 用户三对真实案例）----
# 病根：DWG 图纸/标签这类文件，一页里**每个元素各自错位**——图框挪了 7pt、中间的
#   标签位图被重新嵌入成 1.4% 不同缩放的图、右侧变更表又挪了 1pt。整页只估**一个**
#   刚体变换（`_fast_denoise_pair`）时，只能对齐其中一个，其余全成了「每条笔画边缘
#   都变了」，收成一个覆盖 20% 页面的巨框。
# 做法：`_tight_rects` 收到某个连通域又大又密时，就地在这个域的包围盒里再估一次
#   **仿射**（缩放+旋转+平移），配得上就把这块换成「对齐后的残差」，只框真变的地方。
#   实测 DWG p1 标签块：25.8% → 4.3%（缩放 1.0138），巨框散成几个真实改动点。
# ⚠️ 采用门槛很硬：残差必须**降到原来一半以下、且绝对值 <8%**，否则原样不动。
#   这条挡住「两张本来就不同的标签被强行对上」——那种情况 ECC 相关系数低、残差大，
#   一律拒绝，照实报大块。
FAST_LOCAL_ALIGN = os.environ.get("PDFDIFF_FAST_LOCAL_ALIGN", "1") != "0"
# 多大算「大块」（像素，@120dpi；96px ≈ 57.6pt）。比这小的差异（一个词、一个数字）
# 本来就是精确的，绝不拿去配准——配准只会把它们抹掉。
FAST_LOCAL_MIN_PX = int(os.environ.get("PDFDIFF_FAST_LOCAL_PX", "96"))
FAST_LOCAL_SCALE_TOL = float(os.environ.get("PDFDIFF_FAST_LOCAL_SCALE", "0.05"))
FAST_LOCAL_ANG_TOL = float(os.environ.get("PDFDIFF_FAST_LOCAL_DEG", "2.0"))
FAST_LOCAL_SHIFT_PT = float(os.environ.get("PDFDIFF_FAST_LOCAL_SHIFT", "12.0"))
FAST_LOCAL_GAIN = float(os.environ.get("PDFDIFF_FAST_LOCAL_GAIN", "0.5"))
FAST_LOCAL_MAX_RES = float(os.environ.get("PDFDIFF_FAST_LOCAL_RES", "0.08"))
# 混合页（这一页有文字层）里，**不落在图片上**的紫框超过这个占比就丢掉
# （0.02 = 页面的 2%）。文字与图框线交给字符流引擎去标红绿蓝，像素层再报一遍
# 只会多出一圈贴着页边的紫带 —— 实测 DWG 图纸的外框整体挪了 7.7pt 就是这样。
# 小块一律留：那是文字层未必看得见的点级改动。
FAST_MIX_KEEP_COV = float(os.environ.get("PDFDIFF_FAST_MIX_KEEP", "0.02"))

# ---- 「这块内容对面还找不找得到」（6.0 第六轮，2026-10-27 用户）----
# 病根（用户原话「依旧差异很大…依旧是全部标出来了，那不和没标一样」）：
#   上一轮把**一条满页巨框**拆成了**一堆小框**，但拆出来的每个框仍然是噪声。
#   真正的病根是**判定基准错了**：一直拿「同一坐标的像素」跟「同一坐标的像素」比。
#   · 纯转曲样本：新版在中间插入了两行工序，后面整段往下挪了 28px（≈2 行）。
#     同一坐标上新旧是不同的行 → 22 行无一例外全部报出。其实**一个字都没改**。
#   · DWG 样本：表头、图框、变更表各自挪了 18~21px → 12 条框里 9 条是纯位移。
# 做法（document image diff 的经典招法，不需要 OCR、不需要重排假设）：
#   把 A 侧这个框连少量上下文当模板，在 B 侧该框外扩 `FAST_CORR_PX` 的窗口里做
#   TM_CCOEFF_NORMED 找对应物 → 找到就说明「同一块内容只是挪了位置」。
#   **但找到不等于没改**：把 A 平移过去重叠，再做一次**容忍比较**
#   （`_tol_diff`：A 的墨在 B 的 ±2px 邻域里也找得到 → 不算差异，把亚像素抖动和
#   抗锯齿一起吸收掉）。剩下还成块的，才是真改动 —— 框收缩到那一小块。
# 实测（三对真实案例，120dpi）：
#   转曲 22 框 → 6 框：19 个位移框容忍比较后只剩 0~35 个像素、最大连通块 1~8px；
#       真改动最大块 51~442px。中间 8→51 是空的，没有临界值可调。
#   DWG p1 12 框 → 7 框；Picture 14 框 → 10 框（那对本来就是两个差异很大的标签）。
# ⚠️ 判据用**最大连通块**而不是「残留像素占比」：一整行只改一个字时占比会被行宽
#   稀释到 0.017，看着像纯位移，其实那个字是 441px 的实心块。
FAST_CORR = os.environ.get("PDFDIFF_FAST_CORR", "1") != "0"
# 搜索半径（像素，@120dpi；80px ≈ 48pt ≈ 6 行）。整段重排插几行都能覆盖。
FAST_CORR_PX = int(os.environ.get("PDFDIFF_FAST_CORR_PX", "80"))
# 容忍半径（像素）：±1px 内的墨算「同一笔」。
# 实测 2px 会把「表格里 3.5→4.0」这种数字改动一起吃掉（那些数字笔画本来就挨得近，
# 120dpi 下一个数字只有十几个像素的差异），1px 刚好：纯位移的毛刺仍然 0~8px，
# 改了的数字保留 13~45px。3px 更不行，会把细笔画的粗细变化也抹掉。
FAST_CORR_RAD = int(os.environ.get("PDFDIFF_FAST_CORR_RAD", "1"))
# 容忍比较自己的暗阈：**24，不是页面的 `GRAPHIC_DARK`(60)**。
# 容忍比较已经把 1~2px 的错位吸收掉了，阈值再放到 60 就只剩「黑白整块反转」才算差异
# —— 实测转曲样本里那个 441px 的改动字（一行的 1/30）会被当成纯位移丢掉，
# 22 个框最后只剩 1 个。24 是实测最稳的：纯位移残留 0~35px，真改动 51~442px。
# 容忍比较的「墨」阈值：灰度低于它算墨（0~255）。160 能把抗锯齿的浅边也算进笔画，
# 又不会把浅灰底纹当成墨。**这不是页面暗阈 GRAPHIC_DARK(60)** —— 那个是用来判
# 「像素变了没」，这里是判「有没有笔画」，两件事。
FAST_CORR_INK = int(os.environ.get("PDFDIFF_FAST_CORR_INK", "160"))
# 配对得分低于这个值就不再相信它的位移（对面根本没有这块内容 → 整框照报）
FAST_CORR_MIN_SCORE = float(os.environ.get("PDFDIFF_FAST_CORR_SCORE", "0.5"))
# 框太小（最长边 < 这么多像素，@120dpi；64px ≈ 38pt）就**不做**对应物匹配：
# 十几像素的小框里只有几个笔画，模板没有足够上下文，匹配得分必然在 0.4~0.7 之间乱跳，
# 收缩只会把它碎成一堆更小的框。实测 DOC-1001 那对纯图片标签：6 个框被碎成 23 个。
FAST_CORR_MIN_PX = int(os.environ.get("PDFDIFF_FAST_CORR_MIN_PX", "64"))
# 收缩出来的块数超过「原框数 × 这个倍数」就放弃收缩、保留原框 ——
# 说明匹配没抓住整体，碎下去只会更乱。
FAST_CORR_MAX_SPLIT = float(os.environ.get("PDFDIFF_FAST_CORR_SPLIT", "3.0"))
# 框多到这个数就不做了（整页重绘那种，逐框匹配纯属浪费）：直接照原来的路走
FAST_CORR_MAX_BOXES = int(os.environ.get("PDFDIFF_FAST_CORR_MAX", "240"))
# 对齐后仍有这么大比例（0.35 = 35%）的像素对不上 → 这个匹配不可信，别收缩、整框照报
FAST_CORR_MAX_RESID = float(os.environ.get("PDFDIFF_FAST_CORR_RESID", "0.35"))
# 「同一内容挪了位置」的**最低收益**：对齐后平均差必须降到原来的 35% 以下。
# 为什么需要这一条（实测踩到的）：框往往只是整行的**一段**（比如 x 199~498 的半行），
# 而搜索窗有 80px≈6 行高，一段半行很容易在别处找到一个「长得挺像」的片段，
# 得分不低、残差也不高，于是被当成「挪走了」丢掉 —— 转曲样本里
# 「热风枪」→「热缩管加热器」这处真实改动就是这么丢的。
# 真正同源的位移，残差会掉到原来的 15%~30%；认错亲的匹配掉不了这么多。
FAST_CORR_GAIN = float(os.environ.get("PDFDIFF_FAST_CORR_GAIN", "0.35"))
# 对应物判定里「算作一块」的最小像素数。**比页面级的 FAST_MIN_AREA_PX(20) 小**：
# 这里比的是「对齐后还剩多少」，纯位移只剩 1~8px 的亚像素毛刺，而一个数字改动
# （表格里 3.5→4.0）在 120dpi 下也只有十几个像素。20 会把后者一起吃掉。
FAST_CORR_MIN_BLOB = int(os.environ.get("PDFDIFF_FAST_CORR_BLOB", "12"))
# 一个框收缩出超过这么多块就**不收缩**、保留原框：碎裂的结果和「全部标出来」没区别。
# 实测 DOC-1001 那对（6 个框都是整行文字）会被裂成 21 个词；DWG p1 的 12 个框裂成 42 个。
# 3 块以内才是「一行里改了一两处」这种该收缩的情况。
FAST_CORR_MAX_BLOBS = int(os.environ.get("PDFDIFF_FAST_CORR_BLOBS", "3"))
# ---- 「位移」归位（2026-10-27 第七轮）------------------------------------
# 用户：「很多地方它其实是产生位移的，但是也被标记出来了……标记的量产生的还是太多
# 了，这个多就是大部分来自于位移偏差导致的」。紫框里确实有一大批**只是挪了位置**：
# 两次导出/两次排版让图块、表格、编号整片平移，内容一个字没改。它们该归「位移浅蓝」
# （`KIND_MOV`，默认不显示），不是「待判定紫」。
# 判据（实测四对语料的小框）：配对得分 ≥ 下面这个值、且对齐后残差不足一块
# （< `FAST_CORR_MIN_BLOB`）→ 位移。真改动不是这个样子：
#     位移   ncc 0.84~0.98  残差 0~4px   （DWG 22 个小框、转曲序号整体上移 16.8px）
#     真改   ncc 0.66      残差 30px    （「焊接时间 3.5→4.0」）
#     真改   ncc 0.94      残差 21px    （同位置改字，得分再高残差也下不去）
# ⚠️ 只有**高分**才敢判位移：0.5~0.7 那档是「长得有点像」，配上残差也小的，
#    很可能是认错亲（十几像素的小模板尤其容易）。宁可多留一个紫框。
FAST_CORR_MOVED_SCORE = float(os.environ.get("PDFDIFF_FAST_CORR_MOVED", "0.8"))
# 模板最长边小于这个值还是不做匹配（比 FAST_CORR_MIN_PX 松得多）：只有几个笔画的
# 微框上下文不足，NCC 全是 0.4~0.7 的乱跳。20px≈12pt 是实测能稳住的下限。
# 小于它的照旧整框保留（宁可多报，不能猜）。
FAST_CORR_SMALL_PX = int(os.environ.get("PDFDIFF_FAST_CORR_SMALL", "20"))
# 「这儿根本没变」的残差上限（像素）：≤2px 属于抗锯齿/亚像素毛刺。
FAST_CORR_MOVED_RESID = int(os.environ.get("PDFDIFF_FAST_CORR_MOVED_RESID", "2"))
# 判「整块平移走了」的最小位移（pt）。低于它就当**原地**处理：原地有残差 = 内容改了，
# 必须留紫框。实测踩的坑：合成标签「100→120」的数字框残差只有 5px、位移 0.85pt，
# 被 0.837 的高分配到旁边另一个「0」上，差点当成位移吞掉 —— 真改动一个都不能丢。
FAST_CORR_MOVED_MINOFF = float(os.environ.get("PDFDIFF_FAST_CORR_MOVED_OFF", "2.0"))

# ---- 快速模式的「降噪」= 整页亚像素配准 + 预平滑（6.0 第四轮，2026-10-26）----
# 病根（用户实测「两个新旧标签一比，全局都被标出来了」）：新旧标签常常是**两次导出/
#   两次扫描**，整页带 0.3~2pt 的平移抖动。这时的像素差全集中在**每一条笔画边缘**上，
#   不是「内容变了」——实测一对只改了 2 个数字的标签：
#       未配准：|diff|>60 的像素占 6.8%，30% 瓦片点热，最后并成**1 个 85% 页面的巨框**
#       配准后：0.30%，0.1% 瓦片点热，2 个框、共 0.03% 面积，正好圈住那两个数字
#   ⚠️ 所以这里**不是**「把噪声糊掉」：模糊与开运算对细笔画改动伤害极大（实测
#      3×3 开运算把 2 处真改动全削掉，命中 0/2）。能降的是**错位**，不能降的是**内容**。
#   ⚠️ 只有「抖动级平移」才补偿（≤ `FAST_ALIGN_MAX_PT`）：更大的位移是内容被挪动，
#      整页平移会把它抹掉（正是完整模式 `moved` 要报的东西）。
FAST_DENOISE = os.environ.get("PDFDIFF_FAST_DENOISE", "1") != "0"
FAST_ALIGN_MAX_PT = float(os.environ.get("PDFDIFF_FAST_ALIGN_MAX", "3.0"))
# 刚体配准能接受的旋转上限（度）。扫描件的歪斜实测 0.1~0.5°，2° 已很宽松；
# 再大就不是「同一页的抖动」，而是内容被转了 —— 强行转回来会把真差异一起抹掉。
FAST_ALIGN_MAX_DEG = float(os.environ.get("PDFDIFF_FAST_ALIGN_DEG", "2.0"))
FAST_ALIGN_MIN_RESP = float(os.environ.get("PDFDIFF_FAST_ALIGN_RESP", "0.35"))
# 补偿必须**让差异明显变少**才采用（比率 = 补偿后 / 补偿前）。这条是方向估错、
# 页面本身就是两张不同图时的最终保险：不降就不动，行为退回原逻辑。
# ⚠️ 门槛不能定太严（实测 0.7 会误拒）：整块替换这类**真差异面积很大**的页，
#    分母被真差异撑大，比率本来就降不下来（实测 gain=0.81 却被拒 → 退回满屏
#    噪声）。方向搞反时比率是 **1.4 倍**，所以 0.9 既能挡住错向，又容得下大改动。
FAST_ALIGN_GAIN = float(os.environ.get("PDFDIFF_FAST_ALIGN_GAIN", "0.9"))
# 预平滑核（奇数）：压掉亚像素配准后剩下的 1px 级残差与 JPEG 振铃。
#   实测（两对标签、真改动 2 处）：blur=1 → 7~15 个碎框；blur=3 → 正好 2 个框。
FAST_BLUR_PX = int(os.environ.get("PDFDIFF_FAST_BLUR", "3"))
# 连通域最小差异像素数（@120dpi；20px ≈ 3.9pt²）。旧值 6 太松，零星几个抗锯齿点
# 就能立一个框。实测降到 20 仍保得住「一个数字变了」（数字改动的差异像素 60~200）。
FAST_MIN_AREA_PX = int(os.environ.get("PDFDIFF_FAST_MIN_AREA", "20"))
# 框覆盖面积占页面超过它 = **整页性**差异（整页重绘、换了个版式、位移大到配不准）。
# 这时几十条碎紫框不如一条「整页」说得清楚，也让用户一眼知道「这不是几个点变了」。
# 判据用**面积**而不是框数：行带合并之后框数会掉到十几条，但仍然铺满全页。
# 实测分界：只改 2 个字段 0.06% ｜ 整块替换 85% ｜ 一页改了十几处 15~21%（**不该**并）
# ⚠️ 2026-10-26 第五轮从 0.35 提到 0.65：0.35 把「一页里改了十几处」压成了一条整页框
#    （转曲样本 22 条、Picture 14 条全被压掉），用户看到的仍是「全页都变了」。
FAST_FULLPAGE_FRAC = float(os.environ.get("PDFDIFF_FAST_FULLPAGE", "0.65"))

# ---- 紫框的「方向」（2026-10-29 用户要求；2026-11-02 作用收窄）----
# 用户原话：「整体颜色为紫色，但是这个标记起来的边框用深红色和深绿色去表示。
# 这样子能与普通的那个原生识别出来的红色绿色产生一些差异，就表示这种它是
# 转曲或者图片部分所识别出来的。」
# 即：紫框**仍然是紫框**（召回、矩形、数量一个字都不改），只在能看出方向时
# 记下「这处旧版有、新版没了」（del）/「新版多出来的」（add）。
# ⚠️ 2026-11-02 用户要求紫框统一纯紫、去掉彩边，所以这个方向**不再决定颜色**，
#    也不再把位移类分给「位移」勾 —— 它现在唯一的用处是**标注画在哪一侧**
#    （del 只标旧版、add 只标新版；判不出就两侧都标）。判据本身一个字没动，
#    因为「标错侧」和「染错色」是同一个错误的两种表现，宁可不判。
# 判据（对 39 条真实紫框逐条人工核对，只认「独占墨占绝对多数」这一种）：
#   框内像素三分 —— both（两侧都有墨）/ only_a（只有旧版有）/ only_b（只有新版）；
#   only_a ≥ 55% → 删，only_b ≥ 55% → 增，其余（两侧都改过、整体替换、碎到
#   没几个像素）→ 留纯紫，不猜。
#   实测正确：合成 4/4、Picture 13/13、转曲 5/6、DWG 20/20；唯一判错的是
#   「整段重排 + 末尾追加」，新增被吸附成修改 —— 方向性错误 0 条。
# ⚠️ 用**占比**而不是绝对墨量：紫框大小差两个数量级（一个标点 vs 一个图框），
#    绝对量会让小框永远判不出。
FAST_DIR_FRAC = float(os.environ.get("PDFDIFF_FAST_DIR_FRAC", "0.55"))
# 框内总墨量低于它就不判方向（几个像素的抗锯齿噪声说什么都不算数）。
FAST_DIR_MIN_PX = int(os.environ.get("PDFDIFF_FAST_DIR_MIN_PX", "8"))

# ---- 紫框里的「位移」档（2026-10-30 晚用户要求；2026-11-02 只留判据）----
# 用户原话：「有一些看上去是更改，但是它无法识别出来的……但是还有一些是位移，
# 它这个能不能识别出来呢？如果能识别出来，应该把位移标记成蓝色，就是它那个
# 边框改成蓝色的，就与正常的那个文本类的位移是一样的，并且也是默认不显示。」
# 判据：`_corresp_filter` 那条路已经会把「几乎完全等于挪过去」的框挑走给
#   KIND_MOV；漏下来的是「挪了位置、但残差比一块大」的那些 —— 在紫框里表现为
#   三分之后 only_a / only_b 各占一半（既不到 55% 判 del，也不到 55% 判 add）。
#   这里对这类框再做一次**轻量平移核对**：把框内（带 4px 上下文）的墨当模板，
#   在对面 ±FAST_SHIFT_MAX_PX 的窗口里找最佳落点；找到的位置明显偏离原位
#   （≥ FAST_SHIFT_MIN_PX 像素，比整页配准残差大一个量级）且**双向**墨重叠率
#   都够（≥ FAST_SHIFT_MATCH），才认「这块墨只是挪了位置」。
# ⚠️ 2026-11-02：蓝边那档**已经删掉**（用户要求紫框统一纯紫）。所以这个判据
#    现在只产出一个 `dir="moved"` 标记 —— 标注层拿它跟 `""`（判不出）一样对待，
#    两侧都画、都是纯紫。**判据保留不删**：它描述的是"这块墨到底改没改"这个
#    事实，将来若要恢复"位移类单列"（比如清单里分出来）不必重新测一遍；
#    也因为它已经是零成本 —— 只有"既不像删也不像增"的框才会走到这里。
# ⚠️ 不改矩形的数量与位置，也不改召回。
# ⚠️ 阈值三个都可用环境变量覆盖，便于实测分布后再定（先量后改）。
FAST_SHIFT_MIN_PX = int(os.environ.get("PDFDIFF_FAST_SHIFT_MIN", "3"))
FAST_SHIFT_MATCH = float(os.environ.get("PDFDIFF_FAST_SHIFT_MATCH", "0.72"))
FAST_SHIFT_MAX_PX = int(os.environ.get("PDFDIFF_FAST_SHIFT_MAX", "20"))
#: 模板太大就不搜（那是整页差异，不是「一块图挪了位置」；搜起来也慢）。
FAST_SHIFT_MAX_AREA = int(os.environ.get("PDFDIFF_FAST_SHIFT_AREA", str(220 * 220)))

# ---- 「整页图形重绘」防护（2026-10-13 用户实测 DWG-2000087 标签对）----
# 病根：标签类 PDF 一页里同时有「嵌入位图图片（标签图面）+ 四周转曲文字」，
#   改版（rev17→rev18）把整页图面**整体重绘**——图片 md5 变了、转曲文字全部重排。
#   瓦片对拍的 hot 瓦片因此连成**覆盖大半页的一个连通域**（实测 25%~84% 页面面积），
#   `_decide_region` 对整页 OCR 必然判「新增」，报出一条谁都没法核对的巨框。
#   ⚠️ 平台差异（用户报「Mac 有、Windows 没有」）正是落在这里：Mac 的 Vision OCR
#   认得图片里的标签文字（「示例医疗/ProductA/10x20…」）→ 判新增；Windows 的
#   tesseract 认成乱码碎片 → `_ocr_junk` 拦截 → 不报。根因不是 OCR，是「整页重绘」
#   被当成了「局部转曲区增删」。
# 判据（结构特征，不是魔法数）：一个 hot 连通域覆盖页面面积超过它 = 整页重绘。
#   实测分界：整页重绘 25.1%/84.0%  vs  局部转曲区 6.4%、DCR 表格 3.9%、IFU 2.8%
#   —— 15% 在两者之间留足余量。
# 行为：跳过该连通域（不报巨框）并**记账**（LAST_FULLPAGE_SKIPS），不静默 ——
#   该页的文字层差异仍由字符流引擎正常报出（标签的 DCR 表格改动一点不丢）。
GRAPHIC_FULLPAGE_FRAC = float(os.environ.get("PDFDIFF_FULLPAGE_FRAC", "0.15"))
GRAPHIC_RECHECK_PAD_X = 60.0  # v48 文字层错位补查：判定矩形横向各加宽 (pt)，
                              # 让 OCR 读到整行（残词判定必证据不足）
GRAPHIC_PAD = _CFG.graphic_pad          # 报出来的矩形四周留一点
GRAPHIC_BLANK_RATIO = 0.25  # 一块区域两侧的「墨水比」低于它 = 一边基本是空白（整块增/删）
GRAPHIC_SOLID_MIN = 800     # 「纯图形/色块」增删的墨水量下限（@ GRAPHIC_DPI 下的墨点数）：
                            # 有墨侧墨水量 ≥ 它、OCR 又认不出字，才判「图形增删」——
                            # 门槛够高才挡得住扫描噪点；一块实心黑块动辄几千墨点。
GRAPHIC_SOLID_TAG = "[图形]"  # 纯图形/色块增删的占位文本（OCR 认不出字时用），
                            # 下游 `_graphic_rescue_core` 原样透传，UI 显示成「图形区域」。
GRAPHIC_LINE_GAP = 0.1      # 块内相邻像素行之间空白超过它 (pt) 才算「换行」
                            # ⚠️ 这份 PDF 转曲正文的行距紧到 0.8pt，阈值取 1.4pt
                            #    会把两行粘成一行（详见 _split_lines docstring）
GRAPHIC_MIN_LINE = 3.0      # 细于它的「行」不算一行（是上下行留下的残影）
GRAPHIC_BLEED = 5.0         # 切行前把块矩形上下各外扩这么多 (pt)，见 _refine_region
GRAPHIC_ANCHOR_MIN = 1.0    # 外扩窗口切出的「行」与块本身至少重叠这么多 (pt) 才算数
REFINE_SEAM_MAX = 1.5       # 相邻保留行之间最多把缝补掉这么多 (pt)，见 _refine_region
GRAPHIC_MOD_FALLBACK_MAX_H = 30.0   # mod 行级复核全灭时**整块兜底**的高度上限 (pt)：
                                    # 块级 mod 但行级（宽门槛）一条都凑不出，
                                    # 且块高 ≥ 3 行 → 是聚合噪声不是真改，放弃
                                    # （2026-10-12 失败1：32/46/219pt 巨型黄框的出生点）

# ---- 「块级判否 → 逐行再判一次」（2026-09-30）----
# ⚠️ 这一节补的是**粒度**问题，不是判据问题（判据动了会牵一片）。
# 起因：DOC-1007 左栏 2.0 供货方式那块，新版比旧版多一行「一个再折叠工具」，
# 它下面 `防止重压…` / `须贮存…` 两行只是被顶下去 —— 瓦片对拍把这几行连成
# **同一个**候选块，块级 `_decide_region` 一算覆盖率 0.93（旧版那两行 100% 在新版里）
# → 判「只是重渲染」→ 整块丢掉，真新增的那一行**跟着一起没了**。
# 细切到行之后，`一个再折叠工具` 那行对面是**空白**，走「一边基本空白」分支，一判一个准。
GRAPHIC_LINE_RESCAN = os.environ.get("PDFDIFF_LOCATE_LINE_RESCAN", "1") != "0"
RESCAN_ROW_MIN_CHARS = 2    # 细切行至少认出的字数（少于此视为 OCR 噪声，不报）

# ---- 纯扫描件的候选生成（2026-09-30，DOC-1002 俄文标签实测）----
# 这类文件整页就是**一张位图**（无文字层、无矢量），两次扫描亚像素错位让全页
# 都有差异噪声 —— 瓦片对拍两头不讨好：噪声把大量瓦片点亮（17%），而单个字符
# 的真差异（页脚 Rev02→Rev03，行差异峰值才 17px）又弱到**点不热任何瓦片**，
# 连候选都进不来。改用**行级差异画像**：逐像素行数差异像素，低门槛聚成
# 候选窗口，多给候选没关系 —— 后面有 OCR 判据（`_decide_region`）把关。
GRAPHIC_SCAN_BANDS = os.environ.get("PDFDIFF_SCAN_BANDS", "1") != "0"
SCAN_ROW_PX = 6             # 一行里 |差|>GRAPHIC_DARK 的像素 ≥ 它才算「差异行」
SCAN_MIN_ROWS = 2           # 连续差异行 ≥ 它才聚窗（去孤立噪点行）
SCAN_GAP_ROWS = 1           # 聚窗时允许的中间断档行数；⚠️ 不能大：断档 2 行就把
                            # 日期行和下一行并进一窗，种子会落到隔壁行的粗体噪声上
SCAN_PAD_PT = 3.0           # 窗口上下各外扩这么多 (pt)，别把字的上下沿切掉
SCAN_INK_DARK = 200         # 「墨」的灰度上限（与 _ink_rows 同口径）
SCAN_JOIN_GAP_PT = 3.0      # 簇内列空隙 / 从簇边缘沿墨迹扩展时，空隙 ≤ 它(pt) 就继续
SCAN_VLINE_FRAC = 0.85      # 一列在窗口±60pt 范围里墨迹占比 ≥ 它 = 竖直线（边框/表格框）
SCAN_VLINE_CONTEXT_PT = 60.0  # 竖直线判定的上下文范围（pt）
SCAN_ALIGN_BAND_H = 24      # 垂直配准分带高度 (px)
SCAN_ALIGN_DY = 22          # 每带搜索 ±N 行的纵向错位；⚠️ 底部真值可能到 16+，
                            #   ±14 时被硬夹成 0 把拟合线拉平（DOC-1003 实测）
SCAN_ALIGN_STEP = 4         # 配准时列抽样步长（省时间）
SCAN_ALIGN_TRUST = 0.85     # 一带可信的判据：best SAD < 它 × dy=0 的 SAD
                            # （内容相同的带，对齐后 SAD 显著低于错位状态；
                            #  两个产品大面积不同的带没有对齐信号，best≈dy0，不可信）

# ---- 扫描件「整页 OCR 行对拍」管线（DOC-1003 实测，见 _scan_page_line_regions）----
GRAPHIC_SCAN_LINES = os.environ.get("PDFDIFF_SCAN_LINES", "1") != "0"
SCAN_LINES_DPI = 200        # 整页行级 OCR 的渲染分辨率（400 太慢、120 小字认不全）
SCAN_MOVE_COVER = 0.90      # 对面有**单独一行**与它 ratio ≥ 它 = 挪位/重排，撤
SCAN_MOVE_SAME = 0.995      # ratio ≥ 它 = 同一行（只是 OCR 抖动），撤
SCAN_INPLACE_DY = 4.0       # 「原地副本」判定：对面同文本行中心 y 差 ≤ 它（pt）
                            # = 原地就有一份 → 残余副本是重复计数，不许配成位移
                            # （2026-10-12 22vs23：上下两块相似内容被跨配出 80pt 假位移）
# ⚠️ 2026-10-01 改口径：`SCAN_MOVE_COVER` 原先是「**整页拼串**的子序列覆盖率」。
#    一页几千字符，任何短串都能按序凑齐 → coverage 恒 ≈1.0：DOC-1003 实测
#    `5F(0.072"/1.83mm)`→`6F(0.084"/2.13mm)` 被判 1.0 当挪位撤掉，
#    UDI 的 `48425 0`→`48429 8` 被判 0.944 撤掉 —— **整份的规格数字改动全军覆没**。
#    现在改成「对面**逐行**最佳 ratio」，`SCAN_MOVE_SAME` 挡「同一行抖动」，
#    再加 `SCAN_MOVE_RULES` ③ 那条互改闸门挡「高相似但真改了数字」。
SCAN_MOVE_RULES = [
    {"name": "完全相同", "cond": "对面有归一化相等的行", "act": "抵消/撤",
     "why": "两版都印了这句，位置不同而已"},
    {"name": "同一行的 OCR 抖动", "cond": f"逐行 best ratio ≥ {SCAN_MOVE_SAME}", "act": "撤",
     "why": "同一句话两版认出的字略有出入，不是改动"},
    {"name": "⚠️ 行内小改闸门", "cond": "best ratio ≥ mod 门槛 且 `_mod_worthy`（token 等长 + 数字换数字）",
     "act": "**不撤**（留给 mod 配对）",
     "why": "UDI `48425 0→48429 8` ratio 0.94 看着像同一行，实际换了数字 —— 这是真改动的典型长相"},
    {"name": "挪位 / 重排", "cond": f"best ratio ≥ {SCAN_MOVE_COVER}", "act": "撤",
     "why": "整段被推走或重排版（多语言段、表头合并）"},
    {"name": "短行整行命中", "cond": "长度 < OCR_MIN_LEN 且整行出现在对面", "act": "撤",
     "why": "`REF` / `LOT` / `R` 这类碎片在对面满页都是，逐行 ratio 反而判不出，靠整行命中挡"},
    {"name": "其余", "cond": "ratio 更低", "act": "不撤",
     "why": "对面没有相似行 = 新增/删除候选"},
]
SCAN_PAIR_DY = 20.0      # 一删一增配对：两行 y 中心相距 ≤ 它(pt) 才配（同版式标签的
                         # 行位置漂移实测 < 10pt；不卡位置会把 `150mm` 配到隔了 33pt
                         # 的 `6.5/7.0 mm` 上 —— 2026-10-01 实测，贪心只看 ratio 必错配）
SCAN_PAIR_DX = 30.0      # 同上，x 中心也要够近（**同列**）：`1400`(kPa 列) 曾配到
                         # 隔壁直径列的 `7.10` 上；卡了 x 才各归各列
# ⚠️ 数字主键优先（2026-10-11 DOC-1003 用户拍板）：表格行按**行首数字**对齐才是
#    用户语义 —— 压力表 12 那行新版只是 `12`→`12 (RBP)`，14 整行删掉；
#    按 ratio 贪心却让 `14(RBP)`↔`12 (RBP)`(0.77) 抢走 `12`↔`12 (RBP)`(0.44)，
#    用户看到的是「14 改成 12」而不是「14 行没了、12 加了 RBP」。
#    两候选行首数字相同 → 配对分数抬到 `SCAN_NUM_PAIR_BOOST`（保证压过
#    「数字不同但字面相似」的候选）；位置闸门（DY/DX/OV）照卡不变。
SCAN_NUM_PAIR_BOOST = 0.80
SCAN_PAIR_OV = 0.30      # 但只看中心距会把「同一行变长了」的错开（`Balloon Diameter`
                         # →`Conical Balloon Diameter`：新增前缀让中心左移 68pt）。
                         # 所以「横向重叠 ≥ 它 × 较窄那行」也算同列。
SCAN_LINE_MOD_RATIO = 0.55  # 一删一增行归一化 ratio ≥ 它 = 行内修改
SCAN_LINE_MIN_ROWS = 3      # 两版整页各至少认出这么多行，行对拍才算可用

# ---- 两页位图尺寸不一致怎么办（2026-09-30 图片输入轮实测出的静默失效）----
# 病根：`graphic_regions` 原来一句 `if 位图尺寸不同: continue` 就把整页丢掉。
#   图片输入时这几乎是必踩的：同一份文档一个存成 1430x1543、一个存成 1430x1544
#   （PDF 页高 555.60pt vs PNG 折算 555.84pt，200dpi 下差 0.3px 就进位差一行），
#   于是**整页一处都不查、界面却报「0 处差异」** —— 用户无从判断是「真没改」
#   还是「根本没看」。这是最贵的一类假象（用户阶段 5 的原话：没报错 ≠ 正确）。
# 规矩（表驱动，见 `SIZE_FIT_RULES`）：先拉伸对齐，实在对不上才跳过**并记账**。
SCAN_SIZE_TOL = 0.03        # 两页宽高比差异 ≤ 它（3%）才允许拉伸对齐
SIZE_FIT_RULES = [
    {"name": "尺寸相同", "cond": "w/h 完全一致", "act": "直接用",
     "why": "正常 PDF 两版同页尺寸，绝大多数走这条"},
    {"name": "比例一致、只差渲染取整", "cond": f"|sx-sy|/max ≤ {SCAN_SIZE_TOL}",
     "act": "把新版位图拉伸到旧版尺寸再比对",
     "why": "图片输入 / 不同扫描仪的 1~2px 差异，拉伸后错不了一个字"},
    {"name": "比例真的不同", "cond": "超出容差", "act": "跳过该页**并记账**（LAST_SIZE_SKIPS）",
     "why": "硬拉会把内容压变形，宁可不判也不能给出错坐标；但不能静默，要让用户看见"},
]

# 本次 `graphic_regions()` 因尺寸对不上而跳过的页（每次调用开头清空）。
# ⚠️ 跨进程确定性：只在 `graphic_regions` 里写、调用方读完就清，不进任何缓存 key。
LAST_SIZE_SKIPS: List[Dict[str, Any]] = []

# 本次 `graphic_regions()` 因「整页图形重绘」而拦截的页/块（每次调用开头清空）。
# 与 LAST_SIZE_SKIPS 同口径：只在 graphic_regions 里写、调用方读完就清。
LAST_FULLPAGE_SKIPS: List[Dict[str, Any]] = []

# 本次 `graphic_regions()` 快速模式下**做了整页配准**的页（每次调用开头清空）。
# 记 `{page, dx, dy, gain}`：dx/dy 是补偿掉的抖动位移（pt），gain 是补偿后/补偿前
# 的差异像素比（越小越有效）。只用于日志与自检 —— 界面不显示，避免多一行文字。
LAST_FAST_ALIGN: List[Dict[str, Any]] = []

# 本次快速模式里被判成**整页性差异**的页（框已合并成一条整页框）。
# `cover` 是合并前的覆盖比，便于自检判断阈值是否合适。
LAST_FAST_FULLPAGE: List[Dict[str, Any]] = []

# 本次快速模式「对应物过滤」的账（每次调用开头清空）：
#   {page, boxes, dropped, shrunk, kept}
# dropped = 内容是同一块、只是挪了位置（不报）；shrunk = 匹配上了但还有真改动
# （框已收缩到那一小块）；kept = 对面根本找不到这块内容（整框照报）。
LAST_FAST_CORR: List[Dict[str, Any]] = []

# 本次 `_extract_moved_rows()` 判为「移动但比值落在 (move_ratio_max, low_conf_ratio]
# 低置信带」的块（每次调用开头清空）。这些块**仍标蓝**（不做灰框），只是让报告层
# 能单列「低置信度清单」供人工复核（2026-10-15 用户拍板）。
LAST_LOW_CONF: List[Dict[str, Any]] = []

# ---- OCR 补完（可选，没装 tesseract 就自动跳过）----
# 两个用途，都是「文字层缺行」这个病根的解药：
#   ① **撤销误标**：字符流引擎报「删除/新增」，其实是新版那块被转曲、丢了文字层
#      → OCR 出来一比，内容还在 → 撤掉这条误标（DOC-1007 的 9 处免责声明就是这样）。
#   ② **补上漏标**：转曲区域真新增/删掉的内容，文字层看不见，OCR 能看见。
OCR_LANG = os.environ.get("PDFDIFF_OCR_LANG", "chi_sim+rus+eng")
#                           ⚠️ tesseract 多语言联合是「+」号连；rus 是俄文标签 DOC-1002
#                           加的（整页转曲、vision 不可用时兜底用），中文场景影响实测见回归。
# OCR 引擎优先级表：**按顺序尝试，第一个认出字的胜出**（见 `_ocr_image`）。
# ⚠️ 2026-10-14（4.0 统一 OCR）：默认改为 `rapidocr` 优先 —— RapidOCR 是
# Paddle 模型的 ONNX 版，跨平台、Python 3.13 兼容、中文识别质量远胜 Tesseract，
# 且 Mac/Windows 两端行为一致（取代旧的「Mac 用 Vision、Windows 用 Tesseract」）。
# `PDFDIFF_OCR_ENGINE=tesseract` 可强制退回单引擎。
OCR_ENGINES = tuple(e.strip() for e in
                    (os.environ.get("PDFDIFF_OCR_ENGINE") or "rapidocr,tesseract").split(",")
                    if e.strip())
OCR_DPI = 400              # OCR 渲染分辨率（6pt 小字要 400dpi 才认得出）
OCR_MIN_COVER = 0.85       # OCR 文本被对面文档覆盖到多少 = 「这段内容还在对面」
OCR_MIN_COVER_LINE = 0.75  # 同上，但用在**逐行复核**上（详见 _refine_region）
                           # ⚠️ 行级必须比块级松：转曲小字 OCR 常认错一两个字，
                           #    实测 `- 检查球囊导管是否完整；`（对面也有）被认成
                           #    `含查球宫导管是否完整;` → 覆盖率刚好 0.80，卡在 0.85
                           #    下面 → 把没变的这行也标绿了。而**真新增**的行对面
                           #    基本是完全不同的内容，实测覆盖率只有 0.00~0.11，
                           #    所以放宽到 0.75 收不进任何一条真新增。
OCR_MIN_LEN = 10           # 短于这么多字的片段不做 OCR 核对
OCR_MIN_COVER_LONG = 0.5   # `_longest_cover` 门槛：**最长连续**匹配至少占一半才算「同一句」
                           # ⚠️ 2026-10-27：`_ocr_rescue` 原来只看累加覆盖率 `_coverage`，
                           #    「散字凑得齐」就撤销真改动（实测 DOC-1005 地址行新增
                           #    「1栋101室，…2栋101室、」在旧版地址行 OCR 上累加 0.9+，
                           #    整条凭空消失）。`_longest_cover` 的注释早写明了这个坑，
                           #    这里把它接到救援判定上：整句还在 = 最长块≈1.0；
                           #    只共用几个词的两句话 = 最长块≤0.3。取 0.5 当分界。
OCR_NEAR_PAD = 1.2         # 「对面就近一块」上下各扩这么多倍块高（见 _ocr_near）
# 展示用的正文要**另取一次 OCR，横向多留这么多**（2026-09-30）。
# 起因：判定用的那份裁剪贴着字切（上下只留 `OCR_CROP_PAD`），上下文不够，
# Vision 也会认错 —— 同一块转曲区，两种裁法认出来的字：
#   贴边裁（判定用）：`知果需要…生理盐水纱布擦拭**求**囊导管…`、`分）。`→`7）。`、
#                     `再折叠工具`→`斤叠工具`
#   横向留 10pt：    `如果需要…生理盐水的纱布擦拭球囊导管…`，实测**六行全对**
# 用户原话「你简直有点惜字如金」的另一半其实是**认错字**。
#
# ⚠️ 留白只能加在**横向**：实测 y 方向一放宽（≥3.5pt）就会把上一行的字
#    （`- 检查球囊导管是否完整；` 或只剩个 `-`）一起卷进来，反而更脏；
#    横向放宽 10pt 既让 Vision 有足够上下文，又离对侧栏 >100pt 不会跨栏。
OCR_TEXT_PAD_X = 10.0      # 展示用文本的横向留白（判定用的裁剪**不动**，见 _refine_region）

# ---- 行级 OCR 的双通道配置（2026-10-19，5.0 前置改造）----
# 行级对拍管线原先只走 macOS Vision（`_ocr_image_lines` → `_vision_exe`），
# Windows 包没有 Vision 二进制 → 返回空 → **整条行级路线是死路**，只能退回像素管线。
# 但 RapidOCR 的 `RapidOCROutput` 本来就带 `boxes`/`scores`，只是被 `_rapidocr_ocr`
# 丢掉了（只取 `txts`）。接上这两样 = 不加模型、不加依赖，Windows 也能做行级对拍。
RAPID_LINES = os.environ.get("PDFDIFF_RAPID_LINES", "1") != "0"
# 行级置信度门槛：PP-OCRv6 在清晰文本上普遍 >0.9，而竖排/转曲小字的**误读碎片**
# （实测 `NEX`/`X`/`А` 之类）常落在 0.2~0.3。不过滤就会把这些碎片喂进配对逻辑，
# 制造假新增/假删除 —— 那正是 4.0 阶段反复出现的「没删也标了删」的一类来源。
# 阈值取 0.35（保守）：宁可真低置信行整条丢，也不让它污染增删判定。
OCR_LINE_MIN_SCORE = float(os.environ.get("PDFDIFF_OCR_MIN_SCORE", "0.35"))
# ⚠️ 行级结果的**产出方式**一变就要 +1（缓存 sig 里带它）。
# 不改 = 旧磁盘缓存被钉死，改了代码却看不到效果 —— DOC-M2001 踩过一次
# （`GRAPHIC_RULE_VER` 没 bump，全队以为修复无效）。
_OCR_LINES_VER = 1

OCR_CROP_PAD = 1.2         # OCR 取字时上下留一点边距 —— 贴着字切 tesseract 认得一塌糊涂
#   ⚠️ 10 字这条是实测定的：地址改动「1栋101室」（6 字）拿去核对时，
#   老版本里正好有「1栋101室…2栋101室」，零散重合能到 0.83 —— 真改动会被误撤。
#   短片段本来就不足以证明「对面还有这段」，不做核对就是宁可保留。


def _text_tiles(page, scale: float, tile: int) -> set:
    """这一页**有文字层**覆盖到的瓦片集合。

    有文字的地方交给字符流引擎去标（它能逐字定位，比这里精确得多）；
    本节只负责补「两侧都读不到文字」的空档，两边各管各的、不重叠。
    """
    hit = set()
    try:
        words = page.get_text("words")        # [x0,y0,x1,y1,word,...]
    except Exception:
        return hit
    for w in words:
        x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
        for r in range(int(y0 * scale) // tile, int(y1 * scale) // tile + 1):
            for c in range(int(x0 * scale) // tile, int(x1 * scale) // tile + 1):
                hit.add((r, c))
    return hit


# 位移块要盖住瓦片多少面积，才允许把这块瓦片整体拉进 skip（见 `_rect_tiles`）。
# 0.25 是「擦边不算、真覆盖才算」的分界：moved 行高通常 6~9pt，瓦片行 14.4pt
# （24px@120dpi），本行覆盖 ≈0.5 会跳过、邻行只擦到 ≤0.06 不跳。
MOVE_SKIP_FRAC = 0.25


def _rect_tiles(rects, scale: float, tile: int, min_frac: float = 0.0) -> set:
    """把一批页面坐标矩形（pt）覆盖到的瓦片集合（供位移补偿把 moved 区加入 skip）。

    `min_frac`（2026-11-02 加）：矩形要盖住某块瓦片**这么多面积**才把它拉进 skip，
    默认 0 = 老口径（碰一点就算），保持既有调用点的行为不变。

    ⚠️ 为什么必须有这个门槛：老的纯网格相交口径下，位移块只要碰到瓦片
       0.8pt，**整块 14.4pt 高的瓦片行**就一起跳过。11vs12 左栏就是被这个
       切掉了：moved 行 y 180.0~188.0，瓦片行 187.2~201.6 与它重叠 0.8pt
       （占 5.6%），整行被跳过，而新插的「一个再折叠工具」正好 y 190.2~195.6
       落在这一行里 —— 像素层连看都没看，界面只剩补查路径给的一个整段大框。
    """
    hit = set()
    for r in rects:
        x0, y0, x1, y1 = r
        for rr in range(int(y0 * scale) // tile, int(y1 * scale) // tile + 1):
            for cc in range(int(x0 * scale) // tile, int(x1 * scale) // tile + 1):
                if min_frac > 0:
                    ty0 = rr * tile / scale; ty1 = (rr + 1) * tile / scale
                    tx0 = cc * tile / scale; tx1 = (cc + 1) * tile / scale
                    ix = max(0.0, min(x1, tx1) - max(x0, tx0))
                    iy = max(0.0, min(y1, ty1) - max(y0, ty0))
                    if ix * iy < min_frac * (tx1 - tx0) * (ty1 - ty0):
                        continue          # 只是擦到边，不配把这整块瓦片牺牲掉
                hit.add((rr, cc))
    return hit


def _tol_diff(a, b, ink: int = FAST_CORR_INK, rad: int = FAST_CORR_RAD):
    """**容忍比较**：只有「这边的墨在对面 ±rad 邻域里找不到」才算差异。

    逐像素相减 `|a-b|>dark` 的问题是：只要两边差 0.4 个像素（亚像素错位、
    抗锯齿不同），**每一条笔画的两侧边缘**都会超过阈值。128 线/英寸下一个
    1pt 的字，笔画宽约 2px —— 错位 0.5px 就能让整页密集地「变红」。

    做法：各自取墨掩码（`< ink`），**膨胀 rad 像素**后求异或方向：
        A 有墨、B 的 ±rad 邻域里没墨 → A 这边多了东西
        B 有墨、A 的 ±rad 邻域里没墨 → B 这边多了东西
    1~2px 的错位、抗锯齿差全被邻域吸收；真正的增删改因为邻域里根本没有对方，
    两个方向都会照报。

    ⚠️ 2026-10-27 修：第一版是拿灰度做「比邻域最暗还暗 / 比邻域最亮还亮」，
       其中「B 有墨而 A 没有」那一半**永不触发** —— 文字笔画周围必然挨着白底，
       邻域最亮值恒为 255，「A 比它更亮」不可能成立。结果是**只认新增、不认删除**：
       旧版少一段、新版多一段这种改动会被当成「纯位移」丢掉（合成用例第 6 行
       就是这么漏的）。改成墨掩码对称比对后两个方向都成立。
    """
    import numpy as _np
    import cv2
    k = _np.ones((2 * rad + 1, 2 * rad + 1), _np.uint8)
    ia = (a < ink).astype(_np.uint8)
    ib = (b < ink).astype(_np.uint8)
    da = cv2.dilate(ia, k)
    db = cv2.dilate(ib, k)
    return ((ia > 0) & (db == 0)) | ((ib > 0) & (da == 0))


def _merge_blobs_px(blobs, gap: int):
    """把同一行、横向挨着的残差块并成一个框（像素坐标）。

    收缩是按连通块来的，一个词的几个字会各出一个框（实测「热风枪」三个字
    出了 3 个 9×9pt 小框）。**一处改动就该一个框**：横向间距 < gap（默认约一个字）
    且竖直重叠过半的算同一处。间距再大的是行里两处独立改动，不该并。
    """
    cur = [tuple(map(int, b)) for b in blobs]
    merged = True
    while merged:
        merged = False
        out: List[Tuple[int, int, int, int]] = []
        used = [False] * len(cur)
        for i in range(len(cur)):
            if used[i]:
                continue
            used[i] = True
            x0, y0, x1, y1 = cur[i]
            for j in range(i + 1, len(cur)):
                if used[j]:
                    continue
                a0, b0, a1, b1 = cur[j]
                if (min(y1, b1) - max(y0, b0)
                        < 0.5 * min(y1 - y0, b1 - b0)):
                    continue                      # 不是同一行
                if a0 - x1 > gap or x0 - a1 > gap:
                    continue                      # 隔得太开 = 另一处改动
                used[j] = True
                x0, y0 = min(x0, a0), min(y0, b0)
                x1, y1 = max(x1, a1), max(y1, b1)
                merged = True
            out.append((x0, y0, x1, y1))
        cur = out
    return cur


def _unexplained_ink(B, explained, ra, scale: float,
                     dark: int = FAST_CORR_INK) -> int:
    """`ra` 这块地方在 B 侧有多少墨迹**没被任何位移目标/保留框解释**（返回最大连通块像素数）。

    只用在「位移块前沿」判定里，见 `_corresp_filter` 末尾的调用点。
    ⚠️ 墨迹阈值用 `FAST_CORR_INK`(160) 而不是 `GRAPHIC_DARK`(60)：转曲样本的字
    是灰度（实测最暗 81），60 的门槛下整块都算「没墨」，这道检查等于没开。
    """
    try:
        import numpy as _np
        import cv2
    except Exception:
        return 0
    H, W = B.shape
    tx0 = max(0, int(ra[0] * scale)); ty0 = max(0, int(ra[1] * scale))
    tx1 = min(W, int(ra[2] * scale)); ty1 = min(H, int(ra[3] * scale))
    if tx1 - tx0 < 4 or ty1 - ty0 < 4:
        return 0
    ink = (B[ty0:ty1, tx0:tx1] < dark) & (~explained[ty0:ty1, tx0:tx1])
    if not ink.any():
        return 0
    kh = _np.ones((3, 3), _np.uint8)
    n, lab, st, _ct = cv2.connectedComponentsWithStats(
        cv2.dilate(ink.astype(_np.uint8), kh), connectivity=8, ltype=cv2.CV_32S)
    best = 0
    for ci in range(1, n):
        bx, by, bw, bh = (int(v) for v in st[ci][:4])
        cnt = int((ink[by:by + bh, bx:bx + bw] & (lab[by:by + bh, bx:bx + bw] == ci)).sum())
        if cnt > best:
            best = cnt
    return best


def _front_ink_from_a(A, B, ra, rb, scale: float,
                      dark: int = FAST_CORR_INK) -> int:
    """前沿那点墨，是不是 A 的 `旧位置-位移` 挪过来的（返回最大残差块像素数）。

    配合 `_unexplained_ink` 用：前沿有墨只能说明「这儿有东西」，还得看这东西
    有没有来处。刚体平移时前沿的墨正是从 A 的 `a-d` 挪进来的（整块下移时上方
    那一行被带了进来），比对一下就能区分「挪进来的」和「凭空多出来的」。

    返回 0 = 就是 A 挪过来的（不报）；返回一大块 = 来历不明（报）。
    """
    try:
        import numpy as _np
        import cv2
    except Exception:
        return 0
    H, W = B.shape
    tx0 = max(0, int(ra[0] * scale)); ty0 = max(0, int(ra[1] * scale))
    tx1 = min(W, int(ra[2] * scale)); ty1 = min(H, int(ra[3] * scale))
    if tx1 - tx0 < 4 or ty1 - ty0 < 4:
        return 0
    dx = int(round((rb[0] - ra[0]) * scale))
    dy = int(round((rb[1] - ra[1]) * scale))
    sx0, sx1 = tx0 - dx, tx1 - dx
    sy0, sy1 = ty0 - dy, ty1 - dy
    if sx0 < 0 or sy0 < 0 or sx1 > W or sy1 > H:
        return FAST_CORR_MIN_BLOB * 100        # 来处出界：当成来历不明，宁可报
    t = A[sy0:sy1, sx0:sx1]
    b = B[ty0:ty1, tx0:tx1]
    if t.shape != b.shape or t.size == 0:
        return 0
    mask = _tol_diff(t, b, FAST_CORR_INK, FAST_CORR_RAD)
    if not mask.any():
        return 0
    kh = _np.ones((3, 3), _np.uint8)
    n, lab, st, _ct = cv2.connectedComponentsWithStats(
        cv2.dilate(mask.astype(_np.uint8), kh), connectivity=8, ltype=cv2.CV_32S)
    best = 0
    for ci in range(1, n):
        bx, by, bw, bh = (int(v) for v in st[ci][:4])
        cnt = int((mask[by:by + bh, bx:bx + bw] & (lab[by:by + bh, bx:bx + bw] == ci)).sum())
        if cnt > best:
            best = cnt
    return best


def _corresp_filter(A, B, rects, scale: float,
                    dark: int = GRAPHIC_DARK
                    ) -> Tuple[List[Tuple[float, float, float, float]],
                               List[Tuple[Tuple[float, float, float, float],
                                          Tuple[float, float, float, float]]]]:
    """把「只是挪了位置」的紫框挑出来归「位移浅蓝」，真改动只留那一小块。

    返回 `(内容框, 位移对)`：
      · `内容框`  → 值得人工看的（真改动），走 `KIND_PENDING` 紫；
      · `位移对`  → `[(旧侧框, 新侧对应框)]`，内容一字未改、只是挪了位置，
                    走 `KIND_MOV` 浅蓝（**默认不显示**）。用户原话：
                    「很多地方它其实是产生位移的，但是也被标记出来了……标记的
                    量产生的还是太多了，这个多就是大部分来自于位移偏差导致的」。

    ⚠️ 这一层解决的是「全部标出来了」，不是「框太大」
    -------------------------------------------------
    6.0 前几轮一直在调「框多大」：瓦片框 → 紧框 → 整页合并判据。框是越收越小，
    但**该不该报**这个最根本的判断一直没变过——都是「同一坐标的像素变了没」。
    于是整段重排（内容一字未改、只是整体往下挪了两行）会**每一行都报**；
    图纸里表头/图框各自挪 18pt 也一样。用户原话：「依旧是全部标出来了，
    那不和没标一样」——他说得对，一堆小框和一条大框，信息量都是零。

    做法
    ----
    1. 拿 A 侧这个框 + 6px 上下文当模板，在 B 侧外扩 `FAST_CORR_PX` 的窗口里找
       对应物（`TM_CCOEFF_NORMED`）。得分 < `FAST_CORR_MIN_SCORE` → 对面没有
       这块内容 → **整框照报**（真新增/删除/整行改写）。
    2. 找到就把 A 平移到 B 的匹配位置上重叠，再做 `_tol_diff` 容忍比较。
    3. 对齐后**几乎没残差**（不足 `FAST_CORR_MIN_BLOB` 一块）且配对得分
       ≥ `FAST_CORR_MOVED_SCORE` → **纯位移**，归「位移浅蓝」（第七轮，以前这里是
       直接丢掉/整框保留，两个都不对：丢掉就没证据，整框保留就是用户说的误标）。
    4. 有残差 → 真改动：残差块 ≤ `FAST_CORR_MAX_BLOBS` 且占比 ≤
       `FAST_CORR_MAX_RESID` → 框收缩到这些块；否则整框照报。

    ⚠️ 判据是「最大连通块」不是「残留占比」。一整行里只改一个字时，占比被行宽
    稀释到 0.017（看着像纯位移），但那个字是 441px 的实心块。实测纯位移的
    最大块是 0~8px，真改动是 51~442px，中间是空的。
    ⚠️ 框太小（最长边 < `FAST_CORR_SMALL_PX`）不做匹配：几个笔画的模板上下文不足，
    得分必然乱跳。以前这个门槛是 `FAST_CORR_MIN_PX=64`，**太狠** —— DWG 那对
    33 个紫框里 22 个是 15~50px 的小框，全都没进匹配、被原样保留，而它们恰恰
    是位移噪声的主力（实测 ncc 0.84~0.96、残差 0~4px）。现在 20px 起就参与匹配。
    ⚠️ 没有 numpy/cv2、或框数超过 `FAST_CORR_MAX_BOXES`（整页重绘）时原样返回：
    宁可多报几个框，也不能因为滤镜挂了就什么都不报。
    """
    try:
        import numpy as _np
        import cv2
    except Exception:
        return list(rects), []
    if not FAST_CORR or not rects or len(rects) > FAST_CORR_MAX_BOXES:
        return list(rects), []
    H, W = A.shape
    pad = 6
    # ⚠️ 分组**不做横向膨胀**（和 `_tight_rects` 里不同）。那里的膨胀是为了把同一行
    #    的碎笔画连成「一句话」；这里要判断「有没有一块像样的残留」，把相距 10px 的
    #    几个小点并成一个 24px 的块，纯位移就会被误判成真改动（实测转曲样本
    #    丢框从 19 个掉到 16 个，多出来的 3 个全是这种并出来的假块）。
    kh = _np.ones((3, 3), _np.uint8)
    out: List[Tuple[float, float, float, float]] = []
    moved: List[Tuple[Tuple[float, float, float, float],
                      Tuple[float, float, float, float]]] = []
    n_drop = n_shrink = n_keep = 0

    for r in rects:
        x0, y0, x1, y1 = r
        tx0 = max(0, int(x0 * scale) - pad)
        ty0 = max(0, int(y0 * scale) - pad)
        tx1 = min(W, int(x1 * scale) + pad)
        ty1 = min(H, int(y1 * scale) + pad)
        t = A[ty0:ty1, tx0:tx1]
        # 太小 / 近乎纯色（纯白页、纯色块）→ 配对没有意义，原样保留
        if (t.size < 64 or t.shape[0] < 6 or t.shape[1] < 6
                or max(t.shape) < FAST_CORR_SMALL_PX
                or float(t.std()) < 6.0):
            out.append(r)
            n_keep += 1
            continue
        small = max(t.shape) < FAST_CORR_MIN_PX
        wx0 = max(0, tx0 - FAST_CORR_PX)
        wy0 = max(0, ty0 - FAST_CORR_PX)
        wx1 = min(W, tx1 + FAST_CORR_PX)
        wy1 = min(H, ty1 + FAST_CORR_PX)
        win = B[wy0:wy1, wx0:wx1]
        if win.shape[0] < t.shape[0] or win.shape[1] < t.shape[1]:
            out.append(r)
            n_keep += 1
            continue
        try:
            _mm = cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED)
            _mnv, mxv, _ml, ml = cv2.minMaxLoc(_mm)
        except Exception:
            out.append(r)
            n_keep += 1
            continue
        if mxv < FAST_CORR_MIN_SCORE:
            out.append(r)                      # 对面没有这块内容 → 真变化
            n_keep += 1
            continue
        b = B[wy0 + ml[1]:wy0 + ml[1] + t.shape[0],
              wx0 + ml[0]:wx0 + ml[0] + t.shape[1]]
        if b.shape != t.shape:
            out.append(r)
            n_keep += 1
            continue
        _dx = (wx0 + ml[0]) - tx0
        _dy = (wy0 + ml[1]) - ty0
        # 同一坐标下的平均差（对齐前） vs 匹配位置上的平均差（对齐后）
        _rb = float(_np.abs(t.astype(_np.int16)
                            - B[ty0:ty1, tx0:tx1].astype(_np.int16)).mean())
        _ra = float(_np.abs(t.astype(_np.int16) - b.astype(_np.int16)).mean())
        mask = _tol_diff(t, b, FAST_CORR_INK, FAST_CORR_RAD)
        ncc, lab, st, _ct = cv2.connectedComponentsWithStats(
            cv2.dilate(mask.astype(_np.uint8), kh), connectivity=8,
            ltype=cv2.CV_32S)
        blobs: List[Tuple[int, int, int, int]] = []
        for ci in range(1, ncc):
            bx, by, bw, bh = (int(v) for v in st[ci][:4])
            sub = mask[by:by + bh, bx:bx + bw] & (lab[by:by + bh, bx:bx + bw] == ci)
            cnt = int(sub.sum())
            if cnt < FAST_CORR_MIN_BLOB:
                continue
            ys, xs = _np.nonzero(sub)
            blobs.append((bx + int(xs.min()), by + int(ys.min()),
                          bx + int(xs.max()) + 1, by + int(ys.max()) + 1))
        if not blobs:
            # 残差不足一块（< `FAST_CORR_MIN_BLOB`）。两种解释：
            #   ① 这儿**根本没变**（抗锯齿/亚像素毛刺，残差 ≤2px）→ 归位移，别占紫框；
            #   ② 整块**平移走了**（位移 ≥2pt，对齐后几乎没残差）→ 归位移浅蓝。
            # ⚠️ **同位置有残差 = 原地改了，必须留紫框**。实测踩的坑：合成标签
            #    「100→120」的数字框残差 5px、位移只有 0.85pt，被 0.837 的高分配到
            #    旁边另一个「0」上 —— 只看「得分高 + 残差小」会把真改动吞成位移。
            # ⚠️ 中低分（< MOVED_SCORE）的匹配多半是认错亲，一律整框保留。
            _resid = int(mask.sum())
            _off = float(_np.hypot(_dx, _dy)) / scale
            _pure = (_resid <= FAST_CORR_MOVED_RESID
                     or _off >= FAST_CORR_MOVED_MINOFF)
            if mxv >= FAST_CORR_MOVED_SCORE and _pure:
                moved.append((r, (x0 + _dx / scale, y0 + _dy / scale,
                                  x1 + _dx / scale, y1 + _dy / scale)))
                n_drop += 1
            elif (not small) and (_rb <= 1e-6 or _ra <= FAST_CORR_GAIN * _rb):
                # 大框的兜底：对齐后平均差掉到原来的 35% 以下 = 确实是挪走了
                moved.append((r, (x0 + _dx / scale, y0 + _dy / scale,
                                  x1 + _dx / scale, y1 + _dy / scale)))
                n_drop += 1
            else:
                out.append(r)
                n_keep += 1
            continue
        # 有残差 = 内容真变了。小框的残差判定不稳（就几个笔画），别收缩、整框留。
        if small:
            out.append(r)
            n_keep += 1
            continue
        # 行内字数变化会把整行推右（水平位移为主），这时若配对还不牢（< MOVED_SCORE）
        # 就别猜收缩点 —— 实测「热风枪」→「热缩管加热器」那条半行框会配到另一行
        # （ncc 0.918），一收缩就把真改动收没了。整框留，让用户自己看。
        if abs(_dy) < abs(_dx) and mxv < FAST_CORR_MOVED_SCORE:
            out.append(r)
            n_keep += 1
            continue
        # 对齐之后还有一大片对不上 → 这个匹配不可信（多半是「内容真的换了，
        # 只是恰好长得有点像」）。这种别收缩，整框照报，让用户自己看。
        _frac = float(mask.sum()) / float(t.size)
        if _frac > FAST_CORR_MAX_RESID or len(blobs) > FAST_CORR_MAX_BLOBS:
            out.append(r)
            n_keep += 1
            continue
        n_shrink += 1
        # 同一个词的几个字会各出一个框（「热风枪」3 个 9×9pt 小框），并成一个：
        # 一处改动就该一个框。间距 12pt ≈ 一个字（实测那三个残差框彼此隔 2~10pt，
        # 8pt 会漏掉一个；两个独立改动之间至少隔一个完整的词，不会误并）。
        for (bx0, by0, bx1, by1) in _merge_blobs_px(
                blobs, gap=max(4, int(12 * scale))):
            px0 = (tx0 + bx0) / scale
            py0 = (ty0 + by0) / scale
            px1 = (tx0 + bx1) / scale
            py1 = (ty0 + by1) / scale
            cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
            if px1 - px0 < FAST_MIN_BOX_PT:
                px0, px1 = cx - FAST_MIN_BOX_PT / 2, cx + FAST_MIN_BOX_PT / 2
            if py1 - py0 < FAST_MIN_BOX_PT:
                py0, py1 = cy - FAST_MIN_BOX_PT / 2, cy + FAST_MIN_BOX_PT / 2
            out.append((max(0.0, px0), max(0.0, py0),
                        min(W / scale, px1), min(H / scale, py1)))

    # ---- 位移块「前沿」校验（2026-10-29）----
    # 物理事实：一整块内容平移时，块的**前沿**（逆着位移方向那一侧、且不在任何
    # 位移目标框里的那几行）必然被腾空 —— 没有任何内容会从更远处补进来。
    # 所以前沿还有墨 = 那是新加的内容，这个位移对不成立。
    #
    # 为什么只查前沿、不查整块：块内部那些「真改了一行」的位置，残差框已经收缩
    # 报出来了，整块再查一遍只会重复框（合成用例实测：9 行下移里 2 行真改动，
    # 整块查会多出 2 个重复框，把「不再是 9 条」这条回归打翻）。
    #
    # 实测（对比测试_A/B_转曲）：注意事项新版多第 8 条，全块上移 16.8pt，
    # 位移目标最低到 y719，而第 8 条在 y719–740 —— 恰恰是前沿位置，正向得分
    # 却 0.98。只查前沿就能精确命中，且不动块内的 15 个正常位移对。
    if moved:
        _top = min(mb[1] for (_ma, mb) in moved)
        _bot = max(mb[3] for (_ma, mb) in moved)
        _exp = _np.zeros((H, W), dtype=bool)

        def _mark(rr) -> None:
            a0 = max(0, int(rr[0] * scale) - 2); b0 = max(0, int(rr[1] * scale) - 2)
            a1 = min(W, int(rr[2] * scale) + 2); b1 = min(H, int(rr[3] * scale) + 2)
            if a1 > a0 and b1 > b0:
                _exp[b0:b1, a0:a1] = True

        for (_ma, _mb) in moved:
            _mark(_mb)
        _moved_ids = set(id(_ma) for (_ma, _mb) in moved)
        for _r in rects:
            if id(_r) not in _moved_ids:
                _mark(_r)
        _bad = []
        for (_ma, _mb) in moved:
            _dy = _mb[1] - _ma[1]
            if _dy < 0:                     # 往上挪 → 前沿在下
                _front = _ma[3] > _bot + 2.0
            elif _dy > 0:                   # 往下挪 → 前沿在上
                _front = _ma[1] < _top - 2.0
            else:
                _front = False
            if not _front:
                continue
            if _unexplained_ink(B, _exp, _ma, scale) < FAST_CORR_MIN_BLOB:
                continue                       # 前沿是空的 → 干净的平移
            # 前沿有墨：还得看这墨有没有来处（整块上下移时，前沿会由块外那一行补上）
            if _front_ink_from_a(A, B, _ma, _mb, scale) >= FAST_CORR_MIN_BLOB:
                _bad.append((_ma, _mb))
        if _bad:
            # ⚠️ 用 rect 对象的 id 去匹配 `moved` 里的条目：`_bad` 里是新建的
            #    元组，`id(元组)` 和 `moved` 里那两个不是同一个对象，拿它当键
            #    一条也删不掉（实测：紫框加了、浅蓝也还在，同一处标两遍）。
            _badset = set(id(p[0]) for p in _bad)
            for (ma, mb) in _bad:
                out.append(ma)
                n_keep += 1
                n_drop -= 1
            moved = [p for p in moved if id(p[0]) not in _badset]

    # 同一处改动常常被 `_tight_rects` 拆成两三个相邻输入框，于是收缩后还是
    # 「一字一框」（实测「热风枪」三个字：两个输入框 → 两个输出框，差 10pt 没并上）。
    # 在**整页结果**上再并一次：同一行、横向间距 < 12pt（约一个字）的合成一个框。
    if len(out) > 1:
        out = [(float(a), float(b), float(c), float(d))
               for (a, b, c, d) in _merge_blobs_px(out, gap=12.0)]
    LAST_FAST_CORR.append({"boxes": len(rects), "dropped": n_drop,
                           "shrunk": n_shrink, "kept": n_keep,
                           "moved": len(moved), "out": len(out)})
    # 收缩可能让两个原本不相干的框落到同一处（实测 DWG 出现
    # (571,529)-(587,538) 与 (571,531)-(587,540) 这种几乎重合的一对）。
    # 一个用户没法再读第二遍的位置，报两遍没有意义。
    if len(out) > 1:
        uniq: List[Tuple[float, float, float, float]] = []
        for r in sorted(out, key=lambda t: -((t[2] - t[0]) * (t[3] - t[1]))):
            ra = max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])
            dup = False
            for q in uniq:
                qa = max(0.0, q[2] - q[0]) * max(0.0, q[3] - q[1])
                iw = min(r[2], q[2]) - max(r[0], q[0])
                ih = min(r[3], q[3]) - max(r[1], q[1])
                if iw <= 0 or ih <= 0:
                    continue
                inter = iw * ih
                if inter / max(1e-6, ra + qa - inter) > 0.5:
                    dup = True
                    break
            if not dup:
                uniq.append(r)
        out = uniq
    if os.environ.get("PDFDIFF_CORR_DEBUG"):
        print("[corr] 进%d 移%d 收%d 留%d → 出%d"
              % (len(rects), n_drop, n_shrink, n_keep, len(out)))
    return out, moved


def _hits_any(r, boxes, min_frac: float = 0.3) -> bool:
    """`r` 有多大比例落在 `boxes` 里的任意一个矩形内（用来判断「这块是不是图片」）。"""
    ra = max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])
    if ra <= 0:
        return False
    for b in boxes:
        ov = (min(r[2], b[2]) - max(r[0], b[0])) * (min(r[3], b[3]) - max(r[1], b[1]))
        if ov > 0 and ov >= min_frac * ra:
            return True
    return False


def _hot_tiles(A: bytes, B: bytes, W: int, H: int, skip: set,
               tile: int, dark: int, frac: float) -> set:
    """逐瓦片找「内容变了」的瓦片（跳过有文字层的、以及两侧一样的）。"""
    hot = set()
    for r in range((H + tile - 1) // tile):
        y0, y1 = r * tile, min((r + 1) * tile, H)
        for c in range((W + tile - 1) // tile):
            if (r, c) in skip:
                continue
            x0, x1 = c * tile, min((c + 1) * tile, W)
            npx = (y1 - y0) * (x1 - x0)
            if npx <= 0:
                continue
            cnt = 0
            need = frac * npx            # 达到这个「不一样」的像素数才算变了
            for y in range(y0, y1):
                row = y * W
                # 按行切片 + zip：比逐像素下标快好几倍（纯 Python 没有 numpy 可用）
                for p, q in zip(A[row + x0:row + x1], B[row + x0:row + x1]):
                    d = p - q
                    if (d if d >= 0 else -d) > dark:
                        cnt += 1
                        if cnt >= need:
                            break
                if cnt >= need:
                    hot.add((r, c))
                    break
    return hot


def _tiles_to_rects(hot: set, tile: int, scale: float,
                    min_tiles: int = -1) -> List[Tuple[float, float, float, float]]:
    """把相邻的「变了」瓦片并成矩形（4 邻接），再换算回页面坐标 (pt)。

    `min_tiles`：连通域小于它的不报（去碎点），默认 `GRAPHIC_MIN_TILES`。
    「文字层错位」补查（v48）传 1 —— 真改动可能只落在一个瓦片里
    （M4506 的 `cool` 一词只让 1 个瓦片过 FRAC），判定权在 `_decide_region`
    的 OCR 文本比对，碎点噪声进不了结论。
    """
    if min_tiles < 0:
        min_tiles = GRAPHIC_MIN_TILES
    out: List[Tuple[float, float, float, float]] = []
    seen = set()
    for t in hot:
        if t in seen:
            continue
        comp, q = [], deque([t])
        seen.add(t)
        while q:
            r, c = q.popleft()
            comp.append((r, c))
            for nb in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if nb in hot and nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        if len(comp) < min_tiles:
            continue
        rs = [x[0] for x in comp]
        cs = [x[1] for x in comp]
        x0 = min(cs) * tile / scale - GRAPHIC_PAD
        y0 = min(rs) * tile / scale - GRAPHIC_PAD
        x1 = (max(cs) + 1) * tile / scale + GRAPHIC_PAD
        y1 = (max(rs) + 1) * tile / scale + GRAPHIC_PAD
        out.append((max(0.0, x0), max(0.0, y0), x1, y1))
    return sorted(out, key=lambda r: (round(r[1], 1), r[0]))


class _GrayArr:
    """灰度位图的极小替身：让 `_pm_gray_array()` / `_hot_tiles()` 能直接吃
    「已经处理过的 numpy 数组」（配准/平滑之后的结果），不必再包一层 Pixmap。"""
    __slots__ = ("samples", "width", "height", "stride")

    def __init__(self, arr):
        self.samples = arr.tobytes()
        self.height, self.width = arr.shape
        self.stride = self.width


def _fast_denoise_pair(pa, pb, dpi: int = GRAPHIC_DPI, dark: int = GRAPHIC_DARK):
    """快速模式的降噪前置：**整页亚像素配准 + 预平滑**。

    返回值 `(A, B, info)`：两份同尺寸 u8 灰度数组，加一份说明
    `{"dx", "dy", "resp", "gain", "applied"}`（位移按 pt 记，仅供日志与自检）。

    为什么是「配准」而不是「降噪」
    ------------------------------
    用户报的「全局都被标出来」不是随机噪点，而是**整页平移抖动**：新旧标签是两次
    导出/两次扫描，整页差 0.3~2pt，于是每一条笔画边缘都成了「变了」的像素。这类
    差异在频域上是一个**尖峰**，`cv2.phaseCorrelate` 一次 FFT 就能拿到亚像素级位移；
    把它补掉之后，剩下的才是真改动（实测 6.8% → 0.30%，85% 巨框 → 2 个小框）。

    ⚠️ 顺序不能反：**先配准、后平滑**。反过来平滑会改变相位，估出的位移偏 0.2~0.4px，
       等于没配准。
    ⚠️ 只在「抖动级」才动手：位移 > `FAST_ALIGN_MAX_PT` 说明内容被**挪动**了
       （正是完整模式 `moved` 该报的东西），整页平移会把它抹掉。
    ⚠️ 采用前必须**证明有效**：补偿后 |差|>dark 的像素数要降到原来的
       `FAST_ALIGN_GAIN` 以下才换用；不降就原样退回。方向估错、页面本就是两张
       完全不同的图，都靠这一条兜住。
    """
    import numpy as _np
    A = _pm_gray_array(pa)
    B = _pm_gray_array(pb)
    info = {"dx": 0.0, "dy": 0.0, "resp": 0.0, "gain": 1.0,
            "angle": 0.0, "applied": False}
    if not FAST_DENOISE or A.shape != B.shape or min(A.shape) < 32:
        return A, B, info
    try:
        import cv2
    except Exception:
        return A, B, info
    ppt = float(dpi) / 72.0

    def _try(B2, label):
        """补偿是否值得采用：差异像素数必须降到原来的 FAST_ALIGN_GAIN 以下。"""
        before = int((_np.abs(A.astype(_np.int16) - B.astype(_np.int16)) > dark).sum())
        after = int((_np.abs(A.astype(_np.int16) - B2.astype(_np.int16)) > dark).sum())
        info["gain"] = (after / before) if before else 1.0
        if before and after <= before * FAST_ALIGN_GAIN:
            info["applied"] = True
            return B2
        return B

    # ① 刚体精配准（旋转 + 平移）。**这一条是主力**：扫描件与两次导出的旋转
    #    常达 0.1~0.5°，纯平移补偿压不住（实测 0.12° 就把整页判成「全变了」；
    #    刚体配准后同一对降到 9%）。ECC 同时解旋转与平移，90~160ms/页。
    #    ⚠️ 角度只在 ±`FAST_ALIGN_MAX_DEG` 内接受：再大就不是「同一页的抖动」，
    #       而是内容被转了 —— 强行转回来会把真差异一起抹掉。
    try:
        _w = _np.eye(2, 3, dtype=_np.float32)
        _cc, _w = cv2.findTransformECC(
            A, B, _w, cv2.MOTION_EUCLIDEAN,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5), None, 3)
        _ang = float(_np.degrees(_np.arctan2(_w[1, 0], _w[0, 0])))
        info["resp"] = float(_cc)
        info["angle"] = _ang
        if _cc >= FAST_ALIGN_MIN_RESP and abs(_ang) <= FAST_ALIGN_MAX_DEG:
            B2 = cv2.warpAffine(B, _w, (B.shape[1], B.shape[0]),
                                flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                borderValue=255)
            info.update(dx=float(_w[0, 2]) / ppt, dy=float(_w[1, 2]) / ppt)
            B = _try(B2, "ecc")
    except Exception:
        pass

    # ② ECC 没用上（纯白页无纹理 / 无 cv2 ECC / 不降）时退回纯平移估计。
    #    phaseCorrelate 的返回值是「B 相对 A 的位移」，所以补偿时往**反方向**挪
    #    （2026-10-26 实测把方向搞反过一次，6.8% 反而变成 9.6% —— 这也是
    #    `_try` 那道「必须降才采用」保险的由来）。
    if not info["applied"]:
        try:
            (dx, dy), resp = cv2.phaseCorrelate(_np.float32(A), _np.float32(B))
            lim = FAST_ALIGN_MAX_PT * ppt
            info["resp"] = max(info["resp"], float(resp))
            if (resp >= FAST_ALIGN_MIN_RESP and abs(dx) <= lim and abs(dy) <= lim
                    and (abs(dx) + abs(dy)) >= 0.35):
                B2 = cv2.warpAffine(B, _np.float32([[1, 0, -dx], [0, 1, -dy]]),
                                    (B.shape[1], B.shape[0]),
                                    flags=cv2.INTER_LINEAR, borderValue=255)
                info.update(dx=dx / ppt, dy=dy / ppt)
                B = _try(B2, "shift")
        except Exception:
            pass
    # 预平滑：压掉配准后剩余的 1px 级残差与 JPEG 振铃（实测能把碎框从 7~15 收到 1~2）
    k = int(FAST_BLUR_PX) | 1
    if k >= 3:
        try:
            A = cv2.GaussianBlur(A, (k, k), 0)
            B = cv2.GaussianBlur(B, (k, k), 0)
        except Exception:
            pass
    return A, B, info


def _local_align(A, B, bx0: int, by0: int, bx1: int, by1: int,
                 scale: float, dark: int, pad: int = 6):
    """在一个**大块**的包围盒里再估一次**仿射**（缩放+旋转+平移）。

    「整页只估一个刚体变换」对 DWG 图纸这类文件不够用：图框、中间的标签位图、
    右侧变更表**各自错位量不同**（实测分别是 7pt、缩放 1.4%、1pt），配准其中一个
    就把另外两个变成「整块都变了」。

    返回 `(warp, before, after)`（`before/after` 是差异像素占比），或 `None`。
    `None` 的含义很明确：**这块配不上**（内容真的不同），调用方照实报大块。
    """
    try:
        import numpy as np
        import cv2
    except Exception:
        return None
    H, W = A.shape
    x0 = max(0, bx0 - pad)
    y0 = max(0, by0 - pad)
    x1 = min(W, bx1 + pad)
    y1 = min(H, by1 + pad)
    if x1 - x0 < 24 or y1 - y0 < 24:
        return None
    a = A[y0:y1, x0:x1].astype(np.float32)
    b = B[y0:y1, x0:x1].astype(np.float32)
    before = float((np.abs(a - b) > dark).mean())
    if before <= 0:
        return None
    best = None
    for mode in (cv2.MOTION_AFFINE, cv2.MOTION_EUCLIDEAN):
        w = np.eye(2, 3, dtype=np.float32)
        try:
            _cc, w = cv2.findTransformECC(
                a, b, w, mode,
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5),
                None, 1)
        except Exception:
            continue
        sc = float(np.hypot(w[0, 0], w[1, 0]))
        ang = float(np.degrees(np.arctan2(w[1, 0], w[0, 0])))
        if not (1 - FAST_LOCAL_SCALE_TOL <= sc <= 1 + FAST_LOCAL_SCALE_TOL):
            continue
        if abs(ang) > FAST_LOCAL_ANG_TOL:
            continue
        if (abs(w[0, 2]) > FAST_LOCAL_SHIFT_PT * scale
                or abs(w[1, 2]) > FAST_LOCAL_SHIFT_PT * scale):
            continue
        try:
            b2 = cv2.warpAffine(b, w, (b.shape[1], b.shape[0]),
                                flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                borderValue=255)
        except Exception:
            continue
        after = float((np.abs(a - b2) > dark).mean())
        # 采用门槛：残差要降到一半以下 **且** 绝对值 <8%（见常量处的说明）
        if after <= FAST_LOCAL_GAIN * before and after <= FAST_LOCAL_MAX_RES:
            if best is None or after < best[2]:
                best = (w, before, after)
    if best is None:
        return None
    return best


def _align_images(pg_old, pg_new, A, B, scale: float, dark: int = GRAPHIC_DARK):
    """把**图片对象**逐块配准（6.0 第五轮）。

    这是「整块重新渲染」最干净的入口：DWG 图纸、标签类 PDF 的图面就是一张位图，
    改版重导出时它会被**重新嵌入成不同缩放的图**（实测 rev16→rev18 缩放差 1.38%，
    2438×2644 → 同样的画同一张画，只是被缩过）。这种情况整页一个刚体变换按不住，
    而按图片对象各配一次正好对得上——实测 13.3% → 2.0%。

    返回 `(B2, 记账列表)`；没有任何图片对象、或 `cv2` 不可用时原样返回 B。
    """
    try:
        import numpy as np
        import cv2
    except Exception:
        return B, []
    boxes = []
    for pg in (pg_old, pg_new):
        try:
            boxes.extend(tuple(it["bbox"]) for it in pg.get_image_info())
        except Exception:
            pass
    if not boxes:
        return B, []
    H, W = A.shape
    B2 = B.copy()
    log = []
    for (x0, y0, x1, y1) in boxes:
        bx0, by0 = int(x0 * scale), int(y0 * scale)
        bx1, by1 = int(x1 * scale), int(y1 * scale)
        got = _local_align(A, B, bx0, by0, bx1, by1, scale, dark)
        if got is None:
            continue
        w, before, after = got
        px0, py0 = max(0, bx0 - 4), max(0, by0 - 4)
        px1, py1 = min(W, bx1 + 4), min(H, by1 + 4)
        if px1 - px0 < 8 or py1 - py0 < 8:
            continue
        try:
            B2[py0:py1, px0:px1] = cv2.warpAffine(
                B[py0:py1, px0:px1].astype(np.float32), w,
                (px1 - px0, py1 - py0),
                flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                borderValue=255).astype(np.uint8)
        except Exception:
            continue
        log.append({"rect": (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)),
                    "scale": round(float(np.hypot(w[0, 0], w[1, 0])), 4),
                    "before": round(before, 3), "after": round(after, 3)})
    return B2, log


def _pending_shift(A, B, rect, scale: float, ink: int = SCAN_INK_DARK,
                   min_px: int = FAST_SHIFT_MIN_PX,
                   match: float = FAST_SHIFT_MATCH,
                   max_px: int = FAST_SHIFT_MAX_PX) -> bool:
    """框内这块墨是不是「只是挪了位置」（判据见 `FAST_SHIFT_MIN_PX` 上方的注释）。

    把框内（带 4px 上下文）的墨当模板在对面的邻近窗口里找最佳落点，然后
    **双向**核对墨的重合率。只回答是/否，不改几何、不改数量。

    ⚠️ 双向都要够：单向只要求「A 的墨都能在 B 里找到」的话，「A 是小图、
       B 那边多画了一大块」也会过；反过来只要求 B 单向，则「B 只是碰巧覆盖
       A」也会过。两个方向都 ≥`match` 才说明「两块墨基本是同一块的平移」。
    ⚠️ 偏移必须 ≥`min_px`：整页配准（`_fast_denoise_pair`）已经吃掉全局偏移，
       残留的亚像素抖动不算位移 —— 算进来会把「改了半个字」藏进位移里。
    """
    try:
        import numpy as _np
        import cv2
    except Exception:
        return False
    try:
        h, w = A.shape[:2]
        if B.shape[:2] != (h, w):
            return False
        pad = 4                              # 上下文：纯笔画模板匹配不稳
        tx0 = max(0, int(rect[0] * scale) - pad)
        ty0 = max(0, int(rect[1] * scale) - pad)
        tx1 = min(w, int(_np.ceil(rect[2] * scale)) + pad)
        ty1 = min(h, int(_np.ceil(rect[3] * scale)) + pad)
        th, tw = ty1 - ty0, tx1 - tx0
        if min(th, tw) < 6 or th * tw > FAST_SHIFT_MAX_AREA:
            return False                     # 太小没上下文 / 太大不是「一块墨」
        tpl = A[ty0:ty1, tx0:tx1]
        if float(tpl.std()) < 6.0:
            return False                     # 近乎纯色（纯白/纯色块）：匹配无意义
        wx0, wy0 = max(0, tx0 - max_px), max(0, ty0 - max_px)
        wx1, wy1 = min(w, tx1 + max_px), min(h, ty1 + max_px)
        win = B[wy0:wy1, wx0:wx1]
        if win.shape[0] < th or win.shape[1] < tw:
            return False
        res = cv2.matchTemplate(win.astype(_np.float32),
                                tpl.astype(_np.float32), cv2.TM_CCOEFF_NORMED)
        _mn, _mx, _ml, _loc = cv2.minMaxLoc(res)
        dx = (wx0 + _loc[0]) - tx0
        dy = (wy0 + _loc[1]) - ty0
        if max(abs(dx), abs(dy)) < min_px:
            return False                     # 没挪（或挪得比配准残差还小）
        sx0, sy0 = tx0 + dx, ty0 + dy
        sx1, sy1 = tx1 + dx, ty1 + dy
        if sx0 < 0 or sy0 < 0 or sx1 > w or sy1 > h:
            return False
        a = A[ty0:ty1, tx0:tx1] < ink
        b = B[sy0:sy1, sx0:sx1] < ink
        na = int(_np.count_nonzero(a))
        nb = int(_np.count_nonzero(b))
        if na < FAST_DIR_MIN_PX or nb < FAST_DIR_MIN_PX:
            return False                     # 没几个墨点，说什么都不算数
        inter = int(_np.count_nonzero(a & b))
        return bool(inter >= match * na and inter >= match * nb)
    except Exception:
        return False


def _pending_dir(A, B, rect, scale: float, ink: int = SCAN_INK_DARK,
                 frac: float = FAST_DIR_FRAC,
                 min_px: int = FAST_DIR_MIN_PX) -> str:
    """紫框的**方向**：这一处是「旧版有、新版没了」、「反过来」，还是「只是挪了」。

    返回 `"del"` / `"add"` / `"moved"` / `""`（判不出）。**只影响标注画在哪一侧**，
    不碰矩形、不碰数量、不碰召回 —— 调用方拿到 `_rects` 之后逐条问一句，
    然后就照原样报。
    ⚠️ 2026-11-02 之前它还决定紫框的**边框色**（红/绿/蓝边）和**归属开关**
       （判出位移的跟「位移」勾走）。用户要求紫框统一纯紫后这两件事都没了：
       现在 `add` 只标新版、`del` 只标旧版，`moved` 与判不出的（`""`）一律
       两侧都标、都是纯紫、都归「待判定」那颗勾。也就是说 `moved` 这一档
       在**标注层**已经与 `""` 等价 —— 留着它是因为清单层和统计仍按方向分开
       记（将来若要恢复"位移类单列"也还有据可依），不是为了画得不一样。

    判据见 `FAST_DIR_FRAC` / `FAST_SHIFT_MIN_PX` 的注释：
      ① 框内像素三分，独占墨 ≥`frac` → del / add；
      ② 都不到 → 试着按「平移后能否对上」判位移（`_pending_shift`）；
      ③ 还判不出 → 留纯紫，不猜。
    `A`/`B` 必须是**已配准、已平滑**的同尺寸灰度数组（`_fast_denoise_pair` 的
    产出）—— 用没配准的原图会把「整页抖 0.6pt」判成方向明确的增删。
    """
    try:
        import numpy as _np
        h, w = A.shape[:2]
        x0 = max(0, int(rect[0] * scale))
        x1 = min(w, int(_np.ceil(rect[2] * scale)))
        y0 = max(0, int(rect[1] * scale))
        y1 = min(h, int(_np.ceil(rect[3] * scale)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return ""
        da = A[y0:y1, x0:x1] < ink
        db = B[y0:y1, x0:x1] < ink
        both = int(_np.count_nonzero(da & db))
        only_a = int(_np.count_nonzero(da & ~db))
        only_b = int(_np.count_nonzero(~da & db))
        tot = both + only_a + only_b
        if tot < min_px:
            return ""
        if only_a >= frac * tot:
            return "del"
        if only_b >= frac * tot:
            return "add"
        # 两侧各占一半 —— 最典型的「同一块墨挪了位置」：原位空了、新位有墨。
        # 只有这种「既不像删也不像增」的框才付一次平移核对的代价（其余都已定档）。
        if _pending_shift(A, B, rect, scale, ink):
            return "moved"
    except Exception:
        pass
    return ""


def _tight_rects(pa, pb, rough, scale: float,
                 dark: int = GRAPHIC_DARK, min_px: int = FAST_MIN_PX,
                 pad: float = GRAPHIC_PAD,
                 gap_px: int = FAST_MERGE_GAP_PX,
                 local_align: bool = True, depth: int = 0
                 ) -> List[Tuple[float, float, float, float]]:
    """把瓦片级**粗框**收缩成像素级**紧框**（6.0 快速定位的精度补丁）。

    `rough` 是 `_tiles_to_rects` 出来的瓦片边界框（pt）：每个框最多虚 24px≈14.4pt，
    相邻瓦片还会并成一个连通域，所以「一行里改了一个词」能画成十几个字宽的一条。
    这里回到位图上，按 `|A-B| > dark` 求真正的差异像素，用连通域外接框收回来 ——
    框边贴着实际变了的像素，不再对齐瓦片格。

    `gap_px` 只做**横向**膨胀（竖向 3px，防同一笔画被上下拆成两段）：
    同一行里相距 ≤ gap 的差异并成一句，跨列的大空白不会被连起来。

    `local_align`（6.0 第五轮）：连通域**又大又密**时（≥`FAST_LOCAL_MIN_PX`），
    在这个域里再估一次局部仿射，配得上就只框对齐后**剩下的**残差。
    这是为了 DWG 图纸 / 重嵌入的标签位图 —— 它们整块被重新渲染（缩放差 1%~2%），
    整页一个刚体变换吃不掉，不处理就会收成一个盖住 20% 页面的巨框。

    ⚠️ 本函数**只收缩、不新增**：能进来的都是 `_hot_tiles` 已判「占比达标」的瓦片，
       所以召回与粗框版完全一致，改的只是框的精度。
    ⚠️ 没有 numpy/cv2 时原样返回粗框（宁可框大一点，也不能因此不出结果）。
    """
    try:
        import numpy as np
        import cv2
    except Exception:
        return list(rough)
    A = _pm_gray_array(pa)
    B = _pm_gray_array(pb)
    H, W = A.shape
    k = np.ones((3, max(1, gap_px * 2 + 1)), np.uint8)
    out: List[Tuple[float, float, float, float]] = []

    def _emit(ai, bi, mask, ox: int, oy: int, level: int):
        """从一块 mask 里提取紧框（`ox, oy` 是该 mask 左上角的整页像素坐标）。"""
        n, lab, stats, _ = cv2.connectedComponentsWithStats(
            cv2.dilate(mask.astype(np.uint8), k), connectivity=8, ltype=cv2.CV_32S)
        for ci in range(1, n):
            bx, by, bw, bh = (int(v) for v in stats[ci][:4])
            # 膨胀后的连通域只用来**分组**，框边一律取膨胀前 mask 的真实像素
            sub = mask[by:by + bh, bx:bx + bw] & (lab[by:by + bh, bx:bx + bw] == ci)
            cnt = int(sub.sum())
            if cnt < min_px:
                continue                    # 零星一两个像素：不是内容变化
            ys, xs = np.nonzero(sub)
            nx0 = ox + bx + int(xs.min())
            nx1 = ox + bx + int(xs.max()) + 1
            ny0 = oy + by + int(ys.min())
            ny1 = oy + by + int(ys.max()) + 1
            # 大块（整块图像重新渲染那种）→ 就地再配一次；残差重新提取（只下钻一层）
            if (local_align and level == 0 and FAST_LOCAL_ALIGN
                    and max(nx1 - nx0, ny1 - ny0) >= FAST_LOCAL_MIN_PX):
                got = _local_align(A, B, nx0, ny0, nx1, ny1, scale, dark)
                if got is not None:
                    w, _bf, _af = got
                    px0, py0 = max(0, nx0 - 6), max(0, ny0 - 6)
                    px1, py1 = min(W, nx1 + 6), min(H, ny1 + 6)
                    try:
                        b2 = cv2.warpAffine(
                            B[py0:py1, px0:px1].astype(np.float32), w,
                            (px1 - px0, py1 - py0),
                            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                            borderValue=255)
                    except Exception:
                        got = None
                    else:
                        a2 = A[py0:py1, px0:px1]
                        m2 = np.abs(a2.astype(np.float32) - b2) > dark
                        # 只保留原连通域内的残差，避免把邻块的东西拉进来
                        keep = np.zeros_like(m2)
                        keep[ny0 - py0:ny1 - py0, nx0 - px0:nx1 - px0] = True
                        m2 &= keep
                        if m2.any():
                            _emit(a2, b2.astype(np.uint8), m2, px0, py0, 1)
                        continue
            tx0, ty0 = nx0 / scale - pad, ny0 / scale - pad
            tx1, ty1 = nx1 / scale + pad, ny1 / scale + pad
            # 太小的（一个字的笔画）居中撑到最小可见尺寸，否则预览里看不见
            cx, cy = (tx0 + tx1) / 2, (ty0 + ty1) / 2
            if tx1 - tx0 < FAST_MIN_BOX_PT:
                tx0, tx1 = cx - FAST_MIN_BOX_PT / 2, cx + FAST_MIN_BOX_PT / 2
            if ty1 - ty0 < FAST_MIN_BOX_PT:
                ty0, ty1 = cy - FAST_MIN_BOX_PT / 2, cy + FAST_MIN_BOX_PT / 2
            out.append((max(0.0, tx0), max(0.0, ty0),
                        min(W / scale, tx1), min(H / scale, ty1)))

    for (x0, y0, x1, y1) in rough:
        px0 = max(0, int(x0 * scale) - 2)
        py0 = max(0, int(y0 * scale) - 2)
        px1 = min(W, int(x1 * scale) + 2)
        py1 = min(H, int(y1 * scale) + 2)
        if px1 - px0 < 2 or py1 - py0 < 2:
            out.append((x0, y0, x1, y1))
            continue
        ai = A[py0:py1, px0:px1]
        bi = B[py0:py1, px0:px1]
        if ai.shape != bi.shape:
            out.append((x0, y0, x1, y1))
            continue
        mask = np.abs(ai.astype(np.int16) - bi.astype(np.int16)) > dark
        if not mask.any():
            # 瓦片是「擦边」判热的（3% 占比），像素级一点差都没有 → 这格不报
            continue
        _emit(ai, bi, mask, px0, py0, depth)
    # 膨胀后相邻的框可能仍贴着（分组是逐连通域算的），这里按行再并一次
    return _bands_by_row(out)



def _bands_by_row(rects: List[Tuple[float, float, float, float]]) -> List[Tuple[float, float, float, float]]:
    """同一行（纵向重叠）且横向紧挨着的框并成一个 —— 只并「视觉上连成一句」的。

    ⚠️ 判断「同一行」必须拿**最初那个框**的纵向范围比，不能拿合并后的大框比：
    合并后的大框会越变越高，一路把下面每一行都吸进来 —— 实测转曲样本 22 条
    真实改动（表格整段重排）就这么被并成了一条盖住 30% 页面的竖长条。
    """
    out: List[List[float]] = []
    ref: List[Tuple[float, float]] = []      # 每条带**原始行**的 (y0, y1)
    for r in sorted(rects, key=lambda r: (round(r[1], 1), r[0])):
        if out:
            ry0, ry1 = ref[-1]
            p = out[-1]
            oh = min(ry1, r[3]) - max(ry0, r[1])
            y_hit = oh > 0.3 * max(0.1, min(ry1 - ry0, r[3] - r[1]))
            if y_hit and r[0] <= p[2] + 2.0 and r[2] >= p[0] - 2.0:
                p[0], p[1] = min(p[0], r[0]), min(p[1], r[1])
                p[2], p[3] = max(p[2], r[2]), max(p[3], r[3])
                continue
        out.append([r[0], r[1], r[2], r[3]])
        ref.append((r[1], r[3]))
    return [(a, b, c, d) for a, b, c, d in out]


def _row_bands(rects: List[Tuple[float, float, float, float]]
               ) -> List[Tuple[float, float, float, float]]:
    """碎框太多时的兜底：按视觉行带合并（整页重扫/重绘用）。

    一页出到几十个紧框，说明差异是**整页性**的（重新扫描、整页重绘、压缩噪声），
    这时逐个框反而是噪声，按行并成几条横带更好读 —— 也正好保住「整页重绘」这种
    大改动的可见性。
    """
    out: List[List[float]] = []
    for r in sorted(rects, key=lambda r: (r[1], r[0])):
        if out and r[1] <= out[-1][3] + 1.5:
            p = out[-1]
            p[0], p[1] = min(p[0], r[0]), min(p[1], r[1])
            p[2], p[3] = max(p[2], r[2]), max(p[3], r[3])
            continue
        out.append([r[0], r[1], r[2], r[3]])
    return [(a, b, c, d) for a, b, c, d in out]


# ---------------------------------------------------------------- OCR（可选依赖）
_OCR_EXE: Optional[str] = None
_OCR_PROBED = False
_OCR_CACHE: Dict[Tuple[str, int, int, int, int], str] = {}


def ocr_available() -> bool:
    """本机能不能做 OCR（**引擎表里任一可用**即可；都没有就老实降级，不假装能比对）。"""
    global _OCR_EXE, _OCR_PROBED
    if not _OCR_PROBED:
        _OCR_PROBED = True
        try:
            exe = shutil.which("tesseract")
            if not exe:
                for cand in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
                             r"C:\Program Files\Tesseract-OCR\tesseract.exe"):
                    if os.path.exists(cand):
                        exe = cand
                        break
            _OCR_EXE = exe
        except Exception:
            _OCR_EXE = None
    # rapidocr 是首选：能 import 就算可用（初始化失败已在 _rapidocr_engine 留痕）。
    if _rapidocr_engine() is not None:
        return True
    return bool(_vision_exe()) or bool(_OCR_EXE)


# ---------------------------------------------------------------- OCR 引擎表
# **按顺序尝试，第一个认出字的胜出**（2026-09-30 新增 Vision）。
# 为什么非加 Vision 不可：Tesseract 对这份 IFU 的转曲小字错得离谱 ——
# 同一块「如果需要再次插入该球囊导管…」，tesseract 认成
# 「球圳 / 再折受 / 折县」（囊→圳、叠→受/县/登），而 macOS 自带的 Vision
# 几乎全对（球囊 / 再折叠）。转曲区**原理上只能靠 OCR**（没有文字层），
# 既然只能靠它，就该用认得准的那个 —— 这些字是要给用户看的。
# `PDFDIFF_OCR_ENGINE=tesseract` 可强制退回单引擎。
_VISION_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vision_ocr.swift")
_VISION_VER = 3          # ⚠️ `_vision_ocr.swift` 一改就 +1（二进制缓存 key 带它）
                         # v3: 加 "json" 第二参数 → 行级 JSON（text + boundingBox），
                         #     扫描件整页行对拍管线用（DOC-1003）
                         # v2: 自动语言检测 + 候选池加 ru-RU（俄文标签 DOC-1002）
_vision_bin: Optional[str] = None
_vision_probed = False


def _vision_exe() -> Optional[str]:
    """拿到（必要时**编译一次**）macOS Vision OCR 小工具；不可用返回 None。

    ⚠️ 为什么要预编译成二进制：`xcrun swift foo.swift` 每次都现编译（数秒），
    而一次转曲区比对要 OCR 几十块 —— 不预编译会慢到没法用。
    二进制缓存在系统临时目录，key 带源码路径 + `_VISION_VER`，改源码自动重编。
    非 macOS（或没有 swiftc）直接返回 None，由引擎表退到 tesseract。
    """
    global _vision_bin, _vision_probed
    if _vision_probed:
        return _vision_bin
    _vision_probed = True
    _vision_bin = None
    try:
        if os.uname().sysname != "Darwin":
            return None
        if not os.path.isfile(_VISION_SRC):
            # ⚠️ 教训（2026-10-14）：目录整理把 _vision_ocr.swift 留在根目录、
            #    引擎进了 core/ → 这里静默 None → 图片区文字变化整批漏检，
            #    用户只觉得「识别率低」查了半宿。同场景至少喊一嗓子。
            if shutil.which("xcrun"):
                print("warning: Vision OCR 源码缺失（%s），图片/转曲区识别将退化为"
                      " tesseract 或跳过 —— 检查 _vision_ocr.swift 是否与引擎同目录"
                      % _VISION_SRC, file=sys.stderr)
            return None
        if not shutil.which("xcrun"):
            return None
        key = hashlib.md5(("%s#v%d" % (os.path.abspath(_VISION_SRC), _VISION_VER))
                          .encode("utf-8")).hexdigest()[:16]
        out = os.path.join(tempfile.gettempdir(), "pdfdiff_vision_%s" % key)
        if not (os.path.isfile(out) and os.access(out, os.X_OK)):
            r = subprocess.run(["xcrun", "swiftc", "-O", _VISION_SRC, "-o", out],
                               capture_output=True, timeout=300)
            if r.returncode != 0:
                return None
        _vision_bin = out if os.access(out, os.X_OK) else None
    except Exception:
        _vision_bin = None
    return _vision_bin


def _vision_ocr(img_path: str) -> str:
    """用 macOS Vision 框架认这张图（走编译好的小工具）。不可用 / 失败返回 ""。"""
    exe = _vision_exe()
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, img_path], capture_output=True, timeout=120)
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def _tesseract_ocr(img_path: str) -> str:
    """Tesseract 兜底（跨平台；Windows 交付包里就是它）。"""
    exe = _OCR_EXE or shutil.which("tesseract")
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, img_path, "stdout", "-l", OCR_LANG, "--psm", "6"],
                           capture_output=True, timeout=180)
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


# ---------------------------------------------------------------- RapidOCR
# 4.0 统一 OCR 引擎（2026-10-14）：RapidOCR（Paddle 模型的 ONNX 版）作为
# **跨平台第一优先**，取代「Mac 用 Vision / Windows 用 Tesseract」的不一致现状。
# RapidOCR 识别质量等同 PaddleOCR，但免 paddlepaddle 框架、onnxruntime 推理、
# 体积小、Python 3.13 兼容、Windows 部署干净（转交报告推荐方案）。
#
# 懒加载单例：RapidOCR 首次初始化约 1~2s（加载 det/rec/cls 三个 ONNX 模型），
# 若每次 `_rapidocr_ocr` 都重新 new 一个引擎，整页扫描件管线会慢到没法用。
# 用模块级单例缓存，初始化失败**留痕**（stderr 告警，不能静默 —— 转交报告红线）。
_rapid_engine = None        # RapidOCR 引擎单例（首次调用时初始化）
_rapid_probed = False       # 是否已经尝试过 import/初始化
_rapid_err: Optional[str] = None


def _rapidocr_engine():
    """懒加载 RapidOCR 引擎单例；不可用返回 None（含首次失败原因，留痕用）。

    ⚠️ 2026-10-15 离线化（指令C）：显式传入 det/rec/cls 三个模型路径，从随包分发的
    `models/` 目录定位，**运行时禁止联网下载**（RapidOCR 默认会去 modelscope.cn
    拉模型，断网环境会卡住/失败）。模型查找顺序：exe/脚本同目录 → cwd → _MEIPASS。
    """
    global _rapid_engine, _rapid_probed, _rapid_err
    if _rapid_probed:
        return _rapid_engine
    _rapid_probed = True
    try:
        from rapidocr import RapidOCR
        # 三个模型文件名（RapidOCR 3.x / PP-OCRv6 小模型）
        _det = "PP-OCRv6_det_small.onnx"
        _rec = "PP-OCRv6_rec_small.onnx"
        _cls = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
        _bases = [os.path.dirname(os.path.abspath(__file__)),
                  os.getcwd(),
                  getattr(sys, "_MEIPASS", "")]
        model_dir = None
        for _b in _bases:
            if not _b:
                continue
            _m = os.path.join(_b, "models")
            if os.path.isfile(os.path.join(_m, _det)) \
                    and os.path.isfile(os.path.join(_m, _rec)) \
                    and os.path.isfile(os.path.join(_m, _cls)):
                model_dir = _m
                break
        if not model_dir:
            raise RuntimeError(
                "离线模型缺失：请在程序同目录放 models/ 目录（含 %s/%s/%s）" %
                (_det, _rec, _cls))
        _params = {
            "Global.model_root_dir": model_dir,
            "Det.model_path": os.path.join(model_dir, _det),
            "Rec.model_path": os.path.join(model_dir, _rec),
            "Cls.model_path": os.path.join(model_dir, _cls),
        }
        _rapid_engine = RapidOCR(params=_params)
    except Exception as e:   # noqa: BLE001 —— 任何失败都要留痕，不能静默吞
        _rapid_err = str(e)
        print("warning: RapidOCR 初始化失败（%s）—— OCR 将退化为 tesseract 或跳过"
              % _rapid_err, file=sys.stderr)
        _rapid_engine = None
    return _rapid_engine


def _rapidocr_ocr(img_path: str) -> str:
    """RapidOCR 认图；不可用 / 失败返回 ""。

    输出拼接为纯文本（一行一条，用换行分隔），后续 `_ocr_post` 会做后处理。
    RapidOCR 3.x 返回 `RapidOCROutput` 对象，文本在 `.txts`（元组）。
    """
    eng = _rapidocr_engine()
    if eng is None:
        return ""
    try:
        out = eng(img_path)
        txts = getattr(out, "txts", None) or []
        return "\n".join(t for t in txts if t)
    except Exception:
        return ""


def _rapidocr_lines(img_path: str) -> List[Dict[str, Any]]:
    """用 RapidOCR 出**行级**结果：`[{"t","x0","y0","x1","y1","score"}]`。

    与 Vision 通道（`_ocr_image_lines` 前半段）输出同构：坐标都是
    **左上原点的归一化 [0,1]**，上层两条通道可以无差别混用。

    ⚠️ 为什么需要它（2026-10-19）：`_ocr_image_lines` 原先只走 macOS Vision，
    Windows 包没有 Vision 二进制 → 恒返回 `[]` → 行级对拍管线整条不可用，
    只能退回像素管线。而 RapidOCR 的 `RapidOCROutput` **本来就给行框和置信度**
    （`boxes` 四点多边形 + `scores`），旧代码只取了 `txts`，把两样全扔了。
    接上它们不加任何模型、不加任何依赖，纯属把已有能力用起来。

    ⚠️ 置信度不是摆设：转曲/竖排小字上 PP-OCRv6 会吐 `NEX`/`X` 这类碎片，
    置信度常在 0.2~0.3，不过滤就会进配对逻辑造出假增删。
    """
    checkpoint()          # 中止检查点：OCR 是本引擎最贵的单元（单次约 1s）
    eng = _rapidocr_engine()
    if eng is None:
        return []
    try:
        out = eng(img_path)
    except Exception:
        return []
    # ⚠️ 不能写 `getattr(...) or []`：`boxes`/`scores` 是 numpy 数组，
    #    `or` 会触发 "truth value of an array is ambiguous"（实测踩过）。
    def _as_list(v):
        if v is None:
            return []
        try:
            return list(v)
        except Exception:
            return []

    txts = _as_list(getattr(out, "txts", None))
    boxes = _as_list(getattr(out, "boxes", None))
    scores = _as_list(getattr(out, "scores", None))
    if not txts or not boxes:
        return []
    # 像素尺寸：RapidOCR 给的是图像绝对像素，要归一化才能和 Vision 通道对齐
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            W, H = im.size
    except Exception:
        return []
    if W <= 0 or H <= 0:
        return []
    res: List[Dict[str, Any]] = []
    for k in range(min(len(txts), len(boxes))):
        t = str(txts[k] or "").strip()
        if not t:
            continue
        sc = float(scores[k]) if k < len(scores) else 1.0
        if sc < OCR_LINE_MIN_SCORE:
            continue
        # 四点多边形 → 轴对齐外接框（不 assume 顺序，直接取 min/max）
        try:
            xs = [float(p[0]) for p in boxes[k]]
            ys = [float(p[1]) for p in boxes[k]]
        except Exception:
            continue
        if not xs or not ys:
            continue
        nx0 = max(0.0, min(1.0, min(xs) / W))
        nx1 = max(0.0, min(1.0, max(xs) / W))
        ny0 = max(0.0, min(1.0, min(ys) / H))
        ny1 = max(0.0, min(1.0, max(ys) / H))
        if nx1 <= nx0 or ny1 <= ny0:
            continue
        res.append({"t": _ocr_post(t), "x0": nx0, "y0": ny0,
                    "x1": nx1, "y1": ny1, "score": sc})
    return res


def _ocr_image(img_path: str) -> str:
    """按**引擎表**依次试，返回第一个认出行内容的引擎的结果。

    返回空串 = 所有引擎都没认出东西（图片空白 / 引擎都不可用），调用方据此降级。
    """
    checkpoint()          # 中止检查点
    for eng in OCR_ENGINES:
        if eng == "rapidocr":
            t = _rapidocr_ocr(img_path)
        elif eng == "vision":
            t = _vision_ocr(img_path)
        elif eng == "tesseract":
            t = _tesseract_ocr(img_path)
        else:
            continue
        if (t or "").strip():
            return _ocr_post(t)
    return ""


# 拉丁 ↔ 西里尔**同形字符**折叠表：OCR 在转曲小字上把这俩认混是常态
# （DOC-1002 实测同一处 `80мм` 一次认成 `8омм`——数字 0 vs 西里尔 о，
# `PТА` 一次认成 `PTА`——西里尔 А vs 拉丁 A）。折叠后这些噪声在 diff 里
# 消失，不再触发假的「行内修改」；字形本来就一样，折叠不丢真差异。
# 两份表同键集，仅「是否保留大小写」不同，由下面一张 pairs 字典统一推导。
_HOMOGLYPH_PAIRS = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "Х": "X",
}
# `_HOMOGLYPH` 只在 `_norm_cjk`（覆盖率判重）里用，后面反正要 `.lower()`，
# 所以折成小写即可（`А`→`a`）。
_HOMOGLYPH = str.maketrans({k: v.lower() for k, v in _HOMOGLYPH_PAIRS.items()})

# 同形字折叠（**保留大小写**版）：叠在 OCR 引擎的**原文出口**上。
# 为什么 `_HOMOGLYPH` 之外还要这张表（2026-10-11 DOC-1003 用户实测）：
#   `_HOMOGLYPH` 只在 `_norm_cjk`（覆盖率判重）里用，OCR 的**原文**照样带
#   西里尔字母进清单 —— 实测新版 `A35-070300-200` 被认成 `А35-…`（西里尔 А），
#   用户看到的条目文本就是错的，复制出去搜也搜不到。在引擎出口折叠后：
#   · 同形字形的西里尔字母换成对应的拉丁字母 —— **字形完全一样**，屏幕上
#     看不出差别，但码位干净了（可复制、可搜索、可比对）；
#   · 俄文真词（`Дист`/`м}` 这类非同形字母）不受影响；
#   · 大小写保留（`А`→`A` 而非 `a`），`_HOMOGLYPH` 折成小写是因为它后面
#     反正要 `.lower()`，这里没有那一步，折成小写会把 `А35` 显示成 `a35`。
_HOMOGLYPH_KEEP = str.maketrans(_HOMOGLYPH_PAIRS)


def _ocr_post(t: str) -> str:
    """OCR 引擎原文的统一后处理：同形字折叠（见 `_HOMOGLYPH_KEEP`）。

    ⚠️ 所有引擎出口都必须过这一道（`_ocr_image` / `_ocr_image_lines`），
    漏一条通道清单里就会再冒西里尔字母。
    """
    return (t or "").translate(_HOMOGLYPH_KEEP)


def _fold_line_ts(lines: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """行级 OCR 结果（含**磁盘缓存回读**的旧数据）统一过 `_ocr_post`。

    为什么返回口还要叠一道（2026-10-11 DOC-1003 实测）：`_page_ocr_lines` /
    `_ocr_crop_lines` 的磁盘缓存**不受 `_OCR_DISK_CACHE` 开关管**，修复前写入
    的缓存 JSON 里就带着西里尔 А —— 只在引擎出口折叠的话，旧缓存一命中照样
    把西里尔字母送进清单。翻译是幂等的，重复叠无副作用。
    """
    for it in lines or []:
        if it.get("t"):
            it["t"] = _ocr_post(it["t"])
    return lines


def _norm_cjk(s: str) -> str:
    """归一化：只留中日韩汉字 + 字母数字 + 西里尔字母，丢掉空白/标点/项目符号。

    ⚠️ 西里尔（\\u0400-\\u04ff）不能少：俄文标签 DOC-1002 整页无文字层，全靠
    OCR 兜底 —— 不留西里尔的话 OCR 认得再对也会被归一化删光，coverage 全 0，
    两处真差异（注册证日期 / Rev 号）全漏（2026-09-30 实测）。
    ⚠️ 拉丁↔西里尔同形字符先折叠（`_HOMOGLYPH`）：不折叠的话 `80мм`↔`8омм`
    这类 OCR 抖动会顶满 `_mod_worthy` 的数字差异条件，满页假「修改」。
    ⚠️ **必须先 `.lower()` 再过滤**：过滤正则只放行小写 `a-z`，旧写法
    （先过滤后 lower）把**大写拉丁字母整批删掉** —— `DOC-1002 Rev02` 被归一成
    `3634ev02`、`BrosMed` 变 `rosededical`（DOC-1002 页脚实测），英文内容
    无缘无故丢一半字符，覆盖率判定全部失真。
    """
    return re.sub(r"[^0-9a-z\u0400-\u04ff\u3400-\u9fff]+", "",
                  (s or "").translate(_HOMOGLYPH).lower())


def _coverage(needle: str, hay: str) -> float:
    """needle 有多大比例能在 hay 里**按顺序**找到（容忍 OCR 的少量错字）。

    用 SequenceMatcher 的所有匹配块长度求和 / len(needle)：
    内容真在对面 → 接近 1.0；对面没有这段 → 只能凑到零散几个字，明显偏低。

    ⚠️ 它是**累加**口径 —— 长文本上会虚高，判「是不是同一句话」时**必须**配
    `_longest_cover` 一起用（见那里的实测）。本函数只回答「这些字在不在对面」。
    """
    n = _norm_cjk(needle)
    if len(n) < OCR_MIN_LEN:
        return 0.0
    h = _norm_cjk(hay)
    if not h:
        return 0.0
    sm = SequenceMatcher(None, n, h, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(n)


def _longest_cover(needle: str, hay: str) -> float:
    """needle 的**最长连续**匹配块占 needle 的比例 —— 判「是不是同一句话」。

    为什么 `_coverage` 之外还要这一条（2026-09-30 切到 Vision 后打回来的）
    -------------------------------------------------------------------
    `_coverage` 把**所有**匹配块长度**加起来**，长文本上它会虚高。
    实测新版那条真新增「用充压装置使球囊导管产生负压，并保持；」，
    在**旧版整页** OCR 上的累加覆盖率是 **0.941**（远超 0.85 阈值）→
    被当成「内容还在对面」**撤销**，直接从清单里消失 —— 一条真改动凭空没了。
    可两个文件在说**不同的话**：旧版那句是「使充压装置产生负压使球囊内压力
    完全卸载（卸压终点为球囊工作长度区域基本无液体）」，只是**碰巧**共用了
    「充压装置」「球囊导管」「产生负压」几个词。实测最长连续块只有 4 个字
    （「产生负压」），占 20 字的 **0.20**。

    真正的「内容挪位」是**整句**挪走，最长块接近 1.0。这道闸正好把
    「散落的相同词」和「同一句话」分开 —— 而且是**加**一道，不改 `_coverage`
    的既有语义（它还用在别处，动它会牵动一片判定）。
    """
    n = _norm_cjk(needle)
    if not n:
        return 0.0
    h = _norm_cjk(hay)
    if not h:
        return 0.0
    sm = SequenceMatcher(None, n, h, autojunk=False)
    blocks = sm.get_matching_blocks()
    return (max([b.size for b in blocks]) if blocks else 0) / float(len(n))


# ---- 跨进程 OCR 缓存（见 `_ocr_crop` 的长注释）----
_OCR_DISK_CACHE = os.environ.get("PDFDIFF_OCR_CACHE", "1") != "0"
_OCR_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfdiff_ocr_cache")
_OCR_CACHE_KEEP = 3 * 24 * 3600        # 缓存文件保留 3 天
_ocr_sweep_tick = 0

# ---- 转曲区结果缓存（`graphic_regions` 整体）----
# 实测 DOC-1007：整段流程 78 次 OCR、约 34s，98% 的时间都花在 OCR 上。
# 结果按「文件指纹 + dpi + tile」落盘 —— 同一对文件第二次打开直接命中，
# 从 34s 掉到毫秒级。`PDFDIFF_GRAPHIC_CACHE=0` 可关。
GRAPHIC_CACHE = os.environ.get("PDFDIFF_GRAPHIC_CACHE", "1") != "0"
# ⚠️ 规则版本：`graphic_regions` 的**判定 / 聚段规则**每改一次就 +1。
#    它进缓存 key —— 否则规则改了、缓存还命中旧结果，用户看到的就是「改了个寂寞」
#    （这层缓存跨进程存活，比 OCR 那层更容易骗人）。
GRAPHIC_RULE_VER = 72  # v72: 位移补偿的瓦片跳过加覆盖率门槛 `MOVE_SKIP_FRAC=0.25`（`_rect_tiles(min_frac=)`）——老口径「位移块擦到 0.8pt 就把整块 14.4pt 瓦片行跳过」，把紧挨位移行的下一行一起牺牲，11vs12 左栏新插的「一个再折叠工具」因此进不了像素层，界面只剩补查路径的整段大框。v71: 位移簇命中后**不再 `continue`**——原来簇一命中这块就判完，行级复核 `_line_rescan` 轮不到，块里「新插 1 行 + 被顶下去 2 行」时新增行静默消失（11vs12「内装 → 一个再折叠工具」）。同时缓存 key 加 `_code_stamp()`：本文件 size/mtime 进 key，代码一改自动失效（同日踩了第二次「改了没生效」）。v70: DOC-M2001 删除方向修复（_line_expand 支持缩写方向 removed、_expand_worthy 返回方向、_decide_region removed 对称核查）——⚠️ 该修复当日未 bump 版本号，GUI 命中 v69 修复前缓存导致「只有位移、无增删」（2026-10-19 用户复测发现）；bump 使旧缓存全部失效。v69: 指令D五-1轻量版 `_global_page_offset`（2×3探针模板匹配估整页光栅偏移，≥2块一致且NCC≥0.90判偏移页）——偏移页跳过簇级位移检测（补查/普通两路）与NCC召回；普通路径 moved 位移<TM_SHIFT_MIN_PT 时回落 _line_rescan 行级对拍（根治 v63/v64 把 DOC-M2001 复印件 ~1pt 错位当配准命中→一切判 moved→Storage 真改动被吞的回归，ver62 实测本可报出）。v68: 指令D四 两级召回 `_tm_recall_rects`（行块 NCC<0.85 + 墨迹差≥5% 预筛→`_decide_region` OCR 判定）。v67: 指令D二-2 定稿——簇级位移检测 dpi 150→300（小字匹配质量，150dpi 实测贮存行 NCC 0.275 匹飞、无菌标签假命中）、行带最小高 4→3pt（瓦片边界裁行）、簇最小宽 8→6pt（「有效期 三年」簇 7.2pt 被滤）；补查路径「≥2 簇+位移一致聚类」改「≥2 簇即逐簇报」（行内相对位移各行独立，一致性聚类把 dx=5.3/14.2/13.7/8.9 整群打散→全区漏报）；普通热瓦片路径 side=None 分支新增簇级位移检测（纯转曲行运输条件不在补查路径——瓦片无文字层不进 skip）；补查路径 moved 位移不足时回落簇级检测（_local_shift_overlap 锁死标签≈0 位移被静默丢）。v66: moved 接收加位移下限（TM_SHIFT_MIN_PT）——位移 0 的原位重渲染不报。v65: 指令D二-2 补查路径接簇级位移检测（`_cluster_shift_in_rect` 行→墨迹簇→逐簇模板匹配，≥2 簇位移一致 → moved 浅蓝；`_decide_region` 整体滑动模型对行内相对位移失效）。v64: 指令D二-2 「两边都有字」分支先做位移检测（`_local_shift_overlap` 命中且配准文本一致 → moved 浅蓝；2.0 供应方式区行内相对位移由此检出）。v63: 指令D二-2 `_decide_region` 挪位候选不再静默丢弃——coverage 命中后用 `_local_shift_overlap` 找具体位移报 moved（2.0 供应方式区正文右移 5.5~14.4pt 由静默吞改为标蓝）。v62: 单侧转曲线止损——整页重绘兜底前查两侧文字层词框，一侧 0 一侧有 → 跳过行级 OCR 对拍（rev15vs16 卡死 300s 根治）。v61: 兜底加数字锚点+文字层词框过滤（修 117 条 OCR 抖动假增删）。v60: 整页重绘防护升级——巨框跳过后用行级 OCR 对拍兜回块内局部真差异（rev16vs18 有效期/条码）。v59: 整页图形重绘防护（hot 连通域 ≥15% 页面 → 跳过并记账，修整页标绿巨框）
                       #      也交给 OCR 判定（M4506 实测：隐形文字层与视觉渲染
                       #      错位约 50pt，把视觉上无文字的转曲区标成了「有文字层」，
                       #      「Store in a dry, dark place → dry, dark, cool place」
                       #      整处漏；判定权在 OCR 文本比对，两版相同照旧不报）
                       # v47: mod 行框收窄到真正变了的片段（见 _narrow_mod_region）
                       #      短行先判逐行 ratio 再判整行命中（右上角 `7.0mm` 才报得出）
                       # v19: 行对拍的「挪位撤销」改**逐行最佳 ratio** + 互改闸门
                       #      （旧口径拿整页拼串做子序列覆盖 → coverage 恒≈1.0，
                       #       DOC-1003 的 UDI 数字、鞘管规格全被当挪位撤掉）
                       # v18: 单侧增删候选加「OCR 乱码闸门」`_ocr_junk`
                       #      （DOC-1003 条码区认出 'Oe = > te 4 Пою…' 被当真新增）
                       # v17: 两页位图尺寸不一致时**拉伸对齐**再比（图片输入实测：
                       #      同一份文档 PNG/PDF 折算差 0.3px 就差一行，原来那句
                       #      `continue` 让整页一处都不查还报 0 差异）；
                       #      实在对不上的页记账到 LAST_SIZE_SKIPS，不再静默
                       # v16: 扫描件加「整页 OCR 行对拍」管线（DOC-1003 两张不同
                       #      产品的标签是区域级差异，像素候选结构性失效），
                       #      与像素管线互补去重；swift 加行级 JSON 输出（_VISION_VER=3）
                       # v15: 扫描件先做垂直配准（DOC-1003 两版内容纵向漂移 1.6%，
                       #      不配准整页点亮、只出 4 个候选漏检几十处）
                       # v14: 短文本 mod 分支只看「新侧太短」（n_old 也拉进来会把
                       #      DOC-1007 五块「旧认不出/新认得出」的真新增提前否掉）
                       # v13: 候选矩形 y 沿墨迹扩到字的完整高度（薄片 OCR 读半截字
                       #      造出 3 条假报：9707→1707、表格头乱码×2）
                       # v12: 扫描件候选改「差异列聚簇，每簇独立定行/扩展/判定」
                       # v11: 种子列 + 单行精修 + 墨迹扩展去噪点搭桥 + mod 高分复核
                       # v9: 行级差异画像候选 + _mod_worthy token 级对齐
                       #     + _norm_cjk 先 lower 再过滤（修大写字母被删）+ 短文本 mod 分支
                       # v8: mod 块细切到行（真差行保留、OCR 抖动行丢弃）
                       #      + _norm_cjk 拉丁↔西里尔同形折叠（о↔0、А↔A 噪声源）
_GRAPHIC_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfdiff_graphic_cache")


def _file_fingerprint(p: str) -> str:
    """路径能解析成文件 → `abspath#size#mtime`；否则 `路径#?`（跨进程缓存指纹）。"""
    try:
        st = os.stat(p)
        return "%s#%d#%d" % (os.path.abspath(p), st.st_size, int(st.st_mtime))
    except OSError:
        return str(p) + "#?"


def _code_stamp() -> str:
    """本模块自身的 `size#mtime` —— 给缓存 key 里塞一道「代码一改自动失效」。

    ⚠️ 2026-11-02 加上：`GRAPHIC_RULE_VER` 靠人手 bump，忘了就命中旧缓存，
       界面表现为「改了个寂寞」。同一天踩了第二次（11vs12 少标一行：代码已修好、
       冷缓存跑出 31 框，界面却仍是旧的 30 框，因为 ver 还是 70）。
       注释早写了「DOC-M2001 踩过一次」，靠提醒治不住，改成从机制上兜底：
       只要 `pdfdiff_locate.py` 的 size 或 mtime 变了，key 就变，旧缓存自然落空。
       `GRAPHIC_RULE_VER` 保留 —— 它管的是「语义上的规则代次」，写进日志给人看；
       这一项管的是「这份代码到底跑没跑过」，写进 key 给机器看。
    """
    try:
        st = os.stat(os.path.abspath(__file__))
        return "c%d#%d" % (st.st_size, int(st.st_mtime))
    except Exception:
        return "c?"


def _moved_sig(moved_shifts) -> str:
    """把 `moved_shifts` 压成一段稳定字符串，进缓存 key。

    ⚠️ 2026-11-02：缓存 key 原来**不含 moved_shifts**，于是「不带位移补偿」算出的
       结果会被「带位移补偿」的调用原样吃掉 —— 同一天就是这个把修复顶掉了：
       界面（带 7 条 moved）拿到的是另一条路径算好的 30 条旧结果，左栏一条框都没有。
       它由文字层比对结果算出，而文字层又受界面开关（忽略大小写/标点/页眉页脚）
       影响，所以**不同开关组合本来就该是不同结果**，必须进 key。
    """
    if not moved_shifts:
        return ""
    parts = []
    for _p in sorted(moved_shifts):
        for (_r, _dx, _dy) in moved_shifts[_p]:
            parts.append("%d:%.2f,%.2f,%.2f,%.2f,%.2f,%.2f"
                         % (_p, _r[0], _r[1], _r[2], _r[3], _dx, _dy))
    return "|".join(parts)


def _graphic_cache_file(old_path: str, new_path: str, dpi: int, tile: int,
                        extra: str = "") -> Optional[str]:
    """`graphic_regions` 结果的落盘位置；文件一改（size/mtime 变）就换 key。"""
    if not GRAPHIC_CACHE:
        return None
    try:
        parts = [_file_fingerprint(old_path), _file_fingerprint(new_path),
                 "d%d" % dpi, "t%d" % tile, "v%d" % GRAPHIC_RULE_VER,
                 _code_stamp(),
                 "ms=%s" % extra,
                 "+".join(OCR_ENGINES),
                 "ocr=%s" % OCR_LANG, "vv%d" % _VISION_VER]
        # ⚠️ ocr=/vv= 两项不能少：OCR **能力**变了（语言表 / vision 源码）输出就会变，
        #    key 不带上就命中旧结果 —— 2026-09-30 俄文标签实锤（改完跑出来像没生效）。
        key = hashlib.md5("|".join(parts).encode("utf-8", "replace")).hexdigest()
        os.makedirs(_GRAPHIC_CACHE_DIR, exist_ok=True)
        return os.path.join(_GRAPHIC_CACHE_DIR, key + ".json")
    except Exception:
        return None


def _ocr_cache_file(cache_key, extra: str = "") -> Optional[str]:
    """把 cache_key 折成一个稳定的缓存文件名。

    ⚠️ key 里凡是**能解析成文件路径**的元素，都拼上 `size + mtime`：
    这层缓存跨进程存活，不加指纹的话「同一路径被换成新版文件」会命中旧结果 ——
    那是**假一致**，比慢一百倍都严重。
    """
    if not _OCR_DISK_CACHE or cache_key is None:
        return None
    try:
        parts = []
        for v in (cache_key if isinstance(cache_key, (tuple, list)) else (cache_key,)):
            s = str(v)
            if os.path.isfile(s):
                s = _file_fingerprint(s)
            parts.append(s)
        parts.append(extra)
        key = hashlib.md5("|".join(parts).encode("utf-8", "replace")).hexdigest()
        os.makedirs(_OCR_CACHE_DIR, exist_ok=True)
        return os.path.join(_OCR_CACHE_DIR, key + ".txt")
    except Exception:
        return None


def _ocr_cache_sweep() -> None:
    """偶尔清扫过期缓存（每 50 次写入触发一次），别让临时目录无限长大。"""
    global _ocr_sweep_tick
    _ocr_sweep_tick += 1
    if _ocr_sweep_tick % 50:
        return
    try:
        now = time.time()
        for name in os.listdir(_OCR_CACHE_DIR):
            f = os.path.join(_OCR_CACHE_DIR, name)
            try:
                if now - os.stat(f).st_mtime > _OCR_CACHE_KEEP:
                    os.remove(f)
            except OSError:
                pass
    except Exception:
        pass


def _ocr_cache_load(cache_key, extra: str = "") -> Optional[str]:
    """读**跨进程** OCR 缓存（见 `_ocr_crop` 的长注释）。没有则返回 None。"""
    f = _ocr_cache_file(cache_key, extra)
    if not f or not os.path.isfile(f):
        return None
    try:
        with open(f, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return None


def _ocr_cache_save(cache_key, extra: str, text: str) -> None:
    """写跨进程 OCR 缓存；失败一律静默（缓存只是加速，不该影响正确性）。"""
    f = _ocr_cache_file(cache_key, extra)
    if not f:
        return
    try:
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(text)
        _ocr_cache_sweep()
    except Exception:
        pass


def _ocr_crop(page, clip, dpi: int = OCR_DPI, cache_key=None) -> str:
    """把页面某块渲染成图交给 tesseract，返回识别文本（不可用/失败返回 ""）。

    ⚠️ 缓存分两层（2026-09-30 加的磁盘层）
    ------------------------------------
    `_OCR_CACHE` 是**进程内**的 —— 而用户的实际动线是「打开软件 → 对比 → 导出标注版」，
    两次都跑 OCR，进程一换缓存全废。实测 DOC-1007 补上转曲区兜底后，
    单次对比从 14s 涨到 **96s**（大头就是重复 OCR）。
    第二层落在系统临时目录（`pdfdiff_ocr_cache/`，`PDFDIFF_OCR_CACHE=0` 可关），
    key 里**带文件的 size + mtime**：文件一改，key 就变，绝不会拿到旧结果。
    """
    if not ocr_available():
        return ""
    checkpoint()          # 中止检查点：渲染 + OCR 是最贵的单元
    if cache_key is not None and cache_key in _OCR_CACHE:
        return _OCR_CACHE[cache_key]
    extra = "d%d|%s" % (dpi, "+".join(OCR_ENGINES))
    hit = _ocr_cache_load(cache_key, extra)
    if hit is not None:
        _OCR_CACHE[cache_key] = hit
        return hit
    text = ""
    # ⚠️ 临时 PNG **只落在系统临时目录**（2026-10-10 起不再退回工具目录）：
    #   原来「临时目录写不进就退回本模块所在目录」的兜底，会让 OCR 临时图
    #   漏进工具文件夹 —— 用户得一遍遍手动删 PNG（进度一中断 / 进程被强杀，
    #   `atexit` 清理都不触发，垃圾就留下了）。
    #   正确取舍：临时目录真写不进（极端受限环境），宁可这条 OCR 放弃、
    #   返回空串交给上层兜底，也**绝不往工具目录里塞伴生文件**。
    # ⚠️ 文件名按进程 pid 复用 + **立即清理**（不再依赖 atexit）：见 try/finally。
    img = os.path.join(tempfile.gettempdir(), "_pdfdiff_ocr_%d.png" % os.getpid())
    try:
        page.get_pixmap(dpi=dpi, clip=clip).save(img)
        text = _ocr_image(img)
    except Exception:
        text = ""
    finally:
        try:
            os.remove(img)
        except OSError:
            pass
    if cache_key is not None:
        _OCR_CACHE[cache_key] = text
        # 只缓存成功结果：OCR 引擎缺失时的空串会被"钉死"，
        # 等引擎装好再跑也仍是空 —— 那是自找的假一致。
        if text.strip():
            _ocr_cache_save(cache_key, extra, text)
    return text


@atexit.register
def _ocr_tmp_cleanup() -> None:
    """进程退出时清掉 OCR 临时 png（兜底；正常路径已在 `_ocr_crop` 的 finally 里清过）。

    ⚠️ 只清系统临时目录：工具目录里不再落 OCR 临时图（见 `_ocr_crop`），
       但为保险仍扫一遍工具目录，把**历史遗留**的 `_pdfdiff_ocr_*.png` 一并清掉。
    """
    for d in (tempfile.gettempdir(), os.path.dirname(os.path.abspath(__file__))):
        try:
            os.remove(os.path.join(d, "_pdfdiff_ocr_%d.png" % os.getpid()))
        except OSError:
            pass


# ---- 扫描件「整页 OCR 行对拍」管线（2026-09-30，DOC-1003 实测）----
# 行级 OCR 整页结果的跨进程缓存（key: 文件+页+dpi+引擎+VISION_VER）
_OCR_LINES_CACHE: Dict[Any, List[Dict[str, Any]]] = {}
_OCR_LINES_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfdiff_ocr_lines_cache")


def _ocr_image_lines(img_path: str) -> List[Dict[str, Any]]:
    """行级 OCR：返回 `[{"t": 文本, "x0","y0","x1","y1": 归一化坐标(左上原点)}]`。

    **双通道**，按可用性择一（2026-10-19 起）：
      1. **Vision**（macOS 专属，`core/_vision_ocr.swift`）—— 有二进制就用它，
         精度最好；它返回空就是空，**不再 fallback**（保持 Mac 行为一字不变，
         避免空白页多跑一次 OCR）。
      2. **RapidOCR**（跨平台，Windows 唯一通道）—— Vision 二进制不存在时启用。
         `PDFDIFF_RAPID_LINES=0` 可关掉，退回旧的「Windows 无行级」行为，
         用于 A/B 对比。

    ⚠️ 归一化约定两条通道必须一致（[0,1]，原点左上）：Vision 的原始 bbox
    原点是**左下**，这里已换算；RapidOCR 给的是**像素**，`_rapidocr_lines`
    里已按图像宽高除过。
    """
    exe = _vision_exe()
    if not exe:
        # 没有 Vision（Windows / 未编译）→ 走 RapidOCR 行级通道。
        return _rapidocr_lines(img_path) if RAPID_LINES else []
    try:
        r = subprocess.run([exe, img_path, "json"], capture_output=True, timeout=120)
        data = json.loads((r.stdout or b"[]").decode("utf-8", "replace") or "[]")
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for it in data if isinstance(data, list) else []:
        try:
            x, y = float(it["x"]), float(it["y"])
            w, h = float(it["w"]), float(it["h"])
            t = str(it.get("t", ""))
        except Exception:
            continue
        if not t.strip():
            continue
        out.append({"t": _ocr_post(t.strip()),
                    "x0": x, "x1": x + w,
                    # Vision bbox 原点在**左下**：转成左上原点的 y0/y1
                    "y0": 1.0 - (y + h), "y1": 1.0 - y})
    return out


def _ocr_lines_cached(key: Tuple, sig: str, fetch) -> List[Dict[str, Any]]:
    """行级 OCR 的**通用跨进程缓存骨架**：命中回读 JSON，miss 则 `fetch()` 产出并落盘。

    三个调用方（`_page_ocr_lines` / `_rot90_supplement` / `_ocr_crop_lines`）共享同一套
    「内存缓存 → 磁盘 JSON → 渲染+OCR 生产」样板，差异只在 `sig` 指纹与 `fetch` 的
    渲染/坐标换算。落盘后回填内存缓存并过 `_fold_line_ts`（幂等折叠同形字）。
    """
    if key in _OCR_LINES_CACHE:
        return _OCR_LINES_CACHE[key]
    # ⚠️ 行级结果的**产出方式**一变就要让旧缓存失效（见 `_OCR_LINES_VER`）。
    #    在这里统一拼一次，三个调用方（整页 / 竖排补扫 / 裁剪区）全覆盖 ——
    #    比在每个调用点改 sig 可靠（漏一个就会「代码改了、结果没变」）。
    # ⚠️ 通道也必须进 sig：同一台机器 A/B 切换 Vision / RapidOCR 时，两条通道
    #    产出完全不同（一个读法不同、一个带置信度过滤），不区分就会互相命中
    #    对方缓存 —— 表现是「开关拨了但结果纹丝不动」。
    _chan = "vision" if _vision_exe() else ("rapid" if RAPID_LINES else "none")
    key = tuple(key) + (_OCR_LINES_VER, _chan)
    sig = "%s|L%d|%s" % (sig, _OCR_LINES_VER, _chan)
    os.makedirs(_OCR_LINES_CACHE_DIR, exist_ok=True)
    cf = os.path.join(_OCR_LINES_CACHE_DIR,
                      hashlib.md5(sig.encode("utf-8")).hexdigest()[:20] + ".json")
    lines: Optional[List[Dict[str, Any]]] = None
    if os.path.isfile(cf):
        try:
            with open(cf, "r", encoding="utf-8") as fh:
                lines = json.load(fh)
        except Exception:
            lines = None
    if lines is None:
        lines = fetch() or []
        if lines:
            try:
                with open(cf, "w", encoding="utf-8") as fh:
                    json.dump(lines, fh, ensure_ascii=False)
            except Exception:
                pass
    _OCR_LINES_CACHE[key] = lines
    return _fold_line_ts(lines)


def _sig_with_mtime(path: str, parts: str) -> str:
    """缓存指纹：`abspath|parts` 再拼 size/mtime（跨进程缓存防旧文件假命中）。"""
    sig = "%s|%s" % (os.path.abspath(str(path)), parts)
    try:
        st = os.stat(path)
        sig += "|%d|%d" % (st.st_size, int(st.st_mtime))
    except OSError:
        pass
    return sig


def _page_ocr_lines(page, path: str, i: int, dpi: int) -> List[Dict[str, Any]]:
    """整页行级 OCR（归一化 bbox → pt），带跨进程缓存。"""
    checkpoint()          # 中止检查点：整页 OCR（未命中缓存时按秒计）
    key = (path, i, dpi, "+".join(OCR_ENGINES), _VISION_VER)
    # ⚠️ key 必带 size + mtime：跨进程缓存，不加指纹则「同一路径换新版文件」
    #    会命中旧结果 —— 假一致（skill 铁律）。
    sig = _sig_with_mtime(path, "%d|%d|%s|%d" % (i, dpi, key[3], _VISION_VER))

    def fetch():
        img = os.path.join(tempfile.gettempdir(), "_pdfdiff_lines_%d.png" % os.getpid())
        try:
            page.get_pixmap(dpi=dpi).save(img)
            return _ocr_image_lines(img)
        except Exception:
            return []
        finally:
            try:
                os.remove(img)
            except OSError:
                pass

    return _ocr_lines_cached(key, sig, fetch)


# ---------------------------------------------------------------- 竖排补扫
# 病根（2026-10-11 DOC-1003 用户截图①）：标签顶部那条**旋转 90° 排版**的尺寸
# 区（`.035" OTW / 7.0mm × 150mm / 200cm`，新版多 `Conical`、改 `6.5/7.0`、
# `300`）—— Vision 整页 OCR 对竖排行**整列读丢**（两版各只读出 `Atropos Pro`），
# 行级对拍连候选都形不成；像素管线又因行级路线成功被跳过 → 这块差异一个不报。
# 补扫：把页面位图各转 90° 再 OCR（竖排字转平了就读得出），行框按旋转矩阵
# 映射回原页面归一化坐标。两个方向都扫（竖排有正读/倒读）。
ROT_SUPPLEMENT = os.environ.get("PDFDIFF_ROT_SUPPLEMENT", "1") != "0"


def _rot90_supplement(page, path: str, i: int, dpi: int) -> List[Dict[str, Any]]:
    """整页位图转 90°（顺/逆时针各一次）再行级 OCR，行框映射回原页面。

    坐标映射（归一化、y 向下）：
      · 顺时针转 90°：dst(x', y') = (1−y, x)  → 逆映射 x = y', y = 1−x'
      · 逆时针转 90°：dst(x', y') = (y, 1−x)  → 逆映射 x = 1−y', y = x'
    结果与 `_page_ocr_lines` 同一套跨进程缓存（key 带 rot 标记），
    只在 Vision 行级管线可用时有产出（tesseract 环境自动为空）。
    """
    if not ROT_SUPPLEMENT:
        return []
    try:
        from PIL import Image
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for tag, trans, inv in (
            ("cw", Image.ROTATE_270,
             lambda x0, y0, x1, y1: (y0, 1.0 - x1, y1, 1.0 - x0)),
            ("ccw", Image.ROTATE_90,
             lambda x0, y0, x1, y1: (1.0 - y1, x0, 1.0 - y0, x1))):
        key = (path, i, dpi, tag, "+".join(OCR_ENGINES), _VISION_VER)
        sig = _sig_with_mtime(path, "%d|%d|%s|%s|%d" % (i, dpi,
                              "+".join(OCR_ENGINES), tag, _VISION_VER))

        def fetch(tag=tag, trans=trans, inv=inv):
            lines: List[Dict[str, Any]] = []
            img = os.path.join(tempfile.gettempdir(),
                               "_pdfdiff_rot_%d.png" % os.getpid())
            try:
                pm = page.get_pixmap(dpi=dpi)
                im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
                im.transpose(trans).save(img)
                raw = _ocr_image_lines(img)
                for it in raw:
                    t = it["t"]
                    # 质量闸：真竖排内容要么带数字（`150`/`300`/`2029-07-29`）、
                    # 要么是像样的词（≥5 字，`Conical`/`Prox.`）。没有数字又不足
                    # 5 字的多半是竖排误读碎片（实测 `NEX`/`X` 之类），整条丢。
                    nn = _norm_cjk(t)
                    if not nn or (not any(c.isdigit() for c in nn)
                                  and len(nn) < 5):
                        continue
                    mx0, my0, mx1, my1 = inv(float(it["x0"]), float(it["y0"]),
                                             float(it["x1"]), float(it["y1"]))
                    lines.append({"t": t,
                                  "x0": min(mx0, mx1), "x1": max(mx0, mx1),
                                  "y0": min(my0, my1), "y1": max(my0, my1)})
            except Exception:
                lines = []
            finally:
                try:
                    os.remove(img)
                except OSError:
                    pass
            return lines

        out.extend(_ocr_lines_cached(key, sig, fetch))
    return out


# ---------------------------------------------------------------- 混合输入补行
# 病根：**一侧有文字层、另一侧是扫描件/图片**（旧版电子稿 vs 新版签字扫描件，
# 或反过来）。没有文字层的那一侧在 `extract_rows` 里一行都拿不到 → 字符流只剩
# 一侧的字 → 整份文档被报成「全部删除」（反向则「全部新增」），可对面其实只改了
# 几个字。补行之后两侧都有内容，逐字 diff 自然配出删+增两条。
MIXED_OCR = os.environ.get("PDFDIFF_MIXED_OCR", "1") != "0"


def ocr_page_lines_pt(page, path: str, i: int
                      ) -> List[Tuple[str, float, float, float, float]]:
    """整页 OCR → `[(文本, x0, top, x1, bottom)]`（pt，**pdfminer 口径**）。

    OCR 原生给的是「归一化到 0~1、左上原点」；这里一次翻到位：
    乘页面尺寸得到 pt，再把 y 翻成**原点左下、y 向上**——因为消费方
    `pdfdiff_core` 的行级引擎里 `TextLine.top / bottom` 就是这个口径。
    要构造 `Row`（左上原点）时由调用方翻回去，别在这儿来回改。
    """
    W, H = float(page.rect.width), float(page.rect.height)
    out: List[Tuple[str, float, float, float, float]] = []
    for it in _page_ocr_lines(page, path, i, SCAN_LINES_DPI):
        t = (it.get("t") or "").strip()
        if not t:
            continue
        x0, x1 = float(it["x0"]) * W, float(it["x1"]) * W
        y0, y1 = float(it["y0"]) * H, float(it["y1"]) * H      # 左上原点
        out.append((t, x0, H - y1, x1, H - y0))
    return out


def ocr_page_rows(page, path: str, i: int, page_idx: int,
                  ignore_case: bool = False, ignore_punct: bool = False,
                  min_len: int = MIN_ROW_LEN) -> List[Row]:
    """整页 OCR → `Row` 列表（**字符 bbox 按字符数均分行宽折算**）。

    ⚠️ 折算出来的字符框是**估的**，不是实测 —— 所以这一侧的位置置信度只能是
       `ocr_est`（见 `pdfdiff_core.SRC_CONF`），**不许当成文字层坐标**。
       排「这一处改动到底在哪几个字上」时它够用，但别拿它做像素级判定。
    """
    W, H = float(page.rect.width), float(page.rect.height)
    out: List[Row] = []
    for t, x0, top, x1, bottom in ocr_page_lines_pt(page, path, i):
        norm = _norm(t, ignore_case, ignore_punct)
        if len(norm.replace(" ", "")) < min_len:
            continue                       # 与 extract_rows 同口径：一两字的行不参与
        y0d, y1d = H - bottom, H - top     # 翻回左上原点
        n = max(1, len(t))
        step = (x1 - x0) / n
        chars = [(ch, (x0 + k * step, y0d, x0 + (k + 1) * step, y1d))
                 for k, ch in enumerate(t)]
        out.append(Row(text=t, norm=norm, chars=chars,
                       rect=(x0, y0d, x1, y1d), page=page_idx))
    return out


def _mixed_ocr_fill(pa: List[List[Row]], pb: List[List[Row]],
                    old_path: str, new_path: str, page_idx_off: int = 0,
                    ignore_case: bool = False, ignore_punct: bool = False) -> int:
    """给「**恰好一侧没有文字层**」的页补 OCR 行，返回补了几页。

    ⚠️ 只在下面这几种情况下动手，其余一律不碰（这是零回归的关键）：
       · 两侧都有文字层 → 不是混合输入；
       · 两侧都没有 → 纯扫描件，交给扫描 / 转曲那条管线；
       · 页数不等 → 页对齐另有其事，别在这儿猜；
       · OCR 引擎不可用 → 原样返回，宁可退回旧行为。
    """
    if not MIXED_OCR or not ocr_available():
        return 0
    if len(pa) != len(pb):
        return 0
    if not any((not pa[i]) != (not pb[i]) for i in range(len(pa))):
        return 0                       # 快速短路：没有任何一页是「单侧空」
    try:
        da, db = fitz.open(old_path), fitz.open(new_path)
    except Exception:
        return 0
    n = 0
    try:
        for i in range(len(pa)):
            ea, eb = not pa[i], not pb[i]
            if ea == eb:
                continue
            try:
                if ea:
                    pa[i] = ocr_page_rows(da[i], old_path, i, i + page_idx_off,
                                          ignore_case, ignore_punct)
                else:
                    pb[i] = ocr_page_rows(db[i], new_path, i, i + page_idx_off,
                                          ignore_case, ignore_punct)
            except Exception:
                continue
            if pa[i] or pb[i]:
                n += 1
    finally:
        da.close()
        db.close()
    return n


def _near_shift_hit(page_other, path_other: str, i: int, rect, cand: str) -> bool:
    """短候选沿**同一列**上下扫几个偏移，窄窗**行级**重读（见 `NEAR_SHIFT_RULES`）。

    命中 = 对侧在「同一列、差几 pt~二十几 pt」的地方印着**同一句话** ——
    表格加列/加表头导致的行挪位，不是真增删。

    ⚠️ 必须按**行**对，不能拿整段 OCR 文本对：窄窗差 1pt 就会把上下邻行的字
    一并读进来（`8` → `'8\\n10'`），整段比对必然判「不同」，真挪位就漏判了。
    """
    w_pt, h_pt = float(page_other.rect.width), float(page_other.rect.height)
    for dy in NEAR_SHIFT_DY:
        y0 = max(0.0, float(rect[1]) + dy - NEAR_SHIFT_PAD_Y)
        y1 = min(h_pt, float(rect[3]) + dy + NEAR_SHIFT_PAD_Y)
        x0 = max(0.0, float(rect[0]) - NEAR_SHIFT_PAD_X)
        x1 = min(w_pt, float(rect[2]) + NEAR_SHIFT_PAD_X)
        if y1 - y0 < 4.0 or x1 - x0 < 4.0:
            continue
        for it in _ocr_crop_lines(page_other, fitz.Rect(x0, y0, x1, y1),
                                  path_other, i, "nshift"):
            t = _norm_cjk(it.get("t", ""))
            if t and _near_same(cand, t):
                return True
    return False


def _near_line_hit(page_self, page_other, rect, path_self: str,
                   path_other: str, i: int) -> bool:
    """候选**自己那一行**两侧各分行重读一次、**逐行**比（见 `NEAR_LINE_RULES`）。

    为什么整窗外扩之后再补这一道（DOC-1003 `CE2797当4` 实测）：
    `NEAR_CHECK_STEPS` 的两轮都是拿**整个窗**的 OCR 文本比，而窗的纵向留白是按
    候选行高的**倍数**给的 —— 候选框一高（CE 标志 23.6pt），留白就撑到 19pt，
    把上一行**整行**地址读进来；两版这一行的 OCR 抖动方向不同（新版多出一串
    `staorbrasiirel11s0000000`），整段比必然判「不同」→ 假新增撤不掉。
    可**候选自己那一行**两侧读得一字不差（都是 `СЄ2797`）—— 逐行比就撤得掉。

    ⚠️ 与 `_near_shift_hit` 同一条教训：判「两侧是同一句」**必须按行对**，
    不能拿整段 OCR 文本对（整段里混着邻行，两版邻行读法天然不对称）。

    ⚠️ 只比**候选框管的那一行**（框内、中心离框中心最近的那一行），不是窗里
    所有行 —— 比所有行等于把门槛降到「窗里有一行相同就撤」，那会撤掉真改动。
    """
    r = fitz.Rect(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    w_s, h_s = float(page_self.rect.width), float(page_self.rect.height)
    w_o, h_o = float(page_other.rect.width), float(page_other.rect.height)
    cy = (float(rect[1]) + float(rect[3])) / 2.0

    def _box(it, w_pt, h_pt):
        return (float(it["x0"]) * w_pt, float(it["y0"]) * h_pt,
                float(it["x1"]) * w_pt, float(it["y1"]) * h_pt)

    best, best_dy = None, None
    for it in _ocr_crop_lines(page_self, r, path_self, i, "nlh"):
        nb = _norm_cjk(it.get("t", ""))
        if not nb:
            continue
        mb = _box(it, w_s, h_s)
        if _overlap_frac(mb, rect) < NEAR_LINE_IN_CAND:
            continue                      # 这一行不归候选框管
        dy = abs((mb[1] + mb[3]) / 2.0 - cy)
        if best is None or dy < best_dy:
            best, best_dy = (nb, mb), dy
    if best is None:
        return False
    nb, mb = best
    for ot in _ocr_crop_lines(page_other, r, path_other, i, "nlh"):
        no = _norm_cjk(ot.get("t", ""))
        if not no:
            continue
        if _overlap_frac(_box(ot, w_o, h_o), mb) < NEAR_LINE_OV:
            continue                      # 位置对不上 = 不是同一行
        if _near_same(nb, no):
            return True
    return False


def _line_near_check(page_self, page_other, rect, path_self: str,
                     path_other: str, i: int, text: str = "") -> bool:
    """增删候选的最后一道把关：**两侧同位置各高分 OCR 一次、互相比对**。

    为什么不拿「候选文本」去对面找（第一版踩的坑）：候选文本本身就是**单侧
    乱码**（DOC-1003 底部图标区 `STERILE EO` 在整页 OCR 里被认成
    `STERME AD28@KM米于`），拿乱码当 needle coverage 永远低，假增删撤不掉。
    改成：候选 rect 在**两侧**各高分重读一遍，两段 near 文本归一化 ratio ≥ 0.8 =
    两版这个位置内容相同（整页 OCR 单侧读岔而已）→ 撤。

    ⚠️ 2026-10-01 加**第二轮尝试**（`NEAR_CHECK_STEPS`）：扫描件两版有整体漂移
    （DOC-1003 实测 ~1.6%），且整页 OCR 会**单侧整块漏读** —— 旧版那枚 `GW`
    logo 整页一个 `gw` 都没认出来，贴着框 300dpi 重读也是**空串**，按老口径
    「有一侧认不出 = 证据不足不撤」就被当成新版新增标了出来（用户反馈的假新增）。
    第二轮把框放宽（横 12pt、纵 1.8 倍行高）、分辨率提到 400dpi，
    实测旧版那块能读出 `GW)…` → 与新版一致 → 撤。
    """
    cand = _norm_cjk(text or "")
    for st in NEAR_CHECK_STEPS:
        pad_x, pad_y, dpi = st["pad_x"], st["pad_y"], st["dpi"]
        x0 = max(0.0, rect[0] - pad_x)
        x1 = min(float(page_self.rect.width), rect[2] + pad_x)
        h = max(6.0, rect[3] - rect[1])
        y0 = max(0.0, rect[1] - pad_y * h)
        y1 = min(float(page_self.rect.height), rect[3] + pad_y * h)
        r = fitz.Rect(x0, y0, x1, y1)
        ck = (int(x0 // 20), int(x1 // 20), int(y0 // 20), int(y1 // 20), dpi)
        ns = _norm_cjk(_ocr_crop(page_self, r, dpi=dpi,
                                 cache_key=(path_self, i, "lnr") + ck))
        no = _norm_cjk(_ocr_crop(page_other, r, dpi=dpi,
                                 cache_key=(path_other, i, "lnr") + ck))
        if not ns or not no:
            continue                      # 有一侧读不出 → 换更宽的框再试一轮
        if _near_same(ns, no):
            return True
        # ③ 短候选：对侧重读里能找到这段字 = 单侧切行/漏读造成的假增删
        #    （`GW` 就在旧版 `GW)…` 的乱读结果里）。ratio 闸门是防误杀：
        #    真新增的短词（`Conical`）就算字面出现在对面长串里，占比也远低于阈值。
        if (cand and len(cand) <= NEAR_SUBSTR_MAX and cand in no
                and difflib.SequenceMatcher(
                    None, cand, no, autojunk=False).ratio() >= NEAR_SUBSTR_RATIO):
            return True
    # ③ 纵向重排扫描（`NEAR_SHIFT_RULES`）：短候选沿同一列上下扫几个偏移。
    #    表格加列/加表头会让整表下移，前面两轮只看同一个 y，够不到挪了位的格子。
    if cand and len(cand) <= NEAR_SHIFT_MAX_CHARS:
        if _near_shift_hit(page_other, path_other, i, rect, cand):
            return True
    # ④ 候选**自己那一行**的逐行比对（`NEAR_LINE_RULES`）：①② 拿整窗文本比，
    #    高框候选的留白会把邻行读进来 → 两版整段必然「不同」→ 假增删撤不掉。
    if _near_line_hit(page_self, page_other, rect, path_self, path_other, i):
        return True
    return False                          # ⑤ 证据不足 —— 不撤（与 _scan_mod_confirm 同口径）


def _scan_line_best(n: str, other_lines: Sequence[Dict[str, Any]]) -> Tuple[float, str, Optional[Dict[str, Any]]]:
    """在对面**逐行**找最像的一行，返回 `(ratio, 那一行的原文, 那一行本身)`。

    第三个元素是**对面匹配行**的原始 dict（含 x0/y0/x1/y1 坐标），供调用方
    算位移量（2026-10-12 位移标蓝需求）；找不到时是 None。

    ⚠️ 为什么必须是「逐行」而不是「整页拼串做子序列覆盖」（旧口径，2026-10-01 改）
    ------------------------------------------------------------------------------
    `_coverage(needle, 整页hay)` 是在**一整页的字符海**里按序凑 needle 的字符。
    一页有 3000+ 字符、数字 0-9 到处都是，于是任何短串都能凑齐 →
    coverage 恒 ≈ 1.0。DOC-1003 实测：`5F(0.072"/1.83mm)` 判 **1.0**、
    UDI 那行判 **0.944**，全被当成「挪位」撤掉 —— 用户说的「很多规格数字没识别出来」
    就是这么漏的（一行越长、改得越少，越容易被误撤）。
    """
    best_r, best_raw, best_row = 0.0, "", None
    for it in other_lines or ():
        m = _norm_cjk(it.get("t", ""))
        if not m:
            continue
        r = difflib.SequenceMatcher(None, n, m, autojunk=False).ratio()
        if r > best_r:
            best_r, best_raw, best_row = r, str(it.get("t", "")), it
    return best_r, best_raw, best_row


def _hay_token_hit(n: str, other_lines: Sequence[Dict[str, Any]]) -> bool:
    """「这段字在对面的某个**短条目**里出现过」—— 逐行查容器，不拼整页串。

    ⚠️ 老口径 `n in other_hay` 是在**归一化拼串**上做子串匹配，会撞上「某个长
    单词里正好含着这几个字符」（2026-10-01 实测两处真改动被撤）：
      · 新版表头 `Dist.`（norm `dist`）⊂ 葡语 `Importado e Distribuído`（22 字）
      · 条带 `6.5/`（norm `65`）⊂ 电话号码 `Fone: 11 2548-0365`（15 字）
    改成「容器必须是**短条目**」（≤ `SCAN_MOVE_HAY_MAX_CONT`）两者就分得开：
      · `5` ⊂ `150mm`(5)、`70` ⊂ `6570mm`(6)、`00` ⊂ `200`(3) —— 真·碎片 → 撤
      · `65` ⊂ `fone1125480365`(15)、`dist` ⊂ `importadoedistribudopor`(22) → 巧合 → 留
    """
    for it in other_lines or ():
        m = _norm_cjk(it.get("t", ""))
        if not m or len(m) > SCAN_MOVE_HAY_MAX_CONT:
            continue
        if n in m:
            return True
    return False


def _lead_num(n: str) -> str:
    """行首数字主键：归一化文本里**第一段数字**（`12 (rbp)`→`12`、`6.5/7.0`→`6.5`）。

    表格行的身份就是行首那个数（压力值/尺寸值），字面相似度会骗人
    （`14(RBP)` 和 `12 (RBP)` ratio 0.77，比 `12` 和 `12 (RBP)` 的 0.44 高得多，
    但真正配一对的是后者 —— 见 `SCAN_NUM_PAIR_BOOST`）。
    """
    m = re.search(r"\d+(?:\.\d+)?", n or "")
    return m.group(0) if m else ""


def _same_pos_mod_partner(it, w_a: float, h_a: float,
                          other_lines, w_b: float, h_b: float) -> bool:
    """对面**同一位置**有没有一行「相似但不全同」的行（mod 配对的天然候选）。

    有 → 这行**不能**当挪位撤掉（`_scan_line_moved` 的挪位口径是「这句话
    挪到别处去了」，可对面原地就有一行只差一两个字的 —— 那是行内修改）。
    2026-10-11 DOC-1003 实测：底部 UDI 数字行 50 字只差 `484250`↔`48429 8`
    两个数字，ratio 0.956 过 `SCAN_MOVE_COVER` 被当挪位整行撤掉 ——
    而 token 错位（空格漂移）又让 `_mod_worthy` 拒了互改闸门，两道闸
    叠加 = 真改动消失。位置相同就优先按修改处理。
    """
    ya = (float(it["y0"]) + float(it["y1"])) / 2.0 * h_a
    ax0, ax1 = float(it["x0"]) * w_a, float(it["x1"]) * w_a
    na = _norm_cjk(it["t"])
    for ob_ in other_lines:
        nb = _norm_cjk(ob_["t"])
        if not nb or nb == na:
            continue
        if abs((float(ob_["y0"]) + float(ob_["y1"])) / 2.0 * h_b - ya) > SCAN_PAIR_DY:
            continue
        bx0, bx1 = float(ob_["x0"]) * w_b, float(ob_["x1"]) * w_b
        if abs((bx0 + bx1) / 2.0 - (ax0 + ax1) / 2.0) > SCAN_PAIR_DX:
            if _x_overlap_frac(ax0, ax1, bx0, bx1) < SCAN_PAIR_OV:
                continue
        r = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
        if not (SCAN_LINE_MOD_RATIO <= r < SCAN_MOVE_SAME):
            continue
        # 高相似还不够 —— 得**像一次行内修改**才拦挪位撤除（三选一）：
        # ① 行首数字相同（表格行按主键对齐）；② token 等长的数字小改
        # （`_mod_worthy`，UDI 数字行）；③ 共享一个 ≥6 字的长 token
        # （`6958481` 这类 —— token 错位时 `_mod_worthy` 会拒，长 token 兜住）。
        # 没有这三样就放行挪位判定：多语言段落重排时，相邻两行乱码 vs 正读
        # ratio 也能过 0.55（实测 `paoaajeeo…`→`PTA-pallolaaje…` 0.646），
        # 不加这道闸会把「挪位」误判成「修改」。
        if _lead_num(na) and _lead_num(na) == _lead_num(nb):
            return True
        if _mod_worthy(it["t"], ob_["t"]):
            return True
        ta = set((it.get("t") or "").split())
        tb = set((ob_.get("t") or "").split())
        # ⚠️ 必须卡「差异含数字」：多语言段抖动也共享长 token
        #    （`Recommended` / `Ballon-Dilat…`），不卡会把「挪位/抖动」
        #    误配成「修改」（`R Recommended…` 0.976 实测）。
        if any(len(t) >= 6 for t in (ta & tb)) \
                and any(c.isdigit() for c in set(na) ^ set(nb)):
            return True
    return False


def _scan_line_moved(n: str, raw: str,
                     other_lines: Sequence[Dict[str, Any]],
                     other_hay: str = "") -> bool:
    """这行算不算「只是挪位/重复」（按 `SCAN_MOVE_RULES` 逐条过）。True = 撤。"""
    if not n:
        return True
    if len(n) < OCR_MIN_LEN:
        # ⑥ 短行：**先看逐行 ratio**，只有「对面也有这一整行」才撤。
        #    ⚠️ 顺序不能反（先 `n in other_hay` 会连真改动一起撤）：
        #    `7.0mm` 是 `6.5/7.0mm` 的子串，整行命中 = 撤 → 右上角规格改了却报不出来。
        br, braw, _brow = _scan_line_best(n, other_lines)
        if br >= SCAN_MOVE_SAME:
            return True
        if br >= SCAN_LINE_MOD_RATIO and braw and _mod_worthy(raw, braw):
            return False
        if br >= SCAN_MOVE_COVER:
            return True
        # 含数字的短行对面有「相似但不同」的一行 → 可能是规格数字改了，别撤
        if br >= SCAN_LINE_MOD_RATIO and any(c.isdigit() for c in n):
            return False
        if other_hay and _hay_token_hit(n, other_lines):
            return True
        return False
    br, braw, _brow = _scan_line_best(n, other_lines)
    if br >= SCAN_MOVE_SAME:
        return True                                   # ① 完全相同 / ② 同一行抖动
    # ③ ⚠️ 互改闸门：高相似但两侧构成「行内小改」→ 是真改动，**不撤**
    if br >= SCAN_LINE_MOD_RATIO and braw and _mod_worthy(raw, braw):
        return False
    if br >= SCAN_MOVE_COVER:
        return True                                   # ④ 挪位 / 重排
    return False                                      # ⑤ 保留


def _ocr_crop_lines(page, rect, path: str, i: int, tag: str) -> List[Dict[str, Any]]:
    """裁一块条带做**高分行级 OCR**（Vision json 模式），行框换算回归一化坐标。

    与 `_page_ocr_lines` 的区别：只渲染裁切区（省时），且 400dpi ——
    整页 200dpi 读不全的旋转小字，裁宽了高分读就全（见 `SCAN_REREAD_RULES`）。
    结果按 (文件, 页, 条带量化框, dpi) 跨进程缓存。
    """
    r = fitz.Rect(*rect)
    checkpoint()          # 中止检查点：条带 OCR 与整页 OCR 同级贵
    ck = (int(r.x0 // 20), int(r.x1 // 20), int(r.y0 // 20), int(r.y1 // 20))
    key = (path, i, "band", ck, SCAN_REREAD_DPI, "+".join(OCR_ENGINES), _VISION_VER)
    sig = _sig_with_mtime(path, "%d|band|%s|%d|%s|%d" % (i, ck, SCAN_REREAD_DPI,
                                                        key[5], _VISION_VER))

    def fetch():
        lines: List[Dict[str, Any]] = []
        img = os.path.join(tempfile.gettempdir(), "_pdfdiff_band_%d.png" % os.getpid())
        try:
            page.get_pixmap(dpi=SCAN_REREAD_DPI, clip=r).save(img)
            raw = _ocr_image_lines(img)
        except Exception:
            raw = []
        finally:
            try:
                os.remove(img)
            except OSError:
                pass
        for it in raw:
            lines.append({"t": it["t"],
                          "x0": (r.x0 + it["x0"] * r.width) / page.rect.width,
                          "x1": (r.x0 + it["x1"] * r.width) / page.rect.width,
                          "y0": (r.y0 + it["y0"] * r.height) / page.rect.height,
                          "y1": (r.y0 + it["y1"] * r.height) / page.rect.height})
        return lines

    return _ocr_lines_cached(key, sig, fetch)


def _reread_bands(oa, ob, old_path: str, new_path: str, i: int,
                  la: List[Dict[str, Any]], lb: List[Dict[str, Any]],
                  rem_a: List[Dict[str, Any]], rem_b: List[Dict[str, Any]]
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """对「**真空洞**」条带做两版高分补读，把读全的行并回两侧行集。

    ⚠️ 只补**洞**，不去动两版都读得到的区域（2026-10-01 第一版踩的坑：
    对任何候选簇都补读，两版裁切读出的行切分不一样，把原本恰好抵消的行
    搞得不对称 —— `Compliant` 丢了半截、克罗地亚语整段变假 mod）。
    「洞」= 候选都在同一侧、且**另一侧连一行的中心都落不进这条带** ——
    说明那片是整页 OCR 单侧漏读（DOC-1003 顶部旋转条带），两版各补读
    一次 400dpi 才有行可配。每页最多 `SCAN_REREAD_MAX` 个簇。
    """
    w_a, h_a = oa.rect.width, oa.rect.height
    w_b, h_b = ob.rect.width, ob.rect.height

    def near_other(it, lines, w_pt, h_pt) -> bool:
        """对面在这一行的**近旁**（横向 ±20pt、纵向 ±1.5 行高）有没有行。
        ⚠️ 按项逐个查、别按簇查 —— 簇会把相距几十 pt 的候选链成一串
        （DOC-1003 条带项和第二份标签的候选链成一簇 → 洞判定失效）。"""
        x0 = (float(it["x0"]) * w_pt - 20.0, float(it["y0"]) * h_pt - 1.5 * 22.0,
              float(it["x1"]) * w_pt + 20.0, float(it["y1"]) * h_pt + 1.5 * 22.0)
        for o in lines:
            cx = (float(o["x0"]) + float(o["x1"])) / 2 * w_pt
            cy = (float(o["y0"]) + float(o["y1"])) / 2 * h_pt
            if x0[0] <= cx <= x0[2] and x0[1] <= cy <= x0[3]:
                return True
        return False

    def merge(base: List[Dict[str, Any]], extra: List[Dict[str, Any]],
              w_pt: float, h_pt: float) -> List[Dict[str, Any]]:
        """按 `SCAN_REREAD_RULES` 把补读行并进已有行集。

        ⚠️ 判「同一处」必须**整框重叠**（x、y 都搭上）—— 只看 x 会把 UDI 行
        这类「横向搭着、纵向差得远」的好行也顶掉（第一版踩的坑，DOC-1003
        少了 UDI/规格表一堆真差异）。补读行读不出字（纯符号）时信已有行。
        """
        if not extra:
            return list(base)

        def box(it):
            return (float(it["x0"]) * w_pt, float(it["y0"]) * h_pt,
                    float(it["x1"]) * w_pt, float(it["y1"]) * h_pt)

        def ov_frac(a, b) -> float:
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox <= 0 or oy <= 0:
                return 0.0
            return (ox * oy) / max(1e-9, min((a[2] - a[0]) * (a[3] - a[1]),
                                             (b[2] - b[0]) * (b[3] - b[1])))

        eboxes = [box(e) for e in extra]
        dropped: Set[int] = set()
        claimed: Set[int] = set()
        for j, it in enumerate(base):
            nb = _norm_cjk(it["t"])
            hit = -1
            for k, e in enumerate(extra):
                if ov_frac(box(it), eboxes[k]) >= 0.3:
                    hit = k
                    break
            if hit < 0 or not _norm_cjk(extra[hit]["t"]):
                continue                      # 无补读行接管（或补读行是纯符号）→ 保留
            # ⚠️ 补读行**不比已有行信息量大**就不许顶 —— 裁切带会把整行 UDI
            #    裁成半截 `(01) 0 695848`（v29-32 踩的坑：两版完整 UDI 行都被
            #    半截行顶掉，48425→48429 的真 mod 凭空消失）。
            #    例外：已有的行本身就是 ≤3 字的碎片（`NEX`/`5`/`6.`，整页低分
            #    读岔的）→ 信 400dpi 补读行。
            if len(_norm_cjk(extra[hit]["t"])) < len(nb) and len(nb) >= 4:
                continue
            claimed.add(hit)
            dropped.add(j)                    # 位置被 400dpi 补读行接管
        out2 = [it for j, it in enumerate(base) if j not in dropped]
        for k, e in enumerate(extra):
            ne = _norm_cjk(e["t"])
            if not ne:
                continue
            if k not in claimed:
                taken = False
                for j, it in enumerate(base):
                    if j not in dropped and ov_frac(box(it), eboxes[k]) >= 0.3:
                        taken = True          # 与幸存的已有行同处 → 别重复加
                        break
                if taken:
                    continue
            out2.append(e)                    # 接管位（或整页漏读的新行）→ 加进去
        return out2

    la2, lb2 = list(la), list(lb)
    changed = False
    done_bands: Set[Tuple[int, int, int, int]] = set()
    n_read = 0
    for side, rem, w_pt, h_pt in (("a", rem_a, w_a, h_a), ("b", rem_b, w_b, h_b)):
        for it in sorted(rem, key=lambda x: (float(x["y0"]) + float(x["y1"])) / 2):
            if n_read >= SCAN_REREAD_MAX:
                break
            if side == "a":
                hole = not near_other(it, lb2, w_b, h_b)
            else:
                hole = not near_other(it, la2, w_a, h_a)
            if not hole:
                continue                      # 对面近旁有行 = 不是单侧漏读
            band = (max(0.0, float(it["x0"]) * w_pt - SCAN_REREAD_PAD_X),
                    max(0.0, float(it["y0"]) * h_pt - SCAN_REREAD_PAD_Y),
                    min(w_pt, float(it["x1"]) * w_pt + SCAN_REREAD_PAD_X),
                    min(h_pt, float(it["y1"]) * h_pt + SCAN_REREAD_PAD_Y))
            key = (int(band[0] // 20), int(band[1] // 20),
                   int(band[2] // 20), int(band[3] // 20))
            if key in done_bands:
                continue
            done_bands.add(key)
            n_read += 1
            band_a = (min(band[0], w_a), min(band[1], h_a),
                      min(band[2], w_a), min(band[3], h_a))
            band_b = (min(band[0], w_b), min(band[1], h_b),
                      min(band[2], w_b), min(band[3], h_b))
            la2 = merge(la2, _ocr_crop_lines(oa, band_a, old_path, i, "ba"), w_a, h_a)
            lb2 = merge(lb2, _ocr_crop_lines(ob, band_b, new_path, i, "bb"), w_b, h_b)
            changed = True
        if n_read >= SCAN_REREAD_MAX:
            break
    return (la2, lb2) if changed else (la, lb)


def _line_split_explained(ita, list_a, h_a: float, itb, list_b, h_b: float) -> bool:
    """这对「删除/新增」配成的 mod，是不是**行切分不对称**造成的假象。

    DOC-1003 实测（用户反馈之外、清单里肉眼可见的一处假 mod）：
      · 旧版整页 OCR 把压力表表头读成**两行**：`NP Nominal Pressure` /
        `RBP Rated Burst Pressure`；
      · 新版读成**一行**：`ND Nominal Pressure RBD Rated Burst Pressure`
        （`NP`→`ND`、`RBP`→`RBD` 是 OCR 抖动，两版印的字一模一样）；
      · 配对时「旧一行 ↔ 新一行」相似度 0.552，刚过 `SCAN_LINE_MOD_RATIO`(0.55)
        → 报成一条 mod，`_scan_mod_confirm` 高分重读也推不翻（新版那一行本来就
        多出半句 `NP Nominal Pressure`）。
    判据：**本侧这一行 + 本侧紧邻的另一行** 拼起来 ≈ 对面那一行 —— 说明两版
    内容相同，只是切行方式不同。

    ⚠️ 只跟**纵向紧邻**（|Δ中心 y| ≤ `SCAN_PAIR_DY`×1.5）的行拼，且**不要求**
    横向同列 —— 被并掉的两行本来就一左一右（表头两栏）。
    ⚠️ 两道闸门（少一道就把真改动撤了）：
      ① 两行**本来就**读成同一句（`_near_same(na, nb)`）→ 不走这条；
      ② 邻居本身要够长（≥ `NEAR_SPLIT_PEER_MIN` 字符）—— 拼一个 2 字符的邻居
         谁都能把 ratio 顶过 0.8。
    ⚠️ 拼接结果仍过 `_near_same`（短串必须全等、长串 ratio ≥ 0.8 且长度差 ≤ 10%）
    —— 真改动（`14 atm`→`12 atm`、`Balloon Diameter`→`Conical Balloon Diameter`）
    拼不出对面那句，不会被撤。
    """
    na, nb = _norm_cjk(ita["t"]), _norm_cjk(itb["t"])
    if not na or not nb:
        return False
    if _near_same(na, nb):
        # ⚠️ 两行**本来就**读成「同一句」时不许走这条（2026-10-01 实测打回来的）：
        #    UDI 号那对 `…48425 0…` ↔ `…48429 8…` 是**真 mod**（只差两个字符、
        #    ratio 0.955 → `_near_same` 为真），拿它去和别人拼接，
        #    比例照样 ≥0.8 → 真改动被撤。行切分不对称的前提是「两行本来不像」。
        return False

    def _peers(base, lines, h_pt, nbase, target):
        cy = (float(base["y0"]) + float(base["y1"])) / 2.0 * h_pt
        for p in lines:
            if p is base:
                continue
            np_ = _norm_cjk(p.get("t", ""))
            if len(np_) < NEAR_SPLIT_PEER_MIN:
                continue                       # 太短的邻居拼了也不算「被并掉的一行」
            if abs((float(p["y0"]) + float(p["y1"])) / 2.0 * h_pt - cy) > \
                    1.5 * SCAN_PAIR_DY:
                continue                       # 纵向不紧邻 → 不是「被并掉」的那一行
            if _near_same(nbase + np_, target) or _near_same(np_ + nbase, target):
                return True
        return False

    if _peers(ita, list_a, h_a, na, nb):
        return True
    return _peers(itb, list_b, h_b, nb, na)


def _scan_page_line_regions(pa, pb, oa, ob, i: int, scale: float,
                            old_path: str, new_path: str
                            ) -> Tuple[List[Dict[str, Any]], bool]:



    """扫描件候选的**第二条路线**：两版各整页 OCR 一遍，行级文本对拍。

    为什么需要它（DOC-1003 实测）：两张**不同产品**的标签，差异是**区域级**的
    （整张表格、整块小标签区、整段多语言行），行级差异画像聚出的窗是一个
    363pt 高的巨窗，`_scan_seed_rects` 在巨窗里只出 1 个横跨整页的候选、
    一条都判不出 —— 像素候选路线在「大面积不同」的文件上结构性失效。
    而行对拍根本不依赖像素对齐：每版的行框来自**各版自己的 OCR**，
    内容漂移 1.6% 也无所谓（DOC-1002 那套配准/画像只留给「同版式小改」）。

    对拍规则（与 locate() 字符流引擎同思路，行粒度）：
    ① **全局抵消**：归一化后在两版出现次数相同的行直接消掉（位置无关）；
    ② 剩余行里「只有一侧有」的 → 删除/新增候选；
    ③ 一删一增且归一化相似的 → **行内修改**（mod），再过 `_scan_mod_confirm`
       高分辨率复核（120~200dpi 的 OCR 抖动在 300dpi 下会现形）；
    ④ 剩余单侧行若能在**对面全文**里按序找到（coverage ≥ `SCAN_MOVE_COVER`）
       → 挪位，撤掉（对应字符流引擎的 `_drop_moved`）。

    返回 `(regions, ok)`；`ok=False` = 这条路线不可用（引擎缺失/没认出几行），
    上层退回像素管线。
    """
    dpi = SCAN_LINES_DPI
    la = _page_ocr_lines(oa, old_path, i, dpi)
    lb = _page_ocr_lines(ob, new_path, i, dpi)
    # ⓪.0 竖排补扫（见 `_rot90_supplement`）：整页 OCR 对旋转 90° 排版的行
    #      整列读丢（DOC-1003 顶部尺寸条两版全漏）。转 90° 再读一遍，行框映射
    #      回原页面后**并进行表**——两版都读出的竖排行会在抵消步自然消掉，
    # ⓪.0 竖排补扫（见 `_rot90_supplement`）：整页 OCR 对旋转 90° 排版的行
    #      整列读丢（DOC-1003 顶部尺寸条两版全漏）。转 90° 再读一遍，行框映射
    #      回原页面后**并进行表**——两版都读出的竖排行会在抵消步自然消掉，
    #      只有一版有的（`150`→`300`、多 `Conical`）就成了增删候选。
    #      ⚠️ 用 `+` 拼（别 `+=`）：`_page_ocr_lines` 返回的是缓存列表本体，
    #      原地扩展会让缓存在下次调用时重复叠加。
    #      ⚠️ 只留**新文本**：Vision 连旋转过的水平文字都认得（实测），补扫会把
    #      整页水平行重读一遍 —— 不按归一化文本去重的话，同一物理行多出 2~3 份
    #      候选，mod 配对成倍重复（DOC-1003 PDF 对 31→88 条实测）。
    def _dedup_rot(base: List[Dict[str, Any]], sup: List[Dict[str, Any]]
                   ) -> List[Dict[str, Any]]:
        seen = {_norm_cjk(it.get("t") or "") for it in base}
        out: List[Dict[str, Any]] = []
        for it in sup:
            n = _norm_cjk(it.get("t") or "")
            if not n or n in seen:
                continue
            # 位置去重：Vision 连旋转过的水平文字都认得，重读还会读出**乱码版**
            # （归一化不同、文本去重挡不住），但映射回原页面后**位置与原行重叠**
            # （DOC-1003 芬兰语行被重读成 `paoaajeeo…` 0.646 假 mod 实测）。
            # 与任一未旋转行重叠 ≥60% 的补扫行整条丢。
            ix0, iy0, ix1, iy1 = (float(it["x0"]), float(it["y0"]),
                                  float(it["x1"]), float(it["y1"]))
            dup = False
            for b in base:
                bx0, by0 = float(b["x0"]), float(b["y0"])
                bx1, by1 = float(b["x1"]), float(b["y1"])
                if _overlap_frac((ix0, iy0, ix1, iy1), (bx0, by0, bx1, by1)) >= 0.6:
                    dup = True
                    break
            if dup:
                continue
            seen.add(n)
            out.append(it)
        return out

    la = la + _dedup_rot(la, _rot90_supplement(oa, old_path, i, dpi))
    lb = lb + _dedup_rot(lb, _rot90_supplement(ob, new_path, i, dpi))
    if not la or not lb or len(la) < 3 or len(lb) < 3:
        return [], False
    ca = Counter(_norm_cjk(it["t"]) for it in la if _norm_cjk(it["t"]))
    cb = Counter(_norm_cjk(it["t"]) for it in lb if _norm_cjk(it["t"]))

    hay_a = "\n".join(_norm_cjk(it["t"]) for it in la)
    hay_b = "\n".join(_norm_cjk(it["t"]) for it in lb)

    def leftover(lines, mine, other, other_lines, other_hay,
                 w_self, h_self, w_other, h_other):
        """没被抵消的行；能在对面**找到相似行**的（挪位/重排）收进 `moved`。

        返回 `(out, moved)`：
          · `out` —— 保留的行（进入增删/修改判定）；
          · `moved` —— 被判定为「纯位移」的行，每项 `(本侧行, 对面最像行)`。
            ⚠️ 2026-10-12 位移标蓝需求：纯位移不再静默丢弃，而是转成 moved
            （浅蓝）区域输出 —— 用户反馈「标太少」，内容没变只是平移的行该标蓝。
            ⚠️ 判据必须**严格**（内容一字不差）：`ratio ≥ SCAN_MOVE_SAME`。
            不能用 `_scan_line_moved` 的宽泛 True —— 它会把「真改动但
            `_mod_worthy` 拦不住」的行（条码数字 48425→48429，ratio 0.944、
            token 错位）也判成挪位，误标蓝 = 假一致，比漏标严重得多。

        ⚠️ 位置守卫（2026-10-11）：对面**原地**就有一行「相似但不全同」的
        （`_same_pos_mod_partner`）→ 这行是行内修改候选，**不许**按挪位撤 ——
        底部 UDI 数字行 50 字只差两个数字，ratio 0.956 过挪位线、
        `_mod_worthy` 又被 token 错位拒掉，两道闸叠加真改动就消失了。
        """
        quota = {n: min(mine[n], other.get(n, 0)) for n in mine}
        used: Counter = Counter()
        out = []
        moved = []

        def _has_inplace_copy(it, w_s, h_s, w_o, h_o):
            """对面**原地**是否已有同文本行（中心 y 差 ≤ SCAN_INPLACE_DY）。

            ⚠️ 2026-10-12 22vs23 假位移：同一版面上下各有一块相似内容
            （`3.0mm×80mm 150cm` 上下两块产品标签），计数抵消后残余的那份
            会被 `_scan_line_best` 配到**对面另一块**上，跨出 80pt 的假位移。
            对面原地就有同文本 → 这份是「重复计数残余」，不是位移。
            """
            n = _norm_cjk(it.get("t", ""))
            if not n:
                return False
            cy = (float(it["y0"]) + float(it["y1"])) / 2.0 * h_s
            for ot in other_lines:
                if _norm_cjk(ot.get("t", "")) != n:
                    continue
                if abs((float(ot["y0"]) + float(ot["y1"])) / 2.0 * h_o - cy) \
                        <= SCAN_INPLACE_DY:
                    return True        # 原地就有（x 差异不管：同文本同高即可）
            return False

        for it in lines:
            n = _norm_cjk(it["t"])
            if not n:
                continue
            if used[n] < quota.get(n, 0):
                used[n] += 1                      # 这一份与对面配对抵消
                continue
            if not _same_pos_mod_partner(it, w_self, h_self,
                                         other_lines, w_other, h_other):
                _br, _braw, brow = _scan_line_best(n, other_lines)
                # —— 纯位移（内容一字不差）→ 转蓝；其余挪位/重排仍静默撤 ——
                # ⚠️ 前提：对面**原地**没有同文本行（否则是重复计数残余，
                #    会像 22vs23 那样把上下两块相似内容跨配成假位移）
                if _br >= SCAN_MOVE_SAME and brow is not None \
                        and not _has_inplace_copy(it, w_self, h_self,
                                                  w_other, h_other):
                    moved.append((it, brow))
                    continue
                if _scan_line_moved(n, it["t"], other_lines, other_hay):
                    continue                       # 挪位/重排：对面找得到相似行
            out.append(it)
        return out, moved

    W_pt0, H_pt0 = float(oa.rect.width), float(oa.rect.height)
    W2_pt0, H2_pt0 = float(ob.rect.width), float(ob.rect.height)
    rem_a, moved_a = leftover(la, ca, cb, lb, hay_b, W_pt0, H_pt0, W2_pt0, H2_pt0)
    rem_b, moved_b = leftover(lb, cb, ca, la, hay_a, W2_pt0, H2_pt0, W_pt0, H_pt0)

    # ⓪ 条带补读（2026-10-01 加，见 SCAN_REREAD_RULES）：整页 OCR 对旋转小字
    #    条带读得不稳且**两版不对称**（旧版整条漏、新版出碎片），单侧候选全是
    #    假象。对候选所在条带两版各 400dpi 补读一次，读全了再重新对拍。
    if rem_a or rem_b:
        la2, lb2 = _reread_bands(oa, ob, old_path, new_path, i, la, lb, rem_a, rem_b)
        if (la2, lb2) != (la, lb):
            la, lb = la2, lb2
            ca = Counter(_norm_cjk(it["t"]) for it in la if _norm_cjk(it["t"]))
            cb = Counter(_norm_cjk(it["t"]) for it in lb if _norm_cjk(it["t"]))
            hay_a = "\n".join(_norm_cjk(it["t"]) for it in la)
            hay_b = "\n".join(_norm_cjk(it["t"]) for it in lb)
            rem_a, moved_a = leftover(la, ca, cb, lb, hay_b, W_pt0, H_pt0, W2_pt0, H2_pt0)
            rem_b, moved_b = leftover(lb, cb, ca, la, hay_a, W2_pt0, H2_pt0, W_pt0, H_pt0)

    # ⓪.5 全页高清兜底对拍（见 SCAN_HDPI_RULES）：条带补读只救「单侧读出」的
    #     洞；两版对称漏读（REF 列 200dpi 全空）连洞都形不成，这里独立兜底。
    if SCAN_HDPI_DPI:
        la_h = _page_ocr_lines(oa, old_path, i, SCAN_HDPI_DPI)
        lb_h = _page_ocr_lines(ob, new_path, i, SCAN_HDPI_DPI)
        if (la_h and lb_h
                and len(la_h) >= SCAN_HDPI_MIN_LINES
                and len(lb_h) >= SCAN_HDPI_MIN_LINES):
            ca_h = Counter(_norm_cjk(it["t"]) for it in la_h if _norm_cjk(it["t"]))
            cb_h = Counter(_norm_cjk(it["t"]) for it in lb_h if _norm_cjk(it["t"]))
            hay_a_h = "\n".join(_norm_cjk(it["t"]) for it in la_h)
            hay_b_h = "\n".join(_norm_cjk(it["t"]) for it in lb_h)
            rem_a_h = [it for it in leftover(la_h, ca_h, cb_h, lb_h, hay_b_h,
                                             W_pt0, H_pt0, W2_pt0, H2_pt0)[0]
                       if len(_norm_cjk(it["t"])) > SCAN_HDPI_MIN_CHARS]
            rem_b_h = [it for it in leftover(lb_h, cb_h, ca_h, la_h, hay_a_h,
                                             W2_pt0, H2_pt0, W_pt0, H_pt0)[0]
                       if len(_norm_cjk(it["t"])) > SCAN_HDPI_MIN_CHARS]
            W0, H0 = oa.rect.width, oa.rect.height
            W1, H1 = ob.rect.width, ob.rect.height

            def _cen(it, w, h):
                return ((float(it["x0"]) + float(it["x1"])) / 2.0 * w,
                        (float(it["y0"]) + float(it["y1"])) / 2.0 * h)

            base = [(it, W0, H0) for it in rem_a] + [(it, W1, H1) for it in rem_b]

            def _is_dup(it, w, h):
                cx, cy = _cen(it, w, h)
                for b, bw, bh in base:
                    bx, by = _cen(b, bw, bh)
                    if abs(bx - cx) <= SCAN_HDPI_DUP_XY and abs(by - cy) <= SCAN_HDPI_DUP_XY:
                        return True
                return False

            add_a = [dict(it, h=1) for it in rem_a_h if not _is_dup(it, W0, H0)]
            base += [(it, W0, H0) for it in add_a]
            add_b = [dict(it, h=1) for it in rem_b_h if not _is_dup(it, W1, H1)]
            rem_a += add_a
            rem_b += add_b

    if not rem_a and not rem_b:
        return [], True

    W_pt, H_pt = oa.rect.width, oa.rect.height
    H2_pt = ob.rect.height

    def _pair_x_ok(ita, w_a, itb, w_b) -> bool:
        """两行算不算**同一列**：中心够近，或横向重叠够多（`SCAN_PAIR_DX/OV`）。"""
        ax0, ax1 = float(ita["x0"]) * w_a, float(ita["x1"]) * w_a
        bx0, bx1 = float(itb["x0"]) * w_b, float(itb["x1"]) * w_b
        if abs((bx0 + bx1) / 2 - (ax0 + ax1) / 2) <= SCAN_PAIR_DX:
            return True
        return _x_overlap_frac(ax0, ax1, bx0, bx1) >= SCAN_PAIR_OV

    def to_rect(it, h_pt, w_pt):
        x0 = max(0.0, it["x0"] * w_pt - GRAPHIC_PAD)
        x1 = min(w_pt, it["x1"] * w_pt + GRAPHIC_PAD)
        y0 = max(0.0, it["y0"] * h_pt - GRAPHIC_PAD)
        y1 = min(h_pt, it["y1"] * h_pt + GRAPHIC_PAD)
        return (x0, y0, x1, y1)

    def y_center(it, h_pt):
        return (float(it["y0"]) + float(it["y1"])) / 2.0 * h_pt

    def x_center(it, w_pt):
        return (float(it["x0"]) + float(it["x1"])) / 2.0 * w_pt

    out: List[Dict[str, Any]] = []
    used_a: Set[int] = set()
    used_b: Set[int] = set()
    # ③′ 数字主键预配对（2026-10-11 DOC-1003 用户拍板）：表格**整表下移一行**
    #    （新表多 `Dist./Prox.` 表头）时所有行隔 ~38pt，DY(20pt) 闸门够不着；
    #    `14(RBP)`↔`12 (RBP)` 反而只隔 13pt 先配上 —— 用户语义是「14 行删了、
    #    12 加了 RBP」。行首数字**同列唯一**（'12' 在两侧该列各只出现一次，
    #    右侧 `12 atm` 不同列不算）→ 不看 DY 直接锁配；行距内的普通配对仍走
    #    下面 ③ 的比例闸（数字唯一是强证据，不会误锁）。
    def _col_lead_count(it, lines, w_pt: float, h_pt: float) -> int:
        ka = _lead_num(_norm_cjk(it["t"]))
        ax0, ax1 = float(it["x0"]) * w_pt, float(it["x1"]) * w_pt
        cnt = 0
        for o in lines:
            if _lead_num(_norm_cjk(o["t"])) != ka:
                continue
            bx0, bx1 = float(o["x0"]) * w_pt, float(o["x1"]) * w_pt
            if _x_overlap_frac(ax0, ax1, bx0, bx1) >= SCAN_PAIR_OV:
                cnt += 1
        return cnt

    pre_pairs: List[Tuple[int, int]] = []
    for ia, ita in enumerate(rem_a):
        ka = _lead_num(_norm_cjk(ita["t"]))
        if not ka or ia in used_a:
            continue
        ya = y_center(ita, H_pt)
        hit = -1
        for jb, itb in enumerate(rem_b):
            if jb in used_b or _lead_num(_norm_cjk(itb["t"])) != ka:
                continue
            if abs(y_center(itb, H2_pt) - ya) > 6.0 * SCAN_PAIR_DY:
                continue                     # 同页内放宽，跨半页的不许配
            if not _pair_x_ok(ita, W_pt, itb, ob.rect.width):
                continue
            if hit >= 0:
                hit = -2                     # 不止一个候选 = 主键不唯一，放弃
                break
            hit = jb
        if hit >= 0 and _col_lead_count(ita, rem_a, W_pt, H_pt) == 1 \
                and _col_lead_count(rem_b[hit], rem_b, ob.rect.width, H2_pt) == 1:
            pre_pairs.append((ia, hit))
            used_a.add(ia)
            used_b.add(hit)

    def _mk_mod(ita, itb) -> None:
        rect_a = to_rect(ita, H_pt, W_pt)
        rect_b = to_rect(itb, H2_pt, ob.rect.width)
        # 两侧行框并集当标注矩形（内容在两版位置有漂移；region 只有一个
        # rect（对齐 A 坐标系），另带 rect_a/rect_b 给高分复核分开裁）
        rect = (min(rect_a[0], rect_b[0]), min(rect_a[1], rect_b[1]),
                max(rect_a[2], rect_b[2]), max(rect_a[3], rect_b[3]))
        rg = _mk_region(i, rect, "mod",
                        ita["t"].strip() + "\x00" + itb["t"].strip())
        rg["rect_a"] = rect_a
        rg["rect_b"] = rect_b
        # 行对拍的 OCR 分辨率有限（200dpi），mod 先过高分复核再报
        if _scan_mod_confirm(oa, ob, i, rect_a, old_path, new_path,
                             rect_b=rect_b):
            _narrow_mod_region(rg, oa, ob)   # 整行框 → 只圈真正变了的片段
            out.append(rg)

    for ia, jb in pre_pairs:
        _mk_mod(rem_a[ia], rem_b[jb])
    # ③ 一删一增相似 → mod
    # ⚠️ 2026-10-01 三处实测打回来的配对规矩：
    #    · 只按 ratio 贪心会**跨区错配**（`150mm` 配到隔 33pt 的 `6.5/7.0 mm`、
    #      `1400` 配到页顶的 `200`）→ 先卡 `SCAN_PAIR_DY` / `_pair_x_ok` 同区同列；
    #    · 按 rem_a 的**顺序**贪心会让弱配对抢走强配对的对象（A 的 `12` 先抢掉
    #      `12 (RBP)`，真改动 `14(RBP)→12 (RBP)` 反而落单）→ 先攒候选、
    #      再按 ratio **降序**全局配对；
    #    · **两轮配对**（v39 实测）：高清兜底行（h 标记）先不参与 —— 否则 400dpi
    #      读出的整行 UDI（更长、ratio 更高）会把 200dpi 已配好的 REF mod 的
    #      对面抢走，REF 落单后走撤除链被 `_near_same`（等长数字替换 ratio 0.95
    #      过线）当同一句撤掉 —— v37 时代报得出的改动反而消失。
    cands_200: List[Tuple[float, int, int]] = []
    cands_h: List[Tuple[float, int, int]] = []
    num_reserved_b: Set[int] = set()
    for ia, ita in enumerate(rem_a):
        na = _norm_cjk(ita["t"])
        if not na:
            continue
        ya = y_center(ita, H_pt)
        for jb, itb in enumerate(rem_b):
            nb = _norm_cjk(itb["t"])
            if not nb:
                continue
            if abs(y_center(itb, H2_pt) - ya) > SCAN_PAIR_DY:
                continue
            if not _pair_x_ok(ita, W_pt, itb, ob.rect.width):
                continue
            r = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
            # 数字主键优先（SCAN_NUM_PAIR_BOOST 注释）：`12`↔`12 (RBP)` 0.44
            # 抬到 0.80，压过 `14(RBP)`↔`12 (RBP)` 的 0.77 —— 表格按行首数字对齐。
            if r < SCAN_NUM_PAIR_BOOST:
                la_n, lb_n = _lead_num(na), _lead_num(nb)
                if la_n and la_n == lb_n:
                    r = SCAN_NUM_PAIR_BOOST
            if r >= SCAN_LINE_MOD_RATIO:
                (cands_h if (ita.get("h") or itb.get("h"))
                 else cands_200).append((r, ia, jb))
                # ⚠️ 数字主键预约（SCAN_NUM_PAIR_BOOST 注释）：200dpi 轮里已构成
                #    「行首数字相同」配对的对面行，**不许**让 h 轮的弱相似对抢走 ——
                #    `12`↔`12 (RBP)`(0.80) 是用户语义，`14(RBP)`↔`12 (RBP)`(0.77)
                #    不是（DOC-1003 实测：h 轮先跑，`12 (RBP)` 被抢，表格错配）。
                if not (ita.get("h") or itb.get("h")) \
                        and _lead_num(na) and _lead_num(na) == _lead_num(nb):
                    num_reserved_b.add(jb)
    for cands in (cands_200, cands_h):
        cands.sort(key=lambda c: -c[0])
        for r, ia, jb in cands:
            if ia in used_a or jb in used_b:
                continue
            if cands is cands_h and jb in num_reserved_b:
                continue          # 这行留给 200dpi 的数字主键配对
            ita, itb = rem_a[ia], rem_b[jb]
            if _line_split_explained(ita, rem_a, H_pt, itb, rem_b, H2_pt):
                # 行切分不对称（见函数注释）：不是两版内容不同，是同一段字
                # 一版读成两行、另一版读成一行 —— 配出来的 mod 是假象。
                # 两侧都记 used，防它们再去配别的行。
                used_a.add(ia)
                used_b.add(jb)
                continue
            used_a.add(ia)
            used_b.add(jb)
            rect_a = to_rect(ita, H_pt, W_pt)
            rect_b = to_rect(itb, H2_pt, ob.rect.width)
            # 两侧行框并集当标注矩形（内容在两版位置有漂移；region 只有一个
            # rect（对齐 A 坐标系），另带 rect_a/rect_b 给高分复核分开裁）
            rect = (min(rect_a[0], rect_b[0]), min(rect_a[1], rect_b[1]),
                    max(rect_a[2], rect_b[2]), max(rect_a[3], rect_b[3]))
            rg = _mk_region(i, rect, "mod",
                            ita["t"].strip() + "\x00" + itb["t"].strip())
            rg["rect_a"] = rect_a
            rg["rect_b"] = rect_b
            # 行对拍的 OCR 分辨率有限（200dpi），mod 先过高分复核再报
            if _scan_mod_confirm(oa, ob, i, rect_a, old_path, new_path,
                                 rect_b=rect_b):
                _narrow_mod_region(rg, oa, ob)   # 整行框 → 只圈真正变了的片段
                out.append(rg)
    for ia, ita in enumerate(rem_a):
        if ia in used_a:
            continue
        na = _norm_cjk(ita["t"])
        if not na:
            continue
        # 配不上相似的对面行 = 这一侧整行没有了 → 删除
        # （对面其实有、只是 OCR 读岔的情况已被上面挪位检查撤掉）
        # 最后一道：对面同位置高分重读认得出这行 = 内容还在（单侧乱码/漏读），撤
        rect_a = to_rect(ita, H_pt, W_pt)
        if ita.get("h"):
            # 高清单侧行（SCAN_HDPI_RULES「裁切精读定案」）：全页 400dpi 只有
            # 旧版读出 ≠ 新版页面上没有 —— 全页 OCR 对旋转小字也会整列读丢。
            # 两版各裁切 400dpi 精读一次，读出同一句（_same_text_noise，与
            # mod 宽读对账同口径）= 单侧漏读假象，撤；读出不同 = 行内修改。
            # ⚠️ 两版窗口必须**同规格**（都走 _hdpi_probe_rect）：self 用裸行框、
            #    other 用 pad 窗时，old 侧窄条只读得出半句（`A35-070`），
            #    new 侧读出整句（`REF A35-070 300-200`）—— mod 文本没法看。
            rs = _hdpi_probe_rect(rect_a, float(oa.rect.width), float(oa.rect.height))
            t_self = _ocr_crop(oa, rs, dpi=SCAN_HDPI_DPI,
                               cache_key=(old_path, i, "hself")
                               + tuple(int(v * 10) for v in rs))
            rb = _hdpi_probe_rect(rect_a, float(ob.rect.width), float(ob.rect.height))
            t_oth = _ocr_crop(ob, rb, dpi=SCAN_HDPI_DPI,
                              cache_key=(new_path, i, "hoth")
                              + tuple(int(v * 10) for v in rb))
            if _norm_cjk(t_oth):
                if _same_text_noise(_norm_cjk(t_self), _norm_cjk(t_oth)):
                    continue
                # 结构对账（SCAN_HDPI_RULES「结构对账」）：乱码抖动区的文本对账
                # 永远「不同」，页面长得一样就不许报（绝不凭空造不一致）。
                if _px_struct_dist(oa, ob, rs) <= SCAN_HDPI_STRUCT_MAX:
                    continue
                rg = _mk_region(i, _hdpi_union_box(
                    oa, ob, old_path, new_path, i, rs, rb,
                    float(oa.rect.width), float(oa.rect.height),
                    float(ob.rect.width), float(ob.rect.height)), "mod",
                                t_self.strip() + "\x00" + t_oth.strip())
                rg["rect_a"] = rect_a
                rg["rect_b"] = (rb.x0, rb.y0, rb.x1, rb.y1)
                _narrow_mod_region(rg, oa, ob)   # 整行框 → 只圈真正变了的片段
                out.append(rg)
                continue
            if _line_near_check(oa, ob, rect_a, old_path, new_path, i,
                                text=ita["t"]):
                continue
            out.append(_mk_region(i, rect_a, "old",
                                  ita["t"].strip()))   # 旧版有新版没 = 删除
            continue
        if _line_near_check(oa, ob, rect_a, old_path, new_path, i,
                            text=ita["t"]):
            continue
        out.append(_mk_region(i, rect_a, "old",
                              ita["t"].strip()))   # 旧版有新版没 = 删除
    for jb, itb in enumerate(rem_b):
        if jb in used_b:
            continue
        nb = _norm_cjk(itb["t"])
        if nb and _scan_line_moved(nb, itb["t"], la):
            continue                              # 挪进来的，撤
        # 对面同位置高分重读认得出这行 = 内容两版都有（单侧乱码 / 单侧漏读），撤
        rect_b = to_rect(itb, H2_pt, ob.rect.width)
        if itb.get("h"):
            # ⚠️ 两版窗口必须**同规格**（都走 _hdpi_probe_rect）：理由见上面
            #    rem_a 侧的同名注释 —— 窗口不对称会让 mod 文本只读出半句。
            rs = _hdpi_probe_rect(rect_b, float(ob.rect.width), float(ob.rect.height))
            t_self = _ocr_crop(ob, rs, dpi=SCAN_HDPI_DPI,
                               cache_key=(new_path, i, "hself")
                               + tuple(int(v * 10) for v in rs))
            ra = _hdpi_probe_rect(rect_b, float(oa.rect.width), float(oa.rect.height))
            t_oth = _ocr_crop(oa, ra, dpi=SCAN_HDPI_DPI,
                              cache_key=(old_path, i, "hoth")
                              + tuple(int(v * 10) for v in ra))
            if _norm_cjk(t_oth):
                if _same_text_noise(_norm_cjk(t_self), _norm_cjk(t_oth)):
                    continue
                # 结构对账：同上，乱码抖动区的最后一道防线
                if _px_struct_dist(ob, oa, rs) <= SCAN_HDPI_STRUCT_MAX:
                    continue
                rg = _mk_region(i, _hdpi_union_box(
                    oa, ob, old_path, new_path, i, ra, rs,
                    float(oa.rect.width), float(oa.rect.height),
                    float(ob.rect.width), float(ob.rect.height)), "mod",
                                t_oth.strip() + "\x00" + t_self.strip())
                rg["rect_b"] = rect_b
                rg["rect_a"] = (ra.x0, ra.y0, ra.x1, ra.y1)
                _narrow_mod_region(rg, oa, ob)   # 整行框 → 只圈真正变了的片段
                out.append(rg)
                continue
            if _line_near_check(ob, oa, rect_b, new_path, old_path, i,
                                text=itb["t"]):
                continue
            out.append(_mk_region(i, rect_b, "new",
                                  itb["t"].strip()))   # 新版有旧版没 = 新增
            continue
        if _line_near_check(ob, oa, rect_b, new_path, old_path, i,
                            text=itb["t"]):
            continue
        out.append(_mk_region(i, rect_b, "new",
                              itb["t"].strip()))   # 新版有旧版没 = 新增

    # —— 位移出口（2026-10-12 位移标蓝需求）——
    # `leftover` 把「纯位移」的行收集进 moved_a/moved_b（旧侧/新侧各一份）。
    # 这里把两侧的挪位行**配对去重**后转成 moved 区域（浅蓝）。
    # 判据：内容在对面有**一字不差**的相同行（ratio ≥ SCAN_MOVE_SAME）=
    #   内容没变、只是位置平移。块内真改动仍走 mod/增删链路标红绿黄。
    # ⚠️ 三道守卫（防把真改动/碎片误标蓝）：
    #   ① 归一化后 ≥ 3 字符（`'5'`/`'mm'`/`'R'` 这类单字符碎片是 OCR 噪声）；
    #   ② 两侧行**同列**（中心 x 差 ≤ SCAN_PAIR_DX），跨半页的错配不算位移；
    #   ③ 位移量 ≥ 1pt（同一位置的「重复图层」不是位移）。
    if moved_a or moved_b:
        seen_move: Set[Tuple[str, str]] = set()
        for side, pairs, w_self, h_self, w_oth, h_oth in (
                ("old", moved_a, W_pt, H_pt, ob.rect.width, ob.rect.height),
                ("new", moved_b, ob.rect.width, ob.rect.height, W_pt, H_pt)):
            for it, brow in pairs:
                if brow is None:
                    continue                     # 对面没找到匹配行 → 不算位移
                t_self = _norm_cjk(it.get("t", ""))
                t_oth = _norm_cjk(brow.get("t", ""))
                if not t_self or not t_oth:
                    continue
                if len(t_self) < 3 or len(t_oth) < 3:
                    continue                     # 单字符碎片：OCR 噪声，不标蓝
                key = (t_self, side)
                if key in seen_move:
                    continue
                seen_move.add(key)
                rect_self = to_rect(it, h_self, w_self)
                rect_oth = to_rect(brow, h_oth, w_oth)
                # 同列守卫：中心 x 差过大 = 跨栏错配（条码数字行会被错误配到
                # 半页外的同文行），不算位移
                cx_self = (rect_self[0] + rect_self[2]) / 2.0
                cx_oth = (rect_oth[0] + rect_oth[2]) / 2.0
                if abs(cx_oth - cx_self) > SCAN_PAIR_DX:
                    continue
                dx = (rect_oth[0] - rect_self[0])
                dy = (rect_oth[1] - rect_self[1])
                # 位移量太小（< 1pt）≈ 同一位置 → 不是位移，跳过（避免把
                # 「重复图层/同一行」误标成蓝）
                if abs(dx) < 1.0 and abs(dy) < 1.0:
                    continue
                out.append(_mk_region(
                    i, rect_self, "moved",
                    it["t"].strip() + "\x00%.2f\x00%.2f\x00%s" % (dx, dy, side)))
    return out, True


def ocr_strip(page_idx: int, path: str, x0: float, x1: float, page) -> str:
    """OCR「某一栏的整条竖带」——按 (文件, 页, 栏) 缓存，避免同一条带来回识别。

    为什么要整条带：报「删除/新增」时，我们只知道**旧版**那一块在哪，
    不知道新版对应内容被重排到哪儿去了 —— 索性把同一条栏 OCR 一遍再找。
    """
    x0 = max(0.0, x0 - 6)
    x1 = min(float(page.rect.width), x1 + 6)
    if x1 - x0 < 60:                       # 太窄的带没意义，补宽一点
        mid = (x0 + x1) / 2
        x0, x1 = mid - 30, mid + 30
    key = (path, page_idx, int(x0 // 50), int(x1 // 50), 0)
    return _ocr_crop(page, fitz.Rect(x0, 5, x1, page.rect.height - 5),
                     cache_key=key)



def _ocr_band(page, rect, cache_key=None) -> str:
    """OCR「这一块所在的那条竖带」：横向范围照抄、纵向拉到整页。

    为什么必须整条带（实测打回来的一个误报）
    ----------------------------------------
    只比「同一矩形」时，**整段被推到上面或下面、挪得比这块本身还远**的情况就漏了 ——
    DOC-1007 的免责声明在新版整体下移约 72pt（超过这块本身的高度），同一矩形里
    当然比对不到，于是被误报成「这块新增了内容」。把整条栏 OCR 一遍，才知道那段字
    还在、只是挪了位置。
    """
    x0 = max(0.0, float(rect[0]) - 10)
    x1 = min(float(page.rect.width), float(rect[2]) + 10)
    return _ocr_crop(page, fitz.Rect(x0, 3, x1, page.rect.height - 3),
                     cache_key=cache_key)


def _ocr_near(page, ref, path: str, i: int, tag: str = "near") -> str:
    """OCR「对面这一块**周围**那一小片」——比整页竖带读得准得多。

    为什么不用整页竖带做逐行核对（2026-09-29 实测）
    ----------------------------------------------
    逐行复核时，needle 是**一行**（十几个字），对面那条竖带却是「整列从头到尾」——
    同一列里还混着别的段落、栏板，tesseract 在这条又高又窄的带上错字很多，
    真实覆盖度被压到 0.53~0.73，该撤的行撤不掉。
    改成「块上下各扩 `OCR_NEAR_PAD` 倍块高」这一小片后，实测区分度干净利落：
    真新增那一行 0.000、只是下移的两行 1.000 —— 中间空档极大。
    ⚠️ 必须用**块**矩形（`ref`）而不是行矩形来定范围：整段被推上去/推下来的距离
    可能比一行还大，按行取范围就找不到对面那行了。
    """
    h = max(8.0, float(ref[3]) - float(ref[1]))
    x0 = max(0.0, float(ref[0]) - 8.0)
    x1 = min(float(page.rect.width), float(ref[2]) + 8.0)
    y0 = max(0.0, float(ref[1]) - OCR_NEAR_PAD * h)
    y1 = min(float(page.rect.height), float(ref[3]) + OCR_NEAR_PAD * h)
    return _ocr_crop(page, fitz.Rect(x0, y0, x1, y1),
                     cache_key=(path, i, tag, int(x0 // 40), int(x1 // 40),
                                int(y0 // 40), int(y1 // 40)))


class _ShiftedPixmap:
    """`pb` 的纵向平移视图：`samples` 行已按 dy 表重排。

    只暴露下游用到的四个属性（width/height/stride/samples）——
    `_scan_windows` / `_scan_seed_rects` / `_decide_region` / `_refine_region`
    全都只碰这几个，真正的 `fitz.Pixmap` 没必要再造一份。
    """

    __slots__ = ("width", "height", "stride", "samples")

    def __init__(self, src, rows: bytes):
        self.width = src.width
        self.height = src.height
        self.stride = src.stride
        self.samples = rows


def _align_vertical(pa, pb):
    """两版位图**内容纵向漂移**的配准（DOC-1003 2MB vs 4MB 实测）。

    同一版式的两张标签，不同批次拍摄/扫描，内容放置位置有 ~1.6% 的纵向拉伸差
    （y=120px 处 dy=4、y=500px 处 dy=10，嵌入位图尺寸/页面矩阵完全相同）。
    不配准的后果：逐像素差异画像把**整页点亮**（热瓦片 69.9%、差异行连成
    5~510pt 一整段），行级聚窗失效，只出得了 4 个候选、漏检几十处。

    ⚠️ 逐带 SAD 直接估 dy 不行（第一版踩的坑）：这份语料是**两个不同产品**的
    标签，大面积内容不同的带里 SAD 没有对齐信号，估出的 dy 是乱值
    （实测 39 带里 -12、-9、7、11 什么都有），按它重排会把 B 撕碎。
    但真漂移是**线性**的（缩放差），所以分三步：
    ① 每带求 best dy，并算 dy=0 的 SAD 当参照 —— 只有 `best < TRUST × dy0`
       的带才可信（内容相同的带对齐后 SAD 显著下降；内容不同的带 best≈dy0）；
    ② 对可信带的 (y, dy) 做 RANSAC 式直线拟合（内点 = |dy − 线值| ≤ 1.2）；
    ③ 全页按拟合线赋 dy 重排。

    返回 `(对齐后的 pb 视图, 平均|dy|)`；可信带不足 6 个或拟合内点不足
    可信带的 40% 时**原样返回**（DOC-1002 同批次扫描走这条零开销路径）。
    """
    W, H = pa.width, pa.height
    if (pb.width, pb.height) != (W, H):
        return pb, 0.0
    A, B = pa.samples, pb.samples
    sa, sb = pa.stride, pb.stride
    band = SCAN_ALIGN_BAND_H
    n_bands = (H + band - 1) // band
    dys: List[int] = []
    trusted: List[Tuple[float, int]] = []     # (带中心 y, dy) 只收可信带
    for bi in range(n_bands):
        by0, by1 = bi * band, min(H, (bi + 1) * band)
        rows = range(by0, by1, 4)
        cols = range(0, W, SCAN_ALIGN_STEP)

        def band_sad(dy: int) -> int:
            if by0 + dy < 0 or by1 - 1 + dy >= H:
                return -1
            s = 0
            for y in rows:
                oa = y * sa
                ob = (y + dy) * sb
                for k in cols:
                    s += abs(A[oa + k] - B[ob + k])
            return s

        s0 = band_sad(0)
        best_s, best_d = None, 0
        for dy in range(-SCAN_ALIGN_DY, SCAN_ALIGN_DY + 1):
            if dy == 0:
                continue
            s = band_sad(dy)
            if s < 0:
                continue
            if best_s is None or s < best_s:
                best_s, best_d = s, dy
        dys.append(best_d if best_s is not None and (s0 < 0 or best_s < s0) else 0)
        if s0 > 0 and best_s is not None and best_s < SCAN_ALIGN_TRUST * s0:
            trusted.append((bi * band + band / 2.0, best_d))
    if len(trusted) < 6:
        return pb, float(sum(abs(d) for d in dys)) / max(1, n_bands)
    # RANSAC 线性拟合：dy(y) = a*y + b（只用可信带投票）
    best_model = None
    for ii in range(len(trusted)):
        for jj in range(ii + 1, len(trusted)):
            yi, di = trusted[ii]
            yj, dj = trusted[jj]
            if yj <= yi:
                continue
            a = (dj - di) / (yj - yi)
            if abs(a) > 0.05:
                continue              # 缩放差不会有 5% 这么大，这对样本必是离群
            b = di - a * yi
            inl = sum(1 for y, d in trusted if abs(d - (a * y + b)) <= 1.2)
            if best_model is None or inl > best_model[0]:
                best_model = (inl, a, b)
    if best_model is None or best_model[0] < 0.4 * len(trusted):
        return pb, float(sum(abs(d) for d in dys)) / max(1, n_bands)
    _inl, a, b = best_model
    sm = [int(round(a * (bi * band + band / 2.0) + b)) for bi in range(n_bands)]
    if all(d == 0 for d in sm):
        return pb, 0.0
    # 重排：B2[y] = B[y + dy(y)]
    rows = bytearray()
    for y in range(H):
        yb = min(H - 1, max(0, y + sm[y // band]))
        rows += B[yb * sb:yb * sb + sb]
    avg = sum(abs(d) for d in sm) / n_bands
    return _ShiftedPixmap(pb, bytes(rows)), avg


def _scan_windows(pa, pb, scale: float) -> List[Tuple[float, float]]:
    """纯扫描件：逐像素行统计差异，聚成候选 y 窗口（pt）。

    为什么不用 `_hot_tiles`（见 `GRAPHIC_SCAN_BANDS` 常量处的说明）：
    两次扫描的亚像素错位是**全页噪声**，瓦片法要么被噪声淹没要么漏掉小改动。
    行级画像 + 低门槛聚窗，宁可多给候选 —— 后面 `_decide_region` 的 OCR 判据
    会把噪声窗口一个个否掉。
    """
    W, H = pa.width, pa.height
    A, B = pa.samples, pb.samples
    runs: List[List[int]] = []
    cur: Optional[List[int]] = None
    gap = 0
    for y in range(H):
        a = A[y * W:(y + 1) * W]
        b = B[y * W:(y + 1) * W]
        n = sum(1 for p, q in zip(a, b) if abs(p - q) > GRAPHIC_DARK)
        if n >= SCAN_ROW_PX:
            if cur is None:
                cur = [y, y, 1]
            else:
                cur[1] = y
                cur[2] += 1
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > SCAN_GAP_ROWS:
                if cur[2] >= SCAN_MIN_ROWS:
                    runs.append(cur)
                cur = None
                gap = 0
    if cur is not None and cur[2] >= SCAN_MIN_ROWS:
        runs.append(cur)
    return [(r[0] / scale, (r[1] + 1) / scale) for r in runs]


def _scan_seed_rects(pa, pb, y0: float, y1: float,
                     scale: float) -> List[Tuple[float, float, float, float]]:
    """把一个差异窗口精修成**若干候选矩形**（每个 = 一簇差异所在的那一行）。

    为什么是「若干个」而不是一个种子（DOC-1002 页脚实测）：单个「差异最强列」
    当种子太脆 —— 粗体大字（`STERILE EO`）的扫描错位噪声在每列上积累的差异数
    比真改（`Rev02`→`Rev03`，一共才十几个像素）还高，种子一落错整窗报废。
    改成：差异列先**按竖直线列切开、按空隙聚簇**，每簇独立定行、独立扩展、
    独立判定 —— 真改簇和噪声簇各判各的，互不拖累。

    每个簇的矩形怎么定（三个坑都是实测出来的）：
    1. **x 不能用条带墨迹范围**：边框/表格框是通页竖线，任何条带的墨迹 x 都
       横跨整页 → OCR 混读一大片。所以从簇边缘沿「墨迹连续（字隙 ≤
       `SCAN_JOIN_GAP_PT`）」扩展，停在字间空隙大的地方。
    2. **竖直线列**（窗口 ± `SCAN_VLINE_CONTEXT_PT` 范围里墨迹占比 ≥
       `SCAN_VLINE_FRAC`）既当簇的分隔符、又在扩展掩膜里剔除 —— 否则扩展会
       顺着框线横穿整页。上下文必须远高于窗口本身，否则单行窗口里文字列
       自己就是 100% 有墨，会被误剔。
    3. **y 取该簇差异行**包住峰值的那段连续行，不能用整个窗口（相邻行噪声
       会把好几行并进一窗，整窗 OCR token 错位，`_mod_worthy` 全部拒掉）。
    4. **扩展掩膜只用改动行自身（±2 行）的墨，且一列 ≥2 行有墨才算墨、
       整行墨迹 > 50% 页宽的「结构行」（表格横线）不参与** —— 否则单像素噪点
       或横线搭桥，扩展一路冲到页框（实测一半矩形的 x1 都是 512.1）。
    """
    W = pa.width
    ry0 = max(0, int(y0 * scale) - 1)
    ry1 = min(pa.height, int((y1 + 1) * scale) + 1)
    iy0 = max(0, ry0 - int(SCAN_VLINE_CONTEXT_PT * scale))
    iy1 = min(pa.height, ry1 + int(SCAN_VLINE_CONTEXT_PT * scale))
    A, B = pa.samples, pb.samples

    def row_pairs(y):
        off_a = y * pa.stride
        off_b = y * pb.stride
        return A[off_a:off_a + W], B[off_b:off_b + W]

    col_diff = [0] * W
    diff_cols_of_row = {}
    for y in range(ry0, ry1):
        ra, rb = row_pairs(y)
        cols = [k for k in range(W) if abs(ra[k] - rb[k]) > GRAPHIC_DARK]
        if cols:
            diff_cols_of_row[y] = cols
            for k in cols:
                col_diff[k] += 1
    if not diff_cols_of_row:
        return []
    # 竖直线列（上下文范围）
    ink_n = [0] * W
    for pm in (pa, pb):
        for y in range(iy0, iy1):
            off = y * pm.stride
            for k, v in enumerate(pm.samples[off:off + W]):
                if v < SCAN_INK_DARK:
                    ink_n[k] += 1
    ctx_rows = max(1, iy1 - iy0)
    vline = [ink_n[k] >= SCAN_VLINE_FRAC * ctx_rows for k in range(W)]
    # 结构行（表格横线/框线：整行墨迹 > 50% 页宽）—— 只在窗口行里判
    struct_row = set()
    for y in diff_cols_of_row:
        off = y * pa.stride
        if sum(1 for v in pa.samples[off:off + W] if v < SCAN_INK_DARK) > 0.5 * W:
            struct_row.add(y)
    # x 簇：有差异的列按空隙聚簇（x 升序扫，跨行同簇自然合并），竖直线列当分隔符
    join = max(1, int(round(SCAN_JOIN_GAP_PT * scale)))
    runs: List[List[int]] = []          # [x0, x1, 差异像素总数]
    for k in range(W):
        if vline[k] or col_diff[k] <= 0:
            continue
        if runs and k - runs[-1][1] <= join:
            runs[-1][1] = k
            runs[-1][2] += col_diff[k]
        else:
            runs.append([k, k, col_diff[k]])
    runs = [r for r in runs if r[2] >= SCAN_RUN_MIN_PX]
    if not runs:
        return []
    runs.sort(key=lambda r: -r[2])
    runs = runs[:SCAN_MAX_RUNS]
    out: List[Tuple[float, float, float, float]] = []
    for cx0, cx1, _n in runs:
        # 该簇的差异行 → 每段连续行组各出一个候选（簇跨两行时上下各一个）
        hit = sorted(y for y, cols in diff_cols_of_row.items()
                     if any(cx0 <= k <= cx1 for k in cols))
        if not hit:
            continue
        groups = []
        for y in hit:
            if groups and y - groups[-1][1] <= SCAN_GAP_ROWS:
                groups[-1][1] = y
            else:
                groups.append([y, y])
        for g in groups:
            def _has_ink(pm, y, lo, hi):
                off = y * pm.stride
                return any(v < SCAN_INK_DARK for v in pm.samples[off + lo:off + hi + 1])

            # ① 先按改动行定 x（mask 用改动行自身的墨）
            ln0 = max(0, g[0] - 2)
            ln1 = min(pa.height, g[1] + 3)
            ink2 = [0] * W
            for pm in (pa, pb):
                for y in range(ln0, ln1):
                    if y in struct_row:
                        continue
                    off = y * pm.stride
                    for k, v in enumerate(pm.samples[off:off + W]):
                        if v < SCAN_INK_DARK:
                            ink2[k] += 1
            mask = bytearray(W)
            for k in range(W):
                mask[k] = 1 if (ink2[k] >= 2 and not vline[k]) else 0
            x0 = cx0
            gap = 0
            while x0 > 0 and gap <= join:
                x0 -= 1
                gap = gap + 1 if not mask[x0] else 0
            x1 = cx1
            gap = 0
            while x1 < W - 1 and gap <= join:
                x1 += 1
                gap = gap + 1 if not mask[x1] else 0
            # ② 再把 y 扩到字的完整高度：差异往往只落在数字中间几行，
            #    不扩就是 3~4pt 高的薄片 —— OCR 读半截字，乱码假报一堆
            #    （DOC-1002 实测 3 条假报全是薄片：`9707`→`1707`、表格头乱码）。
            y0p = g[0]
            for _ in range(int(SCAN_Y_EXPAND_PT * scale)):
                ny = y0p - 1
                if ny < 0 or not (_has_ink(pa, ny, x0, x1) or _has_ink(pb, ny, x0, x1)):
                    break
                y0p = ny
            y1p = g[1]
            for _ in range(int(SCAN_Y_EXPAND_PT * scale)):
                ny = y1p + 1
                if ny >= pa.height or not (_has_ink(pa, ny, x0, x1) or _has_ink(pb, ny, x0, x1)):
                    break
                y1p = ny
            out.append((max(0.0, x0 / scale - 1.5),
                        max(0.0, y0p / scale - 1.2),
                        (x1 + 1) / scale + 1.5,
                        (y1p + 1) / scale + 1.2))
    return out


SCAN_CONFIRM_DPI = 300      # 扫描件 mod 候选的高分辨率复核 DPI（低分辨率判的，
                            # 扫描噪声能把数字认岔出假「修改」，高分辨率再读一遍对账）

# 两侧同位置重读的**尝试阶梯**（2026-10-01 加第二轮，理由见 `_line_near_check`）
NEAR_CHECK_STEPS = [
    {"pad_x": 8.0, "pad_y": 0.8, "dpi": SCAN_CONFIRM_DPI},   # ① 贴合框，够用就撤
    {"pad_x": 12.0, "pad_y": 1.8, "dpi": 400},               # ② 放宽 + 提分辨率
]
NEAR_SUBSTR_MAX = 16        # 只对这么短（含）以内的候选做「在对面找得到」判定
NEAR_SUBSTR_RATIO = 0.5     # 短候选在对侧重读里的占比门槛（防 `Conical` 这类真新增被误撤）

# ③ 纵向重排扫描（2026-10-01 加，`NEAR_SHIFT_RULES`）
# 表格**加了一列/一行表头**后，整表会整体下移：DOC-1003 新版压力表多了
# 「Conical Balloon Diameter」表头行，同一格两版差了 ~19pt。而前面两轮重读只看
# **同一个 y**，挪过位的格子读到的永远是邻行的值 → 旧版明明印着 `8`，却被报成
# 「新版新增 `8`」（用户反馈的假新增）。
# 修法：短候选再沿**同一列**上下扫几个偏移，每个偏移用一个**窄窗**（纵向只外扩
# 5pt —— 宽了就会跨行把邻居的值读进来）重读；读出同一句 = 内容还在、只是行挪了。
# ⚠️ 只对短候选扫：长句在这么窄的窗里根本读不全，扫了只会拿到半截噪声。
NEAR_SHIFT_DY = (8.0, -8.0, 16.0, -16.0, 24.0, -24.0)
NEAR_SHIFT_PAD_X = 8.0      # 横向外扩（与 NEAR_CHECK_STEPS ① 同量级）
NEAR_SHIFT_PAD_Y = 5.0      # 纵向外扩：只盖住本行
NEAR_SHIFT_MAX_CHARS = 6    # 只对归一化后 ≤ 它的短候选做
NEAR_SHIFT_DPI = SCAN_CONFIRM_DPI

NEAR_SHIFT_RULES = [
    {"name": "行挪位", "cond": "对侧**同一列**、上下 ≤±24pt 的窄窗里读出同一句",
     "act": "撤", "why": "表格加列/加表头把整表推移了，内容本身没变"},
    {"name": "长候选不扫", "cond": f"归一化后 > {NEAR_SHIFT_MAX_CHARS} 字符", "act": "跳过",
     "why": "窄窗读不全长句，扫了只会拿到半截噪声"},
    {"name": "读出别的值", "cond": "窄窗里读到的不是同一句", "act": "保留",
     "why": "实测 `300` 扫不到旧版的 `150`、`1400` 扫不到新版的 `1200` —— 真改动不会被扫掉"},
]

# 字海兜底（`_hay_token_hit`）的容器长度上限，见该函数注释。
SCAN_MOVE_HAY_MAX_CONT = 8

# 行切分不对称（`_line_split_explained`，2026-10-01 加，DOC-1003 压力表表头假 mod）
# 同一段字，一版整页 OCR 读成两行、另一版读成一行 → 「旧一行 ↔ 新一行」相似度
# 刚过 `SCAN_LINE_MOD_RATIO` → 假 mod；而高分重读也推不翻（新那一行本来就多半句）。
# 判据 = 本侧一行 + 本侧**纵向紧邻**的另一行 拼起来 ≈ 对面那一行。
NEAR_SPLIT_PEER_MIN = 4     # 邻居归一化后至少这么长才算「被并掉的一行」（防短噪声顶高 ratio）

NEAR_SPLIT_RULES = [
    {"name": "本来就同句", "cond": "`_near_same(本行, 对面行)` 已成立", "act": "不走这条",
     "why": "前提是「两行本来不像」；本来就同句的是真 mod（UDI 号只差两字符）"},
    {"name": "邻居太短", "cond": f"邻居归一化后 < {NEAR_SPLIT_PEER_MIN} 字符", "act": "不拼",
     "why": "拼 2 个字符谁都能把 ratio 顶过 0.8，等于没有门槛"},
    {"name": "纵向不紧邻", "cond": "|Δ中心 y| > `SCAN_PAIR_DY`×1.5", "act": "不拼",
     "why": "被并掉的只能是挨着的那一行"},
    {"name": "拼接对得上", "cond": "本行+邻居（两种顺序）经 `_near_same` 命中对面行", "act": "撤",
     "why": "两版内容相同，只是切行方式不同"},
]

# 行级重读比对（`_near_line_hit`，2026-10-01 加，DOC-1003 `CE2797当4` 假新增）
# ①② 两轮重读都是拿**整个窗**的文本比；窗的纵向留白按候选行高的倍数给，
# 候选框一高（CE 标志 23.6pt）留白就 19pt，把上一行地址读进来 —— 两版那一行
# 的 OCR 抖动方向不同，整段必然判「不同」。候选自己那一行两侧却读得一字不差。
NEAR_LINE_OV = 0.4          # 对侧行框与本侧行框的重叠（占较小者面积）≥ 它算「同一行」
NEAR_LINE_IN_CAND = 0.3     # 本侧行框落在候选框里的比例 ≥ 它，这一行才归候选框管

NEAR_LINE_RULES = [
    {"name": "框外行不比", "cond": f"行框落在候选框内的比例 < {NEAR_LINE_IN_CAND}", "act": "跳过",
     "why": "只判候选框管的那些行；窗里所有行都拿来比等于把门槛降到「有一行相同就撤」"},
    {"name": "位置对不上", "cond": f"两行框重叠 < {NEAR_LINE_OV}", "act": "跳过",
     "why": "位置不同的两行本来就不该拿来比（那是挪位/邻列）"},
    {"name": "逐行同一句", "cond": "位置对得上且 `_near_same` 成立", "act": "撤",
     "why": "两侧这一行内容相同，是整窗重读被邻行污染造成的假增删"},
    {"name": "逐行不同", "cond": "`_near_same` 不成立", "act": "保留",
     "why": "`_near_same` 自带短串必须全等、长串长度差 ≤10% 的闸门，真改动不会被撤"},
]

# 「两侧重读出来的是不是同一句话」—— 按 `NEAR_SAME_RULES` 逐条过。
# ⚠️ 光看 ratio ≥ 0.8 会把**真改动**撤掉（2026-10-01 实测）：`3134` vs `31234`
#    只差一个字符，ratio 0.89 直接过线 —— 可那段 LOT 号确实少了一位数字。
#    短串上多/少一个字符就是另一回事，所以短串必须**归一化后完全相等**。
NEAR_SAME_RATIO = 0.8       # 长串：ratio ≥ 它就算同一句
NEAR_SAME_SHORT = 8         # ≤ 这么长的算「短串」→ 走完全相等那条
NEAR_SAME_LEN_FRAC = 0.10   # 长串：两侧长度差 ≤ 10%（+ 至少容 1 字符）才算同一句

NEAR_SAME_RULES = [
    {"name": "完全相等", "cond": "归一化后一字不差", "act": "撤",
     "why": "两版这里内容一样，是单侧 OCR 读岔"},
    {"name": "包含+纯字母尾巴", "cond": "一字是另一字的子串、多出的尾巴 ≤2 字符且**不含数字**",
     "act": "撤", "why": "尾部是邻字噪声（`DOC-1003` vs `DOC-1003 F`）；"
     "尾巴带数字就是真改动（`150` vs `1500`），不能撤"},
    {"name": "短串", "cond": f"长度 ≤ {NEAR_SAME_SHORT} 且不完全相等", "act": "**不撤**",
     "why": "短串上多/少一个字符就是真改动（`3134` vs `31234`）"},
    {"name": "长串相似", "cond": f"ratio ≥ {NEAR_SAME_RATIO} 且长度差 ≤ {NEAR_SAME_LEN_FRAC}",
     "act": "撤", "why": "长串上的 OCR 抖动，字不会平白多一串"},
    {"name": "其余", "cond": "更低的相似 / 长度差更大", "act": "不撤",
     "why": "证据不足 —— 宁可多报，不能把真改动撤掉"},
]


def _substring_alpha_tail(a: str, b: str) -> bool:
    """一句包含另一句、且「剩下的尾巴」是 ≤2 个非数字邻字噪声 → 算同一句。

    `_near_same` 与 `_same_text_noise` 共用。尾巴 = 去掉被包含的那句后**剩下的全部**
    （包含未必是前缀：`70mm` in `6570mm`，剩下的是头上的 `65` —— 头尾都要查）。
    """
    if not ((a in b or b in a) and abs(len(a) - len(b)) <= 2):
        return False
    rest = b.replace(a, "", 1) if a in b else a.replace(b, "", 1)
    return len(rest) <= 2 and not any(ch.isdigit() for ch in rest)


def _near_same(a: str, b: str) -> bool:
    """两侧同位置重读的归一化文本算不算「同一句」（见 `NEAR_SAME_RULES`）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    if _substring_alpha_tail(a, b):
        return True
    if len(a) <= NEAR_SAME_SHORT or len(b) <= NEAR_SAME_SHORT:
        return False                       # 短串：差一个字就是另一回事
    # ⚠️ 阈值卡这么死是实测逼出来的：旧版那行末尾多出 `7omm`（4 字符 / 27 字符长）
    #    —— 那是**真删除**，按 15% 的口子 4 ≤ 4.05 就过了线，真改动被撤掉。
    if abs(len(a) - len(b)) > max(1, NEAR_SAME_LEN_FRAC * max(len(a), len(b))):
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= NEAR_SAME_RATIO


def _same_text_noise(a: str, b: str) -> bool:
    """mod 宽读对账专用：只有「完全一致」或「包含 + 纯字母尾巴」算同一句。

    ⚠️ **不能**拿 `_near_same` 来判 mod —— mod 本身就是「高相似但不同」，
    ratio ≥ 0.8 那条会把真改动整个撤掉（v32 踩的坑：UDI 号只差一位数字、
    ratio 0.97，宽读对账直接判「同一句」→ UDI/规格 mod 全军覆没）。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return _substring_alpha_tail(a, b)
SCAN_RUN_MIN_PX = 8         # 一个 x 簇的总差异像素 ≥ 它才当候选（去孤立噪点簇）
SCAN_MAX_RUNS = 8           # 每个窗口最多出多少个候选簇（防病态窗口爆炸）
SCAN_Y_EXPAND_PT = 9.0      # 定完 x 后把 y 沿墨迹最多扩这么多 (pt)，凑齐字的完整高度

# —— 条带补读（2026-10-01 加，DOC-1003 顶部旋转条带）———————————————
# 整页 200dpi 行级 OCR 对**旋转的小字条带**读得不稳：旧版整条漏读、新版只读出
# 碎片（`NEX`/`•`）—— 两版不对称，于是「新版多的」全是假新增，而真改动
# （`7.0mm→6.5/7.0mm`、`150mm→300mm`）反而没行可配、漏报。
# 实测：**400dpi 宽裁切**两版都能读全（旧 `7.0mm/150mm/200cm`、
# 新 `Conical/6.5/ /7.0mm/300mm/200cm`）。所以：单侧候选聚簇 → 对簇所在的
# 条带两版各补读一次 → 并回行集重新对拍。
SCAN_REREAD_DPI = 400       # 补读分辨率（整页 200dpi 读不全的，裁宽了 400dpi 就全）
SCAN_REREAD_GAP = 45.0      # 簇内间距 (pt)：候选行框扩这么多还搭得上就归同簇
SCAN_REREAD_PAD_X = 60.0    # 条带横向外扩 —— 旋转字必须给**够宽**的裁切才认得出
SCAN_REREAD_PAD_Y = 18.0    # 条带纵向外扩 (pt)
SCAN_REREAD_MAX = 6         # 每页最多补读几个簇（防病态页拖死）

SCAN_REREAD_RULES = [
    {"name": "重复读出", "cond": "文本与已有行相同/互相包含且框重叠", "act": "丢",
     "why": "同一句话读了两遍，留着会把配对算重"},
    {"name": "读岔的碎片", "cond": "与已有行框重叠但文本不同", "act": "**丢已有行、留补读行**",
     "why": "补读是 400dpi，整页 200dpi 的碎片（`NEX`）是读岔的"},
    {"name": "新读出的行", "cond": "不与任何已有行重叠", "act": "并进行集",
     "why": "整页漏读的旋转条带文字，补读才让两版都有行可配"},
]

# ---- 全页高清兜底对拍（2026-10-01，SCAN_HDPI_RULES）----
# 条带补读只能救「**单侧**读出」的洞（对拍剩余项附近找不到对面候选）。
# DOC-1003 打回来一种它够不到的情况：整页 200dpi 对条带右端 REF 列
# （`A35-070 150-200` → `A35-070 300-200`，旋转小字）**两版一个字都没读出**
# —— 对拍两边都空 = 认为无差异，连「洞」都形不成。
# 修法：两版各再整页 400dpi 行级 OCR 一次（结果有缓存，第二次跑免费），
# **独立**对拍出差异行，按位置去重后带 `h` 标记并入剩余项。
# ⚠️ 高清行**不与** 200dpi 行混合对拍 —— 两版行切分不同，混合会互相顶行
# （2026-10-01 上午「半截 UDI 顶掉完整行」连环坑就是这么来的）。
# ⚠️ 全页 400dpi **也会**把整列读丢（本例新版连 `A35-070` 都没读出），而
# 裁切 400dpi 两版都读得全（实测 pad 16/8pt 就够）—— 所以带 `h` 标记的
# 单侧行不走「对面重读认得出就撤」的老链，而是**两版裁切精读**定案：
# 读出同一句 = 单侧漏读假象（撤）；读出不同 = 报 mod；对面读不出 = 删除/新增。
SCAN_HDPI_DPI = 400          # 兜底对拍的整页行级 OCR 分辨率
SCAN_HDPI_MIN_LINES = 3      # 高清读出的行数少于它 = 这版读崩了，放弃兜底
SCAN_HDPI_MIN_CHARS = 3      # 归一化后 ≤ 它的碎片不参与兜底
SCAN_HDPI_DUP_XY = 22.0      # 与 200dpi 剩余项判重的中心距 (pt)（≈1 个行高）
SCAN_HDPI_PROBE_PAD_X = 16.0 # 裁切精读的横向外扩 (pt) —— 竖字必须给够宽才认得出
SCAN_HDPI_PROBE_PAD_Y = 8.0  # 裁切精读的纵向外扩 (pt)；再大会把邻列读进来对不齐
SCAN_HDPI_PX_DPI = 300       # 结构对账（墨迹覆盖）的渲染分辨率
SCAN_HDPI_PX_INK = 128       # 灰度 < 它算「墨」（二值化的分界）
SCAN_HDPI_PX_TOL = 1         # 墨迹容差半径 (px)：扫描整体偏移 / 模糊 ≤ 它算「同一笔」
SCAN_HDPI_STRUCT_MAX = 0.04  # 「找不到对应墨迹」的墨量占比 ≤ 它 = 两版**画面内容相同**
SCAN_HDPI_STRUCT_PX = 600    # 裁切宽度上限：超过就按比例降 dpi（控耗时与内存）

SCAN_HDPI_RULES = [
    {"name": "碎片不兜底", "cond": "归一化后 ≤ SCAN_HDPI_MIN_CHARS 字符", "act": "跳过",
     "why": "两版 OCR 碎片（`ım` vs `m`）天然对不齐，兜底只会造假删除"},
    {"name": "位置重复", "cond": "与 200dpi 剩余项（**两侧都算**）中心距 ≤ SCAN_HDPI_DUP_XY",
     "act": "丢高清行",
     "why": "同一处改动只报一遍；高清行只补 200dpi 完全没读出的位置"},
    {"name": "裁切精读定案", "cond": "带 h 标记的单侧行", "act": "两版各裁切 400dpi 重读",
     "why": "全页 400dpi 也会整列读丢；只有裁切才读得全，读出不同才是真差异"},
    {"name": "结构对账", "cond": "精读文本不同，但两版渲染的**墨迹覆盖**互补不上（结构距离 ≤ SCAN_HDPI_STRUCT_MAX）",
     "act": "撤",
     "why": "图形符号区（DOC-1002 的 `┤ 80MM`）两版 OCR 各读各的乱码（`■ 1H 80MM` "
            "vs `H80MM`），文本对账永远「不同」；页面长什么样才是最终事实。"
            "⚠️ 判据不能是逐像素差、也不能是分块占用率，理由见 `_px_struct_dist`"},
    {"name": "并集框", "cond": "h 精读判定出的 mod", "act": "标注矩形取**两侧读出行的并集**",
     "why": "触发候选往往只是条带里的一行（旧版整页 400dpi 只认出 `A35-070`），"
            "文本却来自外扩后的整列窗（`REF`/`A35-070`/`150-200`）。拿触发行的窄框"
            "当矩形，高亮就只盖住中间一列 —— 用户看到的「`150-200` → `300-200` "
            "没标出来」就是这个"},
]


def _hdpi_read_box(page, path: str, i: int, probe, w_pt: float, h_pt: float):
    """精读窗里**实际读出**的行的外接框（一行都没读出就退回整个窗）。

    为什么要它：触发兜底的候选往往只是条带里的**一行**（旧版整页 400dpi 只认出
    `A35-070`），可外扩后的窗口会把整列都读进来（`REF`/`A35-070`/`150-200`）。
    拿触点那行的窄框当标注矩形，高亮就只盖住中间一列。
    """
    x0s, y0s, x1s, y1s = [], [], [], []
    for it in _ocr_crop_lines(page, probe, path, i, "hbox"):
        if not str(it.get("t", "")).strip():
            continue
        x0s.append(float(it["x0"]) * w_pt)
        x1s.append(float(it["x1"]) * w_pt)
        y0s.append(float(it["y0"]) * h_pt)
        y1s.append(float(it["y1"]) * h_pt)
    if not x0s:
        return (probe.x0, probe.y0, probe.x1, probe.y1)
    return (min(x0s), min(y0s), max(x1s), max(y1s))


def _hdpi_union_box(pa, pb, path_a: str, path_b: str, i: int,
                    probe_a, probe_b, wa: float, ha: float,
                    wb: float, hb: float):
    """两侧精读窗里读出的行**并集**（折算到 A 的坐标系，一页只有一个 rect）。"""
    ba = _hdpi_read_box(pa, path_a, i, probe_a, wa, ha)
    bb = _hdpi_read_box(pb, path_b, i, probe_b, wb, hb)
    sx, sy = wa / max(1e-6, wb), ha / max(1e-6, hb)
    bb = (bb[0] * sx, bb[1] * sy, bb[2] * sx, bb[3] * sy)
    return (min(ba[0], bb[0]), min(ba[1], bb[1]), max(ba[2], bb[2]), max(ba[3], bb[3]))


def _hdpi_probe_rect(rect, w_pt: float, h_pt: float):
    """裁切精读窗：行框外扩 `SCAN_HDPI_PROBE_PAD_*`，clip 到页面内。"""
    return fitz.Rect(max(0.0, float(rect[0]) - SCAN_HDPI_PROBE_PAD_X),
                     max(0.0, float(rect[1]) - SCAN_HDPI_PROBE_PAD_Y),
                     min(w_pt, float(rect[2]) + SCAN_HDPI_PROBE_PAD_X),
                     min(h_pt, float(rect[3]) + SCAN_HDPI_PROBE_PAD_Y))


def _px_struct_dist(pg_a, pg_b, rect, dpi: int = SCAN_HDPI_PX_DPI) -> float:
    """两版渲染同一 rect 的**墨迹分块占用率平均差**（∈[0,1]，越小越像）。

    OCR 乱码抖动的最终防线：图形符号区两版**画面完全一样**，但 OCR 每次读出的
    乱码都不同（`■ 1H 80MM` / `1 30MM` / `→ 80мм`）—— 文本对账在乱码上永远判
    「不同」，只有「页面到底长什么样」才算数。

    返回 `max(旧版没被新版覆盖的墨量比, 新版没被旧版覆盖的墨量比)` ∈ [0,1]：
    **每一笔都要在对面 `SCAN_HDPI_PX_TOL` 像素内找得到对应墨迹**，找不到的部分
    才算真差异。所以「整体偏移」「模糊」「重扫」都不影响它 —— 笔画还在原地附近。

    ⚠️ 为什么不用逐像素差（v42 实测打回来）
    --------------------------------------
    DOC-1002 的两版是**同一张标签的两次扫描**，嵌入图像的原生分辨率差了整整
    一倍（旧 4290×4630 / 新 2145×2315）。逐像素差把「新扫描更糊」的**每个笔画
    边缘**都算成差异 —— 该窗实测 5.6%（行框）/ 6.4%（probe 窗），远超 2% 阈值，
    于是 `┤ 80MM` 处的 OCR 乱码抖动被当成真改动报出来，违反「绝不凭空造不一致」。
    降采样（÷3 → 5.9%）与高斯模糊（6.0%）都压不下去：细笔画在 300dpi 下只有
    2~3 px 宽，边缘像素占比太高。

    ⚠️ 为什么也不用「分块墨迹占用率」（v43 第一版实测打回来）
    --------------------------------------------------------
    按 16×8 分块统计墨迹占比确实能把 3634 那条假 mod 压到 0.011（画面相同的
    条带真改动 0.062，看着能分）—— 但 DOC-1003 的**真**改动里有两处栽了：
      · `(01) … 48425 0 …` → `48429 8`（UDI 序列号）  → 0.0104  ← 被误撤
      · `REF A35-070150-200` → `300-200`（REF 号）      → 0.0105  ← 被误撤
    原因是这两窗都是**一整行小字**，改的只有 1~2 个字符，落到网格里只动一格
    的一小部分 —— 与「扫描变糊」的扰动同量级。**判据必须看「哪一笔没对上」，
    而不是「这一格墨多了还是少了」。**

    实测分离度（`SCAN_HDPI_PX_TOL = 1`）：

      · DOC-1002 假 mod（两版画面相同）            0.021
      · DOC-1003 UDI 序列号（真改动 2 字）          0.076
      · DOC-1003 REF 号（真改动 1 字）              0.077
      · DOC-1003 顶部条带（真改动，旋转小字）        0.428
      → 阈值 0.04 落在 0.021 与 0.076 之间的空档里。

    渲染失败 / 区域太小 → 返回 1.0（**不等于**「画面相同」）：宁可保留候选，
    也不凭空撤掉一条真差异。
    """
    try:
        r = fitz.Rect(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
        if r.is_empty or r.width <= 0 or r.height <= 0:
            return 1.0
        # 控开销：裁切超过 SCAN_HDPI_STRUCT_PX 宽就按比例降 dpi（判据是「笔画在不在
        # 附近」，与分辨率大小无关，降一点不影响结论）
        use_dpi = dpi
        est = r.width * dpi / 72.0
        if est > SCAN_HDPI_STRUCT_PX:
            use_dpi = max(96, int(dpi * SCAN_HDPI_STRUCT_PX / est))
        pa = pg_a.get_pixmap(clip=r, dpi=use_dpi, colorspace=fitz.csGRAY, annots=False)
        pb = pg_b.get_pixmap(clip=r, dpi=use_dpi, colorspace=fitz.csGRAY, annots=False)
        w, h = min(pa.width, pb.width), min(pa.height, pb.height)
        if w < 4 or h < 4:
            return 1.0
        ink = SCAN_HDPI_PX_INK
        ma = _ink_mask(pa, w, h, ink)
        mb = _ink_mask(pb, w, h, ink)
        na, nb = sum(map(sum, ma)), sum(map(sum, mb))
        if na == 0 and nb == 0:
            return 0.0                       # 两版都是空白 → 画面确实相同
        if na == 0 or nb == 0:
            return 1.0                       # 一边有墨一边全白 → 真差异，别撤
        return max(_uncovered_frac(ma, mb, w, h), _uncovered_frac(mb, ma, w, h))
    except Exception:
        return 1.0        # 渲染失败 ≠ 视觉相同：宁可保留候选也不凭空撤


def _ink_mask(pix, w: int, h: int, ink: int) -> List[List[int]]:
    """渲染位图 → 墨迹二值掩码（`1` = 墨）。"""
    s, st = pix.samples, pix.stride
    return [[1 if s[y * st + x] < ink else 0 for x in range(w)] for y in range(h)]


def _uncovered_frac(mine: List[List[int]], other: List[List[int]],
                    w: int, h: int, tol: int = SCAN_HDPI_PX_TOL) -> float:
    """`mine` 的墨迹里，在 `other` 的 `tol` 像素邻域内**找不到对应**的占比。

    邻域用可分离膨胀（先横后纵）算，纯 Python 也不用为每笔做距离变换。
    """
    total = sum(map(sum, mine))
    if total == 0:
        return 0.0
    # other 的可分离膨胀 → 每笔的「容差邻域」掩码
    row = [[0] * w for _ in range(h)]
    for y in range(h):
        oy = other[y]
        ty = row[y]
        for x in range(w):
            if oy[x]:
                for dx in range(-tol, tol + 1):
                    xx = x + dx
                    if 0 <= xx < w:
                        ty[xx] = 1
    near = [[0] * w for _ in range(h)]
    for y in range(h):
        ry = row[y]
        if not any(ry):
            continue
        for x in range(w):
            if ry[x]:
                for dy in range(-tol, tol + 1):
                    yy = y + dy
                    if 0 <= yy < h:
                        near[yy][x] = 1
    bad = 0
    for y in range(h):
        my, nr = mine[y], near[y]
        for x in range(w):
            if my[x] and not nr[x]:
                bad += 1
    return bad / float(total)


def _scan_mod_confirm(oa, ob, i: int, rect, old_path: str, new_path: str,
                      rect_b=None) -> bool:
    """扫描件的 mod 候选在**高分辨率**下复核：两版重新 OCR 后仍不同才算数。

    为什么要有这道（DOC-1002 实测）：120dpi 下表格行 `10 1000 2.96` 被噪声认成
    `1000 4000` 之类，token 级判据看到的是干净的「数字换数字」→ 假 mod。
    300dpi 重读一遍：噪声恢复成两版相同的字 → 归一化相等 → 推翻；真改
    （日期 / Rev 号）高分辨率只会读得更准 → 仍然不同 → 保留。
    复核侧认不出字时不推翻低分辨率的结论（证据不足 ≠ 推翻）。

    `rect_b`：行对拍管线给的**新侧行框**（内容有纵向漂移时两版同一行的位置
    不同，用同一个 rect 裁两版会切偏字；各裁各的才准）。
    """
    r = fitz.Rect(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    ck = tuple(int(v * 10) for v in rect)
    hi_old = _ocr_crop(oa, r, dpi=SCAN_CONFIRM_DPI,
                       cache_key=(old_path, i, "cfm") + ck)
    r2 = fitz.Rect(*[float(v) for v in rect_b]) if rect_b else r
    ck2 = tuple(int(v * 10) for v in r2) if rect_b else ck
    hi_new = _ocr_crop(ob, r2, dpi=SCAN_CONFIRM_DPI,
                       cache_key=(new_path, i, "cfm") + ck2)
    if not hi_old.strip() or not hi_new.strip():
        return True
    if _norm_cjk(hi_old) == _norm_cjk(hi_new):
        return False
    # 第二轮：框放宽再各读一次（`NEAR_CHECK_STEPS` 第②档）—— 行对拍的行框来自
    # 条带补读，可能**把一行裁半截**（DOC-1003 页脚 `DOC-1003` 被裁成
    # `DOC-3`/`DOC-3S` → 假 mod）。放宽到横 ±12pt / 纵 ±0.8 行高后两版都读出
    # 完整的 `DOC-1003` → `_near_same` 判同一句 → 推翻。宽读**不同**才保留。
    h = max(6.0, r.height)
    # ⚠️ 400dpi 宽框第二轮**只对单行框跑**（2026-10-19 性能实测，指令后补）：
    #   宽框是「上下各 +0.8 行高、左右各 +12pt」再按 400dpi 渲染，面积随块高暴涨：
    #     200x14pt 行框 → 1244x202 = 0.3Mpx，1.2s
    #     500x200pt 大块 → 2911x2485 = 7.2Mpx，6.8s
    #   而这一轮救的是「**行框把一行裁半截**」（DOC-1003 页脚 `DOC-3`/`DOC-3S`）——
    #   这种事只可能发生在单行框上，多行大块不存在「被裁半截」。
    #   实测代价：4 页样本 130.0s → 4.0s，报出区域 15 个**一个不差**（同内容扫描件
    #   的 11 个假报也一个不差）。所以超过单行高度就直接保留低分辨率结论 ——
    #   「证据不足 ≠ 推翻」，与下面 `not hi_old.strip()` 同一口径。
    if r.height > GRAPHIC_MOD_FALLBACK_MAX_H:
        return True
    r_w = fitz.Rect(max(0.0, r.x0 - 12.0), max(0.0, r.y0 - 0.8 * h),
                    min(float(oa.rect.width), r.x1 + 12.0),
                    min(float(oa.rect.height), r.y1 + 0.8 * h))
    r2_w = fitz.Rect(max(0.0, r2.x0 - 12.0), max(0.0, r2.y0 - 0.8 * h),
                     min(float(ob.rect.width), r2.x1 + 12.0),
                     min(float(ob.rect.height), r2.y1 + 0.8 * h))
    ckw = (int(r_w.x0 // 20), int(r_w.x1 // 20), int(r_w.y0 // 20),
           int(r_w.y1 // 20), 400)
    hi_old_w = _ocr_crop(oa, r_w, dpi=400, cache_key=(old_path, i, "cfw") + ckw)
    hi_new_w = _ocr_crop(ob, r2_w, dpi=400, cache_key=(new_path, i, "cfw") + ckw)
    if hi_old_w.strip() and hi_new_w.strip() and _same_text_noise(
            _norm_cjk(hi_old_w), _norm_cjk(hi_new_w)):
        return False
    return True


def _overlap_frac(r, s) -> float:
    """两矩形重叠面积 / 较小者的面积（0 = 不相交）。"""
    w = min(r[2], s[2]) - max(r[0], s[0])
    h = min(r[3], s[3]) - max(r[1], s[1])
    if w <= 0 or h <= 0:
        return 0.0
    a1, a2 = (r[2] - r[0]) * (r[3] - r[1]), (s[2] - s[0]) * (s[3] - s[1])
    return (w * h) / max(1e-9, min(a1, a2))


def _textlayer_word_rects(page) -> List[Any]:
    """一页**文字层**的词框列表（fitz Rect）。拿不到返回空表（= 不设防）。"""
    try:
        return [fitz.Rect(w[:4]) for w in page.get_text("words")]
    except Exception:
        return []


def _rect_hits_words(rect, words) -> bool:
    """矩形是否压在某个**文字层词框**上（重叠 ≥ 该词面积的 60%）。

    用于整页重绘兜底：标题栏等有文字层的区域已由字符流引擎逐字报过，
    OCR 对这些区域的重读只会添乱（同一行两版各认各的 → 假增删）。
    """
    try:
        r = fitz.Rect(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    except Exception:
        return False
    for wr in words or []:
        try:
            inter = r & wr
            if not inter.is_empty and inter.get_area() >= 0.6 * max(1e-9, wr.get_area()):
                return True
        except Exception:
            continue
    return False


def _digit_anchored(t_old: str, t_new: str) -> bool:
    """位图图面行级兜底的**数字锚点**验收：两版文本必须有数字差异。

    为什么（2026-10-13 晚，rev16 vs rev18 实测 117 条兜回里一大半是抖动）：
    位图被不同分辨率重新栅格化后，Vision 对**同一行**两版各认各的
    （`ProductA 一次性…` ↔ `ProductA -次性使用冠找动脉球簽扩*写管`），
    归一化不相等 → 全局抵消失败 → 成对假增删。与 `_digit_guard` 同一哲学：
    **数字是最可信的改动锚点**（有效期/批号/版本号/注册证号全是数字改动），
    纯字母差异在重栅格化位图上压倒性是 OCR 抖动 → 不报。
    代价：图面里**纯字母**的真改动会被漏掉 —— 记账里已写明「结论来自 OCR，
    仅供参考」，且这类改动人工对图一眼可见。
    """
    import re as _re
    da_ = set(_re.findall(r"\d+", _norm_cjk(t_old or "")))
    db_ = set(_re.findall(r"\d+", _norm_cjk(t_new or "")))
    return da_ != db_


def _x_overlap_frac(xa0, xa1, xb0, xb1) -> float:
    """两段 x 区间的重叠长度 / 较窄一段的宽度（0 = 不相交）。

    只有横向判据的配对/去重场景用（`_same_pos_mod_partner` / `_pair_x_ok` /
    `_col_lead_count`），语义与 `_overlap_frac` 的 x 分量一致，单独成函数省去伪造高度。
    """
    ov = min(xa1, xb1) - max(xa0, xb0)
    if ov <= 0:
        return 0.0
    return ov / max(1e-9, min(xa1 - xa0, xb1 - xb0))


def _scan_page_regions(pa, pb, oa, ob, i: int, scale: float,
                       old_path: str, new_path: str) -> List[Dict[str, Any]]:
    """纯扫描件（整页无文字层）的候选生成 + 判定，替代瓦片对拍那条路。

    **两条候选路线互补**（DOC-1003 实测出必须分两条）：
    ① **行对拍**（`_scan_page_line_regions`，先跑）：两版各整页 OCR、行级
       文本对拍 —— 不依赖像素对齐，「两张不同产品的标签」这种**区域级差异**
       （整表、整块小标签区）只有它抓得住；同时它对「同版式小改」（DOC-1002）
       也直接给出干净的行级结论。
    ② **像素画像**（`_scan_windows` → `_scan_seed_rects`，原有路线）：
       行对拍抓不到的**无文本差异**（条码、图形、图标）靠它 —— 差异行画像
       在「稀疏小改」上又快又准。
    两条路线的产出按矩形重叠去重（行对拍的文本结论更可信，优先保留）。
    """
    line_out, line_ok = [], False
    if GRAPHIC_SCAN_LINES:
        try:
            line_out, line_ok = _scan_page_line_regions(
                pa, pb, oa, ob, i, scale, old_path, new_path)
        except Exception:
            line_out, line_ok = [], False
    if not line_ok:
        # 引擎不可用 / 没认出几行 → 全靠像素管线（旧行为）
        line_out = []

    px_out: List[Dict[str, Any]] = []
    kept: List[Tuple[float, float, float, float]] = []
    # 两版内容有纵向漂移时先配准（DOC-1003 实测 1.6% 拉伸差把整页点亮），
    # 之后所有画像/判定都用对齐后的 B —— rect 全程在 A（旧版）坐标系。
    pb, _dy_avg = _align_vertical(pa, pb)
    for y0, y1 in _scan_windows(pa, pb, scale):
        rects = _scan_seed_rects(pa, pb, y0, y1, scale)
        for rect in rects:
            # 同一窗口里扩展后重叠的候选只判一次（同一簇上下行组扩到同一行时出现）
            if any(min(rect[2], r[2]) - max(rect[0], r[0]) > 0.6 * min(rect[2] - rect[0], r[2] - r[0])
                   and min(rect[3], r[3]) - max(rect[1], r[1]) > 0 for r in kept):
                continue
            kept.append(rect)
            # 行对拍已经覆盖这块（矩形重叠）→ 它的文本结论更可信，跳过像素判定
            if any(_overlap_frac(rect, rg["rect"]) > 0.5 for rg in line_out):
                continue
            side, txt = _decide_region(pa, pb, oa, ob, i, rect, scale,
                                       old_path, new_path)
            if side is None:
                continue
            if side == "mod":
                rows = _refine_region(pa, pb, oa, ob, i, rect, scale,
                                      old_path, new_path)
                for rg in rows:
                    if _scan_mod_confirm(oa, ob, i, rg["rect"], old_path, new_path):
                        px_out.append(rg)
                continue
            if side == "moved":
                # 位移（2026-10-12）：像素级证明只是挪位 —— **跳过**「两侧同位置
                # 重读」核查（挪过去的行在对面当然找得到同文，过核查必被撤）。
                px_out.append(_mk_region(i, rect, side, txt))
                continue
            else:
                # 单侧候选同样过「两侧同位置重读」—— 扫描噪声/漂移会把两版
                # **相同**的内容推成假新增（DOC-1003 `CE2797当4`：±10pt 重读
                # 两版一字不差，都是 `- 30°C CE2797 UK…`）。
                pg_s, pg_o = (oa, ob) if side == "old" else (ob, oa)
                p_s, p_o = (old_path, new_path) if side == "old" else (new_path, old_path)
                if _line_near_check(pg_s, pg_o, rect, p_s, p_o, i, text=txt):
                    continue
                px_out.append(_mk_region(i, rect, side, txt))
    # 像素候选里再滤一遍与行对拍重叠的（行对拍后补的候选块可能与像素块重叠）
    px_out = [rg for rg in px_out
              if not any(_overlap_frac(rg["rect"], lg["rect"]) > 0.5 for lg in line_out)]
    return line_out + px_out


def _fit_pixmap(page_b, pb, w: int, h: int, scale_a: float):
    """把 B 侧位图对齐到 A 侧尺寸 —— 图片输入的必修课（见 `SIZE_FIT_RULES`）。

    图片没有物理尺寸：同一份文档一次存 1430x1543、一次存 1430x1544，
    原来那句 `尺寸不同就 continue` 会让**整页一处都不查**还报「0 处差异」。
    这里按规则表先拉伸（比例差在 `SCAN_SIZE_TOL` 内才拉，防止把内容压变形），
    拉不动就返回 None，由调用方**记账 + 跳过**（绝不静默）。

    返回值 `(pixmap, scale_b)`：
      · 拉伸过 → 位图与 A 同尺寸，坐标系可以直接沿用 A 的 `scale_a`；
      · 没拉 → 原样，scale 也不变。
    """
    if pb.width == w and pb.height == h:
        return pb, scale_a
    sx, sy = w / max(1, pb.width), h / max(1, pb.height)
    if abs(sx - sy) / max(sx, sy) > SCAN_SIZE_TOL:
        return None, scale_a
    try:
        from PIL import Image                       # 有 PIL：双线性缩放，质量最好
        im = Image.frombytes("L", (pb.width, pb.height), pb.samples)
        im = im.resize((w, h), Image.BILINEAR)
        return fitz.Pixmap(fitz.csGRAY, w, h, im.tobytes(), False), scale_a
    except Exception:
        pass
    try:                                            # 没 PIL：用非等比矩阵重渲染一版
        r = page_b.rect
        if r.width > 0 and r.height > 0:
            pm = page_b.get_pixmap(matrix=fitz.Matrix(w / r.width, h / r.height),
                                   colorspace=fitz.csGRAY, annots=False)
            if (pm.width, pm.height) == (w, h):
                return pm, scale_a
    except Exception:
        pass
    return None, scale_a


def _global_page_offset(page_a, page_b,
                        dpi: int = GRAPHIC_DPI) -> Optional[Tuple[float, float]]:
    """估计整页**光栅级**偏移 (dx, dy)（pt）；无一致偏移返回 None。

    指令D 五-1 的轻量版（v68）：复印件/重扫描的 PDF 整页内容常带 1~2pt 的
    平移错位（DOC-M2001 复制 vs 原件实测）。这种错位会让 `_local_shift_overlap`
    把一切区域判成「moved」（v63/v64 回归：Storage 真改动被吞）、让行块 NCC
    全面跌落（召回候选泛滥）。做法：取页面上中下 3 个大块做模板匹配，
    ≥2 块位移一致（±1.5pt）且 NCC≥0.90 → 认定整页偏移，返回它；调用方据此
    跳过「像素级位移/召回」类判定（这类页面上它们只会产生噪声）。
    """
    import numpy as _np
    try:
        w = page_a.rect.width
        h = page_a.rect.height
        # 2 列 × 3 行探针（单栏版面 3 个、双栏版面 6 个全落在字区）
        probes = []
        for cx in (0.08, 0.58):
            for cy in (0.18, 0.43, 0.68):
                probes.append((w * cx, h * cy,
                               w * (cx + 0.34), h * (cy + 0.14)))
        votes: List[Tuple[int, int]] = []
        for (x0, y0, x1, y1) in probes:
            m = _row_template_match(page_a, page_b, (x0, y0, x1, y1),
                                    (x0, y0, x1, y1),
                                    win_px=(int(6 * dpi / 72.0),
                                            int(6 * dpi / 72.0)), dpi=dpi)
            if m is None or m[0] < 0.90:
                continue
            votes.append((int(round(m[1])), int(round(m[2]))))
        if len(votes) < 2:
            return None
        groups: Dict[Tuple[int, int], int] = {}
        for v in votes:
            for dv in votes:
                if abs(v[0] - dv[0]) <= int(1.5 * dpi / 72.0) and \
                   abs(v[1] - dv[1]) <= int(1.5 * dpi / 72.0):
                    groups[v] = groups.get(v, 0) + 1
        best_k, best_n = max(groups.items(), key=lambda kv: kv[1])
        if best_n >= 2 and (abs(best_k[0]) + abs(best_k[1])) > 0:
            return (best_k[0] * 72.0 / dpi, best_k[1] * 72.0 / dpi)
    except Exception:
        return None
    return None



def graphic_regions(old_path: str, new_path: str, dpi: int = GRAPHIC_DPI,
                    tile: int = GRAPHIC_TILE,
                    moved_shifts: Optional[Dict[int, List[Tuple[Tuple[float, float, float, float], float, float]]]] = None,
                    fast_mode: bool = False,
                    skip_pages=None,
                    on_page=None) -> List[Dict[str, Any]]:
    """找出**两侧都没有文字层、但画面内容不同**的区域。

    `skip_pages`（2026-10-29）：这些页**不参与比对**，一条结果都不出。
    拼接 PDF 里的扫描页由 `pdfdiff_preflight.assess()["skip_pages"]` 给出。
    ⚠️ 有跳页时**不读也不写** `GRAPHIC_CACHE`：缓存 key 里没有页号信息，
       存进去的「缺了几页」的结果会被下一次无跳页的对比当成完整结果拿去用。

    `on_page`（2026-10-29）：**逐页回调** `on_page(页号, 本页新增的条目)`，
    供界面「第一页比完就先标第一页」用（见 `diff_app5._handle_msg` 的 `part`）。
    ⚠️ 只在 `fast_mode=True` 时回调，因为只有那条路的产出是**逐页终稿**：
       快速模式出口不做 `_merge_paragraphs` / `_frag_suppress` /
       `_moved_rescue_regions` 三道整份后处理（见函数末尾），页与页之间
       也就没有任何互相改写。非快速模式有那三道后处理，逐页推出去的东西
       可能被后面改写（红框变浅蓝、碎框被并），推了反而误导，所以不推。
    ⚠️ 回调里抛异常会被吞掉（`try/except`）：**结果不许因为界面出错而变**。

    `fast_mode=True`（6.0 新增「只定位不认字」快速模式）
    ----------------------------------------------------
    只做「像素相减 → 并成矩形」，**不 OCR、不做模板匹配、不判增删位移**，
    直接返回 `{"page", "pending": True, "rect", "old_text": "", "new_text": ""}`。
    调用方把这些区域画成**紫色**（`KIND_PENDING`），语义是「这里有变化，
    但还没认字、也还没判是增还是删」。

    为什么需要它：转曲/图片类走完整链路时，慢在「把曲线认回文字」（OCR），
    而 OCR 对转曲字形的识别本身并不可靠。用户 2026-10-26 的要求是：
    「我不需要具体了解这个是增加了还是删减了，只要精准标出变化位置」。
    这条路径把「定位」和「认字」彻底拆开 —— 定位只要相减，秒级即可完成。

    ⚠️ 快速模式下**不读也不写** `GRAPHIC_CACHE`：它本来就快，且结果与
       完整模式不同（多了 pending 区、少了红绿判定），共用缓存会互相污染。

    参数 `moved_shifts`（2026-10-15 指令C 位移补偿）：`{页号: [(矩形, dx, dy), ...]}`，
    由文字层 `_extract_moved_rows` 已判定的 moved 块提供。这些块覆盖的瓦片直接加入
    skip —— 文字层已判「移动」（标蓝），像素层无需再报，避免「下移内容被 OCR 成
    新增」的噪声误报（实测 11vs12 邮编区、标识说明区×2 三块 OCR 噪声即由此而来）。
    真新增块（不在 moved 覆盖内）不受影响，照常比对报出。

    返回 `[{"page": 页号, "rect": (x0, y0, x1, y1), "old_text": str, "new_text": str}, ...]`
    （PyMuPDF 页面坐标，左上原点、pt）。

    `old_text` / `new_text` 是**OCR 认出的那一段字**，只有「有内容」的那一侧非空：
      · `old_text` 非空、`new_text` 为空 → 旧版有、新版空白 = **整块删除**
      · 反过来 → **整块新增**
    调用方按哪个非空就在哪一侧的页面上标橙 —— 别把两侧都标，空白那侧标了是凭空造错。

    为什么需要它（2026-09-29 实测出来的一个真漏洞）
    ----------------------------------------------
    DOC-1007 这类文件是**转曲**的：正文大量以**矢量曲线**绘制，另外再配一层可提取的
    隐形文字。凡是**隐形文字层缺行**的地方（新版中栏 y≈84~160 那段正是），
    「读文字来比对」的引擎**原理上读不到** —— 新版真新增的那条 bullet 一处都没标，
    用户看到的就是「标少了」。本节用**像素级对拍**把这层补上。

    ⚠️ 为什么按「差异像素**占比**」而不是「平均灰度差」（第一版实测打回来的）
    ------------------------------------------------------------------------
    这份 PDF 的外框细线会因重新排版整体位移零点几个像素：用平均灰度差时
    **每一行都报一片差异**（首版实测 p0 直接报 438 块，全是噪声）。
    细线位移只会让一条 24px 瓦片里的 2~4 列像素变化（占比 < 3%），
    而文字/图形真的改了是**成片**像素变化 —— 占比阈值正好把前者挡住。

    ⚠️ 为什么「两边都有字」也要管（第三版实测打回来的）
    ---------------------------------------------------
    前两版只报「一边基本空白」的区域 —— 但 DOC-1007 第三栏因为插入了新小节，
    旧版的 12.0/13.0 正文（转曲）位置上，新版摆的是「再折叠工具」小节（也转曲）：
    两边都有墨水、墨水比 0.5 上下 → 直接被过滤 → **整节新增一处都没标**，
    用户看到的就是「再折叠工具那一大栏全都没有绿」。
    第三版起：两边都有字的候选先 **OCR 两侧文本**，归一化后相同 → 挪位/重渲染，不报；
    不同 → 再做双向竖带挪位核查（旧侧的字在新版栏里找得到 = 挪走了，不报红；
    新侧的字在旧版栏里找得到 = 挪进来的，不报绿）—— 都找不到对面才报。
    报出的矩形最后做**相邻合并**（`_merge_adjacent`）—— 转曲文字无法逐字定位，
    与其给行尾一小块，不如把整段连成一个块（用户 2026-09-29 拍板）。
    """
    da, db = fitz.open(old_path), fitz.open(new_path)
    scale = dpi / 72.0
    n_pages = min(da.page_count, db.page_count)
    _skip = set(int(i) for i in (skip_pages or ()))
    out: List[Dict[str, Any]] = []
    LAST_SIZE_SKIPS.clear()          # 每次调用重新记账（不进缓存 key，跨进程确定）
    LAST_FULLPAGE_SKIPS.clear()
    LAST_FAST_ALIGN.clear()
    LAST_FAST_FULLPAGE.clear()
    LAST_FAST_CORR.clear()
    # ⚠️ 快速模式**不读写缓存**：结果结构与完整模式不同（pending 区 ≠ 红绿判定），
    #    共用 key 会互相污染；而它本来就是秒级，不需要缓存。
    # ⚠️ 有跳页时同样不读写：缓存 key（文件指纹+dpi+瓦片）里没有页号，
    #    存进去的「少了几页」的结果会被下一次无跳页的对比当成完整结果。
    cf = (None if (fast_mode or _skip)
          else _graphic_cache_file(old_path, new_path, dpi, tile,
                                   extra=_moved_sig(moved_shifts)))
    if cf and os.path.isfile(cf):
        try:
            with open(cf, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):               # v59+：{"regions", "fullpage"}
                LAST_FULLPAGE_SKIPS.extend(raw.get("fullpage", []))
                _cached = [dict(r, rect=tuple(r["rect"])) for r in raw.get("regions", [])]
                _guard_regions_pages(_cached, n_pages, "graphic_regions(cache)")
                return _cached
            # 旧格式（纯 list）：向后兼容，fullpage 记账在旧缓存里本就没有
            _cached = [dict(r, rect=tuple(r["rect"])) for r in raw]
            _guard_regions_pages(_cached, n_pages, "graphic_regions(cache)")
            return _cached
        except Exception:
            pass                                  # 缓存坏了就当没有，重算
    try:
        for i in range(min(da.page_count, db.page_count)):
            checkpoint()  # 中止检查点：页级（一页几千平方 pt 的渲染 + 上百次 OCR）
            _n0 = len(out)      # 本页起点：逐页回调只推这一页新产出的条目
            if i in _skip:
                # 这一页被判成扫描件（见 `pdfdiff_preflight._page_is_scan`）：
                # 不渲染、不出框。谁跳过了几页由调用方写进状态栏。
                continue
            pa = da[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)
            pb = db[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)
            if (pa.width, pa.height) != (pb.width, pb.height):
                # 图片/不同扫描仪：位图差一两像素是常态（见 SIZE_FIT_RULES）。
                # 先尝试拉伸对齐；拉不动才跳过 —— 但**必须记账**，
                # 否则界面上一片「0 处差异」，用户分不清「真没改」还是「根本没看」。
                pb, _ = _fit_pixmap(db[i], pb, pa.width, pa.height, scale)
                if pb is None:
                    LAST_SIZE_SKIPS.append({
                        "page": i,
                        "old": (round(da[i].rect.width, 1), round(da[i].rect.height, 1)),
                        "new": (round(db[i].rect.width, 1), round(db[i].rect.height, 1)),
                    })
                    continue
            skip = _text_tiles(da[i], scale, tile) | _text_tiles(db[i], scale, tile)
            # 位移补偿（2026-10-15 指令C）：文字层已判 moved 的块，其覆盖瓦片加入
            # skip，像素层不再比对（移动由文字层标蓝，无需像素层重复报/误报）。
            # ⚠️ 2026-11-02：加 `MOVE_SKIP_FRAC` 门槛。老口径「擦边就整块跳过」会把
            #    紧挨位移行的下一行一起牺牲掉 —— 11vs12 左栏新插的「一个再折叠工具」
            #    就是这么没的（moved 行 180~188 擦到瓦片行 187.2~201.6 的 0.8pt）。
            #    位移行自己那一行覆盖率≈0.5，照样跳过，误报面没有变大。
            if moved_shifts and i in moved_shifts:
                for (_mr, _dx, _dy) in moved_shifts[i]:
                    skip |= _rect_tiles([_mr], scale, tile, min_frac=MOVE_SKIP_FRAC)
            # ---- 快速模式（6.0）：「只定位不认字」----
            # 只做「逐瓦片相减 → 连通域并成矩形」，一个 OCR 都不跑、一次模板匹配
            # 都不做。产出统一带 `pending: True`，调用方画紫色（KIND_PENDING）。
            # ⚠️ 纯扫描件在这里也走瓦片相减（不进 `_scan_page_regions` 的整页
            #    OCR 行对拍）：用户要的就是「大概位置」，OCR 认字那一步整条省掉。
            if fast_mode:
                # 降噪：整页亚像素配准 + 预平滑（见 `_fast_denoise_pair` 的说明）。
                # 不做这一步时，「两次导出的同一张标签」会因为 0.6pt 的整体错位
                # 让每条笔画边缘都「变了」，最后并成一个覆盖大半页的巨框 ——
                # 用户原话「全局都标出来了」。配准掉的是错位，不是内容。
                _A, _B, _dn = _fast_denoise_pair(pa, pb, dpi)
                if _dn["applied"]:
                    LAST_FAST_ALIGN.append({"page": i,
                                            "dx": round(_dn["dx"], 2),
                                            "dy": round(_dn["dy"], 2),
                                            "angle": round(_dn["angle"], 3),
                                            "gain": round(_dn["gain"], 3)})
                # 再按**图片对象**逐块配准：DWG 图纸/标签的图面是重嵌入的位图，
                # 整页一个刚体变换按不住（实测缩放差 1.38% → 13.3% 的假差异）。
                _IMG_BOXES: List[Tuple[float, float, float, float]] = []
                if FAST_LOCAL_ALIGN:
                    for _pg in (da[i], db[i]):
                        try:
                            _IMG_BOXES.extend(tuple(it["bbox"])
                                              for it in _pg.get_image_info())
                        except Exception:
                            pass
                    if _IMG_BOXES:
                        _B, _il = _align_images(da[i], db[i], _A, _B, scale)
                        for _one in _il:
                            LAST_FAST_ALIGN.append({
                                "page": i, "image": _one["rect"],
                                "scale": _one["scale"],
                                "gain": round(_one["after"] / max(1e-6, _one["before"]), 3)})
                _hot_f = _hot_tiles(_A.tobytes(), _B.tobytes(), pa.width, pa.height,
                                    skip, tile, GRAPHIC_DARK, FAST_TILE_FRAC)
                # 粗框只是噪声门，真正的框在 `_tight_rects` 里按像素收紧：
                # 以前直接把瓦片边界当结果，用户实测「框住一大片、精度低」。
                _rough = _tiles_to_rects(_hot_f, tile, scale, min_tiles=1)
                _rects = _tight_rects(_GrayArr(_A), _GrayArr(_B), _rough, scale,
                                      min_px=FAST_MIN_AREA_PX)
                # 对应物过滤（6.0 第六/七轮）：「这块内容在对面的页面上还找不找得到」。
                # 找得到且对齐后没残差 → 只是挪了位置，归「位移浅蓝」（默认不显示）；
                # 找到了但还有真改动 → 框收缩到那一小块；找不到 → 整框照报。
                # 这是「整段重排每行都报」的正解，详见 `_corresp_filter` 的说明。
                _moved_pairs: List[Any] = []
                if _rects:
                    _rects, _moved_pairs = _corresp_filter(_A, _B, _rects, scale)
                # 混合页（这一页有文字层）：紫框只留「图片里」的和「小块」的。
                # 理由：文字和图框线由字符流引擎负责（红/绿/蓝），像素层再报一遍
                # 只会多出一圈贴着页边的框 —— 实测 DWG 图纸的外框整体挪了 7.7pt，
                # 于是上下左右各出一条整页长的紫带，用户看到就是「全是噪点」。
                # 小块（<`FAST_MIX_KEEP_COV` 页面）一律留：那是精确的点级改动，
                # 文字层未必看得见（隐形文字层错位、图形符号等）。
                if skip and _IMG_BOXES and (_rects or _moved_pairs):
                    _pw0, _ph0 = pa.width / scale, pa.height / scale
                    _lim = FAST_MIX_KEEP_COV * _pw0 * _ph0

                    def _mix_keep(_r):
                        _ra = max(0.0, _r[2] - _r[0]) * max(0.0, _r[3] - _r[1])
                        return _ra <= _lim or _hits_any(_r, _IMG_BOXES, 0.3)

                    _kept = [_r for _r in _rects if _mix_keep(_r)]
                    # 位移对同一把尺子：文字区的位移归字符流引擎（它本来就标浅蓝），
                    # 像素层再报一遍就重复了。
                    _moved_pairs = [(_ma, _mb) for (_ma, _mb) in _moved_pairs
                                    if _mix_keep(_ma)]
                    _rects = _kept
                if len(_rects) > FAST_MAX_RECTS:
                    # 一页几十个碎框 = 整页性差异（重扫/重绘），按行带并起来
                    _rects = _row_bands(_rects)
                # 面积仍铺满大半页 → 直接给一条「整页」，别让用户数几十条紫带
                _pw, _ph = pa.width / scale, pa.height / scale
                _cov = sum(max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1]) for r in _rects)
                _page = _pw * _ph
                # 只有**真的整页都变**才合成一条整页框（2026-10-26 第五轮改）：
                #   旧判据里还有一条「框 ≥12 条且面积 >12% 就算整页」，
                #   它把「一页里改了十几处」误当成「整页重绘」——实测转曲样本 22 条
                #   真实改动（表格整段重排）被压成一条满页紫框，等于什么都没说；
                #   Picture 那对 14 条真改动（条码放大、加了 2.0 / 2.55F）同样被压掉。
                #   现在只按**面积**判：盖住 65% 以上才算整页，其余照实一条条列出来。
                if _page > 0 and _cov > FAST_FULLPAGE_FRAC * _page:
                    LAST_FAST_FULLPAGE.append(
                        {"page": i, "cover": round(_cov / _page, 3),
                         "rects": len(_rects)})
                    _rects = [(0.0, 0.0, _pw, _ph)]
                for _fr in _rects:
                    out.append({"page": i, "pending": True, "rect": _fr,
                                "dir": _pending_dir(_A, _B, _fr, scale),
                                "old_text": "", "new_text": ""})
                # 纯位移（第七轮）：内容一字未改、只是挪了位置 → 归「位移浅蓝」
                # （`KIND_MOV`，界面默认不勾，勾上才画）。`shift_src="old"` 表示
                # `rect` 是旧侧框、`rect_shift` 是它在新侧的对应位置 —— 与
                # `_prepare` 里 moved 分支的口径一致，两侧同色浅蓝。
                for (_ma, _mb) in _moved_pairs:
                    out.append({"page": i, "moved": True, "shift_src": "old",
                                "rect": _ma, "rect_shift": _mb,
                                "old_text": "", "new_text": ""})
                # 逐页推给界面（见 `on_page` 说明）：快速模式这一页的产出
                # 就是终稿，后面三道整份后处理对 fast_mode 整段跳过，不会再改。
                if on_page is not None:
                    try:
                        on_page(i, out[_n0:])
                    except Exception:
                        pass        # 界面出错不许影响比对结果
                continue
            if not skip and GRAPHIC_SCAN_BANDS:
                # ---- 纯扫描件（整页无文字层）----
                # 瓦片对拍在这类文件上不可靠（见 GRAPHIC_SCAN_BANDS 常量处的
                # 说明）：候选改用行级差异画像生成，判定仍走同一套 _decide_region。
                out.extend(_scan_page_regions(pa, pb, da[i], db[i], i,
                                              scale, old_path, new_path))
                continue
            hot = _hot_tiles(pa.samples, pb.samples, pa.width, pa.height,
                             skip, tile, GRAPHIC_DARK, GRAPHIC_FRAC)
            # v48 「文字层错位」补查：skip 瓦片 = 文字层覆盖到的地方，本该交给
            # 字符流引擎。但文字层可能**与视觉渲染错位**（DOC-M2001 实测：隐形
            # 文字层整体下移约 50pt，视觉上无文字的转曲区被标成「有文字层」，
            # 「Store in a dry, dark place → dry, dark, cool place」整处漏）。
            # 补法：skip 瓦片里**像素差异确凿**（同一 FRAC/深阈值，不另设口径）
            # 的，单独聚矩形（单瓦片即可，真改动可能只落一格）后走**同一套**
            # `_decide_region` —— 判定权在 OCR 文本比对（OCR 读的是渲染画面，
            # 不受文字层错位影响），两版相同照旧不报；正常 PDF 的 skip 区只是
            # 字体渲染抖动，远低于 FRAC，连 OCR 都不触发，零新增。
            if skip:
                hot_skipped = _hot_tiles(pa.samples, pb.samples, pa.width, pa.height,
                                         set(), tile, GRAPHIC_DARK, GRAPHIC_FRAC) - hot
            else:
                hot_skipped = set()
            page_out: List[Dict[str, Any]] = []
            page_w_pt = pa.width / scale
            # v68（指令D 五-1 轻量版）：每页先估一次**整页光栅偏移** —— 复印件/
            # 重扫描常见 1~2pt 平移错位（DOC-M2001 实测）。偏移页上一切小位移
            # 都是光栅噪声：簇级位移检测（补查/普通两路）与 NCC 召回全部跳过，
            # 行级 OCR 对拍不受影响（不依赖像素对齐）。
            _global_off = _global_page_offset(da[i], db[i])
            for rect in _tiles_to_rects(hot_skipped, tile, scale, min_tiles=1):
                # 补查矩形可能只有一个瓦片宽（真改动只落一格），OCR 只能读到
                # 残词（'cool plac'）→ 证据不足必判 None。横向加宽到整行量级
                # 再判 —— 读到整行，行内扩写/小改的判据才有着力点。
                r2 = (max(0.0, rect[0] - GRAPHIC_RECHECK_PAD_X), rect[1],
                      min(page_w_pt, rect[2] + GRAPHIC_RECHECK_PAD_X), rect[3])
                side, txt = _decide_region(pa, pb, da[i], db[i], i, r2, scale,
                                           old_path, new_path)
                if side == "moved":
                    # 指令D 二-2：位移在补查路径也要收（2.0 供应方式区的行内
                    # 相对位移正是落在这里 —— 文字层词盖住瓦片 → skip → 补查）。
                    # ⚠️ 位移为 0 的「moved」= 原位重渲染，**不再直接丢弃**——
                    # v67 实测（11vs12）：`_local_shift_overlap` 对「标签不动、
                    # 正文右移」的行内相对位移锁死在标签上（位移≈0），直接丢会
                    # 把贮存/无菌/有效期整批漏掉。正确做法：位移不足时**回落到
                    # 簇级检测**（正文簇单独匹配才有 >2pt 位移），簇命中照常报；
                    # 簇也没命中才是真「原位重渲染」，维持不报。
                    _handled = False
                    try:
                        _t, _fx, _fy, _src = (txt.split("\x00", 3)
                                              if txt.count("\x00") == 3
                                              else ("", "0", "0", "old"))
                        if (float(_fx) ** 2 + float(_fy) ** 2) ** 0.5 >= TM_SHIFT_MIN_PT:
                            out.append(_mk_region(i, r2, side, txt))
                            _handled = True
                    except Exception:
                        pass
                    if not _handled and _global_off is None:
                        _hits = _cluster_shift_in_rect(da[i], db[i], r2)
                        if len(_hits) >= 2:
                            for (_oa, _na) in _hits:
                                out.append({"page": i, "moved": True,
                                            "rect": _oa, "rect_shift": _na,
                                            "old_text": "[转曲]",
                                            "new_text": "[转曲]",
                                            "shift_src": "old"})
                    continue
                if side in (None, "mod") and _global_off is None:
                    # 指令D 二-2 簇级位移检测：`_decide_region` 的**整体滑动**
                    # 模型对「标签不动、正文动」的行内相对位移失效（实测
                    # ov=0.587 不达标、整块 NCC 被错位拉低），判 None/mod 丢弃。
                    # 这里对该矩形做**簇级**模板匹配。
                    # v67（指令D 二-2 定稿）：改「命中簇数 ≥2 即逐簇报」——
                    # 旧版还要求「按位移向量 ±3pt 聚类后最大群 ≥2」，但行内相对
                    # 位移语义下**各行独立位移**（2.0 供应方式区实测 dx=5.3/14.2/
                    # 13.7/8.9 各不相同），一致性聚类会把合法命中整群打散 → 全区
                    # 漏报。命中本身已过 NCC≥0.50 + 位移>2pt + 簇宽≥6pt 三重门槛，
                    # 逐簇报即可，不再强制位移一致。
                    # v68：整页光栅偏移页跳过（见 `_global_page_offset`）。
                    _hits = _cluster_shift_in_rect(da[i], db[i], r2)
                    if len(_hits) >= 2:
                        for (_oa, _na) in _hits:
                            out.append({"page": i, "moved": True,
                                        "rect": _oa, "rect_shift": _na,
                                        "old_text": "[转曲]",
                                        "new_text": "[转曲]",
                                        "shift_src": "old"})
                        continue
                if side != "mod":
                    # ⚠️ 补查**只收 mod（行内小改/扩写）**：整块增删一律不报。
                    #   DOC-1007 实测（2026-10-12）：排版位移会把别处内容推进
                    #   「文字层错位」的 skip 区，判成 "new" 大块 → 宽矩形落清单
                    #    后，`_graphic_rescue_core` 的碎片撤除把坐标重叠的 9 条
                    #    真文字层条目当碎片吃掉（18 条掉成 9 条）。位移内容的
                    #    挪位核查（ext 三路）在这里拦不全 —— v47 本来也不报
                    #    这类块（被 skip 盖住），不收不构成覆盖退步。
                    continue
                # ⚠️ 细切也必须用加宽矩形 r2：窄矩形里 OCR 只见残词，
                #    行级复核同样证据不足 → 会被无声丢掉（本轮实测踩过）。
                out.extend(_refine_region(pa, pb, da[i], db[i], i, r2,
                                          scale, old_path, new_path))
            for rect in _tiles_to_rects(hot, tile, scale):
                # —— 整页图形重绘防护（2026-10-13，DWG-2000087 标签对）——
                # 一个 hot 连通域覆盖页面面积 ≥ GRAPHIC_FULLPAGE_FRAC = 整页图面
                # 被整体重绘（嵌入图片替换 + 转曲文字重排）。这种块对 OCR 来说
                # 没有可对齐的「对面参照」，判「新增」是必然且无信息量 —— 报出去
                # 就是横跨大半页的巨框。跳过并记账；该页文字层差异仍由字符流引擎报。
                # ⚠️ 只拦「大块」，局部转曲区（DOC-1007 的 6.4%）不受影响。
                pg_w = pa.width / scale
                pg_h = pa.height / scale
                r_w, r_h = rect[2] - rect[0], rect[3] - rect[1]
                if r_w * r_h >= GRAPHIC_FULLPAGE_FRAC * pg_w * pg_h:
                    # —— 行级 OCR 对拍兜底（2026-10-13 晚，rev16 vs rev18 实测）——
                    # 巨框不报没错，但里面可能藏着**局部真实改动**（有效期
                    # 2026→2027、条码数字、Rev16→18）。位图被不同分辨率重新
                    # 栅格化（2438×2644 vs 2469×2678）会把整图边缘全部点亮，
                    # 像素阈值分不开「重栅格化噪声」和「语义改动」；行对拍
                    # 不依赖像素对齐：两版各 OCR 一遍、逐行抵消，剩下的就是
                    # 真差异。直接复用扫描件那条久经实战的
                    # `_scan_page_line_regions`（含抵消/位移/行内修改复核），
                    # 只保留落在巨框里的行。
                    rescue: List[Dict[str, Any]] = []
                    # —— 单侧转曲线止损（2026-10-13，rev15 vs rev16 卡死 300s 的根治）——
                    # 行级 OCR 对拍兜底的前提是「两侧图面都是位图、文字层情况对等」
                    # （rev16 vs rev18：两侧词框都 > 0，词框过滤能挡掉字符流引擎
                    # 已报过的行）。但 rev15 是「全转曲线」——文字层词框为 0，
                    # 整页 OCR 补行后 137/375/158 行候选**全部**涌进
                    # `_scan_page_line_regions` 的 `_line_near_check`（每个候选
                    # 两侧各高清 OCR 1~2 轮），单页 104s、三页 300s+，GUI 主线程
                    # 同步等待 → 用户看到「卡死」。这类文件的文字差异
                    # `_mixed_ocr_fill_core` 已经整页 OCR 补行、由字符流引擎报出，
                    # graphic 层再跑一遍行级兜底是**重复且爆炸**的 —— 直接跳过。
                    _words_o = _textlayer_word_rects(da[i])
                    _words_n = _textlayer_word_rects(db[i])
                    if bool(_words_o) != bool(_words_n):
                        # 一侧纯转曲线（0 词框）、另一侧有文字层 → 跳过行级兜底，
                        # 但仍记账（这页确实发生了整页重绘，只是不在 graphic 层补报）。
                        LAST_FULLPAGE_SKIPS.append({
                            "page": i,
                            "rect": (round(rect[0], 1), round(rect[1], 1),
                                     round(rect[2], 1), round(rect[3], 1)),
                            "frac": round(r_w * r_h / (pg_w * pg_h), 2),
                            "rescued": 0,
                        })
                        continue
                    try:
                        _lr, _ok = _scan_page_line_regions(
                            pa, pb, da[i], db[i], i, scale, old_path, new_path)
                    except Exception:
                        _lr, _ok = [], False
                    if _ok:
                        # 文字层词框（每页取一次）：标题栏等有文字层的区域已由
                        # 字符流引擎逐字报过，OCR 重读只会添乱 → 落进词框的行丢弃。
                        for rg in _lr:
                            if _overlap_frac(rg["rect"], rect) < 0.5:
                                continue
                            # 同页去重：上一个巨框的兜底可能已经报过同一行
                            if any(g.get("page") == i
                                   and _overlap_frac(rg["rect"], g["rect"]) > 0.6
                                   for g in out):
                                continue
                            to, tn = (rg.get("old_text") or "").strip(), \
                                     (rg.get("new_text") or "").strip()
                            if rg.get("moved"):
                                rescue.append(rg)      # 位移区域内容未变，无抖动风险
                                continue
                            # ① 文字层词框过滤（mod 两侧行框各查各的）
                            ra = rg.get("rect_a") or rg["rect"]
                            rb = rg.get("rect_b") or rg["rect"]
                            if to and _rect_hits_words(ra, _words_o):
                                continue
                            if tn and _rect_hits_words(rb, _words_n):
                                continue
                            # ② 数字锚点验收（见 _digit_anchored）
                            if not _digit_anchored(to, tn):
                                continue
                            rescue.append(rg)
                    if rescue:
                        out.extend(rescue)
                    LAST_FULLPAGE_SKIPS.append({
                        "page": i,
                        "rect": (round(rect[0], 1), round(rect[1], 1),
                                 round(rect[2], 1), round(rect[3], 1)),
                        "frac": round(r_w * r_h / (pg_w * pg_h), 2),
                        "rescued": len(rescue),
                    })
                    continue
                side, txt = _decide_region(pa, pb, da[i], db[i], i, rect, scale,
                                           old_path, new_path)
                if side == "mod":
                    # 同位置的小改（日期 / 版本号 / 数字）：不进「增删」聚合
                    # （_merge_adjacent 按 old/new 二值设计），直接细切到行 ——
                    # 只留真差行，块内 OCR 抖动的行在行级判定里自然消失。
                    out.extend(_refine_region(pa, pb, da[i], db[i], i, rect,
                                              scale, old_path, new_path))
                    continue
                _force_line = False
                if side == "moved":
                    # 位移（2026-10-12）：**旁路**聚合/细切管线 ——
                    # `_merge_adjacent`/`_refine_region` 都是 old/new 二值设计，
                    # moved 进去会被重判、`moved` 标志丢失。
                    # v68：先验位移大小 —— ≈0 的「moved」是**原位重渲染**或整页
                    # 光栅错位（复印件）的假象（DOC-M2001 回归根因：v63/v64 的
                    # `_local_shift_overlap` 把 ~1pt 的复制错位当配准命中 → 一切
                    # 区域判 moved → Storage 真改动被吞）。位移不足 → 不当位移
                    # 处理，落到下面的行级对拍（ver62 行为，M4506 Storage 根治）。
                    try:
                        _t, _fx, _fy, _src = (txt.split("\x00", 3)
                                              if txt.count("\x00") == 3
                                              else ("", "0", "0", "old"))
                        _mv = (float(_fx) ** 2 + float(_fy) ** 2) ** 0.5
                    except Exception:
                        _mv = TM_SHIFT_MIN_PT
                    if _mv >= TM_SHIFT_MIN_PT:
                        out.append(_mk_region(i, rect, side, txt))
                        continue
                    _force_line = True
                if side is None or _force_line:
                    if side is None and _global_off is None:
                        # v67（指令D 二-2）：无文字层区域（纯转曲行，瓦片不在
                        # skip）的**行块级位移检测** —— 2.0 供应方式区「运输条件/
                        # 贮存」整行转曲、标签不动正文右移，_decide_region 整体
                        # 滑动模型判 None，_line_rescan 的行对拍也拆不出「移动」。
                        # 先做簇级模板匹配：命中簇（NCC≥0.50 + 位移>2pt +
                        # 簇宽≥6pt）逐簇报 moved 浅蓝。
                        # ⚠️ v68 门控：整页光栅偏移页（`_global_page_offset`
                        # 命中，如 DOC-M2001 复印件）**跳过** —— 那种页面上一切
                        # 小位移都是光栅错位噪声（v67 在 M4506 上实测产出 27 块
                        # 假 moved）。
                        # 这里用 ≥1 逐簇口径（与补查路径的 ≥2 不同）：热瓦片合并
                        # 矩形常常只罩住单行，单行只有 1 个位移簇（正文），≥2 会
                        # 重新卡死；且本分支矩形**不在文字层内**，不存在与字符流
                        # 判定打架的问题。
                        _hits = _cluster_shift_in_rect(da[i], db[i], rect)
                        if _hits:
                            for (_oa, _na) in _hits:
                                out.append({"page": i, "moved": True,
                                            "rect": _oa, "rect_shift": _na,
                                            "old_text": "[转曲]",
                                            "new_text": "[转曲]",
                                            "shift_src": "old"})
                            # ⚠️ 2026-11-02：这里原来是 `continue` —— 位移簇一命中，
                            #    这块就**判完了**，下面的行级复核轮不到。后果：
                            #    块里若同时有「新加的一行」+「被顶下去的两行」，
                            #    下移的行会被簇匹配认成位移（浅蓝，默认不显示），
                            #    新增那行既不在任何簇里、也没人再看它一眼，
                            #    整处**静默消失**。用户原话「老生常谈，经常少标一行」，
                            #    11vs12「内装 → 一个再折叠工具」就是这一种：
                            #    瓦片把三行并成一块 (42.2,186.2,145,217)，
                            #    块级判 None → 簇匹配把下行两行当位移 → 新增行丢掉。
                            #    修法：位移照报，但行级复核也要跑；行级判出的
                            #    新增/小改直接进结果（不进 page_out —— 那些行
                            #    已经被 `_merge_adjacent` 并过一次，再并一次会
                            #    和刚报的位移重叠）。
                            for rg in _line_rescan(pa, pb, da[i], db[i], i, rect,
                                                   scale, old_path, new_path):
                                if rg.get("old_text") or rg.get("moved"):
                                    continue      # 位移/旧侧行：已由上面或字符流引擎负责
                                out.append(rg)
                            continue
                    # ⚠️ 块级判否 ≠ 块内没有真改动：这一块可能同时含「一处真新增」
                    #    和「一大片只是被顶下去的旧行」，块级覆盖率会把新增淹没
                    #    （DOC-1007 左栏「一个再折叠工具」实测）。细切到行再判一次。
                    for rg in _line_rescan(pa, pb, da[i], db[i], i, rect,
                                           scale, old_path, new_path):
                        if rg.get("old_text") and rg.get("new_text"):
                            out.append(rg)    # 行内小改（mod）：旁路聚合管线 ——
                                              # _merge_adjacent/_refine_region 都是
                                              # old/new 二值设计，mod 进去会被重判丢失
                        else:
                            page_out.append(rg)
                    continue
                page_out.append(_mk_region(i, rect, side, txt))
            # ① 先按段落把相邻候选并成一块（转曲文字给不出逐字坐标）；
            #    传入文字层瓦片用于识别「栏间通道」，防止把相邻两栏并成跨栏巨框
            for rg in _merge_adjacent(page_out, tiles=skip, tile=tile,
                                      scale=scale, page_rows=pa.height // tile):
                # ② 再把这一块按「行」细切，只留真正变了的那几行（见 _refine_region）
                out.extend(_refine_region(pa, pb, da[i], db[i], i, rg["rect"],
                                          scale, old_path, new_path))
            # ③ 指令D 四（v69 收尾）：DOC-M2001 Storage 漏报的**真正根因**不是
            #    GRAPHIC_FRAC 门槛，而是 `_decide_region` 的 moved 位移下限缺失
            #    （v63/v64 的 `_local_shift_overlap` 把复印件的 ~1pt 错位当配准
            #    命中 → 判 moved → Storage 真改动被吞）。已在 `_decide_region`
            #    内三处 moved return 加 TM_SHIFT_MIN_PT 下限守卫根治。
            #    原 v68 的 `_tm_recall_rects` 全页行块扫描召回通道经实测：
            #    ① 在 Storage 场景没发挥任何作用（根因在别处）；② 每页多出
            #    数百次模板匹配，把 11vs12 图形层从 59s 拖到 116s（DWG 超时是
            #    敏感点）。故**移除**该通道，两级召回由「_decide_region 位移
            #    下限守卫 + 原有 hot_skipped 补查」完成。
    finally:
        da.close()
        db.close()
    # ③ 逐行小块**重新聚成「段」**：标注画一个整框（不再斑马纹）、
    #    清单出一条带完整段落的条目（2026-09-30 用户反馈「分散」「惜字如金」）。
    # ⚠️ **快速模式（紫框）不聚段**（2026-10-26 第五轮）：聚段是为「转曲整段增删、
    #    逐行切开零信息量」设计的，但紫框既没有 OCR 文本、也不判增删，聚段只会把
    #    22 条真实改动（表格整段重排）合成一条盖住 30% 页面的大框 —— 用户原话
    #    「标记的范围太大，还不如 5.0」。紫框要的就是「一条条指出哪里变了」。
    if not fast_mode:
        out = _merge_paragraphs(out)
        out = _frag_suppress(out)
    # ④ 出口统一位移救援（2026-10-12）：行对拍管线的单侧区域没有像素配准
    #    守卫 —— 斜向位移被报成假增删（失败4 'Coronary I' 绿块）。见函数注释。
    #    快速模式没有「单侧红绿」概念，全是 pending，跳过。
    if not fast_mode:
        out = _moved_rescue_regions(out, old_path, new_path, dpi)
    if cf:
        try:
            with open(cf, "w", encoding="utf-8") as fh:
                # v59：缓存结构升级为 dict，把「整页重绘拦截」也一并落盘 ——
                # 否则热缓存命中时 `LAST_FULLPAGE_SKIPS` 丢失，清单 notes 里的
                # 「已跳过图形兜底」提示在二次运行时会消失（记账不静默的铁律）。
                json.dump({"regions": out, "fullpage": LAST_FULLPAGE_SKIPS},
                          fh, ensure_ascii=False)  # tuple→list，读回时转回来
        except Exception:
            pass
    # 页码守卫（指令F 一-2）：region 的 `page` 字段必须落在 [0, min页数) 内。
    _guard_regions_pages(out, n_pages, "graphic_regions")
    return out


def _guard_regions_pages(regions: List[Dict[str, Any]], n_pages: int,
                         where: str = "") -> None:
    """graphic_regions 出口的页码守卫（region 为 dict，`page` 字段）。"""
    for r in regions:
        p = r.get("page")
        if p is not None and not (0 <= p < n_pages):
            _log.error("region 页码越界 %s: page=%s >= n_pages=%s",
                       where, p, n_pages)
            raise ValueError(f"region page={p} 越界（n_pages={n_pages}） @ {where}")






OCR_JUNK_RULES: List[Dict[str, Any]] = [
    # ⚠️ 只对**单侧增删候选**生效：mod 有两侧互证，不需要这道闸门（也别加，
    #    加了会把真行内小改成片撤掉）。
    {"name": "够长才判", "cond": "len(txt) ≥ 10",
     "why": "短文本本来就没有统计意义（'NEX' / 'GW' / '3134' 都可能是真的）"},
    {"name": "token 够多", "cond": "≥ 6 个 token",
     "why": "'REF A35-070150-200' 这种只有 2 个 token 的真编号不能碰"},
    {"name": "几乎全是单字符", "cond": "平均 token 长 < 2.0 且最长 ≤ 3",
     "why": "乱码的典型长相：'Oe = > te 4 Пою м о ь д' 全是 1~2 字的碎片"},
    {"name": "没有两位数以上的数字", "cond": "无长度 ≥2 的纯数字 token",
     "why": "表格行 '8 10 12 14 16 18' 也是单字符碎片，但它是**真内容**，必须放行"},
]


# ---- ≤2 字单侧碎片抑制（2026-10-11 DOC-1003 用户实测「有一些小地方失误」）----
# 病根：`_ocr_junk` 刻意只抓 ≥10 字的多 token 乱码（`'NEX'/'GW'/'3134'` 都可能
#   是真标签，不能碰），于是 1~2 字的碎片整批漏进来 —— 实测图片对上报出
#   `added '©'`、`removed '00'`，用户对着清单一条条问这是什么。
# 判据（保守，两条全中才撤）：
#   ① 单侧文本归一化后 ≤ 2 个字符 —— 短到没有任何统计意义；
#   ② 同页**同侧**还有另一条已报区域、纵向带宽 ≤40pt —— 它是大块改动旁边
#      溅出来的碎片，那块改动本身已经报了，碎片不提供新信息。
# 孤立的 ≤2 字碎片**不撤**（页面上孤零零少了个「12」可能是真删了一行）。
FRAG_SUPPRESS_MAX = 2        # 归一化后 ≤ 这么多字符才算碎片
FRAG_SUPPRESS_BAND = 40.0    # 邻居区域的纵向距离（pt）


def _frag_suppress(regions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 `FRAG_SUPPRESS_*` 撤掉大块改动旁边的 ≤2 字单侧碎片。"""
    kept: List[Dict[str, Any]] = []
    for r in regions:
        o = (r.get("old_text") or "").strip()
        n = (r.get("new_text") or "").strip()
        if bool(o) == bool(n):            # mod / 空：不归这道闸管
            kept.append(r)
            continue
        side, txt = ("old", o) if o else ("new", n)
        if len(_norm_cjk(txt)) > FRAG_SUPPRESS_MAX:
            kept.append(r)
            continue
        y0, y1 = r["rect"][1], r["rect"][3]
        has_neighbor = False
        for q in regions:
            if q is r:
                continue
            qo = (q.get("old_text") or "").strip()
            qn = (q.get("new_text") or "").strip()
            q_txt = qo if side == "old" else qn
            if int(q.get("page", 0)) != int(r.get("page", 0)) or not q_txt:
                continue
            qy0, qy1 = q["rect"][1], q["rect"][3]
            if y1 + FRAG_SUPPRESS_BAND < qy0 or qy1 + FRAG_SUPPRESS_BAND < y0:
                continue                   # 纵向不相邻
            has_neighbor = True
            break
        if has_neighbor:
            continue                       # 大块旁边的碎片：撤
        kept.append(r)
    return kept


def _ocr_junk(txt: str) -> bool:
    """OCR 认出的这串字是不是**乱码**（按 `OCR_JUNK_RULES` 逐条过）。

    为什么需要（DOC-1003 实测）：扫描件上有一块条码/图形区，两版像素确实变了，
    OCR 认出 `'Oe   =    > te   4   Пою        м    о   ь д'` —— 全是单字碎片。
    它过得了「对面没有这段字」的判据（对面当然没有乱码），于是被当成**真新增**
    报到清单里；用户看到一条谁都读不懂的条目，只会怀疑整份结果。

    判「认不出」比判「改了什么」难得多，所以门槛刻意保守：
    **四条全中才撤**，任何一条不满足就原样放行（漏报一条噪声好过撤掉一处真改动）。
    """
    t = (txt or "").strip()
    if len(t) < 10:
        return False
    toks = t.split()
    if len(toks) < 6:
        return False
    if sum(len(x) for x in toks) / len(toks) >= 2.0 or max(len(x) for x in toks) > 3:
        return False
    if any(x.isdigit() and len(x) >= 2 for x in toks):
        return False                      # 表格数字行：放行
    return True


def _decide_region(pa, pb, oa, ob, i: int, rect, scale: float,
                   old_path: str, new_path: str, ref=None,
                   min_cover: float = OCR_MIN_COVER
                   ) -> Tuple[Optional[str], str]:
    """判一块（矩形）区域「算不算真改动」。返回 `(哪一侧, 认出的字)`。

    `side` ∈ `"old"` / `"new"` / `None`；`None` = 证据不足或对面也有 → 不报。
    `txt` 是**有内容那一侧** OCR 认出来的字，判 `None` 时也尽量带上（调用方要用它
    判「这一行字太少、不足以下结论」）。
    `ref` 是**这段改动所属的整块**矩形（逐行复核时传；不传就用 `rect` 自己）——
    对面取参照的范围按它来定，见 `_ocr_near`。
    `min_cover` 是「这段字对面也有」的门槛，默认 `OCR_MIN_COVER`；**逐行复核**
    时传更松的 `OCR_MIN_COVER_LINE`（理由见那里的注释）。

    两条判据（第二、三版实测打回来的）：
      · **一边基本空白**（墨水比 < `GRAPHIC_BLANK_RATIO`）= 整块增/删候选 →
        再 OCR 对面（同一矩形 + 就近一块 + 整条栏）确认这段字对面没有；
      · **两边都有字** → OCR 两侧，归一化相同 = 只是重渲染；
        不同且新侧这段字在旧版那条栏里找不到 = 真新增。
        ⚠️ 这条分支**只报绿**：旧侧 OCR 乱码是常态（转曲小字），
        「旧版这段没了」的证据极不可信；真删除由字符流引擎标红。
    """
    ink_a = _ink_count(pa, rect, scale)
    ink_b = _ink_count(pb, rect, scale)
    checkpoint()          # 中止检查点：一个区域 = 1~4 次 OCR，是最小可中断单元
    if max(ink_a, ink_b) <= 0:
        return None, ""
    # ⚠️ OCR 取字要比判定用的矩形**上下各留一点**：贴着一行字切，
    #    tesseract 会把「重压」认成「重讨」，覆盖率直接从 1.0 掉到 0.53。
    r = fitz.Rect(float(rect[0]) - 2.5, float(rect[1]) - OCR_CROP_PAD,
                  float(rect[2]) + 2.5, float(rect[3]) + OCR_CROP_PAD)
    ref = tuple(ref) if ref else tuple(rect)
    key = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    if min(ink_a, ink_b) / max(ink_a, ink_b) < GRAPHIC_BLANK_RATIO:
        # ---- 一边基本空白 = 整块增/删候选 ----
        blank_old = ink_a < ink_b
        src, src_path = (ob, new_path) if blank_old else (oa, old_path)
        dst, dst_path = (oa, old_path) if blank_old else (ob, new_path)
        # 配准用的像素图（有墨侧 src_pm / 空白侧 dst_pm）：与 doc 对象对应
        src_pm, dst_pm = (pb, pa) if blank_old else (pa, pb)
        txt = _ocr_crop(src, r, cache_key=(src_path, i) + key + (1,))
        ink_src = ink_b if blank_old else ink_a
        if not txt.strip():
            # ⚠️ OCR 认不出字 ≠ 这里没差异：纯色块/图形（大黑块、LOGO、色带）
            #    的整块增删没有文字可认，老逻辑直接 return None 把它们当「无意义」
            #    丢掉 —— DOC-1003 大黑块删除实测漏标。原理修正：有墨侧墨水量
            #    足够大（≥ GRAPHIC_SOLID_MIN）、对面确实空白，就报「图形增删」，
            #    文本用占位符标记，交给下游转成人类可读的「图形区域」。
            if ink_src >= GRAPHIC_SOLID_MIN:
                return ("new" if blank_old else "old"), GRAPHIC_SOLID_TAG
            return None, ""                   # 墨水量小 = 噪点，仍不报
        # ⚠️ 纯色块 OCR 常认出单个符号（`|` / `-` / `_` 等，黑块边缘被当成字符）：
        #    有墨侧墨水量大 + 认出的只是「极短无实义符号」，本质还是图形增删，
        #    不该报成「删除一个 | 字」。判据：归一化后实义字符 < 3 且墨水量 ≥ 阈值。
        _norm_txt = _norm_cjk(txt)
        if ink_src >= GRAPHIC_SOLID_MIN and len(_norm_txt) < 3:
            return ("new" if blank_old else "old"), GRAPHIC_SOLID_TAG
        other = _ocr_crop(dst, r, cache_key=(dst_path, i) + key + (2,))
        near = _ocr_near(dst, ref, dst_path, i)
        band = _ocr_band(dst, r, cache_key=(dst_path, i, "band",
                                            int(rect[0] // 40), int(rect[2] // 40)))
        if (_coverage(txt, other) >= min_cover
                or _coverage(txt, near) >= min_cover
                or _coverage(txt, band) >= min_cover):
            # 这段字对面也有 → 只是挪位/重排版。
            # ⚠️ 2026-10-16 指令D 二-2：**不再静默丢弃** —— 「对面有这段字」
            #    恰恰是位移的特征（内容没变、位置变了）。找出具体位移报 moved：
            #    `_local_shift_overlap` 在 ±窗内搜源块在对面页的墨迹重合位置，
            #    命中且配准位置 OCR 文本一致（防内容替换假位移，同下方闸）则报
            #    moved 浅蓝；找不到位移才回落 None（真重排版）。
            #    11vs12 实测：2.0 供应方式区 5 行正文右移 5.5~14.4pt（标签不动、
            #    正文动，行内相对位移），原逻辑被「对面栏里读得到相同文字」静默
            #    吞掉 → 一处都不标。
            ov, sdx, sdy = _local_shift_overlap(src_pm, dst_pm, r, scale)
            _mv0 = (sdx ** 2 + sdy ** 2) ** 0.5 / scale if scale else 0.0
            if ov >= _SHIFT_OVERLAP_MIN and _mv0 >= TM_SHIFT_MIN_PT:
                r_shift = fitz.Rect(r.x0 + sdx / scale, r.y0 + sdy / scale,
                                    r.x1 + sdx / scale, r.y1 + sdy / scale)
                t_shift = _ocr_crop(dst, r_shift,
                                    cache_key=(dst_path, i) + key + (6,))
                if _coverage(txt, t_shift) >= _SHIFT_TEXT_MIN:
                    return "moved", "%s\x00%.2f\x00%.2f\x00%s" % (
                        txt.strip(), sdx / scale, sdy / scale,
                        "new" if blank_old else "old")
            return None, txt.strip()          # 找不到具体位移 = 真重排版，仍不报
        # ⚠️ 像素位移配准兜底（GPT 第四阶段）：OCR 文本比对判不了「这段字只是
        #    挪了位置」时（韩文/泰文噪声大、短文本 coverage 恒 0、转曲没文字层），
        #    用局部平移搜索补一道：源块在对面页面里能移到一个「墨迹重合 ≥ 0.6」
        #    的位置 = 内容是同一块、只是整体平移。
        # ⚠️ 2026-10-12 升级：原来位移命中**静默丢弃**（return None）——结果
        #    「挪位整行」在清单里消失、标记层也没任何颜色；用户五色体系明确要求
        #    位移归**浅蓝**。现在报 `"moved"` 决策：`\x00` 通道携带 (dx,dy)（pt）
        #    与**源侧方向**，`_mk_region` 展开 `rect_shift`（对侧对应位置），
        #    标记层两侧画浅蓝、清单层出「位置移动」条目。
        # ⚠️ 2026-10-13 文本一致性闸（修复「内容替换」被误判成「位移」）：
        #    `_local_shift_overlap` 是**单向覆盖率**——只算「源墨迹落到目标墨迹
        #    的比例」。当对面同一栏里还有别的内容（行距紧、字形相似的中文），
        #    源行做纵向滑动时总能蹭到对侧**别的文字行**，单向覆盖率虚高
        #    （实测 11vs12「一个再折叠工具」这一行配准到旧版「冠状动脉刻痕球囊」
        #    行，ov 高达 0.833），把「真新增/真删除」错判成「位移」→ 该标绿/标红
        #    的一处都不标，反而多一块假浅蓝。
        #    真位移 = 同一段文字整体平移，**配准位置 OCR 出的字必须与源侧一致**；
        #    内容被替换时，配准位置 OCR 出的是别的字。所以在 ov 达标后，再用
        #    (sdx, sdy) 偏移去目标页 OCR 一段，文本覆盖不达标 → 不是位移，
        #    落回下面的增/删判定（报绿/报红），不再出假 moved。
        ov, sdx, sdy = _local_shift_overlap(src_pm, dst_pm, r, scale)
        _mv1 = (sdx ** 2 + sdy ** 2) ** 0.5 / scale if scale else 0.0
        if ov >= _SHIFT_OVERLAP_MIN and _mv1 >= TM_SHIFT_MIN_PT:
            r_shift = fitz.Rect(r.x0 + sdx / scale, r.y0 + sdy / scale,
                                r.x1 + sdx / scale, r.y1 + sdy / scale)
            t_shift = _ocr_crop(dst, r_shift,
                                cache_key=(dst_path, i) + key + (5,))
            if _coverage(txt, t_shift) >= _SHIFT_TEXT_MIN:
                return "moved", "%s\x00%.2f\x00%.2f\x00%s" % (
                    txt.strip(), sdx / scale, sdy / scale,
                    "new" if blank_old else "old")
        if _ocr_junk(txt):
            # 认出来的是一串单字碎片（条码/图形区的 OCR 噪声）——它当然「对面没有」，
            # 但那不是证据，报出去只会多一条谁都读不懂的假新增（DOC-1003 实测）。
            return None, txt.strip()
        return ("new" if blank_old else "old"), txt.strip()
    # ---- 两边都有字：只报绿（理由见 docstring）----
    # ⚠️ 2026-10-16 指令D 二-2：先做**位移检测** —— 「两边都有字」且像素确有
    #    差异，很可能是「行内相对位移」（2.0 供应方式区实测：标签不动、正文
    #    右移 5.5~14.4pt，同一判定矩形里新旧内容错位 → OCR 文本对不齐 →
    #    走到本分支被判 mod/None 丢弃）。源块在对面 ±窗内墨迹重合达标、且
    #    配准位置 OCR 文本与源侧一致 → 报 moved 浅蓝；找不到位移才落回原判定。
    t_old = _ocr_crop(oa, r, cache_key=(old_path, i) + key + (3,))
    ov2, sdx2, sdy2 = _local_shift_overlap(pa, pb, r, scale)
    # v69 位移下限守卫：`_local_shift_overlap` 对「复印件/重扫描的 ~1pt 错位」
    # 会给极小的 (sdx,sdy)（DOC-M2001 Storage 矩形实测 dx=0.00/dy=0.00），
    # 此时判 moved 是把「复制光栅错位」当「内容移动」——真改动（`dry, dark
    # place`→`dry, dark, cool place`）被吞、整片区域误标浅蓝。位移不足
    # TM_SHIFT_MIN_PT 的「moved」不当位移处理，落回下面的 `_expand_worthy`
    # 行内扩写判定（ver62 行为，Storage 根治）。
    _mv2 = (sdx2 ** 2 + sdy2 ** 2) ** 0.5 / scale if scale else 0.0
    if ov2 >= _SHIFT_OVERLAP_MIN and _mv2 >= TM_SHIFT_MIN_PT:
        r2_shift = fitz.Rect(r.x0 + sdx2 / scale, r.y0 + sdy2 / scale,
                             r.x1 + sdx2 / scale, r.y1 + sdy2 / scale)
        t2_shift = _ocr_crop(ob, r2_shift,
                             cache_key=(new_path, i) + key + (7,))
        if _coverage(t_old, t2_shift) >= _SHIFT_TEXT_MIN:
            return "moved", "%s\x00%.2f\x00%.2f\x00%s" % (
                t_old.strip(), sdx2 / scale, sdy2 / scale, "old")
    t_new = _ocr_crop(ob, r, cache_key=(new_path, i) + key + (4,))
    n_old, n_new = _norm_cjk(t_old), _norm_cjk(t_new)
    if n_old and n_old == n_new:
        return None, t_new.strip()            # 只是重新渲染/字形重绘
    if not n_new or len(n_new) < OCR_MIN_LEN:
        # ⚠️ `_coverage` 对短于 `OCR_MIN_LEN` 的文本恒返 0，下面的 mod 门槛对
        #    短行**永远不生效**。新侧认不出几个字时，唯一还能下的结论就是
        #    「两侧都很短的行内小改」——直接问 `_mod_worthy`（收原文）。
        #    ⚠️ 判据里**不能带上 `n_old` 太短**：DOC-1007 有 5 块转曲区正是
        #    「旧版 OCR 认不出几个字、新版认得出」的真新增（只报绿分支），
        #    把 n_old 也拉进来判会提前否掉它们（2026-09-30 实测 7 块掉成 2 块）。
        if n_old and n_new and _mod_worthy(t_old, t_new):
            return "mod", t_old.strip() + "\x00" + t_new.strip()
        return None, t_new.strip()            # 新侧认不出几个字 → 证据不足
    if (_coverage(t_old, t_new) >= min_cover
            or _coverage(t_new, t_old) >= min_cover):
        # 大部分相同 ≠ 完全相同 —— `n_old != n_new` 已在上面排过「重渲染」，
        # 走到这里 = **同一行的小改**候选（日期 06.04.2023→23.03.2026、
        # 版本号 Rev02→Rev03，DOC-1002 实测）：coverage 0.87~0.99 全过门槛，
        # 按旧口径判「文本相同」直接丢 → 真差异全漏。
        # `_mod_worthy` 收**原文**（token 级对齐判据，见函数 docstring）。
        # `_expand_worthy` 补「一侧多出实义内容」的行内扩写（M4506:
        # `dry, dark place` → `dry, dark, cool place`，coverage 0.96、
        # token 6→7，_mod_worthy 两关都过不去 → 真改动曾被判 None）。
        # ⚠️ 扩写的新增内容必须再过**对面整栏挪位核查**：排版位移会把别处的行
        #    推进判定矩形（M4506 右栏整体上移，'CE Mark' / 'EXPLANATION OF
        #    SYMBOLS' 在矩形内「对面没有」、在整条栏里明明还在）—— 不核查就
        #    把挪位内容当成新增。`cool` 在旧版整条栏里真不存在 → 保留。
        ext_hit = _expand_worthy(t_old, t_new)
        ext = ext_hit[0] if ext_hit else None
        if _mod_worthy(t_old, t_new) or ext:
            if ext:
                # 2026-10-19（M4506 cool 复制件）：`_expand_worthy` 现带方向 —
                # added（新版多出）核查**旧版**有没有这段字（挪位→不报）；
                # removed（旧版多出，如 `dry, dark, cool place → dry, dark
                # place`）核查**新版** —— cool 在新版整栏里真不存在 → 报 mod。
                # 核查侧对称调换，判据两步不变。
                chk_doc, chk_path = ((oa, old_path) if ext_hit[1] == "added"
                                     else (ob, new_path))
                # ① 对面**文字层**（最可靠的存在性证据 —— 'CE Mark' 这类行
                #    常在文字层里，只是被排版推到了矩形外；band 窄栏 OCR 会丢行）
                #    ⚠️ ext 常短于 OCR_MIN_LEN（'CE Mark'=6 字符），_coverage
                #    对它恒返 0 —— 短内容改用「norm 按序子串」判（≥4 字符才判，
                #    2~3 字符的 ext 本来就证据不足，保守保留）。
                tl_a = _page_text_cached(chk_doc)
                ne, nh = _norm_cjk(ext), _norm_cjk(tl_a)
                if len(ne) >= 4 and ne in nh:
                    return None, t_new.strip()   # 对面文字层里有 → 只是挪位
                # ② 对面近旁 / 整条栏（转曲内容文字层没有，只能靠 OCR）
                near_a = _ocr_near(chk_doc, ref, chk_path, i)
                band_a = _ocr_band(chk_doc, r, cache_key=(chk_path, i, "band",
                                                          int(rect[0] // 40), int(rect[2] // 40)))
                if (_coverage(ext, near_a) >= min_cover
                        or _coverage(ext, band_a) >= min_cover):
                    return None, t_new.strip()   # 对面栏里找得到 → 只是挪位
            return "mod", t_old.strip() + "\x00" + t_new.strip()
        return None, t_new.strip()            # 差异纯字母/错位 → OCR 噪声，不报
    near_a = _ocr_near(oa, ref, old_path, i)
    band_a = _ocr_band(oa, r, cache_key=(old_path, i, "band",
                                         int(rect[0] // 40), int(rect[2] // 40)))
    if (_coverage(t_new, near_a) < min_cover
            and _coverage(t_new, band_a) < min_cover):
        if _ocr_junk(t_new):
            return None, t_new.strip()       # 认出来的是乱码碎片 → 不是「真新增」的证据
        return "new", t_new.strip()          # 旧版那条栏里找不到 = 真新增
    return None, t_new.strip()


_MOD_MAX_TOKENS = 3         # 最多允许多少个 token 不一样；再多是错位/重排，不是「行内小改」


def _digit_swap(a: str, b: str) -> bool:
    """两个**已归一化**的 token 算不算一次「数字换数字」：等长、逐字符类别一致
    （数字只换数字、字母只换字母）、且至少一对双侧都是数字。

    `06042023`→`23032026`（注册证日期）、`rev02`→`rev03`（版本号）✓；
    `8oмм`→`80мм`（数字 vs 字母）、`hi`→`h1`（字母 vs 数字）✗。
    """
    if not a or not b or len(a) != len(b):
        return False
    has = False
    for ca, cb in zip(a, b):
        if ca == cb:
            continue
        if not (ca.isdigit() and cb.isdigit()):
            return False                # 数字 vs 字母/标点 → 抖动，不报
        has = True
    return has


def _mod_worthy(old_raw: str, new_raw: str) -> bool:
    """「大部分相同但不完全相同」的两段 OCR 文本，算不算一次**行内修改**。

    ⚠️ 收的是**未归一化的原文**（调用方别先 `_norm_cjk`）——归一化会把空白/
    标点全吃掉，token 结构就没了；而判据必须**按 token（空白分段）对齐**：

    整串 difflib 不可靠（上一版踩过）：`от 06.04.2023`→`от 23.03.2026` 在整串上
    被对齐成「insert + 两段不等长 replace」，等长数字替换的判据够不着 → **真改
    被拒**；反过来把阈值放松又满页误报。按 token 对齐后，日期 token 是干净的
    等长 `06042023`↔`23032026`，一条判据既抓住真改又挡住全部 5 条噪声
    （DOC-1002 rev02→rev03 实测）。

    噪声的**结构特征**（同轮实测，5 条误报全中）：
    · `80мм` 一次认成 `8омм` —— 数字 vs 同形字母（类别不同，`_digit_swap` 拒）；
    · 表格行 `8 ⏎ 10` 一次认成 `10` —— **token 数不等**（OCR 漏行/并行）；
    · `（01） 0 6958…` 认成 `01） 06958…` —— 空格漂移导致 token 错位（数不等）；
    · `Hi-Tech` 认成 `H1-Tech` —— 字母 vs 数字（类别不同）；
    · `Ко.` 认成 `Кo.` —— 同形折叠后相同（`_HOMOGLYPH` 已消）。

    所以口径是：token 数相等 + 不一样的 token ≤ `_MOD_MAX_TOKENS` +
    每个不一样的 token 都过 `_digit_swap`。其余一律不报 —— 漏报一件罕见的
    「纯字母改写」比把每处 OCR 抖动都标成「修改」好（假一致比漏判伤，
    但满屏误报同样让人没法用）。
    """
    to = (old_raw or "").split()
    tn = (new_raw or "").split()
    if not to or not tn or len(to) != len(tn):
        return False
    diff = 0
    for a, b in zip(to, tn):
        na, nb = _norm_cjk(a), _norm_cjk(b)
        if na == nb:
            continue
        diff += 1
        if diff > _MOD_MAX_TOKENS:
            return False
        if not _digit_swap(na, nb):
            return False
    return diff > 0


def _line_expand(old_line: str, new_line: str, old_norms: List[str],
                 new_norms: Optional[List[str]] = None
                 ) -> Optional[Tuple[str, str]]:
    """单行内「一侧多出一两个实义词」= 行内扩写/缩写。

    返回 `(内容原文, 方向)`，方向 ∈ `"added"`（新版多出）/ `"removed"`（旧版多出）；
    不算则返回 None。

    DOC-M2001 实测（2026-10-12）：`Store in a dry, dark place` →
    `Store in a dry, dark, cool place`。coverage ≈0.96 过「行内小改」门槛，
    但 `_mod_worthy` 要求 token 数相等 + 数字类差异 —— 纯**单词新增**两关都
    过不去 → 真改动被判 None。
      · token 对齐必须用**归一化 token**：新增 cool 的同时旧 token `dark` 多了
        个逗号，原文对齐会判成 replace（'dark' ≠ 'dark,'）→「只许 insert」被拒。
        标点差异（用户口径不标）不该破坏 token 对齐。
      · 只许新增、不许丢失/改写（改写归 `_mod_worthy`）；
      · 多出 1~2 个、每个 ≥2 个字母/数字（碎片/符号是 OCR 噪声）；
      · 多出 token 的内容不得在对面任何 token 里（子串）出现 —— 防 OCR
        空格漂移（`6958` 认成 `695 8`，'695' 是 '6958' 的子串 → 拒）。

    ⚠️ 2026-10-19（M4506 复制件回归）：**缩写方向**补齐。此前只收扩写
    （`len(tn) > len(to)`），因为「两边都有字只报绿」——删除由字符流引擎报。
    但这份文件 `dry, dark, cool place → dry, dark place` 的内容是**转曲**
    的，字符流引擎根本看不见 → 删除方向无人认领，`_decide_region` 判 None
    整处漏标。旧版 token 更多且为**纯 delete**（无改写）时同样收下，返回
    `("cool", "removed")`；对面核查由调用方按方向选边（removed 查新版）。
    `new_norms` 是新版全部行的归一化 token（防「删除内容其实在新版别处」）。
    """
    to = (old_line or "").split()
    tn = (new_line or "").split()
    if not to or not tn:
        return None
    no = [_norm_cjk(t) for t in to]
    nn = [_norm_cjk(t) for t in tn]
    if len(tn) > len(to):
        # ---- 扩写方向（原逻辑，一字未动）：新版 token 更多，只许 insert ----
        if len(tn) - len(to) > 2:
            return None
        sm = difflib.SequenceMatcher(a=no, b=nn)
        added: List[str] = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            if op != "insert":
                return None           # 有丢失/改写 → 不是单纯扩写
            for t in tn[j1:j2]:
                nt = _norm_cjk(t)
                if len(nt) < 2 or not nt.isalnum():
                    return None       # 碎片/符号 → 噪声
                if any(nt in on or on in nt for on in old_norms):
                    return None       # 内容对面已有 → OCR 空格漂移
                added.append(t)
        return (" ".join(added), "added") if added else None
    if len(to) > len(tn) and new_norms is not None:
        # ---- 缩写方向（2026-10-19 补）：旧版 token 更多，只许纯 delete ----
        if len(to) - len(tn) > 2:
            return None
        sm = difflib.SequenceMatcher(a=no, b=nn)
        removed: List[str] = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            if op != "delete":
                return None           # 有新增/改写 → 不是单纯缩写（归 _mod_worthy）
            for t in to[i1:i2]:
                nt = _norm_cjk(t)
                if len(nt) < 2 or not nt.isalnum():
                    return None       # 碎片/符号 → 噪声
                if any(nt in on or on in nt for on in new_norms):
                    return None       # 内容新版已有 → OCR 空格漂移
                removed.append(t)
        return (" ".join(removed), "removed") if removed else None
    return None                       # token 数相等 → 归 _mod_worthy


def _expand_worthy(old_raw: str, new_raw: str) -> Optional[Tuple[str, str]]:
    """「两边都有字」且高度相似时，一侧**多出实义内容** = 扩写/缩写。

    返回 `(内容原文, 方向)`，方向 ∈ `"added"`（新版多出）/ `"removed"`（旧版
    多出，2026-10-19 补）；不算扩写返回 None。调用方按方向选边做对面整栏挪位
    核查（added 查旧版、removed 查新版）。

    ⚠️ 判定单位是**行**，不是整段 token：判定矩形常常裹进上下相邻行
    （DOC-M2001 实测：宽矩形把 OLD 侧下一行的 OCR 乱码 `2:10ト2` 也读了进来，
    整段 token 数 9 vs 9，「多出一个词」的结构正好被抵消 → 恒 False）。
    两层：① 归一化行对齐 —— 公共行剔除，**delete 行按「两边都有字只报绿」
    原则忽略**（乱码碎行不该阻止同行其他位置的新增被看见）；
    ② 差异行做 `_line_expand`（整行新增 / 行内扩写 / 2026-10-19 起含行内缩写）。
    ⚠️ added 与 removed 混出时返回 None（语义混乱，保守不报）。
    """
    lo = [x.strip() for x in (old_raw or "").splitlines() if x.strip()]
    ln2 = [x.strip() for x in (new_raw or "").splitlines() if x.strip()]
    if not lo or not ln2:
        return None
    no = [_norm_cjk(x) for x in lo]
    nn2 = [_norm_cjk(x) for x in ln2]
    old_norms = [t for t in no if t]
    new_norms = [t for t in nn2 if t]
    sm = difflib.SequenceMatcher(a=no, b=nn2)
    found: Optional[Tuple[str, str]] = None
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal" or op == "delete":
            continue              # delete：旧侧多出的行，只报绿原则 —— 不动也不拦
        if op == "insert":
            for ln_txt in ln2[j1:j2]:
                nt = _norm_cjk(ln_txt)
                if len(nt) < 2:
                    return None
                # 防空格漂移：整行内容在对面某行里（子串）= 不是新增
                if any(nt in on or on in nt for on in old_norms):
                    return None
                # 行里至少要有一个 ≥2 位字母数字的实义 token
                if not any(len(_norm_cjk(w)) >= 2 and _norm_cjk(w).isalnum()
                           for w in ln_txt.split()):
                    return None   # 碎片行 → 噪声
                if found is not None and found[1] != "added":
                    return None   # 方向混合 → 保守不报
                found = (ln_txt if found is None
                         else found[0] + " " + ln_txt, "added")
        else:                     # replace：逐行对做行内扩写/缩写判定
            for a_ln, b_ln in zip(lo[i1:i2], ln2[j1:j2]):
                hit = _line_expand(a_ln, b_ln, old_norms, new_norms)
                if hit is None:
                    continue
                ext, direction = hit
                if found is not None and found[1] != direction:
                    return None   # 方向混合 → 保守不报
                found = (ext if found is None
                         else found[0] + " " + ext, direction)
    return found


def _page_text_cached(page) -> str:
    """一页的**可提取文字层**全文（进程内缓存 —— `_decide_region` 的扩写挪位
    核查要用：文字层是最可靠的「对面有没有这段字」证据，比窄栏 OCR 稳）。
    ⚠️ 只做存在性核查的原料，不参与判定（判定权仍在 coverage 门槛）。"""
    key = id(page)
    val = _PAGE_TEXT_CACHE.get(key)
    if val is None:
        try:
            val = page.get_text() or ""
        except Exception:
            val = ""
        _PAGE_TEXT_CACHE[key] = val
    return val


_PAGE_TEXT_CACHE: Dict[int, str] = {}


def _mk_region(page: int, rect, side: str, txt: str) -> Dict[str, Any]:
    """拼一条区域记录（哪一侧有字就在那一侧标）。

    `side == "mod"` 时 `txt` 是 `\x00` 分隔的 `旧文本\\x00新文本` —— 两侧都填，
    core 侧据此生成一条「修改」条目。
    `side == "moved"` 时 `txt` 是 `文本\\x00dx\\x00dy\\x00源侧`（位移 pt 与
    有墨那侧）：`rect` = 源侧矩形，`rect_shift` = rect 平移 (dx,dy) 后的
    **对侧对应位置** —— 两侧文字相同、只是位置不同，标记层两侧画浅蓝、
    清单层出「位置移动」条目（2026-10-12 五色体系）。
    """
    if side == "mod" and "\x00" in txt:
        to, tn = txt.split("\x00", 1)
        return {"page": page, "rect": tuple(rect), "old_text": to, "new_text": tn}
    if side == "moved" and txt.count("\x00") == 3:
        t, dxs, dys, src = txt.split("\x00", 3)
        fx, fy = float(dxs), float(dys)
        rect_s = (rect[0] + fx, rect[1] + fy, rect[2] + fx, rect[3] + fy)
        return {"page": page, "rect": tuple(rect), "rect_shift": rect_s,
                "old_text": t, "new_text": t, "moved": True,
                "shift_src": src if src in ("old", "new") else "new"}
    return {"page": page, "rect": tuple(rect),
            "old_text": txt if side == "old" else "",
            "new_text": txt if side == "new" else ""}


# ---- 「行内改写」整行框收窄（2026-10-01 用户反馈 DOC-1002 注册证行多标）----
# 用户原话：「前面这一大段的文字……他没有变动。他好像只有后面那个日期变动了。
#           这个属于是多标了。」
# 病根：扫描件行对拍给 mod 配对的框是**整行**（两侧行框并集）——行里只有末尾的
#   日期变了（06.04.2023 → 23.03.2026），却把「Регистрационное удостоверение
#   № РЗН 2023/20012 от 」这 50 个没动的字一起圈进去。
# 为什么不用像素列差分（2026-10-01 实测后放弃，别再试）：
#   两版扫描的原生分辨率差一倍（DOC-1002 旧 4290px / 新 2145px），同 dpi 渲染后
#   笔画锐度不同 → 逐列墨迹差在**整行到处都是** 5~20% 的假变化（实测注册证行
#   22.7% 列超阈、碎成 19 段），按阈值收窄要么框错位置、要么把真改动裁掉 ——
#   正是「绝不凭空造不一致 / 宁可少标不可乱标」最怕的那类误判。
# 正解是**文本比例**：OCR 已经把两侧行文字都读出来了（mod 配对的前提），
#   SequenceMatcher 的公共前缀/后缀给出变化片段的**字符区间**，按「字符占比 ≈
#   行宽占比」折算成横向区间。实测 DOC-1002 注册证行：日期实际占 [0.839, 0.988]，
#   比例估计 [0.833, 1.0] —— 误差不到 1%，完全够用。
SCAN_NARROW_MAX_FRAC = 0.60   # 变化片段超过行宽六成 = 大段改写，不收窄（整行照旧）
SCAN_NARROW_PAD = 0.02        # 两端各留 2% 行宽余量（比例估计有误差，别贴着字切）


def _mod_span_frac(old_text: str, new_text: str):
    """变化片段在**整行横向**上的占比区间（按字符数折算），不适合收窄时返回 None。

    返回 `((旧侧起, 旧侧止), (新侧起, 新侧止))`，两侧各自算 —— 前缀长度在两版
    可能不同（比如「Rev06/DCR 0000-0000」→「Rev07」之后面的日期会整体左移）。
    多处变化取**并集**（一处收窄框不住就别收）。
    """
    to, tn = old_text or "", new_text or ""
    # 多行 OCR 区域（\n）的字符占比横跨好几行，折算成单行横向没有意义 → 不收窄
    if not to or not tn or "\n" in to or "\n" in tn:
        return None
    if len(to) < 4 or len(tn) < 4:
        return None                     # 太短，占比没有意义
    sm = difflib.SequenceMatcher(None, to, tn, autojunk=False)
    lo_o, hi_o, lo_n, hi_n = len(to), 0, len(tn), 0
    any_change = False
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        any_change = True
        lo_o, hi_o = min(lo_o, i1), max(hi_o, i2)
        lo_n, hi_n = min(lo_n, j1), max(hi_n, j2)
    if not any_change:
        return None
    fo = (lo_o / float(len(to)), hi_o / float(len(to)))
    fn = (lo_n / float(len(tn)), hi_n / float(len(tn)))
    # 任一侧变化过半 = 大段改写，不收窄（收窄省不了多少、还容易切掉字）
    if fo[1] - fo[0] > SCAN_NARROW_MAX_FRAC or fn[1] - fn[0] > SCAN_NARROW_MAX_FRAC:
        return None
    return fo, fn


def _narrow_mod_region(rg: Dict[str, Any], oa, ob) -> None:
    """把 mod 区域的整行框**就地**收窄到真正变了的横向片段（失败保持原样）。

    ⚠️ 只动横向、不动纵向；`rect_a` / `rect_b` / `rect`（并集）一起更新 ——
       清单（`_graphic_rescue_core`）与标注（`pdf_annotate._prepare`）读的是
       同一份 region，两处自然同源。
    ⚠️ 任何一步不满足条件都**原样返回**：收窄是「锦上添花」，绝不能因为收窄
       把真改动裁出去（宁可整行多标，不可框错漏标）。
    """
    try:
        ra, rb = rg.get("rect_a"), rg.get("rect_b")
        to, tn = rg.get("old_text") or "", rg.get("new_text") or ""
        if not ra or not rb or not to or not tn:
            return
        spans = _mod_span_frac(to, tn)
        if spans is None:
            return
        (o0, o1), (n0, n1) = spans
        out = []
        for f0, f1, rect in ((o0, o1, ra), (n0, n1, rb)):
            x0a, y0a, x1a, y1a = (float(v) for v in rect[:4])
            w = x1a - x0a
            if w <= 1.0:
                return                  # 行框异常（太窄），别收
            nx0 = x0a + w * max(0.0, f0 - SCAN_NARROW_PAD)
            nx1 = x0a + w * min(1.0, f1 + SCAN_NARROW_PAD)
            if nx1 - nx0 < max(2.0, w * 0.03):
                return                  # 收窄后窄得离谱 = 估计不可信，别收
            out.append((nx0, y0a, nx1, y1a))
        rg["rect_a"], rg["rect_b"] = out[0], out[1]
        rg["rect"] = (min(out[0][0], out[1][0]), min(out[0][1], out[1][1]),
                      max(out[0][2], out[1][2]), max(out[0][3], out[1][3]))
        # 坐标现在是**按字符占比折算**的，不是实测的 —— 清单侧据此把 loc_conf 降档
        # （见 pdfdiff_core.SRC_CONF["ocr_est"]）。实测误差 <1%，但终究是估的，得如实说。
        rg["narrowed"] = True
    except Exception:
        pass                            # 收窄失败 = 保持整行，绝不让它毁掉标注


def _ink_rows(pm, rect, scale: float):
    """逐像素行的墨水量，外加该行最左/最右有墨的列（用于把行左右贴紧字）。"""
    x0 = max(0, int(rect[0] * scale))
    x1 = min(pm.width, int(rect[2] * scale))
    y0 = max(0, int(rect[1] * scale))
    y1 = min(pm.height, int(rect[3] * scale))
    rows = []
    for y in range(y0, y1):
        off = y * pm.stride
        n, lo, hi = 0, -1, -1
        for k, v in enumerate(pm.samples[off + x0:off + x1]):
            if v < SCAN_INK_DARK:
                if lo < 0:
                    lo = k
                hi = k
                n += 1
        rows.append((n, lo, hi))
    return rows, x0, y0


def _split_lines(pm, rect, scale: float, anchor=None
                 ) -> List[Tuple[float, float, float, float]]:
    """把一块区域按「**行间空白**」切成若干文本行（页面坐标，左右贴住字）。

    为什么要切：转曲块的像素对拍是**整块级**的，一块里往往只有中间一两行是新的，
    上下几行只是被顶下去 —— 整块标出来就是「标多了」。用户 2026-09-29 实测：
    左栏 2.0 供货方式那块，真正新增的只有 `一个再折叠工具` 一行，
    下面 `防止重压…` / `须贮存在…` 两行内容没变，却一起被框住了。

    ⚠️ 细于 `GRAPHIC_MIN_LINE` 的「行」要丢掉：实测块顶有一条 0.6pt 的**残影**
    （上一行字的底部），不丢就会把 `一条冠状动脉刻痕球囊扩张导管` 那行也带上。

    ⚠️ 断行阈值只能是「**有 1px 空白就断**」（`GRAPHIC_LINE_GAP` 调到 0.1pt）：
    这份 PDF 转曲正文的行距极紧 —— 实测行间空白只有 0.8 / 1.2 / 1.8 / 2.4pt 不等。
    阈值取 1.4pt（旧值）时，1.2pt 的缝断不开，`检查球囊导管是否完整；` 和下一行
    被切成**同一行**，OCR 混读出两行的字 → 对面比对不过 → 连没变的那行也被标绿
    （用户 2026-09-29 报的「标多了」就是这么来的）。

    `anchor`：给了它时，`rect` 只当**扫描窗口**用（调用方通常把它上下外扩过，
    好完整切出首/末行），但只保留与 `anchor` 垂直重叠 ≥ `GRAPHIC_ANCHOR_MIN`
    的行 —— 否则外扩会把上一行/下一行也圈进来。不传就按 `rect` 自己过滤。
    """
    rows, x0, y0 = _ink_rows(pm, rect, scale)
    gap_max = max(0, int(round(GRAPHIC_LINE_GAP * scale)))
    min_h = max(1, int(round(GRAPHIC_MIN_LINE * scale)))
    bands: List[List[int]] = []
    cur: Optional[List[int]] = None
    gap = 0
    for k, (n, lo, hi) in enumerate(rows):
        if n:
            if cur is None:
                cur = [k, k, lo, hi]
            else:
                cur[1] = k
                cur[2] = min(cur[2], lo)
                cur[3] = max(cur[3], hi)
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > gap_max:
                bands.append(cur)
                cur = None
                gap = 0
    if cur is not None:
        bands.append(cur)
    ax0 = anchor[1] if anchor else rect[1]
    ax1 = anchor[3] if anchor else rect[3]
    out = []
    for b in bands:
        if b[1] - b[0] + 1 < min_h:
            continue
        r_y0, r_y1 = (y0 + b[0]) / scale, (y0 + b[1] + 1) / scale
        if anchor is not None:
            # 与块本身（不是外扩窗口）的垂直重叠太少 → 是隔壁的行，不要
            if min(r_y1, ax1) - max(r_y0, ax0) < GRAPHIC_ANCHOR_MIN:
                continue
        out.append(((x0 + b[2]) / scale, r_y0, (x0 + b[3] + 1) / scale, r_y1))
    return out


def _strip_lead_mark(line: str, ref: str) -> str:
    """去掉 OCR 在行首多认出来的项目符号。

    横向放宽后上边界会把上一行的符号（`-` / `•` / `♦` / `·`）带进来，
    而**本行判定用的那份 OCR 没有这个符号**——那就说明是带进来的，去掉。
    本行真有符号（`ref` 也以它开头）时原样保留。
    """
    s = line.lstrip()
    if not s or not ref:
        return line
    c = s[0]
    if c not in "-–—•·◦●○*":
        return line
    return s[1:].lstrip() if c not in ref.lstrip()[:1] else line


def _refine_region(pa, pb, oa, ob, i: int, rect, scale: float,
                   old_path: str, new_path: str) -> List[Dict[str, Any]]:
    """过关的转曲块 → **逐行**复核，只留真正变了的那几行。

    行分三种：`keep`（复核通过）、`drop`（对面也有）、`?`（这行认出的字太少、
    不足以下结论）。`?` 行只要挨着一个 `keep` 行就跟着留 —— 否则会漏掉
    「分）。」这种只有两个字的收尾行。
    最后把**相邻保留行**之间的小缝补掉（被丢掉的行不补），免得看着像碎块。
    一行都留不下时退回整块，**宁可标粗也不漏**。

    ⚠️ **展示用的正文另取一次 OCR，横向多留 `OCR_TEXT_PAD_X`**：判定用的那份
    裁剪贴着字切（`_decide_region` 里 `OCR_CROP_PAD` 只有 1.2pt），Vision 会因为
    上下文不足把「如果需要」认成「知果需要」、「球囊导管」认成「求囊导管」、
    「分）。」认成「7）。」、「再折叠工具」认成「斤叠工具」。实测同一批行
    横向放宽到 10pt **六行全对**，而判定口径一个字都不用动。
    纵向**不能**跟着放宽：会把上一行的字卷进来（见 `OCR_TEXT_PAD_X` 的注释）。
    """
    side, txt = _decide_region(pa, pb, oa, ob, i, rect, scale, old_path, new_path)
    if side is None:
        # ⚠️ 2026-11-02：块级 None 是**整块聚合**的结果，块内个别行仍可能是真新增
        #    （被多数相同行平均掉）。但这条路径上 `_line_rescan` 已经在上游
        #    主循环里跑过了（见 `_cluster_shift_in_rect` 命中分支），这里不再重复 ——
        #    同一个动作做两遍只会多付 OCR，不会多捞回东西（5 个真实件实测零差异）。
        return []
    # ⚠️ 纯图形/色块增删（占位符 `[图形]`）没有「行」可切，细切只会重新 OCR 出
    #    垃圾符号（`| |`）把占位符覆盖掉 —— 直接原样返回，保持「图形区域」语义。
    if txt == GRAPHIC_SOLID_TAG:
        return [_mk_region(i, rect, side, txt)]
    if side == "moved":
        # 位移区域（2026-10-12）：rect/rect_shift 已由 `_mk_region` 展开，
        # 细切只会把块拆成行、重判丢标志 —— 原样返回。
        return [_mk_region(i, rect, side, txt)]
    if side == "mod":
        # 行内小改（DOC-1002 注册证日期）：整块都是「两侧都有字」，其中只有
        # 个别行真差、其余行是 OCR 抖动 —— 细切到行，只留 `_decide_region`
        # 也判 mod 的行。不重认展示文本（打包文本 `旧\x00新` 原样保留）。
        rows = _split_lines(pb, (rect[0], rect[1] - GRAPHIC_BLEED,
                                 rect[2], rect[3] + GRAPHIC_BLEED), scale,
                            anchor=rect)
        kept_mod = []
        for rr in rows:
            s2, t2 = _decide_region(pa, pb, oa, ob, i, rr, scale, old_path,
                                    new_path, ref=rect, min_cover=OCR_MIN_COVER_LINE)
            if s2 == "mod" and len(_norm_cjk(t2)) >= OCR_MIN_LEN:
                kept_mod.append(_mk_region(i, rr, "mod", t2.strip()))
            # ⚠️ 2026-10-13 修复「块内混行被吞」：块级判 mod 只是块内**多数行**
            #    两侧都有字（覆盖率高），并不代表**每一行**都是行内小改。转曲
            #    区插入/删除一行时（11vs12「内装 6.0/9.0 → 一个再折叠工具」），
            #    该行行级判的是 `new`/`old`（增删），不是 `mod`。旧口径只收
            #    `mod` 行 → 这条真增删被整块吞掉（kept_mod 空 + 块高超兜底阈值
            #    → return []）。现在把行级判出的**增删行**也一并收下：真增删
            #    的证明力不弱于行内小改，同样不该被 mod 聚合的覆盖率淹没。
            #    ⚠️ 增删行的字数门槛用 `RESCAN_ROW_MIN_CHARS`（不是 OCR_MIN_LEN）：
            #    增删行是「对面基本空白 + OCR 认出了字」的直接证据，行级判据
            #    本就不需要「两侧都有字」那么长的字串；OCR_MIN_LEN=10 会把
            #    「一个再折叠工具」（7 字）这类真新增行挡掉。
            elif s2 in ("new", "old") and len(_norm_cjk(t2)) >= RESCAN_ROW_MIN_CHARS:
                kept_mod.append(_mk_region(i, rr, s2, t2.strip()))
        if kept_mod:
            return kept_mod
        # ⚠️ 2026-10-12 失败1（DOC-1007 11vs12）大面积误标修复：
        #   块级判 mod、**行级复核一行都没过**时，旧口径把**整块**兜底标黄
        #   （`kept_mod or [整块]`）。转曲区把这条兜底喂成了 32/46/219pt 的
        #   巨型黄框 —— 图标列整体微移 + OCR 抖动，行级全是噪声、块级聚合
        #   却过了 mod 门槛。用户拍板：「大面积误标要么标蓝要么删掉」。
        #   行级（更宽的 `OCR_MIN_COVER_LINE`）都凑不出一条真改 → 大块的
        #   mod 证据不足，**直接放弃**。小碎块（< GRAPHIC_MOD_FALLBACK_MAX_H）
        #   保留兜底：一行内的小改（DOC-1002 日期/版本号）不能丢。
        if rect[3] - rect[1] >= GRAPHIC_MOD_FALLBACK_MAX_H:
            return []
        return [_mk_region(i, rect, "mod", txt)]
    pm = pb if side == "new" else pa
    pg, pg_path = (ob, new_path) if side == "new" else (oa, old_path)

    def disp(rr, tag: str, fallback: str) -> str:
        """展示用文本：横向放宽后重认一遍（认不出就返回空，调用方退回原文本）。"""
        t = _ocr_crop(pg, fitz.Rect(rr[0] - OCR_TEXT_PAD_X, rr[1] - OCR_CROP_PAD,
                                    rr[2] + OCR_TEXT_PAD_X, rr[3] + OCR_CROP_PAD),
                      cache_key=(pg_path, i, tag) + tuple(int(v) for v in rr)).strip()
        return _strip_lead_mark(t, fallback) if t else ""

    # ⚠️ 切行的矩形要在 y 方向**外扩一点**：`rect` 是 24px 瓦片拼出来的，粒度 ≈14.4pt，
    #    边界常常正好切在首行/末行的字中间 —— 不外扩就会把首行/末行切掉半截。
    #    实测新版中栏那块末行 `再折叠工具是一个可以…` 只标到 y=145.2，
    #    而这行实际到 148.8（少了 3.6pt）—— 用户报的「标得不完全」就是这个。
    rows = _split_lines(pm, (rect[0], rect[1] - GRAPHIC_BLEED,
                             rect[2], rect[3] + GRAPHIC_BLEED), scale,
                        anchor=rect)
    if len(rows) <= 1:
        return [_mk_region(i, rect, side, disp(rect, "one", txt) or txt)]
    state: List[List[Any]] = []           # [行矩形, 认出的字, keep/drop/?, 行号]
    for ri, rr in enumerate(rows):
        s2, t2 = _decide_region(pa, pb, oa, ob, i, rr, scale, old_path, new_path,
                                ref=rect, min_cover=OCR_MIN_COVER_LINE)
        if s2 == side:
            state.append([rr, t2, "keep", ri])
        elif t2 and len(_norm_cjk(t2)) < OCR_MIN_LEN:
            state.append([rr, t2, "?", ri])   # 字太少，不足以下结论
        else:
            state.append([rr, "", "drop", ri])
    for k, st in enumerate(state):
        if st[2] != "?":
            continue
        nb = [state[j][2] == "keep" for j in (k - 1, k + 1) if 0 <= j < len(state)]
        if any(nb):
            state[k][2] = "keep"              # 挨着一个保留行 → 跟着留，别漏
    kept = [st for st in state if st[2] == "keep"]
    if not kept:
        return [_mk_region(i, rect, side, disp(rect, "one", txt) or txt)]
    out = []
    for k, (rr, t2, _st, ri) in enumerate(kept):
        y0, y1 = rr[1], rr[3]
        # 补掉与相邻保留行之间的缝，但**最多补 REFINE_SEAM_MAX**：
        # ⚠️ 两个保留行之间可能夹着被丢掉的行（缝 ≈ 一整行高 ≈ 9pt），
        #    对半分就会把那行的一半也圈进来 —— 实测把「检查球囊导管是否完整；」
        #    又标回去了一次（正是用户报的「标多了」）。
        if k > 0:
            y0 -= min((rr[1] - kept[k - 1][0][3]) / 2.0, REFINE_SEAM_MAX)
        if k + 1 < len(kept):
            y1 += min((kept[k + 1][0][1] - rr[3]) / 2.0, REFINE_SEAM_MAX)
        # 正文取**横向放宽后重认的那一行**（认不出才退回判定用的那次）
        out.append(_mk_region(i, (rr[0], y0, rr[2], y1), side,
                              disp(rr, "row%d" % ri, t2) or t2 or txt))
    return out


def _line_rescan(pa, pb, oa, ob, i: int, rect, scale: float,
                 old_path: str, new_path: str) -> List[Dict[str, Any]]:
    """块级判**否**的候选块 → **逐行**再判一次，把块内的真增删行捞回来。

    为什么必须有这一步（2026-09-30 DOC-1007 实测）
    ----------------------------------------------
    左栏 2.0 供货方式那块，新版比旧版多一行 `一个再折叠工具`，它下面
    `防止重压、阳光直晒、雨雪浸淋。` / `须贮存在干燥、避光、室温…` 两行只是被
    **顶下去**了（纵向只差 6pt）。瓦片对拍 4 邻接把这几行连成**一个**候选块，
    块级 `_decide_region` 一算：旧版那两行的字 100% 都在新版里（`_coverage` 顶到
    0.93）→ 判「只是重渲染」→ 整块丢掉，**真新增的那一行跟着一起没了**。

    ⚠️ 病根是**粒度**不是判据：块级覆盖率天然会把「块内一处真新增」淹没在
    「整块大面积只是位移」里。细切到行之后，`一个再折叠工具` 那行对面是**空白**，
    走的是「一边基本空白」分支 —— 一判一个准。判据一个字都不用动。

    ⚠️ 逐行用 `OCR_MIN_COVER_LINE`（0.75）而不是块级的 0.85：
    行内范围小、误判空间小；0.85 是为整段设的，用在单行上会偏严。

    两侧 pixmap 各切一次（新版有墨的行走 pb、旧版有墨的行走 pa），
    切出来的「同一行」在几何上几乎重合 → 最后去重。
    """
    if not GRAPHIC_LINE_RESCAN:
        return []
    out: List[Dict[str, Any]] = []
    for side, pm, pg, pg_path in (("new", pb, ob, new_path),
                                  ("old", pa, oa, old_path)):
        rows = _split_lines(pm, (rect[0], rect[1] - GRAPHIC_BLEED,
                                 rect[2], rect[3] + GRAPHIC_BLEED), scale,
                            anchor=rect)
        if len(rows) <= 1:
            continue                  # 整块就是一行：粗判已经看过它，没必要重判
        for rr in rows:
            s2, t2 = _decide_region(pa, pb, oa, ob, i, rr, scale,
                                    old_path, new_path, ref=rect,
                                    min_cover=OCR_MIN_COVER_LINE)
            if s2 == "mod":
                # 行内小改（数字/日期/版本号）：两侧都有字，不隶属 new/old
                # 任何一侧 —— 单独收，去重逻辑会把两侧切出的同一行消掉。
                if len(_norm_cjk(t2)) >= RESCAN_ROW_MIN_CHARS:
                    out.append(_mk_region(i, rr, "mod", t2.strip()))
                continue
            if s2 != side:
                continue
            if len(_norm_cjk(t2)) < RESCAN_ROW_MIN_CHARS:
                continue              # 认出的字太少 → 证据不足，宁可不报
            out.append(_mk_region(i, rr, side, t2.strip()))
    # 两侧各切一次会把同一行切出两遍（几何几乎重合）→ 去重（保留先到的）
    dedup: List[Dict[str, Any]] = []
    for rg in out:
        r = rg["rect"]
        dup = False
        for k in dedup:
            kr = k["rect"]
            if (abs(r[1] - kr[1]) < 2.0 and abs(r[3] - kr[3]) < 2.0
                    and min(r[2], kr[2]) - max(r[0], kr[0]) > 0.0):
                dup = True
                break
        if not dup:
            dedup.append(rg)
    return dedup


def _channel_is_gutter(tiles, x0: float, x1: float, page_rows: int,
                       tile: int, scale: float, min_frac: float = 0.08) -> bool:
    """竖直通道 [x0,x1]（pt）整页高度上几乎贴着**文字层瓦片** → False（栏内）；
    通道整页几乎无字 → True（栏间通道）。

    为什么需要这个判据（2026-09-30 DOC-1007 实测）
    ----------------------------------------------
    `_merge_adjacent` 只看两块横向间隙 ≤ `x_gap`，可这份文档栏间距只有
    25.8pt（中栏右缘 606.3 / 右栏左缘 632.1），30pt 的 `x_gap` 会把中栏
    候选块与右栏候选块并成**跨栏巨框**：标注框横盖两栏（用户说的
    「标注范围过大」），OCR 还把右栏的字认进中栏条目（清单文本污染）。
    单纯调小 `x_gap` 治不了根 —— 别的文档栏间距可能更窄。
    栏间通道的结构特征是**通页无字**：文字层瓦片（`_text_tiles` 的产出）
    在真正栏间的那条竖直带上，从页顶到页底都数不出几块；而栏内两段之间
    的横向间隙带，上下延伸处全是同栏正文。取「该带内出现过文字层瓦片的
    行数 ÷ 全页瓦片行数 ≥ `min_frac`」当「栏内」门槛。
    """
    if not tiles or page_rows <= 0:
        return False
    tx0 = int(x0 * scale / tile)
    tx1 = int(x1 * scale / tile)
    rows_with_text = {ty for tx, ty in tiles if tx0 <= tx <= tx1}
    return len(rows_with_text) / page_rows < min_frac


def _merge_adjacent(regions: List[Dict[str, Any]], y_gap: float = 8.0,
                    x_gap: float = 30.0, tiles=None, tile: int = 24,
                    scale: float = 1.0, page_rows: int = 0) -> List[Dict[str, Any]]:
    """把同页、同侧（同为新增/同为删除）且位置相邻的矩形并成一大块。

    转曲文字没有字符坐标，只给「行尾一小块」用户看着莫名其妙 —— 不如把
    「如果需要再次插入该球囊导管…」+「• 再折叠工具」标题+介绍句这些
    相邻的 hot 块连成一个整段（用户 2026-09-29 拍板）。不同侧（一红一绿）不合并。
    """
    def _side(x):
        return _para_side(x)

    merged: List[Dict[str, Any]] = []
    for rg in sorted(regions, key=lambda x: (x["page"], _side(x), x["rect"][1], x["rect"][0])):
        r = rg["rect"]
        hit = None
        for m in merged:
            if m["page"] != rg["page"] or _side(m) != _side(rg):
                continue
            mr = m["rect"]
            y_ok = (r[1] <= mr[3] + y_gap) and (r[3] >= mr[1] - y_gap)
            x_ok = (r[0] <= mr[2] + x_gap) and (r[2] >= mr[0] - x_gap)
            if y_ok and x_ok:
                lo, hi = min(mr[2], r[2]), max(mr[0], r[0])
                if hi > lo and _channel_is_gutter(tiles, lo, hi, page_rows,
                                                  tile, scale):
                    continue          # 中间隔着贯通整页的栏间通道 → 不是同一栏
                hit = m
                break
        if hit is None:
            merged.append(dict(rg))
            continue
        mr = hit["rect"]
        hit["rect"] = (min(mr[0], r[0]), min(mr[1], r[1]),
                       max(mr[2], r[2]), max(mr[3], r[3]))
        if _side(rg) == "new":
            hit["new_text"] = (hit["new_text"] + "\n" + rg["new_text"]).strip()
        else:
            hit["old_text"] = (hit["old_text"] + "\n" + rg["old_text"]).strip()
    return merged


# ---------------------------------------------------------------- 段落重组
# 「逐行小块 → 整段」参数表（2026-09-30）。三条都是**结构参数**，不是魔法数：
# 纵向 = 段内行距上限（约 1.5 倍行高），横向 = 同栏判据（重叠占比），
# 文本分隔 = 段内多行拼成条目文本时用什么隔开（界面按行分色，所以要留行界）。
PARA_Y_GAP = 12.0        # 同一段内相邻行允许的纵向空隙 (pt)
PARA_X_OVERLAP = 0.35    # 横向重叠占较窄一块的比例 ≥ 它 才算「同一栏」
PARA_TEXT_SEP = "\n"     # 段内多行在条目文本里的分隔符


def _para_side(rg: Dict[str, Any]) -> str:
    """这一块属于哪一侧 —— 判据是「哪一侧 OCR 认出了字」，与 `_merge_adjacent` 同源。"""
    return "new" if (rg.get("new_text") or "").strip() else "old"


def _merge_paragraphs(regions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 `_refine_region` 切出来的「逐行小块」重新聚成「段」（2026-09-30）。

    为什么要在细切之后再聚一次
    --------------------------
    `_refine_region` 的职责是「只留真正变了的那几行」，代价是把一整段切成逐行小块。
    可**转曲文字本来就是整段一起变的**，逐行切开零信息量，只是把一件事拆成七件：
      · 标注版上是 7 条斑马纹 —— 用户原话「标的还比较分散」；
      · 清单里是 7 条孤立的 OCR 短行 —— 用户原话「简直有点惜字如金，把上下文再多写一点」。
    聚段之后：标注是一个整框（更醒目），清单是一条带**完整段落**的条目。

    判据（两条都要满足，缺一不可）
    ------------------------------
    · **同页同侧** —— 一红一绿绝不合并；
    · **纵向贴邻**（空隙 ≤ `PARA_Y_GAP`）**且横向同栏**（重叠 ≥ 较窄一块的
      `PARA_X_OVERLAP`）—— 少了横向这条，左栏的「一个再折叠工具」（x≈59）
      会被链式并进中栏那一大段（x≈444）：DOC-1007 实测两块横向差 385pt、
      纵向也只差 40pt，只按纵向判会并成一个横跨大半个页面的巨框。

    合并不改判定，只改**粒度** —— 块的集合还是 `_refine_region` 认定的那些行。
    """
    if not regions:
        return []
    # ⚠️ **mod 块不参与聚段**（v40 实测）：mod 两侧都有字（old/new 是「旧→新」
    # 结构），`_para_side` 却按「哪侧有字」把它当 new 侧 —— 与贴邻的 new 块
    # 合并后 `old_text` 被覆盖，REF 的 `150-200→300-200` 就这样被 UDI mod 吞掉
    # （框还是那个框、清单里只剩 UDI 数字）。mod 是行级精确定位 + 两侧互证，
    # 本来就不需要聚段 —— 聚段是为「转曲整段增删」设计的。
    def _is_mod(rg: Dict[str, Any]) -> bool:
        return bool((rg.get("old_text") or "").strip()) and \
               bool((rg.get("new_text") or "").strip())

    out: List[Dict[str, Any]] = []
    for rg in sorted(regions, key=lambda x: (x["page"], _para_side(x),
                                             x["rect"][1], x["rect"][0])):
        r = rg["rect"]
        if _is_mod(rg):
            out.append(dict(rg, _rows=[(r[1], rg.get("new_text") or "")]))
            continue
        hit = None
        for m in out:
            if _is_mod(m):
                continue
            if m["page"] != rg["page"] or _para_side(m) != _para_side(rg):
                continue
            mr = m["rect"]
            y_ok = (r[1] <= mr[3] + PARA_Y_GAP) and (r[3] >= mr[1] - PARA_Y_GAP)
            ov = min(r[2], mr[2]) - max(r[0], mr[0])
            x_ok = ov >= PARA_X_OVERLAP * max(min(r[2] - r[0], mr[2] - mr[0]), 1.0)
            if y_ok and x_ok:
                hit = m
                break
        if hit is None:
            out.append(dict(rg, _rows=[(r[1], rg.get("new_text") or rg.get("old_text") or "")]))
            continue
        mr = hit["rect"]
        hit["rect"] = (min(mr[0], r[0]), min(mr[1], r[1]),
                       max(mr[2], r[2]), max(mr[3], r[3]))
        hit["_rows"].append((r[1], rg.get("new_text") or rg.get("old_text") or ""))

    for m in out:
        rows = sorted(m.pop("_rows"), key=lambda t: t[0])
        txt = PARA_TEXT_SEP.join(t[1].strip() for t in rows if t[1].strip())
        m["new_text" if _para_side(m) == "new" else "old_text"] = txt
    out.sort(key=lambda x: (x["page"], x["rect"][1], x["rect"][0]))
    return out


def _ink_count(pm, rect, scale: float) -> int:
    """一块矩形里的「墨水」像素数（灰度 < 200 算有墨）——用来判「这边是不是空白」。"""
    x0 = max(0, int(rect[0] * scale))
    x1 = min(pm.width, int(rect[2] * scale))
    y0 = max(0, int(rect[1] * scale))
    y1 = min(pm.height, int(rect[3] * scale))
    n = 0
    for y in range(y0, y1):
        row = y * pm.stride
        for v in pm.samples[row + x0:row + x1]:
            if v < SCAN_INK_DARK:
                n += 1
    return n


_SHIFT_STRIDE = 4          # 配准掩膜降采样步长（性能：4 即面积 ÷16；配准只需「够像」，
                           # 不必逐像素，墨迹粗到 4px 采样仍能对齐）
_SHIFT_OVERLAP_MIN = 0.60  # 源墨迹被目标墨迹覆盖到这个比例 → 判「只是挪位」
_SHIFT_TEXT_MIN = 0.50     # 位移配准命中后，配准位置 OCR 文本与源文本的**最低覆盖**。
                           # 低于它 = 配准撞到的其实是别的内容（内容替换，非位移）。
                           # 2026-10-13 实测：11vs12「一个再折叠工具」配准到旧版
                           # 「冠状动脉刻痕球囊」行，_coverage 0.0；真位移的文本
                           # 覆盖率 0.8+。0.5 是「OCR 抖动容忍」与「区分替换」的折中。
_SHIFT_MAX_DY_PX = 300     # 纵向搜索半径上限（像素，@ GRAPHIC_DPI ≈150pt）：
                           # 排版位移再大也不会超过这个量级；无上限时大块配准
                           # 的 rows_b 提取会扫到整页高（实测 18s/块）。
_SHIFT_MAX_DX_PX = 72      # 横向搜索半径（像素 @ GRAPHIC_DPI ≈43pt）：
                           # 2026-10-12 失败4 实测段落「下移 44pt + 左移 26pt」的
                           # **斜向位移**——纯纵向搜索从原理上就对不齐（ov 恒
                           # 0.35~0.40），假新增就是这么漏成绿块的。两段式搜索：
                           # 纵向够门槛直接走（主路径零额外代价），不够才付
                           # 二维细化的钱（dx 档位 ≈36，实测单块 <0.5s）。
# —— 出口位移救援的 OCR 覆盖门槛（2026-10-12 失败4）——
# 像素配准对**文字行**没有判别力（实测错位 137pt 的不同行能在 stride 采样下
# 撞出 ov 0.58~0.65 的假峰），所以「是不是位移」的判定改用 OCR 覆盖判据：
# 有墨侧的 OCR 文本，在对面「就近块 + 整条栏」里覆盖到 ≥ 这个比例 = 内容还在
# 对面 = 只是挪位。像素配准只退居「估算位移量（dx,dy）」的次要角色，不再参与判定。
_RESCUE_BAND_COVER = 0.80


def _moved_rescue_regions(regions, old_path: str, new_path: str,
                          dpi: int = GRAPHIC_DPI):
    """出口统一「位移救援」（2026-10-12 失败4 DOC-M2002）。

    病根：`_decide_region` 的位移守卫只护**像素管线**的候选；**行对拍**
    管线给出的单侧区域没有这道守卫 —— 段落斜向位移时行对拍只看到「新位置
    有字、旧位置空白」，报成假新增（'Coronary I' 整段绿块就是这么来的）。

    做法（2026-10-12 改判据）：**判定**用 OCR 覆盖（可靠），**定位**用像素配准
    （只借它的位移量）。原因：像素配准对文字行**没有判别力**（错位 137pt 的
    不同行能撞出 ov 0.58~0.65 的假峰），不能再拿 ov ≥ 门槛当「是不是位移」的
    判据；而 OCR 覆盖（有墨侧文本在对面就近块/整条栏里覆盖 ≥ `_RESCUE_BAND_COVER`）
    与 `_decide_region` 同一判据、对文字有真判别力。
      ① 有墨侧 `txt` 在对面 `_ocr_near` / `_ocr_band` 覆盖 ≥ 门槛 → 判「只是挪位」；
      ② 再用 `_local_shift_overlap` 跑一次拿 (dx,dy) **填 rect_shift**（定位）；
         像素配准拿不出可信位移（ov 很低）时退化为 rect_shift = rect（浅蓝框
         两侧原位、不标偏移）。
    —— 就地转 moved 区域（`rect`/`rect_shift`/`shift_src`，与 `_decide_region`
    的 "moved" 决策同一份 dict 契约），标记层画浅蓝、清单层出「位置移动」。

    ⚠️ 只碰**单侧**区域：mod（两侧都有字）有两侧互证，不是位移救援的对象；
    `[图形]` 占位符（纯色块增删）没有「内容相同」可言，也不碰。
    同一对增删两侧各转成 moved（rect/rect_shift 互换）→ 末尾按互换关系去重。
    """
    if not regions:
        return regions
    need = [r for r in regions
            if not r.get("moved")
            and bool((r.get("old_text") or "").strip())
            != bool((r.get("new_text") or "").strip())
            and (r.get("old_text") or r.get("new_text") or "").strip()
            != GRAPHIC_SOLID_TAG]
    if not need:
        return regions
    try:
        da, db = fitz.open(old_path), fitz.open(new_path)
    except Exception:
        return regions
    try:
        scale = dpi / 72.0
        pms: Dict[int, Any] = {}
        for r in need:
            i = int(r.get("page", 0))
            side = "old" if (r.get("old_text") or "").strip() else "new"
            # ⚠️ 先做 OCR 覆盖**判定**：有墨侧文本在对面「就近块 + 整条栏」里
            #    覆盖达标 = 内容还在对面 = 只是挪位（真增删则对面没有这段字）。
            txt = (r.get("old_text") if side == "old"
                   else r.get("new_text") or "").strip()
            rect = fitz.Rect(*r["rect"])
            other = db[i] if side == "old" else da[i]
            near = _ocr_near(other, rect, (new_path if side == "old"
                                           else old_path), i)
            band = _ocr_band(other, rect, cache_key=(
                (new_path if side == "old" else old_path), i, "rescue-band",
                int(rect[0] // 40), int(rect[2] // 40)))
            # ⚠️ 判定「这段字在不在对面」分两档：
            #   · 归一化 ≥ OCR_MIN_LEN → `_coverage`（容忍少量 OCR 错字）；
            #   · 4~OCR_MIN_LEN 字符 → `_coverage` 恒返 0（短于门槛），改用
            #     **最长前缀命中**判：ne 的前 k 字符逐段缩短，找能在对面
            #     归一化文本里**原样出现**的最长前缀，k ≥ 6 且占 ne ≥ 门槛
            #     才算命中。为什么是前缀而不是子串：区域细切截断的 OCR 噪声
            #     几乎全落在**尾部**（失败4 'Coronary I' 实为 'Coronary D…'
            #     的误读，'coronaryi' 作子串在对面 'coronarydilatation…'
            #     里永远找不到 —— 后面跟的是 d 不是 i；而前缀 'coronary'
            #     稳稳命中）。6 字符下限挡住「rev02 前缀撞 rev01」这类
            #     短真改动的误救（'rev0' 只有 4 字符）。
            ne = _norm_cjk(txt)
            if len(ne) >= OCR_MIN_LEN:
                hit = (_coverage(txt, near) >= _RESCUE_BAND_COVER
                       or _coverage(txt, band) >= _RESCUE_BAND_COVER)
            elif len(ne) >= 4:
                def _prefix_cover(needle: str, hay: str) -> float:
                    for k in range(len(needle), 5, -1):
                        if needle[:k] in hay:
                            return k / len(needle)
                    return 0.0
                hit = (_prefix_cover(ne, _norm_cjk(near)) >= _RESCUE_BAND_COVER
                       or _prefix_cover(ne, _norm_cjk(band)) >= _RESCUE_BAND_COVER)
            else:
                hit = False
            if not hit:
                continue                       # 对面没有这段字 → 真增删，不救
            # ---- 判定为「挪位」；下面只借像素配准的位移量做定位 ----
            if i not in pms:
                try:
                    pms[i] = (
                        da[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY,
                                         annots=False),
                        db[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY,
                                         annots=False))
                except Exception:
                    pms[i] = None
            pair = pms.get(i)
            dx = dy = 0.0
            if pair:
                pa, pb = pair
                src_pm, dst_pm = (pa, pb) if side == "old" else (pb, pa)
                ov, pdx, pdy = _local_shift_overlap(src_pm, dst_pm, rect, scale)
                if ov >= _SHIFT_OVERLAP_MIN:   # 位移量可信才采用
                    dx, dy = pdx / scale, pdy / scale
            r["moved"] = True
            r["shift_src"] = side
            r["rect_shift"] = (rect[0] + dx, rect[1] + dy,
                               rect[2] + dx, rect[3] + dy)
    finally:
        da.close()
        db.close()
    # —— 去重：同一对「删+增」两侧都被救成 moved 时，两条的 rect/rect_shift
    #    正好互换（同一件事报两遍）—— 保留先到的一条。
    out: List[Dict[str, Any]] = []
    for r in regions:
        if r.get("moved"):
            dup = False
            for m in out:
                if not m.get("moved") or m["page"] != r["page"]:
                    continue
                a1, a2 = m["rect"], m["rect_shift"]
                b1, b2 = r["rect"], r["rect_shift"]
                if (abs(a1[0] - b2[0]) < 2 and abs(a1[1] - b2[1]) < 2
                        and abs(a2[0] - b1[0]) < 2 and abs(a2[1] - b1[1]) < 2):
                    dup = True
                    break
            if dup:
                continue
        out.append(r)
    return out


def _pm_gray_array(pm):
    """pixmap → 2D uint8 数组（行首按 stride 截齐）。

    ⚠️ 2026-10-19 性能：Python 里 `pm.samples[row + x]` 每次都是「属性 + 索引 +
    字节装箱」，几十万次就是十几秒（DOC-1009 剖析里 `samples` **621800 次 /
    21.1s**，仅次于模板匹配）。`frombuffer` 一次拿完再 numpy 切片，结果逐字节相同。
    """
    import numpy as _np
    buf = _np.frombuffer(pm.samples, dtype=_np.uint8)
    if pm.stride == pm.width:
        return buf.reshape(pm.height, pm.width)
    # stride > width：右侧有不属于画面的 padding 列，切掉
    return buf.reshape(pm.height, pm.stride)[:, :pm.width]


def _local_shift_overlap(src_pm, dst_pm, rect, scale: float,
                         stride: int = _SHIFT_STRIDE):
    """局部区域**平移**配准：源块在目标页面里滑动，算「墨迹重合度」。

    用途（GPT 指令第四阶段 / 用户「内容叠加 + 位移」设想）：
    当 `_decide_region` 判出「一边基本空白」的增/删候选，但 OCR 文本比对
    因噪声 / 短文本 / 转曲覆盖不了时，用**像素位移配准**兜底：
       · 源块（有墨那侧）在目标页面里平移，算「源墨迹落到目标墨迹上的比例」，
         取最大；
       · 重合度 ≥ `_SHIFT_OVERLAP_MIN` → 内容是同一块、只是被顶下去/挤过来，
         不该报成增删；
       · 怎么移都够不着 → 才是真增删（内容变了配准本来就该失败）。

    搜索策略（2026-10-12 升级为**两段式**）：
      ① 纵向为主（排版位移的绝对主因是「整块被上方内容增减顶下去/挤上来」，
         横向在栏宽固定的版式里几乎不动）—— 复杂度 O(dy×n)，主路径零额外代价；
      ② 纵向不够门槛才做**有界二维细化**（dx ±`_SHIFT_MAX_DX_PX`）——
         斜向位移（失败4：段落因 「Semi」 插行被顶下去同时还左移了半栏）
         只有二维才对得齐。两段合起来复杂度仍有界（O(dy×n + dx×dy×n/16)）。

    ⚠️ rect 太高/太宽（整页量级）不做配准：整页都变，配准说明不了「挪位还是
       增删」，且 rows_b 提取会扫到整页高（性能灾难，实测 20s/块）。交给 OCR。

    返回 `(best_overlap, best_dx, best_dy)`（像素）；best_overlap ∈ [0,1]。
    """
    x0 = max(0, int(rect[0] * scale))
    x1 = min(src_pm.width, int(rect[2] * scale))
    y0 = max(0, int(rect[1] * scale))
    y1 = min(src_pm.height, int(rect[3] * scale))
    w, h = x1 - x0, y1 - y0
    if w <= 4 or h <= 4:
        return 0.0, 0, 0
    # ⚠️ rect 太高/太宽（整页量级）不做配准：整页都变，配准说明不了「挪位还是
    #    增删」，且 rows_b 提取会扫到整页高（性能灾难，实测 20s/块）。交给 OCR。
    if h > _SHIFT_MAX_DY_PX or w > _SHIFT_MAX_DY_PX * 2:
        return 0.0, 0, 0
    dx_rng = min(_SHIFT_MAX_DX_PX, max(stride, w // 2))
    # 源墨迹按行分组（降采样）：每行的墨迹 x 集合 —— 配准按「行集合交集」算，
    #   比逐点 set 查询快一个量级（每轮 dy 只做 H 次集合交集，而非 n 次点查询）。
    # ⚠️ 2026-10-19：取数改 numpy 一次性切片（见 `_pm_gray_array`），
    #    逐像素 `samples[row+x]` 是这里 20s 的来源；采样网格与阈值口径完全不变。
    import numpy as _np
    _sa = _pm_gray_array(src_pm)
    rows_a: Dict[int, Set[int]] = {}
    _mask_a = _sa[y0:y1:stride, x0:x1:stride] < SCAN_INK_DARK
    for _ri, y in enumerate(range(y0, y1, stride)):
        idx = _np.nonzero(_mask_a[_ri])[0]
        if idx.size:
            rows_a[y] = {x0 + int(i) * stride for i in idx}
    if not rows_a:
        return 0.0, 0, 0
    # 搜索范围：纵向扩 rect 高 ×3，但设绝对上限（见 _SHIFT_MAX_DY_PX）
    dy_rng = min(_SHIFT_MAX_DY_PX, max(16, int(h * 3.0)))
    # 目标墨迹按行分组（竖带上下左右各扩搜索半径 —— 二维细化要往旁边挪）
    # ⚠️ 采样网格必须与 rows_a **同余**：rows_a 的 x 网格从 x0 起步、y 网格从
    #    y0 起步（步长 stride）。若 rows_b 从 sx0/sy0 起步直接采样，一旦夹边
    #    （sx0/sy0 被 0 或页边界截断）或 x0/y0 本身非 stride 倍数，两组网格
    #    相差 1~3px —— 点永远对不上、交集恒空，配准静默失效（v48 潜伏 bug，
    #    2026-10-12 失败4 排查时抓到）。所以起点向 x0/y0 的网格对齐。
    sx0 = max(0, x0 - dx_rng)
    sx1 = min(dst_pm.width, x1 + dx_rng)
    sy0 = max(0, y0 - dy_rng)
    sy1 = min(dst_pm.height, y1 + dy_rng)
    gx = (x0 - sx0) % stride
    gy = (y0 - sy0) % stride
    rows_b: Dict[int, Set[int]] = {}
    _sd = _pm_gray_array(dst_pm)
    _mask_b = _sd[sy0 + gy:sy1:stride, sx0 + gx:sx1:stride] < SCAN_INK_DARK
    for _ri, y in enumerate(range(sy0 + gy, sy1, stride)):
        idx = _np.nonzero(_mask_b[_ri])[0]
        if idx.size:
            rows_b[y] = {sx0 + gx + int(i) * stride for i in idx}
    if not rows_b:
        return 0.0, 0, 0
    total = sum(len(v) for v in rows_a.values())

    def _best_1d() -> Tuple[float, int, int]:
        """纵向平移搜索（dx 固定 0）：逐行做 x 集合交集，累加命中墨点数。"""
        bb, bdy = 0.0, 0
        ddy = dy_rng // stride * stride
        for dy in range(-ddy, ddy + 1, stride):
            hit = 0
            for y, xs in rows_a.items():
                ys = y + dy
                if ys in rows_b:
                    hit += len(xs & rows_b[ys])
            ov = hit / total
            if ov > bb:
                bb, bdy = ov, dy
            if bb >= 0.95:
                break
        return bb, 0, bdy

    best, best_dx, best_dy = _best_1d()
    if best >= _SHIFT_OVERLAP_MIN:
        return best, best_dx, best_dy          # 纵向就够了 —— 主路径零额外代价
    # ---- 二维细化：斜向位移（纵向对不齐、但平移 (dx,dy) 能对上）----
    # 目标行的 x 集合按 -dx 平移后与源行交集（源墨迹右移 dx = 目标墨迹左移 dx）。
    ddx = dx_rng // stride * stride
    ddy = dy_rng // stride * stride
    for dx in range(-ddx, ddx + 1, stride):
        if dx == 0:
            continue                            # dx=0 那档 _best_1d 已扫过
        for dy in range(-ddy, ddy + 1, stride):
            hit = 0
            for y, xs in rows_a.items():
                rb = rows_b.get(y + dy)
                if not rb:
                    continue
                # 源点 (x,y) 对上目标点 (x+dx, y+dy)：目标 x 集合统一平移 -dx
                hit += len(xs.intersection([x - dx for x in rb]))
            ov = hit / total
            if ov > best:
                best, best_dx, best_dy = ov, dx, dy
            if best >= 0.95:
                return best, best_dx, best_dy
    return best, best_dx, best_dy



def _graphic_ink_ratio(page, dpi: int = 72) -> float:
    """这一页的墨水有多大比例**不在文字层里**（= 转曲/图形文字占比）。

    用途：页面上没有这种区域时，OCR 完全没必要跑 —— 纯文字页 OCR 一遍要十几秒，
    而多出来的结论一定是「什么都没找到」。实测合成用例页 ratio≈0，
    DOC-1007 新版中栏 ratio 很高，正是该跑 OCR 的那一页。
    """
    pm = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)
    scale = dpi / 72.0
    # ⚠️ 2026-10-19：整页逐像素 Python 循环（150dpi 下 ~200 万次字节访问）改 numpy。
    #    判定口径不变：仍是「灰度 < SCAN_INK_DARK 的像素数」，words 覆盖仍是
    #    **逐词独立累加**（词与词重叠时重复计数，与原实现一致，不做跨词去重）。
    import numpy as _np
    arr = _pm_gray_array(pm)
    ink = int((arr < SCAN_INK_DARK).sum())
    if ink <= 0:
        return 0.0
    cover = 0
    try:
        for w in page.get_text("words"):
            x0 = max(0, int(w[0] * scale)); x1 = min(pm.width, int(w[2] * scale) + 1)
            y0 = max(0, int(w[1] * scale)); y1 = min(pm.height, int(w[3] * scale) + 1)
            if x1 > x0 and y1 > y0:
                cover += int((arr[y0:y1, x0:x1] < SCAN_INK_DARK).sum())
    except Exception:
        return 0.0
    return max(0.0, 1.0 - cover / ink)


def _strip_ws(s: str) -> str:
    """只去空白、**保留标点**。给「文字层里有没有这段」的精确子串判据用。

    ⚠️ 不能用 `_norm_cjk`（它把标点全剥掉）：「DOC-M2001, rev01」和
    「DOC-M2001，rev01」归一化后一字不差，逗号半角→全角这种真改动会被
    当成「挪位」整条撤掉（2026-10-27 实测 M4506 页脚，删0增0）。
    """
    return re.sub(r"\s+", "", s or "")


def _strip_text_layer(page, x0: float, x1: float):
    """「竖带」上**文字层**的全部文本（与 `ocr_strip` 同几何，一比一对照用）。

    读不出返回 None = 说不清 → 调用方**保留原判**（宁可多标，不给撤权）。
    """
    x0 = max(0.0, x0 - 6)
    x1 = min(float(page.rect.width), x1 + 6)
    if x1 - x0 < 60:
        mid = (x0 + x1) / 2
        x0, x1 = mid - 30, mid + 30
    try:
        return page.get_textbox(fitz.Rect(x0, 5, x1, page.rect.height - 5)) or ""
    except Exception:
        return None


# ==================== 6.1：OCR 兜底档位（6.2 起界面不再暴露） ====================
# 只影响「`_ocr_rescue` 这一步做多少」，判定链其余部分与 6.0 完全一致：
#   "full" —— 文字层判据 + OCR 识别（= 6.0 行为）
#   "text" —— 只走文字层判据，**不调 OCR 识别**（**6.2 默认**）
#             转曲页（新版把整段文字画成曲线、文字层读不到）会因此多报「整段删除」
#   "off"  —— 整个兜底关掉（最快，转曲页多报同上）
# 2026-11-02 用户要求：界面上的「OCR 兜底」下拉删掉，档位固定走 text。
# `set_ocr_rescue_mode()` 已删除；档位只由环境变量 PDFDIFF_OCR_RESCUE 决定。
# 命令行：环境变量 PDFDIFF_OCR_RESCUE=full|text|off。
OCR_RESCUE_MODE = os.environ.get("PDFDIFF_OCR_RESCUE", "text").strip().lower()
if OCR_RESCUE_MODE not in ("full", "text", "off"):
    OCR_RESCUE_MODE = "text"

# 最近一次对比的兜底统计（自测用）：OCR 调用次数、OCR 实耗时、被撤销的条目
LAST_OCR_STATS: Dict[str, Any] = {"calls": 0, "secs": 0.0, "revoked": []}




def _reset_ocr_stats() -> None:
    LAST_OCR_STATS["calls"] = 0
    LAST_OCR_STATS["secs"] = 0.0
    LAST_OCR_STATS["revoked"] = []


def _note_revoked(d: LocDiff, why: str) -> None:
    """记下被兜底撤销的条目：why="text" 靠文字层判据，"ocr" 靠 OCR 识别。"""
    txt = (d.old_text if d.kind == "removed" else d.new_text) or ""
    LAST_OCR_STATS["revoked"].append({
        "why": why,
        "kind": d.kind,
        "page": d.page_old if d.page_old is not None else d.page_new,
        "text": txt,
    })


def _ocr_rescue(items: List[LocDiff], old_path: str, new_path: str) -> List[LocDiff]:
    """用 OCR 撤销「文字层缺行」造成的假增删（2026-09-29 实测的 9 处免责声明误标）。

    病根：改版后有一块被**转曲**（矢量曲线，没有文字层）。字符流引擎只认文字层，
    于是「旧版有、新版没有」→ 报**删除** —— 可那段字在新版里明明还在，只是排到下面去了。

    判据：把**对面文档同一条栏**OCR 一遍，这段字还在里面 → 撤掉这条误标。
    这与全局策略一致（**内容挪了位置不算改动**）；OCR 认不出来时**一律保留**
    （宁可多标一处待人工看，也不凭猜测抹掉真改动）。

    ⚠️ 2026-10-27 收窄撤权（原生 PDF「漏掉那么一两个位置」的元凶就在这）：
    以前只要页面墨水占比 ≥2%（一个 logo 就够）就拿「OCR 散字累加覆盖率 ≥0.85」
    撤销条目 —— 真改动只要跟同栏文字共用几个词就会被抹掉。实测两处：
      · DOC-1005 地址行新增「1栋101室，…2栋101室、」被整条撤掉；
      · DOC-1006 页脚 rev01→rev02 的「文件号：DOC-1006rev02」被整条撤掉。
    现在撤权只认两种情形（`tl` = 对面竖带的文字层文本）：
      B. `_strip_ws(txt)` 就在 `_strip_ws(tl)` 里 → 文字层里其实有这段
         （只是拆了行/挪了位），撤。⚠️ 比对保留标点，半角/全角差异是真改动；
      C. OCR 里**整句**在（`_longest_cover ≥ 0.5` 且累加 ≥ 0.85）、而文字层里
         **没有**（`_longest_cover(tl) < 0.5`）→ 只画在曲线里，撤。
    其余一律保留：文字层自己就有一条相似句（页脚 rev01 vs rev02 的最长公共块
    占 95%）= 真改动，OCR 无权推翻文字层。
    """
    if OCR_RESCUE_MODE == "off":
        return items                     # 6.1 自测档：整个兜底不做
    if not ocr_available():
        return items
    need: List[Tuple[str, int, float, float]] = []      # (文件, 页, x0, x1) 待确认要不要 OCR
    plan: List[Tuple[int, str, int]] = []
    for k, d in enumerate(items):
        if d.kind == "removed":
            txt, rects = d.old_text, d.rects_old
            pn = d.page_new if d.page_new is not None else d.page_old
            who = new_path
        elif d.kind == "added":
            txt, rects = d.new_text, d.rects_new
            pn = d.page_old if d.page_old is not None else d.page_new
            who = old_path
        else:
            continue
        if pn is None or not rects or len(_norm_cjk(txt)) < OCR_MIN_LEN:
            continue
        plan.append((k, who, pn))
        need.append((who, pn, min(r[0] for r in rects), max(r[2] for r in rects)))
    if not plan:
        return items
    da, db = fitz.open(old_path), fitz.open(new_path)
    drop: set = set()
    try:
        ratio: Dict[Tuple[str, int], float] = {}
        for k, who, pn in plan:
            doc = db if who == new_path else da
            if not (0 <= pn < doc.page_count):
                continue
            key = (who, pn)
            if key not in ratio:
                ratio[key] = _graphic_ink_ratio(doc[pn])
            if ratio[key] < 0.02:
                continue                 # 这页没有转曲区域 → OCR 无从下手，保留原判
            d = items[k]
            if d.kind == "removed":
                txt, rects = d.old_text, d.rects_old
            else:
                txt, rects = d.new_text, d.rects_new
            x0, x1 = min(r[0] for r in rects), max(r[2] for r in rects)
            tl = _strip_text_layer(doc[pn], x0, x1)
            if tl is None:
                continue                 # 文字层读不出 → 说不清，保留原判
            # B：文字层里就有这段（拆行/挪位）→ 撤。
            # ⚠️ 比对**保留标点**（`_strip_ws`）：半角/全角标点差异是真改动。
            if _strip_ws(txt) and _strip_ws(txt) in _strip_ws(tl):
                drop.add(k)
                _note_revoked(d, "text")
                continue
            tl_long = _longest_cover(txt, tl)
            if tl_long >= OCR_MIN_COVER_LONG:
                continue                 # 文字层有相似长句 = 真改动，OCR 无权撤
            if OCR_RESCUE_MODE == "text":
                continue                 # 6.1 自测档：只走文字层判据，不调 OCR 识别
            _t_ocr = time.perf_counter()
            band = ocr_strip(pn, who, x0, x1, doc[pn])
            LAST_OCR_STATS["calls"] += 1
            LAST_OCR_STATS["secs"] += time.perf_counter() - _t_ocr
            # C：只画在曲线里（OCR 整句在、文字层没有）→ 撤
            if (_coverage(txt, band) >= OCR_MIN_COVER
                    and _longest_cover(txt, band) >= OCR_MIN_COVER_LONG):
                drop.add(k)
                _note_revoked(d, "ocr")
    finally:
        da.close()
        db.close()
    return [d for k, d in enumerate(items) if k not in drop]



# ---------------------------------------------------------------- 自检
if __name__ == "__main__":
    ok = fail = 0

    def chk(name, cond, extra=""):
        global ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name} {extra}")

    def _union_text(path, page_idx, rects):
        if not rects:
            return ""
        xs = [r[0] for r in rects] + [r[2] for r in rects]
        ys = [r[1] for r in rects] + [r[3] for r in rects]
        doc = fitz.open(path)
        try:
            return doc[page_idx].get_textbox(
                fitz.Rect(min(xs), min(ys), max(xs), max(ys))).strip()
        finally:
            doc.close()

    tmp = tempfile.mkdtemp()

    def _mk(path, lines):
        d = fitz.open()
        p = d.new_page()
        for k, ln in enumerate(lines):
            y = ln[0] if isinstance(ln, tuple) else 60 + k * 20
            t = ln[1] if isinstance(ln, tuple) else ln
            # 用内置中文字体：helv 画不出汉字，会让中文用例变成一堆乱码字
            p.insert_text((60, y), t, fontname="china-s", fontsize=11)
        d.save(path)
        d.close()

    # 用例1：改一个字 → 拆成删（旧值）+ 增（新值），各标各的那一侧
    A = os.path.join(tmp, "a.pdf")
    B = os.path.join(tmp, "b.pdf")
    _mk(A, ["Item 9", "Keep same line", "Footer text"])
    _mk(B, ["Item 11", "Keep same line", "Footer text"])
    ds = locate(A, B)
    chk("改一个字拆成删+增两条", len(ds) == 2, f"实际 {len(ds)}")
    if len(ds) == 2:
        d0, d1 = ds
        chk("删的是旧值、增的是新值", d0.kind == "removed" and d1.kind == "added")
        chk("旧侧有矩形", len(d0.rects_old) >= 1)
        chk("新侧有矩形", len(d1.rects_new) >= 1)
        chk(f"旧版标到的正是 9（实际 {_union_text(A,0,d0.rects_old)!r}）",
            _union_text(A, 0, d0.rects_old) == "9")
        chk(f"新版标到的正是 11（实际 {_union_text(B,0,d1.rects_new)!r}）",
            _union_text(B, 0, d1.rects_new) == "11")

    # 用例2：完全相同的两页 → 0 处差异
    _mk(A, ["Alpha", "Beta", "Gamma"])
    _mk(B, ["Alpha", "Beta", "Gamma"])
    chk("完全相同页无差异", len(locate(A, B)) == 0, f"实际 {len(locate(A, B))}")

    # 用例3（关键回归）：两条「长得像但不同」的句子在两份里都没变 → 不误标
    _mk(A, ["PPS 成功率 98.98%", "FAS 成功率 98.99%", "其它不变"])
    _mk(B, ["PPS 成功率 98.98%", "FAS 成功率 98.99%", "其它不变"])
    chk("相似但不变的行不误标", len(locate(A, B)) == 0, f"实际 {len(locate(A, B))}")

    # 用例4：只改了数字 → 拆成删+增，各标各的
    _mk(A, ["PPS 成功率 98.98%", "FAS 成功率 98.99%"])
    _mk(B, ["PPS 成功率 98.98%", "FAS 成功率 99.99%"])
    ds4 = locate(A, B)
    chk("只标到变的数字（拆删+增，共 2 条）", len(ds4) == 2, f"实际 {len(ds4)}")
    if len(ds4) == 2:
        got_a = _union_text(A, 0, ds4[0].rects_old)
        got_b = _union_text(B, 0, ds4[1].rects_new)
        chk(f"只标到变的那一位 8（实际 {got_a!r}）", got_a == "8")
        chk(f"只标到变的那一位 9（实际 {got_b!r}）", got_b == "9")

    # 用例5：整段换位（内容不变，只换了顺序）→ 位移（浅蓝），不刷红/绿/黄
    _mk(A, ["Header", "第一段很长的内容完全没变", "第二段也没变", "Tail"])
    _mk(B, ["Tail", "第二段也没变", "第一段很长的内容完全没变", "Header"])
    _d5 = locate(A, B)
    chk("整段换位 → 只报 moved（位移），不刷增删改（2026-10-12 位移标蓝）",
        _d5 and all(d.kind == "moved" for d in _d5),
        f"实际 {[(d.kind) for d in _d5]}")

    # 用例6：整行改写（内容毫不相似）→ 报成「删+增」，两侧文字都还原得出来
    _mk(A, ["Same one", "Only in old"])
    _mk(B, ["Same one", "完全不同的新句子 xyz"])
    ds6 = locate(A, B)
    chk("整行改写有差异", len(ds6) >= 1, f"实际 {len(ds6)}")
    chk("改写拆成删+增（不再有修改档）",
        {d.kind for d in ds6} == {"removed", "added"},
        f"实际 {[d.kind for d in ds6]}")
    chk("旧侧文字拼起来还是 Only in old",
        "".join(d.old_text for d in ds6) == "Onlyinold",
        f"实际 {''.join(d.old_text for d in ds6)!r}")
    chk("新侧文字拼起来还是 完全不同的新句子xyz",
        "".join(d.new_text for d in ds6) == "完全不同的新句子xyz",
        f"实际 {''.join(d.new_text for d in ds6)!r}")

    # 用例7（关键回归）：一行被拆成两行 → 只标真正变了的字
    _mk(A, [(60, "文件号：DOC-1007 Rev06/DCR 修订日期：2025年12月19日")])
    _mk(B, [(60, "文件号：DOC-1007 Rev07"), (80, "修订日期：2026年05月27日")])
    ds7 = locate(A, B)
    marked_old = "".join(d.old_text for d in ds7)
    marked_new = "".join(d.new_text for d in ds7)
    chk("拆行后旧侧标到版本号那个 6", "6" in marked_old, f"旧侧标到 {marked_old!r}")
    chk("拆行后新侧标到版本号那个 7", "7" in marked_new, f"新侧标到 {marked_new!r}")
    chk("未变部分『文件号：DOC-1007 Rev0』没被标",
        "文件号" not in marked_old and "文件号" not in marked_new)
    chk("未变部分『修订日期：』没被标",
        "修订日期" not in marked_old and "修订日期" not in marked_new)

    # 用例8：跨页移动 + 改写 → 只在改写处标，不移位处不标
    _mk(A, [(60, "固定一行 AAA"), (100, "会挪到下一页的内容 XYZ")])
    _mk(B, [(100, "会挪到下一页的内容 XYZ"), (60, "固定一行 AAA"), (140, "新增的一行 BBB")])
    ds8 = locate(A, B)
    # "固定一行 AAA" 与 "会挪到下一页的内容 XYZ" 都原样存在 → 抵消；
    # 只剩 "新增的一行 BBB" → added
    kinds8 = sorted(d.kind for d in ds8)
    chk("跨页移动不误标，只标新增", kinds8 == ["added"], f"实际 {kinds8}")

    # 用例9：页眉页脚可忽略
    _mk(A, [(18, "HEADER-A"), (300, "Body same"), (825, "FOOT-A")])
    _mk(B, [(18, "HEADER-B"), (300, "Body same"), (825, "FOOT-B")])
    chk("不忽略页眉页脚时判 2 处改（拆成删+增共 4 条）", len(locate(A, B)) == 4)
    chk("忽略页眉页脚后为 0 处",
        len(locate(A, B, drop_header_footer=True)) == 0)

    # 用例10（关键回归 · 用户实测打回来的病根）：重新折行 / 并成一行 → 不算改动
    # 旧版：两行 + 项目符号；新版：并成一行且没有项目符号。文字内容一字没变。
    _mk(A, [(60, "• 本试验主要有效性指标是手"), (80, "术成功率（Procedure Success Rate）。")])
    _mk(B, [(60, "本试验主要有效性指标是手术成功率（Procedure Success Rate）。")])
    chk("折行/并行（只少个项目符号）不误标",
        len(locate(A, B)) == 0, f"实际 {len(locate(A, B))}")

    # 用例11（关键回归）：旧版的孤立行被新版并进长行 → 那串字不该被标
    _mk(A, [(60, "文件号：DOC-1007 Rev06"), (80, "示例医疗"), (300, "正文不变")])
    _mk(B, [(60, "文件号：DOC-1007 Rev07"),
            (80, "修订日期：2026年05月27日  示例医疗"), (300, "正文不变")])
    ds11 = locate(A, B)
    marked11 = "".join(d.old_text + d.new_text for d in ds11)
    chk("被并进长行的『示例医疗』不被误标", "示例医疗" not in marked11,
        f"实际标到 {marked11!r}")
    chk("版本号 6→7 仍被标出", "6" in marked11 and "7" in marked11)

    # 用例12（关键回归）：地址块只「插入」了短语 → 只有新侧该有标记，旧侧一处都不该标
    _mk(A, [(60, "生产地址：示例路1号1栋101室、101室，2栋101室")])
    _mk(B, [(60, "生产地址：示例路1号1栋101室，2栋101室、101室，3栋101室")])
    ds12 = locate(A, B)
    chk("纯插入时旧侧一处不标",
        all(not d.rects_old for d in ds12), f"实际旧侧标记 {[d.old_text for d in ds12]}")
    chk("纯插入时新侧标出被插入的『1栋101室，』",
        any("1栋101室" in d.new_text for d in ds12),
        f"实际新侧标记 {[d.new_text for d in ds12]}")

    # 用例13（关键回归）：转曲块「按行细切」（`_split_lines`）的两条硬规矩
    #   ① 行距紧到 1px 空白也得切开。这份 PDF 转曲正文的行距实测只有
    #      0.8 / 1.2 / 1.8 / 2.4pt，`GRAPHIC_LINE_GAP` 稍大就把两行粘成一行，
    #      两行的字被混着 OCR → 对面比对不过 → **没变的那行也被标绿**
    #      （用户 2026-09-29 报的「标多了」）。
    #   ② 传了 `anchor` 时只保留与它垂直重叠 ≥ `GRAPHIC_ANCHOR_MIN` 的行 ——
    #      扫描窗口上下外扩是为了完整切出首/末行，但别把隔壁行也圈进来。
    class _FakePM:
        """只给 `_ink_rows` 要的那几个属性，省得真去渲染一张图。"""

        def __init__(self, w, h, bands):
            self.width, self.height, self.stride = w, h, w
            buf = bytearray(b"\xff") * (w * h)
            for y0, y1, x0, x1 in bands:
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        buf[y * w + x] = 0
            self.samples = bytes(buf)

    pm = _FakePM(120, 60, [(10, 16, 10, 60), (18, 24, 10, 60), (26, 32, 10, 60)])
    lines = _split_lines(pm, (0, 0, 120, 60), 1.0)
    chk(f"行距只有 1px 空白也要切出 3 行（实际 {len(lines)}）", len(lines) == 3)
    lines_a = _split_lines(pm, (0, 0, 120, 60), 1.0, anchor=(0, 17, 120, 25))
    chk(f"给了 anchor 后只留中间那行（实际 {len(lines_a)}）", len(lines_a) == 1)

    # 用例14（关键回归 · P0-1 漏检修复）：块级判否 ≠ 块内没有真改动
    #   DOC-1007 左栏实测：块里 1 行真新增 + 2 行只是被顶下去，块级覆盖率
    #   0.93 → 整块判「只是重渲染」→ 真新增那行跟着一起没了。
    #   `_line_rescan` 负责细切到行再判一次。这里用桩替掉 `_decide_region`
    #   （不真调 OCR），验证它的 5 条行为：
    _orig_decide = _decide_region
    _band = (15.0, 26.0)          # 中间那行 = 假想的「真新增」行
    _blk = (10.0, 9.0, 110.0, 33.0)

    def _mk_decide(hit_side, hit_text):
        def stub(pa_, pb_, oa_, ob_, i, rr, scale_, op, np_, ref=None, min_cover=0.0):
            if hit_side and rr[1] >= _band[0] and rr[3] <= _band[1]:
                return (hit_side, hit_text)
            return (None, "")
        return stub

    try:
        _decide_region = _mk_decide("new", "一个再折叠工具")
        got = _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf")
        chk(f"行级捞回：真新增行被捞回 1 条（实际 {len(got)}）", len(got) == 1)
        if got:
            chk("行级捞回：side/文本来自命中那行",
                got[0]["new_text"] == "一个再折叠工具" and not got[0]["old_text"],
                repr(got[0]))
            chk("行级捞回：矩形落在命中行的纵带里",
                got[0]["rect"][1] >= _band[0] - 1 and got[0]["rect"][3] <= _band[1] + 1)

        _decide_region = _mk_decide("new", "字")     # 1 个字 < RESCAN_ROW_MIN_CHARS
        chk("行级捞回：认出的字太少不报（宁缺勿滥）",
            _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf") == [])

        _decide_region = _mk_decide("other", "两边都不该收")   # side 对不上 → 不报
        chk("行级捞回：side 与所在侧不符不报",
            _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf") == [])

        # 两侧对同一行（几何几乎重合、传参相同）各判出一次 → 去重后只剩 1 条
        _calls = {"n": 0}

        def stub_both(pa_, pb_, oa_, ob_, i, rr, scale_, op, np_, ref=None, min_cover=0.0):
            if rr[1] >= _band[0] and rr[3] <= _band[1]:
                _calls["n"] += 1
                return (("new" if _calls["n"] % 2 else "old"), "两侧都认出")
            return (None, "")
        _decide_region = stub_both
        got2 = _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf")
        chk(f"行级捞回：两侧重复行去重后 1 条（实际 {len(got2)}）", len(got2) == 1)

        # 整块只有一行 → 粗判已经看过它，没必要重判
        pm1 = _FakePM(120, 60, [(10, 16, 10, 60)])
        _decide_region = _mk_decide("new", "一个再折叠工具")
        chk("行级捞回：整块一行时直接跳过",
            _line_rescan(pm1, pm1, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf") == [])

        _GRAPHIC_LINE_RESCAN = GRAPHIC_LINE_RESCAN
        GRAPHIC_LINE_RESCAN = False
        try:
            _decide_region = _mk_decide("new", "一个再折叠工具")
            chk("行级捞回：开关关闭时返回空",
                _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf") == [])
        finally:
            GRAPHIC_LINE_RESCAN = _GRAPHIC_LINE_RESCAN
    finally:
        _decide_region = _orig_decide

    # 用例15（关键回归 · P1 跨栏并块）：`_merge_adjacent` 识别「栏间通道」
    #   DOC-1007 栏间距 25.8pt < x_gap=30 → 中栏块与右栏块被并成跨栏巨框，
    #   标注框横盖两栏、OCR 还把右栏的字认进中栏条目。判据：两块之间的
    #   竖直通道整页几乎无文字层瓦片 = 栏间通道 → 不并。
    _rg = lambda x0, y0, x1, y1, side="new": (   # noqa: E731
        {"page": 0, "rect": (x0, y0, x1, y1),
         "old_text": "" if side == "new" else "旧", "new_text": "新" if side == "new" else ""})
    # 模拟一页 640×480pt、tile=24pt、scale=1：文字层瓦片在 x<528（左栏）与
    # x≥576（右栏），x∈[552,576] 的竖直带从页顶到页底无字 = 栏间通道
    _tiles = ({(tx, ty) for tx in range(0, 22) for ty in range(0, 20)}
              | {(tx, ty) for tx in range(24, 27) for ty in range(0, 20)})
    _mid = _rg(440.0, 100.0, 548.0, 145.0)       # 左栏一块
    _right = _rg(573.8, 113.7, 640.0, 145.0)     # 右栏一块（横向间隙 25.8pt < x_gap）
    _no_tile = None
    chk("跨栏：不传瓦片时保持旧行为（间隙 25.8 < 30 会并）",
        len(_merge_adjacent([dict(_mid), dict(_right)])) == 1)
    chk("跨栏：通道整页无字 → 判为栏间通道",
        _channel_is_gutter(_tiles, 548.0, 573.8, 20, 24, 1.0))
    chk("跨栏：栏内间隙带（上下有正文行）不判为通道",
        not _channel_is_gutter(_tiles, 300.0, 330.0, 20, 24, 1.0))
    got3 = _merge_adjacent([dict(_mid), dict(_right)], tiles=_tiles, tile=24,
                           scale=1.0, page_rows=20)
    chk("跨栏：传瓦片后栏间通道挡住合并（2 块各自成段）", len(got3) == 2,
        f"实得 {len(got3)} 块")
    # 栏内两块：间隙带上下有文字层瓦片 → 照常并
    _a2 = _rg(100.0, 100.0, 200.0, 130.0)
    _b2 = _rg(214.0, 100.0, 300.0, 130.0)        # 间隙 14pt，带在左栏内部（有瓦片）
    got4 = _merge_adjacent([dict(_a2), dict(_b2)], tiles=_tiles, tile=24,
                           scale=1.0, page_rows=20)
    chk("跨栏：栏内间隙照常并（1 块）", len(got4) == 1, f"实得 {len(got4)} 块")
    # 空瓦片集合：每页全空 → 视作无信息，保持旧行为（不判通道）
    chk("跨栏：空瓦片集合不启用通道判据",
        len(_merge_adjacent([dict(_mid), dict(_right)], tiles=set(), tile=24,
                            scale=1.0, page_rows=20)) == 1)

    # ---- 2026-09-30 新增：转曲段聚段 + 最长连续覆盖 ----
    # ① 聚段：同栏相邻行并成一段、段内多行拼成完整文本；跨栏 / 跨色绝不并
    _segs = _merge_paragraphs([
        {"page": 0, "rect": (444.0, 100.0, 600.0, 108.0), "old_text": "", "new_text": "第一行"},
        {"page": 0, "rect": (444.0, 108.0, 600.0, 116.0), "old_text": "", "new_text": "第二行"},
        {"page": 0, "rect": (60.0, 100.0, 200.0, 108.0), "old_text": "", "new_text": "左栏"},
    ])
    chk("聚段：同栏相邻行并成一段、跨栏各自成段", len(_segs) == 2, f"实得 {len(_segs)} 段")
    _mid = [s for s in _segs if s["rect"][0] > 400]
    chk("聚段：段内多行拼成完整文本（不再惜字如金）",
        bool(_mid) and _mid[0]["new_text"] == "第一行\n第二行",
        repr(_mid[0]["new_text"] if _mid else None))
    chk("聚段：一红一绿不合并（块数）", len(_merge_paragraphs([
        {"page": 0, "rect": (0, 0, 100, 10), "old_text": "旧", "new_text": ""},
        {"page": 0, "rect": (0, 10, 100, 20), "old_text": "", "new_text": "新"},
    ])) == 2)

    # ② 最长连续覆盖：把「碰巧共用几个词的不同句子」和「同一句话」分开
    _a = "用充压装置使球囊导管产生负压，并保持；"
    _b = "使用充压装置产生负压使球囊内压力完全卸载（卸压终点为球囊工作长度区域基本无液体）"
    chk("最长连续：散落相同词判为不同句（实测 0.29）",
        _longest_cover(_a, _b) < 0.45, f"实得 {_longest_cover(_a, _b):.2f}")
    chk("最长连续：整句挪位判为同一句（实测 1.00）",
        _longest_cover(_a, "前文随便什么" + _a + "后文") > 0.9,
        f"实得 {_longest_cover(_a, '前文随便什么' + _a + '后文'):.2f}")
    chk("累加覆盖率比最长连续虚高（这道闸门存在的理由）",
        _coverage(_a, _b) > _longest_cover(_a, _b) * 1.5,
        f"累加 {_coverage(_a, _b):.2f} vs 最长 {_longest_cover(_a, _b):.2f}")

    # ⑨ 展示用正文的「行首符号清除」（2026-09-30 横向放宽后引入）
    chk("行首符号：逐行 OCR 没这个符号 → 去掉带进来的 `-`",
        _strip_lead_mark("- 如果需要再次插入该球囊导管", "知果需要再次插入该球囊导管")
        == "如果需要再次插入该球囊导管")
    chk("行首符号：`- ` 连空格一起清掉",
        _strip_lead_mark("-  检查球囊导管", "含查球宫导管") == "检查球囊导管")
    chk("行首符号：本行真有符号 → 原样保留",
        _strip_lead_mark("• 用充压装置使球囊导管产生负压", "• 用充压装置使球囊导管产生负压")
        == "• 用充压装置使球囊导管产生负压")
    chk("行首符号：普通汉字行不动",
        _strip_lead_mark("球囊导管，使用再折叠工具", "求囊导管，使用再折叠工具")
        == "球囊导管，使用再折叠工具")
    chk("行首符号：没有参照文本时不动（宁可留着）",
        _strip_lead_mark("- 检查球囊导管", "") == "- 检查球囊导管")

    # ⑩ 归一化：大写字母必须保留（2026-09-30 修「先过滤后 lower」丢大写的 bug）
    chk("归一化：大写拉丁字母不丢（DOC-1002 Rev02）",
        _norm_cjk("DOC-1002 Rev02") == "gra3634rev02",
        f"实得 {_norm_cjk('DOC-1002 Rev02')!r}")
    chk("归一化：驼峰词不丢（BrosMed）",
        _norm_cjk("BrosMed") == "brosmed", f"实得 {_norm_cjk('BrosMed')!r}")
    chk("归一化：大写西里尔折叠后保留（РЗН→pзh）",
        _norm_cjk("РЗН") == "pзh", f"实得 {_norm_cjk('РЗН')!r}")

    # ⑪ 行内修改判据（token 级对齐，DOC-1002 rev02→rev03 的 6 对实测定案）
    chk("mod worthy：注册证日期整行（真改 ✓）",
        _mod_worthy("Регистрационное удостоверение Nº РЗН 2023/20012 от 06.04.2023",
                    "Регистрационное удостоверение Nº РЗН 2023/20012 от 23.03.2026"))
    chk("mod worthy：短版本号行（真改 ✓）",
        _mod_worthy("DOC-1002 Rev02", "DOC-1002 Rev03"))
    chk("mod worthy：同形字母抖动（噪声 ✗）",
        not _mod_worthy("опустимое давление 22 атм. H 8омм EF 820-030080-150",
                        "опустимое давление 22 атм. H 80мм EF 820-030080-150"))
    chk("mod worthy：表格行漏一个数（token 数不等 ✗）",
        not _mod_worthy("Давление атм. 8 10 12 НД 14 16 18 20 22 МДД 24 26 28 кМа 800",
                        "Давление атм. 10 12 НД 14 16 18 20 22 МДД 24 26 28 кМа 800"))
    chk("mod worthy：空格漂移导致 token 错位（✗）",
        not _mod_worthy("（01） 0 6958481 451054 （17）260504（10）2305051234",
                        "01） 06958481 451054 （17）260504（10）230505123."))
    chk("mod worthy：字母 vs 数字（Hi-Tech/H1-Tech ✗）",
        not _mod_worthy("SongShan Lake Hi - T Industrial Development Zone",
                        "SongShan Lake H1 - T Industrial Development Zone"))
    chk("mod worthy：纯字母改写不报（保守 ✗）",
        not _mod_worthy("BrosMed Medical", "BrosMed Clinic"))

    # ⑫ 数字换数字原子判据
    chk("digit swap：等长数字串全换 ✓",
        _digit_swap("06042023", "23032026"))
    chk("digit swap：版本号一位换 ✓",
        _digit_swap("rev02", "rev03"))
    chk("digit swap：数字 vs 字母 ✗",
        not _digit_swap("8oмм", "80мм"))
    chk("digit swap：长度不等 ✗",
        not _digit_swap("2305051234", "230505123."))

    # ⑬ 扫描件候选：合成扫描页上能圈出改动行
    # ⚠️ 字号要大：渲染字比扫描字细，11pt 的单数字差异行峰值才 5px，
    #    过不了 SCAN_ROW_PX=6（真实扫描件实测峰值 17）
    A_scan = os.path.join(tmp, "sa.pdf")
    B_scan = os.path.join(tmp, "sb.pdf")
    for path, t in ((A_scan, "DOC-TEST Rev02"), (B_scan, "DOC-TEST Rev03")):
        d_t = fitz.open()
        p_t = d_t.new_page()
        p_t.insert_text((40, 80), t, fontname="china-s", fontsize=26)
        d_t.save(path)
        d_t.close()
    da_t, db_t = fitz.open(A_scan), fitz.open(B_scan)
    sc = GRAPHIC_DPI / 72.0
    pa_t = da_t[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    pb_t = db_t[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    wins_t = _scan_windows(pa_t, pb_t, sc)
    rects_t = _scan_seed_rects(pa_t, pb_t, *(wins_t[0] if wins_t else (0, 1)), sc)
    chk("扫描候选：合成页能找到差异窗口", len(wins_t) >= 1, f"实得 {len(wins_t)}")
    chk("扫描候选：能出一个候选矩形", len(rects_t) >= 1, f"实得 {len(rects_t)}")
    da_t.close(); db_t.close()

    # ⑮ OCR 乱码闸门（DOC-1003 条码区实测，见 `OCR_JUNK_RULES`）
    chk("乱码闸门：单字碎片串判为乱码（真凶）",
        _ocr_junk("Oe   =    > te   4   Пою        м    о   ь д"))
    chk("乱码闸门：噪声但含两位数 → 放行（表格数字行）",
        not _ocr_junk("8 10 12 14 16 18 20 22"))
    chk("乱码闸门：短文本不判（NEX / GW / 3134 可能是真的）",
        not _ocr_junk("NEX") and not _ocr_junk("GW") and not _ocr_junk("3134"))
    chk("乱码闸门：真编号 token 少 → 放行",
        not _ocr_junk("REF A35-070150-200"))
    chk("乱码闸门：正常句子 → 放行",
        not _ocr_junk("Регистрационное удостоверение от 06.04.2023"))
    chk("乱码闸门：空串不判", not _ocr_junk(""))

    # ⑭ 两页位图尺寸不一致（2026-09-30 图片输入轮：差 1px 就整页静默跳过的 bug）
    A_sz = os.path.join(tmp, "sza.pdf")
    B_sz = os.path.join(tmp, "szb.pdf")      # 只差 0.6pt：渲染取整后差一行
    C_sz = os.path.join(tmp, "szc.pdf")      # 高度砍半：比例真不同
    for _p, _h in ((A_sz, 300.0), (B_sz, 300.6), (C_sz, 150.0)):
        _d = fitz.open()
        _d.new_page(width=200, height=_h)
        _d.save(_p)
        _d.close()
    _da, _db, _dc = fitz.open(A_sz), fitz.open(B_sz), fitz.open(C_sz)
    _pa = _da[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    _pb = _db[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    _pc = _dc[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    chk("尺寸对齐：两页位图确实差了一行（用例前提）",
        (_pa.width, _pa.height) != (_pb.width, _pb.height),
        f"{(_pa.width, _pa.height)} vs {(_pb.width, _pb.height)}")
    _fb, _ = _fit_pixmap(_db[0], _pb, _pa.width, _pa.height, sc)
    chk("尺寸对齐：差一两像素能拉伸成同尺寸",
        _fb is not None and (_fb.width, _fb.height) == (_pa.width, _pa.height),
        f"实得 {None if _fb is None else (_fb.width, _fb.height)}")
    chk("尺寸对齐：比例真不同时不硬拉（宁可跳过也不给错坐标）",
        _fit_pixmap(_dc[0], _pc, _pa.width, _pa.height, sc)[0] is None)
    LAST_SIZE_SKIPS.clear()
    graphic_regions(A_sz, C_sz)
    chk("尺寸对不上的页必须记账（不许静默报 0 差异）",
        len(LAST_SIZE_SKIPS) == 1 and LAST_SIZE_SKIPS[0]["page"] == 0, str(LAST_SIZE_SKIPS))
    LAST_SIZE_SKIPS.clear()
    graphic_regions(A_sz, B_sz)
    chk("只差一两像素的页不该记账", len(LAST_SIZE_SKIPS) == 0, str(LAST_SIZE_SKIPS))
    _da.close(); _db.close(); _dc.close()

    # —— 两侧重读「是不是同一句」`_near_same`（2026-10-01 加，见 NEAR_SAME_RULES）——
    chk("两侧重读：完全相等 = 同一句 → 撤", _near_same("gwkre", "gwkre") is True)
    chk("两侧重读：短串差一个字符 = **不是**同一句（`3134` vs `31234` 是真改动）",
        _near_same("3134", "31234") is False)
    chk("两侧重读：长串上多出 4 字 = 不是同一句（旧版多了 `7omm` 属真删除）",
        _near_same("utionlerallawusarestricts7omm", "utionlerallawusarestricts") is False)
    chk("两侧重读：长串小幅抖动仍算同一句",
        _near_same("theballooncatheterisusedfor", "theballooncatheterisusedfar") is True)
    chk("两侧重读：任一侧为空 = 证据不足，不撤",
        _near_same("", "abc") is False and _near_same("abc", "") is False)
    # —— mod 宽读对账 `_same_text_noise`（2026-10-01，见函数说明）——
    chk("mod宽读对账：尾部纯字母噪声 = 同一句 → 撤（DOC-1003 vs DOC-1003F）",
        _same_text_noise("gra3925", "gra3925f") is True)
    chk("mod宽读对账：差一位数字 = **不是**同一句（UDI 48425→48429 保得住）",
        _same_text_noise("010000000000000000000000000000000001",
                         "010000000000000000000000000000000002") is False)
    chk("mod宽读对账：包含但尾巴带数字 = 不是同一句（7.0mm vs 6.5/7.0mm）",
        _same_text_noise("70mm", "6570mm") is False)

    # —— 假 mod 防线「墨迹覆盖 + 容差」`_ink_mask` / `_uncovered_frac` /
    #    `_px_struct_dist`（2026-10-01 v44，见 SCAN_HDPI_RULES「结构对账」）——
    class _FakePix:
        """只带 `.samples` / `.stride` 的最小灰度位图替身。"""
        def __init__(self, rows):
            self.samples = bytes(b for row in rows for b in row)
            self.stride = len(rows[0])

    chk("墨迹二值化：灰度 < 128 才算墨",
        _ink_mask(_FakePix([[10, 200], [130, 0]]), 2, 2, 128) == [[1, 0], [0, 1]])
    _stroke = [[0, 1, 0], [0, 1, 0], [0, 1, 0]]
    chk("墨迹覆盖：两笔完全重合 = 全被覆盖",
        _uncovered_frac(_stroke, _stroke, 3, 3) == 0.0)
    _shift1 = [[0, 0, 1], [0, 0, 1], [0, 0, 1]]
    chk("墨迹覆盖：整笔偏移 1px = 容差内，仍算同一笔",
        _uncovered_frac(_stroke, _shift1, 3, 3) == 0.0)
    _shift3 = [[0, 0, 0], [0, 0, 0], [1, 1, 1]]
    chk("墨迹覆盖：整笔偏移出容差 = 有笔没对上（> 0）",
        _uncovered_frac(_stroke, _shift3, 3, 3) > 0.0)

    def _mk_scan(path, src_page, dpi):
        """把一页按指定 dpi 渲成整页灰度图再铺回 PDF —— 模拟同内容的两次扫描。"""
        pix = src_page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        dd = fitz.open()
        dd.new_page(width=src_page.rect.width, height=src_page.rect.height).insert_image(
            fitz.Rect(0, 0, src_page.rect.width, src_page.rect.height),
            stream=pix.tobytes("png"))
        dd.save(path)
        dd.close()

    def _mk_word(path, word):
        dd = fitz.open()
        dd.new_page(width=300, height=120).insert_text(
            (20, 60), word, fontname="helv", fontsize=22)
        dd.save(path)
        dd.close()

    _sa, _sb, _sc = (os.path.join(tmp, n) for n in ("s_a.pdf", "s_b.pdf", "s_c.pdf"))
    _mk_word(_sa, "80MM TEST")
    _mk_word(_sb, "80MM TEST")
    _mk_word(_sc, "80MM TEZT")
    _rc = (10, 30, 290, 80)
    _pa, _pb, _pc = fitz.open(_sa)[0], fitz.open(_sb)[0], fitz.open(_sc)[0]
    chk("结构对账：两版同内容 = 距离 0（≤ 阈值 → 不报）",
        _px_struct_dist(_pa, _pb, _rc) <= SCAN_HDPI_STRUCT_MAX)
    chk("结构对账：改 1 个字 = 距离超阈值（→ 报）",
        _px_struct_dist(_pa, _pc, _rc) > SCAN_HDPI_STRUCT_MAX)
    _hi = os.path.join(tmp, "s_hi.pdf")
    _lo = os.path.join(tmp, "s_lo.pdf")
    _mk_scan(_hi, _pa, 600)
    _mk_scan(_lo, _pa, 150)
    _ph, _pl = fitz.open(_hi)[0], fitz.open(_lo)[0]
    chk("结构对账：同一页两次扫描（600dpi vs 150dpi 原生分辨率差一倍）"
        "距离仍 ≤ 阈值 —— DOC-1002 假 mod 的回归用例",
        _px_struct_dist(_ph, _pl, _rc) <= SCAN_HDPI_STRUCT_MAX)
    chk("结构对账：无效矩形 = 1.0（证据不足，不撤）",
        _px_struct_dist(_pa, _pb, (10, 30, 10, 30)) == 1.0)
    _pa.parent.close(); _pb.parent.close(); _pc.parent.close()
    _ph.parent.close(); _pl.parent.close()

    # —— 条带/几何类新判据（2026-10-01 v45~v46 加）———————————————
    # ⚠️ 这几个判据要 OCR，而**合成 PDF 上的小字 OCR 不稳**（实测单字符 `8` 裁切后
    #    读不出、`CE2747` 被读成 `1フコつ`）→ 用**注入式假 OCR** 换掉
    #    `_ocr_crop_lines`：只验几何 + 比对逻辑，确定性、不依赖 Vision。
    _PG = 1000.0                    # 假页宽/高（pt），归一化坐标 = pt / _PG

    def _ln(t, x0, y0, x1, y1):
        return {"t": t, "x0": x0 / _PG, "y0": y0 / _PG,
                "x1": x1 / _PG, "y1": y1 / _PG}

    # ① 字海兜底的容器长度上限（`_hay_token_hit`，见 SCAN_MOVE_HAY_MAX_CONT）
    _hay = [{"t": t} for t in ("150mm", "800", "7.84", "200",
                               "fone1125480365", "importadoedistribudopor",
                               "2026-07-31")]
    chk("字海兜底：`5` 被短条目 `150mm` 兜住 → 撤（DOC-1003 条带）",
        _hay_token_hit("5", _hay) is True)
    chk("字海兜底：`8` 被短条目 `800` 兜住 → 撤（DOC-1003 压力表）",
        _hay_token_hit("8", _hay) is True)
    chk("字海兜底：`00` 被短条目 `200` 兜住 → 撤",
        _hay_token_hit("00", _hay) is True)
    chk("字海兜底：`65`（真新增 `6.5/`）只在超长条目里 → **不撤**",
        _hay_token_hit("65", _hay) is False)
    chk("字海兜底：`dist`（真新增 `Dist.`）只在超长条目里 → **不撤**",
        _hay_token_hit("dist", _hay) is False)
    chk("字海兜底：`conical` 哪都没有 → **不撤**",
        _hay_token_hit("conical", _hay) is False)

    # ② 行切分不对称（`_line_split_explained`，见 NEAR_SPLIT_RULES）
    _LA = [_ln("NP Nominal Pressure", 287, 238, 380, 251),
           _ln("RBP Rated Burst Pressure", 380, 237, 491, 251),
           _ln("14 atm", 416, 247, 460, 258),
           _ln("Balloon Diameter", 399, 263, 462, 270),
           _ln("(01) 0 6958481 48425 0 (17) 290729 (10) 2603181234", 263, 28, 390, 38)]
    _LB = [_ln("ND Nominal Pressure RBD Rated Burst Pressure", 297, 236, 496, 249),
           _ln("12 atm", 417, 245, 463, 258),
           _ln("Conical Balloon Diameter", 332, 265, 448, 274),
           _ln("(01) 0 6958481 48429 8 (17) 290729 (10) 2603181234", 263, 28, 390, 38)]
    chk("行切分不对称：新版把两行表头读成一行 → 撤（DOC-1003 表头假 mod）",
        _line_split_explained(_LA[1], _LA, _PG, _LB[0], _LB, _PG) is True)
    chk("行切分不对称：`14 atm`→`12 atm` 真改 → **不撤**",
        _line_split_explained(_LA[2], _LA, _PG, _LB[1], _LB, _PG) is False)
    chk("行切分不对称：`Balloon Diameter`→`Conical Balloon Diameter` 真改 → **不撤**",
        _line_split_explained(_LA[3], _LA, _PG, _LB[2], _LB, _PG) is False)
    chk("行切分不对称：两行**本来就**同句（UDI 只差两字符）→ 不走这条 → **不撤**",
        _line_split_explained(_LA[4], _LA, _PG, _LB[3], _LB, _PG) is False)

    # ③ 纵向重排 / 行级重读 / 精读并集框 —— 注入式假 OCR（假 OCR 只返回与窗口相交的行）
    _FAKE: dict = {}
    _real_crop_lines = _ocr_crop_lines

    def _fake_crop_lines(page, rect, path, i, tag):
        out = []
        for it in _FAKE.get(str(path), ()):
            b = (it["x0"] * _PG, it["y0"] * _PG, it["x1"] * _PG, it["y1"] * _PG)
            if (min(b[2], rect[2]) - max(b[0], rect[0]) > 0
                    and min(b[3], rect[3]) - max(b[1], rect[1]) > 0):
                out.append(dict(it))
        return out

    _fdoc = fitz.open()
    _fpg = _fdoc.new_page(width=_PG, height=_PG)
    _ap, _bp = "/tmp/_st_a.pdf", "/tmp/_st_b.pdf"
    _rect8 = (310, 320, 322, 328)
    try:
        _ocr_crop_lines = _fake_crop_lines
        _FAKE[_bp] = [_ln("8", 312, 336, 320, 344)]
        chk("纵向重排：对侧**同一列**、差 16pt 处读出同一个 `8` → 撤",
            _near_shift_hit(_fpg, _bp, 0, _rect8, "8") is True)
        _FAKE[_bp] = [_ln("10", 312, 336, 322, 344)]
        chk("纵向重排：挪位处印的是**别的值**（`10`）→ 不撤",
            _near_shift_hit(_fpg, _bp, 0, _rect8, "8") is False)
        _FAKE[_bp] = [_ln("8", 312, 384, 320, 392)]
        chk("纵向重排：同值但差 56pt（超出 ±24pt 扫描范围）→ 不撤",
            _near_shift_hit(_fpg, _bp, 0, _rect8, "8") is False)
        _FAKE[_bp] = [_ln("8", 400, 336, 408, 344)]
        chk("纵向重排：同值但在**隔壁列**（横向差 78pt）→ 不撤",
            _near_shift_hit(_fpg, _bp, 0, _rect8, "8") is False)

        # 行级重读（见 NEAR_LINE_RULES）：整窗读会被邻行污染，候选那一行本身相同
        _FAKE.clear()
        _FAKE[_ap] = [_ln("СЄ2797", 334, 525, 380, 539)]
        _FAKE[_bp] = [_ln("СЄ2797", 333, 520, 404, 541)]
        _rce = (331.9, 518.8, 415.5, 542.4)
        chk("行级重读：候选**自己那一行**两侧读得一字不差 → 撤（DOC-1003 `CE2797当4`）",
            _near_line_hit(_fpg, _fpg, _rce, _ap, _bp, 0) is True)
        _FAKE[_bp] = [_ln("СЄ2747", 333, 520, 404, 541)]
        chk("行级重读：候选那一行两侧**不同**（短串必须全等）→ 不撤",
            _near_line_hit(_fpg, _fpg, _rce, _ap, _bp, 0) is False)
        _FAKE[_bp] = [_ln("СЄ2797", 333, 700, 404, 721)]
        chk("行级重读：对面那一行位置对不上（框不重叠）→ 不撤",
            _near_line_hit(_fpg, _fpg, _rce, _ap, _bp, 0) is False)
        del _FAKE[_bp]
        chk("行级重读：候选侧一行都读不出 → 不撤（证据不足）",
            _near_line_hit(_fpg, _fpg, _rce, _ap, "/tmp/_st_none.pdf", 0) is False)

        # 精读并集框（见 SCAN_HDPI_RULES「并集框」）：两侧读出的行并起来当标注矩形
        _FAKE.clear()
        _FAKE[_ap] = [_ln("150-200", 60, 20, 126, 34)]
        _FAKE[_bp] = [_ln("300-200", 55, 20, 122, 34)]
        _probe = fitz.Rect(50, 10, 140, 40)
        _u = _hdpi_union_box(_fpg, _fpg, _ap, _bp, 0, _probe, _probe,
                             _PG, _PG, _PG, _PG)
        chk("精读并集框：盖住两侧读出行的并集（不只是触发行那一侧）",
            abs(_u[0] - 55.0) < 0.6 and abs(_u[2] - 126.0) < 0.6, f"实得 {_u}")
        _FAKE[_bp] = []
        _u1 = _hdpi_union_box(_fpg, _fpg, _ap, _bp, 0, _probe, _probe,
                              _PG, _PG, _PG, _PG)
        chk("精读并集框：对面一行都没读出 → 用它的精读窗兜底（框只会变大不会缩）",
            abs(_u1[0] - 50.0) < 0.6 and abs(_u1[2] - 140.0) < 0.6, f"实得 {_u1}")
    finally:
        _ocr_crop_lines = _real_crop_lines
        _fdoc.close()

    # —— 「行内改写」整行框收窄（2026-10-01 用户报 DOC-1002 注册证行多标）——
    #   真语料实测：日期实际占行宽 [0.839, 0.988]，字符比例估计 [0.833, 1.0]。
    _sp = _mod_span_frac("Регистрационное удостоверение № РЗН 2023/20012 от 06.04.2023",
                         "Регистрационное удостоверение № РЗН 2023/20012 от 23.03.2026")
    chk("收窄：行尾日期改动给出两侧占比区间（前缀 50/60）", _sp is not None
        and abs(_sp[0][0] - 50 / 60) < 0.01 and abs(_sp[0][1] - 1.0) < 1e-9
        and abs(_sp[1][0] - 50 / 60) < 0.01, str(_sp))
    _sp2 = _mod_span_frac("DOC-1002 Rev02", "DOC-1002 Rev03")
    chk("收窄：Rev 号改动占比 ≈ 末尾字符（前缀 13/14）", _sp2 is not None
        and abs(_sp2[0][0] - 13 / 14) < 0.01 and abs(_sp2[1][0] - 13 / 14) < 0.01,
        str(_sp2))
    chk("收窄：多行 OCR 区域不收窄（字符占比跨行没有意义）",
        _mod_span_frac("REF\nA35-070\n150-200", "REF\nA35-070\n300-200") is None)
    chk("收窄：太短的行不收窄（占比没有意义）",
        _mod_span_frac("abc", "abd") is None)
    chk("收窄：一侧为空不收窄（那是纯删/纯增，不是改写）",
        _mod_span_frac("只有旧版有", "") is None)
    _big = _mod_span_frac("AAAA BBBB CCCC DDDD EEEE",
                          "XXXX YYYY ZZZZ WWWW VVVV")
    chk("收窄：变化过半（大段改写）不收窄", _big is None, str(_big))
    _rg = {"rect": (10.0, 100.0, 210.0, 112.0),
           "rect_a": (10.0, 100.0, 210.0, 112.0),
           "rect_b": (10.0, 100.4, 210.0, 111.6),
           "old_text": "AAAA BBBB 06.04.2023", "new_text": "AAAA BBBB 23.03.2026"}
    _narrow_mod_region(_rg, None, None)
    _w_old = _rg["rect_a"][2] - _rg["rect_a"][0]
    chk("收窄：整行框只留变化片段（前缀 10/20 → 左缘收到 ≈48% 行宽处）",
        _rg["rect_a"][0] > 10.0 + 90.0 and abs(_rg["rect_a"][2] - 210.0) < 0.01
        and 30.0 < _w_old < 120.0,
        f"rect_a={_rg['rect_a']}")
    chk("收窄：纵向不动（只动横向）",
        abs(_rg["rect_a"][1] - 100.0) < 1e-9 and abs(_rg["rect_a"][3] - 112.0) < 1e-9
        and abs(_rg["rect_b"][1] - 100.4) < 1e-9)
    chk("收窄：并集 rect 同步更新且盖住两侧",
        _rg["rect"][0] <= min(_rg["rect_a"][0], _rg["rect_b"][0]) + 1e-9
        and _rg["rect"][2] >= max(_rg["rect_a"][2], _rg["rect_b"][2]) - 1e-9)
    _rg2 = {"rect_a": None, "rect_b": None, "old_text": "A", "new_text": "B"}
    _narrow_mod_region(_rg2, None, None)
    chk("收窄：缺行框时原样返回（不抛错）", _rg2["rect_a"] is None)

    # ---- 同形字折叠（OCR 原文出口，2026-10-11 DOC-1003 用户实测）----
    chk("同形字：西里尔 А 折成拉丁 A（保留大写）",
        _ocr_post("А35-070300-200") == "A35-070300-200", repr(_ocr_post("А35-070300-200")))
    chk("同形字：小写 о 折成拉丁 o（м 小写不是同形字，不动）",
        _ocr_post("8омм") == "8oмм", repr(_ocr_post("8омм")))
    chk("同形字：非同形西里尔字母不动（Ж/Щ/Д 原样）",
        _ocr_post("ЖЩД") == "ЖЩД", repr(_ocr_post("ЖЩД")))
    chk("同形字：空串/None 安全", _ocr_post("") == "" and _ocr_post(None) == "")

    # ---- ≤2 字单侧碎片抑制（2026-10-11 DOC-1003 用户实测 '©'/'00'）----
    _R = lambda x0, y0, x1, y1: {"page": 0, "rect": (float(x0), float(y0), float(x1), float(y1))}
    _regs = [dict(_R(100, 100, 300, 120), old_text="14(RBP)", new_text="12 (RBP)"),
             dict(_R(650, 130, 700, 147), old_text="12", new_text=""),      # 碎片，贴着 mod
             dict(_R(700, 1120, 760, 1136), old_text="1400", new_text=""),  # 4 字：不撤
             dict(_R(900, 1114, 930, 1129), old_text="00", new_text=""),    # 碎片，贴 '1400'
             dict(_R(860, 1140, 920, 1156), new_text="Dist."),              # 5 字：不撤
             dict(_R(400, 1152, 560, 1169), new_text="©"),                  # 碎片，贴 'Dist.'
             dict(_R(50, 600, 90, 612), old_text="GW", new_text="")]        # 孤立短标签：留
    _kept = _frag_suppress(_regs)
    _kt = [(r.get("old_text"), r.get("new_text")) for r in _kept]
    chk("碎片：紧邻大块的 ≤2 字单侧碎片被撤（'12'/'00'/'©'）",
        ("12", "") not in _kt and ("00", "") not in _kt and (None, "©") not in _kt,
        str(_kt))
    chk("碎片：mod 区域原样保留", ("14(RBP)", "12 (RBP)") in _kt)
    chk("碎片：>2 字的单侧文本不撤（'1400'/'Dist.'）",
        ("1400", "") in _kt and (None, "Dist.") in _kt)
    chk("碎片：孤立的短标签不撤（'GW' 可能是真删）", ("GW", "") in _kt)
    chk("碎片：撤完只留 4 条", len(_kept) == 4, f"kept={len(_kept)}")

    # ---- 数字主键 + 同位置守卫（2026-10-11 DOC-1003 表格行/UDI 数字行）----
    chk("主键：行首数字（含小数）", _lead_num("12 (rbp)") == "12"
        and _lead_num("6.5/7.0") == "6.5" and _lead_num("REF abc") == "")
    _L = lambda x, y, t: {"x0": x, "x1": x + 0.10, "y0": y, "y1": y + 0.012, "t": t}
    _A, _B = _L(0.10, 0.80, "(01) 0 6958481 48425 0 (17) 29"), \
             _L(0.10, 0.80, "(01) 0 6958481 48429 8 (17) 29")
    chk("守卫：同位置只差两个数字 = mod 候选，不许按挪位撤",
        _same_pos_mod_partner(_A, 100.0, 100.0, [_B], 100.0, 100.0))
    _C = _L(0.60, 0.30, "(01) 0 6958481 48429 8 (17) 29")   # 相似但位置远
    chk("守卫：相似行在别处 = 真挪位，不拦",
        not _same_pos_mod_partner(_A, 100.0, 100.0, [_C], 100.0, 100.0))
    _D = _L(0.10, 0.80, "完全不同的另一句话")
    chk("守卫：同位置但文本无关 = 不拦（该删还删）",
        not _same_pos_mod_partner(_A, 100.0, 100.0, [_D], 100.0, 100.0))

    # ---- 字体变动检测（2026-10-12 五色体系「深蓝」桶）----
    tmp2 = tempfile.mkdtemp(prefix="pdfdiff_font_")
    FA_f = os.path.join(tmp2, "fa.pdf")
    FB_f = os.path.join(tmp2, "fb.pdf")
    for path, fname in ((FA_f, "helv"), (FB_f, "tiro")):
        d_f = fitz.open()
        p_f = d_f.new_page()
        p_f.insert_text((40, 80), "Same Text Different Font 12345",
                        fontname=fname, fontsize=14)
        d_f.save(path)
        d_f.close()
    _fd = locate(FA_f, FB_f)
    # ⚠️ 2026-10-15 重构3（砍字体桶）：4.0 不再报 kind="font"，字体变化走
    # 「文字相同→抵消」或「渲染层兜底」。这里断言「不报 font 桶」即可，
    # 不再断言具体报什么（同文异字体可能因字宽差异被判 moved，属已知边界）。
    chk("字体：同文异字体 → 不再报 font 桶（4.0 砍字体桶）",
        all(d.kind != "font" for d in _fd),
        str([(d.kind, d.old_text[:12]) for d in _fd]))
    _fd2 = locate(FA_f, FA_f)
    chk("字体：同文同字体 → 零差异", len(_fd2) == 0, str(len(_fd2)))
    chk("字体：签名剥子集前缀（ABCDEE+SimSun → SimSun|11.0）",
        _dominant_font([{"chars": list("abc"), "font": "ABCDEE+SimSun",
                         "size": 11.0}]) == "SimSun|11.0")

    # ---- 全页像素画像（2026-10-13 用户「嵌套对比」第三条引擎）----
    #    边缘都成了差异。这里用纯合成位图验证三件事：能识出位移、方向对、
    #    不相干的图不硬凑。
    import numpy as _np

    class _FP3:
        def __init__(self, arr):
            self.samples = arr.tobytes()
            self.height, self.width = arr.shape
            self.stride = self.width

    _rng = _np.random.RandomState(3)
    # 用**低频块状**图而不是逐像素随机噪声：真实页面是有结构的，随机噪声在
    # phaseCorrelate 里没有稳定峰（实测返回 0，断言会假失败）。
    _base = _np.full((200, 200), 255, _np.uint8)
    for _ in range(42):
        _y, _x = _rng.randint(0, 176), _rng.randint(0, 176)
        _base[_y:_y + _rng.randint(8, 24), _x:_x + _rng.randint(8, 24)] = 0
    _sh = _np.full_like(_base, 255)
    _sh[:, 3:] = _base[:, :-3]              # 内容整体右移 3px
    _, _, _i1 = _fast_denoise_pair(_FP3(_base), _FP3(_sh), dpi=72)
    chk("快速降噪：整页平移 3px 被识出并补偿（方向正确）",
        _i1["applied"] and 2.0 < _i1["dx"] < 4.0,
        str({k: round(v, 2) if isinstance(v, float) else v for k, v in _i1.items()}))
    _noise = _np.full((200, 200), 255, _np.uint8)
    for _ in range(42):
        _y, _x = _rng.randint(0, 176), _rng.randint(0, 176)
        _noise[_y:_y + _rng.randint(8, 24), _x:_x + _rng.randint(8, 24)] = 0
    _, _, _i2 = _fast_denoise_pair(_FP3(_base), _FP3(_noise), dpi=72)
    chk("快速降噪：两张不相干的图不硬凑（gain 或不达）", not _i2["applied"],
        str({k: round(v, 2) if isinstance(v, float) else v for k, v in _i2.items()}))
    _, _, _i3 = _fast_denoise_pair(_FP3(_base), _FP3(_base), dpi=72)
    chk("快速降噪：完全一致的两页不动（applied=False）", not _i3["applied"])
    chk("_GrayArr 能被 _pm_gray_array 原样还原",
        _pm_gray_array(_GrayArr(_base)).shape == _base.shape
        and int(_pm_gray_array(_GrayArr(_base))[10, 10]) == int(_base[10, 10]))

    # ⑧b 紫框的「方向」（2026-10-29 加；2026-11-02 起只决定「标哪一侧」）。
    #     判据只有一条：**独占墨占 55% 以上才定方向**，其余留纯紫不猜。
    #     这里用纯合成灰度数组逐条验，不依赖任何真实样本。
    _pa = _np.full((100, 100), 255, _np.uint8)
    _pb = _np.full((100, 100), 255, _np.uint8)
    _pa[20:40, 20:40] = 0                  # 旧版这一块有墨，新版同一块空白
    _R = (18, 18, 42, 42)
    chk("紫框方向：旧版独占这块墨 → del（只标旧版）",
        _pending_dir(_pa, _pb, _R, 1.0) == "del", _pending_dir(_pa, _pb, _R, 1.0))
    chk("紫框方向：新版独占这块墨 → add（只标新版）",
        _pending_dir(_pb, _pa, _R, 1.0) == "add", _pending_dir(_pb, _pa, _R, 1.0))
    chk("紫框方向：两侧墨一模一样（只是没配准好）→ 留紫",
        _pending_dir(_pa, _pa, _R, 1.0) == "")
    _mix = _np.full((100, 100), 255, _np.uint8)
    _mix[20:40, 20:30] = 0                 # 新版只剩左半 → 旧版独占右半 = 50%
    chk("紫框方向：独占量不到 55%（50%）→ 留紫，不硬猜",
        _pending_dir(_pa, _mix, _R, 1.0) == "")
    _few = _np.full((10, 10), 255, _np.uint8)
    _few[2:4, 2:4] = 0                     # 4 个墨点 < FAST_DIR_MIN_PX
    chk("紫框方向：墨量太少（4px）不猜",
        _pending_dir(_np.full((10, 10), 255, _np.uint8), _few, (0, 0, 10, 10), 1.0) == "")
    chk("紫框方向：拿不到灰度数组时不炸、返回空",
        _pending_dir(None, None, (0, 0, 10, 10), 1.0) == ""
        and _pending_dir(_pa, _pb, (500, 500, 600, 600), 1.0) == "")
    chk("紫框方向：只给四档 del/add/moved/空（界面按这四档上色）",
        {_pending_dir(_pa, _pb, _R, 1.0), _pending_dir(_pb, _pa, _R, 1.0),
         _pending_dir(_pa, _pa, _R, 1.0)} == {"del", "add", ""}
        and _pending_dir(_pa, _pb, _R, 1.0) in ("del", "add", "moved", ""))

    # ⑧c 紫框里「其实是位移」的那一档（2026-10-30 晚用户要求：能判出来的标蓝、
    #     与字符流位移同一个开关，默认不显示）。判据是**平移后双向墨重叠**，
    #     这里用合成灰度数组逐条验，不依赖真实样本。
    def _blk(canvas, x, y, w=18, h=12, v=0):
        canvas[y:y + h, x:x + w] = v

    _sa = _np.full((120, 160), 255, _np.uint8)
    _sb = _np.full((120, 160), 255, _np.uint8)
    _blk(_sa, 30, 30)                        # 旧版这块墨
    _blk(_sb, 30 + 9, 30 + 6)                # 新版同一块墨整体挪了 (9,6)
    chk("紫框位移：同一块墨整体平移 → moved",
        _pending_dir(_sa, _sb, (24, 24, 54, 48), 1.0) == "moved",
        _pending_dir(_sa, _sb, (24, 24, 54, 48), 1.0))
    _sc = _np.full((120, 160), 255, _np.uint8)
    _blk(_sc, 30, 30)
    chk("紫框位移：位置没动 → 不是位移（两侧一样，本来就不该有框）",
        _pending_dir(_sa, _sc, (24, 24, 54, 48), 1.0) == "")
    _sd = _np.full((120, 160), 255, _np.uint8)
    _blk(_sd, 30, 30, 18, 5)                 # 新版只剩上半：内容真变了
    chk("紫框位移：内容改了（不是整块挪走）→ 不判位移",
        _pending_dir(_sa, _sd, (24, 24, 54, 48), 1.0) != "moved")
    _se = _np.full((120, 160), 255, _np.uint8)
    _se[80:120, 100:150] = 0                 # 对面另一处有墨（不相干）
    chk("紫框位移：对面那块墨对不上 → 不判位移",
        _pending_dir(_sa, _se, (24, 24, 54, 48), 1.0) != "moved")
    chk("位移判定：无 numpy/cv2 不炸（返回 False 即可）",
        _pending_shift(None, None, (0, 0, 10, 10), 1.0) is False)
    chk("位移判定：整页级巨框不搜（保护性能）",
        _pending_shift(_sa, _sb, (0, 0, 160, 120), 1.0) is False)

    # ⑨ 语义标点 vs 装饰符号（2026-10-27 · DOC-M2001 页脚逗号半角→全角漏标修复）
    #   旧 _substantial 闸门把纯标点替换（, → ，）当「无实义字」整条丢掉。
    #   修后：语义标点（逗号/冒号/括号）改动必须报；装饰符号（• → ◦）照旧不报。
    _mk(A, [(60, "Temperature limit DOC-M2001, rev01")])
    _mk(B, [(60, "Temperature limit DOC-M2001，rev01")])
    ds_p = locate(A, B)
    chk("逗号半角→全角是真改动，必须报",
        len(ds_p) >= 2, f"实际 {len(ds_p)} 条：{[(d.kind, d.old_text, d.new_text) for d in ds_p]}")
    chk("逗号改动的删侧标到逗号",
        any("," in (d.old_text or "") for d in ds_p if d.kind == "removed"),
        f"删侧文本 {[d.old_text for d in ds_p if d.kind == 'removed']}")

    _mk(A, [(60, "• 检查外观无破损。")])
    _mk(B, [(60, "◦ 检查外观无破损。")])
    ds_b = locate(A, B)
    chk("装饰符 •→◦ 不报（noise_only 拦住）",
        len(ds_b) == 0, f"实际 {len(ds_b)} 条：{[(d.kind, d.old_text, d.new_text) for d in ds_b]}")

    _mk(A, [(60, "DOC-M2001, rev01")])
    _mk(B, [(60, "DOC-M2001, rev02")])
    ds_r = locate(A, B)
    chk("数字改动（rev01→rev02）照旧报",
        any("1" in (d.old_text or "") or "2" in (d.new_text or "") for d in ds_r),
        f"实际 {[(d.kind, d.old_text, d.new_text) for d in ds_r]}")

    # ⑩ 重复页眉里的短数字改动（2026-10-28 · DHR-1245 版本号 15→16 三页全漏）
    #   用户原话：「对于原生的 PDF，理论上能完全读取内容才对，居然会有漏读」。
    #   三处病根叠在一起，全部与「短串」有关：
    #     ① `_extract_moved_rows` 的闸门用 len(norm)（含空格）→ `15 ` 算出 3 字，
    #        绕过「过短行不判位移」，跟另一页正文的 `15` 配成位移 → 旧版那行被摘走；
    #     ② `_cancel_identical` 按文本计数抵消、且预算每页重置 → 页眉的 `15`/`16`
    #        被「别处正文的同一个数字」抵掉，抵消量还超过两侧实际共有条数；
    #     ③ `_drop_moved` 拿「`15` 是 `WI-1015` 的子串、`16` 是 `16171819` 的子串」
    #        当免责证据，把仅剩的两条也抹了。
    #   本用例把三处都复现出来（重复页眉 + 正文里含 15/16 的干扰串），
    #   断言「三页都要报、且必须标在页眉、不许牵连正文」。
    _dbody = [(120, "WI-1015 Pouch seal check"),
              (140, "No. 16171819 range check"),
              (160, "Temperature 165~180 C")]

    def _mkp(path, rev):
        d = fitz.open()
        for _ in range(3):
            p = d.new_page()
            p.insert_text((60, 50), "DEVICE HISTORY RECORD / 产品历史记录",
                          fontname="china-s", fontsize=11)
            p.insert_text((60, 72), "Revision No. 版本", fontname="china-s", fontsize=9)
            p.insert_text((420, 72), rev, fontname="china-s", fontsize=11)
            for _y, _t in _dbody:
                p.insert_text((60, _y), _t, fontname="china-s", fontsize=11)
            p.insert_text((60, 780), "Page footer", fontname="china-s", fontsize=9)
        d.save(path)
        d.close()

    _mkp(A, "15")
    _mkp(B, "16")
    ds_h = locate(A, B)
    _rem15 = [d for d in ds_h if d.kind == "removed" and "15" in (d.old_text or "")]
    _add16 = [d for d in ds_h if d.kind == "added" and "16" in (d.new_text or "")]
    chk("重复页眉的版本号改动：3 页都要报出『删 15』",
        len(_rem15) == 3,
        f"实际 {len(_rem15)} 条：{[(d.kind, d.old_text, d.new_text) for d in ds_h]}")
    chk("重复页眉的版本号改动：3 页都要报出『增 16』",
        len(_add16) == 3, f"实际 {len(_add16)} 条")
    chk("版本号的标记必须落在页眉（y<80），不能飘到正文",
        all(min(r[1] for r in d.rects_old) < 80 for d in _rem15)
        and all(min(r[1] for r in d.rects_new) < 80 for d in _add16),
        f"删侧 {[round(min(r[1] for r in d.rects_old), 1) for d in _rem15]}")
    chk("含 15/16 的干扰串（WI-1015 / 16171819）不许被牵连",
        not any("WI-1015" in (d.old_text or "") + (d.new_text or "")
                or "16171819" in (d.old_text or "") + (d.new_text or "") for d in ds_h),
        f"实际 {[(d.kind, d.old_text, d.new_text) for d in ds_h]}")

    # ⑪ 页数不等（2026-10-28 用户定稿）：「一页配一页」的前提是页数对得上。
    #   页数一旦不等（中间插页/删页），第 N 页就对不上第 N 页，任何按页位置的
    #   配对都是错的 → `_extract_moved_rows` / 短行就近抵消**整体跳过**。
    #   这条规则要保住两件事：① 整页增删与正文真改动照报；② 不许刷出一堆假差异。
    def _mkpages(path, pages):
        d = fitz.open()
        for lines in pages:
            p = d.new_page()
            for y, t in lines:
                p.insert_text((60, y), t, fontname="china-s", fontsize=11)
        d.save(path)
        d.close()

    _body = lambda tag: [(60, f"{tag} COMMON PARAGRAPH ALPHA LINE"),
                         (100, f"{tag} COMMON PARAGRAPH BETA LINE"),
                         (140, f"{tag} COMMON PARAGRAPH GAMMA LINE")]

    # ⑪-a 尾部追加一页：整页增 + 一处正文改动，别的都不许有
    _A11 = os.path.join(tmp, "p11a_old.pdf")
    _B11 = os.path.join(tmp, "p11a_new.pdf")
    _mkpages(_A11, [_body("SEC1"), _body("SEC2") + [(200, "Value 100")], _body("SEC3")])
    _mkpages(_B11, [_body("SEC1"), _body("SEC2") + [(200, "Value 200")], _body("SEC3"),
                    [(60, "APPENDIX BRAND NEW PAGE BODY")]])
    _d11 = locate(_A11, _B11)
    chk("页数不等：多出来的整页要报「整页新增」",
        sum(1 for d in _d11 if d.kind == "page_added") == 1,
        f"实际 {[(d.kind, d.page_old, d.page_new) for d in _d11]}")
    chk("页数不等：正文真改动（100→200）照旧报出，没被错位吞掉",
        any(d.kind == "removed" and "100" in (d.old_text or "") for d in _d11)
        and any(d.kind == "added" and "200" in (d.new_text or "") for d in _d11),
        f"实际 {[(d.kind, d.old_text, d.new_text) for d in _d11]}")
    chk("页数不等：不许因为跳过配对而刷出满页假差异",
        len(_d11) <= 6,
        f"实际 {len(_d11)} 条：{[(d.kind, d.old_text, d.new_text) for d in _d11]}")

    # ⑪-b 中间插一页：最难受的场景，只守「不炸」（页级对齐是既有能力缺口，
    #     现在的 page_diffs 只按尾部差额报，中间插页会把末页当整页增删 —— 这里
    #     只断言不会把改动刷成几十条，避免有人把「跳过配对」改成「跨页硬配」。)
    _A12 = os.path.join(tmp, "p11b_old.pdf")
    _B12 = os.path.join(tmp, "p11b_new.pdf")
    _mkpages(_A12, [_body("SEC1"), _body("SEC2"), _body("SEC3")])
    _mkpages(_B12, [_body("SEC1"), [(60, "INSERTED PAGE ONLY TEXT")],
                    _body("SEC2"), _body("SEC3")])
    _n11b = len(locate(_A12, _B12))
    chk("页数不等（中间插页）：不许刷成满页逐字框", _n11b <= 12, f"实际 {_n11b} 条")

    # ⑫ 短行配对（2026-10-28）：短串允许标蓝，但**绝不许被摘成删+增**。
    #   摘走短行的后果是它再也不会进字符流 → 直接漏标（DHR-1245 页眉就这么丢的）。
    #   所以短行走三道窄门：同页 + 两边各只一处 + 判到删增就整块放弃。
    def _mkshort(path, phone_y, far_y=None):
        lines = [(60, "HEADER LINE ALPHA BETA GAMMA"),
                 (phone_y, "15"),
                 (220, "BODY TEXT DELTA EPSILON ZETA"),
                 (260, "FOOTER NOTE ETA THETA")]
        if far_y is not None:
            lines.append((far_y, "15"))
        d = fitz.open()
        p = d.new_page()
        for y, t in sorted(lines):
            p.insert_text((60, y), t, fontname="china-s", fontsize=11)
        d.save(path)
        d.close()

    # ⑫-a 同页唯一 → 位移在 5 倍以内，应标蓝（不报红绿）
    _A13 = os.path.join(tmp, "p12a_old.pdf")
    _B13 = os.path.join(tmp, "p12a_new.pdf")
    _mkshort(_A13, 100)
    _mkshort(_B13, 140)
    _d13 = locate(_A13, _B13)
    chk("短行（同页唯一）页内挪位 → 标蓝，不报红绿",
        sum(1 for d in _d13 if d.kind == "moved") >= 1
        and not any(d.kind in ("removed", "added") for d in _d13),
        f"实际 {[(d.kind, d.old_text, d.new_text) for d in _d13]}")

    # ⑫-b 同页有两处 `15` → 分不清谁是谁 → 不配蓝，但也**不许静默消失**
    _A14 = os.path.join(tmp, "p12b_old.pdf")
    _B14 = os.path.join(tmp, "p12b_new.pdf")
    _mkshort(_A14, 100, far_y=290)
    _mkshort(_B14, 140, far_y=290)
    _d14 = locate(_A14, _B14)
    chk("短行（同页多候选）不做模糊配对：既不标蓝，也不许凭空报红绿",
        not any(d.kind in ("moved", "removed", "added") for d in _d14),
        f"实际 {[(d.kind, d.old_text, d.new_text) for d in _d14]}")

    # ⑫-c 同页唯一但挪得远（>5 倍行高）→ 判到删增就整块放弃，仍不许丢
    _A15 = os.path.join(tmp, "p12c_old.pdf")
    _B15 = os.path.join(tmp, "p12c_new.pdf")
    _mkshort(_A15, 100)
    _mkshort(_B15, 330)
    _d15 = locate(_A15, _B15)
    chk("短行挪得远：宁可报红绿，也不许静默消失",
        (any(d.kind == "removed" and "15" in (d.old_text or "") for d in _d15)
         or any(d.kind == "added" and "15" in (d.new_text or "") for d in _d15))
        or any(d.kind == "moved" for d in _d15),
        f"实际 {[(d.kind, d.old_text, d.new_text) for d in _d15]}")

    # ⑬ 折行挤压：行末单字被挤到下一行（2026-10-28 DHR-1245 rev15→rev16 p3 实测）
    #   旧版 `…PE袋装产品入运输箱` 占一行，新版因为前面插了 `【大灭菌柜】`，
    #   末端那个 `箱` 被挤到下一行单独成行。`MIN_ROW_LEN=2` 把单字行当
    #   「装饰符号行」滤掉 → 新版字符流里少了 `箱` → 报成「`箱` 被删除」。
    #   用户看到的就是「明明只是换了个行，凭什么标我删了一段」。
    #   规矩：**单个字母/汉字**必须进字符流；单个数字/标点/符号照旧滤掉。
    _A16 = os.path.join(tmp, "p13_old.pdf")
    _B16 = os.path.join(tmp, "p13_new.pdf")
    _wr_a = [(100, "Put products into shipping"),
             (120, "PE袋装产品入运输箱"),
             (160, "BODY LINE AFTER WRAP TEST")]
    _wr_b = [(100, "Put products into shipping"),
             (120, "PE袋装产品入运输"),
             (132, "箱"),
             (160, "BODY LINE AFTER WRAP TEST")]

    def _mkwrap(path, lines):
        d = fitz.open()
        p = d.new_page()
        for y, t in lines:
            p.insert_text((60, y), t, fontname="china-s", fontsize=11)
        d.save(path)
        d.close()

    _mkwrap(_A16, _wr_a)
    _mkwrap(_B16, _wr_b)
    _d16 = locate(_A16, _B16)
    chk("折行挤压：行末单字掉到下一行，不许报「箱 被删除」",
        not any(d.kind == "removed" and (d.old_text or "").strip() == "箱" for d in _d16),
        f"实际 {[(d.kind, d.old_text, d.new_text) for d in _d16]}")
    chk("折行挤压：文字一字未改 → 整页不许报任何改动",
        len(_d16) == 0,
        f"实际 {[(d.kind, d.old_text, d.new_text) for d in _d16]}")

    # 单字行的准入规则（extract_rows 层，端到端）
    _A17 = os.path.join(tmp, "p13_single.pdf")
    _mkwrap(_A17, [(100, "箱"), (140, "8")])
    _dsingle = fitz.open(_A17)
    _rows17 = extract_rows(_dsingle[0], 0)
    _dsingle.close()
    chk("单字行：单个汉字保留（折行时它就是正文）",
        any((r.norm or "").strip() == "箱" for r in _rows17),
        f"实际 {[r.norm for r in _rows17]}")
    chk("单字行：单个数字仍按原样丢弃（不承载信息、数量一变就刷噪音）",
        not any((r.norm or "").strip() == "8" for r in _rows17),
        f"实际 {[r.norm for r in _rows17]}")

    # ⑭ 重复标签的真新增（2026-10-28 DHR-1245 p3 实测）
    #   `【大灭菌柜】` 这种标签在文档里出现多次：旧版 6 处、新版 10 处。
    #   `_drop_moved` 原先只问「对面文档里有没有这段字」——有，就整条免责。
    #   于是 diff 已经正确报出的「这里新增了【大灭菌柜】」被撤得干干净净，
    #   用户在新版那一行上看不到任何标记。
    #   规矩：对面有几处就只能承接几处；本侧比对面**多出来的**必须报。
    _TAG = "【大灭菌柜】"
    _one_add = [LocDiff(kind="added", page_new=0, new_text=_TAG,
                        rects_new=[(60, 100, 140, 112)])]
    _one_rem = [LocDiff(kind="removed", page_old=0, old_text=_TAG,
                        rects_old=[(60, 100, 140, 112)])]
    chk("重复标签的真新增：对面 3 处、本侧 4 处 → 必须报出来",
        len(_drop_moved(list(_one_add), _TAG * 3, _TAG * 4)) == 1,
        f"实际 {len(_drop_moved(list(_one_add), _TAG * 3, _TAG * 4))} 条")
    chk("重复标签的真删除：本侧 4 处、对面 3 处 → 必须报出来",
        len(_drop_moved(list(_one_rem), _TAG * 4, _TAG * 3)) == 1,
        f"实际 {len(_drop_moved(list(_one_rem), _TAG * 4, _TAG * 3))} 条")
    chk("重复标签的纯挪位：两边都是 3 处 → 仍要免责（不刷假差异）",
        len(_drop_moved(list(_one_add), _TAG * 3, _TAG * 3)) == 0
        and len(_drop_moved(list(_one_rem), _TAG * 3, _TAG * 3)) == 0,
        f"实际 add={len(_drop_moved(list(_one_add), _TAG * 3, _TAG * 3))} "
        f"rem={len(_drop_moved(list(_one_rem), _TAG * 3, _TAG * 3))}")

    # ⑮ 跳页（2026-10-29 用户定稿）：拼接出来的 PDF（第 1 页扫描件、后面正常页）
    #   里扫描页**默认不参与比对**。页号由 `pdfdiff_preflight.assess()["skip_pages"]`
    #   算出，原样透传给 `locate()` / `graphic_regions()`。这里守三条：
    #     ① 跳过的页哪怕真有改动，也一条都不许报 —— 用户要的就是「当它不存在」；
    #     ② 没跳的页照旧报，别把「跳一页」做成「跳全文」；
    #     ③ 像素层同样按跳页闭嘴 —— 只关文字层不关像素层的话，扫描页上照样会
    #        刷出一堆紫框，等于没跳。
    _A18 = os.path.join(tmp, "p14_old.pdf")
    _B18 = os.path.join(tmp, "p14_new.pdf")
    _mkpages(_A18, [_body("SEC1"), [(60, "SCANNED PAGE BODY KEEP")]])
    _mkpages(_B18, [_body("SEC1"), [(60, "SCANNED PAGE BODY CHANGED")]])
    chk("跳页：不跳时第 2 页那处改动照常报出",
        len(locate(_A18, _B18)) >= 1, f"实际 {len(locate(_A18, _B18))} 条")
    chk("跳页：跳过第 2 页 → 一条都不报（页内真有改动也不报）",
        len(locate(_A18, _B18, skip_pages=[1])) == 0,
        f"实际 {[(d.kind, d.page_old, d.page_new) for d in locate(_A18, _B18, skip_pages=[1])]}")
    chk("跳页：只跳第 1 页时，第 2 页的改动仍在（不许顺带跳全文）",
        any(d.page_old == 1 or d.page_new == 1
            for d in locate(_A18, _B18, skip_pages=[0])),
        f"实际 {[(d.kind, d.page_old, d.page_new) for d in locate(_A18, _B18, skip_pages=[0])]}")

    def _mk_scanpdf(src, dst, dpi=120):
        """整份渲成整页灰度图再铺回去 —— 造无文字层的位图 PDF（模拟扫描件）。"""
        _s = fitz.open(src)
        _d = fitz.open()
        try:
            for _pg in _s:
                _pix = _pg.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
                _d.new_page(width=_pg.rect.width, height=_pg.rect.height).insert_image(
                    fitz.Rect(0, 0, _pg.rect.width, _pg.rect.height),
                    stream=_pix.tobytes("png"))
            _d.save(dst)
        finally:
            _s.close()
            _d.close()

    _A19 = os.path.join(tmp, "p14s_old.pdf")
    _B19 = os.path.join(tmp, "p14s_new.pdf")
    _mk_scanpdf(_A18, _A19)
    _mk_scanpdf(_B18, _B19)
    _g19 = graphic_regions(_A19, _B19, fast_mode=True)
    chk("跳页（像素层）：不跳时有第 2 页的紫框",
        any(r.get("page") == 1 for r in _g19),
        f"实际 {sorted({r.get('page') for r in _g19})}")
    _g19s = graphic_regions(_A19, _B19, fast_mode=True, skip_pages=[1])
    chk("跳页（像素层）：跳过第 2 页后 0 个框（文字层闭嘴、像素层也得闭嘴）",
        not _g19s, f"实际 {len(_g19s)} 个框 {sorted({r.get('page') for r in _g19s})}")
    _g19o = graphic_regions(_A19, _B19, fast_mode=True, skip_pages=[0])
    chk("跳页（像素层）：只跳第 1 页时，第 2 页的紫框仍在",
        any(r.get("page") == 1 for r in _g19o),
        f"实际 {sorted({r.get('page') for r in _g19o})}")

    print(f"定位引擎自检：{ok} 项通过，{fail} 项失败")
    raise SystemExit(1 if fail else 0)
