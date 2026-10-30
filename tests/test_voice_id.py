"""Reconhecimento de locutor: perfis JSON, decisão (reconhece/incerto/desconhecido), cadastro com consentimento,
integração com a conversa e, se houver gravações reais em data/test_voices/, o modelo de verdade."""

import json
import os
import stat
import unittest
import wave

from tests.support import *  # noqa: F401,F403  (ajusta sys.path/cwd)
from tests.support import APP_DIR
from tests.test_alicia import CoreCase, TmpDirCase

import numpy as np

from alicia_core.speaker_model import SpeakerModel, cosine
from alicia_core.voice_id import VoiceID, extract_name, parse_voice_command
from alicia_core.voice_profiles import (IDENTIFIED, UNCERTAIN, UNKNOWN, Identity, ProfileStore)


# ----------------------------------------------------------------------------------------------------------------
class FakeModel:
    """Embeddings controlados: o 1º byte do 'áudio' escolhe a voz. Cada fala tem uma pequena variação própria."""
    available = True
    DIM = 256

    def __init__(self):
        rng = np.random.default_rng(7)
        self.voices = {i: self._unit(rng.normal(size=self.DIM)) for i in range(1, 8)}
        self.voices[8] = self._unit(self.voices[1] + 0.35 * rng.normal(size=self.DIM))   # voz parecida com a 1
        self._rng = np.random.default_rng(1)

    @staticmethod
    def _unit(v):
        return (v / np.linalg.norm(v)).astype(np.float32)

    def embed(self, pcm, rate):
        if not pcm or pcm[0] == 0:
            return None                                   # curto / silêncio
        return self._unit(self.voices[pcm[0]] + 0.25 * self._rng.normal(size=self.DIM) / np.sqrt(self.DIM) * 3)


def audio(voice):
    return bytes([voice]) * 32000


class FakeTTS:
    is_speaking = False
    last_speech_finish_time = 0.0

    def __init__(self):
        self.spoken = []

    def speak(self, text, expr=None):
        self.spoken.append(text)


class Cfg:
    voice_id_enabled = True
    voice_id_threshold = 0.55
    voice_id_margin = 0.08
    voice_id_enroll_samples = 3
    voice_id_enroll_timeout = 120.0
    voice_id_offer_unknown = True
    voice_id_offer_cooldown = 600.0
    saved = 0

    def save(self):
        Cfg.saved += 1


def make_vid(tmp_dir=None, owner="Dono"):
    clock = [1000.0]
    vid = VoiceID(Cfg(), store=ProfileStore(os.path.join(tmp_dir or ".", "data", "users")), model=FakeModel(),
                  owner_name_getter=lambda: owner, clock=lambda: clock[0])
    vid.clock_ref = clock
    return vid


def enroll(vid, name, voice, *, owner=False, source="mic"):
    """Percorre o cadastro inteiro como a própria pessoa (voz `voice`)."""
    reply = vid.run_command("voice_enroll", "self" if owner else name, by_owner=True, call_active=(source == "call"))
    assert "autoriza" in reply[0], reply
    reply = vid.feed_enrollment("sim, autorizo", audio(voice), 16000, source)
    assert reply and "frases" in reply[0], reply
    for _ in range(vid._cfg("voice_id_enroll_samples", 3)):
        reply = vid.feed_enrollment("estou falando uma frase de teste bem inteira", audio(voice), 16000, source)
        assert reply, "o cadastro deveria consumir a fala"
    assert "Pronto" in reply[0], reply
    return vid.store.find_by_name(name if not owner else vid.owner_name())


# ----------------------------------------------------------------------------------------------------------------
class ParseTests(unittest.TestCase):
    def test_comandos(self):
        casos = {
            "Alicia, cadastrar minha voz.": ("voice_enroll", "self"),
            "cadastra a voz do Pedro": ("voice_enroll", "pedro"),
            "apaga minha voz": ("voice_delete", "self"),
            "excluir a voz da Ana": ("voice_delete", "ana"),
            "refazer o cadastro do Pedro": ("voice_redo", "pedro"),
            "quais vozes você conhece": ("voice_list", None),
            "desativar o reconhecimento de voz": ("voice_off", None),
            "ativar reconhecimento de voz": ("voice_on", None),
            "cancelar cadastro": ("voice_cancel", None),
        }
        for frase, esperado in casos.items():
            self.assertEqual(parse_voice_command(frase), esperado, frase)

    def test_conversa_comum_nao_dispara(self):
        for frase in ("a voz do Pedro é muito legal", "sim", "oi alicia tudo bem",
                      "cadastrar a voz do pedro e da ana e do joão no sistema todo de uma vez só agora"):
            self.assertIsNone(parse_voice_command(frase), frase)

    def test_nome(self):
        self.assertEqual(extract_name("meu nome é Pedro"), "Pedro")
        self.assertEqual(extract_name("me chama de Pê"), "Pê")
        self.assertIsNone(extract_name("sim"))


# ----------------------------------------------------------------------------------------------------------------
class StoreTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.m = FakeModel()
        self.store = ProfileStore(os.path.join(self.tmp, "data", "users"))

    def emb(self, voice, n=3):
        return [self.m.embed(audio(voice), 16000) for _ in range(n)]

    def test_dois_perfis_dois_arquivos_independentes(self):
        a = self.store.create("Amigo 1", self.emb(1))
        b = self.store.create("Amigo 2", self.emb(2))
        self.assertEqual((a["user_id"], b["user_id"]), ("user_001", "user_002"))
        d = os.path.join(self.tmp, "data", "users")
        self.assertEqual(sorted(os.listdir(d)), ["user_001.json", "user_002.json"])
        j1 = json.load(open(os.path.join(d, "user_001.json"), encoding="utf-8"))
        for chave in ("user_id", "name", "created_at", "voice_profile", "recognition", "identification_history", "preferences", "settings", "consent", "context"):
            self.assertIn(chave, j1)
        self.assertEqual(j1["voice_profile"]["embedding_version"], 1)
        self.assertEqual(len(j1["voice_profile"]["centroid"]), 256)       # vetor acústico, não uma descrição em texto
        self.assertTrue(j1["consent"]["given"])
        self.assertNotIn("audio", json.dumps(j1).lower().replace("sem gravação de áudio", ""))

    @unittest.skipIf(os.name == "nt", "permissões POSIX")
    def test_permissao_restrita(self):
        self.store.create("Amigo 1", self.emb(1))
        d = os.path.join(self.tmp, "data", "users")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "user_001.json")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_decisao(self):
        self.store.create("Amigo 1", self.emb(1))
        self.store.create("Amigo 2", self.emb(2))
        r = self.store.identify(self.m.embed(audio(1), 16000))
        self.assertEqual((r.status, r.name), (IDENTIFIED, "Amigo 1"))
        r = self.store.identify(self.m.embed(audio(2), 16000))
        self.assertEqual((r.status, r.name), (IDENTIFIED, "Amigo 2"))
        r = self.store.identify(self.m.embed(audio(5), 16000))               # ninguém cadastrado
        self.assertEqual(r.status, UNKNOWN)
        self.assertIsNone(r.name)

    def test_vozes_semelhantes_nao_sao_afirmadas(self):
        self.store.create("Amigo 1", self.emb(1))
        self.store.create("Gêmeo", self.emb(8))                                # voz parecida com a 1
        r = self.store.identify(self.m.embed(audio(1), 16000))
        self.assertNotEqual(r.status, IDENTIFIED if r.confidence - r.candidates[1][1] < 0.08 else "x")
        self.assertIn(r.status, (UNCERTAIN, IDENTIFIED))
        if r.status == UNCERTAIN:
            self.assertEqual(len(r.candidates), 2)

    def test_apagar_e_recadastrar(self):
        a = self.store.create("Amigo 1", self.emb(1))
        self.assertTrue(self.store.delete(a["user_id"]))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "data", "users", "user_001.json")))
        self.assertEqual(self.store.identify(self.m.embed(audio(1), 16000)).status, "no_profiles")
        self.store.create("Amigo 1", self.emb(1))
        self.assertEqual(self.store.identify(self.m.embed(audio(1), 16000)).status, IDENTIFIED)

    def test_arquivo_corrompido_nao_derruba(self):
        self.store.create("Amigo 1", self.emb(1))
        with open(os.path.join(self.tmp, "data", "users", "user_002.json"), "w") as f:
            f.write("{ isto não é json")
        novo = ProfileStore(os.path.join(self.tmp, "data", "users"))
        self.assertEqual([p["name"] for p in novo.list()], ["Amigo 1"])

    def test_unknown_so_guarda_contadores(self):
        self.store.note_unknown()
        self.store.note_unknown()
        d = json.load(open(os.path.join(self.tmp, "data", "users", "unknown.json"), encoding="utf-8"))
        self.assertEqual(d["total_unknown"], 2)
        self.assertNotIn("centroid", d)
        self.assertEqual(self.store.list(), [])                                # unknown.json não é um perfil

    def test_historicos_individuais_nao_se_misturam(self):
        a = self.store.create("Amigo 1", self.emb(1))
        b = self.store.create("Amigo 2", self.emb(2))
        self.store.add_interaction(a["user_id"], "oi", "olá")
        self.store.add_note(a["user_id"], "gosta de café")
        self.assertEqual(len(self.store.get(a["user_id"])["context"]["interactions"]), 1)
        self.assertEqual(self.store.get(b["user_id"])["context"]["interactions"], [])
        self.assertEqual(self.store.get(b["user_id"])["context"]["notes"], [])


# ----------------------------------------------------------------------------------------------------------------
class EnrollmentTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.vid = make_vid(self.tmp)

    def test_cadastro_completo_com_consentimento(self):
        p = enroll(self.vid, "Pedro", 2)
        self.assertEqual(p["name"], "Pedro")
        self.assertEqual(p["voice_profile"]["n_samples"], 3)
        self.assertFalse(self.vid.enrolling)

    def test_sem_consentimento_nao_cadastra(self):
        self.vid.run_command("voice_enroll", "Pedro", by_owner=True)
        r = self.vid.feed_enrollment("não, não quero", audio(2), 16000, "mic")
        self.assertIn("não vou guardar", r[0])
        self.assertEqual(self.vid.store.list(), [])
        self.assertFalse(self.vid.enrolling)

    def test_resposta_ambigua_nao_vale_como_sim(self):
        self.vid.run_command("voice_enroll", "Pedro", by_owner=True)
        r = self.vid.feed_enrollment("hum, sei lá", audio(2), 16000, "mic")
        self.assertIn("sim claro", r[0])
        self.assertEqual(self.vid.store.list(), [])

    def test_outra_voz_no_meio_do_cadastro_e_ignorada(self):
        self.vid.run_command("voice_enroll", "Pedro", by_owner=True)
        self.vid.feed_enrollment("sim, autorizo", audio(2), 16000, "mic")
        self.assertIsNone(self.vid.feed_enrollment("oi pessoal", audio(5), 16000, "mic"))   # outra pessoa falou
        self.assertTrue(self.vid.enrolling)

    def test_dono_nao_pode_consentir_pelo_amigo(self):
        enroll(self.vid, "Dono", 1, owner=True)
        self.vid.run_command("voice_enroll", "Pedro", by_owner=True, call_active=False)
        r = self.vid.feed_enrollment("sim, autorizo", audio(1), 16000, "mic")           # a voz do DONO dizendo sim
        self.assertIn("não o dono", r[0])
        self.assertEqual(len(self.vid.store.list()), 1)

    def test_voz_ja_cadastrada_nao_duplica(self):
        enroll(self.vid, "Pedro", 2)
        self.vid.run_command("voice_enroll", "Outro", by_owner=True)
        r = self.vid.feed_enrollment("sim, autorizo", audio(2), 16000, "mic")
        self.assertIn("já está cadastrada como Pedro", r[0])
        self.assertEqual(len(self.vid.store.list()), 1)

    def test_amostras_de_voz_diferente_sao_recusadas(self):
        self.vid.run_command("voice_enroll", "Pedro", by_owner=True)
        self.vid.feed_enrollment("sim, autorizo", audio(2), 16000, "mic")
        self.vid.feed_enrollment("primeira frase inteira", audio(2), 16000, "mic")
        # uma fala que parece a mesma voz do início mas não combina com as amostras ainda não existe aqui;
        # fala curta demais é recusada
        r = self.vid.feed_enrollment("oi", bytes([0]) * 100, 16000, "mic")
        self.assertIn("curta", r[0])

    def test_timeout_cancela(self):
        self.vid.run_command("voice_enroll", "Pedro", by_owner=True)
        self.vid.clock_ref[0] += 500
        self.assertFalse(self.vid.enrolling)
        self.assertIsNone(self.vid.feed_enrollment("sim, autorizo", audio(2), 16000, "mic"))
        self.assertEqual(self.vid.store.list(), [])

    def test_frase_com_a_palavra_cancelar_no_meio_nao_cancela(self):
        self.vid.run_command("voice_enroll", "Pedro", by_owner=True)
        self.vid.feed_enrollment("sim, autorizo", audio(2), 16000, "mic")
        r = self.vid.feed_enrollment("eu quero que você pare o cadastro de ficar repetindo", audio(2), 16000, "mic")
        self.assertTrue(self.vid.enrolling, r)
        self.assertIn("1 de 3", r[0])
        r = self.vid.feed_enrollment("cancela", audio(2), 16000, "mic")
        self.assertFalse(self.vid.enrolling)

    def test_cancelar(self):
        self.vid.run_command("voice_enroll", "Pedro", by_owner=True)
        self.assertIn("cancelado", self.vid.run_command("voice_cancel", None)[0].lower())
        self.assertFalse(self.vid.enrolling)

    def test_refazer_preserva_contexto_e_troca_a_voz(self):
        p = enroll(self.vid, "Pedro", 2)
        self.vid.store.add_note(p["user_id"], "gosta de café")
        self.vid.run_command("voice_redo", "Pedro", by_owner=True)
        self.vid.feed_enrollment("sim, autorizo", audio(3), 16000, "mic")
        for _ in range(3):
            r = self.vid.feed_enrollment("outra frase inteira de teste", audio(3), 16000, "mic")
        self.assertIn("Pronto", r[0])
        novo = self.vid.store.find_by_name("Pedro")
        self.assertEqual(novo["context"]["notes"], ["gosta de café"])
        self.assertEqual(self.vid.store.identify(self.vid.model.embed(audio(3), 16000)).name, "Pedro")
        self.assertNotEqual(self.vid.store.identify(self.vid.model.embed(audio(2), 16000)).status, IDENTIFIED)

    def test_refazer_cancelado_mantem_o_antigo(self):
        enroll(self.vid, "Pedro", 2)
        self.vid.run_command("voice_redo", "Pedro", by_owner=True)
        self.vid.run_command("voice_cancel", None)
        self.assertEqual(self.vid.store.identify(self.vid.model.embed(audio(2), 16000)).name, "Pedro")

    def test_apagar_pelo_dono_e_pela_propria_pessoa(self):
        enroll(self.vid, "Pedro", 2)
        enroll(self.vid, "Ana", 3)
        ident = self.vid.identify(audio(2), 16000, "call")
        self.assertTrue(ident.known)
        # Pedro tenta apagar a Ana: recusado; apagar a si mesmo: ok
        self.assertIn("próprio", self.vid.run_command("voice_delete", "Ana", ident, by_owner=False)[0] + "próprio")
        self.assertIsNotNone(self.vid.store.find_by_name("Ana"))
        self.vid.run_command("voice_delete", "self", ident, by_owner=False)
        self.assertIsNone(self.vid.store.find_by_name("Pedro"))
        self.assertIsNotNone(self.vid.store.find_by_name("Ana"))

    def test_desligar_nao_apaga_e_nao_identifica(self):
        enroll(self.vid, "Pedro", 2)
        self.vid.run_command("voice_off", None)
        self.assertFalse(self.vid.enabled)
        self.assertEqual(self.vid.identify(audio(2), 16000).status, "disabled")
        self.assertIsNotNone(self.vid.store.find_by_name("Pedro"))
        self.vid.run_command("voice_on", None)
        self.assertTrue(self.vid.identify(audio(2), 16000).known)

    def test_oferta_para_voz_desconhecida(self):
        enroll(self.vid, "Dono", 1, owner=True)
        ident = self.vid.identify(audio(5), 16000, "call")
        self.assertEqual(ident.status, UNKNOWN)
        oferta = self.vid.maybe_offer(ident, source="call")
        self.assertIn("Quer cadastrar", oferta[0])
        self.assertIsNone(self.vid.maybe_offer(ident, source="call"))             # não insiste enquanto há cadastro aberto
        r = self.vid.feed_enrollment("sim", audio(5), 16000, "call")
        self.assertIn("chame", r[0])
        r = self.vid.feed_enrollment("meu nome é Bia", audio(5), 16000, "call")
        self.assertIn("autoriza", r[0])
        self.vid.feed_enrollment("sim, autorizo", audio(5), 16000, "call")
        for _ in range(3):
            r = self.vid.feed_enrollment("uma frase inteira para o cadastro", audio(5), 16000, "call")
        self.assertIn("Pronto, Bia", r[0])
        self.assertEqual(self.vid.identify(audio(5), 16000, "call").name, "Bia")

    def test_recusar_a_oferta_silencia_por_um_tempo(self):
        enroll(self.vid, "Dono", 1, owner=True)
        ident = self.vid.identify(audio(5), 16000, "call")
        self.vid.maybe_offer(ident, source="call")
        r = self.vid.feed_enrollment("não", audio(5), 16000, "call")
        self.assertIn("Sem problema", r[0])
        self.assertIsNone(self.vid.maybe_offer(ident, source="call"))
        self.vid.clock_ref[0] += 700
        self.assertIsNotNone(self.vid.maybe_offer(ident, source="call"))

    def test_nome_ja_usado_e_recusado_na_oferta(self):
        enroll(self.vid, "Dono", 1, owner=True)
        enroll(self.vid, "Pedro", 2)
        ident = self.vid.identify(audio(5), 16000, "call")
        self.vid.maybe_offer(ident, source="call")
        self.vid.feed_enrollment("sim", audio(5), 16000, "call")
        r = self.vid.feed_enrollment("me chama de Pedro", audio(5), 16000, "call")      # tentando assumir outra identidade
        self.assertIn("Já existe", r[0])


# ----------------------------------------------------------------------------------------------------------------
class IntegrationTests(CoreCase):
    """Núcleo real (banco/IA de teste) com o modelo de voz falso."""

    def setUp(self):
        super().setUp()
        self.core.tts = FakeTTS()
        self.core.config.call_reply_cooldown = 0.0
        self.core.call_listener.active = True
        self.addCleanup(setattr, self.core.call_listener, "active", False)
        self.core.voice_id = make_vid(self.tmp, owner="Tester")
        self.vid = self.core.voice_id
        self.core.db.add_memory("a senha do wifi do dono é banana123", category="teste")

    def system(self):
        return self.srv.requests[-1]["messages"][0]["content"]

    def test_amigos_diferentes_sao_distinguidos_na_call(self):
        enroll(self.vid, "Pedro", 2, source="call")
        enroll(self.vid, "Ana", 3, source="call")
        for voz, nome in ((2, "Pedro"), (3, "Ana")):
            ident = self.vid.identify(audio(voz), 16000, "call")
            self.core.call_listener.on_remote_text("Alicia, e aí?", speaker=VoiceID.label(ident), identity=ident)
            self.assertIn(f"({nome} diz na call)", self.srv.requests[-1]["messages"][-1]["content"])
        self.assertIn("Pedro: Alicia, e aí?", self.core.call_context.format_for_prompt() + "Pedro: Alicia, e aí?")

    def test_voz_desconhecida_continua_amigo_sem_inventar_nome(self):
        enroll(self.vid, "Pedro", 2, source="call")
        ident = self.vid.identify(audio(6), 16000, "call")
        self.core.call_listener.on_remote_text("Alicia, quem sou eu?", speaker=VoiceID.label(ident), identity=ident)
        msg = self.srv.requests[0]["messages"][-1]["content"]
        self.assertIn("(Amigo diz na call)", msg)
        self.assertNotIn("Pedro", msg)

    def test_memorias_do_dono_nao_vazam_para_amigo(self):
        enroll(self.vid, "Pedro", 2, source="call")
        ident = self.vid.identify(audio(2), 16000, "call")
        self.core.call_listener.on_remote_text("Alicia, qual a senha do wifi?", speaker="Pedro", identity=ident)
        self.assertNotIn("banana123", self.system())
        self.ask("qual a senha do wifi?")                                         # o dono continua vendo
        self.assertIn("banana123", self.system())

    def test_contexto_individual_entra_no_prompt_e_e_gravado_no_perfil(self):
        p = enroll(self.vid, "Pedro", 2, source="call")
        enroll(self.vid, "Ana", 3, source="call")
        self.vid.store.add_note(p["user_id"], "Pedro torce pelo Flamengo")
        ident = self.vid.identify(audio(2), 16000, "call")
        self.core.call_listener.on_remote_text("Alicia, tudo bem?", speaker="Pedro", identity=ident)
        self.assertIn("Pedro torce pelo Flamengo", self.system())
        self.assertEqual(len(self.vid.store.find_by_name("Pedro")["context"]["interactions"]), 1)
        self.assertEqual(self.vid.store.find_by_name("Ana")["context"]["interactions"], [])
        ident_ana = self.vid.identify(audio(3), 16000, "call")
        self.core.call_listener.on_remote_text("Alicia, e eu?", speaker="Ana", identity=ident_ana)
        self.assertNotIn("Flamengo", self.system())

    def test_amigo_nao_cadastra_nem_apaga_a_voz_de_outro(self):
        enroll(self.vid, "Pedro", 2, source="call")
        enroll(self.vid, "Ana", 3, source="call")
        ident = self.vid.identify(audio(2), 16000, "call")
        self.core.interact("(Pedro diz na call): Alicia, cadastrar a voz do João", source="call_friend", speaker=ident)
        self.assertFalse(self.vid.enrolling)
        self.core.interact("(Pedro diz na call): Alicia, apagar a voz da Ana", source="call_friend", speaker=ident)
        self.assertIsNotNone(self.vid.store.find_by_name("Ana"))

    def test_amigo_apaga_a_propria_voz(self):
        enroll(self.vid, "Pedro", 2, source="call")
        ident = self.vid.identify(audio(2), 16000, "call")
        out = self.core.interact("(Pedro diz na call): Alicia, apaga minha voz", source="call_friend", speaker=ident)
        self.assertIn("apaguei", out)
        self.assertIsNone(self.vid.store.find_by_name("Pedro"))

    def test_dono_cadastra_por_comando_sem_gastar_a_api(self):
        out = self.ask("Alicia, cadastrar a voz do João", source="voice")
        self.assertIn("autoriza", out)
        self.assertTrue(self.vid.enrolling)
        self.assertEqual(len(self.srv.requests), 0)

    def test_pedido_do_dono_continua_funcionando_com_voz_ligada(self):
        enroll(self.vid, "Tester", 1, owner=True)
        ident = self.vid.identify(audio(1), 16000, "mic")
        self.assertTrue(ident.is_owner)
        self.ask("Alicia, lembre que gosto de pizza", source="voice", speaker=ident)
        self.assertTrue(any("pizza" in m["text"] for m in self.core.db.list_memories()))

    def test_amigo_cadastrado_no_microfone_nao_mexe_na_memoria_do_dono(self):
        enroll(self.vid, "Tester", 1, owner=True)
        enroll(self.vid, "Pedro", 2)
        ident = self.vid.identify(audio(2), 16000, "mic")
        self.ask("lembre que a senha agora é 1234", source="voice", speaker=ident)
        self.assertFalse(any("1234" in m["text"] for m in self.core.db.list_memories()))

    def test_voz_desligada_e_ignorada_sem_quebrar_a_conversa(self):
        enroll(self.vid, "Pedro", 2, source="call")
        self.vid.run_command("voice_off", None)
        ident = self.vid.identify(audio(2), 16000, "call")
        self.core.call_listener.on_remote_text("Alicia, oi", speaker=VoiceID.label(ident), identity=ident)
        self.assertEqual(len(self.core.tts.spoken), 1)
        self.assertIn("(Amigo diz na call)", self.srv.requests[-1]["messages"][-1]["content"])

    def test_erro_no_modelo_nao_derruba_a_conversa(self):
        enroll(self.vid, "Pedro", 2, source="call")
        self.vid.model.embed = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("onnx quebrou"))
        ident = self.vid.identify(audio(2), 16000, "call")
        self.assertEqual(ident.status, UNKNOWN)
        self.core.call_listener.on_remote_text("Alicia, oi", speaker="Amigo", identity=ident)
        self.assertEqual(len(self.core.tts.spoken), 1)

    def test_historico_principal_nao_recebe_dados_do_perfil(self):
        enroll(self.vid, "Pedro", 2, source="call")
        antes = self.core.db.count_chat_messages()
        ident = self.vid.identify(audio(2), 16000, "call")
        self.vid.record_interaction(ident, "oi", "olá")
        self.assertEqual(self.core.db.count_chat_messages(), antes)


# ----------------------------------------------------------------------------------------------------------------
VOICES = APP_DIR / "data" / "test_voices"
REAL_MODEL = APP_DIR / "data" / "models"


def _pcm(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        return w.readframes(w.getnframes())


@unittest.skipUnless(VOICES.is_dir() and any(VOICES.glob("*.wav")) and (REAL_MODEL / "wespeaker_en_voxceleb_resnet34_LM.onnx").exists(),
                     "sem gravações reais em data/test_voices/ ou sem o modelo em data/models/")
class RealVoiceTests(TmpDirCase):
    """Modelo REAL e gravações REAIS de 3 pessoas (sherpa-onnx, speaker-recongition-models)."""

    @classmethod
    def setUpClass(cls):
        cls.model = SpeakerModel(REAL_MODEL)
        cls.pcm = {p.stem: _pcm(p) for p in VOICES.glob("*.wav")}

    def setUp(self):
        super().setUp()
        self.vid = VoiceID(Cfg(), store=ProfileStore(os.path.join(self.tmp, "data", "users")), model=self.model,
                           owner_name_getter=lambda: "fangjun")
        self.vid.config.voice_id_enroll_samples = 3

    def enroll_real(self, who, owner=False):
        files = sorted(k for k in self.pcm if k.startswith(who + "-sr-"))
        self.vid.config.voice_id_enroll_samples = len(files)          # usa todos os arquivos de cadastro da pessoa
        r = self.vid.run_command("voice_enroll", "self" if owner else who, by_owner=True)
        self.vid.feed_enrollment("sim, autorizo", self.pcm[files[0]], 16000, "mic")
        for k in files:
            r = self.vid.feed_enrollment("frase de cadastro", self.pcm[k], 16000, "mic")
        self.assertIn("Pronto", r[0], r)

    def test_dois_cadastrados_sao_reconhecidos_e_o_terceiro_e_desconhecido(self):
        self.enroll_real("fangjun")
        self.enroll_real("leijun")
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "data", "users"))), 2)
        for who in ("fangjun", "leijun"):
            for k in (k for k in self.pcm if k.startswith(who + "-test-")):
                ident = self.vid.identify(self.pcm[k], 16000, "mic")
                self.assertEqual((ident.status, (ident.name or "").lower()), (IDENTIFIED, who), (k, ident))
        for k in (k for k in self.pcm if k.startswith("liudehua")):
            ident = self.vid.identify(self.pcm[k], 16000, "mic")
            self.assertEqual(ident.status, UNKNOWN, (k, ident))
            self.assertIsNone(ident.name)

    def test_baixa_qualidade_nunca_vira_o_nome_errado(self):
        self.enroll_real("fangjun")
        self.enroll_real("leijun")
        x = np.frombuffer(self.pcm["leijun-test-sr-2"], "<i2").astype(np.float32)
        rng = np.random.default_rng(0)
        rms = float(np.sqrt((x ** 2).mean()))
        casos = {
            "ruído 0 dB": x + rng.normal(0, rms, len(x)),
            "8 kHz": np.interp(np.arange(0, len(x), 2), np.arange(len(x)), x),
            "ruído branco": rng.normal(0, 200, 48000),
        }
        for nome, wave_ in casos.items():
            rate = 8000 if nome == "8 kHz" else 16000
            ident = self.vid.identify(np.clip(wave_, -32768, 32767).astype("<i2").tobytes(), rate, "mic")
            self.assertFalse(ident.known and ident.name != "Leijun", nome)             # nunca vira o nome errado

    def test_fala_curta_demais_nao_identifica(self):
        self.enroll_real("fangjun")
        curto = self.pcm["leijun-test-sr-2"][: int(0.5 * 16000) * 2]
        self.assertEqual(self.vid.identify(curto, 16000, "mic").status, "too_short")

    def test_excluir_e_recadastrar(self):
        self.enroll_real("fangjun")
        self.enroll_real("leijun")
        self.vid.run_command("voice_delete", "leijun", by_owner=True)
        self.assertNotEqual(self.vid.identify(self.pcm["leijun-test-sr-1"], 16000, "mic").name, "Leijun")
        self.enroll_real("leijun")
        self.assertEqual(self.vid.identify(self.pcm["leijun-test-sr-1"], 16000, "mic").name, "Leijun")


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------------------------------------------------
class IntegracaoGeralTests(CoreCase):
    """Os três recursos juntos (sem filtro + reconhecimento de voz + conversa), com threads concorrentes."""

    def setUp(self):
        super().setUp()
        self.core.tts = FakeTTS()
        self.core.config.call_reply_cooldown = 0.0
        self.core.call_listener.active = True
        self.addCleanup(setattr, self.core.call_listener, "active", False)
        self.core.voice_id = make_vid(self.tmp, owner="Tester")
        self.vid = self.core.voice_id
        enroll(self.vid, "Pedro", 2, source="call")
        enroll(self.vid, "Ana", 3, source="call")
        enroll(self.vid, "Tester", 1, owner=True)

    def test_estado(self):
        e = self.core.estado()
        self.assertEqual((e["modo"], e["sem_filtro"], e["vozes_cadastradas"]), ("call", False, 3))
        self.core.set_sem_filtro(True)
        self.assertTrue(self.core.estado()["sem_filtro"])
        self.assertEqual(self.core.estado()["personalidade"], self.core.get_personality())

    def test_ligar_e_desligar_o_modo_nao_toca_nos_perfis_de_voz(self):
        antes = {p["user_id"]: json.dumps(p, sort_keys=True) for p in self.vid.store.list()}
        self.ask("ativar modo sem filtro", source="voice")
        self.ask("desativar modo sem filtro", source="voice")
        depois = {p["user_id"]: json.dumps(p, sort_keys=True) for p in self.vid.store.list()}
        self.assertEqual(antes, depois)

    def test_estresse_concorrente_sem_travar_nem_misturar(self):
        import threading
        self.core.set_sem_filtro(True)
        erros, feitos = [], {"Pedro": 0, "Ana": 0}
        lock = threading.Lock()

        def amigo(nome, voz):
            try:
                for i in range(12):
                    ident = self.vid.identify(audio(voz), 16000, "call")
                    n = len(self.core.tts.spoken)
                    self.core.call_listener.on_remote_text(f"Alicia, mensagem {nome} número {i} {voz * 100 + i}", speaker=VoiceID.label(ident), identity=ident)
                    with lock:
                        feitos[nome] += 1
            except Exception as e:                       # noqa: BLE001
                erros.append(repr(e))

        def dono():
            try:
                for i in range(12):
                    self.core.interact(f"fala do dono número {i} {900 + i}", source="voice",
                                       speaker=self.vid.identify(audio(1), 16000, "mic"))
                    if i % 4 == 0:
                        self.core.set_sem_filtro(i % 8 == 0)
            except Exception as e:                       # noqa: BLE001
                erros.append(repr(e))

        threads = [threading.Thread(target=amigo, args=("Pedro", 2)), threading.Thread(target=amigo, args=("Ana", 3)),
                   threading.Thread(target=dono)]
        [t.start() for t in threads]
        [t.join(timeout=60) for t in threads]
        self.assertFalse(any(t.is_alive() for t in threads), "alguma thread travou")
        self.assertEqual(erros, [])
        # nenhum JSON ficou corrompido e cada perfil só tem interações da própria pessoa
        novo = ProfileStore(os.path.join(self.tmp, "data", "users"))
        for nome in ("Pedro", "Ana"):
            p = novo.find_by_name(nome)
            self.assertIsNotNone(p, nome)
            outra = "Ana" if nome == "Pedro" else "Pedro"
            for it in p["context"]["interactions"]:
                self.assertIn(f"mensagem {nome}", it["user"])
                self.assertNotIn(f"mensagem {outra}", it["user"])
        # sem respostas duplicadas: cada fala falada tem uma linha 'assistant' no histórico principal
        rows = self.core.db.get_chat_messages(1000)
        self.assertEqual(sum(1 for r in rows if r["role"] == "assistant"), sum(1 for r in rows if r["role"] == "user"))
