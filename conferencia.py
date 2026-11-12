"""ETAPA 2 - Tela de conferência: escolher as páginas de RG/CPF de cada cliente.

Uso: python conferencia.py   (abre http://127.0.0.1:8765 no navegador)

Mostra os clientes PREPARADO e VERIFICAR. Para cada um:
  - clique nas miniaturas que são RG/CPF (a página 1 já vem marcada)
  - "Confirmar"  -> gera "RG E CPF.pdf" na pasta do cliente e marca CONFERIDO
  - "Pular"      -> marca PULAR (não será protocolado)
Atalhos: teclas 1-4 marcam/desmarcam páginas, Enter confirma.
"""
import html
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pymupdf

from comum import etapa_ativa, filtro_etapa, ler_controle, pasta_atual, salvar_linhas, trava_planilha
from config import PAGINAS_CONFERENCIA

PORTA = 8765
STATUS_TELA = ("PREPARADO", "VERIFICAR")
TRAVA = threading.Lock()  # uma gravação na planilha por vez
GERANDO = threading.Semaphore(1)  # miniaturas geradas uma de cada vez (não trava o "Confirmar")


def montar_rg(pasta: Path, paginas: list[int]) -> None:
    with pymupdf.open(pasta / "DOC_ORIGINAL.pdf") as src, pymupdf.open() as out:
        for pg in paginas:
            out.insert_pdf(src, from_page=pg - 1, to_page=pg - 1)
        out.save(pasta / "RG E CPF.pdf", garbage=3, deflate=True)


def paginas_doc(pasta: Path) -> int:
    try:
        with pymupdf.open(pasta / "DOC_ORIGINAL.pdf") as d:
            return len(d)
    except Exception:
        return 0


def miniatura(pasta: Path, n: int) -> Path | None:
    """Miniatura da página n do DOC; gera na hora se ainda não existir."""
    f = pasta / "miniaturas" / f"pag{n}.png"
    if not f.exists():
        with GERANDO:
            return _gerar(pasta, n, f)
    return f


def _gerar(pasta: Path, n: int, f: Path) -> Path | None:
    if not f.exists():
        try:
            with pymupdf.open(pasta / "DOC_ORIGINAL.pdf") as d:
                if not 1 <= n <= len(d):
                    return None
                f.parent.mkdir(exist_ok=True)
                d[n - 1].get_pixmap(dpi=60).save(f)
        except Exception:
            return None
    return f


def pagina_html(somente: int | None = None) -> str:
    df = ler_controle()
    fila = df[df["STATUS ROBÔ"].isin(STATUS_TELA) & filtro_etapa(df)]
    if somente is not None:  # demonstração: só um cliente
        fila = fila[fila.index == somente]
    feitos = df["STATUS ROBÔ"].isin(["CONFERIDO", "PULAR", "PROTOCOLADO"]).sum()
    cards = []
    for i, r in fila.iterrows():
        pasta = pasta_atual(r)
        total = paginas_doc(pasta)
        thumbs = "".join(
            f'<figure data-p="{n}" class="{"on" if n == 1 else ""}"><img loading="lazy" src="/img?i={i}&p={n}">'
            f"<figcaption>Página {n}</figcaption></figure>"
            for n in range(1, total + 1)
        )
        aviso = (f'<div class="aviso">⚠ {html.escape(r["OBSERVAÇÃO ROBÔ"])}</div>'
                 if r["STATUS ROBÔ"] == "VERIFICAR" else "")
        cards.append(f"""
<section class="card" data-i="{i}">
  <header><b>{html.escape(r['COMPRADOR'])}</b>
    <span>QD {r['QD']} · LT {r['LT']} · CCI {r['CCI']} · Venda {r['Nº VENDA']}</span>
    <a href="/pdf?i={i}&f=DOC_ORIGINAL.pdf" target="_blank">abrir DOC</a>
    <a href="/pdf?i={i}&f=CONTRATO.pdf" target="_blank">abrir contrato</a></header>
  {aviso}
  <div class="thumbs">{thumbs}</div>
  <footer><button class="ok">Confirmar páginas</button><button class="pular">Pular cliente</button></footer>
</section>""")
    vazio = ('<p class="vazio">Nada para conferir na etapa <b>' + html.escape(etapa_ativa() or "todas") +
             '</b>. Os clientes desta etapa já foram conferidos, ou ainda falta clicar em <b>Preparar</b> no painel.</p>')
    return f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Conferência RG/CPF</title>
<style>
:root{{--bg:#f4f5f7;--card:#fff;--txt:#1d2433;--sub:#5b6475;--pri:#1f4e78;--ok:#1a7f37;--warn:#fff4ce;--sel:#2f81f7}}
body{{margin:0;font:14px system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--txt)}}
.top{{position:sticky;top:0;background:var(--pri);color:#fff;padding:12px 16px;z-index:2;display:flex;gap:16px;align-items:center}}
.top b{{font-size:16px}} main{{max-width:1200px;margin:0 auto;padding:16px}}
.card{{background:var(--card);border-radius:10px;padding:14px 16px;margin-bottom:16px;box-shadow:0 1px 3px #0002;outline:none}}
.card.atual{{box-shadow:0 0 0 3px var(--sel)}}
.card header{{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline}} .card header span{{color:var(--sub)}}
.card header a{{font-size:12px}} .aviso{{background:var(--warn);padding:6px 10px;border-radius:6px;margin-top:8px}}
.thumbs{{display:flex;flex-wrap:wrap;gap:10px;padding:10px 0}}
figure{{margin:0;border:3px solid transparent;border-radius:8px;cursor:pointer;flex:0 0 auto;text-align:center;opacity:.55}}
figure.on{{border-color:var(--ok);opacity:1}} figure img{{height:230px;display:block;border-radius:5px}}
figcaption{{font-size:12px;padding:4px}} figure.on figcaption::after{{content:" ✔ RG/CPF";color:var(--ok);font-weight:600}}
footer{{display:flex;gap:10px}} button{{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid #ccd;cursor:pointer;background:#fff}}
button.ok{{background:var(--ok);color:#fff;border-color:var(--ok)}} .feito{{opacity:.35;pointer-events:none}}
.vazio{{text-align:center;color:var(--sub);padding:40px}}
</style></head><body>
<div class="top"><b>Conferência RG/CPF</b><span id="cont">{len(fila)} para conferir · {feitos} já resolvidos</span>
<span id="novos" hidden style="background:#ffd54f;color:#1d2433;padding:4px 10px;border-radius:6px;cursor:pointer" onclick="location.reload()"></span>
<span style="margin-left:auto;font-size:12px">Clique nas páginas de RG/CPF (todas as páginas do DOC aparecem) · teclas 1-9 · Enter confirma</span></div>
<main>{''.join(cards) or vazio}</main>
<script>
const cards=[...document.querySelectorAll('.card')];let atual=0;
function foco(n){{cards.forEach(c=>c.classList.remove('atual'));const c=cards.find((c,k)=>k>=n&&!c.classList.contains('feito'));
  if(c){{atual=cards.indexOf(c);c.classList.add('atual');c.scrollIntoView({{block:'center',behavior:'smooth'}})}}}}
document.querySelectorAll('figure').forEach(f=>f.onclick=()=>f.classList.toggle('on'));
async function enviar(card,acao){{
  const pags=[...card.querySelectorAll('figure.on')].map(f=>+f.dataset.p);
  if(acao==='ok'&&!pags.length){{alert('Marque ao menos uma página.');return}}
  let motivo='';if(acao==='pular'){{motivo=prompt('Motivo para pular (ex.: já protocolado):','');if(motivo===null)return}}
  const rodape=card.querySelector('footer'), antes=rodape.innerHTML;
  rodape.innerHTML='<b>Salvando...</b>';
  let j;try{{const r=await fetch('/salvar',{{method:'POST',body:JSON.stringify({{i:+card.dataset.i,acao,pags,motivo}})}});j=await r.json()}}
  catch(e){{j={{ok:false,erro:'sem resposta da tela de conferência'}}}}
  if(!j.ok){{rodape.innerHTML=antes;rodape.querySelector('.ok').onclick=()=>enviar(card,'ok');
    rodape.querySelector('.pular').onclick=()=>enviar(card,'pular');alert('Erro: '+j.erro);return}}
  card.classList.add('feito');card.querySelector('footer').innerHTML='<b>'+j.msg+'</b>';foco(atual+1)}}
cards.forEach(c=>{{c.querySelector('.ok').onclick=()=>enviar(c,'ok');c.querySelector('.pular').onclick=()=>enviar(c,'pular')}});
document.onkeydown=e=>{{const c=cards[atual];if(!c||e.target.tagName==='INPUT')return;
  if(e.key>='1'&&e.key<='9'){{c.querySelector(`figure[data-p="${{e.key}}"]`)?.classList.toggle('on')}}
  if(e.key==='Enter'){{e.preventDefault();enviar(c,'ok')}}}};
foco(0);
// A cada 4s confere na planilha: card já resolvido (em outra aba, ou por outra parte do robô) sai da tela
async function sincronizar(){{
  const ids=cards.filter(c=>!c.classList.contains('feito')).map(c=>c.dataset.i).join(',');
  let j;try{{j=await (await fetch('/situacao?ids='+ids)).json()}}catch(e){{return}}
  cards.forEach(c=>{{const st=j.status[c.dataset.i];
    if(!c.classList.contains('feito')&&st&&st!=='PREPARADO'&&st!=='VERIFICAR'){{
      c.classList.add('feito');c.style.display='none';
      if(cards[atual]===c)foco(atual+1)}}}});
  const n=document.getElementById('novos');n.hidden=!j.novos;n.textContent=j.novos+' cliente(s) novo(s) - clique aqui para carregar';
  const resta=cards.filter(c=>!c.classList.contains('feito')).length;
  document.getElementById('cont').textContent=resta+' para conferir nesta tela'}}
setInterval(sincronizar,4000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def _arquivo(self, i, nome):
        pasta = pasta_atual(ler_controle().loc[int(i)])
        return (pasta / nome) if pasta.name and (pasta / nome).exists() else None

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            somente = int(q["i"]) if q.get("i", "").isdigit() else None
            return self._send(200, pagina_html(somente).encode(), "text/html; charset=utf-8")
        if u.path == "/img":
            pasta = pasta_atual(ler_controle().loc[int(q["i"])])
            f = miniatura(pasta, int(q["p"])) if pasta.name else None
            return self._send(200, f.read_bytes(), "image/png") if f else self._send(404, b"", "text/plain")
        if u.path == "/situacao":   # a tela pergunta: quem já foi resolvido e quantos novos chegaram
            df = ler_controle()
            na_tela = {int(x) for x in q.get("ids", "").split(",") if x.isdigit()}
            fila = df.index[df["STATUS ROBÔ"].isin(STATUS_TELA) & filtro_etapa(df)]
            out = {"status": {str(i): df.at[i, "STATUS ROBÔ"] for i in na_tela if i in df.index},
                   "novos": len(set(fila) - na_tela)}
            return self._send(200, json.dumps(out).encode(), "application/json")
        if u.path == "/pdf" and q.get("f") in ("DOC_ORIGINAL.pdf", "CONTRATO.pdf"):
            f = self._arquivo(q["i"], q["f"])
            return self._send(200, f.read_bytes(), "application/pdf") if f else self._send(404, b"", "text/plain")
        self._send(404, b"", "text/plain")

    def do_POST(self):
        with TRAVA:
            try:
                with trava_planilha(espera_s=30):
                    out = self._salvar()
            except Exception as e:
                out = {"ok": False, "erro": str(e)}
        self._send(200, json.dumps(out).encode(), "application/json")

    def _salvar(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            df = ler_controle()
            i = int(d["i"])
            if df.at[i, "STATUS ROBÔ"] not in STATUS_TELA:
                raise ValueError(f"cliente já está como {df.at[i, 'STATUS ROBÔ']}")
            if d["acao"] == "ok":
                pags = sorted(int(p) for p in d["pags"])
                montar_rg(pasta_atual(df.loc[i]), pags)
                df.loc[i, ["STATUS ROBÔ", "PÁGINAS RG"]] = ["CONFERIDO", ",".join(map(str, pags))]
                msg = f"✔ Conferido (páginas {', '.join(map(str, pags))})"
            else:
                df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["PULAR", d.get("motivo") or "Pulado na conferência"]
                msg = "Pulado"
            salvar_linhas(df, [i], esperar=False)
            return {"ok": True, "msg": msg}
        except Exception as e:
            return {"ok": False, "erro": str(e)}


def pre_gerar():
    """Gera, em segundo plano e aos poucos, as miniaturas que faltarem."""
    try:
        df = ler_controle()
        for _, r in df[df["STATUS ROBÔ"].isin(STATUS_TELA) & filtro_etapa(df)].iterrows():
            pasta = pasta_atual(r)
            if pasta.name:
                for n in range(1, paginas_doc(pasta) + 1):
                    miniatura(pasta, n)
    except Exception:
        pass


if __name__ == "__main__":
    threading.Thread(target=pre_gerar, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), Handler)
    url = f"http://127.0.0.1:{PORTA}"
    print(f"Tela de conferência em {url}  (feche com Ctrl+C)")
    if "--sem-abrir" not in sys.argv:   # o painel deixa a conferência sempre no ar sem abrir janela
        webbrowser.open(url)
    srv.serve_forever()
