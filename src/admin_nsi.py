# -*- coding: utf-8 -*-
"""Управление нормативно-справочной информацией (НСИ) «НарядAI».

Разделы 3 и 5.4 ТЗ АО «Костанайские минералы» (роль Администратора).

Модуль закрывает полный цикл работы со справочниками:

* оборудование (``name``, ``inv_number``, ``unit_id``, ``eq_type``, ``criticality``);
* сотрудники (ФИО, специальность, разряд, бригада, роль, смена, статус);
* шифры поломок (``code``, категория М/Э/Г/П/С, описание, норматив часов);
* материалы и складские остатки (``name``, ``unit_measure``, ``standard_stock``).

Возможности интерфейса :func:`render_admin_panel`:

* табличное редактирование через ``st.data_editor`` (правка, добавление,
  удаление строк);
* выгрузка любого справочника в CSV;
* загрузка CSV и импорт/обновление записей.

Все SQL-запросы обёрнуты в try-except. Функции не выбрасывают исключение
наружу: при ошибке возвращается безопасный результат с описанием проблемы.
"""

from __future__ import annotations

import io
import logging
import os
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)

#: Путь к БД SQLite в корне проекта (рядом с app.py).
DB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "naryad_ai.db")
)

CRITICALITY_OPTIONS = ["Низкая", "Средняя", "Высокая", "Критическая"]
EMPLOYEE_ROLES = ["Мастер", "Исполнитель", "Руководитель", "Администратор"]
EMPLOYEE_STATUSES = ["Свободен", "В работе", "В очереди", "Не на смене"]
FAULT_CATEGORY_OPTIONS = ["Механика", "Электрика", "Гидравлика", "Пневматика", "Смазка"]

#: Числовые колонки справочников (для приведения типов при записи).
_NUMERIC_COLUMNS = {
    "unit_id": int,
    "grade": int,
    "standard_stock": int,
    "normative_hours": float,
}

#: Безопасные значения по умолчанию для обязательных полей при вставке.
_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "equipment": {"eq_type": "Прочее", "criticality": "Средняя"},
    "employees": {
        "specialty": "Слесарь-ремонтник",
        "grade": 4,
        "team_name": "—",
        "role": "Исполнитель",
        "shift": "Смена 1",
        "status": "Свободен",
    },
    "fault_codes": {"category": "Механика", "normative_hours": 2.0, "description": "Описание не задано"},
    "materials_catalog": {"unit_measure": "шт", "standard_stock": 0},
}


# ==================================================================
# Подключение к БД и безопасные запросы
# ==================================================================
def _get_connection() -> sqlite3.Connection:
    """Соединение с ``naryad_ai.db`` с внешними ключами и таймаутом."""
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
    except Exception as exc:  # noqa: BLE001 — экран админа не должен падать
        logger.error("Ошибка чтения НСИ: %s", exc)
        return pd.DataFrame()
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения: %s", close_exc)


def _execute(sql: str, params: Sequence[Any] = ()) -> Tuple[bool, str, Optional[int]]:
    """Безопасный INSERT/UPDATE/DELETE → ``(ok, message, lastrowid)``."""
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        conn.commit()
        return True, "OK", cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        logger.warning("Нарушение целостности НСИ: %s", exc)
        return False, f"Нарушение целостности: {exc}", None
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка записи НСИ: %s", exc)
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                logger.debug("Откат транзакции НСИ не удался.")
        return False, f"{type(exc).__name__}: {exc}", None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения: %s", close_exc)


def ensure_schema_extensions() -> None:
    """Добавить недостающую колонку ``normative_hours`` в ``fault_codes``.

    ТЗ требует хранить норматив часов в справочнике шифров поломок, но
    исходная схема такой колонки не содержит. Расширение выполняется
    безопасно и идемпотентно.
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _get_connection()
        columns = [
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(fault_codes)").fetchall()
        ]
        if "normative_hours" not in columns:
            conn.execute(
                "ALTER TABLE fault_codes ADD COLUMN normative_hours REAL DEFAULT 2.0"
            )
            conn.commit()
            logger.info("Схема расширена: fault_codes.normative_hours добавлена.")
    except sqlite3.Error as exc:
        logger.error("Не удалось расширить схему fault_codes: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Непредвиденная ошибка расширения схемы: %s", type(exc).__name__)
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения: %s", close_exc)


# ==================================================================
# Описание справочников
# ==================================================================
_SPECS: Dict[str, Dict[str, Any]] = {
    "equipment": {
        "title_ru": "Оборудование",
        "title_kz": "Жабдық",
        "pk": "id",
        "select": """
            SELECT e.id AS id, e.name AS name, e.inv_number AS inv_number,
                   e.unit_id AS unit_id, u.name AS unit_name,
                   e.eq_type AS eq_type, e.criticality AS criticality
            FROM equipment AS e
            LEFT JOIN units AS u ON u.id = e.unit_id
            ORDER BY e.id
        """,
        "insert_cols": ["name", "inv_number", "unit_id", "eq_type", "criticality"],
        "update_cols": ["name", "inv_number", "unit_id", "eq_type", "criticality"],
        "disabled": ["id", "unit_name"],
        "numeric": ["unit_id"],
        "csv_cols": ["id", "name", "inv_number", "unit_id", "unit_name", "eq_type", "criticality"],
        "needs_unit": True,
    },
    "employees": {
        "title_ru": "Сотрудники",
        "title_kz": "Қызметкерлер",
        "pk": "id",
        "select": """
            SELECT id, fio, specialty, grade, team_name, role, shift, status
            FROM employees
            ORDER BY id
        """,
        "insert_cols": ["fio", "specialty", "grade", "team_name", "role", "shift", "status"],
        "update_cols": ["fio", "specialty", "grade", "team_name", "role", "shift", "status"],
        "disabled": ["id"],
        "numeric": ["grade"],
        "csv_cols": ["id", "fio", "specialty", "grade", "team_name", "role", "shift", "status"],
    },
    "fault_codes": {
        "title_ru": "Шифры поломок",
        "title_kz": "Ақау шифрлары",
        "pk": "code",
        "select": """
            SELECT code, category, description, normative_hours
            FROM fault_codes
            ORDER BY code
        """,
        "insert_cols": ["code", "category", "description", "normative_hours"],
        "update_cols": ["category", "description", "normative_hours"],
        "disabled": [],
        "numeric": ["normative_hours"],
        "csv_cols": ["code", "category", "description", "normative_hours"],
    },
    "materials_catalog": {
        "title_ru": "Материалы и остатки",
        "title_kz": "Материалдар мен қалдықтар",
        "pk": "id",
        "select": """
            SELECT id, name, unit_measure, standard_stock
            FROM materials_catalog
            ORDER BY id
        """,
        "insert_cols": ["name", "unit_measure", "standard_stock"],
        "update_cols": ["name", "unit_measure", "standard_stock"],
        "disabled": ["id"],
        "numeric": ["standard_stock"],
        "csv_cols": ["id", "name", "unit_measure", "standard_stock"],
    },
}


def _spec(table_key: str) -> Dict[str, Any]:
    return _SPECS[table_key]


# ==================================================================
# Чтение справочников
# ==================================================================
def get_units_df() -> pd.DataFrame:
    """Участки (для подсказок и выпадающих списков)."""
    return _fetch_df("SELECT id, name, description FROM units ORDER BY id")


def get_equipment_df() -> pd.DataFrame:
    """Оборудование с названием участка."""
    return _fetch_df(_SPECS["equipment"]["select"])


def get_employees_df() -> pd.DataFrame:
    """Сотрудники."""
    return _fetch_df(_SPECS["employees"]["select"])


def get_fault_codes_df() -> pd.DataFrame:
    """Шифры поломок с нормативом часов."""
    ensure_schema_extensions()
    return _fetch_df(_SPECS["fault_codes"]["select"])


def get_materials_df() -> pd.DataFrame:
    """Материалы и складские остатки."""
    return _fetch_df(_SPECS["materials_catalog"]["select"])


def get_dictionary_df(table_key: str) -> pd.DataFrame:
    """Универсальное чтение справочника по ключу."""
    if table_key not in _SPECS:
        return pd.DataFrame()
    if table_key == "fault_codes":
        ensure_schema_extensions()
    return _fetch_df(_spec(table_key)["select"])


# ==================================================================
# Приведение типов и запись строк
# ==================================================================
def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        return False
    return str(value).strip() == ""


def _coerce(table_key: str, column: str, value: Any) -> Any:
    """Привести значение колонки к типу БД."""
    if _is_blank(value):
        return None
    if column in _NUMERIC_COLUMNS:
        try:
            return _NUMERIC_COLUMNS[column](float(value))
        except (TypeError, ValueError):
            return None
    return str(value).strip()


def _prepare_row(table_key: str, data: Dict[str, Any], for_insert: bool = False) -> Dict[str, Any]:
    """Собрать валидный набор колонок для записи.

    При ``for_insert=True`` незаполненные обязательные поля получают
    безопасные значения по умолчанию, чтобы добавление строки из
    ``st.data_editor`` не падало на NOT NULL-ограничениях схемы.
    """
    spec = _spec(table_key)
    prepared: Dict[str, Any] = {}
    for column in spec["insert_cols"]:
        if column in data:
            prepared[column] = _coerce(table_key, column, data.get(column))

    # Для оборудования: если unit_id не задан, но есть название участка.
    if table_key == "equipment" and prepared.get("unit_id") is None:
        unit_name = str(data.get("unit_name") or "").strip()
        if unit_name:
            units = _fetch_df("SELECT id FROM units WHERE name = ? LIMIT 1", (unit_name,))
            if not units.empty:
                prepared["unit_id"] = int(units.iloc[0]["id"])

    if for_insert:
        for column, default in _DEFAULTS.get(table_key, {}).items():
            if prepared.get(column) is None:
                prepared[column] = _coerce(table_key, column, default)
        # Оборудование без участка привязываем к первому доступному.
        if table_key == "equipment" and prepared.get("unit_id") is None:
            units = _fetch_df("SELECT id FROM units ORDER BY id LIMIT 1")
            if not units.empty:
                prepared["unit_id"] = int(units.iloc[0]["id"])
        if table_key == "equipment" and prepared.get("criticality") not in CRITICALITY_OPTIONS:
            prepared["criticality"] = "Средняя"
    return prepared


def _insert_row(table_key: str, data: Dict[str, Any]) -> Tuple[bool, str]:
    """Вставить строку справочника."""
    spec = _spec(table_key)
    prepared = _prepare_row(table_key, data, for_insert=True)
    columns = [c for c in spec["insert_cols"] if c in prepared and prepared[c] is not None]
    if not columns:
        return False, "Нет заполненных полей для вставки."
    if table_key == "employees" and prepared.get("role") not in EMPLOYEE_ROLES:
        return False, "Недопустимая роль сотрудника."
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table_key} ({', '.join(columns)}) VALUES ({placeholders})"
    ok, message, _ = _execute(sql, [prepared[c] for c in columns])
    return ok, message


def _update_row(table_key: str, pk_value: Any, data: Dict[str, Any]) -> Tuple[bool, str]:
    """Обновить строку справочника по первичному ключу."""
    spec = _spec(table_key)
    prepared = _prepare_row(table_key, data)
    columns = [c for c in spec["update_cols"] if c in prepared]
    if not columns:
        return False, "Нет полей для обновления."
    assignments = ", ".join(f"{c} = ?" for c in columns)
    sql = f"UPDATE {table_key} SET {assignments} WHERE {spec['pk']} = ?"
    params = [prepared[c] for c in columns] + [pk_value]
    ok, message, _ = _execute(sql, params)
    return ok, message


def _delete_row(table_key: str, pk_value: Any) -> Tuple[bool, str]:
    """Удалить строку справочника по первичному ключу."""
    spec = _spec(table_key)
    sql = f"DELETE FROM {table_key} WHERE {spec['pk']} = ?"
    ok, message, _ = _execute(sql, [pk_value])
    return ok, message


def upsert_row(table_key: str, data: Dict[str, Any]) -> Tuple[bool, str]:
    """Вставить или обновить строку справочника (публичная функция).

    Если первичный ключ присутствует и непустой — выполняется UPDATE,
    иначе INSERT.
    """
    if table_key not in _SPECS:
        return False, "Неизвестный справочник."
    spec = _spec(table_key)
    pk_value = data.get(spec["pk"])
    if not _is_blank(pk_value):
        if spec["pk"] == "id":
            try:
                pk_value = int(float(pk_value))
            except (TypeError, ValueError):
                return False, "Некорректный идентификатор."
        if _row_exists(table_key, pk_value):
            return _update_row(table_key, pk_value, data)
        # Ключ задан, но строки нет — это вставка (например, новый код шифра из CSV).
        return _insert_row(table_key, data)
    return _insert_row(table_key, data)


# ==================================================================
# Применение изменений st.data_editor
# ==================================================================
def _norm_pk(pk_column: str, value: Any) -> Any:
    if pk_column == "id":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return value
    return str(value).strip()


def _row_exists(table_key: str, pk_value: Any) -> bool:
    """Проверить наличие строки справочника по первичному ключу."""
    spec = _spec(table_key)
    rows = _fetch_df(
        f"SELECT 1 AS x FROM {table_key} WHERE {spec['pk']} = ? LIMIT 1",
        (pk_value,),
    )
    return not rows.empty


def _row_has_data(table_key: str, data: Dict[str, Any]) -> bool:
    """True, если в строке есть хотя бы одно заполненное поле справочника."""
    for column in _spec(table_key)["insert_cols"]:
        if not _is_blank(data.get(column)):
            return True
    return False


def _changed(column: str, old: Any, new: Any) -> bool:
    """Сравнить старое и новое значение колонки с учётом типов и NaN."""
    if column in _NUMERIC_COLUMNS:
        try:
            old_norm = float(old) if not _is_blank(old) else None
        except (TypeError, ValueError):
            old_norm = None
        try:
            new_norm = float(new) if not _is_blank(new) else None
        except (TypeError, ValueError):
            new_norm = None
        return old_norm != new_norm
    old_norm = "" if _is_blank(old) else str(old).strip()
    new_norm = "" if _is_blank(new) else str(new).strip()
    return old_norm != new_norm


def apply_editor_changes(
    table_key: str,
    original: pd.DataFrame,
    edited: pd.DataFrame,
) -> Tuple[bool, str]:
    """Синхронизировать изменения ``st.data_editor`` с БД.

    Обновляет изменённые строки, добавляет новые и удаляет убранные.
    Возвращает ``(ok, отчёт)``.
    """
    if table_key not in _SPECS:
        return False, "Неизвестный справочник."
    spec = _spec(table_key)
    pk_column = spec["pk"]

    original_pks: Dict[Any, pd.Series] = {}
    if not original.empty and pk_column in original.columns:
        for _, row in original.iterrows():
            value = row.get(pk_column)
            if not _is_blank(value):
                original_pks[_norm_pk(pk_column, value)] = row

    inserted = 0
    updated = 0
    deleted = 0
    errors: List[str] = []
    seen: set = set()

    if edited is not None and not edited.empty:
        for _, row in edited.iterrows():
            row_dict = {col: row.get(col) for col in edited.columns}
            pk_value = row.get(pk_column)
            if _is_blank(pk_value):
                if not _row_has_data(table_key, row_dict):
                    continue
                ok, message = _insert_row(table_key, row_dict)
                if ok:
                    inserted += 1
                else:
                    errors.append(f"Добавление: {message}")
                continue

            key = _norm_pk(pk_column, pk_value)
            seen.add(key)
            if key not in original_pks:
                ok, message = _insert_row(table_key, row_dict)
                if ok:
                    inserted += 1
                else:
                    errors.append(f"Добавление {key}: {message}")
                continue

            original_row = original_pks[key]
            changed = any(
                _changed(column, original_row.get(column), row.get(column))
                for column in spec["update_cols"]
                if column in edited.columns
            )
            if changed:
                ok, message = _update_row(table_key, key, row_dict)
                if ok:
                    updated += 1
                else:
                    errors.append(f"Обновление {key}: {message}")

    for key in original_pks:
        if key not in seen:
            ok, message = _delete_row(table_key, key)
            if ok:
                deleted += 1
            else:
                errors.append(f"Удаление {key}: {message}")

    report = f"Добавлено: {inserted} · обновлено: {updated} · удалено: {deleted}"
    if errors:
        report += " | Ошибки: " + "; ".join(errors[:5])
        return False, report
    return True, report


# ==================================================================
# CSV: выгрузка и загрузка
# ==================================================================
def export_table_csv(table_key: str) -> bytes:
    """Выгрузить справочник в CSV (UTF-8 с BOM)."""
    spec = _spec(table_key)
    frame = get_dictionary_df(table_key)
    if frame.empty:
        frame = pd.DataFrame(columns=spec["csv_cols"])
    else:
        ordered = [c for c in spec["csv_cols"] if c in frame.columns]
        frame = frame[ordered]
    try:
        return frame.to_csv(index=False).encode("utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка выгрузки CSV: %s", exc)
        return "Ошибка выгрузки".encode("utf-8-sig")


def import_table_csv(table_key: str, data: Any) -> Tuple[bool, str]:
    """Импортировать/обновить справочник из CSV-байтов.

    Строки с существующим первичным ключом обновляются, остальные
    добавляются. Для оборудования поддерживается колонка ``unit_name``.
    """
    if table_key not in _SPECS:
        return False, "Неизвестный справочник."
    try:
        raw = bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)
        frame = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 — некорректный CSV не должен ронять экран
        return False, f"Не удалось прочитать CSV: {type(exc).__name__}: {exc}"

    if frame.empty:
        return False, "CSV не содержит строк."

    inserted = 0
    updated = 0
    errors: List[str] = []
    for _, row in frame.iterrows():
        record = {col: row.get(col) for col in frame.columns}
        pk_value = record.get(_spec(table_key)["pk"])
        existed = False
        if not _is_blank(pk_value):
            existed = _row_exists(
                table_key, _norm_pk(_spec(table_key)["pk"], pk_value)
            )
        ok, message = upsert_row(table_key, record)
        if not ok:
            errors.append(message)
            continue
        if existed:
            updated += 1
        else:
            inserted += 1

    report = f"Импорт: добавлено {inserted}, обновлено {updated}."
    if errors:
        report += " | Ошибки: " + "; ".join(errors[:5])
        return False, report
    return True, report


# ==================================================================
# Streamlit-интерфейс администратора НСИ
# ==================================================================
_LABELS: Dict[str, Dict[str, str]] = {
    "RU": {
        "title": "🗂 Администратор НСИ — управление справочниками",
        "caption": (
            "Правка оборудования, сотрудников, шифров поломок и материалов. "
            "Таблицы редактируются на месте; доступны выгрузка и загрузка CSV."
        ),
        "rows": "Записей",
        "save": "💾 Сохранить изменения",
        "saved": "Изменения применены",
        "save_fail": "Изменения применены с ошибками",
        "export": "📥 Скачать CSV",
        "import_title": "📤 Загрузка CSV",
        "upload": "Выберите CSV-файл",
        "import_btn": "Импортировать в справочник",
        "import_done": "Импорт завершён",
        "units_ref": "Участки (справочник выше)",
        "empty": "Справочник пуст или БД недоступна.",
        "hint_faults": (
            "Первичный ключ — код шифра. Категории: М/Э/Г/П/С "
            "(Механика/Электрика/Гидравлика/Пневматика/Смазка)."
        ),
    },
    "KZ": {
        "title": "🗂 НСИ әкімшісі — анықтамалықтарды басқару",
        "caption": (
            "Жабдықты, қызметкерлерді, ақау шифрларын және материалдарды өңдеу. "
            "Кестелер орнында өңделеді; CSV жүктеу мен жүктеу қолжетімді."
        ),
        "rows": "Жазбалар",
        "save": "💾 Өзгерістерді сақтау",
        "saved": "Өзгерістер қолданылды",
        "save_fail": "Өзгерістер қателермен қолданылды",
        "export": "📥 CSV жүктеу",
        "import_title": "📤 CSV жүктеу",
        "upload": "CSV файлын таңдаңыз",
        "import_btn": "Анықтамалыққа импорттау",
        "import_done": "Импорт аяқталды",
        "units_ref": "Учаскелер (жоғарыдағы анықтамалық)",
        "empty": "Анықтамалық бос немесе ДБ қолжетімсіз.",
        "hint_faults": (
            "Бастапқы кілт — ақау коды. Санаттар: М/Э/Г/П/С "
            "(Механика/Электрика/Гидравлика/Пневматика/Смазка)."
        ),
    },
}


def _label(lang: str, key: str) -> str:
    return _LABELS.get(lang, _LABELS["RU"]).get(key, key)


def _stretch(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Вызвать виджет Streamlit на всю ширину с обратной совместимостью API."""
    try:
        return func(*args, width="stretch", **kwargs)
    except TypeError:
        kwargs.pop("width", None)
        return func(*args, use_container_width=True, **kwargs)


def _column_config(table_key: str) -> Dict[str, Any]:
    """Конфигурация колонок ``st.data_editor`` для справочника."""
    if table_key == "equipment":
        return {
            "name": st.column_config.TextColumn("Наименование"),
            "inv_number": st.column_config.TextColumn("Инв. номер"),
            "unit_id": st.column_config.NumberColumn("ID участка", format="%d"),
            "unit_name": st.column_config.TextColumn("Участок"),
            "eq_type": st.column_config.TextColumn("Тип"),
            "criticality": st.column_config.SelectboxColumn(
                "Критичность", options=CRITICALITY_OPTIONS
            ),
        }
    if table_key == "employees":
        return {
            "fio": st.column_config.TextColumn("ФИО"),
            "specialty": st.column_config.TextColumn("Специальность"),
            "grade": st.column_config.NumberColumn("Разряд", format="%d"),
            "team_name": st.column_config.TextColumn("Бригада"),
            "role": st.column_config.SelectboxColumn("Роль", options=EMPLOYEE_ROLES),
            "shift": st.column_config.TextColumn("Смена"),
            "status": st.column_config.SelectboxColumn("Статус", options=EMPLOYEE_STATUSES),
        }
    if table_key == "fault_codes":
        return {
            "code": st.column_config.TextColumn("Код"),
            "category": st.column_config.SelectboxColumn(
                "Категория", options=FAULT_CATEGORY_OPTIONS
            ),
            "description": st.column_config.TextColumn("Описание"),
            "normative_hours": st.column_config.NumberColumn("Норматив, ч", format="%.1f"),
        }
    return {
        "name": st.column_config.TextColumn("Наименование"),
        "unit_measure": st.column_config.TextColumn("Ед. изм."),
        "standard_stock": st.column_config.NumberColumn("Норматив запаса", format="%d"),
    }


def _render_dictionary_tab(table_key: str, lang: str) -> None:
    """Отрисовать одну вкладку справочника: редактор, сохранение и CSV."""
    spec = _spec(table_key)
    original = get_dictionary_df(table_key)
    if table_key == "equipment":
        st.caption(
            "Участки: "
            + ", ".join(
                f"{int(row['id'])}={row['name']}"
                for _, row in get_units_df().iterrows()
            )
        )
    if table_key == "fault_codes":
        st.caption(_label(lang, "hint_faults"))

    if original.empty:
        st.info(_label(lang, "empty"))
        original = pd.DataFrame(columns=spec["csv_cols"])

    edited = _stretch(
        st.data_editor,
        original,
        key=f"admin_editor_{table_key}",
        num_rows="dynamic",
        column_config=_column_config(table_key),
        disabled=spec["disabled"],
        height=380,
    )

    action_col, count_col = st.columns([2, 3])
    with action_col:
        if st.button(_label(lang, "save"), key=f"admin_save_{table_key}", type="primary"):
            ok, report = apply_editor_changes(table_key, original, edited)
            if ok:
                st.success(f"{_label(lang, 'saved')}. {report}")
            else:
                st.error(f"{_label(lang, 'save_fail')}. {report}")
            st.rerun()
    with count_col:
        st.caption(f"{_label(lang, 'rows')}: {len(original)}")

    st.divider()
    csv_col, upload_col = st.columns(2)
    with csv_col:
        st.download_button(
            _label(lang, "export"),
            data=export_table_csv(table_key),
            file_name=f"{table_key}.csv",
            mime="text/csv",
            key=f"admin_export_{table_key}",
        )
    with upload_col:
        with st.expander(_label(lang, "import_title"), expanded=False):
            uploaded = st.file_uploader(
                _label(lang, "upload"),
                type=["csv"],
                key=f"admin_upload_{table_key}",
            )
            if st.button(_label(lang, "import_btn"), key=f"admin_import_{table_key}"):
                if uploaded is None:
                    st.warning(_label(lang, "upload"))
                else:
                    ok, report = import_table_csv(table_key, uploaded.read())
                    if ok:
                        st.success(f"{_label(lang, 'import_done')}. {report}")
                    else:
                        st.error(report)
                    st.rerun()


def render_admin_panel(lang: str = "RU") -> None:
    """Полноценная панель Администратора НСИ для Streamlit.

    Parameters
    ----------
    lang:
        Язык интерфейса (``RU`` / ``KZ``).
    """
    ensure_schema_extensions()
    st.header(_label(lang, "title"))
    st.caption(_label(lang, "caption"))

    tab_keys = ["equipment", "employees", "fault_codes", "materials_catalog"]
    titles = [
        f"🛠 {_spec(key)['title_ru']}" for key in tab_keys
    ]
    tabs = st.tabs(titles)
    for tab, key in zip(tabs, tab_keys):
        with tab:
            _render_dictionary_tab(key, lang)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ensure_schema_extensions()
    print("Оборудование:", len(get_equipment_df()))
    print("Сотрудники:", len(get_employees_df()))
    print("Шифры поломок:", len(get_fault_codes_df()))
    print("Материалы:", len(get_materials_df()))
