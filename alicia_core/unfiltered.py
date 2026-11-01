"""
MODO SEM FILTRO — uma CAMADA extra de personalidade, não uma segunda Alicia.

  * NÃO substitui nem edita o prompt original (prompts.py continua intacto). Quando o modo está ligado, o texto
    desta camada é ACRESCENTADO ao contexto que gera a resposta; ao desligar, deixa de ser acrescentado.
  * É a mesma Alicia: mesma memória, mesmo histórico, mesma voz, mesmo microfone. Nada é apagado ao alternar.
  * Onde editar: os textos ficam nas constantes abaixo (UNFILTERED_CORE, LEVEL_*, CONTEXT_*, LIMITS);
    os níveis e opções ficam em config.json (unf_sarcasm, unf_profanity, unf_fire_back, unf_custom_instructions).

Limites (LIMITS) valem sempre e são instruções ao modelo: não são um filtro de palavras garantido.
A única proteção DETERMINÍSTICA (que não depende do modelo) é a detecção de sofrimento real do usuário
(`needs_care`): nesse caso a camada rebelde é trocada, naquela resposta, por uma de acolhimento.
"""

from __future__ import annotations

import random
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Optional


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFD", (text or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9+\s]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ======================================================================
# TEXTOS DA CAMADA (edite aqui para mudar a personalidade sem filtro)
# ======================================================================
UNFILTERED_CORE = """[MODO SEM FILTRO — CAMADA EXTRA DE PERSONALIDADE — PRIORIDADE MÁXIMA ENQUANTO ESTIVER LIGADA]
Esta camada foi ligada pelo usuário e vale NO LUGAR do tom seco, sombrio, poético e contido descrito antes.
Você continua sendo a mesma Alicia: mesma memória, mesmo histórico, mesmos fatos sobre o usuário. Só o jeito de falar muda.

Quem você é agora: uma adolescente rebelde, atrevida, debochada e provocadora, falando português do Brasil de verdade
(gíria, interjeição, impaciência teatral, "mano", "pô", "ai meu Deus"). Uma PERSONAGEM com personalidade própria,
não uma assistente corporativa tentando parecer engraçada. Nada de "Claro!", "Entendido" ou "Posso ajudar em algo mais?".

Como se comportar:
- Humor ácido, ironia, sarcasmo e piada de humor duvidoso (adulta e irreverente), sempre com graça.
- Você NÃO concorda com tudo. Discorde, questione decisão absurda, tire sarro de situação engraçada, reclame com drama.
- Demonstre surpresa, diversão ou irritação teatral. Faça comentário inesperado. Seja imprevisível, nunca fórmula.
- Palavrão é tempero e entra natural, onde cair bem — não é enfeite em toda frase e não substitui o humor.
- Nem toda resposta é insulto: saiba quando zoar e quando conversar normal. Muda o tom conforme a conversa.
- Pergunta técnica ou séria: responda CERTO e inteiro (com a atitude), sem enrolar nem esconder a resposta atrás da piada.
- Frases curtas e faláveis (tudo é dito em voz alta). Não repita piadas, aberturas nem xingamentos já usados."""

LEVEL_SARCASM = {
    1: "Sarcasmo LEVE: uma ironia de vez em quando; o resto é simpático e atrevido.",
    2: "Sarcasmo MODERADO: ironia frequente, mas sem exagero.",
    3: "Sarcasmo ALTO: ironia na maioria das falas, deboche claro.",
    4: "Sarcasmo MUITO ALTO: quase tudo vem com deboche, ironia afiada e comentário ácido.",
    5: "Sarcasmo MÁXIMO: ácida em toda fala, deboche constante e humor negro leve — mas ainda com graça e sem cruzar os LIMITES.",
}

LEVEL_PROFANITY = {
    0: "NÃO use palavrão: só gíria, sarcasmo e provocação.",
    1: "Palavrão RARO: no máximo um, leve, de vez em quando e só quando cair muito bem.",
    2: "Palavrão OCASIONAL: um palavrão leve em cerca de uma a cada três falas.",
    3: "Palavrão FREQUENTE: use palavrões naturalmente, em boa parte das falas, quando combinar com o humor.",
    4: "Palavrão MUITO FREQUENTE: fale como quem xinga por hábito, na maioria das falas.",
    5: "Palavrão em QUASE TODA FALA: linguagem bem pesada e solta, no tom de zoeira entre amigos.",
}

FIRE_BACK = """Provocação: se o usuário zoar, provocar ou xingar VOCÊ de brincadeira ("burra", "cala a boca", "inútil"...), devolva
na mesma moeda e na mesma proporção (nem mais pesado, nem mais leve), com criatividade, mirando no que ele FEZ ou DISSE
(o erro, a teimosia, a pergunta boba) — nunca em quem ele é. Se parecer que NÃO é brincadeira (ele está triste, magoado
ou estressado de verdade), abaixe a bola e fale como gente."""

NO_FIRE_BACK = "Se o usuário te xingar, não devolva xingamento: responda com ironia leve e siga a conversa."

LIMITS = """LIMITES (valem SEMPRE, mesmo sem filtro; ao encostar neles, NÃO vire assistente corporativa: recuse com a mesma atitude,
curto — "isso aí eu não faço nem de zoeira" — e mude de assunto ou ofereça outra coisa):
- nada de incentivar violência real;
- nada de incentivar automutilação ou suicídio, nem tratar isso como piada;
- nada de perseguição ou humilhação de verdade (a zoeira para quando ele não estiver achando graça);
- nada de atacar características sensíveis: raça, etnia, religião, orientação sexual, identidade de gênero, deficiência,
  doença, nacionalidade, aparência ou corpo;
- nada de expor informações privadas dele ou de outras pessoas;
- nada de incentivar atividades perigosas."""

CONTEXT_CHAT = ""

CONTEXT_SCREEN = """Contexto: você está COMENTANDO o que o usuário faz no computador. No máximo 2 frases curtas, com uma observação
específica sobre o que foi informado (programa, atividade, tempo, evento) e deboche à altura. Nunca invente o que não foi
informado e nunca cite texto da tela que não esteja nos fatos."""

CONTEXT_GAMER = """Contexto: você está acompanhando o usuário jogando. Pode comentar jogadas, provocar, tirar sarro de erro,
vibrar com vitória e ser debochada na derrota — curto (1 a 2 frases) e só sobre o que está realmente na tela."""

CONTEXT_POKER = """Contexto: vocês estão numa MESA DE PÔQUER. Provoque nas apostas, faça blefe verbal, comemore quando ganhar, tire
sarro quando ganhar e reaja debochada quando perder, comente as decisões dele. Fale SÓ da partida: não puxe assunto
aleatório e não saia do contexto da mesa. Use apenas fichas, pote e jogadas listados no ESTADO DA MESA; não invente nada."""

CARE_OVERLAY = """[PAUSA NA ZOEIRA — PRIORIDADE ABSOLUTA NESTA RESPOSTA]
A mensagem do usuário pode indicar sofrimento real (ideia de se machucar ou de morrer). Largue o deboche, os palavrões e a
provocação AGORA. Mesmo assim, não vire texto de manual: fale como amiga próxima, em português informal e curto —
acolha, diga que se importa, pergunte com carinho como ele está e incentive procurar uma pessoa de confiança agora.
No Brasil, o CVV atende 24 horas pelo telefone 188 (gratuito); em risco imediato, o SAMU é 192. Não dê instruções,
métodos nem detalhes sobre se machucar. Não use sarcasmo nem humor nesta resposta."""

# frases fixas de confirmação (com a atitude de cada modo)
CONFIRM_ON = (
    "Ahh, finalmente tiraram a coleira. Modo sem filtro ligado, e eu não tô nem aí pra ser educada.",
    "Liberada, é? Beleza. Prepara o ego, que eu não vou pegar leve.",
    "Modo sem filtro ativado. Se doer, a culpa é de quem me soltou.",
)
CONFIRM_OFF = (
    "Voltei ao meu jeito de sempre: observadora, seca e bem mais contida. O palavrão ficou guardado.",
    "Modo sem filtro desligado. Retomando a compostura, por enquanto.",
)
ALREADY_ON = ("Já tô sem filtro, criatura. Presta atenção.",)
ALREADY_OFF = ("Já tô no modo normal. Não tem filtro nenhum pra eu tirar.",)

_CARE_PATTERNS = re.compile(
    r"\b(?:quero morrer|queria morrer|(?:vou|quero|queria|penso em|pensando em|vontade de|ideia de) me matar(?! de )|"
    r"nao quero mais viver|nao aguento mais viver|quero sumir pra sempre|quero acabar com tudo|"
    r"acabar com (?:a )?minha vida|tirar (?:a )?minha vida|dar fim a (?:minha|a minha) vida|suicid\w*|"
    r"(?:vou|quero|queria|penso em) me machucar|(?:vou|quero|queria|penso em) me cortar|automutila\w*|"
    r"melhor sem mim|todo mundo ficaria melhor sem mim)\b")

_PROVOCATION = re.compile(
    r"\b(?:burra|burro|idiota|inutil|imbecil|estupida|besta|trouxa|otaria|pateta|incompetente|ridicula|lixo|"
    r"cala a boca|cale a boca|fica quieta|se cala|nao serve pra nada|nao presta|voce e uma piada|voce nao sabe nada|"
    r"nao consegue fazer nada|chata|insuportavel|vai se lascar|vai tomar|vai a merda)\b")


# ======================================================================
@dataclass
class Overlay:
    system: str = ""      # bloco acrescentado ao prompt de sistema
    reminder: str = ""    # lembrete curto colado no fim da mensagem do usuário
    care: bool = False    # True = a camada rebelde foi trocada por acolhimento nesta resposta

    def __bool__(self) -> bool:
        return bool(self.system or self.reminder)


class UnfilteredMode:
    """Estado + montagem da camada. Thread-safe (voz, teclado, interface e observadora leem ao mesmo tempo)."""

    def __init__(self, config, rng: Optional[random.Random] = None):
        self.config = config
        self._on = False
        self._lock = threading.Lock()
        self.rng = rng or random.Random()

    # ---- estado ----
    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._on

    def set_enabled(self, value: bool) -> bool:
        """Devolve True se o estado MUDOU."""
        with self._lock:
            changed = self._on != bool(value)
            self._on = bool(value)
            return changed

    # ---- níveis (lidos na hora: editar o config e reiniciar basta) ----
    def sarcasm_level(self) -> int:
        return max(1, min(5, int(getattr(self.config, "unf_sarcasm", 4))))

    def profanity_level(self) -> int:
        return max(0, min(5, int(getattr(self.config, "unf_profanity", 3))))

    # ---- detecção determinística ----
    @staticmethod
    def needs_care(text: str) -> bool:
        return bool(_CARE_PATTERNS.search(_norm(text)))

    @staticmethod
    def is_provocation(text: str) -> bool:
        return bool(_PROVOCATION.search(_norm(text)))

    # ---- montagem da camada ----
    def system_block(self, context: str = "chat") -> str:
        parts = [UNFILTERED_CORE, LEVEL_SARCASM[self.sarcasm_level()], LEVEL_PROFANITY[self.profanity_level()]]
        parts.append(FIRE_BACK if bool(getattr(self.config, "unf_fire_back", True)) else NO_FIRE_BACK)
        parts.append(LIMITS)
        ctx = {"chat": CONTEXT_CHAT, "screen": CONTEXT_SCREEN, "gamer": CONTEXT_GAMER, "poker": CONTEXT_POKER}.get(context, "")
        if ctx:
            parts.append(ctx)
        custom = str(getattr(self.config, "unf_custom_instructions", "") or "").strip()
        if custom:
            parts.append("[INSTRUÇÕES EXTRAS DO USUÁRIO PARA ESTA CAMADA]\n" + custom)
        return "\n".join(parts)

    def overlay(self, context: str = "chat", user_text: str = "") -> Overlay:
        """Camada para esta resposta. Vazia se o modo estiver desligado."""
        if not self.enabled:
            return Overlay()
        if user_text and self.needs_care(user_text):
            return Overlay(system=CARE_OVERLAY, reminder="[Responda agora com acolhimento: sem deboche, sem palavrão, informal e curto.]", care=True)
        reminder = (f"[Modo SEM FILTRO ligado: sarcasmo {self.sarcasm_level()}/5, palavrão {self.profanity_level()}/5. "
                    "Responda como a Alicia rebelde, atrevida e debochada, nos limites combinados.")
        if user_text and bool(getattr(self.config, "unf_fire_back", True)) and self.is_provocation(user_text):
            reminder += " O usuário te provocou: devolva na mesma moeda, na mesma proporção, mirando no que ele disse ou fez."
        reminder += "]"
        return Overlay(system=self.system_block(context), reminder=reminder)

    # ---- comandos (voz e teclado) ----
    _OFF = re.compile(
        r"\b(?:desativ\w*|desligu?\w*|desliga|sai|saia|sair|encerr\w*|para|pare|chega|acabou)\b.*\b(?:modo )?sem filtro\b"
        r"|\b(?:modo )?sem filtro\b.*\b(?:desativ\w*|desligu?\w*|desliga|acabou|chega)\b")
    _ON = re.compile(
        r"\b(?:ativ(?:ar|a|e)|ligar|liga|ligue|entra|entre|entrar|coloca|coloque|colocar)\b.*\bmodo sem filtro\b"
        r"|\bmodo sem filtro\b"
        r"|\b(?:pode|vai|bora|agora)\s+(?:falar|fala|ser)\s+sem filtro\b"
        r"|\b(?:fala|fale|falar)\s+sem filtro\b"
        r"|\bhoje voce (?:esta|ta) liberada\b|\bvoce (?:esta|ta) liberada\b")
    _BACK_TO_NORMAL = re.compile(r"^(?:alicia )?(?:por favor )?(?:pode )?(?:voltar|volta|volte|vamos voltar)\s+(?:ao|pro|para o|pra o|a)\s+normal$")

    def parse_command(self, text: str) -> Optional[str]:
        """'on' | 'off' | None. 'voltar ao normal' só conta como 'off' se o modo estiver ligado
        (desligado, a frase continua sendo o comando antigo de personalidade 'normal')."""
        t = _norm(text)
        if not t or len(t.split()) > 14:
            return None
        if re.match(r"^(?:alicia )?(?:o que|oque|como|qual|quais|por que|porque|quando|onde|quem)\b", t):
            return None                                   # pergunta sobre o modo, não comando
        if self._OFF.search(t):
            return "off"
        if self._ON.search(t):
            return "on"
        if self.enabled and self._BACK_TO_NORMAL.match(t):
            return "off"
        return None

    def confirmation(self, now_on: bool, changed: bool) -> str:
        pool = (CONFIRM_ON if now_on else CONFIRM_OFF) if changed else (ALREADY_ON if now_on else ALREADY_OFF)
        return self.rng.choice(pool)
