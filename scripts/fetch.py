#!/usr/bin/env python3
"""
Fetch a full webpage (HTML + all CSS/JS/images/fonts) and pack into a ZIP.
No external dependencies except Playwright and BeautifulSoup4.
"""

import os
import sys
import json
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse, urljoin
from datetime import datetime
import hashlib

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def safe_filename(path: str) -> str:
    """Convert a URL path into a safe filesystem name."""
    # Remove query string
    path = path.split('?')[0]
    # Replace dangerous chars
    for ch in '\\/*?"<>|:':
        path = path.replace(ch, '_')
    # Remove leading/trailing dots and spaces
    path = path.strip('. ')
    if not path or path == '/':
        path = 'index'
    if path.startswith('/'):
        path = path[1:]
    # Keep extension if present
    return path

def url_to_local_path(url: str, asset_dir: Path, used_names: set) -> str:
    """Generate a unique local path for a given URL inside asset_dir."""
    parsed = urlparse(url)
    base = safe_filename(parsed.path)
    if not base:
        base = 'resource'
    # Ensure unique name
    counter = 1
    candidate = base
    while candidate in used_names:
        name, ext = os.path.splitext(base)
        candidate = f"{name}_{counter}{ext}"
        counter += 1
    used_names.add(candidate)
    return str(asset_dir / candidate)

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    url = os.environ.get('FETCH_URL')
    if not url:
        print("ERROR: FETCH_URL environment variable is missing.")
        sys.exit(1)

    wait_until = os.environ.get('WAIT_UNTIL', 'networkidle')
    extra_wait = int(os.environ.get('EXTRA_WAIT', '2'))

    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    snapshot_name = f"fullpage_{timestamp}"
    base_dir = Path.cwd() / 'fetched_pages'
    work_dir = base_dir / snapshot_name
    work_dir.mkdir(parents=True, exist_ok=True)

    asset_dir = work_dir / 'assets'
    asset_dir.mkdir(exist_ok=True)

    asset_map = {}          # original_url -> local relative path (to index.html)
    asset_buffers = {}      # original_url -> bytes
    used_names = set()

    print(f"🔍 Fetching: {url}")
    print(f"⏱ Wait until: {wait_until}, extra wait: {extra_wait}s")

    success = False
    final_html = ""
    error_msg = None

    # Launch browser
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = context.new_page()

            # Capture all responses
            def on_response(response):
                try:
                    req_url = response.url
                    if req_url.startswith('data:'):
                        return
                    body = response.body()
                    if body:
                        asset_buffers[req_url] = body
                except Exception as e:
                    # Silently ignore failures for individual assets
                    pass

            page.on('response', on_response)

            try:
                print("🌐 Navigating...")
                page.goto(url, wait_until=wait_until, timeout=90000)
                if extra_wait > 0:
                    print(f"⏳ Waiting extra {extra_wait}s for JS execution...")
                    time.sleep(extra_wait)
                final_html = page.content()
                success = True
                print("✅ Page loaded successfully.")
            except Exception as e:
                error_msg = str(e)
                print(f"⚠️ Navigation error: {error_msg}")
                # Still try to get partial content
                final_html = page.content() if page else "<html><body><h1>Fetch failed</h1></body></html>"

            browser.close()
    except Exception as e:
        error_msg = f"Browser launch failed: {e}"
        print(error_msg)
        final_html = "<html><body><h1>Browser error</h1></body></html>"

    # ------------------------------------------------------------------
    # Save all captured assets
    # ------------------------------------------------------------------
    print(f"💾 Saving {len(asset_buffers)} captured assets...")
    saved_assets = 0
    for asset_url, data in asset_buffers.items():
        try:
            local_rel = url_to_local_path(asset_url, Path('assets'), used_names)
            full_path = work_dir / local_rel
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, 'wb') as f:
                f.write(data)
            asset_map[asset_url] = local_rel   # e.g. "assets/stylesheet.css"
            saved_assets += 1
        except Exception as e:
            print(f"❌ Failed to save {asset_url}: {e}")

    print(f"📦 Saved {saved_assets} assets locally.")

    # ------------------------------------------------------------------
    # Rewrite HTML to point to local assets
    # ------------------------------------------------------------------
    if final_html:
        try:
            soup = BeautifulSoup(final_html, 'html.parser')
            # Rewrite src
            for tag in soup.find_all(src=True):
                src = tag['src']
                if src in asset_map:
                    tag['src'] = asset_map[src]
            # Rewrite href (but skip <a> tags that are hyperlinks, keep only resources)
            for tag in soup.find_all(href=True):
                href = tag['href']
                if href in asset_map and tag.name not in ('a', 'link'):
                    tag['href'] = asset_map[href]
            # Rewrite srcset (images with multiple resolutions)
            for tag in soup.find_all(srcset=True):
                srcset = tag['srcset']
                parts = []
                for part in srcset.split(','):
                    part = part.strip()
                    if not part:
                        continue
                    tokens = part.split()
                    if tokens and tokens[0] in asset_map:
                        tokens[0] = asset_map[tokens[0]]
                    parts.append(' '.join(tokens))
                tag['srcset'] = ', '.join(parts)
            final_html = str(soup)
        except Exception as e:
            print(f"⚠️ HTML rewriting failed: {e}")

        # Save index.html
        index_path = work_dir / 'index.html'
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(final_html)
        print("✅ Saved index.html with local asset references.")
    else:
        print("❌ No HTML content to save.")
        # Write a placeholder
        with open(work_dir / 'index.html', 'w') as f:
            f.write("<html><body><h1>Failed to retrieve page</h1></body></html>")

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    metadata = {
        "original_url": url,
        "timestamp": datetime.utcnow().isoformat(),
        "success": success,
        "error": error_msg,
        "total_assets_captured": len(asset_buffers),
        "assets_saved": saved_assets
    }
    with open(work_dir / 'metadata.json', 'w') as f:
        json.dump(metadata, f, indent=2)

    # ------------------------------------------------------------------
    # Create ZIP archive
    # ------------------------------------------------------------------
    zip_filename = f"{snapshot_name}.zip"
    zip_path = base_dir / zip_filename
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path in work_dir.rglob('*'):
                if file_path.is_file():
                    arcname = file_path.relative_to(work_dir)
                    zf.write(file_path, arcname)
        print(f"🎉 ZIP archive created: {zip_path}")
    except Exception as e:
        print(f"❌ Failed to create ZIP: {e}")
        zip_path = ""

    # ------------------------------------------------------------------
    # Write outputs for GitHub Actions (or local fallback)
    # ------------------------------------------------------------------
    output_file = os.environ.get('GITHUB_OUTPUT')
    if output_file:
        with open(output_file, 'a') as f:
            f.write(f"ZIP_PATH={zip_path}\n")
            f.write(f"SNAPSHOT_DIR={work_dir}\n")
    else:
        # Local run: write to files for debugging
        with open('zip_path.txt', 'w') as f:
            f.write(str(zip_path))
        with open('snapshot_dir.txt', 'w') as f:
            f.write(str(work_dir))
        print("GITHUB_OUTPUT not set, written to zip_path.txt and snapshot_dir.txt")

    # Exit with error if fetch failed
    if not success and not final_html:
        sys.exit(1)

if __name__ == "__main__":
    main()
