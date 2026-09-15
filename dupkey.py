# -*- coding: utf-8 -*-
# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""Единственное место, где живёт правило «это копия»; его читают app.py и scan.py.

Правило одно на программу намеренно. Панель дубликатов в интерфейсе и проверка
каталога в окне запуска должны отвечать на один и тот же вопрос одинаково: если
проверка скажет «этот файл уже есть», а интерфейс потом его копией не посчитает,
верить нельзя ни тому, ни другому ответу.
"""

import sqlite3

# Чем опознавать копию файла. Имени и размера мало: два разных кадра вполне
# могут совпасть и по тому, и по другому — у одной камеры одинаковые имена
# повторяются, а размер JPEG задаётся сюжетом и попадает в те же байты чаще,
# чем кажется. Поэтому в ключ входит ещё и точное время съёмки: два разных
# кадра не совпадут до секунды, а копии одного файла совпадают всегда.
#
# Время берём только настоящее, из EXIF. Там, где его нет, стоит дата файла —
# у копии она своя, и настоящие копии перестали бы находиться.
#
# Если проход `scan.py --hash-only` посчитал подписи содержимого у всех файлов,
# ключом становится подпись: она отвечает на вопрос точно и находит копии даже
# под другими именами. Пока подписи есть не у всех, они не используются вовсе —
# смешивать два правила в одном ключе нельзя, копия с подписью не нашла бы
# копию без неё.


def name_key(alias=""):
    """Ключ по имени, размеру и времени съёмки. Тем же текстом создан индекс
    ix_dupkey — без него подсчёт размера группы перебирал всю таблицу на каждую
    выводимую строку, и список выборки на архиве в сорок тысяч снимков строился
    сорок секунд. Менять это выражение можно только вместе с индексом."""
    p = alias + "." if alias else ""
    return (f"lower({p}filename) || '|' || {p}size || '|' || "
            f"CASE WHEN {p}date_src = 'exif' THEN {p}taken ELSE '' END")


def dup_key(alias="", by_sig=False):
    """Выражение ключа для запроса. by_sig — результат sigs_complete()."""
    if by_sig:
        return (alias + "." if alias else "") + "sig"
    return name_key(alias)


def sigs_complete(con):
    """Подпись посчитана у каждого снимка?

    База прежней версии колонки sig ещё не знает. Это не ошибка и не повод
    падать: значит, прохода подписей не было, и ключом остаётся имя.
    """
    try:
        miss, = con.execute(
            "SELECT COUNT(*) FROM photos WHERE sig IS NULL").fetchone()
    except sqlite3.OperationalError:
        return False
    return miss == 0


def rule_name(by_sig):
    """Как правило называется в выводе — тем же текстом, что в интерфейсе."""
    return "content signature" if by_sig else "name, size and capture time"
