"""Public page follower collector. No login, no fabricated values."""
import asyncio, json, os, re, urllib.request, urllib.error
from playwright.async_api import async_playwright
BASE=os.environ['SITE_URL'].rstrip('/')
TOKEN=os.environ['COLLECTOR_TOKEN']
def api(path, payload=None):
    headers={'X-Collector-Token':TOKEN,'Accept':'application/json','User-Agent':'FollowerBI/3.0'}
    if payload is not None:headers['Content-Type']='application/json'
    data=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(BASE+'/api/'+path,data=data,headers=headers)
    with urllib.request.urlopen(req,timeout=30) as res:
        raw=res.read().decode()
        return json.loads(raw)
def parse(text,platform):
    # Prefer number immediately before 'followers'/'người theo dõi'; do not confuse 'đang theo dõi'.
    patterns=[r'(?<![\w])([\d][\d.,\s]*\s*(?:K|M|nghìn|triệu)?)\s*(?:người theo dõi|followers)\b',r'(?:followers|người theo dõi)\s*[:：]?\s*([\d][\d.,\s]*\s*(?:K|M|nghìn|triệu)?)']
    for pat in patterns:
        for m in re.finditer(pat,text,re.I):
            raw=m.group(1).strip().replace(' ','').lower();factor=1
            if raw.endswith('nghìn'):factor=1000;raw=raw[:-5]
            elif raw.endswith('triệu'):factor=1000000;raw=raw[:-4]
            elif raw.endswith('k'):factor=1000;raw=raw[:-1]
            elif raw.endswith('m'):factor=1000000;raw=raw[:-1]
            if factor>1:
                try:return round(float(raw.replace(',','.'))*factor)
                except ValueError:continue
            try:return int(re.sub(r'[^0-9]','',raw))
            except ValueError:continue
    return None
async def main():
    pages=api('pages.php')['pages'];print('Pages:',len(pages));items=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        context=await browser.new_context(locale='vi-VN',viewport={'width':1365,'height':900})
        for item in pages:
            page=await context.new_page()
            try:
                await page.goto(item['url'],wait_until='domcontentloaded',timeout=45000)
                await page.wait_for_timeout(4500)
                text=await page.locator('body').inner_text(timeout=15000)
                count=parse(text,item['platform'])
                print(json.dumps({'page':item['name'],'followers':count,'url':page.url},ensure_ascii=False))
                if count is not None:items.append({'id':int(item['id']),'followers':count})
            except Exception as exc:print('Error:',item['name'],str(exc)[:200])
            finally:await page.close()
        await browser.close()
    if items:print('Saved:',api('collect.php',{'items':items}))
    else:print('No verified follower values found. No fake values saved.')
if __name__=='__main__':asyncio.run(main())
