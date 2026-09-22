# Tests

Suite de regresion del proyecto. No necesita dependencias nuevas: el runner usa
solo la stdlib.

## Suite rapida (sin red, sin Chrome)

```bash
.venv/Scripts/python.exe tests/run_tests.py
```

Descubre todos los `tests/test_*.py`, ejecuta la funcion `run(check)` de cada
archivo e imprime `PASS`/`FAIL` por check y un resumen final (`RESULTADO: N/N`).
Sale con codigo `0` si todo pasa y `1` si hay fallos.

- `tests/test_selenium_fakes.py`: 75 checks de `plataformas/twitter/selenium_bot.py`
  con `FakeDriver`/`FakeElement` (sin Chrome): pegado de texto con EVENTOS REALES
  de teclado (destruccion previa del `data-testid='mask'`, `editor.click()` +
  `send_keys` en UNA llamada, fallback `ActionChains(driver).send_keys`) y sin
  Insercion silenciosa (nada de CDP `Input.insertText`, `execCommand` ni
  portapapeles); limpieza del borrador restaurado por X, re-localizacion de
  elementos stale, pausa humana de 1.8-3.5s antes de publicar en
  `publicar_tweet`/`responder_tweet`/cita, eleccion del editor
  no ocluido/dialogo, cita de `solo_retwittear` y los refrescos de
  `navegar_tolerante`/`_recuperar_interstitial`.

Tambien se puede correr un archivo suelto:

```bash
.venv/Scripts/python.exe tests/test_selenium_fakes.py
```

## Smoke con Chrome real (manual)

```bash
.venv/Scripts/python.exe tests/smoke_chrome_cdp.py
```

Requiere Chrome instalado. Levanta un Chrome headless, carga
`tests/editor_mask.html` y comprueba el pegado anti-Ghostban: el mask se elimina
del DOM, `_pegar_texto` escribe el contenteditable con `click` + `send_keys`
reales (los listeners de `keydown`/`input` confirman que Chrome genero eventos de
teclado) y la variante wrapper no editable tambien escribe en el editable real
(send_keys o fallback ActionChains enfocado por JS). Cierra Chrome siempre.
No se incluye en `run_tests.py` a proposito: la suite rapida no debe abrir
navegadores.

## Convencion para nuevos tests

Cada `tests/test_*.py` debe exponer:

```python
def run(check):
    check("nombre claro en espanol", condicion, "detalle opcional")
```

`check` lo inyecta `run_tests.py` (o el propio archivo al correrse suelto).
Los archivos `smoke_*.py` no siguen este contrato y no se descubren.
