# -*- coding: utf-8 -*-
"""
diff_app5.py —— PDF差异对比工具 6.2（单窗口直显版）
==================================================================
6.1 追加（2026-11-02）：**界面只删不加，判定链仍是一行未改**。
  ① 「OCR 兜底」那颗下拉整个删掉。三档选择机制一并从界面移除，档位固定在引
     擎里（`core/pdfdiff_locate.OCR_RESCUE_MODE` 默认 "text" = 仅文字层）。
     为什么留 text 而不是 off：实测同一对文件 text 只多花 0~1 秒，却能撤掉
     「OCR 不可用导致的假删除」（DOC-1006 上 109 → 108 条）。
  ② 「对比精度（快 / 准）」那颗下拉也删掉了（同日，用户第二轮要求）。
     原话：「准也准不到哪里去，然后再加上这个叠加的功能，它其实已经也很准了，
     就是能够让人自己去识别。」像素层 dpi 固定成原来的默认档 120
     （`ui_theme.PIXEL_DPI`）：150 只把覆盖率从 58% 提到 83%，代价 +42% 耗时，
     而这 83% 照样要人眼看叠加确认 —— 不值得让用户背这个选择。
     ✅ 6.2（2026-11-06）版本号已跟着改：`APP_TITLE` = 6.2，工具名统一「PDF差异对比工具」。
        6.1 当时没改是因为会牵动 Windows 包和启动器文件名；这一轮两包一起改、一起对 md5。
6.1（2026-10-28）：**判定链一行未改**，只在「OCR 兜底（`_ocr_rescue`）」这一
步加了一个三档开关，用来对照测量它的准确率与耗时（2026-11-02 已从界面移除）。
6.0 起点（2026-10-26）：5.0 定版归档后，本文件从 6.0 备份包原样继承，
文件名仍叫 `diff_app5.py`（改文件名会牵动五份回归脚本与两处启动器，
收益为零，故保持不动；版本号只改 `APP_TITLE`）。
6.0 只加两件事，判定主线一行不动：
  ① 「快速定位（只标紫框）」——转曲/图片类**只定位不认字**，把「像素相减」
     与「OCR 认字」彻底拆开，产出紫色「待判定」框（`KIND_PENDING`）。
  ② `_row_template_match` 提速 A（同位置像素完全一致时短路模板匹配），
     提速 B（先验缩窗）保留为实验开关，默认关（实测会漏报，见常量注释）。
5.0 起点（2026-10-20）：4.0 定版后整体归档到 `vibecoding成品/项目2PDF差异对比/`，
本文件由 `diff_app3.py` 改名而来（文件名后缀终于对上了版本号），
改名当时**代码逻辑一行未改**，行为与 4.0 最终态完全一致；5.0 的新能力从这份基线往上加。
最终方案（用户 2026-10-13 晚拍板，2026-10-15 升级 4.0）：
  识别**用新版引擎**（core/pdfdiff_locate.locate：字符级 diff + 转曲区像素
  兜底 + 5 倍比例移动判定），展示为单窗口直显界面：上面选文件点对比，
  下面左右并排标注，一个窗口完事。不弹第二个预览窗，没有文字清单层。

  曾试过纯像素引擎（pdfdiff3.py），对文字 PDF 太糙（整段糊红绿），弃用；
  pdfdiff3.py 保留但不再被界面调用。

渲染管线（自包含，不依赖 diff_preview）：
  fitz.open(路径) → ann.paint_into(内存画标注) → get_pixmap → PNG → Tk 位图。
  「屏幕看到的」与 paint_into 同源，屏幕=导出。

2026-11-02 拆文件（逻辑一个字节没改，只是各自搬家）：
  ui_theme.py     视觉规格：颜色 / 字体 / 最小尺寸 / 图例排布
  ui_panels.py    面板：布局、控件工厂、四类胶囊、统计、勾选联动
  ui_viewport.py  画面：渲染 / 灰度合成 / 缩放 / 滚动 / 翻页 / 拖动
  ui_swipe.py     另外两种看法：滑动分割、叠加闪烁
  ui_dropzone.py  文件入口：拖放 / 浏览 / 交换 / 清空
  judge/pdfdiff_pairing.py  哪份是旧版（文件名配对）
  judge/pdfdiff_gate.py     能不能比、要不要先问
本文件只剩「流程与状态」：配置、体检、开跑、线程、收尾。
为什么这么分：改界面不用在三千行里翻，且每一层都能单独 import 验。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

# ---- 路径与四层（唯一一次装全）----
# 2026-11-02 目录重构：本文件从根目录挪进 `app/ui/`，配置也跟着从「程序旁边」
# 挪到**项目根的 data/**。所以这里不能再拿 `_HERE` 拼 —— 问 `app/paths.py`。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_sys = __import__("sys")
_APP = os.path.join(_ROOT, "app")
if os.path.isdir(_APP) and _APP not in _sys.path:
    _sys.path.insert(0, _APP)
import paths as _paths                                          # noqa: E402

_paths.install()        # ui / judge / engine / shell 四层一起进 sys.path

import pdf_annotate as ann                                      # noqa: E402
import pdfdiff_locate as loc           # 只为了「停止对比」的取消标志（见 _stop）
import pdfdiff_preflight as pre        # 开跑前体检：纯图片/扫描件先问一句
import pdfdiff_gate as gate            # 能不能比 / 要不要先问（纯决策）

# ⚠️ `messagebox` 必须能在**本模块**上取到：回归脚本用
#    `appmod.messagebox.showerror = ...` 顶掉弹窗，走的就是这条绑定。
CONFIG_PATH = _paths.CONFIG_PATH

# 四个 Mixin 拼出窗口：面板 / 画面 / 两种看法 / 文件入口。Mixin 之间没有同名
# 方法，先后只影响可读性 —— 最右边那个才是真正的基类（Tk 本身）。
from ui_panels import PanelsMixin                               # noqa: E402
from ui_viewport import ViewportMixin                           # noqa: E402
from ui_swipe import SwipeMixin                                 # noqa: E402
from ui_dropzone import DropMixin, TkinterDnD, _HAS_DND          # noqa: E402

# 界面常量整份转出来：本文件的代码照旧直接写 `BG` / `PIXEL_DPI`；回归脚本
# 也一直按 `appmod.CHIP_OFF_BG` 这种方式取常量 —— 搬文件不该让它们改口。
# `ui_theme.__all__` 已经把 `os` / `sys` / `ann` 挡在外面。
from ui_theme import *                                          # noqa: E402,F401,F403


def _fmt_error(e: BaseException) -> str:
    """异常 →「一句人话 + 完整栈落盘路径」。

    ⚠️ 2026-10-22 的教训：Windows 上只显示
       `expected str, bytes or os.PathLike object, not NoneType`
       —— 这种消息里没有一个项目专有名词，光看它无法定位是哪一行，
       只能靠反复猜。以后所有后台异常都连栈写进临时目录，
       用户把路径那一行发过来就能直接跳到文件和行号。
    栈用 `e.__traceback__` 显式取，不依赖 sys.exc_info 的上下文。
    """
    import tempfile
    import traceback
    msg = str(e).strip() or e.__class__.__name__
    log = os.path.join(tempfile.gettempdir(), "PDF差异6.2_错误日志.txt")
    try:
        tb = "".join(traceback.format_exception(
            type(e), e, e.__traceback__))
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("\n===== %s =====\n%s\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), tb))
        return msg + "\n\n（完整技术日志：%s —— 报障时把这一行发过来即可）" % log
    except Exception:
        return msg


class App(PanelsMixin, ViewportMixin, SwipeMixin, DropMixin,
          TkinterDnD.Tk if _HAS_DND else tk.Tk):                # type: ignore[misc]

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1240x860+80+60")
        self.configure(bg=BG)
        self.minsize(WIN_MIN_W, WIN_MIN_H)

        self.old_var = tk.StringVar()
        self.new_var = tk.StringVar()
        # 文件框显示用变量：只显示文件名（真实路径存在 old_var/new_var）
        self.old_disp = tk.StringVar()
        self.new_disp = tk.StringVar()
        # 拖放区里两行槽位的显示文本（空着时给一句灰字引导，别留一片白）
        self.old_slot = tk.StringVar()
        self.new_slot = tk.StringVar()
        # 2026-10-29：格式说明移到顶部标题行（`SUPPORT_HINT`）了，这里不再重复
        # 一遍「PDF / 图片 / Word / Excel」—— 同一句话在一屏里出现两次，用户反而
        # 会以为两处说的不是一回事。
        self.status_var = tk.StringVar(
            value="把两份文件拖进上方那块（拖两个自动排位，早的＝旧版）"
                  "→ 点「开始对比」，结果直接显示在下方")
        self.busy = False
        self.q = queue.Queue()
        self.marks = None
        # 拖入即预览（2026-10-29 用户要求）：没点「开始对比」也能先翻着看原件，
        # 所以「画面上有东西」不再等价于「marks 不为 None」。`docs` 才是唯一判据。
        # `_prev_token` 给预览的异步加载计数：连续拖文件时只认最后一次的结果，
        # 先发起、后返回的旧请求直接丢掉（否则会把上一份文件的画面盖回来）。
        self._prev_token = 0
        self._preview_ready = False     # 当前 docs 是「没标记的原件」
        # 比对过程中「先到的逐页结果」：docs 还没建好（预览仍在本台转）时先攒这儿，
        # 建好后 `_flush_parts()` 补画（见 `_on_part`）。
        self._parts = []
        self._stopping = False          # 已点「停止对比」、等后台真正退栈
        self._t0 = 0.0                  # 本轮开始时间（停止后要报已耗时）
        # 「开跑前提示」：2026-10-26 用户要求**删掉这个开关、永远开着** ——
        #   「这个东西我想要一直默认打开」。所以它不再是 tk 变量，也没有勾选框，
        #   `_gate_image_only()` 无条件生效。
        # 「紫框由谁决定」：2026-10-26 一天里改了两轮，最终口径是**两条并存**——
        #   ① 默认走哪条路由文件类型自动判（`pdfdiff_preflight.pixel_mode`）：
        #     · 每页都有文字层（原生/转曲留隐形字）→ 红/绿/蓝，紫框一次都不出现
        #     · 混合（有文字页 + 无文字页/大面积转曲）→ 文字区红绿蓝、其余紫
        #     · 整份都是图/转曲 → 全紫
        #   ② 但**要不要把紫色画出来**用户自己也能关（紫那颗勾在浅蓝右边，
        #     默认开）。自动判的是「有能力显示几种颜色」，勾选框管「这次显不显示」。
        #   判据与「开跑前提示」同源（`pre.pixel_mode` = `warn_kinds`），
        #   所以不会出现「没弹窗却没紫」或「弹了窗却全红绿」这种自相矛盾。
        # 本轮「这一轮为什么慢 / 为什么只给紫框」的标注（跑完/复位时清空）
        self._slow_note = ""
        # ---- 丢文件时就先判类型（2026-10-30 用户要求）----
        # 原来类型只在点「开始对比」时才算（`_gate_image_only`），用户想知道
        # 「这份能不能比、要怎么比」得先点一下、甚至等弹窗。现在文件一齐就
        # 后台判一次，结果缓存在这里：
        #   `_rep_key`  判的是哪一对（换文件必须作废，否则拿旧结论去比新文件）
        #   `_rep`      体检结果（`pre.assess` 的返回，与开跑时同源）
        #   `_rep_ok`   这份的提醒用户已经点过「继续对比」没有（点过就别再问）
        #   `_rep_busy` 还在判（开跑时若还没回来就自己再判一次，不干等）
        self._rep_key = None
        self._rep = None
        self._rep_ok = False
        self._rep_busy = False
        #: ⚠️ 2026-10-30 晚：原来这里有个 `ask_on_drop`，丢文件就用模态框问
        #:    「这份读不出文字，继续吗」。用户当场否掉 —— 原话：「有可能我就只是想
        #:    丢进来当一个查看器而已」。所以丢文件**只判不问**：点亮四类、
        #:    状态栏说一句，弹窗/拦截一律等用户真点「开始对比」。查看器体验优先。
        # 分类开关：删减/新增默认开，位移默认关（沿用 ann.KIND_DEFAULT_ON）；
        # 「待判定」紫同样默认开（用户：「默认是开启的状态，当然我们可以选择关闭它」）
        self.kind_on = {
            k: tk.BooleanVar(value=(k in ann.KIND_DEFAULT_ON
                                    or k == ann.KIND_PENDING))
            for k in LEGEND_ORDER}
        # 工具条那一格「这一对是什么类型」的文案（完整 / 缩短两份，窄窗自动降级）
        self._cls_on = ()               # 命中哪几类（空 = 还没判 / 读不出）
        self._cls_texts = {}            # 命中后每格的具体文案（{类号: 文案}）
        self._tip_refs = []             # 四格悬浮说明的引用（不存会被 GC 拆掉）
        self._fitting = False           # `_fit_chips` 重入保护（Configure 会连发）
        self._show_long = True          # 上次试的文案能不能放下（避免来回抖）
        self.page = 0                    # 当前页（0 基）
        self.dpi = 110                   # 当前渲染档
        self._auto_fit = True            # 打开/换文档后自动适应宽度
        self._reset_view = True          # 翻页后回顶部/左上角（缩放/勾选时不回）
        self._imgs: dict = {}            # side → PhotoImage（留引用防空白）
        self._cache: dict = {}           # (side, page, dpi) → PhotoImage
        self.docs: dict = {}             # side → 画好标注的内存文档
        self.docs_plain: dict = {}       # side → **没画标注**的同文件副本（底图灰度用）
        self._load_cfg()
        self._build_ui()
        self.after(120, self._poll)
        # 启动时若还记得上一对文件，顺手把类型判出来 —— 四格该亮就亮，
        # 省得用户以为「类型判断坏了」。**只判不弹窗**（丢文件时的口径，
        # 见 `_prejudge`）：一开程序就糊一个模态框在脸上最打扰。
        self.after(240, lambda: self._prejudge(self.old_var.get(),
                                               self.new_var.get()))
        self.bind("<Configure>", self._on_resize)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 记忆 ----------------
    def _load_cfg(self):
        # 2026-11-02：配置里不再有 `quality` 键 —— 「对比精度」下拉删掉了，
        # dpi 固定在 `ui_theme.PIXEL_DPI`。旧配置里残留的这个键不读也不写，
        # 下次存盘自然就没了（和 `export_dir` / `ocr_mode` 一个处理法）。
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.old_var.set(cfg.get("old", ""))
            self.new_var.set(cfg.get("new", ""))
        except Exception:
            pass
        self._sync_disp()

    def _save_cfg(self):
        try:
            # 2026-10-29：`export_dir` 随「导出 JSON」一起删了。旧配置里
            # 残留的这个键不读也不写，下次存盘自然就没了。
            _cfg = {"old": self.old_var.get(), "new": self.new_var.get()}
            # 2026-11-02：`quality` 键随「对比精度」下拉一起删了（dpi 固定）。
            # 2026-11-02：配置挪到 `data/`，那个目录**不随包分发**（空目录没意义），
            # 所以第一次存盘要自己建。按 CONFIG_PATH 的父目录建，测试里把它指到
            # 临时目录也照样成立，不用为测试单独写分支。
            _dd = os.path.dirname(os.path.abspath(CONFIG_PATH))
            if _dd and not os.path.isdir(_dd):
                os.makedirs(_dd, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(_cfg, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ---------------- 丢文件时就先判类型（2026-10-30） ----------------
    def _prejudge(self, a, b):
        """文件一齐就在后台判类型 —— 不等用户点「开始对比」，但**只判不问**。

        用户两轮要求，第二轮把第一轮的口径收紧了：
          第一轮「我丢这个文件进来的时候……能不能做到丢进来的时候就先判断好
                  类型，然后在比对的时候……该产生弹窗就弹窗、该不能对比就不能
                  对比，然后原生能对比就最好直接对比。」
          第二轮「我丢进来的时候让它先显示嘛，但是在我点击开始对比之后才开始
                  反馈……因为有可能我就只是想丢进来当一个查看器而已。」

        所以分工是：
          · **丢进来**：后台跑一次 `pre.assess()`（和开跑时**同一个函数**，结论
            不会打架），结果缓存在 `_rep`；工具条那排四类胶囊点亮；状态栏
            用一句话说清「能不能比 / 一会会不会问你」。**一个弹窗都不出**
            （连「不能对比」也不弹）—— 这一眼只是查看器，不该被打断。
          · **点「开始对比」**：`_gate_image_only` 用这份缓存做准入与提醒 ——
            不能比就弹错误框并拒绝开跑；读不出文字就弹确认框；原生直接跑。
            用户在丢文件阶段什么都没被打扰，真要做重活时才被问。

        ⚠️ 后台线程只读文件、不碰界面；结果经 `self.q` 回到主线程再更新界面
           （Tk 不是线程安全的）。
        ⚠️ `_rep_key` 是关键：用户连拖两个文件时，先发起的那次体检可能后回来，
           靠它把过期结论丢掉，否则会拿旧文件的结论去判新文件。
        """
        a, b = (a or "").strip(), (b or "").strip()
        if not a or not b or not (os.path.isfile(a) and os.path.isfile(b)):
            # 只剩一份 / 清空：结论作废，回到「还没判」的样子
            self._rep_key = None
            self._rep = None
            self._rep_ok = False
            self._rep_busy = False
            if not self.busy:
                self.btn_run.config(state="normal", text="开始对比")
            return
        if self._rep_key == (a, b):
            return                          # 同一对：已判过或正在判，别重复跑
        self._rep_key = (a, b)
        self._rep = None
        self._rep_ok = False
        self._rep_busy = True
        if not self.busy:
            # 上一对可能被判成「不能对比」而置灰了，换文件要恢复
            self.btn_run.config(state="normal", text="开始对比")

        def work():
            try:
                rep = pre.assess(a, b)
            except Exception:
                rep = None                  # 体检自己出错 → 当作「没结论」，不挡路
            self.q.put(("rep", a, b, rep))

        def _start():
            # ⚠️ 押后 250ms 再起线程：`_assign_paths` 紧接着就会发「打开原件预览」，
            #    两条线程同时开跑时，体检里的重活（渲染取样 + 可能的 OCR 初始化）
            #    会把 GIL / import 锁占住，预览那一帧就要多等 —— 用户要的是
            #    「拖进来立刻能看」，不能因为顺手判个类型反而变慢。
            if self._rep_key != (a, b):
                return                      # 这 250ms 里用户又换了文件
            threading.Thread(target=work, daemon=True).start()

        self.after(250, _start)

    # ---------------- 开跑前的「读不出文字」提醒 ----------------
    def _gate_image_only(self, a, b):
        """开跑前的准入 + 「读不出文字」提醒。返回 `(ok, rep)`。

        `ok=False` 有两种情况：**不许比**（准入拦下，弹错误框）和用户点了取消。
        `rep` 是体检结果（可能为 None，体检自己出错时），`_run` 用它决定
        要不要只给紫框（`rep["pixel"]`）和要跳过哪几页（`rep["skip_pages"]`）。

        ⚠️ 2026-10-30：丢文件时已经判过一次并存在 `_rep`（见 `_prejudge`），
           这里**先用缓存**，不再重复读文件；那份提醒用户若已确认过
           （`_rep_ok`），这里也不再问第二遍 —— 否则同一个文件被问两次，很烦。
           缓存对不上（换过文件 / 上次体检失败）才自己再判一次，保证正确优先。

        ---- 2026-10-29 新增：准入（`level == "block"`，直接不许跑）----
        用户定的三条硬规则，判据全在 `core/pdfdiff_preflight.assess()`：
          ① 只允许同格式互比（PDF↔PDF、Word↔Word、Excel↔Excel、图片↔图片）；
          ② 页数必须相同；
          ③ 纯扫描件不参与对比。
        这三条**不给「仍然对比」的按钮**：跨格式/页数不同比出来的每条差异都是
        版式差，扫描件比出来全是噪声 —— 让人点一下就能继续，等于把噪声放进去。
        「拼接 PDF 的扫描页」不算 block，走 `skip_pages` 局部跳过（见 `_run`）。

        ---- 原有的提醒（`level == "warn"`，问一句再跑）----
        判据全在 `core/pdfdiff_preflight.warn_kinds()`：
          · 整份都是图片/扫描件
          · 转曲占比高到接近全转曲（含「每页有字但正文全转曲」）
          · 混合（有文字页 + 无文字页）
        每页都有文字层的文件一律不问 —— 那种紫框一次都不会出现。

        ⚠️ 2026-10-26 口径变了（用户）：「它不一定是花费很久，而是可能不准。
           所以它花费的可能时间被压缩了，但是准确率可能不高了」。所以这里
           不再报「要等几分钟」，`_confirm_slow` 的正文也说清「紫框只标位置」。
        """
        try:
            # 丢文件时已经判过同一对 → 直接用，别再读一遍文件（大件能省 1~2 秒）
            rep = self._rep if self._rep_key == (a, b) else None
            prefetched = rep is not None
            if rep is None:
                rep = pre.assess(a, b)
        except Exception:
            self._slow_note = ""
            return True, None              # 体检自己出错不该挡住用户，交给引擎去跑
        # 「该拦 / 该问 / 该直接跑」的判据在 `judge/pdfdiff_gate.decide()`；
        # 这里只负责把那个动作**做出来**（弹框、写状态栏）。分家的好处是
        # 「什么情况会拦」不用再读这个 70 行的界面方法去推。
        d = gate.decide(rep, confirmed=bool(prefetched and self._rep_ok))
        if d["action"] == gate.ACT_BLOCK:
            self._slow_note = ""
            self.status_var.set("没开始：%s" % d["head"])
            # 用 error 图标而不是 warning：这不是「要不要继续」的选择题，是**不能比**。
            messagebox.showerror(APP_TITLE, d["body"])
            return False, rep
        if d["action"] == gate.ACT_GO:
            # 丢文件时已经问过、用户点了「继续对比」→ 这里不再问第二遍，
            # 只把「为什么只给紫框」那句话留在状态栏（`note` 可能为空）。
            self._slow_note = d["note"]
            if d["note"]:
                self.status_var.set("已确认：" + self._slow_note
                                    + "　·　可随时点「停止对比」")
            return True, rep
        self.status_var.set("开跑前检查：这份有读不出文字的部分，等你确认…")
        self.update_idletasks()
        if self._confirm_slow(rep):
            # 这句话要**留在运行期间**：进度一动就没人记得「为什么只给紫框」了，
            # 所以存成 _slow_note，由 _run / _poll 拼进状态栏（见下）。
            self._rep_ok = True
            self._slow_note = pre.pixel_hint(rep)
            self.status_var.set("已确认：" + self._slow_note
                                + "　·　可随时点「停止对比」")
            return True, rep
        self._slow_note = ""
        self.status_var.set("已取消（没有开始对比）　·　换个有文字层的 PDF 通常几秒就出结果")
        return False, rep

    def _confirm_slow(self, rep):
        """模态确认框。返回 True = 继续对比（按钮文字就叫「继续对比」）。

        自己搭 Toplevel 而不用 messagebox：要摆「两侧各一行说明 + 两段提示 + 两个按钮」，
        messagebox 摆不了这么多东西。

        ⚠️ 这里**故意不提供「以后不再提示」的勾选**（2026-10-21 用户定）：
           这类文件不是每次都有，点一下确认的时间可以忽略，
           但一旦给了复选框，人就会习惯性勾掉，之后所有提示都形同虚设。
           2026-10-26 用户又要求「这个提示一直默认打开」，于是连工具栏那颗
           「开跑前提示」总开关也一起删了 —— 现在没有任何开关能关掉它。
           说清这件事很重要：这个框**不是**在问「你愿不愿意等几分钟」，
           而是在说「接下来的紫框只标位置、不判增删，每条要自己核」。
        """
        dlg = tk.Toplevel(self)
        dlg.withdraw()
        dlg.title(APP_TITLE)
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.configure(bg=BG)
        state = {"ok": False}

        card = tk.Frame(dlg, bg=BG, padx=20, pady=16)
        card.pack(fill=tk.BOTH, expand=True)

        # 标题：用琥珀色而不是红 —— 这不是错误，是「先提醒再继续」
        tk.Label(card, text="⚠  " + rep["title"], bg=BG, fg="#b26a00",
                 font=(FONT, 13, "bold"), anchor="w").pack(fill=tk.X)
        for ln in rep["lines"]:
            tk.Label(card, text=ln, bg=BG, fg=TEXT, font=(FONT, 10),
                     anchor="w", justify="left").pack(fill=tk.X, pady=(7, 0))
        tk.Frame(card, bg=BORDER, height=1).pack(fill=tk.X, pady=(12, 10))

        head, tail = pre.body_text(rep)
        for para in (head, tail):
            tk.Label(card, text=para, bg=BG, fg=TEXT2, font=(FONT, 10),
                     anchor="w", justify="left",
                     wraplength=470).pack(fill=tk.X, pady=(0, 7))

        bar = tk.Frame(card, bg=BG)
        bar.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(bar, text="继续对比，还是先取消？").pack(side=tk.LEFT)

        def done(ok):
            state["ok"] = bool(ok)
            try:
                dlg.grab_release()
            except tk.TclError:
                pass
            dlg.destroy()

        # 按钮按 macOS 习惯：左「取消」、右「继续对比」（主操作在最右）。
        # ⚠️ 两个按钮都**不抢焦点**，回车/空格不会替用户做决定。
        ttk.Button(bar, text="继续对比", style="Primary.TButton",
                   command=lambda: done(True)).pack(side=tk.RIGHT)
        ttk.Button(bar, text="取消", style="Small.TButton",
                   command=lambda: done(False)).pack(side=tk.RIGHT, padx=(0, 8))
        dlg.bind("<Escape>", lambda _e: done(False))
        dlg.protocol("WM_DELETE_WINDOW", lambda: done(False))

        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 3)
        dlg.geometry("+%d+%d" % (x, y))
        dlg.deiconify()
        # ⚠️ **不给按钮设焦点**：设了之后空格/回车可能把焦点按钮直接按掉，
        #    模态框会在用户还没看清时就自己关掉（测试里复现过）。
        #    焦点留在对话框本身，必须用鼠标（或 Tab 到按钮后）主动选。
        dlg.focus_set()
        dlg.grab_set()
        self.wait_window(dlg)
        return state["ok"]

    # ---------------- 对比 ----------------
    def _run(self):
        if self.busy:
            return
        a, b = self.old_var.get().strip(), self.new_var.get().strip()
        if not a or not b:
            messagebox.showwarning(APP_TITLE, "请先选择旧版和新版（PDF / 图片 / Word / Excel）")
            return
        if not os.path.isfile(a) or not os.path.isfile(b):
            messagebox.showerror(APP_TITLE, "文件路径无效，请重新选择")
            return
        # 开跑前体检：读不出文字的部分（整页图 / 转曲 / 混合）先问一句。
        # `_rep` 顺带给出本轮要不要「只定位不认字」（`rep["pixel"]`）——
        # 判据与弹窗同源，不会出现「没问却全紫」或「问了却全红绿」。
        _ok, _rep = self._gate_image_only(a, b)
        if not _ok:
            return
        # Word / Excel 输入：转换发生在后台比对线程里，提前在状态栏说一声
        # （转出来的 PDF 带完整文字层，走逐字比对，通常几秒内完成）。
        try:
            import pdfdiff_office as _off
            if _off.is_office_file(a) or _off.is_office_file(b):
                _note = "Word/Excel 转 PDF 后比对"
                self._slow_note = (self._slow_note + "　·　" + _note
                                   if self._slow_note else _note)
        except Exception:
            pass
        # ⚠️ 必须在这里清标志，且必须在 `busy` 早退之后清：
        #    上一轮点过停止的话标志还立着，不清就会把新一轮立刻打死；
        #    而如果放在 busy 检查之前，用户在「停止中…」里误点「开始对比」
        #    会把还没退栈的线程的取消请求抹掉 → 停不下来。
        loc.clear_cancel()
        # 像素层 dpi 是常量（`ui_theme.PIXEL_DPI`）—— 界面上没有档位可切。
        # 取值仍然放在这里而不是写死在 work() 里：① 和下面 `_fast` / `_skip`
        # 保持同一个写法，将来真要再加档时只改这一处；② 明显一点，免得以后有人
        # 以为它跟界面还有关系。
        _gdpi = PIXEL_DPI
        self._stopping = False
        # 2026-10-29：新的一轮开始前把画面切回「无标记原件」——
        # ① 上一轮的标记必须清掉，否则新框会叠在旧标记上，分不清哪条是哪轮的；
        # ② 转曲/图片件要十几分钟，先把原件显示出来给人翻着看，紫框逐页补
        #    （见 `_on_part`）。`keep_page=True`：用户可能正翻到第 5 页等着。
        self.marks = None
        self._parts = []
        self._update_counts()
        self._preview_refresh(keep_page=True)
        self.busy = True
        self._t0 = time.time()
        # 走不走「只定位不认字」由体检结果定（不是开关，见 __init__ 的说明）。
        # ⚠️ 这个值必须在**主线程**取好再交给后台线程：work() 里再去读界面状态
        #    会撞上 Tk 的解释器锁。
        _fast = bool((_rep or {}).get("pixel"))
        # 拼接 PDF 里被判成扫描件的那几页：**两侧都不比**（2026-10-29 用户要求）。
        # 同样必须在主线程取好再交给后台线程（理由同上）。
        _skip = tuple(int(i) for i in ((_rep or {}).get("skip_pages") or ()))
        if _skip:
            _skip_note = "第 %s 页是扫描件，不参与比对" % "、".join(str(i + 1) for i in _skip)
            # ⚠️ 2026-11-02 拆文件时查出来的**真 bug**：这里原来写的是裸 `_slow_note`
            #    （左边是 `self._slow_note`，右边却漏了 `self.`）。只要同时满足
            #    「体检提醒已确认过」（_slow_note 非空）和「有扫描页要跳过」，
            #    这一行就抛 NameError —— 也就是「拼接 PDF 且读不出文字」这一路
            #    开跑必崩。之前的回归没覆盖这个组合，所以一直没被发现。
            self._slow_note = (self._slow_note + "　·　" + _skip_note
                               if self._slow_note else _skip_note)
        # 工具条那排「四类」：用的就是刚才体检那份结果，不额外再读一遍文件。
        # 两侧不同类时两格同时亮（如旧原生、新混合型），不替用户合并成一个。
        # `class_text` 给的是「命中的那类里具体哪一种」（混合·原生+图片 /
        # 纯转曲），未命中的格子仍显示预设短名。
        self._set_class(pre.class_of_pair(_rep), pre.class_text(_rep))
        self.btn_run.config(state="disabled", text="对比中…")
        self.btn_stop.config(state="normal", text=STOP_IDLE)
        self.status_var.set(
            "正在比对…" + ("　·　" + self._slow_note if self._slow_note else "")
            + "（选错了可随时点「停止对比」）")

        def work():
            try:
                # 老引擎（字符级 + 转曲区像素兜底）：识别质量久经考验，比纯像素强。
                # `fast_mode=True`（6.0）：转曲/图片类只出紫框，不 OCR 认字。
                # `on_page`（2026-10-29）：快速模式每算完一页就先推回界面，
                # 不必等整份（14 页转曲件十几分钟，第 1 页几秒就好了）。
                marks = ann.locate_marks(
                    a, b, prog=lambda i, t: self.q.put(("prog", i, t)),
                    fast_mode=_fast, skip_pages=_skip, graphic_dpi=_gdpi,
                    on_page=lambda i, items: self.q.put(("part", i, items)))
                marks["elapsed"] = time.time() - self._t0
                self.q.put(("done", marks))
            except loc.Cancelled:
                # 用户主动中止：不是错误，单独走一条消息（不弹错误框）
                self.q.put(("cancelled",))
            except Exception as e:
                # ⚠️ 只把 str(e) 甩到界面上是不够的（2026-10-22 Windows 上一条
                #    「expected str, bytes or os.PathLike object, not NoneType」
                #    花了半天猜是哪一行）。异常一律连**完整栈**写进临时目录日志，
                #    界面文案尾部带上路径 —— 用户把那一行发过来就能定位，
                #    不用再靠复述和猜。
                self.q.put(("err", _fmt_error(e)))

        threading.Thread(target=work, daemon=True).start()

    def _stop(self):
        """停止当前比对。

        只做一件事：置取消标志。真正的退栈由引擎在下一个检查点完成
        （OCR 前后 / 区域之间 / 页之间），所以这里是**异步**的 —— 按钮先变
        「停止中…」，等后台线程退了才恢复可用。绝不 join 等待，否则界面卡死。
        """
        if not self.busy or self._stopping:
            return
        self._stopping = True
        loc.request_cancel()
        self.btn_stop.config(state="disabled", text=STOP_GOING)
        self.status_var.set("正在停止…（等当前这一小块识别收尾，通常 1~2 秒）")

    def _poll(self):
        """每 120ms 取一次后台消息。

        ⚠️ 2026-10-26 修：**单条消息处理失败不许把循环打死**。
           原来 `_poll` 自己在末尾重新 `after`，任何一处抛异常（比如结果结构
           不完整时 `_on_done` 里的 KeyError）都会让这行**永远跑不到** ——
           界面从此不再刷新，按钮停在「对比中…」，看起来像卡死，而且没有任何
           提示。现在把消息处理包起来：出错就把原因写进状态栏，循环照转。
        """
        try:
            while True:
                try:
                    msg = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_msg(msg)
                except Exception as e:      # noqa: BLE001
                    self.status_var.set(f"处理结果时出错：{e}")
        finally:
            self.after(120, self._poll)

    def _handle_msg(self, msg):
        if msg[0] == "rep":
            # 丢文件时那次后台体检回来了（2026-10-30）
            self._on_rep(msg[1], msg[2], msg[3])
            return
        if msg[0] == "preview":
            # 拖入即预览的异步加载回来了。`tok` 对不上说明用户已经又拖了
            # 别的文件 —— 旧请求晚到，直接丢掉，别把新画面盖回去。
            if msg[1] != self._prev_token:
                return
            self._open_preview_docs(msg[2], keep_page=(len(msg) > 3 and msg[3]))
            return
        if msg[0] == "part":
            # 快速模式逐页结果（见 `graphic_regions` 的 `on_page`）
            self._on_part(msg[1], msg[2])
            return
        if msg[0] == "prog":
            i, total = msg[1], msg[2]
            if total:
                # ⚠️ prog 的 total 是**进度总量**，两处语义不同：
                #   页级循环传页数（pdfdiff_locate.py:1132），
                #   字符级 diff 传的是 opcodes 块数（:1822）。
                #   两者混在同一个回调里，所以这里**绝不能写「第 X 页」**——
                #   1 页文档会被显示成「正在比对第 25 页」（用户 2026-10-19 报的
                #   「导入一页 PDF 却显示第 25 页」正是此因）。
                pct = int(100 * i / max(1, total))
                if not self._stopping:
                    _sn = ("　·　" + self._slow_note
                           if self._slow_note else "")
                    self.status_var.set(f"正在比对… {pct}%{_sn}")
            elif not self._stopping:
                self.status_var.set("正在比对…")
        elif msg[0] == "done":
            # 点了停止、而后台恰好已经算完（窗口只有 120ms 级）：
            # 按用户意志**丢弃**这次结果，不覆盖画面 —— 否则刚点完停止
            # 屏幕就刷出一份「选错的那对文件」的结果，比不刷新更误导。
            if self._stopping:
                self._on_cancelled()
            else:
                self._on_done(msg[1])
        elif msg[0] == "cancelled":
            self._on_cancelled()
        elif msg[0] == "err":
            self.busy = False
            self._stopping = False
            self.btn_run.config(state="normal", text="开始对比")
            self.btn_stop.config(state="disabled", text=STOP_IDLE)
            self.status_var.set(f"失败：{msg[1]}")
            messagebox.showerror(APP_TITLE, f"失败：\n{msg[1]}")

    def _on_cancelled(self):
        """已停止：恢复按钮，**保留上一次的结果画面**，不重绘不写统计。

        为什么不重绘：用户点停止是因为「选错文件了」，此时画面上的东西
        应该是他上回看的那份（或空），而不是半份新结果。
        """
        el = time.time() - self._t0
        self.busy = False
        self._stopping = False
        self._parts = []
        self.btn_run.config(state="normal", text="开始对比")
        self.btn_stop.config(state="disabled", text=STOP_IDLE)
        if self.marks is None:
            self.status_var.set(f"已停止　·　耗时 {el:.1f}s　·　未产生结果")
        else:
            self.status_var.set(
                f"已停止　·　耗时 {el:.1f}s　·　本次结果已丢弃，画面仍是上一次的结果")

    def _on_done(self, marks):
        self.busy = False
        self._stopping = False
        self.marks = marks
        # ⚠️ 2026-10-29 实测踩到：`_run` 开头发起的那次「原件预览」是异步的，
        #    文件大 / Office 转换时它可能**比比对还晚**回来，然后
        #    `_open_preview_docs` 把 marks 清掉、docs 换成无标记原件 ——
        #    画面和计数全被打回「还没比」。这里把 token 推进一格，
        #    让所有还在路上的预览加载作废（`_handle_msg` 里按 token 丢弃）。
        self._prev_token += 1
        # 逐页先画的那批到此为止：下面 `_add_annots()` 会用精确结果全量重画
        self._parts = []
        self._preview_ready = False
        # 这一批有紫框才让紫那颗勾点得动（纯原生 PDF 勾着也没东西可画）
        self._refresh_pending_check()
        self.btn_run.config(state="normal", text="开始对比")
        self.btn_stop.config(state="disabled", text=STOP_IDLE)
        self.btn_prev.config(state="normal")
        self.btn_next.config(state="normal")
        for w in (self._zoom_out, self._zoom_in):
            w.config(state="normal")
        # 统计不再写进状态文字（右侧两个 lbl 已按桶列出，且与画面同源）。
        # 旧文案「旧版标红 {marked} 处」是错的：`stats["marked"]` 含**位移（浅蓝）**，
        # 41 = 删减 3 + 位移 38，画面红框却只有 3 个 —— 数字和画面对不上。
        self._update_counts()
        _sn = ("　·　" + self._slow_note) if self._slow_note else ""
        # 6.1：耗时时带上 OCR 兜底的账，方便对照三档（次数 / 实耗时 / 撤销条数）
        _st = getattr(loc, "LAST_OCR_STATS", None) or {}
        _ocr_txt = ""
        if _st:
            _oc = int(_st.get("calls", 0) or 0)
            _os = float(_st.get("secs", 0.0) or 0.0)
            _on = len(_st.get("revoked") or [])
            if _oc or _on:
                _ocr_txt = f"　·　OCR {_oc} 次 {_os:.1f}s，撤销 {_on} 条"
            else:
                _ocr_txt = "　·　OCR 0 次（未触发）"
        self.status_var.set(f"完成　·　耗时 {marks.get('elapsed', 0):.1f}s{_ocr_txt}{_sn}")
        self._slow_note = ""
        # 重开标注文档 + 回到第 1 页 + 自适应宽度
        self.page = 0
        self._auto_fit = True
        self._reset_view = True
        self._add_annots()
        self.render()


def _ocr_selfcheck(app) -> None:
    """启动后自检 OCR 引擎；不可用时明确告警，不让它静默失效。

    为什么必须弹窗：OCR 引擎不可用时，`_ocr_image` 会按引擎表一路降级到
    返回空串 —— 整条「转曲文字 / 图片区」识别**静默失效**，界面不报错、
    不崩溃，结果看起来只是「少标了几处」。用户会归因为「识别不准」，
    实际是根本没在认字，查起来极费劲（DOC-1001 排查时踩过同类坑）。

    最常见根因：Windows 的 VC++ 运行库版本低于 ONNX Runtime 1.31 要求的
    14.40，`import onnxruntime` 加载 .pyd 时 DLL 解析失败。
    """
    try:
        import pdfdiff_locate as loc

        if loc._rapidocr_engine() is not None:
            return  # 首选引擎正常，什么都不说
        if loc._vision_exe() or loc._OCR_EXE:
            # 还有兜底引擎：能用，但识别质量下降，状态栏提一句即可
            app.status_var.set("提示：RapidOCR 不可用，已降级到备用 OCR 引擎")
            return
        hint = ("安装微软官方 VC++ 运行库后重启电脑\n"
                "https://aka.ms/vs/17/release/vc_redist.x64.exe"
                if os.name == "nt" else
                "检查 rapidocr / onnxruntime 是否安装成功")
        messagebox.showwarning(
            APP_TITLE,
            "OCR 引擎不可用：图形区与转曲文字的差异将无法识别。\n\n"
            "程序不会崩溃，但结果是「该标出来的差异没标出来」。\n\n"
            "最常见原因：ONNX Runtime 的原生依赖加载失败。\n\n"
            "修复：" + hint)
    except (ImportError, OSError, AttributeError) as e:
        # 自检本身失败不该挡住启动，但必须留痕，不能静默
        print("warning: OCR 启动自检跳过（%s: %s）"
              % (type(e).__name__, e), file=_sys.stderr)


def main():
    app = App()
    app.after(400, lambda: _ocr_selfcheck(app))
    app.mainloop()


if __name__ == "__main__":
    main()
