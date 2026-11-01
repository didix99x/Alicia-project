"""
Cérebro conversacional da Alicia: monta a conversa com papéis corretos,
recupera memória/histórico relevante e chama a API (NVIDIA).

Separação de camadas (ver database.py):
  - system  : personalidade + regras + data + nome + memórias confirmadas + trechos antigos
  - user/assistant: apenas as últimas N mensagens de CONVERSA (visão nunca entra aqui)
  - user    : a mensagem atual (uma única vez)

Este módulo não grava nada no banco: quem decide o que salvar é o fluxo de
turno (conversation.py), para que cada mensagem seja gravada uma única vez.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .database import MemoryDatabase, _strip_accents
from .logging_setup import logger
from .nvidia_client import BrainError, NvidiaClient, discover_model, normalize_messages
from .prompts import PromptEngine

RECALL_RE = re.compile(
    r"\b(lembr\w*|falamos|conversamos|combinamos|decidimos|ontem|semana passada|ultima vez|"
    r"outro dia|antes|disse|falei|comentei|mencionei|te contei|retomar|continuar)\b"
)
VISUAL_RE = re.compile(r"\b(tela|vidro|viu|vendo|olhando|jogo|janela)\b")
WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def format_now(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return f"{WEEKDAYS[now.weekday()]}, {now.strftime('%d/%m/%Y %H:%M')}"


def fmt_db_timestamp(ts: str) -> str:
    """Converte o timestamp UTC do SQLite para data/hora local legível."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return ts or "?"


class ConversationBrain:
    def __init__(self, db: MemoryDatabase, client: NvidiaClient, config: Any):
        self.db = db
        self.client = client
        self.config = config
        self.on_model_change = None   # função(modelo) chamada quando o modelo é trocado sozinho
        self._healed = False

    # ------------------------------------------------------------------
    def _collect_memories(self, user_message: str) -> List[str]:
        relevant = self.db.search_memories(user_message, limit=5)
        latest = self.db.list_memories(limit=4)
        seen, out = set(), []
        for m in relevant + latest:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            out.append(f"{m['text']} (anotado em {fmt_db_timestamp(m['created_at'])}; origem: {m['origin']})")
            if len(out) >= 8:
                break
        return out

    def _collect_recalled(self, user_message: str, skip_latest: int) -> List[str]:
        if not RECALL_RE.search(_strip_accents(user_message.lower())):
            return []
        rows = self.db.search_history(user_message, limit=4, skip_latest=skip_latest)
        return [
            f"{fmt_db_timestamp(r['timestamp'])} — {'usuário' if r['role'] == 'user' else 'Alicia'}: {r['content'][:240]}"
            for r in rows
        ]

    def build_messages(
        self,
        user_message: str,
        user_name: str,
        visual_context: str = "",
        now: Optional[datetime] = None,
        personality: str = "normal",
        call_context: str = "",
        overlay=None,
    ) -> List[Dict[str, str]]:
        """Monta a lista de mensagens (já normalizada). NÃO inclui a mensagem atual no histórico duas vezes."""
        limit = int(getattr(self.config, "brain_history_messages", 20))
        history = self.db.get_chat_messages(limit=limit, after_id=self.db.context_start_id())
        memories = self._collect_memories(user_message)
        recalled = self._collect_recalled(user_message, skip_latest=len(history))
        wants_visual = bool(VISUAL_RE.search(_strip_accents(user_message.lower())))
        system = PromptEngine.chat_system_prompt(
            user_name=user_name,
            now_text=format_now(now),
            memories=memories,
            recalled=recalled,
            visual_context=visual_context if wants_visual else "",
            personality=personality,
            avoid_openers=self._recent_openers(history),
            call_context=call_context,
            extra_layer=(overlay.system if overlay else ""),
        )
        messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
        history = self._history_for_style(history)
        for item in history:
            content = item["content"]
            if item["role"] == "assistant":
                expr = (item.get("expression") or "").upper()
                if expr in ("FELIZ", "SERIO", "SURPRESA", "PENSANDO", "BRAVO"):
                    content = f"[{expr}] {content}"
            messages.append({"role": item["role"], "content": content})
        # lembrete do estilo colado na mensagem atual (só nesta chamada; o histórico guarda a mensagem original)
        current = user_message
        if personality and personality != PromptEngine.DEFAULT_PERSONALITY:
            current = f"{user_message}\n\n{PromptEngine.style_reminder(personality)}"
        if overlay and overlay.reminder:
            current = f"{current}\n\n{overlay.reminder}"
        messages.append({"role": "user", "content": current})
        return normalize_messages(messages)

    @staticmethod
    def _recent_openers(history: List[Dict[str, Any]], how_many: int = 4) -> List[str]:
        """As primeiras palavras das últimas respostas da Alicia: o modelo tende a repetir 'Ah,' / 'Hum,' em toda fala."""
        openers: List[str] = []
        for item in reversed(history):
            if item.get("role") != "assistant":
                continue
            text = re.sub(r"^\s*\[[A-ZÉ]+\]\s*", "", item.get("content") or "")
            words = re.findall(r"[\wÀ-ÿ']+", text)[:2]
            if words:
                o = " ".join(words)
                if o.lower() not in [x.lower() for x in openers]:
                    openers.append(o)
            if len(openers) >= how_many:
                break
        return openers

    STYLE_FRESH_TURNS = 10        # nas primeiras respostas depois da troca, o tom antigo não entra como exemplo

    def _history_for_style(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Logo após trocar de personalidade, mantém só a última troca antes da mudança (para o assunto não se perder)
        + tudo o que veio depois. Assim as respostas no tom velho não puxam o modelo de volta."""
        try:
            since = int(self.db.get_profile("personality_since_id") or 0)
        except (TypeError, ValueError):
            return history
        if since <= 0:
            return history
        before = [h for h in history if h["id"] <= since]
        after = [h for h in history if h["id"] > since]
        if not before or len(after) >= self.STYLE_FRESH_TURNS:
            return history
        return before[-2:] + after

    def reply(self, user_message: str, user_name: str, visual_context: str = "", call_context: str = "", overlay=None) -> str:
        """Texto bruto do modelo (com tag de expressão). Levanta BrainError."""
        personality = self.db.get_profile("personality") or PromptEngine.DEFAULT_PERSONALITY
        messages = self.build_messages(user_message, user_name, visual_context, personality=personality,
                                       call_context=call_context, overlay=overlay)
        kwargs = dict(
            temperature=float(getattr(self.config, "brain_temperature", 0.8)),
            top_p=float(getattr(self.config, "brain_top_p", 0.9)),
            max_tokens=int(getattr(self.config, "brain_max_tokens", 400)),
        )
        try:
            return self.client.chat(messages, **kwargs)
        except BrainError as e:
            # Modelo encerrado/removido: procura outro que funcione (uma vez por execução) e repete.
            if e.kind in ("not_found", "gone") and not self._healed:
                self._healed = True
                if self._heal_model():
                    return self.client.chat(messages, **kwargs)
            raise

    def _heal_model(self) -> bool:
        logger.warning("O modelo configurado não responde mais; procurando outro disponível...")
        result = discover_model(self.client._api_key, self.client.base_url, self.client.model,
                                time_budget=60.0, per_try_timeout=20.0)
        chosen = result.get("chosen") if result.get("ok") else None
        if not chosen:
            return False
        self.client.model = chosen
        self.client._tier = 0
        logger.warning(f"Modelo trocado automaticamente para {chosen}")
        if self.on_model_change:
            try:
                self.on_model_change(chosen)
            except Exception as e:
                logger.warning(f"Não consegui gravar o novo modelo no .env: {e}")
        return True

    def complete_text(self, prompt: str, temperature: float = 0.2, max_tokens: int = 500) -> str:
        """Tarefa de texto avulsa (ex.: gerar perfil de jogo), sem histórico nem personalidade."""
        return self.client.chat(
            [{"role": "user", "content": prompt}], temperature=temperature, top_p=0.8, max_tokens=max_tokens
        )


# ----------------------------------------------------------------------
# COMANDOS DE MEMÓRIA / NOME (determinísticos: a Alicia só diz que fez se fez)
# ----------------------------------------------------------------------
_POKER_REQUEST = re.compile(
    r"^(?!.*\b(como|qual|quais|regra|regras|o que|porque|por que|ensina|explica)\b)"
    r"(?=.*\b(poker|poquer)\b)(?=.*\b(jogar|jogamos|joga|bora|vamos|quero|abre|abrir|partida|mesa|uma|um)\b).{0,80}$")
_PREFIX = r"^(?:alicia[\s,:;-]+)?(?:por favor[\s,]+)?"
_REMEMBER = re.compile(
    _PREFIX + r"(?:lembre-?se|lembra|lembre|anote|anota|guarde|guarda|memorize|memoriza)"
    r"(?:\s+disso|\s+isso)?\s*(?:que|de que|:)\s+(.+)$", re.IGNORECASE)
_FORGET = re.compile(
    _PREFIX + r"(?:esque[cç]a|esquece)\s+(?:(?:que|sobre|o que (?:eu )?(?:disse|falei) sobre)\s+)?(.+)$",
    re.IGNORECASE)
_LIST = re.compile(
    r"(o que voc[eê] (?:lembra|sabe) (?:de|sobre) mim|quais (?:s[aã]o )?(?:as )?(?:suas )?mem[oó]rias|^/mem[oó]rias$)",
    re.IGNORECASE)
_RENAME = re.compile(
    _PREFIX + r"(?:(?:pode|quero que|prefiro que)\s+(?:voc[eê]\s+)?)?"
    r"(?:me\s+cham(?:e|a|ar)(?:-me)?\s+de|meu nome (?:é|e)|/nome)\s+(.+)$", re.IGNORECASE)

_NAME_OK = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\-]*(?: [A-Za-zÀ-ÿ'’\-]+){0,2}$")


def clean_name(raw: str) -> Optional[str]:
    """Valida um nome de tratamento (1 a 3 palavras, até 40 caracteres). None se inválido."""
    name = re.sub(r"[.!?,;:]+$", "", (raw or "").strip())
    name = re.sub(r"\s+", " ", name)
    if not name or len(name) > 40 or not _NAME_OK.match(name):
        return None
    return name


# ---- troca de personalidade com frases livres (voz costuma vir com pontuação e "enfeites") ----
_PERSONA_TRIGGERS = {"seja", "ser", "fique", "fica", "ficar", "vire", "vira", "virar", "mude", "muda", "mudar", "troque", "troca",
                     "trocar", "ative", "ativa", "ativar", "use", "usa", "usar", "volte", "volta", "voltar", "modo",
                     "personalidade", "estilo", "jeito", "tom", "coloque", "coloca", "passe", "passa", "passar", "tornar", "torne"}
_PERSONA_FILLERS = {"alicia", "por", "favor", "pfv", "pf", "quero", "que", "voce", "vc", "pode", "poderia", "podia", "consegue",
                    "da", "de", "do", "dos", "das", "pra", "para", "pro", "o", "a", "os", "as", "ao", "aos", "no", "na", "num", "numa",
                    "um", "uma", "pouco", "mais", "bem", "bastante", "agora", "entao", "vamos", "bora", "ja", "e", "tipo",
                    "minha", "sua", "seu", "meu", "ela", "ta", "ok", "certo", "depois", "so", "um", "uns", "gostaria", "preciso",
                    "queria", "prefiro", "quer", "pode", "estar", "esteja", "se", "comporte", "comportar"} | _PERSONA_TRIGGERS


def _parse_personality_flexible(low: str) -> Optional[Tuple[str, Optional[str]]]:
    """'Seja carinhosa.', 'alicia, muda pra malcriada', 'você pode ser mais séria?', 'volta ao normal'...
    Só vale se a frase for CURTA e tiver um verbo/palavra de troca; o resto precisa ser só enfeite + o nome do estilo."""
    words = re.findall(r"[a-z0-9]+", low)
    if not words or len(words) > 10 or not (set(words) & _PERSONA_TRIGGERS):
        return None
    rest = [w for w in words if w not in _PERSONA_FILLERS]
    if not rest or len(rest) > 2:
        return None
    key = PromptEngine.resolve_personality(" ".join(rest))
    return ("personality", key) if key else None


def parse_command(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """Reconhece ('rename'|'remember'|'forget'|'list', argumento) ou None."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    low = _strip_accents(t.lower())
    if low in ("/ajuda", "/help", "ajuda", "quais sao os comandos", "quais sao seus comandos"):
        return "help", None
    if low in ("/backup", "faca um backup", "fazer backup da memoria"):
        return "backup", None
    if low in ("/exportar", "/exportar conversa", "exporte a conversa"):
        return "export", None
    if low in ("/novoassunto", "/novo", "nova conversa", "vamos comecar uma nova conversa", "mudando de assunto"):
        return "newctx", None
    if low in ("/poker", "/poquer", "/pôquer") or _POKER_REQUEST.search(low):
        return "poker", None
    if low == "/personalidade" or low in ("trocar personalidade", "mudar personalidade", "menu de personalidade"):
        return "personality_menu", None
    m = re.match(r"^(?:/personalidade|(?:alicia[\s,:;-]+)?(?:muda|mude|troca|troque|mudar|trocar)\s+(?:a\s+)?(?:sua\s+)?personalidade(?:\s+(?:para|pra|de))?)\s+(.+)$", low)
    if m:
        key = PromptEngine.resolve_personality(m.group(1))
        return ("personality", key) if key else ("personality_menu", None)
    m = re.match(r"^(?:alicia[\s,:;-]+)?(?:seja|fique|fica|vire|vira|modo)\s+(?:mais\s+)?(\w[\w -]*)$", low)
    if m:
        key = PromptEngine.resolve_personality(m.group(1))
        if key:
            return "personality", key
    flex = _parse_personality_flexible(low)
    if flex:
        return flex
    m = _RENAME.match(t)
    if m:
        return "rename", m.group(1)
    if _LIST.search(t):
        return "list", None
    m = _REMEMBER.match(t)
    if m:
        return "remember", m.group(1).strip().rstrip(".")
    m = _FORGET.match(t)
    if m:
        return "forget", m.group(1).strip().rstrip(".")
    return None
