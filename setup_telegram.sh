#!/bin/bash
# 設定 Telegram 通知：token 只會存進 GitHub Secrets，不會寫進任何檔案
set -e
cd "$(dirname "$0")"
read -rsp "請貼上 BotFather 給你的 token，然後按 Enter（畫面不會顯示）: " TOKEN; echo
CHAT=$(curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | python3 -c '
import json,sys
d=json.load(sys.stdin)
if not d.get("ok"): sys.exit("token 不正確，請確認後再試一次")
ids=[u["message"]["chat"]["id"] for u in d["result"] if "message" in u]
if not ids: sys.exit("找不到訊息：請先在 Telegram 對你的 bot 傳一句 hi，再執行一次")
print(ids[-1])')
curl -s "https://api.telegram.org/bot$TOKEN/sendMessage" -d chat_id="$CHAT" \
  --data-urlencode text="✅ 日光空房監控已連線！有空房時會在這裡通知你。" > /dev/null
printf '%s' "$TOKEN" | gh secret set TELEGRAM_TOKEN
printf '%s' "$CHAT" | gh secret set TELEGRAM_CHAT_ID
echo "完成！已傳測試訊息到你的 Telegram，並把設定存進 GitHub Secrets。"
