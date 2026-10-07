# -*- coding: utf-8 -*-
"""Умный ИИ-учётчик (Smart Ledger AI) для системы «НарядAI».

Модуль обслуживает блок «🤖 Умный ИИ-учётчик и работа с CSV»:

* :func:`load_demo_ledger` — демонстрационная таблица списаний ТМЦ из
  ``naryad_ai.db`` (журнал ``material_writeoffs`` + наряды + оборудование).
* :func:`normalize_ledger` — мягкая нормализация произвольной CSV-таблицы
  (типы, даты, обрезка пробелов) без риска падения на «грязных» данных.
* :func:`audit_ledger` — интеллектуальный аудит: аномалии расхода, всплески
  за короткое окно, выбросы количества, рекомендации для материального
  учёта и обогащённая таблица с рекомендуемыми полями.
* :func:`export_ledger_excel` — выгрузка обработанной таблицы и заключения
  в отформатированный Excel (.xlsx).

Все операции обёрнуты в try-except: функции никогда не выбрасывают
исключение наружу и возвращают безопасный результат.
"""

from __future__ import annotations

import io
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

#: Путь к БД SQLite в корне проекта (рядом с app.py).
DB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "naryad_ai.db")
)

#: Минимальное число списаний позиции, при котором включается проверка.
ANOMALY_MIN_COUNT = 6

#: Окно «свежего» всплеска расхода, дней.
RECENT_WINDOW_DAYS = 3

#: Порог всплеска расхода за окно.
RECENT_SPIKE_THRESHOLD = 4

#: Во сколько раз превышение над медианой считается выбросом количества.
QUANTITY_OUTLIER_FACTOR = 2.0


# ------------------------------------------------------------------
# Подключение к БД и безопасные запросы
# ------------------------------------------------------------------
def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_df(sql: str, params: Sequence[Any] = ()) -> pd.DataFrame:
    """Безопасный SELECT → DataFrame. При ошибке — пустой DataFrame."""
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _get_connection()
        return pd.read_sql_query(sql, conn, params=tuple(params))
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка чтения журнала списаний: %s", exc)
        return pd.DataFrame()
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения: %s", close_exc)


# ------------------------------------------------------------------
# Демонстрационный журнал списаний
# ------------------------------------------------------------------
def _fallback_demo_ledger() -> pd.DataFrame:
    """Статическая демо-таблица, если БД недоступна (приложение не пустое)."""
    return pd.DataFrame(
        [
            {"Номенклатура": "Подшипник роликовый радиальный 22318", "Кол-во": 1.0,
             "Ед.": "шт", "Наряд": "Н-1014", "Оборудование": "Конвейер К-3",
             "Дата": "2026-10-05 08:20", "Статус": "Закрыт", "Шифр": "М-02"},
            {"Номенклатура": "Смазка Литол-24 (ведро 10кг)", "Кол-во": 2.0,
             "Ед.": "шт", "Наряд": "Н-1016", "Оборудование": "Мельница МШР 3.6х4.0",
             "Дата": "2026-10-05 10:05", "Статус": "Закрыт", "Шифр": "С-01"},
            {"Номенклатура": "Манжета армированная 50х70х10", "Кол-во": 4.0,
             "Ед.": "шт", "Наряд": "Н-1021", "Оборудование": "Насос песковый ГрАТ 1800",
             "Дата": "2026-10-06 07:40", "Статус": "Закрыт", "Шифр": "Г-02"},
            {"Номенклатура": "Ремень приводной клиновой SPC-4000", "Кол-во": 3.0,
             "Ед.": "шт", "Наряд": "Н-1024", "Оборудование": "Дробилка КМД-1750",
             "Дата": "2026-10-06 12:15", "Статус": "Закрыт", "Шифр": "М-01"},
        ]
    )


def load_demo_ledger(limit: int = 300) -> pd.DataFrame:
    """Демонстрационный журнал списаний ТМЦ из ``naryad_ai.db``.

    Возвращает таблицу с колонками ``Номенклатура``, ``Кол-во``, ``Ед.``,
    ``Наряд``, ``Оборудование``, ``Дата``, ``Статус``, ``Шифр``.
    При ошибке БД возвращается статический демо-набор.
    """
    try:
        limit_value = max(1, int(limit))
    except (TypeError, ValueError):
        limit_value = 300

    frame = _fetch_df(
        """
        SELECT
            mc.name                 AS "Номенклатура",
            mw.quantity             AS "Кол-во",
            mc.unit_measure         AS "Ед.",
            wo.order_num            AS "Наряд",
            COALESCE(e.name, '—')   AS "Оборудование",
            wo.created_at           AS "Дата",
            wo.status               AS "Статус",
            COALESCE(wo.fault_code, '—') AS "Шифр"
        FROM material_writeoffs AS mw
        JOIN materials_catalog  AS mc ON mc.id = mw.material_id
        JOIN work_orders        AS wo ON wo.id = mw.order_id
        LEFT JOIN equipment     AS e  ON e.id = wo.equipment_id
        ORDER BY wo.created_at DESC, mw.id DESC
        LIMIT ?
        """,
        (limit_value,),
    )
    if frame is None or frame.empty:
        return _fallback_demo_ledger()
    return frame


# ------------------------------------------------------------------
# Нормализация произвольной CSV-таблицы
# ------------------------------------------------------------------
#: Подсказки для распознавания смысловых колонок в «чужом» CSV.
_COLUMN_HINTS: Dict[str, Sequence[str]] = {
    "material": ("номенклатура", "материал", "запчаст", "наименование", "name", "material", "деталь", "тмц"),
    "quantity": ("кол", "qty", "quantity", "расход", "списан", "amount", "количество"),
    "date": ("дата", "date", "created", "время", "период", "month"),
    "status": ("статус", "status", "состояние"),
    "unit": ("ед", "unit", "изм"),
    "equipment": ("оборуд", "equipment", "узел", "агрегат", "станок"),
    "cost": ("цена", "стоим", "сумма", "price", "cost", "total", "тенге"),
    "order": ("наряд", "order", "заявк"),
}


def detect_columns(frame: pd.DataFrame) -> Dict[str, Optional[str]]:
    """Распознать смысловые колонки по названиям (материал, кол-во, дата...)."""
    mapping: Dict[str, Optional[str]] = {role: None for role in _COLUMN_HINTS}
    if frame is None or frame.empty:
        return mapping
    lowered = {column: str(column).strip().casefold() for column in frame.columns}
    for role, hints in _COLUMN_HINTS.items():
        for column, low in lowered.items():
            if any(hint in low for hint in hints):
                mapping[role] = column
                break
    return mapping


def normalize_ledger(frame: pd.DataFrame) -> pd.DataFrame:
    """Мягко нормализовать таблицу: типы, даты, пробелы. Без исключений."""
    try:
        if frame is None or frame.empty:
            return pd.DataFrame()
        result = frame.copy()
        result.columns = [str(column).strip() for column in result.columns]

        mapping = detect_columns(result)
        quantity_col = mapping.get("quantity")
        date_col = mapping.get("date")

        if quantity_col is not None:
            result[quantity_col] = pd.to_numeric(result[quantity_col], errors="coerce")

        if date_col is not None:
            parsed = pd.to_datetime(result[date_col], errors="coerce", dayfirst=False)
            # Заполняем только те значения, которые распознали (иначе оставляем строку).
            result[date_col] = [
                parsed.iloc[i] if pd.notna(parsed.iloc[i]) else result[date_col].iloc[i]
                for i in range(len(result))
            ]

        # Обрезаем пробелы в текстовых колонках (кроме распознанной даты).
        for column in result.columns:
            if column == date_col:
                continue
            if result[column].dtype == object:
                result[column] = result[column].map(
                    lambda value: value.strip() if isinstance(value, str) else value
                )
        return result
    except Exception as exc:  # noqa: BLE001 — «грязный» CSV не должен ронять аудит
        logger.exception("Ошибка нормализации журнала: %s", type(exc).__name__)
        return frame


# ------------------------------------------------------------------
# Интеллектуальный аудит
# ------------------------------------------------------------------
def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def audit_ledger(frame: pd.DataFrame) -> Dict[str, Any]:
    """Провести интеллектуальный аудит журнала списаний ТМЦ.

    Returns
    -------
    dict
        ``summary`` (текст заключения), ``anomalies`` (список словарей с
        ``material``, ``count``, ``quantity``, ``severity``, ``message``),
        ``recommendations`` (список строк), ``stats`` (сводные показатели),
        ``enriched`` (DataFrame с рекомендуемыми полями и флагом ИИ),
        ``columns`` (распознанные колонки).
    """
    result: Dict[str, Any] = {
        "summary": "",
        "anomalies": [],
        "recommendations": [],
        "stats": {},
        "enriched": pd.DataFrame(),
        "columns": {},
    }
    try:
        if frame is None or frame.empty:
            result["summary"] = "Таблица пуста — анализировать нечего."
            return result

        data = normalize_ledger(frame)
        mapping = detect_columns(data)
        result["columns"] = mapping

        material_col = mapping.get("material")
        quantity_col = mapping.get("quantity")
        date_col = mapping.get("date")
        equipment_col = mapping.get("equipment")

        if quantity_col is None:
            # Ищем первую числовую колонку как количество.
            for column in data.columns:
                if pd.api.types.is_numeric_dtype(data[column]):
                    quantity_col = column
                    break
        if quantity_col is None:
            data["Кол-во"] = 1.0
            quantity_col = "Кол-во"

        data[quantity_col] = pd.to_numeric(data[quantity_col], errors="coerce").fillna(0.0)

        rows = int(len(data))
        total_qty = float(data[quantity_col].sum())
        unique_materials = int(data[material_col].nunique()) if material_col else 0

        period_text = "—"
        if date_col is not None:
            parsed = pd.to_datetime(data[date_col], errors="coerce")
            valid = parsed.dropna()
            if not valid.empty:
                period_text = (
                    f"{valid.min().strftime('%d.%m.%Y')} — "
                    f"{valid.max().strftime('%d.%m.%Y')}"
                )

        anomalies: List[Dict[str, Any]] = []

        # 1) Аномалии по частоте списания конкретной номенклатуры.
        if material_col is not None:
            counts = data.groupby(material_col).size().sort_values(ascending=False)
            qty_by_material = data.groupby(material_col)[quantity_col].sum()
            mean_count = float(counts.mean()) if not counts.empty else 0.0
            std_count = float(counts.std(ddof=0)) if len(counts) > 1 else 0.0
            threshold = max(ANOMALY_MIN_COUNT, mean_count + 2 * std_count)

            for material, count in counts.items():
                count_int = int(count)
                if count_int < threshold:
                    continue
                quantity = _safe_float(qty_by_material.get(material, 0.0))
                severity = "high" if count_int >= threshold * 1.5 else "medium"
                anomalies.append(
                    {
                        "material": str(material),
                        "count": count_int,
                        "quantity": round(quantity, 2),
                        "severity": severity,
                        "message": (
                            f"«{material}» списана {count_int} раз (суммарно {quantity:g}). "
                            "Подозрение на брак партии, двойное списание или хищение."
                        ),
                    }
                )

            # 2) Всплеск расхода за короткое окно (последние N дней).
            if date_col is not None:
                parsed = pd.to_datetime(data[date_col], errors="coerce")
                recent_mask = parsed >= (datetime.now() - timedelta(days=RECENT_WINDOW_DAYS))
                if recent_mask.any():
                    recent_counts = (
                        data[recent_mask].groupby(material_col).size().sort_values(ascending=False)
                    )
                    for material, count in recent_counts.items():
                        count_int = int(count)
                        if count_int >= RECENT_SPIKE_THRESHOLD:
                            anomalies.append(
                                {
                                    "material": str(material),
                                    "count": count_int,
                                    "quantity": round(
                                        _safe_float(
                                            data[recent_mask]
                                            .groupby(material_col)[quantity_col]
                                            .sum()
                                            .get(material, 0.0)
                                        ),
                                        2,
                                    ),
                                    "severity": "high",
                                    "message": (
                                        f"«{material}»: {count_int} списаний за "
                                        f"{RECENT_WINDOW_DAYS} дня — аномальный всплеск расхода. "
                                        "Требуется немедленная проверка."
                                    ),
                                }
                            )

        # 3) Выбросы количества по всей таблице.
        quantity_series = data[quantity_col].dropna()
        if len(quantity_series) >= 5:
            median_qty = float(quantity_series.median())
            if median_qty > 0:
                outliers = data[data[quantity_col] > median_qty * QUANTITY_OUTLIER_FACTOR]
                if not outliers.empty and len(outliers) <= max(5, rows // 5):
                    for _, row in outliers.head(5).iterrows():
                        material = str(row.get(material_col, "позиция")) if material_col else "позиция"
                        qty = _safe_float(row[quantity_col])
                        anomalies.append(
                            {
                                "material": material,
                                "count": 1,
                                "quantity": round(qty, 2),
                                "severity": "medium",
                                "message": (
                                    f"«{material}»: разовое списание {qty:g} "
                                    f"превышает медиану ({median_qty:g}) более чем в "
                                    f"{QUANTITY_OUTLIER_FACTOR:g} раза — проверить обоснованность."
                                ),
                            }
                        )

        # 4) Обогащение: рекомендуемая норма, отклонение, флаг ИИ.
        enriched = data.copy()
        if material_col is not None:
            norms = data.groupby(material_col)[quantity_col].median()
            enriched["Рекомендуемая норма"] = enriched[material_col].map(norms).round(2)
        else:
            enriched["Рекомендуемая норма"] = round(float(quantity_series.median()), 2)
        enriched["Отклонение"] = (
            enriched[quantity_col] - enriched["Рекомендуемая норма"]
        ).round(2)

        def _flag(row: pd.Series) -> str:
            deviation = _safe_float(row.get("Отклонение"), 0.0)
            return "⚠️ Проверить" if deviation > 0 else "✅ Норма"

        enriched["Флаг ИИ"] = enriched.apply(_flag, axis=1)

        # 5) Рекомендации материальному учёту.
        recommendations: List[str] = []
        if anomalies:
            recommendations = [
                "Проверить проблемные партии ТМЦ у поставщика и оформить рекламацию при подтверждении брака.",
                "Сверить списания с актами выполненных работ и нарядами — исключить двойное списание.",
                "Провести выборочную инвентаризацию склада по позициям с аномалиями.",
                "Приостановить списание подозрительных позиций до выяснения причин.",
                "Усилить контроль выдачи ТМЦ и назначить ответственного за сверку.",
            ]
        else:
            recommendations = [
                "Аномалий не выявлено — расход ТМЦ распределён равномерно.",
                "Рекомендуется сохранять текущий регламент контроля списаний.",
            ]

        high_count = sum(1 for item in anomalies if item.get("severity") == "high")
        summary = (
            f"ИИ-учётчик проанализировал {rows} строк за период {period_text}. "
            f"Позиций номенклатуры: {unique_materials}. Суммарный расход: {total_qty:g}. "
            f"Обнаружено аномалий: {len(anomalies)} "
            f"(критичных: {high_count}). "
        )
        if anomalies:
            summary += "Рекомендуется немедленная сверка с материальным учётом ТОиР."
        else:
            summary += "Отклонений от нормы не зафиксировано."

        result.update(
            {
                "summary": summary,
                "anomalies": anomalies,
                "recommendations": recommendations,
                "enriched": enriched,
                "stats": {
                    "rows": rows,
                    "total_quantity": round(total_qty, 2),
                    "unique_materials": unique_materials,
                    "anomalies": len(anomalies),
                    "high_severity": high_count,
                    "period": period_text,
                },
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001 — аудит не должен ронять приложение
        logger.exception("Ошибка аудита журнала: %s", type(exc).__name__)
        result["summary"] = (
            "Не удалось завершить аудит: структура файла не распознана. "
            "Проверьте наличие колонок с номенклатурой и количеством."
        )
        result["recommendations"] = [
            "Приведите файл к виду: Номенклатура, Кол-во, Дата, Статус.",
        ]
        return result


# ------------------------------------------------------------------
# Выгрузка результата
# ------------------------------------------------------------------
def export_ledger_excel(
    frame: pd.DataFrame,
    audit: Optional[Dict[str, Any]] = None,
) -> bytes:
    """Выгрузить обработанную таблицу и заключение ИИ в Excel (.xlsx).

    При сбое движка Excel возвращается CSV (UTF-8 с BOM), чтобы результат
    не потерялся и приложение не упало.
    """
    data = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    audit = audit or {}
    try:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            data.to_excel(writer, index=False, sheet_name="Учёт ТМЦ")

            stats = audit.get("stats") or {}
            if stats:
                pd.DataFrame(
                    [{"Показатель": key, "Значение": value} for key, value in stats.items()]
                ).to_excel(writer, index=False, sheet_name="Сводка")

            anomalies = audit.get("anomalies") or []
            if anomalies:
                pd.DataFrame(
                    [
                        {
                            "Номенклатура": item.get("material"),
                            "Кол-во списаний": item.get("count"),
                            "Расход": item.get("quantity"),
                            "Критичность": item.get("severity"),
                            "Заключение": item.get("message"),
                        }
                        for item in anomalies
                    ]
                ).to_excel(writer, index=False, sheet_name="Аномалии ИИ")

            recommendations = audit.get("recommendations") or []
            conclusion_rows = [
                {"Раздел": "Заключение ИИ", "Содержание": audit.get("summary", "")}
            ] + [
                {"Раздел": f"Рекомендация {index + 1}", "Содержание": text}
                for index, text in enumerate(recommendations)
            ]
            pd.DataFrame(conclusion_rows).to_excel(
                writer, index=False, sheet_name="Заключение ИИ"
            )
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось собрать Excel журнала: %s", type(exc).__name__)
        try:
            return data.to_csv(index=False).encode("utf-8-sig")
        except Exception:  # noqa: BLE001
            return "Ошибка формирования выгрузки".encode("utf-8-sig")


def export_ledger_csv(frame: pd.DataFrame) -> bytes:
    """Выгрузить таблицу в CSV (UTF-8 с BOM)."""
    try:
        data = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        return data.to_csv(index=False).encode("utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось собрать CSV журнала: %s", type(exc).__name__)
        return "Ошибка формирования выгрузки".encode("utf-8-sig")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    demo = load_demo_ledger()
    print("Демо-строк:", len(demo))
    report = audit_ledger(demo)
    print(report["summary"])
    print("Аномалий:", len(report["anomalies"]))
    for anomaly in report["anomalies"][:5]:
        print(" -", anomaly["message"])
