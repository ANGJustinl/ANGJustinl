"""Refresh README images; publish only validated downloads and retain last good files."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
import json
import os
from pathlib import Path
import struct
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
import zlib


MAX_BYTES = 8 * 1024 * 1024
ERROR_MESSAGES = (
    "something went wrong",
    "api rate limit exceeded",
    "could not fetch",
    "failed to fetch",
    "user not found",
    "deployment has been disabled",
    "too many requests",
    "temporarily unavailable",
    "service unavailable",
    "invalid token",
    "bad credentials",
)


def validate_image(data, suffix):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("empty or oversized image")
    if suffix == ".svg":
        root = ET.fromstring(data)
        if root.tag != "{http://www.w3.org/2000/svg}svg":
            raise ValueError("response is not an SVG image")
        text = " ".join(root.itertext()).casefold()
        if any(message in text for message in ERROR_MESSAGES):
            raise ValueError("SVG contains a service error message")
    elif suffix == ".png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("response is not a PNG image")
        offset = 8
        first = True
        has_image_data = False
        while offset + 12 <= len(data):
            length = struct.unpack_from(">I", data, offset)[0]
            end = offset + length + 12
            if end > len(data):
                break
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:end - 4]
            checksum = struct.unpack_from(">I", data, end - 4)[0]
            if zlib.crc32(kind + payload) != checksum:
                raise ValueError("invalid PNG checksum")
            if first:
                if kind != b"IHDR" or length != 13:
                    raise ValueError("missing PNG header")
                width, height = struct.unpack_from(">II", payload)
                if not width or not height:
                    raise ValueError("empty PNG dimensions")
                first = False
            has_image_data |= kind == b"IDAT" and length > 0
            if kind == b"IEND":
                if length == 0 and end == len(data) and has_image_data:
                    return
                break
            offset = end
        raise ValueError("incomplete PNG image")
    else:
        raise ValueError(f"unsupported image format: {suffix}")


def download(url):
    # Older README links may use HTTP; fetch their cache over verified HTTPS.
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    request = Request(url, headers={"User-Agent": "ANGJustinl-readme-image-cache/1.0"})
    for attempt in range(2):
        try:
            with urlopen(request, timeout=25) as response:
                return response.read(MAX_BYTES + 1)
        except HTTPError as error:
            if attempt or (error.code != 429 and error.code < 500):
                raise
            time.sleep(1)


def refresh_image(root, entry):
    path = root / entry["path"]
    try:
        data = download(entry["url"])
        validate_image(data, path.suffix)
        if not path.exists() or path.read_bytes() != data:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(data)
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return entry, "updated", ""
    except (OSError, URLError, HTTPException, ValueError, ET.ParseError) as error:
        try:
            validate_image(path.read_bytes(), path.suffix)
        except (OSError, ValueError, ET.ParseError):
            return entry, "live link", str(error)
        return entry, "last good cache", str(error)


def refresh(root):
    entries = json.loads((root / ".github/readme-images.json").read_text())
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda entry: refresh_image(root, entry), entries))

    readme_path = root / "README.md"
    original = readme_path.read_text()
    readme = original
    available = 0
    summary = ["### README image cache", "", "| Image | Result |", "| --- | --- |"]
    for entry, status, error in results:
        cached_path = entry["path"]
        if status == "live link":
            readme = readme.replace(cached_path, entry["url"])
        else:
            readme = readme.replace(entry["url"], cached_path)
            available += 1

        detail = status if not error else f"{status}: {error}"
        detail = detail.replace("\n", " ").replace("|", "\\|")
        summary.append(f"| `{Path(cached_path).name}` | {detail} |")

    summary.extend(["", f"Cached images available: {available}/{len(results)}"])
    report = "\n".join(summary) + "\n"
    print(report)
    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        Path(step_summary).write_text(report)

    if readme != original:
        readme_path.write_text(readme)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=Path(__file__).resolve().parents[2],
        type=Path,
        help="Repository root directory",
    )
    options = parser.parse_args()
    refresh(options.root)


if __name__ == "__main__":
    main()
