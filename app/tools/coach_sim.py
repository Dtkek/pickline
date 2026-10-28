# -*- coding: utf-8 -*-
"""Проверка голосового тренера без игры: «проигрывает» серверу Pickline
настоящий матч из OpenDota так, как его прислала бы Dota через GSI.

    python3 app/tools/coach_sim.py [--match ID] [--slot 0] [--port 8777] [--speed 60]

Берёт разобранный матч (если --match не задан - свежий про-матч с логами),
одного игрока (--slot, 0-9) и каждую игровую секунду (ускоренно в --speed
раз) шлёт POST /gsi: время, KDA, золото, добивания, уровень, здоровье,
предметы. Смерти и убийства - из kills_log, уровень - из xp_t, здоровье -
падает перед смертью. В конце печатает, какие события увидел тренер
(/api/coach/events) и последнюю сводку (/api/coach/state).

Нужен запущенный сервер Pickline на --port.
"""
import argparse
import json
import sys
import time
import urllib.request

API = "https://api.opendota.com/api"
# суммарный опыт до уровня (Dota 7.3x, приблизительно - для симуляции хватает)
XP_TO_LEVEL = [0, 240, 640, 1160, 1760, 2440, 3200, 4000, 4900, 5900, 7000, 8200, 9500, 10900,
               12400, 14000, 15700, 17500, 19400, 21400, 23600, 26000, 28600, 31400, 34400, 38400,
               43400, 49400, 56400, 63900]


sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import net  # noqa: E402  - сетевой слой приложения: сертификаты, curl-запасной путь


def get(url):
    if url.startswith("http://127.0.0.1"):
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    return net.get_json(url, ttl=3600)


def post(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def level_at(xp_t, minute):
    xp = xp_t[min(minute, len(xp_t) - 1)] if xp_t else 0
    return max(1, sum(1 for t in XP_TO_LEVEL if xp >= t))


def find_match():
    for m in get(API + "/proMatches")[:20]:
        d = get(f"{API}/matches/{m['match_id']}")
        p = (d.get("players") or [{}])[0]
        if p.get("kills_log") is not None and p.get("xp_t"):
            return d
    sys.exit("не нашёл свежий разобранный матч - передайте --match ID")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", type=int)
    ap.add_argument("--slot", type=int, default=0)
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--speed", type=float, default=60)
    ap.add_argument("--minutes", type=int, default=0, help="проиграть только первые N минут")
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"
    m = get(f"{API}/matches/{a.match}") if a.match else find_match()
    p = m["players"][a.slot]
    heroes = {h["id"]: h for h in get(API + "/constants/heroes").values()}
    hero = heroes.get(p["hero_id"], {})
    npc = hero.get("name", "npc_dota_hero_unknown")
    team = "radiant" if p.get("isRadiant", p["player_slot"] < 128) else "dire"
    print(f"матч {m['match_id']}, {hero.get('localized_name')} ({team}), {m['duration'] // 60} мин")

    # смерти игрока - когда его убили другие
    deaths = sorted(e["time"] for q in m["players"] for e in (q.get("kills_log") or []) if e.get("key") == npc)
    kills = sorted(e["time"] for e in (p.get("kills_log") or []))
    buys = sorted((e["time"], e["key"]) for e in (p.get("purchase_log") or []) if not e["key"].startswith("recipe"))
    end = m["duration"] if not a.minutes else min(m["duration"], a.minutes * 60)
    max_hp = 600
    for t in range(-60, end + 1):
        minute = max(0, t // 60)
        lvl = level_at(p.get("xp_t"), minute)
        max_hp = 600 + 90 * lvl
        dead_until = next((d + 6 + 2 * lvl for d in deaths if d <= t < d + 6 + 2 * lvl), None)
        next_death = next((d for d in deaths if 0 <= d - t <= 5), None)
        hp = 0 if dead_until else (int(max_hp * 0.15) if next_death is not None else max_hp)
        inv = [k for tt, k in buys if tt <= t][-6:]
        payload = {
            "provider": {"timestamp": int(time.time())},
            "map": {"game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "clock_time": t,
                    "daytime": (max(t, 0) // 300) % 2 == 0},
            "player": {"team_name": team, "kills": sum(1 for k in kills if k <= t),
                       "deaths": sum(1 for d in deaths if d <= t), "assists": 0,
                       "last_hits": (p.get("lh_t") or [0])[min(minute, len(p.get("lh_t") or [0]) - 1)],
                       "denies": 0, "gold": 600, "gpm": p.get("gold_per_min"), "xpm": p.get("xp_per_min")},
            "hero": {"name": npc, "id": p["hero_id"], "level": lvl, "alive": not dead_until,
                     "health": hp, "max_health": max_hp, "mana": 300, "max_mana": 400,
                     "respawn_seconds": (dead_until - t) if dead_until else 0},
            "items": {f"slot{i}": {"name": "item_" + k} for i, k in enumerate(inv)},
            "abilities": {},
        }
        post(base + "/gsi", payload)
        time.sleep(1 / a.speed)

    ev = get(base + "/api/coach/events?since=0")
    print(f"\nсобытий тренера: {len(ev['events'])}")
    for e in ev["events"]:
        print(("  ! " if e["important"] else "    ") + e["text"])
    print("\nсводка:", get(base + "/api/coach/state").get("summary"))
    print(f"\nдля сверки: смертей в матче {len(deaths)}, убийств {len(kills)}")


if __name__ == "__main__":
    main()
