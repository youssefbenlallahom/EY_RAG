# Qdrant Cloud Ingestion Report

| Stage | Status | Seconds | Command |
| --- | --- | ---: | --- |
| text_chunking_methods | ok | 18.19 | `C:\Users\youssef\Desktop\EY_RAG\venv\Scripts\python.exe scripts/indexing/index_qdrant_ollama.py --cloud --chunks-dir chunks --collection-prefix ey_rag --strategies langchain_markdown_recursive,haystack_document_splitter,llamaindex_semantic,chonkie_semantic --ollama-url http://localhost:11434 --ollama-model bge-m3 --embed-batch-size 8 --upsert-batch-size 32 --timeout 600 --qdrant-retries 12 --qdrant-backoff 2.0 --qdrant-max-sleep 120.0 --qdrant-jitter 1.0 --parallel-strategies --max-workers 4` |
