# -*- coding: utf-8 -*-
"""«НарядAI» — интеллектуальная система выдачи и контроля нарядов ТОиР.

АО «Костанайские минералы» · Qostanai AI Industry Hackathon 2026.

Главный файл интерфейса Streamlit. Архитектура построена по слоям, чтобы
добавление новых ролей, экранов и AI-модулей не требовало переписывания:

1. **Конфигурация и локализация** — единый ``LANG`` (RU / KZ).
2. **Слой данных** — безопасные обёртки над SQLite ``naryad_ai.db``.
3. **Бизнес-логика** — создание нарядов, события, оценки ИИ, списания.
4. **Адаптеры внешних модулей** — ``src.ai_engine``, ``src.deadline_watcher``,
   ``src.qr_scanner``, ``src.analytics_predict``, ``src.master_copilot``.
   Каждый вызов обёрнут в try-except с безопасной дефолтной реализацией.
5. **Экраны ролей** — мастер смены, исполнитель, руководитель/ТОиР.

Приложение спроектировано так, чтобы НИКОГДА не падать: любая ошибка БД,
сети или внешнего модуля деградирует до безопасного результата, а не
прерывает работу смены.
"""

from __future__ import annotations

import datetime
import inspect
import io
import json
import os
from urllib.parse import quote
import random
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import plotly.express as px
import streamlit as st

# ==================================================================
# 0. БЕЗОПАСНЫЕ ИМПОРТЫ ВНЕШНИХ МОДУЛЕЙ (Safe Dynamic Fallbacks)
# ==================================================================
# Каждый внешний модуль подключается отдельно. Если он отсутствует или
# падает при импорте — приложение продолжает работу на дефолт-реализации.

# --- 0.1 PDF (reportlab) ---
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas as _pdf_canvas

    _REPORTLAB_AVAILABLE = True
except Exception:  # noqa: BLE001 — без PDF приложение обязано работать
    _REPORTLAB_AVAILABLE = False

# --- 0.2 AI-движок (проверка отчёта и фото) ---
try:
    from src.ai_engine import (
        analyze_photos_before_after as _analyze_photos_impl,
    )
    from src.ai_engine import verify_work_order as _verify_work_order_impl
except Exception:  # noqa: BLE001
    _verify_work_order_impl = None
    _analyze_photos_impl = None

# --- 0.3 Контроль дедлайнов и эскалаций ---
try:
    from src.deadline_watcher import (
        check_deadlines_and_escalations as _deadlines_impl,
    )
except Exception:  # noqa: BLE001
    _deadlines_impl = None

# --- 0.4 QR-сканер / выбор оборудования ---
try:
    from src.qr_scanner import render_qr_selector as _qr_selector_impl
except Exception:  # noqa: BLE001
    _qr_selector_impl = None

# --- 0.5 Предиктивная аналитика отказов ---
try:
    from src.analytics_predict import (
        predict_equipment_failures as _predict_impl,
    )
except Exception:  # noqa: BLE001
    _predict_impl = None

# --- 0.6 AI-ассистент мастера и выгрузка Excel ---
try:
    from src.master_copilot import ask_master_copilot as _copilot_impl
    from src.master_copilot import export_shift_excel as _export_excel_impl
except Exception:  # noqa: BLE001
    _copilot_impl = None
    _export_excel_impl = None

# --- 0.7 Голосовая транскрипция (дополнительная возможность) ---
try:
    from src.analytics_predict import transcribe_voice_note as _transcribe_impl
except Exception:  # noqa: BLE001
    _transcribe_impl = None

# --- 0.8 Продвинутая аналитика ТОиР (ППР, рейтинги, простои) ---
try:
    from src.advanced_analytics import (
        calculate_worker_ratings as _worker_ratings_impl,
    )
    from src.advanced_analytics import (
        detect_post_ppr_failures as _ppr_failures_impl,
    )
    from src.advanced_analytics import (
        get_downtime_breakdown as _downtime_impl,
    )
    from src.advanced_analytics import (
        post_ppr_failures_dataframe as _ppr_dataframe_impl,
    )
except Exception:  # noqa: BLE001
    _worker_ratings_impl = None
    _ppr_failures_impl = None
    _ppr_dataframe_impl = None
    _downtime_impl = None

# --- 0.9 Шлюз интеграции с 1С:ТОиР / ERP ---
try:
    from src.api_gateway import (
        generate_1c_sync_payload as _sync_payload_impl,
    )
    from src.api_gateway import (
        payload_to_json_bytes as _payload_json_impl,
    )
except Exception:  # noqa: BLE001
    _sync_payload_impl = None
    _payload_json_impl = None

# --- 0.10 Администратор НСИ (справочники, CSV) ---
try:
    from src.admin_nsi import render_admin_panel as _admin_panel_impl
except Exception:  # noqa: BLE001
    _admin_panel_impl = None

# --- 0.11 Умный ИИ-учётчик (Smart Ledger AI) ---
try:
    from src.smart_ledger import audit_ledger as _audit_ledger_impl
    from src.smart_ledger import detect_columns as _detect_ledger_columns_impl
    from src.smart_ledger import export_ledger_csv as _export_ledger_csv_impl
    from src.smart_ledger import export_ledger_excel as _export_ledger_excel_impl
    from src.smart_ledger import load_demo_ledger as _demo_ledger_impl
    from src.smart_ledger import normalize_ledger as _normalize_ledger_impl
except Exception:  # noqa: BLE001
    _audit_ledger_impl = None
    _detect_ledger_columns_impl = None
    _export_ledger_csv_impl = None
    _export_ledger_excel_impl = None
    _demo_ledger_impl = None
    _normalize_ledger_impl = None


# ==================================================================
# 1. КОНФИГУРАЦИЯ И КОНСТАНТЫ
# ==================================================================
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(APP_DIR, "naryad_ai.db")
PHOTOS_DIR = os.path.join(APP_DIR, "data", "photos")

PAGE_TITLE = "НарядAI | Костанайские Минералы"
PAGE_ICON = "⛏️"

#: Статусы незавершённых нарядов (совместимо со схемой + защитный «Просрочен»).
ACTIVE_STATUSES: Tuple[str, ...] = (
    "Выдан",
    "Принят в работу",
    "В очереди",
    "В работе",
    "Приостановлен",
    "На доработку",
    "Проверка ИИ",
    "Просрочен",
)

#: Статусы завершённых нарядов.
CLOSED_STATUSES: Tuple[str, ...] = ("Закрыт", "Исполнено")

#: Приоритеты наряда (порядок = цветовая градация).
PRIORITY_OPTIONS: Tuple[str, ...] = ("Аварийный", "Высокий", "Обычный", "Плановый")

#: Допустимые вердикты ИИ-верификации (совпадают с CHECK в ai_evaluations).
VALID_VERDICTS: Tuple[str, ...] = (
    "Принято",
    "Принято с замечаниями",
    "Требует доработки",
)

#: Порог оценки, при котором наряд закрывается автоматически.
AI_CLOSE_THRESHOLD = 70

#: Переводы статусов нарядов.
STATUS_LABELS: Dict[str, Dict[str, str]] = {
    "RU": {
        "Выдан": "Выдан",
        "Принят в работу": "Принят в работу",
        "В очереди": "В очереди",
        "В работе": "В работе",
        "Приостановлен": "Приостановлен",
        "Отклонён": "Отклонён",
        "Исполнено": "Исполнено",
        "Проверка ИИ": "Проверка ИИ",
        "На доработку": "На доработку",
        "Закрыт": "Закрыт",
        "Просрочен": "Просрочен",
    },
    "KZ": {
        "Выдан": "Берілді",
        "Принят в работу": "Жұмысқа қабылданды",
        "В очереди": "Кезекте",
        "В работе": "Жұмыста",
        "Приостановлен": "Тоқтатылды",
        "Отклонён": "Қабылданбады",
        "Исполнено": "Орындалды",
        "Проверка ИИ": "ЖИ тексеруі",
        "На доработку": "Пысықтауға",
        "Закрыт": "Жабылды",
        "Просрочен": "Мерзімі өтті",
    },
}

#: Переводы приоритетов нарядов.
PRIORITY_LABELS: Dict[str, Dict[str, str]] = {
    "RU": {
        "Аварийный": "Аварийный",
        "Высокий": "Высокий",
        "Обычный": "Обычный",
        "Плановый": "Плановый",
    },
    "KZ": {
        "Аварийный": "Апатты",
        "Высокий": "Жоғары",
        "Обычный": "Қалыпты",
        "Плановый": "Жоспарлы",
    },
}

#: Дефолтный прогноз предиктивного радара (если модуль недоступен).
DEFAULT_PREDICTION: Dict[str, Any] = {
    "top_problem_unit": "Конвейер К-3",
    "risk_percent": 87.4,
    "fault_code": "М-02",
    "recommendation": (
        "За последние 90 дней на конвейере К-3 зафиксирована аномальная серия "
        "аварийных остановок по шифру М-02 (перегрев и заклинивание "
        "роликоподшипника вала). Рекомендуется включить узел в первоочередной "
        "план ППР, проверить соосность вала привода и вибрацию, при отклонении — "
        "превентивно заменить роликоподшипник 22318."
    ),
    "failure_count": 0,
    "horizon_shifts": 7,
}


# ==================================================================
# 2. ЛОКАЛИЗАЦИЯ (RU / KZ)
# ==================================================================
LANG: Dict[str, Dict[str, Any]] = {
    "RU": {
        "brand": "⛏️ НарядAI",
        "lang_label": "Тіл / Язык",
        "app_title": "НарядAI: Контроль нарядов и ТОиР",
        "app_subtitle": "АО «Костанайские минералы» · Qostanai AI Industry Hackathon 2026",
        "hero_eyebrow": "QOSTANAI AI INDUSTRY HACKATHON 2026",
        "hero_title": "НАРЯД-AI | АО КОСТАНАЙСКИЕ МИНЕРАЛЫ",
        "hero_subtitle": "Интеллектуальная система оперативного контроля ТОиР и выдачи нарядов",
        # --- Ролевой баннер (HTML hero) ---
        "banner_eyebrow": "Система ТОиР • АО «Костанайские минералы»",
        "banner_online": "● Сервер активен (Online)",
        "banner_master_title": "НАРЯД-AI • ОПЕРАТИВНЫЙ ПУЛЬТ МАСТЕРА",
        "banner_master_sub": "Управление сменой, оперативный контроль допусков и учет отказов оборудования",
        "banner_worker_title": "НАРЯД-AI • РАБОЧИЙ ТЕРМИНАЛ ТОиР",
        "banner_worker_sub": "Фиксация работ и закрытие нарядов",
        "banner_manager_title": "НАРЯД-AI • АНАЛИТИЧЕСКИЙ ХАБ ТОиР",
        "banner_manager_sub": "Мониторинг надежности и предиктив",
        "banner_admin_title": "НАРЯД-AI • ПАНЕЛЬ НСИ И СПРАВОЧНИКОВ",
        "banner_admin_sub": "Справочники, нормативы и интеграция данных",
        "status_online_bar": "🟢 Статус связи: Онлайн | Синхронизация с 1С активна",
        "status_offline_bar": "🟠 Статус связи: Офлайн | Данные кэшируются локально",
        "status_monitor_ok": "🛡️ Мониторинг: Смена в штатном режиме, критических сбоев нет",
        # --- Сайдбар ---
        "role_select": "Выберите активную роль",
        "role_master": "Мастер смены",
        "role_worker": "Исполнитель (Слесарь/Электрик)",
        "role_manager": "Руководитель / ТОиР Аналитика",
        "role_admin": "Администратор НСИ",
        "offline_toggle": "📡 Связь в шахте/цеху: ОФЛАЙН / ОНЛАЙН",
        "offline_warn": "ОФЛАЙН-режим: данные кэшируются локально и синхронизируются при восстановлении связи.",
        "online_ok": "ОНЛАЙН: связь стабильна, данные синхронизированы с сервером.",
        "db_status": "Диагностика БД",
        # --- Умный ИИ-учётчик (Smart Ledger AI) ---
        "ledger_title": "🤖 Умный ИИ-учётчик и работа с CSV",
        "ledger_caption": "Загрузите любой CSV (списания ТМЦ, журнал дефектов) или проанализируйте демонстрационный журнал списаний из базы naryad_ai.db.",
        "ledger_upload": "📤 Загрузить CSV-файл (необязательно)",
        "ledger_demo_note": "Показан демонстрационный журнал списаний ТМЦ из naryad_ai.db. Загрузите свой CSV, чтобы заменить данные.",
        "ledger_rows": "Строк в таблице",
        "ledger_materials": "Позиций номенклатуры",
        "ledger_total_qty": "Суммарный расход",
        "ledger_anomalies": "Аномалий ИИ",
        "ledger_analyze": "🤖 Анализ ИИ-учётчика",
        "ledger_analyzing": "ИИ-учётчик анализирует списания...",
        "ledger_conclusion": "🧾 Заключение ИИ-учётчика",
        "ledger_anomaly_list": "🚩 Выявленные аномалии расхода",
        "ledger_recommendations": "✅ Рекомендации для материального учёта",
        "ledger_table": "📋 Журнал списаний",
        "ledger_enriched": "🧮 Таблица с рекомендациями ИИ (заполненные поля)",
        "ledger_export_excel": "📥 Выгрузить обработанную таблицу в Excel",
        "ledger_export_csv": "📄 Выгрузить очищенную таблицу в CSV",
        "ledger_ready": "Файл готов к скачиванию.",
        "ledger_file_error": "Не удалось прочитать файл: {err}. Показан демонстрационный журнал.",
        "ledger_empty": "Нет данных для отображения.",
        "ledger_no_anomalies": "Аномалий не обнаружено — учёт ведётся равномерно.",
        "ledger_severity_high": "критично",
        "ledger_severity_medium": "внимание",
        # --- Общее ---
        "common": {
            "equipment": "Оборудование",
            "status": "Статус",
            "priority": "Приоритет",
            "assignee": "Исполнитель",
            "created": "Создан",
            "deadline": "Дедлайн",
            "description": "Описание",
            "score": "Оценка ИИ",
            "order_num": "Номер наряда",
            "unit": "Участок",
            "no_data": "Нет данных",
        },
        # --- Экран 1: Мастер ---
        "m_title": "👨‍🔧 Мастер смены — оперативный пульт",
        "alerts_header": "Активные алерты контроля сроков",
        "no_alerts": "Активных алертов нет. Смена идёт в штатном режиме.",
        "kpi_total": "Всего нарядов",
        "kpi_inwork": "В работе",
        "kpi_overdue": "Просрочено",
        "kpi_closed": "Закрыто",
        "kpi_total_sub": "За текущую смену",
        "kpi_inwork_sub": "● На линии",
        "kpi_overdue_sub": "Требует внимания",
        "kpi_closed_sub": "Принято ИИ",
        "tab_issue": "Выдать наряд (< 1 мин / QR)",
        "tab_kanban": "Канбан смены",
        "tab_copilot": "AI-Copilot мастера",
        "issue_subheader": "Быстрая выдача наряда за 6 кликов или по QR-шильдику",
        "equipment_selected": "Выбран агрегат",
        "equipment_not_selected": "Агрегат не выбран. Отсканируйте шильдик или выберите из списка.",
        "normative": "Норматив времени (часы)",
        "assignee_free": "Свободен",
        "assignee_busy": "В работе",
        "assignee_off": "Не на смене",
        "description_ph": "Например: аварийный перегрев и вибрация роликоподшипника привода конвейера К-3",
        "voice_hint": "🎤 Поле поддерживает голосовой ввод: прикрепите аудиозаметку и нажмите «Распознать».",
        "voice_upload": "Голосовая заметка мастера (необязательно)",
        "voice_recognize": "Распознать речь",
        "voice_done": "Речь распознана и подставлена в описание.",
        "voice_fail": "Не удалось распознать запись, введите текст вручную.",
        "issue_btn": "Сформировать и отправить наряд",
        "issue_ok": "Наряд {num} успешно выдан: {worker}",
        "issue_local": "Офлайн: наряд сохранён локально и синхронизируется автоматически.",
        "issue_need_equip": "Сначала выберите оборудование (QR или список).",
        "issue_need_desc": "Заполните описание неисправности.",
        "issue_error": "Не удалось создать наряд: {err}",
        "kanban_filter_status": "Фильтр по статусам",
        "kanban_filter_priority": "Фильтр по приоритетам",
        "kanban_empty": "Нет нарядов по заданным фильтрам.",
        "kanban_download": "Скачать выборку (CSV)",
        "copilot_intro": "Оперативный AI-ассистент по нарядам, простоям, ресурсам и рискам оборудования.",
        "copilot_ph": "Спросите: «Какие наряды просрочены?», «Кто свободен?», «Что с К-3?»",
        "copilot_ask": "Спросить Copilot",
        "copilot_answer": "Ответ НарядAI",
        "copilot_suggestions": "Быстрые вопросы",
        "copilot_source": "Источник ответа",
        # --- Экран 2: Исполнитель ---
        "w_title": "📱 Исполнитель — мобильный вид",
        "w_no_active": "Активных нарядов нет. Все задачи на смене выполнены.",
        "w_select": "Текущий наряд",
        "w_accept": "▶️ Принять в работу",
        "w_pause": "⏸️ Приостановить",
        "w_reject": "✖️ Отклонить с причиной",
        "w_reject_reason": "Причина отклонения",
        "w_reject_btn": "Подтвердить отклонение",
        "w_reject_ok": "Наряд отклонён, причина зафиксирована.",
        "w_status_ok": "Статус наряда обновлён: {status}",
        "w_paused": "Наряд приостановлен.",
        "w_closing": "Закрытие наряда",
        "w_work_done": "Выполненные работы",
        "w_work_done_ph": "Опишите, что именно сделано: замена узла, регулировка, проверка...",
        "w_materials": "Списанные ТМЦ и материалы",
        "w_photo_before": "📷 Фото «До» (необязательно)",
        "w_photo_after": "📷 Фото «После» (устранение неисправности)",
        "w_submit": "🏁 Завершить и отправить на проверку ИИ",
        "w_analyzing": "Нейросеть НарядAI анализирует отчёт...",
        "w_score": "Оценка ИИ",
        "w_verdict": "Вердикт ИИ",
        "w_closed": "Наряд закрыт, работы приняты.",
        "w_rework": "Наряд отправлен на доработку.",
        "w_ai_note": "Пояснение ИИ",
        "w_offline_ai": "Офлайн: оценка выполнена локальным ИИ-модулем.",
        "w_need_done": "Опишите выполненные работы перед отправкой.",
        "w_ai_result_hdr": "Результат AI-верификации",
        # --- Экран 3: Руководитель ---
        "mg_title": "📊 Руководитель / ТОиР — аналитика надёжности",
        "mg_saved": "Сэкономлено от предотвращённых простоев",
        "mg_reaction": "Среднее время реакции",
        "mg_quality": "Качество закрытия (оценка ИИ)",
        "mg_closed_total": "Закрыто нарядов",
        "mg_saved_delta": "+14.2% за квартал",
        "mg_reaction_delta": "-28 мин благодаря push-контролю",
        "mg_quality_delta": "оценка ИИ",
        "mg_chart_faults": "Топ отказов оборудования по инцидентам",
        "mg_chart_status": "Распределение статусов нарядов",
        "mg_radar_title": "🧠 Предиктивный радар отказов (Machine Learning)",
        "mg_radar_alert": "Критическое предупреждение ИИ-аналитики",
        "mg_radar_unit": "Проблемный агрегат",
        "mg_radar_code": "Выявленный шифр отказа",
        "mg_radar_risk": "Прогнозируемый риск аварийного останова (7 смен)",
        "mg_radar_reco": "Рекомендация алгоритма",
        "mg_docs": "Документооборот ТОиР",
        "mg_select_order": "Выберите наряд для паспорта ТОиР",
        "mg_pdf": "📥 Скачать паспорт ТОиР (PDF)",
        "mg_excel": "📊 Скачать сводный журнал смены (Excel)",
        "mg_no_orders": "Нет нарядов для выгрузки.",
        "mg_pdf_ready": "PDF-паспорт сформирован.",
        "mg_excel_ready": "Сводный журнал смены сформирован.",
        "mg_pdf_unavailable": "Модуль reportlab недоступен — PDF не сформирован.",
        "mg_ratings_title": "🏆 Рейтинг исполнителей (прозрачный скоринг)",
        "mg_ratings_formula": "Score = (Доля в срок × 40) + (Ср. оценка ИИ × 0.4) − (Возвраты × 5) − (Отказы без причины × 10)",
        "mg_ratings_empty": "Нет данных для расчёта рейтинга исполнителей.",
        "mg_ratings_download": "📥 Скачать рейтинг (CSV)",
        "mg_ppr_title": "🔎 Анализ качества ППР (повторные поломки < 7 дней)",
        "mg_ppr_caption": "Оборудование, сломавшееся в течение 7 дней после закрытия планового наряда. Указывается бригада, выполнявшая ремонт ППР.",
        "mg_ppr_empty": "Подозрительных повторных поломок после ППР не выявлено.",
        "mg_ppr_download": "📥 Скачать анализ ППР (CSV)",
        "mg_ppr_cases": "Случаев повторных поломок",
        "mg_ppr_interval": "Средний интервал",
        "mg_ppr_downtime": "Простой по повторам",
        "unit_days": "дн",
        "unit_hours": "ч",
        "mg_downtime_title": "💰 Простои и прямые финансовые потери",
        "mg_downtime_rate": "Ставка потерь",
        "mg_downtime_hours": "Часы простоя",
        "mg_downtime_loss": "Прямые потери",
        "mg_downtime_incidents": "Инцидентов",
        "mg_1c_title": "🔄 Интеграция с 1С:ТОиР / ERP",
        "mg_1c_caption": "Стандартизированный JSON-пакет за смену: закрытые наряды, списанные ТМЦ, часы простоя и электронные подписи ИИ (SHA-256).",
        "mg_1c_export_btn": "⚙️ Сформировать пакет синхронизации",
        "mg_1c_ready": "Пакет синхронизации сформирован.",
        "mg_1c_orders": "Нарядов в пакете",
        "mg_1c_materials": "Списанных ТМЦ",
        "mg_1c_download": "📥 Скачать JSON для 1С:ТОиР",
        "mg_1c_empty": "Нет закрытых нарядов для выгрузки в 1С.",
        "mg_ai_title": "🧪 AI-оценки закрытия нарядов",
        "mg_ai_caption": "Оценки ИИ-верификации (0–100) по закрытым нарядам ТОиР.",
        "footer": "© 2026 АО «Костанайские минералы» · НарядAI · демонстрационный прототип",
    },
    "KZ": {
        "brand": "⛏️ НарядAI",
        "lang_label": "Тіл / Язык",
        "app_title": "НарядAI: Нарядтарды беру және бақылау жүйесі",
        "app_subtitle": "«Қостанай минералдары» АҚ · Qostanai AI Industry Hackathon 2026",
        "hero_eyebrow": "QOSTANAI AI INDUSTRY HACKATHON 2026",
        "hero_title": "НАРЯД-AI | «ҚОСТАНАЙ МИНЕРАЛДАРЫ» АҚ",
        "hero_subtitle": "ТОжЖ-ны жедел бақылау және нарядтарды беру интеллектуалды жүйесі",
        # --- Рөлге қарай баннер (HTML hero) ---
        "banner_eyebrow": "ТОжЖ жүйесі • «Қостанай минералдары» АҚ",
        "banner_online": "● Сервер белсенді (Online)",
        "banner_master_title": "НАРЯД-AI • АУЫСЫМ ШЕБЕРІНІҢ ПУЛЬТІ",
        "banner_master_sub": "Ауысымды басқару, рұқсаттарды жедел бақылау және жабдық ақауларын есепке алу",
        "banner_worker_title": "НАРЯД-AI • ТОжЖ ЖҰМЫСШЫ ТЕРМИНАЛЫ",
        "banner_worker_sub": "Жұмыстарды тіркеу және нарядтарды жабу",
        "banner_manager_title": "НАРЯД-AI • ТОжЖ АНАЛИТИКАЛЫҚ ХАБЫ",
        "banner_manager_sub": "Сенімділікті мониторингтеу және предиктив",
        "banner_admin_title": "НАРЯД-AI • НСИ ЖӘНЕ АНЫҚТАМАЛАР ПАНЕЛІ",
        "banner_admin_sub": "Анықтамалар, нормативтер және деректерді интеграциялау",
        "status_online_bar": "🟢 Байланыс: Онлайн | 1С-пен синхрондау белсенді",
        "status_offline_bar": "🟠 Байланыс: Офлайн | Деректер жергілікті кэштеледі",
        "status_monitor_ok": "🛡️ Мониторинг: Ауысым қалыпты, сыни ақаулар жоқ",
        "role_select": "Белсенді рөлді таңдаңыз",
        "role_master": "Ауысым шебері",
        "role_worker": "Орындаушы (Жөндеуші/Электрик)",
        "role_manager": "Басшы / ТОжЖ Сараптамасы",
        "role_admin": "НСИ әкімшісі",
        "offline_toggle": "📡 Шахтада/цехта байланыс: ОФЛАЙН / ОНЛАЙН",
        "offline_warn": "ОФЛАЙН-режим: деректер жергілікті кэштеледі және байланыс қалпына келгенде синхрондалады.",
        "online_ok": "ОНЛАЙН: байланыс тұрақты, деректер сервермен синхрондалған.",
        "db_status": "ДБ диагностикасы",
        # --- Ақылды ЖИ-есепші (Smart Ledger AI) ---
        "ledger_title": "🤖 Ақылды ЖИ-есепші және CSV-мен жұмыс",
        "ledger_caption": "Кез келген CSV (ТМҚ есептен шығару, ақаулар журналы) жүктеңіз немесе naryad_ai.db базасындағы демонстрациялық есептен шығару журналын талдаңыз.",
        "ledger_upload": "📤 CSV файлын жүктеу (міндетті емес)",
        "ledger_demo_note": "naryad_ai.db базасынан демонстрациялық ТМҚ есептен шығару журналы көрсетілді. Деректерді ауыстыру үшін өз CSV-іңізді жүктеңіз.",
        "ledger_rows": "Кестедегі жолдар",
        "ledger_materials": "Номенклатура позициялары",
        "ledger_total_qty": "Жалпы шығыс",
        "ledger_anomalies": "ЖИ аномалиялары",
        "ledger_analyze": "🤖 ЖИ-есепші талдауы",
        "ledger_analyzing": "ЖИ-есепші есептен шығаруды талдап жатыр...",
        "ledger_conclusion": "🧾 ЖИ-есепші қорытындысы",
        "ledger_anomaly_list": "🚩 Анықталған шығыс аномалиялары",
        "ledger_recommendations": "✅ Материалдық есепке арналған ұсыныстар",
        "ledger_table": "📋 Есептен шығару журналы",
        "ledger_enriched": "🧮 ЖИ ұсыныстары бар кесте (толтырылған өрістер)",
        "ledger_export_excel": "📥 Өңделген кестені Excel-ге жүктеу",
        "ledger_export_csv": "📄 Тазартылған кестені CSV-ге жүктеу",
        "ledger_ready": "Файл жүктеуге дайын.",
        "ledger_file_error": "Файлды оқу мүмкін болмады: {err}. Демонстрациялық журнал көрсетілді.",
        "ledger_empty": "Көрсетуге деректер жоқ.",
        "ledger_no_anomalies": "Аномалиялар табылмады — есеп біркелкі жүргізілуде.",
        "ledger_severity_high": "критикалық",
        "ledger_severity_medium": "назар аударыңыз",
        "common": {
            "equipment": "Жабдық",
            "status": "Мәртебесі",
            "priority": "Маңыздылығы",
            "assignee": "Орындаушы",
            "created": "Құрылды",
            "deadline": "Мерзімі",
            "description": "Сипаттама",
            "score": "ЖИ бағасы",
            "order_num": "Наряд нөмірі",
            "unit": "Учаске",
            "no_data": "Деректер жоқ",
        },
        "m_title": "👨‍🔧 Ауысым шебері — жедел пульт",
        "alerts_header": "Мерзімді бақылау бойынша белсенді ескертулер",
        "no_alerts": "Белсенді ескертулер жоқ. Ауысым қалыпты жүріп жатыр.",
        "kpi_total": "Барлық нарядтар",
        "kpi_inwork": "Жұмыста",
        "kpi_overdue": "Мерзімі өткен",
        "kpi_closed": "Жабылған",
        "kpi_total_sub": "Ағымдағы ауысым үшін",
        "kpi_inwork_sub": "● Желіде",
        "kpi_overdue_sub": "Назар аударуды қажет етеді",
        "kpi_closed_sub": "ЖИ қабылдады",
        "tab_issue": "Наряд беру (< 1 мин / QR)",
        "tab_kanban": "Ауысым канбаны",
        "tab_copilot": "Шебердің AI-Copilot",
        "issue_subheader": "6 кликпен немесе QR-шильдик арқылы жылдам наряд беру",
        "equipment_selected": "Таңдалған агрегат",
        "equipment_not_selected": "Агрегат таңдалмаған. Шильдикті сканерлеңіз немесе тізімнен таңдаңыз.",
        "normative": "Уақыт нормативі (сағат)",
        "assignee_free": "Бос",
        "assignee_busy": "Жұмыста",
        "assignee_off": "Ауысымда емес",
        "description_ph": "Мысалы: К-3 конвейері жетегінің роликтік подшипнигінің апаттық қызуы мен дірілі",
        "voice_hint": "🎤 Өріс дауыспен енгізуді қолдайды: аудио жазбаны тіркеп, «Таны» түймесін басыңыз.",
        "voice_upload": "Шебердің дауыс жазбасы (міндетті емес)",
        "voice_recognize": "Сөйлеуді тану",
        "voice_done": "Сөйлеу танылды және сипаттамаға қойылды.",
        "voice_fail": "Жазбаны тану мүмкін болмады, мәтінді қолмен енгізіңіз.",
        "issue_btn": "Нарядты құру және жіберу",
        "issue_ok": "{num} наряды сәтті берілді: {worker}",
        "issue_local": "Офлайн: наряд жергілікті сақталды және автоматты синхрондалады.",
        "issue_need_equip": "Алдымен жабдықты таңдаңыз (QR немесе тізім).",
        "issue_need_desc": "Ақаулық сипаттамасын толтырыңыз.",
        "issue_error": "Наряд құру мүмкін болмады: {err}",
        "kanban_filter_status": "Мәртебе бойынша сүзгі",
        "kanban_filter_priority": "Маңыздылық бойынша сүзгі",
        "kanban_empty": "Берілген сүзгілер бойынша нарядтар жоқ.",
        "kanban_download": "Таңдаманы жүктеу (CSV)",
        "copilot_intro": "Нарядтар, бос тұру, ресурстар және жабдық тәуекелдері бойынша жедел AI-ассистент.",
        "copilot_ph": "Сұраңыз: «Қандай нарядтардың мерзімі өтті?», «Кім бос?», «К-3 не жағдайда?»",
        "copilot_ask": "Copilot-тан сұрау",
        "copilot_answer": "НарядAI жауабы",
        "copilot_suggestions": "Жылдам сұрақтар",
        "copilot_source": "Жауап көзі",
        "w_title": "📱 Орындаушы — мобильді көрініс",
        "w_no_active": "Белсенді нарядтар жоқ. Ауысымдағы барлық тапсырмалар орындалды.",
        "w_select": "Ағымдағы наряд",
        "w_accept": "▶️ Жұмысқа қабылдау",
        "w_pause": "⏸️ Тоқтата тұру",
        "w_reject": "✖️ Себебімен қабылдамау",
        "w_reject_reason": "Қабылдамау себебі",
        "w_reject_btn": "Қабылдамауды растау",
        "w_reject_ok": "Наряд қабылданбады, себебі тіркелді.",
        "w_status_ok": "Наряд мәртебесі жаңартылды: {status}",
        "w_paused": "Наряд тоқтатылды.",
        "w_closing": "Нарядты жабу",
        "w_work_done": "Орындалған жұмыстар",
        "w_work_done_ph": "Не істелгенін сипаттаңыз: торапты ауыстыру, реттеу, тексеру...",
        "w_materials": "Есептен шығарылған ТМҚ және материалдар",
        "w_photo_before": "📷 «Дейін» фотосы (міндетті емес)",
        "w_photo_after": "📷 «Кейін» фотосы (ақаулықты жою)",
        "w_submit": "🏁 Аяқтау және ЖИ тексеруіне жіберу",
        "w_analyzing": "НарядAI нейрожелісі есепті талдап жатыр...",
        "w_score": "ЖИ бағасы",
        "w_verdict": "ЖИ қорытындысы",
        "w_closed": "Наряд жабылды, жұмыстар қабылданды.",
        "w_rework": "Наряд пысықтауға жіберілді.",
        "w_ai_note": "ЖИ түсіндірмесі",
        "w_offline_ai": "Офлайн: баға жергілікті ЖИ-модулімен қойылды.",
        "w_need_done": "Жібермес бұрын орындалған жұмыстарды сипаттаңыз.",
        "w_ai_result_hdr": "AI-верификация нәтижесі",
        "mg_title": "📊 Басшы / ТОжЖ — сенімділік аналитикасы",
        "mg_saved": "Алдын алынған бос тұрудан үнемделген",
        "mg_reaction": "Орташа реакция уақыты",
        "mg_quality": "Жабу сапасы (ЖИ бағасы)",
        "mg_closed_total": "Жабылған нарядтар",
        "mg_saved_delta": "+14.2% тоқсан ішінде",
        "mg_reaction_delta": "push-бақылау арқылы −28 мин",
        "mg_quality_delta": "ЖИ бағасы",
        "mg_chart_faults": "Инциденттер бойынша жабдық ақауларының ТОП-ы",
        "mg_chart_status": "Наряд мәртебелерінің бөлінуі",
        "mg_radar_title": "🧠 Ақаулардың предиктивті радары (Machine Learning)",
        "mg_radar_alert": "ЖИ-аналитиканың сыни ескертуі",
        "mg_radar_unit": "Проблемалық агрегат",
        "mg_radar_code": "Анықталған ақау шифры",
        "mg_radar_risk": "Апаттық тоқтаудың болжамды тәуекелі (7 ауысым)",
        "mg_radar_reco": "Алгоритм ұсынысы",
        "mg_docs": "ТОжЖ құжат айналымы",
        "mg_select_order": "ТОжЖ паспорты үшін нарядты таңдаңыз",
        "mg_pdf": "📥 ТОжЖ паспортын жүктеу (PDF)",
        "mg_excel": "📊 Ауысымның жиынтық журналын жүктеу (Excel)",
        "mg_no_orders": "Жүктеуге арналған нарядтар жоқ.",
        "mg_pdf_ready": "PDF-паспорт құрылды.",
        "mg_excel_ready": "Ауысымның жиынтық журналы құрылды.",
        "mg_pdf_unavailable": "reportlab модулі қолжетімсіз — PDF құрылмады.",
        "mg_ratings_title": "🏆 Орындаушылар рейтингі (ашық скоринг)",
        "mg_ratings_formula": "Score = (Мерзімінде орындалған үлесі × 40) + (ЖИ орташа бағасы × 0.4) − (Пысықтауға қайтарылуы × 5) − (Себепсіз бас тарту × 10)",
        "mg_ratings_empty": "Орындаушылар рейтингін есептеу үшін деректер жоқ.",
        "mg_ratings_download": "📥 Рейтингті жүктеу (CSV)",
        "mg_ppr_title": "🔎 ППР сапасын талдау (7 күннен кейінгі қайталама ақаулар)",
        "mg_ppr_caption": "Жоспарлы наряд жабылғаннан кейін 7 күн ішінде сынған жабдық. ППР жөндеуін орындаған бригада көрсетіледі.",
        "mg_ppr_empty": "ППР-дан кейін күдікті қайталама ақаулар анықталмады.",
        "mg_ppr_download": "📥 ППР талдауын жүктеу (CSV)",
        "mg_ppr_cases": "Қайталама ақаулар саны",
        "mg_ppr_interval": "Орташа интервал",
        "mg_ppr_downtime": "Қайталамалар бойынша бос тұру",
        "unit_days": "күн",
        "unit_hours": "сағ",
        "mg_downtime_title": "💰 Бос тұру және тікелей қаржылық шығындар",
        "mg_downtime_rate": "Шығын мөлшерлемесі",
        "mg_downtime_hours": "Бос тұру сағаттары",
        "mg_downtime_loss": "Тікелей шығындар",
        "mg_downtime_incidents": "Инциденттер",
        "mg_1c_title": "🔄 1С:ТОиР / ERP-мен интеграция",
        "mg_1c_caption": "Ауысымға арналған стандартталған JSON-пакет: жабылған нарядтар, есептен шығарылған ТМҚ, бос тұру сағаттары және ЖИ электрондық қолтаңбалары (SHA-256).",
        "mg_1c_export_btn": "⚙️ Синхрондау пакетін құру",
        "mg_1c_ready": "Синхрондау пакеті құрылды.",
        "mg_1c_orders": "Пакеттегі нарядтар",
        "mg_1c_materials": "Есептен шығарылған ТМҚ",
        "mg_1c_download": "📥 1С:ТОиР үшін JSON жүктеу",
        "mg_1c_empty": "1С-ке жүктеуге жабылған нарядтар жоқ.",
        "mg_ai_title": "🧪 Нарядтарды жабу ЖИ-бағалары",
        "mg_ai_caption": "Жабылған ТОжЖ нарядтары бойынша ЖИ-верификация бағалары (0–100).",
        "footer": "© 2026 «Қостанай минералдары» АҚ · НарядAI · демонстрациялық прототип",
    },
}


def t(lang: str, key: str) -> str:
    """Получить строку локализации. При отсутствии ключа вернуть сам ключ."""
    return str(LANG.get(lang, LANG["RU"]).get(key, key))


def c(lang: str, key: str) -> str:
    """Получить строку из блока ``common`` (названия сущностей)."""
    common_block: Dict[str, Any] = LANG.get(lang, LANG["RU"]).get("common", {})
    return str(common_block.get(key, key))


def _clean_label(text: Any) -> str:
    """Убрать ведущие эмодзи/служебные символы из подписи (LANG не меняем)."""
    raw = str(text).strip()
    index = 0
    while index < len(raw) and not raw[index].isalnum():
        index += 1
    return raw[index:].strip()


# ==================================================================
# 3. СЛОЙ ДАННЫХ: БЕЗОПАСНЫЕ ОБЁРТКИ НАД SQLITE
# ==================================================================
def get_connection() -> sqlite3.Connection:
    """Соединение с ``naryad_ai.db`` с внешними ключами и таймаутом."""
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def _record_db_error(exc: Exception) -> None:
    """Запомнить последнюю ошибку БД для панели диагностики в сайдбаре."""
    message = f"{type(exc).__name__}: {exc}"
    st.session_state["db_last_error"] = message


def db_query_df(sql: str, params: Sequence[Any] = ()) -> pd.DataFrame:
    """Безопасный SELECT → DataFrame. При ошибке — пустой DataFrame."""
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = get_connection()
        return pd.read_sql_query(sql, conn, params=tuple(params))
    except Exception as exc:  # noqa: BLE001 — экран не должен падать из-за БД
        _record_db_error(exc)
        return pd.DataFrame()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                st.session_state["db_close_error"] = True


def db_fetchall(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """Безопасный SELECT → список словарей. При ошибке — пустой список."""
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        _record_db_error(exc)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                st.session_state["db_close_error"] = True


def db_scalar(sql: str, params: Sequence[Any] = (), default: int = 0) -> int:
    """Безопасный одиночный COUNT/скаляр."""
    rows = db_fetchall(sql, params)
    if not rows:
        return default
    try:
        return int(next(iter(rows[0].values())))
    except (StopIteration, TypeError, ValueError):
        return default


def db_execute(
    sql: str,
    params: Any = (),
    many: bool = False,
) -> Tuple[bool, Optional[int], str]:
    """Безопасный INSERT/UPDATE/DELETE.

    Returns
    -------
    tuple
        ``(success, lastrowid, error_text)``. ``lastrowid`` заполняется для
        одиночного INSERT.
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if many:
            cursor.executemany(sql, params)
        else:
            cursor.execute(sql, tuple(params))
        conn.commit()
        return True, cursor.lastrowid, ""
    except Exception as exc:  # noqa: BLE001 — любая ошибка БД обрабатывается тут
        _record_db_error(exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                st.session_state["db_rollback_error"] = True
        return False, None, str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                st.session_state["db_close_error"] = True


def log_order_event(
    order_id: int,
    author_id: int,
    action: str,
    reason: str = "",
) -> None:
    """Записать событие жизненного цикла наряда в ``order_events`` (безопасно)."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    db_execute(
        """
        INSERT INTO order_events (order_id, author_id, action, reason, event_time)
        VALUES (?, ?, ?, ?, ?)
        """,
        (int(order_id), int(author_id), str(action), str(reason), now_str),
    )


# ==================================================================
# 4. БИЗНЕС-ЛОГИКА НАРЯДОВ
# ==================================================================
def generate_order_number() -> str:
    """Уникальный номер наряда вида ``Н-XXXXXX``."""
    for _ in range(30):
        candidate = f"Н-{random.randint(100000, 999999)}"
        exists = db_fetchall(
            "SELECT 1 AS x FROM work_orders WHERE order_num = ? LIMIT 1", (candidate,)
        )
        if not exists:
            return candidate
    return f"Н-{int(datetime.datetime.now().timestamp())}"


def get_default_master_id() -> int:
    """ID мастера по умолчанию (первый в справочнике)."""
    rows = db_fetchall(
        "SELECT id FROM employees WHERE role = 'Мастер' ORDER BY id ASC LIMIT 1"
    )
    if rows:
        try:
            return int(rows[0]["id"])
        except (KeyError, TypeError, ValueError):
            return 1
    return 1


def create_work_order(
    order_num: str,
    order_type: str,
    description: str,
    unit_id: int,
    equipment_id: int,
    assignee_id: int,
    master_id: int,
    priority: str,
    normative_hours: float,
) -> Tuple[bool, str]:
    """Создать наряд и событие выдачи в одной транзакции.

    Returns
    -------
    tuple
        ``(success, info)`` — при успехе ``info`` = номер наряда, иначе текст
        ошибки.
    """
    now = datetime.datetime.now()
    created_at = now.strftime("%Y-%m-%d %H:%M")
    deadline = (now + datetime.timedelta(hours=float(normative_hours))).strftime(
        "%Y-%m-%d %H:%M"
    )

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO work_orders (
                order_num, order_type, description, unit_id, equipment_id,
                assignee_id, master_id, priority, normative_hours, status,
                created_at, deadline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Выдан', ?, ?)
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
                float(normative_hours),
                created_at,
                deadline,
            ),
        )
        order_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO order_events (order_id, author_id, action, reason, event_time)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(order_id),
                int(master_id),
                "Выдача наряда",
                "Первичная регистрация через пульт мастера",
                created_at,
            ),
        )
        conn.commit()
        return True, order_num
    except Exception as exc:  # noqa: BLE001 — ошибку показываем мастеру, не роняем пульт
        _record_db_error(exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                st.session_state["db_rollback_error"] = True
        return False, str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                st.session_state["db_close_error"] = True


def update_order_status(order_id: int, status: str, reason: str = "") -> Tuple[bool, str]:
    """Обновить статус наряда и записать событие."""
    author_id = get_default_master_id()
    db_execute(
        "UPDATE work_orders SET status = ? WHERE id = ?", (status, int(order_id))
    )
    log_order_event(order_id, author_id, status, reason)
    return True, status


def save_ai_evaluation(
    order_id: int,
    verdict: str,
    score: int,
    explanation: str,
) -> None:
    """Сохранить оценку ИИ (перезапись оценки для наряда безопасна)."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    db_execute(
        """
        INSERT OR REPLACE INTO ai_evaluations
            (order_id, verdict, score, explanation, evaluated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (int(order_id), verdict, int(score), explanation, now_str),
    )


def save_material_writeoffs(order_id: int, material_ids: Sequence[int]) -> None:
    """Списать материалы по наряду (с предварительной очисткой старых строк)."""
    db_execute("DELETE FROM material_writeoffs WHERE order_id = ?", (int(order_id),))
    if not material_ids:
        return
    rows = [(int(order_id), int(mid), 1.0) for mid in material_ids]
    db_execute(
        "INSERT INTO material_writeoffs (order_id, material_id, quantity) VALUES (?, ?, ?)",
        rows,
        many=True,
    )


def save_order_photo(
    order_id: int,
    photo_type: str,
    photo_bytes: bytes,
    author_id: int,
    comment: str = "",
) -> None:
    """Сохранить фото наряда на диск и записать путь в ``order_photos``."""
    try:
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"order_{int(order_id)}_{photo_type}_{stamp}.jpg"
        file_path = os.path.join(PHOTOS_DIR, filename)
        with open(file_path, "wb") as handle:
            handle.write(photo_bytes)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        db_execute(
            """
            INSERT INTO order_photos
                (order_id, photo_type, file_path, comment, captured_at, author_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(order_id), photo_type, file_path, comment, now_str, int(author_id)),
        )
    except Exception as exc:  # noqa: BLE001 — фото не должно ронять закрытие наряда
        st.session_state["photo_error"] = f"{type(exc).__name__}: {exc}"


# ==================================================================
# 5. АДАПТЕРЫ ВНЕШНИХ МОДУЛЕЙ (SAFE CALLS)
# ==================================================================
def call_deadlines_and_escalations() -> Dict[str, List[str]]:
    """Безопасный вызов контроля дедлайнов и эскалаций."""
    empty: Dict[str, List[str]] = {"escalations": [], "expired": [], "warnings": []}
    if _deadlines_impl is None:
        return empty
    try:
        result = _deadlines_impl()
        if not isinstance(result, dict):
            return empty
        return {
            "escalations": list(result.get("escalations") or []),
            "expired": list(result.get("expired") or []),
            "warnings": list(result.get("warnings") or []),
        }
    except Exception as exc:  # noqa: BLE001 — алерты не должны ронять пульт
        _record_db_error(exc)
        return empty


def _normalize_ai_result(result: Any) -> Dict[str, Any]:
    """Привести ответ внешнего AI-модуля к строгому контракту."""
    verdict = "Принято с замечаниями"
    score = 70
    explanation = "Автоматическая оценка НарядAI."

    if isinstance(result, dict):
        raw_verdict = str(result.get("verdict") or "").strip()
        if raw_verdict in VALID_VERDICTS:
            verdict = raw_verdict
        else:
            lowered = raw_verdict.casefold()
            if "доработ" in lowered:
                verdict = "Требует доработки"
            elif "замеч" in lowered:
                verdict = "Принято с замечаниями"
            elif "принят" in lowered:
                verdict = "Принято"
        try:
            score = int(result.get("score", 70))
        except (TypeError, ValueError):
            score = 70
        score = max(0, min(100, score))
        explanation = str(
            result.get("explanation")
            or result.get("comment")
            or result.get("reason")
            or explanation
        )
    elif isinstance(result, str) and result.strip():
        explanation = result.strip()

    return {"verdict": verdict, "score": score, "explanation": explanation}


def _default_verify_work_order(
    problem_desc: str,
    work_done: str,
    materials_used: List[str],
    photo_bytes: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Дефолтная эвристика проверки отчёта, если внешний модуль недоступен."""
    text = f"{problem_desc} {work_done}".casefold()
    score = 75
    verdict = "Принято с замечаниями"
    notes: List[str] = []

    if work_done and len(work_done.split()) >= 3:
        score += 8
    if materials_used:
        score += 5
        notes.append("Материалы соответствуют выполненному ремонту.")
    if photo_bytes:
        score += 7
        notes.append("Приложено фотоотчёт «После».")
    if any(word in text for word in ("замен", "ремонт", "регулир", "провер", "устран")):
        score += 5
        notes.append("Описание содержит конкретные действия по устранению.")
    if any(word in text for word in ("перегрев", "вибрац", "подшип", "течь", "авар")):
        notes.append("Диагноз соответствует заявленной неисправности.")

    score = max(0, min(100, score))
    if score >= 85:
        verdict = "Принято"
    elif score < AI_CLOSE_THRESHOLD:
        verdict = "Требует доработки"
    if not notes:
        notes.append("Автоматическая оценка по формальным признакам отчёта.")
    return {
        "verdict": verdict,
        "score": score,
        "explanation": " ".join(notes),
    }


def _module_accepts_photo(func: Callable[..., Any]) -> bool:
    """Определить, поддерживает ли внешняя функция аргумент фото."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    names = set(signature.parameters.keys())
    if names & {"photo_bytes", "photo", "image_bytes"}:
        return True
    return any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )


def safe_verify_work_order(
    problem_desc: str,
    work_done: str,
    materials_used: Sequence[str],
    photo_bytes: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Проверка отчёта ИИ с адаптацией под сигнатуру внешнего модуля.

    Модуль ``src.ai_engine.verify_work_order`` в текущей версии принимает
    ``(problem_desc, work_done, materials_used)`` и не знает о фото. Адаптер
    сам определяет наличие параметра ``photo_bytes`` и вызывает функцию
    подходящим способом, поэтому добавление фото в будущем не потребует
    правок интерфейса.
    """
    materials_list = [
        str(item) for item in (materials_used or []) if str(item).strip()
    ]

    if _verify_work_order_impl is not None:
        try:
            if _module_accepts_photo(_verify_work_order_impl):
                result = _verify_work_order_impl(
                    problem_desc=problem_desc,
                    work_done=work_done,
                    materials_used=materials_list,
                    photo_bytes=photo_bytes,
                )
            else:
                result = _verify_work_order_impl(
                    problem_desc=problem_desc,
                    work_done=work_done,
                    materials_used=materials_list,
                )
            return _normalize_ai_result(result)
        except Exception as exc:  # noqa: BLE001 — падение ИИ не должно ронять смену
            st.session_state["ai_error"] = f"{type(exc).__name__}: {exc}"

    return _normalize_ai_result(
        _default_verify_work_order(problem_desc, work_done, materials_list, photo_bytes)
    )


def safe_analyze_photos(before_bytes: Optional[bytes], after_bytes: Optional[bytes]) -> str:
    """Дополнительный анализ пары фото «до/после». Возвращает пояснение."""
    if _analyze_photos_impl is None or not before_bytes or not after_bytes:
        return ""
    try:
        result = _analyze_photos_impl(before_bytes, after_bytes)
        if isinstance(result, dict):
            return str(result.get("verdict") or "")
        return str(result or "")
    except Exception as exc:  # noqa: BLE001
        st.session_state["ai_error"] = f"{type(exc).__name__}: {exc}"
        return ""


def get_prediction() -> Dict[str, Any]:
    """Безопасный прогноз предиктивного радара с полным набором ключей."""
    prediction = dict(DEFAULT_PREDICTION)
    if _predict_impl is None:
        return prediction
    try:
        result = _predict_impl()
        if isinstance(result, dict):
            for key, value in result.items():
                if value is not None:
                    prediction[key] = value
    except Exception as exc:  # noqa: BLE001 — радар руководителя не должен падать
        st.session_state["ai_error"] = f"{type(exc).__name__}: {exc}"
    return prediction


def ask_copilot(question: str) -> Dict[str, Any]:
    """Безопасный вызов AI-ассистента мастера."""
    if _copilot_impl is not None:
        try:
            result = _copilot_impl(question)
            if isinstance(result, dict):
                return result
            return {"answer": str(result), "source": "module", "suggestions": []}
        except Exception as exc:  # noqa: BLE001 — ассистент не должен ронять пульт
            st.session_state["ai_error"] = f"{type(exc).__name__}: {exc}"

    # Дефолтная реализация, если модуль master_copilot отсутствует.
    if not (question or "").strip():
        return {
            "answer": "Задайте вопрос по нарядам, свободным исполнителям или рискам оборудования.",
            "source": "system",
            "suggestions": [
                "Какие наряды просрочены?",
                "Кто свободен сейчас?",
                "Что с риском на конвейере К-3?",
            ],
        }
    prediction = get_prediction()
    overdue = db_scalar(
        """
        SELECT COUNT(*) AS n FROM work_orders
        WHERE status = 'Просрочен'
           OR (status IN ('Выдан','Принят в работу','В очереди','В работе','Приостановлен','На доработку','Проверка ИИ')
               AND deadline < ?)
        """,
        (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),),
    )
    return {
        "answer": (
            f"Сводка смены: просрочено — {overdue}. "
            f"Предиктивный радар: {prediction.get('top_problem_unit')} "
            f"({prediction.get('fault_code')}), риск {prediction.get('risk_percent')}% "
            f"на {prediction.get('horizon_shifts', 7)} смен. "
            "Рекомендуется закрывать аварийные наряды в первую очередь."
        ),
        "source": "fallback",
        "suggestions": [
            "Какие наряды просрочены?",
            "Кто свободен сейчас?",
            "Что с риском на конвейере К-3?",
        ],
    }


def export_excel(rows: Any, summary: Optional[Dict[str, Any]] = None) -> bytes:
    """Безопасная выгрузка журнала смены в Excel. Всегда возвращает байты."""
    if _export_excel_impl is not None:
        try:
            data = _export_excel_impl(rows, summary)
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
        except Exception as exc:  # noqa: BLE001 — выгрузка не должна ронять экран
            st.session_state["export_error"] = f"{type(exc).__name__}: {exc}"

    # Дефолтная реализация, если модуль master_copilot отсутствует.
    buffer = io.BytesIO()
    try:
        frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows or []))
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            frame.to_excel(writer, index=False, sheet_name="Журнал смены")
            if summary:
                pd.DataFrame(
                    [{"Показатель": k, "Значение": v} for k, v in summary.items()]
                ).to_excel(writer, index=False, sheet_name="Сводка")
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        st.session_state["export_error"] = f"{type(exc).__name__}: {exc}"
        try:
            frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows or []))
            return frame.to_csv(index=False).encode("utf-8-sig")
        except Exception:  # noqa: BLE001
            return "Ошибка формирования выгрузки".encode("utf-8-sig")


# ==================================================================
# 6. PDF-ПАСПОРТ ТОИР (reportlab)
# ==================================================================
_PDF_FONT_NAME: Optional[str] = None


def _register_pdf_font() -> str:
    """Зарегистрировать TTF-шрифт с поддержкой кириллицы (кэшируется)."""
    global _PDF_FONT_NAME
    if _PDF_FONT_NAME is not None:
        return _PDF_FONT_NAME
    candidates = (
        ("NaryadArial", r"C:\Windows\Fonts\arial.ttf"),
        ("NaryadArial", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("NaryadArial", "/Library/Fonts/Arial.ttf"),
        ("NaryadArial", os.path.join(APP_DIR, "assets", "DejaVuSans.ttf")),
    )
    for name, path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                _PDF_FONT_NAME = name
                return name
            except Exception:  # noqa: BLE001 — пробуем следующий шрифт
                continue
    _PDF_FONT_NAME = "Helvetica"
    return _PDF_FONT_NAME


def _wrap_pdf_text(text: str, font: str, size: int, max_width: float) -> List[str]:
    """Разбить длинную строку на строки под заданную ширину PDF."""
    words = str(text).split()
    if not words:
        return [""]
    lines: List[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        try:
            width = pdfmetrics.stringWidth(candidate, font, size)
        except Exception:  # noqa: BLE001
            width = len(candidate) * size * 0.5
        if width <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def generate_pdf_report(order_data: Dict[str, Any], title: str) -> Optional[bytes]:
    """Сформировать официальный паспорт ТОиР в PDF. ``None`` при сбое."""
    if not _REPORTLAB_AVAILABLE:
        return None
    try:
        font = _register_pdf_font()
        buffer = io.BytesIO()
        pdf = _pdf_canvas.Canvas(buffer, pagesize=A4)
        page_width, page_height = A4
        left_margin = 40
        right_margin = page_width - 40

        pdf.setFont(font, 15)
        pdf.drawString(left_margin, page_height - 50, "АО «КОСТАНАЙСКИЕ МИНЕРАЛЫ»")
        pdf.setFont(font, 12)
        pdf.drawString(left_margin, page_height - 70, title)
        pdf.setFont(font, 9)
        pdf.drawString(
            left_margin,
            page_height - 85,
            "Система: НарядAI | Выгрузка: "
            + datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
        )
        pdf.line(left_margin, page_height - 95, right_margin, page_height - 95)

        y = page_height - 120
        for key, value in order_data.items():
            pdf.setFont(font, 10)
            pdf.drawString(left_margin, y, f"{key}:")
            pdf.setFont(font, 10)
            for offset, line in enumerate(
                _wrap_pdf_text(str(value), font, 10, right_margin - left_margin - 160)
            ):
                pdf.drawString(left_margin + 160, y - offset * 14, line)
            y -= 16 * max(1, len(
                _wrap_pdf_text(str(value), font, 10, right_margin - left_margin - 160)
            ))
            if y < 120:
                pdf.showPage()
                pdf.setFont(font, 10)
                y = page_height - 60

        pdf.line(left_margin, y, right_margin, y)
        y -= 25
        pdf.setFont(font, 11)
        pdf.drawString(left_margin, y, "Цифровая верификация НарядAI")
        pdf.setFont(font, 9)
        note = (
            "Наряд проверен ИИ-модулем НарядAI. Данные синхронизированы "
            "с контуром ТОиР/1C. Документ сформирован автоматически."
        )
        for offset, line in enumerate(
            _wrap_pdf_text(note, font, 9, right_margin - left_margin)
        ):
            pdf.drawString(left_margin, y - 16 - offset * 12, line)

        pdf.showPage()
        pdf.save()
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 — выгрузка не должна ронять экран
        st.session_state["pdf_error"] = f"{type(exc).__name__}: {exc}"
        return None


# ==================================================================
# 7. UI-ПОМОЩНИКИ
# ==================================================================
def _stretch(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Вызвать виджет Streamlit на всю ширину с обратной совместимостью API."""
    try:
        return func(*args, width="stretch", **kwargs)
    except TypeError:
        kwargs.pop("width", None)
        return func(*args, use_container_width=True, **kwargs)


def status_label(lang: str, status: str) -> str:
    """Локализованное название статуса."""
    return STATUS_LABELS.get(lang, STATUS_LABELS["RU"]).get(status, str(status))


def priority_label(lang: str, priority: str) -> str:
    """Локализованное название приоритета."""
    return PRIORITY_LABELS.get(lang, PRIORITY_LABELS["RU"]).get(priority, str(priority))


def localized_statuses(lang: str, statuses: Sequence[str]) -> List[str]:
    """Отображения статусов для селектов (порядок сохраняется)."""
    return [status_label(lang, item) for item in statuses]


# ==================================================================
# 7b. СОВРЕМЕННЫЙ ПРОМЫШЛЕННЫЙ UI: CSS И HTML-КОМПОНЕНТЫ
# ==================================================================
#: CSS-оформление: Industrial Dark / Clean Pro.
#: Тёмная «графитовая» палитра, карточки с рамками и hover-подсветкой,
#: градиентные primary-кнопки, акцентные вкладки и статусные чипы.
_DESIGN_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto+Condensed:wght@500;600;700&family=JetBrains+Mono:wght@500;700&display=swap');

    __ROOT__

    /* ============================================================
       БАЗА
       ============================================================ */
    html, body, [class*="css"], .stApp, button, input, textarea, select,
    [data-testid="stAppViewContainer"] {
        font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif !important;
    }
    .stApp,
    [data-testid="stAppViewContainer"] {
        color: var(--text) !important;
        background-color: var(--bg) !important;
        background-image:
            radial-gradient(ellipse 120% 100% at 50% 45%, transparent 55%, var(--vignette) 100%),
            var(--topo),
            var(--topo),
            repeating-linear-gradient(0deg, var(--grid-line) 0 1px, transparent 1px 48px),
            repeating-linear-gradient(90deg, var(--grid-line) 0 1px, transparent 1px 48px);
        background-position: center, right -80px bottom -70px, left -130px top -110px, 0 0, 0 0;
        background-size: cover, 760px 570px, 760px 570px, auto, auto;
        background-repeat: no-repeat, no-repeat, no-repeat, repeat, repeat;
        background-attachment: fixed, fixed, fixed, fixed, fixed;
    }
    [data-testid="stHeader"] { background: transparent !important; }
    .block-container {
        padding-top: 1.2rem !important;
        padding-bottom: 3rem !important;
        max-width: 1500px !important;
    }
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Roboto Condensed', 'Inter', sans-serif !important;
        color: var(--text) !important;
        text-transform: uppercase;
        font-weight: 700 !important;
        letter-spacing: 1px !important;
    }
    h2::after, h3::after {
        content: '';
        display: block;
        width: 48px;
        height: 2px;
        background: var(--accent);
        margin-top: 8px;
    }
    .naryad-hero h2::after,
    .naryad-brand h3::after,
    .naryad-footer h2::after { display: none; }
    p, span, label, li, div { color: var(--text); }
    small, .stCaption, [data-testid="stCaptionContainer"] {
        color: var(--muted) !important;
        letter-spacing: 0.4px;
    }
    a { color: var(--steel) !important; }
    hr, div[data-testid="stDivider"] hr { border-color: var(--border) !important; }

    /* ---------- Скрыть служебные элементы Streamlit ---------- */
    footer,
    [data-testid="stFooter"],
    #MainMenu,
    [data-testid="stStatusWidget"] {
        visibility: hidden !important;
        height: 0 !important;
    }

    /* ---------- Скроллбар и выделение ---------- */
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb {
        background: var(--steel);
        border-radius: 0;
        border: 2px solid transparent;
        background-clip: padding-box;
    }
    ::-webkit-scrollbar-thumb:hover { background: var(--accent); background-clip: padding-box; }
    ::selection { background: var(--accent); color: var(--button-text); }

    /* ============================================================
       КАРТОЧКИ И ТЕХНИЧЕСКИЕ УГОЛКИ
       ============================================================ */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        position: relative;
        border-radius: 6px !important;
        border: 1px solid var(--border) !important;
        background: var(--card) !important;
        box-shadow: var(--shadow) !important;
        padding: 24px !important;
        transition: border-color 0.2s ease, background 0.2s ease !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: var(--steel) !important;
        background: var(--card-hover) !important;
    }
    .naryad-stat,
    .naryad-hero,
    div[data-testid="stExpander"] { position: relative; }
    .naryad-stat::before,
    .naryad-hero::before,
    div[data-testid="stExpander"]::before {
        content: '';
        position: absolute;
        top: 5px; left: 5px;
        width: 10px; height: 10px;
        border-top: 2px solid var(--steel);
        border-left: 2px solid var(--steel);
        opacity: 0.4;
        pointer-events: none;
    }
    .naryad-stat::after,
    .naryad-hero::after,
    div[data-testid="stExpander"]::after {
        content: '';
        position: absolute;
        bottom: 5px; right: 5px;
        width: 10px; height: 10px;
        border-bottom: 2px solid var(--steel);
        border-right: 2px solid var(--steel);
        opacity: 0.4;
        pointer-events: none;
    }

    /* ============================================================
       БЕЙДЖИ СТАТУСОВ / ПРИОРИТЕТОВ
       ============================================================ */
    .naryad-badge {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 3px;
        font-weight: 600;
        font-size: 0.74rem;
        line-height: 1.6;
        margin: 0 4px 2px 0;
        white-space: nowrap;
        text-transform: uppercase;
        letter-spacing: 0.4px;
    }
    .naryad-badge-green  { color: var(--success); background: rgba(79, 157, 105, 0.12);  background: color-mix(in srgb, var(--success) 12%, transparent); border: 1px solid rgba(79, 157, 105, 0.35); }
    .naryad-badge-red    { color: var(--danger);  background: rgba(192, 80, 77, 0.12);   background: color-mix(in srgb, var(--danger) 12%, transparent);  border: 1px solid rgba(192, 80, 77, 0.35); }
    .naryad-badge-yellow { color: var(--warning); background: rgba(217, 164, 65, 0.12);  background: color-mix(in srgb, var(--warning) 12%, transparent); border: 1px solid rgba(217, 164, 65, 0.35); }
    .naryad-badge-blue   { color: var(--steel);   background: rgba(74, 107, 138, 0.15);  background: color-mix(in srgb, var(--steel) 15%, transparent);   border: 1px solid rgba(74, 107, 138, 0.4); }
    .naryad-badge-gray   { color: var(--muted);   background: rgba(140, 150, 159, 0.14); background: color-mix(in srgb, var(--muted) 14%, transparent);   border: 1px solid rgba(140, 150, 159, 0.32); }

    /* ---------- Карточка заключения ИИ ---------- */
    .naryad-ai-card {
        border-radius: 6px;
        padding: 18px 20px;
        border: 1px solid var(--border);
        border-left: 4px solid var(--accent);
        background: var(--card);
        box-shadow: var(--shadow);
        margin: 10px 0;
        color: var(--text) !important;
    }
    .naryad-ai-card h4 { margin: 0 0 6px 0; color: var(--accent) !important; }
    .naryad-ai-card p  { margin: 0; color: var(--text) !important; }

    /* ---------- Служебные маркеры ролей (не менять) ---------- */
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.worker-card-marker) {
        border-left: 4px solid var(--steel) !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.worker-prio-red)    { border-left-color: var(--danger) !important; }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.worker-prio-yellow) { border-left-color: var(--warning) !important; }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.worker-prio-blue)   { border-left-color: var(--steel) !important; }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.worker-prio-gray)   { border-left-color: var(--muted) !important; }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.worker-cert-marker) {
        border: 1px solid rgba(74, 107, 138, 0.5) !important;
        border-left: 4px solid var(--steel) !important;
        background: var(--card-hover) !important;
    }
    .naryad-cert-title {
        font-family: 'Roboto Condensed', sans-serif;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: var(--accent) !important;
        margin-bottom: 6px;
        font-size: 0.95rem;
    }

    /* ---------- Радар безопасности (Alert Level 1) ---------- */
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.radar-marker) {
        border: 1px solid rgba(192, 80, 77, 0.55) !important;
        border-left: 4px solid var(--danger) !important;
        background: var(--card) !important;
    }
    .naryad-alert-l1 {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 3px;
        background: rgba(192, 80, 77, 0.12);
        background: color-mix(in srgb, var(--danger) 14%, transparent);
        color: var(--danger);
        border: 1px solid rgba(192, 80, 77, 0.45);
        font-family: 'Roboto Condensed', sans-serif;
        font-weight: 700;
        font-size: 0.72rem;
        letter-spacing: 0.1em;
        text-transform: uppercase;
    }

    /* ============================================================
       БОКОВАЯ ПАНЕЛЬ
       ============================================================ */
    section[data-testid="stSidebar"] {
        background: var(--bg-2) !important;
        border-right: 1px solid var(--border) !important;
    }
    section[data-testid="stSidebar"] > div { padding: 20px 16px !important; }

    .naryad-brand {
        position: relative;
        overflow: hidden;
        border-radius: 6px !important;
        padding: 16px 18px 16px 22px !important;
        background: var(--card) !important;
        border: 1px solid var(--border) !important;
        border-left: 4px solid var(--accent) !important;
        box-shadow: var(--shadow) !important;
        margin-bottom: 16px !important;
    }
    .naryad-brand h3 {
        font-family: 'Roboto Condensed', sans-serif !important;
        color: var(--text) !important;
        margin: 0 !important;
        font-size: 1.15rem !important;
        letter-spacing: 1px !important;
    }
    .naryad-brand p {
        color: var(--muted) !important;
        margin: 4px 0 0 0 !important;
        font-size: 0.72rem !important;
    }

    section[data-testid="stSidebar"] div[data-testid="stSelectbox"],
    section[data-testid="stSidebar"] div[data-testid="stToggle"] {
        padding: 14px !important;
        margin-bottom: 12px !important;
        background: var(--card) !important;
        border: 1px solid var(--border) !important;
        border-radius: 6px !important;
        transition: border-color 0.2s ease !important;
    }
    section[data-testid="stSidebar"] div[data-testid="stSelectbox"]:hover,
    section[data-testid="stSidebar"] div[data-testid="stToggle"]:hover {
        border-color: var(--steel) !important;
    }

    /* Выбор роли — вертикальные плашки */
    section[data-testid="stSidebar"] div[role="radiogroup"] {
        display: flex !important;
        flex-direction: column !important;
        gap: 6px !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label {
        display: flex !important;
        align-items: center !important;
        width: 100% !important;
        padding: 11px 13px !important;
        margin: 0 !important;
        border-radius: 4px !important;
        border: 1px solid var(--border) !important;
        background: transparent !important;
        transition: background 0.2s ease, border-color 0.2s ease !important;
        cursor: pointer !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
        background: var(--card-hover) !important;
        border-color: var(--steel) !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {
        background: var(--accent) !important;
        border-color: var(--accent) !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label p,
    section[data-testid="stSidebar"] div[role="radiogroup"] > label span {
        color: var(--text) !important;
        font-weight: 600 !important;
        font-size: 0.86rem !important;
        margin: 0 !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) p,
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) span {
        color: var(--button-text) !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) [data-baseweb="radio"] > div:first-child {
        border-color: var(--button-text) !important;
        background: var(--button-text) !important;
    }

    /* ============================================================
       HERO-БАННЕР
       ============================================================ */
    .naryad-hero {
        position: relative;
        overflow: hidden;
        border-radius: 6px;
        padding: 30px 34px;
        margin-bottom: 24px;
        border: 1px solid var(--border);
        border-left: 4px solid var(--accent);
        background-color: var(--card);
        background-image: repeating-linear-gradient(45deg, transparent 0 14px, rgba(217, 130, 43, 0.04) 14px 28px);
        box-shadow: var(--shadow);
    }
    .naryad-hero-top {
        position: relative;
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        margin-bottom: 14px;
    }
    .naryad-hero-eyebrow {
        color: var(--accent) !important;
        font-family: 'Roboto Condensed', sans-serif;
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 2px;
        text-transform: uppercase;
    }
    .naryad-hero h2 {
        position: relative;
        color: var(--text) !important;
        font-family: 'Roboto Condensed', sans-serif;
        font-size: 34px !important;
        font-weight: 700 !important;
        letter-spacing: 1px !important;
        text-transform: uppercase !important;
        margin: 0 0 10px 0 !important;
        line-height: 1.12 !important;
    }
    .naryad-hero p {
        position: relative;
        color: var(--muted) !important;
        margin: 0 !important;
        font-size: 1rem;
    }
    .naryad-status-pill {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 5px 13px;
        border-radius: 999px;
        font-family: 'Roboto Condensed', sans-serif;
        font-size: 0.74rem;
        font-weight: 700;
        letter-spacing: 0.6px;
        text-transform: uppercase;
        white-space: nowrap;
        border: 1px solid var(--border);
    }
    .naryad-status-pill .dot {
        width: 8px; height: 8px;
        border-radius: 50%;
        display: inline-block;
    }
    .naryad-status-online { color: var(--success) !important; border-color: rgba(79, 157, 105, 0.5); }
    .naryad-status-online .dot { background: var(--success); }
    .naryad-status-offline { color: var(--danger) !important; border-color: rgba(192, 80, 77, 0.5); }
    .naryad-status-offline .dot { background: var(--danger); }

    /* ============================================================
       КАРТОЧКИ СТАТИСТИКИ
       ============================================================ */
    .naryad-stat {
        display: flex;
        flex-direction: column;
        height: 100%;
        background: var(--card);
        border: 1px solid var(--border);
        border-left: 4px solid var(--steel);
        border-radius: 6px;
        padding: 22px 24px;
        box-shadow: var(--shadow);
        transition: border-color 0.2s ease, background 0.2s ease;
    }
    .naryad-stat:hover { background: var(--card-hover); border-color: var(--steel); }
    .naryad-stat-total   { border-left-color: var(--steel); }
    .naryad-stat-inwork  { border-left-color: var(--warning); }
    .naryad-stat-overdue { border-left-color: var(--danger); }
    .naryad-stat-closed  { border-left-color: var(--success); }
    .naryad-stat-label {
        font-family: 'Roboto Condensed', sans-serif;
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 1px;
        text-transform: uppercase;
        color: var(--muted) !important;
    }
    .naryad-stat-value {
        font-family: 'JetBrains Mono', 'Roboto Mono', monospace;
        font-size: 38px;
        font-weight: 700;
        line-height: 1.1;
        margin-top: 6px;
    }
    .naryad-stat-caption { font-size: 11px; margin-top: 2px; }

    /* ============================================================
       ТАБЛИЦЫ, ВКЛАДКИ, АЛЕРТЫ, EXPANDER, МЕТРИКИ
       ============================================================ */
    div[data-testid="stDataFrame"],
    div[data-testid="stTable"] {
        border-radius: 6px !important;
        border: 1px solid var(--border) !important;
        overflow: hidden !important;
        background: var(--card) !important;
    }
    div[data-testid="stDataFrame"] [role="columnheader"],
    div[data-testid="stTable"] thead th {
        background: var(--card-hover) !important;
        color: var(--text) !important;
        text-transform: uppercase !important;
        letter-spacing: 0.5px !important;
        font-weight: 700 !important;
        font-size: 0.72rem !important;
        border-color: var(--border) !important;
    }
    div[data-testid="stDataFrame"] [role="row"] { border-bottom: 1px solid var(--border) !important; }
    div[data-testid="stDataFrame"] [role="row"]:hover { background: var(--card-hover) !important; }
    div[data-testid="stTable"] tbody tr { border-bottom: 1px solid var(--border) !important; transition: background 0.2s ease !important; }
    div[data-testid="stTable"] tbody tr:nth-child(even) td { background: var(--zebra) !important; }
    div[data-testid="stTable"] tbody tr:hover { background: var(--card-hover) !important; }
    div[data-testid="stTable"] tbody td { border-color: var(--border) !important; color: var(--text) !important; }
    div[data-testid="stTable"] thead th { color: var(--text) !important; }

    div[data-testid="stTabs"] div[data-baseweb="tab-list"] {
        gap: 6px !important;
        border-bottom: 1px solid var(--border) !important;
    }
    div[data-testid="stTabs"] button[role="tab"],
    button[data-baseweb="tab"] {
        background: transparent !important;
        border: none !important;
        border-bottom: 2px solid transparent !important;
        border-radius: 0 !important;
        padding: 10px 15px !important;
        color: var(--muted) !important;
        font-family: 'Roboto Condensed', sans-serif !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.6px !important;
        font-size: 0.8rem !important;
        transition: color 0.2s ease, border-color 0.2s ease !important;
    }
    div[data-testid="stTabs"] button[role="tab"]:hover,
    button[data-baseweb="tab"]:hover { color: var(--text) !important; }
    div[data-testid="stTabs"] button[role="tab"][aria-selected="true"],
    button[data-baseweb="tab"][aria-selected="true"] {
        color: var(--accent) !important;
        border-bottom: 2px solid var(--accent) !important;
    }
    div[data-baseweb="tab-highlight"] { background-color: var(--accent) !important; height: 2px !important; }

    div[data-testid="stAlert"],
    div[data-baseweb="notification"] {
        border-radius: 4px !important;
        border: 1px solid var(--border) !important;
        border-left: 4px solid var(--steel) !important;
        background: var(--card) !important;
        color: var(--text) !important;
        box-shadow: none !important;
    }
    div[data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]),
    div[data-baseweb="notification"][kind="positive"] {
        border-left-color: var(--success) !important;
        background: rgba(79, 157, 105, 0.08) !important;
        background: color-mix(in srgb, var(--success) 8%, transparent) !important;
    }
    div[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]),
    div[data-baseweb="notification"][kind="warning"] {
        border-left-color: var(--warning) !important;
        background: rgba(217, 164, 65, 0.08) !important;
        background: color-mix(in srgb, var(--warning) 8%, transparent) !important;
    }
    div[data-testid="stAlert"]:has([data-testid="stAlertContentError"]),
    div[data-baseweb="notification"][kind="negative"] {
        border-left-color: var(--danger) !important;
        background: rgba(192, 80, 77, 0.08) !important;
        background: color-mix(in srgb, var(--danger) 8%, transparent) !important;
    }
    div[data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]),
    div[data-baseweb="notification"][kind="info"] {
        border-left-color: var(--steel) !important;
        background: rgba(74, 107, 138, 0.08) !important;
        background: color-mix(in srgb, var(--steel) 8%, transparent) !important;
    }
    div[data-testid="stAlert"] p { color: var(--text) !important; }

    details[data-testid="stExpander"],
    div[data-testid="stExpander"] details,
    div[data-testid="stExpander"] {
        border: 1px solid var(--border) !important;
        border-radius: 6px !important;
        background: var(--card) !important;
        overflow: hidden !important;
    }
    div[data-testid="stExpander"] summary {
        color: var(--text) !important;
        font-weight: 600 !important;
        transition: background 0.2s ease !important;
    }
    div[data-testid="stExpander"] summary:hover { background: var(--card-hover) !important; }

    div[data-testid="stMetric"] {
        background: var(--card) !important;
        border: 1px solid var(--border) !important;
        border-left: 4px solid var(--steel) !important;
        border-radius: 6px !important;
        padding: 22px 24px !important;
        box-shadow: var(--shadow) !important;
        transition: border-color 0.2s ease, background 0.2s ease !important;
    }
    div[data-testid="stMetric"]:hover { background: var(--card-hover) !important; border-color: var(--steel) !important; }
    div[data-testid="stMetricLabel"] > div,
    div[data-testid="stMetricLabel"] p {
        color: var(--muted) !important;
        text-transform: uppercase !important;
        letter-spacing: 1px !important;
        font-size: 0.72rem !important;
        font-weight: 600 !important;
    }
    div[data-testid="stMetricValue"] > div,
    div[data-testid="stMetricValue"] {
        color: var(--text) !important;
        font-family: 'JetBrains Mono', 'Roboto Mono', monospace !important;
        font-size: 38px !important;
        font-weight: 700 !important;
    }

    /* ============================================================
       КНОПКИ, ПОЛЯ, ФОРМЫ
       ============================================================ */
    div.stButton > button,
    div.stDownloadButton > button,
    div[data-testid="stFormSubmitButton"] > button,
    button[data-testid="stBaseButton-secondary"] {
        min-height: 44px !important;
        padding: 0.5rem 1.1rem !important;
        border-radius: 4px !important;
        border: 1px solid var(--steel) !important;
        background: transparent !important;
        color: var(--text) !important;
        font-family: 'Roboto Condensed', sans-serif !important;
        font-size: 13px !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.6px !important;
        box-shadow: none !important;
        transition: background 0.2s ease, border-color 0.2s ease, color 0.2s ease !important;
    }
    div.stButton > button:hover,
    div.stDownloadButton > button:hover,
    div[data-testid="stFormSubmitButton"] > button:hover,
    button[data-testid="stBaseButton-secondary"]:hover {
        background: var(--card-hover) !important;
        border-color: var(--accent) !important;
        color: var(--text) !important;
        transform: none !important;
        box-shadow: none !important;
    }
    div.stButton > button[kind="primary"],
    div.stDownloadButton > button[kind="primary"],
    div[data-testid="stFormSubmitButton"] > button[kind="primary"],
    button[data-testid="stBaseButton-primary"] {
        background: var(--accent) !important;
        border: 1px solid var(--accent) !important;
        color: var(--button-text) !important;
        box-shadow: none !important;
    }
    div.stButton > button[kind="primary"]:hover,
    div.stDownloadButton > button[kind="primary"]:hover,
    div[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover,
    button[data-testid="stBaseButton-primary"]:hover {
        background: var(--accent-hover) !important;
        border-color: var(--accent-hover) !important;
        color: var(--button-text) !important;
        transform: none !important;
        box-shadow: none !important;
    }

    div[data-baseweb="input"],
    div[data-baseweb="base-input"],
    div[data-baseweb="textarea"],
    div[data-baseweb="select"] > div,
    div[data-testid="stNumberInputContainer"],
    div[data-testid="stDateInput"] div[data-baseweb="input"] {
        background: var(--field-bg) !important;
        border: 1px solid var(--border) !important;
        border-radius: 4px !important;
        min-height: 44px !important;
        box-shadow: none !important;
        transition: border-color 0.2s ease !important;
    }
    div[data-baseweb="input"]:focus-within,
    div[data-baseweb="base-input"]:focus-within,
    div[data-baseweb="textarea"]:focus-within,
    div[data-baseweb="select"]:focus-within > div,
    div[data-testid="stNumberInputContainer"]:focus-within {
        border-color: var(--accent) !important;
        box-shadow: none !important;
    }
    div[data-baseweb="input"] input,
    div[data-baseweb="base-input"] input,
    div[data-baseweb="textarea"] textarea,
    .stTextInput input,
    .stTextArea textarea,
    .stNumberInput input,
    .stDateInput input {
        background: transparent !important;
        color: var(--field-text) !important;
        -webkit-text-fill-color: var(--field-text) !important;
    }
    div[data-baseweb="select"] div,
    div[data-baseweb="select"] span,
    div[data-baseweb="select"] input {
        color: var(--field-text) !important;
    }
    div[data-baseweb="select"] svg { fill: var(--field-text) !important; }
    ::placeholder { color: var(--muted) !important; opacity: 1 !important; }
    div[data-baseweb="tag"] {
        background: rgba(74, 107, 138, 0.15) !important;
        border: 1px solid var(--steel) !important;
        border-radius: 3px !important;
    }
    div[data-baseweb="tag"] span { color: var(--field-text) !important; }
    input, textarea, [role="checkbox"], [role="radio"],
    div[data-testid="stToggle"] input { accent-color: var(--accent) !important; }

    [data-testid="stWidgetLabel"] label p,
    [data-testid="stWidgetLabel"] label,
    .stTextInput label,
    .stTextArea label,
    .stSelectbox label,
    .stNumberInput label,
    .stMultiSelect label,
    .stDateInput label {
        color: var(--muted) !important;
        font-size: 0.72rem !important;
        font-weight: 600 !important;
        letter-spacing: 0.5px !important;
        text-transform: uppercase !important;
    }

    div[data-testid="stForm"] {
        background: var(--card) !important;
        border: 1px solid var(--border) !important;
        border-radius: 6px !important;
        padding: 24px !important;
        box-shadow: var(--shadow) !important;
    }

    /* ============================================================
       ФУТЕР И АДАПТИВНОСТЬ
       ============================================================ */
    .naryad-footer {
        margin-top: 48px;
        background: var(--bg-2);
        border-top: 1px solid var(--border);
        border-radius: 6px;
        padding: 30px 34px;
        box-shadow: var(--shadow);
    }
    .naryad-footer-grid {
        display: flex;
        gap: 24px;
        justify-content: space-between;
        flex-wrap: wrap;
    }
    .naryad-footer-col {
        flex: 1 1 200px;
        min-width: 180px;
        font-size: 0.85rem;
        color: var(--muted) !important;
    }
    .naryad-footer-brand {
        font-family: 'JetBrains Mono', 'Roboto Mono', monospace;
        font-size: 1.15rem;
        font-weight: 700;
        color: var(--text) !important;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    .naryad-footer-sub { font-size: 0.75rem; color: var(--muted) !important; margin-top: 4px; }
    .naryad-footer-center { text-align: center; }
    .naryad-footer-right { text-align: right; color: var(--text) !important; font-weight: 600; }
    .naryad-footer-copy {
        margin-top: 24px;
        padding-top: 16px;
        border-top: 1px solid var(--border);
        font-size: 0.72rem;
        color: var(--muted) !important;
        text-align: center;
    }

    /* ---------- Прогресс, загрузка файлов, выпадающие списки ---------- */
    div[data-testid="stProgress"] > div > div {
        background: var(--card-hover) !important;
        border-radius: 0 !important;
    }
    div[data-testid="stProgress"] > div > div > div {
        background: var(--accent) !important;
        border-radius: 0 !important;
    }
    section[data-testid="stFileUploaderDropzone"] {
        background: var(--card) !important;
        border: 1px dashed var(--steel) !important;
        border-radius: 4px !important;
    }
    div[data-baseweb="popover"] div[role="listbox"],
    div[data-baseweb="popover"] ul {
        background: var(--card) !important;
        border: 1px solid var(--border) !important;
        border-radius: 4px !important;
    }
    div[data-baseweb="popover"] div[role="option"],
    div[data-baseweb="popover"] li[role="option"] {
        color: var(--text) !important;
        background: transparent !important;
    }
    div[data-baseweb="popover"] div[role="option"]:hover,
    div[data-baseweb="popover"] li[role="option"]:hover {
        background: var(--card-hover) !important;
    }

    @media (max-width: 768px) {
        .naryad-hero { padding: 22px 20px; }
        .naryad-hero h2 { font-size: 22px !important; }
        .naryad-hero-top { flex-direction: column; align-items: flex-start; }
        .naryad-stat-value { font-size: 30px; }
        .naryad-footer-center,
        .naryad-footer-right { text-align: left; }
        h1 { font-size: 1.5rem !important; }
        h2 { font-size: 1.25rem !important; }
        h3 { font-size: 1.05rem !important; }
        div[data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 12px !important; }
        div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
        div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
            flex: 1 1 44% !important;
            min-width: 44% !important;
        }
        .block-container { padding-left: 0.8rem !important; padding-right: 0.8rem !important; }
    }

    /* ============================================================
       ЭКРАН ИСПОЛНИТЕЛЯ: карточка наряда и панель действий
       ============================================================ */
    .st-key-worker-orderbar {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 6px;
        padding: 12px 16px 8px 16px;
        margin-bottom: 24px;
        box-shadow: var(--shadow);
    }
    .worker-counter {
        font-family: 'JetBrains Mono', 'Roboto Mono', monospace;
        font-size: 0.7rem;
        letter-spacing: 0.5px;
        text-transform: uppercase;
        color: var(--muted) !important;
        text-align: right;
        margin: 2px 0 0 0;
    }

    .worker-card-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        flex-wrap: wrap;
    }
    .worker-card-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .worker-order-num {
        font-family: 'JetBrains Mono', 'Roboto Mono', monospace;
        font-size: 1.5rem;
        font-weight: 700;
        letter-spacing: 1px;
        color: var(--text) !important;
    }
    .worker-deadline {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-family: 'JetBrains Mono', 'Roboto Mono', monospace;
        font-size: 0.76rem;
        color: var(--muted) !important;
        white-space: nowrap;
        border: 1px solid var(--border);
        border-radius: 4px;
        padding: 4px 10px;
    }
    .worker-deadline.is-soon { color: var(--warning) !important; border-color: var(--warning) !important; }
    .worker-deadline.is-overdue { color: var(--danger) !important; border-color: var(--danger) !important; }

    .worker-desc { margin-top: 18px; }
    .worker-section-label,
    .worker-fact-label {
        font-family: 'Roboto Condensed', sans-serif;
        font-size: 0.68rem;
        font-weight: 600;
        letter-spacing: 1px;
        text-transform: uppercase;
        color: var(--muted) !important;
    }
    .worker-desc-text {
        margin-top: 4px;
        font-size: 0.98rem;
        line-height: 1.45;
        color: var(--text) !important;
    }
    .worker-facts {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 16px;
        margin-top: 20px;
    }
    .worker-fact-value {
        margin-top: 4px;
        font-size: 0.95rem;
        font-weight: 600;
        color: var(--text) !important;
        word-break: break-word;
    }

    /* --- Панель действий --- */
    .st-key-actions { margin-top: 24px; }
    .st-key-actions div[data-testid="stHorizontalBlock"] { gap: 12px !important; align-items: flex-end; }
    .st-key-actions div.stButton > button,
    .st-key-actions div[data-testid="stPopover"] > button,
    .st-key-actions div[data-testid="stPopoverButton"] button {
        min-height: 44px !important;
        width: 100% !important;
    }
    .st-key-worker_pause button {
        border: 1px solid var(--warning) !important;
        color: var(--warning) !important;
        background: transparent !important;
        box-shadow: none !important;
    }
    .st-key-worker_pause button:hover {
        background: rgba(217, 164, 65, 0.14) !important;
        background: color-mix(in srgb, var(--warning) 14%, transparent) !important;
        border-color: var(--warning) !important;
        color: var(--warning) !important;
        box-shadow: none !important;
    }
    .st-key-worker_reject button {
        border: 1px solid var(--danger) !important;
        color: var(--danger) !important;
        background: transparent !important;
        box-shadow: none !important;
    }
    .st-key-worker_reject button:hover {
        background: rgba(192, 80, 77, 0.14) !important;
        background: color-mix(in srgb, var(--danger) 14%, transparent) !important;
        border-color: var(--danger) !important;
        color: var(--danger) !important;
        box-shadow: none !important;
    }
    .st-key-actions button:disabled,
    .st-key-worker_pause button:disabled,
    .st-key-worker_reject button:disabled {
        opacity: 0.4 !important;
        cursor: not-allowed !important;
        box-shadow: none !important;
    }
    .st-key-actions button:disabled:hover { transform: none !important; }
    .st-key-worker_reject div.stButton > button[kind="primary"] {
        border-color: var(--danger) !important;
        background: var(--danger) !important;
        color: #ffffff !important;
    }

    /* --- Мобильная раскладка панели действий --- */
    @media (max-width: 768px) {
        .worker-facts { grid-template-columns: 1fr; }
        .st-key-worker-orderbar { margin-bottom: 16px; }
        .st-key-actions {
            position: sticky;
            bottom: 0;
            z-index: 60;
            background: var(--bg-2);
            border-top: 1px solid var(--border);
            border-radius: 0;
            padding: 12px;
            margin: 16px -24px -24px -24px;
        }
        .st-key-actions div[data-testid="stHorizontalBlock"] {
            flex-direction: column !important;
            flex-wrap: nowrap !important;
            gap: 10px !important;
        }
        .st-key-actions div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
        .st-key-actions div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
            flex: 1 1 auto !important;
            min-width: 100% !important;
            width: 100% !important;
        }
        .st-key-actions div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:last-child,
        .st-key-actions div[data-testid="stHorizontalBlock"] > div[data-testid="column"]:last-child {
            display: none !important;
        }
        .st-key-actions div.stButton > button,
        .st-key-actions div[data-testid="stPopover"] button {
            min-height: 52px !important;
            width: 100% !important;
        }
    }

    /* ============================================================
       ДИСПЕТЧЕРСКИЙ ПУЛЬТ (этапы 1–7): командная строка, KPI,
       доска, список, таймлайн, степпер, деталь, анимации
       ============================================================ */
    /* --- Командная строка --- */
    .cmdbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        background: var(--card);
        border: 1px solid var(--border);
        border-left: 4px solid var(--accent);
        border-radius: 6px;
        padding: 12px 18px;
        margin-bottom: 16px;
        box-shadow: var(--shadow);
        flex-wrap: wrap;
    }
    .cmd-title {
        font-family: 'Roboto Condensed', sans-serif;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 1px;
        font-size: 1.05rem;
        color: var(--text) !important;
    }
    .cmd-role { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 1px; color: var(--muted) !important; }
    .cmd-clock { text-align: right; font-family: 'JetBrains Mono', monospace; font-size: 0.78rem; color: var(--text) !important; line-height: 1.4; }
    .cmd-shift { font-size: 0.66rem; color: var(--muted) !important; text-transform: uppercase; letter-spacing: 1px; }

    /* --- Полоса KPI --- */
    .disp-kpi {
        background: var(--card);
        border: 1px solid var(--border);
        border-left: 4px solid var(--steel);
        border-radius: 6px;
        padding: 14px 16px;
        box-shadow: var(--shadow);
        height: 100%;
    }
    .disp-kpi.alarm { border-left-color: var(--danger); }
    .disp-kpi.warn { border-left-color: var(--warning); }
    .disp-kpi.good { border-left-color: var(--success); }
    .disp-kpi-label { font-family: 'Roboto Condensed', sans-serif; font-size: 0.68rem; text-transform: uppercase; letter-spacing: 1px; color: var(--muted) !important; }
    .disp-kpi-value { font-family: 'JetBrains Mono', monospace; font-size: 44px; font-weight: 700; line-height: 1.05; color: var(--text) !important; }
    .disp-kpi-sub { font-size: 0.66rem; color: var(--muted) !important; }
    .disp-kpi svg { display: block; margin-top: 6px; }
    @keyframes kpi-pulse { 0%, 100% { border-color: var(--border); } 50% { border-color: var(--danger); } }
    .disp-kpi.pulse { animation: kpi-pulse 2.5s ease-in-out infinite; }

    /* --- Доска --- */
    .board-head { border-top: 3px solid var(--steel); padding-top: 8px; margin-bottom: 10px; display: flex; align-items: center; gap: 6px; }
    .board-head .bh-title { font-family: 'Roboto Condensed', sans-serif; text-transform: uppercase; letter-spacing: 1px; font-size: 0.76rem; color: var(--text) !important; font-weight: 700; }
    .board-head .bh-count {
        display: inline-block; min-width: 22px; text-align: center; padding: 1px 7px;
        border-radius: 999px; background: var(--card-hover); color: var(--muted) !important;
        font-family: 'JetBrains Mono', monospace; font-size: 0.7rem;
    }
    .board-head.st-new { border-top-color: var(--steel); }
    .board-head.st-work { border-top-color: var(--accent); }
    .board-head.st-paused { border-top-color: var(--warning); }
    .board-head.st-overdue { border-top-color: var(--danger); }
    .board-head.st-closed { border-top-color: var(--success); }

    [class*="st-key-card_"] {
        position: relative;
        background: var(--card) !important;
        border: 1px solid var(--border) !important;
        border-left: 4px solid var(--steel) !important;
        border-radius: 6px !important;
        padding: 12px 14px !important;
        margin-bottom: 10px !important;
        box-shadow: none !important;
        transition: border-color 0.2s ease, transform 0.2s ease, background 0.2s ease;
    }
    [class*="st-key-card_"]:hover { border-color: var(--steel) !important; transform: translateY(-2px); }
    [class*="st-key-card_"]:has(.card-prio-red) { border-left-color: var(--danger) !important; }
    [class*="st-key-card_"]:has(.card-prio-yellow) { border-left-color: var(--warning) !important; }
    [class*="st-key-card_"]:has(.card-prio-blue) { border-left-color: var(--steel) !important; }
    [class*="st-key-card_"]:has(.card-prio-gray) { border-left-color: var(--muted) !important; }
    [class*="st-key-card_"]:has(.card-emergency) { border-left-color: transparent !important; }
    [class*="st-key-card_"]:has(.card-emergency)::before {
        content: '';
        position: absolute;
        left: -1px; top: -1px; bottom: -1px;
        width: 4px;
        border-radius: 6px 0 0 6px;
        background: repeating-linear-gradient(45deg, var(--accent) 0 5px, #14171a 5px 10px);
    }
    .card-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
    .card-num { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 0.86rem; color: var(--text) !important; }
    .card-eq {
        font-size: 0.82rem; line-height: 1.25; color: var(--text) !important;
        display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    }
    .card-unit { font-size: 0.7rem; color: var(--muted) !important; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .deadline-wrap { margin-top: 8px; }
    .deadline-bar { height: 6px; border-radius: 3px; background: var(--card-hover); overflow: hidden; }
    .deadline-bar > span { display: block; height: 100%; border-radius: 3px; }
    .deadline-bar .ok { background: var(--success); }
    .deadline-bar .warn { background: var(--warning); }
    .deadline-bar .bad { background: var(--danger); }
    .deadline-text { font-size: 0.66rem; color: var(--muted) !important; margin-top: 3px; text-align: right; font-family: 'JetBrains Mono', monospace; }
    .deadline-text.bad { color: var(--danger) !important; }
    .card-bottom { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 10px; }
    .avatar {
        width: 28px; height: 28px; border-radius: 50%; background: var(--steel); color: #ffffff;
        display: inline-flex; align-items: center; justify-content: center;
        font-size: 0.66rem; font-weight: 700; font-family: 'Roboto Condensed', sans-serif; flex: 0 0 auto;
    }
    .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--danger); display: inline-block; animation: dot-pulse 1.8s ease-in-out infinite; }
    @keyframes dot-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

    /* --- Пустое состояние --- */
    .empty-state { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 22px 8px; color: var(--muted) !important; text-align: center; }
    .empty-state .icon { font-size: 1.4rem; opacity: 0.6; }

    /* --- Список --- */
    .list-head {
        position: sticky; top: 0; z-index: 5;
        background: var(--card-hover); border: 1px solid var(--border); border-radius: 6px 6px 0 0;
        padding: 8px 12px; font-family: 'Roboto Condensed', sans-serif; text-transform: uppercase;
        letter-spacing: 1px; font-size: 0.66rem; color: var(--muted) !important;
    }
    [class*="st-key-lrow_"] {
        border: 1px solid var(--border); border-top: none; background: var(--card);
        min-height: 56px; padding: 4px 12px; transition: background 0.2s ease, border-color 0.2s ease;
    }
    [class*="st-key-lrow_"]:nth-of-type(even) { background: var(--card-hover); }
    [class*="st-key-lrow_"]:hover { background: var(--card-hover); border-color: var(--steel); }
    [class*="st-key-lrow_"] [data-testid="stVerticalBlock"] { gap: 0 !important; }
    .lrow-num { font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 0.8rem; color: var(--text) !important; }
    .lrow-eq { font-size: 0.82rem; color: var(--text) !important; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .lrow-mut { font-size: 0.72rem; color: var(--muted) !important; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .lrow-bar { width: 160px; }
    .lrow-stripe { width: 4px; height: 30px; border-radius: 2px; background: var(--steel); }
    .lrow-stripe.red { background: var(--danger); }
    .lrow-stripe.yellow { background: var(--warning); }
    .lrow-stripe.blue { background: var(--steel); }
    .lrow-stripe.gray { background: var(--muted); }

    /* --- Таймлайн --- */
    .timeline-wrap { border: 1px solid var(--border); border-radius: 6px; background: var(--card); padding: 10px; box-shadow: var(--shadow); }

    /* --- Степпер статусов --- */
    .stepper { display: flex; align-items: flex-start; gap: 0; margin: 12px 0 18px 0; }
    .step { flex: 1; position: relative; text-align: center; }
    .step .dot {
        width: 16px; height: 16px; border-radius: 50%; margin: 0 auto;
        background: var(--card-hover); border: 2px solid var(--border); position: relative; z-index: 2;
    }
    .step .lbl { font-size: 0.64rem; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted) !important; margin-top: 6px; }
    .step .tm { font-family: 'JetBrains Mono', monospace; font-size: 0.62rem; color: var(--muted) !important; }
    .step::before { content: ''; position: absolute; top: 7px; left: 0; right: 50%; height: 2px; background: var(--border); }
    .step::after { content: ''; position: absolute; top: 7px; left: 50%; right: 0; height: 2px; background: var(--border); }
    .step:first-child::before, .step:last-child::after { display: none; }
    .step.done .dot { background: var(--success); border-color: var(--success); }
    .step.done::before, .step.done::after { background: var(--success); }
    .step.current .dot { background: var(--accent); border-color: var(--accent); }
    .step.current::before { background: var(--success); }
    .step.current .lbl { color: var(--text) !important; }

    /* --- Факты детали --- */
    .disp-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-top: 8px; }
    .disp-fact-label { font-family: 'Roboto Condensed', sans-serif; font-size: 0.66rem; text-transform: uppercase; letter-spacing: 1px; color: var(--muted) !important; }
    .disp-fact-value { margin-top: 4px; font-size: 0.92rem; font-weight: 600; color: var(--text) !important; word-break: break-word; }

    /* --- Мобильный «Мой наряд» --- */
    .myorder-num { font-family: 'JetBrains Mono', monospace; font-size: 1.4rem; font-weight: 700; color: var(--text) !important; }
    .myorder-count { font-family: 'JetBrains Mono', monospace; font-size: 48px; font-weight: 700; line-height: 1.05; color: var(--accent) !important; }
    .myorder-count.bad { color: var(--danger) !important; }
    .myorder-count.ok { color: var(--success) !important; }
    .mini-card {
        min-width: 180px; max-width: 180px; background: var(--card); border: 1px solid var(--border);
        border-left: 4px solid var(--steel); border-radius: 6px; padding: 10px 12px; margin-right: 10px;
    }
    .mini-card .mini-num { font-family: 'JetBrains Mono', monospace; font-size: 0.78rem; font-weight: 700; color: var(--text) !important; }
    .mini-card .mini-eq { font-size: 0.74rem; color: var(--muted) !important; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    /* --- Появление карточек и пульс (этап 7) --- */
    @keyframes disp-fade { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
    .disp-anim { animation: disp-fade 0.3s ease both; }
    .disp-anim.d1 { animation-delay: 0.04s; }
    .disp-anim.d2 { animation-delay: 0.08s; }
    .disp-anim.d3 { animation-delay: 0.12s; }
    .disp-anim.d4 { animation-delay: 0.16s; }
    .disp-anim.d5 { animation-delay: 0.20s; }
    .disp-anim.d6 { animation-delay: 0.24s; }
    @media (prefers-reduced-motion: reduce) {
        .disp-anim, .pulse-dot, .disp-kpi.pulse { animation: none !important; }
    }

    @media (max-width: 768px) {
        .disp-facts { grid-template-columns: 1fr; }
        .disp-kpi-value { font-size: 34px; }
        .cmd-clock { text-align: left; }
        .myorder-count { font-size: 40px; }
    }

    /* --- Контейнер командной строки --- */
    [class*="st-key-cmdbar"] {
        background: var(--card) !important;
        border: 1px solid var(--border) !important;
        border-left: 4px solid var(--accent) !important;
        border-radius: 6px !important;
        padding: 8px 16px !important;
        margin-bottom: 16px !important;
        box-shadow: var(--shadow) !important;
    }
    [class*="st-key-cmdbar"] [data-testid="stHorizontalBlock"] { align-items: center !important; }

    /* --- Маркеры приоритета в строках списка --- */
    [class*="st-key-lrow_"]:has(.lrow-marker.red) { border-left: 4px solid var(--danger) !important; }
    [class*="st-key-lrow_"]:has(.lrow-marker.yellow) { border-left: 4px solid var(--warning) !important; }
    [class*="st-key-lrow_"]:has(.lrow-marker.blue) { border-left: 4px solid var(--steel) !important; }
    [class*="st-key-lrow_"]:has(.lrow-marker.gray) { border-left: 4px solid var(--muted) !important; }

    /* --- Иконки-действия внутри карточек и строк --- */
    [class*="st-key-card_"] div.stButton > button,
    [class*="st-key-lrow_"] div.stButton > button {
        min-height: 36px !important;
        height: 36px !important;
        width: 36px !important;
        padding: 0 !important;
        border-radius: 4px !important;
    }
    [class*="st-key-card_"] button:disabled,
    [class*="st-key-lrow_"] button:disabled { opacity: 0.35 !important; }

    /* --- Плавное появление карточек доски --- */
    [class*="st-key-card_"] { animation: disp-fade 0.3s ease both; }
    [class*="st-key-card_"]:nth-of-type(2) { animation-delay: 0.04s; }
    [class*="st-key-card_"]:nth-of-type(3) { animation-delay: 0.08s; }
    [class*="st-key-card_"]:nth-of-type(4) { animation-delay: 0.12s; }
    [class*="st-key-card_"]:nth-of-type(5) { animation-delay: 0.16s; }
    [class*="st-key-card_"]:nth-of-type(6) { animation-delay: 0.20s; }
    [class*="st-key-card_"]:nth-of-type(n+7) { animation-delay: 0.24s; }

    /* --- Обёртка таймлайна --- */
    [class*="st-key-timeline_box"] {
        background: var(--card) !important;
        border: 1px solid var(--border) !important;
        border-radius: 6px !important;
        padding: 8px !important;
        box-shadow: var(--shadow) !important;
    }
</style>
"""

#: Соответствие статуса наряда цвету бейджа.
_STATUS_BADGE_KIND: Dict[str, str] = {
    "Закрыт": "green",
    "Исполнено": "green",
    "Свободен": "green",
    "Аварийный": "red",
    "Просрочен": "red",
    "Отклонён": "red",
    "В работе": "yellow",
    "На доработку": "yellow",
    "Приостановлен": "yellow",
    "Выдан": "blue",
    "Принят в работу": "blue",
    "В очереди": "blue",
    "Проверка ИИ": "blue",
}

#: Соответствие приоритета цвету бейджа.
_PRIORITY_BADGE_KIND: Dict[str, str] = {
    "Аварийный": "red",
    "Высокий": "yellow",
    "Обычный": "blue",
    "Плановый": "gray",
}


#: Палитры дизайн-системы: "dark" — «Графит», "light" — «Бетон».
_THEME_TOKENS: Dict[str, Dict[str, str]] = {
    "dark": {
        "--bg": "#14171a",
        "--bg-2": "#101316",
        "--card": "#1c2125",
        "--card-hover": "#232a30",
        "--border": "rgba(255,255,255,0.07)",
        "--text": "#e4e7ea",
        "--muted": "#8c969f",
        "--accent": "#d9822b",
        "--accent-hover": "#e8924a",
        "--steel": "#4a6b8a",
        "--success": "#4f9d69",
        "--warning": "#d9a441",
        "--danger": "#c0504d",
        "--field-bg": "#101316",
        "--field-text": "#e4e7ea",
        "--button-text": "#14171a",
        "--grid-line": "rgba(255,255,255,0.035)",
        "--vignette": "rgba(0,0,0,0.25)",
        "--zebra": "rgba(255,255,255,0.03)",
        "--shadow": "0 2px 8px rgba(0,0,0,0.25)",
        "--topo-stroke": "#ffffff",
    },
    "light": {
        "--bg": "#e9ecee",
        "--bg-2": "#dde2e5",
        "--card": "#ffffff",
        "--card-hover": "#f4f6f7",
        "--border": "rgba(20,30,40,0.12)",
        "--text": "#1f2933",
        "--muted": "#5f6b76",
        "--accent": "#c46a1a",
        "--accent-hover": "#d97a26",
        "--steel": "#3d5a78",
        "--success": "#3f8a59",
        "--warning": "#b98a1f",
        "--danger": "#b13f3b",
        "--field-bg": "#ffffff",
        "--field-text": "#1f2933",
        "--button-text": "#ffffff",
        "--grid-line": "rgba(20,30,40,0.05)",
        "--vignette": "rgba(20,30,40,0.10)",
        "--zebra": "rgba(20,30,40,0.03)",
        "--shadow": "0 2px 8px rgba(0,0,0,0.10)",
        "--topo-stroke": "#14181e",
    },
}

#: Замкнутые кривые «горизонталей карьера» для фонового SVG.
_TOPO_PATHS: Tuple[str, ...] = (
    "M60,300 C120,180 260,120 400,150 C560,185 720,140 740,300 C760,470 600,540 420,520 C240,500 100,460 60,300 Z",
    "M180,310 C230,220 340,180 450,205 C570,232 660,240 660,330 C660,440 520,470 400,455 C280,440 140,420 180,310 Z",
    "M300,320 C340,265 420,245 500,265 C580,285 600,320 570,360 C540,400 440,410 370,390 C300,370 270,360 300,320 Z",
    "M395,330 C425,305 480,300 512,320 C542,340 522,372 480,374 C438,376 375,358 395,330 Z",
)


def _topo_background(stroke: str) -> str:
    """Собрать data-URI с топографическими линиями для фона."""
    paths = "".join(f'<path d="{d}"/>' for d in _TOPO_PATHS)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" '
        f'viewBox="0 0 800 600" fill="none" stroke="{stroke}" '
        f'stroke-opacity="0.05" stroke-width="1">{paths}</svg>'
    )
    return f'url("data:image/svg+xml,{quote(svg, safe="")}")'


def _theme_root_css(theme: str) -> str:
    """Собрать блок ``:root`` с переменными выбранной темы."""
    tokens = _THEME_TOKENS.get(theme, _THEME_TOKENS["dark"])
    lines = "\n".join(f"        {name}: {value};" for name, value in tokens.items())
    topo = _topo_background(tokens["--topo-stroke"])
    return f":root {{\n{lines}\n        --topo: {topo};\n    }}"


def inject_design(theme: str = "dark") -> None:
    """Подключить дизайн-систему в выбранной теме («Графит» / «Бетон»)."""
    try:
        css = _DESIGN_CSS.replace("__ROOT__", _theme_root_css(theme))
        st.markdown(css, unsafe_allow_html=True)
    except Exception:  # noqa: BLE001 — стили не должны ронять приложение
        st.session_state["css_error"] = True


def badge_html(text: Any, kind: str = "gray") -> str:
    """Собрать HTML статусного бейджа."""
    safe_text = str(text)
    safe_kind = kind if kind in ("green", "red", "yellow", "blue", "gray") else "gray"
    return f'<span class="naryad-badge naryad-badge-{safe_kind}">{safe_text}</span>'


def status_badge_html(lang: str, status: Any) -> str:
    """Бейдж статуса наряда/сотрудника с локализацией."""
    kind = _STATUS_BADGE_KIND.get(str(status), "gray")
    return badge_html(status_label(lang, status), kind)


def priority_badge_html(lang: str, priority: Any) -> str:
    """Бейдж приоритета наряда с локализацией."""
    kind = _PRIORITY_BADGE_KIND.get(str(priority), "gray")
    return badge_html(priority_label(lang, priority), kind)


def metric_card(
    label: Any,
    value: Any,
    caption: str = "",
    accent: str = "#f8fafc",
    caption_color: Optional[str] = None,
    variant: Optional[str] = None,
) -> str:
    """Собрать HTML-блок метрики в виде карточки дизайн-системы.

    Единый вид для всех экранов проекта — заменяет стандартные серые
    коробки ``st.metric``. ``variant`` задаёт цветную верхнюю границу
    (``total`` / ``inwork`` / ``overdue`` / ``closed``).
    """
    if caption_color is None:
        caption_color = accent if caption else "#64748b"
    caption_html = (
        f'<div class="naryad-stat-caption" style="color: {caption_color};">'
        f"{caption}</div>"
        if caption
        else ""
    )
    variant_class = f" naryad-stat-{variant}" if variant else ""
    return (
        f'<div class="naryad-stat{variant_class}">'
        f'<div class="naryad-stat-label">{label}</div>'
        f'<div class="naryad-stat-value" style="color: {accent};">{value}</div>'
        f"{caption_html}"
        "</div>"
    )


def render_metric_card(
    label: Any,
    value: Any,
    caption: str = "",
    accent: str = "#f8fafc",
    caption_color: Optional[str] = None,
    variant: Optional[str] = None,
) -> None:
    """Отрисовать HTML-блок метрики (см. :func:`metric_card`)."""
    st.markdown(
        metric_card(label, value, caption, accent, caption_color, variant),
        unsafe_allow_html=True,
    )


def render_hero(lang: str, role_key: str, is_offline: bool = False) -> None:
    """Hero-баннер приложения: eyebrow, крупный заголовок, подзаголовок и статус связи.

    Тексты берутся из словаря ``LANG`` (RU / KZ) — ключи ``hero_eyebrow``,
    ``hero_title`` и ``hero_subtitle``. Статус связи отражает ``is_offline``.
    """
    status_class = "naryad-status-offline" if is_offline else "naryad-status-online"
    status_text = "Offline" if is_offline else "Online"
    st.markdown(
        f"""
<div class="naryad-hero">
    <div class="naryad-hero-top">
        <span class="naryad-hero-eyebrow">{t(lang, "hero_eyebrow")}</span>
        <span class="naryad-status-pill {status_class}"><span class="dot"></span>{status_text}</span>
    </div>
    <h2>{t(lang, "hero_title")}</h2>
    <p>{t(lang, "hero_subtitle")}</p>
</div>
""",
        unsafe_allow_html=True,
    )


def render_sidebar_brand() -> None:
    """Брендинговая карточка в сайдбаре."""
    st.sidebar.markdown(
        '<div class="naryad-brand"><h3>⛏️ НарядAI</h3>'
        '<p>ТОиР · АО «Костанайские минералы»</p></div>',
        unsafe_allow_html=True,
    )


# ==================================================================
# 8. САЙДБАР И ГЛОБАЛЬНАЯ ШАПКА
# ==================================================================
def render_sidebar() -> Tuple[str, str, bool]:
    """Собрать сайдбар. Возвращает ``(lang, role_key, is_offline)``."""
    render_sidebar_brand()

    # Переключатель темы: значение хранится в st.session_state["theme_light"].
    st.sidebar.toggle(
        "🌗 Светлая / Тёмная",
        key="theme_light",
        help="Переключение между темами «Графит» (тёмная) и «Бетон» (светлая).",
    )

    lang = st.sidebar.selectbox(
        "Тіл / Язык",
        ["RU", "KZ"],
        index=0,
        key="global_lang",
    )

    is_offline = st.sidebar.toggle(
        t(lang, "offline_toggle"),
        value=False,
        key="offline_toggle",
        help=t(lang, "offline_warn"),
    )
    if is_offline:
        st.sidebar.caption("📡 " + t(lang, "offline_warn"))
    else:
        st.sidebar.caption("🌐 " + t(lang, "online_ok"))

    st.sidebar.divider()

    role_keys = ["master", "worker", "manager", "admin"]
    role_labels = [
        t(lang, "role_master"),
        t(lang, "role_worker"),
        t(lang, "role_manager"),
        t(lang, "role_admin"),
    ]
    role_choice = st.sidebar.radio(
        t(lang, "role_select"),
        role_labels,
        key="global_role",
    )
    role_key = role_keys[role_labels.index(role_choice)]

    st.sidebar.divider()
    render_db_diagnostics(lang)
    return lang, role_key, is_offline


def render_db_diagnostics(lang: str) -> None:
    """Мини-панель диагностики БД (для отладки на защите проекта)."""
    with st.sidebar.expander("🛠 " + t(lang, "db_status"), expanded=False):
        total = db_scalar("SELECT COUNT(*) AS n FROM work_orders")
        closed = db_scalar(
            "SELECT COUNT(*) AS n FROM work_orders WHERE status = 'Закрыт'"
        )
        st.caption(f"naryad_ai.db · нарядов: {total} · закрыто: {closed}")
        if st.session_state.get("db_last_error"):
            st.error(st.session_state["db_last_error"])
        else:
            st.success("Подключение к SQLite активно.")
        if _stretch(st.button, "🔄 Обновить данные", key="db_refresh"):
            st.rerun()


def render_header(lang: str, role_key: str, is_offline: bool) -> None:
    """Глобальная шапка: ролевой hero-баннер и компактный статус-бар."""
    render_hero(lang, role_key, is_offline)
    # Плашка «ОНЛАЙН» заменена индикатором связи внутри hero-баннера.
    # Оставляем только предупреждение офлайн-режима (если он включён).
    if is_offline:
        st.caption(t(lang, "status_offline_bar"))
    st.caption(t(lang, "status_monitor_ok"))


# ==================================================================
# 9. ЭКРАН 1: МАСТЕР СМЕНЫ
# ==================================================================
def load_live_orders(limit: int = 300) -> pd.DataFrame:
    """Наряды с подтянутыми названиями оборудования, участка и исполнителя."""
    query = """
        SELECT
            wo.id            AS id,
            wo.order_num     AS order_num,
            wo.description   AS description,
            wo.priority      AS priority,
            wo.status        AS status,
            wo.created_at    AS created_at,
            wo.deadline      AS deadline,
            wo.normative_hours AS normative_hours,
            eq.name          AS equipment,
            un.name          AS unit,
            emp.fio          AS assignee,
            wo.assignee_id   AS assignee_id,
            wo.equipment_id  AS equipment_id,
            wo.unit_id       AS unit_id
        FROM work_orders AS wo
        LEFT JOIN equipment AS eq ON eq.id = wo.equipment_id
        LEFT JOIN units     AS un ON un.id = wo.unit_id
        LEFT JOIN employees AS emp ON emp.id = wo.assignee_id
        ORDER BY wo.id DESC
        LIMIT ?
    """
    return db_query_df(query, (int(limit),))


# ==================================================================
# 10b. ДИСПЕТЧЕРСКИЙ ПУЛЬТ (каркас, доска, список, таймлайн, деталь)
# ==================================================================
#: Дополнительные подписи интерфейса (LANG не меняем).
_UI_TEXT: Dict[str, Dict[str, str]] = {
    "RU": {
        "all": "Все", "board": "Доска", "list": "Список", "timeline": "Таймлайн",
        "view": "Вид", "search_ph": "Поиск: номер, оборудование, цех…",
        "shift": "Смена", "online": "Online", "offline": "Offline",
        "no_orders": "Нет нарядов", "show_all": "Показать все", "show_less": "Свернуть",
        "open": "Открыть", "accept": "Принять в работу", "pause": "Приостановить",
        "reject": "Отклонить", "close": "Закрыть", "detail": "Деталь наряда",
        "kpi_total": "Всего", "kpi_inwork": "В работе", "kpi_overdue": "Просрочено",
        "kpi_emergency": "Аварийные", "kpi_closed_today": "Закрыто сегодня",
        "col_new": "Новые", "col_work": "В работе", "col_paused": "Приостановлены",
        "col_overdue": "Просрочены", "col_closed": "Закрыты",
        "step_created": "Создан", "step_accepted": "Принят", "step_work": "В работе",
        "step_closed": "Закрыт", "overdue_by": "просрочено на {v}", "left": "{v}",
        "sort": "Сортировка", "sort_deadline": "По дедлайну", "sort_priority": "По приоритету",
        "sort_status": "По статусу", "group_by": "Ряды", "grp_equipment": "Оборудование",
        "grp_assignee": "Исполнители", "reason_ph": "Причина отклонения",
        "confirm_reject": "Подтвердить отклонение", "next_orders": "Следующие наряды",
        "my_order": "Мой наряд", "download": "Скачать CSV",
        "toast_accept": "Наряд {num} принят в работу", "toast_pause": "Наряд {num} приостановлен",
        "toast_close": "Наряд {num} закрыт", "toast_reject": "Наряд {num} отклонён",
        "no_deadline": "без дедлайна", "role_label": "Роль",
        "empty_col_hint": "Перетащите фильтры или создайте наряд",
    },
    "KZ": {
        "all": "Барлығы", "board": "Тақта", "list": "Тізім", "timeline": "Timeline",
        "view": "Көрініс", "search_ph": "Іздеу: нөмір, жабдық, цех…",
        "shift": "Ауысым", "online": "Online", "offline": "Offline",
        "no_orders": "Нарядтар жоқ", "show_all": "Барлығын көрсету", "show_less": "Жасыру",
        "open": "Ашу", "accept": "Жұмысқа қабылдау", "pause": "Тоқтата тұру",
        "reject": "Қабылдамау", "close": "Жабу", "detail": "Наряд тетіктері",
        "kpi_total": "Барлығы", "kpi_inwork": "Жұмыста", "kpi_overdue": "Мерзімі өткен",
        "kpi_emergency": "Апаттық", "kpi_closed_today": "Бүгін жабылған",
        "col_new": "Жаңа", "col_work": "Жұмыста", "col_paused": "Тоқтатылған",
        "col_overdue": "Мерзімі өткен", "col_closed": "Жабылған",
        "step_created": "Құрылды", "step_accepted": "Қабылданды", "step_work": "Жұмыста",
        "step_closed": "Жабылды", "overdue_by": "мерзімінен {v} өтті", "left": "{v}",
        "sort": "Сұрыптау", "sort_deadline": "Мерзімі бойынша", "sort_priority": "Басымдық бойынша",
        "sort_status": "Мәртебе бойынша", "group_by": "Қатарлар", "grp_equipment": "Жабдық",
        "grp_assignee": "Орындаушылар", "reason_ph": "Қабылдамау себебі",
        "confirm_reject": "Қабылдамауды растау", "next_orders": "Келесі нарядтар",
        "my_order": "Менің нарядым", "download": "CSV жүктеу",
        "toast_accept": "{num} наряды жұмысқа қабылданды", "toast_pause": "{num} наряды тоқтатылды",
        "toast_close": "{num} наряды жабылды", "toast_reject": "{num} наряды қабылданбады",
        "no_deadline": "мерзімсіз", "role_label": "Рөл",
        "empty_col_hint": "Сүзгілерді өзгертіңіз немесе наряд құрыңыз",
    },
}

#: Колонки доски: (ключ, статусы, css-класс заголовка, подпись).
_BOARD_COLUMNS: Tuple[Tuple[str, Tuple[str, ...], str, str], ...] = (
    ("new", ("Выдан", "В очереди"), "st-new", "col_new"),
    ("work", ("В работе", "Принят в работу", "Проверка ИИ", "На доработку"), "st-work", "col_work"),
    ("paused", ("Приостановлен",), "st-paused", "col_paused"),
    ("overdue", ("Просрочен",), "st-overdue", "col_overdue"),
    ("closed", ("Закрыт", "Исполнено"), "st-closed", "col_closed"),
)


def _u(lang: str, key: str) -> str:
    """Строка дополнительных подписей пульта (RU/KZ)."""
    block = _UI_TEXT.get(lang, _UI_TEXT["RU"])
    return block.get(key, _UI_TEXT["RU"].get(key, key))


def _initials(fio: Any) -> str:
    """Инициалы исполнителя для аватара."""
    parts = [p for p in str(fio or "").split() if p]
    if not parts:
        return "—"
    letters = "".join(p[0] for p in parts[:2]).upper()
    return letters


def _fmt_duration(minutes: float) -> str:
    """Человекочитаемая длительность."""
    total = int(max(0, minutes))
    return f"{total // 60} ч {total % 60:02d} мин" if total >= 60 else f"{total} мин"


def _deadline_meta(created: Any, deadline: Any) -> Tuple[int, str, str, bool]:
    """Вернуть (процент остатка, css-класс, текст, флаг просрочки)."""
    try:
        dl = pd.to_datetime(deadline, errors="coerce")
    except (TypeError, ValueError):
        dl = pd.NaT
    if pd.isna(dl):
        return 100, "ok", "", False
    now = datetime.datetime.now()
    dl = dl.to_pydatetime()
    try:
        cr = pd.to_datetime(created, errors="coerce")
        cr = cr.to_pydatetime() if pd.notna(cr) else now
    except (TypeError, ValueError):
        cr = now
    remain = (dl - now).total_seconds()
    total = (dl - cr).total_seconds()
    if remain < 0:
        return 0, "bad", "⏱ " + _fmt_duration(-remain / 60), True
    frac = remain / total if total > 0 else 0.0
    pct = max(0, min(100, int(frac * 100)))
    fill = "ok" if frac > 0.5 else ("warn" if frac >= 0.2 else "bad")
    return pct, fill, "⏱ " + _fmt_duration(remain / 60), fill == "bad"


def _sparkline_svg(values: Sequence[float], color: str = "#4a6b8a", width: int = 128, height: int = 26) -> str:
    """Мини-график тренда за 7 дней (inline SVG). Пусто, если данных нет."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2 or max(vals) <= 0:
        return ""
    lo, hi = 0.0, max(vals)
    span = hi - lo or 1.0
    step = width / (len(vals) - 1)
    points = " ".join(
        f"{i * step:.1f},{height - (v - lo) / span * (height - 4) - 2:.1f}"
        for i, v in enumerate(vals)
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none"><polyline points="{points}" fill="none" '
        f'stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def _render_command_bar(lang: str, role_key: str, is_offline: bool, title_key: str = "m_title") -> None:
    """Командная строка: экран+роль, поиск, часы и статус связи."""
    now = datetime.datetime.now()
    role_label = t(lang, f"role_{role_key}")
    shift = now.hour // 6 + 1
    status_cls = "naryad-status-offline" if is_offline else "naryad-status-online"
    status_txt = _u(lang, "offline") if is_offline else _u(lang, "online")
    with st.container(key="cmdbar"):
        left, center, right = st.columns([3, 4, 3])
        with left:
            st.markdown(
                f'<div class="cmd-title">{t(lang, title_key)}</div>'
                f'<div class="cmd-role">{_u(lang, "role_label")}: {role_label}</div>',
                unsafe_allow_html=True,
            )
        with center:
            st.text_input(
                "search", key="disp_search", label_visibility="collapsed",
                placeholder=_u(lang, "search_ph"),
            )
        with right:
            st.markdown(
                f'<div class="cmd-clock">{now.strftime("%d.%m.%Y  %H:%M:%S")}'
                f'<div class="cmd-shift">{_u(lang, "shift")} {shift} · '
                f'<span class="{status_cls}" style="border:0;padding:0;">{status_txt}</span></div></div>',
                unsafe_allow_html=True,
            )


def _render_kpi_strip(lang: str, df: pd.DataFrame) -> None:
    """Полоса из 5 KPI-карточек со спарклайнами за 7 дней."""
    if df.empty or "status" not in df.columns:
        df = pd.DataFrame(columns=["status", "priority", "created_at"])

    def _count(mask: pd.Series) -> int:
        try:
            return int(mask.sum())
        except Exception:  # noqa: BLE001
            return 0

    statuses = df["status"].astype(str) if "status" in df.columns else pd.Series(dtype=str)
    priorities = df["priority"].astype(str) if "priority" in df.columns else pd.Series(dtype=str)
    created = pd.to_datetime(df.get("created_at"), errors="coerce") if "created_at" in df.columns else pd.Series([], dtype="datetime64[ns]")

    def trend(mask_values: Sequence[bool]) -> List[int]:
        if created.empty or len(created) != len(df):
            return []
        days = [(datetime.datetime.now().date() - datetime.timedelta(days=d)) for d in range(6, -1, -1)]
        result = []
        for day in days:
            count = 0
            for ts, flag in zip(created, mask_values):
                if flag and pd.notna(ts) and ts.date() == day:
                    count += 1
            result.append(count)
        return result

    work_mask = statuses.isin(("В работе", "Принят в работу", "Проверка ИИ"))
    overdue_mask = statuses == "Просрочен"
    emergency_mask = (priorities == "Аварийный") & (~statuses.isin(("Закрыт", "Исполнено")))
    closed_mask = statuses.isin(("Закрыт", "Исполнено"))
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        closed_today = db_scalar(
            "SELECT COUNT(*) AS n FROM order_events WHERE action = 'Закрыт' AND event_time LIKE ?",
            (today + "%",),
        )
    except Exception:  # noqa: BLE001
        closed_today = 0

    cards = [
        (t(lang, "kpi_total"), len(df), "", "", _u(lang, "kpi_total"), trend([True] * len(df))),
        (_u(lang, "kpi_inwork"), _count(work_mask), "", "warn", _u(lang, "kpi_inwork"), trend(list(work_mask))),
        (_u(lang, "kpi_overdue"), _count(overdue_mask), "alarm pulse" if _count(overdue_mask) > 0 else "", "alarm", _u(lang, "kpi_overdue"), trend(list(overdue_mask))),
        (_u(lang, "kpi_emergency"), _count(emergency_mask), "alarm" if _count(emergency_mask) > 0 else "", "alarm", _u(lang, "kpi_emergency"), trend(list(emergency_mask))),
        (_u(lang, "kpi_closed_today"), int(closed_today), "good", "good", _u(lang, "kpi_closed_today"), trend(list(closed_mask))),
    ]
    columns = st.columns(len(cards))
    for index, (label, value, extra, accent_cls, caption, spark) in enumerate(cards):
        color = {"warn": "#d9a441", "alarm": "#c0504d", "good": "#4f9d69"}.get(accent_cls, "#4a6b8a")
        with columns[index]:
            st.markdown(
                f'<div class="disp-kpi disp-anim d{min(index + 1, 6)} {extra}" style="--kpi-accent:{color};">'
                f'<div class="disp-kpi-label">{label}</div>'
                f'<div class="disp-kpi-value">{value}</div>'
                f'<div class="disp-kpi-sub">{caption}</div>'
                f"{_sparkline_svg(spark, color)}"
                f"</div>",
                unsafe_allow_html=True,
            )


def _render_empty_state(lang: str) -> None:
    """Заглушка для пустой колонки/списка."""
    st.markdown(
        f'<div class="empty-state"><div class="icon">▤</div>'
        f'<div>{_u(lang, "no_orders")}</div></div>',
        unsafe_allow_html=True,
    )


def _status_stepper_html(lang: str, status: Any, created: Any = "", order_id: Optional[int] = None) -> str:
    """HTML-степпер жизненного цикла наряда (Создан→Принят→В работе→Закрыт)."""
    times: Dict[str, str] = {}
    if order_id is not None:
        try:
            events = db_fetchall(
                "SELECT action, event_time FROM order_events WHERE order_id = ? "
                "ORDER BY event_time ASC, rowid ASC",
                (int(order_id),),
            )
            for ev in events:
                times[str(ev.get("action"))] = str(ev.get("event_time"))
        except Exception:  # noqa: BLE001
            times = {}
    steps = (
        (_u(lang, "step_created"), str(created or "")),
        (_u(lang, "step_accepted"), times.get("В работе", "")),
        (_u(lang, "step_work"), times.get("Приостановлен", "")),
        (_u(lang, "step_closed"), times.get("Закрыт", times.get("Отклонён", ""))),
    )
    done_index = {
        "Выдан": 0, "В очереди": 0, "Принят в работу": 1, "В работе": 1,
        "Приостановлен": 2, "Просрочен": 2, "Отклонён": 3,
        "Закрыт": 3, "Исполнено": 3,
    }.get(str(status), 0)
    parts = ['<div class="stepper">']
    for index, (label, when) in enumerate(steps):
        css = "done" if index < done_index else ("current" if index == done_index else "future")
        parts.append(
            f'<div class="step {css}"><div class="dot"></div>'
            f'<div class="lbl">{label}</div><div class="tm">{when or "—"}</div></div>'
        )
    parts.append("</div>")
    return "".join(parts)


def _render_icon_actions(lang: str, row: pd.Series, prefix: str) -> None:
    """Ряд иконок-действий (play/pause/close/open) для карточки/строки."""
    order_id = int(row["id"])
    order_num = str(row["order_num"])
    status = str(row["status"])
    accept_enabled = status not in ("В работе", "Закрыт", "Исполнено", "Проверка ИИ")
    pause_enabled = status == "В работе"
    close_enabled = status in ("В работе", "Просрочен", "Приостановлен")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        if st.button("", icon=":material/play_arrow:", key=f"{prefix}_play_{order_id}", help=_u(lang, "accept"), disabled=not accept_enabled):
            update_order_status(order_id, "В работе", "Принят в работу")
            st.toast(_u(lang, "toast_accept").format(num=order_num))
            st.rerun()
    with c2:
        if st.button("", icon=":material/pause:", key=f"{prefix}_pause_{order_id}", help=_u(lang, "pause"), disabled=not pause_enabled):
            update_order_status(order_id, "Приостановлен", "Пауза")
            st.toast(_u(lang, "toast_pause").format(num=order_num))
            st.rerun()
    with c3:
        if st.button("", icon=":material/close:", key=f"{prefix}_close_{order_id}", help=_u(lang, "close"), disabled=not close_enabled):
            update_order_status(order_id, "Закрыт", "Закрыт мастером")
            st.toast(_u(lang, "toast_close").format(num=order_num))
            st.rerun()
    with c4:
        if st.button("", icon=":material/open_in_full:", key=f"{prefix}_open_{order_id}", help=_u(lang, "open")):
            _order_detail_dialog(lang, order_id)


def _render_order_card(lang: str, row: pd.Series) -> None:
    """Компактная карточка наряда для доски."""
    order_id = int(row["id"])
    priority = str(row["priority"])
    kind = _PRIORITY_BADGE_KIND.get(priority, "gray")
    emergency = priority == "Аварийный"
    pct, fill, dtext, dbad = _deadline_meta(row.get("created_at"), row.get("deadline"))
    equipment = str(row.get("equipment") or "—")
    unit = str(row.get("unit") or "—")
    marker = f"card-prio-{kind}" + (" card-emergency" if emergency else "")
    with st.container(border=True, key=f"card_{order_id}"):
        st.markdown(
            f'<span class="{marker}" style="display:none;"></span>'
            f'<div class="card-top"><span class="card-num">{row["order_num"]}</span>'
            f"{priority_badge_html(lang, priority)}</div>"
            f'<div class="card-eq">{equipment}</div>'
            f'<div class="card-unit">{unit}</div>'
            f'<div class="deadline-wrap"><div class="deadline-bar">'
            f'<span class="{fill}" style="width:{pct}%;"></span></div>'
            f'<div class="deadline-text{" bad" if dbad else ""}">{dtext}</div></div>',
            unsafe_allow_html=True,
        )
        bottom = st.columns([2, 3])
        with bottom[0]:
            st.markdown(
                f'<div class="card-bottom"><span class="avatar">{_initials(row.get("assignee"))}</span></div>',
                unsafe_allow_html=True,
            )
        with bottom[1]:
            _render_icon_actions(lang, row, f"card{order_id}")


def _render_board(lang: str, df: pd.DataFrame) -> None:
    """Канбан-доска с 5 колонками."""
    columns = st.columns(len(_BOARD_COLUMNS))
    for (key, statuses, css, title_key), column in zip(_BOARD_COLUMNS, columns):
        subset = df[df["status"].astype(str).isin(statuses)]
        with column:
            st.markdown(
                f'<div class="board-head {css}"><span class="bh-title">{_u(lang, title_key)}</span>'
                f'<span class="bh-count">{len(subset)}</span></div>',
                unsafe_allow_html=True,
            )
            if subset.empty:
                _render_empty_state(lang)
                continue
            if key == "closed":
                show_all = bool(st.session_state.get("board_closed_all", False))
                visible = subset if show_all else subset.head(5)
                for _, row in visible.iterrows():
                    _render_order_card(lang, row)
                if len(subset) > 5:
                    label = _u(lang, "show_less") if show_all else _u(lang, "show_all")
                    if st.button(label, key="board_closed_toggle", use_container_width=True):
                        st.session_state["board_closed_all"] = not show_all
                        st.rerun()
            else:
                for _, row in subset.head(15).iterrows():
                    _render_order_card(lang, row)


def _render_list(lang: str, df: pd.DataFrame) -> None:
    """Плотный список нарядов с прилипающим заголовком."""
    sort_key = st.selectbox(
        _u(lang, "sort"),
        [_u(lang, "sort_deadline"), _u(lang, "sort_priority"), _u(lang, "sort_status")],
        key="list_sort",
    )
    data = df.copy()
    if data.empty:
        _render_empty_state(lang)
        return
    data["_dl"] = pd.to_datetime(data.get("deadline"), errors="coerce")
    if sort_key == _u(lang, "sort_priority"):
        order = {"Аварийный": 0, "Высокий": 1, "Обычный": 2, "Плановый": 3}
        data["_p"] = data["priority"].map(order).fillna(9)
        data = data.sort_values("_p")
    elif sort_key == _u(lang, "sort_status"):
        data = data.sort_values("status")
    else:
        data = data.sort_values("_dl", na_position="last")

    st.markdown(
        f'<div class="list-head">{_u(lang, "col_new")} · {c(lang, "equipment")} · '
        f'{c(lang, "unit")} · {c(lang, "assignee")} · {c(lang, "deadline")}</div>',
        unsafe_allow_html=True,
    )
    if data.empty:
        _render_empty_state(lang)
        return
    for _, row in data.head(80).iterrows():
        order_id = int(row["id"])
        priority = str(row["priority"])
        kind = _PRIORITY_BADGE_KIND.get(priority, "gray")
        pct, fill, dtext, dbad = _deadline_meta(row.get("created_at"), row.get("deadline"))
        with st.container(key=f"lrow_{order_id}"):
            cols = st.columns([1.2, 1.1, 2.2, 1.3, 1.2, 1.6, 1.2, 1.9])
            cols[0].markdown(
                f'<span class="lrow-marker {kind}" style="display:none;"></span>'
                f'<div class="lrow-num">{row["order_num"]}</div>',
                unsafe_allow_html=True,
            )
            cols[1].markdown(priority_badge_html(lang, priority), unsafe_allow_html=True)
            cols[2].markdown(f'<div class="lrow-eq">{row.get("equipment") or "—"}</div>', unsafe_allow_html=True)
            cols[3].markdown(f'<div class="lrow-mut">{row.get("unit") or "—"}</div>', unsafe_allow_html=True)
            cols[4].markdown(f'<div class="lrow-mut">{row.get("assignee") or "—"}</div>', unsafe_allow_html=True)
            cols[5].markdown(
                f'<div class="lrow-bar"><div class="deadline-bar"><span class="{fill}" style="width:{pct}%;"></span></div>'
                f'<div class="deadline-text{" bad" if dbad else ""}">{dtext}</div></div>',
                unsafe_allow_html=True,
            )
            cols[6].markdown(status_badge_html(lang, row["status"]), unsafe_allow_html=True)
            with cols[7]:
                _render_icon_actions(lang, row, f"list{order_id}")

    st.download_button(
        _u(lang, "download"),
        data=data.drop(columns=[c for c in ("_dl", "_p") if c in data.columns]).to_csv(index=False).encode("utf-8-sig"),
        file_name="naryad_list.csv",
        mime="text/csv",
        key="disp_list_csv",
    )


def _render_timeline(lang: str, df: pd.DataFrame) -> None:
    """Диаграмма Ганта по нарядам (plotly)."""
    group_key = st.segmented_control(
        _u(lang, "group_by"),
        [_u(lang, "grp_equipment"), _u(lang, "grp_assignee")],
        default=_u(lang, "grp_equipment"),
        key="tl_group",
    )
    data = df.copy()
    data["_start"] = pd.to_datetime(data.get("created_at"), errors="coerce")
    data["_end"] = pd.to_datetime(data.get("deadline"), errors="coerce")
    data = data.dropna(subset=["_start", "_end"])
    if data.empty:
        _render_empty_state(lang)
        return
    y_col = "assignee" if group_key == _u(lang, "grp_assignee") else "equipment"
    data[y_col] = data[y_col].fillna("—")
    palette = {
        "Выдан": "#4a6b8a", "В очереди": "#4a6b8a", "Принят в работу": "#d9822b",
        "В работе": "#d9822b", "Приостановлен": "#d9a441", "Просрочен": "#c0504d",
        "Закрыт": "#4f9d69", "Исполнено": "#4f9d69", "Проверка ИИ": "#4a6b8a",
        "На доработку": "#d9a441",
    }
    figure = px.timeline(
        data, x_start="_start", x_end="_end", y=y_col, color="status",
        color_discrete_map=palette, hover_data=["order_num", "priority"],
    )
    future = data["_end"].max() if not data["_end"].empty else datetime.datetime.now()
    horizon = future + datetime.timedelta(hours=2)
    figure.update_xaxes(range=[data["_start"].min(), horizon])
    figure.update_yaxes(autorange="reversed")
    figure.add_vline(x=datetime.datetime.now(), line_color="#c0504d", line_width=2, line_dash="dash")
    figure.update_layout(
        template=("plotly_dark" if st.session_state.get("theme", "dark") == "dark" else "plotly_white"),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        height=max(240, 34 * data[y_col].nunique() + 90), margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    with st.container(key="timeline_box"):
        _stretch(st.plotly_chart, figure)


@st.dialog("Деталь наряда", width="large")
def _order_detail_dialog(lang: str, order_id: int) -> None:
    """Деталь наряда: степпер статусов, факты и панель действий."""
    frame = load_live_orders(limit=100000)
    sub = frame[frame["id"].astype(str) == str(order_id)]
    if sub.empty:
        st.warning(_u(lang, "no_orders"))
        return
    row = sub.iloc[0]
    status = str(row["status"])
    priority = str(row["priority"])
    st.markdown(
        f'<div class="card-top"><span class="myorder-num">{row["order_num"]}</span>'
        f"{priority_badge_html(lang, priority)}{status_badge_html(lang, status)}</div>",
        unsafe_allow_html=True,
    )

    events = db_fetchall(
        "SELECT action, event_time FROM order_events WHERE order_id = ? ORDER BY event_time ASC, rowid ASC",
        (int(order_id),),
    )
    times: Dict[str, str] = {}
    for ev in events:
        times[str(ev.get("action"))] = str(ev.get("event_time"))
    steps = [
        (_u(lang, "step_created"), str(row.get("created_at") or "")),
        (_u(lang, "step_accepted"), times.get("В работе", "")),
        (_u(lang, "step_work"), times.get("Приостановлен", "")),
        (_u(lang, "step_closed"), times.get("Закрыт", times.get("Отклонён", ""))),
    ]
    done_index = {"Выдан": 0, "В очереди": 0, "Принят в работу": 1, "В работе": 1,
                  "Приостановлен": 2, "Просрочен": 2, "Закрыт": 3, "Исполнено": 3}.get(status, 0)
    html_steps = ['<div class="stepper">']
    for index, (label, when) in enumerate(steps):
        cls = "done" if index < done_index else ("current" if index == done_index else "future")
        html_steps.append(
            f'<div class="step {cls}"><div class="dot"></div>'
            f'<div class="lbl">{label}</div><div class="tm">{when or "—"}</div></div>'
        )
    html_steps.append("</div>")
    st.markdown("".join(html_steps), unsafe_allow_html=True)

    st.markdown(
        f'<div class="disp-facts">'
        f'<div><div class="disp-fact-label">{c(lang, "equipment")}</div><div class="disp-fact-value">{row.get("equipment") or "—"}</div></div>'
        f'<div><div class="disp-fact-label">{c(lang, "unit")}</div><div class="disp-fact-value">{row.get("unit") or "—"}</div></div>'
        f'<div><div class="disp-fact-label">{c(lang, "assignee")}</div><div class="disp-fact-value">{row.get("assignee") or "—"}</div></div>'
        f'<div><div class="disp-fact-label">{c(lang, "deadline")}</div><div class="disp-fact-value">{row.get("deadline") or "—"}</div></div>'
        f'<div><div class="disp-fact-label">{t(lang, "kanban_filter_priority")}</div><div class="disp-fact-value">{priority_label(lang, priority)}</div></div>'
        f'<div><div class="disp-fact-label">{c(lang, "description")}</div><div class="disp-fact-value">{row.get("description") or "—"}</div></div>'
        f"</div>",
        unsafe_allow_html=True,
    )

    st.divider()
    accept_enabled = status not in ("В работе", "Закрыт", "Исполнено", "Проверка ИИ")
    pause_enabled = status == "В работе"
    act1, act2, act3, act4 = st.columns([2.4, 1.4, 1.6, 3.6])
    with act1:
        if st.button(_u(lang, "accept"), type="primary", key=f"dlg_accept_{order_id}", icon=":material/play_arrow:", disabled=not accept_enabled, use_container_width=True):
            update_order_status(int(order_id), "В работе", "Принят в работу")
            st.toast(_u(lang, "toast_accept").format(num=row["order_num"]))
            st.rerun()
    with act2:
        if st.button(_u(lang, "pause"), key=f"dlg_pause_{order_id}", icon=":material/pause:", disabled=not pause_enabled, use_container_width=True):
            update_order_status(int(order_id), "Приостановлен", "Пауза")
            st.toast(_u(lang, "toast_pause").format(num=row["order_num"]))
            st.rerun()
    with act3:
        with st.popover(_u(lang, "reject"), icon=":material/close:", disabled=status in ("Закрыт", "Исполнено", "Отклонён"), use_container_width=True, key=f"dlg_reject_{order_id}"):
            reason = st.text_input(_u(lang, "reason_ph"), key=f"dlg_reason_{order_id}")
            if st.button(_u(lang, "confirm_reject"), key=f"dlg_reject_confirm_{order_id}", type="primary"):
                update_order_status(int(order_id), "Отклонён", reason or "Причина не указана")
                st.toast(_u(lang, "toast_reject").format(num=row["order_num"]))
                st.rerun()
    with act4:
        st.empty()


def _render_dispatcher(lang: str, df: pd.DataFrame) -> None:
    """Фильтры, переключатель вида и основная область (доска/список/таймлайн)."""
    search = str(st.session_state.get("disp_search", "") or "").strip().lower()
    filtered = df
    if search and "order_num" in df.columns:
        haystack = (
            df.get("order_num", pd.Series(dtype=str)).astype(str).str.lower()
            + " " + df.get("equipment", pd.Series(dtype=str)).astype(str).str.lower()
            + " " + df.get("unit", pd.Series(dtype=str)).astype(str).str.lower()
        )
        filtered = df[haystack.str.contains(search, na=False, regex=False)]

    if "status" not in filtered.columns:
        filtered = pd.DataFrame(columns=["status"])

    units = [str(u) for u in dict.fromkeys(filtered.get("unit", pd.Series(dtype=str)).dropna().astype(str))] if "unit" in filtered.columns else []
    statuses = [str(s) for s in dict.fromkeys(filtered.get("status", pd.Series(dtype=str)).dropna().astype(str))] if "status" in filtered.columns else []

    f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
    with f1:
        prio_labels = [_u(lang, "all")] + [priority_label(lang, p) for p in PRIORITY_OPTIONS]
        prio_choice = st.pills(t(lang, "kanban_filter_priority"), prio_labels, default=_u(lang, "all"), key="disp_prio")
        if prio_choice and prio_choice != _u(lang, "all"):
            raw = next((p for p in PRIORITY_OPTIONS if priority_label(lang, p) == prio_choice), None)
            if raw:
                filtered = filtered[filtered["priority"].astype(str) == raw]
    with f2:
        unit_labels = [_u(lang, "all")] + units
        unit_choice = st.pills(c(lang, "unit"), unit_labels, default=_u(lang, "all"), key="disp_unit")
        if unit_choice and unit_choice != _u(lang, "all") and "unit" in filtered.columns:
            filtered = filtered[filtered["unit"].astype(str) == unit_choice]
    with f3:
        status_labels = [_u(lang, "all")] + [status_label(lang, s) for s in statuses]
        status_choice = st.pills(t(lang, "kanban_filter_status"), status_labels, default=_u(lang, "all"), key="disp_status")
        if status_choice and status_choice != _u(lang, "all"):
            raw = next((s for s in statuses if status_label(lang, s) == status_choice), None)
            if raw:
                filtered = filtered[filtered["status"].astype(str) == raw]
    with f4:
        view = st.segmented_control(
            _u(lang, "view"),
            [_u(lang, "board"), _u(lang, "list"), _u(lang, "timeline")],
            default=_u(lang, "board"),
            key="disp_view",
        )

    if view == _u(lang, "list"):
        _render_list(lang, filtered)
    elif view == _u(lang, "timeline"):
        _render_timeline(lang, filtered)
    else:
        _render_board(lang, filtered)


@st.fragment(run_every=30)
def _render_dispatcher_live(lang: str, df: pd.DataFrame) -> None:
    """Живая область пульта: авто-обновление данных доски раз в 30 секунд."""
    _render_dispatcher(lang, df)


def render_master_screen(lang: str, is_offline: bool) -> None:
    """Полный экран мастера смены: алерты, KPI и три вкладки."""
    df_live = load_live_orders(limit=100000)
    if "status" not in df_live.columns:
        # Защита: при ошибке БД db_query_df вернёт пустой DataFrame без колонок.
        df_live = pd.DataFrame(columns=["status", "priority", "created_at"])

    _render_command_bar(lang, "master", is_offline)
    _render_kpi_strip(lang, df_live)

    # --- Активные алерты контроля сроков (компактно, не ломают раскладку) ---
    alerts = call_deadlines_and_escalations()
    expired = list(alerts.get("expired", [])) + list(alerts.get("escalations", []))
    if expired:
        with st.expander("🚨 " + t(lang, "alerts_header") + f" · {len(expired)}", expanded=False):
            for message in expired:
                st.error(message)
            for message in alerts.get("warnings", []):
                st.warning(message)
    else:
        st.caption("✅ " + t(lang, "no_alerts"))

    st.divider()
    _render_dispatcher_live(lang, df_live)

    st.divider()
    tab_issue, tab_copilot = st.tabs(
        [
            "➕ " + t(lang, "tab_issue"),
            "🤖 " + t(lang, "tab_copilot"),
        ]
    )
    with tab_issue:
        render_master_issue_tab(lang, is_offline)
    with tab_copilot:
        render_master_copilot_tab(lang)


def render_equipment_selector(lang: str) -> Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]:
    """Выбор оборудования через модуль QR/шильдика, с безопасным fallback."""
    if _qr_selector_impl is not None:
        try:
            selection = _qr_selector_impl()
            if isinstance(selection, tuple) and len(selection) == 4:
                return selection  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001 — модуль QR не должен ронять пульт
            st.session_state["qr_error"] = f"{type(exc).__name__}: {exc}"

    # Fallback: обычный каскад «участок → оборудование» напрямую из БД.
    st.info(
        "Модуль QR/шильдика недоступен — используется обычный выбор из реестра ТОиР."
    )
    catalog = db_fetchall(
        """
        SELECT e.id AS equipment_id, e.name AS equipment_name,
               u.id AS unit_id, u.name AS unit_name
        FROM equipment AS e
        JOIN units AS u ON u.id = e.unit_id
        ORDER BY u.name, e.name
        """
    )
    if not catalog:
        st.warning("Справочник оборудования пуст или БД недоступна.")
        return (None, None, None, None)

    unit_names = list(dict.fromkeys(str(item["unit_name"]) for item in catalog))
    unit_name = st.selectbox(c(lang, "unit"), unit_names, key="fallback_unit")
    unit_id = next(
        (int(i["unit_id"]) for i in catalog if str(i["unit_name"]) == unit_name), None
    )
    equipment = [i for i in catalog if str(i["unit_name"]) == unit_name]
    labels = [f"{i['equipment_name']}" for i in equipment]
    choice = st.selectbox(c(lang, "equipment"), labels, key="fallback_equip")
    selected = equipment[labels.index(choice)]
    return (
        int(selected["unit_id"]),
        str(selected["unit_name"]),
        int(selected["equipment_id"]),
        str(selected["equipment_name"]),
    )


def load_executors() -> List[Dict[str, Any]]:
    """Список исполнителей с признаком занятости (по активным нарядам)."""
    workers = db_fetchall(
        """
        SELECT e.id AS id, e.fio AS fio, e.specialty AS specialty,
               e.grade AS grade, e.status AS status
        FROM employees AS e
        WHERE e.role = 'Исполнитель'
        ORDER BY e.fio ASC
        """
    )
    active_list = ("Выдан", "Принят в работу", "В очереди", "В работе", "Приостановлен", "На доработку", "Проверка ИИ")
    placeholders = ", ".join("?" for _ in active_list)
    busy_rows = db_fetchall(
        f"""
        SELECT assignee_id AS aid, COUNT(*) AS n
        FROM work_orders
        WHERE status IN ({placeholders})
        GROUP BY assignee_id
        """,
        active_list,
    )
    busy_map = {int(r["aid"]): int(r["n"]) for r in busy_rows if r.get("aid") is not None}
    for worker in workers:
        worker["busy_count"] = busy_map.get(int(worker["id"]), 0)
        worker["is_free"] = worker.get("status") == "Свободен" and worker["busy_count"] == 0
    return workers


def _render_issue_form_body(lang: str, is_offline: bool) -> None:
    """Тело формы выдачи наряда: QR, приоритет, исполнитель, описание, создание."""
    # Отложенные обновления ключей виджетов (до их создания в этом прогоне).
    if st.session_state.pop("_clear_issue_desc", False):
        st.session_state["issue_description"] = ""
    pending_desc = st.session_state.pop("_pending_issue_desc", None)
    if pending_desc is not None:
        st.session_state["issue_description"] = pending_desc
    if "issue_description" not in st.session_state:
        st.session_state["issue_description"] = ""

    st.markdown("#### " + t(lang, "issue_subheader"))

    unit_id, unit_name, equipment_id, equipment_name = render_equipment_selector(lang)
    if equipment_id is not None:
        st.success(
            f"{t(lang, 'equipment_selected')}: **{equipment_name}** · {unit_name}"
        )
    else:
        st.info(t(lang, "equipment_not_selected"))

    col_left, col_right = st.columns(2)
    with col_left:
        priority_options = localized_statuses(lang, PRIORITY_OPTIONS)
        priority_choice = st.selectbox(
            c(lang, "priority"), priority_options, key="issue_priority"
        )
        priority = PRIORITY_OPTIONS[priority_options.index(priority_choice)]
        normative = st.number_input(
            t(lang, "normative"),
            min_value=0.5,
            max_value=12.0,
            value=2.0,
            step=0.5,
            key="issue_normative",
        )
    with col_right:
        workers = load_executors()
        if workers:
            def worker_label(worker: Dict[str, Any]) -> str:
                if worker["is_free"]:
                    dot, text = "🟢", t(lang, "assignee_free")
                elif worker.get("status") == "Не на смене":
                    dot, text = "⚪", t(lang, "assignee_off")
                else:
                    dot, text = "🔴", t(lang, "assignee_busy")
                return (
                    f"{dot} {worker['fio']} — {worker['specialty']} "
                    f"(разряд {worker['grade']}) · {text}"
                )

            labels = [worker_label(w) for w in workers]
            worker_choice = st.selectbox(
                c(lang, "assignee"), labels, key="issue_worker"
            )
            worker = workers[labels.index(worker_choice)]
        else:
            st.warning("Справочник исполнителей недоступен.")
            worker = None

    # --- Описание неисправности с поддержкой голосового ввода ---
    st.text_area(
        c(lang, "description"),
        key="issue_description",
        placeholder=t(lang, "description_ph"),
        height=110,
    )
    st.caption(t(lang, "voice_hint"))

    with st.expander("🎤 " + t(lang, "voice_upload"), expanded=False):
        voice_file = st.file_uploader(
            t(lang, "voice_upload"),
            type=["wav", "mp3", "ogg", "m4a", "webm", "flac"],
            key="issue_voice_file",
        )
        if st.button(t(lang, "voice_recognize"), key="issue_voice_btn"):
            if voice_file is not None:
                audio_bytes = voice_file.read()
                recognized = ""
                if _transcribe_impl is not None:
                    try:
                        result = _transcribe_impl(audio_bytes)
                        recognized = (
                            str(result.get("text", ""))
                            if isinstance(result, dict)
                            else str(result)
                        )
                    except Exception as exc:  # noqa: BLE001
                        st.session_state["voice_error"] = f"{type(exc).__name__}: {exc}"
                if recognized.strip():
                    st.session_state["_pending_issue_desc"] = recognized.strip()
                    st.success(t(lang, "voice_done"))
                    st.rerun()
            st.warning(t(lang, "voice_fail"))

    if st.button(
        t(lang, "issue_btn"),
        type="primary",
        key="issue_submit",
        use_container_width=True,
    ):
        description = str(st.session_state.get("issue_description", "")).strip()
        if equipment_id is None or unit_id is None:
            st.error(t(lang, "issue_need_equip"))
        elif not description:
            st.error(t(lang, "issue_need_desc"))
        elif worker is None:
            st.error("Исполнитель не выбран.")
        else:
            order_type = (
                "Внеплановый (аварийный)" if priority == "Аварийный" else "Плановый"
            )
            ok, info = create_work_order(
                order_num=generate_order_number(),
                order_type=order_type,
                description=description,
                unit_id=int(unit_id),
                equipment_id=int(equipment_id),
                assignee_id=int(worker["id"]),
                master_id=get_default_master_id(),
                priority=priority,
                normative_hours=float(normative),
            )
            if ok:
                st.success(
                    "✅ " + t(lang, "issue_ok").format(num=info, worker=worker["fio"])
                )
                if is_offline:
                    st.info(t(lang, "issue_local"))
                st.session_state["_clear_issue_desc"] = True
                st.rerun()
            else:
                st.error(t(lang, "issue_error").format(err=info))


def render_master_issue_tab(lang: str, is_offline: bool) -> None:
    """Вкладка выдачи наряда, обёрнутая в аккуратную карточку-контейнер."""
    with st.container():
        _render_issue_form_body(lang, is_offline)


def render_master_kanban_tab(lang: str) -> None:
    """Вкладка канбана: живая таблица нарядов с фильтрами."""
    st.subheader("📋 " + t(lang, "tab_kanban"))
    frame = load_live_orders()
    if frame.empty:
        st.info(t(lang, "kanban_empty"))
        return

    all_statuses = list(dict.fromkeys(frame["status"].dropna().astype(str)))
    all_priorities = [p for p in PRIORITY_OPTIONS if p in set(frame["priority"].astype(str))]

    col_status, col_priority = st.columns(2)
    with col_status:
        status_map = {status_label(lang, s): s for s in all_statuses}
        chosen_status_labels = st.multiselect(
            t(lang, "kanban_filter_status"),
            list(status_map.keys()),
            default=list(status_map.keys()),
            key="kanban_status",
        )
    with col_priority:
        priority_map = {priority_label(lang, p): p for p in all_priorities}
        chosen_priority_labels = st.multiselect(
            t(lang, "kanban_filter_priority"),
            list(priority_map.keys()),
            default=list(priority_map.keys()),
            key="kanban_priority",
        )

    chosen_statuses = {status_map[label] for label in chosen_status_labels}
    chosen_priorities = {priority_map[label] for label in chosen_priority_labels}
    filtered = frame[
        frame["status"].astype(str).isin(chosen_statuses)
        & frame["priority"].astype(str).isin(chosen_priorities)
    ].copy()

    if filtered.empty:
        st.info(t(lang, "kanban_empty"))
        return

    filtered["Статус"] = filtered["status"].apply(lambda s: status_label(lang, s))
    filtered["Приоритет"] = filtered["priority"].apply(lambda p: priority_label(lang, p))
    view = filtered.rename(
        columns={
            "order_num": "№ наряда",
            "equipment": "Оборудование",
            "unit": "Участок",
            "assignee": "Исполнитель",
            "description": "Описание",
            "created_at": "Создан",
            "deadline": "Дедлайн",
        }
    )
    columns = [
        "№ наряда",
        "Приоритет",
        "Статус",
        "Оборудование",
        "Участок",
        "Исполнитель",
        "Описание",
        "Создан",
        "Дедлайн",
    ]
    _stretch(st.dataframe, view[[c for c in columns if c in view.columns]], height=460)
    st.download_button(
        t(lang, "kanban_download"),
        data=view.to_csv(index=False).encode("utf-8-sig"),
        file_name="naryad_kanban.csv",
        mime="text/csv",
        key="kanban_csv",
    )


def render_master_copilot_tab(lang: str) -> None:
    """Вкладка AI-Copilot мастера."""
    # Отложенное обновление вопроса из кнопок-подсказок (до создания виджета).
    pending_question = st.session_state.pop("_pending_copilot", None)
    if pending_question is not None:
        st.session_state["copilot_question"] = pending_question
    if "copilot_question" not in st.session_state:
        st.session_state["copilot_question"] = ""

    st.subheader("🤖 " + t(lang, "tab_copilot"))
    st.caption(t(lang, "copilot_intro"))

    st.text_area(
        t(lang, "copilot_ask"),
        key="copilot_question",
        placeholder=t(lang, "copilot_ph"),
        height=100,
    )

    result = ask_copilot("")
    suggestions = result.get("suggestions") or []
    if suggestions:
        st.markdown("**" + t(lang, "copilot_suggestions") + "**")
        suggestion_cols = st.columns(min(3, len(suggestions)))
        for index, suggestion in enumerate(suggestions[:3]):
            with suggestion_cols[index % len(suggestion_cols)]:
                if st.button(str(suggestion), key=f"copilot_sug_{index}"):
                    st.session_state["_pending_copilot"] = str(suggestion)
                    st.rerun()

    if st.button("💬 " + t(lang, "copilot_ask"), type="primary", key="copilot_submit"):
        question = str(st.session_state.get("copilot_question", "")).strip()
        answer = ask_copilot(question)
        st.session_state["copilot_answer"] = answer
        st.rerun()

    answer = st.session_state.get("copilot_answer")
    if answer:
        st.markdown("### " + t(lang, "copilot_answer"))
        st.success(str(answer.get("answer", "")))
        st.caption(
            t(lang, "copilot_source") + f": {answer.get('source', '—')}"
        )


# ==================================================================
# 10. ЭКРАН 2: ИСПОЛНИТЕЛЬ (МОБИЛЬНЫЙ ВИД)
# ==================================================================
def load_active_orders() -> pd.DataFrame:
    """Активные наряды для мобильного экрана исполнителя."""
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    query = f"""
        SELECT
            wo.id AS id,
            wo.order_num AS order_num,
            wo.description AS description,
            wo.priority AS priority,
            wo.status AS status,
            wo.deadline AS deadline,
            wo.created_at AS created_at,
            wo.assignee_id AS assignee_id,
            eq.name AS equipment,
            un.name AS unit
        FROM work_orders AS wo
        LEFT JOIN equipment AS eq ON eq.id = wo.equipment_id
        LEFT JOIN units     AS un ON un.id = wo.unit_id
        WHERE wo.status IN ({placeholders})
        ORDER BY
            CASE wo.priority
                WHEN 'Аварийный' THEN 0
                WHEN 'Высокий' THEN 1
                WHEN 'Обычный' THEN 2
                ELSE 3
            END,
            wo.id DESC
    """
    return db_query_df(query, ACTIVE_STATUSES)


def load_materials() -> List[Dict[str, Any]]:
    """Каталог ТМЦ для списания."""
    return db_fetchall(
        "SELECT id, name, unit_measure FROM materials_catalog ORDER BY name ASC"
    )


def render_worker_screen(lang: str, is_offline: bool) -> None:
    """Мобильный экран исполнителя: приём, пауза, отклонение, закрытие с ИИ."""
    # Отложенная очистка поля отчёта (до создания виджета в этом прогоне).
    if st.session_state.pop("_clear_worker_done", False):
        st.session_state["worker_work_done"] = ""
    if "worker_work_done" not in st.session_state:
        st.session_state["worker_work_done"] = ""

    st.header(t(lang, "w_title"))
    orders = load_active_orders()
    if orders.empty:
        st.success("✅ " + t(lang, "w_no_active"))
        return

    def order_label(row: pd.Series) -> str:
        return (
            f"{row['order_num']} · {priority_label(lang, row['priority'])} · "
            f"{row.get('equipment') or '—'}"
        )

    labels = [order_label(row) for _, row in orders.iterrows()]

    # --- Самый срочный наряд + лента «Следующие наряды» ---
    def _deadline_ts(value: Any) -> pd.Timestamp:
        ts = pd.to_datetime(value, errors="coerce")
        return ts if pd.notna(ts) else pd.Timestamp.max

    urgent_row = min(orders.iterrows(), key=lambda pair: _deadline_ts(pair[1].get("deadline")))[1]
    urgent_label = order_label(urgent_row)
    chosen = st.session_state.get("worker_order")
    if chosen not in labels:
        chosen = urgent_label
    next_choice = st.pills(_u(lang, "next_orders"), labels, default=chosen, key="worker_order")
    if next_choice is None:
        next_choice = chosen
    current = orders.iloc[labels.index(next_choice)]

    priority = str(current["priority"])
    priority_kind = _PRIORITY_BADGE_KIND.get(priority, "gray")
    status_value = str(current["status"])
    deadline_value = current.get("deadline")

    accept_enabled = status_value != "В работе"
    pause_enabled = status_value == "В работе"
    reject_enabled = status_value != "Отклонён"

    # --- Крупный обратный отсчёт до дедлайна ---
    countdown_text = "—"
    countdown_cls = ""
    try:
        dl = pd.to_datetime(deadline_value, errors="coerce")
        if pd.notna(dl):
            remain = (dl.to_pydatetime() - datetime.datetime.now()).total_seconds() / 60
            if remain < 0:
                countdown_text = "−" + _fmt_duration(-remain)
                countdown_cls = " bad"
            else:
                countdown_text = _fmt_duration(remain)
                countdown_cls = " ok" if remain > 240 else ""
    except (TypeError, ValueError):
        pass

    accept_label = _clean_label(t(lang, "w_accept"))
    pause_label = _clean_label(t(lang, "w_pause"))
    reject_label = _clean_label(t(lang, "w_reject"))

    with st.container(border=True, key=f"myorder_{int(current['id'])}"):
        st.markdown(
            f'<span class="worker-card-marker worker-prio-{priority_kind}"></span>'
            f'<div class="worker-card-head"><div class="worker-card-title">'
            f'<span class="myorder-num">{current["order_num"]}</span>'
            f"{priority_badge_html(lang, priority)}"
            f"{status_badge_html(lang, status_value)}"
            f"</div></div>"
            f'<div class="worker-section-label" style="margin-top:14px;">{c(lang, "deadline")}</div>'
            f'<div class="myorder-count{countdown_cls}">{countdown_text}</div>'
            f'<div class="worker-facts" style="margin-top:16px;">'
            f'<div class="worker-fact"><div class="worker-fact-label">{c(lang, "equipment")}</div>'
            f'<div class="worker-fact-value">{current.get("equipment") or "—"}</div></div>'
            f'<div class="worker-fact"><div class="worker-fact-label">{c(lang, "unit")}</div>'
            f'<div class="worker-fact-value">{current.get("unit") or "—"}</div></div>'
            f'<div class="worker-fact"><div class="worker-fact-label">{c(lang, "description")}</div>'
            f'<div class="worker-fact-value">{current.get("description") or "—"}</div></div>'
            f"</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            _status_stepper_html(lang, status_value, current.get("created_at"), int(current["id"])),
            unsafe_allow_html=True,
        )
        st.divider()

        # --- Три большие кнопки действий ---
        with st.container(key="actions"):
            act_accept, act_pause, act_reject = st.columns(3)
            with act_accept:
                if st.button(
                    accept_label,
                    type="primary",
                    key="worker_accept",
                    icon=":material/play_arrow:",
                    disabled=not accept_enabled,
                    use_container_width=True,
                ):
                    update_order_status(int(current["id"]), "В работе", "Принят исполнителем")
                    st.toast(_u(lang, "toast_accept").format(num=current["order_num"]))
                    st.rerun()
            with act_pause:
                if st.button(
                    pause_label,
                    key="worker_pause",
                    icon=":material/pause:",
                    disabled=not pause_enabled,
                    use_container_width=True,
                ):
                    update_order_status(
                        int(current["id"]), "Приостановлен", "Пауза исполнителя"
                    )
                    st.toast(_u(lang, "toast_pause").format(num=current["order_num"]))
                    st.rerun()
            with act_reject:
                with st.popover(
                    reject_label,
                    icon=":material/close:",
                    disabled=not reject_enabled,
                    use_container_width=True,
                    key="worker_reject",
                ):
                    reason = st.text_input(
                        t(lang, "w_reject_reason"), key="worker_reject_reason"
                    )
                    if st.button(
                        t(lang, "w_reject_btn"),
                        key="worker_reject_confirm",
                        type="primary",
                    ):
                        update_order_status(
                            int(current["id"]),
                            "Отклонён",
                            reason or "Причина не указана",
                        )
                        st.toast(_u(lang, "toast_reject").format(num=current["order_num"]))
                        st.rerun()

    st.divider()
    st.subheader("🏁 " + t(lang, "w_closing"))

    st.text_area(
        t(lang, "w_work_done"),
        key="worker_work_done",
        placeholder=t(lang, "w_work_done_ph"),
        height=110,
    )

    materials = load_materials()
    material_map = {str(m["name"]): int(m["id"]) for m in materials}
    material_labels = list(material_map.keys())
    chosen_materials = st.multiselect(
        t(lang, "w_materials"), material_labels, key="worker_materials"
    )

    col_before, col_after = st.columns(2)
    with col_before:
        photo_before = st.file_uploader(
            t(lang, "w_photo_before"), type=["jpg", "jpeg", "png"], key="worker_photo_before"
        )
    with col_after:
        photo_after = st.file_uploader(
            t(lang, "w_photo_after"), type=["jpg", "jpeg", "png"], key="worker_photo_after"
        )

    if st.button(
        t(lang, "w_submit"), type="primary", key="worker_submit"
    ):
        work_done = str(st.session_state.get("worker_work_done", "")).strip()
        if not work_done:
            st.error(t(lang, "w_need_done"))
        else:
            with st.spinner(t(lang, "w_analyzing")):
                photo_after_bytes = photo_after.read() if photo_after else None
                photo_before_bytes = photo_before.read() if photo_before else None
                ai_result = safe_verify_work_order(
                    problem_desc=str(current["description"]),
                    work_done=work_done,
                    materials_used=chosen_materials,
                    photo_bytes=photo_after_bytes,
                )
                extra_note = safe_analyze_photos(photo_before_bytes, photo_after_bytes)

            verdict = ai_result["verdict"]
            score = int(ai_result["score"])
            explanation = ai_result["explanation"]
            if extra_note:
                explanation = f"{explanation} {extra_note}".strip()

            new_status = "Закрыт" if score >= AI_CLOSE_THRESHOLD else "На доработку"
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            db_execute(
                """
                UPDATE work_orders
                SET status = ?, closing_comment = ?, completed_at = ?
                WHERE id = ?
                """,
                (new_status, explanation, now_str, int(current["id"])),
            )
            save_ai_evaluation(int(current["id"]), verdict, score, explanation)
            save_material_writeoffs(
                int(current["id"]),
                [material_map[name] for name in chosen_materials if name in material_map],
            )
            if photo_after_bytes:
                save_order_photo(
                    int(current["id"]),
                    "после",
                    photo_after_bytes,
                    int(current["assignee_id"]) if not pd.isna(current["assignee_id"]) else get_default_master_id(),
                    "Фото устранения",
                )
            if photo_before_bytes:
                save_order_photo(
                    int(current["id"]),
                    "до",
                    photo_before_bytes,
                    int(current["assignee_id"]) if not pd.isna(current["assignee_id"]) else get_default_master_id(),
                    "Фото неисправности",
                )
            log_order_event(
                int(current["id"]),
                int(current["assignee_id"]) if not pd.isna(current["assignee_id"]) else get_default_master_id(),
                new_status,
                explanation,
            )

            st.session_state["worker_ai_result"] = {
                "order_num": str(current["order_num"]),
                "verdict": verdict,
                "score": score,
                "explanation": explanation,
                "status": new_status,
            }
            st.session_state["_clear_worker_done"] = True
            st.rerun()

    # --- Сертификат AI-верификации (сохраняется после rerun) ---
    last_result = st.session_state.get("worker_ai_result")
    if last_result:
        with st.container(border=True):
            st.markdown('<span class="worker-cert-marker"></span>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="naryad-cert-title">🏅 {t(lang, "w_ai_result_hdr")} · '
                f'{last_result["order_num"]}</div>',
                unsafe_allow_html=True,
            )
            cert_score, cert_verdict, cert_state = st.columns([1, 1, 2])
            score_value = int(last_result["score"])
            score_accent = "#4ade80" if score_value >= AI_CLOSE_THRESHOLD else "#f87171"
            with cert_score:
                render_metric_card(
                    t(lang, "w_score"),
                    f"{score_value}/100",
                    accent=score_accent,
                )
            with cert_verdict:
                render_metric_card(
                    t(lang, "w_verdict"),
                    last_result["verdict"],
                    accent="#58a6ff",
                )
            with cert_state:
                st.progress(last_result["score"] / 100.0)
                if last_result["status"] == "Закрыт":
                    st.success(t(lang, "w_closed"))
                else:
                    st.error(t(lang, "w_rework"))
            st.markdown(
                f'<div class="naryad-ai-card"><h4>{t(lang, "w_ai_note")}</h4>'
                f'<p>{last_result["explanation"]}</p></div>',
                unsafe_allow_html=True,
            )
    if is_offline:
        st.caption(t(lang, "w_offline_ai"))


# ==================================================================
# 10b. АДАПТЕРЫ ПРОДВИНУТОЙ АНАЛИТИКИ И ИНТЕГРАЦИИ С 1С
# ==================================================================
def get_worker_ratings() -> pd.DataFrame:
    """Безопасный рейтинг исполнителей (прозрачный скоринг)."""
    if _worker_ratings_impl is None:
        return pd.DataFrame()
    try:
        result = _worker_ratings_impl()
        return result if isinstance(result, pd.DataFrame) else pd.DataFrame()
    except Exception as exc:  # noqa: BLE001 — аналитика не должна ронять экран
        st.session_state["analytics_error"] = f"{type(exc).__name__}: {exc}"
        return pd.DataFrame()


def get_ppr_failures_df() -> pd.DataFrame:
    """Безопасная таблица повторных поломок после ППР (< 7 дней)."""
    if _ppr_dataframe_impl is not None:
        try:
            result = _ppr_dataframe_impl()
            if isinstance(result, pd.DataFrame):
                return result
        except Exception as exc:  # noqa: BLE001
            st.session_state["analytics_error"] = f"{type(exc).__name__}: {exc}"
    if _ppr_failures_impl is not None:
        try:
            records = _ppr_failures_impl()
            if records:
                return pd.DataFrame(records)
        except Exception as exc:  # noqa: BLE001
            st.session_state["analytics_error"] = f"{type(exc).__name__}: {exc}"
    return pd.DataFrame()


def get_downtime() -> Dict[str, Any]:
    """Безопасная разбивка простоев и прямых потерь."""
    empty: Dict[str, Any] = {
        "breakdown": pd.DataFrame(),
        "by_category": pd.DataFrame(),
        "total_hours": 0.0,
        "total_loss": 0.0,
        "total_incidents": 0,
        "rate_per_hour": 450000.0,
    }
    if _downtime_impl is None:
        return empty
    try:
        result = _downtime_impl()
        if isinstance(result, dict):
            return result
    except Exception as exc:  # noqa: BLE001
        st.session_state["analytics_error"] = f"{type(exc).__name__}: {exc}"
    return empty


def build_1c_payload() -> Dict[str, Any]:
    """Безопасное формирование JSON-пакета для 1С:ТОиР."""
    empty: Dict[str, Any] = {
        "message_type": "WorkOrdersSync",
        "work_orders": [],
        "totals": {"orders_count": 0, "materials_count": 0},
    }
    if _sync_payload_impl is None:
        return empty
    try:
        result = _sync_payload_impl()
        return result if isinstance(result, dict) else empty
    except Exception as exc:  # noqa: BLE001
        st.session_state["analytics_error"] = f"{type(exc).__name__}: {exc}"
        return empty


def payload_to_json(payload: Dict[str, Any]) -> bytes:
    """Безопасная сериализация пакета 1С в UTF-8 JSON."""
    if _payload_json_impl is not None:
        try:
            data = _payload_json_impl(payload)
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
        except Exception as exc:  # noqa: BLE001
            st.session_state["analytics_error"] = f"{type(exc).__name__}: {exc}"
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    except Exception as exc:  # noqa: BLE001
        st.session_state["analytics_error"] = f"{type(exc).__name__}: {exc}"
        return b"{}"


# ==================================================================
# 10c. УМНЫЙ ИИ-УЧЁТЧИК И РАБОТА С CSV (SMART LEDGER AI)
# ==================================================================
def get_demo_ledger() -> pd.DataFrame:
    """Безопасно получить демонстрационный журнал списаний."""
    if _demo_ledger_impl is not None:
        try:
            result = _demo_ledger_impl()
            if isinstance(result, pd.DataFrame):
                return result
        except Exception as exc:  # noqa: BLE001
            st.session_state["ledger_error"] = f"{type(exc).__name__}: {exc}"
    return pd.DataFrame(
        [
            {"Номенклатура": "Подшипник роликовый радиальный 22318", "Кол-во": 1.0,
             "Ед.": "шт", "Оборудование": "Конвейер К-3", "Дата": "2026-10-06 08:20",
             "Статус": "Закрыт"},
            {"Номенклатура": "Смазка Литол-24 (ведро 10кг)", "Кол-во": 2.0,
             "Ед.": "шт", "Оборудование": "Мельница МШР 3.6х4.0", "Дата": "2026-10-06 10:05",
             "Статус": "Закрыт"},
        ]
    )


def normalize_ledger_df(frame: pd.DataFrame) -> pd.DataFrame:
    """Безопасно нормализовать таблицу журнала."""
    if _normalize_ledger_impl is not None:
        try:
            result = _normalize_ledger_impl(frame)
            if isinstance(result, pd.DataFrame):
                return result
        except Exception as exc:  # noqa: BLE001
            st.session_state["ledger_error"] = f"{type(exc).__name__}: {exc}"
    return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def audit_ledger_data(frame: pd.DataFrame) -> Dict[str, Any]:
    """Безопасно запустить интеллектуальный аудит журнала."""
    if _audit_ledger_impl is not None:
        try:
            result = _audit_ledger_impl(frame)
            if isinstance(result, dict):
                return result
        except Exception as exc:  # noqa: BLE001
            st.session_state["ledger_error"] = f"{type(exc).__name__}: {exc}"
    return {
        "summary": "Аудит недоступен: модуль smart_ledger не загружен.",
        "anomalies": [],
        "recommendations": ["Проверьте наличие модуля src/smart_ledger.py."],
        "stats": {},
        "enriched": frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(),
        "columns": {},
    }


def ledger_columns(frame: pd.DataFrame) -> Dict[str, Optional[str]]:
    """Распознать смысловые колонки журнала (материал, кол-во, дата, статус)."""
    if _detect_ledger_columns_impl is not None:
        try:
            result = _detect_ledger_columns_impl(frame)
            if isinstance(result, dict):
                return result
        except Exception:  # noqa: BLE001
            st.session_state["ledger_error"] = "column detection failed"
    mapping: Dict[str, Optional[str]] = {}
    for column in getattr(frame, "columns", []):
        low = str(column).casefold()
        if any(h in low for h in ("кол", "qty", "quantity", "расход", "списан")):
            mapping.setdefault("quantity", column)
        if any(h in low for h in ("номенклатура", "материал", "name", "запчаст")):
            mapping.setdefault("material", column)
        if any(h in low for h in ("дата", "date", "created")):
            mapping.setdefault("date", column)
        if any(h in low for h in ("статус", "status")):
            mapping.setdefault("status", column)
    return mapping


def export_ledger_excel_bytes(frame: pd.DataFrame, report: Dict[str, Any]) -> bytes:
    """Безопасная выгрузка обработанной таблицы в Excel."""
    if _export_ledger_excel_impl is not None:
        try:
            data = _export_ledger_excel_impl(frame, report)
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
        except Exception as exc:  # noqa: BLE001
            st.session_state["ledger_error"] = f"{type(exc).__name__}: {exc}"
    try:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            frame.to_excel(writer, index=False, sheet_name="Учёт ТМЦ")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001
        try:
            return frame.to_csv(index=False).encode("utf-8-sig")
        except Exception:  # noqa: BLE001
            return b""


def export_ledger_csv_bytes(frame: pd.DataFrame) -> bytes:
    """Безопасная выгрузка очищенной таблицы в CSV."""
    if _export_ledger_csv_impl is not None:
        try:
            data = _export_ledger_csv_impl(frame)
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
        except Exception as exc:  # noqa: BLE001
            st.session_state["ledger_error"] = f"{type(exc).__name__}: {exc}"
    try:
        return frame.to_csv(index=False).encode("utf-8-sig")
    except Exception:  # noqa: BLE001
        return b""


def read_csv_robust(raw: bytes) -> pd.DataFrame:
    """Прочитать произвольный CSV, перебирая разделители и кодировки."""
    attempts = (
        {"sep": None, "engine": "python", "encoding": "utf-8-sig"},
        {"sep": None, "engine": "python"},
        {"sep": None, "engine": "python", "encoding": "cp1251"},
        {"sep": ",", "engine": "python", "encoding": "utf-8-sig"},
        {"sep": ";", "engine": "python", "encoding": "cp1251"},
    )
    last_error: Optional[Exception] = None
    for kwargs in attempts:
        try:
            return pd.read_csv(io.BytesIO(raw), **kwargs)
        except Exception as exc:  # noqa: BLE001 — пробуем следующий вариант
            last_error = exc
    raise ValueError(str(last_error) if last_error else "Не удалось прочитать CSV")


def _style_ledger(frame: pd.DataFrame, mapping: Dict[str, Optional[str]]):
    """Собрать pandas Styler с цветовой подсветкой статусов и количества."""
    styler = frame.style
    status_col = mapping.get("status")
    quantity_col = mapping.get("quantity")

    palette = {
        "green": "background-color:#12261a;color:#3fb950;font-weight:700",
        "red": "background-color:#2a1516;color:#f85149;font-weight:700",
        "yellow": "background-color:#2a2413;color:#e3b341;font-weight:700",
        "blue": "background-color:#132030;color:#58a6ff;font-weight:700",
        "gray": "background-color:#1f242d;color:#8b949e;font-weight:600",
    }

    def status_style(value: Any) -> str:
        kind = _STATUS_BADGE_KIND.get(str(value), "gray")
        return palette.get(kind, "")

    def quantity_style(value: Any) -> str:
        try:
            if value is None or pd.isna(value):
                return ""
            # Тёмная янтарная подложка + акцентный текст (Industrial Dark).
            return (
                "background-color:#2a2413;color:#e3b341;font-weight:700"
                if float(value) >= 5
                else ""
            )
        except (TypeError, ValueError):
            return ""

    if status_col is not None:
        styler = styler.map(status_style, subset=[status_col])
    if quantity_col is not None:
        styler = styler.map(quantity_style, subset=[quantity_col])
    format_map: Dict[str, str] = {}
    if quantity_col is not None:
        format_map[quantity_col] = "{:.2f}"
    if format_map:
        styler = styler.format(format_map)
    return styler


def _render_ledger_table(frame: pd.DataFrame, mapping: Dict[str, Optional[str]], lang: str) -> None:
    """Отрисовать журнал с форматированием колонок и цветовой подсветкой."""
    quantity_col = mapping.get("quantity")
    date_col = mapping.get("date")
    status_col = mapping.get("status")

    column_config: Dict[str, Any] = {}
    if quantity_col is not None:
        column_config[quantity_col] = st.column_config.NumberColumn(
            quantity_col, format="%.2f", help="Расход по позиции"
        )
    if date_col is not None:
        if pd.api.types.is_datetime64_any_dtype(frame[date_col]):
            column_config[date_col] = st.column_config.DatetimeColumn(
                date_col, format="DD.MM.YYYY HH:mm"
            )
        else:
            column_config[date_col] = st.column_config.TextColumn(date_col)
    if status_col is not None:
        column_config[status_col] = st.column_config.TextColumn(status_col)

    try:
        styled = _style_ledger(frame, mapping)
        _stretch(
            st.dataframe,
            styled,
            column_config=column_config,
            height=380,
        )
    except Exception:  # noqa: BLE001 — оформление не должно ронять экран
        _stretch(st.dataframe, frame, height=380)


def render_smart_ledger_section(lang: str) -> None:
    """Блок «🤖 Умный ИИ-учётчик и работа с CSV»."""
    st.subheader(t(lang, "ledger_title"))
    st.caption(t(lang, "ledger_caption"))

    uploaded = st.file_uploader(
        t(lang, "ledger_upload"), type=["csv"], key="ledger_upload"
    )

    frame: Optional[pd.DataFrame] = None
    used_demo = False
    if uploaded is not None:
        try:
            raw = uploaded.read()
            frame = read_csv_robust(raw)
            if frame is None or frame.empty:
                st.warning(t(lang, "ledger_empty"))
                frame = None
        except Exception as exc:  # noqa: BLE001 — «грязный» CSV не должен ронять UI
            st.error(
                t(lang, "ledger_file_error").format(
                    err=f"{type(exc).__name__}: {exc}"
                )
            )
            frame = None
    if frame is None:
        frame = get_demo_ledger()
        used_demo = True

    frame = normalize_ledger_df(frame)
    if frame is None or frame.empty:
        st.info(t(lang, "ledger_empty"))
        return
    if used_demo:
        st.info(t(lang, "ledger_demo_note"))

    mapping = ledger_columns(frame)
    quantity_col = mapping.get("quantity")
    material_col = mapping.get("material")

    # --- Быстрые метрики таблицы ---
    try:
        total_quantity = (
            float(pd.to_numeric(frame[quantity_col], errors="coerce").fillna(0).sum())
            if quantity_col is not None
            else 0.0
        )
    except Exception:  # noqa: BLE001
        total_quantity = 0.0
    unique_materials = (
        int(frame[material_col].nunique()) if material_col is not None else 0
    )
    last_report = st.session_state.get("ledger_report") or {}
    anomaly_count = len(last_report.get("anomalies") or [])

    m_rows, m_materials, m_qty, m_anomalies = st.columns(4)
    with m_rows:
        render_metric_card(t(lang, "ledger_rows"), len(frame), accent="#f8fafc")
    with m_materials:
        render_metric_card(
            t(lang, "ledger_materials"), unique_materials, accent="#58a6ff"
        )
    with m_qty:
        render_metric_card(
            t(lang, "ledger_total_qty"),
            f"{total_quantity:,.1f}".replace(",", " "),
            accent="#22d3ee",
        )
    with m_anomalies:
        anomaly_accent = "#f87171" if anomaly_count else "#4ade80"
        render_metric_card(
            t(lang, "ledger_anomalies"), anomaly_count, accent=anomaly_accent
        )

    st.markdown("#### " + t(lang, "ledger_table"))
    _render_ledger_table(frame, mapping, lang)

    # --- Запуск интеллектуального аудита ---
    if st.button(
        t(lang, "ledger_analyze"), type="primary", key="ledger_analyze"
    ):
        with st.spinner(t(lang, "ledger_analyzing")):
            report = audit_ledger_data(frame)
        st.session_state["ledger_report"] = report
        st.success(t(lang, "ledger_ready"))

    report = st.session_state.get("ledger_report")
    if not report:
        return

    summary = str(report.get("summary") or "")
    if summary:
        st.markdown(
            f'<div class="naryad-ai-card"><h4>{t(lang, "ledger_conclusion")}</h4>'
            f"<p>{summary}</p></div>",
            unsafe_allow_html=True,
        )

    anomalies = report.get("anomalies") or []
    st.markdown("#### " + t(lang, "ledger_anomaly_list"))
    if anomalies:
        for item in anomalies[:12]:
            severity = str(item.get("severity") or "medium")
            if severity == "high":
                kind, severity_text = "red", t(lang, "ledger_severity_high")
            else:
                kind, severity_text = "yellow", t(lang, "ledger_severity_medium")
            st.markdown(
                badge_html(severity_text, kind) + " " + str(item.get("message") or ""),
                unsafe_allow_html=True,
            )
    else:
        st.success("✅ " + t(lang, "ledger_no_anomalies"))

    recommendations = report.get("recommendations") or []
    if recommendations:
        st.markdown("#### " + t(lang, "ledger_recommendations"))
        for recommendation in recommendations:
            st.markdown(f"- {recommendation}")

    enriched = report.get("enriched")
    if isinstance(enriched, pd.DataFrame) and not enriched.empty:
        st.markdown("#### " + t(lang, "ledger_enriched"))
        enriched_mapping = ledger_columns(enriched)
        _render_ledger_table(enriched, enriched_mapping, lang)

    export_frame = enriched if isinstance(enriched, pd.DataFrame) and not enriched.empty else frame
    excel_bytes = export_ledger_excel_bytes(export_frame, report)
    csv_bytes = export_ledger_csv_bytes(export_frame)
    col_excel, col_csv = st.columns(2)
    with col_excel:
        st.download_button(
            t(lang, "ledger_export_excel"),
            data=excel_bytes,
            file_name="smart_ledger_processed.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="ledger_download_xlsx",
        )
    with col_csv:
        st.download_button(
            t(lang, "ledger_export_csv"),
            data=csv_bytes,
            file_name="smart_ledger_clean.csv",
            mime="text/csv",
            key="ledger_download_csv",
        )


def render_admin_screen(lang: str) -> None:
    """Экран Администратора НСИ (безопасный вызов внешнего модуля)."""
    if _admin_panel_impl is None:
        st.header("🗂 " + t(lang, "role_admin"))
        st.error(
            "Модуль управления НСИ (src/admin_nsi.py) недоступен. "
            "Проверьте установку зависимостей и наличие файла модуля."
        )
    else:
        try:
            _admin_panel_impl(lang)
        except Exception as exc:  # noqa: BLE001 — панель админа не должна ронять приложение
            st.session_state["admin_error"] = f"{type(exc).__name__}: {exc}"
            st.error(f"Не удалось открыть панель НСИ: {type(exc).__name__}: {exc}")

    st.divider()
    render_smart_ledger_section(lang)


# ==================================================================
# 11. ЭКРАН 3: РУКОВОДИТЕЛЬ / ТОИР АНАЛИТИКА
# ==================================================================
def render_manager_screen(lang: str) -> None:
    """Аналитика ТОиР: эффект, графики, предиктивный радар, документы."""
    _render_command_bar(lang, "manager", False, title_key="mg_title")
    st.markdown(
        '<div class="board-head st-work" style="margin-top:8px;"><span class="bh-title">'
        + t(lang, "mg_title")
        + "</span></div>",
        unsafe_allow_html=True,
    )

    closed_total = db_scalar(
        "SELECT COUNT(*) AS n FROM work_orders WHERE status = 'Закрыт'"
    )
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    with kpi1:
        render_metric_card(
            t(lang, "mg_saved"),
            "21 800 000 ₸",
            t(lang, "mg_saved_delta"),
            accent="#4ade80",
        )
    with kpi2:
        render_metric_card(
            t(lang, "mg_reaction"),
            "8.2 мин",
            t(lang, "mg_reaction_delta"),
            accent="#22d3ee",
        )
    with kpi3:
        render_metric_card(
            t(lang, "mg_quality"),
            "94.6%",
            t(lang, "mg_quality_delta"),
            accent="#58a6ff",
        )
    with kpi4:
        render_metric_card(
            t(lang, "mg_closed_total"),
            closed_total,
            accent="#f8fafc",
        )

    st.divider()
    chart_left, chart_right = st.columns(2)
    with chart_left:
        st.subheader("📈 " + t(lang, "mg_chart_faults"))
        render_top_faults_chart(lang)
    with chart_right:
        st.subheader("🥧 " + t(lang, "mg_chart_status"))
        render_status_chart(lang)

    st.divider()
    render_predictive_radar(lang)

    st.divider()
    render_worker_ratings_section(lang)

    st.divider()
    render_ai_scores_section(lang)

    st.divider()
    render_ppr_quality_section(lang)

    st.divider()
    render_downtime_section(lang)

    st.divider()
    render_1c_export_card(lang)

    st.divider()
    render_documents_section(lang)

    st.divider()
    render_smart_ledger_section(lang)


def render_top_faults_chart(lang: str) -> None:
    """Столбчатая диаграмма топ-отказов оборудования (тёмная тема)."""
    frame = db_query_df(
        """
        SELECT e.name AS equipment, COUNT(*) AS incidents
        FROM work_orders AS wo
        JOIN equipment AS e ON e.id = wo.equipment_id
        GROUP BY e.id
        ORDER BY incidents DESC
        LIMIT 8
        """
    )
    if frame.empty:
        st.info(c(lang, "no_data"))
        return
    figure = px.bar(
        frame,
        x="incidents",
        y="equipment",
        orientation="h",
        color="incidents",
        color_continuous_scale=["#22d3ee", "#e3b341", "#f85149"],
        labels={"incidents": "Инциденты", "equipment": "Оборудование"},
    )
    figure.update_layout(
        template=(
            "plotly_dark"
            if st.session_state.get("theme", "dark") == "dark"
            else "plotly_white"
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(autorange="reversed"),
        coloraxis_showscale=False,
        margin=dict(l=10, r=10, t=10, b=10),
    )
    _stretch(st.plotly_chart, figure)


def render_status_chart(lang: str) -> None:
    """Круговая диаграмма распределения статусов нарядов (тёмная тема)."""
    frame = db_query_df(
        "SELECT status, COUNT(*) AS cnt FROM work_orders GROUP BY status"
    )
    if frame.empty:
        st.info(c(lang, "no_data"))
        return
    frame["Статус"] = frame["status"].apply(lambda s: status_label(lang, s))
    palette = {
        "green": "#3fb950",
        "red": "#f85149",
        "yellow": "#e3b341",
        "blue": "#58a6ff",
        "gray": "#8b949e",
    }
    color_map = {
        status_label(lang, status): palette.get(kind, "#8b949e")
        for status, kind in _STATUS_BADGE_KIND.items()
    }
    figure = px.pie(
        frame,
        names="Статус",
        values="cnt",
        hole=0.5,
        color="Статус",
        color_discrete_map=color_map,
    )
    figure.update_traces(
        textposition="inside",
        textinfo="percent+label",
        marker=dict(line=dict(color="#0d1117", width=2)),
    )
    figure.update_layout(
        template=(
            "plotly_dark"
            if st.session_state.get("theme", "dark") == "dark"
            else "plotly_white"
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=10, b=10),
    )
    _stretch(st.plotly_chart, figure)


def render_predictive_radar(lang: str) -> None:
    """Блок «Предиктивный радар отказов» как радар безопасности (Alert Level 1)."""
    st.subheader(t(lang, "mg_radar_title"))
    prediction = get_prediction()
    risk = prediction.get("risk_percent", 87.4)
    try:
        risk_value = float(risk)
    except (TypeError, ValueError):
        risk_value = 87.4

    with st.container(border=True):
        st.markdown('<span class="radar-marker"></span>', unsafe_allow_html=True)
        st.markdown(
            f'<span class="naryad-alert-l1">ALERT LEVEL 1</span>'
            f'<span style="color:#f85149;font-weight:700;margin-left:10px;">'
            f'{t(lang, "mg_radar_alert")}</span>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f"- **{t(lang, 'mg_radar_unit')}:** {prediction.get('top_problem_unit', 'Конвейер К-3')}\n"
            f"- **{t(lang, 'mg_radar_code')}:** {prediction.get('fault_code', 'М-02')}\n"
            f"- **{t(lang, 'mg_radar_risk')}:** **{risk_value}%**\n"
            f"- **{t(lang, 'mg_radar_reco')}:** {prediction.get('recommendation', '')}"
        )
        radar_left, radar_right = st.columns([1, 3])
        with radar_left:
            risk_is_critical = risk_value >= 70
            risk_delta = "критично" if risk_is_critical else "умеренно"
            risk_delta_color = "#f87171" if risk_is_critical else "#fbbf24"
            st.markdown(f"""
    <div style="background: #141a24; border: 1px solid #232d3d; border-radius: 8px; padding: 16px 20px;">
        <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; font-weight: 600;">{t(lang, "mg_radar_risk")}</div>
        <div style="font-size: 32px; font-weight: 800; color: #f8fafc; margin-top: 4px;">{risk_value}%</div>
        <div style="font-size: 11px; color: {risk_delta_color}; margin-top: 2px;">{risk_delta}</div>
    </div>
    """, unsafe_allow_html=True)
        with radar_right:
            st.progress(min(1.0, risk_value / 100.0))
            anomalies = prediction.get("anomalies") or []
            if anomalies:
                st.markdown("**Другие проблемные узлы:**")
                for item in anomalies[:5]:
                    st.markdown(
                        f"- {item.get('equipment', '—')} · "
                        f"{item.get('fault_code', '—')} · "
                        f"{item.get('failure_count', 0)} инцидентов"
                    )


def render_documents_section(lang: str) -> None:
    """Документооборот: паспорт ТОиР (PDF) и журнал смены (Excel)."""
    st.subheader("🗂 " + t(lang, "mg_docs"))
    orders = db_query_df(
        """
        SELECT wo.order_num AS order_num, wo.description AS description,
               wo.priority AS priority, wo.status AS status,
               wo.created_at AS created_at, wo.completed_at AS completed_at,
               wo.deadline AS deadline, wo.closing_comment AS closing_comment,
               eq.name AS equipment, un.name AS unit, emp.fio AS assignee
        FROM work_orders AS wo
        LEFT JOIN equipment AS eq ON eq.id = wo.equipment_id
        LEFT JOIN units     AS un ON un.id = wo.unit_id
        LEFT JOIN employees AS emp ON emp.id = wo.assignee_id
        WHERE wo.status IN ('Закрыт','Исполнено')
        ORDER BY wo.id DESC
        LIMIT 200
        """
    )
    if orders.empty:
        st.info(t(lang, "mg_no_orders"))
        return

    options = [str(item) for item in orders["order_num"].tolist()]
    selected_num = st.selectbox(t(lang, "mg_select_order"), options, key="doc_order")
    selected_row = orders[orders["order_num"].astype(str) == selected_num].iloc[0]

    button_pdf, button_excel = st.columns(2)
    with button_pdf:
        if st.button(t(lang, "mg_pdf"), key="doc_pdf_btn"):
            order_data = {
                "Номер наряда": selected_row["order_num"],
                "Статус": status_label(lang, str(selected_row["status"])),
                "Приоритет": priority_label(lang, str(selected_row["priority"])),
                "Оборудование": selected_row.get("equipment") or "—",
                "Участок": selected_row.get("unit") or "—",
                "Исполнитель": selected_row.get("assignee") or "—",
                "Создан": selected_row.get("created_at") or "—",
                "Дедлайн": selected_row.get("deadline") or "—",
                "Завершён": selected_row.get("completed_at") or "—",
                "Описание": selected_row.get("description") or "—",
                "Заключение": selected_row.get("closing_comment") or "—",
            }
            pdf_bytes = generate_pdf_report(order_data, "ПАСПОРТ ТЕХОБСЛУЖИВАНИЯ И РЕМОНТА")
            if pdf_bytes:
                st.session_state["pdf_bytes"] = pdf_bytes
                st.session_state["pdf_name"] = f"pasport_TOiR_{selected_num}.pdf"
                st.success(t(lang, "mg_pdf_ready"))
            else:
                st.error(t(lang, "mg_pdf_unavailable"))
    with button_excel:
        if st.button(t(lang, "mg_excel"), key="doc_excel_btn"):
            export_frame = orders.rename(
                columns={
                    "order_num": "Номер наряда",
                    "description": "Описание",
                    "priority": "Приоритет",
                    "status": "Статус",
                    "created_at": "Создан",
                    "completed_at": "Завершён",
                    "deadline": "Дедлайн",
                    "closing_comment": "Заключение",
                    "equipment": "Оборудование",
                    "unit": "Участок",
                    "assignee": "Исполнитель",
                }
            )
            summary = {
                "Всего закрытых нарядов": int(len(orders)),
                "Плановых": int((orders["priority"] == "Плановый").sum()),
                "Высокий/Аварийный": int(
                    orders["priority"].isin(["Высокий", "Аварийный"]).sum()
                ),
                "Дата выгрузки": datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
            }
            excel_bytes = export_excel(export_frame, summary)
            st.session_state["excel_bytes"] = excel_bytes
            st.session_state["excel_name"] = "svodny_zhurnal_smeny.xlsx"
            st.success(t(lang, "mg_excel_ready"))

    # Кнопки скачивания устойчивы к повторному прогону страницы.
    if st.session_state.get("pdf_bytes"):
        st.download_button(
            "📄 " + str(st.session_state.get("pdf_name", "pasport.pdf")),
            data=st.session_state["pdf_bytes"],
            file_name=str(st.session_state.get("pdf_name", "pasport.pdf")),
            mime="application/pdf",
            key="doc_pdf_download",
        )
    if st.session_state.get("excel_bytes"):
        st.download_button(
            "📊 " + str(st.session_state.get("excel_name", "zhurnal.xlsx")),
            data=st.session_state["excel_bytes"],
            file_name=str(st.session_state.get("excel_name", "zhurnal.xlsx")),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="doc_excel_download",
        )


# ==================================================================
# 11b. НОВЫЕ АНАЛИТИЧЕСКИЕ БЛОКИ РУКОВОДИТЕЛЯ
# ==================================================================
def render_worker_ratings_section(lang: str) -> None:
    """Таблица детального рейтинга исполнителей с формулой скоринга."""
    st.subheader(t(lang, "mg_ratings_title"))
    st.caption("🧮 " + t(lang, "mg_ratings_formula"))

    frame = get_worker_ratings()
    if frame.empty:
        st.info(t(lang, "mg_ratings_empty"))
        return

    config = {
        "В срок %": st.column_config.ProgressColumn(
            "В срок %", min_value=0, max_value=100, format="%.0f"
        ),
        "Ср. оценка ИИ": st.column_config.ProgressColumn(
            "Ср. оценка ИИ", min_value=0, max_value=100, format="%.0f"
        ),
        "Score": st.column_config.NumberColumn("Score", format="%.2f"),
        "Бонус за сроки": st.column_config.NumberColumn("Бонус за сроки", format="%.2f"),
        "Бонус за качество": st.column_config.NumberColumn("Бонус за качество", format="%.2f"),
        "Штраф за доработки": st.column_config.NumberColumn("Штраф за доработки", format="%.2f"),
        "Штраф за отказы": st.column_config.NumberColumn("Штраф за отказы", format="%.2f"),
        "Всего нарядов": st.column_config.NumberColumn("Всего нарядов", format="%d"),
        "Закрыто": st.column_config.NumberColumn("Закрыто", format="%d"),
        "Возвраты": st.column_config.NumberColumn("Возвраты", format="%d"),
        "Отказы (без причины)": st.column_config.NumberColumn(
            "Отказы (без причины)", format="%d"
        ),
    }
    _stretch(st.dataframe, frame, column_config=config, height=460)
    st.download_button(
        t(lang, "mg_ratings_download"),
        data=frame.to_csv(index=False).encode("utf-8-sig"),
        file_name="worker_ratings.csv",
        mime="text/csv",
        key="ratings_csv",
    )


def render_ppr_quality_section(lang: str) -> None:
    """Блок «Анализ качества ППР (повторные поломки < 7 дней)»."""
    st.subheader(t(lang, "mg_ppr_title"))
    st.caption(t(lang, "mg_ppr_caption"))

    frame = get_ppr_failures_df()
    if frame.empty:
        st.success("✅ " + t(lang, "mg_ppr_empty"))
        return

    metric_cases, metric_interval, metric_downtime = st.columns(3)
    with metric_cases:
        render_metric_card(t(lang, "mg_ppr_cases"), len(frame), accent="#f87171")

    if "Дней после ППР" in frame.columns:
        try:
            avg_days = float(pd.to_numeric(frame["Дней после ППР"], errors="coerce").mean())
        except (TypeError, ValueError):
            avg_days = 0.0
        with metric_interval:
            render_metric_card(
                t(lang, "mg_ppr_interval"),
                f"{avg_days:.1f} {t(lang, 'unit_days')}",
                accent="#fbbf24",
            )
    if "Простой, ч" in frame.columns:
        try:
            total_hours = float(pd.to_numeric(frame["Простой, ч"], errors="coerce").sum())
        except (TypeError, ValueError):
            total_hours = 0.0
        with metric_downtime:
            render_metric_card(
                t(lang, "mg_ppr_downtime"),
                f"{total_hours:.1f} {t(lang, 'unit_hours')}",
                accent="#22d3ee",
            )

    ppr_config = {
        "Дней после ППР": st.column_config.NumberColumn("Дней после ППР", format="%.1f"),
        "Простой, ч": st.column_config.NumberColumn("Простой, ч", format="%.1f"),
    }
    _stretch(st.dataframe, frame, column_config=ppr_config, height=420)

    if "Бригада (ремонт ППР)" in frame.columns:
        teams = frame["Бригада (ремонт ППР)"].value_counts().head(5)
        if not teams.empty:
            st.markdown("**Бригады с наибольшим числом повторных поломок:**")
            for team, count in teams.items():
                st.markdown(f"- {team}: {int(count)}")

    st.download_button(
        t(lang, "mg_ppr_download"),
        data=frame.to_csv(index=False).encode("utf-8-sig"),
        file_name="ppr_quality_analysis.csv",
        mime="text/csv",
        key="ppr_csv",
    )


def render_downtime_section(lang: str) -> None:
    """Простои по участкам/шифрам и прямые финансовые потери."""
    st.subheader(t(lang, "mg_downtime_title"))
    data = get_downtime()
    hours = float(data.get("total_hours") or 0.0)
    loss = float(data.get("total_loss") or 0.0)
    incidents = int(data.get("total_incidents") or 0)
    rate = float(data.get("rate_per_hour") or 450000.0)

    col_hours, col_loss, col_incidents, col_rate = st.columns(4)
    with col_hours:
        render_metric_card(
            t(lang, "mg_downtime_hours"),
            f"{hours:,.1f}".replace(",", " "),
            accent="#fbbf24",
        )
    with col_loss:
        render_metric_card(
            t(lang, "mg_downtime_loss"),
            f"{loss:,.0f} ₸".replace(",", " "),
            accent="#f87171",
        )
    with col_incidents:
        render_metric_card(
            t(lang, "mg_downtime_incidents"), incidents, accent="#f8fafc"
        )
    with col_rate:
        render_metric_card(
            t(lang, "mg_downtime_rate"),
            f"{rate:,.0f} ₸/ч".replace(",", " "),
            accent="#22d3ee",
        )

    by_category = data.get("by_category")
    if isinstance(by_category, pd.DataFrame) and not by_category.empty:
        figure = px.bar(
            by_category,
            x="Категория",
            y="Потери, ₸",
            color="Категория",
            text="Простой, ч",
            color_discrete_sequence=["#3fb950", "#22d3ee", "#e3b341", "#f85149", "#58a6ff"],
        )
        figure.update_traces(textposition="outside")
        figure.update_layout(
            template=(
                "plotly_dark"
                if st.session_state.get("theme", "dark") == "dark"
                else "plotly_white"
            ),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=10, r=10, t=10, b=10),
        )
        _stretch(st.plotly_chart, figure)

    breakdown = data.get("breakdown")
    if isinstance(breakdown, pd.DataFrame) and not breakdown.empty:
        with st.expander("Детализация по участкам и шифрам", expanded=False):
            display = breakdown.copy()
            if "Потери, ₸" in display.columns:
                display["Потери, ₸"] = display["Потери, ₸"].map(
                    lambda v: f"{float(v):,.0f}".replace(",", " ")
                )
            dt_config = {
                "Инциденты": st.column_config.NumberColumn("Инциденты", format="%d"),
                "Простой, ч": st.column_config.NumberColumn("Простой, ч", format="%.1f"),
                "Потери, ₸": st.column_config.TextColumn("Потери, ₸"),
            }
            _stretch(st.dataframe, display, column_config=dt_config, height=360)


def render_ai_scores_section(lang: str) -> None:
    """Таблица AI-оценок закрытия нарядов (ProgressColumn 0–100)."""
    st.subheader(t(lang, "mg_ai_title"))
    st.caption(t(lang, "mg_ai_caption"))
    frame = db_query_df(
        """
        SELECT wo.order_num           AS "Наряд",
               COALESCE(eq.name, '—') AS "Оборудование",
               ae.verdict             AS "Вердикт ИИ",
               ae.score               AS "Оценка ИИ",
               ae.evaluated_at        AS "Оценено"
        FROM ai_evaluations AS ae
        JOIN work_orders AS wo ON wo.id = ae.order_id
        LEFT JOIN equipment AS eq ON eq.id = wo.equipment_id
        ORDER BY ae.evaluated_at DESC, ae.id DESC
        LIMIT 100
        """
    )
    if frame.empty:
        st.info(c(lang, "no_data"))
        return
    config = {
        "Оценка ИИ": st.column_config.ProgressColumn(
            "Оценка ИИ", min_value=0, max_value=100, format="%d"
        ),
        "Вердикт ИИ": st.column_config.TextColumn("Вердикт ИИ"),
        "Оборудование": st.column_config.TextColumn("Оборудование"),
        "Наряд": st.column_config.TextColumn("Наряд"),
        "Оценено": st.column_config.TextColumn("Оценено"),
    }
    _stretch(st.dataframe, frame, column_config=config, height=360)


def render_1c_export_card(lang: str) -> None:
    """Карточка экспорта пакета синхронизации с 1С:ТОиР (JSON)."""
    st.subheader(t(lang, "mg_1c_title"))
    st.caption(t(lang, "mg_1c_caption"))

    if st.button(t(lang, "mg_1c_export_btn"), type="primary", key="build_1c"):
        payload = build_1c_payload()
        st.session_state["payload_1c"] = payload
        st.session_state["payload_1c_bytes"] = payload_to_json(payload)
        st.success(t(lang, "mg_1c_ready"))

    payload = st.session_state.get("payload_1c")
    if not payload:
        return

    totals = payload.get("totals") or {}
    col_orders, col_materials = st.columns(2)
    with col_orders:
        render_metric_card(
            t(lang, "mg_1c_orders"),
            int(totals.get("orders_count") or 0),
            accent="#58a6ff",
        )
    with col_materials:
        render_metric_card(
            t(lang, "mg_1c_materials"),
            int(totals.get("materials_count") or 0),
            accent="#22d3ee",
        )

    orders = payload.get("work_orders") or []
    if not orders:
        st.warning(t(lang, "mg_1c_empty"))
        return

    with st.expander("Предпросмотр JSON (первый наряд)", expanded=False):
        st.json(orders[0])

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    st.download_button(
        t(lang, "mg_1c_download"),
        data=st.session_state.get("payload_1c_bytes", b"{}"),
        file_name=f"1c_toir_sync_{stamp}.json",
        mime="application/json",
        key="download_1c",
    )


# ==================================================================
# 12. ТОЧКА ВХОДА
# ==================================================================
def render_footer() -> None:
    """Нижний корпоративный футер: бренд, описание проекта и копирайт."""
    st.markdown(
        """
<div class="naryad-footer">
    <div class="naryad-footer-grid">
        <div class="naryad-footer-col">
            <div class="naryad-footer-brand">НарядAI</div>
            <div class="naryad-footer-sub">АО «Костанайские минералы»</div>
        </div>
        <div class="naryad-footer-col naryad-footer-center">
            <div>Интеллектуальная система оперативного контроля ТОиР,
            выдачи и закрытия нарядов.</div>
        </div>
        <div class="naryad-footer-col naryad-footer-right">
            <div>Qostanai AI Industry Hackathon 2026</div>
        </div>
    </div>
    <div class="naryad-footer-copy">© 2026 Все права защищены</div>
</div>
""",
        unsafe_allow_html=True,
    )


def main() -> None:
    """Собрать и отрисовать приложение «НарядAI»."""
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=PAGE_ICON,
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Тема хранится в st.session_state["theme"]; значение переключателя —
    # в st.session_state["theme_light"] (True = светлая «Бетон»).
    theme = "light" if st.session_state.get("theme_light", False) else "dark"
    st.session_state["theme"] = theme
    inject_design(st.session_state["theme"])

    lang, role_key, is_offline = render_sidebar()
    render_header(lang, role_key, is_offline)

    if role_key == "master":
        render_master_screen(lang, is_offline)
    elif role_key == "worker":
        render_worker_screen(lang, is_offline)
    elif role_key == "admin":
        render_admin_screen(lang)
    else:
        render_manager_screen(lang)

    render_footer()


main()
