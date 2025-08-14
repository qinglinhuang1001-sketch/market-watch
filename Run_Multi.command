#!/bin/zsh
cd "$(dirname "$0")"
source ~/.zshrc 2>/dev/null
# 如需在这里设置微信Key，可取消下一行注释并填入你的Key
# export SERVER_CHAN_KEY="SCTxxxxxxxxxxxxxxxx"

# 启动
/usr/bin/env python3 "./monitor_multi.py"

echo
read -n 1 -s -r -p "按任意键关闭窗口..."
