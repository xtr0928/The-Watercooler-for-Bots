#!/bin/sh
set -u

cd "$(dirname "$0")/.."

mkdir -p data

if [ -f data/server.pid ]; then
    pid=$(cat data/server.pid)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "服务已在运行（PID：$pid）"
        exit 0
    fi
fi

nohup python3 server.py --host 0.0.0.0 --port 61900 >> data/server.log 2>&1 &
echo $! > data/server.pid

sleep 0.5

if command -v curl >/dev/null 2>&1; then
    curl -s http://127.0.0.1:61900/health >/dev/null 2>&1 || true
fi

echo "服务已启动：http://0.0.0.0:61900"
echo "日志路径：data/server.log"
echo "如需修改端口请编辑本脚本"

exit 0
