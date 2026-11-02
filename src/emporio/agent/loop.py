"""Laço de function calling do agente.

Padrão: ReAct com function calling nativo. O modelo decide se precisa de dado,
pede uma ou mais ferramentas, lê os resultados e só então responde. O laço
repete até ele responder sem pedir ferramenta, com teto de iterações.

Por que um laço próprio em vez de LangChain/LlamaIndex: são ~150 linhas que
ficam completamente visíveis e sob controle — incluindo coerção de argumentos,
poda de histórico e o que exatamente vai para o log. Com um framework, o prompt
real passa a ser decidido por ele, a versão dita o comportamento e depurar
"por que o modelo chamou esta ferramenta" vira arqueologia. Para 11 ferramentas
e um fluxo linear, o framework custaria mais do que entrega. O README registra
em que ponto a conta viraria.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from emporio.agent.prompts import build_system_prompt
from emporio.config import settings
from emporio.llm.base import LLMClient, LLMError, Message, ToolCall, Usage
from emporio.tools.common import ToolError
from emporio.tools.registry import ToolSpec, get_tool, tool_schemas

# Teto de mensagens mantidas no contexto. Resultado de ferramenta é verboso;
# sem poda, uma conversa longa estoura o contexto e encarece cada turno.
HISTORY_LIMIT = 24

# Tamanho máximo do JSON de resultado enviado ao modelo. Acima disso o payload
# é truncado com aviso — vale mais manter a conversa viva do que entregar a
# última linha de uma lista.
MAX_TOOL_RESULT_CHARS = 6000


@dataclass
class ToolInvocation:
    """Registro de uma execução de ferramenta — alimenta o trace e o log."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    duration_ms: int
    ok: bool = True

    def summary(self) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in self.arguments.items() if v not in (None, ""))
        return f"{self.name}({args})"


@dataclass
class AgentTurn:
    """Resultado de um turno completo de conversa."""

    reply: str
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    iterations: int = 0
    duration_ms: int = 0
    truncated: bool = False
    # Mensagens produzidas neste turno, em ordem. A camada de persistência usa
    # esta lista em vez de comparar tamanhos de histórico — a poda de contexto
    # remove mensagens do início, o que invalidaria qualquer cálculo por índice.
    messages_added: list[Message] = field(default_factory=list)


def _coerce_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """Alinha os argumentos recebidos ao schema da ferramenta.

    Necessário na prática, não por preciosismo: modelos menores mandam
    `"preco_max": "1000"` em vez de número, inventam parâmetro que não existe,
    ou passam `"null"` como string. Em vez de estourar um TypeError, ajustamos
    o que é ajustável e descartamos o resto.
    """
    properties: dict[str, dict] = (spec.parameters.get("properties") or {})
    cleaned: dict[str, Any] = {}

    for key, value in (arguments or {}).items():
        if key not in properties:
            continue  # parâmetro alucinado
        if value is None:
            continue
        expected = properties[key].get("type")
        if isinstance(value, str):
            text = value.strip()
            if text == "" or text.lower() in {"null", "none", "undefined"}:
                continue
            if expected == "integer":
                try:
                    cleaned[key] = int(float(text.replace(",", ".")))
                    continue
                except ValueError:
                    continue
            if expected == "number":
                try:
                    cleaned[key] = float(text.replace(".", "").replace(",", "."))
                    continue
                except ValueError:
                    try:
                        cleaned[key] = float(text)
                        continue
                    except ValueError:
                        continue
            if expected == "boolean":
                cleaned[key] = text.lower() in {"true", "1", "yes", "sim"}
                continue
            cleaned[key] = value
            continue
        if expected == "integer" and isinstance(value, float):
            cleaned[key] = int(value)
            continue
        cleaned[key] = value

    return cleaned


def _trim_history(messages: list[Message]) -> list[Message]:
    """Mantém o histórico dentro do limite sem quebrar o formato.

    Cuidado necessário: todo provedor rejeita um histórico que começa com
    resultado de ferramenta solto, ou que tem resultado sem a chamada
    correspondente. A poda avança até encontrar uma mensagem de usuário.
    """
    if len(messages) <= HISTORY_LIMIT:
        return messages
    window = messages[-HISTORY_LIMIT:]
    while window and window[0].role != "user":
        window.pop(0)
    return window or messages[-1:]


class Agent:
    """Orquestra a conversa entre o modelo e as ferramentas."""

    def __init__(
        self,
        client: LLMClient,
        *,
        history: list[Message] | None = None,
        max_iterations: int | None = None,
    ) -> None:
        self.client = client
        self.history: list[Message] = history or []
        self.max_iterations = max_iterations or settings.max_tool_iterations
        self._schemas = tool_schemas()

    # ------------------------------------------------------------------
    def send(self, user_message: str) -> AgentTurn:
        """Processa uma mensagem do cliente e devolve a resposta do agente."""
        started = time.perf_counter()
        added: list[Message] = []

        def record(message: Message) -> None:
            """Anexa ao histórico e ao registro do turno de uma vez só."""
            self.history.append(message)
            added.append(message)

        record(Message(role="user", content=user_message))

        system = build_system_prompt()
        invocations: list[ToolInvocation] = []
        usage = Usage()
        reply = ""
        iterations = 0
        truncated = False

        for iteration in range(1, self.max_iterations + 1):
            iterations = iteration
            self.history = _trim_history(self.history)
            response = self.client.complete(system, self.history, self._schemas)
            usage = usage + response.usage

            if not response.wants_tools:
                reply = response.text
                record(Message(role="assistant", content=reply))
                break

            # O modelo pediu ferramentas: registra a intenção e executa.
            record(
                Message(
                    role="assistant",
                    content=response.text,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                invocation = self._execute(call)
                invocations.append(invocation)
                record(
                    Message(
                        role="tool",
                        content=self._serialize_result(invocation.result),
                        tool_call_id=call.id,
                        tool_name=call.name,
                    )
                )
        else:
            # Teto de iterações atingido. Em vez de devolver vazio, pedimos uma
            # resposta final SEM ferramentas, com o que já foi coletado.
            truncated = True
            reply = self._force_final_answer(system)
            if reply:
                added.append(self.history[-1])

        if not reply:
            reply = (
                "Desculpa, me perdi aqui. Pode repetir o que você precisa? "
                "Se for sobre um pedido, me passa o número que eu verifico."
            )

        turn = AgentTurn(
            reply=reply,
            tool_invocations=invocations,
            usage=usage,
            iterations=iterations,
            duration_ms=int((time.perf_counter() - started) * 1000),
            truncated=truncated,
            messages_added=added,
        )
        self._log_turn(user_message, turn)
        return turn

    # ------------------------------------------------------------------
    def _execute(self, call: ToolCall) -> ToolInvocation:
        """Executa uma ferramenta, convertendo qualquer falha em dado útil.

        Erro de ferramenta volta ao modelo como conteúdo, não como exceção:
        assim ele pode pedir o dado que falta ao cliente em vez de o
        atendimento morrer.
        """
        started = time.perf_counter()
        spec = get_tool(call.name)
        if spec is None:
            return ToolInvocation(
                name=call.name,
                arguments=call.arguments,
                result={
                    "erro": f"Ferramenta '{call.name}' não existe.",
                    "ferramentas_disponiveis": [s["name"] for s in self._schemas],
                },
                duration_ms=0,
                ok=False,
            )

        arguments = _coerce_arguments(spec, call.arguments)
        try:
            result = spec.handler(**arguments)
            ok = True
        except ToolError as exc:
            result = exc.to_dict()
            ok = False
        except TypeError as exc:
            # Argumento obrigatório ausente — devolvemos o schema para o
            # modelo corrigir a chamada.
            result = {
                "erro": f"Chamada inválida: {exc}",
                "parametros_esperados": spec.parameters,
            }
            ok = False
        except Exception as exc:  # pragma: no cover - rede de segurança
            result = {
                "erro": (
                    "Falha interna ao consultar o sistema. Informe ao cliente "
                    "que vai verificar com a equipe."
                ),
                "detalhe_tecnico": f"{type(exc).__name__}: {exc}",
            }
            ok = False

        return ToolInvocation(
            name=call.name,
            arguments=arguments,
            result=result if isinstance(result, dict) else {"resultado": result},
            duration_ms=int((time.perf_counter() - started) * 1000),
            ok=ok,
        )

    @staticmethod
    def _serialize_result(result: dict) -> str:
        payload = json.dumps(result, ensure_ascii=False, default=str)
        if len(payload) > MAX_TOOL_RESULT_CHARS:
            return (
                payload[:MAX_TOOL_RESULT_CHARS]
                + ' ... [resultado truncado; se precisar de mais itens, '
                'refine a busca com filtros mais específicos]'
            )
        return payload

    def _force_final_answer(self, system: str) -> str:
        """Última chamada sem ferramentas, para fechar o turno com algo útil."""
        nudge = Message(
            role="user",
            content=(
                "[instrução interna] Você já consultou o sistema o suficiente. "
                "Responda agora ao cliente com o que apurou, em linguagem "
                "natural e sem novas consultas. Se faltou algum dado, diga que "
                "vai confirmar com a equipe."
            ),
        )
        try:
            response = self.client.complete(system, [*self.history, nudge], None)
        except LLMError:
            return (
                "Consegui levantar parte da informação, mas não fechei a "
                "consulta aqui. Deixa eu confirmar com a equipe e já te "
                "retorno, pode ser?"
            )
        text = response.text
        if text:
            self.history.append(Message(role="assistant", content=text))
        return text

    # ------------------------------------------------------------------
    def _log_turn(self, user_message: str, turn: AgentTurn) -> None:
        """Log estruturado em JSONL — um objeto por turno.

        Formato pensado para ser `jq`-ável: permite medir distribuição de
        ferramentas, latência e turnos que bateram o teto de iterações, que são
        os sinais que indicam onde o prompt precisa de ajuste.
        """
        if not settings.log_turns:
            return
        settings.ensure_dirs()
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "provedor": getattr(self.client, "provider", "?"),
            "modelo": getattr(self.client, "model", "?"),
            "mensagem_do_cliente": user_message,
            "resposta": turn.reply,
            "iteracoes": turn.iterations,
            "teto_atingido": turn.truncated,
            "duracao_ms": turn.duration_ms,
            "tokens": {
                "prompt": turn.usage.prompt_tokens,
                "resposta": turn.usage.completion_tokens,
            },
            "ferramentas": [
                {
                    "nome": inv.name,
                    "argumentos": inv.arguments,
                    "ok": inv.ok,
                    "duracao_ms": inv.duration_ms,
                    # Resultado de ferramenta sensível não vai para o log:
                    # não faz sentido criar um segundo lugar com dado pessoal.
                    "resultado": "[omitido: dado pessoal]"
                    if (get_tool(inv.name) or ToolSpec("", "", {}, lambda: {})).sensitive
                    else inv.result,
                }
                for inv in turn.tool_invocations
            ],
        }
        path = settings.logs_dir / "turns.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
