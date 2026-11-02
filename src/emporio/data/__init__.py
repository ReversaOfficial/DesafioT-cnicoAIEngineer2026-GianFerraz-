"""Camada de dados: ETL dos CSVs para SQLite e acesso ao banco."""

from emporio.data.db import connect, ensure_database
from emporio.data.loader import build_database

__all__ = ["build_database", "connect", "ensure_database"]
