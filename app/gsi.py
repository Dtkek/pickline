# -*- coding: utf-8 -*-
"""Game State Integration: игра сама сообщает, кто вы и за кого играете.

Это официальный механизм Valve: клиент Dota 2 шлёт JSON о состоянии игры
на указанный адрес. Приложение принимает его на /gsi и достаёт оттуда
героя игрока и его команду. Никакого чтения памяти и экрана - игра
рассказывает сама.

Чтобы заработало, в папку игры нужно положить конфиг:
  <Steam>/steamapps/common/dota 2 beta/game/dota/cfg/gamestate_integration/
  файл gamestate_integration_drafthelper.cfg (текст ниже, см. config_text()).
После этого перезапустить Dota.

Что приходит в JSON (нужные части):
  provider.timestamp        - когда отправлено
  map.game_state            - DOTA_GAMERULES_STATE_HERO_SELECTION, ..._STRATEGY_TIME,
                              ..._PRE_GAME, ..._GAME_IN_PROGRESS
  player.team_name          - "radiant" или "dire"
  player.name, player.steamid
  hero.name                 - "npc_dota_hero_nevermore" (появляется после выбора)
  hero.id
  draft.activeteam, draft.activeteam_time_remaining, draft.radiant_bonus_time,
  draft.dire_bonus_time      - чья очередь в драфте и сколько секунд осталось
"""
import os
import threading
import time

_lock = threading.Lock()
_state = {
    "received": 0,
    "last_at": None,
    "game_state": None,
    "team": None,
    "hero_name": None,
    "hero_id": None,
    "player_name": None,
    "steamid": None,
    # номер слота в команде, если игра его сообщает (player.team_slot /
    # player.player_slot): по нему можно понять, какой слот в верхней
    # полосе драфта ваш. Вживую в драфте не проверено - см. лог
    "team_slot": None,
    "player_slot": None,
    # блок draft: чья очередь и сколько секунд осталось на выбор. Valve
    # шлёт его точно наблюдателям; присылает ли игроку в его драфте - вживую
    # не проверено, первое сообщение пишется в лог
    "draft_active_team": None,       # 2 - Radiant, 3 - Dire
    "draft_time_remaining": None,
    "draft_bonus_time": None,        # резерв той команды, чья очередь
    "draft_at": None,
}
_draft_block_logged = False

# --- живой матч для голосового тренера -------------------------------------
# Сводка последнего сообщения игры и события (смерть, убийство, уровень, мало
# здоровья, скоро руны). События - очередь с номерами: страница тренера
# забирает новые по /api/coach/events?since=N.
_live = {}
_events = []          # [{"id", "at", "text", "important"}]
_event_seq = 0
_prev = {}            # прошлые значения для сравнения: deaths, kills, level, low_hp, clock
EVENTS_KEEP = 200


def _event(text, important=False):
    global _event_seq
    _event_seq += 1
    _events.append({"id": _event_seq, "at": time.time(), "text": text, "important": important})
    del _events[:-EVENTS_KEEP]


def _fmt_clock(sec):
    sec = int(sec or 0)
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    return f"{sign}{sec // 60}:{sec % 60:02d}"


def _update_live(payload):
    """Сводка матча + события по разнице с прошлым сообщением (под _lock)."""
    m = payload.get("map") or {}
    p = payload.get("player") or {}
    h = payload.get("hero") or {}
    items = payload.get("items") or {}
    abil = payload.get("abilities") or {}
    if m.get("game_state") != "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS" and not h.get("level"):
        return
    inv = [(items.get(f"slot{i}") or {}).get("name") for i in range(6)]
    inv = [x.replace("item_", "") for x in inv if x and x != "empty"]
    skills = []
    for k in sorted(abil):
        a = abil[k] or {}
        if a.get("name") and not a["name"].startswith("special_bonus"):
            skills.append({"name": a["name"], "level": a.get("level"), "ultimate": a.get("ultimate"),
                           "cooldown": a.get("cooldown"), "can_cast": a.get("can_cast")})
    live = {
        "clock": m.get("clock_time"), "clock_text": _fmt_clock(m.get("clock_time")),
        "daytime": m.get("daytime"), "game_state": m.get("game_state"),
        "kills": p.get("kills"), "deaths": p.get("deaths"), "assists": p.get("assists"),
        "last_hits": p.get("last_hits"), "denies": p.get("denies"),
        "gold": p.get("gold"), "gpm": p.get("gpm"), "xpm": p.get("xpm"),
        "hero": h.get("name"), "level": h.get("level"), "alive": h.get("alive"),
        "health": h.get("health"), "max_health": h.get("max_health"),
        "mana": h.get("mana"), "max_mana": h.get("max_mana"),
        "respawn_seconds": h.get("respawn_seconds"), "buyback_cost": h.get("buyback_cost"),
        "items": inv, "abilities": skills,
    }
    _live.clear()
    _live.update(live)

    # события - только по разнице с прошлым сообщением
    if _prev.get("deaths") is not None and (live["deaths"] or 0) > _prev["deaths"]:
        _event(f"Игрок умер (смертей: {live['deaths']}). Воскрешение через "
               f"{live['respawn_seconds'] or '?'} с.", important=True)
    if _prev.get("kills") is not None and (live["kills"] or 0) > _prev["kills"]:
        _event(f"Игрок сделал убийство (всего {live['kills']}).", important=True)
    if _prev.get("level") is not None and (live["level"] or 0) > _prev["level"]:
        lvl = live["level"]
        _event(f"Новый уровень: {lvl}." + (" Открылась ульта." if lvl == 6 else ""), important=lvl in (6, 12, 18))
    hp = (live["health"] or 0) / max(1, live["max_health"] or 1)
    if live["alive"] and hp < 0.25 and not _prev.get("low_hp"):
        _event(f"Мало здоровья: {live['health']} из {live['max_health']}.", important=True)
        _prev["low_hp"] = True
    elif hp > 0.5:
        _prev["low_hp"] = False
    # руны (за 15 с): силовые - с 6:00 каждые 2 мин, мудрости - каждые 7 мин
    clk = live["clock"]
    last = _prev.get("clock")
    if isinstance(clk, int) and isinstance(last, int) and clk > last:
        for t in range(last + 1, clk + 1):
            nxt = t + 15
            if nxt >= 360 and nxt % 120 == 0:
                _event(f"Через 15 секунд силовые руны ({_fmt_clock(nxt)}).")
            if nxt >= 420 and nxt % 420 == 0:
                _event(f"Через 15 секунд руны мудрости ({_fmt_clock(nxt)}).", important=True)
    _prev.update(deaths=live["deaths"], kills=live["kills"], level=live["level"], clock=clk)


def live_state():
    with _lock:
        return dict(_live)


def events_since(since):
    with _lock:
        return [e for e in _events if e["id"] > since], _event_seq

# что игра присылает в блоке player на стадии выбора героя: пишется в лог
# один раз за запуск, чтобы узнать, есть ли там номер слота
_draft_player_logged = False


def config_text(port, token="drafthelper"):
    """Текст конфига для папки gamestate_integration."""
    return f'''"Draft Helper GSI"
{{
    "uri"           "http://127.0.0.1:{port}/gsi"
    "timeout"       "5.0"
    "buffer"        "0.1"
    "throttle"      "0.1"
    "heartbeat"     "10.0"
    "data"
    {{
        "provider"      "1"
        "map"           "1"
        "player"        "1"
        "hero"          "1"
        "draft"         "1"
        "abilities"     "1"
        "items"         "1"
    }}
    "auth"
    {{
        "token"         "{token}"
    }}
}}
'''


def handle(payload):
    """Обновляет состояние по присланному игрой JSON."""
    if not isinstance(payload, dict):
        return
    global _draft_player_logged, _draft_block_logged
    draft = payload.get("draft") or {}
    hero = payload.get("hero") or {}
    player = payload.get("player") or {}
    game_map = payload.get("map") or {}
    with _lock:
        _state["received"] += 1
        _state["last_at"] = time.time()
        if game_map.get("game_state"):
            _state["game_state"] = game_map["game_state"]
        if player.get("team_name"):
            _state["team"] = player["team_name"]
        if player.get("name"):
            _state["player_name"] = player["name"]
        if player.get("steamid"):
            _state["steamid"] = str(player["steamid"])
        for key in ("team_slot", "player_slot"):
            if player.get(key) is not None:
                _state[key] = player[key]
        # первое сообщение стадии выбора героя - целиком блок player в лог:
        # по нему видно, сообщает ли игра номер слота
        if (game_map.get("game_state") == "DOTA_GAMERULES_STATE_HERO_SELECTION"
                and player and not _draft_player_logged):
            _draft_player_logged = True
            import json
            import sys
            sys.stderr.write("  GSI: блок player на стадии выбора героя: "
                             + json.dumps(player, ensure_ascii=False)[:600] + "\n")
        _update_live(payload)
        # таймер выбора из блока draft (если игра его присылает)
        if isinstance(draft, dict) and draft:
            if not _draft_block_logged:
                _draft_block_logged = True
                import json
                import sys
                sys.stderr.write("  GSI: блок draft: "
                                 + json.dumps(draft, ensure_ascii=False)[:600] + "\n")
            active = draft.get("activeteam")
            if active is not None:
                _state["draft_active_team"] = active
                _state["draft_time_remaining"] = draft.get("activeteam_time_remaining")
                bonus_key = "radiant_bonus_time" if active == 2 else "dire_bonus_time"
                _state["draft_bonus_time"] = draft.get(bonus_key)
                _state["draft_at"] = time.time()
        elif game_map.get("game_state") != "DOTA_GAMERULES_STATE_HERO_SELECTION":
            _state["draft_time_remaining"] = None
        # герой появляется, когда выбран; между играми блок пустой
        if hero.get("name"):
            _state["hero_name"] = hero["name"]
            _state["hero_id"] = hero.get("id")
        elif game_map.get("game_state") in (None, "DOTA_GAMERULES_STATE_INIT",
                                            "DOTA_GAMERULES_STATE_DISCONNECT"):
            _state["hero_name"] = None
            _state["hero_id"] = None


def state():
    with _lock:
        s = dict(_state)
    s["alive"] = bool(s["last_at"]) and (time.time() - s["last_at"] < 30)
    return s


# --- установка конфига в папку игры ------------------------------------------
CFG_NAME = "gamestate_integration_drafthelper.cfg"
DOTA_CFG = os.path.join("steamapps", "common", "dota 2 beta", "game", "dota", "cfg")


def _steam_roots():
    """Где может лежать Steam. Плюс библиотеки из libraryfolders.vdf."""
    home = os.path.expanduser("~")
    roots = [
        r"C:\Program Files (x86)\Steam",
        r"C:\Program Files\Steam",
        r"D:\Steam", r"D:\SteamLibrary", r"E:\Steam", r"E:\SteamLibrary",
        os.path.join(home, "Library", "Application Support", "Steam"),
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".local", "share", "Steam"),
    ]
    extra = []
    for r in roots:
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        if not os.path.isfile(vdf):
            continue
        try:
            with open(vdf, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('"path"'):
                        p = line.split('"')[3].replace("\\\\", "\\")
                        extra.append(p)
        except OSError:
            pass
    seen, out = set(), []
    for r in roots + extra:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def find_dota_cfg():
    for root in _steam_roots():
        p = os.path.join(root, DOTA_CFG)
        if os.path.isdir(p):
            return p
    return None


def check(port):
    """Что с подключением: где папка, лежит ли конфиг, тот ли в нём порт,
    приходили ли сообщения. По этому списку видно, на каком шаге затык."""
    cfg_dir = find_dota_cfg()
    result = {
        "dota_cfg_dir": cfg_dir,
        "config_path": None,
        "config_present": False,
        "config_port_ok": False,
        "steam_roots_checked": _steam_roots(),
    }
    if cfg_dir:
        path = os.path.join(cfg_dir, "gamestate_integration", CFG_NAME)
        result["config_path"] = path
        if os.path.isfile(path):
            result["config_present"] = True
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                result["config_port_ok"] = f":{port}/gsi" in text
                # голосовому тренеру нужны способности и предметы: конфиг из
                # старой версии их не просит - его надо переустановить
                result["config_fields_ok"] = '"abilities"' in text and '"items"' in text
            except OSError:
                pass
    result.update(state())
    return result


def install(port):
    """Кладёт конфиг в папку игры. Возвращает (путь или None, сообщение)."""
    cfg_dir = find_dota_cfg()
    if not cfg_dir:
        return None, ("папка Dota 2 не найдена в обычных местах Steam. "
                      "Скачайте конфиг кнопкой и положите вручную в "
                      "dota 2 beta\\game\\dota\\cfg\\gamestate_integration\\")
    target_dir = os.path.join(cfg_dir, "gamestate_integration")
    try:
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, CFG_NAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write(config_text(port))
    except OSError as e:
        return None, f"не удалось записать конфиг: {e}"
    return path, "конфиг установлен; перезапустите Dota, чтобы игра его подхватила"
