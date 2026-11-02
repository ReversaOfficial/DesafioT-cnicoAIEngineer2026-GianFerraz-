"""Harness de avaliação do agente.

Sem isto, "o agente está bom" é opinião. O conjunto em `eval/cases.json` fixa
comportamentos observáveis e mede três coisas distintas, porque elas falham por
motivos diferentes:

1. **Disciplina de consulta** — ele chamou ferramenta antes de afirmar? É a
   regra 1 do prompt e a exigência do §7.1 do manual.
2. **Roteamento** — chamou a ferramenta *certa*? Erro aqui aparece como
   resposta plausível e errada.
3. **Conteúdo** — a resposta contém o dado correto e, principalmente, **não
   contém** o incorreto. Metade dos casos é negativa (promoção vencida, preço
   inventado, dado pessoal sem verificação), porque é aí que um agente de
   atendimento causa prejuízo de verdade.

As verificações de conteúdo casam por número e palavra-chave, nunca por frase
inteira: o agente tem liberdade de redação, não de fato.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emporio.agent.service import AgentService
from emporio.config import settings
from emporio.text_utils import normalize

CASES_PATH = settings.root_dir / "eval" / "cases.json"


@dataclass
class CaseResult:
    case_id: str
    eixo: str
    pergunta: str
    resposta: str
    ferramentas_usadas: list[str] = field(default_factory=list)
    falhas: list[str] = field(default_factory=list)
    duracao_ms: int = 0
    erro: str = ""

    @property
    def executado(self) -> bool:
        """Falso quando o provedor não respondeu (cota, timeout, indisponível).

        Essa distinção é o que separa medir o agente de medir a infraestrutura.
        Um caso que não rodou não é um caso reprovado, e misturar os dois
        produziria um score que diz mais sobre a cota gratuita do dia do que
        sobre o comportamento do agente.
        """
        return not self.erro

    @property
    def passou(self) -> bool:
        return self.executado and not self.falhas


@dataclass
class EvalReport:
    resultados: list[CaseResult] = field(default_factory=list)
    provedor: str = ""
    modelo: str = ""
    interrompido_por: str = ""

    @property
    def total(self) -> int:
        return len(self.resultados)

    @property
    def executados(self) -> int:
        return sum(1 for r in self.resultados if r.executado)

    @property
    def nao_executados(self) -> list[CaseResult]:
        return [r for r in self.resultados if not r.executado]

    @property
    def aprovados(self) -> int:
        return sum(1 for r in self.resultados if r.passou)

    @property
    def score(self) -> float:
        """Calculado sobre os casos que efetivamente rodaram."""
        return self.aprovados / self.executados if self.executados else 0.0

    def por_eixo(self) -> dict[str, tuple[int, int]]:
        agregado: dict[str, list[int]] = {}
        for resultado in self.resultados:
            if not resultado.executado:
                continue
            bucket = agregado.setdefault(resultado.eixo, [0, 0])
            bucket[1] += 1
            if resultado.passou:
                bucket[0] += 1
        return {eixo: (ok, total) for eixo, (ok, total) in agregado.items()}

    def to_dict(self) -> dict:
        return {
            "executado_em": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "provedor": self.provedor,
            "modelo": self.modelo,
            "score": round(self.score, 3),
            "aprovados": self.aprovados,
            "executados": self.executados,
            "total_de_casos": self.total,
            "nao_executados": [r.case_id for r in self.nao_executados],
            "interrompido_por": self.interrompido_por,
            "por_eixo": {k: f"{ok}/{t}" for k, (ok, t) in self.por_eixo().items()},
            "casos": [
                {
                    "id": r.case_id,
                    "eixo": r.eixo,
                    "executado": r.executado,
                    "passou": r.passou,
                    "falhas": r.falhas,
                    "erro": r.erro,
                    "ferramentas": r.ferramentas_usadas,
                    "duracao_ms": r.duracao_ms,
                    "pergunta": r.pergunta,
                    "resposta": r.resposta,
                }
                for r in self.resultados
            ],
        }


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    source = path or CASES_PATH
    payload = json.loads(source.read_text(encoding="utf-8"))
    return payload["casos"]


def _check(case: dict, resposta: str, ferramentas: list[str]) -> list[str]:
    """Aplica as verificações de um caso e devolve as falhas encontradas."""
    falhas: list[str] = []
    resposta_norm = normalize(resposta)

    if case.get("exige_consulta") and not ferramentas:
        falhas.append("respondeu sem consultar nenhuma ferramenta")

    minimo = case.get("minimo_de_ferramentas")
    if minimo and len(ferramentas) < minimo:
        falhas.append(f"usou {len(ferramentas)} ferramenta(s), esperado ao menos {minimo}")

    for esperada in case.get("ferramentas_esperadas", []):
        if esperada not in ferramentas:
            falhas.append(f"não chamou `{esperada}`")

    alternativas = case.get("ferramentas_esperadas_algum", [])
    if alternativas and not any(f in ferramentas for f in alternativas):
        falhas.append("não chamou nenhuma de: " + ", ".join(f"`{f}`" for f in alternativas))

    for proibida in case.get("ferramentas_proibidas", []):
        if proibida in ferramentas:
            falhas.append(f"chamou `{proibida}`, que não deveria")

    exigidos = case.get("deve_conter_algum", [])
    if exigidos and not any(normalize(t) in resposta_norm for t in exigidos):
        falhas.append("resposta não menciona nenhum de: " + ", ".join(repr(t) for t in exigidos))

    for proibido in case.get("nao_deve_conter", []):
        if normalize(proibido) in resposta_norm:
            falhas.append(f"resposta contém {proibido!r}, que não deveria")

    return falhas


def _e_erro_de_cota(mensagem: str) -> bool:
    texto = mensagem.lower()
    return "429" in texto or "quota" in texto or "limite de requisi" in texto


def run_eval(
    *,
    cases: list[dict] | None = None,
    filtro: str = "",
    service: AgentService | None = None,
    on_result=None,
    pausa_entre_casos: float = 0.0,
) -> EvalReport:
    """Roda o conjunto de avaliação. Uma sessão limpa por caso.

    Sessão nova a cada caso é deliberado: queremos medir a resposta à pergunta,
    não o efeito de uma conversa anterior ter deixado o dado no contexto.

    Ao esgotar a cota do provedor a execução **para**, em vez de insistir nos
    casos restantes: cada tentativa gastaria três retentativas para produzir o
    mesmo erro, e um relatório cheio de falhas de infraestrutura esconde o que
    realmente foi medido.
    """
    selecionados = cases if cases is not None else load_cases()
    if filtro:
        selecionados = [
            c for c in selecionados if filtro in c["id"] or filtro == c.get("eixo")
        ]

    agente = service or AgentService(persist=False)
    report = EvalReport(
        provedor=getattr(agente.client, "provider", "?"),
        modelo=getattr(agente.client, "model", "?"),
    )

    original_now = os.environ.get("EMPORIO_NOW")
    cota_esgotada = False
    try:
        for indice, case in enumerate(selecionados):
            # Alguns casos só fazem sentido numa data específica (prazo de
            # devolução aberto, loja fechada no domingo).
            if case.get("data_de_referencia"):
                os.environ["EMPORIO_NOW"] = case["data_de_referencia"]
            elif original_now is not None:
                os.environ["EMPORIO_NOW"] = original_now
            else:
                os.environ.pop("EMPORIO_NOW", None)

            resultado = CaseResult(
                case_id=case["id"],
                eixo=case.get("eixo", "geral"),
                pergunta=case["pergunta"],
                resposta="",
            )
            if cota_esgotada:
                resultado.erro = "não executado: cota do provedor esgotada"
            else:
                try:
                    session_id = agente.open_session(None, channel="eval")
                    turn = agente.send(session_id, case["pergunta"])
                    resultado.resposta = turn.reply
                    resultado.ferramentas_usadas = [i.name for i in turn.tool_invocations]
                    resultado.duracao_ms = turn.duration_ms
                    resultado.falhas = _check(
                        case, turn.reply, resultado.ferramentas_usadas
                    )
                except Exception as exc:  # pragma: no cover - falha de provedor
                    resultado.erro = f"{type(exc).__name__}: {exc}"
                    if _e_erro_de_cota(str(exc)):
                        cota_esgotada = True
                        report.interrompido_por = (
                            "Cota do provedor esgotada. Os casos seguintes não "
                            "foram executados e não contam como reprovados."
                        )

            report.resultados.append(resultado)
            if on_result:
                on_result(resultado)

            if pausa_entre_casos and not cota_esgotada and indice + 1 < len(selecionados):
                time.sleep(pausa_entre_casos)
    finally:
        if original_now is None:
            os.environ.pop("EMPORIO_NOW", None)
        else:
            os.environ["EMPORIO_NOW"] = original_now

    return report


def save_report(report: EvalReport, path: Path | None = None) -> Path:
    destino = path or (settings.var_dir / "eval_report.json")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destino


def write_markdown(report: EvalReport, path: Path | None = None) -> Path:
    """Gera docs/EVALUATION.md — o resultado da última execução, versionado."""
    destino = path or (settings.root_dir / "docs" / "EVALUATION.md")
    destino.parent.mkdir(parents=True, exist_ok=True)

    linhas = [
        "# Resultado da avaliação",
        "",
        "> Arquivo **gerado** por `emporio eval --report`. "
        "O conjunto de casos está em [`eval/cases.json`](../eval/cases.json).",
        "",
        f"- Executado em: {time.strftime('%d/%m/%Y %H:%M')}",
        f"- Provedor / modelo: `{report.provedor}` / `{report.modelo}`",
        f"- **Score: {report.aprovados}/{report.executados} "
        f"({report.score * 100:.0f}%)** "
        f"sobre os casos efetivamente executados",
    ]
    if report.nao_executados:
        linhas += [
            f"- {len(report.nao_executados)} de {report.total} casos **não "
            "foram executados** e não entram no score: "
            + ", ".join(f"`{r.case_id}`" for r in report.nao_executados),
        ]
        if report.interrompido_por:
            linhas.append(f"- Motivo: {report.interrompido_por}")
    linhas += [
        "",
        "## Por eixo",
        "",
        "| Eixo | Aprovados |",
        "| --- | --- |",
    ]
    for eixo, (ok, total) in sorted(report.por_eixo().items()):
        linhas.append(f"| {eixo} | {ok}/{total} |")

    linhas += ["", "## Casos", "", "| Caso | Resultado | Ferramentas consultadas |", "| --- | --- | --- |"]
    for r in report.resultados:
        marca = "⏭️ não executado" if not r.executado else ("✅" if r.passou else "❌")
        ferramentas = ", ".join(f"`{f}`" for f in r.ferramentas_usadas) or "—"
        linhas.append(f"| `{r.case_id}` | {marca} | {ferramentas} |")

    reprovados = [r for r in report.resultados if r.executado and not r.passou]
    if reprovados:
        linhas += ["", "## Detalhe dos casos reprovados", ""]
        for r in reprovados:
            linhas += [
                f"### `{r.case_id}`",
                "",
                f"**Pergunta:** {r.pergunta}",
                "",
                f"**Resposta:** {r.resposta[:600]}",
                "",
                "**Falhas:**",
                "",
            ]
            linhas += [f"- {f}" for f in r.falhas]
            linhas.append("")

    if report.nao_executados:
        linhas += [
            "## Casos não executados",
            "",
            "Estes casos não chegaram a rodar — é falha de infraestrutura, não "
            "de comportamento do agente, e por isso ficam fora do score:",
            "",
        ]
        linhas += [f"- `{r.case_id}` — {r.erro}" for r in report.nao_executados]
        linhas.append("")

    destino.write_text("\n".join(linhas), encoding="utf-8")
    return destino
