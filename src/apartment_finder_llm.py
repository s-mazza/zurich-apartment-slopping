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
    strong_indicators = ["cloudflare", "captcha", "verify you are human"]
    if not any(k in signal for k in strong_indicators): return False
    cues = ["price_display", "chf", "rooms"]
    return not any(c in signal for c in cues)

def fetch_with_playwright(search_url: str, playwright_cfg: Dict[str, Any]) -> Optional[str]:
    if not playwright_cfg.get("enabled", False): return None
    if sync_playwright is None: return None
    headless = bool(playwright_cfg.get("headless", True))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            page = context.new_page()
            page.goto(search_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(5000)
            html = page.content()
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

def llm_extract_details(description: str, hf_token: str, model_id: str) -> Dict[str, Any]:
    if not hf_token: return {}
    trimmed_desc = description[:2500]
    prompt = f"""[INST] Task: Extract apartment data from a Swiss listing.
Check carefully if it is a 'temporary' rental (sublet, 'befristet', 'Untermiete', 'temporaneo', 'periodo limitato', 'short term').
Check if it is a 'shared flat' (WG, Mitbewohner, room for rent, stanza, camera, coabitazione).

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

def hydrate_details(listings: List[Listing], timeout: int, delay: float, llm_cfg: Dict[str, Any]):
    SHARED_KEYWORDS = ["mitbewohner", "wg-zimmer", "wohngemeinschaft", "shared flat", "stanza in", "roommate", "coloc"]
    TEMP_KEYWORDS = ["befristet", "untermiete", "sublet", "temporary", "short term", "fino al", "bis zum"]
    total = len(listings)
    hf_token = llm_cfg.get("token")
    model_id = llm_cfg.get("model_id")
    logger.info(f"Hydrating {total} listings via Hybrid LLM ({model_id})...")
    for idx, l in enumerate(listings, 1):
        try:
            logger.info(f"[{idx}/{total}] Processing: {l.title[:30]}...")
            r = requests.get(l.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            if r.status_code >= 400: continue
            soup = BeautifulSoup(r.text, "html.parser")
            for s in soup(["script", "style"]): s.decompose()
            text = normalize_spaces(soup.get_text(" ", strip=True))
            l.description = text[:4000]
            l.lat, l.lon = extract_coords(r.text)
            if l.lat and l.lon: l.distance_km = haversine(l.lat, l.lon, OFFICE_LAT, OFFICE_LON)
            
            desc_lower = l.description.lower()
            if any(k in desc_lower for k in SHARED_KEYWORDS): l.likely_shared = True
            if any(k in desc_lower for k in TEMP_KEYWORDS): l.is_temporary = True
            
            data = llm_extract_details(l.description, hf_token, model_id)
            l.likely_shared = l.likely_shared or data.get("likely_shared", False)
            l.is_temporary = l.is_temporary or data.get("is_temporary", False)
            l.furnished = data.get("furnished", l.furnished)
            l.has_kitchen = data.get("has_kitchen", l.has_kitchen)
            l.has_living_room = data.get("has_living_room", l.has_living_room)
            l.has_sofa = data.get("has_sofa", l.has_sofa)
            l.has_washing_machine = data.get("has_washing_machine", l.has_washing_machine)
            l.has_dishwasher = data.get("has_dishwasher", l.has_dishwasher)
            l.price_chf = l.price_chf or parse_price(text)
            l.available_from = parse_date(data.get("available_from")) or parse_date(text)
            l.bedrooms = data.get("bedrooms") or l.bedrooms
            l.total_rooms = data.get("total_rooms") or l.total_rooms
            time.sleep(delay)
        except Exception as e: logger.error(f"Error {l.url}: {e}")

def listing_passes_filters(listing: Listing, criteria: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons = []
    target_date = parse_date(criteria.get("available_on_or_before"))
    if target_date and listing.available_from and listing.available_from > target_date: reasons.append("Date late")
    min_bed = float(criteria.get("min_bedrooms", 2))
    cur_bed = listing.bedrooms or (max(1.0, listing.total_rooms - 1.0) if listing.total_rooms else None)
    if cur_bed is not None and cur_bed < min_bed: reasons.append("Too few bedrooms")
    if criteria.get("must_be_furnished") and listing.furnished is False: reasons.append("Not furnished")
    if criteria.get("must_have_private_entire_place") and listing.likely_shared: reasons.append("Likely shared")
    if criteria.get("must_be_indefinite", True) and listing.is_temporary: reasons.append("Temporary/Sublet")
    return len(reasons) == 0, reasons

from urllib.parse import urlencode

def run(config_path: Path):
    cfg = load_config(config_path)
    search_cfg = cfg.get("search", {})
    criteria = cfg.get("criteria", {})
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("token"): return
    output_dir = Path(search_cfg.get("output_dir", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    all_listings: List[Listing] = []
    if "flatfox" in search_cfg.get("providers", []):
        ff = search_cfg["flatfox"]
        base_search_url = ff.get("base_url", "https://flatfox.ch/it/search/")
        params = ff.get("params", {})
        search_url = f"{base_search_url}?{urlencode(params)}"
        
        logger.info(f"Starting Flatfox search (LLM Mode) ({search_url})...")
        html = None
        try:
            r = requests.get(search_url, timeout=20)
            html = r.text if not is_challenge_html(r.text) else None
        except Exception: pass
        if not html: html = fetch_with_playwright(search_url, ff.get("playwright", {}))
        if html:
            found = parse_listings_from_html(urljoin(search_url, "/"), html)
            hydrate_details(found, 20, 1.0, llm_cfg)
            all_listings.extend(found)
    
    filtered, excluded = [], []
    for l in all_listings:
        p, r = listing_passes_filters(l, criteria)
        if p: filtered.append(l)
        else: excluded.append((l, r))
        
    ordered = sorted(filtered, key=lambda x: (x.price_chf or 999999), reverse=True)
    md_path = output_dir / "listings_filtered_llm.md"
    lines = ["# Zurich Apartment Results (LLM Mode)", ""]
    for l in ordered:
        lines.extend([f"## {l.title}", f"- **Price**: {l.price_chf}", f"- **Distance**: {l.distance_km:.2f} km", f"- [View]({l.url})", ""])
    md_path.write_text("\n".join(lines))
    
    excl_path = output_dir / "listings_excluded_llm.md"
    excl_lines = ["# Excluded (LLM Mode)", ""]
    for l, r in excluded:
        excl_lines.extend([f"## {l.title}", f"- **REASONS**: {', '.join(r)}", f"- [View]({l.url})", ""])
    excl_path.write_text("\n".join(excl_lines))
    logger.info(f"Done. Results in {md_path}")

if __name__ == "__main__":
    run(Path("config.yaml"))
