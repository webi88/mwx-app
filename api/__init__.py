# -*- coding: utf-8 -*-
"""API HTTP del ejecutable MWX APP.

Expone el contrato ``/v1`` que consume el cliente desktop (login con token
HMAC, perfil/campaña, estado de revocación, imágenes de campaña, proxy OpenAI
y auditoría de eventos). Se despliega como servicio INDEPENDIENTE (Railway):

    uvicorn api.main:app --host 0.0.0.0 --port $PORT

La API solo LEE las tablas ``App*``/``Cuenta`` del proyecto; el dashboard sigue
siendo la única interfaz administrativa.
"""

__all__ = []
