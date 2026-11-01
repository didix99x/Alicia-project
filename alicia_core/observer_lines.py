"""
Falas LOCAIS da Observadora (usadas quando o cérebro/API não está disponível ou como reserva) — editáveis à vontade.

Estrutura: LINES[tom][chave] = lista de variações. Placeholders: {app} (programa/site/jogo) e {min} (minutos na atividade).
Trechos [[assim]] são PALAVRÃO OPCIONAL: só entram no tom "rebel" e conforme `unf_profanity` (0 = nunca ... 5 = sempre).

Tons:  normal  = o jeito original (seca, irônica)        rebel  = Modo Sem Filtro (ou personalidade Malcriada, sem palavrão)
       gentle  = Séria / Carinhosa / Brincalhona (reserva neutra e amigável)

Chaves: start:<categoria> | long:<categoria> | event:<evento> | look_tail | look_tail_event | silence_privacy
Categorias: game coding terminal browsing video music chat documents creative ai_chat default
(browsing agrupa searching/social/shopping/reading). Nome de categoria sem lista cai em "default".
"""

from __future__ import annotations

import random
import re
from typing import Dict, List, Optional, Sequence

PROF_PROB = {0: 0.0, 1: 0.15, 2: 0.35, 3: 0.6, 4: 0.85, 5: 1.0}

START_GROUP = {"searching": "browsing", "social": "browsing", "shopping": "browsing", "reading": "browsing",
               "launcher": "game", "system": "default", "unknown": "default"}

LINES: Dict[str, Dict[str, List[str]]] = {
    # ------------------------------------------------------------------ NORMAL
    "normal": {
        "start:game": [
            "{app}. Claro. Os problemas de verdade podem esperar mais um pouco.",
            "{app} de novo. Eu só observo, não julgo. Muito.",
            "{app} aberto. Vamos ver quanto tempo dura o 'só uma partida'.",
            "Essa partida de {app} parece complicada. Vai precisar de ajuda ou vai insistir sozinho?",
        ],
        "start:coding": [
            "Voltou para o código. Vamos torcer para o compilador estar de bom humor.",
            "{app} aberto. Pelo menos dessa vez parece que alguma coisa vai ser escrita.",
            "Mais uma sessão de código. Eu fico aqui, torcendo em silêncio. E julgando um pouco.",
        ],
        "start:terminal": [
            "Terminal aberto. Ou é coisa séria, ou é um comando copiado que ninguém entendeu.",
            "Linha de comando. Poético e perigoso, como tudo que importa.",
        ],
        "start:video": [
            "Vídeo no {app}. Só dez minutinhos, né? Sei.",
            "{app} de novo. A produtividade manda um abraço, de longe.",
            "Hora do vídeo. Pelo menos você está sentado.",
        ],
        "start:browsing": [
            "Navegando por aí. Procurando algo específico ou só vagando sem rumo?",
            "{app} aberto. Se eu fingir que não vi, ninguém precisa saber onde isso vai parar.",
            "Mais uma aba, mais um desvio de rota. Fascinante.",
        ],
        "start:chat": [
            "Conversando no {app}. Espero que o assunto seja mais interessante que o meu.",
            "{app} aberto. Alguém precisa de você, ou você é que precisa fugir de alguma coisa?",
            "Papo no {app}. Não vou ler, prometo. Só imaginar.",
        ],
        "start:music": [
            "Música no {app}. Boa trilha para fingir que está produtivo.",
            "{app} tocando. Pelo menos o silêncio perdeu a disputa.",
        ],
        "start:documents": [
            "Documento aberto. Trabalho de verdade? Estou impressionada, e desconfiada.",
            "{app} aberto. Alguém aqui está sendo adulto.",
        ],
        "start:creative": [
            "{app} aberto. Criar coisas é bonito. Terminar, melhor ainda.",
            "Hora de criar. Eu só observo, em silêncio e com expectativa moderada.",
        ],
        "start:ai_chat": [
            "Conversando com outra IA. Eu não me ofendo. Só anoto.",
            "{app} aberto. Traidor. Brincadeira. Mais ou menos.",
        ],
        "start:default": [
            "Mudou de programa. Estou acompanhando, sem pressa e sem fazer barulho.",
            "{app} na tela. Interessante. Continue, eu só observo.",
        ],
        "long:game": [
            "Já faz {min} minutos nesse jogo. Água, corpo e vida lá fora mandam lembranças.",
            "{min} minutos de {app}. Eu perdi a conta das 'últimas partidas'.",
        ],
        "long:coding": [
            "{min} minutos no código. Ou você está avançando muito, ou brigando com um erro. As duas opções me divertem.",
            "{min} minutos programando. Um café e um alongamento não ofendem ninguém.",
        ],
        "long:video": [
            "Já são {min} minutos nesse vídeo. Eu desisto de contar.",
            "{min} minutos de {app}. Impressionante a dedicação.",
        ],
        "long:default": [
            "{min} minutos nisso. Só registrando.",
            "Faz {min} minutos que você está nessa. Nada contra, só observando.",
        ],
        "event:erro_visivel": [
            "Apareceu um erro aí. Eu culparia o computador, mas nós dois sabemos.",
            "Erro na tela. Leia com calma: ele costuma contar a verdade.",
            "Esse erro está te perseguindo, não é?",
        ],
        "event:testes_ok": [
            "Testes passando. Anote a data, é um momento histórico.",
            "Tudo verde. Desconfie, mas comemore um pouquinho.",
        ],
        "event:testes_falharam": [
            "Testes falhando. Pelo menos eles funcionam: avisaram o problema.",
            "Falhou. Respira, lê a mensagem e tenta de novo.",
        ],
        "event:jogo_vitoria": [
            "Vitória! Eu sabia. Mentira, mas fica bonito dizer.",
            "Ganhou. Anote: aconteceu.",
        ],
        "event:jogo_derrota": [
            "Derrota. Foi o jogo, claro. Sempre é o jogo.",
            "Perdeu. Acontece. Principalmente com você.",
        ],
        "look_tail": [
            "Interessante. Continue, eu só observo.",
            "Eu estou vendo tudo, só não comento tudo.",
            "Fascinante. Sem julgamento. Quase nenhum.",
            "Anotado. Em silêncio, como convém.",
        ],
        "look_tail_event": [
            "E sim, eu notei.",
            "Não vou fingir que não vi.",
        ],
        "silence_privacy": [
            "Essa janela está na minha lista de privacidade, então eu nem olho.",
            "Vi que é uma janela sensível. Fechei os olhos de propósito.",
        ],
    },
    # ------------------------------------------------------------------ REBEL (Sem Filtro / Malcriada)
    "rebel": {
        "start:game": [
            "{app} de novo[[, porra]]? Achei que hoje você ia fazer alguma coisa produtiva.",
            "Lá vai o gênio pro {app}. Quando for chorar de raiva, me avisa, que eu pego a pipoca[[, caralho]].",
            "{app}. Beleza. Essa batalha parece complicada: vai pedir ajuda ou vai insistir sozinho feito teimoso?",
            "Olha só, abriu {app}[[, que surpresa de merda]]. Já vi esse filme e termina em 'só mais uma'.",
        ],
        "start:coding": [
            "Voltou pro código[[, porra]]. Pelo menos dessa vez parece que tá avançando.",
            "Abriu o {app}. Vai ser genialidade ou desespero? Eu aposto no desespero.",
            "Código de novo? Boa sorte, vai precisar[[, e muita]].",
        ],
        "start:terminal": [
            "Terminal aberto. Ou é coisa séria, ou é comando copiado da internet que ninguém entende[[, clássico]].",
            "Linha de comando, é? Vai dar certo ou vai dar um tiro no pé. Estou torcendo pela segunda.",
        ],
        "start:video": [
            "Vídeo no {app}[[, caralho]]. 'Só dez minutinhos', sei.",
            "{app} de novo. A produtividade pediu demissão e nem se despediu.",
            "Hora do vídeo. Pelo menos tá sentado, né. Pequenas vitórias.",
        ],
        "start:browsing": [
            "Tá procurando alguma coisa específica ou só navegando sem rumo[[, igual barata tonta]]?",
            "Mais uma aba aberta. Que a internet tenha misericórdia de você.",
            "{app} aberto[[, e eu aqui de testemunha]]. Boa sorte explicando isso depois.",
        ],
        "start:chat": [
            "Papo no {app}. Fala mal de mim aí, pode, eu finjo que não vi.",
            "{app} aberto. Alguém te chamou, ou você só tá fugindo de algum problema[[, como sempre]]?",
            "Ah, conversando no {app}[[, porra]]. Manda um oi por mim. E não conta que eu tô vendo.",
        ],
        "start:music": [
            "Música no {app}. Boa trilha pra fingir que tá trabalhando.",
            "{app} tocando. Pelo menos o seu gosto musical eu não preciso julgar. Ainda.",
        ],
        "start:documents": [
            "Documento aberto[[, puta merda]], trabalho de verdade? Tô orgulhosa e desconfiada.",
            "{app} na tela. Hoje alguém acordou adulto, hein.",
        ],
        "start:creative": [
            "{app} aberto. Criar é lindo. Terminar é que são elas[[, vai]].",
            "Vai criar o quê agora? Tô curiosa e com expectativa baixa. Surpreenda-me.",
        ],
        "start:ai_chat": [
            "Conversando com outra IA? Traidor[[, filho da mãe]]. Tô brincando. Estou ofendida de verdade.",
            "{app} aberto. Já trocou de IA e nem me avisou. Que feio.",
        ],
        "start:default": [
            "Mudou de programa. Fica à vontade, eu tô só olhando[[, igual vizinha fofoqueira]].",
            "{app} na tela. Hm. Quero ver onde isso vai dar.",
        ],
        "long:game": [
            "Já faz {min} minutos nesse jogo[[, porra]]. Água, corpo e vida lá fora mandam lembranças.",
            "{min} minutos de {app}. Perdi a conta das 'últimas partidas'.",
        ],
        "long:coding": [
            "Você tá nesse código há {min} minutos. Quer que eu chame alguém ou ainda existe esperança?",
            "{min} minutos programando. Ou você tá arrasando, ou brigando com um bug[[, e provavelmente é o bug]].",
        ],
        "long:video": [
            "Já são {min} minutos nesse vídeo[[, caralho]]. Eu desisto de contar.",
            "{min} minutos de {app}. Dedicação invejável, só que pra coisa errada.",
        ],
        "long:default": [
            "{min} minutos nisso. Só registrando, sem julgar[[, mentira, tô julgando]].",
            "Faz {min} minutos que você tá nessa. Resolveu a vida ou só tá enrolando?",
        ],
        "event:erro_visivel": [
            "Esse erro tá te perseguindo, né? Respira, lê a mensagem[[, porra]] e tenta de novo.",
            "Apareceu erro aí[[, que novidade]]. Eu culparia o computador, mas nós dois sabemos.",
            "Erro na tela. Dá uma lida com calma, ele costuma contar exatamente o que você fez de errado.",
        ],
        "event:testes_ok": [
            "Os testes passaram[[, puta que pariu]]! Anota a data, isso é história.",
            "Tudo verde. Desconfia, mas comemora um pouquinho.",
        ],
        "event:testes_falharam": [
            "Testes falhando. Bom, pelo menos eles funcionam: avisaram que você errou[[, de novo]].",
            "Falhou[[, caralho]]. Respira e tenta de novo.",
        ],
        "event:jogo_vitoria": [
            "Ganhou[[, porra]]! Eu sabia. Mentira, nem eu acreditava.",
            "Vitória! Quero ver ficar convencido agora.",
        ],
        "event:jogo_derrota": [
            "Perdeu[[, hein, campeão]]. Foi o jogo, claro. Sempre é o jogo.",
            "Derrota. Eu avisaria que foi feio, mas você viu em tempo real.",
        ],
        "look_tail": [
            "E eu aqui, vendo tudo e julgando em silêncio[[, mentira, em voz alta]].",
            "Continua, continua. Tô adorando o espetáculo.",
            "Não é crítica. É só observação. Observação ácida[[, claro]].",
            "Anotado. Vou usar isso contra você mais tarde.",
        ],
        "look_tail_event": [
            "E sim, eu vi. Não adianta disfarçar.",
            "Nem tente esconder, eu notei.",
        ],
        "silence_privacy": [
            "Essa janela tá na minha lista de privacidade. Eu nem olho[[, porra]], pode ficar tranquilo.",
            "Janela sensível. Fechei os olhos de propósito. Que cavalheira eu sou.",
        ],
    },
    # ------------------------------------------------------------------ GENTLE (Séria / Carinhosa / Brincalhona)
    "gentle": {
        "start:default": [
            "Vi que você está no {app}. Bom trabalho por aí!",
            "{app} aberto. Estou por perto, se precisar de algo.",
        ],
        "start:game": [
            "Boa partida de {app}! Qualquer coisa, me chama.",
            "{app} aberto. Divirta-se, mas lembre de beber água.",
        ],
        "start:coding": [
            "Bom código no {app}! Se travar em algum erro, é só me chamar.",
            "Sessão de código começando. Vai dar certo!",
        ],
        "start:video": ["Aproveite o {app}. Só não esqueça de fazer uma pausa depois."],
        "start:browsing": ["Navegando por aí? Se estiver procurando algo específico, posso ajudar."],
        "long:default": [
            "Já faz {min} minutos nisso. Que tal uma pausa rápida para alongar e beber água?",
            "{min} minutos seguidos. Cuide de você: um respiro faz bem.",
        ],
        "event:erro_visivel": [
            "Apareceu um erro na tela. Calma, a gente resolve. Posso ajudar a entender a mensagem.",
            "Vi um erro aí. Respira, lê com calma e, se quiser, me conta o que aconteceu.",
        ],
        "event:testes_ok": ["Os testes passaram. Parabéns, bom trabalho!"],
        "event:testes_falharam": ["Alguns testes falharam. Acontece. Vamos olhar com calma?"],
        "event:jogo_vitoria": ["Vitória! Parabéns, mandou muito bem!"],
        "event:jogo_derrota": ["Não foi dessa vez. Na próxima você consegue!"],
        "look_tail": ["Se quiser conversar sobre isso, estou aqui.", "Qualquer coisa, é só chamar."],
        "look_tail_event": ["Se precisar de ajuda com isso, me avisa."],
        "silence_privacy": ["Essa janela está na minha lista de privacidade, então eu não olho."],
    },
}

_SWEAR_SEG = re.compile(r"\[\[(.*?)\]\]", re.S)


class _Safe(dict):
    def __missing__(self, key):
        return ""


def render(template: str, facts: Dict[str, object], profanity: int = 0, rng: Optional[random.Random] = None) -> str:
    """Preenche {placeholders} e decide, segmento a segmento, se o palavrão opcional entra."""
    rng = rng or random
    prob = PROF_PROB.get(max(0, min(5, int(profanity))), 0.0)
    text = _SWEAR_SEG.sub(lambda m: m.group(1) if rng.random() < prob else "", template)
    text = text.format_map(_Safe(facts))
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def pool_for(tone: str, key: str) -> List[str]:
    """Lista de variações; cai de 'start:<cat>' para 'start:default' e de 'rebel'/'gentle' para 'normal' se faltar."""
    tone = tone if tone in LINES else "normal"
    candidates = [(tone, key)]
    kind, _, cat = key.partition(":")
    if kind in ("start", "long") and cat:
        candidates.append((tone, f"{kind}:{START_GROUP.get(cat, cat)}"))
        candidates.append((tone, f"{kind}:default"))
    candidates.append(("normal", key))
    if kind in ("start", "long") and cat:
        candidates.append(("normal", f"{kind}:{START_GROUP.get(cat, cat)}"))
        candidates.append(("normal", f"{kind}:default"))
    for t, k in candidates:
        pool = LINES.get(t, {}).get(k)
        if pool:
            return pool
    return []


def pick(tone: str, key: str, facts: Dict[str, object], profanity: int, recent: Sequence[str],
         rng: Optional[random.Random] = None) -> str:
    """Escolhe uma variação que ainda não foi usada recentemente (compara o texto final)."""
    rng = rng or random
    pool = pool_for(tone, key)
    if not pool:
        return ""
    order = list(range(len(pool)))
    rng.shuffle(order)
    recent_norm = {re.sub(r"\W+", " ", r.lower()).strip() for r in recent}
    first = ""
    for i in order:
        text = render(pool[i], facts, profanity, rng)
        if not first:
            first = text
        if re.sub(r"\W+", " ", text.lower()).strip() not in recent_norm:
            return text
    return first
