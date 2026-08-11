#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MOTOR DE SEÑAL — Mercados Polymarket "Elon Musk # tweets" (ventanas de 48 h)
============================================================================
Estrategia objetiva y medible basada en:

    AVG7  = media de tweets/día de los últimos 7 días COMPLETOS
    V2    = total de tweets de los últimos 2 días completos
    R     = V2 / (2 × AVG7)                     (ratio de actividad reciente)
    λ48   = 2 × AVG7 × ajuste(R)                (tweets esperados en 48 h)
            ajuste(R) = clamp(1 + 0.5×(R−1), 0.5, 1.5)   (momentum con regresión
                                                          parcial hacia la media)
    X48 ~ Poisson(λ48)                          (modelo de conteo)

Reglas de entrada (TODAS obligatorias):
    R1  Mercado de ventana 48 h con reglas de resolución claras
        (conteo estándar de estos mercados: posts + quote posts + reposts)
    R2  Volumen ≥ $5.000 y liquidez ≥ $1.000 en el mercado
    R3  Cuota ≥ 3.00  →  precio del lado elegido ≤ 0.33
    R4  p_modelo ≥ 0.60  → candidato a YES
        p_modelo ≤ 0.30  → candidato a NO
    R5  AVG7 ≥ 5  (base mínima de datos para que el modelo sea estable)
    R6  UNA sola apuesta activa (secuencial). La siguiente apuesta solo
        se abre cuando la anterior se ha RESUELTO.
    R7  Progresión 3.30 × 1.5^(paso−1), reinicio a 3.30 tras ganar,
        stop-loss de ciclo a los 7 pasos.

CSV de entrada: columnas  fecha (YYYY-MM-DD), tweets (entero, día completo,
hora ET, mismo criterio de conteo que el mercado).

Uso:
    python3 senal.py --csv datos.csv                                  # señal HOY (bin genérico ≥1)
    python3 senal.py --csv datos.csv --bin 90 114 --precio 0.15      # evalúa un bin concreto
    python3 senal.py --csv datos.csv --bin 50 9999 --precio 0.33 --ya-publicados 12 --horas 6
    python3 senal.py --csv datos.csv --backtest --bin 50 9999 --precio 0.33
"""

import argparse
import csv
import math
import sys
from datetime import date, datetime, timedelta

# ----------------------------------------------------------------------------
# Parámetros fijos de la estrategia (no se cambian a mano: son las reglas)
# ----------------------------------------------------------------------------
STAKE_INICIAL   = 3.30          # $, primera apuesta de cada ciclo
FACTOR          = 1.53          # multiplicador tras cada fallo (óptimo para cuota 2.90)
CUOTA_MINIMA    = 2.90          # cuota mínima aceptada (precio ≤ 0.345)
P_MIN_YES       = 0.60          # p_modelo mínima para apostar YES
P_MAX_NO        = 0.30          # p_modelo máxima para apostar NO
PASOS_MAX       = 7             # stop-loss del ciclo (paso máximo)
AVG7_MIN        = 5.0           # AVG7 mínimo para operar
VOL_MIN         = 5_000         # $ volumen mínimo del mercado
LIQ_MIN         = 1_000         # $ liquidez mínima del mercado
BETA            = 0.5           # coeficiente de momentum del ajuste
AJUSTE_MIN, AJUSTE_MAX = 0.5, 1.5
# ----------------------------------------------------------------------------


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def poisson_cdf(k, lam):
    """P(X ≤ k) con X ~ Poisson(lam). Estable para lam hasta ~700;
    aproximación normal (corrección de continuidad) para lam mayor."""
    if lam <= 0:
        return 1.0 if k >= 0 else 0.0
    if lam > 700:
        z = (k + 0.5 - lam) / math.sqrt(lam)
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    s, term = 0.0, math.exp(-lam)
    kmax = max(0, int(k))
    for i in range(0, kmax + 1):
        s += term
        term *= lam / (i + 1)
    return min(1.0, max(0.0, s))


def p_bin(lo, hi, lam):
    """P(lo ≤ X ≤ hi) con X ~ Poisson(lam). hi puede ser inf."""
    if hi == math.inf:
        return 1.0 - poisson_cdf(lo - 1, lam)
    return poisson_cdf(hi, lam) - poisson_cdf(lo - 1, lam)


def tabla_apuestas(stake_inicial=STAKE_INICIAL, factor=FACTOR, pasos=PASOS_MAX):
    """Genera la tabla de apuestas (stake, pérdida acumulada, beneficio neto)."""
    filas = []
    perdida_prev = 0.0
    for i in range(1, pasos + 1):
        stake = math.ceil(stake_inicial * factor ** (i - 1) * 100) / 100  # redondeo al alza
        perdida_acum = perdida_prev + stake
        # beneficio neto si acierta a cuota 3.00 (pago = stake × 2):
        benef = round(stake * (CUOTA_MINIMA - 1) - perdida_prev, 2)
        filas.append((i, stake, perdida_acum, benef))
        perdida_prev = perdida_acum
    return filas


def cargar_csv(path):
    """Devuelve lista ordenada de (fecha, tweets)."""
    datos = []
    with open(path, newline="", encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            fecha = datetime.strptime(fila["fecha"].strip(), "%Y-%m-%d").date()
            tweets = int(float(fila["tweets"].strip()))
            datos.append((fecha, tweets))
    datos.sort(key=lambda x: x[0])
    if len(datos) < 9:
        sys.exit("ERROR: se necesitan al menos 9 días de datos completos (7 para AVG7 + 2 para V2).")
    return datos


def metricas(datos):
    """AVG7, V2, R, ajuste, λ48 a partir de los 7/2 últimos días COMPLETOS."""
    ult7 = [t for _, t in datos[-7:]]
    ult2 = [t for _, t in datos[-2:]]
    avg7 = sum(ult7) / 7.0
    v2 = sum(ult2)
    r = v2 / (2.0 * avg7) if avg7 > 0 else float("nan")
    ajuste = clamp(1.0 + BETA * (r - 1.0), AJUSTE_MIN, AJUSTE_MAX) if not math.isnan(r) else 1.0
    lam48 = 2.0 * avg7 * ajuste
    return dict(avg7=avg7, v2=v2, r=r, ajuste=ajuste, lam48=lam48,
                ult7=ult7, ult2=ult2, fecha_base=datos[-1][0])


def decision(m, lo, hi, precio_yes, ya_publicados=0, horas=0):
    """Aplica las reglas R1–R5 y devuelve (p_modelo, cuota_yes, cuota_no,
    veredicto, razon)."""
    if math.isnan(m["r"]):
        return None, None, None, "PASAR", "AVG7 = 0: sin actividad base"
    lam_rest = m["lam48"] * max(0.0, (48.0 - horas)) / 48.0
    p = p_bin(lo - ya_publicados, (hi - ya_publicados) if hi != math.inf else math.inf, lam_rest)
    cuota_yes = 1.0 / precio_yes if precio_yes > 0 else float("inf")
    precio_no = 1.0 - precio_yes
    cuota_no = 1.0 / precio_no if precio_no > 0 else float("inf")

    razones = []
    if m["avg7"] < AVG7_MIN:
        return p, cuota_yes, cuota_no, "PASAR", f"AVG7 = {m['avg7']:.1f} < {AVG7_MIN} (base insuficiente)"
    if precio_yes > 1 / CUOTA_MINIMA and (1 - precio_yes) > 1 / CUOTA_MINIMA:
        return p, cuota_yes, cuota_no, "PASAR", f"Ningún lado cumple cuota ≥ {CUOTA_MINIMA:.2f} (precio > {1/CUOTA_MINIMA:.2f} en ambos lados)"
    if p >= P_MIN_YES and precio_yes <= 1 / CUOTA_MINIMA:
        return p, cuota_yes, cuota_no, "APOSTAR YES", f"p_modelo {p:.1%} ≥ {P_MIN_YES:.0%} y cuota {cuota_yes:.2f} ≥ {CUOTA_MINIMA:.2f}"
    if p <= P_MAX_NO and precio_no <= 1 / CUOTA_MINIMA:
        return p, cuota_yes, cuota_no, "APOSTAR NO", f"p_modelo {p:.1%} ≤ {P_MAX_NO:.0%} y cuota NO {cuota_no:.2f} ≥ {CUOTA_MINIMA:.2f}"
    if p >= P_MIN_YES:
        return p, cuota_yes, cuota_no, "PASAR", f"p_modelo {p:.1%} alta pero precio YES {precio_yes:.3f} > {1/CUOTA_MINIMA:.3f} (cuota < {CUOTA_MINIMA:.2f})"
    if p <= P_MAX_NO:
        return p, cuota_yes, cuota_no, "PASAR", f"p_modelo {p:.1%} baja pero precio NO {precio_no:.3f} > {1/CUOTA_MINIMA:.3f} (cuota < {CUOTA_MINIMA:.2f})"
    return p, cuota_yes, cuota_no, "PASAR", f"p_modelo {p:.1%} sin ventaja (0.30 < p < 0.60)"


def formatear_senal(m, lo, hi, precio_yes, ya_publicados, horas, paso=None):
    p, c_yes, c_no, veredicto, razon = decision(m, lo, hi, precio_yes, ya_publicados, horas)
    print("=" * 72)
    print(f"SEÑAL  ·  fecha de datos: {m['fecha_base']}  ·  bin [{lo}, {hi if hi != math.inf else '∞'}]  ·  precio YES {precio_yes:.3f}")
    print("=" * 72)
    print(f"  Últimos 7 días        : {m['ult7']}")
    print(f"  Últimos 2 días        : {m['ult2']}")
    print(f"  AVG7 (media 7 días)   : {m['avg7']:.2f} tweets/día")
    print(f"  V2   (últimos 2 días) : {m['v2']} tweets")
    print(f"  R    = V2/(2·AVG7)    : {m['r']:.3f}   ({'bajo → posible recuperación' if m['r'] < 1 else 'alto → momentum'})")
    print(f"  ajuste(R)             : {m['ajuste']:.3f}")
    print(f"  λ48 (esperado 48 h)   : {m['lam48']:.1f} tweets")
    if horas > 0:
        print(f"  ya publicados         : {ya_publicados}  ·  horas transcurridas: {horas}")
    print(f"  p_modelo = P({lo} ≤ X ≤ {hi if hi != math.inf else '∞'}) : {p:.1%}" if p is not None else "  p_modelo = n/d")
    if c_yes is not None:
        print(f"  cuota YES = {c_yes:.2f}  ·  cuota NO = {c_no:.2f}")
    print(f"  ► VEREDICTO: {veredicto}")
    print(f"    Razón: {razon}")
    if veredicto.startswith("APOSTAR") and paso is not None:
        fila = tabla_apuestas()[paso - 1]
        print(f"    Paso {paso} del ciclo → stake ${fila[1]:.2f}  (pérdida acumulada si falla: ${fila[2]:.2f})")
    print()


def backtest(datos, lo, hi, precio_yes):
    """Simula la estrategia completa (reglas R1–R7) sobre el histórico.
    Supuestos documentados: entrada al inicio del día t (mercado de 48 h que
    cubre t+1 y t+2), precio de entrada FIJO = precio_yes (sin slippage),
    sin comisiones. El resultado real se calcula con los tweets observados."""
    apuestas = tabla_apuestas()
    ciclo = 0            # paso actual del ciclo (0 = sin ciclo abierto)
    pendiente_hasta = -1 # índice hasta el que hay una apuesta sin resolver
    saldo = 0.0
    max_exp = 0.0
    wins = losses = 0
    racha_p = racha_p_max = 0
    saldo_min = 0.0
    registro = []

    for t in range(9, len(datos) - 2):
        if t < pendiente_hasta:
            continue
        hist = datos[:t]                      # solo días ya completos
        m = metricas(hist)
        p, c_yes, c_no, veredicto, razon = decision(m, lo, hi, precio_yes)
        if not veredicto.startswith("APOSTAR"):
            continue
        if ciclo == 0:
            ciclo = 1
        paso = ciclo
        _, stake, perd_acum, benef = apuestas[paso - 1]
        max_exp = max(max_exp, perd_acum)
        real = datos[t + 1][1] + datos[t + 2][1]
        ok = (lo <= real <= hi) if hi != math.inf else real >= lo
        if ok:
            saldo += stake * (1 / precio_yes - 1)
            wins += 1
            racha_p = 0
            registro.append((datos[t][0], paso, stake, real, "G", round(saldo, 2)))
            ciclo = 0                          # reinicio del ciclo
        else:
            saldo -= stake
            losses += 1
            racha_p += 1
            racha_p_max = max(racha_p_max, racha_p)
            registro.append((datos[t][0], paso, stake, real, "P", round(saldo, 2)))
            if paso >= PASOS_MAX:
                ciclo = 0                      # stop-loss del ciclo
            else:
                ciclo = paso + 1
        saldo_min = min(saldo_min, saldo)
        pendiente_hasta = t + 3                # resuelve al final de t+2

    total = wins + losses
    print("=" * 72)
    print("BACKTEST (datos históricos)  ·  bin [%s, %s]  ·  precio YES fijo %.2f" %
          (lo, hi if hi != math.inf else "∞", precio_yes))
    print("=" * 72)
    print(f"  Apuestas totales      : {total}   (ganadas {wins}, perdidas {losses})")
    print(f"  Win rate              : {wins / total:.1%}" if total else "  Win rate: n/d")
    print(f"  Beneficio neto        : ${saldo:+.2f}")
    print(f"  Racha máx de pérdidas : {racha_p_max}")
    print(f"  Exposición máx. ciclo : ${max_exp:.2f}  (máx. teórica paso {PASOS_MAX}: ${apuestas[-1][2]:.2f})")
    print(f"  Peor saldo            : ${saldo_min:.2f}")
    print("\n  Paso a paso (fecha | paso | stake | tweets reales | resultado | saldo):")
    for fecha, paso, stake, real, res, sal in registro:
        print(f"    {fecha}  paso {paso}  ${stake:>7.2f}  real={real:>4}  {res}  saldo ${sal:>9.2f}")
    return registro


def main():
    ap = argparse.ArgumentParser(description="Motor de señal — mercados Polymarket de tweets de Elon Musk (48 h)")
    ap.add_argument("--csv", required=True, help="CSV con columnas fecha,tweets (días completos, hora ET)")
    ap.add_argument("--bin", nargs=2, type=int, metavar=("A", "B"), default=[1, 9999],
                    help="Bin del mercado [A, B]; B=9999 significa '≥ A'")
    ap.add_argument("--precio", type=float, default=0.33, help="Precio actual del YES (0-1)")
    ap.add_argument("--ya-publicados", type=int, default=0, help="Tweets ya publicados dentro de la ventana del mercado")
    ap.add_argument("--horas", type=float, default=0.0, help="Horas transcurridas de la ventana de 48 h")
    ap.add_argument("--paso", type=int, default=1, help="Paso actual del ciclo (1-7) para calcular el stake")
    ap.add_argument("--backtest", action="store_true", help="Simular la estrategia sobre el histórico")
    args = ap.parse_args()

    datos = cargar_csv(args.csv)
    lo, hi = args.bin
    hi = math.inf if hi >= 9999 else hi

    if args.backtest:
        backtest(datos, lo, hi, args.precio)
    else:
        m = metricas(datos)
        formatear_senal(m, lo, hi, args.precio, args.ya_publicados, args.horas, args.paso)
        # Tabla de apuestas de referencia
        print("Tabla de apuestas (ciclo):")
        print(f"  {'Paso':<5}{'Stake':>10}{'Pérdida acum.':>15}{'Neto si gana (cuota 3)':>24}")
        for paso, stake, perd, benef in tabla_apuestas():
            print(f"  {paso:<5}{stake:>10.2f}{perd:>15.2f}{benef:>24.2f}")


if __name__ == "__main__":
    main()
