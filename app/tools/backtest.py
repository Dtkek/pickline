# -*- coding: utf-8 -*-
"""Бэктест модели подбора: предсказывает ли балл исход реальных матчей.

Запуск:
  python3 app/tools/backtest.py pub  [--limit 6000]              # публичные ранговые
  python3 app/tools/backtest.py pro  [--months 3] [--tier top]   # турнирные

Для каждого матча считается симметричное преимущество Radiant той же
математикой, что и подбор (scoring.py): матчапы со сглаживанием, база,
синергия - и сравнивается знак с фактическим победителем.

Что смотреть в выводе:
  - точность знака против «всегда Radiant»;
  - AUC: 0.5 - монетка, чем выше, тем лучше балл ранжирует исходы;
  - корзины по |edge|: если при большом преимуществе точность выше, чем
    при малом, величина балла что-то значит, а не только знак;
  - сетка весов: точность при разных W_BASE / W_SYNERGY.

Откуда матчи и что с чем сравнивать честно:
  pub - таблица public_matches базы OpenDota: ранговые All Pick с
        avg_rank_tier, только сыгранные ПОСЛЕ даты снимка STRATZ, чтобы
        проверка была вне выборки. Матчи группируются по рангу и
        сверяются с таблицами STRATZ того же ранга - как в приложении.
  pro - турнирные матчи. Матчапы OpenDota (/heroes/{id}/matchups) сами
        посчитаны по про-матчам базы, то есть содержат проверяемые игры:
        их точность на pro завышена (внутри выборки) и её нельзя
        принимать за прогноз. STRATZ по пабам на pro - честно, но драфты
        про заранее контрят друг друга, и сигнал слабее, чем в пабах.
"""
import argparse
import os
import sys
import time
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

import scoring  # noqa: E402
from sources import get_source, get_stratz  # noqa: E402
from sources.opendota import _explorer, TTL_TOURNAMENT  # noqa: E402

TIERS = {"top": ("premium", "professional"), "premium": ("premium",),
         "all": ("premium", "professional", "excluded", "amateur")}
PAGE = 200
PUB_PAGE = 1000

# десяток avg_rank_tier (1 Herald .. 8 Immortal) -> ранг снимка STRATZ
RANK_TO_BRACKET = {1: "HERALD_GUARDIAN", 2: "HERALD_GUARDIAN",
                   3: "CRUSADER_ARCHON", 4: "CRUSADER_ARCHON",
                   5: "LEGEND_ANCIENT", 6: "LEGEND_ANCIENT",
                   7: "DIVINE_IMMORTAL", 8: "DIVINE_IMMORTAL"}


def fetch_pro(src, months, tier, limit, log):
    """Турнирные матчи с полными драфтами: [(id, radiant_win, radiant, dire)].

    Пики берутся из таблицы picks_bans (как в tournament_stats), а не из
    jsonb-колонки matches: так запросы укладываются в лимит базы.
    """
    bound = src.period_bound(months)
    tiers = ", ".join(f"'{t}'" for t in TIERS[tier])
    out, last = [], bound - 1
    while len(out) < limit:
        rows = _explorer(f"""
            select m.match_id, m.radiant_win,
              string_agg(pb.team::text || ':' || pb.hero_id::text, ',') as picks
            from matches m join leagues l on l.leagueid = m.leagueid
              join picks_bans pb on pb.match_id = m.match_id and pb.is_pick
            where m.match_id > {last}
              and m.start_time > extract(epoch from now() - interval '{int(months)} months')
              and l.tier in ({tiers}) and m.radiant_win is not null
            group by m.match_id, m.radiant_win
            order by m.match_id limit {PAGE}
        """, ttl=TTL_TOURNAMENT).get("rows") or []
        if not rows:
            break
        for r in rows:
            last = int(r["match_id"])
            radiant, dire = [], []
            for token in (r.get("picks") or "").split(","):
                team, _, hero = token.partition(":")
                if hero:
                    (radiant if team == "0" else dire).append(int(hero))
            if len(radiant) == 5 and len(dire) == 5:
                out.append((last, bool(r["radiant_win"]), radiant, dire, None))
        log(f"  матчей собрано: {len(out)}")
        if len(rows) < PAGE:
            break
    return out[:limit]


def match_id_at(ts):
    """Первый match_id в public_matches не раньше ts - бисекцией по индексу.

    Условие по start_time без границы по match_id база не тянет (нет
    индекса), а запрос «последняя строка с match_id <= X» отвечает за
    доли секунды: ~35 таких запросов дешевле одного полного прохода.
    """
    hi = int(_explorer("select max(match_id) as m from public_matches",
                       ttl=TTL_TOURNAMENT)["rows"][0]["m"])
    lo = 0
    while hi - lo > 1000:
        mid = (lo + hi) // 2
        rows = _explorer(f"""
            select start_time from public_matches
            where match_id <= {mid} order by match_id desc limit 1
        """, ttl=TTL_TOURNAMENT).get("rows") or []
        if rows and int(rows[0]["start_time"]) >= ts:
            hi = mid
        else:
            lo = mid
    return hi


def fetch_pub(since_ts, limit, log):
    """Ранговые All Pick из public_matches после since_ts: [(id, win, r, d, ранг)]."""
    lower = match_id_at(since_ts)
    out, last = [], None
    while len(out) < limit:
        upper = f"and match_id < {last}" if last else ""
        rows = _explorer(f"""
            select match_id, radiant_win, avg_rank_tier, radiant_team, dire_team
            from public_matches
            where match_id > {lower} {upper} and lobby_type = 7 and game_mode = 22
              and radiant_team[1] > 0 and avg_rank_tier is not null
            order by match_id desc limit {PUB_PAGE}
        """, ttl=TTL_TOURNAMENT).get("rows") or []
        if not rows:
            break
        for r in rows:
            last = int(r["match_id"])
            radiant, dire = list(r["radiant_team"] or []), list(r["dire_team"] or [])
            if len(radiant) == 5 and len(dire) == 5 and 0 not in radiant and 0 not in dire:
                out.append((last, bool(r["radiant_win"]), radiant, dire, int(r["avg_rank_tier"]) // 10))
        log(f"  матчей собрано: {len(out)}")
        if len(rows) < PUB_PAGE:
            break
    return out[:limit]


class Tables:
    """Кэш сырых таблиц по герою: сглаживание - по набору, как в recommend."""

    def __init__(self, raw_fn):
        self.raw_fn = raw_fn
        self.cache = {}

    def raw(self, hero_id):
        if hero_id not in self.cache:
            try:
                self.cache[hero_id] = self.raw_fn(hero_id)
            except Exception:  # noqa: BLE001 - нет таблицы: герой без данных
                self.cache[hero_id] = {}
        return self.cache[hero_id]

    def smoothed(self, ids):
        raw = {h: self.raw(h) for h in ids}
        return scoring.shrink(raw, scoring.estimate_k(raw))


def side_edge(tables, my_ids, enemy_ids):
    """Среднее преимущество своих героев против врагов (как в recommend)."""
    total = 0.0
    for h in my_ids:
        total += sum(tables[e].get(h, (0.0, 0))[0] for e in enemy_ids) / len(enemy_ids)
    return total / len(my_ids)


def matchup_edge(tables, r_ids, d_ids):
    """Преимущество Radiant по матчапам: их таблицы против Dire минус наоборот."""
    against_dire = tables.smoothed(d_ids)
    against_radiant = tables.smoothed(r_ids)
    return side_edge(against_dire, r_ids, d_ids) - side_edge(against_radiant, d_ids, r_ids)


def synergy_edge(tables, ids):
    """Средняя синергия внутри команды по парам «вместе»."""
    smoothed = tables.smoothed(ids)
    total, n = 0.0, 0
    for a in ids:
        for h in ids:
            if h != a:
                total += smoothed[a].get(h, (0.0, 0))[0]
                n += 1
    return total / n if n else 0.0


def base_edge(base, mean, r_ids, d_ids):
    """Разница средних отклонений базового винрейта от среднего."""
    def delta(h):
        v = base.get(h)
        return (v - mean) if v is not None else 0.0
    return sum(delta(h) for h in r_ids) / 5 - sum(delta(h) for h in d_ids) / 5


def accuracy(rows):
    n = len(rows)
    return sum(1 for e, rw in rows if (e > 0) == rw) / n if n else float("nan")


def auc(rows):
    wins = [e for e, rw in rows if rw]
    losses = [e for e, rw in rows if not rw]
    if not wins or not losses:
        return float("nan")
    return sum(1.0 if w > l else 0.5 if w == l else 0.0
               for w in wins for l in losses) / (len(wins) * len(losses))


def evaluate(matches, name, edge_fn, log, buckets=True):
    """Точность знака, AUC и точность по корзинам |edge|."""
    rows = [(edge_fn(r_ids, d_ids), rw) for _mid, rw, r_ids, d_ids, _rank in matches]
    log(f"\n{name}: матчей {len(rows)}, точность знака {accuracy(rows) * 100:.1f}%, AUC {auc(rows):.3f}")
    if not buckets:
        return
    for lo, hi in [(0, 1), (1, 2), (2, 3), (3, 5), (5, 100)]:
        sel = [(e, rw) for e, rw in rows if lo <= abs(e) * 100 < hi]
        if sel:
            log(f"   |edge| {lo}-{hi} п.п.: {len(sel):5} матчей, точность {accuracy(sel) * 100:.1f}%")


def weight_grid(matches, parts, log):
    """Точность и AUC при разных весах базы и синергии.

    parts(r, d) -> (matchup, base, synergy, base_opendota) без весов, чтобы
    не пересчитывать таблицы для каждой комбинации. Последняя колонка -
    то же, но с базой OpenDota вместо базы снимка.
    """
    cache = [(parts(r, d), rw) for _mid, rw, r, d, _rank in matches]
    log("\n   сетка весов (W_MATCHUP = 1); справа - с базой OpenDota вместо STRATZ:")
    log("   W_BASE  W_SYN   точность   AUC    | точность   AUC")
    for wb in (0.0, 0.1, 0.2, 0.3, 0.6, 1.0):
        for ws in (0.0, 0.5, 1.0):
            rows = [(m + wb * b + ws * s, rw) for (m, b, s, _), rw in cache]
            alt = [(m + wb * b2 + ws * s, rw) for (m, _, s, b2), rw in cache]
            log(f"   {wb:5.1f}  {ws:5.1f}   {accuracy(rows) * 100:6.1f}%   {auc(rows):.3f}"
                f"  |  {accuracy(alt) * 100:6.1f}%   {auc(alt):.3f}")


def stratz_parts(stz, bracket, od_base):
    """Составляющие балла по снимку STRATZ выбранного ранга + база OpenDota."""
    bound = stz.bound(bracket)
    if bound is None:
        return None
    stats = stz.base_stats(bracket)
    base = {hid: (w / g) for hid, (g, w) in stats.items() if g >= scoring.MIN_PICKS}
    mean = sum(base.values()) / len(base) if base else 0.5
    vs = Tables(lambda h: scoring.raw_matchup_table(bound, h))
    syn = Tables(lambda h: scoring.raw_synergy_table(bound, h))
    base_od, mean_od = od_base

    def parts(r, d):
        return (matchup_edge(vs, r, d), base_edge(base, mean, r, d),
                synergy_edge(syn, r) - synergy_edge(syn, d),
                base_edge(base_od, mean_od, r, d))
    return parts


def report_stratz(matches, stz, bracket, od_base, log, grid=True):
    parts = stratz_parts(stz, bracket, od_base)
    if parts is None:
        log(f"в снимке STRATZ нет ранга {bracket}")
        return
    evaluate(matches, f"STRATZ {bracket}: только матчапы", lambda r, d: parts(r, d)[0], log)
    evaluate(matches, f"STRATZ {bracket}: только база", lambda r, d: parts(r, d)[1], log, buckets=False)
    evaluate(matches, f"STRATZ {bracket}: только синергия", lambda r, d: parts(r, d)[2], log, buckets=False)

    def full(r, d):
        m, b, s, _ = parts(r, d)
        return m + scoring.W_BASE * b + scoring.W_SYNERGY * s
    evaluate(matches, f"STRATZ {bracket}: матчапы + база + синергия (веса приложения)", full, log)
    if grid:
        weight_grid(matches, parts, log)


def report_opendota(matches, src, log):
    """Отчёт по OpenDota; возвращает (база, среднее) для сеток STRATZ."""
    base_od, mean_od, _ = scoring.build_base_winrates(src, "all")
    od = Tables(lambda h: scoring.raw_matchup_table(src, h))
    evaluate(matches, "OpenDota: только матчапы", lambda r, d: matchup_edge(od, r, d), log)
    evaluate(matches, "OpenDota: только база", lambda r, d: base_edge(base_od, mean_od, r, d), log, buckets=False)
    evaluate(matches, "OpenDota: матчапы + база (веса приложения)",
             lambda r, d: matchup_edge(od, r, d) + scoring.W_BASE * base_edge(base_od, mean_od, r, d), log)
    return base_od, mean_od


def main():
    ap = argparse.ArgumentParser(description="Бэктест подбора на реальных матчах")
    ap.add_argument("kind", choices=("pub", "pro"))
    ap.add_argument("--months", type=int, default=3, help="pro: период")
    ap.add_argument("--tier", default="top", choices=sorted(TIERS), help="pro: уровень турниров")
    ap.add_argument("--limit", type=int, default=0, help="сколько матчей (0 - по умолчанию для вида)")
    ap.add_argument("--bracket", default="", help="pro: ранг снимка STRATZ (по умолчанию all и DIVINE_IMMORTAL)")
    args = ap.parse_args()

    def log(msg):
        print(msg, flush=True)

    src = get_source()
    stz = get_stratz()
    started = time.time()

    if args.kind == "pro":
        matches = fetch_pro(src, args.months, args.tier, args.limit or 3000, log)
        log(f"турнирных матчей с полными драфтами: {len(matches)} ({time.time() - started:.0f} с)")
        if not matches:
            return 1
        log(f"победы Radiant: {sum(1 for m in matches if m[1]) / len(matches) * 100:.1f}% "
            "- базовая точность «всегда Radiant»")
        od_base = report_opendota(matches, src, log)
        log("\n(матчапы OpenDota посчитаны по этим же про-матчам - их точность выше внутри выборки)")
        if stz.available:
            for bracket in ([args.bracket] if args.bracket else ["all", "DIVINE_IMMORTAL"]):
                report_stratz(matches, stz, bracket, od_base, log)
        log(f"\nвсего {time.time() - started:.0f} с")
        return 0

    if not stz.available:
        log("снимка STRATZ нет - для pub он обязателен")
        return 1
    since = datetime.combine(date.fromisoformat(stz.date), datetime.min.time()).timestamp()
    log(f"снимок STRATZ от {stz.date}; берём ранговые матчи после этой даты")
    matches = fetch_pub(since, args.limit or 6000, log)
    log(f"публичных ранговых матчей: {len(matches)} ({time.time() - started:.0f} с)")
    if not matches:
        return 1
    log(f"победы Radiant: {sum(1 for m in matches if m[1]) / len(matches) * 100:.1f}% "
        "- базовая точность «всегда Radiant»")

    log("\n=== все ранги вместе, таблицы STRATZ all ===")
    od_base = report_opendota(matches, src, log)
    report_stratz(matches, stz, "all", od_base, log)

    by_bracket = {}
    for m in matches:
        key = RANK_TO_BRACKET.get(m[4])
        if key:
            by_bracket.setdefault(key, []).append(m)
    for bracket in ("HERALD_GUARDIAN", "CRUSADER_ARCHON", "LEGEND_ANCIENT", "DIVINE_IMMORTAL"):
        sel = by_bracket.get(bracket) or []
        if len(sel) < 200:
            continue
        log(f"\n=== матчи ранга {bracket}: {len(sel)}, таблицы STRATZ того же ранга ===")
        report_stratz(sel, stz, bracket, od_base, log)
    log(f"\nвсего {time.time() - started:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
