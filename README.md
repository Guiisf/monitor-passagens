# ✈ Monitor de passagens — SP → Orlando / Tampa

Robô que roda sozinho no **GitHub Actions**, consulta o **Google Flights via SerpApi** uma vez por dia, grava histórico em `data/precos.csv`, manda **alerta por e-mail** quando o preço cai e publica um **dashboard** via GitHub Pages.

## O que ele monitora

| Item | Valor |
|---|---|
| Datas | ida **23/09/2026**, volta **29/09/2026** (fixas) |
| Passageiros | 2 adultos, econômica |
| Formato | ida e volta fechado |
| Conexões | até 1 |
| Rotas | GRU→MCO e VCP→MCO (diárias, 2 idas exploradas) · GRU→TPA (seg/qua/sex) · VCP→TPA (seg) |
| Opções | top 5 de cada rota, com companhia, voos, aeroporto de conexão, horários e duração |
| Favoritos | AA930 e AD8706, com alerta próprio |

### Prioridades embutidas no dashboard

- **MCO saindo de GRU é o alvo.** VCP e TPA aparecem como comparativos, mas nunca lado a lado "crus": o painel soma os custos de solo antes de comparar.
- **VCP** carrega R$ 200 de Uber (+1h30 de deslocamento) — `vcp_custo_extra_brl`.
- **TPA** carrega R$ 1.800 de carro/estrada — `tampa_custo_extra_brl` — **e só é marcado como viável** se o voo chegar de manhã (5h–12h) e partir à tarde (12h–19h), por causa da 1h de estrada até Orlando de cada lado (`tampa_janela`). Fora dessa janela a opção aparece com o selo vermelho *horário ruim p/ estrada*.
- A coluna **"custo total na porta"** (tarifa + bagagem + solo) é a que ordena tudo. É nela que se vê se a economia do VCP realmente compensa.

## Bagagem: custo real, não estimativa

Você é **Safira na Azul**, então voos Azul saem com **bagagem inclusa** (`cias_isentas: ["AD"]`) e custo zero no painel.

Nas demais companhias o robô lê o campo `baggage_prices` da SerpApi e calcula `2 malas × valor real × 2 trechos`. Como esse campo só existe na resposta de *booking options* (uma chamada a mais por itinerário), o valor é **cacheado por companhia + rota** em `data/bagagem_cache.json` e revalidado a cada 14 dias — taxa de bagagem não muda de hora em hora.

Cada linha do painel diz de onde veio o número:

| Rótulo | Significado |
|---|---|
| *real* | valor único informado pela API |
| *piso da faixa* | a API devolveu faixa (ex.: "265-885", tarifas diferentes da mesma cia) e o painel usa o piso |
| *estimada* | a API não informou; usa o fallback `custo_por_mala_trecho_brl` (R$ 380) |

A escolha do piso é deliberada: usar o teto inflava tanto o custo que chegava a inverter a comparação entre rotas.

### ⚠ Uma suposição que ainda precisa ser confirmada

A API devolve a bagagem como texto puro — `"1st checked bag: 885"` — **sem dizer se o valor é por trecho ou pela viagem inteira**. O robô assume *viagem inteira* (`bagagem.valor_api_cobre: "viagem"`), porque o valor vem sob a chave `together`, que identifica a reserva feita como bloco único, e porque R$ 885 por trecho daria ~US$ 160 só na ida — caro demais para econômica.

**Isso muda qual rota ganha.** Com os números reais de 18/08:

| suposição | Avianca | American | ganha |
|---|---|---|---|
| cobre a viagem (atual) | R$ 7.995 | R$ 8.157 | Avianca |
| cobre cada trecho | R$ 9.765 | R$ 9.615 | American |

Para confirmar: abra o link do Google Flights de uma linha do painel e veja a taxa de bagagem na página de compra. Se for por trecho, troque `valor_api_cobre` para `"trecho"` — é uma linha, e o robô recalcula tudo na coleta seguinte. O rodapé do painel exibe qual suposição está valendo.

## Cota da SerpApi

O free tier dá **250 buscas/mês**. Uma rota custa `1 + idas_exploradas` buscas: a 1ª chamada lista as idas, e cada ida explorada exige uma 2ª chamada (com `departure_token`) para fechar a volta com o preço do pacote.

**Por que explorar mais de uma ida:** olhar só a ida mais barata engana. Na primeira coleta real, VCP→MCO fechou em R$ 13.891 porque a ida mais barata da Azul só combinava com voltas caras. Uma ida R$ 200 mais cara pode abrir voltas R$ 2.000 mais baratas — por isso as duas rotas MCO exploram 2 idas.

| Dia | Rotas | Buscas |
|---|---|---|
| segunda | 4 | 10 |
| qua e sex | 3 | 8 |
| demais dias | 2 | 6 |

≈ **217 buscas/mês** em coleta, mais alguns créditos esporádicos de bagagem quando o cache expira.

O consumo é contado em `data/uso_serpapi.json`. Se a rodada do dia fosse estourar `orcamento_buscas_mes`, o robô **pula a coleta e te avisa por e-mail** em vez de falhar silenciosamente.

## Passo a passo

### 1. Chave da SerpApi
Painel da SerpApi → **Your Account** → *API Key* → Copy.

### 2. Senha de app do Gmail (pros alertas)
1. Ative verificação em 2 etapas na conta Google.
2. https://myaccount.google.com/apppasswords → crie "Monitor passagens" → guarde a senha de 16 letras.

### 3. Repositório no GitHub
1. Repositório **público** (Actions ilimitado; o CSV não tem dado sensível).
2. **Settings → Secrets and variables → Actions**, crie os secrets:
   - `SERP_KEY` (chave da SerpApi)
   - `MAIL_USER` (seu Gmail), `MAIL_PASS` (senha de app), `MAIL_TO` (quem recebe alerta)
3. **Settings → Pages**: Deploy from a branch → `main` → pasta `/docs`.
4. **Actions** → workflow **monitor-passagens** → *Run workflow* pra testar. Depois roda sozinho todo dia às **09:00 BRT**.

> Se você ainda tiver o secret `TP_TOKEN` da Travelpayouts, pode apagar — não é mais usado.

## Alertas por e-mail

Sempre sobre o **custo total** (tarifa + bagagem), não sobre a tarifa seca:
- 🔥 novo mínimo histórico;
- 📉 queda ≥ 8% desde a última leitura (`alerta_queda_pct`);
- 🎯 abaixo do seu teto (`alerta_limite_brl` por destino ou `limite_brl` por favorito).

Os favoritos casam pelo número exato do voo dentro do itinerário — `AD87` não dispara por causa de um `AD8700`.

## Dashboard

- **Cards "custo total na porta"**: uma por rota, ordenadas do melhor negócio pro pior, com o selo *melhor agora* e o alerta de horário inviável no TPA.
- **Melhores opções agora**: top 3 por rota com companhia, aeroporto de conexão, horários, duração (selo ≤11h) e link do Google Flights pronto pra conferir e comprar.
- **Evolução do custo** e **⭐ voos favoritos**: histórico diário.

## Ajustes rápidos (`config.json`)

| Quero… | Mexer em |
|---|---|
| mudar as datas | `data_ida` / `data_volta` |
| monitorar outra rota ou mudar frequência | `rotas` (`dias` aceita `"todos"` ou lista com 0=seg … 6=dom) |
| procurar mais combinações numa rota | `idas_exploradas` da rota (cada +1 custa 1 busca por coleta) |
| deixar passar tarifas mais caras no painel | `max_fator_preco` (padrão 2.5x a mais barata da rota) |
| mudar o que conta como horário bom pro TPA | `tampa_janela` |
| ser avisado a partir de um preço | `alerta_limite_brl` ou `limite_brl` do favorito |
| apertar/afrouxar a cota | `orcamento_buscas_mes` |

Sugestão: nos favoritos, defina `"limite_brl"` com o patamar que você já viu, pra ser avisado quando voltar a ele.

## Complemento recomendado (grátis)

Ative também o **"Acompanhar preços" do Google Flights** nas suas datas — ele vigia o itinerário exato com o preço final de venda e completa o radar do robô.
