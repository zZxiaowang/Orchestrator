"""OpenAI 兼容中转客户端。

设计要点：

* 一个 ``RelayClient`` 对应一个端点（base_url + api_key + wire_api）；
* 同时支持 ``chat_completions`` 与 ``responses`` 两种协议；
* base_url 是否带 ``/v1`` 由客户端自动探测并缓存，兼容各家中转的写法差异；
* 对模型参数差异（gpt-5 不接受 temperature、部分网关不支持 response_format）
  做**自动降级重试**，避免把网关差异暴露成用户故障；
* 限流与 5xx 做指数退避重试。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.errors import RelayError

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3

#: 命中这些关键字时，去掉对应参数重试（不同模型/网关支持度不同）
_PARAM_FALLBACKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("temperature", ("temperature",)),
    ("max_tokens", ("max_tokens", "max_completion_tokens")),
    ("response_format", ("response_format",)),
)


@dataclass
class RelayResult:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    endpoint_path: str = ""
    #: 指标采集用：本次调用实际发出的 HTTP 次数（含参数降级重发与 5xx/流式重试）
    attempts: int = 0
    #: 指标采集用：本次调用总耗时（毫秒，单调时钟，不参与事件排序）
    duration_ms: int = 0
    #: 本次实际使用的协议，便于统计口径区分（chat_completions / responses）
    protocol: str = "chat_completions"

    @property
    def normalized_usage(self) -> dict[str, int | None]:
        """指标采集用的统一 usage 口径（chat_completions / responses 通用）。"""
        return normalize_usage(self.usage)


def _excerpt(text: str | bytes, limit: int = 400) -> str:
    # 有些网关的错误体是 bytes（例如 resp.aread()），必须先解码再拼接，
    # 否则 " ".join(...) 会抛 "expected str instance, bytes found" 这种看不懂的错误。
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def normalize_usage(payload: Any) -> dict[str, int | None]:
    """把两种协议返回的 usage 归一化成统一口径（指标采集侧的唯一入口）。

    * ``chat_completions``：``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``；
    * ``responses``：``input_tokens`` / ``output_tokens`` / ``total_tokens``；
    * 既接受完整响应体（自动取其中的 ``usage``），也接受裸 usage 字典；
    * 缺失项一律保持 ``None``，由展示层渲染成「未知」——绝不用 0 冒充已知值。
    """

    result: dict[str, int | None] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    if not isinstance(payload, dict):
        return result
    candidate = payload.get("usage")
    source: dict[str, Any] = candidate if isinstance(candidate, dict) else payload
    for target, keys in (
        ("prompt_tokens", ("prompt_tokens", "input_tokens")),
        ("completion_tokens", ("completion_tokens", "output_tokens")),
        ("total_tokens", ("total_tokens",)),
    ):
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            result[target] = int(value)
            break
    if result["total_tokens"] is None:
        prompt, completion = result["prompt_tokens"], result["completion_tokens"]
        if prompt is not None and completion is not None:
            result["total_tokens"] = prompt + completion
    return result


def _hint_for(status: int) -> str:
    if status == 401:
        return "中转 Key 无效或未生效，请在设置里重新填写 RELAY_API_KEY。"
    if status == 403:
        return "该 Key 无权访问此模型，可能需要在网关侧开放对应模型分组。"
    if status == 404:
        return "端点路径不存在：检查 base_url 是否正确（是否需要 /v1 前缀）。"
    if status == 429:
        return "触发限流，稍后重试或检查网关额度。"
    if status == 502:
        return (
            "网关回源失败（Cloudflare 常见 bad_response_status_code，表示上游过载或异常）。"
            "已自动重试；仍失败请稍后再试，或在设置里切换到另一套配置。"
        )
    if status == 503:
        return "网关暂时不可用（503）。已自动重试；仍失败请稍后再试或切换配置。"
    if status == 504:
        return "网关上游超时（504）。已自动重试；仍失败请稍后再试或切换配置。"
    if status >= 500:
        return "网关或上游异常，已自动重试；持续失败请检查中转服务状态。"
    return "检查模型名是否在网关处可用。"


class RelayClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        wire_api: str = "chat_completions",
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
        model_hint: str = "",
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        # 统一清洗：粘贴 Key 时常见的引号 / Bearer 前缀 / 空白
        key = (api_key or "").strip().strip("\"'`").strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        self.api_key = key
        self.wire_api = wire_api
        self.timeout = timeout
        self._transport = transport
        #: 仅用于连通性自检时核对"目标模型是否在网关的模型列表里"
        self.model_hint = (model_hint or "").strip()
        self._resolved_base: str | None = None

    # ── 对外接口 ──

    async def acomplete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> RelayResult:
        body: dict[str, Any] = self._build_body(
            messages, model=model, stream=False, json_mode=json_mode, temperature=temperature
        )
        data, path = await self._post_with_fallbacks(body)
        return RelayResult(
            text=self._extract_text(data),
            model=data.get("model") or model,
            usage=data.get("usage") or {},
            raw=data,
            endpoint_path=path,
        )

    async def astream(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        body = self._build_body(
            messages, model=model, stream=True, json_mode=False, temperature=temperature
        )
        async for chunk in self._post_stream(body):
            yield chunk

    async def astream_with_fallback(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """优先流式；若网关明确拒绝流式（如不支持 ``stream: true``），退回一次性请求。

        很多中转对 ``stream`` 支持不一致，直接失败会让整个流程走不下去；
        这里在**尚未产出任何内容**时降级重试，已有输出则照常抛出，避免重复内容。
        """
        emitted = False
        try:
            async for chunk in self.astream(messages, model=model, temperature=temperature):
                emitted = True
                yield chunk
            return
        except RelayError as exc:
            if emitted or exc.status_code is None:
                raise

        result = await self.acomplete(messages, model=model, temperature=temperature)
        for chunk in iter_chunks(result.text, 120):
            yield chunk

    async def aping(self, timeout: float = 20.0) -> dict[str, Any]:
        """连通性自检：请求 ``GET {base}/models``（不消耗额度）。

        用途：保存配置后立刻知道"地址错、路径错、Key 无效"中的哪一种，
        而不是等跑完一次任务才看到 401。
        """
        self._require_configured()
        last_error: RelayError | None = None
        for base in self._candidate_bases():
            url = f"{base}/models"
            try:
                async with self._client() as client:
                    resp = await client.get(url, headers=self._headers(), timeout=timeout)
            except httpx.HTTPError as exc:
                last_error = RelayError(
                    f"无法连接 {url}：{exc.__class__.__name__}",
                    hint="检查网络、代理，或地址是否写错。",
                )
                continue

            if resp.status_code == 200:
                self._resolved_base = base
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                models = [
                    str(item.get("id"))
                    for item in (data.get("data") or [])
                    if isinstance(item, dict) and item.get("id")
                ]
                return {
                    "ok": True,
                    "url": url,
                    "model_count": len(models),
                    "models": models[:20],
                    "has_target_model": bool(self.model_hint and self.model_hint in models)
                    if models
                    else None,
                }

            if resp.status_code in (404, 405):
                last_error = RelayError(
                    f"{url} 返回 HTTP {resp.status_code}（路径不存在）",
                    status_code=resp.status_code,
                    hint="地址可达，但该路径不存在：检查 base_url 是否需要 /v1，或网关是否仅支持 chat/completions。",
                )
                continue

            return {
                "ok": False,
                "url": url,
                "status": resp.status_code,
                "message": _excerpt(resp.content),
                "hint": _hint_for(resp.status_code),
            }

        if last_error is None:
            raise RelayError("连接测试失败。")
        raise last_error

    # ── 请求构造 ──

    def _build_body(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str,
        stream: bool,
        json_mode: bool,
        temperature: float | None,
    ) -> dict[str, Any]:
        if self.wire_api == "responses":
            body: dict[str, Any] = {
                "model": model,
                "input": [
                    {
                        "role": m.get("role", "user"),
                        "content": [{"type": "input_text", "text": str(m.get("content", ""))}],
                    }
                    for m in messages
                ],
                "stream": stream,
            }
            if stream:
                body["stream"] = True
            return body

        body = {"model": model, "messages": list(messages), "stream": stream}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if temperature is not None:
            body["temperature"] = temperature
        return body

    def _candidate_bases(self) -> list[str]:
        if self._resolved_base:
            return [self._resolved_base]
        base = self.base_url
        candidates = [base]
        if base and not base.rstrip("/").endswith("/v1"):
            candidates.append(f"{base}/v1")
        return candidates

    def _path_for(self, base: str) -> str:
        suffix = "/chat/completions" if self.wire_api == "chat_completions" else "/responses"
        return f"{base}{suffix}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── 发送 ──

    async def _post_with_fallbacks(self, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        self._require_configured()
        last_error: RelayError | None = None
        current = dict(body)
        dropped: set[str] = set()

        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry_immediately = False
            for base in self._candidate_bases():
                path = self._path_for(base)
                try:
                    async with self._client() as client:
                        resp = await client.post(path, json=current, headers=self._headers())
                except httpx.HTTPError as exc:
                    last_error = RelayError(
                        f"无法连接中转服务：{exc.__class__.__name__}",
                        hint="检查网络、代理或 base_url 是否可达。",
                    )
                    continue

                if resp.status_code == 200:
                    self._resolved_base = base
                    try:
                        return resp.json(), path
                    except ValueError as exc:  # pragma: no cover - 网关返回非 JSON
                        raise RelayError(
                            "中转返回了非 JSON 响应。",
                            details={"body": _excerpt(resp.text)},
                        ) from exc

                if resp.status_code in (404, 405) and base != self._resolved_base:
                    last_error = RelayError(
                        f"端点不可用（HTTP {resp.status_code}）：{path}",
                        status_code=resp.status_code,
                        hint=_hint_for(404),
                    )
                    continue

                message = _excerpt(resp.text)
                if resp.status_code == 400 and self._drop_unsupported_param(
                    message, current, dropped
                ):
                    last_error = RelayError(
                        f"中转拒绝了请求参数（HTTP 400）：{message}",
                        status_code=400,
                        hint="已自动降级重试。",
                    )
                    retry_immediately = True
                    break  # 重新尝试外层循环

                last_error = RelayError(
                    f"中转返回 HTTP {resp.status_code}：{message}",
                    status_code=resp.status_code,
                    hint=_hint_for(resp.status_code),
                )
                if resp.status_code in RETRY_STATUS:
                    break  # 交给退避重试
                raise last_error

            if last_error is None:
                break
            if attempt < MAX_ATTEMPTS and not retry_immediately:
                await asyncio.sleep(1.5 * attempt)

        raise last_error or RelayError("中转调用失败，且没有可用端点。")

    async def _post_stream(self, body: dict[str, Any]) -> AsyncIterator[str]:
        self._require_configured()
        last_error: RelayError | None = None
        # 流式请求同样要重试：网关/上游偶发 502、503 很常见。
        # 只要尚未吐出任何内容，就按与非流式一致的退避策略重试。
        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry = False
            for base in self._candidate_bases():
                path = self._path_for(base)
                try:
                    async with (
                        self._client() as client,
                        client.stream("POST", path, json=body, headers=self._headers()) as resp,
                    ):
                        if resp.status_code in (404, 405):
                            last_error = RelayError(
                                f"端点不可用（HTTP {resp.status_code}）：{path}",
                                status_code=resp.status_code,
                                hint=_hint_for(404),
                            )
                            continue
                        if resp.status_code != 200:
                            detail = _excerpt(await resp.aread())
                            last_error = RelayError(
                                f"中转返回 HTTP {resp.status_code}：{detail}",
                                status_code=resp.status_code,
                                hint=_hint_for(resp.status_code),
                            )
                            if resp.status_code in RETRY_STATUS:
                                retry = True
                                break
                            raise last_error
                        self._resolved_base = base
                        async for chunk in self._parse_sse(resp):
                            yield chunk
                        return
                except httpx.HTTPError as exc:
                    last_error = RelayError(
                        f"流式连接中断：{exc.__class__.__name__}",
                        hint="检查网络稳定性或调大 REQUEST_TIMEOUT_SECONDS。",
                    )
                    retry = True
                    break
            if not retry:
                break
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(1.5 * attempt)
        raise last_error or RelayError("流式请求失败，且没有可用端点。")

    async def _parse_sse(self, resp: httpx.Response) -> AsyncIterator[str]:
        emitted = False
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if self.wire_api == "responses":
                chunk = self._extract_responses_delta(data, emitted)
            else:
                chunk = self._extract_delta(data)
            if chunk:
                emitted = True
                yield chunk

    def _extract_responses_delta(self, data: dict[str, Any], emitted: bool) -> str:
        """Responses 协议：以增量事件为准；只有全程没有增量时才用 completed 的全文。

        否则会把"逐字推送"和"最终全文"叠加，导致 JSON 被重复拼接而解析失败。
        """
        event_type = str(data.get("type") or "")
        if event_type.endswith("output_text.delta"):
            return str(data.get("delta") or "")
        if event_type == "response.completed" and not emitted:
            return self._extract_responses_text(data.get("response") or {})
        return ""

    # ── 响应解析 ──

    def _extract_text(self, data: dict[str, Any]) -> str:
        if self.wire_api == "responses":
            return self._extract_responses_text(data)
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 少数网关返回分段内容
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content or "")

    def _extract_responses_text(self, data: dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        parts: list[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        return "".join(parts)

    def _extract_delta(self, data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return ""

    # ── 辅助 ──

    def _drop_unsupported_param(
        self, message: str, body: dict[str, Any], dropped: set[str]
    ) -> bool:
        lowered = message.lower()
        for label, names in _PARAM_FALLBACKS:
            if label in dropped:
                continue
            if any(name.lower() in lowered for name in names):
                for name in names:
                    body.pop(name, None)
                dropped.add(label)
                return True
        return False

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=True,
        )

    def _require_configured(self) -> None:
        if not self.base_url:
            raise RelayError(
                "未配置中转地址（base_url）。",
                hint="在设置中填写中转网关地址，例如 https://your-relay.example.com/v1。",
            )
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RelayError(
                f"中转地址格式不正确：{self.base_url}",
                hint="地址必须以 http:// 或 https:// 开头，例如 https://your-relay.example.com/v1。",
                details={"base_url": self.base_url},
            )
        if not self.api_key:
            raise RelayError(
                "未配置中转 Key。",
                hint="在设置中填写 RELAY_API_KEY，或在 .env 中设置后重启。",
            )


def iter_chunks(text: str, size: int = 24) -> Iterable[str]:
    """把整段文本切成小块，用于模拟流式输出（测试与回放用）。"""
    for index in range(0, len(text), size):
        yield text[index : index + size]
