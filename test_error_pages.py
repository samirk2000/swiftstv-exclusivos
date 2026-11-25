import json
import os
import tempfile
import unittest
from unittest import mock

import scraper

CF_522 = """<!DOCTYPE html><html><head><title>tudeporteshoy.xyz | 522: Connection timed out</title></head>
<body><div id="cf-wrapper"><div id="cf-error-details"><h1>Connection timed out</h1>
<span>Error code 522</span><p>Cloudflare Ray ID: 8a1b2c3d4e5f</p></div></div></body></html>"""
CHALLENGE = "<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={}</script></body></html>"
REAL = "<html><head><title>Agenda deportiva</title></head><body>" + ("<li><a href='/e/1'>20:00 Liga MX: America vs Chivas</a></li>" * 50) + "<footer>protegido por cloudflare</footer></body></html>"


class ErrorPageTests(unittest.TestCase):
    def test_cloudflare_pages_detected(self):
        self.assertTrue(scraper.looks_like_error_page(CF_522))
        self.assertTrue(scraper.looks_like_error_page(CHALLENGE))
        self.assertTrue(scraper.looks_like_error_page("error code: 522", 522))

    def test_real_page_not_flagged(self):
        self.assertFalse(scraper.looks_like_error_page(REAL * 30))

    def test_error_event_text(self):
        self.assertTrue(scraper.is_error_text("[01:55 PM] Connection timed out Error code 522 (cloudflare.com)"))
        self.assertTrue(scraper.is_error_text("Bad gateway Error code 502"))
        self.assertFalse(scraper.is_error_text("[08:00 PM] Liga MX: América vs Chivas"))

    def test_get_html_raises_on_error_status(self):
        settings = scraper.Settings.from_env()
        client = scraper.HttpClient(settings)
        resp = mock.Mock(status_code=522, text="error code: 522", apparent_encoding="utf-8", encoding="utf-8")
        with mock.patch.object(client.session, "get", return_value=resp):
            with self.assertRaises(scraper.UpstreamError):
                client.get_html("https://example.test/agenda.html")
        resp200 = mock.Mock(status_code=200, text=CF_522, apparent_encoding="utf-8", encoding="utf-8")
        with mock.patch.object(client.session, "get", return_value=resp200):
            with self.assertRaises(scraper.UpstreamError):
                client.get_html("https://example.test/agenda.html")

    def test_read_json_list_drops_error_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([{"name": "[01:55 PM] Connection timed out Error code 522 (cloudflare.com)"}, {"name": "[08:00 PM] NBA: Lakers vs Celtics"}], fh)
            kept = scraper.read_json_list(path)
            self.assertEqual([r["name"] for r in kept], ["[08:00 PM] NBA: Lakers vs Celtics"])


if __name__ == "__main__":
    unittest.main()
