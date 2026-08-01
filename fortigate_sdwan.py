#!/usr/bin/env python3
"""
Monitor de SLA de links SD-WAN (FortiGate) - complementa o monitor_rede.py.

Em vez de so pingar host a host, esse modulo trabalha com o conceito de SLA
de performance por link que o FortiGate usa no SD-WAN: cada link (wan1,
wan2, lte, etc) tem metricas de latencia, jitter e perda de pacote, e um
link pode estar "up" mas fora do SLA configurado (por exemplo, latencia alta
demais pra VoIP passar por ali) - e e exatamente isso que a logica abaixo
tenta enxergar, alem do simples up/down.

Duas formas de rodar:
  1) --fixture arquivo.json  -> le uma resposta de exemplo (formato baseado
     na API de monitoramento do FortiGate), sem precisar de FortiGate nenhum.
     serve pra testar a logica ou rodar numa maquina sem acesso a producao.
  2) --api                   -> consulta de verdade a API REST do FortiGate
     (endpoint /api/v2/monitor/virtual-wan/health-check). host e token vem
     de variavel de ambiente (FORTIGATE_HOST / FORTIGATE_TOKEN) - nunca fica
     hardcoded no script.

Aviso sobre os nomes de campo: a resposta dessa API varia um pouco entre
versoes do FortiOS. O normalizador abaixo tenta os aliases mais comuns
(latency/latency_ms, packetloss/packet_loss etc), mas vale conferir contra
a resposta real da sua API antes de apontar isso pra um FortiGate de
producao.
"""

import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

# limites default de SLA - os mesmos conceitos que se configura no health-check
# do FortiGate (aba SD-WAN > Performance SLA). Da pra passar limites diferentes
# por link na hora de criar o EstadoLink, ja que perfis de SLA diferentes
# (ex: voz vs dados) tem tolerancia diferente.
LIMITE_LATENCIA_MS = 150
LIMITE_JITTER_MS = 30
LIMITE_PERDA_PCT = 5
JANELA_HISTORICO = 10
TROCAS_PARA_FLAPPING = 4


def normalizar_membro(bruto):
    """
    Converte um item 'membro' da resposta da API pro formato interno usado
    aqui. Tenta varios nomes de campo possiveis porque isso varia entre
    versoes do FortiOS - melhor nao grudar em um nome so.
    """

    def pega(*chaves, default=None):
        for chave in chaves:
            if chave in bruto and bruto[chave] is not None:
                return bruto[chave]
        return default

    status_bruto = str(pega("status", "state", "link_status", default="down")).lower()

    return {
        "interface": pega("interface", "child", "name", default="desconhecido"),
        "up": status_bruto in ("up", "alive", "online"),
        "latencia_ms": pega("latency", "latency_ms"),
        "jitter_ms": pega("jitter", "jitter_ms"),
        "perda_pct": pega("packetloss", "packet_loss", "packet_loss_percent", "loss"),
    }


class EstadoLink:
    """Historico e classificacao de um link/membro SD-WAN (mesma ideia do EstadoHost do monitor_rede.py)."""

    def __init__(self, nome, limite_latencia=LIMITE_LATENCIA_MS, limite_jitter=LIMITE_JITTER_MS, limite_perda=LIMITE_PERDA_PCT):
        self.nome = nome
        self.historico = deque(maxlen=JANELA_HISTORICO)
        self.trocas_estado = 0
        self.ultimo_status = None
        self.limite_latencia = limite_latencia
        self.limite_jitter = limite_jitter
        self.limite_perda = limite_perda

    def registrar(self, membro):
        self.historico.append(membro)
        status_atual = self._classificar(membro)
        if self.ultimo_status is not None and status_atual != self.ultimo_status:
            self.trocas_estado += 1
        self.ultimo_status = status_atual
        return status_atual

    def _classificar(self, membro):
        if not membro["up"]:
            return "CRITICO"

        if self.trocas_estado >= TROCAS_PARA_FLAPPING:
            return "INSTAVEL"

        fora_do_sla = (
            (membro["latencia_ms"] or 0) >= self.limite_latencia
            or (membro["jitter_ms"] or 0) >= self.limite_jitter
            or (membro["perda_pct"] or 0) >= self.limite_perda
        )
        if fora_do_sla:
            return "FORA_DO_SLA"

        return "OK"

    def resumo(self):
        ultimo = self.historico[-1] if self.historico else {}
        return {
            "link": self.nome,
            "status": self.ultimo_status,
            "latencia_ms": ultimo.get("latencia_ms"),
            "jitter_ms": ultimo.get("jitter_ms"),
            "perda_pct": ultimo.get("perda_pct"),
            "trocas_estado": self.trocas_estado,
        }


CORES = {
    "OK": "\033[92m",
    "FORA_DO_SLA": "\033[93m",
    "CRITICO": "\033[91m",
    "INSTAVEL": "\033[95m",
}
RESET = "\033[0m"


def imprimir_status(resumo):
    cor = CORES.get(resumo["status"], "")
    lat = f"{resumo['latencia_ms']}ms" if resumo["latencia_ms"] is not None else "N/A"
    jit = f"{resumo['jitter_ms']}ms" if resumo["jitter_ms"] is not None else "N/A"
    perda = f"{resumo['perda_pct']}%" if resumo["perda_pct"] is not None else "N/A"
    print(
        f"{cor}[{resumo['status']:>11}]{RESET} {resumo['link']:<25} "
        f"latencia={lat} jitter={jit} perda={perda} trocas={resumo['trocas_estado']}"
    )


def extrair_membros(dados):
    """
    Espera o formato de lista de health-checks, cada um com uma lista de
    membros (formato baseado no que a Fortinet documenta pra
    /monitor/virtual-wan/health-check - mas ver aviso no topo do arquivo
    sobre variacao de nomes de campo entre versoes).
    """
    membros = {}
    resultados = dados.get("results", dados) if isinstance(dados, dict) else dados
    if isinstance(resultados, dict):
        resultados = [resultados]

    for healthcheck in resultados:
        nome_sla = healthcheck.get("name", "sla")
        for bruto in healthcheck.get("members", healthcheck.get("children", [])):
            m = normalizar_membro(bruto)
            chave = f"{nome_sla}/{m['interface']}"
            membros[chave] = m
    return membros


def ler_fixture(caminho):
    with open(caminho, encoding="utf-8") as f:
        dados = json.load(f)
    return extrair_membros(dados)


def consultar_api(host, token, verificar_ssl=True):
    url = f"https://{host}/api/v2/monitor/virtual-wan/health-check"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    contexto = ssl.create_default_context()
    if not verificar_ssl:
        contexto.check_hostname = False
        contexto.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=contexto, timeout=10) as resp:
        dados = json.loads(resp.read().decode("utf-8"))
    return extrair_membros(dados)


def salvar_csv(caminho, linhas_resumo):
    existe = Path(caminho).exists()
    with open(caminho, "a", newline="", encoding="utf-8") as f:
        campos = ["timestamp", "link", "status", "latencia_ms", "jitter_ms", "perda_pct", "trocas_estado"]
        writer = csv.DictWriter(f, fieldnames=campos)
        if not existe:
            writer.writeheader()
        agora = datetime.now().isoformat(timespec="seconds")
        for resumo in linhas_resumo:
            writer.writerow({"timestamp": agora, **resumo})


def rodar_ciclo(membros, estados, log_csv=None):
    resumos = []
    for chave, membro in membros.items():
        if chave not in estados:
            estados[chave] = EstadoLink(chave)
        status = estados[chave].registrar(membro)
        resumo = estados[chave].resumo()
        imprimir_status(resumo)
        resumos.append(resumo)

    if log_csv:
        salvar_csv(log_csv, resumos)

    criticos = [r["link"] for r in resumos if r["status"] == "CRITICO"]
    fora_sla = [r["link"] for r in resumos if r["status"] == "FORA_DO_SLA"]
    if criticos:
        print(f"\n>> ALERTA: link(s) fora do ar -> {', '.join(criticos)}")
    if fora_sla:
        print(f">> ALERTA: link(s) fora do SLA de performance -> {', '.join(fora_sla)}")

    return resumos


def main():
    parser = argparse.ArgumentParser(description="Monitor de SLA de links SD-WAN (FortiGate)")
    fonte = parser.add_mutually_exclusive_group(required=True)
    fonte.add_argument("--fixture", help="arquivo JSON de exemplo (sem precisar de FortiGate real)")
    fonte.add_argument("--api", action="store_true", help="consulta a API real do FortiGate")
    parser.add_argument("--host", default=os.environ.get("FORTIGATE_HOST"), help="IP/hostname do FortiGate (ou variavel FORTIGATE_HOST)")
    parser.add_argument("--inseguro", action="store_true", help="nao valida certificado SSL (comum em FortiGate com cert self-signed)")
    parser.add_argument("--intervalo", type=int, default=30, help="segundos entre ciclos (com --loop)")
    parser.add_argument("--loop", action="store_true", help="roda continuamente ate Ctrl+C")
    parser.add_argument("--log", default="historico_sdwan.csv", help="arquivo CSV de saida")
    args = parser.parse_args()

    token = os.environ.get("FORTIGATE_TOKEN")
    if args.api and not (args.host and token):
        print("faltando host ou token: configure as variaveis FORTIGATE_HOST e FORTIGATE_TOKEN", file=sys.stderr)
        sys.exit(1)

    if args.inseguro:
        print("aviso: verificacao de certificado SSL desativada (--inseguro)\n")

    estados = {}

    print("monitorando SLA de SD-WAN | ctrl+c para sair\n")

    try:
        while True:
            print(f"--- {datetime.now().strftime('%d/%m %H:%M:%S')} ---")
            if args.fixture:
                membros = ler_fixture(args.fixture)
            else:
                membros = consultar_api(args.host, token, verificar_ssl=not args.inseguro)

            rodar_ciclo(membros, estados, log_csv=args.log)
            if not args.loop:
                break
            print()
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print("\nencerrado pelo usuario")


if __name__ == "__main__":
    main()
