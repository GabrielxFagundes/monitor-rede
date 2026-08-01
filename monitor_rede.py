#!/usr/bin/env python3
"""
Monitor de saude de rede - ping continuo com deteccao de degradacao.

Faz ping em uma lista de hosts (servidores, switches, gateways) e classifica
o estado de cada um com base em perda de pacote, latencia e instabilidade
(flapping), nao so em "respondeu ou nao respondeu".

Uso:
    python monitor_rede.py --hosts hosts.txt
    python monitor_rede.py --hosts hosts.txt --intervalo 30 --loop
"""

import argparse
import csv
import json
import platform
import statistics
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# limites default - da pra sobrescrever via --config
LIMITE_LATENCIA_MS = 100
LIMITE_PERDA_PCT = 20
JANELA_HISTORICO = 10
FALHAS_PARA_CRITICO = 3
TROCAS_PARA_FLAPPING = 4


def ping_host(host, timeout_ms=1000):
    """Dispara um ping unico e devolve (respondeu, latencia_ms)."""
    sistema = platform.system().lower()
    if sistema == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]

    try:
        saida = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return False, None

    if saida.returncode != 0:
        return False, None

    latencia = extrair_latencia(saida.stdout, sistema)
    return latencia is not None, latencia


def extrair_latencia(texto, sistema):
    """Pega o tempo de resposta do output do ping (windows e linux tem formatos diferentes)."""
    texto = texto.lower()
    try:
        if sistema == "windows":
            # ex: "tempo=23ms" ou "time=23ms" dependendo do idioma do windows
            for token in texto.replace("=", " ").split():
                if token.endswith("ms"):
                    return float(token[:-2].replace(",", "."))
        else:
            # ex: "time=23.4 ms"
            idx = texto.find("time=")
            if idx == -1:
                return None
            trecho = texto[idx + 5:].split()[0]
            return float(trecho.replace("ms", ""))
    except (ValueError, IndexError):
        return None
    return None


class EstadoHost:
    """Guarda o historico recente de um host e decide o status atual."""

    def __init__(self, nome):
        self.nome = nome
        self.historico = deque(maxlen=JANELA_HISTORICO)
        self.falhas_seguidas = 0
        self.trocas_estado = 0
        self.ultimo_status = None

    def registrar(self, respondeu, latencia):
        self.historico.append((respondeu, latencia))

        if respondeu:
            self.falhas_seguidas = 0
        else:
            self.falhas_seguidas += 1

        status_atual = self._classificar()
        if self.ultimo_status is not None and status_atual != self.ultimo_status:
            self.trocas_estado += 1
        self.ultimo_status = status_atual
        return status_atual

    def _classificar(self):
        if self.falhas_seguidas >= FALHAS_PARA_CRITICO:
            return "CRITICO"

        if self.trocas_estado >= TROCAS_PARA_FLAPPING:
            return "INSTAVEL"

        respostas = [h for h in self.historico if h[0]]
        if not respostas:
            return "SEM_DADOS"

        perda_pct = 100 * (1 - len(respostas) / len(self.historico))
        latencias = [h[1] for h in respostas if h[1] is not None]
        latencia_media = statistics.mean(latencias) if latencias else 0

        if perda_pct >= LIMITE_PERDA_PCT or latencia_media >= LIMITE_LATENCIA_MS:
            return "ATENCAO"

        return "OK"

    def resumo(self):
        respostas = [h for h in self.historico if h[0]]
        latencias = [h[1] for h in respostas if h[1] is not None]
        return {
            "host": self.nome,
            "status": self.ultimo_status,
            "perda_pct": round(100 * (1 - len(respostas) / len(self.historico)), 1) if self.historico else None,
            "latencia_media_ms": round(statistics.mean(latencias), 1) if latencias else None,
            "trocas_estado": self.trocas_estado,
        }


CORES = {
    "OK": "\033[92m",
    "ATENCAO": "\033[93m",
    "CRITICO": "\033[91m",
    "INSTAVEL": "\033[95m",
    "SEM_DADOS": "\033[90m",
}
RESET = "\033[0m"


def imprimir_status(resumo):
    cor = CORES.get(resumo["status"], "")
    latencia = f"{resumo['latencia_media_ms']}ms" if resumo["latencia_media_ms"] is not None else "N/A"
    print(
        f"{cor}[{resumo['status']:>9}]{RESET} {resumo['host']:<20} "
        f"perda={resumo['perda_pct']}% latencia_media={latencia} "
        f"trocas={resumo['trocas_estado']}"
    )


def carregar_hosts(caminho):
    with open(caminho, encoding="utf-8") as f:
        return [linha.strip() for linha in f if linha.strip() and not linha.startswith("#")]


def salvar_csv(caminho, linhas_resumo):
    existe = Path(caminho).exists()
    with open(caminho, "a", newline="", encoding="utf-8") as f:
        campos = ["timestamp", "host", "status", "perda_pct", "latencia_media_ms", "trocas_estado"]
        writer = csv.DictWriter(f, fieldnames=campos)
        if not existe:
            writer.writeheader()
        agora = datetime.now().isoformat(timespec="seconds")
        for resumo in linhas_resumo:
            writer.writerow({"timestamp": agora, **resumo})


def rodar_ciclo(hosts, estados, log_csv=None):
    resumos = []
    for host in hosts:
        respondeu, latencia = ping_host(host)
        status = estados[host].registrar(respondeu, latencia)
        resumo = estados[host].resumo()
        imprimir_status(resumo)
        resumos.append(resumo)

    if log_csv:
        salvar_csv(log_csv, resumos)

    criticos = [r["host"] for r in resumos if r["status"] == "CRITICO"]
    instaveis = [r["host"] for r in resumos if r["status"] == "INSTAVEL"]
    if criticos:
        print(f"\n>> ALERTA: host(s) fora do ar -> {', '.join(criticos)}")
    if instaveis:
        print(f">> ALERTA: host(s) instavel(is)/flapping -> {', '.join(instaveis)}")

    return resumos


def main():
    parser = argparse.ArgumentParser(description="Monitor de saude de rede via ping")
    parser.add_argument("--hosts", required=True, help="arquivo com um host/IP por linha")
    parser.add_argument("--intervalo", type=int, default=30, help="segundos entre ciclos (com --loop)")
    parser.add_argument("--loop", action="store_true", help="roda continuamente ate Ctrl+C")
    parser.add_argument("--log", default="historico_monitor.csv", help="arquivo CSV de saida")
    args = parser.parse_args()

    hosts = carregar_hosts(args.hosts)
    if not hosts:
        print("nenhum host encontrado no arquivo informado", file=sys.stderr)
        sys.exit(1)

    estados = {host: EstadoHost(host) for host in hosts}

    print(f"monitorando {len(hosts)} host(s) | ctrl+c para sair\n")

    try:
        while True:
            print(f"--- {datetime.now().strftime('%d/%m %H:%M:%S')} ---")
            rodar_ciclo(hosts, estados, log_csv=args.log)
            if not args.loop:
                break
            print()
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print("\nencerrado pelo usuario")


if __name__ == "__main__":
    main()
