# -*- coding: utf-8 -*-
"""AI-ассистент мастера «НарядAI» и выгрузка сводного журнала смены.

Модуль закрывает Разделы 6 и 10 ТЗ АО «Костанайские минералы»:

* :func:`ask_master_copilot` — оперативный AI-ассистент мастера смены.
  В боевом режиме отвечает через Google Gemini (``GEMINI_API_KEY``),
  без ключа и без сети — выдаёт детерминированные инженерные подсказки
  по живым данным SQLite ``naryad_ai.db``.
* :func:`export_shift_excel` — формирует сводный журнал смены в формате
  Excel (.xlsx) средствами ``pandas`` + ``openpyxl``.

Проектная защита: ни одна функция не выбрасывает исключение наружу.
При любой ошибке БД, отсутствии ключа или сбое сети возвращается
безопасный структурированный результат.
"""

from __future__ import annotations

import io
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Путь к БД SQLite в корне проекта (рядом с app.py).
DB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "naryad_ai.db")
)

#: Статусы, при которых наряд считается незавершённым.
ACTIVE_STATUSES = (
    "Выдан",
    "Принят в работу",
    "В очереди",
    "В работе",
    "Приостановлен",
    "На доработку",
    "Проверка ИИ",
)

#: Статусы, при которых наряд считается закрытым.
CLOSED_STATUSES = ("Закрыт", "Исполнено")

#: Заголовок смены в выгрузке Excel.
SHIFT_TITLE = "Сводный журнал смены ТОиР · АО «Костанайские минералы»"

#: Готовые подсказки-вопросы для интерфейса мастера.
DEFAULT_SUGGESTIONS: List[str] = [
    "Какие наряды просрочены и что делать в первую очередь?",
    "Кто из исполнителей свободен прямо сейчас?",
    "Что с риском аварии на конвейере К-3?",
    "Какие материалы списываются чаще всего?",
]


# ------------------------------------------------------------------
# Подключение к БД и снимок состояния смены
# ------------------------------------------------------------------
def _get_connection() -> sqlite3.Connection:
    """Соединение с БД с внешними ключами и таймаутом ожидания."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    return conn


def _fetchall(sql: str, params: Sequence[Any] = ()) -> Optional[List[Dict[str, Any]]]:
    """Запрос к БД. ``None`` — ошибка БД, список — результат (возможно пустой)."""
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _get_connection()
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        logger.error("Ошибка чтения naryad_ai.db в copilot: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — путь/драйвер не должны ронять пульт
        logger.exception("Непредвиденная ошибка БД в copilot: %s", type(exc).__name__)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                logger.debug("Ошибка закрытия соединения copilot: %s", close_exc)


def _count(sql: str, params: Sequence[Any] = ()) -> int:
    """Безопасный одиночный счётчик."""
    rows = _fetchall(sql, params)
    if not rows:
        return 0
    try:
        return int(next(iter(rows[0].values())))
    except (StopIteration, TypeError, ValueError):
        return 0


def _active_placeholders() -> str:
    return ", ".join("?" for _ in ACTIVE_STATUSES)


def _context_snapshot() -> Dict[str, Any]:
    """Снимок оперативного состояния смены для ответа и для Gemini."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    snapshot: Dict[str, Any] = {
        "total_orders": 0,
        "active_orders": 0,
        "closed_orders": 0,
        "overdue_orders": 0,
        "free_workers": [],
        "busy_workers": [],
        "overdue_list": [],
        "top_equipment": [],
        "generated_at": now_str,
    }

    try:
        snapshot["total_orders"] = _count("SELECT COUNT(*) AS n FROM work_orders")
        snapshot["active_orders"] = _count(
            f"SELECT COUNT(*) AS n FROM work_orders WHERE status IN ({_active_placeholders()})",
            ACTIVE_STATUSES,
        )
        closed_ph = ", ".join("?" for _ in CLOSED_STATUSES)
        snapshot["closed_orders"] = _count(
            f"SELECT COUNT(*) AS n FROM work_orders WHERE status IN ({closed_ph})",
            CLOSED_STATUSES,
        )
        snapshot["overdue_orders"] = _count(
            f"""
            SELECT COUNT(*) AS n FROM work_orders
            WHERE status = 'Просрочен'
               OR (status IN ({_active_placeholders()}) AND deadline < ?)
            """,
            (*ACTIVE_STATUSES, now_str),
        )

        workers = _fetchall(
            """
            SELECT e.fio AS fio, e.specialty AS specialty, e.status AS status,
                   SUM(CASE WHEN wo.id IS NOT NULL THEN 1 ELSE 0 END) AS active_cnt
            FROM employees AS e
            LEFT JOIN work_orders AS wo
                   ON wo.assignee_id = e.id
                  AND wo.status IN ({placeholders})
            WHERE e.role = 'Исполнитель'
            GROUP BY e.id
            ORDER BY active_cnt ASC, e.fio ASC
            """.format(placeholders=_active_placeholders()),
            ACTIVE_STATUSES,
        )
        if workers is not None:
            free = [
                f"{w['fio']} ({w['specialty']})"
                for w in workers
                if int(w.get("active_cnt") or 0) == 0
            ]
            busy = [
                f"{w['fio']} ({w['specialty']})"
                for w in workers
                if int(w.get("active_cnt") or 0) > 0
            ]
            snapshot["free_workers"] = free
            snapshot["busy_workers"] = busy

        overdue_rows = _fetchall(
            f"""
            SELECT wo.order_num AS order_num, wo.priority AS priority,
                   wo.deadline AS deadline, e.name AS equipment
            FROM work_orders AS wo
            LEFT JOIN equipment AS e ON e.id = wo.equipment_id
            WHERE wo.status = 'Просрочен'
               OR (wo.status IN ({_active_placeholders()}) AND wo.deadline < ?)
            ORDER BY wo.deadline ASC
            LIMIT 10
            """,
            (*ACTIVE_STATUSES, now_str),
        )
        if overdue_rows:
            snapshot["overdue_list"] = [
                f"№{r['order_num']} · {r.get('equipment') or '—'} · "
                f"приоритет {r.get('priority') or '—'} · дедлайн {r.get('deadline') or '—'}"
                for r in overdue_rows
            ]

        top_rows = _fetchall(
            """
            SELECT e.name AS equipment, COUNT(*) AS n
            FROM work_orders AS wo
            JOIN equipment AS e ON e.id = wo.equipment_id
            GROUP BY e.id
            ORDER BY n DESC
            LIMIT 5
            """
        )
        if top_rows:
            snapshot["top_equipment"] = [
                f"{r['equipment']} — {r['n']} нарядов" for r in top_rows
            ]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка снимка состояния в copilot: %s", type(exc).__name__)

    return snapshot


# ------------------------------------------------------------------
# Боевой ответ через Gemini
# ------------------------------------------------------------------
def _ask_gemini(question: str, snapshot: Dict[str, Any]) -> Optional[str]:
    """Ответ Gemini по текущему состоянию смены. ``None`` — недоступно."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        import google.generativeai as genai
    except ImportError:
        logger.error("Пакет google-generativeai не установлен, copilot работает офлайн.")
        return None

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")

        context_lines = [
            f"Всего нарядов в системе: {snapshot.get('total_orders')}",
            f"Активных нарядов: {snapshot.get('active_orders')}",
            f"Просрочено: {snapshot.get('overdue_orders')}",
            f"Закрыто: {snapshot.get('closed_orders')}",
            f"Свободных исполнителей: {len(snapshot.get('free_workers') or [])}",
            "Свободны: " + (", ".join(snapshot.get("free_workers") or []) or "нет данных"),
            "Просроченные наряды: "
            + ("; ".join(snapshot.get("overdue_list") or []) or "нет"),
            "Проблемные узлы: "
            + ("; ".join(snapshot.get("top_equipment") or []) or "нет данных"),
            "Заложенная аномалия модели: конвейер К-3, шифр М-02, риск 87.4% на 7 смен",
        ]
        prompt = (
            "Ты — опытный мастер по техническому обслуживанию и ремонту (ТОиР) "
            "АО «Костанайские минералы». Отвечай кратко, по делу, на русском языке, "
            "используя только приведённые оперативные данные. Дай конкретные "
            "рекомендации и при необходимости расставь приоритеты.\n\n"
            "ОПЕРАТИВНАЯ ОБСТАНОВКА СМЕНЫ:\n"
            + "\n".join(context_lines)
            + f"\n\nВОПРОС МАСТЕРА: {question}\n\nОТВЕТ:"
        )
        response = model.generate_content(prompt, request_options={"timeout": 20})
        text = (getattr(response, "text", "") or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 — сеть/квота не должны ронять пульт
        logger.error("Gemini copilot недоступен: %s", type(exc).__name__)
        return None


# ------------------------------------------------------------------
# Детерминированные инженерные подсказки (офлайн-режим)
# ------------------------------------------------------------------
def _heuristic_answer(question: str, snapshot: Dict[str, Any]) -> str:
    """Ответ по ключевым словам на основе снимка состояния смены."""
    q = question.casefold()
    free = snapshot.get("free_workers") or []
    busy = snapshot.get("busy_workers") or []
    overdue = snapshot.get("overdue_list") or []
    top = snapshot.get("top_equipment") or []

    if any(word in q for word in ("просроч", "дедлайн", "срок", "горит")):
        if overdue:
            return (
                "⚠️ Требуют немедленного внимания просроченные наряды:\n\n- "
                + "\n- ".join(overdue)
                + "\n\nРекомендация: перераспределить свободных исполнителей "
                "и эскалировать аварийные наряды в первую очередь."
            )
        return "✅ Просроченных нарядов нет. Все активные наряды идут в рамках дедлайнов."

    if any(word in q for word in ("свобод", "исполнител", "кто может", "рабоч")):
        if free:
            return (
                f"🟢 Свободны прямо сейчас ({len(free)} чел.): {', '.join(free[:8])}."
                + (f"\n\n🔴 Заняты ({len(busy)}): {', '.join(busy[:8])}." if busy else "")
            )
        return "Все исполнители распределены по нарядам. Рекомендуется подключать резерв бригады."

    if any(word in q for word in ("к-3", "к3", "подшип", "м-02", "м02", "радар", "прогноз", "риск")):
        return (
            "🧠 Предиктивный радар: конвейер К-3 (инв. INV-DR-001), шифр отказа М-02 "
            "(перегрев и заклинивание роликоподшипника вала). Вероятность аварийного "
            "останова в ближайшие 7 смен — 87.4%.\n\n"
            "Рекомендация: включить узел в первоочередной план ППР, проверить соосность "
            "вала привода и вибрацию, при отклонении — превентивно заменить подшипник 22318."
        )

    if any(word in q for word in ("материал", "тмц", "списан", "запас", "подшипник")):
        materials = _fetchall(
            """
            SELECT mc.name AS name, mc.standard_stock AS stock
            FROM materials_catalog AS mc
            ORDER BY mc.standard_stock ASC
            LIMIT 5
            """
        )
        if materials:
            lines = [
                f"{m['name']} — норматив запаса {m['stock']} {''}"
                for m in materials
            ]
            return (
                "📦 Наименьший нормативный запас ТМЦ:\n\n- "
                + "\n- ".join(lines)
                + "\n\nРекомендация: проверить фактический склад и дозаказать позиции "
                "перед плановыми ремонтами."
            )
        return "Каталог ТМЦ недоступен. Проверьте базу naryad_ai.db."

    if any(word in q for word in ("приоритет", "что сначала", "план", "очередь")):
        return (
            "🎯 Порядок работы на смену:\n\n"
            "1. Аварийные наряды и эскалации (не приняты более 3 минут).\n"
            "2. Просроченные наряды с истёкшим дедлайном.\n"
            "3. Нарады «В работе» с дедлайном менее 30 минут.\n"
            "4. Плановые ППР и работы по предиктивному радару (К-3 / М-02)."
        )

    if any(word in q for word in ("отчёт", "отчет", "закрыт", "верификац", "ии", "проверка")):
        return (
            "🤖 Закрытие наряда проходит AI-верификацию «НарядAI»: модуль оценивает "
            "соответствие выполненных работ описанию неисправности и логику списания ТМЦ. "
            "Итог — оценка 0–100 и вердикт. При оценке ≥ 70 наряд закрывается, иначе "
            "уходит на доработку. Прикрепите фото «После» для повышения достоверности."
        )

    return (
        "📋 Оперативная сводка смены:\n\n"
        f"- Всего нарядов: {snapshot.get('total_orders')}\n"
        f"- Активных: {snapshot.get('active_orders')}\n"
        f"- Просрочено: {snapshot.get('overdue_orders')}\n"
        f"- Закрыто: {snapshot.get('closed_orders')}\n"
        f"- Свободных исполнителей: {len(free)}\n\n"
        "Уточните вопрос: просрочки, свободные рабочие, риск по К-3, списание ТМЦ "
        "или порядок приоритетов."
    )


# ------------------------------------------------------------------
# Публичная точка входа №1: AI-ассистент мастера
# ------------------------------------------------------------------
def ask_master_copilot(
    question: str,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ответить мастеру на оперативный вопрос по данным смены.

    Parameters
    ----------
    question:
        Текст вопроса мастера (может быть пустым).
    context:
        Необязательный словарь с дополнительными данными, которые нужно
        учесть (например, выбранный наряд). Дополняет снимок БД.

    Returns
    -------
    dict
        ``{"answer": str, "source": "gemini"|"heuristic"|"system",
        "suggestions": list[str], "snapshot": dict}``.

        Исключение наружу не выбрасывается.
    """
    try:
        text = (question or "").strip()
        snapshot = _context_snapshot()
        if context:
            try:
                snapshot.update(context)
            except Exception:  # noqa: BLE001
                logger.debug("Некорректный context, проигнорирован.")

        if not text:
            return {
                "answer": (
                    "Задайте вопрос по смене: просрочки, свободные исполнители, "
                    "риск оборудования, списание ТМЦ или приоритеты работ."
                ),
                "source": "system",
                "suggestions": list(DEFAULT_SUGGESTIONS),
                "snapshot": snapshot,
            }

        gemini_answer = _ask_gemini(text, snapshot)
        if gemini_answer:
            return {
                "answer": gemini_answer,
                "source": "gemini",
                "suggestions": list(DEFAULT_SUGGESTIONS),
                "snapshot": snapshot,
            }

        return {
            "answer": _heuristic_answer(text, snapshot),
            "source": "heuristic",
            "suggestions": list(DEFAULT_SUGGESTIONS),
            "snapshot": snapshot,
        }
    except Exception as exc:  # noqa: BLE001 — ассистент не должен ронять смену
        logger.exception("Ошибка AI-ассистента мастера: %s", type(exc).__name__)
        return {
            "answer": (
                "AI-ассистент временно недоступен. Проверьте базу naryad_ai.db "
                "и повторите запрос."
            ),
            "source": "system",
            "suggestions": list(DEFAULT_SUGGESTIONS),
            "snapshot": {},
        }


# ------------------------------------------------------------------
# Публичная точка входа №2: выгрузка журнала смены в Excel
# ------------------------------------------------------------------
def _to_dataframe(rows: Any):
    """Привести входные данные к DataFrame без импорта pandas на уровне модуля."""
    import pandas as pd  # локальный импорт: Excel нужен только тут

    if rows is None:
        return pd.DataFrame()
    if isinstance(rows, pd.DataFrame):
        return rows.copy()
    if isinstance(rows, (list, tuple)):
        return pd.DataFrame(list(rows))
    if isinstance(rows, dict):
        return pd.DataFrame(list(rows.values()))
    try:
        return pd.DataFrame(rows)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def export_shift_excel(
    rows: Any,
    summary: Optional[Dict[str, Any]] = None,
    sheet_name: str = "Журнал смены",
) -> bytes:
    """Собрать сводный журнал смены в формате .xlsx и вернуть байты.

    Parameters
    ----------
    rows:
        Список словарей, список записей или ``pandas.DataFrame`` с нарядами.
    summary:
        Необязательная сводка ``{"Показатель": "Значение"}`` — попадёт на
        отдельный лист «Сводка».
    sheet_name:
        Имя листа с журналом.

    Returns
    -------
    bytes
        Содержимое .xlsx. При сбое движка Excel возвращается CSV в кодировке
        UTF-8 (с BOM), чтобы выгрузка не пропала и приложение не упало.
    """
    import pandas as pd

    df = _to_dataframe(rows)

    buffer = io.BytesIO()
    try:
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            if df.empty:
                pd.DataFrame(
                    [{"Информация": "За выбранный период нарядов не найдено"}]
                ).to_excel(writer, index=False, sheet_name=sheet_name)
            else:
                df.to_excel(writer, index=False, sheet_name=sheet_name)

            summary_data = dict(summary or {})
            if not summary_data and not df.empty:
                status_col = next(
                    (c for c in df.columns if str(c).lower() in ("статус", "status")),
                    None,
                )
                if status_col is not None:
                    summary_data = {
                        str(k): int(v)
                        for k, v in df[status_col].value_counts().items()
                    }
            if summary_data:
                pd.DataFrame(
                    [{"Показатель": k, "Значение": v} for k, v in summary_data.items()]
                ).to_excel(writer, index=False, sheet_name="Сводка")
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 — выгрузка не должна ронять интерфейс
        logger.exception("Не удалось собрать Excel: %s", type(exc).__name__)
        try:
            return df.to_csv(index=False).encode("utf-8-sig")
        except Exception:  # noqa: BLE001
            return "Ошибка формирования выгрузки".encode("utf-8-sig")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    demo = ask_master_copilot("Какие наряды просрочены?")
    print(demo["source"])
    print(demo["answer"])
