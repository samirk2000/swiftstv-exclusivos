"""Agenda clock conversion to America/Mexico_City."""

from __future__ import annotations

import unittest

from scraper import (
    convert_agenda_time,
    format_mexico_city_display,
    parse_hora_js_source_offset,
)


HORA_JS_SNIPPET = """
			var b = new Date(mi_array[i]);
			b.setMinutes(b.getMinutes()-(120-huso));
"""


class AgendaTimezoneTest(unittest.TestCase):
    def test_detects_hora_js_utc_plus_two(self) -> None:
        self.assertEqual(parse_hora_js_source_offset(HORA_JS_SNIPPET), 120)

    def test_converts_source_afternoon_to_mexico_morning(self) -> None:
        # 18:30 UTC+2 -> 10:30 America/Mexico_City (UTC-6)
        self.assertEqual(
            convert_agenda_time("18:30", "America/Mexico_City", 120),
            "10:30",
        )

    def test_converts_midnight_source_to_afternoon(self) -> None:
        self.assertEqual(
            convert_agenda_time("00:00", "America/Mexico_City", 120),
            "16:00",
        )

    def test_display_twelve_hour_brackets(self) -> None:
        self.assertEqual(format_mexico_city_display("10:30"), "[10:30 AM]")
        self.assertEqual(format_mexico_city_display("16:00"), "[04:00 PM]")
        self.assertEqual(format_mexico_city_display("00:00"), "[12:00 AM]")
        self.assertEqual(format_mexico_city_display("12:00"), "[12:00 PM]")


if __name__ == "__main__":
    unittest.main()
