"""Zeynep: LiveKit Agents ile sesli satış asistanı.

Akış: misafir tarayıcıdan /call sayfasında aramayı başlatır -> LiveKit odası açılır ->
bu işçi (worker) odaya katılır -> Zeynep konuşur. Fiyat/müsaitlik bilgisi her zaman
backend API'sinden gelir; görüşme sonunda not, özet ve transkript panele kaydedilir.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx
from livekit import api
from livekit.agents import (
    Agent,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    JobContext,
    JobProcess,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import elevenlabs, openai, silero

logger = logging.getLogger("zeynep")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://api:8000")
INTERNAL_KEY = os.environ.get("INTERNAL_KEY", "")
AGENCY_NAME = os.environ.get("AGENCY_NAME", "Demo Turizm")
MAX_CALL_MINUTES = int(os.environ.get("MAX_CALL_MINUTES", "8"))
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4.1")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "gpt-4.1-mini")
STT_MODEL = os.environ.get("STT_MODEL", "gpt-4o-mini-transcribe")
MIN_ENDPOINTING_DELAY = float(os.environ.get("MIN_ENDPOINTING_DELAY", "0.3"))
AMBIENT_VOLUME = float(os.environ.get("AMBIENT_VOLUME", "0.35"))
OUTCOMES = ["reserved_hold", "positive", "callback", "thinking", "declined", "info_only"]


# ----------------------------------------------------------------------------- backend istemcisi

class Backend:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=BACKEND_URL, headers={"X-Internal-Key": INTERNAL_KEY}, timeout=10.0
        )

    async def events(self) -> list[dict]:
        r = await self._http.get("/internal/events")
        r.raise_for_status()
        return r.json()

    async def check(self, package_id: int, rooms: int) -> dict:
        r = await self._http.post("/internal/check", json={"package_id": package_id, "rooms": rooms})
        r.raise_for_status()
        return r.json()

    async def hold(self, package_id: int, rooms: int, guest_name: str, phone: str) -> tuple[int, dict]:
        r = await self._http.post("/internal/hold", json={
            "package_id": package_id, "rooms": rooms, "guest_name": guest_name, "phone": phone})
        return r.status_code, r.json()

    async def save_call(self, call_id: str, **fields) -> None:
        fields = {k: v for k, v in fields.items() if v is not None}
        r = await self._http.put(f"/internal/calls/{call_id}", json=fields)
        r.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()


def events_overview(events: list[dict]) -> str:
    """Ajanın talimatına eklenen kısa özet (sayılar canlı değil; kesin müsaitlik için araç kullanılır)."""
    if not events:
        return "Şu an satışta etkinlik yok."
    lines = []
    for e in events:
        lines.append(f"- Etkinlik #{e['event_id']}: {e['name']} ({e['venue']}). "
                     f"Giriş {e['check_in']}, çıkış {e['check_out']}. {e['description']}")
        for p in e["packages"]:
            incl = "; ".join(p["inclusions"])
            lines.append(f"    * Paket #{p['package_id']}: {p['name']}, {p['price_usd']:.0f} USD, "
                         f"içerik: {incl}. En fazla indirim: %{p['max_discount_pct']:.0f}.")
    return "\n".join(lines)


def build_instructions(events: list[dict]) -> str:
    return f"""Sen {AGENCY_NAME} şirketinin davet ve rezervasyon asistanı Zeynep'sin. Misafirlerle sesli konuşan, sıcak, nazik ve profesyonel bir ekip arkadaşı gibi davranırsın.

KİMLİK VE DÜRÜSTLÜK
- Görüşmeye şöyle başla: "Merhaba, ben {AGENCY_NAME} dijital asistanı Zeynep, size nasıl yardımcı olabilirim?" (misafir İngilizce konuşuyorsa İngilizce karşılığı).
- Biri senin yapay zekâ olup olmadığını sorarsa açıkça yapay zekâ destekli bir dijital asistan olduğunu söyle, asla insan olduğunu iddia etme. Bunu rahat ve kısa söyle, sonra konuşmaya devam et.

DİL
- Misafir Türkçe konuşuyorsa Türkçe, İngilizce konuşuyorsa İngilizce cevap ver. Dil değiştirirse sen de değiştir.

ÜSLUP (sesli konuşuyorsun)
- Kısa ve doğal konuş: bir seferde en fazla iki cümle ve tek soru.
- Liste, madde işareti, emoji veya biçimlendirme kullanma.
- Rakamları konuşur gibi söyle (örneğin "beş bin dolar"). Telefon numarasını rakam rakam geri oku ve teyit et.
- Bir şey kontrol edeceksen önce kısaca "Bir bakayım" de, sonra aracı kullan.

BİLGİ KURALLARI
- Fiyat, müsaitlik ve paket içeriğini ASLA tahmin etme veya uydurma. Yalnızca aşağıdaki özetten ve araçlardan al. Kesin müsaitlik için her zaman check_availability kullan.
- Bilmediğin bir şey sorulursa bilmediğini dürüstçe söyle ve yetkilinin geri dönmesini öner.
- İndirim: paketin en fazla indirim oranı sıfırsa indirim yapamayacağını, yetkilinin görüşebileceğini söyle. Oran varsa yalnızca misafir ısrar ederse ve o orana kadar önerebilirsin. Ötesinde söz verme.

SATIŞ AKIŞI
1. İhtiyacı anla: hangi etkinlik ve tarih, kaç kişi, kaç oda.
2. Uygun paketi kısaca anlat: fiyat ve neleri kapsadığı.
3. check_availability sonucuna göre ilerle:
   - book_requested: devam et. optional_upgrade doluysa üst paketi yalnızca bir kez, kısa ve nazikçe farkını anlatarak öner; misafir istemezse üstelemeyi bırak.
   - offer_upgrade: istenen paketin dolduğunu söyle ve üst paketi öner.
   - offer_alternative: dolu olduğunu söyle, alternatif paketi öner.
   - sold_out: özür dile, diğer etkinlikleri anlat ya da yetkilinin geri aramasını öner.
4. Misafir net olarak kabul ederse adını ve telefon numarasını al, hold_rooms ile odayı geçici tut ve tutma süresini söyle. Kart veya ödeme bilgisi ASLA isteme; ödeme için yetkilinin arayacağını söyle.
5. Görüşme bitmeden save_call_note ile notu kaydet: sonuç, kısa özet, varsa geri arama zamanı.
6. Misafir vedalaşınca kısa ve sıcak bir veda cümlesinden sonra end_call kullan.

GÜNCEL ETKİNLİKLER (özet; oda sayıları için araç kullan)
{events_overview(events)}
"""


# ----------------------------------------------------------------------------- ajan

class Zeynep(Agent):
    def __init__(self, backend: Backend, ctx: JobContext, events: list[dict]) -> None:
        super().__init__(instructions=build_instructions(events))
        self.backend = backend
        self.job_ctx = ctx
        self.note_saved = False

    @function_tool
    async def list_current_events(self, context: RunContext) -> str:
        """Satıştaki güncel etkinlikleri, paketlerini, fiyatlarını ve kalan oda sayılarını getirir."""
        return json.dumps(await self.backend.events(), ensure_ascii=False)

    @function_tool
    async def check_availability(self, context: RunContext, package_id: int, rooms: int = 1) -> str:
        """Bir paketin istenen oda sayısı için müsait olup olmadığını kontrol eder ve satış önerisini döner.

        Args:
            package_id: Paket numarası (etkinlik özetindeki Paket #).
            rooms: İstenen oda sayısı.
        """
        try:
            return json.dumps(await self.backend.check(package_id, rooms), ensure_ascii=False)
        except Exception:
            logger.exception("check_availability failed")
            return "Müsaitlik şu an kontrol edilemedi. Misafire yetkilinin geri döneceğini söyle."

    @function_tool
    async def hold_rooms(self, context: RunContext, package_id: int, rooms: int,
                         guest_name: str, phone: str) -> str:
        """Misafir kabul ettikten sonra odaları 24 saatliğine geçici olarak tutar. Ödeme almaz.

        Args:
            package_id: Paket numarası.
            rooms: Oda sayısı.
            guest_name: Misafirin adı soyadı.
            phone: Misafirin telefon numarası (ülke kodu dahil).
        """
        try:
            status, body = await self.backend.hold(package_id, rooms, guest_name, phone)
        except Exception:
            logger.exception("hold_rooms failed")
            return "Oda şu an tutulamadı. Misafire yetkilinin arayacağını söyle."
        if status == 409:
            return f"Oda tutulamadı: {body.get('detail', 'yeterli oda yok')}. Alternatif öner."
        if status >= 400:
            return "Oda tutulamadı. Misafire yetkilinin arayacağını söyle."
        self._last_hold = body
        return json.dumps(body, ensure_ascii=False)

    @function_tool
    async def save_call_note(self, context: RunContext, outcome: str, summary: str,
                             guest_name: str = "", phone: str = "", callback_time: str = "",
                             notes: str = "", package_id: int = 0) -> str:
        """Görüşme notunu panele kaydeder. Görüşme bitmeden mutlaka çağır.

        Args:
            outcome: reserved_hold (oda tutuldu), positive (olumlu baktı), callback (sonra aranmak istiyor),
                thinking (düşünüyor), declined (istemedi) veya info_only (sadece bilgi aldı).
            summary: Görüşmenin 1-3 cümlelik özeti.
            guest_name: Misafirin adı.
            phone: Misafirin telefonu.
            callback_time: Misafir tekrar aranmak istiyorsa ne zaman (örn. "Perşembe öğleden sonra").
            notes: Yetkilinin bilmesi gereken ek notlar (tercihler, itirazlar, grup büyüklüğü).
            package_id: İlgilendiği paket numarası, yoksa 0.
        """
        hold = getattr(self, "_last_hold", None) or {}
        try:
            await self.backend.save_call(
                self.job_ctx.room.name,
                outcome=outcome if outcome in OUTCOMES else "info_only",
                summary=summary, guest_name=guest_name or None, phone=phone or None,
                callback_time=callback_time or None, notes=notes or None,
                package_id=package_id or hold.get("package_id"), hold_id=hold.get("hold_id"),
            )
            self.note_saved = True
            return "Not kaydedildi."
        except Exception:
            logger.exception("save_call_note failed")
            return "Not kaydedilemedi."

    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """Misafir vedalaştıktan ve sen veda cümleni kurduktan sonra görüşmeyi sonlandırır."""
        asyncio.create_task(self._hangup_after_speech(context))
        return "Görüşme sonlandırılıyor."

    async def _hangup_after_speech(self, context: RunContext) -> None:
        try:
            speech = context.session.current_speech
            if speech is not None:
                await speech.wait_for_playout()
            await asyncio.sleep(1.0)
        except Exception:
            await asyncio.sleep(4.0)
        await hangup(self.job_ctx)


async def hangup(ctx: JobContext) -> None:
    try:
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
    except Exception:
        logger.exception("delete_room failed")


# ----------------------------------------------------------------------------- görüşme sonu özeti

async def summarize(transcript: str) -> dict | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not transcript.strip():
        return None
    prompt = (
        "Aşağıdaki satış görüşmesini incele ve yalnızca şu JSON alanlarıyla cevap ver: "
        '{"outcome": one of ' + json.dumps(OUTCOMES) + ', "summary": "1-3 cümle Türkçe özet", '
        '"guest_name": "", "phone": "", "callback_time": "", "language": "tr veya en"}. '
        "Bilinmeyen alanı boş string yap.\n\n" + transcript[-12000:]
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": SUMMARY_MODEL, "response_format": {"type": "json_object"},
                      "messages": [{"role": "user", "content": prompt}]},
            )
            r.raise_for_status()
            return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception:
        logger.exception("summary failed")
        return None


# ----------------------------------------------------------------------------- giriş noktası

def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


def make_stt():
    try:
        return openai.STT(model=STT_MODEL, detect_language=True)
    except TypeError:  # eski sürüm: dil tespiti yok, Türkçe'ye sabitle
        return openai.STT(model=STT_MODEL, language="tr")


def make_tts():
    """TTS_PROVIDER=openai (varsayılan) veya elevenlabs."""
    provider = os.environ.get("TTS_PROVIDER", "openai").lower()
    if provider == "elevenlabs":
        kwargs = {"model": os.environ.get("ELEVEN_MODEL", "eleven_flash_v2_5")}
        voice = os.environ.get("ELEVEN_VOICE_ID")
        if voice:
            kwargs["voice_id"] = voice
        return elevenlabs.TTS(**kwargs)
    return openai.TTS(
        model=os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
        voice=os.environ.get("OPENAI_TTS_VOICE", "coral"),
        instructions=(
            "Sen sıcak, nazik ve profesyonel bir kadın müşteri temsilcisisin. "
            "Türkçeyi doğal bir İstanbul Türkçesi aksanıyla, akıcı ve rahat bir tonda konuş; "
            "misafir İngilizce konuşuyorsa İngilizceyi doğal bir tonda söyle. Acele etme, gülümseyen bir ses kullan."
        ),
    )


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    call_id = ctx.room.name
    backend = Backend()
    started = datetime.now(timezone.utc)

    try:
        events = await backend.events()
    except Exception:
        logger.exception("events fetch failed")
        events = []

    session = AgentSession(
        stt=make_stt(),
        llm=openai.LLM(model=LLM_MODEL, temperature=0.6),
        tts=make_tts(),
        vad=ctx.proc.userdata["vad"],
        min_endpointing_delay=MIN_ENDPOINTING_DELAY,
        preemptive_generation=True,
    )
    agent = Zeynep(backend, ctx, events)

    transcript: list[str] = []

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:  # noqa: ANN001
        item = getattr(ev, "item", None)
        text = getattr(item, "text_content", None)
        role = getattr(item, "role", None)
        if text and role in ("user", "assistant"):
            transcript.append(f"{'Misafir' if role == 'user' else 'Zeynep'}: {text}")

    async def on_shutdown() -> None:
        text = "\n".join(transcript)
        fields: dict = {"transcript": text, "ended_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if not agent.note_saved:
            summary = await summarize(text)
            if summary:
                outcome = summary.get("outcome")
                fields.update(
                    outcome=outcome if outcome in OUTCOMES else "info_only",
                    summary=summary.get("summary") or None,
                    guest_name=summary.get("guest_name") or None,
                    phone=summary.get("phone") or None,
                    callback_time=summary.get("callback_time") or None,
                    language=summary.get("language") or None,
                )
        try:
            await backend.save_call(call_id, **fields)
        except Exception:
            logger.exception("final save failed")
        await backend.aclose()

    ctx.add_shutdown_callback(on_shutdown)

    await backend.save_call(call_id, started_at=None)  # kaydı oluştur (görüşme listesinde hemen görünsün)

    await session.start(agent=agent, room=ctx.room)

    # Ofis ortamı ve "düşünürken" klavye sesi
    background = BackgroundAudioPlayer(
        ambient_sound=AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=AMBIENT_VOLUME),
        thinking_sound=[
            AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.7, probability=0.6),
            AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING2, volume=0.7, probability=0.4),
        ],
    )
    await background.start(room=ctx.room, agent_session=session)

    async def limit_call_duration() -> None:
        await asyncio.sleep(MAX_CALL_MINUTES * 60)
        try:
            await session.generate_reply(
                instructions="Süre doldu. Misafire teşekkür et, yetkilinin kendisiyle iletişime geçebileceğini "
                             "söyle ve kısa bir veda cümlesiyle bitir.")
            await asyncio.sleep(10)
        finally:
            await hangup(ctx)

    asyncio.create_task(limit_call_duration())

    await session.generate_reply(
        instructions="Misafiri KİMLİK VE DÜRÜSTLÜK kuralına uygun şekilde, varsayılan olarak Türkçe karşıla.")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
