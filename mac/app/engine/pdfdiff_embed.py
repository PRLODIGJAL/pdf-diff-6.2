# -*- coding: utf-8 -*-
"""内嵌图片区域专用路线（指令 E，2026-10-16）。

背景：混合型 PDF 里的内嵌位图（BarTender 标签图等）是干净的合成图，不是纸张扫描。
对它们走"整块像素加 OCR"在 DWG rev16 vs rev18 上超时且结果不可用。本模块按五层实现：

  L1 精确同一性：解码像素 md5 相同 → 直接判无变化，零成本跳过。
  L2 方向与坐标归一化：按变换矩阵还原页面所见方向，记录页面 bbox 与像素密度。
  L3 配准（含缩放）：缩放由原生尺寸比精确推导（合成图全局一致，无需 ORB 猜），
     平移用 ORB+RANSAC 精修；缩放≈1 不重采样；内点<50% 判整图被替换。
  L4 字形级比对（核心）：二值化 → 连通域 → 行分组 → 行内 DP 对齐（汉明距离阈值）
     → 不匹配字形精确标字框。条码/二维码区域先检测跳过，二维码用 QRCodeDetector 解码比对。
  L5 OCR 回退：仅在字形层判不出的差异块上调用，用于生成文字说明。

把握档：哈希命中=高；字形比对=中；回退/待复核=低（低档不改红绿蓝三色框）。
依赖：numpy, opencv-python, pymupdf(fitz), Pillow。
"""

from __future__ import annotations

import hashlib
import io
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import cv2
import fitz
import numpy as np
from PIL import Image

# ---------------------------------------------------------------- 开关与阈值
EMBED_ROUTE_ON = True          # 总开关（指令 E 五层路线）
EMBED_L1_HASH_ON = True        # L1 精确同一性
EMBED_L2_NORMALIZE_ON = True   # L2 方向归一化（关=直接用原生像素，不推荐）
EMBED_L3_REGISTER_ON = True    # L3 配准
EMBED_L4_GLYPH_ON = True       # L4 字形级比对
EMBED_L5_OCR_FALLBACK = False  # L5 OCR 回退（默认关：OCR 由上层统一调度）


def _loc_checkpoint() -> None:
    """「停止对比」中止检查点（2026-10-20）：直接借用 `pdfdiff_locate` 的机制。

    本模块是纯图片型 PDF 的主路线（S1~S9），一份整页位图跑下来可能几十秒，
    用户点「停止对比」也得停得住。标志只有一份（在 pdfdiff_locate 里），
    这里不复制状态，只做一次函数级 import（模块级 import 会引入循环依赖风险）。

    ⚠️ 抛出的 `Cancelled` 继承 `BaseException`，**不会被本模块的
       `except Exception` 兜底吞掉** —— 这是「不静默改结果」的前提。
    """
    import pdfdiff_locate as _loc
    _loc.checkpoint()

EMBED_SCALE_SAME = 0.005       # 缩放比偏差 <0.5% 视为 1，不重采样
EMBED_INLIER_MIN = 0.50        # ORB 内点比例下限，低于它判整图被替换
EMBED_MIN_AREA = 12            # 字形连通域最小面积(px)
EMBED_MAX_AREA = 20000         # 字形连通域最大面积(px)（超过=图形/表格线）
EMBED_GLYPH_H_MAX = 300        # 字形最大高度(px)
EMBED_ROW_Y_TOL_RATIO = 0.6    # 行分组 y 容差 = 字形高 × 该系数
EMBED_HAMMING_MAX = 0.30       # 字形指纹汉明距离阈值（DP 匹配门槛）
# 同位字形对 XOR 判据（指令F 四-2 重定，原 320 仅一对样本且未按面积分段）：
#   · EMBED_GLYPH_AREA_MIN/Max 界定「正常文字字形」的组件面积（px）——
#     极小组件（<100）是重采样边缘噪声、大组件（>500）是表格线/条码/粘连块，
#     两者 XOR 值都不可靠，只有落在 [100,500] 的字形对才是可信文字比对。
#   · EMBED_XOR_ABS_MIN 是**该面积带内**的 XOR 绝对像素门槛。实测 DWG rev16/17/18
#     911 个字形对：无变化中等组件 XOR p99=171，真变化（Rev16→Rev18 单字、
#     地址块、尺寸标注）xor=150~371。取 150 分离带清晰（无变化 p99=171 之上）。
EMBED_GLYPH_AREA_MIN = 100     # 参与 XOR 判据的字形最小面积（px）
EMBED_GLYPH_AREA_MAX = 500     # 参与 XOR 判据的字形最大面积（px）
EMBED_XOR_ABS_MIN = 150        # 面积∈[100,500] 内，XOR≥此值判「同位字形变化」
EMBED_GRID_H = 24              # 字形指纹归一化网格高
EMBED_COARSE_GRID_H = 12       # 粗网格高（缩放鲁棒二次确认用，指令F 四-1）
EMBED_COARSE_SLACK = 0.15      # 细网格汉明在 (0.30, 0.30+此值] 时触发粗网格重算
EMBED_QR_SIDE_MIN = 60         # 二维码最小边长(px)
EMBED_TIME_BUDGET_S = 60.0     # 整份文档图片路线时间预算（秒）


@dataclass
class EmbedImage:
    """一页里的一张内嵌位图（已归一化）。"""
    page: int
    xref: int
    pix_w: int
    pix_h: int
    bbox: Tuple[float, float, float, float]      # 页面 pt 坐标 (x0,y0,x1,y1)
    flipped: bool                                 # 存储是否上下翻转（d<0）
    density: Tuple[float, float]                  # px/pt
    stream_md5: str
    gray: Optional[np.ndarray] = None             # 页面所见方向的灰度图（lazy）
    px2pt: Any = None                             # (px,py)->(pt_x,pt_y) 的系数 (sx,sy,ox,oy)

    def to_pt(self, px: float, py: float) -> Tuple[float, float]:
        sx, sy, ox, oy = self.px2pt
        return (ox + px * sx, oy + py * sy)


@dataclass
class EmbedDiff:
    """图片路线输出的一条差异。"""
    page: int
    rect: Tuple[float, float, float, float]       # 页面 pt
    kind: str                                     # "removed"/"added"/"changed"/"replaced"/"qr"/"review"
    conf: str                                     # "high"/"mid"/"low"
    old_text: str = ""
    new_text: str = ""
    note: str = ""                                # 报告说明（如"哈希相同，已跳过"）


@dataclass
class EmbedReport:
    """整份文档的图片路线报告（含每层统计）。"""
    items: List[EmbedDiff] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0


# ---------------------------------------------------------------- L1/L2：枚举与归一化
def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _page_images(doc: fitz.Document, page_idx: int) -> List[EmbedImage]:
    """L2：枚举一页的内嵌位图并归一化（方向/坐标/密度），不做像素解码。"""
    page = doc[page_idx]
    out: List[EmbedImage] = []
    for im in page.get_images(full=True):
        xref = im[0]
        try:
            infos = page.get_image_info(xrefs=[xref])
            if not infos:
                continue
            gi = infos[0]
            info = doc.extract_image(xref)
        except Exception:
            continue
        a, b, c, d, e, f = gi["transform"]
        flipped = (a * d - b * c) < 0          # 行列式<0 → 存储上下翻转
        bbox = tuple(gi["bbox"])                # type: ignore[assignment]
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        w, h = info["width"], info["height"]
        out.append(EmbedImage(
            page=page_idx, xref=xref, pix_w=w, pix_h=h, bbox=bbox,   # type: ignore[arg-type]
            flipped=flipped,
            density=(abs(a) / max(w, 1) * w / max(bw, 1e-6) if False else abs(a), abs(d)),
            stream_md5=_md5(info["image"]),
            # flipud 还原后 py=0 对应页面 bbox 顶部 y0
            px2pt=(bw / max(w, 1), bh / max(h, 1), bbox[0], bbox[1]),
        ))
    return out


def _decode_gray(doc: fitz.Document, img: EmbedImage) -> np.ndarray:
    """解码为页面所见方向的灰度图（上下翻转还原）。"""
    info = doc.extract_image(img.xref)
    pil = Image.open(io.BytesIO(info["image"])).convert("L")
    arr = np.asarray(pil)
    if img.flipped:
        arr = np.flipud(arr)
    return arr


# ---------------------------------------------------------------- L3：配准
def _pair_images(old_imgs: List[EmbedImage], new_imgs: List[EmbedImage],
                 ) -> List[Tuple[EmbedImage, Optional[EmbedImage]]]:
    """按 bbox 交叠把新旧图片配对；对不上的返回 None（=新增或删除）。"""
    used = set()
    pairs = []
    for oi in old_imgs:
        best, best_ov = None, 0.0
        for j, ni in enumerate(new_imgs):
            if j in used:
                continue
            x0 = max(oi.bbox[0], ni.bbox[0]); y0 = max(oi.bbox[1], ni.bbox[1])
            x1 = min(oi.bbox[2], ni.bbox[2]); y1 = min(oi.bbox[3], ni.bbox[3])
            if x1 <= x0 or y1 <= y0:
                continue
            inter = (x1 - x0) * (y1 - y0)
            ov = inter / min((oi.bbox[2]-oi.bbox[0])*(oi.bbox[3]-oi.bbox[1]),
                             (ni.bbox[2]-ni.bbox[0])*(ni.bbox[3]-ni.bbox[1]))
            if ov > best_ov:
                best_ov, best = ov, j
        if best is not None and best_ov > 0.3:
            pairs.append((oi, new_imgs[best]))
            used.add(best)
        else:
            pairs.append((oi, None))
    for j, ni in enumerate(new_imgs):
        if j not in used:
            pairs.append((None, ni))
    return pairs


def _register(ga: np.ndarray, gb: np.ndarray, scale_hint: float
              ) -> Tuple[str, float, float, float, int]:
    """配准：返回 (状态, 缩放, tx, ty, 内点比例*1000)。
    状态: "same"(缩放≈1) / "resampled"(需重采样) / "replaced"(内点不足)。"""
    if ga.shape == gb.shape:
        s = 1.0
    else:
        s = scale_hint
    # ORB 估平移（缩放已知，只需平移与验证）
    maxdim = 1600
    def cv_(img):
        h, w = img.shape
        if max(h, w) > maxdim:
            r = maxdim / max(h, w)
            return cv2.resize(img, (int(w*r), int(h*r)), interpolation=cv2.INTER_AREA)
        return img
    try:
        orb = cv2.ORB_create(nfeatures=8000)
        ka, da = orb.detectAndCompute(cv_(ga), None)
        kb, db = orb.detectAndCompute(cv_(gb), None)
        if da is None or db is None or len(ka) < 30 or len(kb) < 30:
            return ("resampled", s, 0.0, 0.0, 0)
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(da, db)
        src = np.float32([ka[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
        dst = np.float32([kb[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
        M, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                              ransacReprojThreshold=3.0)
        if M is None or mask is None:
            return ("replaced", s, 0.0, 0.0, 0)
        inlier = float(mask.sum()) / len(matches)
        if inlier < EMBED_INLIER_MIN:
            return ("replaced", s, 0.0, 0.0, int(inlier * 1000))
        return ("same" if abs(s - 1.0) < EMBED_SCALE_SAME else "resampled",
                s, float(M[0, 2]), float(M[1, 2]), int(inlier * 1000))
    except cv2.error:
        return ("resampled", s, 0.0, 0.0, 0)


def _resample_to(gb: np.ndarray, target_hw: Tuple[int, int]) -> np.ndarray:
    """对称重采样：把 new 侧缩到参考网格（Lanczos）。"""
    return cv2.resize(gb, (target_hw[1], target_hw[0]), interpolation=cv2.INTER_LANCZOS4)


# ---------------------------------------------------------------- L4：字形级比对
def _binarize(img: np.ndarray) -> np.ndarray:
    _, bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return bw


def _glyphs(bw: np.ndarray) -> List[Dict[str, Any]]:
    """连通域 → 字形候选（排除超大图形/表格线/细长条碎框）。"""
    inv = (bw < 128).astype(np.uint8)
    n, _, stats, cents = cv2.connectedComponentsWithStats(inv, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        cx, cy = cents[i]
        if (EMBED_MIN_AREA <= area <= EMBED_MAX_AREA and 3 <= h <= EMBED_GLYPH_H_MAX
                and 3 <= w <= EMBED_GLYPH_H_MAX):
            # 细长条碎框过滤（指令F 四-1）：表格线被二值化后断成「宽 3~5px、高
            # 95px+、h/w 达 19~32」的竖条碎片，实测 DWG rev16 有 259 个、rev18 有
            # 169 个，是 removed/added 误报的主要来源。它们宽高比极端（h/w>8 或
            # w/h>8），而真实文字字符 h/w ≤ 5（竖排字也就 2~5）。过滤后既不再进入
            # DP 干扰对齐，也不会被 `_emit` 当独立字形报 removed/added。
            if h > w * 8 or w > h * 8:
                continue
            out.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h),
                        "area": int(area), "cx": float(cx), "cy": float(cy)})
    return out


def _rows(glyphs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """按基线聚行。"""
    glyphs = sorted(glyphs, key=lambda g: g["cy"])
    rows: List[List[Dict[str, Any]]] = []
    cur_cy = 0.0
    for g in glyphs:
        tol = max(8.0, g["h"] * EMBED_ROW_Y_TOL_RATIO)
        if rows and abs(g["cy"] - cur_cy) <= tol:
            rows[-1].append(g)
            cur_cy = float(np.mean([x["cy"] for x in rows[-1]]))
        else:
            rows.append([g])
            cur_cy = g["cy"]
    return rows


def _fp(bw: np.ndarray, g: Dict[str, Any], grid_h: int = EMBED_GRID_H) -> np.ndarray:
    """字形指纹：归一化 grid_h 高的量化网格。

    grid_h 越小越「粗」——粗网格对 scale 微变的重采样边缘抖动不敏感，
    用于 `_align_row` 里对「细网格汉明略超阈值」的字形对做二次确认（指令F 四-1）。
    """
    x, y, w, h = g["x"], g["y"], g["w"], g["h"]
    x0, y0 = max(0, x - 1), max(0, y - 1)
    x1, y1 = min(bw.shape[1], x + w + 1), min(bw.shape[0], y + h + 1)
    patch = bw[y0:y1, x0:x1]
    tw = max(2, int(round(patch.shape[1] * grid_h / patch.shape[0])))
    norm = cv2.resize(patch, (tw, grid_h), interpolation=cv2.INTER_AREA)
    return (norm < 128).astype(np.uint8)


def _hamming(fa: np.ndarray, fb: np.ndarray) -> float:
    """归一化汉明距离 0~1（宽度对齐居中比较）。"""
    ha, wa_ = fa.shape
    hb, wb_ = fb.shape
    w = max(wa_, wb_)
    ca = np.zeros((ha, w), np.uint8); cb = np.zeros((hb, w), np.uint8)
    ca[:, (w - wa_) // 2:(w - wa_) // 2 + wa_] = fa
    cb[:, (w - wb_) // 2:(w - wb_) // 2 + wb_] = fb
    return float((ca != cb).sum()) / (ha * w)


def _align_row(rowa: List[Dict[str, Any]], rowb: List[Dict[str, Any]],
               bwa: np.ndarray, bwb: np.ndarray,
               ) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    """行内 DP 对齐（类 LCS，相似度=1-汉明）。返回 (匹配对, a未匹配, b未匹配)。"""
    na, nb = len(rowa), len(rowb)
    if na == 0 or nb == 0:
        return [], list(range(na)), list(range(nb))
    fpsa = [_fp(bwa, g) for g in rowa]
    fpsb = [_fp(bwb, g) for g in rowb]
    # 相似度矩阵（只算 x 邻近带，省时）
    sim = np.zeros((na, nb), np.float32)
    for i, ga in enumerate(rowa):
        for j, gb in enumerate(rowb):
            if abs(ga["cx"] - gb["cx"]) > max(40, ga["w"] * 3):
                sim[i, j] = 0.0
                continue
            h = _hamming(fpsa[i], fpsb[j])
            # 缩放鲁棒二次确认（指令F 四-1）：scale≈1.013 的轻微重采样会让字形
            # 边缘像素抖动，细网格（EMBED_GRID_H=24）汉明略超 0.30（实测 DWG
            # rev16→18 大量字形落在 0.30~0.45）。用粗网格（grid_h=12）重算——粗
            # 网格对边缘抖动不敏感，同一字符的粗网格汉明能落回 0.30 以下。仅当
            # 细网格汉明在 (0.30, 0.45] 且粗网格汉明 ≤0.30 时才放行，其余照旧。
            if EMBED_HAMMING_MAX < h <= EMBED_HAMMING_MAX + EMBED_COARSE_SLACK:
                hc = _hamming(_fp(bwa, ga, EMBED_COARSE_GRID_H),
                              _fp(bwb, gb, EMBED_COARSE_GRID_H))
                if hc <= EMBED_HAMMING_MAX:
                    h = hc
            sim[i, j] = 1.0 - h
    # DP：match 得 sim-0.55，skip 各罚 0.2（阈值 0.55=1-EMBED_HAMMING_MAX 的对偶）
    MATCH_BONUS = 1.0 - EMBED_HAMMING_MAX - 0.33   # 匹配净收益
    SKIP_A, SKIP_B = 0.20, 0.20
    dp = np.zeros((na + 1, nb + 1), np.float32)
    for i in range(1, na + 1):
        dp[i, 0] = dp[i - 1, 0] - SKIP_A
    for j in range(1, nb + 1):
        dp[0, j] = dp[0, j - 1] - SKIP_B
    for i in range(1, na + 1):
        for j in range(1, nb + 1):
            dp[i, j] = max(dp[i - 1, j - 1] + (sim[i - 1, j - 1] - 0.33 if sim[i-1, j-1] >= 1.0 - EMBED_HAMMING_MAX else -1e9),
                           dp[i - 1, j] - SKIP_A, dp[i, j - 1] - SKIP_B)
    # 回溯
    pairs = []
    i, j = na, nb
    while i > 0 and j > 0:
        if (sim[i - 1, j - 1] >= 1.0 - EMBED_HAMMING_MAX
                and abs(dp[i, j] - (dp[i - 1, j - 1] + sim[i - 1, j - 1] - 0.33)) < 1e-6):
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif abs(dp[i, j] - (dp[i - 1, j] - SKIP_A)) < 1e-6:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    ma = {p[0] for p in pairs}; mb = {p[1] for p in pairs}
    return pairs, [i for i in range(na) if i not in ma], [j for j in range(nb) if j not in mb]


def _detect_qr(bw: np.ndarray, glyphs: List[Dict[str, Any]],
               ) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int, int, int]]]:
    """检测二维码/条码区域：返回 (非码字形, 码区矩形列表)。

    条码判据（v2）：大量**细长连通域**在 60px 网格内密集聚集（条码=几十根竖条；
    正常文字行的组件宽高比适中、密度低，不会触发。v1 的形态学梯度判据会把
    所有横向密集文字行误判成码区——DWG 实测 68 块连 Rev 行都被吞，已废弃）。
    二维码用 QRCodeDetector（可靠）。"""
    rects: List[Tuple[int, int, int, int]] = []
    det = cv2.QRCodeDetector()
    try:
        ok, pts = det.detect(bw)
        if ok and pts is not None:
            for p in pts:
                xs = p[:, 0]; ys = p[:, 1]
                rects.append((int(xs.min()), int(ys.min()),
                              int(xs.max() - xs.min()), int(ys.max() - ys.min())))
    except cv2.error:
        pass
    # 条码：细长组件（宽高比<0.3）按 60px 网格聚集
    H, W = bw.shape
    grid = 60
    acc: Dict[Tuple[int, int], int] = {}
    for g in glyphs:
        w_, h_ = g["w"], g["h"]
        if min(w_, h_) < 3:
            continue  # 碎屑
        ar = min(w_, h_) / max(w_, h_)
        if ar < 0.3 and max(w_, h_) >= 14:
            acc[(g["x"] // grid, g["y"] // grid)] = acc.get((g["x"] // grid, g["y"] // grid), 0) + 1
    hot = {k for k, v in acc.items() if v >= 12}
    # 相邻 hot 网格合并成矩形
    seen = set()
    for (gx, gy) in hot:
        if (gx, gy) in seen:
            continue
        stack = [(gx, gy)]
        x0g, y0g, x1g, y1g = gx, gy, gx, gy
        seen.add((gx, gy))
        while stack:
            cx, cy = stack.pop()
            x0g, y0g = min(x0g, cx), min(y0g, cy)
            x1g, y1g = max(x1g, cx), max(y1g, cy)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    k = (cx + dx, cy + dy)
                    if k in hot and k not in seen:
                        seen.add(k)
                        stack.append(k)
        rects.append((x0g * grid, y0g * grid,
                      (x1g - x0g + 1) * grid, (y1g - y0g + 1) * grid))
    # 字形里落在码区内的剔除
    def in_rect(g):
        for (rx, ry, rw, rh) in rects:
            gx, gy = g["cx"], g["cy"]
            if rx - 5 <= gx <= rx + rw + 5 and ry - 5 <= gy <= ry + rh + 5:
                return True
        return False
    return [g for g in glyphs if not in_rect(g)], rects


def _decode_qr_content(gray: np.ndarray) -> str:
    det = cv2.QRCodeDetector()
    try:
        data, _, _ = det.detectAndDecode(gray)
        return data or ""
    except cv2.error:
        return ""


# ---------------------------------------------------------------- 转曲矢量文字（指令 F 三）
# 文字样曲线簇判据（实验 2026-10-16，11.pdf 249 个 drawing 实测）：
#   · 转曲文字 = 有 fill 且 rect 高度落在文字行高带 [3,15]pt 且 items≥50。
#     逐字符 drawing（宽 4~37pt、items 51~511）与整行 drawing（宽 67~185pt、
#     高统一 5.5~5.6pt、items 680~2059）都满足，宽度不限。
#   · 线段/边框 = 无 fill 或 items<50（实测 items 1~41），高度判据天然排除。
VECTOR_TEXT_H_MIN = 3.0          # 文字样曲线簇最小高度(pt)
VECTOR_TEXT_H_MAX = 15.0         # 文字样曲线簇最大高度(pt)
VECTOR_TEXT_ITEMS_MIN = 50       # 有 fill 曲线簇的最小 path 项数（文字 vs 线段分界）
VECTOR_TEXT_DPI = 600            # 转曲区渲染 dpi（字形级比对用高分辨率）
VECTOR_ROW_Y_TOL = 8.0           # 转曲文字行聚合 y 容差(pt)


def _vector_text_clusters(page: fitz.Page) -> List[List[fitz.Rect]]:
    """检测一页里的「文字样曲线簇」，按行聚合成簇列表。

    判据（实验标定）：有 fill 且高度∈[3,15]pt 且 items≥50 的 drawing 是转曲
    文字字形（线段/边框无 fill 或 items<50）。把 y 中心相近（容差 8pt）的
    字形合并成「行」，返回每行的 rect 列表（页面 pt 坐标）。

    注意：这里返回的是**文字字形所在的矩形**，用于后续渲染比对；不含图形块、
    表格线、图片（那些走别的路线）。"""
    try:
        dws = page.get_drawings()
    except Exception:
        return []
    glyph_rects: List[fitz.Rect] = []
    for dw in dws:
        r = dw.get("rect")
        if r is None:
            continue
        if not dw.get("fill"):
            continue
        h = r.height
        if not (VECTOR_TEXT_H_MIN <= h <= VECTOR_TEXT_H_MAX):
            continue
        if len(dw.get("items") or []) < VECTOR_TEXT_ITEMS_MIN:
            continue
        glyph_rects.append(fitz.Rect(r))
    if not glyph_rects:
        return []
    # 按 y 中心聚行（转曲文字里整行 drawing 也会与同行的逐字符 drawing 合并）
    glyph_rects.sort(key=lambda r: (r.y0 + r.y1) / 2)
    rows: List[List[fitz.Rect]] = []
    cur_y = 0.0
    for r in glyph_rects:
        cy = (r.y0 + r.y1) / 2
        if rows and abs(cy - cur_y) <= VECTOR_ROW_Y_TOL:
            rows[-1].append(r)
            cur_y = sum((x.y0 + x.y1) / 2 for x in rows[-1]) / len(rows[-1])
        else:
            rows.append([r])
            cur_y = cy
    return rows


def _render_vector_region(page: fitz.Page, rect: fitz.Rect) -> Optional[np.ndarray]:
    """把转曲文字区按 600dpi 渲染成灰度位图（供字形级比对）。"""
    try:
        mat = fitz.Matrix(VECTOR_TEXT_DPI / 72.0, VECTOR_TEXT_DPI / 72.0)
        pm = page.get_pixmap(matrix=mat, clip=rect, colorspace=fitz.csGRAY,
                             annots=False)
        return np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width)
    except Exception:
        return None


def _text_word_rects(page: fitz.Page) -> List[fitz.Rect]:
    """一页文字层覆盖到的词矩形（页面 pt）。"""
    out: List[fitz.Rect] = []
    try:
        for w in page.get_text("words"):
            out.append(fitz.Rect(w[0], w[1], w[2], w[3]))
    except Exception:
        pass
    return out


def _covered_by_text(rect: fitz.Rect, word_rects: List[fitz.Rect],
                     cover_ratio: float = 0.3) -> bool:
    """判断一个转曲行矩形是否已被文字层覆盖（避免与字符流路线重复报）。

    11.pdf 实测：文字层与转曲层是「同一段文字的两个副本」，两者 y 中心几乎
    重合，但转曲行的合并 rect 很宽（整行 x 到 666pt），文字词分散在各列，
    面积覆盖比例会 <30%。故改用**「y 带交叠」判据**：转曲行与任一文字词的
    垂直范围有交叠即视为被覆盖（一行文字高度固定，副本层必然同 y）。"""
    if not word_rects:
        return False
    for wr in word_rects:
        # 垂直交叠且水平有部分交叠（避免把远处不同列的同行误判）
        y_overlap = min(rect.y1, wr.y1) - max(rect.y0, wr.y0)
        x_overlap = min(rect.x1, wr.x1) - max(rect.x0, wr.x0)
        if y_overlap > 0 and x_overlap > 0:
            return True
    return False


# ---------------------------------------------------------------- 主入口
def embedded_image_diff(old_path: str, new_path: str,
                        ) -> EmbedReport:
    """内嵌图片专用路线主入口（指令 E 五层）。"""
    t0 = time.time()
    rep = EmbedReport()
    rep.stats = {"pages": [], "skipped_hash": 0, "replaced": 0, "resampled": 0,
                 "glyph_stats": [], "qr": []}
    if not EMBED_ROUTE_ON:
        rep.stats["disabled"] = True
        return rep
    da = fitz.open(old_path)
    db = fitz.open(new_path)
    try:
        npages = min(da.page_count, db.page_count)
        for i in range(npages):
            _loc_checkpoint()     # 「停止对比」中止检查点（页级）
            page_stat: Dict[str, Any] = {"page": i + 1, "imgs": 0}
            old_imgs = _page_images(da, i)
            new_imgs = _page_images(db, i)
            page_stat["imgs"] = len(old_imgs)
            for (pa, pb) in _pair_images(old_imgs, new_imgs):
                # L1 精确同一性：流哈希相同 → 跳过
                if pa and pb and EMBED_L1_HASH_ON and pa.stream_md5 == pb.stream_md5:
                    rep.stats["skipped_hash"] += 1
                    page_stat.setdefault("hash_same", 0)
                    page_stat["hash_same"] += 1
                    continue
                if pa is None and pb is not None:
                    rep.items.append(EmbedDiff(page=i, rect=pb.bbox, kind="added",
                                               conf="high", note="新增图片"))
                    continue
                if pb is None and pa is not None:
                    rep.items.append(EmbedDiff(page=i, rect=pa.bbox, kind="removed",
                                               conf="high", note="删除图片"))
                    continue
                assert pa is not None and pb is not None
                # L2 解码（方向已归一化）
                ga = _decode_gray(da, pa)
                gb = _decode_gray(db, pb)
                # L3 配准
                scale_hint = (pb.pix_h / max(pa.pix_h, 1) + pb.pix_w / max(pa.pix_w, 1)) / 2
                if EMBED_L3_REGISTER_ON:
                    state, s, tx, ty, inl = _register(ga, gb, scale_hint)
                else:
                    state, s, tx, ty, inl = "resampled", scale_hint, 0.0, 0.0, 0
                page_stat.setdefault("register", []).append(
                    {"xref": pa.xref, "state": state, "scale": round(s, 5),
                     "tx": round(tx, 2), "ty": round(ty, 2), "inlier": inl / 1000})
                if state == "replaced":
                    rep.stats["replaced"] += 1
                    rep.items.append(EmbedDiff(page=i, rect=pa.bbox, kind="replaced",
                                               conf="high", note="整图被替换"))
                    continue
                if state == "resampled" and abs(s - 1.0) >= EMBED_SCALE_SAME:
                    gb = _resample_to(gb, ga.shape)
                    rep.stats["resampled"] += 1
                # L4 字形级比对
                if EMBED_L4_GLYPH_ON:
                    _glyph_compare(da, db, i, pa, pb, ga, gb, rep, page_stat)
            rep.stats["pages"].append(page_stat)
        rep.stats["elapsed_s"] = round(time.time() - t0, 2)
    finally:
        da.close()
        db.close()
    rep.elapsed_s = time.time() - t0
    return rep


def _normalize_items(rep: EmbedReport) -> None:
    """rect 归一化（to_pt 的 y 是反向的？否，已修正为正向）+ 碎框过滤 + removed/added 高重合配成 changed。"""
    for d in rep.items:
        x0, x1 = min(d.rect[0], d.rect[2]), max(d.rect[0], d.rect[2])
        y0, y1 = min(d.rect[1], d.rect[3]), max(d.rect[1], d.rect[3])
        d.rect = (x0, y0, x1, y1)
    # 碎框过滤：短边 <2pt（表格线 1px 漂移、退化框）
    rep.items = [d for d in rep.items
                 if d.kind in ("qr", "replaced")
                 or min(d.rect[2] - d.rect[0], d.rect[3] - d.rect[1]) >= 2.0]
    adds = [d for d in rep.items if d.kind == "added"]
    consumed = set()
    for r in [d for d in rep.items if d.kind == "removed"]:
        best, best_iou = None, 0.0
        for k, a in enumerate(adds):
            if k in consumed:
                continue
            x0 = max(r.rect[0], a.rect[0]); y0 = max(r.rect[1], a.rect[1])
            x1 = min(r.rect[2], a.rect[2]); y1 = min(r.rect[3], a.rect[3])
            if x1 <= x0 or y1 <= y0:
                continue
            inter = (x1 - x0) * (y1 - y0)
            ar = max(1e-6, (r.rect[2]-r.rect[0])*(r.rect[3]-r.rect[1]))
            aa = max(1e-6, (a.rect[2]-a.rect[0])*(a.rect[3]-a.rect[1]))
            iou = inter / min(ar, aa)
            if iou > best_iou:
                best_iou, best = iou, k
        if best is not None and best_iou > 0.55:
            consumed.add(best)
            r.kind = "changed"
            r.note += "(同位字形变化)"
    rep.items = [d for d in rep.items
                 if d.kind != "added" or adds.index(d) not in consumed]


def _glyph_compare(da: fitz.Document, db: fitz.Document, page_idx: int,
                   pa: EmbedImage, pb: EmbedImage, ga: np.ndarray, gb: np.ndarray,
                   rep: EmbedReport, page_stat: Dict[str, Any]) -> None:
    """L4 字形级比对（含二维码跳过与解码比对）。"""
    bwa = _binarize(ga)
    bwb = _binarize(gb)
    gla, qa = _detect_qr(bwa, _glyphs(bwa))
    glb, qb = _detect_qr(bwb, _glyphs(bwb))
    # 二维码内容比对
    for (ax, ay, aw, ah) in qa:
        ca = _decode_qr_content(ga[max(0, ay):ay + ah + 1, max(0, ax):ax + aw + 1])
        if ca:
            rep.stats["qr"].append({"page": page_idx + 1, "decoded": True, "content": ca})
            # 找 new 侧同区域二维码
            for (bx, by, bw_, bh_) in qb:
                cb = _decode_qr_content(gb[max(0, by):by + bh_ + 1, max(0, bx):bx + bw_ + 1])
                if cb and cb != ca:
                    rep.items.append(EmbedDiff(
                        page=page_idx, rect=pa.to_pt(ax, ay) + pa.to_pt(ax + aw, ay + ah),
                        kind="qr", conf="mid",
                        old_text=ca, new_text=cb, note="二维码内容变化"))
                    break
    # 行分组 + 配对 + DP 对齐
    rowsa = _rows(gla)
    rowsb = _rows(glb)
    # 行配对（cy 最近）
    used = set()
    dists = []          # 匹配字形的汉明距离分布（报告用）
    for ra in rowsa:
        cya = float(np.mean([g["cy"] for g in ra]))
        best, bestd = None, 1e9
        for j, rb in enumerate(rowsb):
            if j in used:
                continue
            cyb = float(np.mean([g["cy"] for g in rb]))
            d = abs(cya - cyb)
            if d < bestd:
                bestd, best = d, j
        if best is None or bestd > 30:
            # 整行旧侧独有 → removed
            ra.sort(key=lambda g: g["cx"])
            x0 = min(g["x"] for g in ra); x1 = max(g["x"] + g["w"] for g in ra)
            y0 = min(g["y"] for g in ra); y1 = max(g["y"] + g["h"] for g in ra)
            rep.items.append(EmbedDiff(
                page=page_idx,
                rect=pa.to_pt(x0, y0) + pa.to_pt(x1, y1),
                kind="removed", conf="mid", note="行级：仅旧版有"))
            continue
        rb = rowsb[best]
        used.add(best)
        pairs, uma, umb = _align_row(ra, rb, bwa, bwb)
        # 同位字形对的 XOR 判据（指令F 四-2 重定：原 EMBED_XOR_ABS_MIN=320 仅
        # 来自一对样本且**未按组件面积分段**。实测 DWG rev16/17/18 三版 911 对：
        # 大组件（面积>500，表格线/条码/粘连块）XOR 天然到 1000~7000，会淹掉阈值；
        # 极小组件（面积<100）边缘重采样抖动占比高。真文字变化集中在
        # 面积∈[100,500] 的组件、XOR≥150（rev16→18 的 Rev16→Rev18 单字 + 地址块
        # 7 处 xor=169~371 全落此带）。故改为「面积分段 + XOR 绝对像素」双门槛）。
        for (ia, jb) in pairs:
            ga_, gb_ = ra[ia], rb[jb]
            hh = min(ga_["h"], gb_["h"]); ww = min(ga_["w"], gb_["w"])
            area = ww * hh
            # 面积上限：排除表格线/条码/图片粘连块（XOR 值巨大但非文字变化）。
            if area > EMBED_GLYPH_AREA_MAX:
                continue
            xa = bwa[ga_["y"]:ga_["y"]+hh, ga_["x"]:ga_["x"]+ww] < 128
            xb = bwb[gb_["y"]:gb_["y"]+hh, gb_["x"]:gb_["x"]+ww] < 128
            n_xor = int((xa != xb).sum())
            if area >= EMBED_GLYPH_AREA_MIN and n_xor >= EMBED_XOR_ABS_MIN:
                r = pa.to_pt(ga_["x"], ga_["y"]) + pa.to_pt(ga_["x"] + ga_["w"], ga_["y"] + ga_["h"])
                if min(r[2] - r[0], r[3] - r[1]) >= 2.5:
                    rep.items.append(EmbedDiff(
                        page=page_idx, rect=r, kind="changed", conf="mid",
                        note="同位字形变化 XOR=%dpx" % n_xor))
        # 未匹配字形 → 差异（旧侧=removed 红框，新侧=added 绿框）
        def _emit(gs, kind):
            if not gs:
                return
            gs.sort(key=lambda g: g["cx"])
            # 把相邻字形合并成词块（间距<字高×0.8）
            groups: List[List[Dict[str, Any]]] = [[gs[0]]]
            for g in gs[1:]:
                prev = groups[-1][-1]
                if g["x"] - (prev["x"] + prev["w"]) < max(10, g["h"] * 0.8):
                    groups[-1].append(g)
                else:
                    groups.append([g])
            for grp in groups:
                x0 = min(g["x"] for g in grp); x1 = max(g["x"] + g["w"] for g in grp)
                y0 = min(g["y"] for g in grp); y1 = max(g["y"] + g["h"] for g in grp)
                src = pa if kind == "removed" else pb
                r = src.to_pt(x0, y0) + src.to_pt(x1, y1)
                # 碎线过滤：短边<3pt 的框是表格线 1px 漂移，不报
                if min(r[3] - r[1], r[2] - r[0]) < 3.0:
                    continue
                rep.items.append(EmbedDiff(
                    page=page_idx, rect=r, kind=kind, conf="mid",
                    note="字形级:%d字形" % len(grp)))
        _emit([ra[i] for i in uma], "removed")
        _emit([rb[j] for j in umb], "added")
        # 统计匹配距离
        for (ia, jb) in pairs:
            d = _hamming(_fp(bwa, ra[ia]), _fp(bwb, rb[jb]))
            dists.append(round(d, 3))
    # 新侧独有整行 → added
    for j, rb in enumerate(rowsb):
        if j in used:
            continue
        rb.sort(key=lambda g: g["cx"])
        x0 = min(g["x"] for g in rb); x1 = max(g["x"] + g["w"] for g in rb)
        y0 = min(g["y"] for g in rb); y1 = max(g["y"] + g["h"] for g in rb)
        rep.items.append(EmbedDiff(
            page=page_idx,
            rect=pb.to_pt(x0, y0) + pb.to_pt(x1, y1),
            kind="added", conf="mid", note="行级：仅新版有"))
    # rect 归一化 + removed/added 高重合配成 changed
    _normalize_items(rep)
    if dists:
        rep.stats["glyph_stats"].append({
            "page": page_idx + 1, "matched": len(dists),
            "p50": float(np.percentile(dists, 50)),
            "p90": float(np.percentile(dists, 90)),
            "max": max(dists)})


# ================================================================
# 指令 G（2026-10-19）：纯图片 / 内嵌图片的「块匹配路线」
# ----------------------------------------------------------------
# 取代以 L4 字形级「整页铺开」为主的做法。字形级只用于「已判变化的块」
# 内部细化，不再整页铺开（整页铺开已被证明产生大量重采样噪声）。
#
# 分层（对应用户给出的步骤）：
#   S1 精确同一性：内嵌图片像素 md5 相同 → 跳过（保留，复用已有逻辑）
#   S2 方向归一化：按页面矩阵还原页面所见方向（保留，`_decode_gray` 已做）
#   S3 尺度归一化：缩放比由原生尺寸比得出，0.995~1.005 视为 1；否则对称
#      重采样到「较低分辨率那一侧」的网格（INTER_AREA，不凭空放大）
#   S4 整图配准：只估平移（相位相关 / ECC），内点<50% 判整图替换
#   S5 行块切分与匹配：原位 NCC → 窗口搜索 → 位移向量分组 → 5 倍判据
#   S6 分层细化：未匹配块先拆词块再拆字形，用邻居位移预测位置找匹配
#   S7 成对关联：同一位置附近的旧独有 + 新独有合并成「修改」
#   S8 条码/二维码：二维码解码比对；一维条码块相关系数 → 待复核低档
#   S9 OCR 角色：只给已判定的变化块写文字说明，不参与判定
# ================================================================

# ---- S3~S5 阈值（进 Config，先用样本标定值，后续可用校准脚本重定）----
BLOCK_SAME_SCALE_LO = 0.995    # 缩放比下限，[lo,hi] 内视为 1 不重采样
BLOCK_SAME_SCALE_HI = 1.005    # 缩放比上限
BLOCK_HALF_SCALE = 0.5         # 处理用半分辨率（相对原生位图）
BLOCK_INPLACE_NCC = 0.93       # 原位模板匹配 NCC 阈值，≥ 判「没变」
BLOCK_UNMATCH_NCC = 0.80       # 窗口搜索 NCC 阈值，< 判「未匹配」
BLOCK_SEARCH_WIN = 300         # 窗口搜索半径(px@半分辨率)，对应 ±300px
BLOCK_SHIFT_TOL = 12           # 位移向量分组容差(px@半分辨率)
BLOCK_MOVE_RATIO = 5.0         # 位移/弦长 ≤ 此值判「移动」
BLOCK_MIN_AREA = 40            # 行块最小面积(px@半分辨率)，滤碎屑
BLOCK_MAX_AREA = 400000        # 行块最大面积，滤整页大块
BLOCK_INLIER_MIN = 0.50        # 整图配准内点比例下限，< 判整图替换
BLOCK_ECC_EPS = 1e-4           # ECC 收敛阈值
BLOCK_ECC_MAX_ITER = 200       # ECC 最大迭代
# S6 分层细化
BLOCK_WORD_KERNEL = (9, 3)     # 词块膨胀核（更小，字间粘连成词）
BLOCK_WORD_OFF_DX = 250        # 词块命中点相对预测的水平偏差上限(px@半分辨率)。
                               # 行内重排（DOC-1001 右上标签 LOT 左移 202px）也要收；
BLOCK_WORD_OFF_DY = 40         # 垂直偏差上限 —— 副本误配（多联标签上下同文行）
                               # 的 dy≈±225px，用小 dy 容差即可排除（DOC-1001 实测：
                               # 欧氏容差区分不了 202px 真匹配与 225px 副本）。
BLOCK_WORD_NCC_MIN = 0.72      # 词块放宽 NCC 下限：跨版本重光栅化噪声会把
                               # 同内容词的 NCC 压到 0.76~0.78（DOC-1001 实测），
                               # 0.80 会漏；配合「偏移共识聚类」防噪声误配。
BLOCK_ALIGN_INK_TOL = 0.15     # 对齐后墨迹残差容差：双向未覆盖比例 ≤ 此值判
                               # 「内容一致」（纯平移/重切分），高于它判行内改写。
BLOCK_GLYPH_KERNEL = (3, 3)    # 字形膨胀核
BLOCK_NEIGH_PREDICT_R = 2.0    # 邻居位移预测窗口放大系数
BLOCK_MERGE_GAP = 6            # 相邻块合并间距(px@半分辨率)
# S8 条码
BLOCK_BAR_NCC = 0.60           # 一维条码块相关系数下限（待复核档）
BLOCK_QR_SIDE_MIN = 50         # 二维码最小边长(px@半分辨率)


@dataclass
class Block:
    """S5 行块：一个连通域块（半分辨率像素坐标）。"""
    x: int
    y: int
    w: int
    h: int
    area: int
    cx: float
    cy: float
    # 匹配结果（S5/S6 填充）
    matched: bool = False
    dx: float = 0.0             # 位移向量（相对原位，像素@半分辨率）
    dy: float = 0.0
    ncc: float = 0.0            # 最佳 NCC 得分
    kind: str = ""              # "same"/"moved"/"removed"/"added"/"changed"
    matched_with: Optional["Block"] = None   # 配对的对面块




def _iou_blocks(a: Block, b: Block) -> float:
    """两块的 IoU（像素坐标）。"""
    x0 = max(a.x, b.x); y0 = max(a.y, b.y)
    x1 = min(a.x + a.w, b.x + b.w); y1 = min(a.y + a.h, b.y + b.h)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    ar_a = a.w * a.h
    ar_b = b.w * b.h
    return inter / max(1e-6, min(ar_a, ar_b))


def _ncc(template: np.ndarray, image: np.ndarray, x: int, y: int) -> float:
    """在 image 的 (x,y) 位置计算与 template 的 NCC（零均值归一化互相关）。

    template/image 都是 uint8 灰度。返回 [-1,1]。"""
    th, tw = template.shape
    if y < 0 or x < 0 or y + th > image.shape[0] or x + tw > image.shape[1]:
        return -1.0
    patch = image[y:y + th, x:x + tw].astype(np.float32)
    t = template.astype(np.float32)
    tp = t - t.mean()
    pp = patch - patch.mean()
    denom = np.sqrt((tp * tp).sum() * (pp * pp).sum())
    if denom < 1e-9:
        return 0.0
    return float((tp * pp).sum() / denom)


def _best_match(tmpl: np.ndarray, image: np.ndarray,
                x0: int, y0: int, win: int
                ) -> Tuple[float, float, float]:
    """在 image 的 (x0,y0)±win 窗口内，用 matchTemplate(TM_CCOEFF_NORMED)
    找 tmpl 的最佳匹配位置，返回 (ncc, dx, dy)。

    相比逐点 `_ncc`，matchTemplate 是 C++ 实现，速度快 1~2 个数量级。
    """
    th, tw = tmpl.shape
    ih, iw = image.shape
    # 裁出搜索窗（含模板大小余量），避免整图 matchTemplate
    sx0 = max(0, x0 - win)
    sy0 = max(0, y0 - win)
    sx1 = min(iw, x0 + tw + win)
    sy1 = min(ih, y0 + th + win)
    if sx1 - sx0 < tw or sy1 - sy0 < th:
        return (-1.0, 0.0, 0.0)
    roi = image[sy0:sy1, sx0:sx1]
    res = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    dx = float(maxloc[0] + sx0 - x0)
    dy = float(maxloc[1] + sy0 - y0)
    return (float(maxv), dx, dy)


def _best_match_rect(tmpl: np.ndarray, image: np.ndarray,
                     x0: int, y0: int, win_x: int, win_y: int,
                     mask_dy: int
                     ) -> Tuple[float, float, float]:
    """矩形窗口 matchTemplate：水平 ±win_x、垂直 ±win_y，且把匹配位置
    垂直偏差 |dy| > mask_dy 的结果区置 -1（多联标签上下同文副本防护 ——
    副本 NCC=1.0 会抢占方形窗口的最高分，DOC-1001 实测）。

    返回 (ncc, dx, dy)，dx/dy 相对 (x0,y0)。
    """
    th, tw = tmpl.shape
    ih, iw = image.shape
    sx0 = max(0, x0 - win_x)
    sy0 = max(0, y0 - win_y)
    sx1 = min(iw, x0 + tw + win_x)
    sy1 = min(ih, y0 + th + win_y)
    if sx1 - sx0 < tw or sy1 - sy0 < th:
        return (-1.0, 0.0, 0.0)
    roi = image[sy0:sy1, sx0:sx1]
    res = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
    # 屏蔽带：res 行 r 对应匹配位置相对预测顶的 dy = r + sy0 - y0。
    # |dy| > mask_dy 的行置 -1（上带 r < y0 - sy0 - mask_dy，下带对称）。
    top_band = max(0, (y0 - sy0) - mask_dy)
    bot_band = max(0, (sy1 - th - y0) - mask_dy)
    if top_band:
        res[:top_band, :] = -1.0
    if bot_band:
        res[res.shape[0] - bot_band:, :] = -1.0
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    dx = float(maxloc[0] + sx0 - x0)
    dy = float(maxloc[1] + sy0 - y0)
    return (float(maxv), dx, dy)


def _extract_blocks(gray: np.ndarray, half_scale: float) -> List[Block]:
    """S5：二值化 + 膨胀 + 连通域 → 行块列表（半分辨率坐标）。

    与用户流程一致：转灰度（已是灰度）、缩到 50%、二值化、膨胀(25×5)、连通域。
    但增加**水平投影切行**：先按行的水平投影把整页切成独立行带，再在每行带内
    做膨胀连通域——避免横向膨胀跨行粘连出一个「整页大块」（本样本标签图文字
    密布，直接膨胀 25px 会把相邻行连成 358809px 的整页块）。
    """
    h, w = gray.shape
    hw, hh = max(1, int(round(w * half_scale))), max(1, int(round(h * half_scale)))
    small = cv2.resize(gray, (hw, hh), interpolation=cv2.INTER_AREA)
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    inv = (bw < 128).astype(np.uint8)      # 前景墨迹 = 黑像素 = 1
    # ---- 水平投影切行：每行前景像素数 > 阈值 的行带切出来 ----
    rowsum = inv.sum(axis=1)
    row_thresh = max(2, int(hh * 0.003))   # 一行至少 ~0.3% 高度有墨迹才成行
    # 找连续行带
    bands: List[Tuple[int, int]] = []
    y = 0
    while y < hh:
        if rowsum[y] >= row_thresh:
            y0 = y
            while y < hh and rowsum[y] >= row_thresh:
                y += 1
            # 行带之间间距 < 8px 视为同一行（文字上下结构）
            y1 = y
            while y1 < hh and rowsum[y1] == 0:
                gap_start = y1
                while y1 < hh and rowsum[y1] == 0:
                    y1 += 1
                if y1 - gap_start <= 8 and y1 < hh and rowsum[y1] >= row_thresh:
                    # 间隙小，合并
                    y = y1
                    continue
                else:
                    break
            bands.append((y0, y1))
            y = y1
        else:
            y += 1
    # ---- 在每个行带内膨胀 + 连通域 ----
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 5))
    blocks: List[Block] = []
    for (y0, y1) in bands:
        band = inv[y0:y1, :]
        dilated = cv2.dilate(band, kernel, iterations=1)
        n, _, stats, cents = cv2.connectedComponentsWithStats(dilated, 8)
        for i in range(1, n):
            x, yy, w_, h_, area = stats[i]
            if area < BLOCK_MIN_AREA or area > BLOCK_MAX_AREA:
                continue
            # 过滤「整页大块」：块高度超过半图高度 60% 或宽度超过 95% 的，
            # 是膨胀过度把多行/整页粘连成的块（DWG 密集标签图实测 2438×2626pt），
            # 这类块不是单一语义单元，直接丢弃（真变化会由更细的块报出）。
            if h_ > hh * 0.6 or w_ > hw * 0.95:
                continue
            cx, cy = cents[i]
            blocks.append(Block(x=int(x), y=int(y0 + yy), w=int(w_), h=int(h_),
                                area=int(area), cx=float(cx), cy=float(y0 + cy)))
    return blocks, bw, small


def _register_block(ga: np.ndarray, gb: np.ndarray, scale_hint: float
                    ) -> Tuple[str, float, float, float, float]:
    """S4：整图配准，只估平移（相位相关 + ECC 精修）。

    返回 (state, scale, tx, ty, inlier_ratio)。
    state: "same"(缩放≈1) / "resampled"(需重采样) / "replaced"(内点不足)。
    """
    if abs(scale_hint - 1.0) < (BLOCK_SAME_SCALE_HI - 1.0):
        s = 1.0
    else:
        s = scale_hint
    # 用互相关（相位相关）粗估平移，再 ECC 精修
    try:
        # 降到 ~800px 以内提速
        maxdim = 800
        def cv_(img):
            h, w = img.shape
            if max(h, w) > maxdim:
                r = maxdim / max(h, w)
                return cv2.resize(img, (int(w * r), int(h * r)), interpolation=cv2.INTER_AREA)
            return img
        A = cv_(ga); B = cv_(gb)
        A32 = A.astype(np.float32); B32 = B.astype(np.float32)
        # 相位相关
        (tx, ty), resp = cv2.phaseCorrelate(A32, B32)
        # ECC 精修（平移模型）
        warp = np.eye(2, 3, dtype=np.float32)
        warp[0, 2] = tx; warp[1, 2] = ty
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                    BLOCK_ECC_MAX_ITER, BLOCK_ECC_EPS)
        try:
            _, warp = cv2.findTransformECC(A32, B32, warp,
                                           cv2.MOTION_TRANSLATION, criteria)
        except cv2.error:
            pass
        tx, ty = warp[0, 2], warp[1, 2]
        # 内点比例：重投影后残差小像素占比
        if A.shape == B.shape:
            M = np.float32([[1, 0, tx], [0, 1, ty]])
            Bw = cv2.warpAffine(B, M, (B.shape[1], B.shape[0]))
            diff = np.abs(A.astype(np.int16) - Bw.astype(np.int16))
            inlier = float((diff < 40).mean())
        else:
            inlier = 1.0
        if inlier < BLOCK_INLIER_MIN:
            return ("replaced", s, tx, ty, inlier)
        state = "same" if abs(s - 1.0) < (BLOCK_SAME_SCALE_HI - 1.0) else "resampled"
        return (state, s, tx, ty, inlier)
    except cv2.error:
        return ("resampled", s, 0.0, 0.0, 0.0)


def _resample_block(gb: np.ndarray, target_hw: Tuple[int, int]) -> np.ndarray:
    """S3：对称重采样到目标网格（INTER_AREA，只降不升）。"""
    return cv2.resize(gb, (target_hw[1], target_hw[0]), interpolation=cv2.INTER_AREA)


def _match_blocks(blocks_a: List[Block], blocks_b: List[Block],
                  bwa: np.ndarray, bwb: np.ndarray,
                  half_scale: float) -> None:
    """S5：行块匹配（用 matchTemplate 高效实现）。

    对每个旧块：① 原位 NCC≥0.93 判「没变」；② 否则在 ±300px 窗口内搜索最佳
    位移；③ 位移/弦长≤5 判「移动」。匹配不上的（NCC<0.8）旧侧=removed。
    新侧块的反向匹配在 `_match_blocks` 调用后由 `_refine_unmatched` 统一补齐。
    """
    H, W = bwa.shape
    center_x, center_y = W / 2.0, H / 2.0
    win = int(BLOCK_SEARCH_WIN * half_scale)
    for a in blocks_a:
        tmpl = bwa[a.y:a.y + a.h, a.x:a.x + a.w]
        # ① 原位匹配
        ncc0 = _ncc(tmpl, bwb, a.x, a.y)
        if ncc0 >= BLOCK_INPLACE_NCC:
            a.matched = True
            a.kind = "same"
            a.ncc = ncc0
            a.dx = a.dy = 0.0
            continue
        # ② 窗口搜索最佳位移（matchTemplate 局部搜索）
        best_ncc, best_dx, best_dy = _best_match(tmpl, bwb, a.x, a.y, win)
        if best_ncc >= BLOCK_UNMATCH_NCC:
            a.matched = True
            a.ncc = best_ncc
            a.dx, a.dy = best_dx, best_dy
            # ④ 位移/弦长判据：弦长 = 块中心到画面中心的距离
            #    ⚠️ 位移≈0 的块判「没变」（same），不是「移动」——shift/chord 在
            #    位移为 0 时恒为 0≤阈值，若不先判 0 会把所有原位块误判成 moved。
            shift = float(np.hypot(best_dx, best_dy))
            if shift < 2.0:                     # 位移 <2px@半分辨率 = 基本没动
                a.kind = "same"
            else:
                chord = float(np.hypot(a.cx - center_x, a.cy - center_y))
                if chord > 1e-6 and shift / chord <= BLOCK_MOVE_RATIO:
                    a.kind = "moved"
                else:
                    a.kind = "same"
        else:
            a.matched = False
            a.kind = "removed"
            a.ncc = best_ncc
    # 新侧独有：反向——未被任何旧块匹配的新块 = added
    # 用旧块的位移预测新块位置，建立配对（moved/same 的块找到对应新块）
    used_b = set()
    for a in blocks_a:
        if not a.matched or a.kind == "removed":
            continue
        px, py = a.cx + a.dx, a.cy + a.dy
        best, best_d = None, 1e9
        for b in blocks_b:
            if id(b) in used_b:
                continue
            d = (b.cx - px) ** 2 + (b.cy - py) ** 2
            if d < best_d:
                best_d, best = d, b
        # 预测点与新块中心足够近（块对角线的一半）才配对
        if best is not None:
            thresh = max(a.w, a.h, best.w, best.h) * 0.6
            if best_d <= thresh * thresh:
                best.matched = True
                best.kind = a.kind
                best.dx, best.dy = a.dx, a.dy
                best.ncc = a.ncc
                best.matched_with = a
                a.matched_with = best
                used_b.add(id(best))
    for b in blocks_b:
        if not b.matched:
            b.kind = "added"


def _split_block(bw: np.ndarray, block: Block, kernel_size: Tuple[int, int]
                 ) -> List[Block]:
    """S6：把一个块在它的 bbox 内，用更小的膨胀核拆成子块（词块/字形）。

    返回子块列表（坐标仍是整图半分辨率坐标）。
    """
    sub = bw[block.y:block.y + block.h, block.x:block.x + block.w]
    if sub.size == 0:
        return []
    inv = (sub < 128).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, kernel_size)
    dilated = cv2.dilate(inv, kernel, iterations=1)
    n, _, stats, cents = cv2.connectedComponentsWithStats(dilated, 8)
    out: List[Block] = []
    for i in range(1, n):
        x, y, w_, h_, area = stats[i]
        if area < 8:
            continue
        cx, cy = cents[i]
        out.append(Block(x=int(block.x + x), y=int(block.y + y),
                         w=int(w_), h=int(h_), area=int(area),
                         cx=float(block.x + cx), cy=float(block.y + cy)))
    return out


def _refine_unmatched(blocks_a: List[Block], blocks_b: List[Block],
                      bwa: np.ndarray, bwb: np.ndarray,
                      half_scale: float) -> List[Tuple[Block, Optional[Block]]]:
    """S6：分层细化。对「未匹配/低分」的块，先拆词块，用邻居位移预测位置找
    匹配（matchTemplate 局部搜索）。返回最终「确实找不到对应」的 (旧块或None,
    新块或None) 对。

    核心解决缺点 A（NP+Nominal Pressure 合并差异误报）与 B（末行被吞）。
    """
    removed_a = [a for a in blocks_a if a.kind == "removed"]
    added_b = [b for b in blocks_b if b.kind == "added"]
    moved_a = [a for a in blocks_a if a.kind == "moved"]

    def predict(a: Block) -> Tuple[float, float]:
        if moved_a:
            nearest = min(moved_a, key=lambda m: (m.cx - a.cx) ** 2 + (m.cy - a.cy) ** 2)
            return a.cx + nearest.dx, a.cy + nearest.dy
        return a.cx, a.cy

    # 先把新侧 added 块也拆成词块（一次性），供后续匹配
    added_words: List[Tuple[Block, Block]] = []  # (父块, 词块)
    for b in added_b:
        for sb in _split_block(bwb, b, BLOCK_WORD_KERNEL):
            added_words.append((b, sb))

    matched_b_ids = set()
    unresolved_a: List[Block] = []
    changed_pairs: List[Tuple[Block, Block]] = []   # 词块确认的行内改写 → changed
    for a in removed_a:
        sub_a = _split_block(bwa, a, BLOCK_WORD_KERNEL)
        if not sub_a:
            unresolved_a.append(a)
            continue
        px, py = predict(a)
        # 词块搜索窗口（矩形）：水平 ±(OFF_DX+30) 容纳大幅行内重排，垂直
        # ±(OFF_DY+30) 并把 |dy|>OFF_DY 的结果带屏蔽 —— 多联标签上下同文
        # 副本（NCC=1.0）无法抢占真位置匹配（DOC-1001 实测）。
        win_x = int(min(BLOCK_SEARCH_WIN * half_scale * BLOCK_NEIGH_PREDICT_R,
                        BLOCK_WORD_OFF_DX + 30))
        win_y = int(min(BLOCK_SEARCH_WIN * half_scale * BLOCK_NEIGH_PREDICT_R,
                        BLOCK_WORD_OFF_DY + 30))
        # 统计：该块有多少个词块在对面找到了匹配。
        # ⚠️ 2026-10-19 修复（DOC-1001 rev15vs16 12 处假删）：词块命中**不再要求**
        #    落进新侧 added 父块 —— 行块只是平移几~十几 pt 时，新侧对应行早被
        #    `_match_blocks` 反向配对认领（没判 added），added_words 里根本没有
        #    它，命中率恒 0 → 整行误报 removed。改为：词块在对面窗口内 NCC 达标
        #    即算「内容还在」；落点恰在 added 父块内的，顺带记 matched（防该
        #    added 再误报绿框）。
        hit_words: List[Tuple[Block, Block]] = []   # (词块, 落进的 added 父块)
        cands: List[Tuple[float, float]] = []       # 放宽 NCC 的候选词位移
        for sa in sub_a:
            tmpl = bwa[sa.y:sa.y + sa.h, sa.x:sa.x + sa.w]
            # 在预测位置附近用矩形窗 matchTemplate 找。
            # 匹配点词中心 ≈ (px + best_dx, py + best_dy)（x0 = px - sa.w//2）。
            best_ncc, best_dx, best_dy = _best_match_rect(
                tmpl, bwb, int(px - sa.w // 2), int(py - sa.h // 2),
                win_x, win_y, BLOCK_WORD_OFF_DY)
            if best_ncc < BLOCK_WORD_NCC_MIN:
                continue
            # 词中心相对「预测位置」的偏差：水平放宽（行内重排幅度大，DOC-1001
            # 右上标签 LOT 左移 202px）、垂直收紧（多联标签上下同文副本的
            # dy≈±225px，小 dy 容差即可排除 —— 欧氏容差区分不了两者）。
            off_x = best_dx - (sa.cx - a.cx)
            off_y = best_dy - (sa.cy - a.cy)
            if abs(off_x) > BLOCK_WORD_OFF_DX or abs(off_y) > BLOCK_WORD_OFF_DY:
                continue
            cands.append((px + best_dx - sa.cx, py + best_dy - sa.cy))
        # 共识聚类：取候选位移中位数，只保留与中位数一致(±BLOCK_SHIFT_TOL)的词。
        # 单词 NCC 有重光栅化噪声（同内容 0.76~0.78），但**真匹配的偏移彼此
        # 一致**、噪声匹配（剩字/污点）偏移发散 —— 聚类天然去噪。
        hits: List[Tuple[float, float]] = []
        if cands:
            cmx = float(np.median([c[0] for c in cands]))
            cmy = float(np.median([c[1] for c in cands]))
            hits = [c for c in cands
                    if float(np.hypot(c[0] - cmx, c[1] - cmy)) <= BLOCK_SHIFT_TOL]
        # 落点在 added 父块内的，记 matched 防该 added 误报绿框
        for (dxw, dyw) in hits:
            bx = int(round(a.cx + dxw))
            by = int(round(a.cy + dyw))
            for (b, sb) in added_words:
                if sb.x <= bx < sb.x + sb.w and sb.y <= by < sb.y + sb.h:
                    hit_words.append((None, b))
                    break
        # ⚠️ 关键判据：**大部分词块**在对面找到才不算删除。阈值 0.5（DOC-1001
        #    实测：行内重排+插入时 REF 行 4 词中 3 命中率恰 0.5，0.6 会漏）；
        #    灰色地带不致误报 —— 后面还有墨迹残差闸门：残差低 → same/moved
        #    不报，残差高 → changed 成对报。真删除（电话号 10000000000000→
        #    100 00 00 00）词命中率远低于 0.5，仍走 removed。
        hit_ratio = len(hits) / max(1, len(sub_a))
        if hit_ratio >= 0.5:
            # 词位移中位数对齐 → 墨迹残差（双向未覆盖）分流：
            #   残差低 = 内容一致（整体平移/重切分）→ moved / same（不报红绿）
            #   残差高 = 行内真插入/删除 → changed（旧红+新绿成对）
            # （DOC-1001 rev15vs16 实测：REF 行内插入「生产日期」、地址行内插入
            #   「1栋101室」——行内前后位移不一致，中位对齐后残差仍高 → changed）
            med_dx = float(np.median([h[0] for h in hits]))
            med_dy = float(np.median([h[1] for h in hits]))
            missing = extra = 1.0
            ax2, ay2 = int(round(a.x + med_dx)), int(round(a.y + med_dy))
            tmpl = bwa[a.y:a.y + a.h, a.x:a.x + a.w]
            if (ax2 >= 0 and ay2 >= 0
                    and ay2 + a.h <= bwb.shape[0] and ax2 + a.w <= bwb.shape[1]):
                roi = bwb[ay2:ay2 + a.h, ax2:ax2 + a.w]
                om = tmpl < 128
                nm = roi < 128
                missing = float((om & ~nm).sum()) / max(1, int(om.sum()))
                extra = float((nm & ~om).sum()) / max(1, int(nm.sum()))
            a.matched = True
            if max(missing, extra) <= BLOCK_ALIGN_INK_TOL:
                # 内容一致：位移≥2px 报 moved（标蓝），原位 same（不报）
                if float(np.hypot(med_dx, med_dy)) >= 2.0:
                    a.kind = "moved"
                    a.dx, a.dy = med_dx, med_dy
                    a.ncc = 0.95
                else:
                    a.kind = "same"
                    a.dx = a.dy = 0.0
                for (_, b) in hit_words:
                    matched_b_ids.add(id(b))
                continue
            # 行内真改写 → changed：找与「对齐后位置」最重叠的新侧块配对
            ar = (ax2, ay2, ax2 + a.w, ay2 + a.h)
            b_sel, best_iou = None, 0.0
            for b in blocks_b:
                ix = max(0.0, min(ar[2], b.x + b.w) - max(ar[0], b.x))
                iy = max(0.0, min(ar[3], b.y + b.h) - max(ar[1], b.y))
                inter = ix * iy
                if inter <= 0:
                    continue
                iou = inter / float(a.w * a.h + b.w * b.h - inter)
                if iou > best_iou:
                    best_iou, b_sel = iou, b
            if b_sel is None and best_iou < 0.05:
                # ⚠️ 新版对齐位置可能没有独立行块（rev16 行距变化 → 行带切分
                #    并块，DOC-1001 实测 REF 行在新版不成块）——但词块命中率已
                #    ≥0.5 证明内容还在，不能回退 removed。用对齐位置的虚拟块
                #    配对（changed 输出只用旧块 rect，新块仅参与配对）。
                b_sel = Block(x=ax2, y=ay2, w=a.w, h=a.h, area=a.area,
                              cx=ax2 + a.w / 2.0, cy=ay2 + a.h / 2.0)
            changed_pairs.append((a, b_sel))
            matched_b_ids.add(id(b_sel))
            for (_, b) in hit_words:
                matched_b_ids.add(id(b))
            continue
        unresolved_a.append(a)
    # 未解决的旧块 + 未被匹配的新块 = 最终差异。
    # ⚠️ 词块确认的行内改写**不进 final**（不让 `_pair_changes` 重新贪心配对）
    #    —— 多行同时重排时 added 候选与多个 removed 距离都近，贪心顺序配对
    #    会「链式抢配」（removed[i] 抢走 added[i+1]），真正配对轮到时已被抢
    #    走 → changed 退化为 removed（DOC-1001 实测）。changed_pairs 由调用方
    #    直接作为已配对 changed 输出。
    final: List[Tuple[Block, Optional[Block]]] = []
    for a in unresolved_a:
        final.append((a, None))
    for b in added_b:
        if id(b) not in matched_b_ids:
            final.append((None, b))
    return final, changed_pairs


def _merge_moved(blocks_a: List[Block]) -> List[Block]:
    """S5 步骤③：位移向量相近(±BLOCK_SHIFT_TOL px)且相邻的 moved 块合并成一大块。

    相邻 = 垂直间距 < BLOCK_MERGE_GAP 且水平有交叠（同一段落的连续行）。
    合并后返回新的 moved 块列表（位移取平均），供输出用（避免把一段下移拆成
    十几条 moved）。
    """
    moved = [a for a in blocks_a if a.kind == "moved"]
    if not moved:
        return []
    moved = sorted(moved, key=lambda a: (round(a.dy / BLOCK_SHIFT_TOL),
                                         round(a.dx / BLOCK_SHIFT_TOL),
                                         a.y, a.x))
    merged: List[List[Block]] = []
    for a in moved:
        placed = False
        for grp in merged:
            ref = grp[0]
            if abs(a.dx - ref.dx) <= BLOCK_SHIFT_TOL and abs(a.dy - ref.dy) <= BLOCK_SHIFT_TOL:
                # 相邻：垂直间距小且水平交叠
                for m in grp:
                    ygap = min(a.y + a.h, m.y + m.h) - max(a.y, m.y)
                    xover = min(a.x + a.w, m.x + m.w) - max(a.x, m.x)
                    if ygap > -BLOCK_MERGE_GAP and xover > 0:
                        grp.append(a)
                        placed = True
                        break
                if placed:
                    break
        if not placed:
            merged.append([a])
    out: List[Block] = []
    for grp in merged:
        if len(grp) == 1:
            out.append(grp[0])
            continue
        x0 = min(g.x for g in grp); y0 = min(g.y for g in grp)
        x1 = max(g.x + g.w for g in grp); y1 = max(g.y + g.h for g in grp)
        dx = float(np.mean([g.dx for g in grp]))
        dy = float(np.mean([g.dy for g in grp]))
        ncc = float(np.mean([g.ncc for g in grp]))
        out.append(Block(x=x0, y=y0, w=x1 - x0, h=y1 - y0,
                         area=(x1 - x0) * (y1 - y0),
                         cx=(x0 + x1) / 2, cy=(y0 + y1) / 2,
                         matched=True, dx=dx, dy=dy, ncc=ncc, kind="moved"))
    return out


def _merge_adjacent_changed(changed: List[Dict[str, Any]]
                            ) -> List[Dict[str, Any]]:
    """S7 辅助：相邻的 changed 块（同一行带）合并，避免一行改动拆成多条。"""
    if len(changed) <= 1:
        return changed
    changed = sorted(changed, key=lambda p: (p["a"].y, p["a"].x))
    out = [changed[0]]
    for p in changed[1:]:
        last = out[-1]
        la, lb = last["a"], last["b"]
        a, b = p["a"], p["b"]
        # 同一行带（y 接近）且水平相邻或重叠
        if (abs(a.y - la.y) < max(a.h, la.h) * 2
                and min(a.x + a.w, la.x + la.w) - max(a.x, la.x) > -BLOCK_MERGE_GAP * 2):
            # 合并 rect
            x0 = min(a.x, la.x); y0 = min(a.y, la.y)
            x1 = max(a.x + a.w, la.x + la.w); y1 = max(a.y + a.h, la.y + la.h)
            la.x, la.y, la.w, la.h = x0, y0, x1 - x0, y1 - y0
            la.cx, la.cy = (x0 + x1) / 2, (y0 + y1) / 2
            la.area = (x1 - x0) * (y1 - y0)
            if b is not None and last["b"] is not None:
                bx0 = min(b.x, lb.x); by0 = min(b.y, lb.y)
                bx1 = max(b.x + b.w, lb.x + lb.w); by1 = max(b.y + b.h, lb.y + lb.h)
                lb.x, lb.y, lb.w, lb.h = bx0, by0, bx1 - bx0, by1 - by0
        else:
            out.append(p)
    return out


def _pair_changes(final: List[Tuple[Block, Optional[Block]]],
                  ) -> List[Dict[str, Any]]:
    """S7：成对关联。同一位置附近（重叠或邻近）的「旧独有」和「新独有」合并成
    一条「修改」（旧红+新绿），而不是各自独立报增删。

    ⚠️ 超大块（面积 > 某阈值，通常是「整行宽的大块」，标题区膨胀过度产物）不
    参与 changed 配对——它们应该被 S6 分层细化拆成词块，而不是整块配成 changed
    （否则会把标题区整片标成「修改」，语义错误）。
    """
    removed = [(a, None) for (a, b) in final if a is not None and b is None]
    added = [(None, b) for (a, b) in final if a is None and b is not None]
    paired = []
    consumed = set()
    for (a, _) in removed:
        # 超大块跳过配对（交由后续词块细化或直接报 removed）
        # ⚠️ 用 bbox 面积 w*h，不是 area（area 是墨迹像素数，整行大块大部分空白）
        if a.w * a.h > BLOCK_MAX_AREA // 4:
            continue
        best, best_score = None, -1.0
        for k, (_, b) in enumerate(added):
            if k in consumed:
                continue
            if b.w * b.h > BLOCK_MAX_AREA // 4:
                continue
            iou = _iou_blocks(a, b)
            # 邻近度：中心距离（归一化到块尺寸）。
            # ⚠️ 2026-10-19 收紧：除数原为 scale*2 —— 大块（scale=630px）相距
            # 734px 仍得 0.42 分（>0.3），removed 逐个「链式抢配」下一个 added，
            # 真正的配对对象轮到时已被抢走 → changed 退化为 removed+added
            # （DOC-1001 实测 12 处假删的最后一环）。dist < scale 才给分。
            dist = np.hypot(a.cx - b.cx, a.cy - b.cy)
            scale = max(a.w, a.h, b.w, b.h)
            score = max(iou, 1.0 - dist / (scale + 1e-6))
            if score > best_score:
                best_score, best = score, k
        # IoU≥0.3 或 中心距离<块尺寸（邻近）都算成对
        if best is not None and best_score > 0.3:
            consumed.add(best)
            paired.append({"kind": "changed", "a": a, "b": added[best][1]})
    singles = []
    for i, (a, _) in enumerate(removed):
        if not any(p["a"] is a for p in paired):
            singles.append({"kind": "removed", "a": a, "b": None})
    for k, (_, b) in enumerate(added):
        if k not in consumed:
            singles.append({"kind": "added", "a": None, "b": b})
    return paired + singles


def _detect_qr_region(gray: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """S8：在灰度图上检测二维码区域（半分辨率坐标），返回矩形列表。

    二维码用 QRCodeDetector 检测（可靠），后续对其解码比对内容，不做像素比对。
    """
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    rects: List[Tuple[int, int, int, int]] = []
    det = cv2.QRCodeDetector()
    try:
        ok, pts = det.detect(bw)
        if ok and pts is not None:
            for p in pts:
                xs = p[:, 0]; ys = p[:, 1]
                rects.append((int(xs.min()), int(ys.min()),
                              int(xs.max() - xs.min()), int(ys.max() - ys.min())))
    except cv2.error:
        pass
    return rects


def _compare_qr(ga: np.ndarray, gb: np.ndarray, pa: EmbedImage, pb: EmbedImage,
                page: int, rep: EmbedReport) -> None:
    """S8：二维码解码内容比对。两侧二维码解码内容不同 → 报 qr（低档，不改三色）。"""
    qa = _detect_qr_region(ga)
    qb = _detect_qr_region(gb)
    det = cv2.QRCodeDetector()
    for (ax, ay, aw, ah) in qa:
        ca, _, _ = det.detectAndDecode(ga[ay:ay + ah + 1, ax:ax + aw + 1])
        if not ca:
            continue
        for (bx, by, bw_, bh_) in qb:
            cb, _, _ = det.detectAndDecode(gb[by:by + bh_ + 1, bx:bx + bw_ + 1])
            if cb and cb != ca:
                r0 = pa.to_pt(ax * pa.pix_w / ga.shape[1], ay * pa.pix_h / ga.shape[0])
                r1 = pa.to_pt((ax + aw) * pa.pix_w / ga.shape[1],
                              (ay + ah) * pa.pix_h / ga.shape[0])
                rep.items.append(EmbedDiff(
                    page=page, rect=(r0[0], r0[1], r1[0], r1[1]),
                    kind="qr", conf="low", old_text=ca, new_text=cb,
                    note="二维码内容变化"))
                rep.stats.setdefault("qr", []).append(
                    {"page": page + 1, "old": ca, "new": cb})
                break


def _is_pure_image_pdf(path: str) -> bool:
    """判断一份 PDF 是否「纯图片型」：整份只有位图、无文字层、无矢量曲线。

    判据：每页都「无文字层（get_text 为空）且至少一张内嵌位图覆盖页面」。
    这类 PDF（BarTender 标签图等）应走块匹配路线 S1~S9，而不是文字层或 OCR。
    """
    try:
        d = fitz.open(path)
    except Exception:
        return False
    try:
        if d.page_count == 0:
            return False
        for i in range(d.page_count):
            pg = d[i]
            if pg.get_text().strip():
                return False                  # 有文字层 → 不是纯图片型
            imgs = pg.get_images(full=True)
            if not imgs:
                return False                  # 无内嵌位图 → 不是
        return True
    finally:
        d.close()


def block_diff_to_regions(rep: EmbedReport) -> List[Dict[str, Any]]:
    """把块匹配路线的 EmbedReport 转成 `graphic_regions` 的 regions 格式。

    映射：
      · changed → {"old_text":…, "new_text":…}（两侧都标，走 g_mod 黄色）
      · removed → {"old_text":…, "new_text":""}（旧侧红）
      · added   → {"old_text":"", "new_text":…}（新侧绿）
      · moved   → {"moved":True, "shift_src":…, "rect":…, "rect_shift":…}（浅蓝）
    把握档写进 note，供报告。
    """
    regions: List[Dict[str, Any]] = []
    for it in rep.items:
        if it.kind == "changed":
            regions.append({"page": it.page, "rect": it.rect,
                            "old_text": it.old_text or "〔修改〕",
                            "new_text": it.new_text or "〔修改〕",
                            "note": it.note})
        elif it.kind == "removed":
            regions.append({"page": it.page, "rect": it.rect,
                            "old_text": it.old_text or "〔删除〕", "new_text": "",
                            "note": it.note})
        elif it.kind == "added":
            regions.append({"page": it.page, "rect": it.rect,
                            "old_text": "", "new_text": it.new_text or "〔新增〕",
                            "note": it.note})
        elif it.kind == "moved":
            regions.append({"page": it.page, "rect": it.rect,
                            "moved": True, "shift_src": "old",
                            "rect_shift": it.rect, "note": it.note})
        elif it.kind in ("replaced", "qr"):
            regions.append({"page": it.page, "rect": it.rect,
                            "old_text": it.old_text, "new_text": it.new_text,
                            "note": it.note})
    return regions


def route_pdf(old_path: str, new_path: str) -> Tuple[str, EmbedReport]:
    """四类 PDF 路由入口（指令 G 三）。

    返回 (路由类型, EmbedReport)。路由类型：
      · "pure_image"   纯图片型（整页只有位图、无文字层）→ 块匹配路线 S1~S9
      · "text_vector"  文字层 / 转曲矢量 / 混合型 → 交给上层现有链路
    每页输出一条路由记录（页码、分类、走的路线）写进 report.stats["route"]。
    """
    old_img = _is_pure_image_pdf(old_path)
    new_img = _is_pure_image_pdf(new_path)
    if old_img and new_img:
        rep = block_match_diff(old_path, new_path)
        rep.stats["route"] = [{"page": p + 1, "class": "pure_image",
                               "route": "S1~S9 块匹配"}
                              for p in range(len(rep.stats.get("pages", [])))]
        return "pure_image", rep
    # 混合/文字/矢量：块匹配路线只对内嵌图片区做（若两侧都有内嵌图片且无文字层）
    # 这里暂不做混合型的区域级路由（体量大），返回空报告交上层现有链路。
    rep = EmbedReport()
    rep.stats = {"route": [{"page": 1, "class": "text/vector/mixed",
                            "route": "existing_text_layer", "note": "非纯图片型"}]}
    return "existing", rep




def block_match_diff(old_path: str, new_path: str) -> EmbedReport:
    """指令 G：纯图片 / 内嵌图片的「块匹配路线」主入口（S1~S9）。

    取代 `embedded_image_diff` 成为整页图片的主路线。字形级只对「已判变化的
    块」内部细化（S6），不再整页铺开。返回 EmbedReport（rect 为页面 pt）。
    """
    t0 = time.time()
    rep = EmbedReport()
    rep.stats = {"pages": [], "skipped_hash": 0, "replaced": 0, "resampled": 0,
                 "blocks_a": 0, "blocks_b": 0, "qr": [], "route": []}
    if not EMBED_ROUTE_ON:
        rep.stats["disabled"] = True
        return rep
    da = fitz.open(old_path)
    db = fitz.open(new_path)
    try:
        npages = min(da.page_count, db.page_count)
        for i in range(npages):
            _loc_checkpoint()     # 「停止对比」中止检查点（页级）
            page_stat: Dict[str, Any] = {"page": i + 1, "imgs": 0}
            old_imgs = _page_images(da, i)
            new_imgs = _page_images(db, i)
            page_stat["imgs"] = len(old_imgs)
            # 无内嵌图片的页（纯文字/矢量）交给上层，这里跳过
            if not old_imgs and not new_imgs:
                rep.stats["route"].append({"page": i + 1, "class": "text/vector",
                                           "route": "text_layer", "note": "无内嵌图片"})
                continue
            for (pa, pb) in _pair_images(old_imgs, new_imgs):
                # S1 精确同一性
                if pa and pb and EMBED_L1_HASH_ON and pa.stream_md5 == pb.stream_md5:
                    rep.stats["skipped_hash"] += 1
                    page_stat["hash_same"] = page_stat.get("hash_same", 0) + 1
                    continue
                if pa is None and pb is not None:
                    rep.items.append(EmbedDiff(page=i, rect=pb.bbox, kind="added",
                                               conf="high", note="新增图片"))
                    continue
                if pb is None and pa is not None:
                    rep.items.append(EmbedDiff(page=i, rect=pa.bbox, kind="removed",
                                               conf="high", note="删除图片"))
                    continue
                assert pa is not None and pb is not None
                # S2 方向归一化解码
                ga = _decode_gray(da, pa)
                gb = _decode_gray(db, pb)
                # S3 尺度归一化
                scale_hint = (pb.pix_h / max(pa.pix_h, 1) + pb.pix_w / max(pa.pix_w, 1)) / 2
                if abs(scale_hint - 1.0) >= (BLOCK_SAME_SCALE_HI - 1.0):
                    # 对称重采样到较低分辨率侧
                    if scale_hint > 1.0:
                        # pb 分辨率更高，把 pb 降到 pa 的网格
                        gb = _resample_block(gb, ga.shape)
                        s = 1.0
                    else:
                        # pa 分辨率更高，把 pa 降到 pb 的网格
                        ga = _resample_block(ga, gb.shape)
                        s = 1.0
                    rep.stats["resampled"] += 1
                else:
                    s = scale_hint
                # S4 整图配准
                state, s, tx, ty, inl = _register_block(ga, gb, s)
                page_stat.setdefault("register", []).append(
                    {"xref": pa.xref, "state": state, "scale": round(s, 5),
                     "tx": round(tx, 2), "ty": round(ty, 2), "inlier": round(inl, 4)})
                if state == "replaced":
                    rep.stats["replaced"] += 1
                    rep.items.append(EmbedDiff(page=i, rect=pa.bbox, kind="replaced",
                                               conf="high", note="整图被替换"))
                    continue
                # S5 行块切分 + 匹配
                blocks_a, bwa, _ = _extract_blocks(ga, BLOCK_HALF_SCALE)
                blocks_b, bwb, _ = _extract_blocks(gb, BLOCK_HALF_SCALE)
                rep.stats["blocks_a"] += len(blocks_a)
                rep.stats["blocks_b"] += len(blocks_b)
                # S8 二维码内容比对（先于块匹配，码区不做像素比对）
                _compare_qr(ga, gb, pa, pb, i, rep)
                _match_blocks(blocks_a, blocks_b, bwa, bwb, BLOCK_HALF_SCALE)
                # S6 分层细化（返回未配对差异 + 词块确认的行内改写对）
                final, pre_paired = _refine_unmatched(
                    blocks_a, blocks_b, bwa, bwb, BLOCK_HALF_SCALE)
                # S7 成对关联（词块确认的 changed 直接前置，不走重新配对）
                pairs = [{"kind": "changed", "a": pa_, "b": pb_}
                         for (pa_, pb_) in pre_paired]
                pairs += _pair_changes(final)
                # S5 步骤③：moved 块合并（位移相近且相邻 → 一大块）
                moved_merged = _merge_moved(blocks_a)
                # 坐标映射：半分辨率像素 → 原像素 → 页面 pt
                def to_pt(img: EmbedImage, px: float, py: float) -> Tuple[float, float]:
                    fx = img.pix_w / max(bwa.shape[1], 1)   # 半分辨率→原像素
                    fy = img.pix_h / max(bwa.shape[0], 1)
                    return img.to_pt(px * fx, py * fy)
                # 输出差异项（changed 先合并相邻）
                changed = [p for p in pairs if p["kind"] == "changed"]
                changed = _merge_adjacent_changed(changed)
                for p in changed:
                    a, b = p["a"], p["b"]
                    r0 = to_pt(pa, a.x, a.y); r1 = to_pt(pa, a.x + a.w, a.y + a.h)
                    rect = (r0[0], r0[1], r1[0], r1[1])
                    rep.items.append(EmbedDiff(
                        page=i, rect=rect, kind="changed", conf="mid",
                        note="块匹配：修改（旧%sx%s→新%sx%s）" % (
                            a.w, a.h, b.w if b else 0, b.h if b else 0)))
                for p in pairs:
                    if p["kind"] == "removed":
                        a = p["a"]
                        r0 = to_pt(pa, a.x, a.y); r1 = to_pt(pa, a.x + a.w, a.y + a.h)
                        # 边缘细条过滤：短边 <2pt 的框是表格线漂移
                        if min(r1[1] - r0[1], r1[0] - r0[0]) < 2.0:
                            continue
                        rep.items.append(EmbedDiff(
                            page=i, rect=(r0[0], r0[1], r1[0], r1[1]),
                            kind="removed", conf="mid",
                            note="块匹配：删除（NCC=%.2f）" % a.ncc))
                    elif p["kind"] == "added":
                        b = p["b"]
                        r0 = to_pt(pb, b.x, b.y); r1 = to_pt(pb, b.x + b.w, b.y + b.h)
                        if min(r1[1] - r0[1], r1[0] - r0[0]) < 2.0:
                            continue
                        rep.items.append(EmbedDiff(
                            page=i, rect=(r0[0], r0[1], r1[0], r1[1]),
                            kind="added", conf="mid",
                            note="块匹配：新增"))
                # moved 块（合并后输出）
                for a in moved_merged:
                    r0 = to_pt(pa, a.x, a.y); r1 = to_pt(pa, a.x + a.w, a.y + a.h)
                    shift_pt_x = a.dx * (pa.bbox[2] - pa.bbox[0]) / max(bwa.shape[1], 1)
                    shift_pt_y = a.dy * (pa.bbox[3] - pa.bbox[1]) / max(bwa.shape[0], 1)
                    if min(r1[1] - r0[1], r1[0] - r0[0]) < 2.0:
                        continue
                    rep.items.append(EmbedDiff(
                        page=i, rect=(r0[0], r0[1], r1[0], r1[1]),
                        kind="moved", conf="mid",
                        note="块匹配：移动 (%.1f,%.1f)pt" % (shift_pt_x, shift_pt_y)))
            rep.stats["pages"].append(page_stat)
        rep.stats["elapsed_s"] = round(time.time() - t0, 2)
    finally:
        da.close()
        db.close()
    rep.elapsed_s = time.time() - t0
    return rep
