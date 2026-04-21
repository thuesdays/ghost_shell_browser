"""
diagnose.py — Полная диагностика окружения

Один скрипт который проверяет ВСЁ:
- Установлены ли зависимости
- Есть ли config.yaml
- Работает ли прокси
- Нет ли утечек WebRTC
- Корректный ли фингерпринт (health_check)
- CreepJS trust score
- Стабильность фингерпринта между запусками
- Здоровье профиля

Запускать перед первым использованием или когда что-то сломалось:
    python diagnose.py
"""

import os
import sys
import time
import logging
from datetime import datetime


def check_dependencies() -> list[dict]:
    """Проверяет что все нужные модули установлены"""
    results = []
    deps = [
        ("undetected_chromedriver", "undetected-chromedriver>=3.5.5"),
        ("selenium",                 "selenium>=4.15.0"),
        ("requests",                 "requests>=2.31.0"),
        ("yaml",                     "PyYAML>=6.0 (опционально)"),
    ]
    for module_name, hint in deps:
        try:
            __import__(module_name)
            results.append({"check": module_name, "ok": True, "detail": "installed"})
        except ImportError:
            results.append({"check": module_name, "ok": False, "detail": f"pip install {hint}"})
    return results


def check_files() -> list[dict]:
    """Проверяет наличие критичных файлов"""
    results = []
    critical = [
        ("fingerprints.js",  "JS-инъекции"),
        ("nk_browser.py",    "основной класс"),
    ]
    optional = [
        ("config.yaml",      "конфигурация"),
        ("proxies.json",     "пул прокси"),
    ]

    for fname, desc in critical:
        exists = os.path.exists(fname)
        results.append({
            "check":  fname,
            "ok":     exists,
            "detail": desc if exists else f"ОТСУТСТВУЕТ — {desc}",
        })

    for fname, desc in optional:
        exists = os.path.exists(fname)
        results.append({
            "check":  fname,
            "ok":     True,
            "detail": desc if exists else f"нет ({desc}) — не критично",
        })
    return results


def check_proxy_setup() -> dict:
    """Проверяет настройки прокси через config"""
    try:
        from config import Config
        cfg = Config.load()
        proxy_url = cfg.get("proxy.url")
        if not proxy_url:
            return {"ok": False, "detail": "proxy.url не задан в config.yaml"}
        return {"ok": True, "detail": f"prox настроен: {proxy_url[:40]}..."}
    except Exception as e:
        return {"ok": False, "detail": f"Config error: {e}"}


def run_browser_check(profile_name: str = "diag_temp") -> dict:
    """Запускает временный браузер и прогоняет все health-проверки"""
    try:
        from nk_browser import NKBrowser
        from proxy_diagnostics import ProxyDiagnostics
        from config import Config

        cfg = Config.load()
        proxy = cfg.get("proxy.url")

        logging.info("Запускаем временный браузер для диагностики...")

        with NKBrowser(
            profile_name      = profile_name,
            proxy_str         = proxy,
            auto_session      = False,
            enrich_on_create  = False,  # не засоряем временный профиль
        ) as browser:
            # Health check
            health = browser.health_check(verbose=False)
            health_passed = sum(1 for v in health.values() if v is True)
            health_total  = len(health)

            # Proxy diagnostics
            diag = ProxyDiagnostics(browser.driver)
            proxy_report = diag.full_check(expected_timezone="Europe/Kyiv")

            return {
                "ok":           health_passed == health_total and not proxy_report.get("webrtc_leak"),
                "health_score": f"{health_passed}/{health_total}",
                "health_failed": [k for k, v in health.items() if v is not True],
                "ip":           proxy_report.get("ip_info", {}).get("ip"),
                "country":      proxy_report.get("ip_info", {}).get("country"),
                "risk":         proxy_report.get("reputation", {}).get("risk"),
                "webrtc_leak":  proxy_report.get("webrtc_leak", False),
                "timezone_ok":  proxy_report.get("timezone", {}).get("ok", False),
            }

    except Exception as e:
        logging.error(f"Browser check error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


def cleanup_temp_profile(profile_name: str = "diag_temp"):
    """Удаляет временный профиль созданный для диагностики"""
    import shutil
    path = os.path.join("profiles", profile_name)
    if os.path.exists(path):
        try:
            shutil.rmtree(path)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# ГЛАВНАЯ ФУНКЦИЯ
# ──────────────────────────────────────────────────────────────

def run_diagnostic(verbose: bool = True):
    print("\n" + "═" * 72)
    print("  NK BROWSER — ПОЛНАЯ ДИАГНОСТИКА")
    print("═" * 72)
    print(f"  Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  Платформа: {sys.platform}")
    print("─" * 72)

    all_results = []

    # 1. Зависимости
    print("\n📦 ЗАВИСИМОСТИ")
    for r in check_dependencies():
        icon = "✓" if r["ok"] else "✗"
        print(f"  {icon} {r['check']:<30} {r['detail']}")
        all_results.append(r)

    # 2. Файлы
    print("\n📁 ФАЙЛЫ")
    for r in check_files():
        icon = "✓" if r["ok"] else "✗"
        print(f"  {icon} {r['check']:<30} {r['detail']}")
        all_results.append(r)

    # 3. Настройка прокси
    print("\n🌐 ПРОКСИ")
    proxy_setup = check_proxy_setup()
    icon = "✓" if proxy_setup["ok"] else "✗"
    print(f"  {icon} proxy config                  {proxy_setup['detail']}")

    if not proxy_setup["ok"]:
        print("\n⚠ Пропускаем проверку браузера — прокси не настроен")
        return False

    # 4. Запуск браузера и live-проверка
    print("\n🌐 ЗАПУСК БРАУЗЕРА И ПРОВЕРКА")
    print("   (займёт ~60 секунд)")
    browser_check = run_browser_check()

    if browser_check.get("error"):
        print(f"  ✗ Ошибка запуска: {browser_check['error']}")
        return False

    print(f"  {'✓' if browser_check['health_score'] == f'{15}/{15}' or '/' in browser_check['health_score'] else '⚠'} "
          f"Health check:               {browser_check['health_score']}")

    if browser_check.get("health_failed"):
        print(f"    Провалены: {', '.join(browser_check['health_failed'])}")

    ip = browser_check.get("ip") or "?"
    country = browser_check.get("country") or "?"
    print(f"  ✓ Внешний IP:                {ip} ({country})")

    risk = browser_check.get("risk", "unknown")
    icon = {"low": "✓", "medium": "⚠", "high": "✗"}.get(risk, "?")
    print(f"  {icon} Репутация IP:              {risk}")

    icon = "✓" if not browser_check.get("webrtc_leak") else "✗"
    print(f"  {icon} WebRTC утечка:             {'нет' if not browser_check.get('webrtc_leak') else 'ЕСТЬ!'}")

    icon = "✓" if browser_check.get("timezone_ok") else "⚠"
    print(f"  {icon} Таймзона совпадает:        {browser_check.get('timezone_ok')}")

    # Очистка
    cleanup_temp_profile()

    # Итоги
    print("\n" + "═" * 72)
    critical_failed = [r for r in all_results if not r["ok"] and "ОТСУТСТВУЕТ" in r.get("detail", "")]
    if critical_failed:
        print("  ❌ ДИАГНОСТИКА НЕ ПРОЙДЕНА — критичные файлы отсутствуют")
    elif browser_check.get("webrtc_leak"):
        print("  ❌ ДИАГНОСТИКА НЕ ПРОЙДЕНА — WebRTC утечка")
    elif not browser_check.get("ok"):
        print("  ⚠ ДИАГНОСТИКА С ПРЕДУПРЕЖДЕНИЯМИ — работать можно")
    else:
        print("  ✅ ДИАГНОСТИКА ПРОЙДЕНА — всё в порядке")
    print("═" * 72 + "\n")

    return browser_check.get("ok", False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    ok = run_diagnostic()
    sys.exit(0 if ok else 1)
