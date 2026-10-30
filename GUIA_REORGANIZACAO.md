> **Nota:** este guia descreve a estrutura anterior (Ollama no chat). Para o fluxo atual, leia README.md.

# Guia de reorganização — `assistente.py` → pacote Python

O arquivo hoje tem **5.417 linhas**, 14 classes e 2 funções soltas, tudo em um
único arquivo. Este guia mapeia o que existe e propõe uma estrutura de
pastas/arquivos para você organizar o projeto, sem mudar o comportamento dele.

---

## 1. Inventário do arquivo atual

### Imports e dependências opcionais (linhas 1–96)

O topo do arquivo já separa bem duas coisas:
- **Imports obrigatórios** (`requests`, `Pillow`, `pygame`, `edge_tts`) — se
  faltarem, o programa encerra com `sys.exit(...)`.
- **Imports opcionais**, cada um guardado por uma flag booleana:
  `HAS_MSS`, `HAS_SPEECH`, `HAS_PSUTIL`, `HAS_WIN32`.

| Flag | Biblioteca | Onde é usada hoje |
|---|---|---|
| `HAS_MSS` | `mss` | `ScreenAnalyzerEngine` (captura de tela) |
| `HAS_SPEECH` | `speech_recognition` | `VoiceInputEngine` |
| `HAS_PSUTIL` | `psutil` | `HardwareMonitor` e `AliciaAssistantCore` (detecção de jogo) |
| `HAS_WIN32` | `ctypes`/`user32` | `HardwareMonitor` (título da janela ativa) |

### Classes e funções (ordem de aparição no arquivo)

| # | Nome | Tipo | Linhas | Depende de |
|---|---|---|---|---|
| 1 | `ColorFormatter` | classe | 23 | — |
| 2 | `setup_logger()` | função | — | `ColorFormatter` |
| 3 | `logger` | variável global | — | `setup_logger()` |
| 4 | `AliciaExpression` | classe (Enum) | 12 | — |
| 5 | `HardwareSnapshot` | classe (dataclass) | 9 | — |
| 6 | `VisionAnalysisResult` | classe (dataclass) | 8 | `AliciaExpression` |
| 7 | `SystemConfig` | classe | 80 | — |
| 8 | `MemoryDatabase` | classe | 67 | — |
| 9 | `HardwareMonitor` | classe | 91 | `HardwareSnapshot` |
| 10 | `ScreenAnalyzerEngine` | classe | 61 | — |
| 11 | `OllamaClient` | classe | 68 | — |
| 12 | `TextToSpeechEngine` | classe | 88 | `AliciaExpression` |
| 13 | `PromptEngine` | classe | 480 | `HardwareSnapshot` |
| 14 | `VoiceInputEngine` | classe | 326 | `AliciaAssistantCore` (só como *type hint*) |
| 15 | `AliciaDesktopPetUI` | classe (QWidget) | 179 | `AliciaAssistantCore`, `AliciaExpression` |
| 16 | `AliciaAssistantCore` | classe | **3.716** | todas as anteriores |

`AliciaAssistantCore` sozinha é **69% do arquivo** (53 métodos). Ela orquestra
tudo: conversa, memória, modo gamer, TTS, microfone, tela. Por isso a proposta
abaixo tem dois níveis — separar as 16 classes primeiro, e opcionalmente
quebrar essa classe gigante depois.

### Um detalhe que apareci ao mapear

O método `_empty_gamer_profile` está **definido duas vezes, idêntico**
(linhas 3583 e 3599 — a segunda simplesmente sobrescreve a primeira, não
quebra nada, mas é lixo). Vale apagar uma cópia quando for mexer nesse
arquivo.

---

## 2. Por que dá pra separar sem medo de import circular

Rodei uma checagem de "quem usa quem" entre as classes:

```
VoiceInputEngine     -> usa AliciaAssistantCore   (só como type hint)
AliciaDesktopPetUI   -> usa AliciaAssistantCore   (só como type hint)
AliciaAssistantCore  -> usa TODAS as outras 10 classes (de verdade, em runtime)
```

O arquivo já tem `from __future__ import annotations` no topo (linha 15).
Isso faz com que **qualquer anotação de tipo vire só texto**, nunca é avaliada
de verdade. É por isso que `VoiceInputEngine.__init__(self, core_engine:
'AliciaAssistantCore')` funciona hoje sem importar `AliciaAssistantCore` lá
em cima do arquivo — e é por isso que dividir em arquivos não vai gerar
import circular: `voice_input.py` e `ui/desktop_pet.py` **não precisam
importar** o módulo de `AliciaAssistantCore`, só usam o nome dela dentro de
aspas/anotação.

Só `core.py` (o arquivo da `AliciaAssistantCore`) importa de verdade todo o
resto — e ele é o topo da pirâmide, então não fecha ciclo.

---

## 3. Estrutura de pastas proposta

```
alicia/
├── main.py                      # ponto de entrada (o if __name__ == "__main__")
├── config.json                  # (dado, já existe)
├── alicia_memory.db             # (dado, já existe)
├── gamer_profiles.json          # (dado, já existe)
│
└── alicia_core/                 # o pacote Python de verdade
    ├── __init__.py
    ├── deps.py                  # imports opcionais centralizados (HAS_*)
    ├── logging_setup.py         # ColorFormatter, setup_logger, logger
    ├── enums.py                 # AliciaExpression
    ├── models.py                # HardwareSnapshot, VisionAnalysisResult
    ├── config.py                # SystemConfig
    ├── database.py              # MemoryDatabase
    ├── hardware.py              # HardwareMonitor
    ├── screen.py                # ScreenAnalyzerEngine
    ├── ollama_client.py         # OllamaClient
    ├── tts.py                   # TextToSpeechEngine
    ├── prompts.py                # PromptEngine
    ├── voice_input.py            # VoiceInputEngine
    ├── ui/
    │   ├── __init__.py
    │   └── desktop_pet.py         # AliciaDesktopPetUI
    └── core.py                    # AliciaAssistantCore (ou pasta core/, ver seção 5)
```

### Mapa classe → arquivo

| Arquivo | Conteúdo |
|---|---|
| `alicia_core/deps.py` | Todo o bloco de `try/except ImportError` do topo (linhas 47–95): `requests`, `Image/ImageChops/ImageGrab/ImageStat`, `mss`+`HAS_MSS`, `pygame`, `speech_recognition`+`HAS_SPEECH`, `psutil`+`HAS_PSUTIL`, `ctypes`/`user32`+`HAS_WIN32`, `edge_tts` |
| `alicia_core/logging_setup.py` | `ColorFormatter`, `setup_logger()`, `logger` |
| `alicia_core/enums.py` | `AliciaExpression` |
| `alicia_core/models.py` | `HardwareSnapshot`, `VisionAnalysisResult` |
| `alicia_core/config.py` | `SystemConfig` |
| `alicia_core/database.py` | `MemoryDatabase` |
| `alicia_core/hardware.py` | `HardwareMonitor` |
| `alicia_core/screen.py` | `ScreenAnalyzerEngine` |
| `alicia_core/ollama_client.py` | `OllamaClient` |
| `alicia_core/tts.py` | `TextToSpeechEngine` |
| `alicia_core/prompts.py` | `PromptEngine` |
| `alicia_core/voice_input.py` | `VoiceInputEngine` |
| `alicia_core/ui/desktop_pet.py` | `AliciaDesktopPetUI` |
| `alicia_core/core.py` | `AliciaAssistantCore` |
| `main.py` | só o bloco `if __name__ == "__main__":` do final |

---

## 4. Passo a passo da migração

Faça isso com o programa **fechado** e, de preferência, com o projeto em um
git local (ou pelo menos um zip de backup) — assim qualquer erro de import é
só reverter.

### Passo 1 — Criar a estrutura de pastas vazia

```bash
mkdir -p alicia/alicia_core/ui
touch alicia/alicia_core/__init__.py
touch alicia/alicia_core/ui/__init__.py
```

### Passo 2 — Mover os dados (não é código)

Copie `config.json`, `alicia_memory.db` e `gamer_profiles.json` (se já
existirem) para dentro de `alicia/` — ao lado do `main.py`, não dentro do
pacote `alicia_core/`.

### Passo 3 — Criar `deps.py` primeiro

Esse é o único arquivo que **não é uma classe** do original — ele reúne o
bloco de `try/except` do topo. Todo outro módulo que precisar de uma dessas
libs importa daqui, por exemplo:

```python
# alicia_core/deps.py
import sys

try:
    import requests
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'requests' é necessária. Instale com: pip install requests")

try:
    from PIL import Image, ImageChops, ImageGrab, ImageStat
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'Pillow' é necessária. Instale com: pip install pillow")

try:
    import mss
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

try:
    import pygame
    pygame.mixer.init()
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'pygame' é necessária para o áudio. Instale com: pip install pygame")

try:
    import speech_recognition as sr
    HAS_SPEECH = True
except ImportError:
    HAS_SPEECH = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import ctypes
    if sys.platform == "win32":
        user32 = ctypes.windll.user32
        HAS_WIN32 = True
    else:
        HAS_WIN32 = False
except Exception:
    HAS_WIN32 = False

try:
    import edge_tts
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'edge-tts' é necessária. Instale com: pip install edge-tts")
```

Quem precisar, por exemplo, do `psutil` faz:

```python
from alicia_core.deps import psutil, HAS_PSUTIL
```

### Passo 4 — Copiar cada classe para seu arquivo (na ordem da tabela)

Para cada linha da tabela da seção 3:

1. Abra `assistente.py` na faixa de linhas daquela classe (a tabela da
   seção 1 já te dá o intervalo).
2. Copie o bloco **inteiro** da classe (do `class X:` até a linha antes do
   próximo `class`/comentário de seção).
3. Cole no arquivo novo correspondente.
4. No topo do arquivo novo, adicione só os imports que aquele bloco
   realmente usa (veja a tabela de dependências abaixo).

Isso é literalmente recortar e colar — nenhuma linha *dentro* das classes
precisa mudar, porque nada nelas usa import relativo implícito.

#### Imports que cada arquivo novo vai precisar

| Arquivo | Imports no topo |
|---|---|
| `logging_setup.py` | `import logging` |
| `enums.py` | `from enum import Enum` |
| `models.py` | `from dataclasses import dataclass, field`<br>`from typing import Optional`<br>`from .enums import AliciaExpression` |
| `config.py` | `import pathlib, json, logging`<br>`from typing import Optional`<br>`from .logging_setup import logger` |
| `database.py` | `import sqlite3, json, time`<br>`from typing import Any, Dict, List, Optional`<br>`from .logging_setup import logger` |
| `hardware.py` | `import sys`<br>`from .deps import psutil, HAS_PSUTIL, HAS_WIN32, user32 (se HAS_WIN32)`<br>`from .models import HardwareSnapshot`<br>`from .logging_setup import logger` |
| `screen.py` | `import io, hashlib`<br>`from .deps import Image, ImageChops, ImageGrab, ImageStat, mss, HAS_MSS`<br>`from .logging_setup import logger` |
| `ollama_client.py` | `import json`<br>`from .deps import requests`<br>`from typing import Any, Dict, List, Optional`<br>`from .logging_setup import logger` |
| `tts.py` | `import asyncio, tempfile, threading, re`<br>`from .deps import pygame, edge_tts`<br>`from .enums import AliciaExpression`<br>`from .logging_setup import logger` |
| `prompts.py` | `from typing import Any, Dict, List`<br>`from .models import HardwareSnapshot` |
| `voice_input.py` | `import time, threading`<br>`from __future__ import annotations` (para o type hint)<br>`from .deps import sr, HAS_SPEECH`<br>`from .logging_setup import logger` |
| `ui/desktop_pet.py` | `from __future__ import annotations`<br>`from PyQt6.QtCore import Qt, QPoint, QTimer`<br>`from PyQt6.QtGui import QPixmap, QAction, QImage, QPainter`<br>`from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QWidget`<br>`from ..enums import AliciaExpression` |
| `core.py` | importa **todos os módulos acima** (é o único arquivo assim) |

> `from __future__ import annotations` precisa estar em **qualquer** arquivo
> que use `'AliciaAssistantCore'` como texto de anotação de tipo
> (`voice_input.py` e `ui/desktop_pet.py`).

### Passo 5 — `core.py`

No topo dele:

```python
from __future__ import annotations

import json, os, random, re, sys, threading, time
from typing import Any, Dict, List, Optional, Tuple

from .config import SystemConfig
from .database import MemoryDatabase
from .hardware import HardwareMonitor
from .screen import ScreenAnalyzerEngine
from .ollama_client import OllamaClient
from .tts import TextToSpeechEngine
from .prompts import PromptEngine
from .voice_input import VoiceInputEngine
from .enums import AliciaExpression
from .models import HardwareSnapshot
from .logging_setup import logger
from .ui.desktop_pet import AliciaDesktopPetUI
```

Depois cole a classe `AliciaAssistantCore` inteira (linhas 1690–5405) sem
alterar nada dentro dela.

### Passo 6 — `main.py`

```python
import os
import pathlib

from alicia_core.core import AliciaAssistantCore

if __name__ == "__main__":
    app_dir = pathlib.Path(__file__).parent.resolve()
    os.chdir(app_dir)

    assistant = AliciaAssistantCore(config_path="config.json")
    assistant.start()
```

### Passo 7 — Testar

```bash
cd alicia
python main.py
```

Erros nesse ponto são quase sempre `ImportError`/`NameError` por um import
esquecido em algum arquivo novo — a mensagem de erro já aponta a linha exata.
Vá arquivo por arquivo até rodar limpo.

---

## 5. Opcional (avançado): quebrar a `AliciaAssistantCore` também

Mesmo depois do passo 4, `core.py` ainda vai ter ~3.700 linhas — é uma classe
só, mas gigante. Se quiser ir além, dá pra quebrar os 53 métodos dela em
**mixins**: cada mixin é uma classe só com um pedaço dos métodos, e a classe
final junta todas por herança múltipla. O comportamento em runtime não muda
nada, porque todos os métodos continuam enxergando o mesmo `self`.

Agrupamento sugerido pelos nomes dos métodos que já existem hoje:

| Mixin | Arquivo | Métodos (prefixo/nome) |
|---|---|---|
| `ConversationMixin` | `core/conversation.py` | `_load_persisted_history`, `_normalize_text`, `_fingerprint`, `_limit_response_length`, `_remember_conversation`, `_build_cognitive_history`, `_detect_topic_change`, `_lexical_similarity`, `_update_topic_state`, `_build_brain_prompt`, `_response_seems_stuck`, `_repair_response`, `parse_expression_tags`, `interact`, `_interact_impl` |
| `SocialCommandsMixin` | `core/social.py` | `_detect_alicia_greeting_command`, `_alicia_apresentar`, `_alicia_cumprimentar`, `_handle_alicia_social_command` |
| `VisionMixin` | `core/vision.py` | `_remember_vision_comment`, `_is_direct_visual_command`, `analyze_screen_now`, `screen_observation_loop` |
| `GamerModeMixin` | `core/gamer_mode.py` | todos os `*_gamer_*` e `*modo_gamer*` (19 métodos — o maior grupo) |
| `RestModeMixin` | `core/rest_mode.py` | `entrar_modo_descanso`, `sair_modo_descanso` |
| `SystemMixin` | `core/system.py` | `__init__`, `check_prerequisites`, `_ensure_model_available`, `_speak_async`, `mudar_microfone`, `cli_loop`, `start` |

E `core.py` vira só isto:

```python
from .conversation import ConversationMixin
from .social import SocialCommandsMixin
from .vision import VisionMixin
from .gamer_mode import GamerModeMixin
from .rest_mode import RestModeMixin
from .system import SystemMixin

class AliciaAssistantCore(
    SystemMixin,
    ConversationMixin,
    SocialCommandsMixin,
    VisionMixin,
    GamerModeMixin,
    RestModeMixin,
):
    pass
```

Isso é opcional e mais trabalhoso — eu recomendo fazer **só a divisão da
seção 3 primeiro**, usar o projeto por um tempo, e voltar pra isso depois se
ainda incomodar ter um arquivo de ~3.700 linhas.

---

## 6. Resumo do que fazer, na ordem

1. Backup/git commit do estado atual.
2. Criar as pastas (`alicia/alicia_core/ui/`).
3. Criar `deps.py` com os imports opcionais.
4. Copiar as 13 classes restantes para seus arquivos, uma de cada vez,
   testando com `python -c "import alicia_core.NOME_DO_ARQUIVO"` a cada uma.
5. Copiar `AliciaAssistantCore` para `core.py`.
6. Criar `main.py` com o bloco de entrada.
7. Rodar `python main.py` e corrigir imports que faltarem.
8. (Opcional) Quebrar `core.py` em mixins conforme a seção 5.
