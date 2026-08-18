# -*- coding: utf-8 -*-
"""Gera o dashboard docs/index.html a partir de data/precos.csv."""

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from monitor import regex_favorito

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


def brl(v) -> str:
    return "R$ " + f"{v:,.0f}".replace(",", ".") if v == v and v is not None else "—"


def _con_txt(via, conexoes) -> str:
    """Texto da conexão: aeroporto se conhecido, senão a contagem."""
    if str(via) not in ("", "nan", "None"):
        return f"via {via}"
    try:
        n = int(float(conexoes))
    except (TypeError, ValueError):
        return "direto"
    return "direto" if n == 0 else (f"{n} conexão" if n == 1 else f"{n} conexões")


def fonte_bagagem(fonte) -> str:
    """Rótulo honesto da origem do custo de bagagem mostrado na linha."""
    f = str(fonte)
    if "faixa" in f:
        return "piso da faixa"
    if f.startswith("api"):
        return "real"
    return "estimada"


def _hora(txt) -> int | None:
    try:
        return int(str(txt)[:2])
    except (TypeError, ValueError):
        return None


def tampa_ok(chegada_ida, partida_volta, cfg: dict) -> bool:
    """
    TPA só é viável com 1h de estrada até Orlando dos dois lados:
    chegar de manhã na ida e partir à tarde na volta.
    """
    j = cfg.get("tampa_janela", {})
    ch_min, ch_max = j.get("chegada_ida_h", [5, 12])
    pt_min, pt_max = j.get("partida_volta_h", [12, 19])
    ch, pt = _hora(chegada_ida), _hora(partida_volta)
    if ch is None or pt is None:
        return False
    return ch_min <= ch < ch_max and pt_min <= pt < pt_max


def custo_extra(origem: str, destino: str, cfg: dict) -> tuple[float, list[str]]:
    """Custos de solo que não estão na tarifa: Uber até VCP e carro/estrada de TPA."""
    total, itens = 0.0, []
    if origem == "VCP":
        v = cfg.get("vcp_custo_extra_brl", 0)
        if v:
            total += v
            itens.append(("Uber até VCP (+1h30)", v))
    if destino == "TPA":
        v = cfg.get("tampa_custo_extra_brl", 0)
        if v:
            total += v
            itens.append(("carro/estrada de Tampa", v))
    return total, itens


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


def _cenarios(df: pd.DataFrame, cfg: dict) -> list[dict]:
    """Melhor opção atual de cada rota, já com bagagem e custo de solo somados."""
    ult = df[df["rank"] == 1].sort_values("ts_utc").groupby(
        ["origem", "destino"], as_index=False
    ).last()

    linhas = []
    for r in cfg["rotas"]:
        g = ult[(ult.origem == r["origem"]) & (ult.destino == r["destino"])]
        if not len(g):
            continue
        row = g.iloc[0]
        extra, itens = custo_extra(r["origem"], r["destino"], cfg)
        linhas.append(dict(
            origem=r["origem"], destino=r["destino"], papel=r.get("papel", ""),
            tarifa=row.preco_total_brl, bagagem=row.bagagem_brl,
            bagagem_fonte=row.bagagem_fonte, extras=itens,
            total=row.custo_total_brl + extra,
            viavel=(tampa_ok(row.chegada_ida, row.partida_volta, cfg)
                    if r["destino"] == "TPA" else True),
        ))
    linhas.sort(key=lambda x: x["total"])
    return linhas


def _tabela_opcoes(df: pd.DataFrame, cfg: dict) -> str:
    """Tabela HTML com as melhores opções da coleta mais recente de cada rota."""
    max_min = cfg.get("max_duracao_voo_h", 11) * 60
    ult_ts = df.groupby(["origem", "destino"])["ts_utc"].transform("max")
    atual = df[df.ts_utc == ult_ts].copy()
    atual = atual[atual["rank"] <= 3]
    # ordena pelo custo na porta (com solo), senão VCP/TPA sobem indevidamente
    atual = atual.assign(_solo=[custo_extra(o, d, cfg)[0] for o, d in
                                zip(atual.origem, atual.destino)])
    atual = atual.assign(_total=atual.custo_total_brl + atual._solo).sort_values("_total")

    linhas_html = ""
    for _, r in atual.iterrows():
        rota = f"{r.origem}→{r.destino}→{r.origem}"
        datas = f"{str(r.data_ida)[5:]} a {str(r.data_volta)[5:]}"
        url = link_gf(r.origem, r.destino, r.data_ida, r.data_volta)
        dur = f"{fmt_dur(r.duracao_ida)} / {fmt_dur(r.duracao_volta)}"
        ok11 = (r.duracao_ida <= max_min) and (r.duracao_volta <= max_min)
        via = f"ida {_con_txt(r.via_ida, r.conexoes_ida)} · volta {_con_txt(r.via_volta, r.conexoes_volta)}"
        horario = f"{r.partida_ida}→{r.chegada_ida} / {r.partida_volta}→{r.chegada_volta}"
        voos = f"{r.voos_ida} / {r.voos_volta}"

        badge = '<span class="ok11">≤11h</span>' if ok11 else '<span class="no11">+11h</span>'
        if r.destino == "TPA":
            viavel = tampa_ok(r.chegada_ida, r.partida_volta, cfg)
            sel_tpa = (' <span class="ok11">✓ estrada ok</span>' if viavel
                       else ' <span class="no11">✗ horário ruim p/ estrada</span>')
        else:
            sel_tpa = ""

        if r.bagagem_brl == 0:
            bag_html = '<div class="sub bag-free">bagagem inclusa ✓</div>'
        else:
            bag_html = f'<div class="sub">+ {brl(r.bagagem_brl)} bagagem ({fonte_bagagem(r.bagagem_fonte)})</div>'

        extra, total = r._solo, r._total
        extra_html = f'<div class="sub">+ {brl(extra)} solo</div>' if extra else ""
        classe = "sem-bag" if r.bagagem_brl == 0 else "com-bag"

        linhas_html += (
            f'<tr><td>{rota}<div class="sub">{datas} · {via} · {voos}</div></td>'
            f'<td>{nome_cia(r.cia)}<div class="sub">{horario}{sel_tpa}</div></td>'
            f'<td>{dur} {badge}</td>'
            f'<td class="preco-td">{brl(r.preco_total_brl)}{bag_html}{extra_html}</td>'
            f'<td class="preco-td"><span class="{classe}">{brl(total)}</span></td>'
            f'<td><a href="{url}" target="_blank">abrir ↗</a></td></tr>'
        )
    return (
        '<table class="opcoes"><thead><tr><th>rota</th><th>companhia · horários</th>'
        '<th>duração</th><th>tarifa 2 adultos</th><th>custo total na porta</th>'
        '<th>Google Flights</th></tr></thead>'
        f"<tbody>{linhas_html}</tbody></table>"
    )


def gerar_dashboard(csv_path: Path, saida: Path, cfg: dict) -> None:
    df = pd.read_csv(csv_path)
    df["ts_brt"] = pd.to_datetime(df["ts_brt"])
    rt = df[df["rank"] == 1].copy()
    rt["rota"] = rt.origem + "→" + rt.destino

    # ---- série temporal: custo total real (tarifa + bagagem) ----
    fig1 = go.Figure()
    for rota, g in rt.groupby("rota"):
        dest = rota.split("→")[1][:3]
        fig1.add_trace(go.Scatter(
            x=g.ts_brt, y=g.custo_total_brl, name=rota, mode="lines+markers",
            line=dict(color=CORES_ROTA.get(dest, ROSE), width=1.8,
                      dash="dot" if rota.startswith("VCP") else "solid"),
            marker=dict(size=5),
        ))
    _layout(fig1, "EVOLUÇÃO DO CUSTO — IDA E VOLTA · 2 ADULTOS · TARIFA + BAGAGEM")

    # ---- cards de cenário ----
    cen = _cenarios(df, cfg)
    cards = ""
    for i, c in enumerate(cen):
        nome = f"{c['origem']} → {'ORLANDO (MCO)' if c['destino'] == 'MCO' else 'TAMPA (TPA)'}"
        cor = CORES_ROTA[c["destino"]]
        selo = ""
        if i == 0:
            selo = '<span class="melhor">melhor agora</span>'
        if not c["viavel"]:
            selo = '<span class="invi">horário inviável p/ estrada</span>'
        bag_txt = "inclusa ✓" if c["bagagem"] == 0 else brl(c["bagagem"])
        extras_html = "".join(
            f'<div class="linha"><span>+ {rot}</span><b>{brl(v)}</b></div>' for rot, v in c["extras"]
        )
        cards += f"""
        <div class="card" style="border-top:3px solid {cor}">
          <div class="eyebrow">{nome} {selo}</div>
          <div class="preco">{brl(c['total'])}</div>
          <div class="linha"><span>tarifa 2 adultos</span><b>{brl(c['tarifa'])}</b></div>
          <div class="linha"><span>bagagem despachada</span><b>{bag_txt}</b></div>
          {extras_html}
          <div class="linha total"><span>custo total na porta</span><b>{brl(c['total'])}</b></div>
        </div>"""

    tabela = _tabela_opcoes(df, cfg)

    # ---- favoritos ----
    fig_fav = None
    favs = cfg.get("favoritos", [])
    if favs:
        fig_fav = go.Figure()
        tem_dado = False
        for i, fav in enumerate(favs):
            alvo = fav["voos"]
            padrao = regex_favorito(alvo)
            m = (df.voos_ida.astype(str).str.contains(padrao, regex=True)
                 | df.voos_volta.astype(str).str.contains(padrao, regex=True))
            g = df[m].sort_values("ts_brt")
            if not len(g):
                continue
            tem_dado = True
            fig_fav.add_trace(go.Scatter(
                x=g.ts_brt, y=g.custo_total_brl, name=fav.get("nome", alvo),
                mode="lines+markers", line=dict(color=[AMBER, TEAL, ROSE][i % 3], width=2),
                marker=dict(size=5),
            ))
        if tem_dado:
            _layout(fig_fav, "⭐ VOOS FAVORITOS — histórico do itinerário vigiado", 360)
        else:
            fig_fav = None

    ultima = df.ts_brt.max().strftime("%d/%m/%Y %H:%M")
    n = len(df)
    ida_fmt = f"{cfg['data_ida'][8:]}/{cfg['data_ida'][5:7]}"
    volta_fmt = f"{cfg['data_volta'][8:]}/{cfg['data_volta'][5:7]}"

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
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;margin:18px 0 26px}}
  .card{{background:{PANEL};border-radius:10px;padding:16px 18px}}
  .eyebrow{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:{MUT};letter-spacing:.12em}}
  .melhor{{color:{BG};background:{TEAL};border-radius:3px;padding:1px 5px;font-size:9.5px;letter-spacing:.06em;margin-left:4px}}
  .invi{{color:{BG};background:{ROSE};border-radius:3px;padding:1px 5px;font-size:9.5px;letter-spacing:.06em;margin-left:4px}}
  .preco{{font-family:'IBM Plex Mono',monospace;font-size:30px;font-weight:600;margin:8px 0 12px;color:{TXT}}}
  .linha{{display:flex;justify-content:space-between;font-size:12.5px;color:{MUT};padding:3px 0;gap:10px}}
  .linha b{{color:{TXT};font-family:'IBM Plex Mono',monospace;font-weight:400;white-space:nowrap}}
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
  .com-bag{{color:{ROSE};font-weight:600}}
  .sem-bag{{color:{TEAL};font-weight:600}}
  .bag-free{{color:{TEAL}}}
  table.opcoes a{{color:{TEAL};text-decoration:none}}
  .ok11{{color:{TEAL};font-size:10px;font-family:'IBM Plex Mono',monospace}}
  .no11{{color:{ROSE};font-size:10px;font-family:'IBM Plex Mono',monospace}}
  footer{{color:{MUT};font-size:11px;text-align:center;padding:14px;font-family:'IBM Plex Mono',monospace;line-height:1.7}}
</style></head><body>
<header>
  <div class="board">✈ Painel de preços · monitoramento automático</div>
  <h1>SÃO PAULO → ORLANDO / TAMPA</h1>
  <div class="meta">última coleta {ultima} BRT · {n} leituras acumuladas · ida {ida_fmt} · volta {volta_fmt} · 2 adultos · até 1 conexão</div>
</header>
<main>
  <h2>Custo total na porta <span class="h2sub">tarifa + bagagem despachada + custo de solo (Uber até VCP, carro de Tampa) — comparação justa entre as rotas</span></h2>
  <div class="cards">{cards}</div>
  <h2>Melhores opções agora <span class="h2sub">top 3 de cada rota · Azul sai com bagagem inclusa pelo seu Safira</span></h2>
  <div class="tabela-wrap">{tabela}</div>
  <div class="grafico">{fig1.to_html(full_html=False, include_plotlyjs="cdn")}</div>
  {('<div class="grafico">' + fig_fav.to_html(full_html=False, include_plotlyjs=False) + "</div>") if fig_fav is not None else ""}
</main>
<footer>
  gerado automaticamente pelo monitor · preços do Google Flights via SerpApi · bagagem real do campo baggage_prices (fallback R$ {cfg.get("bagagem", {}).get("custo_por_mala_trecho_brl", 0)}/mala/trecho quando a API não informa)<br>
  Tampa só conta como opção se o voo chegar de manhã e partir à tarde — 1h de estrada até Orlando de cada lado<br>
  <b>suposição a confirmar:</b> o valor de bagagem da API é tratado como cobrindo {"cada trecho (ida e volta contam separado)" if cfg.get("bagagem", {}).get("valor_api_cobre") == "trecho" else "a viagem inteira (ida e volta juntas)"} — confira na página de compra; se estiver errado, o custo de bagagem dobra ou cai pela metade e pode inverter qual rota ganha
</footer>
</body></html>"""

    saida.parent.mkdir(exist_ok=True)
    saida.write_text(html, encoding="utf-8")
