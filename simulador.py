#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIMULADOR DE PAPEL — prueba la estrategia SIN dinero
====================================================
Simula la estrategia completa sobre datos históricos (igual que el
backtest de senal.py pero con más detalles y análisis de riesgo):

  · Reglas R1-R7 idénticas (AVG7≥5, cuota≥3, p_modelo≥60% YES / ≤30% NO,
    una sola apuesta activa, secuencial, reinicio tras ganar).
  · Progresión 3.30 × 1.5^(paso−1); stop de ciclo en el paso 7 (pausa 1 día).
  · Lado AUTO: YES si p≥0.60 con cuota YES≥3, NO si p≤0.30 con cuota NO≥3.
  · Supuestos explícitos: mercado de 48 h disponible cada 2 días (la ventana
    siguiente al día de datos), precio de entrada fijo (sin comisiones,
    sin slippage salvo --slippage), resolución con el total real de tweets
    de la ventana (suma de los 2 días siguientes del CSV).

Salidas:
  · resultados_simulacion.csv  (cada operación: fecha, bin, lado, precio,
    cuota, p_modelo, paso, stake, real, resultado, beneficio, saldo)
  · Resultados_Simulacion.xlsx (Excel completo: resultados + KPIs + curva)

USO:
  python3 simulador.py --csv datos_elon.csv --bin 40 64 --precio 0.33 --excel
  python3 simulador.py --csv datos_ejemplo.csv --bin 50 9999 --precio 0.25
  python3 simulador.py --csv datos_ejemplo.csv --bin 50 9999 --precio 0.33 \
      --montecarlo 200 --slippage 0.02
"""
import argparse
import csv
import json
import math
import os
import random
import sys

import senal


def simular(datos, lo, hi, precio, lado="auto", bankroll=500.0,
            stake_inicial=3.30, factor=1.50, pasos=7, slippage=0.0, rng=None):
    """Ejecuta la estrategia sobre el histórico. Devuelve (trades, metrics).
    trades: lista de dicts; metrics: dict con KPIs de riesgo."""
    apuestas = senal.tabla_apuestas(stake_inicial, factor, pasos)
    ciclo = 0                # paso del ciclo (0 = sin ciclo abierto)
    pendiente_hasta = -1
    pausa_hasta = -1         # tras perder el ciclo completo: 1 día de pausa
    saldo = bankroll
    wins = losses = 0
    racha_p = racha_p_max = 0
    saldo_max = saldo
    saldo_min = saldo
    dd_max = 0.0
    expos_max = 0.0
    ciclos_completos = 0
    stops_aplicados = 0
    trades = []

    for t in range(9, len(datos) - 2):
        if t < pendiente_hasta or t < pausa_hasta:
            continue
        hist = datos[:t]
        m = senal.metricas(hist)
        # --- lado y señal ---
        p, c_yes, c_no, veredicto, _ = senal.decision(m, lo, hi, precio)
        if not veredicto.startswith("APOSTAR"):
            continue
        if lado == "auto":
            lado_ap = "YES" if veredicto == "APOSTAR YES" else "NO"
        else:
            lado_ap = lado
        if lado_ap == "YES":
            if rng is not None:
                precio_ent = precio * (1 + abs(rng.uniform(0, slippage)))
            else:
                precio_ent = precio
            cuota = 1 / precio_ent if precio_ent > 0 else math.inf
        else:
            if rng is not None:
                precio_ent = (1 - precio) * (1 + abs(rng.uniform(0, slippage)))
            else:
                precio_ent = 1 - precio
            cuota = 1 / precio_ent if precio_ent > 0 else math.inf
        if cuota < senal.CUOTA_MINIMA:
            continue
        if ciclo == 0:
            ciclo = 1
        paso = ciclo
        _, stake, perd_acum, _ = apuestas[paso - 1]
        expos_max = max(expos_max, perd_acum)
        real = datos[t + 1][1] + datos[t + 2][1]
        if lado_ap == "YES":
            ok = (lo <= real <= hi) if hi != math.inf else real >= lo
        else:
            ok = not ((lo <= real <= hi) if hi != math.inf else real >= lo)
        if ok:
            benef = stake * (cuota - 1)
            saldo += benef
            wins += 1
            racha_p = 0
            ciclo = 0
        else:
            benef = -stake
            saldo -= stake
            losses += 1
            racha_p += 1
            racha_p_max = max(racha_p_max, racha_p)
            if paso >= pasos:
                ciclo = 0
                stops_aplicados += 1
                pausa_hasta = t + 1
                ciclos_completos += 1
            else:
                ciclo = paso + 1
        saldo_max = max(saldo_max, saldo)
        saldo_min = min(saldo_min, saldo)
        dd_max = max(dd_max, saldo_max - saldo)
        trades.append({
            "fecha": datos[t][0].isoformat(), "bin_lo": lo, "bin_hi": hi,
            "lado": lado_ap, "precio": round(precio_ent, 4),
            "cuota": round(cuota, 2), "p_modelo": round(p, 4), "paso": paso,
            "stake": round(stake, 2), "real": real, "resultado": "G" if ok else "P",
            "beneficio": round(benef, 2), "saldo": round(saldo, 2)})
        pendiente_hasta = t + 3

    total = wins + losses
    metrics = {
        "apuestas": total, "ganadas": wins, "perdidas": losses,
        "win_rate": wins / total if total else 0.0,
        "beneficio_neto": round(saldo - bankroll, 2),
        "saldo_final": round(saldo, 2),
        "racha_max_perdidas": racha_p_max,
        "drawdown_max": round(dd_max, 2),
        "exposicion_max": round(expos_max, 2),
        "ciclos_completos": ciclos_completos, "stops_aplicados": stops_aplicados,
        "cuota_media": round(sum(x["cuota"] for x in trades) / total, 2) if total else 0.0,
        "total_invertido": round(sum(x["stake"] for x in trades), 2),
        "beneficio_por_apuesta": round((saldo - bankroll) / total, 2) if total else 0.0,
    }
    return trades, metrics


def escribir_csv(trades, ruta="resultados_simulacion.csv"):
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["fecha", "bin", "lado", "precio", "cuota", "p_modelo",
                    "paso", "stake", "real", "resultado", "beneficio", "saldo"])
        for tr in trades:
            bin_ = (f"{tr['bin_lo']}-{tr['bin_hi']}" if tr["bin_hi"] != math.inf
                    else f"{tr['bin_lo']}+")
            w.writerow([tr["fecha"], bin_, tr["lado"], tr["precio"], tr["cuota"],
                        tr["p_modelo"], tr["paso"], tr["stake"], tr["real"],
                        tr["resultado"], tr["beneficio"], tr["saldo"]])


def montecarlo(datos, lo, hi, precio, n=200, slippage=0.02, bankroll=500.0, seed=42):
    rng = random.Random(seed)
    beneficios, winrates = [], []
    for _ in range(n):
        tr, met = simular(datos, lo, hi, precio, bankroll=bankroll,
                          slippage=slippage, rng=rng)
        beneficios.append(met["beneficio_neto"])
        winrates.append(met["win_rate"])
    beneficios.sort()
    winrates.sort()
    def pct(x, p):
        return x[min(len(x) - 1, int(len(x) * p))]
    return {
        "n": n, "slippage": slippage,
        "beneficio": {"p5": pct(beneficios, .05), "p25": pct(beneficios, .25),
                      "p50": pct(beneficios, .50), "p75": pct(beneficios, .75),
                      "p95": pct(beneficios, .95)},
        "winrate": {"p5": pct(winrates, .05), "p50": pct(winrates, .50),
                    "p95": pct(winrates, .95)},
        "prob_perder": sum(1 for b in beneficios if b < 0) / len(beneficios),
    }


def main():
    ap = argparse.ArgumentParser(description="Simulador de papel — estrategia Polymarket · @elonmusk")
    ap.add_argument("--csv", default="datos_elon.csv", help="CSV de tweets (fecha,tweets)")
    ap.add_argument("--bin", nargs=2, type=int, metavar=("A", "B"), default=[40, 64],
                    help="bin del mercado [A,B]; B=9999 significa '≥A'")
    ap.add_argument("--precio", type=float, default=0.33, help="precio de entrada del YES (0-1)")
    ap.add_argument("--lado", choices=["auto", "YES", "NO"], default="auto")
    ap.add_argument("--bankroll", type=float, default=500.0, help="saldo inicial en $")
    ap.add_argument("--slippage", type=float, default=0.0,
                    help="deslizamiento simulado (0.02 = precio peor en un 2%)")
    ap.add_argument("--excel", action="store_true", help="generar Excel con los resultados")
    ap.add_argument("--salida", default="Resultados_Simulacion.xlsx",
                    help="nombre del Excel de salida (con --excel)")
    ap.add_argument("--montecarlo", type=int, default=0,
                    help="simular N escenarios con precios aleatorios (distribución de resultados)")
    args = ap.parse_args()

    datos = senal.cargar_csv(args.csv)
    lo, hi = args.bin
    hi = math.inf if hi >= 9999 else hi

    trades, met = simular(datos, lo, hi, args.precio, lado=args.lado,
                          bankroll=args.bankroll, slippage=args.slippage)
    escribir_csv(trades)

    print("=" * 78)
    print(f"SIMULADOR DE PAPEL — {len(datos)} días · bin [{lo}, {hi if hi != math.inf else '∞'}] "
          f"· precio {args.precio} · lado {args.lado} · bankroll ${args.bankroll:.0f}")
    print("=" * 78)
    if trades:
        print(f"{'fecha':<12}{'bin':<10}{'lado':<5}{'precio':>7}{'cuota':>7}{'p_mod':>7}"
              f"{'paso':>5}{'stake':>8}{'real':>6}{'res':>4}{'benef':>9}{'saldo':>9}")
        for tr in trades:
            bin_ = (f"{tr['bin_lo']}-{tr['bin_hi']}" if tr["bin_hi"] != math.inf
                    else f"{tr['bin_lo']}+")
            print(f"{tr['fecha']:<12}{bin_:<10}{tr['lado']:<5}{tr['precio']:>7.3f}"
                  f"{tr['cuota']:>7.2f}{tr['p_modelo']:>7.1%}{tr['paso']:>5}"
                  f"{tr['stake']:>8.2f}{tr['real']:>6}{tr['resultado']:>4}"
                  f"{tr['beneficio']:>+9.2f}{tr['saldo']:>9.2f}")
    print("-" * 78)
    print(f"Apuestas: {met['apuestas']}  (G {met['ganadas']} / P {met['perdidas']})  ·  "
          f"Win rate: {met['win_rate']:.1%}")
    print(f"Beneficio neto: ${met['beneficio_neto']:+.2f}  ·  Saldo final: ${met['saldo_final']:.2f}  "
          f"·  ROI: {met['beneficio_neto']/args.bankroll:+.1%}")
    print(f"Racha máx. de pérdidas: {met['racha_max_perdidas']}  ·  Drawdown máx.: ${met['drawdown_max']:.2f}  "
          f"·  Exposición máx.: ${met['exposicion_max']:.2f}")
    print(f"Ciclos completados: {met['ciclos_completos']}  ·  Stops aplicados: {met['stops_aplicados']}  "
          f"·  Cuota media: {met['cuota_media']:.2f}  ·  Total invertido: ${met['total_invertido']:.2f}")
    print(f"Beneficio por apuesta: ${met['beneficio_por_apuesta']:+.2f}")
    if args.excel:
        from generar_excel_resultados import generar
        ruta = generar("resultados_simulacion.csv", bankroll=args.bankroll,
                       titulo_extra=f"Bin [{lo}, {hi if hi != math.inf else '∞'}] @ {args.precio}",
                       salida=args.salida)
        print(f"\nExcel generado: {ruta}")

    if args.montecarlo:
        print("\n" + "=" * 78)
        mc = montecarlo(datos, lo, hi, args.precio, n=args.montecarlo,
                        slippage=args.slippage, bankroll=args.bankroll)
        print(f"MONTE CARLO ({mc['n']} escenarios con slippage {mc['slippage']:.0%}):")
        print(f"  Beneficio neto  —  p5: ${mc['beneficio']['p5']:+.2f}  p25: ${mc['beneficio']['p25']:+.2f}  "
              f"p50: ${mc['beneficio']['p50']:+.2f}  p75: ${mc['beneficio']['p75']:+.2f}  "
              f"p95: ${mc['beneficio']['p95']:+.2f}")
        print(f"  Win rate        —  p5: {mc['winrate']['p5']:.1%}  p50: {mc['winrate']['p50']:.1%}  "
              f"p95: {mc['winrate']['p95']:.1%}")
        print(f"  Prob. de terminar en pérdidas: {mc['prob_perder']:.1%}")
        with open("montecarlo.json", "w", encoding="utf-8") as f:
            json.dump(mc, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
