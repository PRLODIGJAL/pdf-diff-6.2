#!/bin/bash
# PDF差异对比工具 —— 日常使用双击这个
# 自检通过就直接打开界面；不通过会自动修复，修不好会说清楚原因。
cd "$(dirname "$0")" || exit 1

PY="runtime/bin/python3"

if [ ! -e "$PY" ]; then
  echo "找不到 runtime/bin/python3"
  echo "这个包不完整 —— runtime 目录要和本文件在同一层，重新拷一份完整的"
  echo
  read -r -p "按回车键关闭…"
  exit 1
fi

# 修执行权限（拷到 FAT/exFAT 盘会丢 +x）
chmod -R u+x runtime/bin/ 2>/dev/null
# 去 macOS 隔离属性
xattr -rd com.apple.quarantine runtime 2>/dev/null

"$PY" -B app/shell/setup_env.py "$@"
rc=$?
if [ $rc -ne 0 ]; then
  echo
  read -r -p "出错了（退出码 $rc）。按回车键关闭…"
fi
exit $rc
