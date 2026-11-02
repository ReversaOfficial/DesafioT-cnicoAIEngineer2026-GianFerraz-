"""Helpers compartilhados pelas ferramentas.

Regra de ouro desta camada: **nada de LLM aqui**. Toda ferramenta é uma função
Python determinística que lê o banco ou o índice de políticas e devolve um
dicionário serializável. Isso coloca a fronteira no lugar certo — o modelo
decide *o que* perguntar e como conversar; preço, prazo e disponibilidade saem
de código testável.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from emporio.text_utils import brl

# Tamanho máximo de lista devolvida ao modelo. Limite existe para controlar
# contexto: 10 produtos descritos já ocupam espaço suficiente para degradar a
# atenção do modelo no resto da conversa.
MAX_RESULTS = 10


class ToolError(Exception):
    """Erro de uso previsível de uma ferramenta.

    O laço do agente converte em `{"erro": ...}` e devolve ao modelo, que então
    pede o dado que falta ao cliente. Diferente de uma exceção inesperada, que
    sobe e é logada.
    """

    def __init__(self, message: str, *, suggestion: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion

    def to_dict(self) -> dict:
        payload: dict[str, str] = {"erro": self.message}
        if self.suggestion:
            payload["sugestao"] = self.suggestion
        return payload


# ---------------------------------------------------------------------------
# Datas
# ---------------------------------------------------------------------------

WEEKDAY_NAMES_PT = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def format_date_pt(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def add_business_days(start: date, days: int) -> date:
    """Soma dias úteis considerando apenas sábado e domingo como não úteis.

    Limitação conhecida e documentada: não há calendário de feriados nacionais
    ou de Campo Grande/MS na base fornecida. Prazos em dias úteis são, portanto,
    estimativas otimistas — as ferramentas que os usam marcam o resultado como
    estimativa, e o agente é instruído a apresentá-lo como tal.
    """
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def days_between(start: date, end: date) -> int:
    return (end - start).days


# ---------------------------------------------------------------------------
# Serialização de produto
# ---------------------------------------------------------------------------

AVAILABILITY_LABELS = {
    "in_stock": "disponível em estoque",
    "out_of_stock": "sem estoque no momento",
    "discontinued": "descontinuado (fora de catálogo)",
    "coming_soon": "lançamento futuro (ainda não disponível para venda)",
}


def availability_label(availability: str) -> str:
    return AVAILABILITY_LABELS.get(availability, availability)


def product_payload(row: sqlite3.Row, *, detailed: bool = False) -> dict:
    """Converte uma linha de produto no payload entregue ao modelo.

    Preço promocional só aparece quando há promoção **ativa** — a view
    `v_product_pricing` já filtra `is_active = 1`, então nenhuma promoção
    vencida chega até aqui (§7.3: nunca prometer desconto fora de vigência).
    """
    list_price = float(row["list_price"] if "list_price" in row.keys() else row["price_brl"])
    discount = float(row["active_discount_percent"] or 0) if "active_discount_percent" in row.keys() else 0.0

    payload: dict = {
        "product_id": row["product_id"],
        "nome": row["name"],
        "marca": row["brand"],
        "categoria": row["category_name"] if "category_name" in row.keys() else None,
        "preco_tabela": round(list_price, 2),
        "preco_tabela_formatado": brl(list_price),
        "disponibilidade": row["availability"],
        "disponibilidade_descricao": availability_label(row["availability"]),
        "estoque": row["stock_quantity"],
    }

    if discount > 0:
        final = round(list_price * (1 - discount / 100), 2)
        payload.update(
            {
                "promocao_ativa": True,
                "promocao_nome": row["active_promotion_label"]
                if "active_promotion_label" in row.keys()
                else None,
                "desconto_percentual": discount,
                "preco_promocional": final,
                "preco_promocional_formatado": brl(final),
                # §6.2 — exigência de transparência: preço promocional sempre
                # apresentado junto do original e do percentual.
                "aviso_apresentacao": (
                    "Apresentar sempre o preço promocional junto do preço "
                    "original e do percentual de desconto (§6.2)."
                ),
            }
        )
    else:
        payload["promocao_ativa"] = False

    if detailed:
        payload.update(
            {
                "descricao": row["description"],
                "especificacoes": _parse_specs(row["specs_json"]),
                "status_catalogo": row["status"],
                "no_catalogo_desde": row["created_at"],
            }
        )
    return payload


def _parse_specs(specs_json: str) -> dict:
    import json

    try:
        data = json.loads(specs_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


PRODUCT_SELECT = """
SELECT p.*, c.name AS category_name,
       v.list_price, v.active_discount_percent, v.active_promotion_label
  FROM products p
  JOIN categories c ON c.category_id = p.category_id
  JOIN v_product_pricing v ON v.product_id = p.product_id
"""


def effective_unit_price(row: sqlite3.Row) -> tuple[float, float]:
    """(preço unitário vigente, desconto percentual aplicado)."""
    list_price = float(row["list_price"])
    discount = float(row["active_discount_percent"] or 0)
    return round(list_price * (1 - discount / 100), 2), discount
