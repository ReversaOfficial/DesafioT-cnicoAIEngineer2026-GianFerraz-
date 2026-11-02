"""Motor de precificação: promoção, PIX e parcelamento.

Todo o §3, §3.1 e §6.2 do manual vira código aqui. É o tipo de regra que um
LLM "quase acerta": ele lembra dos 5% do PIX e esquece que não acumulam com
preço promocional, ou oferece 10x em um produto cuja parcela ficaria abaixo do
mínimo. Errar preço para cliente é, nas palavras do próprio manual (§7.1),
risco de propaganda enganosa — então isso não fica a cargo do modelo.

Ambiguidade resolvida: a tabela do §3 diz "Cartão de Crédito — até 12x sem
juros — parcela mínima de R$ 100,00", enquanto o §3.1 detalha faixas (até 3x
sem mínimo, 4–6x mínimo R$ 80, 7–12x mínimo R$ 100). Tratamos o §3 como resumo
e o §3.1 como regra vigente, por ser a mais específica. A decisão está
registrada no README.
"""

from __future__ import annotations

from dataclasses import dataclass

from emporio.data.db import connect, ensure_database
from emporio.text_utils import brl
from emporio.tools.common import PRODUCT_SELECT, ToolError, availability_label

# §3 — PIX à vista: 5% sobre o preço de tabela.
PIX_DISCOUNT_PERCENT = 5.0

# §3.1 — faixas de parcelamento: (máximo de parcelas, parcela mínima em R$).
INSTALLMENT_TIERS: tuple[tuple[int, float], ...] = (
    (3, 0.0),
    (6, 80.0),
    (12, 100.0),
)
MAX_INSTALLMENTS = 12

# §3.1 — abaixo deste total não há parcelamento.
MIN_TOTAL_FOR_INSTALLMENTS = 50.0

# §3.1 — combinação de formas de pagamento permitida acima deste valor.
MIN_TOTAL_FOR_COMBINED_PAYMENT = 2000.0


@dataclass(frozen=True)
class InstallmentOption:
    parcelas: int
    valor_parcela: float

    def to_dict(self) -> dict:
        return {
            "parcelas": self.parcelas,
            "valor_parcela": round(self.valor_parcela, 2),
            "descricao": f"{self.parcelas}x de {brl(self.valor_parcela)} sem juros",
        }


def minimum_installment_for(parcelas: int) -> float:
    """Parcela mínima exigida para um número de parcelas (§3.1)."""
    for limit, minimum in INSTALLMENT_TIERS:
        if parcelas <= limit:
            return minimum
    return INSTALLMENT_TIERS[-1][1]


def installment_options(total: float) -> list[InstallmentOption]:
    """Parcelamentos viáveis para um total, aplicando as faixas do §3.1."""
    if total < MIN_TOTAL_FOR_INSTALLMENTS:
        return []
    options: list[InstallmentOption] = []
    for parcelas in range(2, MAX_INSTALLMENTS + 1):
        valor = total / parcelas
        if valor >= minimum_installment_for(parcelas):
            options.append(InstallmentOption(parcelas, valor))
    return options


def max_installments(total: float) -> int:
    """Maior número de parcelas possível. 1 significa somente à vista."""
    options = installment_options(total)
    return options[-1].parcelas if options else 1


def calcular_preco(
    product_id: int,
    quantidade: int = 1,
    forma_pagamento: str = "",
) -> dict:
    """Composição completa de preço de um produto.

    Devolve o preço de tabela, a promoção ativa (se houver), o valor no PIX e
    todos os parcelamentos possíveis — já filtrados pelas regras de parcela
    mínima. `forma_pagamento` é opcional e apenas destaca a opção pedida; o
    retorno sempre traz o quadro inteiro, para o agente poder comparar sem
    precisar de uma segunda chamada.
    """
    quantidade = max(1, int(quantidade or 1))
    ensure_database()
    conn = connect(read_only=True)
    try:
        row = conn.execute(
            PRODUCT_SELECT + " WHERE p.product_id = :pid", {"pid": int(product_id)}
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"Não existe produto com id {product_id} no catálogo.")

    list_price = float(row["list_price"])
    discount = float(row["active_discount_percent"] or 0)
    has_promo = discount > 0

    unit_price = round(list_price * (1 - discount / 100), 2)
    subtotal = round(unit_price * quantidade, 2)
    subtotal_tabela = round(list_price * quantidade, 2)

    payload: dict = {
        "product_id": row["product_id"],
        "nome": row["name"],
        "quantidade": quantidade,
        "disponibilidade": row["availability"],
        "disponibilidade_descricao": availability_label(row["availability"]),
        "preco_tabela_unitario": round(list_price, 2),
        "preco_tabela_unitario_formatado": brl(list_price),
        "subtotal_preco_tabela": subtotal_tabela,
    }

    if has_promo:
        payload["promocao"] = {
            "ativa": True,
            "nome": row["active_promotion_label"],
            "desconto_percentual": discount,
            "preco_unitario_promocional": unit_price,
            "preco_unitario_promocional_formatado": brl(unit_price),
            "economia_total": round(subtotal_tabela - subtotal, 2),
        }
    else:
        payload["promocao"] = {"ativa": False}

    # --- PIX (§3 + §6.2) ---------------------------------------------------
    # O 5% do PIX incide sobre o preço de tabela e NÃO acumula com preço
    # promocional. Quando há promoção ativa, o cliente paga o preço
    # promocional no PIX, sem desconto adicional.
    if has_promo:
        pix_total = subtotal
        payload["pix"] = {
            "valor_total": pix_total,
            "valor_total_formatado": brl(pix_total),
            "desconto_pix_aplicado": False,
            "observacao": (
                "O desconto de 5% do PIX não se aplica sobre preço já "
                "promocional (§6.2). No PIX o cliente paga o preço "
                f"promocional de {brl(unit_price)} por unidade."
            ),
        }
    else:
        pix_total = round(subtotal * (1 - PIX_DISCOUNT_PERCENT / 100), 2)
        payload["pix"] = {
            "valor_total": pix_total,
            "valor_total_formatado": brl(pix_total),
            "desconto_pix_aplicado": True,
            "desconto_percentual": PIX_DISCOUNT_PERCENT,
            "economia": round(subtotal - pix_total, 2),
            "observacao": f"PIX à vista com {PIX_DISCOUNT_PERCENT:.0f}% de desconto (§3).",
        }

    # --- Cartão de crédito (§3.1) -----------------------------------------
    # O parcelamento incide sobre o subtotal vigente (promocional, se houver),
    # sem o desconto do PIX — são formas de pagamento distintas.
    options = installment_options(subtotal)
    payload["cartao_credito"] = {
        "valor_total": subtotal,
        "valor_total_formatado": brl(subtotal),
        "sem_juros": True,
        "max_parcelas": max_installments(subtotal),
        "opcoes": [o.to_dict() for o in options],
    }
    if not options:
        payload["cartao_credito"]["observacao"] = (
            f"Compras abaixo de {brl(MIN_TOTAL_FOR_INSTALLMENTS)} não são "
            "parceladas (§3.1)."
        )
    else:
        payload["cartao_credito"]["regras_de_parcela_minima"] = (
            "Até 3x sem parcela mínima; 4x a 6x com parcela mínima de "
            f"{brl(80)}; 7x a 12x com parcela mínima de {brl(100)} (§3.1)."
        )

    payload["debito_e_boleto"] = {
        "valor_total": subtotal,
        "valor_total_formatado": brl(subtotal),
        "observacao": (
            "Débito e boleto são à vista, sem o desconto do PIX. Boleto "
            "compensa em até 3 dias úteis (§3)."
        ),
    }

    payload["combinacao_de_pagamentos"] = {
        "permitida": subtotal > MIN_TOTAL_FOR_COMBINED_PAYMENT,
        "regra": (
            "Combinar formas de pagamento (ex.: PIX + cartão) é permitido para "
            f"compras acima de {brl(MIN_TOTAL_FOR_COMBINED_PAYMENT)} (§3.1)."
        ),
    }

    if forma_pagamento:
        payload["destaque"] = _highlight(forma_pagamento, payload)

    if row["availability"] != "in_stock":
        payload["aviso_disponibilidade"] = (
            "Atenção: o preço acima é válido, mas o produto está "
            f"{availability_label(row['availability'])}. Informe a situação ao "
            "cliente e ofereça alternativas (§7.3)."
        )
    return payload


def _highlight(forma_pagamento: str, payload: dict) -> dict:
    """Resumo da forma de pagamento que o cliente mencionou."""
    key = forma_pagamento.strip().lower()
    if "pix" in key:
        return {"forma": "pix", **payload["pix"]}
    if "boleto" in key:
        return {"forma": "boleto", **payload["debito_e_boleto"]}
    if "debito" in key or "débito" in key:
        return {"forma": "debito", **payload["debito_e_boleto"]}
    if "credito" in key or "crédito" in key or "cartao" in key or "x" in key:
        # Aceita "6x", "credit_6x", "6 vezes".
        import re

        match = re.search(r"(\d{1,2})\s*x", key)
        if match:
            wanted = int(match.group(1))
            found = next(
                (o for o in payload["cartao_credito"]["opcoes"] if o["parcelas"] == wanted),
                None,
            )
            if found:
                return {"forma": f"credito_{wanted}x", **found}
            minimo = minimum_installment_for(wanted)
            return {
                "forma": f"credito_{wanted}x",
                "disponivel": False,
                "motivo": (
                    f"{wanted}x não é possível para este valor: a parcela "
                    f"ficaria abaixo do mínimo de {brl(minimo)} exigido para "
                    f"essa faixa (§3.1). O máximo é "
                    f"{payload['cartao_credito']['max_parcelas']}x."
                ),
            }
        return {"forma": "credito", **payload["cartao_credito"]}
    return {"forma": key, "observacao": "Forma de pagamento não reconhecida."}
