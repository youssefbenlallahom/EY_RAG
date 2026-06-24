#!/usr/bin/env python3
"""
Quick test: send ONE image to an NVIDIA-hosted multimodal model and print the
model's text response. Built to test image -> text extraction for the RAG corpus.

Setup (pick one):
    setx NVIDIA_API_KEY "nvapi-xxxxxxxx"   # Windows; open a NEW terminal after
    # or create a .env file containing:  NVIDIA_API_KEY=nvapi-xxxxxxxx
    # or pass --api-key on the command line

Usage:
    python test_kimi_image.py                      # opens a file-picker dialog
    python test_kimi_image.py path/to/image.png    # or pass the path directly
    python test_kimi_image.py "filtered_images/ey-frd-series-spring-2025__p11__imageFile93__score3.png"
    python test_kimi_image.py img.png --prompt "Transcribe every number in this chart."
    python test_kimi_image.py img.png --stream
    python test_kimi_image.py img.png --model meta/llama-3.2-90b-vision-instruct

Tip: instead of typing a long path, run `python test_kimi_image.py ` (with a
trailing space) and DRAG the image from Explorer into the terminal to paste it.
"""

import argparse
import base64
import io
import json
import mimetypes
import os
import sys
from pathlib import Path

import requests

try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False


INVOKE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# NVIDIA accepts inline base64 images only while the encoded string stays under
# ~180k characters. Larger images need the NVCF asset-upload API; here we simply
# downscale to fit so the test stays a single self-contained request.
MAX_B64_CHARS = 180_000

DEFAULT_PROMPT = (
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


def load_api_key(cli_key):
    """Resolve the API key from --api-key, env var, then a simple .env parse."""
    if cli_key:
        return cli_key
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("NVIDIA_API_KEY") and "=" in line:
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        sys.exit(
            "ERROR: no API key. Set NVIDIA_API_KEY, add it to a .env file, "
            "or pass --api-key."
        )
    return key


def pick_image_dialog():
    """Open a native file-picker so the user can browse/select an image."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None
    root = tk.Tk()
    root.withdraw()           # hide the empty root window
    root.attributes("-topmost", True)  # bring the dialog to the front
    path = filedialog.askopenfilename(
        title="Select an image to send to the model",
        initialdir=str(Path("filtered_images").resolve()
                       if Path("filtered_images").is_dir() else Path.cwd()),
        filetypes=[
            ("Images", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return path or None


def encode_image(path, max_b64_chars):
    """Return (mime_type, base64_str). Downscale large images to fit the limit."""
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    b64 = base64.b64encode(raw).decode()
    if len(b64) <= max_b64_chars:
        return mime, b64

    if not _HAVE_PIL:
        sys.exit(
            f"ERROR: {path.name} is too large to send inline "
            f"({len(b64)} b64 chars > {max_b64_chars}) and Pillow is missing. "
            "Install it with: pip install pillow"
        )

    print(
        f"  image too large ({len(b64)} b64 chars) -> downscaling to fit "
        f"{max_b64_chars} chars...",
        file=sys.stderr,
    )
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
            print(f"  downscaled to {w}x{h} ({len(b64)} b64 chars)", file=sys.stderr)
            return "image/jpeg", b64

    sys.exit("ERROR: could not shrink the image enough to fit the inline limit.")


def build_payload(args, mime, b64):
    data_uri = f"data:{mime};base64,{b64}"
    return {
        "model": args.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": args.prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "stream": args.stream,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Test an NVIDIA vision model on one image.")
    p.add_argument(
        "image",
        nargs="?",
        default=None,
        help="Path to the image file. If omitted, a file-picker dialog opens.",
    )
    p.add_argument("--model", default="moonshotai/kimi-k2.6")
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--api-key", default=None, help="NVIDIA API key (else env / .env).")
    p.add_argument("--max-tokens", type=int, default=16384)
    p.add_argument("--temperature", type=float, default=1.0)
    p.add_argument("--top-p", type=float, default=1.0)
    p.add_argument("--stream", action="store_true", help="Stream the response (SSE).")
    p.add_argument("--max-b64-chars", type=int, default=MAX_B64_CHARS)
    p.add_argument("--timeout", type=int, default=180)
    return p.parse_args()


def main():
    args = parse_args()

    image_arg = args.image
    if not image_arg:
        print("No image path given - opening file picker...")
        image_arg = pick_image_dialog()
        if not image_arg:
            sys.exit("No image selected. Pass a path or pick a file. Exiting.")

    image_path = Path(image_arg)
    if not image_path.is_file():
        sys.exit(f"ERROR: image not found: {image_path}")

    api_key = load_api_key(args.api_key)

    print(f"Image : {image_path}")
    print(f"Model : {args.model}")
    mime, b64 = encode_image(image_path, args.max_b64_chars)
    print(f"Sent  : {mime}, {len(b64)} base64 chars\n")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream" if args.stream else "application/json",
        "Content-Type": "application/json",
    }
    payload = build_payload(args, mime, b64)

    try:
        resp = requests.post(
            INVOKE_URL, headers=headers, json=payload,
            stream=args.stream, timeout=args.timeout,
        )
    except requests.RequestException as exc:
        sys.exit(f"ERROR: request failed: {exc}")

    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}:")
        print(resp.text)
        sys.exit(1)

    print("----- MODEL RESPONSE -----")
    if args.stream:
        for line in resp.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8")
            if text.startswith("data: "):
                text = text[len("data: "):]
            if text.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(text)
                print(chunk["choices"][0]["delta"].get("content", ""), end="", flush=True)
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
        print()
    else:
        data = resp.json()
        try:
            print(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError):
            print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
