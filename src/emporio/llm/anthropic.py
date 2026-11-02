"""Adaptador da API Anthropic (Messages API)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from emporio.llm.base import (
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    Usage,
    friendly_http_error,
    parse_arguments,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
MAX_TOKENS = 4096
MAX_RETRIES = 3


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "",
        temperature: float = 0.3,
        timeout: float = 90.0,
    ) -> None:
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY não definida. Preencha o arquivo .env.")
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.temperature = temperature
        self.timeout = timeout

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        return [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]

    @staticmethod
    def _convert_messages(messages: list[Message]) -> list[dict]:
        """Converte para blocos de conteúdo da Messages API.

        Particularidade: resultados de ferramenta vão em mensagem de role
        'user', e vários resultados consecutivos precisam ser agrupados em uma
        única mensagem — mandar um por mensagem é erro de formato.
        """
        out: list[dict] = []
        pending_results: list[dict] = []

        def flush() -> None:
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for message in messages:
            if message.role == "tool":
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
                continue
            flush()
            if message.role == "user":
                out.append({"role": "user", "content": message.content})
            elif message.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
        flush()
        return out

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "temperature": self.temperature,
            "messages": self._convert_messages(messages),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._convert_tools(tools)

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        data = self._post(f"{self.base_url}/v1/messages", payload, headers)
        return self._parse(data)

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
            except httpx.HTTPError as exc:
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429 or response.status_code >= 500:
                last_error = friendly_http_error(
                    self.provider, response.status_code, response.text
                )
                time.sleep(2.0 * (attempt + 1))
                continue
            raise friendly_http_error(self.provider, response.status_code, response.text)
        raise LLMError(f"anthropic: falha após {MAX_RETRIES} tentativas. {last_error}")

    @staticmethod
    def _parse(data: dict) -> LLMResponse:
        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=parse_arguments(block.get("input")),
                    )
                )
        usage = data.get("usage") or {}
        return LLMResponse(
            text="".join(texts).strip(),
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
            ),
            finish_reason=data.get("stop_reason", ""),
        )
