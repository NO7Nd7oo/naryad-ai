# -*- coding: utf-8 -*-
"""Шлюз интеграции «НарядAI» ↔ 1С:ТОиР / ERP.

Разделы 9 и 12 ТЗ АО «Костанайские минералы».

Модуль реализует двусторонний обмен:

* :func:`generate_1c_sync_payload` — формирует стандартизированный
  JSON-пакет за смену (закрытые наряды, списанные ТМЦ, часы простоя,
  электронные подписи ИИ).
* :func:`import_1c_work_orders` — принимает JSON от внешней системы
  планирования и создаёт наряды в SQLite ``naryad_ai.db``.

Все обращения к БД обёрнуты в try-except. Функции не выбрасывают
исключение наружу: при ошибке возвращается безопасный результат.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Путь к БД SQLite в корне проекта (рядом с app.py).
DB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "naryad_ai.db")
)

#: Идентификация системы-источника и версия контракта обмена.
SOURCE_SYSTEM = "NaryadAI"
PAYLOAD_VERSION = "1.0"
COMPANY_NAME = "АО «Костанайские минералы»"

#: Значения, разрешённые CHECK-ограничениями схемы work_orders.
ALLOWED_PRIORITIES: Sequence[str] = ("Аварийный", "Высокий", "Обычный", "Плановый")
ALLOWED_ORDER_TYPES: Sequence[str] = ("Плановый", "Внеплановый (аварийный)")
ALLOWED_STATUSES: Sequence[str] = (
    "Выдан",
    "Принят в работу",
    "В очереди",
    "В работе",
    "Приостановлен",
    "Отклонён",
    "Исполнено",
    "Проверка ИИ",
    "На доработку",
    "Закрыт",
)
CLOSED_STATUSES: Sequence[str] = ("Закрыт", "Исполнено")

_DATETIME_FORMAT = "%Y-%m-%d %H:%M"


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
        logger.error("Ошибка SQL в api_gateway: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Непредвиденная ошибка БД в api_gateway: %s", type(exc).__name__)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения: %s", close_exc)


def _execute(sql: str, params: Sequence[Any] = ()) -> Tuple[bool, Optional[int], str]:
    """Безопасный INSERT/UPDATE. Возвращает ``(ok, lastrowid, error)``."""
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        conn.commit()
        return True, cursor.lastrowid, ""
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка записи в api_gateway: %s", exc)
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.debug("Не удалось откатить транзакцию api_gateway.")
        return False, None, str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения: %s", close_exc)


# ------------------------------------------------------------------
# Вспомогательные преобразования
# ------------------------------------------------------------------
def _now_str() -> str:
    return datetime.now().strftime(_DATETIME_FORMAT)


def _parse_datetime(value: Any, default: Optional[datetime] = None) -> datetime:
    """Устойчиво разобрать дату из внешнего JSON."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
    return default or datetime.now()


def _normalize_priority(value: Any) -> str:
    text = str(value or "").strip()
    if text in ALLOWED_PRIORITIES:
        return text
    lowered = text.casefold()
    if "авар" in lowered or "emerg" in lowered:
        return "Аварийный"
    if "высок" in lowered or "high" in lowered:
        return "Высокий"
    if "план" in lowered or "plan" in lowered:
        return "Плановый"
    return "Обычный"


def _normalize_order_type(value: Any, priority: str) -> str:
    text = str(value or "").strip()
    if text in ALLOWED_ORDER_TYPES:
        return text
    lowered = text.casefold()
    if "авар" in lowered or "внеплан" in lowered or priority == "Аварийный":
        return "Внеплановый (аварийный)"
    return "Плановый"


def _normalize_status(value: Any) -> str:
    text = str(value or "").strip()
    if text in ALLOWED_STATUSES:
        return text
    lowered = text.casefold()
    mapping = {
        "выдан": "Выдан",
        "нов": "Выдан",
        "работ": "В работе",
        "закрыт": "Закрыт",
        "исполн": "Исполнено",
        "доработ": "На доработку",
        "отклон": "Отклонён",
    }
    for marker, status in mapping.items():
        if marker in lowered:
            return status
    return "Выдан"


def _electronic_signature(order: Dict[str, Any]) -> Dict[str, Any]:
    """Детерминированная электронная подпись ИИ для наряда (SHA-256)."""
    payload = "|".join(
        str(order.get(key, ""))
        for key in (
            "order_num",
            "status",
            "completed_at",
            "assignee",
            "equipment",
            "ai_verdict",
            "ai_score",
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "algorithm": "SHA-256",
        "hash": digest,
        "signed_at": _now_str(),
        "signed_by": "NaryadAI AI Engine",
        "verdict": order.get("ai_verdict") or "—",
        "score": order.get("ai_score"),
    }


# ==================================================================
# 1. Экспорт пакета для 1С:ТОиР / ERP
# ==================================================================
def _load_order_materials(order_id: int) -> List[Dict[str, Any]]:
    """Списанные ТМЦ по наряду с номенклатурными данными."""
    rows = _fetchall(
        """
        SELECT
            mc.id           AS material_id,
            mc.name         AS name,
            mc.unit_measure AS unit_measure,
            mw.quantity     AS quantity
        FROM material_writeoffs AS mw
        LEFT JOIN materials_catalog AS mc ON mc.id = mw.material_id
        WHERE mw.order_id = ?
        ORDER BY mc.name ASC
        """,
        (int(order_id),),
    )
    if not rows:
        return []
    materials: List[Dict[str, Any]] = []
    for row in rows:
        materials.append(
            {
                "material_id": row.get("material_id"),
                "nomenclature": row.get("name") or "—",
                "unit_measure": row.get("unit_measure") or "—",
                "quantity": float(row.get("quantity") or 0.0),
            }
        )
    return materials


def generate_1c_sync_payload(
    since: Optional[str] = None,
    shift_date: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Сформировать JSON-пакет синхронизации с 1С:ТОиР за смену.

    Parameters
    ----------
    since:
        Начало периода в формате ``YYYY-MM-DD HH:MM``. Если не задано —
        берётся начало дня ``shift_date`` (или сегодняшнего дня).
    shift_date:
        Дата смены ``YYYY-MM-DD`` (используется, когда ``since`` пуст).
    limit:
        Максимум нарядов в пакете.

    Returns
    -------
    dict
        Готовый к сериализации пакет с ключами ``message_type``,
        ``version``, ``company``, ``generated_at``, ``period``,
        ``work_orders`` и ``totals``. Исключение наружу не выбрасывается.
    """
    generated_at = _now_str()
    period_date = (shift_date or date.today().isoformat()).strip()
    auto_period = since is None
    period_start = (since or f"{period_date} 00:00").strip()

    try:
        limit_value = max(1, int(limit))
    except (TypeError, ValueError):
        limit_value = 50

    closed_placeholders = ", ".join("?" for _ in CLOSED_STATUSES)
    rows = _fetchall(
        f"""
        SELECT
            wo.id              AS id,
            wo.order_num       AS order_num,
            wo.order_type      AS order_type,
            wo.description     AS description,
            wo.priority        AS priority,
            wo.status          AS status,
            wo.normative_hours AS normative_hours,
            wo.created_at      AS created_at,
            wo.deadline        AS deadline,
            wo.completed_at    AS completed_at,
            wo.downtime_hours  AS downtime_hours,
            wo.fault_code      AS fault_code,
            wo.closing_comment AS closing_comment,
            eq.name            AS equipment,
            eq.inv_number      AS inv_number,
            un.name            AS unit,
            emp.fio            AS assignee,
            mst.fio            AS master,
            ae.verdict         AS ai_verdict,
            ae.score           AS ai_score,
            ae.explanation     AS ai_explanation,
            ae.evaluated_at    AS ai_evaluated_at
        FROM work_orders AS wo
        LEFT JOIN equipment AS eq ON eq.id = wo.equipment_id
        LEFT JOIN units     AS un ON un.id = wo.unit_id
        LEFT JOIN employees AS emp ON emp.id = wo.assignee_id
        LEFT JOIN employees AS mst ON mst.id = wo.master_id
        LEFT JOIN ai_evaluations AS ae ON ae.order_id = wo.id
        WHERE wo.status IN ({closed_placeholders})
          AND wo.completed_at IS NOT NULL
          AND wo.completed_at >= ?
        ORDER BY wo.completed_at DESC, wo.id DESC
        LIMIT ?
        """,
        (*CLOSED_STATUSES, period_start, limit_value),
    )

    fallback_used = False
    if (rows is None or not rows) and auto_period:
        rows = _fetchall(
            f"""
            SELECT
                wo.id              AS id,
                wo.order_num       AS order_num,
                wo.order_type      AS order_type,
                wo.description     AS description,
                wo.priority        AS priority,
                wo.status          AS status,
                wo.normative_hours AS normative_hours,
                wo.created_at      AS created_at,
                wo.deadline        AS deadline,
                wo.completed_at    AS completed_at,
                wo.downtime_hours  AS downtime_hours,
                wo.fault_code      AS fault_code,
                wo.closing_comment AS closing_comment,
                eq.name            AS equipment,
                eq.inv_number      AS inv_number,
                un.name            AS unit,
                emp.fio            AS assignee,
                mst.fio            AS master,
                ae.verdict         AS ai_verdict,
                ae.score           AS ai_score,
                ae.explanation     AS ai_explanation,
                ae.evaluated_at    AS ai_evaluated_at
            FROM work_orders AS wo
            LEFT JOIN equipment AS eq ON eq.id = wo.equipment_id
            LEFT JOIN units     AS un ON un.id = wo.unit_id
            LEFT JOIN employees AS emp ON emp.id = wo.assignee_id
            LEFT JOIN employees AS mst ON mst.id = wo.master_id
            LEFT JOIN ai_evaluations AS ae ON ae.order_id = wo.id
            WHERE wo.status IN ({closed_placeholders})
              AND wo.completed_at IS NOT NULL
            ORDER BY wo.completed_at DESC, wo.id DESC
            LIMIT ?
            """,
            (*CLOSED_STATUSES, limit_value),
        )
        fallback_used = True

    orders_payload: List[Dict[str, Any]] = []
    total_downtime = 0.0
    total_materials = 0
    ai_scores: List[float] = []

    for row in rows or []:
        order_core = {
            "order_num": row.get("order_num"),
            "status": row.get("status"),
            "completed_at": row.get("completed_at"),
            "assignee": row.get("assignee"),
            "equipment": row.get("equipment"),
            "ai_verdict": row.get("ai_verdict"),
            "ai_score": row.get("ai_score"),
        }
        materials = _load_order_materials(int(row.get("id") or 0))
        total_materials += len(materials)
        downtime = float(row.get("downtime_hours") or 0.0)
        total_downtime += downtime
        if row.get("ai_score") is not None:
            try:
                ai_scores.append(float(row["ai_score"]))
            except (TypeError, ValueError):
                logger.debug("Некорректная оценка ИИ в наряде %s", row.get("order_num"))

        orders_payload.append(
            {
                "order_num": row.get("order_num"),
                "order_type": row.get("order_type"),
                "description": row.get("description"),
                "priority": row.get("priority"),
                "status": row.get("status"),
                "normative_hours": row.get("normative_hours"),
                "unit": row.get("unit"),
                "equipment": row.get("equipment"),
                "equipment_inv": row.get("inv_number"),
                "assignee": row.get("assignee"),
                "master": row.get("master"),
                "fault_code": row.get("fault_code"),
                "created_at": row.get("created_at"),
                "deadline": row.get("deadline"),
                "completed_at": row.get("completed_at"),
                "downtime_hours": downtime,
                "closing_comment": row.get("closing_comment"),
                "ai_verification": {
                    "verdict": row.get("ai_verdict") or "—",
                    "score": row.get("ai_score"),
                    "explanation": row.get("ai_explanation") or "—",
                    "evaluated_at": row.get("ai_evaluated_at") or "—",
                },
                "materials": materials,
                "electronic_signature": _electronic_signature(order_core),
            }
        )

    avg_ai = round(sum(ai_scores) / len(ai_scores), 1) if ai_scores else 0.0

    return {
        "message_type": "WorkOrdersSync",
        "source": SOURCE_SYSTEM,
        "version": PAYLOAD_VERSION,
        "company": COMPANY_NAME,
        "generated_at": generated_at,
        "period": {
            "shift_date": period_date,
            "since": period_start,
            "fallback_last_closed": fallback_used,
        },
        "work_orders": orders_payload,
        "totals": {
            "orders_count": len(orders_payload),
            "materials_count": total_materials,
            "total_downtime_hours": round(total_downtime, 1),
            "avg_ai_score": avg_ai,
        },
    }


def payload_to_json_bytes(payload: Dict[str, Any]) -> bytes:
    """Сериализовать пакет в UTF-8 JSON (без ASCII-экранирования)."""
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 — выгрузка не должна ронять экран
        logger.exception("Не удалось сериализовать пакет 1С: %s", type(exc).__name__)
        return json.dumps({"error": "serialization_failed"}, ensure_ascii=False).encode("utf-8")


# ==================================================================
# 2. Импорт нарядов из внешней системы планирования
# ==================================================================
def _resolve_unit(value: Any, create_missing: bool) -> Optional[int]:
    """Найти участок по ID или названию; при необходимости создать."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if text.isdigit():
        rows = _fetchall("SELECT id FROM units WHERE id = ?", (int(text),))
        if rows:
            return int(rows[0]["id"])
    rows = _fetchall("SELECT id FROM units WHERE name = ? LIMIT 1", (text,))
    if rows:
        return int(rows[0]["id"])
    if not create_missing:
        return None
    ok, lastrowid, _ = _execute(
        "INSERT INTO units (name, description) VALUES (?, ?)",
        (text, "Создан при импорте из 1С"),
    )
    return int(lastrowid) if ok and lastrowid is not None else None


def _resolve_equipment(
    item: Dict[str, Any],
    unit_id: Optional[int],
    create_missing: bool,
) -> Optional[int]:
    """Найти оборудование по ID / инв. номеру / названию; создать при необходимости."""
    raw_id = item.get("equipment_id")
    if raw_id is not None and str(raw_id).strip().isdigit():
        rows = _fetchall("SELECT id FROM equipment WHERE id = ?", (int(raw_id),))
        if rows:
            return int(rows[0]["id"])

    inv = str(item.get("equipment_inv") or item.get("inv_number") or "").strip()
    if inv:
        rows = _fetchall("SELECT id FROM equipment WHERE inv_number = ? LIMIT 1", (inv,))
        if rows:
            return int(rows[0]["id"])

    name = str(item.get("equipment") or item.get("equipment_name") or "").strip()
    if name:
        rows = _fetchall("SELECT id FROM equipment WHERE name = ? LIMIT 1", (name,))
        if rows:
            return int(rows[0]["id"])

    if not create_missing or unit_id is None or not (name or inv):
        return None

    eq_name = name or f"Оборудование {inv}"
    eq_inv = inv or f"INV-1C-{abs(hash(eq_name)) % 100000:05d}"
    eq_type = str(item.get("eq_type") or "Прочее").strip() or "Прочее"
    criticality = str(item.get("criticality") or "Средняя").strip()
    if criticality not in ("Низкая", "Средняя", "Высокая", "Критическая"):
        criticality = "Средняя"
    ok, lastrowid, _ = _execute(
        """
        INSERT INTO equipment (name, inv_number, unit_id, eq_type, criticality)
        VALUES (?, ?, ?, ?, ?)
        """,
        (eq_name, eq_inv, int(unit_id), eq_type, criticality),
    )
    return int(lastrowid) if ok and lastrowid is not None else None


def _resolve_employee(
    value: Any,
    role: str,
    create_missing: bool,
    default_id: Optional[int] = None,
) -> Optional[int]:
    """Найти сотрудника по ID или ФИО; при необходимости создать."""
    if value is not None and str(value).strip().isdigit():
        rows = _fetchall("SELECT id FROM employees WHERE id = ?", (int(str(value).strip()),))
        if rows:
            return int(rows[0]["id"])
    text = str(value or "").strip()
    if text:
        rows = _fetchall("SELECT id FROM employees WHERE fio = ? LIMIT 1", (text,))
        if rows:
            return int(rows[0]["id"])
    if create_missing and text:
        ok, lastrowid, _ = _execute(
            """
            INSERT INTO employees (fio, specialty, grade, team_name, role, shift, status)
            VALUES (?, ?, ?, ?, ?, ?, 'Свободен')
            """,
            (text, "Слесарь-ремонтник", 4, "Импорт из 1С", role, "Смена 1"),
        )
        if ok and lastrowid is not None:
            return int(lastrowid)
    if default_id is not None:
        return default_id
    rows = _fetchall(
        "SELECT id FROM employees WHERE role = ? ORDER BY id ASC LIMIT 1", (role,)
    )
    if rows:
        return int(rows[0]["id"])
    return None


def _default_master_id() -> Optional[int]:
    rows = _fetchall("SELECT id FROM employees WHERE role = 'Мастер' ORDER BY id ASC LIMIT 1")
    if rows:
        return int(rows[0]["id"])
    rows = _fetchall("SELECT id FROM employees ORDER BY id ASC LIMIT 1")
    return int(rows[0]["id"]) if rows else None


def _default_executor_id() -> Optional[int]:
    rows = _fetchall(
        "SELECT id FROM employees WHERE role = 'Исполнитель' ORDER BY id ASC LIMIT 1"
    )
    return int(rows[0]["id"]) if rows else None


def import_1c_work_orders(
    json_data: Any,
    create_missing: bool = True,
) -> Dict[str, Any]:
    """Импортировать наряды из JSON внешней системы планирования.

    Parameters
    ----------
    json_data:
        ``dict``, JSON-строка или байты. Поддерживается как обёртка
        ``{"work_orders": [...]}``, так и «плоский» список нарядов.
    create_missing:
        Создавать отсутствующие участки, оборудование и сотрудников.

    Returns
    -------
    dict
        ``{"imported": int, "skipped": int, "errors": [...],
        "imported_orders": [...]}``. Исключение наружу не выбрасывается.
    """
    summary: Dict[str, Any] = {
        "imported": 0,
        "skipped": 0,
        "errors": [],
        "imported_orders": [],
    }

    try:
        if isinstance(json_data, (bytes, bytearray)):
            data = json.loads(bytes(json_data).decode("utf-8"))
        elif isinstance(json_data, str):
            data = json.loads(json_data)
        else:
            data = json_data
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        summary["errors"].append(f"Некорректный JSON: {type(exc).__name__}: {exc}")
        return summary

    if isinstance(data, dict):
        items = data.get("work_orders") or data.get("orders") or data.get("items") or []
    elif isinstance(data, list):
        items = data
    else:
        summary["errors"].append("Ожидался объект или список нарядов.")
        return summary

    if not isinstance(items, list):
        summary["errors"].append("Поле work_orders должно быть списком.")
        return summary

    master_default = _default_master_id()
    executor_default = _default_executor_id()

    for index, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                summary["errors"].append(f"Строка {index + 1}: элемент не является объектом.")
                summary["skipped"] += 1
                continue

            order_num = str(
                item.get("order_num") or item.get("number") or item.get("id") or ""
            ).strip()
            if not order_num:
                summary["errors"].append(f"Строка {index + 1}: не указан номер наряда.")
                summary["skipped"] += 1
                continue

            existing = _fetchall(
                "SELECT id FROM work_orders WHERE order_num = ? LIMIT 1", (order_num,)
            )
            if existing:
                summary["skipped"] += 1
                continue

            description = str(item.get("description") or "Наряд из внешней системы планирования").strip()
            priority = _normalize_priority(item.get("priority"))
            order_type = _normalize_order_type(item.get("order_type"), priority)
            status = _normalize_status(item.get("status"))

            unit_id = _resolve_unit(item.get("unit") or item.get("unit_id"), create_missing)
            equipment_id = _resolve_equipment(item, unit_id, create_missing)
            assignee_id = _resolve_employee(
                item.get("assignee") or item.get("assignee_id"),
                "Исполнитель",
                create_missing,
                default_id=executor_default,
            )
            master_id = _resolve_employee(
                item.get("master") or item.get("master_id"),
                "Мастер",
                create_missing,
                default_id=master_default,
            )

            if unit_id is None:
                summary["errors"].append(f"{order_num}: не удалось определить участок.")
                summary["skipped"] += 1
                continue
            if equipment_id is None:
                summary["errors"].append(f"{order_num}: не удалось определить оборудование.")
                summary["skipped"] += 1
                continue
            if assignee_id is None:
                summary["errors"].append(f"{order_num}: не удалось определить исполнителя.")
                summary["skipped"] += 1
                continue
            if master_id is None:
                summary["errors"].append(f"{order_num}: не удалось определить мастера.")
                summary["skipped"] += 1
                continue

            created_dt = _parse_datetime(item.get("created_at"), default=datetime.now())
            try:
                normative = float(item.get("normative_hours") or 2.0)
            except (TypeError, ValueError):
                normative = 2.0
            normative = max(0.5, min(24.0, normative))
            deadline_dt = _parse_datetime(
                item.get("deadline"), default=created_dt + timedelta(hours=normative)
            )
            completed_raw = item.get("completed_at")
            completed_dt = _parse_datetime(completed_raw) if completed_raw else None
            try:
                downtime = float(item.get("downtime_hours") or 0.0)
            except (TypeError, ValueError):
                downtime = 0.0
            fault_code = str(item.get("fault_code") or "").strip() or None
            closing_comment = item.get("closing_comment")

            ok, order_id, error = _execute(
                """
                INSERT INTO work_orders (
                    order_num, order_type, description, unit_id, equipment_id,
                    assignee_id, master_id, priority, normative_hours, status,
                    created_at, deadline, completed_at, closing_comment,
                    fault_code, downtime_hours
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_num,
                    order_type,
                    description,
                    int(unit_id),
                    int(equipment_id),
                    int(assignee_id),
                    int(master_id),
                    priority,
                    normative,
                    status,
                    created_dt.strftime(_DATETIME_FORMAT),
                    deadline_dt.strftime(_DATETIME_FORMAT),
                    completed_dt.strftime(_DATETIME_FORMAT) if completed_dt else None,
                    closing_comment,
                    fault_code,
                    downtime,
                ),
            )
            if not ok:
                summary["errors"].append(f"{order_num}: {error}")
                summary["skipped"] += 1
                continue

            _execute(
                """
                INSERT INTO order_events (order_id, author_id, action, reason, event_time)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(order_id),
                    int(master_id),
                    "Импорт из 1С",
                    "Наряд создан по входящему пакету внешней системы планирования",
                    created_dt.strftime(_DATETIME_FORMAT),
                ),
            )
            summary["imported"] += 1
            summary["imported_orders"].append(order_num)
        except Exception as exc:  # noqa: BLE001 — одна плохая строка не рушит импорт
            logger.exception("Ошибка импорта наряда #%s: %s", index + 1, type(exc).__name__)
            summary["errors"].append(f"Строка {index + 1}: {type(exc).__name__}: {exc}")
            summary["skipped"] += 1

    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    payload = generate_1c_sync_payload()
    print(json.dumps(payload["totals"], ensure_ascii=False, indent=2))
