import json
import random
import re
import unicodedata

import openai
from core.config import settings, resolver_ruta
from core.perfiles import (
    colocar_hashtag_en_medio,
    elegir_hashtag,
    etiqueta_perfil,
    normalizar_perfil,
    tiene_hashtag,
)
from core.registros import normalizar_tipo_cuenta
from ia.prompts import (
    REGLA_MAX_100,
    TEMAS_MANTENIMIENTO,
    bloque_estilo_perfil,
    bloque_tweet_ancla,
    get_prompt_hashtags,
    get_prompt_verificado_ambiental,
    get_prompt_harfuch,
    get_prompt_por_tipo,
    instrucciones_tema,
    _normalizar_tema,
    _reglas_registro,
    _reglas_trasfondo,
)
from loguru import logger


class GeneradorContenido:
    def __init__(self):
        self.ultimo_error = ""
        openai.api_key = settings.openai_api_key

    # ------------------------------------------------------------------ #
    # Llamada a OpenAI (compatible con openai 0.28 y >= 1.0)
    # ------------------------------------------------------------------ #
    def _chat(self, prompt: str, temperature: float = 0.7) -> str:
        """Hace una llamada de chat y devuelve el texto.

        Guarda `self.ultimo_error` y propaga la excepcion para que el
        llamador decida como continuar.
        """
        try:
            if hasattr(openai, "OpenAI"):  # openai >= 1.0
                client = openai.OpenAI(api_key=settings.openai_api_key)
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=temperature,
                )
                return (response.choices[0].message.content or "").strip()

            # openai 0.28.x
            openai.api_key = settings.openai_api_key
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            self.ultimo_error = str(e)[:300]
            logger.error(f"Error llamando a OpenAI: {e}")
            raise

    # ------------------------------------------------------------------ #
    # Utilidades de parseo
    # ------------------------------------------------------------------ #
    @staticmethod
    def _limpiar_texto(texto: str) -> str:
        """Quita prefijos numerados/vinetas, comillas y corchetes sobrantes."""
        t = (texto or "").strip()
        t = re.sub(r"^\s*(?:[-*•]\s+|\d+[.)\-:]\s*)", "", t).strip()
        t = t.strip().strip('"').strip("'").strip()
        t = re.sub(r"^\[(.+)\]$", r"\1", t).strip()
        return t.strip()

    @staticmethod
    def _deduplicar(textos) -> list[str]:
        unicos = []
        vistos = set()
        for t in textos:
            if t and t not in vistos:
                vistos.add(t)
                unicos.append(t)
        return unicos

    def _parsear_textos(self, content: str, cantidad: int = 0) -> list[str]:
        """Parsea la respuesta: JSON array, separador '---', parrafos o lineas."""
        content = (content or "").strip()
        if not content:
            return []

        crudos = []

        # 1) Array JSON de strings (o dict con lista).
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                crudos = [str(t) for t in parsed]
            elif isinstance(parsed, dict):
                for clave in ("textos", "posts", "variaciones", "resultados"):
                    if isinstance(parsed.get(clave), list):
                        crudos = [str(t) for t in parsed[clave]]
                        break
        except Exception:
            crudos = []

        # 2) Separador explicito '---'.
        if not crudos:
            crudos = content.split("---")

        # 3) Fallback: separar por parrafos (lineas dobles).
        if len([c for c in crudos if str(c).strip()]) <= 1:
            crudos = re.split(r"\n\s*\n", content)

        # 4) Ultimo recurso: una linea por texto (solo si se pidio mas de 1).
        if len([c for c in crudos if str(c).strip()]) <= 1 and cantidad != 1:
            crudos = content.splitlines()

        return self._deduplicar(self._limpiar_texto(c) for c in crudos)

    # ------------------------------------------------------------------ #
    # Generacion principal
    # ------------------------------------------------------------------ #
    def generar_contenido(
        self,
        tipo: str,
        narrativa: str,
        entrenamiento: str = "",
        contexto: str = "",
        cantidad: int = 10,
        registro: str = "",
        tema: str = "",
        personalidad: str = "",
    ) -> list[str]:
        self.ultimo_error = ""
        try:
            cantidad = int(cantidad)
        except (TypeError, ValueError):
            cantidad = 10

        # Tipos con su PROPIO limite de largo, fuera de la regla de 100:
        # blog (600), verificado y harfuch (240).
        tipo_norm = str(tipo or "").strip().lower()
        exento_100 = tipo_norm in ("blog", "verificado", "harfuch")

        try:
            if tipo == "verificado":
                prompt = get_prompt_verificado_ambiental(contexto)
            elif tipo == "harfuch":
                prompt = get_prompt_harfuch(contexto)
            else:
                prompt = get_prompt_por_tipo(
                    tipo,
                    narrativa,
                    entrenamiento,
                    contexto,
                    cantidad,
                    registro,
                    tema=tema,
                    personalidad=personalidad,
                )

            content = self._chat(prompt, temperature=0.7)
            textos = self._parsear_textos(content, cantidad)

            if not textos:
                self.ultimo_error = "OpenAI devolvio una respuesta vacia"
                logger.error("Generacion de contenido vacia")
                return []

            if cantidad > 0 and len(textos) < cantidad:
                # Relleno 1: variaciones con OpenAI a partir del primer texto.
                faltante = cantidad - len(textos)
                try:
                    variaciones = self.generar_variaciones_masivas(
                        textos[0], faltante, narrativa, entrenamiento, registro
                    )
                except Exception as e:
                    logger.error(f"Error generando variaciones de relleno: {e}")
                    variaciones = []
                textos = self._deduplicar(textos + list(variaciones))

            if cantidad > 0 and len(textos) < cantidad:
                # Relleno 2: variaciones locales (sin OpenAI).
                faltante = cantidad - len(textos)
                try:
                    from activaciones.variaciones import generar_pool_variaciones
                    relleno = generar_pool_variaciones(textos[0], faltante)
                except Exception as e:
                    logger.error(f"Error en fallback de variaciones: {e}")
                    relleno = []
                textos = self._deduplicar(textos + list(relleno))

            # La generacion principal tuvo exito (el relleno pudo fallar).
            self.ultimo_error = ""
            resultado = textos[:cantidad] if cantidad > 0 else textos

            # Corte DURO (regla de oro) SOLO para los tipos de POST estandar:
            # si la IA ignora el prompt, igual se publica <= 100 caracteres.
            # Blog/verificado/harfuch conservan su limite propio. El mapeo
            # respeta la cantidad y el orden de los textos.
            if not exento_100:
                resultado = [_acotar_limite(t) for t in resultado]

            return resultado

        except Exception as e:
            self.ultimo_error = str(e)[:300]
            logger.error(f"Error generando contenido: {e}")
            return []

    def generar_variaciones_masivas(
        self,
        base: str,
        cantidad: int,
        narrativa: str = "",
        entrenamiento: str = "",
        registro: str = "",
        perfil: str = "",
    ) -> list[str]:
        """Genera EXACTAMENTE 'cantidad' versiones distintas y unicas de 'base'.

        `registro` ("politica"/"ciudadana"/"") agrega las reglas de estilo al
        prompt para que las variaciones respeten el registro de la cuenta.
        `perfil` ("formal"/"ciudadano"/"popular") agrega el formato obligatorio
        del perfil (3 bloques / par de renglones / un renglon casual) y
        garantiza hashtag integrado en medio del texto.

        Nunca lanza excepcion: si OpenAI falla o no alcanza, rellena con
        variaciones locales y, en ultimo caso, con sufijos numerados.
        """
        try:
            cantidad = int(cantidad)
        except (TypeError, ValueError):
            cantidad = 0
        if cantidad <= 0:
            return []

        base = (base or "").strip()
        perfil_norm = normalizar_perfil(perfil)
        textos: list[str] = []

        try:
            prompt = (
                f"Genera EXACTAMENTE {cantidad} versiones distintas y únicas de la "
                "siguiente cita. Mantén el mismo sentido, pero varía la "
                "redacción, el orden de las ideas y las palabras. Cada texto debe "
                "tener un MÁXIMO ESTRICTO de 100 caracteres (incluyendo "
                "hashtags y espacios)."
            )
            prompt += _reglas_registro(registro, incluir_limite=False)
            prompt += f"\n{REGLA_MAX_100}\n"
            prompt += bloque_estilo_perfil(perfil_norm)
            prompt += _reglas_trasfondo()
            prompt += (
                "\nLas variaciones conservan el SENTIDO de la CITA BASE y el "
                "REGISTRO/PERFIL de la cuenta; PROHIBIDO incorporar datos, "
                "nombres propios, cifras, fechas o frases de la "
                "NARRATIVA/TRASFONDO.\n"
            )
            if perfil_norm:
                prompt += (
                    "\nEl texto DEBE incluir al menos un hashtag integrado EN MEDIO "
                    "del texto; NUNCA lo pongas al final.\n"
                )
            if narrativa:
                prompt += (
                    f"\n\nNARRATIVA GENERAL (TRASFONDO INVISIBLE):\n{narrativa}"
                )
            if entrenamiento:
                prompt += f"\n\nENTRENAMIENTO DEL CLIENTE:\n{entrenamiento}"
            prompt += (
                f"\n\nCITA BASE:\n{base}\n\n"
                "Responde ÚNICAMENTE con un array JSON de strings, por ejemplo: "
                '["texto1", "texto2", ...]. No incluyas numeración, prefijos ni '
                "texto adicional fuera del JSON."
            )

            content = self._chat(prompt, temperature=0.9)

            # 1) Array JSON de strings (o dict con lista).
            try:
                parsed = json.loads(content)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                textos = [str(t) for t in parsed]
            elif isinstance(parsed, dict):
                for clave in ("textos", "variaciones", "citas", "resultados"):
                    if isinstance(parsed.get(clave), list):
                        textos = [str(t) for t in parsed[clave]]
                        break

            # 2) Fallback: '---' / parrafos / lineas.
            if not textos:
                if "---" in content:
                    textos = content.split("---")
                elif re.search(r"\n\s*\n", content):
                    textos = re.split(r"\n\s*\n", content)
                else:
                    textos = content.splitlines()

        except Exception as e:
            self.ultimo_error = str(e)[:300]
            logger.error(f"Error generando variaciones masivas: {e}")
            textos = []

        limpios = self._deduplicar(self._limpiar_texto(t) for t in textos)

        # Red de seguridad: la narrativa/noticias son SOLO trasfondo. Si una
        # variacion de la IA filtra material reconocible de la narrativa, se
        # DESCARTA; el relleno local de abajo (generar_pool_variaciones) no
        # lee la narrativa y garantiza la cantidad pedida.
        if narrativa and limpios:
            sin_fuga = []
            for t in limpios:
                if _fuga_narrativa(t, narrativa):
                    logger.warning(
                        "Se detecto fuga de la narrativa; se descarta la "
                        "variacion de IA y se usa fallback local"
                    )
                    continue
                sin_fuga.append(t)
            limpios = sin_fuga

        # Signos de apertura: activista/ciudadana NUNCA abren "¿"/"¡"
        # (aplica tambien a los textos de la IA); politica conserva los suyos.
        if limpios:
            limpios = [
                _quitar_signos_por_registro(t, registro) for t in limpios
            ]

        # Relleno 1: variaciones locales (sinonimos/hashtags).
        if len(limpios) < cantidad:
            try:
                from activaciones.variaciones import generar_pool_variaciones
                fallback = generar_pool_variaciones(base, cantidad - len(limpios))
            except Exception as e:
                logger.error(f"Error en fallback local de variaciones: {e}")
                fallback = []
            # El fallback local tambien se humaniza segun el registro
            # (antes del hashtag para no deformar el tag).
            humanizados = []
            for idx, t in enumerate(fallback):
                t = self._limpiar_texto(t)
                t = _humanizar_por_registro(
                    t, registro, semilla=len(limpios) + idx
                )
                humanizados.append(t)
            limpios = self._deduplicar(limpios + humanizados)

        # Relleno 2 (ultimo caso): sufijos numerados para garantizar cantidad.
        sufijo = 1
        vistos = set(limpios)
        while len(limpios) < cantidad:
            variante = f"{base} ({sufijo})" if base else f"Variacion {sufijo}"
            sufijo += 1
            if variante not in vistos:
                variante = _humanizar_por_registro(
                    variante, registro, semilla=len(limpios) + sufijo + 5000
                )
                if variante in vistos:
                    continue
                vistos.add(variante)
                limpios.append(variante)

        # Regla de perfil: hashtag obligatorio integrado EN MEDIO (nunca al
        # final), sin importar si el texto vino de OpenAI o del fallback.
        if perfil_norm:
            limpios = [colocar_hashtag_en_medio(t) for t in limpios]

        # Corte DURO final (regla de oro): TODA variacion/cita <= 100 chars,
        # venga de la IA o del relleno local/sufijos. Contrato "EXACTAMENTE
        # cantidad": si el recorte crea duplicados se intenta variarlos (3
        # intentos) y, si siguen colisionando, se CONSERVA el texto acotado
        # (mejor un duplicado que devolver menos de los pedidos).
        acotados: list[str] = []
        vistos_final: set[str] = set()
        for t in limpios:
            t = _acotar_limite(t)
            if not t:
                continue
            if t in vistos_final:
                for _ in range(3):
                    try:
                        alterno = _variar_hasta_unico(t, vistos_final)
                    except Exception:
                        alterno = ""
                    alterno = _acotar_limite(alterno)
                    if alterno and alterno not in vistos_final:
                        t = alterno
                        break
            vistos_final.add(t)
            acotados.append(t)

        return acotados[:cantidad]

    def generar_imagen(self, prompt: str) -> str:
        try:
            if hasattr(openai, "OpenAI"):  # openai >= 1.0
                client = openai.OpenAI(api_key=settings.openai_api_key)
                response = client.images.generate(
                    model="gpt-image-1",
                    prompt=prompt,
                    n=1,
                    size="1024x1024",
                )
            else:  # openai 0.28.x
                openai.api_key = settings.openai_api_key
                response = openai.Image.create(
                    model="gpt-image-1",
                    prompt=prompt,
                    n=1,
                    size="1024x1024",
                )

            data = response.data[0]
            output_path = resolver_ruta(f"data/temp/ai_generated_{abs(hash(prompt))}.png")

            import base64
            import os
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            if getattr(data, "b64_json", None):
                image_data = base64.b64decode(data.b64_json)
                with open(output_path, "wb") as f:
                    f.write(image_data)
            elif getattr(data, "url", None):
                import requests
                resp = requests.get(data.url)
                with open(output_path, "wb") as f:
                    f.write(resp.content)
            else:
                logger.error("La respuesta no trae imagen (ni b64_json ni url)")
                return None

            return output_path

        except Exception as e:
            self.ultimo_error = str(e)[:300]
            logger.error(f"Error generando imagen: {e}")
            return None


_MAX_VARIACIONES_POR_LLAMADA = 25


def generar_pool_por_cuenta(
    base: str,
    n_cuentas: int,
    narrativa: str = "",
    entrenamiento: str = "",
    registros: list | None = None,
    perfiles: list | None = None,
) -> list[str]:
    """Devuelve EXACTAMENTE 'n_cuentas' textos UNICOS (uno por cuenta).

    `registros` es una lista opcional alineada con las cuentas (mismo indice):
    valores "politica", "ciudadana" o "". Cuando se recibe, las cuentas se
    agrupan por registro y las variaciones de cada grupo se piden con ese
    registro (cap de `_MAX_VARIACIONES_POR_LLAMADA` textos por llamada).

    `perfiles` es otra lista opcional alineada por indice: "formal",
    "ciudadano", "popular" o "". Cuando se recibe, cada grupo pide a la IA el
    formato obligatorio del perfil y los textos llevan hashtag integrado en
    medio. Puede venir `perfiles` sin `registros` (y viceversa).

    Sin `registros` ni `perfiles` mantiene el comportamiento clasico: una
    sola llamada sin reglas de estilo.

    El indice i del pool siempre corresponde a la cuenta i. Si OpenAI falla,
    rellena con variaciones locales y, en ultimo caso, con sufijos.
    Firma congelada: los argumentos nuevos van al final.
    """
    try:
        n_cuentas = int(n_cuentas)
    except (TypeError, ValueError):
        return []
    if n_cuentas <= 0:
        return []

    base = (base or "").strip()
    vistos: set[str] = set()

    def _agregar(destino: list, texto) -> bool:
        t = (texto or "").strip()
        if t and t not in vistos:
            vistos.add(t)
            destino.append(t)
            return True
        return False

    # Normaliza las listas por cuenta (tolerante a largos distintos).
    def _clave(lista, i, normalizador) -> str:
        try:
            if lista and i < len(lista):
                valor = lista[i]
                return normalizador(valor) if str(valor or "").strip() else ""
        except Exception:
            pass
        return ""

    hay_estilos = bool(registros) or bool(perfiles)
    if not hay_estilos:
        # Comportamiento clasico: una sola llamada sin registro.
        try:
            nuevos = GeneradorContenido().generar_variaciones_masivas(
                base, n_cuentas, narrativa, entrenamiento
            )
        except Exception as e:
            logger.error(f"Error generando pool por cuenta: {e}")
            nuevos = []

        unicos = []
        for t in nuevos:
            _agregar(unicos, t)

        # Relleno 1: variaciones locales.
        if len(unicos) < n_cuentas:
            try:
                from activaciones.variaciones import generar_pool_variaciones
                fallback = generar_pool_variaciones(base, n_cuentas - len(unicos))
            except Exception as e:
                logger.error(f"Error en fallback de pool por cuenta: {e}")
                fallback = []
            for t in fallback:
                _agregar(unicos, t)
                if len(unicos) >= n_cuentas:
                    break

        # Relleno 2 (ultimo caso): sufijos numerados.
        sufijo = 1
        while len(unicos) < n_cuentas:
            variante = f"{base} ({sufijo})" if base else f"Variacion {sufijo}"
            sufijo += 1
            if variante not in vistos:
                vistos.add(variante)
                unicos.append(variante)

        # Corte DURO final (regla de oro): cada texto del pool <= 100 chars,
        # incluidos los rellenos locales y los sufijos numerados. Mapeo directo
        # que conserva SIEMPRE el conteo y el orden (mejor un duplicado que
        # devolver menos textos de los pedidos).
        return [_acotar_limite(t) for t in unicos][:n_cuentas]

    # --- Con registro y/o perfil: agrupar cuentas por estilo. ---
    reglas = []
    for i in range(n_cuentas):
        registro = _clave(
            registros, i, lambda v: str(v).strip().lower()
        )
        perfil = _clave(perfiles, i, normalizar_perfil)
        reglas.append((registro, perfil))

    grupos: dict[tuple, list[int]] = {}
    for i, clave in enumerate(reglas):
        grupos.setdefault(clave, []).append(i)

    generador = GeneradorContenido()
    resultado: list[str | None] = [None] * n_cuentas

    for (registro, perfil), indices in grupos.items():
        pendientes = list(indices)
        while pendientes:
            lote = pendientes[:_MAX_VARIACIONES_POR_LLAMADA]
            try:
                textos = generador.generar_variaciones_masivas(
                    base, len(lote), narrativa, entrenamiento, registro, perfil
                )
            except Exception as e:
                logger.error(f"Error generando variaciones por estilo: {e}")
                textos = []

            asignados = 0
            for t in textos:
                t = (t or "").strip()
                if perfil:
                    t = colocar_hashtag_en_medio(t)
                if not t or t in vistos or asignados >= len(lote):
                    continue
                vistos.add(t)
                resultado[lote[asignados]] = t
                asignados += 1

            pendientes = pendientes[asignados:]
            if asignados < len(lote):
                # Duplicados entre grupos/grupo agotado: el resto va al fallback.
                break

    # Relleno 1 y 2 EN SU LUGAR (mantiene indice i -> cuenta i).
    sufijo = 1
    faltantes = [i for i, t in enumerate(resultado) if not t]
    if faltantes:
        try:
            from activaciones.variaciones import generar_pool_variaciones
            fallback = list(generar_pool_variaciones(base, len(faltantes)))
        except Exception as e:
            logger.error(f"Error en fallback de pool por cuenta: {e}")
            fallback = []

        for i in faltantes:
            relleno = None
            while fallback:
                candidato = (fallback.pop(0) or "").strip()
                if candidato and candidato not in vistos:
                    relleno = candidato
                    break
            if relleno is None:
                while True:
                    variante = f"{base} ({sufijo})" if base else f"Variacion {sufijo}"
                    sufijo += 1
                    if variante not in vistos:
                        relleno = variante
                        break
            # Estilo local segun registro (antes del hashtag) + hashtag en medio.
            try:
                relleno = _humanizar_por_registro(
                    relleno, reglas[i][0], semilla=i + sufijo
                )
            except Exception:
                pass
            if reglas[i][1]:
                relleno = colocar_hashtag_en_medio(relleno)
            vistos.add(relleno)
            resultado[i] = relleno

    # Corte DURO final (regla de oro): cada texto <= 100 chars, conservando
    # SIEMPRE la alineacion indice i -> cuenta i.
    return [_acotar_limite(t or "") for t in resultado][:n_cuentas]


# ===================================================================== #
# Mantenimiento organico: textos variados por cuenta/tema/personalidad
# ===================================================================== #
_MAX_PERFILES_POR_LLAMADA_MANTENIMIENTO = 15

# Temas de mantenimiento: UNA sola fuente de verdad (``ia.prompts``). El
# fallback local solo se usa si el import no expusiera la constante.
_TEMAS_MANTENIMIENTO_FALLBACK = (
    "azteca",
    "dia",
    "tendencias",
    "gustos",
    "trafico",
    "clima",
    "comida",
    "series",
    "lunes",
    "insomnio",
    "futbol",
    "memes",
    "motivacion",
    "mascotas",
)
try:
    _TEMAS_MANTENIMIENTO = (
        tuple(TEMAS_MANTENIMIENTO) or _TEMAS_MANTENIMIENTO_FALLBACK
    )
except Exception:
    _TEMAS_MANTENIMIENTO = _TEMAS_MANTENIMIENTO_FALLBACK

# --------------------------------------------------------------------- #
# Fallback local DINAMICO POR PARTES (Sujeto + Accion + Comentario).
#
# En vez de frases rigidas, el fallback ensambla textos con piezas de pools
# grandes (>=30 sujetos, >=20 acciones y >=30 comentarios por perfil) mas las
# piezas de la FAMILIA de vocabulario del tema; con eso se generan cientos de
# combinaciones por perfil. Los pools se comparten entre perfiles y el TONO
# lo aporta el ensamblado (formal/ciudadano/popular/generico).
# --------------------------------------------------------------------- #

# Sujetos neutros (aplican a cualquier tema; el tema aporta su familia).
_SUJETOS_NEUTROS = (
    "el dia de hoy",
    "la semana que corre",
    "la rutina de siempre",
    "las horas de la tarde",
    "la agenda apretada",
    "los pendientes de casa",
    "el paso de los dias",
    "la vida diaria",
    "las cosas sencillas",
    "los planes de la tarde",
    "el descanso merecido",
    "la platica de sobremesa",
    "los ratos libres",
    "el andar de la semana",
    "las pequenas alegrias",
    "la manana de hoy",
    "el ruido de la calle",
    "la calma de la casa",
    "los quehaceres",
    "el tiempo libre",
    "la tarde tranquila",
    "los buenos ratos",
    "las ganas de avanzar",
    "los planes pendientes",
    "la lista de tareas",
    "el ritmo de la ciudad",
    "las horas tranquilas",
    "el dia a dia",
    "los momentos simples",
    "la jornada de hoy",
    "los pequenos descansos",
    "la energia del dia",
)

# Acciones neutras (verbos en primera/tercera persona, cotidianos).
_ACCIONES_NEUTRAS = (
    "me tomo por sorpresa",
    "nos cambio el plan",
    "me alegro la manana",
    "nos hizo platicar un rato",
    "me dejo pensando",
    "nos reunio a todos",
    "me saco una sonrisa",
    "nos puso de buenas",
    "me dio mucha risa",
    "nos recordo lo importante",
    "me lleno de energia",
    "nos dejo una leccion",
    "me hizo el dia",
    "nos unio un poco mas",
    "me trajo recuerdos",
    "me quito el sueno",
    "nos dio tema de platica",
    "me puso a reflexionar",
    "nos contagio el animo",
    "me dejo buen sabor",
    "nos hizo reir",
    "me dio un respiro",
)

# Comentarios/remates neutros (cierres de opinion cotidianos).
_COMENTARIOS_NEUTROS = (
    "asi que hay que disfrutarlo",
    "ojala se repita pronto",
    "vale la pena aprovechar",
    "no hay nada mejor",
    "asi es la vida diaria",
    "conviene ir con calma",
    "todo suma al final",
    "y asi vamos pasando",
    "hay que cuidarlo",
    "es parte de lo cotidiano",
    "siempre queda algo bueno",
    "mejor tomarlo con humor",
    "lo importante es compartir",
    "asi se aprende",
    "nunca falta el detalle",
    "hay que seguirle",
    "con calma todo mejora",
    "es cosa de actitud",
    "y que siga la costumbre",
    "vale la pena conversarlo",
    "aqui seguimos",
    "un gusto saberlo",
    "dan ganas de repetir",
    "que cosa tan cierta",
    "se agradece el gesto",
    "asi son las cosas",
    "es otro rollo",
    "para eso estan los amigos",
    "y no es para menos",
    "que tiempos aquellos",
    "nadie se salva",
    "la verdad se agradece",
)

# Extras de tono por perfil (se suman a los pools neutros).
_SUJETOS_FORMAL_EXTRA = (
    "la jornada que inicia",
    "el balance del dia",
    "las prioridades de la semana",
    "el orden de las tareas",
    "la agenda del dia",
    "el proposito de la jornada",
)
_ACCIONES_FORMAL_EXTRA = (
    "merece una mirada serena",
    "invita a la reflexion",
    "pide orden y calma",
    "deja una ensenanza",
    "conviene valorar",
)
_COMENTARIOS_FORMAL_EXTRA = (
    "conviene mantener la calma y seguir adelante",
    "es sano reconocer lo bueno de cada dia",
    "reflexionar sobre lo cotidiano ayuda a ordenar prioridades",
    "vale la pena mirar el dia con serenidad",
    "la constancia tambien es una forma de cuidar el futuro",
    "los pequenos avances construyen lo grande",
)

_SUJETOS_CIUDADANO_EXTRA = (
    "la platica de la tarde",
    "el pendiente de siempre",
    "las cosas de la casa",
    "la vuelta por el rumbo",
    "los ratos de tranquilidad",
    "el rato de descanso",
)
_ACCIONES_CIUDADANO_EXTRA = (
    "me hizo sonreir sin querer",
    "me ayudo a ordenar la cabeza",
    "me dio un momento de paz",
    "me recordo lo simple",
    "me dejo con buen animo",
)
_COMENTARIOS_CIUDADANO_EXTRA = (
    "no hay mejor plan que ese",
    "me quedo con lo bueno",
    "asi se disfruta mas",
    "eso si se agradece",
    "yo lo veo igual",
    "me parece de lo mas normal",
)

_SUJETOS_POPULAR_EXTRA = (
    "la banda de la cuadra",
    "el cotorreo de siempre",
    "la vuelta a la tienda",
    "el rol de la tarde",
    "los compas del rumbo",
    "la loquera del dia",
)
_ACCIONES_POPULAR_EXTRA = (
    "me saco de onda",
    "me puso de buenas",
    "me dio risa",
    "me cayo bien",
    "me movio el piso",
)
_COMENTARIOS_POPULAR_EXTRA = (
    "ya con eso la armamos",
    "asi me gusta la vida",
    "pa que veas",
    "y asi le seguimos",
    "no hay bronca",
    "todo relax",
)

_SUJETOS_GENERICO_EXTRA = (
    "el dia a dia",
    "las cosas que pasan",
    "los ratos normales",
    "la vida de todos los dias",
    "los momentos de siempre",
    "las vueltas de la vida",
)
_ACCIONES_GENERICO_EXTRA = (
    "da para comentar",
    "deja buen sabor",
    "invita a la conversacion",
    "se disfruta distinto",
    "acerca a la gente",
)
_COMENTARIOS_GENERICO_EXTRA = (
    "nunca cae mal un momento asi",
    "estos ratos son los mejores",
    "todo va sumando",
    "asi es esto",
    "mejor disfrutarlo",
    "la vida sigue",
)

# POOLS POR PARTES por perfil (sujetos/acciones/comentarios). "generico"
# aplica a cuentas sin perfil definido.
_PLANTILLAS_MANTENIMIENTO = {
    "formal": {
        "sujetos": _SUJETOS_NEUTROS + _SUJETOS_FORMAL_EXTRA,
        "acciones": _ACCIONES_NEUTRAS + _ACCIONES_FORMAL_EXTRA,
        "comentarios": _COMENTARIOS_NEUTROS + _COMENTARIOS_FORMAL_EXTRA,
    },
    "ciudadano": {
        "sujetos": _SUJETOS_NEUTROS + _SUJETOS_CIUDADANO_EXTRA,
        "acciones": _ACCIONES_NEUTRAS + _ACCIONES_CIUDADANO_EXTRA,
        "comentarios": _COMENTARIOS_NEUTROS + _COMENTARIOS_CIUDADANO_EXTRA,
    },
    "popular": {
        "sujetos": _SUJETOS_NEUTROS + _SUJETOS_POPULAR_EXTRA,
        "acciones": _ACCIONES_NEUTRAS + _ACCIONES_POPULAR_EXTRA,
        "comentarios": _COMENTARIOS_NEUTROS + _COMENTARIOS_POPULAR_EXTRA,
    },
    "generico": {
        "sujetos": _SUJETOS_NEUTROS + _SUJETOS_GENERICO_EXTRA,
        "acciones": _ACCIONES_NEUTRAS + _ACCIONES_GENERICO_EXTRA,
        "comentarios": _COMENTARIOS_NEUTROS + _COMENTARIOS_GENERICO_EXTRA,
    },
}

# Pools por partes para COMENTARIOS/RESPUESTAS: aperturas conversacionales y
# remates (los remates reutilizan los comentarios neutros + familia del tema).
_APERTURAS_COMENTARIO_FORMAL = (
    "De acuerdo con el planteamiento",
    "Punto valido",
    "Reflexion necesaria",
    "Buena aportacion",
    "Coincido en lo general",
    "Gracias por compartirlo",
    "El tema da para mas",
    "Lectura atenta",
)
_APERTURAS_COMENTARIO_CIUDADANO = (
    "Buen punto",
    "Totalmente de acuerdo",
    "Interesante lo que dices",
    "Asi es",
    "Tienes razon",
    "Vale la pena verlo asi",
    "Me parece justo",
    "Bien dicho",
)
_APERTURAS_COMENTARIO_POPULAR = (
    "x2",
    "jaja tienes razon",
    "que bueno que lo dices",
    "asi es",
    "bien dicho",
    "que buen punto",
    "duro con eso",
    "ya era hora",
)
_APERTURAS_COMENTARIO_GENERICO = (
    "Buen punto",
    "Interesante",
    "De acuerdo",
    "Vale la pena",
    "Bien dicho",
    "Gracias por compartir",
    "Coincido",
    "Buen aporte",
)

_PLANTILLAS_COMENTARIO = {
    "formal": {
        "aperturas": _APERTURAS_COMENTARIO_FORMAL,
        "remates": tuple(_COMENTARIOS_NEUTROS),
    },
    "ciudadano": {
        "aperturas": _APERTURAS_COMENTARIO_CIUDADANO,
        "remates": tuple(_COMENTARIOS_NEUTROS),
    },
    "popular": {
        "aperturas": _APERTURAS_COMENTARIO_POPULAR,
        "remates": tuple(_COMENTARIOS_NEUTROS),
    },
    "generico": {
        "aperturas": _APERTURAS_COMENTARIO_GENERICO,
        "remates": tuple(_COMENTARIOS_NEUTROS),
    },
}
_PLANTILLAS_MANTENIMIENTO["comentario"] = _PLANTILLAS_COMENTARIO

# Familias de vocabulario del tema (piezas propias por familia).
_PIEZAS_FAMILIA = {
    "clima": {
        "sujetos": (
            "el calor de estas tardes",
            "la lluvia que cayo ayer",
            "el clima loco de la ciudad",
            "las noches frescas",
            "el sol a mediodia",
            "el aire de la manana",
        ),
        "acciones": (
            "nos cambio el plan",
            "me tomo por sorpresa",
            "nos hizo buscar sombra",
            "me dejo mojado",
            "nos refresco el dia",
        ),
        "comentarios": (
            "hay que adaptarse al tiempo",
            "ojala se componga pronto",
            "no queda mas que llevarlo con calma",
            "asi esta la temporada",
            "mejor salir preparado",
        ),
    },
    "comida": {
        "sujetos": (
            "el cafe de la manana",
            "los tacos de la esquina",
            "la comida de casa",
            "el pan dulce del domingo",
            "los antojos de la tarde",
            "la receta de la abuela",
        ),
        "acciones": (
            "me levanto el animo",
            "nos junto en la mesa",
            "me dio buen sabor",
            "me dejo satisfecho",
            "nos dio tema de platica",
        ),
        "comentarios": (
            "no hay mejor plan",
            "eso si se disfruta",
            "vale la pena repetirlo",
            "asi sabe mejor",
            "me quedo con eso",
        ),
    },
    "deportes": {
        "sujetos": (
            "el partido del fin de semana",
            "la porra en la sala",
            "los entrenamientos del equipo",
            "la cancha del barrio",
            "el juego de anoche",
            "las carreras del domingo",
        ),
        "acciones": (
            "nos puso de pie",
            "me dejo sin voz",
            "nos unio a todos",
            "me lleno de energia",
            "me contagio la emocion",
        ),
        "comentarios": (
            "asi se vive la pasion",
            "que buen ambiente",
            "ojala se repita",
            "eso es puro equipo",
            "dan ganas de jugar",
        ),
    },
    "pantallas": {
        "sujetos": (
            "la serie que estoy viendo",
            "el capitulo de anoche",
            "las peliculas de siempre",
            "el estreno del viernes",
            "los videojuegos de la tarde",
            "el podcast de la manana",
        ),
        "acciones": (
            "me tuvo pegado a la pantalla",
            "me sorprendio bastante",
            "me dio risa",
            "nos dejo comentando",
            "me dejo pensando",
        ),
        "comentarios": (
            "denme recomendaciones",
            "valio la pena",
            "asi se pasa el rato",
            "no me lo esperaba",
            "quiero ver mas",
        ),
    },
    "calle": {
        "sujetos": (
            "el trafico de la manana",
            "las filas del banco",
            "el camion que no llega",
            "las obras de la avenida",
            "el metro a reventar",
            "la gasolina otra vez",
        ),
        "acciones": (
            "me quito tiempo",
            "me puso a pensar",
            "nos hizo esperar",
            "me saco de quicio",
            "me dio chance de leer",
        ),
        "comentarios": (
            "hay que salir temprano",
            "asi es esto",
            "toca tener paciencia",
            "mejor con musica",
            "ya ni modo",
        ),
    },
    "casa": {
        "sujetos": (
            "los vecinos de al lado",
            "la familia reunida",
            "la platica con mis abuelos",
            "la cuadra donde creci",
            "los domingos en casa",
            "el barrio de siempre",
        ),
        "acciones": (
            "me lleno de alegria",
            "nos junto a todos",
            "me trajo recuerdos",
            "me dio tranquilidad",
            "me regalo un buen rato",
        ),
        "comentarios": (
            "eso es lo que vale",
            "asi se vive mejor",
            "que buenos momentos",
            "nada como la familia",
            "hay que cuidarlos",
        ),
    },
    "mascotas": {
        "sujetos": (
            "mi perro jugueton",
            "la gata de casa",
            "el paseo con mi mascota",
            "los michis del patio",
            "mi lomito consentido",
        ),
        "acciones": (
            "me espera con alegria",
            "me saco a caminar",
            "me dio ternura",
            "me sigue a todos lados",
            "me alegra las mananas",
        ),
        "comentarios": (
            "son parte de la familia",
            "que cosa tan linda",
            "asi se alegra el dia",
            "no hay mejor compania",
            "merezco sus travesuras",
        ),
    },
    "oficina": {
        "sujetos": (
            "el trabajo acumulado",
            "las tareas de la escuela",
            "la junta de la manana",
            "el home office de hoy",
            "los examenes de la semana",
            "la chamba de siempre",
        ),
        "acciones": (
            "me dejo ocupado",
            "me puso a estudiar",
            "me quito tiempo",
            "me tuvo en la compu",
            "me dio aprendizaje",
        ),
        "comentarios": (
            "hay que organizarse",
            "todo se saca",
            "con calma avanza",
            "eso tambien suma",
            "un dia a la vez",
        ),
    },
    "descanso": {
        "sujetos": (
            "las desveladas de la semana",
            "el insomnio de ayer",
            "los planes del fin de semana",
            "la flojera del domingo",
            "el descanso pendiente",
        ),
        "acciones": (
            "me dejo cansado",
            "me puso a pensar",
            "me dio flojera",
            "me recargo pilas",
            "me ayudo a descansar",
        ),
        "comentarios": (
            "hoy toca recuperarse",
            "a dormir temprano",
            "el cuerpo lo pide",
            "nada como descansar",
            "manana sera otro dia",
        ),
    },
    "musica": {
        "sujetos": (
            "la musica de siempre",
            "la cancion que no me cansa",
            "los boleros del abuelo",
            "la playlist del camino",
            "el concierto del barrio",
        ),
        "acciones": (
            "me acompano toda la tarde",
            "me sube el animo",
            "me trae recuerdos",
            "me pone a bailar",
            "me dio tranquilidad",
        ),
        "comentarios": (
            "que buenas rolas",
            "asi suena mejor el dia",
            "pongan mas musica",
            "no me canso de oirla",
            "me encanta ese ritmo",
        ),
    },
    "animo": {
        "sujetos": (
            "las ganas de salir adelante",
            "los recuerdos bonitos",
            "la amistad de anos",
            "las metas del ano",
            "los animos de la gente",
        ),
        "acciones": (
            "me impulsan",
            "me motivan",
            "me acompanan",
            "me dan fuerza",
            "me llenan de ganas",
        ),
        "comentarios": (
            "hay que seguir",
            "todo llega a su tiempo",
            "eso es lo que importa",
            "nunca hay que rendirse",
            "paso a paso",
        ),
    },
    "humor": {
        "sujetos": (
            "los memes del grupo",
            "la risa de los compas",
            "el chisme del barrio",
            "los videos chistosos",
            "la carrilla entre amigos",
        ),
        "acciones": (
            "me alegro el dia",
            "me saco una carcajada",
            "nos hizo reir",
            "me quito el mal humor",
            "me dibujo una sonrisa",
        ),
        "comentarios": (
            "jajaja que bueno",
            "asi se lleva la vida",
            "no manches que risa",
            "pasen mas memes",
            "ya me rei bastante",
        ),
    },
    "viajes": {
        "sujetos": (
            "el viaje del fin de semana",
            "la carretera y el paisaje",
            "la playa en temporada",
            "las ganas de conocer",
            "el pueblito de mis padres",
        ),
        "acciones": (
            "me llena de ilusion",
            "nos dio aventura",
            "me relajo",
            "me dejo recuerdos",
            "me invita a planear",
        ),
        "comentarios": (
            "hay que viajar mas",
            "que ganas de escaparse",
            "ojala pronto",
            "eso si es descanso",
            "me apunto",
        ),
    },
    "generico": {
        "sujetos": (
            "el dia a dia",
            "las cosas que pasan",
            "la vida de todos los dias",
            "los momentos simples",
            "el andar de la gente",
        ),
        "acciones": (
            "da para pensar",
            "deja una ensenanza",
            "invita a la conversacion",
            "se disfruta distinto",
            "acerca a la gente",
        ),
        "comentarios": (
            "asi es la vida",
            "hay que aprovechar",
            "todo cuenta",
            "un dia a la vez",
            "mejor disfrutarlo",
        ),
    },
}

# Tema -> familia de vocabulario (los temas legacy caen a "generico").
_FAMILIA_POR_TEMA = {
    "azteca": "generico",
    "dia": "generico",
    "tendencias": "generico",
    "gustos": "generico",
    "trafico": "calle",
    "clima": "clima",
    "comida": "comida",
    "series": "pantallas",
    "lunes": "descanso",
    "insomnio": "descanso",
    "futbol": "deportes",
    "memes": "humor",
    "motivacion": "animo",
    "mascotas": "mascotas",
    "cafe": "comida",
    "musica": "musica",
    "vecinos": "casa",
    "transporte": "calle",
    "filas": "calle",
    "calor": "clima",
    "lluvia": "clima",
    "finde": "descanso",
    "deportes": "deportes",
    "recuerdos": "animo",
    "escuela": "oficina",
    "trabajo": "oficina",
    "home_office": "oficina",
    "super": "comida",
    "cocina": "comida",
    "postres": "comida",
    "tacos": "comida",
    "netflix": "pantallas",
    "videojuegos": "pantallas",
    "podcast": "pantallas",
    "libros": "pantallas",
    "viajes": "viajes",
    "carretera": "viajes",
    "playa": "viajes",
    "barrio": "casa",
    "familia": "casa",
    "abuelos": "casa",
    "amistad": "animo",
}

_INTENTOS_PLANTILLA_LOCAL = 40
_INTENTOS_TEXTO_LOCAL = 25
_MUESTRAS_PLANTILLAS = 8


def _hash_estable(texto: str) -> int:
    """Hash determinista (NO depende de PYTHONHASHSEED) para sembrar textos."""
    h = 0
    for ch in str(texto or ""):
        h = (h * 131 + ord(ch)) & 0x7FFFFFFF
    return h


def _semilla_plantilla(i: int, j: int, usuario: str = "") -> int:
    """Semilla estable por (cuenta, texto, usuario): 200 cuentas no comparten."""
    try:
        base = (int(i) + 1) * 100003 + (int(j) + 1) * 1009
    except Exception:
        base = 7
    return (base * 31 + _hash_estable(usuario)) & 0x7FFFFFFF


# Palabras de funcion/genericas que NO cuentan como "palabras clave" de
# diversidad (todas de 5+ letras; las de <=4 quedan fuera por longitud).
_STOPWORDS_DIVERSIDAD = frozenset({
    "siempre", "nunca", "todos", "todas", "tengo", "tiene", "tienen",
    "vamos", "nuestra", "nuestro", "nuestros", "nuestras", "estamos",
    "estaba", "estaban", "despues", "tambien", "mucho", "mucha", "muchos",
    "muchas", "poco", "pocos", "pocas", "otra", "otras", "otro", "otros",
    "mejor", "mejora", "cosas", "cosa", "parte", "partes", "gente",
    "puede", "pueden", "quiero", "quiere", "hacer", "hacemos", "hecho",
    "hechos", "decir", "dice", "dicen", "luego", "donde", "cuando",
    "porque", "aunque", "mismo", "misma", "mismos", "mismas", "sobre",
    "entre", "desde", "hasta", "para", "como", "esto", "estas", "estos",
    "esas", "esos", "aquel", "aquella", "aqui", "alla", "alli", "antes",
    "ahora", "ayer", "manana", "tarde", "noche", "noches", "semana",
    "punto", "puntos", "bueno", "buena", "buenas", "verdad", "diaria",
    "diario",
})


def _palabras_clave(texto, minimo: int = 5) -> set:
    """Palabras clave normalizadas (>= ``minimo`` letras, sin stopwords).

    Normaliza a minusculas sin acentos/diacriticos ni signos y descarta las
    palabras de funcion de ``_STOPWORDS_DIVERSIDAD``. Los hashtags/@menciones
    cuentan por su palabra (``#HomeOffice`` -> ``homeoffice``). Es la base de
    la garantia "cero palabras compartidas por cuenta" del fallback local.
    Nunca lanza: ante cualquier error devuelve un set vacio.
    """
    try:
        t = str(texto or "").lower()
        plano = unicodedata.normalize("NFKD", t)
        plano = "".join(c for c in plano if not unicodedata.combining(c))
        plano = plano.replace("ñ", "n")
        palabras = re.findall(r"[a-z0-9]+", plano)
    except Exception:
        return set()
    try:
        limite = max(1, int(minimo))
    except (TypeError, ValueError):
        limite = 5
    return {
        p for p in palabras
        if len(p) >= limite and p not in _STOPWORDS_DIVERSIDAD
    }


def _familia_de_tema(tema: str) -> str:
    """Familia de vocabulario del tema (tema vacio/desconocido -> generico)."""
    try:
        clave = _normalizar_tema(tema)
    except Exception:
        clave = ""
    if not clave:
        return "generico"
    return _FAMILIA_POR_TEMA.get(clave, "generico")


def _hashtag_de_tema(tema: str) -> str:
    """Hashtag determinista del tema ('' si no hay): home_office -> #HomeOffice."""
    try:
        clave = _normalizar_tema(tema)
    except Exception:
        clave = ""
    partes = re.findall(r"[a-z0-9]+", clave or "")
    if not partes:
        return ""
    return "#" + "".join(p[:1].upper() + p[1:] for p in partes)


# Hashtags de respaldo cuando el del tema choca con palabras ya usadas por la
# misma cuenta (p. ej. tema "recuerdos" y un texto anterior que dijo
# "me dejo recuerdos"): se elige uno determinista que no comparta palabras.
_HASHTAGS_ALTERNOS = (
    "#Comunidad",
    "#Mexico",
    "#Ciudad",
    "#Pueblo",
    "#Barrio",
    "#Informacion",
    "#Actualidad",
    "#Opinion",
    "#Conversacion",
    "#Cultura",
)


def _hashtag_alternativo(evitar, semilla: int = 0) -> str:
    """Hashtag de respaldo determinista sin palabras en ``evitar`` (nunca lanza)."""
    try:
        base = int(semilla)
    except Exception:
        base = 0
    try:
        usadas = set(evitar or ())
    except Exception:
        usadas = set()
    total = len(_HASHTAGS_ALTERNOS)
    for desplazamiento in range(total):
        tag = _HASHTAGS_ALTERNOS[(base + desplazamiento) % total]
        if not (_palabras_clave(tag) & usadas):
            return tag
    return _HASHTAGS_ALTERNOS[base % total]


def _elegir_pieza(rng, perfil_key: str, familia: str, tipo: str) -> str:
    """Elige una pieza del pool: prioriza la familia del tema sobre el perfil."""
    fam = _PIEZAS_FAMILIA.get(familia) or {}
    propias = list(fam.get(tipo) or ())
    perfil_piezas = _PLANTILLAS_MANTENIMIENTO.get(perfil_key) or {}
    comunes = list(perfil_piezas.get(tipo) or ())
    if propias and (not comunes or rng.random() < 0.7):
        opciones = propias
    elif comunes:
        opciones = comunes
    else:
        opciones = propias
    if not opciones:
        return ""
    return rng.choice(opciones)


def _componer_post_local(rng, perfil_key: str, familia: str) -> str:
    """Ensambla un POST local (Sujeto + Accion + Comentario) segun el perfil."""
    sujeto = _elegir_pieza(rng, perfil_key, familia, "sujetos") or "el dia de hoy"
    accion = _elegir_pieza(rng, perfil_key, familia, "acciones") or "da para pensar"
    comentario = (
        _elegir_pieza(rng, perfil_key, familia, "comentarios")
        or "asi es la vida"
    )
    if perfil_key == "formal":
        titulo = sujeto[:1].upper() + sujeto[1:]
        cierre = (
            _elegir_pieza(rng, perfil_key, familia, "comentarios") or comentario
        )
        cierre = cierre[:1].upper() + cierre[1:]
        return f"{titulo}\n\n{sujeto} {accion}, y {comentario}.\n\n{cierre}."
    if perfil_key == "ciudadano":
        arranque = rng.choice(("La verdad, ", "Pues ", "Mira, ", ""))
        return f"{arranque}{sujeto} {accion}; {comentario}."
    if perfil_key == "popular":
        marco = rng.choice((
            f"{sujeto} {accion} y {comentario}, pa q mas",
            f"tons {sujeto} {accion}; {comentario}",
            f"{sujeto} {accion} y {comentario}, xq si",
            f"k {sujeto} {accion}; {comentario}",
            f"{sujeto} {accion}, {comentario} jajaja",
        ))
        if not marco.startswith(("k ", "tons ", "xq ", "q ", "pa ")):
            marco = marco[:1].upper() + marco[1:]
        return marco
    return f"{sujeto} {accion}. {comentario}."


def _componer_comentario_local(rng, perfil_key: str, familia: str) -> str:
    """Ensambla un COMENTARIO/RESPUESTA breve y conversacional (sin hashtags)."""
    aperturas = list(
        (_PLANTILLAS_COMENTARIO.get(perfil_key) or {}).get("aperturas") or ()
    )
    apertura = rng.choice(aperturas) if aperturas else "Buen punto"
    fam = _PIEZAS_FAMILIA.get(familia) or {}
    remates_familia = list(fam.get("comentarios") or ())
    comunes = list(
        (_PLANTILLAS_MANTENIMIENTO.get(perfil_key) or {}).get("comentarios") or ()
    )
    if remates_familia and (not comunes or rng.random() < 0.6):
        remate = rng.choice(remates_familia)
    elif comunes:
        remate = rng.choice(comunes)
    else:
        remate = "vale la pena conversarlo"
    if perfil_key == "formal":
        cierre = rng.choice(comunes) if comunes else remate
        return (
            f"{apertura}\n\n{remate[:1].upper() + remate[1:]}.\n\n"
            f"{cierre[:1].upper() + cierre[1:]}."
        )
    if perfil_key == "popular":
        return f"{apertura}, {remate}, pa q mas"
    return f"{apertura}: {remate}."


def _generar_plantilla_local(
    perfil: str = "",
    tema: str = "",
    semilla: int = 0,
    comentario: bool = False,
    evitar=None,
) -> str:
    """Texto local dinamico por PARTES (Sujeto + Accion + Comentario).

    - Determinista: misma (perfil, tema, semilla, comentario, evitar) -> mismo
      texto (``random.Random`` sembrado).
    - Theme-aware: mapea el tema a una FAMILIA de vocabulario y prefiere sus
      piezas; tema vacio/desconocido usa los pools genericos del perfil.
    - Respeta el FORMATO del perfil: formal = 3 bloques con doble enter;
      ciudadano = 1-2 frases; popular = 1 renglon casual con faltas
      intencionales (q/pa/xq/tons/k); generico = 1-2 frases neutras.
    - POSTS: agrega un hashtag determinista del tema (el llamador lo reubica EN
      MEDIO y aplica el limite de 100). COMENTARIOS: sin hashtags.
    - ``evitar`` (set de palabras clave ya usadas por la MISMA cuenta/campana):
      reintenta la combinacion (tope de intentos) hasta no compartir ninguna.
    - NUNCA lee ``narrativa`` (garantia anti-fuga). Nunca lanza.
    """
    try:
        clave = normalizar_perfil(perfil) or "generico"
    except Exception:
        clave = "generico"
    etiqueta = _PLANTILLAS_MANTENIMIENTO.get(clave)
    if not etiqueta:
        clave = "generico"
    try:
        rng = random.Random(int(semilla))
    except Exception:
        rng = random.Random(0)
    try:
        evitar_norm = {
            str(w).strip().lower() for w in (evitar or ()) if str(w).strip()
        }
    except Exception:
        evitar_norm = set()
    familia = _familia_de_tema(tema)
    hashtag = "" if comentario else _hashtag_de_tema(tema)
    if hashtag and (_palabras_clave(hashtag) & evitar_norm):
        # El hashtag del tema choca con palabras ya usadas por la cuenta:
        # se usa un hashtag de respaldo determinista que no comparta palabras.
        hashtag = _hashtag_alternativo(evitar_norm, semilla)
    texto = ""
    for _intento in range(_INTENTOS_PLANTILLA_LOCAL):
        if comentario:
            texto = _componer_comentario_local(rng, clave, familia)
        else:
            texto = _componer_post_local(rng, clave, familia)
        if hashtag:
            texto = f"{texto} {hashtag}"
        claves = _palabras_clave(texto)
        if not (claves & evitar_norm):
            return texto.strip()
    return (texto or "").strip()


def _normalizar_texto_local(
    texto: str, registro, semilla: int = 0, es_comentario: bool = False
) -> str:
    """Humaniza por registro y normaliza un texto local (nunca lanza).

    POSTS: hashtag integrado EN MEDIO (el texto local ya trae uno estable del
    tema) y corte DURO de 100 caracteres. COMENTARIOS: sin hashtags, links ni
    @menciones (spam-safe para X) y sin limite de 100.
    """
    t = (texto or "").strip()
    if not t:
        return t
    try:
        t = _humanizar_por_registro(t, registro, semilla=semilla)
    except Exception:
        pass
    try:
        t = _quitar_signos_por_registro(t, registro)
    except Exception:
        pass
    if es_comentario:
        try:
            t = limpiar_comentario_spam(t)
        except Exception:
            pass
    else:
        t = _con_hashtag_en_medio(t)
        t = _acotar_limite(t)
    return t


def _texto_local_diverso(
    *,
    perfil: str = "",
    registro: str = "",
    tema: str = "",
    es_comentario: bool = False,
    nombre: str = "",
    personalidad: str = "",
    semilla_base: int = 0,
    evitar=None,
    vistos=None,
    vistos_global=None,
) -> str:
    """Genera UN texto local unico y sin palabras compartidas (nunca lanza).

    Reintenta con semillas distintas hasta encontrar un texto que (a) no
    comparta palabras clave con ``evitar`` (otros textos de la MISMA cuenta),
    (b) no este en ``vistos`` (misma cuenta) ni en ``vistos_global`` (toda la
    campana). Si no lo logra devuelve el ultimo candidato normalizado (nunca
    vacio en la practica: hay respaldo final). El texto NUNCA lee narrativa.
    """
    try:
        evitar = set(evitar or ())
    except Exception:
        evitar = set()
    vistos = vistos if isinstance(vistos, set) else set()
    vistos_global = vistos_global if isinstance(vistos_global, set) else set()
    pie = _pie_personal(nombre, personalidad)
    ultimo = ""
    for intento in range(_INTENTOS_TEXTO_LOCAL):
        try:
            semilla = int(semilla_base) + intento * 7919
        except Exception:
            semilla = intento * 7919
        with_pie = bool(pie) and (intento % 2 == 0)
        try:
            bruto = _generar_plantilla_local(
                perfil=perfil,
                tema=tema,
                semilla=semilla,
                comentario=es_comentario,
                evitar=evitar,
            )
        except Exception:
            bruto = ""
        if not bruto:
            continue
        if with_pie:
            # El pie personal aporta unicidad, pero en POSTS no debe empujar
            # el texto mas alla del limite de 100 (se omitiria a medias).
            if es_comentario or len(bruto) + len(pie) <= 100:
                bruto = f"{bruto}{pie}"
        t = _normalizar_texto_local(bruto, registro, semilla, es_comentario)
        if not t:
            continue
        ultimo = t
        if t in vistos or t in vistos_global:
            continue
        if _palabras_clave(t) & evitar:
            continue
        return t
    if ultimo:
        return ultimo
    try:
        return _normalizar_texto_local(
            "El dia de hoy da para pensar. #Comunidad",
            registro,
            semilla_base,
            es_comentario,
        ) or "Buen punto, vale la pena conversarlo."
    except Exception:
        return "Buen punto, vale la pena conversarlo."


def _plantillas_por_perfil(tema: str, perfil: str = "") -> tuple:
    """Muestras locales de mantenimiento del tema/perfil (nunca lanza).

    Wrapper fino de `_generar_plantilla_local`: devuelve muestras deterministas
    (semillas 0..M-1) con el formato del perfil y hashtag en medio, en vez de
    la lista rigida antigua. Conserva la firma publica.
    """
    muestras = []
    for semilla in range(_MUESTRAS_PLANTILLAS):
        try:
            t = _generar_plantilla_local(perfil=perfil, tema=tema, semilla=semilla)
            if t:
                t = _acotar_limite(_con_hashtag_en_medio(t))
        except Exception:
            t = ""
        if t and t not in muestras:
            muestras.append(t)
    return tuple(muestras)


def _plantillas_comentario(perfil: str = "") -> tuple:
    """Muestras locales de comentario para el perfil (nunca lanza).

    Wrapper fino de `_generar_plantilla_local(comentario=True)`: textos
    breves y conversacionales, sin hashtags/links/@menciones.
    """
    muestras = []
    for semilla in range(_MUESTRAS_PLANTILLAS):
        try:
            t = _generar_plantilla_local(
                perfil=perfil, tema="", semilla=semilla, comentario=True
            )
            t = limpiar_comentario_spam(t)
        except Exception:
            t = ""
        if t and t not in muestras:
            muestras.append(t)
    return tuple(muestras)


def _pie_personal(nombre: str = "", personalidad: str = "") -> str:
    """Cierre humano que refleja la personalidad (o el nombre) de la cuenta."""
    personalidad = " ".join((personalidad or "").split())
    if personalidad:
        frase = re.split(r"(?<=[.!?])\s+|\n", personalidad)[0].strip()
        if len(frase) > 110:
            frase = frase[:107].rstrip() + "..."
        if frase:
            return f"\n\nAsí soy: {frase}"
    nombre = " ".join((nombre or "").split())
    if nombre:
        return f"\n\n— {nombre}"
    return ""


def _variar_hasta_unico(texto: str, vistos: set, intentos: int = 8) -> str:
    """Devuelve un texto no vacio que no este en `vistos` (best effort).

    Usa `activaciones.variaciones` con import perezoso; si no alcanza,
    agrega cierres naturales y, en ultimo caso, un sufijo numerado.
    """
    texto = (texto or "").strip()
    if not texto:
        texto = "Buen dia a todos."
    if texto not in vistos:
        return texto

    try:
        from activaciones.variaciones import variar_texto
        for _ in range(max(1, intentos)):
            nuevo = (variar_texto(texto, n_hashtags=0) or "").strip()
            if nuevo and nuevo not in vistos:
                return nuevo
    except Exception as e:
        logger.error(f"Error variando texto local de mantenimiento: {e}")

    for cierre in (
        "\n\nSaludos.",
        "\n\nBuen dia.",
        "\n\nAsi las cosas.",
        "\n\nUn abrazo.",
    ):
        nuevo = f"{texto}{cierre}".strip()
        if nuevo not in vistos:
            return nuevo

    sufijo = 2
    while True:
        nuevo = f"{texto} ({sufijo})"
        if nuevo not in vistos:
            return nuevo
        sufijo += 1


def _normalizar_accion(tipo_accion, forzada: str = "") -> str:
    """Devuelve "comentario" o "post" a partir de ``tipo_accion`` (nunca lanza)."""
    try:
        accion = str(forzada or tipo_accion or "").strip().lower()
    except Exception:
        return "post"
    if accion.startswith(("coment", "resp", "reply", "contest")):
        return "comentario"
    return "post"


def _info_cuenta(cuentas_info, indice: int) -> dict:
    """Dict de la cuenta ``indice`` (o {} si no aplica); nunca lanza."""
    try:
        if 0 <= indice < len(cuentas_info):
            info = cuentas_info[indice]
            if isinstance(info, dict):
                return info
    except Exception:
        pass
    return {}


def _con_hashtag_en_medio(texto: str) -> str:
    """Garantiza un hashtag integrado en MEDIO (nunca al final); nunca lanza."""
    t = (texto or "").strip()
    if not t:
        return t
    try:
        colocado = colocar_hashtag_en_medio(t)
    except Exception as e:
        logger.error(f"Error colocando hashtag en medio: {e}")
        colocado = ""
    if colocado and tiene_hashtag(colocado):
        return colocado

    # Ultra-fallback: inserta el hashtag en el punto medio del texto.
    try:
        tag = elegir_hashtag(t)
        mitad = len(t) // 2
        espacios = [m.start() for m in re.finditer(r"\s", t)]
        if espacios:
            punto = min(espacios, key=lambda p: abs(p - mitad))
            return f"{t[:punto].rstrip()} {tag} {t[punto:].lstrip()}".strip()
        corte = max(1, mitad)
        return f"{t[:corte]} {tag} {t[corte:]}".strip()
    except Exception:
        return t


# Cierre conversacional de ultimo recurso para comentarios: X marca las
# respuestas con hashtags, links o @menciones como "probable spam".
_CIERRE_COMENTARIO_SPAM = "Buen punto, vale la pena conversarlo."


def _limpiar_espacios_y_saltos(texto: str) -> str:
    """Limpia espacios antes de puntuacion, dobles y saltos 3+; nunca lanza."""
    t = str(texto or "")
    t = re.sub(r"[ \t]+([.,;:!?])", r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = "\n".join(linea.strip() for linea in t.splitlines())
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def limpiar_comentario_spam(texto: str) -> str:
    """Deja un comentario/respuesta apto para X (spam-safe).

    X marca como "probable spam" las respuestas que llevan hashtags, links o
    @menciones. Este helper los elimina SIEMPRE, limpia los espacios huerfanos
    (``" ,"`` -> ``,``; dobles espacios; saltos 3+) y garantiza un texto
    conversacional no vacio: si tras limpiar quedan menos de 8 caracteres
    utiles, devuelve un cierre generico corto. Tras esta funcion el texto NO
    contiene ``#``, ``http``, ``www.`` ni ``@``. Nunca lanza.
    """
    try:
        t = str(texto or "")
        t = re.sub(r"https?://\S+", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"\bhttps?\b[^\s]*", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"www\.\S+", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", t)
        t = re.sub(r"@[A-Za-z0-9_]+", " ", t)
        t = _limpiar_espacios_y_saltos(t)
    except Exception as e:
        logger.error(f"Error limpiando comentario spam-safe: {e}")
        t = ""
    if len(re.sub(r"\s+", "", t)) < 8:
        return _CIERRE_COMENTARIO_SPAM
    return t


# ===================================================================== #
# Estilos locales por REGISTRO (fallback sin OpenAI):
# - ciudadana: persona real "sin estudios" -> malos signos de puntuacion
#   (casi sin comas/puntos, nunca "¿"/"¡") + 4-7 errores legibles.
# - activista: tecnico-coloquial -> 2-3 errores ortograficos leves y
#   NUNCA signos de apertura "¿"/"¡".
# - politica: no se toca localmente (ortografia cuidada, maximo 1 error
#   leve opcional permitido por el prompt).
# ===================================================================== #
_MAPA_SIN_TILDES = str.maketrans({
    "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u",
    "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U",
})

# (patron_regex, reemplazo): se aplican con probabilidad por semilla.
_ABREVIATURAS_CIUDADANO = (
    (r"\bque\b", "q"),
    (r"\bporque\b", "xq"),
    (r"\bpara\b", "pa"),
    (r"\btambién\b", "tmb"),
    (r"\btambien\b", "tmb"),
    (r"\bbueno\b", "weno"),
)

# Dislexias legibles (una por palabra como maximo, ver funcion).
_DISLEXIAS_CIUDADANO = (
    # h muda omitida / de mas
    (r"\bhola\b", "ola"),
    (r"\bhacer\b", "aser"),
    (r"\bhace\b", "ase"),
    (r"\bhasta\b", "asta"),
    (r"\bhay\b", "ay"),
    # s/c/z
    (r"\bvez\b", "ves"),
    (r"\bgracias\b", "grasias"),
    (r"\bcasa\b", "casa"),  # testigo (no cambia, evita sobre-corregir)
    (r"\bcansado\b", "kansado"),
    # b/v
    (r"\bhaber\b", "aver"),
    (r"\ba ver\b", "aver"),
    (r"\bvolver\b", "bolber"),
    (r"\bbueno\b", "bueno"),  # testigo
    # haya -> haiga (h + y/ll)
    (r"\bhaya\b", "haiga"),
    # y/ll
    (r"\bllegó\b", "yego"),
    (r"\bllego\b", "yego"),
    (r"\bcalle\b", "caye"),
    # g/j
    (r"\bgente\b", "jente"),
    (r"\bméxico\b", "mejico"),
    (r"\bmexico\b", "mejico"),
)

# Errores ORTOGRAFICOS leves del registro activista (2-3 por texto).
# Maximo 1 por palabra; ninguno impide entender el mensaje.
_ERRORES_ACTIVISTA = (
    (r"\bhola\b", "ola"),
    (r"\bhacer\b", "aser"),
    (r"\bhace\b", "ase"),
    (r"\bhasta\b", "asta"),
    (r"\bhay\b", "ay"),
    (r"\bhaber\b", "aver"),
    (r"\bvez\b", "ves"),
    (r"\bgracias\b", "grasias"),
    (r"\btambién\b", "tmbn"),
    (r"\btambien\b", "tmbn"),
    (r"\bllegó\b", "yego"),
    (r"\bllego\b", "yego"),
    (r"\bcalle\b", "caye"),
    (r"\bgente\b", "jente"),
)

# Licencias de redes del activista (q/pa/xq/tons/k ocasionales). Solo se
# aplican si los errores ortograficos de arriba no alcanzaron el objetivo.
_LICENCIAS_ACTIVISTA = (
    (r"\bque\b", "q"),
    (r"\bporque\b", "xq"),
    (r"\bpara\b", "pa"),
    (r"\bentonces\b", "tons"),
)

_MULETILLAS_CIUDADANO_INICIO = (
    "la neta ",
    "no manches, ",
    "tons ",
    "pues ",
    "oigan, ",
    "",
    "",
)

_MULETILLAS_CIUDADANO_CIERRE = (
    " la neta",
    ", no manches",
    " tons",
    " pues",
    "",
    "",
)


# ------------------------------------------------------------------ #
# Edits genericos de un solo error leve. Devuelven
# (texto, aplicado, palabra_tocada) y aceptan `evitar` (palabras ya
# editadas) para no revertir ni encadenar errores sobre la misma palabra
# (maximo 1 error por palabra).
# ------------------------------------------------------------------ #

# Palabras de funcion que NUNCA se deforman (p.ej. "se"->"ce", "es"->"ec"):
# deformarlas vuelve ilegible el texto. Todos los edits genericos las
# saltan y buscan la siguiente candidata.
_PALABRAS_FUNCION = {
    "se", "es", "son", "era", "un", "una", "unos", "unas",
    "el", "la", "los", "las", "lo", "de", "del", "al", "en", "con",
    "sin", "por", "para", "su", "sus", "mi", "mis", "tu", "tus",
    "no", "si", "ya", "mas", "más", "muy", "me", "le", "les", "nos",
    "da", "di", "ni", "tan", "te", "os",
}


def _es_palabra_funcion(palabra) -> bool:
    """True si la palabra (lower, sin signos) es de funcion (nunca lanza)."""
    try:
        limpia = re.sub(
            r"^[^\wáéíóúüñÁÉÍÓÚÜÑ]+|[^\wáéíóúüñÁÉÍÓÚÜÑ]+$",
            "",
            str(palabra or ""),
        ).lower()
    except Exception:
        return False
    return limpia in _PALABRAS_FUNCION


def _error_h_muda(texto: str, evitar: set | None = None):
    """Quita una h muda inicial ('hacer'->'acer', 'hay'->'ay').

    Devuelve (texto, aplicado, palabra_nueva); la palabra nueva es la que se
    registra en `evitar` para que otro edit no la revierta.
    """
    try:
        for m in re.finditer(r"\b[Hh][aeiouáéíóú]\w*", texto):
            palabra = m.group(0)
            # "ha"/"he" (2 letras) se conservan; "hay"/"hola" si se editan.
            if len(palabra) < 3:
                continue
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = palabra[1:]
            return (
                texto[:m.start()] + nueva + texto[m.end():], True, nueva
            )
    except Exception:
        pass
    return (texto, False, "")


def _error_ll_y(texto: str, evitar: set | None = None):
    """Cambia una 'll' por 'y' ('calle'->'caye', 'llegar'->'yegar')."""
    try:
        for m in re.finditer(r"\w*ll\w*", texto):
            palabra = m.group(0)
            if "\x00" in palabra:
                continue
            if len(palabra) < 4:
                continue
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = palabra.replace("ll", "y", 1)
            return (
                texto[:m.start()] + nueva + texto[m.end():], True, nueva
            )
    except Exception:
        pass
    return (texto, False, "")


def _error_c_s(texto: str, evitar: set | None = None):
    """Cambia c+e/i por s ('hacer'->'haser', 'gracias'->'grasias')."""
    try:
        for m in re.finditer(r"\b[Cc][eiéí]\w*", texto):
            palabra = m.group(0)
            if len(palabra) < 4:
                continue
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = "s" + palabra[1:]
            return (
                texto[:m.start()] + nueva + texto[m.end():], True, nueva
            )
    except Exception:
        pass
    return (texto, False, "")


def _error_s_c(texto: str, evitar: set | None = None):
    """Inversa s+e/i -> c ('sentirse'->'centirse'); s/c/z leve."""
    try:
        for m in re.finditer(r"\b[Ss][eiéí]\w*", texto):
            palabra = m.group(0)
            if len(palabra) < 4:
                continue
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = "c" + palabra[1:]
            return (
                texto[:m.start()] + nueva + texto[m.end():], True, nueva
            )
    except Exception:
        pass
    return (texto, False, "")


def _error_bv(texto: str, evitar: set | None = None):
    """Intercambia b<->v en UNA palabra ('volver'->'bolber', 'base'->'vase')."""
    try:
        for m in re.finditer(r"\b\w*[bvBV]\w*\b", texto):
            palabra = m.group(0)
            if len(palabra) <= 3:
                continue
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            if "b" in palabra.lower():
                nueva = palabra.replace("b", "v", 1).replace("B", "V", 1)
            else:
                nueva = palabra.replace("v", "b", 1).replace("V", "B", 1)
            if nueva != palabra:
                return (
                    texto[:m.start()] + nueva + texto[m.end():], True, nueva
                )
    except Exception:
        pass
    return (texto, False, "")


def _error_tilde(texto: str, evitar: set | None = None):
    """Omite UNA tilde ('también'->'tambien'); error leve permitido."""
    try:
        for m in re.finditer(r"\w*[áéíóú]\w*", texto):
            palabra = m.group(0)
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = re.sub(
                r"[áéíóú]",
                lambda c: _MAPA_SIN_TILDES.get(c.group(0), c.group(0)),
                palabra,
                count=1,
            )
            if nueva != palabra:
                return (
                    texto[:m.start()] + nueva + texto[m.end():], True, nueva
                )
    except Exception:
        pass
    return (texto, False, "")


def _error_que_k(texto: str, evitar: set | None = None):
    """Cambia 'que'/'qué' por 'ke' y la 'q' suelta por 'k' (licencia k)."""
    try:
        for m in re.finditer(r"\bqu[eé]\w*|\bq\b", texto):
            palabra = m.group(0)
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = "ke" if palabra.lower() in ("que", "qué") else "k"
            if nueva != palabra:
                return (
                    texto[:m.start()] + nueva + texto[m.end():], True, nueva
                )
    except Exception:
        pass
    return (texto, False, "")


def _error_g_j(texto: str, evitar: set | None = None):
    """Cambia g+e/i por j ('gente'->'jente', 'general'->'jeneral')."""
    try:
        for m in re.finditer(r"\b[Gg][eiéí]\w*", texto):
            palabra = m.group(0)
            if len(palabra) < 4:
                continue
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = "j" + palabra[1:]
            return (
                texto[:m.start()] + nueva + texto[m.end():], True, nueva
            )
    except Exception:
        pass
    return (texto, False, "")


def _error_z_s(texto: str, evitar: set | None = None):
    """Cambia una z final de palabra por s ('vez'->'ves')."""
    try:
        for m in re.finditer(r"\b\w*z\b", texto):
            palabra = m.group(0)
            # Minimo 3 letras: "vez"->"ves" se conserva como licencia.
            if len(palabra) < 3:
                continue
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = palabra[:-1] + "s"
            return (
                texto[:m.start()] + nueva + texto[m.end():], True, nueva
            )
    except Exception:
        pass
    return (texto, False, "")


def _error_s_final(texto: str, evitar: set | None = None):
    """Quita la s final de una palabra larga ('tenemos'->'tenemo', 'cosas'->'cosa').

    Error tipico de escritura con poca escuela; nunca toca palabras funcion,
    hashtags ni palabras ya editadas. Exige >=5 letras para no romper plurales
    cortos que ya estan protegidos como funcion.
    """
    try:
        for m in re.finditer(r"\b\w{5,}s\b", texto):
            palabra = m.group(0)
            if "\x00" in palabra:
                continue
            if _es_palabra_funcion(palabra):
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = palabra[:-1]
            return (
                texto[:m.start()] + nueva + texto[m.end():], True, nueva
            )
    except Exception:
        pass
    return (texto, False, "")


def _error_qu_k(texto: str, evitar: set | None = None):
    """Cambia la 'qu' de una palabra por 'k' ('quiero'->'kiero', 'aqui'->'aki').

    Licencia tipica de redes; exige >=4 letras y respeta palabras funcion.
    """
    try:
        for m in re.finditer(r"\b\w*[Qq]u[eiéíí]\w*", texto):
            palabra = m.group(0)
            if "\x00" in palabra:
                continue
            if _es_palabra_funcion(palabra):
                continue
            if len(re.sub(r"\W", "", palabra)) < 4:
                continue
            if evitar and palabra.lower() in evitar:
                continue
            nueva = re.sub(
                r"[Qq]u", "k" if palabra[0].islower() else "K", palabra, count=1
            )
            if nueva == palabra:
                continue
            return (
                texto[:m.start()] + nueva + texto[m.end():], True, nueva
            )
    except Exception:
        pass
    return (texto, False, "")


# Edits genericos usados por los estilos locales (cada uno aplica como
# maximo UNA vez por texto, sobre una palabra distinta).
_GENERICOS_ESTILO = (
    _error_h_muda,
    _error_c_s,
    _error_s_c,
    _error_ll_y,
    _error_bv,
    _error_g_j,
    _error_z_s,
    _error_s_final,
    _error_qu_k,
    _error_que_k,
)


def _aplicar_genericos_estilo(
    cuerpo: str,
    restantes: int,
    evitar: set,
    rng,
    incluir_tilde: bool = False,
):
    """Aplica hasta ``restantes`` edits genericos (1 por palabra); nunca lanza.

    Devuelve ``(texto, aplicados)``. Cada funcion aplica una sola vez y las
    palabras tocadas se acumulan en ``evitar`` para no encadenar ni revertir
    errores sobre la misma palabra.
    """
    hechas = 0
    intentos = 0
    while hechas < restantes and intentos < 40:
        intentos += 1
        pendientes = list(_GENERICOS_ESTILO)
        rng.shuffle(pendientes)
        aplicado_alguno = False
        for fn in pendientes:
            try:
                nuevo, aplicado, palabra = fn(cuerpo, evitar)
            except Exception:
                continue
            if aplicado:
                cuerpo = nuevo
                hechas += 1
                # `evitar` guarda la PALABRA NUEVA: evita que otro edit la
                # revierta, pero permite reutilizar el genérico en otra
                # palabra (textos cortos necesitan mas de un error).
                if palabra:
                    evitar.add(str(palabra).lower())
                aplicado_alguno = True
                break
        if not aplicado_alguno:
            break
    if incluir_tilde and hechas < restantes:
        try:
            nuevo, aplicado, palabra = _error_tilde(cuerpo, evitar)
        except Exception:
            aplicado = False
        if aplicado:
            cuerpo = nuevo
            hechas += 1
            if palabra:
                evitar.add(str(palabra).lower())
    return cuerpo, hechas


def _quitar_tildes(texto: str) -> str:
    """Quita tildes sin tocar eñes ni hashtags (nunca lanza)."""
    try:
        # Protege hashtags: los restaura al final.
        tags = re.findall(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", texto or "")
        cuerpo = re.sub(
            r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", "\x00", texto or ""
        )
        cuerpo = cuerpo.translate(_MAPA_SIN_TILDES)
        # Resto de acentos via NFD (conserva ñ ya traducida arriba).
        cuerpo = "".join(
            c for c in unicodedata.normalize("NFD", cuerpo)
            if unicodedata.category(c) != "Mn" or c in ("̃",)
        )
        # Re-compone y restaura hashtags intactos.
        cuerpo = unicodedata.normalize("NFC", cuerpo)
        for tag in tags:
            cuerpo = cuerpo.replace("\x00", tag, 1)
        return cuerpo
    except Exception:
        return texto


def _es_registro_ciudadano(registro) -> bool:
    """True si el registro pide el modo humano con errores (nunca lanza)."""
    try:
        r = str(registro or "").strip().lower()
    except Exception:
        return False
    return r.startswith("ciudadan")


def _es_registro_activista(registro) -> bool:
    """True si el registro pide el estilo tecnico-coloquial (nunca lanza)."""
    try:
        r = str(registro or "").strip().lower()
    except Exception:
        return False
    return r.startswith("activis")


# ===================================================================== #
# RED DE SEGURIDAD DETERMINISTA: la narrativa/noticias son SOLO trasfondo.
# El modelo (gpt-4o-mini) a veces filtra palabras de la noticia aunque el
# prompt lo prohiba; aqui se detecta y se sustituye/descarta ese texto por
# el fallback local (que JAMAS lee la narrativa). Ademas se garantiza que
# los registros activista/ciudadana NUNCA abran "¿" ni "¡" (tambien en
# textos de IA); el registro politica conserva sus signos correctos.
# ===================================================================== #
_FUGA_STOPLIST = {
    "gobierno", "gobiernos", "presidente", "presidenta", "mexico",
    "ciudad", "ciudades", "ciudadania", "seguridad", "pueblo", "nacion",
    "nacional", "federal", "estado", "estados", "publica", "publico",
    "millones", "nosotros", "tambien", "porque", "entonces", "mientras",
    "durante", "trabajo", "trabajadores", "noticia", "noticias",
    "informacion", "personas", "social", "sociedad", "politica", "politico",
    "economia", "mexicano", "mexicana", "mexicanos", "mexicanas", "pais",
}


def _normalizar_para_fuga(texto) -> str:
    """Minusculas sin acentos/signos raros, palabras separadas (nunca lanza)."""
    try:
        t = str(texto or "").lower()
        t = _quitar_tildes(t)
        t = "".join(
            c for c in unicodedata.normalize("NFD", t)
            if unicodedata.category(c) != "Mn"
        )
        t = unicodedata.normalize("NFC", t)
        return re.sub(r"[^a-z0-9ñ]+", " ", t).strip()
    except Exception:
        return ""


def _fuga_narrativa(texto, narrativa, exentos: str = "") -> bool:
    """True si ``texto`` filtra material reconocible de ``narrativa``.

    Compara palabras completas tras normalizar (minusculas, sin acentos ni
    signos). Cuenta como fuga cuando aparecen **>=2 tokens distintos** de la
    narrativa (palabras de >=7 letras, sin la stoplist de genericos) o **UN
    token de >=10 letras** (suficientemente distintivo, p. ej. "descuentazo").

    ``exentos`` (kwarg nuevo AL FINAL, opcional): material que SI es tema
    legitimo del texto (p. ej. el TEXTO REAL del tweet ancla al que se
    responde). Sus palabras NO cuentan como fuga aunque coincidan con la
    narrativa. Vacio = comportamiento anterior intacto.
    Nunca lanza: ante cualquier error/entrada rara devuelve False.
    """
    try:
        if not texto or not narrativa:
            return False
        texto_norm = _normalizar_para_fuga(texto)
        narrativa_norm = _normalizar_para_fuga(narrativa)
        if not texto_norm or not narrativa_norm:
            return False
        tokens = {
            tok for tok in re.findall(r"[a-záéíóúüñ]{7,}", narrativa_norm)
            if tok not in _FUGA_STOPLIST
        }
        if exentos:
            # El ancla es el TEMA de la respuesta: sus palabras son legitimas.
            exentos_norm = _normalizar_para_fuga(exentos)
            tokens -= set(re.findall(r"[a-záéíóúüñ]{7,}", exentos_norm))
        apariciones = 0
        for tok in tokens:
            if re.search(r"\b" + re.escape(tok) + r"\b", texto_norm):
                if len(tok) >= 10:
                    return True
                apariciones += 1
                if apariciones >= 2:
                    return True
        return False
    except Exception:
        return False


def _reemplazo_sin_fuga(texto, narrativa, reemplazo_fn, exentos: str = ""):
    """Devuelve ``texto`` intacto o el fallback local si hay fuga (nunca lanza).

    Si ``_fuga_narrativa(texto, narrativa, exentos)`` es True, loguea WARNING y
    devuelve el resultado de ``reemplazo_fn()`` (texto local que no lee la
    narrativa). Si no hay fuga, devuelve el texto tal cual. Ante error en el
    reemplazo devuelve "" para que el llamador use su propio fallback.

    ``exentos`` (kwarg nuevo AL FINAL, opcional) se pasa a `_fuga_narrativa`:
    permite marcar como legitimo el material que SI es tema del texto (p. ej.
    el tweet ancla). Vacio = comportamiento anterior intacto.
    """
    try:
        if not _fuga_narrativa(texto, narrativa, exentos):
            return texto
        logger.warning(
            "Se detecto fuga de la narrativa; se reemplaza el texto por "
            "fallback local"
        )
        try:
            return reemplazo_fn() or ""
        except Exception as e:
            logger.error(f"Error construyendo reemplazo sin fuga: {e}")
            return ""
    except Exception:
        return texto


def _quitar_signos_apertura(texto: str) -> str:
    """Elimina "¿" y "¡" conservando "?"/"!" y los hashtags (nunca lanza)."""
    try:
        t = str(texto or "")
        if not t:
            return t
        # Quita el signo y normaliza el hueco que deja ("¿ Que" -> "Que").
        t = re.sub(r"[ \t]*[¿¡][ \t]*", " ", t)
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"^[ \t]+", "", t)
        t = re.sub(r"[ \t]+$", "", t)
        return t
    except Exception:
        return texto


def _quitar_signos_por_registro(texto: str, registro) -> str:
    """Quita "¿"/"¡" SOLO para activista/ciudadana (politica intacta)."""
    try:
        canon = normalizar_tipo_cuenta(registro)
    except Exception:
        canon = ""
    if (
        canon in ("activista", "ciudadana")
        or _es_registro_activista(registro)
        or _es_registro_ciudadano(registro)
    ):
        return _quitar_signos_apertura(texto)
    return texto


def _aplicar_estilo_ciudadano_local(texto: str, semilla: int = 0) -> str:
    """Humaniza un texto al registro ciudadano (fallback local, nunca lanza).

    Persona real "sin estudios": aplica, con un RNG sembrado para variar entre
    textos, 4-7 errores ortograficos legibles (sin tildes, abreviaturas
    q/pa/xq/tons/k/tmb, dislexias b/v-s/c/z-h muda-y/ll-g/j y confusiones
    hay/ay, haber/a ver->aver, haya->haiga), MALOS SIGNOS de puntuacion
    (nunca "¿" ni "¡", casi sin comas ni puntos internos, "..." ocasional al
    cierre), mayusculas inconsistentes y muletilla distinta. Maximo 1 error
    por palabra; los hashtags se conservan intactos y bien escritos.
    """
    t = (texto or "").strip()
    if not t:
        return t
    try:
        rng = random.Random(int(semilla))
    except Exception:
        rng = random.Random(0)

    try:
        # 1) Protege hashtags para no deformarlos.
        tags = re.findall(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", t)
        cuerpo = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", "\x00", t)

        # 2) Sin tildes (casi siempre en ciudadano).
        cuerpo = _quitar_tildes(cuerpo)

        # 3) 4-7 errores en total: primero abreviaturas (2-3) y luego
        #    dislexias/genericos hasta cumplir el objetivo.
        objetivo = rng.randint(4, 7)
        hechas = 0

        n_abrev = min(max(1, objetivo - 1), rng.randint(2, 3))
        candidatas = list(_ABREVIATURAS_CIUDADANO)
        rng.shuffle(candidatas)
        for patron, repl in candidatas:
            if hechas >= n_abrev:
                break
            nuevo, n = re.subn(patron, repl, cuerpo, count=1,
                               flags=re.IGNORECASE)
            if n:
                cuerpo = nuevo
                hechas += 1

        # 'que' -> 'q' -> 'k' ocasional (cuenta como un error mas).
        if hechas < objetivo and re.search(r"\bq\b", cuerpo):
            if rng.random() < 0.5:
                cuerpo = re.sub(r"\bq\b", "k", cuerpo, count=1)
                hechas += 1

        # 4) Dislexias variadas de la lista (una por palabra).
        dis = list(_DISLEXIAS_CIUDADANO)
        rng.shuffle(dis)
        for patron, repl in dis:
            if hechas >= objetivo:
                break
            if patron in (r"\bcasa\b", r"\bbueno\b"):
                continue  # testigos, no cambian
            nuevo, n = re.subn(patron, repl, cuerpo, count=1,
                               flags=re.IGNORECASE)
            if n:
                cuerpo = nuevo
                hechas += 1

        # 4b) Genericos hasta cumplir 4-7 (h muda, ll->y, c->s, s->c, b<->v,
        #     que/q->k, g->j, z->s); maximo 1 error por palabra. (Tilde no
        #     aplica: el paso 2 ya quito todas las tildes.)
        evitar: set = set()
        cuerpo, extra = _aplicar_genericos_estilo(
            cuerpo, objetivo - hechas, evitar, rng
        )
        hechas += extra

        # 5) MALOS SIGNOS: nunca "¿" ni "¡"; casi sin comas ni puntos
        #    internos (se conserva el punto final si lo habia).
        cuerpo = cuerpo.strip()
        cuerpo = cuerpo.replace("¿", "").replace("¡", "")
        termina_punto = cuerpo.endswith(".") and not cuerpo.endswith("...")
        final = ""
        nucleo = cuerpo
        if termina_punto:
            nucleo = cuerpo[:-1]
            final = "."
        nucleo = re.sub(
            r",", lambda _m: "" if rng.random() < 0.85 else ",", nucleo
        )
        nucleo = re.sub(
            r"(?<!\.)\.(?!\.)",
            lambda _m: "" if rng.random() < 0.8 else ".",
            nucleo,
        )
        cuerpo = (nucleo + final).strip()

        # 6) Mayusculas/minusculas inconsistentes: inicio en minuscula
        #    frecuente + una palabra en MAYUSCULA suelta para enfasis.
        if cuerpo and rng.random() < 0.85:
            cuerpo = cuerpo[0].lower() + cuerpo[1:]
        if cuerpo and rng.random() < 0.35:
            palabras = cuerpo.split()
            if len(palabras) >= 3:
                idx = rng.randrange(1, len(palabras))
                limpia = re.sub(r"[^\wñÑ]", "", palabras[idx])
                if len(limpia) >= 3 and not limpia.startswith("\x00"):
                    palabras[idx] = palabras[idx].upper()
                    cuerpo = " ".join(palabras)

        # 7) Muletilla de apertura/cierre distinta por semilla (variacion).
        apertura = rng.choice(_MULETILLAS_CIUDADANO_INICIO)
        cierre = rng.choice(_MULETILLAS_CIUDADANO_CIERRE)
        if apertura and not cuerpo.lower().startswith(apertura.strip()[:4]):
            cuerpo = f"{apertura}{cuerpo}"
        if cierre and cierre.strip() not in cuerpo.lower()[-20:]:
            cuerpo = f"{cuerpo}{cierre}"
        # Limpieza de puntuacion duplicada ("., " / ",.") tras la muletilla.
        cuerpo = re.sub(r"([.!?])\s*,", r"\1", cuerpo)
        cuerpo = re.sub(r",\s*([.!?])", r"\1", cuerpo)

        # 8) "..." ocasional al cierre (a veces, modo sin estudios).
        cuerpo = cuerpo.rstrip()
        if cuerpo and rng.random() < 0.35:
            if cuerpo.endswith(".") and not cuerpo.endswith("..."):
                cuerpo = cuerpo[:-1] + "..."
            elif not cuerpo.endswith((".", "!", "?", "…")):
                cuerpo = cuerpo + "..."

        # 9) Restaura hashtags intactos.
        for tag in tags:
            cuerpo = cuerpo.replace("\x00", tag, 1)
        cuerpo = cuerpo.replace("\x00", "").strip()
        cuerpo = re.sub(r"[ \t]{2,}", " ", cuerpo)
        return cuerpo.strip() or t
    except Exception as e:
        logger.error(f"Error humanizando estilo ciudadano: {e}")
        return t


def _aplicar_estilo_activista_local(texto: str, semilla: int = 0) -> str:
    """Humaniza un texto al registro activista (fallback local, nunca lanza).

    Tecnico-coloquial: aplica EXACTAMENTE 2-3 errores ortograficos leves
    (tildes omitidas, hay->ay, haber->aver, vez->ves, hacer->aser,
    gracias->grasias, tambien->tmbn, b/v suave, q/pa/xq/tons/k ocasionales),
    maximo 1 por palabra, y ELIMINA los signos de apertura "¿" y "¡" (los
    cierres "?" y "!" se conservan). El mensaje sigue siendo legible y los
    hashtags se conservan intactos y bien escritos.
    """
    t = (texto or "").strip()
    if not t:
        return t
    try:
        rng = random.Random(int(semilla))
    except Exception:
        rng = random.Random(0)

    try:
        # 1) Protege hashtags para no deformarlos.
        tags = re.findall(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", t)
        cuerpo = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", "\x00", t)

        # 2) Exactamente 2-3 errores: primero los ORTOGRAFICOS (obligatorios)
        #    y solo si no alcanzan, licencias q/pa/xq/tons y genericos.
        objetivo = rng.randint(2, 3)
        hechas = 0

        candidatos = list(_ERRORES_ACTIVISTA)
        rng.shuffle(candidatos)
        for patron, repl in candidatos:
            if hechas >= objetivo:
                break
            nuevo, n = re.subn(patron, repl, cuerpo, count=1,
                               flags=re.IGNORECASE)
            if n:
                cuerpo = nuevo
                hechas += 1

        # Genericos (con tilde como ultimo recurso de ortografia).
        evitar: set = set()
        cuerpo, extra = _aplicar_genericos_estilo(
            cuerpo, objetivo - hechas, evitar, rng, incluir_tilde=True
        )
        hechas += extra

        # Licencias de redes al final: solo si los errores ortograficos y los
        # genericos no alcanzaron (el activista no debe vivir de q/pa/xq/tons).
        licencias = list(_LICENCIAS_ACTIVISTA)
        rng.shuffle(licencias)
        for patron, repl in licencias:
            if hechas >= objetivo:
                break
            nuevo, n = re.subn(patron, repl, cuerpo, count=1,
                               flags=re.IGNORECASE)
            if n:
                cuerpo = nuevo
                hechas += 1

        # 3) PROHIBIDO abrir signos: nunca "¿" ni "¡" (cierres intactos).
        cuerpo = cuerpo.replace("¿", "").replace("¡", "")
        cuerpo = re.sub(r"[ \t]{2,}", " ", cuerpo)

        # 4) Restaura hashtags intactos.
        for tag in tags:
            cuerpo = cuerpo.replace("\x00", tag, 1)
        cuerpo = cuerpo.replace("\x00", "").strip()
        return cuerpo.strip() or t
    except Exception as e:
        logger.error(f"Error humanizando estilo activista: {e}")
        return t


def _humanizar_por_registro(texto: str, registro, semilla: int = 0) -> str:
    """Aplica el estilo local del REGISTRO (nunca lanza).

    - "ciudadana" -> modo humano completo (4-7 errores + malos signos).
    - "activista" -> 2-3 errores ortograficos leves SIN abrir "¿"/"¡".
    - "politica" / vacio / desconocido -> texto intacto (ortografia cuidada).
    """
    try:
        canon = normalizar_tipo_cuenta(registro)
    except Exception:
        canon = ""
    if canon == "ciudadana" or (not canon and _es_registro_ciudadano(registro)):
        return _aplicar_estilo_ciudadano_local(texto, semilla)
    if canon == "activista" or (not canon and _es_registro_activista(registro)):
        return _aplicar_estilo_activista_local(texto, semilla)
    return texto


def _humanizar_si_ciudadano(texto: str, registro, semilla: int = 0) -> str:
    """Alias historico de `_humanizar_por_registro` (compatibilidad)."""
    return _humanizar_por_registro(texto, registro, semilla)


# Angulos narrativos rotativos: a cada PERFIL del lote le toca uno distinto.
# Rompen las estructuras clonadas (dos textos con el mismo tema y el mismo
# arranque se delatan como bot). Se rotan por POSICION en el lote.
_ANGULOS_MANTENIMIENTO = (
    "anecdota personal",
    "pregunta al aire",
    "opinion directa",
    "recomendacion",
    "recuerdo de infancia",
    "humor ligero",
    "mini-lista",
    "comparacion",
)


def _prompt_lote_mantenimiento(
    lote: list,
    cuentas_info: list,
    narrativa: str = "",
    entrenamiento: str = "",
    tweet_ancla_texto: str = "",
) -> str:
    """Prompt para pedir UN texto por PERFIL (lote <=15), separados por '---'.

    Cada item del lote es ``(indice_cuenta, numero_texto, tema)`` y la info de
    la cuenta puede traer ``"perfil"`` (formal/ciudadano/popular) y
    ``"tipo_accion"`` ("post" o "comentario"). El prompt respeta formato,
    registro, personalidad y tema de cada cuenta; los POSTS exigen hashtag en
    MEDIO y los COMENTARIOS/RESPUESTAS van SIN hashtags, links ni @menciones
    (X marca las respuestas con esos elementos como probable spam).

    ``tweet_ancla_texto`` (kwarg nuevo AL FINAL, opcional): TEXTO REAL del
    tweet al que responden los comentarios. Si viene, se agrega como TEMA
    PRINCIPAL de los COMENTARIOS/RESPUESTAS (manda sobre la narrativa/contexto
    de campana) sin copiarlo ni parafrasearlo. Vacio = comportamiento previo.
    """
    n_textos = len(lote)
    infos = [_info_cuenta(cuentas_info, item[0]) for item in lote]
    acciones = [_normalizar_accion(info.get("tipo_accion")) for info in infos]
    solo_comentarios = bool(acciones) and all(a == "comentario" for a in acciones)
    hay_comentarios = any(a == "comentario" for a in acciones)

    encabezado = (
        "COMENTARIOS/RESPUESTAS" if solo_comentarios else "MANTENIMIENTO DE CUENTA"
    )
    partes = [
        "Genera EXACTAMENTE {} textos de {}, uno por cada PERFIL y en el MISMO "
        "ORDEN en que aparecen.".format(n_textos, encabezado),
        "TAREA: mantener la cuenta con presencia y conversacion diaria, con "
        "comentarios cercanos, naturales y humanos."
        + (
            " En este lote hay COMENTARIOS/RESPUESTAS: deben ser breves y "
            "conversacionales (1-2 frases; el perfil formal mantiene sus 3 "
            "bloques pero MUY cortos)."
            if hay_comentarios
            else ""
        ),
    ]
    if narrativa:
        partes.append(f"NARRATIVA GENERAL (TRASFONDO INVISIBLE):\n{narrativa}")
        partes.append(_reglas_trasfondo().strip())
    # El tweet ancla real es el TEMA PRINCIPAL de las respuestas: por encima
    # de la narrativa/contexto de campana y sin material copiable.
    ancla_prompt = bloque_tweet_ancla(tweet_ancla_texto).strip()
    if hay_comentarios and ancla_prompt:
        partes.append(
            "TEMA PRINCIPAL DE LOS COMENTARIOS/RESPUESTAS (por encima de "
            "la narrativa/contexto de campana):\n" + ancla_prompt
        )
    if entrenamiento:
        partes.append(f"ENTRENAMIENTO GENERAL:\n{entrenamiento}")

    temas_presentes = []
    for _i, _j, tema in lote:
        if tema not in temas_presentes:
            temas_presentes.append(tema)
    for tema in temas_presentes:
        instructivo = instrucciones_tema(tema)
        if instructivo:
            partes.append(instructivo)

    lineas = [
        "PERFILES DEL LOTE (respeta el tema, la personalidad, el registro y el "
        "FORMATO OBLIGATORIO POR PERFIL de cada uno):"
    ]
    for idx, (i, _j, tema) in enumerate(lote, 1):
        info = infos[idx - 1]
        usuario = str(info.get("usuario") or "").strip()
        personalidad = str(info.get("personalidad") or "").strip()
        registro = str(info.get("registro") or "").strip().lower()
        perfil = str(info.get("perfil") or "").strip()
        accion = _normalizar_accion(info.get("tipo_accion"))
        angulo = _ANGULOS_MANTENIMIENTO[
            (idx - 1) % len(_ANGULOS_MANTENIMIENTO)
        ]
        linea = (
            f"PERFIL {idx} | usuario: {usuario or 'cuenta'} | registro: "
            f"{registro or 'libre'} | perfil: {etiqueta_perfil(perfil)} | "
            f"accion: {accion} | tema: {tema} | angulo: {angulo}"
        )
        linea += (
            "\nANGULO NARRATIVO OBLIGATORIO DE ESTE TEXTO: "
            f"{angulo} (escribe el texto desde ESE angulo; no lo repitas con "
            "el de otro PERFIL)."
        )
        linea += (
            "\nPERSONALIDAD DE LA CUENTA (respeta su tono e intereses): "
            f"{personalidad or 'persona mexicana comun'}"
        )
        reglas = _reglas_registro(
            registro, incluir_limite=(accion != "comentario")
        ).strip()
        if reglas:
            linea += f"\n{reglas}"
        estilo = bloque_estilo_perfil(perfil).strip()
        if estilo:
            linea += f"\n{estilo}"
        if accion == "comentario":
            linea += (
                "\nACCION COMENTARIO/RESPUESTA: texto apto para responder una "
                "publicacion; breve y conversacional (1-2 frases) y SIN "
                "hashtags, links ni @menciones."
            )
        lineas.append(linea)
    partes.append("\n".join(lineas))

    if solo_comentarios:
        reglas_hashtag = (
            "- Los textos de COMENTARIO/RESPUESTA NO deben llevar hashtags, NI "
            "links, NI @menciones (X marca las respuestas con esos elementos "
            "como probable spam): texto conversacional limpio.\n"
            "- No repitas frases de relleno; aporta un angulo distinto y "
            "conversacional en cada texto.\n"
        )
    elif hay_comentarios:
        reglas_hashtag = (
            "- Los textos de POST DEBEN incluir al menos un hashtag INTEGRADO "
            "EN MEDIO del texto; NUNCA lo pongas al final (bien escrito, sin "
            "deformar).\n"
            "- Los textos de COMENTARIO/RESPUESTA NO deben llevar hashtags, NI "
            "links, NI @menciones (X marca las respuestas con esos elementos "
            "como probable spam): texto conversacional limpio.\n"
        )
    else:
        reglas_hashtag = (
            "- TODOS los textos DEBEN incluir al menos un hashtag INTEGRADO EN "
            "MEDIO del texto; NUNCA lo pongas al final (bien escrito, sin "
            "deformar).\n"
        )
    if hay_comentarios and ancla_prompt:
        reglas_hashtag += (
            "- Los COMENTARIOS/RESPUESTA deben reaccionar al TEMA PRINCIPAL "
            "(el tweet ancla indicado arriba), NO a la narrativa/contexto de "
            "campana, y sin copiarlo ni parafrasearlo.\n"
        )

    # Largo del lote: regla de oro de 100 SOLO para POSTS; los COMENTARIOS
    # quedan fuera del limite (240 como antes).
    if solo_comentarios:
        reglas_largo = "- Maximo 240 caracteres por texto.\n"
    elif hay_comentarios:
        reglas_largo = (
            "- Maximo 100 caracteres para los POSTS (estricto); los "
            "COMENTARIOS pueden llegar a 240.\n"
        )
    else:
        reglas_largo = "- Maximo 100 caracteres por texto.\n"

    partes.append(
        "REGLA DE DIVERSIDAD (OBLIGATORIA):\n"
        "- NINGUN texto puede parecerse a otro del lote en TEMA ni en "
        "ESTRUCTURA: cada texto debe poder leerse como de una persona distinta.\n"
        "- Cada PERFIL tiene asignado SU tema y SU angulo narrativo: trata SOLO "
        "ese tema, desde ESE angulo, sin mezclar temas ni angulos de otros "
        "textos del lote.\n"
        "- Si un texto habla de cafe, el SIGUIENTE debe hablar de otro tema "
        "(clima, trafico, mascotas...): PROHIBIDO repetir el tema de otro "
        "texto del lote.\n"
        "- PROHIBIDO repetir palabras clave, objetos, aperturas, cierres, "
        "muletillas, estructuras o hashtags entre los textos del lote.\n"
        "- PROHIBIDO reciclar oraciones modelo: varia largo, ritmo, "
        "vocabulario y tipo de frase en cada texto.\n"
    )

    partes.append(
        "REGLAS DEL LOTE:\n"
        f"- Genera EXACTAMENTE {n_textos} textos DISTINTOS entre si, sin repetir "
        "frases, ideas, aperturas, muletillas ni estructuras (alterna anecdota / "
        "opinion directa / pregunta, pregunta-afirmacion-exclamacion, con y sin "
        "emoji, longitudes distintas).\n"
        "- Un texto por PERFIL, en el MISMO ORDEN en que aparecen.\n"
        "- Cada texto debe respetar el TEMA, la PERSONALIDAD, el REGISTRO y el "
        "FORMATO OBLIGATORIO POR PERFIL de su cuenta.\n"
        "- Con REGISTRO CIUDADANO: cada texto con 4-7 errores ortograficos "
        "legibles (hay/ay, haber/a ver->aver, haya->haiga, b/v, s/c/z, h muda, "
        "g/j, y/ll, abreviaturas q/pa/xq/tons/k), MALOS SIGNOS (casi sin comas "
        "ni puntos, nunca '¿' ni '¡') y variacion total entre textos.\n"
        "- Con REGISTRO ACTIVISTA: 2-3 errores ortograficos leves por texto, "
        "PROHIBIDO abrir '¿' y '¡' (los cierres '?' y '!' se usan normales).\n"
        "- Con REGISTRO POLITICO: ortografia cuidada; se permite COMO MÁXIMO UN "
        "error leve opcional (p. ej. una tilde omitida) y ninguna abreviatura.\n"
        "- Los textos de COMENTARIO/RESPUESTA deben ser breves y conversacionales "
        "(1-2 frases; el perfil formal con sus 3 bloques pero cortos).\n"
        + reglas_hashtag
        + reglas_largo
        + "- NO numeres, NO uses etiquetas ni corchetes.\n"
        "- Responde SOLO con los textos separados por '---'."
    )
    return "\n\n".join(partes)


def _fallback_estructura_mantenimiento(cuentas_info, n_por_cuenta) -> list[list[str]]:
    """Ultimo recurso: estructura valida con textos no vacios (nunca lanza).

    Usa el fallback local DINAMICO por partes (`_generar_plantilla_local`):
    tema rotativo por (cuenta, texto), perfil/tipo_accion/registro de cada
    cuenta, palabras clave sin repetir dentro de la MISMA cuenta y unicidad
    global dentro de la llamada. Los POSTS llevan hashtag en medio y <=100
    caracteres; los COMENTARIOS salen spam-safe (sin hashtags, links ni
    @menciones). Aplica el estilo local de cada registro (ciudadana: malos
    signos + 4-7 errores; activista: 2-3 errores sin abrir "¿"/"¡"; politica
    intacta). NUNCA lee la narrativa (garantia anti-fuga).
    """
    try:
        lista = list(cuentas_info or [])
    except Exception:
        lista = []
    try:
        n = int(n_por_cuenta)
    except (TypeError, ValueError):
        n = 2
    if n < 0:
        n = 0

    resultado = []
    vistos_global: set = set()
    total_temas = len(_TEMAS_MANTENIMIENTO) or 1
    for i, cu in enumerate(lista):
        info = cu if isinstance(cu, dict) else {}
        perfil = str(info.get("perfil") or "").strip()
        registro = str(info.get("registro") or "").strip()
        accion = _normalizar_accion(info.get("tipo_accion"))
        es_comentario = accion == "comentario"
        usuario = str(info.get("usuario") or "").strip()
        nombre = str(info.get("nombre") or info.get("usuario") or "").strip()
        personalidad = str(info.get("personalidad") or "").strip()
        fila = []
        vistos: set = set()
        evitar: set = set()
        for j in range(n):
            tema = _TEMAS_MANTENIMIENTO[(i * n + j) % total_temas]
            t = _texto_local_diverso(
                perfil=perfil,
                registro=registro,
                tema=tema,
                es_comentario=es_comentario,
                nombre=nombre,
                personalidad=personalidad,
                semilla_base=_semilla_plantilla(i, j, usuario),
                evitar=evitar,
                vistos=vistos,
                vistos_global=vistos_global,
            )
            evitar |= _palabras_clave(t)
            vistos.add(t)
            vistos_global.add(t)
            fila.append(t)
        resultado.append(fila)
    return resultado


def _generar_textos_mantenimiento_impl(
    cuentas_info: list,
    n_por_cuenta: int = 2,
    narrativa: str = "",
    entrenamiento: str = "",
    temas: list | None = None,
    callback=None,
    tweet_ancla_texto: str = "",
) -> list[list[str]]:
    try:
        lista = list(cuentas_info or [])
    except Exception:
        lista = []
    if not lista:
        return []

    try:
        n = int(n_por_cuenta)
    except (TypeError, ValueError):
        n = 2
    if n < 0:
        n = 0
    total_cuentas = len(lista)
    if n == 0:
        return [[] for _ in range(total_cuentas)]

    # Temas limpios (acepta alias/acentos) sin romper si viene algo raro.
    temas_limpios: list[str] = []
    try:
        propuestos = list(temas) if temas else list(_TEMAS_MANTENIMIENTO)
    except Exception:
        propuestos = list(_TEMAS_MANTENIMIENTO)
    for t in propuestos:
        try:
            clave = _normalizar_tema(str(t))
        except Exception:
            clave = str(t).strip().lower()
        if clave and clave not in temas_limpios:
            temas_limpios.append(clave)
    if not temas_limpios:
        temas_limpios = list(_TEMAS_MANTENIMIENTO)

    resultado: list[list] = [[None] * n for _ in range(total_cuentas)]
    total_textos = total_cuentas * n
    hechas = 0

    def _reportar() -> None:
        if callback is None:
            return
        try:
            callback(hechas, total_textos)
        except Exception as e:
            logger.error(f"Error en callback de mantenimiento: {e}")

    # 1) Un slot por (cuenta, tuit) con tema rotativo. Los lotes van en ORDEN
    #    NATURAL (i, j), MEZCLANDO temas: cada lote de <=15 slots lleva temas
    #    TODOS distintos (antes se agrupaba por tema y los lotes salian
    #    homogeneos, con textos clonados).
    slots = []
    for i in range(total_cuentas):
        for j in range(n):
            tema = temas_limpios[(i * n + j) % len(temas_limpios)]
            slots.append((i, j, tema))

    generador = None
    try:
        generador = GeneradorContenido()
    except Exception as e:
        logger.error(f"No se pudo crear GeneradorContenido: {e}")

    # 2) OpenAI: lotes de <=15 perfiles, un texto por perfil separado por '---'.
    if generador is not None:
        for inicio in range(
            0, len(slots), _MAX_PERFILES_POR_LLAMADA_MANTENIMIENTO
        ):
            lote = slots[
                inicio:inicio + _MAX_PERFILES_POR_LLAMADA_MANTENIMIENTO
            ]
            textos_lote = []
            try:
                prompt = _prompt_lote_mantenimiento(
                    lote,
                    lista,
                    narrativa,
                    entrenamiento,
                    tweet_ancla_texto,
                )
                contenido = generador._chat(prompt, temperature=0.8)
                textos_lote = generador._parsear_textos(contenido, len(lote))
            except Exception as e:
                logger.error(f"Error OpenAI en lote de mantenimiento: {e}")
                textos_lote = []
            for k, (i, j, _tema) in enumerate(lote):
                if resultado[i][j]:
                    continue
                if k < len(textos_lote):
                    t = (textos_lote[k] or "").strip()
                    if t:
                        resultado[i][j] = t
                        hechas += 1
            _reportar()

    # 3) Relleno local + normalizacion final: exactamente n textos por cuenta,
    #    GLOBALMENTE unicos, sin compartir palabras clave entre los textos de
    #    una MISMA cuenta y NUNCA vacios; los POSTS con hashtag integrado EN
    #    MEDIO y <=100 chars; los COMENTARIOS sin hashtags/links/@menciones.
    vistos_global: set = set()
    total_temas = len(temas_limpios) or 1
    for i in range(total_cuentas):
        info = _info_cuenta(lista, i)
        nombre = str(info.get("nombre") or info.get("usuario") or "").strip()
        personalidad = str(info.get("personalidad") or "").strip()
        perfil = str(info.get("perfil") or "").strip()
        registro = str(info.get("registro") or "").strip()
        accion = _normalizar_accion(info.get("tipo_accion"))
        es_comentario = accion == "comentario"
        usuario = str(info.get("usuario") or "").strip()

        fila: list[str] = []
        vistos: set = set()
        evitar: set = set()

        def _plantilla_local(j: int) -> str:
            """Fallback local diverso (NO lee narrativa) del tema/perfil/accion.

            Acumula `evitar`/`vistos` de la cuenta para que sus n textos no
            compartan palabras clave ni se repitan (y `vistos_global` para no
            repetirse entre cuentas).
            """
            tema = temas_limpios[(i * n + j) % total_temas]
            return _texto_local_diverso(
                perfil=perfil,
                registro=registro,
                tema=tema,
                es_comentario=es_comentario,
                nombre=nombre,
                personalidad=personalidad,
                semilla_base=_semilla_plantilla(i, j, usuario) + (i * n + j),
                evitar=evitar,
                vistos=vistos,
                vistos_global=vistos_global,
            )

        for j in range(n):
            t = (resultado[i][j] or "").strip()
            if not t:
                t = _plantilla_local(j)
                hechas += 1
                _reportar()
            else:
                if narrativa:
                    # Red de seguridad: si el texto de la IA filtra la
                    # narrativa, se cambia por el fallback local del mismo
                    # tema/perfil (que NO lee la narrativa) y conserva
                    # registro/perfil de la cuenta. En COMENTARIOS, las
                    # palabras del tweet ancla (tema legitimo) quedan exentas.
                    original_ia = t
                    t = _reemplazo_sin_fuga(
                        t,
                        narrativa,
                        lambda j=j: _plantilla_local(j),
                        exentos=(tweet_ancla_texto if es_comentario else ""),
                    )
                    if not t or t != original_ia:
                        if not t:
                            t = _plantilla_local(j)
                        # El reemplazo local ya viene normalizado y diverso.
                        evitar |= _palabras_clave(t)
                        vistos.add(t)
                        vistos_global.add(t)
                        fila.append(t)
                        continue
                # Normalizacion del texto de la IA (estilo por registro,
                # hashtag/limite o spam-safe) con el mismo tratamiento local.
                t = _normalizar_texto_local(
                    t, registro, i * 100 + j, es_comentario
                )
                # Diversidad: si el texto de la IA comparte palabras clave con
                # otro de la MISMA cuenta, o esta repetido (cuenta o campana),
                # se reemplaza por el fallback local del tema.
                if (
                    not t
                    or (_palabras_clave(t) & evitar)
                    or (t in vistos)
                    or (t in vistos_global)
                ):
                    t = _plantilla_local(j)
            if not t:
                t = _plantilla_local(j)
            evitar |= _palabras_clave(t)
            vistos.add(t)
            vistos_global.add(t)
            fila.append(t)
        resultado[i] = fila

    _reportar()
    return [fila for fila in resultado]


def generar_textos_mantenimiento(
    cuentas_info: list[dict],
    n_por_cuenta: int = 2,
    narrativa: str = "",
    entrenamiento: str = "",
    temas: list | None = None,
    callback=None,
) -> list[list[str]]:
    """Genera n_por_cuenta textos por cuenta, alineados al indice de cuentas_info.

    cuentas_info: [{"usuario","registro","personalidad","seccion","nombre",
                    "perfil","tipo_accion"}, ...]
    `perfil` ("formal"/"ciudadano"/"popular") define el formato obligatorio del
    texto (ver `core.perfiles`); vacio/desconocido usa el comportamiento
    generico. `tipo_accion` ("post" o "comentario") permite pedir textos aptos
    para responder (breves y conversacionales).
    temas default: ``ia.prompts.TEMAS_MANTENIMIENTO`` (los 4 legacy + mas de
    30 cotidianos, con rotacion que alterna familias de vocabulario).
    - Asigna temas rotativos por cuenta/tuit (variedad garantizada). Los lotes
      de <=15 cuentas van en orden natural MEZCLANDO temas distintos (ya no se
      agrupan por tema): pide a OpenAI UN texto por
      personalidad (repitiendo registro, personalidad, perfil, tema, angulo y
      accion de cada una) separados por '---' y parsea en orden.
    - Si OpenAI falla o devuelve menos, rellena con plantillas locales
      (>=3 por perfil en cada tema, mas comentarios por perfil) + variaciones +
      el nombre/personalidad, garantizando n_por_cuenta textos NO VACIOS.
    - Los textos de POST llevan un hashtag INTEGRADO EN MEDIO
      (`core.perfiles.colocar_hashtag_en_medio`), nunca al final; los de
      COMENTARIO/RESPUESTA van SIN hashtags, links ni @menciones (spam-safe).
    - callback(hechas, total) opcional para progreso.
    - Devuelve len(cuentas_info) listas de exactamente n_por_cuenta textos
      (unicos dentro de la medida de lo posible; nunca vacios). Nunca lanza.
    """
    try:
        return _generar_textos_mantenimiento_impl(
            cuentas_info,
            n_por_cuenta=n_por_cuenta,
            narrativa=narrativa,
            entrenamiento=entrenamiento,
            temas=temas,
            callback=callback,
        )
    except Exception as e:
        logger.error(f"Error inesperado generando textos de mantenimiento: {e}")
        return _fallback_estructura_mantenimiento(cuentas_info, n_por_cuenta)


# ===================================================================== #
# Comentarios/respuestas breves por cuenta (mismo contrato de salida)
# ===================================================================== #
def generar_textos_comentario(
    cuentas_info: list[dict],
    n_por_cuenta: int = 1,
    narrativa: str = "",
    entrenamiento: str = "",
    callback=None,
    tweet_ancla_texto: str = "",
) -> list[list[str]]:
    """Genera n_por_cuenta COMENTARIOS/RESPUESTAS por cuenta (alineados).

    Misma firma/contrato que `generar_textos_mantenimiento`: nunca lanza,
    devuelve len(cuentas_info) listas de exactamente n_por_cuenta textos no
    vacios (o el fallback estructural). Cada texto usa el PERFIL de su cuenta
    (formal/ciudadano/popular) y es apto para responder (breve y
    conversacional, el perfil formal con Titulo/Descripcion/Conclusion
    cortos). Son SPAM-SAFE: no llevan hashtags, links ni @menciones (X marca
    las respuestas con esos elementos como probable spam).

    ``tweet_ancla_texto`` (kwarg nuevo AL FINAL, retrocompatible): TEXTO REAL
    del tweet al que se responde. Cuando viene, ese contenido es el TEMA
    PRINCIPAL del comentario (por encima de la narrativa/contexto de campana):
    se pasa al prompt de los lotes de comentarios para que la respuesta
    reaccione a ESE tema sin copiarlo ni parafrasearlo. NUNCA se mete en
    ``narrativa`` (la red anti-fuga `_fuga_narrativa` lo reemplazaria) y el
    fallback local tampoco lo copia. Vacio = comportamiento anterior intacto.
    """
    try:
        base = list(cuentas_info or [])
    except Exception:
        base = []

    # No mutar los dicts del llamador: se trabaja sobre copias con la accion.
    info_comentarios = []
    for cu in base:
        try:
            copia = dict(cu) if isinstance(cu, dict) else {}
        except Exception:
            copia = {}
        copia["tipo_accion"] = "comentario"
        info_comentarios.append(copia)

    try:
        return _generar_textos_mantenimiento_impl(
            info_comentarios,
            n_por_cuenta=n_por_cuenta,
            narrativa=narrativa,
            entrenamiento=entrenamiento,
            callback=callback,
            tweet_ancla_texto=tweet_ancla_texto,
        )
    except Exception as e:
        logger.error(f"Error inesperado generando textos de comentario: {e}")
        return _fallback_estructura_mantenimiento(info_comentarios, n_por_cuenta)


# ===================================================================== #
# Campaña 3+3+3 por cuenta: 3 posts + 3 comentarios + 3 citas/RTs.
# Activación ideal por cuenta por campaña (ya no 7/hora): 9 acciones con
# textos distintos, sin duplicados y con hashtag EN MEDIO.
# ===================================================================== #
def _copias_con_accion(cuentas_info, accion: str) -> list[dict]:
    """Copias de cuentas_info con ``tipo_accion`` forzada (no muta al llamador)."""
    base = list(cuentas_info or [])
    copias = []
    for cu in base:
        try:
            copia = dict(cu) if isinstance(cu, dict) else {}
        except Exception:
            copia = {}
        copia["tipo_accion"] = accion
        copias.append(copia)
    return copias


def _generar_citas_campana(
    lista: list[dict],
    n_citas: int,
    narrativa: str = "",
    entrenamiento: str = "",
    base_cita: str = "",
) -> list[list[str]]:
    """Genera n_citas citas/RTs por cuenta (alineadas, nunca lanza).

    Si hay ``base_cita`` usa variaciones agrupadas por (registro, perfil) para
    no mezclar estilos; si no, genera como posts con temas rotados. Todo pasa
    por estilo ciudadano local (si aplica) y hashtag en medio.
    """
    try:
        total = len(list(lista or []))
    except Exception:
        total = 0
    if total <= 0 or n_citas <= 0:
        return [[] for _ in range(max(0, total))]

    base = (base_cita or "").strip()
    if not base:
        # Sin cita base: genera como posts (el llamador deduplica contra posts).
        try:
            return _generar_textos_mantenimiento_impl(
                _copias_con_accion(lista, "post"),
                n_por_cuenta=n_citas,
                narrativa=narrativa,
                entrenamiento=entrenamiento,
            )
        except Exception as e:
            logger.error(f"Error generando citas sin base: {e}")
            return _fallback_estructura_mantenimiento(
                _copias_con_accion(lista, "post"), n_citas
            )

    # Con cita base: agrupa por (registro, perfil) y pide variaciones por lote.
    reglas = []
    for i in range(total):
        info = _info_cuenta(lista, i)
        registro = str(info.get("registro") or "").strip().lower()
        try:
            perfil = normalizar_perfil(info.get("perfil"))
        except Exception:
            perfil = ""
        reglas.append((registro, perfil))

    grupos: dict[tuple, list[int]] = {}
    for i, clave in enumerate(reglas):
        grupos.setdefault(clave, []).append(i)

    generador = None
    try:
        generador = GeneradorContenido()
    except Exception as e:
        logger.error(f"No se pudo crear GeneradorContenido para citas: {e}")

    resultado: list[list] = [[None] * n_citas for _ in range(total)]
    for (registro, perfil), indices in grupos.items():
        total_grupo = len(indices) * n_citas
        textos_grupo: list[str] = []
        pendientes = total_grupo
        while pendientes > 0 and generador is not None:
            pedir = min(pendientes, _MAX_VARIACIONES_POR_LLAMADA)
            try:
                lote = generador.generar_variaciones_masivas(
                    base, pedir, narrativa, entrenamiento, registro, perfil
                )
            except Exception as e:
                logger.error(f"Error OpenAI en citas de campaña: {e}")
                lote = []
            textos_grupo.extend(t or "" for t in (lote or []))
            if not lote:
                break
            pendientes = total_grupo - len(textos_grupo)
            if pendientes > 0 and len(lote) < pedir:
                break
        # Relleno local del grupo si OpenAI no alcanzo.
        if len(textos_grupo) < total_grupo:
            try:
                from activaciones.variaciones import generar_pool_variaciones
                faltan = total_grupo - len(textos_grupo)
                extra = generar_pool_variaciones(base, faltan)
            except Exception as e:
                logger.error(f"Error en fallback de citas de campaña: {e}")
                extra = []
            textos_grupo.extend(extra)
        # Reparte en orden por cuenta y humaniza+hashtag el fallback.
        pos = 0
        for idx_cuenta in indices:
            for j in range(n_citas):
                if resultado[idx_cuenta][j] is not None:
                    continue
                if pos < len(textos_grupo):
                    t = (textos_grupo[pos] or "").strip()
                    pos += 1
                    if t:
                        resultado[idx_cuenta][j] = t
        # Lo que falte se rellena abajo en la normalizacion final.

    # Normalizacion final: sin vacios, estilo ciudadano SIEMPRE (OpenAI tiende
    # a devolver limpio), hashtag en medio y unicos dentro de la cuenta.
    for i in range(total):
        info = _info_cuenta(lista, i)
        registro = str(info.get("registro") or "").strip()
        vistos_local: set = set()
        for j in range(n_citas):
            t = (resultado[i][j] or "").strip()
            if not t:
                t = f"{base} ({j + 1})" if base else f"Cita {j + 1}"
            t = _humanizar_por_registro(t, registro, semilla=i * 100 + j + 7)
            t = _con_hashtag_en_medio(t)
            # Corte DURO (regla de oro): ninguna cita/RT supera 100 chars.
            t = _acotar_limite(t)
            if t in vistos_local:
                t = _variar_hasta_unico(t, vistos_local)
                t = _humanizar_por_registro(
                    t, registro, semilla=i * 100 + j + 1997
                )
                t = _con_hashtag_en_medio(t)
                t = _acotar_limite(t)
            vistos_local.add(t)
            resultado[i][j] = t
    return [list(fila) for fila in resultado]


def generar_pool_campana_por_cuenta(
    cuentas_info: list[dict],
    n_posts: int = 3,
    n_comentarios: int = 3,
    n_citas: int = 3,
    narrativa: str = "",
    entrenamiento: str = "",
    base_cita: str = "",
    temas: list | None = None,
    callback=None,
) -> list[dict]:
    """Pool de campaña 3+3+3 por cuenta (9 acciones distintas por cuenta).

    Devuelve una lista alineada a ``cuentas_info`` con un dict por cuenta::

        {"posts": [...3], "comentarios": [...3], "citas": [...3]}

    - ``posts``: textos de post (formato por perfil de cada cuenta).
    - ``comentarios``: textos breves aptos para responder, SIN hashtags, links
      ni @menciones (spam-safe para X).
    - ``citas``: textos para citas/RTs del tweet principal; si hay
      ``base_cita`` son variaciones de esa cita, si no son posts alternos.
    - Los 9 textos de cada cuenta son DISTINTOS entre si (sin duplicados en lo
      posible), respetan registro (ciudadano con errores humanos legibles;
      politico/activista sin errores fuertes) y perfil; posts y citas pasan
      por ``colocar_hashtag_en_medio`` (hashtag obligatorio EN MEDIO, nunca al
      final, bien escrito) y los comentarios por ``limpiar_comentario_spam``.
    - Nunca lanza: en el peor caso devuelve estructura valida con fallbacks
      locales. ``callback(hechas, total)`` opcional para progreso.
    """
    try:
        lista = list(cuentas_info or [])
    except Exception:
        lista = []
    if not lista:
        return []
    try:
        n_posts = max(0, int(n_posts))
        n_comentarios = max(0, int(n_comentarios))
        n_citas = max(0, int(n_citas))
    except (TypeError, ValueError):
        n_posts, n_comentarios, n_citas = 3, 3, 3

    total = len(lista)
    total_acciones = total * (n_posts + n_comentarios + n_citas)
    hechas = 0

    def _reportar(nueva: int = 0) -> None:
        nonlocal hechas
        hechas += nueva
        if callback is None:
            return
        try:
            callback(hechas, total_acciones)
        except Exception as e:
            logger.error(f"Error en callback de campaña 3+3+3: {e}")

    # 1-2) Posts y comentarios con el motor de mantenimiento (ya humaniza el
    # fallback ciudadano y pone hashtag en medio).
    try:
        posts = _generar_textos_mantenimiento_impl(
            _copias_con_accion(lista, "post"),
            n_por_cuenta=n_posts,
            narrativa=narrativa,
            entrenamiento=entrenamiento,
            temas=temas,
        )
    except Exception as e:
        logger.error(f"Error generando posts de campaña: {e}")
        posts = _fallback_estructura_mantenimiento(
            _copias_con_accion(lista, "post"), n_posts
        )
    _reportar(total * n_posts)

    try:
        comentarios = _generar_textos_mantenimiento_impl(
            _copias_con_accion(lista, "comentario"),
            n_por_cuenta=n_comentarios,
            narrativa=narrativa,
            entrenamiento=entrenamiento,
        )
    except Exception as e:
        logger.error(f"Error generando comentarios de campaña: {e}")
        comentarios = _fallback_estructura_mantenimiento(
            _copias_con_accion(lista, "comentario"), n_comentarios
        )
    _reportar(total * n_comentarios)

    # 3) Citas/RTs con texto.
    try:
        citas = _generar_citas_campana(
            lista, n_citas, narrativa, entrenamiento, base_cita
        )
    except Exception as e:
        logger.error(f"Error generando citas de campaña: {e}")
        citas = _fallback_estructura_mantenimiento(
            _copias_con_accion(lista, "post"), n_citas
        )
    _reportar(total * n_citas)

    # 4) Dedup global dentro de cada cuenta (los 9 distintos) + hashtag final.
    resultado: list[dict] = []
    for i in range(total):
        vistos: set = set()

        def _unico(
            texto: str, registro, semilla: int, es_comentario: bool = False
        ) -> str:
            t = (texto or "").strip()
            if not t:
                t = (
                    _CIERRE_COMENTARIO_SPAM
                    if es_comentario
                    else "Buen dia a todos."
                )
            if es_comentario:
                t = limpiar_comentario_spam(t)
            else:
                # Corte DURO (regla de oro): posts y citas <= 100 chars.
                t = _acotar_limite(_con_hashtag_en_medio(t))
            if t in vistos:
                t = _variar_hasta_unico(t, vistos)
                t = _humanizar_por_registro(t, registro, semilla=semilla)
                if es_comentario:
                    t = limpiar_comentario_spam(t)
                else:
                    t = _acotar_limite(_con_hashtag_en_medio(t))
            vistos.add(t)
            return t

        info = _info_cuenta(lista, i)
        registro = str(info.get("registro") or "").strip()
        fila_posts = []
        fila_coms = []
        fila_citas = []
        for j, t in enumerate(list(posts[i]) if i < len(posts) else []):
            fila_posts.append(_unico(t, registro, semilla=i * 1000 + j))
        for j, t in enumerate(list(comentarios[i]) if i < len(comentarios) else []):
            fila_coms.append(
                _unico(
                    t, registro, semilla=i * 1000 + 100 + j, es_comentario=True
                )
            )
        for j, t in enumerate(list(citas[i]) if i < len(citas) else []):
            fila_citas.append(_unico(t, registro, semilla=i * 1000 + 200 + j))
        # Garantiza longitudes exactas aunque algo fallara arriba.
        while len(fila_posts) < n_posts:
            fila_posts.append(
                _unico(f"Post extra {len(fila_posts) + 1}", registro,
                       semilla=i * 1000 + 300 + len(fila_posts))
            )
        while len(fila_coms) < n_comentarios:
            fila_coms.append(
                _unico(
                    f"Comentario extra {len(fila_coms) + 1}", registro,
                    semilla=i * 1000 + 400 + len(fila_coms),
                    es_comentario=True,
                )
            )
        while len(fila_citas) < n_citas:
            fila_citas.append(
                _unico(f"Cita extra {len(fila_citas) + 1}", registro,
                       semilla=i * 1000 + 500 + len(fila_citas))
            )
        resultado.append({
            "posts": fila_posts[:n_posts],
            "comentarios": fila_coms[:n_comentarios],
            "citas": fila_citas[:n_citas],
        })

    return resultado


# Alias explicito del pool ideal 3+3+3 (misma firma, valores por defecto 3).
def generar_textos_campana_3_3_3(
    cuentas_info: list[dict],
    narrativa: str = "",
    entrenamiento: str = "",
    base_cita: str = "",
    temas: list | None = None,
    callback=None,
) -> list[dict]:
    """Atajo de ``generar_pool_campana_por_cuenta`` con 3+3+3 fijos."""
    return generar_pool_campana_por_cuenta(
        cuentas_info,
        n_posts=3,
        n_comentarios=3,
        n_citas=3,
        narrativa=narrativa,
        entrenamiento=entrenamiento,
        base_cita=base_cita,
        temas=temas,
        callback=callback,
    )


# ===================================================================== #
# Rol "hashtags": post ORIGINAL por cuenta sobre un contexto, con los
# hashtags pedidos SIEMPRE bien escritos e integrados EN MEDIO del texto.
# ===================================================================== #
_MAX_CUENTAS_HASHTAGS_POR_LLAMADA = 15

# Limite DURO de largo para POSTS y RETWEETS CON CITA generados por IA
# (regla de oro: maximo estricto de 100 caracteres, incluidos hashtags y
# espacios). X corta en 280 como red externa, pero aqui el contrato es 100.
# TODO texto de `generar_textos_hashtags_por_cuenta`, de `_texto_hashtag_local`
# y del corte final `_acotar_limite` sale con len(texto) <= _MAX_LARGO_HASHTAG.
_MAX_LARGO_HASHTAG = 100


def _reemplazar_tag_exacto(texto: str, tag_pedido: str, reemplazo: str) -> str:
    """Sustituye la primera aparicion del tag por ``reemplazo`` (grupo exacto).

    ``colocar_hashtag_en_medio`` normaliza la grafia del hashtag (p. ej.
    ``#mexico`` -> ``#Mexico``); este helper restaura la grafia EXACTA pedida
    y permite insertar un grupo de tags juntos. Nunca lanza.
    """
    t = str(texto or "")
    pedido = str(tag_pedido or "").strip()
    nuevo = str(reemplazo or "").strip()
    if not t or not pedido or not nuevo:
        return t
    try:
        patron = re.compile(re.escape(pedido), re.IGNORECASE)
        return patron.sub(lambda _m: nuevo, t, count=1)
    except Exception:
        return t


def _insertar_grupo_tras_tag(texto: str, tag_ancla: str, extras) -> str:
    """Inserta ``extras`` justo despues del primer ``tag_ancla`` encontrado.

    Asi el grupo de hashtags pedidos queda unido (``... #mexico #futbol ...``)
    en el punto medio donde ya estaba el ancla. Nunca lanza.
    """
    t = str(texto or "")
    ancla = str(tag_ancla or "").strip()
    try:
        resto = [str(x or "").strip() for x in (extras or []) if str(x or "").strip()]
    except Exception:
        resto = []
    if not t or not ancla or not resto:
        return t
    try:
        m = re.search(re.escape(ancla), t, re.IGNORECASE)
        if not m:
            return t
        grupo = " ".join(resto)
        return f"{t[:m.end()]} {grupo}{t[m.end():]}"
    except Exception:
        return t


def _cortar_texto_a_limite(texto: str, limite: int) -> str:
    """Corta ``texto`` a ``limite`` (frase completa si puede); nunca lanza."""
    t = str(texto or "").strip()
    try:
        limite = max(1, int(limite))
    except (TypeError, ValueError):
        return t
    if len(t) <= limite:
        return t
    corte = None
    for signo in (".", "!", "?"):
        pos = t.rfind(signo, 0, limite)
        if pos >= limite // 2 and (corte is None or pos > corte):
            corte = pos
    if corte is not None:
        return t[: corte + 1].strip()
    pos = t.rfind(" ", 0, limite)
    if pos <= 0:
        pos = limite
    return t[:pos].rstrip()


def _recortar_limite_hashtag(
    texto, tags=None, limite: int = _MAX_LARGO_HASHTAG
) -> str:
    """Recorta un texto a ``limite`` caracteres sin dejarlo a medias si puede.

    - Si ya cabe, se devuelve tal cual (salvo que termine en hashtag: se
      reubica el grupo en el medio).
    - Busca el ultimo cierre de frase (``.``, ``!`` o ``?``) dentro del limite
      y a partir del 50% del limite; si existe, corta ahi (conservando el
      signo y la frase completa, sin ``…``).
    - Si no hay cierre, corta en el ultimo espacio antes del limite y agrega
      ``…`` SOLO en ese caso (corte de palabra).
    - Preserva los ``tags`` pedidos: si el recorte se llevo alguno, reinserta
      el grupo COMPLETO (los faltantes) EN MEDIO del texto recortado, sin
      volver a pasarse del limite. Garantiza que el texto nunca termine en
      hashtag.
    Nunca lanza: ante cualquier error devuelve el texto original.
    """
    try:
        t = str(texto or "").strip()
        limite = max(40, int(limite))
        if len(t) <= limite:
            rec = t
        else:
            # 1) Ultimo cierre de frase dentro del limite y desde la mitad.
            corte = None
            for signo in (".", "!", "?"):
                pos = t.rfind(signo, 0, limite)
                if pos >= limite // 2 and (corte is None or pos > corte):
                    corte = pos
            if corte is not None:
                rec = t[: corte + 1].strip()
            else:
                # 2) Corte en el ultimo espacio (frontera de palabra).
                pos = t.rfind(" ", 0, limite)
                if pos <= 0:
                    pos = limite
                rec = t[:pos].rstrip() + "…"

        if tags:
            etiquetas = [
                str(tag or "").strip()
                for tag in tags
                if str(tag or "").strip()
            ]
            # 3) Seguridad: si el recorte se llevo parte del grupo pedido, se
            #    reinsertan los faltantes EN MEDIO, dejando margen para no
            #    exceder otra vez el limite.
            faltantes = [
                tag for tag in etiquetas if tag.lower() not in rec.lower()
            ]
            if faltantes:
                grupo = " ".join(faltantes)
                margen = len(grupo) + 2
                if margen < limite:
                    # Abre espacio recortando mas el texto si hace falta.
                    disponible = limite - margen
                    if len(rec) > disponible:
                        rec = _cortar_texto_a_limite(rec, disponible)
                    ancla = next(
                        (tag for tag in etiquetas if tag.lower() in rec.lower()),
                        "",
                    )
                    if ancla:
                        rec = _limpiar_espacios_y_saltos(
                            _insertar_grupo_tras_tag(rec, ancla, faltantes)
                        )
                    else:
                        base = re.sub(
                            r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", rec
                        )
                        base = _limpiar_espacios_y_saltos(base) or rec
                        try:
                            con_tag = colocar_hashtag_en_medio(
                                base, hashtag=faltantes[0]
                            )
                            # Grafia EXACTA + grupo completo tras el primer tag.
                            con_tag = _reemplazar_tag_exacto(
                                con_tag, faltantes[0], grupo
                            )
                            if (
                                con_tag
                                and all(
                                    tag.lower() in con_tag.lower()
                                    for tag in etiquetas
                                )
                                and len(con_tag) <= limite
                            ):
                                rec = con_tag
                        except Exception:
                            pass

        # 4) Regla del proyecto: el texto NUNCA termina en hashtag. Si quedo el
        #    grupo al final (aunque sea antes del punto), se mueve al medio.
        if _hashtag_al_final(rec) and tags:
            grupo_exacto = [
                str(tag or "").strip()
                for tag in tags
                if str(tag or "").strip()
                and str(tag).strip().lower() in rec.lower()
            ]
            if grupo_exacto:
                grupo = " ".join(grupo_exacto)
                base = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", rec)
                base = _limpiar_espacios_y_saltos(base)
                if base:
                    try:
                        margen = len(grupo) + 2
                        if margen < limite and len(base) > limite - margen:
                            base = _cortar_texto_a_limite(
                                base, limite - margen
                            )
                        movido = colocar_hashtag_en_medio(
                            base, hashtag=grupo_exacto[0]
                        )
                        movido = _reemplazar_tag_exacto(
                            movido, grupo_exacto[0], grupo
                        )
                        if (
                            movido
                            and len(movido) <= limite
                            and not _hashtag_al_final(movido)
                        ):
                            rec = _limpiar_espacios_y_saltos(movido)
                    except Exception:
                        pass
        return rec
    except Exception as e:
        logger.error(f"Error recortando texto de hashtag: {e}")
        return str(texto or "").strip()


def _acotar_limite(texto, limite: int = _MAX_LARGO_HASHTAG) -> str:
    """Corte DURO de ultima barrera: deja ``texto`` en <= ``limite`` chars.

    Extrae los hashtags presentes (unicos, case-insensitive) y delega en
    `_recortar_limite_hashtag`, que recorta en cierre de frase o en el ultimo
    espacio + "…" (sin partir palabras), preserva/reinserta los hashtags EN
    MEDIO y garantiza que el texto NUNCA termine en hashtag.

    Es la ultima barrera del limite de 100 de posts/citas generados por IA:
    se aplica aunque la IA (o el relleno local/sufijos) se pase. Nunca lanza;
    si todo falla devuelve un corte seguro que respeta el limite.
    """
    try:
        t = str(texto or "").strip()
    except Exception:
        return texto
    if not t:
        return t
    try:
        limite = max(40, int(limite))
    except (TypeError, ValueError):
        limite = _MAX_LARGO_HASHTAG
    if len(t) <= limite:
        return t
    try:
        tags: list[str] = []
        vistos: set = set()
        for tag in re.findall(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", t):
            clave = tag.lower()
            if clave not in vistos:
                vistos.add(clave)
                tags.append(tag)
        acotado = _recortar_limite_hashtag(t, tags, limite)
        if acotado and len(acotado) <= limite:
            return acotado
    except Exception as e:
        logger.error(f"Error acotando texto a {limite} caracteres: {e}")
    # Ultimo recurso: corte seguro sin salir del limite.
    try:
        return _cortar_texto_a_limite(t, limite)
    except Exception:
        return t[:limite].rstrip()


# Plantillas locales (fallback sin OpenAI) para el rol "hashtags".
# Estructura: registro -> perfil -> plantillas. `{contexto}` y `{tag}` se usan
# SIEMPRE dentro de una opinion propia (prohibido devolver el contexto pelado o
# "contexto + hashtag"). La clave "generico" aplica cuando el registro o el
# perfil no se reconocen. El estilo humano ciudadano se aplica despues con
# `_humanizar_por_registro` (protege el hashtag para no deformarlo).
_PLANTILLAS_HASHTAGS = {
    "politica": {
        "formal": (
            "Compromiso con lo nuestro\n\n"
            "Hablando de {contexto}, conviene reconocer su valor en la vida "
            "diaria y el orgullo que despierta; {tag} expresa bien ese "
            "sentimiento colectivo.\n\n"
            "Sigamos construyendo identidad con información y respeto.",
            "Una mirada serena\n\n"
            "El tema de {contexto} merece atención y calma para valorar lo que "
            "aporta a la comunidad; {tag} acompaña esta reflexión.\n\n"
            "Mantengamos el diálogo informado.",
            "Orgullo bien entendido\n\n"
            "Reconocer {contexto} es reconocer lo que somos y lo que podemos "
            "compartir; {tag} lo resume con claridad.\n\n"
            "Ese orgullo se demuestra con hechos.",
        ),
        "ciudadano": (
            "Con esto de {contexto}, uno se da cuenta de lo mucho que tenemos; "
            "{tag} lo dice todo sin exagerar.",
            "Vale la pena detenerse en {contexto}: hay cosas que se disfrutan "
            "más cuando se comparten; {tag} va bien con esa idea.",
            "A veces se nos olvida lo bueno de {contexto}, y {tag} nos recuerda "
            "de dónde viene ese orgullo.",
        ),
        "popular": (
            "Con {contexto} y {tag}, así se siente el orgullo de lo nuestro.",
            "Hay que disfrutar {contexto}: {tag} y buena compañía lo dicen todo.",
            "Lo bueno de {contexto} merece contarse; {tag} lo resume en pocas "
            "palabras.",
        ),
    },
    "activista": {
        "formal": (
            "Apunte ciudadano\n\n"
            "Hablando de {contexto}, hay q reconocer el valor q tiene pa la "
            "comunidad; {tag} lo resume bien.\n\n"
            "Mantengamos el ojo en lo importante.",
            "Una reflexión breve\n\n"
            "Con esto de {contexto} queda claro q hay cosas q se defienden "
            "desde el barrio; {tag} acompaña ese ánimo.\n\n"
            "Sigamos informados.",
            "Contrapunto\n\n"
            "El debate sobre {contexto} merece seriedad y pa no perder el rumbo "
            "hay q escuchar a la gente; {tag} apunta a esa conversación.\n\n"
            "No nos distraigamos.",
        ),
        "ciudadano": (
            "Con esto de {contexto}, la verdad es q hay cosas q vale la pena "
            "defender; {tag} me parece un buen recordatorio.",
            "Tons con {contexto} queda claro q no todo está perdido; {tag} lo "
            "dice bien clarito.",
            "Hablando de {contexto}, xq hay motivos pa estar orgullosos; {tag} "
            "resume bien el sentimiento.",
        ),
        "popular": (
            "tons {tag} y {contexto}, todo cuenta pa sentirse bien",
            "q chido es {contexto}, pa eso esta {tag} y las ganas de compartir",
            "con {contexto} y {tag} queda claro q lo nuestro vale",
        ),
    },
    "ciudadana": {
        "formal": (
            "La neta de lo nuestro\n\n"
            "Me pongo a pensar en {contexto} y {tag} me hace sentir orgulloso "
            "de mi tierra.\n\n"
            "Hay que querer lo que tenemos.",
            "Orgullo de barrio\n\n"
            "Hablando de {contexto}, la neta hay mucho que agradecer; {tag} lo "
            "dice mejor que yo.\n\n"
            "Ojalá todos lo valoren.",
            "Así se siente\n\n"
            "Cuando se trata de {contexto}, {tag} me recuerda que lo bueno sí "
            "existe.\n\n"
            "Y no hay que olvidarlo.",
        ),
        "ciudadano": (
            "Con esto de {contexto}, la neta me dan ganas de compartir; {tag} "
            "lo resume bien.",
            "Hablando de {contexto}, la verdad {tag} dice lo que muchos "
            "pensamos.",
            "A mí me gusta {contexto} y {tag} lo deja bien claro, no manches.",
        ),
        "popular": (
            "tons {tag} y {contexto}, puro amor",
            "la neta {contexto} esta bien chido, {tag} y ya",
            "k chido es {contexto}, con {tag} todo se dice mejor",
        ),
    },
    "generico": {
        "formal": (
            "Una buena noticia\n\n"
            "Hablando de {contexto}, conviene valorar lo que tenemos; {tag} "
            "resume esa idea.\n\n"
            "Es un orgullo compartirlo.",
            "Motivo de conversación\n\n"
            "El tema de {contexto} da para comentar y reconocer lo bueno; {tag} "
            "invita a opinar.\n\n"
            "Sigamos conversando.",
            "Para reflexionar\n\n"
            "Detenerse en {contexto} ayuda a mirar lo que nos une; {tag} lo dice "
            "de forma sencilla.\n\n"
            "Compartamos la idea.",
        ),
        "ciudadano": (
            "Con esto de {contexto}, vale la pena conversar un rato; {tag} "
            "invita a opinar.",
            "Hablando de {contexto}, hay cosas buenas que conviene contar; "
            "{tag} lo resume bien.",
            "A veces no valoramos {contexto} hasta que lo vemos de cerca; {tag} "
            "lo recuerda.",
        ),
        "popular": (
            "con {contexto} y {tag}, así de simple",
            "hay cosas como {contexto} que se disfrutan, {tag} y ya",
            "qué bueno es {contexto}, {tag} y a disfrutar",
        ),
        "generico": (
            "Con esto de {contexto} uno aprende a valorar lo que tiene; {tag} "
            "lo dice bien.",
            "Hablando de {contexto}, siempre hay algo bueno que contar; {tag} "
            "resume esa idea.",
            "Cuando se trata de {contexto}, conviene opinar con calma; {tag} "
            "invita al diálogo.",
        ),
    },
}


def _normalizar_hashtags_pedidos(hashtags) -> list[str]:
    """Normaliza los hashtags pedidos (helper local, nunca lanza).

    Acepta un string ("#mexico, #futbol") o una lista; separa por comas,
    espacios y saltos de linea; garantiza el '#' y deduplica sin distinguir
    mayusculas/minusculas (conserva la grafia de la primera aparicion).
    """
    try:
        if not hashtags:
            return []
        if isinstance(hashtags, (list, tuple, set)):
            crudos = []
            for item in hashtags:
                crudos.extend(re.split(r"[,\s]+", str(item or "")))
        else:
            crudos = re.split(r"[,\s]+", str(hashtags))
    except Exception:
        return []

    resultado: list[str] = []
    vistos: set = set()
    for crudo in crudos:
        limpio = str(crudo or "").strip().lstrip("#").strip()
        if not limpio:
            continue
        partes = re.findall(r"[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", limpio)
        if not partes:
            continue
        tag = "#" + "".join(partes)
        clave = tag.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        resultado.append(tag)
    return resultado


def _subconjunto_hashtags_pedidos(tags, rng=None) -> list[str]:
    """Subconjunto aleatorio NO vacio de ``tags`` (1..len), en orden original.

    Con ``tags`` vacio devuelve ``[]``. Nunca lanza.
    """
    try:
        lista = []
        for tag in (tags or []):
            limpio = str(tag or "").strip()
            if limpio:
                lista.append(limpio)
    except Exception:
        return []
    if not lista:
        return []
    if rng is None:
        rng = random.Random()
    try:
        k = rng.randint(1, len(lista))
        indices = sorted(rng.sample(range(len(lista)), k))
    except Exception:
        return list(lista)
    return [lista[i] for i in indices]


def solo_hashtags_pedidos(texto: str, tags, semilla: int = 0) -> str:
    """Deja en ``texto`` SOLO un subconjunto aleatorio de los ``tags`` pedidos.

    - Normaliza ``tags`` con `_normalizar_hashtags_pedidos`; sin tags devuelve
      el texto intacto (NUNCA inventa hashtags).
    - Elige 1..len(tags) hashtags con ``random.Random(semilla)`` y elimina del
      texto CUALQUIER otro hashtag (case-insensitive). La grafia del hashtag
      pedido se conserva exacta (no se capitaliza a la fuerza).
    - Garantiza que el subconjunto elegido quede presente, integrado EN MEDIO
      (nunca al final): el primero con `colocar_hashtag_en_medio` y el resto
      justo despues de el, separados por un espacio (``... #mexico #futbol
      ...``).
    - Limpia espacios huerfanos (``" ,"``, dobles, saltos 3+). Si el texto
      queda vacio devuelve el original. Nunca lanza.
    """
    original = str(texto or "")
    t = original.strip()
    if not t:
        return original
    tags_norm = _normalizar_hashtags_pedidos(tags)
    if not tags_norm:
        return original
    try:
        rng = random.Random(int(semilla))
    except Exception:
        rng = random.Random(0)
    subset = _subconjunto_hashtags_pedidos(tags_norm, rng)
    if not subset:
        return original
    mapa_exacto = {tag.lower(): tag for tag in subset}

    def _conservar(m):
        # Conserva (con su grafia exacta pedida) solo los tags del subset.
        return mapa_exacto.get(m.group(0).lower(), " ")

    try:
        # 1) Fuera los hashtags que no fueron elegidos (y grafia exacta).
        limpio = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", _conservar, t)
        limpio = _limpiar_espacios_y_saltos(limpio)
        if not limpio:
            return original

        # 2) Garantiza TODO el subconjunto elegido, en medio del texto.
        if not all(tag.lower() in limpio.lower() for tag in subset):
            base = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", limpio)
            base = _limpiar_espacios_y_saltos(base) or limpio
            primero = subset[0]
            grupo = " ".join(subset)
            try:
                colocado = colocar_hashtag_en_medio(base, hashtag=primero)
            except Exception:
                colocado = f"{base} {primero}".strip()
            if not colocado:
                return original
            colocado = _reemplazar_tag_exacto(colocado, primero, grupo)
            limpio = _limpiar_espacios_y_saltos(colocado)

        # 3) Regla del proyecto: nunca terminar en hashtag.
        if _hashtag_al_final(limpio):
            base = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", limpio)
            base = _limpiar_espacios_y_saltos(base)
            if not base:
                return original
            primero = subset[0]
            grupo = " ".join(subset)
            try:
                movido = colocar_hashtag_en_medio(base, hashtag=primero)
            except Exception:
                movido = f"{primero} {base}".strip()
            if movido:
                movido = _reemplazar_tag_exacto(movido, primero, grupo)
                limpio = _limpiar_espacios_y_saltos(movido)

        if not limpio:
            return original
        return limpio
    except Exception as e:
        logger.error(f"Error aplicando solo_hashtags_pedidos: {e}")
        return original


# ===================================================================== #
# VALIDACION RIGUROSA del hashtag (pedido del dueño): el hashtag pedido
# debe quedar NATURAL EN MEDIO de la oracion y el texto final NO puede
# superar 100 caracteres. Se expone `validar_texto_hashtag` (diagnostico) y
# `garantizar_texto_hashtag` (pipeline determinista que SIEMPRE cumple).
# ===================================================================== #

# Articulos, determinantes y preposiciones que NO pueden quedar pegados justo
# antes de un hashtag. El bug historico fue insertarlo ENTRE el articulo y su
# sustantivo ("el orgullo #Mexico nacional"): ademas del token inmediatamente
# anterior, se detecta cuando el hashtag parte la frase
# "articulo/determinante + sustantivo".
_PALABRAS_ANTES_HASHTAG = frozenset({
    "el", "la", "los", "las",
    "un", "una", "unos", "unas",
    "este", "esta", "estos", "estas",
    "ese", "esa", "esos", "esas",
    "del", "al", "de",
})

_RE_TAG_TOKEN = re.compile(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+")
_RE_SOLO_PALABRA = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+")
_PUNTUACION_CLAUSULA = ".!?,;:"

# Plantillas minimas de ULTIMO recurso: hashtag tras dos puntos (limite de
# clausula), dentro de la ventana central y con texto COMPLETO a ambos lados.
# `{tag}` se sustituye por el grupo pedido (1..n hashtags).
_PLANTILLAS_GARANTIA_HASHTAG = (
    "Una reflexion: {tag} Gracias.",
    "Sobre lo que tenemos: {tag} Sigamos.",
    "Una opinion breve: {tag} Buen dia.",
)


def _tokens_antes_hashtag(texto, inicio: int) -> tuple:
    """Devuelve ``(ultimo, penultimo)`` token de la clausula previa al hashtag.

    Con un limite de clausula (puntuacion o salto de linea) justo antes del
    hashtag devuelve ``("", "")`` porque ahi SI es natural. Nunca lanza.
    """
    try:
        antes = str(texto or "")[: max(0, int(inicio))]
    except Exception:
        return "", ""
    if not antes:
        return "", ""
    if re.search(r"[\n\r][ \t]*$", antes):
        return "", ""
    recortado = antes.rstrip()
    if not recortado:
        return "", ""
    if recortado[-1] in ".,;:!?)»”’\"'":
        return "", ""
    fragmento = re.split(r"[.!?,;:\n\r]+", recortado)[-1]
    palabras = _RE_SOLO_PALABRA.findall(fragmento)
    if not palabras:
        return "", ""
    ultimo = palabras[-1].lower()
    penultimo = palabras[-2].lower() if len(palabras) >= 2 else ""
    return ultimo, penultimo


def validar_texto_hashtag(texto, hashtags, limite: int = 100) -> dict:
    """Valida ``texto`` contra las reglas duras de hashtags del proyecto.

    Reglas duras:
    * Deben estar TODOS los hashtags pedidos (normalizados con ``#`` y
      comparados case-insensitive) y NINGUN otro hashtag.
    * Cada hashtag pedido NO puede ser el inicio ni el final del texto y su
      posicion debe caer dentro de la ventana central 15%-85% del largo.
    * PROHIBIDO que el token inmediatamente anterior sea articulo,
      determinante o preposicion (`_PALABRAS_ANTES_HASHTAG`); tambien se
      marca cuando el hashtag parte la frase "articulo + sustantivo"
      (bug historico: ``"el orgullo #Mexico nacional"``).
    * ``len(texto) <= limite`` y el texto no puede terminar en hashtag.

    Devuelve un dict con ``ok``, ``largo``, ``dentro_limite``,
    ``hashtags_pedidos``, ``hashtags_presentes``, ``hashtags_faltantes``,
    ``hashtags_ajenos``, ``en_medio`` y ``problemas`` (lista de strings
    legibles en espanol). Nunca lanza: ante entradas invalidas devuelve el
    diagnostico con ``ok=False`` (o ``True`` si no habia nada que validar).

    Sin hashtags pedidos no se exige ninguno (retrocompatible); solo se
    validan el largo y que el texto no termine en hashtag.
    """
    problemas: list = []

    def _agregar(msg: str) -> None:
        if msg not in problemas:
            problemas.append(msg)

    try:
        t = str(texto or "")
    except Exception:
        t = ""
    largo = len(t)
    try:
        limite = max(1, int(limite))
    except (TypeError, ValueError):
        limite = _MAX_LARGO_HASHTAG

    pedidos = _normalizar_hashtags_pedidos(hashtags)
    claves_pedidas = {p.lower() for p in pedidos}
    tokens = [
        (m.group(0), m.start(), m.end()) for m in _RE_TAG_TOKEN.finditer(t)
    ]
    claves_en_texto = {tok.lower() for tok, _ini, _fin in tokens}

    presentes = [p for p in pedidos if p.lower() in claves_en_texto]
    faltantes = [p for p in pedidos if p.lower() not in claves_en_texto]

    ajenos: list = []
    if pedidos:
        vistos_ajenos: set = set()
        for tok, _ini, _fin in tokens:
            clave = tok.lower()
            if clave not in claves_pedidas and clave not in vistos_ajenos:
                vistos_ajenos.add(clave)
                ajenos.append(tok)

    dentro_limite = largo <= limite
    if not dentro_limite:
        _agregar(
            f"El texto supera el limite de {limite} caracteres "
            f"(largo={largo})."
        )
    if faltantes:
        _agregar(f"Faltan hashtags pedidos: {', '.join(faltantes)}.")
    if ajenos:
        _agregar(f"Hay hashtags no pedidos: {', '.join(ajenos)}.")

    termina_en_hashtag = bool(t) and _hashtag_al_final(t)
    if termina_en_hashtag:
        _agregar("El texto termina en hashtag (prohibido).")

    en_medio = bool(pedidos) and not faltantes
    for tok, ini, _fin in tokens:
        if tok.lower() not in claves_pedidas:
            continue
        if ini == 0:
            _agregar(f"{tok} esta al inicio del texto (prohibido).")
            en_medio = False
            continue
        fraccion = (ini / largo) if largo else 0.0
        if fraccion < 0.15 or fraccion > 0.85:
            _agregar(
                f"{tok} queda fuera de la ventana central 15%-85% "
                f"(posicion {ini} de {largo})."
            )
            en_medio = False
        if termina_en_hashtag:
            en_medio = False
        ultimo, penultimo = _tokens_antes_hashtag(t, ini)
        if ultimo in _PALABRAS_ANTES_HASHTAG:
            _agregar(
                f"{tok} esta pegado a un articulo/determinante "
                f"('... {ultimo} {tok}')."
            )
        elif penultimo in _PALABRAS_ANTES_HASHTAG:
            _agregar(
                f"{tok} parte la frase entre '{penultimo}' y su sustantivo "
                f"(ejemplo prohibido: 'el orgullo #Mexico nacional')."
            )

    return {
        "ok": not problemas,
        "largo": largo,
        "dentro_limite": dentro_limite,
        "hashtags_pedidos": list(pedidos),
        "hashtags_presentes": presentes,
        "hashtags_faltantes": faltantes,
        "hashtags_ajenos": ajenos,
        "en_medio": en_medio,
        "problemas": problemas,
    }


def _forzar_grupo_hashtags(texto: str, pedidos) -> str:
    """Asegura que TODOS los ``pedidos`` esten en ``texto`` (best effort).

    Si falta alguno, quita todos los hashtags, inserta el grupo COMPLETO EN
    MEDIO con `colocar_hashtag_en_medio` (restaurando la grafia exacta pedida)
    y limpia espacios. Nunca lanza.
    """
    t = str(texto or "").strip()
    if not t or not pedidos:
        return t
    try:
        if all(str(p).lower() in t.lower() for p in pedidos):
            return t
        base = _limpiar_espacios_y_saltos(_RE_TAG_TOKEN.sub(" ", t))
        if not base:
            base = "Una opinion breve"
        primero = str(pedidos[0])
        grupo = " ".join(str(p) for p in pedidos)
        try:
            colocado = colocar_hashtag_en_medio(base, hashtag=primero)
        except Exception:
            colocado = f"{base} {primero}".strip()
        colocado = _reemplazar_tag_exacto(colocado, primero, grupo)
        return _limpiar_espacios_y_saltos(colocado) or t
    except Exception:
        return t


def _reubicar_grupo_hashtags(texto: str, pedidos, limite: int) -> str:
    """Reubica el grupo COMPLETO de ``pedidos`` en el mejor punto natural.

    Prefiere insertarlo tras un cierre de frase (``. ! ? , ; :`` o salto de
    linea) cercano al medio y evita el token anterior prohibido; devuelve el
    primer candidato que pasa `validar_texto_hashtag` o ``""``. Nunca lanza.
    """
    t = str(texto or "").strip()
    if not t or not pedidos:
        return ""
    grupo = " ".join(str(p) for p in pedidos)
    try:
        limite = max(20, int(limite))
    except (TypeError, ValueError):
        limite = _MAX_LARGO_HASHTAG
    try:
        base = _limpiar_espacios_y_saltos(_RE_TAG_TOKEN.sub(" ", t))
        if not base:
            return ""
        presupuesto = max(20, limite - len(grupo) - 2)
        if len(base) > presupuesto:
            base = _cortar_texto_a_limite(base, presupuesto)
            base = _limpiar_espacios_y_saltos(base)
        if not base:
            return ""

        puntos: set = set()
        for m in re.finditer(r"[.!?,;:]+\s*|\n+\s*", base):
            puntos.add(m.end())
        for m in re.finditer(r"\s+", base):
            puntos.add(m.start())

        mitad = len(base) / 2.0
        candidatos = []
        for pos in puntos:
            if pos <= 0 or pos >= len(base):
                continue
            izq = base[:pos].rstrip()
            der = base[pos:].lstrip()
            if not izq or not der:
                continue
            tras_cierre = izq[-1] in _PUNTUACION_CLAUSULA
            ultimo, penultimo = _tokens_antes_hashtag(base, pos)
            pegado = (
                not tras_cierre
                and (
                    ultimo in _PALABRAS_ANTES_HASHTAG
                    or penultimo in _PALABRAS_ANTES_HASHTAG
                )
            )
            # 1) tras cierre de frase; 2) espacio con token previo seguro;
            # 3) espacio "pegado" a articulo (solo como ultimo recurso).
            prioridad = 0 if tras_cierre else (2 if pegado else 1)
            candidatos.append((prioridad, abs(pos - mitad), pos))
        candidatos.sort()

        for _prioridad, _dist, pos in candidatos:
            candidato = _limpiar_espacios_y_saltos(
                f"{base[:pos].rstrip()} {grupo} {base[pos:].lstrip()}"
            )
            if not candidato:
                continue
            if len(candidato) > limite:
                candidato = _acotar_limite(candidato, limite)
            if validar_texto_hashtag(candidato, pedidos, limite).get("ok"):
                return candidato
        return ""
    except Exception:
        return ""


def garantizar_texto_hashtag(
    texto, hashtags, limite: int = 100, semilla: int = 0
) -> str:
    """Devuelve SIEMPRE un texto valido con los hashtags pedidos.

    Pipeline determinista (nunca lanza):
    1. Aplica lo existente: `solo_hashtags_pedidos` (quita ajenos y conserva
       la grafia exacta), garantiza el grupo completo EN MEDIO y acota a
       ``limite`` con `_acotar_limite`/`_recortar_limite_hashtag`.
    2. Valida con `validar_texto_hashtag`; si aun no cumple, reubica el grupo
       tras el cierre de frase (``. ! ? , ; :`` o salto de linea) mas cercano
       al medio.
    3. Si sigue sin cumplir (p. ej. texto vacio o sin ningun limite natural),
       reconstruye con una plantilla local que SI cumple todas las reglas.
    4. Ante cualquier error devuelve el mejor texto posible limpio.

    Sin hashtags pedidos conserva el comportamiento clasico: solo limita el
    largo y evita terminar en hashtag (NUNCA inventa hashtags).
    """
    try:
        limite = max(20, int(limite))
    except (TypeError, ValueError):
        limite = _MAX_LARGO_HASHTAG
    try:
        pedidos = _normalizar_hashtags_pedidos(hashtags)
    except Exception:
        pedidos = []
    try:
        t = str(texto or "").strip()
    except Exception:
        t = ""

    try:
        if not pedidos:
            if len(t) > limite:
                t = _acotar_limite(t, limite)
            if t and _hashtag_al_final(t):
                t = _con_hashtag_en_medio(t) or t
            return t

        # 1) Pipeline existente.
        try:
            t = solo_hashtags_pedidos(t, pedidos, semilla) or t
        except Exception:
            pass
        t = _forzar_grupo_hashtags(t, pedidos)
        t = _acotar_limite(t, limite)
        if validar_texto_hashtag(t, pedidos, limite).get("ok"):
            return t

        # 2) Reubicacion natural tras el cierre de frase mas cercano al medio.
        reubicado = _reubicar_grupo_hashtags(t, pedidos, limite)
        if reubicado and validar_texto_hashtag(
            reubicado, pedidos, limite
        ).get("ok"):
            return reubicado

        # 3) Plantillas minimas locales que SI cumplen.
        grupo = " ".join(pedidos)
        for plantilla in _PLANTILLAS_GARANTIA_HASHTAG:
            candidato = _limpiar_espacios_y_saltos(
                plantilla.replace("{tag}", grupo)
            )
            if len(candidato) > limite:
                candidato = _acotar_limite(candidato, limite)
            if validar_texto_hashtag(candidato, pedidos, limite).get("ok"):
                return candidato

        # 4) Mejor esfuerzo.
        return reubicado or _limpiar_espacios_y_saltos(t)
    except Exception as e:
        logger.error(f"Error garantizando texto de hashtag: {e}")
        try:
            limpio = _limpiar_espacios_y_saltos(str(texto or ""))
            if len(limpio) > limite:
                limpio = _acotar_limite(limpio, limite)
            return limpio
        except Exception:
            return ""


def _plantillas_hashtags(registro: str = "", perfil: str = "") -> tuple:
    """Plantillas locales del rol hashtags para el (registro, perfil) dado.

    Cadena de fallback: (registro, perfil) -> (registro, generico) ->
    (generico, perfil) -> (generico, generico). Nunca lanza.
    """
    try:
        reg = normalizar_tipo_cuenta(registro) or "generico"
    except Exception:
        reg = "generico"
    try:
        perf = normalizar_perfil(perfil) or "generico"
    except Exception:
        perf = "generico"

    generico = _PLANTILLAS_HASHTAGS.get("generico", {})
    por_registro = _PLANTILLAS_HASHTAGS.get(reg) or generico
    plantillas = (
        por_registro.get(perf)
        or por_registro.get("generico")
        or generico.get(perf)
        or generico.get("generico")
    )
    return tuple(plantillas or ())


def _registro_normalizado(info) -> str:
    """Registro canonico de la cuenta ('politica'/'activista'/'ciudadana'/'')."""
    try:
        return normalizar_tipo_cuenta((info or {}).get("registro"))
    except Exception:
        return ""


def _perfil_normalizado(info) -> str:
    """Perfil canonico de la cuenta ('formal'/'ciudadano'/'popular'/'')."""
    try:
        return normalizar_perfil((info or {}).get("perfil"))
    except Exception:
        return ""


def _hashtag_al_final(texto: str) -> bool:
    """True si el texto termina con un hashtag (regla del proyecto: nunca)."""
    t = re.sub(r"[\s.,;:!?)…\"']+$", "", str(texto or "").rstrip())
    return bool(re.search(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+$", t))


def _es_copia_contexto(texto: str, contexto: str) -> bool:
    """True si el texto es el contexto pelado o 'contexto + hashtag'."""
    t = " ".join(str(texto or "").lower().split())
    ctx = " ".join(str(contexto or "").lower().split())
    if not t or not ctx:
        return False
    if t == ctx:
        return True
    sin_tags = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", t)
    return " ".join(sin_tags.split()) == ctx


def _personalidad_del_lote(cuentas_info, indices) -> str:
    """Personalidad del lote si TODAS los indices comparten una; si no, ''."""
    try:
        valores: list[str] = []
        for i in indices:
            info = _info_cuenta(cuentas_info, i)
            p = " ".join(str(info.get("personalidad") or "").split())
            if p and p not in valores:
                valores.append(p)
        if len(valores) == 1:
            return valores[0]
    except Exception:
        pass
    return ""


def _texto_hashtag_local(
    info, contexto: str, tags, indice, j, rng, inicio: int | None = None
) -> str:
    """Construye un post ORIGINAL local con el contexto y tags en medio.

    Elige una plantilla por (registro, perfil), sustituye `{contexto}` y
    `{tag}` (un subconjunto aleatorio de 1..len(tags) de los hashtags pedidos,
    unido por espacios) de forma natural y rota las plantillas con
    aleatoriedad para que los textos varíen entre cuentas y entre llamadas.
    ``inicio`` permite fijar el punto de rotacion por cuenta para que dos
    textos consecutivos de la misma cuenta NO repitan plantilla. Nunca lanza.
    """
    try:
        registro = _registro_normalizado(info)
        perfil = _perfil_normalizado(info)
        plantillas = _plantillas_hashtags(registro, perfil) or (
            "Con esto de {contexto} uno valora lo que tiene; {tag} lo dice bien.",
        )
        ctx = " ".join(str(contexto or "").split()) or "lo que tenemos"
        if tags:
            subset = _subconjunto_hashtags_pedidos(tags, rng)
            if not subset:
                subset = [str(tags[0] or "").strip()]
            tag_grupo = " ".join(subset)
        else:
            try:
                tag_grupo = elegir_hashtag("", rng=rng)
            except Exception:
                tag_grupo = "#Mexico"
            subset = [tag_grupo]
        k = int(indice) + int(j)
        if inicio is None:
            try:
                inicio = rng.randrange(len(plantillas))
            except Exception:
                inicio = 0
        plantilla = plantillas[(int(inicio) + k) % len(plantillas)]
        texto = (
            str(plantilla)
            .replace("{contexto}", ctx)
            .replace("{tag}", str(tag_grupo or ""))
            .strip()
        )
        # Contrato de largo tambien para el fallback local.
        return _recortar_limite_hashtag(texto, subset)
    except Exception as e:
        logger.error(f"Error construyendo texto local de hashtag: {e}")
        ctx = " ".join(str(contexto or "").split()) or "lo que tenemos"
        tag = (tags or ["#Mexico"])[0]
        return _recortar_limite_hashtag(
            f"Con esto de {ctx} uno aprende a valorarlo; {tag} lo dice bien.",
            [str(tag or "").strip()],
        )


def _garantizar_hashtag_pedido(texto: str, tags, semilla: int = 0) -> str:
    """Garantiza los hashtags pedidos (en medio) en el texto; nunca lanza.

    Con ``tags`` no vacio delega en `solo_hashtags_pedidos`: solo sobreviven
    hashtags pedidos (subconjunto aleatorio del texto) y el grupo queda en
    medio, nunca al final. Sin tags pedidos se conserva el comportamiento
    clasico: todo texto lleva al menos un hashtag integrado en medio.
    """
    t = str(texto or "").strip()
    if not t:
        return t
    if not tags:
        if not tiene_hashtag(t):
            return _con_hashtag_en_medio(t)
        return t
    return solo_hashtags_pedidos(t, tags, semilla)


def generar_textos_hashtags_por_cuenta(
    cuentas_info,
    hashtags="",
    contexto="",
    n_por_cuenta=1,
    narrativa="",
    entrenamiento="",
    callback=None,
) -> dict:
    """Genera posts ORIGINALES con hashtag(s), ``n_por_cuenta`` por cuenta.

    ``cuentas_info``: lista de dicts con "usuario", "registro"
    ("politica"/"activista"/"ciudadana"/""), "personalidad", "seccion",
    "nombre" y "perfil" ("formal"/"ciudadano"/"popular"/""); tolerante a
    claves faltantes o valores invalidos.

    Devuelve ``{usuario: [textos]}`` con EXACTAMENTE ``n_por_cuenta`` textos
    por cuenta. Cada texto es una publicacion ORIGINAL sobre ``contexto``
    (NUNCA el contexto copiado ni "contexto + hashtag"), respeta el registro y
    el perfil de la cuenta y lleva UN SUBCONJUNTO ALEATORIO (1..len) de los
    ``hashtags`` pedidos, bien escritos, integrados EN MEDIO del texto (nunca
    al final) y SIN ningun otro hashtag inventado.

    Agrupa por (registro, perfil) y pide a OpenAI en lotes de <=15 cuentas
    (``get_prompt_hashtags`` + temperature 0.9). Si OpenAI falla o devuelve
    menos, rellena con plantillas locales por registro/perfil. Nunca lanza.
    ``callback(hechas, total)`` opcional para progreso. Firma congelada.
    """
    try:
        lista = list(cuentas_info or [])
    except Exception:
        lista = []

    try:
        n = int(n_por_cuenta)
    except (TypeError, ValueError):
        n = 1
    if n < 0:
        n = 0

    tags = _normalizar_hashtags_pedidos(hashtags)
    contexto = " ".join(str(contexto or "").split())
    total = len(lista)
    total_textos = total * n
    hechas = 0

    def _reportar() -> None:
        if callback is None:
            return
        try:
            callback(hechas, total_textos)
        except Exception as e:
            logger.error(f"Error en callback de hashtags: {e}")

    resultado: list[list] = [[None] * n for _ in range(total)]
    vistos: set = set()
    rng = random.Random()

    # 1) OpenAI: lotes de <=15 cuentas agrupadas por (registro, perfil).
    if total and n:
        generador = None
        try:
            generador = GeneradorContenido()
        except Exception as e:
            logger.error(f"No se pudo crear GeneradorContenido para hashtags: {e}")

        if generador is not None:
            reglas = []
            for i in range(total):
                info = _info_cuenta(lista, i)
                reglas.append(
                    (_registro_normalizado(info), _perfil_normalizado(info))
                )
            grupos: dict = {}
            for i, clave in enumerate(reglas):
                grupos.setdefault(clave, []).append(i)

            for (registro, perfil), indices in grupos.items():
                for inicio in range(
                    0, len(indices), _MAX_CUENTAS_HASHTAGS_POR_LLAMADA
                ):
                    lote = indices[
                        inicio:inicio + _MAX_CUENTAS_HASHTAGS_POR_LLAMADA
                    ]
                    cantidad = n * len(lote)
                    try:
                        prompt = get_prompt_hashtags(
                            hashtags=" ".join(tags),
                            contexto=contexto,
                            narrativa=narrativa,
                            entrenamiento=entrenamiento,
                            cantidad=cantidad,
                            registro=registro,
                            personalidad=_personalidad_del_lote(lista, lote),
                            perfil=perfil,
                        )
                        content = generador._chat(prompt, temperature=0.9)
                        textos_lote = generador._parsear_textos(content, cantidad)
                    except Exception as e:
                        logger.error(f"Error OpenAI en lote de hashtags: {e}")
                        textos_lote = []

                    limpios: list[str] = []
                    vistos_lote: set = set()
                    for t in textos_lote or []:
                        t = generador._limpiar_texto(t)
                        if t and t not in vistos_lote:
                            vistos_lote.add(t)
                            limpios.append(t)

                    pos = 0
                    for idx_cuenta in lote:
                        for j in range(n):
                            if pos >= len(limpios):
                                break
                            if resultado[idx_cuenta][j] is None:
                                resultado[idx_cuenta][j] = limpios[pos]
                                pos += 1
                    _reportar()

    # 2) Normalizacion final por cuenta: relleno local, estilo del registro,
    #    contexto siempre original, subconjunto aleatorio de hashtags pedidos
    #    EN MEDIO (nunca otros) y unicidad global.
    for i in range(total):
        info = _info_cuenta(lista, i)
        registro = _registro_normalizado(info)
        # Punto de rotacion de plantillas fijo por cuenta: como el indice j
        # avanza, los textos consecutivos de la MISMA cuenta no repiten
        # plantilla (salvo que el registro/perfil tenga una sola).
        try:
            inicio_cuenta = rng.randrange(10 ** 6)
        except Exception:
            inicio_cuenta = i * 97

        def _estilizar(texto: str, semilla: int) -> str:
            return _humanizar_por_registro(texto, registro, semilla=semilla)

        for j in range(n):
            # Un subconjunto aleatorio POR TEXTO (1..len(tags)): es el UNICO
            # conjunto de hashtags valido para este texto. Sin tags pedidos se
            # conserva el comportamiento previo (hashtag en medio).
            subset = _subconjunto_hashtags_pedidos(tags, rng) if tags else []

            def _ajustar(texto: str, semilla: int) -> str:
                if subset:
                    return solo_hashtags_pedidos(texto, subset, semilla)
                return _garantizar_hashtag_pedido(texto, tags, semilla)

            def _acotar(texto: str) -> str:
                return _recortar_limite_hashtag(texto, subset or tags)

            t = str(resultado[i][j] or "").strip()
            if not t:
                t = _texto_hashtag_local(
                    info, contexto, tags, i, j, rng, inicio=inicio_cuenta
                )
            elif narrativa:
                # Red de seguridad: si el texto de la IA filtra la narrativa,
                # se cambia por el texto local de la MISMA maquinaria (que no
                # lee la narrativa), conservando hashtag pedido y estilo.
                t = _reemplazo_sin_fuga(
                    t,
                    narrativa,
                    lambda j=j: _texto_hashtag_local(
                        info, contexto, tags, i, j, rng, inicio=inicio_cuenta
                    ),
                )
                if not t:
                    t = _texto_hashtag_local(
                        info, contexto, tags, i, j, rng, inicio=inicio_cuenta
                    )
            t = _estilizar(t, rng.randint(1, 10 ** 9))
            t = _ajustar(t, i * 1000 + j)

            # Prohibido: contexto pelado o simple concatenacion contexto+tag.
            if _es_copia_contexto(t, contexto):
                t = _texto_hashtag_local(
                    info, contexto, tags, i + 7919, j, rng,
                    inicio=inicio_cuenta + 7919,
                )
                t = _estilizar(t, rng.randint(1, 10 ** 9))
                t = _ajustar(t, i * 1000 + j + 17)

            # Unicidad global (y por cuenta, que es subconjunto).
            if t in vistos:
                alterno = _texto_hashtag_local(
                    info, contexto, tags, i + 104729, j + len(vistos), rng,
                    inicio=inicio_cuenta + 104729,
                )
                alterno = _estilizar(alterno, rng.randint(1, 10 ** 9))
                alterno = _ajustar(alterno, i * 1000 + j + 997)
                if (
                    alterno
                    and alterno not in vistos
                    and not _es_copia_contexto(alterno, contexto)
                ):
                    t = alterno
                else:
                    t = _variar_hasta_unico(t, vistos)
                    t = _ajustar(t, i * 1000 + j + 1999)

            # Signos de apertura: activista/ciudadana nunca "¿"/"¡" (IA
            # incluida); politica conserva los signos correctos.
            t = _quitar_signos_por_registro(t, registro)
            # Ultima pasada: solo el subset pedido, en medio, y <= 100 chars.
            t = _ajustar(t, i * 1000 + j + 313)
            t = _acotar(t)
            # VALIDACION RIGUROSA (dueño): PASO FINAL de TODO texto (venga de
            # la IA, del fallback local o de cualquier recorte): el hashtag
            # pedido queda NATURAL EN MEDIO y el largo no supera los 100.
            t = garantizar_texto_hashtag(
                t, subset or tags,
                limite=_MAX_LARGO_HASHTAG,
                semilla=i * 1000 + j + 6161,
            )
            if t in vistos:
                # El recorte/garantia pudo colisionar: se varía y se
                # re-garantiza el texto alterno.
                alterno = _variar_hasta_unico(t, vistos)
                alterno = _ajustar(alterno, i * 1000 + j + 4919)
                alterno = _acotar(alterno)
                alterno = garantizar_texto_hashtag(
                    alterno, subset or tags,
                    limite=_MAX_LARGO_HASHTAG,
                    semilla=i * 1000 + j + 7919,
                )
                if alterno and alterno not in vistos:
                    t = alterno
            vistos.add(t)
            resultado[i][j] = t
            hechas += 1
            _reportar()

    # 3) Salida {usuario: [n textos]} (tolerante a usuario faltante/duplicado).
    salida: dict = {}
    usados: set = set()
    for i, cu in enumerate(lista):
        info = cu if isinstance(cu, dict) else {}
        usuario = str(info.get("usuario") or "").strip()
        clave = usuario or f"cuenta_{i + 1}"
        if clave in usados:
            sufijo = 2
            while f"{clave}_{sufijo}" in usados:
                sufijo += 1
            clave = f"{clave}_{sufijo}"
        usados.add(clave)
        salida[clave] = [
            str(resultado[i][j] or "").strip() for j in range(n)
        ]
    return salida
