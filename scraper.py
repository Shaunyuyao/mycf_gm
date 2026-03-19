import argparse
import base64
import json
import os
import re
import sqlite3
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from Cryptodome.Cipher import AES
from curl_cffi import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from scrapling import Selector
import win32crypt

SITEMAP_URL = "https://www.gsmarena.com/sitemaps/phones.xml"
BOOTSTRAP_URL = "https://www.gsmarena.com/makers.php3"
DEFAULT_COOKIE_FILE = Path("cookies") / "gsmarena_cookies.json"
DEFAULT_OUTPUT_FILE = Path("data") / "gsmarena_specs_sample.json"


class CloudflareBlockedError(RuntimeError):
    pass


class AccessDeniedError(RuntimeError):
    pass


class RateLimitedError(RuntimeError):
    pass


def save_cookies(cookie_file: Path, cookies: list[dict[str, Any]]) -> None:
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    with cookie_file.open("w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.replace("\xa0", " ").split()).strip()


def cookie_presence(cookies: list[dict[str, Any]], name: str) -> bool:
    return any((c.get("name") or "").lower() == name.lower() for c in cookies)


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


def bootstrap_cookies(
    cookie_file: Path = DEFAULT_COOKIE_FILE,
    user_data_dir: Path = Path("browser_profile"),
    timeout_seconds: int = 180,
    headless: bool = False,
    browser_channel: str = "chrome",
) -> None:
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=headless,
            channel=browser_channel if browser_channel != "chromium" else None,
            locale="en-US",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 900},
            args=[
                "--window-size=1366,900",
            ],
        )
        try:
            page = context.new_page()
            page.goto(BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=45_000)
            print("Browser opened. If Turnstile appears, solve it in this browser window.")
            print(f"Waiting up to {timeout_seconds} seconds for validation...")

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                try:
                    page.locator("button[type='submit']").first.click(timeout=1000)
                except Exception:
                    pass

                cookies = context.cookies("https://www.gsmarena.com")
                if cookie_presence(cookies, "cf_clearance") or cookie_presence(cookies, "ts_ok"):
                    save_cookies(cookie_file, cookies)
                    print(f"Saved {len(cookies)} cookies to: {cookie_file}")
                    return
                page.wait_for_timeout(1000)

            html = page.content()
            if "cf-turnstile" in html:
                raise RuntimeError(
                    "Turnstile still present after timeout. Try again with:\n"
                    "1) --browser-channel chrome\n"
                    "2) larger timeout (e.g. 600)\n"
                    "3) close VPN/proxy and complete challenge manually in opened window."
                )

            cookies = context.cookies("https://www.gsmarena.com")
            save_cookies(cookie_file, cookies)
            print(f"Saved {len(cookies)} cookies to: {cookie_file}")
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "Timed out waiting for Turnstile to pass. "
                "Retry with --timeout-seconds 300 and solve challenge manually in opened browser."
            ) from exc
        finally:
            context.close()


def import_chrome_cookies(
    cookie_file: Path = DEFAULT_COOKIE_FILE,
    chrome_cookie_db: Path | None = None,
    chrome_key_file: Path | None = None,
) -> None:
    default_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    cookie_db = chrome_cookie_db or (default_root / "Default" / "Network" / "Cookies")
    key_file = chrome_key_file or (default_root / "Local State")

    if not cookie_db.exists():
        raise RuntimeError(f"Chrome cookie DB not found: {cookie_db}")
    if not key_file.exists():
        raise RuntimeError(f"Chrome Local State not found: {key_file}")

    def load_master_key(local_state_file: Path) -> bytes:
        state = json.loads(local_state_file.read_text(encoding="utf-8"))
        enc_key_b64 = state.get("os_crypt", {}).get("encrypted_key")
        if not enc_key_b64:
            raise RuntimeError("Cannot find encrypted_key in Local State")
        enc_key = base64.b64decode(enc_key_b64)
        if enc_key.startswith(b"DPAPI"):
            enc_key = enc_key[5:]
        return win32crypt.CryptUnprotectData(enc_key, None, None, None, 0)[1]

    def decrypt_cookie_value(encrypted_value: bytes, master_key: bytes) -> str:
        if not encrypted_value:
            return ""
        if encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11"):
            nonce = encrypted_value[3:15]
            cipher_bytes = encrypted_value[15:-16]
            tag = encrypted_value[-16:]
            cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(cipher_bytes, tag).decode("utf-8", errors="ignore")
        return win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1].decode(
            "utf-8", errors="ignore"
        )

    def chrome_ts_to_unix(expires_utc: int) -> int:
        if not expires_utc:
            return -1
        return int(expires_utc / 1_000_000 - 11644473600)

    master_key = load_master_key(key_file)

    with tempfile.TemporaryDirectory() as td:
        copied_db = Path(td) / "Cookies"
        shutil.copy2(cookie_db, copied_db)
        conn = sqlite3.connect(copied_db)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT name, host_key, path, expires_utc, is_secure, is_httponly, value, encrypted_value
            FROM cookies
            WHERE host_key LIKE ? OR host_key LIKE ?
            """,
            ("%gsmarena.com%", "%.gsmarena.com%"),
        )
        rows = cur.fetchall()
        conn.close()

    cookies: list[dict[str, Any]] = []
    for name, host_key, path, expires_utc, is_secure, is_httponly, value, encrypted_value in rows:
        cookie_val = value or decrypt_cookie_value(encrypted_value, master_key)
        cookies.append(
            {
                "name": name,
                "value": cookie_val,
                "domain": host_key,
                "path": path or "/",
                "secure": bool(is_secure),
                "httpOnly": bool(is_httponly),
                "expires": chrome_ts_to_unix(int(expires_utc or 0)),
            }
        )

    if not cookies:
        raise RuntimeError(
            f"No gsmarena cookies found in cookie DB: {cookie_db}. "
            "Open GSMArena in this Chrome profile and complete verification once, then retry."
        )

    save_cookies(cookie_file, cookies)
    has_cf = cookie_presence(cookies, "cf_clearance")
    has_ts_ok = cookie_presence(cookies, "ts_ok")
    states = verify_session_with_cookies(cookie_file)
    print(f"Imported {len(cookies)} cookies from Chrome into: {cookie_file}")
    print(f"cf_clearance present: {has_cf}")
    print(f"ts_ok present: {has_ts_ok}")
    print(f"verify makers={states['makers']} sample_device={states['sample_device']}")


def export_cookies_via_profile_browser(
    cookie_file: Path = DEFAULT_COOKIE_FILE,
    chrome_user_data_dir: Path | None = None,
    profile_directory: str = "Default",
    browser_channel: str = "chrome",
) -> None:
    if chrome_user_data_dir is None:
        chrome_user_data_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"

    if not chrome_user_data_dir.exists():
        raise RuntimeError(f"Chrome user data dir not found: {chrome_user_data_dir}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(chrome_user_data_dir),
            headless=False,
            channel=browser_channel if browser_channel != "chromium" else None,
            args=[f"--profile-directory={profile_directory}", "--window-size=1200,800"],
        )
        try:
            page = context.new_page()
            page.goto("https://www.gsmarena.com/", wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1500)
            cookies = context.cookies("https://www.gsmarena.com")
            if not cookies:
                raise RuntimeError("No cookies exported from profile browser.")
            save_cookies(cookie_file, cookies)
            has_cf = cookie_presence(cookies, "cf_clearance")
            has_ts_ok = cookie_presence(cookies, "ts_ok")
            states = verify_session_with_cookies(cookie_file)
            print(f"Exported {len(cookies)} cookies via browser profile into: {cookie_file}")
            print(f"cf_clearance present: {has_cf}")
            print(f"ts_ok present: {has_ts_ok}")
            print(f"verify makers={states['makers']} sample_device={states['sample_device']}")
        finally:
            context.close()


def export_cookies_via_cdp(
    cookie_file: Path = DEFAULT_COOKIE_FILE,
    cdp_url: str = "http://127.0.0.1:9222",
) -> None:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        try:
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = browser.new_context()
            cookies = context.cookies("https://www.gsmarena.com")
            if not cookies:
                raise RuntimeError(
                    "No gsmarena cookies from CDP browser. "
                    "Open https://www.gsmarena.com in that Chrome and complete verification first."
                )
            save_cookies(cookie_file, cookies)
            has_cf = cookie_presence(cookies, "cf_clearance")
            has_ts_ok = cookie_presence(cookies, "ts_ok")
            states = verify_session_with_cookies(cookie_file)
            print(f"Exported {len(cookies)} cookies via CDP into: {cookie_file}")
            print(f"cf_clearance present: {has_cf}")
            print(f"ts_ok present: {has_ts_ok}")
            print(f"verify makers={states['makers']} sample_device={states['sample_device']}")
        finally:
            browser.close()


def find_chrome_cookie_profiles() -> list[tuple[str, Path, int]]:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    candidates: list[Path] = []
    default_dir = root / "Default"
    if default_dir.exists():
        candidates.append(default_dir)
    if root.exists():
        candidates.extend(sorted(p for p in root.glob("Profile *") if p.is_dir()))

    results: list[tuple[str, Path, int]] = []
    for profile_dir in candidates:
        cookie_db = profile_dir / "Network" / "Cookies"
        if not cookie_db.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{cookie_db.as_posix()}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE ? OR host_key LIKE ?",
                ("%gsmarena.com%", "%.gsmarena.com%"),
            )
            count = int(cur.fetchone()[0])
            conn.close()
            results.append((profile_dir.name, cookie_db, count))
        except Exception:
            results.append((profile_dir.name, cookie_db, -1))
    return results


def diagnose_chrome_cookie_sources() -> None:
    try:
        profiles = find_chrome_cookie_profiles()
    except PermissionError as exc:
        print(f"Permission denied while reading Chrome profile folder: {exc}")
        print("Run this command in your local terminal with your normal user account.")
        return
    if not profiles:
        print("No Chrome profiles with cookie DB found.")
        return
    print("Chrome profile cookie scan:")
    for name, db_path, count in profiles:
        if count >= 0:
            print(f"- {name}: gsmarena cookie rows={count} db={db_path}")
        else:
            print(f"- {name}: unreadable db={db_path}")


def create_session(cookie_file: Path = DEFAULT_COOKIE_FILE) -> requests.Session:
    session = requests.Session(impersonate="chrome124")
    session.headers.update(
        {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": BOOTSTRAP_URL,
            "upgrade-insecure-requests": "1",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        }
    )

    if cookie_file.exists():
        raw_cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
        for cookie in raw_cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            session.cookies.set(
                name=name,
                value=value,
                domain=cookie.get("domain"),
                path=cookie.get("path") or "/",
            )
    return session


def classify_html_state(text: str) -> str:
    lower = text.lower()
    if any(
        s in lower
        for s in [
            "too many requests",
            "rate limit exceeded",
            "http error 429",
            "error 429",
            "status code 429",
            "cf-error-code",
        ]
    ):
        return "RATE_LIMITED"
    if "GSMArena Turnstile check" in text or "cf-turnstile" in text:
        return "TURNSTILE"
    if any(
        s.lower() in text.lower()
        for s in [
            "Access denied",
            "You are not authorized to access this page",
            "您未获授权，无法查看此网页",
            "Request blocked",
        ]
    ):
        return "ACCESS_DENIED"
    if (
        ".specs-phone-name-title" in text
        or 'id="specs-list"' in text
        or 'class="brandmenu-v2"' in text
        or 'class="main"' in text
    ):
        return "PASS_OR_PARTIAL"
    return "UNKNOWN"


def verify_session_with_cookies(cookie_file: Path = DEFAULT_COOKIE_FILE) -> dict[str, str]:
    session = create_session(cookie_file)
    urls = {
        "makers": BOOTSTRAP_URL,
        "sample_device": "https://www.gsmarena.com/xiaomi_15_ultra-13661.php",
    }
    states: dict[str, str] = {}
    for key, url in urls.items():
        r = session.get(url, timeout=30)
        states[key] = classify_html_state(r.text or "")
    return states


def parse_device_page(html: str, url: str) -> dict[str, Any]:
    lower = html.lower()
    if any(
        s in lower
        for s in [
            "too many requests",
            "rate limit exceeded",
            "http error 429",
            "error 429",
            "status code 429",
            "cf-error-code",
        ]
    ):
        raise RateLimitedError("429 Too Many Requests page returned")
    if "GSMArena Turnstile check" in html or "cf-turnstile" in html:
        raise CloudflareBlockedError("Turnstile challenge page returned")
    deny_signals = [
        "Access denied",
        "You are not authorized to access this page",
        "您未获授权，无法查看此网页",
        "Request blocked",
    ]
    if any(s.lower() in html.lower() for s in deny_signals):
        raise AccessDeniedError("Access denied by Cloudflare/WAF")

    sel = Selector(html)
    model = normalize_text(sel.css(".specs-phone-name-title::text").get())
    if not model:
        raise ValueError("Could not parse model name from page")

    brand = normalize_text(sel.css(".breadcrumb a[href*='-phones-']::text").get()) or "Unknown"

    specs: dict[str, Any] = {"model": model, "brand": brand, "url": url}
    for table in sel.css("#specs-list table"):
        section_name = normalize_text(table.css("th::text").get())
        if not section_name:
            continue

        section: dict[str, str] = {}
        last_label = ""
        for row in table.css("tr"):
            label = normalize_text(row.css(".ttl a::text").get() or row.css(".ttl::text").get())
            value = normalize_text(row.css(".nfo").xpath("string()").get())
            if label:
                last_label = label
            if last_label and value:
                if last_label in section:
                    section[last_label] = f"{section[last_label]} | {value}"
                else:
                    section[last_label] = value
        specs[section_name] = section

    return specs


def compute_retry_wait_seconds(
    retry_after_header: str | None,
    attempt: int,
    retry_wait_seconds: float,
    max_retry_wait_seconds: float,
) -> float:
    if retry_after_header:
        try:
            retry_after = float(retry_after_header)
            if retry_after > 0:
                return min(retry_after, max_retry_wait_seconds)
        except (TypeError, ValueError):
            pass
    backoff = retry_wait_seconds * (2**attempt)
    return min(backoff, max_retry_wait_seconds)


def scrape_devices(
    limit: int = 5,
    start: int = 0,
    interval_seconds: float = 1.5,
    max_retries: int = 3,
    retry_wait_seconds: float = 4.0,
    max_retry_wait_seconds: float = 90.0,
    cookie_file: Path = DEFAULT_COOKIE_FILE,
    output_file: Path = DEFAULT_OUTPUT_FILE,
    provider: str = "direct",
    zenrows_api_key: str | None = None,
) -> list[dict[str, Any]]:
    urls = discover_device_urls()
    target_urls = urls[start : start + limit]
    if not target_urls:
        raise RuntimeError(f"No URLs found for start={start}, limit={limit}")

    session = create_session(cookie_file)
    output: list[dict[str, Any]] = []
    output_file.parent.mkdir(parents=True, exist_ok=True)

    for index, url in enumerate(target_urls, start=1):
        success = False
        for attempt in range(max_retries + 1):
            response = None
            try:
                if provider == "direct":
                    response = session.get(url, timeout=30)
                elif provider == "zenrows":
                    api_key = zenrows_api_key or os.getenv("ZENROWS_API_KEY")
                    if not api_key:
                        raise RuntimeError("ZENROWS_API_KEY is required when provider=zenrows")
                    response = requests.get(
                        "https://api.zenrows.com/v1/",
                        params={
                            "apikey": api_key,
                            "url": url,
                            "js_render": "true",
                            "premium_proxy": "true",
                        },
                        timeout=60,
                        impersonate="chrome124",
                    )
                else:
                    raise RuntimeError(f"Unknown provider: {provider}")

                if response.status_code == 429:
                    wait_s = compute_retry_wait_seconds(
                        response.headers.get("Retry-After"),
                        attempt,
                        retry_wait_seconds,
                        max_retry_wait_seconds,
                    )
                    raise RateLimitedError(f"HTTP 429 Too Many Requests (suggested wait {wait_s:.1f}s)")

                response.raise_for_status()
                record = parse_device_page(response.text, url)
                output.append(record)
                print(f"[{index}/{len(target_urls)}] OK: {record['model']}")
                success = True
                break
            except RateLimitedError as exc:
                if attempt < max_retries:
                    retry_after = response.headers.get("Retry-After") if response is not None else None
                    wait_s = compute_retry_wait_seconds(
                        retry_after,
                        attempt,
                        retry_wait_seconds,
                        max_retry_wait_seconds,
                    )
                    print(
                        f"[{index}/{len(target_urls)}] RETRY {attempt + 1}/{max_retries}: "
                        f"{url} ({exc}); sleeping {wait_s:.1f}s"
                    )
                    time.sleep(wait_s)
                    continue
                print(f"[{index}/{len(target_urls)}] FAIL: {url} ({exc})")
                break
            except CloudflareBlockedError:
                raise RuntimeError(
                    f"Blocked by Cloudflare at {url}. "
                    f"Run bootstrap first: python scraper.py bootstrap-cookies --cookie-file {cookie_file}"
                ) from None
            except AccessDeniedError:
                raise RuntimeError(
                    f"Access denied at {url}. This is usually IP/network reputation blocking. "
                    "Try another clean network/IP, disable VPN/proxy, then re-run bootstrap-cookies."
                ) from None
            except Exception as exc:
                print(f"[{index}/{len(target_urls)}] FAIL: {url} ({exc})")
                break
        time.sleep(interval_seconds)

    output_file.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(output)} device records to: {output_file}")
    return output


def run_scraper(limit: int = 5) -> None:
    scrape_devices(limit=limit)


def diagnose_access(cookie_file: Path = DEFAULT_COOKIE_FILE) -> None:
    session = create_session(cookie_file)
    sample_url = "https://www.gsmarena.com/xiaomi_15_ultra-13661.php"
    for url in [BOOTSTRAP_URL, sample_url]:
        r = session.get(url, timeout=30)
        text = r.text or ""
        state = classify_html_state(text)
        print(f"{url}\n  status={r.status_code} state={state} len={len(text)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GSMArena scraper with Turnstile cookie bootstrap flow.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_boot = sub.add_parser("bootstrap-cookies", help="Open a browser and save validated cookies.")
    p_boot.add_argument("--cookie-file", type=Path, default=DEFAULT_COOKIE_FILE)
    p_boot.add_argument("--user-data-dir", type=Path, default=Path("browser_profile"))
    p_boot.add_argument("--timeout-seconds", type=int, default=180)
    p_boot.add_argument("--headless", action="store_true")
    p_boot.add_argument("--browser-channel", choices=["chrome", "chromium"], default="chrome")

    p_import = sub.add_parser("import-chrome-cookies", help="Import gsmarena cookies from local Chrome profile.")
    p_import.add_argument("--cookie-file", type=Path, default=DEFAULT_COOKIE_FILE)
    p_import.add_argument("--chrome-cookie-db", type=Path, default=None)
    p_import.add_argument("--chrome-key-file", type=Path, default=None)

    sub.add_parser("diagnose-chrome-cookies", help="Scan local Chrome profiles for gsmarena cookie rows.")
    p_export = sub.add_parser(
        "export-profile-cookies",
        help="Export cookies by opening your real Chrome user profile via Playwright.",
    )
    p_export.add_argument("--cookie-file", type=Path, default=DEFAULT_COOKIE_FILE)
    p_export.add_argument("--chrome-user-data-dir", type=Path, default=None)
    p_export.add_argument("--profile-directory", default="Default")
    p_export.add_argument("--browser-channel", choices=["chrome", "chromium"], default="chrome")
    p_cdp = sub.add_parser("export-cookies-cdp", help="Export cookies from a running Chrome with remote debugging.")
    p_cdp.add_argument("--cookie-file", type=Path, default=DEFAULT_COOKIE_FILE)
    p_cdp.add_argument("--cdp-url", default="http://127.0.0.1:9222")

    p_scrape = sub.add_parser("scrape", help="Scrape device spec pages.")
    p_scrape.add_argument("--limit", type=int, default=5)
    p_scrape.add_argument("--start", type=int, default=0)
    p_scrape.add_argument("--interval-seconds", type=float, default=1.5)
    p_scrape.add_argument("--max-retries", type=int, default=3)
    p_scrape.add_argument("--retry-wait-seconds", type=float, default=4.0)
    p_scrape.add_argument("--max-retry-wait-seconds", type=float, default=90.0)
    p_scrape.add_argument("--cookie-file", type=Path, default=DEFAULT_COOKIE_FILE)
    p_scrape.add_argument("--output-file", type=Path, default=DEFAULT_OUTPUT_FILE)
    p_scrape.add_argument("--provider", choices=["direct", "zenrows"], default="direct")
    p_scrape.add_argument("--zenrows-api-key", default=None)

    p_diag = sub.add_parser("diagnose", help="Diagnose current access status (turnstile / access denied).")
    p_diag.add_argument("--cookie-file", type=Path, default=DEFAULT_COOKIE_FILE)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "bootstrap-cookies":
        bootstrap_cookies(
            cookie_file=args.cookie_file,
            user_data_dir=args.user_data_dir,
            timeout_seconds=args.timeout_seconds,
            headless=args.headless,
            browser_channel=args.browser_channel,
        )
    elif args.command == "import-chrome-cookies":
        import_chrome_cookies(
            cookie_file=args.cookie_file,
            chrome_cookie_db=args.chrome_cookie_db,
            chrome_key_file=args.chrome_key_file,
        )
    elif args.command == "diagnose-chrome-cookies":
        diagnose_chrome_cookie_sources()
    elif args.command == "export-profile-cookies":
        export_cookies_via_profile_browser(
            cookie_file=args.cookie_file,
            chrome_user_data_dir=args.chrome_user_data_dir,
            profile_directory=args.profile_directory,
            browser_channel=args.browser_channel,
        )
    elif args.command == "export-cookies-cdp":
        export_cookies_via_cdp(cookie_file=args.cookie_file, cdp_url=args.cdp_url)
    elif args.command == "scrape":
        scrape_devices(
            limit=args.limit,
            start=args.start,
            interval_seconds=args.interval_seconds,
            max_retries=args.max_retries,
            retry_wait_seconds=args.retry_wait_seconds,
            max_retry_wait_seconds=args.max_retry_wait_seconds,
            cookie_file=args.cookie_file,
            output_file=args.output_file,
            provider=args.provider,
            zenrows_api_key=args.zenrows_api_key,
        )
    elif args.command == "diagnose":
        diagnose_access(cookie_file=args.cookie_file)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
