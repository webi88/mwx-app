import time
import random


def delay_aleatorio(minimo: float = 1.0, maximo: float = 3.0):
    delay = random.uniform(minimo, maximo)
    time.sleep(delay)


def scroll_aleatorio(driver, min_px: int = 200, max_px: int = 500):
    scroll_amount = random.randint(min_px, max_px)
    driver.execute_script(f"window.scrollBy(0, {scroll_amount})")
    delay_aleatorio(0.5, 1.5)


def comportamiento_humano_visualizacion(driver, duracion_min: float = 3.0, duracion_max: float = 8.0):
    scroll_aleatorio(driver, 200, 400)
    delay_aleatorio(0.5, 1.2)
    
    if random.random() < 0.15:
        scroll_aleatorio(driver, -300, -100)
    
    delay_aleatorio(duracion_min, duracion_max)
