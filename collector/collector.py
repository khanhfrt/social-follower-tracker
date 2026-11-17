"""Collect publicly visible followers and send verified counts to PHP API."""
import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from playwright.async_api import async_playwright

BASE = os.environ['SITE_URL'].rstrip('/')
TOKEN = os.environ['COLLECTOR_TOKEN']


def api(path, payload=None):
    url = BASE + '/api/' + path
    headers = {
        'X-Collector-Token': TOKEN,
        'Accept': 'application/json',
        'User-Agent': 'FollowerBI/3.0',
    }
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
            status = res.status
            content_type = res.headers.get('Content-Type', '')
            final_url = res.url
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        content_type = exc.headers.get('Content-Type', '')
        final_url = exc.url
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Cannot reach PHP API ({path}): {exc.reason}') from exc

    print(f'API {path}: HTTP {status}; type={content_type}; bytes={len(raw)}; redirected={final_url != url}', flush=True)
    if not raw.strip():
        raise RuntimeError(f'API {path} returned an empty response. Check SITE_URL and hosting rules.')
    try:
        result = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as exc:
        looks_html = raw.lstrip().lower().startswith((b'<!doctype', b'<html'))
        reason = 'HTML page (possibly hosting protection/challenge)' if looks_html else 'non-JSON content'
        raise RuntimeError(f'API {path} returned {reason}, not JSON (HTTP {status}, {content_type}). Check hosting and API URL.') from exc
    if status >= 400:
        raise RuntimeError(f'API {path} rejected request: HTTP {status}; error={result.get("error", "unknown") if isinstance(result, dict) else "unknown"}')
    if not isinstance(result, dict):
        raise RuntimeError(f'API {path} JSON response must be an object.')
    if result.get('error'):
        raise RuntimeError(f'API {path} error: {result["error"]}')
    return result


def parse(text, platform):
    # Accept only visible follower labels, not "following" labels.
    patterns = [
        r'(?<![\w])([\d][\d.,\s]*\s*(?:K|M|nghìn|triệu)?)\s*(?:người theo dõi|followers)\b',
        r'(?:followers|người theo dõi)\s*[:：]?\s*([\d][\d.,\s]*\s*(?:K|M|nghìn|triệu)?)',
    ]
    for pat in patterns:
        for match in re.finditer(pat, text, re.I):
            raw = match.group(1).strip().replace(' ', '').lower()
            factor = 1
            for suffix, multiplier in [('nghìn', 1000), ('triệu', 1000000), ('k', 1000), ('m', 1000000)]:
                if raw.endswith(suffix):
                    factor = multiplier
                    raw = raw[:-len(suffix)]
                    break
            try:
                if factor > 1:
                    return round(float(raw.replace(',', '.')) * factor)
                return int(re.sub(r'[^0-9]', '', raw))
            except ValueError:
                continue
    return None


async def main():
    response = api('pages.php')
    pages = response.get('pages')
    if not isinstance(pages, list):
        raise RuntimeError('pages.php did not return a pages array.')
    print('Pages:', len(pages), flush=True)
    items = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale='vi-VN', viewport={'width': 1365, 'height': 900})
        for item in pages:
            page = await context.new_page()
            try:
                await page.goto(item['url'], wait_until='domcontentloaded', timeout=45000)
                await page.wait_for_timeout(4500)
                body = await page.locator('body').inner_text(timeout=15000)
                count = parse(body, item['platform'])
                print(json.dumps({'page': item['name'], 'followers': count, 'url': page.url}, ensure_ascii=False), flush=True)
                if count is not None:
                    items.append({'id': int(item['id']), 'followers': count})
            except Exception as exc:
                print('Page error:', item.get('name', '?'), str(exc)[:200], flush=True)
            finally:
                await page.close()
        await browser.close()
    if items:
        print('Saved:', api('collect.php', {'items': items}), flush=True)
    else:
        print('No verified follower values found. No fake values saved.', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
