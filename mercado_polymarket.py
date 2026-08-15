#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MERCADOS POLYMARKET — «Elon Musk # tweets» (precios y cuotas en vivo)
=====================================================================
Descarga TODOS los mercados de Polymarket sobre el nº de tweets de
@elonmusk (48h, semanales y mensuales), parsea los bins (rangos) y
precios YES, calcula las cuotas (=1/precio), identifica la ventana
(48 h, semanal, mensual) y guarda todo en mercado_activo.json.

DETECCIÓN AUTOMÁTICA DE VENTANAS NUEVAS
----------------------------------------
Siempre que una ventana termina, Polymarket crea la siguiente. Este
módulo enumera día a día TODOS los slugs posibles de ventanas
recientes y futuras (48h día a día, semanales día a día, mensuales
mes a mes), así que cualquier ventana nueva se detecta en la misma
pasada en que se crea, se añade a mercado_activo.json y se avisa por
ntfy (🆕 Nueva ventana). Las ventanas ya vistas se guardan en
ventanas_vistas.json para no repetir avisos y para no re-sondear las
que ya se resolvieron.

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
VISTAS = "ventanas_vistas.json"
MESES = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
         "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
         "December": 12}
MESES_NOMBRE = {1: "january", 2: "february", 3: "march", 4: "april", 5: "may",
                6: "june", 7: "july", 8: "august", 9: "september", 10: "october",
                11: "november", 12: "december"}


def curl(url):
    r = subprocess.run(["curl", "-s", "--max-time", "40", url],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("curl falló o respuesta vacía")
    return json.loads(r.stdout)


def _cargar_vistas():
    try:
        d = json.load(open(VISTAS, encoding="utf-8"))
        return d.get("vistas", {})
    except Exception:
        return {}


def _guardar_vistas(vistas):
    try:
        with open(VISTAS, "w", encoding="utf-8") as f:
            json.dump({"actualizado": datetime.now(timezone.utc).isoformat(),
                       "vistas": vistas}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _detectar_nuevas(res):
    """Compara las ventanas del JSON con las ya vistas. Devuelve la lista de
    ventanas ABIERTAS nuevas (nunca vistas antes) y actualiza el registro.
    Las ventanas que ya no están abiertas se marcan como resueltas (no se
    volverán a sondear)."""
    vistas = _cargar_vistas()
    nuevas = []
    ahora = datetime.now(timezone.utc)
    for mk in res:
        slug = mk.get("slug") or ""
        if not slug:
            continue
        if mk["cerrado"] or (mk.get("fin_iso") and datetime.fromisoformat(mk["fin_iso"]) < ahora):
            vistas[slug] = {"cerrada": True, "primera_vista": vistas.get(slug, {}).get("primera_vista", datetime.now(timezone.utc).isoformat())}
            continue
        if slug not in vistas:
            vistas[slug] = {"cerrada": False, "primera_vista": datetime.now(timezone.utc).isoformat()}
            nuevas.append(mk)
    _guardar_vistas(vistas)
    return nuevas


def fetch_events():
    """Búsqueda pública + enumeración de slugs de TODAS las ventanas
    posibles (48h día a día, semanales día a día, mensuales mes a mes).
    Así, cuando una ventana termina y Polymarket crea la siguiente, esta
    se detecta en la misma pasada (sin importar el día en que la creen).

    Solo se sondean los slugs que la búsqueda pública no devuelve y que
    no están registrados como resueltos (eficiencia: ~0-3 requests/pasada
    en condiciones normales)."""
    d = curl(GAMMA)
    eventos = [e for e in d.get("events", []) if "tweets" in (e.get("title") or "").lower()]
    vistos = {e.get("id"): e for e in eventos if e.get("id")}
    vistos_slugs = {e.get("slug"): e for e in eventos if e.get("slug")}
    conocidas = _cargar_vistas()

    hoy = datetime.now(timezone.utc).date()
    slugs = set()
    for i in range(-2, 16):            # 48h: desde hace 2 días hasta +15 (día a día)
        d1 = hoy + timedelta(days=i)
        d2 = d1 + timedelta(days=2)
        slugs.add(f"elon-musk-of-tweets-{MESES_NOMBRE[d1.month]}-{d1.day}-{MESES_NOMBRE[d2.month]}-{d2.day}")
    for i in range(-7, 22):            # semanales: desde hace 7 días hasta +21 (día a día)
        d1 = hoy + timedelta(days=i)
        d2 = d1 + timedelta(days=7)
        slugs.add(f"elon-musk-of-tweets-{MESES_NOMBRE[d1.month]}-{d1.day}-{MESES_NOMBRE[d2.month]}-{d2.day}")
    for delta in (-2, -1, 0, 1, 2, 3):  # mensuales: 2 atrás, actual y 3 futuros
        d1 = hoy + timedelta(days=31 * delta)
        slugs.add(f"elon-musk-of-tweets-{MESES_NOMBRE[d1.month]}-{d1.year}")

    for s in sorted(slugs):
        if s in vistos_slugs:          # ya lo trae la búsqueda pública
            continue
        info = conocidas.get(s)
        if info and info.get("cerrada"):
            continue                   # resuelta: no volverá a abrirse
        try:
            data = curl(f"https://gamma-api.polymarket.com/events?slug={s}")
            if isinstance(data, list) and data and not data[0].get("closed"):
                vistos.setdefault(data[0].get("id"), data[0])
        except Exception:
            pass
    return list(vistos.values())


def parse_bin(titulo):
    """'<20' → (0,19) · '20-39' → (20,39) · '1000+' → (1000,∞)"""
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


def parse_ventana(desc, end_iso, titulo):
    """Ventana del mercado:
    - 48h/semanal: 'from August 15 12:00 PM ET to August 17, 2026 12:00 PM ET'.
    - MENSUSAL: 'during the month of August 2026' → día 1 00:00 ET al día 1
      del mes siguiente 00:00 ET (fin = endDate cuando existe).
    Devuelve (inicio_utc, fin_utc) o None."""
    # ---- mensual: "the month of August 2026" en la descripción/título
    m = re.search(r"month of ([A-Z][a-z]+) (\d{4})", desc or "")
    if not m:
        m = re.search(r"in ([A-Z][a-z]+) (\d{4})", titulo or "")
    if m:
        mes, anio = m.group(1), int(m.group(2))
        if mes in MESES:
            inicio = datetime(anio, MESES[mes], 1, 0, 0, tzinfo=ET)
            if end_iso:
                fin = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).astimezone(ET)
            else:
                nxt = anio + 1 if MESES[mes] == 12 else anio
                nm = 1 if MESES[mes] == 12 else MESES[mes] + 1
                fin = datetime(nxt, nm, 1, 0, 0, tzinfo=ET)
            if inicio < fin:
                return inicio, fin
            return None
    # ---- 48h / semanal: "from ... to ..."
    pat = re.compile(
        r"from ([A-Z][a-z]+) (\d{1,2})(?:,? (\d{4}))? (\d{1,2}):(\d{2}) ([AP]M) ET "
        r"to ([A-Z][a-z]+) (\d{1,2})(?:,? (\d{4}))? (\d{1,2}):(\d{2}) ([AP]M) ET")
    mm = pat.search(desc or "")
    if not mm:
        return None

    def to_dt(month, day, year, hh, mm2, ap):
        anio = int(year) if year else int(end_iso[:4])
        h = int(hh) % 12 + (12 if ap == "PM" else 0)
        return datetime(anio, MESES[month], int(day), h, int(mm2), tzinfo=ET)

    inicio = to_dt(mm.group(1), mm.group(2), mm.group(3), mm.group(4), mm.group(5), mm.group(6))
    fin = to_dt(mm.group(7), mm.group(8), mm.group(9), mm.group(10), mm.group(11), mm.group(12))
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
        ventana = parse_ventana(ev.get("description"), ev.get("endDate") or "",
                                ev.get("title") or "")
        dur_h = None
        tipo = "otro"
        if ventana:
            dur_h = (ventana[1] - ventana[0]).total_seconds() / 3600
            if abs(dur_h - 48) < 2:
                tipo = "48h"
            elif abs(dur_h - 168) < 8:
                tipo = "semanal"
            elif 660 < dur_h < 780:            # 28-32 días → mensual
                tipo = "mensual"
            else:
                tipo = "otro"
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
    """Descarga, procesa y guarda mercado_activo.json. Devuelve la lista.
    Si detecta ventanas NUEVAS (recién creadas), las añade al registro y
    avisa por ntfy (🆕 Nueva ventana)."""
    res = procesar(fetch_events())
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump({"actualizado": datetime.now(timezone.utc).isoformat(),
                   "mercados": res}, f, ensure_ascii=False, indent=1)
    nuevas = _detectar_nuevas(res)
    for mk in nuevas:
        try:
            import notificar
            notificar.ventana_nueva(mk)
        except Exception:
            pass
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
