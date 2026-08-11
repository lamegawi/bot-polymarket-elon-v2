#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CHEQUEAR CUENTA — verifica credenciales y saldo de Polymarket
==============================================================
Se conecta a la CLOB API con tu config_real.json y comprueba:
  · que la configuración es un JSON válido y tiene los campos clave
  · que el cliente CLOB se crea con tu clave privada del firmante
  · la dirección derivada del firmante
  · tu saldo USDC y allowance en Polymarket

NO coloca ninguna orden y NO mueve dinero. 100% seguro.

USO:
  python chequear_cuenta.py
"""
import os
import sys
import json

# permitir importar desde esta misma carpeta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from operar_real import cargar_config, get_client, saldo_usdc_onchain


def main():
    cfg = cargar_config()
    print("=" * 60)
    print("CHEQUEO DE CUENTA POLYMARKET (sin órdenes, sin riesgo)")
    print("=" * 60)

    # --- mostrar qué campos hay (sin revelar secretos completos) ---
    campos = ["wallet_private_key", "wallet_address", "relayer_api_key",
              "relayer_api_key_address", "api_key", "api_secret",
              "api_passphrase", "confirmado", "bankroll"]
    for k in campos:
        v = cfg.get(k)
        if v in (None, ""):
            print(f"  · {k:<22}: (vacío)")
        elif k in ("wallet_private_key", "api_secret", "relayer_api_key"):
            print(f"  · {k:<22}: OK ({str(v)[:10]}…{str(v)[-4:]})")
        else:
            print(f"  · {k:<22}: {v}")

    if not cfg.get("wallet_private_key"):
        print("\n❌ Falta wallet_private_key. Revisa config_real.json (debe ser un JSON válido).")
        return 1
    if not cfg.get("wallet_address"):
        print("\n⚠️ Falta wallet_address: pon la dirección 0x... de tu perfil de Polymarket.")

    # --- crear cliente (requiere py-clob-client) ---
    print("\nCreando cliente CLOB…")
    try:
        client = get_client()
        print("✅ Cliente CLOB creado correctamente (credenciales aceptadas).")
    except SystemExit as e:
        print(f"❌ {e}")
        print("   → Asegúrate de:  python -m pip install py-clob-client")
        return 1
    except Exception as e:
        print(f"❌ Error creando el cliente: {e}")
        return 1

    # --- dirección derivada del firmante ---
    try:
        addr = client.get_address()
        print(f"Dirección del firmante (derivada de tu clave privada): {addr}")
        wa = cfg.get("wallet_address", "")
        if wa and addr and addr.lower() == wa.lower():
            print("   ✅ Coincide con wallet_address: firma correcta para esta cuenta.")
        else:
            print(f"   ℹ️  Es distinta de wallet_address ({wa}).")
            print("      Normal si tu cuenta usa deposit wallet (el firmante ≠ la cuenta).")
    except Exception as e:
        print(f"  (no se pudo obtener dirección: {e})")

    # --- saldo y allowance (vía CLOB) ---
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        r = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        saldo = float(r.get("balance", 0))
        allowance = float(r.get("allowance", 0))
        print(f"\nSaldo USDC vía CLOB (firmante):  ${saldo:.2f}")
        print(f"Allowance USDC (aprobado):        ${allowance:.2f}")
    except Exception as e:
        print(f"\n⚠️ No se pudo consultar el saldo vía CLOB: {e}")
        saldo = None

    # --- saldo on-chain (fuente de verdad): dónde está el dinero REALMENTE ---
    print("\n--- Comprobación on-chain (fuente de verdad) ---")
    print("   (Polymarket V2: el colateral de trading es pUSD; los depósitos")
    print("    de tarjeta/Bitunix se convierten automáticamente a pUSD)")
    wallet = (cfg.get("wallet_address") or "").strip()
    addr_signer = None
    try:
        addr_signer = client.get_address()
    except Exception:
        pass
    direcciones = []
    if wallet:
        direcciones.append(("Deposit wallet (wallet_address)", wallet))
    if addr_signer:
        direcciones.append(("Firmante (signer)", addr_signer))
    saldo_total = 0.0
    for etiqueta, addr in direcciones:
        sp = saldo_usdc_onchain(addr, "polygon")
        se = saldo_usdc_onchain(addr, "ethereum")
        print(f"  {etiqueta}: {addr}")
        if sp:
            for simbolo in ("pUSD", "USDC", "USDC.e", "POL"):
                v = sp.get(simbolo, 0)
                if v:
                    print(f"      {simbolo:<7} en POLYGON: ${v:,.2f}")
            saldo_total += sp.get("pUSD", 0) + sp.get("USDC", 0) + sp.get("USDC.e", 0)
        else:
            print("      (no se pudo consultar Polygon)")
        if se:
            for simbolo, v in se.items():
                if v:
                    print(f"      {simbolo:<7} en ETHEREUM: ${v:,.2f}")
                    print("      ⚠️  ¡Fondos en ETHEREUM! Si retiraste con red ERC-20,")
                    print("          están aquí; haz puente a Polygon (bridge.polygon.technology).")
        print()

    if saldo_total >= 10:
        print(f"✅ Saldo de trading (pUSD+USDC) en Polygon: ${saldo_total:,.2f} — suficiente.")
    elif saldo_total > 0:
        print(f"⚠️ Saldo bajo (${saldo_total:,.2f}): deposita al menos $10 (ideal $500).")
    else:
        print("❌ NO se encuentra pUSD/USDC en Polygon en ninguna de tus direcciones.")
        print("   Posibles causas:")
        print("   1. El depósito (tarjeta o Bitunix) no se completó — mira el estado en el")
        print("      exchange o en el historial de depósitos de Polymarket.")
        print("   2. La transferencia desde Bitunix se hizo por otra red (no Polygon).")
        print("      Comprueba en Bitunix la red del retiro (debe ser POLYGON).")
        print("   3. El dinero sigue dentro de Bitunix (comprado, pero no retirado).")
        print("   4. La dirección de depósito usada no es 0xb0E1... (tu wallet_address).")
        print("   📌 En la WEB de Polymarket (Portafolio) verás tu saldo real en pUSD.")

    print("\n✔ Chequeo completado. Si ves 'Cliente CLOB creado correctamente' y tu saldo,")
    print("  ya puedes pasar a REAL: pon \"confirmado\": true en config_real.json y ejecuta:")
    print("  python bot.py --modo real --excel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
