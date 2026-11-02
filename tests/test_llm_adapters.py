"""Tradução de formatos entre a interface neutra e cada provedor.

É a camada onde mais erro de integração se esconde, e dá para testar inteira
sem rede: a conversão de mensagens e o parsing de resposta são funções puras.
Cada teste fixa um detalhe de protocolo que, errado, custa um HTTP 400.
"""

from __future__ import annotations

import json

import pytest

from emporio.llm.anthropic import AnthropicClient
from emporio.llm.base import (
    LLMError,
    Message,
    ToolCall,
    friendly_http_error,
    parse_arguments,
)
from emporio.llm.gemini import GeminiClient
from emporio.llm.openai_compat import OpenAICompatClient
from emporio.tools.registry import tool_schemas

CONVERSA = [
    Message(role="user", content="quanto custa o Takamine GD20?"),
    Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="c1", name="detalhar_produto", arguments={"nome": "GD20"})],
    ),
    Message(
        role="tool",
        content='{"preco_tabela": 2199.0}',
        tool_call_id="c1",
        tool_name="detalhar_produto",
    ),
    Message(role="assistant", content="Sai por R$ 2.199,00."),
]


class TestGemini:
    def test_ferramenta_sem_argumento_omite_parameters(self):
        """O Gemini rejeita `parameters` com properties vazio em tipo OBJECT."""
        convertidas = GeminiClient._convert_tools(tool_schemas())
        declaracoes = convertidas[0]["functionDeclarations"]
        sem_args = next(d for d in declaracoes if d["name"] == "listar_categorias")
        assert "parameters" not in sem_args

    def test_ferramenta_com_argumento_declara_schema(self):
        declaracoes = GeminiClient._convert_tools(tool_schemas())[0]["functionDeclarations"]
        com_args = next(d for d in declaracoes if d["name"] == "calcular_preco")
        assert com_args["parameters"]["properties"]["product_id"]["type"] == "integer"
        assert com_args["parameters"]["required"] == ["product_id"]

    def test_required_vazio_e_omitido(self):
        declaracoes = GeminiClient._convert_tools(tool_schemas())[0]["functionDeclarations"]
        busca = next(d for d in declaracoes if d["name"] == "buscar_produtos")
        assert "required" not in busca["parameters"]

    def test_papeis_e_partes(self):
        contents = GeminiClient._convert_messages(CONVERSA)
        assert [c["role"] for c in contents] == ["user", "model", "user", "model"]
        # A chamada de ferramenta vira functionCall na vez do modelo.
        assert contents[1]["parts"][0]["functionCall"]["name"] == "detalhar_produto"
        # O resultado casa por NOME no Gemini, não por id.
        assert contents[2]["parts"][0]["functionResponse"]["name"] == "detalhar_produto"

    def test_assistente_sem_conteudo_nem_ferramenta_e_descartado(self):
        contents = GeminiClient._convert_messages([Message(role="assistant", content="")])
        assert contents == []

    def test_parsing_de_texto(self):
        resposta = GeminiClient._parse(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "Olá!"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 8},
            }
        )
        assert resposta.text == "Olá!"
        assert resposta.wants_tools is False
        assert resposta.usage.prompt_tokens == 120
        assert resposta.usage.total == 128

    def test_parsing_de_chamada_de_ferramenta(self):
        resposta = GeminiClient._parse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "buscar_produtos",
                                        "args": {"categoria": "Violões", "preco_max": 1000},
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        )
        assert resposta.wants_tools is True
        assert resposta.tool_calls[0].arguments["preco_max"] == 1000

    def test_thought_signature_e_preservada_no_parsing(self):
        """Modelos Gemini 3.x exigem a assinatura de volta no histórico."""
        resposta = GeminiClient._parse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "buscar_produtos",
                                        "args": {"termo": "violao"},
                                    },
                                    "thoughtSignature": "Ct4BAbc123==",
                                }
                            ]
                        }
                    }
                ]
            }
        )
        assert resposta.tool_calls[0].metadata["thoughtSignature"] == "Ct4BAbc123=="

    def test_thought_signature_volta_na_requisicao(self):
        """Sem devolver a assinatura, a API responde HTTP 400."""
        conversa = [
            Message(role="user", content="oi"),
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="buscar_produtos",
                        arguments={"termo": "violao"},
                        metadata={"thoughtSignature": "Ct4BAbc123=="},
                    )
                ],
            ),
        ]
        contents = GeminiClient._convert_messages(conversa)
        parte = contents[1]["parts"][0]
        assert parte["thoughtSignature"] == "Ct4BAbc123=="
        assert parte["functionCall"]["name"] == "buscar_produtos"

    def test_sem_assinatura_a_chave_nao_e_enviada(self):
        """Provedores que não usam assinatura não podem receber campo vazio."""
        conversa = [
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="c1", name="x", arguments={})],
            )
        ]
        parte = GeminiClient._convert_messages(conversa)[0]["parts"][0]
        assert "thoughtSignature" not in parte

    def test_resposta_bloqueada_vira_erro_explicito(self):
        """Sem candidatos normalmente significa filtro de segurança."""
        with pytest.raises(LLMError):
            GeminiClient._parse({"promptFeedback": {"blockReason": "SAFETY"}})

    def test_sem_chave_falha_na_construcao(self):
        with pytest.raises(LLMError) as exc:
            GeminiClient(api_key="", model="gemini-2.5-flash")
        assert "aistudio" in str(exc.value)


class TestFallbackDeModelo:
    """Cota do nível gratuito do Gemini é por modelo; trocar mantém de pé."""

    def _cliente(self, respostas: list) -> GeminiClient:
        """Cliente com `_post` substituído por um roteiro de resultados."""
        cliente = GeminiClient(
            api_key="k",
            model="principal",
            fallback_models=("reserva-1", "reserva-2"),
        )
        chamadas: list[str] = []

        def fake_post(url: str, payload: dict) -> dict:
            modelo = url.split("/models/")[1].split(":")[0]
            chamadas.append(modelo)
            resultado = respostas.pop(0)
            if isinstance(resultado, Exception):
                raise resultado
            return resultado

        cliente._post = fake_post  # type: ignore[method-assign]
        cliente.chamadas = chamadas  # type: ignore[attr-defined]
        return cliente

    _OK = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    def test_usa_o_principal_quando_ele_responde(self):
        cliente = self._cliente([self._OK])
        assert cliente.complete("s", [Message(role="user", content="oi")]).text == "ok"
        assert cliente.chamadas == ["principal"]
        assert cliente.model_switches == []

    def test_desce_para_a_reserva_quando_a_cota_esgota(self):
        cliente = self._cliente(
            [LLMError("gemini: limite de requisições atingido (HTTP 429)"), self._OK]
        )
        assert cliente.complete("s", [Message(role="user", content="oi")]).text == "ok"
        assert cliente.chamadas == ["principal", "reserva-1"]
        assert cliente.active_model == "reserva-1"
        assert cliente.model_switches[0][:2] == ("principal", "reserva-1")

    def test_desce_tambem_em_modelo_descontinuado(self):
        cliente = self._cliente([LLMError("gemini: modelo não encontrado (HTTP 404)"), self._OK])
        cliente.complete("s", [Message(role="user", content="oi")])
        assert cliente.active_model == "reserva-1"

    def test_desce_em_sobrecarga_do_modelo(self):
        cliente = self._cliente([LLMError("gemini: instabilidade no provedor (HTTP 503)"), self._OK])
        cliente.complete("s", [Message(role="user", content="oi")])
        assert cliente.active_model == "reserva-1"

    def test_credencial_invalida_nao_tenta_outro_modelo(self):
        """Chave ruim vale para todos; insistir só gastaria tempo."""
        cliente = self._cliente([LLMError("gemini: credencial rejeitada (HTTP 401)")])
        with pytest.raises(LLMError):
            cliente.complete("s", [Message(role="user", content="oi")])
        assert cliente.chamadas == ["principal"]

    def test_permanece_na_reserva_nas_chamadas_seguintes(self):
        """Não faz sentido voltar a bater num modelo cuja cota já acabou."""
        cliente = self._cliente(
            [LLMError("HTTP 429 limite de requisições"), self._OK, self._OK]
        )
        cliente.complete("s", [Message(role="user", content="oi")])
        cliente.complete("s", [Message(role="user", content="de novo")])
        assert cliente.chamadas == ["principal", "reserva-1", "reserva-1"]

    def test_todos_falhando_gera_erro_listando_as_tentativas(self):
        cliente = self._cliente([LLMError("HTTP 429")] * 3)
        with pytest.raises(LLMError) as exc:
            cliente.complete("s", [Message(role="user", content="oi")])
        assert "principal" in str(exc.value)
        assert "reserva-2" in str(exc.value)

    def test_sem_reservas_configuradas_propaga_o_erro(self):
        cliente = GeminiClient(api_key="k", model="unico", fallback_models=())
        cliente._post = lambda url, payload: (_ for _ in ()).throw(  # type: ignore[method-assign]
            LLMError("HTTP 429 limite de requisições")
        )
        with pytest.raises(LLMError):
            cliente.complete("s", [Message(role="user", content="oi")])


class TestOpenAICompat:
    def test_ferramentas_no_envelope_function(self):
        convertidas = OpenAICompatClient._convert_tools(tool_schemas())
        assert all(t["type"] == "function" for t in convertidas)
        assert convertidas[0]["function"]["name"] == "buscar_produtos"

    def test_system_vai_como_primeira_mensagem(self):
        mensagens = OpenAICompatClient._convert_messages("regras", CONVERSA)
        assert mensagens[0] == {"role": "system", "content": "regras"}

    def test_argumentos_de_ferramenta_vao_como_string_json(self):
        mensagens = OpenAICompatClient._convert_messages("", CONVERSA)
        assistente = mensagens[1]
        bruto = assistente["tool_calls"][0]["function"]["arguments"]
        assert isinstance(bruto, str)
        assert json.loads(bruto) == {"nome": "GD20"}

    def test_resultado_casa_por_tool_call_id(self):
        mensagens = OpenAICompatClient._convert_messages("", CONVERSA)
        assert mensagens[2] == {
            "role": "tool",
            "tool_call_id": "c1",
            "content": '{"preco_tabela": 2199.0}',
        }

    def test_parsing_com_ferramenta(self):
        resposta = OpenAICompatClient._parse(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "function": {
                                        "name": "calcular_preco",
                                        "arguments": '{"product_id": 95}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            }
        )
        assert resposta.tool_calls[0].name == "calcular_preco"
        assert resposta.tool_calls[0].arguments == {"product_id": 95}
        assert resposta.text == ""

    def test_resposta_sem_choices_e_erro(self):
        with pytest.raises(LLMError):
            OpenAICompatClient._parse({})

    def test_ollama_nao_exige_chave(self):
        cliente = OpenAICompatClient(provider="ollama", api_key="", model="qwen2.5:7b")
        assert "11434" in cliente.base_url

    def test_outros_provedores_exigem_chave(self):
        with pytest.raises(LLMError):
            OpenAICompatClient(provider="groq", api_key="", model="llama-3.3-70b-versatile")

    def test_base_url_por_provedor(self):
        for provedor, fragmento in (
            ("openai", "api.openai.com"),
            ("groq", "api.groq.com"),
            ("openrouter", "openrouter.ai"),
        ):
            cliente = OpenAICompatClient(provider=provedor, api_key="k", model="m")
            assert fragmento in cliente.base_url

    def test_base_url_customizada_prevalece(self):
        cliente = OpenAICompatClient(
            provider="openai", api_key="k", model="m", base_url="https://proxy.local/v1"
        )
        assert cliente.base_url == "https://proxy.local/v1"


class TestAnthropic:
    def test_ferramentas_usam_input_schema(self):
        convertidas = AnthropicClient._convert_tools(tool_schemas())
        assert "input_schema" in convertidas[0]
        assert convertidas[0]["input_schema"]["type"] == "object"

    def test_blocos_de_conteudo(self):
        mensagens = AnthropicClient._convert_messages(CONVERSA)
        assert mensagens[1]["role"] == "assistant"
        assert mensagens[1]["content"][0]["type"] == "tool_use"
        assert mensagens[2]["content"][0]["type"] == "tool_result"
        assert mensagens[2]["content"][0]["tool_use_id"] == "c1"

    def test_resultados_consecutivos_sao_agrupados(self):
        """Mandar um tool_result por mensagem é erro de formato na Messages API."""
        conversa = [
            Message(role="user", content="oi"),
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(id="a", name="x", arguments={}),
                    ToolCall(id="b", name="y", arguments={}),
                ],
            ),
            Message(role="tool", content="{}", tool_call_id="a", tool_name="x"),
            Message(role="tool", content="{}", tool_call_id="b", tool_name="y"),
            Message(role="assistant", content="pronto"),
        ]
        mensagens = AnthropicClient._convert_messages(conversa)
        resultados = [m for m in mensagens if isinstance(m["content"], list)
                      and m["content"][0].get("type") == "tool_result"]
        assert len(resultados) == 1
        assert len(resultados[0]["content"]) == 2

    def test_resultado_no_fim_da_conversa_e_descarregado(self):
        conversa = [
            Message(role="user", content="oi"),
            Message(role="assistant", tool_calls=[ToolCall(id="a", name="x")]),
            Message(role="tool", content="{}", tool_call_id="a", tool_name="x"),
        ]
        mensagens = AnthropicClient._convert_messages(conversa)
        assert mensagens[-1]["content"][0]["type"] == "tool_result"

    def test_parsing_misto_de_texto_e_ferramenta(self):
        resposta = AnthropicClient._parse(
            {
                "content": [
                    {"type": "text", "text": "Deixa eu verificar."},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "buscar_produtos",
                        "input": {"termo": "violao"},
                    },
                ],
                "usage": {"input_tokens": 200, "output_tokens": 30},
                "stop_reason": "tool_use",
            }
        )
        assert resposta.text == "Deixa eu verificar."
        assert resposta.tool_calls[0].name == "buscar_produtos"
        assert resposta.usage.total == 230

    def test_sem_chave_falha(self):
        with pytest.raises(LLMError):
            AnthropicClient(api_key="", model="claude-sonnet-4-5")


class TestHelpersComuns:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ({"a": 1}, {"a": 1}),
            ('{"a": 1}', {"a": 1}),
            ("", {}),
            ("   ", {}),
            ("{quebrado", {}),  # JSON malformado não derruba o laço
            ("[1,2]", {}),  # array onde se espera objeto
            (None, {}),
            (42, {}),
        ],
    )
    def test_parse_arguments_tolerante(self, entrada, esperado):
        assert parse_arguments(entrada) == esperado

    @pytest.mark.parametrize(
        ("status", "pista"),
        [
            (401, ".env"),
            (403, ".env"),
            (429, "limite"),
            (404, "EMPORIO_LLM_MODEL"),
            (500, "instabilidade"),
            (418, "HTTP 418"),
        ],
    )
    def test_erro_http_vira_mensagem_acionavel(self, status, pista):
        erro = friendly_http_error("gemini", status, "{}")
        assert pista in str(erro)


class TestFactory:
    def test_provedor_invalido(self, monkeypatch):
        from emporio.llm.factory import build_client

        monkeypatch.setenv("EMPORIO_LLM_PROVIDER", "inexistente")
        with pytest.raises(LLMError) as exc:
            build_client(provider="inexistente")
        assert "não suportado" in str(exc.value)

    def test_descricao_nao_vaza_a_chave(self, monkeypatch):
        from emporio.llm.factory import describe_configuration

        monkeypatch.setenv("GEMINI_API_KEY", "segredo-nao-pode-aparecer")
        config = describe_configuration()
        assert "segredo-nao-pode-aparecer" not in str(config)
        assert config["chave_configurada"] is True

    def test_modelo_default_por_provedor(self):
        from emporio.config import DEFAULT_MODELS

        assert DEFAULT_MODELS["gemini"].startswith("gemini")
        assert set(DEFAULT_MODELS) >= {
            "gemini",
            "openai",
            "anthropic",
            "groq",
            "openrouter",
            "ollama",
        }
