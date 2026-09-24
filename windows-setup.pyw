#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校园网自动登录 —— Windows 图形化配置

双击运行, 填好学号/密码点“保存并登录”: 写入 config.ini 后立刻登录一次, 过程显示在窗口里。
可选同时安装开机自启(任务计划程序)。只用 Python 标准库(tkinter), 不需要额外依赖。
"""
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

APP_TITLE = "校园网自动登录 (深大 Dr.COM)"
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

DEFAULT_TEMPLATE = """[account]
username =
password =
suffix =

[portal]
host = 172.30.255.42
port = 801
account_prefix = auto

[security]
allow_cidrs = 172.30.0.0/16
expect_keywords = szu

[runtime]
probe_urls = http://cp.cloudflare.com/generate_204, http://www.gstatic.com/generate_204, http://www.baidu.com/
timeout = 8
interval = 15
retry = 3
retry_delay = 5
verify_delay = 2
auth_error_interval = 600
mac = 000000000000
notify = false
"""


def here():
    return os.path.dirname(os.path.abspath(sys.argv[0] or __file__))


def find_config():
    for path in (os.path.join(os.environ.get("APPDATA", ""), "net-login", "config.ini"),
                 os.path.join(here(), "config.ini")):
        if path and os.path.isfile(path):
            return path
    return os.path.join(here(), "config.ini")


def find_script():
    for path in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "net-login", "net-login.py"),
                 os.path.join(here(), "auto-login.py")):
        if path and os.path.isfile(path):
            return path
    return os.path.join(here(), "auto-login.py")


def find_python():
    if sys.executable and os.path.basename(sys.executable).lower().startswith("python"):
        return sys.executable
    return "python"


def read_credentials(path):
    user = pwd = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        m = re.search(r"(?m)^\s*username\s*=(.*)$", text)
        if m:
            user = m.group(1).strip()
        m = re.search(r"(?m)^\s*password\s*=(.*)$", text)
        if m:
            pwd = m.group(1).strip()
    return user, pwd


def _set_key(text, section, key, value):
    pattern = re.compile(r"(?m)^\s*%s\s*=.*$" % re.escape(key))
    line = "%s = %s" % (key, value)
    if pattern.search(text):
        return pattern.sub(lambda _m: line, text, count=1)
    head = re.compile(r"(?m)^\s*\[%s\]\s*$" % re.escape(section))
    m = head.search(text)
    if m:
        return text[:m.end()] + "\n" + line + text[m.end():]
    return text.rstrip("\n") + "\n\n[%s]\n%s\n" % (section, line)


def write_credentials(path, user, pwd):
    text = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    if not text.strip():
        text = DEFAULT_TEMPLATE
    text = _set_key(text, "account", "username", user)
    text = _set_key(text, "account", "password", pwd)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def stream_command(cmd, on_line, cwd=None):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                cwd=cwd, creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError as exc:
        on_line("无法执行: %s (%s)" % (cmd[0], exc))
        return 127
    for line in proc.stdout:
        on_line(line.rstrip())
    return proc.wait()


class SetupWindow:
    def __init__(self, root):
        self.root = root
        self.config_path = find_config()
        self.script_path = find_script()
        self.installer = os.path.join(here(), "install-windows.ps1")
        self.busy = False

        root.title(APP_TITLE)
        root.minsize(640, 470)
        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="校园网账号(学号)").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 6))
        self.user = ttk.Entry(frame, width=30)
        self.user.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="密码").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=(0, 6))
        self.pwd = ttk.Entry(frame, width=30, show="*")
        self.pwd.grid(row=1, column=1, sticky="ew", pady=(0, 6))

        self.show_pwd = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="显示密码", variable=self.show_pwd,
                        command=self.toggle_pwd).grid(row=1, column=2, sticky="w", padx=(10, 6))

        self.autostart = tk.BooleanVar(value=False)
        self.auto_check = ttk.Checkbutton(frame, text="同时设置开机/登录后自动运行",
                                          variable=self.autostart)
        self.auto_check.grid(row=2, column=1, columnspan=2, sticky="w", pady=(4, 12))
        if not os.path.isfile(self.installer):
            self.auto_check.state(["disabled"])

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.login_btn = ttk.Button(buttons, text="保存并登录", command=self.save_and_login)
        self.login_btn.pack(side="left")
        ttk.Button(buttons, text="仅保存", command=self.save_only).pack(side="left", padx=8)
        ttk.Button(buttons, text="清空日志", command=self.clear_log).pack(side="left")

        self.log = tk.Text(frame, width=58, height=13, wrap="word", state="disabled",
                           background="#111", foreground="#ddd", insertbackground="#ddd")
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        scroll.grid(row=4, column=3, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        self.status = ttk.Label(frame, text="配置文件: %s" % self.config_path, foreground="#555")
        self.status.grid(row=5, column=0, columnspan=4, sticky="w", pady=(10, 0))

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)

        user, pwd = read_credentials(self.config_path)
        if user:
            self.user.insert(0, user)
        if pwd:
            self.pwd.insert(0, pwd)
        self.write("账号密码会写入: %s" % self.config_path)
        self.write("登录脚本: %s" % self.script_path)
        self.write("填好账号密码后点“保存并登录”。")

    def toggle_pwd(self):
        self.pwd.configure(show="" if self.show_pwd.get() else "*")

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", str(text) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def save_only(self):
        try:
            write_credentials(self.config_path, self.user.get().strip(), self.pwd.get())
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "写入配置失败: %s" % exc)
            return False
        self.status.configure(text="已保存到 %s" % self.config_path, foreground="#0a0")
        self.write("已保存账号密码到 %s" % self.config_path)
        return True

    def save_and_login(self):
        if self.busy:
            return
        user = self.user.get().strip()
        pwd = self.pwd.get()
        if not user or not pwd:
            messagebox.showwarning(APP_TITLE, "请先填写账号和密码")
            return
        if not self.save_only():
            return
        self.busy = True
        self.login_btn.state(["disabled"])
        self.status.configure(text="正在登录 ...", foreground="#555")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        if self.autostart.get() and os.path.isfile(self.installer):
            self.ui(self.write, "---- 安装开机自启 ----")
            code = stream_command(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                   "-File", self.installer], lambda line: self.ui(self.write, line),
                                  cwd=here())
            self.ui(self.write, "安装脚本退出码: %s" % code)
        self.ui(self.write, "---- 登录 ----")
        code = stream_command([find_python(), self.script_path, "--config", self.config_path,
                               "login", "--force", "--verbose"],
                              lambda line: self.ui(self.write, line))
        ok = (code == 0)
        self.ui(self.write, "登录%s (退出码 %s)" % ("成功" if ok else "失败", code))
        self.ui(self._finish, ok)

    def ui(self, fn, *args):
        self.root.after(0, lambda: fn(*args))

    def _finish(self, ok):
        self.busy = False
        self.login_btn.state(["!disabled"])
        self.status.configure(text="已登录" if ok else "登录失败, 请看窗口日志",
                              foreground="#0a0" if ok else "#c00")
        if ok:
            messagebox.showinfo(APP_TITLE, "登录成功, 现在可以上网了。")
        else:
            messagebox.showerror(APP_TITLE, "登录没有成功, 请把窗口里的日志发给维护者。")


def main():
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    SetupWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
