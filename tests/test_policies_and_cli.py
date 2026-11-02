"""Ferramenta de políticas, parsing do PDF e comandos da CLI."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from emporio.cli import app
from emporio.config import settings
from emporio.retrieval.chunker import OUTLINE, chunk_policy_pdf, extract_pages
from emporio.tools.common import ToolError
from emporio.tools.policies import consultar_politicas, obter_secao_politica

runner = CliRunner()


class TestFerramentaDePoliticas:
    def test_devolve_trechos_com_secao_e_pagina(self):
        resultado = consultar_politicas("qual o prazo para devolver uma compra?")
        assert resultado["encontrado"] is True
        assert resultado["trechos"]
        primeiro = resultado["trechos"][0]
        assert primeiro["secao"].startswith("§")
        assert isinstance(primeiro["pagina"], int)
        assert "7" in " ".join(t["texto"] for t in resultado["trechos"])

    def test_instrucao_de_fidelidade_a_fonte_acompanha_o_resultado(self):
        resultado = consultar_politicas("formas de pagamento")
        assert "apenas com o que está nestes trechos" in resultado["instrucao"]

    def test_limite_respeitado_e_limitado(self):
        assert len(consultar_politicas("troca", limite=1)["trechos"]) == 1
        assert len(consultar_politicas("troca", limite=99)["trechos"]) <= 5

    def test_pergunta_vazia_gera_erro(self):
        with pytest.raises(ToolError):
            consultar_politicas("")

    def test_assunto_fora_do_manual_orienta_escalar(self):
        resultado = consultar_politicas("xilofone quântico interplanetário")
        if not resultado["encontrado"]:
            assert "verificar com a equipe" in resultado["explicacao"]

    def test_acesso_direto_a_secao(self):
        secao = obter_secao_politica("4.1")
        assert "Arrependimento" in secao["secao"]
        assert "7" in secao["texto"]

    def test_secao_com_prefixo_de_paragrafo(self):
        assert obter_secao_politica("§4.1")["secao"] == obter_secao_politica("4.1")["secao"]

    def test_secao_inexistente_lista_as_validas(self):
        with pytest.raises(ToolError) as exc:
            obter_secao_politica("99.9")
        assert "4.1" in exc.value.suggestion


class TestParsingDoPdf:
    """Reconstrói o índice do zero, exercitando o parser de verdade."""

    def test_extrai_oito_paginas(self):
        paginas = extract_pages(settings.policy_pdf_path)
        assert len(paginas) == 8
        assert all(isinstance(p, str) for p in paginas)

    def test_remove_cabecalho_e_rodape_repetidos(self):
        paginas = extract_pages(settings.policy_pdf_path)
        for numero, texto in enumerate(paginas[1:], start=2):
            assert "Manual de Políticas e Procedimentos" not in texto
            assert f"Página {numero}" not in texto

    def test_chunking_a_partir_do_pdf(self):
        chunks = chunk_policy_pdf(settings.policy_pdf_path)
        assert len(chunks) >= len(OUTLINE) - 3  # §5, §6 e §8 só têm título
        assert all(chunk.text.strip() for chunk in chunks)
        assert all(chunk.page >= 1 for chunk in chunks)

    def test_regras_numericas_sobrevivem_ao_chunking(self):
        """O risco do chunking cego: separar o número da regra que ele qualifica."""
        chunks = chunk_policy_pdf(settings.policy_pdf_path)
        por_secao = {c.section: " ".join(x.text for x in chunks if x.section == c.section)
                     for c in chunks}
        assert "7" in por_secao["4.1"]  # 7 dias de arrependimento
        assert "30" in por_secao["4.2"]  # 30 dias de troca por defeito
        assert "90" in por_secao["8.1"]  # 90 dias de garantia legal
        assert "500" in por_secao["5.1"]  # frete grátis acima de R$ 500
        assert "5%" in por_secao["3"] or "5" in por_secao["3"]  # desconto do PIX

    def test_ida_e_volta_do_indice_em_disco(self, tmp_path):
        from emporio.retrieval.index import PolicyIndex

        original = PolicyIndex.build()
        destino = tmp_path / "idx.json"
        original.save(destino)
        recarregado = PolicyIndex.load(destino)
        assert recarregado is not None
        assert len(recarregado.chunks) == len(original.chunks)
        assert recarregado.chunks[0].citation == original.chunks[0].citation

    def test_indice_de_formato_antigo_e_descartado(self, tmp_path):
        from emporio.retrieval.index import PolicyIndex

        destino = tmp_path / "velho.json"
        destino.write_text('{"format_version": 1, "chunks": []}', encoding="utf-8")
        assert PolicyIndex.load(destino) is None

    def test_indice_corrompido_e_descartado(self, tmp_path):
        from emporio.retrieval.index import PolicyIndex

        destino = tmp_path / "ruim.json"
        destino.write_text("{isso nao e json", encoding="utf-8")
        assert PolicyIndex.load(destino) is None

    def test_pdf_ausente_gera_erro_claro(self, tmp_path):
        from emporio.retrieval.index import PolicyIndex

        with pytest.raises(FileNotFoundError) as exc:
            PolicyIndex.build(tmp_path / "nao_existe.pdf")
        assert "políticas_da_loja.pdf" in str(exc.value)


class TestComandosDaCli:
    def test_version(self):
        resultado = runner.invoke(app, ["version"])
        assert resultado.exit_code == 0
        assert "emporio" in resultado.stdout

    def test_doctor_sem_chave_sai_com_codigo_1_e_orienta(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        resultado = runner.invoke(app, ["doctor"])
        assert resultado.exit_code == 1
        assert "pytest" in resultado.stdout  # aponta o que funciona sem chave

    def test_doctor_com_chave_sai_com_codigo_0(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "chave-de-teste")
        resultado = runner.invoke(app, ["doctor"])
        assert resultado.exit_code == 0

    def test_tool_executa_sem_llm(self):
        resultado = runner.invoke(
            app, ["tool", "buscar_produtos", "categoria=Violões", "preco_max=600", "limite=3"]
        )
        assert resultado.exit_code == 0
        assert "total_encontrado" in resultado.stdout

    def test_tool_inexistente_lista_as_validas(self):
        resultado = runner.invoke(app, ["tool", "ferramenta_que_nao_existe"])
        assert resultado.exit_code == 1
        assert "buscar_produtos" in resultado.stdout

    def test_tool_com_argumento_malformado(self):
        resultado = runner.invoke(app, ["tool", "buscar_produtos", "semigual"])
        assert resultado.exit_code == 1
        assert "chave=valor" in resultado.stdout

    def test_tool_propaga_erro_de_ferramenta_como_json(self):
        resultado = runner.invoke(app, ["tool", "calcular_preco", "product_id=999999"])
        assert resultado.exit_code == 0
        assert "erro" in resultado.stdout

    def test_chat_sem_chave_explica_o_que_fazer(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        resultado = runner.invoke(app, ["chat", "-m", "oi"])
        assert resultado.exit_code == 1
        assert "aistudio.google.com" in resultado.stdout

    def test_build_reconstroi_os_artefatos(self):
        resultado = runner.invoke(app, ["build"])
        assert resultado.exit_code == 0
        assert "Banco da operação" in resultado.stdout
        assert "Índice de políticas" in resultado.stdout
        assert settings.db_path.exists()
        assert settings.policy_index_path.exists()
