# -*- coding: utf-8 -*-
"""
pdfdiff_office.py —— Office 输入归一化（Word / Excel → PDF）
==================================================================
用户 2026-10-21：「再给我加一下那个 Word 和 Excel，看看这个怎么个对比法。
我觉得也都是可以优先转成 PDF，就跟图片类导入之后转成 PDF 一样的……
优先选择速度和准确率高的。」

**为什么不「原生 Word 直接对比」**（先定路线再动手，2026-10-21 实测）
------------------------------------------------------------------
docx / xlsx 里**不存页码和坐标** —— Word 是打开时才排版分页的；拿 PyMuPDF
直接开 docx 只能得到 400×600 的假分页（实测过）。而本工具的标注框、新旧双栏
拖动、JSON 导出**全部靠页面坐标**。原生直比就得再写一套排版引擎 + 一套新
显示模式 —— 正是用户说的「写很多代码、加很多规则引擎」那种情况，被否。

⭐ 所以走 **Office 自己「另存为 PDF」**，理由是速度和准确率双高：
· Word / Excel 导出的 PDF **带完整文字层** → 走原生类字符级比对（逐字、
  坐标精确、不进 OCR 慢速通道），速度和直接比文本一个量级；
· 白得真实页面与坐标 → 标注、双栏拖动、导出 JSON 原样生效，一处不用改；
· 保真度最高：分页、纸型、打印区域**用文件里自己存的**（与项目3 同口径）。

**转换器从哪来**：项目3「PDF转换拼接 3.0」seq_core 的成熟实现（用户 2026-
10-21 明示「转换这边我这里有成品，可以用作参考」）。本模块只留 Word / Excel
一族的精简版（5.0 一次只比一对文件，用不上批量梯队），但**踩过的坑逐条保留**：

⚠️ AppleScript 的三条硬约束（2026-10-08/09 项目3 真实事故，别改回去）：
   ① `tell application "<应用名>"` 必须是**字面量**（名字拼进脚本），不能用
      变量 —— AppleScript 编译期认不出变量的词典，`file format format PDF`
      这些词全被当裸标识符，整段 -2741，一份都转不出来；
   ② Word 与 Excel 的语句**不能塞进同一个 tell 块**（一个 tell 只认一本词典）；
   ③ POSIX → HFS（`Macintosh HD:Users:…`）转换必须在 tell 块**外面**做 ——
      Office 的 `file name` 历来只认冒号分隔；`POSIX file` 不要求文件存在，
      产物路径（还不存在）照样转得出来。
   三步曲顺序（open → 等它真把文档交出来 → 存 → 关）**不能省中间那步**：
   Word 没在跑时 `open` 是跟着启动事件一起发出去的，立刻拿活动文档会拿到
   missing value，报错还指在 `d` 那行，看着像「Word 转失败了」。

⚠️ macOS 的「自动化」授权（TCC）只认**用户自己双击启动器**那个宿主
   （`启动PDF差异对比工具.command` 由 Terminal 跑 → 授权落在 Terminal）。
   从 IDE / 后台进程代开的实例拿不到授权，会一路 -10004「权限违例」。
   **这不是 bug、不用修** —— 报错翻译会把补救步骤说清楚（见 OFFICE_ERR_HINTS）。
   回归测试因此一律**注入假引擎**（engine= 参数），真引擎只做探测性自检。

⚠️ quarantine（「来自网络」的文件）会让 Word 以「受保护的视图」打开、
   每份都要人工点一次「启用编辑」。预处理办法：`/bin/cp` 出一份副本
   （实测 cp **不搬**扩展属性，ditto 会搬）再 `xattr -d` 双保险。
   动的永远是副本，原件一个字节不碰。

缓存：转换结果按**文件内容 sha1** 落系统临时目录复用（同一份 Word 第二次
对比不重转）。缓存在 /tmp，系统自己会清。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

# ==================================================================
# 第 1 节  认哪些扩展名、各归哪一族
# ==================================================================
# ⚠️ 与项目3 ⑩ 表同口径：加一种格式（比如 PPT）＝ 往这里加一行，
#    脚本生成那几节按 kind 分支即可，别处不用动。
OFFICE_KINDS: Dict[str, Tuple[str, ...]] = {
    "word": (".doc", ".docx", ".docm", ".rtf"),
    "excel": (".xls", ".xlsx", ".xlsm"),
}
_OFFICE_EXT: Dict[str, str] = {e: k for k, exts in OFFICE_KINDS.items() for e in exts}

# 每族在两个平台上「叫哪个应用」（AppleScript 字面量 / COM ProgID）
_OFFICE_APP = {
    "word": ("Microsoft Word", "Word.Application"),
    "excel": ("Microsoft Excel", "Excel.Application"),
}

CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfdiff_off_cache")
CONVERT_TIMEOUT = 300          # 单份上限；整批按份数给富余（首次拉应用偏慢）
QUARANTINE_XATTR = "com.apple.quarantine"


def is_office_file(path: str) -> bool:
    """是不是「本模块能接管的 Office 文档」（PDF / 图片返回 False，走原链路）。"""
    return os.path.splitext(str(path or ""))[1].lower() in _OFFICE_EXT


def office_kind(path: str) -> str:
    """→ "word" / "excel" / ""（不是 Office 文件）。"""
    return _OFFICE_EXT.get(os.path.splitext(str(path or ""))[1].lower(), "")


# ==================================================================
# 第 2 节  缓存（与 pdfdiff_img 同一套写法）
# ==================================================================
def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_cache_dir() -> None:
    """把缓存目录建出来 —— **不建就一定转不出来**。

    ⚠️ 2026-10-22 Windows 实机报错的原因之一：`_cache_path()` 把产物指到
       `<temp>/pdfdiff_off_cache/xxx.pdf`，但全模块从没创建过这个目录。
       Mac 上因为回归测试自己 makedirs 过而侥幸存在，Windows 新机器必然缺 ——
       Word 的 `ExportAsFixedFormat` 写到不存在的目录会直接失败，报出来的
       还是与「目录不存在」毫不相干的一句。
    """
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError:
        pass                       # 建不出来就让后面的 open 去报真话


def _cache_path(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = "".join(c for c in stem if c not in '/\\:*?"<>|')[:40] or "office"
    return os.path.join(CACHE_DIR, f"{stem}_{_sha1(path)[:16]}.pdf")


# ==================================================================
# 第 3 节  quarantine 预处理（macOS 专有；Windows 直通）
# ==================================================================
def has_quarantine(path: str) -> bool:
    """这份文件带 macOS「来自网络」标记吗？（只读 xattr，不起 Office）"""
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(["/usr/bin/xattr", "-p", QUARANTINE_XATTR, str(path)],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False                  # 查不出来 → 当没有 → 退回原件（保守）
    return r.returncode == 0 and bool((r.stdout or "").strip())


def stage_quarantine_free(src: str, tmpdir: str) -> Optional[str]:
    """复制一份**没有 quarantine** 的副本（`/bin/cp` 不搬扩展属性），失败 = None。"""
    dst = os.path.join(tmpdir, os.path.basename(src))
    n = 2
    while os.path.exists(dst):
        dst = os.path.join(tmpdir, f"{n}_{os.path.basename(src)}")
        n += 1
    try:
        r = subprocess.run(["/bin/cp", str(src), str(dst)],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not os.path.isfile(dst):
        return None
    try:
        subprocess.run(["/usr/bin/xattr", "-d", QUARANTINE_XATTR, str(dst)],
                       capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None if has_quarantine(dst) else dst


def prep_source(src: str, tmpdir: str) -> Tuple[str, str]:
    """这份原件**真正该喂给 Office** 的是谁？返回 (路径, 原因)；原因 "" = 原样用。

    **只读引用，绝不改动原件。** 命中 quarantine 才动，绝大多数文件都直通。
    """
    if not has_quarantine(src):
        return src, ""
    out = stage_quarantine_free(src, tmpdir)
    if out is None:
        return src, ""
    return out, "带「来自网络」标记，已复制一份干净副本给 Word（原件不动）"


# ==================================================================
# 第 4 节  macOS 通道 —— 一次 osascript、一个应用会话、整批一次授权
# ==================================================================
# ⚠️ 头尾两段**不含任何用户数据**（路径全从 argv 来），整段脚本当命令行参数传，
#    不用落地成临时文件。报错文本里可能带换行 / 制表符，neat() 压成空格，
#    保证「一行一份」解析得干净。
_MAC_OSA_HEADER = (
    "on run argv\n"
    "  set srcList to {}\n"
    "  set dstList to {}\n"
    "  repeat with i from 1 to (count of argv) by 2\n"
    "    set end of srcList to ((POSIX file (item i of argv)) as text)\n"
    "    set end of dstList to ((POSIX file (item (i + 1) of argv)) as text)\n"
    "  end repeat\n"
    "  set nFiles to (count of srcList)\n"
)
_MAC_OSA_FOOTER = (
    "  set AppleScript's text item delimiters to linefeed\n"
    "  return out as text\n"
    "end run\n"
    "\n"
    "on neat(t)\n"
    "  set AppleScript's text item delimiters to {return, linefeed, tab}\n"
    "  set parts to text items of t\n"
    "  set AppleScript's text item delimiters to \" \"\n"
    "  set flat to parts as text\n"
    "  set AppleScript's text item delimiters to \"\"\n"
    "  return flat\n"
    "end neat\n"
)

# 三步曲：open → 等文档真交出来 → 另存为 PDF → 关且不存。
# ⚠️ `set d to open file name …` 是 2026-10-09 的事故写法，别改回去。
# ⚠️ `activate` 在循环**外面**只来一次（每份都 activate 一次正是
#    「窗口乱跳 + 反复问授权」的来源，用户实测反馈过）。
_MAC_STEPS = {
    "word": (
        "set beforeCount to (count of documents)\n"
        "open srcHFS\n"
        "set d to missing value\n"
        "repeat 40 times\n"
        "  delay 0.25\n"
        "  try\n"
        "    if (count of documents) > beforeCount then "
        "set d to document (beforeCount + 1)\n"
        "  end try\n"
        "  if d is not missing value then exit repeat\n"
        "end repeat\n"
        "if d is missing value then error "
        "\"Word 没能打开这份文档（多半是弹了对话框在等人点）\"\n"
        "save as d file name dstHFS file format format PDF\n"
        "close d saving no"
    ),
    "excel": (
        "set beforeCount to (count of workbooks)\n"
        "open workbook workbook file name srcHFS\n"
        "set wb to missing value\n"
        "repeat 40 times\n"
        "  delay 0.25\n"
        "  try\n"
        "    if (count of workbooks) > beforeCount then "
        "set wb to workbook (beforeCount + 1)\n"
        "  end try\n"
        "  if wb is not missing value then exit repeat\n"
        "end repeat\n"
        "if wb is missing value then error "
        "\"Excel 没能打开这份工作簿（多半是弹了对话框在等人点）\"\n"
        "save as wb filename dstHFS file format PDF file format\n"
        "close wb saving no"
    ),
}


def build_mac_script(kind: str) -> str:
    """把某一族的三步曲拼成**一段整批 AppleScript**（app 名是拼进字面量的）。"""
    app = _OFFICE_APP[kind][0]
    steps = _MAC_STEPS[kind]
    lines = [
        _MAC_OSA_HEADER.rstrip("\n"),
        f'  tell application "{app}"',
        "    activate",
        "    set out to {}",
        "    repeat with i from 1 to nFiles",
        "      set srcHFS to item i of srcList",
        "      set dstHFS to item i of dstList",
        "      try",
    ]
    for ln in steps.split("\n"):
        lines.append("        " + ln)
    lines += [
        '        set end of out to (i as text) & "|OK"',
        "      on error errMsg number errNum",
        '        set end of out to (i as text) & "|ERR|" '
        '& (errNum as text) & "|" & my neat(errMsg)',
        "      end try",
        "    end repeat",
        "  end tell",
        _MAC_OSA_FOOTER.rstrip("\n"),
        "",
    ]
    return "\n".join(lines)


def _parse_mac_batch_output(text: str, n: int) -> List[Optional[str]]:
    """`N|OK` / `N|ERR|号|话` → 与 jobs 等长的失败原因（None = 转好了）。"""
    out: List[Optional[str]] = [None] * n
    for ln in (text or "").splitlines():
        parts = ln.split("|", 3)
        if len(parts) < 2:
            continue
        try:
            i = int(parts[0].strip()) - 1
        except ValueError:
            continue
        if not (0 <= i < n):
            continue
        if parts[1].strip().upper() == "OK":
            out[i] = None
        else:
            num = parts[2].strip() if len(parts) > 2 else ""
            msg = parts[3].strip() if len(parts) > 3 else ""
            out[i] = (f"{msg} ({num})" if num else msg) or "没有报错信息"
    return out


# ==================================================================
# 第 5 节  Windows 通道 —— PowerShell + COM（零第三方依赖）
# ==================================================================
# ⚠️ 不用 pywin32：5.0 的 Windows 包依赖清单里没有它（setup_env 的 PKGS
#    只有 numpy/pymupdf/tkinterdnd2/rapidocr/onnxruntime），不为转换加依赖。
#    PowerShell 是系统自带，`New-Object -ComObject` 直接拿 Office 的 COM 口。
# ⚠️ `DisplayAlerts = 0` 必须设：不关它，「是否保存」这类弹窗会把流程挂住。
# ⚠️ 绝不 `Quit()` 用户自己正开着的实例之外的东西？—— 这里用
#    `New-Object -ComObject` 起的是**新实例**（不是 GetActiveObject），
#    转完 Quit 只关我们自己拉起来的那个，不动用户已开的窗口。
# ⚠️ 路径**必须走环境变量**，不能用 `-Command <脚本> -src x -dst y`（2026-10-22
#    Windows 实机报错后改）：`-Command` 后面整段都当「要执行的命令」文本，
#    `-src` 会被 powershell.exe 自己当成未知参数，脚本里的 $src 拿到 $null ——
#    于是 Open($null) 失败，报出来的还是与真实原因无关的一句。
#    环境变量走 Unicode（os.environ 是 W 版 API），中文路径 / 中文文件名不会乱码，
#    比命令行参数稳一档。
# ⚠️ `[Console]::OutputEncoding` 也必设：不设时 PowerShell 按 OEM 码页（936）吐
#    中文，Python 用 utf-8 解会成乱码，报错文案就没法看了。
_WIN_PS = {
    "word": (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$src = $env:PDFDIFF_SRC\n"
        "$dst = $env:PDFDIFF_DST\n"
        "$app = New-Object -ComObject Word.Application\n"
        "$app.Visible = $false\n"
        "$app.DisplayAlerts = 0\n"
        "try {\n"
        "  $doc = $app.Documents.Open($src, $false, $true)\n"
        "  $doc.ExportAsFixedFormat($dst, 17)   # 17 = wdExportFormatPDF\n"
        "  $doc.Close(0)\n"
        "} finally {\n"
        "  $app.Quit()\n"
        "}\n"
    ),
    "excel": (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$src = $env:PDFDIFF_SRC\n"
        "$dst = $env:PDFDIFF_DST\n"
        "$app = New-Object -ComObject Excel.Application\n"
        "$app.Visible = $false\n"
        "$app.DisplayAlerts = $false\n"
        "try {\n"
        "  $wb = $app.Workbooks.Open($src)\n"
        "  $wb.ExportAsFixedFormat(0, $dst)     # 0 = xlTypePDF（整本，每表一页）\n"
        "  $wb.Close($false)\n"
        "} finally {\n"
        "  $app.Quit()\n"
        "}\n"
    ),
}


# ==================================================================
# 第 6 节  失败归类 —— Mac 与 Windows 共用同一套口径
# ==================================================================
# 两个平台的报错长得完全不一样（AppleScript 给 -10004，COM 给「拒绝访问」/
# HRESULT），但**要表达的是同一批事**。原始文本 → 稳定 code → 补救文案。
OFFICE_ERR_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("no_permission", ("-10004", "权限违例", "not permitted", "not authorized",
                       "permission denied", "拒绝访问", "access is denied",
                       "0x80070005")),
    ("syntax", ("-2741", "syntax error", "expected end of line", "cannot compile",
                "语法错误")),
    # 「叫起来了但没交出文档」：报错长得像脚本写错，其实是 Office 弹窗 / 没就绪。
    # 末两条是脚本里自己 error 出来的原话（等不到文档时），别删。
    ("no_doc", ("-2753", "没有定义", "-1728", "is not defined", "missing value",
                "can't get", "cannot get", "无效的引用", "没能打开")),
    ("no_app", ("无效的类字符串", "class not registered", "0x80040154",
                "found no application", "application isn't running",
                "找不到应用程序", "not installed", "没有安装")),
    ("busy", ("正在使用", "already open", "已打开", "被占用", "locked",
              "0x800a0046", "sharing violation")),
)

OFFICE_ERR_TITLE: Dict[str, str] = {
    "no_permission": "系统没放行「自动化控制」",
    "syntax": "给 Office 的那段脚本没编译过",
    "no_doc": "Office 起来了，但没把文档交出来",
    "no_app": "本机没找到这个 Office 程序",
    "busy": "文件正被别的程序占用",
    "other": "转换失败",
}

OFFICE_ERR_HINTS: Dict[Tuple[str, str], str] = {
    ("mac", "no_permission"): (
        "macOS 的「自动化」授权没放行这次操作。按顺序试，每步做完**重开一次本工具**：\n"
        "① 系统设置 → 隐私与安全性 → 自动化：给「终端」勾上允许控制 "
        "Microsoft Word / Excel；\n"
        "② 系统设置 → 隐私与安全性 → 完全磁盘访问权限：把 Microsoft Word / Excel 勾上。\n\n"
        "⚠️ 还有一种情况：本工具**不是你自己双击启动器启动的**（比如从别的程序里"
        "代开）。那种窗口拿不到这个授权，请关掉、自己双击「启动PDF差异对比工具」"
        "再跑一次。"),
    ("mac", "no_doc"): (
        "Word / Excel 被叫起来了，但**没把文档交给我们**（不是你的文件有问题）。\n"
        "按顺序试：\n"
        "① 切到 Word / Excel 窗口看一眼 —— 是不是弹着对话框在等人点"
        "（「启用编辑」/ 只读 / 修订 / 登录激活）；点掉它再重试；\n"
        "② 把你自己已经打开的 Word / Excel 窗口全关掉，再重试。"),
    ("mac", "no_app"): "这台电脑上没装 {app} —— 装好它再试。",
    ("mac", "busy"): "文件正被别的程序打开着（可能就是你自己的 {app}）。关掉那个窗口再试。",
    ("win", "no_permission"): (
        "Office 拒绝了这次自动化。常见两条：\n"
        "① Office 正卡在一个对话框上（宏 / 只读 / 修订）—— 手动打开这份文件看一眼；\n"
        "② 公司策略禁用了自动化 —— 问一下 IT 能不能给 Office 放行。"),
    ("win", "no_doc"): (
        "Office 起来了但没交出文档。多半是它卡在对话框上（只读 / 宏 / 修订），\n"
        "手动打开这份文件看一眼、点掉提示，再重试。"),
    ("win", "no_app"): "这台电脑上没装 {app}（或它注册坏了）—— 装好 / 修复它再试。",
    ("win", "busy"): "文件正被别的程序打开着（可能就是 {app} 自己）。关掉那个窗口再试。",
}


def classify_office_error(raw: str) -> str:
    """原始报错文本 → 稳定 code（两个平台同一套规则，命中第一条即返回）。"""
    low = (raw or "").lower()
    for code, pats in OFFICE_ERR_RULES:
        if any(p.lower() in low for p in pats):
            return code
    return "other"


def office_error_note(kind: str, raw: str) -> str:
    """统一的失败文案：抬头 + 补救说明 + 原始报错尾巴。两个平台的所有失败都从这里出。"""
    plat = "mac" if sys.platform == "darwin" else "win"
    app = _OFFICE_APP[kind][0 if plat == "mac" else 1]
    code = classify_office_error(raw)
    head = f"{app} 转换失败：{OFFICE_ERR_TITLE.get(code, OFFICE_ERR_TITLE['other'])}"
    hint = OFFICE_ERR_HINTS.get((plat, code), "")
    hint = hint.replace("{app}", app)
    tail = (raw or "").strip()[:300]
    parts = [head]
    if hint:
        parts.append(hint)
    if tail and tail not in hint:
        parts.append("原始报错：" + tail)
    return "\n".join(parts)


# ==================================================================
# 第 7 节  转换执行（engine 可注入 —— 回归一律走假引擎）
# ==================================================================
# engine 约定：engine(jobs: List[(src, dst)], kind: str) -> List[str]，
# 返回与 jobs 等长的失败原因（"" = 这份转好了）。注入假引擎后全程不起 Office，
# 真引擎只在用户实机跑（回归里即使 -10004 也只是探测，不当失败）。
Engine = Callable[[List[Tuple[str, str]], str], List[str]]


def _run_mac_many(jobs: List[Tuple[str, str]], kind: str) -> List[str]:
    n = len(jobs)
    if n == 0:
        return []
    app = _OFFICE_APP[kind][0]
    app_path = f"/Applications/{app}.app"
    if not os.path.isdir(app_path):
        return [office_error_note(kind, f"found no application {app}")] * n
    argv = ["osascript", "-e", build_mac_script(kind)]
    for src, dst in jobs:
        argv += [str(src), str(dst)]
    limit = max(30, CONVERT_TIMEOUT) + 15 * n
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return [(f"{app} 超过 {limit} 秒没转完 —— 多半是弹了对话框在等人点"
                 "（只读 / 有修订 / 首次运行的提示）。去 Office 窗口点掉它再重试。")] * n
    except OSError as exc:
        return [f"叫不动 {app}：{exc}"] * n

    raw = ((r.stderr or "") + (r.stdout or "")).strip()
    errs = _parse_mac_batch_output(r.stdout or "", n)
    if all(e is None for e in errs) and not any(
            (ln or "").strip() for ln in (r.stdout or "").splitlines()):
        # 一行结果都没回来（编译不过 / 授权被拒 / 应用直接退出）→ 整批报同一句
        return [office_error_note(kind, raw or "没有报错信息，osascript 直接退出了")] * n
    for i, (_src, dst) in enumerate(jobs):
        e = errs[i]
        if e is None and not os.path.isfile(dst):
            e = "没有报错信息，但也没生成 PDF"
        if e:
            errs[i] = office_error_note(kind, e)
    return ["" if e is None else e for e in errs]


def _run_win_many(jobs: List[Tuple[str, str]], kind: str) -> List[str]:
    """Windows：PowerShell + COM。逐份转（5.0 只有两份，起两次会话无所谓）。"""
    out: List[str] = []
    ps = _WIN_PS[kind]
    for src, dst in jobs:
        env = dict(os.environ)
        env["PDFDIFF_SRC"] = str(src)
        env["PDFDIFF_DST"] = str(dst)
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", ps],
                capture_output=True, encoding="utf-8", errors="replace",
                env=env, timeout=max(30, CONVERT_TIMEOUT))
        except subprocess.TimeoutExpired:
            out.append(office_error_note(kind, "转换超时（多半是弹了对话框在等人点）"))
            continue
        except OSError as exc:
            out.append(f"叫不动 PowerShell：{exc}")
            continue
        raw = ((r.stderr or "") + (r.stdout or "")).strip()
        if r.returncode != 0 or not os.path.isfile(dst):
            out.append(office_error_note(kind, raw or "没有报错信息，但也没生成 PDF"))
        else:
            out.append("")
    return out


def _default_engine(jobs: List[Tuple[str, str]], kind: str) -> List[str]:
    if sys.platform == "darwin":
        return _run_mac_many(jobs, kind)
    return _run_win_many(jobs, kind)


# ==================================================================
# 第 8 节  对外入口
# ==================================================================
def to_pdf(path: str, out_path: Optional[str] = None,
           engine: Optional[Engine] = None) -> str:
    """Word / Excel → PDF（sha1 缓存）。不是 Office 文件 → **原样返回**（零回归）。

    `engine` 注入给回归用（假引擎全程不起 Office）；默认走系统 Office。
    """
    kind = office_kind(path)
    if not kind:
        return path
    if not os.path.isfile(path):
        return path                     # 不存在 → 原样返回，由上层如实报错
    _ensure_cache_dir()
    out_path = out_path or _cache_path(path)
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return out_path

    with tempfile.TemporaryDirectory(prefix="pdfdiff_off_") as tmpdir:
        src, note = prep_source(path, tmpdir)   # noqa: F841  note 留给将来进状态栏
        fn = engine or _default_engine
        errs = fn([(src, out_path)], kind)
        err = errs[0] if errs else ""
        if err:
            # 引擎契约：返回最终文案（真引擎内部已过 office_error_note），照传。
            raise ValueError(err)
        if not os.path.isfile(out_path):
            raise ValueError(office_error_note(kind, "没有报错信息，但也没生成 PDF"))
    return out_path


def to_pdf_many(paths: List[str], engine: Optional[Engine] = None
                ) -> Dict[str, str]:
    """一批输入 → {原路径: PDF 路径}。同族的一次会话转完（只问一次授权）。"""
    out: Dict[str, str] = {}
    todo: List[Tuple[str, str, str]] = []           # (原路径, 产物, kind)
    _ensure_cache_dir()
    for p in paths:
        kind = office_kind(p)
        if not kind or not os.path.isfile(p):
            out[p] = p
            continue
        dst = _cache_path(p)
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            out[p] = dst
        else:
            todo.append((p, dst, kind))

    fn = engine or _default_engine
    for kind in ("word", "excel"):                   # 一族一段脚本，不能混 tell
        group = [t for t in todo if t[2] == kind]
        if not group:
            continue
        with tempfile.TemporaryDirectory(prefix="pdfdiff_off_") as tmpdir:
            jobs: List[Tuple[str, str]] = []
            for p, dst, _k in group:
                src, _note = prep_source(p, tmpdir)
                jobs.append((src, dst))
            errs = fn(jobs, kind)
            for i, (p, dst, _k) in enumerate(group):
                e = errs[i] if i < len(errs) else ""
                if not e and not os.path.isfile(dst):
                    e = "没有报错信息，但也没生成 PDF"
                if e:
                    # 引擎契约：返回**最终给用户看的文案**（真引擎内部已过
                    # office_error_note），这里照传，不二次包裹。
                    raise ValueError(e)
                out[p] = dst
    return out


def ensure_input(path: str, engine: Optional[Engine] = None) -> str:
    """输入归一化（一次到位）：Office → 转 PDF；图片 → 折单页 PDF；其余原样。

    ⚠️ **不做 `/Rotate` 归一化**（与 pdf_annotate._prepare 同口径，指令F 一-4）：
       旋转交给 `extract_rows` 的 `page.rotation_matrix` 正确处理，渲染用原始
       文件。详见 pdfdiff_img.ensure_pdf 的反证注释。
    """
    if not path or not os.path.isfile(path):
        return path
    if is_office_file(path):
        return to_pdf(path, engine=engine)
    import pdfdiff_img as img
    if img.is_image_file(path):
        return img.image_to_pdf(path)
    return path


def prepare(old_path: str, new_path: str, engine: Optional[Engine] = None
            ) -> Dict[str, object]:
    """一对输入的归一化结果：`{old_pdf, new_pdf, old_src, new_src, is_office, notes}`。

    「old_pdf / new_pdf」是转换后的 PDF —— 渲染 / 拖动 / 标注都该用它们；
    导出文件名仍用**原件**的名字（old_src / new_src）。

    ⭐ 两侧都是 Office 时走 `to_pdf_many` **一次会话转完**（一次授权询问、
      一次应用冷启动）—— 每份单独起一次 Word 正是项目3 用户反馈过的
      「每一份都要点一下允许」。
    """
    io_, in_ = is_office_file(old_path), is_office_file(new_path)
    notes: List[str] = []
    try:
        if io_ or in_:
            conv = to_pdf_many([old_path, new_path], engine=engine)
            o, n = conv[old_path], conv[new_path]
        else:
            o, n = old_path, new_path
        import pdfdiff_img as img
        if img.is_image_file(o):
            o = img.image_to_pdf(o)
        if img.is_image_file(n):
            n = img.image_to_pdf(n)
    except ValueError:
        raise                            # 报错文案已从 office_error_note 出，照传
    except Exception as e:
        raise ValueError(str(e))
    for flag, p, side in ((io_, old_path, "旧版"), (in_, new_path, "新版")):
        if flag:
            notes.append(f"{side}是 {office_kind(p)} 文档，已用 "
                         f"{_OFFICE_APP[office_kind(p)][0]} 转成 PDF"
                         "（带完整文字层，走逐字比对）")
    return {"old_pdf": o, "new_pdf": n, "old_src": old_path, "new_src": new_path,
            "is_office": (io_, in_), "notes": notes}


# ==================================================================
# 第 9 节  自检 self_test()
# ==================================================================
def self_test() -> int:
    """纯本地自检（不起 Office）。返回失败条数。"""
    import fitz

    ok = fail = 0

    def chk(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {label}")

    # —— 认扩展名 ——
    chk(is_office_file("a.docx") and office_kind("a.docx") == "word", "docx → word")
    chk(is_office_file("a.DOC") and office_kind("a.DOC") == "word", "doc → word")
    chk(is_office_file("b.xlsx") and office_kind("b.xlsx") == "excel", "xlsx → excel")
    chk(is_office_file("b.xlsm") and office_kind("b.xlsm") == "excel", "xlsm → excel")
    chk(not is_office_file("c.pdf") and office_kind("c.pdf") == "", "pdf 不接管")
    chk(not is_office_file("c.png"), "png 不接管")
    chk(not is_office_file("noext"), "无扩展名不接管")

    # —— AppleScript 生成：字面量 tell、三步曲顺序、HFS 在 tell 外 ——
    s = build_mac_script("word")
    chk('tell application "Microsoft Word"' in s, "Word 脚本是字面量 tell")
    chk("open srcHFS" in s and "file format format PDF" in s
        and "close d saving no" in s, "Word 三步曲齐全")
    chk(s.index("POSIX file") < s.index('tell application'), "HFS 转换在 tell 外")
    se = build_mac_script("excel")
    chk('tell application "Microsoft Excel"' in se
        and "open workbook workbook file name srcHFS" in se
        and "file format PDF file format" in se, "Excel 三步曲齐全")

    # —— 批量结果解析 ——
    chk(_parse_mac_batch_output("1|OK\n2|ERR|-10004|权限违例。", 2)
        == [None, "权限违例。 (-10004)"], "批量结果解析")
    chk(_parse_mac_batch_output("", 1) == [None], "空输出不报错")

    # —— 失败归类 ——
    chk(classify_office_error("(-10004)") == "no_permission", "-10004 → no_permission")
    chk(classify_office_error("发生权限违例") == "no_permission", "权限违例 → no_permission")
    chk(classify_office_error("拒绝访问 0x80070005") == "no_permission", "COM 拒绝访问")
    chk(classify_office_error("没能打开这份文档") == "no_doc", "没能打开 → no_doc")
    note = office_error_note("word", "(-10004) 权限违例。")
    chk("自动化" in note and "双击" in note and "终端" in note,
        "no_permission 文案含补救步骤")

    # —— 假引擎：转换 / 缓存 / 原件不动 ——
    tmpd = tempfile.mkdtemp(prefix="pdfdiff_off_t_")
    src = os.path.join(tmpd, "甲.docx")
    with open(src, "wb") as fh:
        fh.write(b"PK\x03\x04 fake docx bytes")
    calls: List[int] = []

    def fake_engine(jobs, kind):
        calls.append(len(jobs))
        errs = []
        for _s, d in jobs:
            doc = fitz.open()
            pg = doc.new_page(width=595, height=842)
            pg.insert_text((72, 72), "hello office", fontsize=12)
            os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
            doc.save(d)
            doc.close()
            errs.append("")
        return errs

    pdf1 = to_pdf(src, engine=fake_engine)
    chk(os.path.isfile(pdf1) and pdf1.endswith(".pdf"), "假引擎转出 PDF")
    n_calls = len(calls)
    pdf2 = to_pdf(src, engine=fake_engine)
    chk(pdf2 == pdf1 and len(calls) == n_calls, "二次调用走缓存不再转")
    chk(open(src, "rb").read()[:2] == b"PK", "原件一个字节没动")
    d = fitz.open(pdf1)
    chk(d.page_count == 1 and len(d[0].get_text().strip()) > 0, "产物有文字层")
    d.close()

    # —— ensure_input 三分支 ——
    img = os.path.join(tmpd, "x.png")
    import struct, zlib                    # 手造最小 PNG
    def _mini_png(p):
        sig = b"\x89PNG\r\n\x1a\n"
        def chunk(tag, data):
            c = tag + data
            return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        idat = zlib.compress(b"\x00\xff\x00\x00")
        with open(p, "wb") as fh:
            fh.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
                     + chunk(b"IEND", b""))
    _mini_png(img)
    chk(ensure_input(img) != img and ensure_input(img).endswith(".pdf"),
        "图片走折页")
    pdf_src = os.path.join(tmpd, "y.pdf")
    doc = fitz.open(); doc.new_page(); doc.save(pdf_src); doc.close()
    chk(ensure_input(pdf_src) == pdf_src, "PDF 原样返回")

    # —— prepare 双侧 ——
    pr = prepare(src, pdf_src, engine=fake_engine)
    chk(pr["old_pdf"] == pdf1 and pr["new_pdf"] == pdf_src, "prepare 一侧转一侧直通")
    chk(pr["is_office"] == (True, False) and pr["notes"], "notes 记了转换")
    chk(pr["old_src"] == src, "原件名保留给导出用")

    shutil.rmtree(tmpd, ignore_errors=True)
    print(f"pdfdiff_office 自检：{ok} 通过 / {fail} 失败")
    return fail


if __name__ == "__main__":
    sys.exit(1 if self_test() else 0)
