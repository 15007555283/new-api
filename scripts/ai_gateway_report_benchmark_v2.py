#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 网关 HTML 报告型压测脚本 v2。

目标：
1. 对 OpenAI 兼容的 /v1/chat/completions 网关做接口、并发、上下文、Token、缓存和一致性检查。
2. 生成一份可直接交付的 HTML 报告，同时输出原始 JSON 数据便于复盘。
3. 保持脚本结构清楚：请求客户端、测试执行、结果分析、HTML 渲染彼此分离。

示例：
  AI_GATEWAY_API_KEY=sk-xxx python3 scripts/ai_gateway_report_benchmark_v2.py \
    --url https://ai.iootx.com/v1 \
    --model deepseek-v4-flash-ga-260731 \
    --output DeepSeek-V4-Flash-基准测试报告-v2.html
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ai_gateway_report_renderer import HtmlReportRenderer


SCRIPT_VERSION = "v2"
DEFAULT_MODEL = "deepseek-v4-flash-ga-260731"
DEFAULT_URL = "https://ai.iootx.com/v1"


CONTEXT_STANDARDS: Dict[str, Dict[str, Any]] = {
    "short": {
        "label": "短上下文",
        "chars": 2_000,
        "a_ttft_p50_ms": 800,
        "b_ttft_p50_ms": 1_200,
    },
    "medium": {
        "label": "中上下文",
        "chars": 16_000,
        "a_ttft_p50_ms": 1_600,
        "b_ttft_p50_ms": 2_200,
    },
    "long": {
        "label": "长上下文",
        "chars": 48_000,
        "a_ttft_p50_ms": 2_800,
        "b_ttft_p50_ms": 3_800,
    },
}


CONSISTENCY_CASES: List[Dict[str, Any]] = [
    {
        "question": "1 + 1 等于几？请只回答数字。",
        "keywords": ["2", "二"],
    },
    {
        "question": "水的化学式是什么？",
        "keywords": ["h2o", "h₂o", "水"],
    },
    {
        "question": "中国首都是哪个城市？",
        "keywords": ["北京"],
    },
    {
        "question": "Python 中用于打印输出的内置函数是什么？",
        "keywords": ["print"],
    },
    {
        "question": "HTTP 协议默认使用哪个端口？",
        "keywords": ["80"],
    },
    {
        "question": "大语言模型的核心注意力机制英文名称是什么？",
        "keywords": ["attention", "self-attention", "自注意力", "注意力"],
    },
]


@dataclasses.dataclass
class BenchmarkConfig:
    endpoint: str
    api_key: str
    model: str
    output: Path
    json_output: Optional[Path]
    stream: bool
    prompt: str
    max_tokens: int
    concurrency_levels: List[int]
    requests_per_level: int
    timeout: float
    long_timeout: float
    retries: int
    backoff: float
    extra_headers: Dict[str, str]
    skip_api: bool
    skip_context: bool
    skip_token: bool
    skip_consistency: bool
    context_levels: List[str]
    context_samples: int
    context_max_tokens: int
    reasoning_scan: bool
    reasoning_effort: str
    token_runs: int
    token_max_tokens: List[int]
    cache_repeat: int
    cache_prefix_chars: int
    prompt_cache_key: str
    consistency_repeat: int
    started_at: str

    def public_meta(self) -> Dict[str, Any]:
        return {
            "script_version": SCRIPT_VERSION,
            "endpoint": self.endpoint,
            "model": self.model,
            "stream": self.stream,
            "concurrency_levels": self.concurrency_levels,
            "requests_per_level": self.requests_per_level,
            "timeout": self.timeout,
            "long_timeout": self.long_timeout,
            "retries": self.retries,
            "backoff": self.backoff,
            "context_levels": self.context_levels,
            "context_samples": self.context_samples,
            "context_max_tokens": self.context_max_tokens,
            "reasoning_scan": self.reasoning_scan,
            "reasoning_effort": self.reasoning_effort,
            "token_runs": self.token_runs,
            "token_max_tokens": self.token_max_tokens,
            "cache_repeat": self.cache_repeat,
            "cache_prefix_chars": self.cache_prefix_chars,
            "prompt_cache_key": self.prompt_cache_key,
            "consistency_repeat": self.consistency_repeat,
            "started_at": self.started_at,
        }


@dataclasses.dataclass
class RequestResult:
    ok: bool
    latency_ms: float
    status: int = 0
    first_byte_ms: Optional[float] = None
    ttft_ms: Optional[float] = None
    content: str = ""
    reasoning_content: str = ""
    raw_text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    reasoning_tokens: int = 0
    chunk_count: int = 0
    error: str = ""
    error_kind: str = ""
    attempts: int = 1
    request_id: str = ""
    tool_calls: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def infra_error(self) -> bool:
        return self.error_kind == "infra"

    @property
    def business_error(self) -> bool:
        return self.error_kind == "business"

    @property
    def effective_ttft_ms(self) -> Optional[float]:
        return self.ttft_ms if self.ttft_ms is not None else self.first_byte_ms

    @property
    def cache_hit_tokens(self) -> int:
        return max(self.cached_tokens, self.prompt_cache_hit_tokens)

    @property
    def tpot_ms(self) -> Optional[float]:
        ttft = self.effective_ttft_ms
        if not self.ok or ttft is None or self.completion_tokens <= 1:
            return None
        remain_ms = max(self.latency_ms - ttft, 0.0)
        return remain_ms / max(self.completion_tokens - 1, 1)

    @property
    def otps(self) -> float:
        if not self.ok or self.latency_ms <= 0 or self.completion_tokens <= 0:
            return 0.0
        return self.completion_tokens / (self.latency_ms / 1000.0)

    def usage_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "prompt_cache_hit_tokens": self.prompt_cache_hit_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def avg(values: Iterable[float]) -> float:
    items = [v for v in values if v is not None and math.isfinite(v)]
    if not items:
        return 0.0
    return sum(items) / len(items)


def pct(values: Sequence[float], percentile: float) -> float:
    items = sorted(v for v in values if v is not None and math.isfinite(v))
    if not items:
        return 0.0
    if len(items) == 1:
        return items[0]
    rank = (len(items) - 1) * (percentile / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return items[int(rank)]
    return items[low] * (high - rank) + items[high] * (rank - low)


def rate(ok: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return ok * 100.0 / total


def ms(value: Optional[float]) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    return f"{value:.0f}ms"


def one_decimal(value: float) -> str:
    if not math.isfinite(value):
        return "-"
    return f"{value:.1f}"


def short_err(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def normalize_chat_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return DEFAULT_URL + "/chat/completions"
    if cleaned.endswith("/chat/completions"):
        return cleaned
    if cleaned.endswith("/v1"):
        return cleaned + "/chat/completions"
    return cleaned + "/v1/chat/completions"


def estimate_tokens(text: str) -> int:
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, int(ascii_count / 4 + non_ascii_count * 0.8))


def text_from_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def make_context(chars: int) -> str:
    base = (
        "这是一段用于 AI 网关压测的稳定上下文。它重复描述系统架构、缓存命中、"
        "首 token 延迟、并发吞吐、错误率、重试策略、Nginx 转发和上游模型响应。"
        "请把它当作真实业务中的长系统提示词或长知识库片段。"
    )
    repeated = (base * ((chars // len(base)) + 2))[:chars]
    return repeated


class UsageAccumulator:
    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cached_tokens = 0
        self.prompt_cache_hit_tokens = 0
        self.reasoning_tokens = 0

    def merge(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return

        self.prompt_tokens = max(
            self.prompt_tokens,
            as_int(usage.get("prompt_tokens")),
            as_int(usage.get("input_tokens")),
        )
        self.completion_tokens = max(
            self.completion_tokens,
            as_int(usage.get("completion_tokens")),
            as_int(usage.get("output_tokens")),
        )
        self.total_tokens = max(self.total_tokens, as_int(usage.get("total_tokens")))

        prompt_details = usage.get("prompt_tokens_details")
        input_details = usage.get("input_tokens_details")
        completion_details = usage.get("completion_tokens_details")
        output_details = usage.get("output_tokens_details")

        cache_candidates = [
            usage.get("cached_tokens"),
            usage.get("prompt_cache_hit_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("prompt_cache_read_tokens"),
        ]
        if isinstance(prompt_details, dict):
            cache_candidates.extend(
                [
                    prompt_details.get("cached_tokens"),
                    prompt_details.get("cache_read_input_tokens"),
                    prompt_details.get("prompt_cache_hit_tokens"),
                ]
            )
        if isinstance(input_details, dict):
            cache_candidates.extend(
                [
                    input_details.get("cached_tokens"),
                    input_details.get("cache_read_input_tokens"),
                    input_details.get("prompt_cache_hit_tokens"),
                ]
            )
        self.cached_tokens = max(self.cached_tokens, *(as_int(v) for v in cache_candidates))

        top_level_hit = as_int(usage.get("prompt_cache_hit_tokens"))
        if top_level_hit:
            self.prompt_cache_hit_tokens = max(self.prompt_cache_hit_tokens, top_level_hit)
        elif self.cached_tokens:
            self.prompt_cache_hit_tokens = max(self.prompt_cache_hit_tokens, self.cached_tokens)

        reasoning_candidates = []
        if isinstance(completion_details, dict):
            reasoning_candidates.extend(
                [
                    completion_details.get("reasoning_tokens"),
                    completion_details.get("reasoning_output_tokens"),
                ]
            )
        if isinstance(output_details, dict):
            reasoning_candidates.extend(
                [
                    output_details.get("reasoning_tokens"),
                    output_details.get("reasoning_output_tokens"),
                ]
            )
        self.reasoning_tokens = max(self.reasoning_tokens, *(as_int(v) for v in reasoning_candidates), 0)

    def apply_to(self, result: RequestResult) -> None:
        result.prompt_tokens = self.prompt_tokens
        result.completion_tokens = self.completion_tokens
        result.total_tokens = self.total_tokens
        result.cached_tokens = self.cached_tokens
        result.prompt_cache_hit_tokens = self.prompt_cache_hit_tokens
        result.reasoning_tokens = self.reasoning_tokens


class AiGatewayClient:
    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
        stream: Optional[bool] = None,
        timeout: Optional[float] = None,
        temperature: float = 0.2,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> RequestResult:
        last: Optional[RequestResult] = None
        attempts = max(self.config.retries, 1)
        for attempt in range(1, attempts + 1):
            result = self._chat_once(
                messages,
                max_tokens=max_tokens or self.config.max_tokens,
                stream=self.config.stream if stream is None else stream,
                timeout=timeout or self.config.timeout,
                temperature=temperature,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                extra_body=extra_body,
                attempt=attempt,
            )
            last = result
            if result.ok or not self._should_retry(result) or attempt == attempts:
                return result
            time.sleep(self.config.backoff * attempt)
        return last or RequestResult(ok=False, latency_ms=0, error="unknown error", error_kind="infra")

    def _chat_once(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int,
        stream: bool,
        timeout: float,
        temperature: float,
        response_format: Optional[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        tool_choice: Optional[Any],
        extra_body: Optional[Dict[str, Any]],
        attempt: int,
    ) -> RequestResult:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if response_format is not None:
            payload["response_format"] = response_format
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if extra_body:
            payload.update(extra_body)

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": f"ai-gateway-report-benchmark/{SCRIPT_VERSION}",
        }
        headers.update(self.config.extra_headers)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.config.endpoint, data=body, headers=headers, method="POST")
        started = time.perf_counter()

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                request_id = response.headers.get("x-request-id") or response.headers.get("request-id") or ""
                if stream:
                    return self._read_stream(response, started, attempt, request_id)
                return self._read_json_response(response, started, attempt, request_id)
        except urllib.error.HTTPError as exc:
            return self._from_http_error(exc, started, attempt)
        except urllib.error.URLError as exc:
            return RequestResult(
                ok=False,
                latency_ms=elapsed_ms(started),
                error=short_err(str(exc)),
                error_kind="infra",
                attempts=attempt,
            )
        except TimeoutError as exc:
            return RequestResult(
                ok=False,
                latency_ms=elapsed_ms(started),
                error=short_err(str(exc) or "timeout"),
                error_kind="infra",
                attempts=attempt,
            )
        except Exception as exc:
            return RequestResult(
                ok=False,
                latency_ms=elapsed_ms(started),
                error=short_err(f"{type(exc).__name__}: {exc}"),
                error_kind="infra",
                attempts=attempt,
            )

    def _read_json_response(
        self,
        response: Any,
        started: float,
        attempt: int,
        request_id: str,
    ) -> RequestResult:
        raw = response.read().decode("utf-8", errors="replace")
        latency = elapsed_ms(started)
        usage = UsageAccumulator()
        result = RequestResult(
            ok=False,
            latency_ms=latency,
            status=getattr(response, "status", 200),
            first_byte_ms=latency,
            ttft_ms=latency,
            raw_text=raw,
            attempts=attempt,
            request_id=request_id,
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            result.error = short_err(f"JSON parse error: {exc}; body={raw[:300]}")
            result.error_kind = "infra"
            return result

        usage.merge(data.get("usage"))
        result.content, result.reasoning_content, result.tool_calls = self._extract_message(data)
        usage.apply_to(result)

        if isinstance(data.get("error"), dict):
            result.error = short_err(data["error"].get("message") or str(data["error"]))
            result.error_kind = self._classify_status(result.status)
        else:
            result.ok = 200 <= result.status < 300
        return result

    def _read_stream(
        self,
        response: Any,
        started: float,
        attempt: int,
        request_id: str,
    ) -> RequestResult:
        usage = UsageAccumulator()
        result = RequestResult(
            ok=False,
            latency_ms=0,
            status=getattr(response, "status", 200),
            attempts=attempt,
            request_id=request_id,
        )
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        raw_lines: List[str] = []
        saw_payload = False

        for raw_line in response:
            if result.first_byte_ms is None:
                result.first_byte_ms = elapsed_ms(started)

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            raw_lines.append(line)
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            result.chunk_count += 1
            usage.merge(event.get("usage"))

            if isinstance(event.get("error"), dict):
                result.error = short_err(event["error"].get("message") or str(event["error"]))
                result.error_kind = self._classify_status(result.status)
                continue

            delta_content, delta_reasoning, delta_tools = self._extract_stream_delta(event)
            if delta_content:
                content_parts.append(delta_content)
            if delta_reasoning:
                reasoning_parts.append(delta_reasoning)
            if delta_tools:
                result.tool_calls.extend(delta_tools)
            if (delta_content or delta_reasoning or delta_tools) and result.ttft_ms is None:
                result.ttft_ms = elapsed_ms(started)
            if delta_content or delta_reasoning or delta_tools or event.get("choices"):
                saw_payload = True

        result.latency_ms = elapsed_ms(started)
        result.content = "".join(content_parts)
        result.reasoning_content = "".join(reasoning_parts)
        result.raw_text = "\n".join(raw_lines[-80:])
        usage.apply_to(result)
        result.ok = 200 <= result.status < 300 and not result.error and saw_payload
        if not result.ok and not result.error:
            result.error = "stream ended without model payload"
            result.error_kind = "infra"
        return result

    def _from_http_error(self, exc: urllib.error.HTTPError, started: float, attempt: int) -> RequestResult:
        raw = exc.read().decode("utf-8", errors="replace")
        message = raw
        try:
            data = json.loads(raw)
            if isinstance(data.get("error"), dict):
                message = data["error"].get("message") or str(data["error"])
        except json.JSONDecodeError:
            pass
        return RequestResult(
            ok=False,
            latency_ms=elapsed_ms(started),
            status=exc.code,
            error=short_err(message),
            error_kind=self._classify_status(exc.code),
            raw_text=raw,
            attempts=attempt,
        )

    def _extract_message(self, data: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return "", "", []
        message = choices[0].get("message") or {}
        content = text_from_content(message.get("content"))
        reasoning = text_from_content(message.get("reasoning_content") or message.get("reasoning"))
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        return content, reasoning, tool_calls

    def _extract_stream_delta(self, event: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
        choices = event.get("choices")
        if not isinstance(choices, list):
            return "", "", []

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        for choice in choices:
            delta = choice.get("delta") or choice.get("message") or {}
            content = text_from_content(delta.get("content"))
            reasoning = text_from_content(delta.get("reasoning_content") or delta.get("reasoning"))
            if content:
                content_parts.append(content)
            if reasoning:
                reasoning_parts.append(reasoning)
            delta_tool_calls = delta.get("tool_calls")
            if isinstance(delta_tool_calls, list):
                tool_calls.extend(delta_tool_calls)
        return "".join(content_parts), "".join(reasoning_parts), tool_calls

    def _classify_status(self, status: int) -> str:
        if status == 429 or status >= 500 or status == 0:
            return "infra"
        return "business"

    def _should_retry(self, result: RequestResult) -> bool:
        return result.infra_error or result.status in {408, 409, 425, 429, 500, 502, 503, 504}


class BenchmarkRunner:
    def __init__(self, client: AiGatewayClient, config: BenchmarkConfig) -> None:
        self.client = client
        self.config = config

    def run(self) -> Dict[str, Any]:
        started = time.perf_counter()
        module_times: Dict[str, str] = {}
        data: Dict[str, Any] = {
            "meta": self.config.public_meta(),
            "api_rows": [],
            "concurrency_rows": [],
            "context_rows": [],
            "token_rows": [],
            "cache_rows": [],
            "cache_summary": {},
            "consistency_rows": [],
        }

        if not self.config.skip_api:
            module_times["api_started_at"] = now_str()
            data["api_rows"] = self.test_api_capabilities()

        module_times["concurrency_started_at"] = now_str()
        data["concurrency_rows"] = self.test_concurrency()

        if not self.config.skip_context:
            module_times["context_started_at"] = now_str()
            data["context_rows"] = self.test_context()

        if not self.config.skip_token:
            module_times["token_started_at"] = now_str()
            token_rows, cache_rows, cache_summary = self.test_token_metrics()
            data["token_rows"] = token_rows
            data["cache_rows"] = cache_rows
            data["cache_summary"] = cache_summary

        if not self.config.skip_consistency:
            module_times["consistency_started_at"] = now_str()
            data["consistency_rows"] = self.test_consistency()

        data["meta"]["module_times"] = module_times
        data["meta"]["finished_at"] = now_str()
        data["meta"]["duration_seconds"] = round(time.perf_counter() - started, 2)
        data["admission"] = AdmissionAnalyzer().analyze(data)
        return data

    def test_api_capabilities(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []

        def add_row(name: str, result: RequestResult, ok: bool, detail: str) -> None:
            rows.append(
                {
                    "name": name,
                    "ok": ok,
                    "status": result.status,
                    "latency_ms": result.latency_ms,
                    "ttft_ms": result.effective_ttft_ms,
                    "detail": detail,
                }
            )

        basic = self._simple_call("用一句话说明 AI 网关是什么。", max_tokens=120)
        add_row(
            "基础对话",
            basic,
            basic.ok and bool(basic.content),
            f"响应长度={len(basic.content)}，reasoning长度={len(basic.reasoning_content)}，内容片段='{short_err(basic.content, 90)}'",
        )

        stream = self._simple_call("请流式输出：1, 2, 3, 4, 5。", max_tokens=80, stream=True)
        add_row(
            "流式输出",
            stream,
            stream.ok and (stream.chunk_count > 1 or bool(stream.content)),
            f"chunk数={stream.chunk_count}，TTFT={ms(stream.effective_ttft_ms)}，总时长={ms(stream.latency_ms)}，内容='{short_err(stream.content, 80)}'",
        )

        json_result = self._simple_call(
            '只输出 JSON，不要解释。JSON 内容为 {"name":"张三","age":25}。',
            max_tokens=120,
            response_format={"type": "json_object"},
        )
        json_ok = False
        json_detail = short_err(json_result.content or json_result.raw_text, 120)
        if json_result.ok:
            try:
                parsed = json.loads(json_result.content)
                json_ok = parsed.get("name") == "张三" and as_int(parsed.get("age")) == 25
                json_detail = f"解析成功: {parsed}"
            except json.JSONDecodeError as exc:
                json_detail = f"解析失败: {exc}; 内容={short_err(json_result.content, 100)}"
        add_row("JSON 格式输出", json_result, json_ok, json_detail)

        system_result = self.client.chat(
            [
                {"role": "system", "content": "You must answer in English only."},
                {"role": "user", "content": "请用一句英文说明今天适合做什么测试。"},
            ],
            max_tokens=120,
        )
        englishish = system_result.ok and not any("\u4e00" <= ch <= "\u9fff" for ch in system_result.content)
        add_row(
            "系统提示词遵循",
            system_result,
            englishish,
            f"全英文={englishish}，内容='{short_err(system_result.content, 100)}'",
        )

        first_turn = self._simple_call("请记住我的名字是张三，只回复已记住。", max_tokens=80)
        second_turn = self.client.chat(
            [
                {"role": "user", "content": "请记住我的名字是张三，只回复已记住。"},
                {"role": "assistant", "content": first_turn.content or "已记住"},
                {"role": "user", "content": "我的名字是什么？"},
            ],
            max_tokens=120,
        )
        add_row(
            "多轮对话",
            second_turn,
            second_turn.ok and "张三" in second_turn.content,
            f"第一轮='{short_err(first_turn.content, 60)}'，第二轮='{short_err(second_turn.content, 90)}'",
        )

        tool_schema = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查询城市天气",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string"},
                            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                        },
                        "required": ["city"],
                    },
                },
            }
        ]
        tool_result = self.client.chat(
            [{"role": "user", "content": "深圳现在天气怎么样？请调用工具 get_weather。"}],
            max_tokens=120,
            tools=tool_schema,
            tool_choice="auto",
        )
        tool_name = self._first_tool_name(tool_result.tool_calls)
        add_row(
            "函数调用",
            tool_result,
            tool_result.ok and tool_name == "get_weather",
            f"函数名={tool_name or '-'}，工具调用={short_err(json.dumps(tool_result.tool_calls, ensure_ascii=False), 160)}",
        )

        context = make_context(1_500)
        long_result = self.client.chat(
            [
                {"role": "system", "content": context},
                {"role": "user", "content": "上面这段文字主要测试什么？请用一句话回答。"},
            ],
            max_tokens=120,
        )
        add_row(
            "长文本处理",
            long_result,
            long_result.ok and bool(long_result.content),
            f"输入长度={len(context)}字，输出长度={len(long_result.content)}字，回答='{short_err(long_result.content, 100)}'",
        )

        bad_role = self.client.chat(
            [{"role": "invalid_role", "content": "这条请求应该失败"}],
            max_tokens=40,
            stream=False,
        )
        add_row(
            "错误处理",
            bad_role,
            (not bad_role.ok) and bad_role.business_error,
            f"HTTP {bad_role.status}，错误信息='{short_err(bad_role.error or bad_role.raw_text, 160)}'",
        )

        contract = self._simple_call("请回复 ok。", max_tokens=20)
        usage_ok = contract.ok and contract.prompt_tokens >= 0 and contract.completion_tokens >= 0
        add_row(
            "响应契约校验",
            contract,
            usage_ok,
            f"usage={contract.usage_dict()}，request_id={contract.request_id or '-'}",
        )

        rows.sort(key=lambda row: (not row["ok"], row["latency_ms"]))
        return rows

    def test_concurrency(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for concurrency in self.config.concurrency_levels:
            print(f"[concurrency] c={concurrency}, requests={self.config.requests_per_level}", flush=True)
            rows.append(self.run_concurrency_level(concurrency))
        return rows

    def run_concurrency_level(self, concurrency: int) -> Dict[str, Any]:
        prompt = self.config.prompt
        results: List[RequestResult] = []
        started = time.perf_counter()
        lock = threading.Lock()

        def worker(index: int) -> RequestResult:
            user_prompt = f"{prompt}\n\n请求编号：{index}。"
            try:
                return self._simple_call(user_prompt, max_tokens=self.config.max_tokens)
            except Exception as exc:
                return RequestResult(
                    ok=False,
                    latency_ms=0,
                    error=short_err(f"{type(exc).__name__}: {exc}"),
                    error_kind="infra",
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(worker, i + 1) for i in range(self.config.requests_per_level)]
            for future in concurrent.futures.as_completed(futures):
                with lock:
                    results.append(future.result())

        wall = max(time.perf_counter() - started, 0.001)
        ok_results = [r for r in results if r.ok]
        latencies = [r.latency_ms for r in ok_results]
        ttfts = [r.effective_ttft_ms for r in ok_results if r.effective_ttft_ms is not None]
        tpots = [r.tpot_ms for r in ok_results if r.tpot_ms is not None]
        infra_errors = sum(1 for r in results if r.infra_error)
        business_errors = sum(1 for r in results if r.business_error)
        completion_tokens = sum(r.completion_tokens for r in ok_results)
        total_tokens = sum(r.total_tokens for r in ok_results)

        return {
            "concurrency": concurrency,
            "requests": len(results),
            "success": len(ok_results),
            "success_rate": rate(len(ok_results), len(results)),
            "error_rate": rate(len(results) - len(ok_results), len(results)),
            "infra_error_rate": rate(infra_errors, len(results)),
            "business_error_rate": rate(business_errors, len(results)),
            "avg_latency_ms": avg(latencies),
            "p50_ms": pct(latencies, 50),
            "p90_ms": pct(latencies, 90),
            "p99_ms": pct(latencies, 99),
            "avg_ttft_ms": avg(ttfts),
            "ttft_p50_ms": pct(ttfts, 50),
            "ttft_p90_ms": pct(ttfts, 90),
            "ttft_p99_ms": pct(ttfts, 99),
            "avg_tpot_ms": avg(tpots),
            "throughput_rps": len(ok_results) / wall,
            "wall_seconds": wall,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "token_per_sec_wall": completion_tokens / wall if wall > 0 else 0.0,
            "errors": self._top_errors(results),
        }

    def test_context(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        reasoning_modes = ["reasoning_on", "reasoning_off"] if self.config.reasoning_scan else ["default"]

        for level in self.config.context_levels:
            standard = CONTEXT_STANDARDS[level]
            context = make_context(standard["chars"])
            for reasoning_mode in reasoning_modes:
                print(f"[context] {standard['label']} {reasoning_mode}", flush=True)
                extra_body: Dict[str, Any] = {}
                if reasoning_mode == "reasoning_on":
                    extra_body["reasoning_effort"] = self.config.reasoning_effort
                elif reasoning_mode == "reasoning_off":
                    extra_body["reasoning_effort"] = "none"

                results = [
                    self.client.chat(
                        [
                            {"role": "system", "content": context},
                            {"role": "user", "content": "请用 5 点总结上文，并只围绕压测指标回答。"},
                        ],
                        max_tokens=self.config.context_max_tokens,
                        timeout=self.config.long_timeout,
                        extra_body=extra_body or None,
                    )
                    for _ in range(self.config.context_samples)
                ]

                ok_results = [r for r in results if r.ok]
                ttfts = [r.effective_ttft_ms for r in ok_results if r.effective_ttft_ms is not None]
                latencies = [r.latency_ms for r in ok_results]
                success_rate = rate(len(ok_results), len(results))
                error_rate = rate(len(results) - len(ok_results), len(results))
                ttft_p50 = pct(ttfts, 50)
                ttft_p90 = pct(ttfts, 90)
                grade, diagnosis = grade_context(success_rate, ttft_p50, error_rate, standard)
                rows.append(
                    {
                        "level": level,
                        "context": standard["label"],
                        "chars": standard["chars"],
                        "estimated_tokens": estimate_tokens(context),
                        "reasoning": reasoning_mode,
                        "samples": len(results),
                        "success": len(ok_results),
                        "success_rate": success_rate,
                        "error_rate": error_rate,
                        "latency_p50_ms": pct(latencies, 50),
                        "latency_p90_ms": pct(latencies, 90),
                        "ttft_p50_ms": ttft_p50,
                        "ttft_p90_ms": ttft_p90,
                        "grade": grade,
                        "diagnosis": diagnosis,
                        "top_errors": self._top_errors(results),
                    }
                )
        return rows

    def test_token_metrics(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
        token_rows: List[Dict[str, Any]] = []
        for index in range(self.config.token_runs):
            max_tokens = self.config.token_max_tokens[index % len(self.config.token_max_tokens)]
            print(f"[token] run={index + 1}, max_tokens={max_tokens}", flush=True)
            result = self._simple_call(
                (
                    "请写一段结构化说明，主题是 AI 网关压测指标。"
                    "要求包含 TTFT、TPOT、吞吐、错误率、缓存命中、上下文长度，每点两句话。"
                ),
                max_tokens=max_tokens,
                timeout=self.config.long_timeout,
            )
            token_rows.append(
                {
                    "ok": result.ok,
                    "status": result.status,
                    "max_tokens": max_tokens,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "total_tokens": result.total_tokens,
                    "cached_tokens": result.cached_tokens,
                    "prompt_cache_hit_tokens": result.prompt_cache_hit_tokens,
                    "cache_hit_tokens": result.cache_hit_tokens,
                    "cache_hit_rate": (result.cache_hit_tokens / result.prompt_tokens * 100.0) if result.prompt_tokens else 0.0,
                    "reasoning_tokens": result.reasoning_tokens,
                    "latency_ms": result.latency_ms,
                    "ttft_ms": result.effective_ttft_ms,
                    "tpot_ms": result.tpot_ms,
                    "otps": result.otps,
                    "error": result.error,
                }
            )

        cache_rows = self._test_prompt_cache()
        hits = sum(1 for row in cache_rows if row["hit"])
        ok_count = sum(1 for row in cache_rows if row["ok"])
        max_cached = max([row["cache_hit_tokens"] for row in cache_rows] or [0])
        cache_summary = {
            "total": len(cache_rows),
            "ok": ok_count,
            "hits": hits,
            "hit_rate": rate(hits, len(cache_rows)),
            "max_cached_tokens": max_cached,
            "passed": hits > 0,
            "note": self._cache_note(hits, len(cache_rows), ok_count, max_cached),
        }
        return token_rows, cache_rows, cache_summary

    def _test_prompt_cache(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        stable_prefix = make_context(self.config.cache_prefix_chars)
        system_prompt = (
            "以下是固定不变的缓存前缀。压测时每次请求都保持这一段完全一致，用于观察上游是否返回缓存命中字段。\n\n"
            + stable_prefix
        )
        user_prompt = "请只回复 ok，不要添加其他内容。"
        extra_body = {"prompt_cache_key": self.config.prompt_cache_key}

        for index in range(self.config.cache_repeat):
            print(f"[cache] repeat={index + 1}/{self.config.cache_repeat}", flush=True)
            result = self.client.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=32,
                timeout=self.config.long_timeout,
                temperature=0.0,
                extra_body=extra_body,
            )
            rows.append(
                {
                    "index": index + 1,
                    "ok": result.ok,
                    "status": result.status,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "cached_tokens": result.cached_tokens,
                    "prompt_cache_hit_tokens": result.prompt_cache_hit_tokens,
                    "cache_hit_tokens": result.cache_hit_tokens,
                    "hit": result.cache_hit_tokens > 0,
                    "latency_ms": result.latency_ms,
                    "ttft_ms": result.effective_ttft_ms,
                    "error": result.error,
                }
            )
        return rows

    def test_consistency(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for case in CONSISTENCY_CASES:
            question = case["question"]
            print(f"[consistency] {question}", flush=True)
            results = [
                self._simple_call(question, max_tokens=80, stream=False, temperature=0.0)
                for _ in range(self.config.consistency_repeat)
            ]
            ok_results = [r for r in results if r.ok]
            contents = [r.content.strip() for r in ok_results if r.content.strip()]
            keyword_hits = sum(1 for text in contents if contains_any_keyword(text, case["keywords"]))
            similarity = average_similarity(contents)
            consistent = len(results) > 0 and rate(keyword_hits, len(results)) >= 80.0
            rows.append(
                {
                    "question": question,
                    "repeat": len(results),
                    "success": len(ok_results),
                    "success_rate": rate(len(ok_results), len(results)),
                    "keyword_hits": keyword_hits,
                    "keyword_hit_rate": rate(keyword_hits, len(results)),
                    "similarity": similarity,
                    "consistent": consistent,
                    "sample": short_err(contents[0] if contents else "", 120),
                    "errors": self._top_errors(results),
                }
            )
        return rows

    def _simple_call(
        self,
        question: str,
        *,
        max_tokens: int,
        stream: Optional[bool] = None,
        timeout: Optional[float] = None,
        temperature: float = 0.2,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> RequestResult:
        return self.client.chat(
            [{"role": "user", "content": question}],
            max_tokens=max_tokens,
            stream=stream,
            timeout=timeout,
            temperature=temperature,
            response_format=response_format,
        )

    def _first_tool_name(self, tool_calls: List[Dict[str, Any]]) -> str:
        for tool_call in tool_calls:
            function = tool_call.get("function") if isinstance(tool_call, dict) else None
            if isinstance(function, dict) and function.get("name"):
                return str(function["name"])
        return ""

    def _top_errors(self, results: List[RequestResult], limit: int = 3) -> List[str]:
        counts: Dict[str, int] = {}
        for result in results:
            if result.ok:
                continue
            key = short_err(result.error or result.raw_text or f"HTTP {result.status}", 140)
            counts[key] = counts.get(key, 0) + 1
        return [f"{text} × {count}" for text, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]]

    def _cache_note(self, hits: int, total: int, ok_count: int, max_cached: int) -> str:
        if hits > 0:
            return f"重复请求出现缓存命中，最大命中 {max_cached} tokens。"
        if ok_count == 0:
            return "缓存测试请求均未成功，无法判断上游缓存能力。"
        return (
            "重复完全相同长前缀请求均成功，但缓存命中字段仍为 0；"
            "需要对比官方直连端点，判断是上游未启用缓存，还是网关未透传 usage 字段。"
        )


def grade_context(
    success_rate: float,
    ttft_p50: float,
    error_rate: float,
    standard: Dict[str, Any],
) -> Tuple[str, str]:
    if success_rate >= 99.0 and error_rate <= 1.0 and ttft_p50 <= standard["a_ttft_p50_ms"]:
        return "A", "成功率、错误率和 TTFT 均达到 A 级。"
    if success_rate >= 98.0 and error_rate <= 3.0 and ttft_p50 <= standard["b_ttft_p50_ms"]:
        return "B", "达到 B 级准入线。"
    if success_rate >= 95.0 and error_rate <= 5.0:
        over = max(ttft_p50 - standard["b_ttft_p50_ms"], 0.0)
        if over > 0:
            ratio = over / standard["b_ttft_p50_ms"] * 100.0
            return "C", f"成功率达标，但 TTFT P50 超过 B 级阈值 {over:.1f}ms / {ratio:.1f}%。"
        return "C", "成功率接近准入线，但错误率或稳定性未达到 B 级。"
    if success_rate >= 90.0:
        return "D", "成功率偏低，需要排查超时、限流或上游错误。"
    return "E", "成功率明显不足，不建议进入准入判断。"


def contains_any_keyword(text: str, keywords: Sequence[str]) -> bool:
    lowered = text.lower().replace(" ", "")
    return any(str(keyword).lower().replace(" ", "") in lowered for keyword in keywords)


def average_similarity(contents: Sequence[str]) -> float:
    if len(contents) <= 1:
        return 1.0 if contents else 0.0
    base = contents[0]
    scores = [SequenceMatcher(None, base, item).ratio() for item in contents[1:]]
    return avg(scores)


class AdmissionAnalyzer:
    def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        api_rows = data.get("api_rows") or []
        conc_rows = data.get("concurrency_rows") or []
        context_rows = data.get("context_rows") or []
        consistency_rows = data.get("consistency_rows") or []
        cache_summary = data.get("cache_summary") or {}

        api_total = len(api_rows)
        api_ok = sum(1 for row in api_rows if row.get("ok"))
        api_rate = rate(api_ok, api_total) if api_total else 100.0
        api_pass = api_rate >= 80.0

        total_requests = sum(as_int(row.get("requests")) for row in conc_rows)
        total_success = sum(as_int(row.get("success")) for row in conc_rows)
        overall_success = rate(total_success, total_requests) if total_requests else 0.0
        max_error_rate = max([float(row.get("error_rate") or 0.0) for row in conc_rows] or [0.0])
        max_infra_error_rate = max([float(row.get("infra_error_rate") or 0.0) for row in conc_rows] or [0.0])
        competition = self._competition(conc_rows)
        concurrency_pass = total_requests > 0 and overall_success >= 98.0 and max_error_rate <= 5.0

        context_summary = self._context_summary(context_rows)
        context_total = len(context_summary)
        context_below_b = [row for row in context_summary if grade_order(row.get("grade", "E")) > grade_order("B")]
        context_pass = context_total == 0 or not context_below_b

        consistency_total = len(consistency_rows)
        consistency_ok = sum(1 for row in consistency_rows if row.get("consistent"))
        consistency_rate = rate(consistency_ok, consistency_total) if consistency_total else 100.0
        consistency_pass = consistency_rate >= 70.0

        checks = [
            {
                "name": "上下文长度分级(需达B级)",
                "status": "通过" if context_pass else "未通过",
                "kind": "ok" if context_pass else "bad",
                "detail": self._context_detail(context_summary, context_below_b),
            },
            {
                "name": "并发压测准入",
                "status": "通过" if concurrency_pass else "未通过",
                "kind": "ok" if concurrency_pass else "bad",
                "detail": (
                    f"整体成功率 {overall_success:.1f}%，最高错误率 {max_error_rate:.1f}%，"
                    f"基础设施错误率峰值 {max_infra_error_rate:.1f}%，资源竞争严重度 {competition['severity']}。"
                ),
            },
            {
                "name": "能力一致性(≥70%)",
                "status": "通过" if consistency_pass else "未通过",
                "kind": "ok" if consistency_pass else "bad",
                "detail": f"一致通过 {consistency_ok}/{consistency_total}，整体一致率 {consistency_rate:.1f}%。",
            },
            {
                "name": "API能力通过率(≥80%)",
                "status": "通过" if api_pass else "未通过",
                "kind": "ok" if api_pass else "bad",
                "detail": f"API 通过率 {api_rate:.1f}% ({api_ok}/{api_total})。",
            },
            {
                "name": "Token指标(缓存命中率)",
                "status": "信息",
                "kind": "info" if cache_summary else "muted",
                "detail": (
                    f"缓存命中次数={cache_summary.get('hits', 0)}/{cache_summary.get('total', 0)}，"
                    f"命中率 {float(cache_summary.get('hit_rate') or 0.0):.1f}%。"
                    if cache_summary
                    else "已跳过 Token/缓存测试。"
                ),
            },
        ]

        required_pass = context_pass and concurrency_pass and consistency_pass and api_pass
        return {
            "admission_ok": required_pass,
            "api_rate": api_rate,
            "overall_success": overall_success,
            "max_error_rate": max_error_rate,
            "peak_throughput": competition["peak_throughput"],
            "peak_concurrency": competition["peak_concurrency"],
            "competition": competition,
            "context_summary": context_summary,
            "consistency_rate": consistency_rate,
            "checks": checks,
        }

    def _competition(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {
                "severity": "未知",
                "peak_throughput": 0.0,
                "peak_concurrency": 0,
                "drop_after_peak_pct": 0.0,
                "latency_growth": 0.0,
                "detail": "未执行并发压测。",
            }

        peak = max(rows, key=lambda row: float(row.get("throughput_rps") or 0.0))
        peak_index = rows.index(peak)
        later = rows[peak_index + 1 :]
        peak_throughput = float(peak.get("throughput_rps") or 0.0)
        later_min = min([float(row.get("throughput_rps") or 0.0) for row in later] or [peak_throughput])
        drop_pct = ((peak_throughput - later_min) / peak_throughput * 100.0) if peak_throughput > 0 else 0.0

        first_latency = float(rows[0].get("avg_latency_ms") or 0.0)
        last_latency = float(rows[-1].get("avg_latency_ms") or 0.0)
        latency_growth = (last_latency / first_latency) if first_latency > 0 else 0.0

        if drop_pct >= 35.0 or latency_growth >= 3.0:
            severity = "高"
        elif drop_pct >= 15.0 or latency_growth >= 1.8:
            severity = "中"
        elif drop_pct >= 5.0 or latency_growth >= 1.2:
            severity = "低"
        else:
            severity = "无明显"

        detail = (
            f"吞吐峰值 {peak_throughput:.2f} req/s @ c={peak.get('concurrency')}；"
            f"峰值后最大回落 {drop_pct:.1f}%；平均延迟从 c={rows[0].get('concurrency')} "
            f"到 c={rows[-1].get('concurrency')} 增长 {latency_growth:.2f} 倍。"
        )
        return {
            "severity": severity,
            "peak_throughput": peak_throughput,
            "peak_concurrency": peak.get("concurrency"),
            "drop_after_peak_pct": drop_pct,
            "latency_growth": latency_growth,
            "detail": detail,
        }

    def _context_summary(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        summary: List[Dict[str, Any]] = []
        for level in ["short", "medium", "long"]:
            level_rows = [row for row in rows if row.get("level") == level]
            if not level_rows:
                continue
            best = min(level_rows, key=lambda row: grade_order(row.get("grade", "E")))
            summary.append(best)
        return summary

    def _context_detail(self, summary: List[Dict[str, Any]], below_b: List[Dict[str, Any]]) -> str:
        if not summary:
            return "已跳过上下文长度测试。"
        if not below_b:
            return f"共检查 {len(summary)} 个分级项，均达到 B 级。"
        details: List[str] = []
        max_over = 0.0
        max_ratio = 0.0
        for row in below_b:
            standard = CONTEXT_STANDARDS[row["level"]]
            over = max(float(row.get("ttft_p50_ms") or 0.0) - standard["b_ttft_p50_ms"], 0.0)
            ratio = over / standard["b_ttft_p50_ms"] * 100.0 if standard["b_ttft_p50_ms"] else 0.0
            max_over = max(max_over, over)
            max_ratio = max(max_ratio, ratio)
            details.append(f"{row['context']}={row.get('grade')}")
        return (
            f"共检查 {len(summary)} 个分级项，{len(below_b)} 项低于 B 级（{', '.join(details)}）；"
            f"最大 TTFT P50 超标 {max_over:.1f}ms / {max_ratio:.1f}%。"
        )


def grade_order(grade: str) -> int:
    return {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}.get(str(grade).upper(), 5)


def parse_csv_ints(text: str, name: str) -> List[int]:
    try:
        values = [int(item.strip()) for item in text.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} 必须是逗号分隔整数") from exc
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError(f"{name} 必须包含正整数")
    return values


def parse_context_levels(text: str) -> List[str]:
    values = [item.strip() for item in text.split(",") if item.strip()]
    unknown = [item for item in values if item not in CONTEXT_STANDARDS]
    if unknown:
        allowed = ",".join(CONTEXT_STANDARDS.keys())
        raise argparse.ArgumentTypeError(f"未知上下文档位 {unknown}，可选：{allowed}")
    return values


def parse_headers(values: List[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for item in values:
        if ":" not in item:
            raise argparse.ArgumentTypeError("--header 格式应为 key:value")
        key, value = item.split(":", 1)
        key = key.strip()
        if not key:
            raise argparse.ArgumentTypeError("--header 的 key 不能为空")
        headers[key] = value.strip()
    return headers


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI 网关 HTML 报告型压测脚本 v2")
    parser.add_argument("--url", default=os.getenv("AI_GATEWAY_URL", DEFAULT_URL), help="OpenAI 兼容地址，可填 https://host/v1 或完整 /v1/chat/completions")
    parser.add_argument("--api-key", default=os.getenv("AI_GATEWAY_API_KEY") or os.getenv("OPENAI_API_KEY") or "", help="API Key；推荐使用 AI_GATEWAY_API_KEY 环境变量")
    parser.add_argument("--model", default=os.getenv("AI_GATEWAY_MODEL", DEFAULT_MODEL), help="模型名称")
    parser.add_argument("--output", default="benchmark_report_v2.html", help="HTML 报告输出路径")
    parser.add_argument("--json-output", default="", help="原始 JSON 输出路径；默认与 HTML 同名 .json；填 none 可关闭")
    parser.add_argument("--prompt", default="请用三句话说明 AI 网关压测的核心关注点。", help="并发压测基础提示词")
    parser.add_argument("--max-tokens", type=int, default=256, help="并发压测 max_tokens")
    parser.add_argument("--concurrency-levels", type=lambda s: parse_csv_ints(s, "--concurrency-levels"), default=parse_csv_ints("1,5,10,20", "--concurrency-levels"), help="并发档位，逗号分隔")
    parser.add_argument("--requests-per-level", type=int, default=20, help="每个并发档位请求数")
    parser.add_argument("--timeout", type=float, default=90.0, help="普通请求超时秒")
    parser.add_argument("--long-timeout", type=float, default=240.0, help="长上下文/长输出请求超时秒")
    parser.add_argument("--retries", type=int, default=3, help="失败重试次数")
    parser.add_argument("--backoff", type=float, default=1.0, help="重试退避秒")
    parser.add_argument("--header", action="append", default=[], help="追加请求头，格式 key:value，可重复")
    parser.add_argument("--no-stream", action="store_true", help="关闭流式请求；关闭后 TTFT 近似为完整响应时间")
    parser.add_argument("--skip-api", action="store_true", help="跳过 API 接入能力测试")
    parser.add_argument("--skip-context", action="store_true", help="跳过上下文长度测试")
    parser.add_argument("--skip-token", action="store_true", help="跳过 Token 指标和缓存测试")
    parser.add_argument("--skip-consistency", action="store_true", help="跳过能力一致性测试")
    parser.add_argument("--context-levels", type=parse_context_levels, default=parse_context_levels("short,medium,long"), help="上下文档位：short,medium,long")
    parser.add_argument("--context-samples", type=int, default=3, help="每个上下文档位样本数")
    parser.add_argument("--context-max-tokens", type=int, default=512, help="上下文测试 max_tokens")
    parser.add_argument("--reasoning-scan", action="store_true", help="上下文测试同时扫描 reasoning_on/reasoning_off")
    parser.add_argument("--reasoning-effort", default="medium", help="reasoning_on 时传入的 reasoning_effort")
    parser.add_argument("--token-runs", type=int, default=5, help="Token 指标测试次数")
    parser.add_argument("--token-max-tokens", type=lambda s: parse_csv_ints(s, "--token-max-tokens"), default=parse_csv_ints("512,1024,1536,2048,3072", "--token-max-tokens"), help="Token 测试 max_tokens 列表")
    parser.add_argument("--cache-repeat", type=int, default=7, help="缓存命中重复请求次数")
    parser.add_argument("--cache-prefix-chars", type=int, default=8000, help="缓存测试重复长前缀字符数")
    parser.add_argument("--prompt-cache-key", default=os.getenv("AI_GATEWAY_PROMPT_CACHE_KEY", "ai-gateway-benchmark-v2"), help="传给上游的 prompt_cache_key")
    parser.add_argument("--consistency-repeat", type=int, default=6, help="一致性测试每个问题重复次数")
    parser.add_argument("--quick", action="store_true", help="快速模式：降低样本量，适合冒烟测试")
    parser.add_argument("--demo-report", action="store_true", help="不发请求，生成一份演示报告，用于验证 HTML 渲染")
    return parser


def apply_quick_mode(args: argparse.Namespace) -> None:
    if not args.quick:
        return
    args.concurrency_levels = [1, 2]
    args.requests_per_level = min(args.requests_per_level, 3)
    args.context_levels = ["short"]
    args.context_samples = 1
    args.token_runs = 1
    args.cache_repeat = min(args.cache_repeat, 2)
    args.consistency_repeat = min(args.consistency_repeat, 2)


def build_config(args: argparse.Namespace) -> BenchmarkConfig:
    output = Path(args.output).expanduser().resolve()
    if str(args.json_output).lower() == "none":
        json_output = None
    elif args.json_output:
        json_output = Path(args.json_output).expanduser().resolve()
    else:
        json_output = output.with_suffix(".json")

    return BenchmarkConfig(
        endpoint=normalize_chat_url(args.url),
        api_key=args.api_key,
        model=args.model,
        output=output,
        json_output=json_output,
        stream=not args.no_stream,
        prompt=args.prompt,
        max_tokens=args.max_tokens,
        concurrency_levels=args.concurrency_levels,
        requests_per_level=args.requests_per_level,
        timeout=args.timeout,
        long_timeout=args.long_timeout,
        retries=args.retries,
        backoff=args.backoff,
        extra_headers=parse_headers(args.header),
        skip_api=args.skip_api,
        skip_context=args.skip_context,
        skip_token=args.skip_token,
        skip_consistency=args.skip_consistency,
        context_levels=args.context_levels,
        context_samples=args.context_samples,
        context_max_tokens=args.context_max_tokens,
        reasoning_scan=args.reasoning_scan,
        reasoning_effort=args.reasoning_effort,
        token_runs=args.token_runs,
        token_max_tokens=args.token_max_tokens,
        cache_repeat=args.cache_repeat,
        cache_prefix_chars=args.cache_prefix_chars,
        prompt_cache_key=args.prompt_cache_key,
        consistency_repeat=args.consistency_repeat,
        started_at=now_str(),
    )


def write_json_sidecar(data: Dict[str, Any], path: Optional[Path]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_demo_data(config: BenchmarkConfig) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "meta": config.public_meta(),
        "api_rows": [
            {"name": "基础对话", "ok": True, "status": 200, "latency_ms": 980, "ttft_ms": 420, "detail": "演示数据：基础回复正常。"},
            {"name": "流式输出", "ok": True, "status": 200, "latency_ms": 1220, "ttft_ms": 360, "detail": "演示数据：chunk=28。"},
            {"name": "错误处理", "ok": True, "status": 400, "latency_ms": 90, "ttft_ms": 90, "detail": "演示数据：非法 role 返回 400。"},
        ],
        "concurrency_rows": [
            {"concurrency": 1, "requests": 3, "success": 3, "success_rate": 100.0, "error_rate": 0.0, "infra_error_rate": 0.0, "business_error_rate": 0.0, "avg_latency_ms": 1100, "p50_ms": 1080, "p90_ms": 1300, "p99_ms": 1400, "avg_ttft_ms": 520, "ttft_p50_ms": 510, "ttft_p90_ms": 650, "ttft_p99_ms": 700, "avg_tpot_ms": 8.2, "throughput_rps": 0.9, "wall_seconds": 3.3, "completion_tokens": 400, "total_tokens": 520, "token_per_sec_wall": 121.2, "errors": []},
            {"concurrency": 5, "requests": 10, "success": 10, "success_rate": 100.0, "error_rate": 0.0, "infra_error_rate": 0.0, "business_error_rate": 0.0, "avg_latency_ms": 1480, "p50_ms": 1420, "p90_ms": 1900, "p99_ms": 2200, "avg_ttft_ms": 680, "ttft_p50_ms": 650, "ttft_p90_ms": 880, "ttft_p99_ms": 980, "avg_tpot_ms": 9.1, "throughput_rps": 3.1, "wall_seconds": 3.2, "completion_tokens": 1180, "total_tokens": 1520, "token_per_sec_wall": 368.7, "errors": []},
        ],
        "context_rows": [
            {"level": "short", "context": "短上下文", "chars": 2000, "estimated_tokens": 480, "reasoning": "default", "samples": 1, "success": 1, "success_rate": 100.0, "error_rate": 0.0, "latency_p50_ms": 1500, "latency_p90_ms": 1500, "ttft_p50_ms": 780, "ttft_p90_ms": 780, "grade": "A", "diagnosis": "演示数据：达到 A 级。", "top_errors": []},
            {"level": "medium", "context": "中上下文", "chars": 16000, "estimated_tokens": 3800, "reasoning": "default", "samples": 1, "success": 1, "success_rate": 100.0, "error_rate": 0.0, "latency_p50_ms": 2800, "latency_p90_ms": 2800, "ttft_p50_ms": 2100, "ttft_p90_ms": 2100, "grade": "B", "diagnosis": "演示数据：达到 B 级。", "top_errors": []},
        ],
        "token_rows": [
            {"ok": True, "status": 200, "max_tokens": 1024, "prompt_tokens": 260, "completion_tokens": 800, "total_tokens": 1060, "cached_tokens": 0, "prompt_cache_hit_tokens": 0, "cache_hit_tokens": 0, "cache_hit_rate": 0.0, "reasoning_tokens": 120, "latency_ms": 7200, "ttft_ms": 600, "tpot_ms": 8.2, "otps": 111.1, "error": ""},
        ],
        "cache_rows": [
            {"index": 1, "ok": True, "status": 200, "prompt_tokens": 5600, "completion_tokens": 2, "cached_tokens": 0, "prompt_cache_hit_tokens": 0, "cache_hit_tokens": 0, "hit": False, "latency_ms": 980, "ttft_ms": 600, "error": ""},
            {"index": 2, "ok": True, "status": 200, "prompt_tokens": 5600, "completion_tokens": 2, "cached_tokens": 5376, "prompt_cache_hit_tokens": 5376, "cache_hit_tokens": 5376, "hit": True, "latency_ms": 720, "ttft_ms": 390, "error": ""},
        ],
        "cache_summary": {"total": 2, "ok": 2, "hits": 1, "hit_rate": 50.0, "max_cached_tokens": 5376, "passed": True, "note": "演示数据：第二次请求出现缓存命中。"},
        "consistency_rows": [
            {"question": "1 + 1 等于几？请只回答数字。", "repeat": 2, "success": 2, "success_rate": 100.0, "keyword_hits": 2, "keyword_hit_rate": 100.0, "similarity": 1.0, "consistent": True, "sample": "2", "errors": []},
        ],
    }
    data["meta"]["finished_at"] = now_str()
    data["meta"]["duration_seconds"] = 0
    data["admission"] = AdmissionAnalyzer().analyze(data)
    return data


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    apply_quick_mode(args)
    config = build_config(args)

    if args.demo_report:
        data = build_demo_data(config)
        HtmlReportRenderer().write(data, config.output)
        write_json_sidecar(data, config.json_output)
        print(f"demo html report: {config.output}")
        if config.json_output:
            print(f"demo json data: {config.json_output}")
        return 0

    if not config.api_key:
        parser.error("缺少 API Key：请设置 AI_GATEWAY_API_KEY/OPENAI_API_KEY，或传入 --api-key")

    print(f"[start] endpoint={config.endpoint}, model={config.model}, output={config.output}", flush=True)
    runner = BenchmarkRunner(AiGatewayClient(config), config)
    data = runner.run()
    HtmlReportRenderer().write(data, config.output)
    write_json_sidecar(data, config.json_output)

    print(f"[done] html report: {config.output}")
    if config.json_output:
        print(f"[done] json data: {config.json_output}")
    return 0 if (data.get("admission") or {}).get("admission_ok") else 2


if __name__ == "__main__":
    sys.exit(main())
