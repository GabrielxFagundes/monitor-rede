# monitor-rede

Script em Python pra monitorar disponibilidade e latencia de uma lista de hosts (servidores, switches, gateways) via ping, com uma logica de classificacao um pouco mais esperta que "respondeu / nao respondeu".

Surgiu de uma necessidade real: em ambiente com dezenas de servidores e switches, um host que cai uma vez e volta gera ruido; o que importa e distinguir queda real, degradacao (latencia subindo, perda de pacote) e instabilidade (flapping).

## Como classifica

Cada host mantem um historico das ultimas N checagens (janela deslizante) e o status sai desse historico, nao de um ping isolado:

- `OK` - respondendo dentro do esperado
- `ATENCAO` - perda de pacote ou latencia media acima do limite configurado
- `CRITICO` - falhou N vezes seguidas (host fora do ar)
- `INSTAVEL` - troca de estado (up/down) demais dentro da janela - flapping
- `SEM_DADOS` - ainda nao tem historico suficiente pra classificar

Os limites (`LIMITE_LATENCIA_MS`, `LIMITE_PERDA_PCT`, tamanho da janela, etc) estao no topo do arquivo, da pra ajustar direto.

## Uso

```
python monitor_rede.py --hosts hosts.txt
python monitor_rede.py --hosts hosts.txt --intervalo 30 --loop
```

`hosts.txt` e um host/IP por linha:

```
192.168.0.1
switch-core-01
10.0.5.20
```

Cada ciclo gera log em CSV (`historico_monitor.csv` por padrao) com timestamp, status e metricas de cada host, pra dar pra puxar depois numa planilha ou plotar um grafico de tendencia.

Funciona em Windows e Linux (o parsing do output do ping muda entre os dois).

## fortigate_sdwan.py

Extensao do mesmo raciocinio, mas pro mundo SD-WAN. Em vez de so pingar host, esse aqui trabalha com o conceito de SLA de performance por link que o FortiGate usa: cada link (wan1, wan2, lte, etc) tem latencia, jitter e perda de pacote, e um link pode estar "up" mas fora do SLA configurado - por exemplo, latencia alta demais pra um perfil de voz passar por ali.

Estados possiveis: `OK`, `FORA_DO_SLA` (link ativo mas fora de algum limite de latencia/jitter/perda), `CRITICO` (link fora do ar) e `INSTAVEL` (flapping).

Duas formas de rodar:

```
# sem precisar de FortiGate nenhum, usando uma resposta de exemplo
python fortigate_sdwan.py --fixture exemplo_health_check.json

# apontando pra API real (endpoint /api/v2/monitor/virtual-wan/health-check)
export FORTIGATE_HOST=10.0.0.1
export FORTIGATE_TOKEN=xxxxx
python fortigate_sdwan.py --api --loop --intervalo 30
```

Host e token sempre vem de variavel de ambiente, nunca hardcoded. `--inseguro` desativa a validacao de certificado SSL pra quem usa cert self-signed no FortiGate (avisa no console quando ligado).

Importante: os nomes de campo dessa API variam um pouco entre versoes do FortiOS. Tem um normalizador (`normalizar_membro`) que tenta os aliases mais comuns (`latency`/`latency_ms`, `packetloss`/`packet_loss`, etc), mas vale conferir contra a resposta real da sua API antes de apontar isso pra um FortiGate de producao - o `exemplo_health_check.json` mostra o formato esperado.

## Proximos passos

- Alerta via webhook/Slack quando um host ou link vira CRITICO
- Exportar direto pra um dashboard (Grafana le CSV via plugin, ou dá pra jogar num banco simples)
