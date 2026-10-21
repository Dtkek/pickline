# -*- coding: utf-8 -*-
"""Свежие снимки данных с GitHub - без переустановки и без OpenDota.

Снимки (app/data/*.json.gz) раз в неделю пересобирает CI и коммитит в
репозиторий. Exe везёт с собой те, что были на момент сборки, и у того,
кто не переустанавливает приложение, они стареют («показан снимок от
19.09»). А на канале, где OpenDota не открывается (российские провайдеры
обрывают соединения с сайтами за Cloudflare), живых данных нет вовсе -
только снимок.

GitHub (raw.githubusercontent.com) при этом открывается. Поэтому при
запуске в фоне скачиваем снимки из репозитория и, если в них дата «снято»
новее, кладём в папку пользователя (paths.UPDATED_DATA). Загрузчики берут
файл через paths.data_file(): обновлённая копия, если есть, иначе из
сборки. После обновления сбрасываем их кэши - новые данные видны сразу,
без перезапуска.

Из исходников (не exe) ничего не делаем: там UPDATED_DATA - это сам
app/data в репозитории, свежие снимки приходят через git pull.
"""
import gzip
import json
import os
import threading
import time

import net
import paths

RAW = "https://raw.githubusercontent.com/Dtkek/pickline/main/app/data/"
NAMES = [
    "herostats_fallback.json.gz", "matchups_fallback.json.gz", "items_fallback.json.gz",
    "abilities_fallback.json.gz", "stratz_fallback.json.gz", "tournament_fallback.json.gz",
    "builds_fallback.json.gz", "wards_fallback.json.gz",
]
# чаще не нужно: CI обновляет снимки раз в неделю
CHECK_EVERY = 12 * 3600
STAMP = os.path.join(paths.UPDATED_DATA, ".checked")

status = {"checked": False, "updated": [], "error": None, "newest": None}
_resets = []


def on_update(fn):
    """Загрузчик регистрирует сброс своего кэша: вызовется после обновления."""
    _resets.append(fn)


def snapshot_date(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f).get("снято")
    except (OSError, ValueError, AttributeError):
        return None


def update_once():
    os.makedirs(paths.UPDATED_DATA, exist_ok=True)
    updated, errors = [], []
    for name in NAMES:
        cur = snapshot_date(paths.data_file(name))
        tmp = os.path.join(paths.UPDATED_DATA, name + ".new")
        if os.path.exists(tmp):
            os.remove(tmp)
        ok, why = net.download(RAW + name, tmp, timeout=60)
        if not ok:
            errors.append(f"{name}: {why}")
            continue
        new = snapshot_date(tmp)
        if new and (not cur or new > cur):
            os.replace(tmp, os.path.join(paths.UPDATED_DATA, name))
            updated.append(f"{name} {cur or '-'} -> {new}")
        else:
            os.remove(tmp)
    if updated:
        for fn in _resets:
            try:
                fn()
            except Exception:  # noqa: BLE001 - сброс кэша не должен ронять обновление
                pass
    dates = [d for d in (snapshot_date(paths.data_file(n)) for n in NAMES) if d]
    status.update(checked=True, updated=updated, newest=max(dates) if dates else None,
                  error="; ".join(errors)[:300] or None)
    if not errors:
        with open(STAMP, "w") as f:
            f.write(str(int(time.time())))
    return updated


def start_in_background(log=print):
    if not paths.FROZEN:
        status.update(checked=True, error="из исходников - снимки обновляет git pull")
        return
    try:
        if time.time() - os.path.getmtime(STAMP) < CHECK_EVERY:
            status["checked"] = True
            return
    except OSError:
        pass

    def worker():
        try:
            for line in update_once():
                log(f"  снимок обновлён с GitHub: {line}")
        except Exception as e:  # noqa: BLE001 - без сети останемся на старых снимках
            status.update(checked=True, error=str(e)[:200])

    threading.Thread(target=worker, daemon=True).start()
