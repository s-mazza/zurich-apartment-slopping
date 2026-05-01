import pytest
from pathlib import Path
from datetime import date
from src.apartment_finder import (
    parse_price, parse_date, infer_bool_from_text, 
    infer_likely_shared, infer_bedrooms, haversine,
    Listing, listing_passes_filters, extract_coords
)

# --- Price Parsing Tests (10 tests) ---

@pytest.mark.parametrize("input_str, expected", [
    ("2'500", 2500.0),
    ("CHF 3’200.50", 3200.5),
    ("Price: 1‘800.-", 1800.0),
    ("1,200", 1200.0),
    ("4.500,00", 4500.0),
    ("8005", None), # ZIP code filter (Zurich 8000-8099)
    ("8005 CHF", 8005.0), # Context override
    ("Total gross rent is 2950 CHF per month", 2950.0),
    ("No price here", None),
    (None, None),
])
def test_parse_price(input_str, expected):
    assert parse_price(input_str) == expected

# --- Date Parsing Tests (8 tests) ---

@pytest.mark.parametrize("input_str, expected", [
    ("2026-06-15", date(2026, 6, 15)),
    ("15.06.2026", date(2026, 6, 15)),
    ("Available from 1st July 2026", date(2026, 7, 1)),
    ("Verfügbar ab 01.05.2026", date(2026, 5, 1)),
    ("immediately", None), # Fuzzy parser might fail on just "immediately"
    ("15/06/26", date(2026, 6, 15)),
    ("", None),
    ("random text", None),
])
def test_parse_date(input_str, expected):
    # Some dates might be parsed relative to current year if only month/day provided, 
    # but we focus on explicit formats
    res = parse_date(input_str)
    if expected:
        assert res == expected
    else:
        assert res is None

# --- Multi-language Keyword Tests (10 tests) ---

@pytest.mark.parametrize("text, category, expected", [
    ("Fully furnished apartment", "furnished", True),
    ("Möblierte Wohnung", "furnished", True),
    ("Arredato con gusto", "furnished", True),
    ("Modern kitchen with dishwasher", "kitchen", True),
    ("Cucina abitabile", "kitchen", True),
    ("Wohnzimmer con divano", "living", True),
    ("Area giorno ampia", "living", True),
    ("Sofa and TV", "sofa", True),
    ("Waschmaschine in der Wohnung", "washing_machine", True),
    ("Empty room", "furnished", None),
])
def test_infer_bool_from_text(text, category, expected):
    assert infer_bool_from_text(text, category) == expected

# --- Shared Apartment Detection (5 tests) ---

@pytest.mark.parametrize("text, expected", [
    ("WG-Zimmer in Zürich", True),
    ("Shared apartment for students", True),
    ("Roommate wanted", True),
    ("Stanza in coabitazione", True),
    ("Entire 3.5 room apartment", None), # Not shared
])
def test_infer_likely_shared(text, expected):
    assert infer_likely_shared(text) == expected

# --- Bedroom/Room Extraction (5 tests) ---

@pytest.mark.parametrize("text, expected_bed, expected_tot", [
    ("3.5 rooms with 2 bedrooms", 2.0, 3.5),
    ("4.5 Zimmer Wohnung", None, 4.5),
    ("2 Schlafzimmer", 2.0, None),
    ("3 locali e 2 camere", 2.0, 3.0),
    ("Apartment with rooms", None, None),
])
def test_infer_bedrooms(text, expected_bed, expected_tot):
    bed, tot = infer_bedrooms(text)
    assert bed == expected_bed
    assert tot == expected_tot

# --- Distance Calculation (4 tests) ---

def test_haversine():
    # Zurich HB to Europaallee (approx)
    lat1, lon1 = 47.3773, 8.5402
    lat2, lon2 = 47.3781, 8.5342
    dist = haversine(lat1, lon1, lat2, lon2)
    assert 0.4 < dist < 0.6 # Roughly 500m

def test_extract_coords():
    html = '... north=47.38&amp;east=8.53&amp;south=47.36&amp;west=8.51 ...'
    lat, lon = extract_coords(html)
    assert lat == (47.38 + 47.36) / 2
    assert lon == (8.53 + 8.51) / 2

# --- Filtering Logic (10 tests) ---

@pytest.fixture
def base_listing():
    return Listing(
        provider="test", listing_id="1", title="Test", url="http", contact_url="http",
        price_chf=2000.0, bedrooms=2.0, total_rooms=3.0, available_from=date(2026, 6, 1),
        furnished=True, has_kitchen=True, has_bathroom=True, has_living_room=True,
        has_sofa=True, likely_shared=False, address="Zurich", description="Desc"
    )

def test_filter_pass(base_listing):
    criteria = {"min_bedrooms": 2, "must_be_furnished": True, "available_on_or_before": "2026-06-15"}
    is_pass, reasons = listing_passes_filters(base_listing, criteria)
    assert is_pass is True
    assert len(reasons) == 0

def test_filter_fail_bedrooms(base_listing):
    base_listing.bedrooms = 1.0
    criteria = {"min_bedrooms": 2}
    is_pass, reasons = listing_passes_filters(base_listing, criteria)
    assert is_pass is False
    assert any("Bedrooms" in r for r in reasons)

def test_filter_fail_date(base_listing):
    base_listing.available_from = date(2026, 7, 1)
    criteria = {"available_on_or_before": "2026-06-15"}
    is_pass, reasons = listing_passes_filters(base_listing, criteria)
    assert is_pass is False
    assert any("Available date" in r for r in reasons)

def test_filter_fail_furnished(base_listing):
    base_listing.furnished = False
    criteria = {"must_be_furnished": True}
    is_pass, reasons = listing_passes_filters(base_listing, criteria)
    assert is_pass is False
    assert "Not furnished" in reasons

def test_filter_fail_shared(base_listing):
    base_listing.likely_shared = True
    criteria = {"must_have_private_entire_place": True}
    is_pass, reasons = listing_passes_filters(base_listing, criteria)
    assert is_pass is False
    assert "Likely shared/WG" in reasons

def test_filter_fail_kitchen(base_listing):
    base_listing.has_kitchen = False
    criteria = {"must_have_kitchen": True}
    is_pass, reasons = listing_passes_filters(base_listing, criteria)
    assert is_pass is False
    assert "No kitchen" in reasons

def test_filter_include_unknown(base_listing):
    base_listing.furnished = None
    criteria = {"must_be_furnished": True, "include_unknowns_to_avoid_false_negatives": True}
    is_pass, reasons = listing_passes_filters(base_listing, criteria)
    assert is_pass is True

def test_filter_exclude_unknown(base_listing):
    base_listing.furnished = None
    criteria = {"must_be_furnished": True, "include_unknowns_to_avoid_false_negatives": False}
    is_pass, reasons = listing_passes_filters(base_listing, criteria)
    assert is_pass is False
    assert "Not furnished (unknown)" in reasons
