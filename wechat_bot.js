import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { WeixinBot } from 'weixin-bot-sdk';
import qrcode from 'qrcode-terminal';
import { sendText } from './wx_send.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.join(__dirname, 'data');
const CRED_PATH = path.join(DATA_DIR, 'wx-credentials.json');
const QRCODE_FILE = path.join(DATA_DIR, 'qrcode_url.txt');
const STATUS_FILE = path.join(DATA_DIR, 'bot_status.json');
// 扫码页上的「重新签发」按钮写这个标记再重启进程，启动时在下面被消费掉
const REISSUE_FLAG = path.join(DATA_DIR, '.reissue-qr');
const AGENT_API_URL = 'http://127.0.0.1:8765/api/message';

// 与 Python 侧 Config.MODEL_NAME 保持同一个模型（文本对话 + 多模态识别共用）
const MODEL_LABEL = process.env.MODEL_NAME || 'deepseek-flash';

if (!fs.existsSync(DATA_DIR)) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

const setBotStatus = (status) => {
  try {
    fs.writeFileSync(STATUS_FILE, JSON.stringify({ status, time: new Date().toISOString() }), 'utf-8');
  } catch (e) {}
};

console.log('====================================================');
console.log('🤖 个人单据整理助手 - 微信直连启动中...');
console.log(`📡 后端 Agent 服务接口: ${AGENT_API_URL}`);
console.log('====================================================');

const bot = new WeixinBot({ credentialsPath: CRED_PATH });

bot.on('message', async (msg) => {
  const userId = msg.from || 'unknown';
  const text = msg.text || '';
  const msgType = msg.type;

  console.log(`\n📩 [微信] 收到来自 [${userId}] 的消息 (类型: ${msgType}): ${text || '[附件/媒体消息]'}`);

  // 1. 立即激活微信“对方正在输入...”状态，并通过定时器在处理过程中定期续期
  let typingTimer = null;
  const triggerTyping = () => {
    bot.sendTyping(msg.from, msg.contextToken).catch(() => {});
  };
  triggerTyping();
  typingTimer = setInterval(triggerTyping, 4000);

  const clearTypingState = () => {
    if (typingTimer) {
      clearInterval(typingTimer);
      typingTimer = null;
    }
    bot.cancelTyping(msg.from, msg.contextToken).catch(() => {});
  };

  try {
    let filePath = null;

    if (msgType === 'image' && msg.image) {
      console.log('🖼️ 正在从微信安全下载用户发送的图片/截图...');
      const imgBuffer = await bot.downloadImage(msg.image);
      const filename = `wx_img_${Date.now()}.jpg`;
      const targetPath = path.join(DATA_DIR, 'attachments', filename);
      fs.mkdirSync(path.join(DATA_DIR, 'attachments'), { recursive: true });
      fs.writeFileSync(targetPath, imgBuffer);
      filePath = targetPath;
      console.log(`💾 图片保存完成: ${targetPath} (${imgBuffer.length} 字节)`);
    } else if (msgType === 'file' && msg.file) {
      console.log(`📎 正在从微信安全下载用户发送的文件: ${msg.file.file_name}...`);
      const fileBuffer = await bot.downloadFile(msg.file);
      const safeName = (msg.file.file_name || 'document.pdf').replace(/[/\\?%*:|"<>]/g, '_');
      const filename = `wx_file_${Date.now()}_${safeName}`;
      const targetPath = path.join(DATA_DIR, 'attachments', filename);
      fs.mkdirSync(path.join(DATA_DIR, 'attachments'), { recursive: true });
      fs.writeFileSync(targetPath, fileBuffer);
      filePath = targetPath;
      console.log(`💾 文件保存完成: ${targetPath} (${fileBuffer.length} 字节)`);
    } else if (msgType === 'text') {
      if (!text.trim()) {
        clearTypingState();
        return;
      }
    } else {
      console.log(`⚠️ 暂未处理的消息类型: ${msgType}`);
      clearTypingState();
      return;
    }

    // 2. 转发给 Python 后端 Agent (单模型同时完成推理与多模态解析)
    console.log(`🚀 转发请求给 Python Agent (${MODEL_LABEL})...`);
    const res = await fetch(AGENT_API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, filePath }),
    });

    const data = await res.json();
    const reply = data.reply || (data.error ? `处理遇到问题: ${data.error}` : '系统处理中...');
    console.log(`📤 [模型] 准备回复微信: ${reply.slice(0, 70)}...`);

    // 3. 发文字回复。这一步走的是自己实现的 sendText：SDK 的版本会吞掉失败（详见 wx_send.js）
    clearTypingState();
    await sendText(bot.api, msg.from, reply, msg.contextToken);
    console.log('✅ [微信] 文字回复已送达');
  } catch (err) {
    clearTypingState();
    console.error('❌ 处理并回复消息失败:', err.message);
    try {
      await sendText(bot.api, msg.from, '抱歉，助理在处理该消息或识别图片时遇到异常，请稍后再试。', msg.contextToken);
    } catch (_) {}
  }
});

bot.on('error', (err) => {
  console.error('❌ [WeixinBot] 错误:', err.message);
});

const attemptLogin = () => bot.login({
  onQrCode: (url) => {
    fs.writeFileSync(QRCODE_FILE, url, 'utf-8');
    console.log('\n====================================================');
    console.log('📱 请使用手机微信扫码下方二维码绑定机器人:');
    console.log('====================================================\n');
    qrcode.generate(url, { small: true });
    console.log('\n💡 想把这个机器人交给别人体验：');
    console.log('   1) 浏览器打开 http://<本机或服务器地址>:8765/qrcode ，页面可直接下载二维码图片');
    console.log('   2) 或者把下面这条链接/二维码图片发给对方，对方用手机微信扫一扫即可绑定');
    console.log(`   二维码内容: ${url}\n`);
    console.log('====================================================');
  },
  onStatus: (status) => {
    if (status === 'scanned') {
      console.log('👀 手机微信已扫码，请在手机上点击【确认登录】...');
      setBotStatus('scanned');
    } else if (status === 'confirmed') {
      console.log('🎉 手机微信已确认，微信机器人连接成功！');
      setBotStatus('connected');
    }
  },
});

async function main() {
  while (true) {
    const byFlag = fs.existsSync(REISSUE_FLAG);
    if (byFlag) {
      try { fs.unlinkSync(REISSUE_FLAG); } catch (_) {}
    }
    if (byFlag || process.argv.includes('--new-qr')) {
      bot.api.token = null;
      try { fs.unlinkSync(CRED_PATH); } catch (_) {}
      try { fs.unlinkSync(QRCODE_FILE); } catch (_) {}
      console.log('🔄 重新签发二维码：已清除历史凭据，开始全新扫码绑定');
    }

    if (!bot.isLoggedIn) {
      console.log('📱 正在生成专属连接二维码，请准备好手机微信扫码...');
      setBotStatus('waiting_scan');

      let attempt = 0;
      for (;;) {
        attempt++;
        try {
          await attemptLogin();
          break;
        } catch (err) {
          console.log(`⏳ 第 ${attempt} 轮扫码等待结束（${err.message}）——换一张新二维码继续等待，无需人工干预`);
          setBotStatus('waiting_scan');
          await new Promise(r => setTimeout(r, 3000));
        }
      }
    } else {
      console.log('🔑 使用已保存的历史微信凭据登录...');
    }

    let sessionExpired = false;
    const onExpired = () => {
      console.log('⚠️ 微信会话已过期（可能在其他设备登录或凭据失效），正在清理凭据并重新生成二维码...');
      sessionExpired = true;
      bot.api.token = null;
      try { fs.unlinkSync(CRED_PATH); } catch (_) {}
      try { fs.unlinkSync(QRCODE_FILE); } catch (_) {}
      setBotStatus('waiting_scan');
    };

    bot.once('session:expired', onExpired);

    try {
      bot.start();
      setBotStatus('connected');
      console.log('🚀 微信机器人监听服务已就绪！你现在可以在微信中随时发指令测试了。');

      while (bot._running && !sessionExpired) {
        await new Promise(r => setTimeout(r, 1000));
      }
    } catch (err) {
      console.error('❌ 监听循环异常:', err.message);
      bot.api.token = null;
      try { fs.unlinkSync(CRED_PATH); } catch (_) {}
      setBotStatus('waiting_scan');
    } finally {
      bot.removeListener('session:expired', onExpired);
    }

    console.log('🔄 准备重新进入连接状态循环...');
    await new Promise(r => setTimeout(r, 2000));
  }
}

main().catch((err) => {
  console.error('Fatal error:', err);
});
