#!/usr/bin/env bash
# 部署到华为云 ECS（venv + systemd，不用 Docker / Nginx）
#
# 用法：
#   bash scripts/deploy_cloud.sh root@1.2.3.4
#   bash scripts/deploy_cloud.sh root@1.2.3.4 --restart-only
#
# 前置条件：
#   1. ECS 已开通（Ubuntu 22.04，1核2G 起），安全组放行 18080
#   2. 本机可 ssh 免密登录，且已装 rsync
#   3. 本地 .env 已配好 DB_MODE=rds、CACHE_MODE=redis（不会被同步上去，需在服务器上单独放）
#
# 说明：.env 有意排除在同步之外，避免把生产密码带进 Git 工作区；
#       首次部署后请在服务器 /opt/elevator-rescue/.env 里填一次。
set -euo pipefail

TARGET="${1:?用法: bash scripts/deploy_cloud.sh user@公网IP}"
MODE="${2:-full}"
REMOTE_USER="${TARGET%@*}"   # 本地就确定用户名，避免 heredoc 里 $USER 展开成当前机器用户
APP_DIR="/opt/elevator-rescue"
SERVICE="elevator-rescue"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> 目标 $TARGET:$APP_DIR  模式 $MODE"

if [[ "$MODE" == "full" ]]; then
  echo "==> 1/5 准备远程目录与系统依赖"
  ssh "$TARGET" "sudo mkdir -p $APP_DIR && sudo chown -R \$USER:\$USER $APP_DIR"
  ssh "$TARGET" "sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv python3-pip rsync"

  echo "==> 2/5 同步代码（排除 .venv / .env / data/raw / 本地库）"
  rsync -avz --delete \
    --exclude '.venv' --exclude '.git' --exclude '.env' \
    --exclude 'data/raw' --exclude 'elevator.db' --exclude '__pycache__' \
    "$LOCAL_DIR/" "$TARGET:$APP_DIR/"

  echo "==> 3/5 创建虚拟环境并安装锁定依赖"
  ssh "$TARGET" "cd $APP_DIR && python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.lock.txt"

  echo "==> 4/5 写 systemd 单元（Restart=always）"
  ssh "$TARGET" "sudo tee /etc/systemd/system/$SERVICE.service > /dev/null <<'UNIT'
[Unit]
Description=Elevator Rescue FastAPI Service
After=network.target

[Service]
Type=simple
User=$REMOTE_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python -m service.main
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT"
fi

echo "==> 5/5 启动并设置开机自启"
ssh "$TARGET" "sudo systemctl daemon-reload && sudo systemctl enable $SERVICE && sudo systemctl restart $SERVICE && sleep 2 && systemctl --no-pager status $SERVICE | head -12"

PUBLIC_IP="${TARGET#*@}"
echo ""
echo "部署完成，访问："
echo "  看板   http://$PUBLIC_IP:18080/"
echo "  接口页 http://$PUBLIC_IP:18080/docs"
echo "  健康   http://$PUBLIC_IP:18080/api/v1/health"
echo ""
echo "常用命令："
echo "  ssh $TARGET 'sudo journalctl -u $SERVICE -f'      # 看日志"
echo "  ssh $TARGET 'sudo systemctl restart $SERVICE'     # 重启"
echo "  bash scripts/deploy_cloud.sh $TARGET --restart-only   # 只重启不改代码"
