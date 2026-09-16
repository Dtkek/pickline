# -*- coding: utf-8 -*-
"""Источник данных OpenDota — публичный API, ключ не нужен.

Ограничения, которые важно понимать при чтении рекомендаций:
матчапы «герой против героя» OpenDota отдаёт по профессиональным матчам,
и выборка там небольшая — медиана около 30 игр на пару героев. Поэтому
в scoring.py применяется сглаживание, а в интерфейсе показывается объём выборки.
"""
import gzip
import json
import os

import net
from net import get_json
from sources.base import HeroSource

API = "https://api.opendota.com/api"

FALLBACK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "herostats_fallback.json.gz")

MATCHUPS_FALLBACK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "matchups_fallback.json.gz")

ITEMS_FALLBACK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "items_fallback.json.gz")

_fallback_cache = None
fallback_date = None
_matchups_cache = None
matchups_date = None
_items_cache = None
_abilities_cache = None

ABILITIES_FALLBACK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "abilities_fallback.json.gz")


def _load_abilities():
    global _abilities_cache
    if _abilities_cache is not None:
        return _abilities_cache
    try:
        with gzip.open(ABILITIES_FALLBACK, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        _abilities_cache = {
            "ids": payload.get("ids") or {},
            "heroes": payload.get("heroes") or {},
            "abilities": payload.get("abilities") or {},
        }
    except (OSError, ValueError):
        _abilities_cache = {"ids": {}, "heroes": {}, "abilities": {}}
    return _abilities_cache


def _load_items():
    global _items_cache
    if _items_cache is not None:
        return _items_cache
    try:
        with gzip.open(ITEMS_FALLBACK, "rt", encoding="utf-8") as f:
            _items_cache = json.load(f).get("items") or {}
    except (OSError, ValueError):
        _items_cache = {}
    return _items_cache


def _load_matchups():
    """Снимок матчапов, приложенный к репозиторию: {id героя: [[id, игр, побед]]}."""
    global _matchups_cache, matchups_date
    if _matchups_cache is not None:
        return _matchups_cache
    try:
        with gzip.open(MATCHUPS_FALLBACK, "rt", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        _matchups_cache = {}
        return _matchups_cache
    _matchups_cache = payload.get("matchups") or {}
    matchups_date = payload.get("снято")
    return _matchups_cache


def _load_fallback():
    """Читает снимок справочника, приложенный к репозиторию."""
    global _fallback_cache, fallback_date
    if _fallback_cache is not None:
        return _fallback_cache
    try:
        with gzip.open(FALLBACK, "rt", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    _fallback_cache = payload.get("heroes")
    fallback_date = payload.get("снято")
    return _fallback_cache

# час для меты, сутки для справочника героев: герои меняются куда реже статистики
TTL_HEROES = 24 * 3600
TTL_STATS = 3600
TTL_MATCHUPS = 6 * 3600
TTL_PRO_LIST = 600
TTL_MATCH = 7 * 24 * 3600
TTL_TOURNAMENT = 6 * 3600


def _explorer(sql, ttl, retries=3):
    """Запрос к базе OpenDota через /explorer с кэшем, как у остальных данных.

    У базы лимит около 15 секунд на запрос, и первый запрос по герою
    в него часто не укладывается: данные читаются с диска «холодными».
    Замер: два простейших запроса подряд - таймаут, следующие пять по тому
    же герою - 0.8-3.7 с. То есть первая попытка греет кэш базы, вторая
    проходит. Поэтому повторяем с паузой, а не упрощаем SQL.

    Ответ с err нельзя ни принимать за пустой результат, ни оставлять
    в кэше - иначе ошибка живёт шесть часов.
    """
    import time as _time
    import urllib.parse
    url = f"{API}/explorer?sql={urllib.parse.quote(' '.join(sql.split()))}"
    last = None
    for attempt in range(retries + 1):
        data = get_json(url, ttl=ttl, timeout=120)
        err = data.get("err") if isinstance(data, dict) else "не JSON-объект"
        if not err:
            return data
        last = str(err)[:200]
        net.forget(url)
        if attempt < retries:
            _time.sleep(2.0)
    raise RuntimeError(f"база OpenDota не ответила после {retries + 1} попыток: {last}")


class OpenDotaSource(HeroSource):
    name = "OpenDota"

    # у OpenDota поля вида 1_pick..8_pick — это ранги от Herald до Immortal.
    # Список кандидатов; пустые отсеивает available_brackets().
    brackets = [
        ("all", "Все ранги"),
        ("8", "Immortal"),
        ("7", "Divine"),
        ("6", "Ancient"),
        ("5", "Legend"),
        ("4", "Archon"),
        ("3", "Crusader"),
        ("2", "Guardian"),
        ("1", "Herald"),
        ("turbo", "Turbo"),
    ]

    #: True, если справочник пришёл из локального снимка, а не из сети
    using_fallback = False

    def heroes(self):
        return get_json(f"{API}/heroes", ttl=TTL_HEROES)

    def hero_stats(self):
        """Справочник героев: свежий кэш → снимок с фоновым обновлением → сеть.

        Тот же порядок, что у матчапов, и по той же причине: на медленном
        канале поход в сеть за справочником занимал до полутора минут, и всё
        это время сервер даже не стартовал — окно выглядело зависшим.
        Со снимком запуск мгновенный, а свежие данные подтягиваются фоном
        и подхватятся на следующем запросе.
        """
        url = f"{API}/heroStats"

        fresh = net.cached(url, TTL_STATS)
        if fresh is not None:
            OpenDotaSource.using_fallback = False
            return fresh

        snapshot = _load_fallback()
        if snapshot is not None:
            OpenDotaSource.using_fallback = True
            net.refresh_in_background(url, TTL_STATS)
            return snapshot

        data = get_json(url, ttl=TTL_STATS)
        OpenDotaSource.using_fallback = False
        return data

    def matchups(self, hero_id):
        """Матчапы героя. Снимок в приоритете, сеть — фоном.

        На медленном канале запрос к OpenDota стоит десятки секунд, а на
        драфт из пяти врагов их нужно пять. Поэтому порядок такой:
        свежий кэш → снимок из репозитория (мгновенно, обновление уходит
        в фон) → и только если снимка нет, ждём сеть.
        """
        hero_id = int(hero_id)
        url = f"{API}/heroes/{hero_id}/matchups"

        fresh = net.cached(url, TTL_MATCHUPS)
        if fresh is not None:
            return fresh

        snapshot = _load_matchups().get(str(hero_id))
        if snapshot:
            net.refresh_in_background(url, TTL_MATCHUPS)
            return [{"hero_id": a, "games_played": b, "wins": c}
                    for a, b, c in snapshot]

        return get_json(url, ttl=TTL_MATCHUPS)

    def pro_matches(self):
        return get_json(f"{API}/proMatches", ttl=TTL_PRO_LIST)

    def pro_matches_status(self):
        """Когда список обновлялся и почему не обновился, если не обновился."""
        return net.status(f"{API}/proMatches")

    # --- турнирная статистика ------------------------------------------
    # Поля pro_pick/pro_win в heroStats почти пустые, поэтому турнирная
    # статистика считается напрямую по базе матчей через /explorer:
    # picks_bans — драфт, matches — исход, leagues — уровень турнира.

    TIERS = {
        "top": ("premium", "professional"),
        "premium": ("premium",),
        "all": ("premium", "professional", "excluded", "amateur"),
    }

    def period_bound(self, months=3):
        """Наименьший match_id за период - нижняя граница для запросов к базе.

        У базы OpenDota нет индекса по start_time: условие «за 3 месяца»
        перебирает всю историю, и даже count(*) по герою не укладывается
        в их лимит 15 с. Номера матчей растут со временем, и по match_id
        индекс есть: с условием match_id >= граница тот же запрос проходит
        за 4-6 с (замер на Axe, Juggernaut, Invoker).

        Точную границу база тоже ищет долго, поэтому в два шага: грубая
        оценка по скорости роста номеров в /proMatches (за неделю, с
        трёхкратным запасом), а с ней точный min(match_id) - 0.3 с.
        """
        months = int(months)
        pro = [x for x in (self.pro_matches() or [])
               if x.get("match_id") and x.get("start_time")]
        if len(pro) < 2:
            return 0
        newest = max(pro, key=lambda x: x["start_time"])
        oldest = min(pro, key=lambda x: x["start_time"])
        span = max(newest["start_time"] - oldest["start_time"], 3600)
        rate = (newest["match_id"] - oldest["match_id"]) / span
        rough = int(newest["match_id"] - rate * months * 31 * 86400 * 3)
        rows = _explorer(f"""
            select min(match_id) as mid from matches
            where match_id >= {max(rough, 0)}
              and start_time > extract(epoch from now() - interval '{months} months')
        """, ttl=TTL_TOURNAMENT).get("rows") or []
        mid = rows[0].get("mid") if rows else None
        return int(mid) if mid else max(rough, 0)

    def _period_sql(self, months, alias="m"):
        """Условие «матч за период» с границей по match_id."""
        return (f"{alias}.match_id >= {self.period_bound(months)} and "
                f"{alias}.start_time > extract(epoch from now() - interval '{int(months)} months')")

    def tournament_leagues(self, months=3):
        """Турниры за период: id, название, уровень, число матчей."""
        sql = f"""
        select l.leagueid, l.name, l.tier, count(*) as matches
        from matches m join leagues l on l.leagueid = m.leagueid
        where {self._period_sql(months)}
        group by l.leagueid, l.name, l.tier
        order by matches desc
        limit 60
        """
        return _explorer(sql, ttl=TTL_TOURNAMENT).get("rows") or []

    def tournament_stats(self, months=3, tier="top", leagueid=None):
        """Пики, баны и победы каждого героя в турнирных матчах.

        Возвращает (строки, всего_матчей). Победа засчитывается по стороне:
        team 0 в picks_bans — Radiant, 1 — Dire.
        """
        tiers = self.TIERS.get(tier, self.TIERS["top"])
        tier_sql = ", ".join(f"'{t}'" for t in tiers)
        league_sql = f"and m.leagueid = {int(leagueid)}" if leagueid else ""
        sql = f"""
        with pro as (
          select m.match_id, m.radiant_win
          from matches m join leagues l on l.leagueid = m.leagueid
          where {self._period_sql(months)}
            and l.tier in ({tier_sql}) {league_sql}
        )
        select * from (
          select pb.hero_id,
            sum(case when pb.is_pick then 1 else 0 end) as picks,
            sum(case when not pb.is_pick then 1 else 0 end) as bans,
            sum(case when pb.is_pick and ((pb.team = 0 and pro.radiant_win)
                  or (pb.team = 1 and not pro.radiant_win)) then 1 else 0 end) as wins,
            (select count(*) from pro) as total_matches
          from picks_bans pb join pro on pro.match_id = pb.match_id
          group by pb.hero_id
        ) t
        order by picks + bans desc
        """
        rows = _explorer(sql, ttl=TTL_TOURNAMENT).get("rows") or []
        total = rows[0]["total_matches"] if rows else 0
        return rows, total

    def match(self, match_id):
        """Матч целиком с /matches/{id} — 45 КБ на проводе и больше.

        На каналах, где соединение обрывается на 20-30 КБ, это никогда
        не доходит. Основной путь теперь match_slim(); этот — запасной.
        """
        return get_json(f"{API}/matches/{int(match_id)}", ttl=TTL_MATCH)

    def match_slim(self, match_id):
        """Только нужные поля матча через базу OpenDota, тремя запросами.

        Каждый кусок — единицы килобайт, чтобы проходить через самый
        капризный канал: шапка с драфтом, игроки со статистикой, закупы.
        Закупы идут отдельно и последними: они самые тяжёлые, и без них
        матч всё равно показать можно. Возвращает структуру, совместимую
        с ответом /matches/{id} в той части, которую использует приложение.
        """
        mid = int(match_id)
        hdr = _explorer(f"""
            select m.match_id, m.radiant_win, m.duration, m.radiant_score,
                   m.dire_score, m.picks_bans, l.name as league,
                   rt.name as radiant_name, dt.name as dire_name
            from matches m
            left join leagues l on l.leagueid = m.leagueid
            left join teams rt on rt.team_id = m.radiant_team_id
            left join teams dt on dt.team_id = m.dire_team_id
            where m.match_id = {mid}
        """, ttl=TTL_MATCH).get("rows") or []
        if not hdr:
            raise RuntimeError(f"матч {mid} не найден в базе OpenDota")
        h = hdr[0]

        players = _explorer(f"""
            select pm.player_slot, pm.hero_id, pm.kills, pm.deaths, pm.assists,
                   pm.gold_per_min, pm.xp_per_min,
                   pm.item_0, pm.item_1, pm.item_2, pm.item_3, pm.item_4,
                   pm.item_5, pm.item_neutral, np.name as player
            from player_matches pm
            left join notable_players np on np.account_id = pm.account_id
            where pm.match_id = {mid}
            order by pm.player_slot
        """, ttl=TTL_MATCH).get("rows") or []

        logs = {}
        try:
            for r in _explorer(f"""
                select pm.player_slot, pm.purchase_log
                from player_matches pm where pm.match_id = {mid}
            """, ttl=TTL_MATCH).get("rows") or []:
                logs[r["player_slot"]] = r.get("purchase_log") or []
        except Exception:  # noqa: BLE001 — без закупов матч всё равно показываем
            pass

        for p in players:
            p["isRadiant"] = p["player_slot"] < 128
            p["name"] = p.get("player")
            p["purchase_log"] = logs.get(p["player_slot"], [])

        return {
            "match_id": h["match_id"],
            "radiant_win": h["radiant_win"],
            "duration": h["duration"],
            "radiant_score": h["radiant_score"],
            "dire_score": h["dire_score"],
            "picks_bans": h.get("picks_bans") or [],
            "league": {"name": h.get("league")},
            "radiant_team": {"name": h.get("radiant_name")},
            "dire_team": {"name": h.get("dire_name")},
            "players": players,
        }

    # --- сборка героя по про-матчам ------------------------------------
    def hero_builds(self, hero_id, months=3, tier="top", enemy_ids=None):
        """Агрегированные закупы героя в турнирных матчах.

        Закупы каждой игры тянуть нельзя: 50 игр - 75 КБ, на слабом канале
        не дойдёт. Агрегируем в базе: для каждого предмета - в скольких
        играх куплен, сколько раз до рога, медианная минута первой покупки.
        Ответ - единицы килобайт. Возвращает (покупки, итоговые предметы).

        enemy_ids - сборка ПРОТИВ этих героев. Точных совпадений «пять этих
        врагов» в про-играх почти нет, поэтому берутся игры, где встречался
        хотя бы один из них, а вес игры - число совпавших врагов: игра против
        трёх из списка весит втрое больше, чем против одного. Доли считаются
        по весу (wgames / total_weight), число игр - по факту.
        """
        hid = int(hero_id)
        tiers = self.TIERS.get(tier, self.TIERS["top"])
        tier_sql = ", ".join(f"'{t}'" for t in tiers)
        bound = self.period_bound(months)
        period = self._period_sql(months)

        games_cte = f"""
            g as (
              select pm.match_id, pm.purchase_log, (pm.player_slot < 128) as radiant,
                     ((pm.player_slot < 128) = m.radiant_win) as won,
                     pm.item_0, pm.item_1, pm.item_2, pm.item_3, pm.item_4, pm.item_5
              from player_matches pm
              join matches m on m.match_id = pm.match_id
              join leagues l on l.leagueid = m.leagueid
              where pm.match_id >= {bound} and pm.hero_id = {hid} and {period}
                and l.tier in ({tier_sql})
            )"""
        enemy_ids = [int(e) for e in (enemy_ids or []) if int(e) != hid]
        if enemy_ids:
            ids = ", ".join(str(e) for e in enemy_ids)
            weight_cte = f"""
            w as (
              select g.match_id, count(pm2.hero_id) as weight
              from g join player_matches pm2 on pm2.match_id = g.match_id
                 and ((pm2.player_slot < 128) <> g.radiant)
                 and pm2.hero_id in ({ids})
              group by g.match_id
            ),
            gw as (select g.*, w.weight from g join w on w.match_id = g.match_id)"""
        else:
            weight_cte = "gw as (select g.*, 1 as weight from g)"

        purchases = _explorer(f"""
            with {games_cte},
            {weight_cte},
            p as (
              select gw.match_id, gw.weight, gw.won, (e->>'key') as key, (e->>'time')::int as t
              from gw, unnest(gw.purchase_log) e
              where gw.purchase_log is not null
            ),
            pk as (
              select match_id, key, max(weight) as weight, bool_or(won) as won,
                     min(t) filter (where t > 0) as first_time,
                     count(*) filter (where t <= 0) as start_cnt
              from p group by match_id, key
            )
            select key,
              count(*) as games,
              sum(weight) as wgames,
              sum(weight) filter (where won) as wwins,
              sum(weight) filter (where start_cnt > 0) as wstart,
              avg(start_cnt) filter (where start_cnt > 0) as start_per_game,
              percentile_cont(0.5) within group (order by first_time) as median_first,
              (select count(*) from gw) as total_games,
              (select sum(weight) from gw) as total_weight,
              (select count(*) from gw where won) as wins
            from pk group by key order by wgames desc
        """, ttl=TTL_TOURNAMENT).get("rows") or []

        final = _explorer(f"""
            with {games_cte},
            {weight_cte}
            select item_id, sum(weight) as wgames,
                   sum(weight) filter (where won) as wwins,
                   (select sum(weight) from gw) as total_weight
            from (select unnest(array[item_0, item_1, item_2, item_3, item_4, item_5])
                         as item_id, weight, won from gw) t
            where item_id > 0 group by item_id order by wgames desc limit 20
        """, ttl=TTL_TOURNAMENT).get("rows") or []
        return purchases, final

    # --- варды ------------------------------------------------------------
    def ward_cells(self, months=3, tier="top", kind="obs", side="radiant",
                   hero_id=None, t_from=-120, t_to=600):
        """Где ставят варды в турнирных матчах: клетки карты с числом вардов.

        Логи вардов у OpenDota есть у каждого разобранного матча
        (obs_log / sen_log: время в секундах от рога, x и y в клетках
        карты 64..192). Реплеи для этого не нужны. Агрегация в базе:
        клетка (округлённые x, y) -> сколько вардов и в скольких матчах.
        side - чьи варды: radiant / dire / both. Возвращает (строки,
        всего матчей за период).
        """
        col = "sen_log" if kind == "sen" else "obs_log"
        tiers = self.TIERS.get(tier, self.TIERS["top"])
        tier_sql = ", ".join(f"'{t}'" for t in tiers)
        bound = self.period_bound(months)
        side_sql = {"radiant": "and (pm.player_slot < 128)",
                    "dire": "and (pm.player_slot >= 128)"}.get(side, "")
        hero_sql = f"and pm.hero_id = {int(hero_id)}" if hero_id else ""
        rows = _explorer(f"""
            with pro as (
              select m.match_id from matches m join leagues l on l.leagueid = m.leagueid
              where {self._period_sql(months)} and l.tier in ({tier_sql})
            ),
            w as (
              select pm.match_id, (e->>'time')::int as t,
                     (e->>'x')::float as x, (e->>'y')::float as y
              from player_matches pm join pro on pro.match_id = pm.match_id, unnest(pm.{col}) e
              where pm.match_id >= {bound} and pm.{col} is not null {side_sql} {hero_sql}
            )
            select round(x)::int as cx, round(y)::int as cy, count(*) as n,
                   count(distinct match_id) as matches,
                   (select count(*) from pro) as total_matches
            from w where t >= {int(t_from)} and t < {int(t_to)}
            group by cx, cy order by n desc limit 600
        """, ttl=TTL_TOURNAMENT).get("rows") or []
        total = int(rows[0]["total_matches"]) if rows else 0
        return rows, total

    # --- аккаунт игрока ---------------------------------------------------
    # Данные публичные при включённой в Steam настройке «Открытая история
    # матчей»; без неё OpenDota о игроке ничего не знает.

    def player_profile(self, account_id):
        return get_json(f"{API}/players/{int(account_id)}", ttl=24 * 3600)

    def player_heroes(self, account_id, fast=False):
        """Игры и победы на каждом герое, с ним и против него.

        fast - не ждать сеть: из кэша (пусть устаревшего), иначе None и
        загрузка в фоне. Так работает подбор во время пика.
        """
        url = f"{API}/players/{int(account_id)}/heroes"
        return net.get_json_fast(url, ttl=3600) if fast else get_json(url, ttl=3600)

    def player_wl(self, account_id, fast=False):
        url = f"{API}/players/{int(account_id)}/wl"
        return net.get_json_fast(url, ttl=3600) if fast else get_json(url, ttl=3600)

    # --- прокачка и таланты -----------------------------------------------
    def hero_skills(self, hero_id, months=3, tier="top"):
        """Порядок прокачки героя в турнирных матчах: для каждой позиции
        (первый апгрейд, второй, ...) - какие способности брали и сколько раз,
        с победами. Таланты - те же способности, в массиве они на позициях
        уровней 10/15/20/25. Ответ - около килобайта."""
        hid = int(hero_id)
        tiers = self.TIERS.get(tier, self.TIERS["top"])
        tier_sql = ", ".join(f"'{t}'" for t in tiers)
        bound = self.period_bound(months)
        return _explorer(f"""
            with g as (
              select pm.ability_upgrades_arr as arr,
                     ((pm.player_slot < 128) = m.radiant_win) as won
              from player_matches pm
              join matches m on m.match_id = pm.match_id
              join leagues l on l.leagueid = m.leagueid
              where pm.match_id >= {bound} and pm.hero_id = {hid}
                and {self._period_sql(months)}
                and l.tier in ({tier_sql})
                and pm.ability_upgrades_arr is not null
            ),
            u as (
              select lvl, ability_id, won
              from g, unnest(g.arr) with ordinality as t(ability_id, lvl)
            )
            select lvl, ability_id, count(*) as n,
                   sum(case when won then 1 else 0 end) as wins,
                   (select count(*) from g) as games
            from u where lvl <= 25
            group by lvl, ability_id order by lvl, n desc
        """, ttl=TTL_TOURNAMENT).get("rows") or []

    def abilities(self):
        """Справочники способностей из снимка: ids, heroes, abilities."""
        return _load_abilities()

    # --- предметы ---------------------------------------------------------
    def items(self):
        """Справочник предметов: {внутреннее_имя: {id, dname, img, cost, qual}}.

        Снимок в приоритете: полный /constants/items весит 336 КБ и меняется
        раз в патч, гонять его по сети незачем.
        """
        snapshot = _load_items()
        if snapshot:
            return snapshot
        return get_json(f"{API}/constants/items", ttl=TTL_HEROES)

    # --- вспомогательное ---------------------------------------------------

    @staticmethod
    def picks_wins(hero_stat, bracket):
        """Пики и победы героя в выбранном ранге: (picks, wins).

        Подмены нет: если в ранге данных нет, возвращается (0, 0), и герой
        останется без базового винрейта. Молчаливый откат на общую публику
        показывал бы чужие числа под видом выбранного ранга.
        """
        if bracket == "all":
            return hero_stat.get("pub_pick") or 0, hero_stat.get("pub_win") or 0
        if bracket == "turbo":
            return hero_stat.get("turbo_picks") or 0, hero_stat.get("turbo_wins") or 0
        return hero_stat.get(f"{bracket}_pick") or 0, hero_stat.get(f"{bracket}_win") or 0

    def available_brackets(self):
        """Только те ранги, по которым у OpenDota реально есть данные.

        Поля 1_pick..8_pick заполняются не всегда: на момент написания
        Immortal (8) пустой, а pro_pick по всем героям даёт около тысячи игр —
        слишком мало, чтобы показывать это как отдельный режим.
        """
        stats = self.hero_stats()
        out = []
        for key, label in self.brackets:
            total = sum(self.picks_wins(h, key)[0] for h in stats)
            if total > 0:
                out.append({"key": key, "label": label, "games": total})
        return out
