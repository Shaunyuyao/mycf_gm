from scrapling import Fetcher

fetcher = Fetcher(engine='stealth')
page = fetcher.get('https://www.gsmarena.com/makers.php3')
print(f"Status: {page.status}")

# Find all links
links = page.css('a')
brand_links = []
for link in links:
    href = link.attrib.get('href', '')
    if '-phones-' in href and '.php' in href:
        text = link.css('::text').get() or ""
        brand_links.append((text.strip(), href))

print(f"Found {len(brand_links)} potential brand links.")
for name, link in brand_links[:20]:
    print(f"{name}: {link}")

# Try to find the container
# Often it's a table
tables = page.css('table')
print(f"Found {len(tables)} tables.")
