import asyncio, json, re, os, pathlib, datetime
from playwright.async_api import async_playwright

TARGET = os.getenv('TARGET_PAGE','https://www.facebook.com/FPTShop37LTT')
OUT=pathlib.Path('results'); OUT.mkdir(exist_ok=True)
URLS=[TARGET.rstrip('/'), TARGET.rstrip('/')+'/followers', TARGET.replace('www.facebook.com','m.facebook.com').rstrip('/')]
PATTERNS=[r'([\d][\d.,\s]*\s*(?:K|M|nghìn|triệu)?)\s*(?:người theo dõi|followers)',r'(?:người theo dõi|followers)\s*[:：]?\s*([\d][\d.,\s]*\s*(?:K|M|nghìn|triệu)?)']
async def main():
  findings=[]
  async with async_playwright() as p:
    browser=await p.chromium.launch(headless=True)
    context=await browser.new_context(locale='vi-VN',viewport={'width':1440,'height':1000})
    for idx,url in enumerate(URLS,1):
      page=await context.new_page()
      item={'attempt':idx,'requested_url':url,'timestamp_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
      try:
        response=await page.goto(url,wait_until='domcontentloaded',timeout=45000)
        await page.wait_for_timeout(6000)
        item['final_url']=page.url; item['http_status']=response.status if response else None
        await page.screenshot(path=str(OUT/f'attempt_{idx}.png'),full_page=True,timeout=20000)
        body=await page.locator('body').inner_text(timeout=15000)
        (OUT/f'attempt_{idx}.txt').write_text(body,encoding='utf-8')
        item['login_wall']=bool(re.search(r'đăng nhập vào facebook|log in to facebook|email hoặc số di động',body,re.I))
        item['matches']=[m.group(1).strip() for pat in PATTERNS for m in re.finditer(pat,body,re.I)]
        item['status']='login_required' if item['login_wall'] else ('candidate_found' if item['matches'] else 'not_found')
      except Exception as exc:
        item['status']='error';item['error']=str(exc)
      findings.append(item)
      print(json.dumps(item,ensure_ascii=False))
      await page.close()
    await browser.close()
  (OUT/'result.json').write_text(json.dumps({'target':TARGET,'attempts':findings,'note':'Candidate matches require screenshot verification before recording a numeric follower count.'},ensure_ascii=False,indent=2),encoding='utf-8')
asyncio.run(main())
