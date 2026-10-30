#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALICIA — PONTO DE ENTRADA
Use Iniciar_Alicia.bat (prepara tudo na primeira vez) ou `python main.py`
com o ambiente virtual ativo.
"""

import os
import pathlib
import sys

if __name__ == "__main__":
    # Dados (config.json, alicia_memory.db, .env, logs/) ficam ao lado deste arquivo.
    app_dir = pathlib.Path(__file__).parent.resolve()
    os.chdir(app_dir)

    try:
        from alicia_core.winperf import harden_process
        harden_process()      # sem isto o Windows 'adormece' o microfone quando a janela perde o foco
    except Exception as e:
        print(f"[aviso] ajustes de desempenho do Windows não aplicados: {e}")

    try:
        from alicia_core.core import AliciaAssistantCore
        from alicia_core.database import DatabaseUnavailable
    except ImportError as e:
        print(f"\n[ERRO] Dependência ausente: {e}\nRode Iniciar_Alicia.bat para instalar tudo.")
        sys.exit(1)

    try:
        assistant = AliciaAssistantCore(config_path="config.json")
    except DatabaseUnavailable as e:
        print(f"\n[ERRO NA MEMÓRIA] {e}")
        sys.exit(2)

    assistant.start()
    if not assistant.is_running and not assistant.nvidia.has_key:
        print("\n[ERRO] Chave da API não configurada. Abra o arquivo .env e preencha NVIDIA_API_KEY.")
        sys.exit(3)
