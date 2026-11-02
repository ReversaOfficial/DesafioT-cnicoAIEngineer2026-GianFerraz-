"""Chunking e recuperação sobre o manual de políticas.

O teste de cobertura de seções é proposital: se uma revisão do PDF mudar a
estrutura, o build falha aqui em vez de o agente passar a responder política
pela metade.
"""

from __future__ import annotations

import pytest

from emporio.retrieval.bm25 import BM25Index, expand_query
from emporio.retrieval.chunker import OUTLINE
from emporio.text_utils import fold_preserving, normalize, tokenize


class TestCobertura:
    def test_todas_as_secoes_do_sumario_foram_encontradas(self, indice_politicas):
        """Nenhuma regra do manual pode ficar fora do índice."""
        indexadas = {c.section for c in indice_politicas.chunks}
        esperadas = {numero for numero, _ in OUTLINE}
        # §5, §6 e §8 só trazem o título antes da primeira subseção, então são
        # anexadas ao chunk seguinte em vez de virarem chunk próprio.
        apenas_titulo = {"5", "6", "8"}
        faltando = esperadas - indexadas - apenas_titulo
        assert not faltando, f"seções ausentes do índice: {sorted(faltando)}"

    def test_titulo_de_capitulo_sem_chunk_proprio_sobrevive_no_texto(
        self, indice_politicas
    ):
        """O texto do título do capítulo não pode ser perdido."""
        todo_o_texto = fold_preserving(
            " ".join(c.text for c in indice_politicas.chunks)
        )
        for titulo in ("politica de frete e entregas", "promocoes e descontos", "garantia"):
            assert titulo in todo_o_texto

    def test_capa_com_endereco_e_telefone_foi_indexada(self, indice_politicas):
        capa = indice_politicas.get_section("0")
        assert capa
        assert "14 de Maio" in capa[0].text

    def test_chunks_tem_tamanho_razoavel(self, indice_politicas):
        for chunk in indice_politicas.chunks:
            assert 100 <= len(chunk.text) <= 1400, chunk.citation

    def test_citacao_e_legivel(self, indice_politicas):
        chunk = indice_politicas.get_section("4.1")[0]
        assert chunk.citation == "§4.1 Direito de Arrependimento (Compras Online)"


class TestRecuperacao:
    """Perguntas reais de atendimento contra a seção que deveria responder."""

    CASOS = [
        ("Me arrependi da compra, posso devolver?", "4.1"),
        ("Qual o endereço da loja?", "1.2"),
        ("Que horas vocês abrem no sábado?", "2"),
        ("Abre domingo?", "2"),
        ("Posso parcelar em 10x?", "3.1"),
        ("Tem desconto no pix?", "3"),
        ("Quanto tempo leva pra chegar em Dourados?", "5.2"),
        ("Frete grátis aqui em Campo Grande?", "5.1"),
        ("Meu violão veio com defeito, tem garantia?", "4.2"),
        ("Vocês vendem cordas e palhetas?", "7.1"),
        ("Quero excluir meus dados", "9"),
        ("Como rastreio meu pedido?", "5.3"),
        ("A promoção de black friday tá valendo?", "6.1"),
        ("Promoção acumula com desconto do pix?", "6.2"),
        ("Quanto tempo de garantia tem?", "8.1"),
        ("O que a garantia não cobre?", "8.3"),
        ("Posso trocar porque não gostei da cor?", "4.3"),
    ]

    @pytest.mark.parametrize(("pergunta", "secao"), CASOS)
    def test_secao_correta_no_top3(self, indice_politicas, pergunta, secao):
        encontradas = [h.chunk.section for h in indice_politicas.search(pergunta, limit=3)]
        assert secao in encontradas, f"{pergunta!r} -> {encontradas}"

    def test_recall_arroba_1_como_guarda_de_regressao(self, indice_politicas):
        """Métrica mais exigente: a seção certa como *primeiro* resultado.

        Medido nesta base: 10/17 (59%). O limiar aqui é guarda de regressão,
        não meta — a métrica que importa para o agente é recall@3, porque a
        ferramenta entrega três trechos ao modelo.

        Os sete casos que não lideram erram todos para uma seção *vizinha do
        mesmo assunto*: "quanto tempo de garantia" traz §8.2 (garantia do
        fabricante) antes de §8.1 (garantia legal); "não gostei da cor" traz
        §4.4 (itens não elegíveis) antes de §4.3 (troca por preferência). Como
        o contexto vizinho é informação legítima para a resposta, trocar de
        ranqueador para corrigir isso seria otimizar a métrica errada.
        """
        acertos = 0
        for pergunta, secao in self.CASOS:
            hits = indice_politicas.search(pergunta, limit=1)
            if hits and hits[0].chunk.section == secao:
                acertos += 1
        assert acertos / len(self.CASOS) >= 0.55

    def test_pergunta_fora_do_manual_nao_forca_resultado(self, indice_politicas):
        hits = indice_politicas.search("qual a capital da Mongólia", limit=3)
        assert not hits or hits[0].score < 2.0

    def test_resultado_traz_secao_e_pagina_para_citacao(self, indice_politicas):
        hit = indice_politicas.search("prazo de devolução", limit=1)[0].to_dict()
        assert hit["secao"].startswith("§")
        assert isinstance(hit["pagina"], int)
        assert hit["texto"]


class TestExpansaoDeConsulta:
    def test_sinonimo_do_dominio_entra_na_consulta(self):
        tokens = expand_query("quero devolver")
        assert "arrependimento" in tokens
        assert "devolucao" in tokens

    def test_termo_original_vem_antes_do_sinonimo(self):
        tokens = expand_query("devolver")
        assert tokens.index("devolver") < tokens.index("arrependimento")

    def test_flexao_verbal_e_resolvida_sem_enumerar_conjugacao(self):
        """'chegar' não está no mapa; o stem mínimo resolve."""
        assert "prazo" in expand_query("quanto tempo pra chegar")

    def test_consulta_sem_sinonimo_fica_intacta(self):
        assert expand_query("violao") == ["violao"]


class TestBM25:
    def test_termo_raro_pesa_mais_que_termo_comum(self):
        docs = [
            ["violao", "nylon", "yamaha"],
            ["violao", "aco", "tagima"],
            ["violao", "aco", "giannini"],
            ["bateria", "pearl"],
        ]
        index = BM25Index(docs)
        assert index.idf["bateria"] > index.idf["violao"]

    def test_idf_nunca_negativo(self):
        """Termo presente em quase todo documento não pode empurrar para baixo."""
        docs = [["a", "b"], ["a", "c"], ["a", "d"]]
        assert all(v >= 0 for v in BM25Index(docs).idf.values())

    def test_documento_vazio_nao_quebra(self):
        assert BM25Index([]).score(["x"]) == []

    def test_retorna_termos_que_casaram(self):
        index = BM25Index([["violao", "nylon"], ["bateria"]])
        resultado = index.score(["violao"])
        assert resultado[0].matched_terms == ("violao",)


class TestNormalizacaoDeTexto:
    def test_remove_acento_e_minusculiza(self):
        assert normalize("Violão Elétrico") == "violao eletrico"

    def test_fold_preserva_comprimento(self):
        """Índices encontrados no texto dobrado têm de valer no original."""
        for texto in ("Violão", "Promoções e Descontos", "Missão", "ÁÉÍÓÚàçãõ"):
            assert len(fold_preserving(texto)) == len(texto)

    def test_tokenize_remove_stopword_mas_mantem_numero(self):
        tokens = tokenize("posso parcelar em 12 vezes?")
        assert "12" in tokens
        assert "em" not in tokens

    def test_tokenize_de_texto_vazio(self):
        assert tokenize("") == []
