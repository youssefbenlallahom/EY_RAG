# Qdrant Snapshot Transfer

Use this when you want another PC to get the same Qdrant vector database without
running chunking, embedding, or ingestion again.

## On Your PC

Start Docker Desktop, then start Qdrant:

```powershell
docker compose up -d qdrant
```

Check that Qdrant is reachable at `http://localhost:6333` before exporting.

Export the four text RAG collections:

```powershell
python scripts\indexing\transfer_qdrant_snapshots.py export
```

The output bundle is:

```text
qdrant_transfer\ey_rag_qdrant_snapshots.zip
```

Send that `.zip` file to your friend.

## Restore Directly To Qdrant Cloud

Put your Qdrant Cloud credentials in `.env`:

```text
QDRANT_CLOUD_URL=https://YOUR-QDRANT-CLOUD-CLUSTER-URL
QDRANT_CLOUD_API_KEY=YOUR_QDRANT_CLOUD_API_KEY
```

Then restore the same bundle directly to Qdrant Cloud without ingestion:

```powershell
python scripts\indexing\transfer_qdrant_snapshots.py --timeout 1800 restore --cloud --bundle qdrant_transfer\ey_rag_qdrant_snapshots.zip --yes --upload-retries 3
```

The restore command uses `requests-toolbelt` to stream large snapshot uploads,
prints upload progress, and retries failed upload connections. If Qdrant Cloud
closes the TLS connection repeatedly, retry once with `--skip-checksum`.

To check what already exists in Qdrant Cloud:

```powershell
python scripts\indexing\transfer_qdrant_snapshots.py --timeout 60 collections --cloud
```

The snapshot bundle copies vectors and payloads. Payload indexes can be recreated
later if you need metadata-filter performance, but vector search works without
re-ingestion.

## On Your Friend's PC

They need Qdrant running. If they use this project, they can run:

```powershell
docker compose up -d qdrant
```

Inspect the bundle:

```powershell
python scripts\indexing\transfer_qdrant_snapshots.py inspect --bundle qdrant_transfer\ey_rag_qdrant_snapshots.zip
```

Restore all collections:

```powershell
python scripts\indexing\transfer_qdrant_snapshots.py restore --bundle qdrant_transfer\ey_rag_qdrant_snapshots.zip --yes
```

This overwrites target collections with the snapshot data.

## Collections Included By Default

- `ey_rag_langchain_markdown_recursive`
- `ey_rag_llamaindex_semantic`
- `ey_rag_haystack_document_splitter`
- `ey_rag_chonkie_semantic`

PixelRAG is not included by default.
