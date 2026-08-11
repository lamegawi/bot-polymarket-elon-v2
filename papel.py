#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PAPER TRADING EN VIVO — prueba la estrategia sin dinero, con señales reales
===========================================================================
Cada ejecución (puede llamarse desde el loop de recogida con --papel o por
cron) hace:

  1) Si hay una apuesta de papel ACTIVA: comprueba si su mercado ya se
     resolvió (gamma-api de Polymarket) y, si es así, la RESUELVE:
     resultado G/P, beneficio, saldo, y pasa al siguiente paso del ciclo
     o reinicia tras ganar.
  2) Si no hay apuesta activa: evalúa las señales en vivo (mismas reglas
     R1-R7) y, si hay candidata (solo mercados de 48 h), la ABRE con el
     stake del paso actual del ciclo.
  3) Mantiene papel.json (estado) y resultados_papel.csv (historial,
     mismo formato que el simulador → sirve para generar el Excel).

USO:
  python3 papel.py                     # una pasada (abrir/resolver)
  python3 papel.py --actualizar        # + refrescar precios Polymarket
  python3 papel.py --excel             # + generar Resultados_Papel.xlsx
  # en el loop: recoger_tweets.py --loop ... --papel
"""
import argparse
import csv
import json
import math
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import senal
import senal_vivo
import mercado_polymarket as mp
import notificar

try:
    ET = ZoneInfo("America/New_York")
except Exception:
    # Windows sin tzdata: fallback a hora fija de verano (UTC-4)
    from datetime import timedelta
    from datetime import timezone as _tz
    ET = _tz(timedelta(hours=-4), name="EDT")
ESTADO = "papel.json"
HISTORIAL = "resultados_papel.csv"
BANKROLL = 500.0


def cargar_estado():
    if not os.path.exists(ESTADO):
        return {"saldo": BANKROLL, "paso": 1, "activa": None, "historial": []}
    try:
        d = json.load(open(ESTADO, encoding="utf-8"))
        d.setdefault("saldo", BANKROLL)
        d.setdefault("paso", 1)
        d.setdefault("activa", None)
        d.setdefault("historial", [])
        return d
    except Exception:
        return {"saldo": BANKROLL, "paso": 1, "activa": None, "historial": []}


def guardar_estado(estado):
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


def escribir_historial(historial):
    with open(HISTORIAL, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "fecha", "mercado", "bin", "lado", "precio", "cuota",
                    "p_modelo", "paso", "stake", "real", "resultado",
                    "beneficio", "saldo"])
        for tr in historial:
            w.writerow([tr.get("id", ""), tr["fecha"], tr.get("mercado", "—"),
                        tr["bin"], tr["lado"], tr["precio"], tr["cuota"],
                        tr["p_modelo"], tr["paso"], tr["stake"], tr["real"],
                        tr["resultado"], tr["beneficio"], tr["saldo"]])


def evento_resuelto(slug):
    """Devuelve (resuelto, winner_bin_titulo) si el mercado ya tiene ganador."""
    r = subprocess.run(["curl", "-s", "--max-time", "40",
                        f"https://gamma-api.polymarket.com/events?slug={slug}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        evs = json.loads(r.stdout)
        if not isinstance(evs, list) or not evs:
            return False, None
        ev = evs[0]
        if not ev.get("closed"):
            return False, None
        for m in ev.get("markets", []):
            try:
                p = json.loads(m.get("outcomePrices") or "[]")
            except Exception:
                continue
            if p and p[0] == "1":
                return True, m.get("groupItemTitle")
    except Exception:
        pass
    return False, None


def resolver(estado):
    """Si la apuesta activa tiene mercado resuelto, la cierra."""
    act = estado.get("activa")
    if not act:
        return False
    resuelto, winner_titulo = evento_resuelto(act["slug"])
    if not resuelto or not winner_titulo:
        return False
    win = mp.parse_bin(winner_titulo)
    if not win:
        return False
    # normalizar (lo, hi) con hi=None para '≥ X' (evita float('inf') en JSON)
    w_lo, w_hi = win
    w_hi = None if w_hi == float("inf") else w_hi
    if act["lado"] == "YES":
        ok = (w_lo == act["lo"] and w_hi == act["hi"])
    else:
        ok = not (w_lo == act["lo"] and w_hi == act["hi"])
    if ok:
        benef = round(act["stake"] * (act["cuota"] - 1), 2)
        estado["saldo"] += benef
        res = "G"
        estado["paso"] = 1
    else:
        benef = -round(act["stake"], 2)
        estado["saldo"] += benef
        res = "P"
        estado["paso"] = 1 if act["paso"] >= 7 else act["paso"] + 1
    registro = {"id": uuid.uuid4().hex[:12], "fecha": act["fecha"],
                "mercado": act.get("slug", "—"), "bin": act["bin_titulo"],
                "lado": act["lado"], "precio": act["precio"], "cuota": act["cuota"],
                "p_modelo": act["p_modelo"], "paso": act["paso"],
                "stake": round(act["stake"], 2), "real": winner_titulo,
                "resultado": res, "beneficio": round(benef, 2),
                "saldo": round(estado["saldo"], 2)}
    estado["historial"].append(registro)
    estado["activa"] = None
    escribir_historial(estado["historial"])
    guardar_estado(estado)
    print(f"  ✔ RESUELTA apuesta de papel del {act['fecha']}: {act['bin_titulo']} {act['lado']} "
          f"→ ganador {winner_titulo} → {res}  ${benef:+.2f}  (saldo ${estado['saldo']:.2f})")
    try:
        notificar.apuesta_cerrada(registro, estado["saldo"], prefijo="[V2-48H] ")
    except Exception:
        pass
    return True


def abrir(estado, actualizar=False):
    """Evalúa señales y abre apuesta de papel si hay candidata 48 h."""
    try:
        datos = senal.cargar_csv("datos_elon.csv")
    except SystemExit as e:
        print(f"  (sin datos suficientes: {e})")
        return False
    m = senal.metricas(datos)
    if actualizar:
        try:
            mp.actualizar_mercado()
        except Exception as e:
            print(f"  (no se pudo actualizar precios: {e})")
    try:
        mercados = json.load(open(mp.SALIDA, encoding="utf-8"))["mercados"]
    except Exception:
        print("  (sin mercado_activo.json)")
        return False
    _, candidatas = senal_vivo.evaluar(m["avg7"], m["v2"], m["ajuste"], m["lam48"],
                                       mercados, estado["paso"])
    candidatas = [c for c in candidatas if c["tipo"] == "48h"]
    if not candidatas:
        print(f"  (sin señal 48 h → no se abre apuesta de papel · paso actual: {estado['paso']})")
        return False
    c = candidatas[0]
    bin_titulo = c["bin_titulo"]
    estado["activa"] = {"slug": c["slug"], "fecha": datetime.now(ET).strftime("%Y-%m-%d"),
                        "bin_titulo": bin_titulo, "lo": c["lo"],
                        "hi": (c["hi"] if c["hi"] != math.inf else None),
                        "lado": c["lado"], "precio": round(c["precio"], 4),
                        "cuota": round(c["cuota"], 2), "p_modelo": round(c["p_modelo"], 4),
                        "paso": estado["paso"], "stake": c["stake"],
                        "ventana_fin": c["ventana"][1].isoformat()}
    guardar_estado(estado)
    print(f"  ✔ ABIERTA apuesta de papel: {c['mercado']} · bin {bin_titulo} {c['lado']} "
          f"a {c['precio']:.3f} (cuota {c['cuota']:.2f}, p_modelo {c['p_modelo']:.0%}) "
          f"· paso {estado['paso']} · stake ${c['stake']:.2f}")
    try:
        notificar.apuesta_abierta(estado["activa"], estado["saldo"], prefijo="[V2-48H] ")
    except Exception:
        pass
    return True


def pasada(actualizar=False, excel=False):
    """Una pasada completa de paper trading: resuelve si procede y abre si
    no hay apuesta activa. Devuelve el estado actualizado."""
    estado = cargar_estado()
    print(f"[{datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')}] Paper trading · "
          f"saldo ${estado['saldo']:.2f} · paso {estado['paso']}")

    if estado.get("activa"):
        resolver(estado)
    if not estado.get("activa"):
        abrir(estado, actualizar=actualizar)

    n = len(estado["historial"])
    if n:
        g = sum(1 for h in estado["historial"] if h["resultado"] == "G")
        print(f"  Historial: {n} apuestas resueltas · {g}G/{n-g}P · "
              f"beneficio ${sum(h['beneficio'] for h in estado['historial']):+.2f}")
    if excel and os.path.exists(HISTORIAL):
        try:
            from excel_historial import generar as gen_hist
            ruta, anadidas, total = gen_hist(HISTORIAL,
                                             salida="Historial_Operaciones.xlsx",
                                             bankroll=BANKROLL,
                                             titulo_extra="paper trading en vivo")
            print(f"Excel historial: {ruta} (añadidas {anadidas}, total {total})")
        except ImportError:
            print("openpyxl no instalado: pip install openpyxl")
    return estado


def main():
    ap = argparse.ArgumentParser(description="Paper trading en vivo")
    ap.add_argument("--actualizar", action="store_true", help="refrescar precios Polymarket")
    ap.add_argument("--excel", action="store_true", help="generar Resultados_Papel.xlsx")
    args = ap.parse_args()
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    pasada(actualizar=args.actualizar, excel=args.excel)


if __name__ == "__main__":
    main()
