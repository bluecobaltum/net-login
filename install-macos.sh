#!/usr/bin/env bash
# 校园网自动登录: macOS 一键安装 / 卸载 (launchd)
#
#   ./install-macos.sh              用户级(登录后自动运行), 不需要 sudo
#   sudo ./install-macos.sh --system  系统级, 开机即运行(还没登录也会运行)
#   ./install-macos.sh --uninstall   卸载(系统级请加 sudo 与 --system)
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
ACTION="install"
MODE="user"

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

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "没找到 python3, 先安装: xcode-select --install   或   brew install python3"
  exit 1
fi

if [[ "$MODE" == "system" ]]; then
  [[ $EUID -eq 0 ]] || { echo "系统级安装需要 root: sudo $0 --system"; exit 1; }
  LABEL="cn.netlogin.daemon"
  BIN="/usr/local/bin/net-login.py"
  CONF="/Library/Application Support/net-login/config.ini"
  PLIST="/Library/LaunchDaemons/$LABEL.plist"
  LOG="/var/log/netlogin.log"
  DOMAIN="system"
else
  LABEL="cn.netlogin.agent"
  BIN="$HOME/.local/bin/net-login.py"
  CONF="$HOME/Library/Application Support/net-login/config.ini"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  LOG="$HOME/Library/Logs/netlogin.log"
  DOMAIN="gui/$(id -u)"
fi

bootout() { launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true; }

if [[ "$ACTION" == "uninstall" ]]; then
  bootout
  rm -f "$PLIST" "$BIN"
  echo "已卸载。配置文件保留在: $CONF"
  exit 0
fi

install -d -m 0755 "$(dirname "$BIN")" "$(dirname "$PLIST")" "$(dirname "$CONF")" "$(dirname "$LOG")"
install -m 0755 "$SRC_DIR/auto-login.py" "$BIN"
if [[ -f "$CONF" ]]; then
  echo "已存在配置文件, 保留原内容: $CONF"
else
  install -m 0600 "$SRC_DIR/config.example.ini" "$CONF"
  echo "已生成配置文件: $CONF"
fi

bootout
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$BIN</string>
        <string>--config</string>
        <string>$CONF</string>
        <string>watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>20</integer>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
PLIST
chmod 0644 "$PLIST"
[[ "$MODE" == "system" ]] && chown root:wheel "$PLIST"

launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
launchctl print "$DOMAIN/$LABEL" 2>/dev/null | head -8 || true

cat <<TIP

安装完成, 下一步:
  1. 填写账号密码:  open -e "$CONF"
  2. 试一次登录:    "$BIN" login --verbose
  3. 查看日志:      tail -f "$LOG"

提示: 想查看状态用  launchctl print $DOMAIN/$LABEL
      停掉服务用      launchctl bootout $DOMAIN/$LABEL
      重启用          launchctl kickstart -k $DOMAIN/$LABEL
TIP
