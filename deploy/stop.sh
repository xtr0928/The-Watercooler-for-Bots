#!/bin/sh
set -u

# 切换到仓库根目录（脚本位于 deploy/ 下）
cd "$(dirname "$0")/.." || exit 1

PID_FILE="data/server.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "服务未在运行"
    exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
    # 进程存活，先发送 TERM 信号
    kill "$PID" 2>/dev/null || true
    sleep 0.5

    if kill -0 "$PID" 2>/dev/null; then
        # 仍存活，强制杀死
        kill -9 "$PID" 2>/dev/null || true
    fi

    echo "已停止（PID：$PID）"
else
    # 进程已不存在，按已清理处理（幂等）
    echo "已清理（PID：$PID）"
fi

rm -f "$PID_FILE"
exit 0
