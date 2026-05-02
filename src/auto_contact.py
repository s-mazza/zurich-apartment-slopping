import argparse
import logging
import re
import time
from pathlib import Path
from typing import List, Dict, Any
from urllib.parse import urlparse

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
    # Find segments starting with ## and containing the provider
    segments = re.split(r'##\s+', content)[1:]
    urls = []
    for seg in segments:
        if f"- **Provider**: {provider}" in seg or f"Provider: {provider}" in seg:
            match = re.search(r'\[View\]\((https?://[^)]+)\)', seg)
            if match:
                urls.append(match.group(1))
    return urls

def contact_flatfox(page, url: str, message: str, dry_run: bool = False):
    logger.info(f"Opening {url}...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        
        # 1. Force click the blocker or button via JS to reveal the form
        logger.info("Triggering contact form via JS...")
        page.evaluate('''() => {
            const blocker = document.querySelector('.contact-request-form-blocker');
            if (blocker) {
                blocker.click();
            } else {
                const btn = document.querySelector('button[name="contact-advertiser"]');
                if (btn) btn.click();
            }
        }''')
        page.wait_for_timeout(1000)

        # 2. Fill the textarea via fill()
        textarea = page.locator('textarea[name="text"]').first
        # If still not visible, force it
        if not textarea.is_visible():
            logger.info("Forcing form visibility via JS...")
            page.evaluate('''() => {
                const content = document.querySelector('[id*="contact-request-form-content"]');
                if (content) content.style.display = 'block';
            }''')
            page.wait_for_timeout(500)

        if textarea.count() > 0:
            logger.info("Filling message via JS...")
            page.evaluate('''(msg) => {
                const ta = document.querySelector('textarea[name="text"]');
                if (ta) {
                    ta.value = msg;
                    // Trigger events to notify JS frameworks
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }''', message)
            logger.info("Message filled via JS.")
            
            # Check name to confirm session
            name_val = page.evaluate('() => document.querySelector(\'input[name="name"]\')?.value || ""')
            logger.info(f"Session: {name_val}")

            if dry_run:
                logger.info("[Dry Run] Skipping final submit.")
                return True
            else:
                logger.info("Clicking final submit via JS...")
                page.evaluate('''() => {
                    const btns = document.querySelectorAll('button[name="contact-advertiser"]');
                    if (btns.length > 0) {
                        btns[btns.length - 1].click();
                    }
                }''')
                page.wait_for_timeout(3000)
                return True
        else:
            logger.error("Could not find contact form textarea.")
    except Exception as e:
        logger.error(f"Failed to contact {url}: {e}")
    return False

def run():
    parser = argparse.ArgumentParser(description="Auto Contact Apartments")
    parser.add_argument("--mode", choices=["filtered", "excluded"], default="excluded", help="Which listings to contact")
    parser.add_argument("--provider", choices=["flatfox", "homegate", "comparis"], default="flatfox", help="Provider to target")
    parser.add_argument("--limit", type=int, help="Limit number of contacts")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Do not actually click submit (default: True)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Actually click submit")
    args = parser.parse_args()

    # Load message template
    template_path = Path("message_template.txt")
    if not template_path.exists():
        logger.error("message_template.txt not found.")
        return
    message = template_path.read_text(encoding="utf-8")

    # Load URLs
    output_dir = Path("output")
    file_name = f"listings_{args.mode}_llm.md"
    urls = get_urls_from_markdown(output_dir / file_name, args.provider)
    
    if not urls:
        logger.info(f"No listings found for {args.provider} in {args.mode} mode.")
        return

    if args.limit:
        urls = urls[:args.limit]

    logger.info(f"Starting auto-contact for {len(urls)} listings (Mode: {args.mode}, Provider: {args.provider})...")

    # Load cookies
    cookie_file = Path(f"cookies_{args.provider}.txt")
    cookies = []
    if cookie_file.exists():
        domain = ".flatfox.ch" if args.provider == "flatfox" else f".{args.provider}.ch"
        cookies = parse_cookie_string(cookie_file.read_text(encoding="utf-8"), domain)
        logger.info(f"Loaded {len(cookies)} cookies.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 1024}
        )
        if cookies:
            context.add_cookies(cookies)
        
        page = context.new_page()
        
        for url in urls:
            success = contact_flatfox(page, url, message, dry_run=args.dry_run)
            if success:
                logger.info(f"Successfully processed {url}")
            time.sleep(2)
            
        browser.close()

if __name__ == "__main__":
    run()
