# Text RAG Improvement Runbook

PixelRAG is intentionally excluded from this pass.

## What Changed

- Structured pages are protected during chunking:
  - image extraction pages stay as atomic chunks
  - Markdown table pages stay as atomic chunks
- Chunk payloads now include retrieval metadata:
  - `parent_id`
  - `section_id`
  - `chunk_sequence`
  - `previous_chunk_id`
  - `next_chunk_id`
  - `retrieval_role`
  - `is_atomic`
  - `image_paths`
  - `primary_image_path`
- Qdrant indexing now creates payload indexes for the new routing fields.
- Retrieval benchmark now defaults to the four text strategies only.
- Retrieval benchmark uses:
  - Qdrant vector candidates
  - local BM25 candidates from `chunks/<strategy>/chunks.jsonl`
  - lightweight lexical and numeric reranking
  - adjacent chunk-window expansion

Selected benchmark defaults after parameter sweep:

- `top_k`: 10
- `candidate_k`: 20
- `bm25_k`: 20
- `window_radius`: 1
- `context_char_limit`: 4500
- `answer_contexts`: 5
- reranking weights: vector `0.45`, BM25 `0.25`, lexical `0.25`, numeric `0.05`

## Rerun Order

Regenerate chunks:

```powershell
venv\Scripts\python.exe scripts\chunking\chunk_langchain_markdown_recursive.py
venv\Scripts\python.exe scripts\chunking\chunk_haystack_document_splitter.py
venv\Scripts\python.exe scripts\chunking\chunk_llamaindex_semantic.py
venv\Scripts\python.exe scripts\chunking\chunk_chonkie_semantic.py
```

Recreate the four text Qdrant collections:

```powershell
venv\Scripts\python.exe scripts\indexing\index_qdrant_ollama.py --recreate --parallel-strategies --continue-on-error
```

Run the upgraded benchmark:

```powershell
venv\Scripts\python.exe scripts\evaluation\run_retrieval_benchmark_qdrant.py
```

Generate the PDF report:

```powershell
venv\Scripts\python.exe scripts\evaluation\generate_retrieval_eval_report.py --results eval\retrieval_results_qdrant_bge_m3.json --output output\pdf\retrieval_evaluation_report.pdf --include-ground-truth-images
```

If `venv\Scripts\python.exe` still fails with a stale Python path, recreate the venv before running these commands.
