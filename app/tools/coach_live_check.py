# -*- coding: utf-8 -*-
"""Проверка протокола Gemini Live без браузера и микрофона - тем же форматом,
что шлёт web/coach.js.

    pip install websockets
    python3 app/tools/coach_live_check.py          # ключ из gemini_key.txt или GEMINI_API_KEY

Что проверяет:
1. setup с теми же полями, что у страницы, -> пришёл setupComplete;
2. текстовый вопрос «как дела в матче?» -> модель вызывает get_game_state
   (toolCall), мы отвечаем toolResponse с выдуманной сводкой;
3. приходит ответ голосом (inlineData с аудио, его mimeType и длина) и
   расшифровка этого ответа (outputTranscription);
4. sessionResumptionUpdate с handle - для продления сессии.
Печатает каждое сообщение сервера кратко; звук сохраняет в /tmp/coach_reply.wav.
"""
import asyncio
import base64
import json
import os
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import paths  # noqa: E402

MODEL = "models/gemini-3.8-live"
URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=")


def key():
    k = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not k and os.path.exists(paths.GEMINI_KEY_FILE):
        k = open(paths.GEMINI_KEY_FILE, encoding="utf-8").read().strip()
    if not k:
        sys.exit("нет ключа: GEMINI_API_KEY или gemini_key.txt рядом с проектом")
    return k


async def main():
    import ssl
    import websockets
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    async with websockets.connect(URL + key(), ssl=ctx, max_size=None) as ws:
        await ws.send(json.dumps({"setup": {
            "model": MODEL,
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}}},
            "systemInstruction": {"parts": [{"text": "Ты голосовой тренер по Dota 2. Отвечай по-русски, коротко. "
                                                     "Факты о матче бери только из get_game_state."}]},
            "tools": [{"googleSearch": {}}, {"functionDeclarations": [
                {"name": "get_game_state", "description": "Текущее состояние матча игрока."}]}],
            "inputAudioTranscription": {}, "outputAudioTranscription": {},
            "contextWindowCompression": {"slidingWindow": {}},
            "sessionResumption": {},
        }}))
        audio, rate, text = bytearray(), 24000, ""
        asked = False
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=40)
            except asyncio.TimeoutError:
                print("— 40 с тишины, заканчиваю")
                break
            msg = json.loads(raw)
            keys = list(msg.keys())
            if "setupComplete" in msg:
                print("OK setupComplete")
                if not asked:
                    asked = True
                    await ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [
                        {"text": "Как у меня дела в матче? Посмотри состояние."}]}], "turnComplete": True}}))
                continue
            if "toolCall" in msg:
                for fc in msg["toolCall"].get("functionCalls", []):
                    print(f"OK toolCall: {fc.get('name')} {fc.get('args')}")
                    await ws.send(json.dumps({"toolResponse": {"functionResponses": [{
                        "id": fc.get("id"), "name": fc.get("name"),
                        "response": {"summary": "Время 14:30. Герой Juggernaut, уровень 11, жив. "
                                                "KDA 3/5/2, добито 98, GPM 410. Предметы: Battle Fury, Phase Boots."}}]}}))
                continue
            if "sessionResumptionUpdate" in msg:
                u = msg["sessionResumptionUpdate"]
                print("OK sessionResumptionUpdate, handle:", bool(u.get("newHandle")), "resumable:", u.get("resumable"))
                continue
            sc = msg.get("serverContent")
            if sc:
                for p in (sc.get("modelTurn") or {}).get("parts", []):
                    if "inlineData" in p:
                        mt = p["inlineData"].get("mimeType", "")
                        if "rate=" in mt:
                            rate = int(mt.split("rate=")[1].split(";")[0])
                        audio += base64.b64decode(p["inlineData"]["data"])
                if (sc.get("outputTranscription") or {}).get("text"):
                    text += sc["outputTranscription"]["text"]
                if sc.get("turnComplete"):
                    print(f"OK turnComplete: аудио {len(audio)} байт ({rate} Гц, {len(audio) / 2 / rate:.1f} с)")
                    print("Расшифровка ответа:", text.strip() or "(нет)")
                    break
                continue
            print("сообщение:", keys, json.dumps(msg, ensure_ascii=False)[:300])
        if audio:
            with wave.open("/tmp/coach_reply.wav", "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(bytes(audio))
            print("звук: /tmp/coach_reply.wav")


if __name__ == "__main__":
    asyncio.run(main())
