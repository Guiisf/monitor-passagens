# ✈ Monitor de passagens — SP → Orlando / Tampa

Robô que roda sozinho no **GitHub Actions**, consulta a **Data API da Travelpayouts (Aviasales)** de hora em hora, grava histórico em `data/precos.csv`, manda **alerta por e-mail** quando o preço cai e publica um **dashboard** via GitHub Pages.

## O que ele monitora (toda hora, 24x/dia)

| Item | Valor |
|---|---|
| Origens | GRU e VCP (ida e volta); GRU, VCP e **CGH** nos trechos avulsos |
| Destinos | MCO (Orlando) e TPA (Tampa) |
| Datas | 23→29/09 e 24→30/09/2026 |
| Formatos | ida e volta fechado **e** só ida + só volta |
| Conexões | até 1 |
| Opções | top 5 de cada consulta, com companhia, 1º voo (ex.: AA930), horários e duração |
| Favoritos | itinerários vigiados de perto (ex.: AA930), com alerta próprio |

**Importante entender o dado:** o preço vem do cache real de buscas da Aviasales — tarifa base, por 1 adulto (o robô multiplica por 2). **Bagagem despachada não é garantida** nessa tarifa: o robô é o radar de tendência e oportunidade; a conferência final com mala você faz no link do Google Flights de cada linha do dashboard. A API é gratuita e sem cota apertada, por isso a grade completa (32 consultas) roda de hora em hora.

## Passo a passo (uma vez só, ~15 min)

### 1. Token da Travelpayouts ✅ (você já tem)
Painel → Profile → **API token** → Copy.

### 2. Senha de app do Gmail (pros alertas)
1. Ative verificação em 2 etapas na conta Google.
2. https://myaccount.google.com/apppasswords → crie "Monitor passagens" → guarde a senha de 16 letras.

### 3. Repositório no GitHub
1. Crie um repositório **público** (Actions ilimitado; o CSV não tem dado sensível).
2. Suba todos os arquivos desta pasta.
3. **Settings → Secrets and variables → Actions**, crie os secrets:
   - `TP_TOKEN` (token da Travelpayouts)
   - `MAIL_USER` (seu Gmail), `MAIL_PASS` (senha de app), `MAIL_TO` (quem recebe alerta)
4. **Settings → Pages**: Deploy from a branch → `main` → pasta `/docs`. Dashboard em `https://SEU_USUARIO.github.io/NOME_DO_REPO/`.
5. **Actions** → workflow **monitor-passagens** → *Run workflow* pra testar. Depois roda sozinho de hora em hora.

## Alertas por e-mail

Nas rotas ida-e-volta (melhor preço) e nos **favoritos**:
- 🔥 novo mínimo histórico;
- 📉 queda ≥ 8% desde a última leitura (`alerta_queda_pct`);
- 🎯 abaixo do seu teto (`alerta_limite_brl` por destino ou `limite_brl` por favorito).

Sugestão: no favorito AA930, defina `"limite_brl": 5500` pra ser avisado quando voltar ao patamar que você já viu.

## Dashboard

- **Cards de cenário**: melhor preço por destino/data, RT vs. só ida + só volta, com Tampa somando R$ 1.800 de carro/estrada (`tampa_custo_extra_brl`).
- **Melhores opções agora**: top 3 por consulta com companhia, horários (selo 🌅 chega manhã), duração (selo ≤11h) e link do Google Flights pronto pra conferir e comprar.
- **⭐ Voos favoritos**: histórico de preço do itinerário exato vigiado.
- **Evolução do preço** + **padrão por horário** e **por dia da semana** — com grade horária 24x/dia, em ~1 semana dá pra ver se existe hora mais barata de verdade.

## Complemento recomendado (grátis)

Ative também o **"Acompanhar preços" do Google Flights** no voo AA930+AA1693 e nas buscas GRU→MCO/TPA das suas datas — ele vigia o itinerário exato com o preço final de venda e completa o radar do robô.
