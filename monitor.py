# -*- coding: utf-8 -*-
"""
Monitor de passagens aéreas SP -> Orlando/Tampa (Travelpayouts / Aviasales Data API).

Toda hora roda a grade completa: origens x destinos x datas, em ida-e-volta,
só ida e só volta (com CGH nos trechos avulsos). Grava em data/precos.csv,
dispara alerta por e-mail em queda relevante e regenera docs/index.html.

Observações do motor Travelpayouts:
- Preço vem do cache real de buscas da Aviasales (tarifa base, por 1 adulto).
  O robô multiplica por 2 (casal). Bagagem despachada NÃO é garantida — conferir no link.
- A API informa companhia + número do 1º voo (ex.: AA930), nº de conexões e duração,
  mas não o aeroporto de conexão.

Variáveis de ambiente (GitHub Secrets):
  TP_TOKEN, MAIL_USER, MAIL_PASS, MAIL_TO
"""

import csv
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "data" / "precos.csv"
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

BRT = timezone(timedelta(hours=-3))
TP_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

# fuso local (horas) por aeroporto — Flórida em setembro = EDT (-4)
TZ_AEROPORTO = {"GRU": -3, "VCP": -3, "CGH": -3, "MCO": -4, "TPA": -4}

CSV_COLS = [
    "ts_utc", "ts_brt", "hora_brt", "dia_semana_brt",
    "origem", "destino", "data_ida", "data_volta", "tipo", "rank",
    "preco_total_brl", "cia", "voos_ida", "voos_volta", "via_ida", "via_volta",
    "partida_ida", "chegada_ida", "partida_volta", "chegada_volta",
    "conexoes_ida", "conexoes_volta", "duracao_ida", "duracao_volta",
]


def brl(v) -> str:
    return "R$ " + f"{v:,.0f}".replace(",", ".")


# ------------------------------------------------------------- travelpayouts ---
def _hora(iso: str) -> str:
    """'2026-09-23T21:25:00-03:00' -> '21:25'."""
    return iso[11:16] if iso and len(iso) >= 16 else ""


def _chegada(iso_partida: str, duracao_min, origem: str, destino: str) -> str:
    """Chegada no horário local do destino, estimada por partida + duração + fuso."""
    if not iso_partida or not duracao_min:
        return ""
    try:
        dep = datetime.fromisoformat(iso_partida)
        delta_fuso = TZ_AEROPORTO.get(destino, -3) - TZ_AEROPORTO.get(origem, -3)
        arr = dep + timedelta(minutes=int(duracao_min) + delta_fuso * 60)
        mais1 = "+1" if arr.date() > dep.date() else ""
        return arr.strftime("%H:%M") + mais1
    except (ValueError, TypeError):
        return ""


def buscar(origem: str, destino: str, ida: str, volta: str | None) -> list[dict]:
    """Top N ofertas do cache da Aviasales que respeitam o limite de conexões."""
    params = {
        "origin": origem,
        "destination": destino,
        "departure_at": ida,
        "one_way": "false" if volta else "true",
        "direct": "false",
        "unique": "false",
        "sorting": "price",
        "currency": CFG["moeda"].lower(),
        "market": "br",
        "limit": 30,
        "token": os.environ["TP_TOKEN"],
    }
    if volta:
        params["return_at"] = volta

    r = requests.get(TP_URL, params=params, timeout=60)
    if r.status_code == 429:
        time.sleep(3)
        r = requests.get(TP_URL, params=params, timeout=60)
    r.raise_for_status()

    brutos = r.json().get("data", [])
    print(f"[API] {origem}->{destino} {ida} volta={volta}: {len(brutos)} ofertas brutas")

    max_con = CFG["max_conexoes"]
    validas = []
    for of in brutos:
        if (of.get("transfers") or 0) > max_con:
            continue
        if volta and (of.get("return_transfers") or 0) > max_con:
            continue
        validas.append(of)

    if not brutos and len(ida) == 10:
        # cache por data exata vazio -> consulta o mês inteiro e filtra
        params_mes = dict(params, departure_at=ida[:7])
        if volta:
            params_mes["return_at"] = volta[:7]
        r2 = requests.get(TP_URL, params=params_mes, timeout=60)
        if r2.ok:
            todos = r2.json().get("data", [])
            brutos = [o for o in todos
                      if str(o.get("departure_at", ""))[:10] == ida
                      and (not volta or str(o.get("return_at", ""))[:10] == volta)]
            print(f"[API-mes] {origem}->{destino}: {len(todos)} no mês, {len(brutos)} nas datas exatas")
            for of in brutos:
                if (of.get("transfers") or 0) > max_con:
                    continue
                if volta and (of.get("return_transfers") or 0) > max_con:
                    continue
                validas.append(of)

    validas.sort(key=lambda o: float(o["price"]))
    opcoes = []
    for rank, of in enumerate(validas[: CFG.get("opcoes_por_consulta", 5)], start=1):
        dur_ida = of.get("duration_to") or of.get("duration") or ""
        dur_volta = of.get("duration_back") or "" if volta else ""
        partida_ida = _hora(of.get("departure_at", ""))
        partida_volta = _hora(of.get("return_at", "")) if volta else ""
        voo = f'{of.get("airline", "")}{of.get("flight_number", "")}'
        opcoes.append({
            "rank": rank,
            # preço do cache é por 1 adulto -> x2 (casal)
            "preco": round(float(of["price"]) * CFG["adultos"], 2),
            "cia": of.get("airline", ""),
            "voos_ida": voo,
            "voos_volta": "",
            "via_ida": "",
            "via_volta": "",
            "partida_ida": partida_ida,
            "chegada_ida": _chegada(of.get("departure_at", ""), dur_ida, origem, destino),
            "partida_volta": partida_volta,
            "chegada_volta": _chegada(of.get("return_at", ""), dur_volta, destino, origem) if volta else "",
            "conexoes_ida": of.get("transfers", 0),
            "conexoes_volta": of.get("return_transfers", 0) if volta else "",
            "duracao_ida": dur_ida,
            "duracao_volta": dur_volta,
        })
    return opcoes


# ------------------------------------------------------------------ coleta ---
def montar_consultas(agora_brt: datetime) -> list[dict]:
    """Grade completa em toda execução (a API da Travelpayouts é gratuita)."""
    consultas = []

    def add(origem, destino, ida, volta, tipo):
        consultas.append(dict(origem=origem, destino=destino, ida=ida, volta=volta, tipo=tipo))

    for d in CFG["destinos"]:
        for ida, volta in CFG["datas"]:
            for o in CFG["origens"]:
                add(o, d, ida, volta, "RT")
            for o in CFG.get("origens_one_way", CFG["origens"]):
                add(o, d, ida, None, "IDA")
                add(d, o, volta, None, "VOLTA")

    vistos, unicas = set(), []
    for c in consultas:
        k = (c["origem"], c["destino"], c["ida"], c["volta"], c["tipo"])
        if k not in vistos:
            vistos.add(k)
            unicas.append(c)
    return unicas


def coletar() -> list[dict]:
    agora_utc = datetime.now(timezone.utc)
    agora_brt = agora_utc.astimezone(BRT)
    linhas = []

    for c in montar_consultas(agora_brt):
        try:
            opcoes = buscar(c["origem"], c["destino"], c["ida"], c["volta"])
        except Exception as e:  # noqa: BLE001 — coleta não pode parar por 1 falha
            print(f"[ERRO] {c}: {e}", file=sys.stderr)
            continue
        if not opcoes:
            print(f"[VAZIO] {c}")
            continue
        for res in opcoes:
            linhas.append({
                "ts_utc": agora_utc.strftime("%Y-%m-%d %H:%M"),
                "ts_brt": agora_brt.strftime("%Y-%m-%d %H:%M"),
                "hora_brt": agora_brt.hour,
                "dia_semana_brt": agora_brt.strftime("%a"),
                "origem": c["origem"],
                "destino": c["destino"],
                "data_ida": c["ida"] if c["tipo"] != "VOLTA" else "",
                "data_volta": c["volta"] or (c["ida"] if c["tipo"] == "VOLTA" else ""),
                "tipo": c["tipo"],
                **{k: res[k] for k in (
                    "rank", "cia", "voos_ida", "voos_volta", "via_ida", "via_volta",
                    "partida_ida", "chegada_ida", "partida_volta", "chegada_volta",
                    "conexoes_ida", "conexoes_volta", "duracao_ida", "duracao_volta",
                )},
                "preco_total_brl": res["preco"],
            })
        time.sleep(0.4)  # gentileza com a API
    return linhas


def gravar(linhas: list[dict]) -> None:
    CSV_PATH.parent.mkdir(exist_ok=True)
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
    df = pd.read_csv(CSV_PATH)
    df = df[(df["tipo"] == "RT") & (df["rank"] == 1)]
    msgs = []
    for ln in linhas:
        if ln["tipo"] != "RT" or ln["rank"] != 1:
            continue
        chave = (
            (df["origem"] == ln["origem"]) & (df["destino"] == ln["destino"])
            & (df["data_ida"] == ln["data_ida"])
        )
        hist = df[chave]["preco_total_brl"]
        if len(hist) < 2:
            continue
        anterior = hist.iloc[-2]
        minimo = hist.iloc[:-1].min()
        preco = ln["preco_total_brl"]
        rota = f"{ln['origem']}→{ln['destino']} {ln['data_ida']} a {ln['data_volta']}"

        queda = (anterior - preco) / anterior * 100 if anterior else 0
        limite = CFG["alerta_limite_brl"].get(ln["destino"], 0)

        if preco < minimo:
            msgs.append(f"🔥 NOVO MÍNIMO: {rota} — {brl(preco)} (mínimo anterior {brl(minimo)})")
        elif queda >= CFG["alerta_queda_pct"]:
            msgs.append(f"📉 Queda de {queda:.0f}%: {rota} — {brl(preco)} (antes {brl(anterior)})")
        elif limite and preco <= limite:
            msgs.append(f"🎯 Abaixo do limite: {rota} — {brl(preco)} (limite {brl(limite)})")

    # --- favoritos: vigia itinerários pelo número do 1º voo (ex.: AA930) ---
    # Comparação sempre dentro do mesmo trecho: tipo + rota + datas.
    df_all = pd.read_csv(CSV_PATH)
    chaves = ["tipo", "origem", "destino", "data_ida", "data_volta"]
    for fav in CFG.get("favoritos", []):
        alvo = fav["voos"]
        nome = fav.get("nome", alvo)
        limite = fav.get("limite_brl", 0)
        atuais = [l for l in linhas if str(l.get("voos_ida", "")).startswith(alvo)]
        grupos = {}
        for l in atuais:
            grupos.setdefault(tuple(str(l.get(k, "")) for k in chaves), []).append(l)
        for chave, grupo in grupos.items():
            ln = min(grupo, key=lambda x: x["preco_total_brl"])
            preco = ln["preco_total_brl"]
            m = df_all["voos_ida"].astype(str).str.startswith(alvo)
            for k, v in zip(chaves, chave):
                m &= df_all[k].astype(str).fillna("").eq(v)
            hist = df_all[m & (df_all["ts_utc"] < ln["ts_utc"])]["preco_total_brl"]
            desc = f"{nome} ({alvo}, {ln['tipo']} {ln['origem']}→{ln['destino']})"
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
    linhas = coletar()
    if not linhas:
        print("[AVISO] nenhuma linha coletada nesta execução.")
        return
    primeira_coleta = not CSV_PATH.exists()
    gravar(linhas)
    print(f"[OK] {len(linhas)} preços gravados.")

    if primeira_coleta:
        melhor = min(linhas, key=lambda x: x["preco_total_brl"])
        enviar_email([
            "🎉 PRIMEIRA COLETA COM DADOS! O cache da Aviasales começou a ter preços pras suas rotas.",
            f"Melhor preço visto: {melhor['origem']}→{melhor['destino']} ({melhor['tipo']}) — {brl(melhor['preco_total_brl'])}",
            "A partir de agora o robô acumula histórico de hora em hora. Bom momento pra ativar o GitHub Pages (dashboard).",
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
