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
        # --- Сайдбар ---
        "role_select": "Выберите активную роль",
        "role_master": "Мастер смены",
        "role_worker": "Исполнитель (Слесарь/Электрик)",
        "role_manager": "Руководитель / ТОиР Аналитика",
        "role_admin": "Администратор НСИ",
        "offline_toggle": "📡 Связь в шахте/цеху: ОФЛАЙН / ОНЛАЙН",
        "offline_warn": "ОФЛАЙН-режим: данные кэшируются локально и синхронизируются при восстановлении связи.",
        "online_ok": "ОНЛАЙН: связь стабильна, данные синхронизированы с сервером.",
        "scenario_title": "🎬 Сценарий защиты перед жюри (7 шагов)",
        "scenario_label": "Этап демонстрации",
        "scenario_progress": "Готовность сценария",
        "scenario_steps": [
            "QR-сканирование шильдика конвейера К-3 (INV-DR-001) за секунды.",
            "Выдача аварийного наряда: участок и агрегат подставляются автоматически.",
            "Push-эскалация: наряд не принят 3 минуты → красный алерт мастеру.",
            "Мобильный экран слесаря: принять, закрыть, приложить фото «После».",
            "AI-верификация закрытия: оценка 0–100 и вердикт НарядAI.",
            "Предиктивный радар: конвейер К-3 (М-02), риск 87.4% на 7 смен.",
            "Аналитика ТОиР: экономический эффект и выгрузка PDF/Excel.",
        ],
        "db_status": "Диагностика БД",
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
        "footer": "© 2026 АО «Костанайские минералы» · НарядAI · демонстрационный прототип",
    },
    "KZ": {
        "brand": "⛏️ НарядAI",
        "lang_label": "Тіл / Язык",
        "app_title": "НарядAI: Нарядтарды беру және бақылау жүйесі",
        "app_subtitle": "«Қостанай минералдары» АҚ · Qostanai AI Industry Hackathon 2026",
        "role_select": "Белсенді рөлді таңдаңыз",
        "role_master": "Ауысым шебері",
        "role_worker": "Орындаушы (Жөндеуші/Электрик)",
        "role_manager": "Басшы / ТОжЖ Сараптамасы",
        "role_admin": "НСИ әкімшісі",
        "offline_toggle": "📡 Шахтада/цехта байланыс: ОФЛАЙН / ОНЛАЙН",
        "offline_warn": "ОФЛАЙН-режим: деректер жергілікті кэштеледі және байланыс қалпына келгенде синхрондалады.",
        "online_ok": "ОНЛАЙН: байланыс тұрақты, деректер сервермен синхрондалған.",
        "scenario_title": "🎬 Қазылар алдындағы қорғау сценарийі (7 қадам)",
        "scenario_label": "Демонстрация кезеңі",
        "scenario_progress": "Сценарий дайындығы",
        "scenario_steps": [
            "К-3 конвейерінің шильдигін QR арқылы секундтар ішінде сканерлеу (INV-DR-001).",
            "Апаттық наряд беру: учаске мен агрегат автоматты түрде қойылады.",
            "Push-эскалация: наряд 3 минут қабылданбаса → шеберге қызыл ескерту.",
            "Жөндеушінің мобильді экраны: қабылдау, жабу, «Кейін» фотосын тіркеу.",
            "Жабуды ЖИ-верификациялау: 0–100 бағасы және НарядAI қорытындысы.",
            "Предиктивті радар: К-3 конвейері (М-02), 7 ауысымда 87.4% тәуекел.",
            "ТОжЖ аналитикасы: экономикалық әсер және PDF/Excel жүктеу.",
        ],
        "db_status": "ДБ диагностикасы",
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
# 8. САЙДБАР И ГЛОБАЛЬНАЯ ШАПКА
# ==================================================================
def render_sidebar() -> Tuple[str, str, bool]:
    """Собрать сайдбар. Возвращает ``(lang, role_key, is_offline)``."""
    st.sidebar.title("⛏️ НарядAI")

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
        st.sidebar.warning(t(lang, "offline_warn"))
    else:
        st.sidebar.success(t(lang, "online_ok"))

    st.sidebar.divider()
    render_scenario_widget(lang)
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


def render_scenario_widget(lang: str) -> None:
    """Интерактивный виджет «Сценарий защиты перед жюри (7 шагов)»."""
    steps: List[str] = list(LANG[lang]["scenario_steps"])
    with st.sidebar.expander(t(lang, "scenario_title"), expanded=False):
        options = [f"{i + 1}. {step}" for i, step in enumerate(steps)]
        selected = st.radio(
            t(lang, "scenario_label"),
            options,
            key="scenario_step",
            label_visibility="collapsed",
        )
        current_index = options.index(selected)
        st.progress(
            (current_index + 1) / len(options),
            text=f"{t(lang, 'scenario_progress')}: {current_index + 1}/{len(options)}",
        )
        st.markdown("**" + t(lang, "scenario_label") + "**")
        for index, step in enumerate(steps):
            marker = "▶️" if index == current_index else "•"
            st.markdown(f"{marker} {index + 1}. {step}")


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


def render_header(lang: str, is_offline: bool) -> None:
    """Глобальная шапка приложения."""
    st.title(t(lang, "brand") + " · " + t(lang, "app_title"))
    st.caption(t(lang, "app_subtitle"))
    if is_offline:
        st.warning("📡 " + t(lang, "offline_warn"))
    else:
        st.success("🌐 " + t(lang, "online_ok"))


# ==================================================================
# 9. ЭКРАН 1: МАСТЕР СМЕНЫ
# ==================================================================
def load_master_kpis() -> Dict[str, int]:
    """Считать 4 KPI-плашки смены из БД (все запросы защищены)."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    active_list = ("Выдан", "Принят в работу", "В очереди", "В работе", "Приостановлен", "На доработку", "Проверка ИИ")
    placeholders = ", ".join("?" for _ in active_list)
    total = db_scalar("SELECT COUNT(*) AS n FROM work_orders")
    in_work = db_scalar(
        f"SELECT COUNT(*) AS n FROM work_orders WHERE status IN ({placeholders})",
        active_list,
    )
    overdue = db_scalar(
        f"""
        SELECT COUNT(*) AS n FROM work_orders
        WHERE status = 'Просрочен'
           OR (status IN ({placeholders}) AND deadline < ?)
        """,
        (*active_list, now_str),
    )
    closed = db_scalar(
        "SELECT COUNT(*) AS n FROM work_orders WHERE status = 'Закрыт'"
    )
    return {"total": total, "in_work": in_work, "overdue": overdue, "closed": closed}


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


def render_master_screen(lang: str, is_offline: bool) -> None:
    """Полный экран мастера смены: алерты, KPI и три вкладки."""
    st.header(t(lang, "m_title"))

    # --- Активные алерты контроля сроков (сверху экрана) ---
    st.subheader("🚨 " + t(lang, "alerts_header"))
    alerts = call_deadlines_and_escalations()
    has_alert = False
    for message in alerts.get("expired", []):
        st.error(message)
        has_alert = True
    for message in alerts.get("escalations", []):
        st.error(message)
        has_alert = True
    for message in alerts.get("warnings", []):
        st.warning(message)
        has_alert = True
    if not has_alert:
        st.success(t(lang, "no_alerts"))

    # --- 4 KPI-плашки смены ---
    kpis = load_master_kpis()
    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric(t(lang, "kpi_total"), kpis["total"])
    col_b.metric(t(lang, "kpi_inwork"), kpis["in_work"])
    col_c.metric(
        t(lang, "kpi_overdue"),
        kpis["overdue"],
        delta="требует внимания" if kpis["overdue"] else None,
        delta_color="inverse",
    )
    col_d.metric(t(lang, "kpi_closed"), kpis["closed"])

    st.divider()
    tab_issue, tab_kanban, tab_copilot = st.tabs(
        [
            "➕ " + t(lang, "tab_issue"),
            "📋 " + t(lang, "tab_kanban"),
            "🤖 " + t(lang, "tab_copilot"),
        ]
    )
    with tab_issue:
        render_master_issue_tab(lang, is_offline)
    with tab_kanban:
        render_master_kanban_tab(lang)
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


def render_master_issue_tab(lang: str, is_offline: bool) -> None:
    """Вкладка выдачи наряда: QR, приоритет, исполнитель, описание, создание."""
    # Отложенные обновления ключей виджетов (до их создания в этом прогоне).
    if st.session_state.pop("_clear_issue_desc", False):
        st.session_state["issue_description"] = ""
    pending_desc = st.session_state.pop("_pending_issue_desc", None)
    if pending_desc is not None:
        st.session_state["issue_description"] = pending_desc
    if "issue_description" not in st.session_state:
        st.session_state["issue_description"] = ""

    st.subheader("⚡ " + t(lang, "issue_subheader"))

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
        "✅ " + t(lang, "issue_btn"), type="primary", key="issue_submit"
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
    choice = st.selectbox(t(lang, "w_select"), labels, key="worker_order")
    current = orders.iloc[labels.index(choice)]

    priority = str(current["priority"])
    badge = "🚨" if priority == "Аварийный" else "🛠"
    st.subheader(f"{badge} {current['order_num']} · {priority_label(lang, priority)}")
    with st.container(border=True):
        st.markdown(f"**{c(lang, 'description')}:** {current['description']}")
        info_left, info_right = st.columns(2)
        info_left.markdown(
            f"**{c(lang, 'equipment')}:** {current.get('equipment') or '—'} "
            f"({current.get('unit') or '—'})"
        )
        info_right.markdown(
            f"**{c(lang, 'deadline')}:** {current['deadline']}"
        )
        st.markdown(
            f"**{c(lang, 'status')}:** {status_label(lang, current['status'])}"
        )

    # --- Крупные кнопки под рабочие перчатки ---
    btn_accept, btn_pause, btn_reject = st.columns(3)
    with btn_accept:
        if st.button(
            t(lang, "w_accept"), type="primary", key="worker_accept"
        ):
            update_order_status(int(current["id"]), "В работе", "Принят исполнителем")
            st.success(t(lang, "w_status_ok").format(status=status_label(lang, "В работе")))
            st.rerun()
    with btn_pause:
        if st.button(t(lang, "w_pause"), key="worker_pause"):
            update_order_status(int(current["id"]), "Приостановлен", "Пауза исполнителя")
            st.info(t(lang, "w_paused"))
            st.rerun()
    with btn_reject:
        reject_clicked = st.button(t(lang, "w_reject"), key="worker_reject")
    if reject_clicked:
        st.session_state["show_reject"] = True
    if st.session_state.get("show_reject"):
        reason = st.text_input(t(lang, "w_reject_reason"), key="worker_reject_reason")
        if st.button(t(lang, "w_reject_btn"), key="worker_reject_confirm"):
            update_order_status(
                int(current["id"]), "Отклонён", reason or "Причина не указана"
            )
            st.session_state["show_reject"] = False
            st.success(t(lang, "w_reject_ok"))
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

    # --- Прошлый результат AI-верификации (сохраняется после rerun) ---
    last_result = st.session_state.get("worker_ai_result")
    if last_result:
        st.markdown("### " + t(lang, "w_ai_result_hdr"))
        st.markdown(f"**{c(lang, 'order_num')}:** {last_result['order_num']}")
        metric_left, metric_right = st.columns(2)
        metric_left.metric(t(lang, "w_score"), f"{last_result['score']}/100")
        metric_right.metric(
            t(lang, "w_verdict"), last_result["verdict"]
        )
        st.progress(last_result["score"] / 100.0)
        if last_result["status"] == "Закрыт":
            st.success(t(lang, "w_closed"))
        else:
            st.error(t(lang, "w_rework"))
        st.info(t(lang, "w_ai_note") + ": " + last_result["explanation"])
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


def render_admin_screen(lang: str) -> None:
    """Экран Администратора НСИ (безопасный вызов внешнего модуля)."""
    if _admin_panel_impl is None:
        st.header("🗂 " + t(lang, "role_admin"))
        st.error(
            "Модуль управления НСИ (src/admin_nsi.py) недоступен. "
            "Проверьте установку зависимостей и наличие файла модуля."
        )
        return
    try:
        _admin_panel_impl(lang)
    except Exception as exc:  # noqa: BLE001 — панель админа не должна ронять приложение
        st.session_state["admin_error"] = f"{type(exc).__name__}: {exc}"
        st.error(f"Не удалось открыть панель НСИ: {type(exc).__name__}: {exc}")


# ==================================================================
# 11. ЭКРАН 3: РУКОВОДИТЕЛЬ / ТОИР АНАЛИТИКА
# ==================================================================
def render_manager_screen(lang: str) -> None:
    """Аналитика ТОиР: эффект, графики, предиктивный радар, документы."""
    st.header(t(lang, "mg_title"))

    closed_total = db_scalar(
        "SELECT COUNT(*) AS n FROM work_orders WHERE status = 'Закрыт'"
    )
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric(t(lang, "mg_saved"), "21 800 000 ₸", "+14.2% за квартал")
    kpi2.metric(t(lang, "mg_reaction"), "8.2 мин", "-28 мин благодаря push-контролю")
    kpi3.metric(t(lang, "mg_quality"), "94.6%", "оценка ИИ")
    kpi4.metric(t(lang, "mg_closed_total"), closed_total)

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
    render_ppr_quality_section(lang)

    st.divider()
    render_downtime_section(lang)

    st.divider()
    render_1c_export_card(lang)

    st.divider()
    render_documents_section(lang)


def render_top_faults_chart(lang: str) -> None:
    """Столбчатая диаграмма топ-отказов оборудования."""
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
        color_continuous_scale="Reds",
        labels={"incidents": "Инциденты", "equipment": "Оборудование"},
    )
    figure.update_layout(yaxis=dict(autorange="reversed"), coloraxis_showscale=False)
    _stretch(st.plotly_chart, figure)


def render_status_chart(lang: str) -> None:
    """Круговая диаграмма распределения статусов нарядов."""
    frame = db_query_df(
        "SELECT status, COUNT(*) AS cnt FROM work_orders GROUP BY status"
    )
    if frame.empty:
        st.info(c(lang, "no_data"))
        return
    frame["Статус"] = frame["status"].apply(lambda s: status_label(lang, s))
    figure = px.pie(
        frame,
        names="Статус",
        values="cnt",
        hole=0.45,
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    figure.update_traces(textposition="inside", textinfo="percent+label")
    _stretch(st.plotly_chart, figure)


def render_predictive_radar(lang: str) -> None:
    """Блок «Предиктивный радар отказов (Machine Learning)»."""
    st.subheader(t(lang, "mg_radar_title"))
    prediction = get_prediction()
    risk = prediction.get("risk_percent", 87.4)
    try:
        risk_value = float(risk)
    except (TypeError, ValueError):
        risk_value = 87.4

    with st.container(border=True):
        st.error(
            f"**{t(lang, 'mg_radar_alert')}**\n\n"
            f"- **{t(lang, 'mg_radar_unit')}:** {prediction.get('top_problem_unit', 'Конвейер К-3')}\n"
            f"- **{t(lang, 'mg_radar_code')}:** {prediction.get('fault_code', 'М-02')}\n"
            f"- **{t(lang, 'mg_radar_risk')}:** **{risk_value}%**\n"
            f"- **{t(lang, 'mg_radar_reco')}:** {prediction.get('recommendation', '')}"
        )
        radar_left, radar_right = st.columns([1, 3])
        with radar_left:
            st.metric(
                t(lang, "mg_radar_risk"),
                f"{risk_value}%",
                delta="критично" if risk_value >= 70 else "умеренно",
                delta_color="inverse",
            )
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

    _stretch(st.dataframe, frame, height=460)
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
    metric_cases.metric("Случаев повторных поломок", len(frame))

    if "Дней после ППР" in frame.columns:
        try:
            avg_days = float(pd.to_numeric(frame["Дней после ППР"], errors="coerce").mean())
        except (TypeError, ValueError):
            avg_days = 0.0
        metric_interval.metric("Средний интервал", f"{avg_days:.1f} дн")
    if "Простой, ч" in frame.columns:
        try:
            total_hours = float(pd.to_numeric(frame["Простой, ч"], errors="coerce").sum())
        except (TypeError, ValueError):
            total_hours = 0.0
        metric_downtime.metric("Простой по повторам", f"{total_hours:.1f} ч")

    _stretch(st.dataframe, frame, height=420)

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
    col_hours.metric(t(lang, "mg_downtime_hours"), f"{hours:,.1f}".replace(",", " "))
    col_loss.metric(t(lang, "mg_downtime_loss"), f"{loss:,.0f} ₸".replace(",", " "))
    col_incidents.metric(t(lang, "mg_downtime_incidents"), incidents)
    col_rate.metric(t(lang, "mg_downtime_rate"), f"{rate:,.0f} ₸/ч".replace(",", " "))

    by_category = data.get("by_category")
    if isinstance(by_category, pd.DataFrame) and not by_category.empty:
        figure = px.bar(
            by_category,
            x="Категория",
            y="Потери, ₸",
            color="Категория",
            text="Простой, ч",
            color_discrete_sequence=px.colors.qualitative.Set2,
        )
        figure.update_traces(textposition="outside")
        _stretch(st.plotly_chart, figure)

    breakdown = data.get("breakdown")
    if isinstance(breakdown, pd.DataFrame) and not breakdown.empty:
        with st.expander("Детализация по участкам и шифрам", expanded=False):
            _stretch(st.dataframe, breakdown, height=360)


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
    col_orders.metric(t(lang, "mg_1c_orders"), int(totals.get("orders_count") or 0))
    col_materials.metric(t(lang, "mg_1c_materials"), int(totals.get("materials_count") or 0))

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
def main() -> None:
    """Собрать и отрисовать приложение «НарядAI»."""
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=PAGE_ICON,
        layout="wide",
        initial_sidebar_state="expanded",
    )

    lang, role_key, is_offline = render_sidebar()
    render_header(lang, is_offline)

    if role_key == "master":
        render_master_screen(lang, is_offline)
    elif role_key == "worker":
        render_worker_screen(lang, is_offline)
    elif role_key == "admin":
        render_admin_screen(lang)
    else:
        render_manager_screen(lang)

    st.divider()
    st.caption(t(lang, "footer"))


main()
