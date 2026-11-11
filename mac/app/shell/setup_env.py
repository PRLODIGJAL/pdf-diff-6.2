#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_env.py —— 环境自检与启动器（Windows / macOS 共用这一份）。

双击 .bat / .command 会调到这里，由它统一负责三件事：
  1. 检查工具自带的运行时（runtime/）还能不能用；
  2. 不能用就现场修复（重新识别：修权限、去隔离属性、补依赖）；
  3. 就绪后用 runtime 里的解释器拉起界面。

**从 6.2 起用内嵌运行时（runtime/），不再依赖系统 Python。**
整个文件夹拷到任何电脑、任何位置，双击「启动PDF差异对比工具」就能用。
6.1 及以前是 `.venv` 模式，那套东西记着创建时那台机器的 Python 绝对路径，
换台电脑必废 —— 这也是 6.2 换掉它的唯一原因。

**打印格式统一走 bootui.py**（2026-10-23 抽出来，各项目逐字节同一份）：
标记只有 `[ OK ] [ !! ] [ -- ] [ .. ]` 四种，分节只用 `-- 名字 ----` 一行，
宽度固定 62 列。**别在这里直接 print** —— 一直接 print，几个项目的启动界面
就各长各的，那正是使用者抱怨的「文字部分千奇百怪」。

为什么比对过程完全不联网
------------------------
本工具在「比对阶段」**完全离线**——两份 PDF 的内容绝不联网、绝不上传。
「补依赖」这一步理论上可以走本地 wheels/ 离线完成（随包带着），
实在不全才回退联网。数据安全边界没变：只有「装依赖」可能联网，比对数据永不外传。

用法
----
    python setup_env.py            自检 → 按需修复 → 启动（正常双击走这条）
    python setup_env.py --check    只自检并打印结论，不修复、不启动
    python setup_env.py --setup    只把环境备好（重新识别），不启动界面
    python setup_env.py --rebuild  同 --setup，强制重走一次修复与补依赖
"""
import os
import shutil
import subprocess
import sys
from typing import Optional, Tuple

import bootui as B

# 2026-11-02 目录重构：本文件从项目根挪进 `app/shell/`。
# 这里必须分清两个概念，别再混用：
#   HERE  = 本文件所在目录（app/shell）—— 只用来找同目录的 bootui.py
#   ROOT  = 项目根 —— runtime、wheels、界面入口都在根那边
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RUNTIME = os.path.join(ROOT, "runtime")
WHEELS = os.path.join(ROOT, "wheels")
APP = os.path.join(ROOT, "app", "ui", "diff_app5.py")

TOOL_NAME = "PDF差异对比工具"
# ⚠️ 出包时改这一行：控制台抬头打印的就是它。多个版本同时放在一台机器上时，
#    抬头 + 下面的「位置」行是唯一能立刻分清「我现在跑的是哪个文件夹」的地方。
TOOL_VERSION = "6.2"

# 依赖清单：numpy + pymupdf + 拖放库 + RapidOCR（统一 OCR 引擎，取代 Mac Vision
# 与 Windows Tesseract 的不一致）。这些依赖在打包阶段已装进 runtime/，
# 正常情况零动作；wheels/ 是杀软误删后的离线兜底。
PKGS = ["numpy", "pymupdf", "tkinterdnd2", "rapidocr", "onnxruntime"]

IS_WIN = os.name == "nt"


def env_fix_hint() -> str:
    """环境坏了该怎么做 —— 6.2 起只有一个启动器，不再让人去找 `.bat`。

    启动器本身就带完整体检与自愈，所以这里的指引只能是「再双击一次」。
    """
    return "再双击一次启动器，它会把运行时重新识别一遍"


# ONNX Runtime 1.31 的预编译 Windows 包硬性要求 VC++ 运行库 >= 14.40
# （官方 release note 原话：prebuilt Windows packages now require VC++ Runtime
#   version >= 14.40 instead of 14.38；低于此版本的典型现象是 import 时
#   "DLL load failed" 或初始化阶段直接崩）。
# 14.40 对应 VS 2022 17.10+；VS 2019 只到 14.29，差得远。
VC_MIN = (14, 40)
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

# 依赖存在性探测：用 importlib.util.find_spec 只查「装没装」，**不真正 import**。
# 为什么不能直接 import onnxruntime 判断：载入 .pyd 会触发 DLL 解析，若 VC 运行库
# 太旧，import 必然失败 → 脚本会误判为「依赖没装」→ 每次双击都重装一遍。
_PROBE = (
    "import importlib.util as u;"
    "miss=[n for n in %r if u.find_spec(n) is None];"
    "print('MISS='+','.join(miss))"
)


def vc_runtime() -> Optional[Tuple[int, int]]:
    """读本机 x64 的 VC++ 运行库版本，返回 (major, minor)；读不到返回 None。

    读的是 VC++ 2015-2022 Redistributable 官方注册表键的 x64 子键。加
    KEY_WOW64_64KEY 是为了让 32 位 Python 也能读到原生视图的键（默认会被
    重定向到 WOW6432Node，那里没有这条记录）。
    """
    if not IS_WIN:
        return None
    try:
        import winreg  # 仅 Windows 提供

        key = r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            ver, _ = winreg.QueryValueEx(k, "Version")  # 形如 "v14.40.33807.00"
        parts = str(ver).lstrip("vV").split(".")
        return (int(parts[0]), int(parts[1]))
    except (ImportError, OSError, ValueError, IndexError):
        return None


def vc_row() -> bool:
    """VC++ 运行库体检，打一行结论。返回 True = 满足 ONNX Runtime 要求。

    ⭐ 这一步必须在「补依赖」**之前**报出来：运行库旧的时候，依赖补得再全
       也加载不了 OCR，用户会白等几分钟再撞同一堵墙。
    """
    if not IS_WIN:
        B.item(B.SKIP, "运行库", "不是 Windows，这一项不适用")
        return True
    v = vc_runtime()
    if v is None:
        B.item(B.SKIP, "运行库", "读不到版本，跳过检查（不影响后面的安装）")
        return True
    if v >= VC_MIN:
        B.item(B.OK, "运行库", "VC++ %d.%d（OCR 引擎要求 %d.%d 以上）"
               % (v[0], v[1], VC_MIN[0], VC_MIN[1]))
        return True
    B.item(B.FAIL, "运行库", "VC++ %d.%d，低于要求的 %d.%d —— OCR 引擎会加载失败"
           % (v[0], v[1], VC_MIN[0], VC_MIN[1]))
    B.note("影响：转曲 / 图片区的文字识别静默失效，表现为「该标出来的差异没标」，"
           "程序本身不报错。")
    B.note("修复：装微软官方运行库，装完必须重启电脑（文件被占用时不会替换）：")
    B.raw("    " + VC_REDIST_URL)
    return False


def runtime_python() -> str:
    """runtime 里的解释器路径（Windows 在根目录，其余在 bin 下）。"""
    if IS_WIN:
        return os.path.join(RUNTIME, "python.exe")
    return os.path.join(RUNTIME, "bin", "python3")


def run(cmd, **kw) -> int:
    """跑子进程，输出直接接当前终端（补依赖时要能看到进度）。"""
    try:
        return subprocess.run(cmd, **kw).returncode
    except OSError as e:
        B.note("调用失败：%s" % e)
        return 1


def run_out(cmd, **kw):
    """跑子进程并把输出接回来，返回 (成功?, 文本)。用于探测，不用于装包。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", **kw)
    except OSError as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def missing_pkgs(py: str) -> list:
    """这个 runtime 里还缺哪些包。只查「装没装」，不真正 import。"""
    ok, out = run_out([py, "-c", _PROBE % (PKGS,)])
    if not ok:
        # 解释器自己都跑不起来（runtime 不完整 / 架构不对）：当成「全都缺」，
        # 让后面的修复流程去处理，而不是在这里猜。
        return list(PKGS)
    for ln in out.splitlines():
        if ln.startswith("MISS="):
            return [x for x in ln[5:].strip().split(",") if x]
    return list(PKGS)


def version_code() -> str:
    """读各依赖版本用的小脚本（包名从 PKGS 派生，不写死）。"""
    return ("import importlib.metadata as md\n"
            "for n in %r:\n"
            "    try:\n"
            "        print(f'{n} {md.version(n)}')\n"
            "    except Exception:\n"
            "        print(n)" % (PKGS,))


def dep_versions(py: str) -> str:
    """「依赖」那一行的值：`numpy 2.5.4 · pymupdf 1.28.2 · …`。

    读不出版本时只列包名 —— 宁可少一样信息，也不要让这一行报错。
    """
    ok, out = run_out([py, "-c", version_code()])
    if not ok:
        return "、".join(PKGS)
    items = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return " · ".join(items) or "、".join(PKGS)


# ── 修复运行时（「重新识别」）────────────────────────────────────────────────
def fix_runtime() -> list:
    """修执行权限、去隔离属性、清旧字节码。返回修了什么的清单。

    这就是「重新识别」的核心：**整包拷到新位置后，这三样都可能不对** ——
    拷到 FAT/exFAT 盘会丢执行位；从网上/邮件下载会带 quarantine 属性；
    旧 __pycache__ 可能跟这个 Python 版本不兼容。
    """
    changed = []

    # 1. 修执行权限（FAT/exFAT 拷贝会丢 +x）
    if not IS_WIN:
        bin_dir = os.path.join(RUNTIME, "bin")
        if os.path.isdir(bin_dir):
            for name in os.listdir(bin_dir):
                p = os.path.join(bin_dir, name)
                if os.path.isfile(p) and not os.access(p, os.X_OK):
                    try:
                        os.chmod(p, os.stat(p).st_mode | 0o755)
                        changed.append("+" + name)
                    except OSError:
                        pass

    # 2. 去 macOS 隔离属性（从网上下载的会带 com.apple.quarantine）
    if sys.platform == "darwin":
        ok, _ = run_out(["xattr", "-rd", "com.apple.quarantine", RUNTIME])
        if ok:
            changed.append("去隔离")

    # 3. 清 __pycache__（旧字节码可能跟新 Python 不兼容）
    pyc_count = 0
    for dirpath, dirnames, _files in os.walk(os.path.join(ROOT, "app")):
        if "__pycache__" in dirnames:
            shutil.rmtree(os.path.join(dirpath, "__pycache__"), ignore_errors=True)
            pyc_count += 1
    if pyc_count:
        changed.append("清缓存")

    return changed


def install_deps(py: str, need: list) -> bool:
    """补依赖：先试本地 wheels（离线），再回退联网。返回补好没有。"""
    wheels = []
    if os.path.isdir(WHEELS):
        wheels = [os.path.join(WHEELS, f)
                  for f in os.listdir(WHEELS) if f.endswith(".whl")]
    if wheels:
        B.step("正在从本地 wheels 补 %s（不联网）" % "、".join(need))
        if run([py, "-m", "pip", "install", "--disable-pip-version-check",
                "--no-input", "-q", "--no-index",
                "--find-links", WHEELS] + need) == 0:
            B.item(B.OK, "依赖", "已从本地 wheels 补齐（没有联网）")
            return True
        B.note("本地 wheels 补不全，改用联网…")
    # 联网兜底：只用于「补依赖」，比对数据绝不联网。
    B.step("正在联网补依赖：%s" % "、".join(need))
    B.note("首次约 1～3 分钟，请勿关窗口")
    if run([py, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "-q", "--upgrade"] + need) != 0:
        B.item(B.FAIL, "依赖", "联网也没补上 —— 检查网络后重试")
        return False
    B.item(B.OK, "依赖", "已补齐")
    return True


def probe_onnx(py: str) -> bool:
    """真加载一次 onnxruntime。

    「装上了」≠「能加载」—— DLL 缺失只有真正 import 才暴露，而它一旦失败，
    主程序会静默降级成「OCR 全部失效但界面正常」，用户只会觉得「识别不准」。
    这里把它提前变成一句明确的报错。
    """
    if not IS_WIN:
        return True
    B.step("试加载 OCR 引擎 onnxruntime")
    ok, out = run_out([py, "-c", "import onnxruntime as o; print(o.__version__)"])
    if ok:
        B.item(B.OK, "依赖", "OCR 引擎 onnxruntime %s 加载正常" % out.strip())
        return True
    B.item(B.FAIL, "依赖", "onnxruntime 装上了但加载不了 —— 基本都是 VC++ 运行库太低")
    tail = out.strip().splitlines()[-3:]
    if tail:
        B.note("原始报错：")
        for line in tail:
            B.note(line)
    B.note("修复：装下面这个运行库，装完必须重启电脑：")
    B.raw("    " + VC_REDIST_URL)
    return False


# ── 运行时可用性探测 ────────────────────────────────────────────────────────
def probe_runtime(py: str) -> Tuple[bool, str]:
    """runtime 里的解释器能不能跑、依赖全不全、它是不是**本包**的运行时。

    最后一条才是关键：`sys.prefix` 必须指回包内 `runtime/`。
    指到别处说明这个包被搬过而解释器没跟上，跑下去会出现
    「一半依赖来自旧机器」这种最难查的故障。
    """
    if not py or not os.path.lexists(py):
        return False, "解释器不存在"
    code = ("import importlib.util as u, sys\n"
            "miss=[n for n in %r if u.find_spec(n) is None]\n"
            "print('MISS='+','.join(miss))\n"
            "print('PREFIX='+sys.prefix)\n" % (PKGS,))
    ok, out = run_out([py, "-c", code])
    if not ok:
        first = next((ln.strip() for ln in out.splitlines()
                      if ln.strip() and "PREFIX=" not in ln), "")
        return False, "解释器跑不起来" + ("（%s）" % first[:110] if first else "")
    miss = ""
    prefix = ""
    for ln in out.splitlines():
        if ln.startswith("MISS="):
            miss = ln[5:].strip()
        elif ln.startswith("PREFIX="):
            prefix = ln[7:].strip()
    real = os.path.normcase(os.path.realpath(prefix or "/"))
    want = os.path.normcase(os.path.realpath(RUNTIME))
    if real != want:
        return False, "运行时路径不对（%s）" % (prefix or "?")
    if miss:
        return False, "缺依赖：%s" % "、".join(x for x in miss.split(",") if x)
    return True, "就绪"


_TK_PROBE = ("import tkinter\n"
             "r = tkinter.Tk()\n"
             "try:\n"
             "    r.attributes('-alpha', 0.0)\n"
             "except Exception:\n"
             "    pass\n"
             "r.withdraw()\n"
             "r.destroy()\n"
             "print('TKPROBE=ok')\n")


def probe_tk(py: str) -> Tuple[bool, str]:
    """这个解释器能不能把 Tk 真起起来（不是只看 import 成功）。"""
    ok, out = run_out([py, "-c", _TK_PROBE])
    if ok and "TKPROBE=ok" in out:
        return True, "能起 Tk"
    for ln in out.strip().splitlines():
        ln = ln.strip()
        if "init.tcl" in ln or "TclError" in ln or "No module named" in ln:
            return False, ln[:130]
    return False, "Tk 起不来"


def probe_modules(py: str) -> Tuple[bool, str]:
    """程序文件齐不齐 —— 少一个 .py 也算不可用，比起到界面里才炸，不如现在说。"""
    dirs = [os.path.join(ROOT, "app")]
    dirs += [os.path.join(ROOT, "app", d) for d in ("ui", "judge", "engine", "shell")]
    mods = ("paths", "bootui", "diff_app5", "pdfdiff_locate", "pdf_annotate")
    code = ("import sys\n"
            "sys.path[:0] = %r\n"
            "import %s\n"
            "print('MODS=ok')\n" % (dirs, ", ".join(mods)))
    ok, out = run_out([py, "-c", code], cwd=ROOT)
    if ok and "MODS=ok" in out:
        return True, "齐（%s）" % "、".join(m + ".py" for m in mods)
    miss = next((ln.strip() for ln in out.strip().splitlines()
                 if "Error" in ln), "")
    if not miss:
        miss = next((ln.strip() for ln in out.strip().splitlines() if ln.strip()),
                    "读不出来")
    return False, "缺东西 —— " + miss[:110]


def ready_rows(py: str, show_vc: bool = True) -> bool:
    """环境就绪时的固定几行。返回「程序文件在不在」。

    ⚠️ 「运行库」夹在「运行环境」和「依赖」中间，是**故意**的：
       它是这套工具唯一一个「装上了也未必能加载」的环节（OCR 引擎），
       紧跟在运行环境后面，出问题时第一眼就能看到，不用往下翻。
    """
    B.item(B.OK, "运行环境", os.path.relpath(RUNTIME, ROOT))
    if show_vc:
        vc_row()
    B.item(B.OK, "依赖", dep_versions(py))
    tk_ok, tk_why = probe_tk(py)
    B.item(B.OK if tk_ok else B.FAIL, "界面库", tk_why)
    mods_ok, mods_why = probe_modules(py)
    B.item(B.OK if mods_ok else B.FAIL, "程序文件", mods_why)
    return mods_ok


def ensure_ready(force: bool = False) -> str:
    """确保运行时可用，返回解释器路径（失败返回空串）。

    「重新识别」的完整流程：探测 → 修复 → 补依赖 → 复核。

    ⚠️ 行序是**故意**的：运行环境 → 运行库 → 依赖。
       「运行库」必须在「补依赖」之前报出来：它不行的话，依赖补得再全
       也加载不了 OCR，用户会白等几分钟再撞同一堵墙。
    """
    py = runtime_python()

    if not force:
        ok, why = probe_runtime(py)
        if ok:
            tk_ok, tk_why = probe_tk(py)
            if tk_ok:
                if ready_rows(py):
                    return py
                B.note("这个文件夹可能没拷全 —— 重新拷一份完整的交付包再试。")
                return ""
            B.item(B.FAIL, "界面库", tk_why)
            B.note("需要重新识别一次（修权限、补依赖）。")
        elif os.path.lexists(py):
            B.item(B.FAIL, "运行环境", why)
            B.note("需要重新识别一次（修权限、补依赖）。")
        else:
            B.item(B.SKIP, "运行环境", "还没就绪，这一次先把运行时备好")
        B.section("", "重新识别")

    # 1) 修权限 / 属性 / 缓存
    B.step("检查运行时权限与属性")
    changed = fix_runtime()
    if changed:
        B.item(B.OK, "修复", "已修：" + "、".join(changed))
    else:
        B.item(B.OK, "修复", "无需修复")

    # 2) 运行时在不在
    if not os.path.lexists(py):
        B.item(B.FAIL, "运行环境", "找不到 runtime 里的 Python")
        B.note("确认这个文件夹是完整的（runtime/ 目录要和启动器在同一层）")
        return ""

    # 3) 依赖（先看运行库，再补包）
    vc_ok = vc_row()
    need = missing_pkgs(py)
    if need:
        B.item(B.SKIP, "依赖", "缺 %s，开始补" % "、".join(need))
        if not install_deps(py, need):
            return ""
    else:
        B.item(B.OK, "依赖", dep_versions(py))

    # 4) ONNX 真加载（Windows 才有意义）
    if vc_ok and not probe_onnx(py):
        return ""

    # 5) 复核（运行库在上面已经报过一次，这里不再重复打）
    ok, why = probe_runtime(py)
    if not ok:
        B.item(B.FAIL, "可用性", "修完仍然不可用 —— %s" % why)
        return ""
    tk_ok, tk_why = probe_tk(py)
    mods_ok = ready_rows(py, show_vc=False)
    return py if (tk_ok and mods_ok) else ""


_TCL_PROBE = (
    "import sys,os,glob\n"
    "b=sys.base_prefix\n"
    "def pick(pat):\n"
    "    for d in sorted(glob.glob(os.path.join(b,pat))):\n"
    "        if os.path.isfile(os.path.join(d,'init.tcl')): return d\n"
    "    return ''\n"
    "print(pick('lib/tcl*') or pick('tcl/tcl*') or '-')\n"
    "print(pick('lib/tk*') or pick('tcl/tk*') or '-')\n"
)


def tcl_env(py: str) -> dict:
    """找出 Tcl/Tk 的库目录并交给子进程（找不到就什么都不做）。

    ⚠️ 2026-11-02 踩到的坑：界面入口从「项目根」挪进 `app/ui/` 之后，
       `python app/ui/diff_app5.py` 起不来，报 `Cannot find a usable init.tcl`；
       同一份代码用 `python -m diff_app5` 或 `-c` 起就一切正常 ——
       区别只在 argv[0] / sys.path[0]，Tcl 据此推算库目录，推算落空整个 Tk 就起不来。
       跟它猜不划算，直接把目录告诉它。

    内嵌 runtime 自带 tcl/tk（runtime/lib/tcl9.x），正常也能自己推对；
    这一步是**保险**：推不对时兜住，推对了设成同一个值也无害。
    找到含 `init.tcl` 的那个才算数，**不硬编码版本号**；一个都找不到就不设，
    宁可让 Tk 自己去试，也不塞一个错路径进去把它彻底弄坏。
    """
    try:
        out = subprocess.run([py, "-c", _TCL_PROBE], capture_output=True,
                             text=True, timeout=20).stdout.split()
    except Exception:
        return {}
    out = (out + ["-", "-"])[:2]
    env = {}
    if out[0] != "-" and os.path.isdir(out[0]):
        env["TCL_LIBRARY"] = out[0]
    if out[1] != "-" and os.path.isdir(out[1]):
        env["TK_LIBRARY"] = out[1]
    return env


def launch(py: str) -> int:
    """用运行时的解释器拉起界面。"""
    if not os.path.exists(APP):
        B.end(B.FAIL, "找不到界面入口",
              steps=["确认这个文件夹是完整的（app/ui/diff_app5.py 在不在）",
                     "重新拷一份完整的交付包"])
        return 1
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"       # 免得 Windows 控制台用 cp936 打印报错
    env["PYTHONUTF8"] = "1"
    env.update(tcl_env(py))                 # 见 tcl_env 的说明（换目录后 Tk 起不来）
    B.end(B.OK, "环境就绪，正在打开界面…（关掉界面窗口即可退出）")
    try:
        # 工作目录取**项目根**而不是 app/shell：data/、临时文件都相对根算，
        # 也让用户从控制台看到的那条路径就是他自己双击的那个文件夹。
        return subprocess.run([py, "-B", APP], cwd=ROOT, env=env).returncode
    except KeyboardInterrupt:
        return 0


def do_check() -> int:
    """`--check`：只看不动，把结论一次说完。"""
    py = runtime_python()
    exist = os.path.isdir(RUNTIME)
    B.section("", "运行环境")
    B.item(B.OK if exist else B.SKIP, "运行环境",
           "%s（%s）" % (os.path.relpath(RUNTIME, ROOT), "存在" if exist else "不存在"))
    ok, why = probe_runtime(py)
    if not ok:
        B.item(B.FAIL, "可用性", "不可用 —— %s" % why)
        B.end(B.FAIL, "运行环境还没准备好",
              steps=["再双击一次启动器，它会自动修一遍",
                     "仍不行就重新拷一份完整的包（runtime/ 要和启动器在同一层）"])
        return 1
    vc_row()
    B.item(B.OK, "依赖", dep_versions(py))
    tk_ok, tk_why = probe_tk(py)
    B.item(B.OK if tk_ok else B.FAIL, "界面库", tk_why)
    mods_ok, mods_why = probe_modules(py)
    B.item(B.OK if mods_ok else B.FAIL, "程序文件", mods_why)
    if not mods_ok:
        B.end(B.FAIL, "程序文件不全，先别启动",
              steps=["重新拷一份完整的交付包（少的是 app/ 里的 .py）",
                     "确认没被杀毒软件吃掉文件"])
        return 1
    B.item(B.OK, "可用性", "可用")
    B.end(B.OK, "可以正常启动")
    return 0


def main() -> int:
    args = sys.argv[1:]

    B.head(TOOL_NAME, "v" + TOOL_VERSION, "运行前自检",
           [("系统", B.os_line()),
            ("Python", "%d.%d.%d" % sys.version_info[:3]),
            ("位置", ROOT)])

    if "--check" in args:
        return do_check()

    B.section("", "运行环境")
    py = ensure_ready(force="--rebuild" in args)
    if not py:
        B.end(B.FAIL, "没能准备好环境",
              steps=["把这一屏截图发给我，我按情况处理",
                     "想重来一次：" + env_fix_hint()])
        return 1

    if "--setup" in args:
        B.end(B.OK, "环境已就绪，以后双击启动器就能直接用")
        return 0

    return launch(py)


if __name__ == "__main__":
    raise SystemExit(main())
