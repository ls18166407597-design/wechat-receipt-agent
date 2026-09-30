"""
全局配置 (单用户 + 单模型，默认适配 DeepSeek / OpenAI 兼容接口)

**一个值只有一个家**:
- `.env` 只放三样——API Key、模型入口、模型名。它们因部署而异(密钥不能进仓库)，
  所以必须从外面喂进来；
- 其余**全部**是这里写死的常量，不再读环境变量。

为什么不像通用做法那样让 .env 也能覆盖这些: 那套做法是给"同一份代码跑在多个环境"
准备的，本项目只有一台服务器、一个用户，多出来的只是"改了 config 却不生效"的坑
(踩过一次)。更要紧的是 .env 被 gitignore，在那上面调的参数不进版本控制——
服务器重建就没了，翻仓库也翻不到。而这些参数改完本来就要重启服务，放哪边都一样。
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 读取 .env (无需外部依赖，手写稳健解析)
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.exists():
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip("'\""))


class Config:
    # 1. 单一模型的秘钥、入口与名称 (文本 + 视觉 + Tool Calling 共用)
    #    这三样来自 .env —— 唯一会因部署而异的东西
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    MODEL_NAME: str = os.getenv("MODEL_NAME", "deepseek-flash")

    # 2. 上下文窗口与整理阈值 (记忆怎么分层见 context_compressor 模块开头)
    #
    # 两个值都以「轮」计，一轮 = 一条用户消息及其回复。工具调用的结果帧也落库留底，
    # 但不进上下文、也不参与这里的计数（口径理由见 context_compressor 开头）。
    #
    # 这是"批量整理"，不是"滑动窗口": 攒够 MAX_UNCOMPRESSED_ROUNDS 轮才整理一次，
    # 砍回 RECENT_RAW_BUFFER_ROUNDS 轮，中间的轮次一条都不动。这样两次整理之间前缀
    # 逐字节不变、历史只追加，缓存次次命中；只有整理那一次改写前缀、丢一次缓存。
    #
    # 为什么不改成"按上下文长度(token)触发": 会累积的只有对话文本——单据的多模态载荷
    # 只在抽取那一次调用里存在，不进历史；实测一轮约 170 token，100 轮满载才约 1.7 万
    # token、占百万级窗口的 1.7%。也就是说这条触发器管的从来不是容量，而是**行为**:
    # 旧回复长期留在提示词里会被模型当范例照抄(踩过: 它学会说"我无法发送文件"并反复强化)，
    # 而"那张发票"这类指代只需要最近几轮。
    # 两者都是"该逐字记得往回多久"的问题，只有轮数表达得了，长度表达不了。
    # 推论: 轮数阈值就是上下文体积的唯一旋钮——以后若换到窗口小于 10 万的模型，
    # 把 MAX_UNCOMPRESSED_ROUNDS 降到 20~30 即可，不必再加一个长度开关。
    # (实测 300 轮工具密集会话: 296 轮前缀原样复用、仅 3 次改写，即整理那 3 次。)
    #
    # 窗口给 20 轮是够用的: 覆盖"发单据 → 模型追问 → 用户补充 → 改库"任意往返，
    # 又不至于把上个月的闲聊一直扛着(旧回复会被模型当范例照抄)。
    # 阈值定得"晚": 模型窗口 1M，几百轮远未触顶，整理是为了丢噪音而非省长度；
    # 而缓存命中比未命中便宜约 50 倍(0.003 vs 0.15 美元/百万 token)。
    # 附带一个反直觉的推论: 整理间隔 = 阈值 − 窗口，所以窗口给小一点，整理反而更少。
    MAX_UNCOMPRESSED_ROUNDS: int = 100
    RECENT_RAW_BUFFER_ROUNDS: int = 20

    # 3. 单用户: 全流程只使用这一个会话标识
    SESSION_ID: str = "local_user"

    # 4. 单次只读查询最多返回的行数 (防止模型 SELECT * 拉全表撑爆上下文)
    MAX_QUERY_ROWS: int = 200

    # 5. 工具结果回灌给模型时的最大行数 (超出部分不进上下文，只回报行数)
    MAX_TOOL_ROWS_IN_CONTEXT: int = 50

    # 6. 模型请求参数
    # 上限给小了会出现"推理吃满、正文为空"（机制见 llm_client 模块开头）——实测 2048
    # 就会撞上，而正文本身只要几百 token。常态推理量实测 60~694，按最坏情况留到 16384。
    # 调大不额外花钱：它只是天花板，用不到就不产生费用。
    REQUEST_TIMEOUT: int = 180
    MAX_OUTPUT_TOKENS: int = 16384

    # 7. 文本类文件的读取上限，以及 PDF 渲染参数
    #    PDF 只读前几页的理由见 pdf_processor 模块开头；实测渲染页约 1000 token/页。
    MAX_TEXT_CHARS: int = 20000
    PDF_MAX_SAMPLE_PAGES: int = 3
    PDF_RENDER_DPI: int = 150

    # 8. 存储路径
    DATA_DIR: Path = BASE_DIR / "data"
    ATTACHMENTS_DIR: Path = DATA_DIR / "attachments"
    DB_PATH: Path = DATA_DIR / "doc_agent.db"


Config.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
