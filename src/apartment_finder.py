#!/usr/bin/env python3
import csv
import json
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

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency at runtime
    sync_playwright = None


MONEY_REGEX = re.compile(r"(\d[\d'., ]{2,})")
DATE_REGEX = re.compile(r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2})")
BEDROOM_REGEX = re.compile(
    r"(?:(\d+)\s*(?:bedroom|bedrooms|schlafzimmer|chambre|camera(?: da letto)?|zimmer))",
    re.IGNORECASE,
)
TOTAL_ROOMS_REGEX = re.compile(
    r"(?:(\d+(?:[.,]\d+)?)\s*(?:rooms|zimmer|pi[eè]ces|locali|stanze))",
    re.IGNORECASE,
)


NEGATIVE_SHARED = [
    "wg",
    "wohngemeinschaft",
    "shared",
    "roommate",
    "colocation",
    "stanza in appartamento",
    "sublet room",
    "single room",
    "chambre dans",
]

POSITIVE_FURNISHED = [
    "furnished",
    "möbliert",
    "mobilato",
    "ameubl",
]
POSITIVE_KITCHEN = ["kitchen", "küche", "cucina", "cuisine"]
POSITIVE_BATHROOM = ["bathroom", "badzimmer", "bad", "bagno", "salle de bain"]
POSITIVE_LIVING = ["living room", "wohnzimmer", "salotto", "soggiorno", "séjour"]
POSITIVE_SOFA = ["sofa", "couch", "canape", "divano"]
POSITIVE_WASH = ["washing machine", "waschmaschine", "lavatrice", "lave-linge"]
POSITIVE_DISH = ["dishwasher", "geschirrspüler", "lavastoviglie", "lave-vaisselle"]


@dataclass
class Listing:
    provider: str
    listing_id: str
    title: str
    url: str
    contact_url: str
    price_chf: Optional[float]
    bedrooms: Optional[float]
    total_rooms: Optional[float]
    available_from: Optional[date]
    furnished: Optional[bool]
    has_kitchen: Optional[bool]
    has_bathroom: Optional[bool]
    has_living_room: Optional[bool]
    has_sofa: Optional[bool]
    has_washing_machine: Optional[bool]
    has_dishwasher: Optional[bool]
    likely_shared: Optional[bool]
    address: Optional[str]
    description: str
    raw: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


def load_config(config_path: Path) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_price(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw)
    m = MONEY_REGEX.search(text)
    if not m:
        return None
    cleaned = m.group(1).replace("'", "").replace(" ", "")
    cleaned = cleaned.replace(",", ".") if cleaned.count(",") == 1 and "." not in cleaned else cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(raw: Any) -> Optional[date]:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return dt_parser.parse(text, dayfirst=True, fuzzy=True).date()
    except Exception:
        m = DATE_REGEX.search(text)
        if not m:
            return None
        try:
            return dt_parser.parse(m.group(1), dayfirst=True).date()
        except Exception:
            return None


def infer_bool_from_text(text: str, positive_keywords: List[str]) -> Optional[bool]:
    t = text.lower()
    if any(k in t for k in positive_keywords):
        return True
    return None


def infer_likely_shared(text: str) -> Optional[bool]:
    t = text.lower()
    if any(k in t for k in NEGATIVE_SHARED):
        return True
    return None


def infer_bedrooms(text: str) -> Tuple[Optional[float], Optional[float]]:
    m = BEDROOM_REGEX.search(text)
    bedrooms = float(m.group(1)) if m else None

    m2 = TOTAL_ROOMS_REGEX.search(text)
    total_rooms = float(m2.group(1).replace(",", ".")) if m2 else None
    return bedrooms, total_rooms


def to_absolute(base_url: str, maybe_url: Optional[str]) -> str:
    if not maybe_url:
        return base_url
    return urljoin(base_url, maybe_url)


def flatten_ld_json(doc: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(doc, dict):
        out.append(doc)
        for v in doc.values():
            out.extend(flatten_ld_json(v))
    elif isinstance(doc, list):
        for x in doc:
            out.extend(flatten_ld_json(x))
    return out


def try_flatfox_api(base_url: str, query: Dict[str, List[str]], timeout: int) -> List[Dict[str, Any]]:
    paths = [
        "/api/v1/public/search/",
        "/api/v1/public/listings/",
        "/api/public/search/",
        "/api/public/listings/",
    ]
    headers = {"User-Agent": "Mozilla/5.0 apartment-finder-bot"}
    for p in paths:
        url = urljoin(base_url, p)
        try:
            r = requests.get(url, params=query, headers=headers, timeout=timeout)
            if r.status_code >= 400:
                continue
            data = r.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("results", "objects", "items", "listings"):
                    if isinstance(data.get(key), list):
                        return data[key]
        except Exception:
            continue
    return []


def has_listing_cues(html: str) -> bool:
    signal = html.lower()
    cues = [
        "/rent/",
        "/listing/",
        "/offer/",
        "price_display",
        "number_of_rooms",
        "is_furnished",
    ]
    return any(c in signal for c in cues)


def is_challenge_html(html: str) -> bool:
    signal = html.lower()
    # Keep this strict: false positives are worse than missing a weak signal.
    strong_indicators = [
        "cloudflare",
        "cf-chl",
        "captcha",
        "hcaptcha",
        "recaptcha",
        "turnstile",
        "verify you are human",
        "attention required",
        "access denied",
        "security check",
    ]
    if not any(k in signal for k in strong_indicators):
        return False
    # If we can already see listing cues, do not treat it as a blocking challenge.
    return not has_listing_cues(html)


def parse_listings_from_html(base_url: str, html: str) -> List[Listing]:
    listings: List[Listing] = []
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.get_text(strip=True)
        if not raw:
            continue
        try:
            doc = json.loads(raw)
        except Exception:
            continue
        for node in flatten_ld_json(doc):
            if "url" not in node and "name" not in node:
                continue
            listing = build_listing_from_ld_json("flatfox", base_url, node)
            if listing:
                listings.append(listing)

    if not listings:
        link_candidates = soup.select('a[href*="/rent/"], a[href*="/listing/"], a[href*="/offer/"], a[href*="/de/"], a[href*="/it/"]')
        for idx, a in enumerate(link_candidates):
            href = a.get("href")
            title = normalize_spaces(a.get_text(" ", strip=True)) or f"Listing {idx+1}"
            if not href:
                continue
            url = to_absolute(base_url, href)
            listings.append(
                Listing(
                    provider="flatfox",
                    listing_id=str(abs(hash(url))),
                    title=title,
                    url=url,
                    contact_url=url,
                    price_chf=None,
                    bedrooms=None,
                    total_rooms=None,
                    available_from=None,
                    furnished=None,
                    has_kitchen=None,
                    has_bathroom=None,
                    has_living_room=None,
                    has_sofa=None,
                    has_washing_machine=None,
                    has_dishwasher=None,
                    likely_shared=None,
                    address=None,
                    description=title,
                )
            )
    return dedupe_listings(listings)


def fetch_with_playwright(search_url: str, playwright_cfg: Dict[str, Any]) -> Optional[str]:
    if not playwright_cfg.get("enabled", False):
        return None
    if sync_playwright is None:
        print("[flatfox] Playwright non disponibile. Esegui: pip install -r requirements.txt && playwright install chromium")
        return None

    headless = bool(playwright_cfg.get("headless", True))
    wait_after_load = float(playwright_cfg.get("wait_after_load_seconds", 6))
    challenge_wait = float(playwright_cfg.get("challenge_wait_seconds", 20))
    manual_continue = bool(playwright_cfg.get("manual_continue", False))
    cookies = playwright_cfg.get("cookies") or []
    extra_headers = playwright_cfg.get("extra_headers") or {}
    dump_html_path = playwright_cfg.get("dump_html_path")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(extra_http_headers=extra_headers if isinstance(extra_headers, dict) else None)
            if isinstance(cookies, list) and cookies:
                context.add_cookies(cookies)

            page = context.new_page()
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(int(wait_after_load * 1000))

            html = page.content()
            if is_challenge_html(html):
                print(f"[flatfox] Challenge rilevata. Attendo {challenge_wait:.0f}s per eventuale risoluzione automatica.")
                page.wait_for_timeout(int(challenge_wait * 1000))
                if manual_continue and not headless:
                    input("Risolvi eventuale challenge nel browser, poi premi Invio...")
                html = page.content()

            if dump_html_path:
                dump_path = Path(dump_html_path)
                dump_path.parent.mkdir(parents=True, exist_ok=True)
                dump_path.write_text(html, encoding="utf-8")

            browser.close()
            return html
    except Exception as exc:
        print(f"[flatfox] Errore Playwright: {exc}")
        return None


def fetch_flatfox_listings(
    search_url: str,
    timeout: int,
    detail_delay_s: float,
    use_playwright_fallback: bool = False,
    playwright_cfg: Optional[Dict[str, Any]] = None,
) -> List[Listing]:
    parsed = urlparse(search_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    query = parse_qs(parsed.query)
    headers = {"User-Agent": "Mozilla/5.0 apartment-finder-bot"}
    listings: List[Listing] = []

    api_items = try_flatfox_api(base_url, query, timeout)
    if api_items:
        for item in api_items:
            listings.append(build_listing_from_api_item("flatfox", base_url, item))
        return dedupe_listings(listings)

    html: Optional[str] = None
    try:
        response: Response = requests.get(search_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        html = response.text
    except requests.RequestException as exc:
        print(f"[flatfox] Errore rete/HTTP sulla pagina ricerca: {exc}")

    if html:
        listings = parse_listings_from_html(base_url, html)
        if is_challenge_html(html):
            print("[flatfox] HTML da requests sembra challenge/anti-bot (segnali forti, senza listing visibili).")

    if (not listings or (html and is_challenge_html(html))) and use_playwright_fallback:
        pw_html = fetch_with_playwright(search_url, playwright_cfg or {})
        if pw_html:
            listings = parse_listings_from_html(base_url, pw_html)
            if is_challenge_html(pw_html):
                print("[flatfox] Anche Playwright riceve challenge. Prova cookies reali o headless:false + manual_continue:true.")
            elif not listings:
                print("[flatfox] Playwright ha aperto la pagina ma non ho trovato listing parsabili. Salvo HTML di debug.")

    hydrate_flatfox_listing_details(listings, timeout=timeout, delay_s=detail_delay_s)
    return listings


def build_listing_from_api_item(provider: str, base_url: str, item: Dict[str, Any]) -> Listing:
    title = normalize_spaces(
        str(
            item.get("public_title")
            or item.get("short_title")
            or item.get("title")
            or item.get("description_title")
            or "Untitled listing"
        )
    )
    listing_url = (
        item.get("short_url")
        or (item.get("url", {}) or {}).get("default")
        or item.get("submit_url", {}).get("default")
        or item.get("url")
    )
    if isinstance(listing_url, dict):
        listing_url = listing_url.get("default")
    listing_url = to_absolute(base_url, listing_url)

    submit_url = item.get("submit_url", {})
    contact_url = (
        (submit_url.get("default") if isinstance(submit_url, dict) else submit_url)
        or item.get("live_viewing_url")
        or item.get("website_url")
        or listing_url
    )
    contact_url = to_absolute(base_url, contact_url)

    description = normalize_spaces(str(item.get("description") or ""))
    full_text = f"{title} {description}"
    inferred_bed, inferred_total = infer_bedrooms(full_text)

    return Listing(
        provider=provider,
        listing_id=str(item.get("pk") or item.get("id") or abs(hash(listing_url))),
        title=title,
        url=listing_url,
        contact_url=contact_url,
        price_chf=parse_price(item.get("price_display") or item.get("rent_gross") or item.get("rent_net")),
        bedrooms=parse_float(item.get("bedrooms")) or inferred_bed,
        total_rooms=parse_float(item.get("number_of_rooms")) or inferred_total,
        available_from=parse_date(item.get("moving_date") or item.get("available_from")),
        furnished=bool_or_none(item.get("is_furnished")) or infer_bool_from_text(full_text, POSITIVE_FURNISHED),
        has_kitchen=infer_bool_from_text(full_text, POSITIVE_KITCHEN),
        has_bathroom=infer_bool_from_text(full_text, POSITIVE_BATHROOM),
        has_living_room=infer_bool_from_text(full_text, POSITIVE_LIVING),
        has_sofa=infer_bool_from_text(full_text, POSITIVE_SOFA),
        has_washing_machine=infer_bool_from_text(full_text, POSITIVE_WASH),
        has_dishwasher=infer_bool_from_text(full_text, POSITIVE_DISH),
        likely_shared=infer_likely_shared(full_text),
        address=normalize_spaces(
            str(item.get("public_address") or f"{item.get('street', '')}, {item.get('city', '')}").strip(", ")
        ),
        description=description,
        raw=item,
    )


def build_listing_from_ld_json(provider: str, base_url: str, node: Dict[str, Any]) -> Optional[Listing]:
    title = normalize_spaces(str(node.get("name") or node.get("headline") or "Untitled listing"))
    url = to_absolute(base_url, node.get("url"))
    if url == base_url:
        return None
    offers = node.get("offers", {})
    description = normalize_spaces(str(node.get("description") or ""))
    full_text = f"{title} {description}"
    inferred_bed, inferred_total = infer_bedrooms(full_text)

    return Listing(
        provider=provider,
        listing_id=str(node.get("@id") or abs(hash(url))),
        title=title,
        url=url,
        contact_url=url,
        price_chf=parse_price(offers.get("price") if isinstance(offers, dict) else None),
        bedrooms=inferred_bed,
        total_rooms=inferred_total,
        available_from=None,
        furnished=infer_bool_from_text(full_text, POSITIVE_FURNISHED),
        has_kitchen=infer_bool_from_text(full_text, POSITIVE_KITCHEN),
        has_bathroom=infer_bool_from_text(full_text, POSITIVE_BATHROOM),
        has_living_room=infer_bool_from_text(full_text, POSITIVE_LIVING),
        has_sofa=infer_bool_from_text(full_text, POSITIVE_SOFA),
        has_washing_machine=infer_bool_from_text(full_text, POSITIVE_WASH),
        has_dishwasher=infer_bool_from_text(full_text, POSITIVE_DISH),
        likely_shared=infer_likely_shared(full_text),
        address=normalize_spaces(str(node.get("address") or "")),
        description=description,
        raw=node,
    )


def dedupe_listings(listings: List[Listing]) -> List[Listing]:
    by_url: Dict[str, Listing] = {}
    for l in listings:
        if l.url not in by_url:
            by_url[l.url] = l
    return list(by_url.values())


def hydrate_flatfox_listing_details(listings: List[Listing], timeout: int, delay_s: float) -> None:
    headers = {"User-Agent": "Mozilla/5.0 apartment-finder-bot"}
    for listing in listings:
        try:
            r = requests.get(listing.url, headers=headers, timeout=timeout)
            if r.status_code >= 400:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            text = normalize_spaces(soup.get_text(" ", strip=True))
            listing.description = normalize_spaces(f"{listing.description} {text}")[:3000]

            if listing.price_chf is None:
                listing.price_chf = parse_price(text)
            if listing.available_from is None:
                listing.available_from = parse_date(text)
            if listing.bedrooms is None or listing.total_rooms is None:
                b, t = infer_bedrooms(text)
                listing.bedrooms = listing.bedrooms or b
                listing.total_rooms = listing.total_rooms or t
            listing.furnished = listing.furnished or infer_bool_from_text(text, POSITIVE_FURNISHED)
            listing.has_kitchen = listing.has_kitchen or infer_bool_from_text(text, POSITIVE_KITCHEN)
            listing.has_bathroom = listing.has_bathroom or infer_bool_from_text(text, POSITIVE_BATHROOM)
            listing.has_living_room = listing.has_living_room or infer_bool_from_text(text, POSITIVE_LIVING)
            listing.has_sofa = listing.has_sofa or infer_bool_from_text(text, POSITIVE_SOFA)
            listing.has_washing_machine = listing.has_washing_machine or infer_bool_from_text(text, POSITIVE_WASH)
            listing.has_dishwasher = listing.has_dishwasher or infer_bool_from_text(text, POSITIVE_DISH)
            listing.likely_shared = listing.likely_shared or infer_likely_shared(text)

            contact_link = soup.select_one('a[href*="mailto:"], a[href*="/contact"], a[href*="submit"]')
            if contact_link and contact_link.get("href"):
                listing.contact_url = to_absolute(listing.url, contact_link.get("href"))
            time.sleep(delay_s)
        except Exception as exc:
            listing.warnings.append(f"detail_fetch_failed: {exc}")


def parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return None


def bool_or_none(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def listing_passes_filters(listing: Listing, criteria: Dict[str, Any]) -> bool:
    include_unknown = bool(criteria.get("include_unknowns_to_avoid_false_negatives", True))

    target_date = parse_date(criteria.get("available_on_or_before"))
    min_bedrooms = float(criteria.get("min_bedrooms", 2))

    if target_date:
        if listing.available_from and listing.available_from > target_date:
            return False
        if not listing.available_from and not include_unknown:
            return False

    inferred_bedrooms = listing.bedrooms
    if inferred_bedrooms is None and listing.total_rooms is not None:
        inferred_bedrooms = max(1.0, listing.total_rooms - 1.0)
    if inferred_bedrooms is not None and inferred_bedrooms < min_bedrooms:
        return False
    if inferred_bedrooms is None and not include_unknown:
        return False

    if criteria.get("must_be_furnished", True):
        if listing.furnished is False:
            return False
        if listing.furnished is None and not include_unknown:
            return False

    if criteria.get("must_have_private_entire_place", True):
        if listing.likely_shared is True:
            return False
        if listing.likely_shared is None and not include_unknown:
            return False

    for field_name, criterion_key in [
        ("has_kitchen", "must_have_kitchen"),
        ("has_bathroom", "must_have_bathroom"),
        ("has_living_room", "must_have_living_room"),
        ("has_sofa", "must_have_sofa"),
    ]:
        required = bool(criteria.get(criterion_key, True))
        value = getattr(listing, field_name)
        if required and value is False:
            return False
        if required and value is None and not include_unknown:
            return False

    return True


def annotate_uncertainties(listing: Listing) -> List[str]:
    flags = []
    if listing.available_from is None:
        flags.append("available_from_unknown")
    if listing.bedrooms is None and listing.total_rooms is None:
        flags.append("bedrooms_unknown")
    if listing.furnished is None:
        flags.append("furnished_not_explicit")
    if listing.has_kitchen is None:
        flags.append("kitchen_not_explicit")
    if listing.has_living_room is None:
        flags.append("living_room_not_explicit")
    if listing.has_sofa is None:
        flags.append("sofa_not_explicit")
    return flags


def export_results(listings: List[Listing], output_dir: Path, message_template_path: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for l in listings:
        rows.append(
            {
                "provider": l.provider,
                "listing_id": l.listing_id,
                "title": l.title,
                "price_chf": l.price_chf if l.price_chf is not None else "",
                "bedrooms": l.bedrooms if l.bedrooms is not None else "",
                "total_rooms": l.total_rooms if l.total_rooms is not None else "",
                "available_from": l.available_from.isoformat() if l.available_from else "",
                "furnished": l.furnished,
                "has_kitchen": l.has_kitchen,
                "has_bathroom": l.has_bathroom,
                "has_living_room": l.has_living_room,
                "has_sofa": l.has_sofa,
                "has_washing_machine": l.has_washing_machine,
                "has_dishwasher": l.has_dishwasher,
                "address": l.address or "",
                "listing_url": l.url,
                "contact_url": l.contact_url,
                "uncertainties": "|".join(annotate_uncertainties(l)),
            }
        )

    csv_path = output_dir / "listings_filtered.csv"
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    md_path = output_dir / "listings_filtered.md"
    message_template = ""
    if message_template_path.exists():
        message_template = message_template_path.read_text(encoding="utf-8").strip()

    lines = ["# Appartamenti idonei (ordinati per prezzo decrescente)", ""]
    for i, l in enumerate(listings, 1):
        price = f"CHF {l.price_chf:,.0f}".replace(",", "'") if l.price_chf is not None else "Prezzo non trovato"
        available = l.available_from.isoformat() if l.available_from else "Data non esplicita"
        uncertain = ", ".join(annotate_uncertainties(l)) or "nessuna"
        lines.extend(
            [
                f"## {i}. {l.title}",
                f"- Prezzo: {price}",
                f"- Camere da letto stimate: {l.bedrooms if l.bedrooms is not None else 'n/d'}",
                f"- Locali totali: {l.total_rooms if l.total_rooms is not None else 'n/d'}",
                f"- Disponibile da: {available}",
                f"- Arredato: {l.furnished}",
                f"- Lavatrice: {l.has_washing_machine}",
                f"- Lavastoviglie: {l.has_dishwasher}",
                f"- Incertezze estrazione: {uncertain}",
                f"- Annuncio: [{l.url}]({l.url})",
                f"- Contatta con 1 click: [{l.contact_url}]({l.contact_url})",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")

    contact_path = output_dir / "quick_contact_message.txt"
    contact_path.write_text(message_template, encoding="utf-8")


def sort_by_price_desc(listings: List[Listing]) -> List[Listing]:
    return sorted(listings, key=lambda x: (x.price_chf is not None, x.price_chf or -1), reverse=True)


def run(config_path: Path) -> int:
    cfg = load_config(config_path)
    search_cfg = cfg.get("search", {})
    criteria = cfg.get("criteria", {})
    output_dir = Path(search_cfg.get("output_dir", "output"))
    providers = search_cfg.get("providers", [])

    all_listings: List[Listing] = []

    if "flatfox" in providers:
        ff = search_cfg.get("flatfox", {})
        search_url = ff["search_url"]
        timeout = int(ff.get("request_timeout_seconds", 20))
        detail_delay = float(ff.get("detail_request_delay_seconds", 0.2))
        use_playwright_fallback = bool(ff.get("use_playwright_fallback", False))
        playwright_cfg = ff.get("playwright", {})
        all_listings.extend(
            fetch_flatfox_listings(
                search_url,
                timeout=timeout,
                detail_delay_s=detail_delay,
                use_playwright_fallback=use_playwright_fallback,
                playwright_cfg=playwright_cfg,
            )
        )

    filtered = [l for l in all_listings if listing_passes_filters(l, criteria)]
    ordered = sort_by_price_desc(filtered)

    message_path = Path(cfg.get("contact", {}).get("message_template_path", "message_template.txt"))
    export_results(ordered, output_dir, message_path)

    print(f"Listings trovati: {len(all_listings)}")
    print(f"Listings idonei: {len(ordered)}")
    print(f"Output: {output_dir.resolve()}")
    if len(all_listings) == 0:
        print(
            "Nessun annuncio recuperato. Se dal tuo PC Flatfox richiede login/sessione, posso integrare i cookie "
            "del browser nella configurazione per fare richieste autenticate."
        )
    return 0


if __name__ == "__main__":
    config_arg = Path("config.yaml")
    raise SystemExit(run(config_arg))
