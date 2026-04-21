"""
watchdog.py — Защита от зависаний браузера

Следит за браузером в отдельном потоке. Если браузер не отвечает
дольше max_stall_sec — убивает процесс чтобы избежать зависания
главного скрипта навсегда.

Использование:
    watchdog = BrowserWatchdog(browser.driver, max_stall_sec=120)
    watchdog.start()

    try:
        # ... долгая работа ...
    finally:
        watchdog.stop()
"""

import time
import threading
import logging
import os
import signal


class BrowserWatchdog:
    """
    Watchdog в отдельном потоке. Каждые check_interval секунд пингует
    драйвер. Если driver не отвечает дольше max_stall_sec — убивает
    процесс Chrome.
    """

    def __init__(
        self,
        driver,
        max_stall_sec:  int = 120,
        check_interval: int = 15,
        on_hang_callback=None,
    ):
        self.driver           = driver
        self.max_stall_sec    = max_stall_sec
        self.check_interval   = check_interval
        self.on_hang_callback = on_hang_callback

        self._stop            = threading.Event()
        self._thread          = None
        self._last_heartbeat  = time.time()

        # Получаем PID Chrome
        self._chrome_pids     = self._find_chrome_pids()

    def _find_chrome_pids(self) -> list[int]:
        """Находит PID'ы Chrome процессов запущенных этим драйвером"""
        pids = []
        try:
            # Из WebDriver сервиса
            if hasattr(self.driver, "service") and self.driver.service.process:
                pids.append(self.driver.service.process.pid)

            # Пытаемся найти PID самого браузера через CDP
            try:
                version_info = self.driver.execute_cdp_cmd("Browser.getVersion", {})
                # Browser.getVersion не даёт PID напрямую, но получаем процесс через отдельный API
                targets = self.driver.execute_cdp_cmd("Target.getTargets", {})
                # (PID детально не получить через CDP без SystemInfo — просто что есть)
            except Exception:
                pass
        except Exception as e:
            logging.debug(f"[Watchdog] find_pids: {e}")
        return pids

    def heartbeat(self):
        """Главный поток должен периодически вызывать это — знак что жив"""
        self._last_heartbeat = time.time()

    def start(self):
        """Запустить watchdog"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_heartbeat = time.time()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        logging.info(f"[Watchdog] 🐕 Запущен (max_stall={self.max_stall_sec}с)")

    def stop(self):
        """Остановить watchdog"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logging.info("[Watchdog] 🐕 Остановлен")

    def _watch_loop(self):
        while not self._stop.is_set():
            # Спим с проверкой на остановку
            for _ in range(self.check_interval):
                if self._stop.is_set():
                    return
                time.sleep(1)

            # Проверяем время с последнего heartbeat
            stalled_for = time.time() - self._last_heartbeat
            if stalled_for < self.max_stall_sec:
                # Всё ок — ещё попробуем активно попинговать драйвер
                if not self._ping_driver():
                    logging.warning(f"[Watchdog] ⚠ Драйвер не отвечает")
                continue

            # Превышен таймаут
            logging.error(
                f"[Watchdog] 🚨 Браузер завис на {stalled_for:.0f}с "
                f"(лимит {self.max_stall_sec}с) — убиваем процесс"
            )
            try:
                if self.on_hang_callback:
                    self.on_hang_callback()
            except Exception as e:
                logging.error(f"[Watchdog] callback error: {e}")

            self._kill_chrome()
            return  # после kill — watchdog завершается

    def _ping_driver(self, timeout: float = 10) -> bool:
        """
        Асинхронный пинг драйвера с таймаутом.
        Возвращает True если драйвер ответил, False если нет.
        """
        result = {"alive": False}

        def ping():
            try:
                _ = self.driver.title  # быстрая операция
                result["alive"] = True
            except Exception:
                pass

        t = threading.Thread(target=ping, daemon=True)
        t.start()
        t.join(timeout=timeout)
        return result["alive"]

    def _kill_chrome(self):
        """Принудительно убивает процесс Chrome"""
        for pid in self._chrome_pids:
            try:
                if os.name == "nt":
                    os.system(f"taskkill /F /PID {pid} >nul 2>&1")
                else:
                    os.kill(pid, signal.SIGKILL)
                logging.warning(f"[Watchdog] kill -9 {pid}")
            except Exception as e:
                logging.error(f"[Watchdog] kill {pid}: {e}")

        # Дополнительно — все Chrome процессы как fallback (мягкий способ)
        try:
            if os.name == "nt":
                # Убиваем только chromedriver — это безопаснее чем все chrome.exe
                os.system("taskkill /F /IM chromedriver.exe >nul 2>&1")
                os.system("taskkill /F /IM undetected_chromedriver.exe >nul 2>&1")
        except Exception:
            pass
