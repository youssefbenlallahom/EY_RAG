# EY RAG Pipeline

This project prepares EY, IFRS, and IAASB documents for retrieval-augmented generation.

## Layout

```text
data/                 Source PDFs
output/               Raw extraction outputs
output_clean/         Cleaned Markdown corpus ready for chunking
filtered_images/      OCR-filtered figure/table/chart images
chunks/               Generated chunk JSONL outputs
src/ey_rag/           Reusable cleaning and chunking utilities
scripts/extraction/   PDF-to-Markdown extraction
scripts/images/       Image filtering and vision-description enrichment
scripts/cleaning/     Markdown cleaning
scripts/chunking/     Comparable chunking techniques
scripts/indexing/     Text and visual index builders
```

## Main Commands

Install dependencies:

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
```

Extract PDFs:

```powershell
venv\Scripts\python.exe scripts\extraction\run_pipeline.py
```

Filter and enrich images:

```powershell
venv\Scripts\python.exe scripts\images\run_image_filter_pipeline.py
venv\Scripts\python.exe scripts\images\enrich_markdown_with_images.py
```

Clean Markdown:

```powershell
venv\Scripts\python.exe scripts\cleaning\clean_markdown.py
```

Create comparable chunk outputs:

```powershell
venv\Scripts\python.exe scripts\chunking\chunk_langchain_markdown_recursive.py
venv\Scripts\python.exe scripts\chunking\chunk_llamaindex_semantic.py
venv\Scripts\python.exe scripts\chunking\chunk_haystack_document_splitter.py
venv\Scripts\python.exe scripts\chunking\chunk_chonkie_semantic.py
```

Each chunker writes a `chunks.jsonl` file with compatible metadata fields, so each technique can be indexed into a separate vector collection for fair retrieval comparison.

Start local Qdrant and index chunks with Ollama/BGE:

```powershell
docker compose up -d qdrant
ollama pull bge-m3
venv\Scripts\python.exe scripts\indexing\index_qdrant_ollama.py --recreate --parallel-strategies
```

Run a one-document parallel ingestion smoke test across the Qdrant text indexes
and PixelRAG visual index builder:

```powershell
venv\Scripts\python.exe scripts\indexing\run_single_doc_parallel_ingestion_smoke.py
```

Prepare a PixelRAG visual index config:

```powershell
venv\Scripts\python.exe scripts\indexing\build_pixelrag_visual_index.py
```

Index PixelRAG visual embedding shards into Qdrant instead of FAISS:

```powershell
venv\Scripts\python.exe scripts\indexing\index_pixelrag_qdrant.py --source data\ey-gl-sustainability-deve-10-ed-sasb-ifrs-07-2025.pdf --pages 1 --embed-limit 1 --recreate --force
```

The PixelRAG wrapper stages source files into numeric filenames under the
target index folder and writes `_source_mapping.json` for traceability.

PixelRAG declares Python 3.12+ and uses heavier visual-index dependencies. The
project venv is already Python 3.12; install the requirements before building:

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
```

Build a small PixelRAG visual index smoke test:

```powershell
venv\Scripts\python.exe scripts\indexing\build_pixelrag_visual_index.py --build --limit 1
```
