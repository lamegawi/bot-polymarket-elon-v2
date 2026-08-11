#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEÑAL EN VIVO — integra los datos de tweets + los precios de Polymarket
=======================================================================
Flujo completo y automático:
  1) (opcional) refresca los datos de tweets (recoger_tweets.py --fuente jina)
  2) calcula AVG7 / V2 / R / λ48 desde datos_elon.csv (o usa overrides)
  3) descarga (o carga) los mercados activos de Polymarket y sus bins
  4) calcula p_modelo (Poisson) para cada bin, aplica las reglas R1-R7
     y da el veredicto: APOSTAR YES / APOSTAR NO / PASAR + stake del ciclo

USO:
  python3 senal_vivo.py                     # señal con datos actuales
  python3 senal_vivo.py --actualizar        # + refrescar precios Polymarket
  python3 senal_vivo.py --recoger           # + refrescar tweets
  python3 senal_vivo.py --paso 2            # paso actual del ciclo (stake)
  python3 senal_vivo.py --avg7 27 --v2 50   # override (pruebas / datos de
                                            # referencia de la semana resuelta)
"""
import argparse
import json
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import senal
import mercado_polymarket as mp

try:
    ET = ZoneInfo("America/New_York")
except Exception:
    # Windows sin tzdata: fallback a hora fija de verano (UTC-4)
    from datetime import timedelta
    from datetime import timezone as _tz
    ET = _tz(timedelta(hours=-4), name="EDT")
T_FMT = "%a %b %d %H:%M:%S +0000 %Y"


def conteo_ventana(inicio_utc, ahora_utc):
    """T0: tweets recogidos dentro de la ventana del mercado (dato PARCIAL
    hasta que el loop tenga cobertura continua de 24 h)."""
    try:
        estado = json.load(open("estado_tweets.json", encoding="utf-8")).get("tweets", {})
    except Exception:
        return 0, 0
    n = 0
    for v in estado.values():
        if v.get("kind") == "repost" and not v.get("exacto"):
            continue  # repost de xcancel sin hora exacta
        try:
            ts = datetime.strptime(v["created_at"], T_FMT).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if inicio_utc <= ts <= ahora_utc:
            n += 1
    return n, len(estado)


def evaluar(avg7, v2, ajuste, lam48, mercados, paso, t0_override=-1, ahora=None):
    """Evalúa todos los mercados abiertos y devuelve (evaluados, candidatas).
    - evaluados: lista de dicts {titulo, tipo, inicio, fin, horas_rest,
      lam_rest, t0, bins:[{titulo, lo, hi, precio_yes, cuota_yes, cuota_no,
      p_modelo, veredicto}]}
    - candidatas: lista de dicts {mercado, bin, lado, precio, cuota, p_modelo}
      que cumplen las reglas R3+R4 (para abrir apuesta de papel)."""
    ahora = ahora or datetime.now(timezone.utc)
    tabla = senal.tabla_apuestas()
    _, stake, _, _ = tabla[paso - 1] if 1 <= paso <= len(tabla) else tabla[0]
    evaluados, candidatas = [], []
    for mk in mercados:
        if mk["cerrado"] or not mk.get("inicio_iso"):
            continue
        inicio = datetime.fromisoformat(mk["inicio_iso"]).astimezone(timezone.utc)
        fin = datetime.fromisoformat(mk["fin_iso"]).astimezone(timezone.utc)
        if ahora < inicio or ahora > fin:
            continue
        horas_rest = (fin - ahora).total_seconds() / 3600
        if mk["tipo"] == "48h":
            lam_rest = lam48 * max(0.0, horas_rest) / 48.0
        elif mk["tipo"] == "semanal":
            lam_rest = 7 * avg7 * ajuste * max(0.0, horas_rest) / 168.0
        else:
            lam_rest = lam48 * max(0.0, horas_rest) / 48.0
        t0_auto, total_estado = conteo_ventana(inicio, ahora)
        t0 = t0_override if t0_override >= 0 else t0_auto
        bins = []
        for b in mk["bins"]:
            hi = b["hi"] if b["hi"] != float("inf") else math.inf
            p = senal.p_bin(b["lo"] - t0, (hi - t0) if hi != math.inf else math.inf, lam_rest)
            cy, cn = b["cuota_yes"], b["cuota_no"]
            veredicto, lado = "PASAR", None
            if p >= senal.P_MIN_YES and cy and cy >= senal.CUOTA_MINIMA:
                veredicto, lado = "APOSTAR YES", "YES"
            elif p <= senal.P_MAX_NO and cn and cn >= senal.CUOTA_MINIMA:
                veredicto, lado = "APOSTAR NO", "NO"
            bins.append({"titulo": b["titulo"], "lo": b["lo"], "hi": b["hi"],
                         "precio_yes": b["precio_yes"], "cuota_yes": cy,
                         "cuota_no": cn, "p_modelo": p, "veredicto": veredicto})
            if lado:
                candidatas.append({"mercado": mk["titulo"], "slug": mk["slug"],
                                   "ventana": (inicio, fin), "tipo": mk["tipo"],
                                   "bin_titulo": b["titulo"], "lo": b["lo"], "hi": b["hi"],
                                   "lado": lado, "precio": (b["precio_yes"] if lado == "YES" else 1 - b["precio_yes"]),
                                   "cuota": (cy if lado == "YES" else cn), "p_modelo": p,
                                   "stake": stake})
        evaluados.append({"titulo": mk["titulo"], "tipo": mk["tipo"],
                          "inicio": inicio, "fin": fin, "horas_rest": horas_rest,
                          "lam_rest": lam_rest, "t0": t0, "bins": bins})
    return evaluados, candidatas


def main():
    ap = argparse.ArgumentParser(description="Señal en vivo Polymarket · @elonmusk")
    ap.add_argument("--csv", default="datos_elon.csv", help="CSV de tweets (por defecto datos_elon.csv)")
    ap.add_argument("--actualizar", action="store_true", help="refrescar precios de Polymarket")
    ap.add_argument("--recoger", action="store_true", help="refrescar datos de tweets primero")
    ap.add_argument("--paso", type=int, default=1, help="paso actual del ciclo (1-7)")
    ap.add_argument("--avg7", type=float, default=None, help="override AVG7 (pruebas)")
    ap.add_argument("--v2", type=float, default=None, help="override V2 (pruebas)")
    ap.add_argument("--t0", type=int, default=-1, help="override tweets ya publicados en la ventana (-1 = automático)")
    ap.add_argument("--sin-reposts", action="store_true", help="excluir reposts")
    args = ap.parse_args()

    # ---------------------------------------------------------- 1) tweets
    if args.recoger:
        print("[1/4] Refrescando datos de tweets…")
        subprocess.run([sys.executable, "recoger_tweets.py", "--fuente", "jina"], check=False)

    # ---------------------------------------------------------- 2) métricas
    print("[2/4] Métricas de actividad…")
    if args.avg7 is not None and args.v2 is not None:
        avg7, v2 = args.avg7, args.v2
        r = v2 / (2 * avg7) if avg7 > 0 else float("nan")
        ajuste = senal.clamp(1 + 0.5 * (r - 1), 0.5, 1.5)
        lam48 = 2 * avg7 * ajuste
        origen = f"OVERRIDE (--avg7 {avg7} --v2 {v2})"
    else:
        try:
            datos = senal.cargar_csv(args.csv)
        except SystemExit as e:
            print(f"  ERROR: {e}")
            print("  Usa --avg7 X --v2 Y con datos de referencia (p. ej. del último")
            print("  mercado semanal resuelto) o espera a tener ≥ 9 días de datos.")
            return
        m = senal.metricas(datos)
        avg7, v2, r, ajuste, lam48 = m["avg7"], m["v2"], m["r"], m["ajuste"], m["lam48"]
        origen = f"datos propios ({len(datos)} días)"
    print(f"  AVG7 = {avg7:.2f} · V2 = {v2} · R = {r:.3f} · ajuste = {ajuste:.3f} · "
          f"λ48 = {lam48:.1f}   [{origen}]")

    # ---------------------------------------------------------- 3) mercado
    print("[3/4] Mercados Polymarket…")
    if args.actualizar:
        try:
            mp.actualizar_mercado()
            print("  precios actualizados ✓")
        except Exception as e:
            print(f"  [ERROR] no se pudo actualizar: {e}")
    try:
        mercados = json.load(open(mp.SALIDA, encoding="utf-8"))["mercados"]
    except Exception:
        print("  No hay mercado_activo.json. Ejecuta: python3 mercado_polymarket.py")
        return

    ahora = datetime.now(timezone.utc)
    tabla = senal.tabla_apuestas()
    _, stake, perd_acum, _ = tabla[args.paso - 1] if 1 <= args.paso <= len(tabla) else tabla[0]

    # ---------------------------------------------------------- 4) señal
    print("[4/4] Evaluación por bin…\n")
    evaluados, candidatas = evaluar(avg7, v2, ajuste, lam48, mercados, args.paso,
                                    t0_override=args.t0)
    for ev in evaluados:
        print(f"■ {ev['titulo']}  [{ev['tipo']}]")
        print(f"  ventana: {ev['inicio'].astimezone(ET).strftime('%m-%d %H:%M %Z')} → "
              f"{ev['fin'].astimezone(ET).strftime('%m-%d %H:%M %Z')}  ·  "
              f"ahora: {ahora.astimezone(ET).strftime('%m-%d %H:%M %Z')}")
        etiq = f"λ48={lam48:.1f}" if ev['tipo'] == "48h" else f"λ7={7*avg7*ajuste:.1f}"
        print(f"  {etiq} → λ restante={ev['lam_rest']:.1f} · horas restantes: {ev['horas_rest']:.1f} "
              f"· tweets ya en ventana (T0): {ev['t0']}")
        print(f"  {'bin':<10}{'p_modelo':>10}{'precio':>9}{'cuotaY':>8}{'cuotaN':>8}   veredicto")
        for b in ev["bins"]:
            cy = ("{:.2f}".format(b['cuota_yes']) if b['cuota_yes'] else '—')
            cn = ("{:.2f}".format(b['cuota_no']) if b['cuota_no'] else '—')
            print("  {:<10}{:>10.1%}{:>9.3f}{:>8}{:>8}   {}".format(
                b['titulo'], b['p_modelo'], b['precio_yes'], cy, cn, b['veredicto']))
        print()
    if not candidatas:
        print("► VEREDICTO GLOBAL: PASAR — ningún bin cumple (p_modelo ≥ 60% o ≤ 30%) "
              "con cuota ≥ 3.00.")
        print("  La regla es no apostar: la paciencia es parte de la estrategia.")
    else:
        print("► VEREDICTO GLOBAL: hay apuesta candidata (ver tabla). Recuerda:")
        print("  - una sola apuesta activa (espera a que la anterior se resuelva)")
        print(f"  - paso {args.paso} → stake ${stake:.2f} (pérdida acumulada si falla: ${perd_acum:.2f})")
        print("  - registra la operación en el Excel (hoja Registro)")


if __name__ == "__main__":
    main()
