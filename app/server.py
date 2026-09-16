# -*- coding: utf-8 -*-
"""Локальный веб-сервер Pickline.

Запуск:  python3 app/server.py
Затем открыть http://127.0.0.1:8777 (браузер откроется сам).

Никаких зависимостей: только стандартная библиотека.
"""
import os

# Одно ядро под все вычисления - до импорта numpy и opencv, иначе они
# уже подняли свои пулы потоков. Игре нужны ядра больше, чем нам.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "OPENCV_FOR_THREADS_NUM"):
    os.environ.setdefault(_var, "1")

import argparse
import json
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Русский вывод при перенаправлении в файл на Windows: без этого локальная
# кодировка (cp1251/cp1252) роняет первый же print с кириллицей
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import gsi  # noqa: E402
import net  # noqa: E402
import scoring  # noqa: E402
import vision  # noqa: E402
import window  # noqa: E402
from sources import get_source, get_stratz  # noqa: E402

_watcher = None
_watcher_lock = threading.Lock()


IN_GAME_STATES = {"DOTA_GAMERULES_STATE_PRE_GAME", "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
                  "DOTA_GAMERULES_STATE_POST_GAME"}


def _gsi_says_in_game():
    s = gsi.state()
    return bool(s.get("alive")) and s.get("game_state") in IN_GAME_STATES


def get_watcher():
    """Создаёт наблюдателя за экраном при первом обращении.

    Зависимости зрения опциональные, поэтому импорт отложенный: без них
    приложение работает как обычно, просто без чтения экрана.
    """
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            from vision.watcher import ScreenWatcher
            src = get_source()
            names = {h["id"]: h.get("localized_name") for h in src.hero_stats()}
            # источник нужен наблюдателю, чтобы самому докачать портреты
            _watcher = ScreenWatcher(names, source=src)
            # если игра на связи и матч уже идёт - экран не сканируем
            _watcher.pause_check = _gsi_says_in_game
            # своя сторона по данным игры: подписи ролей раскладываются
            # по слотам от центра экрана, а Radiant слева, Dire справа
            _watcher.team_hint = lambda: gsi.state().get("team")
        return _watcher


def vision_status():
    """Состояние чтения экрана.

    Функция обязана отвечать и тогда, когда пакеты зрения не установлены:
    её задача в этом случае — сказать, чего не хватает. Поэтому всё, что
    тянет opencv, numpy или mss, импортируется только после проверки.
    """
    from vision import icons  # зависит лишь от стандартной библиотеки
    src = get_source()
    have = len(icons.available_ids())
    total = len(src.hero_stats())
    out = {
        "available": vision.AVAILABLE,
        "hint": vision.requirements_hint(),
        "icons": {"have": have, "total": total, "ready": have >= total * 0.95},
        "monitors": [],
        "state": None,
    }
    if vision.AVAILABLE:
        from vision import capture
        out["monitors"] = capture.monitors()
    if vision.AVAILABLE and have:
        w = get_watcher()
        out["state"] = w.state()
        out["config"] = {"monitor": w.monitor, "interval": w.interval,
                         "region": list(w.region)}
        # файлов на диске и эталонов в распознавателе может быть разное
        # число: на Windows cv2.imread молчит на путях с кириллицей
        out["templates_loaded"] = len(w.recognizer.ids)
        out["assets_dir"] = icons.ASSETS
    return out


def vision_self_test():
    """Самопроверка распознавания на собственных эталонах."""
    from vision import recognize
    w = get_watcher()
    if not w.recognizer.ready:
        w.reload_templates()
    return recognize.self_test(w.recognizer)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
CDN = "https://cdn.cloudflare.steamstatic.com"

# Версия показывается в консоли и в шапке страницы: когда что-то идёт не так,
# первым делом нужно понять, какой код на самом деле запущен.
VERSION = "2026-09-16.4"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def hero_list(source):
    """Справочник героев для интерфейса: имя, картинка, роли, позиции."""
    positions = hero_positions()
    out = []
    for h in source.hero_stats():
        out.append({
            "id": h["id"],
            "name": h.get("localized_name"),
            "img": CDN + h["img"] if h.get("img") else None,
            "roles": h.get("roles") or [],
            "positions": positions.get(h["id"], []),
            "attr": h.get("primary_attr"),
        })
    out.sort(key=lambda x: (x["name"] or "").lower())
    return out


POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "positions.json")

POSITION_LABELS = [
    ("1", "Керри (1)"),
    ("2", "Мид (2)"),
    ("3", "Хард (3)"),
    ("4", "Роумер (4)"),
    ("5", "Хардсаппорт (5)"),
]

_positions_cache = None


def hero_positions():
    """Справочник позиций: {hero_id: [позиции]}.

    Это не данные источника, а файл, составленный вручную: OpenDota позиций
    не отдаёт. Файл перечитывается при изменении, чтобы правки подхватывались
    без перезапуска сервера.
    """
    global _positions_cache
    try:
        mtime = os.path.getmtime(POSITIONS_FILE)
    except OSError:
        return {}
    if _positions_cache and _positions_cache[0] == mtime:
        return _positions_cache[1]
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            raw = json.load(f).get("positions", {})
        table = {int(k): list(v) for k, v in raw.items()}
    except (OSError, ValueError):
        return {}
    _positions_cache = (mtime, table)
    return table


def ids_for_position(position):
    """id героев, играющих на этой позиции. None — фильтр не задан."""
    if not position:
        return None
    try:
        want = int(position)
    except (TypeError, ValueError):
        return None
    return {hid for hid, pos in hero_positions().items() if want in pos}


POSITION_NAMES = {1: "Лёгкая", 2: "Центр", 3: "Сложная", 4: "Поддержка", 5: "Полная поддержка"}


def screen_roles():
    """Подписи ролей с экрана из слежения: {"slots", "taken", "side"} или None.

    Есть только пока слежение включено и подписи читаются (рейтинг с
    выбором ролей, своя команда). Устаревшие данные не отдаём: если
    последний скан старше минуты, драфт мог смениться.
    """
    if not vision.AVAILABLE or _watcher is None:
        return None
    st = _watcher.state()
    roles = st.get("roles")
    if not roles or not any(roles.get("slots") or []):
        return None
    if not st.get("last_scan") or time.time() - st["last_scan"] > 60:
        return None
    return roles


# позиция считается занятой союзником, если на неё приходится хотя бы
# столько его «веса»: герой строго одной позиции занимает её целиком,
# герой двух позиций - каждую наполовину
OCCUPIED_AT = 0.5


def free_positions(ally_ids):
    """Какие позиции остались свободными, судя по взятым союзникам.

    Только по тому, что видно на экране (или отмечено рукой): игра свою
    роль из очереди не сообщает, а история аккаунта здесь не участвует -
    по решению пользователя. Позиции союзников - по справочнику из
    про-матчей; герой на нескольких позициях занимает каждую частично.

    Возвращает (свободные позиции, занятые позиции). Без союзников
    свободны все пять - то есть фильтра нет.
    """
    positions = hero_positions()
    occupied = {p: 0.0 for p in range(1, 6)}
    for hid in ally_ids:
        pos = positions.get(int(hid)) or []
        for p in pos:
            occupied[p] = min(1.0, occupied[p] + 1.0 / len(pos))
    taken = [p for p in range(1, 6) if occupied[p] >= OCCUPIED_AT]
    free = [p for p in range(1, 6) if p not in taken]
    # если союзники «заняли» всё (справочник неточен) - фильтр не нужен
    return (free or list(range(1, 6))), taken


def resolve_bracket(source, requested):
    """Проверяет, что по запрошенному рангу есть данные.

    Если нет — честно откатывается на «Все ранги» и возвращает пояснение,
    чтобы интерфейс показал чужие числа не молча.
    """
    available = {b["key"] for b in source.available_brackets()}
    if requested in available:
        return requested, None
    return "all", (f"по рангу «{requested}» у источника нет данных, "
                   f"показаны все ранги")


def meta_table(source, bracket, role=None, allowed_ids=None):
    """Таблица меты: винрейт, доля пиков, про-пики и про-баны."""
    stats = source.hero_stats()
    total_picks = 0
    rows = []
    for h in stats:
        picks, wins = source.picks_wins(h, bracket)
        if allowed_ids is not None and h["id"] not in allowed_ids:
            continue
        if role and role not in (h.get("roles") or []):
            continue
        total_picks += picks
        rows.append({
            "id": h["id"],
            "name": h.get("localized_name"),
            "img": CDN + h["img"] if h.get("img") else None,
            "roles": h.get("roles") or [],
            "picks": picks,
            "winrate": round(wins / picks * 100, 2) if picks else None,
            "pro_pick": h.get("pro_pick") or 0,
            "pro_ban": h.get("pro_ban") or 0,
            "pro_winrate": (round((h.get("pro_win") or 0) / h["pro_pick"] * 100, 2)
                            if h.get("pro_pick") else None),
        })
    for r in rows:
        r["pick_share"] = round(r["picks"] / total_picks * 100, 2) if total_picks else 0
    rows.sort(key=lambda r: (r["winrate"] is None, -(r["winrate"] or 0)))
    return rows


TOUR_SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "tournament_fallback.json.gz")
# турнирная статистика свежая столько же, сколько кэш её запросов к базе
TOUR_FRESH = 6 * 3600
# устаревшая памятка старше этого не используется - лучше снимок из репозитория
TOUR_MAX_AGE = 14 * 24 * 3600

_tour_snapshot_cache = None
tour_snapshot_date = None


def tournament_snapshot():
    """Снимок турнирной статистики из репозитория: {tier: {months: {rows, total}}}."""
    global _tour_snapshot_cache, tour_snapshot_date
    if _tour_snapshot_cache is not None:
        return _tour_snapshot_cache
    try:
        import gzip
        with gzip.open(TOUR_SNAPSHOT, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        _tour_snapshot_cache = payload.get("stats") or {}
        tour_snapshot_date = payload.get("снято")
    except (OSError, ValueError):
        _tour_snapshot_cache = {}
    return _tour_snapshot_cache


def tournament_fast(source, months=3, tier="top", leagueid=None, wait=False):
    """Турнирная статистика без ожидания сети: (rows, total, pending).

    Во время пика ждать нельзя: сбор статистики - несколько запросов к базе
    OpenDota, каждый по секундам, а при её сбоях - по минутам с повторами.
    Порядок: свежая памятка → устаревшая памятка (пересчёт в фоне) →
    снимок из репозитория (пересчёт в фоне) → ничего, pending=True, и
    страница дозапросит позже. wait=True - посчитать сейчас (вкладка
    «Турниры», там пользователь готов подождать) и обновить памятку.
    """
    key = f"tournament/{int(months)}/{tier}/{int(leagueid or 0)}"

    def compute():
        rows, total = source.tournament_stats(months, tier, leagueid)
        return {"rows": rows, "total": total}

    if wait:
        data = compute()
        net.memo_write(key, data)
        return data["rows"], data["total"], False

    memo = net.memo_read(key, TOUR_FRESH)
    if memo:
        return memo["rows"], memo["total"], False
    net.compute_in_background(key, compute)
    stale = net.memo_read(key, TOUR_MAX_AGE)
    if stale:
        return stale["rows"], stale["total"], False
    if not leagueid:
        snap = (tournament_snapshot().get(tier) or {}).get(str(int(months)))
        if snap:
            return snap["rows"], snap["total"], False
    return None, 0, True


def tournament_table(source, months=3, tier="top", leagueid=None):
    """Турнирная статистика по героям с долями от числа матчей."""
    rows, total, _ = tournament_fast(source, months, tier, leagueid, wait=True)
    stats_by_id = {h["id"]: h for h in source.hero_stats()}
    out = []
    for r in rows:
        s = stats_by_id.get(r["hero_id"], {})
        picks, bans, wins = int(r["picks"]), int(r["bans"]), int(r["wins"])
        out.append({
            "id": r["hero_id"],
            "name": s.get("localized_name"),
            "img": CDN + s["img"] if s.get("img") else None,
            "picks": picks,
            "bans": bans,
            "wins": wins,
            "winrate": round(wins / picks * 100, 1) if picks else None,
            "pick_rate": round(picks / total * 100, 1) if total else 0,
            "ban_rate": round(bans / total * 100, 1) if total else 0,
            "contest_rate": round((picks + bans) / total * 100, 1) if total else 0,
        })
    return out, total


def pro_matches(source, limit=40):
    out = []
    for m in source.pro_matches()[:limit]:
        out.append({
            "match_id": m.get("match_id"),
            "league": m.get("league_name"),
            "radiant": m.get("radiant_name") or m.get("radiant_team_name") or "Radiant",
            "dire": m.get("dire_name") or m.get("dire_team_name") or "Dire",
            "radiant_win": m.get("radiant_win"),
            "duration": m.get("duration"),
            "start_time": m.get("start_time"),
            "score": [m.get("radiant_score"), m.get("dire_score")],
        })
    return out


# расходники в ленте сборки только шумят; стартовый закуп показываем целиком
CONSUMABLE_QUALS = {"consumable", "consumable;laning"}

# по имени, на случай если в справочнике у чего-то из этого нет метки
CONSUMABLE_KEYS = {
    "tango", "tango_single", "flask", "clarity", "faerie_fire", "enchanted_mango",
    "ward_observer", "ward_sentry", "ward_dispenser", "smoke_of_deceit", "dust",
    "tpscroll", "blood_grenade", "tome_of_knowledge", "cheese", "famango",
    "great_famango", "greater_famango",
}


def is_consumable(key, qual):
    return key in CONSUMABLE_KEYS or qual in CONSUMABLE_QUALS


def is_build_item(key, it):
    """Предмет, который стоит показывать в сборке: не расходник, не рецепт и
    не часть другого предмета. Признак part считается при сборке снимка
    справочника (см. tools/update_items.py): базовый предмет, входящий в
    состав другого. По метке qual это не определить - component стоит
    и на Blink Dagger."""
    it = it or {}
    if is_consumable(key, it.get("qual")) or key.startswith("recipe_"):
        return False
    return not it.get("part")


def item_builds(player, items_by_name, items_by_id):
    """Предметы игрока: стартовый закуп, сборка по времени, итоговый инвентарь.

    purchase_log — покупки с временем в секундах от рога; отрицательное время
    значит «до начала игры», это и есть стартовый закуп. В сборке расходники
    и компоненты не показываем: интересны собранные предметы и порядок.
    """
    def info(name):
        it = items_by_name.get(name)
        if not it:
            return {"key": name, "dname": name, "img": None, "cost": 0}
        return {"key": name, "dname": it.get("dname") or name,
                "img": CDN + it["img"] if it.get("img") else None,
                "cost": it.get("cost") or 0}

    log = sorted((e for e in (player.get("purchase_log") or []) if e.get("key")),
                 key=lambda e: e.get("time", 0))
    start = [info(e["key"]) for e in log if (e.get("time") or 0) <= 0]

    build = []
    first_time = {}
    for e in log:
        t = e.get("time") or 0
        if t > 0:
            first_time.setdefault(e["key"], t)
        if t <= 0:
            continue
        it = items_by_name.get(e["key"]) or {}
        if not is_build_item(e["key"], it):
            continue
        entry = info(e["key"])
        entry["minute"] = int(t // 60)
        build.append(entry)

    final = []
    for slot in ("item_0", "item_1", "item_2", "item_3", "item_4", "item_5"):
        iid = player.get(slot)
        if iid:
            name = items_by_id.get(int(iid))
            if name and not is_consumable(name, (items_by_name.get(name) or {}).get("qual")):
                entry = info(name)
                t = first_time.get(name)
                entry["minute"] = int(t // 60) if t else None
                final.append(entry)
    # итог - в порядке покупки; собранные без рецепта в закупах не значатся
    final.sort(key=lambda x: (x["minute"] is None, x["minute"] or 0))
    neutral = None
    if player.get("item_neutral"):
        name = items_by_id.get(int(player["item_neutral"]))
        if name:
            neutral = info(name)
    return {"start": start, "build": build, "final": final, "neutral": neutral,
            "has_log": bool(log)}


STEAM64_BASE = 76561197960265728


def parse_account(text):
    """Достаёт account_id из того, что человек скопировал.

    Понимает: числовой account_id, Steam64, ссылки на профиль OpenDota,
    Dotabuff и steamcommunity.com/profiles/. Ссылку вида
    steamcommunity.com/id/<имя> разобрать нельзя без ключа Steam API.
    """
    import re
    s = (text or "").strip()
    if not s:
        return None, "пусто"
    m = re.search(r"/(?:players|profiles)/(\d+)", s)
    if m:
        s = m.group(1)
    if "steamcommunity.com/id/" in s:
        return None, ("ссылка с именем профиля не подходит — нужен числовой ID: "
                      "откройте свой профиль на opendota.com или dotabuff.com "
                      "и скопируйте ссылку оттуда")
    if not s.isdigit():
        return None, "не похоже на ID: нужны только цифры или ссылка на профиль"
    n = int(s)
    if n > STEAM64_BASE:
        n -= STEAM64_BASE
    return n, None


def player_summary(source, account_id):
    """Профиль игрока для проверки: ник, ранг, игры, топ героев."""
    prof = source.player_profile(account_id) or {}
    p = prof.get("profile") or {}
    wl = source.player_wl(account_id) or {}
    heroes = source.player_heroes(account_id) or []
    stats_by_id = {h["id"]: h for h in source.hero_stats()}
    total = int(wl.get("win") or 0) + int(wl.get("lose") or 0)
    top = sorted(heroes, key=lambda h: -int(h.get("games") or 0))[:8]
    return {
        "account_id": account_id,
        "name": p.get("personaname"),
        "avatar": p.get("avatarfull"),
        "rank_tier": prof.get("rank_tier"),
        "games": total,
        "winrate": round(int(wl.get("win") or 0) / total * 100, 1) if total else None,
        "public": total > 0,
        "top_heroes": [{
            "id": h["hero_id"],
            "name": stats_by_id.get(h["hero_id"], {}).get("localized_name"),
            "img": CDN + stats_by_id[h["hero_id"]]["img"]
                   if stats_by_id.get(h["hero_id"], {}).get("img") else None,
            "games": int(h.get("games") or 0),
            "winrate": round(int(h.get("win") or 0) / int(h["games"]) * 100)
                       if int(h.get("games") or 0) else None,
        } for h in top],
    }


# меньше стольких игр против этого драфта - показываем общую сборку,
# а условную - только как подсказку о сдвигах
MIN_VS_GAMES = 15

# Предметы, из которых в одной игре берут один: сборка считается по каждому
# предмету независимо («в скольких играх куплен»), и без этого в неё
# попадали сразу Power Treads и Arcane Boots - оба выше порога, потому что
# в разных играх берут разные. В сборке остаётся самый частый из группы,
# остальные показываются как альтернативы. Поздние апгрейды (Travel,
# Greaves, Bearing) - отдельные шаги, а не альтернативы: их берут поверх.
EXCLUSIVE_GROUPS = [
    {"power_treads", "phase_boots", "arcane_boots", "tranquil_boots"},
    {"overwhelming_blink", "swift_blink", "arcane_blink"},
]


def _collapse_alternatives(order, situational):
    """Схлопывает взаимоисключающие предметы: один в сборке, остальные - в нём.

    Победитель - самый частый в группе среди попавших в сборку (order);
    остальные члены группы и из сборки, и из ситуативных уходят к нему в
    alternatives. Если в сборке никого из группы нет, ситуативные не трогаем.
    """
    for group in EXCLUSIVE_GROUPS:
        in_order = [e for e in order if e["key"] in group]
        if not in_order:
            continue
        winner = max(in_order, key=lambda e: e["share"])
        losers = [e for e in in_order if e is not winner] + [e for e in situational if e["key"] in group]
        losers.sort(key=lambda e: -e["share"])
        winner["alternatives"] = [
            {k: e[k] for k in ("key", "dname", "img", "share", "winrate", "minute_f")}
            for e in losers]
        order[:] = [e for e in order if e is winner or e["key"] not in group]
        situational[:] = [e for e in situational if e["key"] not in group]


def _build_from_rows(source, purchases, final, items, by_id):
    """Разбирает строки из базы в стартовый закуп, порядок, ситуативные, итог."""
    def info(name):
        it = items.get(name) or {}
        return {"key": name, "dname": it.get("dname") or name,
                "img": CDN + it["img"] if it.get("img") else None,
                "cost": it.get("cost") or 0, "qual": it.get("qual")}

    if not purchases:
        return None
    total = int(purchases[0]["total_games"]) or 1
    tw = float(purchases[0]["total_weight"] or total) or 1.0
    wins = int(purchases[0]["wins"] or 0)

    by_key = {}
    start, order, situational = [], [], []
    for r in purchases:
        e = info(r["key"])
        wg = float(r["wgames"] or 0)
        share = wg / tw
        e["share"] = round(share * 100)
        # винрейт игр, где предмет куплен: по нему видно, что реально тащит
        e["winrate"] = round(float(r.get("wwins") or 0) / wg * 100) if wg else None
        e["minute"] = (int(float(r["median_first"]) // 60)
                       if r.get("median_first") is not None else None)
        # ранние предметы различаются полуминутами: 2.0' и 5.5' - разные покупки
        e["minute_f"] = (round(float(r["median_first"]) / 60, 1)
                         if r.get("median_first") is not None else None)
        by_key[r["key"]] = e

        wstart = float(r["wstart"] or 0) / tw
        if wstart >= 0.4:
            s = dict(e)
            s["share"] = round(wstart * 100)
            s["count"] = round(float(r["start_per_game"] or 1))
            start.append(s)

        if e["minute"] is None:
            continue
        # в сборке - только собранные предметы: части (Ogre Axe, Circlet)
        # только шумят, они видны через то, во что собраны. Стартовый закуп
        # показан отдельно (покупки до рога сюда не попадают - minute
        # считается по покупкам после рога).
        if not is_build_item(e["key"], items.get(e["key"])):
            continue
        if share >= 0.25:
            order.append(e)
        elif share >= 0.10 and e["cost"] >= 1500:
            situational.append(e)

    start.sort(key=lambda x: -x["share"])
    order.sort(key=lambda x: x["minute"])
    situational.sort(key=lambda x: -x["share"])
    _collapse_alternatives(order, situational)

    final_items = []
    for r in final:
        name = by_id.get(int(r["item_id"]))
        if not name:
            continue
        e = info(name)
        # варды и недособранные части в инвентаре к концу игры - не сборка
        if not is_build_item(name, items.get(name)):
            continue
        wg = float(r["wgames"] or 0)
        e["share"] = round(wg / float(r["total_weight"] or 1) * 100)
        e["winrate"] = round(float(r.get("wwins") or 0) / wg * 100) if wg else None
        # минута покупки известна из закупов - по ней и сортируем итог
        e["minute"] = (by_key.get(name) or {}).get("minute")
        final_items.append(e)
    # итог - в порядке покупки, а не по частоте: собранные без рецепта
    # предметы в закупах не встречаются, у них минуты нет - они в конце
    final_items.sort(key=lambda x: (x["minute"] is None, x["minute"] or 0, -x["share"]))
    # в итоговом инвентаре те же альтернативы: два вида ботинок сразу не носят
    for e in final_items:
        e.setdefault("minute_f", None)
    _collapse_alternatives(final_items, [])

    return {
        "games": total, "wins": wins,
        "winrate": round(wins / total * 100, 1),
        "start": start, "order": order,
        "situational": situational[:8], "final": final_items[:10],
        "by_key": by_key,
    }


TALENT_TIERS = {1: 10, 2: 15, 3: 20, 4: 25}


def _talent_name(dname):
    """У уникальных талантов справочник хранит шаблон без числа:
    «+{s:bonus_heal_per_second} Living Armor Heal Per Second». Число
    OpenDota не отдаёт; показываем название без него и помечаем."""
    import re
    if "{s:" not in (dname or ""):
        return dname, False
    # вырезаем знак, шаблон и единицу, приклеенную к нему: «-{s:value}s»
    cleaned = re.sub(r"[+\-]?\{s:[^}]*\}[a-z%]*", "", dname)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" +-%")
    return cleaned, True


def hero_skills(source, hero_id, months=3, tier="top"):
    """Прокачка и таланты героя по турнирным матчам.

    Порядок - для каждой позиции апгрейда самая частая способность и доля
    игр, где взяли именно её. Таланты по уровням 10/15/20/25: доля среди
    игр, где на этом уровне взят какой-либо талант, и винрейт этих игр.
    """
    rows = source.hero_skills(hero_id, months, tier)
    ab = source.abilities()
    stats_by_id = {h["id"]: h for h in source.hero_stats()}
    hero_name = (stats_by_id.get(hero_id) or {}).get("name")
    hero_ab = ab["heroes"].get(hero_name) or {}
    talent_names = {t["name"]: t.get("level") for t in hero_ab.get("talents", [])}
    ids = ab["ids"]
    abilities = ab["abilities"]

    def info(ability_id):
        name = ids.get(str(ability_id))
        a = abilities.get(name) or {}
        return {"id": ability_id, "name": name,
                "dname": a.get("dname") or name or str(ability_id),
                "img": CDN + a["img"] if a.get("img") else None,
                "talent": name in talent_names}

    if not rows:
        return {"hero_id": hero_id, "games": 0}
    games = int(rows[0]["games"]) or 1

    by_lvl = {}
    for r in rows:
        by_lvl.setdefault(int(r["lvl"]), []).append(r)

    order = []
    for lvl in range(1, 19):
        cands = [r for r in by_lvl.get(lvl, []) if ids.get(str(r["ability_id"])) not in talent_names]
        if not cands:
            continue
        top = cands[0]
        e = info(int(top["ability_id"]))
        e["level"] = lvl
        e["share"] = round(int(top["n"]) / games * 100)
        order.append(e)

    talent_stats = {}
    for r in rows:
        name = ids.get(str(r["ability_id"]))
        if name in talent_names:
            t = talent_stats.setdefault(name, [0, 0])
            t[0] += int(r["n"])
            t[1] += int(r["wins"])

    # доля - от игр, где на этом уровне талант вообще взят: до 25-го уровня
    # доживает малая часть игр, и доля от всех игр показывала бы не выбор
    # между двумя талантами, а длину матчей
    tier_total = {}
    for name, level in talent_names.items():
        tier = TALENT_TIERS.get(level, level)
        tier_total[tier] = tier_total.get(tier, 0) + talent_stats.get(name, [0, 0])[0]

    talents = []
    for name, level in talent_names.items():
        a = abilities.get(name) or {}
        dname, approx = _talent_name(a.get("dname") or name)
        n, w = talent_stats.get(name, [0, 0])
        tier = TALENT_TIERS.get(level, level)
        total = tier_total.get(tier) or 1
        talents.append({
            "name": name, "dname": dname, "approx": approx,
            "tier": tier, "tier_games": tier_total.get(tier, 0),
            "picked": n, "share": round(n / total * 100, 1),
            "winrate": round(w / n * 100, 1) if n else None,
        })
    talents.sort(key=lambda t: t["tier"])

    return {"hero_id": hero_id, "games": games, "order": order, "talents": talents}


def hero_build(source, hero_id, months=3, enemy_ids=None, tier="top"):
    """Сборка героя по турнирным матчам: общая и против конкретного драфта.

    Стартовый закуп - предметы, купленные до рога хотя бы в 40% игр.
    Порядок сборки - собранные предметы (без частей и расходников) из ≥25%
    игр по медианной минуте первой покупки. Ситуативные - дорогие предметы
    из 10-25% игр. У каждого предмета - винрейт игр, где он куплен.

    Если переданы враги, считается вторая сборка - по играм против них -
    и сдвиги: какие предметы против этого драфта берут заметно чаще, реже,
    раньше или позже. Основной показ - условная сборка, если по ней хватает
    игр; иначе общая, а сдвиги остаются подсказкой.
    """
    items = source.items() or {}
    by_id = {it["id"]: name for name, it in items.items() if it.get("id")}

    general = _build_from_rows(source, *source.hero_builds(hero_id, months, tier), items, by_id)
    if not general:
        return {"hero_id": hero_id, "games": 0}

    enemy_ids = [int(e) for e in (enemy_ids or []) if int(e) != int(hero_id)]
    vs, shifts = None, []
    if enemy_ids:
        vs = _build_from_rows(source, *source.hero_builds(hero_id, months, tier, enemy_ids=enemy_ids),
                              items, by_id)
        if vs:
            for key, ev in vs["by_key"].items():
                eg = general["by_key"].get(key)
                if not eg or not is_build_item(key, items.get(key)):
                    continue
                if max(ev["share"], eg["share"]) < 20:
                    continue
                d_share = ev["share"] - eg["share"]
                d_min = ((ev["minute"] - eg["minute"])
                         if ev["minute"] is not None and eg["minute"] is not None else 0)
                if abs(d_share) >= 8 or abs(d_min) >= 3:
                    s = dict(ev)
                    s["general_share"] = eg["share"]
                    s["general_minute"] = eg["minute"]
                    s["d_share"] = d_share
                    s["d_minute"] = d_min
                    shifts.append(s)
            shifts.sort(key=lambda s: -(abs(s["d_share"]) + abs(s["d_minute"]) * 2))

    use_vs = bool(vs and vs["games"] >= MIN_VS_GAMES)
    shown = vs if use_vs else general
    out = {
        "hero_id": hero_id,
        "months": months,
        "tier": tier,
        "games": general["games"],
        "wins": general["wins"],
        "winrate": general["winrate"],
        "start": shown["start"],
        "order": shown["order"],
        "situational": shown["situational"],
        "final": shown["final"],
        "vs": {
            "enemy_ids": enemy_ids,
            "games": vs["games"] if vs else 0,
            "winrate": vs["winrate"] if vs else None,
            "used": use_vs,
            "shifts": shifts[:10],
        } if enemy_ids else None,
    }
    return out


def pro_match_detail(source, match_id):
    """Драфт матча + кто выигрывал по матчапам ещё до начала игры."""
    # лёгкий путь через базу; полный JSON матча - только если база не отдала
    slim_error = None
    try:
        m = source.match_slim(match_id)
    except Exception as e:  # noqa: BLE001
        slim_error = str(e)[:160]
        m = source.match(match_id)
    stats_by_id = {h["id"]: h for h in source.hero_stats()}

    items_by_name = source.items() or {}
    items_by_id = {it["id"]: name for name, it in items_by_name.items() if it.get("id")}

    def hero_info(hid):
        s = stats_by_id.get(hid, {})
        return {"id": hid, "name": s.get("localized_name"),
                "img": CDN + s["img"] if s.get("img") else None}

    radiant, dire = [], []
    for p in m.get("players") or []:
        entry = hero_info(p.get("hero_id"))
        entry.update({
            "player": p.get("name") or p.get("personaname"),
            "kda": [p.get("kills"), p.get("deaths"), p.get("assists")],
            "gpm": p.get("gold_per_min"),
            "xpm": p.get("xp_per_min"),
            "net_worth": p.get("net_worth"),
            "items": item_builds(p, items_by_name, items_by_id),
        })
        (radiant if p.get("isRadiant") else dire).append(entry)

    draft_order = []
    for pb in m.get("picks_bans") or []:
        draft_order.append({
            "is_pick": pb.get("is_pick"),
            "team": "radiant" if pb.get("team") == 0 else "dire",
            "order": pb.get("order"),
            **hero_info(pb.get("hero_id")),
        })
    draft_order.sort(key=lambda x: x["order"] if x["order"] is not None else 0)

    edge = {}
    if radiant and dire:
        edge = scoring.draft_edge(source, [h["id"] for h in radiant],
                                  [h["id"] for h in dire])

    return {
        "match_id": m.get("match_id"),
        "league": (m.get("league") or {}).get("name"),
        "radiant_name": (m.get("radiant_team") or {}).get("name") or "Radiant",
        "dire_name": (m.get("dire_team") or {}).get("name") or "Dire",
        "radiant_win": m.get("radiant_win"),
        "duration": m.get("duration"),
        "score": [m.get("radiant_score"), m.get("dire_score")],
        "radiant": radiant,
        "dire": dire,
        "draft_order": draft_order,
        "edge": edge,
        "has_draft": bool(draft_order),
        "slim_error": slim_error,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "DraftHelper/1.0"

    def log_message(self, fmt, *args):
        # Стандартный лог отключён: вместо него печатаем каждый запрос к API
        # с временем ответа, см. _timed. По такому логу видно, что именно
        # тормозит, без отдельной диагностики.
        pass

    def _timed(self, method, fn):
        import time
        path = self.path.split("?")[0]
        if not path.startswith("/api/"):
            return fn()
        started = time.time()
        sys.stderr.write(f"  {method} {path} …\n")
        sys.stderr.flush()
        try:
            return fn()
        finally:
            ms = (time.time() - started) * 1000
            sys.stderr.write(f"  {method} {path} — {ms:.0f} мс\n")
            sys.stderr.flush()

    # --- ответы --------------------------------------------------------
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        if not os.path.isfile(path):
            self._json({"error": "не найдено"}, 404)
            return
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as f:
            body = f.read()
        if ext == ".html":
            # версия в адресе скрипта и стилей: даже если браузер проигнорирует
            # заголовки кэширования, новый адрес он обязан скачать заново
            body = (body.replace(b'/static/app.js', f'/static/app.js?v={VERSION}'.encode())
                        .replace(b'/static/styles.css', f'/static/styles.css?v={VERSION}'.encode()))
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # без этого браузер после обновления кода подсовывает старый app.js
        # к новому серверу, и поведение становится необъяснимым
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    # --- маршруты ------------------------------------------------------
    def do_GET(self):
        return self._timed("GET", self._do_get)

    def do_POST(self):
        return self._timed("POST", self._do_post)

    def _do_get(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        src = get_source()
        try:
            if url.path in ("/", "/index.html"):
                return self._file(os.path.join(WEB_DIR, "index.html"))
            if url.path.startswith("/static/"):
                name = os.path.basename(url.path)
                return self._file(os.path.join(WEB_DIR, name))

            if url.path == "/api/heroes":
                heroes = hero_list(src)
                snapshot = getattr(src, "using_fallback", False)
                from sources import opendota as od
                return self._json({
                    "heroes": heroes,
                    "brackets": src.available_brackets(),
                    "positions": [{"key": k, "label": v}
                                  for k, v in POSITION_LABELS],
                    "source": src.name,
                    "stratz": get_stratz().status(),
                    "version": VERSION,
                    "snapshot": snapshot,
                    "snapshot_note": (
                        f"Справочник героев взят из снимка от {od.fallback_date}; "
                        "свежие данные подтягиваются в фоне и появятся при "
                        "следующем обновлении страницы."
                    ) if snapshot else None,
                })
            if url.path == "/api/meta":
                bracket, note = resolve_bracket(src, q.get("bracket", ["all"])[0])
                return self._json({
                    "rows": meta_table(
                        src, bracket,
                        (q.get("role") or [None])[0] or None,
                        ids_for_position((q.get("position") or [None])[0])),
                    "bracket_used": bracket,
                    "note": note,
                })
            if url.path == "/api/pro/matches":
                matches = pro_matches(src)
                # свежесть - после запроса: если OpenDota не ответила, здесь
                # будет причина, а список - последняя удачная копия
                return self._json({"matches": matches,
                                   "freshness": src.pro_matches_status()})
            if url.path == "/api/tournaments":
                months = int((q.get("months") or ["3"])[0])
                tier = (q.get("tier") or ["top"])[0]
                league = (q.get("league") or [None])[0] or None
                rows, total = tournament_table(src, months, tier, league)
                return self._json({"rows": rows, "total_matches": total,
                                   "months": months, "tier": tier,
                                   "league": league})
            if url.path == "/api/gsi/state":
                s = gsi.state()
                # имя героя из игры -> id из справочника
                if s.get("hero_name") and not s.get("hero_id"):
                    for h in src.hero_stats():
                        if h.get("name") == s["hero_name"]:
                            s["hero_id"] = h["id"]
                            break
                if s.get("hero_id"):
                    for h in src.hero_stats():
                        if h["id"] == s["hero_id"]:
                            s["hero_localized"] = h.get("localized_name")
                            break
                s["config_path_hint"] = (
                    "<Steam>\\steamapps\\common\\dota 2 beta\\game\\dota\\cfg\\"
                    "gamestate_integration\\gamestate_integration_drafthelper.cfg")
                return self._json(s)
            if url.path == "/api/gsi/check":
                return self._json(gsi.check(self.server.server_address[1]))
            if url.path == "/api/gsi/config":
                body = gsi.config_text(self.server.server_address[1]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition",
                                 'attachment; filename="gamestate_integration_drafthelper.cfg"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            if url.path == "/api/player":
                acc, err = parse_account((q.get("id") or [""])[0])
                if err:
                    return self._json({"error": err}, 400)
                return self._json(player_summary(src, acc))
            if url.path == "/api/skills":
                hero = (q.get("hero") or [None])[0]
                if not hero:
                    return self._json({"error": "не передан герой"}, 400)
                months = min(int((q.get("months") or ["3"])[0]), 3)
                tier = (q.get("tier") or ["top"])[0]
                return self._json(hero_skills(src, int(hero), months, tier))
            if url.path == "/api/build":
                hero = (q.get("hero") or [None])[0]
                if not hero:
                    return self._json({"error": "не передан герой"}, 400)
                # база не тянет период больше трёх месяцев - упирается в таймаут
                months = min(int((q.get("months") or ["3"])[0]), 3)
                enemies = [int(x) for x in (q.get("enemy") or [""])[0].split(",") if x.strip()]
                tier = (q.get("tier") or ["top"])[0]
                return self._json(hero_build(src, int(hero), months, enemies, tier))
            if url.path == "/api/tournaments/leagues":
                months = int((q.get("months") or ["3"])[0])
                return self._json({"leagues": src.tournament_leagues(months)})
            if url.path == "/api/pro/match":
                mid = (q.get("id") or [None])[0]
                if not mid:
                    return self._json({"error": "не передан id матча"}, 400)
                return self._json(pro_match_detail(src, int(mid)))
            if url.path == "/api/cache/clear":
                return self._json({"removed": net.clear_cache()})
            if url.path == "/api/update":
                return self._json(update_status())

            if url.path == "/api/vision/status":
                return self._json(vision_status())
            if url.path == "/api/vision/selftest":
                if not vision.AVAILABLE:
                    return self._json({"error": vision.requirements_hint()}, 400)
                return self._json(vision_self_test())
            if url.path == "/api/vision/state":
                if not vision.AVAILABLE:
                    return self._json({"error": vision.requirements_hint()}, 400)
                return self._json(get_watcher().state())
            if url.path == "/api/vision/frame.jpg":
                # последний захваченный кадр: главный инструмент диагностики,
                # когда «не распознаёт» — видно, что реально попало в кадр
                if not vision.AVAILABLE:
                    return self._json({"error": vision.requirements_hint()}, 400)
                data = get_watcher().last_frame_jpeg()
                if not data:
                    return self._json({"error": "кадров ещё не было"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return None

            return self._json({"error": "неизвестный маршрут"}, 404)
        except Exception as e:  # noqa: BLE001 — сервер не должен падать от сбоя API
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def _do_post(self):
        url = urlparse(self.path)
        src = get_source()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)

            # сюда стучится сама игра через Game State Integration
            if url.path == "/gsi":
                try:
                    gsi.handle(json.loads(raw or b"{}"))
                except ValueError:
                    pass
                # первые сообщения - в консоль: так видно, что игра вообще пишет
                n = gsi.state()["received"]
                if n <= 3 or n % 100 == 0:
                    sys.stderr.write(f"  GSI: сообщение #{n} от игры, {len(raw)} байт\n")
                    sys.stderr.flush()
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None

            # изображение приходит сырыми байтами, а не JSON
            if url.path == "/api/vision/recognize":
                if not vision.AVAILABLE:
                    return self._json({"error": vision.requirements_hint()}, 400)
                if not raw:
                    return self._json({"error": "файл пустой"}, 400)
                return self._json(get_watcher().scan_image(raw))

            data = json.loads(raw or b"{}")

            if url.path == "/api/gsi/install":
                path, msg = gsi.install(self.server.server_address[1])
                return self._json({"path": path, "message": msg})

            if url.path == "/api/recommend":
                enemy = data.get("enemy") or []
                if not enemy:
                    return self._json({"error": "не выбран ни один герой противника"}, 400)

                # источник матчапов: OpenDota (по умолчанию) или снимок STRATZ.
                # Со STRATZ ранг, базовые винрейты, фильтр по позиции и
                # синергия берутся из его снимка; всё остальное - как обычно.
                matchup_source = (data.get("matchup_source") or "opendota").lower()
                position = data.get("position")

                # «Авто»: позицию выводим из союзников и истории аккаунта.
                # История - из кэша без ожидания сети; нет её - только союзники
                # «Авто»: свободные позиции - по союзникам с экрана, и только
                # по ним. Если свободна одна - она и есть позиция; если
                # несколько - показываем героев всех свободных, не гадая
                position_auto = None
                auto_positions = None
                if position == "auto":
                    # 1) подписи ролей с экрана + номер своего слота от игры;
                    # 2) подписи с экрана: роли слотов без портретов свободны;
                    # 3) по союзникам (справочник позиций) - запасной путь
                    free, taken, reason = None, [], None
                    screen = screen_roles()
                    if screen:
                        slots, taken_slots = screen["slots"], screen["taken"]
                        my_slot = gsi.state().get("team_slot")
                        if isinstance(my_slot, int) and 0 <= my_slot < 5 and slots[my_slot]:
                            free = [slots[my_slot]]
                            reason = (f"по подписи под вашим слотом на экране: "
                                      f"«{POSITION_NAMES.get(slots[my_slot])}»")
                        else:
                            free = sorted({p for i, p in enumerate(slots)
                                           if p and i not in taken_slots})
                            if free:
                                reason = ("по подписям ролей на экране: свободны слоты без героев"
                                          + ("" if my_slot is None else
                                             ", подпись под вашим слотом не прочиталась"))
                    if not free:
                        free, taken = free_positions([int(x) for x in (data.get("ally") or [])])
                        reason = (("союзники заняли " + ", ".join(map(str, taken))) if taken
                                  else "союзники ещё не взяты - позиции все")
                    auto_positions = free if len(free) < 5 else None
                    position = str(free[0]) if len(free) == 1 else ""
                    position_auto = {"positions": auto_positions, "taken": taken,
                                     "reason": reason, "screen": bool(screen)}
                stratz_bound, base_stats, allowed_ids = None, None, ids_for_position(position)
                if auto_positions and not position:
                    allowed_ids = set().union(*(ids_for_position(p) for p in auto_positions))
                if matchup_source == "stratz":
                    stz = get_stratz()
                    stz.maybe_refresh()
                    bracket = data.get("bracket") or "all"
                    stratz_bound = stz.bound(bracket)
                    if stratz_bound is None and bracket != "all":
                        stratz_bound = stz.bound("all")
                        note = f"в снимке STRATZ нет ранга «{bracket}», показаны все ранги"
                        bracket = "all"
                    else:
                        note = None
                    if stratz_bound is None:
                        matchup_source = "opendota"
                        note = "снимок STRATZ недоступен, матчапы взяты из OpenDota"
                    else:
                        base_stats = stz.base_stats(bracket, position)
                        if auto_positions and not position:
                            # несколько свободных позиций: объединение по данным STRATZ
                            allowed_ids = set().union(
                                *(stz.position_ids(bracket, p) for p in auto_positions))
                        else:
                            allowed_ids = stz.position_ids(bracket, position)
                if matchup_source != "stratz":
                    bracket, note = resolve_bracket(src, data.get("bracket") or "all")

                # турнирная составляющая: вес выбирает пользователь,
                # 0 - не учитывать. Если база турниров недоступна, подбор
                # работает без неё и говорит об этом, а не падает.
                # Во время пика ждать сеть нельзя: всё, что не лежит в кэше
                # или снимке, считается в фоне, а подбор отдаётся сразу без
                # этой составляющей с флагом pending - страница дозапросит.
                tour_weight = float(data.get("tour_weight", scoring.W_TOUR))
                tour, tour_note, tour_matches, tour_pending = None, None, 0, False
                if tour_weight > 0:
                    try:
                        t_rows, tour_matches, tour_pending = tournament_fast(
                            src, int(data.get("tour_months") or 3), "top")
                        if t_rows is not None:
                            tour = scoring.tournament_strength(t_rows, tour_matches)
                        else:
                            tour_note = ("турнирная статистика собирается в фоне, "
                                         "таблица обновится сама")
                    except Exception as e:  # noqa: BLE001
                        tour_note = f"турнирная статистика недоступна: {str(e)[:120]}"
                        tour_weight = 0.0

                # личная составляющая: по аккаунту игрока, если он указан
                personal, personal_info, personal_note = None, None, None
                personal_pending = False
                personal_weight = float(data.get("personal_weight", scoring.W_PERSONAL))
                account = data.get("account")
                if account and personal_weight > 0:
                    acc, err = parse_account(str(account))
                    if err:
                        personal_note, personal_weight = err, 0.0
                    else:
                        try:
                            ph = src.player_heroes(acc, fast=True)
                            wl = src.player_wl(acc, fast=True)
                            if ph is None or wl is None:
                                # ещё не скачано - подбор без личной части
                                personal_pending, personal_weight = True, 0.0
                                personal_note = ("личная статистика подгружается, "
                                                 "таблица обновится сама")
                                total_games = -1
                            else:
                                total_games = int(wl.get("win") or 0) + int(wl.get("lose") or 0)
                            # пустой аккаунт отдаёт 127 строк с нулями, а не пустой
                            # список - проверять надо по общему числу игр
                            if total_games == -1:
                                pass
                            elif not total_games:
                                personal_note = (f"по аккаунту {acc} нет ни одной игры: "
                                                 "история матчей в Steam закрыта "
                                                 "(«Открытая история матчей») или ID не тот")
                                personal_weight = 0.0
                            else:
                                personal, personal_info = scoring.personal_strength(ph, wl)
                        except Exception as e:  # noqa: BLE001
                            personal_note = f"данные аккаунта недоступны: {str(e)[:120]}"
                            personal_weight = 0.0
                else:
                    personal_weight = 0.0

                rows, k_shrink, k_synergy = scoring.recommend(
                    src,
                    enemy_ids=enemy,
                    ally_ids=data.get("ally") or [],
                    banned_ids=data.get("banned") or [],
                    bracket=bracket,
                    role=data.get("role") or None,
                    limit=int(data.get("limit") or 15),
                    allowed_ids=allowed_ids,
                    tour=tour,
                    tour_weight=tour_weight,
                    personal=personal,
                    personal_info=personal_info,
                    personal_weight=personal_weight,
                    matchups=stratz_bound,
                    synergies=stratz_bound,
                    base_stats=base_stats,
                )
                return self._json({
                    "rows": rows,
                    "source": src.name,
                    "matchup_source": matchup_source,
                    "bracket_used": bracket,
                    "note": note,
                    "k_shrink": k_shrink,
                    "k_synergy": k_synergy,
                    "tour_weight": tour_weight,
                    "tour_matches": tour_matches,
                    "tour_note": tour_note,
                    "personal_weight": personal_weight,
                    "personal_note": personal_note,
                    # что-то ещё считается в фоне - страница повторит запрос
                    "pending": bool(tour_pending or personal_pending),
                    "position_used": position or None,
                    "position_auto": position_auto,
                })

            if url.path.startswith("/api/vision/"):
                if not vision.AVAILABLE:
                    return self._json({"error": vision.requirements_hint()}, 400)
                action = url.path.rsplit("/", 1)[-1]

                if action == "icons":
                    from vision import icons
                    got, failed, total, reason = icons.ensure_icons(src)
                    # без перечитывания распознаватель остался бы пустым
                    # до перезапуска сервера
                    ready = get_watcher().reload_templates()
                    return self._json({"downloaded": got, "failed": failed,
                                       "total": total, "ready": ready,
                                       "reason": reason,
                                       "have": len(icons.available_ids())})

                w = get_watcher()
                if action == "config":
                    w.configure(monitor=data.get("monitor"),
                                interval=data.get("interval"),
                                region=data.get("region"))
                    return self._json({"ok": True, "monitor": w.monitor,
                                       "interval": w.interval,
                                       "region": list(w.region)})
                if action == "start":
                    started = w.start()
                    return self._json({"started": started, "state": w.state()})
                if action == "stop":
                    w.stop()
                    return self._json({"state": w.state()})
                if action == "scan":
                    return self._json(w.scan_once())

            return self._json({"error": "неизвестный маршрут"}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


# --- проверка обновлений -----------------------------------------------------
# CI кладёт в релиз latest файл version.txt с VERSION собранного exe.
# Проверка идёт в фоне при старте; страница спрашивает /api/update.
RELEASE_PAGE = "https://github.com/Dtkek/pickline/releases/tag/latest"
VERSION_URL = "https://github.com/Dtkek/pickline/releases/download/latest/version.txt"
_update = {"checked": False, "latest": None, "available": False, "error": None}


def _version_key(v):
    """«2026-09-16.2» -> (2026, 9, 16, 2) для сравнения."""
    import re
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def check_update_in_background():
    def worker():
        try:
            latest = net.get_text(VERSION_URL).strip()
            _update.update(latest=latest, error=None,
                           available=_version_key(latest) > _version_key(VERSION))
        except Exception as e:  # noqa: BLE001 - без сети просто не узнаем
            _update.update(error=str(e)[:160])
        finally:
            _update["checked"] = True

    threading.Thread(target=worker, daemon=True).start()


def update_status():
    return dict(_update, current=VERSION, url=RELEASE_PAGE)


def has_console():
    """Есть ли у процесса консоль. У exe, собранного без консоли, - нет."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001
        return True


LOG_PATH = None


def redirect_output_to_log():
    """Exe без консоли: весь вывод - в файл рядом с exe.

    Консоль убрана, потому что пользователю она мешает, но без вывода
    приложение нельзя чинить: сегодняшнее «ничего не происходит» разобрали
    только по тексту из консоли. Файл перезаписывается на каждом запуске -
    в нём всегда текущая сессия.
    """
    global LOG_PATH
    if not getattr(sys, "frozen", False) or has_console():
        return None
    import paths
    path = paths.LOG_PATH
    try:
        f = open(path, "w", encoding="utf-8", buffering=1)
    except OSError:
        return None
    sys.stdout = f
    sys.stderr = f
    LOG_PATH = path
    return path


def message_box(text, title="Pickline", error=False):
    """Окно сообщения Windows. Блокирует до нажатия ОК; вне Windows - print."""
    if sys.platform == "win32":
        try:
            import ctypes
            MB_ICONERROR, MB_ICONINFORMATION, MB_TOPMOST = 0x10, 0x40, 0x40000
            flags = (MB_ICONERROR if error else MB_ICONINFORMATION) | MB_TOPMOST
            ctypes.windll.user32.MessageBoxW(None, text, title, flags)
            return
        except Exception:  # noqa: BLE001
            pass
    print(text)


def lower_priority():
    """Уступаем процессор игре: приложению спешить некуда, а игре - есть.

    На Windows - класс приоритета BELOW_NORMAL, на остальных - nice.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x4000
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(5)
        return True
    except Exception:  # noqa: BLE001 — не критично
        return False


def main():
    ap = argparse.ArgumentParser(description="Pickline — подсказки по драфту Dota 2")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true",
                    help="только сервер: ни окна, ни браузера")
    ap.add_argument("--browser", action="store_true",
                    help="открыть вкладку в браузере вместо собственного окна")
    ap.add_argument("--on-top", action="store_true",
                    help="собственное окно сразу поверх остальных")
    args = ap.parse_args()

    # Прогреваем справочник до старта сервера: так в консоли сразу видно,
    # есть ли доступ к OpenDota. Иначе пользователь видит пустой интерфейс
    # и не понимает, грузится он или сломался.
    # flush обязателен: вывод в stdout буферизуется, и сообщения о ходе
    # запуска пользователь увидел бы только под конец, а нужны они сразу
    def say(msg):
        print(msg, flush=True)

    src = get_source()
    say(f"Pickline, версия {VERSION}. Источник данных: {src.name}.")
    say("  приоритет процесса понижен: игра важнее"
        if lower_priority() else "  приоритет процесса понизить не удалось")
    try:
        heroes = src.hero_stats()
        if getattr(src, "using_fallback", False):
            from sources import opendota as od
            say(f"  справочник героев: снимок от {od.fallback_date}, "
                f"{len(heroes)} героев. Свежие данные подтягиваю в фоне.")
        else:
            say(f"  справочник героев: {len(heroes)} героев из кэша или сети.")
    except Exception as e:  # noqa: BLE001 — сервер поднимаем в любом случае
        say(f"  справочник героев НЕ ЗАГРУЖЕН: {e}")
        say("  Запустите diagnose.bat, чтобы понять причину.")

    # Самопроверка распознавания при старте: находит ли распознаватель
    # собственные эталоны. Если нет — проблема не в игре и не в захвате.
    if vision.AVAILABLE:
        try:
            from vision import icons
            have = len(icons.available_ids())
            if have:
                res = vision_self_test()
                loaded = len(get_watcher().recognizer.ids)
                if res.get("ok"):
                    say(f"  распознавание: самопроверка ок "
                        f"({res['found']}/{res['placed']} за {res['seconds']} с, "
                        f"эталонов загружено {loaded} из {have} файлов)")
                else:
                    say(f"  распознавание: САМОПРОВЕРКА НЕ ПРОШЛА — "
                        f"найдено {res.get('found', 0)} из {res.get('placed', 0)}, "
                        f"эталонов загружено {loaded} из {have} файлов. "
                        f"{res.get('reason', '')}")
                    say(f"  папка эталонов: {icons.ASSETS}")
            else:
                say("  распознавание: портреты героев ещё не скачаны — "
                    "скачаются при первом запуске слежения")
        except Exception as e:  # noqa: BLE001 — самопроверка не должна ронять старт
            say(f"  распознавание: самопроверка упала: {type(e).__name__}: {e}")
    else:
        say(f"  распознавание отключено: {vision.requirements_hint()}")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    say(f"\nPickline запущен: {url}")
    check_update_in_background()

    # Собственное окно, если есть pywebview и не просили браузер. Сервер
    # уходит в поток, окно занимает главный поток (так требует macOS);
    # закрыли окно - остановили сервер.
    use_window = window.AVAILABLE and not args.no_browser and not args.browser
    if use_window:
        say("Закройте окно, чтобы остановить.")
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        try:
            window.run(url, on_top=args.on_top, on_closed=httpd.shutdown, log=say)
            print("\nОстановлено.")
            return
        except KeyboardInterrupt:
            print("\nОстановлено.")
            return
        except Exception as e:  # noqa: BLE001 - окно не критично, есть браузер
            # на Windows окно может не открыться без WebView2 или .NET;
            # приложение от этого не должно умирать
            say(f"  окно не открылось: {type(e).__name__}: {str(e)[:200]}")
            if "Failed to resolve" in str(e) or "0x80131515" in str(e):
                say("  .NET не принял Python.Runtime.dll. Чаще всего это метка "
                    "«скачано из интернета» на файлах: правый клик по "
                    "скачанному архиву → Свойства → «Разблокировать», "
                    "распаковать заново и запустить.")
            elif not sys.executable.isascii():
                say("  Похоже, дело в кириллице в пути к приложению: перенесите "
                    "папку, например, в C:\\pickline")
            webbrowser.open(url)
            wait_in_browser_mode(httpd, server_thread, url, say,
                                 reason=f"Окно не открылось: {type(e).__name__}: {str(e)[:160]}")
            return

    if not window.AVAILABLE and not args.no_browser and not args.browser:
        say(f"  ({window.requirements_hint()} - открываю в браузере)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    if has_console() or args.no_browser:
        say("Ctrl+C — остановить.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nОстановлено.")
        return
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    wait_in_browser_mode(httpd, server_thread, url, say)


def wait_in_browser_mode(httpd, server_thread, url, say, reason=None):
    """Ждать, пока пользователь не остановит приложение, работающее в браузере.

    С консолью - Ctrl+C. Без консоли (exe собран без неё) остановить
    процесс было бы нечем, кроме диспетчера задач, поэтому показываем окно
    сообщения: приложение работает по такому-то адресу, ОК - остановить.
    """
    if has_console():
        say("  открываю в браузере. Ctrl+C — остановить.")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            print("\nОстановлено.")
        return
    say("  открываю в браузере; остановка - кнопкой ОК в окне сообщения.")
    text = ((reason + "\n\n") if reason else "") + (
        f"Приложение работает в браузере: {url}\n\n"
        "Нажмите ОК, чтобы остановить приложение."
        + (f"\n\nПодробности в файле {LOG_PATH}" if LOG_PATH else ""))
    message_box(text)
    httpd.shutdown()
    print("\nОстановлено.")


if __name__ == "__main__":
    redirect_output_to_log()
    try:
        main()
    except Exception:  # noqa: BLE001
        # Падение на старте не должно выглядеть как «ничего не произошло»:
        # с консолью - текст и ожидание Enter, без консоли - окно сообщения
        # с концом трассировки и путём к логу.
        trace = traceback.format_exc()
        print(trace)
        if getattr(sys, "frozen", False):
            if has_console():
                try:
                    input("\nПриложение упало. Скопируйте текст выше и нажмите Enter…")
                except EOFError:
                    pass
            else:
                message_box("Приложение упало.\n\n" + trace[-1500:]
                            + (f"\n\nПолный текст: {LOG_PATH}" if LOG_PATH else ""),
                            error=True)
        sys.exit(1)
