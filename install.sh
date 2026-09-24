#!/usr/bin/env bash
# 校园网自动登录: 一键安装 / 卸载
#
#   ./install.sh                 用户级安装 (不需要 sudo), 登录桌面后自动运行
#   sudo ./install.sh --system   系统级安装, 开机即运行 (还没登录也会运行)
#   ./install.sh --uninstall     卸载 (系统级请加 sudo 与 --system)
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
MODE="user"
ACTION="install"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --system)    MODE="system" ;;
    --user)      MODE="user" ;;
    --uninstall) ACTION="uninstall" ;;
    -h|--help)   sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
  shift
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "缺少 python3, 请先安装: sudo apt install python3"
  exit 1
fi

if [[ "$MODE" == "system" ]]; then
  [[ $EUID -eq 0 ]] || { echo "系统级安装需要 root: sudo $0 --system"; exit 1; }
  BIN="/usr/local/bin/net-login.py"
  CONF="/etc/net-login/config.ini"
  UNIT="/etc/systemd/system/netlogin.service"
  TPL="$SRC_DIR/netlogin-system.service"
  SYSTEMCTL=(systemctl)
else
  BIN="$HOME/.local/bin/net-login.py"
  CONF="$HOME/.config/net-login/config.ini"
  UNIT="$HOME/.config/systemd/user/netlogin.service"
  TPL="$SRC_DIR/netlogin.service"
  SYSTEMCTL=(systemctl --user)
fi

if [[ "$ACTION" == "uninstall" ]]; then
  "${SYSTEMCTL[@]}" disable --now netlogin.service 2>/dev/null || true
  rm -f "$UNIT" "$BIN"
  "${SYSTEMCTL[@]}" daemon-reload 2>/dev/null || true
  echo "已卸载。配置文件保留在 $CONF (要清干净可自行删除)"
  exit 0
fi

install -D -m 0755 "$SRC_DIR/auto-login.py" "$BIN"

if [[ -f "$CONF" ]]; then
  echo "已存在配置文件, 保留原内容: $CONF"
else
  install -D -m 0600 "$SRC_DIR/config.example.ini" "$CONF"
  echo "已生成配置文件: $CONF"
fi

mkdir -p "$(dirname "$UNIT")"
sed -e "s|@BIN@|$BIN|g" -e "s|@CONFIG_ARGS@|--config $CONF |g" "$TPL" > "$UNIT"

"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable --now netlogin.service
"${SYSTEMCTL[@]}" --no-pager status netlogin.service 2>/dev/null || true

if [[ "$MODE" == "user" ]]; then
  LOGCMD="journalctl --user -u netlogin -f"
else
  LOGCMD="journalctl -u netlogin -f"
fi

cat <<TIP

安装完成, 下一步:
  1. 填写账号密码:  nano $CONF
  2. 试一次登录:    $BIN login --verbose
  3. 查看运行日志:  $LOGCMD

提示: 用户级服务在你登录桌面后运行; 想让它退出登录也保持运行可执行(需要一次 sudo):
      sudo loginctl enable-linger $USER
      或改用系统级安装: sudo $0 --system
TIP
