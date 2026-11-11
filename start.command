#!/bin/bash
# 双击启动：歌曲工作台（依赖 suno-panel 在 8765 起）
cd "$(dirname "$0")" || exit 1
if ! lsof -ti:7792 >/dev/null 2>&1; then
  /usr/bin/python3 app.py > server.log 2>&1 &
  sleep 1.5
fi
if ! lsof -ti:8765 >/dev/null 2>&1; then
  ( cd ../suno-panel && /usr/bin/python3 server.py > /tmp/suno-panel.log 2>&1 & )
  sleep 2
fi
open "http://127.0.0.1:7792/"
