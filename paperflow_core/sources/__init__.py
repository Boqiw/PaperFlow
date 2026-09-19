"""数据源适配器包。

每个子模块暴露统一接口 ``search(query, limit, min_year) -> list[paper]``。
调度与别名解析见 :mod:`.registry`，检索编排见 :mod:`paperflow_core.search`。
"""

from . import arxiv, openalex, pubmed, semantic_scholar  # noqa: F401
