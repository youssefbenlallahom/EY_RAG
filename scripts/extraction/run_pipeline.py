import argparse
import glob
import os
import subprocess
import sys
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract PDFs into Markdown and JSON with opendataloader-pdf."
    )
    parser.add_argument("--input-dir", default="data", help="Folder containing source PDF files.")
    parser.add_argument("--output-dir", default="output", help="Folder for extracted JSON/Markdown/images.")
    parser.add_argument(
        "--jdk-bin",
        default=r"C:\Program Files\Java\jdk-25\bin",
        help="Optional JDK bin folder to prepend to PATH.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to run opendataloader_pdf.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    input_dir = os.path.abspath(args.input_dir)
    output_dir = os.path.abspath(args.output_dir)

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Find all PDFs
    pdf_files = glob.glob(os.path.join(input_dir, "*.pdf"))
    print(f"Found {len(pdf_files)} PDF files to process.")

    # Set up environment with the configured JDK when present.
    env = os.environ.copy()
    if args.jdk_bin and os.path.isdir(args.jdk_bin):
        env["PATH"] = args.jdk_bin + os.path.pathsep + env["PATH"]

    total_start_time = time.time()
    success_count = 0

    for idx, pdf_path in enumerate(pdf_files, 1):
        filename = os.path.basename(pdf_path)
        print(f"\n[{idx}/{len(pdf_files)}] Processing: {filename}")

        start_time = time.time()

        cmd = [
            args.python,
            "-m",
            "opendataloader_pdf",
            pdf_path,
            "-o",
            output_dir,
            "-f",
            "markdown,json",
            "--markdown-with-html",
            "--markdown-page-separator",
            "<!-- PAGE %page-number% -->",
        ]

        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
            elapsed = time.time() - start_time
            print(f"Successfully processed {filename} in {elapsed:.2f} seconds.")
            success_count += 1
            # Print brief logs from the tool if any
            for line in result.stderr.splitlines():
                if "INFO:" in line or "WARNING:" in line:
                    print(f"  {line}")
        except subprocess.CalledProcessError as e:
            print(f"Error processing {filename}:")
            print(e.stderr)
            continue

    total_elapsed = time.time() - total_start_time
    print(f"\nBatch processing complete: {success_count}/{len(pdf_files)} successful.")
    print(f"Total time elapsed: {total_elapsed:.2f} seconds.")


if __name__ == "__main__":
    main()
