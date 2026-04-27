"""Tests for the by_domain breakdown in fingerprint validator."""

from __future__ import annotations

import pytest


@pytest.fixture
def desktop_template():
    """Minimal valid desktop template for validator runs."""
    return {
        "id":                 "test_desktop",
        "label":              "Test Desktop",
        "category":           "desktop",
        "ua_platform_token":  "Windows NT 10.0; Win64; x64",
        "platform":           "Win32",
        "expected_gpu_vendor_marker": "NVIDIA",
        "min_chrome_version": 100,
        "max_chrome_version": 200,
        "screen_width_min":   1280,
        "screen_width_max":   3840,
        "screen_height_min":  720,
        "screen_height_max":  2160,
        "min_hardware_concurrency": 4,
        "max_hardware_concurrency": 32,
        "min_device_memory":  4,
        "max_device_memory":  32,
        "max_touch_points":   0,
        "preferred_languages": ["en-US"],
        "expected_audio_sample_rate": 44100,
        "expected_timezones": ["America/New_York"],
        "fonts_required":     ["Arial"],
        "fonts_forbidden":    ["Helvetica"],   # Mac-typical
    }


def test_validate_returns_by_domain(desktop_template):
    from ghost_shell.fingerprint.validator import validate
    fp = {
        "navigator": {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/149.0.0.0 Safari/537.36",
            "platform":  "Win32",
            "vendor":    "Google Inc.",
            "webdriver": False,
            "hardwareConcurrency": 8,
            "deviceMemory": 8,
            "maxTouchPoints": 0,
        },
        "screen": {"width": 1920, "height": 1080},
    }
    report = validate(fp, desktop_template)
    assert "by_domain" in report
    assert isinstance(report["by_domain"], dict)


def test_by_domain_categories_present(desktop_template):
    """Each canonical domain that has at least one applicable check
    should appear in by_domain output (the four sprint-1.3 domains)."""
    from ghost_shell.fingerprint.validator import validate
    fp = {
        "navigator": {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/149.0.0.0 Safari/537.36",
            "platform":  "Win32",
            "vendor":    "Google Inc.",
            "webdriver": False,
            "hardwareConcurrency": 8,
            "deviceMemory": 8,
            "maxTouchPoints": 0,
            "languages": ["en-US"],
        },
        "screen": {"width": 1920, "height": 1080},
        "timezone": {"intl": "America/New_York", "offset": 300},
    }
    report = validate(fp, desktop_template)
    domains = set(report["by_domain"].keys())
    # At minimum identity, hardware, automation should be present
    assert "identity" in domains
    assert "hardware" in domains
    assert "automation" in domains


def test_by_domain_score_range(desktop_template):
    from ghost_shell.fingerprint.validator import validate
    fp = {
        "navigator": {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/149.0.0.0 Safari/537.36",
            "platform":  "Win32",
            "vendor":    "Google Inc.",
            "webdriver": False,
        },
        "screen": {"width": 1920, "height": 1080},
    }
    report = validate(fp, desktop_template)
    for domain, stats in report["by_domain"].items():
        assert 0 <= stats["score"] <= 100
        assert stats["grade"] in (
            "excellent", "good", "warning", "critical", "unknown"
        )
        assert stats["pass"] >= 0
        assert stats["fail"] >= 0
        assert stats["warn"] >= 0
        assert stats["total"] == (
            stats["pass"] + stats["fail"] + stats["warn"] + stats["skip"]
        )


def test_webdriver_exposure_collapses_automation_score(desktop_template):
    """webdriver=true is the bot-killer — dragging automation score
    way below other domains."""
    from ghost_shell.fingerprint.validator import validate
    fp = {
        "navigator": {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/149.0.0.0 Safari/537.36",
            "platform":  "Win32",
            "vendor":    "Google Inc.",
            "webdriver": True,    # ← bot-killer
        },
        "screen": {"width": 1920, "height": 1080},
    }
    report = validate(fp, desktop_template)
    auto_score = report["by_domain"]["automation"]["score"]
    assert auto_score < 50, f"automation score should collapse, got {auto_score}"


def test_validate_legacy_consumers_still_get_score():
    """Existing callers reading just `report['score']` keep working —
    by_domain is additive."""
    from ghost_shell.fingerprint.validator import validate
    template = {"id": "x", "label": "X", "ua_platform_token": "Linux",
                "platform": "Linux", "expected_gpu_vendor_marker": "Mesa",
                "min_chrome_version": 100, "max_chrome_version": 200,
                "preferred_languages": ["en-US"],
                "expected_timezones": ["UTC"],
                "fonts_required": [], "fonts_forbidden": []}
    fp = {"navigator": {"userAgent": "Mozilla/5.0 (Linux) Chrome/149.0.0.0",
                        "platform": "Linux", "webdriver": False,
                        "vendor": "Google Inc."}}
    report = validate(fp, template)
    assert "score" in report
    assert "grade" in report
    assert "summary" in report
    assert "checks" in report
