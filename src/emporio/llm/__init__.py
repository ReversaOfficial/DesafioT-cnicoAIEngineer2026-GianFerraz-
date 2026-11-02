"""Adaptadores de provedores de LLM, atrás de uma interface única."""

from emporio.llm.base import (
    LLMClient,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    Usage,
)
from emporio.llm.factory import build_client, describe_configuration

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "Message",
    "ToolCall",
    "Usage",
    "build_client",
    "describe_configuration",
]
