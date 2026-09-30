#!/usr/bin/env python3
"""
一键启动脚本：启动 Web 控制台（支持网页端配置模型与扫码绑定）并自动打开浏览器
使用方式:
    python3 start.py
"""
import os
import sys
import time
import shutil
import subprocess
import webbrowser
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
PORT = int(os.getenv("WEB_PORT", "8765"))
HOST = os.getenv("WEB_HOST", "127.0.0.1")
URL = f"http://{HOST}:{PORT}"

BANNER = """
============================================================
       🧾 个人单据整理助手 (WeChat Receipt Agent)
   - 端到端多模态单据解析 · 只读 SQL 防幻觉 · 微信智能归档 -
============================================================
"""

def main():
    print(BANNER)
    print(f"[*] 工作目录: {ROOT_DIR}")

    # 1. 检查数据目录
    data_dir = ROOT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 2. 启动 Python Web 服务
    server_script = ROOT_DIR / "web" / "server.py"
    if not server_script.exists():
        print(f"[!] 找不到 Web 服务脚本: {server_script}")
        sys.exit(1)

    print(f"[*] 正在启动控制台服务: {URL} ...")
    # -u 关掉 stdout 缓冲: 日志被重定向到文件时，不加这个会攒在 8KB 缓冲区里，
    # 服务明明在跑、日志文件却半天不更新（实测 server.log 停在两天前）。
    web_proc = subprocess.Popen([sys.executable, "-u", str(server_script)], cwd=str(ROOT_DIR))

    # 等待服务就绪
    time.sleep(1.2)

    # 3. 自动在浏览器中打开页面
    print(f"[+] 控制台已就绪，正在打开浏览器: {URL}")
    print("[+] 提示: 你可以在浏览器页面中直接配置模型 API Key 与查看微信扫码连接")
    try:
        webbrowser.open(URL)
    except Exception:
        pass

    # 微信端接入服务已由 server.py 统一自动托管启动，无需在此重复拉起

    print("\n[✓] 系统运行中。按 Ctrl+C 退出服务。\n")
    try:
        web_proc.wait()
    except KeyboardInterrupt:
        print("\n[*] 正在关闭服务...")
        web_proc.terminate()
        print("[*] 已安全退出。")

if __name__ == "__main__":
    main()
