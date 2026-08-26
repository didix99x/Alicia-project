import sys, threading, time, json, os, re, tempfile, asyncio, base64, io
import speech_recognition as sr
import requests, edge_tts, pyautogui
from playsound import playsound
from PyQt6.QtCore import Qt, QPoint, QTimer
from PyQt6.QtGui import QPixmap, QAction
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QWidget

# ===================== CONFIG =====================
HISTORICO = 'historico_alicia.json'
OLLAMA = 'http://localhost:11434/api/generate'
CHAT_MODEL = 'llama3.2'
VISION_MODEL = 'qwen2.5vl:7b'   # troque se seu modelo visual tiver outro nome
MIC_INDEX = 1                    # None = microfone padrão
MIC_SENSITIVITY = 160            # menor = mais sensível
PHRASE_LIMIT = 45
SCREEN_INTERVAL = 20             # segundos entre capturas
SCREEN_COOLDOWN = 45             # mínimo entre comentários falados
SCREEN_QUALITY = 68
os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '1'


def load_history():
    try:
        with open(HISTORICO, 'r', encoding='utf-8') as f:
            x = json.load(f)
            return x if isinstance(x, list) else []
    except Exception:
        return []


def save_history(h):
    try:
        with open(HISTORICO, 'w', encoding='utf-8') as f:
            json.dump(h[-120:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('[HISTÓRICO]', e)


class AliciaDesktopPet(QWidget):
    def __init__(self):
        super().__init__()
        self.history = load_history()
        self.user = 'Anderson'
        self.listening = True
        self.screen_watch = True
        self.expression = 'neutro'
        self.display_text = 'Iniciando Alicia...'
        self.voice = 'pt-BR-FranciscaNeural'
        self.speaking = False
        self.processing_voice = False
        self.mic_ok = False
        self.last_user_time = 0
        self.last_screen_time = 0
        self.last_comment_time = 0
        self.last_screen_signature = ''
        self.screen_busy = False
        self.first_run = True
        self.recognizer = sr.Recognizer()
        self.recognizer.pause_threshold = 0.72
        self.recognizer.non_speaking_duration = 0.22
        self.recognizer.energy_threshold = MIC_SENSITIVITY
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.dynamic_energy_adjustment_damping = 0.10
        self.recognizer.dynamic_energy_ratio = 1.15
        self.init_ui()
        self.prepare_mic()
        self.start_threads()

    # ---------------- UI ----------------
    def init_ui(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.SubWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(500, 500)
        self.avatar = QLabel(self)
        self.avatar.setGeometry(0, 0, 500, 500)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.load_images()
        self.update_visual()
        self.drag = False
        self.old_pos = QPoint()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_visual)
        self.timer.start(200)
        screen = QApplication.primaryScreen().geometry()
        self.move(screen.width() - 550, screen.height() - 580)

    def load_images(self):
        self.images = {}
        names = ['neutro', 'falando', 'pensando', 'feliz', 'seria', 'surpresa', 'bravo']
        os.makedirs('imagens', exist_ok=True)
        fallback = None
        p = os.path.join('imagens', 'alicia.png')
        if os.path.exists(p):
            fallback = QPixmap(p).scaled(460, 460, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        for name in names:
            p = os.path.join('imagens', name + '.png')
            self.images[name] = QPixmap(p).scaled(460, 460, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation) if os.path.exists(p) else fallback

    def update_visual(self):
        img = self.images.get(self.expression) or self.images.get('neutro')
        if img:
            self.avatar.setPixmap(img)
        else:
            self.avatar.setText('ALICIA')

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.drag, self.old_pos = True, e.globalPosition().toPoint()

    def mouseMoveEvent(self, e):
        if self.drag:
            d = e.globalPosition().toPoint() - self.old_pos
            self.move(self.x()+d.x(), self.y()+d.y())
            self.old_pos = e.globalPosition().toPoint()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.drag = False

    def contextMenuEvent(self, e):
        m = QMenu(self)
        s = QAction('Status: ' + ('Ativa' if self.listening else 'Em descanso'), self); s.setEnabled(False); m.addAction(s)
        a = QAction('Observação da tela: ' + ('Ativa' if self.screen_watch else 'Desativada'), self); a.triggered.connect(self.toggle_screen); m.addAction(a)
        m.addSeparator()
        if self.listening:
            a = QAction('Colocar Alicia para descansar', self); a.triggered.connect(self.rest); m.addAction(a)
        else:
            a = QAction('Acordar Alicia', self); a.triggered.connect(self.wake); m.addAction(a)
        m.addSeparator()
        a = QAction('Fechar Alicia', self); a.triggered.connect(self.close_app); m.addAction(a)
        m.exec(e.globalPos())

    # ---------------- state ----------------
    def rest(self):
        self.listening = False
        self.display_text = 'Em descanso'
        self.say('[SERIO] Vou ficar quietinha por enquanto. Quando quiser, me acorde.')

    def wake(self):
        self.listening = True
        self.last_user_time = time.time()
        self.say('[FELIZ] Acordei! Pode falar comigo.')

    def toggle_screen(self):
        self.screen_watch = not self.screen_watch
        self.say('[FELIZ] Observação da tela ativada.' if self.screen_watch else '[SERIO] Parei de observar sua tela.')

    def close_app(self):
        self.screen_watch = False; self.listening = False
        save_history(self.history)
        QApplication.quit()
        os._exit(0)

    # ---------------- voice ----------------
    def prepare_mic(self):
        try:
            with sr.Microphone(device_index=MIC_INDEX) as source:
                print('[MIC] Calibrando uma vez...')
                self.recognizer.adjust_for_ambient_noise(source, duration=0.7)
            # Impede a calibração de deixar a Alicia surda.
            self.recognizer.energy_threshold = max(90, min(self.recognizer.energy_threshold, 300))
            self.mic_ok = True
            print('[MIC] Sensibilidade:', self.recognizer.energy_threshold)
        except Exception as e:
            print('[MIC] ERRO:', e)
            self.mic_ok = False

    async def make_audio(self, text, path):
        await edge_tts.Communicate(text, self.voice).save(path)

    def say(self, full):
        if self.speaking: return
        self.speaking = True
        try:
            if '[FELIZ]' in full: self.expression = 'feliz'
            elif '[BRAVO]' in full: self.expression = 'bravo'
            elif '[SERIO]' in full: self.expression = 'seria'
            elif '[PENSANDO]' in full: self.expression = 'pensando'
            elif '[SURPRESA]' in full: self.expression = 'surpresa'
            text = re.sub(r'\[.*?\]', '', full).replace('*','').strip()
            if not text: return
            self.display_text = text
            print(f'[ALICIA/{self.expression.upper()}] {text}')
            fd, path = tempfile.mkstemp(suffix='.mp3'); os.close(fd)
            try:
                asyncio.run(self.make_audio(text, path))
                if os.path.exists(path) and os.path.getsize(path): playsound(path)
            finally:
                try: os.remove(path)
                except: pass
        except Exception as e:
            print('[TTS]', e)
        finally:
            self.expression = 'neutro'; self.speaking = False

    def listen_once(self):
        if not self.mic_ok:
            self.prepare_mic()
            if not self.mic_ok: time.sleep(1); return ''
        try:
            with sr.Microphone(device_index=MIC_INDEX) as source:
                self.display_text = 'Ouvindo...'
                audio = self.recognizer.listen(source, timeout=None, phrase_time_limit=PHRASE_LIMIT)
            self.processing_voice = True
            self.display_text = 'Pensando...'
            text = self.recognizer.recognize_google(audio, language='pt-BR')
            self.processing_voice = False
            return text.strip()
        except sr.UnknownValueError:
            self.processing_voice = False; return ''
        except sr.RequestError as e:
            self.processing_voice = False; print('[MIC] Reconhecimento:', e); return ''
        except Exception as e:
            self.processing_voice = False; print('[MIC]', e); time.sleep(.25); return ''

    # ---------------- screen vision ----------------
    def capture_screen(self):
        try:
            img = pyautogui.screenshot()
            w,h = img.size
            if w > 1280:
                img = img.resize((1280, int(h*1280/w)))
            b = io.BytesIO(); img.save(b, format='JPEG', quality=SCREEN_QUALITY, optimize=True)
            return base64.b64encode(b.getvalue()).decode()
        except Exception as e:
            print('[TELA]', e); return None

    def vision_model_exists(self):
        try:
            r = requests.get('http://localhost:11434/api/tags', timeout=3)
            if not r.ok: return False
            names = [x.get('name','') for x in r.json().get('models', [])]
            return VISION_MODEL in names or VISION_MODEL.split(':')[0] in [n.split(':')[0] for n in names]
        except Exception:
            return False

    def ask_vision(self, image64):
        prompt = '''Você é Alicia, uma personagem de desktop divertida, curiosa e espontânea. Olhe a captura do PC do Anderson e faça um comentário curto sobre algo realmente visível. Não invente. Não faça relatório e não diga "na imagem vejo". Fale como uma amiga que está olhando por cima do ombro. Pode brincar, demonstrar curiosidade ou preocupação. Se a tela estiver banal, uma piada curta está ótima. No máximo duas frases. Comece com [FELIZ], [SERIO], [SURPRESA], [PENSANDO] ou [BRAVO].'''
        payload = {'model': VISION_MODEL, 'prompt': prompt, 'images':[image64], 'stream':False, 'options':{'temperature':.8,'num_predict':80}}
        try:
            r = requests.post(OLLAMA, json=payload, timeout=75); r.raise_for_status()
            return r.json().get('response','').strip() or None
        except Exception as e:
            print('[VISÃO]', e); return None

    def screen_loop(self):
        checked = False; available = False
        while True:
            try:
                if not self.screen_watch or not self.listening:
                    time.sleep(2); continue
                now = time.time()
                if now-self.last_screen_time < SCREEN_INTERVAL or self.speaking or self.processing_voice or now-self.last_user_time < 8:
                    time.sleep(1); continue
                if not checked:
                    available = self.vision_model_exists(); checked = True
                    print('[TELA] Modelo visual:', 'OK' if available else 'NÃO ENCONTRADO')
                    if not available:
                        self.screen_watch = False
                        print(f'[TELA] Instale/configure {VISION_MODEL} no Ollama para ativar a visão.')
                        continue
                image = self.capture_screen(); self.last_screen_time = time.time()
                if not image: continue
                # Comparação barata: evita inferência quando a captura não mudou.
                sig = image[:250] + image[-250:]
                if sig == self.last_screen_signature: continue
                self.last_screen_signature = sig
                if self.screen_busy: continue
                self.screen_busy = True
                try:
                    comment = self.ask_vision(image)
                    if not comment or self.speaking or self.processing_voice: continue
                    if now-self.last_comment_time < SCREEN_COOLDOWN: continue
                    self.last_comment_time = time.time()
                    self.say(comment)
                finally:
                    self.screen_busy = False
            except Exception as e:
                print('[TELA LOOP]', e); time.sleep(3)

    # ---------------- chat ----------------
    def chat(self, message):
        self.expression = 'pensando'; self.display_text = 'Pensando...'
        self.history.append(f'{self.user}: {message}')
        context = '\n'.join(self.history[-12:])
        prompt = f'''Você é Alicia, uma assistente que vive no desktop do Anderson. Sua personalidade é divertida, espontânea, curiosa, inteligente, carinhosa e levemente provocadora sem ser cruel. Fale naturalmente como uma personagem, não como manual. Faça piadas quando combinar. Em perguntas técnicas, seja útil e correta. Não invente ações que não executou. Normalmente responda em 1 a 4 frases. Comece obrigatoriamente com [FELIZ], [SERIO], [BRAVO], [PENSANDO] ou [SURPRESA].\n\nHistórico:\n{context}\n\nAnderson: {message}\nAlicia:'''
        payload = {'model':CHAT_MODEL,'prompt':prompt,'stream':False,'options':{'temperature':.85,'top_p':.9,'num_predict':220}}
        try:
            r = requests.post(OLLAMA, json=payload, timeout=60); r.raise_for_status()
            answer = r.json().get('response','[FELIZ] Estou ouvindo.').strip()
        except requests.exceptions.ConnectionError:
            answer = '[BRAVO] O Ollama caiu. Eu estava pronta para conversar e ele resolveu tirar férias.'
        except Exception as e:
            print('[OLLAMA]', e); answer = '[BRAVO] Tive um probleminha para pensar agora.'
        self.history.append('Alicia: '+answer); save_history(self.history)
        return answer

    # ---------------- commands ----------------
    def command(self, text):
        t = text.lower().strip()
        if t in {'descansa','fique quieta','silêncio','silencio','vai descansar','pode descansar'}:
            self.rest(); return True
        if t in {'acorda','acordar alicia','alicia acorda','volte','pode voltar'}:
            self.wake(); return True
        if t in {'sair','fechar alicia','feche a alicia','encerrar'}:
            self.say('[SERIO] Até daqui a pouco.'); self.close_app(); return True
        if any(x in t for x in ['pare de olhar minha tela','não olhe minha tela','nao olhe minha tela']):
            self.screen_watch=False; self.say('[SERIO] Parei de observar sua tela.'); return True
        if any(x in t for x in ['olhe minha tela','volte a olhar a tela','observe minha tela']):
            self.screen_watch=True; self.say('[FELIZ] Voltei a observar. Agora quero ver o que você está aprontando.'); return True
        if 'aumente a sensibilidade' in t or 'microfone mais sensível' in t:
            self.recognizer.energy_threshold=max(70,self.recognizer.energy_threshold-50)
            self.say('[FELIZ] Pronto. Deixei meus ouvidos mais atentos.'); return True
        return False

    # ---------------- threads ----------------
    def start_threads(self):
        threading.Thread(target=self.voice_loop, daemon=True).start()
        threading.Thread(target=self.screen_loop, daemon=True).start()

    def voice_loop(self):
        time.sleep(1)
        if self.first_run:
            self.say('[FELIZ] Oi! Eu sou a Alicia. Agora eu posso conversar com você e ficar de olho no que acontece na sua tela.')
            self.first_run=False
        while True:
            try:
                if not self.listening:
                    time.sleep(.5); continue
                text=self.listen_once()
                if not text: continue
                self.last_user_time=time.time(); print('[VOCÊ]',text)
                if self.command(text): continue
                self.say(self.chat(text))
            except Exception as e:
                print('[VOICE LOOP]',e); time.sleep(.5)


def main():
    app=QApplication(sys.argv)
    pet=AliciaDesktopPet(); pet.show()
    sys.exit(app.exec())

if __name__=='__main__': main()