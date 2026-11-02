"""API HTTP, persistência de conversa e registro de ferramentas."""

from __future__ import annotations

from fastapi.testclient import TestClient

from emporio.agent.service import AgentService
from emporio.api import api, get_service
from emporio.llm.base import Message, ToolCall
from emporio.memory.store import ConversationStore


class TestEndpointsPublicos:
    def setup_method(self):
        self.client = TestClient(api)

    def test_health_responde_sem_chave_de_api(self):
        """A API tem de subir mesmo sem configuração, para poder diagnosticar."""
        resposta = self.client.get("/api/health")
        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["dados_carregados"] is True
        assert corpo["indice_de_politicas"] is True
        assert "llm" in corpo

    def test_health_nunca_expoe_a_chave(self):
        corpo = self.client.get("/api/health").json()
        assert set(corpo["llm"]) >= {"provedor", "modelo", "chave_configurada"}
        assert isinstance(corpo["llm"]["chave_configurada"], bool)
        assert "api_key" not in str(corpo).lower()

    def test_lista_as_ferramentas_com_schema(self):
        corpo = self.client.get("/api/tools").json()
        assert corpo["total"] == 11
        nomes = {f["nome"] for f in corpo["ferramentas"]}
        assert "verificar_elegibilidade_devolucao" in nomes
        sensiveis = {f["nome"] for f in corpo["ferramentas"] if f["le_dado_pessoal"]}
        assert sensiveis == {
            "consultar_pedido",
            "identificar_cliente",
            "verificar_elegibilidade_devolucao",
        }

    def test_saudacao(self):
        corpo = self.client.get("/api/saudacao").json()
        assert corpo["agente"] == "Téo"
        assert "Empório da Música" in corpo["mensagem"]

    def test_interface_web_e_servida(self):
        resposta = self.client.get("/")
        assert resposta.status_code == 200
        assert "text/html" in resposta.headers["content-type"]
        assert "Empório da Música" in resposta.text

    def test_mensagem_vazia_e_rejeitada(self):
        assert self.client.post("/api/chat", json={"mensagem": ""}).status_code == 422

    def test_sessao_inexistente_da_404(self):
        assert self.client.get("/api/session/naoexiste123").status_code == 404


class TestChatComDubleDeLlm:
    """Exercita /api/chat injetando um serviço com LLM falso."""

    def setup_method(self):
        from tests.conftest import FakeLLM

        self.fake = FakeLLM()
        get_service.cache_clear()
        get_service.__wrapped__ = lambda: AgentService(client=self.fake)  # type: ignore[attr-defined]
        self._service = AgentService(client=self.fake)
        api.dependency_overrides = {}
        self.client = TestClient(api)

    def teardown_method(self):
        get_service.cache_clear()

    def test_chat_responde_e_devolve_session_id(self, monkeypatch):
        from emporio import api as api_module

        monkeypatch.setattr(api_module, "get_service", lambda: self._service)
        self.fake.scripted = [self.fake.texto("Oi! Sou o Téo.")]
        resposta = TestClient(api_module.api).post(
            "/api/chat", json={"mensagem": "oi"}
        )
        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["resposta"] == "Oi! Sou o Téo."
        assert corpo["session_id"]

    def test_trace_so_aparece_quando_pedido(self, monkeypatch):
        from emporio import api as api_module

        monkeypatch.setattr(api_module, "get_service", lambda: self._service)
        client = TestClient(api_module.api)

        self.fake.scripted = [
            self.fake.chama("listar_categorias"),
            self.fake.texto("Trabalhamos com estas categorias…"),
        ]
        sem = client.post("/api/chat", json={"mensagem": "o que vendem?"}).json()
        assert sem["trace"] == []

        self.fake.scripted = [
            self.fake.chama("listar_categorias"),
            self.fake.texto("Trabalhamos com estas categorias…"),
        ]
        com = client.post(
            "/api/chat", json={"mensagem": "o que vendem?", "incluir_trace": True}
        ).json()
        assert com["trace"][0]["ferramenta"] == "listar_categorias"
        assert com["trace"][0]["ok"] is True


class TestPersistencia:
    def test_sessao_e_criada_e_listada(self, tmp_path):
        store = ConversationStore(tmp_path / "c.sqlite3")
        session_id = store.create_session(channel="teste")
        assert session_id in {s.session_id for s in store.list_sessions()}

    def test_mensagens_ida_e_volta(self, tmp_path):
        store = ConversationStore(tmp_path / "c.sqlite3")
        session_id = store.create_session()
        store.append_messages(
            session_id,
            [
                Message(role="user", content="quanto custa o Takamine GD20?"),
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(id="t1", name="detalhar_produto", arguments={"nome": "GD20"})
                    ],
                ),
                Message(role="tool", content='{"ok":1}', tool_call_id="t1", tool_name="x"),
                Message(role="assistant", content="Sai por R$ 2.199,00."),
            ],
        )
        recarregadas = store.load_messages(session_id)
        assert [m.role for m in recarregadas] == ["user", "assistant", "tool", "assistant"]
        assert recarregadas[1].tool_calls[0].name == "detalhar_produto"

    def test_transcricao_traz_so_as_falas(self, tmp_path):
        store = ConversationStore(tmp_path / "c.sqlite3")
        session_id = store.create_session()
        store.append_messages(
            session_id,
            [
                Message(role="user", content="oi"),
                Message(role="tool", content="{}", tool_name="x"),
                Message(role="assistant", content="olá"),
            ],
        )
        assert store.transcript(session_id) == [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "olá"},
        ]

    def test_sequencia_continua_entre_gravacoes(self, tmp_path):
        """Retomar sessão não pode sobrescrever o histórico anterior."""
        store = ConversationStore(tmp_path / "c.sqlite3")
        session_id = store.create_session()
        store.append_messages(session_id, [Message(role="user", content="um")])
        store.append_messages(session_id, [Message(role="user", content="dois")])
        assert [m.content for m in store.load_messages(session_id)] == ["um", "dois"]

    def test_ensure_session_retoma_existente(self, tmp_path):
        store = ConversationStore(tmp_path / "c.sqlite3")
        criada = store.create_session()
        assert store.ensure_session(criada) == criada
        assert store.ensure_session(None) != criada

    def test_delete_limpa_tudo(self, tmp_path):
        store = ConversationStore(tmp_path / "c.sqlite3")
        session_id = store.create_session()
        store.append_messages(session_id, [Message(role="user", content="oi")])
        store.delete_session(session_id)
        assert store.load_messages(session_id) == []
        assert session_id not in {s.session_id for s in store.list_sessions()}


class TestServicoDeAtendimento:
    def test_historico_sobrevive_a_reabertura_da_sessao(self, tmp_path, fake_llm):
        """O caso de uso real: cliente volta no dia seguinte."""
        store = ConversationStore(tmp_path / "c.sqlite3")
        service = AgentService(client=fake_llm, store=store)
        session_id = service.open_session()

        fake_llm.scripted = [fake_llm.texto("O Takamine GD20 sai por R$ 2.199,00.")]
        service.send(session_id, "quanto custa o Takamine GD20?")

        # Novo serviço, mesma sessão — simula outro processo.
        outro = AgentService(client=fake_llm, store=store)
        outro.open_session(session_id)
        fake_llm.scripted = [fake_llm.texto("Sim, ainda temos.")]
        outro.send(session_id, "e ainda tem em estoque?")

        transcricao = outro.transcript(session_id)
        assert len(transcricao) == 4
        assert "Takamine" in transcricao[0]["content"]

    def test_chamadas_de_ferramenta_sao_auditadas(self, tmp_path, fake_llm):
        store = ConversationStore(tmp_path / "c.sqlite3")
        service = AgentService(client=fake_llm, store=store)
        session_id = service.open_session()
        fake_llm.scripted = [
            fake_llm.chama("listar_categorias"),
            fake_llm.texto("pronto"),
        ]
        service.send(session_id, "o que vendem?")
        stats = store.tool_usage_stats()
        assert stats[0]["name"] == "listar_categorias"
        assert stats[0]["chamadas"] == 1

    def test_reset_abre_sessao_nova(self, tmp_path, fake_llm):
        store = ConversationStore(tmp_path / "c.sqlite3")
        service = AgentService(client=fake_llm, store=store)
        primeira = service.open_session()
        fake_llm.scripted = [fake_llm.texto("oi")]
        service.send(primeira, "oi")
        segunda = service.reset_session(primeira)
        assert segunda != primeira
        assert service.transcript(segunda) == []

    def test_modo_sem_persistencia(self, fake_llm):
        service = AgentService(client=fake_llm, persist=False)
        session_id = service.open_session()
        fake_llm.scripted = [fake_llm.texto("oi")]
        service.send(session_id, "oi")
        assert len(service.transcript(session_id)) == 2
        assert service.configuration()["persistencia"] == "desativada"


class TestExecucaoDeFerramentaPelaAPI:
    """`POST /api/tool/{nome}` — a camada determinística exposta como serviço.

    Existe para que a regra de negócio seja inspecionável sem gastar chamada de
    modelo, e por isso tem de funcionar **sem chave de API configurada**. É
    também a parte do serviço que continua de pé quando a cota do provedor
    esgota, então o contrato aqui é mais importante do que parece.
    """

    def setup_method(self):
        self.client = TestClient(api)

    def _executar(self, nome: str, **argumentos):
        return self.client.post(f"/api/tool/{nome}", json={"argumentos": argumentos})

    def test_executa_sem_chave_de_api(self):
        resposta = self._executar("promocoes_ativas")
        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["ferramenta"] == "promocoes_ativas"
        assert corpo["le_dado_pessoal"] is False
        # Só 4 das 25 promoções da base estão ativas.
        assert corpo["resultado"]["total"] == 4

    def test_coage_argumento_que_chega_como_texto(self):
        """A interface web manda tudo como string; o endpoint tem de aceitar."""
        resultado = self._executar("calcular_preco", product_id="121").json()["resultado"]
        assert resultado["product_id"] == 121
        # §6.2: os 5% do PIX não incidem sobre preço já promocional.
        assert resultado["pix"]["desconto_pix_aplicado"] is False

    def test_ferramenta_sensivel_nao_vaza_pela_api(self):
        """§9 — expor a ferramenta como endpoint não afrouxa a verificação."""
        resposta = self._executar("consultar_pedido", order_id=7)
        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["le_dado_pessoal"] is True
        assert corpo["resultado"]["verificacao_necessaria"] is True
        # Mesma tentativa de vazamento dos testes da ferramenta, agora no HTTP.
        serializado = resposta.text.lower()
        for vazamento in ("letícia", "leticia", "99789", "159", "shelby"):
            assert vazamento not in serializado

    def test_argumento_obrigatorio_faltando_diz_o_que_falta(self):
        resposta = self._executar("consultar_politicas")
        assert resposta.status_code == 422
        detalhe = resposta.json()["detail"]
        assert detalhe["faltando"] == ["pergunta"]
        assert "limite" in detalhe["aceita"]

    def test_ferramenta_inexistente_lista_as_validas(self):
        resposta = self._executar("ferramenta_que_nao_existe")
        assert resposta.status_code == 404
        detalhe = resposta.json()["detail"]
        assert len(detalhe["disponiveis"]) == 11
        assert "buscar_produtos" in detalhe["disponiveis"]

    def test_erro_de_negocio_vem_como_200_com_payload(self):
        """Pedido inexistente é informação, não falha de transporte."""
        resposta = self._executar("consultar_pedido", order_id=999999)
        assert resposta.status_code == 200
        assert "erro" in str(resposta.json()["resultado"]).lower()

    def test_explorador_e_servido(self):
        resposta = self.client.get("/ferramentas")
        assert resposta.status_code == 200
        assert "text/html" in resposta.headers["content-type"]
