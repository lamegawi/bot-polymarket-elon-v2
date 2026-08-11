#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DIAGNÓSTICO — por qué no se importa py-clob-client
===================================================
Muestra qué Python se está usando, dónde está instalado el paquete
y el error EXACTO del import (traceback completo). Sin riesgo.
"""
import os
import sys
import traceback

print("=" * 62)
print("DIAGNÓSTICO DE PYTHON / PY-CLOB-CLIENT")
print("=" * 62)

print(f"\n1) Intérprete de Python en uso:")
print(f"   ejecutable : {sys.executable}")
print(f"   versión    : {sys.version.split()[0]}")
print(f"   prefix     : {sys.prefix}")
print(f"   sys.path   :")
for p in sys.path:
    print(f"      - {p}")

print(f"\n2) ¿Dónde instala pip? (mismo python que arriba)")
print(f"   site-packages (user) : {os.path.expanduser('~')}\\AppData\\Roaming\\Python")
print(f"   site-packages (local): {sys.prefix}\\Lib\\site-packages")

# buscar py_clob_client en el disco del site-packages del python en uso
candidatos = []
for base in (sys.prefix, os.path.expanduser("~") + "\\AppData\\Roaming\\Python",
             os.path.expanduser("~") + "\\AppData\\Local\\Programs\\Python"):
    sp = os.path.join(base, "Lib", "site-packages")
    pkg = os.path.join(sp, "py_clob_client")
    if os.path.isdir(pkg):
        candidatos.append(pkg)
print(f"\n3) Carpetas 'py_clob_client' encontradas en site-packages:")
if candidatos:
    for c in candidatos:
        print(f"   ✔ {c}")
else:
    print("   ✖ NINGUNA. El paquete NO está instalado para este Python.")

print(f"\n4) Intento de import (con error real):")
try:
    import py_clob_client
    print(f"   ✔ import py_clob_client OK → {py_clob_client.__file__}")
except Exception:
    print("   ✖ FALLA al importar. Traceback completo:")
    traceback.print_exc()

try:
    from py_clob_client.client import ClobClient
    print("   ✔ from py_clob_client.client import ClobClient OK")
except Exception:
    print("   ✖ FALLA importar ClobClient. Traceback:")
    traceback.print_exc()

print("\n5) Conclusión:")
if candidatos and not (os.path.isdir(os.path.join(candidatos[0], "..", "..", "..", "Lib", "site-packages"))):
    pass
# comprobar si el pip que usó el usuario es de OTRO python
import subprocess
try:
    out = subprocess.run([sys.executable, "-m", "pip", "show", "py-clob-client"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode == 0:
        for linea in out.stdout.splitlines():
            if linea.lower().startswith(("name", "version", "location")):
                print(f"   {linea}")
    else:
        print("   ✖ este Python no ve el paquete con 'python -m pip show'")
except Exception as e:
    print(f"   (no se pudo ejecutar pip show: {e})")

print("\nSi el paso 3 dice NINGUNA pero pip lo instaló, hay DOS Pythons en juego:")
print("   → El pip que usaste instaló en un Python distinto al que ejecuta los scripts.")
print("   Solución: instala SIEMPRE con 'python -m pip install ...' (mismo python).")
