"""
tab_manager.py — Управление несколькими вкладками

Реальный юзер почти всегда имеет несколько открытых вкладок:
Gmail, YouTube (пауза), пара рабочих, заметки. Когда мы работаем
только в одной вкладке — это уже отличает нас от среднего юзера.

Использование:
    tabs = TabManager(browser)
    tabs.open_background_tabs(count=3)     # открываем фоновые
    # ... делаем основную работу в активной вкладке ...
    tabs.switch_to_random_background()      # иногда переключаемся
    tabs.back_to_main()                      # возвращаемся
"""

import time
import random
import logging


class TabManager:
    """Управление фоновыми вкладками"""

    # Сайты которые типично открыты в фоне у живого юзера
    BACKGROUND_SITES = [
        "https://www.youtube.com/",
        "https://mail.google.com/",
        "https://www.google.com/",
        "https://uk.wikipedia.org/wiki/Головна_сторінка",
        "https://www.rozetka.com.ua/",
        "https://www.olx.ua/",
        "https://www.pravda.com.ua/",
        "https://www.ukr.net/",
    ]

    def __init__(self, browser):
        self.browser    = browser
        self.driver     = browser.driver
        self.main_tab   = None
        self.bg_tabs    = []

    # ──────────────────────────────────────────────────────────

    def _capture_main_tab(self):
        """Запоминаем текущую вкладку как основную"""
        if self.main_tab is None:
            try:
                self.main_tab = self.driver.current_window_handle
            except Exception:
                pass

    def open_background_tabs(self, count: int = None, min_dwell: float = 2, max_dwell: float = 5):
        """
        Открывает несколько фоновых вкладок с разными сайтами.
        В каждой даём странице загрузиться на min_dwell-max_dwell секунд.
        count: если None — случайно 2-5
        """
        self._capture_main_tab()

        if count is None:
            count = random.randint(2, 5)

        sites = random.sample(self.BACKGROUND_SITES, min(count, len(self.BACKGROUND_SITES)))
        logging.info(f"[TabManager] 📑 Открываем {count} фоновых вкладок")

        for url in sites:
            try:
                # Открываем через JS — новая вкладка в фоне
                self.driver.execute_script(f"window.open('{url}', '_blank');")
                time.sleep(random.uniform(0.3, 0.7))

                # Запоминаем handle
                handles = self.driver.window_handles
                new_handle = handles[-1]
                if new_handle != self.main_tab:
                    self.bg_tabs.append(new_handle)

                # Переключаемся на новую, даём загрузиться, возвращаемся
                self.driver.switch_to.window(new_handle)
                time.sleep(random.uniform(min_dwell, max_dwell))

                # Легкий скролл в фоне
                try:
                    self.driver.execute_script(
                        f"window.scrollBy(0, {random.randint(100, 400)});"
                    )
                except Exception:
                    pass

                # Возвращаемся в основную
                self.driver.switch_to.window(self.main_tab)
                time.sleep(random.uniform(0.3, 0.7))

            except Exception as e:
                logging.debug(f"[TabManager] open tab {url}: {e}")

        logging.info(f"[TabManager] ✓ Открыто фоновых: {len(self.bg_tabs)}")

    def switch_to_random_background(self, dwell_sec: float = None):
        """Переключается в случайную фоновую вкладку, проводит там время"""
        if not self.bg_tabs:
            return

        try:
            # Убираем закрытые
            open_handles = set(self.driver.window_handles)
            self.bg_tabs = [h for h in self.bg_tabs if h in open_handles]

            if not self.bg_tabs:
                return

            handle = random.choice(self.bg_tabs)
            self.driver.switch_to.window(handle)

            dwell = dwell_sec or random.uniform(3, 10)
            logging.debug(f"[TabManager] → фоновая вкладка на {dwell:.1f}с")

            # Небольшая активность
            try:
                self.driver.execute_script(
                    f"window.scrollBy(0, {random.randint(-200, 400)});"
                )
            except Exception:
                pass

            time.sleep(dwell)

            # Возвращаемся в основную
            self.back_to_main()

        except Exception as e:
            logging.debug(f"[TabManager] switch_to_bg: {e}")

    def back_to_main(self):
        """Возврат в основную вкладку"""
        if self.main_tab is None:
            return
        try:
            if self.main_tab in self.driver.window_handles:
                self.driver.switch_to.window(self.main_tab)
        except Exception as e:
            logging.debug(f"[TabManager] back_to_main: {e}")

    def close_all_background(self):
        """Закрывает все фоновые вкладки"""
        for handle in list(self.bg_tabs):
            try:
                if handle in self.driver.window_handles:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
            except Exception:
                pass
        self.bg_tabs = []
        self.back_to_main()

    # ──────────────────────────────────────────────────────────

    def maybe_switch_around(self, probability: float = 0.3):
        """С заданной вероятностью переключается в фон и возвращается"""
        if not self.bg_tabs:
            return
        if random.random() < probability:
            self.switch_to_random_background()
