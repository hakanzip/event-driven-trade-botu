"""
proje.py — Event-Driven Otomatik Trade Botu
=============================================

Bu script, "dikkat haber geliyor" diyen ekonomik takvim ajanının (proje 04)
tetiği çeken kardeşidir: bir olay (burada büyük günlük fiyat şoku + o şoku
teyit eden haber hacmi) gerçekleştiğinde kural bazlı bir pozisyon açar.

Akış (uçtan uca, tek çalıştırma):
    1) yfinance'ten NVDA günlük fiyat verisi çekilir (uzun vadeli backtest için)
    2) Finnhub'dan aynı sembol için son dönem haber akışı çekilir
    3) "Olay" tanımı: günlük |getiri| >= ESIK (fiyat şoku) — bu deterministik
       ve bar-kapanışına dayalı olduğu için backtesting.py ile test edilebilir.
       Haber hacmi bu şokları "teyit" etmek için ikinci, görsel bir katman
       olarak eklenir (asıl execution tetikleyicisi HER ZAMAN fiyat şokudur).
    4) backtesting.py ile kural bazlı bir "olay tetikli momentum" stratejisi
       geriye dönük test edilir (repaint yok: sinyal barın KAPANIŞINDA
       üretilir, emir bir SONRAKI barın açılışında gerçekleşir).
    5) Alpaca paper trading hesabına gerçekten bağlanılır, hesap bilgisi
       okunur ve TEK bir küçük demo emri gönderilip durumu sorgulanır.
    6) 6 adet plotly görseli (.html + .png) gorseller/ altına yazılır.

Kullanım:
    ~/Desktop/quant_projeleri/.venv/bin/python proje.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# 0) Ortam / sabitler
# ----------------------------------------------------------------------------

PROJE_KOK = Path(__file__).resolve().parent
VERI_DIZINI = PROJE_KOK / "veri"
GORSEL_DIZINI = PROJE_KOK / "gorseller"
ENV_DOSYASI = PROJE_KOK.parent / ".env"

VERI_DIZINI.mkdir(parents=True, exist_ok=True)
GORSEL_DIZINI.mkdir(parents=True, exist_ok=True)

load_dotenv(ENV_DOSYASI)

import os  # noqa: E402  (dotenv'den sonra okunmalı)

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
ALPACA_API_KEY_ID = os.getenv("ALPACA_API_KEY_ID")
ALPACA_API_SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# --- Strateji sabitleri (sayısallaştırılmış, robota çevrilebilir kurallar) ---
SEMBOL = "NVDA"                 # Yüksek hacimli haber akışı + yüksek volatilite -> event-driven showcase için uygun
FIYAT_PERIYODU = "3y"           # yfinance periyodu (günlük bar)
SOK_ESIGI = 0.03                # Günlük |getiri| >= %3 => "fiyat şoku" olayı (VARSAYIM, aşağıda gerekçeli)
TUTMA_SURESI_BAR = 5            # Pozisyon en fazla 5 iş günü tutulur (zaman-bazlı çıkış)
STOP_LOSS_YUZDE = 0.05          # %5 stop-loss
TAKE_PROFIT_YUZDE = 0.10        # %10 take-profit (1:2 risk/ödül, sektör standardı bir varsayım)
KOMISYON = 0.001                # %0.1 komisyon varsayımı (gerçekçi aracı kurum ücreti)
BASLANGIC_SERMAYE = 100_000     # Alpaca paper hesabındaki gerçek nakit ile tutarlı

HABER_GUN_SAYISI = 180          # Finnhub'dan geriye dönük kaç gün haber çekilecek (teyit katmanı için)
HABER_PENCERE_GUN = 30          # Tek bir Finnhub isteğinin kapsadığı gün aralığı (rate-limit dostu)

print("=" * 78)
print("EVENT-DRIVEN OTOMATİK TRADE BOTU — proje.py")
print(f"Sembol: {SEMBOL} | Şok eşiği: %{SOK_ESIGI*100:.1f} | Tutma süresi: {TUTMA_SURESI_BAR} bar")
print("=" * 78)


# ----------------------------------------------------------------------------
# 1) Fiyat verisi (yfinance)
# ----------------------------------------------------------------------------

def fiyat_verisi_cek(sembol: str, periyot: str) -> pd.DataFrame:
    """yfinance'ten günlük OHLCV verisi çeker, backtesting.py'nin beklediği
    Open/High/Low/Close/Volume kolon isimlerine indirger (MultiIndex'i düzleştirir)."""
    import yfinance as yf

    print(f"\n[1/6] Fiyat verisi çekiliyor: {sembol} ({periyot}, günlük)...")
    df = yf.download(sembol, period=periyot, interval="1d", auto_adjust=True, progress=False)

    if df is None or df.empty:
        raise RuntimeError(f"yfinance'ten {sembol} için veri gelmedi.")

    # yfinance tekli sembolde bile MultiIndex kolon döndürebiliyor (Price, Ticker)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index.name = "Tarih"
    df = df.dropna()

    print(f"   -> {len(df)} günlük bar alındı ({df.index[0].date()} .. {df.index[-1].date()})")
    df.to_csv(VERI_DIZINI / f"{sembol.lower()}_fiyat_ham.csv")
    return df


# ----------------------------------------------------------------------------
# 2) Haber verisi (Finnhub) — teyit katmanı
# ----------------------------------------------------------------------------

def haber_verisi_cek(sembol: str, gun_sayisi: int, pencere_gun: int = 30) -> pd.DataFrame:
    """Finnhub company-news uç noktasından geriye dönük haber çeker.
    429 (rate limit) alırsa birkaç saniye bekleyip tekrar dener (aynı anahtarı
    başka ajanlar da kullanıyor olabilir)."""
    if not FINNHUB_API_KEY:
        print("\n[2/6] UYARI: FINNHUB_API_KEY bulunamadı, haber teyit katmanı atlanıyor.")
        return pd.DataFrame(columns=["tarih", "haber_sayisi"])

    print(f"\n[2/6] Finnhub'dan haber akışı çekiliyor: {sembol}, son {gun_sayisi} gün...")
    bugun = datetime.now(timezone.utc).date()
    tum_haberler = []

    pencere_sayisi = max(1, gun_sayisi // pencere_gun)
    for i in range(pencere_sayisi):
        bitis = bugun - timedelta(days=i * pencere_gun)
        baslangic = bitis - timedelta(days=pencere_gun)

        params = {
            "symbol": sembol,
            "from": baslangic.isoformat(),
            "to": bitis.isoformat(),
            "token": FINNHUB_API_KEY,
        }

        for deneme in range(4):
            try:
                r = requests.get("https://finnhub.io/api/v1/company-news", params=params, timeout=15)
            except requests.RequestException as e:
                print(f"   ! ağ hatası ({baslangic}..{bitis}): {e} -> 3sn bekleyip tekrar deneniyor")
                time.sleep(3)
                continue

            if r.status_code == 429:
                bekleme = 3 + deneme * 2
                print(f"   ! 429 rate-limit ({baslangic}..{bitis}), {bekleme}sn bekleniyor...")
                time.sleep(bekleme)
                continue
            if r.status_code != 200:
                print(f"   ! Finnhub hata {r.status_code} ({baslangic}..{bitis}): {r.text[:150]}")
                break

            veri = r.json()
            if isinstance(veri, list):
                tum_haberler.extend(veri)
                print(f"   -> {baslangic}..{bitis}: {len(veri)} haber")
            break

        time.sleep(1.1)  # kibarca hız sınırlaması

    if not tum_haberler:
        print("   -> Hiç haber alınamadı, teyit katmanı boş kalacak.")
        return pd.DataFrame(columns=["tarih", "haber_sayisi"])

    hdf = pd.DataFrame(tum_haberler).drop_duplicates(subset=["id"])
    hdf["tarih"] = pd.to_datetime(hdf["datetime"], unit="s", utc=True).dt.date
    gunluk = hdf.groupby("tarih").size().reset_index(name="haber_sayisi")

    gunluk.to_json(VERI_DIZINI / f"{sembol.lower()}_haber_gunluk.json", orient="records", date_format="iso")
    hdf.to_csv(VERI_DIZINI / f"{sembol.lower()}_haber_ham.csv", index=False)

    print(f"   -> Toplam {len(hdf)} benzersiz haber, {len(gunluk)} farklı günde.")
    return gunluk


# ----------------------------------------------------------------------------
# 3) Tetikleyici (olay) tanımı
# ----------------------------------------------------------------------------

def tetikleyicileri_hesapla(fiyat_df: pd.DataFrame, haber_gunluk: pd.DataFrame, esik: float) -> pd.DataFrame:
    """Günlük getiriyi hesaplar, |getiri| >= esik olan günleri 'olay' işaretler.
    Haber verisi varsa, olay gününde/1 gün öncesinde günlük ortalamanın üstünde
    haber trafiği olup olmadığını 'haber_teyitli' bayrağıyla ekler (yalnız
    bilgi/görselleştirme amaçlı — stratejinin execution tetikleyicisi DEĞİL)."""
    print(f"\n[3/6] Tetikleyiciler hesaplanıyor (eşik: %{esik*100:.1f})...")
    df = fiyat_df.copy()
    df["getiri"] = df["Close"].pct_change()
    df["olay"] = df["getiri"].abs() >= esik
    df["yon"] = np.where(df["getiri"] > 0, "yukari", np.where(df["getiri"] < 0, "asagi", "notr"))

    if not haber_gunluk.empty:
        haber_map = dict(zip(haber_gunluk["tarih"], haber_gunluk["haber_sayisi"]))
        ort_haber = haber_gunluk["haber_sayisi"].mean()

        def haber_teyit(tarih) -> bool:
            t = tarih.date() if hasattr(tarih, "date") else tarih
            gun0 = haber_map.get(t, 0)
            gun_eksi1 = haber_map.get(t - timedelta(days=1), 0)
            return (gun0 + gun_eksi1) > ort_haber

        df["haber_teyitli"] = [haber_teyit(t) for t in df.index]
    else:
        df["haber_teyitli"] = False

    olay_sayisi = int(df["olay"].sum())
    teyitli_sayisi = int((df["olay"] & df["haber_teyitli"]).sum())
    print(f"   -> {olay_sayisi} olay günü tespit edildi "
          f"({int((df['olay'] & (df['yon']=='yukari')).sum())} yukarı / "
          f"{int((df['olay'] & (df['yon']=='asagi')).sum())} aşağı), "
          f"bunların {teyitli_sayisi} tanesi haber hacmiyle teyitli.")
    return df


# ----------------------------------------------------------------------------
# 4) Backtest — kural bazlı olay-tetikli momentum stratejisi
# ----------------------------------------------------------------------------

def backtest_calistir(fiyat_df: pd.DataFrame):
    """backtesting.py ile: dünün kapanışında |getiri|>=eşik oluşursa BUGÜNÜN
    açılışında şok yönünde pozisyon aç, TUTMA_SURESI_BAR bar sonra ya da
    SL/TP'ye çarpınca kapat. Look-ahead yok: next() bar KAPANDIKTAN sonra
    çağrılır, backtesting.py emri bir sonraki barın açılışında doldurur."""
    from backtesting import Backtest, Strategy

    print("\n[4/6] Backtest çalıştırılıyor (backtesting.py)...")

    class OlayTetikliMomentum(Strategy):
        esik = SOK_ESIGI
        tutma_suresi = TUTMA_SURESI_BAR
        sl_yuzde = STOP_LOSS_YUZDE
        tp_yuzde = TAKE_PROFIT_YUZDE

        def init(self):
            close = pd.Series(self.data.Close)
            self.getiri = self.I(lambda c: pd.Series(c).pct_change().values, close, name="gunluk_getiri")

        def next(self):
            i = len(self.data) - 1

            if self.position:
                acik_trade = self.trades[-1] if self.trades else None
                if acik_trade is not None and (i - acik_trade.entry_bar) >= self.tutma_suresi:
                    self.position.close()
                return

            r = self.getiri[-1]
            if np.isnan(r):
                return

            fiyat = self.data.Close[-1]
            if r >= self.esik:
                self.buy(sl=fiyat * (1 - self.sl_yuzde), tp=fiyat * (1 + self.tp_yuzde))
            elif r <= -self.esik:
                self.sell(sl=fiyat * (1 + self.sl_yuzde), tp=fiyat * (1 - self.tp_yuzde))

    bt = Backtest(fiyat_df[["Open", "High", "Low", "Close", "Volume"]], OlayTetikliMomentum,
                  cash=BASLANGIC_SERMAYE, commission=KOMISYON, exclusive_orders=True)
    stats = bt.run()

    trade_sayisi = int(stats["# Trades"])
    print(f"   -> İşlem sayısı: {trade_sayisi}")
    if trade_sayisi < 100:
        print(f"   -> UYARI: İşlem sayısı 100'ün altında ({trade_sayisi}). "
              f"İstatistiksel anlam SINIRLI, sonuçlar gürültü içerebilir (Kural 3.11).")

    ozet = {
        "islem_sayisi": trade_sayisi,
        "getiri_yuzde": float(stats["Return [%]"]),
        "buy_hold_getiri_yuzde": float(stats["Buy & Hold Return [%]"]),
        "profit_factor": None if pd.isna(stats.get("Profit Factor", np.nan)) else float(stats["Profit Factor"]),
        "kazanma_orani_yuzde": float(stats["Win Rate [%]"]) if trade_sayisi else None,
        "max_drawdown_yuzde": float(stats["Max. Drawdown [%]"]),
        "sharpe": None if pd.isna(stats.get("Sharpe Ratio", np.nan)) else float(stats["Sharpe Ratio"]),
        "sure": str(stats["Duration"]),
    }
    print(f"   -> Getiri: %{ozet['getiri_yuzde']:.2f}  (Buy&Hold: %{ozet['buy_hold_getiri_yuzde']:.2f})")
    pf_str = f"{ozet['profit_factor']:.2f}" if ozet['profit_factor'] is not None else "N/A"
    print(f"   -> Profit Factor: {pf_str} | Max Drawdown: %{ozet['max_drawdown_yuzde']:.2f}")

    with open(VERI_DIZINI / "backtest_sonuc_ozeti.json", "w", encoding="utf-8") as f:
        json.dump(ozet, f, ensure_ascii=False, indent=2)

    return stats, ozet


# ----------------------------------------------------------------------------
# 5) Alpaca paper trading — gerçek bağlantı + tek demo emri
# ----------------------------------------------------------------------------

def alpaca_demo_emri_gonder(sembol: str) -> dict:
    """Alpaca paper trading hesabına bağlanır, hesap bilgisini okur, 1 adet
    market emri gönderir (GTC — piyasa kapalıyken de sistemde kalıp bir
    sonraki açılışta gerçekleşebilsin diye) ve emrin GÜNCEL durumunu
    sorgulayıp döner. Sürekli çalışan bir bot DEĞİL — tek seferlik kanıt."""
    print("\n[5/6] Alpaca paper trading hesabına bağlanılıyor...")

    sonuc = {
        "baglanti_basarili": False,
        "hesap": None,
        "piyasa_acik_mi": None,
        "emir_gonderildi": False,
        "emir": None,
        "hata": None,
    }

    if not (ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY):
        sonuc["hata"] = "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY .env içinde bulunamadı."
        print(f"   ! {sonuc['hata']}")
        return sonuc

    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        tc = TradingClient(ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY, paper=True)
        hesap = tc.get_account()
        saat = tc.get_clock()

        sonuc["baglanti_basarili"] = True
        sonuc["hesap"] = {
            "durum": str(hesap.status),
            "nakit": float(hesap.cash),
            "alim_gucu": float(hesap.buying_power),
            "portfoy_degeri": float(hesap.portfolio_value),
        }
        sonuc["piyasa_acik_mi"] = bool(saat.is_open)
        sonuc["sonraki_acilis"] = str(saat.next_open)

        print(f"   -> Bağlantı BAŞARILI. Hesap durumu: {sonuc['hesap']['durum']}, "
              f"nakit: ${sonuc['hesap']['nakit']:,.2f}")
        print(f"   -> Piyasa şu an {'AÇIK' if sonuc['piyasa_acik_mi'] else 'KAPALI'} "
              f"(sonraki açılış: {sonuc['sonraki_acilis']})")

        emir_istegi = MarketOrderRequest(
            symbol=sembol,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        )
        print(f"   -> Demo emri gönderiliyor: 1 adet {sembol}, MARKET, BUY, GTC...")
        emir = tc.submit_order(emir_istegi)
        sonuc["emir_gonderildi"] = True

        time.sleep(2)
        guncel_emir = tc.get_order_by_id(emir.id)

        sonuc["emir"] = {
            "id": str(guncel_emir.id),
            "sembol": guncel_emir.symbol,
            "adet": float(guncel_emir.qty) if guncel_emir.qty else None,
            "yon": str(guncel_emir.side),
            "tip": str(guncel_emir.order_type),
            "durum": str(guncel_emir.status),
            "gonderim_zamani": str(guncel_emir.submitted_at),
            "dolan_fiyat": float(guncel_emir.filled_avg_price) if guncel_emir.filled_avg_price else None,
        }
        print(f"   -> Emir gönderildi. ID: {sonuc['emir']['id']}")
        print(f"   -> Emir durumu: {sonuc['emir']['durum']}")
        if sonuc["emir"]["dolan_fiyat"]:
            print(f"   -> Dolan fiyat: ${sonuc['emir']['dolan_fiyat']:.2f}")
        elif not sonuc["piyasa_acik_mi"]:
            print("   -> Piyasa kapalı olduğu için emir henüz DOLMADI (bir sonraki açılışta gerçekleşecek).")

    except Exception as e:
        sonuc["hata"] = f"{type(e).__name__}: {e}"
        print(f"   ! Alpaca işlemi sırasında hata: {sonuc['hata']}")

    with open(VERI_DIZINI / "alpaca_demo_emri_sonucu.json", "w", encoding="utf-8") as f:
        json.dump(sonuc, f, ensure_ascii=False, indent=2, default=str)

    return sonuc


# ----------------------------------------------------------------------------
# 6) Görselleştirme (plotly) — 6 grafik, .html + .png
# ----------------------------------------------------------------------------

# dataviz skill paletinden alınan roller (light-mode chart chrome + kategorik/status renkler)
RENK = {
    "surface": "#fcfcfb",
    "primary_ink": "#0b0b0b",
    "secondary_ink": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "baseline": "#c3c2b7",
    "mavi": "#2a78d6",      # kategorik slot 1 — LONG / yukarı şok
    "kirmizi": "#e34948",   # kategorik slot 8 — SHORT / aşağı şok
    "yesil_good": "#0ca30c",
    "kirmizi_kritik": "#d03b3b",
    "sari": "#eda100",
    "turuncu": "#eb6834",
}

PLOTLY_TEMA = dict(
    plot_bgcolor=RENK["surface"],
    paper_bgcolor=RENK["surface"],
    font=dict(family="system-ui, -apple-system, 'Segoe UI', sans-serif", color=RENK["primary_ink"], size=13),
    xaxis=dict(gridcolor=RENK["grid"], linecolor=RENK["baseline"], zerolinecolor=RENK["baseline"]),
    yaxis=dict(gridcolor=RENK["grid"], linecolor=RENK["baseline"], zerolinecolor=RENK["baseline"]),
    legend=dict(bgcolor="rgba(0,0,0,0)"),
    margin=dict(l=60, r=30, t=70, b=50),
)


def _kaydet(fig: go.Figure, dosya_adi: str):
    fig.update_layout(**PLOTLY_TEMA)
    html_yolu = GORSEL_DIZINI / f"{dosya_adi}.html"
    png_yolu = GORSEL_DIZINI / f"{dosya_adi}.png"
    fig.write_html(html_yolu)
    fig.write_image(png_yolu, width=1280, height=720, scale=2)
    print(f"   -> {html_yolu.name} + {png_yolu.name} yazıldı.")


def gorsel_1_tetikleyici_olaylar(df: pd.DataFrame):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df.index, y=df["Close"], mode="lines",
                              line=dict(color=RENK["muted"], width=1.5), name=SEMBOL))

    yukari = df[df["olay"] & (df["yon"] == "yukari")]
    asagi = df[df["olay"] & (df["yon"] == "asagi")]

    fig.add_trace(go.Scatter(x=yukari.index, y=yukari["Close"], mode="markers",
                              marker=dict(color=RENK["mavi"], size=9, symbol="triangle-up",
                                          line=dict(color=RENK["surface"], width=1)),
                              name=f"Yukarı şok (≥%{SOK_ESIGI*100:.0f})"))
    fig.add_trace(go.Scatter(x=asagi.index, y=asagi["Close"], mode="markers",
                              marker=dict(color=RENK["kirmizi"], size=9, symbol="triangle-down",
                                          line=dict(color=RENK["surface"], width=1)),
                              name=f"Aşağı şok (≤-%{SOK_ESIGI*100:.0f})"))

    teyitli = df[df["olay"] & df["haber_teyitli"]]
    if not teyitli.empty:
        fig.add_trace(go.Scatter(x=teyitli.index, y=teyitli["Close"] * 1.03, mode="markers",
                                  marker=dict(color=RENK["sari"], size=6, symbol="circle"),
                                  name="Haber hacmiyle teyitli"))

    fig.update_layout(title=f"{SEMBOL} — Tetikleyici Olaylar (Fiyat Şoku) Fiyat Üzerinde",
                       xaxis_title="Tarih", yaxis_title="Kapanış Fiyatı ($)")
    _kaydet(fig, "01_tetikleyici_olaylar_fiyat_grafigi")


def gorsel_2_equity_curve(stats):
    egri = stats["_equity_curve"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=egri.index, y=egri["Equity"], mode="lines",
                              line=dict(color=RENK["mavi"], width=2), fill="tozeroy",
                              fillcolor="rgba(42,120,214,0.08)", name="Portföy Değeri"))
    fig.add_trace(go.Scatter(x=egri.index, y=[BASLANGIC_SERMAYE] * len(egri), mode="lines",
                              line=dict(color=RENK["baseline"], width=1, dash="dash"),
                              name=f"Başlangıç sermayesi (${BASLANGIC_SERMAYE:,.0f})"))
    fig.update_layout(title="Backtest Equity Curve — Olay Tetikli Momentum Stratejisi",
                       xaxis_title="Bar (işlem günü)", yaxis_title="Portföy Değeri ($)")
    _kaydet(fig, "02_backtest_equity_curve")


def gorsel_3_olay_turune_gore_performans(stats):
    trades = stats["_trades"].copy()
    if trades.empty:
        print("   -> UYARI: Hiç trade oluşmadı, görsel 3 boş bir uyarı grafiği olarak üretiliyor.")
        fig = go.Figure()
        fig.add_annotation(text="Backtest'te hiç işlem oluşmadı", showarrow=False,
                            font=dict(size=16, color=RENK["muted"]))
        _kaydet(fig, "03_olay_turune_gore_performans")
        return

    trades["yon"] = np.where(trades["Size"] > 0, "Yukarı şok (LONG)", "Aşağı şok (SHORT)")
    ozet = trades.groupby("yon").agg(
        islem_sayisi=("PnL", "size"),
        toplam_pnl=("PnL", "sum"),
        ort_getiri_yuzde=("ReturnPct", lambda x: x.mean() * 100),
        kazanan_oran_yuzde=("PnL", lambda x: (x > 0).mean() * 100),
    ).reset_index()

    fig = go.Figure()
    renkler = [RENK["mavi"] if y.startswith("Yukarı") else RENK["kirmizi"] for y in ozet["yon"]]
    fig.add_trace(go.Bar(x=ozet["yon"], y=ozet["toplam_pnl"], marker_color=renkler,
                          text=[f"${v:,.0f}<br>n={n}" for v, n in zip(ozet["toplam_pnl"], ozet["islem_sayisi"])],
                          textposition="outside", name="Toplam PnL ($)"))
    fig.update_layout(title="Olay Türüne Göre Trade Performansı (Toplam PnL)",
                       xaxis_title="Olay Türü", yaxis_title="Toplam Kâr/Zarar ($)")
    _kaydet(fig, "03_olay_turune_gore_performans")


def gorsel_4_zamanlama_dagilimi(stats):
    trades = stats["_trades"].copy()
    if trades.empty:
        fig = go.Figure()
        fig.add_annotation(text="Backtest'te hiç işlem oluşmadı", showarrow=False,
                            font=dict(size=16, color=RENK["muted"]))
        _kaydet(fig, "04_giris_cikis_zamanlama_dagilimi")
        return

    trades["tutma_suresi_bar"] = trades["ExitBar"] - trades["EntryBar"]
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=trades["tutma_suresi_bar"], marker_color=RENK["mavi"],
                                xbins=dict(size=1), name="Tutma süresi (bar)"))
    fig.update_layout(title="Giriş/Çıkış Zamanlaması — Pozisyon Tutma Süresi Dağılımı",
                       xaxis_title="Tutma Süresi (işlem günü sayısı)", yaxis_title="İşlem Sayısı")
    _kaydet(fig, "04_giris_cikis_zamanlama_dagilimi")


def gorsel_5_kagit_ticaret_panosu(alpaca_sonuc: dict):
    basarili = alpaca_sonuc.get("baglanti_basarili", False)
    hesap = alpaca_sonuc.get("hesap") or {}
    emir = alpaca_sonuc.get("emir") or {}

    if basarili:
        durum_rengi = RENK["yesil_good"] if alpaca_sonuc.get("emir_gonderildi") else RENK["kirmizi_kritik"]
        durum_metni = "BAĞLANTI BAŞARILI" if alpaca_sonuc.get("emir_gonderildi") else "BAĞLANTI OK, EMİR BAŞARISIZ"
    else:
        durum_rengi = RENK["kirmizi_kritik"]
        durum_metni = "BAĞLANTI BAŞARISIZ"

    satirlar = [
        ["Hesap Durumu", hesap.get("durum", "N/A")],
        ["Nakit", f"${hesap.get('nakit', 0):,.2f}" if hesap else "N/A"],
        ["Alım Gücü", f"${hesap.get('alim_gucu', 0):,.2f}" if hesap else "N/A"],
        ["Portföy Değeri", f"${hesap.get('portfoy_degeri', 0):,.2f}" if hesap else "N/A"],
        ["Piyasa Şu An", "AÇIK" if alpaca_sonuc.get("piyasa_acik_mi") else "KAPALI"],
        ["Emir ID", emir.get("id", "—")],
        ["Emir Sembolü / Adet", f"{emir.get('sembol','—')} / {emir.get('adet','—')}"],
        ["Emir Yönü / Tipi", f"{emir.get('yon','—')} / {emir.get('tip','—')}"],
        ["Emir Durumu", emir.get("durum", alpaca_sonuc.get("hata") or "—")],
        ["Dolan Fiyat", f"${emir['dolan_fiyat']:.2f}" if emir.get("dolan_fiyat") else "Henüz dolmadı"],
    ]

    fig = go.Figure()
    fig.add_trace(go.Indicator(
        mode="number",
        value=1,
        number={"font": {"size": 1, "color": RENK["surface"]}},
        domain={"x": [0, 1], "y": [0.85, 1]},
    ))
    fig.add_trace(go.Table(
        header=dict(values=["<b>Alan</b>", "<b>Değer</b>"], fill_color=RENK["mavi"],
                    font=dict(color="white", size=14), align="left", height=32),
        cells=dict(values=[[s[0] for s in satirlar], [s[1] for s in satirlar]],
                   fill_color=[[RENK["surface"]] * len(satirlar)],
                   font=dict(color=RENK["primary_ink"], size=13), align="left", height=30),
        domain={"x": [0, 1], "y": [0, 0.8]},
    ))
    fig.update_layout(title=f"Kağıt-Ticaret (Alpaca Paper) Sonuç Panosu — {durum_metni}",
                       height=560)
    fig.update_layout(**{k: v for k, v in PLOTLY_TEMA.items() if k not in ("xaxis", "yaxis")})
    html_yolu = GORSEL_DIZINI / "05_kagit_ticaret_sonuc_panosu.html"
    png_yolu = GORSEL_DIZINI / "05_kagit_ticaret_sonuc_panosu.png"
    fig.write_html(html_yolu)
    fig.write_image(png_yolu, width=1100, height=560, scale=2)
    print(f"   -> {html_yolu.name} + {png_yolu.name} yazıldı.")


def gorsel_6_getiri_dagilimi(stats):
    trades = stats["_trades"].copy()
    if trades.empty:
        fig = go.Figure()
        fig.add_annotation(text="Backtest'te hiç işlem oluşmadı", showarrow=False,
                            font=dict(size=16, color=RENK["muted"]))
        _kaydet(fig, "06_getiri_dagilimi")
        return

    getiriler_yuzde = trades["ReturnPct"] * 100
    renkler = np.where(getiriler_yuzde >= 0, RENK["yesil_good"], RENK["kirmizi_kritik"])

    fig = go.Figure()
    fig.add_trace(go.Histogram(x=getiriler_yuzde, marker_color=RENK["mavi"], opacity=0.85,
                                nbinsx=30, name="Trade getirisi (%)"))
    fig.add_vline(x=0, line_width=1.5, line_dash="dash", line_color=RENK["baseline"])
    fig.add_vline(x=getiriler_yuzde.mean(), line_width=1.5, line_color=RENK["turuncu"],
                  annotation_text=f"Ortalama: %{getiriler_yuzde.mean():.2f}", annotation_position="top")
    fig.update_layout(title="Trade Bazlı Getiri Dağılımı (%)",
                       xaxis_title="İşlem Getirisi (%)", yaxis_title="İşlem Sayısı")
    _kaydet(fig, "06_getiri_dagilimi")


# ----------------------------------------------------------------------------
# Ana akış
# ----------------------------------------------------------------------------

def ana():
    fiyat_df = fiyat_verisi_cek(SEMBOL, FIYAT_PERIYODU)
    haber_gunluk = haber_verisi_cek(SEMBOL, HABER_GUN_SAYISI, HABER_PENCERE_GUN)
    olay_df = tetikleyicileri_hesapla(fiyat_df, haber_gunluk, SOK_ESIGI)

    stats, backtest_ozeti = backtest_calistir(fiyat_df)

    alpaca_sonuc = alpaca_demo_emri_gonder(SEMBOL)

    print("\n[6/6] Görseller üretiliyor (plotly, .html + .png)...")
    gorsel_1_tetikleyici_olaylar(olay_df)
    gorsel_2_equity_curve(stats)
    gorsel_3_olay_turune_gore_performans(stats)
    gorsel_4_zamanlama_dagilimi(stats)
    gorsel_5_kagit_ticaret_panosu(alpaca_sonuc)
    gorsel_6_getiri_dagilimi(stats)

    print("\n" + "=" * 78)
    print("TAMAMLANDI. Özet:")
    print(f"  - Backtest işlem sayısı : {backtest_ozeti['islem_sayisi']}")
    print(f"  - Backtest getirisi     : %{backtest_ozeti['getiri_yuzde']:.2f}")
    print(f"  - Alpaca bağlantısı     : {'BAŞARILI' if alpaca_sonuc['baglanti_basarili'] else 'BAŞARISIZ'}")
    if alpaca_sonuc.get("emir"):
        print(f"  - Alpaca emir ID        : {alpaca_sonuc['emir']['id']} ({alpaca_sonuc['emir']['durum']})")
    print(f"  - Görseller             : {GORSEL_DIZINI}")
    print(f"  - Ara veriler           : {VERI_DIZINI}")
    print("=" * 78)


if __name__ == "__main__":
    try:
        ana()
    except Exception as e:
        print(f"\n!!! SCRIPT HATA İLE DURDU: {type(e).__name__}: {e}", file=sys.stderr)
        raise
