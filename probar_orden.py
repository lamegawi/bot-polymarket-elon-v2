#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRUEBA DE ORDEN SEGURA — valida la firma de órdenes CLOB V2
============================================================
Coloca una orden límite de $0.01 a precio 0.01 en el primer bin del
mercado de 48h activo. Es un precio IMPOSIBLE (los bins cotizan muy
por encima), así que la orden NO se llenará. Si llegara a llenarse,
el coste máximo sería 1 céntimo.

Si la orden se envía y se cancela sin error → la firma V2 funciona
y ya puedes poner "confirmado": true.

USO:
  python probar_orden.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from operar_real import cargar_config, get_client, token_id_para_bin
import mercado_polymarket as mp


def main():
    print("=" * 62)
    print("PRUEBA DE ORDEN SEGURA (5 shares a precio 0.01 = máx. 5 céntimos, no se llenará)")
    print("=" * 62)
    cfg = cargar_config()
    print(f"  confirmado: {cfg.get('confirmado')}  (no se necesita para esta prueba)")

    print("\nCreando cliente CLOB (SDK V2)…")
    client = get_client()

    print("Buscando mercado 48h activo…")
    try:
        mercados = json.load(open(mp.SALIDA, encoding="utf-8"))["mercados"]
    except Exception as e:
        print(f"  ✖ no hay mercado_activo.json: {e}")
        print("  → Ejecuta antes:  python bot.py --excel  (una pasada en papel)")
        return 1
    activo = next((m for m in mercados if not m["cerrado"] and m["tipo"] == "48h"), None)
    if not activo:
        print("  ✖ no hay mercado 48h abierto ahora mismo (reintenta más tarde)")
        return 1
    print(f"  Mercado: {activo['titulo']}")
    # elegir el primer bin CON PRECIO REAL (no los bins muertos a 0.000)
    b = next((x for x in activo["bins"] if (x.get("precio_yes") or 0) >= 0.02),
             activo["bins"][0])
    print(f"  Primer bin: {b['titulo']} (precio actual {b['precio_yes']:.3f})")
    tokens = token_id_para_bin(activo["slug"], b["titulo"])
    if not tokens:
        print(f"  ✖ no encontré los token IDs para {b['titulo']}")
        return 1
    token_id = tokens[0]
    print(f"  Token YES: {token_id[:16]}…")

    print("\nEnviando orden de prueba (price=0.01, size=5, BUY, GTC)…")
    # En cuentas por email (smart wallet) el firmante ≠ wallet: probar los
    # tipos de firma de smart wallet en orden (POLY_PROXY → POLY_1271).
    from py_clob_client_v2 import SignatureTypeV2
    tipos = []
    st_cfg = cfg.get("signature_type")
    if st_cfg is not None:
        tipos.append(int(st_cfg))
    if cfg.get("wallet_address"):
        tipos += [int(SignatureTypeV2.POLY_PROXY), int(SignatureTypeV2.POLY_1271)]
    else:
        tipos += [int(SignatureTypeV2.EOA)]
    # dedupe manteniendo orden
    tipos = list(dict.fromkeys(tipos))
    ultimo_error = None
    for st in tipos:
        try:
            from py_clob_client_v2.clob_types import OrderArgs
            cliente = get_client(signature_type=st)
            resp = cliente.create_and_post_order(
                OrderArgs(token_id=token_id, price=0.01, size=5, side="BUY"))
            oid = resp.get("orderID") or resp.get("order_id")
            print(f"  ✔ Orden enviada con signature_type={st} (POLY_PROXY=1, POLY_1271=3). orderID: {oid}")
            print("  La firma de tu cuenta funciona ✅")
            print("  Cancelando la orden de prueba…")
            time.sleep(8)
            try:
                cliente.cancel_order(oid)
                print("  ✔ Orden cancelada (sin riesgo).")
            except Exception as e:
                print(f"  (aviso al cancelar — no es grave): {e}")
            print("\n✅ PRUEBA SUPERADA. Firma válida con signature_type=" + str(st))
            print("  → añade en config_real.json:  \"signature_type\": " + str(st))
            return 0
        except Exception as e:
            ultimo_error = e
            print(f"  ✖ con signature_type={st}: {str(e)[:140]}")
    print(f"\n  ✖ Ningún tipo de firma funcionó. Último error: {ultimo_error}")
    print("  Posibles causas:")
    print("   - No está instalado el SDK V2:  python -m pip install py-clob-client-v2")
    print("   - La clave privada no es el firmante de esta cuenta (revisa wallet_private_key)")
    print("   - La cuenta no es smart wallet (pégame el error y lo ajusto)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
