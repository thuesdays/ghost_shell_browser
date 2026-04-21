"""
session_quality.py — Мониторинг качества сессии профиля

Отслеживает метрики которые показывают насколько профиль "сгорел":
- Частота капчи при запросах
- Успешность поисков (находит результаты или пусто)
- Время до результата
- Consecutive failures

На основе этих метрик профиль может быть помечен как "деградировавший"
и пересоздан. Это то что делает профили у Dolphin долгоживущими —
они следят за здоровьем и вовремя меняют отпечаток.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict


@dataclass
class SessionMetric:
    """Одна запись метрики"""
    timestamp:     str
    event:         str          # "search_ok" | "captcha" | "search_fail" | "blocked"
    query:         str = ""
    results_count: int = 0
    duration_sec:  float = 0.0
    details:       str = ""


class SessionQualityMonitor:
    """
    Использование:
        sqm = SessionQualityMonitor(browser.user_data_path)

        # В цикле поиска
        sqm.record("search_ok", query=query, results_count=len(results))
        sqm.record("captcha", query=query)

        # В начале запуска
        health = sqm.get_health()
        if health["status"] == "critical":
            logging.warning("Профиль деградировал — пора пересоздавать")
    """

    # Пороги для определения статуса
    CRITICAL_CAPTCHA_RATE = 0.5   # 50%+ капчи за последние 24ч → critical
    WARNING_CAPTCHA_RATE  = 0.2   # 20%+ капчи → warning
    CRITICAL_BLOCKED_IN_ROW = 3   # 3 блокировки подряд → critical

    def __init__(self, profile_path: str):
        self.profile_path = profile_path
        self.metrics_file = os.path.join(profile_path, "session_quality.json")
        self._metrics     = self._load()

    # ──────────────────────────────────────────────────────────
    # IO
    # ──────────────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        if not os.path.exists(self.metrics_file):
            return []
        try:
            with open(self.metrics_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save(self):
        try:
            # Храним только последние 1000 записей
            self._metrics = self._metrics[-1000:]
            with open(self.metrics_file, "w", encoding="utf-8") as f:
                json.dump(self._metrics, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.debug(f"[SessionQuality] save: {e}")

    # ──────────────────────────────────────────────────────────
    # ЗАПИСЬ
    # ──────────────────────────────────────────────────────────

    def record(self, event: str, **kwargs):
        """
        Регистрирует событие. Доступные event:
        - search_ok: успешный поиск (results_count)
        - search_empty: поиск без результатов
        - captcha: появилась капча
        - captcha_solved: капча решена
        - blocked: IP/профиль заблокирован Google
        - search_fail: ошибка поиска (details)
        """
        metric = SessionMetric(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            event=event,
            query=kwargs.get("query", ""),
            results_count=kwargs.get("results_count", 0),
            duration_sec=kwargs.get("duration_sec", 0.0),
            details=kwargs.get("details", ""),
        )
        self._metrics.append(asdict(metric))
        self._save()

    # ──────────────────────────────────────────────────────────
    # АНАЛИЗ ЗДОРОВЬЯ
    # ──────────────────────────────────────────────────────────

    def _metrics_within(self, hours: int) -> list[dict]:
        """Метрики за последние N часов"""
        threshold = datetime.now() - timedelta(hours=hours)
        result = []
        for m in self._metrics:
            try:
                ts = datetime.fromisoformat(m["timestamp"])
                if ts >= threshold:
                    result.append(m)
            except Exception:
                continue
        return result

    def get_health(self) -> dict:
        """
        Возвращает здоровье профиля:
        - status: "healthy" | "warning" | "critical"
        - captcha_rate_24h: доля запросов где была капча
        - consecutive_blocks: блокировок подряд
        - total_searches: всего поисков
        - recommendations: что делать
        """
        recent_24h = self._metrics_within(24)
        recent_1h  = self._metrics_within(1)

        # Базовые счётчики
        searches   = sum(1 for m in recent_24h if m["event"] in ("search_ok", "search_empty"))
        captchas   = sum(1 for m in recent_24h if m["event"] == "captcha")
        blocks     = sum(1 for m in recent_24h if m["event"] == "blocked")
        empty      = sum(1 for m in recent_24h if m["event"] == "search_empty")

        # Captcha rate
        total_requests = searches + captchas + blocks
        captcha_rate   = captchas / total_requests if total_requests > 0 else 0

        # Consecutive блокировки (с конца)
        consecutive_blocks = 0
        for m in reversed(self._metrics):
            if m["event"] == "blocked":
                consecutive_blocks += 1
            elif m["event"] in ("search_ok",):
                break

        # Captcha rate за последний час — более чувствительно
        recent_searches_1h = sum(1 for m in recent_1h if m["event"] in ("search_ok", "search_empty"))
        recent_captchas_1h = sum(1 for m in recent_1h if m["event"] == "captcha")
        recent_total_1h    = recent_searches_1h + recent_captchas_1h
        captcha_rate_1h    = recent_captchas_1h / recent_total_1h if recent_total_1h > 0 else 0

        # Определяем статус
        status = "healthy"
        recommendations = []

        if consecutive_blocks >= self.CRITICAL_BLOCKED_IN_ROW:
            status = "critical"
            recommendations.append(
                f"⛔ {consecutive_blocks} блокировок подряд — пересоздай профиль"
            )
        elif captcha_rate >= self.CRITICAL_CAPTCHA_RATE:
            status = "critical"
            recommendations.append(
                f"⛔ Капча в {captcha_rate:.0%} запросов за 24ч — профиль сгорел"
            )
        elif captcha_rate_1h >= self.CRITICAL_CAPTCHA_RATE and recent_total_1h >= 5:
            status = "critical"
            recommendations.append(
                f"⛔ Внезапный всплеск капчи: {captcha_rate_1h:.0%} за час"
            )
        elif captcha_rate >= self.WARNING_CAPTCHA_RATE:
            status = "warning"
            recommendations.append(
                f"⚠ Повышенная капча {captcha_rate:.0%} — сделай паузу на 30+ минут"
            )

        # Пустые результаты — возможно soft-block
        if searches > 5 and empty / searches >= 0.8:
            if status == "healthy":
                status = "warning"
            recommendations.append(
                f"⚠ {empty}/{searches} поисков пустые — возможен soft-block"
            )

        return {
            "status":              status,
            "captcha_rate_24h":    round(captcha_rate, 3),
            "captcha_rate_1h":     round(captcha_rate_1h, 3),
            "consecutive_blocks":  consecutive_blocks,
            "total_searches_24h":  searches,
            "total_captchas_24h":  captchas,
            "empty_results_24h":   empty,
            "total_in_log":        len(self._metrics),
            "recommendations":     recommendations,
        }

    def print_report(self):
        health = self.get_health()

        icons = {"healthy": "✓", "warning": "⚠", "critical": "⛔"}
        icon  = icons.get(health["status"], "?")

        print("\n" + "═" * 60)
        print(f" ЗДОРОВЬЕ ПРОФИЛЯ  {icon}  {health['status'].upper()}")
        print("═" * 60)
        print(f" Поисков за 24ч:       {health['total_searches_24h']}")
        print(f" Капч за 24ч:          {health['total_captchas_24h']}")
        print(f" Rate капчи (24ч):     {health['captcha_rate_24h']:.1%}")
        print(f" Rate капчи (1ч):      {health['captcha_rate_1h']:.1%}")
        print(f" Пустых результатов:   {health['empty_results_24h']}")
        print(f" Блокировок подряд:    {health['consecutive_blocks']}")
        print(f" Записей в истории:    {health['total_in_log']}")

        if health["recommendations"]:
            print("\n Рекомендации:")
            for rec in health["recommendations"]:
                print(f"   {rec}")
        print("═" * 60 + "\n")

    # ──────────────────────────────────────────────────────────
    # УПРАВЛЕНИЕ
    # ──────────────────────────────────────────────────────────

    def should_abort(self) -> tuple[bool, str]:
        """
        Возвращает (should_abort, reason). True если следует остановить
        работу на этом профиле прямо сейчас.
        """
        health = self.get_health()
        if health["status"] == "critical":
            return True, health["recommendations"][0] if health["recommendations"] else "critical status"
        return False, ""

    def clear(self):
        """Сбрасывает всю историю (например после пересоздания профиля)"""
        self._metrics = []
        self._save()
        logging.info("[SessionQuality] История очищена")
