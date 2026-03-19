import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from curl_cffi import requests
from lxml import html as lxml_html
from playwright.sync_api import sync_playwright

SITEMAP_URL = "https://www.gsmarena.com/sitemaps/phones.xml"
BOOTSTRAP_URL = "https://www.gsmarena.com/makers.php3"
DEFAULT_OUTPUT_FILE = Path("data") / "gsmarena_specs_browser_only.json"


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.replace("\xa0", " ").split()).strip()


def discover_device_urls() -> list[str]:
    headers = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(SITEMAP_URL, headers=headers, timeout=30, impersonate="chrome124")
    response.raise_for_status()

    root = ET.fromstring(response.content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls: list[str] = []
    for url_tag in root.findall("s:url", ns):
        loc_tag = url_tag.find("s:loc", ns)
        if loc_tag is None or not loc_tag.text:
            continue
        loc = loc_tag.text.strip()
        if re.search(r"-\d+\.php$", loc) and "-pictures-" not in loc and "/related.php3" not in loc:
            urls.append(loc)
    return urls


def is_turnstile_html(html: str) -> bool:
    return "GSMArena Turnstile check" in html or "cf-turnstile" in html


def is_access_denied_html(html: str) -> bool:
    deny_signals = [
        "Access denied",
        "You are not authorized to access this page",
        "您未获授权，无法查看此网页",
        "Request blocked",
    ]
    lower = html.lower()
    return any(s.lower() in lower for s in deny_signals)


def is_rate_limited_html(html: str) -> bool:
    lower = html.lower()
    rate_limit_signals = [
        "too many requests",
        "rate limit exceeded",
        "http error 429",
        "error 429",
        "status code 429",
        "cf-error-code",
    ]
    return any(token in lower for token in rate_limit_signals)


def ensure_access(page, url: str, timeout_seconds: int) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        html = page.content()
        if is_access_denied_html(html):
            raise RuntimeError(f"Access denied at {url}. Try another network/IP.")
        if is_rate_limited_html(html):
            raise RuntimeError(f"Rate limited (429) at {url}.")
        if not is_turnstile_html(html):
            return html
        try:
            page.locator("button[type='submit']").first.click(timeout=1000)
        except Exception:
            pass
        page.wait_for_timeout(1000)
    raise RuntimeError(f"Turnstile still present after {timeout_seconds}s: {url}")


def parse_device_page(html: str, url: str) -> dict[str, Any]:
    if is_turnstile_html(html):
        raise RuntimeError("Turnstile page returned.")
    if is_access_denied_html(html):
        raise RuntimeError("Access denied page returned.")

    doc = lxml_html.fromstring(html)

    model = normalize_text("".join(doc.xpath("//*[contains(@class,'specs-phone-name-title')]/text()")[:1]))
    if not model:
        raise RuntimeError("Could not parse model name.")

    brand_nodes = doc.xpath("//*[contains(@class,'breadcrumb')]//a[contains(@href,'-phones-')]/text()")
    brand = normalize_text(brand_nodes[0] if brand_nodes else "") or "Unknown"
    specs: dict[str, Any] = {"model": model, "brand": brand, "url": url}

    tables = doc.xpath("//div[@id='specs-list']//table")
    for table in tables:
        section_name = normalize_text("".join(table.xpath(".//th[1]/text()")[:1]))
        if not section_name:
            continue
        section: dict[str, str] = {}
        last_label = ""
        for row in table.xpath(".//tr"):
            label = normalize_text("".join(row.xpath(".//*[contains(@class,'ttl')]//a/text()")[:1]))
            if not label:
                label = normalize_text("".join(row.xpath(".//*[contains(@class,'ttl')]/text()")[:1]))
            value = normalize_text("".join(row.xpath(".//*[contains(@class,'nfo')]//text()")))
            if label:
                last_label = label
            if last_label and value:
                if last_label in section:
                    section[last_label] = f"{section[last_label]} | {value}"
                else:
                    section[last_label] = value
        specs[section_name] = section
    return specs


def scrape_browser_only(
    limit: int,
    start: int,
    interval_seconds: float,
    timeout_seconds: int,
    max_retries: int,
    retry_wait_seconds: float,
    max_retry_wait_seconds: float,
    user_data_dir: Path,
    output_file: Path,
    browser_channel: str,
    cdp_url: str | None,
) -> None:
    urls = discover_device_urls()
    if limit == 0:
        target_urls = urls[start:]
    else:
        target_urls = urls[start : start + limit]
    if not target_urls:
        raise RuntimeError(f"No URLs found for start={start}, limit={limit}")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = None
        context = None
        if cdp_url:
            browser = p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
        else:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,
                channel=browser_channel if browser_channel != "chromium" else None,
                locale="en-US",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1366, "height": 900},
                args=["--window-size=1366,900"],
            )
        try:
            page = context.new_page()
            if cdp_url:
                print("Browser-only CDP mode started. Reusing your existing Chrome session.")
            else:
                print("Browser-only mode started. If Turnstile appears, solve it in this same browser window.")
            ensure_access(page, BOOTSTRAP_URL, timeout_seconds)

            results: list[dict[str, Any]] = []
            for index, url in enumerate(target_urls, start=1):
                ok = False
                for attempt in range(max_retries + 1):
                    try:
                        html = ensure_access(page, url, timeout_seconds)
                        record = parse_device_page(html, url)
                        results.append(record)
                        print(f"[{index}/{len(target_urls)}] OK: {record['model']}")
                        ok = True
                        break
                    except Exception as exc:
                        message = str(exc)
                        transient = any(
                            token in message
                            for token in [
                                "ERR_CONNECTION_CLOSED",
                                "ERR_CONNECTION_RESET",
                                "ERR_CONNECTION_ABORTED",
                                "ERR_TIMED_OUT",
                                "Target page, context or browser has been closed",
                            ]
                        )
                        rate_limited = "Rate limited (429)" in message
                        if (transient or rate_limited) and attempt < max_retries:
                            if rate_limited:
                                wait_s = min(retry_wait_seconds * (2**attempt), max_retry_wait_seconds)
                            else:
                                wait_s = min(retry_wait_seconds * (attempt + 1), max_retry_wait_seconds)
                            print(
                                f"[{index}/{len(target_urls)}] RETRY {attempt + 1}/{max_retries}: {url} "
                                f"(error: {exc}); sleeping {wait_s:.1f}s"
                            )
                            time.sleep(wait_s)
                            try:
                                page.close()
                            except Exception:
                                pass
                            page = context.new_page()
                            continue
                        print(f"[{index}/{len(target_urls)}] FAIL: {url} ({exc})")
                        break
                if not ok:
                    pass
                time.sleep(interval_seconds)

            output_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Saved {len(results)} records to: {output_file}")
        finally:
            if browser is not None:
                browser.close()
            else:
                context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GSMArena browser-only scraper (single browser context).")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--interval-seconds", type=float, default=1.5)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-wait-seconds", type=float, default=2.0)
    parser.add_argument("--max-retry-wait-seconds", type=float, default=90.0)
    parser.add_argument("--user-data-dir", type=Path, default=Path("browser_profile_browser_only"))
    parser.add_argument("--output-file", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--browser-channel", choices=["chrome", "chromium"], default="chrome")
    parser.add_argument("--cdp-url", default=None, help="Reuse an existing Chrome via CDP, e.g. http://127.0.0.1:9222")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    scrape_browser_only(
        limit=args.limit,
        start=args.start,
        interval_seconds=args.interval_seconds,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        retry_wait_seconds=args.retry_wait_seconds,
        max_retry_wait_seconds=args.max_retry_wait_seconds,
        user_data_dir=args.user_data_dir,
        output_file=args.output_file,
        browser_channel=args.browser_channel,
        cdp_url=args.cdp_url,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
