# -*- coding: utf-8 -*-
"""
Testes automatizados da Alicia (unittest, só biblioteca padrão).

Rodar:   python -m unittest discover -s tests -v

O que estes testes PROVAM: banco/memória, mecânica do cliente HTTP (retry limitado, erros,
chave fora dos logs), fluxo de turnos (sem duplicação, sem loops, concorrência), nome persistente.
O que NÃO provam: a API real da NVIDIA (use Diagnostico_Alicia.bat com a sua chave), a
interface Qt, o áudio e o microfone (dependem do seu hardware).
"""

import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from tests.support import FAKE_KEY, MockNvidia, APP_DIR

from alicia_core.database import MemoryDatabase, DatabaseUnavailable, normalize_role
from alicia_core.env import parse_env_text, is_placeholder, set_env_value
from alicia_core.logging_setup import redact
from alicia_core.nvidia_client import NvidiaClient, BrainError, normalize_messages
from alicia_core.brain import parse_command, clean_name


# ======================================================================
class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="alicia_test_")
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ======================================================================
class TestDatabase(TmpDirCase):
    def test_empty_db(self):
        db = MemoryDatabase("m.db")
        self.assertEqual(db.get_chat_messages(), [])
        self.assertIsNone(db.get_user_name())
        self.assertEqual(db.list_memories(), [])
        self.assertTrue(db.check_integrity())

    def test_exchange_saved_and_survives_restart(self):
        db = MemoryDatabase("m.db")
        self.assertTrue(db.add_exchange("oi", "olá!", "feliz", "t1", source="keyboard"))
        db2 = MemoryDatabase("m.db")  # "reiniciar o programa"
        msgs = db2.get_chat_messages()
        self.assertEqual([(m["role"], m["content"]) for m in msgs], [("user", "oi"), ("assistant", "olá!")])

    def test_duplicate_turn_not_saved_twice(self):
        db = MemoryDatabase("m.db")
        self.assertTrue(db.add_exchange("oi", "olá", "feliz", "t1"))
        self.assertFalse(db.add_exchange("oi", "olá", "feliz", "t1"))
        self.assertEqual(db.count_chat_messages(), 2)

    def test_user_name_persists(self):
        db = MemoryDatabase("m.db")
        db.set_user_name("Ana")
        self.assertEqual(MemoryDatabase("m.db").get_user_name(), "Ana")
        db.set_user_name("Ana Clara")
        self.assertEqual(MemoryDatabase("m.db").get_user_name(), "Ana Clara")

    def test_vision_rows_never_enter_chat_history(self):
        db = MemoryDatabase("m.db")
        db.add_history("Alicia (observação)", "vi um erro no código", "serio")
        db.add_history("Alicia (gamer)", "boa jogada", "feliz")
        db.add_exchange("oi", "olá", "feliz", "t1")
        self.assertEqual(db.count_chat_messages(), 2)
        self.assertEqual(len(db.get_recent_history(10)), 4)  # a visão continua guardada

    def test_migration_of_old_schema_keeps_all_data(self):
        conn = sqlite3.connect("old.db")
        conn.execute("""CREATE TABLE conversation_history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
            content TEXT NOT NULL, expression TEXT DEFAULT 'feliz', timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, metadata TEXT)""")
        conn.execute("""CREATE TABLE vision_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, image_hash TEXT NOT NULL, comment TEXT,
            expression TEXT, perceived_activity TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        rows = [("Fulano", "meu jogo é um tetris roguelite"), ("Alicia", "interessante"),
                ("Alicia (observação)", "código com erro"), ("Fulano", "valeu")]
        for r, c in rows:
            conn.execute("INSERT INTO conversation_history(role, content, metadata) VALUES(?,?,?)", (r, c, "{}"))
        conn.commit(); conn.close()

        db = MemoryDatabase("old.db")
        self.assertEqual(len(db.get_recent_history(100)), 4)  # nada perdido
        msgs = db.get_chat_messages()
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertNotIn("código com erro", [m["content"] for m in msgs])
        MemoryDatabase("old.db")  # idempotente
        self.assertEqual(len(MemoryDatabase("old.db").get_recent_history(100)), 4)

    def test_corrupted_db_raises_and_is_not_deleted(self):
        pathlib.Path("bad.db").write_bytes(b"isto nao e um banco sqlite" * 100)
        before = pathlib.Path("bad.db").read_bytes()
        with self.assertRaises(DatabaseUnavailable):
            MemoryDatabase("bad.db")
        self.assertEqual(pathlib.Path("bad.db").read_bytes(), before)

    def test_memories_dedupe_search_forget(self):
        db = MemoryDatabase("m.db")
        self.assertIsNotNone(db.add_memory("Meu projeto atual é a Alicia"))
        self.assertIsNone(db.add_memory("meu projeto atual é a alicia"))  # duplicada
        db.add_memory("Prefiro respostas curtas")
        found = db.search_memories("qual é o meu projeto?")
        self.assertTrue(found and "projeto" in found[0]["text"])
        self.assertEqual(db.delete_memories_matching("projeto atual"), 1)
        self.assertEqual(len(db.list_memories()), 1)

    def test_search_old_history(self):
        db = MemoryDatabase("m.db")
        db.add_exchange("vamos falar do sistema de combate do tetris roguelite", "boa ideia", "feliz", "a")
        for i in range(6):
            db.add_exchange(f"assunto qualquer {i}", f"resposta {i}", "feliz", f"x{i}")
        hits = db.search_history("o que falamos sobre combate?", limit=3, skip_latest=4)
        self.assertTrue(any("combate" in h["content"] for h in hits))

    def test_normalize_role(self):
        self.assertEqual(normalize_role("Alicia"), "assistant")
        self.assertEqual(normalize_role("Alicia (gamer)"), "assistant")
        self.assertEqual(normalize_role("Qualquer Nome"), "user")


# ======================================================================
class TestEnvAndSecrets(TmpDirCase):
    def test_parse_env(self):
        d = parse_env_text('# c\nNVIDIA_API_KEY="abc"\nNVIDIA_MODEL=m  # x\n\nexport A=1')
        self.assertEqual(d, {"NVIDIA_API_KEY": "abc", "NVIDIA_MODEL": "m", "A": "1"})

    def test_placeholder_detection(self):
        self.assertTrue(is_placeholder(""))
        self.assertTrue(is_placeholder("COLE_SUA_CHAVE_AQUI"))
        self.assertFalse(is_placeholder(FAKE_KEY))

    def test_set_env_value_preserves_other_lines(self):
        pathlib.Path(".env").write_text("A=1\nNVIDIA_API_KEY=x\nB=2\n", encoding="utf-8")
        set_env_value(".env", "NVIDIA_API_KEY", "novo")
        self.assertEqual(pathlib.Path(".env").read_text(), "A=1\nNVIDIA_API_KEY=novo\nB=2\n")

    def test_redaction(self):
        self.assertNotIn("nvapi-ABCDEFG1234567", redact("erro com nvapi-ABCDEFG1234567 no header"))
        self.assertNotIn("SEGREDOSEGREDO", redact("Authorization: Bearer SEGREDOSEGREDO123"))

    def test_env_example_and_gitignore_have_no_secrets(self):
        example = (APP_DIR / ".env.example").read_text(encoding="utf-8")
        self.assertIn("COLE_SUA_CHAVE_AQUI", example)
        self.assertNotRegex(example, r"nvapi-[A-Za-z0-9_\-]{10,}")
        self.assertIn(".env", (APP_DIR / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_no_key_hardcoded_in_source(self):
        import re
        for path in list(APP_DIR.rglob("*.py")) + list(APP_DIR.rglob("*.md")) + list(APP_DIR.rglob("*.bat")):
            if ".venv" in path.parts or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(re.search(r"nvapi-[A-Za-z0-9_\-]{20,}", text), f"possível chave em {path}")


# ======================================================================
class TestNvidiaClient(unittest.TestCase):
    def setUp(self):
        self.srv = MockNvidia()
        self.addCleanup(self.srv.stop)

    def client(self, **kw):
        kw.setdefault("timeout", 2)
        kw.setdefault("max_retries", 2)
        c = NvidiaClient(FAKE_KEY, "test-model", self.srv.base_url, backoff=0.01, **kw)
        return c

    MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "oi"}]

    def test_success_and_auth_header_and_payload(self):
        self.srv.script = [("ok", "olá")]
        self.assertEqual(self.client().chat(self.MSGS), "olá")
        self.assertEqual(self.srv.headers_seen[-1]["Authorization"], f"Bearer {FAKE_KEY}")
        body = self.srv.requests[-1]
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"], self.MSGS)
        self.assertFalse(body["stream"])

    def test_auth_error_not_retried_and_key_not_in_error(self):
        self.srv.script = [("status", 401, "bad key")]
        with self.assertRaises(BrainError) as cm:
            self.client().chat(self.MSGS)
        self.assertEqual(cm.exception.kind, "auth")
        self.assertEqual(len(self.srv.requests), 1)
        self.assertNotIn(FAKE_KEY, str(cm.exception) + cm.exception.user_message)

    def test_rate_limit_retried_then_succeeds(self):
        self.srv.script = [("status", 429, "slow down", {"Retry-After": "0"}), ("ok", "depois")]
        self.assertEqual(self.client().chat(self.MSGS), "depois")
        self.assertEqual(len(self.srv.requests), 2)

    def test_server_error_retries_are_limited(self):
        self.srv.script = [("status", 500, "x")] * 10
        with self.assertRaises(BrainError) as cm:
            self.client(max_retries=2).chat(self.MSGS)
        self.assertEqual(cm.exception.kind, "server")
        self.assertEqual(len(self.srv.requests), 3)  # 1 + 2 repetições, nunca infinito

    def test_bad_request_and_not_found_not_retried(self):
        for status, kind, expected_calls in ((400, "bad_request", 3), (404, "not_found", 1)):
            self.srv.requests.clear()
            self.srv.script = [("status", status, "x")] * 3   # 400: desce 2 degraus de formato e para
            with self.assertRaises(BrainError) as cm:
                self.client().chat(self.MSGS)
            self.assertEqual(cm.exception.kind, kind)
            self.assertEqual(len(self.srv.requests), expected_calls)  # nunca repete em loop

    def test_empty_and_malformed_responses(self):
        for step in (("empty",), ("badjson",)):
            self.srv.requests.clear()
            self.srv.script = [step]
            with self.assertRaises(BrainError) as cm:
                self.client().chat(self.MSGS)
            self.assertEqual(cm.exception.kind, "bad_response")
            self.assertEqual(len(self.srv.requests), 1)  # sem ciclo de novas chamadas

    def test_think_blocks_removed(self):
        self.srv.script = [("ok", "<think>raciocínio</think>[FELIZ] oi")]
        self.assertEqual(self.client().chat(self.MSGS), "[FELIZ] oi")

    def test_timeout(self):
        self.srv.script = [("sleep", 3, "tarde")]
        with self.assertRaises(BrainError) as cm:
            self.client(timeout=0.5, max_retries=0).chat(self.MSGS)
        self.assertEqual(cm.exception.kind, "timeout")

    def test_connection_refused_limited(self):
        c = NvidiaClient(FAKE_KEY, "m", "http://127.0.0.1:9/v1", timeout=1, max_retries=1, backoff=0.01)
        t0 = time.time()
        with self.assertRaises(BrainError) as cm:
            c.chat(self.MSGS)
        self.assertEqual(cm.exception.kind, "network")
        self.assertLess(time.time() - t0, 10)

    def test_missing_key_makes_no_request(self):
        c = NvidiaClient("COLE_SUA_CHAVE_AQUI", "m", self.srv.base_url)
        with self.assertRaises(BrainError) as cm:
            c.chat(self.MSGS)
        self.assertEqual(cm.exception.kind, "config")
        self.assertEqual(self.srv.requests, [])

    def test_cancel_stops_retries(self):
        self.srv.script = [("status", 503, "x")] * 5
        c = self.client(max_retries=4)
        c.backoff = 5
        threading.Timer(0.3, c.cancel).start()
        t0 = time.time()
        with self.assertRaises(BrainError) as cm:
            c.chat(self.MSGS)
        self.assertEqual(cm.exception.kind, "cancelled")
        self.assertLess(time.time() - t0, 3)

    def test_list_models(self):
        self.assertIn("test-model", self.client().list_models())

    def test_normalize_messages(self):
        out = normalize_messages([
            {"role": "system", "content": "S"},
            {"role": "assistant", "content": "saudação sem usuário antes"},
            {"role": "user", "content": "a"}, {"role": "user", "content": "b"},
            {"role": "assistant", "content": "r"}, {"role": "user", "content": "  "},
            {"role": "user", "content": "c"},
        ])
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "user"])
        self.assertEqual(out[1]["content"], "a\nb")


# ======================================================================
class CoreCase(TmpDirCase):
    """Cria a AliciaAssistantCore real, apontando para o servidor local de teste."""

    def setUp(self):
        super().setUp()
        self.srv = MockNvidia()
        self.addCleanup(self.srv.stop)
        os.environ.update({"NVIDIA_API_KEY": FAKE_KEY, "NVIDIA_MODEL": "test-model",
                           "NVIDIA_BASE_URL": self.srv.base_url, "NVIDIA_TIMEOUT": "2", "NVIDIA_MAX_RETRIES": "2"})
        self.core = self.make_core()

    def make_core(self):
        from alicia_core.core import AliciaAssistantCore
        core = AliciaAssistantCore(config_path="config.json")
        core.nvidia.backoff = 0.01
        core.db.set_user_name("Tester")
        core.chat_ready.set()
        core.is_running = True
        return core

    def ask(self, text, **kw):
        return self.core.interact(text, **kw)


class TestConversation(CoreCase):
    def test_greeting_then_topic_change_builds_clean_request(self):
        self.ask("Bom dia!")
        self.ask("Como funciona o sistema de combate do meu jogo?")
        self.assertEqual(len(self.srv.requests), 2)
        msgs = self.srv.requests[1]["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        roles = [m["role"] for m in msgs[1:]]
        self.assertEqual(roles, ["user", "assistant", "user"])  # alternância correta
        self.assertEqual(msgs[-1]["content"], "Como funciona o sistema de combate do meu jogo?")
        joined = "\n".join(m["content"] for m in msgs[1:])
        self.assertEqual(joined.count("Como funciona o sistema de combate do meu jogo?"), 1)  # atual só uma vez
        self.assertIn("Tester", msgs[0]["content"])
        self.assertNotIn("ABANDONE", msgs[0]["content"])  # nada de "mudança de assunto" forçada

    def test_many_messages_each_saved_once_and_one_call_each(self):
        for i in range(5):
            self.ask(f"mensagem número {i} sobre assuntos diferentes {i * 7}")
        self.assertEqual(len(self.srv.requests), 5)
        rows = self.core.db.get_chat_messages(100)
        self.assertEqual(len(rows), 10)
        self.assertEqual([r["role"] for r in rows], ["user", "assistant"] * 5)
        users = [r["content"] for r in rows if r["role"] == "user"]
        self.assertEqual(len(set(users)), 5)

    def test_question_about_previous_subject_gets_history(self):
        self.ask("meu jogo é um tetris roguelite com bosses")
        self.ask("qual era o gênero do meu jogo?")
        last = self.srv.requests[-1]["messages"]
        self.assertTrue(any("tetris roguelite" in m["content"] for m in last))

    def test_old_conversation_recalled_beyond_history_window(self):
        self.core.config.brain_history_messages = 2
        self.core.db.add_exchange("vamos falar do sistema de combate do tetris roguelite", "combinado", "feliz", "old1")
        for i in range(4):
            self.core.db.add_exchange(f"outro assunto {i}", f"resp {i}", "feliz", f"o{i}")
        self.ask("lembra do que falamos sobre combate?")
        system = self.srv.requests[-1]["messages"][0]["content"]
        self.assertIn("[TRECHOS DE CONVERSAS ANTIGAS —", system)
        self.assertIn("sistema de combate", system)

    def test_no_context_question_has_no_invented_memory(self):
        self.ask("você lembra do nome do meu cachorro?")
        system = self.srv.requests[-1]["messages"][0]["content"]
        self.assertNotIn("[MEMÓRIAS CONFIRMADAS —", system)
        self.assertNotIn("[TRECHOS DE CONVERSAS ANTIGAS —", system)
        self.assertIn("Nunca invente lembranças", system)

    def test_empty_response_is_controlled_and_not_saved_and_not_looped(self):
        self.srv.script = [("empty",)]
        out = self.ask("alguma pergunta qualquer")
        self.assertIn("vazia", out)
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(self.core.db.count_chat_messages(), 0)
        out2 = self.ask("alguma pergunta qualquer")  # retry imediato do mesmo texto é permitido
        self.assertNotIn("vazia", out2)
        self.assertEqual(len(self.srv.requests), 2)
        self.assertEqual(self.core.db.count_chat_messages(), 2)

    def test_http_error_not_stored_and_limited_calls(self):
        self.srv.script = [("status", 500, "x")] * 10
        out = self.ask("pergunta")
        self.assertIn("NVIDIA", out)
        self.assertEqual(len(self.srv.requests), 3)
        self.assertEqual(self.core.db.count_chat_messages(), 0)

    def test_timeout_not_stored(self):
        self.core.nvidia.timeout = 0.5
        self.core.nvidia.max_retries = 0
        self.srv.script = [("sleep", 2, "tarde")]
        out = self.ask("pergunta lenta")
        self.assertIn("demorou", out)
        self.assertEqual(self.core.db.count_chat_messages(), 0)

    def test_auth_error_message(self):
        self.srv.script = [("status", 401, "x")]
        out = self.ask("oi")
        self.assertIn("chave", out)
        self.assertEqual(len(self.srv.requests), 1)

    def test_duplicate_input_within_window_is_dropped(self):
        a = self.ask("mensagem repetida por callback duplo")
        b = self.ask("mensagem repetida por callback duplo", source="voice")
        self.assertTrue(a)
        self.assertEqual(b, "")
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(self.core.db.count_chat_messages(), 2)

    def test_concurrent_messages_do_not_mix(self):
        self.srv.script = [("sleep", 1.2, "[FELIZ] resposta A")]
        results = {}

        def run(key, text, delay):
            time.sleep(delay)
            results[key] = self.ask(text)

        t1 = threading.Thread(target=run, args=("a", "primeira pergunta longa", 0))
        t2 = threading.Thread(target=run, args=("b", "segunda pergunta ao mesmo tempo", 0.2))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertIn("resposta A", results["a"])
        self.assertIn("ainda estou respondendo", results["b"])
        self.assertEqual(len(self.srv.requests), 1)
        rows = self.core.db.get_chat_messages(10)
        self.assertEqual([r["content"] for r in rows if r["role"] == "user"], ["primeira pergunta longa"])

    def test_voice_busy_is_silent(self):
        self.srv.script = [("sleep", 1.0, "[FELIZ] ok")]
        t = threading.Thread(target=lambda: self.ask("pergunta demorada aqui"))
        t.start(); time.sleep(0.2)
        self.assertEqual(self.ask("fala captada durante a resposta", source="voice"), "")
        t.join()

    def test_not_ready_blocks_conversation(self):
        self.core.chat_ready.clear()
        self.assertIn("me apresentando", self.ask("oi"))
        self.assertEqual(self.ask("oi", source="voice"), "")
        self.assertEqual(self.srv.requests, [])

    def test_vision_context_not_in_normal_chat(self):
        self.core.last_visual_context = "uma janela de código com erro vermelho"
        self.core.db.add_history("Alicia (observação)", "observação automática da tela", "serio")
        self.ask("me explique o que é uma lista em python")
        req = self.srv.requests[-1]["messages"]
        self.assertNotIn("observação automática", json.dumps(req, ensure_ascii=False))
        self.assertNotIn("CONTEXTO VISUAL", req[0]["content"])

    def test_model_inventing_user_turn_is_cut(self):
        self.srv.script = [("ok", "[FELIZ] Claro.\nTester: e então?\nAlicia: continuo sozinha")]
        out = self.ask("diga algo")
        self.assertNotIn("continuo sozinha", out)
        self.assertNotIn("e então", out)

    def test_expression_tag_parsed(self):
        self.srv.script = [("ok", "[BRAVO] Isso não compila.")]
        out = self.ask("por que não compila?")
        self.assertTrue(out.startswith("[BRAVO]"))
        self.assertEqual(self.core.current_expression.value, "bravo")

    def test_persistence_across_restart(self):
        self.ask("meu projeto é um jogo de puzzle")
        core2 = self.make_core()
        self.assertEqual(core2.db.get_user_name(), "Tester")
        core2.interact("o que eu estava fazendo?")
        sent = self.srv.requests[-1]["messages"]
        self.assertTrue(any("jogo de puzzle" in m["content"] for m in sent))


class TestNameAndMemoryCommands(CoreCase):
    def setUp(self):
        super().setUp()
        # Começa sem nome salvo para testar a primeira execução.
        with self.core.db._conn() as c:
            c.execute("DELETE FROM user_profile")

    def test_first_run_asks_once_then_remembers(self):
        asked = []
        self.core._say = lambda *a, **k: None
        self.assertTrue(self.core.ensure_user_name(ask_fn=lambda p: asked.append(p) or "Anderson"))
        self.assertEqual(self.core.db.get_user_name(), "Anderson")
        core2 = self.make_core()
        core2.db.set_user_name("Anderson")  # make_core define "Tester"; simula o valor salvo
        def boom(_): raise AssertionError("não deveria perguntar de novo")
        self.assertTrue(core2.ensure_user_name(ask_fn=boom))
        self.assertEqual(len(asked), 1)

    def test_invalid_names_never_invent_one(self):
        self.core._say = lambda *a, **k: None
        answers = iter(["", "123", "!!!"])
        self.assertFalse(self.core.ensure_user_name(ask_fn=lambda p: next(answers)))
        self.assertIsNone(self.core.db.get_user_name())

    def test_legacy_config_name_is_migrated_not_asked(self):
        self.core.config.first_run = False
        self.core.config.user_name = "Beatriz"
        def boom(_): raise AssertionError("não deveria perguntar")
        self.assertTrue(self.core.ensure_user_name(ask_fn=boom))
        self.assertEqual(self.core.db.get_user_name(), "Beatriz")

    def test_rename_command(self):
        self.core.db.set_user_name("Antigo")
        out = self.ask("pode me chamar de Carlos")
        self.assertIn("Carlos", out)
        self.assertEqual(self.core.db.get_user_name(), "Carlos")
        self.assertEqual(self.srv.requests, [])  # comando local, sem chamar a API

    def test_remember_use_and_forget(self):
        out = self.ask("lembre que meu projeto atual é a Alicia")
        self.assertIn("Anotado", out)
        self.assertEqual(self.srv.requests, [])
        self.ask("qual é o meu projeto atual?")
        system = self.srv.requests[-1]["messages"][0]["content"]
        self.assertIn("meu projeto atual é a Alicia", system)
        self.assertIn("[MEMÓRIAS CONFIRMADAS —", system)
        self.ask("esqueça projeto atual")
        self.assertEqual(self.core.db.list_memories(), [])

    def test_commands_parsing_does_not_hijack_normal_sentences(self):
        for t in ["como funciona o sistema de combate do meu jogo?", "o nome do meu personagem é Rex",
                  "Bom dia, Alicia.", "você lembra do que falamos?"]:
            self.assertIsNone(parse_command(t), t)
        self.assertEqual(clean_name("Zé."), "Zé")
        self.assertIsNone(clean_name("123"))


class TestSourceIntegrity(unittest.TestCase):
    def test_no_undefined_names_in_package(self):
        import ast, builtins
        for path in (APP_DIR / "alicia_core").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defined = set(dir(builtins))
            for n in ast.walk(tree):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    defined |= {(a.asname or a.name).split(".")[0] for a in n.names}
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(n.name)
                elif isinstance(n, ast.arg):
                    defined.add(n.arg)
                elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    defined.add(n.id)
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    defined.add(n.name)
            used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            self.assertEqual(used - defined, set(), f"nomes sem definição em {path.name}")


if __name__ == "__main__":
    unittest.main()


# ======================================================================
class TestExtras(CoreCase):
    def test_new_context_keeps_history_but_stops_sending_it(self):
        self.ask("vamos falar do sistema de combate do jogo")
        out = self.ask("/novoassunto")
        self.assertIn("assunto novo", out)
        n_requests = len(self.srv.requests)
        self.ask("como faço um bolo de cenoura?")
        sent = json.dumps(self.srv.requests[-1]["messages"], ensure_ascii=False)
        self.assertEqual(len(self.srv.requests), n_requests + 1)
        self.assertNotIn("sistema de combate", sent)
        self.assertGreaterEqual(self.core.db.count_chat_messages(), 4)  # nada foi apagado

    def test_old_context_still_recallable_after_new_context(self):
        self.ask("vamos falar do sistema de combate do jogo")
        self.ask("/novoassunto")
        self.ask("lembra do que falamos sobre combate?")
        system = self.srv.requests[-1]["messages"][0]["content"]
        self.assertIn("[TRECHOS DE CONVERSAS ANTIGAS —", system)

    def test_backup_is_consistent_and_rotated(self):
        self.ask("uma conversa para guardar no backup")
        paths = []
        for i in range(7):
            time.sleep(1.01)
            paths.append(self.core.db.backup(keep=5))
        self.assertEqual(len(list(pathlib.Path("backups").glob("*.db"))), 5)
        restored = MemoryDatabase(paths[-1])
        self.assertEqual(restored.count_chat_messages(), 2)
        self.assertIsNone(self.core.db.backup(min_interval_hours=24))  # já existe um recente

    def test_export_and_help_and_backup_commands(self):
        self.core.db.add_memory("Prefiro respostas curtas")
        self.ask("uma mensagem qualquer para exportar")
        self.assertIn("exportada", self.ask("/exportar"))
        txt = next(pathlib.Path("exportacoes").glob("*.txt")).read_text(encoding="utf-8")
        self.assertIn("Prefiro respostas curtas", txt)
        self.assertIn("uma mensagem qualquer para exportar", txt)
        self.assertIn("Comandos", self.ask("/ajuda"))
        self.assertIn("Backup", self.ask("/backup"))


class TestLauncherHelpers(unittest.TestCase):
    def test_requirements_parsing_and_import_names(self):
        from alicia_core import envcheck
        specs = envcheck.parse_requirements(APP_DIR / "requirements-optional.txt", False)
        self.assertIn("edge-tts>=7.0.0", [s.line for s in specs])
        self.assertTrue(all("#" not in s.line for s in specs))
        mods = {s.name: s.info.module for s in envcheck.load_specs(APP_DIR)}
        self.assertEqual(mods["Pillow"], "PIL")
        self.assertEqual(mods["edge-tts"], "edge_tts")
        self.assertEqual(mods["SpeechRecognition"], "speech_recognition")
        self.assertEqual(mods["rapidocr-onnxruntime"], "rapidocr_onnxruntime")


class TestPersonality(CoreCase):
    def setUp(self):
        super().setUp()
        self.core._say = lambda *a, **k: None

    def system_of_last(self):
        return self.srv.requests[-1]["messages"][0]["content"]

    def test_first_run_asks_once_by_number_or_name_and_persists(self):
        answers = iter(["xyz", "4"])
        self.assertTrue(self.core.ensure_personality(ask_fn=lambda p: next(answers)))
        self.assertEqual(self.core.get_personality(), "malcriada")
        core2 = self.make_core()
        def boom(_): raise AssertionError("não deveria perguntar de novo")
        self.assertTrue(core2.ensure_personality(ask_fn=boom))
        self.assertEqual(core2.get_personality(), "malcriada")

    def test_three_invalid_answers_fall_back_to_normal(self):
        self.assertTrue(self.core.ensure_personality(ask_fn=lambda p: "???"))
        self.assertEqual(self.core.get_personality(), "normal")

    def test_all_styles_reach_the_prompt(self):
        for key, marker in (("brincalhona", "BRINCALHONA"), ("seria", "SÉRIA"), ("malcriada", "MALCRIADA"),
                            ("carinhosa", "CARINHOSA"), ("normal", "NORMAL")):
            self.core.set_personality(key)
            self.ask(f"pergunta de teste para o estilo {key}")
            self.assertIn(f"[ESTILO ATUAL: {marker}", self.system_of_last())

    def test_switch_midway_keeps_dialogue_and_memories(self):
        self.ask("lembre que meu projeto atual é a Alicia")
        self.ask("meu jogo é um tetris roguelite")
        before = self.core.db.get_chat_messages(100)
        out = self.ask("seja carinhosa")
        self.assertIn("carinhosa", out.lower())
        self.assertEqual(self.core.get_personality(), "carinhosa")
        self.ask("qual é o meu jogo mesmo?")
        sent = self.srv.requests[-1]["messages"]
        flat = json.dumps(sent, ensure_ascii=False)
        self.assertIn("tetris roguelite", flat)                 # diálogo anterior continua no contexto
        self.assertIn("meu projeto atual é a Alicia", flat)     # memória continua
        self.assertIn("[ESTILO ATUAL: CARINHOSA", sent[0]["content"])
        after = self.core.db.get_chat_messages(100)
        self.assertEqual([m["content"] for m in after[:len(before)]], [m["content"] for m in before])

    def test_switch_commands_are_local_and_menu_lists_options(self):
        n = len(self.srv.requests)
        self.ask("/personalidade seria")
        self.assertEqual(self.core.get_personality(), "seria")
        out = self.ask("/personalidade")
        self.assertIn("Séria", out)
        self.assertIn("Malcriada", out)
        self.assertEqual(len(self.srv.requests), n)

    def test_natural_phrases_switch_locally_even_with_punctuation(self):
        """Voz/teclado: 'Seja carinhosa.', 'alicia, muda pra malcriada', 'você pode ser mais séria?' trocam sem chamar a API."""
        n = len(self.srv.requests)
        for phrase, key in (("Seja carinhosa.", "carinhosa"), ("alicia, muda pra malcriada", "malcriada"),
                            ("você pode ser mais séria?", "seria"), ("quero a personalidade brincalhona", "brincalhona"),
                            ("volta ao normal!", "normal"), ("ativar modo carinhosa", "carinhosa")):
            self.ask(phrase)
            self.assertEqual(self.core.get_personality(), key, phrase)
        self.assertEqual(len(self.srv.requests), n)

    def test_phrases_that_only_mention_a_style_do_not_switch(self):
        self.core.set_personality("seria")
        for phrase in ("a Maria é muito carinhosa", "seja carinhosa com o Pedro quando escrever a mensagem", "isso seria bom",
                       "você está brincalhona hoje?", "muda de assunto", "o modo normal do windows"):
            self.ask(phrase)
            self.assertEqual(self.core.get_personality(), "seria", phrase)

    def test_style_is_the_last_block_of_the_system_prompt_and_reminds_in_the_user_turn(self):
        self.core.set_personality("carinhosa")
        self.ask("me ajuda com uma dúvida de python")
        req = self.srv.requests[-1]["messages"]
        system = req[0]["content"]
        self.assertTrue(system.rstrip().endswith("assunto em andamento."), system[-200:])
        self.assertGreater(system.index("[ESTILO ATUAL: CARINHOSA"), system.index("[REGRAS DA CONVERSA"))
        self.assertIn("SUBSTITUI o tom seco", system)
        self.assertIn("Exemplos do jeito de falar", system)
        self.assertIn("[Responda agora no estilo CARINHOSA", req[-1]["content"])
        self.assertEqual([m["content"] for m in self.core.db.get_chat_messages(10)][0], "me ajuda com uma dúvida de python")  # histórico guarda o original
        self.core.set_personality("normal")
        self.ask("outra pergunta qualquer")
        self.assertNotIn("[Responda agora no estilo", self.srv.requests[-1]["messages"][-1]["content"])

    def test_old_tone_in_history_stops_pulling_right_after_a_switch(self):
        self.srv.reply_fn = lambda msgs: "[SERIO] resposta no tom velho %d" % len(msgs)
        for i in range(4):
            self.ask(f"pergunta antiga número {i} sobre coisas variadas {i * 11}")
        self.ask("seja carinhosa")
        self.ask("e agora, o que você acha?")
        flat = json.dumps(self.srv.requests[-1]["messages"], ensure_ascii=False)
        self.assertNotIn("pergunta antiga número 0", flat)               # o tom velho antigo some do contexto...
        self.assertNotIn("pergunta antiga número 1", flat)
        self.assertIn("pergunta antiga número 3", flat)                  # ...mas a última troca fica, para o assunto não se perder
        for i in range(12):                                              # depois de várias respostas no estilo novo, o histórico volta inteiro
            self.ask(f"conversa nova número {i} com assuntos {i * 13}")
        flat = json.dumps(self.srv.requests[-1]["messages"], ensure_ascii=False)
        self.assertIn("conversa nova número 5", flat)

    def test_screen_and_gamer_prompts_carry_the_chosen_style(self):
        import types
        from alicia_core.prompts import PromptEngine as P
        snap = types.SimpleNamespace(active_window_title="Janela de teste")
        self.assertIn("ESTILO ESCOLHIDO PELO USUÁRIO: CARINHOSA", P.build_vision_prompt(snap, [], "Ana", personality="carinhosa"))
        self.assertNotIn("ESTILO ESCOLHIDO", P.build_vision_prompt(snap, [], "Ana"))
        direct = P.build_direct_screen_query_prompt("o que é isso?", snap, "Ana", personality="seria")
        self.assertIn("ESTILO ESCOLHIDO PELO USUÁRIO: SÉRIA", direct)
        self.assertNotIn("ESTILO ESCOLHIDO", P.build_direct_screen_query_prompt("o que é isso?", snap, "Ana"))

    def test_malcriada_style_forbids_real_offense(self):
        from alicia_core.prompts import PromptEngine
        self.assertIn("NUNCA ofenda", PromptEngine.PERSONALITIES["malcriada"]["style"])

    def test_resolve_aliases(self):
        from alicia_core.prompts import PromptEngine as P
        for text, key in (("Séria", "seria"), ("1", "normal"), ("mal-criada", "malcriada"), ("5", "carinhosa"), ("BRINCALHONA!", "brincalhona")):
            self.assertEqual(P.resolve_personality(text), key)
        self.assertIsNone(P.resolve_personality("grosseira"))


class TestModelSelection(TmpDirCase):
    def run_validate(self, srv, configured):
        import subprocess, sys, launcher
        pathlib.Path(".env").write_text("", encoding="utf-8")
        env = dict(os.environ, NVIDIA_API_KEY=FAKE_KEY, NVIDIA_MODEL=configured, NVIDIA_BASE_URL=srv.base_url,
                   PYTHONPATH=str(APP_DIR))
        r = subprocess.run([sys.executable, "-c", "import os;os.chdir(%r);" % str(APP_DIR) + launcher.VALIDATE_CODE],
                           capture_output=True, text=True, env=env, cwd=str(APP_DIR), timeout=120)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def srv(self, models):
        srv = MockNvidia(); self.addCleanup(srv.stop); srv.models = models
        return srv

    def test_configured_model_that_responds_is_kept(self):
        out = self.run_validate(self.srv(["test-model"]), "test-model")
        self.assertEqual((out["ok"], out["chosen"], out["changed"]), (True, "test-model", False))

    def test_preferred_model_is_tried_even_if_account_list_omits_it(self):
        srv = self.srv(["ai21labs/jamba-1.5-large-instruct"])   # lista da conta incompleta
        srv.script = [("status", 404, "Function not found")]    # o configurado falha
        out = self.run_validate(srv, "deepseek-ai/deepseek-v3.1")
        self.assertEqual(out["chosen"], "nvidia/nemotron-3-ultra-550b-a55b")
        self.assertTrue(out["changed"])
        self.assertFalse(out["listed"])
        self.assertEqual(out["tried"][0][1], "not_found")

    def test_failures_report_kind_and_detail_for_every_attempt(self):
        srv = self.srv(["zyphra/zamba2-7b-instruct"])
        srv.script = [("status", 400, "campo invalido")] * 40
        out = self.run_validate(srv, "x/y")
        self.assertIsNone(out["chosen"])
        self.assertGreaterEqual(len(out["tried"]), 10)
        self.assertEqual(out["tried"][0][:2], ["x/y", "bad_request"])
        self.assertIn("campo invalido", out["tried"][0][2])
        self.assertIn("zyphra/zamba2-7b-instruct", out["models"])

    def test_auth_error_stops_immediately(self):
        srv = self.srv(["a/instruct"]); srv.script = [("status", 401, "x")]
        out = self.run_validate(srv, "a/instruct")
        self.assertEqual((out["ok"], out["kind"]), (False, "auth"))
        self.assertEqual(len(srv.requests), 1)

    def test_candidate_order_skips_non_chat_models(self):
        from alicia_core.nvidia_client import chat_candidates
        c = chat_candidates(["nvidia/riva-translate-4b-instruct", "ibm/granite-34b-code-instruct", "qwen/qwen9-chat"], "mine/x")
        self.assertEqual(c[0], "mine/x")
        self.assertEqual(c[1], "nvidia/nemotron-3-ultra-550b-a55b")
        self.assertIn("qwen/qwen9-chat", c)
        self.assertNotIn("nvidia/riva-translate-4b-instruct", c)
        self.assertNotIn("ibm/granite-34b-code-instruct", c)


class TestAvatarAssetsAndMic(unittest.TestCase):
    def test_avatar_images_complete_consistent_and_transparent(self):
        from PIL import Image
        from alicia_core.enums import AliciaExpression
        sizes = set()
        names = [e.value for e in AliciaExpression] + ["feliz_2", "feliz_3"]
        for n in names:
            path = APP_DIR / "assets" / f"{n}.png"
            self.assertTrue(path.exists(), f"falta assets/{n}.png")
            im = Image.open(path)
            self.assertEqual(im.mode, "RGBA")
            sizes.add(im.size)
            alpha = im.getchannel("A")
            self.assertEqual(alpha.getpixel((2, 2)), 0, f"{n}: canto deveria ser transparente")
            self.assertGreater(alpha.getextrema()[1], 250)
        self.assertEqual(len(sizes), 1, "todas as expressões devem ter o mesmo tamanho de tela")

    def test_mic_without_pyaudio_is_disabled_quietly(self):
        from alicia_core import deps
        from alicia_core.voice_input import VoiceInputEngine
        if deps.HAS_MIC:
            self.skipTest("PyAudio presente neste computador")
        class _Core: pass
        eng = VoiceInputEngine(_Core())
        self.assertFalse(eng.enabled)
        eng.start_listening()          # não deve criar thread nem levantar erro
        self.assertFalse(eng.thread_ativa)


class TestMicrophoneBackend(unittest.TestCase):
    """Testa a captura via sounddevice com dispositivos FALSOS (o hardware real só se valida no seu PC)."""

    def setUp(self):
        import sys, types
        from alicia_core import audio_input, deps
        self.ai, self.deps = audio_input, deps
        self._saved = (audio_input.sd, audio_input.sr, audio_input.HAS_SOUNDDEVICE, audio_input.HAS_SPEECH, audio_input._SD_CLASS)

        class FakeStream:
            def __init__(s, **kw): s.kw, s.started, s.closed = kw, False, False
            def start(s): s.started = True
            def stop(s): s.started = False
            def close(s): s.closed = True
            def read(s, n): return (b"\x01\x00" * n, False)

        class FakeSD:
            default = types.SimpleNamespace(device=(3, 5), hostapi=0)
            RawInputStream = FakeStream
            @staticmethod
            def query_devices(device=None, kind=None):
                devs = [{"name": "Alto-falante", "max_input_channels": 0, "default_samplerate": 48000.0},
                        {"name": "Microfone USB", "max_input_channels": 1, "default_samplerate": 44100.0},
                        {"name": "Mic duplicado (outra API)", "max_input_channels": 2, "default_samplerate": 44100.0},
                        {"name": "Microfone (Realtek)", "max_input_channels": 2, "default_samplerate": 48000.0}]
                return devs if device is None else devs[device]
            @staticmethod
            def query_hostapis(i): return {"devices": [0, 1, 3]}
            @staticmethod
            def check_input_settings(**kw):
                if kw["device"] == 3: raise ValueError("16 kHz não suportado")

        class FakeSR:
            class AudioSource: pass

        audio_input.sd, audio_input.sr = FakeSD, FakeSR
        audio_input.HAS_SOUNDDEVICE = audio_input.HAS_SPEECH = True
        audio_input._SD_CLASS = None
        self.FakeSD = FakeSD

    def tearDown(self):
        a = self.ai
        a.sd, a.sr, a.HAS_SOUNDDEVICE, a.HAS_SPEECH, a._SD_CLASS = self._saved

    def test_lists_only_input_devices_of_default_api(self):
        self.assertEqual(self.ai.list_microphones(), [(1, "Microfone USB"), (3, "Microfone (Realtek)")])

    def test_open_default_device_streams_int16_mono(self):
        mic = self.ai.open_microphone(None)
        self.assertEqual(mic.SAMPLE_WIDTH, 2)
        self.assertEqual(mic.SAMPLE_RATE, 44100 if False else 48000)  # device 3: 16 kHz recusado -> taxa nativa
        with mic as src:
            self.assertEqual(len(src.stream.read(8)), 16)
            raw = src._raw
            self.assertTrue(raw.started)
            self.assertEqual(raw.kw["channels"], 1)
            self.assertEqual(raw.kw["dtype"], "int16")
        self.assertTrue(raw.closed)
        self.assertIsNone(mic.stream)

    def test_prefers_16khz_when_supported(self):
        self.assertEqual(self.ai.open_microphone(1).SAMPLE_RATE, 16000)

    def test_no_default_input_device_gives_clear_error(self):
        self.FakeSD.default = __import__("types").SimpleNamespace(device=(-1, 5), hostapi=0)
        with self.assertRaises(OSError) as cm:
            self.ai.open_microphone(None)
        self.assertIn("microfone", str(cm.exception).lower())


class TestAudioOutputWindowsMCI(unittest.TestCase):
    """Sequência de comandos MCI com um winmm FALSO (a reprodução real só se valida no seu Windows)."""

    def setUp(self):
        from alicia_core import audio_output
        self.ao = audio_output
        self._saved = (audio_output.AUDIO_BACKEND, audio_output._mci)
        self.sent = []
        self.modes = ["playing", "playing", "stopped"]

        def fake_mci(cmd):
            self.sent.append(cmd)
            if cmd.startswith("status"):
                return self.modes.pop(0) if self.modes else "stopped"
            return ""
        audio_output.AUDIO_BACKEND = "mci"
        audio_output._mci = fake_mci

    def tearDown(self):
        self.ao.AUDIO_BACKEND, self.ao._mci = self._saved

    def test_play_opens_plays_waits_and_closes(self):
        self.ao.play_mp3(r"C:\Users\ander\AppData\Local\Temp\fala.mp3")
        cmds = [c.split()[0] for c in self.sent]
        self.assertIn('open "C:\\Users\\ander\\AppData\\Local\\Temp\\fala.mp3" type mpegvideo alias alicia_voice', self.sent)
        self.assertEqual(cmds.count("status"), 3)          # esperou até "stopped"
        self.assertEqual(self.sent[-1], "close alicia_voice")  # sempre fecha

    def test_stop_playback_interrupts(self):
        self.modes = ["playing"] * 1000
        threading.Timer(0.4, self.ao.stop_playback).start()
        t0 = time.time()
        self.ao.play_mp3("x.mp3")
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(self.sent[-1], "close alicia_voice")

    def test_errors_still_close_the_device(self):
        def broken(cmd):
            self.sent.append(cmd)
            if cmd.startswith("play"):
                raise OSError("falhou")
            return ""
        self.ao._mci = broken
        with self.assertRaises(OSError):
            self.ao.play_mp3("x.mp3")
        self.assertEqual(self.sent[-1], "close alicia_voice")

    def test_no_pygame_requirement(self):
        reqs = (APP_DIR / "requirements.txt").read_text() + (APP_DIR / "requirements-optional.txt").read_text()
        self.assertNotIn("pygame", reqs.replace("sem pygame", "").replace("não precisa de pygame", ""))


class TestPayloadTiersAndThinking(unittest.TestCase):
    def setUp(self):
        self.srv = MockNvidia()
        self.addCleanup(self.srv.stop)

    def client(self):
        return NvidiaClient(FAKE_KEY, "m", self.srv.base_url, timeout=2, max_retries=0)

    def test_first_request_disables_thinking(self):
        self.client().chat([{"role": "user", "content": "x"}])
        body = self.srv.requests[0]
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertIn("temperature", body)

    def test_steps_down_one_tier_and_remembers(self):
        c = self.client()
        self.srv.script = [("status", 400, "unknown field top_p"), ("ok", "oi")]
        self.assertEqual(c.chat([{"role": "user", "content": "x"}]), "oi")
        self.assertEqual(set(self.srv.requests[1]), {"model", "messages", "max_tokens", "chat_template_kwargs"})
        c.chat([{"role": "user", "content": "y"}])
        self.assertEqual(set(self.srv.requests[2]), {"model", "messages", "max_tokens", "chat_template_kwargs"})

    def test_steps_down_to_minimal(self):
        c = self.client()
        self.srv.script = [("status", 400, "a"), ("status", 422, "b"), ("ok", "oi")]
        self.assertEqual(c.chat([{"role": "user", "content": "x"}]), "oi")
        self.assertEqual(set(self.srv.requests[2]), {"model", "messages", "max_tokens"})

    def test_all_tiers_rejected_fails_cleanly(self):
        c = self.client()
        self.srv.script = [("status", 400, "a"), ("status", 400, "b"), ("status", 404, "Function not found")]
        with self.assertRaises(BrainError) as cm:
            c.chat([{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.kind, "not_found")
        self.assertEqual(len(self.srv.requests), 3)

    def test_410_end_of_life_is_reported_as_gone(self):
        self.srv.script = [("status", 410, "The model has reached its end of life")]
        with self.assertRaises(BrainError) as cm:
            self.client().chat([{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.kind, "gone")
        self.assertEqual(len(self.srv.requests), 1)
        self.assertIn("Iniciar_Alicia.bat", cm.exception.user_message)




class TestSelfHealingModel(CoreCase):
    def test_dead_model_is_replaced_automatically_once(self):
        pathlib.Path(".env").write_text("NVIDIA_MODEL=old/gone\n", encoding="utf-8")
        self.core.nvidia.model = "old/gone"
        self.srv.bad_models = {"old/gone": (410, "The model has reached its end of life")}
        out = self.ask("olá, tudo bem?")
        self.assertNotIn("descontinuado", out)
        self.assertEqual(self.core.nvidia.model, "nvidia/nemotron-3-ultra-550b-a55b")
        self.assertIn("NVIDIA_MODEL=nvidia/nemotron-3-ultra-550b-a55b", pathlib.Path(".env").read_text())
        self.assertEqual(self.core.db.count_chat_messages(), 2)

    def test_no_working_model_reports_error_without_looping(self):
        self.core.nvidia.model = "old/gone"
        from alicia_core.nvidia_client import PREFERRED_MODELS
        self.srv.bad_models = {m: (404, "Not found for account") for m in PREFERRED_MODELS + ["old/gone"]}
        self.srv.models = []
        out = self.ask("olá?")
        self.assertIn("Iniciar_Alicia.bat", out)
        n = len(self.srv.requests)
        out2 = self.ask("olá de novo?")           # não procura de novo na mesma execução
        self.assertIn("Iniciar_Alicia.bat", out2)
        self.assertEqual(len(self.srv.requests), n + 1)
        self.assertEqual(self.core.db.count_chat_messages(), 0)


# ======================================================================
class TestVisionAndVoiceCommands(CoreCase):
    """Fluxo completo de "Alicia, olha": comando -> captura -> visão -> resposta -> histórico."""

    def setUp(self):
        super().setUp()
        import base64, io
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (64, 64), (10, 120, 200)).save(buf, "JPEG")
        self.b64 = base64.b64encode(buf.getvalue()).decode()
        self.core.screen_engine.capture_and_encode = lambda: (self.b64, Image.new("RGB", (64, 64)), "hash123")
        # Estes testes cobrem o caminho de RESERVA (modelo de visão remoto da NVIDIA). O olhar principal agora é local
        # (ver tests/test_observer.py): aqui simulamos que ele falhou e que o envio remoto foi autorizado.
        def _no_local_capture(*a, **k):
            raise RuntimeError("captura local indisponível neste teste")
        self.core.screen_engine.capture_raw = _no_local_capture
        self.core.vision.allow_remote = True
        self.srv.reply_fn = lambda msgs: ("[SURPRESA] Vejo uma tela azul." if isinstance(msgs[-1]["content"], list)
                                          else "[FELIZ] Resposta de texto.")
        self.core.vision.on_model_change = None

    def test_all_wake_variants_trigger_analysis(self):
        for phrase in ("Alicia, olha", "Alícia, ver", "Alicia olhar isso", "Alicia, por favor olha", "olha isso, Alicia"):
            self.core.vision.remote = None
            self.core.vision._discovery_done_at = 0
            out = self.core.interact(phrase, source="voice")
            self.assertIn("tela azul", out, phrase)

    def test_remote_vision_fallback_sends_real_image_payload(self):
        out = self.ask("Alicia, olha")
        self.assertTrue(out.startswith("[SURPRESA]"))
        img_requests = [r for r in self.srv.requests if isinstance(r["messages"][-1]["content"], list)]
        parts = img_requests[-1]["messages"][-1]["content"]
        self.assertEqual({p["type"] for p in parts}, {"text", "image_url"})
        url = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
        self.assertTrue(url.startswith("data:image/jpeg;base64,/9j/"))
        # grava o par pergunta/resposta com o evento certo, e a visão não entra como fala do usuário
        rows = self.core.db.get_chat_messages(10)
        self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
        self.assertEqual(rows[0]["content"], "Alicia, olha")

    def test_no_vision_anywhere_gives_clear_message_and_is_not_saved(self):
        self.srv.bad_models = {m: (404, "Not found") for m in ["nvidia/nemotron-3.5-super-vl-120b-a12b", "moonshotai/kimi-k3", "z-ai/glm-5.3", "google/gemma-4-31b-it"]}
        self.srv.models = []
        out = self.ask("Alicia, olha")
        self.assertIn("visão não está disponível", out)
        self.assertEqual(self.core.db.count_chat_messages(), 0)
        n = len(self.srv.requests)
        self.ask("Alicia, ver")                       # não repete a busca por 30 min
        self.assertEqual(len(self.srv.requests), n)

    def test_direct_command_works_even_if_spontaneous_watch_is_off(self):
        self.core.screen_watch_enabled = False
        self.assertIn("tela azul", self.ask("Alicia, olha"))

    def test_normal_sentences_do_not_trigger_vision(self):
        for t in ("Alicia, você viu o jogo ontem?", "como faço um bolo?", "Bom dia, Alicia."):
            self.srv.requests.clear()
            self.ask(t)
            for r in self.srv.requests:
                self.assertFalse(isinstance(r["messages"][-1]["content"], list), t)


class TestCliLoopEndToEnd(CoreCase):
    """A janela de teclado de verdade: nome -> personalidade -> saudação -> conversa -> encerramento."""

    def test_first_run_flow_through_cli_loop(self):
        import builtins
        with self.core.db._conn() as c:
            c.execute("DELETE FROM user_profile")
        self.core.chat_ready.clear()
        self.core.config.first_run = True
        self.core.config.user_name = "Usuário"
        self.core._say = lambda *a, **k: None
        script = iter(["Anderson", "3", "Bom dia!", "meu jogo é um tetris roguelite", "/personalidade carinhosa", "qual é o meu jogo?"])

        def fake_input(prompt=""):
            try:
                return next(script)
            except StopIteration:
                raise EOFError

        real_input = builtins.input
        builtins.input = fake_input
        try:
            t = threading.Thread(target=self.core.cli_loop, daemon=True)
            t.start(); t.join(timeout=60)
        finally:
            builtins.input = real_input
        self.assertFalse(t.is_alive(), "o loop do teclado não terminou")
        self.assertEqual(self.core.db.get_user_name(), "Anderson")
        self.assertEqual(self.core.get_personality(), "carinhosa")
        self.assertTrue(self.core.chat_ready.is_set())
        rows = self.core.db.get_chat_messages(50)
        users = [r["content"] for r in rows if r["role"] == "user"]
        self.assertEqual(users, ["Bom dia!", "meu jogo é um tetris roguelite", "/personalidade carinhosa", "qual é o meu jogo?"])
        last_system = self.srv.requests[-1]["messages"][0]["content"]
        self.assertIn("[ESTILO ATUAL: CARINHOSA", last_system)
        self.assertIn("Anderson", last_system)
        # a pergunta final enxergou a conversa anterior
        self.assertIn("tetris roguelite", json.dumps(self.srv.requests[-1]["messages"], ensure_ascii=False))


class TestSelfTestScript(TmpDirCase):
    def test_report_is_generated_without_secrets(self):
        import subprocess, sys
        srv = MockNvidia(); self.addCleanup(srv.stop)
        srv.reply_fn = lambda m: "[SURPRESA] Vejo uma tela." if isinstance(m[-1]["content"], list) else ("[FELIZ] São 56." if "7 vezes 8" in m[-1]["content"] else "[FELIZ] Bom dia!")
        code = (
            "import sys, runpy; sys.path.insert(0, %r)\n"
            "import tests.support\n"
            "from PIL import Image\n"
            "from alicia_core import screen\n"
            "screen.ScreenAnalyzerEngine._capture = lambda self: Image.new('RGB', (800, 600), (10, 90, 160))\n"
            "from alicia_core.hardware import HardwareMonitor\n"
            "HardwareMonitor.get_active_process_name = staticmethod(lambda: 'code.exe')\n"
            "HardwareMonitor.get_active_window_title = staticmethod(lambda: 'main.py - Alicia - Visual Studio Code')\n"
            "sys.argv = ['teste_completo.py', '--sem-audio']\n"
            "runpy.run_path(%r, run_name='__main__')\n"
        ) % (str(APP_DIR), str(APP_DIR / "teste_completo.py"))
        env = dict(os.environ, NVIDIA_API_KEY=FAKE_KEY, NVIDIA_MODEL="test-model", NVIDIA_BASE_URL=srv.base_url)
        subprocess.run([sys.executable, "-c", code], cwd=str(APP_DIR), env=env, capture_output=True, text=True, timeout=300)
        report = (APP_DIR / "relatorio_alicia.txt").read_text(encoding="utf-8")
        for n in ("2. Importar", "3. Banco", "4. API", "5. Conversa", "6. Reconhecimento", "7. Captura", "8. Visão"):
            resumo = report.split("=== RESUMO ===")[1]
            line = next(l for l in resumo.splitlines() if l.startswith("[") and n in l)
            self.assertTrue(line.startswith("[OK"), line)
        self.assertNotIn(FAKE_KEY, report)
        (APP_DIR / "relatorio_alicia.txt").unlink()


# ======================================================================
class TestVoiceSegmenterNoClippedStart(unittest.TestCase):
    """Reproduz o defeito 'só captura depois da metade da frase' com sinais sintéticos."""

    RATE, CHUNK = 16000, 1024

    def tone(self, rms, freq=300.0, n=None):
        import array, math
        n = n or self.CHUNK
        amp = rms * math.sqrt(2)
        a = array.array("h", [int(amp * math.sin(2 * math.pi * freq * i / self.RATE)) for i in range(n)])
        return a.tobytes()

    def seg(self, **kw):
        from alicia_core.voice_input import UtteranceSegmenter
        return UtteranceSegmenter(self.RATE, self.CHUNK, floor=250, **kw)

    def run_chunks(self, seg, chunks):
        out = []
        for c in chunks:
            r = seg.feed(c)
            if r is not None:
                out.append(r)
        return out

    def test_soft_onset_is_included_by_preroll(self):
        seg = self.seg()
        noise = [self.tone(25, 90 + i) for i in range(40)]          # calibra + silêncio (>2 s)
        ramp = [self.tone(70, 411.0), self.tone(120, 523.0), self.tone(350, 330.0), self.tone(1500, 330.0)]
        speech = [self.tone(1800, 300 + (i % 5) * 7) for i in range(40)]
        tail = [self.tone(25, 90 + i) for i in range(30)]
        out = self.run_chunks(seg, noise + ramp + speech + tail)
        self.assertEqual(len(out), 1)
        # o 1º bloco do começo (abaixo do limiar!) está dentro da frase gravada
        self.assertIn(ramp[0], out[0])
        self.assertIn(ramp[1], out[0])
        self.assertGreaterEqual(len(out[0]), sum(len(c) for c in ramp + speech))

    def test_old_behaviour_would_have_missed_it_threshold_is_above_onset(self):
        seg = self.seg()
        self.run_chunks(seg, [self.tone(25, 90 + i) for i in range(30)])
        self.assertGreater(seg.threshold, 120)   # confirma: o começo suave NÃO passa do limiar sozinho

    def test_two_phrases_are_split_and_noise_alone_gives_nothing(self):
        seg = self.seg()
        noise = lambda n: [self.tone(25, 90 + i) for i in range(n)]
        talk = lambda n: [self.tone(1500, 300 + (i % 4) * 11) for i in range(n)]
        out = self.run_chunks(seg, noise(40) + talk(20) + noise(30) + talk(20) + noise(30))
        self.assertEqual(len(out), 2)
        self.assertEqual(self.run_chunks(self.seg(), noise(400)), [])

    def test_short_click_is_ignored(self):
        seg = self.seg()
        noise = lambda n: [self.tone(25, 90 + i) for i in range(n)]
        out = self.run_chunks(seg, noise(30) + [self.tone(4000, 1000)] + noise(40))
        self.assertEqual(out, [])

    def test_very_long_speech_is_cut_not_unbounded(self):
        seg = self.seg()
        out = self.run_chunks(seg, [self.tone(25, 90 + i) for i in range(30)] + [self.tone(1500, 300 + (i % 4) * 11) for i in range(600)])
        self.assertGreaterEqual(len(out), 1)
        for o in out:
            self.assertLessEqual(len(o) / 2 / self.RATE, 25.5)

    def test_block_discards_pending_audio_while_alicia_speaks(self):
        seg = self.seg()
        self.run_chunks(seg, [self.tone(25, 90 + i) for i in range(30)] + [self.tone(1500, 300 + i) for i in range(5)])
        self.assertTrue(seg.recording)
        seg.block()
        self.assertFalse(seg.recording)
        self.assertEqual(len(seg.preroll), 0)


class TestVoiceEngineNeverStopsListening(unittest.TestCase):
    """Duas frases seguidas; o reconhecimento da 1ª demora — a 2ª NÃO pode ser perdida."""

    RATE, CHUNK = 16000, 1024

    def make(self, script, flip=None):
        import types
        from alicia_core import voice_input
        tone = TestVoiceSegmenterNoClippedStart().tone
        tone_obj = TestVoiceSegmenterNoClippedStart()
        tone_obj.RATE, tone_obj.CHUNK = self.RATE, self.CHUNK

        class Expr: value = "feliz"
        tts = types.SimpleNamespace(is_speaking=False, last_speech_finish_time=0.0, spoken=[],
                                    speak=lambda t, e: None)
        cfg = types.SimpleNamespace(mic_index=None, mic_energy_floor=250, mic_recalibration_interval=9999,
                                    mic_recalibration_interval_gamer=9999, mic_fails_before_recalibration=5,
                                    save=lambda: None)
        core = types.SimpleNamespace(is_running=True, config=cfg, tts=tts, modo_gamer=False, modo_descanso=False,
                                     answered=[], parse_expression_tags=lambda r: (r, Expr()),
                                     call_listener=types.SimpleNamespace(active=False))
        core.interact = lambda text, source="voice": core.answered.append(text) or "[FELIZ] " + text

        state = {"i": 0}
        chunks = script

        class Stream:
            def read(s, n):
                i = state["i"]
                state["i"] += 1
                if flip and i in flip:
                    flip[i](tts)
                if i < len(chunks):
                    return chunks[i]
                time.sleep(0.002)
                return tone_obj.tone(25, 90)

        class Src:
            SAMPLE_RATE, CHUNK, SAMPLE_WIDTH = self.RATE, self.CHUNK, 2
            stream = Stream()
            def __enter__(s): return s
            def __exit__(s, *a): return False

        voice_input.open_microphone = lambda idx: Src()
        eng = voice_input.VoiceInputEngine(core)
        eng.enabled = True
        return eng, core, tone_obj

    def test_second_phrase_survives_slow_recognition(self):
        from alicia_core import voice_input
        saved = voice_input.open_microphone
        try:
            probe = TestVoiceSegmenterNoClippedStart(); probe.RATE, probe.CHUNK = self.RATE, self.CHUNK
            noise = lambda n: [probe.tone(25, 90 + i) for i in range(n)]
            talk = lambda base, n: [probe.tone(1500, base + (i % 4) * 11) for i in range(n)]
            script = noise(40) + talk(300, 20) + noise(30) + talk(500, 20) + noise(40)
            eng, core, _ = self.make(script)
            order = []

            def fake_transcribe(data, rate):
                order.append(len(data))
                if len(order) == 1:
                    time.sleep(1.0)            # reconhecimento lento da 1ª frase
                return f"frase {len(order)}"
            eng._transcribe = fake_transcribe
            eng.start_listening()
            t0 = time.time()
            while len(core.answered) < 2 and time.time() - t0 < 20:
                time.sleep(0.05)
            core.is_running = False
            eng.stop_listening()
            self.assertEqual(core.answered, ["frase 1", "frase 2"])
        finally:
            voice_input.open_microphone = saved

    def test_speech_while_alicia_talks_is_ignored_but_reply_right_after_is_kept(self):
        from alicia_core import voice_input
        saved = voice_input.open_microphone
        try:
            probe = TestVoiceSegmenterNoClippedStart(); probe.RATE, probe.CHUNK = self.RATE, self.CHUNK
            noise = lambda n: [probe.tone(25, 90 + i) for i in range(n)]
            talk = lambda base, n: [probe.tone(1500, base + (i % 4) * 11) for i in range(n)]
            # [0..39] calibra | [40..59] ELA fala e há "eco" | [60..] silêncio | resposta do usuário logo depois
            script = noise(40) + talk(300, 20) + noise(10) + talk(500, 20) + noise(40)

            def start_talking(tts): tts.is_speaking = True
            def stop_talking(tts):
                tts.is_speaking = False
                tts.last_speech_finish_time = time.time() - 100     # já passou o cooldown
            eng, core, _ = self.make(script, flip={40: start_talking, 60: stop_talking})
            eng._transcribe = lambda data, rate: "resposta do usuário"
            eng.start_listening()
            t0 = time.time()
            while not core.answered and time.time() - t0 < 20:
                time.sleep(0.05)
            time.sleep(0.5)
            core.is_running = False
            eng.stop_listening()
            self.assertEqual(core.answered, ["resposta do usuário"])    # só 1: o eco foi ignorado
        finally:
            voice_input.open_microphone = saved


class TestPython314Compatibility(unittest.TestCase):
    def test_no_stacked_classmethod_staticmethod(self):
        """@classmethod + @staticmethod empilhados funcionavam até o Python 3.12 e quebram no 3.13/3.14."""
        import ast
        bad = []
        for f in APP_DIR.rglob("*.py"):
            if ".venv" in f.parts:
                continue
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names = [getattr(d, "id", getattr(d, "attr", "")) for d in n.decorator_list]
                    if "classmethod" in names and "staticmethod" in names:
                        bad.append(f"{f.name}:{n.name}")
        self.assertEqual(bad, [])

    def test_hardware_snapshot_and_static_helpers_callable(self):
        from alicia_core.hardware import HardwareMonitor
        snap = HardwareMonitor.capture_snapshot()
        self.assertIsInstance(snap.active_process_name, str)
        self.assertIsInstance(HardwareMonitor.get_active_process_name(), str)
        self.assertIsInstance(HardwareMonitor.get_active_window_title(), str)

    def test_no_removed_stdlib_modules_imported(self):
        import ast
        removed = {"audioop", "aifc", "cgi", "cgitb", "imghdr", "pipes", "telnetlib", "distutils", "imp", "asyncore", "asynchat", "smtpd"}
        found = []
        for f in (APP_DIR / "alicia_core").rglob("*.py"):
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                mods = [a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import) else \
                       [n.module.split(".")[0]] if isinstance(n, ast.ImportFrom) and n.module else []
                found += [f"{f.name}:{m}" for m in mods if m in removed]
        self.assertEqual(found, [])


class TestBackgroundLoopsDoNotSpamErrors(CoreCase):
    def test_visual_loop_runs_without_errors(self):
        import logging
        records = []
        class H(logging.Handler):
            def emit(s, r): records.append(r)
        h = H(); lg = logging.getLogger("AliciaCore"); lg.addHandler(h)
        try:
            t = threading.Thread(target=self.core.screen_observation_loop, daemon=True)
            t.start(); time.sleep(1.5)
            self.core.is_running = False
            t.join(timeout=10)
        finally:
            lg.removeHandler(h)
        errors = [r.getMessage() for r in records if r.levelno >= logging.ERROR]
        self.assertEqual(errors, [])

    def test_repeated_error_is_logged_once_with_backoff(self):
        import logging
        records = []
        class H(logging.Handler):
            def emit(s, r): records.append(r)
        h = H(); lg = logging.getLogger("AliciaCore"); lg.addHandler(h)
        try:
            waits = [self.core._loop_error("loop de teste", TypeError("mesmo erro")) for _ in range(6)]
        finally:
            lg.removeHandler(h)
        self.assertEqual(len([r for r in records if r.levelno >= logging.ERROR]), 1)   # sem spam
        self.assertEqual(waits[:4], [2.0, 4.0, 8.0, 16.0])
        self.assertLessEqual(max(waits), 30.0)


class TestWindowsBackgroundThrottlingFix(unittest.TestCase):
    """O Windows real não está aqui: verificamos que chamamos as APIs certas, com os argumentos certos."""

    def setUp(self):
        import ctypes
        from alicia_core import winperf
        self.wp = winperf
        self.calls = []
        outer = self

        class FakeK32:
            def GetCurrentProcess(s): return -1
            def GetCurrentThread(s): return -2
            def SetProcessInformation(s, h, cls, ptr, size):
                st = ptr._obj
                outer.calls.append(("SetProcessInformation", h, cls, st.Version, st.ControlMask, st.StateMask, size))
                return 1
            def SetThreadInformation(s, h, cls, ptr, size):
                st = ptr._obj
                outer.calls.append(("SetThreadInformation", h, cls, st.Version, st.ControlMask, st.StateMask))
                return 1
            def SetThreadPriority(s, h, p):
                outer.calls.append(("SetThreadPriority", h, p)); return 1
            def GetStdHandle(s, n): return 7
            def GetConsoleMode(s, h, ptr):
                ptr._obj.value = 0x01F7   # QuickEdit (0x40) ligado
                return 1
            def SetConsoleMode(s, h, mode):
                outer.calls.append(("SetConsoleMode", h, mode)); return 1

        class FakeWinmm:
            def timeBeginPeriod(s, ms):
                outer.calls.append(("timeBeginPeriod", ms)); return 0

        self._saved = (winperf._is_windows, winperf._kernel32, winperf._winmm)
        winperf._is_windows = lambda: True
        winperf._kernel32 = lambda: FakeK32()
        winperf._winmm = lambda: FakeWinmm()

    def tearDown(self):
        self.wp._is_windows, self.wp._kernel32, self.wp._winmm = self._saved

    def test_process_power_throttling_is_turned_off(self):
        res = self.wp.harden_process()
        self.assertTrue(all(res.values()), res)
        call = next(c for c in self.calls if c[0] == "SetProcessInformation")
        _, handle, info_class, version, control, state, size = call
        self.assertEqual((info_class, version), (4, 1))              # ProcessPowerThrottling, versão 1
        self.assertEqual(control, 0x1 | 0x4)                         # execução + resolução de timer
        self.assertEqual(state, 0)                                   # 0 = SEM economia de energia
        self.assertEqual(size, 12)
        self.assertIn(("timeBeginPeriod", 1), self.calls)

    def test_quickedit_is_cleared_so_clicking_console_does_not_pause(self):
        self.wp.harden_process()
        mode = next(c[2] for c in self.calls if c[0] == "SetConsoleMode")
        self.assertEqual(mode & 0x40, 0)         # QuickEdit desligado
        self.assertNotEqual(mode & 0x80, 0)      # flags estendidas ligadas (necessário para valer)

    def test_capture_thread_gets_priority_and_no_throttling(self):
        self.assertTrue(self.wp.boost_current_thread())
        self.assertIn(("SetThreadPriority", -2, 1), self.calls)
        t = next(c for c in self.calls if c[0] == "SetThreadInformation")
        self.assertEqual((t[2], t[4], t[5]), (3, 0x1, 0))

    def test_noop_off_windows_and_never_raises(self):
        self.wp._is_windows = lambda: False
        self.assertEqual(self.wp.harden_process(), {"power_throttling_off": False, "timer_1ms": False, "quickedit_off": False})
        self.assertFalse(self.wp.boost_current_thread())
        self.wp._is_windows = lambda: True
        self.wp._kernel32 = lambda: (_ for _ in ()).throw(OSError("sem kernel32"))
        self.assertFalse(self.wp.harden_process()["power_throttling_off"])   # não propaga exceção

    def test_main_applies_hardening_before_starting(self):
        src = (APP_DIR / "main.py").read_text(encoding="utf-8")
        self.assertLess(src.index("harden_process()"), src.index("AliciaAssistantCore(config_path"))


class TestMeasureCapture(unittest.TestCase):
    def test_detects_gaps_and_overflows(self):
        from alicia_core.audio_input import measure_capture
        import types
        calls = {"n": 0}
        class Stream:
            overflows = 3
            def read(s, n):
                calls["n"] += 1
                if calls["n"] == 5:
                    time.sleep(0.6)            # o Windows "adormeceu" a thread por 0,6 s
                time.sleep(0.01)
                return b"\x10\x00" * n
        src = types.SimpleNamespace(stream=Stream(), CHUNK=1024, SAMPLE_RATE=16000)
        r = measure_capture(src, 1.2)
        self.assertGreaterEqual(r["max_gap"], 0.6)
        self.assertEqual(r["overflows"], 3)
        self.assertEqual(r["peak"], 16)


# ======================================================================
class TestAvatarAnimator(unittest.TestCase):
    MANIFEST = {"expression": "serio", "idle": {"frames": 18, "fps": 10},
                "blink": {"frames": 4, "sequence": [[2, 0.05], [3, 0.08], [4, 0.06]]}, "talk": {"frames": 4, "fps": 8}}

    def anim(self, seed=1):
        import random
        from alicia_core.ui.avatar_animator import AvatarAnimator
        return AvatarAnimator(self.MANIFEST, rng=random.Random(seed))

    def run_for(self, a, seconds, speaking=False, expr="serio", t0=0.0, step=0.04):
        keys, t = [], t0
        while t < t0 + seconds:
            keys.append((t, a.update(expr, speaking, t)))
            t += step
        return keys

    def test_idle_loops_through_all_breathing_frames(self):
        keys = [k for _, k in self.run_for(self.anim(), 3.5) if k and k.startswith("idle_")]
        self.assertEqual({k for k in keys if k.startswith("idle_")}, {f"idle_{i:02d}" for i in range(1, 19)})

    def test_blinks_at_irregular_intervals_and_returns_to_breathing(self):
        a = self.anim()
        seq = self.run_for(a, 60)
        starts = [t for (t, k), (_, prev) in zip(seq[1:], seq) if k == "blink_2" and prev != "blink_2"]
        self.assertGreaterEqual(len(starts), 8)
        gaps = [b - a_ for a_, b in zip(starts, starts[1:])]
        self.assertTrue(all(g >= 0.1 for g in gaps))                    # nunca grudado
        self.assertTrue(any(g > 2.4 for g in gaps))                     # há intervalos longos
        self.assertGreater(len(set(round(g, 1) for g in gaps)), 4)      # não é regular como relógio
        for i, (_, k) in enumerate(seq):                                # cada piscar passa por 2 -> 3 -> 4
            if k == "blink_3":
                self.assertIn("blink_2", [x for _, x in seq[max(0, i - 3):i]])
        self.assertTrue(seq[-1][1].startswith(("idle_", "blink_")))

    def test_talking_cycles_mouth_shapes_then_closes_and_resumes_breathing(self):
        a = self.anim()
        self.run_for(a, 1.0)
        talk = self.run_for(a, 3.0, speaking=True, t0=1.0)
        shapes = [k for _, k in talk]
        self.assertTrue(all(k.startswith("talk_") for k in shapes))
        self.assertEqual({k for k in shapes}, {"talk_1", "talk_2", "talk_3", "talk_4"})
        changes = sum(1 for x, y in zip(shapes, shapes[1:]) if x != y)
        self.assertGreater(changes, 12)                                  # ritmo de fala, não parada
        after = self.run_for(a, 1.0, speaking=False, t0=4.0)
        self.assertEqual(after[0][1], "talk_1")                          # fecha a boca primeiro
        self.assertTrue(after[-1][1].startswith(("idle_", "blink_")))

    def test_other_expressions_are_not_animated_and_state_resets(self):
        a = self.anim()
        self.run_for(a, 1.0)
        self.assertIsNone(a.update("feliz", False, 1.1))
        self.assertIsNone(a.update("bravo", True, 1.2))                  # falar em outra expressão: estática
        self.assertTrue(a.update("serio", False, 1.3).startswith("idle_"))

    def test_text_only_reply_moves_mouth_for_a_while(self):
        a = self.anim()
        a.talk_for(1.0, now=0.0)
        k = [x for _, x in self.run_for(a, 1.0, speaking=False)]
        self.assertTrue(all(x.startswith("talk_") for x in k[:20]))
        late = self.run_for(a, 1.0, speaking=False, t0=2.0)
        self.assertTrue(late[-1][1].startswith(("idle_", "blink_")))

    def test_every_emotion_has_complete_same_size_frames_in_assets(self):
        """assets/anim/<emoção>/: manifesto + todos os quadros que o animador pode pedir, no mesmo tamanho."""
        import json
        import random
        from PIL import Image
        from alicia_core.enums import AliciaExpression
        from alicia_core.ui.avatar_animator import AvatarAnimator
        root = APP_DIR / "assets" / "anim"
        static = set()
        for e in AliciaExpression:
            with Image.open(APP_DIR / "assets" / f"{e.value}.png") as im:
                static.add(im.size)
        self.assertEqual(len(static), 1)
        for e in AliciaExpression:
            base = root / e.value
            manifest = json.loads((base / "anim.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["expression"], e.value)
            a = AvatarAnimator(manifest, rng=random.Random(1))
            keys = {k for _, k in self.run_for(a, 40, expr=e.value) + self.run_for(a, 6, speaking=True, expr=e.value, t0=40)}
            want = ({f"idle_{i:02d}" for i in range(1, manifest["idle"]["frames"] + 1)}
                    | {f"blink_{i}" for i in (2, 3, 4)} | {f"talk_{i}" for i in range(1, manifest["talk"]["frames"] + 1)})
            self.assertLessEqual(keys, want, e.value)                      # o animador nunca pede um quadro que não existe
            for k in want:
                p = base / f"{k}.png"
                self.assertTrue(p.exists(), p)
                with Image.open(p) as im:
                    self.assertEqual(im.mode, "RGBA", p)
                    self.assertEqual(im.size, tuple(manifest["size"]), p)
                    alpha = im.getchannel("A")
                    self.assertIsNotNone(alpha.getbbox(), f"{p} vazio")
                    self.assertEqual(alpha.crop((0, im.height - 1, im.width, im.height)).getextrema()[1], 0, f"{p}: base sem esmaecer")
            self.assertEqual(tuple(manifest["size"]), list(static)[0])   # animadas e estáticas têm a mesma moldura


class TestAvatarAnimatorSet(unittest.TestCase):
    def manifests(self):
        names = ("serio", "feliz", "bravo")
        return {n: {"expression": n, "idle": {"frames": 6, "fps": 10},
                    "blink": {"frames": 4, "sequence": [[2, .05], [3, .08], [4, .06]]}, "talk": {"frames": 4}} for n in names}

    def make(self):
        import random
        from alicia_core.ui.avatar_animator import AvatarAnimatorSet
        return AvatarAnimatorSet(self.manifests(), rng=random.Random(2))

    def test_each_emotion_animates_with_namespaced_keys(self):
        s = self.make()
        for name in ("serio", "feliz", "bravo"):
            k = s.update(name, False, 100.0)
            self.assertTrue(k.startswith(f"{name}/idle_"), k)
            self.assertTrue(s.update(name, True, 100.1).startswith(f"{name}/talk_"))

    def test_emotion_without_animation_returns_none_and_switch_restarts_breathing(self):
        s = self.make()
        self.assertEqual(s.update("feliz", False, 0.0), "feliz/idle_01")
        self.assertEqual(s.update("feliz", False, 0.35), "feliz/idle_04")
        self.assertIsNone(s.update("pensando", False, 0.4))              # sem animação: imagem estática
        self.assertEqual(s.update("feliz", False, 5.0), "feliz/idle_01")  # voltou: recomeça respirando

    def test_text_only_talk_reaches_every_emotion(self):
        s = self.make()
        s.talk_for(1.0, now=0.0)
        self.assertTrue(s.update("bravo", False, 0.2).startswith("bravo/talk_"))


class TestAvatarLoader(unittest.TestCase):
    """_carregar_animacoes: uma animação por emoção, layout antigo ainda aceito, quadro faltando não derruba as outras."""

    def run_loader(self, assets_dirs):
        import types
        from alicia_core.ui import desktop_pet as dp

        class Pix:
            def __init__(self, path=""):
                self.ok = pathlib.Path(path).exists()

            def scaled(self, *a, **k):
                return self

            def isNull(self):
                return not self.ok

        saved = (dp.QPixmap, dp.Qt)
        dp.QPixmap = Pix
        dp.Qt = types.SimpleNamespace(AspectRatioMode=types.SimpleNamespace(KeepAspectRatio=1),
                                      TransformationMode=types.SimpleNamespace(SmoothTransformation=1))
        try:
            ui = dp.AliciaDesktopPetUI.__new__(dp.AliciaDesktopPetUI)
            ui.anim_pixmaps, ui.anim_manifest, ui.anim_manifests, ui.animator = {}, None, {}, None
            ui._carregar_animacoes(assets_dirs)
            return ui
        finally:
            dp.QPixmap, dp.Qt = saved

    def test_loads_all_five_emotions_from_the_project_assets(self):
        from alicia_core.ui.avatar_animator import AvatarAnimatorSet
        ui = self.run_loader([APP_DIR / "assets"])
        self.assertEqual(set(ui.anim_manifests), {"serio", "feliz", "bravo", "pensando", "surpresa"})
        self.assertIsInstance(ui.animator, AvatarAnimatorSet)
        total = sum(m["idle"]["frames"] + m["blink"]["frames"] + m["talk"]["frames"] for m in ui.anim_manifests.values())
        self.assertEqual(len(ui.anim_pixmaps), total)
        self.assertIn("feliz/idle_01", ui.anim_pixmaps)
        self.assertIn("bravo/talk_4", ui.anim_pixmaps)
        self.assertEqual(ui.anim_manifest["expression"], "serio")

    def test_old_flat_layout_still_loads_as_serio_only(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(APP_DIR / "assets" / "anim" / "serio", pathlib.Path(tmp) / "anim")
            ui = self.run_loader([pathlib.Path(tmp)])
        self.assertEqual(set(ui.anim_manifests), {"serio"})
        self.assertIn("serio/idle_01", ui.anim_pixmaps)

    def test_missing_frame_disables_only_that_emotion(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(APP_DIR / "assets" / "anim", pathlib.Path(tmp) / "anim")
            (pathlib.Path(tmp) / "anim" / "feliz" / "talk_2.png").unlink()
            ui = self.run_loader([pathlib.Path(tmp)])
        self.assertEqual(set(ui.anim_manifests), {"serio", "bravo", "pensando", "surpresa"})
        self.assertFalse(any(k.startswith("feliz/") for k in ui.anim_pixmaps))


class TestAvatarWindowLogic(unittest.TestCase):
    """Lógica de atualizar_visual sem abrir janela (Qt trocado por objetos falsos)."""

    def make(self, expr="serio", audio=False, tts_enabled=True):
        import types
        from alicia_core.enums import AliciaExpression
        from alicia_core.ui import desktop_pet
        from alicia_core.ui.avatar_animator import AvatarAnimator
        shown = []
        ui = desktop_pet.AliciaDesktopPetUI.__new__(desktop_pet.AliciaDesktopPetUI)
        ui.core = types.SimpleNamespace(
            modo_descanso=False, current_expression=AliciaExpression(expr), last_alicia_message="",
            tts=types.SimpleNamespace(enabled=tts_enabled, audio_playing=audio, is_speaking=audio),
            config=types.SimpleNamespace(avatar_relax_seconds=8.0))
        ui.label_avatar = types.SimpleNamespace(setPixmap=lambda p: shown.append(p))
        ui.pixmaps = {e: f"static_{e.value}" for e in AliciaExpression}
        ui.variantes = {}
        keys = ([f"idle_{i:02d}" for i in range(1, 19)] + [f"blink_{i}" for i in range(1, 5)] + [f"talk_{i}" for i in range(1, 5)])
        ui.anim_pixmaps = {k: f"anim_{k}" for k in keys}
        ui.animator = AvatarAnimator(TestAvatarAnimator.MANIFEST)
        ui.estado_atual = None
        ui._last_anim_key = None
        ui._expr_since = 0.0
        ui._last_expr_seen = None
        ui._last_msg_seen = ""
        return ui, shown, desktop_pet

    def tick(self, ui, desktop_pet, times):
        for t in times:
            desktop_pet.time.monotonic = lambda t=t: t
            ui.atualizar_visual()

    def test_neutral_face_breathes_and_blinks(self):
        ui, shown, dp = self.make()
        real = dp.time.monotonic
        try:
            self.tick(ui, dp, [i * 0.04 for i in range(300)])
        finally:
            dp.time.monotonic = real
        kinds = {p.split("_")[1] for p in shown}
        self.assertIn("idle", kinds)
        self.assertIn("blink", kinds)
        self.assertLess(len(shown), 300)          # só redesenha quando o quadro muda

    def test_mouth_follows_audio_playing(self):
        ui, shown, dp = self.make(audio=True)
        real = dp.time.monotonic
        try:
            self.tick(ui, dp, [i * 0.04 for i in range(50)])
        finally:
            dp.time.monotonic = real
        self.assertTrue(all(p.startswith("anim_talk_") for p in shown))
        self.assertGreater(len(shown), 4)

    def test_emotion_shows_static_then_relaxes_to_neutral_animation(self):
        ui, shown, dp = self.make(expr="feliz")
        real = dp.time.monotonic
        try:
            self.tick(ui, dp, [0.0, 0.04, 1.0])
            self.assertEqual(shown[0], "static_feliz")
            self.tick(ui, dp, [9.0, 9.04, 9.2])
        finally:
            dp.time.monotonic = real
        self.assertEqual(ui.core.current_expression.value, "serio")
        self.assertTrue(shown[-1].startswith("anim_"))

    def test_text_only_reply_animates_mouth(self):
        ui, shown, dp = self.make(tts_enabled=False)
        real = dp.time.monotonic
        try:
            self.tick(ui, dp, [0.0, 0.04])
            ui.core.last_alicia_message = "Uma resposta bem comprida para a boca mexer por um tempinho."
            self.tick(ui, dp, [0.1 + i * 0.04 for i in range(20)])
        finally:
            dp.time.monotonic = real
        self.assertTrue(any(p.startswith("anim_talk_") for p in shown))

    def test_animation_error_falls_back_to_static_images(self):
        ui, shown, dp = self.make()
        ui.animator.update = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quebrou"))
        real = dp.time.monotonic
        try:
            self.tick(ui, dp, [0.0, 0.04])
        finally:
            dp.time.monotonic = real
        self.assertIsNone(ui.animator)
        self.assertEqual(shown[-1], "static_serio")
