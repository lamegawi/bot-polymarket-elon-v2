#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OPERAR REAL — trading con dinero REAL en Polymarket (CLOB API)
==============================================================
Módulo para que el bot coloque APUESTAS REALES en los mercados
«Elon Musk # tweets» de Polymarket, en lugar de (o además de) el
paper trading. Incluye TODAS las salvaguardas:

  · NUNCA coloca una orden si config_real.json no tiene "confirmado": true
  · --simular / --dry: imprime la orden sin enviarla (modo seco)
  · Verifica saldo USDC antes de operar
  · Una sola apuesta real activa (secuencial)
  · Stake según la tabla 3.30 × 1.5^(paso-1) y límite de ciclo paso 7
  · Si la orden no se llena en X minutos → CANCELA (no persigue precio)
  · Registra todo en resultados_real.csv y notifica al móvil

REQUISITO: pip install py-clob-client

CONFIGURACIÓN: copia config_real.json.example → config_real.json y rellena.
  También puede leer variables de entorno: POLY_API_KEY, POLY_API_SECRET,
  POLY_API_PASSPHRASE, POLY_PRIVATE_KEY, REAL_CONFIRMADO=1

USO:
  python3 operar_real.py --simular       # modo seco (recomendado primero)
  python3 operar_real.py                 # una pasada real
  python3 bot.py --modo real --simular   # desde el bot, en seco
  python3 bot.py --modo real             # desde el bot, en serio

⚠️ Este módulo NO ha sido probado con dinero real. Úsalo primero en
modo seco y con cantidades mínimas. No es asesoramiento financiero.
"""
import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import senal
import senal_vivo
import mercado_polymarket as mp
import notificar
import saldo_ntfy

try:
    ET = ZoneInfo("America/New_York")
except Exception:
    # Windows sin tzdata: fallback a hora fija de verano (UTC-4)
    from datetime import timedelta
    from datetime import timezone as _tz
    ET = _tz(timedelta(hours=-4), name="EDT")
CFG = "config_real.json"
ESTADO = "real.json"
HISTORIAL = "resultados_real.csv"
BANKROLL = 500.0
HOST = "https://clob.polymarket.com"
FILL_TIMEOUT_MIN = 60          # cancelar si no se llena en 60 min
CHECK_INTERVAL_S = 60          # comprobar fill cada 60 s


# ------------------------------------------------------------------ config
def cargar_config():
    """Config desde archivo + variables de entorno (las env ganan)."""
    cfg = {}
    if os.path.exists(CFG):
        try:
            cfg = json.load(open(CFG, encoding="utf-8"))
        except Exception as e:
            print(f"  [ERROR] config_real.json inválido: {e}")
    env = {
        "api_key": os.environ.get("POLY_API_KEY", ""),
        "api_secret": os.environ.get("POLY_API_SECRET", ""),
        "api_passphrase": os.environ.get("POLY_API_PASSPHRASE", ""),
        "wallet_private_key": os.environ.get("POLY_PRIVATE_KEY", ""),
        "wallet_address": os.environ.get("POLY_WALLET_ADDRESS", ""),
        "relayer_api_key": os.environ.get("POLY_RELAYER_API_KEY", ""),
        "relayer_api_key_address": os.environ.get("POLY_RELAYER_API_KEY_ADDRESS", ""),
        "confirmado": os.environ.get("REAL_CONFIRMADO", "") == "1",
    }
    for k, v in env.items():
        if v:
            cfg[k] = v
    cfg.setdefault("bankroll", BANKROLL)
    cfg.setdefault("fee_pct", 0.0)
    cfg.setdefault("confirmado", False)
    return cfg


def get_client(signature_type=None):
    """Cliente CLOB (import diferido: solo se necesita para dinero real).
    IMPORTANTE (Polymarket V2, 28-abr-2026): se necesita el SDK V2:
    pip install py-clob-client-v2
    FIRMAS SMART WALLET: si tu cuenta se creó por email (deposit/proxy
    wallet), el firmante y el dueño de los fondos son distintos. Hay que
    usar signature_type=1 (POLY_PROXY) o 3 (POLY_1271). Por defecto se
    usa POLY_PROXY cuando hay wallet_address (funder); puedes forzarlo
    con el campo "signature_type" (0-3) en config_real.json."""
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
        from py_clob_client_v2 import SignatureTypeV2
    except ImportError as e:
        sys.exit(f"Falta el SDK V2 de Polymarket ({e}).\n"
                 f"Ejecuta:  python -m pip install py-clob-client-v2")
    cfg = cargar_config()
    signer = (cfg.get("wallet_private_key") or "").strip()
    if not signer:
        sys.exit("Sin clave privada del firmante: en config_real.json pon "
                 "wallet_private_key (la clave privada exportada en "
                 "https://reveal.magic.link/polymarket para cuentas por email).")
    kwargs = {}
    if cfg.get("api_key") and cfg.get("api_secret") and cfg.get("api_passphrase"):
        kwargs["creds"] = ApiCreds(cfg["api_key"], cfg["api_secret"],
                                   cfg["api_passphrase"])
    wallet = (cfg.get("wallet_address") or cfg.get("funder_address") or "").strip()
    if wallet:
        kwargs["funder"] = wallet
    if signature_type is None:
        signature_type = cfg.get("signature_type")
    if signature_type is None:
        # auto: smart wallet (funder distinto del signer) → POLY_PROXY; EOA puro → EOA
        signature_type = int(SignatureTypeV2.POLY_PROXY) if wallet else int(SignatureTypeV2.EOA)
    kwargs["signature_type"] = int(signature_type)
    client = ClobClient(HOST, chain_id=137, key=signer, **kwargs)
    if "creds" not in kwargs:
        try:
            creds = client.derive_api_key()
            client.set_api_creds(creds)
            print("  · Credenciales CLOB derivadas automáticamente de la clave privada.")
        except Exception as e:
            print(f"  [aviso] no se pudieron derivar credenciales API: {e}")
    return client


# ------------------------------------------------------------------ estado
def cargar_estado():
    if not os.path.exists(ESTADO):
        return {"saldo": BANKROLL, "paso": 1, "activa": None, "historial": []}
    try:
        d = json.load(open(ESTADO, encoding="utf-8"))
        for k in ("saldo", "paso", "activa", "historial"):
            d.setdefault(k, [] if k == "historial" else (None if k == "activa"
                        else (1 if k == "paso" else BANKROLL)))
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


# ------------------------------------------------------------------ mercado
def token_id_para_bin(slug, bin_titulo):
    """Devuelve (token_id_YES, token_id_NO) del bin pedido, o None."""
    r = subprocess.run(["curl", "-s", "--max-time", "40",
                        f"https://gamma-api.polymarket.com/events?slug={slug}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        evs = json.loads(r.stdout)
        ev = evs[0]
        for m in ev.get("markets", []):
            if (m.get("groupItemTitle") or "") == bin_titulo:
                tokens = json.loads(m.get("clobTokenIds") or "[]")
                if len(tokens) >= 2:
                    return tokens[0], tokens[1]
    except Exception:
        pass
    return None


def saldo_usdc_onchain(direccion, red="polygon"):
    """Consulta el saldo de tokens de una dirección directamente en la
    blockchain (sin API key, vía RPC público). Es la fuente de verdad.
    Devuelve dict {simbolo: float} o None si no se pudo consultar.
    IMPORTANTE (Polymarket V2, abril 2026): el colateral de trading es
    pUSD (0xC011a7E1...), no USDC. Los depósitos se convierten a pUSD."""
    rpcs = {
        "polygon": ["https://polygon-rpc.com", "https://1rpc.io/matic",
                    "https://rpc.ankr.com/polygon"],
        "ethereum": ["https://eth.llamarpc.com", "https://cloudflare-eth.com",
                     "https://rpc.ankr.com/eth"],
    }
    # tokens por red: (símbolo, contrato, decimales)
    tokens = {
        "polygon": [
            ("pUSD", "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", 6),   # colateral V2
            ("USDC.e", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6),  # puenteado
            ("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),    # nativo
            ("POL", "0x455e53CBB86018Ac2B8092FdCd39d8444aFFC3F6", 18),    # gas
        ],
        "ethereum": [
            ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
        ],
    }
    direccion = (direccion or "").lower().strip()
    if not direccion.startswith("0x") or len(direccion) != 42:
        return None
    data = "0x70a08231" + "0" * 24 + direccion[2:]
    resultado = {}
    for simbolo, contrato, decimales in tokens[red]:
        body = json.dumps({"jsonrpc": "2.0", "method": "eth_call",
                           "params": [{"to": contrato, "data": data}, "latest"],
                           "id": 1})
        for rpc in rpcs[red]:
            try:
                out = subprocess.run(
                    ["curl", "-s", "--max-time", "12", "-X", "POST", rpc,
                     "-H", "Content-Type: application/json", "-d", body],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace").stdout
                r = json.loads(out)
                if "result" in r and r["result"] not in ("0x", "0x0", None):
                    resultado[simbolo] = int(r["result"], 16) / (10 ** decimales)
                    break
            except Exception:
                continue
    return resultado if resultado else None


def verificar_saldo_usdc(client):
    """Devuelve (saldo, allowance) o (None, None) si no se puede consultar.
    Compatible con py-clob-client 0.34.x: get_balance_allowance espera un
    objeto BalanceAllowanceParams (dataclass), no un dict.
    IMPORTANTE: la API CLOB consulta el saldo del FIRMANTE (signer), que en
    cuentas con deposit wallet NO es donde está el dinero. La fuente de
    verdad es el saldo on-chain del wallet_address (ver saldo_usdc_onchain)."""
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        r = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return float(r.get("balance", 0)), float(r.get("allowance", 0))
    except Exception as e:
        print(f"  [aviso] no se pudo consultar saldo USDC vía CLOB: {e}")
        return None, None


# ------------------------------------------------------------------ resolver
def evento_resuelto(slug):
    r = subprocess.run(["curl", "-s", "--max-time", "40",
                        f"https://gamma-api.polymarket.com/events?slug={slug}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        evs = json.loads(r.stdout)
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


def resolver(estado, fee_pct=0.0):
    """Cierra la apuesta real activa si su mercado ya se resolvió.
    (Polymarket canjea automáticamente las shares ganadoras → el USDC
    vuelve solo al wallet; aquí solo anotamos el resultado.)"""
    act = estado.get("activa")
    if not act:
        return False
    resuelto, winner_titulo = evento_resuelto(act["slug"])
    if not resuelto or not winner_titulo:
        return False
    win = mp.parse_bin(winner_titulo)
    if not win:
        return False
    w_lo, w_hi = win
    w_hi = None if w_hi == float("inf") else w_hi
    if act["lado"] == "YES":
        ok = (w_lo == act["lo"] and w_hi == act["hi"])
    else:
        ok = not (w_lo == act["lo"] and w_hi == act["hi"])
    fee = round(act["stake"] * fee_pct, 2)
    if ok:
        benef = round(act["stake"] * (act["cuota"] - 1) - fee, 2)
        estado["saldo"] += benef
        res = "G"
        estado["paso"] = 1
    else:
        benef = -round(act["stake"] + fee, 2)
        estado["saldo"] += benef
        res = "P"
        estado["paso"] = 1 if act["paso"] >= 7 else act["paso"] + 1
    registro = {"id": uuid.uuid4().hex[:12], "fecha": act["fecha"],
                "mercado": act.get("slug", "—"), "bin": act["bin_titulo"],
                "lado": act["lado"], "precio": act["precio"], "cuota": act["cuota"],
                "p_modelo": act["p_modelo"], "paso": act["paso"],
                "stake": round(act["stake"], 2), "real": winner_titulo,
                "resultado": res, "beneficio": benef,
                "saldo": round(estado["saldo"], 2)}
    estado["historial"].append(registro)
    estado["activa"] = None
    escribir_historial(estado["historial"])
    guardar_estado(estado)
    print(f"  ✔ RESUELTA apuesta REAL del {act['fecha']}: {act['bin_titulo']} "
          f"{act['lado']} → ganador {winner_titulo} → {res} ${benef:+.2f} "
          f"(saldo ${estado['saldo']:.2f})")
    try:
        notificar.enviar(
            f"{'✅ GANADA' if res == 'G' else '❌ PERDIDA'}  ${benef:+.2f} (REAL)\n"
            f"Bin {act['bin_titulo']} · {act['lado']} · ganador real {winner_titulo}\n"
            f"Stake ${act['stake']:.2f} · saldo bot ${estado['saldo']:.2f}\n"
            f"{saldo_ntfy.saldo_real_texto()}",
            titulo="💰 Apuesta REAL cerrada",
            etiqueta="white_check_mark" if res == "G" else "x")
    except Exception:
        pass
    return True


# ------------------------------------------------------------------ abrir
def abrir(estado, dry=False, actualizar=False):
    """Evalúa la señal y coloca una orden REAL (o la simula si dry=True)."""
    cfg = cargar_config()
    if not cfg.get("confirmado") and not dry:
        print("  [BLOQUEADO] config_real.json no tiene 'confirmado': true. "
              "Usa --simular para probar.")
        return False
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
    _, candidatas = senal_vivo.evaluar(m["avg7"], m["v2"], m["ajuste"],
                                       m["lam48"], mercados, estado["paso"])
    candidatas = [c for c in candidatas if c["tipo"] == "48h"]
    if not candidatas:
        print(f"  (sin señal 48 h → no se abre apuesta real · paso {estado['paso']})")
        return False
    c = candidatas[0]

    # ---- salvaguardas
    if estado["paso"] > 7:
        print("  [BLOQUEADO] paso > 7: stop de ciclo. Reinicia el ciclo.")
        return False
    fee = cfg.get("fee_pct", 0.0)
    coste_total = c["stake"] * (1 + fee)
    if coste_total > cfg.get("bankroll", BANKROLL) * 0.5:
        print(f"  [BLOQUEADO] el stake ${c['stake']:.2f} supera el 50% del "
              f"bankroll ${cfg.get('bankroll', BANKROLL):.0f}.")
        return False

    # ---- orden
    client = get_client() if not dry else None
    tokens = None
    if not dry:
        tokens = token_id_para_bin(c["slug"], c["bin_titulo"])
        if not tokens:
            print(f"  [ERROR] no encontré el token del bin {c['bin_titulo']} "
                  f"en {c['slug']}")
            return False
        # Saldo real: consultar la deposit wallet (wallet_address) ON-CHAIN,
        # porque el saldo CLOB es del firmante y en deposit wallets los
        # fondos están en la wallet de la cuenta (pUSD).
        wallet = (cfg.get("wallet_address") or "").strip()
        saldo_real = 0.0
        if wallet:
            saldos = saldo_usdc_onchain(wallet, "polygon") or {}
            saldo_real = (saldos.get("pUSD", 0) + saldos.get("USDC", 0)
                          + saldos.get("USDC.e", 0))
            print(f"  Saldo on-chain ({wallet[:10]}…): "
                  f"pUSD ${saldos.get('pUSD', 0):.2f} · "
                  f"USDC ${saldos.get('USDC', 0):.2f} · "
                  f"USDC.e ${saldos.get('USDC.e', 0):.2f}")
        else:
            saldo, _ = verificar_saldo_usdc(client)
            saldo_real = saldo or 0.0
        if saldo_real < coste_total:
            print(f"  [BLOQUEADO] saldo insuficiente en la cuenta "
                  f"(${saldo_real:.2f} < ${coste_total:.2f}).")
            print("  Deposita pUSD/USDC en Polymarket (dirección wallet_address).")
            return False

    token_id = (tokens[0] if c["lado"] == "YES" else tokens[1]) if tokens else "TOKEN_DEMO"
    print(f"  → Orden {'SIMULADA' if dry else 'REAL'}: "
          f"{c['mercado']} · {c['bin_titulo']} {c['lado']} "
          f"@{c['precio']:.3f} (cuota {c['cuota']:.2f}) · "
          f"size {c['stake']:.2f} USDC · token {token_id[:12]}…")
    order_id = None
    if dry:
        print("  (modo seco: no se envió nada)")
    else:
        try:
            from py_clob_client_v2.clob_types import OrderArgs
            # SDK V2: el 'size' de OrderArgs son SHARES, no dólares.
            # stake $X a precio P → shares = X/P (redondeo a 2 dec).
            size_shares = round(c["stake"] / c["precio"], 2)
            if size_shares < 5:
                print(f"  [BLOQUEADO] tamaño {size_shares} shares < mínimo 5 "
                      f"(stake ${c['stake']:.2f} a precio {c['precio']:.3f}).")
                return False
            print(f"  (stake ${c['stake']:.2f} → {size_shares} shares a "
                  f"{c['precio']:.3f})")
            resp = client.create_and_post_order(
                OrderArgs(price=c["precio"], size=size_shares,
                          side="BUY", token_id=token_id))
            order_id = resp.get("orderID") or resp.get("order_id")
            print(f"  Orden enviada: {order_id}  (respuesta: {str(resp)[:120]})")
        except Exception as e:
            print(f"  [ERROR] no se pudo enviar la orden: {e}")
            return False
        # esperar fill (con timeout y cancelación de seguridad)
        if order_id:
            llenada = False
            t0 = time.time()
            while time.time() - t0 < FILL_TIMEOUT_MIN * 60:
                try:
                    detalle = client.get_order(order_id)
                    estado_ord = detalle.get("status")
                    size_matched = float(detalle.get("size_matched", 0) or 0)
                    if estado_ord == "matched" or size_matched >= c["stake"] * 0.99:
                        llenada = True
                        break
                    if estado_ord == "cancelled":
                        break
                except Exception:
                    pass
                time.sleep(CHECK_INTERVAL_S)
            if not llenada:
                try:
                    client.cancel(order_id)
                    print("  Orden cancelada (no se llenó en el tiempo límite).")
                except Exception as e:
                    print(f"  [aviso] no se pudo cancelar: {e}")
                try:
                    notificar.enviar(
                        f"Orden REAL cancelada por timeout: {c['bin_titulo']} "
                        f"{c['lado']} @ {c['precio']}",
                        titulo="⚠️ Orden cancelada (no llenada)",
                        etiqueta="warning")
                except Exception:
                    pass
                return False

    estado["activa"] = {"slug": c["slug"], "fecha": datetime.now(ET).strftime("%Y-%m-%d"),
                        "bin_titulo": c["bin_titulo"], "lo": c["lo"],
                        "hi": (c["hi"] if c["hi"] != math.inf else None),
                        "lado": c["lado"], "precio": round(c["precio"], 4),
                        "cuota": round(c["cuota"], 2), "p_modelo": round(c["p_modelo"], 4),
                        "paso": estado["paso"], "stake": c["stake"],
                        "order_id": order_id, "token_id": token_id,
                        "ventana_fin": c["ventana"][1].isoformat()}
    guardar_estado(estado)
    print(f"  ✔ ABIERTA apuesta REAL (o simulada): {c['bin_titulo']} {c['lado']} "
          f"a {c['precio']:.3f} · paso {estado['paso']} · stake ${c['stake']:.2f}")
    try:
        notificar.enviar(
            f"💰 ORDEN {'SIMULADA' if dry else 'REAL'} enviada\n"
            f"Mercado: {c['slug']}\nBin {c['bin_titulo']} · {c['lado']} "
            f"@ {c['precio']:.3f} (cuota {c['cuota']:.2f})\n"
            f"Paso {estado['paso']} · stake ${c['stake']:.2f}\n"
            f"{saldo_ntfy.saldo_real_texto()}",
            titulo="💰 Apuesta REAL abierta",
            etiqueta="moneybag")
    except Exception:
        pass
    return True


# ------------------------------------------------------------------ pasada
def pasada_real(dry=False, actualizar=False, excel=False):
    """Una pasada completa de trading real."""
    cfg = cargar_config()
    estado = cargar_estado()
    print(f"[{datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')}] Trading REAL "
          f"{'(SECO)' if dry else ''} · saldo ${estado['saldo']:.2f} · "
          f"paso {estado['paso']}")
    if estado.get("activa"):
        resolver(estado, fee_pct=cfg.get("fee_pct", 0.0))
    if not estado.get("activa"):
        abrir(estado, dry=dry, actualizar=actualizar)
    n = len(estado["historial"])
    if n:
        g = sum(1 for h in estado["historial"] if h["resultado"] == "G")
        print(f"  Historial REAL: {n} apuestas · {g}G/{n-g}P · "
              f"beneficio ${sum(h['beneficio'] for h in estado['historial']):+.2f}")
    if excel and os.path.exists(HISTORIAL):
        try:
            from excel_historial import generar as gen_hist
            ruta, anadidas, total = gen_hist(HISTORIAL,
                                             salida="Historial_Operaciones.xlsx",
                                             bankroll=cfg.get("bankroll", BANKROLL),
                                             titulo_extra="trading REAL")
            print(f"Excel historial: {ruta} (añadidas {anadidas}, total {total})")
        except Exception as e:
            print(f"  (no se pudo generar Excel: {e})")
    return estado


def probar_orden():
    """PRUEBA SEGURA del pipeline de órdenes reales (firma V2):
    coloca una orden límite de 5 shares a precio 0.01 (máx. 5 céntimos) en el primer bin del
    mercado 48h activo — es un precio imposible (los bins cotizan ≥ 0.10),
    por lo que NO se llenará nunca. Si se llenara, costaría 1 céntimo.
    Sirve para validar que la firma de órdenes V2 funciona antes de
    confiar dinero real. La orden se cancela a los 2 minutos."""
    print("Prueba de orden SEGURA (precio 0.01, tamaño $0.01 — no se llenará).")
    cfg = cargar_config()
    client = get_client()
    try:
        mercados = json.load(open(mp.SALIDA, encoding="utf-8"))["mercados"]
    except Exception:
        print("  (sin mercado_activo.json: ejecuta primero el bot en papel)")
        return
    ahora = datetime.now(timezone.utc)
    activo = next((m for m in mercados
                   if not m["cerrado"] and m["tipo"] == "48h"
                   and m.get("fin_iso")
                   and datetime.fromisoformat(m["fin_iso"]) > ahora), None)

    if not activo:
        print("  (no hay mercado 48h abierto ahora mismo)")
        return
    # elegir el primer bin CON PRECIO REAL (no los bins muertos a 0.000)
    b = next((x for x in activo["bins"] if (x.get("precio_yes") or 0) >= 0.02),
             activo["bins"][0])
    tokens = token_id_para_bin(activo["slug"], b["titulo"])
    if not tokens:
        print(f"  (no encontré tokens para {b['titulo']})")
        return
    token_id = tokens[0]  # YES
    try:
        from py_clob_client_v2.clob_types import OrderArgs
        resp = client.create_and_post_order(
            OrderArgs(token_id=token_id, price=0.01, size=5, side="BUY"))
        oid = resp.get("orderID") or resp.get("order_id")
        print(f"  ✔ Orden de prueba enviada: {oid}")
        print("  Si no hay error, la firma V2 funciona → puedes activar confirmado:true.")
        time.sleep(8)
        try:
            client.cancel_order(oid)
            print("  ✔ Orden de prueba cancelada (sin riesgo).")
        except Exception as e:
            print(f"  (aviso al cancelar: {e})")
    except Exception as e:
        print(f"  ✖ ERROR al enviar la orden de prueba: {e}")
        print("  → Si es un error de firma/dominio, el SDK V2 no está bien instalado")
        print("    o las credenciales no corresponden. Pega el error aquí.")


def main():
    ap = argparse.ArgumentParser(description="Trading real en Polymarket (con salvaguardas)")
    ap.add_argument("--simular", action="store_true", help="modo seco (no envía órdenes)")
    ap.add_argument("--actualizar", action="store_true", help="refrescar precios")
    ap.add_argument("--excel", action="store_true", help="actualizar Excel historial")
    ap.add_argument("--probar-orden", action="store_true",
                    help="prueba SEGURA de orden real (0.01$ a precio 0.01, no se llena)")
    args = ap.parse_args()
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if args.probar_orden:
        probar_orden()
        return
    pasada_real(dry=args.simular, actualizar=args.actualizar, excel=args.excel)


if __name__ == "__main__":
    main()
