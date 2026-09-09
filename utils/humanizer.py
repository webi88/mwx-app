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


def delay_entre_acciones(base: float = 2.0, variacion: float = 1.5):
    delay = base + random.uniform(0, variacion)
    time.sleep(delay)


def delay_entre_publicaciones():
    delay = random.uniform(30, 120)
    time.sleep(delay)


def delay_entre_likes():
    delay = random.uniform(2, 5)
    time.sleep(delay)


def delay_entre_retweets():
    delay = random.uniform(2.5, 6.0)
    time.sleep(delay)


def simular_lectura(driver, texto: str):
    palabras = len(texto.split())
    tiempo_lectura = palabras * random.uniform(0.1, 0.3)
    delay_aleatorio(tiempo_lectura * 0.5, tiempo_lectura * 1.5)


def comportamiento_aleatorio(driver, probabilidad: float = 0.1):
    if random.random() < probabilidad:
        scroll_aleatorio(driver, 100, 300)
        delay_aleatorio(1, 3)
        
        if random.random() < 0.3:
            scroll_aleatorio(driver, -200, -50)
            delay_aleatorio(0.5, 1)
