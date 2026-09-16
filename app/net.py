# -*- coding: utf-8 -*-
"""HTTP-запросы с кэшем на диск.

У системного python на macOS часто не настроены корневые сертификаты, поэтому
запрос сначала пробуется через urllib, а при ошибке SSL — через curl.
Кэш нужен, чтобы не упираться в лимит OpenDota (60 запросов в минуту).
"""
import gzip
import hashlib
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from paths import CACHE_DIR  # noqa: E402 - где хранить кэш, решает paths.py

UA = "pickline/1.0 (personal use)"

# Дочерний curl на Windows без этого флага открывает себе чёрное окно
# консоли, когда сам exe собран без консоли: у пользователя на каждый
# запрос выскакивал «пустой PowerShell» на долю секунды
SUBPROCESS_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

_ssl_context = None
_use_curl = False


def _context():
    """SSL-контекст с сертификатами certifi, если он установлен.

    Без certifi на macOS берётся системный файл /etc/ssl/cert.pem: у python
    из коробки там пустое хранилище, и любой https падал на проверке
    сертификата. Раньше это спасал откат на curl, но постоянное соединение
    (STRATZ) через curl не сделать.
    """
    global _ssl_context
    if _ssl_context is None:
        try:
            import certifi
            _ssl_context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            system_pem = "/etc/ssl/cert.pem"
            if os.path.exists(system_pem):
                _ssl_context = ssl.create_default_context(cafile=system_pem)
            else:
                _ssl_context = ssl.create_default_context()
    return _ssl_context


def _cache_path(url):
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return os.path.join(CACHE_DIR, key + ".json")


def _read_cache(url, ttl):
    path = _cache_path(url)
    if not os.path.exists(path):
        return None
    if ttl is not None and time.time() - os.path.getmtime(path) > ttl:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(url, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _cache_path(url) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _cache_path(url))


class HttpStatusError(RuntimeError):
    """Сервер ответил, но кодом ошибки. Отдельный тип, чтобы 429/5xx повторять."""

    def __init__(self, code, url):
        self.code = code
        hint = {429: "лимит запросов, подождите минуту", 502: "сервер недоступен",
                503: "сервер недоступен", 504: "сервер не ответил вовремя"}.get(code, "")
        super().__init__(f"HTTP {code}" + (f" ({hint})" if hint else "") + f" от {url.split('/api/')[0]}")


def _parse_json(raw_text, url):
    # Пустой ответ или HTML-страница ошибки вместо JSON - частая форма
    # отказа OpenDota. Сообщение «Expecting value: line 1 column 1» ничего
    # не говорит пользователю; говорим, что именно пришло.
    text = raw_text.strip()
    if not text:
        raise RuntimeError(f"пустой ответ от {url.split('/api/')[0]}")
    if text[0] not in "[{":
        raise RuntimeError(f"не JSON, а «{text[:60]}…» от {url.split('/api/')[0]} "
                           "(обычно страница ошибки или лимита запросов)")
    return json.loads(text)


def _fetch_urllib(url, timeout):
    # Сжатие обязательно: heroStats весит 161 КБ, а со сжатием 33 КБ.
    # На нестабильных каналах большой ответ просто не доходит — соединение
    # рвётся на середине, и приложение выглядит зависшим.
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_context()) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise HttpStatusError(e.code, url) from None
    return _parse_json(raw.decode("utf-8", "replace"), url)


def _fetch_curl(url, timeout):
    # -f: страница ошибки 429/5xx не должна попадать в разбор JSON
    out = subprocess.run(
        ["curl", "-sS", "-L", "-f", "--compressed", "--max-time", str(timeout),
         "-w", "\n%{http_code}",
         "-H", f"User-Agent: {UA}", url],
        capture_output=True, text=True, **SUBPROCESS_FLAGS,
    )
    body, _, code = out.stdout.rpartition("\n")
    if out.returncode == 22 and code.strip().isdigit():
        raise HttpStatusError(int(code.strip()), url)
    if out.returncode != 0:
        raise RuntimeError(f"curl вернул код {out.returncode}: {out.stderr.strip()[:200]}")
    return _parse_json(body, url)


# после таких кодов вторая попытка через паузу обычно проходит
RETRY_CODES = {429, 500, 502, 503, 504}


def get_json(url, ttl=3600, timeout=45, stale_ok=True):
    """Забирает JSON по url. ttl — сколько секунд кэш считается свежим.

    Если сеть недоступна, а в кэше есть просроченная копия, возвращается она
    (stale_ok): для Pickline вчерашние винрейты лучше, чем ошибка.
    Одна повторная попытка через пару секунд - и на 429/5xx, и на обрыв
    соединения (у пользователя рвалось TLS-рукопожатие с OpenDota на
    секунды - с повтором это не ошибка, а задержка).
    """
    global _use_curl
    cached = _read_cache(url, ttl)
    if cached is not None:
        return cached

    error = None
    order = (_fetch_curl, _fetch_urllib) if _use_curl else (_fetch_urllib, _fetch_curl)
    for attempt in range(2):
        for fetch in order:
            try:
                data = fetch(url, timeout)
                _use_curl = fetch is _fetch_curl
                _write_cache(url, data)
                _last_error.pop(url, None)
                return data
            except HttpStatusError as e:
                # сервер ответил - вторым способом ответ будет тот же
                error = e
                break
            except Exception as e:  # noqa: BLE001 — сбой соединения: пробуем запасной путь
                error = e
        # HTTP-ошибка не из списка повторяемых (404, 400) - повторять незачем
        if isinstance(error, HttpStatusError) and error.code not in RETRY_CODES:
            break
        if attempt == 0:
            time.sleep(2.5)

    if stale_ok:
        stale = _read_cache(url, ttl=None)
        if stale is not None:
            # Устаревшая копия вместо ошибки - но не молча: иначе вкладка
            # «Про-матчи» сутки показывала вчерашний список, и по ней было
            # не понять, что OpenDota не отвечает. Причину помним и отдаём
            # через status(), в лог - только при смене текста ошибки.
            text = describe_error(error)
            if _last_error.get(url) != text:
                sys.stderr.write(f"  сеть: {text}; показана копия из кэша для {short_url(url)}\n")
            _last_error[url] = text
            return stale
    raise RuntimeError(f"нет связи с {short_url(url)}: {describe_error(error)}")


def short_url(url):
    """Адрес без параметров: у /explorer в них SQL на два экрана."""
    return url.split("?", 1)[0]


def describe_error(error):
    """Человеческое описание сетевой ошибки вместо кода curl."""
    text = str(error)
    low = text.lower()
    if "schannel" in low or "ssl/tls" in low or "handshake" in low or "ssl:" in low:
        return "SSL/TLS-соединение не установилось (обрыв сети или VPN)"
    if "timed out" in low or "timeout" in low:
        return "сервер не ответил вовремя (медленный канал)"
    if "could not resolve" in low or "getaddrinfo" in low or "name or service" in low:
        return "не удалось найти адрес сервера (нет DNS или сети)"
    if "connection refused" in low or "connection reset" in low or "remotedisconnected" in low:
        return "соединение оборвано"
    return text[:200]


def get_json_fast(url, ttl=3600, max_age=7 * 24 * 3600):
    """То же, но никогда не ждёт сеть.

    Свежий кэш - вернуть. Иначе запустить обновление в фоне и вернуть
    устаревшую копию, если она не старше max_age; если копии нет - None.
    Для всего, что участвует в подборе во время пика: там ждать нельзя.
    """
    fresh = _read_cache(url, ttl)
    if fresh is not None:
        return fresh
    refresh_in_background(url, ttl)
    return _read_cache(url, max_age)


# --- памятка вычислений ------------------------------------------------------
# Результат дорогой функции (турнирная статистика собирается несколькими
# запросами к базе OpenDota) кладётся в тот же кэш под своим ключом, чтобы
# подбор брал готовое, а пересчёт шёл в фоне.

def memo_read(key, max_age=None):
    return _read_cache("memo://" + key, max_age)


def memo_write(key, data):
    _write_cache("memo://" + key, data)


def compute_in_background(key, fn):
    """Один поток на ключ: считает fn() и кладёт результат в памятку."""
    url = "memo://" + key
    with _refresh_lock:
        if url in _refreshing:
            return None
        _refreshing.add(url)

    def worker():
        try:
            memo_write(key, fn())
        except Exception:  # noqa: BLE001 — фоновый пересчёт не критичен
            pass
        finally:
            with _refresh_lock:
                _refreshing.discard(url)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


# url -> текст последней ошибки сети, из-за которой отдана копия из кэша
_last_error = {}


def status(url):
    """Свежесть данных по url для интерфейса.

    Возвращает {"updated_at": время последней удачной загрузки (unix) или
    None, "age": секунд с тех пор, "error": текст ошибки, если последняя
    попытка обновить не удалась}. Честность важнее гладкой картинки:
    пользователь должен видеть, что смотрит на вчерашнюю копию.
    """
    path = _cache_path(url)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    return {
        "updated_at": int(mtime) if mtime else None,
        "age": int(time.time() - mtime) if mtime else None,
        "error": _last_error.get(url),
    }


def forget(url):
    """Удаляет из кэша один ответ - когда выяснилось, что он был с ошибкой."""
    try:
        os.remove(_cache_path(url))
        return True
    except OSError:
        return False


def cached(url, ttl=3600):
    """Отдаёт данные из кэша, если они свежие. В сеть не ходит.

    Нужно, чтобы решать, идти ли в сеть вообще: на медленном канале
    поход за данными стоит десятки секунд, и лучше сперва посмотреть,
    нет ли готового ответа под рукой.
    """
    return _read_cache(url, ttl)


_refreshing = set()
_refresh_lock = threading.Lock()


# url -> когда фоновое обновление в последний раз не удалось
_refresh_failed_at = {}
REFRESH_RETRY_AFTER = 15 * 60


def refresh_in_background(url, ttl=3600):
    """Обновляет кэш по-тихому, не задерживая ответ пользователю.

    На один url — не больше одного потока. Без этой защиты каждый запрос
    страницы плодил десяток одновременных скачиваний одного и того же:
    hero_stats() зовут отовсюду, и пока кэш пуст, все они просили обновление.
    На медленном канале такой шторм душил сам себя.
    """
    with _refresh_lock:
        if url in _refreshing:
            return None
        # после неудачи - пауза: на медленном канале heroStats не докачивался
        # за 45 с, и попытка повторялась каждые несколько минут впустую
        if time.time() - _refresh_failed_at.get(url, 0) < REFRESH_RETRY_AFTER:
            return None
        _refreshing.add(url)

    def worker():
        try:
            get_json(url, ttl=0, stale_ok=False)
            _refresh_failed_at.pop(url, None)
        except Exception as e:  # noqa: BLE001 — фоновое обновление не критично
            _refresh_failed_at[url] = time.time()
            text = str(e)[:200]
            if _last_error.get(url) != text:
                sys.stderr.write(f"  сеть: фоновое обновление не удалось ({text}); "
                                 f"следующая попытка через {REFRESH_RETRY_AFTER // 60} мин, "
                                 f"пока копия из кэша: {url}\n")
            _last_error[url] = text
        finally:
            with _refresh_lock:
                _refreshing.discard(url)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def _download_urllib(url, tmp, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_context()) as r:
        data = r.read()
    if not data:
        raise RuntimeError("пустой ответ")
    with open(tmp, "wb") as f:
        f.write(data)


def _download_curl(url, tmp, timeout):
    out = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", str(timeout),
         "-H", f"User-Agent: {UA}", "-o", tmp, url],
        capture_output=True, text=True, **SUBPROCESS_FLAGS,
    )
    if out.returncode != 0:
        raise RuntimeError(f"curl: {out.stderr.strip()[:160] or 'код ' + str(out.returncode)}")
    if not os.path.exists(tmp):
        raise RuntimeError("curl не создал файл")


def download(url, dest, timeout=20):
    """Скачивает файл (картинку) в dest.

    Возвращает (True, None) при успехе или (False, причина). Причина нужна
    обязательно: молчаливый False оставлял пользователя с «0 из 127» и без
    единой подсказки, что произошло.

    Способ скачивания липкий, как в get_json: если urllib однажды не смог,
    дальше сразу идём через curl. Иначе на канале, где urllib подвисает,
    127 портретов ждали бы по таймауту каждый.
    """
    global _use_curl
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return True, None
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"

    errors = []
    order = (_download_curl, _download_urllib) if _use_curl else (_download_urllib, _download_curl)
    for fetch in order:
        try:
            fetch(url, tmp, timeout)
            if os.path.getsize(tmp) == 0:
                raise RuntimeError("скачан пустой файл")
            _use_curl = fetch is _download_curl
            os.replace(tmp, dest)
            return True, None
        except Exception as e:  # noqa: BLE001 — пробуем второй способ
            errors.append(f"{fetch.__name__.replace('_download_', '')}: "
                          f"{type(e).__name__}: {str(e)[:120]}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    return False, "; ".join(errors)


def clear_cache():
    """Удаляет кэш целиком — на случай, когда нужны свежие данные немедленно."""
    if not os.path.isdir(CACHE_DIR):
        return 0
    removed = 0
    for name in os.listdir(CACHE_DIR):
        if name.endswith(".json"):
            os.remove(os.path.join(CACHE_DIR, name))
            removed += 1
    return removed
