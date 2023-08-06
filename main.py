from mss import mss
import pyautogui
import numpy as np
import time
import cv2
import json

with open("settings.json", "r", encoding="utf-8") as file_profile:
    file_user = json.load(file_profile)
    file_profile.close()
sleep_after_capture = file_user['sleep_after_capture']
sleep_between_click = file_user['sleep_between_click']
fishing_rod_restart_first_circle = file_user['fishing_rod_restart_first_circle']
fishing_rod_restart_second_circle = file_user['fishing_rod_restart_second_circle']
monitor1 = file_user['monitor1']
region_search = file_user['region_search']
np_mean_value = file_user['np_mean_value']

key = input("Введите клавишу для удочки: ")


class Player:
    monitor1 = {  # Область поиска
        'left': monitor1[0],
        'top': monitor1[1],
        'width': monitor1[2],
        'height': monitor1[3],

    }

    def __init__(self):
        self.screen = 0
        self.fish_screen_v1 = None
        self.x = None
        self.y = None
        self.np_mean = None
        self.click = None
        self.grab = None

    def __str__(self):
        print("Класс успешно загружен...")

    # @staticmethod
    # def connect_wow():
    #     """Подключение к игре"""
    #     app = pywinauto.Application().connect(best_match='World of Warcraft')
    #     app.GxWindowClass.set_focus()
    #     time.sleep(0.3)

    def screen_monitor(self):
        """Матрица экрана"""
        with mss() as sct:
            img = sct.grab(player.monitor1)
            img = np.array(img)
            self.screen = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # m = mss()

    def load_img(self, img):
        """Загрузка изображений"""
        self.fish_screen_v1 = cv2.imread(str(img))
        self.fish_screen_v1 = cv2.cvtColor(self.fish_screen_v1, cv2.COLOR_BGR2HSV)
        self.fish_screen_v1 = np.array(self.fish_screen_v1)

    def find_load_img(self, img, similarity, x_offset=0, y_offset=0):
        """Нахождение изображения на экране"""
        try:
            res = cv2.matchTemplate(self.screen, img, cv2.TM_CCOEFF_NORMED)
            loc = np.where(res >= similarity)
            self.x, self.y = loc[1][0], loc[0][0]
            self.x += region_search[0]
            self.y += region_search[1]
        except Exception as ex:
            print("Ничего не нашел...")

    def mouse(self):
        """Функция мышки"""
        pyautogui.moveTo(self.x, self.y, duration=0.2)
        pyautogui.click(self.x, self.y)
        time.sleep(sleep_after_capture)
        player.click = 1

    @staticmethod
    def board():
        """Функция клавиатуры"""
        pyautogui.keyDown(f"{key}")
        time.sleep(sleep_between_click)
        pyautogui.keyUp(f"{key}")
        player.grab = 1

    def mean(self):
        """Среднее значение массива"""
        monitor2 = {
            'left': self.x,
            'top': self.y,
            'width': 17,
            'height': 17,
        }

        # m = mss()
        with mss() as sct:
            img = sct.grab(monitor2)
            img = np.array(img)
            screen_2 = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            self.np_mean = np.mean(screen_2)
            print(f"Разброс значений: {self.np_mean}")

    def clear(self):
        self.screen = 0
        self.x = None
        self.y = None
        self.np_mean = None
        self.click = None
        self.grab = None
        player.grab = None

    @staticmethod
    def method():
        # player.connect_wow()
        timeM0 = 0
        player.load_img("1.png")
        while player.x is None:
            timeM1 = time.time()
            if player.grab is None or timeM0 > fishing_rod_restart_first_circle:
                player.board()
                timeM0 = 0
            try:
                player.screen_monitor()
            except Exception as ex:
                pass
            player.find_load_img(player.fish_screen_v1, 0.8)

            timeM2 = time.time()
            timeM3 = timeM2 - timeM1
            timeM0 += timeM3
            print(timeM0)

            if player.x:
                time0 = 0
                while player.click is None and time0 < fishing_rod_restart_second_circle:
                    time1 = time.time()
                    try:
                        player.mean()
                    except:
                        pass
                    if player.np_mean > np_mean_value:
                        player.mouse()
                    time2 = time.time()
                    time3 = time2 - time1
                    time0 += time3
                    print(time0)
                player.clear()


player = Player()
player.method()