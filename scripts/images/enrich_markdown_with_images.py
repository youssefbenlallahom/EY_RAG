#!/usr/bin/env python3
"""
Enrich Markdown files with Kimi vision model text extractions of filtered images.
This script scans the 'filtered_images' directory, queries Kimi (via NVIDIA API) for
each image, caches the results in JSON to avoid duplicate calls, and inserts the
extracted Markdown text directly in place of the image reference lines in the main
Markdown corpus. All other (decorative/unhelpful) image references are stripped.
"""

import base64
import concurrent.futures
import io
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path

# ==============================================================================
# CONFIGURATION CONSTANTS (Adjust these directly as needed)
# ==============================================================================
OUTPUT_DIR = "output"
FILTERED_DIR = "filtered_images"
CACHE_FILE = "output/image_extraction_cache.json"
CONCURRENCY = 1
MODELS_TO_TRY = [
    "moonshotai/kimi-k2.6",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "mistralai/mistral-large-3-675b-instruct-2512",
    "microsoft/phi-4-multimodal-instruct"
]
LIMIT = None         # Set to an integer (e.g. 1) to test a subset of images
DRY_RUN = False      # Set to True to verify file mapping without querying API

# ==============================================================================
# VISION MODEL PROMPT
# ==============================================================================
SYSTEM_PROMPT = (
    "You are a highly precise document and data extraction system designed to ingest images for a Retrieval-Augmented Generation (RAG) pipeline. "
    "Your goal is to extract the maximum amount of information from this image in a structured, readable Markdown format.\n\n"
    "Please follow these detailed rules based on what elements are present in the image:\n\n"
    "1. TEXT AND LAYOUT TRANSCRIPTION:\n"
    "   - Transcribe all visible text exactly.\n"
    "   - For multi-column text layouts, read each column completely from top to bottom before moving to the next. Do not mix text across adjacent columns.\n"
    "   - Automatically reassemble any hyphenated or split words (e.g., words broken across line wraps or column gaps) into complete words.\n"
    "   - Clean up spacing anomalies, mid-word line breaks, or artificial whitespace.\n\n"
    "2. TABLES:\n"
    "   - Transcribe tabular data into clean Markdown tables.\n"
    "   - Ensure header rows are correctly identified and formatted.\n"
    "   - Do not skip rows or columns.\n"
    "   - Include footnotes, source notes, or unit indicators immediately below the table.\n\n"
    "3. CHARTS, GRAPHS, AND VISUAL STATS:\n"
    "   - Provide a concise summary of what the visual represents (e.g., title, axis labels, timeframe).\n"
    "   - Convert data points, values, and trends shown in the chart or graph into a structured Markdown table or clear key-value bullet points.\n"
    "   - Explicitly note any legend keys, labels, or callout values.\n\n"
    "4. DIAGRAMS, FLOWCHARTS, AND RELATIONSHIP MAPS:\n"
    "   - Describe the layout and flow of the diagram.\n"
    "   - Represent sequential steps as numbered steps (e.g., 'Step 1 -> Step 2').\n"
    "   - Use nested bullet points to represent hierarchical relationships or structured groupings.\n\n"
    "5. STRICT EXTRACTION:\n"
    "   - Keep your transcription objective and literal. Do not hallucinate or extrapolate details. Only extract what is explicitly visible in the image."
)

# Try to import Pillow for downscaling large images
try:
    # pyrefly: ignore [missing-import]
    from PIL import Image
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False
    print("WARNING: Pillow (PIL) is not installed. Images exceeding character limits cannot be downscaled.")

try:
    import requests
except ImportError:
    sys.exit("ERROR: The 'requests' library is not installed in the active environment. Run: pip install requests")


def load_api_key():
    """Resolve the API key from environment variables or .env file."""
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("NVIDIA_API_KEY") and "=" in line:
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return key


def encode_image(path: Path, max_b64_chars=180_000):
    """Base64 encode the image, downscaling using Pillow if it exceeds size limits."""
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    b64 = base64.b64encode(raw).decode()
    if len(b64) <= max_b64_chars:
        return mime, b64

    if not _HAVE_PIL:
        print(f"WARNING: {path.name} is too large ({len(b64)} chars) and Pillow is missing. Sending raw.")
        return mime, b64

    print(f"  {path.name} too large ({len(b64)} b64 chars) -> downscaling to fit {max_b64_chars} limit...")
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    scale = 1.0
    for _ in range(15):
        scale *= 0.85
        w = max(1, int(img.width * scale))
        h = max(1, int(img.height * scale))
        buf = io.BytesIO()
        img.resize((w, h)).save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) <= max_b64_chars:
            print(f"  Downscaled {path.name} to {w}x{h} ({len(b64)} b64 chars)")
            return "image/jpeg", b64

    print(f"WARNING: Could not shrink {path.name} under limit. Sending current scale.")
    return "image/jpeg", b64


def build_model_payload(model_name: str, data_uri: str) -> dict:
    """Construct OpenAI-compatible payload tailored to each NVIDIA API model."""
    if model_name == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning":
        return {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SYSTEM_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "temperature": 0.6,
            "top_p": 0.95,
            "max_tokens": 16384,
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning_budget": 16384
        }
    elif model_name == "mistralai/mistral-large-3-675b-instruct-2512":
        return {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SYSTEM_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "max_tokens": 2048,
            "temperature": 0.15,
            "top_p": 1.00,
            "frequency_penalty": 0.00,
            "presence_penalty": 0.00,
        }
    elif model_name == "microsoft/phi-4-multimodal-instruct":
        return {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SYSTEM_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "max_tokens": 2048,
            "temperature": 0.10,
            "top_p": 0.70,
            "frequency_penalty": 0.00,
            "presence_penalty": 0.00,
        }
    else:  # moonshotai/kimi-k2.6
        return {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SYSTEM_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "max_tokens": 16384,
            "temperature": 1.0,
            "top_p": 1.0,
        }


def query_vision_model_with_fallback(image_path: Path, api_key: str) -> str:
    """Send request to the NVIDIA multimodal endpoints with fallback models and retries."""
    mime, b64 = encode_image(image_path)
    data_uri = f"data:{mime};base64,{b64}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    url = "https://integrate.api.nvidia.com/v1/chat/completions"

    for model_name in MODELS_TO_TRY:
        print(f"  Attempting extraction with model: {model_name} for {image_path.name}")
        payload = build_model_payload(model_name, data_uri)
        
        max_retries_per_model = 3
        backoff = 5

        for attempt in range(max_retries_per_model):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=90)
                if resp.status_code == 200:
                    data = resp.json()
                    message = data["choices"][0]["message"]
                    
                    # Print reasoning content if present (for nemotron, etc.)
                    reasoning = message.get("reasoning_content")
                    if reasoning:
                        print(f"\n--- Reasoning trace for {image_path.name} ---")
                        print(reasoning)
                        print("--------------------------------------------\n")
                    
                    content = message.get("content")
                    if content:
                        time.sleep(5)  # Cooldown to stay under rate limits
                        return content
                    else:
                        print(f"  [Warning] Model {model_name} returned empty content.")
                        break  # Fall back to next model
                elif resp.status_code == 429:
                    print(f"  Rate limited (429) for {model_name} on {image_path.name}. Waiting {backoff}s (Attempt {attempt+1}/{max_retries_per_model})...")
                    time.sleep(backoff)
                    backoff *= 2
                else:
                    print(f"  HTTP error {resp.status_code} for {model_name} on {image_path.name}: {resp.text[:200]}. Trying next fallback model...")
                    break
            except requests.Timeout:
                print(f"  Timeout for {model_name} on {image_path.name}. Trying next fallback model...")
                break
            except Exception as e:
                print(f"  Error with {model_name} on {image_path.name}: {e}. Trying next fallback model...")
                break

    raise RuntimeError(f"Failed to extract text from {image_path.name} after trying all fallback models.")


def load_cache(cache_file: Path) -> dict:
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading cache file: {e}. Starting fresh.")
    return {}


def save_cache(cache: dict, cache_file: Path):
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        # Write to temp file first to ensure atomic save
        temp_file = cache_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        temp_file.replace(cache_file)
    except Exception as e:
        print(f"Error saving cache file: {e}")


def main():
    api_key = load_api_key()
    if not api_key and not DRY_RUN:
        sys.exit("ERROR: NVIDIA_API_KEY not found. Set it in your environment or a .env file.")

    output_path = Path(OUTPUT_DIR)
    filtered_path = Path(FILTERED_DIR)
    cache_path = Path(CACHE_FILE)

    if not output_path.exists():
        sys.exit(f"ERROR: Output directory does not exist: {output_path}")
    if not filtered_path.exists():
        sys.exit(f"ERROR: Filtered images directory does not exist: {filtered_path}")

    # Gather images to process
    image_files = sorted(list(filtered_path.glob("*.png")))
    print(f"Found {len(image_files)} images in '{filtered_path.name}'.")

    if LIMIT is not None:
        image_files = image_files[:LIMIT]
        print(f"Limit applied. Processing first {len(image_files)} images.")

    # Load cache
    cache = load_cache(cache_path)
    print(f"Loaded cache containing {len(cache)} entries.")

    # Map target details from filenames
    # Filename format: {document}__p{page}__imageFile{N}__score{X}.png
    image_tasks = []
    for img_path in image_files:
        parts = img_path.stem.split("__")
        if len(parts) < 3:
            print(f"Warning: Filename does not follow pattern, skipping: {img_path.name}")
            continue
        document = parts[0]
        page = parts[1]
        image_file_id = parts[2]
        
        md_file = output_path / f"{document}.md"
        if not md_file.exists():
            print(f"Warning: Markdown target file not found for {img_path.name} -> {md_file.name}")
            continue

        image_tasks.append({
            "path": img_path,
            "filename": img_path.name,
            "document": document,
            "image_id": image_file_id,
            "md_file": md_file
        })

    if not image_tasks:
        print("No valid tasks found. Exiting.")
        return

    # Phase 1: Query API in parallel (or read cache)
    extractions = {}
    images_to_query = []

    for task in image_tasks:
        filename = task["filename"]
        if filename in cache:
            extractions[filename] = cache[filename]
        else:
            images_to_query.append(task)

    print(f"Using cached responses for {len(extractions)} images.")
    
    if images_to_query:
        if DRY_RUN:
            print(f"[DRY RUN] Would query Kimi API for {len(images_to_query)} images.")
            for task in images_to_query:
                extractions[task["filename"]] = f"<!-- DRY RUN EXTRACTION FOR {task['filename']} -->"
        else:
            print(f"Querying Vision APIs for {len(images_to_query)} images with concurrency={CONCURRENCY}...")
            
            # Using ThreadPoolExecutor for concurrent requests
            with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
                # Submit tasks
                future_to_task = {
                    executor.submit(query_vision_model_with_fallback, task["path"], api_key): task
                    for task in images_to_query
                }
                
                completed_count = 0
                for future in concurrent.futures.as_completed(future_to_task):
                    task = future_to_task[future]
                    filename = task["filename"]
                    try:
                        extracted_text = future.result()
                        extractions[filename] = extracted_text
                        
                        # Update cache atomically
                        cache[filename] = extracted_text
                        save_cache(cache, cache_path)
                        
                        completed_count += 1
                        print(f"[{completed_count}/{len(images_to_query)}] Successfully processed {filename}")
                    except Exception as exc:
                        print(f"Error processing {filename}: {exc}")

    # Phase 2: Markdown replacement and stripping (done sequentially to avoid file corruption)
    print("\nStarting Markdown integration stage...")
    
    # Group tasks by Markdown file
    tasks_by_md = {}
    for task in image_tasks:
        md_file = task["md_file"]
        if md_file not in tasks_by_md:
            tasks_by_md[md_file] = []
        tasks_by_md[md_file].append(task)

    for md_file, file_tasks in tasks_by_md.items():
        print(f"Updating {md_file.name}...")
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  Error reading {md_file.name}: {e}. Skipping file.")
            continue

        replaced_count = 0
        missing_extractions = 0

        # Replace each active image with its extraction
        for task in file_tasks:
            filename = task["filename"]
            image_id = task["image_id"]
            document = task["document"]

            if filename not in extractions:
                print(f"  Warning: No extraction available for {filename}, skipping replacement.")
                missing_extractions += 1
                continue

            extracted_text = extractions[filename]
            formatted_text = (
                f"\n<!-- START IMAGE EXTRACTION ({image_id}) -->\n"
                f"{extracted_text}\n"
                f"<!-- END IMAGE EXTRACTION ({image_id}) -->\n"
            )

            # Build regex to match the image link
            # Matches: ![image <num>](<[document]_images/[image_id].png>)
            # Using [^>]+_images to be robust to path variation
            pattern_str = r'!\[image\s+\d+\]\(<[^>]+_images/' + re.escape(image_id) + r'\.png>\)'
            pattern = re.compile(pattern_str, re.IGNORECASE)

            if pattern.search(content):
                content = pattern.sub(formatted_text, content)
                replaced_count += 1
            else:
                print(f"  Warning: Reference for {image_id} not found in {md_file.name}")

        # Strip remaining image references (which are decorative / unhelpful)
        # Matches any typical ![image X](<..._images/imageFileY.png>)
        strip_pattern = re.compile(r'!\[image\s+\d+\]\(<[^>]+_images/imageFile\d+\.png>\)', re.IGNORECASE)
        matches_to_strip = len(strip_pattern.findall(content))
        if matches_to_strip > 0:
            content = strip_pattern.sub("", content)
            print(f"  Stripped {matches_to_strip} decorative/unhelpful image references.")

        # Save modified markdown if not dry run
        if not DRY_RUN:
            try:
                md_file.write_text(content, encoding="utf-8")
                print(f"  Successfully wrote changes to {md_file.name} ({replaced_count} replaced, {matches_to_strip} stripped).")
            except Exception as e:
                print(f"  Error writing {md_file.name}: {e}")
        else:
            print(f"  [DRY RUN] Would write changes to {md_file.name} ({replaced_count} replaced, {matches_to_strip} stripped).")

    print("\nProcessing completed successfully.")


if __name__ == "__main__":
    main()
