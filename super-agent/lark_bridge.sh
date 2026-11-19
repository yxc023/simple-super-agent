#!/usr/bin/env bash
# 飞书 → super-agent 桥接守护进程。
#   ./super-agent/lark_bridge.sh start|stop|status|logs
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="/tmp/super-agent-lark-bridge.pid"
LOG_FILE="/tmp/super-agent-lark-bridge.out"

is_running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

# 子进程要用独立进程组启动（start_new_session=True）：
# 否则父 shell 退出/被中断时，守护进程会跟着一起死。
export BRIDGE_LOG="$LOG_FILE"

case "${1:-status}" in
  start)
    if is_running; then echo "已在运行 (pid $(cat "$PID_FILE"))"; exit 0; fi
    # 清掉任何残留实例（避免两个轮询进程重复建任务）
    pkill -f "lark_bridge.py follow" 2>/dev/null || true
    sleep 0.5
    # follow = 官方事件长连接（feishu-websocket）为主，轮询兜底
    # 注意：用 exec 让 python 直接占用这个 pid —— 之前记的是中间 shell 的 pid，
    # status 判定指错了对象。python 自己也会把真实 pid 写进 PID_FILE。
    #
    # 启动方式用 python 的 start_new_session=True：**独立进程组**，
    # 这样父 shell（哪怕被 Ctrl-C / 被工具中断）退出时不会把守护进程一起带走。
    # 之前用 nohup ... & 时，父进程组一被清理，桥接就跟着没了。
    ( cd "$DIR" && python3 -c '
import os, subprocess, sys
log = open(os.environ["BRIDGE_LOG"], "ab")
p = subprocess.Popen([sys.executable, "lark_bridge.py", "follow"],
                     stdout=log, stderr=subprocess.STDOUT,
                     cwd=os.getcwd(), start_new_session=True)
print(p.pid)
' ) >/dev/null
    sleep 1.5
    if is_running; then
      echo "桥接已启动 (pid $(cat "$PID_FILE"))，日志：$LOG_FILE"
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
    cd "$DIR" && python3 lark_bridge.py status
    ;;
  logs)
    tail -n 50 "$LOG_FILE" 2>/dev/null || echo "暂无日志"
    ;;
  *)
    echo "用法: $0 start|stop|status|logs" >&2; exit 2
    ;;
esac
