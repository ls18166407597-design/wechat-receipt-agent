"""
Agent 后端 Web 服务: 提供统一消息处理接口与扫码页面
"""
import http.server
import socketserver
import json
import os
import shutil
import subprocess
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

# 将项目根目录加入模块搜索路径
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core.agent_runner import AgentRunner
from core.db import DatabaseManager

PORT = int(os.getenv("WEB_PORT", "8765"))
# 只监听本机回环。扫码页等于机器人的绑定入口，谁拿到链接谁就能绑。
# 服务器上演示不需要暴露端口——走 SSH 隧道即可：
#     ssh -N -L 8899:127.0.0.1:8765 myserver   # 本地开 http://127.0.0.1:8899/qrcode
# 想让别人扫码就把二维码截个图发给他，不必为此改这个值。见 服务器部署.md。
# WEB_HOST 只在确有需要时改；改成非回环地址时，下面的启动日志会明确报出对外监听的后果。
HOST = os.getenv("WEB_HOST", "127.0.0.1")
# 「重新签发二维码」需要重启的机器人服务（systemd 单元名）
BOT_SERVICE = os.getenv("BOT_SERVICE", "wechat-doc-agent-bot")
# 认机器人进程用的模式: 必须是"node 跑这个脚本"。不要用裸 "wechat_bot.js"——
# 那是全命令行子串匹配，任何命令行里碰巧提到这个文件名的进程都会被算进去
# （编辑器打开它、grep 它），既会让自启动被误判成"已在运行"而跳过，也会让
# stop 时的 pkill 误杀无关进程。
BOT_PATTERN = r"node.*wechat_bot\.js"
QRCODE_FILE = ROOT_DIR / "data" / "qrcode_url.txt"
REISSUE_FLAG = ROOT_DIR / "data" / ".reissue-qr"
CRED_FILE = ROOT_DIR / "data" / "wx-credentials.json"
runner = AgentRunner()

# 全局微信机器人子进程引用
_BOT_PROC = None

# 本服务是多线程的，而 AgentRunner 是有状态的：它持有一份共享的会话历史，
# 每轮开头还会把 self.last_usage 清零。两条消息同时在处理时会互相覆盖用量、
# 交错写库，甚至让后一条的模型读到前一条刚插进去的消息（对话串味）。
# 单用户场景下一次处理只要几秒，串行完全够用，所以直接加锁把处理排队。
_AGENT_LOCK = threading.Lock()


def get_node_bin() -> str | None:
    """寻找系统的 Node.js 可执行文件路径"""
    node_path = shutil.which("node")
    if node_path:
        return node_path
    for candidate in ["/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node"]:
        if Path(candidate).exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def is_bot_running() -> bool:
    """检查微信机器人进程是否正在运行"""
    global _BOT_PROC
    if _BOT_PROC is not None and _BOT_PROC.poll() is None:
        return True
    try:
        out = subprocess.run(["pgrep", "-f", BOT_PATTERN], capture_output=True, text=True)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def _auto_start_bot() -> None:
    """启动时自动拉起机器人，**并把结果打出来**。

    start_bot_process() 的返回值原先被线程直接丢掉，失败完全无声——实测踩到过:
    某次重启后机器人没起来，server.log 里一个字的线索都没有，/api/status 只显示
    bot_not_running，只能靠手动 POST /api/start-bot 才拿到那句失败原因。
    """
    ok, msg = start_bot_process()
    print(f"[Python Agent Server] 自动启动微信服务: {'成功' if ok else '失败'} —— {msg}")


def start_bot_process() -> tuple[bool, str]:
    """一键启动微信机器人服务"""
    global _BOT_PROC
    if is_bot_running():
        return True, "微信服务已在运行中"

    node_bin = get_node_bin()
    if not node_bin:
        return False, "未检测到 Node.js 环境。请确保已安装 Node.js (https://nodejs.org)"

    bot_script = ROOT_DIR / "wechat_bot.js"
    if not bot_script.exists():
        return False, f"未找到机器人脚本: {bot_script}"

    try:
        log_file = ROOT_DIR / "data" / "bot.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(log_file, "a", encoding="utf-8")
        _BOT_PROC = subprocess.Popen(
            [node_bin, str(bot_script)],
            cwd=str(ROOT_DIR),
            stdout=log_fp,
            stderr=log_fp,
            start_new_session=True
        )
        return True, "微信服务已启动"
    except Exception as e:
        return False, f"启动微信服务失败: {e}"


def stop_bot_process() -> tuple[bool, str]:
    """停止微信机器人服务"""
    global _BOT_PROC
    if _BOT_PROC is not None:
        try:
            _BOT_PROC.terminate()
            _BOT_PROC.wait(timeout=3)
        except Exception:
            pass
        _BOT_PROC = None
    try:
        subprocess.run(["pkill", "-f", BOT_PATTERN], capture_output=True)
    except Exception:
        pass
    return True, "微信服务已停止"


def reissue_qr_process() -> tuple[bool, str]:
    """重新签发二维码：支持 Linux systemd 或本地多端子进程自管理"""
    # 1. 如果在 Linux 服务器环境上且 systemd 托管可用
    if shutil.which("systemctl") is not None:
        try:
            status = subprocess.run(["systemctl", "is-active", BOT_SERVICE], capture_output=True, text=True)
            if status.returncode == 0:
                REISSUE_FLAG.parent.mkdir(parents=True, exist_ok=True)
                REISSUE_FLAG.write_text("1", encoding="utf-8")
                QRCODE_FILE.unlink(missing_ok=True)
                subprocess.run(["systemctl", "restart", BOT_SERVICE], check=True, capture_output=True, timeout=15)
                return True, "已通知后台服务重新签发，新二维码即将生成"
        except Exception:
            pass

    # 2. 本地开发/体验环境: 自主重启子进程
    REISSUE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    REISSUE_FLAG.write_text("1", encoding="utf-8")
    QRCODE_FILE.unlink(missing_ok=True)
    stop_bot_process()
    time.sleep(0.6)
    ok, msg = start_bot_process()
    if ok:
        return True, "已重新签发二维码，新二维码生成中..."
    return False, f"重新签发失败: {msg}"


def _log_usage(usage: dict) -> None:
    """打印本轮 token 用量、缓存命中率与上下文深度 (缓存命中单价约为未命中的 1/50)

    上下文轮数单独打出来，是因为它和缓存命中率是同一件事的两面: 轮数落在窗口内
    说明前缀只增不改、缓存该命中；如果轮数明显小于窗口，说明窗口被非对话帧占了。
    """
    prompt = usage.get("prompt_tokens", 0)
    if not prompt:
        return
    hit = usage.get("cache_hit_tokens", 0)
    print(f"[Agent Web] tokens: 输入={prompt} 缓存命中={hit}({hit / prompt * 100:.0f}%) "
          f"输出={usage.get('completion_tokens', 0)}(含推理 {usage.get('reasoning_tokens', 0)}) "
          f"上下文={usage.get('context_rounds', 0)} 轮")

def get_config_dict() -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "")
    masked_key = ""
    if api_key:
        if len(api_key) > 8:
            masked_key = api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:]
        else:
            masked_key = "****"
    return {
        "apiKeyMasked": masked_key,
        "hasApiKey": bool(api_key),
        "baseUrl": os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
        "modelName": os.getenv("MODEL_NAME", "deepseek-flash"),
    }


def update_config_data(new_key: str, new_url: str, new_model: str) -> None:
    env_file = ROOT_DIR / ".env"
    lines = []
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()

    updated = {"OPENAI_API_KEY": False, "OPENAI_BASE_URL": False, "MODEL_NAME": False}
    new_lines = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _ = s.split("=", 1)
            k = k.strip()
            if k == "OPENAI_API_KEY":
                if new_key:
                    new_lines.append(f"OPENAI_API_KEY={new_key.strip()}")
                    os.environ["OPENAI_API_KEY"] = new_key.strip()
                else:
                    new_lines.append(line)
                updated["OPENAI_API_KEY"] = True
                continue
            elif k == "OPENAI_BASE_URL":
                val = (new_url or "https://api.deepseek.com/v1").strip().rstrip("/")
                new_lines.append(f"OPENAI_BASE_URL={val}")
                os.environ["OPENAI_BASE_URL"] = val
                updated["OPENAI_BASE_URL"] = True
                continue
            elif k == "MODEL_NAME":
                val = (new_model or "deepseek-flash").strip()
                new_lines.append(f"MODEL_NAME={val}")
                os.environ["MODEL_NAME"] = val
                updated["MODEL_NAME"] = True
                continue
        new_lines.append(line)

    if not updated["OPENAI_API_KEY"] and new_key:
        new_lines.append(f"OPENAI_API_KEY={new_key.strip()}")
        os.environ["OPENAI_API_KEY"] = new_key.strip()
    if not updated["OPENAI_BASE_URL"]:
        val = (new_url or "https://api.deepseek.com/v1").strip().rstrip("/")
        new_lines.append(f"OPENAI_BASE_URL={val}")
        os.environ["OPENAI_BASE_URL"] = val
    if not updated["MODEL_NAME"]:
        val = (new_model or "deepseek-flash").strip()
        new_lines.append(f"MODEL_NAME={val}")
        os.environ["MODEL_NAME"] = val

    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    from core.config import Config
    from core.llm_client import OpenAICompatibleClient
    Config.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    Config.OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
    Config.MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-flash")
    runner.llm = OpenAICompatibleClient(
        api_key=Config.OPENAI_API_KEY,
        base_url=Config.OPENAI_BASE_URL,
        model=Config.MODEL_NAME,
    )


class AgentServerHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, payload: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):
        if self.path == "/api/start-bot":
            ok, msg = start_bot_process()
            self._send_json({"ok": ok, "message": msg})
            return

        if self.path == "/api/stop-bot":
            ok, msg = stop_bot_process()
            self._send_json({"ok": ok, "message": msg})
            return

        if self.path == "/api/reissue-qr":
            ok, msg = reissue_qr_process()
            self._send_json({"ok": ok, "message": msg})
            return

        if self.path == "/api/config":
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len).decode("utf-8")
            try:
                data = json.loads(post_body)
                update_config_data(
                    new_key=data.get("apiKey", ""),
                    new_url=data.get("baseUrl", ""),
                    new_model=data.get("modelName", "")
                )
                self._send_json({"ok": True, "message": "配置已保存并即时生效"})
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            return

        if self.path == "/api/message":
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len).decode("utf-8")
            try:
                data = json.loads(post_body)
                text = data.get("text", "")
                file_path = data.get("filePath")
                
                if file_path:
                    print(f"[Agent Web] 收到来自微信用户的附件/图片: {file_path}, 附带文字: {text}")
                else:
                    print(f"[Agent Web] 收到来自微信用户的消息: {text}")

                with _AGENT_LOCK:
                    if file_path:
                        reply = runner.handle_document_upload(file_path, user_text=text)
                    else:
                        reply = runner.handle_user_message(text)
                    usage = dict(runner.last_usage)   # 在锁内取走，避免被下一轮清零
                    usage["context_rounds"] = runner.compressor.last_context_rounds

                _log_usage(usage)
                print(f"[Agent Web] 模型回复: {reply[:60]}...")

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"reply": reply}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/api/config":
            self._send_json(get_config_dict())
            return

        if self.path == "/api/status":
            bot_running = is_bot_running()
            status_data = {"status": "bot_not_running" if not bot_running else "waiting_scan"}
            status_file = ROOT_DIR / "data" / "bot_status.json"
            if bot_running and status_file.exists():
                try:
                    data = json.loads(status_file.read_text(encoding="utf-8"))
                    status_data["status"] = data.get("status", "waiting_scan")
                except Exception:
                    pass

            if QRCODE_FILE.exists():
                status_data["qrcode"] = QRCODE_FILE.read_text(encoding="utf-8").strip()
                status_data["qrcode_issued_at"] = QRCODE_FILE.stat().st_mtime
            self._send_json(status_data)
            return

        if self.path in ["/", "/qrcode", "/login"]:
            qr_url = ""
            qr_issued_at = 0
            if QRCODE_FILE.exists():
                qr_url = QRCODE_FILE.read_text(encoding="utf-8").strip()
                qr_issued_at = QRCODE_FILE.stat().st_mtime

            html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>个人单据整理助手 - 控制台</title>
  <script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; background: #f0f2f5; color: #1f2937; padding: 30px 15px; }}
    .container {{ max-width: 860px; margin: 0 auto; }}
    .header {{ text-align: center; margin-bottom: 25px; }}
    .header h1 {{ font-size: 22px; font-weight: 700; margin: 0 0 6px; color: #111827; }}
    .header p {{ font-size: 13px; color: #6b7280; margin: 0; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    @media (max-width: 768px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .card {{ background: #fff; padding: 24px; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); display: flex; flex-direction: column; }}
    .card-title {{ font-size: 15px; font-weight: 600; margin-bottom: 6px; display: flex; align-items: center; justify-content: space-between; }}
    .card-desc {{ font-size: 12px; color: #6b7280; margin-bottom: 18px; line-height: 1.5; }}
    .form-group {{ margin-bottom: 14px; text-align: left; }}
    .form-group label {{ display: block; font-size: 12px; font-weight: 500; color: #374151; margin-bottom: 5px; }}
    .form-control {{ width: 100%; padding: 8px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 13px; outline: none; transition: border-color .15s; }}
    .form-control:focus {{ border-color: #2563eb; }}
    .btn {{ padding: 8px 16px; border-radius: 6px; font-size: 13px; cursor: pointer; border: 1px solid transparent; font-weight: 500; transition: all .15s; }}
    .btn-primary {{ background: #2563eb; color: #fff; border: 1px solid #2563eb; }}
    .btn-primary:hover {{ background: #1d4ed8; }}
    .btn-ghost {{ background: #fff; border: 1px solid #07c160; color: #07c160; }}
    .btn-ghost:hover {{ background: #f0fdf4; }}
    .btn-warn {{ background: #fff; border: 1px solid #d97706; color: #d97706; }}
    .btn-warn:hover {{ background: #fffbeb; }}
    .btn-danger {{ background: #fff; border: 1px solid #dc2626; color: #dc2626; }}
    .btn-danger:hover {{ background: #fef2f2; }}
    .btn:disabled {{ opacity: .5; cursor: not-allowed; }}
    .btn-row {{ display: flex; gap: 10px; justify-content: center; margin: 12px 0; }}
    #qrcode {{ display: flex; justify-content: center; align-items: center; margin: 15px 0; min-height: 220px; }}
    .badge {{ display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 500; }}
    .badge-green {{ background: #e8f7ee; color: #07c160; }}
    .badge-amber {{ background: #fef3c7; color: #d97706; }}
    .badge-red {{ background: #fee2e2; color: #dc2626; }}
    .badge-gray {{ background: #f3f4f6; color: #6b7280; }}
    .qr-age {{ font-size: 12px; margin-top: 8px; color: #888; min-height: 17px; text-align: center; }}
    .url-text {{ word-break: break-all; font-size: 11px; color: #888; margin-top: 12px; background: #f9f9f9; padding: 8px; border-radius: 4px; text-align: center; }}
    .msg-box {{ font-size: 12px; margin-top: 10px; min-height: 18px; text-align: center; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>个人单据整理助手 · 控制台</h1>
      <p>端到端多模态单据自动归档与自然语言智能对账系统</p>
    </div>

    <div class="grid">
      <!-- 模块一: 模型与接口配置 -->
      <div class="card">
        <div class="card-title">
          <span>⚙️ 模型 API 配置</span>
          <span id="cfg-status-badge" class="badge badge-amber">● 读取中...</span>
        </div>
        <div class="card-desc">配置支持视觉与工具调用的模型接口。克隆代码后直接在此填写即可生效，无需手动编辑 .env。</div>

        <div class="form-group">
          <label>API Key (密钥)</label>
          <input id="cfg-key" class="form-control" type="password" placeholder="未配置或输入新 Key">
          <div id="cfg-key-hint" style="font-size: 11px; color: #9ca3af; margin-top: 4px;"></div>
        </div>

        <div class="form-group">
          <label>API Base URL (接口入口)</label>
          <input id="cfg-url" class="form-control" type="text" value="https://api.deepseek.com/v1">
        </div>

        <div class="form-group">
          <label>Model Name (模型名称)</label>
          <input id="cfg-model" class="form-control" type="text" value="deepseek-flash">
          <div style="font-size: 11px; color: #9ca3af; margin-top: 4px;">建议使用同时具备视觉和推理能力的模型</div>
        </div>

        <div style="margin-top: auto; padding-top: 10px;">
          <button id="btn-save-cfg" class="btn btn-primary" style="width: 100%;">保存配置并即时生效</button>
          <div id="cfg-msg" class="msg-box"></div>
        </div>
      </div>

      <!-- 模块二: 微信配对连接 -->
      <div class="card">
        <div class="card-title">
          <span>📱 微信终端连接</span>
          <span id="status" class="badge badge-gray">● 检测中...</span>
        </div>
        <div class="card-desc">手机微信扫码连接专属助理。配对完成后即可在聊天中发单据、查账。</div>

        <div id="qrcode"></div>
        <div class="qr-age" id="qr-age"></div>

        <div class="btn-row" id="btn-row">
          <button class="btn btn-ghost" id="download" style="display:none;">下载二维码</button>
          <button class="btn btn-warn" id="reissue" style="display:none;">重新签发码</button>
          <button class="btn btn-danger" id="disconnect" style="display:none;">断开微信</button>
        </div>

        <div class="url-text" id="raw-url">正在检测服务状态...</div>
      </div>
    </div>
  </div>

  <script>
    // 1. 模型配置加载与保存
    async function loadConfig() {{
      try {{
        const r = await (await fetch('/api/config')).json();
        const badge = document.getElementById('cfg-status-badge');
        const keyHint = document.getElementById('cfg-key-hint');
        if (r.hasApiKey) {{
          badge.innerText = '● 已配置密钥';
          badge.className = 'badge badge-green';
          keyHint.innerText = '当前已有密钥: ' + r.apiKeyMasked + '（留空表示保持当前不变）';
        }} else {{
          badge.innerText = '● 待配置密钥';
          badge.className = 'badge badge-amber';
          keyHint.innerText = '暂未配置 API Key，请填入后点击下方保存';
        }}
        if (r.baseUrl) document.getElementById('cfg-url').value = r.baseUrl;
        if (r.modelName) document.getElementById('cfg-model').value = r.modelName;
      }} catch (e) {{}}
    }}
    loadConfig();

    document.getElementById('btn-save-cfg').onclick = async () => {{
      const key = document.getElementById('cfg-key').value.trim();
      const url = document.getElementById('cfg-url').value.trim();
      const model = document.getElementById('cfg-model').value.trim();
      const msg = document.getElementById('cfg-msg');
      msg.innerText = '正在保存…'; msg.style.color = '#2563eb';
      try {{
        const r = await (await fetch('/api/config', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ apiKey: key, baseUrl: url, modelName: model }})
        }})).json();
        if (r.ok) {{
          msg.innerText = '✅ ' + r.message; msg.style.color = '#059669';
          document.getElementById('cfg-key').value = '';
          loadConfig();
        }} else {{
          msg.innerText = '❌ 保存失败: ' + (r.error || '未知错误'); msg.style.color = '#dc2626';
        }}
      }} catch (e) {{
        msg.innerText = '❌ 网络请求失败'; msg.style.color = '#dc2626';
      }}
    }};

    // 2. 微信二维码展示与轮询
    let qrUrl = "{qr_url}";
    let qrIssuedAt = {qr_issued_at};
    let currentStatus = "";
    const QR_TTL = 120;

    function tickQrAge() {{
      const el = document.getElementById('qr-age');
      if (!qrUrl || !qrIssuedAt || currentStatus !== 'waiting_scan') {{ el.innerText = ''; return; }}
      const left = Math.round(QR_TTL - (Date.now() / 1000 - qrIssuedAt));
      if (left <= 0) {{
        el.innerText = '二维码已到期，正在自动换新…'; el.style.color = '#dc2626';
      }} else if (left <= 30) {{
        el.innerText = '本码剩余约 ' + left + ' 秒，请尽快扫码'; el.style.color = '#d97706';
      }} else {{
        el.innerText = '本码剩余约 ' + left + ' 秒'; el.style.color = '#888';
      }}
    }}
    setInterval(tickQrAge, 1000);

    function setBadge(text, color, bg) {{
      const b = document.getElementById('status');
      b.innerText = text; b.style.color = color; b.style.background = bg;
    }}

    function renderNotRunning() {{
      currentStatus = 'bot_not_running';
      qrUrl = ''; qrIssuedAt = 0;
      setBadge('● 微信服务未运行', '#6b7280', '#f3f4f6');
      document.getElementById('qrcode').innerHTML = `
        <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; padding:20px 0;">
          <div style="font-size:36px; margin-bottom:10px;">🔌</div>
          <div style="font-size:15px; font-weight:600; color:#374151; margin-bottom:6px;">微信桥接服务未启动</div>
          <div style="font-size:12px; color:#9ca3af; margin-bottom:16px;">无需在终端手动敲命令，点击下方按钮即可一键启动</div>
          <button id="btn-start-bot" class="btn btn-primary" onclick="startBotNow()" style="padding:9px 24px; font-size:13px;">🚀 一键启动微信服务</button>
        </div>
      `;
      document.getElementById('download').style.display = 'none';
      document.getElementById('reissue').style.display = 'none';
      document.getElementById('disconnect').style.display = 'none';
      document.getElementById('qr-age').innerText = '';
      document.getElementById('raw-url').innerText = '点击上方按钮即可启动微信连接并获取配对二维码';
    }}

    function renderConnected() {{
      currentStatus = 'connected';
      qrUrl = ''; qrIssuedAt = 0;
      setBadge('● 微信已连接', '#07c160', '#e8f7ee');
      document.getElementById('qrcode').innerHTML = `
        <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; padding:20px 0;">
          <div style="font-size:44px; margin-bottom:10px;">🎉</div>
          <div style="font-size:16px; font-weight:600; color:#07c160; margin-bottom:6px;">微信助理已连接就绪</div>
          <div style="font-size:12px; color:#6b7280; max-width:280px; line-height:1.5;">
            你现在可以在手机微信中随时向助手发送票据图片、PDF发票或提问查账。
          </div>
        </div>
      `;
      document.getElementById('download').style.display = 'none';
      document.getElementById('reissue').style.display = 'inline-block';
      document.getElementById('reissue').innerText = '更换微信 / 重新扫码';
      document.getElementById('disconnect').style.display = 'inline-block';
      document.getElementById('qr-age').innerText = '';
      document.getElementById('raw-url').innerText = '设备已就绪，实时双向通信中';
    }}

    function drawQr(url, issuedAt) {{
      currentStatus = 'waiting_scan';
      qrUrl = url;
      if (issuedAt) qrIssuedAt = issuedAt;
      const box = document.getElementById('qrcode');
      box.innerHTML = '';
      new QRCode(box, {{ text: url, width: 220, height: 220 }});
      document.getElementById('download').style.display = 'inline-block';
      document.getElementById('reissue').style.display = 'inline-block';
      document.getElementById('reissue').innerText = '重新签发码';
      document.getElementById('disconnect').style.display = 'none';
      document.getElementById('raw-url').innerText = '连接地址: ' + url;
      tickQrAge();
    }}

    async function startBotNow() {{
      const btn = document.getElementById('btn-start-bot');
      if (btn) {{ btn.disabled = true; btn.innerText = '正在启动微信服务...'; }}
      setBadge('● 正在启动服务...', '#2563eb', '#eff6ff');
      try {{
        const r = await (await fetch('/api/start-bot', {{ method: 'POST' }})).json();
        if (!r.ok) {{
          alert('启动失败: ' + r.message);
          renderNotRunning();
        }} else {{
          checkStatus();
        }}
      }} catch (e) {{
        alert('启动请求失败: ' + e);
        renderNotRunning();
      }}
    }}

    document.getElementById("download").onclick = () => {{
      const img = document.querySelector("#qrcode img");
      const canvas = document.querySelector("#qrcode canvas");
      const dataUrl = (img && img.src && img.src.startsWith("data:image")) ? img.src
                    : (canvas ? canvas.toDataURL("image/png") : "");
      if (!dataUrl) {{ alert("二维码还没生成，请稍候刷新"); return; }}
      const a = document.createElement("a");
      a.href = dataUrl; a.download = "wechat-bot-qrcode.png"; a.click();
    }};

    let reissuing = false;
    document.getElementById("reissue").onclick = async () => {{
      if (!confirm('重新签发会让当前二维码或连接失效，需要重新扫码。确定继续？')) return;
      reissuing = true; qrUrl = '';
      setBadge('● 正在重新签发…', '#d97706', '#fef3c7');
      document.getElementById('qrcode').innerHTML = '<div style="font-size:14px;color:#d97706;margin:80px 0;text-align:center;">正在签发新二维码…</div>';
      try {{
        const r = await (await fetch('/api/reissue-qr', {{ method: 'POST' }})).json();
        document.getElementById('raw-url').innerText = r.message;
        if (!r.ok) {{ reissuing = false; setBadge('● 重新签发失败', '#dc2626', '#fee2e2'); }}
      }} catch (e) {{
        reissuing = false; setBadge('● 重新签发失败', '#dc2626', '#fee2e2');
      }}
    }};

    document.getElementById("disconnect").onclick = async () => {{
      if (!confirm('确定断开当前微信连接？')) return;
      try {{
        await fetch('/api/stop-bot', {{ method: 'POST' }});
        checkStatus();
      }} catch (e) {{}}
    }};

    async function checkStatus() {{
      try {{
        const data = await (await fetch('/api/status')).json();
        if (data.status === 'bot_not_running') {{
          if (currentStatus !== 'bot_not_running') renderNotRunning();
          return;
        }}
        if (data.status === 'connected') {{
          if (currentStatus !== 'connected') renderConnected();
          return;
        }}
        if (data.status === 'scanned') {{
          setBadge('● 已扫码，请在手机上确认', '#d97706', '#fef3c7');
        }} else if (!reissuing) {{
          setBadge('● 等待微信扫码连接...', '#07c160', '#e8f7ee');
        }}
        if (data.qrcode && (data.qrcode !== qrUrl || currentStatus !== 'waiting_scan')) {{
          drawQr(data.qrcode, data.qrcode_issued_at);
          reissuing = false;
        }}
      }} catch (e) {{}}
    }}
    setInterval(checkStatus, 1500);
    checkStatus();
  </script>
</body>
</html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def _code_version() -> str:
    """已加载代码的版本 = core/ 下最新的源文件修改时间 (改完代码没重启时一眼可辨)"""
    newest = max((f.stat().st_mtime for f in (ROOT_DIR / "core").glob("*.py")), default=0)
    return datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S") if newest else "未知"


def start_server():
    # 默认自动拉起微信机器人子进程（除非显式指定 AUTO_START_BOT=0）
    auto_start = os.getenv("AUTO_START_BOT", "1").lower() not in ("0", "false", "no")
    if auto_start:
        threading.Thread(target=_auto_start_bot, daemon=True).start()

    # 用多线程：一次模型调用要几秒，单线程会把扫码页的状态轮询一起堵住
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    server = socketserver.ThreadingTCPServer((HOST, PORT), AgentServerHandler)
    print(f"[Python Agent Server] 服务已启动: http://{HOST}:{PORT}")
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        # 这不是"多一条提示"，而是这套接口的真实后果: /api/* 与 /qrcode 都没有鉴权，
        # 而 POST /api/reissue-qr 可以随时清凭据、签发一张新码——外部因此能在任意
        # 时刻制造出一个可被绑定的窗口，不只是初次扫码那会儿。
        print(f"[!] 正在监听非回环地址 {HOST}: /qrcode 与 /api/* 都没有鉴权，"
              f"网络内任何人都能重新签发二维码并绑定机器人、改写模型配置。")
        print("[!] 服务器上演示请改用 SSH 隧道（见 服务器部署.md）；要让别人扫码就截图发给他。")
    print(f"[Python Agent Server] 已加载代码版本: {_code_version()}")
    print("[Python Agent Server] 提示: 改动 core/ 或 web/ 下的代码后必须重启本进程才会生效")
    try:
        server.serve_forever()
    finally:
        stop_bot_process()

if __name__ == "__main__":
    start_server()
