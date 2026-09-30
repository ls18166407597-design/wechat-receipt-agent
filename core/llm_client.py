"""
标准通用 OpenAI Chat Completions 协议客户端 (适配 DeepSeek / 百炼 / 月之暗面 / OpenAI)
零外部重型依赖，基于 Python 标准库 urllib.request 构建。

关于本项目的模型特性 (实测):
- 所选模型是常开推理模型: 每次调用都会返回 reasoning_content 与 reasoning_tokens，
  且 reasoning token 计入输出上限，上限给小了会出现"推理吃满、正文为空"。
- reasoning_effort / thinking / enable_thinking 等思考强度参数虽被接口接受，
  但实测对 reasoning_tokens 无明显影响，因此不引入该配置，避免造出无效开关。
- 支持 response_format={"type":"json_object"}，可用于单据抽取的强约束输出。
"""
import json
import time
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional

from .config import Config


class OpenAICompatibleClient:
    # 值得重试的错误：限流、网关抖动、服务端临时故障。其余 4xx 是请求本身的问题，
    # 重试只是白等，直接返回。连接超时/断连一律按可重试处理。
    RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
    MAX_ATTEMPTS = 3
    BACKOFF_BASE = 1.5      # 秒；第 n 次失败后等 BACKOFF_BASE * 2^(n-1)

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None):
        self.api_key = api_key or Config.OPENAI_API_KEY
        self.base_url = (base_url or Config.OPENAI_BASE_URL).rstrip("/")
        self.model = model or Config.MODEL_NAME

    def _post_once(self, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        """单次请求，只负责发出并把响应解析成 dict；异常一律往上抛由调用方决定是否重试"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _success(result: Dict[str, Any]) -> Dict[str, Any]:
        choice = result["choices"][0]
        message = choice.get("message", {})
        usage = result.get("usage", {})
        details = usage.get("completion_tokens_details", {}) or {}
        return {
            "status": "success",
            "role": message.get("role", "assistant"),
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls", []),
            "finish_reason": choice.get("finish_reason"),
            "reasoning_content": message.get("reasoning_content"),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "reasoning_tokens": details.get("reasoning_tokens", 0),
                "cache_hit_tokens": usage.get("prompt_cache_hit_tokens", 0),
            },
        }

    def chat_completion(self, messages: List[Dict[str, Any]],
                        tools: Optional[List[Dict[str, Any]]] = None,
                        temperature: float = 0.1,
                        response_format: Optional[Dict[str, str]] = None,
                        max_tokens: Optional[int] = None,
                        timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        调用标准 /v1/chat/completions 接口，带有限次退避重试。

        为什么要重试：模型服务不在本项目控制范围内，一次偶发的连接超时或 503
        会让用户直接看到"模型服务暂时异常"。重试两次的成本远低于演示当场失败。

        :param response_format: 传 {"type": "json_object"} 可强约束输出为合法 JSON
        :param max_tokens: 输出上限，需为推理 token + 正文留足空间
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format
        if max_tokens:
            payload["max_tokens"] = max_tokens

        effective_timeout = timeout or Config.REQUEST_TIMEOUT
        last_error: Dict[str, Any] = {"status": "error", "code": -1, "message": "未发起请求"}

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                return self._success(self._post_once(payload, effective_timeout))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                last_error = {
                    "status": "error",
                    "code": e.code,
                    "message": f"HTTP错误 ({e.code}): {body}",
                }
                if e.code not in self.RETRYABLE_STATUS:
                    return last_error
            except Exception as e:
                last_error = {"status": "error", "code": -1, "message": f"请求异常: {str(e)}"}

            if attempt < self.MAX_ATTEMPTS:
                delay = self.BACKOFF_BASE * (2 ** (attempt - 1))
                print(f"[LLM] 第 {attempt} 次调用失败（{last_error['message'][:100]}），"
                      f"{delay:.1f}s 后重试")
                time.sleep(delay)

        print(f"[LLM] 重试 {self.MAX_ATTEMPTS} 次后仍失败：{last_error['message'][:150]}")
        return last_error

