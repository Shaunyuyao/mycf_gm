from scrapling import Fetcher

fetcher = Fetcher()
page = fetcher.get('https://httpbin.org/get')
print(f"Status: {page.status}")
print(f"URL: {page.url}")

# Check for common names
for attr in ['text', 'content', 'body', 'raw', 'data']:
    if hasattr(page, attr):
        val = getattr(page, attr)
        print(f"- {attr}: {type(val)}")
        try:
            print(f"  Len: {len(val)}")
            if len(val) > 0:
                print(f"  Sample: {str(val)[:100]}")
        except:
            pass

print("\nFull dir(page):")
print(dir(page))
