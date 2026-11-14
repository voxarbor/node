"""Regression check: the Озвучити page renders with settings saved by older UI versions (browser localStorage).
docker run --rm --network host -v $PWD/tests:/w:ro -v <file with TTS_UI_PASSWORD=...>:/s/tts-ui.env:ro \
  mcr.microsoft.com/playwright/python:v1.55.0-noble sh -c "pip install -q playwright==1.55.0; python /w/ui_saved_settings.py"
"""
import asyncio
from playwright.async_api import async_playwright
PW = [l.split("=",1)[1].strip() for l in open("/s/tts-ui.env") if l.startswith("TTS_UI_PASSWORD=")][0]
STATES = {
  "fresh": None,
  "v041": {"engine":"holos","voice":"Speaker_53","speed":1,"pause_ms":150,"verbalize":True,"english":True,"clean":True,"response_format":"mp3"},
  "brackets-voice-old": {"engine":"holos","voice":"Speaker_53","brackets":"voice","brackets_voice":"Speaker_30","brackets_gain_db":-3},
  "brackets-announce-old": {"engine":"holos","voice":"Speaker_1","brackets":"announce"},
  "extra-voice": {"engine":"holos","voice":"Speaker_53","extra_voice":"Speaker_30","brackets":"aside","brackets_voice_on":True},
  "styletts-single": {"engine":"styletts2","voice":"Марина Панас","model":"single","extra_voice":"Марина Панас"},
  "voice-gone": {"engine":"holos","voice":"Speaker_999","extra_voice":"Speaker_998"},
  "nulls": {"engine":None,"voice":None,"speed":None,"pause_ms":None,"brackets":None},
}
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width":1920,"height":1080})
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:8774/"); await page.wait_for_timeout(1500)
        await page.fill("input[type=password]", PW); await page.keyboard.press("Enter"); await page.wait_for_timeout(2500)
        for name, st in STATES.items():
            errs = []
            h1 = lambda e: errs.append(f"{e}")
            page.on("pageerror", h1)
            await page.evaluate("s => s ? localStorage.setItem('ozv.settings.v1', JSON.stringify(s)) : localStorage.removeItem('ozv.settings.v1')", st)
            await page.goto("http://127.0.0.1:8774/#/speak"); await page.reload(); await page.wait_for_timeout(3500)
            n = await page.locator("textarea").count()
            print(f"{name}: textarea={n} pageerrors={errs[:2]}")
            assert n == 1 and not errs, name
            page.remove_listener("pageerror", h1)
        await b.close()
    print("ALL OK")
asyncio.run(main())
