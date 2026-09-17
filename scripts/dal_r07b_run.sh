#!/bin/bash
# DAL-R07B 真实验证：跑一个真实 claude -p happy path。
#
# appkey 从环境变量 DAL_CODER_TOKEN 读；若未设置则用 read -s 提示输入（不回显）。
# appkey 不落盘（脚本只把它写进一个 0600 临时文件，跑完即删）、不进日志、不进任何
# 会回显到屏幕的地方。
#
# 用法：
#   bash scripts/dal_r07b_run.sh
#   （或先 export DAL_CODER_TOKEN=... 再跑，跳过交互输入）
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -z "${DAL_CODER_TOKEN:-}" ]]; then
    # -r 保留反斜杠原样，-s 不回显。
    read -r -s -p "claude→CCR appkey: " DAL_CODER_TOKEN
    echo
fi
if [[ -z "${DAL_CODER_TOKEN}" ]]; then
    echo "error: empty appkey" >&2
    exit 1
fi
export DAL_CODER_TOKEN

exec .venv/bin/python scripts/dal_r07b_coder_slice.py
