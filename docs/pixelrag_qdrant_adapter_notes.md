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
  --device cuda `
  --recreate `
  --force
```

Check CUDA before a long PixelRAG run:

```powershell
venv\Scripts\python.exe scripts\indexing\index_pixelrag_qdrant.py --check-cuda
```

Run PixelRAG for the full `data/` folder and write the visual vectors to Qdrant:

```powershell
venv\Scripts\python.exe scripts\indexing\index_pixelrag_qdrant.py `
  --source data `
  --output pixelrag_indexes\ey_visual_index `
  --collection ey_rag_pixelrag_visual `
  --pages all `
  --source-limit 0 `
  --embed-limit 0 `
  --device cuda `
  --recreate `
  --embed-timeout 86400 `
  --heartbeat-seconds 120
```

`--source-limit 0` means all source documents. `--embed-limit 0` means all
visual chunks. The script prints progress for source staging, per-document
rendering, PixelRAG chunking/embedding, and Qdrant upserts.

For recovery after a local render, embedding, or Qdrant upload failure, rerun
without `--force`. Existing completed renders are reused page by page. Use
`--force` only when you intentionally want to delete the generated tiles and
embedding shards before rebuilding them. Render failures are recorded in
`render_errors.jsonl`; by default the renderer keeps going after a bad page.

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

Current local CUDA check:

```text
torch=2.11.0+cu128
torch_cuda_version=12.8
cuda_available=True
device_count=1
device_name=NVIDIA GeForce RTX 3050 Laptop GPU
total_memory_gb=4.00
```

Current CUDA smoke result: rendering, chunking, embedding, and local Qdrant
upsert succeeded with `Qwen/Qwen3-VL-Embedding-2B` on CUDA. The test collection
`ey_rag_pixelrag_visual_test` was created with vector size `2048`.
