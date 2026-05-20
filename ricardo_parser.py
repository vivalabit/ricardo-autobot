import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from scrapling.fetchers import StealthySession

from settings import get_proxy_from_env

DEFAULT_OUTPUT_DIR = Path("data/openclaw")
DEFAULT_RAW_DIR = Path("data/raw")
MAX_BID_HISTORY = 10
DEFAULT_SEARCH_RESULT_LIMIT = 15
DEFAULT_SEARCH_SCAN_LIMIT = 60
DEFAULT_SEARCH_ENRICH_LIMIT = 3
SHIPPING_METHOD_KEYWORDS = ["versand", "postversand", "paket", "porto", "kurier", "sperrgut"]
SEARCH_QUERY_ALIASES = [
    (r"\b(?:видео\s*карт\w*|видеокарт\w*|графическ\w*\s+карт\w*)\b", ["grafikkarte", "gpu"]),
    (r"\b(?:ноутбук\w*|лэптоп\w*)\b", ["laptop", "notebook"]),
    (r"\b(?:телефон\w*|смартфон\w*)\b", ["smartphone", "handy"]),
    (r"\b(?:наушник\w*|гарнитур\w*)\b", ["kopfhörer", "headphones"]),
    (r"\bмонитор\w*\b", ["monitor"]),
    (r"\b(?:велосипед\w*|велик\w*)\b", ["velo", "fahrrad"]),
    (r"\bпылесос\w*\b", ["staubsauger"]),
    (r"\b(?:камер\w*|фотоаппарат\w*)\b", ["kamera"]),
    (r"\bчас\w*\b", ["uhr"]),
]
GERMAN_SEARCH_TERM_REPLACEMENTS = [
    (r"\b(?:для\s+игр|игров\w*)\b", "gaming"),
    (r"\b(?:нов\w*|новый|новая|новое)\b", "neu"),
    (r"\b(?:б\s*/?\s*у|бу|подержанн\w*)\b", "gebraucht"),
]


def get_meta(soup, *, name=None, property_=None):
    if name:
        tag = soup.find("meta", attrs={"name": name})
    else:
        tag = soup.find("meta", attrs={"property": property_})

    return tag.get("content", "").strip() if tag else None


def clean(text):
    if not text:
        return None
    return re.sub(r"\s+", " ", str(text)).strip()


def parse_price(text):
    if not text:
        return None

    match = re.search(r"CHF\s*[\d'’.,]+", text)
    return match.group(0) if match else None


def parse_number_value(value):
    if value is None:
        return None

    normalized = re.sub(r"[^\d'’.,]", "", str(value))
    normalized = normalized.replace("'", "").replace("’", "")

    if "," in normalized and "." in normalized:
        normalized = normalized.replace(",", "")
    else:
        normalized = normalized.replace(",", ".")

    try:
        return round(float(normalized), 2)
    except ValueError:
        return None


def cents_to_chf(value):
    if value is None:
        return None

    try:
        return round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return None


def parse_money_value(text):
    price = parse_price(text)
    if not price:
        return None

    return parse_number_value(price)


def parse_money_values(text):
    if not text:
        return []

    values = []
    for match in re.finditer(r"CHF\s*[\d'’.,]+", str(text)):
        value = parse_number_value(match.group(0))
        if value is not None:
            values.append(value)

    return values


def parse_location(text):
    if not text:
        return None

    match = re.search(r"in\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\- ]+)", text)
    if not match:
        return None

    location = clean(match.group(1))
    location = re.split(r"\s+online kaufen|\s+auf ricardo", location, flags=re.IGNORECASE)[0]
    return clean(location)


def parse_percent(text):
    if not text:
        return None

    match = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*%", text)
    if not match:
        return None

    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_bid_count(text):
    if not text:
        return None

    match = re.search(r"(\d+)\s*(?:gebote|gebot|bids?|bieter)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_price_line(text):
    if not text:
        return None

    match = re.fullmatch(r"\d+(?:[.'’]\d{3})*(?:[.,]\d{1,2})?", clean(text) or "")
    return parse_number_value(match.group(0)) if match else None


def parse_sales_count(text):
    if not text:
        return None

    match = re.search(
        r"(\d[\d'’]*)\s*(?:verkäufe|bewertungen|bewertung|reviews|sales)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None

    return int(match.group(1).replace("'", "").replace("’", ""))


def parse_listing_id(page_url):
    path = urlparse(page_url).path
    match = re.search(r"(\d{6,})(?:/)?$", path)
    if match:
        return match.group(1)

    return re.sub(r"\W+", "-", path.strip("/")).strip("-") or "ricardo-item"


def build_ricardo_search_url(query, language="de", *, min_price_chf=None, max_price_chf=None):
    language = language if language in {"de", "fr", "it", "en"} else "de"
    url = f"https://www.ricardo.ch/{language}/s/{quote(clean(query) or '', safe='')}/"
    params = []
    if max_price_chf is not None:
        params.append(("range_filters.price.max", int(max_price_chf)))
    if min_price_chf is not None:
        params.append(("range_filters.price.min", int(min_price_chf)))

    if params:
        url = f"{url}?{urlencode(params)}"

    return url


def append_unique(values, value):
    value = clean(value)
    if value and value not in values:
        values.append(value)


def contains_cyrillic(text):
    return bool(re.search(r"[А-Яа-яЁё]", text or ""))


def germanize_search_terms(text):
    value = clean(text) or ""
    for pattern, replacement in GERMAN_SEARCH_TERM_REPLACEMENTS:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)

    value = re.sub(r"[А-Яа-яЁё]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ,;:-")
    return clean(value)


def build_ricardo_search_queries(query):
    cleaned = clean(query) or ""
    variants = []
    search_in_german_only = contains_cyrillic(cleaned)
    if not search_in_german_only:
        append_unique(variants, cleaned)

    for pattern, aliases in SEARCH_QUERY_ALIASES:
        if not re.search(pattern, cleaned, flags=re.IGNORECASE):
            continue

        remainder = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
        remainder = germanize_search_terms(remainder)
        for alias in aliases:
            if remainder:
                append_unique(variants, f"{alias} {remainder}")
                append_unique(variants, f"{remainder} {alias}")
            append_unique(variants, alias)
        if not search_in_german_only:
            append_unique(variants, remainder)

    return variants


def extract_search_listing_urls(html, base_url="https://www.ricardo.ch/"):
    return [candidate["url"] for candidate in extract_search_card_candidates(html, base_url)]


def normalize_ricardo_listing_url(url, base_url="https://www.ricardo.ch/"):
    absolute_url = urljoin(base_url, url or "")
    parsed = urlparse(absolute_url)
    if not parsed.netloc.endswith("ricardo.ch"):
        return None
    if not re.search(r"/(?:de|fr|it|en)/a/.+-\d{6,}/?$", parsed.path):
        return None

    return parsed._replace(query="", fragment="").geturl()


def extract_search_card_money_values(text):
    values = []
    for match in re.finditer(r"(?<![\w:])\d{1,3}(?:[’']\d{3})*(?:\.\d{2})(?!\w)", text or ""):
        value = parse_number_value(match.group(0))
        if value is not None:
            values.append(value)
    return values


def extract_search_card_title(link):
    ignored_alts = {"beliebt", "ricardo ai icon", "moneyguard"}
    image = link.find("img", alt=True)
    for image in link.find_all("img", alt=True):
        alt = clean(image.get("alt"))
        if not alt or alt.lower() in ignored_alts:
            continue
        return alt

    ignored_texts = ignored_alts | {"|"}
    title_candidates = []
    price_pattern = re.compile(r"^\d{1,3}(?:[’']\d{3})*(?:\.\d{2})$")

    for element in link.find_all(["span", "p", "h2", "h3"]):
        text = clean(element.get_text(" ", strip=True))
        if not text or text.lower() in ignored_texts:
            continue
        if price_pattern.match(text) or parse_bid_count(text) is not None:
            continue
        if "sofort kaufen" in text.lower():
            continue
        title_candidates.append(text)

    if title_candidates:
        return max(title_candidates, key=len)

    return None


def search_card_sale_format(text, money_values):
    low = (text or "").lower()
    has_auction = "gebot" in low or "bieter" in low
    has_buy_now = "sofort kaufen" in low

    if has_auction and has_buy_now:
        return "auction_with_buy_now"
    if has_auction:
        return "auction_only"
    if has_buy_now or len(money_values) == 1:
        return "buy_now_only"
    return "unknown"


def search_candidate_risk_flags(*parts):
    text = " ".join(clean(part) or "" for part in parts).lower()
    risky_words = [
        "defekt",
        "ungetestet",
        "bastler",
        "ersatzteil",
        "artefakte",
        "funktioniert nicht",
    ]
    return [word for word in risky_words if word in text]


def build_search_page_candidate(link, base_url, search_query=None):
    url = normalize_ricardo_listing_url(link.get("href"), base_url)
    if not url:
        return None

    title = extract_search_card_title(link)
    text = clean(link.get_text(" ", strip=True)) or ""
    summary = text.replace(title, "", 1) if title else text
    summary = re.sub(r"^(?:beliebt\s*)?[|,\s-]+", "", summary, flags=re.IGNORECASE)
    summary = clean(summary)

    money_values = extract_search_card_money_values(text)
    sale_format_value = search_card_sale_format(text, money_values)
    price_chf = money_values[0] if money_values else None
    buy_now_price_chf = None
    auction_current_price_chf = None
    if sale_format_value == "auction_with_buy_now":
        auction_current_price_chf = money_values[0] if money_values else None
        buy_now_price_chf = money_values[1] if len(money_values) > 1 else None
    elif sale_format_value == "auction_only":
        auction_current_price_chf = money_values[0] if money_values else None
    elif sale_format_value == "buy_now_only":
        buy_now_price_chf = money_values[0] if money_values else None

    image = link.find("img", src=True)
    bid_count = parse_bid_count(text)
    listing_id = parse_listing_id(url)

    return {
        "listing_id": listing_id,
        "title": title,
        "url": url,
        "price_chf": price_chf,
        "buy_now_price_chf": buy_now_price_chf,
        "auction_current_price_chf": auction_current_price_chf,
        "auction_next_minimum_bid_chf": None,
        "sale_format": sale_format_value,
        "condition": None,
        "location": None,
        "shipping_cost_chf": None,
        "shipping": None,
        "pickup_only": None,
        "seller": None,
        "seller_rating_percent": None,
        "seller_sales_count": None,
        "bid_count": bid_count,
        "auction_end_at": None,
        "active_check": {
            "active": True,
            "confidence": "search_page",
            "has_action_signal": True,
            "reasons": [],
        },
        "risk_flags": search_candidate_risk_flags(title, summary),
        "description_excerpt": summary[:260] if summary else None,
        "primary_image_url": image.get("src") if image else None,
        "matched_search_query": search_query,
        "candidate_source": "search_page_card",
    }


def extract_search_card_candidates(html, base_url="https://www.ricardo.ch/", search_query=None):
    soup = BeautifulSoup(html or "", "html.parser")
    seen = set()
    candidates = []

    for link in soup.find_all("a", href=True):
        candidate = build_search_page_candidate(link, base_url, search_query)
        if not candidate:
            continue

        if candidate["url"] in seen:
            continue

        seen.add(candidate["url"])
        candidates.append(candidate)

    return candidates


def extract_line_after(lines, keywords):
    for i, line in enumerate(lines):
        low = line.lower()
        if any(keyword in low for keyword in keywords):
            return lines[i + 1] if i + 1 < len(lines) else line

    return None


def contains_keyword_word(text, keywords):
    if not text:
        return False

    low = str(text).lower()
    return any(re.search(r"(?<!\w)" + re.escape(keyword.lower()) + r"(?!\w)", low) for keyword in keywords)


def is_ricardo_listing_title(text):
    return "auf ricardo kaufen" in str(text or "").lower()


def has_shipping_method(text):
    low = str(text or "").lower()
    return contains_keyword_word(low, SHIPPING_METHOD_KEYWORDS) or any(term in low for term in ["a-post", "b-post"])


def extract_section_text(lines, keywords, following_lines=2, whole_word=False):
    for i, line in enumerate(lines):
        if is_ricardo_listing_title(line):
            continue

        low = line.lower()
        matched = contains_keyword_word(line, keywords) if whole_word else any(keyword in low for keyword in keywords)
        if matched:
            parts = [line]
            parts.extend(lines[i + 1 : i + 1 + following_lines])
            return clean(" ".join(parts))

    return None


def sale_format(has_auction, has_buy_now):
    if has_auction and has_buy_now:
        return "auction_with_buy_now"
    if has_auction:
        return "auction_only"
    if has_buy_now:
        return "buy_now_only"
    return "unknown"


def strip_ricardo_title(title):
    if not title:
        return None

    return clean(re.sub(r"\s*\|\s*Kaufen auf Ricardo\s*$", "", title))


def extract_offer(product):
    if not isinstance(product, dict):
        return {}

    offers = product.get("offers") or {}
    if isinstance(offers, list):
        return next((offer for offer in offers if isinstance(offer, dict)), {})
    if isinstance(offers, dict):
        return offers
    return {}


def extract_offer_price(offer):
    price = offer.get("price") if isinstance(offer, dict) else None
    currency = offer.get("priceCurrency") if isinstance(offer, dict) else None

    if price is None:
        return None, None

    return parse_number_value(price), currency


def extract_offer_seller(offer):
    seller = offer.get("seller") if isinstance(offer, dict) else None
    if isinstance(seller, dict):
        return seller.get("name")

    return seller if isinstance(seller, str) else None


def extract_offer_end(offer):
    if not isinstance(offer, dict):
        return None

    return offer.get("availabilityEnds") or offer.get("priceValidUntil")


def extract_condition(product, offer):
    condition = offer.get("itemCondition") if isinstance(offer, dict) else None
    if not condition and isinstance(product, dict):
        condition = product.get("itemCondition")
    if not condition:
        return None

    condition = str(condition).rsplit("/", 1)[-1]
    return re.sub(r"Condition$", "", condition)


def extract_category_path(product):
    if not isinstance(product, dict):
        return []

    category = product.get("category")
    if isinstance(category, str):
        return [clean(category)]

    if not isinstance(category, list):
        return []

    names = []
    for item in category:
        if isinstance(item, str):
            names.append(clean(item))
            continue

        if not isinstance(item, dict):
            continue

        value = item.get("name")
        if not value:
            url = item.get("url") or item.get("@id")
            if url:
                path = urlparse(url).path.strip("/")
                slug = path.rsplit("/", 1)[-1]
                if not slug or slug.lower() in {"de", "fr", "it", "en"}:
                    continue
                slug = re.sub(r"-\d+$", "", slug)
                value = slug.replace("-", " ").title()

        if value:
            names.append(clean(value))

    return [name for name in names if name]


def is_pickup_only(text):
    if not text:
        return False

    low = text.lower()
    has_pickup = contains_keyword_word(low, ["abholung", "pickup"])
    explicit_pickup_only = any(
        term in low
        for term in [
            "nur abholung",
            "nur selbstabholung",
            "kein versand",
            "keine lieferung",
            "ohne versand",
            "pickup only",
        ]
    )
    has_delivery = has_shipping_method(low)

    if has_delivery and not explicit_pickup_only:
        return False

    return explicit_pickup_only or has_pickup


def value_contains(value, needle):
    if value is None:
        return False

    if isinstance(value, str):
        return needle in value

    if isinstance(value, dict):
        return any(value_contains(item, needle) for item in value.values())

    if isinstance(value, list):
        return any(value_contains(item, needle) for item in value)

    return needle in str(value)


def get_json_ld_types(item):
    types = item.get("@type") if isinstance(item, dict) else None
    if isinstance(types, str):
        return {types.lower()}
    if isinstance(types, list):
        return {str(item_type).lower() for item_type in types}
    return set()


def is_lot_json_ld(item):
    if not isinstance(item, dict):
        return False

    item_types = get_json_ld_types(item)
    return bool({"product", "offer"} & item_types) or bool(item.get("offers") and item.get("name"))


def flatten_json_ld(data):
    if isinstance(data, list):
        items = []
        for value in data:
            items.extend(flatten_json_ld(value))
        return items

    if not isinstance(data, dict):
        return []

    items = [data]
    graph = data.get("@graph")
    if graph:
        items.extend(flatten_json_ld(graph))

    return items


def select_current_json_ld(json_ld_items, listing_id):
    flat_items = []
    for item in json_ld_items:
        flat_items.extend(flatten_json_ld(item))

    lot_items = [item for item in flat_items if is_lot_json_ld(item)]
    current_items = [item for item in lot_items if value_contains(item, listing_id)]

    if current_items:
        return current_items

    if lot_items:
        return lot_items[:1]

    return []


def extract_images(soup, json_ld_items=None):
    images = set()

    og_image = get_meta(soup, property_="og:image")
    if og_image:
        images.add(og_image)

    for item in json_ld_items or []:
        image_value = item.get("image") if isinstance(item, dict) else None
        if isinstance(image_value, str):
            images.add(image_value)
        elif isinstance(image_value, list):
            images.update(image for image in image_value if isinstance(image, str))

    return list(images)


def extract_json_ld(soup):
    items = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items.append(data)
        except Exception:
            continue

    return items


def extract_next_data(soup):
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return {}

    content = script.string or script.get_text()
    if not content:
        return {}

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {}


def get_page_props(next_data):
    return next_data.get("props", {}).get("pageProps", {}) if isinstance(next_data, dict) else {}


def extract_bid_state(next_data, listing_id):
    page_props = get_page_props(next_data)
    dehydrated_state = page_props.get("dehydratedState") or {}
    queries = dehydrated_state.get("queries") or []

    for query in queries:
        state_data = query.get("state", {}).get("data") if isinstance(query, dict) else None
        if not isinstance(state_data, dict):
            continue

        bid_state = state_data.get("bid")
        if not isinstance(bid_state, dict):
            continue

        bids = bid_state.get("bids") or []
        if bid_state.get("offer_id") == listing_id or any(bid.get("offer_id") == listing_id for bid in bids if isinstance(bid, dict)):
            return bid_state

    return {}


def extract_bid_history_from_state(bid_state):
    bids = bid_state.get("bids") if isinstance(bid_state, dict) else None
    if not isinstance(bids, list):
        return []

    bid_history = []
    for bid in bids:
        if not isinstance(bid, dict):
            continue

        bid_history.append(
            {
                "bidder": bid.get("buyer_nick"),
                "amount_chf": cents_to_chf(bid.get("price")),
                "bid_at": bid.get("date"),
                "winning": bid.get("is_winning_bid"),
                "auto_bid": bid.get("is_autobid"),
            }
        )

    return bid_history[:MAX_BID_HISTORY]


def extract_bid_history_from_text(lines):
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("Bisherige Gebote"))
    except StopIteration:
        return []

    end = next((i for i in range(start + 1, len(lines)) if lines[i] == "Mehr Informationen"), len(lines))
    section = lines[start + 1 : end]
    bids = []
    i = 0
    while i + 2 < len(section):
        bidder = section[i]
        amount = parse_price_line(section[i + 1])
        bid_at = section[i + 2]

        if bidder and "*" in bidder and amount is not None:
            bids.append(
                {
                    "bidder": bidder,
                    "amount_chf": amount,
                    "bid_at": bid_at,
                    "winning": None,
                    "auto_bid": None,
                }
            )
            i += 3
            continue

        i += 1

    return bids[:MAX_BID_HISTORY]


def extract_end_time_text(lines):
    keywords = ["endet", "enddatum", "endzeit", "auktion endet", "läuft ab"]

    for line in lines:
        low = line.lower()
        if any(keyword in low for keyword in keywords):
            return line

    return None


def extract_shipping_cost(text):
    if not text:
        return None
    if is_ricardo_listing_title(text):
        return None

    lines = [clean(line) for line in str(text).splitlines() if clean(line)]
    lines = lines or [clean(text)]

    for line in lines:
        low = line.lower()
        amounts = parse_money_values(line)
        if not amounts:
            continue
        if contains_keyword_word(low, ["abholung", "pickup"]) and not (
            has_shipping_method(low)
        ):
            continue
        if has_shipping_method(low):
            return next((amount for amount in amounts if amount > 0), amounts[0])

    amounts = parse_money_values(text)
    if not amounts:
        return None

    return next((amount for amount in amounts if amount > 0), amounts[0])


def build_risk_signals(data):
    description = data.get("description") or ""
    shipping = data.get("shipping") or ""
    searchable = " ".join(
        filter(
            None,
            [
                data.get("title"),
                description,
                shipping,
                data.get("bid_info"),
            ],
        )
    ).lower()

    risky_words = [
        "defekt",
        "bastler",
        "ohne garantie",
        "ersatzteil",
        "reparatur",
        "funktioniert nicht",
        "ungeprüft",
    ]

    return {
        "few_photos": len(data.get("images") or []) < 3,
        "short_description": len(description) < 120,
        "seller_rating_missing": data.get("seller_rating_percent") is None,
        "only_pickup": bool(data.get("pickup_only")) or is_pickup_only(shipping),
        "risky_words": [word for word in risky_words if word in searchable],
        "shipping_missing": data.get("shipping") is None and not data.get("pickup_only"),
        "bid_count_missing": data.get("bid_count") is None,
        "auction_end_missing": data.get("auction_end_text") is None,
    }


def build_openclaw_payload(data):
    current_price_chf = data.get("current_price_chf")
    shipping_cost_chf = data.get("shipping_cost_chf")
    images = data.get("images") or []

    return {
        "schema": "openclaw.ricardo.lot.v1",
        "source": {
            "provider": "ricardo",
            "url": data.get("source_url"),
            "listing_id": data.get("listing_id"),
            "parsed_at": data.get("parsed_at"),
        },
        "lot": {
            "title": data.get("title"),
            "category": data.get("category"),
            "category_path": data.get("category_path") or [],
            "condition": data.get("condition"),
            "offer_type": data.get("offer_type"),
            "sale_format": data.get("sale_format"),
            "auction_only": data.get("auction_only"),
            "has_buy_now": data.get("has_buy_now"),
            "description": data.get("description"),
            "image_count": len(images),
            "primary_image_url": images[0] if images else None,
            "location": data.get("location"),
            "current_price_chf": current_price_chf,
            "buy_now_price_chf": data.get("buy_now_price_chf"),
            "auction_current_price_chf": data.get("auction_current_price_chf"),
            "auction_start_price_chf": data.get("auction_start_price_chf"),
            "auction_next_minimum_bid_chf": data.get("auction_next_minimum_bid_chf"),
            "bid_increment_chf": data.get("bid_increment_chf"),
            "shipping_cost_chf": shipping_cost_chf,
            "shipping_text": data.get("shipping"),
            "pickup_only": data.get("pickup_only"),
            "bid_count": data.get("bid_count"),
            "bids": data.get("bids") or [],
            "auction_end_at": data.get("auction_end_at"),
        },
        "seller": {
            "name": data.get("seller"),
            "rating_percent": data.get("seller_rating_percent"),
            "sales_count": data.get("seller_sales_count"),
        },
    }


def parse_ricardo_page(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    listing_id = parse_listing_id(page_url)
    next_data = extract_next_data(soup)
    page_props = get_page_props(next_data)
    article = page_props.get("article") if isinstance(page_props.get("article"), dict) else {}
    article_offer = article.get("offer") if isinstance(article.get("offer"), dict) else {}
    article_seller = article.get("seller") if isinstance(article.get("seller"), dict) else {}

    meta_title = get_meta(soup, property_="og:title") or clean(soup.title.text if soup.title else None)
    meta_description = get_meta(soup, name="description") or get_meta(soup, property_="og:description")

    full_text = soup.get_text("\n", strip=True)
    json_ld_items = extract_json_ld(soup)
    current_json_ld_items = select_current_json_ld(json_ld_items, listing_id)
    product = current_json_ld_items[0] if current_json_ld_items else {}
    schema_offer = extract_offer(product)
    schema_offer_price, schema_offer_currency = extract_offer_price(schema_offer)
    offer_seller = extract_offer_seller(schema_offer)
    offer_end = article_offer.get("end_date") or extract_offer_end(schema_offer)
    category_path = extract_category_path(product)
    condition = article.get("conditionKey") or extract_condition(product, schema_offer)
    product_title = article.get("title") or product.get("name") if isinstance(product, dict) else article.get("title")
    product_description = product.get("description") if isinstance(product, dict) else None

    title = strip_ricardo_title(product_title or meta_title)
    description = clean(product_description) or meta_description
    current_json_ld_text = json.dumps(current_json_ld_items, ensure_ascii=False)
    current_lot_text = "\n".join(filter(None, [title, description, current_json_ld_text]))

    lines = [clean(x) for x in full_text.split("\n") if clean(x)]
    seller = extract_line_after(lines, ["verkäufer", "anbieter", "seller"])
    shipping = extract_section_text(lines, ["lieferung", "versand", "abholung"], following_lines=4, whole_word=True)
    bid_info = extract_section_text(lines, ["gebot", "bieter", "auktion", "sofort kaufen"])
    category = extract_line_after(lines, ["kategorie", "category"])
    pickup_only = is_pickup_only(" ".join(filter(None, [description, shipping])))
    shipping = "nur Abholung" if pickup_only else shipping
    shipping_cost_chf = None if pickup_only else extract_shipping_cost(shipping)
    bid_state = extract_bid_state(next_data, listing_id)
    bids = extract_bid_history_from_state(bid_state) or extract_bid_history_from_text(lines)
    offer_type = str(article_offer.get("offer_type") or "").lower()
    has_auction = "auction" in offer_type or article_offer.get("start_price") is not None or bool(bid_state)
    has_buy_now = "buynow" in offer_type or (not has_auction and article_offer.get("price") is not None)
    buy_now_price_chf = parse_number_value(article_offer.get("price")) if has_buy_now else None
    if has_buy_now and buy_now_price_chf is None and str(schema_offer_currency).upper() == "CHF":
        buy_now_price_chf = schema_offer_price
    listing_sale_format = sale_format(has_auction, has_buy_now)

    bid_count = bid_state.get("bids_count") or article_offer.get("bids_count") or len(bids)
    auction_start_price_chf = cents_to_chf(bid_state.get("start_price") or article_offer.get("start_price"))
    auction_current_price_chf = cents_to_chf(bid_state.get("last_bid") or article_offer.get("last_bid"))
    if auction_current_price_chf is not None and auction_current_price_chf <= 0:
        auction_current_price_chf = None
    if auction_current_price_chf is None and has_auction:
        auction_current_price_chf = auction_start_price_chf
    auction_next_minimum_bid_chf = cents_to_chf(
        bid_state.get("next_minimum_bid") or article_offer.get("next_minimum_bid")
    )
    bid_increment_chf = cents_to_chf(bid_state.get("increment") or article_offer.get("increment"))
    current_price_chf = auction_current_price_chf if has_auction else buy_now_price_chf
    current_price_chf = current_price_chf or parse_money_value(description) or parse_money_value(current_lot_text)

    data = {
        "source_url": page_url,
        "listing_id": listing_id,
        "parsed_at": datetime.now().isoformat(timespec="seconds"),

        "title": title,
        "price": f"CHF {current_price_chf:g}" if current_price_chf is not None else parse_price(description) or parse_price(current_lot_text),
        "current_price_chf": current_price_chf,
        "buy_now_price_chf": buy_now_price_chf,
        "offer_type": offer_type or None,
        "sale_format": listing_sale_format,
        "auction_only": listing_sale_format == "auction_only",
        "has_buy_now": has_buy_now,
        "auction_current_price_chf": auction_current_price_chf,
        "auction_start_price_chf": auction_start_price_chf,
        "auction_next_minimum_bid_chf": auction_next_minimum_bid_chf,
        "bid_increment_chf": bid_increment_chf,
        "seller": offer_seller or article_seller.get("nickname") or seller,
        "seller_rating_percent": round(article_seller.get("score") * 100, 1)
        if isinstance(article_seller.get("score"), (int, float))
        else parse_percent(full_text),
        "seller_sales_count": article_seller.get("ratingsCount") or parse_sales_count(full_text),
        "location": parse_location(meta_description) or parse_location(description) or parse_location(current_lot_text),
        "images": extract_images(soup, current_json_ld_items),
        "description": description,
        "category": category_path[0] if category_path else category,
        "category_path": category_path,
        "condition": condition,
        "shipping": shipping,
        "shipping_cost_chf": shipping_cost_chf,
        "pickup_only": pickup_only,
        "bid_info": bid_info,
        "bid_count": bid_count,
        "bids": bids,
        "auction_end_at": offer_end,
        "auction_end_text": offer_end or extract_end_time_text(lines),
    }

    data["json_ld"] = current_json_ld_items

    for item in current_json_ld_items:
        if isinstance(item, dict):
            data["title"] = data["title"] or item.get("name")
            data["description"] = data["description"] or item.get("description")
            data["category"] = data["category"] or item.get("category")

            offers = item.get("offers")
            if isinstance(offers, dict):
                price = offers.get("price")
                currency = offers.get("priceCurrency")
                if price and currency:
                    if data["has_buy_now"] and data["buy_now_price_chf"] is None and str(currency).upper() == "CHF":
                        data["buy_now_price_chf"] = parse_number_value(price)

    for i, line in enumerate(lines):
        low = line.lower()

        if data["seller"] is None and any(x in low for x in ["verkäufer", "anbieter", "seller"]):
            data["seller"] = lines[i + 1] if i + 1 < len(lines) else line

        if (
            data["shipping"] is None
            and not is_ricardo_listing_title(line)
            and contains_keyword_word(low, ["lieferung", "versand", "abholung"])
        ):
            data["shipping"] = line

        if data["bid_info"] is None and any(x in low for x in ["gebot", "bieter", "auktion", "sofort kaufen"]):
            data["bid_info"] = line

        if data["category"] is None and any(x in low for x in ["kategorie", "category"]):
            data["category"] = lines[i + 1] if i + 1 < len(lines) else line

    return data


def parse_iso_datetime(value):
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def listing_budget_price(item):
    return (
        item.get("current_price_chf")
        or item.get("buy_now_price_chf")
        or item.get("auction_current_price_chf")
        or item.get("auction_start_price_chf")
    )


def classify_listing_activity(item, html, *, from_live_search=False):
    text = BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)
    low = text.lower()
    reasons = []
    has_action = any(
        term in low
        for term in [
            "sofort kaufen",
            "bieten",
            "preis vorschlagen",
            "acheter maintenant",
            "faire une offre",
            "fai un'offerta",
        ]
    )
    inactive_terms = [
        "angebot beendet",
        "auktion beendet",
        "dieser artikel ist nicht mehr verfügbar",
        "artikel ist nicht mehr verfügbar",
        "article n'est plus disponible",
        "annonce terminée",
        "vente terminée",
        "non più disponibile",
        "vendita terminata",
    ]

    if any(term in low for term in inactive_terms):
        reasons.append("inactive_text")

    ended_at = parse_iso_datetime(item.get("auction_end_at"))
    if ended_at:
        now = datetime.now(ended_at.tzinfo) if ended_at.tzinfo else datetime.now()
        if ended_at < now:
            reasons.append("past_end_date")

    if re.search(r"\b0\s+offene(?:s|n)?\s+angebote\b", low) and not has_action:
        reasons.append("seller_has_zero_open_offers")

    return {
        "active": not reasons,
        "confidence": "action_button" if has_action else ("live_search" if from_live_search and not reasons else "low"),
        "has_action_signal": has_action,
        "reasons": reasons,
    }


def build_search_candidate(item, activity):
    description = clean(item.get("description")) or ""
    if len(description) > 260:
        description = f"{description[:257].rstrip()}..."
    images = item.get("images") or []

    risk_signals = build_risk_signals(item)
    risk_flags = [
        key for key, value in risk_signals.items() if value is True or (isinstance(value, list) and value)
    ]

    return {
        "listing_id": item.get("listing_id"),
        "title": item.get("title"),
        "url": item.get("source_url"),
        "price_chf": listing_budget_price(item),
        "buy_now_price_chf": item.get("buy_now_price_chf"),
        "auction_current_price_chf": item.get("auction_current_price_chf"),
        "auction_next_minimum_bid_chf": item.get("auction_next_minimum_bid_chf"),
        "sale_format": item.get("sale_format"),
        "condition": item.get("condition"),
        "location": item.get("location"),
        "shipping_cost_chf": item.get("shipping_cost_chf"),
        "shipping": item.get("shipping"),
        "pickup_only": item.get("pickup_only"),
        "seller": item.get("seller"),
        "seller_rating_percent": item.get("seller_rating_percent"),
        "seller_sales_count": item.get("seller_sales_count"),
        "bid_count": item.get("bid_count"),
        "auction_end_at": item.get("auction_end_at"),
        "active_check": activity,
        "risk_flags": risk_flags,
        "description_excerpt": description,
        "primary_image_url": images[0] if images else None,
        "candidate_source": "listing_page",
    }


def merge_search_candidate_with_lot(search_candidate, item, activity):
    detailed = build_search_candidate(item, activity)
    merged = dict(search_candidate)
    for key, value in detailed.items():
        if value is not None and value != []:
            merged[key] = value

    merged["matched_search_query"] = search_candidate.get("matched_search_query")
    merged["candidate_source"] = "search_page_card_enriched"
    return merged


def build_openclaw_search_payload(
    query,
    max_price_chf,
    search_url,
    candidates,
    *,
    min_price_chf=None,
    scanned_count,
    rejected=None,
    query_variants=None,
    search_urls=None,
    enriched_count=0,
    fetch_error=None,
):
    return {
        "schema": "openclaw.ricardo.search.v1",
        "source": {
            "provider": "ricardo",
            "url": search_url,
            "parsed_at": datetime.now().isoformat(timespec="seconds"),
        },
        "search": {
            "query": query,
            "min_price_chf": min_price_chf,
            "max_price_chf": max_price_chf,
            "max_budget_chf": max_price_chf,
            "search_url": search_url,
            "query_variants": query_variants or [query],
            "search_urls": search_urls or [search_url],
            "scanned_listing_count": scanned_count,
            "result_count": len(candidates),
            "enriched_listing_count": enriched_count,
            "fetch_error": fetch_error,
        },
        "candidates": candidates,
        "rejected": (rejected or [])[:10],
    }


def fetch_ricardo_search(
    query,
    max_price_chf=None,
    *,
    min_price_chf=None,
    proxy=None,
    output_dir=DEFAULT_OUTPUT_DIR,
    raw_dir=DEFAULT_RAW_DIR,
    headless=False,
    result_limit=DEFAULT_SEARCH_RESULT_LIMIT,
    scan_limit=DEFAULT_SEARCH_SCAN_LIMIT,
    enrich_limit=DEFAULT_SEARCH_ENRICH_LIMIT,
    save=True,
):
    query_variants = build_ricardo_search_queries(query)
    if not query_variants:
        raise RuntimeError("Could not build German Ricardo search terms for this request")

    search_urls = [
        build_ricardo_search_url(search_query, min_price_chf=min_price_chf, max_price_chf=max_price_chf)
        for search_query in query_variants
    ]
    search_url = search_urls[0]
    logging.info("Starting browser session")

    session_kwargs = {
        "headless": headless,
        "solve_cloudflare": True,
    }
    if proxy is None:
        proxy = get_proxy_from_env()
    if proxy:
        session_kwargs["proxy"] = proxy

    candidates = []
    rejected = []
    scanned_count = 0
    seen_listing_urls = set()
    fetched_search_pages = 0
    enriched_count = 0

    with StealthySession(**session_kwargs) as session:
        for search_query, current_search_url in zip(query_variants, search_urls):
            if len(candidates) >= result_limit or scanned_count >= scan_limit:
                break

            logging.info("Fetching Ricardo search page: %s", current_search_url)
            try:
                page = session.fetch(current_search_url, google_search=False)
                logging.info("Fetched search page")
                logging.info("Status: %s", page.status)
                logging.info("Final URL: %s", page.url)

                html = str(page.html_content)
                ensure_fetchable_page(page, html)
            except Exception as exc:
                logging.warning("Failed to fetch Ricardo search page %s: %s", current_search_url, exc)
                rejected.append({"url": current_search_url, "reason": str(exc)[:180], "search_query": search_query})
                continue

            fetched_search_pages += 1

            if save:
                raw_dir.mkdir(parents=True, exist_ok=True)
                search_slug = re.sub(r"\W+", "-", clean(search_query) or "search").strip("-") or "search"
                raw_path = raw_dir / f"ricardo_search_{search_slug[:80]}.html"
                raw_path.write_text(html, encoding="utf-8")

            search_page_candidates = extract_search_card_candidates(html, page.url, search_query)
            logging.info("Found %s search card candidates in search page", len(search_page_candidates))

            for candidate in search_page_candidates:
                if len(candidates) >= result_limit or scanned_count >= scan_limit:
                    break
                if candidate["url"] in seen_listing_urls:
                    continue

                seen_listing_urls.add(candidate["url"])
                scanned_count += 1
                price = candidate.get("price_chf")

                if price is None:
                    rejected.append({"url": candidate["url"], "reason": "missing_search_page_price"})
                    continue
                if max_price_chf is not None and float(price) > float(max_price_chf):
                    rejected.append({"url": candidate["url"], "reason": "over_budget", "price_chf": price})
                    continue
                if min_price_chf is not None and float(price) < float(min_price_chf):
                    rejected.append({"url": candidate["url"], "reason": "under_min_price", "price_chf": price})
                    continue

                candidates.append(candidate)

        for index, candidate in enumerate(list(candidates[: max(0, enrich_limit)])):
            try:
                lot_page = session.fetch(candidate["url"], google_search=False)
                lot_html = str(lot_page.html_content)
                ensure_fetchable_page(lot_page, lot_html)
                item = parse_ricardo_page(lot_html, lot_page.url)
                activity = classify_listing_activity(item, lot_html, from_live_search=True)
                price = listing_budget_price(item) or candidate.get("price_chf")

                if not activity["active"]:
                    rejected.append({"url": lot_page.url, "reason": ",".join(activity["reasons"])})
                    candidates[index] = {**candidate, "active_check": activity}
                    continue
                if price is not None and max_price_chf is not None and float(price) > float(max_price_chf):
                    rejected.append({"url": lot_page.url, "reason": "over_budget_after_enrichment", "price_chf": price})
                    continue
                if price is not None and min_price_chf is not None and float(price) < float(min_price_chf):
                    rejected.append({"url": lot_page.url, "reason": "under_min_price_after_enrichment", "price_chf": price})
                    continue

                candidates[index] = merge_search_candidate_with_lot(candidate, item, activity)
                enriched_count += 1
            except Exception as exc:
                logging.warning("Failed to enrich search candidate %s: %s", candidate["url"], exc)
                rejected.append({"url": candidate["url"], "reason": f"enrichment_failed: {str(exc)[:150]}"})

    filtered_candidates = []
    for candidate in candidates:
        activity = candidate.get("active_check") or {}
        price = candidate.get("price_chf")
        if activity.get("active") is False:
            continue
        if price is not None and max_price_chf is not None and float(price) > float(max_price_chf):
            continue
        if price is not None and min_price_chf is not None and float(price) < float(min_price_chf):
            continue
        filtered_candidates.append(candidate)
    candidates = filtered_candidates[:result_limit]

    payload = build_openclaw_search_payload(
        query,
        max_price_chf,
        search_url,
        candidates,
        min_price_chf=min_price_chf,
        scanned_count=scanned_count,
        rejected=rejected,
        query_variants=query_variants,
        search_urls=search_urls,
        enriched_count=enriched_count,
        fetch_error="all_search_pages_failed" if fetched_search_pages == 0 else None,
    )
    paths = {}
    if save:
        output_dir.mkdir(parents=True, exist_ok=True)
        search_slug = re.sub(r"\W+", "-", clean(query) or "search").strip("-") or "search"
        output_path = output_dir / f"openclaw_search_{search_slug[:80]}.json"
        write_json(output_path, payload)
        paths["openclaw_search_json"] = output_path

    return candidates, payload, paths


def parse_ricardo_search(
    query,
    max_price_chf=None,
    *,
    min_price_chf=None,
    proxy=None,
    output_dir=DEFAULT_OUTPUT_DIR,
    raw_dir=DEFAULT_RAW_DIR,
    headless=False,
    result_limit=DEFAULT_SEARCH_RESULT_LIMIT,
    scan_limit=DEFAULT_SEARCH_SCAN_LIMIT,
    enrich_limit=DEFAULT_SEARCH_ENRICH_LIMIT,
    save=True,
):
    _, payload, _ = fetch_ricardo_search(
        query,
        max_price_chf,
        min_price_chf=min_price_chf,
        proxy=proxy,
        output_dir=output_dir,
        raw_dir=raw_dir,
        headless=headless,
        result_limit=result_limit,
        scan_limit=scan_limit,
        enrich_limit=enrich_limit,
        save=save,
    )
    return payload


def ensure_fetchable_page(page, html):
    status = getattr(page, "status", None)
    if status is not None and int(status) >= 400:
        raise RuntimeError(f"Ricardo returned HTTP {status}")

    if not html or len(html) < 500:
        raise RuntimeError("HTML is empty or too short")

    title_match = re.search(r"<title[^>]*>\s*([^<]+?)\s*</title>", html, re.IGNORECASE)
    title = clean(title_match.group(1)) if title_match else None
    if title and title.lower() in {"forbidden", "access denied"}:
        raise RuntimeError(f"Ricardo returned {title}")


def write_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_csv_row(item):
    risk_signals = build_risk_signals(item)
    row = item.copy()
    row["risk_flags"] = " | ".join(
        key for key, value in risk_signals.items() if value is True or (isinstance(value, list) and value)
    )
    row["images"] = " | ".join(item.get("images", []))
    row["bids"] = " | ".join(
        f"{bid.get('bidder')}:{bid.get('amount_chf')}@{bid.get('bid_at')}" for bid in item.get("bids", [])
    )
    return row


def upsert_csv(path, item):
    csv_fields = [
        "source_url",
        "listing_id",
        "parsed_at",
        "title",
        "current_price_chf",
        "buy_now_price_chf",
        "offer_type",
        "sale_format",
        "auction_only",
        "has_buy_now",
        "auction_current_price_chf",
        "auction_start_price_chf",
        "auction_next_minimum_bid_chf",
        "bid_increment_chf",
        "price",
        "seller",
        "seller_rating_percent",
        "location",
        "description",
        "category",
        "condition",
        "shipping_cost_chf",
        "shipping",
        "pickup_only",
        "bid_count",
        "bids",
        "bid_info",
        "auction_end_at",
        "auction_end_text",
        "risk_flags",
        "images",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    csv_row = build_csv_row(item)
    new_row = {field: csv_row.get(field) for field in csv_fields}
    rows = [row for row in rows if row.get("listing_id") != item.get("listing_id")]
    rows.append(new_row)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(rows)


def save_outputs(html, item, output_dir=DEFAULT_OUTPUT_DIR, raw_dir=DEFAULT_RAW_DIR):
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    listing_id = item["listing_id"]
    raw_html_path = raw_dir / f"ricardo_{listing_id}.html"
    item_json_path = output_dir / f"ricardo_{listing_id}.json"
    openclaw_json_path = output_dir / f"openclaw_{listing_id}.json"
    csv_path = output_dir / "ricardo_items.csv"

    raw_html_path.write_text(html, encoding="utf-8")
    write_json(item_json_path, item)
    write_json(openclaw_json_path, build_openclaw_payload(item))
    upsert_csv(csv_path, item)

    return {
        "raw_html": raw_html_path,
        "item_json": item_json_path,
        "openclaw_json": openclaw_json_path,
        "csv": csv_path,
    }


def fetch_ricardo_lot(
    url,
    *,
    proxy=None,
    output_dir=DEFAULT_OUTPUT_DIR,
    raw_dir=DEFAULT_RAW_DIR,
    headless=False,
    save=True,
):
    logging.info("Starting browser session")

    session_kwargs = {
        "headless": headless,
        "solve_cloudflare": True,
    }
    if proxy is None:
        proxy = get_proxy_from_env()
    if proxy:
        session_kwargs["proxy"] = proxy

    with StealthySession(**session_kwargs) as session:
        logging.info("Fetching page: %s", url)

        page = session.fetch(url, google_search=False)

        logging.info("Fetched page")
        logging.info("Status: %s", page.status)
        logging.info("Final URL: %s", page.url)

        html = str(page.html_content)

        ensure_fetchable_page(page, html)

        item = parse_ricardo_page(html, page.url)
        paths = save_outputs(html, item, output_dir, raw_dir) if save else {}

        for name, path in paths.items():
            logging.info("Saved %s to %s", name, path)

        return item, build_openclaw_payload(item), paths


def parse_ricardo_url(
    url,
    *,
    proxy=None,
    output_dir=DEFAULT_OUTPUT_DIR,
    raw_dir=DEFAULT_RAW_DIR,
    headless=False,
    save=True,
):
    _, payload, _ = fetch_ricardo_lot(
        url,
        proxy=proxy,
        output_dir=output_dir,
        raw_dir=raw_dir,
        headless=headless,
        save=save,
    )
    return payload
