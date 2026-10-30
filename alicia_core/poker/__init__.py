"""
Minigame de pôquer (Five-Card Draw, cabeça a cabeça) da Alicia.

Camadas independentes (as regras funcionam e são testadas sem abrir janela):
  cards.py       baralho de 52 cartas e embaralhamento (secrets.SystemRandom)
  evaluator.py   classificação oficial das mãos, desempates e descrições em português
  engine.py      mão, apostas, fichas, pote, all-in, troca de cartas e showdown; partida (1000 fichas cada)
  ai.py          estratégia da Alicia (só enxerga o que uma pessoa honesta enxergaria)
  reactions.py   falas e expressões, acionadas por eventos reais do jogo
  controller.py  fluxo da partida entre engine, IA, reações e interface
A janela fica em alicia_core/ui/poker_window.py.
"""
