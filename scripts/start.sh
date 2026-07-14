#!/usr/bin/env bash
# 启动贾维斯 AI + 舆情监控大屏
set -e
cd "$(dirname "$0")/.."

# 加载环境变量
if [ -f .env ]; then
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

PORT="${DASHBOARD_PORT:-8765}"
echo "🚀 启动服务: http://127.0.0.1:${PORT}/public/jarvis.html"
python3 scripts/serve.py
