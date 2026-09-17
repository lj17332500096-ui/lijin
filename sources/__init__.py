"""Sources 深度 RAG（参考资料检索）——只读 Context 来源，不是新 Runtime。

接线：Source Storage(forge_data/…) → Parser/Chunker → FTS5+向量索引(agent.db)
      → Hybrid Retriever（强制 project 过滤）→ 现有 Context Builder / search_sources 工具。
不绕过 FileScope / Trust Boundary；不把 Retrieval 塞进 Agent Runner 之外的新执行链。
"""

from sources.service import build_reference_block, scoped_search
