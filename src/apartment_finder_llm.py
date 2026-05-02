import argparse
import csv
import json
import logging
import re
import time
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse, urlencode

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dt_parser
from requests import Response

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("apartment_finder_llm.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

# Regex patterns
MONEY_REGEX = re.compile(r"(\d[\d'’‘,. ]*)")
DATE_REGEX = re.compile(r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2})")
BEDROOM_REGEX = re.compile(
    r"(?:(?<!\.)(\d+)\s*(?:bedroom|bedrooms|schlafzimmer|chambre|camera|camere|stanze da letto))",
    re.IGNORECASE,
)
TOTAL_ROOMS_REGEX = re.compile(
    r"(?:(\d+(?:[.,]\d+)?)\s*(?:rooms|zimmer|pi[eè]ces|locali|stanze))",
    re.IGNORECASE,
)

# Reference office coordinates: Europaallee 1, Zurich
OFFICE_LAT = 47.3781
OFFICE_LON = 8.5342

@dataclass
class Listing:
    provider: str
    listing_id: str
    title: str
    url: str
    contact_url: str
    price_chf: Optional[float] = None
    bedrooms: Optional[float] = None
    total_rooms: Optional[float] = None
    available_from: Optional[date] = None
    furnished: Optional[bool] = None
    has_kitchen: Optional[bool] = None
    has_bathroom: Optional[bool] = None
    has_living_room: Optional[bool] = None
    has_sofa: Optional[bool] = None
    has_washing_machine: Optional[bool] = None
    has_dishwasher: Optional[bool] = None
    likely_shared: Optional[bool] = None
    is_temporary: Optional[bool] = None
    address: Optional[str] = None
    description: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_km: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

def load_config(config_path: Path) -> Dict[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load config from {config_path}: {e}")
        raise

def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

def parse_price(raw: Any) -> Optional[float]:
    if raw is None: return None
    if isinstance(raw, (int, float)): return float(raw)
    text = str(raw)
    context_found = False
    if len(text) > 50:
        chf_match = re.search(r"(?:CHF|affitto|rent|prezzo|preis|gross|net)\s*[:\-\s]*([\d'’‘,. ]{3,})", text, re.I)
        if chf_match:
            text = chf_match.group(1)
            context_found = True
    elif "CHF" in text.upper():
        context_found = True
            
    m = MONEY_REGEX.search(text)
    if not m: return None
    raw_val = m.group(1).strip()
    if not raw_val: return None
    cleaned = re.sub(r"['’‘\s]", "", raw_val)
    
    if "." in cleaned and "," in cleaned:
        if cleaned.find(".") < cleaned.find(","):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        if len(cleaned.split(",")[-1]) == 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        if len(cleaned.split(".")[-1]) == 2:
            pass
        else:
            cleaned = cleaned.replace(".", "")
            
    try:
        val = float(cleaned)
        if 8000 <= val <= 8999 and not context_found:
             return None
        return val
    except ValueError:
        return None

def parse_date(raw: Any) -> Optional[date]:
    if raw is None: return None
    if isinstance(raw, date): return raw
    text = str(raw).strip()
    if not text: return None
    try:
        return dt_parser.parse(text, dayfirst=True, fuzzy=True).date()
    except Exception:
        m = DATE_REGEX.search(text)
        if not m: return None
        try:
            return dt_parser.parse(m.group(1), dayfirst=True).date()
        except Exception:
            return None

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * \
        math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def extract_coords(html: str) -> Tuple[Optional[float], Optional[float]]:
    m = re.search(r'north=([\d.]+)&amp;east=([\d.]+)&amp;south=([\d.]+)&amp;west=([\d.]+)', html)
    if m:
        n, e, s, w = map(float, m.groups())
        return (n + s) / 2, (e + w) / 2
    m = re.search(r'"latitude":\s*([\d.]+),"longitude":\s*([\d.]+)', html)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None

def is_challenge_html(html: str) -> bool:
    signal = html.lower()
    strong_indicators = ["cloudflare", "captcha", "verify you are human", "javascript and disable any ad blocker", "datadome", "initialstate"]
    if not any(k in signal for k in strong_indicators): return False
    cues = ["price_display", "chf", "rooms", "matching-list"]
    return not any(c in signal for c in cues)

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

def fetch_with_playwright(search_url: str, playwright_cfg: Dict[str, Any]) -> Optional[str]:
    if not playwright_cfg.get("enabled", False): return None
    if sync_playwright is None: return None
    headless = bool(playwright_cfg.get("headless", True))
    wait_time = int(playwright_cfg.get("wait_after_load_seconds", 8)) * 1000
    challenge_wait = float(playwright_cfg.get("challenge_wait_seconds", 20))
    manual_continue = bool(playwright_cfg.get("manual_continue", False))
    dump_html_path = playwright_cfg.get("dump_html_path")
    
    cookies = playwright_cfg.get("cookies") or []
    cookies_file = playwright_cfg.get("cookies_file")
    parsed_url = urlparse(search_url)
    domain = parsed_url.netloc
    if not domain.startswith("."): domain = "." + domain
    
    if cookies_file and Path(cookies_file).exists():
        logger.info(f"Loading cookies from {cookies_file} for {domain}...")
        try:
            content = Path(cookies_file).read_text(encoding="utf-8")
            cookies.extend(parse_cookie_string(content, domain))
        except Exception as e:
            logger.error(f"Failed to read cookies: {e}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1920, 'height': 1080}
            )
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
            """)
            if cookies: context.add_cookies(cookies)
            page = context.new_page()
            logger.info(f"Navigating to {search_url}...")
            page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(wait_time)
            
            html = page.content()
            if is_challenge_html(html):
                logger.warning("Bot challenge detected. Waiting for resolution...")
                page.wait_for_timeout(int(challenge_wait * 1000))
                if manual_continue and not headless:
                    input("Solve the challenge in the browser, then press Enter here...")
                html = page.content()
            
            if dump_html_path:
                Path(dump_html_path).parent.mkdir(parents=True, exist_ok=True)
                Path(dump_html_path).write_text(html, encoding="utf-8")
                logger.info(f"Debug HTML dumped to {dump_html_path}")

            browser.close()
            return html
    except Exception as exc:
        logger.error(f"Playwright error: {exc}")
        return None

def parse_listings_from_html(base_url: str, html: str) -> List[Listing]:
    listings: List[Listing] = []
    soup = BeautifulSoup(html, "html.parser")
    for thumb in soup.select(".listing-thumb"):
        link_tag = thumb.select_one("a.listing-thumb__image, a.listing-thumb-title")
        if not link_tag: continue
        href = link_tag.get("href")
        if not href: continue
        url = urljoin(base_url, href)
        listings.append(Listing(
            provider="flatfox", listing_id=str(abs(hash(url))),
            title=normalize_spaces(thumb.select_one(".listing-thumb-title").get_text()) if thumb.select_one(".listing-thumb-title") else "Untitled",
            url=url, contact_url=url, address=normalize_spaces(thumb.select_one(".listing-thumb-title__location").get_text()) if thumb.select_one(".listing-thumb-title__location") else ""
        ))
    return list({l.url: l for l in listings}.values())

def _find_homegate_results(node: Any) -> List[Dict[str, Any]]:
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            keys = set().union(*(set(x.keys()) for x in node if isinstance(x, dict)))
            if any(k in keys for k in ("id", "listingId", "slug")): return node
        for item in node:
            found = _find_homegate_results(item)
            if found: return found
    elif isinstance(node, dict):
        for key in ("results", "items", "listings", "hits"):
            value = node.get(key)
            if isinstance(value, list):
                found = _find_homegate_results(value)
                if found: return found
        for value in node.values():
            found = _find_homegate_results(value)
            if found: return found
    return []

def _find_key_recursive(obj, key):
    if isinstance(obj, dict):
        if key in obj: return obj[key]
        for v in obj.values():
            res = _find_key_recursive(v, key)
            if res is not None: return res
    elif isinstance(obj, list):
        for i in obj:
            res = _find_key_recursive(i, key)
            if res is not None: return res
    return None

def parse_listings_from_html_homegate(base_url: str, html: str) -> Tuple[List[Listing], bool, int]:
    listings: List[Listing] = []
    has_next = False
    total_results = 0
    
    json_data = None
    for pattern in [r'window\.__INITIAL_STATE__\s*=\s*', r'window\.__PINIA_INITIAL_STATE__\s*=\s*']:
        match = re.search(pattern, html)
        if match:
            try:
                start_index = match.end()
                decoder = json.JSONDecoder()
                json_data, _ = decoder.raw_decode(html[start_index:])
                break
            except Exception: pass
            
    if not json_data:
        next_data_tag = BeautifulSoup(html, "html.parser").select_one('script#__NEXT_DATA__')
        if next_data_tag:
            try:
                json_data = json.loads(next_data_tag.get_text())
            except Exception: pass
            
    if json_data:
        try:
            # Homegate verification: resultList.search.fullSearch.result
            res_obj = _find_key_recursive(json_data, "result") or {}
            items = res_obj.get("listings", [])
            has_next = res_obj.get("hasNextPage", False)
            total_results = res_obj.get("resultCount", 0)

            if not items: # Fallback to broader result find
                items = _find_homegate_results(json_data)

            for item in items:
                listing_data = item.get("listing") or item
                id_ = item.get("id") or listing_data.get("id") or listing_data.get("listingId")
                if not id_ or not str(id_).isdigit(): continue
                
                url = urljoin(base_url, f"/rent/{id_}")
                
                # Nested data mapping
                prices = listing_data.get("prices", {})
                price_val = prices.get("rent", {}).get("gross") or prices.get("rent", {}).get("net") or listing_data.get("price")
                price = parse_price(price_val)
                
                chars = listing_data.get("characteristics", {})
                rooms = chars.get("totalRooms") or listing_data.get("rooms")
                
                addr = listing_data.get("address", {})
                geo = addr.get("geoCoordinates") or addr.get("geo") or _find_key_recursive(item, "geoCoordinates")
                lat, lon = None, None
                if geo:
                    lat = geo.get("latitude") or geo.get("lat")
                    lon = geo.get("longitude") or geo.get("lon")
                
                dist = haversine(lat, lon, OFFICE_LAT, OFFICE_LON) if lat and lon else None

                listings.append(Listing(
                    provider="homegate", listing_id=str(id_),
                    title=listing_data.get("localization", {}).get("de", {}).get("text", {}).get("title") or listing_data.get("title") or "Homegate Listing",
                    url=url, contact_url=url,
                    price_chf=price, total_rooms=float(rooms) if rooms else None,
                    address=f"{addr.get('street', '')}, {addr.get('postalCode', '')} {addr.get('locality', '')}".strip(", "),
                    lat=lat, lon=lon, distance_km=dist
                ))
            if listings: return list({l.url: l for l in listings}.values()), has_next, total_results
        except Exception as e:
            logger.debug(f"Homegate JSON mapping failed: {e}")

    # Final Link fallback
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select('a[href*="/rent/"], a[href*="/mieten/"]'):
        href = a.get("href")
        if not href or len(href) < 15: continue
        if any(x in href for x in ["/rent/real-estate", "/mieten/immobilien", "city-zurich", "/matching-list"]): continue
        url = urljoin(base_url, href)
        title = normalize_spaces(a.get_text(" ", strip=True)) or "Homegate Listing"
        price = parse_price(title)
        rooms_match = re.search(r'(\d+(?:\.\d+)?)\s*rooms', title, re.I)
        rooms = float(rooms_match.group(1)) if rooms_match else None
        listings.append(Listing(
            provider="homegate", listing_id=str(abs(hash(url))),
            title=title, url=url, contact_url=url,
            price_chf=price, total_rooms=rooms, description=title
        ))
    return list({l.url: l for l in listings}.values()), False, len(listings)

def llm_extract_details(description: str, hf_token: str, model_id: str) -> Dict[str, Any]:
    if not hf_token: return {}
    trimmed_desc = description[:2500]
    prompt = f"""[INST] Task: Analyze this Zurich apartment listing and extract data.
Determine if it is a shared apartment (WG/roommate) or entire place.
Check carefully if it is a 'temporary' rental (sublet, 'befristet', 'Untermiete').

JSON Keys required:
- furnished (bool)
- has_kitchen (bool)
- has_bathroom (bool)
- has_living_room (bool)
- has_sofa (bool)
- has_washing_machine (bool)
- has_dishwasher (bool)
- likely_shared (bool)
- is_temporary (bool)
- bedrooms (float)
- total_rooms (float)
- available_from (YYYY-MM-DD or null)

Description:
{trimmed_desc}

Output ONLY the JSON object. [/INST]"""
    headers = {"Authorization": f"Bearer {hf_token}"}
    api_url = f"https://api-inference.huggingface.co/models/{model_id}"
    try:
        payload = {
            "inputs": prompt, 
            "parameters": {"return_full_text": False, "temperature": 0.1, "max_new_tokens": 1500},
            "options": {"wait_for_model": True}
        }
        response = requests.post(api_url, headers=headers, json=payload, timeout=90)
        if response.status_code != 200: return {}
        res_json = response.json()
        raw_text = res_json[0].get("generated_text", "") if isinstance(res_json, list) else res_json.get("generated_text", "")
        clean_text = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL).strip()
        json_match = re.search(r'\{.*\}', clean_text, re.DOTALL)
        return json.loads(json_match.group(0)) if json_match else {}
    except Exception: return {}

def listing_passes_filters(listing: Listing, criteria: Dict[str, Any]) -> Tuple[bool, List[str]]:
    include_unknown = bool(criteria.get("include_unknowns_to_avoid_false_negatives", True))
    reasons = []
    
    # Price filter
    max_price = criteria.get("max_price")
    if max_price and listing.price_chf:
        if listing.price_chf > float(max_price):
            reasons.append(f"Price CHF {listing.price_chf} > {max_price}")
    elif max_price and not include_unknown and listing.price_chf is None:
        reasons.append("Price unknown")

    # Date filter
    target_date = parse_date(criteria.get("available_on_or_before"))
    if target_date:
        if listing.available_from:
            if listing.available_from > target_date:
                reasons.append(f"Date late ({listing.available_from})")
        elif not include_unknown:
            reasons.append("Date unknown")

    # Bedrooms filter
    min_bed = float(criteria.get("min_bedrooms", 2))
    cur_bed = listing.bedrooms or (max(1.0, listing.total_rooms - 1.0) if listing.total_rooms else None)
    if cur_bed is not None:
        if cur_bed < min_bed:
            reasons.append(f"Too few bedrooms ({cur_bed})")
    elif not include_unknown:
        reasons.append("Bedrooms unknown")

    # Boolean flags
    checks = [
        ("must_be_furnished", "furnished", "Not furnished"),
        ("must_have_private_entire_place", "likely_shared", "Likely shared", True), # negate
        ("must_be_indefinite", "is_temporary", "Temporary/Sublet", True) # negate
    ]
    
    for crit_key, field_name, error_msg, *negate in checks:
        required = criteria.get(crit_key, False)
        if not required: continue
        
        val = getattr(listing, field_name)
        is_negated = negate[0] if negate else False
        
        if val is None:
            if not include_unknown:
                reasons.append(f"{error_msg} (unknown)")
            continue
            
        actual_val = not val if is_negated else val
        if actual_val is False:
            reasons.append(error_msg)
            
    return len(reasons) == 0, reasons

def hydrate_details(listings: List[Listing], timeout: int, delay: float, llm_cfg: Dict[str, Any]):
    SHARED_KEYWORDS = ["mitbewohner", "wg-zimmer", "wohngemeinschaft", "shared flat", "stanza in", "roommate", "coloc"]
    TEMP_KEYWORDS = ["befristet", "untermiete", "sublet", "temporary", "short term", "fino al", "bis zum"]
    total = len(listings)
    hf_token = llm_cfg.get("token")
    model_id = llm_cfg.get("model_id")
    logger.info(f"Hydrating {total} listings via Hybrid LLM...")
    
    for idx, l in enumerate(listings, 1):
        try:
            logger.info(f"[{idx}/{total}] Processing ({l.provider}): {l.title[:30]}...")
            
            # Try to catch obvious shared/temp from title first
            desc_lower = l.title.lower()
            if any(k in desc_lower for k in SHARED_KEYWORDS): l.likely_shared = True
            if any(k in desc_lower for k in TEMP_KEYWORDS): l.is_temporary = True

            # Try to fetch detail page
            r = None
            try:
                r = requests.get(l.url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}, timeout=timeout)
                if r.status_code >= 400:
                    logger.debug(f"Detail fetch failed with {r.status_code} for {l.url}")
                    r = None
            except Exception: r = None

            if r:
                soup = BeautifulSoup(r.text, "html.parser")
                for s in soup(["script", "style"]): s.decompose()
                text = normalize_spaces(soup.get_text(" ", strip=True))
                l.description = text[:4000]
                if l.lat is None:
                    l.lat, l.lon = extract_coords(r.text)
                    if l.lat and l.lon: l.distance_km = haversine(l.lat, l.lon, OFFICE_LAT, OFFICE_LON)
            else:
                # If detail page fails, use the title for LLM inference as a last resort
                l.description = l.title
            
            # Update keywords from description if we got it
            desc_lower = l.description.lower()
            if any(k in desc_lower for k in SHARED_KEYWORDS): l.likely_shared = True
            if any(k in desc_lower for k in TEMP_KEYWORDS): l.is_temporary = True
            
            # Use LLM (either on full desc or just title)
            data = llm_extract_details(l.description, hf_token, model_id)
            
            # Merge data
            l.likely_shared = l.likely_shared or data.get("likely_shared", False)
            l.is_temporary = l.is_temporary or data.get("is_temporary", False)
            l.furnished = data.get("furnished", l.furnished)
            l.has_kitchen = data.get("has_kitchen", l.has_kitchen)
            l.has_living_room = data.get("has_living_room", l.has_living_room)
            l.has_sofa = data.get("has_sofa", l.has_sofa)
            l.has_washing_machine = data.get("has_washing_machine", l.has_washing_machine)
            l.has_dishwasher = data.get("has_dishwasher", l.has_dishwasher)
            
            # Only update price/date if not already set by structured search result
            if l.price_chf is None: l.price_chf = parse_price(l.description)
            if l.available_from is None:
                l.available_from = parse_date(data.get("available_from")) or parse_date(l.description)
            if l.bedrooms is None:
                l.bedrooms = data.get("bedrooms")
            
            time.sleep(delay)
        except Exception as e: logger.error(f"Error {l.url}: {e}")

def run(config_path: Path, providers_override: Optional[List[str]] = None):
    cfg = load_config(config_path)
    search_cfg = cfg.get("search", {})
    criteria = cfg.get("criteria", {})
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("token"): return
    output_dir = Path(search_cfg.get("output_dir", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    all_listings: List[Listing] = []
    
    providers = providers_override or search_cfg.get("providers", [])
    for provider in providers:
        if provider not in search_cfg: continue
        p_cfg = search_cfg[provider]
        base_search_url = p_cfg.get("base_url")
        params = p_cfg.get("params", {}).copy()
        
        page = 1
        has_next = True
        provider_listings = []
        logger.info(f"Starting {provider} search...")
        
        while has_next and page <= 25: # Safety limit
            if provider == "homegate":
                params["ep"] = page
            
            search_url = f"{base_search_url}?{urlencode(params)}"
            logger.info(f"[{provider}] Fetching page {page}...")
            
            html = fetch_with_playwright(search_url, p_cfg.get("playwright", {}))
            if not html: break
                
            if provider == "flatfox":
                found = parse_listings_from_html(urljoin(search_url, "/"), html)
                has_next = False
            elif provider == "homegate":
                found, has_next, total = parse_listings_from_html_homegate(urljoin(search_url, "/"), html)
                logger.info(f"[{provider}] Found {len(found)} listings on page {page} (Total available: {total})")
            else:
                found, has_next = [], False
            
            provider_listings.extend(found)
            if not has_next: break
            page += 1
            time.sleep(1)
            
        logger.info(f"[{provider}] Completed search. Total collected: {len(provider_listings)}")
        hydrate_details(provider_listings, 20, 1.0, llm_cfg)
        all_listings.extend(provider_listings)
    
    filtered, excluded = [], []
    for l in all_listings:
        p, r = listing_passes_filters(l, criteria)
        if p: filtered.append(l)
        else: excluded.append((l, r))
        
    ordered = sorted(filtered, key=lambda x: (x.price_chf or 999999), reverse=True)
    md_path = output_dir / "listings_filtered_llm.md"
    lines = ["# Zurich Apartment Results (LLM Mode)", ""]
    for l in ordered:
        price = f"CHF {l.price_chf:,.0f}".replace(",", "'") if l.price_chf else "Unknown"
        lines.extend([f"## {l.title}", f"- **Price**: {price}", f"- **Provider**: {l.provider}", f"- **Distance**: {l.distance_km:.2f} km" if l.distance_km else "- **Distance**: Unknown", f"- [View]({l.url})", ""])
    md_path.write_text("\n".join(lines))
    
    excl_path = output_dir / "listings_excluded_llm.md"
    excl_lines = ["# Excluded (LLM Mode)", ""]
    for l, r in excluded:
        excl_lines.extend([f"## {l.title}", f"- **REASONS**: {', '.join(r)}", f"- **Provider**: {l.provider}", f"- [View]({l.url})", ""])
    excl_path.write_text("\n".join(excl_lines))
    logger.info(f"Done. Total: {len(all_listings)}, Filtered: {len(filtered)}. Results in {md_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apartment finder (LLM mode)")
    parser.add_argument("--config", default="config.yaml", help="Path to config")
    parser.add_argument("--providers", help="Comma-separated list of providers")
    args = parser.parse_args()
    selected = [p.strip().lower() for p in args.providers.split(",")] if args.providers else None
    run(Path(args.config), selected)
