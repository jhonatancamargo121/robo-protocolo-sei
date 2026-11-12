"""ROBÔ DE DÉBITOS DO IPTU - antes de transferir, confere se o IPTU do lote tem débitos.

Só LÊ os sistemas (não altera nada no SIG nem no Loteamento). Uso:
    python debitos.py <exportação Smartsheet.xlsx> "3ª Etapa"     -> uma etapa
    python debitos.py <exportação Smartsheet.xlsx> TODAS
    --refazer   -> consulta de novo quem já foi consultado hoje

Para cada lote "TROCAR TITULAR P/ ATUAL COMPRADOR" ou "VOLTAR NOME DA LOTEADORA" da etapa:
  1. SIG da Prefeitura: titular do CCI e TODOS os débitos (botão "Exibir Débitos")
  2. se o titular não é a Natela Empreendimentos nem o comprador atual (= antigo comprador):
     Loteamento/TOTVS -> a venda dele nesse lote foi distratada?
  3. grava em Desktop\\PROTOCOLOS SEI CIDADE EXEMPLO\\DEBITOS IPTU.xlsx
     (aba Débitos = 1 linha por lote, aba Parcelas = cada parcela em aberto,
      aba Dinâmica = tabela dinâmica do Excel, aba Resumo = totais)

Cada consulta fica salva na hora (debitos_consultas.json): se parar no meio, continua de onde parou.
"""
from datetime import date, datetime
import json
import os
import random
import re
import subprocess
import sys
import tempfile

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from playwright.sync_api import sync_playwright

from comum import conectar_navegador, sem_acento
from config import AQUI, API_TOTVS, DOCUMENTOS, URL_SIG, CAMPO_CCI
from preparar import nome_parecido

PLANILHA = DOCUMENTOS / "DEBITOS IPTU.xlsx"
CONSULTAS = AQUI / "debitos_consultas.json"
SITUACOES = ("TROCAR TITULAR P/ ATUAL COMPRADOR", "VOLTAR NOME DA LOTEADORA")
PAUSA_MS = (1500, 3000)   # ritmo humano entre consultas

# Loteamento/TOTVS: vendas do antigo comprador NESTE lote (busca pelo CPF do SIG e, se não achar, pelo nome)
JS_VENDAS_DO_LOTE = """async ([api, termos, qd, lt]) => {
  const H = {'Authorization': 'Bearer ' + localStorage.getItem('tokenLoteamento')};
  const ip = localStorage.getItem('ipUsuario') || '';
  let pessoa = null;
  for (const t of termos) {
    if (!t) continue;
    const r = await fetch(api + '/cliente/com-vendas/nome-ou-cgc/' + encodeURIComponent(t) + '?ip=' + ip, {headers: H});
    if (!r.ok) return {erro: 'cliente HTTP ' + r.status};
    for (const c of await r.json()) {
      pessoa = pessoa || c.nome;
      const v = await fetch(api + '/venda/cod-cfo/' + c.codcfo + '?ip=' + ip, {headers: H});
      if (!v.ok) continue;
      const vendas = (await v.json()).filter(x => +x.numUnidade === +qd && +x.numSubUnidade === +lt);
      if (vendas.length) return {nome: c.nome, cpf: c.cgccfo,
                                 vendas: vendas.map(x => ({venda: x.numVenda, situacao: x.situacao}))};
    }
  }
  return {nao_achou: true, pessoa};
}"""


# ---------------------------------------------------------------- consultas
def abrir_sig(ctx):
    """Aba do SIG pronta na tela "Consulta de débito do imóvel". Se a sessão do SIG expirou, ele joga a aba para
    a página inicial (sig/index.html), onde não existe o campo do CCI: aqui ela volta para a tela certa."""
    sig = next((pg for pg in ctx.pages if "sig.prefeitura-exemplo.gov.br" in pg.url), None) or ctx.new_page()
    ir_para_consulta(sig)
    return sig


def ir_para_consulta(sig):
    if "debito-imovel" not in sig.url or not sig.locator(CAMPO_CCI).count():
        sig.goto(URL_SIG, timeout=60000)
    sig.locator(CAMPO_CCI).first.wait_for(timeout=60000)


def consultar(sig, cci, tentativas=3):
    """(imóvel, débitos) do SIG. O SIG às vezes responde vazio/instável: tenta de novo antes de desistir."""
    for t in range(1, tentativas + 1):
        try:
            return _consultar(sig, cci)
        except Exception:
            if t == tentativas:
                raise RuntimeError(f"O SIG da Prefeitura não respondeu direito ao consultar o CCI {cci} "
                                   f"({tentativas} tentativas)")
            print(f"   - SIG instável, tentando de novo ({t + 1}/{tentativas})...", flush=True)
            try:
                sig.keyboard.press("Escape")
                sig.goto(URL_SIG, timeout=60000)          # volta para a tela de consulta (não só recarrega)
                sig.locator(CAMPO_CCI).first.wait_for(timeout=60000)
            except Exception:
                pass
            sig.wait_for_timeout(5000)


def _consultar(sig, cci):
    """(imóvel, débitos) do SIG, pelo mesmo caminho da tela: Pesquisar > Exibir Débitos."""
    ir_para_consulta(sig)
    sig.locator(CAMPO_CCI).first.fill(str(cci))
    with sig.expect_response(lambda r: "pesquisarImoveis" in r.url, timeout=30000) as ri:
        sig.locator('button[ng-click="vm.pesquisar()"]').click()
    imoveis = ri.value.json()
    if not imoveis:
        return None, None
    sig.wait_for_timeout(random.randint(600, 1200))
    with sig.expect_response(lambda r: "pesquisarDebitos" in r.url, timeout=60000) as rd:
        sig.get_by_text("EXIBIR DÉBITOS", exact=False).first.click()
    debitos = rd.value.json()
    sig.wait_for_timeout(random.randint(600, 1200))
    sig.keyboard.press("Escape")          # fecha a janela de débitos
    sig.wait_for_timeout(500)
    return imoveis[0], debitos


def resumir(debitos, hoje: date):
    """Totais das parcelas em aberto (a 'Única' é alternativa às parcelas: só conta se não houver parcelas)."""
    itens = debitos.get("listaRetorno") or [] if debitos else []
    parcelas = [x for x in itens if str(x.get("parcela_descricao")).upper() != "UNICA"] or itens
    abertas = []
    for x in parcelas:
        valor = float(x.get("vl_divida") or 0)
        if valor <= 0:
            continue
        venc = datetime.strptime(x["vencimento"][:10], "%Y-%m-%d").date() if x.get("vencimento") else None
        abertas.append({
            "EXERCÍCIO": x.get("exercicio"), "RECEITA": x.get("receita"), "PARCELA": x.get("parcela_descricao"),
            "VENCIMENTO": venc.strftime("%d/%m/%Y") if venc else "", "VALOR": round(valor, 2),
            "SITUAÇÃO PARCELA": "VENCIDA" if venc and venc < hoje else "A VENCER",
            "DÍVIDA ATIVA": "SIM" if x.get("divida_ativa") else "", "EXECUÇÃO": "SIM" if x.get("divida_ativa_executada") else "",
            "PROTESTO": "SIM" if x.get("protesto_cartorio") else "",
            "PROCESSO": "" if x.get("processo_descricao") in (None, "", "Inexistente") else x.get("processo_descricao"),
        })
    vencidas = [a for a in abertas if a["SITUAÇÃO PARCELA"] == "VENCIDA"]
    return abertas, {
        "TOTAL EM ABERTO": round(sum(a["VALOR"] for a in abertas), 2),
        "TOTAL VENCIDO": round(sum(a["VALOR"] for a in vencidas), 2),
        "PARCELAS VENCIDAS": len(vencidas),
        "EXERCÍCIOS COM DÉBITO": ", ".join(sorted({str(a["EXERCÍCIO"]) for a in abertas})),
        "DÉBITO ANOS ANTERIORES": "SIM" if any(int(a["EXERCÍCIO"] or 0) < hoje.year for a in abertas) else "",
        "DÍVIDA ATIVA": "SIM" if any(a["DÍVIDA ATIVA"] for a in abertas) else "",
        "PROTESTO/EXECUÇÃO": "SIM" if any(a["PROTESTO"] or a["EXECUÇÃO"] or a["PROCESSO"] for a in abertas) else "",
        "ALERTA": ("COM DÉBITO VENCIDO" if vencidas else "SÓ PARCELAS A VENCER" if abertas else "SEM DÉBITO"),
    }


def consulta_salva(cci):
    """Quando o SIG está fora do ar: titular e débito pela última verificação de débitos salva
    (debitos_consultas.json = planilha DEBITOS IPTU). As parcelas guardadas são reavaliadas com a data
    de HOJE (parcela que venceu depois da verificação conta como vencida).
    Devolve (imóvel, bloqueio, data da verificação) ou None se o CCI não foi verificado."""
    r = ler_consultas().get(str(cci))
    if not r or not r.get("TITULAR NA PREFEITURA (SIG)") or r.get("ALERTA") == "ERRO NA CONSULTA":
        return None
    hoje = date.today()
    vencidas = [p for p in r.get("parcelas", [])
                if p.get("VENCIMENTO") and datetime.strptime(p["VENCIMENTO"], "%d/%m/%Y").date() < hoje]
    bloqueio = ""
    if vencidas:
        total = sum(float(p["VALOR"]) for p in vencidas)
        bloqueio = (f"IPTU com débito vencido: R$ {total:.2f} em {len(vencidas)} parcela(s) "
                    f"(planilha de débitos de {r['DATA CONSULTA']}) - não protocolar")
    imovel = {"nome": r["TITULAR NA PREFEITURA (SIG)"], "quadra": r["QD"], "lote": r["LT"]}
    return imovel, bloqueio, r["DATA CONSULTA"]


def bloqueio_debito(debitos) -> str:
    """Regra do protocolo: lote com débito VENCIDO no IPTU não é protocolado. "" = pode protocolar."""
    _, tot = resumir(debitos, date.today())
    if tot["TOTAL VENCIDO"] > 0:
        return (f"IPTU com débito vencido: R$ {tot['TOTAL VENCIDO']:.2f} em {tot['PARCELAS VENCIDAS']} parcela(s)"
                f" (exercícios {tot['EXERCÍCIOS COM DÉBITO']}) - não protocolar")
    return ""


def tipo_titular(titular, comprador):
    if "Natela Empreendimentos" in sem_acento(titular):
        return "Natela Empreendimentos"
    if comprador and (nome_parecido(comprador, titular) or nome_parecido(titular, comprador)):
        return "COMPRADOR ATUAL"
    return "ANTIGO COMPRADOR / OUTRO"


def distrato(lot, imovel, qd, lt):
    """Situação da venda do antigo comprador neste lote no Loteamento."""
    cpf = re.sub(r"\D", "", str(imovel.get("cgc") or ""))
    termos = []
    if 0 < len(cpf) <= 11:
        cpf = cpf.zfill(11)
        termos += [cpf, f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"]
    termos.append(sem_acento(imovel["nome"]))
    r = lot.evaluate(JS_VENDAS_DO_LOTE, [API_TOTVS, termos, qd, lt])
    if "erro" in r:
        raise RuntimeError(f"Loteamento: {r['erro']} (login expirou?)")
    if r.get("nao_achou"):
        quem = "pessoa achada, mas sem venda neste lote" if r.get("pessoa") else "pessoa não encontrada"
        return "NÃO CONFIRMADO", f"Loteamento: {quem}"
    sits = [v["situacao"] or "" for v in r["vendas"]]
    texto = "; ".join(f"venda {v['venda']}: {v['situacao']}" for v in r["vendas"])
    if any(re.search(r"DISTRAT|CANCEL|RESCI", sem_acento(s)) for s in sits):
        return "SIM", texto
    return "NÃO", texto


# ---------------------------------------------------------------- dados
def ler_consultas() -> dict:
    try:
        return json.loads(CONSULTAS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gravar_consultas(c: dict) -> None:
    tmp = CONSULTAS.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, CONSULTAS)


def lotes(planilha: str, etapa: str) -> pd.DataFrame:
    ss = pd.read_excel(planilha)
    for c in ("QD", "LT", "MATRICULA PREFEITURA", "Nº VENDA"):
        ss[c] = pd.to_numeric(ss[c], errors="coerce")
    sel = ss[ss["SITUAÇÃO"].isin(SITUACOES) & ss["MATRICULA PREFEITURA"].notna() & ss["QD"].notna() & ss["LT"].notna()]
    if etapa not in ("", "TODAS"):
        sel = sel[sel["Etapa"].eq(etapa)]
    return pd.DataFrame({
        "ETAPA": sel["Etapa"].fillna("").astype(str), "QD": sel["QD"].astype(int), "LT": sel["LT"].astype(int),
        "CCI": sel["MATRICULA PREFEITURA"].astype(int).astype(str),
        "NOVO COMPRADOR": sel["COMPRADOR"].fillna("").astype(str).str.strip(),
        "Nº VENDA": sel["Nº VENDA"].fillna(0).astype(int).astype(str).replace("0", ""),
        "SITUAÇÃO SMARTSHEET": sel["SITUAÇÃO"], "TITULAR NO SMARTSHEET": sel["TITULARIDADE NA PREFEITURA"].fillna("").astype(str),
        "LINHA SMARTSHEET": (sel.index + 2).astype(str),
    }).sort_values(["ETAPA", "QD", "LT"])


# ---------------------------------------------------------------- planilha
COLS = ["ETAPA", "QD", "LT", "CCI", "TITULAR NA PREFEITURA (SIG)", "TIPO TITULAR", "ANTIGO COMPRADOR",
        "DISTRATO", "SITUAÇÃO NO LOTEAMENTO", "NOVO COMPRADOR", "Nº VENDA", "ALERTA", "TOTAL EM ABERTO",
        "TOTAL VENCIDO", "PARCELAS VENCIDAS", "EXERCÍCIOS COM DÉBITO", "DÉBITO ANOS ANTERIORES", "DÍVIDA ATIVA",
        "PROTESTO/EXECUÇÃO", "SITUAÇÃO SMARTSHEET", "LINHA SMARTSHEET", "DATA CONSULTA", "OBSERVAÇÃO"]
COLS_PARC = ["ETAPA", "QD", "LT", "CCI", "TITULAR NA PREFEITURA (SIG)", "TIPO TITULAR", "DISTRATO", "NOVO COMPRADOR",
             "EXERCÍCIO", "RECEITA", "PARCELA", "VENCIMENTO", "VALOR", "SITUAÇÃO PARCELA", "DÍVIDA ATIVA",
             "EXECUÇÃO", "PROTESTO", "PROCESSO"]
CORES = {"COM DÉBITO VENCIDO": "F8CBAD", "SÓ PARCELAS A VENCER": "FFF2CC", "SEM DÉBITO": "C6EFCE", "ERRO NA CONSULTA": "D9D9D9"}


def montar_planilha(consultas: dict) -> None:
    linhas = sorted(consultas.values(), key=lambda r: (r["ETAPA"], int(r["QD"]), int(r["LT"])))
    deb = pd.DataFrame([{c: r.get(c, "") for c in COLS} for r in linhas], columns=COLS)
    parc = pd.DataFrame([{**{c: r.get(c, "") for c in COLS_PARC}, **p} for r in linhas for p in r.get("parcelas", [])],
                        columns=COLS_PARC)
    DOCUMENTOS.mkdir(parents=True, exist_ok=True)
    tmp = PLANILHA.with_name(PLANILHA.stem + ".tmp.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        deb.to_excel(w, sheet_name="Débitos", index=False)
        parc.to_excel(w, sheet_name="Parcelas", index=False)
        resumo(deb).to_excel(w, sheet_name="Resumo", index=False)
    wb = load_workbook(tmp)
    for ws, nome in ((wb["Débitos"], "TabDebitos"), (wb["Parcelas"], "TabParcelas"), (wb["Resumo"], "TabResumo")):
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
        for i, cell in enumerate(ws[1], 1):
            ws.column_dimensions[get_column_letter(i)].width = max(10, min(45, len(str(cell.value)) + 4))
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            t = Table(displayName=nome, ref=ws.dimensions)
            t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
            ws.add_table(t)
    ws = wb["Débitos"]
    col_alerta = COLS.index("ALERTA") + 1
    for row in ws.iter_rows(min_row=2):
        cor = CORES.get(row[col_alerta - 1].value)
        if cor:
            row[col_alerta - 1].fill = PatternFill("solid", fgColor=cor)
    for c in ("TOTAL EM ABERTO", "TOTAL VENCIDO"):
        for row in ws.iter_rows(min_row=2, min_col=COLS.index(c) + 1, max_col=COLS.index(c) + 1):
            row[0].number_format = 'R$ #,##0.00'
    for row in wb["Parcelas"].iter_rows(min_row=2, min_col=COLS_PARC.index("VALOR") + 1, max_col=COLS_PARC.index("VALOR") + 1):
        row[0].number_format = 'R$ #,##0.00'
    wb.save(tmp)
    try:
        os.replace(tmp, PLANILHA)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        print(f">>> '{PLANILHA.name}' está aberta no Excel - feche e rode de novo para atualizar (as consultas estão salvas).")
        return
    if tabela_dinamica(PLANILHA):
        print("Tabela dinâmica criada na aba 'Dinâmica'.")
    print(f"Planilha: {PLANILHA}")


def resumo(deb: pd.DataFrame) -> pd.DataFrame:
    if deb.empty:
        return pd.DataFrame(columns=["ETAPA", "TIPO TITULAR", "ALERTA", "LOTES", "TOTAL EM ABERTO", "TOTAL VENCIDO"])
    return (deb.groupby(["ETAPA", "TIPO TITULAR", "ALERTA"], dropna=False)
               .agg(LOTES=("CCI", "count"), **{"TOTAL EM ABERTO": ("TOTAL EM ABERTO", "sum"),
                                               "TOTAL VENCIDO": ("TOTAL VENCIDO", "sum")})
               .reset_index())


# Tabela dinâmica de verdade (Excel instalado). Sem Excel, fica só a aba Resumo.
PS_DINAMICA = r"""
$ErrorActionPreference = 'Stop'
$x = New-Object -ComObject Excel.Application
$x.Visible = $false; $x.DisplayAlerts = $false
try {
  $wb = $x.Workbooks.Open($args[0])
  $ws = $wb.Worksheets.Add($wb.Worksheets.Item(1))
  $ws.Name = 'Dinâmica'
  $pc = $wb.PivotCaches().Create(1, 'TabDebitos')
  $pt = $pc.CreatePivotTable($ws.Range('A6'), 'DinamicaDebitos')
  $f = $pt.PivotFields('ETAPA');            $f.Orientation = 1; $f.Position = 1
  $f = $pt.PivotFields('TIPO TITULAR');     $f.Orientation = 1; $f.Position = 2
  $f = $pt.PivotFields('ALERTA');           $f.Orientation = 2
  $f = $pt.PivotFields('DISTRATO');         $f.Orientation = 3
  $f = $pt.PivotFields('DÍVIDA ATIVA');     $f.Orientation = 3
  $d = $pt.AddDataField($pt.PivotFields('CCI'), 'Qtde de lotes', -4112)
  $d = $pt.AddDataField($pt.PivotFields('TOTAL VENCIDO'), 'Total vencido (R$)', -4157)
  $d.NumberFormat = 'R$ #.##0,00'
  $ws.Range('A1').Value2 = 'Débitos de IPTU - arraste os campos para analisar (filtros: Distrato, Dívida ativa)'
  $ws.Range('A1').Font.Bold = $true
  $wb.Save(); $wb.Close($true)
} finally { $x.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($x) }
"""


def tabela_dinamica(arquivo) -> bool:
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
            f.write(PS_DINAMICA)
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", f.name, str(arquivo)],
                           capture_output=True, text=True, timeout=180, creationflags=0x08000000)
        os.unlink(f.name)
        if r.returncode != 0:
            print("Tabela dinâmica não criada (Excel indisponível?) - use a aba Resumo.", r.stderr.strip()[-200:])
        return r.returncode == 0
    except Exception as e:
        print("Tabela dinâmica não criada:", e)
        return False


# ---------------------------------------------------------------- principal
def main(planilha: str, etapa: str, refazer=False):
    fila = lotes(planilha, etapa)
    consultas = ler_consultas()
    hoje = date.today()
    falta = [r for _, r in fila.iterrows()
             if refazer or consultas.get(r["CCI"], {}).get("DATA CONSULTA") != hoje.strftime("%d/%m/%Y")
             or consultas[r["CCI"]].get("ALERTA") == "ERRO NA CONSULTA"]
    print(f"Etapa: {etapa or 'todas'} - {len(fila)} lotes; {len(falta)} para consultar hoje")
    if falta:
        with sync_playwright() as p:
            # SIG é público: consulta num navegador invisível próprio (rápido e não atrapalha a tela);
            # do navegador do robô só usamos o login do Loteamento (para o distrato)
            sig = p.chromium.launch().new_page()
            sig.goto(URL_SIG)
            sig.locator(CAMPO_CCI).first.wait_for(timeout=60000)
            try:
                _, ctx = conectar_navegador(p)
                lot = next((pg for pg in ctx.pages if "loteamento.exemplo.com.br" in pg.url), None)
            except SystemExit:
                lot = None
            if not lot:
                print(">>> Loteamento não está aberto/logado: o distrato não será conferido (só os débitos).")
            for n, r in enumerate(falta, 1):
                reg = {**r.to_dict(), "QD": int(r["QD"]), "LT": int(r["LT"]),
                       "DATA CONSULTA": hoje.strftime("%d/%m/%Y"), "parcelas": []}
                print(f"[{n}/{len(falta)}] QD{r['QD']:02d} LT{r['LT']:02d} CCI {r['CCI']} ...", end=" ", flush=True)
                try:
                    imovel, debitos = consultar(sig, r["CCI"])
                    if not imovel:
                        reg.update({"ALERTA": "ERRO NA CONSULTA", "OBSERVAÇÃO": "CCI não encontrado no SIG"})
                    else:
                        tipo = tipo_titular(imovel["nome"], r["NOVO COMPRADOR"])
                        parcelas, tot = resumir(debitos, hoje)
                        reg.update(tot)
                        reg.update({"TITULAR NA PREFEITURA (SIG)": imovel["nome"], "TIPO TITULAR": tipo,
                                    "parcelas": parcelas})
                        obs = []
                        if (int(imovel["quadra"] or 0), int(imovel["lote"] or 0)) != (int(r["QD"]), int(r["LT"])):
                            obs.append(f"SIG diz QD{imovel['quadra']} LT{imovel['lote']}")
                        if tipo == "ANTIGO COMPRADOR / OUTRO":
                            reg["ANTIGO COMPRADOR"] = imovel["nome"]
                            if lot:
                                reg["DISTRATO"], reg["SITUAÇÃO NO LOTEAMENTO"] = distrato(lot, imovel, r["QD"], r["LT"])
                            else:
                                reg["DISTRATO"] = "NÃO CONFERIDO"
                        reg["OBSERVAÇÃO"] = " | ".join(obs)
                    print(reg.get("ALERTA"), f"R$ {reg.get('TOTAL VENCIDO', 0):.2f} vencido" if reg.get("TOTAL VENCIDO") else "",
                          f"| distrato: {reg['DISTRATO']}" if reg.get("DISTRATO") else "")
                except Exception as e:
                    if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                        print("\n>>> O login do Loteamento expirou. Faça login de novo e rode outra vez (continua de onde parou).")
                        break
                    reg.update({"ALERTA": "ERRO NA CONSULTA", "OBSERVAÇÃO": str(e)[:200]})
                    print("ERRO:", e)
                    try:
                        sig.keyboard.press("Escape")
                    except Exception:
                        pass
                consultas[r["CCI"]] = reg
                gravar_consultas(consultas)
                if n % 100 == 0:
                    montar_planilha(consultas)
                sig.wait_for_timeout(random.randint(*PAUSA_MS))
    montar_planilha(consultas)
    sel = [consultas[c] for c in fila["CCI"] if c in consultas]
    if sel:
        print("\nResumo desta etapa:")
        print(pd.Series([s.get("ALERTA", "") for s in sel]).value_counts().to_string())
        print(f"Total vencido: R$ {sum(float(s.get('TOTAL VENCIDO') or 0) for s in sel):,.2f}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit("Uso: python debitos.py <exportação do Smartsheet.xlsx> [etapa|TODAS]")
    main(args[0], args[1] if len(args) > 1 else "TODAS", refazer="--refazer" in sys.argv)
