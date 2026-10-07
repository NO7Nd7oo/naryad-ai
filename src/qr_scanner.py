# -*- coding: utf-8 -*-
"""Быстрое QR-сканирование оборудования для системы «НарядAI».

Раздел 10 ТЗ АО «Костанайские минералы».

Мастер выбирает агрегат одним из двух способов:

* обычный каскад «участок → оборудование»;
* сканирование QR-кода / шильдика — ввод инвентарного номера или выбор
  симуляции вида ``QR: INV-DR-001 [Конвейер К-3]``.

При распознавании шильдика форма сразу подставляет участок и станок и
показывает мастеру последние 3 инцидента по этому узлу.

Точка входа: :func:`render_qr_selector`.
"""

import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

import streamlit as st

try:
    from src.database.schema import get_connection
except ImportError:  # запуск как скрипт из каталога src/
    from database.schema import get_connection

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Контракт возврата и подписи интерфейса
# ------------------------------------------------------------------

#: (unit_id, unit_name, equipment_id, equipment_name). None — агрегат не выбран.
EquipmentSelection = Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]

_EMPTY_SELECTION: EquipmentSelection = (None, None, None, None)

MODE_LIST = "Обычный выбор из списка"
MODE_QR = "Сканирование QR-кода / шильдика агрегата"

_QR_PLACEHOLDER = "— выберите шильдик или отсканируйте код —"
_INCIDENT_LIMIT = 3

#: «QR: INV-DR-001 [Конвейер К-3]»
_QR_LABEL_RE = re.compile(
    r"QR:\s*(?P<inv>[A-Za-z0-9][A-Za-z0-9\-]*)\s*\[(?P<name>[^\]]+)\]",
    re.IGNORECASE,
)
_INV_RE = re.compile(r"INV-[A-Za-z0-9\-]+", re.IGNORECASE)

_KEY_MODE = "naryad_qr_input_mode"
_KEY_SIM = "naryad_qr_sim"
_KEY_INV = "naryad_qr_inv"
_KEY_UNIT = "naryad_qr_unit_widget"
_KEY_EQUIPMENT = "naryad_qr_eq_widget"
_KEY_FILLED_UNIT = "naryad_qr_filled_unit"
_KEY_FILLED_EQ = "naryad_qr_filled_eq"


# ------------------------------------------------------------------
# Разбор шильдика
# ------------------------------------------------------------------

def _qr_label(inv_number: str, equipment_name: str) -> str:
    """Собрать строку симуляции QR строго в формате ТЗ."""
    return f"QR: {inv_number} [{equipment_name}]"


def _extract_lookup_keys(payload: str) -> Dict[str, Optional[str]]:
    """Достать инвентарный номер и имя агрегата из ввода мастера.

    Принимает полную строку симуляции, голый инвентарный номер
    (``INV-DR-001``) или точное наименование оборудования.
    """
    text = (payload or "").strip()
    if not text:
        return {"inv": None, "name": None}

    qr_match = _QR_LABEL_RE.search(text)
    if qr_match is not None:
        return {
            "inv": qr_match.group("inv"),
            "name": qr_match.group("name").strip() or None,
        }

    inv_match = _INV_RE.search(text)
    if inv_match is not None:
        return {"inv": inv_match.group(0), "name": None}

    return {"inv": None, "name": text}


def _resolve_equipment(
    payload: str,
    catalog: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Найти агрегат в уже загруженном справочнике.

    Сначала точное совпадение инвентарного номера (без учёта регистра),
    затем точное имя. Неоднозначное имя не угадывается.
    """
    keys = _extract_lookup_keys(payload)
    inv_number = keys["inv"]
    if inv_number:
        inv_norm = inv_number.upper()
        for item in catalog:
            if str(item["inv_number"]).strip().upper() == inv_norm:
                return item

    equipment_name = keys["name"]
    if equipment_name:
        name_norm = equipment_name.casefold()
        matches = [
            item
            for item in catalog
            if str(item["equipment_name"]).strip().casefold() == name_norm
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _equipment_label(item: Dict[str, Any]) -> str:
    """Подпись станка в списке: имя и инвентарный номер."""
    return f"{item['equipment_name']} ({item['inv_number']})"


def _selection_of(item: Dict[str, Any]) -> EquipmentSelection:
    """Собрать контрактный кортеж из строки справочника."""
    return (
        int(item["unit_id"]),
        str(item["unit_name"]),
        int(item["equipment_id"]),
        str(item["equipment_name"]),
    )


# ------------------------------------------------------------------
# Доступ к БД (только через src/database/schema.py)
# ------------------------------------------------------------------

def _fetchall(
    sql: str,
    params: Sequence[Any] = (),
) -> Optional[List[Dict[str, Any]]]:
    """Выполнить запрос и вернуть список словарей.

    ``None`` означает ошибку БД (её уже залогировали). Пустой список —
    успешный запрос без строк.
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = get_connection()
        conn.execute("PRAGMA busy_timeout = 5000;")
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        logger.error("Ошибка запроса к naryad_ai.db: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — соединение/путь/драйвер не должны ронять UI
        logger.exception("Непредвиденная ошибка чтения справочника оборудования: %s", exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения: %s", close_exc)


def _load_catalog() -> Optional[List[Dict[str, Any]]]:
    """Участки и оборудование одним соединением."""
    return _fetchall(
        """
        SELECT
            e.id            AS equipment_id,
            e.name          AS equipment_name,
            e.inv_number    AS inv_number,
            e.eq_type       AS eq_type,
            e.criticality   AS criticality,
            u.id            AS unit_id,
            u.name          AS unit_name
        FROM equipment AS e
        JOIN units AS u ON u.id = e.unit_id
        ORDER BY u.name, e.name
        """
    )


def _load_recent_incidents(
    equipment_id: int,
    limit: int = _INCIDENT_LIMIT,
) -> Optional[List[Dict[str, Any]]]:
    """Последние инциденты (наряды) по узлу — подсказка мастеру."""
    return _fetchall(
        """
        SELECT
            wo.order_num     AS order_num,
            wo.created_at    AS created_at,
            wo.priority      AS priority,
            wo.status        AS status,
            wo.description   AS description,
            wo.order_type    AS order_type,
            wo.fault_code    AS fault_code,
            wo.downtime_hours AS downtime_hours,
            COALESCE(fc.description, '') AS fault_desc
        FROM work_orders AS wo
        LEFT JOIN fault_codes AS fc ON fc.code = wo.fault_code
        WHERE wo.equipment_id = ?
        ORDER BY wo.created_at DESC, wo.id DESC
        LIMIT ?
        """,
        (equipment_id, limit),
    )


# ------------------------------------------------------------------
# Мелкие помощники интерфейса
# ------------------------------------------------------------------

def _ensure_widget_value(key: str, options: Sequence[str]) -> None:
    """Подогнать session_state под допустимые варианты до создания виджета."""
    if not options:
        return
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0]


def _sync_list_form(item: Dict[str, Any]) -> None:
    """Предзаполнить виджеты обычного выбора — они в этом прогоне не создаются."""
    st.session_state[_KEY_UNIT] = str(item["unit_name"])
    st.session_state[_KEY_EQUIPMENT] = _equipment_label(item)


def _shorten(text: str, limit: int = 160) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _repeated_fault_hint(incidents: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Если в последних инцидентах повторяется шифр — подсказать мастеру."""
    codes = [str(item["fault_code"]) for item in incidents if item.get("fault_code")]
    if len(codes) < 2:
        return None
    top_code = max(set(codes), key=codes.count)
    if codes.count(top_code) < 2:
        return None
    fault_desc = next(
        (
            str(item["fault_desc"])
            for item in incidents
            if item.get("fault_code") == top_code and item.get("fault_desc")
        ),
        "",
    )
    detail = f" ({fault_desc})" if fault_desc else ""
    return (
        f"Повторяющийся шифр в последних инцидентах: {top_code}{detail}. "
        "Имеет смысл проверить этот узел до выдачи наряда."
    )


def _render_prefilled_form(item: Dict[str, Any]) -> None:
    """Показать участок и станок уже подставленными в форму."""
    st.success(
        f"Шильдик распознан: {item['inv_number']}. "
        "Участок и агрегат подставлены в форму автоматически."
    )
    # Ключи задаём до виджетов, чтобы следующее сканирование обновляло поля.
    st.session_state[_KEY_FILLED_UNIT] = str(item["unit_name"])
    st.session_state[_KEY_FILLED_EQ] = str(item["equipment_name"])
    unit_col, equipment_col = st.columns(2)
    unit_col.text_input("Участок", key=_KEY_FILLED_UNIT, disabled=True)
    equipment_col.text_input("Станок / агрегат", key=_KEY_FILLED_EQ, disabled=True)
    st.caption(
        f"Инв. № {item['inv_number']} · {item.get('eq_type') or '—'} · "
        f"критичность: {item.get('criticality') or '—'}"
    )


def _render_incident_hint(
    equipment_name: str,
    incidents: Optional[List[Dict[str, Any]]],
) -> None:
    """История последних инцидентов по отсканированному узлу."""
    st.markdown(f"**Подсказка мастеру — последние инциденты: {equipment_name}**")
    if incidents is None:
        st.warning("Историю инцидентов по узлу загрузить не удалось. Выбор агрегата сохранён.")
        return
    if not incidents:
        st.info("По этому узлу инцидентов в журнале нарядов пока нет.")
        return

    for index, incident in enumerate(incidents, start=1):
        fault_code = incident.get("fault_code") or "—"
        fault_desc = incident.get("fault_desc") or ""
        fault_part = f"{fault_code} ({fault_desc})" if fault_desc else str(fault_code)
        downtime = incident.get("downtime_hours")
        downtime_part = ""
        if downtime is not None:
            downtime_part = f" · простой {float(downtime):.1f} ч"
        description = _shorten(str(incident.get("description") or ""))
        st.markdown(
            f"{index}. **{incident.get('order_num') or '—'}** · "
            f"{incident.get('created_at') or '—'} · "
            f"{incident.get('order_type') or '—'} · "
            f"{incident.get('priority') or '—'} · "
            f"{fault_part} · "
            f"{incident.get('status') or '—'}"
            f"{downtime_part}  \n"
            f"{description}"
        )

    repeated = _repeated_fault_hint(incidents)
    if repeated:
        st.warning(repeated)


# ------------------------------------------------------------------
# Режимы ввода
# ------------------------------------------------------------------

def _render_list_mode(catalog: Sequence[Dict[str, Any]]) -> EquipmentSelection:
    """Каскад: участок, затем оборудование этого участка."""
    unit_names = list(dict.fromkeys(str(item["unit_name"]) for item in catalog))
    if not unit_names:
        st.warning("В справочнике нет участков.")
        return _EMPTY_SELECTION

    _ensure_widget_value(_KEY_UNIT, unit_names)
    unit_name = st.selectbox("Участок", unit_names, key=_KEY_UNIT)

    equipment = [item for item in catalog if str(item["unit_name"]) == unit_name]
    equipment_labels = [_equipment_label(item) for item in equipment]
    if not equipment_labels:
        st.warning(f"На участке «{unit_name}» нет оборудования.")
        return _EMPTY_SELECTION

    _ensure_widget_value(_KEY_EQUIPMENT, equipment_labels)
    equipment_label = st.selectbox("Оборудование", equipment_labels, key=_KEY_EQUIPMENT)
    selected = equipment[equipment_labels.index(equipment_label)]
    return _selection_of(selected)


def _render_qr_mode(catalog: Sequence[Dict[str, Any]]) -> EquipmentSelection:
    """Симуляция сканера и ручной ввод инвентарного номера."""
    labels = [_qr_label(str(item["inv_number"]), str(item["equipment_name"])) for item in catalog]
    options = [_QR_PLACEHOLDER, *labels]

    scan_col, inv_col = st.columns(2)
    with scan_col:
        _ensure_widget_value(_KEY_SIM, options)
        qr_choice = st.selectbox(
            "Симуляция QR-кода",
            options,
            key=_KEY_SIM,
            help="Пример шильдика: QR: INV-DR-001 [Конвейер К-3]",
        )
    with inv_col:
        inv_typed = st.text_input(
            "Инвентарный номер с шильдика",
            key=_KEY_INV,
            placeholder="INV-DR-001",
            help="Можно вставить и полную строку QR: INV-DR-001 [Конвейер К-3]",
        )

    typed_payload = inv_typed.strip()
    if typed_payload:
        payload = typed_payload
        st.caption("Используется введённый инвентарный номер / строка шильдика.")
    elif qr_choice != _QR_PLACEHOLDER:
        payload = qr_choice
        st.code(qr_choice, language=None)
    else:
        st.caption("Отсканируйте QR шильдика или введите инвентарный номер агрегата.")
        return _EMPTY_SELECTION

    resolved = _resolve_equipment(payload, catalog)
    if resolved is None:
        st.error(f"Агрегат по коду «{payload}» не найден в реестре ТОиР.")
        return _EMPTY_SELECTION

    # Виджеты списка в этом прогоне не рисуются — их session_state можно заполнить.
    _sync_list_form(resolved)
    _render_prefilled_form(resolved)

    incidents = _load_recent_incidents(int(resolved["equipment_id"]))
    _render_incident_hint(str(resolved["equipment_name"]), incidents)
    return _selection_of(resolved)


def render_qr_selector() -> EquipmentSelection:
    """Нарисовать выбор оборудования и сразу вернуть выбранный агрегат.

    Два способа ввода (переключатель на форме):

    * обычный список: участок → оборудование;
    * QR / шильдик: выбор симуляции ``QR: INV-DR-001 [Конвейер К-3]``
      или ввод инвентарного номера. Участок и станок подставляются
      автоматически, ниже показываются последние 3 инцидента по узлу.

    Подключение к ``naryad_ai.db`` идёт через
    :func:`src.database.schema.get_connection`. Отдельная кнопка
    подтверждения не нужна: кортеж возвращается в том же прогоне Streamlit,
    в котором мастер выбрал агрегат.

    Returns
    -------
    tuple
        ``(unit_id, unit_name, equipment_id, equipment_name)``.

        Если агрегат ещё не выбран или база недоступна, все четыре
        элемента равны ``None``. Функция не выбрасывает исключение наружу.
    """
    st.markdown("**Оборудование**")
    mode = st.radio(
        "Способ ввода оборудования",
        [MODE_LIST, MODE_QR],
        horizontal=True,
        key=_KEY_MODE,
    )

    try:
        catalog = _load_catalog()
        if catalog is None:
            st.error("Не удалось прочитать справочник оборудования. Проверьте базу naryad_ai.db.")
            return _EMPTY_SELECTION
        if not catalog:
            st.warning("Справочник оборудования пуст.")
            return _EMPTY_SELECTION

        if mode == MODE_QR:
            return _render_qr_mode(catalog)
        return _render_list_mode(catalog)
    except Exception as exc:  # noqa: BLE001 — виджет не должен ронять пульт мастера
        logger.exception("Ошибка модуля QR-сканирования: %s", exc)
        st.error("Не удалось открыть выбор оборудования. Повторите действие.")
        return _EMPTY_SELECTION


def _running_inside_streamlit() -> bool:
    """True, если модуль исполняется командой ``streamlit run``."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Контекст Streamlit недоступен: %s", exc)
        return False


if __name__ == "__main__":
    if not _running_inside_streamlit():
        print("Запуск демо: streamlit run src/qr_scanner.py")
    else:
        st.set_page_config(
            page_title="QR-сканер НарядAI",
            page_icon="📷",
            layout="centered",
        )
        st.title("НарядAI — сканирование шильдика")
        st.caption("Раздел 10 ТЗ · АО «Костанайские минералы»")
        unit_id, unit_name, equipment_id, equipment_name = render_qr_selector()
        st.divider()
        if equipment_id is None:
            st.info("Агрегат ещё не выбран.")
        else:
            st.write(
                "Выбрано:",
                {
                    "unit_id": unit_id,
                    "unit_name": unit_name,
                    "equipment_id": equipment_id,
                    "equipment_name": equipment_name,
                },
            )
