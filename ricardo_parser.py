import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from scrapling.fetchers import StealthySession

from settings import get_proxy_from_env

DEFAULT_OUTPUT_DIR = Path("data/openclaw")
DEFAULT_RAW_DIR = Path("data/raw")
MAX_BID_HISTORY = 10
SHIPPING_METHOD_KEYWORDS = ["versand", "postversand", "paket", "porto", "kurier", "sperrgut"]


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
