import asyncio
from patchright.async_api import async_playwright
from scrapling import Selector

async def test():
    try:
        async with async_playwright() as p:
            # Launching with stealth-like settings
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            print("Navigating to GSMArena brands page...")
            await page.goto("https://www.gsmarena.com/makers.php3", wait_until="networkidle")
            
            title = await page.title()
            print(f"Initial Title: {title}")
            
            if "Turnstile" in title:
                print("Turnstile check detected! Waiting for solve...")
                # Wait for redirect or for .main to appear
                try:
                    await page.wait_for_selector(".main", timeout=30000)
                    print("Bypassed Turnstile!")
                except:
                    print("Timed out waiting for .main")
            
            print(f"Final Title: {await page.title()}")
            content = await page.content()
            print(f"Content Length: {len(content)}")
            
            sel = Selector(content)
            brand_links = sel.css('a[href*="-phones-"]')
            print(f"Found {len(brand_links)} potential brand links.")
            
            await browser.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test())
