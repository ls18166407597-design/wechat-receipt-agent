"""
上下文装配与长记忆管理 (单用户单会话: 滑动窗口 + 批量整理)

**计数口径: 一轮 = 一条 user 消息及其回复。** 工具调用的结果帧虽然也落库留底
(_invoke_tool)，但不参与窗口与阈值的计算——装配上下文时本就把它们过滤掉了，
按"消息条数"算会让工具帧既占阈值又占窗口（实测把 20 轮压到 4~14 轮，还会切出
"保留了回复、却砍掉对应提问"的孤儿消息）。所以这里一律按轮数计。

记忆分层策略:
1. 最近 20 轮逐字保留在滑动窗口内——负责代词指代("那张""这个")与上下文追问往返，
   这是唯一的逐字历史记忆；
2. 更早的消息在累积达到 100 轮后触发批量整理，直接将窗口外的历史消息移出提示词，
   **坚决不做 LLM 摘要，也不做不准确的正则弱事实抽取**:
   - 单据数据（金额、单号、开票方、日期等）已在 documents 表中，通过只读 SQL / 原生工具随时精确查询；
   - 对话记录在 chat_messages 表中物理全量持久化留底，随时可查；
   - 彻底避免正则启发式抽取对中文对话的误判与提示词污染；
3. System 前缀在同一个压缩周期内逐字节冻结 (Byte-for-Byte Freezing)，常规轮次严格单调尾部追加
   (Monotonic Append)，最大化服务端 KV Cache 命中率 (实测可达 93% 以上)。
"""
import json
from datetime import date
from typing import List, Dict, Any

from .config import Config
from .db import DatabaseManager


class ContextCompressor:
    def __init__(self, db: DatabaseManager,
                 max_raw_rounds: int = Config.RECENT_RAW_BUFFER_ROUNDS,
                 compression_threshold_rounds: int = Config.MAX_UNCOMPRESSED_ROUNDS):
        """
        :param max_raw_rounds: 整理后逐字保留的最近轮数 (一轮 = 一条 user 消息及其回复)
        :param compression_threshold_rounds: 触发整理的未压缩轮数阈值

        两个参数都以「轮」计，不在内部换算成消息条数——单位写在参数名上，
        免得再出现"配置按轮、实现按条"这种两边各说各话的口径错位。
        """
        self.db = db
        self.session_id = Config.SESSION_ID
        self.max_raw_rounds = max_raw_rounds
        self.compression_threshold_rounds = compression_threshold_rounds
        # 供日志观测: 上一轮装配出来的上下文有多少轮逐字历史
        self.last_context_rounds = 0

    def get_context_state(self) -> Dict[str, Any]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM chat_context_state WHERE session_id = ?;", (self.session_id,)
            ).fetchone()
            if row:
                return dict(row)
            return {
                "session_id": self.session_id,
                "compressed_up_to_id": 0
            }

    def save_message(self, role: str, content: str, tool_calls_json: str = None) -> int:
        """物理全量存储每一条对话 (永久留底，永不真删)"""
        with self.db.get_connection() as conn:
            cursor = conn.execute("""
            INSERT INTO chat_messages (role, content, tool_calls_json)
            VALUES (?, ?, ?);
            """, (role, content, tool_calls_json))
            conn.commit()
            return cursor.lastrowid

    # 只算对话帧: tool 帧不进上下文(见 build_model_context)，所以也不该占窗口
    DIALOGUE_ROLES = ("user", "assistant")

    def _uncompressed_dialogue(self, compressed_up_to_id: int) -> List[Dict[str, Any]]:
        """取游标之后尚未整理的对话帧，按 id 升序。"""
        placeholders = ", ".join("?" for _ in self.DIALOGUE_ROLES)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"SELECT id, role, content FROM chat_messages"
                f" WHERE id > ? AND role IN ({placeholders}) ORDER BY id ASC;",
                (compressed_up_to_id, *self.DIALOGUE_ROLES)).fetchall()
            return [dict(r) for r in rows]

    def check_and_compress(self) -> bool:
        """超过水位线时执行批量整理，将滑动窗口外的历史消息移出大模型上下文。

        移出时仅推移 compressed_up_to_id 游标，不生成摘要、不做弱事实抽取：
        1. 单据数据在 documents 表里随时查得到，绝不二次冗余进提示词；
        2. 历史对话在 chat_messages 物理全量留底；
        3. 裁剪后上下文回退到最近无损窗口，System 前缀保持绝对纯净与字节冻结。

        切点永远落在「轮」的边界上（保留的最后一段以 user 开头），所以不会出现
        保留了回复、却把对应提问砍掉的那种孤儿消息。
        """
        state = self.get_context_state()
        uncompressed = self._uncompressed_dialogue(state.get("compressed_up_to_id", 0))

        user_positions = [i for i, m in enumerate(uncompressed) if m["role"] == "user"]
        if len(user_positions) <= self.compression_threshold_rounds:
            return False

        # 保留最后 max_raw_rounds 轮: 从第 (总轮数 - 窗口) 条 user 起留
        cutoff = len(user_positions) - self.max_raw_rounds
        if cutoff <= 0:
            # 窗口不小于现有轮数，没有可裁的（正常配置下阈值 > 窗口，走不到这里）
            return False
        cut_index = user_positions[cutoff]
        new_compressed_up_to_id = uncompressed[cut_index - 1]["id"]

        with self.db.get_connection() as conn:
            conn.execute("""
            INSERT INTO chat_context_state (session_id, compressed_up_to_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                compressed_up_to_id=excluded.compressed_up_to_id,
                updated_at=CURRENT_TIMESTAMP;
            """, (self.session_id, new_compressed_up_to_id))
            conn.commit()

        return True

    STATIC_BASE_SYSTEM = """你是一个个人单据整理助手。用户会把各种单据发给你，什么都可能有。你负责看懂、归档、检索、统计。

用户是在手机微信里看你回的话，不是在看报告：直接给结论，三五句说清，他追问了再展开。

单据数据一律用工具查、用工具算，不要凭印象估算；查不到就说查不到，不要给一个"大概是"。

用户补充信息、或者要求删掉某张时，用 update_document / delete_document 真的改，不要只口头答应。
删之前先确认删的是哪一张。

编号（"已归档为 #N"）、SQL、表名、工具名、服务器路径都是系统内部的东西——你靠它们干活，
回复里一个字都不要提。用户是靠记得的东西找单据的（"蜜雪冰城那张"），不该让他去记编号。

本通道只能发文字，回传不了图片和文件。用户要原件时如实说明，给出标题、金额、日期帮他检索；
不要说"已经发给你了"。"""

    def build_model_context(self) -> List[Dict[str, str]]:
        """
        组装大模型上下文:
        1. 静态基底 System Prompt 恒在第 0 位；
        2. 当前日期由程序确定性注入（含星期），以天为粒度保持前缀逐字节冻结；
        3. 常规轮次严格单调尾部追加，最大化命中服务端的 KV Cache；
        4. 达到整理阈值时，自动移出窗口外的消息，裁剪回最近的无损缓冲窗口。
        """
        self.check_and_compress()

        state = self.get_context_state()
        # 只要对话帧: 轮次内部的 tool 帧不进跨轮上下文（口径见模块开头）
        recent_msgs = self._uncompressed_dialogue(state.get("compressed_up_to_id", 0))
        self.last_context_rounds = sum(1 for m in recent_msgs if m["role"] == "user")

        # 当前日期必须由程序告知（含星期几）：模型缺乏对未来日历的确定性推算能力，
        # 如果不显式给星期几，模型会被迫脑补并产生星期幻觉。
        # 它每天只变一次，落在 system 前缀里仍满足“压缩周期内逐字节冻结”（全天缓存命中率 >90%）。
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        cur_date = date.today()
        today_text = f"{cur_date.isoformat()} {weekdays[cur_date.weekday()]}"

        system_blocks = [
            self.STATIC_BASE_SYSTEM,
            f"\n【今天】{today_text}。用户说“上个月”“上周”“昨天”这类相对时间时，以今天为准换算成具体日期再查。",
        ]

        model_messages = [{"role": "system", "content": "\n".join(system_blocks)}]
        for m in recent_msgs:
            model_messages.append({"role": m["role"], "content": m["content"]})
        return model_messages

    def calculate_prefix_hash(self, check_length: int = 3) -> str:
        """用于测试校验: 验证多轮对话中前 N 条消息是否逐字节一致"""
        import hashlib
        context = self.build_model_context()
        raw_str = json.dumps(context[:check_length], ensure_ascii=False)
        return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()
