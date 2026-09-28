"""Offline, source-cited strategy knowledge retrieval.

Imports are lazy so ``python -m spirebrain.knowledge.rag`` does not import the
module twice and emit a runpy warning on Windows.
"""

__all__ = [
    "KnowledgeChunk",
    "StrategyKnowledgeBase",
    "default_knowledge_base",
    "retrieve_strategy_context",
]


def __getattr__(name):
    if name in __all__:
        from . import rag
        return getattr(rag, name)
    raise AttributeError(name)
