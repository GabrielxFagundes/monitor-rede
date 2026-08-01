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

## Proximos passos

- Alerta via webhook/Slack quando um host vira CRITICO
- Exportar direto pra um dashboard (Grafana le CSV via plugin, ou dá pra jogar num banco simples)
