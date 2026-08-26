import speech_recognition as sr

print("--- LISTA DE MICROFONES DISPONÍVEIS ---")
for i, nome in enumerate(sr.Microphone.list_microphone_names()):
    print(f"[{i}] {nome}")