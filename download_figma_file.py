#!/usr/bin/env python3
"""
Download a Figma file JSON for use with figma_to_prototype.py.

Usage:
  FIGMA_TOKEN=figd_xxx python3 download_figma_file.py \
      --figma-url "https://www.figma.com/design/FILE_KEY/Name?node-id=1-2" \
      --output figma_file.json
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def extract_file_key(figma_url):
    parsed = urllib.parse.urlparse(figma_url)
    parts = [part for part in parsed.path.split("/") if part]

    if parsed.netloc not in {"figma.com", "www.figma.com"}:
        raise ValueError("URL must be a figma.com URL")

    if len(parts) >= 2 and parts[0] in {"design", "file", "proto"}:
        return parts[1]

    raise ValueError("Could not find a Figma file key in the URL")


def download_figma_file(file_key, token):
    url = f"https://api.figma.com/v1/files/{file_key}"
    req = urllib.request.Request(url, headers={"X-Figma-Token": token})

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Figma API error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def write_json_file(data, output_path):
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write("\n")


def validate_json_file(path):
    try:
        with open(path, "r", encoding="utf-8", newline=None) as f:
            json.load(f)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Downloaded JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc


def main():
    parser = argparse.ArgumentParser(description="Download Figma file JSON.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--figma-url", help="Full Figma design/file/prototype URL")
    source.add_argument("--file-key", help="Figma file key")
    parser.add_argument("--figma-token", default=os.environ.get("FIGMA_TOKEN", ""), help="Figma personal access token")
    parser.add_argument("--output", default="figma_file.json", help="Output JSON file path")
    args = parser.parse_args()

    if not args.figma_token:
        parser.error("Pass --figma-token or set FIGMA_TOKEN")

    file_key = args.file_key or extract_file_key(args.figma_url)
    print(f"Downloading Figma file {file_key}...")

    data = download_figma_file(file_key, args.figma_token)

    write_json_file(data, args.output)
    validate_json_file(args.output)

    print(f"Saved and validated {data.get('name', 'Figma file')} to {args.output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
