# Qdrant Cloud Ingestion

The all-in-one Cloud ingestion script reads credentials from `.env`:

```text
QDRANT_CLOUD_URL=...
QDRANT_CLOUD_API_KEY=...
```

It runs:

- LangChain Markdown recursive chunks
- Haystack document splitter chunks
- LlamaIndex semantic chunks
- Chonkie semantic chunks
- PixelRAG visual embeddings

## Smoke Test

Use this before a full run:

```powershell
python scripts\indexing\index_all_qdrant_cloud.py `
  --recreate `
  --text-limit-per-strategy 20 `
  --pixelrag-source data `
  --pixelrag-output pixelrag_indexes\cloud_smoke `
  --pixelrag-collection ey_rag_pixelrag_visual_cloud_smoke `
  --pixelrag-pages 1 `
  --pixelrag-source-limit 2 `
  --pixelrag-embed-limit 5 `
  --pixelrag-device cuda `
  --pixelrag-embed-timeout 3600 `
  --pixelrag-heartbeat-seconds 60 `
  --timeout 600 `
  --qdrant-retries 12
```

## Full Run

```powershell
python scripts\indexing\index_all_qdrant_cloud.py `
  --recreate `
  --parallel-text `
  --text-workers 4 `
  --pixelrag-source data `
  --pixelrag-output pixelrag_indexes\ey_visual_index_cloud `
  --pixelrag-collection ey_rag_pixelrag_visual `
  --pixelrag-pages all `
  --pixelrag-source-limit 0 `
  --pixelrag-embed-limit 0 `
  --pixelrag-device cuda `
  --pixelrag-embed-timeout 86400 `
  --pixelrag-heartbeat-seconds 120 `
  --timeout 600 `
  --qdrant-retries 12
```

The text methods use local Ollama embeddings and write vectors to Qdrant Cloud.
PixelRAG uses the cached Hugging Face model after the first download.
PixelRAG rendering and embedding still happen locally before vectors are sent
to Qdrant Cloud, so local PDF rendering errors can also stop a Cloud run.

## Recovery After HTTP/SSL Errors

The Cloud indexers use deterministic point IDs, retry failed Qdrant operations
with exponential backoff, and resume by default. If a long run stops after an
HTTP, SSL, timeout, or connection reset error, rerun the same command without
`--recreate`:

```powershell
python scripts\indexing\index_all_qdrant_cloud.py `
  --parallel-text `
  --text-workers 4 `
  --pixelrag-source data `
  --pixelrag-output pixelrag_indexes\ey_visual_index_cloud `
  --pixelrag-collection ey_rag_pixelrag_visual `
  --pixelrag-pages all `
  --pixelrag-source-limit 0 `
  --pixelrag-embed-limit 0 `
  --pixelrag-device cuda `
  --pixelrag-embed-timeout 86400 `
  --pixelrag-heartbeat-seconds 120 `
  --timeout 600 `
  --qdrant-retries 12
```

Do not use `--recreate` for recovery unless you intentionally want to delete
the partially ingested Cloud collections and start from zero.

If the failure happened after PixelRAG embeddings were already produced and
only the Cloud upload failed, reuse the local visual work and rerun only the
Qdrant upsert part:

```powershell
python scripts\indexing\index_all_qdrant_cloud.py `
  --skip-text `
  --pixelrag-source data `
  --pixelrag-output pixelrag_indexes\ey_visual_index_cloud `
  --pixelrag-collection ey_rag_pixelrag_visual `
  --pixelrag-pages all `
  --pixelrag-source-limit 0 `
  --pixelrag-embed-limit 0 `
  --pixelrag-device cuda `
  --pixelrag-skip-render `
  --pixelrag-skip-embed `
  --timeout 600 `
  --qdrant-retries 12
```

If the failure happened during PDF rendering, rerun without
`--pixelrag-force`. The renderer skips already completed documents/pages,
retries problematic pages, falls back to lower DPI, and writes skipped-page
details to `render_errors.jsonl`.

Use `--pixelrag-force` only when you intentionally want to delete existing
PixelRAG tiles/embeddings and rebuild the visual index from zero.

Cloud-safe defaults:

- Text upsert batch size: `32`
- PixelRAG upsert batch size: `16`
- Qdrant timeout: `600` seconds
- Qdrant retry attempts: `12`
- Exponential retry backoff with jitter
- PixelRAG PDF page retries: `3`
- PixelRAG fallback render DPI: `150`
