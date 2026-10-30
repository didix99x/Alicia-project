# Alicia — assistente pessoal (cérebro via API NVIDIA)

## Primeira vez (Windows 10/11)
1. **Crie uma chave NOVA** em https://build.nvidia.com (Settings → API Keys). A chave que foi colada
   numa conversa deve ser considerada exposta: **revogue-a** no painel.
2. Dê dois cliques em **`Iniciar_Alicia.bat`**. Ele:
   - procura o Python e, **se não achar, instala o Python 3.12 sozinho** (winget ou download oficial do python.org);
   - cria o ambiente `.venv`, instala as bibliotecas essenciais e tenta as opcionais uma a uma;
   - cria o `.env` e pede a chave (a digitação fica oculta);
   - valida a chave e o modelo na API da NVIDIA e roda o diagnóstico;
   - abre a Alicia e **pergunta como você quer ser chamado** (salvo para sempre no banco).
   Logo depois do nome ela pergunta a **personalidade** (1 Normal, 2 Brincalhona, 3 Séria, 4 Malcriada, 5 Carinhosa).
3. Converse **na janela preta** (teclado) ou pelo microfone. O avatar é só a parte visual; não feche a janela preta.

O iniciador também **baixa o que faltar** em qualquer execução (bibliotecas essenciais e opcionais). A visão (Ollama + modelo, vários GB) só é baixada se você responder **s** à pergunta — ou depois com `Iniciar_Alicia.bat --vision`.

## Próximas vezes
Dois cliques em `Iniciar_Alicia.bat`. Só instala o que faltar, não pede nome nem chave de novo.

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
| `Alicia, olha a tela` | análise de tela (exige Ollama) |

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
"Alicia, olha" / "ver" / "olhar" (também "Alícia", com acento) inicia a análise na hora, mesmo com a observação espontânea desligada.
Caminhos, nesta ordem: **Ollama local** (se instalado) → **modelo de visão da API da NVIDIA**, descoberto sozinho com um teste real.
Comentários espontâneos e o modo gamer só usam o Ollama local (para não gastar a cota da API).

## Logs e diagnóstico
- Logs: `logs\alicia_system.log` (chaves são mascaradas). Nível: `ALICIA_LOG_LEVEL=DEBUG` no `.env`.
- Diagnóstico completo: `Diagnostico_Alicia.bat` (Python, bibliotecas, banco, chave sem revelá-la, API, modelo, interface, áudio, Ollama).
- Forçar reinstalação: `Iniciar_Alicia.bat --reinstall`.

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

## 🔥 Modo sem filtro (conversa solta entre amigos)

Uma camada de **tom** por cima da personalidade escolhida: gírias, zoeira, sarcasmo, humor ácido, palavrão de brincadeira, sem sermão
nem avisos desnecessários. Ela continua sendo a Alicia (gótica, sarcástica, provocadora); o modo só solta a linguagem.

* **Ligar:** menu do avatar (botão direito) → `🔥 Modo sem filtro`, ou por voz/texto: *"Alicia, ativar modo sem filtro"*,
  *"Alicia, pode falar livremente"*. **Desligar:** o mesmo item do menu, *"desativar modo sem filtro"* ou *"volta ao normal"*.
* Enquanto estiver ligado aparece o selo **🔥 SEM FILTRO** no avatar e `· SEM FILTRO` no status do menu. O estado fica salvo (sobrevive a reiniciar).
* Parâmetros próprios (`sem_filtro_temperature`, `sem_filtro_top_p`, `sem_filtro_max_tokens` no `config.json`); desligado, voltam os `brain_*` de sempre.
* Amigos na call **não** conseguem ligar/desligar. O pôquer tem o próprio prompt e não é afetado.
* **Limite:** o modo só muda o que a Alicia pede ao modelo. O modelo hospedado (NVIDIA) tem as próprias regras e pode recusar algo mesmo assim.
  As proteções contra risco real (ódio contra grupos, assédio, ameaças, instruções perigosas, alguém em crise) continuam valendo.

## 🎤 Reconhecimento de voz (quem está falando)

Distingue pessoas pela voz (embeddings de locutor, WeSpeaker ResNet34 em ONNX). **Não é o STT**: o texto continua vindo do SpeechRecognition.
Roda local, em CPU; nenhum áudio é gravado. Pede `onnxruntime` + `numpy` (o launcher instala) e um modelo de ~26 MB baixado **uma vez**
do GitHub (checksum SHA-256 verificado) na primeira vez que alguém for cadastrado (`data/models/`).

* **Cadastrar (sempre por voz e com autorização falada da própria pessoa):** *"Alicia, cadastrar minha voz"* ou *"cadastrar a voz do Pedro"*.
  Ela explica o que será guardado, pede *"sim, autorizo"* e ouve 5 frases. Na call, uma voz desconhecida que chama a Alicia recebe a oferta
  (*quer cadastrar? como te chamo?*); dizer não silencia a oferta por 10 min.
* **Gerenciar:** *"apagar a voz do Pedro"*, *"apaga minha voz"* (a própria pessoa pode, se for reconhecida), *"refazer o cadastro do Pedro"*,
  *"quais vozes você conhece"*, *"desativar/ativar reconhecimento de voz"*, *"cancelar cadastro"*; ou pelo menu `🎤 Reconhecimento de voz`.
* **Perfis:** `data/users/user_001.json`, `user_002.json`… (um por pessoa: nome, consentimento, embedding, limiar, histórico de identificações,
  preferências e contexto individual) e `unknown.json` (só contadores). `data/` está no `.gitignore`; arquivos com permissão 0600 no Linux.
* **Decisão:** só afirma quem é com similaridade ≥ 0,55 **e** vantagem ≥ 0,08 sobre a segunda voz mais parecida. Entre 0,43 e 0,55, ou com duas
  vozes parecidas, ela diz que não tem certeza (e pergunta *"é você, X?"*). Abaixo disso a pessoa é "Amigo" (desconhecido). Fala curta
  (< 1 s de voz) não é identificada.
* **Privacidade:** o contexto de cada pessoa é separado; amigos na call não veem as memórias pessoais do dono, e o dono não vê as de ninguém
  misturadas. **A identificação por voz pode errar e não é autenticação**: nada sensível deve depender dela.
* **Limites conhecidos:** duas pessoas falando ao mesmo tempo viram um trecho só (vence a voz mais forte); áudio comprimido do Discord, ruído
  alto ou telefone degradam a confiança (viram "incerto"/"desconhecido", não um nome errado).
* Config (`config.json`): `voice_id_enabled`, `voice_id_threshold`, `voice_id_margin`, `voice_id_enroll_samples`, `voice_id_offer_unknown`,
  `voice_id_mic_default_owner` (no SEU microfone, voz não reconhecida continua sendo tratada como a sua).

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

## Voz, visão e Ollama
Voz de saída: edge-tts gera a fala e o próprio Windows toca (sem pygame). Microfone: SpeechRecognition + **sounddevice** (traz o PortAudio junto; funciona em qualquer Python no Windows, sem PyAudio). Escolha o microfone no botão direito do avatar. Se o Windows bloquear, vá em *Configurações → Privacidade → Microfone* e permita o acesso. Se faltar alguma biblioteca, esse recurso fica desligado e o resto funciona.
O **Ollama agora serve só à visão/modo gamer** (opcional). A conversa usa exclusivamente a NVIDIA.

## Gerar .exe (opcional, não testado)
Só depois de tudo funcionar: `.venv\Scripts\python -m pip install pyinstaller` e empacotar `main.py`.
