import csv
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

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
        logging.FileHandler("apartment_finder.log", encoding="utf-8")
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

# Multi-language keyword maps
NEGATIVE_SHARED = [
    "wg", "wohngemeinschaft", "shared", "roommate", "colocation", 
    "stanza in appartamento", "sublet room", "single room", "chambre dans",
    "condiviso", "coabitazione", "mitbewohner"
]

KEYWORDS = {
    "furnished": ["furnished", "möbliert", "mobilato", "ameubl", "arredato", "completo di mobili"],
    "kitchen": ["kitchen", "küche", "cucina", "cuisine", "angolo cottura", "wohnküche"],
    "bathroom": ["bathroom", "badzimmer", "bad", "bagno", "salle de bain", "wc", "doccia"],
    "living": ["living room", "wohnzimmer", "salotto", "soggiorno", "séjour", "area giorno"],
    "sofa": ["sofa", "couch", "canape", "divano", "poltrona"],
    "washing_machine": ["washing machine", "waschmaschine", "lavatrice", "lave-linge", "waschturm"],
    "dishwasher": ["dishwasher", "geschirrspüler", "lavastoviglie", "lave-vaisselle", "spülmaschine"]
}

import math

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
    address: Optional[str] = None
    description: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_km: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * \
        math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def extract_coords(html: str) -> Tuple[Optional[float], Optional[float]]:
    # Try Flatfox bounding box pattern (center of similar objects search)
    m = re.search(r'north=([\d.]+)&amp;east=([\d.]+)&amp;south=([\d.]+)&amp;west=([\d.]+)', html)
    if m:
        n, e, s, w = map(float, m.groups())
        return (n + s) / 2, (e + w) / 2
        
    # Standard JSON patterns
    m = re.search(r'"latitude":\s*([\d.]+),"longitude":\s*([\d.]+)', html)
    if m:
        return float(m.group(1)), float(m.group(2))
    
    # Try alternate pattern
    m = re.search(r'lat[:=]\s*([\d.]+),?\s*lon[:=]\s*([\d.]+)', html, re.I)
    if m:
        return float(m.group(1)), float(m.group(2))
    
    return None, None

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
    
    # Try to find price near currency or rent hint first if it's a large text
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
    
    # Clean up all types of apostrophes and spaces
    raw_val = m.group(1).strip()
    if not raw_val: return None
    
    cleaned = re.sub(r"['’‘\s]", "", raw_val)
    
    # Handle thousands separators vs decimals
    if "." in cleaned and "," in cleaned:
        if cleaned.find(".") < cleaned.find(","): # 1.200,00
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else: # 1,200.00
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # 1,200 or 1,2
        if len(cleaned.split(",")[-1]) == 2: # Likely decimal
            cleaned = cleaned.replace(",", ".")
        else: # Likely thousands
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        # 1.200 or 1.2
        if len(cleaned.split(".")[-1]) == 2: # Decimal
            pass
        else: # Thousands
            cleaned = cleaned.replace(".", "")
            
    try:
        val = float(cleaned)
        if 8000 <= val <= 8999 and not context_found:
             return None
        return val
    except ValueError:
        return None
    
    # Handle decimal comma vs thousands separator
    if cleaned.count(",") == 1 and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
        
    try:
        val = float(cleaned)
        # Sanity check: ZIP codes in Zurich are 8000-8099. 
        # If it looks like a ZIP and we're parsing a large text without explicit CHF context, skip.
        if 8000 <= val <= 8999 and len(str(raw)) > 50 and "CHF" not in str(raw).upper():
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

def infer_bool_from_text(text: str, category: str) -> Optional[bool]:
    t = text.lower()
    keywords = KEYWORDS.get(category, [])
    if any(k in t for k in keywords):
        return True
    return None

def infer_likely_shared(text: str) -> Optional[bool]:
    t = text.lower()
    if any(k in t for k in NEGATIVE_SHARED):
        return True
    return None

def infer_bedrooms(text: str) -> Tuple[Optional[float], Optional[float]]:
    m = BEDROOM_REGEX.search(text)
    bedrooms = float(m.group(1).replace(",", ".")) if m else None

    m2 = TOTAL_ROOMS_REGEX.search(text)
    total_rooms = float(m2.group(1).replace(",", ".")) if m2 else None
    return bedrooms, total_rooms

def to_absolute(base_url: str, maybe_url: Optional[str]) -> str:
    if not maybe_url: return base_url
    return urljoin(base_url, maybe_url)

def is_challenge_html(html: str) -> bool:
    signal = html.lower()
    strong_indicators = [
        "cloudflare", "cf-chl", "captcha", "hcaptcha", "recaptcha", 
        "turnstile", "verify you are human", "attention required", 
        "access denied", "security check"
    ]
    if not any(k in signal for k in strong_indicators):
        return False
    # If we see actual listing prices or rooms, it's probably not a block
    cues = ["price_display", "number_of_rooms", "is_furnished", "chf", "rooms"]
    return not any(c in signal for c in cues)

def fetch_with_playwright(search_url: str, playwright_cfg: Dict[str, Any]) -> Optional[str]:
    if not playwright_cfg.get("enabled", False):
        return None
    if sync_playwright is None:
        logger.warning("Playwright not available. Install with: pip install playwright && playwright install chromium")
        return None

    headless = bool(playwright_cfg.get("headless", True))
    wait_after_load = float(playwright_cfg.get("wait_after_load_seconds", 6))
    challenge_wait = float(playwright_cfg.get("challenge_wait_seconds", 20))
    manual_continue = bool(playwright_cfg.get("manual_continue", False))
    cookies = playwright_cfg.get("cookies") or []
    dump_html_path = playwright_cfg.get("dump_html_path")

    try:
        with sync_playwright() as p:
            logger.info(f"Launching browser (headless={headless})...")
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            if cookies:
                context.add_cookies(cookies)

            page = context.new_page()
            logger.info(f"Navigating to {search_url}...")
            page.goto(search_url, wait_until="networkidle", timeout=60000)
            
            # Wait for content or challenge
            page.wait_for_timeout(int(wait_after_load * 1000))
            
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

    # Try LD+JSON first
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            doc = json.loads(script.get_text(strip=True))
            nodes = doc if isinstance(doc, list) else [doc]
            for node in nodes:
                if isinstance(node, dict) and ("Product" in str(node.get("@type")) or "Accommodation" in str(node.get("@type"))):
                    l = build_listing_from_ld_json("flatfox", base_url, node)
                    if l: listings.append(l)
        except Exception:
            pass

    # Specific Flatfox listing cards
    for thumb in soup.select(".listing-thumb"):
        link_tag = thumb.select_one("a.listing-thumb__image, a.listing-thumb-title")
        if not link_tag: continue
        href = link_tag.get("href")
        if not href: continue
        
        url = to_absolute(base_url, href)
        title = normalize_spaces(thumb.select_one(".listing-thumb-title").get_text()) if thumb.select_one(".listing-thumb-title") else "Untitled"
        price_tag = thumb.select_one(".price")
        price = parse_price(price_tag.get_text()) if price_tag else None
        
        # Address/Location
        loc_tag = thumb.select_one(".listing-thumb-title__location")
        address = normalize_spaces(loc_tag.get_text()) if loc_tag else ""
        
        # Attributes (Furnished, etc.)
        attrs_text = normalize_spaces(thumb.select_one(".attributes").get_text(" ")) if thumb.select_one(".attributes") else ""
        
        listings.append(Listing(
            provider="flatfox",
            listing_id=str(abs(hash(url))),
            title=title,
            url=url,
            contact_url=url,
            price_chf=price,
            bedrooms=None, # Will hydrate
            total_rooms=None, # Will hydrate
            available_from=None, # Will hydrate
            furnished=infer_bool_from_text(attrs_text + " " + title, "furnished"),
            has_kitchen=None,
            has_bathroom=None,
            has_living_room=None,
            has_sofa=None,
            has_washing_machine=None,
            has_dishwasher=None,
            likely_shared=infer_likely_shared(attrs_text + " " + title),
            address=address,
            description=attrs_text
        ))

    # General fallback
    if not listings:
        logger.info("No specific listing cards found, falling back to broad link scraping.")
        for a in soup.select('a[href*="/flat/"], a[href*="/listing/"], a[href*="/rent/"]'):
            href = a.get("href")
            if not href or len(href) < 10: continue
            url = to_absolute(base_url, href)
            listings.append(Listing(
                provider="flatfox", listing_id=str(abs(hash(url))),
                title=normalize_spaces(a.get_text()) or "Listing", url=url, contact_url=url,
                price_chf=None, bedrooms=None, total_rooms=None, available_from=None,
                furnished=None, has_kitchen=None, has_bathroom=None, has_living_room=None,
                has_sofa=None, has_washing_machine=None, has_dishwasher=None,
                likely_shared=None, address=None, description=""
            ))
    
    return dedupe_listings(listings)

def build_listing_from_ld_json(provider: str, base_url: str, node: Dict[str, Any]) -> Optional[Listing]:
    url = to_absolute(base_url, node.get("url"))
    if url == base_url: return None
    
    title = normalize_spaces(node.get("name", "Untitled"))
    desc = normalize_spaces(node.get("description", ""))
    full_text = f"{title} {desc}"
    
    price = None
    offers = node.get("offers")
    if isinstance(offers, dict):
        price = parse_price(offers.get("price"))
    
    bed, tot = infer_bedrooms(full_text)
    
    return Listing(
        provider=provider,
        listing_id=str(node.get("@id") or abs(hash(url))),
        title=title,
        url=url,
        contact_url=url,
        price_chf=price,
        bedrooms=bed,
        total_rooms=tot,
        available_from=parse_date(full_text), # Try to find date in text if not explicit
        furnished=infer_bool_from_text(full_text, "furnished"),
        has_kitchen=infer_bool_from_text(full_text, "kitchen"),
        has_bathroom=infer_bool_from_text(full_text, "bathroom"),
        has_living_room=infer_bool_from_text(full_text, "living"),
        has_sofa=infer_bool_from_text(full_text, "sofa"),
        has_washing_machine=infer_bool_from_text(full_text, "washing_machine"),
        has_dishwasher=infer_bool_from_text(full_text, "dishwasher"),
        likely_shared=infer_likely_shared(full_text),
        address=normalize_spaces(str(node.get("address", ""))),
        description=desc,
        raw=node
    )

def dedupe_listings(listings: List[Listing]) -> List[Listing]:
    seen = {}
    for l in listings:
        if l.url not in seen:
            seen[l.url] = l
    return list(seen.values())

def hydrate_details(listings: List[Listing], timeout: int, delay: float):
    total = len(listings)
    logger.info(f"Hydrating details for {total} listings (estimated time: {total * (delay + 1):.0f}s)...")
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    for idx, l in enumerate(listings, 1):
        try:
            if idx % 5 == 0 or idx == 1 or idx == total:
                logger.info(f"[{idx}/{total}] Processing: {l.title[:30]}...")
            
            r = requests.get(l.url, headers=headers, timeout=timeout)
            if r.status_code >= 400:
                logger.warning(f"Failed to fetch {l.url}: HTTP {r.status_code}")
                continue
            
            soup = BeautifulSoup(r.text, "html.parser")
            # Remove script and style elements from text extraction
            for script_or_style in soup(["script", "style"]):
                script_or_style.decompose()
                
            text = normalize_spaces(soup.get_text(" ", strip=True))
            l.description = text[:5000]
            
            # Update fields
            l.price_chf = l.price_chf or parse_price(text)
            l.available_from = l.available_from or parse_date(text)
            b, t = infer_bedrooms(text)
            l.bedrooms = l.bedrooms or b
            l.total_rooms = l.total_rooms or t
            
            # Extract coordinates and calculate distance
            l.lat, l.lon = extract_coords(r.text)
            if l.lat and l.lon:
                l.distance_km = haversine(l.lat, l.lon, OFFICE_LAT, OFFICE_LON)
            
            for cat in KEYWORDS:
                field_map = {
                    "furnished": "furnished",
                    "sofa": "has_sofa",
                    "living": "has_living_room",
                    "kitchen": "has_kitchen",
                    "bathroom": "has_bathroom",
                    "washing_machine": "has_washing_machine",
                    "dishwasher": "has_dishwasher"
                }
                field_name = field_map.get(cat)
                if field_name:
                    current_val = getattr(l, field_name)
                    if current_val is None:
                        setattr(l, field_name, infer_bool_from_text(text, cat))
            
            if l.likely_shared is None:
                l.likely_shared = infer_likely_shared(text)
            
            # Contact button
            contact_btn = soup.select_one('a[href*="/contact"], a[href*="mailto:"], button[data-href*="contact"]')
            if contact_btn:
                l.contact_url = to_absolute(l.url, contact_btn.get("href") or contact_btn.get("data-href"))
            
            time.sleep(delay)
        except Exception as e:
            logger.error(f"Error hydrating {l.url}: {e}")
            l.warnings.append(f"hydration_error: {e}")

def listing_passes_filters(listing: Listing, criteria: Dict[str, Any]) -> Tuple[bool, List[str]]:
    include_unknown = bool(criteria.get("include_unknowns_to_avoid_false_negatives", True))
    reasons = []
    
    # Date filter
    target_date = parse_date(criteria.get("available_on_or_before"))
    if target_date and listing.available_from:
        if listing.available_from > target_date:
            reasons.append(f"Available date {listing.available_from} > {target_date}")
    elif target_date and not include_unknown and not listing.available_from:
        reasons.append("Available date unknown")

    # Bedrooms filter
    min_bed = float(criteria.get("min_bedrooms", 2))
    current_bed = listing.bedrooms
    if current_bed is None and listing.total_rooms:
        current_bed = max(1.0, listing.total_rooms - 1.0)
    
    if current_bed is not None and current_bed < min_bed:
        reasons.append(f"Bedrooms {current_bed} < {min_bed}")
    elif current_bed is None and not include_unknown:
        reasons.append("Bedrooms unknown")

    # Boolean flags
    filters = [
        ("must_be_furnished", "furnished", "Not furnished"),
        ("must_have_private_entire_place", "likely_shared", "Likely shared/WG", True), # negate
        ("must_have_kitchen", "has_kitchen", "No kitchen"),
        ("must_have_bathroom", "has_bathroom", "No bathroom"),
        ("must_have_living_room", "has_living_room", "No living room"),
        ("must_have_sofa", "has_sofa", "No sofa"),
    ]
    
    for crit_key, field_name, error_msg, *negate in filters:
        required = criteria.get(crit_key, False)
        if not required: continue
        
        val = getattr(listing, field_name)
        is_negated = negate[0] if negate else False
        
        actual_val = not val if is_negated and val is not None else val
        
        if actual_val is False:
            reasons.append(error_msg)
        elif val is None and not include_unknown:
            reasons.append(f"{error_msg} (unknown)")
            
    return len(reasons) == 0, reasons

def run(config_path: Path):
    cfg = load_config(config_path)
    search_cfg = cfg.get("search", {})
    criteria = cfg.get("criteria", {})
    output_dir = Path(search_cfg.get("output_dir", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    
    all_listings: List[Listing] = []
    
    if "flatfox" in search_cfg.get("providers", []):
        ff = search_cfg["flatfox"]
        logger.info("Starting Flatfox search...")
        
        html = None
        try:
            r = requests.get(ff["search_url"], timeout=ff.get("request_timeout_seconds", 20))
            if not is_challenge_html(r.text):
                html = r.text
            else:
                logger.info("Direct request blocked by anti-bot, trying Playwright...")
        except Exception as e:
            logger.warning(f"Direct request failed: {e}")
            
        if not html and ff.get("use_playwright_fallback"):
            html = fetch_with_playwright(ff["search_url"], ff.get("playwright", {}))
            
        if html:
            found = parse_listings_from_html(urljoin(ff["search_url"], "/"), html)
            logger.info(f"Found {len(found)} initial listings.")
            hydrate_details(found, ff.get("request_timeout_seconds", 20), ff.get("detail_request_delay_seconds", 0.5))
            all_listings.extend(found)
        else:
            logger.error("Could not retrieve search page.")

    # Filtering and Auditing
    filtered = []
    excluded = []
    for l in all_listings:
        is_pass, reasons = listing_passes_filters(l, criteria)
        if is_pass:
            filtered.append(l)
        else:
            excluded.append((l, reasons))

    # Sorting
    ordered = sorted(filtered, key=lambda x: (x.price_chf or 999999), reverse=True)
    
    logger.info(f"Total: {len(all_listings)}, Filtered: {len(ordered)}, Excluded: {len(excluded)}")
    
    # Export Filtered
    md_path = output_dir / "listings_filtered.md"
    lines = ["# Zurich Apartment Search Results", f"Generated on {date.today()}", ""]
    for i, l in enumerate(ordered, 1):
        price = f"CHF {l.price_chf:,.0f}".replace(",", "'") if l.price_chf else "Unknown"
        dist_info = f"{l.distance_km:.2f} km" if l.distance_km is not None else "Unknown"
        lines.extend([
            f"## {i}. {l.title}",
            f"- **Price**: {price}",
            f"- **Distance to Office**: {dist_info}",
            f"- **Bedrooms**: {l.bedrooms or 'n/a'} (Total rooms: {l.total_rooms or 'n/a'})",
            f"- **Available**: {l.available_from or 'Unknown'}",
            f"- **Furnished**: {l.furnished}",
            f"- **Address**: {l.address or 'See listing'}",
            f"- **Features**: Kitchen: {l.has_kitchen}, Living: {l.has_living_room}, Sofa: {l.has_sofa}",
            f"- **Optional**: Wash: {l.has_washing_machine}, Dish: {l.has_dishwasher}",
            f"- [View Listing]({l.url})",
            f"- [**Contact with 1 Click**]({l.contact_url})",
            ""
        ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    
    # Export Excluded for Audit
    excl_path = output_dir / "listings_excluded.md"
    excl_lines = ["# Excluded Listings (Audit Log)", f"Generated on {date.today()}", ""]
    for i, (l, reasons) in enumerate(excluded, 1):
        price = f"CHF {l.price_chf:,.0f}".replace(",", "'") if l.price_chf else "Unknown"
        reasons_str = ", ".join(reasons)
        excl_lines.extend([
            f"## {i}. {l.title}",
            f"- **REASONS FOR EXCLUSION**: **{reasons_str}**",
            f"- **Price**: {price}",
            f"- **Bedrooms**: {l.bedrooms or 'n/a'} (Total rooms: {l.total_rooms or 'n/a'})",
            f"- **Available**: {l.available_from or 'Unknown'}",
            f"- **Address**: {l.address or 'n/a'}",
            f"- [View Listing]({l.url})",
            ""
        ])
    excl_path.write_text("\n".join(excl_lines), encoding="utf-8")
    
    logger.info(f"Results exported to {md_path} and audit log to {excl_path}")

if __name__ == "__main__":
    run(Path("config.yaml"))
