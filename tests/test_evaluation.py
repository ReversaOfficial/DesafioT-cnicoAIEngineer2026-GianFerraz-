"""Harness de avaliação.

O teste central aqui é a distinção entre *caso reprovado* e *caso não
executado*. Sem ela, um dia de cota esgotada no provedor produziria um
relatório indistinguível de um agente quebrado — e foi exatamente o que
aconteceu na primeira execução real, o que motivou esta separação.
"""

from __future__ import annotations

from emporio.agent.service import AgentService
from emporio.evaluation import (
    CaseResult,
    EvalReport,
    _check,
    _e_erro_de_cota,
    load_cases,
    run_eval,
    write_markdown,
)


class TestConjuntoDeCasos:
    def test_casos_carregam_e_tem_campos_obrigatorios(self):
        casos = load_cases()
        assert len(casos) >= 20
        for caso in casos:
            assert caso["id"]
            assert caso["pergunta"]
            assert caso["eixo"]
            assert caso["cenario"], f"{caso['id']} sem explicação de cenário"

    def test_ids_sao_unicos(self):
        ids = [c["id"] for c in load_cases()]
        assert len(ids) == len(set(ids))

    def test_metade_dos_casos_tem_verificacao_negativa(self):
        """É onde um agente de atendimento causa prejuízo de verdade."""
        casos = load_cases()
        negativos = [c for c in casos if c.get("nao_deve_conter")]
        assert len(negativos) >= len(casos) / 2

    def test_ferramentas_citadas_existem(self):
        from emporio.tools.registry import TOOLS_BY_NAME

        for caso in load_cases():
            esperadas = caso.get("ferramentas_esperadas", []) + caso.get(
                "ferramentas_esperadas_algum", []
            )
            for nome in esperadas:
                assert nome in TOOLS_BY_NAME, f"{caso['id']} cita `{nome}`, que não existe"


class TestVerificacoes:
    def test_exige_consulta(self):
        falhas = _check({"exige_consulta": True}, "resposta qualquer", [])
        assert any("sem consultar" in f for f in falhas)

    def test_ferramenta_esperada_ausente(self):
        falhas = _check({"ferramentas_esperadas": ["calcular_preco"]}, "ok", ["buscar_produtos"])
        assert any("calcular_preco" in f for f in falhas)

    def test_alternativa_satisfeita_por_qualquer_uma(self):
        caso = {"ferramentas_esperadas_algum": ["a", "b"]}
        assert _check(caso, "ok", ["b"]) == []
        assert _check(caso, "ok", ["c"]) != []

    def test_minimo_de_ferramentas(self):
        falhas = _check({"minimo_de_ferramentas": 2}, "ok", ["uma"])
        assert any("esperado ao menos 2" in f for f in falhas)

    def test_conteudo_exigido_casa_sem_acento_e_sem_caixa(self):
        """As verificações casam por fato, não por grafia."""
        assert _check({"deve_conter_algum": ["promoção"]}, "a PROMOCAO acabou", []) == []

    def test_conteudo_proibido_e_detectado(self):
        falhas = _check({"nao_deve_conter": ["12%"]}, "tem 12% de desconto", [])
        assert any("12%" in f for f in falhas)

    def test_caso_sem_verificacao_de_conteudo_passa(self):
        assert _check({"exige_consulta": False}, "qualquer coisa", []) == []


class TestDeteccaoDeCota:
    def test_reconhece_erro_de_cota(self):
        for mensagem in (
            "gemini: limite de requisições atingido (HTTP 429)",
            "You exceeded your current quota",
            "HTTP 429 RESOURCE_EXHAUSTED",
        ):
            assert _e_erro_de_cota(mensagem)

    def test_nao_confunde_outros_erros(self):
        assert not _e_erro_de_cota("modelo não encontrado (HTTP 404)")
        assert not _e_erro_de_cota("TypeError: argumento ausente")


class TestRelatorio:
    def _report(self) -> EvalReport:
        return EvalReport(
            provedor="fake",
            modelo="fake-1",
            resultados=[
                CaseResult("ok-1", "catalogo", "p", "r", ["buscar_produtos"]),
                CaseResult("ruim-1", "catalogo", "p", "r", [], falhas=["não chamou X"]),
                CaseResult("pulado-1", "escopo", "p", "", erro="cota esgotada"),
            ],
        )

    def test_score_ignora_casos_nao_executados(self):
        """1 de 2 executados = 50%, e não 1 de 3 = 33%."""
        report = self._report()
        assert report.total == 3
        assert report.executados == 2
        assert report.aprovados == 1
        assert report.score == 0.5

    def test_caso_nao_executado_nao_conta_como_reprovado(self):
        pulado = self._report().resultados[2]
        assert pulado.executado is False
        assert pulado.passou is False  # não passou, mas também não foi medido

    def test_por_eixo_exclui_nao_executados(self):
        por_eixo = self._report().por_eixo()
        assert por_eixo["catalogo"] == (1, 2)
        assert "escopo" not in por_eixo  # só tinha o caso pulado

    def test_dicionario_expoe_a_distincao(self):
        payload = self._report().to_dict()
        assert payload["executados"] == 2
        assert payload["total_de_casos"] == 3
        assert payload["nao_executados"] == ["pulado-1"]

    def test_markdown_separa_as_secoes(self, tmp_path):
        destino = write_markdown(self._report(), tmp_path / "EVAL.md")
        texto = destino.read_text(encoding="utf-8")
        assert "Casos não executados" in texto
        assert "não entram no score" in texto
        assert "⏭️ não executado" in texto
        assert "Detalhe dos casos reprovados" in texto


class TestExecucaoComDuble:
    def test_roda_os_casos_com_llm_falso(self, fake_llm):
        casos = [
            {
                "id": "t1",
                "eixo": "teste",
                "cenario": "x",
                "pergunta": "oi",
                "exige_consulta": False,
                "deve_conter_algum": ["ola"],
            }
        ]
        fake_llm.scripted = [fake_llm.texto("ola, tudo bem?")]
        report = run_eval(
            cases=casos, service=AgentService(client=fake_llm, persist=False)
        )
        assert report.aprovados == 1
        assert report.score == 1.0

    def test_sessao_limpa_por_caso(self, fake_llm):
        """Medimos a resposta à pergunta, não o resíduo da conversa anterior."""
        casos = [
            {"id": f"t{i}", "eixo": "t", "cenario": "x", "pergunta": "oi"}
            for i in range(3)
        ]
        fake_llm.scripted = [fake_llm.texto("ola") for _ in range(3)]
        run_eval(cases=casos, service=AgentService(client=fake_llm, persist=False))
        # Cada chamada deve começar com exatamente uma mensagem de usuário.
        for chamada in fake_llm.calls:
            assert len([m for m in chamada["messages"] if m.role == "user"]) == 1

    def test_filtro_por_eixo(self, fake_llm):
        casos = [
            {"id": "a", "eixo": "catalogo", "cenario": "x", "pergunta": "p"},
            {"id": "b", "eixo": "escopo", "cenario": "x", "pergunta": "p"},
        ]
        fake_llm.scripted = [fake_llm.texto("ok") for _ in range(2)]
        report = run_eval(
            cases=casos,
            filtro="escopo",
            service=AgentService(client=fake_llm, persist=False),
        )
        assert report.total == 1
        assert report.resultados[0].case_id == "b"

    def test_data_de_referencia_do_caso_e_restaurada(self, fake_llm, monkeypatch):
        """Um caso com data fixa não pode contaminar os seguintes."""
        import os

        monkeypatch.setenv("EMPORIO_NOW", "2026-01-01")
        casos = [
            {
                "id": "com-data",
                "eixo": "t",
                "cenario": "x",
                "pergunta": "p",
                "data_de_referencia": "2026-03-22T15:00",
            },
            {"id": "sem-data", "eixo": "t", "cenario": "x", "pergunta": "p"},
        ]
        fake_llm.scripted = [fake_llm.texto("ok") for _ in range(2)]
        run_eval(cases=casos, service=AgentService(client=fake_llm, persist=False))
        assert os.environ["EMPORIO_NOW"] == "2026-01-01"
