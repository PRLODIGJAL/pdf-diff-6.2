# -*- coding: utf-8 -*-
"""
ui_viewport.py —— **画面**：一份 PDF 怎么变成你眼前那一页，以及怎么动它。
==================================================================
一条线，从上往下：

  `_open_preview_docs` → `_add_annots` → `render` → `_render_side`
       → `_pixmap_gray_under_annots`（要灰度底图时）→ `_cached_pixmap` → Tk 位图

然后是「怎么动它」：翻页 / 缩放（`_zoom` + 锚点还原）/ 滚动条同步 / 滚轮 /
按住拖动。这些全是纯显示态 —— **不改 marks、不改文档、不落盘**，关掉重开就没了。

⚠️ 灰度合成（`_pixmap_gray_under_annots`）必须留在显示层：它只在「页面渲染出来
   之后」动像素，`locate_marks()` 的判定一个字节都不经过它。这条有断言守着
   （`dev/_regress/ui_gray_test.py` 的 [E] 段）。

2026-11-02 目录重构时从 `diff_app5.py` 原样切出来，逻辑一个字节没改。
"""

from __future__ import annotations

import base64
import os
import sys
import threading
import time

import tkinter as tk

# 本文件是 ui 层的一个模块，单独 import 也要能用：先把 `app/` 塞进 sys.path，
# 再让 `paths.install()` 把 ui/judge/engine/shell 四层一起铺开。
_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP not in sys.path:
    sys.path.insert(0, _APP)
import paths as _paths                                          # noqa: E402

_paths.install()

try:                                                            # noqa: E402
    import pymupdf as fitz
except ImportError:                                             # pragma: no cover
    import fitz  # type: ignore

import pdf_annotate as ann                                      # noqa: E402
import pdfdiff_gate as gate                                     # noqa: E402
from ui_theme import (                                          # noqa: E402
    CACHE_MAX, DPI_MAX, DPI_MIN, DPI_STEPS, FONT,
    VIEW_BLINK, VIEW_SWIPE,
)


# ---- 灰度底图 + 保色标注的合成参数（见 `_pixmap_gray_under_annots`）----
#: 两版渲染的同点像素差 ≤ 这个数就当噪点归零，保证没标注的地方严格 R=G=B。
_GRAY_DELTA_FLOOR = 2
#: 差异 ≥ 这个数视为**不透明**标注（1pt 描边 / 编号），直接用有标注版的像素。
#: 半透明填充的最大偏移 = annot_opacity(≤0.18) × 255 = 46，留 10 的余量。
_GRAY_OPAQUE_DIFF = 56
#: 合成系数 1-a 的整数分母（见 `_pixmap_gray_under_annots`）。取 64 = 2^6 有讲究：
#: ① `chroma * 64` 最大 255×64 = 16320，int16 装得下；② 是 2 的幂，可以用算术
#: 右移代替整除 —— int16 数组上 `// 64` 会走逐元素整数除法，557 万像素实测
#: 0.05s，`>> 6` 只要 0.006s（结果相同，负数也一样向下取整）。
_GRAY_K_SHIFT = 6
_GRAY_K_DEN = 1 << _GRAY_K_SHIFT


def _gray_luma(rgb):
    """按 ITU-R BT.601 亮度公式把 RGB（最后一维 3 或 4 通道）压成灰度。"""
    import numpy as np
    return ((rgb[..., 0].astype(np.int32) * 299
             + rgb[..., 1].astype(np.int32) * 587
             + rgb[..., 2].astype(np.int32) * 114) // 1000)


def _annot_opacity() -> float:
    """当前标注的填充不透明度（合成灰度底图时要按它反推底图色度残留）。

    直接问 `ann._paint_style()`，配置（`Config.annot_opacity`）改了这里自动
    跟上，不写死。取不到就给文档里写的默认 0.15 —— 这个值只影响「框内底色
    偏多少」，取值错了画面上也只是淡一点点，不会崩、不会改判定。
    """
    try:
        return float(ann._paint_style()[1])
    except Exception:
        return 0.15


def _pixmap_gray_under_annots(page, dpi: int, plain_page=None):
    """底图去色、标注**保持原色**地渲染一页。

    为什么不能直接用 `get_pixmap(colorspace=csGRAY)`：那会把红/绿/浅蓝标注
    一起灰掉，三色区分就没了。

    为什么需要 `plain_page`：标注不是 PDF annotation，而是 `paint_into()` 用
    `draw_rect` 直接写进**页面内容流**的（见 core/pdf_annotate.py `_paint`），
    所以 `annots=False` 根本去不掉它，两次渲染的结果一模一样。要拿到「只有
    底图」的那一版，只能拿同一个文件**没画标注**的另一份文档来渲染。

    合成公式（2026-10-30 晚改，用户报「框内恢复了原颜色」）：
        P  = 干净底图（彩色）        L  = BT.601 亮度(P)，广播成 RGB
        C  = 底图 + 标注（彩色）     chroma = P - L（底图自己的色度）
        设该像素被标注覆盖的等效不透明为 a，标注色为 c，则
            C = P(1-a) + c·a
        我们想要的是「同一层标注压在同一张**灰**底图上」：
            out = L(1-a) + c·a = C - (1-a)·chroma     ← 恒等式，代入即得
        于是只剩一个未知量 a，按三档取值（a 是连续量，这里只取端点）：
            · 干净像素 / 渲染噪点   a=0      → k=1         out = L
            · 半透明填充            a=opa    → k=1-opa     out = C - (1-opa)·chroma
            · 1pt 描边、编号小字     a=1      → k=0         out = C
        ⚠️ 关键就在这个 `- (1-a)·chroma`：不减它，框内的底色就会跟着原色走。
           用户原话「框起来的地方的中间又恢复了原颜色，显得整个布局很乱」——
           旧实现是 `out[diff>2] = C`（整块搬运），半透明填充里 80%+ 是底图
           原色，所以彩稿上的框内一律是原色。不要再退回那个写法。
        ⚠️ 也别退回上一版的 `out = L + (C-P)`：它等价于漏掉 `opa·chroma`，
           在色相上会偏 —— 实测紫框压在蓝块上会偏棕（蓝分量被吃掉 20 级）。
        `opa` 从 `ann._paint_style()` 现取（配置改了这里自动跟上），不是写死。
        分层阈值 `_GRAY_OPAQUE_DIFF` 的依据：填充最大偏移 = opa(≤0.18) × 255
        = 46，留 10 的余量；a≈1 的描边偏移是 |c-p|，通常 >60。

    好处：标注的颜色、半透明填充、编号、抗锯齿全都是 PyMuPDF 亲手画的那一份，
    和导出 PDF 里看到的完全一致，不可能走样；只是把**底图那一半**换成灰度。

    只作用在**渲染**这一层：`locate_marks()` 的判定一个字节都不经过这里。
    """
    import numpy as np

    pm_c = page.get_pixmap(dpi=dpi, annots=True)          # 底图 + 标注
    if plain_page is None:
        # 拿不到干净底图就整页去色：标注也会变灰，但至少能看、且绝不崩
        return page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=True)
    pm_p = plain_page.get_pixmap(dpi=dpi, annots=True)    # 只有底图
    h, w = pm_c.height, pm_c.width
    if (pm_p.height, pm_p.width) != (h, w) or pm_p.n != pm_c.n:
        return pm_c                                       # 尺寸对不上就别冒险
    n = pm_c.n

    def _arr(pm):
        # 用 stride 切片：PyMuPDF 的每行可能有尾部填充，不能直接按 w*n 展开
        buf = np.frombuffer(pm.samples, dtype=np.uint8)
        return buf.reshape(pm.height, pm.stride)[:, :w * n].reshape(h, w, n)

    C = _arr(pm_c)
    P = _arr(pm_p)
    lum = _gray_luma(P).astype(np.uint8)                  # 输出底色（纯灰）
    # ⚠️ 下面这段的类型是**量出来的**（A4@240dpi 557 万像素）。四个坑，都别改回去：
    #    · 全用 int32：十几个 67MB 中间量，0.23s；
    #    · 用布尔索引取「有标注的像素」再合成：fancy indexing 在 3 通道上反而
    #      更慢（掩码 0.036 + 取子集 0.037 + 写回 0.018），还不如整图向量化；
    #    · 系数用 float：33MB×3 的 float32 乘，白给 0.03s。改用整数千分率
    #      （`_GRAY_K_DEN`），`chroma * g` 最大 255×64=16320，int16 装得下；
    #    · 整图一次算完是 0.07s，和旧实现（0.05s）基本持平。
    Cc = C[..., :3].astype(np.int16)
    Pc = P[..., :3].astype(np.int16)
    chroma = Pc - lum.astype(np.int16)[..., None]         # 底图色度（偏红为正…）
    # 标注在这一点落笔多重。⚠️ 别写成 `np.abs(Cc-Pc).max(axis=2)`：沿最后一轴
    # 归约是跨 stride 访问，int16 不走 SIMD，557 万像素实测 0.056s；拆成三条
    # 连续的单通道再取 max 只要 0.010s（5.6 倍），结果逐像素相同。
    dmax = Cc[:, :, 0] - Pc[:, :, 0]
    np.abs(dmax, out=dmax)
    for _ch in (1, 2):
        _d = Cc[:, :, _ch] - Pc[:, :, _ch]
        np.abs(_d, out=_d)
        np.maximum(dmax, _d, out=dmax)
    # k = 1-a，三个端点：干净 1 / 半透明填充 1-opa / 不透明 0（见上面公式推导）
    k = np.full((h, w), _GRAY_K_DEN, np.int16)
    k[dmax > _GRAY_DELTA_FLOOR] = max(
        0, int(round((1.0 - _annot_opacity()) * _GRAY_K_DEN)))
    k[dmax >= _GRAY_OPAQUE_DIFF] = 0
    rgb = Cc - ((chroma * k[..., None]) >> _GRAY_K_SHIFT)
    np.clip(rgb, 0, 255, out=rgb)
    out = np.empty((h, w, n), np.uint8)
    out[..., :3] = rgb.astype(np.uint8)
    if n == 4:
        out[..., 3] = P[..., 3]
    return fitz.Pixmap(fitz.csRGB, w, h, out.tobytes(), n == 4)


class ViewportMixin:
    """窗口「画什么、怎么看」的那一半。`App` 继承它。"""

    # ---------------- 拖入即预览（2026-10-29）----------------
    def _page_count(self) -> int:
        """当前能翻几页。有对比结果就用结果里的页数，否则用预览文档的页数。"""
        if self.marks:
            return max(self.marks["old"].get("pages", 0),
                       self.marks["new"].get("pages", 0))
        return max((int(d.page_count) for d in self.docs.values() if d is not None),
                   default=0)

    def _has_view(self) -> bool:
        """画面上有没有东西可看（预览的原件也算）。"""
        return bool(self.docs)

    def _close_docs(self):
        # ⚠️ 2026-10-30：**先清画布、再放引用**。`_cache` / `_imgs` 一清，
        #    那几张 PhotoImage 立刻被回收，但画布上还挂着用它们的 item ——
        #    下一次重绘就抛 `image "pyimageN" does not exist`（新增的「丢文件
        #    即判」回归里当场暴露：状态栏被打成「处理结果时出错」）。先 delete
        #    掉 item，引用才真正没人用。
        for side in ("old", "new"):
            cv = (getattr(self, "canv", None) or {}).get(side)
            if cv is not None:
                try:
                    cv.delete("all")
                except tk.TclError:
                    pass
        for d in list(self.docs.values()) + list(self.docs_plain.values()):
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass
        self.docs.clear()
        self.docs_plain.clear()
        self._cache.clear()
        self._imgs.clear()

    def _preview_refresh(self, keep_page=False):
        """文件一变就把原件显示出来 —— **不等比对**。

        用户 2026-10-29：「其实这个软件本身就是一个很好的查看器……拖入文件的时候
        它就直接在下面显示出来先，然后开始比对的话就慢慢地标记上去」。
        所以选好文件的那一刻就该能翻页、能缩放、能拖动，至于标记，跑完再画。

        · 转换（图片折 PDF / Word、Excel 转 PDF）放后台线程：Word 要调本机
          Office，几秒起步，同步做会把界面冻住。
        · 连续拖文件用 `_prev_token` 只认最后一次，避免「后发先至」的旧结果
          把新文件盖回去。
        · 正在比对时不打断（`busy`）：后台手里攥着这对路径，换画面只会对不上。
          ⚠️ 例外是 `_run` 自己开头要重置画面 —— 那时它赶在 `busy=True` 之前调，
             并且带 `keep_page=True`（用户可能正翻到第 5 页等着看结果）。
        """
        if self.busy:
            return
        self._prev_token += 1
        tok = self._prev_token
        a = self.old_var.get().strip()
        b = self.new_var.get().strip()
        if not a and not b:
            self.marks = None
            self._preview_ready = False
            self._parts = []
            self._close_docs()
            self.page = 0
            self._update_counts()
            for side in ("old", "new"):
                self._render_side(side)
            self.lbl_page.config(text="0 / 0")
            self.btn_prev.config(state="disabled")
            self.btn_next.config(state="disabled")
            return

        def work():
            got = {}
            for side, p in (("old", a), ("new", b)):
                if not p or not os.path.isfile(p):
                    continue
                try:
                    import pdfdiff_office as off
                    got[side] = off.ensure_input(p)
                except Exception:
                    got[side] = None
            self.q.put(("preview", tok, got, keep_page))

        threading.Thread(target=work, daemon=True).start()

    def _open_preview_docs(self, paths: dict, keep_page=False):
        """把 `ensure_input` 给出的 PDF 路径打开成预览文档（无标记）。"""
        self.marks = None
        self._close_docs()
        for side in ("old", "new"):
            p = paths.get(side)
            if not p or not os.path.isfile(p):
                continue
            try:
                self.docs[side] = fitz.open(p)
                # 底图灰度要拿「没画过标注」的同文件做差分；预览阶段两者本就相同
                self.docs_plain[side] = fitz.open(p)
            except Exception:
                pass
        self._preview_ready = bool(self.docs)
        if not keep_page:
            self.page = 0
        self._auto_fit = True
        self._reset_view = True
        self._update_counts()
        self.render()
        if self._preview_ready:
            _n = self._page_count()
            _miss = [("旧版" if k == "old" else "新版")
                     for k in ("old", "new")
                     if (self.old_var.get() if k == "old" else self.new_var.get()).strip()
                     and k not in self.docs]
            _msg = ("已显示原件（%d 页）· 先翻着看，点「开始对比」再把差异标上去" % _n
                    + ("　·　%s打不开" % "、".join(_miss) if _miss else ""))
            # ⚠️ 2026-10-30：预览是异步的，它回来时**不要把「不能对比」的原因盖掉** ——
            #    用户刚拖进来的那一刻就知道这份不行，一转身状态栏又变成「可以开始对比」，
            #    等于白判。同理，已确认过的紫框提醒也带一句。
            _bn = gate.block_reason(self._rep)
            if _bn:
                _msg = "不能对比：%s　·　（仍可翻看原件）" % _bn
            elif self._rep_ok and (self._rep or {}).get("level") == "warn":
                _msg += "　·　已确认：这份只给紫框"
            elif (self._rep or {}).get("level") == "ok":
                # 预判和预览是两条异步线，谁先到没准。类型这句两边都拼，
                # 否则「原生可直接比」的提示会被后到的预览文案盖掉（时有时无）。
                _msg += "　·　类型已确认，可以直接比"
            self.status_var.set(_msg)
        # 比对已经开始、增量结果先到了（docs 那时还没建好）—— 现在补画上去
        self._flush_parts()

    # ---------------- 逐页标记（2026-10-29）----------------
    def _paint_parts(self, page: int, regions) -> None:
        """把**某一页**的像素层结果画进当前文档（追加，不是重画整份）。

        为什么能「追加」而不担心和最终结果打架：这些条目来自
        `graphic_regions(fast_mode=True)`，那条路的出口不做 `_merge_paragraphs`
        / `_frag_suppress` / `_moved_rescue_regions` 三道整份后处理，
        每一页产出的就是终稿，页与页之间不会互相改写（见该函数 `on_page` 说明）。
        跑完 `_on_done` 仍会用 `_add_annots()` 全量重画一次 —— 结果与这里一致，
        所以用户看到的是「框一个个出现」，而不是「最后一刻全变样」。
        """
        # ⚠️ 这里**不能**用 `_enabled_kinds()`：它靠 `self.marks["fast_mode"]`
        #    判断「这批有没有紫框」，而比对过程中 `marks` 还是 None —— 紫会被
        #    剔掉，结果就是「跑的过程中一条紫框都看不到」（2026-10-29 实测）。
        #    逐页阶段既然手上有 pending 条目，就说明这批确实有紫，只按勾选判。
        enabled = tuple(k for k in ann.KIND_ALL if self.kind_on[k].get())
        pend = {"old": {page: []}, "new": {page: []}}
        mov = {"old": {page: []}, "new": {page: []}}
        for r in regions:
            rc = fitz.Rect(*r["rect"])
            if r.get("moved"):
                src = r.get("shift_src") or "new"
                a, b = ("new", "old") if src == "new" else ("old", "new")
                mov[a][page].append(rc)
                mov[b][page].append(fitz.Rect(*r["rect_shift"]))
            else:
                # 快速模式的正常产出是 `pending`（紫）。真出现别的形态也照紫画，
                # 宁可多一个「待判定」也不能静默丢掉一条改动。
                # ⚠️ 2026-10-29：逐页阶段就得知道「画哪一侧」，不能等跑完
                #    `_add_annots` 再补 —— 否则用户看着一屏两侧都有框，
                #    最后一秒集体消失，像是结果被改了一样。
                # ⚠️ 两侧分开算：只标**有墨那侧**（2026-10-30 用户「不要画」：
                #    空那侧连空心框也去掉）。两侧塞同一个矩形就是「对称复制」，
                #    用户当场报过错。判不出方向 → 两侧都标。
                # ⚠️ 2026-11-02：颜色恒为紫，不再按方向染红/绿/蓝边。
                _dir = (r.get("dir") or "").strip().lower()
                for _sd in ("old", "new"):
                    if not ann.pending_draw(_dir, _sd):
                        continue
                    pend[_sd][page].append(fitz.Rect(rc))
        for side, fallback in (("old", ann.RED), ("new", ann.GREEN)):
            doc = self.docs.get(side)
            if doc is None or page >= doc.page_count:
                continue
            if not (pend[side][page] or mov[side][page]):
                continue
            try:
                sm = ann._side_marks(doc, [], {}, side, None, mov, pend)
                # 紫框现在是一整桶（统一纯紫，不再分成「位移 / 其余」两档），
                # 所以这里就按普通桶过滤，跟红/绿/浅蓝一个待遇。
                ann.paint_into(doc, ann.filter_kinds(sm, enabled), fallback)
            except Exception:
                continue        # 画一页失败不该影响后面几页
            # 这一页的像素变了 → 渲染缓存必须失效，否则重绘拿到的还是旧图
            for k in [k for k in self._cache if k[0] == side and k[1] == page]:
                self._cache.pop(k, None)
        if self.page == page:
            self._reset_view = False
            self.render()

    def _on_part(self, page: int, regions) -> None:
        """后台推来一页结果（`graphic_regions` 的 `on_page` 回调转过来的）。"""
        if not regions:
            return
        # docs 还没建好 / 画面上还是上一轮的结果 → 先攒着，别往旧文档上乱画
        if not self.docs or self.marks is not None:
            self._parts.append((page, regions))
            return
        self._paint_parts(page, regions)

    def _flush_parts(self) -> None:
        """把攒下的逐页结果补画上去（docs 建好之后调）。"""
        parts, self._parts = self._parts, []
        for page, regions in parts:
            try:
                self._paint_parts(page, regions)
            except Exception:
                pass

    def _add_annots(self, keep_cache: bool = False):
        """把标记画进内存里的文档对象（原文件一个字节不动）。

        每次新对比 / 切换勾选都重开干净文档 —— 标记是写进页内容流的，摘不掉，
        只能重开重画（`fitz.open` 懒加载，重开本身只要 1ms 量级）。
        过滤走 `ann.filter_kinds`：没勾的桶（如默认关的位移）不画。

        `keep_cache`（2026-10-30）：只切勾选时保留渲染缓存。缓存键里带了勾选
        桶指纹（见 `_cache_key`），所以旧图不会串味；保留之后「关掉再开回来」
        这一下直接命中，不用再渲染一遍。换文件 / 新一轮结果必须清（内容变了）。
        """
        enabled = self._enabled_kinds()
        for side, fallback in (("old", ann.RED), ("new", ann.GREEN)):
            old = self.docs.pop(side, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            path = (self.marks.get(side) or {}).get("path")
            if not path or not os.path.isfile(path):
                continue
            try:
                doc = fitz.open(path)
                ann.paint_into(doc, ann.filter_kinds(self.marks.get(side), enabled),
                               fallback)
                self.docs[side] = doc
                # 「底图灰度」要拿它做差分：标记是写进内容流的，
                # 只有另开一份**没画过**的同文件，才知道哪些像素是标记。
                plain = self.docs_plain.pop(side, None)
                if plain is not None:
                    try:
                        plain.close()
                    except Exception:
                        pass
                self.docs_plain[side] = fitz.open(path)
            except Exception:
                pass
        if not keep_cache:
            self._cache.clear()

    # ---------------- 渲染 ----------------
    def render(self):
        """按当前页 / 当前缩放重画两侧。只读渲染，不写盘。

        ⚠️ 2026-10-29：判据从「有没有 marks」改成「有没有 docs」。
           拖入文件后先显示原件（预览），那时 marks 还是 None，但画面**必须能画**
           —— 用户要拿它当查看器翻页。`_page_count()` 也会自动落到 docs 的页数上。
        ⚠️ 2026-10-30 晚：滑动视图走 `_render_swipe`（一块画布装两版），
           并排才走 `_render_side` 两侧。
        """
        # 滑动和叠加都是「一块画布装两版」，走同一个渲染；只有并排画两侧。
        swipe = self.view_mode.get() in (VIEW_SWIPE, VIEW_BLINK)
        _draw = (lambda: self._render_swipe()) if swipe else (
            lambda: [self._render_side(s) for s in ("old", "new")])
        if not self._has_view():
            _draw()
            return
        self.configure(cursor="watch")
        try:
            self.update_idletasks()
            if self._auto_fit:
                self.dpi = self._fit_dpi()
                self._auto_fit = False
            self.lbl_dpi.config(text=f"{self.dpi} dpi")
            _draw()
            n = self._page_count()
            _pg = f"{self.page + 1} / {n}" if n else "0 / 0"
            no_ = nn_ = 0
            if self.marks:
                no_ = self.marks["old"].get("pages", 0)
                nn_ = self.marks["new"].get("pages", 0)
            if no_ and nn_ and no_ != nn_:
                # 两侧页数不同时说明是**跟哪一侧**在翻页 —— 否则用户只看得到
                # 大的那个数字（如「1 / 25」），会以为自己导入的文件有 25 页。
                _pg += f"　（旧 {no_} 页 · 新 {nn_} 页）"
            self.lbl_page.config(text=_pg)
            self.btn_prev.config(state="normal" if self.page > 0 else "disabled")
            self.btn_next.config(state="normal" if self.page < n - 1 else "disabled")
            self._sync_scrollbars()
        finally:
            self.configure(cursor="")

    def _fit_dpi(self) -> int:
        """按画布宽度算「适应宽度」的 dpi（并排时两张页各占一半窗宽）。"""
        if self.view_mode.get() in (VIEW_SWIPE, VIEW_BLINK):
            # 滑动/叠加只有一块画布、铺满整窗，直接按它算。旧版那块是隐藏的，
            # 宽度不可信（`winfo_width()` 会退到 1）。
            w = self.canv["new"].winfo_width()
        else:
            cv = self.canv["old"]
            w = cv.winfo_width()
        if w < 80:                       # 还没布局好，别照 1px 算
            return 110
        pw = 595.0
        try:
            doc = self.docs.get("old")
            if doc is not None and self.page < doc.page_count:
                pw = max(200.0, float(doc[self.page].rect.width))
        except Exception:
            pass
        dpi = int(w / pw * 72.0)
        return max(DPI_MIN, min(DPI_MAX, dpi))

    def _render_side(self, side: str):
        cv = self.canv[side]
        doc = self.docs.get(side)
        if doc is None or self.page >= doc.page_count:
            cv.delete("all")
            cv.configure(scrollregion=(0, 0, 10, 10))
            # 三句话分别对应：这一份没有这一页 / 有别的文件但这份没打开 /
            # 什么都没选。原来只有两句，预览接入后「有 marks 但不是这一页」和
            # 「压根没选文件」会撞成同一句，用户分不清。
            if doc is not None:
                _tip = "（此侧没有这一页）"
            elif self._has_view():
                _tip = "（这一侧的文件打不开）"
            else:
                _tip = "把两个版本拖进上方，这里先显示原件"
            cv.create_text(20, 20, anchor="nw", fill="#6b7280", font=(FONT, 11),
                           text=_tip)
            return
        # ⚠️ 2026-10-30：**先渲染出新图，再清画布**。原来是先 `delete("all")`
        #    再算像素，那 0.2 秒里画布是空的 —— 用户点勾选时看到的「闪一下」
        #    就是这段空白。图在手再一次性替换，旧那一帧会一直撑到新帧就位。
        img = self._cached_pixmap(side, doc[self.page])
        cv.delete("all")
        self._imgs[side] = img           # ⚠️ 不留引用 = 画布空白
        cv.create_image(0, 0, anchor="nw", image=img)
        cv.configure(scrollregion=(0, 0, img.width(), img.height()))
        # 翻页才回顶部（缩放/勾选切换保持当前视角，见 _zoom/_on_kind_toggle）
        if self._reset_view:
            cv.yview_moveto(0)
            cv.xview_moveto(0)

    def _cache_key(self, side: str, page_no: int) -> tuple:
        """渲染缓存键 —— **必须带勾选桶的指纹**。

        图像内容只由「哪一侧 / 哪一页 / dpi / 灰度 / 画了哪些颜色桶」决定。
        带上桶指纹之后，切换勾选不必清空整个缓存：切回去时那一版还在就直接命中
        （和「底图灰度」开关同样的机制，来回点不再重画）。清空反而会让每次
        切换都要重渲染两侧。
        """
        return (side, page_no, self.dpi, bool(self.gray_on.get()),
                self._enabled_kinds())

    def _cached_pixmap(self, side: str, page) -> tk.PhotoImage:
        gray = bool(self.gray_on.get())
        key = self._cache_key(side, self.page)
        img = self._cache.get(key)
        if img is not None:
            return img
        if gray:
            pp = None
            pd = self.docs_plain.get(side)
            if pd is not None and self.page < pd.page_count:
                pp = pd[self.page]
            pm = _pixmap_gray_under_annots(page, self.dpi, pp)
        else:
            pm = page.get_pixmap(dpi=self.dpi, annots=True)
        img = tk.PhotoImage(
            data=base64.b64encode(pm.tobytes("png")).decode("ascii"),
            # ⚠️ 2026-10-30：**必须显式给 master**。不给的话 `tk.PhotoImage`
            #    挂到 `tkinter._default_root`（进程里**第一个** Tk）的解释器上，
            #    而画布属于当前这个 App 的解释器 —— 同一进程里开第二个窗口
            #    （回归套件就是这么跑的）时 `create_image` 会抛
            #    `image "pyimageN" does not exist`，状态栏被打成「处理结果时出错」。
            #    实际使用只有一个窗口，所以这是潜伏问题；但既然踩到了就钉死。
            master=self)
        while len(self._cache) >= CACHE_MAX * 2:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = img
        return img

    def _on_gray_toggle(self):
        """「底图灰度」开关：只换渲染方式，不重算、不碰判定结果。

        缓存键里带了 gray 标志，所以来回切换不必清空 —— 切回去时若那一版
        还在缓存里就是瞬间的；不在就重渲染当前页（一页，几十毫秒）。
        """
        if not self._has_view():
            return                        # 画面上还没有东西可重绘
        self._reset_view = False          # 保持当前滚动位置
        self.render()
        self.status_var.set(
            "底图灰度：开（只影响显示，不影响对比结果）" if self.gray_on.get()
            else "底图灰度：关（按原件原色显示）")

    # ---------------- 翻页 / 缩放 / 滚动 ----------------
    def _prev_page(self):
        # 预览时也能翻页（用户要拿它当查看器用），所以判据是「有没有页面可看」
        if self._has_view() and self.page > 0:
            self.page -= 1
            self._reset_view = True
            self.render()

    def _next_page(self):
        if self._has_view():
            n = self._page_count()
            if self.page < n - 1:
                self.page += 1
                self._reset_view = True
                self.render()

    def _zoom(self, d: int, anchor=None):
        """放大/缩小。`anchor=(side, x, y)` 时以该鼠标点为锚点缩放（滚轮用）。"""
        idx = min(range(len(DPI_STEPS)),
                  key=lambda i: abs(DPI_STEPS[i] - self.dpi))
        idx = max(0, min(len(DPI_STEPS) - 1, idx + d))
        if DPI_STEPS[idx] == self.dpi:
            return                                    # 已到顶/到底，不必重绘
        keep = self._anchor_rel(anchor) if anchor else None
        self.dpi = DPI_STEPS[idx]
        self._reset_view = False
        self.render()
        if keep:
            self._anchor_restore(*keep)

    def _anchor_rel(self, anchor):
        """换算鼠标点对应的**内容相对坐标**（0~1），供缩放后复位。"""
        side, ex, ey = anchor
        cv = self.canv.get(side)
        bb = cv.bbox("all") if cv is not None else None
        if not bb or bb[2] < 2 or bb[3] < 2:
            return None
        return (side, cv.canvasx(ex) / bb[2], cv.canvasy(ey) / bb[3], ex, ey)

    def _anchor_restore(self, side, rx, ry, ex, ey):
        """缩放后让该内容点仍落在鼠标原位 —— 否则放大一下画面就飘走。"""
        cv = self.canv.get(side)
        bb = cv.bbox("all") if cv is not None else None
        if not bb or bb[2] < 2 or bb[3] < 2:
            return
        w, h = float(bb[2]), float(bb[3])
        cv.xview_moveto(max(0.0, min(1.0, (rx * w - ex) / w)))
        cv.yview_moveto(max(0.0, min(1.0, (ry * h - ey) / h)))
        self._sync_scrollbars(side)

    # ---------------- 联动滚动（两栏横向 + 纵向都同步） ----------------
    def _scroll(self, side, *args):
        self.canv[side].yview(*args)
        self._sync_scrollbars(side)

    def _hscroll(self, side, *args):
        self.canv[side].xview(*args)
        self._sync_scrollbars(side)

    def _from_bar(self, side, first, last):
        self.vsb[side].set(first, last)
        self._sync_scrollbars(side)

    def _from_hbar(self, side, first, last):
        self.hsb[side].set(first, last)
        self._sync_scrollbars(side)

    def _sync_scrollbars(self, src=None):
        """左右两块**横向 + 纵向**都对齐到 `src` 那一侧的位置。

        参考 2.0 `diff_preview._sync_scrollbars`：放大之后主要靠横向拖，
        横向纵向都要一起跟，否则「左边滑一次、右边滑一次」。
        """
        if self._syncing:
            return
        if self.view_mode.get() in (VIEW_SWIPE, VIEW_BLINK):
            # 滑动/叠加只有一块画布 —— 两版在同一个坐标系里，滚动天然同步，
            # 没有「对不上位」这回事。这里只需要把滚动条和滑钮跟上。
            self._syncing = True
            try:
                cv = self.canv["new"]
                self.vsb["new"].set(*cv.yview())
                self.hsb["new"].set(*cv.xview())
                self._swipe_sync_knob()
            except Exception:
                pass
            finally:
                self._syncing = False
            return
        self._syncing = True
        try:
            order = ("old", "new")
            if src in order:
                yf = self.canv[src].yview()[0]
                xf = self.canv[src].xview()[0]
                for s in order:
                    if s != src:
                        self.canv[s].yview_moveto(yf)
                        self.canv[s].xview_moveto(xf)
            for s in order:
                self.vsb[s].set(*self.canv[s].yview())
                if self.hsb.get(s) is not None:
                    self.hsb[s].set(*self.canv[s].xview())
        except Exception:
            pass
        finally:
            self._syncing = False

    def _wheel_zoom(self, side, event):
        """滚轮 = 缩放，锚定鼠标位置（滚到哪就放大到哪）。

        ⚠️ 触控板双指滑动会连发大量 MouseWheel 事件，不节流会一次跨过好几档
        并让主线程发卡 —— 40ms 内只认第一次。
        """
        now = time.monotonic()
        if now - self._last_zoom < 0.04:
            return "break"
        d = getattr(event, "delta", 0)
        if d == 0:
            num = getattr(event, "num", 0)
            step = 1 if num == 4 else (-1 if num == 5 else 0)
        else:
            step = 1 if d > 0 else -1
        if step:
            self._last_zoom = now
            self._zoom(step, anchor=(side, event.x, event.y))
        return "break"

    def _wheel(self, side, event):
        """Shift+滚轮 / 触控板双指：**纵向**滚动（两栏同步）。

        macOS 与 Windows 的 delta 符号相反，只取方向 ±1 折算滚动单位，
        方向交给 delta 本身，不猜正负。
        """
        d = getattr(event, "delta", 0)
        if d == 0:
            num = getattr(event, "num", 0)
            step = -1 if num == 4 else (1 if num == 5 else 0)
        else:
            step = int(d / max(1, abs(d)))
        if step:
            self.canv[side].yview_scroll(-step * 3, "units")
            self._sync_scrollbars(side)
        return "break"

    def _wheel_h(self, side, event):
        """横向滚动（Shift+滚轮 / 触控板双指横滑 / 鼠标左右滚轮）。"""
        d = getattr(event, "delta", 0)
        num = getattr(event, "num", 0)
        if d == 0 and num:
            step = -1 if num == 6 else (1 if num == 7 else 0)
        elif d:
            step = int(d / max(1, abs(d)))
        else:
            step = 0
        if step:
            self.canv[side].xview_scroll(-step * 3, "units")
            self._sync_scrollbars(side)
        return "break"

    def _drag_start(self, event):
        """按下左键：按在分割线附近就是「拖线」，其余都是「平移」。

        （叠加视图那颗「旧 / 新」开关 2026-11-02 晚搬进了底栏，事件直接由
        它自己的画布接住，这里不再需要判它 —— 页面画布上已经没有开关了。）
        """
        side = "old" if self.canv["old"] is event.widget else "new"
        if self._swipe_hit(side, event.x, event.y):
            self._swipe_dragging = True
            self._drag = None
            return "break"
        self._swipe_dragging = False
        self._drag = (event.x, event.y)

    def _release_drag(self, _event=None):
        self._drag = None
        self._swipe_dragging = False

    def _drag_move(self, event):
        """按住左键拖动 = 平移（放大之后靠滚动条太慢），两栏一起跟。

        滑动视图下一旦按在分割线上，这里就改成拖线 —— 走 `_swipe_set`，
        只改画布内部图形，实测 0.2~1ms/帧（改控件几何要 72ms，见文件头）。
        """
        if self._swipe_dragging:

            cv = self.canv["new"]
            w = max(1, self._swipe_img_w)
            self._swipe_set((event.x + cv.canvasx(0)) / float(w))
            return "break"
        if self._drag is None:
            self._drag = (event.x, event.y)
            return
        x0, y0 = self._drag
        side = "old" if self.canv["old"] is event.widget else "new"
        cv = self.canv[side]
        bx0, by0 = cv.canvasx(0), cv.canvasy(0)
        self._drag = (event.x, event.y)
        bb = cv.bbox("all") or (0, 0, 1, 1)
        cv.xview_moveto(max(0.0, (bx0 - (event.x - x0)) / max(1.0, bb[2])))
        cv.yview_moveto(max(0.0, (by0 - (event.y - y0)) / max(1.0, bb[3])))
        self._sync_scrollbars(side)

    def _on_resize(self, _e=None):
        # 四类胶囊的文案要跟着窗口宽度走：命中态（「② 混合型·原生+图片+转曲」）
        # 在 900px 极限窗放不下，得退回预设短名，见 `_fit_chips()`。
        self._fit_chips()
        if self._has_view() and self._auto_fit:
            self.render()

    def _on_close(self):
        # 先掐掉闪烁定时器：`after` 挂在已销毁的解释器上会抛 TclError，
        # 而这时窗口正在关，异常会被吞掉、只在终端留一串红字（看着像崩溃）。
        self._blink_stop()
        self._save_cfg()
        for d in list(self.docs.values()) + list(self.docs_plain.values()):
            try:
                d.close()
            except Exception:
                pass
        self.destroy()
