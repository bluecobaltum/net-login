#!/usr/bin/env python3
"""校园网自动登录 / 掉线重连 (Dr.COM eportal 认证).

命令:
  check     检查是否已联网, 已联网返回 0, 需要登录返回 1
  login     登录一次 (已在线则跳过, 除非 --force)
  watch     常驻守护, 定时检查, 掉线自动登录
  status    打印门户返回的在线状态
  discover  自动发现认证门户地址
  logout    注销当前账号 (用于换账号)

只用 Python 标准库, 无第三方依赖。
"""

import argparse
import base64
import configparser
import http.client
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

JS_VERSION = "4.1.3"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DEFAULT_PORTAL_PORT = 801
PORTAL_MARKERS = (b"Dr.COM", b"eportal", b"authloginpath", b"0MKKey", b"DDDDD", b"authuserfield")
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

if IS_WINDOWS:
    CONF_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "net-login")
elif IS_MACOS:
    CONF_DIR = os.path.expanduser("~/Library/Application Support/net-login")
else:
    CONF_DIR = os.path.expanduser("~/.config/net-login")
CONFIG_PATHS = [os.path.join(CONF_DIR, "config.ini"), "/etc/net-login/config.ini"]
DEFAULTS = {
    "account": {"username": "", "password": "", "suffix": ""},
    "portal": {"host": "", "port": str(DEFAULT_PORTAL_PORT), "account_prefix": "auto"},
    "security": {
        # 只允许在这些网段内登录: 本机 IP 和门户地址都必须落在其中,
        # 防止连上别的网络(酒店/其他学校/伪造热点)时把校园网账密发出去
        "allow_cidrs": "172.30.0.0/16",
        # 门户响应里必须出现的特征串(校园门户的 page_name 是 szuXXXX, 用来确认"这是我们学校那个门户")
        "expect_keywords": "szu",
    },
    "runtime": {
        "probe_urls": "http://cp.cloudflare.com/generate_204, http://www.gstatic.com/generate_204, http://www.baidu.com/",
        "timeout": "8",
        "interval": "15",
        "retry": "3",
        "retry_delay": "5",
        "verify_delay": "2",
        "auth_error_interval": "600",
        "mac": "000000000000",
        "notify": "false",
    },
}


class PortalError(Exception):
    """配置/协议类错误, 重试也没用。"""


class PortalUnavailable(PortalError):
    """门户暂时不可用(5xx、超时等), 稍后重试即可。"""


class AuthError(PortalError):
    """账号/密码/欠费类问题, 重试没有意义, 而且反复试可能触发账号锁定。"""


LOG_FILE = None


def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def find_config(explicit=None):
    if explicit:
        return os.path.expanduser(explicit)
    env = os.environ.get("NETLOGIN_CONFIG")
    if env:
        return os.path.expanduser(env)
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            return path
    return CONFIG_PATHS[0]


def load_config(path=None):
    # interpolation=None: 密码里出现 % 也不会被当成插值语法
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(find_config(path), encoding="utf-8")
    return cfg


def cfg_str(cfg, section, key):
    return (cfg.get(section, key, fallback=DEFAULTS[section][key]) or "").strip()


def cfg_int(cfg, section, key):
    raw = cfg.get(section, key, fallback=DEFAULTS[section][key]).strip()
    try:
        return int(float(raw))
    except ValueError:
        return int(float(DEFAULTS[section][key]))


def cfg_bool(cfg, section, key):
    return cfg.get(section, key, fallback=DEFAULTS[section][key]).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# HTTP 基础
# --------------------------------------------------------------------------
def http_get(url, timeout=8, data=None):
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.geturl()


def _connect_ipv4(host, port, timeout):
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    if not infos:
        raise OSError("no IPv4 address for %s" % host)
    family, socktype, proto, _canon, sockaddr = infos[0]
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(timeout)
    try:
        sock.connect(sockaddr)
    except OSError:
        sock.close()
        raise
    return sock


class IPv4HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _connect_ipv4(self.host, self.port, self.timeout)


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(_connect_ipv4(self.host, self.port, self.timeout),
                                              server_hostname=self.host)


def http_get_ipv4(url, timeout=8, max_redirects=3):
    """强制走 IPv4 的 GET。

    校园网的认证只管 IPv4, IPv6 往往免认证就通。如果用默认的双栈请求,
    掉线时探针可能走 IPv6 返回成功, 于是误判成“已联网”而不去登录 —— 必须只认 IPv4。
    """
    for _ in range(max_redirects + 1):
        parts = urllib.parse.urlsplit(url)
        cls = IPv4HTTPSConnection if parts.scheme == "https" else IPv4HTTPConnection
        conn = cls(parts.hostname, parts.port, timeout=timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            conn.request("GET", path, headers={"User-Agent": UA, "Accept": "*/*"})
            resp = conn.getresponse()
            status, body, location = resp.status, resp.read(), resp.getheader("Location")
        finally:
            conn.close()
        if status in (301, 302, 303, 307, 308) and location:
            url = urllib.parse.urljoin(url, location)
            continue
        return status, body, url
    raise OSError("重定向次数过多: %s" % url)


def jsonp_get(url, params, timeout=8):
    """门户接口是 JSONP: callback({...}), 这里解析成 dict。"""
    return jsonp_get_raw(url, params, timeout)[0]


def jsonp_get_raw(url, params, timeout=8):
    query = urllib.parse.urlencode(params)
    full = "%s%s%s" % (url, "&" if "?" in url else "?", query)
    if "v=" not in query:
        full += "&v=%d&lang=zh" % random.randint(500, 9999)
    try:
        status, body, _ = http_get(full, timeout)
    except Exception as exc:
        raise PortalUnavailable("门户请求失败: %s: %s" % (type(exc).__name__, exc))
    if status >= 500:
        raise PortalUnavailable("门户暂时不可用 (HTTP %s), 稍后重试" % status)
    match = re.match(rb"\s*[\w$.]+\((.*)\)\s*;?\s*$", body, re.S)
    if not match:
        raise PortalUnavailable("门户返回异常 (HTTP %s): %s" % (status, body[:120]))
    text = match.group(1).decode("utf-8", "replace")
    try:
        return json.loads(text), text
    except ValueError as exc:
        raise PortalError("门户返回数据解析失败: %s" % exc)


def local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


# --------------------------------------------------------------------------
# 联网检测
# --------------------------------------------------------------------------
def looks_like_portal(body):
    return any(marker in body[:20000] for marker in PORTAL_MARKERS)


def probe_urls(cfg):
    raw = cfg.get("runtime", "probe_urls", fallback=DEFAULTS["runtime"]["probe_urls"])
    return [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]


def internet_ok(cfg):
    timeout = cfg_int(cfg, "runtime", "timeout")
    for url in probe_urls(cfg):
        expect_204 = ("generate_204" in url) or ("connecttest" in url)
        try:
            status, body, _ = http_get_ipv4(url, timeout)
        except Exception:
            continue
        if looks_like_portal(body):
            continue
        if expect_204 and status == 204:
            return True
        if not expect_204 and status == 200 and len(body) > 200:
            return True
    return False


def portal_status(cfg, verbose=False):
    """门户在线状态接口, result=1 表示在线。返回 None 表示门户不可达。"""
    timeout = cfg_int(cfg, "runtime", "timeout")
    try:
        host, _port = resolve_portal(cfg)
    except PortalError as exc:
        if verbose:
            log("门户地址未知: %s" % exc)
        return None
    try:
        return jsonp_get("http://%s/drcom/chkstatus" % host,
                         {"callback": "dr1003", "jsVersion": JS_VERSION}, timeout)
    except Exception as exc:
        if verbose:
            log("门户状态接口不可达: %s" % exc)
        return None


def is_online(cfg, verbose=False):
    """在校园网内以门户的会话状态为准, 门户不可达时才看 IPv4 探针。"""
    status = portal_status(cfg)
    if status is not None:
        online = str(status.get("result")) == "1"
        if verbose:
            log("门户会话状态: %s" % ("已认证(在线)" if online else "未认证(需要登录)"))
        return online
    if internet_ok(cfg):
        if verbose:
            log("门户不可达, 但 IPv4 外网探针通过, 判定为已联网")
        return True
    if verbose:
        log("门户不可达, IPv4 探针也不通")
    return False


# --------------------------------------------------------------------------
# 门户发现 / 参数
# --------------------------------------------------------------------------
_DISCOVERY_CACHE = {}
_FOREIGN_LOGGED = False
_LAST_DISCOVERY = None


def reset_discovery_cache():
    _DISCOVERY_CACHE.clear()


def discover_portal(cfg):
    """未认证时访问外网会被重定向到认证页, 从中解析门户地址。"""
    if "portal" in _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE["portal"]
    timeout = cfg_int(cfg, "runtime", "timeout")
    for url in probe_urls(cfg):
        try:
            status, body, final = http_get(url, timeout)
        except Exception:
            continue
        if not looks_like_portal(body):
            continue
        host = urllib.parse.urlsplit(final).hostname
        if not host:
            continue
        port = DEFAULT_PORTAL_PORT
        match = re.search(rb"authloginport\s*=\s*'?(\d+)", body)
        if not match:
            match = re.search(rb"epHTTPPort\s*=\s*(\d+)", body)
        if match:
            port = int(match.group(1))
        global _LAST_DISCOVERY
        if _LAST_DISCOVERY != (host, port):      # 同一个门户只提示一次
            log("发现认证门户: %s:%d" % (host, port))
            _LAST_DISCOVERY = (host, port)
        _DISCOVERY_CACHE["portal"] = (host, port)
        return host, port
    raise PortalError("无法自动发现认证门户, 请在 config.ini 里手动填写 [portal] host")


def resolve_portal(cfg, force=False):
    host = cfg_str(cfg, "portal", "host")
    port = cfg_int(cfg, "portal", "port")
    if host and not force:
        return host, port
    return discover_portal(cfg)


def portal_page_config(cfg, host, port):
    """门户页面配置: 登录方式、账号前缀、端口等。"""
    timeout = cfg_int(cfg, "runtime", "timeout")
    ip = local_ip()
    params = {
        "program_index": "",
        "wlan_vlan_id": "",
        "wlan_user_ip": base64.b64encode(ip.encode()).decode(),
        "wlan_user_ipv6": "",
        "wlan_user_ssid": "",
        "wlan_user_areaid": "",
        "wlan_ac_ip": "",
        "wlan_ap_mac": "",
        "gw_id": "",
        "callback": "dr1003",
        "jsVersion": JS_VERSION,
    }
    result, raw = jsonp_get_raw("http://%s:%d/eportal/portal/page/loadConfig" % (host, port), params, timeout)
    if "data" not in result:
        raise PortalError("读取门户配置失败: %s" % result)
    return result["data"], raw


def portal_ready(cfg):
    """返回 (host, port, page_config, 原始响应); 配置的门户不可达时自动重新发现。"""
    host, port = resolve_portal(cfg)
    try:
        data, raw = portal_page_config(cfg, host, port)
        return host, port, data, raw
    except PortalUnavailable as exc:
        log("配置的门户 %s:%d 不可用: %s" % (host, port, exc))
    host, port = discover_portal(cfg)
    data, raw = portal_page_config(cfg, host, port)
    return host, port, data, raw


def allowed_networks(cfg):
    raw = cfg.get("security", "allow_cidrs", fallback=DEFAULTS["security"]["allow_cidrs"]).strip()
    if raw.lower() in ("any", "*", "none", ""):
        return None
    nets = []
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if item:
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                raise PortalError("config.ini 的 [security] allow_cidrs 不合法: %s" % item)
    return nets or None


def expect_keywords(cfg):
    raw = cfg.get("security", "expect_keywords", fallback=DEFAULTS["security"]["expect_keywords"]).strip()
    if raw.lower() in ("any", "*", "none", ""):
        return []
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


def security_check(cfg, host, portal_text=""):
    """确认'这确实是我们学校的门户、我们确实在校园网里'。返回 (是否通过, 原因)。"""
    nets = allowed_networks(cfg)
    if nets:
        allowed_text = "、".join(str(n) for n in nets)
        for label, value in (("本机 IP", local_ip()), ("门户地址", host)):
            if not value:
                continue
            try:
                addr = ipaddress.ip_address(value)
            except ValueError:
                return False, "%s %r 不是合法 IP" % (label, value)
            if not any(addr in net for net in nets):
                return False, "%s %s 不在允许网段 %s 内" % (label, value, allowed_text)
    keywords = expect_keywords(cfg)
    if keywords and portal_text:
        lowered = portal_text.lower()
        if not any(k.lower() in lowered for k in keywords):
            return False, "门户特征校验失败(响应里没有 %s)" % "/".join(keywords)
    return True, ""


def account_prefix(cfg, page_config):
    """PC 端账号前缀: 门户开启 account_prefix 时为 ',0,'。"""
    mode = cfg_str(cfg, "portal", "account_prefix").lower()
    if mode == "off":
        return ""
    if mode not in ("auto", ""):
        return cfg_str(cfg, "portal", "account_prefix")
    return ",0," if str(page_config.get("account_prefix", "0")) == "1" else ""


# --------------------------------------------------------------------------
# 登录 / 注销
# --------------------------------------------------------------------------
AUTH_ERROR_KEYWORDS = ("密码错误", "密码不正确", "账号或密码", "账号不存在", "用户不存在",
                       "已停机", "请充值", "账户余额", "账号已锁定", "认证失败次数")


def classify_auth_error(msg):
    return any(keyword in msg for keyword in AUTH_ERROR_KEYWORDS)


def build_account(cfg, page_config):
    username = cfg_str(cfg, "account", "username")
    if not username:
        raise PortalError("config.ini 里还没填 [account] username")
    suffix = cfg_str(cfg, "account", "suffix") or str(page_config.get("account_suffix", "") or "")
    return account_prefix(cfg, page_config) + username + suffix


def do_login(cfg, force=False, dry_run=False):
    username = cfg_str(cfg, "account", "username")
    password = cfg_str(cfg, "account", "password")
    if not username or not password:
        raise PortalError("config.ini 里还没填 [account] username / password")
    if not force and is_online(cfg):
        log("当前已联网, 无需登录")
        return True

    host, port, page_config, portal_text = portal_ready(cfg)
    global _FOREIGN_LOGGED
    allowed, reason = security_check(cfg, host, portal_text)
    if not allowed:
        if not _FOREIGN_LOGGED:      # 同一个陌生网络只提示一次, 不刷屏
            log("安全检查未通过: %s" % reason)
            log("当前网络可能不是校园网, 为避免账号密码泄露到别的门户, 已放弃登录")
            log("(确实要在该网络登录, 可修改 config.ini 的 [security] allow_cidrs)")
            _FOREIGN_LOGGED = True
        return "foreign"
    _FOREIGN_LOGGED = False
    login_method = int(page_config.get("login_method") or 0)
    if login_method != 1:
        raise PortalError("该门户认证方式为 login_method=%s, 脚本目前只支持账号密码直连(1)" % login_method)
    if int(page_config.get("en_md5") or 0):
        raise PortalError("该门户启用了 MD5 加密认证, 需要门户额外的密钥参数, 请在 issue 里反馈")

    account = build_account(cfg, page_config)
    ip = local_ip()
    timeout = cfg_int(cfg, "runtime", "timeout")
    params = {
        "login_method": login_method,
        "user_account": account,
        "user_password": password,
        "wlan_user_ip": ip,
        "wlan_user_ipv6": "",
        "wlan_user_mac": cfg_str(cfg, "runtime", "mac") or "000000000000",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "terminal_type": "1",
        "lang": "zh-cn",
        "jsVersion": JS_VERSION,
        "callback": "dr1003",
    }
    url = "http://%s:%d/eportal/portal/login" % (host, port)
    if dry_run:
        masked = dict(params, user_password="***")
        log("[dry-run] 门户=%s:%d login_method=%s" % (host, port, login_method))
        log("[dry-run] 账号=%s 本机IP=%s" % (account, ip))
        log("[dry-run] GET %s?%s" % (url, urllib.parse.urlencode(masked)))
        return None

    log("正在登录: 账号=%s 门户=%s:%d" % (account, host, port))
    result = jsonp_get(url, params, timeout)
    msg = str(result.get("msg") or "")
    # 门户对同一 IP 的重复登录会返回 result=0 + “已经在线”, 这种情况按成功处理
    ok = str(result.get("result")) in ("1", "ok", "True") or "已在线" in msg or "已经在线" in msg
    if ok:
        log("登录请求已被接受: %s" % msg)
        time.sleep(cfg_int(cfg, "runtime", "verify_delay"))
        if is_online(cfg):
            log("联网确认成功")
            notify(cfg, "校园网已自动登录", "账号 %s" % username)
            return True
        log("提示: 登录返回成功, 但联网检测尚未通过 (网关生效可能有延迟)")
        return True
    log("登录失败: %s (result=%s)" % (msg, result.get("result")))
    notify(cfg, "校园网自动登录失败", msg)
    if classify_auth_error(msg):
        raise AuthError(msg)
    return False


def do_logout(cfg):
    host, port, page_config, portal_text = portal_ready(cfg)
    allowed, reason = security_check(cfg, host, portal_text)
    if not allowed:
        log("安全检查未通过: %s" % reason)
        return False
    timeout = cfg_int(cfg, "runtime", "timeout")
    url = "http://%s:%d/eportal/portal/logout" % (host, port)
    # 门户的注销是按 IP 注销, 账号字段是固定的 drcom/123
    params = {
        "login_method": page_config.get("login_method", 1),
        "user_account": "drcom",
        "user_password": "123",
        "ac_logout": page_config.get("ac_logout", 0),
        "register_mode": page_config.get("register_mode", 1),
        "wlan_vlan_id": page_config.get("cvlan_id", 4095),
        "wlan_user_ip": local_ip(),
        "wlan_user_ipv6": "",
        "wlan_user_mac": cfg_str(cfg, "runtime", "mac") or "000000000000",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion": JS_VERSION,
        "callback": "dr1003",
    }
    result = jsonp_get(url, params, timeout)
    log("注销结果: %s" % result)
    return str(result.get("result")) in ("1", "ok")


def notify(cfg, title, body):
    if not cfg_bool(cfg, "runtime", "notify"):
        return
    title = str(title).replace('"', "'").replace("'", "''")
    body = str(body).replace('"', "'").replace("'", "''")
    try:
        if IS_MACOS:
            subprocess.run(["osascript", "-e",
                            'display notification "%s" with title "%s"' % (body, title)],
                           timeout=10, check=False)
        elif IS_WINDOWS:
            script = ("Add-Type -AssemblyName System.Windows.Forms;"
                      "$n=New-Object System.Windows.Forms.NotifyIcon;"
                      "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                      "$n.ShowBalloonTip(5000,'%s','%s');Start-Sleep -Seconds 5" % (title, body))
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           timeout=20, check=False)
        else:
            if not os.environ.get("DISPLAY") or not shutil.which("notify-send"):
                return
            subprocess.run(["notify-send", "-i", "network-wired", title, body], timeout=5, check=False)
    except Exception:
        pass


def login_with_retry(cfg):
    retry = max(cfg_int(cfg, "runtime", "retry"), 1)
    delay = max(cfg_int(cfg, "runtime", "retry_delay"), 1)
    for attempt in range(1, retry + 1):
        try:
            result = do_login(cfg)
            if result is True:
                return True
            if isinstance(result, str):
                return result
        except PortalUnavailable as exc:
            log("门户暂时不可用: %s" % exc)
            delay = max(delay, 15)
        except AuthError as exc:
            log("账号/密码问题, 不再重试(避免触发锁定): %s" % exc)
            return "auth"
        except PortalError as exc:
            log("登录出错: %s" % exc)
            return False
        except Exception as exc:
            log("登录异常: %s: %s" % (type(exc).__name__, exc))
        if attempt < retry:
            wait = delay * attempt
            log("第 %d 次登录未成功, %d 秒后重试" % (attempt, wait))
            time.sleep(wait)
    return False


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------
def cmd_check(cfg, args):
    verbose = getattr(args, "verbose", False)
    online = is_online(cfg, verbose=verbose)
    if verbose:
        status = portal_status(cfg, verbose=True)
        if status is not None:
            log("门户状态: result=%s uid=%s ip=%s" % (status.get("result"), status.get("uid"), status.get("v4ip")))
        log("已联网" if online else "未联网, 需要认证")
    else:
        print("online" if online else "offline")
    return 0 if online else 1


def cmd_login(cfg, args):
    result = do_login(cfg, force=args.force, dry_run=args.dry_run)
    if result is None:          # dry-run
        return 0
    if result is True:
        return 0
    if result == "foreign":
        return 3                # 网络不在白名单内, 主动放弃(不是失败)
    return 1


def cmd_watch(cfg, args):
    # 每轮重新读配置, 改完 config.ini 立即生效, 不用重启服务
    config_path = find_config(getattr(args, "config", None))
    interval = cfg_int(cfg, "runtime", "interval")
    log("守护启动: 每 %d 秒检查一次, 配置文件 %s" % (interval, config_path))
    state = None
    while True:
        try:
            cfg = load_config(config_path)
            reset_discovery_cache()
            if is_online(cfg):
                if state in (False, "foreign", "auth"):
                    log("网络已恢复")
                state = True
            else:
                if state != "foreign":
                    log("检测到未联网, 开始自动登录")
                result = login_with_retry(cfg)
                if result == "foreign":
                    if state != "foreign":
                        log("已暂停自动登录: 当前不在校园网, 不会把账号密码发到其他网络的门户")
                    state = "foreign"
                elif result == "auth":
                    state = "auth"
                    pause = cfg_int(cfg, "runtime", "auth_error_interval")
                    log("账号信息有问题, 暂停 %d 秒后再试 (请检查 config.ini)" % pause)
                    time.sleep(pause)
                    continue
                elif result and is_online(cfg):
                    log("网络已恢复")
                    state = True
                else:
                    log("网络仍未连通, %d 秒后重试" % interval)
                    state = False
            time.sleep(interval)
        except KeyboardInterrupt:
            log("已退出")
            return 0
        except Exception as exc:
            log("守护异常: %s: %s" % (type(exc).__name__, exc))
            time.sleep(interval)


def cmd_status(cfg, args):
    status = portal_status(cfg, verbose=True)
    if status is None:
        log("门户不可达 (可能不在校园网内)")
        return 2
    if str(status.get("result")) == "1":
        log("在线: 账号=%s IP=%s 上线时间=%s 已用时长=%s 已用流量=%sMB"
            % (status.get("uid"), status.get("v4ip"), status.get("stime"),
               status.get("time"), int(status.get("flow") or 0) // 1048576))
        return 0
    log("离线 (门户 result=%s)" % status.get("result"))
    return 1


def cmd_discover(cfg, args):
    host, port = resolve_portal(cfg, force=True)
    page_config, portal_text = portal_page_config(cfg, host, port)
    allowed, reason = security_check(cfg, host, portal_text)
    log("安全检查: %s" % ("通过" if allowed else "未通过 -> %s" % reason))
    log("门户: %s:%d" % (host, port))
    log("登录方式 login_method=%s, 账号前缀 account_prefix=%s, md5=%s"
        % (page_config.get("login_method"), page_config.get("account_prefix"), page_config.get("en_md5")))
    log("可以把 host/port 填进 config.ini 的 [portal], 加快下次登录")
    return 0


def cmd_logout(cfg, args):
    return 0 if do_logout(cfg) else 1


def build_parser():
    # 子命令用 SUPPRESS 默认值, 避免覆盖 "-c" 这种写在子命令前面的全局参数
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help="配置文件路径")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    common.add_argument("--log-file", default=argparse.SUPPRESS, help="同时把日志追加到文件(Windows 计划任务用)")

    parser = argparse.ArgumentParser(description="校园网自动登录 (Dr.COM eportal)", parents=[common])
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("check", help="检查是否已联网", parents=[common])
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("login", help="登录一次", parents=[common])
    p.add_argument("--force", action="store_true", help="已在线也重新登录")
    p.add_argument("--dry-run", action="store_true", help="只打印将要发送的请求, 不真正登录")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("watch", help="常驻守护, 掉线自动登录", parents=[common])
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("status", help="查看门户在线状态", parents=[common])
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("discover", help="自动发现认证门户", parents=[common])
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("logout", help="注销当前账号", parents=[common])
    p.set_defaults(func=cmd_logout)
    return parser


def main(argv=None):
    global LOG_FILE
    parser = build_parser()
    args = parser.parse_args(argv)
    LOG_FILE = getattr(args, "log_file", None) or os.environ.get("NETLOGIN_LOG")
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    cfg = load_config(getattr(args, "config", None))
    try:
        return args.func(cfg, args) or 0
    except PortalError as exc:
        log("错误: %s" % exc)
        return 2
    except Exception as exc:
        log("未预期异常: %s: %s" % (type(exc).__name__, exc))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
