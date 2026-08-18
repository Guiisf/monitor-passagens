# -*- coding: utf-8 -*-
"""
Monitor de passagens aéreas SP -> Orlando/Tampa (SerpApi / Google Flights).

Roda 1x por dia, consulta as rotas configuradas em ida-e-volta fechado nas datas
fixas, grava histórico em data/precos.csv, dispara alerta por e-mail em queda
relevante e regenera docs/index.html.

Motor SerpApi (engine=google_flights):
- Ida e volta exige DUAS chamadas: a 1ª devolve as opções de ida com um
  departure_token; a 2ª (com esse token) devolve a volta já com o preço do
  pacote fechado. Custo: 2 buscas por rota.
- O custo de bagagem despachada só existe na resposta de booking options
  (3ª chamada, via booking_token). Como taxa de bagagem não muda de hora em
  hora, o valor é cacheado por (cia, rota) em data/bagagem_cache.json.

Variáveis de ambiente (GitHub Secrets):
  SERP_KEY, MAIL_USER, MAIL_PASS, MAIL_TO
"""

import csv
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CSV_PATH = DATA_DIR / "precos.csv"
CACHE_BAGAGEM = DATA_DIR / "bagagem_cache.json"
USO_PATH = DATA_DIR / "uso_serpapi.json"
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

BRT = timezone(timedelta(hours=-3))
SERP_URL = "https://serpapi.com/search.json"

CSV_COLS = [
    "ts_utc", "ts_brt", "hora_brt", "dia_semana_brt",
    "origem", "destino", "data_ida", "data_volta", "tipo", "rank",
    "preco_total_brl", "bagagem_brl", "bagagem_fonte", "custo_total_brl",
    "cia", "voos_ida", "voos_volta", "via_ida", "via_volta",
    "partida_ida", "chegada_ida", "partida_volta", "chegada_volta",
    "conexoes_ida", "conexoes_volta", "duracao_ida", "duracao_volta",
]

_creditos = 0

# Teto de consultas novas de bagagem por execução: no 1º dia o cache está vazio e
# cada combinação (cia, rota) gastaria 1 busca. O que não couber usa a estimativa
# e é consultado nas próximas rodadas, conforme o cache se preenche.
MAX_BAGAGEM_NOVAS = 6
_bagagem_novas = 0


def brl(v) -> str:
    return "R$ " + f"{v:,.0f}".replace(",", ".")


# -------------------------------------------------------------- serpapi io ---
def _serp(params: dict) -> dict:
    """Uma chamada à SerpApi. Cada chamada consome 1 busca da cota mensal."""
    global _creditos
    p = dict(params, engine="google_flights", api_key=os.environ["SERP_KEY"])
    r = requests.get(SERP_URL, params=p, timeout=90)
    if r.status_code == 429:
        time.sleep(5)
        r = requests.get(SERP_URL, params=p, timeout=90)
    _creditos += 1
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"SerpApi: {j['error']}")
    return j


def _params_base(origem: str, destino: str) -> dict:
    return {
        "departure_id": origem,
        "arrival_id": destino,
        "outbound_date": CFG["data_ida"],
        "return_date": CFG["data_volta"],
        "type": 1,                       # 1 = ida e volta
        "travel_class": 1,               # econômica
        "adults": CFG["adultos"],
        "currency": CFG["moeda"],
        "stops": 2 if CFG["max_conexoes"] == 1 else 0,   # 2 = até 1 conexão
        "gl": "br",
        "hl": "en",   # textos em inglês: o parser de bagagem casa "checked bag"
        "deep_search": "true",
    }


# ------------------------------------------------------------- parsing ---
def _hhmm(ts: str) -> str:
    """'2026-09-23 21:25' -> '21:25'."""
    return ts[11:16] if ts and len(ts) >= 16 else ""


def _dia(ts: str) -> str:
    return ts[:10] if ts and len(ts) >= 10 else ""


def _cod_voo(seg: dict) -> str:
    """'AA 930' -> 'AA930'."""
    return str(seg.get("flight_number", "")).replace(" ", "")


def regex_favorito(alvo: str) -> str:
    """Casa o alvo no início de um número de voo, não no meio (AD87 ≠ AD8700)."""
    return rf"(?:^|,){re.escape(alvo)}(?:,|$)"


def casa_favorito(voos, alvo: str) -> bool:
    return any(v == alvo for v in str(voos or "").split(","))


def _separar_legs(segs: list[dict], origem: str, destino: str) -> tuple[list, list]:
    """
    Divide os segmentos em ida e volta.

    A 2ª chamada pode devolver só os segmentos da volta ou o itinerário inteiro,
    dependendo da rota; separar pelo ponto em que o voo parte do destino cobre
    os dois casos sem depender do formato.
    """
    corte = None
    for i, s in enumerate(segs):
        if s.get("departure_airport", {}).get("id") == destino:
            corte = i
            break
    if corte is None:
        return segs, []
    if corte == 0 and segs and segs[0].get("arrival_airport", {}).get("id") != origem:
        # começa no destino mas não é o itinerário todo: são só os segmentos da volta
        return [], segs
    return segs[:corte], segs[corte:]


def _resumo_leg(segs: list[dict]) -> dict:
    if not segs:
        return {"voos": "", "via": "", "conexoes": "", "partida": "", "chegada": "",
                "duracao": "", "cias": []}
    partida = segs[0].get("departure_airport", {}).get("time", "")
    chegada_ts = segs[-1].get("arrival_airport", {}).get("time", "")
    chegada = _hhmm(chegada_ts)
    if _dia(chegada_ts) > _dia(partida):
        chegada += "+1"
    vias = [s.get("arrival_airport", {}).get("id", "") for s in segs[:-1]]
    duracao = sum(int(s.get("duration") or 0) for s in segs)
    return {
        "voos": ",".join(_cod_voo(s) for s in segs),
        "via": ",".join(v for v in vias if v),
        "conexoes": len(segs) - 1,
        "partida": _hhmm(partida),
        "chegada": chegada,
        "duracao": duracao,
        "cias": [_cod_voo(s)[:2] for s in segs],
    }


# ------------------------------------------------------------- bagagem ---
def _cache_bagagem() -> dict:
    if CACHE_BAGAGEM.exists():
        return json.loads(CACHE_BAGAGEM.read_text(encoding="utf-8"))
    return {}


def _grava_cache_bagagem(cache: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_BAGAGEM.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _valores(txt: str) -> list[float]:
    """Extrai valores monetários de 'R$ 1,234' / '99-187' / 'R$ 250,00'."""
    out = []
    for bruto in re.findall(r"\d[\d.,]*", txt):
        n = bruto.rstrip(".,")
        if "," in n and "." in n:
            n = n.replace(".", "").replace(",", ".") if n.rfind(",") > n.rfind(".") \
                else n.replace(",", "")
        elif "," in n:
            # vírgula com 2 casas = decimal; com 3 = milhar
            n = n.replace(",", ".") if len(n.split(",")[-1]) == 2 else n.replace(",", "")
        elif "." in n:
            n = n if len(n.split(".")[-1]) == 2 else n.replace(".", "")
        try:
            out.append(float(n))
        except ValueError:
            continue
    return out


def _preco_mala(linhas) -> tuple[float | None, bool]:
    """
    Custo da 1ª mala despachada, por trecho e por pessoa. Devolve (valor, é_faixa).

    Quando a API devolve faixa ("265-885", tarifas diferentes da mesma cia), usa o
    piso: o teto inflava o custo a ponto de inverter a comparação entre rotas.
    """
    if isinstance(linhas, dict):
        linhas = [t for v in linhas.values() for t in (v if isinstance(v, list) else [v])]
    for linha in linhas or []:
        txt = str(linha)
        if not re.search(r"checked bag|bagagem despachada", txt, re.I):
            continue
        # "1st checked bag" e "2 free checked bags" trazem dígitos que não são preço
        limpo = re.sub(r"\b\d+\s*(st|nd|rd|th|ª|a)\b", " ", txt, flags=re.I)
        limpo = re.sub(r"\b\d+\s+(free|gr[áa]tis)\b", " free ", limpo, flags=re.I)
        vals = _valores(limpo)
        if vals:
            return min(vals), len(set(vals)) > 1
        if re.search(r"free|gr[áa]tis|inclu", limpo, re.I):
            return 0.0, False
    return None, False


def custo_bagagem(booking_token: str, cias: list[str], origem: str, destino: str,
                  params_busca: dict) -> tuple[float, str]:
    """
    Custo de bagagem despachada da viagem inteira (2 malas x 2 trechos x 2 adultos
    conforme config), em R$. Devolve (valor, fonte).

    Cias isentas (Azul/Safira) saem zeradas sem gastar chamada. Nas demais,
    consulta booking options 1x por (cia, rota) e cacheia por 'cache_dias'.
    """
    bag = CFG.get("bagagem") or {}
    isentas = set(bag.get("cias_isentas", []))
    cias_set = {c for c in cias if c}
    if cias_set and cias_set.issubset(isentas):
        return 0.0, "isenta"

    malas = bag.get("malas_total", 0)
    # o fallback do config é declaradamente por trecho, então sempre dobra
    fallback = malas * bag.get("custo_por_mala_trecho_brl", 0) * 2
    # já o valor da API cobre a viagem toda ou cada trecho — ver 'valor_api_cobre'
    trechos_api = 2 if bag.get("valor_api_cobre") == "trecho" else 1

    chave = f"{'-'.join(sorted(cias_set))}|{origem}-{destino}"
    cache = _cache_bagagem()
    reg = cache.get(chave)
    if reg and (datetime.now(timezone.utc) - datetime.fromisoformat(reg["ts"])).days < bag.get("cache_dias", 14):
        if reg["mala"] is None:
            return fallback, "estimativa"
        return (reg["mala"] * malas * trechos_api,
                "api-cache-faixa" if reg.get("faixa") else "api-cache")

    global _bagagem_novas
    if _bagagem_novas >= MAX_BAGAGEM_NOVAS:
        print(f"[BAG] teto de {MAX_BAGAGEM_NOVAS} consultas novas atingido; {chave} fica na estimativa")
        return fallback, "estimativa"

    if not booking_token:
        return fallback, "estimativa"

    try:
        _bagagem_novas += 1
        j = _serp(dict(params_busca, booking_token=booking_token))
        precos = j.get("baggage_prices")
        if precos is None:
            for o in j.get("booking_options") or []:
                alvo = o.get("together") or o.get("departing") or o
                if isinstance(alvo, dict) and alvo.get("baggage_prices"):
                    precos = alvo["baggage_prices"]
                    break
        # DIAGNÓSTICO: os valores vindos da API (AV R$ 885, AA R$ 729 por mala)
        # parecem altos demais, e a fórmula multiplica por 2 malas x 2 trechos.
        # Logar a string crua é o único jeito de saber se o número já é do
        # trajeto todo ou já cobre o casal, antes de mexer na multiplicação.
        print(f"[BAG-RAW] {chave}: {json.dumps(precos, ensure_ascii=False)[:400]}")
        mala, faixa = _preco_mala(precos)
    except Exception as e:  # noqa: BLE001 — bagagem não pode derrubar a coleta
        # falha transitória não vai pro cache: tenta de novo na próxima rodada
        print(f"[BAG] falha em {chave}: {e}", file=sys.stderr)
        return fallback, "estimativa"

    cache[chave] = {"ts": datetime.now(timezone.utc).isoformat(), "mala": mala, "faixa": faixa}
    _grava_cache_bagagem(cache)

    if mala is None:
        return fallback, "estimativa"
    return mala * malas * trechos_api, "api-faixa" if faixa else "api"


# -------------------------------------------------------------- busca ---
def buscar(origem: str, destino: str, n_idas: int = 1) -> list[dict]:
    """
    Melhores pacotes ida-e-volta da rota. Consome 1 + n_idas buscas, mais a
    bagagem não cacheada.

    Cada ida explorada rende um conjunto próprio de voltas: olhar só a ida mais
    barata escondia combinações melhores (uma ida R$ 200 mais cara pode abrir
    voltas R$ 2.000 mais baratas).
    """
    base = _params_base(origem, destino)
    ida_j = _serp(base)
    idas = (ida_j.get("best_flights") or []) + (ida_j.get("other_flights") or [])
    print(f"[API] {origem}->{destino}: {len(idas)} opções de ida")
    if not idas:
        return []

    idas.sort(key=lambda o: float(o.get("price") or 10**9))
    max_con = CFG["max_conexoes"]
    validos = []

    for i, ida in enumerate(idas[:n_idas], start=1):
        token = ida.get("departure_token")
        if not token:
            print(f"[AVISO] {origem}->{destino}: ida {i} sem departure_token", file=sys.stderr)
            continue
        volta_j = _serp(dict(base, departure_token=token))
        pacotes = (volta_j.get("best_flights") or []) + (volta_j.get("other_flights") or [])
        print(f"[API] {origem}->{destino}: ida {i}/{min(n_idas, len(idas))} -> {len(pacotes)} pacotes")
        for p in pacotes:
            segs = p.get("flights") or []
            ida_segs, volta_segs = _separar_legs(segs, origem, destino)
            if not ida_segs:
                ida_segs = ida.get("flights") or []
            if len(ida_segs) - 1 > max_con or len(volta_segs) - 1 > max_con:
                continue
            validos.append((p, ida_segs, volta_segs))

    if not validos:
        return []

    validos.sort(key=lambda t: float(t[0].get("price") or 10**9))

    # idas diferentes podem devolver o mesmo pacote: fica o 1º (mais barato)
    vistos, unicos = set(), []
    for p, ida_segs, volta_segs in validos:
        chave = (",".join(_cod_voo(s) for s in ida_segs),
                 ",".join(_cod_voo(s) for s in volta_segs))
        if chave in vistos:
            continue
        vistos.add(chave)
        unicos.append((p, ida_segs, volta_segs))

    # corta tarifas premium que vazam da busca por econômica e nunca serão a resposta
    fator = CFG.get("max_fator_preco", 0)
    if fator:
        piso = float(unicos[0][0].get("price") or 0)
        antes = len(unicos)
        unicos = [t for t in unicos if float(t[0].get("price") or 0) <= piso * fator]
        if antes > len(unicos):
            print(f"[FILTRO] {origem}->{destino}: {antes - len(unicos)} opções acima de "
                  f"{fator}x R$ {piso:,.0f} descartadas")

    n_final = CFG.get("opcoes_por_consulta", 5)
    # Avalia um pool maior que o top-N final: uma tarifa mais cara pode vencer no
    # total se a bagagem for mais barata. Como a bagagem é cacheada por cia+rota,
    # o pool extra quase nunca custa chamada nova.
    candidatos = []
    for p, ida_segs, volta_segs in unicos[: n_final * 2]:
        ri, rv = _resumo_leg(ida_segs), _resumo_leg(volta_segs)
        cias = sorted(set(ri["cias"] + rv["cias"]))
        # SerpApi devolve o preço já totalizado para os `adults` da busca.
        preco = float(p.get("price") or 0)
        bagagem, fonte = custo_bagagem(p.get("booking_token", ""), cias, origem, destino, base)
        candidatos.append({
            "preco": round(preco, 2),
            "bagagem": round(bagagem, 2),
            "bagagem_fonte": fonte,
            "custo_total": round(preco + bagagem, 2),
            "cia": ",".join(cias),
            "voos_ida": ri["voos"], "voos_volta": rv["voos"],
            "via_ida": ri["via"], "via_volta": rv["via"],
            "partida_ida": ri["partida"], "chegada_ida": ri["chegada"],
            "partida_volta": rv["partida"], "chegada_volta": rv["chegada"],
            "conexoes_ida": ri["conexoes"], "conexoes_volta": rv["conexoes"],
            "duracao_ida": ri["duracao"], "duracao_volta": rv["duracao"],
        })

    # Ranqueia pelo CUSTO TOTAL, não pela tarifa: é o critério de decisão, e a
    # tarifa sozinha já inverteu a ordem na prática (Avianca com tarifa menor
    # perdia da American depois de somar bagagem).
    candidatos.sort(key=lambda o: o["custo_total"])
    opcoes = []
    for rank, o in enumerate(candidatos[:n_final], start=1):
        opcoes.append({"rank": rank, **o})
    return opcoes


# ------------------------------------------------------------------ coleta ---
def rotas_do_dia(agora_brt: datetime) -> list[dict]:
    """Rotas marcadas para hoje. 'dias' aceita 'todos' ou lista de weekday() (0=seg)."""
    hoje = agora_brt.weekday()
    ativas = []
    for r in CFG["rotas"]:
        dias = r.get("dias", "todos")
        if dias == "todos" or hoje in dias:
            ativas.append(r)
    return ativas


def _uso_mes(mes: str) -> int:
    if not USO_PATH.exists():
        return 0
    reg = json.loads(USO_PATH.read_text(encoding="utf-8"))
    return reg.get("buscas", 0) if reg.get("mes") == mes else 0


def _grava_uso(mes: str, buscas: int) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    USO_PATH.write_text(json.dumps({"mes": mes, "buscas": buscas}, indent=2), encoding="utf-8")


def coletar(rotas: list[dict]) -> list[dict]:
    agora_utc = datetime.now(timezone.utc)
    agora_brt = agora_utc.astimezone(BRT)
    linhas = []

    for r in rotas:
        origem, destino = r["origem"], r["destino"]
        try:
            opcoes = buscar(origem, destino, r.get("idas_exploradas", 1))
        except Exception as e:  # noqa: BLE001 — uma rota quebrada não para as outras
            print(f"[ERRO] {origem}->{destino}: {e}", file=sys.stderr)
            continue
        if not opcoes:
            print(f"[VAZIO] {origem}->{destino}")
            continue
        for res in opcoes:
            linhas.append({
                "ts_utc": agora_utc.strftime("%Y-%m-%d %H:%M"),
                "ts_brt": agora_brt.strftime("%Y-%m-%d %H:%M"),
                "hora_brt": agora_brt.hour,
                "dia_semana_brt": agora_brt.strftime("%a"),
                "origem": origem,
                "destino": destino,
                "data_ida": CFG["data_ida"],
                "data_volta": CFG["data_volta"],
                "tipo": "RT",
                "preco_total_brl": res["preco"],
                "bagagem_brl": res["bagagem"],
                "bagagem_fonte": res["bagagem_fonte"],
                "custo_total_brl": res["custo_total"],
                **{k: res[k] for k in (
                    "rank", "cia", "voos_ida", "voos_volta", "via_ida", "via_volta",
                    "partida_ida", "chegada_ida", "partida_volta", "chegada_volta",
                    "conexoes_ida", "conexoes_volta", "duracao_ida", "duracao_volta",
                )},
            })
        time.sleep(1)
    return linhas


def gravar(linhas: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    novo = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        if novo:
            w.writeheader()
        w.writerows(linhas)


# ------------------------------------------------------------------ alerta ---
def checar_alertas(linhas: list[dict]) -> list[str]:
    """Compara a coleta atual com o histórico e devolve mensagens de alerta."""
    import pandas as pd

    if not CSV_PATH.exists():
        return []
    df_all = pd.read_csv(CSV_PATH)
    df = df_all[(df_all["tipo"] == "RT") & (df_all["rank"] == 1)]
    msgs = []
    for ln in linhas:
        if ln["rank"] != 1:
            continue
        chave = (df["origem"] == ln["origem"]) & (df["destino"] == ln["destino"])
        hist = df[chave]["custo_total_brl"]
        if len(hist) < 2:
            continue
        anterior = hist.iloc[-2]
        minimo = hist.iloc[:-1].min()
        preco = ln["custo_total_brl"]
        rota = f"{ln['origem']}→{ln['destino']} {ln['data_ida']} a {ln['data_volta']}"

        queda = (anterior - preco) / anterior * 100 if anterior else 0
        limite = CFG["alerta_limite_brl"].get(ln["destino"], 0)

        if preco < minimo:
            msgs.append(f"🔥 NOVO MÍNIMO: {rota} — {brl(preco)} (mínimo anterior {brl(minimo)})")
        elif queda >= CFG["alerta_queda_pct"]:
            msgs.append(f"📉 Queda de {queda:.0f}%: {rota} — {brl(preco)} (antes {brl(anterior)})")
        elif limite and preco <= limite:
            msgs.append(f"🎯 Abaixo do limite: {rota} — {brl(preco)} (limite {brl(limite)})")

    # --- favoritos: vigia itinerários pelo número do voo (ex.: AA930) ---
    chaves = ["origem", "destino", "data_ida", "data_volta"]
    for fav in CFG.get("favoritos", []):
        alvo = fav["voos"]
        nome = fav.get("nome", alvo)
        limite = fav.get("limite_brl", 0)
        atuais = [l for l in linhas
                  if casa_favorito(l.get("voos_ida"), alvo)
                  or casa_favorito(l.get("voos_volta"), alvo)]
        grupos = {}
        for l in atuais:
            grupos.setdefault(tuple(str(l.get(k, "")) for k in chaves), []).append(l)
        for chave, grupo in grupos.items():
            ln = min(grupo, key=lambda x: x["custo_total_brl"])
            preco = ln["custo_total_brl"]
            padrao = regex_favorito(alvo)
            m = (df_all["voos_ida"].astype(str).str.contains(padrao, regex=True)
                 | df_all["voos_volta"].astype(str).str.contains(padrao, regex=True))
            for k, v in zip(chaves, chave):
                m &= df_all[k].astype(str).fillna("").eq(v)
            hist = df_all[m & (df_all["ts_utc"] < ln["ts_utc"])]["custo_total_brl"]
            desc = f"{nome} ({alvo}, {ln['origem']}→{ln['destino']})"
            if len(hist):
                minimo, anterior = hist.min(), hist.iloc[-1]
                queda = (anterior - preco) / anterior * 100 if anterior else 0
                if preco < minimo:
                    msgs.append(f"⭐ FAVORITO em novo mínimo: {desc} — {brl(preco)} (antes {brl(minimo)})")
                elif queda >= CFG["alerta_queda_pct"]:
                    msgs.append(f"⭐ FAVORITO caiu {queda:.0f}%: {desc} — {brl(preco)}")
            if limite and preco <= limite:
                msgs.append(f"⭐🎯 FAVORITO abaixo do seu limite: {desc} — {brl(preco)} (limite {brl(limite)})")
    return msgs


def enviar_email(msgs: list[str]) -> None:
    user, pw, to = os.environ.get("MAIL_USER"), os.environ.get("MAIL_PASS"), os.environ.get("MAIL_TO")
    if not (user and pw and to):
        print("[AVISO] e-mail não configurado; alertas apenas no log.")
        return
    corpo = "\n".join(msgs) + "\n\nDashboard completo: veja o GitHub Pages do repositório."
    m = MIMEText(corpo, "plain", "utf-8")
    m["Subject"] = f"✈️ Alerta de passagem — {msgs[0][:60]}"
    m["From"], m["To"] = user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [to], m.as_string())
    print(f"[OK] e-mail de alerta enviado para {to}")


# -------------------------------------------------------------------- main ---
def main() -> None:
    agora_brt = datetime.now(timezone.utc).astimezone(BRT)
    mes = agora_brt.strftime("%Y-%m")
    usado = _uso_mes(mes)
    orcamento = CFG.get("orcamento_buscas_mes", 250)

    rotas = rotas_do_dia(agora_brt)
    estimativa = sum(1 + r.get("idas_exploradas", 1) for r in rotas)
    if usado + estimativa > orcamento:
        aviso = (f"⚠️ Coleta pulada: cota SerpApi de {orcamento} buscas/mês quase no fim "
                 f"({usado} usadas, mais {estimativa} nesta rodada). Volta no dia 1º.")
        print(aviso, file=sys.stderr)
        enviar_email([aviso])
        return

    print(f"[COTA] {usado}/{orcamento} buscas usadas em {mes}; {len(rotas)} rotas hoje.")
    linhas = coletar(rotas)
    _grava_uso(mes, usado + _creditos)
    print(f"[COTA] +{_creditos} buscas nesta execução.")

    if not linhas:
        print("[AVISO] nenhuma linha coletada nesta execução.")
        return

    primeira_coleta = not CSV_PATH.exists()
    gravar(linhas)
    print(f"[OK] {len(linhas)} preços gravados.")

    if primeira_coleta:
        melhor = min(linhas, key=lambda x: x["custo_total_brl"])
        enviar_email([
            "🎉 PRIMEIRA COLETA COM DADOS! O monitor agora roda na SerpApi (Google Flights).",
            f"Melhor custo total visto: {melhor['origem']}→{melhor['destino']} — "
            f"{brl(melhor['custo_total_brl'])} (tarifa {brl(melhor['preco_total_brl'])} "
            f"+ bagagem {brl(melhor['bagagem_brl'])})",
            "A partir de agora o robô acumula histórico 1x por dia.",
        ])

    msgs = checar_alertas(linhas)
    if msgs:
        for m in msgs:
            print(m)
        enviar_email(msgs)

    from dashboard import gerar_dashboard
    gerar_dashboard(CSV_PATH, BASE_DIR / "docs" / "index.html", CFG)
    print("[OK] dashboard atualizado.")


if __name__ == "__main__":
    main()
