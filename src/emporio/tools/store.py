"""Dados cadastrais e horário de funcionamento da loja.

Estes valores vêm do manual de políticas (§1.2 e §2), mas estão estruturados
em código em vez de serem recuperados como texto. Motivo: o horário precisa
de *aritmética* ("vocês estão abertos agora?", "quando voltam a atender?"), e
aritmética sobre prosa recuperada é exatamente o tipo de coisa que um LLM erra
com confiança. O texto original continua indexado para quando o cliente quiser
a regra na íntegra.

Divergências do próprio manual estão registradas em `INCONSISTENCIAS_MANUAL` e
expostas ao agente — preferimos sinalizar a ambiguidade a escolher um lado em
silêncio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from emporio.config import settings
from emporio.tools.common import WEEKDAY_NAMES_PT

# §2 — Horário de funcionamento. Índice = weekday() do Python (0 = segunda).
# `None` significa fechado.
BUSINESS_HOURS: dict[int, tuple[time, time] | None] = {
    0: (time(9, 0), time(18, 0)),  # segunda
    1: (time(9, 0), time(18, 0)),
    2: (time(9, 0), time(18, 0)),
    3: (time(9, 0), time(18, 0)),
    4: (time(9, 0), time(18, 0)),  # sexta
    5: (time(9, 0), time(13, 0)),  # sábado
    6: None,  # domingo
}

HOURS_HUMAN = (
    "Segunda a sexta, 09:00 às 18:00. Sábado, 09:00 às 13:00. "
    "Domingos e feriados, fechado."
)


@dataclass(frozen=True)
class StoreProfile:
    """Identificação da loja, conforme §1.2 do manual."""

    razao_social: str = "Empório da Música Instrumentos Musicais Ltda."
    nome_fantasia: str = "Empório da Música"
    slogan: str = "Sua música começa aqui."
    cnpj: str = "12.345.678/0001-90"
    endereco: str = "Rua 14 de Maio, 3200 — Centro, Campo Grande - MS, 79202-333"
    cidade: str = "Campo Grande"
    estado: str = "MS"
    cep: str = "79202-333"
    telefone: str = "(67) 3341-4444"
    whatsapp: str = "(67) 3321-4500"
    email: str = "contato@emporiodamusica.com.br"
    fundacao: int = 2008
    site_rastreio: str = "Correios ou Jadlog, conforme a modalidade de envio"


STORE = StoreProfile()

# O manual se contradiz nestes dois pontos. O agente recebe a observação junto
# da resposta para não afirmar com certeza o que a fonte não sustenta.
INCONSISTENCIAS_MANUAL = [
    (
        "O §1.2 registra (67) 3341-4444 como 'Telefone / WhatsApp', enquanto o "
        "§7 apresenta (67) 3321-4500 como o canal de WhatsApp. Os dois números "
        "são informados: o primeiro como telefone da loja, o segundo como "
        "WhatsApp. Vale a equipe confirmar qual está ativo."
    ),
    (
        "O e-mail aparece como contato@emporiodamusica.com.br na capa e com "
        "acento no §1.2. Adotamos a versão sem acento, que é o domínio válido."
    ),
]

# §1 — a loja trabalha exclusivamente com instrumentos. Lista usada para
# redirecionar pedidos de acessório (§7.1).
NAO_COMERCIALIZADOS = (
    "cordas",
    "palhetas",
    "cabos",
    "cases",
    "pedais",
    "amplificadores",
)


def _hours_label(day: int) -> str:
    window = BUSINESS_HOURS[day]
    if window is None:
        return "fechado"
    opening, closing = window
    return f"{opening.strftime('%H:%M')} às {closing.strftime('%H:%M')}"


def next_opening(reference: datetime) -> datetime:
    """Próximo instante em que a loja abre, a partir de `reference`."""
    candidate = reference
    for _ in range(8):  # uma semana + folga
        window = BUSINESS_HOURS[candidate.weekday()]
        if window is not None:
            opening, closing = window
            opening_dt = candidate.replace(
                hour=opening.hour, minute=opening.minute, second=0, microsecond=0
            )
            closing_dt = candidate.replace(
                hour=closing.hour, minute=closing.minute, second=0, microsecond=0
            )
            if candidate < opening_dt:
                return opening_dt
            if candidate < closing_dt:
                return candidate  # já está aberto
        candidate = (candidate + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return reference


def is_open_at(moment: datetime) -> bool:
    window = BUSINESS_HOURS[moment.weekday()]
    if window is None:
        return False
    opening, closing = window
    return opening <= moment.time() < closing


def informacoes_da_loja(incluir_horarios: bool = True) -> dict:
    """Dados cadastrais, contato e situação de funcionamento agora.

    A checagem de "aberto agora" usa o fuso da loja (America/Campo_Grande,
    UTC-4), não o fuso de quem roda o processo.
    """
    now = settings.now()
    aberto = is_open_at(now)

    payload: dict = {
        "nome": STORE.nome_fantasia,
        "razao_social": STORE.razao_social,
        "slogan": STORE.slogan,
        "cnpj": STORE.cnpj,
        "endereco": STORE.endereco,
        "cidade": f"{STORE.cidade}/{STORE.estado}",
        "cep": STORE.cep,
        "telefone": STORE.telefone,
        "whatsapp": STORE.whatsapp,
        "email": STORE.email,
        "fundada_em": STORE.fundacao,
        "escopo": (
            "Trabalha exclusivamente com instrumentos musicais. Não "
            "comercializa acessórios: " + ", ".join(NAO_COMERCIALIZADOS) + " (§1)."
        ),
        "observacoes_sobre_a_fonte": INCONSISTENCIAS_MANUAL,
    }

    if incluir_horarios:
        payload["horarios"] = {
            WEEKDAY_NAMES_PT[day]: _hours_label(day) for day in range(7)
        }
        payload["horarios_resumo"] = HOURS_HUMAN
        payload["agora"] = {
            "data_hora_local": now.strftime("%d/%m/%Y %H:%M"),
            "dia_da_semana": WEEKDAY_NAMES_PT[now.weekday()],
            "aberto": aberto,
        }
        if aberto:
            closing = BUSINESS_HOURS[now.weekday()][1]  # type: ignore[index]
            payload["agora"]["fecha_hoje_as"] = closing.strftime("%H:%M")
        else:
            reopen = next_opening(now)
            mesmo_dia = reopen.date() == now.date()
            payload["agora"]["proxima_abertura"] = (
                f"{'hoje' if mesmo_dia else WEEKDAY_NAMES_PT[reopen.weekday()]} "
                f"às {reopen.strftime('%H:%M')}"
            )
            payload["agora"]["proxima_abertura_iso"] = reopen.strftime("%Y-%m-%d %H:%M")
            # §7.1 — fora do expediente, informar o horário de retorno.
            payload["agora"]["orientacao"] = (
                "Fora do horário de atendimento. Informe ao cliente quando a "
                "equipe retorna e siga ajudando com o que for possível agora."
            )
        payload["observacao_datas_especiais"] = (
            "Em Black Friday, aniversário da loja e Natal o horário pode ser "
            "estendido; alterações são comunicadas nas redes sociais (§2)."
        )
    return payload


def feriado_ou_domingo(dia: date) -> bool:
    """Apenas domingo é detectável — não há calendário de feriados na base."""
    return dia.weekday() == 6
