"""
rotating_proxy.py — Поддержка прокси с ротацией IP (asocks, bright data, etc)

У таких прокси один endpoint но меняющийся исходящий IP. Мы отдельно
трекаем health каждого IP который нам выдал провайдер и:

- Если текущий IP попал под капчу — запрашиваем ротацию
- Не используем уже "сгоревшие" IP
- Собираем статистику: какой IP/ASN/страна работает лучше

Интеграция с asocks API:
  GET https://api.asocks.com/v2/proxy/refresh/<port_id> — форсит смену IP
  (точный URL зависит от твоего тарифа — есть в личном кабинете)
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta


class RotatingProxyTracker:
    """
    Треккер IP для rotating-прокси.

    Использование:
        tracker = RotatingProxyTracker(
            proxy_url        = "user:pass@host:port",
            rotation_api_url = "https://...asocks.com/.../refresh",  # опционально
        )

        # Перед сессией
        ip = tracker.get_current_ip(driver)
        if tracker.is_ip_burned(ip):
            tracker.force_rotate()
            time.sleep(5)
            ip = tracker.get_current_ip(driver)

        # После сессии
        tracker.report(ip, captcha=False, success=True)
    """

    STATE_FILE_DEFAULT = "rotating_proxy_state.json"

    # Пороги
    BURN_AFTER_CAPTCHAS = 3   # после 3 капч подряд IP считаем сгоревшим
    COOLDOWN_HOURS      = 12  # сгоревшие IP снова пробуем через 12 часов

    def __init__(
        self,
        proxy_url:         str,
        rotation_api_url:  str = None,
        rotation_api_key:  str = None,
        state_file:        str = None,
    ):
        self.proxy_url        = proxy_url
        self.rotation_api_url = rotation_api_url
        self.rotation_api_key = rotation_api_key
        self.state_file       = state_file or self.STATE_FILE_DEFAULT
        self.state            = self._load()

    # ──────────────────────────────────────────────────────────
    # IO
    # ──────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if not os.path.exists(self.state_file):
            return {"ips": {}, "last_rotation": None, "total_rotations": 0}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"ips": {}, "last_rotation": None, "total_rotations": 0}

    def _save(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.debug(f"[RotatingProxy] save: {e}")

    def _ip_state(self, ip: str) -> dict:
        if ip not in self.state["ips"]:
            self.state["ips"][ip] = {
                "first_seen":        datetime.now().isoformat(timespec="seconds"),
                "last_seen":         datetime.now().isoformat(timespec="seconds"),
                "total_uses":        0,
                "total_captchas":    0,
                "consecutive_capchas": 0,
                "burned_at":         None,
                "country":           None,
                "org":               None,
            }
        return self.state["ips"][ip]

    # ──────────────────────────────────────────────────────────
    # ОПРЕДЕЛЕНИЕ ТЕКУЩЕГО IP
    # ──────────────────────────────────────────────────────────

    def get_current_ip(self, driver=None) -> str | None:
        """Узнаёт текущий исходящий IP через прокси"""
        try:
            if driver:
                # Через браузер — тот же путь что реальные запросы пойдут
                driver.get("https://api.ipify.org?format=json")
                time.sleep(2)
                body = driver.execute_script("return document.body.innerText;")
                data = json.loads(body)
                return data.get("ip")
            else:
                # Через requests с прокси
                proxies = {"http": f"http://{self.proxy_url}", "https": f"http://{self.proxy_url}"}
                r = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=15)
                return r.json().get("ip")
        except Exception as e:
            logging.warning(f"[RotatingProxy] get_ip: {e}")
            return None

    def enrich_ip_info(self, ip: str, driver=None):
        """Получаем метаданные IP (страна, ASN) — один раз на IP"""
        state = self._ip_state(ip)
        if state.get("country"):
            return  # уже есть

        try:
            if driver:
                driver.get("https://ipapi.co/json/")
                time.sleep(2)
                body = driver.execute_script("return document.body.innerText;")
                data = json.loads(body)
            else:
                proxies = {"http": f"http://{self.proxy_url}", "https": f"http://{self.proxy_url}"}
                r = requests.get("https://ipapi.co/json/", proxies=proxies, timeout=15)
                data = r.json()

            state["country"] = data.get("country_name")
            state["city"]    = data.get("city")
            state["org"]     = data.get("org")
            state["asn"]     = data.get("asn")
            self._save()
        except Exception as e:
            logging.debug(f"[RotatingProxy] enrich: {e}")

    # ──────────────────────────────────────────────────────────
    # ПРОВЕРКА ЗДОРОВЬЯ
    # ──────────────────────────────────────────────────────────

    def is_ip_burned(self, ip: str) -> bool:
        """Сгоревший ли IP (в cooldown или с слишком многими капчами)"""
        state = self._ip_state(ip)

        if state.get("burned_at"):
            burned_time = datetime.fromisoformat(state["burned_at"])
            if datetime.now() - burned_time < timedelta(hours=self.COOLDOWN_HOURS):
                return True
            # Cooldown прошёл — снимаем метку, пусть попробует
            state["burned_at"]           = None
            state["consecutive_capchas"] = 0
            self._save()

        return False

    def is_ip_fresh(self, ip: str) -> bool:
        """IP который никогда не использовался"""
        return ip not in self.state["ips"]

    # ──────────────────────────────────────────────────────────
    # ОТЧЁТНОСТЬ
    # ──────────────────────────────────────────────────────────

    def report(self, ip: str, success: bool = True, captcha: bool = False):
        """Записать результат использования IP"""
        if not ip:
            return

        state = self._ip_state(ip)
        state["last_seen"]   = datetime.now().isoformat(timespec="seconds")
        state["total_uses"] += 1

        if captcha:
            state["total_captchas"]      += 1
            state["consecutive_capchas"] += 1
            if state["consecutive_capchas"] >= self.BURN_AFTER_CAPTCHAS:
                state["burned_at"] = datetime.now().isoformat(timespec="seconds")
                logging.warning(
                    f"[RotatingProxy] 🔥 IP {ip} помечен как burned "
                    f"({state['consecutive_capchas']} капч подряд)"
                )
        elif success:
            state["consecutive_capchas"] = 0

        self._save()

    # ──────────────────────────────────────────────────────────
    # ПРИНУДИТЕЛЬНАЯ РОТАЦИЯ
    # ──────────────────────────────────────────────────────────

    def force_rotate(self) -> bool:
        """
        Запросить у провайдера смену IP.
        Возвращает True если rotation API настроен и запрос прошёл.
        """
        if not self.rotation_api_url:
            logging.info(
                "[RotatingProxy] rotation_api_url не задан — ротация произойдёт "
                "самостоятельно по таймеру провайдера"
            )
            # Просто ждём — asocks сам меняет IP каждые N минут
            return False

        try:
            headers = {}
            if self.rotation_api_key:
                headers["Authorization"] = f"Bearer {self.rotation_api_key}"

            r = requests.get(self.rotation_api_url, headers=headers, timeout=10)
            if r.status_code == 200:
                self.state["last_rotation"]   = datetime.now().isoformat(timespec="seconds")
                self.state["total_rotations"] += 1
                self._save()
                logging.info("[RotatingProxy] 🔄 IP ротирован через API")
                return True
            else:
                logging.warning(f"[RotatingProxy] rotation API: HTTP {r.status_code}")
        except Exception as e:
            logging.error(f"[RotatingProxy] rotation failed: {e}")
        return False

    def wait_for_rotation(self, driver, old_ip: str, timeout: int = 60) -> str | None:
        """
        Ждёт пока провайдер сам поменяет IP (если API нет).
        Возвращает новый IP или None если не дождались.
        """
        logging.info(f"[RotatingProxy] Ждём смены IP (текущий {old_ip})...")
        started = time.time()

        while time.time() - started < timeout:
            time.sleep(5)
            current = self.get_current_ip(driver)
            if current and current != old_ip:
                logging.info(f"[RotatingProxy] ✓ IP сменился: {old_ip} → {current}")
                return current

        logging.warning(f"[RotatingProxy] IP не сменился за {timeout}с")
        return None

    # ──────────────────────────────────────────────────────────
    # ОТЧЁТЫ
    # ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Общая статистика по всем IP"""
        ips        = self.state["ips"]
        total_ips  = len(ips)
        burned     = sum(1 for s in ips.values() if s.get("burned_at"))
        total_uses = sum(s["total_uses"] for s in ips.values())
        total_capt = sum(s["total_captchas"] for s in ips.values())

        return {
            "total_unique_ips":  total_ips,
            "burned_count":      burned,
            "healthy_count":     total_ips - burned,
            "total_requests":    total_uses,
            "total_captchas":    total_capt,
            "overall_captcha_rate": (total_capt / total_uses) if total_uses else 0,
            "total_rotations":   self.state.get("total_rotations", 0),
        }

    def print_stats(self):
        stats = self.get_stats()
        print("\n" + "═" * 60)
        print(" ROTATING PROXY STATS")
        print("═" * 60)
        print(f" Уникальных IP видели:  {stats['total_unique_ips']}")
        print(f" Сгоревших (cooldown):  {stats['burned_count']}")
        print(f" Здоровых:              {stats['healthy_count']}")
        print(f" Всего запросов:        {stats['total_requests']}")
        print(f" Капч:                  {stats['total_captchas']}")
        print(f" Rate капчи:            {stats['overall_captcha_rate']:.1%}")
        print(f" Ротаций:               {stats['total_rotations']}")

        # Топ-5 лучших и худших IP
        ips_sorted = sorted(
            self.state["ips"].items(),
            key=lambda x: (x[1]["total_captchas"] / x[1]["total_uses"]) if x[1]["total_uses"] else 0
        )
        if ips_sorted:
            print("\n Лучшие IP (мало капчи):")
            for ip, s in ips_sorted[:3]:
                rate = (s["total_captchas"] / s["total_uses"]) if s["total_uses"] else 0
                print(f"   {ip:<15} {s.get('country','?'):<15} uses={s['total_uses']} rate={rate:.0%}")

            print("\n Худшие IP (много капчи):")
            for ip, s in ips_sorted[-3:]:
                rate = (s["total_captchas"] / s["total_uses"]) if s["total_uses"] else 0
                print(f"   {ip:<15} {s.get('country','?'):<15} uses={s['total_uses']} rate={rate:.0%}")
        print("═" * 60 + "\n")


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if len(sys.argv) < 2:
        print("Использование:")
        print("  python rotating_proxy.py stats    — показать статистику IP")
        print("  python rotating_proxy.py reset    — сбросить состояние")
        sys.exit(0)

    tracker = RotatingProxyTracker(proxy_url="")
    cmd = sys.argv[1]

    if cmd == "stats":
        tracker.print_stats()
    elif cmd == "reset":
        if os.path.exists(tracker.state_file):
            os.remove(tracker.state_file)
            print("✓ Состояние сброшено")
