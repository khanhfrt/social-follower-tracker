"""Public follower collector for Cloudflare Worker/D1.

Uses only publicly accessible page content. No login, cookies, proxy or fabricated counts.
Requires SITE_URL and COLLECTOR_TOKEN GitHub Actions secrets.
"""
import asyncio
import html
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse, urlunparse
from playwright.async_api import async_playwright

BASE = os.environ.get('SITE_URL', '').strip().rstrip('/')
TOKEN = os.environ.get('COLLECTOR_TOKEN', '').strip()
if not BASE.startswith('https://'):
    raise SystemExit('SITE_URL must be a full HTTPS Cloudflare Worker URL')
if not TOKEN:
    raise SystemExit('Missing COLLECTOR_TOKEN GitHub Actions secret')


def api(path, payload=None):
    url = BASE + '/api/' + path.lstrip('/')
    headers = {'Authorization': 'Bearer ' + TOKEN,
               'Accept': 'application/json', 'User-Agent': 'FollowerBI/4.0'}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method='POST' if payload is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw, status = res.read(), res.status
            content_type = res.headers.get('Content-Type', '')
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
        content_type = exc.headers.get('Content-Type', '')
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Cannot reach API /api/{path}: {exc.reason}') from exc
    print(f'API /api/{path}: HTTP {status}; type={content_type}; bytes={len(raw)}', flush=True)
    try:
        result = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f'API /api/{path} returned non-JSON (HTTP {status})') from exc
    if not isinstance(result, dict):
        raise RuntimeError(f'API /api/{path} response must be a JSON object')
    if status >= 400 or result.get('error'):
        raise RuntimeError(f'API /api/{path}: HTTP {status}; error={result.get("error", "unknown")}')
    return result


def number(raw, suffix=''):
    raw = str(raw).strip().replace('\u00a0', '').replace(' ', '')
    suffix = (suffix or '').strip().lower()
    if suffix:
        factor = 1_000_000 if suffix in ('m', 'triệu', 'million') else 1_000
        if not re.fullmatch(r'\d+(?:[.,]\d+)?', raw):
            return None
        return round(float(raw.replace(',', '.')) * factor)
    if raw.isdigit():
        return int(raw)
    if re.fullmatch(r'\d{1,3}(?:[.,]\d{3})+', raw):
        return int(re.sub(r'[.,]', '', raw))
    return None


NUM = r'(\d+(?:[.,]\d{3})*|\d+(?:[.,]\d+)?)\s*(nghìn|triệu|million|[kKmM])?'
FOLLOWERS = r'(?:người\s+theo\s+dõi|followers?)'
PATTERNS = [
    re.compile(r'(?<!\w)' + NUM + r'\s*' + FOLLOWERS + r'\b', re.I),
    re.compile(r'\b' + FOLLOWERS + r'\s*[:：]?\s*' + NUM, re.I),
]


def parse_text(text):
    if not isinstance(text, str):
        return None
    for pattern in PATTERNS:
        for m in pattern.finditer(text):
            try:
                n = number(m.group(1), m.group(2))
                if n is not None and 0 <= n <= 10_000_000_000:
                    return n
            except (ValueError, OverflowError):
                pass
    return None


def parse_tiktok_structured(source):
    # TikTok publicly embedded JSON may contain a user stats followerCount.
    # Restrict to stats objects to avoid video/hashtag counters.
    for match in re.finditer(r'"stats"\s*:\s*\{[^{}]{0,2500}?"followerCount"\s*:\s*(\d+)', source):
        return int(match.group(1))
    for match in re.finditer(r'"followerCount"\s*:\s*(\d+)', source):
        return int(match.group(1))
    return None


def facebook_urls(original):
    parsed = urlparse(original)
    path = parsed.path.rstrip('/') + '/'
    # These are public web variants, not a guaranteed way around login requirements.
    hosts = ['www.facebook.com', 'm.facebook.com', 'mbasic.facebook.com']
    return [urlunparse(('https', host, path, '', '', '')) for host in hosts]


def allowed(item):
    platform = str(item.get('platform', '')).strip().lower()
    url = str(item.get('url', '')).strip()
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    domains = {'facebook': {'facebook.com', 'www.facebook.com', 'm.facebook.com', 'mbasic.facebook.com'},
               'tiktok': {'tiktok.com', 'www.tiktok.com'}}
    return platform in domains and parsed.scheme == 'https' and host in domains[platform]


async def collect_one(context, item):
    platform = str(item['platform']).lower()
    original = str(item['url'])
    urls = facebook_urls(original) if platform == 'facebook' else [original]
    attempts = []
    for target in urls:
        page = await context.new_page()
        try:
            response = await page.goto(target, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(2500)
            final = page.url
            body = await page.locator('body').inner_text(timeout=10000)
            source = await page.content()
            login_wall = bool(re.search(r'facebook\.com/login|/login/|/login\?', final, re.I))
            login_wall = login_wall or bool(re.search(r'log in to facebook|đăng nhập vào facebook', body[:1500], re.I))
            if platform == 'tiktok':
                login_wall = login_wall or bool(re.search(r'tiktok\.com/login', final, re.I))
            count = None if login_wall else parse_text(body)
            if count is None and platform == 'tiktok' and not login_wall:
                count = parse_tiktok_structured(html.unescape(source))
            attempts.append({'host': urlparse(target).hostname, 'http': response.status if response else None,
                             'login_wall': login_wall, 'followers': count})
            if count is not None:
                return count, attempts
        except Exception as exc:
            attempts.append({'host': urlparse(target).hostname, 'error': str(exc)[:140]})
        finally:
            await page.close()
    return None, attempts


async def main():
    pages = api('pages').get('pages')
    if not isinstance(pages, list):
        raise RuntimeError('API /api/pages did not return pages array')
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
            if not allowed(item):
                print(json.dumps({'page': name, 'error': 'Invalid platform or URL'}, ensure_ascii=False), flush=True)
                continue
            count, attempts = await collect_one(context, item)
            print(json.dumps({'page': name, 'platform': item['platform'],
                              'followers': count, 'attempts': attempts}, ensure_ascii=False), flush=True)
            if count is not None:
                items.append({'id': int(item['id']), 'followers': count})
        await browser.close()
    if items:
        print('Saved:', json.dumps(api('collect', {'items': items}), ensure_ascii=False), flush=True)
    else:
        print('No verified follower counts. No fake data saved.', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
