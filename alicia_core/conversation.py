"""
Fluxo de turnos da conversa (mixin da AliciaAssistantCore).

Cada interação é um TURNO com identificador único e segue sempre a mesma ordem:

  receber -> descartar duplicata -> garantir um único turno em andamento ->
  (comando de nome/memória | comando social | comando visual | cérebro/API) ->
  validar resposta -> salvar usuário+resposta numa única transação -> devolver

Regras que evitam loops e mistura de mensagens:
  * Duplicatas (mesmo texto em poucos segundos, ex.: callback de voz disparado
    duas vezes ou teclado+voz) são descartadas antes de qualquer processamento.
  * Só UM turno roda por vez. Uma segunda mensagem durante a resposta recebe um
    aviso explícito (não entra no histórico e não gera chamada à API).
  * Falha da API NÃO gera nova chamada automática nem é gravada como resposta
    da Alicia; o histórico só recebe turnos que terminaram com sucesso.
  * Resposta vazia/inválida vira erro controlado, sem ciclo de novas chamadas.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Optional, Tuple

from .brain import ConversationBrain, clean_name, parse_command
from .database import _hash, _strip_accents
from .enums import AliciaExpression
from .logging_setup import logger
from .nvidia_client import BrainError
from .prompts import PromptEngine


class ConversationMixin:
    DUPLICATE_WINDOW = 4.0  # segundos

    # ------------------------------------------------------------------
    def _init_conversation(self):
        self.session_id = uuid.uuid4().hex[:12]
        self.brain = ConversationBrain(self.db, self.nvidia, self.config)
        self.chat_ready = threading.Event()
        self._turn_lock = threading.Lock()
        self._dup_lock = threading.Lock()
        self._last_input_hash: Optional[str] = None
        self._last_input_time = 0.0
        self.last_user_message = ""
        self.last_alicia_message = ""
        self.total_interactions = 0

    # ------------------------------------------------------------------
    # NOME (persistido no SQLite)
    # ------------------------------------------------------------------
    def display_name(self) -> str:
        name = self.db.get_user_name()
        return name or ""

    def ensure_user_name(self, ask_fn=None) -> bool:
        """
        Garante que o nome do usuário exista no banco. Pergunta apenas se ainda não
        houver nome salvo. Nunca inventa um nome. Devolve True quando há nome salvo.
        """
        ask_fn = ask_fn or input   # resolvido na hora da chamada
        name = self.db.get_user_name()
        if name:
            self.config.user_name = name
            return True

        # Migração: instalações antigas guardavam o nome só no config.json.
        legacy = (self.config.user_name or "").strip()
        if (not self.config.first_run) and legacy and legacy.lower() not in ("usuário", "usuario", "viajante"):
            valid = clean_name(legacy)
            if valid:
                self.db.set_user_name(valid)
                logger.info("Nome migrado do config.json para o banco de dados.")
                return True

        question = "Antes de começarmos, como você gostaria que eu te chamasse?"
        for attempt in range(3):
            self._say(question, AliciaExpression.FELIZ)
            try:
                answer = ask_fn("\nSeu nome > ")
            except (EOFError, KeyboardInterrupt):
                return False
            valid = clean_name(answer)
            if valid:
                self.db.set_user_name(valid)
                self.config.user_name = valid
                self.config.first_run = False
                self.config.save()
                self._say(f"Prazer, {valid}. Vou usar esse nome nas nossas conversas.", AliciaExpression.FELIZ)
                return True
            question = "Não consegui entender. Me diz só o nome (uma a três palavras, sem números)."
        logger.warning("Nome não informado; vou perguntar de novo na próxima inicialização.")
        return False

    # ------------------------------------------------------------------
    # PERSONALIDADE (salva no SQLite; trocável a qualquer momento)
    # ------------------------------------------------------------------
    def get_personality(self) -> str:
        key = self.db.get_profile("personality")
        return key if key in PromptEngine.PERSONALITIES else PromptEngine.DEFAULT_PERSONALITY

    def set_personality(self, key: str) -> bool:
        """Troca o estilo. Não toca no histórico nem nas memórias: o próximo turno já usa o novo tom."""
        if key not in PromptEngine.PERSONALITIES:
            return False
        if key != self.db.get_profile("personality"):
            # marca o ponto da troca: as respostas ANTERIORES (no tom velho) deixam de puxar o tom das próximas
            try:
                last = self.db.get_chat_messages(limit=1)
                self.db.set_profile("personality_since_id", str(last[0]["id"] if last else 0))
            except Exception as e:
                logger.warning(f"Não consegui marcar o ponto da troca de personalidade: {e}")
        self.db.set_profile("personality", key)
        logger.info(f"🎭 Personalidade: {key}")
        return True

    def ensure_personality(self, ask_fn=None) -> bool:
        """Na primeira execução pergunta como a Alicia deve ser. Depois nunca pergunta de novo."""
        ask_fn = ask_fn or input
        if self.db.get_profile("personality") in PromptEngine.PERSONALITIES:
            return True
        print("\nComo você quer que eu seja? (dá para trocar a qualquer momento: /personalidade)")
        print(PromptEngine.personality_menu_text())
        for _ in range(3):
            try:
                answer = ask_fn("\nPersonalidade (número ou nome) > ")
            except (EOFError, KeyboardInterrupt):
                return False
            key = PromptEngine.resolve_personality(answer)
            if key:
                self.set_personality(key)
                self._say(PromptEngine.PERSONALITIES[key]["confirm"], AliciaExpression.FELIZ)
                return True
            print("Não entendi. Digite um número de 1 a 5 ou o nome (ex.: carinhosa).")
        self.set_personality(PromptEngine.DEFAULT_PERSONALITY)
        print("Vou começar no modo Normal; você pode trocar com /personalidade.")
        return True

    def _say(self, text: str, expr: AliciaExpression):
        print(f"\n🤖 Alicia [{expr.value.upper()}]: {text}")
        try:
            self.tts.speak(text, expr)
        except Exception as e:
            logger.warning(f"Erro no TTS: {e}")

    # ------------------------------------------------------------------
    # ENTRADA ÚNICA DE MENSAGENS
    # ------------------------------------------------------------------
    def interact(self, user_message: str, source: str = "keyboard") -> str:
        text = self._normalize_text(user_message)
        if not text:
            return ""

        if not self.chat_ready.is_set():
            return "" if source == "voice" else "[SERIO] Calma, ainda estou me apresentando."

        now = time.monotonic()
        digest = _hash(text)
        with self._dup_lock:
            if digest == self._last_input_hash and (now - self._last_input_time) < self.DUPLICATE_WINDOW:
                logger.info("Mensagem duplicada descartada (mesmo texto em poucos segundos).")
                return ""
            self._last_input_hash, self._last_input_time = digest, now

        if not self._turn_lock.acquire(timeout=0.5):
            self._forget_last_input()
            logger.info(f"Mensagem ({source}) recusada: outro turno em andamento.")
            return "" if source == "voice" else "[SERIO] Calma, ainda estou respondendo à mensagem anterior."
        try:
            return self._interact_impl(text, source)
        finally:
            self._turn_lock.release()

    def _forget_last_input(self):
        with self._dup_lock:
            self._last_input_hash = None

    # ------------------------------------------------------------------
    def _tag(self, text: str, expr: AliciaExpression) -> str:
        return f"[{expr.value.upper()}] {text}"

    def _persist(self, user_text: str, answer: str, expr: AliciaExpression, turn_id: str, source: str, meta: dict):
        try:
            self.db.add_exchange(
                user_text, answer, expr.value, turn_id, session_id=self.session_id, source=source, metadata=meta
            )
        except Exception as e:  # falha ao salvar não derruba a conversa
            logger.error(f"Falha ao salvar o turno {turn_id}: {e}")

    def _finish(self, user_text, answer, expr, turn_id, source, meta) -> str:
        self.current_expression = expr
        self.last_user_message = user_text
        self.last_alicia_message = answer
        self._persist(user_text, answer, expr, turn_id, source, meta)
        return self._tag(answer, expr)

    # ------------------------------------------------------------------
    def _interact_impl(self, text: str, source: str) -> str:
        turn_id = uuid.uuid4().hex

        # ---- modo descanso (não grava nada) ----
        if self.modo_descanso:
            lower = text.lower()
            if any(w in lower for w in ("acordar", "acorda", "despertar", "sair do descanso", "/acordar", "/voltar")):
                self.sair_modo_descanso()
                return "[FELIZ] Já acordei!"
            return "[SERIO] Zzz... estou em modo descanso."

        # ---- mesa de pôquer aberta: a conversa fica SÓ na partida (sem memória, tela, modo gamer ou chat normal) ----
        if getattr(self, "poker_active", False):
            self.last_user_activity_time = time.time()
            return self._poker_interact(text)

        self.last_user_activity_time = time.time()
        self.total_interactions += 1
        meta = {"turn": self.total_interactions}

        # ---- 1. nome e memória (determinístico) ----
        cmd = parse_command(text)
        if cmd:
            answer, expr = self._run_memory_command(*cmd)
            meta["event"] = f"command_{cmd[0]}"
            return self._finish(text, answer, expr, turn_id, source, meta)

        # ---- 2. comandos sociais já existentes ----
        social = self._social_reply(text)
        if social:
            answer, expr, kind, target = social
            meta.update({"event": "social_command", "command_type": kind, "target_name": target})
            return self._finish(text, answer, expr, turn_id, source, meta)

        # ---- 3. pedido explícito para olhar a tela ----
        if self._is_direct_visual_command(text):   # pedido explícito vale mesmo com a observação espontânea desligada
            answer, expr, ok = self.analyze_screen_now(text)
            if not answer:
                return ""
            if not ok:  # erro de visão: mostra, mas não grava como fala normal
                self._forget_last_input()
                return self._tag(answer, expr)
            meta["event"] = "screen_analysis"
            return self._finish(text, answer, expr, turn_id, source, meta)

        # ---- 4. cérebro (API NVIDIA) ----
        name = self.db.get_user_name() or ""
        try:
            raw = self.brain.reply(text, name, self.last_visual_context)
        except BrainError as e:
            logger.error(f"Cérebro indisponível neste turno: {e.kind}")
            self._forget_last_input()  # permite tentar de novo com a mesma frase
            return self._tag(e.user_message, AliciaExpression.BRAVO)
        except Exception as e:
            logger.exception(f"Erro inesperado no cérebro: {type(e).__name__}")
            self._forget_last_input()
            return self._tag("Meu cérebro tropeçou por um erro interno. Veja o log e tente de novo.", AliciaExpression.BRAVO)

        # remove "falas" inventadas do usuário no fim da resposta
        raw = re.split(r"\n\s*(?:usu[aá]rio|user|voc[eê]|" + re.escape(name or "§§") + r")\s*:", raw, maxsplit=1, flags=re.IGNORECASE)[0]
        answer, expr = self.parse_expression_tags(raw)
        answer = self._limit_response_length(
            answer, max_sentences=int(self.config.brain_max_sentences), max_chars=int(self.config.brain_max_chars)
        )
        if not answer:
            logger.error("Resposta do modelo vazia após o tratamento.")
            self._forget_last_input()
            return self._tag(BrainError("bad_response").user_message, AliciaExpression.BRAVO)

        return self._finish(text, answer, expr, turn_id, source, meta)

    # ------------------------------------------------------------------
    # CONVERSA DURANTE O PÔQUER
    # ------------------------------------------------------------------
    def _poker_interact(self, text: str) -> str:
        """Resposta no modo pôquer. Não grava no histórico/memória da conversa normal e não olha a tela.

        Só os comandos de personalidade continuam valendo (mudam o tom da mesa). Frases como 'esquece isso' ou
        'lembra que...' ditas no calor do jogo NÃO apagam nem criam memórias: viram conversa da partida.
        """
        cmd = parse_command(text)
        if cmd and cmd[0] in ("personality", "personality_menu"):
            answer, expr = self._run_memory_command(*cmd)
            return self._tag(answer, expr)
        handler = getattr(self, "poker_chat_handler", None)
        if handler is None:
            return self._tag("Estou concentrada na partida. Sua vez de jogar.", AliciaExpression.SERIO)
        try:
            answer, expr = handler(text)
        except BrainError as e:
            logger.error(f"Pôquer: conversa indisponível neste turno: {e.kind}")
            self._forget_last_input()
            return self._tag(e.user_message, AliciaExpression.BRAVO)
        except Exception as e:
            logger.exception(f"Pôquer: erro inesperado na conversa: {type(e).__name__}")
            self._forget_last_input()
            return self._tag("Perdi o fio da partida por um erro interno. Tente de novo.", AliciaExpression.BRAVO)
        return self._tag(answer, expr)

    # ------------------------------------------------------------------
    # COMANDOS DE NOME / MEMÓRIA
    # ------------------------------------------------------------------
    def _run_memory_command(self, kind: str, arg: Optional[str]) -> Tuple[str, AliciaExpression]:
        try:
            if kind == "rename":
                name = clean_name(arg or "")
                if not name:
                    return "Esse nome não me pareceu válido. Use de uma a três palavras, sem números.", AliciaExpression.SERIO
                self.db.set_user_name(name)
                self.config.user_name = name
                self.config.save()
                return f"Combinado. A partir de agora te chamo de {name}.", AliciaExpression.FELIZ

            if kind == "remember":
                mem_id = self.db.add_memory(arg or "", category="pedido do usuário")
                if mem_id is None:
                    return "Isso eu já tinha anotado, ou a frase ficou curta demais para guardar.", AliciaExpression.PENSANDO
                return "Anotado. Vou guardar isso.", AliciaExpression.FELIZ

            if kind == "forget":
                removed = self.db.delete_memories_matching(arg or "")
                if removed:
                    return f"Pronto, apaguei {removed} lembrança(s) sobre isso.", AliciaExpression.SERIO
                return "Não achei nenhuma memória guardada que combine com isso.", AliciaExpression.PENSANDO

            if kind == "personality":
                if not self.set_personality(arg or ""):
                    return "Não conheço esse estilo.", AliciaExpression.SERIO
                return PromptEngine.PERSONALITIES[arg]["confirm"], AliciaExpression.FELIZ

            if kind == "personality_menu":
                atual = PromptEngine.PERSONALITIES[self.get_personality()]["label"]
                opcoes = ", ".join(v["label"] for v in PromptEngine.PERSONALITIES.values())
                return (f"Agora estou no modo {atual}. Posso ser: {opcoes}. "
                        "Diga, por exemplo, 'seja carinhosa' ou use o menu do avatar (botão direito)."), AliciaExpression.PENSANDO

            if kind == "poker":
                if self.open_poker():
                    return "Senta aí. Eu embaralho… e você tenta não chorar.", AliciaExpression.FELIZ
                return "Não consegui abrir a mesa agora. A janela da Alicia precisa estar aberta.", AliciaExpression.SERIO

            if kind == "help":
                return ("Comandos: 'me chama de X' troca seu nome; 'lembre que ...' guarda uma memória; "
                        "'esqueça ...' apaga; 'o que você lembra de mim' lista; 'vamos jogar pôquer' abre a mesa; 'seja carinhosa/séria/brincalhona/malcriada/normal' troca meu jeito; '/novoassunto' começa um assunto "
                        "limpo sem apagar nada; '/backup' copia a memória; '/exportar' gera um texto da conversa; "
                        "'Alicia, olha a tela' analisa a tela; '/descanso', '/acordar', '/gamer' e 'sair'."), AliciaExpression.PENSANDO

            if kind == "backup":
                path = self.db.backup()
                return f"Backup da memória salvo em {path}.", AliciaExpression.FELIZ

            if kind == "export":
                path = self.db.export_text()
                return f"Conversa exportada para {path}.", AliciaExpression.FELIZ

            if kind == "newctx":
                self.db.start_new_context()
                return "Certo, assunto novo. Guardei tudo o que conversamos, só não vou arrastar isso para as próximas respostas.", AliciaExpression.FELIZ

            if kind == "list":
                mems = self.db.list_memories(limit=10)
                name = self.db.get_user_name()
                parts = [f"Seu nome é {name}."] if name else []
                if mems:
                    parts.append("Guardei isto: " + "; ".join(m["text"] for m in mems) + ".")
                else:
                    parts.append("Não tenho nenhuma memória guardada além disso. Peça com 'lembre que ...'.")
                return " ".join(parts), AliciaExpression.PENSANDO
        except Exception as e:
            logger.error(f"Falha no comando de memória '{kind}': {e}")
            return "Não consegui mexer na minha memória agora. Veja o log.", AliciaExpression.BRAVO
        return "Não entendi esse comando.", AliciaExpression.SERIO

    # ------------------------------------------------------------------
    # COMANDOS SOCIAIS (lógica original, agora sem gravar sozinha)
    # ------------------------------------------------------------------
    def _social_reply(self, text: str):
        detected, command_type, name = self._detect_alicia_greeting_command(text)
        if not detected:
            return None
        if command_type == "apresentacao":
            response = self._alicia_apresentar()
        elif command_type == "cumprimento":
            response = self._alicia_cumprimentar(name)
        else:
            return None
        clean_text, expression = self.parse_expression_tags(response)
        clean_text = self._limit_response_length(clean_text)
        if not clean_text:
            return None
        return clean_text, expression, command_type, name
