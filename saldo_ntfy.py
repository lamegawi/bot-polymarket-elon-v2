#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SALDO NTFY — saldo real de Polymarket para notificaciones
==========================================================
Lee el saldo on-chain (pUSD + USDC + USDC.e) de la deposit wallet
y lo devuelve formateado para incluirlo en los avisos al móvil.

USO (desde cualquier bot):
    from saldo_ntfy import saldo_real_texto
    texto = saldo_real_texto()   # "Saldo real: $36.55"
"""
import json
import os


def saldo_real_texto():
    """Devuelve 'Saldo real: $X.XX' o '' si no se puede calcular."""
    try:
        # Reutilizar la función on-chain de operar_real si existe
        import sys
        import importlib.util
        # buscar operar_real en el directorio actual
        for nombre in ("operar_real", "operar_real_semanal"):
            if importlib.util.find_spec(nombre):
                mod = importlib.import_module(nombre)
                if hasattr(mod, "saldo_usdc_onchain") and hasattr(mod, "cargar_config"):
                    cfg = mod.cargar_config()
                    wallet = (cfg.get("wallet_address") or "").strip()
                    if wallet:
                        saldos = mod.saldo_usdc_onchain(wallet, "polygon") or {}
                        total = (saldos.get("pUSD", 0) + saldos.get("USDC", 0)
                                 + saldos.get("USDC.e", 0))
                        if total > 0 or any(v > 0 for v in saldos.values()):
                            return f"Saldo real: ${total:,.2f}"
        # fallback: leer config_real.json directamente
        if os.path.exists("config_real.json"):
            cfg = json.load(open("config_real.json", encoding="utf-8"))
            wallet = (cfg.get("wallet_address") or "").strip()
            if wallet:
                import subprocess
                rpcs = ["https://polygon-rpc.com", "https://1rpc.io/matic"]
                tokens = [
                    ("pUSD", "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", 6),
                    ("USDC.e", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6),
                    ("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
                ]
                data = "0x70a08231" + "0" * 24 + wallet.lower()[2:]
                total = 0.0
                for simbolo, contrato, dec in tokens:
                    body = json.dumps({"jsonrpc": "2.0", "method": "eth_call",
                                       "params": [{"to": contrato, "data": data}, "latest"], "id": 1})
                    for rpc in rpcs:
                        try:
                            out = subprocess.run(
                                ["curl", "-s", "--max-time", "10", "-X", "POST", rpc,
                                 "-H", "Content-Type: application/json", "-d", body],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace").stdout
                            r = json.loads(out)
                            if "result" in r and r["result"] not in ("0x", "0x0"):
                                total += int(r["result"], 16) / (10 ** dec)
                                break
                        except Exception:
                            continue
                if total > 0:
                    return f"Saldo real: ${total:,.2f}"
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    print(saldo_real_texto() or "(no se pudo obtener el saldo)")
