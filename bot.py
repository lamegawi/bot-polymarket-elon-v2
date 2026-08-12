#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🤖 BOT DEFINITIVO — Polymarket · Elon Musk # tweets (paper trading)
====================================================================
Orquestador único que lo hace TODO en cada pasada:

  1) Recoge tweets de @elonmusk (jina/x.com con tiempos exactos +
     xcancel de respaldo), fusiona con el estado y actualiza
     datos_elon.csv (guard monótono: solo sube, nunca degrada).
  2) Actualiza los mercados de Polymarket (bins, precios, cuotas)
     → mercado_activo.json.
  3) Ejecuta el paper trading: resuelve apuestas de papel ya
     decididas por el mercado real y abre nuevas cuando la señal
     cumple las reglas (cuota ≥ 3.00, p_modelo ≥ 60% / ≤ 30%,
     una sola apuesta activa, progresión 3.30 × 1.5^n).
  4) Regenera automáticamente el Excel de resultados cuando hay
     cambios (Resultados_Papel.xlsx) y muestra el estado.

Todo queda registrado en bot.log y en los archivos de estado.

USO:
  python3 bot.py                       # una pasada completa
  python3 bot.py --loop                # modo continuo (RECOMENDADO)
  python3 bot.py --loop --intervalo 15 # cada 15 minutos
  python3 bot.py --loop --excel        # + Excel en cada cambio
  python3 bot.py --estado              # solo ver el estado actual
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    import recoger_tweets as rt
    import mercado_polymarket as mp
    import papel
    import senal
    import senal_vivo
    import notificar
except ImportError as e:
    print(f"ERROR de importación: {e}")
    print("Ejecuta el bot desde la carpeta estrategia_elon_tweets")
    sys.exit(1)

try:
    ET = ZoneInfo("America/New_York")
except Exception:
    # Windows sin tzdata: fallback a hora fija de verano (UTC-4)
    from datetime import timedelta
    from datetime import timezone as _tz
    ET = _tz(timedelta(hours=-4), name="EDT")
LOG = "bot_v2.log"
EXCEL_HIST = "Historial_Operaciones_V2.xlsx"
ESTADO_BOT = "estado_bot_v2.json"


def cargar_estado_bot():
    try:
        return json.load(open(ESTADO_BOT, encoding="utf-8"))
    except Exception:
        return {}


def guardar_estado_bot(d):
    try:
        with open(ESTADO_BOT, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def log(msg):
    linea = f"[{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}"
    print(linea, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------- 1) tweets
def recoger(opts):
    log("1/4 · Recogiendo tweets de @elonmusk…")
    vistos_n = {}
    for nombre, fn in (("jina", rt.descargar_jina_x), ("xcancel", rt.descargar_xcancel)):
        try:
            p = fn()
            if p:
                vistos_n.update(p)
                log(f"      · {nombre}: {len(p)} items")
            else:
                log(f"      · {nombre}: sin items")
        except Exception as e:
            log(f"      · {nombre}: ERROR {e}")
    if not vistos_n:
        log("      ⚠ sin datos de ninguna fuente (se conserva el estado anterior)")
        try:
            notificar.alerta_error(
                "⚠️ No se pudieron recoger tweets de @elonmusk (jina y xcancel "
                "fallaron). Se conserva el estado anterior. Revisa bot.log.")
        except Exception:
            pass
        return
    vistos = rt.fusionar(vistos_n, sin_reposts=opts.sin_reposts)
    rt.guardar_estado(vistos)
    dias = rt.conteo_diario(vistos, sin_reposts=opts.sin_reposts, modo_loop=opts.loop)
    nuevos, total = rt.actualizar_csv(dias)
    n_rep = sum(1 for v in vistos.values() if v["kind"] == "repost")
    log(f"      · tweets únicos: {len(vistos)} (reposts {n_rep}) · "
        f"CSV: {total} días (nuevos/mejorados: {nuevos})")


# ------------------------------------------------------------------- 2) mercado
def actualizar_mercado():
    log("2/4 · Actualizando mercados de Polymarket…")
    try:
        mks = mp.actualizar_mercado()
        abiertos = [m for m in mks if not m["cerrado"] and m["tipo"] == "48h"]
        log(f"      · {len(mks)} mercados · {len(abiertos)} de 48 h abiertos "
            f"→ mercado_activo.json")
        return mks
    except Exception as e:
        log(f"      · ERROR {e}")
        try:
            notificar.alerta_error(
                f"⚠️ No se pudo actualizar el mercado de Polymarket: {e}")
        except Exception:
            pass
        return None


# ------------------------------------------------------------------- 3) trading
def trading(opts):
    excel_al_cambio = opts.excel
    if opts.modo == "real":
        log("3/4 · Trading REAL" + (" (MODO SECO)" if opts.simular else "") + "…")
        try:
            import operar_real
            estado = operar_real.pasada_real(dry=opts.simular, actualizar=False,
                                             excel=False)
            if excel_al_cambio:
                try:
                    from excel_historial import generar as gen_hist
                    ruta, anadidas, total = gen_hist(operar_real.HISTORIAL,
                                                     salida=EXCEL_HIST,
                                                     bankroll=operar_real.BANKROLL,
                                                     titulo_extra="trading REAL")
                    log(f"      · Excel historial actualizado: {ruta} "
                        f"(añadidas {anadidas}, total {total})")
                except Exception as e:
                    log(f"      · no se pudo generar Excel: {e}")
            return estado
        except Exception as e:
            log(f"      · ERROR REAL: {e}\n{traceback.format_exc()}")
            return None
    log("3/4 · Paper trading…")
    try:
        prev = len(papel.cargar_estado().get("historial", []))
        estado = papel.pasada(actualizar=False, excel=False)
        nuevo = len(estado.get("historial", []))
        if excel_al_cambio and nuevo > prev:
            try:
                from excel_historial import generar as gen_hist
                ruta, anadidas, total = gen_hist(papel.HISTORIAL,
                                                 salida=EXCEL_HIST,
                                                 bankroll=papel.BANKROLL,
                                                 titulo_extra="paper trading en vivo")
                log(f"      · Excel historial actualizado: {ruta} "
                    f"(añadidas {anadidas}, total {total})")
            except ImportError:
                log("      · openpyxl no instalado: ejecuta  pip install openpyxl")
            except Exception as e:
                log(f"      · no se pudo generar Excel: {e}")
        return estado
    except Exception as e:
        log(f"      · ERROR {e}\n{traceback.format_exc()}")
        return None


# ------------------------------------------------------------------- 4) estado
def mostrar_estado():
    log("4/4 · Estado:")
    try:
        rt.resumen(12)
    except Exception as e:
        log(f"      · resumen: {e}")


def avisar_casi_senal():
    """Avisa al móvil si hay un mercado 48 h cerca de cumplir las reglas
    pero que no llega a apostarse (informativo, con cooldown)."""
    try:
        datos = senal.cargar_csv("datos_elon.csv")
        m = senal.metricas(datos)
        mercados = json.load(open(mp.SALIDA, encoding="utf-8"))["mercados"]
        evaluados, _ = senal_vivo.evaluar(m["avg7"], m["v2"], m["ajuste"],
                                          m["lam48"], mercados, 1)
        notificar.casi_senal(evaluados)
    except Exception as e:
        log(f"      · aviso casi-señal: {e}")


def avisar_resumen_diario():
    """Envía el resumen diario una vez al día, a la hora configurada
    (config.json → "resumen": {"hora": H}; -1 = desactivado)."""
    try:
        cfg = notificar.cargar_config()
        hora = int((cfg.get("resumen") or {}).get("hora", 20))
        if hora < 0:
            return
        ahora = datetime.now(ET)
        eb = cargar_estado_bot()
        hoy = ahora.date().isoformat()
        if eb.get("ultimo_resumen") == hoy:
            return
        # Ventana de envío: 20:00-23:59 (hora normal) o 00:00-06:00
        # (recuperación si el cron se retrasó y el de ayer no se envió).
        if not (ahora.hour >= hora or ahora.hour < 6):
            return
        # construir el resumen con el estado real
        est = papel.cargar_estado()
        metricas = None
        mercados_48h = None
        try:
            datos = senal.cargar_csv("datos_elon.csv")
            m = senal.metricas(datos)
            metricas = {"avg7": m["avg7"], "v2": m["v2"], "r": m["r"],
                        "lam48": m["lam48"]}
        except Exception:
            pass
        try:
            mercados = json.load(open(mp.SALIDA, encoding="utf-8"))["mercados"]
            mercados_48h = [x for x in mercados if not x["cerrado"] and x["tipo"] == "48h"]
        except Exception:
            pass
        notificar.resumen_diario(saldo=est["saldo"], paso=est["paso"],
                                 historial=est["historial"],
                                 apuesta_activa=est.get("activa"),
                                 mercados_48h=mercados_48h,
                                 metricas=metricas)
        eb["ultimo_resumen"] = hoy
        guardar_estado_bot(eb)
        log(f"      · Resumen diario enviado ({hoy}, hora {hora}:00)")
    except Exception as e:
        log(f"      · resumen diario: {e}")


def pasada(opts):
    t0 = time.time()
    log("=" * 62)
    log("PASADA COMPLETA" + (f"  ·  MODO: {opts.modo.upper()}" if opts.modo == "real" else ""))
    recoger(opts)
    actualizar_mercado()
    trading(opts)
    avisar_casi_senal()
    avisar_resumen_diario()
    mostrar_estado()
    log(f"Pasada completada en {time.time() - t0:.1f} s")


def ver_estado():
    print("=" * 62)
    print("ESTADO DEL BOT")
    print("=" * 62)
    try:
        rt.resumen(14)
    except Exception as e:
        print(f"  resumen: {e}")
    try:
        est = papel.cargar_estado()
        print(f"\nPaper trading: saldo ${est['saldo']:.2f} · paso {est['paso']} · "
              f"apuestas resueltas: {len(est['historial'])}")
        if est.get("activa"):
            a = est["activa"]
            print(f"  Activa: {a['bin_titulo']} {a['lado']} @ {a['precio']} "
                  f"(cuota {a['cuota']}) · paso {a['paso']} · "
                  f"stake ${a['stake']:.2f} · fin ventana {a.get('ventana_fin', '?')}")
        else:
            print("  Sin apuesta activa (esperando señal 48 h con cuota ≥ 3.00)")
        if est["historial"]:
            g = sum(1 for h in est["historial"] if h["resultado"] == "G")
            print(f"  Historial: {len(est['historial'])} apuestas · {g}G/{len(est['historial'])-g}P · "
                  f"beneficio ${sum(h['beneficio'] for h in est['historial']):+.2f}")
    except Exception as e:
        print(f"  papel: {e}")
    print("\nArchivos clave: datos_elon.csv · estado_tweets.json · mercado_activo.json · "
          "papel.json · resultados_papel.csv · bot.log")


def main():
    ap = argparse.ArgumentParser(description="Bot definitivo — Polymarket · Elon Musk # tweets")
    ap.add_argument("--loop", action="store_true", help="modo continuo (recomendado)")
    ap.add_argument("--intervalo", type=int, default=15, help="minutos entre pasadas (default 15)")
    ap.add_argument("--excel", action="store_true",
                    help="regenerar Excel de resultados cuando haya cambios")
    ap.add_argument("--sin-reposts", action="store_true", help="excluir reposts del conteo")
    ap.add_argument("--estado", action="store_true", help="solo mostrar estado actual y salir")
    ap.add_argument("--notificar-test", action="store_true",
                    help="enviar una notificación de prueba al móvil y salir")
    ap.add_argument("--modo", choices=["papel", "real"], default="papel",
                    help="papel = simulado (default) · real = dinero REAL (¡requiere config_real.json!)")
    ap.add_argument("--simular", action="store_true",
                    help="(con --modo real) simular las órdenes sin enviarlas")
    args = ap.parse_args()
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


    if args.notificar_test:
        res = notificar.enviar("🧪 Prueba del bot. Si lo ves en el móvil, todo funciona.",
                               titulo="Test notificaciones", etiqueta="robot")
        print("Canales:", res)
        print("Config:", notificar.estado_texto())
        return

    log(f"🤖 BOT V2 (48h) arrancado (loop={args.loop}, intervalo={args.intervalo} min, "
        f"excel={args.excel}) · notificaciones: {notificar.estado_texto()}")

    if args.estado:
        ver_estado()
        return

    if args.loop:
        while True:
            try:
                pasada(args)
            except Exception as e:
                log(f"ERROR GLOBAL: {e}\n{traceback.format_exc()}")
            log(f"Próxima pasada en {args.intervalo} min…")
            time.sleep(args.intervalo * 60)
    else:
        pasada(args)


if __name__ == "__main__":
    main()
