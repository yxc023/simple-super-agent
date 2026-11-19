#!/usr/bin/env bash
# super-agent 看板（只绑 127.0.0.1）的启停。
#
#   ./super-agent/board.sh            # 默认 8788
#   ./super-agent/board.sh 9000       # 指定端口
#   ./super-agent/board.sh stop [端口]
#   ./super-agent/board.sh status [端口]
#
# 说明：
#   · 看板读的是**状态根**（SA_ROOT，默认是本仓库的 super-agent/ 目录）里的文件。
#     设了 SA_ROOT 时看板看到的是沙箱数据，不是生产任务。
#   · pid / 日志文件都放在 /tmp 下，端口不同则文件名不同。
set -euo pipefail

PORT="8788"
ACTION="start"
if [[ "${1:-}" == "start" || "${1:-}" == "stop" || "${1:-}" == "status" ]]; then
  ACTION="$1"
  [[ -n "${2:-}" ]] && PORT="$2"
else
  PORT="${1:-8788}"
  [[ -n "${2:-}" ]] && ACTION="$2"
fi

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
  echo "端口必须是数字：$PORT" >&2
  exit 2
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="/tmp/super-agent-board-$PORT.pid"
LOG_FILE="/tmp/super-agent-board-$PORT.log"

is_running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "$ACTION" in
  stop)
    if is_running; then
      kill "$(cat "$PID_FILE")" 2>/dev/null || true
      sleep 0.5
      echo "已停止 (pid $(cat "$PID_FILE"))"
    else
      echo "未在运行"
    fi
    rm -f "$PID_FILE"
    ;;
  status)
    if is_running; then echo "运行中 (pid $(cat "$PID_FILE"))  http://127.0.0.1:$PORT"
    else echo "未在运行"; fi
    ;;
  start)
    if is_running; then
      echo "已在运行 (pid $(cat "$PID_FILE"))  http://127.0.0.1:$PORT"
      exit 0
    fi
    if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "端口 $PORT 已被占用，先停掉那个进程，或换个端口：./super-agent/board.sh 9000" >&2
      exit 1
    fi

    echo "启动看板：http://127.0.0.1:$PORT"
    ( cd "$DIR" && nohup python3 serve.py --port "$PORT" >"$LOG_FILE" 2>&1 & echo $! > "$PID_FILE" )

    # 冷启动可能慢一点，等 15 秒
    for _ in $(seq 1 75); do
      if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
        (command -v open >/dev/null && open "http://127.0.0.1:$PORT") || true
        echo "就绪（日志：$LOG_FILE）"
        exit 0
      fi
      sleep 0.2
    done

    echo "启动失败，日志尾部：" >&2
    tail -n 20 "$LOG_FILE" >&2 2>/dev/null || true
    exit 1
    ;;
  *)
    echo "用法: $0 [端口] [start|stop|status]" >&2
    exit 2
    ;;
esac
