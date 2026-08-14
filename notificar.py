#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NOTIFICAR — avisos al móvil (ntfy.sh / Telegram)
=================================================
Envía notificaciones al móvil cuando el bot ABRE o CIERRA una apuesta
de papel (y su resultado). Canales soportados:

  1) ntfy.sh  ★ RECOMENDADO (gratis, sin registro, app iOS/Android)
     - El bot genera un tema único automáticamente (config.json).
     - En el móvil: instala la app "ntfy" y suscríbete a ese tema.
  2) Telegram (opcional, más privado)
     - Crea un bot con @BotFather y copia el token.
     - Consigue tu chat_id (p. ej. @userinfobot).
     - Pon ambos en config.json.

La configuración se guarda en config.json (se crea sola la primera vez).

USO:
  python3 notificar.py --test          # enviar mensaje de prueba al móvil
"""
import argparse
import json
import os
import random
import string
import subprocess
import time
from datetime import datetime
import saldo_ntfy
from zoneinfo import ZoneInfo

try:
    ET = ZoneInfo("America/New_York")
except Exception:
    # Windows sin tzdata: fallback a hora fija de verano (UTC-4)
    from datetime import timedelta
    from datetime import timezone as _tz
    ET = _tz(timedelta(hours=-4), name="EDT")

CONFIG = "config.json"


def cargar_config():
    """Carga config.json o lo crea con un tema ntfy aleatorio."""
    if os.path.exists(CONFIG):
        try:
            cfg = json.load(open(CONFIG, encoding="utf-8"))
            if cfg.get("ntfy", {}).get("topic"):
                cfg.setdefault("resumen", {"hora": 20})
                return cfg
        except Exception:
            pass
    tema = "elon-poly-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    cfg = {"ntfy": {"topic": tema, "token": None},
           "telegram": {"token": "", "chat_id": ""},
           "resumen": {"hora": 20}}
    guardar_config(cfg)
    return cfg


def guardar_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def enviar(mensaje, titulo=None, etiqueta=None, prioridad="normal"):
    """Envía a todos los canales configurados. Devuelve lista (canal, ok)."""
    cfg = cargar_config()
    resultados = []

    # ------------------------------ ntfy.sh
    nt = cfg.get("ntfy") or {}
    if nt.get("topic"):
        cmd = ["curl", "-s", "--max-time", "15", "-d", mensaje,
               f"https://ntfy.sh/{nt['topic']}"]
        if titulo:
            cmd += ["-H", f"Title: {titulo}"]
        if etiqueta:
            cmd += ["-H", f"Tags: {etiqueta}"]
        if prioridad:
            cmd += ["-H", f"Priority: {prioridad}"]
        if nt.get("token"):
            cmd += ["-H", f"Authorization: Bearer {nt['token']}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            resultados.append(("ntfy", r.returncode == 0))
        except Exception as e:
            resultados.append(("ntfy", False))

    # ------------------------------ telegram
    tg = cfg.get("telegram") or {}
    if tg.get("token") and tg.get("chat_id"):
        texto = f"{titulo}\n{mensaje}" if titulo else mensaje
        cmd = ["curl", "-s", "--max-time", "15", "-X", "POST",
               f"https://api.telegram.org/bot{tg['token']}/sendMessage",
               "--data-urlencode", f"chat_id={tg['chat_id']}",
               "--data-urlencode", f"text={texto}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            resultados.append(("telegram", '"ok":true' in r.stdout))
        except Exception as e:
            resultados.append(("telegram", False))

    return resultados


def apuesta_abierta(act, saldo):
    """Notificación cuando se abre una apuesta de papel."""
    mensaje = (
        f"📈 APUESTA ABIERTA (paper trading)\n"
        f"Mercado: {act['slug']}\n"
        f"Bin {act['bin_titulo']} · Lado {act['lado']}\n"
        f"Precio ${act['precio']:.3f} · Cuota {act['cuota']:.2f}\n"
        f"p_modelo: {act['p_modelo']:.0%} · Paso {act['paso']}\n"
        f"Stake: ${act['stake']:.2f} · Saldo bot: ${saldo:.2f}\n"
        f"{saldo_ntfy.saldo_real_texto()}"
    )
    return enviar(mensaje, titulo="🟢 Nueva apuesta abierta",
                  etiqueta="chart_with_upwards_trend")


def apuesta_cerrada(reg, saldo):
    """Notificación cuando se cierra una apuesta de papel con su resultado."""
    if reg["resultado"] == "G":
        cabecera = f"✅ GANADA  +${reg['beneficio']:.2f}"
        etiqueta = "white_check_mark"
    else:
        cabecera = f"❌ PERDIDA  ${reg['beneficio']:.2f}"
        etiqueta = "x"
    mensaje = (
        f"{cabecera}\n"
        f"Bin {reg['bin']} · Lado {reg['lado']}\n"
        f"Ganador real del mercado: {reg['real']}\n"
        f"Stake ${reg['stake']:.2f} · Paso {reg['paso']}\n"
        f"Saldo bot: ${saldo:.2f}\n"
        f"{saldo_ntfy.saldo_real_texto()}"
    )
    return enviar(mensaje, titulo="🔔 Apuesta cerrada", etiqueta=etiqueta)


def estado_texto():
    """Describe los canales configurados (para el log del bot)."""
    cfg = cargar_config()
    partes = []
    nt = cfg.get("ntfy") or {}
    if nt.get("topic"):
        partes.append(f"ntfy → {nt['topic']}")
    tg = cfg.get("telegram") or {}
    if tg.get("token") and tg.get("chat_id"):
        partes.append("telegram ✓")
    return " · ".join(partes) if partes else "ninguno configurado"


# =====================================================================
# AVISOS EXTRA: casi-señal y errores (con cooldown anti-spam)
# =====================================================================
COOLDOWN = "avisos_cooldown.json"


def _leer_cooldown():
    try:
        return json.load(open(COOLDOWN, encoding="utf-8"))
    except Exception:
        return {}


def _guardar_cooldown(d):
    try:
        with open(COOLDOWN, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _puede_avisar(clave, horas=6):
    """Anti-spam: cada clave (p. ej. un mercado) solo avisa 1 vez cada X horas."""
    d = _leer_cooldown()
    ahora = time.time()
    if ahora - d.get(clave, 0) < horas * 3600:
        return False
    d[clave] = ahora
    _guardar_cooldown(d)
    return True


def casi_senal(evaluados, horas=6):
    """Avisa cuando un mercado de 48 h tiene un bin CERCA de cumplir las
    reglas (p_modelo alto o cuota ≥ 3) pero NO se apuesta. Informativo.
    Máx. 1 aviso por mercado cada `horas` horas."""
    import senal
    for ev in evaluados:
        if ev.get("tipo") != "48h":
            continue
        mejor = None      # (distancia, lado, bin, p, cuota_lado)
        for b in ev["bins"]:
            p = b["p_modelo"]
            cy = b["cuota_yes"] or 0
            cn = b["cuota_no"] or 0
            # candidato a YES: cerca en p_modelo o ya tiene cuota
            if p >= 0.50 or cy >= 3.0:
                d = max(0.0, senal.P_MIN_YES - p) + max(0.0, senal.CUOTA_MINIMA - cy) * 0.3
                if mejor is None or d < mejor[0]:
                    mejor = (d, "YES", b, p, cy, cn)
            # candidato a NO
            if p <= 0.35 or cn >= 3.0:
                d = max(0.0, p - senal.P_MAX_NO) + max(0.0, senal.CUOTA_MINIMA - cn) * 0.3
                if mejor is None or d < mejor[0]:
                    mejor = (d, "NO", b, p, cy, cn)
        if mejor is None or mejor[0] > 0.15:
            continue
        d, lado, b, p, cy, cn = mejor
        clave = f"casi_{ev['titulo'][:40]}"
        if not _puede_avisar(clave, horas=horas):
            continue
        cuota_lado = cy if lado == "YES" else cn
        if cuota_lado < senal.CUOTA_MINIMA:
            falta = f"cuota {cuota_lado:.2f} < {senal.CUOTA_MINIMA:.2f} (falta precio más barato)"
        else:
            falta = f"p_modelo {p:.0%} fuera de zona (necesita ≥60% o ≤30%)"
        # enlace directo al mercado (para operar manualmente)
        slug = ev.get('slug') or ''
        enlace = f"https://polymarket.com/event/{slug}" if slug else ""
        mensaje = (
            f"🟡 CASI SEÑAL — NO se apuesta (informativo)\n"
            f"{ev['titulo']}\n"
            f"Bin {b['titulo']} · lado {lado} · p_modelo {p:.0%}\n"
            f"Cuota YES {cy:.2f} · NO {cn:.2f}\n"
            f"Falta: {falta}\n"
            f"🔗 {enlace}"
        )
        enviar(mensaje, titulo="👀 Casi señal (sin apuesta)",
               etiqueta="eyes", prioridad="default")


def alerta_error(texto, horas=1):
    """Aviso de error (recogida de datos, mercado…) con prioridad ALTA.
    Máx. 1 aviso cada `horas` horas para no spamear."""
    clave = "error_general"
    if not _puede_avisar(clave, horas=horas):
        return False
    enviar(texto, titulo="🚨 Alerta del bot", etiqueta="rotating_light",
           prioridad="high")
    return True


# =====================================================================
# RESUMEN DIARIO (una vez al día, hora configurable en config.json)
# =====================================================================
def resumen_diario(saldo, paso, historial, apuesta_activa=None,
                   mercados_48h=None, metricas=None):
    """Aviso diario con el estado completo: saldo, operaciones del día,
    apuesta activa, mercados abiertos y métricas de actividad."""
    hoy = datetime.now(ET).date()
    ops_hoy = [h for h in historial if h.get("fecha") == hoy.isoformat()]
    n = len(historial)
    g = sum(1 for h in historial if h["resultado"] == "G")
    n_hoy = len(ops_hoy)
    g_hoy = sum(1 for h in ops_hoy if h["resultado"] == "G")
    tot = sum(h["beneficio"] for h in historial)

    lineas = [f"📊 RESUMEN DIARIO — {hoy}",
              f"Saldo bot: ${saldo:.2f} · {saldo_ntfy.saldo_real_texto()}",
              f"Beneficio acumulado: ${tot:+.2f}",
              f"Operaciones: {n} totales ({g}G/{n-g}P) · Hoy: {n_hoy} ({g_hoy}G/{n_hoy-g_hoy}P)",
              f"Paso del ciclo: {paso}"]
    if apuesta_activa:
        a = apuesta_activa
        fin = (a.get("ventana_fin") or "?")[:16]
        lineas.append(f"🟢 Activa: {a.get('bin_titulo')} {a.get('lado')} a ${a.get('precio', 0):.3f} "
                      f"(cuota {a.get('cuota')}) · resuelve {fin}")
    else:
        lineas.append("⚪ Sin apuesta activa (vigilando señales)")
    if mercados_48h:
        nombres = ", ".join(
            (m["titulo"].replace("Elon Musk # tweets ", "").replace("?", ""))
            for m in mercados_48h[:2])
        lineas.append(f"🕐 Mercados 48 h abiertos: {len(mercados_48h)} ({nombres})")
    if metricas:
        lineas.append(f"📈 AVG7 {metricas.get('avg7', 0):.1f} · V2 {metricas.get('v2', 0)} · "
                      f"R {metricas.get('r', 0):.2f} · λ48 {metricas.get('lam48', 0):.1f}")
    return enviar("\n".join(lineas), titulo="📊 Resumen diario del bot",
                  etiqueta="bar_chart", prioridad="default")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Notificaciones al móvil")
    ap.add_argument("--test", action="store_true", help="enviar mensaje de prueba")
    ap.add_argument("--topic", help="fijar tema ntfy manualmente")
    ap.add_argument("--resumen-hora", type=int,
                    help="fijar la hora del resumen diario (0-23)")
    args = ap.parse_args()
    if args.topic:
        cfg = cargar_config()
        cfg["ntfy"]["topic"] = args.topic
        guardar_config(cfg)
        print(f"Tema ntfy fijado: {args.topic}")
    if args.resumen_hora is not None:
        cfg = cargar_config()
        cfg["resumen"]["hora"] = args.resumen_hora
        guardar_config(cfg)
        print(f"Hora del resumen diario fijada: {args.resumen_hora}:00")
    if args.test:
        res = enviar("🧪 Mensaje de prueba del bot de Polymarket. "
                     "Si ves esto en tu móvil, ¡todo funciona!",
                     titulo="Test bot Elon Musk", etiqueta="robot")
        print("Canales:", res)
        cfg = cargar_config()
        nt = cfg.get("ntfy", {})
        if nt.get("topic"):
            print(f"En ntfy, suscríbete al tema: {nt['topic']}")
