#!/bin/sh
# Starts llama-server and the Telegram bot. Run inside the Libertine container:
#   libertine-launch -i ai sh ~/ai/start-ai.sh
# Logs: ~/ai/logs/

AI=~/ai
MODEL=${MODEL:-$AI/models/qwen2.5-1.5b-instruct-q4_k_m.gguf}
mkdir -p $AI/logs

# Allowed Telegram user ids, comma-separated. Send the bot a message once and
# copy your id from the "ignored message from user id" line in bot.log.
export BOT_ALLOWED_IDS=${BOT_ALLOWED_IDS:-}

$AI/llama.cpp/build/bin/llama-server -m "$MODEL" -t 4 -c 4096 \
    --host 127.0.0.1 --port 8080 >> $AI/logs/llama-server.log 2>&1 &
LLM_PID=$!
trap 'kill $LLM_PID 2>/dev/null' EXIT INT TERM

# Wait until the model is loaded (llama-server answers /health with 200).
for i in $(seq 1 60); do
    curl -sf http://127.0.0.1:8080/health > /dev/null && break
    sleep 2
done

python3 $AI/ai_bot.py >> $AI/logs/bot.log 2>&1
