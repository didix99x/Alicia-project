"""
Enum de expressões faciais/emocionais da Alicia.
"""

from enum import Enum

class AliciaExpression(Enum):
    FELIZ = "feliz"
    SERIO = "serio"
    SURPRESA = "surpresa"
    PENSANDO = "pensando"
    BRAVO = "bravo"

    @classmethod
    def from_str(cls, val: str) -> "AliciaExpression":
        if val and val.upper() in cls.__members__:
            return cls[val.upper()]
        return cls.FELIZ
