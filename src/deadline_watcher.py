# -*- coding: utf-8 -*-
"""Модуль контроля сроков и эскалаций для системы «НарядAI».

Реализует правила Раздела 6.1 ТЗ АО «Костанайские минералы»:

1. Эскалация не принятых аварийных нарядов — если наряд со статусом
   «Выдан» и приоритетом «Аварийный» висит более 3 минут, формируется
   критический алерт мастеру с предложением переназначить исполнителя.
2. Контроль дедлайнов — наряды со статусом «В работе», у которых истёк
   ``deadline``, переводятся в статус «Просрочен», а в журнал
   ``order_events`` добавляется автоматическая запись ИИ-контролёра.
3. Предупреждение за 30 минут — наряды «В работе», до дедлайна которых
   осталось менее 30 минут, попадают в список предупреждений.

Модуль полностью защищён: любая ошибка БД логируется и не роняет
вызывающую программу — вместо исключения возвращается пустой результат.

Точка входа: :func:`check_deadlines_and_escalations`.
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Конфигурация
# ------------------------------------------------------------------

#: Путь к БД SQLite в корне проекта (рядом с app.py).
DB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "naryad_ai.db")
)

#: Сколько минут ожидания принятия аварийного наряда до эскалации (Раздел 6.1).
ESCALATION_ACCEPT_MINUTES = 3

#: За сколько минут до дедлайна выдавать предупреждение (Раздел 6.1).
WARNING_BEFORE_DEADLINE_MINUTES = 30

#: Формат хранения дат в БД (см. src/database/schema.py).
DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M"

#: Поддерживаемые форматы для устойчивого разбора дат из legacy-записей.
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)


# ------------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------------

def _get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Открыть соединение с БД с включёнными внешними ключами."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Устойчиво разобрать дату из БД в ``datetime``.

    Возвращает ``None``, если значение пустое или формат не распознан.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None

    # Сначала пробуем ISO-формат (покрывает и 'YYYY-MM-DD HH:MM').
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        logger.debug("ISO-разбор не удался для %r: %s", value, exc)

    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    logger.warning("Не удалось разобрать дату из БД: %r", value)
    return None


def _fetch_active_orders(conn: sqlite3.Connection, status: str) -> List[sqlite3.Row]:
    """Выбрать наряды указанного статуса с человекочитаемыми названиями.

    Содержит джойны на оборудование, участок и исполнителя, чтобы можно
    было сформировать алерты без дополнительных запросов.
    """
    query = """
        SELECT
            wo.id            AS order_id,
            wo.order_num     AS order_num,
            wo.priority      AS priority,
            wo.status        AS status,
            wo.created_at    AS created_at,
            wo.deadline      AS deadline,
            wo.master_id     AS master_id,
            eq.name          AS equipment,
            un.name          AS unit,
            emp.fio          AS assignee
        FROM work_orders AS wo
        JOIN equipment AS eq ON eq.id = wo.equipment_id
        JOIN units     AS un ON un.id = wo.unit_id
        JOIN employees AS emp ON emp.id = wo.assignee_id
        WHERE wo.status = ?
        ORDER BY wo.id ASC
    """
    return conn.execute(query, (status,)).fetchall()


# ------------------------------------------------------------------
# Логика №1: эскалация не принятых аварийных нарядов
# ------------------------------------------------------------------

def _collect_escalations(
    conn: sqlite3.Connection, now: datetime
) -> List[str]:
    """Найти непринятые аварийные наряды старше 3 минут и вернуть алерты."""
    escalations: List[str] = []
    threshold = now - timedelta(minutes=ESCALATION_ACCEPT_MINUTES)

    rows = conn.execute(
        """
        SELECT order_num, created_at
        FROM work_orders
        WHERE status = 'Выдан' AND priority = 'Аварийный'
        ORDER BY id ASC
        """
    ).fetchall()

    for row in rows:
        created_at = _parse_datetime(row["created_at"])
        if created_at is None:
            continue
        if created_at < threshold:
            escalations.append(
                f"🚨 ЭСКАЛАЦИЯ: Аварийный наряд №{row['order_num']} не принят "
                f"исполнителем более {ESCALATION_ACCEPT_MINUTES} минут! "
                f"Предлагается переназначить на свободного слесаря."
            )

    return escalations


# ------------------------------------------------------------------
# Логика №2: просроченные наряды «В работе»
# ------------------------------------------------------------------

def _collect_and_expire(
    conn: sqlite3.Connection, now: datetime
) -> List[str]:
    """Найти просроченные наряды, перевести в «Просрочен» и вернуть алерты.

    Обновление статуса и запись в ``order_events`` выполняются в одной
    транзакции на наряд (SAVEPOINT). Если БД отклоняет перевод (например,
    CHECK-ограничение статуса), наряд пропускается, ошибка логируется, а
    остальные наряды обрабатываются дальше.
    """
    expired: List[str] = []
    now_str = now.strftime(DB_DATETIME_FORMAT)

    try:
        active_rows = _fetch_active_orders(conn, "В работе")
    except sqlite3.Error as exc:
        logger.error("Не удалось прочитать наряды 'В работе': %s", exc)
        return expired

    cursor = conn.cursor()
    for row in active_rows:
        deadline = _parse_datetime(row["deadline"])
        if deadline is None or deadline >= now:
            continue

        try:
            cursor.execute("SAVEPOINT sp_expire_order")
            cursor.execute(
                "UPDATE work_orders SET status = 'Просрочен' WHERE id = ?",
                (row["order_id"],),
            )
            cursor.execute(
                """
                INSERT INTO order_events (order_id, author_id, action, reason, event_time)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["order_id"],
                    row["master_id"],
                    "Просрочен",
                    "Автоматическая фиксация ИИ-контролером",
                    now_str,
                ),
            )
            cursor.execute("RELEASE SAVEPOINT sp_expire_order")
        except sqlite3.Error as exc:
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT sp_expire_order")
                cursor.execute("RELEASE SAVEPOINT sp_expire_order")
            except sqlite3.Error as rollback_exc:
                logger.debug(
                    "Не удалось откатить SAVEPOINT для наряда %s: %s",
                    row["order_num"],
                    rollback_exc,
                )
            logger.error(
                "Не удалось перевести наряд %s в статус 'Просрочен': %s",
                row["order_num"],
                exc,
            )
            continue

        expired.append(
            f"⏰ ПРОСРОЧКА: Наряд №{row['order_num']} "
            f"({row['equipment']}, {row['unit']}) просрочен! "
            f"Исполнитель: {row['assignee']}."
        )

    try:
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Ошибка фиксации просроченных нарядов: %s", exc)
        try:
            conn.rollback()
        except sqlite3.Error as rollback_exc:
            logger.error("Не удалось откатить транзакцию: %s", rollback_exc)

    return expired


# ------------------------------------------------------------------
# Логика №3: предупреждение за 30 минут до дедлайна
# ------------------------------------------------------------------

def _collect_warnings(
    conn: sqlite3.Connection, now: datetime
) -> List[str]:
    """Найти наряды «В работе» с дедлайном менее чем через 30 минут."""
    warnings: List[str] = []
    warning_limit = now + timedelta(minutes=WARNING_BEFORE_DEADLINE_MINUTES)

    try:
        active_rows = _fetch_active_orders(conn, "В работе")
    except sqlite3.Error as exc:
        logger.error("Не удалось прочитать наряды 'В работе': %s", exc)
        return warnings

    for row in active_rows:
        deadline = _parse_datetime(row["deadline"])
        if deadline is None:
            continue
        if now < deadline < warning_limit:
            minutes_left = max(0, int((deadline - now).total_seconds() // 60))
            warnings.append(
                f"⚠️ ПРЕДУПРЕЖДЕНИЕ: Наряд №{row['order_num']} "
                f"({row['equipment']}, {row['unit']}) — до дедлайна осталось "
                f"{minutes_left} мин.! Исполнитель: {row['assignee']}."
            )

    return warnings


# ------------------------------------------------------------------
# Основная точка входа
# ------------------------------------------------------------------

def check_deadlines_and_escalations() -> Dict[str, List[str]]:
    """Проверить сроки нарядов и сформировать алерты (Раздел 6.1 ТЗ).

    Выполняет три независимые проверки за один проход:

    * **escalations** — аварийные наряды «Выдан», не принятые более 3 минут;
    * **expired** — наряды «В работе» с истёкшим дедлайном (переводятся в
      статус «Просрочен» + запись в ``order_events``);
    * **warnings** — наряды «В работе» с дедлайном менее чем через 30 минут.

    Returns
    -------
    dict
        ``{"escalations": [...], "expired": [...], "warnings": [...]}``.

        При любой ошибке БД возвращается словарь с тремя пустыми списками —
        функция никогда не выбрасывает исключение наружу.
    """
    empty_result: Dict[str, List[str]] = {
        "escalations": [],
        "expired": [],
        "warnings": [],
    }

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _get_connection(DB_PATH)
        now = datetime.now()

        escalations = _collect_escalations(conn, now)
        expired = _collect_and_expire(conn, now)
        warnings = _collect_warnings(conn, now)

        return {
            "escalations": escalations,
            "expired": expired,
            "warnings": warnings,
        }

    except sqlite3.Error as exc:
        logger.error("Ошибка БД при контроле сроков нарядов: %s", exc)
        return empty_result
    except Exception as exc:  # noqa: BLE001 — защита от любых непредвиденных сбоев
        logger.exception("Непредвиденная ошибка в контроле сроков: %s", exc)
        return empty_result
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения: %s", close_exc)


if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = check_deadlines_and_escalations()
    print(json.dumps(result, ensure_ascii=False, indent=2))
