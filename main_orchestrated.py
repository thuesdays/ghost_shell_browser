"""
main_orchestrated.py — Главный скрипт с использованием пулов

Использует ProfilePool + ProxyPool для автоматического выбора
здоровой пары профиль+прокси. Применяет browsing patterns для
правдоподобного поведения.

Требует:
- proxies.json  — список прокси
- Существующие или пустые папки профилей в profiles/

Это заменяет обычный main.py для продакшн-использования с несколькими
профилями/прокси. Обычный main.py оставляем для отладки одного профиля.
"""

import os
import time
import random
import logging
from datetime import datetime

from nk_browser import NKBrowser
from proxy_diagnostics import ProxyDiagnostics
from session_quality import SessionQualityMonitor
from profile_pool import ProfilePool
from proxy_pool import ProxyPool
from browsing_patterns import BrowsingPatterns
from watchdog import BrowserWatchdog
from main import (
    parse_ads, bypass_consent, solve_captcha, page_state,
    print_report, save_report, SEARCH_QUERIES
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ──────────────────────────────────────────────────────────────
# ОДНА СЕССИЯ
# ──────────────────────────────────────────────────────────────

def run_single_session(profile_name: str, proxy: dict) -> dict:
    """
    Запускает одну сессию мониторинга на заданной паре профиль+прокси.
    Возвращает словарь с результатами для report() в pool.
    """
    result = {
        "profile":     profile_name,
        "proxy_id":    proxy["id"],
        "success":     False,
        "captcha":     False,
        "blocked":     False,
        "competitors": {},
        "error":       None,
    }

    try:
        with NKBrowser(
            profile_name    = profile_name,
            proxy_str       = proxy["url"],
            device_template = None,  # шаблон выбирается автоматически взвешенно
            auto_session    = True,
        ) as browser:

            driver = browser.driver
            browser.setup_profile_logging()

            # Watchdog на случай зависания
            watchdog = BrowserWatchdog(driver, max_stall_sec=180)
            watchdog.start()

            try:
                # Мониторинг профиля
                sqm = SessionQualityMonitor(browser.user_data_path)
                should_abort, reason = sqm.should_abort()
                if should_abort:
                    logging.error(f"⛔ {profile_name}: {reason}")
                    result["blocked"] = True
                    return result

                # Health check
                browser.health_check(verbose=False)

                # Прокси диагностика (короткая)
                diag = ProxyDiagnostics(driver)
                ip_info = diag.get_ip_info()
                if ip_info.get("ok"):
                    logging.info(f"  Proxy IP: {ip_info.get('ip')} ({ip_info.get('country')})")

                watchdog.heartbeat()

                # Request blocking
                browser.enable_request_blocking()

                # Прогрев (быстрый если сессия уже есть)
                if not os.path.exists(browser.session_dir):
                    browser.warmup_profile(depth="hybrid")
                else:
                    browser.warmup_profile(depth="fast")

                watchdog.heartbeat()

                # Правдоподобное поведение перед целевым действием
                patterns = BrowsingPatterns(browser)
                patterns.pre_target_warmup()

                watchdog.heartbeat()

                # ЦИКЛ ПОИСКА
                browser.stealth_get("https://www.google.com")
                time.sleep(random.uniform(3, 6))
                bypass_consent(driver)
                solve_captcha(driver)

                competitors = {}

                for i, query in enumerate(SEARCH_QUERIES):
                    watchdog.heartbeat()

                    if not browser.is_alive():
                        break

                    logging.info(f"🔎 {profile_name}/{query}")
                    search_started = time.time()

                    if i > 0:
                        try:
                            driver.get("https://www.google.com")
                            time.sleep(random.uniform(2, 4))
                            bypass_consent(driver)
                        except Exception:
                            if not browser.is_alive():
                                break
                            continue

                    bypass_consent(driver)
                    if page_state(driver) == "captcha":
                        sqm.record("captcha", query=query)
                        result["captcha"] = True
                        if not solve_captcha(driver):
                            sqm.record("blocked", query=query)
                            result["blocked"] = True
                            time.sleep(random.uniform(30, 60))
                            continue

                    # Поиск
                    try:
                        from selenium.webdriver.common.by import By
                        from selenium.webdriver.common.keys import Keys
                        from selenium.webdriver.support.ui import WebDriverWait
                        from selenium.webdriver.support import expected_conditions as EC

                        search_box = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.NAME, "q"))
                        )
                        browser.bezier_move_to(search_box)
                        time.sleep(random.uniform(0.5, 1.2))
                        search_box.send_keys(Keys.CONTROL + "a")
                        search_box.send_keys(Keys.BACKSPACE)
                        time.sleep(random.uniform(0.3, 0.7))
                        browser.human_type(search_box, query)
                        time.sleep(random.uniform(0.5, 1.2))
                        search_box.send_keys(Keys.RETURN)
                    except Exception as e:
                        logging.error(f"  Search failed: {e}")
                        continue

                    time.sleep(random.uniform(4, 7))

                    if page_state(driver) == "captcha":
                        sqm.record("captcha", query=query)
                        result["captcha"] = True
                        if not solve_captcha(driver):
                            result["blocked"] = True
                            continue

                    browser.smart_dwell(min_sec=2, max_sec=8)
                    ads = parse_ads(driver, query)
                    duration = time.time() - search_started

                    if ads:
                        sqm.record("search_ok", query=query, results_count=len(ads),
                                   duration_sec=duration)
                    else:
                        sqm.record("search_empty", query=query, duration_sec=duration)

                    # Собираем конкурентов
                    for ad in ads:
                        domain = ad["domain"]
                        if domain in competitors:
                            if query not in competitors[domain]["queries"]:
                                competitors[domain]["queries"].append(query)
                        else:
                            competitors[domain] = {
                                "domain":           domain,
                                "title":            ad["title"],
                                "display_url":      ad["display_url"],
                                "clean_url":        ad.get("clean_url", ""),
                                "google_click_url": ad.get("google_click_url", ""),
                                "queries":          [query],
                                "first_seen":       ad["found_at"],
                            }

                    # Idle между запросами
                    browser.idle_pause(kind="random")

                # Post-target cooldown для натуральности
                if browser.is_alive():
                    patterns.post_target_cooldown(min_sec=5, max_sec=20)

                result["competitors"] = competitors
                result["success"]     = True

            finally:
                watchdog.stop()

    except Exception as e:
        result["error"] = str(e)
        logging.error(f"Session error: {e}", exc_info=True)

    return result


# ──────────────────────────────────────────────────────────────
# ГЛАВНЫЙ ЦИКЛ
# ──────────────────────────────────────────────────────────────

def run_orchestrated():
    # Инициализация пулов
    if not os.path.exists("proxies.json"):
        logging.error("proxies.json не найден. Скопируй proxies.json.example в proxies.json")
        return

    proxy_pool   = ProxyPool("proxies.json")
    profile_pool = ProfilePool(
        profiles_dir = "profiles",
        proxy_pool   = proxy_pool,
        min_profiles = 3,
    )

    profile_pool.print_status()

    # Получаем пару
    profile_name, proxy = profile_pool.acquire_pair()
    if not profile_name or not proxy:
        logging.error("Нет доступных профилей/прокси. Пул пуст или всё сгорело.")
        return

    logging.info(f"━━━ Сессия: {profile_name} + {proxy['id']} ━━━")

    # Запускаем
    result = run_single_session(profile_name, proxy)

    # Отчитываемся в пул
    profile_pool.release_pair(
        profile_name = result["profile"],
        proxy_id     = result["proxy_id"],
        success      = result["success"] and not result["captcha"],
        captcha      = result["captcha"],
        blocked      = result["blocked"],
    )

    # Сохраняем результаты
    if result["competitors"]:
        print_report(result["competitors"])
        save_report(result["competitors"])

    # Статус пула после сессии
    logging.info("")
    profile_pool.print_status()


if __name__ == "__main__":
    run_orchestrated()
