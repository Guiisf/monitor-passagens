# -*- coding: utf-8 -*-
"""Gera o dashboard docs/index.html a partir de data/precos.csv."""

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

# Paleta "painel de embarque"
BG = "#0A1626"
PANEL = "#101F33"
GRID = "#1D3049"
TXT = "#E8EDF4"
MUT = "#8FA1B8"
AMBER = "#FFB840"   # Orlando
TEAL = "#3FD0B6"    # Tampa
ROSE = "#FF7A9E"

CORES_ROTA = {"MCO": AMBER, "TPA": TEAL}
TRACO_DATA = {0: "solid", 1: "dot"}



CIAS = {
    "LA": "LATAM", "CM": "Copa", "AD": "Azul", "G3": "GOL", "AA": "American",
    "UA": "United", "DL": "Delta", "AV": "Avianca", "TP": "TAP", "B6": "JetBlue",
    "NK": "Spirit", "F9": "Frontier", "AM": "Aeroméxico", "AR": "Aerolíneas Arg.",
}


def nome_cia(codigos: str) -> str:
    return ", ".join(CIAS.get(c, c) for c in str(codigos).split(",") if c)


def fmt_dur(minutos) -> str:
    try:
        m = int(minutos)
    except (TypeError, ValueError):
        return "—"
    return f"{m // 60}h{m % 60:02d}"


def _con_txt(via, conexoes) -> str:
    """Texto da conexão: aeroporto se conhecido, senão a contagem."""
    if str(via) not in ("", "nan", "None"):
        return f"via {via}"
    try:
        n = int(float(conexoes))
    except (TypeError, ValueError):
        return "direto"
    return "direto" if n == 0 else (f"{n} conexão" if n == 1 else f"{n} conexões")


def chega_manha(chegada: str) -> bool:
    try:
        h = int(str(chegada)[:2])
    except ValueError:
        return False
    return 5 <= h < 12


def link_gf(origem, destino, ida, volta=None) -> str:
    from urllib.parse import quote_plus
    if volta:
        q = f"Flights from {origem} to {destino} on {ida} through {volta}"
    else:
        q = f"One way flights from {origem} to {destino} on {ida}"
    return f"https://www.google.com/travel/flights?q={quote_plus(q)}&hl=pt-BR&curr=BRL"


def _layout(fig: go.Figure, titulo: str, altura: int = 420) -> go.Figure:
    fig.update_layout(
        title=dict(text=titulo, font=dict(size=15, color=TXT)),
        height=altura,
        paper_bgcolor=PANEL,
        plot_bgcolor=PANEL,
        font=dict(family="'IBM Plex Mono', monospace", color=MUT, size=11),
        margin=dict(l=50, r=20, t=50, b=40),
        legend=dict(orientation="h", y=-0.18, font=dict(size=10)),
        hovermode="x unified",
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, tickprefix="R$ ", separatethousands=True)
    return fig


def _cenarios(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Melhor preço atual por destino/data: RT fechado vs. só ida + só volta."""
    ult = df[df["rank"] == 1].sort_values("ts_utc").groupby(
        ["origem", "destino", "data_ida", "data_volta", "tipo"], as_index=False
    ).last()

    linhas = []
    for d in cfg["destinos"]:
        for ida, volta in cfg["datas"]:
            rt = ult[(ult.tipo == "RT") & (ult.destino == d) & (ult.data_ida == ida)]
            melhor_rt = rt.preco_total_brl.min() if len(rt) else None

            so_ida = ult[(ult.tipo == "IDA") & (ult.destino == d) & (ult.data_ida == ida)]
            so_volta = ult[(ult.tipo == "VOLTA") & (ult.origem == d) & (ult.data_volta == volta)]
            melhor_ow = (
                so_ida.preco_total_brl.min() + so_volta.preco_total_brl.min()
                if len(so_ida) and len(so_volta) else None
            )
            extra = cfg["tampa_custo_extra_brl"] if d == "TPA" else 0
            linhas.append(dict(
                destino=d, ida=ida, volta=volta,
                rt=melhor_rt, ow=melhor_ow, extra=extra,
                total=(melhor_rt + extra) if melhor_rt else None,
            ))
    return pd.DataFrame(linhas)


def brl(v) -> str:
    return "R$ " + f"{v:,.0f}".replace(",", ".") if v == v and v is not None else "—"




def _tabela_opcoes(df: pd.DataFrame, cfg: dict) -> str:
    """Tabela HTML com as melhores opções da coleta mais recente de cada consulta."""
    max_min = cfg.get("max_duracao_voo_h", 11) * 60
    ult_ts = df.groupby(["origem", "destino", "data_ida", "data_volta", "tipo"])["ts_utc"].transform("max")
    atual = df[df.ts_utc == ult_ts].copy()
    atual = atual[atual["rank"] <= 3].sort_values(["tipo", "preco_total_brl"])

    linhas_html = ""
    for _, r in atual.iterrows():
        if r.tipo == "RT":
            rota = f"{r.origem}→{r.destino}→{r.origem}"
            datas = f"{str(r.data_ida)[5:]} a {str(r.data_volta)[5:]}"
            url = link_gf(r.origem, r.destino, r.data_ida, r.data_volta)
            dur = f"{fmt_dur(r.duracao_ida)} / {fmt_dur(r.duracao_volta)}"
            ok11 = (r.duracao_ida <= max_min) and (r.duracao_volta <= max_min)
            via = f"ida {_con_txt(r.via_ida, r.conexoes_ida)} · volta {_con_txt(r.via_volta, r.conexoes_volta)}"
            horario = f"{r.partida_ida}→{r.chegada_ida} / {r.partida_volta}→{r.chegada_volta}"
            manha = chega_manha(r.chegada_ida)
            voos = f"{r.voos_ida} / {r.voos_volta}"
        else:
            sentido = "IDA" if r.tipo == "IDA" else "VOLTA"
            rota = f"{r.origem}→{r.destino} ({sentido.lower()})"
            data_ref = r.data_ida if r.tipo == "IDA" else r.data_volta
            datas = str(data_ref)[5:]
            url = link_gf(r.origem, r.destino, data_ref)
            dur = fmt_dur(r.duracao_ida)
            ok11 = r.duracao_ida <= max_min
            via = _con_txt(r.via_ida, r.conexoes_ida)
            horario = f"{r.partida_ida}→{r.chegada_ida}"
            manha = chega_manha(r.chegada_ida) if r.tipo == "IDA" else False
            voos = r.voos_ida
        badge = '<span class="ok11">≤11h</span>' if ok11 else '<span class="no11">+11h</span>'
        sol = ' <span class="manha">🌅 chega manhã</span>' if manha else ""
        linhas_html += (
            f'<tr><td>{rota}<div class="sub">{datas} · {via} · {voos}</div></td>'
            f'<td>{nome_cia(r.cia)}<div class="sub">{horario}{sol}</div></td>'
            f'<td>{dur} {badge}</td>'
            f'<td class="preco-td">{brl(r.preco_total_brl)}</td>'
            f'<td><a href="{url}" target="_blank">abrir ↗</a></td></tr>'
        )
    return (
        '<table class="opcoes"><thead><tr><th>rota</th><th>companhia · horários</th>'
        '<th>duração</th><th>preço 2 adultos</th><th>Google Flights</th></tr></thead>'
        f"<tbody>{linhas_html}</tbody></table>"
    )


def gerar_dashboard(csv_path: Path, saida: Path, cfg: dict) -> None:
    df = pd.read_csv(csv_path)
    df["ts_brt"] = pd.to_datetime(df["ts_brt"])
    rt = df[(df.tipo == "RT") & (df["rank"] == 1)].copy()
    rt["rota"] = rt.origem + "→" + rt.destino + " · " + rt.data_ida.str[5:].str.replace("-", "/")

    # ---- 1. série temporal ----
    fig1 = go.Figure()
    for i, (rota, g) in enumerate(rt.groupby("rota")):
        dest = rota.split("→")[1][:3]
        fig1.add_trace(go.Scatter(
            x=g.ts_brt, y=g.preco_total_brl, name=rota, mode="lines+markers",
            line=dict(color=CORES_ROTA.get(dest, ROSE), width=1.6,
                      dash="dot" if "24/" in rota else "solid"),
            marker=dict(size=4),
        ))
    _layout(fig1, "EVOLUÇÃO DO PREÇO — IDA E VOLTA · 2 ADULTOS · TARIFA BASE")

    # ---- 2. padrão por hora do dia ----
    por_hora = rt.groupby(["hora_brt", "destino"]).preco_total_brl.agg(["mean", "min"]).reset_index()
    fig2 = go.Figure()
    for d in por_hora.destino.unique():
        g = por_hora[por_hora.destino == d]
        fig2.add_trace(go.Bar(x=g.hora_brt, y=g["mean"], name=f"{d} · média",
                              marker_color=CORES_ROTA.get(d, ROSE), opacity=0.85))
    _layout(fig2, "PADRÃO POR HORÁRIO DA CONSULTA (BRT) — preço médio", 380)
    fig2.update_xaxes(title="hora do dia", dtick=1)

    # ---- 3. padrão por dia da semana ----
    ordem = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    nomes = dict(zip(ordem, ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]))
    por_dia = rt.groupby(["dia_semana_brt", "destino"]).preco_total_brl.mean().reset_index()
    fig3 = go.Figure()
    for d in por_dia.destino.unique():
        g = por_dia[por_dia.destino == d].set_index("dia_semana_brt").reindex(ordem).reset_index()
        fig3.add_trace(go.Bar(x=[nomes[x] for x in g.dia_semana_brt], y=g.preco_total_brl,
                              name=d, marker_color=CORES_ROTA.get(d, ROSE), opacity=0.85))
    _layout(fig3, "PADRÃO POR DIA DA SEMANA DA CONSULTA — preço médio", 380)

    # ---- cards de cenário ----
    cen = _cenarios(df, cfg)
    cards = ""
    for _, r in cen.iterrows():
        if r.rt is None:
            continue
        nome = "ORLANDO (MCO)" if r.destino == "MCO" else "TAMPA (TPA) + carro"
        cor = CORES_ROTA[r.destino]
        ow_html = brl(r.ow) if r.ow else "—"
        extra_html = (
            f'<div class="linha"><span>+ carro/estrada</span><b>{brl(r.extra)}</b></div>'
            if r.extra else ""
        )
        cards += f"""
        <div class="card" style="border-top:3px solid {cor}">
          <div class="eyebrow">{nome} · {r.ida[8:]}/{r.ida[5:7]} → {r.volta[8:]}/{r.volta[5:7]}</div>
          <div class="preco">{brl(r.rt)}</div>
          <div class="linha"><span>ida e volta fechado</span><b>{brl(r.rt)}</b></div>
          <div class="linha"><span>só ida + só volta</span><b>{ow_html}</b></div>
          {extra_html}
          <div class="linha total"><span>custo total do cenário</span><b>{brl(r.total)}</b></div>
        </div>"""

    tabela = _tabela_opcoes(df, cfg)

    # ---- favoritos: histórico de preço dos itinerários vigiados ----
    fig_fav = None
    favs = cfg.get("favoritos", [])
    if favs and "voos_ida" in df.columns:
        fig_fav = go.Figure()
        tem_dado = False
        for i, fav in enumerate(favs):
            alvo = fav["voos"]
            g = df[df.voos_ida.astype(str).str.startswith(alvo) | df.voos_volta.astype(str).str.startswith(alvo)].sort_values("ts_brt")
            if not len(g):
                continue
            tem_dado = True
            fig_fav.add_trace(go.Scatter(
                x=g.ts_brt, y=g.preco_total_brl, name=fav.get("nome", alvo),
                mode="lines+markers", line=dict(color=[AMBER, TEAL, ROSE][i % 3], width=2),
                marker=dict(size=5),
            ))
        if tem_dado:
            _layout(fig_fav, "⭐ VOOS FAVORITOS — histórico do itinerário vigiado", 360)
        else:
            fig_fav = None

    ultima = df.ts_brt.max().strftime("%d/%m/%Y %H:%M")
    n = len(df)

    html = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel de preços · SP ✈ Flórida</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>
  body{{margin:0;background:{BG};color:{TXT};font-family:Inter,sans-serif}}
  header{{padding:28px 24px 18px;border-bottom:1px solid {GRID}}}
  .board{{font-family:'IBM Plex Mono',monospace;letter-spacing:.18em;font-size:13px;color:{AMBER};text-transform:uppercase}}
  h1{{margin:6px 0 4px;font-family:'IBM Plex Mono',monospace;font-size:clamp(19px,4vw,26px);letter-spacing:.04em}}
  .meta{{color:{MUT};font-size:12px;font-family:'IBM Plex Mono',monospace}}
  main{{max-width:1080px;margin:0 auto;padding:20px 16px 60px}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:18px 0 26px}}
  .card{{background:{PANEL};border-radius:10px;padding:16px 18px}}
  .eyebrow{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:{MUT};letter-spacing:.12em}}
  .preco{{font-family:'IBM Plex Mono',monospace;font-size:30px;font-weight:600;margin:8px 0 12px;color:{TXT}}}
  .linha{{display:flex;justify-content:space-between;font-size:12.5px;color:{MUT};padding:3px 0}}
  .linha b{{color:{TXT};font-family:'IBM Plex Mono',monospace;font-weight:400}}
  .linha.total{{border-top:1px solid {GRID};margin-top:8px;padding-top:8px}}
  .linha.total b{{color:{AMBER}}}
  .grafico{{background:{PANEL};border-radius:10px;margin-bottom:18px;overflow:hidden}}
  h2{{font-family:'IBM Plex Mono',monospace;font-size:15px;letter-spacing:.06em;margin:26px 0 10px}}
  .h2sub{{display:block;font-size:11px;color:{MUT};font-weight:400;margin-top:3px}}
  .tabela-wrap{{background:{PANEL};border-radius:10px;padding:6px 14px 10px;overflow-x:auto;margin-bottom:26px}}
  table.opcoes{{width:100%;border-collapse:collapse;font-size:12.5px}}
  table.opcoes th{{text-align:left;color:{MUT};font-family:'IBM Plex Mono',monospace;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;padding:10px 8px;border-bottom:1px solid {GRID}}}
  table.opcoes td{{padding:9px 8px;border-bottom:1px solid {GRID};vertical-align:top}}
  table.opcoes .sub{{color:{MUT};font-size:11px;margin-top:2px}}
  .preco-td{{font-family:'IBM Plex Mono',monospace;color:{AMBER};white-space:nowrap}}
  table.opcoes a{{color:{TEAL};text-decoration:none}}
  .manha{{color:{AMBER};font-size:10px;font-family:'IBM Plex Mono',monospace}}
  .ok11{{color:{TEAL};font-size:10px;font-family:'IBM Plex Mono',monospace}}
  .no11{{color:{ROSE};font-size:10px;font-family:'IBM Plex Mono',monospace}}
  footer{{color:{MUT};font-size:11px;text-align:center;padding:14px;font-family:'IBM Plex Mono',monospace}}
</style></head><body>
<header>
  <div class="board">✈ Painel de preços · monitoramento automático</div>
  <h1>SÃO PAULO → ORLANDO / TAMPA</h1>
  <div class="meta">última coleta {ultima} BRT · {n} leituras acumuladas · 2 adultos (2x tarifa individual do cache Aviasales) · até 1 conexão · tarifa base — confira a bagagem no link</div>
</header>
<main>
  <div class="cards">{cards}</div>
  <h2>Melhores opções agora <span class="h2sub">top 3 de cada consulta · combine uma IDA com uma VOLTA de cias diferentes</span></h2>
  <div class="tabela-wrap">{tabela}</div>
  <div class="grafico">{fig1.to_html(full_html=False, include_plotlyjs="cdn")}</div>
  {('<div class="grafico">' + fig_fav.to_html(full_html=False, include_plotlyjs=False) + "</div>") if fig_fav is not None else ""}
  <div class="grafico">{fig2.to_html(full_html=False, include_plotlyjs=False)}</div>
  <div class="grafico">{fig3.to_html(full_html=False, include_plotlyjs=False)}</div>
</main>
<footer>gerado automaticamente pelo monitor · menores preços do cache Aviasales (Travelpayouts), tarifa base por 2 adultos · cenário Tampa soma {brl(cfg["tampa_custo_extra_brl"])} de carro/estrada</footer>
</body></html>"""

    saida.parent.mkdir(exist_ok=True)
    saida.write_text(html, encoding="utf-8")
