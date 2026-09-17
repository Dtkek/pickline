# -*- coding: utf-8 -*-
"""Снимок тепловой карты вардов в репозиторий.

Запуск:  python3 app/tools/update_wards.py [--months 3] [--tier top]

Зачем: вкладка «Варды» считается по базе OpenDota (секунды), а на канале,
где OpenDota не отвечает, не показывалась вовсе. Снимок покрывает все
сочетания без фильтра по герою: тип (обзорные / стражи) × сторона
(Radiant / Dire / обе) × окно времени - 30 запросов. Сервер отдаёт его,
пока живой ответ не пришёл. В репозитории обновляет CI раз в неделю.
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

OUT = os.path.join(APP, "data", "wards_fallback.json.gz")
# те же окна, что в server.WARD_WINDOWS (минуты от рога)
WINDOWS = {"early": (-3, 5), "lane": (5, 10), "mid": (10, 20), "late": (20, 35), "end": (35, 180)}


def main():
    ap = argparse.ArgumentParser(description="Снимок вардов по турнирным матчам")
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--tier", default="top")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    src = get_source()
    cells, failed = {}, []
    started = time.time()
    for kind in ("obs", "sen"):
        for side in ("radiant", "dire", "both"):
            for window, (t0, t1) in WINDOWS.items():
                key = f"{kind}/{side}/{window}"
                try:
                    rows, total = src.ward_cells(args.months, args.tier, kind, side, None, t0 * 60, t1 * 60)
                    cells[key] = {"rows": rows, "total": total}
                    print(f"  {key:18} клеток {len(rows):3}, вардов {sum(int(r['n']) for r in rows):6}, "
                          f"матчей {total}  ({time.time() - started:.0f} с)", flush=True)
                except Exception as e:  # noqa: BLE001 - одно сочетание не должно ронять снимок
                    failed.append((key, str(e)[:120]))
                    print(f"  {key:18} НЕ УДАЛОСЬ - {str(e)[:120]}", flush=True)

    if len(cells) < 20:
        print(f"собрано только {len(cells)} сочетаний из 30, снимок не записан", file=sys.stderr)
        return 1

    payload = {
        "_комментарий": "Снимок вардов по турнирным матчам: клетки карты с числом вардов "
                        "по типу, стороне и окну времени. Пересобрать: "
                        "python3 app/tools/update_wards.py",
        "снято": date.today().isoformat(),
        "months": args.months,
        "tier": args.tier,
        "cells": cells,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = args.out + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, args.out)
    print(f"записано {args.out}: {len(cells)} сочетаний, {os.path.getsize(args.out) // 1024} КБ, "
          f"{time.time() - started:.0f} с")
    if failed:
        print("не собрано: " + ", ".join(k for k, _ in failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
