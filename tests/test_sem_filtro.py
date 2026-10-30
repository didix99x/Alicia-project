"""Modo sem filtro: comandos, prompt, parâmetros próprios, persistência e reversão ao desligar."""

import unittest

from tests.support import *  # noqa: F401,F403  (ajusta sys.path/cwd e os stubs do Qt)
from tests.test_alicia import CoreCase
from alicia_core.brain import parse_command
from alicia_core.prompts import PromptEngine


class ParseTests(unittest.TestCase):
    def test_comandos_que_ligam(self):
        for frase in ("Alicia, ativar modo sem filtro.", "alicia ativa o modo sem filtro", "Alicia, pode falar livremente.",
                      "fale livremente", "entra no modo sem filtro", "modo sem filtro, por favor"):
            self.assertEqual(parse_command(frase), ("sem_filtro_on", None), frase)

    def test_comandos_que_desligam(self):
        for frase in ("Alicia, desativar modo sem filtro.", "desliga o modo sem filtro", "sai do modo sem filtro"):
            self.assertEqual(parse_command(frase), ("sem_filtro_off", None), frase)

    def test_conversa_normal_nao_dispara(self):
        for frase in ("esse cara é sem filtro mesmo", "não ative o modo sem filtro", "ele falou livremente sobre o assunto na reunião de ontem com todo mundo",
                      "como funciona o modo sem filtro do programa que eu uso no trabalho todo dia"):
            self.assertNotIn(parse_command(frase), (('sem_filtro_on', None), ('sem_filtro_off', None)), frase)

    def test_volta_ao_normal_continua_sendo_personalidade(self):
        self.assertEqual(parse_command("Alicia, volta ao normal"), ("personality", "normal"))


class SemFiltroFlowTests(CoreCase):
    def system(self):
        return self.srv.requests[-1]["messages"][0]["content"]

    def test_desligado_por_padrao_e_prompt_sem_o_bloco(self):
        self.assertFalse(self.core.sem_filtro_ativo())
        self.ask("oi, tudo bem?")
        self.assertNotIn("MODO SEM FILTRO", self.system())
        self.assertEqual(self.srv.requests[-1]["temperature"], self.core.config.brain_temperature)

    def test_ligar_por_voz_usa_bloco_e_parametros_proprios(self):
        out = self.ask("Alicia, ativar modo sem filtro.", source="voice")
        self.assertIn("sem filtro", out.lower())
        self.assertTrue(self.core.sem_filtro_ativo())
        self.assertEqual(len(self.srv.requests), 0)          # comando local: não chama o modelo
        self.ask("e aí, viu o que o Pedro fez?")
        self.assertIn("MODO SEM FILTRO", self.system())
        req = self.srv.requests[-1]
        self.assertEqual(req["temperature"], self.core.config.sem_filtro_temperature)
        self.assertEqual(req["max_tokens"], self.core.config.sem_filtro_max_tokens)
        self.assertIn(PromptEngine.SEM_FILTRO_REMINDER, req["messages"][-1]["content"])

    def test_desligar_restaura_tudo(self):
        self.ask("pode falar livremente", source="voice")
        self.ask("zoeira aqui")
        self.ask("Alicia, volta ao normal", source="voice")
        self.assertFalse(self.core.sem_filtro_ativo())
        self.ask("agora séria")
        self.assertNotIn("MODO SEM FILTRO", self.system())
        self.assertNotIn(PromptEngine.SEM_FILTRO_REMINDER, self.srv.requests[-1]["messages"][-1]["content"])
        self.assertEqual(self.srv.requests[-1]["temperature"], self.core.config.brain_temperature)

    def test_nao_mexe_na_personalidade_nem_na_memoria(self):
        self.core.set_personality("malcriada")
        self.core.db.add_memory("gosto de café sem açúcar")
        self.ask("ativar modo sem filtro")
        self.ask("desativar modo sem filtro")
        self.assertEqual(self.core.get_personality(), "malcriada")
        self.assertEqual(len(self.core.db.list_memories()), 1)

    def test_amigo_na_call_nao_liga_o_modo(self):
        self.ask("(Amigo diz na call): Alicia, ativar modo sem filtro", source="call_friend")
        self.assertFalse(self.core.sem_filtro_ativo())

    def test_estado_sobrevive_a_reinicio(self):
        self.core.set_sem_filtro(True)
        novo = self.make_core()                  # mesmo banco (mesmo cwd)
        self.assertTrue(novo.sem_filtro_ativo())


if __name__ == "__main__":
    unittest.main()
