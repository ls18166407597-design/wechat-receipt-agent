"""
轻量原生 Agent 调度运行引擎
(单模型同时承担文本对话 / Tool Calling / 图片与 PDF 多模态识别，单用户单会话)
"""
import json
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Optional

from .config import Config
from .db import DatabaseManager
from .context_compressor import ContextCompressor
from .native_tools import NativeToolRegistry
from .media_reader import MediaReader
from .llm_client import OpenAICompatibleClient

MAX_TOOL_ITERATIONS = 5
DOC_TYPE_NAMES = {
    "invoice": "发票",
    "order": "购物订单/支付截图",
    "receipt": "小票/收据",
    "contract": "合同/协议",
    "delivery": "送货单/物流单",
    "ticket": "车票/机票/行程单",
    "statement": "对账单/结算单",
    "insurance": "保单",
    "reimbursement": "报销单",
    "other": "其他单据",
}

# 模型偶尔会返回自由文本类型 (如"增值税电子普通发票")，按关键字归一化到统一分类，保证检索口径一致
DOC_TYPE_KEYWORDS = (
    ("invoice", ("发票", "增值税", "专票", "普票", "invoice")),
    ("ticket", ("车票", "机票", "行程单", "火车票", "高铁", "登机", "航空", "客票", "ticket")),
    ("reimbursement", ("报销", "费用单", "差旅单")),
    ("delivery", ("送货", "物流", "运单", "提货", "签收", "配送")),
    ("contract", ("合同", "协议", "契约", "contract")),
    ("statement", ("对账", "结算单", "流水", "账单", "月结", "明细表")),
    ("insurance", ("保险", "保单", "投保", "车险")),
    ("receipt", ("小票", "收据", "凭条", "receipt")),
    ("order", ("订单", "下单", "实付款", "购物", "支付凭证", "交易记录", "order")),
)


# 单据抽取读不出可用 JSON 时最多试几次。偶发失败（推理把输出预算吃光 / 只吐几个
# token 就停）实测能碰到，而提取是幂等的，再要一次比让用户重发划算。
MAX_EXTRACT_ATTEMPTS = 2


def normalize_doc_type(raw: Any, title: str = "") -> str:
    """把模型给出的类型归一到统一分类；无法判断时归入 other。

    模型偶尔会回自由文本类型（"增值税电子普通发票"），所以退回按关键字匹配标题。
    """
    if raw in DOC_TYPE_NAMES and raw != "other":
        return raw
    haystack = f"{title} {raw or ''}".lower()
    for canonical, keywords in DOC_TYPE_KEYWORDS:
        if any(k.lower() in haystack for k in keywords):
            return canonical
    return "other"


class AgentRunner:
    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()
        self.compressor = ContextCompressor(self.db)
        self.tools = NativeToolRegistry(self.db)
        self.llm = OpenAICompatibleClient(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_BASE_URL,
            model=Config.MODEL_NAME,
        )
        # 本轮所有模型调用的 token 用量，用于观测缓存命中率 (命中价约便宜 50 倍)
        self.last_usage: Dict[str, int] = {}

    def _record_usage(self, response: Dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cache_hit_tokens"):
            self.last_usage[key] = self.last_usage.get(key, 0) + int(usage.get(key) or 0)

    def _invoke_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具并把结果留底到消息流水"""
        result = self.tools.execute_tool(name, arguments)
        self.compressor.save_message("tool", json.dumps(result, ensure_ascii=False))
        return result

    # ------------------------------------------------------------------ 文本对话
    def handle_user_message(self, user_text: str,
                            mock_llm_response: Optional[Dict[str, Any]] = None) -> str:
        """
        处理微信用户发来的自然语言提问:
        1. 写入持久化消息历史
        2. 获取装配后的上下文 (按需触发记忆压缩)
        3. 调度模型执行 Tool Calling (支持循环调用)
        4. 返回最终回答并写入持久化
        """
        self.compressor.save_message("user", user_text)
        self.last_usage = {}
        context = self.compressor.build_model_context()

        if mock_llm_response is not None:
            final_reply = self._run_mocked_llm(mock_llm_response)
        else:
            final_reply = self._run_tool_calling_loop(context)

        self.compressor.save_message("assistant", final_reply)
        return final_reply

    def _run_tool_calling_loop(self, context: List[Dict[str, Any]]) -> str:
        """真实的 Tool Calling 循环: 最多执行 MAX_TOOL_ITERATIONS 轮"""
        tool_defs = self.tools.get_tool_definitions()

        for _ in range(MAX_TOOL_ITERATIONS):
            resp = self.llm.chat_completion(context, tools=tool_defs)
            self._record_usage(resp)
            if resp.get("status") != "success":
                # 原始错误里是 HTTP 响应体（可能是一大坨 JSON），只进日志不进微信
                print(f"[Agent] 模型调用失败，原始错误: {resp.get('message', '')}")
                return "模型这会儿没连上，稍后再发我一次就行。"

            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                return resp.get("content") or "已为您处理。"

            # 思考模式要求：assistant 消息带 tool_calls 时必须原样回传
            # reasoning_content，漏掉会被服务端 400 拒掉。只在当轮回填，不落库
            # ——推理过程不属于跨轮记忆。
            assistant_turn = {
                "role": "assistant",
                "content": resp.get("content") or "",
                "tool_calls": tool_calls,
            }
            if resp.get("reasoning_content"):
                assistant_turn["reasoning_content"] = resp["reasoning_content"]
            context.append(assistant_turn)

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_res = self._invoke_tool(
                    fn.get("name", ""), self._parse_tool_arguments(fn.get("arguments"))
                )
                context.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", "call_default"),
                    "content": json.dumps(tool_res, ensure_ascii=False),
                })

        # 达到轮次上限仍未给出自然语言结论: 去掉 tools 再要一次总结
        resp = self.llm.chat_completion(context)
        self._record_usage(resp)
        return resp.get("content") or "已完成多轮数据查询，请补充你想看的统计维度。"

    def _run_mocked_llm(self, mock_response: Dict[str, Any]) -> str:
        """单测注入路径: 与真实分支使用完全相同的 tool_calls 结构"""
        for tc in mock_response.get("tool_calls") or []:
            fn = tc.get("function", {})
            self._invoke_tool(fn.get("name", ""), self._parse_tool_arguments(fn.get("arguments")))
        return mock_response.get("final_reply") or mock_response.get("content") or "收到您的消息。"

    @staticmethod
    def _parse_tool_arguments(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except ValueError:
                return {}
        return {}

    # ------------------------------------------------------------------ 单据上传
    def handle_document_upload(self, file_path: str, user_text: str = "",
                               mock_llm_response: Optional[Dict[str, Any]] = None) -> str:
        """
        处理微信用户上传的单据图片/发票/合同文件:
        1. 本地轻量归档与文件哈希计算
        2. 打包多模态 Vision 载荷 (多页 PDF 只渲染开头几页，见 pdf_processor)
        3. 调用同一个模型做多模态结构化抽取 (算式勾稽也在这一步由模型自己验)
        4. 写入 SQLite documents 表与消息流水
        5. 查一遍模型看不见的问题，回给它由它决定说不说
        6. 拼出回执：模型写正文，程序只补"已归档"和页数实情
        """
        path_obj = Path(file_path)
        # 只接受附件目录内的文件：/api/message 无鉴权，filePath 又是客户端传进来的，
        # 不做这层校验就等于开了一个任意文件读取口（模型会把内容原样回显在回复里）
        try:
            path_obj.resolve().relative_to(Config.ATTACHMENTS_DIR.resolve())
        except ValueError:
            print(f"[Agent] 拒绝处理附件目录外的路径: {file_path}")
            return "没找到这条消息带过来的文件，麻烦重新发一次。"
        if not path_obj.exists():
            # 不回显 file_path: 那是服务器上的归档路径，用户看了没用，
            # 也和"归档目录不对外暴露"这条设计原则相抵触
            print(f"[Agent] 上传的文件不存在: {file_path}")
            return "没找到这条消息带过来的文件，麻烦重新发一次。"

        self.last_usage = {}

        try:
            rel_path, _ = MediaReader.save_and_archive(path_obj)
        except OSError:
            rel_path = str(file_path)

        # 归档对已在 attachments 目录内的文件是 move 而非 copy，原路径已不存在，
        # 必须读归档后的那份，否则微信发来的每张图/每个文件都会 FileNotFoundError
        archived_obj = Config.ATTACHMENTS_DIR.parent / rel_path
        read_obj = archived_obj if archived_obj.exists() else path_obj

        try:
            vision_blocks, media_meta = MediaReader.build_model_payloads(read_obj)
        except ValueError as e:
            # 格式不支持：这条消息本身列了支持范围，是写给用户看的
            return str(e)
        except RuntimeError as e:
            # 缺第三方库属于部署问题，原文是"请执行 pip install ..."，别丢给用户
            print(f"[Agent] 文件解析依赖缺失: {e}")
            return "这个格式我暂时读不了，换成图片或者 PDF 再发我一次。"

        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        cur_d = date.today()
        today = f"{cur_d.isoformat()} {weekdays[cur_d.weekday()]}"
        sampling_note = ""
        if media_meta.get("kind") == "pdf":
            read_pages = "、".join(str(p) for p in media_meta["sampled_pages"])
            sampling_note = (f"\n（这是一份 {media_meta['total_pages']} 页的文档，"
                             f"为控制长度只送入了第 {read_pages} 页。）")

        prompt = f"""你是一个个人单据整理助手。用户随手发来各种单据，什么都可能有。你的活是看懂它、记下来、以后能查。
{sampling_note}
当前日期: {today}
用户附带说明: {user_text.strip() or "无附带说明"}

【怎么认】
先判断这是什么单据，再想这张单据上什么值得记下来。
判断标准只有一条：**以后回头查这张单据时，会想知道什么？**
不要套模板——合同和体检报告该记的东西完全不同。
单据上没有的不要编；不知道就说不知道。

【算账】
单据上如果有构成算式的数字（不含税＋税额＝价税合计、单价×数量＝合计、明细相加＝总计），
自己验一遍，对不上就在 reply 里说。

【怎么回】
reply 直接发到用户手机微信里——他在看一句话，不是在看报告。
- 直接说结论，不要"好的，收到"这类开场
- 三五句，一行一件事
- 不重复同一个信息，不把金额、日期换个说法再说一遍
- 不写"未列示""未标注"这类占位话——那只是把"这里没信息"换个说法，白占一行
- 不写"已归档"，不提编号，也不说文档有几页、读了几页——系统会补

【拿不准】
信息缺了、图糊了、有两种读法——如实说，或者问用户。不要替他决定。

严格输出为纯 JSON（不要任何多余文字）：
{{
  "doc_type": "invoice"/"order"/"receipt"/"contract"/"delivery"/"ticket"/"statement"/"insurance"/"reimbursement"/"other" 之一，都不像就用 "other",
  "title": "这张单据是什么",
  "main_amount": 核心金额（数字；没有就填 0.0）,
  "main_date": "单据日期 YYYY-MM-DD。看不清、或确实没有，就留空字符串——**不要拿今天的日期顶上**",
  "main_entity": "对方是谁（开票方／商家／对方单位）",
  "type_fields": {{ 上面几项没覆盖、但值得记下来的信息。键名用中文，2~4 条，按重要程度排 }},
  "other_type_name": "仅当 doc_type 为 other 时填：这张单据叫什么；其余留空",
  "reply": "发给用户的回执正文，见【怎么回】"
}}
"""

        if mock_llm_response is not None:
            doc_data = mock_llm_response
        else:
            messages = [{
                "role": "user",
                "content": [{"type": "text", "text": prompt}, *vision_blocks],
            }]
            doc_data = None
            for attempt in range(1, MAX_EXTRACT_ATTEMPTS + 1):
                # 用 JSON 模式强约束输出，避免从自由文本里刮 JSON
                resp = self.llm.chat_completion(
                    messages,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                    max_tokens=Config.MAX_OUTPUT_TOKENS,
                )
                self._record_usage(resp)
                if resp.get("status") != "success":
                    print(f"[Agent] 单据识别调用失败，原始错误: {resp.get('message', '')}")
                    return "这次没认出这张单据——模型那边没返回结果。麻烦重新发一次。"

                doc_data = self._parse_doc_json(resp.get("content", ""))
                if doc_data is not None:
                    break
                # 这一轮没吐出可用 JSON，原样再要一次（原因见 MAX_EXTRACT_ATTEMPTS）
                usage = resp.get("usage") or {}
                print(f"[Agent] 第 {attempt} 次抽取没有可用 JSON"
                      f"（finish_reason={resp.get('finish_reason')}，正文仅 "
                      f"{usage.get('completion_tokens', 0) - usage.get('reasoning_tokens', 0)} token）")

            if doc_data is None:
                # 两遍都读不出来为止，但绝不能往库里塞垃圾记录——兜底逻辑会把
                # 文件名当品目、金额记成 0 入库，那种记录比没有更糟。
                return ("这张单据我读了两遍都没读出结构化信息，先没有入账。"
                        "麻烦重发一次，或者换一张清楚一点的图。")

        title = doc_data.get("title") or path_obj.stem
        doc_type = normalize_doc_type(doc_data.get("doc_type"), title)
        doc_data["doc_type"] = doc_type      # 归档时统一存归一化后的类型
        main_amount = self._to_float(doc_data.get("main_amount"))
        # 日期**不拿今天顶上**——那是编数据，事后还看不出是猜的。缺了就空着，
        # 下面把这件事回给模型，由它去问用户。
        main_date = str(doc_data.get("main_date") or "").strip()
        main_entity = str(doc_data.get("main_entity") or "").strip()
        raw_type_fields = doc_data.get("type_fields")
        type_fields = raw_type_fields if isinstance(raw_type_fields, dict) else {}
        other_type_name = str(doc_data.get("other_type_name") or "").strip()

        doc_id = self.db.insert_document(
            doc_type=doc_type,
            title=title,
            main_amount=main_amount,
            main_date=main_date,
            main_entity=main_entity,
            file_path=rel_path,
            extra_data=doc_data,
        )

        # 程序才看得见的问题攒起来回给模型，由它决定补正还是问用户。
        # 只提醒不拦截，单据已经入库了。
        warnings = self._collect_warnings(doc_id, main_date, main_entity, main_amount)

        model_reply = str(doc_data.get("reply") or "").strip()
        if warnings:
            extra = self._ask_model_about_warnings(model_reply, warnings)
            if extra:
                model_reply = f"{model_reply}\n{extra}" if model_reply else extra

        # 编号写在这条内部记录里（发给用户的是下面的 final_reply，不含编号）：
        # 用户事后补充信息时，模型得靠它知道该 update 哪一条。
        self.compressor.save_message(
            "user",
            f"[发送单据/截图: {path_obj.name}（已归档为 #{doc_id}）] {user_text}".strip())

        final_reply = self._render_receipt(
            doc_type=doc_type, title=title, main_amount=main_amount,
            main_date=main_date, main_entity=main_entity, type_fields=type_fields,
            media_meta=media_meta, other_type_name=other_type_name,
            model_reply=model_reply,
        )
        self.compressor.save_message("assistant", final_reply)
        return final_reply

    # ---------------------------------------------------------------- 回执与提示
    def _collect_warnings(self, doc_id: int, main_date: str, main_entity: str,
                          main_amount: float) -> List[str]:
        """查出"模型自己看不见"的问题。

        两类：核心信息没读到（模型可能以为读全了，但关键项是空的），以及库里已经
        有一条很接近的记录（得查过库才知道）。只提醒不拦截——单据照常入库。
        """
        warnings = []
        if not main_date:
            warnings.append("这张单据的日期没有读到")
        if not main_entity:
            warnings.append("这张单据的对方主体没有读到")
        try:
            similar = self.db.find_similar_documents(main_entity, main_amount, main_date,
                                                     exclude_id=doc_id)
        except Exception:
            similar = []
        if similar:
            d = similar[0]
            warnings.append(f"库里已经有一条很接近的记录（{d['title']} "
                            f"¥{d['main_amount']:.2f} / {d['main_date']} / {d['main_entity']}）")
        return warnings

    def _ask_model_about_warnings(self, just_said: str, warnings: List[str]) -> str:
        """把程序才发现的问题回给模型，由它自己决定怎么跟用户说。"""
        messages = [
            {"role": "assistant", "content": just_said or "（已归档，未附说明）"},
            {"role": "user", "content":
                "系统在你归档之后做了几项检查，发现下面这些情况：\n"
                + "\n".join(f"- {w}" for w in warnings)
                + "\n\n请补一句话发给用户，不要重复你上面已经说过的内容：\n"
                "- 信息缺了，就自然地问一句，不要写成报错\n"
                "- 疑似重复，说明你没有自动处理、请用户自己判断\n"
                "- 如果这几条其实不值得打扰用户，就只回四个字：无需补充"},
        ]
        resp = self.llm.chat_completion(messages, temperature=0.3,
                                        max_tokens=Config.MAX_OUTPUT_TOKENS)
        self._record_usage(resp)
        if resp.get("status") != "success":
            print(f"[Agent] 补充说明调用失败: {resp.get('message', '')}")
            return ""
        text = (resp.get("content") or "").strip()
        return "" if ("无需补充" in text or not text) else text

    @staticmethod
    def _render_receipt(*, doc_type: str, title: str, main_amount: float,
                        main_date: str, main_entity: str,
                        type_fields: Dict[str, Any], media_meta: Dict[str, Any],
                        other_type_name: str = "", model_reply: str = "") -> str:
        """拼出给用户看的回执。

        正文由模型写。程序只拼两样它拿不到的东西：
        - 开头一句"已归档"——存没存进去只有程序知道；
        - 页数的实情——模型只看到前几页，只有程序知道总共几页。

        这里**不要加回编号**（理由见 system 提示词）。
        """
        type_name = DOC_TYPE_NAMES.get(doc_type, "单据")
        # 归到 other 时"其他单据"等于没说，模型会给一个具体的中文名（如"电费账单"）
        if doc_type == "other" and other_type_name:
            type_name = other_type_name

        lines = [f"已归档 · {type_name}"]
        lines.append(model_reply or AgentRunner._fallback_summary(
            title, main_amount, main_date, main_entity, type_fields))

        if media_meta.get("kind") == "pdf":
            pages = media_meta.get("sampled_pages") or []
            total = media_meta.get("total_pages") or 0
            if total and len(pages) < total:
                read = "、".join(str(p) for p in pages)
                lines.append(f"（这份文档共 {total} 页，只读了第 {read} 页——金额、条款这类"
                             f"关键信息可能落在没读到的页上。）")
        return "\n".join(lines)

    @staticmethod
    def _fallback_summary(title: str, main_amount: float, main_date: str,
                          main_entity: str, type_fields: Dict[str, Any]) -> str:
        """模型没写 reply 时的兜底：把核心几项摆出来，不至于发一条空回执。

        刻意做得很薄——正常路径根本用不到它，不值得再为它写一套排版规则。
        """
        parts = [str(p) for p in (title, main_entity,
                                  f"¥{main_amount:,.2f}" if main_amount else "",
                                  main_date) if p]
        for key, value in list(type_fields.items())[:3]:
            if value not in (None, "", 0, 0.0):
                parts.append(f"{key} {value}")
        return " · ".join(parts) if parts else "（这张单据没读出内容）"

    @staticmethod
    def _parse_doc_json(content: str) -> Optional[Dict[str, Any]]:
        """从模型回复里找出那个 JSON 对象；确实找不到就返回 None。

        不要用 r"\\{.*\\}" 这种贪婪匹配——模型偶尔会在 JSON 前面多吐一段东西
        （实测见过它把 response_format 参数原样回显成 {"type": "json_object"}），
        贪婪匹配会把两段粘成一段，再解析必然失败。这里从每个 "{" 起试着解码，
        取第一个解得出来、且带 doc_type 的对象。
        """
        decoder = json.JSONDecoder()
        for i, ch in enumerate(content or ""):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(content[i:])
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("doc_type"):
                return obj
        return None

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
