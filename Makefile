# Atalhos do projeto. Em Windows sem `make`, cada receita mostra o comando
# equivalente — rode direto o que está na linha abaixo do alvo.

PY ?= python

.PHONY: help install build chat serve test cov lint eval examples clean

help:
	@echo "install    instala o projeto e as dependencias de desenvolvimento"
	@echo "build      prepara os dados (CSV -> SQLite) e indexa as politicas"
	@echo "chat       abre o chat no terminal com trace das consultas"
	@echo "serve      sobe a API e a interface web em http://127.0.0.1:8000"
	@echo "test       roda a suite de testes"
	@echo "cov        roda a suite com relatorio de cobertura"
	@echo "lint       checa estilo com ruff"
	@echo "eval       roda o conjunto de avaliacao do agente (precisa de chave)"
	@echo "examples   regenera as conversas de exemplo (precisa de chave)"
	@echo "clean      remove artefatos gerados (var/, logs/, caches)"

install:
	$(PY) -m pip install -e ".[dev]"

build:
	$(PY) -m emporio.cli build --force

chat:
	$(PY) -m emporio.cli chat --trace

serve:
	$(PY) -m emporio.cli serve

test:
	$(PY) -m pytest

cov:
	$(PY) -m pytest --cov=emporio --cov-report=term-missing

lint:
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

eval:
	$(PY) -m emporio.cli eval --report

examples:
	$(PY) scripts/gerar_exemplos.py

clean:
	rm -rf var logs .pytest_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} +
