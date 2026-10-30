"""
Roteamento de áudio virtual para o modo call (PipeWire/PulseAudio, via `pactl`).

              Discord (voz do amigo) ─► alicia_discord ─┬─► loopback ─► seu fone
                                                         └─► .monitor ─► Alicia ouve
              TTS da Alicia ─► alicia_voz ─┬─► loopback ─► seu fone
                                           └─► loopback ─► alicia_mix ◄─ loopback ◄─ seu microfone real
              alicia_mix.monitor  ==  fonte "alicia_mic"  ◄── é a ENTRADA que o Discord deve usar

Tudo é criado ao entrar no modo call e descarregado ao sair (nada fica no sistema).
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable, List, Optional, Sequence, Tuple

from .logging_setup import logger

SINK_DISCORD = "alicia_discord"
SINK_VOICE = "alicia_voz"
SINK_MIX = "alicia_mix"
SOURCE_MIC = "alicia_mic"
_OWN = (SINK_DISCORD, SINK_VOICE, SINK_MIX, SOURCE_MIC)

DEFAULT_DISCORD_APPS = ("discord", "vesktop", "webcord", "armcord", "legcord", "equibop")


class PactlError(RuntimeError):
    pass


def run_pactl(args: Sequence[str], timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PactlError(f"pactl {args[0]} falhou: {e}") from e
    if r.returncode != 0:
        raise PactlError(f"pactl {' '.join(args[:2])} falhou: {r.stderr.strip() or r.returncode}")
    return r.stdout


def parse_sink_inputs(text: str) -> List[Tuple[int, dict]]:
    """Saída de `pactl list sink-inputs` -> [(id, {propriedade: valor})]."""
    result: List[Tuple[int, dict]] = []
    current: Optional[Tuple[int, dict]] = None
    for line in text.splitlines():
        m = re.match(r"\s*Sink Input #(\d+)", line)
        if m:
            current = (int(m.group(1)), {})
            result.append(current)
            continue
        if current is not None:
            p = re.match(r'\s*([\w.\-]+)\s*=\s*"(.*)"\s*$', line)
            if p:
                current[1][p.group(1)] = p.group(2)
    return result


class VirtualAudio:
    def __init__(self, runner: Callable[[Sequence[str]], str] = run_pactl,
                 mic_source: Optional[str] = None, discord_apps: Sequence[str] = DEFAULT_DISCORD_APPS):
        self._run = runner
        self.mic_source = mic_source
        self.discord_apps = tuple(a.lower() for a in discord_apps)
        self._modules: List[int] = []

    @property
    def active(self) -> bool:
        return bool(self._modules)

    @property
    def capture_device(self) -> str:
        return f"{SINK_DISCORD}.monitor"

    # ------------------------------------------------------------------
    def _load(self, module: str, **params: str) -> int:
        out = self._run(["load-module", module, *[f"{k}={v}" for k, v in params.items()]])
        mid = int(out.strip())
        self._modules.append(mid)
        return mid

    def _purge_stale(self):
        """Módulos de uma execução anterior que não foi encerrada direito."""
        try:
            listing = self._run(["list", "short", "modules"])
        except PactlError:
            return
        for line in listing.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and any(f"={name}" in parts[2] for name in _OWN):
                try:
                    self._run(["unload-module", parts[0]])
                except PactlError:
                    pass

    def setup(self):
        if self.active:
            return
        self._purge_stale()
        try:
            sink = self._run(["get-default-sink"]).strip()
            mic = self.mic_source or self._run(["get-default-source"]).strip()
            if not sink or not mic:
                raise PactlError("não achei o destino/microfone padrão do sistema")

            def null_sink(name: str):
                self._load("module-null-sink", sink_name=name,
                           sink_properties=f"device.description={name}")

            def loop(source: str, dest: str):
                self._load("module-loopback", source=source, sink=dest, latency_msec="40")

            null_sink(SINK_DISCORD)
            loop(f"{SINK_DISCORD}.monitor", sink)          # você continua ouvindo o amigo
            null_sink(SINK_VOICE)
            loop(f"{SINK_VOICE}.monitor", sink)            # você ouve a Alicia
            null_sink(SINK_MIX)
            loop(f"{SINK_VOICE}.monitor", SINK_MIX)        # o amigo ouve a Alicia
            loop(mic, SINK_MIX)                            # ...e ouve você
            self._load("module-remap-source", master=f"{SINK_MIX}.monitor", source_name=SOURCE_MIC,
                       source_properties="device.description=Alicia_Mic")
            logger.info("🎧 Áudio virtual da call criado (alicia_discord, alicia_voz, alicia_mix).")
        except Exception:
            self.teardown()
            raise

    def teardown(self):
        modules, self._modules = self._modules, []
        for mid in reversed(modules):
            try:
                self._run(["unload-module", str(mid)])
            except PactlError as e:
                logger.debug(f"unload-module {mid}: {e}")

    # ------------------------------------------------------------------
    def discord_stream_ids(self) -> List[int]:
        try:
            streams = parse_sink_inputs(self._run(["list", "sink-inputs"]))
        except PactlError:
            return []
        ids = []
        for sid, props in streams:
            name = (props.get("application.name", "") + " " + props.get("application.process.binary", "")).lower()
            if any(app in name for app in self.discord_apps):
                ids.append(sid)
        return ids

    def move_discord_streams(self) -> int:
        """Leva a saída do Discord para `alicia_discord`. Chamar de tempos em tempos (cada pessoa que entra na call cria um stream novo)."""
        moved = 0
        for sid in self.discord_stream_ids():
            try:
                self._run(["move-sink-input", str(sid), SINK_DISCORD])
                moved += 1
            except PactlError:
                pass
        return moved
