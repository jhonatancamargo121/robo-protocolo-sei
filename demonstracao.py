"""DEMONSTRAÇÃO COMPLETA (1 cliente, tudo visível no navegador do robô, para gravar).

Uso: python demonstracao.py

  1. Loteamento: busca o cliente pelo CPF, abre os anexos da venda, mostra e baixa CONTRATO e DOC
  2. Conferência RG/CPF: abre a tela de conferência só com esse cliente - VOCÊ confirma as páginas
  3. SIG: confere o CCI / titular do IPTU (regra: Natela Empreendimentos ou ex-comprador -> protocola)
  4. SEI: protocolo completo e para na assinatura - VOCÊ assina

O cliente escolhido é o próximo PENDENTE que passa em todas as checagens
(venda efetivada, anexos do próprio cliente, contrato com nome/CPF, IPTU apto a protocolar).
"""
import threading
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import conferencia
import protocolar as P
from debitos import bloqueio_debito, consultar as consultar_debitos
from comum import aba, conectar_navegador, filtro_etapa, ler_controle, pasta_cliente, salvar_linhas
from config import API_LOTEAMENTO, API_TOTVS, URL_SIG
from preparar import (JS_ANEXOS, JS_CLIENTE, baixar, classificar_titular, consultar_sig,
                      contrato_confere, eh_contrato, eh_doc, escolher, miniaturas)

URL_LOT_CLIENTES = "http://loteamento.exemplo.com.br/#/dados-cliente"
ESPERA_CONFERENCIA_MIN = 15


def escolher_cliente(df, lot, sig):
    """Próximo PENDENTE que passa em todas as checagens (sem mostrar nada ainda)."""
    for i in df.index[df["STATUS ROBÔ"].eq("PENDENTE") & filtro_etapa(df)]:
        row = df.loc[i]
        r = lot.evaluate(JS_ANEXOS, [API_LOTEAMENTO, row["Nº VENDA"]])
        if "erro" in r or not r["anexos"]:
            continue
        nome_lot = r["resumo"]["nomeCliente"]
        contrato = escolher(r["anexos"], eh_contrato, row["Nº VENDA"], nome_lot)
        doc = escolher(r["anexos"], eh_doc, row["Nº VENDA"], nome_lot)
        if not contrato or not doc:
            continue
        cli = lot.evaluate(JS_CLIENTE, [API_TOTVS, [nome_lot, row["COMPRADOR"]], row["Nº VENDA"]])
        if cli.get("situacao") != "Efetivada":
            continue
        imovel, debitos_sig = consultar_debitos(sig, row["CCI"])
        if not imovel or classificar_titular(imovel["nome"], cli["nome"], r["anexos"], row["Nº VENDA"]) \
                not in ("Natela Empreendimentos", "EX-COMPRADOR") or bloqueio_debito(debitos_sig):
            continue
        return i, cli, contrato, doc
    raise SystemExit("Nenhum cliente PENDENTE apto para a demonstração.")


def trocar_aba(page, nome):
    """Traz a aba para a frente na tela (para aparecer na gravação)."""
    page.bring_to_front()
    page.evaluate("() => window.focus()")
    page.wait_for_timeout(1500)
    print(f"   [aba] {nome}", flush=True)


def abrir_busca_clientes(lot):
    """Início do Loteamento (recarregado) > menu Clientes: garante o campo de busca limpo."""
    lot.goto("http://loteamento.exemplo.com.br/#/index")
    lot.reload()
    lot.wait_for_load_state("networkidle")
    lot.get_by_text("Clientes", exact=True).first.click()
    lot.wait_for_selector("input[name=nomeCpf]", timeout=30000)


def mostrar_anexo(lot, anexo, rotulo):
    P.etapa(lot, f"Abrindo o {rotulo}: {anexo['nomeAnexo']}", pausa=False)
    linha = lot.locator("mat-row", has_text=anexo["nomeAnexo"][:30]).first
    linha.scroll_into_view_if_needed()
    linha.evaluate("e => { e.style.outline = '4px solid #f5a623'; }")
    lot.wait_for_timeout(1500)
    linha.locator(".mat-column-visualizar button").click()   # nunca o botão de excluir ao lado
    lot.wait_for_timeout(6000)  # deixa o PDF aparecer na tela


def main():
    df = ler_controle()
    with sync_playwright() as p:
        _, ctx = conectar_navegador(p)
        lot = aba(ctx, "loteamento.exemplo.com.br")
        sig = aba(ctx, "sig.prefeitura-exemplo.gov.br", URL_SIG)
        sig.wait_for_load_state("networkidle")
        sei = next((pg for pg in ctx.pages if "sei.cidadeexemplo" in pg.url
                    and "usuario_externo_logar" not in pg.url), None) or aba(ctx, "sei.prefeitura-exemplo.gov.br")

        print("Escolhendo o cliente da demonstração...")
        i, cli, contrato, doc = escolher_cliente(df, lot, sig)
        row = df.loc[i]
        print(f"Cliente: {cli['nome']}  CPF {cli['cpf']}  QD{row['QD']} LT{row['LT']}")

        # ------------------------------------------------ contagem regressiva
        trocar_aba(lot, "LOTEAMENTO")
        abrir_busca_clientes(lot)
        for k in range(20, 0, -1):
            P.etapa(lot, f"Robô de protocolo SEI - demonstração começando em {k} segundos...", pausa=False)
            lot.wait_for_timeout(1000)

        # ------------------------------------------------ 1) Loteamento
        P.etapa(lot, "1) LOTEAMENTO: buscando o cliente pelo CPF")
        P.digitar(lot, "input[name=nomeCpf]", cli["cpf"])
        lot.keyboard.press("Enter")
        try:
            lot.get_by_text(cli["nome"], exact=False).first.wait_for(timeout=15000)
        except Exception:  # se a busca por CPF não trouxer, busca pelo nome
            P.digitar(lot, "input[name=nomeCpf]", cli["nome"])
            lot.keyboard.press("Enter")
            lot.get_by_text(cli["nome"], exact=False).first.wait_for(timeout=15000)
        P.etapa(lot, f"Cliente encontrado: {cli['nome']} - CPF {cli['cpf']}")
        lot.get_by_text(cli["nome"], exact=False).first.click()
        lot.wait_for_timeout(4000)
        P.etapa(lot, f"Venda {row['Nº VENDA']} - QD {row['QD']} LT {row['LT']} - situação: {cli['situacao']}")

        P.etapa(lot, "Abrindo os anexos da venda (só os documentos DESTE cliente)", pausa=False)
        botao = lot.locator("button[mattooltip='Visualizar Anexos']").first
        botao.evaluate("e => { e.style.outline = '4px solid #f5a623'; }")
        lot.wait_for_timeout(1500)
        botao.click()
        lot.locator("mat-row").first.wait_for(timeout=20000)
        lot.wait_for_timeout(2500)

        pasta = pasta_cliente(row)
        mostrar_anexo(lot, contrato, "CONTRATO")
        P.etapa(lot, f"Baixando o contrato para a pasta: {pasta.name}")
        baixar(contrato["id"], pasta / "CONTRATO.pdf")
        ok = contrato_confere(pasta / "CONTRATO.pdf", cli["nome"], cli["cpf"])
        P.etapa(lot, "Contrato conferido: nome/CPF do cliente encontrados no documento  OK" if ok
                else "ATENÇÃO: contrato sem o nome/CPF do cliente - conferir")
        if ok is False:
            raise SystemExit("Contrato não confere com o cliente - demonstração interrompida.")

        mostrar_anexo(lot, doc, "DOC (documentos pessoais)")
        P.etapa(lot, "Baixando o DOC (RG/CPF) para a mesma pasta")
        baixar(doc["id"], pasta / "DOC_ORIGINAL.pdf")
        paginas = miniaturas(pasta / "DOC_ORIGINAL.pdf", pasta)

        df = ler_controle()
        df.loc[i, ["CPF", "PASTA", "PÁGINAS RG", "STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = \
            [cli["cpf"], str(pasta), "1", "PREPARADO", f"DOC com {paginas} páginas (demonstração)"]
        salvar_linhas(df, [i])

        # ------------------------------------------------ 2) Conferência RG/CPF
        srv = None
        try:
            srv = conferencia.ThreadingHTTPServer(("127.0.0.1", conferencia.PORTA), conferencia.Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        except OSError:
            pass  # a tela de conferência já está rodando
        P.etapa(lot, "Documentos baixados. Agora: conferência do RG/CPF")
        conf = ctx.new_page()
        conf.goto(f"http://127.0.0.1:{conferencia.PORTA}/?i={i}")
        trocar_aba(conf, "CONFERÊNCIA RG/CPF")
        P.etapa(conf, "2) CONFERÊNCIA: marque as páginas que são RG/CPF e clique em Confirmar", pausa=False)
        print("\n>>> Na tela de conferência: marque as páginas de RG/CPF e clique em 'Confirmar páginas'.")
        limite = time.time() + ESPERA_CONFERENCIA_MIN * 60
        while time.time() < limite:
            status = ler_controle().at[i, "STATUS ROBÔ"]
            if status != "PREPARADO":
                break
            conf.wait_for_timeout(2000)
        else:
            raise SystemExit("Conferência não feita a tempo.")
        if status != "CONFERIDO":
            raise SystemExit(f"Cliente marcado como {status} na conferência - protocolo não será feito.")
        P.etapa(conf, "Páginas de RG/CPF confirmadas  OK - agora: conferência do CCI no SIG (IPTU)")
        conf.close()
        trocar_aba(sig, "SIG - PREFEITURA")
        if srv:
            srv.shutdown()

        # ------------------------------------------------ 3) SIG + 4) SEI
        df = ler_controle()
        row = df.loc[i]
        avisos = []
        sei.on("dialog", lambda d: (avisos.append(d.message), d.accept()))
        print(f"\n[SIG + SEI] {row['COMPRADOR']} QD{row['QD']} LT{row['LT']} CCI {row['CCI']}")
        try:
            proc = P.protocolar(ctx, sei, sig, row)
            df.loc[i, ["NOVO PROTOCOLO SEI", "DATA NOVO PROTOCOLO", "STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = \
                [proc, datetime.now().strftime("%d/%m/%Y"), "PROTOCOLADO", f"RG págs {row['PÁGINAS RG']} (demonstração)"]
            salvar_linhas(df, [i])
            P.etapa(sei, f"PROTOCOLADO: {proc} - {row['COMPRADOR']}", pausa=False)
            print(f"  ✔ PROTOCOLADO: {proc}")
        except P.ComDebito as e:
            df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["COM DÉBITO", str(e)]
            salvar_linhas(df, [i])
            print(f"  = COM DÉBITO - não protocolado: {e}")
        except P.JaNoNome as e:
            df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["JÁ NO NOME", f"IPTU já está no nome do cliente ({e})"]
            salvar_linhas(df, [i])
            print(f"  = JÁ NO NOME do cliente ({e}) - não precisa protocolo")
        except Exception as e:
            msg = f"{e}" + (f" | SEI disse: {' / '.join(avisos)}" if avisos else "")
            df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["ERRO", msg[:250]]
            salvar_linhas(df, [i])
            print(f"  ✘ ERRO: {msg}")


if __name__ == "__main__":
    P.APRESENTACAO = True
    main()
