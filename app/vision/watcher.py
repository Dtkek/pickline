# -*- coding: utf-8 -*-
"""Фоновое слежение за экраном: раз в несколько секунд обновляет список героев.

Работает в отдельном потоке, состояние читается из веб-интерфейса опросом.
Логика простая: сначала калибровка (полный поиск), дальше — быстрая
классификация найденных прямоугольников. Если уверенных попаданий стало
заметно меньше, калибровка повторяется: значит, картинка на экране уехала.
"""
import threading
import time

from vision import capture, recognize, roles


class ScreenWatcher:
    def __init__(self, hero_names=None, source=None, interval=3.0, monitor=1,
                 region=None):
        self.hero_names = hero_names or {}
        self.source = source
        self.recognizer = recognize.Recognizer(self.hero_names)
        self.role_reader = roles.RoleReader()
        self.interval = interval
        # функция без аргументов: True, если сейчас сканировать не нужно
        # (например, игра через GSI сообщила, что матч уже идёт)
        self.pause_check = None
        # функция без аргументов: "radiant" / "dire" / None - своя сторона
        # по данным игры (GSI); без неё сторона берётся по подписям ролей
        self.team_hint = None
        self.monitor = monitor
        # по умолчанию верхняя половина экрана: там идёт драфт
        self.region = region or (0.0, 0.0, 1.0, 0.55)

        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # последний захваченный кадр в JPEG — чтобы в интерфейсе было видно,
        # что именно попало в объектив: игра, браузер или чёрный экран
        self._last_jpeg = None
        self._state = {
            "running": False,
            "heroes": [],
            "boxes": [],
            "template_width": None,
            "last_scan": None,
            "last_error": None,
            "scans": 0,
            "mode": "ожидание",
            "frame_brightness": None,
            "frame_hint": None,
            "frame_width": None,
            # подписи ролей своей команды: {"side", "slots": [поз|None]*5,
            # "labels": [...], "taken": [слоты с портретами]} или None
            "roles": None,
        }

    # --- состояние --------------------------------------------------------
    def state(self):
        with self._lock:
            return dict(self._state)

    def last_frame_jpeg(self):
        with self._lock:
            return self._last_jpeg

    def _grab(self):
        """Захват кадра + сохранение превью и оценка, что в нём вообще есть."""
        import cv2
        frame = capture.grab(self.monitor, self.region)
        h, w = frame.shape[:2]
        k = min(1.0, 640 / w)
        small = cv2.resize(frame, (int(w * k), int(h * k)),
                           interpolation=cv2.INTER_AREA) if k < 1 else frame
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])

        brightness = float(frame.mean())
        hint = None
        if brightness < 6:
            hint = ("кадр почти чёрный. Обычно это Dota в режиме «Полноэкранный»: "
                    "захват экрана его не видит. Переключите игру в "
                    "«Оконный без рамки» (настройки → видео)")
        with self._lock:
            if ok:
                self._last_jpeg = buf.tobytes()
            self._state["frame_brightness"] = round(brightness, 1)
            self._state["frame_hint"] = hint
            # ширина кадра нужна интерфейсу, чтобы делить стороны по центру
            # экрана, а не по середине между найденными портретами
            self._state["frame_width"] = w
        return frame

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    # --- эталоны ----------------------------------------------------------
    def reload_templates(self):
        """Перечитывает портреты с диска.

        Нужно после докачки: распознаватель загружает эталоны при создании,
        и без перечитывания слежение не заработало бы до перезапуска сервера.
        """
        self.recognizer = recognize.Recognizer(self.hero_names)
        return self.recognizer.ready

    def _download_templates(self):
        """Качает недостающие портреты, показывая прогресс в состоянии."""
        from vision import icons
        if self.source is None:
            self._set(last_error="нет источника данных для скачивания портретов")
            return False

        def progress(done, total):
            self._set(mode=f"скачиваю портреты героев: {done} из {total}")

        self._set(mode="скачиваю портреты героев…", last_error=None)
        try:
            got, failed, total, reason = icons.ensure_icons(self.source, progress)
        except Exception as e:  # noqa: BLE001
            self._set(last_error=f"не удалось скачать портреты: {e}")
            return False

        ready = self.reload_templates()
        if failed:
            # причина обязательна: раньше здесь молча получалось «0 из 127»
            self._set(last_error=(
                f"портреты: скачано {got}, не удалось {failed} из {total}. "
                f"Последняя ошибка: {reason}"))
        if not ready:
            self._set(mode="остановлено: нет эталонов героев")
        return ready

    # --- управление -------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        if not capture.available():
            self._set(last_error="не установлены пакеты для захвата экрана")
            return False

        # эталонов может не быть: в репозиторий они не входят. Не ругаемся,
        # а качаем сами — пользователю незачем знать про это устройство.
        if not self.recognizer.ready:
            self.reload_templates()

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._set(running=True, last_error=None)
        return True

    def _run(self):
        if not self.recognizer.ready and not self._download_templates():
            self._set(running=False, mode="остановлено")
            return
        self._loop()

    def stop(self):
        self._stop.set()
        self._set(running=False, mode="остановлено")

    def configure(self, monitor=None, interval=None, region=None):
        if monitor is not None:
            self.monitor = int(monitor)
        if interval is not None:
            self.interval = max(0.5, float(interval))
        if region is not None:
            self.region = tuple(region)
        # настройки поменялись — прежние прямоугольники больше не годятся
        self._set(boxes=[], template_width=None)

    def scan_image(self, image_bytes):
        """Распознаёт героев на присланном изображении, минуя захват экрана.

        Нужно, чтобы отделить «не захватывает» от «не распознаёт»:
        если на скриншоте герои находятся, а вживую нет — виноват захват.
        """
        import cv2
        import numpy as np
        if not self.recognizer.ready:
            self.reload_templates()
        if not self.recognizer.ready and not self._download_templates():
            return self.state()
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            self._set(last_error="не удалось прочитать изображение")
            return self.state()
        h, w = frame.shape[:2]
        k = min(1.0, 640 / w)
        small = cv2.resize(frame, (int(w * k), int(h * k)),
                           interpolation=cv2.INTER_AREA) if k < 1 else frame
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        with self._lock:
            if ok:
                self._last_jpeg = buf.tobytes()
            self._state["frame_brightness"] = round(float(frame.mean()), 1)
            self._state["frame_hint"] = None
            self._state["frame_width"] = w
        hits, width = self.recognizer.scan(frame)
        self._apply(hits, width, mode=f"проверка на файле {w}×{h}")
        # у файла область захвата - весь кадр, а не настроенная полоса
        region, self.region = self.region, (0.0, 0.0, 1.0, 1.0)
        try:
            self._read_roles(frame, hits, width if hits else None)
        finally:
            self.region = region
        self._set(last_error=None)
        return self.state()

    def scan_once(self):
        """Разовый полный поиск — для кнопки «сканировать сейчас»."""
        if not self.recognizer.ready:
            self.reload_templates()
        if not self.recognizer.ready and not self._download_templates():
            return self.state()
        frame = self._grab()
        hits, width = self.recognizer.scan(frame)
        self._apply(hits, width, mode="разовый поиск")
        self._read_roles(frame, hits, width if hits else None)
        return self.state()

    # --- подписи ролей -----------------------------------------------------
    def _read_roles(self, frame, hits, width):
        """Подписи ролей своей команды в кадре -> в состояние.

        Сторона - от игры (GSI), а без неё - та, где подписи нашлись:
        у врагов их нет. Слоты с портретами - чтобы знать, какие роли
        уже заняты. Ошибка чтения не должна ронять слежение.
        """
        if not self.role_reader.ready:
            return
        try:
            boxes = [h["box"] for h in hits] if hits else None
            # width - ширина портрета, которой можно верить: измеренная по
            # найденным портретам сейчас или при калибровке; без попаданий
            # распознаватель отдаёт последнюю пробную ширину, её не передают
            out = self.role_reader.read(frame, boxes=boxes, template_width=width,
                                        region=self.region)
            labels = out["labels"]
            fw = frame.shape[1]
            side = self.team_hint() if self.team_hint else None
            if side not in ("radiant", "dire"):
                if not labels:
                    self._set(roles=None)
                    return
                mean_cx = sum(l["cx"] for l in labels) / len(labels)
                side = "dire" if mean_cx > fw / 2 else "radiant"
            if not labels:
                self._set(roles={"side": side, "slots": [None] * 5, "labels": [],
                                 "taken": [], "scale": out["scale"]})
                return
            slots = roles.assign_slots(labels, side, fw, out["scale"])
            # портреты своей стороны -> номера занятых слотов
            taken = []
            for h in hits or []:
                x, y, w, _ = h["box"]
                cx = x + w / 2
                on_my_side = (cx > fw / 2) == (side == "dire")
                if not on_my_side:
                    continue
                i = roles.slot_index(cx, side, fw, out["scale"])
                if 0 <= i <= 4:
                    taken.append(i)
            self._set(roles={"side": side, "slots": slots, "labels": labels,
                             "taken": sorted(set(taken)), "scale": out["scale"]})
        except Exception as e:  # noqa: BLE001 - подписи вторичны, героев это не касается
            self._set(roles=None, last_error=f"подписи ролей: {e}")

    # --- внутреннее -------------------------------------------------------
    def _apply(self, hits, width, mode):
        seen, heroes = set(), []
        for h in sorted(hits, key=lambda x: x["box"][0]):
            if h["hero_id"] in seen:
                continue
            seen.add(h["hero_id"])
            heroes.append(h)
        self._set(
            heroes=heroes,
            boxes=[h["box"] for h in heroes],
            template_width=width or self._state.get("template_width"),
            last_scan=time.time(),
            mode=mode,
            scans=self._state.get("scans", 0) + 1,
        )

    # Страховочный полный поиск, даже если кадр «не менялся»: на случай,
    # если детектор изменений что-то проглядел. Редкий, потому дешёвый.
    SAFETY_RESCAN = 30.0

    # Кадр считается изменившимся, если хотя бы такая доля пикселей
    # уменьшенной копии сдвинулась по яркости заметно (больше PIXEL_DELTA).
    # Именно доля, а не среднее: пять новых портретов в углу почти не двигают
    # среднее по кадру, и первая версия проверки их пропускала.
    CHANGE_FRACTION = 0.004
    PIXEL_DELTA = 20

    @staticmethod
    def _thumb(frame, band=None):
        """Крошечная серая копия для сравнения «изменилось ли что-то».

        band - (y0, y1): после калибровки сравниваем только полосу
        с портретами, а середину полосы (там таймер, тикающий каждую
        секунду) вырезаем - иначе каждый тик считался бы изменением.
        """
        import cv2
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if band:
            y0, y1 = band
            g = g[max(0, y0):max(y0 + 1, y1)].copy()
            w = g.shape[1]
            g[:, int(w * 0.42):int(w * 0.58)] = 0
        return cv2.resize(g, (96, 54), interpolation=cv2.INTER_AREA).astype("float32")

    def _loop(self):
        """Изменился кадр - ищем заново; не изменился - ничего не делаем.

        Раньше здесь было слежение по известным прямоугольникам, и оно
        создало дыру: изменение кадра «не по расписанию» уходило в слежение
        (которое видит только старые места), а потом кадр был статичен,
        и полный поиск не наступал никогда. Враги, выбравшие героев после
        калибровки, терялись. Теперь единственный триггер поиска - изменение.
        """
        last_full = 0.0
        last_thumb = None
        known_width = None
        band = None
        while not self._stop.is_set():
            try:
                # Если игра сама говорит, что драфт кончился и матч идёт,
                # экран трогать незачем: ни захвата, ни поиска, ни нагрузки.
                if self.pause_check and self.pause_check():
                    self._set(mode="пауза: матч идёт, драфт закончен", last_error=None)
                    self._stop.wait(self.interval)
                    continue

                frame = self._grab()
                thumb = self._thumb(frame, band)
                if last_thumb is None or last_thumb.shape != thumb.shape:
                    changed = True
                else:
                    moved = (abs(thumb - last_thumb) > self.PIXEL_DELTA).mean()
                    changed = float(moved) > self.CHANGE_FRACTION
                safety_due = (time.time() - last_full) >= self.SAFETY_RESCAN

                if changed or safety_due:
                    # масштаб интерфейса за драфт не меняется: после первой
                    # калибровки ищем только вокруг известной ширины портрета
                    hits, width = self.recognizer.scan(frame, hint_width=known_width)
                    if not hits and known_width:
                        hits, width = self.recognizer.scan(frame)
                    self._apply(hits, width, mode="поиск" if changed else "страховочный поиск")
                    self._read_roles(frame, hits, width if hits else known_width)
                    if hits:
                        known_width = width
                        tops = [h["box"][1] for h in hits]
                        bottoms = [h["box"][1] + h["box"][3] for h in hits]
                        pad = max(8, int((max(bottoms) - min(tops)) * 0.3))
                        band = (min(tops) - pad, max(bottoms) + pad)
                        # полоса поменялась - следующий кадр сравниваем уже по ней
                        thumb = self._thumb(frame, band)
                    last_full = time.time()
                else:
                    self._set(mode="слежение: кадр без изменений", last_error=None)
                last_thumb = thumb
                self._set(last_error=None)
            except Exception as e:  # noqa: BLE001 — поток не должен умирать
                self._set(last_error=str(e))
            self._stop.wait(self.interval)
        self._set(running=False)
