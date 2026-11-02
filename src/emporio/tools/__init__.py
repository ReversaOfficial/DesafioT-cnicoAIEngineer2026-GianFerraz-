"""Ferramentas determinísticas disponibilizadas ao agente.

Nenhum módulo desta camada chama LLM. Toda ferramenta é função Python pura
sobre o SQLite da operação ou o índice de políticas, com retorno serializável
— o que permite testá-las sem rede, sem chave de API e sem variância.
"""

from emporio.tools.common import ToolError
from emporio.tools.registry import TOOLS, TOOLS_BY_NAME, ToolSpec, get_tool, tool_schemas

__all__ = [
    "TOOLS",
    "TOOLS_BY_NAME",
    "ToolError",
    "ToolSpec",
    "get_tool",
    "tool_schemas",
]
