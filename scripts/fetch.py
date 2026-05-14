#!/usr/bin/env python3
"""
Fetch a full webpage including all assets (CSS, JS, images, fonts, etc.)
using Playwright, save everything locally, rewrite links, and package into a ZIP.
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

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def sanitize_filename(path: str) -> str:
    """Sanitize a path to be safe for filesystem."""
    # Replace problematic characters
    safe = path.replace('\\', '/').replace(':', '_').replace('?', '_').replace('*', '_')
    safe = safe.replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
    # Remove leading slashes
    if safe.startswith('/'):
        safe = safe[1:]
    # Prevent directory traversal
    parts = safe.split('/')
    parts = [p for p in parts if p and p not in ('.', '..')]
    return '/'.join(parts) if parts else 'index'

def get_url_hash(url: str) -> str:
    """Return a short hash of the URL for unique naming."""
    return hashlib.md5(url.encode()).hexdigest()[:12]

def download_and_save_assets(page, output_dir: Path, url_map: dict):
    """
    Intercept and save all fetched resources.
    Returns a mapping from original URL to local relative path.
    """
    asset_dir = output_dir / 'assets'
    asset_dir.mkdir(exist_ok=True)

    # We'll collect resources via page.on('response') that we set before goto
    # This function will be called from the main script where we already registered the handler.
    # Better to collect inside the handler and store in a mutable dict passed to this function.
    # For simplicity, we assume the handler already populated `url_map` with raw buffers.
    pass

def rewrite_html(html: str, asset_map: dict) -> str:
    """Rewrite all src, href, srcset to point to local asset paths."""
    soup = BeautifulSoup(html, 'lxml')

    # Rewrite tags with src attribute
    for tag in soup.find_all(src=True):
        src = tag['src']
        if src in asset_map:
            tag['src'] = asset_map[src]

    # Rewrite tags with href attribute (but skip anchors that are not resources)
    for tag in soup.find_all(href=True):
        href = tag['href']
        if href in asset_map and tag.name not in ('a', 'link'):
            tag['href'] = asset_map[href]

    # Rewrite srcset attributes (images)
    for tag in soup.find_all(srcset=True):
        srcset = tag['srcset']
        new_parts = []
        for part in srcset.split(','):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            url = tokens[0]
            if url in asset_map:
                tokens[0] = asset_map[url]
            new_parts.append(' '.join(tokens))
        tag['srcset'] = ', '.join(new_parts)

    return str(soup)

def main():
    url = os.environ.get('FETCH_URL')
    if not url:
        print("ERROR: FETCH_URL environment variable not set")
        sys.exit(1)

    wait_until = os.environ.get('WAIT_UNTIL', 'networkidle')
    extra_wait = int(os.environ.get('EXTRA_WAIT', '2'))

    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    snapshot_name = f"snapshot_{timestamp}"
    work_dir = Path(f"snapshots/{snapshot_name}")
    work_dir.mkdir(parents=True, exist_ok=True)

    asset_map = {}  # url -> local relative path
    asset_buffers = {}  # url -> bytes

    print(f"Fetching: {url}")
    print(f"Wait until: {wait_until}, extra wait: {extra_wait}s")

    success = False
    final_html = ""
    error_msg = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = context.new_page()

        # Intercept all responses to capture assets
        def on_response(response):
            try:
                req_url = response.url
                # Skip data URLs
                if req_url.startswith('data:'):
                    return
                # Skip same-page anchors
                if req_url == url or req_url.startswith('#'):
                    return
                body = response.body()
                if body:
                    asset_buffers[req_url] = body
            except Exception:
                pass

        page.on('response', on_response)

        try:
            print("Navigating...")
            page.goto(url, wait_until=wait_until, timeout=90000)
            if extra_wait > 0:
                print(f"Waiting extra {extra_wait}s for dynamic content...")
                time.sleep(extra_wait)
            final_html = page.content()
            success = True
        except Exception as e:
            error_msg = str(e)
            print(f"Navigation error: {error_msg}")
            # Try to get whatever content is available
            try:
                final_html = page.content()
            except:
                final_html = "<html><body><h1>Fetch failed</h1></body></html>"

        browser.close()

    # Save all captured assets to disk
    print(f"Saving {len(asset_buffers)} assets...")
    assets_saved = 0
    for asset_url, data in asset_buffers.items():
        try:
            parsed = urlparse(asset_url)
            # Create a unique but readable filename based on path + query
            path_part = sanitize_filename(parsed.path)
            if parsed.query:
                path_part += '_' + sanitize_filename(parsed.query)
            if not path_part or path_part == '/':
                path_part = 'index'
            # Add hash to avoid collisions
            hash_suffix = get_url_hash(asset_url)
            base, ext = os.path.splitext(path_part)
            if not ext:
                ext = '.bin'
            filename = f"{base}_{hash_suffix}{ext}"
            # Keep directory structure from domain? Use a flat assets folder for simplicity
            local_path = f"assets/{filename}"
            full_path = work_dir / local_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, 'wb') as f:
                f.write(data)
            asset_map[asset_url] = local_path
            assets_saved += 1
        except Exception as e:
            print(f"Failed to save asset {asset_url}: {e}")

    print(f"Saved {assets_saved} assets locally")

    # Rewrite HTML to use local assets
    if final_html:
        rewritten_html = rewrite_html(final_html, asset_map)
        index_path = work_dir / 'index.html'
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(rewritten_html)
        print("Saved rewritten index.html")
    else:
        print("WARNING: No HTML content to save")

    # Save metadata
    metadata = {
        "original_url": url,
        "timestamp": datetime.utcnow().isoformat(),
        "success": success,
        "error": error_msg,
        "assets_count": len(asset_buffers),
        "assets_saved": assets_saved
    }
    with open(work_dir / 'metadata.json', 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2)

    # Create ZIP archive
    zip_path = Path(f"snapshots/{snapshot_name}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file_path in work_dir.rglob('*'):
            if file_path.is_file():
                arcname = file_path.relative_to(work_dir.parent)
                zf.write(file_path, arcname)

    print(f"Created ZIP archive: {zip_path}")

    # Set GitHub Actions outputs
    with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
        out.write(f"zip_path={zip_path}\n")
        out.write(f"snapshot_dir={work_dir}\n")
        out.write(f"success={str(success).lower()}\n")

    if not success:
        sys.exit(1)

if __name__ == "__main__":
    main()
