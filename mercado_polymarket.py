#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MERCADOS POLYMARKET — «Elon Musk # tweets» (precios y cuotas en vivo)
=====================================================================
Descarga los mercados de Polymarket sobre el nº de tweets de @elonmusk,
parsea los bins (rangos) y precios YES, calcula las cuotas (=1/precio),
identifica la ventana de resolución (48 h, semanal, mensual) y guarda
todo en mercado_activo.json.

USO:
  python3 mercado_polymarket.py            # tabla en pantalla + guardar JSON
  python3 mercado_polymarket.py --json     # solo salida JSON (para scripting)
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    ET = ZoneInfo("America/New_York")
except Exception:
    # Windows sin tzdata: fallback a hora fija de verano (UTC-4)
    from datetime import timedelta
    from datetime import timezone as _tz
    ET = _tz(timedelta(hours=-4), name="EDT")
GAMMA = "https://gamma-api.polymarket.com/public-search?q=%22elon%20musk%22&limit=100"
SALIDA = "mercado_activo.json"
MESES = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
         "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
         "December": 12}


def curl(url):
    r = subprocess.run(["curl", "-s", "--max-time", "40", url],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("curl falló o respuesta vacía")
    return json.loads(r.stdout)


def fetch_events():
    d = curl(GAMMA)
    return [e for e in d.get("events", []) if "tweets" in (e.get("title") or "").lower()]


def parse_bin(titulo):
    """'<40' → (0,39) · '40-64' → (40,64) · '90+' → (90,∞) · '1000+' etc."""
    t = (titulo or "").strip()
    m = re.match(r"^<(\d+)$", t)
    if m:
        return (0, int(m.group(1)) - 1)
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", t)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d+)\+$", t)
    if m:
        return (int(m.group(1)), float("inf"))
    return None


def parse_ventana(desc, end_iso):
    """Extrae de la descripción: 'from August 6 12:00 PM ET to August 8, 2026 12:00 PM ET'."""
    pat = re.compile(
        r"from ([A-Z][a-z]+) (\d{1,2})(?:,? (\d{4}))? (\d{1,2}):(\d{2}) ([AP]M) ET "
        r"to ([A-Z][a-z]+) (\d{1,2})(?:,? (\d{4}))? (\d{1,2}):(\d{2}) ([AP]M) ET")
    m = pat.search(desc or "")
    if not m:
        return None

    def to_dt(month, day, year, hh, mm, ap):
        anio = int(year) if year else int(end_iso[:4])
        h = int(hh) % 12 + (12 if ap == "PM" else 0)
        return datetime(anio, MESES[month], int(day), h, int(mm), tzinfo=ET)

    inicio = to_dt(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))
    fin = to_dt(m.group(7), m.group(8), m.group(9), m.group(10), m.group(11), m.group(12))
    if inicio >= fin:
        return None
    return inicio, fin


def procesar(events):
    res = []
    for ev in events:
        bins = []
        for m in ev.get("markets", []):
            bin_ = parse_bin(m.get("groupItemTitle") or "")
            if not bin_:
                continue
            try:
                precios = json.loads(m.get("outcomePrices") or "[]")
            except Exception:
                continue
            if len(precios) < 2:
                continue
            try:
                px = float(precios[0])
                pn = float(precios[1]) if precios[1] not in (None, "") else 1 - px
            except Exception:
                continue
            cuota_yes = 1 / px if px > 0 else None
            cuota_no = 1 / (1 - px) if px < 1 else None
            try:
                volumen = float(m.get("volume") or 0)
            except Exception:
                volumen = 0
            bins.append({"titulo": m.get("groupItemTitle"), "lo": bin_[0],
                         "hi": bin_[1], "precio_yes": round(px, 4),
                         "precio_no": round(pn, 4), "cuota_yes": cuota_yes,
                         "cuota_no": cuota_no, "volumen": volumen})
        if not bins:
            continue
        ventana = parse_ventana(ev.get("description"), ev.get("endDate") or "")
        dur_h = None
        tipo = "otro"
        if ventana:
            dur_h = (ventana[1] - ventana[0]).total_seconds() / 3600
            tipo = ("48h" if abs(dur_h - 48) < 2 else
                    "semanal" if abs(dur_h - 168) < 8 else "otro")
        res.append({
            "id": ev.get("id"), "titulo": ev.get("title"), "slug": ev.get("slug"),
            "cerrado": bool(ev.get("closed")), "volumen": ev.get("volume"),
            "endDate": ev.get("endDate"),
            "inicio_iso": ventana[0].isoformat() if ventana else None,
            "fin_iso": ventana[1].isoformat() if ventana else None,
            "duracion_h": dur_h, "tipo": tipo, "bins": bins})
    res.sort(key=lambda x: (x["cerrado"], x["fin_iso"] or ""))
    return res


def actualizar_mercado():
    """Descarga, procesa y guarda mercado_activo.json. Devuelve la lista."""
    res = procesar(fetch_events())
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump({"actualizado": datetime.now(timezone.utc).isoformat(),
                   "mercados": res}, f, ensure_ascii=False, indent=1)
    return res


def main():
    ap = argparse.ArgumentParser(description="Mercados Polymarket de tweets de @elonmusk")
    ap.add_argument("--json", action="store_true", help="salida solo JSON")
    args = ap.parse_args()
    res = actualizar_mercado()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    print(f"Mercados Polymarket «Elon Musk # tweets»  (actualizado: "
          f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')})")
    print("=" * 92)
    for mk in res:
        estado = "CERRADO" if mk["cerrado"] else "ABIERTO"
        print(f"\n■ {mk['titulo']}  [{estado}]  tipo: {mk['tipo']}  "
              f"volumen: ${mk['volumen']:,.0f}")
        if mk.get("inicio_iso"):
            print(f"   ventana: {mk['inicio_iso']} → {mk['fin_iso']} "
                  f"({mk['duracion_h']:.0f} h)")
        print(f"   {'bin':<10}{'precio YES':>12}{'cuota YES':>12}{'cuota NO':>12}"
              f"{'volumen':>12}")
        for b in mk["bins"]:
            cy = f"{b['cuota_yes']:.2f}" if b["cuota_yes"] else "—"
            cn = f"{b['cuota_no']:.2f}" if b["cuota_no"] else "—"
            marca = "  ← cuota ≥ 3" if (b["cuota_yes"] and b["cuota_yes"] >= 3) or \
                    (b["cuota_no"] and b["cuota_no"] >= 3) else ""
            print(f"   {b['titulo']:<10}{b['precio_yes']:>12.4f}{cy:>12}{cn:>12}"
                  f"{b['volumen']:>12,.0f}{marca}")


if __name__ == "__main__":
    main()
