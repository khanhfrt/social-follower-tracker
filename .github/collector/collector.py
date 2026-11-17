"""Public follower collector for Cloudflare Worker/D1.
No login, cookies, proxy, or fabricated follower counts.
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
    raise SystemExit('SITE_URL must be an HTTPS Cloudflare Worker URL')
if not TOKEN:
    raise SystemExit('Missing COLLECTOR_TOKEN GitHub Actions secret')


def api(path, payload=None):
    url = BASE + '/api/' + path
    headers = {'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/json', 'User-Agent': 'FollowerBI/5.0'}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST' if payload is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw, status = res.read(), res.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    result = json.loads(raw.decode('utf-8-sig'))
    print(f'API /api/{path}: HTTP {status}', flush=True)
    if not isinstance(result, dict) or status >= 400 or result.get('error'):
        raise RuntimeError(f'API /api/{path} failed: HTTP {status}, {str(result)[:200]}')
    return result


# Require the number to appear directly beside a follower label.
NUM = r'(\d+(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*(nghìn|triệu|million|[kKmM])?'
LABEL = r'(?:người\s+theo\s+dõi|followers?)'
PATTERNS = [
    re.compile(r'(?<!\w)' + NUM + r'\s*' + LABEL + r'\b', re.I),
    re.compile(r'\b' + LABEL + r'\s*[:：]?\s*' + NUM, re.I),
]


def parse_number(raw, suffix):
    raw = raw.replace('\u00a0', '').replace(' ', '')
    suffix = (suffix or '').lower()
    if suffix:
        if not re.fullmatch(r'\d+(?:[.,]\d+)?', raw):
            return None
        return round(float(raw.replace(',', '.')) * (1_000_000 if suffix in ('m', 'triệu', 'million') else 1_000))
    if raw.isdigit():
        return int(raw)
    if re.fullmatch(r'\d{1,3}(?:[.,]\d{3})+', raw):
        return int(re.sub(r'[.,]', '', raw))
    return None


def parse_followers(text):
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            count = parse_number(match.group(1), match.group(2))
            if count is not None and 0 <= count <= 10_000_000_000:
                return count, match.group(0)[:100]
    return None, None


def tiktok_structured(source):
    # Prefer stats within user information, not arbitrary followerCount keys in unrelated objects.
    source = html.unescape(source)
    for pat in (r'"stats"\s*:\s*\{[^{}]{0,2500}?"followerCount"\s*:\s*(\d+)',
                r'"followerCount"\s*:\s*(\d+)'):
        match = re.search(pat, source)
        if match:
            return int(match.group(1)), 'embedded followerCount'
    return None, None


def allowed(item):
    platform = str(item.get('platform', '')).lower()
    parsed = urlparse(str(item.get('url', '')))
    hosts = {'facebook': {'facebook.com', 'www.facebook.com', 'm.facebook.com', 'mbasic.facebook.com'},
             'tiktok': {'tiktok.com', 'www.tiktok.com'}}
    return platform in hosts and parsed.scheme == 'https' and parsed.hostname in hosts[platform]


def targets(item):
    url = str(item['url'])
    if str(item['platform']).lower() != 'facebook':
        return [url]
    parsed = urlparse(url)
    path = parsed.path.rstrip('/')
    if not path or path == '/':
        return [url]
    if path.endswith('/followers'):
        path = path[:-len('/followers')]
    # The V2 test found an unblocked candidate at /followers, unlike the profile URL.
    return [urlunparse(('https', 'www.facebook.com', path + '/followers', '', '', '')),
            urlunparse(('https', 'www.facebook.com', path, '', '', '')),
            urlunparse(('https', 'm.facebook.com', path, '', '', '')),
            urlunparse(('https', 'mbasic.facebook.com', path, '', '', ''))]


async def collect_one(context, item):
    platform = str(item['platform']).lower()
    attempts = []
    for target in targets(item):
        page = await context.new_page()
        try:
            response = await page.goto(target, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(2200)
            final = page.url
            body = await page.locator('body').inner_text(timeout=12000)
            login_wall = bool(re.search(r'facebook\.com/login|tiktok\.com/login|/login[/?]', final, re.I))
            login_wall = login_wall or bool(re.search(r'log in to facebook|đăng nhập vào facebook', body[:1200], re.I))
            count, evidence = (None, None) if login_wall else parse_followers(body)
            if count is None and platform == 'tiktok' and not login_wall:
                count, evidence = tiktok_structured(await page.content())
            # V2's bare '4,7K' is only a candidate. Never store it without follower context.
            attempts.append({'url': target, 'http': response.status if response else None,
                             'login_wall': login_wall, 'followers': count, 'evidence': evidence,
                             'final_url': final})
            if count is not None:
                return count, attempts
        except Exception as exc:
            attempts.append({'url': target, 'error': str(exc)[:160]})
        finally:
            await page.close()
    return None, attempts


async def main():
    pages = api('pages').get('pages')
    if not isinstance(pages, list):
        raise RuntimeError('API /api/pages did not return pages array')
    print('Pages:', len(pages), flush=True)
    if not pages:
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
            print(json.dumps({'page': name, 'platform': item['platform'], 'followers': count,
                              'attempts': attempts}, ensure_ascii=False), flush=True)
            if count is not None:
                items.append({'id': int(item['id']), 'followers': count})
        await browser.close()
    if items:
        print('Saved:', json.dumps(api('collect', {'items': items}), ensure_ascii=False), flush=True)
    else:
        print('No verified follower counts. No fake data saved.', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
