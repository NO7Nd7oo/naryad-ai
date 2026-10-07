# -*- coding: utf-8 -*-
"""Продвинутая аналитика ТОиР для системы «НарядAI».

Разделы 6.5, 6.6 и 7 ТЗ АО «Костанайские минералы».

Модуль предоставляет три независимые функции:

* :func:`detect_post_ppr_failures` — находит повторные поломки оборудования
  в течение 7 дней после закрытия планового наряда (ППР) и определяет
  бригаду, выполнявшую ремонт.
* :func:`calculate_worker_ratings` — прозрачный скоринг исполнителей
  по формуле ТЗ с детализацией бонусов и штрафов.
* :func:`get_downtime_breakdown` — группировка часов простоя по участкам
  и шифрам неисправностей (М, Э, Г, П, С) с расчётом прямых финансовых
  потерь по ставке 450 000 ₸/час.

Все SQL-запросы обёрнуты в try-except. Ни одна функция не выбрасывает
исключение наружу: при ошибке БД возвращается безопасный результат.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

#: Путь к БД SQLite в корне проекта (рядом с app.py).
DB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "naryad_ai.db")
)

#: Ставка прямых потерь от простоя, ₸/час (Раздел 7 ТЗ).
DOWNTIME_RATE_PER_HOUR = 450_000

#: Горизонт анализа повторных поломок после ППР, дней.
POST_PPR_WINDOW_DAYS = 7

#: Категории шифров неисправностей (Раздел 5.4 ТЗ).
FAULT_CATEGORIES: Dict[str, str] = {
    "М": "Механика",
    "Э": "Электрика",
    "Г": "Гидравлика",
    "П": "Пневматика",
    "С": "Смазка",
}

#: Статусы, при которых наряд считается закрытым.
CLOSED_STATUSES: Sequence[str] = ("Закрыт", "Исполнено")

#: Ключевые слова «уважительной причины» отказа исполнителя.
VALID_REJECT_MARKERS: Sequence[str] = (
    "уваж",
    "болезн",
    "травм",
    "отпуск",
    "авари",
    "поломк",
    "отсутств",
    "согласован",
)


# ------------------------------------------------------------------
# Подключение к БД и безопасные запросы
# ------------------------------------------------------------------
def _get_connection() -> sqlite3.Connection:
    """Соединение с ``naryad_ai.db`` с внешними ключами и таймаутом."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    return conn


def _fetchall(sql: str, params: Sequence[Any] = ()) -> Optional[List[Dict[str, Any]]]:
    """Безопасный SELECT → список словарей. ``None`` — ошибка БД."""
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _get_connection()
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        logger.error("Ошибка SQL в advanced_analytics: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — путь/драйвер не должны ронять экран
        logger.exception("Непредвиденная ошибка БД в advanced_analytics: %s", type(exc).__name__)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения: %s", close_exc)


def _fetch_df(sql: str, params: Sequence[Any] = ()) -> pd.DataFrame:
    """Безопасный SELECT → DataFrame. При ошибке — пустой DataFrame."""
    rows = _fetchall(sql, params)
    if rows is None:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _category_of(code: Optional[str], fallback: Optional[str] = None) -> str:
    """Категория шифра: из справочника, иначе по префиксу кода."""
    if fallback:
        return str(fallback)
    if not code:
        return "Прочее"
    prefix = str(code).strip()[:1].upper()
    return FAULT_CATEGORIES.get(prefix, "Прочее")


# ==================================================================
# 1. Анализ качества ППР: повторные поломки < 7 дней
# ==================================================================
def detect_post_ppr_failures(
    window_days: int = POST_PPR_WINDOW_DAYS,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Найти повторные поломки в течение ``window_days`` дней после ППР.

    Для каждого закрытого планового наряда (ППР) проверяются последующие
    аварийные/высокоприоритетные наряды по тому же оборудованию. Если такой
    наряд создан не позже ``window_days`` дней после завершения ППР — случай
    считается подозрительным (некачественный ремонт).

    Returns
    -------
    list[dict]
        Отсортированный по «свежести» повторной поломки список записей:

        ``equipment``, ``equipment_id``, ``ppr_order_num``,
        ``ppr_completed_at``, ``ppr_team`` (бригада, выполнявшая ремонт),
        ``ppr_assignee``, ``failure_order_num``, ``failure_created_at``,
        ``failure_priority``, ``fault_code``, ``days_after``,
        ``downtime_hours``.

        При ошибке БД возвращается пустой список.
    """
    try:
        window = max(1, int(window_days))
    except (TypeError, ValueError):
        window = POST_PPR_WINDOW_DAYS

    try:
        limit_value = max(1, int(limit))
    except (TypeError, ValueError):
        limit_value = 100

    closed_placeholders = ", ".join("?" for _ in CLOSED_STATUSES)
    sql = f"""
        SELECT
            e.id                AS equipment_id,
            e.name              AS equipment,
            ppr.order_num       AS ppr_order_num,
            ppr.completed_at    AS ppr_completed_at,
            ppr_assignee.fio    AS ppr_assignee,
            ppr_assignee.team_name AS ppr_team,
            f.order_num         AS failure_order_num,
            f.created_at        AS failure_created_at,
            f.priority          AS failure_priority,
            f.order_type        AS failure_order_type,
            f.fault_code        AS fault_code,
            COALESCE(f.downtime_hours, 0.0) AS downtime_hours,
            ROUND(julianday(f.created_at) - julianday(ppr.completed_at), 1) AS days_after
        FROM work_orders AS ppr
        JOIN equipment AS e ON e.id = ppr.equipment_id
        LEFT JOIN employees AS ppr_assignee ON ppr_assignee.id = ppr.assignee_id
        JOIN work_orders AS f ON f.equipment_id = ppr.equipment_id
        WHERE ppr.order_type = 'Плановый'
          AND ppr.status IN ({closed_placeholders})
          AND ppr.completed_at IS NOT NULL
          AND f.id <> ppr.id
          AND f.created_at > ppr.completed_at
          AND julianday(f.created_at) - julianday(ppr.completed_at) <= ?
          AND (
                f.order_type = 'Внеплановый (аварийный)'
                OR f.priority IN ('Аварийный', 'Высокий')
              )
        ORDER BY days_after ASC, f.created_at DESC
        LIMIT ?
    """
    params = (*CLOSED_STATUSES, float(window), limit_value)
    rows = _fetchall(sql, params)
    if not rows:
        return []

    result: List[Dict[str, Any]] = []
    for row in rows:
        days_after = row.get("days_after")
        try:
            days_after = float(days_after) if days_after is not None else None
        except (TypeError, ValueError):
            days_after = None
        result.append(
            {
                "equipment": row.get("equipment") or "—",
                "equipment_id": row.get("equipment_id"),
                "ppr_order_num": row.get("ppr_order_num") or "—",
                "ppr_completed_at": row.get("ppr_completed_at") or "—",
                "ppr_team": row.get("ppr_team") or "—",
                "ppr_assignee": row.get("ppr_assignee") or "—",
                "failure_order_num": row.get("failure_order_num") or "—",
                "failure_created_at": row.get("failure_created_at") or "—",
                "failure_priority": row.get("failure_priority") or "—",
                "failure_order_type": row.get("failure_order_type") or "—",
                "fault_code": row.get("fault_code") or "—",
                "days_after": days_after,
                "downtime_hours": float(row.get("downtime_hours") or 0.0),
            }
        )
    return result


def post_ppr_failures_dataframe(window_days: int = POST_PPR_WINDOW_DAYS) -> pd.DataFrame:
    """Табличное представление :func:`detect_post_ppr_failures` для Streamlit."""
    records = detect_post_ppr_failures(window_days=window_days)
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    frame = frame.rename(
        columns={
            "equipment": "Оборудование",
            "ppr_order_num": "Наряд ППР",
            "ppr_completed_at": "ППР завершён",
            "ppr_team": "Бригада (ремонт ППР)",
            "ppr_assignee": "Исполнитель ППР",
            "failure_order_num": "Повторный наряд",
            "failure_created_at": "Поломка",
            "failure_priority": "Приоритет",
            "fault_code": "Шифр",
            "days_after": "Дней после ППР",
            "downtime_hours": "Простой, ч",
        }
    )
    ordered = [
        "Оборудование",
        "Наряд ППР",
        "ППР завершён",
        "Бригада (ремонт ППР)",
        "Исполнитель ППР",
        "Повторный наряд",
        "Поломка",
        "Приоритет",
        "Шифр",
        "Дней после ППР",
        "Простой, ч",
    ]
    return frame[[col for col in ordered if col in frame.columns]]


# ==================================================================
# 2. Рейтинг исполнителей (прозрачный скоринг)
# ==================================================================
def calculate_worker_ratings() -> pd.DataFrame:
    """Рассчитать прозрачный скоринг исполнителей по формуле ТЗ.

    .. code-block:: text

        Score = (Доля нарядов в срок * 40)
              + (Средняя оценка ИИ * 0.4)
              - (Кол-во возвратов на доработку * 5)
              - (Отказы без уважительной причины * 10)

    Returns
    -------
    pandas.DataFrame
        Отсортированная по убыванию ``Score`` таблица с детализацией:

        ``Сотрудник``, ``Специальность``, ``Бригада``, ``Всего нарядов``,
        ``Закрыто``, ``В срок %``, ``Ср. оценка ИИ``, ``Возвраты``,
        ``Отказы (без причины)``, ``Бонус за сроки``, ``Бонус за качество``,
        ``Штраф за доработки``, ``Штраф за отказы``, ``Score``.

        При ошибке БД возвращается пустой DataFrame.
    """
    workers = _fetch_df(
        """
        SELECT id AS employee_id, fio, specialty, team_name
        FROM employees
        WHERE role = 'Исполнитель'
        ORDER BY fio ASC
        """
    )
    if workers.empty:
        return pd.DataFrame()

    closed_placeholders = ", ".join("?" for _ in CLOSED_STATUSES)
    orders = _fetch_df(
        f"""
        SELECT
            assignee_id,
            COUNT(*) AS total_orders,
            SUM(CASE WHEN status IN ({closed_placeholders}) THEN 1 ELSE 0 END) AS closed_orders,
            SUM(CASE
                    WHEN status IN ({closed_placeholders})
                     AND completed_at IS NOT NULL
                     AND completed_at <= deadline
                    THEN 1 ELSE 0 END) AS on_time_orders,
            SUM(CASE WHEN status = 'На доработку' THEN 1 ELSE 0 END) AS rework_current,
            SUM(CASE WHEN status = 'Отклонён' THEN 1 ELSE 0 END) AS reject_current
        FROM work_orders
        GROUP BY assignee_id
        """,
        (*CLOSED_STATUSES, *CLOSED_STATUSES),
    )

    ai_scores = _fetch_df(
        """
        SELECT wo.assignee_id AS assignee_id,
               AVG(ae.score)  AS avg_score,
               COUNT(ae.id)   AS ai_count
        FROM work_orders AS wo
        JOIN ai_evaluations AS ae ON ae.order_id = wo.id
        GROUP BY wo.assignee_id
        """
    )

    rework_events = _fetch_df(
        """
        SELECT wo.assignee_id AS assignee_id,
               COUNT(DISTINCT oe.order_id) AS rework_events
        FROM order_events AS oe
        JOIN work_orders AS wo ON wo.id = oe.order_id
        WHERE oe.action = 'На доработку'
        GROUP BY wo.assignee_id
        """
    )

    reject_reasons = _fetch_df(
        """
        SELECT wo.assignee_id AS assignee_id, oe.reason AS reason
        FROM order_events AS oe
        JOIN work_orders AS wo ON wo.id = oe.order_id
        WHERE oe.action = 'Отклонён'
        """
    )

    orders_map: Dict[int, Dict[str, Any]] = {}
    for row in orders.to_dict("records"):
        try:
            orders_map[int(row["assignee_id"])] = row
        except (TypeError, ValueError, KeyError):
            continue

    ai_map: Dict[int, Dict[str, Any]] = {}
    for row in ai_scores.to_dict("records"):
        try:
            ai_map[int(row["assignee_id"])] = row
        except (TypeError, ValueError, KeyError):
            continue

    rework_map: Dict[int, int] = {}
    for row in rework_events.to_dict("records"):
        try:
            rework_map[int(row["assignee_id"])] = int(row.get("rework_events") or 0)
        except (TypeError, ValueError, KeyError):
            continue

    unjust_rejects: Dict[int, int] = {}
    if not reject_reasons.empty:
        for row in reject_reasons.to_dict("records"):
            try:
                employee_id = int(row["assignee_id"])
            except (TypeError, ValueError, KeyError):
                continue
            reason = str(row.get("reason") or "").casefold()
            has_valid_reason = any(marker in reason for marker in VALID_REJECT_MARKERS)
            if not has_valid_reason:
                unjust_rejects[employee_id] = unjust_rejects.get(employee_id, 0) + 1

    records: List[Dict[str, Any]] = []
    for worker in workers.to_dict("records"):
        employee_id = int(worker["employee_id"])
        order_stats = orders_map.get(employee_id, {})
        ai_stats = ai_map.get(employee_id, {})

        total_orders = int(order_stats.get("total_orders") or 0)
        closed_orders = int(order_stats.get("closed_orders") or 0)
        on_time_orders = int(order_stats.get("on_time_orders") or 0)
        rework_current = int(order_stats.get("rework_current") or 0)
        reject_current = int(order_stats.get("reject_current") or 0)

        rework_count = max(rework_current, rework_map.get(employee_id, 0))
        reject_count = max(reject_current, unjust_rejects.get(employee_id, 0))

        on_time_rate = (on_time_orders / closed_orders) if closed_orders > 0 else 0.0
        try:
            avg_ai = float(ai_stats.get("avg_score") or 0.0)
        except (TypeError, ValueError):
            avg_ai = 0.0

        bonus_schedule = on_time_rate * 40.0
        bonus_quality = avg_ai * 0.4
        penalty_rework = rework_count * 5.0
        penalty_rejects = reject_count * 10.0
        score = bonus_schedule + bonus_quality - penalty_rework - penalty_rejects

        records.append(
            {
                "Сотрудник": worker.get("fio") or "—",
                "Специальность": worker.get("specialty") or "—",
                "Бригада": worker.get("team_name") or "—",
                "Всего нарядов": total_orders,
                "Закрыто": closed_orders,
                "В срок %": round(on_time_rate * 100.0, 1),
                "Ср. оценка ИИ": round(avg_ai, 1),
                "Возвраты": rework_count,
                "Отказы (без причины)": reject_count,
                "Бонус за сроки": round(bonus_schedule, 2),
                "Бонус за качество": round(bonus_quality, 2),
                "Штраф за доработки": round(penalty_rework, 2),
                "Штраф за отказы": round(penalty_rejects, 2),
                "Score": round(score, 2),
            }
        )

    frame = pd.DataFrame(records)
    if frame.empty:
        return frame
    return frame.sort_values("Score", ascending=False).reset_index(drop=True)


# ==================================================================
# 3. Разбивка простоев и финансовые потери
# ==================================================================
def get_downtime_breakdown(rate_per_hour: float = DOWNTIME_RATE_PER_HOUR) -> Dict[str, Any]:
    """Сгруппировать простои по участкам и шифрам неисправностей.

    Прямые потери считаются как ``часы простоя × rate_per_hour``
    (по умолчанию 450 000 ₸/час, Раздел 7 ТЗ).

    Returns
    -------
    dict
        ``breakdown`` — DataFrame (``Участок``, ``Шифр``, ``Категория``,
        ``Инциденты``, ``Простой, ч``, ``Потери, ₸``);

        ``by_category`` — DataFrame по категориям М/Э/Г/П/С;

        ``total_hours``, ``total_loss``, ``rate_per_hour``, ``total_incidents``.

        При ошибке БД возвращаются пустые таблицы и нулевые итоги.
    """
    try:
        rate = float(rate_per_hour)
    except (TypeError, ValueError):
        rate = float(DOWNTIME_RATE_PER_HOUR)

    empty = {
        "breakdown": pd.DataFrame(),
        "by_category": pd.DataFrame(),
        "total_hours": 0.0,
        "total_loss": 0.0,
        "total_incidents": 0,
        "rate_per_hour": rate,
    }

    rows = _fetchall(
        """
        SELECT
            COALESCE(u.name, '—')                       AS unit_name,
            COALESCE(wo.fault_code, '—')                AS fault_code,
            COALESCE(fc.category, '')                   AS category,
            COUNT(*)                                    AS incidents,
            COALESCE(SUM(wo.downtime_hours), 0.0)       AS downtime_hours
        FROM work_orders AS wo
        LEFT JOIN units AS u ON u.id = wo.unit_id
        LEFT JOIN fault_codes AS fc ON fc.code = wo.fault_code
        GROUP BY u.name, wo.fault_code
        ORDER BY downtime_hours DESC, incidents DESC
        """
    )
    if rows is None:
        return empty
    if not rows:
        return empty

    records: List[Dict[str, Any]] = []
    for row in rows:
        hours = float(row.get("downtime_hours") or 0.0)
        category = _category_of(row.get("fault_code"), row.get("category"))
        records.append(
            {
                "Участок": row.get("unit_name") or "—",
                "Шифр": row.get("fault_code") or "—",
                "Категория": category,
                "Инциденты": int(row.get("incidents") or 0),
                "Простой, ч": round(hours, 1),
                "Потери, ₸": round(hours * rate, 2),
            }
        )

    breakdown = pd.DataFrame(records).sort_values("Потери, ₸", ascending=False).reset_index(drop=True)
    by_category = (
        breakdown.groupby("Категория", as_index=False)[["Инциденты", "Простой, ч", "Потери, ₸"]]
        .sum()
        .sort_values("Потери, ₸", ascending=False)
        .reset_index(drop=True)
    )

    total_hours = float(breakdown["Простой, ч"].sum())
    total_loss = float(breakdown["Потери, ₸"].sum())
    total_incidents = int(breakdown["Инциденты"].sum())

    return {
        "breakdown": breakdown,
        "by_category": by_category,
        "total_hours": round(total_hours, 1),
        "total_loss": round(total_loss, 2),
        "total_incidents": total_incidents,
        "rate_per_hour": rate,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print("Повторные поломки после ППР:", len(detect_post_ppr_failures()))
    print(calculate_worker_ratings().head(5).to_string(index=False))
    breakdown = get_downtime_breakdown()
    print("Часы простоя:", breakdown["total_hours"], "| Потери, ₸:", breakdown["total_loss"])
