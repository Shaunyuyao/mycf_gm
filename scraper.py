from scrapling import StealthyFetcher, Selector
import json
import time
import os
import re
import requests
import xml.etree.ElementTree as ET

class GSMArenaScraper:
    """
    GSMArena Scraper using Sitemap discovery and Scrapling.
    Supports multi-domain cookie injection for bypassing Cloudflare Turnstile.
    """
    BASE_URL = "https://www.gsmarena.com/"
    SITEMAP_URL = "https://www.gsmarena.com/sitemaps/phones.xml"
    
    def __init__(self, cookies=None):
        self.fetcher = StealthyFetcher()
        self.cookies = cookies
        if self.cookies:
            try:
                formatted_cookies = []
                for domain, domain_cookies in self.cookies.items():
                    clean_domain = domain.replace("https://", "").replace("http://", "")
                    for name, value in domain_cookies.items():
                        formatted_cookies.append({
                            "name": name,
                            "value": value,
                            "domain": clean_domain if clean_domain.startswith(".") else f".{clean_domain}"
                        })
                
                self.fetcher.configure(cookies=formatted_cookies)
                print(f"Configured with {len(formatted_cookies)} cookies.")
            except Exception as e:
                print(f"Warning: Could not configure cookies: {e}")

    def discover_device_urls(self):
        """Discovers over 14,000 device URLs from the unprotected sitemap."""
        print(f"Discovering device URLs from sitemap: {self.SITEMAP_URL}...")
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}
            response = requests.get(self.SITEMAP_URL, headers=headers, timeout=30)
            if response.status_code != 200: return []
            
            root = ET.fromstring(response.content)
            urls = []
            ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
            for url_tag in root.findall('s:url', ns):
                loc_tag = url_tag.find('s:loc', ns)
                if loc_tag is not None:
                    loc = loc_tag.text
                    if re.search(r'-\d+\.php$', loc) and '-pictures-' not in loc and '/related.php3' not in loc:
                        urls.append(loc)
            
            print(f"Found {len(urls)} URLs.")
            return urls
        except Exception as e:
            print(f"Error: {e}")
            return []

    def fetch_page(self, url):
        """Fetches a page and handles Cloudflare check detection."""
        try:
            print(f"Fetching {url}...")
            page = self.fetcher.fetch(url)
            
            if "Turnstile" in page.text or page.status == 403:
                print(f"Warning: Cloudflare/Turnstile blocked access to {url}")
                return None
            return page
        except Exception as e:
            print(f"Error: {e}")
            return None

    def fetch_device_specs(self, device_url):
        """Extracts specs for a device."""
        page = self.fetch_page(device_url)
        if not page: return None
        
        specs = {}
        model = page.css(".specs-phone-name-title::text").get()
        if not model: return None
            
        specs["model"] = model.strip()
        brand_el = page.css(".breadcrumb a[href*='-phones-']::text").get()
        specs["brand"] = brand_el.strip() if brand_el else "Unknown"
        specs["url"] = device_url
        
        tables = page.css("table")
        for table in tables:
            section_name_el = table.css("th::text").get()
            if not section_name_el: continue
            
            section_name = section_name_el.strip()
            section_data = {}
            rows = table.css("tr")
            current_label = ""
            
            for row in rows:
                label_el = row.css(".ttl a::text").get() or row.css(".ttl::text").get()
                value_el = row.css(".nfo")
                value = value_el.xpath("string()").get()
                
                if label_el and label_el.strip() != "\u00a0":
                    current_label = label_el.strip()
                
                if current_label and value:
                    val_str = value.strip()
                    if current_label in section_data:
                        section_data[current_label] += " | " + val_str
                    else:
                        section_data[current_label] = val_str
            
            specs[section_name] = section_data
            
        return specs

    def save_data(self, data, filename):
        if not os.path.exists("data"):
            os.makedirs("data")
        filepath = os.path.join("data", filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Data saved to {filepath}")

def run_scraper(cookies=None, limit=5):
    scraper = GSMArenaScraper(cookies=cookies)
    device_urls = scraper.discover_device_urls()
    
    if not device_urls:
        print("No device URLs found. Aborting.")
        return

    results = []
    print(f"Starting extraction for first {limit} devices...")
    for url in device_urls[:limit]:
        specs = scraper.fetch_device_specs(url)
        if specs:
            results.append(specs)
            print(f"Successfully scraped: {specs['model']}")
        else:
            print(f"Failed to scrape: {url} (Possible blocking)")
        time.sleep(1) 
    
    if results:
        scraper.save_data(results, "gsmarena_specs_sample.json")
        print(f"Total scraped: {len(results)}")

if __name__ == "__main__":
    # User provided cookies
    user_cookies = {
        "https://www.gsmarena.com": {
            "_ga": "GA1.1.703161741.1773852267",
            "_ga_WECNNBCHQE": "GS2.1.s1773852266$o1$g0$t1773852266$j60$l0$h0",
            "DeviceID": "10769",
            "keyw": "Nokia",
            "ts_ok": "MTc3NDQ1NzA2N3xkYmJkZThjMmE3Zjk3NmFkMzc3NzllM2E2MjQ4NDBkMjA4Nzg5ODc4MzEwNDgyMTMyMzIxZGUxZDAxNmUzYzZifGVkYTVhZDdkZTNkMDQ2OWVmYWI0ZjkzMGVkNjMxMDcyMTJkMTJiNTBlN2Y3ZGY5ODQ3OTQ0YTU2MDBkZDE4ZTk.4a262fe1fb3ec2d2bce1cc234cec681ac642fba3af964d61b980ea9683f1e13c"
        },
        "https://fdn.gsmarena.com": {
            "_ga": "GA1.1.703161741.1773852267",
            "_ga_WECNNBCHQE": "GS2.1.s1773852266$o1$g0$t1773852266$j60$l0$h0",
            "keyw": "Nokia",
            "ts_ok": "MTc3NDQ1NzA2N3xkYmJkZThjMmE3Zjk3NmFkMzc3NzllM2E2MjQ4NDBkMjA4Nzg5ODc4MzEwNDgyMTMyMzIxZGUxZDAxNmUzYzZifGVkYTVhZDdkZTNkMDQ2OWVmYWI0ZjkzMGVkNjMxMDcyMTJkMTJiNTBlN2Y3ZGY5ODQ3OTQ0YTU2MDBkZDE4ZTk.4a262fe1fb3ec2d2bce1cc234cec681ac642fba3af964d61b980ea9683f1e13c"
        }
    }
    run_scraper(cookies=user_cookies, limit=3)
