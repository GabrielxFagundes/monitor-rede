#!/usr/bin/env python3
"""
Monitor de saude de rede - ping continuo com deteccao de degradacao,
agora com suporte a topologia SD-WAN (multiplos links por site).

Modo classico (lista simples de hosts, sem SD-WAN):
    python monitor_rede.py --hosts hosts.txt
    python monitor_rede.py --hosts hosts.txt --intervalo 30 --loop

Modo SD-WAN (sites com multiplos links/circuitos: MPLS, INTERNET, LTE...):
    python monitor_rede.py --topologia topologia.json
    python monitor_rede.py --topologia topologia.json --intervalo 30 --loop

No modo SD-WAN cada link e testado individualmente (status, latencia, jitter,
perda de pacote) e, se configurado com SNMP, tem o uso de banda medido.
O status do SITE leva em conta que SD-WAN faz failover: o site so fica
critico se TODOS os links caírem; se sobrar pelo menos um link bom, o site
fica "operando sem redundancia" (DEGRADADO), nao caido.
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

# limites default - da pra sobrescrever no JSON de topologia, por link
LIMITE_LATENCIA_MS = 100
LIMITE_JITTER_MS = 30
LIMITE_PERDA_PCT = 20
JANELA_HISTORICO = 10
FALHAS_PARA_CRITICO = 3
TROCAS_PARA_FLAPPING = 4

OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"

_AVISO_SNMP_EXIBIDO = False


# ---------------------------------------------------------------------------
# Ping (ICMP) - reachability, latencia, perda, jitter
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Banda via SNMP (opcional - so roda se o link tiver bloco "snmp" configurado)
# ---------------------------------------------------------------------------

def consultar_snmp_contador(agente, community, if_index, oid_base, timeout=3):
    """Le um contador de interface (ifHCInOctets/ifHCOutOctets) via snmpget.

    Requer o pacote net-snmp instalado no sistema (comando `snmpget`).
    Devolve None se o snmpget nao estiver disponivel ou a consulta falhar -
    o monitoramento de ping continua funcionando normalmente sem banda.
    """
    global _AVISO_SNMP_EXIBIDO
    oid = f"{oid_base}.{if_index}"
    cmd = ["snmpget", "-v2c", "-c", community, "-Oqv", "-t", "2", agente, oid]
    try:
        saida = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        if not _AVISO_SNMP_EXIBIDO:
            print(
                "[aviso] comando 'snmpget' nao encontrado - instale o pacote "
                "net-snmp (ex: apt install snmp) para medir uso de banda. "
                "O monitoramento de ping/latencia/jitter continua normalmente.",
                file=sys.stderr,
            )
            _AVISO_SNMP_EXIBIDO = True
        return None
    except subprocess.TimeoutExpired:
        return None
    if saida.returncode != 0:
        return None
    try:
        return int(saida.stdout.strip())
    except ValueError:
        return None


def medir_banda(link_cfg, estado, agora):
    """Calcula Mbps de entrada/saida a partir da diferenca entre duas leituras
    de contador SNMP (ifHCInOctets/ifHCOutOctets). Retorna (None, None) se o
    link nao tiver SNMP configurado ou ainda nao houver uma leitura anterior
    para calcular a diferenca."""
    snmp_cfg = link_cfg.get("snmp")
    if not snmp_cfg:
        return None, None

    agente = snmp_cfg["agente"]
    community = snmp_cfg.get("community", "public")
    if_index = snmp_cfg["if_index"]

    octets_in = consultar_snmp_contador(agente, community, if_index, OID_IF_HC_IN_OCTETS)
    octets_out = consultar_snmp_contador(agente, community, if_index, OID_IF_HC_OUT_OCTETS)
    if octets_in is None or octets_out is None:
        return None, None

    mbps_in = mbps_out = None
    if estado.ultima_leitura_banda is not None:
        octets_in_ant, octets_out_ant, ts_ant = estado.ultima_leitura_banda
        dt = (agora - ts_ant).total_seconds()
        if dt > 0:
            # contador pode ter zerado (reboot da interface) - nesse caso usa o valor bruto
            delta_in = octets_in - octets_in_ant if octets_in >= octets_in_ant else octets_in
            delta_out = octets_out - octets_out_ant if octets_out >= octets_out_ant else octets_out
            mbps_in = round((delta_in * 8) / dt / 1_000_000, 2)
            mbps_out = round((delta_out * 8) / dt / 1_000_000, 2)

    estado.ultima_leitura_banda = (octets_in, octets_out, agora)
    return mbps_in, mbps_out


# ---------------------------------------------------------------------------
# Estado de cada link (host monitorado)
# ---------------------------------------------------------------------------

class EstadoLink:
    """Guarda o historico recente de um link (ping + banda) e decide o status atual.

    Um "link" e um circuito WAN (MPLS, Internet, LTE...) de um site. No modo
    classico (--hosts) cada host vira um link solto, sem site associado.
    """

    def __init__(self, nome, site="-", tipo="-", capacidade_mbps=None):
        self.nome = nome
        self.site = site
        self.tipo = tipo
        self.capacidade_mbps = capacidade_mbps
        self.historico = deque(maxlen=JANELA_HISTORICO)
        self.falhas_seguidas = 0
        self.trocas_estado = 0
        self.ultimo_status = None
        self.ultima_leitura_banda = None  # (octets_in, octets_out, timestamp)
        self.ultimo_bw_in = None
        self.ultimo_bw_out = None

    def registrar_ping(self, respondeu, latencia):
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

    def registrar_banda(self, mbps_in, mbps_out):
        if mbps_in is not None:
            self.ultimo_bw_in = mbps_in
        if mbps_out is not None:
            self.ultimo_bw_out = mbps_out

    def _jitter_ms(self):
        """Jitter = variacao da latencia entre as amostras da janela (desvio padrao)."""
        latencias = [h[1] for h in self.historico if h[0] and h[1] is not None]
        if len(latencias) < 2:
            return None
        return round(statistics.pstdev(latencias), 1)

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
        jitter = self._jitter_ms() or 0
        if perda_pct >= LIMITE_PERDA_PCT or latencia_media >= LIMITE_LATENCIA_MS or jitter >= LIMITE_JITTER_MS:
            return "ATENCAO"
        return "OK"

    def resumo(self):
        respostas = [h for h in self.historico if h[0]]
        latencias = [h[1] for h in respostas if h[1] is not None]
        uso_pct = None
        maior_bw = max([v for v in (self.ultimo_bw_in, self.ultimo_bw_out) if v is not None], default=None)
        if maior_bw is not None and self.capacidade_mbps:
            uso_pct = round(100 * maior_bw / self.capacidade_mbps, 1)
        return {
            "site": self.site,
            "link": self.nome,
            "tipo": self.tipo,
            "status": self.ultimo_status,
            "perda_pct": round(100 * (1 - len(respostas) / len(self.historico)), 1) if self.historico else None,
            "latencia_media_ms": round(statistics.mean(latencias), 1) if latencias else None,
            "jitter_ms": self._jitter_ms(),
            "trocas_estado": self.trocas_estado,
            "bw_in_mbps": self.ultimo_bw_in,
            "bw_out_mbps": self.ultimo_bw_out,
            "capacidade_mbps": self.capacidade_mbps,
            "uso_pct": uso_pct,
        }


def status_site(links_do_site):
    """Status do site como um todo, considerando que SD-WAN faz failover entre links:
    - SITE_CRITICO: todos os links caidos (site isolado, sem WAN)
    - SITE_DEGRADADO: pelo menos 1 link bom, mas tem link ruim/caido (sem redundancia total)
    - SITE_OK: todos os links bons
    """
    status_links = [link.ultimo_status for link in links_do_site]
    if all(s == "CRITICO" for s in status_links):
        return "SITE_CRITICO"
    if all(s == "OK" for s in status_links):
        return "SITE_OK"
    return "SITE_DEGRADADO"


# ---------------------------------------------------------------------------
# Saida (console + CSV)
# ---------------------------------------------------------------------------

CORES = {
    "OK": "\033[92m",
    "ATENCAO": "\033[93m",
    "CRITICO": "\033[91m",
    "INSTAVEL": "\033[95m",
    "SEM_DADOS": "\033[90m",
    "SITE_OK": "\033[92m",
    "SITE_DEGRADADO": "\033[93m",
    "SITE_CRITICO": "\033[91m",
}
RESET = "\033[0m"


def imprimir_status(resumo):
    cor = CORES.get(resumo["status"], "")
    latencia = f"{resumo['latencia_media_ms']}ms" if resumo["latencia_media_ms"] is not None else "N/A"
    jitter = f"{resumo['jitter_ms']}ms" if resumo["jitter_ms"] is not None else "N/A"
    banda = ""
    if resumo["bw_in_mbps"] is not None or resumo["bw_out_mbps"] is not None:
        bw_in = f"{resumo['bw_in_mbps']}Mb/s" if resumo["bw_in_mbps"] is not None else "N/A"
        bw_out = f"{resumo['bw_out_mbps']}Mb/s" if resumo["bw_out_mbps"] is not None else "N/A"
        uso = f" ({resumo['uso_pct']}% da capacidade)" if resumo["uso_pct"] is not None else ""
        banda = f" banda_in={bw_in} banda_out={bw_out}{uso}"
    prefixo = f"{resumo['site']:<18} " if resumo["site"] != "-" else ""
    print(
        f"{cor}[{resumo['status']:>9}]{RESET} {prefixo}{resumo['link']:<18} ({resumo['tipo']}) "
        f"perda={resumo['perda_pct']}% latencia={latencia} jitter={jitter} "
        f"trocas={resumo['trocas_estado']}{banda}"
    )


def imprimir_status_site(nome_site, status):
    cor = CORES.get(status, "")
    print(f"{cor}>> {nome_site}: {status}{RESET}")


def carregar_hosts(caminho):
    with open(caminho, encoding="utf-8") as f:
        return [linha.strip() for linha in f if linha.strip() and not linha.startswith("#")]


def carregar_topologia(caminho):
    """Le o JSON de topologia SD-WAN e devolve lista de (site, [EstadoLink, link_cfg])."""
    with open(caminho, encoding="utf-8") as f:
        dados = json.load(f)
    sites = []
    for site_cfg in dados.get("sites", []):
        nome_site = site_cfg["nome"]
        links = []
        for link_cfg in site_cfg.get("links", []):
            estado = EstadoLink(
                nome=link_cfg["nome"],
                site=nome_site,
                tipo=link_cfg.get("tipo", "-"),
                capacidade_mbps=link_cfg.get("capacidade_mbps"),
            )
            links.append((estado, link_cfg))
        sites.append((nome_site, links))
    return sites


def salvar_csv(caminho, linhas_resumo):
    existe = Path(caminho).exists()
    campos = [
        "timestamp", "site", "link", "tipo", "status", "perda_pct",
        "latencia_media_ms", "jitter_ms", "trocas_estado",
        "bw_in_mbps", "bw_out_mbps", "capacidade_mbps", "uso_pct",
    ]
    with open(caminho, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        if not existe:
            writer.writeheader()
        agora = datetime.now().isoformat(timespec="seconds")
        for resumo in linhas_resumo:
            writer.writerow({"timestamp": agora, **resumo})


# ---------------------------------------------------------------------------
# Ciclos de monitoramento
# ---------------------------------------------------------------------------

def rodar_ciclo_classico(hosts, estados, log_csv=None):
    """Modo antigo: lista simples de hosts, sem agrupar por site."""
    resumos = []
    for host in hosts:
        respondeu, latencia = ping_host(host)
        estados[host].registrar_ping(respondeu, latencia)
        resumo = estados[host].resumo()
        imprimir_status(resumo)
        resumos.append(resumo)
    if log_csv:
        salvar_csv(log_csv, resumos)
    _emitir_alertas(resumos)
    return resumos


def rodar_ciclo_sdwan(sites, log_csv=None):
    """Modo SD-WAN: testa ping (+ banda, se configurado) de cada link, agrupado por site."""
    resumos = []
    for nome_site, links in sites:
        links_estado = []
        for estado, link_cfg in links:
            respondeu, latencia = ping_host(link_cfg["host_ping"])
            estado.registrar_ping(respondeu, latencia)
            mbps_in, mbps_out = medir_banda(link_cfg, estado, datetime.now())
            estado.registrar_banda(mbps_in, mbps_out)
            resumo = estado.resumo()
            imprimir_status(resumo)
            resumos.append(resumo)
            links_estado.append(estado)
        imprimir_status_site(nome_site, status_site(links_estado))
    if log_csv:
        salvar_csv(log_csv, resumos)
    _emitir_alertas(resumos)
    return resumos


def _emitir_alertas(resumos):
    criticos = [f"{r['site']}/{r['link']}" if r["site"] != "-" else r["link"] for r in resumos if r["status"] == "CRITICO"]
    instaveis = [f"{r['site']}/{r['link']}" if r["site"] != "-" else r["link"] for r in resumos if r["status"] == "INSTAVEL"]
    if criticos:
        print(f"\n>> ALERTA: link(s) fora do ar -> {', '.join(criticos)}")
    if instaveis:
        print(f">> ALERTA: link(s) instavel(is)/flapping -> {', '.join(instaveis)}")


def main():
    parser = argparse.ArgumentParser(description="Monitor de saude de rede via ping, com suporte a SD-WAN")
    grupo_fonte = parser.add_mutually_exclusive_group(required=True)
    grupo_fonte.add_argument("--hosts", help="arquivo com um host/IP por linha (modo classico)")
    grupo_fonte.add_argument("--topologia", help="arquivo JSON com sites e links SD-WAN (modo SD-WAN)")
    parser.add_argument("--intervalo", type=int, default=30, help="segundos entre ciclos (com --loop)")
    parser.add_argument("--loop", action="store_true", help="roda continuamente ate Ctrl+C")
    parser.add_argument("--log", default="historico_monitor.csv", help="arquivo CSV de saida")
    args = parser.parse_args()

    if args.hosts:
        hosts = carregar_hosts(args.hosts)
        if not hosts:
            print("nenhum host encontrado no arquivo informado", file=sys.stderr)
            sys.exit(1)
        estados = {host: EstadoLink(nome=host) for host in hosts}
        print(f"monitorando {len(hosts)} host(s) [modo classico] | ctrl+c para sair\n")
        rodar = lambda: rodar_ciclo_classico(hosts, estados, log_csv=args.log)
    else:
        sites = carregar_topologia(args.topologia)
        total_links = sum(len(links) for _, links in sites)
        if not sites or total_links == 0:
            print("nenhum site/link encontrado na topologia informada", file=sys.stderr)
            sys.exit(1)
        print(f"monitorando {len(sites)} site(s) / {total_links} link(s) [modo SD-WAN] | ctrl+c para sair\n")
        rodar = lambda: rodar_ciclo_sdwan(sites, log_csv=args.log)

    try:
        while True:
            print(f"--- {datetime.now().strftime('%d/%m %H:%M:%S')} ---")
            rodar()
            if not args.loop:
                break
            print()
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print("\nencerrado pelo usuario")


if __name__ == "__main__":
    main()
