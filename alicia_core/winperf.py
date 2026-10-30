"""
Ajustes de desempenho do Windows para a Alicia continuar ouvindo com a janela em segundo plano.

Problema: o Windows 10/11 coloca processos "em segundo plano" (janela sem foco, coberta ou minimizada) em
modo de economia (EcoQoS / "modo de eficiência"): a CPU atende as threads com menos frequência e os
temporizadores ficam grossos. A leitura do microfone atrasa, o buffer da placa de som transborda e a Alicia
só "acorda" quando você clica na janela (que volta ao primeiro plano).

O que fazemos (tudo reversível, só afeta este processo):
  1. Desliga a limitação de energia do processo e das threads de áudio (SetProcessInformation / SetThreadInformation).
  2. Pede resolução de temporizador de 1 ms (timeBeginPeriod) e impede que o Windows a ignore.
  3. Eleva um pouco a prioridade da thread que lê o microfone.
  4. Desativa o "QuickEdit" do console: clicar na janela preta NÃO pausa mais o programa.

Em outros sistemas as funções não fazem nada. Nunca levantam exceção.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Dict

from .logging_setup import logger

PROCESS_POWER_THROTTLING = 4          # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
THREAD_POWER_THROTTLING = 3           # THREAD_INFORMATION_CLASS.ThreadPowerThrottling
CURRENT_VERSION = 1
EXECUTION_SPEED = 0x1
IGNORE_TIMER_RESOLUTION = 0x4
THREAD_PRIORITY_ABOVE_NORMAL = 1
STD_INPUT_HANDLE = -10
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080


class PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_uint32), ("ControlMask", ctypes.c_uint32), ("StateMask", ctypes.c_uint32)]   # ULONG do Windows = 32 bits


def _is_windows() -> bool:
    return sys.platform == "win32"


def _kernel32():
    return ctypes.windll.kernel32


def _winmm():
    return ctypes.windll.winmm


def _no_throttle_state(include_timer: bool) -> PowerThrottlingState:
    control = EXECUTION_SPEED | (IGNORE_TIMER_RESOLUTION if include_timer else 0)
    # ControlMask diz "eu quero controlar estes bits"; StateMask=0 diz "desligado" (= sem economia).
    return PowerThrottlingState(CURRENT_VERSION, control, 0)


def disable_console_quickedit(k32=None) -> bool:
    """Clicar na janela do console (modo QuickEdit) PAUSA o programa até você apertar Enter. Desliga isso."""
    try:
        k32 = k32 or _kernel32()
        handle = k32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32(0)
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False          # não é um console clássico (ex.: Windows Terminal trata isso por conta própria)
        new_mode = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
        return bool(k32.SetConsoleMode(handle, new_mode))
    except Exception as e:
        logger.debug(f"QuickEdit não alterado: {e}")
        return False


def harden_process() -> Dict[str, bool]:
    """Chamar uma vez, cedo, em main.py. Devolve o que funcionou (para o log/diagnóstico)."""
    result = {"power_throttling_off": False, "timer_1ms": False, "quickedit_off": False}
    if not _is_windows():
        return result
    try:
        k32 = _kernel32()
        state = _no_throttle_state(include_timer=True)
        result["power_throttling_off"] = bool(k32.SetProcessInformation(
            k32.GetCurrentProcess(), PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state)))
    except Exception as e:
        logger.debug(f"SetProcessInformation indisponível: {e}")
    try:
        result["timer_1ms"] = _winmm().timeBeginPeriod(1) == 0
    except Exception as e:
        logger.debug(f"timeBeginPeriod indisponível: {e}")
    result["quickedit_off"] = disable_console_quickedit()
    logger.info(f"⚙️ Windows: economia de energia desligada={result['power_throttling_off']}, "
                f"temporizador 1ms={result['timer_1ms']}, QuickEdit desligado={result['quickedit_off']}")
    return result


def boost_current_thread() -> bool:
    """Para a thread de captura do microfone: sem economia de energia e prioridade um pouco maior."""
    if not _is_windows():
        return False
    ok = False
    try:
        k32 = _kernel32()
        thread = k32.GetCurrentThread()
        state = _no_throttle_state(include_timer=False)
        k32.SetThreadInformation(thread, THREAD_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state))
        ok = bool(k32.SetThreadPriority(thread, THREAD_PRIORITY_ABOVE_NORMAL))
    except Exception as e:
        logger.debug(f"Prioridade da thread não alterada: {e}")
    return ok
