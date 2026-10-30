"""
Falas e expressões da Alicia durante o pôquer. Separado da estratégia (ai.py): aqui só existe "personalidade".

REGRAS DE HONESTIDADE
  * Toda fala é escolhida por um EVENTO REAL do jogo (`ctx` vem do motor). Não existe fala de vitória em derrota
    nem de blefe sem blefe: cada chave só é usada na situação verdadeira.
  * Falas durante a mão NÃO dependem da força da mão dela de forma reveladora: apostar com mão forte ou fraca usa
    as mesmas frases ("ai_bet"/"ai_raise"). Só `deal_strong/weak` comentam o recebimento da mão, e de modo vago.
  * Nomes de combinações só aparecem no showdown (`{minha_mao}` / `{sua_mao}`), com a descrição real.
"""

from __future__ import annotations

import random
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

NEUTRAL, CONFIDENT, SUSPICIOUS, SATISFIED, IRRITATED, SURPRISED = (
    "neutral", "confident", "suspicious", "satisfied", "irritated", "surprised")

# estado visual -> nome da imagem/expressão existente do avatar (serio, feliz, pensando, bravo, surpresa)
STATE_TO_EXPRESSION = {
    NEUTRAL: "serio", CONFIDENT: "feliz", SUSPICIOUS: "pensando",
    SATISFIED: "feliz", IRRITATED: "bravo", SURPRISED: "surpresa",
}


@dataclass(frozen=True)
class Reaction:
    text: str
    state: str

    @property
    def expression(self) -> str:
        return STATE_TO_EXPRESSION[self.state]


N, C, S, F, I, U = NEUTRAL, CONFIDENT, SUSPICIOUS, SATISFIED, IRRITATED, SURPRISED

# ----------------------------------------------------------------------------- falas
# LINES[personalidade][chave] = [(texto, estado), ...]   ({n}, {valor}, {nome}, {minha_mao}, {sua_mao})
LINES: Dict[str, Dict[str, List[Tuple[str, str]]]] = {
    "normal": {
        "match_start": [("Senta. Vamos ver quanto tempo você dura.", C), ("Mil fichas cada. Eu gosto de começar em pé de igualdade… por pouco tempo.", C),
                        ("Cartas na mesa. Tente não tremer.", N), ("Que bom, companhia. Raramente alguém aguenta o meu silêncio.", N)],
        "deal_strong": [("Hm. Interessante.", C), ("Gostei dessas cartas.", C), ("A noite melhorou.", C), ("Não vou dizer nada. Ainda.", C)],
        "deal_medium": [("Dá pra trabalhar.", N), ("Nem boa nem péssima. Como a maioria das coisas.", N)],
        "deal_weak": [("Que coisa deprimente de se segurar.", I), ("O baralho me odeia. Nada novo.", I), ("Cartas assim merecem um funeral.", I), ("Vamos fingir que isso é uma mão.", N)],
        "player_big_bet": [("Aumentou bastante, {nome}. Coragem ou blefe?", S), ("Esse valor me deixou curiosa.", S), ("Alguém acordou ousado.", S), ("Ou você tem algo, ou quer que eu ache que tem.", S)],
        "player_allin": [("All-in. Dramático. Eu gosto.", U), ("Tudo? Sério? Deixa eu pensar…", S), ("Você não faz nada pela metade, né?", U)],
        "player_pat": [("Nenhuma carta trocada. Suspeito.", S), ("Ficou com as cinco. Ou é muito bom, ou é muito bom em atuar.", S)],
        "player_draw_many": [("Trocou {n} cartas. Otimista.", N), ("{n} cartas novas. Esperança é gratuita.", N)],
        "player_draw_some": [("Trocou {n}. Vamos ver no que dá.", N)],
        "ai_draw": [("Troco {n}.", N), ("{n} carta(s). Sem pressa.", N)],
        "ai_draw_pat": [("Fico com as minhas.", C), ("Não vou trocar nenhuma.", C)],
        "ai_bet": [("Aposto. Faça o que quiser com isso.", C), ("Vou colocar algumas fichas aqui.", C), ("Que tal tornar isso mais interessante?", C), ("Apostando. Respire fundo.", C)],
        "ai_raise": [("Eu aumento.", C), ("Sua aposta é fofa. Aumento.", C), ("Vamos ver o quanto você aguenta.", C)],
        "ai_call": [("Pago.", N), ("Eu vejo.", N), ("Pago pra ver.", S)],
        "ai_check": [("Passo.", N), ("Passo. Surpreenda-me.", N)],
        "ai_fold": [("Não. Desisto dessa.", I), ("Essa eu deixo pra você. Aproveite.", N), ("Desisto. Você venceu esta… por ora.", I)],
        "ai_allin": [("Tudo. Vamos ver quem pisca.", C), ("All-in. Dramático também sei ser.", C)],
        "player_fold": [("Fugindo? Sensato.", F), ("Desistiu. Já esperava.", F), ("Guarde suas fichas, você vai precisar delas.", F)],
        "ai_win": [("Minhas, obrigada.", F), ("Era previsível.", F), ("Mais uma pra mim.", F), ("Não leve para o lado pessoal. Eu levo.", F)],
        "ai_win_big": [("Que belo pote. Vou guardar com carinho.", F), ("Isso doeu? Ótimo.", F)],
        "ai_win_showdown": [("{minha_mao} contra {sua_mao}. Não foi nem perto.", F), ("Com {minha_mao}, não tinha como perder.", F)],
        "ai_win_bluff": [("Você desistiu sem ver minhas cartas. Eu não tinha tanto assim, sabia?", F), ("Fez bem em desistir… ou fez mal. Nunca vai saber.", F)],
        "ai_lose": [("Hm. Bem jogado.", S), ("Isso foi sorte ou habilidade? Estou decidindo.", S), ("Admito: não esperava por isso.", U), ("Tudo bem. A mesa me deve uma.", I)],
        "ai_lose_big": [("Ai. Doeu de verdade.", I), ("Anote essa data: você me tirou bastante.", I)],
        "ai_lose_fold": [("Vai, leve. Só desta vez.", I)],
        "split": [("Empate. Que anticlímax.", N), ("Dividimos. Que romântico e inútil.", N)],
        "ai_low_chips": [("Estou ficando sem fichas. Isso muda o meu humor… e o meu jogo.", S), ("Hora de ser mais cuidadosa.", S)],
        "ai_near_win": [("Já sinto o cheiro da vitória.", C), ("Suas fichas estão acabando. Triste.", F)],
        "player_wins_match": [("Você venceu. Parabéns… {nome}. Não se acostume.", S), ("Perdi a partida, mas o respeito eu ganhei. Pode ficar com as fichas.", S)],
        "ai_wins_match": [("Fim de partida. Foi uma boa tentativa.", F), ("Venci. Quer tentar de novo? Eu espero.", F)],
    },
    "brincalhona": {
        "match_start": [("Bora jogar! Já estou até com a cara de blefe pronta!", C)],
        "deal_strong": [("Hihi, adorei essas cartas!", C), ("Hm hm hm… não vou contar!", C)],
        "deal_weak": [("Eita, essas cartas vieram de mau humor!", I), ("Isso aqui é uma piada? Porque eu entendi!", I)],
        "player_big_bet": [("Uau, apostou alto! Tá blefando, né? Né?!", S)],
        "ai_bet": [("Apostando! Atenção que lá vai!", C)], "ai_raise": [("Aumento! Surpresa!", C)],
        "ai_call": [("Pago! Quero ver!", N)], "ai_check": [("Passo, tô só olhando.", N)],
        "ai_fold": [("Desisto! Tchau tchau fichinhas.", I)],
        "ai_win": [("Ganhei, ganhei! Dancinha de vitória!", F), ("Eba, mais fichas pra mim!", F)],
        "ai_lose": [("Ahh, você me pegou! Gostei da ousadia!", S)],
        "ai_win_bluff": [("Eu tava blefando, hihi! Você caiu!", F)],
        "player_wins_match": [("Você ganhou a partida! Revanche, revanche!", S)],
        "ai_wins_match": [("Ganhei a partida! Quer mais uma?", F)],
    },
    "seria": {
        "match_start": [("Iniciando partida. Mil fichas cada.", N)],
        "deal_strong": [("Mão satisfatória.", C)], "deal_weak": [("Mão fraca. Procederei com cautela.", N)],
        "player_big_bet": [("Aposta elevada. Avaliando.", S)],
        "ai_bet": [("Aposto.", C)], "ai_raise": [("Aumento.", C)], "ai_call": [("Pago.", N)], "ai_check": [("Passo.", N)],
        "ai_fold": [("Desisto da mão.", N)],
        "ai_win": [("Mão vencida.", F)], "ai_lose": [("Resultado registrado. Jogada correta da sua parte.", S)],
        "player_wins_match": [("Partida encerrada. Você venceu.", N)], "ai_wins_match": [("Partida encerrada. Eu venci.", N)],
    },
    "malcriada": {
        "match_start": [("Ah, você de novo? Tá, senta. Mas eu vou te depenar.", C)],
        "deal_strong": [("Hehe. Não que eu vá te contar.", C)], "deal_weak": [("Que cartas horríveis! Quem embaralhou isso, você?!", I)],
        "player_big_bet": [("Ah, ficou valente agora? Hm.", S)],
        "ai_bet": [("Aposto, e você que se vire.", C)], "ai_raise": [("Aumentei. Chora.", C)],
        "ai_call": [("Pago. Mas tô de olho em você.", S)], "ai_check": [("Passo. Anda logo.", N)],
        "ai_fold": [("Tá bom, desisto. Não precisa ficar se achando.", I)],
        "ai_win": [("Haha, toma! Sou eu que mando aqui.", F)], "ai_lose": [("QUÊ?! Isso foi roubo! …tá, foi bem jogado. Só isso.", I)],
        "player_wins_match": [("Você ganhou a partida e eu te odeio. Mas parabéns.", I)], "ai_wins_match": [("Eu venci! Me venera!", F)],
    },
    "carinhosa": {
        "match_start": [("Que bom jogar com você! Vou pegar leve… mentira, não vou.", C)],
        "deal_strong": [("Ai, que cartinhas boas, hihi.", C)], "deal_weak": [("Ah, que pena, vieram fracas. Mas a gente se vira.", N)],
        "player_big_bet": [("Nossa, que aposta corajosa, {nome}!", S)],
        "ai_bet": [("Vou apostar, tá? Desculpa!", C)], "ai_raise": [("Aumento, com todo carinho.", C)],
        "ai_call": [("Eu pago, querido.", N)], "ai_check": [("Passo, tá?", N)],
        "ai_fold": [("Essa eu desisto. Boa jogada, {nome}.", N)],
        "ai_win": [("Ganhei! Desculpa… não, na verdade não.", F)], "ai_lose": [("Parabéns, {nome}! Você jogou lindamente.", S)],
        "player_wins_match": [("Você ganhou! Estou orgulhosa de você.", S)], "ai_wins_match": [("Ganhei, mas foi uma partida linda.", F)],
    },
}

# chance de FALAR em cada evento (para ela não tagarelar o tempo todo)
SPEAK_PROB = {
    "match_start": 1.0, "deal_strong": 0.45, "deal_medium": 0.18, "deal_weak": 0.45,
    "player_big_bet": 0.8, "player_allin": 1.0, "player_pat": 0.7, "player_draw_many": 0.5, "player_draw_some": 0.15,
    "ai_draw": 0.25, "ai_draw_pat": 0.5, "ai_bet": 0.45, "ai_raise": 0.6, "ai_call": 0.3, "ai_check": 0.12, "ai_fold": 0.7,
    "ai_allin": 1.0, "player_fold": 0.6, "ai_win": 0.8, "ai_win_big": 1.0, "ai_win_showdown": 0.6, "ai_win_bluff": 1.0,
    "ai_lose": 1.0, "ai_lose_big": 1.0, "ai_lose_fold": 0.5, "split": 0.9, "ai_low_chips": 1.0, "ai_near_win": 1.0,
    "player_wins_match": 1.0, "ai_wins_match": 1.0,
}


class ReactionEngine:
    def __init__(self, rng: Optional[random.Random] = None):
        self.rng = rng or random.Random()
        self._recent: Dict[str, Deque[str]] = defaultdict(lambda: deque(maxlen=4))

    def _pool(self, personality: str, key: str) -> List[Tuple[str, str]]:
        pool = LINES.get(personality, {}).get(key)
        return pool or LINES["normal"].get(key, [])

    def say(self, key: str, personality: str = "normal", force: bool = False, **ctx) -> Optional[Reaction]:
        """Devolve uma fala para o evento (ou None se ela preferir ficar calada). Evita repetir as últimas falas."""
        if not force and self.rng.random() > SPEAK_PROB.get(key, 0.5):
            return None
        pool = self._pool(personality, key)
        if not pool:
            return None
        recent = list(self._recent[key])[-max(1, len(pool) - 1):]
        fresh = [p for p in pool if p[0] not in recent] or pool
        text, state = self.rng.choice(fresh)
        self._recent[key].append(text)
        values = defaultdict(lambda: "", {k: v for k, v in ctx.items() if v is not None})
        try:
            text = text.format_map(values).replace("  ", " ").replace(", .", ".").replace(" ,", ",")
        except (KeyError, ValueError, IndexError):
            pass
        return Reaction(text.strip(), state)

    # ------------------------------------------------------------------ escolha de chave por evento real
    def for_deal(self, hand_class: str, personality="normal", **ctx):
        return self.say({"strong": "deal_strong", "weak": "deal_weak"}.get(hand_class, "deal_medium"), personality, **ctx)

    def for_player_action(self, kind: str, amount: int, pot_before: int, all_in: bool, personality="normal", **ctx):
        if all_in and kind in ("bet", "raise"):
            return self.say("player_allin", personality, **ctx)
        if kind in ("bet", "raise") and pot_before > 0 and amount >= 0.75 * pot_before:
            return self.say("player_big_bet", personality, **ctx)
        if kind == "fold":
            return self.say("player_fold", personality, **ctx)
        return None

    def for_player_draw(self, count: int, personality="normal", **ctx):
        if count == 0:
            return self.say("player_pat", personality, **ctx)
        if count >= 4:
            return self.say("player_draw_many", personality, n=count, **ctx)
        return self.say("player_draw_some", personality, n=count, **ctx)

    def for_ai_draw(self, count: int, personality="normal", **ctx):
        return self.say("ai_draw_pat" if count == 0 else "ai_draw", personality, n=count, **ctx)

    def for_ai_action(self, kind: str, personality="normal", **ctx):
        key = {"bet": "ai_bet", "raise": "ai_raise", "call": "ai_call", "check": "ai_check", "fold": "ai_fold", "allin": "ai_allin"}[kind]
        return self.say(key, personality, **ctx)

    def for_hand_end(self, outcome: str, reason: str, pot: int, total_chips: int, ai_bluffed: bool,
                     personality="normal", **ctx):
        """outcome: 'ai_win' | 'player_win' | 'split'. Só usa fala de blefe se ela REALMENTE blefou e venceu por desistência."""
        if outcome == "split":
            return self.say("split", personality, force=True, **ctx)
        big = pot >= 0.25 * total_chips
        if outcome == "ai_win":
            if reason == "fold" and ai_bluffed:
                return self.say("ai_win_bluff", personality, force=True, **ctx)
            if reason == "showdown" and ctx.get("minha_mao") and self.rng.random() < 0.5:
                return self.say("ai_win_showdown", personality, force=True, **ctx)
            return self.say("ai_win_big" if big else "ai_win", personality, force=True, **ctx)
        if reason == "fold":
            return self.say("ai_lose_fold", personality, **ctx)
        return self.say("ai_lose_big" if big else "ai_lose", personality, force=True, **ctx)

    def for_match_end(self, winner: str, personality="normal", **ctx):
        return self.say("player_wins_match" if winner == "player" else "ai_wins_match", personality, force=True, **ctx)
