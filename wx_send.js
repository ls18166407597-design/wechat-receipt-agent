/**
 * 微信文字回复。
 *
 * 为什么不直接用 SDK 的 bot.reply / api.sendText：
 * sendmessage 失败时依然返回 HTTP 200，真正的错误在响应体里
 * （配额用尽时实测返回 {"ret":-2,"errmsg":"prepare failed"}，社区文档口径为
 *  "用户主动发起一次对话后才刷新约 10 条下行额度"），而 SDK 丢掉了响应体、只看 HTTP 状态，
 * 于是失败会被当成"已送达"上报。这里补上 ret 校验，让失败可见。
 */
import crypto from 'node:crypto';

const DEFAULT_BASE_URL = 'https://ilinkai.weixin.qq.com';
const ITEM_TEXT = 1;
const MESSAGE_TYPE_BOT = 2;
const MESSAGE_STATE_FINISH = 2;

export async function sendText(api, toUserId, text, contextToken) {
  const res = await fetch(`${api.baseUrl || DEFAULT_BASE_URL}/ilink/bot/sendmessage`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      AuthorizationType: 'ilink_bot_token',
      'X-WECHAT-UIN': Buffer.from(String(crypto.randomBytes(4).readUInt32BE(0)), 'utf8').toString('base64'),
      Authorization: `Bearer ${api.token}`,
    },
    body: JSON.stringify({
      msg: {
        from_user_id: '',
        to_user_id: toUserId,
        client_id: `wx-doc-${crypto.randomBytes(8).toString('hex')}`,
        message_type: MESSAGE_TYPE_BOT,
        message_state: MESSAGE_STATE_FINISH,
        item_list: [{ type: ITEM_TEXT, text_item: { text } }],
        context_token: contextToken,
      },
      base_info: { channel_version: api.version || '1.0.0' },
    }),
  });

  const raw = await res.text();
  let body = null;
  try { body = JSON.parse(raw); } catch { /* 保持 null，走下面的报错分支 */ }
  if (!res.ok || (body && body.ret !== undefined && body.ret !== 0)) {
    throw new Error(`sendmessage 被拒: HTTP ${res.status} ret=${body?.ret} errmsg=${body?.errmsg || raw.slice(0, 120)}`);
  }
  return body;
}
