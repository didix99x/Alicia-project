"""
Leitura de hardware e da janela ativa do sistema (CPU, RAM, título de
janela, detecção de processos de jogo).
"""

import sys

from .deps import psutil, HAS_PSUTIL, HAS_WIN32
from .logging_setup import logger
from .models import HardwareSnapshot

if HAS_WIN32:
    from .deps import user32
    import ctypes


class HardwareMonitor:
    # Lista de processos reconhecidos como jogos para o Modo Jogo
    # automático. Se o seu jogo não entrar sozinho no modo, adicione
    # o nome do executável (minúsculo, com .exe) aqui.
    GAMING_PROCESSES = {
        "valorant.exe", "valorant-win64-shipping.exe",
        "leagueoflegends.exe", "league of legends.exe",
        "gta5.exe", "gta5_enhanced.exe",
        "csgo.exe", "cs2.exe",
        "fortniteclient-win64-shipping.exe",
        "eldenring.exe", "cyberpunk2077.exe",
        "minecraft.exe", "javaw.exe",
        "dota2.exe", "overwatch.exe",
        "r5apex.exe", "rocketleague.exe",
        "starcraft ii.exe", "wow.exe",
        "eafc24.exe", "eafc25.exe"
    }
    CODING_PROCESSES = {"code.exe", "pycharm64.exe", "godot.exe", "renpy.exe", "python.exe", "studio64.exe"}
    AI_WORKFLOW_PROCESSES = {"python.exe", "comfyui.exe", "stable-diffusion.exe", "lmstudio.exe"}

    @staticmethod
    def get_active_window_title() -> str:
        if sys.platform == "win32" and HAS_WIN32:
            try:
                hwnd = user32.GetForegroundWindow()
                length = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                return buf.value if buf.value else "Área de Trabalho"
            except Exception:
                return "Desconhecido"
        return "Sistema Genérico"

    @staticmethod
    def get_active_process_name() -> str:
        """Retorna o executável da janela em primeiro plano no Windows."""
        if sys.platform != "win32" or not HAS_WIN32:
            return "Desconhecido"

        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return "Desconhecido"

            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            if not pid.value or not HAS_PSUTIL:
                return "Desconhecido"

            try:
                name = psutil.Process(pid.value).name()
                return name if name else "Desconhecido"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return "Desconhecido"
        except Exception as e:
            logger.debug(f"Erro obtendo processo ativo: {e}")
            return "Desconhecido"
    @classmethod
    def capture_snapshot(cls) -> HardwareSnapshot:
        snapshot = HardwareSnapshot()

        if HAS_PSUTIL:
            try:
                snapshot.cpu_usage = psutil.cpu_percent(interval=None)
                snapshot.ram_usage = psutil.virtual_memory().percent

                for proc in psutil.process_iter(['name']):
                    try:
                        pname = (proc.info.get('name') or '').lower()
                        if not pname:
                            continue
                        if pname in cls.GAMING_PROCESSES:
                            snapshot.is_gaming = True
                        if pname in cls.CODING_PROCESSES:
                            snapshot.is_coding = True
                        if pname in cls.AI_WORKFLOW_PROCESSES:
                            snapshot.is_ai_workflow = True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception as e:
                logger.debug(f"Erro psutil: {e}")

        snapshot.active_window_title = cls.get_active_window_title()
        snapshot.active_process_name = cls.get_active_process_name()

        if snapshot.active_process_name.lower() in cls.GAMING_PROCESSES:
            snapshot.is_gaming = True

        return snapshot
