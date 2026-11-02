"""Laço de function calling, exercitado com um dublê de LLM.

O que se testa aqui não é o modelo — é o **orquestrador**: encadeamento de
ferramentas, coerção de argumentos, tolerância a chamada inválida, teto de
iterações e poda de histórico. Tudo determinístico, sem rede e sem chave.
"""

from __future__ import annotations

from emporio.agent.loop import Agent, _coerce_arguments, _trim_history
from emporio.llm.base import LLMResponse, Message, ToolCall, Usage
from emporio.tools.registry import get_tool


class TestFluxoBasico:
    def test_responde_sem_ferramenta_quando_nao_precisa(self, fake_llm):
        fake_llm.scripted = [FakeTexto("Oi! Como posso ajudar?")]
        turn = Agent(fake_llm).send("oi")
        assert turn.reply == "Oi! Como posso ajudar?"
        assert turn.tool_invocations == []
        assert turn.iterations == 1

    def test_chama_ferramenta_e_depois_responde(self, fake_llm):
        fake_llm.scripted = [
            fake_llm.chama("buscar_produtos", categoria="Violões", preco_max=1000),
            FakeTexto("Tenho estas opções…"),
        ]
        turn = Agent(fake_llm).send("violões até mil reais")
        assert len(turn.tool_invocations) == 1
        assert turn.tool_invocations[0].name == "buscar_produtos"
        assert turn.tool_invocations[0].ok is True
        assert turn.iterations == 2
        assert turn.reply == "Tenho estas opções…"

    def test_varias_ferramentas_no_mesmo_passo(self, fake_llm):
        fake_llm.scripted = [
            fake_llm.chama_varias(
                ("calcular_preco", {"product_id": 95}),
                ("calcular_frete", {"cidade": "Dourados"}),
            ),
            FakeTexto("O preço é X e o frete sai por cotação."),
        ]
        turn = Agent(fake_llm).send("quanto custa com frete pra Dourados?")
        assert [i.name for i in turn.tool_invocations] == [
            "calcular_preco",
            "calcular_frete",
        ]

    def test_encadeia_consultas_em_passos_distintos(self, fake_llm):
        fake_llm.scripted = [
            fake_llm.chama("detalhar_produto", nome="Takamine GD20"),
            fake_llm.chama("calcular_preco", product_id=95, forma_pagamento="pix"),
            FakeTexto("Sai por R$ 2.089,05 no PIX."),
        ]
        turn = Agent(fake_llm).send("quanto fica o Takamine no pix?")
        assert turn.iterations == 3
        assert len(turn.tool_invocations) == 2

    def test_soma_tokens_de_todas_as_iteracoes(self, fake_llm):
        fake_llm.scripted = [
            fake_llm.chama("listar_categorias"),
            FakeTexto("pronto"),
        ]
        turn = Agent(fake_llm).send("o que vocês vendem?")
        assert turn.usage.total == 30  # 2 iterações de 15 tokens


class TestResiliencia:
    def test_ferramenta_inexistente_volta_como_dado(self, fake_llm):
        """Erro de ferramenta não derruba o turno: volta ao modelo."""
        fake_llm.scripted = [
            fake_llm.chama("consultar_estoque_mundial", termo="x"),
            FakeTexto("Deixa eu verificar de outro jeito."),
        ]
        turn = Agent(fake_llm).send("teste")
        assert turn.tool_invocations[0].ok is False
        assert "não existe" in turn.tool_invocations[0].result["erro"]
        assert "ferramentas_disponiveis" in turn.tool_invocations[0].result
        assert turn.reply  # a conversa segue

    def test_argumento_obrigatorio_ausente_devolve_o_schema(self, fake_llm):
        fake_llm.scripted = [
            fake_llm.chama("calcular_preco"),  # falta product_id
            FakeTexto("De qual produto?"),
        ]
        turn = Agent(fake_llm).send("quanto custa?")
        resultado = turn.tool_invocations[0].result
        assert turn.tool_invocations[0].ok is False
        assert "parametros_esperados" in resultado

    def test_erro_de_ferramenta_vira_payload_amigavel(self, fake_llm):
        fake_llm.scripted = [
            fake_llm.chama("calcular_preco", product_id=999999),
            FakeTexto("Não encontrei esse produto."),
        ]
        turn = Agent(fake_llm).send("preço do produto 999999")
        assert "erro" in turn.tool_invocations[0].result

    def test_teto_de_iteracoes_forca_resposta_final(self, fake_llm):
        """Em vez de devolver vazio, fecha o turno com o que apurou.

        Roteiro: o modelo insiste em consultar nas 3 iterações permitidas; a
        4ª resposta é a que o laço arranca sem oferecer ferramentas.
        """
        fake_llm.scripted = [fake_llm.chama("listar_categorias") for _ in range(3)]
        fake_llm.scripted.append(FakeTexto("Resumindo o que encontrei…"))
        turn = Agent(fake_llm, max_iterations=3).send("teste")
        assert turn.truncated is True
        assert turn.iterations == 3
        assert turn.reply == "Resumindo o que encontrei…"
        assert len(turn.tool_invocations) == 3

    def test_teto_atingido_sem_resposta_util_ainda_fala_algo(self, fake_llm):
        """Pior caso: nem a chamada final devolve texto."""
        fake_llm.scripted = [fake_llm.chama("listar_categorias") for _ in range(6)]
        turn = Agent(fake_llm, max_iterations=2).send("teste")
        assert turn.truncated is True
        assert turn.reply  # fallback, nunca vazio

    def test_resposta_vazia_tem_fallback(self, fake_llm):
        fake_llm.scripted = [LLMResponse(text="", usage=Usage(1, 1))]
        turn = Agent(fake_llm).send("???")
        assert turn.reply  # nunca devolve string vazia ao cliente


class TestCoercaoDeArgumentos:
    """Modelos menores mandam tipo errado; o laço corrige o corrigível."""

    def test_numero_em_string_vira_numero(self):
        spec = get_tool("buscar_produtos")
        limpo = _coerce_arguments(spec, {"preco_max": "1000", "limite": "3"})
        assert limpo["preco_max"] == 1000.0
        assert limpo["limite"] == 3

    def test_formato_brasileiro_de_valor(self):
        spec = get_tool("buscar_produtos")
        assert _coerce_arguments(spec, {"preco_max": "1.500,50"})["preco_max"] == 1500.5

    def test_booleano_em_string(self):
        spec = get_tool("buscar_produtos")
        assert _coerce_arguments(spec, {"apenas_disponiveis": "false"})[
            "apenas_disponiveis"
        ] is False
        assert _coerce_arguments(spec, {"apenas_disponiveis": "sim"})[
            "apenas_disponiveis"
        ] is True

    def test_parametro_alucinado_e_descartado(self):
        spec = get_tool("buscar_produtos")
        limpo = _coerce_arguments(spec, {"termo": "violao", "cor_favorita": "azul"})
        assert limpo == {"termo": "violao"}

    def test_nulos_textuais_sao_descartados(self):
        spec = get_tool("buscar_produtos")
        limpo = _coerce_arguments(
            spec, {"termo": "violao", "categoria": "null", "marca": "", "preco_max": None}
        )
        assert limpo == {"termo": "violao"}

    def test_float_onde_se_espera_inteiro(self):
        spec = get_tool("calcular_preco")
        assert _coerce_arguments(spec, {"product_id": 95.0})["product_id"] == 95

    def test_coercao_e_aplicada_no_laco(self, fake_llm):
        fake_llm.scripted = [
            fake_llm.chama("buscar_produtos", categoria="Violões", preco_max="1000"),
            FakeTexto("ok"),
        ]
        turn = Agent(fake_llm).send("violões até mil")
        assert turn.tool_invocations[0].arguments["preco_max"] == 1000.0
        assert turn.tool_invocations[0].ok is True


class TestPodaDeHistorico:
    def test_historico_curto_e_preservado(self):
        mensagens = [Message(role="user", content=f"m{i}") for i in range(5)]
        assert _trim_history(mensagens) == mensagens

    def test_nunca_comeca_com_resultado_de_ferramenta(self):
        """Todo provedor rejeita histórico que abre com tool_result solto."""
        mensagens: list[Message] = []
        for i in range(20):
            mensagens.append(Message(role="user", content=f"pergunta {i}"))
            mensagens.append(
                Message(
                    role="assistant",
                    tool_calls=[ToolCall(id=f"t{i}", name="listar_categorias")],
                )
            )
            mensagens.append(
                Message(role="tool", content="{}", tool_call_id=f"t{i}", tool_name="x")
            )
        podado = _trim_history(mensagens)
        assert len(podado) <= 24
        assert podado[0].role == "user"


class TestContextoDoPrompt:
    def test_prompt_do_sistema_vai_em_toda_chamada(self, fake_llm):
        fake_llm.scripted = [FakeTexto("oi")]
        Agent(fake_llm).send("oi")
        system = fake_llm.calls[0]["system"]
        assert "Téo" in system
        assert "Nunca informe preço" in system

    def test_todas_as_ferramentas_sao_oferecidas(self, fake_llm):
        fake_llm.scripted = [FakeTexto("oi")]
        Agent(fake_llm).send("oi")
        assert len(fake_llm.calls[0]["tools"]) == 11

    def test_data_congelada_aparece_no_prompt(self, fake_llm, agora):
        agora("2026-03-22T15:00")  # domingo
        fake_llm.scripted = [FakeTexto("oi")]
        Agent(fake_llm).send("vocês estão abertos?")
        system = fake_llm.calls[0]["system"]
        assert "22/03/2026" in system
        assert "FECHADA" in system

    def test_resposta_final_forcada_nao_oferece_ferramentas(self, fake_llm):
        fake_llm.scripted = [fake_llm.chama("listar_categorias") for _ in range(3)]
        fake_llm.scripted.append(FakeTexto("fechando"))
        Agent(fake_llm, max_iterations=2).send("teste")
        assert fake_llm.calls[-1]["tools"] == []


class TestRegistroDoTurno:
    def test_mensagens_adicionadas_sao_rastreadas(self, fake_llm):
        """A persistência depende desta lista, não de contar o histórico."""
        fake_llm.scripted = [
            fake_llm.chama("listar_categorias"),
            FakeTexto("pronto"),
        ]
        turn = Agent(fake_llm).send("o que vendem?")
        papeis = [m.role for m in turn.messages_added]
        assert papeis == ["user", "assistant", "tool", "assistant"]

    def test_duracao_e_medida(self, fake_llm):
        fake_llm.scripted = [FakeTexto("oi")]
        assert Agent(fake_llm).send("oi").duration_ms >= 0


def FakeTexto(conteudo: str) -> LLMResponse:
    """Atalho local: resposta de texto puro com uso de tokens fixo."""
    return LLMResponse(text=conteudo, usage=Usage(10, 5))
