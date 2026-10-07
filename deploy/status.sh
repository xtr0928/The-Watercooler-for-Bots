#!/bin/sh
set -u

# 切换到仓库根目录（脚本位于 deploy/ 下）
cd "$(dirname "$0")/.." || exit 1

PID_FILE="data/server.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "服务未在运行"
else
    PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "运行中（PID：$PID）"
    else
        echo "进程不存在（残留 PID 文件）"
    fi
fi

if command -v curl >/dev/null 2>&1; then
    HEALTH=$(curl -s http://127.0.0.1:61900/health 2>/dev/null)
    if [ -n "$HEALTH" ]; then
        echo "$HEALTH"
    else
        echo "健康检查失败"
    fi
else
    echo "curl 未安装，无法执行健康检查"
fi

exit 0
