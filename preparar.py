"""ETAPA 1 - Preparar: baixa contrato + DOC do Loteamento e confere o CCI no SIG.

Só LÊ os sistemas (não protocola nada). Uso:
    python preparar.py            -> todos os PENDENTE
    python preparar.py 10         -> para depois de baixar documentos de 10 clientes
                                     (os SEM ANEXO no caminho não contam)

Resultado por cliente (coluna STATUS ROBÔ):
    PREPARADO  -> documentos baixados, pronto para a tela de conferência
    SEM ANEXO  -> não achou CONTRATO e/ou DOC no Loteamento
    VERIFICAR  -> cliente/venda não confere no Loteamento, contrato sem o nome/CPF do cliente,
                  ou titular/quadra/lote diferente no SIG

Segurança contra documento de outra pessoa (mesmo lote pode ter vários compradores):
  - o cliente é identificado no TOTVS pela venda -> nome exato e CPF
  - só usa anexos com o MESMO nº de venda e com o nome do cliente no nome do anexo
  - o texto do contrato baixado precisa conter o nome ou o CPF do cliente
"""
import re
from difflib import SequenceMatcher
import sys
import time
import urllib.request

import pymupdf
from playwright.sync_api import sync_playwright

from comum import (aba, conectar_navegador, etapa_ativa, filtro_etapa, ler_controle, pasta_cliente,
                   salvar_linhas, sem_acento)
from config import CAMPO_CCI, API_LOTEAMENTO, API_TOTVS, PAGINAS_CONFERENCIA, URL_ANEXO, URL_SIG

JS_ANEXOS = """async ([api, venda]) => {
  const H = {'Authorization': 'Bearer ' + localStorage.getItem('tokenLoteamento'),
             'Content-Type': 'application/json'};
  const r = await fetch(api + '/unidade-subunidade/resumo/venda/' + venda, {headers: H});
  if (!r.ok) return {erro: 'resumo HTTP ' + r.status};
  const resumo = await r.json();
  const a = await fetch(api + '/unidade-subunidade-anexo/buscar-por-qd-lt-emp',
                        {method: 'POST', headers: H, body: JSON.stringify(resumo)});
  if (!a.ok) return {erro: 'anexos HTTP ' + a.status};
  return {resumo, anexos: await a.json()};
}"""


# Acha no TOTVS o cliente dono da venda: busca pelo nome e confirma pelo nº da venda.
JS_CLIENTE = """async ([api, nomes, venda]) => {
  const H = {'Authorization': 'Bearer ' + localStorage.getItem('tokenLoteamento')};
  const ip = localStorage.getItem('ipUsuario') || '';
  for (const nome of nomes) {
    const r = await fetch(api + '/cliente/com-vendas/nome-ou-cgc/' + encodeURIComponent(nome) + '?ip=' + ip, {headers: H});
    if (!r.ok) return {erro: 'cliente HTTP ' + r.status};
    for (const c of await r.json()) {
      const v = await fetch(api + '/venda/cod-cfo/' + c.codcfo + '?ip=' + ip, {headers: H});
      if (!v.ok) continue;
      const venda_ok = (await v.json()).find(x => String(x.numVenda) === String(venda));
      if (venda_ok) return {nome: c.nome, cpf: c.cgccfo, codcfo: c.codcfo, situacao: venda_ok.situacao,
                            quadra: venda_ok.numUnidade, lote: venda_ok.numSubUnidade};
    }
  }
  return {nao_achou: true};
}"""


def nome_parecido(nome, texto):
    """Nome do cliente aparece no texto, tolerando erro de digitação (ex.: OIVEIRA x OLIVEIRA)."""
    nome, texto = sem_acento(nome), sem_acento(texto)
    if nome in texto:
        return True
    n = len(nome)
    return any(SequenceMatcher(None, nome, texto[k:k + n]).ratio() >= 0.9 for k in range(max(1, len(texto) - n + 1)))


def escolher(anexos, filtro, venda, nome):
    """Só anexos da MESMA venda e com o nome do cliente; o mais recente."""
    cands = [a for a in anexos
             if filtro(a["nomeAnexo"].upper())
             and str(a.get("numVenda")) == str(venda)
             and nome_parecido(nome, a["nomeAnexo"])]
    return max(cands, key=lambda a: a.get("dataInclusao") or 0) if cands else None


def contrato_confere(pdf, nome, cpf):
    """True/False se o texto tem o nome ou CPF do cliente; None se o PDF não tem texto (escaneado)."""
    with pymupdf.open(pdf) as d:
        texto = sem_acento(" ".join(p.get_text() for p in d))
    if len(texto) < 300:
        return None
    digitos = re.sub(r"\D", "", cpf or "")
    return sem_acento(nome) in texto or (len(digitos) >= 11 and digitos in re.sub(r"\D", "", texto))


def contrato_do_lote(pdf, qd, lt):
    """True/False se o quadro 'DESCRIÇÃO E MEDIDAS DO IMÓVEL' do contrato traz a quadra/lote do cliente;
    None se não achar o quadro (contrato em outro formato ou escaneado)."""
    with pymupdf.open(pdf) as d:
        texto = sem_acento(" ".join(p.get_text() for p in d))
    k = texto.find("DESCRICAO E MEDIDAS")
    if k < 0:
        return None
    m = re.search(r"RUA/AVENIDA\s+0*(\d+)\s+0*(\d+)\s", texto[k:k + 400])
    if not m:
        return None
    return int(m.group(1)) == int(qd) and int(m.group(2)) == int(lt)


def eh_contrato(n):
    return "CONTRATO" in n and not any(x in n for x in ("CARTA", "DISTRATO", "CANCELAMENTO", "ADITIVO"))


def eh_doc(n):
    # aceita "DOC-NOME", "44QD..-DOC-NOME", "CO44-QD..-DOC-NOME" etc. (nunca um contrato)
    # também "DOCUMENTOS PESSOAIS NOME"
    return "DOC" in n and "CONTRATO" not in n and ("DOCUMENTO" not in n or "PESSOA" in n)


def classificar_titular(titular, cliente, anexos, venda):
    """Regra do IPTU (SIG):
    Natela Empreendimentos  -> protocolar
    EX-COMPRADOR  -> protocolar (titular é comprador de outra venda do mesmo lote, ex.: distrato)
    CLIENTE       -> NÃO protocolar (já está no nome do cliente atual)
    OUTRO         -> verificar
    """
    if "Natela Empreendimentos" in sem_acento(titular):
        return "Natela Empreendimentos"
    if nome_parecido(cliente, titular) or nome_parecido(titular, cliente):
        return "CLIENTE"
    outras = [a["nomeAnexo"] for a in anexos if str(a.get("numVenda")) != str(venda)]
    if any(nome_parecido(titular, n) for n in outras):
        return "EX-COMPRADOR"
    return "OUTRO"


def baixar(id_anexo, destino, tentativas=3):
    """Baixa o PDF; se a conexão cair no meio, tenta de novo."""
    for t in range(tentativas):
        try:
            req = urllib.request.Request(URL_ANEXO.format(id=id_anexo), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                dados = r.read()
            with open(destino, "wb") as f:
                f.write(dados)
            return
        except Exception:
            if t == tentativas - 1:
                raise
            time.sleep(3)


def consultar_sig(sig, cci):
    inp = sig.locator(CAMPO_CCI).first
    inp.fill(str(cci))
    with sig.expect_response(lambda r: "pesquisarImoveis" in r.url, timeout=30000) as ri:
        sig.locator('button[ng-click="vm.pesquisar()"]').click()
    dados = ri.value.json()
    return dados[0] if dados else None


def miniaturas(doc_pdf, pasta):
    with pymupdf.open(doc_pdf) as d:
        (pasta / "miniaturas").mkdir(exist_ok=True)
        for i in range(len(d)):   # todas as páginas (o RG pode estar no fim do DOC)
            d[i].get_pixmap(dpi=60).save(pasta / "miniaturas" / f"pag{i + 1}.png")
        return len(d)


def main(limite=None, reprocessar=False):
    df = ler_controle()
    pend = df.index[df["STATUS ROBÔ"].eq("PENDENTE") & filtro_etapa(df)].tolist()
    print(f"Etapa: {etapa_ativa() or 'todas'}")
    if reprocessar:  # só os VERIFICAR de anexos e as falhas de download, depois de um ajuste no robô
        pend = df.index[(df["STATUS ROBÔ"].eq("VERIFICAR") & df["OBSERVAÇÃO ROBÔ"].str.startswith("Sem "))
                        | df["STATUS ROBÔ"].eq("COM DÉBITO")   # débito pode ter sido pago
                        | (df["STATUS ROBÔ"].eq("PENDENTE") & df["OBSERVAÇÃO ROBÔ"].str.startswith("Falha"))].tolist()
        limite = None
    print(f"{len(pend)} clientes pendentes" + (f" (parando após {limite} com documentos)" if limite else ""))
    baixados = 0
    if not pend:
        return

    with sync_playwright() as p:
        _, ctx = conectar_navegador(p)
        lot = aba(ctx, "loteamento.exemplo.com.br")
        from debitos import abrir_sig
        sig = abrir_sig(ctx)          # garante a tela de consulta (sessão do SIG expira)

        for n, i in enumerate(pend, 1):
            if limite and baixados >= limite:
                break
            row = df.loc[i]
            obs = []
            obs_titular = ""
            print(f"[{n}/{len(pend)}] {row['COMPRADOR']} QD{row['QD']} LT{row['LT']} ...", end=" ", flush=True)
            try:
                if not row["Nº VENDA"].isdigit():
                    df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["VERIFICAR", "Nº VENDA vazio/inválido no Smartsheet"]
                    print("VERIFICAR: sem nº de venda")
                    continue
                # 1) Loteamento: anexos da venda
                r = lot.evaluate(JS_ANEXOS, [API_LOTEAMENTO, row["Nº VENDA"]])
                if "erro" in r:
                    raise RuntimeError(f"Loteamento: {r['erro']} (login expirou?)")
                nome_lot = r["resumo"]["nomeCliente"]
                contrato = escolher(r["anexos"], eh_contrato, row["Nº VENDA"], nome_lot)
                doc = escolher(r["anexos"], eh_doc, row["Nº VENDA"], nome_lot)
                if not contrato or not doc:
                    faltam = [x for x, a in (("CONTRATO", contrato), ("DOC", doc)) if not a]
                    if r["anexos"]:
                        nomes = "; ".join(a["nomeAnexo"] for a in r["anexos"])[:180]
                        df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = [
                            "VERIFICAR", f"Sem {', '.join(faltam)} com o nome do cliente nesta venda. Anexos do lote: {nomes}"]
                        print("VERIFICAR: anexos não batem com o cliente")
                    else:
                        df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["SEM ANEXO", "Falta no Loteamento: " + ", ".join(faltam)]
                        print("SEM ANEXO")
                    continue
                res = r["resumo"]
                cli = lot.evaluate(JS_CLIENTE, [API_TOTVS, [res["nomeCliente"], row["COMPRADOR"]], row["Nº VENDA"]])
                if "erro" in cli:
                    raise RuntimeError(f"TOTVS: {cli['erro']}")
                if cli.get("nao_achou"):
                    df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["VERIFICAR", f"Venda {row['Nº VENDA']} não encontrada no cadastro do cliente (TOTVS)"]
                    print("VERIFICAR: venda não achada no TOTVS")
                    continue
                df.loc[i, "CPF"] = cli["cpf"]
                if cli.get("situacao") != "Efetivada":
                    atuais = [a["nomeAnexo"] for a in r["anexos"] if str(a.get("numVenda")) != str(row["Nº VENDA"])]
                    df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = [
                        "VERIFICAR", f"Venda {row['Nº VENDA']} está {cli.get('situacao')} - planilha desatualizada? "
                                     f"Outros anexos do lote: {'; '.join(atuais)[:150]}"]
                    print(f"VERIFICAR: venda {cli.get('situacao')}")
                    continue
                if sem_acento(cli["nome"]) != sem_acento(row["COMPRADOR"]):
                    obs.append(f"Nome no Loteamento: {cli['nome']}")
                if int(res["quadra"]) != int(row["QD"]) or int(res["lote"]) != int(row["LT"]):
                    obs.append(f"Loteamento diz QD{res['quadra']} LT{res['lote']}")

                # 2) SIG: titular ainda é a Natela Empreendimentos? quadra/lote batem?
                from debitos import bloqueio_debito, consultar as consultar_debitos
                imovel, debitos_sig = consultar_debitos(sig, row["CCI"])
                if not imovel:
                    obs.append("CCI não encontrado no SIG")
                else:
                    df.loc[i, "TITULAR SIG"] = imovel["nome"]
                    tipo = classificar_titular(imovel["nome"], cli["nome"], r["anexos"], row["Nº VENDA"])
                    if tipo == "CLIENTE":
                        df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = [
                            "JÁ NO NOME", f"IPTU já está no nome do cliente ({imovel['nome']}) - não precisa protocolo"]
                        print("JÁ NO NOME do cliente")
                        continue
                    bloqueio = bloqueio_debito(debitos_sig)
                    if bloqueio:
                        df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["COM DÉBITO", bloqueio]
                        print("COM DÉBITO:", bloqueio)
                        continue
                    if tipo == "EX-COMPRADOR":
                        obs_titular = f"Titular no SIG é ex-comprador (distrato): {imovel['nome']}"
                    elif tipo == "OUTRO":
                        obs.append(f"Titular no SIG é outra pessoa: {imovel['nome']}")
                    if int(imovel["quadra"]) != int(row["QD"]) or int(imovel["lote"]) != int(row["LT"]):
                        obs.append(f"SIG diz QD{imovel['quadra']} LT{imovel['lote']}")

                # 3) Baixa os PDFs e gera miniaturas do DOC
                pasta = pasta_cliente(row)
                baixar(contrato["id"], pasta / "CONTRATO.pdf")
                baixar(doc["id"], pasta / "DOC_ORIGINAL.pdf")
                paginas = miniaturas(pasta / "DOC_ORIGINAL.pdf", pasta)
                baixados += 1
                confere = contrato_confere(pasta / "CONTRATO.pdf", cli["nome"], cli["cpf"])
                if confere is False:
                    obs.append("CONTRATO NÃO TEM o nome/CPF do cliente - pode ser de outra pessoa")
                elif confere is None:
                    obs.append("Contrato escaneado (sem texto) - conferir o nome manualmente")
                if contrato_do_lote(pasta / "CONTRATO.pdf", row["QD"], row["LT"]) is False:
                    obs.append(f"CONTRATO NÃO É DO LOTE QD{row['QD']} LT{row['LT']}")
                tam = (pasta / "CONTRATO.pdf").stat().st_size / 1e6
                if tam > 20:
                    obs.append(f"Contrato com {tam:.1f} MB (limite SEI 20 MB)")

                df.loc[i, "PASTA"] = str(pasta)
                df.loc[i, "PÁGINAS RG"] = "1"
                if obs:
                    df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["VERIFICAR", " | ".join(obs)]
                    print("VERIFICAR:", " | ".join(obs))
                else:
                    df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = [
                        "PREPARADO", " | ".join(x for x in (f"DOC com {paginas} páginas", obs_titular) if x)]
                    print("ok")
            except Exception as e:
                if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                    print("\n\n>>> O login do Loteamento expirou. Faça login de novo no navegador do robô "
                          "e rode o preparar outra vez. Nada foi alterado neste cliente.")
                    break
                df.loc[i, "OBSERVAÇÃO ROBÔ"] = f"Falha ao preparar: {e}"[:250]
                print("FALHA:", e)
            finally:
                # grava SÓ este cliente, na hora (nunca regrava os anteriores: a conferência pode ter
                # mudado algum deles enquanto o preparar continua rodando)
                salvar_linhas(df, [i])
    print("\nResumo:\n" + df["STATUS ROBÔ"].value_counts().to_string())
    print("\nPróximo passo: no painel, clique em Abrir conferência")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(int(args[0]) if args else None, reprocessar="--reprocessar" in sys.argv)
