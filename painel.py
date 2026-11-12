"""PAINEL do robô de protocolo SEI - escolhe a etapa e comanda as etapas do robô.

Uso: python painel.py   (abre http://127.0.0.1:8780 no navegador)

Tudo que antes era comando no terminal vira botão:
  - escolher a etapa (lida da exportação do Smartsheet) e gerar/atualizar a fila dela
  - abrir o navegador do robô (para os logins)
  - preparar N clientes / reprocessar os VERIFICAR
  - abrir a tela de conferência do RG/CPF
  - protocolar o próximo, ou todos (sempre um por vez, cada um com a sua assinatura)
  - modo apresentação e demonstração completa
Só roda uma tarefa por vez. O andamento aparece na própria página.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from comum import RESUMO, ajustar_a_esta_maquina, definir_etapa, etapa_ativa, ler_controle
from config import AQUI, CONGELADO, CONTROLE, DECISAO_PDF, SMARTSHEET

PORTA = 8780
PORTA_CONFERENCIA = 8765
ARQ_SMARTSHEET = AQUI / "smartsheet.txt"   # caminho da exportação escolhida no painel
ARQ_FINALIZADAS = AQUI / "etapas_finalizadas.txt"   # etapas que não aparecem mais no painel (uma por linha)


def etapas_finalizadas() -> set[str]:
    try:
        return {l.strip() for l in ARQ_FINALIZADAS.read_text(encoding="utf-8").splitlines() if l.strip()}
    except OSError:
        return set()
ORDEM = ["PENDENTE", "PREPARADO", "VERIFICAR", "CONFERIDO", "EM ANDAMENTO", "PROTOCOLADO",
         "JÁ NO NOME", "COM DÉBITO", "PULAR", "SEM ANEXO", "ERRO"]


# ---------------------------------------------------------------- smartsheet / etapas
def caminho_smartsheet() -> Path:
    try:
        p = Path(ARQ_SMARTSHEET.read_text(encoding="utf-8").strip())
        if p.exists():
            return p
    except OSError:
        pass
    return SMARTSHEET


_cache = {"chave": None, "etapas": []}


def etapas_smartsheet():
    """[(etapa, total_trocar_santa_helena)] da exportação do Smartsheet (com cache por data do arquivo)."""
    p = caminho_smartsheet()
    if not p.exists():
        return []
    chave = (str(p), p.stat().st_mtime)
    if _cache["chave"] != chave:
        df = pd.read_excel(p, usecols=["Etapa", "SITUAÇÃO", "TITULARIDADE NA PREFEITURA"])
        sel = df[df["SITUAÇÃO"].eq("TROCAR TITULAR P/ ATUAL COMPRADOR")
                 & df["TITULARIDADE NA PREFEITURA"].astype(str).str.contains("Natela Empreendimentos", case=False)]
        cont = sel["Etapa"].value_counts()
        nomes = sorted((e for e in df["Etapa"].dropna().unique() if "Etapa" in str(e)),
                       key=lambda e: (len(str(e)), str(e)))
        _cache.update(chave=chave, etapas=[(str(e), int(cont.get(e, 0))) for e in nomes])
    fim = etapas_finalizadas()
    return [(e, c) for e, c in _cache["etapas"] if e not in fim]


def estado():
    garantir_conferencia()            # religa a conferência se ela caiu (a página consulta a cada 2s)
    etapa = etapa_ativa()
    contagem, na_fila, em_andamento = {}, False, 0
    if CONTROLE.exists():
        df = ler_controle()
        sub = df[df["ETAPA"].eq(etapa)] if etapa else df
        na_fila = bool(len(sub))
        contagem = sub["STATUS ROBÔ"].value_counts().to_dict()
        em_andamento = int(df["STATUS ROBÔ"].eq("EM ANDAMENTO").sum())   # de qualquer etapa
    return {
        "etapa": etapa,
        "na_fila": na_fila,
        "em_andamento": em_andamento,
        "contagem": {k: int(contagem.get(k, 0)) for k in ORDEM},
        "smartsheet": str(caminho_smartsheet()),
        "smartsheet_ok": caminho_smartsheet().exists(),
        "decisao_ok": DECISAO_PDF.exists(),
        "navegador_ok": porta_aberta(9222),
        "conferencia_ok": porta_aberta(PORTA_CONFERENCIA),
        "tarefa": TAREFA.resumo(),
    }


def comando(script: str, *args) -> list[str]:
    """Linha de comando para rodar uma parte do robô (no .exe ou no Python)."""
    if CONGELADO:
        return [sys.executable, script.removesuffix(".py"), *args]
    python = Path(sys.executable).with_name("python.exe")        # mesmo se o painel rodar com pythonw
    return [str(python if python.exists() else sys.executable), "-u", "-W", "ignore", script, *args]


def porta_aberta(porta: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", porta)) == 0


# ---------------------------------------------------------------- tarefas
class Tarefa:
    """Uma tarefa por vez; guarda as linhas de saída para mostrar no painel."""

    def __init__(self):
        self.nome, self.linhas, self.proc, self.rodando, self.parar = "", [], None, False, False
        self.trava = threading.Lock()

    def resumo(self):
        return {"nome": self.nome, "rodando": self.rodando, "linhas": self.linhas[-400:]}

    def _rodar_script(self, args) -> str:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(comando(*args), cwd=AQUI, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     creationflags=0x08000000 if os.name == "nt" else 0)   # sem janela
        saida = []
        for linha in self.proc.stdout:
            linha = linha.rstrip()
            if "começando em" in linha:
                continue
            saida.append(linha)
            self.linhas.append(linha)
        self.proc.wait()
        return "\n".join(saida)

    def iniciar(self, nome, passos):
        """passos: função que recebe a tarefa e roda os scripts."""
        with self.trava:
            if self.rodando:
                return False
            self.nome, self.linhas, self.rodando, self.parar = nome, [f">>> {nome}"], True, False

        def corpo():
            try:
                passos(self)
            except Exception as e:
                self.linhas.append(f"ERRO no painel: {e}")
            finally:
                self.linhas.append(">>> fim")
                self.rodando, self.proc = False, None
        threading.Thread(target=corpo, daemon=True).start()
        return True

    def interromper(self):
        self.parar = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.linhas.append(">>> tarefa interrompida pelo painel")


TAREFA = Tarefa()


def protocolar_todos(t: Tarefa, apresentacao=False):
    """Protocola os CONFERIDO da etapa, um por execução, até acabar ou dar erro.
    Assinatura que não veio no tempo NÃO para a fila: o cliente fica EM ANDAMENTO, a próxima execução
    confere nos Recibos do SEI (se foi assinado, só registra o nº) e segue com os próximos.
    Duas seguidas sem assinatura = ninguém assinando -> para."""
    sem_assinatura = 0
    while not t.parar:
        df = ler_controle()
        e = etapa_ativa()
        restantes = (df["STATUS ROBÔ"].eq("CONFERIDO") & (df["ETAPA"].eq(e) if e else True))             | df["STATUS ROBÔ"].eq("EM ANDAMENTO")
        if not restantes.any():
            t.linhas.append(">>> Não há mais clientes CONFERIDOS nesta etapa.")
            return
        t.linhas.append(f">>> Faltam {int(restantes.sum())} conferido(s).")
        saida = t._rodar_script(["protocolar.py"] + (["--apresentacao"] if apresentacao else []))
        if "SEM ASSINATURA NO TEMPO" in saida:
            sem_assinatura += 1
            if sem_assinatura >= 2:
                t.linhas.append(">>> Parei: duas assinaturas seguidas não vieram. Clique em Protocolar quando "
                                "voltar (o robô confere nos Recibos antes de seguir).")
                return
            t.linhas.append(">>> Sigo: antes do próximo, o robô confere nos Recibos se este foi assinado depois.")
            continue
        sem_assinatura = 0
        if "PROTOCOLADO:" not in saida and "= JÁ NO NOME" not in saida and "= COM DÉBITO" not in saida:
            t.linhas.append(">>> Parei: o último cliente não foi protocolado (veja a mensagem acima).")
            return


def abrir_navegador():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        exe = p.chromium.executable_path
    perfil = AQUI / "perfil_navegador"
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED + NEW_PROCESS_GROUP
    subprocess.Popen([exe, f"--user-data-dir={perfil}", "--remote-debugging-port=9222", "--no-first-run",
                      "--no-default-browser-check", "--start-maximized",
                      "http://loteamento.exemplo.com.br/#/index",   # o SIG o robô abre sozinho quando precisa
                      "https://sei.prefeitura-exemplo.gov.br/sei/controlador_externo.php"
                      "?acao=usuario_externo_logar&id_orgao_acesso_externo=0"],
                     creationflags=flags, close_fds=True)


_ultima_partida = {"t": 0.0}


def garantir_conferencia() -> None:
    """A tela de conferência fica SEMPRE no ar (sem abrir janela): se cair ou o PC reiniciar, o painel religa."""
    import time
    if porta_aberta(PORTA_CONFERENCIA) or time.time() - _ultima_partida["t"] < 20:
        return
    _ultima_partida["t"] = time.time()
    flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0   # separado, sem janela
    subprocess.Popen(comando("conferencia.py", "--sem-abrir"), cwd=AQUI,
                     env=dict(os.environ, PYTHONIOENCODING="utf-8"), creationflags=flags)


def abrir_conferencia():
    import time
    garantir_conferencia()
    for _ in range(40):                 # espera subir (até ~20s) e abre a página
        if porta_aberta(PORTA_CONFERENCIA):
            break
        time.sleep(0.5)
    webbrowser.open(f"http://127.0.0.1:{PORTA_CONFERENCIA}")


# ---------------------------------------------------------------- ações
def acao(nome, dados):
    e = etapa_ativa()
    if nome == "etapa":
        e = dados.get("etapa", "").strip()
        if not e:
            raise ValueError("Escolha uma etapa na lista.")
        if e in etapas_finalizadas():
            raise ValueError(f"A {e} está marcada como finalizada.")
        definir_etapa(e)
        ok = TAREFA.iniciar(f"Montar fila - {e}",
                            lambda t: t._rodar_script(["gerar_fila.py", str(caminho_smartsheet()), e]))
        return f"Etapa escolhida: {e}." + (" Montando a fila desta etapa..." if ok else
               " (A fila será atualizada no próximo Preparar - há uma tarefa rodando.)")
    if nome == "encerrar":
        TAREFA.interromper()
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return "Painel encerrado. Para abrir de novo: dois cliques em ABRIR PAINEL."
    if nome == "smartsheet":
        p = Path(dados.get("caminho", "").strip().strip('"'))
        if not p.exists():
            raise ValueError(f"Arquivo não encontrado: {p}")
        ARQ_SMARTSHEET.write_text(str(p), encoding="utf-8")
        return "Exportação do Smartsheet atualizada."
    if nome == "navegador":
        if porta_aberta(9222):
            return "O navegador do robô já está aberto."
        abrir_navegador()
        return "Navegador do robô aberto. Faça login no Loteamento e no SEI."
    if nome == "conferencia":
        abrir_conferencia()
        n = sum(estado()["contagem"][k] for k in ("PREPARADO", "VERIFICAR"))
        return ("Tela de conferência aberta (http://127.0.0.1:8765)." if n else
                "Conferência aberta, mas não há clientes para conferir nesta etapa (rode Preparar antes).")
    if nome == "parar":
        TAREFA.interromper()
        return "Pedido de parada enviado."
    if nome == "abrir_pasta":
        os.startfile(RESUMO.parent) if os.name == "nt" else None
        return "Pasta aberta."
    if nome == "abrir_debitos":
        from debitos import PLANILHA
        if not PLANILHA.exists():
            raise ValueError("Ainda não há planilha de débitos. Clique em 'Verificar débitos' antes.")
        os.startfile(PLANILHA)
        return "Planilha de débitos aberta."
    if nome == "debitos_todas":
        ok = TAREFA.iniciar("Débitos do IPTU - todas as etapas",
                            lambda t: t._rodar_script(["debitos.py", str(caminho_smartsheet()), "TODAS"]))
        if not ok:
            raise ValueError("Já existe uma tarefa rodando. Espere terminar ou clique em Parar.")
        return "Tarefa iniciada."

    if not e:
        raise ValueError("Escolha uma etapa na lista e clique em 'Usar esta etapa'.")
    rotulo = e
    atualizar_fila = ["gerar_fila.py", str(caminho_smartsheet()), e]
    if nome == "gerar_fila":
        ok = TAREFA.iniciar(f"Atualizar do Smartsheet - {rotulo}", lambda t: t._rodar_script(atualizar_fila))
    elif nome in ("protocolar_um", "protocolar_todos", "apresentacao") and not (estado()["contagem"]["CONFERIDO"] or estado()["em_andamento"]):
        raise ValueError("Não há clientes CONFERIDOS nesta etapa para protocolar.")
    elif nome == "preparar":
        n = str(max(1, int(dados.get("n", 20))))

        def preparar(t):
            t.linhas.append(">>> Atualizando a fila a partir do Smartsheet...")
            t._rodar_script(atualizar_fila)
            if not t.parar:
                t._rodar_script(["preparar.py", n])
        ok = TAREFA.iniciar(f"Preparar {n} - {rotulo}", preparar)
    elif nome == "debitos":
        ok = TAREFA.iniciar(f"Débitos do IPTU - {rotulo}",
                            lambda t: t._rodar_script(["debitos.py", str(caminho_smartsheet()), e]))
    elif nome == "reprocessar":
        ok = TAREFA.iniciar(f"Reprocessar VERIFICAR - {rotulo}", lambda t: t._rodar_script(["preparar.py", "--reprocessar"]))
    elif nome == "protocolar_um":
        ok = TAREFA.iniciar(f"Protocolar o próximo - {rotulo}", lambda t: t._rodar_script(["protocolar.py"]))
    elif nome == "protocolar_todos":
        ok = TAREFA.iniciar(f"Protocolar todos (um por vez) - {rotulo}", protocolar_todos)
    elif nome == "apresentacao":
        ok = TAREFA.iniciar(f"Protocolar (modo apresentação) - {rotulo}",
                            lambda t: t._rodar_script(["protocolar.py", "--apresentacao"]))
    elif nome == "demonstracao":
        ok = TAREFA.iniciar(f"Demonstração completa - {rotulo}", lambda t: t._rodar_script(["demonstracao.py"]))
    else:
        raise ValueError("Ação desconhecida")
    if not ok:
        raise ValueError("Já existe uma tarefa rodando. Espere terminar ou clique em Parar.")
    return "Tarefa iniciada."


# ---------------------------------------------------------------- página
def pagina():
    opcoes = "".join(f'<option value="{escape(n)}">{escape(n)} — {c} para trocar titular</option>'
                     for n, c in etapas_smartsheet())
    total = sum(c for _, c in etapas_smartsheet())
    return PAGINA.replace("__OPCOES__", opcoes).replace("__TOTAL__", str(total))


PAGINA = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Painel - Robô Protocolo SEI</title>
<style>
:root{--bg:#f3f5f8;--card:#fff;--txt:#18212e;--sub:#5b6475;--pri:#1f4e78;--ok:#1a7f37;--warn:#9a5b00;--bad:#b3261e;--line:#dde2ea}
*{box-sizing:border-box} body{margin:0;font:15px "Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--txt)}
header{background:var(--pri);color:#fff;padding:14px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header h1{font-size:19px;margin:0} header .sinais{margin-left:auto;display:flex;gap:14px;font-size:13px}
.sinal::before{content:"●";margin-right:5px;color:#e57373} .sinal.ok::before{color:#7bd389}
main{max-width:1150px;margin:0 auto;padding:18px 16px;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:18px}
@media(max-width:900px){main{grid-template-columns:minmax(0,1fr)}}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
h2{font-size:14px;letter-spacing:.06em;text-transform:uppercase;color:var(--sub);margin:0 0 12px}
select,input{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--txt)}
select{width:100%} .linha{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
button{font:inherit;padding:9px 14px;border-radius:7px;border:1px solid var(--line);background:#fff;cursor:pointer}
button:hover{border-color:var(--pri)} button.pri{background:var(--pri);color:#fff;border-color:var(--pri)}
button.ok{background:var(--ok);color:#fff;border-color:var(--ok)} button.bad{color:var(--bad);border-color:var(--bad)}
button:disabled{opacity:.45;cursor:not-allowed}
.nums{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.num{border:1px solid var(--line);border-radius:8px;padding:8px 10px} .num b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.num span{font-size:12px;color:var(--sub)} .num.ok b{color:var(--ok)} .num.warn b{color:var(--warn)} .num.bad b{color:var(--bad)}
.passos{display:grid;gap:10px} .passo{display:grid;grid-template-columns:28px minmax(0,1fr);gap:10px;align-items:start}
.passo .n{width:28px;height:28px;border-radius:50%;background:var(--pri);color:#fff;font-weight:700;display:grid;place-items:center}
.passo p{margin:2px 0 6px;color:var(--sub);font-size:13.5px}
#log{background:#10161e;color:#d7e2ee;font:12.5px/1.5 Consolas,monospace;border-radius:8px;padding:12px;height:420px;overflow:auto;white-space:pre-wrap}
#msg{min-height:22px;margin-top:8px;font-size:14px} #msg.err{color:var(--bad)} #msg.ok{color:var(--ok)}
.aviso{background:#fff4dc;border-radius:6px;padding:8px 10px;font-size:13.5px;margin-top:10px}
small{color:var(--sub)}
.proximo{margin-top:12px;padding:10px 12px;border-radius:8px;background:#e7eef6;font-size:14.5px}
.proximo b{color:var(--pri)}
</style></head><body>
<header><h1>Robô de Protocolo SEI · Painel</h1>
<div class="sinais"><span class="sinal" id="s-nav">Navegador do robô</span><span class="sinal" id="s-conf">Conferência</span><span class="sinal" id="s-dec">Decisão judicial</span></div></header>
<main>
<div style="display:grid;gap:18px;min-width:0">
<section><h2>Etapa</h2>
  <select id="etapa" onchange="mexeu=true"><option value="" disabled>— escolha a etapa —</option>__OPCOES__</select>
  <div class="linha"><button class="pri" onclick="escolher()">Usar esta etapa</button>
  <button onclick="acao('gerar_fila')">Atualizar do Smartsheet</button>
  <button class="bad" style="margin-left:auto" onclick="if(confirm_enc())acao('encerrar')">Encerrar painel</button></div>
  <small>O robô trabalha só com a etapa escolhida. Ao escolher, a fila dela é montada sozinha a partir do Smartsheet; antes de cada Preparar ela é atualizada.</small>
  <div id="semfila" class="aviso" hidden>Esta etapa ainda não tem fila. Clique em <b>Usar esta etapa</b> que ela é montada sozinha.</div>
  <div class="linha"><small>Smartsheet: <span id="ss"></span></small></div>
  <div class="linha"><input id="sscam" placeholder="Outro arquivo exportado do Smartsheet (caminho completo)" style="flex:1;min-width:0">
  <button onclick="acao('smartsheet',{caminho:document.getElementById('sscam').value})">Trocar</button></div>
</section>
<section><h2>Situação da etapa <span id="etapa-nome"></span></h2><div class="nums" id="nums"></div>
  <div id="proximo" class="proximo"></div></section>
<section><h2>Passo a passo</h2><div class="passos">
  <div class="passo"><span class="n">1</span><div><b>Navegador e logins</b><p>Abra o navegador do robô e faça login no Loteamento e no SEI.</p>
    <button onclick="acao('navegador')">Abrir navegador do robô</button></div></div>
  <div class="passo"><span class="n">2</span><div><b>Preparar</b><p>Baixa contrato e DOC, confere CPF, venda, lote e IPTU. Não protocola nada.</p>
    <div class="linha" style="margin-top:0"><input id="n" type="number" value="20" min="1" style="width:80px">
    <button onclick="acao('preparar',{n:document.getElementById('n').value})">Preparar</button>
    <button onclick="acao('reprocessar')">Reprocessar VERIFICAR</button></div></div></div>
  <div class="passo"><span class="n">3</span><div><b>Conferência do RG/CPF</b><p>Mostra todas as páginas do DOC para você marcar as de RG/CPF.</p>
    <button onclick="acao('conferencia')">Abrir conferência</button></div></div>
  <div class="passo"><span class="n">4</span><div><b>Protocolar</b><p>Um cliente por vez. O robô para na assinatura e você assina com a sua senha.</p>
    <div class="linha" style="margin-top:0"><button class="ok" onclick="acao('protocolar_todos')">Protocolar todos (um por vez)</button>
    <button onclick="acao('protocolar_um')">Só o próximo</button>
    <button onclick="acao('apresentacao')">Modo apresentação</button></div></div></div>
  <div class="passo"><span class="n">5</span><div><b>Resultado</b><p>PROTOCOLOS REALIZADOS.xlsx (nome, CPF, quadra, lote, protocolo) para atualizar o Smartsheet.</p>
    <button onclick="acao('abrir_pasta')">Abrir pasta dos protocolos</button>
    <button onclick="acao('demonstracao')">Demonstração completa (1 cliente)</button></div></div>
</div></section>
<section><h2>Débitos do IPTU (antes de transferir)</h2>
  <p style="margin:0 0 10px;color:var(--sub);font-size:13.5px">Consulta no SIG os débitos de cada CCI (Trocar titular e Voltar nome da loteadora)
  e, quando o titular é um antigo comprador, confere no Loteamento se houve distrato. Só consulta, não altera nada.
  Gera <b>DEBITOS IPTU.xlsx</b> com tabela dinâmica.</p>
  <div class="linha" style="margin-top:0"><button class="pri" onclick="acao('debitos')">Verificar débitos da etapa</button>
  <button onclick="acao('debitos_todas')">Todas as etapas</button>
  <button onclick="acao('abrir_debitos')">Abrir planilha de débitos</button></div>
</section>
</div>
<section style="min-width:0"><h2>Andamento <span id="tnome"></span></h2>
  <div id="log"></div>
  <div class="linha"><button class="bad" onclick="acao('parar')">Parar tarefa</button><span id="msg"></span></div>
</section>
</main>
<script>
const ROT={PENDENTE:"Pendentes",PREPARADO:"Preparados",VERIFICAR:"Verificar",CONFERIDO:"Conferidos","EM ANDAMENTO":"Em andamento",PROTOCOLADO:"Protocolados",
"JÁ NO NOME":"Já no nome","COM DÉBITO":"Com débito vencido",PULAR:"Pulados","SEM ANEXO":"Sem anexo",ERRO:"Erro"};
const COR={PROTOCOLADO:"ok","JÁ NO NOME":"ok",VERIFICAR:"warn","EM ANDAMENTO":"bad","COM DÉBITO":"bad","SEM ANEXO":"warn",ERRO:"bad"};
let ultimo=0, mexeu=false;
function msg(t,ok){const m=document.getElementById('msg');m.textContent=t;m.className=ok?'ok':'err'}
async function acao(nome,dados={}){
  const r=await fetch('/acao',{method:'POST',body:JSON.stringify({nome,dados})});const j=await r.json();
  msg(j.ok?j.msg:j.erro,j.ok);await atualizar();return j}
function escolher(){const v=document.getElementById('etapa').value;
  if(!v){msg('Escolha uma etapa na lista.',false);return}
  acao('etapa',{etapa:v}).then(()=>{mexeu=false})}
let enc=0;function confirm_enc(){enc++;if(enc<2){msg('Clique de novo em "Encerrar painel" para confirmar.',false);setTimeout(()=>enc=0,4000);return false}return true}
async function atualizar(){
  const j=await (await fetch('/estado')).json();
  if(!mexeu) document.getElementById('etapa').value=j.etapa||'';
  document.getElementById('etapa-nome').textContent=j.etapa?'· '+j.etapa:'· nenhuma etapa escolhida';
  document.getElementById('semfila').hidden=!j.etapa||j.na_fila;
  document.getElementById('ss').textContent=(j.smartsheet_ok?'':'⚠ não encontrado: ')+j.smartsheet;
  document.getElementById('nums').innerHTML=Object.entries(j.contagem).map(([k,v])=>
    `<div class="num ${COR[k]||''}"><b>${v}</b><span>${ROT[k]}</span></div>`).join('');
  [['s-nav',j.navegador_ok],['s-conf',j.conferencia_ok],['s-dec',j.decisao_ok]].forEach(([id,ok])=>
    document.getElementById(id).classList.toggle('ok',ok));
  const c=j.contagem;let px='';
  if(!j.etapa) px='Escolha uma <b>etapa</b> na lista e clique em <b>Usar esta etapa</b>.';
  else if(!j.na_fila) px='Clique em <b>Usar esta etapa</b> para montar a fila desta etapa a partir do Smartsheet.';
  else if(!j.navegador_ok) px='Abra o <b>navegador do robô</b> (passo 1) e faça os logins.';
  else if(j.em_andamento>0) px='Um protocolo foi <b>interrompido</b> depois de Peticionar. Clique em <b>Só o próximo</b>: o robô confere nos Recibos do SEI se ele foi assinado (não protocola de novo).';
  else if(c.PREPARADO+c.VERIFICAR>0) px=`Próximo passo: <b>Abrir conferência</b> (${c.PREPARADO+c.VERIFICAR} para conferir).`;
  else if(c.CONFERIDO>0) px=`Próximo passo: <b>Protocolar todos (um por vez)</b> — ${c.CONFERIDO} conferido(s) aguardando.`;
  else if(c.PENDENTE>0) px=`Próximo passo: <b>Preparar</b> — ${c.PENDENTE} pendente(s).`;
  else if(c.ERRO>0) px=`Há ${c.ERRO} cliente(s) com <b>ERRO</b> — veja a coluna OBSERVAÇÃO na planilha de controle.`;
  else px='<b>Etapa concluída.</b> Escolha outra etapa.';
  document.getElementById('proximo').innerHTML=px;
  const t=j.tarefa;document.getElementById('tnome').textContent=t.nome?('· '+t.nome+(t.rodando?' (rodando)':'')):'';
  const log=document.getElementById('log');
  if(t.linhas.length!==ultimo){log.textContent=t.linhas.join('\\n');log.scrollTop=log.scrollHeight;ultimo=t.linhas.length}
}
atualizar();setInterval(atualizar,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, pagina().encode(), "text/html; charset=utf-8")
        if u.path == "/estado":
            return self._send(200, json.dumps(estado()).encode(), "application/json")
        self._send(404, b"", "text/plain")

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = {"ok": True, "msg": acao(d["nome"], d.get("dados") or {})}
        except Exception as e:
            out = {"ok": False, "erro": str(e)}
        self._send(200, json.dumps(out).encode(), "application/json")


if __name__ == "__main__":
    if porta_aberta(PORTA):            # já está rodando: só abre a página
        webbrowser.open(f"http://127.0.0.1:{PORTA}")
        sys.exit(0)
    try:
        print(ajustar_a_esta_maquina())
    except Exception as e:             # nunca impede o painel de abrir
        print("Aviso ao ajustar a planilha:", e)
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), Handler)
    garantir_conferencia()
    url = f"http://127.0.0.1:{PORTA}"
    print(f"Painel em {url}  (deixe esta janela aberta; feche com Ctrl+C)")
    webbrowser.open(url)
    srv.serve_forever()
