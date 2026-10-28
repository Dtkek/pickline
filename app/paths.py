# -*- coding: utf-8 -*-
"""Где приложение хранит своё: кэш, портреты, хранилище окна, обновлённые снимки.

Из исходников всё лежит внутри app/, как и раньше: app/.cache, app/assets,
app/data. В собранном exe так нельзя: exe теперь один файл и при каждом
запуске распаковывается во временную папку заново - всё, что было записано
рядом с кодом, пропадёт. Поэтому в exe данные живут в профиле пользователя:

    Windows   %LOCALAPPDATA%\\Pickline
    macOS     ~/Library/Application Support/Pickline
    Linux     ~/.local/share/pickline

Побочная польза: обновление exe больше не стирает портреты и настройки.
Лог и файл токена - рядом с exe: их пользователь должен находить глазами.
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))

# папка с кодом (в exe - временная папка распаковки), только для чтения
APP_DIR = os.path.dirname(os.path.abspath(__file__))
# папка, где лежит exe (из исходников - корень проекта)
EXE_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
           else os.path.dirname(APP_DIR))

# снимки данных из репозитория: только чтение
BUNDLED_DATA = os.path.join(APP_DIR, "data")
WEB_DIR = os.path.join(APP_DIR, "web")


def _user_data_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "Pickline")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Pickline")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "pickline")


DATA_DIR = _user_data_dir() if FROZEN else APP_DIR

# кэш ответов API и памятки вычислений
CACHE_DIR = os.path.join(DATA_DIR, ".cache")
# эталонные портреты героев (качаются при первом запуске слежения)
ASSETS_DIR = os.path.join(DATA_DIR, "assets", "heroes")
# обновлённые в фоне снимки (STRATZ); если нет - берётся из BUNDLED_DATA
UPDATED_DATA = os.path.join(DATA_DIR, "data")
# хранилище окна pywebview: localStorage страницы, cookies
WEBVIEW_DIR = os.path.join(CACHE_DIR, "webview")

LOG_PATH = os.path.join(EXE_DIR, "pickline.log")
TOKEN_FILE = os.path.join(EXE_DIR, "stratz_token.txt")
# ключ Gemini API для голосового тренера (aistudio.google.com) - рядом с exe, в git не попадает
GEMINI_KEY_FILE = os.path.join(EXE_DIR, "gemini_key.txt")


def data_file(name):
    """Путь к снимку данных: обновлённая копия, если есть, иначе из сборки."""
    updated = os.path.join(UPDATED_DATA, name)
    return updated if os.path.exists(updated) else os.path.join(BUNDLED_DATA, name)
