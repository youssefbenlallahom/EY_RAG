# Qdrant + Ollama Indexing Notes

This project indexes each chunking strategy into a separate Qdrant collection.
That keeps retrieval evaluation fair: same embedding model, same vector DB, same
payload schema, only the chunking strategy changes.

## Local Services

Start Qdrant:

```powershell
docker compose up -d qdrant
```

Start Ollama separately, then pull the local BGE model:

```powershell
ollama pull bge-m3
```

## Collections

The indexer creates one dense-vector collection per strategy:

```text
ey_rag_langchain_markdown_recursive
ey_rag_haystack_document_splitter
ey_rag_chonkie_semantic
ey_rag_llamaindex_semantic
```

## Payload Indexes

The indexer creates payload indexes for fields used in filtered retrieval:

```text
chunk_id
strategy
source_name
page_start
page_end
page_type
content_type
standard_refs
heading_path_text
token_estimate
```

These support later filters like:

```text
content_type = table
standard_refs contains IFRS 18
source_name = IAASB-2025-Handbook-Volume-1.md
page_start between 100 and 120
```

## References

- [Qdrant quickstart](https://qdrant.tech/documentation/quickstart/)
- [Qdrant collections](https://qdrant.tech/documentation/concepts/collections/)
- [Qdrant payload indexing](https://qdrant.tech/documentation/concepts/indexing/)
- [Qdrant points and upserts](https://qdrant.tech/documentation/concepts/points/)
- [Qdrant filtering](https://qdrant.tech/documentation/concepts/filtering/)
- [Ollama API: embeddings](https://github.com/ollama/ollama/blob/main/docs/api.md)
