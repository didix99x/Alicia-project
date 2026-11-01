"""
MODO OBSERVADORA — a Alicia acompanha o que você faz no computador e comenta (100% local).

Peças (todas neste arquivo, exceto a visão e as falas):
  local_vision.py     janela ativa + captura + OCR + classificação (100% local)
  observer_lines.py   falas locais, nos tons normal / rebelde (Sem Filtro) / gentil
  VisualContext       memória VISUAL TEMPORÁRIA: só em RAM, nunca no banco, apagada ao desligar
  CommentPolicy       decide SE e QUANDO falar (silêncio é a regra; fala só com motivo)
  ObserverMode        liga/desliga, thread em segundo plano, comando imediato ("Alicia, olha"), privacidade

Privacidade (resumo; detalhes no README):
  * só observa depois de ativação explícita (botão, voz ou /observar); ao desligar, para tudo e apaga o contexto;
  * janela/app da lista de privacidade: nem captura; texto sensível (cartão, CPF, senha...) é descartado;
  * nenhuma imagem é gravada em disco nem enviada a serviço externo;
  * se o cérebro (NVIDIA) escrever o comentário, recebe SÓ fatos abstratos (categoria, programa, minutos, tipo de evento).
"""

from __future__ import annotations

import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .enums import AliciaExpression
from .local_vision import ActivityClassifier, Classification, LocalVision, ScreenReading, norm, np
from .logging_setup import logger
from . import observer_lines


# ======================================================================
# CONTEXTO VISUAL TEMPORÁRIO
# ======================================================================
@dataclass
class VisualEntry:
    ts: float
    key: str
    app: str
    category: str
    activity: str
    change: float
    events: List[str]
    excerpt: str


def _excerpt(text: str, limit: int = 200) -> str:
    out, used = [], 0
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(re.findall(r"[A-Za-zÀ-ÿ]", line)) < 4:
            continue
        out.append(line)
        used += len(line) + 1
        if used >= limit:
            break
    return " | ".join(out)[:limit]


class VisualContext:
    """Lembra o que acabou de acontecer na tela para entender CONTINUIDADE (jogo -> menu -> jogo = mesma atividade).

    Só RAM. `clear()` apaga tudo. Nada aqui é copiado para o banco nem para a memória principal."""

    def __init__(self, config):
        self.config = config
        self.clear()

    def clear(self):
        self.entries: Deque[VisualEntry] = deque()
        self.current_key = ""
        self.current_since = 0.0
        self.current_reading: Optional[ScreenReading] = None
        self.announced = False                   # esta atividade já "foi assunto"?
        self.candidate_key = ""
        self.candidate_since = 0.0
        self.last_seen_ts = 0.0
        self.last_analysis_ts = 0.0
        self.last_comment = ""
        self.last_comment_ts = 0.0
        self.last_comment_reason = ""
        self.comment_log: Deque[Tuple[float, str]] = deque(maxlen=30)
        self.fired_marks = set()
        self.event_log = {}                      # "evento:assinatura" -> quando foi comentado
        self.switches = 0

    # ------------------------------------------------------------------
    def update(self, r: ScreenReading, now: float) -> str:
        """Atualiza o contexto. Devolve: 'first' | 'continue' | 'pending' | 'switch'."""
        self.last_analysis_ts = now
        self.last_seen_ts = now
        settle = float(getattr(self.config, "obs_settle_seconds", 20.0))
        key = r.activity_key
        phase = "continue"
        if not self.current_key:
            self._begin(key, now)
            phase = "first"
        elif key == self.current_key:
            self.candidate_key = ""
        else:
            if key != self.candidate_key:
                self.candidate_key, self.candidate_since = key, now
            if now - self.candidate_since >= settle:       # ficou tempo bastante: é uma atividade nova de verdade
                self._begin(key, self.candidate_since)
                self.switches += 1
                phase = "switch"
            else:
                phase = "pending"                          # alt-tab rápido, menu, janela passageira: ainda é a mesma
        if phase in ("first", "switch", "continue"):
            self.current_reading = r
        self.entries.append(VisualEntry(now, key, r.app_label, r.category, r.activity, r.change, list(r.events), _excerpt(r.ocr_text)))
        self._prune(now)
        return phase

    def _begin(self, key: str, since: float):
        self.current_key, self.current_since = key, since
        self.candidate_key = ""
        self.announced = False
        self.fired_marks = set()

    def _prune(self, now: float):
        horizon = float(getattr(self.config, "obs_context_minutes", 30.0)) * 60.0
        cap = int(getattr(self.config, "obs_context_max_items", 60))
        while self.entries and (now - self.entries[0].ts > horizon or len(self.entries) > cap):
            self.entries.popleft()

    # ------------------------------------------------------------------
    def minutes_in_current(self, now: float) -> float:
        return max(0.0, (now - self.current_since) / 60.0) if self.current_key else 0.0

    def remember_comment(self, text: str, reason: str, now: float):
        self.last_comment, self.last_comment_ts, self.last_comment_reason = text, now, reason
        self.comment_log.append((now, text))

    def comments_last_hour(self, now: float) -> int:
        return sum(1 for ts, _ in self.comment_log if now - ts < 3600.0)

    def recent_comment_texts(self, n: int = 8) -> List[str]:
        return [t for _, t in list(self.comment_log)[-n:]]

    def summary(self, now: float) -> str:
        if not self.current_key or not self.current_reading:
            return ""
        r = self.current_reading
        mins = self.minutes_in_current(now)
        tempo = "agora há pouco" if mins < 1 else f"há uns {int(round(mins))} minutos"
        return f"{r.activity} ({tempo})"


# ======================================================================
# DECISÃO
# ======================================================================
@dataclass
class Decision:
    reason: str                 # "start" | "long" | "event"
    event: str = ""
    minutes: int = 0
    mark: int = 0


EXPR = {
    "event:erro_visivel": AliciaExpression.SURPRESA, "event:testes_falharam": AliciaExpression.SURPRESA,
    "event:testes_ok": AliciaExpression.FELIZ, "event:jogo_vitoria": AliciaExpression.FELIZ,
    "event:jogo_derrota": AliciaExpression.BRAVO, "start": AliciaExpression.SERIO, "long": AliciaExpression.PENSANDO,
}

EVENT_FACT = {
    "erro_visivel": "apareceu uma mensagem de erro na tela dele",
    "testes_ok": "os testes que ele rodou passaram",
    "testes_falharam": "os testes que ele rodou falharam",
    "jogo_vitoria": "ele acabou de vencer no jogo",
    "jogo_derrota": "ele acabou de perder no jogo",
}


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-zà-ÿ0-9]{4,}", (text or "").lower())}


def too_similar(text: str, previous: List[str], threshold: float = 0.6) -> bool:
    cur = _tokens(text)
    if not cur:
        return True
    for p in previous:
        if re.sub(r"\W+", "", p.lower()) == re.sub(r"\W+", "", text.lower()):
            return True
        prev = _tokens(p)
        union = len(cur | prev)
        if union and len(cur & prev) / union >= threshold:
            return True
    return False


# ======================================================================
# COMANDOS (voz e teclado)
# ======================================================================
_Q = re.compile(r"^(?:alicia )?(?:o que e|oque e|como|qual|quais|por que|porque|quando|onde|quem|pra que)\b")
_OFF = re.compile(
    r"\b(?:pare|para|parar|chega|deixa|deixe|desligue|desligar|desliga|desative|desativar|desativa|encerre|encerrar|cancele|cancela)\b"
    r".*\b(?:observar|observando|observadora|vigiar|olhar a minha tela|olhar minha tela)\b"
    r"|\bnao (?:me )?observe mais\b|\bchega de observar\b")
_ON = re.compile(
    r"\b(?:observar|observa|observe|vigiar|vigia) (?:a |minha |a minha )?tela\b"
    r"|\b(?:comece|comeca|comecar|inicie|iniciar|inicia|bora|vamos) (?:a )?observar\b"
    r"|\b(?:fica|fique|ficar|continue|continua) observando\b"
    r"|\b(?:ativ(?:ar|a|e)|ligar|liga|ligue)\b.*\bmodo observadora\b|^(?:alicia )?modo observadora$")
_WHAT = re.compile(r"\bo que (?:eu )?(?:estou|to|tou|ta) fazendo\b|\bo que (?:eu )?faco agora\b")


def parse_observer_command(text: str) -> Optional[str]:
    """'on' | 'off' | 'what' | None. 'Alicia, olha minha tela' NÃO entra aqui: continua sendo o olhar imediato."""
    t = norm(re.sub(r"[^\w\s+]", " ", text or ""))
    if not t or len(t.split()) > 12 or _Q.match(t):
        return None
    if _OFF.search(t):
        return "off"
    if _ON.search(t):
        return "on"
    if _WHAT.search(t):
        return "what"
    return None


# ======================================================================
# A OBSERVADORA
# ======================================================================
class ObserverMode:
    def __init__(self, core, vision: Optional[LocalVision] = None, rng: Optional[random.Random] = None,
                 clock: Callable[[], float] = time.time):
        self.core = core
        self.config = core.config
        self.clock = clock
        self.rng = rng or random.Random()
        self.ctx = VisualContext(self.config)
        self.vision = vision or self._default_vision()
        self._lock = threading.RLock()
        self._read_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._active = False
        self._generation = 0
        self._brain_block_until = 0.0
        self._recent_lines: Deque[str] = deque(maxlen=12)
        self.stats = {"ticks": 0, "captures": 0, "ocr_runs": 0, "comments": 0, "dropped": 0, "errors": 0}
        self._release_pending = False
        self._ocr_warned = False
        self.last_block = ""         # último motivo de silêncio (útil no log e nos testes)
        self.last_reading: Optional[ScreenReading] = None
        self._gamer_log: Dict[str, float] = {}   # eventos/marcos do Modo Gamer já comentados

    # ------------------------------------------------------------------ montagem
    def _default_vision(self) -> LocalVision:
        from .hardware import HardwareMonitor

        def window():
            return HardwareMonitor.get_active_process_name(), HardwareMonitor.get_active_window_title()

        def game(title, process):
            category, name = self.core._classify_window(title, process)
            return name if category == "game" else None

        return LocalVision(self.config, self.core.screen_engine, window, game_detector=game,
                           rect_provider=HardwareMonitor.get_active_window_rect)

    # ------------------------------------------------------------------ estado
    @property
    def active(self) -> bool:
        return self._active

    def status_text(self) -> str:
        if not self._active:
            return "Observadora desligada"
        now = self.clock()
        s = self.ctx.summary(now)
        return f"Observando: {s}" if s else "Observando (aguardando a primeira leitura)"

    def _paused_reason(self) -> str:
        c = self.core
        if getattr(c, "modo_descanso", False):
            return "modo descanso"
        if getattr(c, "poker_active", False):
            return "mesa de pôquer aberta"
        if not getattr(c, "is_running", True):
            return "Alicia encerrando"
        return ""

    # ------------------------------------------------------------------ liga / desliga
    def start(self, run_thread: bool = True) -> str:
        """`run_thread=False` liga o modo sem criar a thread (os testes dirigem `tick()` à mão)."""
        with self._lock:
            if self._active:
                return "Já estou observando a sua tela."
            if np is None:
                return "Não consigo observar a tela: falta a biblioteca NumPy (rode Iniciar_Alicia.bat --reinstall)."
            process, title, blocked = self.vision.inspect_window()
            if not blocked:
                try:                                    # teste real de captura: se não funciona, avisa já, com a causa
                    self.core.screen_engine.capture_raw(str(getattr(self.config, "obs_monitor", "active")),
                                                        self._rect_safe())
                except Exception as e:
                    logger.error(f"Observadora: captura de tela falhou ao ligar: {e}")
                    return f"Não consegui capturar a tela, então não liguei a observadora. Causa: {e}"
            self._generation += 1
            gen = self._generation
            self.ctx.clear()
            self.vision.reset()
            self._stop.clear()
            self._active = True
            if run_thread:
                self._thread = threading.Thread(target=self._run, args=(gen,), daemon=True, name="AliciaObservadora")
                self._thread.start()
        logger.info("👁️ Modo Observadora LIGADO.")
        return "Observadora ligada. Para parar é só dizer 'Alicia, pare de observar'."

    def stop(self, wait: bool = False) -> str:
        with self._lock:
            was = self._active
            self._active = False
            self._generation += 1                       # qualquer leitura em andamento passa a ser descartada
            self._stop.set()
            thread = self._thread
            self._thread = None
        self.ctx.clear()
        # Libera quadro, texto lido e motor de OCR. Se uma leitura estiver em andamento, a própria thread
        # termina a limpeza ao voltar (não travamos o botão da interface esperando o OCR).
        if self._read_lock.acquire(blocking=bool(wait)):
            try:
                self.vision.release()
                self._release_pending = False
            finally:
                self._read_lock.release()
        else:
            self._release_pending = True
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=15)
        if was:
            logger.info("👁️ Modo Observadora DESLIGADO (contexto visual apagado).")
            return "Observadora desligada. Parei de capturar a tela e apaguei o que eu lembrava dela."
        return "A observadora já estava desligada."

    def _rect_safe(self):
        try:
            return self.vision.rect_provider() if self.vision.rect_provider else None
        except Exception:
            return None

    # ------------------------------------------------------------------ laço em segundo plano
    def _run(self, gen: int):
        logger.info("👁️ Thread da Observadora iniciada.")
        while self._active and gen == self._generation and getattr(self.core, "is_running", True):
            delay = max(1.0, float(getattr(self.config, "obs_interval", 4.0)))
            try:
                self.tick()
            except Exception as e:
                self.stats["errors"] += 1
                loop_error = getattr(self.core, "_loop_error", None)
                delay = loop_error("observadora", e) if loop_error else 5.0
            else:
                ok = getattr(self.core, "_loop_ok", None)
                if ok:
                    ok("observadora")
            if self._stop.wait(delay):
                break
        logger.info("👁️ Thread da Observadora encerrada.")

    # ------------------------------------------------------------------ uma volta (também usada nos testes)
    def tick(self, now: Optional[float] = None, image=None) -> str:
        now = self.clock() if now is None else now
        gen = self._generation
        if not self._active:
            return "inactive"
        self.stats["ticks"] += 1
        paused = self._paused_reason()
        if paused:
            self.vision.reset()                         # pausa de verdade: sem captura, sem quadro antigo
            self.last_block = f"pausada ({paused})"
            return "paused"
        with self._read_lock:
            reading = self.vision.read(now=now, image=image)
        if gen != self._generation or not self._active:
            if not self._active and self._release_pending:          # desligada durante a leitura: termina a limpeza
                with self._read_lock:
                    self.vision.release()
                self._release_pending = False
            return "stale"                              # foi desligada enquanto lia: descarta tudo
        self.last_reading = reading
        if (not self._ocr_warned and bool(getattr(self.config, "obs_ocr", True)) and self.vision.ocr._probed
                and not self.vision.ocr.backend):
            self._ocr_warned = True
            logger.warning(f"Observadora sem OCR: {self.vision.ocr.error}")
            print(f"\n⚠️ Observadora: {self.vision.ocr.error}\n   Ela continua funcionando só com a janela ativa e a imagem.")
        if reading.skipped_reason:
            self.last_block = f"não observado: {reading.skipped_reason}"
            return "private"
        self.stats["captures"] += 1
        if reading.ocr_fresh:
            self.stats["ocr_runs"] += 1
        phase = self.ctx.update(reading, now)
        decision = self._decide(reading, phase, now)
        if decision is None:
            return "observed"
        return "commented" if self._comment(decision, reading, now, gen) else "dropped"

    # ------------------------------------------------------------------ porteiro e política
    def _gate(self, now: float, event: bool = False, category: str = "") -> str:
        """Motivo para NÃO falar agora ('' = pode)."""
        c, cfg, ctx = self.core, self.config, self.ctx
        tts = getattr(c, "tts", None)
        if tts is not None and (getattr(tts, "is_speaking", False) or getattr(tts, "audio_playing", False)):
            return "Alicia está falando"
        turn_lock = getattr(c, "_turn_lock", None)
        if turn_lock is not None and turn_lock.locked():
            return "conversa direta em andamento"
        quiet = float(getattr(cfg, "obs_quiet_after_user_seconds", 20.0))
        if now - float(getattr(c, "last_user_activity_time", 0.0) or 0.0) < quiet:
            return "você acabou de falar com ela"
        if category == "game" and getattr(c, "modo_gamer", False) and getattr(c, "vision_spontaneous", False):
            return "o Modo Gamer já comenta este jogo"
        cooldown = float(getattr(cfg, "obs_event_cooldown" if event else "obs_comment_cooldown", 90.0))
        last = max(ctx.last_comment_ts, float(getattr(c, "last_comment_time", 0.0) or 0.0))
        if now - last < cooldown:
            return "cooldown entre comentários"
        if ctx.comments_last_hour(now) >= int(getattr(cfg, "obs_max_comments_per_hour", 12)):
            return "limite de comentários por hora"
        return ""

    def _decide(self, r: ScreenReading, phase: str, now: float) -> Optional[Decision]:
        cfg, ctx = self.config, self.ctx
        talk = max(0.0, min(1.0, float(getattr(cfg, "obs_talkativeness", 0.7))))

        # 1) eventos (erro na tela, vitória, derrota...): o motivo mais forte para falar
        for ev in r.events:
            sig = f"{ev}:{r.event_sig}"
            if now - ctx.event_log.get(sig, 0.0) < 600.0:
                continue                                  # o MESMO erro/evento não é comentado de novo
            block = self._gate(now, event=True, category=r.category)
            if block:
                self.last_block = block
                continue
            ctx.event_log[sig] = now
            if self.rng.random() < min(1.0, talk + 0.3):
                return Decision("event", event=ev)

        # 2) atividade nova que se firmou
        if not ctx.announced and ctx.current_key == r.activity_key and (now - ctx.current_since) >= float(getattr(cfg, "obs_settle_seconds", 20.0)):
            block = self._gate(now, category=r.category)
            if block:
                self.last_block = block
            else:
                ctx.announced = True                      # decidiu (falar OU ficar quieta): não sorteia de novo a cada captura
                if r.category not in ("system", "unknown") and self.rng.random() < talk:
                    return Decision("start")
                self.last_block = "silêncio de propósito (sorteio de conversa)"

        # 3) muito tempo na mesma coisa
        if ctx.current_key == r.activity_key and r.category not in ("system", "launcher", "unknown"):
            minutes = ctx.minutes_in_current(now)
            marks = sorted(int(m) for m in (getattr(cfg, "obs_long_activity_minutes", [20, 45, 90]) or []))
            due = [m for m in marks if minutes >= m and m not in ctx.fired_marks]
            if due:
                for m in due[:-1]:
                    ctx.fired_marks.add(m)                # marcos já ultrapassados sem fala: não acumula
                mark = due[-1]
                block = self._gate(now, category=r.category)
                if block:
                    self.last_block = block
                else:
                    ctx.fired_marks.add(mark)
                    if self.rng.random() < min(1.0, talk + 0.15):
                        return Decision("long", minutes=int(minutes), mark=mark)
        return None

    # ------------------------------------------------------------------ tom e composição
    def tone(self) -> Tuple[str, int]:
        c = self.core
        unf = getattr(c, "unfiltered", None)
        if unf is not None and unf.enabled:
            return "rebel", unf.profanity_level()
        personality = c.get_personality() if hasattr(c, "get_personality") else "normal"
        if personality == "malcriada":
            return "rebel", 0
        if personality in ("seria", "carinhosa", "brincalhona"):
            return "gentle", 0
        return "normal", 0

    def _line_key(self, d: Decision, r: ScreenReading) -> str:
        if d.reason == "event":
            return f"event:{d.event}"
        return f"{d.reason}:{r.category}"

    def _local_line(self, d: Decision, r: ScreenReading) -> str:
        tone, prof = self.tone()
        facts = {"app": r.app_label or "esse programa", "min": d.minutes or int(self.ctx.minutes_in_current(self.clock()))}
        key = self._line_key(d, r)
        return observer_lines.pick(tone, key, facts, prof, list(self._recent_lines) + self.ctx.recent_comment_texts(), self.rng)

    def _expression(self, d: Decision) -> AliciaExpression:
        key = f"event:{d.event}" if d.reason == "event" else d.reason
        return EXPR.get(key, AliciaExpression.SERIO)

    def _brain_line(self, d: Decision, r: ScreenReading) -> Optional[Tuple[str, AliciaExpression]]:
        """Pede o comentário ao cérebro. Só vão FATOS ABSTRATOS: nada de título de janela, texto de tela ou imagem."""
        c, cfg = self.core, self.config
        nv = getattr(c, "nvidia", None)
        if not (bool(getattr(cfg, "obs_use_brain_for_comments", True)) and nv is not None and getattr(nv, "has_key", False)):
            return None
        if self.clock() < self._brain_block_until:
            return None
        from .prompts import PromptEngine
        name = (c.db.get_user_name() if getattr(c, "db", None) else "") or ""
        personality = c.get_personality() if hasattr(c, "get_personality") else "normal"
        activity = ActivityClassifier._phrase(Classification(r.category, r.app_label, "", ""))
        if d.reason == "event":
            motivo = f"Aconteceu agora: {EVENT_FACT.get(d.event, d.event)}."
        elif d.reason == "long":
            motivo = f"Ele está nessa mesma atividade há cerca de {d.minutes} minutos."
        else:
            motivo = "Ele acabou de começar essa atividade."
        recentes = "\n".join(f"- {t}" for t in self.ctx.recent_comment_texts(5)) or "(nenhum)"
        unf = getattr(c, "unfiltered", None)
        overlay = unf.overlay("screen") if (unf is not None and unf.enabled) else None
        system = "\n\n".join(filter(None, [
            PromptEngine.chat_personality(name),
            "[TAREFA — COMENTÁRIO ESPONTÂNEO SOBRE O QUE O USUÁRIO ESTÁ FAZENDO]\n"
            "Você está observando o computador dele e quer soltar UM comentário curto, natural e falável.\n"
            f"FATOS (não existe nada além disto; não invente):\n- Atividade: {activity}\n- Programa: {r.app_label or 'desconhecido'}\n- {motivo}\n"
            "Regras: 1 a 2 frases curtas; específica ao fato; não cite texto da tela nem nomes de arquivo; "
            "não faça pergunta se não combinar; não repita os comentários recentes.\n"
            f"Comentários recentes (NÃO repita ideia nem estrutura):\n{recentes}\n"
            "Comece com exatamente uma tag: [FELIZ] [SERIO] [SURPRESA] [PENSANDO] [BRAVO].",
            PromptEngine.style_line(personality) if personality != PromptEngine.DEFAULT_PERSONALITY else "",
            overlay.system if overlay else "",
        ]))
        try:
            from .nvidia_client import BrainError
            raw = nv.chat([{"role": "system", "content": system}, {"role": "user", "content": "Comente agora."}],
                          temperature=0.9, top_p=0.92, max_tokens=int(getattr(cfg, "brain_max_tokens", 400)) // 2 or 120)
        except Exception as e:
            self._brain_block_until = self.clock() + 300.0
            logger.warning(f"Observadora: o cérebro não gerou o comentário ({type(e).__name__}); uso as falas locais por 5 min.")
            return None
        text, expr = c.parse_expression_tags(raw)
        text = c._limit_response_length(text, max_sentences=2, max_chars=260)
        if not text or text.upper() == "VAZIO":
            return None
        return text, expr

    # ------------------------------------------------------------------ comentar
    def _comment(self, d: Decision, r: ScreenReading, now: float, gen: int) -> bool:
        brain = self._brain_line(d, r)
        text, expr = brain if brain else (self._local_line(d, r), self._expression(d))
        if brain and too_similar(text, self.ctx.recent_comment_texts()):
            text, expr = self._local_line(d, r), self._expression(d)
        if not text or too_similar(text, self.ctx.recent_comment_texts(10)):
            self.stats["dropped"] += 1
            self.last_block = "comentário repetido descartado"
            return False
        # a geração pode ter demorado (rede): confere de novo se ainda vale a pena falar
        later = self.clock() if brain else now
        if gen != self._generation or not self._active:
            self.stats["dropped"] += 1
            return False
        block = self._gate(later, event=(d.reason == "event"), category=r.category)
        if block:
            self.stats["dropped"] += 1
            self.last_block = f"comentário descartado: {block}"
            return False
        key = r.activity_key
        self.ctx.remember_comment(text, d.reason, later)
        self._recent_lines.append(text)
        self.stats["comments"] += 1
        c = self.core
        c.last_comment_time = later
        c.current_expression = expr
        c.last_visual_context = f"(observadora) {r.activity}"
        print(f"\n🤖 Alicia [{expr.value.upper()}] (Observadora): {text}")

        def still_relevant() -> bool:
            return (self._active and gen == self._generation and self.ctx.current_key == key
                    and not getattr(c, "poker_active", False) and not getattr(c, "modo_descanso", False))

        try:
            c._speak_async(text, expr, should_play=still_relevant)
        except TypeError:                                  # núcleo sem should_play (testes antigos)
            c._speak_async(text, expr)
        return True

    # ------------------------------------------------------------------ Modo Gamer (análise 100% local)
    def gamer_comment(self, image, game: str = "", minutes: float = 0.0) -> Optional[Tuple[str, AliciaExpression, ScreenReading]]:
        """Análise LOCAL do Modo Gamer: lê o quadro do jogo (janela + OCR + movimento) e, se houve vitória/derrota
        na tela ou um marco de tempo de jogo (obs_long_activity_minutes), devolve (fala, expressão, leitura).
        Nada sai do computador e a imagem fica só na memória. None = nada a comentar agora."""
        if np is None:
            return None
        was_active = self._active
        try:
            with self._read_lock:
                reading = self.vision.read(now=self.clock(), image=image)
        except Exception as e:
            logger.debug(f"Gamer local: leitura falhou: {e}")
            return None
        finally:
            if not was_active:
                self.vision.release()
        if reading.skipped_reason:                       # janela/texto sensível: não observa
            return None
        now = self.clock()
        tone, prof = self.tone()
        facts = {"app": game or reading.app_label or "o jogo", "min": int(minutes)}
        recent = list(self._recent_lines) + self.ctx.recent_comment_texts()
        log = self._gamer_log
        for ev in reading.events:
            if ev not in ("jogo_vitoria", "jogo_derrota"):
                continue
            sig = f"{ev}:{reading.event_sig}"
            if now - log.get(sig, 0.0) < 120.0:          # o mesmo resultado na tela não é comentado de novo
                continue
            log[sig] = now
            text = observer_lines.pick(tone, f"event:{ev}", facts, prof, recent, self.rng)
            if text:
                self._recent_lines.append(text)
                return text, EXPR.get(f"event:{ev}", AliciaExpression.SERIO), reading
        marks = sorted(int(m) for m in (getattr(self.config, "obs_long_activity_minutes", [20, 45, 90]) or []))
        due = [m for m in marks if minutes >= m and f"{game}:{m}" not in log]
        if due:
            for m in due:
                log[f"{game}:{m}"] = now                  # marcos passados sem fala não se acumulam
            text = observer_lines.pick(tone, "long:game", {**facts, "min": due[-1]}, prof, recent, self.rng)
            if text:
                self._recent_lines.append(text)
                return text, EXPR["long"], reading
        return None

    # ------------------------------------------------------------------ comando imediato ("Alicia, olha")
    def analyze_now(self, user_message: str = "", what_doing: bool = False) -> Tuple[str, AliciaExpression, bool]:
        """Olha a tela AGORA (com ou sem a observadora ligada) e responde com o que de fato leu. Usa somente a leitura local."""
        if np is None:
            return ("Não consigo analisar a tela: falta a biblioteca NumPy (rode Iniciar_Alicia.bat --reinstall).",
                    AliciaExpression.SERIO, False)
        was_active = self._active
        try:
            with self._read_lock:
                reading = self.vision.read(force_ocr=True, now=self.clock())
        except Exception as e:
            logger.error(f"Olhar imediato: captura falhou: {e}")
            return (f"Não consegui capturar a tela agora. Causa: {e}", AliciaExpression.BRAVO, False)
        finally:
            if not was_active:
                self.vision.release()                      # olhar avulso não deixa nada carregado na memória
        self.last_reading = reading
        tone, prof = self.tone()
        pick = lambda key, **f: observer_lines.pick(tone, key, f, prof, [], self.rng)

        if reading.skipped_reason:
            return (pick("silence_privacy"), AliciaExpression.SERIO, True)

        facts = []
        if reading.low_confidence:
            why = ("o OCR não leu texto nenhum" if self.vision.ocr.backend or self.vision.ocr.available
                   else "não há OCR instalado para ler o texto")
            return (f"Capturei a tela, mas não consegui identificar o que você está fazendo: a janela ativa "
                    f"({reading.process or 'desconhecida'}) não é um programa que eu conheça e {why}.",
                    AliciaExpression.PENSANDO, False)

        now = self.clock()
        if what_doing and self._active and self.ctx.current_key == reading.activity_key and self.ctx.minutes_in_current(now) >= 1:
            facts.append(f"Você está {reading.activity}, e isso já dura uns {int(round(self.ctx.minutes_in_current(now)))} minutos.")
        else:
            facts.append(f"Pelo que eu vejo, você está {reading.activity}.")
        if "erro_visivel" in reading.events:
            facts.append("Tem uma mensagem de erro visível na tela.")
        if "testes_falharam" in reading.events:
            facts.append("Os testes na tela estão falhando.")
        if "testes_ok" in reading.events:
            facts.append("Os testes na tela passaram.")
        if "jogo_vitoria" in reading.events:
            facts.append("Parece que você acabou de vencer.")
        if "jogo_derrota" in reading.events:
            facts.append("Parece que você acabou de perder.")
        if reading.category in ("video", "game") and reading.motion >= 15:
            facts.append("A imagem está em movimento, então deve estar rolando agora.")
        elif reading.motion < 1 and reading.change < 1 and not reading.first_frame and reading.category in ("video", "game"):
            facts.append("A tela está parada.")
        if not reading.ocr_text and getattr(self.config, "obs_ocr", True) and not self.vision.ocr.backend:
            facts.append("Não consigo ler o texto da tela porque não tenho OCR instalado.")
        tail = pick("look_tail_event" if reading.events else "look_tail")
        expr = AliciaExpression.SURPRESA if reading.events else AliciaExpression.PENSANDO
        return (" ".join(facts + ([tail] if tail else [])), expr, True)

    # ------------------------------------------------------------------ comandos
    def handle_command(self, kind: str) -> Tuple[str, AliciaExpression]:
        if kind == "on":
            msg = self.start()
            return msg, AliciaExpression.PENSANDO if self._active else AliciaExpression.SERIO
        if kind == "off":
            return self.stop(), AliciaExpression.SERIO
        if kind == "what":
            text, expr, _ = self.analyze_now("o que estou fazendo?", what_doing=True)
            return text, expr
        return "Não entendi esse comando da observadora.", AliciaExpression.SERIO

    @staticmethod
    def parse_command(text: str) -> Optional[str]:
        return parse_observer_command(text)
