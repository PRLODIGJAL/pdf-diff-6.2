# -*- coding: utf-8 -*-
"""paths.py —— **全项目唯一的路径来源**。

2026-11-02 目录重构时新建。以前每个入口自己 `os.path.dirname(__file__)` 拼一遍，
结果同一件事（「模型在哪儿」「配置写哪儿」）在五六个文件里各有一套说法；
2026-10-14 就是因为 `_vision_ocr.swift` 和引擎不在同一目录，Vision 默默失效、
图片区整批漏检，还查了半天。**凡是「某个东西在哪儿」的问题，只能问这里。**

四层结构（详见 `vibecoding成品/项目i5统一UI美学/统一架构_目录规范.md`）

    app/ui/      界面层    只画界面、收事件、把活派给下面
    app/judge/   判定层    业务规则：什么算差异、怎么分类、要不要先问用户
    app/engine/  引擎层    可复用的底层能力：渲染、OCR、像素比对、格式转换
    app/shell/   启动壳    环境自检、装依赖、启动过程的那几行字

    data/        运行期产物（配置记忆等），可删，删了只是忘记上次选择
    dev/         开发资产（回归、样例、截图），**交付时可以整个删掉**
    .venv/       本地环境，不随包分发

⚠️ 模块名保持扁平（`import pdfdiff_locate`）。四层只是**目录**分层，
   不是 Python 包分层 —— 引擎内部互相 import 的地方有二十多处，
   改成 `from app.engine import ...` 收益为零、风险极高。
   `install()` 把四个目录一起塞进 `sys.path`，扁平名字照旧能解析。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP = os.path.join(ROOT, "app")
UI = os.path.join(APP, "ui")
JUDGE = os.path.join(APP, "judge")
ENGINE = os.path.join(APP, "engine")
SHELL = os.path.join(APP, "shell")

# 四层一次性加进搜索路径：顺序即优先级（同名的模块谁先命中用谁，目前无重名）
LAYERS = (UI, JUDGE, ENGINE, SHELL)

DATA = os.path.join(ROOT, "data")
DEV = os.path.join(ROOT, "dev")
VENV = os.path.join(ROOT, ".venv")

# 运行期产物
CONFIG_PATH = os.path.join(DATA, "上次选择.json")

# 随引擎分发的离线资源 —— **必须与 pdfdiff_locate.py 同目录**。
# 引擎是按「自己所在目录」找 models/ 与 _vision_ocr.swift 的（各有一处硬编码），
# 抽到公共 assets/ 会让它找不到；找不到时 Vision 那条路是**静默降级**，不报错。
MODELS = os.path.join(ENGINE, "models")
VISION_SRC = os.path.join(ENGINE, "_vision_ocr.swift")


def install() -> str:
    """把四层加进 `sys.path`（幂等），返回项目根目录。

    入口脚本只要两行就能自举：

        sys.path.insert(0, os.path.join(_ROOT, "app"))
        import paths; paths.install()
    """
    for p in reversed(LAYERS):          # 反向插入，最终 UI 在最前
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    return ROOT


def ensure_data_dir() -> str:
    """`data/` 只在第一次写配置时需要，按需创建（不让空目录进交付包）。"""
    os.makedirs(DATA, exist_ok=True)
    return DATA


def describe() -> str:
    """一行说明这份代码在哪、资源在不在 —— 给启动自检和报障日志用。"""
    return "根目录 %s｜引擎 %s（模型 %s）" % (
        ROOT, "就位" if os.path.isdir(ENGINE) else "缺失",
        "就位" if os.path.isdir(MODELS) else "缺失")
