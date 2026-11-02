"""Consulta de pedidos, LGPD e contagem de prazo de devolução."""

from __future__ import annotations

import pytest

from emporio.tools.common import ToolError
from emporio.tools.orders import (
    consultar_pedido,
    identificar_cliente,
    verificar_elegibilidade_devolucao,
)

# Pedido 7: cliente Letícia Gonçalves Rocha, (67) 99789-0123,
# feito em 03/02/2026, entregue (previsão 17/02/2026), R$ 159,90.
PEDIDO_ENTREGUE = 7
TITULAR = "Letícia Gonçalves Rocha"
TELEFONE = "(67) 99789-0123"


class TestVerificacaoDeIdentidade:
    """§9 — nada de dado pessoal antes de confirmar quem está falando."""

    def test_sem_confirmacao_nao_revela_nada(self):
        resultado = consultar_pedido(PEDIDO_ENTREGUE)
        assert resultado["verificacao_necessaria"] is True
        # O payload não pode conter nome, telefone, itens ou valores.
        serializado = str(resultado).lower()
        for vazamento in ("letícia", "leticia", "99789", "159", "shelby"):
            assert vazamento not in serializado

    def test_confirmacao_errada_tambem_nao_revela(self):
        resultado = consultar_pedido(PEDIDO_ENTREGUE, confirmacao="Outra Pessoa Qualquer")
        assert resultado["verificacao_necessaria"] is True

    def test_nome_completo_libera(self):
        resultado = consultar_pedido(PEDIDO_ENTREGUE, confirmacao=TITULAR)
        assert resultado.get("verificacao_necessaria") is None
        assert resultado["cliente"] == TITULAR

    def test_nome_parcial_com_sobrenome_libera(self):
        """'Letícia Rocha' deve casar com 'Letícia Gonçalves Rocha'."""
        resultado = consultar_pedido(PEDIDO_ENTREGUE, confirmacao="Leticia Rocha")
        assert resultado["cliente"] == TITULAR

    def test_telefone_em_qualquer_formato_libera(self):
        for variante in (TELEFONE, "67997890123", "99789-0123", "997890123"):
            resultado = consultar_pedido(PEDIDO_ENTREGUE, confirmacao=variante)
            assert resultado["cliente"] == TITULAR, variante

    def test_primeiro_nome_sozinho_nao_libera(self):
        """Nome isolado é fácil de adivinhar — exige nome completo ou telefone."""
        resultado = consultar_pedido(PEDIDO_ENTREGUE, confirmacao="Letícia")
        assert resultado["verificacao_necessaria"] is True

    def test_pedido_inexistente(self):
        with pytest.raises(ToolError):
            consultar_pedido(99999, confirmacao=TITULAR)


class TestConsultaDePedido:
    def test_dados_do_pedido_entregue(self):
        resultado = consultar_pedido(PEDIDO_ENTREGUE, confirmacao=TELEFONE)
        assert resultado["status"] == "delivered"
        assert resultado["status_descricao"] == "entregue"
        assert resultado["valor_total"] == 159.9
        assert resultado["codigo_de_rastreio"] == "BRKR1122334BR"
        assert len(resultado["itens"]) == 1

    def test_pedido_sem_rastreio_avisa_em_vez_de_inventar(self):
        """Pedido 9 está 'confirmed' e não tem código gerado ainda."""
        resultado = consultar_pedido(9, confirmacao="Juliana Almeida Rodrigues")
        assert "observacao_rastreio" in resultado
        assert "despacho" in resultado["observacao_rastreio"]

    def test_pedido_cancelado_traz_a_nota_interna(self):
        resultado = consultar_pedido(17, confirmacao="Matheus Ramos Correia")
        assert resultado["status"] == "cancelled"
        assert "observacoes_internas" in resultado

    def test_nunca_sugere_recalcular_total(self):
        resultado = consultar_pedido(PEDIDO_ENTREGUE, confirmacao=TELEFONE)
        assert "nota_sobre_o_total" in resultado
        assert "não recalcule" in resultado["nota_sobre_o_total"].lower()


class TestIdentificacaoDeCliente:
    def test_por_telefone(self):
        resultado = identificar_cliente(telefone="99789-0123")
        assert resultado["encontrado"] is True
        assert resultado["primeiro_nome"] == "Letícia"
        assert resultado["total_de_pedidos"] == 1

    def test_por_nome_completo(self):
        assert identificar_cliente(nome=TITULAR)["encontrado"] is True

    def test_cliente_de_outra_cidade_nao_e_metro(self):
        """A flag alimenta a regra de frete do §5.1."""
        encontrados = [
            identificar_cliente(nome=n)
            for n in ("Lucas Mendes da Silva",)
        ]
        assert encontrados[0]["regiao_metropolitana_cg"] is True

    def test_desconhecido_nao_inventa_cadastro(self):
        resultado = identificar_cliente(telefone="11999998888")
        assert resultado["encontrado"] is False
        assert "primeiro contato" in resultado["explicacao"]

    def test_sem_parametro_gera_erro(self):
        with pytest.raises(ToolError):
            identificar_cliente()


class TestArrependimento:
    """§4.1 — 7 dias corridos a partir do recebimento."""

    def test_dentro_do_prazo(self, agora):
        agora("2026-02-20")  # recebido em 17/02, 3 dias depois
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "arrependimento", TITULAR)
        assert r["elegivel"] is True
        assert r["veredito"] == "elegivel_arrependimento"
        assert r["data_limite"] == "24/02/2026"
        assert r["dias_restantes"] == 4

    def test_ultimo_dia_do_prazo_ainda_vale(self, agora):
        agora("2026-02-24")
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "arrependimento", TITULAR)
        assert r["elegivel"] is True
        assert r["dias_restantes"] == 0

    def test_dia_seguinte_ja_nao_vale(self, agora):
        agora("2026-02-25")
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "arrependimento", TITULAR)
        assert r["elegivel"] is False
        assert r["veredito"] == "fora_do_prazo_arrependimento"
        assert r["dias_excedidos"] == 1

    def test_fora_do_prazo_oferece_alternativas(self, agora):
        agora("2026-11-02")
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "arrependimento", TITULAR)
        assert r["elegivel"] is False
        assert r["alternativas"]  # não encerra sem caminho

    def test_condicoes_e_frete_citam_a_secao(self, agora):
        agora("2026-02-20")
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "arrependimento", TITULAR)
        assert any("embalagem original" in c for c in r["condicoes"])
        assert any("conta da loja" in p for p in r["proximos_passos"])
        assert r["citacoes"] == ["§4.1 Direito de Arrependimento (Compras Online)"]


class TestDefeito:
    """§4.2 (30 dias da compra) e §8.1 (90 dias de garantia legal)."""

    def test_dentro_dos_30_dias(self, agora):
        agora("2026-02-20")  # compra em 03/02
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "defeito", TITULAR)
        assert r["elegivel"] is True
        assert r["data_limite"] == "05/03/2026"

    def test_conta_da_data_da_compra_nao_do_recebimento(self, agora):
        agora("2026-03-01")
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "defeito", TITULAR)
        assert r["inicio_da_contagem"] == "03/02/2026"  # data do pedido

    def test_passado_30_dias_encaminha_para_garantia(self, agora):
        agora("2026-04-10")
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "defeito", TITULAR)
        assert r["veredito"] == "fora_do_prazo_troca_direta"
        assert r["garantia_legal"]["dentro_do_prazo"] is True
        assert "nao_coberto_pela_garantia" in r

    def test_garantia_legal_de_90_dias_vence(self, agora):
        agora("2026-11-02")
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "defeito", TITULAR)
        assert r["garantia_legal"]["data_limite"] == "18/05/2026"
        assert r["garantia_legal"]["dentro_do_prazo"] is False


class TestTrocaPorPreferencia:
    """§4.3 — 7 dias, condicionada à disponibilidade do item desejado."""

    def test_dentro_do_prazo_condiciona_a_estoque(self, agora):
        agora("2026-02-20")
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "preferencia", TITULAR)
        assert r["elegivel"] is True
        assert any("disponibilidade" in c or "estoque" in c for c in r["condicoes"])


class TestStatusQueMudaOVeredito:
    def test_cancelado_nao_tem_o_que_devolver(self, agora):
        agora("2026-03-20")
        r = verificar_elegibilidade_devolucao(17, "arrependimento", "Matheus Ramos Correia")
        assert r["veredito"] == "pedido_cancelado"
        assert r["elegivel"] is False

    def test_pendente_escala_em_vez_de_prometer(self, agora):
        """O manual não cobre cancelamento pré-despacho; §10 manda escalar."""
        agora("2026-03-20")
        r = verificar_elegibilidade_devolucao(13, "arrependimento", "Fernanda Ribeiro Souza")
        assert r["veredito"] == "ainda_nao_entregue"
        assert r["elegivel"] is None
        assert any("§10" in c for c in r["citacoes"])

    def test_em_transporte_explica_que_o_prazo_nao_comecou(self, agora):
        agora("2026-03-20")
        r = verificar_elegibilidade_devolucao(8, "arrependimento", "Ana Carolina Ferreira")
        assert r["veredito"] == "em_transporte"
        assert r["elegivel"] is None


class TestRestricoesDoParagrafo44:
    def test_item_de_liquidacao_sinaliza_conferencia_humana(self, agora):
        """§4.4 exige aviso explícito de 'venda final', que a base não registra."""
        agora("2026-02-20")
        r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "arrependimento", TITULAR)
        assert "restricoes_a_verificar" in r
        assert any("liquidação" in a.lower() for a in r["restricoes_a_verificar"])


def test_motivo_invalido_gera_erro():
    with pytest.raises(ToolError):
        verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "porque_quero", TITULAR)


def test_ressalva_sobre_data_estimada_sempre_presente(agora):
    """Transparência: a base só tem entrega estimada, não a real."""
    agora("2026-02-20")
    r = verificar_elegibilidade_devolucao(PEDIDO_ENTREGUE, "arrependimento", TITULAR)
    assert "estimada" in r["ressalva_sobre_a_data"]
