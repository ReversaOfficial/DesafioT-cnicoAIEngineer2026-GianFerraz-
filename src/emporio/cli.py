"""Interface de terminal.

A CLI é a interface principal do projeto, e tem uma função além de conversar:
com `--trace` ela mostra cada ferramenta chamada, com argumentos e tempo. Para
quem avalia o agente, ver *por que* ele respondeu aquilo vale mais do que a
resposta — é a diferença entre acreditar e verificar.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from emporio import __version__
from emporio.agent.prompts import AGENT_NAME, SAUDACAO_INICIAL, build_system_prompt
from emporio.agent.service import AgentService
from emporio.config import settings
from emporio.llm.base import LLMError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Agente de atendimento da Empório da Música.",
)
console = Console()

CLIENTE_COR = "bold cyan"
AGENTE_COR = "bold green"
TRACE_COR = "dim yellow"

AJUDA_INTERNA = """\
Comandos da sessão:
  /sair, /quit     encerra
  /trace           liga e desliga o detalhamento das consultas
  /reset           começa uma conversa nova
  /sessao          mostra o id da sessão atual
  /historico       mostra a conversa até aqui
  /config          mostra provedor, modelo e configuração
  /prompt          mostra o prompt do sistema em uso
  /ferramentas     lista as ferramentas disponíveis ao agente
  /ajuda           mostra esta lista\
"""


# ---------------------------------------------------------------------------
# Helpers de apresentação
# ---------------------------------------------------------------------------


def _print_trace(turn) -> None:
    if not turn.tool_invocations:
        console.print(
            Text("  (respondeu sem consultar o sistema)", style=TRACE_COR)
        )
        return
    for inv in turn.tool_invocations:
        marca = "✓" if inv.ok else "✗"
        estilo = TRACE_COR if inv.ok else "bold red"
        console.print(Text(f"  {marca} {inv.summary()}  {inv.duration_ms}ms", style=estilo))
        preview = json.dumps(inv.result, ensure_ascii=False, default=str)
        if len(preview) > 220:
            preview = preview[:220] + "…"
        console.print(Text(f"     → {preview}", style="dim"))


def _print_turn_footer(turn) -> None:
    partes = [f"{turn.iterations} iteração(ões)", f"{turn.duration_ms}ms"]
    if turn.usage.total:
        partes.append(f"{turn.usage.total} tokens")
    if turn.truncated:
        partes.append("[teto de iterações atingido]")
    console.print(Text("  " + " · ".join(partes), style="dim"))


def _build_service() -> AgentService:
    try:
        return AgentService()
    except LLMError as exc:
        console.print(
            Panel(
                Text(str(exc), style="red"),
                title="Não consegui iniciar o agente",
                border_style="red",
            )
        )
        console.print(
            "\nConfigure o provedor em [bold].env[/bold] "
            "(copie de [bold].env.example[/bold]).\n"
            "Chave gratuita do Gemini: "
            "[link]https://aistudio.google.com/apikey[/link]"
        )
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


@app.command()
def build(
    força: bool = typer.Option(
        False, "--force", help="Reconstrói mesmo que os artefatos já existam."
    ),
) -> None:
    """Prepara os dados: CSV -> SQLite e indexação do manual de políticas."""
    from emporio.data.loader import build_database
    from emporio.retrieval.index import PolicyIndex

    settings.ensure_dirs()

    if força and settings.db_path.exists():
        settings.db_path.unlink()
    if força and settings.policy_index_path.exists():
        settings.policy_index_path.unlink()

    with console.status("Carregando CSVs no SQLite…"):
        achados = build_database()
    console.print(f"[green]✓[/green] Banco da operação: {settings.db_path}")

    avisos = [a for a in achados if a.severity == "warning"]
    console.print(
        f"  {len(achados)} ocorrências de qualidade registradas "
        f"({len(avisos)} de atenção) → docs/DATA_QUALITY.md"
    )

    with console.status("Indexando o manual de políticas…"):
        index = PolicyIndex.build()
        index.save()
    console.print(
        f"[green]✓[/green] Índice de políticas: {len(index.chunks)} trechos "
        f"→ {settings.policy_index_path}"
    )
    console.print("\nPronto. Rode [bold]emporio chat[/bold] para conversar com o agente.")


@app.command()
def chat(
    sessao: str = typer.Option(
        "", "--session", "-s", help="Retoma uma sessão existente pelo id."
    ),
    trace: bool = typer.Option(
        False, "--trace", "-t", help="Mostra as consultas feitas em cada resposta."
    ),
    mensagem: str = typer.Option(
        "", "--message", "-m", help="Envia uma única mensagem e sai (modo não interativo)."
    ),
) -> None:
    """Conversa com o agente no terminal."""
    service = _build_service()
    session_id = service.open_session(sessao or None, channel="cli")

    # Modo one-shot: útil para script, demo e pipeline.
    if mensagem:
        turn = service.send(session_id, mensagem)
        if trace:
            _print_trace(turn)
        console.print(turn.reply)
        raise typer.Exit()

    config = service.configuration()
    console.print(
        Panel(
            Text.from_markup(
                f"[bold]{AGENT_NAME}[/bold] · Empório da Música\n"
                f"[dim]{config['provedor']} · {config['modelo']} · "
                f"sessão {session_id}[/dim]\n"
                f"[dim]agora: {config['data_de_referencia']}[/dim]"
            ),
            border_style="green",
        )
    )
    if config.get("aviso_data"):
        console.print(Text(f"  {config['aviso_data']}", style="yellow"))
    console.print(Text("Digite /ajuda para os comandos da sessão.\n", style="dim"))
    console.print(Text(f"{AGENT_NAME}:", style=AGENTE_COR), end=" ")
    console.print(SAUDACAO_INICIAL)

    while True:
        try:
            console.print()
            entrada = console.input(f"[{CLIENTE_COR}]Você:[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Até mais![/dim]")
            break
        if not entrada:
            continue

        if entrada.startswith("/"):
            comando = entrada.lower().split()[0]
            if comando in {"/sair", "/quit", "/exit"}:
                console.print("[dim]Até mais![/dim]")
                break
            if comando == "/trace":
                trace = not trace
                console.print(
                    f"[dim]detalhamento {'ligado' if trace else 'desligado'}[/dim]"
                )
                continue
            if comando == "/reset":
                session_id = service.reset_session(session_id)
                console.print(f"[dim]nova sessão: {session_id}[/dim]")
                continue
            if comando == "/sessao":
                console.print(f"[dim]sessão atual: {session_id}[/dim]")
                continue
            if comando == "/historico":
                for item in service.transcript(session_id):
                    quem = "Você" if item["role"] == "user" else AGENT_NAME
                    estilo = CLIENTE_COR if item["role"] == "user" else AGENTE_COR
                    console.print(Text(f"{quem}:", style=estilo), end=" ")
                    console.print(item["content"])
                continue
            if comando == "/config":
                _print_config(service)
                continue
            if comando == "/prompt":
                console.print(Panel(Markdown(build_system_prompt()), border_style="dim"))
                continue
            if comando == "/ferramentas":
                _print_tools()
                continue
            if comando == "/ajuda":
                console.print(Panel(AJUDA_INTERNA, border_style="dim"))
                continue
            console.print("[yellow]Comando não reconhecido. Tente /ajuda.[/yellow]")
            continue

        try:
            with console.status("[dim]consultando…[/dim]", spinner="dots"):
                turn = service.send(session_id, entrada)
        except LLMError as exc:
            console.print(Panel(Text(str(exc), style="red"), border_style="red"))
            continue

        console.print()
        if trace:
            _print_trace(turn)
        console.print(Text(f"{AGENT_NAME}:", style=AGENTE_COR), end=" ")
        console.print(turn.reply)
        if trace:
            _print_turn_footer(turn)


def _print_config(service: AgentService) -> None:
    table = Table(show_header=False, box=None)
    for key, value in service.configuration().items():
        table.add_row(Text(key, style="dim"), str(value))
    console.print(Panel(table, title="Configuração", border_style="dim"))


def _print_tools() -> None:
    from emporio.tools.registry import TOOLS

    table = Table(box=None, pad_edge=False)
    table.add_column("ferramenta", style="bold")
    table.add_column("parâmetros", style="dim")
    table.add_column("dado pessoal", style="dim")
    for tool in TOOLS:
        params = ", ".join((tool.parameters.get("properties") or {}).keys()) or "—"
        table.add_row(tool.name, params, "sim" if tool.sensitive else "—")
    console.print(Panel(table, title=f"{len(TOOLS)} ferramentas", border_style="dim"))


@app.command()
def ask(
    pergunta: list[str] = typer.Argument(..., help="A pergunta do cliente."),
    trace: bool = typer.Option(False, "--trace", "-t", help="Mostra as consultas."),
) -> None:
    """Faz uma pergunta avulsa ao agente, sem abrir sessão interativa."""
    service = _build_service()
    session_id = service.open_session(None, channel="cli-ask")
    turn = service.send(session_id, " ".join(pergunta))
    if trace:
        _print_trace(turn)
        console.print()
    console.print(turn.reply)


@app.command("tool")
def run_tool(
    nome: str = typer.Argument(..., help="Nome da ferramenta."),
    args: list[str] = typer.Argument(None, help="Argumentos em chave=valor."),
) -> None:
    """Executa uma ferramenta diretamente, sem LLM.

    Serve para inspecionar a camada determinística — é com isto que se verifica
    um preço ou um prazo sem gastar uma chamada de modelo.
    """
    from emporio.agent.loop import _coerce_arguments
    from emporio.tools.common import ToolError
    from emporio.tools.registry import get_tool

    spec = get_tool(nome)
    if spec is None:
        from emporio.tools.registry import TOOLS

        console.print(f"[red]Ferramenta '{nome}' não existe.[/red]")
        console.print("Disponíveis: " + ", ".join(t.name for t in TOOLS))
        raise typer.Exit(code=1)

    bruto: dict = {}
    for item in args or []:
        if "=" not in item:
            console.print(f"[red]Argumento inválido: {item!r}. Use chave=valor.[/red]")
            raise typer.Exit(code=1)
        chave, _, valor = item.partition("=")
        bruto[chave.strip()] = valor.strip()

    try:
        resultado = spec.handler(**_coerce_arguments(spec, bruto))
    except ToolError as exc:
        resultado = exc.to_dict()
    console.print_json(json.dumps(resultado, ensure_ascii=False, default=str))


@app.command()
def sessions(limite: int = typer.Option(20, "--limit", "-n")) -> None:
    """Lista as sessões de atendimento gravadas."""
    from emporio.memory.store import ConversationStore

    store = ConversationStore()
    registros = store.list_sessions(limite)
    if not registros:
        console.print("[dim]Nenhuma sessão gravada ainda.[/dim]")
        return
    table = Table(box=None)
    table.add_column("sessão", style="bold")
    table.add_column("canal")
    table.add_column("mensagens", justify="right")
    table.add_column("atualizada em", style="dim")
    for item in registros:
        table.add_row(item.session_id, item.channel, str(item.n_messages), item.updated_at)
    console.print(table)

    stats = store.tool_usage_stats()
    if stats:
        console.print()
        uso = Table(box=None, title="Uso de ferramentas", title_style="dim")
        uso.add_column("ferramenta", style="bold")
        uso.add_column("chamadas", justify="right")
        uso.add_column("ok", justify="right")
        uso.add_column("média ms", justify="right")
        for row in stats:
            uso.add_row(
                row["name"],
                str(row["chamadas"]),
                str(row["sucessos"]),
                str(row["media_ms"]),
            )
        console.print(uso)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Endereço de bind."),
    port: int = typer.Option(8000, help="Porta."),
) -> None:
    """Sobe a API HTTP e a interface web de chat."""
    import uvicorn

    console.print(
        Panel(
            Text.from_markup(
                f"Interface web: [bold link]http://{host}:{port}[/bold link]\n"
                f"[dim]API: POST /api/chat · docs em /docs[/dim]"
            ),
            border_style="green",
        )
    )
    uvicorn.run("emporio.api:api", host=host, port=port, log_level="warning")


@app.command()
def doctor() -> None:
    """Diagnóstico do ambiente: dados, índice, provedor e chave."""
    from emporio.data.db import database_exists
    from emporio.llm.factory import describe_configuration

    table = Table(show_header=False, box=None)

    def linha(nome: str, ok: bool, detalhe: str) -> None:
        table.add_row(
            Text("✓" if ok else "✗", style="green" if ok else "red"),
            Text(nome, style="bold"),
            Text(detalhe, style="dim"),
        )

    csvs = list(settings.data_dir.glob("*.csv"))
    linha("CSVs do desafio", len(csvs) >= 6, f"{len(csvs)} arquivos em {settings.data_dir}")
    linha(
        "Manual de políticas",
        settings.policy_pdf_path.exists(),
        str(settings.policy_pdf_path.name),
    )
    linha(
        "Banco da operação",
        database_exists(),
        str(settings.db_path) if database_exists() else "ausente — rode `emporio build`",
    )
    linha(
        "Índice de políticas",
        settings.policy_index_path.exists(),
        str(settings.policy_index_path)
        if settings.policy_index_path.exists()
        else "ausente — rode `emporio build`",
    )

    config = describe_configuration()
    linha("Provedor", True, f"{config['provedor']} · {config['modelo']}")
    linha(
        "Chave de API",
        bool(config["chave_configurada"]),
        "configurada" if config["chave_configurada"] else "ausente — preencha o .env",
    )
    linha("Data de referência", True, settings.now().strftime("%d/%m/%Y %H:%M (%Z)"))

    console.print(Panel(table, title=f"emporio {__version__}", border_style="dim"))

    if not config["chave_configurada"]:
        console.print(
            "\n[yellow]Sem chave de API o agente não roda, mas a camada "
            "determinística funciona:[/yellow]\n"
            "  [bold]pytest[/bold]                                      roda a suíte\n"
            "  [bold]emporio tool buscar_produtos termo=violao[/bold]   consulta direta"
        )
        raise typer.Exit(code=1)


@app.command("eval")
def run_eval_command(
    filtro: str = typer.Option(
        "", "--filter", "-f", help="Roda só os casos cujo id ou eixo casar com o texto."
    ),
    report: bool = typer.Option(
        False, "--report", help="Grava docs/EVALUATION.md e var/eval_report.json."
    ),
    verboso: bool = typer.Option(
        False, "--verbose", "-v", help="Mostra a resposta completa de cada caso."
    ),
) -> None:
    """Roda o conjunto de avaliação do agente (eval/cases.json)."""
    from emporio.evaluation import load_cases, run_eval, save_report, write_markdown

    service = _build_service()
    casos = load_cases()
    total = len([c for c in casos if not filtro or filtro in c["id"] or filtro == c.get("eixo")])
    console.print(f"[dim]Rodando {total} caso(s)…[/dim]\n")

    def mostrar(resultado) -> None:
        marca = "[green]PASS[/green]" if resultado.passou else "[red]FAIL[/red]"
        ferramentas = ", ".join(resultado.ferramentas_usadas) or "nenhuma"
        console.print(f"{marca}  {resultado.case_id}  [dim]({ferramentas})[/dim]")
        for falha in resultado.falhas:
            console.print(f"       [red]· {falha}[/red]")
        if resultado.erro:
            console.print(f"       [red]· {resultado.erro}[/red]")
        if verboso:
            console.print(f"       [dim]{resultado.resposta}[/dim]\n")

    resultado = run_eval(filtro=filtro, service=service, on_result=mostrar)

    console.print()
    tabela = Table(box=None, title="Resultado por eixo", title_style="dim")
    tabela.add_column("eixo", style="bold")
    tabela.add_column("aprovados", justify="right")
    for eixo, (ok, qtd) in sorted(resultado.por_eixo().items()):
        estilo = "green" if ok == qtd else "yellow" if ok else "red"
        tabela.add_row(eixo, f"[{estilo}]{ok}/{qtd}[/{estilo}]")
    console.print(tabela)

    cor = "green" if resultado.score >= 0.9 else "yellow" if resultado.score >= 0.7 else "red"
    console.print(
        f"\n[bold {cor}]{resultado.aprovados}/{resultado.total} "
        f"({resultado.score * 100:.0f}%)[/bold {cor}]"
    )

    if report:
        console.print(f"[dim]→ {save_report(resultado)}[/dim]")
        console.print(f"[dim]→ {write_markdown(resultado)}[/dim]")

    if resultado.aprovados < resultado.total:
        raise typer.Exit(code=1)


@app.command()
def demo(
    roteiro: str = typer.Option(
        "", "--script", help="Arquivo .txt com uma mensagem do cliente por linha."
    ),
    saida: str = typer.Option(
        "", "--out", "-o", help="Arquivo .md onde gravar a transcrição."
    ),
    data: str = typer.Option(
        "", "--date", help="Congela a data de referência (ex.: 2026-03-20T10:30)."
    ),
    titulo: str = typer.Option("Conversa de exemplo", "--title"),
    contexto: str = typer.Option(
        "", "--context", help="Parágrafo de contexto incluído no topo da transcrição."
    ),
) -> None:
    """Roda um roteiro de mensagens e grava a transcrição em Markdown.

    É assim que os exemplos em `examples/` são gerados: conversas reais com o
    agente, registradas com as consultas que ele fez em cada turno, em vez de
    diálogos escritos à mão.
    """
    if data:
        os.environ["EMPORIO_NOW"] = data

    if roteiro:
        linhas = [
            linha.strip()
            for linha in Path(roteiro).read_text(encoding="utf-8").splitlines()
            if linha.strip() and not linha.startswith("#")
        ]
    else:
        console.print("[red]Informe --script com o roteiro de mensagens.[/red]")
        raise typer.Exit(code=1)

    service = _build_service()
    session_id = service.open_session(None, channel="demo")
    config = service.configuration()

    partes: list[str] = [
        f"# {titulo}",
        "",
        f"> Transcrição real gerada por `emporio demo`. "
        f"Modelo: `{config['provedor']}/{config['modelo']}`. "
        f"Data de referência do sistema: **{config['data_de_referencia']}**.",
        "",
    ]
    if contexto:
        partes += [f"**Cenário.** {contexto}", ""]
    partes += ["---", ""]

    for mensagem in linhas:
        console.print(f"[{CLIENTE_COR}]Você:[/] {mensagem}")
        with console.status("[dim]consultando…[/dim]", spinner="dots"):
            turn = service.send(session_id, mensagem)
        console.print(Text(f"{AGENT_NAME}:", style=AGENTE_COR), end=" ")
        console.print(turn.reply)
        _print_trace(turn)
        console.print()

        partes += [f"**Cliente:** {mensagem}", ""]
        if turn.tool_invocations:
            consultas = "  \n".join(
                f"`{inv.summary()}`" + ("" if inv.ok else "  ← erro")
                for inv in turn.tool_invocations
            )
            partes += [
                "<details><summary>consultas feitas pelo agente</summary>",
                "",
                consultas,
                "",
                "</details>",
                "",
            ]
        partes += [f"**{AGENT_NAME}:** {turn.reply}", "", "---", ""]

    partes += [
        "",
        f"<sub>Sessão `{session_id}`. "
        "As consultas listadas são as chamadas de ferramenta efetivamente "
        "executadas em cada turno, capturadas durante a conversa.</sub>",
        "",
    ]

    texto = "\n".join(partes)
    if saida:
        destino = Path(saida)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(texto, encoding="utf-8")
        console.print(f"[green]✓[/green] transcrição gravada em {destino}")
    else:
        console.print(texto)


@app.command()
def version() -> None:
    """Mostra a versão."""
    console.print(f"emporio {__version__}")


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
