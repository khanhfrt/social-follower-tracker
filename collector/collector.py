"""Collect publicly visible follower counts and submit to Cloudflare Worker API.

Required GitHub Actions secrets:
  SITE_URL=https://your-worker.workers.dev
  COLLECTOR_TOKEN=<same token configured in Cloudflare Worker>

Expected endpoints:
  GET  /api/pages   -> {"pages": [{"id": 1, "name": "...", "platform": "facebook", "url": "..."}]}
  POST /api/collect -> JSON acknowledgment; body {"items": [{"id": 1, "followers": 123}]}
"""
import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from playwright.async_api import async_playwright

BASE = os.environ.get('SITE_URL', '').strip().rstrip('/')
TOKEN = os.environ.get('COLLECTOR_TOKEN', '').strip()
if not BASE.startswith('https://'):
    raise SystemExit('SITE_URL must be a full HTTPS Cloudflare Worker URL')
if not TOKEN:
    raise SystemExit('Missing COLLECTOR_TOKEN GitHub Actions secret')


def api(path, payload=None):
    url = BASE + '/api/' + path.lstrip('/')
    headers = {
        'X-Collector-Token': TOKEN,
        'Accept': 'application/json',
        'User-Agent': 'FollowerBI/3.0',
    }
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST' if payload is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw, status = res.read(), res.status
            content_type, final_url = res.headers.get('Content-Type', ''), res.url
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
        content_type, final_url = exc.headers.get('Content-Type', ''), exc.url
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Cannot reach API /api/{path}: {exc.reason}') from exc
    print(f'API /api/{path}: HTTP {status}; type={content_type}; bytes={len(raw)}; redirected={final_url != url}', flush=True)
    if not raw.strip():
        raise RuntimeError(f'API /api/{path} returned empty body')
    try:
        result = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f'API /api/{path} returned non-JSON (HTTP {status}, {content_type}). Check Worker deployment and URL.') from exc
    if status >= 400:
        err = result.get('error', 'unknown') if isinstance(result, dict) else 'unknown'
        raise RuntimeError(f'API /api/{path} rejected request (HTTP {status}): {err}')
    if not isinstance(result, dict):
        raise RuntimeError(f'API /api/{path} must return a JSON object')
    if result.get('error'):
        raise RuntimeError(f'API /api/{path} error: {result["error"]}')
    return result


NUMBER = r'(\d+(?:[.,]\d{3})*|\d+(?:[.,]\d+)?)\s*(nghìn|triệu|[kKmM])?'
FOLLOWER = r'(?:người\s+theo\s+dõi|followers?)'
PATTERNS = [
    re.compile(r'(?<!\w)' + NUMBER + r'\s*' + FOLLOWER + r'\b', re.I),
    re.compile(r'\b' + FOLLOWER + r'\s*[:：]?\s*' + NUMBER, re.I),
]


def normalize_number(raw, suffix):
    raw = raw.strip()
    suffix = (suffix or '').lower()
    if suffix:
        multiplier = 1_000_000 if suffix in ('m', 'triệu') else 1_000
        # e.g. 1,2K, 1.2M
        return round(float(raw.replace(',', '.')) * multiplier)
    # e.g. 12.345 or 12,345 = thousands separators; reject ambiguous decimals
    if re.fullmatch(r'\d{1,3}(?:[.,]\d{3})+', raw):
        return int(re.sub(r'[.,]', '', raw))
    if raw.isdigit():
        return int(raw)
    return None


def parse(text, platform):
    if not isinstance(text, str):
        return None
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            try:
                count = normalize_number(match.group(1), match.group(2))
                if count is not None and 0 <= count <= 10_000_000_000:
                    return count
            except (ValueError, OverflowError):
                pass
    return None


async def main():
    response = api('pages')
    pages = response.get('pages')
    if not isinstance(pages, list):
        raise RuntimeError('API /api/pages did not return {"pages": [...]}')
    print('Pages:', len(pages), flush=True)
    if not pages:
        print('No pages configured in D1; nothing to collect.', flush=True)
        return

    items = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale='vi-VN', viewport={'width': 1365, 'height': 900})
        for item in pages:
            name = str(item.get('name', '?'))
            platform = str(item.get('platform', '')).lower()
            url = str(item.get('url', ''))
            if platform not in ('facebook', 'tiktok') or not url.startswith(('https://www.facebook.com/', 'https://facebook.com/', 'https://www.tiktok.com/', 'https://tiktok.com/')):
                print(f'Skipping invalid page: {name} ({platform})', flush=True)
                continue
            page = await context.new_page()
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=45000)
                await page.wait_for_timeout(4500)
                body = await page.locator('body').inner_text(timeout=15000)
                count = parse(body, platform)
                login_wall = bool(re.search(r'đăng nhập vào facebook|log in to facebook|log in to tiktok', body, re.I))
                print(json.dumps({'page': name, 'platform': platform, 'followers': count, 'login_wall': login_wall, 'final_url': page.url}, ensure_ascii=False), flush=True)
                if count is not None and not login_wall:
                    items.append({'id': int(item['id']), 'followers': count})
            except Exception as exc:
                print(f'Page error: {name}: {str(exc)[:250]}', flush=True)
            finally:
                await page.close()
        await browser.close()
    if items:
        print('Saved:', json.dumps(api('collect', {'items': items}), ensure_ascii=False), flush=True)
    else:
        print('No verified follower counts. No fake data saved.', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
