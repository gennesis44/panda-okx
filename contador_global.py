import json
import os

HISTORIAL_FILE = "historial_operaciones.json"

def cargar_historial():
    if os.path.exists(HISTORIAL_FILE):
        with open(HISTORIAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"registro": []}

def mostrar_resumen_global():
    data = cargar_historial()
    registros = data.get("registro", [])

    if not registros:
        print("No hay operaciones registradas todavía.")
        return

    # Diccionario para agrupar por moneda
    resumen_por_par = {}

    for op in registros:
        par = op.get("par", "DESCONOCIDO")
        estado = op.get("estado", "").upper()

        if par not in resumen_por_par:
            resumen_por_par[par] = {"ganadas": 0, "perdidas": 0}

        if estado == "G":
            resumen_por_par[par]["ganadas"] += 1
        elif estado == "P":
            resumen_por_par[par]["perdidas"] += 1

    print("=========================================")
    print("   RESUMEN GLOBAL DE OPERACIONES (G/P)   ")
    print("=========================================")

    total_global_g = 0
    total_global_p = 0

    for par, stats in resumen_por_par.items():
        g = stats["ganadas"]
        p = stats["perdidas"]
        total_ops = g + p
        win_rate = (g / total_ops * 100) if total_ops > 0 else 0.0

        total_global_g += g
        total_global_p += p

        print(f"\nMoneda / Par: {par}")
        print(f"  * Ganadas (G)  : {g}")
        print(f"  * Perdidas (P) : {p}")
        print(f"  * Total Ops    : {total_ops}")
        print(f"  * Efectividad  : {win_rate:.1f}%")

    total_general = total_global_g + total_global_p
    win_rate_general = (total_global_g / total_general * 100) if total_general > 0 else 0.0

    print("\n-----------------------------------------")
    print(f"TOTAL GENERAL ACUMULADO:")
    print(f"  * Ganadas  : {total_global_g}")
    print(f"  * Perdidas : {total_global_p}")
    print(f"  * Efectividad Global: {win_rate_general:.1f}%")
    print("=========================================")

if __name__ == "__main__":
    mostrar_resumen_global()
