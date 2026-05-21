import unittest

from bot import (
    extract_find_argument,
    extract_question_argument,
    is_find_command,
    is_question_command,
    parse_find_argument,
)
from openclaw_client import build_find_payload, build_question_agent_message, build_question_payload
from ricardo_parser import (
    build_ricardo_search_queries,
    build_ricardo_search_url,
    classify_listing_activity,
    extract_search_card_candidates,
    extract_search_listing_urls,
    fetch_ricardo_search,
)
import ricardo_parser


class FakeSearchPage:
    status = 404
    url = "https://www.ricardo.ch/de/s/unknown/"
    html_content = "<html><title>Not found</title></html>"


class Always404Session:
    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def fetch(self, url, google_search=False):
        page = FakeSearchPage()
        page.url = url
        return page


class FindCommandTest(unittest.TestCase):
    def test_extracts_russian_budget_phrase(self):
        self.assertEqual(
            parse_find_argument("видеокарту до 500 франков"),
            {"item_query": "видеокарту", "budget_chf": 500, "min_price_chf": None, "max_price_chf": 500},
        )

    def test_keeps_model_number_in_item_query(self):
        self.assertEqual(
            parse_find_argument("RTX 4070 500 CHF"),
            {"item_query": "RTX 4070", "budget_chf": 500, "min_price_chf": None, "max_price_chf": 500},
        )

    def test_allows_find_without_price(self):
        self.assertEqual(
            parse_find_argument("RTX 4070"),
            {"item_query": "RTX 4070", "budget_chf": None, "min_price_chf": None, "max_price_chf": None},
        )

    def test_parses_swiss_thousands_separator(self):
        self.assertEqual(
            parse_find_argument("macbook pro 1'200 CHF"),
            {"item_query": "macbook pro", "budget_chf": 1200, "min_price_chf": None, "max_price_chf": 1200},
        )

    def test_parses_budget_range_as_upper_budget(self):
        self.assertEqual(
            parse_find_argument("Видеокарта для игр 350-500 франков"),
            {"item_query": "Видеокарта для игр", "budget_chf": 500, "min_price_chf": 350, "max_price_chf": 500},
        )

    def test_parses_swiss_price_suffix(self):
        self.assertEqual(
            parse_find_argument("stand mixer 500.-"),
            {"item_query": "stand mixer", "budget_chf": 500, "min_price_chf": None, "max_price_chf": 500},
        )

    def test_consumes_full_currency_word(self):
        self.assertEqual(
            parse_find_argument("monitor 500 francs"),
            {"item_query": "monitor", "budget_chf": 500, "min_price_chf": None, "max_price_chf": 500},
        )

    def test_parses_find_command_with_bot_username(self):
        self.assertTrue(is_find_command("/find@ricardo_resale_bot iphone 13 400 CHF"))
        self.assertEqual(extract_find_argument("/find@ricardo_resale_bot iphone 13 400 CHF"), "iphone 13 400 CHF")

    def test_parses_question_command_with_bot_username(self):
        self.assertTrue(is_question_command("/question@ricardo_resale_bot Is it risky?"))
        self.assertEqual(extract_question_argument("/question@ricardo_resale_bot Is it risky?"), "Is it risky?")

    def test_question_payload_preserves_recent_lot_context(self):
        context = {
            "kind": "lot",
            "saved_at": "2026-05-21T10:00:00+00:00",
            "telegram_message": "https://www.ricardo.ch/de/a/test-1234567890/",
            "payload": {
                "schema": "openclaw.ricardo.lot.v1",
                "source": {
                    "url": "https://www.ricardo.ch/de/a/test-1234567890/",
                    "listing_id": "1234567890",
                },
                "lot": {"title": "Test lot"},
            },
        }

        payload = build_question_payload("Is it risky?", "/question Is it risky?", "en", context)

        self.assertEqual(payload["schema"], "openclaw.ricardo.question.v1")
        self.assertEqual(payload["source"]["context_kind"], "lot")
        self.assertEqual(payload["source"]["listing_id"], "1234567890")
        self.assertEqual(payload["recent_context"], context)

    def test_question_message_instructs_context_only_when_relevant(self):
        message = build_question_agent_message("What is a fair bid?", response_language="en")

        self.assertIn("use it only when the question refers", message)
        self.assertIn("openclaw.ricardo.question.v1", message)

    def test_rejects_empty_find_argument(self):
        self.assertIsNone(parse_find_argument(""))

    def test_find_payload_contains_direct_ricardo_search_url(self):
        self.assertEqual(
            build_find_payload("RTX 4070", 500)["search"]["search_url"],
            "https://www.ricardo.ch/de/s/RTX%204070/?range_filters.price.max=500",
        )

    def test_find_payload_contains_price_range_search_url(self):
        self.assertEqual(
            build_find_payload("grafikkarte", min_price_chf=300, max_price_chf=450)["search"]["search_url"],
            "https://www.ricardo.ch/de/s/grafikkarte/?range_filters.price.max=450&range_filters.price.min=300",
        )

    def test_parser_search_url_matches_find_payload_url(self):
        self.assertEqual(build_ricardo_search_url("RTX 4070"), "https://www.ricardo.ch/de/s/RTX%204070/")

    def test_parser_search_url_includes_price_range_filters(self):
        self.assertEqual(
            build_ricardo_search_url("grafikkarte", min_price_chf=300, max_price_chf=450),
            "https://www.ricardo.ch/de/s/grafikkarte/?range_filters.price.max=450&range_filters.price.min=300",
        )

    def test_builds_translated_search_queries_for_russian_item(self):
        self.assertEqual(
            build_ricardo_search_queries("видеокарту RTX 4070"),
            [
                "grafikkarte RTX 4070",
                "RTX 4070 grafikkarte",
                "grafikkarte",
                "gpu RTX 4070",
                "RTX 4070 gpu",
                "gpu",
            ],
        )

    def test_builds_german_only_search_queries_for_russian_gaming_item(self):
        self.assertEqual(
            build_ricardo_search_queries("Видеокарта для игр"),
            [
                "grafikkarte gaming",
                "gaming grafikkarte",
                "grafikkarte",
                "gpu gaming",
                "gaming gpu",
                "gpu",
            ],
        )

    def test_extracts_listing_urls_from_search_html(self):
        html = """
        <a href="/de/a/rtx-4070-gigabyte-1303611968/">RTX 4070</a>
        <a href="https://www.ricardo.ch/de/a/rtx-4070-gigabyte-1303611968/?foo=bar">duplicate</a>
        <a href="/de/s/rtx-4070/">search</a>
        <a href="https://example.com/de/a/fake-1303611968/">external</a>
        """
        self.assertEqual(
            extract_search_listing_urls(html, "https://www.ricardo.ch/de/s/RTX%204070/"),
            ["https://www.ricardo.ch/de/a/rtx-4070-gigabyte-1303611968/"],
        )

    def test_extracts_search_card_candidate_from_search_html(self):
        html = """
        <a href="/de/a/gigabyte-geforce-rtx-3060-vision-oc-12g-lhr-1319456602/">
          <img src="https://img.example/rtx3060.jpg" alt="Gigabyte GeForce RTX 3060 Vision OC 12G LHR">
          <span>Gigabyte GeForce RTX 3060 Vision OC 12G LHR</span>
          <span>| Nvidia</span>
          <span>269.00 (0 Gebote)</span>
          <span>399.00 Sofort kaufen</span>
          <span>Fr, 22 Mai, 17:01</span>
        </a>
        """
        self.assertEqual(
            extract_search_card_candidates(html, "https://www.ricardo.ch/de/s/grafikkarte%20gaming/"),
            [
                {
                    "listing_id": "1319456602",
                    "title": "Gigabyte GeForce RTX 3060 Vision OC 12G LHR",
                    "url": "https://www.ricardo.ch/de/a/gigabyte-geforce-rtx-3060-vision-oc-12g-lhr-1319456602/",
                    "price_chf": 269.0,
                    "buy_now_price_chf": 399.0,
                    "auction_current_price_chf": 269.0,
                    "auction_next_minimum_bid_chf": None,
                    "sale_format": "auction_with_buy_now",
                    "condition": None,
                    "location": None,
                    "shipping_cost_chf": None,
                    "shipping": None,
                    "pickup_only": None,
                    "seller": None,
                    "seller_rating_percent": None,
                    "seller_sales_count": None,
                    "bid_count": 0,
                    "auction_end_at": None,
                    "active_check": {
                        "active": True,
                        "confidence": "search_page",
                        "has_action_signal": True,
                        "reasons": [],
                    },
                    "risk_flags": [],
                    "description_excerpt": "Nvidia 269.00 (0 Gebote) 399.00 Sofort kaufen Fr, 22 Mai, 17:01",
                    "primary_image_url": "https://img.example/rtx3060.jpg",
                    "matched_search_query": None,
                    "candidate_source": "search_page_card",
                }
            ],
        )

    def test_marks_risky_search_card_candidate(self):
        html = """
        <a href="/de/a/gtx-1060-6gb-defekter-luefter-1319875656/">
          <img src="https://img.example/gtx1060.jpg" alt="GTX 1060 6GB - defekter Lüfter">
          <span>GTX 1060 6GB - defekter Lüfter</span>
          <span>2.00 (0 Gebote)</span>
        </a>
        """
        candidate = extract_search_card_candidates(html)[0]
        self.assertEqual(candidate["risk_flags"], ["defekt"])

    def test_search_returns_empty_payload_when_all_search_pages_404(self):
        original_session = ricardo_parser.StealthySession
        ricardo_parser.StealthySession = Always404Session
        try:
            _, payload, _ = fetch_ricardo_search("RTX 4070", save=False)
        finally:
            ricardo_parser.StealthySession = original_session

        self.assertEqual(payload["search"]["fetch_error"], "all_search_pages_failed")
        self.assertEqual(payload["search"]["result_count"], 0)
        self.assertEqual(payload["candidates"], [])
        self.assertTrue(payload["rejected"])

    def test_french_sale_description_is_not_closed_status(self):
        activity = classify_listing_activity(
            {"auction_end_at": None},
            "Vendue pour changement de setup. Fonctionne parfaitement. Sofort kaufen",
            from_live_search=True,
        )
        self.assertTrue(activity["active"])


if __name__ == "__main__":
    unittest.main()
