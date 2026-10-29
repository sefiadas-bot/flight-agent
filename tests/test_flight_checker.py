"""
Tests for flight_checker.py.

All external services (Playwright, SMTP) are mocked.
No real emails are sent and no real API calls are made.
"""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Set dummy env vars BEFORE importing the module so validate_env() isn't
# needed at import time and module-level code doesn't crash.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:dummy-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "999999")
# Ensure optional vars are cleared so defaults are exercised
os.environ.pop("SEND_NO_DEALS_NOTIFICATION", None)
os.environ.pop("MAX_MONTHLY_REQUESTS", None)
os.environ.pop("DEDUP_TTL_DAYS", None)

import flight_checker as fc  # noqa: E402  (must come after env setup)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_state_file(tmp_path, monkeypatch):
    """Redirect _STATE_FILE to a per-test temp path so tests don't share state."""
    monkeypatch.setattr(fc, "_STATE_FILE", tmp_path / "state.json")
    yield


# ---------------------------------------------------------------------------
# parse_price
# ---------------------------------------------------------------------------

class TestParsePrice:
    def test_plain_dollar(self):
        assert fc.parse_price("$450") == 450

    def test_comma_separated(self):
        assert fc.parse_price("$1,200") == 1200

    def test_embedded_in_text(self):
        assert fc.parse_price("Flight for $350 round trip") == 350

    def test_no_price(self):
        assert fc.parse_price("No price here") is None

    def test_currency_without_dollar(self):
        assert fc.parse_price("EUR 300") is None


# ---------------------------------------------------------------------------
# parse_hour
# ---------------------------------------------------------------------------

class TestParseHour:
    def test_am_with_space(self):
        assert fc.parse_hour("10:30 AM") == 10

    def test_pm_converts(self):
        assert fc.parse_hour("2:15 PM") == 14

    def test_noon(self):
        assert fc.parse_hour("12:00 PM") == 12

    def test_midnight(self):
        assert fc.parse_hour("12:00 AM") == 0

    def test_am_no_space(self):
        assert fc.parse_hour("6:00AM") == 6

    def test_invalid_returns_none(self):
        assert fc.parse_hour("invalid") is None


# ---------------------------------------------------------------------------
# build_google_url
# ---------------------------------------------------------------------------

class TestBuildGoogleUrl:
    def test_contains_route(self):
        url = fc.build_google_url("TLV", "AMS", "2027-10-12", "2027-10-21")
        assert "TLV.AMS.2027-10-12" in url
        assert "AMS.TLV.2027-10-21" in url

    def test_https(self):
        url = fc.build_google_url("TLV", "AMS", "2027-10-12", "2027-10-21")
        assert url.startswith("https://www.google.com/travel/flights")

    def test_usd_currency(self):
        url = fc.build_google_url("TLV", "MUC", "2027-10-12", "2027-10-20")
        assert "curr=USD" in url


# ---------------------------------------------------------------------------
# expand_date_pairs
# ---------------------------------------------------------------------------

class TestExpandDatePairs:
    _pairs = [["2027-04-01", "2027-04-08"]]

    def test_none_returns_unchanged(self):
        assert fc.expand_date_pairs(self._pairs, "none") == self._pairs

    def test_empty_flexibility_returns_unchanged(self):
        assert fc.expand_date_pairs(self._pairs, "") == self._pairs

    def test_plus1_adds_next_day(self):
        result = fc.expand_date_pairs(self._pairs, "+1")
        assert result == [
            ["2027-04-01", "2027-04-08"],
            ["2027-04-02", "2027-04-09"],
        ]

    def test_minus1_adds_prev_day(self):
        result = fc.expand_date_pairs(self._pairs, "-1")
        assert result == [
            ["2027-03-31", "2027-04-07"],
            ["2027-04-01", "2027-04-08"],
        ]

    def test_plus_minus_1_three_variants(self):
        result = fc.expand_date_pairs(self._pairs, "+/-1")
        assert len(result) == 3
        assert result[0] == ["2027-03-31", "2027-04-07"]
        assert result[1] == ["2027-04-01", "2027-04-08"]
        assert result[2] == ["2027-04-02", "2027-04-09"]

    def test_plus_minus_2_five_variants(self):
        result = fc.expand_date_pairs(self._pairs, "+/-2")
        assert len(result) == 5

    def test_plus_minus_3_seven_variants(self):
        result = fc.expand_date_pairs(self._pairs, "+/-3")
        assert len(result) == 7

    def test_trip_length_preserved(self):
        result = fc.expand_date_pairs(self._pairs, "+/-3")
        for pair in result:
            from datetime import date
            out = date.fromisoformat(pair[0])
            ret = date.fromisoformat(pair[1])
            assert (ret - out).days == 7  # original trip was 7 nights

    def test_duplicates_removed(self):
        # Two overlapping pairs: shifting +/-1 creates some overlaps
        pairs = [["2027-04-01", "2027-04-08"], ["2027-04-02", "2027-04-09"]]
        result = fc.expand_date_pairs(pairs, "+/-1")
        # unique pairs only
        assert len(result) == len({tuple(p) for p in result})

    def test_unknown_flexibility_returns_unchanged(self):
        assert fc.expand_date_pairs(self._pairs, "bogus") == self._pairs


# ---------------------------------------------------------------------------
# deal_id  (stable deduplication key)
# ---------------------------------------------------------------------------

class TestDealId:
    def test_same_inputs_same_id(self):
        a = fc.deal_id("TLV", "AMS", "2027-10-12", "2027-10-21", "Ryanair", 350)
        b = fc.deal_id("TLV", "AMS", "2027-10-12", "2027-10-21", "Ryanair", 350)
        assert a == b

    def test_different_price_different_id(self):
        a = fc.deal_id("TLV", "AMS", "2027-10-12", "2027-10-21", "Ryanair", 350)
        b = fc.deal_id("TLV", "AMS", "2027-10-12", "2027-10-21", "Ryanair", 351)
        assert a != b

    def test_different_airline_different_id(self):
        a = fc.deal_id("TLV", "AMS", "2027-10-12", "2027-10-21", "Ryanair", 350)
        b = fc.deal_id("TLV", "AMS", "2027-10-12", "2027-10-21", "EasyJet", 350)
        assert a != b

    def test_airline_case_insensitive(self):
        a = fc.deal_id("TLV", "AMS", "2027-10-12", "2027-10-21", "RYANAIR", 350)
        b = fc.deal_id("TLV", "AMS", "2027-10-12", "2027-10-21", "ryanair", 350)
        assert a == b

    def test_returns_12_char_hex(self):
        did = fc.deal_id("TLV", "AMS", "2027-10-12", "2027-10-21", "Ryanair", 350)
        assert len(did) == 12
        int(did, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# Deduplication helpers (is_seen / mark_seen / prune_state)
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_empty_state_not_seen(self):
        assert not fc.is_seen({}, "abc123")

    def test_mark_then_seen(self):
        state = {}
        fc.mark_seen(state, "abc123", "test deal")
        assert fc.is_seen(state, "abc123")

    def test_other_id_not_seen(self):
        state = {}
        fc.mark_seen(state, "abc123", "test deal")
        assert not fc.is_seen(state, "xyz999")

    def test_prune_removes_old(self):
        state = {
            "seen_deals": {
                "new": {"ts": time.time(), "label": "new"},
                "old": {"ts": time.time() - 61 * 86400, "label": "old"},
            }
        }
        fc.prune_state(state, ttl_days=60)
        assert "new" in state["seen_deals"]
        assert "old" not in state["seen_deals"]

    def test_prune_keeps_recent(self):
        state = {
            "seen_deals": {
                "a": {"ts": time.time() - 1 * 86400, "label": "a"},
                "b": {"ts": time.time() - 59 * 86400, "label": "b"},
            }
        }
        fc.prune_state(state, ttl_days=60)
        assert "a" in state["seen_deals"]
        assert "b" in state["seen_deals"]

    def test_prune_empty_seen_deals(self):
        state = {}
        fc.prune_state(state, ttl_days=60)
        assert state["seen_deals"] == {}


# ---------------------------------------------------------------------------
# Quota guard
# ---------------------------------------------------------------------------

class TestQuotaGuard:
    def test_fresh_state_has_quota(self):
        assert fc.quota_ok({}, max_requests=2000)

    def test_count_below_limit(self):
        now = datetime.now(tz=timezone.utc)
        ym = f"{now.year}-{now.month:02d}"
        state = {"monthly_usage": {"month": ym, "count": 100}}
        assert fc.quota_ok(state, max_requests=2000)

    def test_count_at_limit_fails(self):
        now = datetime.now(tz=timezone.utc)
        ym = f"{now.year}-{now.month:02d}"
        state = {"monthly_usage": {"month": ym, "count": 2000}}
        assert not fc.quota_ok(state, max_requests=2000)

    def test_old_month_resets(self):
        # Previous month's exhausted quota should reset for current month
        state = {"monthly_usage": {"month": "2020-01", "count": 99999}}
        assert fc.quota_ok(state, max_requests=2000)
        now = datetime.now(tz=timezone.utc)
        ym = f"{now.year}-{now.month:02d}"
        assert state["monthly_usage"]["month"] == ym
        assert state["monthly_usage"]["count"] == 0

    def test_charge_increments(self):
        state = {}
        fc.quota_ok(state, max_requests=2000)  # initialise
        fc.charge_quota(state, 5)
        assert state["monthly_usage"]["count"] == 5

    def test_charge_multiple_times(self):
        state = {}
        fc.quota_ok(state, max_requests=2000)
        fc.charge_quota(state)
        fc.charge_quota(state)
        fc.charge_quota(state)
        assert state["monthly_usage"]["count"] == 3

    def test_charge_resets_on_new_month(self):
        state = {"monthly_usage": {"month": "2020-01", "count": 500}}
        total = fc.charge_quota(state, 1)
        assert total == 1  # reset + 1
        now = datetime.now(tz=timezone.utc)
        ym = f"{now.year}-{now.month:02d}"
        assert state["monthly_usage"]["month"] == ym


# ---------------------------------------------------------------------------
# State persistence (load_state / save_state)
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_load_missing_file_returns_empty(self):
        # _STATE_FILE is redirected to a non-existent path by autouse fixture
        state = fc.load_state()
        assert state == {}

    def test_save_then_load(self):
        original = {"seen_deals": {"abc": {"ts": 1000, "label": "test"}}}
        fc.save_state(original)
        loaded = fc.load_state()
        assert loaded == original

    def test_save_creates_parent_dirs(self, tmp_path, monkeypatch):
        deep = tmp_path / "a" / "b" / "state.json"
        monkeypatch.setattr(fc, "_STATE_FILE", deep)
        fc.save_state({"x": 1})
        assert deep.exists()

    def test_load_corrupt_file_returns_empty(self, tmp_path, monkeypatch):
        p = tmp_path / "corrupt.json"
        p.write_text("not valid json!!!", encoding="utf-8")
        monkeypatch.setattr(fc, "_STATE_FILE", p)
        assert fc.load_state() == {}


# ---------------------------------------------------------------------------
# extract_prices  (mocked Playwright page)
# ---------------------------------------------------------------------------

class TestExtractPrices:
    def _mock_page(self, card_texts):
        page = MagicMock()
        cards = []
        for t in card_texts:
            c = MagicMock()
            c.inner_text.return_value = t
            cards.append(c)
        page.locator.return_value.all.return_value = cards
        return page

    def test_basic_deal_extraction(self):
        page = self._mock_page([
            "Ryanair\n6:00 AM\n2:30 PM\n8hr 30min\nNonstop\n$350\n"
        ])
        deals = fc.extract_prices(page)
        assert len(deals) == 1
        assert deals[0]["price"] == 350
        assert deals[0]["stops"] == 0

    def test_filters_price_above_5000(self):
        page = self._mock_page(["Airline\n6:00 AM\n$6000\nNonstop"])
        deals = fc.extract_prices(page)
        assert all(d["price"] <= 5000 for d in deals)

    def test_filters_price_below_50(self):
        page = self._mock_page(["Airline\n6:00 AM\n$10\nNonstop"])
        deals = fc.extract_prices(page)
        assert deals == []

    def test_no_cards(self):
        page = self._mock_page([])
        assert fc.extract_prices(page) == []

    def test_stop_count_extracted(self):
        page = self._mock_page([
            "EasyJet\n8:00 AM\n4:00 PM\n8hr 0min\n1 stop\n$420\n"
        ])
        deals = fc.extract_prices(page)
        assert len(deals) == 1
        assert deals[0]["stops"] == 1

    def test_multiple_valid_cards(self):
        page = self._mock_page([
            "Ryanair\n6:00 AM\n$350\nNonstop",
            "EasyJet\n8:00 AM\n$420\nNonstop",
        ])
        deals = fc.extract_prices(page)
        assert len(deals) == 2


# ---------------------------------------------------------------------------
# find_deals_with_retry
# ---------------------------------------------------------------------------

class TestFindDealsWithRetry:
    def test_succeeds_first_attempt(self):
        expected = [{"price": 350, "airline": "Ryanair"}]
        with patch("flight_checker.find_deals", return_value=expected) as mock_fd, \
             patch("time.sleep"):
            result = fc.find_deals_with_retry(
                "TLV", "AMS", "2027-10-12", "2027-10-21", 650, 14,
                MagicMock(), max_retries=3,
            )
        assert result == expected
        assert mock_fd.call_count == 1

    def test_retries_then_succeeds(self):
        expected = [{"price": 350}]
        side_effects = [Exception("timeout"), Exception("timeout"), expected]
        with patch("flight_checker.find_deals", side_effect=side_effects) as mock_fd, \
             patch("time.sleep"):
            result = fc.find_deals_with_retry(
                "TLV", "AMS", "2027-10-12", "2027-10-21", 650, 14,
                MagicMock(), max_retries=3,
            )
        assert result == expected
        assert mock_fd.call_count == 3

    def test_raises_after_max_retries(self):
        with patch("flight_checker.find_deals", side_effect=Exception("persistent")), \
             patch("time.sleep"):
            with pytest.raises(Exception, match="persistent"):
                fc.find_deals_with_retry(
                    "TLV", "AMS", "2027-10-12", "2027-10-21", 650, 14,
                    MagicMock(), max_retries=2,
                )

    def test_sleep_doubles_on_retry(self):
        calls = []
        with patch("flight_checker.find_deals", side_effect=[Exception("x"), Exception("x"), []]), \
             patch("time.sleep", side_effect=lambda s: calls.append(s)):
            fc.find_deals_with_retry(
                "TLV", "AMS", "2027-10-12", "2027-10-21", 650, 14,
                MagicMock(), max_retries=3,
            )
        assert calls == [5, 10]  # 5s then 10s


# ---------------------------------------------------------------------------
# validate_env
# ---------------------------------------------------------------------------

class TestValidateEnv:
    def test_passes_with_all_vars(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        fc.validate_env()  # should not raise

    def test_exits_on_missing_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(SystemExit):
            fc.validate_env()


# ---------------------------------------------------------------------------
# build_notification_body
# ---------------------------------------------------------------------------

class TestBuildNotificationBody:
    def test_header_contains_date(self):
        body = fc.build_notification_body("27 Oct 2026", [], [])
        assert "27 Oct 2026" in body

    def test_no_deals_footer(self):
        body = fc.build_notification_body("27 Oct 2026", [], [])
        assert "No new deals" in body

    def test_deals_footer(self):
        body = fc.build_notification_body("27 Oct 2026", ["OK TLV → AMS"], ["section"])
        assert "Book now" in body

    def test_summary_included(self):
        body = fc.build_notification_body("27 Oct 2026", ["OK some route", "NO other"], [])
        assert "OK some route" in body
        assert "NO other" in body

    def test_deal_section_included(self):
        body = fc.build_notification_body("27 Oct 2026", [], ["deal detail here"])
        assert "deal detail here" in body


# ---------------------------------------------------------------------------
# send_telegram  (urllib mocked – no real HTTP request sent)
# ---------------------------------------------------------------------------

class TestSendTelegram:
    def _mock_urlopen(self, status=200):
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_posts_to_telegram_api(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fc.send_telegram("Title", "body")
        assert "api.telegram.org" in captured["url"]
        assert "123:abc" in captured["url"]

    def test_includes_title_in_body(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["data"] = req.data.decode("utf-8")
            return self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fc.send_telegram("My Title", "body text")
        assert "My Title" in captured["data"]

    def test_truncates_long_message(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["data"] = req.data.decode("utf-8")
            return self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fc.send_telegram("Title", "x" * 5000)
        assert len(captured["data"]) < 6000

    def test_no_deals_notification_skipped(self, monkeypatch):
        monkeypatch.setenv("SEND_NO_DEALS_NOTIFICATION", "false")
        with patch("flight_checker.send_telegram") as mock_send:
            fc._send_report("27 Oct 2026", [], [], any_new_deals=False)
        mock_send.assert_not_called()

    def test_no_deals_notification_sent_when_configured(self, monkeypatch):
        monkeypatch.setenv("SEND_NO_DEALS_NOTIFICATION", "true")
        with patch("flight_checker.send_telegram") as mock_send:
            fc._send_report("27 Oct 2026", [], [], any_new_deals=False)
        mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# No-results behaviour
# ---------------------------------------------------------------------------

class TestNoResultsBehaviour:
    def test_empty_deals_produces_no_section(self):
        body = fc.build_notification_body("27 Oct 2026", ["NO TLV \u2192 AMS"], [])
        assert "=" * 20 not in body  # no deal section separator

    def test_no_deals_does_not_raise(self):
        # Ensure empty deal list flows without exception
        fc.build_notification_body("27 Oct 2026", [], [])


# ---------------------------------------------------------------------------
# API error and rate-limit handling
# ---------------------------------------------------------------------------

class TestApiErrorHandling:
    def test_partial_failure_continues(self):
        """Errors for one route should not prevent processing other routes."""
        results = [Exception("rate limited"), [{"price": 350}]]
        with patch("flight_checker.find_deals", side_effect=results), \
             patch("time.sleep"):
            # First call raises, second returns a deal
            try:
                fc.find_deals_with_retry(
                    "TLV", "AMS", "2027-10-12", "2027-10-21", 650, 14,
                    MagicMock(), max_retries=1,
                )
            except Exception:
                pass
            result = fc.find_deals_with_retry(
                "TLV", "MUC", "2027-10-12", "2027-10-20", 650, 14,
                MagicMock(), max_retries=1,
            )
        assert result == [{"price": 350}]


# ---------------------------------------------------------------------------
# Monthly quota guard integration
# ---------------------------------------------------------------------------

class TestMonthlyQuotaIntegration:
    def test_quota_stops_before_limit(self):
        now = datetime.now(tz=timezone.utc)
        ym = f"{now.year}-{now.month:02d}"
        # Set count to one below limit
        state = {"monthly_usage": {"month": ym, "count": 1999}}
        assert fc.quota_ok(state, max_requests=2000)
        fc.charge_quota(state)
        # Now at limit
        assert not fc.quota_ok(state, max_requests=2000)
