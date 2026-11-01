# Alicia — assistente pessoal (cérebro via API NVIDIA)

## Primeira vez (Windows 10/11, 64 bits)
1. **Crie uma chave NOVA** em https://build.nvidia.com (Settings → API Keys). Uma chave colada em conversa deve ser considerada exposta: **revogue-a** no painel.
2. Dê dois cliques em **`Iniciar_Alicia.bat`**. Você não precisa saber nada de Python nem abrir terminal. Ele:
   - verifica o seu computador **sem alterar nada**;
   - se estiver tudo certo, segue direto; se faltar algo, **explica em linguagem simples e pergunta** o que fazer (veja *Instalação automática*);
   - cria o `.env` e pede a chave (a digitação fica oculta);
   - valida a chave e o modelo na API da NVIDIA e roda o diagnóstico;
   - abre a Alicia e **pergunta como você quer ser chamado** (salvo para sempre no banco).
   Logo depois do nome ela pergunta a **personalidade** (1 Normal, 2 Brincalhona, 3 Séria, 4 Malcriada, 5 Carinhosa).
3. Converse **na janela preta** (teclado) ou pelo microfone. O avatar é só a parte visual; não feche a janela preta.

**Sem Python?** O `.bat` explica o que é, pede a sua autorização e só então baixa o instalador oficial do python.org (confere a assinatura digital da Python Software Foundation,
abre o instalador com barra de progresso visível, instala só para o seu usuário, sem pedir administrador e sem mexer no PATH).

## Instalação automática (como o iniciador funciona)
A cada execução ele confere: Python (versão e 64 bits), ambiente `.venv` (existe? funciona?), **cada biblioteca de `requirements.txt` e `requirements-optional.txt` é realmente importada**
(listar não basta), interface gráfica, áudio, reconhecimento e síntese de voz, captura de tela, imagens, OCR, arquivos essenciais e `config.json`.
Se algo faltar ou estiver quebrado, aparece:

```
Alicia identificou alguns componentes necessários que ainda não estão instalados no seu computador.
Deseja continuar?
  1. Instalar todos os componentes necessários.
  2. Instalar somente os componentes obrigatórios.
  3. Visualizar detalhes dos componentes ausentes.
  4. Continuar sem instalar, quando possível.
  5. Cancelar inicialização.
```
- **Nada é baixado nem alterado antes da sua resposta.** Tudo vai para dentro da pasta `.venv` da Alicia (nunca no Python do sistema), de fontes oficiais (PyPI / python.org).
- Só instala **o que falta** ou o que está quebrado/desatualizado; não reinstala nem atualiza o que já funciona.
- Mostra o progresso (`[2/5] Instalando PyQt6 ...`), avisa `[OK]` por componente e, no fim, **confere tudo de novo**.
- Falhou? Explica em português simples (sem internet, sem espaço, sem versão compatível...) e oferece tentar de novo. **Sem internet:** diz quais componentes não foram baixados e você tenta depois.
- **Componentes opcionais** (voz, microfone, OCR, captura rápida...) nunca impedem a Alicia de abrir. Se você escolher a opção 4, ele lembra e não pergunta de novo até algo mudar.
- Tudo é registrado (sem conversas, telas, chave ou dados pessoais) em `logs\instalacao_alicia.log`.

## Diagnóstico e reparo
`Diagnostico_Alicia.bat` usa **exatamente os mesmos critérios** do iniciador (módulo `alicia_core/envcheck.py`) e faz testes reais:
```
[OK]    Interface gráfica          [OK]    Sistema de áudio        [OK]    Reconhecimento de voz
[OK]    Síntese de voz             [OK]    Captura de tela         [ERRO]  OCR (leitura de texto da tela)
[AVISO] Componente opcional ausente: ...
Resumo: componentes funcionando / ausentes / com falha / opcionais indisponíveis
```
Depois do relatório ele oferece corrigir. Outras opções: `Iniciar_Alicia.bat --repair` (verifica e repara, sem abrir a Alicia), `--reinstall` (pede confirmação e reinstala tudo).
- **`.venv` corrompida ou ausente:** é recriada. Só a pasta `.venv` é removida: memória, banco, `config.json`, históricos, imagens, animações e perfis **não são tocados**.
- **`config.json` inválido ou com valores errados:** é corrigido só com a sua autorização, **sempre com cópia** (`config.json.bak-AAAAMMDD-HHMMSS`).
- **Arquivos do programa faltando:** baixe a Alicia de novo (seus dados não são afetados).

## OCR (leitura de texto da tela)
Usado pela Observadora e por "Alicia, olha". A escolha preferida é o **RapidOCR** (`rapidocr-onnxruntime`, só pip). O diagnóstico confere se o RapidOCR e o ONNX Runtime
importam, se o motor inicia e se ele lê um texto de teste desenhado na memória (nunca sua tela); a captura de tela também é testada só em memória e descartada.
Se o RapidOCR não funcionar no seu PC (por exemplo, Python muito novo), o iniciador **oferece**, com autorização, o **Tesseract** (programa externo, instalador visível) + `pytesseract` como alternativa;
os dois nunca são instalados juntos sem necessidade. Sem nenhum OCR a Alicia abre normalmente e observa só a janela ativa. Recomendado: **Python 3.12 de 64 bits**.

## Solução de problemas comuns
| Sintoma | O que fazer |
|---|---|
| "Sem conexão com a internet" | conecte-se e rode `Iniciar_Alicia.bat --repair` |
| Interface (PyQt6) não instala | use Python 3.11/3.12 **de 64 bits** |
| Microfone não aparece | *Configurações do Windows → Privacidade → Microfone* e permita o acesso |
| OCR com erro | `Diagnostico_Alicia.bat` mostra o motivo; `--repair` tenta corrigir |
| Algo estranho no ambiente | `Iniciar_Alicia.bat --repair` (não apaga seus dados) |
| Precisa pedir ajuda | envie `logs\instalacao_alicia.log` (sem dados pessoais) |

## Próximas vezes
Dois cliques em `Iniciar_Alicia.bat`. Se tudo estiver certo, abre direto; só pergunta se algo faltar. Não pede nome nem chave de novo.

## Chave e configurações (`.env`)
```
NVIDIA_API_KEY=nvapi-...        # nunca compartilhe; o .env é ignorado pelo Git
NVIDIA_MODEL=nvidia/nemotron-3-ultra-550b-a55b
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
```
Trocar a chave: `Iniciar_Alicia.bat --set-key`. Se o modelo não existir na sua conta, o iniciador lista alternativas.
Outros ajustes (timeouts, tamanho do histórico, resposta máxima, voz, microfone, visão) ficam em `config.json`.

## Comandos na conversa
| Diga | Efeito |
|---|---|
| `me chama de Ana` / `meu nome é Ana` / `/nome Ana` | troca o nome (salvo no banco) |
| `lembre que meu projeto atual é X` | guarda uma memória (com data e origem) |
| `esqueça X` | apaga memórias que combinem |
| `o que você lembra de mim?` / `/memorias` | lista o que está guardado |
| `seja carinhosa` / `fica séria` / `/personalidade malcriada` / `/personalidade` | troca o estilo na hora (também: botão direito no avatar → 🎭 Personalidade) |
| `/novoassunto` | começa um assunto limpo (nada é apagado; ainda dá para ela recordar o antigo) |
| `/backup` / `/exportar` | copia a memória / gera um .txt da conversa e das memórias (`backups\`, `exportacoes\`) |
| `/ajuda` | lista os comandos |
| `/descanso`, `/acordar`, `/gamer`, `sair` | modos e saída (como antes) |
| `Alicia, olha a tela` | olha a tela agora e diz o que está vendo (visão local) |
| `Alicia, observar minha tela` / `pare de observar` | liga/desliga a **Modo Observadora** (ou `/observar`, ou o botão 👁) |
| `Alicia, o que estou fazendo?` | responde com o que viu na tela e há quanto tempo |
| `Alicia, ativar modo sem filtro` / `voltar ao normal` | liga/desliga o **Modo Sem Filtro** (ou `/semfiltro`, ou o botão 🔥) |

## Personalidades
Normal (o jeito original), Brincalhona, Séria, Malcriada (de brincadeira, nunca ofende de verdade) e Carinhosa. A escolha fica salva no banco e pode mudar a qualquer momento: só o *tom* muda, o histórico, as memórias e o assunto em andamento continuam iguais.

## Memória (3 camadas)
- **Histórico** (`conversation_history`): cada fala, uma única vez, com data, sessão e turno.
- **Memória persistente** (`memories`, `user_profile`): só o que você pediu para guardar + o nome. Nada inferido.
- **Contexto do turno**: últimas N mensagens (config `brain_history_messages`) + memórias e trechos antigos *relevantes*.
Observações de tela ficam separadas e nunca entram no chat. Erros da API nunca são gravados como resposta da Alicia.
Seus dados antigos são preservados (migração só adiciona colunas).

## Backup
A cada início (no máximo 1x por dia) a Alicia copia `alicia_memory.db` para `backups\`, mantendo as 5 últimas. Para restaurar, feche a Alicia e substitua `alicia_memory.db` por uma dessas cópias.

## Teste completo (use isto quando algo não funcionar)
`Teste_Completo_Alicia.bat` roda de verdade, um por um: ambiente, módulos, banco, API do modelo, conversa de ponta a ponta,
comandos "Alicia, olha/ver/olhar", captura de tela, visão, voz (você deve ouvir "Teste de voz da Alicia"), microfone
(fale quando pedir) e interface. Grava **`relatorio_alicia.txt`** (sem a chave e sem o conteúdo da sua tela): envie esse arquivo.
Variações: `Teste_Completo_Alicia.bat --sem-audio` (não toca nem grava) e `--rapido` (pula voz, microfone e visão).

## Visão (olhar a tela)
"Alicia, olha" / "ver" / "olhar" (também "Alícia", com acento, e "olha minha tela") analisa a tela na hora, mesmo com a observadora desligada.
**A análise é local:** janela ativa + captura + OCR + classificação (veja *Modo Observadora*). Um modelo de visão da NVIDIA fica só como **reserva**, usado apenas se a leitura local não entender nada **e** você autorizou o envio de imagens (`obs_allow_remote_vision`, desligado por padrão).

## Modo Observadora
Ela acompanha o que você faz no computador e comenta com contexto, tudo local. Nasce **desligada** e só liga com ativação explícita:
botão **👁 Observadora** na janela (ou botão direito), voz ("Alicia, observar minha tela", "comece a observar") ou `/observar`.
Desligar: botão, "Alicia, pare de observar" ou `/observar`. Um selo vermelho **● OBSERVANDO A TELA** fica visível enquanto ela estiver ligada.

**Como funciona (cada ciclo, a cada `obs_interval` s):** lê a janela/processo ativo → se estiver na lista de privacidade, **nem captura** →
captura o monitor da janela ativa → compara com o quadro anterior (quanto mudou) → OCR (no máximo a cada `obs_ocr_every` s) → descarta texto sensível
(cartão válido, CPF, "senha:", chaves de API) → classifica a atividade (jogo, código, terminal, vídeo, navegação, chat...) → atualiza o **contexto visual
temporário** (só RAM, apagado ao desligar; jogo → menu → jogo conta como a mesma atividade) → decide se fala.
Ela só fala com motivo: atividade nova que se firmou (`obs_settle_seconds`), tempo longo na mesma coisa (`obs_long_activity_minutes`) ou evento na tela
(erro, testes, vitória/derrota — o mesmo erro não é comentado duas vezes). Fica quieta enquanto você conversa, enquanto ela fala, no pôquer e no descanso,
e respeita cooldown e limite por hora. Comentário que ficou velho esperando a vez é descartado.

**Privacidade:** nenhuma imagem é gravada em disco nem enviada a serviço externo; o banco não recebe nada da observadora; a lista `obs_ignored_apps`/`obs_ignored_titles`
(senhas, bancos, navegação anônima...) é editável. Se o cérebro (NVIDIA) escreve o comentário, recebe **só fatos abstratos** (categoria, nome do programa, minutos, tipo de evento),
nunca título de janela, texto da tela ou imagem (`obs_use_brain_for_comments: false` mantém tudo local, com as falas de `observer_lines.py`).
Atenção: a **voz** usa o Edge TTS (serviço da Microsoft), então o texto FALADO sai do PC como qualquer fala da Alicia.

**OCR (opcional):** `pip install rapidocr-onnxruntime` (só pip, recomendado) ou Tesseract + `pytesseract`. Sem OCR ela continua funcionando só com janela ativa e imagem.

## Modo Sem Filtro
Uma **camada extra de personalidade** (rebelde, debochada, ácida, com palavrão e capacidade de devolver provocação), não uma segunda Alicia: mesma memória, voz, microfone e
histórico. Ao ligar, o texto da camada é acrescentado ao contexto da resposta; ao desligar, deixa de ser. A personalidade original e a escolhida (normal, carinhosa...) nunca são alteradas.
Liga/desliga por botão **🔥**, voz ("ativar modo sem filtro", "pode falar sem filtro", "hoje você está liberada"; "voltar ao normal" / "desativar modo sem filtro") ou `/semfiltro`.
Funciona junto com a Observadora, no Modo Gamer e no pôquer (lá ela provoca nas apostas, mas só fala da mesa). Sempre começa desligado (`unf_remember_state` muda isso).
Limites que ficam sempre (instruções ao modelo, não um filtro garantido): sem incentivo a violência, automutilação/suicídio, perseguição, ataques a características sensíveis,
exposição de dados privados ou atividades perigosas. Se você falar de sofrimento real, ela troca a zoeira por acolhimento naquela resposta (essa detecção é por regras, não depende do modelo).

## Como editar essas funções
- **Todas as opções** ficam em `config.json` (gerado na primeira execução; feche a Alicia antes de editar): `obs_*` (observadora) e `unf_*` (sem filtro). As principais:
  `obs_interval`, `obs_comment_cooldown`, `obs_event_cooldown`, `obs_max_comments_per_hour`, `obs_talkativeness`, `obs_change_threshold`, `obs_change_sensitivity`,
  `obs_ignored_apps`, `obs_ignored_titles`, `obs_ocr`, `obs_ocr_every`, `obs_monitor`, `unf_sarcasm` (1–5), `unf_profanity` (0–5), `unf_fire_back`, `unf_custom_instructions`.
- **Personalidade normal:** `BASE_PERSONALITY` e `PERSONALITIES` em `alicia_core/prompts.py`. **Personalidade sem filtro:** textos em `alicia_core/unfiltered.py`
  (`UNFILTERED_CORE`, `LEVEL_SARCASM`, `LEVEL_PROFANITY`, `LIMITS`, `CONTEXT_*`, `CONFIRM_*`).
- **Falas locais da observadora:** `alicia_core/observer_lines.py` (trechos `[[assim]]` são palavrão opcional, controlado por `unf_profanity`).
- **Ensinar um programa/site novo:** `PROCESS_MAP` e `SITE_HINTS` em `alicia_core/local_vision.py`. **Eventos de tela:** `ERROR_PATTERNS`, `GAME_WIN`, `GAME_LOSE` no mesmo arquivo.
- Arquivos: `local_vision.py` (visão), `observer.py` (modo, contexto, política), `unfiltered.py` (camada). Testes: `tests/test_observer.py` e `tests/test_unfiltered.py`.

## Logs e diagnóstico
- Logs: `logs\alicia_system.log` (chaves são mascaradas). Nível: `ALICIA_LOG_LEVEL=DEBUG` no `.env`.
- Diagnóstico completo: `Diagnostico_Alicia.bat` (ambiente, bibliotecas, banco, chave sem revelá-la, API, modelo, interface, áudio, tela, OCR).
- Instalação/reparo: `logs\\instalacao_alicia.log`. Reinstalar tudo: `Iniciar_Alicia.bat --reinstall`.

## Testes
`python -m unittest discover -s tests -t .` (usa um servidor local falso; não prova a API real — para isso use o diagnóstico).

## Captura de voz (como funciona e como ajustar)
O microfone é lido **sem parar**, e a Alicia guarda os 1,6 s de áudio anteriores ao momento em que detecta a fala, então o começo
da frase não é cortado. O reconhecimento roda em outra thread, então uma frase nova nunca se perde enquanto a anterior é processada.
Ajustes em `config.json` (feche a Alicia antes de editar):
- `mic_preroll_seconds` (1.6): áudio guardado antes da fala. Se ainda cortar o começo, suba para 2.2.
- `mic_end_silence` (1.0): silêncio que encerra a frase. Suba para 1.4 se ela te interromper no meio.
- `mic_post_speech_cooldown` (1.0): pausa depois que a Alicia fala. Suba se o microfone captar a voz dela.
- `mic_start_multiplier` (2.5) e `mic_energy_floor` (250): sensibilidade. Se captar ruído demais, suba; se não pegar sua voz baixa, desça.

## "Só ouve quando eu clico na janela"
O Windows 10/11 põe programas sem foco em **modo de economia** (EcoQoS): as threads do microfone são atendidas devagar, o áudio
transborda e a Alicia só "acorda" quando você clica. A partir desta versão ela **desliga esse modo para si mesma** ao iniciar
(também desliga o QuickEdit do console, que pausava o programa quando você clicava nele). Para conferir na sua máquina:
`Teste_Completo_Alicia.bat --foco` mede a leitura do microfone com a janela em foco e em segundo plano.
Se ainda houver atraso, abra o **Gerenciador de Tarefas → Detalhes**, clique com o botão direito em `python.exe` (e `WindowsTerminal.exe`) e
desmarque **Modo de eficiência**; em *Configurações → Sistema → Energia*, use o modo **Melhor desempenho**.

## Voz e jeito de falar (menos robótica)
- A persona agora pede **fala de gente**: português do dia a dia ("tá", "né", "pra"), frases curtas, reação antes da resposta, opinião própria, sem "Claro!/Entendido./Posso ajudar em mais algo?", sem listas em conversa. As últimas aberturas que ela usou ("Ah,", "Hum,") entram no prompt como "não repetir".
- A **voz** muda ritmo, tom e volume conforme a emoção (feliz mais ligeira, pensativa mais lenta, brava mais firme, surpresa mais aguda), com uma pequena variação a cada frase. Emojis, links, markdown e travessões são tratados antes de falar. Para voltar à voz neutra: `"voice_expressive": false` no `config.json`.
- Outra voz do Edge: `"voice_name": "pt-BR-ThalitaMultilingualNeural"` no `config.json` (se não funcionar, ela volta sozinha para a Francisca).

## 🃏 Minigame de pôquer (Five-Card Draw contra a Alicia)
**Abrir:** botão direito no avatar → **🃏 Jogar pôquer com a Alicia**, ou digite/diga *"vamos jogar pôquer"*, *"bora jogar poker"* ou `/poker`.
O avatar flutuante some enquanto você joga e volta quando você clica em **Sair do jogo** (ou fecha a janela). A Alicia continua funcionando normalmente (conversa por teclado e voz, comandos); só os comentários espontâneos sobre a tela ficam pausados durante a partida.

**Regras (resumo):** baralho de 52 cartas, 5 cartas para cada um, 1000 fichas cada, ante de 10. Apostas → troca (0 a 5 cartas, **uma vez** por mão) → apostas → showdown.
Aposta mínima 20; aumento mínimo = a última aposta; no máximo 3 aumentos por rodada. Passar só sem aposta aberta; desistir só diante de uma aposta.
Quem não é o dealer age primeiro; o botão alterna a cada mão. Se alguém não consegue pagar tudo (all-in), a parte da aposta que o outro não igualou **volta ao dono** antes do showdown.
Empate divide o pote. A partida acaba quando alguém fica sem fichas; **Nova partida** recomeça com 1000 para cada.

**Controles:** clique nas suas cartas na fase de troca para marcar as que quer **TROCAR** (ficam destacadas), depois *Confirmar troca* (ou *Ficar com as cinco*). Apostar/Aumentar usa o valor da caixa numérica
e os atalhos *Mín*, *½ pote*, *Pote*; *All-in* põe tudo. Os botões ilegais ficam desativados. *Próxima mão* aparece ao fim de cada mão (serve de pausa).

**A Alicia:** joga por cálculo local (não depende do modelo de linguagem nem de internet): estima a força da mão por simulação, escolhe as trocas pelo valor esperado, considera pote, tamanho das apostas, fichas restantes e o seu estilo (quanto você desiste ou aumenta), e blefa de vez em quando.
Ela **só enxerga o que uma pessoa honesta enxergaria** (as cartas dela, as apostas e quantas cartas você trocou). As falas e expressões vêm de eventos reais e mudam com a personalidade escolhida; ela fala em voz alta se a voz estiver ligada (`poker_voice` no `config.json`) e sempre mostra o balão.

**Conversa na mesa:** com a mesa aberta, tudo o que você disser (voz ou teclado) é respondido só sobre a partida, no tom da personalidade escolhida; as reações às jogadas também são geradas na hora a partir do que realmente aconteceu (se a API demorar ou falhar, entra a fala pronta de sempre). Enquanto a mesa está aberta, a conversa normal, a análise de tela e o modo gamer ficam fora, e nada do que é dito ali entra na memória da conversa normal. Só os comandos de personalidade continuam valendo.

## 📞 Modo call (Discord): a Alicia ouve a conversa e participa — Linux (PipeWire/PulseAudio) e Windows
Ela escuta o áudio que **sai do computador** (a voz dos seus amigos), guarda os últimos ~10 min como contexto e **só fala quando alguém diz "Alicia"** (você ou seu amigo).
**Linux:** precisa de `parec` e `pactl` (pacote `libpulse`/`pipewire-pulse`). **Windows:** usa `soundcard` + `numpy` (loopback do alto-falante padrão; sem áudio virtual, então seu amigo não ouve a Alicia e ela pausa a escuta enquanto fala). Em ambos precisa do SpeechRecognition e de internet (a transcrição usa o Google, como o microfone: **avise seus amigos** de que o áudio da call é transcrito).

**Ligar/desligar:** digite `/call` na janela do terminal. `/resumo` faz ela resumir a conversa da call.
Ao ligar, ela cria 3 canais virtuais (e remove tudo ao desligar ou sair):
- `alicia_discord`: a saída do Discord (`discord`, `vesktop`, `equibop`...) é movida para cá sozinha; você continua ouvindo normalmente. É só isto que ela escuta, então música, jogo e a própria voz dela **não** entram.
- `alicia_voz`: por onde a voz dela sai (você ouve, e seu amigo também).
- `alicia_mix`/**`alicia_mic`**: mistura **seu microfone + a voz da Alicia**.

**Passo manual (uma vez por sessão):** no Discord → Configurações de Voz → **Dispositivo de entrada = `Alicia_Mic`**. Assim seu amigo ouve você e a Alicia. Sem isso ela escuta a call, mas o amigo não a ouve.
Sem `pactl`, ou com `"call_virtual_audio": false`, ela escuta o áudio geral do sistema e pausa a escuta enquanto fala (amigo não a ouve).

**Segurança:** o que o *amigo* fala é só conversa para ela: não troca seu nome, não grava/apaga memórias, não muda a personalidade e não faz ela olhar a sua tela.
**Limites:** todos os falantes remotos aparecem como "Amigo" (sem separar vozes); o contexto da call fica só em memória (não vai para o banco); latência típica de 3–6 s por resposta.
**Ajustes** (`config.json`, feche a Alicia antes): `call_discord_apps` (Discord no navegador: `["firefox"]`, mas isso captura tudo do navegador), `call_mic_source` (microfone real do mix), `call_context_seconds`, `call_reply_cooldown`, `call_end_silence`, `call_energy_floor` (suba se ela captar ruído como fala).

## Avatar animado (uma animação por emoção)
Cada emoção tem a sua pasta em `assets\anim\<emoção>\` (`serio`, `feliz`, `bravo`, `pensando`, `surpresa`) com:
`idle_NN` (respiração e cabelo, em loop), `blink_N` (piscar), `talk_N` (formas da boca) e o `anim.json`.

Em **qualquer emoção** ela respira, pisca em intervalos irregulares (2,5 a 6 s, às vezes duas vezes) e mexe a boca **enquanto o áudio toca**
(se a voz estiver desligada, a boca mexe por um tempo proporcional ao tamanho da resposta). A emoção aparece na hora e,
depois de ~8 s (`avatar_relax_seconds` no `config.json`), ela relaxa para o rosto neutro (**serio**). O mesmo vale para a mesa de pôquer.
Emoção sem pasta (ou com algum quadro faltando) aparece como imagem estática, e as demais continuam animadas.
O layout antigo (`assets\anim\anim.json` solto, só da *serio*) ainda funciona.

**Trocar/refazer as animações:** coloque as folhas do ChatGPT (fundo verde, 4 colunas x 5 linhas: 12 respiração + 4 piscar + 4 boca) em uma pasta
(`serio.png`, `feliz.png`, `brava.png`, `pensando.png`, `surpresa.png`) e rode
`python tools\montar_animacoes.py --folhas <pasta> --previas previas` (precisa de `pip install numpy scipy Pillow`).
Ele recorta, tira o fundo verde, alinha os quadros, completa o corpo das linhas cortadas e grava tudo em `assets\anim\<emoção>\`.
`tools\gerar_avatar.py` é a ferramenta antiga (animação única da *serio*).

## Avatar
As imagens ficam em `assets\` (`feliz`, `serio`, `surpresa`, `pensando`, `bravo`, mais `feliz_2` e `feliz_3`, que alternam sozinhas quando ela está feliz). Para trocar o visual, substitua os PNGs mantendo os nomes (fundo transparente).

## Voz e visão
Voz de saída: edge-tts gera a fala e o próprio Windows toca (sem pygame). Microfone: SpeechRecognition + **sounddevice** (traz o PortAudio junto; funciona em qualquer Python no Windows, sem PyAudio). Escolha o microfone no botão direito do avatar. Se o Windows bloquear, vá em *Configurações → Privacidade → Microfone* e permita o acesso. Se faltar alguma biblioteca, esse recurso fica desligado e o resto funciona.
A conversa usa exclusivamente a API da NVIDIA; a Observadora é 100% local. No **Modo Gamer** a análise do jogo é **100% local** (nada sai do PC):
- **Padrão (sem instalar nada):** lê o quadro com OCR + movimento, comenta **vitória/derrota** vistas na tela e marcos de tempo de jogo (`obs_long_activity_minutes`). Desligar: `gamer_local_analysis: false`.
- **Modelo de visão local (opcional):** um modelo de IA que roda dentro do seu PC e *descreve* a cena do jogo. Instale com `Iniciar_Alicia.bat --vision-local`: ele explica o custo e pede autorização;
  depois instala o `llama-cpp-python` na `.venv` (versão pronta para CPU, Python 3.10–3.12) e baixa o modelo **Qwen2.5-VL-3B** (≈ 3 GB, conferido por SHA-256) em `models\vision\`.
  Usa 4–6 GB de RAM enquanto analisa e é descarregado da memória depois de 5 min parado (`local_vlm_idle_unload_s`). O diagnóstico completo testa o modelo com uma imagem desenhada na memória.
  Ele também responde ao "Alicia, olha". A NVIDIA só é usada como reserva se **você** autorizar (`obs_allow_remote_vision`, desligado por padrão).

## Gerar .exe (opcional, não testado)
Só depois de tudo funcionar: `.venv\Scripts\python -m pip install pyinstaller` e empacotar `main.py`.
