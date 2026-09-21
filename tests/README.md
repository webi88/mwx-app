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

- `tests/test_selenium_fakes.py`: 38 checks de `plataformas/twitter/selenium_bot.py`
  con `FakeDriver`/`FakeElement` (sin Chrome): pegado de texto por CDP
  `Input.insertText`, portapapeles sin clic pese al `data-testid='mask'`,
  `execCommand`, `send_keys`, re-localizacion de elementos stale, eleccion del
  editor no ocluido/dialogo, cita de `solo_retwittear` y los refrescos de
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
`tests/editor_mask.html` y comprueba que `_pegar_texto` escribe por CDP sobre un
contenteditable tapado por el overlay `data-testid='mask'` (y que
`_primer_editor_visible` elige el editor no ocluido). Cierra Chrome siempre.
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
