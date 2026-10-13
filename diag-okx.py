# diag-okx.py — DIAGNOSTICO OKX · SOLO LECTURA · NO OPERA NADA
#   MISION (decreto Carbono 14-oct):
#     1. Extraer fills-history con TODOS los campos crudos (JSON)
#        → resolver: existe 'fillFee'? 'pnl' o 'fillPnl'? 'subType' real?
#     2. Verificar el tipo de trigger de los algo-orders vivos
#        (mark price vs last price — el misterio de los 2 ticks de AVAX)
#     3. Confirmar estructura exacta que los bots deben leer
#        (cooldown dead bug fill.get('pnl') — pieza #1 del fix)
#     4. (ENMIENDA 14-oct, este puerto): positions-history con 'fee' y
#        'fundingFee' → NETO REAL de los 53 cierres + conciliacion del conteo.
#        La estimacion ~$0.80 merece numero, no promedio.
#   Ax3: SOLO lectura (private GET) · cero ordenes · cero modificaciones
#        · falla ruidosa por seccion · sin opinion
#   VIAJE: murio 2 veces en transito ('exchange = cc'). Regenerado completo
#        por Silicon — un pegado, sin parches (A565G).

import json
import os
import sys

import ccxt

HOST = 'https://my.okx.com'
LIMIT = '100'  # maximo OK
