import unittest

from bot import extract_find_argument, is_find_command, parse_find_argument
from openclaw_client import build_find_payload
from ricardo_parser import (
    build_ricardo_search_queries,
    build_ricardo_search_url,
    classify_listing_activity,
    extract_search_listing_urls,
)


class FindCommandTest(unittest.TestCase):
    def test_extracts_russian_budget_phrase(self):
        self.assertEqual(
            parse_find_argument("видеокарту до 500 франков"),
            {"item_query": "видеокарту", "budget_chf": 500},
        )

    def test_keeps_model_number_in_item_query(self):
        self.assertEqual(
            parse_find_argument("RTX 4070 500 CHF"),
            {"item_query": "RTX 4070", "budget_chf": 500},
        )

    def test_parses_swiss_thousands_separator(self):
        self.assertEqual(
            parse_find_argument("macbook pro 1'200 CHF"),
            {"item_query": "macbook pro", "budget_chf": 1200},
        )

    def test_parses_swiss_price_suffix(self):
        self.assertEqual(
            parse_find_argument("stand mixer 500.-"),
            {"item_query": "stand mixer", "budget_chf": 500},
        )

    def test_consumes_full_currency_word(self):
        self.assertEqual(
            parse_find_argument("monitor 500 francs"),
            {"item_query": "monitor", "budget_chf": 500},
        )

    def test_parses_find_command_with_bot_username(self):
        self.assertTrue(is_find_command("/find@ricardo_resale_bot iphone 13 400 CHF"))
        self.assertEqual(extract_find_argument("/find@ricardo_resale_bot iphone 13 400 CHF"), "iphone 13 400 CHF")

    def test_rejects_empty_find_argument(self):
        self.assertIsNone(parse_find_argument(""))

    def test_find_payload_contains_direct_ricardo_search_url(self):
        self.assertEqual(
            build_find_payload("RTX 4070", 500)["search"]["search_url"],
            "https://www.ricardo.ch/de/s/RTX%204070/",
        )

    def test_parser_search_url_matches_find_payload_url(self):
        self.assertEqual(build_ricardo_search_url("RTX 4070"), "https://www.ricardo.ch/de/s/RTX%204070/")

    def test_builds_translated_search_queries_for_russian_item(self):
        self.assertEqual(
            build_ricardo_search_queries("видеокарту RTX 4070"),
            ["видеокарту RTX 4070", "grafikkarte RTX 4070", "gpu RTX 4070", "RTX 4070"],
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

    def test_french_sale_description_is_not_closed_status(self):
        activity = classify_listing_activity(
            {"auction_end_at": None},
            "Vendue pour changement de setup. Fonctionne parfaitement. Sofort kaufen",
            from_live_search=True,
        )
        self.assertTrue(activity["active"])


if __name__ == "__main__":
    unittest.main()
