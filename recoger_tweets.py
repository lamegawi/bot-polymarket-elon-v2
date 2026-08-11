#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RECOGIDA AUTOMÁTICA DE TWEETS DE @elonmusk — para mercados Polymarket
=====================================================================
Recoge la actividad de @elonmusk (posts + quote posts + reposts;
SIN replies, igual que las reglas de resolución de los mercados
«Elon Musk # tweets») y genera datos_elon.csv con el formato que
espera senal.py (fecha,tweets) en días COMPLETOS (hora ET).

FUENTES (por orden de preferencia):
  1) x.com renderizado vía r.jina.ai (timestamps EXACTOS de posts Y
     reposts — la hora que se ve en el perfil es la del repost).
     Sin API key. ~15-20 tweets por pasada: en modo --loop se acumula.
  2) xcancel.com (espejo Nitter, página 1) — respaldo si jina falla.
     Ojo: los reposts muestran la fecha del tweet ORIGINAL.
  3) X API v2 oficial (exacta, con paginación completa):
     export X_BEARER_TOKEN=...   (plan Basic $100/mes para conteo
     completo ~60k posts/mes; free tier insuficiente para esto)

USO:
  python3 recoger_tweets.py --dias 30              # pasada única
  python3 recoger_tweets.py --resumen              # conteos diarios + métricas
  python3 recoger_tweets.py --loop --intervalo 20  # continuo (recomendado)
  python3 recoger_tweets.py --manual 2026-08-08 35 # corregir un día a mano
  python3 recoger_tweets.py --limpiar              # borrar datos y empezar de cero

CRON sugerido (1 vez al día basta para la señal del día):
  0 12 * * * cd /ruta/estrategia_elon_tweets && python3 recoger_tweets.py --dias 30 >> recoger.log 2>&1
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import mercado_polymarket as mp
except ImportError:
    mp = None

# ----------------------------------------------------------------------------
SCREEN = "elonmusk"
try:
    ET = ZoneInfo("America/New_York")
except Exception:
    # Windows sin tzdata: fallback a hora fija de verano (UTC-4)
    from datetime import timedelta
    from datetime import timezone as _tz
    ET = _tz(timedelta(hours=-4), name="EDT")
CSV = "datos_elon.csv"
ESTADO = "estado_tweets.json"
RAW_DIR = "datos_raw"
JINA_X = f"https://r.jina.ai/https://x.com/{SCREEN}"
XCANCEL = f"https://xcancel.com/{SCREEN}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
T_FMT = "%a %b %d %H:%M:%S +0000 %Y"
MESES = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
         "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
# ----------------------------------------------------------------------------


def hoy_et():
    return datetime.now(ET).date()


def ahora_utc():
    return datetime.now(timezone.utc)


def curl(url, headers=None, timeout=60):
    cmd = ["curl", "-s", "--max-time", str(timeout), "-L", url]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"curl falló ({r.returncode}): {r.stderr[:200]}")
    return r.stdout


# ---------------------------------------------------------------- fuentes

def descargar_jina_x():
    """Render de x.com/@elonmusk vía r.jina.ai. Devuelve {status_id: item}.
    La hora que muestra el perfil para CADA entrada (posts y reposts) es la
    del propio tweet/repost → timestamps EXACTOS (created_at), con flag
    'exacto': True para que el conteo diario los use siempre."""
    # Cloudflare es intermitente: reintentar con distintos UA y pausas
    uas = [None, UA, "curl/8.0"]
    ultimo_error = None
    for intento in range(4):
        try:
            hdrs = {"Accept": "text/plain"}
            if uas[intento % len(uas)]:
                hdrs["User-Agent"] = uas[intento % len(uas)]
            md = curl(JINA_X, hdrs)
            if ("Just a moment" in md or "Verifying your browser" in md
                    or "Rate limit" in md or "[@elonmusk]" not in md):
                ultimo_error = "desafío o respuesta sin contenido"
                time.sleep(8 + intento * 5)
                continue
            ahora = ahora_utc()
            vistos = {}
            # cada entrada del timeline: [@elonmusk](...)  [TIEMPO](https://x.com/elonmusk/status/ID)
            pat = re.compile(
                r"\[@elonmusk\]\(https://x\.com/elonmusk\)\s+\[([^\]]+)\]\(https://x\.com/elonmusk/status/(\d+)\)")
            posiciones = list(pat.finditer(md))
            for i, m in enumerate(posiciones):
                rel, sid = m.group(1).strip(), m.group(2)
                ts = rel_a_utc(rel, ahora)
                if not ts:
                    continue
                # trozo de la entrada hasta la siguiente entrada
                fin = posiciones[i + 1].start() if i + 1 < len(posiciones) else len(md)
                trozo = md[m.end():fin]
                # es repost si dentro hay un enlace de estado de OTRO usuario
                es_repost = bool(re.search(
                    r"\]\(https://x\.com/(?!elonmusk/status/)\w+/status/\d+\)", trozo))
                vistos[sid] = {"id": sid,
                               "kind": "repost" if es_repost else "post",
                               "autor": SCREEN,
                               "created_at": ts.strftime(T_FMT),
                               "primera_vista": ahora.strftime(T_FMT),
                               "exacto": True}
            return vistos
        except RuntimeError as e:
            ultimo_error = str(e)
            time.sleep(8 + intento * 5)
    raise RuntimeError(f"jina no disponible tras reintentos: {ultimo_error}")


def descargar_xcancel():
    """Página 1 de xcancel (respaldo). Reposts con fecha del original (aprox.)."""
    html = curl(XCANCEL, {"User-Agent": UA})
    ahora = ahora_utc()
    vistos = {}
    for b in re.split(r'<div class="timeline-item', html)[1:]:
        es_repost = 'retweet-header' in b
        m_id = re.search(r'/(?:status)/(\d+)', b)
        m_fecha = re.search(r'class="tweet-date"><a[^>]*title="([^"]+)"', b)
        if not m_id or not m_fecha:
            continue
        ts = parse_fecha_nitter(m_fecha.group(1))
        if not ts:
            continue
        vistos[m_id.group(1)] = {"id": m_id.group(1),
                                 "kind": "repost" if es_repost else "post",
                                 "autor": SCREEN,
                                 "created_at": ts.strftime(T_FMT),
                                 "primera_vista": ahora.strftime(T_FMT)}
    return vistos


def descargar_api_v2(max_paginas=50):
    """X API v2 oficial (exacta). Requiere X_BEARER_TOKEN."""
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        sys.exit("Modo api requiere: export X_BEARER_TOKEN=<tu token>")
    vistos, token_pag = {}, None
    for p in range(1, max_paginas + 1):
        url = ("https://api.x.com/2/users/44196397/tweets?exclude=replies"
               "&max_results=100&tweet.fields=created_at")
        if token_pag:
            url += f"&pagination_token={token_pag}"
        d = json.loads(curl(url, {"Authorization": f"Bearer {token}"}, timeout=30))
        if d.get("errors"):
            print("  error API:", d["errors"][0].get("detail", d["errors"]))
            break
        for t in d.get("data", []):
            ts = datetime.strptime(t["created_at"], "%Y-%m-%dT%H:%M:%S.000Z") \
                .replace(tzinfo=timezone.utc)
            vistos[t["id"]] = {"id": t["id"], "kind": "post", "autor": SCREEN,
                               "created_at": ts.strftime(T_FMT),
                               "primera_vista": ts.strftime(T_FMT)}
        token_pag = (d.get("meta") or {}).get("next_token")
        print(f"  [página {p}] tweets: {len(vistos)}")
        if not token_pag:
            break
        time.sleep(1.5)
    return vistos


def parse_fecha_nitter(titulo):
    m = re.match(r"([A-Z][a-z]{2} \d{1,2}, \d{4}) · (\d{1,2}:\d{2} [AP]M) UTC", titulo)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%b %d, %Y %I:%M %p") \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def rel_a_utc(rel, ahora):
    """Convierte '2h', '3m', '1d', 'Aug 6', 'Aug 6, 2025' a datetime UTC."""
    rel = rel.strip()
    m = re.match(r"^(\d+)([smhd])$", rel)
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {"s": 0, "m": 60, "h": 3600, "d": 86400}[u] * n
        return ahora - timedelta(seconds=delta)
    m = re.match(r"^([A-Z][a-z]{2}) (\d{1,2})(?:, (\d{4}))?$", rel)
    if m:
        mes, dia = MESES[m.group(1)], int(m.group(2))
        anio = int(m.group(3)) if m.group(3) else ahora.year
        try:
            ts = datetime(anio, mes, dia, tzinfo=timezone.utc)
        except ValueError:
            return None
        # si la fecha es claramente futura, es del año pasado
        if ts > ahora + timedelta(days=1):
            ts = ts.replace(year=anio - 1)
        return ts
    return None


# ---------------------------------------------------------------- procesado

def conteo_diario(vistos, sin_reposts=False, modo_loop=False):
    """Agrupa por día ET.
    - Items con timestamp exacto (jina/x.com): created_at siempre.
    - Reposts de xcancel (fecha del original): primera_vista en modo_loop
      (≈ momento real, error < intervalo); si no, fecha del original."""
    dias = {}
    for v in vistos.values():
        if v.get("kind") == "repost" and sin_reposts:
            continue
        if v.get("exacto") or v.get("kind") != "repost" or not modo_loop:
            base = v["created_at"]
        else:
            base = v["primera_vista"]
        fecha = datetime.strptime(base, T_FMT).astimezone(ET).date()
        dias[fecha] = dias.get(fecha, 0) + 1
    return dias


def actualizar_csv(dias_conteo, completos=True):
    hoy = hoy_et()
    filas = {}
    if os.path.exists(CSV):
        with open(CSV, newline="", encoding="utf-8") as f:
            for fila in csv.DictReader(f):
                filas[fila["fecha"]] = int(fila["tweets"])
    nuevos = 0
    for fecha, n in sorted(dias_conteo.items()):
        if completos and fecha >= hoy:
            continue
        # MONÓTONO: el conteo directo es una cota inferior (subconteo parcial);
        # solo se actualiza si el nuevo valor es MAYOR. Así los días
        # reconstruidos desde los mercados resueltos no se degradan.
        if n > filas.get(fecha.isoformat(), -1):
            filas[fecha.isoformat()] = n
            nuevos += 1
    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["fecha", "tweets"])
        for fecha in sorted(filas):
            w.writerow([fecha, filas[fecha]])
    return nuevos, len(filas)


def guardar_estado(vistos):
    os.makedirs(RAW_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(RAW_DIR, f"tweets_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(vistos, f, ensure_ascii=False, indent=1)
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump({"actualizado": datetime.now().isoformat(), "tweets": vistos}, f,
                  ensure_ascii=False, indent=1)


def cargar_estado():
    if not os.path.exists(ESTADO):
        return {}
    try:
        return json.load(open(ESTADO, encoding="utf-8")).get("tweets", {})
    except Exception:
        return {}


def fusionar(vistos_nuevos, sin_reposts=False):
    """Fusiona con el estado previo: por status id, gana el item nuevo
    (jina/x.com da el tiempo exacto del repost) pero se CONSERVA la
    primera_vista más antigua (el repost se atribuye al día en que se
    vio por PRIMERA vez). Si sin_reposts, elimina los reposts."""
    estado = cargar_estado()
    if sin_reposts:
        estado = {sid: v for sid, v in estado.items() if v.get("kind") != "repost"}
    for sid, v in vistos_nuevos.items():
        previo = estado.get(sid)
        if previo:
            v["primera_vista"] = min(v["primera_vista"], previo["primera_vista"])
        estado[sid] = v
    return estado


def resumen(dias=14):
    if not os.path.exists(CSV):
        print("Sin datos todavía. Ejecuta primero: recoger_tweets.py")
        return
    with open(CSV, newline="", encoding="utf-8") as f:
        filas = [(r["fecha"], int(r["tweets"])) for r in csv.DictReader(f)]
    hoy = hoy_et().isoformat()
    print(f"{'fecha':<12}{'tweets':>7}")
    for fecha, n in filas[-dias:]:
        marca = "  ← hoy (incompleto, no entra en la señal)" if fecha == hoy else ""
        print(f"{fecha:<12}{n:>7}{marca}")
    if len(filas) >= 9:
        ult7 = [n for _, n in filas[-7:]]
        ult2 = [n for _, n in filas[-2:]]
        avg7 = sum(ult7) / 7
        v2 = sum(ult2)
        r = v2 / (2 * avg7)
        aj = min(1.5, max(0.5, 1 + 0.5 * (r - 1)))
        lam = 2 * avg7 * aj
        print(f"\nAVG7 = {avg7:.2f}  V2 = {v2}  R = {r:.3f}  ajuste = {aj:.3f}  λ48 = {lam:.1f}")
    print("Días completos en CSV:", len(filas))


def main():
    ap = argparse.ArgumentParser(description="Recogida automática de tweets de @elonmusk")
    ap.add_argument("--fuente", choices=["jina", "xcancel", "api"], default="jina",
                    help="fuente de datos (api requiere X_BEARER_TOKEN)")
    ap.add_argument("--sin-reposts", action="store_true",
                    help="excluir reposts del conteo")
    ap.add_argument("--resumen", action="store_true", help="mostrar conteos y métricas")
    ap.add_argument("--loop", action="store_true", help="modo continuo (polling)")
    ap.add_argument("--intervalo", type=int, default=15, help="minutos entre pasadas")
    ap.add_argument("--mercado", action="store_true",
                    help="refrescar también los precios de Polymarket en cada pasada")
    ap.add_argument("--papel", action="store_true",
                    help="ejecutar también el paper trading (abrir/resolver apuestas simuladas)")
    ap.add_argument("--limpiar", action="store_true", help="borrar estado y CSV y empezar de cero")
    ap.add_argument("--manual", nargs=2, metavar=("FECHA", "N"),
                    help="añadir/corregir un día: --manual 2026-08-08 35")
    args = ap.parse_args()

    if args.limpiar:
        for f in (CSV, ESTADO):
            if os.path.exists(f):
                os.remove(f)
        print("Estado y CSV borrados. Empieza de cero.")
        return

    if args.manual:
        filas = {}
        if os.path.exists(CSV):
            with open(CSV, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    filas[r["fecha"]] = int(r["tweets"])
        filas[args.manual[0]] = int(args.manual[1])
        with open(CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["fecha", "tweets"])
            for f_ in sorted(filas):
                w.writerow([f_, filas[f_]])
        print(f"Manual: {args.manual[0]} = {args.manual[1]} tweets. CSV actualizado.")
        return

    if args.resumen:
        resumen()
        return

    def una_pasada():
        print(f"[{datetime.now(ET).isoformat()}] Recogiendo @{SCREEN} (fuente: {args.fuente})…")
        vistos_n = {}
        if args.fuente in ("jina", "xcancel"):
            # fusionar SIEMPRE ambas fuentes: jina (tiempos exactos) + xcancel
            for nombre, fn in (("jina", descargar_jina_x), ("xcancel", descargar_xcancel)):
                try:
                    parcial = fn()
                    if parcial:
                        vistos_n.update(parcial)
                        print(f"  {nombre}: {len(parcial)} items")
                    else:
                        print(f"  {nombre}: sin items")
                except RuntimeError as e:
                    print(f"  {nombre}: [ERROR] {e}")
            if not vistos_n:
                print("  Sin datos de ninguna fuente. Reintenta en un rato.")
                if args.papel:
                    try:
                        subprocess.run([sys.executable, "papel.py"], check=False)
                    except Exception as e:
                        print(f"  papel: [ERROR] {e}")
                return
        else:
            vistos_n = descargar_api_v2()
            if not vistos_n:
                print("  Sin datos. Reintenta en un rato.")
                return
        vistos = fusionar(vistos_n, sin_reposts=args.sin_reposts)
        guardar_estado(vistos)
        dias = conteo_diario(vistos, sin_reposts=args.sin_reposts, modo_loop=args.loop)
        nuevos, total = actualizar_csv(dias)
        n_rep = sum(1 for v in vistos.values() if v["kind"] == "repost")
        print(f"  Tweets únicos: {len(vistos)} (reposts: {n_rep}) · días con datos: {len(dias)} "
              f"· filas CSV: {total} (nuevas/actualizadas: {nuevos})")
        if args.mercado and mp:
            try:
                mk = mp.actualizar_mercado()
                abiertos = [m for m in mk if not m["cerrado"] and m["tipo"] == "48h"]
                print(f"  Polymarket: {len(mk)} mercados · {len(abiertos)} de 48 h abiertos "
                      f"→ mercado_activo.json actualizado")
            except Exception as e:
                print(f"  Polymarket: [ERROR] {e}")
        if args.papel:
            try:
                subprocess.run([sys.executable, "papel.py"], check=False)
            except Exception as e:
                print(f"  papel: [ERROR] {e}")
        resumen(dias=12)

    if args.loop:
        while True:
            try:
                una_pasada()
            except Exception as e:
                print(f"[ERROR general] {e}")
            print(f"[espera] {args.intervalo} min…")
            time.sleep(args.intervalo * 60)
    else:
        una_pasada()


if __name__ == "__main__":
    main()
