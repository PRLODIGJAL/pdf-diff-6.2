# -*- coding: utf-8 -*-
"""开跑前的「输入体检」：这对文件是不是**纯图片/扫描件**，要不要先问一句。

为什么要有这个模块（2026-10-21 用户要求）
------------------------------------------------------------------
纯图片 PDF、纯扫描件、以及 PNG/JPG 直接投进来的图片，**都能跑**，但它们走的是
OCR 路线：单页实测 100~280 秒（原生 PDF 只要几秒），结论只能当「提示器」，
每条都得人工核。所以开跑前先说一句，比跑到一半才发现要等五分钟好。

⚠️ 只提醒**纯图片类**，不提醒转曲类（2026-10-21 用户补充）：
   「转曲类的就不用在意了，因为它并不是每次都出现，也并不是百分百全部都是转曲。
     只对那种图片类、还有导入 PNG 的，就是纯图片类进行提示。
     像 DWG 那种中间穿插图片类的也不需要提示。」
   所以判据里必须把「扫描件」和「转曲（矢量）」分开 —— 两者都是「没有文字层」，
   但一个是**整页位图**、一个是**矢量线条**。

判据：文字层 → 位图 → 矢量，三级
------------------------------------------------------------------
逐页看，先看文字层（这是决定准确率的那条线，见项目说明第十四节）：

    文字字符 ≥ MIN_TEXT_CHARS           → 文字页（逐字比对，快且准）
    无文字 + 位图覆盖 ≥ IMG_COVER_MIN   → 图片页（扫描件/标签图，只能 OCR）
    无文字 + 位图覆盖 <  IMG_COVER_MIN  → 矢量页（转曲/图形，按图形比对）

整份的结论：

    有文字页                             → text / mixed   （**不提醒**）
    没有文字页，图片页占多数或全部        → scan           （**提醒**）
    没有文字页，矢量页更多               → vector         （不提醒，按用户这条）

⚠️ 图像覆盖率**不能单独当判据**：`DOC-1008 rev01` 整页是一张图（覆盖 1.00）但
   文字层 2/2 完整，引擎走逐字比对，快且准；`DWG` 图像覆盖 24% 且文字层完整。
   这两种都不该被打扰 —— 所以「有文字页」永远优先。

只读、不渲染、不 OCR
------------------------------------------------------------------
全程只调 `get_text` / `get_images` / `get_image_rects` / `get_drawings`，
**不 get_pixmap、不调 OCR、不写缓存**。实测 3 页 DWG 约 0.5 秒、1 页 IFU 约 0.02 秒；
超过 `MAX_PAGES` 页时等间隔抽样（只影响提示精度，不影响判定）。

⚠️ 2026-10-29 一个例外：**判扫描件要渲染**。扫描件和「软件导出的整页位图」
   在 PDF 结构上完全一样（都是无文字层 + 一张满页图），只能看像素。
   所以只在「无文字 + 整页位图」这一小撮页上取 100dpi 灰度（单页约 20ms），
   其余页一次 pixmap 都不取。判据与实测数据见 `SCAN_BAND_MIN` 那段注释。

准入（2026-10-29 用户要求，`assess()` 的 `level == "block"`）
------------------------------------------------------------------
1. **只允许同格式互比**：PDF↔PDF、Word↔Word、Excel↔Excel、图片↔图片；
   跨格式直接不许跑（Word 比 Excel 的「差异」全是版式差，没有一条可看）。
2. **页数必须相同**：不同就拦住，不做「整页增删」那套。
3. **纯扫描件不许比**：扫描噪声会让逐像素对比全是假差异。
4. **拼接 PDF 的扫描页不参与比对**：某一页是扫描件、其余页正常时，只跳过
   那几页（用户原话：「哪怕它是图片或者转曲，那就直接第一页默认不参加比对」），
   其余页照常比。页号由 `assess()["skip_pages"]` 给出，界面传给引擎。

⚠️ 本模块 2026-10-29 起**在判定链上**（准入与跳页都靠它），
   `diff_app5` 每次开跑前都调 `assess()`；三处准入规则的回归在
   `_regress/ui_preflight_test.py` 与本文件 `self_test()`。
"""
import os
import re

try:
    import pymupdf as fitz
except ImportError:                       # pragma: no cover
    import fitz  # type: ignore

# 引擎的「转曲文字」判据（pdfdiff_embed 第 440~444 行）。**必须复用同一套**，
# 不能自己另定：这里判的「转曲」要和引擎实际走块级/OCR 的对象一致，
# 否则提示和真实行为对不上（见项目说明第十四节）。
try:
    from pdfdiff_embed import (  # noqa: E402
        VECTOR_TEXT_H_MIN, VECTOR_TEXT_H_MAX,
        VECTOR_TEXT_ITEMS_MIN, VECTOR_ROW_Y_TOL,
    )
except Exception:                         # pragma: no cover  独立自测也能跑
    VECTOR_TEXT_H_MIN = 3.0
    VECTOR_TEXT_H_MAX = 15.0
    VECTOR_TEXT_ITEMS_MIN = 50
    VECTOR_ROW_Y_TOL = 8.0

# 与 core/pdfdiff_img.IMG_EXT_MAP 保持一致的图片后缀（那份管「能不能折成 PDF」，
# 这里管「要不要提醒」，认同一批格式，避免「能拖进来却不提醒」）
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".jpe", ".bmp", ".tif", ".tiff",
             ".gif", ".webp")

# 一页有多少字符才算「有文字层」。
# 取 10 而不是 0：扫描件上常有页码/水印被留成 1~2 个字符的文字对象，
# 那几行撑不起逐字比对；10 能挡住这类假文字层。
MIN_TEXT_CHARS = 10

# 一页里位图要覆盖多少面积，才算「图片页」而不是「带了个 logo 的矢量页」。
# 扫描件/标签图实测 0.85~1.00；转曲页即使带小图标也远低于这个数。
IMG_COVER_MIN = 0.35
# 页内图文混排的位图覆盖下限（2026-10-26 用户：「大部分左半边是图片、右半边是原生
# 文字……它显示的是原生 PDF，实际上不是」）。
# 原来的分类是**按页一刀切**：一页只要有 ≥10 个字就算文字页，同一页里占了一半的
# 位图被完全忽略 —— 于是「左图右字」被报成「原生 PDF」。
# 这里单独认这一档：有文字层、但同时有一大块位图。
# 0.30 取在「约三分之一页」：普通文档插个 logo / 小配图（cover 0.01~0.2）不误判，
# 而「左半边整块图」（实测 cover 0.44）能抓住 —— 首版定 0.45 时漏了这类，因为
# 图片按原比例缩放后实际覆盖比框小（实测框占半页、真实 cover 只有 0.44）。
PAGE_MIX_COVER = 0.30

# 最多逐页扫多少页。超过就等间隔抽样 —— 体检只为「要不要提醒」，
# 不值得为 200 页文档花几秒钟。
MAX_PAGES = 60

# 「转曲占比」两个口径的提醒阈值（2026-10-21 用户拍板）。
# 只用「墨迹占比」或只用「行数占比」都会误伤：
#   · 11/12.pdf：转曲行 8.8% 但墨迹 24%（留了完整文字层，走逐字，不该拦）
#   · M4506：墨迹 62.8% 但行数占比 40.7%（文字层还剩大半，准确率尚可，不拦）
#   · 4048：墨迹 85% 且行数占比 83.9%（正文几乎全转曲、文字层只剩 9~15 行，该拦）
# 所以取「墨迹 ≥ 80% 或 行数占比 ≥ 60%」，两者命中其一才提醒。
CURVE_INK_MIN = 0.80        # 转曲墨迹面积 / 页面面积
CURVE_LINE_MIN = 0.60       # 转曲行数 / (转曲行 + 文字层行数)

KIND_TEXT = "text"          # 每页都有文字层 → 原生 / 转曲带隐形字 / 图文混排
KIND_OFFICE = "office"      # Word / Excel 输入（转 PDF 后自带文字层 → 不提醒）
KIND_MIXED = "mixed"        # 有文字页，也有一部分页没有文字层
KIND_SCAN = "scan"          # 没有文字页、以整页位图为主 → 扫描件 / 标签图（要提醒）
KIND_VECTOR = "vector"      # 没有文字页、以矢量线条为主 → 转曲（不提醒）
KIND_IMAGE = "image"        # 本身就是 PNG/JPG 等图片文件（要提醒）
KIND_EMPTY = "empty"        # 0 页 / 全是空白页
KIND_UNKNOWN = "unknown"    # 打不开 / 读不了（交给引擎报错，这里不拦）

KIND_CN = {
    KIND_TEXT: "文字层完整",
    KIND_OFFICE: "Word/Excel（转 PDF 后逐字比对）",
    KIND_MIXED: "部分页没有文字层",
    KIND_SCAN: "整页图像，没有文字层",
    KIND_VECTOR: "矢量图形，没有文字层",
    KIND_IMAGE: "图片文件",
    KIND_EMPTY: "空文档",
    KIND_UNKNOWN: "读不出结构",
}

# 单页 OCR 路线的实测耗时区间（秒）：纯转曲合成样本 102 秒、4048 抹掉文字层后
# 276 秒、3021 标签对 561 秒（单页约 280 秒）；取 60~300 是个诚实的粗区间。
SEC_PER_PAGE_LOW = 60
SEC_PER_PAGE_HIGH = 300

# ------------------------------------------------------------------
# 准入：只允许「同一种格式」互比（2026-10-29 用户要求）
# ------------------------------------------------------------------
# 用户原话：「只允许对比两个相同的格式，Word 比 Word 可以，Word 比 Excel 不行。
#   PDF 可以比 PDF，图片能比图片，跨格式是不允许的。」
#
# 分族只按**扩展名**分，不打开文件：两类东西走的是两条完全不同的管线，
# 跨族比对在物理上就没有意义（Word 转出来的版式跟 Excel 转出来的永远对不上，
# 报出来的「差异」全是版式差，没有一条可看）。
# ⚠️ 同族内部不拦：.doc 比 .docx、png 比 jpg 都放行 —— 它们转换后是同一条链路。
FAMILY_PDF = "pdf"
FAMILY_WORD = "word"
FAMILY_EXCEL = "excel"
FAMILY_IMAGE = "image"
FAMILY_UNKNOWN = ""             # 认不出的扩展名：不拦，交给引擎如实报错

FAMILY_CN = {
    FAMILY_PDF: "PDF",
    FAMILY_WORD: "Word",
    FAMILY_EXCEL: "Excel",
    FAMILY_IMAGE: "图片",
    FAMILY_UNKNOWN: "未知格式",
}


def family_of(path):
    """按扩展名分族 → "pdf" / "word" / "excel" / "image" / ""（认不出）。

    ⚠️ 与 `pdfdiff_office.OFFICE_KINDS`、本模块 `IMAGE_EXT` 同一份口径，
       不另立标准：加一种格式只改那两处，这里跟着走。
    """
    ext = os.path.splitext(str(path or ""))[1].lower()
    if not ext:
        return FAMILY_UNKNOWN
    if ext in IMAGE_EXT:
        return FAMILY_IMAGE
    if ext == ".pdf":
        return FAMILY_PDF
    try:
        import pdfdiff_office as _off
        k = _off.office_kind(path)
    except Exception:
        k = ""
    if k == "word":
        return FAMILY_WORD
    if k == "excel":
        return FAMILY_EXCEL
    return FAMILY_UNKNOWN


# ------------------------------------------------------------------
# 纯扫描件识别（2026-10-29 用户要求：扫描件不参与对比，直接禁比）
# ------------------------------------------------------------------
# 难点：**扫描件和「图片类 PDF」在结构上一模一样** —— 都是「没有文字层 + 整页一张
# 位图」。`get_images` / `get_drawings` 分不开，只能看像素。
#
# 物理差别在**纸面**：扫描件的白纸不是纯白，扫描噪声 + JPEG 压缩把它摊成一个
# 200~249 的连续带；软件导出的位图（标签图/曲线图）背景是**精确的 255**，
# 浅底纹是**精确的单一灰阶**（比如恰好 237）。所以主判据是「灰带占比」：
#
#   实测（100dpi 灰度，`[200,250)` 占比 / 亮像素相邻差）：
#     真实语料 13 页（原生/转曲/标签图/DWG/纯图片类/重压 JPEG）  band 0.01~0.11
#     合成扫描件（纸 238~252、噪声 σ3~8、200/300dpi、JPEG q80） band 0.17~0.88
#     设计出来的整页浅灰面板（矢量/导出位图）                    band 0.98 但 grain 0.06
#   中间是空的，band 阈值取 0.15。
#
# ⚠️ `grain` 不是用来分扫描/不扫描的（实测它区分不开：原生页的抗锯齿字边
#    也有 5~12），它只干一件事 —— 排除「**设计出来的平灰底**」：那种页面
#    整页灰得和扫描件一模一样（band 0.98），但相邻像素差只有 0.06，而扫描
#    纸面最低也有 2.3。阈值 1.0 落在这两个数中间。
# ⚠️ 三条都满足才判扫描件。这是**保守**取舍：宁可漏判（当普通图片类照常比），
#    也不能错判 —— 错判的后果是用户根本没法干活。已知会漏的：纸面几乎纯白、
#    噪声被压得很干净的扫描件（band 会低于 0.15），那种文件退回「图片类」照常比。
SCAN_BAND_LO = 200              # 「纸面灰带」下界
SCAN_BAND_HI = 250              # 上界（>=250 算纯白纸面）
SCAN_BAND_MIN = 0.15            # 灰带占比 ≥ 这个数 → 纸面不是纯白
SCAN_GRAIN_MIN = 1.0            # 亮像素相邻差 ≥ 这个数 → 不是「设计出来的平灰底」
SCAN_MEAN_MIN = 200.0           # 整页平均亮度 ≥ 这个数 → 纸面为主（整页照片挡在这里）
SCAN_COVER_MIN = 0.80           # 整页一张位图（结构前提，先用便宜的 get_image_rects 判）
SCAN_DPI = 100                  # 判扫描件的渲染分辨率：100dpi 够看噪声，单页约 25ms

_SQUASH = re.compile(r"\s+")


def _squash(s):
    return _SQUASH.sub("", s or "")


def _page_text_chars(pg):
    try:
        return len(_squash(pg.get_text("text")))
    except Exception:
        return 0


def _image_cover(pg):
    """位图覆盖面积 / 页面面积。"""
    try:
        pa = abs(pg.rect.width * pg.rect.height) or 1.0
        area = 0.0
        for im in pg.get_images(full=True):
            for r in pg.get_image_rects(im[0]):
                area += abs(r.width * r.height)
        return min(1.0, area / pa)
    except Exception:
        return 0.0


def _is_blank(pg, chars, cover):
    """空白页：没字、没位图、没矢量。这种页不该被算成「只能看图」。"""
    if chars >= MIN_TEXT_CHARS or cover >= 0.01:
        return False
    try:
        return not pg.get_drawings()
    except Exception:
        return False


def _glyph_rows(pg):
    """一页里的「文字样矢量簇」（转曲后的字形轮廓）。

    判据与引擎一致（`pdfdiff_embed`：有 fill、高 ∈[3,15]pt、path 项数 ≥50），
    再用行聚合（y 容差 8pt）把相邻字形拼成「行」。返回 (每行外接矩形列表,
    这些行的墨迹总面积)。

    ⚠️ 用 `get_cdrawings` 不用 `get_drawings`：结果路径数一致，但快 2~8 倍
    （4048 单页 0.94s → 0.34s），这是「1 秒内判出转曲比例」的关键。
    ⚠️ 引擎判据分不出「矢量图标」和「转曲文字」——图标也满足 fill + 高 3~15pt +
    items≥50。所以行数占比会偏高，必须和墨迹占比一起看，且阈值只认「占比很大」。
    """
    try:
        dws = pg.get_cdrawings()
    except Exception:
        return [], 0.0
    rects = []
    for d in dws:
        if not d.get("fill") or d.get("rect") is None:
            continue
        r = fitz.Rect(d["rect"])
        if VECTOR_TEXT_H_MIN <= r.height <= VECTOR_TEXT_H_MAX and \
                len(d.get("items") or []) >= VECTOR_TEXT_ITEMS_MIN:
            rects.append(r)
    if not rects:
        return [], 0.0
    rects.sort(key=lambda r: (r.y0 + r.y1) / 2.0)
    rows, cur_y = [], None
    for r in rects:
        cy = (r.y0 + r.y1) / 2.0
        if rows and abs(cy - cur_y) <= VECTOR_ROW_Y_TOL:
            rows[-1].append(r)
            cur_y = sum((x.y0 + x.y1) / 2.0 for x in rows[-1]) / len(rows[-1])
        else:
            rows.append([r])
            cur_y = cy
    out = []
    area = 0.0
    for grp in rows:
        u = fitz.Rect(grp[0])
        for g in grp[1:]:
            u |= g
        out.append(u)
        area += abs(u.width * u.height)
    return out, area


def _text_line_count(pg):
    """一页的文字层行数（只对有文字的页调用）。"""
    try:
        dd = pg.get_text("dict")
        return sum(len(b.get("lines", [])) for b in dd["blocks"])
    except Exception:
        return 0


def _curve_metrics(pg):
    """单页转曲占比：返回 (行数占比, 墨迹占比)。无文字层时行数占比按 1.0 算。"""
    glyph_rows, ink = _glyph_rows(pg)
    pa = abs(pg.rect.width * pg.rect.height) or 1.0
    ink_frac = min(1.0, ink / pa)
    if not glyph_rows:
        return 0.0, ink_frac
    chars = _page_text_chars(pg)
    if chars < MIN_TEXT_CHARS:
        # 这页没有文字层，却有一堆字形簇 → 行数占比按「全部转曲」算
        return 1.0, ink_frac
    tl = _text_line_count(pg)
    line_frac = len(glyph_rows) / max(1, len(glyph_rows) + tl)
    return line_frac, ink_frac


def _scan_metrics(pg):
    """渲染一页（100dpi 灰度）算扫描件指标 → dict；算不了返回 None。

    三个指标，缺一不可（实测数据见 `SCAN_BAND_MIN` 那段注释）：
      band  = [200,250) 像素占比  → 纸面不是纯白（扫描噪声把它摊开来）
      grain = 亮像素上的**相邻像素平均差** → 纸面有颗粒（设计出来的浅灰面板是平的）
      mean  = 整页平均亮度 → 纸面为主（整页照片挡在这里）
    """
    try:
        pm = pg.get_pixmap(dpi=SCAN_DPI, colorspace=fitz.csGRAY, annots=False)
        w, h, stride = int(pm.width), int(pm.height), int(pm.stride)
        if not w or not h:
            return None
        import numpy as np
        a = np.frombuffer(pm.samples, dtype=np.uint8).reshape(h, stride)[:, :w]
        n = float(a.size)
        ai = a.astype(np.int16)
        bright = ai >= 180
        # 只在「两边都亮」的像素对上取差：文字边缘、图线边缘不算纸面颗粒
        mx = bright[:, 1:] & bright[:, :-1]
        my = bright[1:, :] & bright[:-1, :]
        gx = float(np.abs(np.diff(ai, axis=1))[mx].mean()) if mx.sum() > 200 else 0.0
        gy = float(np.abs(np.diff(ai, axis=0))[my].mean()) if my.sum() > 200 else 0.0
        return {"band": float(((a >= SCAN_BAND_LO) & (a < SCAN_BAND_HI)).sum()) / n,
                "white": float((a >= SCAN_BAND_HI).sum()) / n,
                "grain": (gx + gy) / 2.0,
                "mean": float(a.mean())}
    except Exception:
        # 没 numpy / 渲染失败 → **不判扫描件**。宁可漏判（当普通图片类比），
        # 也不能错判把用户挡在门外。
        return None


def _page_is_scan(pg, cover=None):
    """这一页是不是**扫描件**（返回 (bool, 指标 dict)）。

    结构前提先过一遍（便宜）：整页一张位图。过了才渲染算像素指标。
    """
    if cover is None:
        cover = _image_cover(pg)
    if cover < SCAN_COVER_MIN:
        return False, {"band": 0.0, "white": 1.0, "grain": 0.0,
                       "mean": 255.0, "cover": cover}
    m = _scan_metrics(pg)
    if not m:
        return False, {"band": 0.0, "white": 1.0, "grain": 0.0,
                       "mean": 255.0, "cover": cover}
    m["cover"] = cover
    ok = (m["band"] >= SCAN_BAND_MIN
          and m["grain"] >= SCAN_GRAIN_MIN
          and m["mean"] >= SCAN_MEAN_MIN)
    return ok, m


def probe(path, max_pages=MAX_PAGES):
    """体检**单个文件**，返回全裸类型 dict（可 JSON 化）。

    键：path / kind / pages / pages_seen / text_pages / image_pages /
        vector_pages / chars / cover / sampled / is_image / note
        / scan_pages（被判成扫描件的页号）/ scan_page_count / pure_scan
    """
    out = {
        "path": path or "",
        "kind": KIND_UNKNOWN,
        "pages": 0,
        "pages_seen": 0,
        "text_pages": 0,
        "image_pages": 0,
        "vector_pages": 0,
        "mixed_pages": 0,        # 页内图文混排：有文字层，但同页还压着大块位图
        "chars": 0,
        "cover": 0.0,
        "sampled": False,
        "is_image": False,
        "curve_line": 0.0,       # 转曲行数占比（抽样页平均）
        "curve_ink": 0.0,        # 转曲墨迹占比（抽样页平均）
        "curve_heavy": False,    # 是否「转曲占比高到该提醒」
        "scan_pages": [],        # 被判成**扫描件**的页号（0 基）；空 = 一页都不是
        "scan_page_count": 0,
        "pure_scan": False,      # 整份都是扫描件 → 直接禁止对比
        "note": "",
    }
    if not path or not os.path.isfile(path):
        out["note"] = "文件不存在"
        return out

    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        # 图片文件：折成单页 PDF 后必然没有文字层，一步到位
        out.update(kind=KIND_IMAGE, pages=1, pages_seen=1, image_pages=1,
                   is_image=True, cover=1.0,
                   note="图片文件，折成单页 PDF 后走扫描件链路")
        return out

    # Word / Excel（2026-10-21 加）：转 PDF 后**自带完整文字层**，走原生逐字比对
    # （快且准），不属于「纯图片类」—— 不提醒。判据只看扩展名，不为体检去起 Office
    # （体检必须 1 秒内出结论）。真正转不出来时由转换层如实报错，不在这里拦。
    import pdfdiff_office as _off
    if _off.is_office_file(path):
        out.update(kind=KIND_OFFICE,
                   note="Word/Excel 文档，转成 PDF 后自带文字层、走逐字比对")
        return out

    try:
        doc = fitz.open(path)
    except Exception as e:                # 打不开：不在这一层拦，让引擎如实报错
        out["note"] = "打开失败：%s" % e
        return out
    try:
        n = doc.page_count
        out["pages"] = n
        if n == 0:
            out["kind"] = KIND_EMPTY
            out["note"] = "文档没有页面"
            return out

        # 页数多就等间隔抽样（首尾必取），只影响提示精度
        if n > max_pages:
            step = max(1, n // max_pages)
            idx = list(range(0, n, step))[:max_pages]
            if idx[-1] != n - 1:
                idx[-1] = n - 1
            out["sampled"] = True
        else:
            idx = list(range(n))

        tp = ip = vp = 0
        mp = 0                           # **页内**图文混排（有字页里还压着大块位图）
        chars = 0
        covers = []
        line_fracs = []
        ink_fracs = []
        scan_idx = []                    # 被判成**扫描件**的页号
        scan_bands = []                  # 只是为了自检/日志好看，正式判定不看它
        for i in idx:
            pg = doc[i]
            c = _page_text_chars(pg)
            chars += c
            cv = _image_cover(pg)
            covers.append(cv)
            if c >= MIN_TEXT_CHARS:
                tp += 1
                # 这一页有文字层，但页面里还有一大块位图（左图右字那种）
                if cv >= PAGE_MIX_COVER:
                    mp += 1
            elif _is_blank(pg, c, cv):
                pass                       # 空白页：三类都不算
            elif cv >= IMG_COVER_MIN:
                ip += 1                    # 整页位图 → 扫描件/标签图
                # 结构上跟「标签图」分不开，只能看像素：纸面有灰带就是扫描件。
                # 只在这一支里渲染，别的页一次 pixmap 都不取（见模块说明）。
                _is_scan, _m = _page_is_scan(pg, cv)
                if _is_scan:
                    scan_idx.append(i)
                    scan_bands.append(round(_m["band"], 3))
            else:
                vp += 1                    # 矢量线条 → 转曲/图形
            # 转曲占比：只对「有转曲嫌疑」的页才值得算，但 get_cdrawings 便宜，
            # 这里对每页都算，两个口径取页平均（与 curve_probe 的整份合计语义一致）
            lf, ifr = _curve_metrics(pg)
            line_fracs.append(lf)
            ink_fracs.append(ifr)
        seen = len(idx)
        out.update(pages_seen=seen, text_pages=tp, image_pages=ip,
                   vector_pages=vp, mixed_pages=mp, chars=chars,
                   cover=round(sum(covers) / seen, 3) if covers else 0.0,
                   scan_pages=scan_idx, scan_page_count=len(scan_idx))
        if scan_bands:
            out["scan_band"] = round(sum(scan_bands) / len(scan_bands), 3)
        if seen:
            out["curve_line"] = round(sum(line_fracs) / seen, 3)
            out["curve_ink"] = round(sum(ink_fracs) / seen, 3)
            out["curve_heavy"] = (out["curve_ink"] >= CURVE_INK_MIN
                                  or out["curve_line"] >= CURVE_LINE_MIN)

        # 抽样时按比例推算回整份（只用于提示与耗时估算）
        if out["sampled"] and seen:
            scale = n / float(seen)
            out["text_pages"] = int(round(tp * scale))
            out["image_pages"] = int(round(ip * scale))
            out["vector_pages"] = int(round(vp * scale))
            out["mixed_pages"] = int(round(mp * scale))

        if tp + ip + vp == 0:
            out["kind"] = KIND_EMPTY       # 全是空白页
        elif tp and (ip + vp + mp) == 0:
            out["kind"] = KIND_TEXT
        elif tp:
            # 有文字页，另外还有「整页图/转曲」或「同页里一大块图」→ 混合型
            out["kind"] = KIND_MIXED
        elif ip > vp:
            out["kind"] = KIND_SCAN
        else:
            out["kind"] = KIND_VECTOR

        # 「纯扫描件」＝ 一页文字都没有、也没有矢量页，**而且每个位图页都查出了
        # 扫描特征**（纸面灰带）。只要有一页不像扫描件（比如软件生成的标签图），
        # 就不算纯扫描件 —— 那种文件照常能比，别禁。
        # ⚠️ 这里用的是**抽样页**的原始计数（tp/ip/vp 局部变量没被上面的按比例
        #    推算覆盖，`out[...]` 才是推算后的）；抽样时按「抽样到的位图页全是扫描
        #    页」下结论，未抽到的页不做假设。
        out["pure_scan"] = bool(
            ip > 0 and vp == 0 and tp == 0
            and out["scan_page_count"] == ip)
        if out["pure_scan"]:
            out["note"] = ("整份 %d 页都是扫描图（纸面灰带 %.2f）：没有文字层，"
                           "扫描噪声会让逐像素对比全是假差异"
                           % (n, out.get("scan_band", 0.0)))
        return out

    except Exception as e:                # 读页失败：同样交给引擎
        out["note"] = "读取失败：%s" % e
        return out
    finally:
        try:
            doc.close()
        except Exception:
            pass


def warn_kinds(probe_result):
    """这一侧的值不值得「开跑前问一句」。

    2026-10-26 用户：「后面两种都需要有弹窗 —— 有大面积转曲或图片的 PDF、
    还有全部都是转曲/图片的 PDF」。所以判据是三类：
      · 整份都是图片/扫描件（image / scan）
      · 转曲占比高到接近全转曲（curve_heavy，含「每页有字但正文全转曲」）
      · **混合**（有的页有文字、有的页整页是图/转曲）—— 这类以前不问，
        但它正是「一半红蓝、一半紫」的那种，必须先说清紫框的含义
    ⚠️ 每页都有文字层（text）的一律不问：那种文件紫框根本不会出现，
       拦下来只是白点一次。
    """
    if probe_result["kind"] in (KIND_IMAGE, KIND_SCAN, KIND_MIXED):
        return True
    return bool(probe_result.get("curve_heavy"))


def pixel_mode(probe_result):
    """这一份要不要走「只定位、不认字」（紫框）那条路。

    就是 `warn_kinds` 的三类 —— 提醒与走法是**同一个判据**，不会出现
    「点了继续、结果却没紫框」或「没提醒、却满屏紫」这种自相矛盾。

    ⚠️ 每页都有文字层（text / office）→ False：那种文件照旧红/绿/蓝，
       紫一次都不出现（2026-10-26 用户：「一般的文件就用普通的就行了」）。
    """
    return warn_kinds(probe_result)


def _is_curve(s):
    """这一侧触发提醒的，是「高转曲占比」而不是纯图片类。"""
    return s["kind"] not in (KIND_IMAGE, KIND_SCAN) and bool(s.get("curve_heavy"))


def assess(old_path, new_path, max_pages=MAX_PAGES):
    """体检一对输入。

    返回：{level, severity, old, new, sides, slow_pages, seconds, reasons,
           title, lines, block, skip_pages}
      level    : "ok"（直接跑）| "warn"（问一句再跑）| "block"（**不许跑**）
      severity : 0 不提醒 / 3 纯图片类（要提醒）
      block    : 不许跑的原因（人话，逐条），level=="block" 时非空
      skip_pages: 两侧都**不参与比对**的页号（0 基）。拼接 PDF 里那些扫描页
                  —— 用户 2026-10-29：「第一页是扫描件后面几页正常的，那就直接
                  第一页默认不参加比对，哪怕它是图片或者转曲」。
    """
    sides = {"old": probe(old_path, max_pages), "new": probe(new_path, max_pages)}
    need = []
    slow_pages = 0
    for key, name in (("old", "旧版"), ("new", "新版")):
        s = sides[key]
        if warn_kinds(s):
            need.append((key, name, s))
            slow_pages += max(1, s["pages"]) if s["kind"] == KIND_IMAGE \
                else s["pages"]

    # ---- 准入（2026-10-29 用户要求的三条硬规则）----
    block = []
    # ① 只允许同族互比：Word↔Word、Excel↔Excel、PDF↔PDF、图片↔图片。
    fo, fn = family_of(old_path), family_of(new_path)
    if fo and fn and fo != fn:
        block.append("格式不同：%s 只能和 %s 比，不能和「%s」比"
                     % (FAMILY_CN.get(fo, fo), FAMILY_CN.get(fo, fo),
                        FAMILY_CN.get(fn, fn)))
    # ② 页数必须相同（用户此前已定，2026-10-29 重申）。
    #    ⚠️ 只在两侧都读出了页数时判：打不开/0 页的文件交给引擎如实报错，
    #       不要在这里报一句「页数不同」把人绕晕。
    po, pn = sides["old"]["pages"], sides["new"]["pages"]
    if po and pn and po != pn:
        block.append("页数不同：旧版 %d 页、新版 %d 页" % (po, pn))
    # ③ 纯扫描件不参与对比：扫描噪声会让逐像素对比全是假差异。
    for key, name in (("old", "旧版"), ("new", "新版")):
        s = sides[key]
        if s.get("pure_scan"):
            block.append("%s是纯扫描件（%d 页全是扫描图，没有文字层）"
                         % (name, s["pages"]))

    # 拼接 PDF：扫描页不参与比对。两侧并用同一个页号集合 —— 页数已经要求相同，
    # 同号页就是同号页，不会错位。
    skip_pages = sorted(set(sides["old"].get("scan_pages") or [])
                        | set(sides["new"].get("scan_pages") or []))

    level = "block" if block else ("warn" if need else "ok")
    reasons = []
    for _key, name, s in need:
        if s["kind"] == KIND_IMAGE:
            reasons.append("%s是图片文件（%s）" % (name, os.path.splitext(
                s["path"])[1].lower().lstrip(".").upper()))
        elif _is_curve(s):
            reasons.append("%s文字大多是转曲（墨迹 %.0f%%、转曲行 %.0f%%）"
                           % (name, s["curve_ink"] * 100, s["curve_line"] * 100))
        elif s.get("image_pages") or s.get("vector_pages"):
            _n_img = s.get("image_pages", 0) + s.get("vector_pages", 0)
            if _n_img >= s["pages"]:
                reasons.append("%s整份 %d 页都是图像、没有文字层" % (name, s["pages"]))
            else:
                reasons.append("%s有 %d 页是图像、没有文字层，其余 %d 页有文字"
                               % (name, _n_img, s["pages"] - _n_img))
        else:
            # 纯「页内图文混排」：页数都是文字页，只是每页还压着大块图
            reasons.append("%s有 %d 页一半是图片、一半是文字，图那半读不出字"
                           % (name, s.get("mixed_pages") or s["pages"]))
    # 标题：区分「纯图片/混合」和「高转曲」两种提醒原因，别把转曲说成扫描件
    # 2026-10-26 起标题一律点明「只能按图象定位」—— 这条链路的代价是**准确率**
    # （只给紫框、不判增删），不是「要等很久」，标题得跟正文口径一致。
    has_img = any(s["kind"] in (KIND_IMAGE, KIND_SCAN, KIND_MIXED)
                  for _k, _n, s in need)
    has_crv = any(_is_curve(s) for _k, _n, s in need)
    if need and not has_img and has_crv:
        title = ("这两份文字大多转成了轮廓（只能按图象定位）" if len(need) >= 2
                 else "其中一份文字大多转成了轮廓（只能按图象定位）")
    else:
        title = ("这两份都有读不出文字的部分（只能按图象定位）" if len(need) >= 2
                 else "其中一份有读不出文字的部分（只能按图象定位）") if need else ""
    return {
        "level": level,
        "severity": 3 if need else 0,
        "old": sides["old"],
        "new": sides["new"],
        "sides": sides,
        "slow_pages": slow_pages,
        "seconds": (slow_pages * SEC_PER_PAGE_LOW,
                    slow_pages * SEC_PER_PAGE_HIGH),
        "reasons": reasons,
        # 只有一侧是需要提醒的，标题不能写「这对文件」—— 另一侧可能完全正常，
        # 说成「两份都不行」会让人以为没救了，直接放弃对比。
        "warn_sides": len(need),
        # 要不要走「只定位、不认字」（紫框）：与提醒同源，见 pixel_mode()
        "pixel": any(pixel_mode(sides[k]) for k in ("old", "new")),
        "title": title,
        "lines": side_lines(sides),
        # ---- 准入结果（2026-10-29）----
        "block": block,            # level=="block" 时是「为什么不许比」的逐条说明
        "skip_pages": skip_pages,  # 两侧都不参与比对的页号（0 基）
    }


def side_lines(sides):
    """给弹窗用：每侧一行「旧版　文件名　·　几页　·　什么情况」。"""
    out = []
    for key, name in (("old", "旧版"), ("new", "新版")):
        s = sides[key]
        fn = os.path.basename(s["path"]) or "—"
        if len(fn) > 34:
            fn = fn[:32] + "…"
        if not s["pages"]:
            out.append("%s　%s　·　%s" % (name, fn, KIND_CN.get(s["kind"], "未知")))
            continue
        bits = ["%d 页" % s["pages"]]
        # 高转曲优先显示「文字多为转曲」，别再写「文字层完整」——
        # 4048 有 9~15 行地址的文字层，但正文全转曲，写「完整」会误导
        if _is_curve(s):
            bits.append("文字多为转曲（墨迹 %.0f%%）" % (s["curve_ink"] * 100))
        else:
            bits.append(KIND_CN.get(s["kind"], "未知"))
            # 只有「不是整页图、但图占了大半」才补一句覆盖率，
            # 否则会写成「整页图像，没有文字层　·　整页图像」（重复且啰嗦）
            if s["kind"] == KIND_SCAN and s["cover"] < 0.9:
                bits.append("约 %d%% 面积是图像" % int(round(s["cover"] * 100)))
        # 拼接 PDF：这一侧有扫描页 → 说清哪几页不比了（弹窗里就要看到，
        # 不能等跑完才发现少了页）
        sp = s.get("scan_pages") or []
        if sp and not s.get("pure_scan"):
            bits.append("第 %s 页是扫描件，不参与比对"
                        % "、".join(str(i + 1) for i in sp))
        out.append("%s　%s　·　%s" % (name, fn, "　·　".join(bits)))
    return out


def block_text(rep):
    """不许对比时的弹窗正文：(标题, [逐条原因], 该怎么改)。

    三条硬规则的说明都写在这里，界面只负责显示 —— 文案与判据放在同一个文件里，
    以后改判据不会忘改文案（这就是 6.0 那次「提示说不用等、其实要等」的教训）。
    """
    if not rep or rep.get("level") != "block":
        return "", [], ""
    reasons = list(rep.get("block") or [])

    def _cat(r):
        if r.startswith("格式不同"):
            return "格式"
        if r.startswith("页数不同"):
            return "页数"
        return "扫描件"

    cats = []
    for r in reasons:
        c = _cat(r)
        if c not in cats:
            cats.append(c)
    # 标题按第一条原因定调，别把「格式不同」写成「页数不同」
    if "格式" in cats:
        main = "格式"
        title = "这两种格式不能互相比对"
        tip = ("换成同一种格式再比：PDF 比 PDF、Word 比 Word、Excel 比 Excel、"
               "图片比图片。要拿 Word 和 Excel 比内容，请先各自导成 PDF 再比。")
    elif "页数" in cats:
        main = "页数"
        title = "两份文件的页数不一样"
        tip = ("页数不同说明其中一份可能少传了几页、或者混进了别的文件。"
               "先把两份补齐成同样的页数（或删掉多出来的页）再比。")
    else:
        main = "扫描件"
        title = "这是纯扫描件，不能参与对比"
        tip = ("扫描件没有文字层，纸上还有扫描噪声和轻微倾斜，逐像素比出来的"
               "「差异」几乎全是噪声，标了也没法用。请拿原始电子版（导出的 PDF / "
               "Word）来比；只有扫描件时，先用别的工具做 OCR 转成可搜索 PDF。")
    # ⚠️ 只说**别的类别**还有问题，不要重复「两侧都是扫描件」这种同一件事
    #    （第一版把 reasons[1:] 直接拼上去，两个扫描件就出现「另外还有：新版是
    #    纯扫描件」—— 同一件事说两遍，看着像有三条毛病）。
    extra = [c for c in cats if c != main]
    if extra:
        tip += "（另外还有：%s，一并处理后才能比。）" % "、".join(extra)
    return title, reasons, tip


def body_text(rep):
    """弹窗正文（纯文字，便于测试与复用）：(标题下第一段, 第二段)。

    ⚠️ 2026-10-26 口径改了（用户原话：「它不一定是花费很久，而是可能不准。
       所以它花费的可能时间被压缩了，但是准确率可能不高了」）：
       第一段说**紫框是什么**（只标位置、不判增删），
       第二段说**代价与收益**（秒级出结果 / 改了什么得自己看）。
       别再写「单页要 1~5 分钟」——那是 5.0 走 OCR 的老账，6.0 这条路不认字了。
    """
    crv = any(_is_curve(rep["sides"][k]) for k in ("old", "new"))
    sides_need = rep.get("warn_sides", 2)
    mixed = any(rep["sides"][k]["kind"] == KIND_MIXED for k in ("old", "new"))
    if crv and rep.get("warn_sides", 2) >= 2:
        head = ("这两份的文字大多转成了图形轮廓，认不出字。工具改成「两页相减」："
                "变了的像素直接框出来，不判断是删还是加。")
    elif crv:
        head = ("其中一份的文字大多转成了图形轮廓，认不出字。那一份改成「两页相减」："
                "变了的像素直接框出来，不判断是删还是加；另一份有文字层，照常逐字比。")
    elif mixed:
        head = ("有的页有文字、有的页是图或转曲。有文字的页照旧逐字比（红＝删、"
                "绿＝加、浅蓝＝挪位）；读不出字的那几页只能「两页相减」，"
                "变了的像素框成紫色，不判断是删还是加。")
    elif sides_need <= 1:
        head = ("其中一份没有可读的文字层（整页是图或是转曲），那一份只能「两页相减」："
                "变了的像素框成紫色，不判断是删还是加；另一份照常逐字比。")
    else:
        head = ("这两份都没有可读的文字层（整页是图或是转曲），只能「两页相减」："
                "变了的像素框成紫色，不判断是删还是加。")
    tail = ("快是真的快：原来要把图形认回文字，那一步整段省掉，通常几秒到几十秒。"
            "代价是结果只剩位置 —— 紫框里「有变化」是确定的，改了什么、"
            "是删还是加，得自己看。")
    return head, tail


def summary_line(rep):
    """状态栏一句话（确认继续之后用）。"""
    if rep["level"] != "warn":
        return ""
    return "已确认：读不出文字的部分只标紫框（只定位、不判增删），每条都要自己核"


# ------------------------------------------------------------------
# 「这一对文件属于哪一类」——界面工具条上那一格类型文案
# ------------------------------------------------------------------
# 2026-10-26 用户要求：「再右边就空了这么多位置嘛，给我分类一下……我方便也判断
# 一下」。分类口径就按他说的三分法，但混合型要写清「原生 + 什么」：
#   纯文字          → 原生 PDF
#   文字 + 图片/转曲 → 混合型（原生+图片）/（原生+转曲）/（原生+图片+转曲）
#   全是图/转曲      → 纯图片 / 纯转曲 / 纯矢量
_TYPE_BASE = {
    KIND_TEXT: "原生 PDF",
    KIND_OFFICE: "Word/Excel",
    KIND_IMAGE: "纯图片",
    KIND_EMPTY: "空文档",
    KIND_UNKNOWN: "读不出结构",
}

# 窗口窄时的说法（只留类别，能省一个词就省一个词）
_TYPE_SHORT = {
    KIND_TEXT: "原生",
    KIND_OFFICE: "Office",
    KIND_IMAGE: "纯图片",
    KIND_EMPTY: "空文档",
    KIND_UNKNOWN: "读不出",
}


def _one_type(s, short=False):
    """一侧文件的类型文案。`short=True` 只给类别，不带括号里的成分。"""
    k = s.get("kind", KIND_UNKNOWN)
    if short and k in _TYPE_SHORT:
        return _TYPE_SHORT[k]
    if k == KIND_MIXED:
        if short:
            return "混合型"
        # `mixed_pages` = 页内图文混排（左图右字那种）：它也算「+图片」，
        # 因为那半页确实读不出文字（2026-10-26 用户报的分类不准）
        ip = s.get("image_pages", 0) + s.get("mixed_pages", 0)
        vp = s.get("vector_pages", 0)
        if ip and vp:
            return "混合型（原生+图片+转曲）"
        if ip:
            return "混合型（原生+图片）"
        if vp:
            return "混合型（原生+转曲）"
        return "混合型"
    if k == KIND_SCAN:
        # 整份都是扫描件 → 直接点名，别再说「纯图片」（两者在界面上后果不同：
        # 扫描件是禁比，图片类只是提醒一句）
        if s.get("pure_scan"):
            return "扫描件（禁比）"
        # 没有文字层的位图页：夹了矢量页就说清「含转曲」，别一律写成纯图片
        if s.get("vector_pages"):
            return "纯图片" if short else "纯图片（含转曲）"
        return "纯图片"
    if k == KIND_VECTOR:
        return "纯转曲" if s.get("curve_heavy") else "纯矢量"
    if k in (KIND_TEXT, KIND_OFFICE) and s.get("curve_heavy"):
        # DOC-1004 那类：文字层还剩几行、正文几乎全转曲。写「原生 PDF」会误导
        # —— 这种文件的紫框照样会出现（curve_heavy 也在 pixel_mode 的判据里）
        return "原生（多转曲）" if short else "原生 PDF（正文多为转曲）"
    return _TYPE_BASE.get(k, "未知")


def type_cn(rep):
    """这一**对**文件是什么类型：返回 (完整文案, 缩短文案)。

    缩短文案给窗口很窄时用（900px 最小窗下不许切掉半个字），只丢括号里的成分
    与「旧/新」两个字 —— 界面上本来就是旧版在左、新版在右，顺序不会错。
    两侧同类型只写一遍；不同就并排写，如「原生 / 混合型」。
    """
    if not rep or not rep.get("sides"):
        return "", ""
    o = _one_type(rep["sides"]["old"])
    n = _one_type(rep["sides"]["new"])
    so = _one_type(rep["sides"]["old"], True)
    sn = _one_type(rep["sides"]["new"], True)
    if o == n:
        return o, so
    if so == sn:
        # 类别一样、成分不一样（一侧「混合型（原生+转曲）」另一侧「（原生+图片）」）：
        # 缩短版**不能**把成分丢了，否则就成了「混合型 / 混合型」，等于没说
        return "旧 %s / 新 %s" % (o, n), "%s / %s" % (o, n)
    return "旧 %s / 新 %s" % (o, n), "%s / %s" % (so, sn)


# ------------------------------------------------------------------
# 四类总览（用户 2026-10-29 定稿）
# ------------------------------------------------------------------
# 用户原话：「第一类就是纯原生的，第二类就是原生带转曲的，或者原生带图片的，
# 或者转曲带图片的这种混合型。第三类就是纯图片或者纯转曲的，而第四类就是纯
# 扫描件的……先把预设好的这个名字给放到空白处，然后如果是哪一类识别的时候，
# 就把那一类给高亮出来，证明现在是这一类。就是让人有个总体的观念，知道一共
# 有哪些类别，而这个是哪一类。」
#
# 所以界面上**四格始终全部显示**（这一对是哪一类一眼可见，剩下的也知道存在），
# 当前命中哪格就亮哪格。这与 `type_cn()` 的自由文案是两套：那个写细节
# （「混合型（原生+图片+转曲）」），这个只给归类，两者都从 `probe()` 的同一份
# 字段推出来，不会互相矛盾。
CLASS_PURE_TEXT = 1
CLASS_MIXED = 2
CLASS_PURE_IMAGE = 3
CLASS_SCAN = 4

CLASS_ORDER = (CLASS_PURE_TEXT, CLASS_MIXED, CLASS_PURE_IMAGE, CLASS_SCAN)

CLASS_CN = {
    CLASS_PURE_TEXT: "纯原生",
    CLASS_MIXED: "混合型",
    CLASS_PURE_IMAGE: "图片/转曲",
    CLASS_SCAN: "扫描件",
}
CLASS_MARK = {CLASS_PURE_TEXT: "①", CLASS_MIXED: "②",
              CLASS_PURE_IMAGE: "③", CLASS_SCAN: "④"}
# 悬浮说明（2026-10-29 第二轮）。用户原话：「混合型，那你是什么样的混合型呢？
# 是不是得描述清楚？它混合性有哪几种？然后这个图片也是，是纯图片还是纯转曲的，
# 是不是也应该描述清楚？因为它内部还分很多种类型的。」
# 一行胶囊放不下这许多字，所以：**胶囊只写这一对命中的那种**（见 `class_text`），
# 完整枚举放在悬停提示里 —— 没比对的时候也能读到「这一类底下还有哪几种」。
CLASS_TIP = {
    CLASS_PURE_TEXT: (
        "① 纯原生：每一页都有可提取的文字层（含 Word / Excel 转出来的 PDF）。\n"
        "比对方式：逐字比对 —— 红的删减、绿的新增、浅蓝是位置挪动。"),
    CLASS_MIXED: (
        "② 混合型：整份里「有字的页」和「读不出字的页 / 区域」混在一起，共三种——\n"
        "　· 原生+图片：有的页是字，有的页是整页位图（截图、照片）\n"
        "　· 原生+转曲：有的页正文被转成曲线轮廓，文字层只剩零星几行\n"
        "　· 原生+图片+转曲：上面两种都有\n"
        "比对方式：有字的部分照常红/绿/蓝，读不出字的部分只标紫框。"),
    CLASS_PURE_IMAGE: (
        "③ 图片 / 转曲：整份都读不出字，共三种——\n"
        "　· 纯图片：全是位图，如 PNG / JPG / 整页截图\n"
        "　· 纯转曲：字形全部转成曲线，没有文字层（DWG 出图常见）\n"
        "　· 图片+转曲：两种混在同一份里\n"
        "比对方式：只标紫框，不判是删还是加。"),
    CLASS_SCAN: (
        "④ 扫描件：纸扫进来的，带纸面噪点、轻微倾斜、JPEG 压缩痕迹 ——\n"
        "逐像素比对会满屏假差异，所以**不参与对比**。\n"
        "要格外小心：改完再扫一遍，两次扫描的差就是噪声，不能当改版依据。"),
}


def class_of(s):
    """一侧文件归到四类里的哪一类（0 = 还没判出来 / 读不出，界面全灰）。

    ⚠️ 与 `_one_type()` 的对应关系必须保持一致，否则会出现「胶囊亮②、
       旁边文案写①」这种自相矛盾：
         · scan + pure_scan        → ④扫描件（禁比）
         · scan（非纯扫描）/vector / image → ③图片/转曲
         · mixed，或 text 但正文多为转曲 → ②混合型
         · text / office 且没转曲   → ①纯原生
       注意 `kind == text` 时 `image_pages` 必为 0（有整页位图就判 mixed 了），
       所以「原生+图片」只可能落在 mixed 这一支，不用在这里再查一遍。
    """
    if not s:
        return 0
    k = s.get("kind")
    if k == KIND_SCAN:
        return CLASS_SCAN if s.get("pure_scan") else CLASS_PURE_IMAGE
    if k in (KIND_VECTOR, KIND_IMAGE):
        return CLASS_PURE_IMAGE
    if k == KIND_MIXED:
        return CLASS_MIXED
    if k in (KIND_TEXT, KIND_OFFICE):
        # DOC-1004 那类：文字层只剩几行、正文几乎全转曲 —— 用户明确归到第二类
        return CLASS_MIXED if s.get("curve_heavy") else CLASS_PURE_TEXT
    return 0


def class_of_pair(rep):
    """这一对命中哪几类（去重、按 ①②③④ 升序）。

    两侧同类只回一个；不同就回两个 —— 界面两格同时亮。不取「更差的那个」：
    旧版原生、新版混合型时，用户需要知道**两侧不一样**，只亮②会让人以为
    旧版也是混合型。
    """
    if not rep or not rep.get("sides"):
        return []
    got = []
    for key in ("old", "new"):
        c = class_of(rep["sides"][key])
        if c and c not in got:
            got.append(c)
    return sorted(got)


# ------------------------------------------------------------------
# 胶囊上「具体是这一大类里的哪一种」（2026-10-29 第二轮用户要求）
# ------------------------------------------------------------------
# 细分**不写成一串字符串再拼**，而是先归到「成分集合」再渲染。原因是
# 最常见的那种改版动作 —— 旧版把插图印在位图上、新版换成矢量转曲 ——
# 两侧同属②混合型但成分不同，拼字符串会得到「混合·原生+图片 / 混合·原生+转曲」
# 这种又长又容易读串的东西；取并集则写「混合·原生+图片+转曲」，长度只有一行，
# 语义也仍然是真的（这份文件里这三样都有，两侧合起来）。
FEAT_TEXT = "原生"
FEAT_IMG = "图片"
FEAT_VEC = "转曲"
_FEAT_ORDER = (FEAT_TEXT, FEAT_IMG, FEAT_VEC)


def chip_label(c):
    """胶囊的**预设**文案（还没判 / 没命中这一类时显示这句）。"""
    return "%s %s" % (CLASS_MARK.get(c, ""), CLASS_CN.get(c, ""))


def class_feats(s):
    """一侧文件「由什么成分组成」的集合。只给胶囊文案用，不参与任何判定。

    ⚠️ 判据与 `_one_type()` 必须同源（同一批字段：image_pages / mixed_pages /
       vector_pages / curve_heavy / kind），改一边就得改另一边，否则会出现
       「胶囊写混合·原生+转曲、体检弹窗写原生+图片」这种自相矛盾。
    """
    if not s:
        return frozenset()
    c = class_of(s)
    if c in (CLASS_SCAN, CLASS_PURE_TEXT):
        # ①④ 不细分：①就是「整份都有字」，④是「整份都不比」，没有子类型可讲
        return frozenset()
    if c == CLASS_PURE_IMAGE:
        k = s.get("kind")
        if k == KIND_IMAGE:
            return frozenset((FEAT_IMG,))          # 直接拖进来的 PNG/JPG
        if k == KIND_VECTOR:
            return frozenset((FEAT_VEC,))          # 纯矢量/纯转曲
        # kind == scan 但没到「纯扫描」（标签图那类）：位图为主，可能夹转曲页
        f = {FEAT_IMG}
        if s.get("vector_pages"):
            f.add(FEAT_VEC)
        return frozenset(f)
    # ② 混合型：一定含「原生」，再看有没有整页图 / 转曲
    f = {FEAT_TEXT}
    if s.get("image_pages", 0) + s.get("mixed_pages", 0):
        f.add(FEAT_IMG)
    if s.get("vector_pages") or s.get("curve_heavy"):
        f.add(FEAT_VEC)
    return frozenset(f)


def class_text(rep):
    """{类别号: 命中后胶囊该写的文案}，只含**命中**的类别。

    未命中的类别不在这里，界面用 `chip_label()` 的预设短名 —— 这样「一共有
    四类」和「这一对是其中哪一类里的哪一种」在同一排里同时成立。
    """
    if not rep or not rep.get("sides"):
        return {}
    feats = {}
    for key in ("old", "new"):
        s = rep["sides"][key]
        c = class_of(s)
        if not c:
            continue
        feats.setdefault(c, set()).update(class_feats(s))
    out = {}
    for c, f in feats.items():
        sub = "+".join(x for x in _FEAT_ORDER if x in f)
        # 「只有原生」不叫混合，别写成「② 混合型·原生」那种等于没说的话
        if c == CLASS_MIXED and sub and sub != FEAT_TEXT:
            out[c] = "%s %s·%s" % (CLASS_MARK[c], CLASS_CN[c], sub)
        elif c == CLASS_PURE_IMAGE and sub:
            # ③ 的预设名是「图片/转曲」，命中时换成本次实际的那种，
            # 单个成分叫「纯图片」「纯转曲」，两个都有就叫「图片+转曲」
            out[c] = "%s %s" % (CLASS_MARK[c],
                                ("纯" + sub) if "+" not in sub else sub)
        else:
            out[c] = chip_label(c)
    return out


def pixel_hint(rep):
    """点「开始对比」后状态栏那句长提示（说明这一轮为什么会出现紫框）。"""
    if not rep.get("pixel"):
        return ""
    crv = any(_is_curve(rep["sides"][k]) for k in ("old", "new"))
    return "转曲/图片区只标紫框（不认字）" if crv else "无文字区只标紫框（不认字）"


# ==================================================================
# 自检：不依赖真实语料，造「原生 / 纯图 / 纯矢量 / 混合 / 图片」五类
# ==================================================================
def _make_text_pdf(fp, pages=1):
    d = fitz.open()
    for _ in range(pages):
        p = d.new_page(width=300, height=200)
        p.insert_text((20, 40), "Warning: do not reuse the device.", fontsize=9)
        p.insert_text((20, 60), "使用期限过后请勿继续使用本器械。", fontsize=9,
                      fontname="china-s")
    d.save(fp)
    d.close()
    return fp


def _make_image_pdf(fp, png):
    """纯图像页：整页一张位图、无文字层（模拟 BarTender 标签/扫描件）。"""
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    p.insert_image(fitz.Rect(0, 0, 300, 200), filename=png)
    d.save(fp)
    d.close()
    return fp


def _make_vector_pdf(fp):
    """纯矢量页：无文字层、无位图，只有一堆小填充路径（模拟转曲后的字形轮廓）。"""
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    for i in range(60):
        x = 15 + (i % 12) * 22
        y = 30 + (i // 12) * 22
        p.draw_rect(fitz.Rect(x, y, x + 12, y + 12), color=(0, 0, 0),
                    fill=(0, 0, 0), width=0)
    d.save(fp)
    d.close()
    return fp


def _make_page_mix_pdf(fp, png):
    """**页内**图文混排：左半边整块位图、右半边原生文字。

    2026-10-26 用户：「大部分左半边是图片、右半边是原生文字……它显示的是原生 PDF，
    实际上不是」。旧分类按页一刀切（有 10 个字就算文字页），这类被误报成 text。
    """
    d = fitz.open()
    p = d.new_page(width=400, height=300)
    p.insert_image(fitz.Rect(20, 20, 195, 280), filename=png, keep_proportion=False)
    y = 40
    for i in range(6):
        p.insert_text((210, y), "Right side native text line %d" % i, fontsize=8)
        y += 20
    d.save(fp)
    d.close()
    return fp


def _make_curve_pdf(fp, rows=6):
    """真·转曲页：无文字层、无位图，只有一堆高复杂度 fill 矢量簇。

    每个「字形」画成一个 60 顶点的锯齿形填充 polyline（`get_cdrawings` 会
    报告 items≈60 ≥ VECTOR_TEXT_ITEMS_MIN），高度落在 [3,15]pt 内、有 fill，
    满足引擎的「转曲文字簇」判据 —— 但没有文字层。模拟 4048 那类正文全转曲。
    """
    import math
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    for r in range(rows):
        y = 26 + r * 22
        for k in range(12):
            x = 16 + k * 22
            pts = []
            for i in range(61):          # 61 个顶点 → items≈60
                fx = x + i * (12.0 / 60.0)
                fy = y - 5.0 + 3.0 * math.sin(i * math.pi / 4.0)
                pts.append((fx, fy))
            pts.append((x, y + 6.0))     # 闭合回底边
            p.draw_polyline(pts, color=(0, 0, 0), fill=(0, 0, 0), width=0)
    d.save(fp)
    d.close()
    return fp


def self_test():
    import tempfile

    ok = 0

    def chk(cond, msg, extra=""):
        nonlocal ok
        if cond:
            ok += 1
            print("  ✓ %s%s" % (msg, ("  " + str(extra)) if extra else ""))
        else:
            print("  ✗ %s%s" % (msg, ("  → " + str(extra)) if extra else ""))

    tmp = tempfile.mkdtemp(prefix="pdftest_pre_")
    png = os.path.join(tmp, "src.png")
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    p.draw_rect(fitz.Rect(0, 0, 300, 200), fill=(0.92, 0.92, 0.92), width=0)
    p.insert_text((20, 60), "LABEL", fontsize=24)
    p.get_pixmap(dpi=72).save(png)
    d.close()

    T1 = _make_text_pdf(os.path.join(tmp, "t1.pdf"))
    T3 = _make_text_pdf(os.path.join(tmp, "t3.pdf"), pages=3)
    I1 = _make_image_pdf(os.path.join(tmp, "i1.pdf"), png)
    V1 = _make_vector_pdf(os.path.join(tmp, "v1.pdf"))
    C1 = _make_curve_pdf(os.path.join(tmp, "c1.pdf"))   # 真转曲：无文字层、全是字形簇
    M = os.path.join(tmp, "m.pdf")                 # 一页文字 + 一页纯图
    PX = _make_page_mix_pdf(os.path.join(tmp, "px.pdf"), png)   # 同一页里左图右字
    d = fitz.open(T1)
    _make_image_pdf(os.path.join(tmp, "_i.pdf"), png)
    d.insert_pdf(fitz.open(os.path.join(tmp, "_i.pdf")))
    d.save(M)
    d.close()

    print("[1] 分档")
    chk(probe(T1)["kind"] == KIND_TEXT, "有文字层 → text")
    chk(probe(T3)["pages"] == 3 and probe(T3)["kind"] == KIND_TEXT, "3 页全文字 → text")
    chk(probe(I1)["kind"] == KIND_SCAN, "整页位图无文字 → scan")
    chk(probe(I1)["image_pages"] == 1, "图片页计数正确")
    chk(probe(V1)["kind"] == KIND_VECTOR, "纯矢量无文字 → vector（转曲）")
    chk(probe(V1)["vector_pages"] == 1 and probe(V1)["image_pages"] == 0,
        "矢量页与图片页分开计数")
    chk(probe(M)["kind"] == KIND_MIXED, "一页文字+一页纯图 → mixed")
    # 页内混排：同一页里左半图右半字。旧口径直接判成 text（用户实测正是这个）
    chk(probe(PX)["kind"] == KIND_MIXED and probe(PX)["mixed_pages"] == 1,
        "同页左图右字 → mixed（页内混排），不再误报成 text",
        (probe(PX)["kind"], probe(PX)["mixed_pages"], probe(PX)["cover"]))
    chk("混合型（原生+图片）" == _one_type(probe(PX)),
        "页内混排的类型文案写明「+图片」", _one_type(probe(PX)))
    chk(warn_kinds(probe(PX)) and pixel_mode(probe(PX)),
        "页内混排也要走紫框那条路（那半页确实读不出字）")
    chk(probe(png)["kind"] == KIND_IMAGE and probe(png)["is_image"],
        "PNG → image")
    chk(probe("/不存在.pdf")["kind"] == KIND_UNKNOWN, "不存在 → unknown")

    print("\n[2] 谁该提醒（纯图片类 + 高转曲占比）")
    chk(assess(T1, T3)["level"] == "ok", "原生一对 → 不提醒")
    chk(assess(I1, I1)["level"] == "warn", "纯扫描件 → 提醒")
    chk(assess(I1, I1)["severity"] == 3, "severity=3")
    chk(assess(T1, I1)["level"] == "warn", "一原一扫描 → 提醒")
    chk(assess(png, T1)["level"] == "warn", "PNG 直接投 → 提醒")
    chk(assess(V1, V1)["level"] == "ok", "纯矢量（小图标、非转曲字形）→ **不**提醒")
    chk(assess(T1, V1)["level"] == "ok", "原生 vs 转曲 → 不提醒")
    # 2026-10-26 起「混合」也要提醒：它正是「一半红蓝、一半紫」的那种，
    # 必须先说清紫框的含义（用户：「后面两种都需要有弹窗」）。
    chk(assess(T1, M)["level"] == "warn", "原生 vs 混合 → 提醒（一半文字一半图）")
    chk(assess(V1, I1)["level"] == "warn", "转曲 vs 扫描 → 提醒（有一侧是扫描件）")
    chk(assess(I1, I1)["title"] == "这两份都有读不出文字的部分（只能按图象定位）",
        "双侧都是图片 → 标题写「这两份」", assess(I1, I1)["title"])
    chk(assess(T1, I1)["title"] == "其中一份有读不出文字的部分（只能按图象定位）",
        "只有一侧是图片 → 标题写「其中一份」", assess(T1, I1)["title"])
    chk("其中一份" in body_text(assess(T1, I1))[0]
        and "另一份" in body_text(assess(T1, I1))[0],
        "单侧时正文点明另一份照常逐字比对")
    chk(assess(T1, M)["pixel"] is True, "混合 → pixel=True（那半边会出紫框）",
        assess(T1, M)["pixel"])
    chk(assess(T1, T3)["pixel"] is False, "原生一对 → pixel=False（紫框不出现）")

    # ---- 高转曲占比提醒（2026-10-21 用户补充：转曲 >80~90% 也拦）----
    cr = probe(C1)
    chk(cr["kind"] == KIND_VECTOR and cr["vector_pages"] == 1,
        "真转曲页 → vector", cr["kind"])
    chk(cr["curve_heavy"], "真转曲页曲线占比高 → curve_heavy",
        (cr["curve_line"], cr["curve_ink"]))
    chk(assess(C1, C1)["level"] == "warn", "高转曲一对 → 提醒")
    chk(assess(T1, C1)["level"] == "warn", "原生 vs 高转曲 → 提醒")
    chk(assess(C1, C1)["title"] == "这两份文字大多转成了轮廓（只能按图象定位）",
        "双侧转曲 → 标题写「文字大多转成了轮廓」", assess(C1, C1)["title"])
    chk(assess(T1, C1)["title"] == "其中一份文字大多转成了轮廓（只能按图象定位）",
        "单侧转曲 → 标题写「其中一份」", assess(T1, C1)["title"])
    chk("转曲" in assess(C1, C1)["reasons"][0],
        "reason 里写「转曲」不是「图像」", assess(C1, C1)["reasons"])

    print("\n[3] 文案与数据")
    r = assess(I1, I1)
    chk(len(r["lines"]) == 2, "每侧一行说明", len(r["lines"]))
    chk("整页图像" in r["lines"][0], "写明整页图像", r["lines"][0])
    chk(r["slow_pages"] == 2, "两侧各 1 页慢页 → 2", r["slow_pages"])
    lo, hi = r["seconds"]
    chk(0 < lo < hi, "耗时是区间", (lo, hi))
    head, tail = body_text(r)
    chk("紫色" in head and "不判断是删还是加" in head, "正文说清紫框只标位置", head)
    chk("几秒" in tail and "分钟" not in tail,
        "正文说清「快」，不再写要等几分钟", tail)
    chk("紫框" in summary_line(r), "状态栏一句话可用", summary_line(r))
    chk(assess(T1, T3)["title"] == "" and summary_line(assess(T1, T3)) == "",
        "不提醒时没有标题/状态文案")

    print("\n[4] 类型分类文案（工具条上「这一对是什么类型」那一格）")
    chk(type_cn(assess(T1, T3))[0] == "原生 PDF", "文字一对 → 原生 PDF",
        type_cn(assess(T1, T3)))
    chk(type_cn(assess(I1, I1))[0] == "纯图片", "扫描件一对 → 纯图片",
        type_cn(assess(I1, I1)))
    chk(type_cn(assess(png, png))[0] == "纯图片", "PNG 一对 → 纯图片",
        type_cn(assess(png, png)))
    chk(type_cn(assess(C1, C1))[0] == "纯转曲", "真转曲一对 → 纯转曲",
        type_cn(assess(C1, C1)))
    chk(type_cn(assess(V1, V1))[0] == "纯矢量", "小图标矢量 → 纯矢量（没到转曲占比）",
        type_cn(assess(V1, V1)))
    chk(type_cn(assess(T1, M))[0] == "旧 原生 PDF / 新 混合型（原生+图片）",
        "一原一混合 → 并排写「旧 … / 新 …」，括号里点名是图片",
        type_cn(assess(T1, M)))
    chk(type_cn(assess(T1, C1))[0] == "旧 原生 PDF / 新 纯转曲",
        "一原一转曲 → 并排", type_cn(assess(T1, C1)))
    chk(type_cn(assess(V1, I1))[0] == "旧 纯矢量 / 新 纯图片", "矢量 vs 图 → 并排",
        type_cn(assess(V1, I1)))
    _f, _s = type_cn(assess(T1, M))
    chk(_s == "原生 / 混合型" and len(_s) < len(_f),
        "缩短文案只丢括号、PDF 字样与旧/新，类别留住", (_f, _s))
    chk(type_cn(None) == ("", "") and type_cn({}) == ("", ""),
        "没有体检结果 → 空文案（界面留空，不瞎猜）")

    print("\n[6] 四类胶囊：归类 +「这一大类里的哪一种」")
    chk(class_of({"kind": KIND_TEXT}) == CLASS_PURE_TEXT, "文字层 → ①")
    chk(class_of({"kind": KIND_OFFICE}) == CLASS_PURE_TEXT, "Word/Excel → ①")
    chk(class_of({"kind": KIND_MIXED}) == CLASS_MIXED, "mixed → ②")
    chk(class_of({"kind": KIND_TEXT, "curve_heavy": True}) == CLASS_MIXED,
        "文字层只剩几行、正文多为转曲 → ②")
    chk(class_of({"kind": KIND_VECTOR}) == CLASS_PURE_IMAGE, "纯转曲 → ③")
    chk(class_of({"kind": KIND_IMAGE}) == CLASS_PURE_IMAGE, "PNG → ③")
    chk(class_of({"kind": KIND_SCAN, "pure_scan": True}) == CLASS_SCAN, "纯扫描 → ④")
    chk(class_of({"kind": KIND_SCAN, "pure_scan": False}) == CLASS_PURE_IMAGE,
        "标签图（没到纯扫描）→ ③")
    chk(class_of({}) == 0 and class_of(None) == 0, "读不出 → 0（界面全灰）")

    _mi = {"kind": KIND_MIXED, "image_pages": 2}
    _mv = {"kind": KIND_MIXED, "vector_pages": 2}
    _t = class_text({"sides": {"old": _mi, "new": _mi}})
    chk(_t.get(CLASS_MIXED) == "② 混合型·原生+图片",
        "混合型写清是「原生+图片」", _t)
    chk(class_text({"sides": {"old": _mv, "new": _mv}}).get(CLASS_MIXED)
        == "② 混合型·原生+转曲", "混合型写清是「原生+转曲」")
    _both = class_text({"sides": {"old": _mi, "new": _mv}}).get(CLASS_MIXED)
    chk(_both == "② 混合型·原生+图片+转曲",
        "两侧成分不同 → 取并集（不写两遍「混合型」）", _both)
    chk(class_text({"sides": {"old": {"kind": KIND_VECTOR},
                              "new": {"kind": KIND_VECTOR}}}).get(CLASS_PURE_IMAGE)
        == "③ 纯转曲", "图片类写清是「纯转曲」")
    chk(class_text({"sides": {"old": probe(png), "new": probe(png)}}).get(CLASS_PURE_IMAGE)
        == "③ 纯图片", "图片类写清是「纯图片」")
    chk(chip_label(CLASS_MIXED) == "② 混合型", "没命中时写预设短名",
        chip_label(CLASS_MIXED))
    chk(class_text(None) == {} and class_text({}) == {}, "没有体检结果 → 空")
    chk(class_of_pair({"sides": {"old": {"kind": KIND_TEXT},
                                 "new": {"kind": KIND_MIXED}}}) == [1, 2],
        "两侧不同类 → 两格都亮")

    print("\n[5] 只读、幂等、不上判定链")
    a1, a2 = probe(I1), probe(I1)
    chk(a1 == a2, "同一文件两次结果一致")
    _b = (os.path.getsize(I1), os.path.getmtime(I1))
    probe(I1)
    chk(_b == (os.path.getsize(I1), os.path.getmtime(I1)), "体检不改文件")
    # ⚠️ 判据只能看**真的 import**。原来的写法是「源码里出现 pdfdiff_preflight
    #    这个字符串就算引用」，于是 2026-10-29 在注释里写明「页号由
    #    `pdfdiff_preflight.assess()["skip_pages"]` 给出」就把它判红了 ——
    #    注释里提模块名是正常的文档行为，要防的是运行期的依赖方向反了
    #    （判定链反过来依赖体检 → 体检一改就可能影响比对结果）。
    #
    # ⚠️ 2026-11-02 目录重构：判定链现在分在**两个目录**（本文件在 app/judge/，
    #    引擎在 app/engine/）。再用「只看自己所在目录」的写法，三个文件全都
    #    `continue`，这条断言会**静默变成永远通过** —— 比判红糟得多。所以改成
    #    跨层查找，且「找不到文件」直接判红，不许沉默。
    _here = os.path.dirname(os.path.abspath(__file__))
    _app = os.path.dirname(_here)
    _dirs = (_here, os.path.join(_app, "engine"))

    def _locate_src(fn):
        for _d in _dirs:
            _p = os.path.join(_d, fn)
            if os.path.isfile(_p):
                return _p
        return None

    bad = []
    for fn in ("pdfdiff_locate.py", "pdf_annotate.py", "pdfdiff_embed.py"):
        p = _locate_src(fn)
        if p is None:
            bad.append(fn + "（找不到，这条查不了）")
            continue
        for line in open(p, encoding="utf-8"):
            s = line.strip()
            if s.startswith("#"):
                continue
            if re.match(r"(import|from)\s+pdfdiff_preflight\b", s):
                bad.append(fn)
                break
    chk(not bad, "判定链没引用体检模块（只看真 import，不算注释）", bad or "")
    # 只看**生产代码**（跳过模块 docstring；self_test 里造样本要 get_pixmap，
    # 那是测试夹具，也不算）
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = src.split('"""', 2)[2].split("def _make_text_pdf", 1)[0]
    # `_scan_metrics` 里那一次 `get_pixmap` 是**判扫描件必须**的（算纸面噪声
    # 指标），除此之外体检一次都不许渲染；OCR 全程不许碰（那会把体检变成
    # 几十秒的重活，等于把「开跑前问一句」变成新的等待）。
    chk(prod.count("get_pixmap") == 1 and "_ocr" not in prod,
        "体检只有判扫描件那一处渲染（get_pixmap×1），全程不 OCR",
        prod.count("get_pixmap"))

    print("\n  %d 项通过" % ok)
    # 下限比当前项数低一些（留出删改余量），但不能低到「删掉半套断言也绿」。
    return 0 if ok >= 45 else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
