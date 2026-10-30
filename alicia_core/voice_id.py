"""
Reconhecimento de quem está falando (locutor) + cadastro de vozes com consentimento.

Não substitui o STT: o texto continua vindo do SpeechRecognition. Aqui só se decide "quem falou", com base nos
embeddings de speaker_model.py e nos perfis de voice_profiles.py. Tudo local; nenhum áudio é gravado em disco.

Cadastro (sempre com consentimento explícito da própria pessoa, por voz):
    [oferta] -> [nome] -> [autorização] -> [amostras] -> perfil criado
  * Dono: "cadastrar minha voz" / "cadastrar a voz do Pedro" / "refazer o cadastro do Pedro".
  * Voz desconhecida que chama a Alicia na call recebe a oferta (e pode recusar).
  * A autorização e as amostras precisam vir da MESMA voz (comparação por embedding).
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from typing import Callable, List, Optional, Tuple

from .deps import np
from .logging_setup import logger
from .speaker_model import SpeakerModel, cosine, ensure_model
from .voice_profiles import (DISABLED, IDENTIFIED, NO_PROFILES, TOO_SHORT, UNCERTAIN, UNKNOWN, Identity,
                             ProfileStore)

REPLY = Tuple[str, str]                    # (fala, expressão: feliz|serio|pensando|surpresa|bravo)

SAMPLE_PROMPTS = (
    "Me conta, numa frase inteira, o que você fez hoje.",
    "Agora descreve o lugar onde você está neste momento.",
    "Diz qual é o seu filme ou série favorita e por quê.",
    "Fala uma coisa engraçada que já aconteceu com você.",
    "Conta o que você costuma fazer no fim de semana.",
    "Explica como você prepara o seu prato preferido.",
    "Fala sobre o último jogo que você jogou.",
)
SAME_VOICE_MIN = 0.40          # a voz que consente/fala as amostras tem que ser parecida com a primeira ouvida
SAMPLE_VS_CENTROID_MIN = 0.45
MAX_BAD_TURNS = 4

_YES = re.compile(r"\b(sim|autorizo|autorizado|pode|concordo|aceito|claro|quero|topo|com certeza)\b")
_NO = re.compile(r"\b(nao|nunca|negativo|recuso|cancela\w*|deixa pra la|esquece)\b")


def _plain(text: str) -> str:
    t = "".join(c for c in unicodedata.normalize("NFD", (text or "").lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", t)).strip()


# ----------------------------------------------------------------------------------------------------------------
# Comandos (texto já falado/digitado). Quem pode executar é decidido por quem chama.
# ----------------------------------------------------------------------------------------------------------------
_NAME_TAIL = r"(?:d[oae]|pr[oa]|para)\s+(?:o\s+|a\s+)?(?P<n>[a-z0-9][a-z0-9 ]{0,38})"


def parse_voice_command(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """('voice_enroll'|'voice_redo'|'voice_delete'|'voice_list'|'voice_on'|'voice_off'|'voice_cancel', alvo) ou None.
    Alvo: nome, 'self' (a própria pessoa/dono) ou None."""
    low = _plain(text)
    words = low.split()
    if not words or len(words) > 14:
        return None
    low = re.sub(r"^alicia\s+", "", low)
    low = re.sub(r"^(por favor|pode|poderia|quero que voce|preciso que voce)\s+", "", low)
    if re.fullmatch(r"(cancel\w+|para|pare|aborta\w*)( o)?( cadastro( de voz)?)?", low) and "cadastro" in low:
        return "voice_cancel", None
    if re.search(r"\b(desativ\w*|deslig\w*|paus\w*)\b.*\breconhecimento de voz\b", low):
        return "voice_off", None
    if re.search(r"\b(ativ\w*|lig[ae]\w*|retom\w*)\b.*\breconhecimento de voz\b", low):
        return "voice_on", None
    if re.search(r"\b(list\w+|quais|quem)\b.*\bvozes?\b|\bquem voce (conhece|reconhece) pela voz\b", low):
        return "voice_list", None
    verb_enroll = r"(?:cadastr\w+|registr\w+|guard\w+|memoriz\w+|aprend\w+)"
    verb_del = r"(?:apag\w+|exclu\w+|delet\w+|remov\w+|esquec\w+)"
    verb_redo = r"(?:refa[zc]\w*|recadastr\w+|cadastr\w+ de novo|atualiz\w+)"
    if re.fullmatch(rf"{verb_redo}\s+(?:o\s+|a\s+)?(?:meu\s+|minha\s+)?(?:cadastro|voz)(?:\s+de\s+voz)?", low):
        return "voice_redo", "self"
    m = re.fullmatch(rf"{verb_redo}\s+(?:o\s+)?(?:cadastro|voz)(?:\s+de\s+voz)?\s+{_NAME_TAIL}", low)
    if m:
        return "voice_redo", m.group("n").strip()
    if re.fullmatch(rf"{verb_del}\s+(?:a\s+)?(?:minha\s+)?(?:voz|cadastro(?: de voz)?)", low) and ("minha" in low or "meu" in low):
        return "voice_delete", "self"
    m = re.fullmatch(rf"{verb_del}\s+(?:a\s+)?(?:voz|cadastro(?: de voz)?)\s+{_NAME_TAIL}", low)
    if m:
        return "voice_delete", m.group("n").strip()
    if re.fullmatch(rf"{verb_enroll}\s+(?:a\s+)?(?:minha\s+)?voz(?:\s+(?:agora|ai|pra voce))?", low) and "minha" in low:
        return "voice_enroll", "self"
    m = re.fullmatch(rf"{verb_enroll}\s+(?:a\s+)?voz\s+{_NAME_TAIL}", low)
    if m:
        return "voice_enroll", m.group("n").strip()
    return None


def extract_name(text: str) -> Optional[str]:
    """'meu nome é Pedro', 'me chama de Pê', 'Pedro' -> 'Pedro' (1 a 3 palavras, sem números)."""
    from .brain import clean_name
    t = re.sub(r"\s+", " ", (text or "").strip())
    t = re.sub(r"(?i)^(?:ah\s+|então\s+|entao\s+)?(?:meu nome é|meu nome e|me chamo|pode me chamar de|me chama de|me chame de|"
               r"eu sou (?:o|a)|sou (?:o|a)|eu sou|sou|é|e)\s+", "", t)
    name = clean_name(t)
    if name and name.lower() not in {"sim", "nao", "não", "alicia"}:
        return name[:1].upper() + name[1:] if name.islower() else name
    return None


class _Enroll:
    def __init__(self, stage: str, name: Optional[str], source: str, *, is_owner: bool = False,
                 redo_of: Optional[str] = None, now: float = 0.0):
        self.stage = stage                 # offer | name | consent | samples
        self.name = name
        self.source = source               # mic | call: de onde vêm as falas desta pessoa
        self.is_owner = is_owner
        self.redo_of = redo_of
        self.ref = None                    # embedding de referência: a mesma voz precisa continuar até o fim
        self.samples: List = []
        self.bad = 0
        self.last = now
        self.prompt_i = 0


class VoiceID:
    def __init__(self, config, store: Optional[ProfileStore] = None, model: Optional[SpeakerModel] = None,
                 owner_name_getter: Callable[[], str] = lambda: "", clock: Callable[[], float] = time.time):
        self.config = config
        self.store = store or ProfileStore()
        self.model = model or SpeakerModel()
        self.owner_name = owner_name_getter
        self.clock = clock
        self._lock = threading.RLock()
        self._enroll: Optional[_Enroll] = None
        self._offer_snooze_until = 0.0
        self.last_error = ""

    # ------------------------------------------------------------------ estado
    def _cfg(self, name, default):
        return getattr(self.config, name, default)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("voice_id_enabled", True))

    @property
    def ready(self) -> bool:
        return self.enabled and self.model.available

    @property
    def enrolling(self) -> bool:
        with self._lock:
            self._expire()
            return self._enroll is not None

    def has_profiles(self) -> bool:
        return bool(self.store.active_profiles())

    def _expire(self):
        e = self._enroll
        if e and self.clock() - e.last > float(self._cfg("voice_id_enroll_timeout", 120.0)):
            logger.info("Cadastro de voz cancelado por inatividade.")
            self._enroll = None

    # ------------------------------------------------------------------ identificação
    def identify(self, pcm: bytes, rate: int, source: str = "mic") -> Identity:
        """Quem falou neste trecho. Só roda o modelo se houver perfis (ou cadastro em andamento) e a voz estiver ativa."""
        if not self.enabled:
            return Identity(status=DISABLED)
        if not self.has_profiles() and not self.enrolling:
            return Identity(status=NO_PROFILES)
        try:
            emb = self.model.embed(pcm, rate)
        except Exception as e:
            logger.error(f"Falha ao analisar a voz: {type(e).__name__}: {e}")
            return Identity(status=UNKNOWN)
        if emb is None:
            return Identity(status=TOO_SHORT if self.model.available else DISABLED)
        ident = self.store.identify(emb, margin=float(self._cfg("voice_id_margin", 0.08)))
        ident._emb = emb                                    # usado só em memória (cadastro / oferta)
        if ident.known:
            self.store.record_identification(ident.user_id, ident.confidence, source)
        elif ident.status in (UNKNOWN, UNCERTAIN):
            self.store.note_unknown("uncertain" if ident.status == UNCERTAIN else "unknown")
        logger.info(f"🔎 voz ({source}): {ident.status} {ident.name or ''} conf={ident.confidence:.2f} {ident.candidates[:2]}")
        return ident

    @staticmethod
    def label(ident: Optional[Identity], default: str = "Amigo") -> str:
        """Como a fala aparece no contexto da call. Nunca afirma um nome sem confiança."""
        if ident is None:
            return default
        if ident.known:
            return ident.name or default
        if ident.status == UNCERTAIN and ident.name:
            return f"{default} (voz parecida com {ident.name}, sem certeza)"
        return default

    def context_for(self, ident: Optional[Identity]) -> str:
        """Bloco de prompt com o que ESTA pessoa autorizou guardar. Vazio para o dono (a memória dele fica no banco)."""
        if ident is None or not ident.known or ident.is_owner:
            return ""
        p = self.store.get(ident.user_id)
        if not p:
            return ""
        lines = [f"Quem está falando agora: {p['name']} (reconhecido pela voz com {ident.confidence:.0%} de similaridade; a identificação pode errar)."]
        if p.get("preferences"):
            lines.append("Preferências: " + "; ".join(f"{k}: {v}" for k, v in p["preferences"].items()))
        if p["context"]["notes"]:
            lines.append("O que ela(e) pediu para você guardar: " + "; ".join(p["context"]["notes"][-8:]))
        inter = p["context"]["interactions"][-4:]
        if inter:
            lines.append("Últimas conversas com esta pessoa:\n" + "\n".join(f"- {i['user']} → {i['alicia']}" for i in inter))
        return "\n".join(lines)

    def record_interaction(self, ident: Optional[Identity], user_text: str, answer: str):
        if ident is not None and ident.known:
            try:
                self.store.add_interaction(ident.user_id, user_text, answer)
            except Exception as e:
                logger.error(f"Não consegui salvar a interação do perfil: {e}")

    # ------------------------------------------------------------------ comandos
    def run_command(self, kind: str, arg: Optional[str], speaker: Optional[Identity] = None, *, by_owner: bool = True,
                    call_active: bool = False) -> REPLY:
        """Executa um comando de voz. `by_owner`: quem pediu é o dono. Outros só mexem no PRÓPRIO perfil."""
        try:
            if kind == "voice_list":
                names = [p["name"] + (" (você)" if p.get("is_owner") else "") for p in self.store.list()]
                state = "ligado" if self.enabled else "desligado"
                if not names:
                    return f"Reconhecimento de voz {state}. Ainda não conheço ninguém pela voz. Diga 'cadastrar minha voz'.", "pensando"
                return f"Reconhecimento de voz {state}. Conheço pela voz: {', '.join(names)}.", "pensando"
            if kind in ("voice_on", "voice_off"):
                if not by_owner:
                    return "Isso só quem me configura pode mexer.", "serio"
                self.config.voice_id_enabled = kind == "voice_on"
                self.config.save()
                return ("Reconhecimento de voz ligado." if kind == "voice_on" else
                        "Reconhecimento de voz desligado. Seus perfis continuam guardados, mas não vou usá-los."), "feliz"
            if kind == "voice_cancel":
                with self._lock:
                    had = self._enroll is not None
                    self._enroll = None
                return ("Cadastro cancelado. Não guardei nada." if had else "Não há cadastro em andamento."), "serio"

            target = self._resolve_target(arg, speaker, by_owner)
            if isinstance(target, str):
                return target, "serio"
            name, profile, is_owner = target
            if kind == "voice_delete":
                if not profile:
                    return f"Não tenho voz cadastrada de {name}.", "pensando"
                self.store.delete(profile["user_id"])
                return f"Pronto, apaguei o perfil de voz de {name}, junto com tudo o que eu guardava sobre essa pessoa.", "serio"
            if kind in ("voice_enroll", "voice_redo"):
                if not by_owner:
                    return "Só quem me configura pode abrir um cadastro. Mas você pode pedir pra apagar a sua voz quando quiser.", "serio"
                if kind == "voice_enroll" and profile:
                    return f"Já tenho a voz de {name}. Para trocar, diga 'refazer cadastro do {name}'.", "pensando"
                if kind == "voice_redo" and not profile:
                    return f"Não tenho cadastro de {name} para refazer. Diga 'cadastrar a voz do {name}'.", "pensando"
                return self._begin(name, is_owner=is_owner, redo_of=profile["user_id"] if profile else None,
                                   source="mic" if is_owner or not call_active else "call")
        except Exception as e:
            logger.exception(f"Erro no comando de voz '{kind}': {type(e).__name__}")
            return "Não consegui mexer nos perfis de voz agora. Veja o log.", "bravo"
        return "Não entendi esse comando de voz.", "serio"

    def _resolve_target(self, arg: Optional[str], speaker: Optional[Identity], by_owner: bool):
        if arg == "self":
            if by_owner:
                owner_name = (self.owner_name() or "").strip() or "Dono"
                return owner_name, self.store.owner(), True
            if speaker is not None and speaker.known:
                return speaker.name, self.store.get(speaker.user_id), False
            return "Não reconheci quem está pedindo, então não mexo em perfil nenhum. Peça de novo mais perto do microfone ou peça ao dono."
        if not arg:
            return "Qual nome? Diga, por exemplo, 'apagar a voz do Pedro'."
        profile = self.store.find_by_name(arg)
        if not by_owner and (speaker is None or not speaker.known or not profile or profile["user_id"] != speaker.user_id):
            return "Eu só apago a voz de alguém a pedido da própria pessoa (reconhecida) ou do dono."
        name = profile["name"] if profile else arg.strip().title()
        return name, profile, bool(profile and profile.get("is_owner"))

    # ------------------------------------------------------------------ cadastro
    def _begin(self, name: str, *, is_owner: bool, redo_of: Optional[str], source: str) -> REPLY:
        if not self.enabled:
            return "O reconhecimento de voz está desligado. Diga 'ativar reconhecimento de voz' antes.", "serio"
        if not self.model.available:
            ok = ensure_model(download=True) is not None
            self.model = SpeakerModel() if ok else self.model
            if not ok or not self.model.available:
                return ("Não consegui preparar o modelo de voz (precisa do onnxruntime e de internet uma vez para baixar ~26 MB). "
                        "Veja o log."), "bravo"
        with self._lock:
            self._enroll = _Enroll("consent", name, source, is_owner=is_owner, redo_of=redo_of, now=self.clock())
        return self._consent_text(name, is_owner), "pensando"

    @staticmethod
    def _consent_text(name: str, is_owner: bool) -> str:
        return (f"{name}, antes de tudo: vou guardar um perfil numérico da sua voz, só neste computador, sem gravar áudio, "
                "para saber quem está falando. Dá pra apagar quando quiser. Você autoriza? Diga: sim, autorizo.")

    def maybe_offer(self, ident: Identity, *, source: str = "call") -> Optional[REPLY]:
        """Voz DESCONHECIDA chamou a Alicia: oferece o cadastro (uma vez por intervalo; 'não' silencia por um tempo)."""
        if not (self.ready and self._cfg("voice_id_offer_unknown", True) and self.has_profiles()):
            return None
        if ident.status != UNKNOWN or getattr(ident, "_emb", None) is None:
            return None
        now = self.clock()
        with self._lock:
            self._expire()
            if self._enroll is not None or now < self._offer_snooze_until:
                return None
            e = _Enroll("offer", None, source, now=now)
            e.ref = ident._emb
            self._enroll = e
            self._offer_snooze_until = now + float(self._cfg("voice_id_offer_cooldown", 600.0))
        return ("Ainda não conheço a sua voz. Se quiser, guardo ela para te reconhecer nas próximas vezes e te chamar pelo nome. "
                "Quer cadastrar? Responda sim ou não."), "pensando"

    def feed_enrollment(self, text: str, pcm: bytes, rate: int, source: str) -> Optional[REPLY]:
        """Chamado com cada fala transcrita. Devolve a resposta da Alicia se o cadastro consumiu a fala; senão None."""
        with self._lock:
            self._expire()
            e = self._enroll
            if e is None or e.source != source:
                return None
            e.last = self.clock()
            low = _plain(text)
            if re.fullmatch(r"(?:alicia )?(?:cancel\w*|para|pare|aborta\w*|desisti\w*)(?: (?:o )?(?:cadastro|tudo))?", low):
                self._enroll = None
                return "Tudo bem, cancelei. Não guardei nada.", "serio"
            emb = None
            try:
                emb = self.model.embed(pcm, rate) if self.ready else None
            except Exception as ex:
                logger.error(f"Falha ao analisar a voz no cadastro: {type(ex).__name__}")
            # a voz tem que ser a mesma do início do cadastro; outra pessoa falando não entra
            if emb is not None and e.ref is not None and cosine(emb, e.ref) < SAME_VOICE_MIN:
                logger.info("Cadastro de voz: fala de outra voz ignorada.")
                return None
            if e.ref is None and emb is not None:
                e.ref = emb
            return getattr(self, f"_stage_{e.stage}")(e, text, low, emb)

    def _fail(self, e: _Enroll, msg: str) -> REPLY:
        e.bad += 1
        if e.bad >= MAX_BAD_TURNS:
            self._enroll = None
            return "Não estou conseguindo, então vou parar por aqui. Nada foi guardado. Tente de novo mais tarde.", "serio"
        return msg, "pensando"

    def _stage_offer(self, e, text, low, emb) -> REPLY:
        if _NO.search(low):
            self._enroll = None
            return "Sem problema. Não guardo nada.", "serio"
        if _YES.search(low):
            e.stage = "name"
            return "Combinado. Como você quer que eu te chame?", "feliz"
        return self._fail(e, "Não entendi. Quer que eu guarde a sua voz? Diga sim ou não.")

    def _stage_name(self, e, text, low, emb) -> REPLY:
        name = extract_name(text)
        if not name:
            return self._fail(e, "Não peguei o nome. Diga só como quer ser chamado, por exemplo: Pedro.")
        if self.store.find_by_name(name) or name.lower() == (self.owner_name() or "").strip().lower():
            return self._fail(e, f"Já existe um {name} entre as vozes que conheço. Escolha outro nome ou apelido.")
        e.name = name
        e.stage = "consent"
        return self._consent_text(name, False), "pensando"

    def _stage_consent(self, e, text, low, emb) -> REPLY:
        if _NO.search(low):
            self._enroll = None
            return "Tudo certo, não vou guardar nada.", "serio"
        if not _YES.search(low):
            return self._fail(e, f"Preciso de um sim claro de {e.name}. Diga: sim, autorizo. Ou diga não.")
        if not e.is_owner and emb is not None:
            owner = self.store.owner()
            if owner and cosine(emb, owner["voice_profile"]["centroid"]) >= float(self._cfg("voice_id_threshold", 0.55)):
                return self._fail(e, f"Quem autoriza precisa ser {e.name}, não o dono. Peça para {e.name} dizer: sim, autorizo.")
        if emb is not None:
            existing = self.store.identify(emb)
            if existing.known and not (e.redo_of and existing.user_id == e.redo_of):
                self._enroll = None
                return f"Essa voz já está cadastrada como {existing.name}. Não vou duplicar.", "pensando"
        e.stage = "samples"
        return f"Obrigada, {e.name}. Agora vou ouvir {self._cfg('voice_id_enroll_samples', 5)} frases. {SAMPLE_PROMPTS[0]}", "feliz"

    def _stage_samples(self, e, text, low, emb) -> REPLY:
        if emb is None:
            return self._fail(e, "Essa frase ficou curta ou baixa demais. Fala uma frase inteira, um pouco mais alto.")
        if e.samples:
            centroid = np.mean(np.stack(e.samples), axis=0)
            if cosine(emb, centroid) < SAMPLE_VS_CENTROID_MIN:
                return self._fail(e, "Essa frase não combinou com as anteriores. Tenta de novo, só você falando, sem barulho.")
        e.samples.append(emb)
        need = int(self._cfg("voice_id_enroll_samples", 5))
        if len(e.samples) < need:
            e.prompt_i += 1
            return f"{len(e.samples)} de {need}. {SAMPLE_PROMPTS[e.prompt_i % len(SAMPLE_PROMPTS)]}", "feliz"
        return self._finish(e)

    def _finish(self, e: _Enroll) -> REPLY:
        try:
            old = self.store.get(e.redo_of) if e.redo_of else None
            keep = {k: old[k] for k in ("preferences", "settings", "context", "aliases")} if old else {}
            if old:
                self.store.delete(old["user_id"])
            owner_flag = e.is_owner or bool(old and old.get("is_owner"))
            profile = self.store.create(e.name, e.samples, is_owner=owner_flag,
                                        threshold=float(self._cfg("voice_id_threshold", 0.55)))
            if keep:
                profile.update(keep)
                self.store.save(profile)
        except Exception as ex:
            logger.exception(f"Falha ao criar o perfil de voz: {type(ex).__name__}")
            self._enroll = None
            return "Algo deu errado ao salvar o perfil. Nada foi guardado; veja o log.", "bravo"
        self._enroll = None
        return (f"Pronto, {e.name}. Guardei a sua voz com {len(e.samples)} amostras. "
                "Se quiser apagar, é só dizer: apaga minha voz."), "feliz"
