#!/usr/bin/env bash
# 企微智能机器人 → super-agent 桥接守护进程。
#   ./super-agent/wecom_bridge.sh start|stop|status|logs
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="/tmp/super-agent-wecom-bridge.pid"
LOG_FILE="/tmp/super-agent-wecom-bridge.out"

is_running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

export BRIDGE_LOG="$LOG_FILE"

case "${1:-status}" in
  start)
    if is_running; then echo "已在运行 (pid $(cat "$PID_FILE"))"; exit 0; fi
    # 清掉残留实例：企微**同一时间只允许一条长连接**，
    # 两个进程会互相把对方踢下线（表现为「刚连上就断」）。
    pkill -f "wecom_bridge.py follow" 2>/dev/null || true
    sleep 0.5
    # 独立进程组启动（start_new_session=True）：父 shell 退出时守护进程不死
    ( cd "$DIR" && python3 -c '
import os, subprocess, sys
log = open(os.environ["BRIDGE_LOG"], "ab")
p = subprocess.Popen([sys.executable, "wecom_bridge.py", "follow"],
                     stdout=log, stderr=subprocess.STDOUT,
                     cwd=os.getcwd(), start_new_session=True)
print(p.pid)
' ) >/dev/null
    sleep 2.5
    if is_running; then
      echo "企微桥接已启动 (pid $(cat "$PID_FILE")))，日志：$LOG_FILE"
    else
      echo "启动失败，看日志：$LOG_FILE" >&2; exit 1
    fi
    ;;
  stop)
    if is_running; then
      kill "$(cat "$PID_FILE")" 2>/dev/null || true
      sleep 0.5
      echo "已停止"
    else
      echo "未在运行"
    fi
    rm -f "$PID_FILE"
    ;;
  status)
    if is_running; then echo "运行中 (pid $(cat "$PID_FILE"))"; else echo "未运行"; fi
    cd "$DIR" && python3 wecom_bridge.py status
    ;;
  logs)
    tail -n 50 "$LOG_FILE" 2>/dev/null || echo "暂无日志"
    ;;
  *)
    echo "用法: $0 start|stop|status|logs" >&2; exit 2
    ;;
esac
