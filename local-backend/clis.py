"""Registro de adaptadores de CLI (un módulo por CLI) + el runner compartido.
server.py importa de acá: CLIS (dict key→adaptador) y run_cli (el núcleo)."""
from cli_base import run_cli  # re-export para server.py
from claude import ClaudeAdapter
from codex import CodexAdapter
from gemini import GeminiAdapter

CLIS = {a.key: a for a in (ClaudeAdapter(), CodexAdapter(), GeminiAdapter())}

__all__ = ["CLIS", "run_cli"]
