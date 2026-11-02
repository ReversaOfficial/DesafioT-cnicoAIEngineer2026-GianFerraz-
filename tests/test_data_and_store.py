"""ETL, campos derivados, auditoria de qualidade, frete e horário."""

from __future__ import annotations

from datetime import datetime

from emporio.config import STORE_TIMEZONE
from emporio.data.db import connect
from emporio.data.loader import (
    _derive_availability,
    _extract_brand,
    _to_float,
    build_database,
)
from emporio.text_utils import brl
from emporio.tools.shipping import (
    FLAT_FEE_BELOW_THRESHOLD,
    FREE_SHIPPING_THRESHOLD,
    calcular_frete,
)
from emporio.tools.store import informacoes_da_loja, is_open_at, next_opening


class TestParsingDoEtl:
    def test_formato_brasileiro_de_numero(self):
        assert _to_float("1.234,56") == 1234.56
        assert _to_float("R$ 429,90") == 429.90
        assert _to_float("429.90") == 429.90
        assert _to_float("") == 0.0
        assert _to_float("abc") == 0.0

    def test_marca_de_token_unico(self):
        assert _extract_brand("Yamaha C40 Nylon Natural") == "Yamaha"

    def test_marca_composta(self):
        """Sem tratamento, 'Music Man' viraria marca 'Music'."""
        assert _extract_brand("Music Man Bass 1X Electric Bass") == "Music Man"

    def test_marca_de_nome_vazio(self):
        assert _extract_brand("") == ""


class TestDisponibilidadeDerivada:
    def test_ativo_com_estoque(self):
        assert _derive_availability("active", 5) == "in_stock"

    def test_ativo_sem_estoque(self):
        assert _derive_availability("active", 0) == "out_of_stock"

    def test_descontinuado_vence_o_estoque(self):
        """§7.3 manda informar que saiu do catálogo, mesmo com unidades."""
        assert _derive_availability("discontinued", 13) == "discontinued"

    def test_lancamento_futuro_vence_o_estoque(self):
        assert _derive_availability("coming_soon", 2) == "coming_soon"


class TestBancoCarregado:
    def test_volumetria(self):
        conn = connect(read_only=True)
        try:
            contagens = {
                tabela: conn.execute(f"SELECT COUNT(*) AS n FROM {tabela}").fetchone()["n"]
                for tabela in (
                    "categories",
                    "products",
                    "customers",
                    "orders",
                    "order_items",
                    "promotions",
                )
            }
        finally:
            conn.close()
        assert contagens == {
            "categories": 9,
            "products": 65,
            "customers": 50,
            "orders": 20,
            "order_items": 22,
            "promotions": 25,
        }

    def test_view_de_preco_ignora_promocao_inativa(self):
        conn = connect(read_only=True)
        try:
            ativas = conn.execute(
                "SELECT COUNT(*) AS n FROM v_product_pricing WHERE active_discount_percent > 0"
            ).fetchone()["n"]
        finally:
            conn.close()
        assert ativas == 4  # só 4 das 25 promoções estão ativas

    def test_view_aplica_o_maior_desconto_entre_os_ativos(self):
        """§6.2 — promoções não são cumulativas."""
        conn = connect(read_only=True)
        try:
            linhas = conn.execute(
                """
                SELECT v.product_id, v.active_discount_percent,
                       (SELECT COUNT(*) FROM promotions p
                         WHERE p.product_id = v.product_id AND p.is_active = 1) AS n_ativas
                  FROM v_product_pricing v
                 WHERE v.active_discount_percent > 0
                """
            ).fetchall()
        finally:
            conn.close()
        assert all(linha["n_ativas"] <= 1 for linha in linhas)

    def test_banco_abre_em_modo_somente_leitura(self):
        import sqlite3

        import pytest

        conn = connect(read_only=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM products")
        finally:
            conn.close()

    def test_flag_de_regiao_metropolitana(self):
        conn = connect(read_only=True)
        try:
            cg = conn.execute(
                "SELECT is_metro_cg FROM customers WHERE city_norm = 'campo grande' LIMIT 1"
            ).fetchone()["is_metro_cg"]
            fora = conn.execute(
                "SELECT is_metro_cg FROM customers WHERE city_norm = 'dourados' LIMIT 1"
            ).fetchone()["is_metro_cg"]
        finally:
            conn.close()
        assert cg == 1
        assert fora == 0


class TestAuditoria:
    def test_achados_esperados_desta_base(self, tmp_path):
        achados = build_database(db_path=tmp_path / "t.sqlite3", write_report=False)
        temas = {a.issue for a in achados}
        assert "Categorias cadastradas sem nenhum produto" in temas
        assert "Total do pedido difere da soma dos itens a preço de tabela" in temas
        assert "Produto com mais de uma promoção cadastrada" in temas
        assert "Produto descontinuado com estoque remanescente" in temas
        assert (
            "Produto marcado como lançamento futuro já consta em pedidos" in temas
        )

    def test_todo_achado_tem_decisao_registrada(self, tmp_path):
        """O ETL não conserta a origem em silêncio."""
        achados = build_database(db_path=tmp_path / "t.sqlite3", write_report=False)
        assert achados
        assert all(a.decision for a in achados)
        assert all(a.severity in {"warning", "info"} for a in achados)

    def test_etl_e_idempotente(self, tmp_path):
        destino = tmp_path / "t.sqlite3"
        primeiro = build_database(db_path=destino, write_report=False)
        segundo = build_database(db_path=destino, write_report=False)
        assert len(primeiro) == len(segundo)


class TestFrete:
    """§5.1 e §5.2."""

    def test_metropolitana_acima_do_minimo_e_gratis(self):
        r = calcular_frete("Campo Grande", FREE_SHIPPING_THRESHOLD + 1)
        assert r["valor_frete"] == 0.0

    def test_metropolitana_abaixo_do_minimo_tem_taxa_fixa(self):
        r = calcular_frete("Campo Grande", 400)
        assert r["valor_frete"] == FLAT_FEE_BELOW_THRESHOLD
        assert "Faltam" in r["dica"]

    def test_exatamente_500_ainda_paga_taxa(self):
        """A regra é 'acima de R$ 500,00', não 'a partir de'."""
        assert calcular_frete("Campo Grande", 500.0)["valor_frete"] == 35.0

    def test_sem_valor_do_pedido_explica_a_regra(self):
        r = calcular_frete("Campo Grande")
        assert r["valor_frete"] is None
        assert "sugestao" in r

    def test_fora_da_metropolitana_nao_estima_valor(self):
        """Estimar frete sem tabela seria inventar número (§7.1)."""
        r = calcular_frete("Dourados", 600)
        assert r["valor_frete"] is None
        assert len(r["modalidades"]) == 3
        assert "cotação" in r["regra_de_valor"] or "CEP" in r["regra_de_valor"]

    def test_cidade_do_interior_nao_entra_na_regra_metropolitana(self):
        for cidade in ("Três Lagoas", "Corumbá", "Ponta Porã"):
            assert calcular_frete(cidade, 900)["valor_frete"] is None

    def test_municipio_da_regiao_metropolitana_entra(self):
        assert calcular_frete("Sidrolândia", 900)["valor_frete"] == 0.0

    def test_instrumento_de_grande_porte_avisa_cotacao(self):
        """§5.2 — bateria pode exigir frete especial."""
        conn = connect(read_only=True)
        try:
            bateria = conn.execute(
                "SELECT product_id FROM products WHERE category_id = 3 LIMIT 1"
            ).fetchone()["product_id"]
        finally:
            conn.close()
        r = calcular_frete("Dourados", 5000, product_id=bateria)
        assert "aviso_grande_porte" in r

    def test_cidade_vazia_pede_a_informacao(self):
        assert "erro" in calcular_frete("")


class TestHorarioDeFuncionamento:
    """§2 — e a aritmética de fuso que o LLM não deve fazer."""

    def test_aberto_em_dia_util(self):
        assert is_open_at(datetime(2026, 3, 19, 10, 0, tzinfo=STORE_TIMEZONE))

    def test_fechado_antes_de_abrir(self):
        assert not is_open_at(datetime(2026, 3, 19, 8, 59, tzinfo=STORE_TIMEZONE))

    def test_fechado_no_instante_do_fechamento(self):
        assert not is_open_at(datetime(2026, 3, 19, 18, 0, tzinfo=STORE_TIMEZONE))

    def test_sabado_fecha_as_13(self):
        assert is_open_at(datetime(2026, 3, 21, 12, 59, tzinfo=STORE_TIMEZONE))
        assert not is_open_at(datetime(2026, 3, 21, 13, 0, tzinfo=STORE_TIMEZONE))

    def test_domingo_fechado(self):
        assert not is_open_at(datetime(2026, 3, 22, 11, 0, tzinfo=STORE_TIMEZONE))

    def test_proxima_abertura_pula_o_domingo(self):
        domingo = datetime(2026, 3, 22, 15, 0, tzinfo=STORE_TIMEZONE)
        reabre = next_opening(domingo)
        assert reabre.day == 23
        assert reabre.hour == 9

    def test_proxima_abertura_no_mesmo_dia_antes_de_abrir(self):
        cedo = datetime(2026, 3, 19, 7, 0, tzinfo=STORE_TIMEZONE)
        assert next_opening(cedo).hour == 9

    def test_sabado_a_tarde_reabre_na_segunda(self):
        sabado = datetime(2026, 3, 21, 16, 0, tzinfo=STORE_TIMEZONE)
        assert next_opening(sabado).day == 23


class TestInformacoesDaLoja:
    def test_dados_cadastrais(self):
        info = informacoes_da_loja()
        assert "Rua 14 de Maio, 3200" in info["endereco"]
        assert info["cidade"] == "Campo Grande/MS"
        assert info["cnpj"] == "12.345.678/0001-90"

    def test_declara_que_nao_vende_acessorios(self):
        escopo = informacoes_da_loja()["escopo"]
        for item in ("cordas", "palhetas", "amplificadores"):
            assert item in escopo

    def test_expoe_as_divergencias_do_manual(self):
        """Preferimos sinalizar a ambiguidade a escolher um lado em silêncio."""
        observacoes = " ".join(informacoes_da_loja()["observacoes_sobre_a_fonte"])
        assert "3341-4444" in observacoes
        assert "3321-4500" in observacoes

    def test_fora_do_expediente_informa_retorno(self, agora):
        agora("2026-03-22T15:00")  # domingo
        agent_view = informacoes_da_loja()["agora"]
        assert agent_view["aberto"] is False
        assert "segunda-feira" in agent_view["proxima_abertura"]
        assert "orientacao" in agent_view

    def test_dentro_do_expediente_informa_fechamento(self, agora):
        agora("2026-03-19T10:00")
        agent_view = informacoes_da_loja()["agora"]
        assert agent_view["aberto"] is True
        assert agent_view["fecha_hoje_as"] == "18:00"


class TestFormatacaoMonetaria:
    def test_padrao_brasileiro(self):
        assert brl(1234.5) == "R$ 1.234,50"
        assert brl(429.9) == "R$ 429,90"
        assert brl(0) == "R$ 0,00"
        assert brl(18900) == "R$ 18.900,00"
