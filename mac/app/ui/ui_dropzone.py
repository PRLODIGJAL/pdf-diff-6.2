# -*- coding: utf-8 -*-
"""
ui_dropzone.py —— **文件怎么进来**：拖放、浏览、交换、清空。
==================================================================
只有一块拖放区（2026-10-20 用户要求「三行合成一块」）：一次拖两个、或者
一份一份拖，都走同一套自动排位 —— 早的当旧版。排位规则在
`judge/pdfdiff_pairing.py`，这里只负责把结果写进界面。

四个动作（拖 / 选 / 换 / 清）都收敛到 `_assign_paths()` 一个入口，所以三种
入口进去出来的状态一定一致：重排 → 刷显示 → 判类型（`App._prejudge`）→ 开预览。

⚠️ 这里**不许**自己判哪个是旧版：谁旧谁新是业务判定，判错了整套红绿都是反的。
   规则改了要能脱离界面单独验，所以它在 judge 层。

📌 拖放能力（`tkinterdnd2` 装没装上）也在这里探一次：这是**拖放模块**的
   家事，`_HAS_DND` / `DND_FILES` / `TkinterDnD` 谁要用谁来取。

2026-11-02 目录重构时从 `diff_app5.py` 原样切出来，逻辑一个字节没改。
"""

from __future__ import annotations

import os
import sys

import tkinter as tk
from tkinter import filedialog

# 本文件是 ui 层的一个模块，单独 import 也要能用：先把 `app/` 塞进 sys.path，
# 再让 `paths.install()` 把 ui/judge/engine/shell 四层一起铺开。
_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP not in sys.path:
    sys.path.insert(0, _APP)
import paths as _paths                                          # noqa: E402

_paths.install()

from pdfdiff_pairing import pair_from_paths                     # noqa: E402
from ui_theme import BORDER, DZ_BG, DZ_BG_ON, DZ_BORDER_ON      # noqa: E402

try:                                                            # noqa: E402
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:                                               # pragma: no cover
    TkinterDnD = None
    DND_FILES = None
    _HAS_DND = False


class DropMixin:
    """拖放区与「选文件」那一套。`App` 继承它。"""

    # ---------------- 拖放（唯一入口） ----------------
    def _drop_paths(self, data):
        """把 tkinterdnd2 的 `event.data` 解析成真实存在的文件路径列表。

        格式坑（macOS）：多个文件是 Tcl 列表，路径带空格时会被花括号包起来，
        `{/a b/x.pdf} {/c/y.pdf}`；单个文件也可能带花括号。先按整串判断
        是不是一个存在的文件（避免把带空格的单路径拆成两半），再退到
        `splitlist`（Tcl 列表解析，天然认花括号）。
        """
        raw = (data or "").strip()
        if not raw:
            return []
        single = raw.strip("{}").strip()
        if os.path.isfile(single):
            return [os.path.normpath(single)]
        try:
            parts = list(self.tk.splitlist(raw))
        except Exception:
            parts = [raw]
        out = []
        for p in parts:
            p = str(p).strip().strip("{}").strip()
            if p and os.path.isfile(p):
                out.append(os.path.normpath(p))
        return out

    def _dz_paint(self, active: bool):
        """拖进来时把整块点亮（不然用户不知道拖没拖对地方）。

        整块一起变色：这块现在既是投放区、也是「当前选了哪两个文件」的显示区，
        只亮提示那一行会让人以为下半截不能拖。
        """
        bg = DZ_BG_ON if active else DZ_BG
        bd = DZ_BORDER_ON if active else BORDER
        for w in self._dz_targets:
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass
        try:
            self.drop_zone.configure(highlightbackground=bd, highlightcolor=bd)
        except tk.TclError:
            pass

    def _drop_enter(self, event=None):
        self._dz_paint(True)

    def _drop_leave(self, event=None):
        self._dz_paint(False)

    def _on_drop_many(self, event, side=None):
        """拖放入口。`side` 只为兼容旧调用保留，界面已不再按侧投放。"""
        paths = self._drop_paths(getattr(event, "data", ""))
        self._dz_paint(False)
        self._assign_paths(paths, side)

    def _assign_paths(self, paths, side=None):
        """把一到多个文件分配进旧版/新版（**界面唯一入口**：拖放与「浏览…」共用）。

        规则（2026-10-20 用户定的操作习惯）：
        · 两个以上：按文件名自然序，早的＝旧版、晚的＝新版；多余的不收，状态栏说明。
        · 一个 + 两侧都空：先占**旧版**（第一份默认是旧的，第二份进来再排）。
        · 一个 + 只填了一侧：与已有那份一起**重新按文件名排位**（不看拖的时间先后）。
        · 一个 + 两侧都满：默认**挤掉旧版** —— 留下「现有的新版 + 刚拖进来的」，
          再按文件名排位。第三份进来的常见场景就是「上一版我拖错了/这是更新的一版」，
          所以淘汰的永远是旧的那头，而不是随机顶掉一侧。
        · `side`（"old"/"new"）：仅测试与旧调用用，显式指定某一侧时不排队、只替换那侧。
        """
        if not paths:
            self.status_var.set("拖进来的不是文件（只认 PDF / 图片 / Word / Excel 的实际路径）")
            return
        cur_o = self.old_var.get().strip()
        cur_n = self.new_var.get().strip()
        if len(paths) >= 2:
            old, new = pair_from_paths(paths)
            note = "已按文件名排序分配（早的＝旧版）"
            if len(paths) > 2:
                note += "；一次只收两个，已取排序前两个"
        else:
            p = paths[0]
            if side in ("old", "new"):
                old, new = (p, cur_n) if side == "old" else (cur_o, p)
                note = "已放到「%s」" % ("旧版" if side == "old" else "新版")
            elif not cur_o and not cur_n:
                old, new = p, ""
                note = "已放到「旧版」（再拖一份进来就自动排位）"
            elif not cur_o:
                old, new = pair_from_paths([cur_n, p])
                note = "已按文件名排位"
            elif not cur_n:
                old, new = pair_from_paths([cur_o, p])
                note = "已按文件名排位"
            elif p in (cur_o, cur_n):
                old, new = cur_o, cur_n
                note = "这份已经在列表里了，未改动"
            else:
                # 挤掉旧版：{现有的新版, 刚拖进来的} 重新排位
                old, new = pair_from_paths([cur_n, p])
                note = "已挤掉旧版（留「%s」+ 这份，重新排位）" % (
                    os.path.basename(cur_n) or "—")
        self.old_var.set(old)
        self.new_var.set(new)
        self._sync_disp()
        self._save_cfg()
        # 2026-10-30：文件一齐就先在后台判类型（详见 `_prejudge`）。
        # 放在 `_sync_disp` 之后 —— 它会把四类胶囊按「换文件」清灰，这里再点亮。
        self._prejudge(old, new)
        self._preview_refresh()
        if self._rep_busy:
            note += "　·　正在判断文件类型…"
        self.status_var.set("%s　旧版：%s　｜　新版：%s" % (
            note, os.path.basename(old) or "—", os.path.basename(new) or "—"))

    def _pick(self):
        """「浏览…」：一次挑**一到两个**，走与拖放**完全同一套**分配规则。

        （2026-10-21 用户要求）原来这里用的是单选对话框：拖放能一次给两个，
        浏览却只能一个一个来，两套入口行为不一致。改成多选后，挑一份与挑两份
        都行 —— 挑一份按「填空 / 重排」规则走，挑两份按文件名排序，与拖放逐字相同。
        """
        ps = filedialog.askopenfilenames(
            title="选择一或两个文件（旧版 + 新版，会自动排位）",
            initialdir=(os.path.dirname(self.old_var.get())
                        or os.path.dirname(self.new_var.get()) or None),
            filetypes=[("PDF / 图片 / Word / Excel",
                        "*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff"
                        " *.doc *.docx *.docm *.rtf *.xls *.xlsx *.xlsm"),
                       ("所有文件", "*.*")])
        if not ps:
            return
        # tkinter 正常返回 tuple；个别 Tk 版本把 Tcl 列表整串吐回来，
        # 这里统一过一次 splitlist，避免带空格的路径被劈成两段。
        if isinstance(ps, str):
            ps = self.tk.splitlist(ps)
        paths = [os.path.normpath(p) for p in ps if str(p).strip()]
        if paths:
            self._assign_paths(paths)

    def _clear_pair(self):
        """「清空」：两侧一起清掉，回到「拖进来就能用」的状态。

        对比进行中不清 —— 后台线程手里还攥着这对路径，清了只会让状态栏和
        实际在跑的东西对不上；先点「停止对比」再来。
        """
        if self.busy:
            self.status_var.set("正在对比，先点「停止对比」再清空")
            return
        self.old_var.set("")
        self.new_var.set("")
        self._sync_disp()
        self._save_cfg()
        self._prejudge("", "")          # 类型结论作废，「取消不能对比」的置灰也恢复
        # 清空 = 连画面一起清（2026-10-29 起画面就是「原件预览」，留着上一对的
        # 页面会让用户以为这就是刚清掉的那份）。
        self._preview_refresh()
        self.status_var.set("已清空选择　·　把文件拖进来或点「浏览…」重新选")

    def _swap(self):
        a, b = self.old_var.get(), self.new_var.get()
        self.old_var.set(b)
        self.new_var.set(a)
        self._sync_disp()
        self._save_cfg()
        self._prejudge(b, a)            # 顺序换了要重新判（页数/格式的结论会跟着变）
        self._preview_refresh()
        if a or b:
            self.status_var.set("已交换：旧版 ⇄ 新版")
