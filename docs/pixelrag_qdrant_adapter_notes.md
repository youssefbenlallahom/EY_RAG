# PixelRAG to Qdrant Adapter Notes

PixelRAG's default index builder writes FAISS. The adapter in this project
uses PixelRAG only up to the embedding-shard stage, then indexes the
`embeddings/shard_*.npz` files into Qdrant.

```powershell
venv\Scripts\python.exe scripts\indexing\index_pixelrag_qdrant.py `
  --source data\ey-gl-sustainability-deve-10-ed-sasb-ifrs-07-2025.pdf `
  --output pixelrag_indexes\qdrant_one_doc_smoke `
  --collection ey_rag_pixelrag_visual_smoke `
  --pages 1 `
  --embed-limit 1 `
  --recreate `
  --force
```

The Qdrant point payload keeps the visual chunk traceable:

```text
source_name
original_path
staged_path
article_id
tile_index
chunk_index
y_offset
tile_height
tile_path
pixelrag_output
```

Current local smoke result: rendering and chunking succeeded on one PDF page.
The real PixelRAG embedding stage timed out while loading/downloading
`Qwen/Qwen3-VL-Embedding-2B` on CPU, so a real PixelRAG shard was not produced
within the bounded test. A synthetic PixelRAG-shaped shard was used only to
verify the `.npz -> Qdrant` adapter path, and Qdrant accepted the point with
the expected payload.
