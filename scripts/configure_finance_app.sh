#!/bin/sh
# Write the Finance Feishu credentials and test-Base identifiers to a local,
# Git-ignored file with mode 600.
#
# Run this yourself. The secret is never echoed, never passed as a command line
# argument, and never leaves this machine. Nothing here should be pasted into a
# chat, a commit, or an issue.
#
# Production uses systemd `LoadCredentialEncrypted=` instead of this file; this
# is the local development equivalent, kept separate from the model credentials
# in `.env.local` so the two rotate independently.
set -eu

output_file=".env.finance.local"
old_mask="$(umask)"
umask 077

cleanup() {
  stty echo 2>/dev/null || true
  unset app_secret
  umask "$old_mask"
}
trap cleanup EXIT HUP INT TERM

printf "\n配置 Personal Agent Finance 飞书应用（本地开发用）\n" >&2
printf "所有值只写入 %s，该文件已被 Git 忽略且权限为 600。\n\n" "$output_file" >&2

printf "App ID: " >&2
IFS= read -r app_id

printf "App Secret（输入不会回显）: " >&2
stty -echo
IFS= read -r app_secret
stty echo
printf "\n" >&2

printf "\n以下是**合成测试 Base** 的标识符。G5 之前不要填写个人年度账本。\n" >&2
printf "测试 Base app_token: " >&2
IFS= read -r base_token
printf "支出记录 table_id: " >&2
IFS= read -r expense_table
printf "收入记录 table_id: " >&2
IFS= read -r income_table
printf "家庭基金 table_id: " >&2
IFS= read -r family_fund_table

if [ -z "$app_id" ] || [ -z "$app_secret" ] || [ -z "$base_token" ] ||
  [ -z "$expense_table" ] || [ -z "$income_table" ] ||
  [ -z "$family_fund_table" ]; then
  printf "\n未写入：App ID、App Secret、Base app_token 和三张 table_id 都不能为空。\n" >&2
  exit 1
fi

{
  printf "FEISHU_FINANCE_APP_ID=%s\n" "$app_id"
  printf "FEISHU_FINANCE_APP_SECRET=%s\n" "$app_secret"
  printf "FEISHU_FINANCE_BASE_TOKEN=%s\n" "$base_token"
  printf "FEISHU_FINANCE_TABLE_EXPENSE=%s\n" "$expense_table"
  printf "FEISHU_FINANCE_TABLE_INCOME=%s\n" "$income_table"
  printf "FEISHU_FINANCE_TABLE_FAMILY_FUND=%s\n" "$family_fund_table"
  printf "FEISHU_FINANCE_LEDGER_KIND=synthetic_test\n"
} > "$output_file"
chmod 600 "$output_file"

unset app_secret
umask "$old_mask"
trap - EXIT HUP INT TERM

printf "\n已写入 %s（mode 600，Git 已忽略）。\n" "$output_file" >&2
printf "别把其中任何一行贴进聊天、commit 或 issue。\n" >&2
