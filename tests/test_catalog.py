"""Busca no catálogo e diagnóstico de resultado vazio."""

from __future__ import annotations

import pytest

from emporio.tools.catalog import (
    buscar_produtos,
    detalhar_produto,
    listar_categorias,
    promocoes_ativas,
)
from emporio.tools.common import ToolError


class TestBuscaBasica:
    def test_cenario_do_enunciado_violoes_ate_1000(self):
        """'Quais opções de violões disponíveis custando até R$1000?'"""
        resultado = buscar_produtos(categoria="Violões", preco_max=1000, limite=10)
        assert resultado["total_encontrado"] > 0
        for produto in resultado["produtos"]:
            assert produto["categoria"] == "Violões"
            assert produto["disponibilidade"] == "in_stock"
            preco = produto.get("preco_promocional") or produto["preco_tabela"]
            assert preco <= 1000

    def test_ordena_do_mais_barato_para_o_mais_caro(self):
        produtos = buscar_produtos(categoria="Violões", limite=10)["produtos"]
        precos = [p.get("preco_promocional") or p["preco_tabela"] for p in produtos]
        assert precos == sorted(precos)

    def test_filtra_por_marca(self):
        resultado = buscar_produtos(marca="Yamaha", limite=10)
        assert resultado["total_encontrado"] > 0
        assert all(p["marca"] == "Yamaha" for p in resultado["produtos"])

    def test_termo_livre_casa_especificacao(self):
        """Specs entram no índice: 'tampo sólido' tem de ser buscável."""
        resultado = buscar_produtos(termo="solido", limite=10)
        assert resultado["total_encontrado"] > 0

    def test_termos_combinam_em_and_nao_em_or(self):
        """'violao nylon' não deve trazer tudo que é violão OU nylon.

        O contrato é: todo resultado contém TODOS os termos no seu texto
        buscável. A categoria não é o invariante — um ukulele barítono de
        cordas de nylon cuja descrição compara o instrumento a um violão casa
        legitimamente, e é um resultado útil para quem perguntou.
        """
        from emporio.data.db import connect

        resultado = buscar_produtos(termo="violao nylon", limite=10)
        assert resultado["total_encontrado"] > 0
        ids = [p["product_id"] for p in resultado["produtos"]]
        conn = connect(read_only=True)
        try:
            for product_id in ids:
                blob = conn.execute(
                    "SELECT search_blob FROM products WHERE product_id = ?",
                    (product_id,),
                ).fetchone()["search_blob"]
                assert "violao" in blob and "nylon" in blob, product_id
        finally:
            conn.close()
        # E a esmagadora maioria deve, ainda assim, ser de violões.
        violoes = sum(1 for p in resultado["produtos"] if p["categoria"] == "Violões")
        assert violoes >= len(resultado["produtos"]) - 1

    def test_limite_respeitado_e_limitado(self):
        assert len(buscar_produtos(limite=3)["produtos"]) <= 3
        assert len(buscar_produtos(limite=999)["produtos"]) <= 10

    def test_esconde_indisponivel_por_padrao(self):
        produtos = buscar_produtos(limite=10, apenas_disponiveis=True)["produtos"]
        assert all(p["disponibilidade"] == "in_stock" for p in produtos)


class TestDiagnosticoDeVazio:
    """Um 'nenhum resultado' seco é o que faz o modelo improvisar."""

    def test_categoria_cadastrada_mas_sem_itens(self):
        resultado = buscar_produtos(categoria="Instrumentos de Sopro (Madeiras)")
        assert resultado["total_encontrado"] == 0
        assert resultado["motivo"] == "categoria_sem_produtos"
        assert "não há nenhum produto" in resultado["explicacao"]

    def test_teto_de_preco_baixo_demais_informa_o_minimo(self):
        resultado = buscar_produtos(categoria="Guitarras", preco_max=1000)
        assert resultado["motivo"] == "fora_da_faixa_de_preco"
        assert resultado["menor_preco_disponivel"] == 5400.0

    def test_item_existe_mas_esta_sem_estoque(self):
        """Produto 96 (Giannini GF-3D) está ativo com estoque zero."""
        resultado = buscar_produtos(termo="Giannini GF-3D")
        assert resultado["motivo"] == "encontrado_mas_indisponivel"
        assert resultado["produtos_indisponiveis"][0]["product_id"] == 96

    def test_termo_sem_correspondencia(self):
        resultado = buscar_produtos(termo="gaita de boca eletronica")
        assert resultado["total_encontrado"] == 0
        assert resultado["motivo"] == "sem_correspondencia"

    def test_categoria_inexistente_lista_as_validas(self):
        with pytest.raises(ToolError) as exc:
            buscar_produtos(categoria="Instrumentos de Cozinha")
        assert "Violões" in exc.value.suggestion


class TestCategorias:
    def test_lista_as_nove_categorias(self):
        resultado = listar_categorias()
        assert len(resultado["categorias"]) == 9

    def test_sinaliza_as_tres_categorias_vazias(self):
        resultado = listar_categorias()
        vazias = resultado["categorias_sem_itens"]
        assert len(vazias) == 3
        assert "Cordas Orquestrais" in vazias
        assert "Não ofereça" in next(
            c["observacao"] for c in resultado["categorias"] if c["nome"] in vazias
        )

    def test_categoria_com_itens_traz_faixa_de_preco(self):
        violoes = next(
            c for c in listar_categorias()["categorias"] if c["nome"] == "Violões"
        )
        assert violoes["produtos_disponiveis"] > 0
        assert "R$" in violoes["faixa_de_preco"]


class TestDetalhamento:
    def test_cenario_do_enunciado_takamine_gd20(self):
        """'Quanto custa o Takamine GD20?'"""
        resultado = detalhar_produto(nome="Takamine GD20")
        produto = resultado["produto"]
        assert produto["product_id"] == 95
        assert produto["preco_tabela"] == 2199.0
        # A promoção de 12% cadastrada está inativa e não pode aparecer.
        assert produto["promocao_ativa"] is False

    def test_por_id_traz_especificacoes(self):
        produto = detalhar_produto(product_id=95)["produto"]
        assert produto["especificacoes"]["top"] == "Cedro Sólido"

    def test_nome_ambiguo_devolve_candidatos_em_vez_de_escolher(self):
        """Chutar o modelo errado gera preço errado."""
        resultado = detalhar_produto(nome="Takamine")
        assert resultado["produto"] is None
        assert resultado["motivo"] == "ambiguo"
        assert len(resultado["candidatos"]) > 1

    def test_produto_inexistente_nao_inventa(self):
        resultado = detalhar_produto(nome="Fender Stratocaster Mexicana")
        assert resultado["produto"] is None
        assert resultado["motivo"] == "nao_encontrado"
        assert "Não invente" in resultado["explicacao"]

    def test_sem_parametros_gera_erro(self):
        with pytest.raises(ToolError):
            detalhar_produto()

    def test_produto_descontinuado_e_rotulado(self):
        """Produto 136 é descontinuado apesar de ter 13 unidades."""
        produto = detalhar_produto(product_id=136)["produto"]
        assert produto["disponibilidade"] == "discontinued"
        assert "fora de catálogo" in produto["disponibilidade_descricao"]

    def test_lancamento_futuro_e_rotulado(self):
        produto = detalhar_produto(product_id=130)["produto"]
        assert produto["disponibilidade"] == "coming_soon"


class TestPromocoes:
    def test_retorna_somente_as_quatro_ativas(self):
        resultado = promocoes_ativas()
        assert resultado["total"] == 4
        assert all(p["promocao_ativa"] for p in resultado["promocoes"])

    def test_ordena_pelo_maior_desconto(self):
        descontos = [p["desconto_percentual"] for p in promocoes_ativas()["promocoes"]]
        assert descontos == sorted(descontos, reverse=True)

    def test_traz_preco_original_e_percentual(self):
        """§6.2 — transparência na apresentação do preço promocional."""
        promo = promocoes_ativas()["promocoes"][0]
        assert promo["preco_tabela"] > promo["preco_promocional"]
        assert promo["desconto_percentual"] > 0
        assert "aviso_apresentacao" in promo

    def test_declara_as_regras_de_promocao(self):
        regras = " ".join(promocoes_ativas()["regras"])
        assert "não são cumulativas" in regras
        assert "PIX" in regras


def test_produto_com_promocoes_duplicadas_aplica_apenas_uma():
    """Produtos 93, 96 e 99 têm duas promoções cadastradas, todas inativas."""
    for product_id in (93, 96, 99):
        produto = detalhar_produto(product_id=product_id)["produto"]
        assert produto["promocao_ativa"] is False
