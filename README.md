# Zeynep: etkinlik paketi satan sesli asistan (demo)

Tarayıcıdan sesli görüşme yapılan bir demo. **Zeynep**, otele misafir getiren bir turizm acentesinin dijital asistanıdır:
etkinlik paketlerini (örn. konser haftasonu) anlatır, canlı müsaitliği kontrol eder, paket doluysa üst paketi önerir,
odayı 24 saat tutar, görüşme notunu ve transkripti yönetim paneline kaydeder. Arka planda hafif ofis ortamı sesi çalar,
bir şey kontrol ederken klavye sesi duyulur.

## Parçalar

| Parça | Ne yapar |
|---|---|
| `backend/` (FastAPI + SQLite) | Etkinlik/paket/kontenjan, oda tutma, yukarı satış önerisi, görüşme kayıtları, LiveKit jetonu |
| `agent/` (LiveKit Agents) | Zeynep: ses (ElevenLabs), anlama (OpenAI), araçlar, ofis ortamı ve klavye sesi |
| `web/call.html` | Misafirin tıkladığı arama sayfası (`/call?key=...`) |
| `web/admin.html` | Yönetim paneli (`/admin`): görüşmeler ve notlar, teklifler/paketler, tutulan yerler, **Senaryo** (firma adı, açılış cümlesi, senaryo metni; deploy gerekmez) |

Satış kuralları (hangi paket önerilir, indirim sınırı) kodda ve veride tanımlıdır; yapay zekâ fiyat veya müsaitlik uyduramaz,
bu bilgileri yalnızca araçlardan alır.

## Gerekli hesaplar ve anahtarlar

1. **LiveKit Cloud** (cloud.livekit.io): proje oluşturun, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` alın.
2. **OpenAI**: `OPENAI_API_KEY` (anlama, konuşma tanıma ve görüşme özeti için).
3. **ElevenLabs**: `ELEVEN_API_KEY` ve Zeynep için seçtiğiniz kadın sesin `ELEVEN_VOICE_ID` değeri.
   Sesi seçmeden önce Türkçe bir cümleyle deneyin; Türkçe kalitesi sese göre değişir.

Anahtarları sohbete veya depoya yazmayın; yalnızca Dokploy > **Environment** bölümüne girin (`.env.example` şablondur).

## Dokploy'da yayına alma

1. Dokploy'da yeni bir **Compose** uygulaması oluşturun, bu depoyu bağlayın, derleme dosyası `docker-compose.yml`.
2. **Environment** bölümüne `.env.example` içindeki değişkenleri doldurarak girin.
   `ADMIN_KEY`, `INTERNAL_KEY` ve `CALL_PAGE_KEY` için uzun rastgele değerler üretin.
3. **Domains** bölümünde servis olarak `api`, port olarak `8000` seçin (aşağıdaki HTTPS notuna bakın).
4. Deploy edin. Açılış: `https://ALAN-ADI/call?key=CALL_PAGE_KEY`, panel: `https://ALAN-ADI/admin`.

### Önemli: mikrofon için HTTPS gerekir

Tarayıcılar mikrofonu yalnızca HTTPS sayfalarda verir. Alan adı alınana kadar sunucu IP'sine bağlı ücretsiz bir adres
kullanılabilir: sunucu IP'si `185.97.146.46` ise `185-97-146-46.sslip.io` bu IP'ye çözülür. Dokploy'da bu adresi domain olarak
ekleyip sertifika türünü **Let's Encrypt** seçmeniz yeterlidir. Kalıcı alan adı alınca yalnızca domain değiştirilir.

## Yerel deneme (yalnızca iş mantığı testi)

```bash
cd backend
python3 -m unittest discover -s tests -v
```

## Güvenlik ve uyum notları

- `/call` sayfası `CALL_PAGE_KEY` ile korunur; saatlik görüşme sayısı (`MAX_CALLS_PER_HOUR`) ve görüşme süresi
  (`MAX_CALL_MINUTES`) sınırlıdır. Böylece bağlantı sızsa bile maliyet kontrol altında kalır.
- Zeynep görüşmeye kendini "dijital asistan" olarak tanıtır ve sorulursa yapay zekâ olduğunu söyler. Sayfada ayrıca kayıt bilgisi vardır.
  Canlıya geçerken KVKK aydınlatma metni ve gerçek telefon/giden arama için izin (İYS vb.) süreçleri eklenmelidir.
- Zeynep kart/ödeme bilgisi istemez; ödeme için yetkiliye yönlendirir.

## Bu sürümde doğrulanmamış noktalar

Kod, paket kurulumu olmayan bir ortamda yazıldı. İş mantığı testleri çalıştırıldı (15 test geçti), ancak aşağıdakiler ilk
deploy'da gerçek ortamda denenmelidir:

- `agent/agent.py` içinde LiveKit Agents API'si (`conversation_item_added` olayı, `BackgroundAudioPlayer`, `detect_language`).
  Sürüm farkı olursa derleme/çalışma günlüğünde hata görünür ve düzeltilir.
- `web/call.html` içinde LiveKit istemci kütüphanesi CDN adresi.
- Türkçe ses kalitesi ve gecikme (sese ve model seçimine göre ayarlanır).
