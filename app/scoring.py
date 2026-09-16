# -*- coding: utf-8 -*-
"""Подбор героев против вражеского драфта.

Как считается оценка
--------------------
Итоговый балл героя H против вражеского драфта E складывается из двух частей
(и третьей - синергии, см. ниже - когда источник её умеет):

    score(H) = W_MATCHUP * среднее_{e∈E} adv(H, e) + W_BASE * (winrate(H) − средний winrate)

1. adv(H, e) — насколько H лучше играет против e, чем e играет в среднем.
   Считается из матчапов: если e против H побеждает реже, чем вообще,
   значит H его контрит.

2. Вторая часть — базовая сила героя в выбранном ранге. Вес у неё малый:
   adv(H, e) уже содержит силу героя (сильный герой выигрывает у всех),
   и большая база считает её дважды. Бэктест на 20 000 ранговых матчей
   (app/tools/backtest.py) это подтверждает: одни матчапы предсказывают
   исход с AUC 0.598, с базой 0.6 — 0.588, с базой 0.2 — 0.594. База
   оставлена как страховка на случай, когда матчапов по врагу мало и
   сглаживание прижимает их к нулю.

Откуда берутся матчапы
----------------------
OpenDota отдаёт матчапы по матчам лиг (то, что лежит в их таблице
matches): у популярного героя ~3000 матчей в сумме по 126 соперникам,
у редкого ~800, отсюда медиана всего ~30 игр на пару. На исход ранговых
пабов эти таблицы не указывают вовсе (бэктест: AUC 0.51 - монетка), поэтому
основной источник - снимок STRATZ по публичным матчам (AUC 0.60).
Базовый винрейт соперника считается по той же таблице матчапов (сумма
побед делить на сумму игр). Смешивать датасеты нельзя: поля pro_win/pro_pick
почти пустые — у отдельных героев там 3 игры из 3, что дало бы «базу»
в 100% и сделало бы все оценки бессмысленными.

Сглаживание
-----------
При n=30 разброс винрейта — примерно ±9 процентных пунктов, то есть сырое число
почти ничего не значит. Поэтому каждое преимущество умножается на n / (n + K):
при маленькой выборке оценка прижимается к нулю, при большой — работает почти
целиком. K задаётся в K_SHRINK.

Синергия
--------
OpenDota не отдаёт винрейт пар героев в одной команде, поэтому с ним свои
герои учитываются только как «этих уже нельзя брать». STRATZ такие пары
отдаёт, и с ним в балл входит третье слагаемое - среднее по союзникам
syn(H, a): насколько H вместе с a выигрывает чаще, чем a в среднем.
Сглаживается так же, как матчапы, со своим K: разброс у синергий другой.

Веса проверены бэктестом
------------------------
python3 app/tools/backtest.py pub - берёт ранговые матчи из базы OpenDota,
сыгранные после даты снимка STRATZ, считает преимущество Radiant той же
формулой и сравнивает с исходом. На 20 000 матчей (2026-09-16): матчапы
STRATZ - точность знака 57%, AUC 0.60, и точность растёт с величиной
преимущества (при |edge| 3–5 п.п. - 70%). База и синергия сами по себе
слабее (AUC 0.56 и 0.58) и почти целиком дублируют то, что уже есть в
матчапах, поэтому их веса малы. Сетка весов - в выводе бэктеста.
"""

# вес матчапов и вес базовой силы героя в итоговом балле.
# База 0.2, а не больше: по бэктесту каждый шаг вверх снижает точность
# (0.0 → AUC 0.598, 0.2 → 0.594, 0.6 → 0.588, 1.0 → 0.582)
W_MATCHUP = 1.0
W_BASE = 0.2

# вес синергии с уже взятыми союзниками; работает только с источником,
# который отдаёт пары «вместе» (STRATZ). По бэктесту нейтральна к AUC,
# в Herald–Guardian и Divine–Immortal чуть поднимает точность знака
W_SYNERGY = 0.5

# Турнирная составляющая: насколько герой востребован и успешен у про.
# Вес выше, чем у остальных: по просьбе владельца турнирная мета имеет
# приоритет. Из интерфейса переключается: 0 - не учитывать, 0.6 - умеренно.
W_TOUR = 1.5

# Личная составляющая: как сам игрок играет на герое. Складывается из
# личного винрейта относительно общего (сжатого по числу игр) и опыта
# на герое: сотня игр на герое стоит больше, чем удачные три.
W_PERSONAL = 1.0
PERSONAL_WR_K = 20        # при стольких играх личный винрейт учитывается наполовину
PERSONAL_WR_SCALE = 0.5
PERSONAL_GAMES_SCALE = 0.01  # масштаб log(1 + игр) относительно среднего

# при таком числе турнирных пиков винрейт учитывается наполовину
TOUR_WR_K = 30
# масштаб спорности (пики+баны / матчей), чтобы единицы совпали с матчапами
TOUR_CONTEST_SCALE = 0.10
# масштаб турнирного винрейта
TOUR_WR_SCALE = 0.5

# границы — только страховка от вырожденных данных, не рабочая настройка
K_MIN, K_MAX = 20, 2000

# ранг с числом пиков меньше этого считаем непоказательным
MIN_PICKS = 200


def _safe_ratio(wins, games):
    return (wins / games) if games else None


def build_base_winrates(source, bracket, base_stats=None):
    """Базовый винрейт каждого героя в выбранном ранге + средний по всем.

    base_stats - {hero_id: (игр, побед)} от другого источника (STRATZ по
    рангу и позиции). Если передан, ранг source не используется: справочник
    героев всё равно берётся из source, а числа - отсюда.
    """
    stats = source.hero_stats()
    base = {}
    for h in stats:
        if base_stats is not None:
            picks, wins = base_stats.get(h["id"], (0, 0))
        else:
            picks, wins = source.picks_wins(h, bracket)
        base[h["id"]] = _safe_ratio(wins, picks) if picks >= MIN_PICKS else None
    known = [v for v in base.values() if v is not None]
    mean = sum(known) / len(known) if known else 0.5
    return base, mean, {h["id"]: h for h in stats}


def raw_matchup_table(source, enemy_id):
    """Несглаженные преимущества против enemy_id: {hero_id: (raw_adv, n, p)}.

    Базовый винрейт соперника берётся из его же таблицы матчапов, а не из
    heroStats: только так числитель и знаменатель считаются по одной выборке.
    """
    rows = source.matchups(enemy_id)
    total_games = sum((r.get("games_played") or 0) for r in rows)
    total_wins = sum((r.get("wins") or 0) for r in rows)
    baseline = _safe_ratio(total_wins, total_games)
    if baseline is None:
        return {}

    table = {}
    for row in rows:
        n = row.get("games_played") or 0
        if n <= 0:
            continue
        # wins в ответе — победы enemy_id против этого героя
        p = (row.get("wins") or 0) / n
        table[row["hero_id"]] = (baseline - p, n, p)
    return table


def estimate_k(tables):
    """Оценивает силу сглаживания K по самим данным (эмпирический байес).

    Разброс наблюдаемых преимуществ складывается из настоящего эффекта контрпика
    и шума малой выборки:

        Var(наблюдаемое) ≈ Var(истинное) + среднее(p(1-p)/n)

    Отсюда Var(истинное) = Var(наблюдаемое) − шум, а оптимальный коэффициент
    сжатия для пары — n / (n + K), где K = p(1-p) / Var(истинное).

    Смысл простой: чем сильнее наблюдаемый разброс объясняется шумом, тем больше
    K и тем жёстче оценки прижимаются к нулю. Считается по тем же таблицам,
    которые уже загружены для текущего запроса, — лишних обращений к API нет.

    Более известную оценку DerSimonian–Laird здесь применять нельзя: объёмы
    выборок различаются в сотни раз, её знаменатель вырождается, и она выдаёт
    заведомо абсурдный разброс. Простая оценка при этом несмещённая.
    """
    advs, noises = [], []
    for table in tables.values():
        for raw_adv, n, p in table.values():
            advs.append(raw_adv)
            noises.append(p * (1 - p) / n)
    if len(advs) < 30:
        return K_MAX

    mean_adv = sum(advs) / len(advs)
    var_observed = sum((a - mean_adv) ** 2 for a in advs) / len(advs)
    mean_noise = sum(noises) / len(noises)
    var_true = var_observed - mean_noise
    if var_true <= 0:
        # весь разброс объясняется шумом — значит, настоящего сигнала не видно
        return K_MAX
    k = 0.25 / var_true
    return max(K_MIN, min(K_MAX, k))


def shrink(tables, k):
    """Применяет сглаживание: {enemy_id: {hero_id: (adv, n)}}."""
    out = {}
    for enemy_id, table in tables.items():
        out[enemy_id] = {
            hero_id: (raw_adv * n / (n + k), n)
            for hero_id, (raw_adv, n, _p) in table.items()
        }
    return out


def tournament_strength(rows, total_matches):
    """Турнирная сила героя: {hero_id: доля} в тех же единицах, что матчапы.

    Складывается из двух вещей:
    1. спорность (пики + баны) / матчей, минус средняя по героям — насколько
       чаще среднего про-команды тянутся к этому герою или боятся его;
    2. турнирный винрейт минус 50%, сжатый по числу пиков: у героя с пятью
       пиками винрейт ничего не значит, у героя с двумя сотнями — значит.
    """
    if not rows or not total_matches:
        return {}
    contest = {}
    for r in rows:
        contest[int(r["hero_id"])] = (int(r["picks"]) + int(r["bans"])) / total_matches
    mean_contest = sum(contest.values()) / max(len(contest), 1)

    out = {}
    for r in rows:
        hid = int(r["hero_id"])
        picks, wins = int(r["picks"]), int(r["wins"])
        c = TOUR_CONTEST_SCALE * (contest[hid] - mean_contest)
        w = 0.0
        if picks:
            w = TOUR_WR_SCALE * (wins / picks - 0.5) * picks / (picks + TOUR_WR_K)
        out[hid] = c + w
    return out


def personal_strength(player_heroes, wl):
    """Личная сила на герое: {hero_id: доля} + сводка для показа.

    1. личный винрейт минус общий винрейт игрока, сжатый по числу игр:
       три победы из трёх ничего не значат, тридцать из сорока - значат;
    2. опыт: log(1 + игр) относительно среднего по героям - герой, которого
       игрок ни разу не брал, получает минус, основной герой - плюс.
    """
    import math
    if not player_heroes:
        return {}, {}
    total_w = int((wl or {}).get("win") or 0)
    total_l = int((wl or {}).get("lose") or 0)
    overall = total_w / (total_w + total_l) if (total_w + total_l) else 0.5

    logs = {}
    for r in player_heroes:
        logs[int(r["hero_id"])] = math.log1p(int(r.get("games") or 0))
    mean_log = sum(logs.values()) / max(len(logs), 1)

    strength, info = {}, {}
    for r in player_heroes:
        hid = int(r["hero_id"])
        g = int(r.get("games") or 0)
        w = int(r.get("win") or 0)
        wr_part = 0.0
        if g:
            wr_part = PERSONAL_WR_SCALE * (w / g - overall) * g / (g + PERSONAL_WR_K)
        exp_part = PERSONAL_GAMES_SCALE * (logs[hid] - mean_log)
        strength[hid] = wr_part + exp_part
        info[hid] = {"games": g, "winrate": round(w / g * 100, 1) if g else None}
    return strength, info


def matchup_tables(source, enemy_ids):
    """Сглаженные таблицы матчапов + использованное значение K."""
    raw = {e: raw_matchup_table(source, e) for e in enemy_ids}
    k = estimate_k(raw)
    return shrink(raw, k), k


def raw_synergy_table(provider, ally_id):
    """Несглаженные синергии с ally_id: {hero_id: (raw_syn, n, p)}.

    provider.synergies(ally_id) отдаёт игры и победы ally_id в одной команде
    с каждым героем. База - винрейт ally_id по всей его таблице «вместе»,
    как у матчапов: числитель и знаменатель из одной выборки.
    """
    rows = provider.synergies(ally_id)
    total_games = sum((r.get("games_played") or 0) for r in rows)
    total_wins = sum((r.get("wins") or 0) for r in rows)
    baseline = _safe_ratio(total_wins, total_games)
    if baseline is None:
        return {}

    table = {}
    for row in rows:
        n = row.get("games_played") or 0
        if n <= 0:
            continue
        p = (row.get("wins") or 0) / n
        # выигрывают вместе - знак плюс, в отличие от матчапов
        table[row["hero_id"]] = (p - baseline, n, p)
    return table


def synergy_tables(provider, ally_ids):
    """Сглаженные таблицы синергий + своё K: разброс у пар «вместе» другой."""
    raw = {a: raw_synergy_table(provider, a) for a in ally_ids}
    k = estimate_k(raw)
    return shrink(raw, k), k


def recommend(source, enemy_ids, ally_ids=(), banned_ids=(), bracket="all",
              role=None, limit=15, allowed_ids=None, tour=None, tour_weight=W_TOUR,
              personal=None, personal_info=None, personal_weight=W_PERSONAL,
              matchups=None, synergies=None, base_stats=None,
              synergy_weight=W_SYNERGY):
    """Топ героев против вражеского драфта.

    enemy_ids — герои противника, ally_ids — уже взятые свои,
    banned_ids — забаненные. Возвращает список словарей, отсортированный по баллу.

    matchups - откуда брать матчапы: объект с .matchups(hero_id); по умолчанию
    сам source. synergies - объект с .synergies(hero_id), если источник умеет
    пары «вместе»; без него синергия не считается. base_stats - базовые
    винрейты другого источника, см. build_base_winrates.
    """
    enemy_ids = [int(x) for x in enemy_ids]
    ally_ids = [int(x) for x in ally_ids]
    excluded = set(ally_ids) | set(int(x) for x in banned_ids) | set(enemy_ids)

    base, mean_base, stats_by_id = build_base_winrates(source, bracket, base_stats)
    tables, k_used = matchup_tables(matchups or source, enemy_ids)

    syn_tables, k_syn = {}, None
    with_synergy = bool(synergies is not None and ally_ids and synergy_weight)
    if with_synergy:
        syn_tables, k_syn = synergy_tables(synergies, ally_ids)

    results = []
    for hero_id, stat in stats_by_id.items():
        if hero_id in excluded:
            continue
        # allowed_ids — фильтр по позиции: он приходит снаружи, потому что
        # позиции берутся из отдельного справочника, а не из данных источника
        if allowed_ids is not None and hero_id not in allowed_ids:
            continue
        if role and role not in (stat.get("roles") or []):
            continue

        per_enemy = []
        matchup_sum = 0.0
        games_total = 0
        for e in enemy_ids:
            adv, n = tables[e].get(hero_id, (0.0, 0))
            matchup_sum += adv
            games_total += n
            per_enemy.append({"enemy_id": e, "adv_pp": round(adv * 100, 2), "games": n})

        # среднее, а не сумма: складывать пять матчапов значит утверждать, что
        # они независимы и складываются один к одному — это неверно и завышает
        # оценку в разы. На порядок героев среднее не влияет, зато число
        # остаётся интерпретируемым: «столько винрейта против типичного из них».
        matchup_avg = matchup_sum / len(enemy_ids)

        # синергия: среднее по союзникам, по той же логике, что и матчапы
        per_ally = []
        syn_avg = 0.0
        if with_synergy:
            syn_sum = 0.0
            for a in ally_ids:
                syn, n = syn_tables[a].get(hero_id, (0.0, 0))
                syn_sum += syn
                per_ally.append({"ally_id": a, "syn_pp": round(syn * 100, 2), "games": n})
            syn_avg = syn_sum / len(ally_ids)

        hero_base = base.get(hero_id)
        base_delta = (hero_base - mean_base) if hero_base is not None else 0.0

        # турнирная составляющая: словарь приходит снаружи, потому что
        # его источник и период выбирает сервер, а не логика подбора
        tour_val = (tour or {}).get(hero_id, 0.0) if tour_weight else 0.0
        pers_val = (personal or {}).get(hero_id, 0.0) if personal_weight else 0.0
        mine = (personal_info or {}).get(hero_id) or {}

        score = (W_MATCHUP * matchup_avg + W_BASE * base_delta
                 + synergy_weight * syn_avg
                 + tour_weight * tour_val + personal_weight * pers_val)

        results.append({
            "synergy_pp": round(synergy_weight * syn_avg * 100, 2) if with_synergy else None,
            "per_ally": per_ally,
            "personal_pp": round(personal_weight * pers_val * 100, 2),
            "my_games": mine.get("games"),
            "my_winrate": mine.get("winrate"),
            "hero_id": hero_id,
            "name": stat.get("localized_name"),
            "img": stat.get("img"),
            "roles": stat.get("roles") or [],
            "primary_attr": stat.get("primary_attr"),
            # вклады уже с весами, чтобы matchup_pp + base_pp + tour_pp == score_pp
            "score_pp": round(score * 100, 2),
            "matchup_pp": round(W_MATCHUP * matchup_avg * 100, 2),
            "base_pp": round(W_BASE * base_delta * 100, 2),
            "tour_pp": round(tour_weight * tour_val * 100, 2),
            "base_winrate": round(hero_base * 100, 2) if hero_base is not None else None,
            "games_total": games_total,
            "per_enemy": per_enemy,
        })

    results.sort(key=lambda r: r["score_pp"], reverse=True)
    return results[:limit], round(k_used), (round(k_syn) if k_syn is not None else None)


def draft_edge(source, radiant_ids, dire_ids, bracket="all"):
    """Кто выигрывал по матчапам на стадии драфта. Для разбора про-матчей.

    Возвращает суммарное преимущество Radiant в процентных пунктах и разбивку
    по героям: у кого из Radiant драфт сложился удачно, у кого нет.
    """
    _, _, stats_by_id = build_base_winrates(source, bracket)
    tables, k_used = matchup_tables(source, dire_ids)

    per_hero = []
    total = 0.0
    for h in radiant_ids:
        # как и в recommend: среднее по соперникам, а не сумма
        adv = (sum(tables[e].get(h, (0.0, 0))[0] for e in dire_ids)
               / max(len(dire_ids), 1))
        games = sum(tables[e].get(h, (0.0, 0))[1] for e in dire_ids)
        total += adv
        stat = stats_by_id.get(h, {})
        per_hero.append({
            "hero_id": h,
            "name": stat.get("localized_name"),
            "img": stat.get("img"),
            "adv_pp": round(adv * 100, 2),
            "games": games,
        })
    per_hero.sort(key=lambda r: r["adv_pp"], reverse=True)
    # среднее по героям Radiant: «типичный герой Radiant имел столько-то
    # преимущества против типичного героя Dire»
    total /= max(len(radiant_ids), 1)
    return {
        "radiant_edge_pp": round(total * 100, 2),
        "radiant_heroes": per_hero,
        "k_shrink": round(k_used),
    }
