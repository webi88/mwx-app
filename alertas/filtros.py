class FiltrosGeograficos:
    def __init__(self):
        self.paises_extranjeros = [
            "argentina", "brasil", "colombia", "peru", "chile", "venezuela",
            "ecuador", "bolivia", "paraguay", "uruguay", "guatemala", "honduras",
            "el salvador", "nicaragua", "costa rica", "panama", "cuba",
            "república dominicana", "puerto rico"
        ]
        
        self.sinonimos_regiones = {
            "quintana roo": ["qroo", "qr", "cancun", "playa del Carmen", "Tulum", "Cozumel"],
            "mexico": ["cdmx", "ciudad de mexico", "estado de mexico", "edomex"],
        }
    
    def verificar_region(self, mencion: dict, localidad: str) -> bool:
        texto = mencion.get("titulo", "").lower()
        enlace = mencion.get("enlace", "").lower()
        
        localidad_lower = localidad.lower()
        
        tiene_mencion_local = False
        for region, sinonimos in self.sinonimos_regiones.items():
            if region in localidad_lower:
                if any(s.lower() in texto for s in sinonimos) or region in texto:
                    tiene_mencion_local = True
                    break
        
        if not tiene_mencion_local:
            if localidad_lower in texto:
                tiene_mencion_local = True
        
        tiene_pais_extranjero = any(pais in texto for pais in self.paises_extranjeros)
        
        if tiene_pais_extranjero and not tiene_mencion_local:
            return False
        
        return True


class FiltrosTematicos:
    def __init__(self):
        self.temas_excluidos = [
            "deportes", "futbol", "liga", "champions", "super bowl",
            "entretenimiento", "pelicula", "serie", "netflix", "disney",
            "turismo", "hotel", "resort", "vuelo", "aeropuerto",
            "gastronomia", "restaurante", "receta", "comida",
            "moda", "belleza", "maquillaje", "ropa"
        ]
    
    def excluir(self, mencion: dict, exclude_terms: list[str]) -> bool:
        texto = mencion.get("titulo", "").lower()
        
        for termino in exclude_terms:
            if termino.lower() in texto:
                return True
        
        for tema in self.temas_excluidos:
            if tema in texto:
                return True
        
        return False
    
    def verificar_keywords(self, mencion: dict, keywords: list[str], num_principales: int) -> dict:
        titulo = mencion.get("titulo", "").lower()
        
        kw_principales = keywords[:num_principales]
        kw_secundarias = keywords[num_principales:]
        
        for kw in kw_principales:
            if kw.lower() in titulo:
                return {"es_principal": True, "kw_principal": kw}
        
        for kw in kw_secundarias:
            if kw.lower() in titulo:
                return {"es_principal": False, "kw_principal": kw}
        
        return {"es_principal": False, "kw_principal": None}
