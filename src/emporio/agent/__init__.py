"""O agente: persona, prompt e laço de function calling."""

from emporio.agent.loop import Agent, AgentTurn, ToolInvocation
from emporio.agent.prompts import AGENT_NAME, SAUDACAO_INICIAL, build_system_prompt
from emporio.agent.service import AgentService

__all__ = [
    "AGENT_NAME",
    "SAUDACAO_INICIAL",
    "Agent",
    "AgentService",
    "AgentTurn",
    "ToolInvocation",
    "build_system_prompt",
]
