import asyncio
from scrapling import AsyncFetcher

async def test():
    try:
        fetcher = AsyncFetcher(engine='stealth')
        print("Fetching GSMArena brands...")
        page = await fetcher.get('https://www.gsmarena.com/makers.php3')
        print(f"Status: {page.status}")
        print(f"Body length: {len(page.body)}")
        print(f"Text snippet: {page.text[:200]}")
        
        with open('async_response.html', 'w', encoding='utf-8') as f:
            f.write(page.text)
            
        brand_links = page.css('a[href*="-phones-"]')
        print(f"Found {len(brand_links)} potential brand links.")
        
        await fetcher.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test())
