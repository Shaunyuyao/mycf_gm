from seleniumbase import Driver
import time
from scrapling import Selector

def test():
    print("Starting SeleniumBase UC mode...")
    # Driver(uc=True) starts an undetected chromedriver
    driver = Driver(uc=True, headless=True)
    try:
        print("Navigating to GSMArena brands page...")
        driver.get("https://www.gsmarena.com/makers.php3")
        
        # Wait for potential Turnstile or page load
        print("Waiting for page load...")
        time.sleep(10) 
        
        title = driver.title
        print(f"Page Title: {title}")
        
        content = driver.page_source
        print(f"Content Length: {len(content)}")
        
        if "Turnstile" in content:
            print("Turnstile check still detected.")
        else:
            print("Turnstile bypass might have worked!")
            
        sel = Selector(content)
        brand_links = sel.css('a[href*="-phones-"]')
        print(f"Found {len(brand_links)} potential brand links.")
        
    finally:
        driver.quit()

if __name__ == "__main__":
    test()
