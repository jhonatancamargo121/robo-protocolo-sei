"""ETAPA 3 - Protocolar no SEI (modo semiautomático).

Uso:
    python protocolar.py                  -> protocola 1 cliente CONFERIDO (sempre 1 por vez)
    python protocolar.py --apresentacao   -> devagar, com faixa explicando cada etapa
                                             e campos destacados (para gravar/demonstrar)

Para cada cliente o robô:
  1. reconfere no SIG se o titular ainda é a Natela Empreendimentos
  2. abre Processo Novo > Requerimento Geral, preenche a especificação e o requerimento
  3. anexa RG/CPF, contrato e Decisão
  4. clica em Peticionar e escolhe o cargo
  5. PARA e espera VOCÊ digitar a senha e clicar em "Assinar"
  6. lê o número do processo nos Recibos e grava na planilha (PROTOCOLADO)
O robô nunca digita a senha.
"""
import random
import re
import sys
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from playwright.sync_api import sync_playwright

from comum import (aba, conectar_navegador, pasta_atual, etapa_ativa, filtro_etapa, ler_controle, salvar_linhas, sem_acento,
                   trava_planilha)
from config import (CAMPO_CCI, CARGO, DECISAO_PDF, DOC_CONTRATO, DOC_DECISAO, DOC_RG, EMPRESA, ESPECIFICACAO,
                    FUNDAMENTACAO, PEDIDO, TIPO_PROCESSO, URL_SIG)
from debitos import abrir_sig, bloqueio_debito, consulta_salva, consultar as consultar_debitos
from preparar import nome_parecido

ESPERA_ASSINATURA_MIN = 30
RE_PROCESSO = re.compile(r"\d{5}\.\d{7}/\d{4}-\d{2}")


FONTE_SIG = ""   # preenchido quando titular/débito vieram da planilha de débitos (SIG fora do ar)


class Falha(Exception):
    pass


class SemAssinatura(Exception):
    """Acabou o tempo esperando a assinatura: pode ter sido assinado depois -> fica EM ANDAMENTO
    e o próximo protocolo confere nos Recibos do SEI antes de seguir."""


class ComDebito(Exception):
    """IPTU com débito vencido: não protocolar."""


class JaNoNome(Exception):
    """IPTU já está no nome do cliente: não protocolar."""


# ---------------------------------------------------------------- modo apresentação
APRESENTACAO = False
PAUSA_MS = 2500  # pausa entre etapas no modo apresentação
# Ritmo humano (sempre ligado): pausas variadas entre etapas e digitação tecla a tecla,
# para não "atropelar" o SEI.
PAUSA_HUMANA_MS = (1500, 3000)
DIGITACAO_MS = (60, 140)


def pausa_humana(page):
    page.wait_for_timeout(random.randint(*PAUSA_HUMANA_MS))

JS_FAIXA = """t => {
  let d = document.getElementById('robo-faixa');
  if (!d) {
    d = document.createElement('div'); d.id = 'robo-faixa';
    document.documentElement.appendChild(d);
  }
  {
    d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;background:#1f4e78;' +
      'color:#fff;font:600 20px Segoe UI,Arial,sans-serif;padding:14px 20px;text-align:center;' +
      'box-shadow:0 -2px 10px #0007;pointer-events:none';
  }
  d.textContent = 'ROBÔ  |  ' + t;
}"""


def etapa(page, texto, pausa=True):
    print("   -", texto, flush=True)
    if not APRESENTACAO:
        try:
            pausa_humana(page)
        except Exception:
            pass
        return
    try:
        page.evaluate(JS_FAIXA, texto)
        if pausa:
            page.wait_for_timeout(PAUSA_MS)
    except Exception:
        pass


def destacar(page, seletor):
    if not APRESENTACAO:
        return
    try:
        el = page.locator(seletor).first
        el.scroll_into_view_if_needed()
        el.evaluate("e => { e.style.outline = '4px solid #f5a623'; e.style.outlineOffset = '3px'; }")
        page.wait_for_timeout(1200)
    except Exception:
        pass


def digitar(page, seletor, texto):
    destacar(page, seletor)
    campo = page.locator(seletor).first
    campo.click()
    campo.fill("")
    for ch in texto:  # tecla a tecla, com ritmo variado
        campo.press_sequentially(ch)
        page.wait_for_timeout(random.randint(*DIGITACAO_MS))
    page.wait_for_timeout(800)


# ---------------------------------------------------------------- requerimento
def preencher_modelo(html: str, row) -> str:
    """Completa o modelo do requerimento (mesmo texto que era digitado à mão)."""
    qd, lt = f"{int(row['QD']):02d}", f"{int(row['LT']):02d}"
    e = {k: escape(v) for k, v in EMPRESA.items()}
    endereco = e["endereco"]  # endereço fixo da empresa
    trocas = [
        ("Nome/Raz&atilde;o Social: ", f"Nome/Raz&atilde;o Social: {e['nome']}"),
        ("CPF / CNPJ:", f"CPF / CNPJ: {e['cnpj']}"),
        ("Telefone: ", f"Telefone: {e['telefone']}"),
        ("Endere&ccedil;o:", f"Endere&ccedil;o: {endereco}"),
        ("CEP:", f"CEP: {e['cep']}"),
        ("Bairro:", f"Bairro: {e['bairro']}"),
        ("Cidade:", f"Cidade: {e['cidade']}"),
        ("E-mail: ", f"E-mail: {e['email']}"),
        ("Representante Legal (Se for o caso): ", f"Representante Legal (Se for o caso): {e['representante']}"),
        ("(&nbsp; &nbsp;) Outros:", "( x ) Outros:"),
    ]
    for antes, depois in trocas:
        if html.count(antes) != 1:
            raise Falha(f"Modelo do requerimento mudou: não achei '{antes}' (achei {html.count(antes)}x)")
        html = html.replace(antes, depois, 1)
    # o modelo tem muitos espaços depois de CNPJ/Endereço/Bairro; encurta para não quebrar a linha
    for valor in (e["cnpj"], endereco, e["bairro"]):
        html = re.sub(re.escape(valor) + r"(?:&nbsp;|\s)+", valor + "&nbsp;" * 8, html, count=1)

    pedido = PEDIDO.format(nome=row["COMPRADOR"], qd=qd, lt=lt, cci=row["CCI"])
    texto = (f"<p>Prezados Senhores,</p>"
             f"<p style=\"text-indent:2cm\">{escape(FUNDAMENTACAO)}</p>"
             f"<p>{escape(pedido)}</p>")
    vazio = "<p>&nbsp;</p>\n\t\t\t</td>"
    pos = html.find("ALEGA&Ccedil;&Otilde;ES")
    k = html.find(vazio, pos)
    if pos < 0 or k < 0:
        raise Falha("Modelo do requerimento mudou: não achei o quadro de Fundamentações")
    return html[:k] + texto + html[k + len("<p>&nbsp;</p>"):]


def editar_requerimento(ctx, sei, row):
    sei.wait_for_function("() => typeof abrirJanelaDocumento === 'function'")
    ed = None
    for _ in range(3):
        try:
            with ctx.expect_page(timeout=20000) as pi:
                sei.evaluate("abrirJanelaDocumento()")
            ed = pi.value
            break
        except Exception:
            # a janela pode ter aberto sem o evento ter sido capturado
            ed = next((pg for pg in ctx.pages if "md_pet_editor_montar" in pg.url), None)
            if ed:
                break
            sei.wait_for_timeout(2000)
    if not ed:
        raise Falha("A janela do editor do requerimento não abriu")
    ed.wait_for_load_state()
    ed.wait_for_function("() => window.CKEDITOR && CKEDITOR.instances.txaConteudo "
                         "&& CKEDITOR.instances.txaConteudo.status === 'ready'", timeout=60000)
    html = ed.evaluate("() => CKEDITOR.instances.txaConteudo.getData()")
    novo = preencher_modelo(html, row)
    etapa(ed, "Modelo do requerimento da Prefeitura (em branco)")
    ed.evaluate("h => CKEDITOR.instances.txaConteudo.setData(h)", novo)
    ed.wait_for_timeout(1000)
    pausa_humana(ed)
    if APRESENTACAO:
        etapa(ed, f"Requerimento preenchido: dados da Natela Empreendimentos, ( x ) Outros e pedido para {row['COMPRADOR']}")
        altura = ed.evaluate("() => CKEDITOR.instances.txaConteudo.document.$.body.scrollHeight")
        for y in range(0, altura + 300, 120):   # rola o documento devagar
            ed.evaluate("y => CKEDITOR.instances.txaConteudo.window.$.scrollTo(0, y)", y)
            ed.wait_for_timeout(250)
        ed.wait_for_timeout(2500)
        etapa(ed, "Salvando o requerimento")
    with ed.expect_response(lambda r: r.request.method == "POST", timeout=60000):
        ed.locator(".cke_button__save").first.click()
    ed.wait_for_timeout(1500)
    if not ed.is_closed():
        ed.close()


# ---------------------------------------------------------------- anexos
def anexar(sei, grupo: str, arquivo: Path, nome_envio: str, tipo: str, complemento: str):
    """grupo '2' = Documento Essencial, '3' = Documentos Complementares."""
    sufixo = "Essencial" if grupo == "2" else "Complementar"
    etapa(sei, f"Anexando {nome_envio}  ->  {tipo}")
    destacar(sei, f"#fileArquivo{sufixo}")
    sei.set_input_files(f"#fileArquivo{sufixo}",
                        {"name": nome_envio, "mimeType": "application/pdf", "buffer": arquivo.read_bytes()})
    # espera o upload terminar (botão "Cancelar" do upload some)
    sei.wait_for_function(
        f"() => {{const b=document.querySelector('#btnUploadCancelarfrmDocumentos{sufixo}s');"
        f" return !b || getComputedStyle(b).display==='none'}}", timeout=180000)
    pausa_humana(sei)
    destacar(sei, f"#tipoDocumento{sufixo}")
    sei.select_option(f"#tipoDocumento{sufixo}", label=tipo)
    pausa_humana(sei)
    digitar(sei, f"#complemento{sufixo}", complemento)
    pausa_humana(sei)
    destacar(sei, f"label.form-check-label[for='rdNato{grupo}_1']")
    sei.click(f"label.form-check-label[for='rdNato{grupo}_1']")
    pausa_humana(sei)
    if not sei.locator(f"#rdNato{grupo}_1").is_checked():
        raise Falha("Não consegui marcar 'Nato-digital'")
    destacar(sei, f"input[onclick=\"validarUploadArquivo('{grupo}')\"]:visible")
    sei.locator(f"input[onclick=\"validarUploadArquivo('{grupo}')\"]:visible").first.click()
    try:
        # o SEI às vezes demora a processar o arquivo: espera até 3 min o anexo aparecer na lista
        sei.get_by_text(nome_envio, exact=True).first.wait_for(timeout=180000)
    except Exception:
        sei.wait_for_timeout(5000)
        if not sei.get_by_text(nome_envio, exact=True).count():
            raise Falha(f"O SEI não aceitou o anexo '{nome_envio}' (esperei 3 minutos)")
    pausa_humana(sei)
    if APRESENTACAO:
        sei.get_by_text(nome_envio, exact=True).first.scroll_into_view_if_needed()
        etapa(sei, f"Anexado: {nome_envio}")


# ---------------------------------------------------------------- assinatura
def achar_frame_assinatura(sei):
    for _ in range(60):
        for f in sei.frames:
            try:
                if f.locator("select option", has_text=CARGO).count():
                    return f
            except Exception:
                pass
        sei.wait_for_timeout(500)
    raise Falha("Não abriu a janela de assinatura (cargo/senha)")


def ler_recibo(sei, inicio: datetime) -> str:
    """Número do processo deste peticionamento na lista de Recibos: recibo aberto depois de 'inicio' e no
    formato do robô (se outra pessoa protocolar à mão com o mesmo login no mesmo minuto, o dela é ignorado)."""
    sei.locator("table tr", has=sei.locator("td")).filter(has_text=RE_PROCESSO).first.wait_for(timeout=60000)
    novos = []
    ja_usados = set(ler_controle()["NOVO PROTOCOLO SEI"])          # ex.: o do cliente anterior
    for txt in sei.locator("table tr").all_inner_texts():
        proc, data = RE_PROCESSO.search(txt), re.search(r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})", txt)
        if proc and data and proc.group(0) not in ja_usados \
                and datetime.strptime(data.group(1), FMT) >= inicio - timedelta(minutes=2):
            novos.append(proc.group(0))
    if not novos:
        raise Falha("Não achei nos Recibos um protocolo aberto depois do Peticionar")
    do_robo = []
    for p in novos:
        try:
            if eh_do_robo(sei, p):
                do_robo.append(p)
        except Exception:
            pass
    if len(do_robo) == 1:
        return do_robo[0]
    if not do_robo and len(novos) == 1:      # não deu para abrir o recibo, mas só há um: é este
        return novos[0]
    raise Falha(f"Não deu para saber qual recibo é deste cliente: {', '.join(novos)} - confira no SEI")


# ---------------------------------------------------------------- proteção contra protocolo duplicado
EM_ANDAMENTO = "EM ANDAMENTO"
FMT = "%d/%m/%Y %H:%M:%S"


def marcar_em_andamento(i, inicio: datetime):
    """Antes de clicar em Peticionar: se o robô for interrompido depois disso (Parar, queda de energia),
    o cliente NÃO volta sozinho para a fila; antes vamos aos Recibos do SEI ver se ele foi protocolado."""
    with trava_planilha():
        df = ler_controle()
        df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = [EM_ANDAMENTO, f"Peticionar em {inicio.strftime(FMT)}"]
        salvar_linhas(df, [i])


def recibos(sei) -> list[tuple[str, datetime]]:
    """[(processo, data/hora)] da lista de Recibos Eletrônicos de Protocolo do SEI."""
    link = sei.locator("a[href*='md_pet_usu_ext_recibo_listar']").first
    if not link.count():
        link = sei.get_by_text("Recibos Eletrônicos de Protocolo").first
    href = link.get_attribute("href")
    sei.goto(href if href.startswith("http") else sei.url.split("controlador_externo.php")[0] + href)
    sei.wait_for_load_state()
    lista = []
    for txt in sei.locator("table tr").all_inner_texts():
        proc, data = RE_PROCESSO.search(txt), re.search(r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2}(?::\d{2})?)", txt)
        if proc and data:
            d = data.group(1)
            lista.append((proc.group(0), datetime.strptime(d, FMT if d.count(":") == 2 else FMT[:-3])))
    return lista


# Como o robô descreve os anexos: um recibo só é "do robô" se tiver estes textos (protocolos feitos à mão
# com o mesmo login escrevem diferente, ex.: "DECISÃO JUDICIAL", "contrato compra e venda")
MARCA_ROBO = (f"{DOC_CONTRATO[0]} {DOC_CONTRATO[1]}", f"{DOC_DECISAO[0]} {DOC_DECISAO[1]}", f"{DOC_RG[0]} {DOC_RG[1]}")


def eh_do_robo(sei, proc: str) -> bool:
    """Abre o recibo do processo (numa aba separada) e confere se os anexos estão no formato do robô.
    'sei' precisa estar na lista de Recibos."""
    href = sei.locator("table tr", has_text=proc).first.locator("a").first.get_attribute("href")
    pg = sei.context.new_page()
    try:
        pg.goto(href if href.startswith("http") else sei.url.split("controlador_externo.php")[0] + href, timeout=60000)
        texto = re.sub(r"\s+", " ", pg.inner_text("body"))
        return all(m in texto for m in MARCA_ROBO)
    finally:
        pg.close()


def resolver_em_andamento(sei, df) -> bool:
    """Resolve quem ficou EM ANDAMENTO. True = pode seguir protocolando; False = parar."""
    pend = df.index[df["STATUS ROBÔ"].eq(EM_ANDAMENTO)].tolist()
    if not pend:
        return True
    lista = recibos(sei)
    ja_usados = set(df["NOVO PROTOCOLO SEI"])
    for i in pend:
        row = df.loc[i]
        m = re.search(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", row["OBSERVAÇÃO ROBÔ"])
        inicio = datetime.strptime(m.group(0), FMT) if m else None
        print(f"\n>>> {row['COMPRADOR']} ficou EM ANDAMENTO (o robô foi interrompido depois de Peticionar). "
              f"Conferindo nos Recibos do SEI...")
        if not inicio:
            achados = None
        else:
            achados = [(p, d) for p, d in lista if p not in ja_usados
                       and inicio - timedelta(minutes=2) <= d <= inicio + timedelta(minutes=ESPERA_ASSINATURA_MIN + 5)
                       and eh_do_robo(sei, p)]          # ignora protocolos feitos à mão com o mesmo login
        if achados is not None and len(achados) == 1:
            proc, d = achados[0]
            df.loc[i, ["NOVO PROTOCOLO SEI", "DATA NOVO PROTOCOLO", "STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = [
                proc, d.strftime("%d/%m/%Y"), "PROTOCOLADO",
                f"RG págs {row['PÁGINAS RG']} | nº lido nos Recibos depois de interrupção - confira no SEI"]
            ja_usados.add(proc)
            print(f"  ✔ PROTOCOLADO: {proc} (estava assinado; nº lido nos Recibos)")
        elif achados == []:
            df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["CONFERIDO", "Não foi assinado (interrompido) - volta para a fila"]
            print("  = Não foi assinado. Volta para a fila (CONFERIDO).")
        else:
            df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = [
                "ERRO", "Interrompido depois de Peticionar e não deu para saber pelos Recibos qual nº é dele: "
                        + ", ".join(p for p, _ in (achados or []))[:150] + " - confira no SEI"]
            print("  ✘ Não deu para saber pelos Recibos. Marcado ERRO para você conferir no SEI.")
        salvar_linhas(df, [i])
    return not df.loc[pend, "STATUS ROBÔ"].eq("PROTOCOLADO").any()


# ---------------------------------------------------------------- fluxo
def abrir_com_paciencia(sei, url, tentativas=3):
    """O SEI às vezes demora a abrir uma página: tenta de novo (até 3x, 60s cada) antes de desistir."""
    for t in range(1, tentativas + 1):
        try:
            sei.goto(url, timeout=60000)
            return
        except Exception:
            if t == tentativas:
                raise Falha(f"O SEI não abriu a página depois de {tentativas} tentativas (SEI lento ou fora do ar)")
            print(f"   - SEI lento, tentando de novo ({t + 1}/{tentativas})...", flush=True)
            sei.wait_for_timeout(10000)


def protocolar(ctx, sei, sig, row) -> str:
    pasta = pasta_atual(row)
    rg, contrato = pasta / "RG E CPF.pdf", pasta / "CONTRATO.pdf"
    for f in (rg, contrato, DECISAO_PDF):
        if not f.exists():
            raise Falha(f"Arquivo não encontrado: {f}")

    if APRESENTACAO:
        sig.bring_to_front()
        sig.evaluate("() => window.focus()")
        sig.wait_for_timeout(1500)
        etapa(sig, f"1) Conferindo no SIG da Prefeitura o CCI {row['CCI']} ({row['COMPRADOR']})")
    global FONTE_SIG
    FONTE_SIG = ""
    try:
        if sig is None:
            raise RuntimeError("SIG não abriu")
        imovel, debitos_sig = consultar_debitos(sig, row["CCI"], tentativas=2)
        bloqueio = bloqueio_debito(debitos_sig) if imovel else ""
    except Exception as e:
        # SIG da Prefeitura fora do ar: usa a planilha de débitos (verificação salva), reavaliada com a data de hoje
        salvo = consulta_salva(row["CCI"])
        if not salvo:
            raise Falha(f"SIG fora do ar e o CCI {row['CCI']} não está na planilha de débitos ({e})")
        imovel, bloqueio, data_ver = salvo
        FONTE_SIG = f"titular/débito pela planilha de débitos de {data_ver} (SIG fora do ar)"
        print(f"   - SIG fora do ar: usando a planilha de débitos de {data_ver}", flush=True)
    if not imovel:
        raise Falha(f"CCI {row['CCI']} não encontrado no SIG")
    titular = imovel["nome"]
    if nome_parecido(row["COMPRADOR"], titular) or nome_parecido(titular, row["COMPRADOR"]):
        raise JaNoNome(titular)
    if bloqueio:
        raise ComDebito(bloqueio)
    # pode protocolar: Natela Empreendimentos, ou o ex-comprador que o preparar já identificou
    if "Natela Empreendimentos" not in sem_acento(titular) and sem_acento(titular) != sem_acento(row["TITULAR SIG"]):
        raise Falha(f"Titular no SIG mudou para: {titular} (não é Natela Empreendimentos nem o ex-comprador conferido)")
    etapa(sig or sei, f"Titular atual na Prefeitura: {imovel['nome']}  -  QD {imovel['quadra']} LT {imovel['lote']}  OK"
                      + (f"  [{FONTE_SIG}]" if FONTE_SIG else ""))

    # Processo Novo > tipo de processo
    sei.bring_to_front()
    if APRESENTACAO:
        sei.evaluate("() => window.focus()")
        sei.wait_for_timeout(1500)
    etapa(sei, "2) SEI: Peticionamento > Processo Novo")
    href = sei.locator("a[href*='acao=md_pet_usu_ext_iniciar']").first.get_attribute("href")
    abrir_com_paciencia(sei, href if href.startswith("http") else sei.url.split("controlador_externo.php")[0] + href)
    etapa(sei, f"Escolhendo o tipo de processo: {TIPO_PROCESSO}", pausa=False)
    destacar(sei, f"text={TIPO_PROCESSO}")
    sei.get_by_text(TIPO_PROCESSO, exact=True).first.click()
    sei.wait_for_selector("#txtEspecificacao")

    etapa(sei, "3) Preenchendo a especificação", pausa=False)
    digitar(sei, "#txtEspecificacao", ESPECIFICACAO)
    etapa(sei, "4) Abrindo o requerimento (documento principal)")
    editar_requerimento(ctx, sei, row)
    etapa(sei, "5) Anexando os documentos (todos Nato-digital, Restrito)")

    nome = row["COMPRADOR"]
    anexar(sei, "2", rg, f"RG E CPF - {nome}.pdf", *DOC_RG)
    anexar(sei, "3", contrato, f"CONTRATO - {nome}.pdf", *DOC_CONTRATO)
    anexar(sei, "3", DECISAO_PDF, "Decisão.pdf", *DOC_DECISAO)

    inicio = datetime.now()
    marcar_em_andamento(row.name, inicio)
    etapa(sei, "6) Clicando em Peticionar", pausa=False)
    destacar(sei, "#Peticionar")
    sei.click("#Peticionar")
    frame = achar_frame_assinatura(sei)
    if APRESENTACAO:
        sei.wait_for_timeout(PAUSA_MS)
    pausa_humana(sei)
    sel_cargo = frame.locator("select", has=frame.locator("option", has_text=CARGO)).first
    sel_cargo.select_option(label=CARGO)
    if sel_cargo.evaluate("e => e.options[e.selectedIndex].text").strip() != CARGO:
        raise Falha("Não consegui escolher o cargo")
    etapa(sei, f"Pronto! Cargo: {CARGO}. Confira e ASSINE com sua senha (o robô não digita a senha).", pausa=False)
    try:
        frame.locator("input[type=password]").first.focus()
    except Exception:
        pass

    print("\n" + "=" * 70)
    print(f"  CONFIRA a tela do SEI: {nome}  QD{row['QD']} LT{row['LT']}  CCI {row['CCI']}")
    print("  Se estiver tudo certo: digite a SENHA e clique em ASSINAR.")
    print("  Se algo estiver errado: clique em FECHAR (o robô marca ERRO e para).")
    print("=" * 70)

    limite = time.time() + ESPERA_ASSINATURA_MIN * 60
    while time.time() < limite:
        if "md_pet_usu_ext_recibo" in sei.url:
            return ler_recibo(sei, inicio)
        try:
            # se o cargo esvaziar (o SEI às vezes recarrega a janela), escolhe de novo
            if sel_cargo.count() and sel_cargo.evaluate("e => e.options[e.selectedIndex].text").strip() != CARGO:
                sel_cargo.select_option(label=CARGO)
            if not frame.locator("select option", has_text=CARGO).count():
                sei.wait_for_timeout(2000)
                if "md_pet_usu_ext_recibo" not in sei.url:
                    raise Falha("Janela de assinatura fechada sem assinar")
        except Falha:
            raise
        except Exception:
            pass  # frame recarregando durante o envio
        sei.wait_for_timeout(1000)
    raise SemAssinatura(f"Sem assinatura em {ESPERA_ASSINATURA_MIN} minutos")


def main(limite=1):
    df = ler_controle()
    print(f"Etapa: {etapa_ativa() or 'todas'}")
    sobrou = df["STATUS ROBÔ"].eq(EM_ANDAMENTO).any()
    if not sobrou and not (df["STATUS ROBÔ"].eq("CONFERIDO") & filtro_etapa(df)).any():
        print("0 cliente(s) para protocolar")
        return

    with sync_playwright() as p:
        _, ctx = conectar_navegador(p)
        sei = next((pg for pg in ctx.pages if "sei.cidadeexemplo" in pg.url
                    and "usuario_externo_logar" not in pg.url), None) or aba(ctx, "sei.prefeitura-exemplo.gov.br")
        if sobrou:
            # 1 ação por execução: se achou um protocolo assinado, só registra e para aqui
            if not resolver_em_andamento(sei, df):
                return
            df = ler_controle()
        fila = df.index[df["STATUS ROBÔ"].eq("CONFERIDO") & filtro_etapa(df)].tolist()[:limite]
        print(f"{len(fila)} cliente(s) para protocolar")
        if not fila:
            return
        try:
            # SIG num navegador invisível SEPARADO (sessão limpa a cada cliente): a sessão do SIG no navegador
            # do robô expira/bloqueia depois de muitas consultas
            sig = p.chromium.launch().new_page()
            sig.goto(URL_SIG, timeout=60000)
            sig.locator(CAMPO_CCI).first.wait_for(timeout=60000)
        except Exception:
            sig = None      # SIG fora do ar: o protocolo usa a planilha de débitos
            print("   - SIG da Prefeitura não abriu: titular/débito pela planilha de débitos", flush=True)
        avisos = []
        sei.on("dialog", lambda d: (avisos.append(d.message), d.accept()))
        if APRESENTACAO:
            sig.bring_to_front()
            for k in range(10, 0, -1):
                etapa(sig, f"Robô de protocolo SEI - começando em {k} segundos...", pausa=False)
                sig.wait_for_timeout(1000)

        for n, i in enumerate(fila, 1):
            row = df.loc[i]
            print(f"\n[{n}/{len(fila)}] {row['COMPRADOR']} QD{row['QD']} LT{row['LT']} CCI {row['CCI']}")
            avisos.clear()
            try:
                proc = protocolar(ctx, sei, sig, row)
                df.loc[i, ["NOVO PROTOCOLO SEI", "DATA NOVO PROTOCOLO", "STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = \
                    [proc, datetime.now().strftime("%d/%m/%Y"), "PROTOCOLADO",
                     f"RG págs {row['PÁGINAS RG']}" + (f" | {FONTE_SIG}" if FONTE_SIG else "")]
                print(f"  ✔ PROTOCOLADO: {proc}")
                salvar_linhas(df, [i])
            except SemAssinatura as e:
                # NÃO marca ERRO: se assinarem depois, o próximo "Protocolar" acha o nº nos Recibos
                print(f"  ⏸ SEM ASSINATURA NO TEMPO ({e}). O cliente fica EM ANDAMENTO: no próximo Protocolar o robô confere "
                      "nos Recibos do SEI se foi assinado depois (não protocola de novo).")
                break
            except ComDebito as e:
                df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["COM DÉBITO", str(e)]
                salvar_linhas(df, [i])
                print(f"  = COM DÉBITO - não protocolado: {e}")
            except JaNoNome as e:
                df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = [
                    "JÁ NO NOME", f"IPTU já está no nome do cliente ({e}) - não protocolado"]
                salvar_linhas(df, [i])
                print(f"  = JÁ NO NOME do cliente ({e}) - não precisa protocolo")
            except Exception as e:
                msg = f"{e}" + (f" | SEI disse: {' / '.join(avisos)}" if avisos else "")
                fechou_sem_assinar = "fechada sem assinar" in str(e)
                if ler_controle().at[i, "STATUS ROBÔ"] == EM_ANDAMENTO and not fechou_sem_assinar:
                    # já clicou em Peticionar e não sabemos se assinaram (navegador fechado, queda...):
                    # fica EM ANDAMENTO -> o próximo Protocolar confere nos Recibos (nunca protocola 2x)
                    print(f"  ⏸ Interrompido depois de Peticionar ({msg[:120]}). Fica EM ANDAMENTO: "
                          "o próximo Protocolar confere nos Recibos do SEI se foi assinado.")
                    break
                df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["ERRO", msg[:250]]
                salvar_linhas(df, [i])
                print(f"  ✘ ERRO: {msg}\n  Parando para você verificar.")
                break

    print("\nResumo:\n" + df["STATUS ROBÔ"].value_counts().to_string())


if __name__ == "__main__":
    # Sempre 1 cliente por execução (decisão do usuário: evita erro/sobrecarga no SEI)
    APRESENTACAO = "--apresentacao" in sys.argv
    main(1)
