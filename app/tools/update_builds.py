# -*- coding: utf-8 -*-
"""Снимок сборок и прокачки всех героев по турнирным матчам в репозиторий.

Запуск:  python3 app/tools/update_builds.py [--months 3] [--tier top]

Зачем: сборка героя - три запроса к базе OpenDota (/explorer), по секундам
каждый, а на канале, где рукопожатие с OpenDota рвётся или данные идут
по полкилобайта в секунду, - минуты и ошибка. Со снимком общая сборка
показывается сразу с первого запуска, а живая (и сборка против текущего
драфта) подтягивается в фоне, когда сеть позволяет. В репозитории снимок
обновляет CI раз в неделю вместе с остальными.

Внутри - сырые строки ответов базы (закупы, итоговые предметы, прокачка),
чтобы сервер собирал из них показ тем же кодом, что и из живых данных.
"""
import argparse
import gzip
import json
import os
import sys
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

from sources import get_source  # noqa: E402

OUT = os.path.join(APP, "data", "builds_fallback.json.gz")


def main():
    ap = argparse.ArgumentParser(description="Снимок сборок героев")
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--tier", default="top")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    src = get_source()
    heroes = sorted(src.hero_stats(), key=lambda h: h["id"])
    builds, skills, failed = {}, {}, []
    started = time.time()
    for i, h in enumerate(heroes, 1):
        hid = h["id"]
        try:
            purchases, final = src.hero_builds(hid, args.months, args.tier)
            builds[str(hid)] = {"purchases": purchases, "final": final}
            skills[str(hid)] = src.hero_skills(hid, args.months, args.tier)
            games = int(purchases[0]["total_games"]) if purchases else 0
            print(f"  {i:3}/{len(heroes)} {h.get('localized_name'):20} игр {games:4}  "
                  f"({time.time() - started:.0f} с)", flush=True)
        except Exception as e:  # noqa: BLE001 - один герой не должен ронять снимок
            failed.append((h.get("localized_name"), str(e)[:120]))
            print(f"  {i:3}/{len(heroes)} {h.get('localized_name'):20} НЕ УДАЛОСЬ - {str(e)[:120]}",
                  flush=True)

    if len(builds) < len(heroes) // 2:
        print(f"собрано только {len(builds)} героев из {len(heroes)}, снимок не записан",
              file=sys.stderr)
        return 1

    payload = {
        "_комментарий": "Снимок сборок героев по турнирным матчам: сырые строки "
                        "закупов, итоговых предметов и прокачки. Пересобрать: "
                        "python3 app/tools/update_builds.py",
        "снято": date.today().isoformat(),
        "months": args.months,
        "tier": args.tier,
        "builds": builds,
        "skills": skills,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = args.out + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, args.out)
    size = os.path.getsize(args.out) // 1024
    print(f"записано {args.out}: {len(builds)} героев, {size} КБ, {time.time() - started:.0f} с")
    if failed:
        print(f"не собрано {len(failed)}: " + ", ".join(n for n, _ in failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
