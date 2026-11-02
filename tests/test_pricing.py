"""Regras de preço e parcelamento (§3, §3.1, §6.2 do manual).

São as regras mais fáceis de o agente errar com confiança, então são as mais
testadas. Cada teste nomeia a seção que está verificando.
"""

from __future__ import annotations

import pytest

from emporio.tools.pricing import (
    MIN_TOTAL_FOR_COMBINED_PAYMENT,
    MIN_TOTAL_FOR_INSTALLMENTS,
    PIX_DISCOUNT_PERCENT,
    calcular_preco,
    installment_options,
    max_installments,
    minimum_installment_for,
)

# Produtos reais da base usados como âncora.
TAKAMINE_GD20 = 95  # R$ 2.199,00, sem promoção ativa
CRAFTER_HT100 = 90  # R$ 2.399,00, promoção ativa de 18%
KALANI_TENOR = 127  # R$ 359,00, promoção ativa de 10%
UKULELE_BARATO = 119  # R$ 159,90


class TestParcelaMinima:
    """§3.1 — faixas de parcela mínima."""

    @pytest.mark.parametrize(
        ("parcelas", "minimo"),
        [(2, 0.0), (3, 0.0), (4, 80.0), (6, 80.0), (7, 100.0), (12, 100.0)],
    )
    def test_minimo_por_faixa(self, parcelas, minimo):
        assert minimum_installment_for(parcelas) == minimo

    def test_total_baixo_nao_parcela(self):
        """Abaixo de R$ 50,00 não há parcelamento (§3.1)."""
        assert installment_options(MIN_TOTAL_FOR_INSTALLMENTS - 0.01) == []
        assert max_installments(49.99) == 1

    def test_corte_respeita_parcela_minima(self):
        """R$ 359,00 para em 4x: a 5ª parcela ficaria em R$ 71,80 (< R$ 80)."""
        opcoes = installment_options(359.0)
        assert max(o.parcelas for o in opcoes) == 4
        assert all(o.valor_parcela >= minimum_installment_for(o.parcelas) for o in opcoes)

    def test_valor_alto_chega_a_12x(self):
        assert max_installments(2199.0) == 12

    def test_fronteira_de_80_reais(self):
        """Total de R$ 480,00 permite exatamente 6x de R$ 80,00."""
        assert max_installments(480.0) == 6
        assert max_installments(479.0) == 5


class TestPix:
    """§3 e §6.2 — desconto do PIX e não-cumulatividade."""

    def test_desconto_de_5_porcento_sem_promocao(self):
        resultado = calcular_preco(TAKAMINE_GD20)
        assert resultado["promocao"]["ativa"] is False
        assert resultado["pix"]["desconto_pix_aplicado"] is True
        assert resultado["pix"]["desconto_percentual"] == PIX_DISCOUNT_PERCENT
        esperado = round(2199.0 * 0.95, 2)
        assert resultado["pix"]["valor_total"] == esperado

    def test_pix_nao_acumula_com_promocao(self):
        """§6.2: o 5% do PIX não incide sobre preço já promocional."""
        resultado = calcular_preco(CRAFTER_HT100)
        assert resultado["promocao"]["ativa"] is True
        assert resultado["pix"]["desconto_pix_aplicado"] is False
        # No PIX paga-se o preço promocional, nem mais nem menos.
        assert resultado["pix"]["valor_total"] == resultado["promocao"][
            "preco_unitario_promocional"
        ]
        assert "não se aplica" in resultado["pix"]["observacao"]

    def test_pix_com_promocao_nao_aplica_desconto_duplo(self):
        resultado = calcular_preco(KALANI_TENOR)
        duplo = round(359.0 * 0.90 * 0.95, 2)
        assert resultado["pix"]["valor_total"] != duplo
        assert resultado["pix"]["valor_total"] == 323.10


class TestPromocao:
    def test_aplica_apenas_promocao_ativa(self):
        """O produto 95 tem promoção de 12% cadastrada, porém INATIVA."""
        resultado = calcular_preco(TAKAMINE_GD20)
        assert resultado["promocao"]["ativa"] is False
        assert resultado["preco_tabela_unitario"] == 2199.0

    def test_desconto_e_economia_coerentes(self):
        resultado = calcular_preco(CRAFTER_HT100, quantidade=2)
        promo = resultado["promocao"]
        assert promo["desconto_percentual"] == 18.0
        esperado_unit = round(2399.0 * 0.82, 2)
        assert promo["preco_unitario_promocional"] == esperado_unit
        assert promo["economia_total"] == round(2 * 2399.0 - 2 * esperado_unit, 2)


class TestQuantidadeECombinacao:
    def test_quantidade_multiplica_subtotal(self):
        um = calcular_preco(TAKAMINE_GD20, quantidade=1)
        tres = calcular_preco(TAKAMINE_GD20, quantidade=3)
        assert tres["subtotal_preco_tabela"] == round(um["subtotal_preco_tabela"] * 3, 2)

    def test_quantidade_invalida_vira_um(self):
        assert calcular_preco(TAKAMINE_GD20, quantidade=0)["quantidade"] == 1

    def test_combinacao_liberada_acima_de_2000(self):
        """§3.1 — combinar formas de pagamento acima de R$ 2.000,00."""
        caro = calcular_preco(TAKAMINE_GD20)
        assert caro["subtotal_preco_tabela"] > MIN_TOTAL_FOR_COMBINED_PAYMENT
        assert caro["combinacao_de_pagamentos"]["permitida"] is True

        barato = calcular_preco(UKULELE_BARATO)
        assert barato["combinacao_de_pagamentos"]["permitida"] is False


class TestDestaqueDeFormaDePagamento:
    def test_parcelamento_pedido_acima_do_maximo_explica_motivo(self):
        """Pedir 10x em item de R$ 359,00 deve recusar com o motivo, não inventar."""
        resultado = calcular_preco(KALANI_TENOR, forma_pagamento="10x")
        destaque = resultado["destaque"]
        assert destaque["disponivel"] is False
        assert "mínimo" in destaque["motivo"]
        assert "4x" in destaque["motivo"]  # informa o máximo possível

    def test_parcelamento_viavel_retorna_valor_da_parcela(self):
        resultado = calcular_preco(TAKAMINE_GD20, forma_pagamento="credito_6x")
        assert resultado["destaque"]["parcelas"] == 6
        assert resultado["destaque"]["valor_parcela"] == round(2199.0 / 6, 2)

    def test_destaque_pix(self):
        assert calcular_preco(TAKAMINE_GD20, forma_pagamento="pix")["destaque"][
            "forma"
        ] == "pix"


class TestDisponibilidade:
    def test_produto_sem_estoque_recebe_aviso(self):
        """Produto 96 está ativo mas com estoque zero."""
        resultado = calcular_preco(96)
        assert "aviso_disponibilidade" in resultado
        assert resultado["disponibilidade"] == "out_of_stock"

    def test_produto_disponivel_nao_recebe_aviso(self):
        assert "aviso_disponibilidade" not in calcular_preco(TAKAMINE_GD20)


def test_produto_inexistente_gera_erro_de_ferramenta():
    from emporio.tools.common import ToolError

    with pytest.raises(ToolError):
        calcular_preco(999999)
