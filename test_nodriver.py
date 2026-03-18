import nodriver as uc
import asyncio
from scrapling import Selector

async def test():
    print("Starting nodriver...")
    browser = await uc.start()
    try:
        print("Navigating to GSMArena brands page...")
        page = await browser.get('https://www.gsmarena.com/makers.php3')
        
        # Wait for potential Turnstile or page load
        print("Waiting for page load...")
        # nodriver handles many things automatically, but let's wait a bit
        await asyncio.sleep(10) 
        
        title = await page.evaluate("document.title")
        print(f"Page Title: {title}")
        
        content = await page.get_content()
        print(f"Content Length: {len(content)}")
        
        if "Turnstile" in content:
            print("Turnstile check still detected.")
        else:
            print("Turnstile bypass might have worked!")
            
        sel = Selector(content)
        brand_links = sel.css('a[href*="-phones-"]')
        print(f"Found {len(brand_links)} potential brand links.")
        
    finally:
        browser.stop()

if __name__ == "__main__":
    uc.loop().run_until_complete(test())
