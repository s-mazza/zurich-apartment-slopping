import argparse
import logging
import re
import time
import json
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from urllib.parse import urlparse, urljoin

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

def is_challenge_html(html: str) -> bool:
    signal = html.lower()
    if "verify you are human" in signal or "incident_id" in signal:
        return True
    if "<title>just a moment...</title>" in signal:
        return True
    return False

def parse_cookie_string(cookie_str: str, domain: str) -> List[Dict[str, Any]]:
    cookies = []
    parts = cookie_str.strip().split(";")
    for part in parts:
        if "=" not in part: continue
        name, value = part.strip().split("=", 1)
        cookies.append({
            "name": name, "value": value,
            "domain": domain, "path": "/"
        })
    return cookies

def get_urls_from_markdown(file_path: Path, provider: str) -> List[str]:
    if not file_path.exists():
        return []
    content = file_path.read_text(encoding="utf-8")
    segments = re.split(r'##\s+', content)[1:]
    urls = []
    for seg in segments:
        if f"- **Provider**: {provider}" in seg or f"Provider: {provider}" in seg:
            match = re.search(r'\[View\]\((https?://[^)]+)\)', seg)
            if match:
                urls.append(match.group(1))
    return urls

def wait_for_login(page, provider: str, timeout_sec: int = 15):
    """Wait for the session to be established, either via cookies or manual login."""
    logger.info(f"Checking {provider} session state...")
    
    # Specific selectors that ONLY appear when logged in
    logged_in_selectors = [
        '.HgHeader_userName_', 'text="Logout"', 'text="Simone"',
        'text="Mio account"', 'text="Mein Konto"', 'text="Mon compte"',
        'text="Account"'
    ]
    
    start_time = time.time()
    while time.time() - start_time < timeout_sec:
        for sel in logged_in_selectors:
            try:
                if page.locator(sel).first.is_visible():
                    logger.info(f"✅ {provider} Session verified via {sel}!")
                    return True
            except: continue
        page.wait_for_timeout(1000)
    
    logger.warning(f"❌ {provider} session NOT detected.")
    logger.warning("PLEASE MANUALLY LOGIN in the browser window now.")
    logger.warning("The script will wait for you to be logged in before continuing...")
    
    while True:
        for sel in logged_in_selectors:
            try:
                if page.locator(sel).first.is_visible():
                    logger.info(f"✅ {provider} Session established manually!")
                    return True
            except: continue
        page.wait_for_timeout(2000)

def contact_flatfox(page, url: str, message: str, dry_run: bool = False):
    logger.info(f"[flatfox] Processing {url}...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if "vai alla chat" in page.content().lower():
            logger.info("ALREADY APPLIED. Skipping.")
            return True

        page.evaluate('''() => {
            const b = document.querySelector('.contact-request-form-blocker');
            if (b) b.click();
        }''')
        page.wait_for_timeout(1000)
        
        logger.info("[DEBUG] Starting textarea search...")
        try:
            Path("output/flatfox_debug_page.html").write_text(page.content(), encoding="utf-8")
            logger.info("[DEBUG] Dumped HTML to output/flatfox_debug_page.html")
        except Exception as e:
            logger.error(f"Failed to dump HTML: {e}")
        
        target_textarea = None
        for step in range(3):
            textarea_locator = page.locator('textarea[name="text"], #id_top_text, #id_text, textarea')
            logger.info(f"[DEBUG] Search step {step}: found {textarea_locator.count()} matching elements.")
            for i in range(textarea_locator.count()):
                el = textarea_locator.nth(i)
                is_vis = el.is_visible()
                try:
                    tag_html = el.evaluate('el => el.outerHTML')
                except Exception:
                    tag_html = "<error getting HTML>"
                logger.info(f"[DEBUG] Element {i}: visible={is_vis}, html={tag_html[:150]}")
                if is_vis:
                    target_textarea = el
                    logger.info(f"[DEBUG] Selected element {i} as target.")
                    break
            if target_textarea:
                break
            page.wait_for_timeout(1000)

        page.screenshot(path="output/flatfox_debug_before.png")

        if not target_textarea:
            textarea_locator = page.locator('textarea[name="text"], #id_top_text, #id_text, textarea')
            if textarea_locator.count() > 0:
                target_textarea = textarea_locator.first
                logger.info("[DEBUG] Falling back to the first non-visible textarea.")

        if target_textarea:
            user_data = page.evaluate('() => window.flatfoxConfig?.user?.full_name || ""')
            if user_data: logger.info(f"Logged in as: {user_data}")
            else: logger.warning("NOT LOGGED IN on Flatfox!")

            try:
                logger.info("[DEBUG] Attempting to click the target textarea.")
                target_textarea.click(timeout=3000)
            except Exception as e:
                logger.error(f"[DEBUG] Click failed: {e}")
            
            logger.info("[DEBUG] Attempting to fill the target textarea.")
            try:
                target_textarea.fill(message, force=True)
                logger.info("[DEBUG] Fill executed successfully.")
            except Exception as e:
                logger.error(f"[DEBUG] Fill failed: {e}")
                
            try:
                logger.info("[DEBUG] Attempting to evaluate input/change events.")
                target_textarea.evaluate("(el) => { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }")
                logger.info("[DEBUG] Events dispatched successfully.")
            except Exception as e:
                logger.error(f"[DEBUG] Dispatch events failed: {e}")
            
            try:
                val = target_textarea.evaluate("el => el.value")
                logger.info(f"[DEBUG] Textarea value after filling: {repr(val[:50])}...")
            except Exception as e:
                logger.error(f"[DEBUG] Value check failed: {e}")
            
            page.screenshot(path="output/flatfox_debug_after.png")
            
            if dry_run: logger.info("[Dry Run] Message filled.")
            else:
                page.evaluate('() => { const btns = document.querySelectorAll("button[name=\'contact-advertiser\']"); if(btns.length) btns[btns.length-1].click(); }')
                page.wait_for_timeout(3000)
                logger.info("Request sent.")
            return True
    except Exception as e: logger.error(f"Flatfox error: {e}")
    return False

def contact_homegate(page, url: str, message: str, dry_run: bool = False):
    logger.info(f"[homegate] Processing {url}...")
    try:
        # Removed wait_for_login block to match apartment_finder_llm.py

        # 2. Extract ID and go directly to Contact Page
        listing_id = url.rstrip("/").split("/")[-1]
        contact_url = f"https://www.homegate.ch/rent/{listing_id}/contact"
        logger.info(f"Navigating directly to contact page: {contact_url}")
        page.goto(contact_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        
        if is_challenge_html(page.content()):
            logger.warning("Bot challenge detected. Solve it in the browser.")
            while is_challenge_html(page.content()):
                page.wait_for_timeout(2000)

        # 3. Find and fill form
        # Personal details (required if not fully logged in)
        details = {
            "firstName": "Simone",
            "lastName": "Mazzacano",
            "email": "simoneitalia10@gmail.com",
            "phone": "+39 3453043789",
            "street": "Via Roma, 400",
            "zip": "40100",
            "city": "Bologna"
        }
        for name, value in details.items():
            field = page.locator(f'input[name="{name}"]').first
            if field.count() > 0:
                curr = field.evaluate("el => el.value")
                if not curr:
                    logger.info(f"Filling {name}...")
                    field.fill(value)

        textarea = page.locator('textarea[name="message"], textarea[name="text"], textarea').first
        if textarea.count() > 0:
            textarea.fill(message)
            if dry_run:
                logger.info("[Dry Run] Message filled.")
                return True
            else:
                send_btn = page.locator('button[type="submit"]:has-text("invia"), button[type="submit"]:has-text("Send"), button:has-text("richiesta")').first
                if send_btn.count() == 0:
                    send_btn = page.locator('button[type="submit"]').first

                if send_btn.count() > 0:
                    logger.info("Waiting a moment before sending...")
                    page.wait_for_timeout(2000)
                    logger.info("Clicking send...")
                    send_btn.click()
                    page.wait_for_timeout(4000)
                    if any(x in page.content().lower() for x in ["grazie", "success", "inviata", "sent"]):
                        logger.info("SUCCESS: Request sent.")
                else:
                    logger.warning("Send button not found. Trying Enter key.")
                    textarea.press("Enter")
            return True
        else:
            logger.error("Homegate textarea not found.")
            page.screenshot(path="output/homegate_contact_fail.png")
    except Exception as e:
        logger.error(f"Homegate error: {e}")
    return False

def contact_comparis(page, url: str, message: str, dry_run: bool = False):
    logger.info(f"[comparis] Processing {url}...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        target_btn = page.get_by_role("link", name=re.compile(r"offer|offerta|inserzione", re.I)).first
        if target_btn.count() > 0:
            target_url = target_btn.get_attribute("href")
            if target_url:
                full_target = urljoin(url, target_url)
                logger.info(f"Redirecting from Comparis to: {full_target}")
                if "flatfox.ch" in full_target: return contact_flatfox(page, full_target, message, dry_run)
                elif "homegate.ch" in full_target: return contact_homegate(page, full_target, message, dry_run)
        textarea = page.locator('textarea').first
        if textarea.count() > 0 and textarea.is_visible():
            textarea.fill(message)
            if not dry_run:
                send_btn = page.get_by_role("button", name=re.compile(r"send|invia", re.I)).first
                if send_btn.count() > 0:
                    send_btn.click()
                    page.wait_for_timeout(3000)
            return True
    except Exception as e: logger.error(f"Comparis error: {e}")
    return False

def run():
    parser = argparse.ArgumentParser(description="Auto Contact CLI")
    parser.add_argument("--mode", choices=["filtered", "excluded"], default="filtered")
    parser.add_argument("--provider", choices=["flatfox", "homegate", "comparis"], required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    parser.set_defaults(dry_run=True)
    args = parser.parse_args()

    message = Path("message_template.txt").read_text(encoding="utf-8")
    if not message:
        print("Nessun messaggio")
        return
    urls = get_urls_from_markdown(Path(f"output/listings_{args.mode}_llm.md"), args.provider)
    if not urls:
        print("Nessun indirizzo")
        return
    if args.limit: urls = urls[:args.limit]

    cookie_file = Path(f"cookies_{args.provider}.txt")
    cookies = []
    if cookie_file.exists():
        raw_cookie_str = cookie_file.read_text()
        domain = f"{args.provider}.ch"
        for sub in ["", "www.", "chat.", "account."]:
            cookies.extend(parse_cookie_string(raw_cookie_str, f".{sub}{domain}" if sub else f".{domain}"))
        logger.info(f"Loaded {len(cookies)} cookies (broadcast injection).")
    else:
        logger.error("Nessun cookie")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 1024}
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = { runtime: {} };")
        if cookies: context.add_cookies(cookies)
        page = context.new_page()
        for url in urls:
            success = False
            if args.provider == "flatfox": success = contact_flatfox(page, url, message, args.dry_run)
            elif args.provider == "homegate": success = contact_homegate(page, url, message, args.dry_run)
            elif args.provider == "comparis": success = contact_comparis(page, url, message, args.dry_run)
            if success: logger.info(f"Processed: {url}")
            time.sleep(4)
        browser.close()

if __name__ == "__main__":
    run()
