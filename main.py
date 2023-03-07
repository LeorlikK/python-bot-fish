from mss import mss
import pyautogui
import numpy as np
import time
import cv2


key = input("Введите клавишу для удочки: ")

class Player:
    monitor1 = {  # Область поиска
        'left': 450,
        'top': 70,
        'width': 1200,
        'height': 710,

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
        m = mss()
        img = m.grab(player.monitor1)
        img = np.array(img)
        self.screen = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

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
            self.x += 450
            self.y += 70
        except Exception as ex:
            print("Ничего не нашел...")

    def mouse(self):
        """Функция мышки"""
        pyautogui.moveTo(self.x, self.y, duration=0.2)
        pyautogui.click(self.x, self.y)
        player.click = 1

    @staticmethod
    def board():
        """Функция клавиатуры"""
        pyautogui.keyDown(f"{key}")
        time.sleep(0.5)
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

        m = mss()
        img = m.grab(monitor2)
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
        player.grab = None

    @staticmethod
    def method():
        player.connect_wow()
        player.load_img("1.png")
        while player.x is None:
            if player.grab is None:
                player.board()
            player.screen_monitor()
            player.find_load_img(player.fish_screen_v1, 0.8)

            if player.x:
                time0 = 0
                while player.click is None and time0 < 30:
                    time1 = time.time()
                    player.mean()
                    if player.np_mean > 112:
                        player.mouse()
                    time2 = time.time()
                    time3 = time2 - time1
                    time0 += time3
                    print(time0)
                player.clear()


player = Player()
player.method()