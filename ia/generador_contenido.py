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
from ia.prompts import (
    bloque_estilo_perfil,
    get_prompt_generico,
    get_prompt_verificado_ambiental,
    get_prompt_harfuch,
    get_prompt_por_tipo,
    instrucciones_tema,
    _normalizar_tema,
    _reglas_registro,
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
            return textos[:cantidad] if cantidad > 0 else textos

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
                "tener un MÁXIMO de 240 caracteres."
            )
            prompt += _reglas_registro(registro)
            prompt += bloque_estilo_perfil(perfil_norm)
            if perfil_norm:
                prompt += (
                    "\nEl texto DEBE incluir al menos un hashtag integrado EN MEDIO "
                    "del texto; NUNCA lo pongas al final.\n"
                )
            if narrativa:
                prompt += f"\n\nNARRATIVA GENERAL:\n{narrativa}"
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

        # Relleno 1: variaciones locales (sinonimos/hashtags).
        if len(limpios) < cantidad:
            try:
                from activaciones.variaciones import generar_pool_variaciones
                fallback = generar_pool_variaciones(base, cantidad - len(limpios))
            except Exception as e:
                logger.error(f"Error en fallback local de variaciones: {e}")
                fallback = []
            # Con registro ciudadano el fallback local tambien se humaniza
            # (antes del hashtag para no deformar el tag).
            humanizados = []
            for idx, t in enumerate(fallback):
                t = self._limpiar_texto(t)
                t = _humanizar_si_ciudadano(
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
                variante = _humanizar_si_ciudadano(
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

        return limpios[:cantidad]

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

        return unicos[:n_cuentas]

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
            # Estilo ciudadano en fallback (antes del hashtag) + hashtag en medio.
            try:
                relleno = _humanizar_si_ciudadano(
                    relleno, reglas[i][0], semilla=i + sufijo
                )
            except Exception:
                pass
            if reglas[i][1]:
                relleno = colocar_hashtag_en_medio(relleno)
            vistos.add(relleno)
            resultado[i] = relleno

    return [t or "" for t in resultado][:n_cuentas]


# ===================================================================== #
# Mantenimiento organico: textos variados por cuenta/tema/personalidad
# ===================================================================== #
_MAX_PERFILES_POR_LLAMADA_MANTENIMIENTO = 15

_TEMAS_MANTENIMIENTO = ("azteca", "dia", "tendencias", "gustos")

# Plantillas locales usadas cuando OpenAI falla o no alcanza.
# Estructura: tema -> perfil -> plantillas. "generico" conserva las clasicas
# (cuentas sin perfil definido). {nombre} se reemplaza por el nombre de la
# cuenta si existe. Cada perfil respeta su formato:
#   - formal: 3 bloques (Titulo / Descripcion / Conclusion) con doble enter.
#   - ciudadano: 1-2 frases de analisis intermedio.
#   - popular: 1 renglon casual con faltas intencionales (sin tildes,
#     q/pa/xq/tons/k) que igual se entiende.
_PLANTILLAS_MANTENIMIENTO = {
    "azteca": {
        "formal": (
            "Nuestras raices prehispanicas\n\n"
            "La grandeza de Mexico-Tenochtitlan se construyo con organizacion, "
            "conocimiento y comunidad; sus aportes siguen presentes en nuestra "
            "cultura.\n\n"
            "Recordar de donde venimos fortalece la identidad y el orgullo nacional.",
            "El legado mexica\n\n"
            "La astronomia, el arte y la herbolaria de nuestros antepasados son "
            "testimonio de una civilizacion sofisticada y profunda.\n\n"
            "Honrar esa memoria es reconocer la sabiduria que nos antecede.",
            "Memoria viva\n\n"
            "El Templo Mayor y los mercados prehispanicos recuerdan que Mexico ya "
            "era grande antes de la conquista.\n\n"
            "Su historia merece difundirse con respeto y orgullo.",
        ),
        "ciudadano": (
            "A veces se nos olvida que nuestra historia prehispanica sigue viva "
            "en la comida, las palabras y las tradiciones de todos los dias.",
            "Los mexicas nos dejaron una ciudad enorme y bien organizada; saber "
            "eso da orgullo y ganas de conocer mas de nuestras raices.",
            "Me gusta pensar que parte de lo que somos viene de siglos de arte, "
            "astronomia y comunidad; no es poca cosa.",
        ),
        "popular": (
            "q chido es recordar q nuestros abuelos ya sabian un buen, puro "
            "orgullo mexica",
            "los aztecas andaban bien adelantados pa su epoca, xq eso si es cultura",
            "tons si andas orgulloso de tus raices, presuma q es gratis",
        ),
        "generico": (
            "Ayer me puse a pensar en todo lo que construyeron nuestros abuelos. "
            "Mexico-Tenochtitlan no se entiende sin su gente. Puro orgullo mexica.",
            "El Templo Mayor sigue contandonos historias: cada piedra y cada ofrenda "
            "son memoria viva de lo que fuimos y de lo que seguimos siendo.",
            "No hay nada como recordar de donde venimos: arte mexica, herbolaria, "
            "mercados, palabra y comunidad. Raices que siguen bien firmes.",
            "Los mexicas median el tiempo con el sol y las estrellas. Su sabiduria "
            "sigue viva en nuestra memoria y en nuestras tradiciones.",
            "Un chocolate caliente y una platica de historia: asi sabe Mexico. "
            "Nuestras tradiciones ancestrales estan mas vivas que nunca.",
            "La grandeza de Mexico viene de siglos de sabiduria, astronomia, poesia "
            "y organizacion. Recordarlo es un acto de orgullo y de identidad.",
        ),
    },
    "dia": {
        "formal": (
            "Buen dia\n\n"
            "La jornada comienza y conviene ordenar prioridades con calma, sin "
            "perder el animo ni la claridad.\n\n"
            "Que el esfuerzo de hoy acerque cada meta pendiente.",
            "Agenda y actitud\n\n"
            "Cada dia trae pendientes, pero tambien oportunidades para avanzar y "
            "aprender algo nuevo.\n\n"
            "Cumplir con lo planeado tambien es una forma de cuidar el futuro.",
            "Reflexion de la manana\n\n"
            "Las efemerides y la actualidad recuerdan que la historia tambien se "
            "escribe en lo cotidiano.\n\n"
            "Buen dia a todas y todos.",
        ),
        "ciudadano": (
            "Buenos dias, hoy toca levantarse con animo y sacar los pendientes "
            "aunque la semana venga pesada.",
            "¿Como va su dia? Por aca ya con cafe en mano y ganas de que salgan "
            "bien las cosas.",
            "Hay dias que empiezan lentos, pero con buena actitud todo se acomoda "
            "mejor.",
        ),
        "popular": (
            "buenos dias, a levantarse q la chamba no se hace sola",
            "tons ya listos pa arrancar el dia? yo ando en modo cafe",
            "k tal ese animo? hoy si se puede con todo",
        ),
        "generico": (
            "Buenos dias. Hoy toca levantarse con animo, cumplir la agenda y no "
            "perder el humor. ¿Que trae su dia?",
            "Se va la semana y queda la sensacion de que hay mucho por hacer. "
            "¿Como va su dia, gente?",
            "Hoy amanecio fresco y con buena vibra por aca. Aprovechen para sacar "
            "eso que traen pendiente.",
            "Las efemerides nos recuerdan que la historia tambien se escribe en lo "
            "cotidiano. Buen dia a todos.",
            "El trafico, la chamba y los pendientes... pero aqui seguimos. "
            "¿Un cafe para arrancar?",
            "Dia de ordenar pendientes y proponerse algo nuevo. ¿Ustedes que plan "
            "traen para hoy?",
        ),
    },
    "tendencias": {
        "formal": (
            "Conversacion digital\n\n"
            "Temas como los del momento muestran que la sociedad participa y opina "
            "mas alla del ruido.\n\n"
            "Escuchar y contrastar informacion fortalece el debate publico.",
            "El debate de hoy\n\n"
            "La conversacion en redes refleja intereses legitimos de la ciudadania "
            "y merece analizarse con seriedad.\n\n"
            "Participar con respeto eleva la calidad del intercambio.",
            "Agenda publica\n\n"
            "Lo que hoy domina la conversacion digital tambien anticipa "
            "preocupaciones reales de la gente.\n\n"
            "Conviene informarse antes de opinar.",
        ),
        "ciudadano": (
            "Se esta hablando de ese tema en todos lados y la verdad vale la pena "
            "escuchar las distintas opiniones.",
            "¿Ya vieron lo que anda circulando hoy en redes? Esta bueno el debate, "
            "aunque hay de todo.",
            "El tema del momento tiene a la gente dividida pero conversando, y eso "
            "ya es avance.",
        ),
        "popular": (
            "ya viste q andan diciendo? esta bueno el chisme pero con respeto",
            "tons de q se esta hablando hoy? ando perdido en el timeline",
            "las redes andan q arden jajaja k opinan ustedes?",
        ),
        "generico": (
            "Vi que Mexico esta otra vez en la conversacion. Cuentenme, ¿de que se "
            "esta hablando hoy en sus redes?",
            "El tema del momento tiene a todos opinando. ¿Ustedes que piensan de lo "
            "que se esta diciendo?",
            "Las redes andan que arden hoy. ¿Ya vieron los memes que estan "
            "circulando?",
            "Se puso de moda hablar de esto y se agradece la conversacion. "
            "¿Cual es su opinion?",
            "Lo que hoy esta sonando en internet: musica nueva, estrenos y un par "
            "de sorpresas. ¿Que me recomiendan?",
            "El timeline esta que no se puede con tanto contenido bueno. "
            "¿Que estan viendo ustedes?",
        ),
    },
    "gustos": {
        "formal": (
            "Cocina y memoria\n\n"
            "La comida de casa conserva sabores e historias que ninguna moda "
            "gastronomica puede sustituir.\n\n"
            "Sentarse a la mesa tambien es un acto de identidad.",
            "Futbol y comunidad\n\n"
            "Un partido reune familias, amigos y vecinos alrededor de una misma "
            "pasion.\n\n"
            "Esos rituales cotidianos construyen pertenencia.",
            "Musica de siempre\n\n"
            "Las canciones que heredamos de nuestros mayores acompanan momentos "
            "que no vuelven.\n\n"
            "Cuidar esa musica es cuidar la memoria afectiva.",
        ),
        "ciudadano": (
            "Unos tacos con la familia arreglan cualquier dia pesado, la verdad.",
            "¿Cual es su cancion de domingo? A mi me gana la musica de antes.",
            "Me encanta desconectar con buena comida y una platica larga con los "
            "mios.",
        ),
        "popular": (
            "unos taquitos y ya, pa q mas",
            "k rico es comer en casa, nada le gana",
            "tons cual es su rolita favorita? yo ando en modo cumbia",
        ),
        "generico": (
            "Partido, buena compania y algo rico para botanear: no hay plan mas "
            "mexicano que ese.",
            "No hay tristeza que aguante unos tacos a la hora correcta. "
            "¿Cuales son sus favoritos?",
            "Me encanta la musica de antes: Jose Alfredo, Juan Gabriel, Los Bukis. "
            "¿Cual es su cancion de domingo?",
            "Con esto de la tecnologia hasta mi abuela pide videollamada. "
            "El mundo cambio y uno sigue extrañando las cartas a mano.",
            "Un viaje en carretera, una playlist y paisaje mexicano: plan perfecto "
            "para desconectar.",
            "La comida de casa sigue siendo el mejor restaurante del planeta. "
            "¿Cual es su platillo de la infancia?",
        ),
    },
}

# Plantillas locales de COMENTARIO/RESPUESTA por perfil (breves y
# conversacionales). "generico" aplica a cuentas sin perfil definido.
_PLANTILLAS_COMENTARIO = {
    "formal": (
        "De acuerdo con el planteamiento\n\n"
        "El argumento invita a reflexionar con seriedad sobre el tema.\n\n"
        "Conviene mantener el debate informado.",
        "Punto valido\n\n"
        "La publicacion aporta elementos que merecen considerarse con calma.\n\n"
        "Gracias por abrir la conversacion.",
        "Reflexion necesaria\n\n"
        "Es un tema que exige analisis y no solo reacciones inmediatas.\n\n"
        "Ojala se siga discutiendo con respeto.",
        "Buena aportacion\n\n"
        "Comparto la importancia de mirar el asunto con profundidad.\n\n"
        "Seguimos conversando.",
    ),
    "ciudadano": (
        "Buen punto, la verdad es que el tema da para pensar y conversar mas.",
        "Totalmente de acuerdo, hace falta hablar de esto con calma.",
        "Interesante lo que planteas, yo lo veo parecido aunque con matices.",
        "Asi es, ojala mas gente se sume a la conversacion.",
    ),
    "popular": (
        "x2, q bueno q se hable de esto",
        "jaja tienes razon, q bueno q lo dices",
        "tons q bueno q alguien lo dice, ya era hora",
        "k buen punto, pa eso estan las redes",
    ),
    "generico": (
        "Buen punto, hace falta seguir hablando de esto.",
        "Interesante lo que compartes, vale la pena conversarlo.",
        "De acuerdo, ojala se sume mas gente a la conversacion.",
    ),
}


def _plantillas_por_perfil(tema: str, perfil: str = "") -> tuple:
    """Plantillas de mantenimiento del tema para el perfil (o las genericas)."""
    entradas = _PLANTILLAS_MANTENIMIENTO.get(tema) or _PLANTILLAS_MANTENIMIENTO["gustos"]
    if not isinstance(entradas, dict):
        return tuple(entradas)
    try:
        clave = normalizar_perfil(perfil)
    except Exception:
        clave = ""
    plantillas = entradas.get(clave) if clave else None
    if not plantillas:
        plantillas = entradas.get("generico") or ()
    return tuple(plantillas)


def _plantillas_comentario(perfil: str = "") -> tuple:
    """Plantillas locales de comentario para el perfil (con generico de fallback)."""
    try:
        clave = normalizar_perfil(perfil)
    except Exception:
        clave = ""
    return tuple(
        _PLANTILLAS_COMENTARIO.get(clave) or _PLANTILLAS_COMENTARIO["generico"]
    )


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


# ===================================================================== #
# Estilo ciudadano local: humaniza el fallback sin OpenAI.
# Faltas frecuentes pero legibles + dislexias variadas + mayusculas
# inconsistentes + abreviaturas. Politico/activista NO usan esto.
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
    (r"\bhaber\b", "haver"),
    (r"\bvolver\b", "bolber"),
    (r"\bbueno\b", "bueno"),  # testigo
    # y/ll
    (r"\bllegó\b", "yego"),
    (r"\bllego\b", "yego"),
    (r"\bcalle\b", "caye"),
    # g/j
    (r"\bgente\b", "jente"),
    (r"\bméxico\b", "mejico"),
    (r"\bmexico\b", "mejico"),
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


def _aplicar_estilo_ciudadano_local(texto: str, semilla: int = 0) -> str:
    """Humaniza un texto al registro ciudadano (fallback local, nunca lanza).

    Aplica, con un RNG sembrado para variar entre textos: 3-6 rasgos humanos
    legibles (sin tildes, abreviaturas q/pa/xq/tons/k/tmb, dislexias b/d-q/p-
    s/c/z-h muda-y/ll, mayusculas inconsistentes, muletilla distinta). Maximo
    1 error por palabra; los hashtags se conservan intactos y bien escritos.
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

        # 3) Abreviaturas: 2-4 sustituciones por texto.
        cuerpo_low = cuerpo
        n_abrev = rng.randint(2, 4)
        candidatas = list(_ABREVIATURAS_CIUDADANO)
        rng.shuffle(candidatas)
        aplicadas = 0
        for patron, repl in candidatas:
            if aplicadas >= n_abrev:
                break
            nuevo, n = re.subn(patron, repl, cuerpo_low, count=1,
                               flags=re.IGNORECASE)
            if n:
                # Respeta minusculas del entorno ciudadano.
                cuerpo_low = nuevo
                aplicadas += 1
        cuerpo = cuerpo_low
        # 'que' -> 'k' ocasional (una sola vez, para variar).
        if rng.random() < 0.35:
            cuerpo = re.sub(r"\bq\b", "k", cuerpo, count=1)

        # 4) Dislexias: 1-3 sustituciones distintas por texto (lista + genericos
        #    para que CUALQUIER texto quede humanizado aunque no traiga las
        #    palabras exactas de la lista).
        n_dis = rng.randint(1, 3)
        dis = list(_DISLEXIAS_CIUDADANO)
        rng.shuffle(dis)
        hechas = 0
        for patron, repl in dis:
            if hechas >= n_dis:
                break
            if patron in (r"\bcasa\b", r"\bbueno\b"):
                continue  # testigos, no cambian
            nuevo, n = re.subn(patron, repl, cuerpo, count=1,
                               flags=re.IGNORECASE)
            if n:
                cuerpo = nuevo
                hechas += 1

        # 4b) Genericos (si la lista no alcanzo): una edicion legible sobre una
        # palabra cualquiera; maximo 1 error por palabra para seguir legible.
        if hechas < n_dis:
            # h muda inicial omitida: "hace"->"ace", "hay"->"ay", "hola"->"ola".
            if hechas < n_dis and rng.random() < 0.8:
                nuevo, n = re.subn(r"\b[Hh]([aeiouáéíóú])", r"\1", cuerpo,
                                   count=1)
                if n:
                    cuerpo, hechas = nuevo, hechas + 1
            # ll -> y: "llegar"->"yegar", "calle"->"caye" (una sola palabra).
            if hechas < n_dis and rng.random() < 0.6:
                nuevo, n = re.subn(r"ll", "y", cuerpo, count=1)
                # Evita romper el placeholder de hashtag.
                if n and "\x00" in cuerpo and nuevo.count("\x00") != cuerpo.count("\x00"):
                    pass
                elif n:
                    cuerpo, hechas = nuevo, hechas + 1
            # c+e/i -> s ("hacer"->"haser", "gracias"->"grasias").
            if hechas < n_dis and rng.random() < 0.6:
                nuevo, n = re.subn(r"[Cc]([eiéí])", r"s\1", cuerpo, count=1)
                if n:
                    cuerpo, hechas = nuevo, hechas + 1
            # b <-> v en una palabra ("volver"->"bolber", "bien"->"vien").
            if hechas < n_dis and rng.random() < 0.5:
                m = re.search(r"\b\w*[bvBV]\w*\b", cuerpo)
                if m:
                    palabra = m.group(0)
                    if "b" in palabra.lower():
                        nueva = palabra.replace("b", "v", 1).replace("B", "V", 1)
                    else:
                        nueva = palabra.replace("v", "b", 1).replace("V", "B", 1)
                    if nueva != palabra and len(palabra) > 3:
                        cuerpo = cuerpo[:m.start()] + nueva + cuerpo[m.end():]
                        hechas += 1

        # 5) Mayusculas/minusculas inconsistentes: inicio en minuscula +
        #    una palabra en MAYUSCULA suelta para enfasis (ocasional).
        cuerpo = cuerpo.strip()
        if cuerpo and rng.random() < 0.7:
            cuerpo = cuerpo[0].lower() + cuerpo[1:]
        if rng.random() < 0.35:
            palabras = cuerpo.split()
            if len(palabras) >= 3:
                idx = rng.randrange(1, len(palabras))
                limpia = re.sub(r"[^\wñÑ]", "", palabras[idx])
                if len(limpia) >= 3 and not limpia.startswith("\x00"):
                    palabras[idx] = palabras[idx].upper()
                    cuerpo = " ".join(palabras)

        # 6) Muletilla de apertura/cierre distinta por semilla (variacion).
        apertura = rng.choice(_MULETILLAS_CIUDADANO_INICIO)
        cierre = rng.choice(_MULETILLAS_CIUDADANO_CIERRE)
        if apertura and not cuerpo.lower().startswith(apertura.strip()[:4]):
            cuerpo = f"{apertura}{cuerpo}"
        if cierre and cierre.strip() not in cuerpo.lower()[-20:]:
            cuerpo = f"{cuerpo}{cierre}"

        # 7) Restaura hashtags intactos.
        for tag in tags:
            cuerpo = cuerpo.replace("\x00", tag, 1)
        cuerpo = cuerpo.replace("\x00", "").strip()
        cuerpo = re.sub(r"[ \t]{2,}", " ", cuerpo)
        return cuerpo.strip() or t
    except Exception as e:
        logger.error(f"Error humanizando estilo ciudadano: {e}")
        return t


def _humanizar_si_ciudadano(texto: str, registro, semilla: int = 0) -> str:
    """Aplica el estilo ciudadano solo si el registro lo pide (nunca lanza)."""
    try:
        if _es_registro_ciudadano(registro):
            return _aplicar_estilo_ciudadano_local(texto, semilla)
    except Exception:
        pass
    return texto


def _prompt_lote_mantenimiento(
    lote: list,
    cuentas_info: list,
    narrativa: str = "",
    entrenamiento: str = "",
) -> str:
    """Prompt para pedir UN texto por PERFIL (lote <=15), separados por '---'.

    Cada item del lote es ``(indice_cuenta, numero_texto, tema)`` y la info de
    la cuenta puede traer ``"perfil"`` (formal/ciudadano/popular) y
    ``"tipo_accion"`` ("post" o "comentario"). El prompt respeta formato,
    registro, personalidad y tema de cada cuenta, y exige hashtag en MEDIO.
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
        partes.append(f"NARRATIVA GENERAL:\n{narrativa}")
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
        linea = (
            f"PERFIL {idx} | usuario: {usuario or 'cuenta'} | registro: "
            f"{registro or 'libre'} | perfil: {etiqueta_perfil(perfil)} | "
            f"accion: {accion} | tema: {tema}"
        )
        linea += (
            "\nPERSONALIDAD DE LA CUENTA (respeta su tono e intereses): "
            f"{personalidad or 'persona mexicana comun'}"
        )
        reglas = _reglas_registro(registro).strip()
        if reglas:
            linea += f"\n{reglas}"
        estilo = bloque_estilo_perfil(perfil).strip()
        if estilo:
            linea += f"\n{estilo}"
        if accion == "comentario":
            linea += (
                "\nACCION COMENTARIO/RESPUESTA: texto apto para responder una "
                "publicacion; breve y conversacional (1-2 frases)."
            )
        lineas.append(linea)
    partes.append("\n".join(lineas))

    partes.append(
        "REGLAS DEL LOTE:\n"
        f"- Genera EXACTAMENTE {n_textos} textos DISTINTOS entre si, sin repetir "
        "frases, ideas, aperturas, muletillas ni estructuras (alterna anecdota / "
        "opinion directa / pregunta, pregunta-afirmacion-exclamacion, con y sin "
        "emoji, longitudes distintas).\n"
        "- Un texto por PERFIL, en el MISMO ORDEN en que aparecen.\n"
        "- Cada texto debe respetar el TEMA, la PERSONALIDAD, el REGISTRO y el "
        "FORMATO OBLIGATORIO POR PERFIL de su cuenta.\n"
        "- Con REGISTRO CIUDADANO: cada texto con 3-6 rasgos humanos legibles "
        "(faltas frecuentes, dislexias b/d-q/p-s/c/z-h muda-y/ll, mayusculas "
        "inconsistentes, abreviaturas q/pa/xq/tons/k) y variacion total entre "
        "textos; con REGISTRO POLITICO/ACTIVISTA: PROHIBIDOS los errores fuertes "
        "(politico ortografia impecable, activista maximo 2-3 licencias leves).\n"
        "- Los textos de COMENTARIO/RESPUESTA deben ser breves y conversacionales "
        "(1-2 frases; el perfil formal con sus 3 bloques pero cortos).\n"
        "- TODOS los textos DEBEN incluir al menos un hashtag INTEGRADO EN MEDIO "
        "del texto; NUNCA lo pongas al final (bien escrito, sin deformar).\n"
        "- Maximo 240 caracteres por texto.\n"
        "- NO numeres, NO uses etiquetas ni corchetes.\n"
        "- Responde SOLO con los textos separados por '---'."
    )
    return "\n\n".join(partes)


def _fallback_estructura_mantenimiento(cuentas_info, n_por_cuenta) -> list[list[str]]:
    """Ultimo recurso: estructura valida con textos no vacios (nunca lanza).

    Respeta el perfil/tipo_accion/registro de cada cuenta y garantiza hashtag
    en medio. Con registro ciudadano aplica el estilo humano local (faltas
    legibles + dislexias + mayusculas inconsistentes + abreviaturas).
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
    for i, cu in enumerate(lista):
        info = cu if isinstance(cu, dict) else {}
        perfil = str(info.get("perfil") or "").strip()
        registro = str(info.get("registro") or "").strip()
        accion = _normalizar_accion(info.get("tipo_accion"))
        if accion == "comentario":
            plantillas = _plantillas_comentario(perfil)
        else:
            plantillas = _PLANTILLAS_MANTENIMIENTO["gustos"]["generico"]
        fila = []
        vistos: set = set()
        for j in range(n):
            t = plantillas[(i + j) % len(plantillas)]
            # Estilo ciudadano ANTES del hashtag (no deforma el tag).
            t = _humanizar_si_ciudadano(t, registro, semilla=i * 100 + j)
            t = _con_hashtag_en_medio(t)
            if t in vistos:
                t = _variar_hasta_unico(t, vistos)
                t = _humanizar_si_ciudadano(t, registro, semilla=i * 100 + j + 997)
                t = _con_hashtag_en_medio(t)
            vistos.add(t)
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

    # 1) Un slot por (cuenta, tuit) con tema rotativo; se agrupan por tema.
    slots = []
    for i in range(total_cuentas):
        for j in range(n):
            tema = temas_limpios[(i * n + j) % len(temas_limpios)]
            slots.append((i, j, tema))
    slots_ordenados = []
    for tema in temas_limpios:
        slots_ordenados.extend(s for s in slots if s[2] == tema)

    generador = None
    try:
        generador = GeneradorContenido()
    except Exception as e:
        logger.error(f"No se pudo crear GeneradorContenido: {e}")

    # 2) OpenAI: lotes de <=15 perfiles, un texto por perfil separado por '---'.
    if generador is not None:
        for inicio in range(
            0, len(slots_ordenados), _MAX_PERFILES_POR_LLAMADA_MANTENIMIENTO
        ):
            lote = slots_ordenados[
                inicio:inicio + _MAX_PERFILES_POR_LLAMADA_MANTENIMIENTO
            ]
            textos_lote = []
            try:
                prompt = _prompt_lote_mantenimiento(
                    lote, lista, narrativa, entrenamiento
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
    #    unicos en lo posible, NUNCA vacios y con hashtag integrado EN MEDIO.
    for i in range(total_cuentas):
        info = _info_cuenta(lista, i)
        nombre = str(info.get("nombre") or info.get("usuario") or "").strip()
        personalidad = str(info.get("personalidad") or "").strip()
        perfil = str(info.get("perfil") or "").strip()
        registro = str(info.get("registro") or "").strip()
        accion = _normalizar_accion(info.get("tipo_accion"))

        fila: list[str] = []
        vistos: set = set()
        for j in range(n):
            t = (resultado[i][j] or "").strip()
            es_fallback = not bool(t)
            if not t:
                tema = temas_limpios[(i * n + j) % len(temas_limpios)]
                if accion == "comentario":
                    plantillas = _plantillas_comentario(perfil)
                else:
                    plantillas = _plantillas_por_perfil(tema, perfil)
                t = (
                    plantillas[(i * n + j) % len(plantillas)]
                    .replace("{nombre}", nombre or "amig@")
                    .strip()
                )
                t += _pie_personal(nombre, personalidad)
                hechas += 1
                _reportar()
            # Estilo ciudadano en TODOS los textos de ese registro (OpenAI y
            # fallback): el LLM tiende a devolver texto limpio aunque el prompt
            # pida errores, asi que se garantiza aqui. Se aplica ANTES del
            # hashtag para no deformar el tag; con semilla por (cuenta, texto)
            # para que cada texto varie.
            t = _humanizar_si_ciudadano(t, registro, semilla=i * 100 + j)
            t = _con_hashtag_en_medio(t)
            if t in vistos:
                t = _variar_hasta_unico(t, vistos)
                t = _humanizar_si_ciudadano(
                    t, registro, semilla=i * 100 + j + 997
                )
                t = _con_hashtag_en_medio(t)
            vistos.add(t)
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
    temas default: ["azteca", "dia", "tendencias", "gustos"].
    - Asigna temas rotativos por cuenta/tuit (variedad garantizada).
    - Agrupa por tema en lotes de <=15 cuentas: pide a OpenAI UN texto por
      personalidad (repitiendo registro, personalidad, perfil y accion de cada
      una) separados por '---' y parsea en orden.
    - Si OpenAI falla o devuelve menos, rellena con plantillas locales
      (>=3 por perfil en cada tema, mas comentarios por perfil) + variaciones +
      el nombre/personalidad, garantizando n_por_cuenta textos NO VACIOS.
    - TODOS los textos finales llevan un hashtag INTEGRADO EN MEDIO
      (`core.perfiles.colocar_hashtag_en_medio`), nunca al final.
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
) -> list[list[str]]:
    """Genera n_por_cuenta COMENTARIOS/RESPUESTAS por cuenta (alineados).

    Misma firma/contrato que `generar_textos_mantenimiento`: nunca lanza,
    devuelve len(cuentas_info) listas de exactamente n_por_cuenta textos no
    vacios (o el fallback estructural). Cada texto usa el PERFIL de su cuenta
    (formal/ciudadano/popular), es apto para responder (breve y conversacional,
    el perfil formal con Titulo/Descripcion/Conclusion cortos) y lleva un
    hashtag INTEGRADO EN MEDIO del texto.
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
            t = _humanizar_si_ciudadano(t, registro, semilla=i * 100 + j + 7)
            t = _con_hashtag_en_medio(t)
            if t in vistos_local:
                t = _variar_hasta_unico(t, vistos_local)
                t = _humanizar_si_ciudadano(
                    t, registro, semilla=i * 100 + j + 1997
                )
                t = _con_hashtag_en_medio(t)
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
    - ``comentarios``: textos breves aptos para responder.
    - ``citas``: textos para citas/RTs del tweet principal; si hay
      ``base_cita`` son variaciones de esa cita, si no son posts alternos.
    - Los 9 textos de cada cuenta son DISTINTOS entre si (sin duplicados en lo
      posible), respetan registro (ciudadano con errores humanos legibles;
      politico/activista sin errores fuertes), perfil y pasan TODOS por
      ``colocar_hashtag_en_medio`` (hashtag obligatorio EN MEDIO, nunca al
      final, bien escrito).
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

        def _unico(texto: str, registro, semilla: int) -> str:
            t = (texto or "").strip()
            if not t:
                t = "Buen dia a todos."
            t = _con_hashtag_en_medio(t)
            if t in vistos:
                t = _variar_hasta_unico(t, vistos)
                t = _humanizar_si_ciudadano(t, registro, semilla=semilla)
                t = _con_hashtag_en_medio(t)
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
            fila_coms.append(_unico(t, registro, semilla=i * 1000 + 100 + j))
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
                _unico(f"Comentario extra {len(fila_coms) + 1}", registro,
                       semilla=i * 1000 + 400 + len(fila_coms))
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
