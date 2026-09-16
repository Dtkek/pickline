# -*- coding: utf-8 -*-
"""Чтение подписей ролей в верхней полосе драфта.

В рейтинге с выбором ролей игра подписывает роль под именем каждого игрока
своей команды: «Лёгкая», «Центр», «Сложная», «Поддержка», «Полная
поддержка» (у врагов подписей нет). Подписи - фиксированные картинки, и
ищутся так же, как портреты героев: сопоставлением с эталонами. OCR не
нужен.

Эталоны (app/data/roles/*.png) вырезаны из скриншота, сжатого до ширины
1280, поэтому и живой кадр перед поиском приводится к этому масштабу:
«размытое с размытым» сопоставляется лучше, чем чёткое с размытым.
Масштаб берётся от ширины портрета, найденной распознавателем героев
(портрет - 6,4% ширины 16:9-экрана), а без неё - от ширины кадра.

Пять подписей слева направо - это слоты 0..4 своей команды; номер своего
слота сообщает игра через GSI (player.team_slot), и подпись под ним - ваша
роль. Соответствие: Лёгкая - 1, Центр - 2, Сложная - 3, Поддержка - 4,
Полная поддержка - 5.

Проверено на двух сжатых скриншотах с разных сторон (Dire в фазе пика,
Radiant в планировании): правильная подпись 0,82-0,87, лучшая чужая -
0,62. На живом экране в полном разрешении - не проверено.
"""
import glob
import os

import numpy as np

from paths import BUNDLED_DATA

try:
    import cv2
    AVAILABLE = True
except ImportError:  # pragma: no cover
    cv2 = None
    AVAILABLE = False

TEMPLATES_DIR = os.path.join(BUNDLED_DATA, "roles")

# ширина кадра, к которой приведены эталоны, и ширина портрета в ней
BASE_WIDTH = 1280
PORTRAIT_BASE = 82
# шаг между слотами и отступ первого слота от центра экрана в базовом
# масштабе (замерено по скриншоту 1280×720)
SLOT_STEP = 83
SLOT_FROM_CENTER = 128

# где искать подписи: доли высоты 16:9-кадра (без найденных портретов)
LABEL_BAND = (0.075, 0.135)
# с найденными портретами: полоса относительно верха портрета в его полных
# высотах (распознаватель отдаёт верхние 45% портрета)
PORTRAIT_TOP_PART = 0.45
LABEL_BAND_BY_PORTRAIT = (1.15, 2.05)

# порог совпадения и минимальный отрыв от следующей подписи в том же месте
SCORE_MIN = 0.66
MARGIN_MIN = 0.06

NAMES = {1: "Лёгкая", 2: "Центр", 3: "Сложная", 4: "Поддержка", 5: "Полная поддержка"}


def _read_gray(path):
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)


class RoleReader:
    def __init__(self):
        self.templates = {}
        if not AVAILABLE:
            return
        for path in sorted(glob.glob(os.path.join(TEMPLATES_DIR, "*.png"))):
            pos = int(os.path.basename(path).split("_", 1)[0])
            img = _read_gray(path)
            if img is not None:
                self.templates[pos] = img

    @property
    def ready(self):
        return AVAILABLE and len(self.templates) == 5

    def read(self, frame, boxes=None, template_width=None, region=None):
        """Подписи ролей в кадре.

        boxes - найденные портреты [x, y, w, h] (для полосы поиска и
        масштаба), template_width - ширина портрета в кадре, region -
        область захвата в долях монитора (left, top, width, height).
        Возвращает {"labels": [{"position", "name", "score", "x", "cx"}...],
        "scale": s, "band": (y0, y1)} - подписи отсортированы по x.
        """
        if not self.ready:
            return {"labels": [], "scale": None, "band": None}
        fh, fw = frame.shape[:2]
        region = region or (0.0, 0.0, 1.0, 1.0)
        _, top_frac, w_frac, h_frac = region

        # масштаб: от портрета, иначе от ширины монитора
        if template_width:
            scale = template_width / PORTRAIT_BASE
        else:
            scale = (fw / max(w_frac, 1e-6)) / BASE_WIDTH

        # полоса поиска
        if boxes:
            top = min(b[1] for b in boxes)
            ph = max(b[3] for b in boxes)
            full = ph / PORTRAIT_TOP_PART
            y0 = int(top + full * LABEL_BAND_BY_PORTRAIT[0])
            y1 = int(top + full * LABEL_BAND_BY_PORTRAIT[1])
        else:
            monitor_h = fh / max(h_frac, 1e-6)
            y0 = int((LABEL_BAND[0] - top_frac) * monitor_h)
            y1 = int((LABEL_BAND[1] - top_frac) * monitor_h)
        y0, y1 = max(0, y0), min(fh, y1)
        if y1 - y0 < 6:
            return {"labels": [], "scale": scale, "band": (y0, y1)}

        gray = cv2.cvtColor(frame[y0:y1], cv2.COLOR_BGR2GRAY)
        # к базовому масштабу эталонов
        bw = max(8, int(round(gray.shape[1] / scale)))
        bh = max(4, int(round(gray.shape[0] / scale)))
        small = cv2.resize(gray, (bw, bh), interpolation=cv2.INTER_AREA)

        hits = []
        for pos, tpl in self.templates.items():
            th, tw = tpl.shape[:2]
            if bh < th or bw < tw:
                continue
            res = cv2.matchTemplate(small, tpl, cv2.TM_CCOEFF_NORMED)
            # все локальные максимумы выше порога, без наложений одной подписи
            r = res.copy()
            for _ in range(6):
                _, mx, _, loc = cv2.minMaxLoc(r)
                if mx < SCORE_MIN:
                    break
                hits.append({"position": pos, "name": NAMES[pos], "score": float(mx),
                             "x": loc[0], "w": tw, "y": loc[1], "h": th})
                r[:, max(0, loc[0] - tw // 2):loc[0] + tw // 2] = -1

        # «Поддержка» лежит целиком внутри «Полная поддержка» и находится
        # там с высоким баллом, а длинная подпись из-за размытия может
        # набрать меньше. Поэтому не по баллу: если слева от «Поддержка»
        # в той же строке есть ещё слово - это «Полная поддержка»
        for h in hits:
            if h["position"] == 4 and _word_left_of(small, h):
                h["position"], h["name"] = 5, NAMES[5]
        hits.sort(key=lambda h: -h["score"])
        kept = []
        for h in hits:
            if any(_overlap(h, k) > 0.4 for k in kept):
                continue
            kept.append(h)
        kept.sort(key=lambda h: h["x"])

        labels = []
        for h in kept:
            # обратно в координаты кадра
            x = int(h["x"] * scale)
            w = int(h["w"] * scale)
            labels.append({"position": h["position"], "name": h["name"],
                           "score": round(h["score"], 3), "x": x, "cx": x + w // 2, "w": w})
        return {"labels": labels, "scale": round(scale, 3), "band": (y0, y1)}


# ширина слова «Полная » перед «поддержка» в базовом масштабе и доля
# светлых пикселей, начиная с которой считаем, что слово там есть
WORD_LEFT_WIDTH = 40
WORD_LEFT_GAP = 4
TEXT_BRIGHT = 110
TEXT_FILL_MIN = 0.06


def _word_left_of(small, hit):
    """Есть ли текст слева от подписи в той же строке (для «Полная поддержка»)."""
    x1 = hit["x"] - WORD_LEFT_GAP
    x0 = x1 - WORD_LEFT_WIDTH
    if x0 < 0:
        return False
    y0, y1 = hit["y"], hit["y"] + hit["h"]
    patch = small[y0:y1, x0:x1]
    if patch.size == 0:
        return False
    return float((patch > TEXT_BRIGHT).mean()) >= TEXT_FILL_MIN


def _overlap(a, b):
    """Доля перекрытия по x относительно более короткой подписи."""
    left, right = max(a["x"], b["x"]), min(a["x"] + a["w"], b["x"] + b["w"])
    if right <= left:
        return 0.0
    return (right - left) / min(a["w"], b["w"])


def slot_index(cx, side, frame_w, scale):
    """Номер слота 0..4 по центру подписи, если игра занимает кадр целиком.

    Слоты стоят симметрично от центра экрана: Radiant слева, Dire справа.
    Вне полного экрана (игра окном в углу) центр не совпадает - тогда
    номера считаются по порядку найденных подписей, см. assign_slots.
    """
    center = frame_w / 2
    step = SLOT_STEP * scale
    if side == "dire":
        i = (cx - (center + SLOT_FROM_CENTER * scale)) / step
    else:
        i = 4 - ((center - SLOT_FROM_CENTER * scale) - cx) / step
    return int(round(i))


def assign_slots(labels, side, frame_w, scale):
    """Раскладывает найденные подписи по слотам 0..4: [позиция или None]*5.

    Если подписей пять - по порядку. Иначе - по геометрии от центра
    экрана, а если она даёт бессмыслицу (окно не на весь экран) - по
    шагу от первой найденной, считая её слотом 0.
    """
    slots = [None] * 5
    if not labels:
        return slots
    if len(labels) >= 5:
        for i, lab in enumerate(labels[:5]):
            slots[i] = lab["position"]
        return slots
    idx = [slot_index(lab["cx"], side, frame_w, scale) for lab in labels]
    if all(0 <= i <= 4 for i in idx) and len(set(idx)) == len(idx):
        for i, lab in zip(idx, labels):
            slots[i] = lab["position"]
        return slots
    step = SLOT_STEP * scale
    x0 = labels[0]["cx"]
    for lab in labels:
        i = int(round((lab["cx"] - x0) / step))
        if 0 <= i <= 4 and slots[i] is None:
            slots[i] = lab["position"]
    return slots
