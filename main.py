"""Fish bot для World of Warcraft.

Цикл: заброс (горячая клавиша) -> ожидание поклёвки -> подсечка
(та же/другая горячая клавиша или клик мышью) -> случайная задержка -> заброс.

Поклёвка определяется:
  * detect_mode = "sound" — по всплеску громкости звука процесса игры (Wow.exe),
    читается только звук игры, Discord/браузер не мешают;
  * detect_mode = "image" — по изменению яркости поплавка (шаблон float.png).

Плюс случайные короткие и длинные перерывы, пауза/стоп по горячим клавишам.
Запуск с ключом --calibrate — режим калибровки звука.
"""

import collections
import ctypes
import json
import logging
import os
import random
import shutil
import sys
import time
import warnings

import cv2
import numpy as np
import pyautogui

try:
    from mss import MSS as mss  # mss >= 10
except ImportError:  # старые версии mss
    from mss import mss

try:
    import psutil
    from comtypes import COMError
    from pycaw.api.audiopolicy import IAudioSessionControl2
    from pycaw.api.endpointvolume import IAudioMeterInformation
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.pycaw import AudioUtilities

    AUDIO_IMPORT_ERROR = None
except ImportError as _ex:  # без pycaw доступен только режим "image"
    AUDIO_IMPORT_ERROR = _ex

VERSION = "3.2"
SETTINGS_FILE = "settings.json"
TEMPLATE_FILE = "float.png"

log = logging.getLogger("fishbot")

# ----------------------------------------------------------------------------
# Пути
# ----------------------------------------------------------------------------


def app_dir() -> str:
    """Папка рядом с exe (или с main.py) — там лежат settings.json и лог."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name: str) -> str:
    """Файл рядом с exe имеет приоритет, иначе — встроенный в exe."""
    local = os.path.join(app_dir(), name)
    if os.path.exists(local):
        return local
    bundled = os.path.join(getattr(sys, "_MEIPASS", app_dir()), name)
    return bundled


# ----------------------------------------------------------------------------
# Настройки
# ----------------------------------------------------------------------------

DEFAULTS = {
    "key": "",
    "detect_mode": "sound",
    "loot_mode": "key",
    "loot_key": "",
    "start_delay": 5,
    "cast_delay": [1, 10],
    "sleep_after_capture": 1,
    "sleep_between_click": 0.5,
    "fishing_rod_restart_first_circle": 30,
    "fishing_rod_restart_second_circle": 30,
    "monitor1": [450, 70, 1200, 710],
    "region_search": None,
    "np_mean_value": 112,
    "np_mean_delta": 0,
    "similarity": 0.8,
    "sound_process": "Wow.exe",
    "sound_ignore_after_cast": 1.5,
    "sound_threshold": 0,
    "sound_ratio": 3.0,
    "sound_min_delta": 0.05,
    "short_break": {"enabled": True, "every_minutes": [10, 15], "duration_minutes": [1, 2]},
    "long_break": {"enabled": True, "every_minutes": [55, 65], "duration_minutes": [5, 6]},
    "session_limit_minutes": 0,
    "pause_key": "f10",
    "stop_key": "f11",
    "window_title": "",
    "log_to_file": True,
    "debug": False,
}

VK_CODES = {f"f{i}": 0x6F + i for i in range(1, 13)}
VK_CODES.update({
    "pause": 0x13, "scrolllock": 0x91, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
})


class ConfigError(Exception):
    pass


def as_range(value, name, minimum=0.0):
    """Число -> (x, x); [a, b] -> (a, b). Проверяет корректность."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        lo = hi = float(value)
    elif isinstance(value, (list, tuple)) and len(value) == 2 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
    ):
        lo, hi = float(value[0]), float(value[1])
    else:
        raise ConfigError(f"'{name}': ожидается число или [min, max], получено {value!r}")
    if lo > hi:
        lo, hi = hi, lo
    if lo < minimum:
        raise ConfigError(f"'{name}': значения должны быть >= {minimum}, получено {value!r}")
    return lo, hi


def rand(rng):
    return random.uniform(rng[0], rng[1])


def load_settings() -> dict:
    path = os.path.join(app_dir(), SETTINGS_FILE)
    if not os.path.exists(path):
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), SETTINGS_FILE)
        if getattr(sys, "frozen", False) and os.path.exists(bundled):
            shutil.copyfile(bundled, path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
        print(f"Создан файл настроек по умолчанию: {path}")

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            user = json.load(f)
    except json.JSONDecodeError as ex:
        raise ConfigError(f"Ошибка в {SETTINGS_FILE}: строка {ex.lineno}, столбец {ex.colno}: {ex.msg}") from None
    if not isinstance(user, dict):
        raise ConfigError(f"{SETTINGS_FILE} должен содержать JSON-объект {{...}}")

    cfg = json.loads(json.dumps(DEFAULTS))  # глубокая копия
    for k, v in user.items():
        if k.startswith("_"):
            continue  # комментарии вида "_comment"
        if k not in DEFAULTS:
            print(f"Внимание: неизвестный параметр '{k}' в {SETTINGS_FILE} — игнорирую")
            continue
        if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return validate(cfg)


def validate(cfg: dict) -> dict:
    c = {}
    for k in ("key", "loot_key", "pause_key", "stop_key", "window_title", "sound_process"):
        if not isinstance(cfg[k], str):
            raise ConfigError(f"'{k}' должен быть строкой")
        c[k] = cfg[k].strip()
    for k in ("key", "loot_key", "pause_key", "stop_key"):
        c[k] = c[k].lower()

    if cfg["loot_mode"] not in ("key", "mouse"):
        raise ConfigError("'loot_mode' должен быть \"key\" или \"mouse\"")
    c["loot_mode"] = cfg["loot_mode"]

    if cfg["detect_mode"] not in ("sound", "image"):
        raise ConfigError("'detect_mode' должен быть \"sound\" или \"image\"")
    c["detect_mode"] = cfg["detect_mode"]
    if c["detect_mode"] == "sound" and not c["sound_process"]:
        raise ConfigError("'sound_process' не может быть пустым в режиме \"sound\"")
    c["sound_ignore_after_cast"] = as_range(cfg["sound_ignore_after_cast"], "sound_ignore_after_cast")[0]
    c["sound_threshold"] = as_range(cfg["sound_threshold"], "sound_threshold")[0]
    if c["sound_threshold"] > 1:
        raise ConfigError("'sound_threshold' должен быть от 0 до 1 (0 — автоматически)")
    c["sound_ratio"] = as_range(cfg["sound_ratio"], "sound_ratio", 1)[0]
    c["sound_min_delta"] = as_range(cfg["sound_min_delta"], "sound_min_delta")[0]
    c["np_mean_delta"] = as_range(cfg["np_mean_delta"], "np_mean_delta")[0]
    if c["detect_mode"] == "sound" and c["loot_mode"] == "mouse":
        raise ConfigError("в режиме detect_mode=\"sound\" поплавок не ищется, поэтому нужен loot_mode=\"key\"")

    for k in ("pause_key", "stop_key"):
        if c[k] and c[k] not in VK_CODES:
            raise ConfigError(f"'{k}': допустимо {', '.join(VK_CODES)} или \"\" (выключено)")
    if c["pause_key"] and c["pause_key"] == c["stop_key"]:
        raise ConfigError("'pause_key' и 'stop_key' не должны совпадать")

    c["start_delay"] = as_range(cfg["start_delay"], "start_delay")[0]
    c["cast_delay"] = as_range(cfg["cast_delay"], "cast_delay")
    c["sleep_after_capture"] = as_range(cfg["sleep_after_capture"], "sleep_after_capture")
    c["sleep_between_click"] = as_range(cfg["sleep_between_click"], "sleep_between_click")
    c["first_circle"] = as_range(cfg["fishing_rod_restart_first_circle"], "fishing_rod_restart_first_circle", 1)[0]
    c["second_circle"] = as_range(cfg["fishing_rod_restart_second_circle"], "fishing_rod_restart_second_circle", 1)[0]
    c["np_mean_value"] = as_range(cfg["np_mean_value"], "np_mean_value")[0]
    c["similarity"] = as_range(cfg["similarity"], "similarity")[0]
    if not 0 < c["similarity"] <= 1:
        raise ConfigError("'similarity' должен быть в диапазоне (0, 1]")
    c["session_limit"] = as_range(cfg["session_limit_minutes"], "session_limit_minutes")[0] * 60

    m = cfg["monitor1"]
    if not (isinstance(m, list) and len(m) == 4 and all(isinstance(v, int) for v in m) and m[2] > 0 and m[3] > 0):
        raise ConfigError("'monitor1' должен быть [left, top, width, height] (целые, width/height > 0)")
    c["monitor"] = {"left": m[0], "top": m[1], "width": m[2], "height": m[3]}

    rs = cfg["region_search"]
    if rs is None:
        rs = [m[0], m[1]]
    if not (isinstance(rs, list) and len(rs) == 2 and all(isinstance(v, int) for v in rs)):
        raise ConfigError("'region_search' должен быть [x, y] или null")
    c["region_offset"] = (rs[0], rs[1])

    for name in ("short_break", "long_break"):
        b = cfg[name]
        if not isinstance(b, dict):
            raise ConfigError(f"'{name}' должен быть объектом")
        c[name] = {
            "enabled": bool(b.get("enabled", True)),
            "every": tuple(x * 60 for x in as_range(b.get("every_minutes"), f"{name}.every_minutes", 0.1)),
            "duration": tuple(x * 60 for x in as_range(b.get("duration_minutes"), f"{name}.duration_minutes")),
        }

    c["log_to_file"] = bool(cfg["log_to_file"])
    c["debug"] = bool(cfg["debug"])
    return c


# Коды клавиш Windows (virtual-key codes), не зависят от раскладки (RU/EN).
# https://learn.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes
SEND_VK = {chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)}
SEND_VK.update({str(d): 0x30 + d for d in range(10)})
SEND_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
SEND_VK.update({f"num{d}": 0x60 + d for d in range(10)})
SEND_VK.update({
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08, "esc": 0x1B,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    # знаки — по положению на клавиатуре (как в английской раскладке)
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
})
SEND_MODS = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12}

RU_TO_EN = dict(zip("йцукенгшщзхъфывапролджэячсмитьбю.ё", "qwertyuiop[]asdfghjkl;'zxcvbnm,./`"))

KEYEVENTF_KEYUP = 0x0002


def parse_key(spec: str):
    """'z' / 'f5' / 'shift+1' -> (список модификаторов VK, VK клавиши). Ошибка — ConfigError."""
    parts = [p.strip() for p in spec.lower().split("+")] if spec != "+" else ["+"]
    if not parts or any(not p for p in parts):
        raise ConfigError(f"клавиша '{spec}' записана неверно")
    *mods, main_key = parts
    for m in mods:
        if m not in SEND_MODS:
            raise ConfigError(f"'{m}' в '{spec}': допустимы модификаторы shift, ctrl, alt")
    if main_key in RU_TO_EN:
        raise ConfigError(f"клавиша '{spec}' указана русской буквой — укажите английскую "
                          f"букву на той же клавише: '{RU_TO_EN[main_key]}'")
    if main_key not in SEND_VK:
        raise ConfigError(f"клавиша '{spec}' не поддерживается. Примеры: z, 1, f5, space, num1, shift+1")
    return [SEND_MODS[m] for m in mods], SEND_VK[main_key]


def send_vk(vk: int, up: bool):
    u = ctypes.windll.user32
    scan = u.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC — часть игр смотрит на скан-код
    u.keybd_event(vk, scan, KEYEVENTF_KEYUP if up else 0, 0)


def _process_elevated(pid: int):
    """True/False — запущен ли процесс от администратора, None — не удалось узнать."""
    from ctypes import wintypes

    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
            return None
        try:
            val, ret = wintypes.DWORD(), wintypes.DWORD()
            ok = adv.GetTokenInformation(tok, 20, ctypes.byref(val), 4, ctypes.byref(ret))  # TokenElevation
            return bool(val.value) if ok else None
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


def check_elevation(process_name: str):
    """Игра от администратора, бот — нет: Windows молча не пропустит нажатия клавиш."""
    if os.name != "nt" or AUDIO_IMPORT_ERROR is not None:
        return
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
        for p in psutil.process_iter(["name", "pid"]):
            if (p.info["name"] or "").lower() == process_name.lower():
                if _process_elevated(p.info["pid"]) is not False:
                    log.warning("ВНИМАНИЕ: игра запущена от администратора (или это не удалось проверить), "
                                "а бот — нет. Windows может не пропускать нажатия клавиш в игру. "
                                "Если заброса не происходит — запустите FishBot.exe от администратора.")
                return
    except (OSError, psutil.Error) as ex:
        log.debug("Проверка прав не удалась: %s", ex)


def ask_key(prompt: str) -> str:
    while True:
        k = input(prompt).strip().lower()
        try:
            parse_key(k)
            return k
        except ConfigError as ex:
            print(ex)


def check_key(k: str, name: str):
    try:
        parse_key(k)
    except ConfigError as ex:
        raise ConfigError(f"'{name}': {ex}") from None


# ----------------------------------------------------------------------------
# Управление: пауза / стоп / активное окно
# ----------------------------------------------------------------------------


class StopBot(Exception):
    pass


class Controls:
    """Глобальные горячие клавиши через GetAsyncKeyState (работают при фокусе в игре)."""

    def __init__(self, pause_key: str, stop_key: str, window_title: str):
        self.pause_vk = VK_CODES.get(pause_key)
        self.stop_vk = VK_CODES.get(stop_key)
        self.pause_key, self.stop_key = pause_key, stop_key
        self.window_title = window_title.lower()
        self._down = {}
        self._user32 = ctypes.windll.user32 if os.name == "nt" else None

    def _pressed(self, vk) -> bool:
        """True один раз на нажатие (по фронту)."""
        if vk is None or self._user32 is None:
            return False
        down = bool(self._user32.GetAsyncKeyState(vk) & 0x8000)
        was = self._down.get(vk, False)
        self._down[vk] = down
        return down and not was

    def _active_title(self) -> str:
        if self._user32 is None:
            return ""
        hwnd = self._user32.GetForegroundWindow()
        length = self._user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def window_ok(self) -> bool:
        return not self.window_title or self.window_title in self._active_title().lower()

    def poll(self, check_window: bool = True) -> bool:
        """Проверка клавиш. Блокирует на время паузы. True — была пауза/ожидание окна
        (текущий цикл рыбалки надо начать заново)."""
        if self._pressed(self.stop_vk):
            raise StopBot()
        interrupted = False
        if self._pressed(self.pause_vk):
            interrupted = True
            log.info("ПАУЗА. Нажмите %s для продолжения, %s — выход.", self.pause_key.upper(),
                     self.stop_key.upper() or "Ctrl+C")
            while not self._pressed(self.pause_vk):
                if self._pressed(self.stop_vk):
                    raise StopBot()
                time.sleep(0.05)
            log.info("Продолжаю.")
        if check_window and not self.window_ok():
            interrupted = True
            log.info("Окно '%s' не активно — жду...", self.window_title)
            while not self.window_ok():
                if self._pressed(self.stop_vk):
                    raise StopBot()
                self._pressed(self.pause_vk)
                time.sleep(0.2)
            log.info("Окно активно, продолжаю.")
        return interrupted

    def wait(self, seconds: float, check_window: bool = True):
        """Сон с реакцией на пауза/стоп. Пауза останавливает отсчёт."""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            t0 = time.monotonic()
            self.poll(check_window)
            end += max(0.0, time.monotonic() - t0 - 0.05)  # время паузы не считается
            time.sleep(min(0.05, left))


# ----------------------------------------------------------------------------
# Перерывы
# ----------------------------------------------------------------------------


class BreakScheduler:
    def __init__(self, short_cfg: dict, long_cfg: dict):
        self.short, self.long = short_cfg, long_cfg
        now = time.monotonic()
        self.next_short = self._schedule(self.short, now)
        self.next_long = self._schedule(self.long, now)

    @staticmethod
    def _schedule(cfg, now):
        return now + rand(cfg["every"]) if cfg["enabled"] else float("inf")

    def describe(self) -> str:
        now = time.monotonic()
        parts = []
        for name, t in (("короткий", self.next_short), ("длинный", self.next_long)):
            if t != float("inf"):
                parts.append(f"{name} через {fmt(t - now)}")
        return ", ".join(parts) or "перерывы выключены"

    def due(self):
        """Возвращает ('long'|'short', длительность) или None."""
        now = time.monotonic()
        if now >= self.next_long:
            return "long", rand(self.long["duration"])
        if now >= self.next_short:
            return "short", rand(self.short["duration"])
        return None

    def done(self, kind: str):
        now = time.monotonic()
        # после любого перерыва короткий отсчитывается заново, чтобы не было двух подряд
        self.next_short = self._schedule(self.short, now)
        if kind == "long":
            self.next_long = self._schedule(self.long, now)


def fmt(seconds: float) -> str:
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}ч {m:02d}м {s:02d}с" if h else f"{m}м {s:02d}с"


# ----------------------------------------------------------------------------
# Звук
# ----------------------------------------------------------------------------


class AudioError(Exception):
    pass


class AudioMeter:
    """Пиковая громкость звука конкретного процесса (WASAPI session peak meter).

    Слушает только звук игры, поэтому Discord, браузер и т.п. не мешают.
    Значение 0..1. Сессии ищутся на всех активных устройствах вывода.
    """

    REFRESH_EVERY = 10.0

    def __init__(self, process_name: str):
        if AUDIO_IMPORT_ERROR is not None:
            raise AudioError(f"нет модулей для работы со звуком: {AUDIO_IMPORT_ERROR}")
        self.process_name = process_name.lower()
        self.meters = []
        self.devices = []
        self._last_refresh = 0.0

    def refresh(self):
        meters, devices = [], []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # pycaw шумит предупреждениями о свойствах устройств
            for dev in AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value):
                try:
                    enum = dev.AudioSessionManager.GetSessionEnumerator()
                    for i in range(enum.GetCount()):
                        ctl = enum.GetSession(i).QueryInterface(IAudioSessionControl2)
                        pid = ctl.GetProcessId()
                        if not pid:
                            continue
                        try:
                            name = psutil.Process(pid).name().lower()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                        if name == self.process_name:
                            meters.append(ctl.QueryInterface(IAudioMeterInformation))
                            devices.append(dev.FriendlyName)
                except (COMError, OSError) as ex:
                    log.debug("Устройство %s пропущено: %s", dev.FriendlyName, ex)
        self.meters, self.devices = meters, devices
        self._last_refresh = time.monotonic()
        return bool(meters)

    def peak(self) -> float:
        if not self.meters or time.monotonic() - self._last_refresh > self.REFRESH_EVERY:
            self.refresh()
        if not self.meters:
            raise AudioError(f"не найден звук процесса '{self.process_name}' — игра запущена и звук включён?")
        try:
            return max(m.GetPeakValue() for m in self.meters)
        except (COMError, OSError):
            self.meters = []  # сессия пересоздана (смена устройства и т.п.) — найдём заново
            return 0.0


class SplashDetector:
    """Детектор всплеска: резкий рост громкости относительно фона."""

    SAMPLE = 0.02  # период опроса, с
    WINDOW = 3.0  # окно фона, с
    CONFIRM = 2  # сколько подряд замеров выше порога

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.history = collections.deque(maxlen=int(self.WINDOW / self.SAMPLE))
        self.above = 0

    def baseline(self) -> float:
        return float(np.median(self.history)) if self.history else 0.0

    def threshold(self) -> float:
        if self.cfg["sound_threshold"] > 0:
            return self.cfg["sound_threshold"]
        b = self.baseline()
        return max(b * self.cfg["sound_ratio"], b + self.cfg["sound_min_delta"])

    def feed(self, value: float, armed: bool) -> bool:
        """armed=False — только набираем фон (сразу после заброса)."""
        thr = self.threshold()
        hit = armed and len(self.history) >= 10 and value > thr
        self.above = self.above + 1 if hit else 0
        if not hit:
            self.history.append(value)  # всплеск в фон не пишем
        return self.above >= self.CONFIRM


def calibrate(cfg: dict) -> int:
    """Живой индикатор громкости игры. Клавиши не нажимает."""
    meter = AudioMeter(cfg["sound_process"])
    if not meter.refresh():
        print(f"Звук процесса '{cfg['sound_process']}' не найден. Запустите игру и включите звук.")
        return 1
    print(f"Звук '{cfg['sound_process']}' найден на: {', '.join(meter.devices)}")
    print("Порыбачьте вручную. Строка показывает громкость; '!' — бот принял бы это за поклёвку.")
    print("Ctrl+C — завершить и получить рекомендации.\n")
    det = SplashDetector(cfg)
    peaks, splashes = [], []
    last_print = 0.0
    try:
        while True:
            v = meter.peak()
            peaks.append(v)
            hit = det.feed(v, armed=True)
            if hit and (not splashes or time.monotonic() - splashes[-1][0] > 2):
                splashes.append((time.monotonic(), v))
            now = time.monotonic()
            if now - last_print >= 0.1:
                bar = "#" * int(min(v, 1.0) * 60)
                mark = "!" if hit else " "
                print(f"\r{mark} {v:0.3f} фон {det.baseline():0.3f} порог {det.threshold():0.3f} |{bar:<60}|",
                      end="", flush=True)
                last_print = now
            time.sleep(SplashDetector.SAMPLE)
    except KeyboardInterrupt:
        pass
    print("\n")
    if len(peaks) < 50:
        print("Слишком мало данных.")
        return 0
    p = np.array(peaks)
    print(f"Громкость: медиана {np.median(p):.3f}, 99% {np.percentile(p, 99):.3f}, максимум {p.max():.3f}")
    print(f"Срабатываний детектора: {len(splashes)}")
    print("Если срабатывания были только на всплески поплавка — настройки подходят.")
    print("Ложные срабатывания -> увеличьте sound_ratio / sound_min_delta; пропуски -> уменьшите.")
    return 0


# ----------------------------------------------------------------------------
# Бот
# ----------------------------------------------------------------------------


class Player:
    def __init__(self, cfg: dict, controls: Controls):
        self.cfg = cfg
        self.controls = controls
        self.caught = 0
        self.casts = 0
        self.sct = None
        self.audio = None
        if cfg["detect_mode"] == "sound":
            self.audio = AudioMeter(cfg["sound_process"])
            if self.audio.refresh():
                log.info("Звук '%s' найден на: %s", cfg["sound_process"], ", ".join(self.audio.devices))
            else:
                log.warning("Звук '%s' пока не найден — проверю позже. Звук в игре включён?",
                            cfg["sound_process"])
        else:
            self.sct = mss()
            self.template = self._load_template()
            self.th, self.tw = self.template.shape[:2]

    def close(self):
        if self.sct is not None:
            self.sct.close()

    @staticmethod
    def _load_template():
        path = resource_path(TEMPLATE_FILE)
        img = cv2.imread(path)
        if img is None:
            raise ConfigError(f"Не удалось загрузить шаблон поплавка: {path}")
        return cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    def grab_hsv(self, region: dict):
        img = np.array(self.sct.grab(region))
        return cv2.cvtColor(img, cv2.COLOR_BGR2HSV)  # как в исходной версии (BGRA -> HSV)

    def find_float(self):
        """Лучшее совпадение шаблона в области monitor1. (x, y) в координатах экрана или None."""
        screen = self.grab_hsv(self.cfg["monitor"])
        if screen.shape[0] < self.th or screen.shape[1] < self.tw:
            raise ConfigError("Область 'monitor1' меньше шаблона поплавка")
        res = cv2.matchTemplate(screen, self.template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val < self.cfg["similarity"]:
            return None
        log.debug("Поплавок: совпадение %.3f в %s", max_val, max_loc)
        ox, oy = self.cfg["region_offset"]
        return int(max_loc[0] + ox), int(max_loc[1] + oy)

    def float_mean(self, x, y) -> float:
        region = {"left": x, "top": y, "width": self.tw, "height": self.th}
        return float(np.mean(self.grab_hsv(region)))

    def press(self, key: str):
        pyautogui.failSafeCheck()  # мышь в углу экрана — аварийная остановка
        mods, vk = parse_key(key)
        for m in mods:
            send_vk(m, up=False)
        send_vk(vk, up=False)
        try:
            time.sleep(rand(self.cfg["sleep_between_click"]))
        finally:  # клавиша не должна «залипнуть», даже при Ctrl+C
            send_vk(vk, up=True)
            for m in reversed(mods):
                send_vk(m, up=True)

    def cast(self):
        self.casts += 1
        log.info("Заброс #%d", self.casts)
        self.press(self.cfg["key"])

    def loot(self, x, y):
        if self.cfg["loot_mode"] == "mouse":
            cx = x + self.tw // 2 + random.randint(-2, 2)
            cy = y + self.th // 2 + random.randint(-2, 2)
            pyautogui.moveTo(cx, cy, duration=random.uniform(0.15, 0.35))
            pyautogui.click(cx, cy)
        else:
            self.press(self.cfg["loot_key"] or self.cfg["key"])

    def fish_once(self) -> bool:
        """Один заброс. True — рыба подсечена."""
        if self.cfg["detect_mode"] == "sound":
            return self.fish_once_sound()
        return self.fish_once_image()

    def hooked(self, pos=None) -> bool:
        self.loot(*(pos or (0, 0)))
        self.caught += 1
        self.controls.wait(rand(self.cfg["sleep_after_capture"]))
        return True

    def fish_once_sound(self) -> bool:
        c = self.controls
        det = SplashDetector(self.cfg)
        self.cast()
        t0 = time.monotonic()
        ignore = self.cfg["sound_ignore_after_cast"]
        lost_logged = False
        while time.monotonic() - t0 < self.cfg["second_circle"]:
            if c.poll():
                return False
            try:
                v = self.audio.peak()
                lost_logged = False
            except AudioError as ex:
                if not lost_logged:
                    log.warning("%s", ex)
                    lost_logged = True
                c.wait(1.0)
                continue
            armed = time.monotonic() - t0 >= ignore
            if det.feed(v, armed):
                log.info("Поклёвка! (звук %.3f, фон %.3f, порог %.3f)", v, det.baseline(), det.threshold())
                return self.hooked()
            log.debug("Звук %.3f фон %.3f порог %.3f", v, det.baseline(), det.threshold())
            time.sleep(SplashDetector.SAMPLE)
        log.info("Поклёвки нет за %.0f с — перезаброс", self.cfg["second_circle"])
        return False

    def fish_once_image(self) -> bool:
        c = self.controls
        self.cast()

        # 1) ищем поплавок
        pos = None
        t0 = time.monotonic()
        while pos is None:
            if c.poll():
                return False
            if time.monotonic() - t0 > self.cfg["first_circle"]:
                log.info("Поплавок не найден за %.0f с — перезаброс", self.cfg["first_circle"])
                return False
            try:
                pos = self.find_float()
            except ConfigError:
                raise
            except Exception as ex:  # noqa: BLE001 — сбой захвата экрана, пробуем снова
                log.debug("Ошибка захвата экрана: %s", ex)
                time.sleep(0.1)
        log.info("Поплавок найден: %s", pos)

        # 2) ждём поклёвку
        delta = self.cfg["np_mean_delta"]
        base = []  # яркость спокойного поплавка (первые кадры) — для относительного порога
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.cfg["second_circle"]:
            if c.poll():
                return False
            try:
                mean = self.float_mean(*pos)
            except Exception as ex:  # noqa: BLE001
                log.debug("Ошибка захвата экрана: %s", ex)
                time.sleep(0.05)
                continue
            if delta > 0:
                if len(base) < 10:
                    base.append(mean)
                    time.sleep(0.05)
                    continue
                limit = float(np.median(base)) + delta
            else:
                limit = self.cfg["np_mean_value"]
            log.debug("Разброс значений: %.2f (порог %.2f)", mean, limit)
            if mean > limit:
                log.info("Поклёвка! (%.1f > %.1f)", mean, limit)
                return self.hooked(pos)
        log.info("Поклёвки нет за %.0f с — перезаброс", self.cfg["second_circle"])
        return False

    def run(self):
        c = self.controls
        breaks = BreakScheduler(self.cfg["short_break"], self.cfg["long_break"])
        started = time.monotonic()
        log.info("Перерывы: %s", breaks.describe())

        while True:
            if self.cfg["session_limit"] and time.monotonic() - started >= self.cfg["session_limit"]:
                log.info("Достигнут лимит сессии (%s). Завершаю.", fmt(self.cfg["session_limit"]))
                return

            due = breaks.due()
            if due:
                kind, dur = due
                log.info("%s перерыв на %s", "Длинный" if kind == "long" else "Короткий", fmt(dur))
                c.wait(dur, check_window=False)  # во время перерыва можно уйти в другое окно
                breaks.done(kind)
                log.info("Перерыв окончен. Следующие: %s", breaks.describe())

            c.poll()
            if self.fish_once():
                delay = rand(self.cfg["cast_delay"])
                log.info("Поймано: %d/%d. Следующий заброс через %.1f с", self.caught, self.casts, delay)
                c.wait(delay)
            else:
                c.wait(random.uniform(0.5, 1.5))


# ----------------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------------


def setup_logging(cfg):
    handlers = [logging.StreamHandler(sys.stdout)]
    if cfg["log_to_file"]:
        handlers.append(logging.FileHandler(os.path.join(app_dir(), "fishbot.log"), encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if cfg["debug"] else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass  # stdout без reconfigure — оставляем как есть
    print(f"Fish bot v{VERSION}")
    try:
        cfg = load_settings()
        if "--calibrate" in sys.argv[1:]:
            logging.basicConfig(level=logging.INFO, format="%(message)s")
            try:
                return calibrate(cfg)
            except AudioError as ex:
                print(f"Ошибка звука: {ex}")
                return 1
        if not cfg["key"]:
            cfg["key"] = ask_key("Введите клавишу для удочки: ")
        check_key(cfg["key"], "key")
        if cfg["loot_key"]:
            check_key(cfg["loot_key"], "loot_key")
    except ConfigError as ex:
        print(f"Ошибка настроек: {ex}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nВвод прерван. Укажите 'key' в settings.json.")
        return 1

    setup_logging(cfg)
    controls = Controls(cfg["pause_key"], cfg["stop_key"], cfg["window_title"])
    pyautogui.FAILSAFE = True  # мышь в угол экрана — аварийная остановка

    log.info("Режим поклёвки: %s", "звук игры" if cfg["detect_mode"] == "sound" else "картинка поплавка")
    log.info("Клавиша заброса: %s | подсечка: %s", cfg["key"],
             "мышь" if cfg["loot_mode"] == "mouse" else (cfg["loot_key"] or cfg["key"]))
    log.info("Пауза: %s | Стоп: %s | Аварийно: мышь в угол экрана или Ctrl+C",
             cfg["pause_key"].upper() or "-", cfg["stop_key"].upper() or "-")
    check_elevation(cfg["sound_process"])

    player = None
    try:
        player = Player(cfg, controls)
        if cfg["start_delay"]:
            log.info("Старт через %.0f с — переключитесь в игру...", cfg["start_delay"])
            controls.wait(cfg["start_delay"], check_window=False)
        player.run()
    except StopBot:
        log.info("Остановлено клавишей %s.", cfg["stop_key"].upper())
    except KeyboardInterrupt:
        log.info("Остановлено (Ctrl+C).")
    except pyautogui.FailSafeException:
        log.info("Остановлено: мышь в углу экрана (fail-safe).")
    except ConfigError as ex:
        log.error("Ошибка настроек: %s", ex)
        return 1
    except AudioError as ex:
        log.error("Ошибка звука: %s", ex)
        return 1
    except Exception:
        log.exception("Непредвиденная ошибка")
        return 1
    finally:
        if player is not None:
            log.info("Итого: поймано %d из %d забросов.", player.caught, player.casts)
            player.close()
    return 0


if __name__ == "__main__":
    code = main()
    if getattr(sys, "frozen", False):
        try:
            input("\nНажмите Enter для выхода...")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
