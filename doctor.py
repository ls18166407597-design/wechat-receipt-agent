#!/usr/bin/env python3
"""演示前自检 —— 一条命令确认现在能不能拿出去演示。

检查四件事，任何一项挂了都会明确告诉你：
  1. Web 服务可达（机器人靠它干活）
  2. 模型真的通（真发一次请求，不是看端口开着就算数）
  3. 库里还有没有单据（演示时查不到东西最尴尬）
  4. 机器人绑没绑微信、手里的二维码还剩多久

在服务器上跑:
    cd /opt/wechat-doc-agent && .venv/bin/python doctor.py

本地隔着 SSH 隧道检查:
    python3 doctor.py --url http://127.0.0.1:8899

退出码 0 = 可以演示，1 = 有检查项没过。
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

DEFAULT_URL = "http://127.0.0.1:8765"
QR_TTL_SECONDS = 120          # 实测微信侧二维码约 121 秒作废
WEB_SERVICE = "wechat-doc-agent-web"
BOT_SERVICE = "wechat-doc-agent-bot"

OK, BAD, WARN, SKIP = "✅", "❌", "⚠️ ", "⏭ "


# 显式绕开系统代理再连本机。macOS 的系统代理（实测 http://127.0.0.1:1082）会把
# 连 127.0.0.1 的请求也一并拦下，于是"服务没起来"会伪装成 HTTP 503，
# 排查方向完全被带偏。检查本机服务就该直连。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, timeout: float = 5.0) -> dict:
    with _LOCAL_OPENER.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _systemd_available() -> bool:
    return bool(shutil.which("systemctl")) and Path("/run/systemd/system").exists()


def check_services() -> bool:
    """只在服务器上有意义；本地跑直接跳过"""
    if not _systemd_available():
        print(f"{SKIP} systemd 不可用（本地环境），跳过服务状态检查")
        return True
    ok = True
    for svc in (WEB_SERVICE, BOT_SERVICE):
        try:
            state = subprocess.run(["systemctl", "is-active", svc],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception as e:
            state = f"查询失败: {e}"
        if state == "active":
            print(f"{OK} 服务运行中: {svc}")
        else:
            print(f"{BAD} 服务异常: {svc} → {state}")
            if svc == BOT_SERVICE:
                print(f"     看日志: journalctl -u {svc} -n 30")
            ok = False
    return ok


def check_web(url: str) -> bool:
    try:
        data = _get(f"{url}/api/status")
    except Exception as e:
        print(f"{BAD} Web 服务不可达: {url} → {e}")
        print("     服务器上: systemctl restart wechat-doc-agent-web")
        print("     本地检查: 确认 SSH 隧道还开着")
        return False
    print(f"{OK} Web 服务可达: {url}")
    return True


def check_model() -> bool:
    """真发一次请求。注意走的是 LLM 客户端本身，不经过 /api/message，
    所以不会往演示的对话历史里塞测试消息。"""
    try:
        from core.llm_client import OpenAICompatibleClient
        client = OpenAICompatibleClient()
    except Exception as e:
        print(f"{BAD} 模型配置加载失败: {e}")
        return False

    t0 = time.time()
    resp = client.chat_completion(
        [{"role": "user", "content": "回复两个字：收到"}], max_tokens=512)
    cost = time.time() - t0

    if resp.get("status") != "success":
        print(f"{BAD} 模型调用失败（{cost:.1f}s）: {str(resp.get('message'))[:160]}")
        print("     模型服务不由本项目控制，演示前务必先解决")
        return False
    print(f"{OK} 模型连通 {client.model}（{cost:.1f}s）→ {str(resp.get('content'))[:20]!r}")
    return True


def check_documents() -> bool:
    try:
        from core.db import DatabaseManager
        db = DatabaseManager()
        rows = db.query_documents(
            "SELECT COUNT(*) AS n, SUM(main_amount) AS total FROM documents")
    except Exception as e:
        print(f"{BAD} 读数据库失败: {e}")
        return False

    n = (rows[0].get("n") if rows else 0) or 0
    total = (rows[0].get("total") if rows else 0) or 0
    if n == 0:
        print(f"{BAD} 库里一张单据都没有，演示会查不到东西")
        print("     灌入样例: .venv/bin/python seed_demo_data.py --reset")
        return False
    print(f"{OK} 已归档 {n} 张单据，合计 ¥{total:,.2f}")
    return True


def check_wechat(url: str) -> bool:
    try:
        status = _get(f"{url}/api/status")
    except Exception:
        print(f"{BAD} 读不到机器人状态")
        return False

    state = status.get("status")
    if state == "connected":
        print(f"{OK} 微信已连接，随时可以聊")
        return True

    if state == "scanned":
        print(f"{WARN}已扫码，等手机上点【确认登录】")
        return False

    # waiting_scan / 未知：给一张能直接用的码就是可用状态
    issued = status.get("qrcode_issued_at")
    if not issued:
        print(f"{WARN}还没绑定微信，页面上也没有二维码")
        print("     打开扫码页点「重新签发二维码」")
        return False

    left = int(QR_TTL_SECONDS - (time.time() - issued))
    if left > 30:
        print(f"{WARN}还没绑定微信；当前二维码还剩约 {left} 秒，可随时发给人扫")
        return True
    print(f"{WARN}还没绑定微信；当前二维码只剩约 {left} 秒，别发出去")
    print("     等页面换出一张新码再发（二维码约 2 分钟就换）")
    return False


def print_config() -> None:
    """把**实际生效**的调优值打出来。

    为什么要这一步: 这些值只写在 core/config.py 里（.env 不覆盖它们，理由见那个文件
    开头"一个值只有一个家"）。但配置散在 .env(三样密钥) 与 config.py(其余全部) 两处，
    排查时很容易记错哪边生效——打出来就一眼看得见，不用猜。
    """
    from core.config import Config
    print(f"生效配置  窗口 {Config.RECENT_RAW_BUFFER_ROUNDS} 轮 / 整理阈值 "
          f"{Config.MAX_UNCOMPRESSED_ROUNDS} 轮 | 输出上限 {Config.MAX_OUTPUT_TOKENS} | "
          f"PDF 读前 {Config.PDF_MAX_SAMPLE_PAGES} 页")
    print("-" * 52)


def main() -> int:
    parser = argparse.ArgumentParser(description="演示前自检")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"Web 服务地址（默认 {DEFAULT_URL}；隔着隧道时填隧道那头）")
    args = parser.parse_args()

    print("=" * 52)
    print("  个人单据整理助手 · 演示前自检")
    print("=" * 52)
    print_config()

    results = [
        ("服务状态", check_services()),
        ("Web 可达", check_web(args.url)),
        ("模型连通", check_model()),
        ("单据数据", check_documents()),
        ("微信绑定", check_wechat(args.url)),
    ]

    failed = [name for name, passed in results if not passed]
    print("-" * 52)
    if failed:
        print(f"结论：还不能演示 —— {'、'.join(failed)} 有问题")
        return 1
    print("结论：可以演示 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
