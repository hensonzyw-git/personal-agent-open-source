#!/bin/sh
set -eu

output_file=".env.local"
old_mask="$(umask)"
umask 077

cleanup() {
  stty echo 2>/dev/null || true
  unset zai_api_key
  umask "$old_mask"
}
trap cleanup EXIT HUP INT TERM

printf "请输入智谱 API Key（输入不会回显）: " >&2
stty -echo
IFS= read -r zai_api_key
stty echo
printf "\n" >&2

if [ -z "$zai_api_key" ]; then
  printf "未写入：Key 不能为空。\n" >&2
  exit 1
fi

{
  printf "ZAI_API_KEY=%s\n" "$zai_api_key"
  printf "MODEL_API_BASE=https://open.bigmodel.cn/api/paas/v4/\n"
  printf "MODEL_ID=glm-5.2\n"
  printf "ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic\n"
  printf "ANTHROPIC_DEFAULT_HAIKU_MODEL=glm-4.7\n"
  printf "ANTHROPIC_DEFAULT_SONNET_MODEL=glm-5.2\n"
  printf "ANTHROPIC_DEFAULT_OPUS_MODEL=glm-5.2\n"
} > "$output_file"
chmod 600 "$output_file"

unset zai_api_key
umask "$old_mask"
trap - EXIT HUP INT TERM

printf "已写入 %s；该文件已被 Git 忽略。\n" "$output_file"
