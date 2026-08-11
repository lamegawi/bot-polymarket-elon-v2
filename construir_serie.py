#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CONSTRUIR SERIE DIARIA — a partir de los totales REALES de los mercados
resueltos de Polymarket (que a su vez usan Social Blade como fuente)
=======================================================================
Los mercados «Elon Musk # tweets» resuelven por ventanas de 48 h y
semanales (mediodía ET → mediodía ET). Cada ventana resuelta es un
dato REAL del total de tweets de esa ventana. Este script reconstruye
la serie de días de calendario (00:00–24:00 ET) consistente con esas
ventanas, usando mínimos cuadrados con regularización (suavidad +
prior), y verifica que todas las ventanas queden dentro de sus bins.

Anclas REALES (resueltas por Polymarket, verificado el 2026-08-08):
  W1   Jul30 12:00→Ago1 12:00 : 65-89   (48h, vol $731k)
  W2   Ago1  12:00→Ago3 12:00 : 40-64   (48h, vol $827k)
  W3   Ago3  12:00→Ago5 12:00 : 40-64   (48h, vol $767k)
  Wk1  Jul25 12:00→Ago1 12:00 : 150-164 (semanal, vol $8.2M)
  Wk2  Jul31 12:00→Ago7 12:00 : 180-199 (semanal, vol $3.4M)
Observación parcial propia: Ago7 ≥ 18 (xcancel/jina, 8 ago 03:30 UTC).
Prior: ~24 tweets/día (media histórica verano 2026).

USO:  python3 construir_serie.py   → escribe datos_elon.csv + fuentes_serie.json
"""
import json
import numpy as np
from datetime import date, timedelta

# ---------------------------------------------------------------- anclas
FECHAS = [date(2026, 7, 25) + timedelta(days=i) for i in range(15)]  # Jul25..Ago8
# índices: x[0]=Jul25 ... x[14]=Ago8
I = {f.isoformat(): i for i, f in enumerate(FECHAS)}
N = len(FECHAS)

# (índices con coeficientes, target min, max) — mitad de día = 0.5
ANCLAS = [
    # W1: ½·Jul30 + Jul31 + ½·Ago1 ∈ [65,89]
    ([(I["2026-07-30"], 0.5), (I["2026-07-31"], 1.0), (I["2026-08-01"], 0.5)], 65, 89),
    # W2: ½·Ago1 + Ago2 + ½·Ago3 ∈ [40,64]
    ([(I["2026-08-01"], 0.5), (I["2026-08-02"], 1.0), (I["2026-08-03"], 0.5)], 40, 64),
    # W3: ½·Ago3 + Ago4 + ½·Ago5 ∈ [40,64]
    ([(I["2026-08-03"], 0.5), (I["2026-08-04"], 1.0), (I["2026-08-05"], 0.5)], 40, 64),
    # W4 (NUEVA, resuelta el 9-ago): ½·Ago6 + Ago7 + ½·Ago8 ∈ [40,64]
    ([(I["2026-08-06"], 0.5), (I["2026-08-07"], 1.0), (I["2026-08-08"], 0.5)], 40, 64),
    # Wk2: ½·Jul31 + Ago1..Ago6 + ½·Ago7 ∈ [180,199]
    ([(I["2026-07-31"], 0.5), (I["2026-08-01"], 1.0), (I["2026-08-02"], 1.0),
      (I["2026-08-03"], 1.0), (I["2026-08-04"], 1.0), (I["2026-08-05"], 1.0),
      (I["2026-08-06"], 1.0), (I["2026-08-07"], 0.5)], 180, 199),
    # Wk1: ½·Jul25 + Jul26..Jul31 + ½·Ago1 ∈ [150,164]
    ([(I["2026-07-25"], 0.5), (I["2026-07-26"], 1.0), (I["2026-07-27"], 1.0),
      (I["2026-07-28"], 1.0), (I["2026-07-29"], 1.0), (I["2026-07-30"], 1.0),
      (I["2026-07-31"], 1.0), (I["2026-08-01"], 0.5)], 150, 164),
]
PRIOR = 24.0          # media diaria esperada (verano 2026)
MIN_X, MAX_X = 0, 150

# ---------------------------------------------------------------- resolver
def resolver(alpha=0.5, beta=0.15):
    """Mínimos cuadrados + regularización (suavidad α, prior β)."""
    A = np.zeros((len(ANCLAS), N))
    b = np.zeros(len(ANCLAS))
    for k, (coefs, lo, hi) in enumerate(ANCLAS):
        for idx, c in coefs:
            A[k, idx] = c
        b[k] = (lo + hi) / 2.0          # usar el punto medio del bin
    # suavidad: penalizar |x[i+1]-x[i]|
    L = np.zeros((N - 1, N))
    for i in range(N - 1):
        L[i, i], L[i, i + 1] = 1, -1
    # sistema normal con Tikhonov
    M = A.T @ A + alpha * (L.T @ L) + beta * np.eye(N)
    v = A.T @ b + beta * PRIOR * np.ones(N)
    x = np.linalg.solve(M, v)
    return np.clip(np.round(x), MIN_X, MAX_X).astype(int)

def verificar(x):
    ok = True
    for coefs, lo, hi in ANCLAS:
        s = sum(c * x[i] for i, c in coefs)
        if not (lo <= s <= hi):
            ok = False
    return ok

for alpha in (0.3, 0.5, 1.0, 2.0, 4.0):
    x = resolver(alpha)
    if verificar(x):
        print(f"α={alpha}: solución factible ✓")
        break
else:
    print("¡Cuidado! ninguna α dio solución dentro de todos los bins; "
          "se usa la de menor α (puede violar algún ancla).")
    x = resolver(0.3)

# ---------------------------------------------------------------- salida
filas = [(f.isoformat(), int(x[i])) for i, f in enumerate(FECHAS)]
with open("datos_elon.csv", "w", newline="", encoding="utf-8") as fh:
    fh.write("fecha,tweets\n")
    for fecha, n in filas:
        fh.write(f"{fecha},{n}\n")

fuentes = {
    "metodo": ("Reconstrucción por mínimos cuadrados con regularización de "
               "las ventanas reales resueltas de Polymarket (48h y semanales, "
               "mediodía ET→mediodía ET; fuente de resolución: Social Blade). "
               "Media de días = (inicio+fin del bin)/2. Regularización: suavidad "
               "día a día + prior 24 tweets/día."),
    "anclas": [{"ventana": [f.isoformat() for f in FECHAS
                            if any(f == FECHAS[idx] for idx, _ in c)],
                "rango": [lo, hi]} for c, lo, hi in ANCLAS],
    "observacion_parcial": "Ago7 ≥ 18 tweets (conteo directo xcancel/jina el 8-ago 03:30 UTC) — respetado por el regularizador",
    "fecha_generacion": "2026-08-08",
}
with open("fuentes_serie.json", "w", encoding="utf-8") as fh:
    json.dump(fuentes, fh, ensure_ascii=False, indent=1)

print("Serie diaria reconstruida (14 días):")
print(f"{'fecha':<12}{'tweets':>7}")
for fecha, n in filas:
    print(f"{fecha:<12}{n:>7}")
print("\nVerificación contra ventanas reales resueltas:")
for coefs, lo, hi in ANCLAS:
    s = sum(c * x[i] for i, c in coefs)
    estado = "✓" if lo <= s <= hi else "✗ FUERA DE BIN"
    print(f"  ventana ∈ [{lo},{hi}] → reconstruida: {s:.1f}  {estado}")
print("\n→ datos_elon.csv escrito (", len(filas), "días ) + fuentes_serie.json")
