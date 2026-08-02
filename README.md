# Event-Driven Otomatik Trade Botu — Haber Patlayınca Tetiği Kim Çekiyor?

Dumanı görüp "itfaiye oradaymış" demek kolaydır; zor olan, dumanı gördüğün an telefonu kapıp 112'yi çevirebilmektir. Proje 04'teki ekonomik takvim ajanı sana "dikkat, önemli bir haber geliyor" diye dürtüyordu ve orada duruyordu, karar hâlâ sendeydi. Bu proje onun lafı bırakıp işi bitiren kardeşi: fiyat bir anda sarsıldığında soru sormadan, tereddüt etmeden pozisyon açıyor. Terminator gibi düşünün, hedefi görünce durmuyor. İyi haber şu ki gerçek parayla değil, Alpaca'nın kağıt (paper) hesabıyla çalışıyor, yani kimse gerçekten yanmıyor.

Bu README hem projenin ne yaptığını hem de dürüstçe ne kadar işe yaradığını (ya da yaramadığını) anlatıyor. Sonda uydurma bir "harika sonuç" yok, gerçek sayılar var, kötü olanlar dahil.

## Fikir ne, nereden çıktı

Piyasada bazı günler sıradan değildir: bir hisse günü %3'ten fazla oynar, haber akışı patlar, herkes aynı anda aynı şeyi konuşur. Bu botun tezi basit: büyük ve ani bir fiyat hareketi başlı başına bir "olay"dır, ve bu olaya kural bazlı, duygusuz bir şekilde tepki vermek test edilebilir bir stratejiye dönüşebilir.

Mimari klasik "event-driven" (olay güdümlü) üç katman:

```
TETİKLEYİCİ  →  SİNYAL  →  EMİR
(fiyat şoku)   (yön kararı)  (backtest'te simüle / Alpaca'da gerçek emir)
```

Tetikleyici katmanı, yani execution'ı gerçekten tetikleyen kural, şu: günlük kapanış-kapanış getirisi `|getiri| ≥ %3` olursa "olay" ilan edilir. Bu tanım bilinçli olarak yalnızca fiyattan çıkarılıyor, çünkü bar-kapanışına bağlı, deterministik ve dolayısıyla gerçekten backtest edilebilir. Haberden bir tetikleyici kurmak da mümkündü ama "bugün X hissesi için kaç haber çıktı" bilgisi backtesting motoruna (bar bazlı çalışan `backtesting.py`) temiz şekilde bağlanmıyor, o yüzden haber ikinci bir katman olarak kaldı.

Bu ikinci katman, haber teyit katmanı, tamamen bilgi amaçlı: Finnhub'dan çekilen günlük haber sayısı, şok günlerinin gerçekten "haberli" günler olup olmadığını görselleştirmek için kullanıldı (görsel 1'deki sarı noktalar bunlar). Finnhub'ın ücretsiz planı geriye dönük haberde yaklaşık 6 ay ile sınırlı olduğundan (180 günlük pencere denendi, pratikte 14 farklı günde toplam 1.488 benzersiz haber döndü), 3 yıllık backtest'in tamamını haberle teyit etmek mümkün olmadı. Bu bilinçli bir sınırlama ve aşağıda tekrar geçiyor.

Sinyal ve emir tarafı basit: şok yukarı yöndeyse ertesi barın açılışında LONG, aşağı yöndeyse SHORT açılıyor. Pozisyon en fazla 5 işlem günü tutuluyor, ya da %5 stop-loss / %10 take-profit'e çarpınca kapanıyor.

Sayısallaştırılmış kural seti, robota çevrilebilir, belirsiz sıfat yok:

| Parametre | Değer | Not |
|---|---|---|
| Sembol | NVDA | Yüksek haber hacmi + yüksek volatilite, showcase için uygun |
| Şok eşiği | günlük \|getiri\| ≥ %3 | Varsayım: sektörde yaygın "anlamlı hareket" eşiği, test edilebilir parametre |
| Yön | şokla aynı yön (momentum) | Varsayım: mean-reversion değil, momentum devam hipotezi test edildi |
| Tutma süresi | maksimum 5 işlem günü | Varsayım |
| Stop-loss / Take-profit | %5 / %10 | Varsayım: 1:2 risk/ödül |
| Komisyon | %0.1 | Gerçekçi aracı kurum ücreti varsayımı |
| Başlangıç sermayesi | $100.000 | Alpaca paper hesabındaki gerçek nakitle tutarlı tutuldu |

Look-ahead bias yok: `backtesting.py`'de `next()` fonksiyonu bar kapandıktan sonra çağrılıyor, emir bir sonraki barın açılışında doluyor. Yani "bugünün kapanışını bilerek bugün işlem açmak" gibi bir repaint hatası bulunmuyor.

## Backtest sonucu — dürüst kısım

3 yıllık günlük veri (2023-08-01 ila 2026-07-31, 753 bar) üzerinde 109 işlem üretti. 109 işlem 100'ün üzerinde olduğu için istatistiksel olarak "gürültü" sınırının dışında sayılabilir, ama yine de tek sembol ve tek eşik değeriyle sınırlı bir sonuç.

| Metrik | Değer |
|---|---|
| İşlem sayısı | 109 |
| Toplam getiri | %-29.63 |
| Buy & Hold getirisi (aynı dönem) | %354.36 |
| Profit Factor | 0.94 (1'in altı, kazandırdığından fazlasını kaybediyor demek) |
| Kazanma oranı | %41.3 (bilgi amaçlı, expectancy'ye bakılıyor, winrate'e değil) |
| Max Drawdown | %-60.77 |
| Sharpe Ratio | -0.33 |

Açık söylemek gerekirse strateji net zarar ediyor ve edge'i yok. Aynı dönemde NVDA'yı alıp elde tutmak %354 kazandırırken, bu event-driven strateji sermayenin yaklaşık üçte birini eritiyor. Bunu süslemeden yazıyorum, kötü sonucu gizlemek kaynak israfına yol açar.

Trade'leri LONG (yukarı şok sonrası) ve SHORT (aşağı şok sonrası) olarak ayırınca ilginç bir şey çıktı, bu turun en değerli bulgusu bu oldu:

| Olay türü | İşlem sayısı | Toplam PnL | Kazanma oranı |
|---|---|---|---|
| Yukarı şok (LONG) | 62 | +$18.798 | %46.8 |
| Aşağı şok (SHORT) | 47 | -$46.225 | %34.0 |

LONG bacağı tek başına kârlı; sistemi batıran tamamen SHORT bacağı. Nedeni NVDA'nın 3 yıllık grafiğine bakınca zaten görülüyor (görsel 1): hisse güçlü bir boğa trendinde, aşağı yöndeki ani şoklar çoğunlukla "dip" oluyor ve hızla toparlanıyor. Yani gerçekte mean-reversion (ortalamaya dönüş) baskın, momentum-devam hipotezi aşağı yönde çalışmıyor. Şok yukarıysa momentumu kovalamak makul; şok aşağıysa aynı mantıkla kısa pozisyon açmak, trende karşı bahis oynamak anlamına geliyor.

Bunun bir sonraki adımı belli: yalnızca LONG bacağı alınıp SHORT bacağı tamamen kapatılsaydı, üç yılda yaklaşık +$18.798 (yaklaşık %18.8 getiri, 62 işlemle) elde edilmiş olurdu. Hâlâ Buy & Hold'un çok gerisinde ama en azından pozitif, ve SHORT'un yarattığı -$46.225'lik hasar olmazdı. Bunu kod çalıştırmadan "muhtemelen iyileşir" diye iddia edip geçmiyorum: bu, 62 örneklemlik kendi alt-kümesinin zaten kârlı çıkmasına dayanan güçlü bir hipotez (güven: yüksek), ama tek parametre değiştirilip (yön filtresi: sadece yukarı şok) ayrı bir çalıştırmayla doğrulanması gereken bir hipotez olarak kalıyor, henüz kesinleşmiş değil.

## Alpaca paper trading — gerçekten bağlanabildik mi?

Evet. Script çalıştırıldığında Alpaca paper trading hesabına gerçekten bağlanıldı, hesap bilgisi okundu ve 1 adet NVDA için gerçek bir market emri gönderildi:

| Alan | Değer |
|---|---|
| Hesap durumu | `ACTIVE` |
| Nakit | $100.000,00 |
| Alım gücü | $400.000,00 |
| Emir ID | `38dc7248-3424-4826-ae38-f30959517d6e` |
| Emir | 1 adet NVDA, MARKET, BUY, GTC |
| Emir durumu | `ACCEPTED` |

Dürüstlük payı burada da lazım: script'in çalıştığı an piyasa kapalıydı (hafta sonu, sonraki açılış Pazartesi 2026-08-03 09:30 New York saati). Bu yüzden emir `ACCEPTED` durumunda kaldı, henüz `FILLED` (dolmuş) değil. GTC (good-till-cancelled) olarak gönderildiği için piyasa açılınca dolması bekleniyor. Emri sahte bir "doldu" görünümüyle süslemedim, gerçek durum neyse o raporlanıyor. Bağlantının ve emir gönderiminin gerçek olduğunun kanıtı ortada: gerçek bir UUID formatlı emir ID'si dönmüş olması ve tablodaki her satırın Alpaca'nın kendi hesap/emir API'sinden okunmuş olması.

## Görseller

`gorseller/` altında 6 görsel var, her biri `.html` (etkileşimli) ve `.png` (statik) olarak kaydedildi:

1. `01_tetikleyici_olaylar_fiyat_grafigi` — NVDA fiyat grafiği üzerinde yukarı (mavi üçgen) ve aşağı (kırmızı üçgen) şok günleri işaretli, haber hacmiyle teyitli günler sarı nokta ile ayrıca vurgulanmış.
2. `02_backtest_equity_curve` — Stratejinin 3 yıllık portföy değeri eğrisi, yukarıda anlatılan zararı görsel olarak gösteriyor.
3. `03_olay_turune_gore_performans` — LONG ile SHORT bacağının toplam kâr/zarar karşılaştırması (yukarıdaki tablo).
4. `04_giris_cikis_zamanlama_dagilimi` — Pozisyonların kaç işlem günü tutulduğunun histogramı. Çoğu işlem ya çok hızlı SL/TP'ye çarpıp kapanıyor ya da 5-6 barlık zaman aşımına kadar gidiyor.
5. `05_kagit_ticaret_sonuc_panosu` — Alpaca paper hesabı ve demo emrinin durumunu gösteren kanıt panosu.
6. `06_getiri_dagilimi` — 109 işlemin getiri yüzdesi dağılımı (histogram), ortalama getiri turuncu çizgiyle işaretli.

## Kurulum ve çalıştırma

```bash
cd ~/Desktop/quant_projeleri/35_event_driven_trade_botu
~/Desktop/quant_projeleri/.venv/bin/python proje.py
```

Ortam zaten hazır kuruluysa (`requirements.txt`'teki sürümler `~/Desktop/quant_projeleri/.venv` içinde mevcut), tek komutla uçtan uca çalışır: veri çeker, backtest yapar, Alpaca'ya gerçek bir demo emri gönderir, 6 görseli üretir. `.env` dosyasındaki `FINNHUB_API_KEY` ve `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` değerleri kullanılır, değiştirilmedi.

## Bilinçli sınırlamalar ve dürüst notlar

Strateji kârsız çıktı, ve bu showcase'i kurtarmak için gizlenecek bir detay değil. Buradaki asıl hedef event-driven mimarinin uçtan uca çalıştığını kanıtlamaktı, o hedef gerçekleşti. Kârlılık ayrı bir soru, ve o sorunun cevabı bu haliyle hayır.

SHORT bacağı elenmeli. Yukarıdaki analiz net: sistemin tüm zararı SHORT (aşağı şok) trade'lerinden geliyor. Boğa trendindeki bir hissede trende karşı momentum oynamak, ki burada aslında mean-reversion'a karşı bahis oynanmış oluyor, sistematik olarak kaybettiriyor.

Haber teyit katmanı sınırlı kaldı. Finnhub ücretsiz planında geriye dönük haber erişimi yaklaşık 6 ay ile sınırlı; 3 yıllık backtest'in tamamı haberle çapraz doğrulanamadı. Bu bilinçli bir kapsam sınırlaması, gizlenmedi.

Piyasa kapalıyken test edildi. Alpaca emri gerçek ve kabul edildi ama script çalıştığı anda hafta sonu olduğu için dolmadı. Bağlantının ve emir mekanizmasının çalıştığını kanıtlamak için yeterli; "gerçek zamanlı execution" iddiası için piyasa açıkken tekrar çalıştırılması gerekir.

Tek sembol, tek parametre seti kullanıldı. Overfitting riskinden kaçınmak için parametreler (eşik, tutma süresi, SL/TP) tek bir makul varsayım seti olarak bırakıldı, cımbızlanmadı. Yani sonuç "en iyi NVDA stratejisi" değil, ilk makul varsayımların dürüst sonucu.

---

Bu proje, `quant_projeleri` klasöründeki 9 bağımsız showcase projesinden biri. Kod, veri ve görseller `35_event_driven_trade_botu/` altında; başka projelere dokunulmadı.
