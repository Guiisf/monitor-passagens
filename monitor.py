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
    "dentro_teto",
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
    """
    Uma chamada à SerpApi.

    O contador é um TETO, não o valor exato cobrado: a SerpApi serve buscas
    idênticas repetidas em janela curta do cache dela, sem debitar da cota.
    Com uma coleta por dia isso não acontece (as buscas ficam 24h distantes),
    mas várias execuções seguidas — depuração, testes manuais — inflam o
    contador. Errar para cima é o lado seguro num guarda de cota; para
    reconciliar, veja o valor real em https://serpapi.com/account.
    """
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


def _sprint() -> dict:
    """
    Config do sprint de ida, ou {} quando o modo está desligado.

    Desliga sozinho depois do prazo da reserva: sem isso o cron de hora em hora
    continuaria queimando cota buscando uma ida que já foi decidida.
    """
    s = CFG.get("sprint") or {}
    if not s.get("ativo"):
        return {}
    prazo = s.get("prazo_utc")
    if prazo:
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(prazo.replace("Z", "+00:00")):
                return {}
        except ValueError:
            pass
    return s


def _params_base(origem: str, destino: str) -> dict:
    p = {
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
    sp = _sprint()
    if sp:
        # Só ida: 1 busca por rota em vez de 2, porque não existe a 2ª chamada
        # com departure_token para fechar a volta. type=2 é one-way na SerpApi.
        p["type"] = 2
        p["outbound_date"] = sp["data_ida"]
        p.pop("return_date", None)
    return p


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


# Fuso local em setembro/2026: Brasil não tem mais horário de verão (UTC-3);
# Flórida está em EDT (UTC-4). Só os extremos do trecho importam para o tempo
# decorrido — o aeroporto de conexão se cancela na conta.
TZ_AEROPORTO = {"GRU": -3, "VCP": -3, "CGH": -3, "MCO": -4, "TPA": -4}


def _dur_leg(segs: list[dict]) -> int:
    """
    Tempo REAL de porta a porta do trecho, em minutos — conexão inclusa.

    Somar a duração dos voos ignora a espera e engana feio: um GRU->ORD->MCO
    aparecia como 13h22 de voo quando na verdade leva 26h34, com 13h12 parado
    em Chicago. Cai na soma dos voos se os horários não vierem utilizáveis.
    """
    if not segs:
        return 0
    voos = sum(int(s.get("duration") or 0) for s in segs)
    ts_dep = segs[0].get("departure_airport", {}).get("time", "")
    ts_arr = segs[-1].get("arrival_airport", {}).get("time", "")
    id_dep = segs[0].get("departure_airport", {}).get("id", "")
    id_arr = segs[-1].get("arrival_airport", {}).get("id", "")
    try:
        dep = datetime.strptime(ts_dep, "%Y-%m-%d %H:%M")
        arr = datetime.strptime(ts_arr, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return voos
    # local -> UTC: utc = local - offset
    delta = (arr - dep).total_seconds() / 60
    delta += (TZ_AEROPORTO.get(id_dep, -3) - TZ_AEROPORTO.get(id_arr, -3)) * 60
    return int(delta) if delta > 0 else voos


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
    # porta a porta, conexão inclusa — é o que o passageiro sente
    duracao = _dur_leg(segs)
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
    so_ida = bool(_sprint())
    trechos = 1 if so_ida else 2
    # o fallback do config é declaradamente por trecho
    fallback = malas * bag.get("custo_por_mala_trecho_brl", 0) * trechos
    # Numa busca só de ida não há ambiguidade: o valor é daquele trecho único.
    # Na ida-e-volta, depende de 'valor_api_cobre' (suposição ainda em aberto).
    trechos_api = 1 if so_ida else (2 if bag.get("valor_api_cobre") == "trecho" else 1)

    # Cache separado por tipo de busca: o valor de só-ida não é intercambiável
    # com o de ida-e-volta. De quebra, comparar os dois resolve a dúvida de
    # 'valor_api_cobre' — se o só-ida repetir 885, o valor é por trecho.
    chave = f"{'-'.join(sorted(cias_set))}|{origem}-{destino}{'|IDA' if so_ida else ''}"
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

    if _sprint():
        # Só ida: a 1ª resposta já traz o itinerário completo e o preço final.
        # Não há 2ª chamada, então a rota inteira custa 1 busca.
        for o in idas:
            segs = o.get("flights") or []
            if not segs or len(segs) - 1 > max_con:
                continue
            validos.append((o, segs, []))
        print(f"[API] {origem}->{destino}: {len(validos)} idas válidas (só ida, 1 busca)")
        return _montar_opcoes(validos, origem, destino, base)

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

    return _montar_opcoes(validos, origem, destino, base)


def _montar_opcoes(validos: list, origem: str, destino: str, base: dict) -> list[dict]:
    """Dedup, filtros de duração e preço, bagagem e ranking — comum a ida e a RT."""
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

    # Corta itinerários longos demais. O teto já existia no config, mas só pintava
    # um selo no painel — em 20/08 uma ida de 13h22 via ORD (desvio ao norte de
    # Orlando) virou rank 1 e disparou e-mail. Agora sai do ranking, do CSV e dos
    # alertas. Se não sobrar nada, o filtro é ignorado: melhor dado ruim que rota
    # sem leitura nenhuma.
    teto_min = CFG.get("max_duracao_voo_h", 0) * 60
    if teto_min:
        dentro = [t for t in unicos
                  if _dur_leg(t[1]) <= teto_min and (_dur_leg(t[2]) or 0) <= teto_min]
        if not dentro:
            # Rota sem nenhuma opção viável no dia: registra assim mesmo, para o
            # histórico não ficar com buraco, mas todas saem marcadas fora do teto
            # e os alertas as ignoram — o painel mostra, o e-mail não incomoda.
            print(f"[DURACAO] {origem}->{destino}: nenhuma opção dentro de "
                  f"{teto_min // 60}h; registrando fora do teto", file=sys.stderr)
        else:
            if len(dentro) < len(unicos):
                print(f"[DURACAO] {origem}->{destino}: {len(unicos) - len(dentro)} "
                      f"opções acima de {teto_min // 60}h descartadas")
            unicos = dentro

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
        # O preço já vem totalizado para os `adults` da busca — CONFERIDO em
        # 18/08/2026 contra o Google Voos: o mesmo itinerário Avianca (AV86+AV118,
        # 07:35->21:20) aparecia por R$ 3.113 para 1 adulto, e a API devolveu
        # R$ 6.225 com adults=2. Não multiplicar por adultos.
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
            # trecho ausente (só ida) conta como 0, não como string vazia
            "dentro_teto": (not teto_min
                            or ((ri["duracao"] or 0) <= teto_min
                                and (rv["duracao"] or 0) <= teto_min)),
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
    sp = _sprint()
    if sp:
        # No sprint todas as rotas rodam em toda execução: a janela é curta e o
        # que interessa é cobrir as 4 combinações a cada hora.
        return [{"origem": o, "destino": d, "idas_exploradas": 1}
                for o, d in sp["rotas"]]

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
                    "dentro_teto",
                )},
            })
        time.sleep(1)
    return linhas


def _migrar_csv(header_atual: list[str]) -> None:
    """
    Reescreve o CSV quando CSV_COLS muda, preservando o histórico.

    Sem isso, acrescentar uma coluna quebra o arquivo em silêncio: o cabeçalho
    velho continua com N campos e as linhas novas passam a ter N+1, e a leitura
    morre com "Expected N fields, saw N+1". Foi o que derrubou a coleta quando
    'dentro_teto' entrou.
    """
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        antigas = list(csv.DictReader(f))
    tmp = CSV_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in antigas:
            w.writerow({c: r.get(c, "") for c in CSV_COLS})
    tmp.replace(CSV_PATH)
    novas = [c for c in CSV_COLS if c not in header_atual]
    print(f"[CSV] esquema migrado: {len(antigas)} linhas, colunas novas {novas}")


def gravar(linhas: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    novo = not CSV_PATH.exists()
    if not novo:
        with CSV_PATH.open(newline="", encoding="utf-8") as f:
            header_atual = next(csv.reader(f), [])
        if header_atual and header_atual != CSV_COLS:
            _migrar_csv(header_atual)
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        if novo:
            w.writeheader()
        w.writerows(linhas)


# ------------------------------------------------------------------ alerta ---
def solo_ida(origem: str, destino: str) -> tuple[float, list[str]]:
    """Custo de solo da IDA: Uber até VCP e traslado Tampa->Orlando."""
    cfg = CFG.get("solo_ida") or {}
    total, itens = 0.0, []
    if origem == "VCP":
        v = cfg.get("VCP_origem_brl", 0)
        if v:
            total += v
            itens.append(f"Uber até VCP {brl(v)}")
    if destino == "TPA":
        v = cfg.get("TPA_destino_brl", 0)
        if v:
            total += v
            itens.append(f"traslado Tampa→Orlando {brl(v)}")
    return total, itens


def custo_na_porta(ln: dict) -> float:
    """Tarifa + bagagem + solo. É a única base honesta para comparar rotas."""
    return ln["custo_total_brl"] + solo_ida(ln["origem"], ln["destino"])[0]


def baseline_na_porta() -> tuple[float, dict]:
    """
    Custo na porta da reserva Copa que está travada, para servir de régua.

    A tarifa é conhecida (R$ 3.290,46, 2 passageiros). Bagagem sai do cache real
    da Copa quando existir; senão, do fallback do config. O traslado de Tampa
    entra porque a volta já comprada parte de MCO.
    """
    sp = _sprint()
    if not sp:
        return 0.0, {}
    b = sp["baseline"]
    bag_cfg = CFG.get("bagagem") or {}
    malas = bag_cfg.get("malas_total", 0)
    reg = _cache_bagagem().get(f"CM|{b['origem']}-{b['destino']}|IDA")
    if reg and reg.get("mala") is not None:
        bagagem, fonte_bag = reg["mala"] * malas, "api"
    else:
        bagagem = malas * bag_cfg.get("custo_por_mala_trecho_brl", 0)
        fonte_bag = "estimativa"
    solo, itens = solo_ida(b["origem"], b["destino"])
    return b["tarifa_brl"] + bagagem + solo, {
        "tarifa": b["tarifa_brl"], "bagagem": bagagem, "bagagem_fonte": fonte_bag,
        "solo": solo, "itens_solo": itens,
    }


def checar_sprint(linhas: list[dict]) -> list[str]:
    """
    Compara a coleta contra a reserva da Copa e avisa só quando vale trocar.

    Exige margem mínima: mudar de voo por R$ 50 não paga o trabalho nem o risco
    de mexer numa reserva que já está de pé.
    """
    sp = _sprint()
    if not sp:
        return []
    ref, det = baseline_na_porta()
    b = sp["baseline"]
    margem = sp.get("margem_alerta_brl", 0)

    print(f"[SPRINT] régua Copa: tarifa {brl(det['tarifa'])} + bagagem "
          f"{brl(det['bagagem'])} ({det['bagagem_fonte']}) + solo {brl(det['solo'])} "
          f"= {brl(ref)} na porta")

    viaveis = [l for l in linhas if l.get("dentro_teto", True)]
    if not viaveis:
        print("[SPRINT] nenhuma opção dentro do teto de duração nesta rodada")
        return []

    # O voo da própria reserva aparece nos resultados. Tratá-lo como
    # "alternativa" é enganoso: não é troca, é a mesma passagem com outro preço
    # — quase sempre outra família tarifária, não uma oferta melhor.
    alvo = str(sp["baseline"].get("voos", ""))
    eh_baseline = [l for l in viaveis if str(l.get("voos_ida", "")) == alvo]
    outros = [l for l in viaveis if str(l.get("voos_ida", "")) != alvo]

    msgs = []
    if eh_baseline:
        mesmo = min(eh_baseline, key=custo_na_porta)
        dif = sp["baseline"]["tarifa_brl"] - mesmo["preco_total_brl"]
        print(f"[SPRINT] a própria reserva aparece a {brl(mesmo['preco_total_brl'])} "
              f"de tarifa ({dif:+,.0f} vs os {brl(sp['baseline']['tarifa_brl'])} travados)")
        if dif >= margem:
            msgs += [
                f"🔁 SEU PRÓPRIO VOO ESTÁ {brl(dif)} MAIS BARATO AGORA",
                f"{alvo} — mesma partida {sp['baseline']['partida']}, mesma chegada "
                f"{sp['baseline']['chegada']} — aparece por {brl(mesmo['preco_total_brl'])} "
                f"de tarifa, contra {brl(sp['baseline']['tarifa_brl'])} da sua reserva.",
                "⚠️ Confira a FAMÍLIA TARIFÁRIA antes de refazer: a sua é Economy "
                "Classic (L). Tarifa mais barata no mesmo voo costuma ser Basic, "
                "com menos bagagem, sem escolha de assento e sem alteração.",
                "",
            ]

    if not outros:
        return msgs

    melhor = min(outros, key=custo_na_porta)
    porta = custo_na_porta(melhor)
    ganho = ref - porta
    solo, itens = solo_ida(melhor["origem"], melhor["destino"])
    print(f"[SPRINT] melhor alternativa: {melhor['origem']}→{melhor['destino']} "
          f"{melhor['cia']} {brl(porta)} na porta ({ganho:+,.0f} vs Copa)")

    if ganho < margem:
        return msgs

    extra = f" (inclui {', '.join(itens)})" if itens else ""
    return msgs + [
        f"💰 ACHOU ALGO MELHOR QUE A COPA — economia de {brl(ganho)}",
        "",
        f"Alternativa: {melhor['origem']}→{melhor['destino']} · {melhor['cia']} · "
        f"{melhor['voos_ida']}",
        f"  parte {melhor['partida_ida']}, chega {melhor['chegada_ida']} · "
        f"{melhor['duracao_ida'] // 60}h{melhor['duracao_ida'] % 60:02d} · "
        f"{melhor['conexoes_ida']} conexão(ões) via {melhor['via_ida'] or '—'}",
        f"  tarifa {brl(melhor['preco_total_brl'])} + bagagem "
        f"{brl(melhor['bagagem_brl'])} ({melhor['bagagem_fonte']}) + solo {brl(solo)}"
        f" = {brl(porta)} na porta{extra}",
        "",
        f"Reserva atual: Copa {b['voos']} · parte {b['partida']}, chega {b['chegada']}"
        f" · {brl(ref)} na porta",
        "",
        f"⏰ A Copa vence em {sp['prazo_utc']} — se for trocar, decida antes disso.",
    ]


def _queda_relevante(minimo: float, preco: float) -> bool:
    """
    Se a queda abaixo do mínimo histórico merece e-mail.

    Sem piso, qualquer centavo virava "NOVO MÍNIMO": R$ 80 numa rota de
    R$ 13.891 (0,6%) disparou alerta em 20/08. Ruído assim ensina a ignorar a
    caixa de entrada justamente quando vier o alerta que importa.
    """
    if minimo <= 0:
        return True
    piso = max(CFG.get("alerta_min_queda_brl", 0),
               minimo * CFG.get("alerta_min_queda_pct", 0) / 100)
    return (minimo - preco) >= piso


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
        if not ln.get("dentro_teto", True):
            print(f"[ALERTA] {ln['origem']}→{ln['destino']}: melhor opção está fora do "
                  f"teto de duração; sem e-mail nesta rodada")
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

        if preco < minimo and _queda_relevante(minimo, preco):
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
                  if l.get("dentro_teto", True)
                  and (casa_favorito(l.get("voos_ida"), alvo)
                       or casa_favorito(l.get("voos_volta"), alvo))]
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
                if preco < minimo and _queda_relevante(minimo, preco):
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
    # só ida gasta 1 busca por rota; ida-e-volta gasta 1 + as idas exploradas
    estimativa = (len(rotas) if _sprint()
                  else sum(1 + r.get("idas_exploradas", 1) for r in rotas))
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

    # No sprint o alerta é outro: não interessa "caiu 8%", e sim "existe algo
    # melhor que a reserva que vence amanhã".
    msgs = checar_sprint(linhas) if _sprint() else checar_alertas(linhas)
    if msgs:
        for m in msgs:
            print(m)
        enviar_email(msgs)

    from dashboard import gerar_dashboard
    gerar_dashboard(CSV_PATH, BASE_DIR / "docs" / "index.html", CFG)
    print("[OK] dashboard atualizado.")


if __name__ == "__main__":
    main()
