"""
main.py — Мониторинг контекстной рекламы конкурентов по брендовым запросам

Стратегия:
1. Открываем браузер (с обязательной init-навигацией в start() для активации инъекций)
2. Идём на google.com СНАЧАЛА (тёплый сайт, проверяем сеть, решаем consent/captcha)
3. Для каждого запроса — открываем stealth_get с прямым URL поиска с
   параметрами &gl=ua&hl=uk — реклама появляется СРАЗУ
4. Если рекламы нет — refresh-loop: обновляем каждые 10-15с до появления
   (макс N попыток)
5. Собираем domain/title/clean_url/google_click_url
6. ВСЕ google_click_url (кроме нашего домена) — пишем в append-файл
7. В конце — JSON + CSV отчёт
"""

import os
import re
import time
import random
import logging
import json
import requests
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote, quote
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from ghost_shell_browser import GhostShellBrowser
from proxy_diagnostics import ProxyDiagnostics
from session_quality import SessionQualityMonitor
from tab_manager import TabManager
from config import Config
from stealth_improvements import QueryRateLimiter, interact_with_serp

# ──────────────────────────────────────────────────────────────
# КОНФИГУРАЦИЯ — загружаем из config.yaml (с fallback defaults)
# ──────────────────────────────────────────────────────────────

CFG = Config.load("config.yaml")

SEARCH_QUERIES     = CFG.get("search.queries")
MY_DOMAINS         = CFG.get("search.my_domains")
TWOCAPTCHA_API_KEY = CFG.get("captcha.twocaptcha_key", "")
PROXY              = CFG.get("proxy.url")
PROFILE_NAME       = CFG.get("browser.profile_name")
IS_ROTATING_PROXY  = CFG.get("proxy.is_rotating", True)
ROTATION_API_URL   = CFG.get("proxy.rotation_api_url")

# Параметры refresh-loop для поиска рекламы
REFRESH_MIN_SEC       = 10   # минимум между обновлениями
REFRESH_MAX_SEC       = 15   # максимум
REFRESH_MAX_ATTEMPTS  = 4    # максимум обновлений на один запрос

# Файл куда пишутся ВСЕ google_click_url кроме нашего домена
COMPETITOR_URLS_FILE  = "competitor_urls.txt"

import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium").setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────
# СОСТОЯНИЕ СТРАНИЦЫ
# ──────────────────────────────────────────────────────────────

def page_state(driver) -> str:
    try:
        url = driver.current_url
    except Exception:
        return "dead"
    if "sorry/index" in url or "/sorry/" in url:
        return "captcha"
    if "consent.google.com" in url:
        return "consent"
    if "/search" in url and "q=" in url:
        return "search_results"
    if url.startswith("https://www.google.") or url == "about:blank" or url.startswith("data:"):
        return "home"
    return "other"


def is_offline_page(driver) -> bool:
    """Проверка не показал ли Chrome 'Вы в режиме офлайн'"""
    try:
        title = (driver.title or "").lower()
        if any(m in title for m in ("офлайн", "offline", "недоступно")):
            return True
        body_text = driver.execute_script(
            "return (document.body && document.body.innerText || '').substring(0, 300).toLowerCase();"
        )
        markers = [
            "підключіться до інтернету", "connect to the internet",
            "в режимі офлайн", "you're offline", "you are offline",
            "нет соединения", "подключитесь к интернету",
        ]
        return any(m in body_text for m in markers)
    except Exception:
        return False


def bypass_consent(driver):
    if page_state(driver) != "consent":
        return
    logging.info("🍪 Принимаем куки...")
    try:
        btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//*[contains(text(),'Принять все') or contains(text(),'Accept all') or contains(text(),'Прийняти все')]"
            ))
        )
        btn.click()
        time.sleep(random.uniform(2, 4))
    except Exception:
        pass


def solve_captcha(driver) -> bool:
    if page_state(driver) != "captcha":
        return True
    if not TWOCAPTCHA_API_KEY or TWOCAPTCHA_API_KEY == "ВАШ_КЛЮЧ":
        logging.warning("2Captcha API ключ не задан")
        return False
    logging.info("⚠️ Капча — решаем через 2Captcha...")
    try:
        wait = WebDriverWait(driver, 10)
        el = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "div.g-recaptcha, div[data-sitekey]"
        )))
        sitekey = el.get_attribute("data-sitekey") or el.get_attribute("data-s")
        if not sitekey:
            return False
        create = requests.get(
            f"https://2captcha.com/in.php?key={TWOCAPTCHA_API_KEY}"
            f"&method=userrecaptcha&googlekey={sitekey}&pageurl={driver.current_url}&json=1"
        ).json()
        if create.get("status") != 1:
            return False
        task_id = create["request"]
        time.sleep(20)
        for _ in range(24):
            poll = requests.get(
                f"https://2captcha.com/res.php?key={TWOCAPTCHA_API_KEY}"
                f"&action=get&id={task_id}&json=1"
            ).json()
            if poll.get("status") == 1:
                token = poll["request"]
                driver.execute_script(
                    "document.getElementById('g-recaptcha-response').value = arguments[0];",
                    token
                )
                time.sleep(5)
                return True
            time.sleep(5)
    except Exception as e:
        logging.error(f"Капча: {e}")
    return False


# ──────────────────────────────────────────────────────────────
# ПРЯМОЙ URL ПОИСКА ДЛЯ РЕКЛАМЫ
# ──────────────────────────────────────────────────────────────

def build_search_url(query: str) -> str:
    """
    Строит прямой URL Google-поиска — МИНИМАЛИСТИЧНЫЙ,
    как будто юзер ввёл запрос в поле и нажал Enter.

    КРИТИЧНО: не добавляем pws=0, adtest=on и другие "служебные"
    параметры — они включают Google Ads Preview режим (страница с
    желтым предупреждением "призначена для випробовування"), при
    котором НАСТОЯЩАЯ реклама не показывается.

    gl/hl тоже лучше не добавлять — Google сам определяет локаль
    по IP прокси и Accept-Language. Лишние параметры выглядят
    подозрительно (настоящие юзеры так не делают).
    """
    return f"https://www.google.com/search?q={quote(query)}"


def is_ads_preview_page(driver) -> bool:
    """
    Детектирует страницу Google Ads Preview:
    "Ця сторінка призначена для випробовування рекламних оголошень Google Ads"

    Эта страница появляется когда URL содержит служебные параметры типа
    pws=0 или adtest=on. Настоящей рекламы на ней нет, поэтому парсер
    её не должен считать валидной.
    """
    try:
        body_text = driver.execute_script(
            "return (document.body && document.body.innerText || '').substring(0, 500).toLowerCase();"
        )
        markers = [
            "ця сторінка призначена для випробовування",
            "this page is for testing google ads",
            "эта страница предназначена для тестирования",
            "google ads preview",
        ]
        return any(m in body_text for m in markers)
    except Exception:
        return False


def do_manual_search(browser, query: str) -> bool:
    """
    Fallback: вводит запрос в поле поиска вручную (как юзер).
    Возвращает True если поиск выполнен успешно.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = browser.driver
    try:
        # Убеждаемся что мы на google.com
        if "google.com" not in driver.current_url or "/search" in driver.current_url:
            browser.stealth_get("https://www.google.com/")
            time.sleep(random.uniform(2, 4))
            bypass_consent(driver)

        search_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.NAME, "q"))
        )
        WebDriverWait(driver, 5).until(EC.visibility_of(search_box))

        browser.bezier_move_to(search_box)
        time.sleep(random.uniform(0.4, 0.9))

        # Фокус
        focused = driver.execute_script(
            "return document.activeElement === arguments[0];", search_box
        )
        if not focused:
            driver.execute_script("arguments[0].focus();", search_box)
            time.sleep(0.3)

        # Очистка поля
        search_box.send_keys(Keys.CONTROL + "a")
        search_box.send_keys(Keys.BACKSPACE)
        time.sleep(random.uniform(0.3, 0.7))

        # Печать
        browser.human_type(search_box, query)
        time.sleep(random.uniform(0.5, 1.2))

        # Проверка что текст ввёлся
        typed_value = driver.execute_script("return arguments[0].value;", search_box)
        if query not in typed_value:
            driver.execute_script(
                "arguments[0].value = arguments[1]; "
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                search_box, query
            )
            time.sleep(0.3)

        search_box.send_keys(Keys.RETURN)
        time.sleep(random.uniform(3, 5))
        return True
    except Exception as e:
        logging.error(f"  do_manual_search: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# ИЗВЛЕЧЕНИЕ РЕКЛАМНЫХ БЛОКОВ
# ──────────────────────────────────────────────────────────────

def extract_real_url(href: str) -> str:
    """Распарсить параметр adurl/url/q из Google-редиректа"""
    if not href:
        return ""
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        for key in ("adurl", "url", "q"):
            if key in qs:
                real = unquote(qs[key][0])
                if real.startswith("http"):
                    return real
    except Exception:
        pass
    return href


def extract_domain(url: str) -> str:
    """Извлекает домен без www."""
    if not url:
        return ""
    try:
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def parse_ads(driver, query: str) -> list[dict]:
    """
    Извлекает ТОЛЬКО рекламные блоки со страницы результатов.
    Возвращает для каждой: title, display_url, clean_url, google_click_url, domain
    """
    state = page_state(driver)
    if state != "search_results":
        logging.warning(f"  Не на странице результатов: state={state}")
        return []

    js_script = r"""
    const SPONSORED_MARKERS = [
        'Sponsored', 'Реклама', 'Спонсировано', 'Спонсоване',
        'Anuncio', 'Annonce', 'Werbung', 'Annuncio'
    ];

    const adBlocks = new Set();

    // Способ 1: поиск по метке "Sponsored" / "Реклама"
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let node;
    while (node = walker.nextNode()) {
        const text = (node.textContent || '').trim();
        if (text.length < 20 && SPONSORED_MARKERS.some(m => text === m || text.startsWith(m))) {
            let parent = node;
            for (let i = 0; i < 8 && parent; i++) {
                parent = parent.parentElement;
                if (!parent) break;
                const link = parent.querySelector('a[href]');
                if (link && link.href && link.href.startsWith('http')) {
                    adBlocks.add(parent);
                    break;
                }
            }
        }
    }

    // Способ 2: data-text-ad атрибут
    document.querySelectorAll('div[data-text-ad]').forEach(el => adBlocks.add(el));

    const results = [];
    adBlocks.forEach(block => {
        let title = '';
        const heading = block.querySelector('[role="heading"], h3');
        if (heading) title = heading.textContent.trim();

        let displayUrl = '';
        const cite = block.querySelector('cite, span.VuuXrf, span.x2VHCd, span[role="text"]');
        if (cite) displayUrl = cite.textContent.trim();

        let googleClickUrl = '';
        let cleanUrl       = '';

        const allLinks = block.querySelectorAll('a[href]');

        // Сначала ищем ссылки через Google-редирект
        for (const link of allLinks) {
            const href = link.href || '';
            if (href.includes('/aclk?') || href.includes('googleadservices.com')) {
                if (!googleClickUrl) googleClickUrl = href;
                for (const attr of ['data-rw', 'data-pcu', 'data-rh', 'data-agdh']) {
                    const val = link.getAttribute(attr);
                    if (val && val.startsWith('http') && !cleanUrl) {
                        cleanUrl = val;
                    }
                }
            }
        }

        if (!googleClickUrl) {
            for (const link of allLinks) {
                const href = link.href || '';
                if (href && href.startsWith('http')) {
                    googleClickUrl = href;
                    break;
                }
            }
        }

        if (googleClickUrl || displayUrl) {
            results.push({
                title:           title,
                displayUrl:      displayUrl,
                googleClickUrl:  googleClickUrl,
                cleanFromDataRw: cleanUrl,
            });
        }
    });

    return results;
    """

    try:
        raw_ads = driver.execute_script(js_script) or []
    except Exception as e:
        logging.warning(f"  Ошибка JS-парсинга: {e}")
        return []

    ads = []
    seen_domains = set()

    for raw in raw_ads:
        try:
            google_click_url = raw.get("googleClickUrl", "")
            clean_from_rw    = raw.get("cleanFromDataRw", "")
            display_url      = raw.get("displayUrl", "")
            title            = raw.get("title", "")

            # Чистый URL в порядке приоритета
            clean_url = ""
            if clean_from_rw and clean_from_rw.startswith("http"):
                clean_url = clean_from_rw
            elif google_click_url:
                parsed = extract_real_url(google_click_url)
                if (parsed and parsed.startswith("http") and
                        "google" not in (parsed.split("/")[2] if "/" in parsed[8:] else "")):
                    clean_url = parsed

            if not clean_url and display_url:
                du = display_url.strip()
                if du.startswith("http"):
                    clean_url = du
                elif "." in du and " " not in du:
                    first = du.split("›")[0].split("·")[0].split(" ")[0].strip()
                    if first and "." in first:
                        clean_url = "https://" + first

            domain = extract_domain(clean_url) or extract_domain(display_url)

            if not domain:
                continue

            # Фильтр: Google-внутренние
            if any(g in domain for g in ("google.com", "google.ua", "googleusercontent.com")):
                continue

            # Фильтр: наши домены
            if any(my in domain for my in MY_DOMAINS):
                logging.info(f"  · [наш] {domain} — {title[:50]}")
                continue

            # Дедупликация по домену в рамках одного запроса
            if domain in seen_domains:
                continue
            seen_domains.add(domain)

            ads.append({
                "query":             query,
                "title":             title,
                "display_url":       display_url,
                "clean_url":         clean_url,
                "google_click_url":  google_click_url,
                "domain":            domain,
                "found_at":          datetime.now().isoformat(timespec="seconds"),
            })
            logging.info(f"  ✓ {domain} — {title[:60]}")

        except Exception as e:
            logging.debug(f"  Ошибка обработки блока: {e}")

    return ads


# ──────────────────────────────────────────────────────────────
# ПОИСК ПО ОДНОМУ ЗАПРОСУ С REFRESH-LOOP
# ──────────────────────────────────────────────────────────────

def search_with_refresh_loop(browser, query: str, sqm, current_ip: str | None = None):
    """
    Поиск с refresh-loop для появления рекламы.

    Стратегия:
    1. Пробуем прямой URL (быстро)
    2. Если Google показал Ads Preview страницу — делаем ручной ввод (надёжнее)
    3. Парсим рекламу
    4. Нет рекламы → refresh через 10-15с (макс N попыток)
    """
    driver = browser.driver
    url = build_search_url(query)
    logging.info(f"🌐 Прямой URL: {url}")

    # Первый переход
    try:
        browser.stealth_get(url, referer="https://www.google.com/")
    except Exception as e:
        logging.error(f"  stealth_get провален: {e}")
        return []

    time.sleep(random.uniform(3, 5))

    # Проверка Ads Preview — если появилось, переходим на ручной ввод
    if is_ads_preview_page(driver):
        logging.warning(
            "  ⚠ Google показал Ads Preview страницу — переходим на ручной ввод"
        )
        if not do_manual_search(browser, query):
            logging.error("  ручной ввод тоже провалился")
            return []
        time.sleep(random.uniform(2, 4))

    attempt = 0
    while True:
        attempt += 1

        # Офлайн
        if is_offline_page(driver):
            logging.error("  ✗ Офлайн-страница. Прокси сломался")
            return []

        bypass_consent(driver)

        # Капча
        if page_state(driver) == "captcha":
            sqm.record("captcha", query=query, details=f"refresh_attempt_{attempt}")
            if current_ip:
                browser.report_rotating(current_ip, success=False, captcha=True)
            if not solve_captcha(driver):
                sqm.record("blocked", query=query)
                logging.warning(f"  Капча не решена")
                return []
            sqm.record("captcha_solved", query=query)

        # Снова проверка Ads Preview — на случай refresh
        if is_ads_preview_page(driver):
            logging.warning(f"  ⚠ Ads Preview на попытке {attempt} — ручной ввод")
            if not do_manual_search(browser, query):
                return []
            time.sleep(random.uniform(2, 4))
            continue

        # Ждём загрузки контейнеров
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    "#search, #rso, [data-text-ad], #center_col"
                ))
            )
        except Exception:
            pass
        time.sleep(random.uniform(1.5, 3))

        # Скролл — реклама часто подгружается при взаимодействии
        try:
            browser.human_scroll(1, 2)
        except Exception:
            pass
        time.sleep(random.uniform(0.5, 1.5))

        # Парсим рекламу
        ads = parse_ads(driver, query)

        if ads:
            logging.info(f"  ✓ Реклама найдена на попытке {attempt}: {len(ads)} блоков")
            return ads

        # Рекламы нет
        if attempt >= REFRESH_MAX_ATTEMPTS:
            logging.info(f"  ✗ За {attempt} попыток реклама не появилась")
            return []

        wait_sec = random.uniform(REFRESH_MIN_SEC, REFRESH_MAX_SEC)
        logging.info(
            f"  🔄 Рекламы нет, попытка {attempt}/{REFRESH_MAX_ATTEMPTS} — "
            f"обновляем через {wait_sec:.0f}с"
        )
        time.sleep(wait_sec)

        try:
            driver.refresh()
        except Exception as e:
            logging.warning(f"  refresh error: {e}")
            if not browser.is_alive():
                return []
            continue

        time.sleep(random.uniform(2, 4))


# ──────────────────────────────────────────────────────────────
# ЗАПИСЬ URL В ФАЙЛ (APPEND)
# ──────────────────────────────────────────────────────────────

def append_competitor_urls(ads: list[dict], filepath: str = COMPETITOR_URLS_FILE):
    """
    Дописывает все google_click_url в файл (append).
    Фильтр наших доменов уже сделан в parse_ads — сюда приходят только чужие.
    Формат строки: <timestamp>\t<query>\t<domain>\t<google_click_url>
    """
    if not ads:
        return

    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

    with open(filepath, "a", encoding="utf-8") as f:
        for ad in ads:
            google_url = ad.get("google_click_url", "")
            if not google_url:
                continue
            line = "\t".join([
                ad["found_at"],
                ad["query"],
                ad["domain"],
                google_url,
            ])
            f.write(line + "\n")

    logging.info(f"  📝 Записано в {filepath}: {len(ads)} URL")


# ──────────────────────────────────────────────────────────────
# ОСНОВНАЯ ЛОГИКА
# ──────────────────────────────────────────────────────────────

def run_monitor():
    competitors: dict[str, dict] = {}

    with GhostShellBrowser(
        profile_name      = PROFILE_NAME,
        proxy_str         = PROXY,
        device_template   = CFG.get("browser.device_template"),
        auto_session      = CFG.get("browser.auto_session", True),
        is_rotating_proxy = IS_ROTATING_PROXY,
        rotation_api_url  = ROTATION_API_URL,
        enrich_on_create  = CFG.get("browser.enrich_on_create", True),
    ) as browser:

        driver = browser.driver
        browser.setup_profile_logging()

        # ── Мониторинг профиля ───────────────────────
        sqm = SessionQualityMonitor(browser.user_data_path)
        should_abort, reason = sqm.should_abort()
        if should_abort:
            logging.error(f"⛔ Профиль деградировал: {reason}")
            logging.error(f"   Удали nk_session/ и fingerprint.json в {browser.user_data_path}")
            return

        # ── 1. Проверки ──────────────────────────────
        browser.health_check(verbose=True)

        # Диагностика прокси — ВАЖНО делать ПОСЛЕ init nav в start()
        # (чтобы браузер уже был инициализирован)
        diag = ProxyDiagnostics(driver, proxy_url=PROXY)
        report = diag.full_check(expected_timezone="Europe/Kyiv")
        diag.print_report(report)

        if report["webrtc_leak"]:
            logging.error("✗ WebRTC УТЕЧКА — останавливаемся")
            return

        # ── 2. Блокировка трекеров ───────────────────
        browser.enable_request_blocking()

        # ── 3. Идём на google.com СНАЧАЛА ─────────
        # Это тёплый сайт (cookies уже есть), проверяем что сеть работает,
        # решаем consent если нужно
        logging.info("🏠 Идём на google.com для инициализации сессии...")
        browser.stealth_get("https://www.google.com/")
        time.sleep(random.uniform(3, 5))

        if is_offline_page(driver):
            logging.error("✗ Google показал офлайн — прокси проблема, выходим")
            return

        bypass_consent(driver)
        if page_state(driver) == "captcha":
            if not solve_captcha(driver):
                logging.warning("Капча на входе не решена")

        # ── 4. Прогрев ТОЛЬКО для нового профиля ─────
        if not os.path.exists(browser.session_dir):
            logging.info("📥 Новый профиль — гибридный прогрев")
            browser.warmup_profile(depth="hybrid")
        else:
            logging.info("✓ Сессия восстановлена — быстрый прогрев через cookies")
            browser.warmup_profile(depth="fast")

        # ── 5. Фоновые вкладки (как у живого юзера) ──
        tabs = TabManager(browser)
        if CFG.get("behavior.open_background_tabs", True):
            bg_range = CFG.get("behavior.bg_tabs_count", [2, 4])
            tabs.open_background_tabs(count=random.randint(bg_range[0], bg_range[1]))

        # ── 6. Фиксируем IP для rotating-прокси ──────
        current_ip = None
        if IS_ROTATING_PROXY:
            current_ip = browser.check_and_rotate_if_burned()
            if current_ip:
                logging.info(f"🌐 Работаем с IP: {current_ip}")

        # Rate limiter для избежания частых запросов
        rate_limiter = QueryRateLimiter()

        # ── 7. ЦИКЛ ПОИСКА ───────────────────────────
        for i, query in enumerate(SEARCH_QUERIES):
            if not browser.is_alive():
                logging.error("Окно закрыто — выходим")
                break

            # Проверка rate limit ПЕРЕД запросом
            rate_limiter.wait_if_needed()

            logging.info("")
            logging.info("=" * 60)
            logging.info(f"🔎 Запрос {i+1}/{len(SEARCH_QUERIES)}: {query}")
            logging.info("=" * 60)

            search_started = time.time()
            rate_limiter.record_query(query, ip=current_ip)

            # Поиск с refresh-loop
            ads = search_with_refresh_loop(browser, query, sqm, current_ip=current_ip)

            duration = time.time() - search_started

            # После парсинга — взаимодействие с выдачей (даже если рекламы нет)
            # Это сигнал "живой юзер прочитал страницу"
            try:
                interact_with_serp(browser, dwell_min=2, dwell_max=6)
            except Exception:
                pass

            # Метрика результата
            if ads:
                sqm.record("search_ok", query=query,
                           results_count=len(ads), duration_sec=duration)
                if IS_ROTATING_PROXY and current_ip:
                    browser.report_rotating(current_ip, success=True, captcha=False)
            else:
                sqm.record("search_empty", query=query, duration_sec=duration)

            # ЗАПИСЬ ССЫЛОК В ФАЙЛ
            append_competitor_urls(ads, COMPETITOR_URLS_FILE)

            # Собираем в сводку конкурентов
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
                        "clean_url":        ad["clean_url"],
                        "google_click_url": ad["google_click_url"],
                        "queries":          [query],
                        "first_seen":       ad["found_at"],
                    }

            # Между запросами
            tabs.maybe_switch_around(probability=0.3)
            if CFG.get("behavior.idle_pauses", True):
                browser.idle_pause(kind="random")
            time.sleep(random.uniform(8, 15))

        tabs.close_all_background()

        # ── 8. ИТОГИ ──────────────────────────────────
        print_report(competitors)
        save_report(competitors)

        if IS_ROTATING_PROXY:
            tracker = browser.get_rotating_tracker()
            if tracker:
                tracker.print_stats()

        # HTML dashboard
        try:
            from dashboard import (
                collect_profile_stats, collect_latest_competitors,
                collect_proxy_stats, generate_html
            )
            generate_html(
                profiles    = collect_profile_stats(),
                competitors = collect_latest_competitors(),
                proxy_stats = collect_proxy_stats(),
            )
        except Exception as e:
            logging.debug(f"Dashboard: {e}")


# ──────────────────────────────────────────────────────────────
# ОТЧЁТ
# ──────────────────────────────────────────────────────────────

def print_report(competitors: dict):
    logging.info("")
    logging.info("╔" + "═" * 68 + "╗")
    logging.info("║" + " ИТОГОВЫЙ ОТЧЁТ — КОНКУРЕНТЫ В КОНТЕКСТНОЙ РЕКЛАМЕ ".center(68) + "║")
    logging.info("╚" + "═" * 68 + "╝")

    if not competitors:
        logging.info("Конкурентов не обнаружено.")
        return

    logging.info(f"Найдено уникальных рекламодателей: {len(competitors)}")
    logging.info("")

    sorted_items = sorted(
        competitors.values(),
        key=lambda c: (-len(c["queries"]), c["domain"])
    )

    for i, c in enumerate(sorted_items, 1):
        logging.info(f"[{i}] {c['domain']}")
        if c["title"]:
            logging.info(f"    Заголовок:  {c['title']}")
        if c["display_url"]:
            logging.info(f"    Display:    {c['display_url']}")
        if c.get("clean_url"):
            logging.info(f"    Clean URL:  {c['clean_url']}")
        if c.get("google_click_url"):
            logging.info(f"    Google ref: {c['google_click_url'][:120]}...")
        logging.info(f"    Запросы:    {', '.join(c['queries'])}")
        logging.info("")


def save_report(competitors: dict):
    """Сохраняет отчёт в JSON и CSV"""
    if not competitors:
        return

    reports_dir = "reports"
    os.makedirs(reports_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = os.path.join(reports_dir, f"competitors_{timestamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(list(competitors.values()), f, indent=2, ensure_ascii=False)
    logging.info(f"📄 JSON отчёт: {json_path}")

    csv_path = os.path.join(reports_dir, f"competitors_{timestamp}.csv")
    with open(csv_path, "w", encoding="utf-8-sig") as f:
        f.write("Домен;Заголовок;Display URL;Clean URL;Google Click URL;Запросы;Впервые замечен\n")
        for c in competitors.values():
            row = [
                c["domain"],
                (c["title"] or "").replace(";", ","),
                (c["display_url"] or "").replace(";", ","),
                (c.get("clean_url") or "").replace(";", ","),
                (c.get("google_click_url") or "").replace(";", ","),
                "|".join(c["queries"]),
                c["first_seen"],
            ]
            f.write(";".join(row) + "\n")
    logging.info(f"📊 CSV отчёт: {csv_path}")


if __name__ == "__main__":
    run_monitor()
