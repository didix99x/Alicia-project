"""
Estruturas de dados simples (dataclasses) usadas entre módulos.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from .enums import AliciaExpression


@dataclass
class HardwareSnapshot:
    cpu_usage: float = 0.0
    ram_usage: float = 0.0
    vram_usage: Optional[float] = None
    active_window_title: str = "Desconhecido"
    active_process_name: str = "Desconhecido"
    is_gaming: bool = False
    is_coding: bool = False
    is_ai_workflow: bool = False


@dataclass
class VisionAnalysisResult:
    comment: Optional[str]
    expression: AliciaExpression
    should_speak: bool
    perceptual_hash: str
    difference_score: float
    raw_response: str
    timestamp: float = field(default_factory=time.time)
