# -*- coding: utf-8 -*-
"""
Модуль схемы БД SQLite для проекта «НарядAI».
Строго по Разделу 8 ТЗ АО «Костанайские минералы».
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "naryad_ai.db")

def get_connection():
    """Соединение с БД с включенными внешними ключами."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn

def init_schema():
    conn = get_connection()
    c = conn.cursor()

    # 1. Участки (дробление, обогащение, РМЦ и т.д.)
    c.execute('''
    CREATE TABLE IF NOT EXISTS units (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT
    );
    ''')

    # 2. Оборудование с привязкой к участку
    c.execute('''
    CREATE TABLE IF NOT EXISTS equipment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        inv_number TEXT NOT NULL UNIQUE,
        unit_id INTEGER NOT NULL,
        eq_type TEXT NOT NULL,
        criticality TEXT NOT NULL CHECK(criticality IN ('Низкая', 'Средняя', 'Высокая', 'Критическая')),
        FOREIGN KEY (unit_id) REFERENCES units(id) ON DELETE RESTRICT
    );
    ''')

    # 3. Сотрудники и смены
    c.execute('''
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fio TEXT NOT NULL,
        specialty TEXT NOT NULL,
        grade INTEGER NOT NULL,
        team_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('Мастер', 'Исполнитель', 'Руководитель', 'Администратор')),
        shift TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Свободен' CHECK(status IN ('Свободен', 'В работе', 'В очереди', 'Не на смене'))
    );
    ''')

    # Справочник шифров неисправностей (М, Э, Г, П, С)
    c.execute('''
    CREATE TABLE IF NOT EXISTS fault_codes (
        code TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        description TEXT NOT NULL
    );
    ''')

    # Справочник ТМЦ и материалов
    c.execute('''
    CREATE TABLE IF NOT EXISTS materials_catalog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        unit_measure TEXT NOT NULL,
        standard_stock INTEGER DEFAULT 100
    );
    ''')

    # 4. Наряды (10 статусов жизненного цикла)
    c.execute('''
    CREATE TABLE IF NOT EXISTS work_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_num TEXT NOT NULL UNIQUE,
        order_type TEXT NOT NULL CHECK(order_type IN ('Плановый', 'Внеплановый (аварийный)')),
        description TEXT NOT NULL,
        unit_id INTEGER NOT NULL,
        equipment_id INTEGER NOT NULL,
        assignee_id INTEGER NOT NULL,
        master_id INTEGER NOT NULL,
        priority TEXT NOT NULL CHECK(priority IN ('Аварийный', 'Высокий', 'Обычный', 'Плановый')),
        normative_hours REAL DEFAULT 2.0,
        status TEXT NOT NULL DEFAULT 'Выдан' CHECK(status IN (
            'Выдан', 'Принят в работу', 'В очереди', 'В работе',
            'Приостановлен', 'Отклонён', 'Исполнено', 'Проверка ИИ',
            'На доработку', 'Закрыт'
        )),
        created_at TEXT NOT NULL,
        deadline TEXT NOT NULL,
        completed_at TEXT,
        closing_comment TEXT,
        fault_code TEXT,
        downtime_hours REAL DEFAULT 0.0,
        FOREIGN KEY (unit_id) REFERENCES units(id),
        FOREIGN KEY (equipment_id) REFERENCES equipment(id),
        FOREIGN KEY (assignee_id) REFERENCES employees(id),
        FOREIGN KEY (master_id) REFERENCES employees(id),
        FOREIGN KEY (fault_code) REFERENCES fault_codes(code)
    );
    ''')

    # 5. События наряда (история переходов статусов)
    c.execute('''
    CREATE TABLE IF NOT EXISTS order_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        reason TEXT,
        event_time TEXT NOT NULL,
        FOREIGN KEY (order_id) REFERENCES work_orders(id) ON DELETE CASCADE,
        FOREIGN KEY (author_id) REFERENCES employees(id)
    );
    ''')

    # 6. Фото «до / после»
    c.execute('''
    CREATE TABLE IF NOT EXISTS order_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        photo_type TEXT NOT NULL CHECK(photo_type IN ('до', 'после')),
        file_path TEXT NOT NULL,
        comment TEXT,
        captured_at TEXT NOT NULL,
        author_id INTEGER NOT NULL,
        FOREIGN KEY (order_id) REFERENCES work_orders(id) ON DELETE CASCADE,
        FOREIGN KEY (author_id) REFERENCES employees(id)
    );
    ''')

    # 7. Списание материалов
    c.execute('''
    CREATE TABLE IF NOT EXISTS material_writeoffs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        material_id INTEGER NOT NULL,
        quantity REAL NOT NULL,
        FOREIGN KEY (order_id) REFERENCES work_orders(id) ON DELETE CASCADE,
        FOREIGN KEY (material_id) REFERENCES materials_catalog(id)
    );
    ''')

    # 8. Оценки ИИ
    c.execute('''
    CREATE TABLE IF NOT EXISTS ai_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL UNIQUE,
        verdict TEXT NOT NULL CHECK(verdict IN ('Принято', 'Принято с замечаниями', 'Требует доработки')),
        score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
        explanation TEXT NOT NULL,
        master_score INTEGER,
        master_override_reason TEXT,
        evaluated_at TEXT NOT NULL,
        FOREIGN KEY (order_id) REFERENCES work_orders(id) ON DELETE CASCADE
    );
    ''')

    conn.commit()
    conn.close()
    print("[SUCCESS] База данных naryad_ai.db создана! 8 сущностей готовы.")

if __name__ == "__main__":
    init_schema()