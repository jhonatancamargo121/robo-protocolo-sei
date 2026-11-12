"""Funções compartilhadas: planilha de controle e conexão com o navegador do robô."""
from contextlib import contextmanager
import os
import re
import shutil
from pathlib import Path
import msvcrt
import threading
import time
import unicodedata

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from config import CDP, CONTROLE, DOCUMENTOS, ETAPA_ATIVA

# Planilha simples na pasta do Desktop, só com os protocolados (para atualizar o Smartsheet)
RESUMO = DOCUMENTOS / "PROTOCOLOS REALIZADOS.xlsx"
COLUNAS_RESUMO = {"ETAPA": "ETAPA", "COMPRADOR": "NOME", "CPF": "CPF", "QD": "QUADRA", "LT": "LOTE", "CCI": "CCI",
                  "NOVO PROTOCOLO SEI": "PROTOCOLO SEI", "DATA NOVO PROTOCOLO": "DATA",
                  "LINHA SMARTSHEET": "LINHA SMARTSHEET", "OBSERVAÇÃO ROBÔ": "OBSERVAÇÃO"}

COLUNAS = [
    "LINHA SMARTSHEET", "COMPRADOR", "CPF", "QD", "LT", "CCI", "Nº VENDA", "ETAPA",
    "PROCESSO ANTIGO", "DATA PROCESSO ANTIGO",
    "NOVO PROTOCOLO SEI", "DATA NOVO PROTOCOLO", "STATUS ROBÔ", "OBSERVAÇÃO ROBÔ",
    "PÁGINAS RG", "PASTA", "TITULAR SIG",
]
LARGURAS = [10, 42, 16, 6, 6, 10, 10, 10, 24, 14, 24, 14, 16, 55, 10, 40, 40]

# Fluxo de status:
# PENDENTE -> (preparar) PREPARADO | SEM ANEXO | VERIFICAR | JÁ NO NOME (IPTU já está no nome do cliente)
# PREPARADO -> (conferência) CONFERIDO | PULAR
# CONFERIDO -> (protocolar) EM ANDAMENTO (clicou Peticionar) -> PROTOCOLADO | ERRO
# EM ANDAMENTO que sobrou (robô interrompido) é resolvido pelos Recibos do SEI antes do próximo protocolo.


_CACHE = {"chave": None, "df": None}
_TRAVA_LOCAL = threading.RLock()
_TRAVA_N = {"n": 0, "f": None}


@contextmanager
def trava_planilha(espera_s: int = 120):
    """Cadeado entre as partes do robô (preparar, conferência, protocolar, painel):
    só uma por vez lê-altera-grava a planilha de controle, assim uma não apaga a alteração da outra."""
    with _TRAVA_LOCAL:
        if _TRAVA_N["n"] == 0:
            f = open(CONTROLE.with_name(CONTROLE.stem + ".trava"), "a+b")
            limite = time.time() + espera_s
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > limite:
                        f.close()
                        raise RuntimeError("A planilha de controle está ocupada por outra parte do robô. Tente de novo.")
                    time.sleep(0.1)
            _TRAVA_N["f"] = f
        _TRAVA_N["n"] += 1
        try:
            yield
        finally:
            _TRAVA_N["n"] -= 1
            if _TRAVA_N["n"] == 0:
                f = _TRAVA_N["f"]
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                f.close()
                _TRAVA_N["f"] = None


def ler_controle() -> pd.DataFrame:
    """Lê a planilha de controle. Guarda na memória e só relê quando o arquivo muda
    (assim leituras frequentes não seguram o arquivo e não atrapalham as gravações)."""
    st = os.stat(CONTROLE)
    chave = (str(CONTROLE), st.st_mtime_ns, st.st_size)
    if _CACHE["chave"] != chave:
        for tentativa in range(10):   # tenta de novo se pegar o arquivo no meio de uma gravação
            try:
                df = pd.read_excel(CONTROLE, dtype=str).fillna("")
                break
            except Exception:
                if tentativa == 9:
                    raise
                time.sleep(1)
        for c in COLUNAS:
            if c not in df.columns:
                df[c] = ""
        _CACHE.update(chave=chave, df=df[COLUNAS])
    return _CACHE["df"].copy()


def salvar_controle(df: pd.DataFrame, esperar: bool = True) -> None:
    """Grava a planilha de forma segura: monta num arquivo temporário e troca de uma vez.
    Se estiver aberta no Excel: espera fechar (esperar=True) ou dá erro."""
    tmp = CONTROLE.with_name(CONTROLE.stem + ".tmp.xlsx")
    df[COLUNAS].to_excel(tmp, index=False, sheet_name="Controle")
    wb = load_workbook(tmp)
    ws = wb.active
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    destaque = PatternFill("solid", fgColor="FFF2CC")
    col_proto = COLUNAS.index("NOVO PROTOCOLO SEI") + 1
    for row in ws.iter_rows(min_row=2, min_col=col_proto, max_col=col_proto):
        row[0].fill = destaque
    for i, w in enumerate(LARGURAS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(tmp)
    while not _trocar_arquivo(tmp, CONTROLE):
        if not esperar:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Feche a planilha '{CONTROLE.name}' no Excel e tente de novo.")
        print(f">>> Feche a planilha '{CONTROLE.name}' no Excel. Tentando de novo em 10s...", flush=True)
        time.sleep(10)
    salvar_resumo(df)


def ajustar_a_esta_maquina() -> str:
    """Deixa a planilha de controle certa para ESTE computador:
    - pastas de clientes apontam para a pasta de protocolos do Desktop desta máquina;
    - quem estava PREPARADO/VERIFICAR/CONFERIDO mas cujos documentos não estão nesta máquina
      volta para PENDENTE (o robô baixa tudo de novo aqui);
    - cria a planilha PROTOCOLOS REALIZADOS.xlsx desta máquina.
    Os PROTOCOLADOS, PULAR e SEM ANEXO não mudam (nada é protocolado de novo)."""
    DOCUMENTOS.mkdir(parents=True, exist_ok=True)
    if not CONTROLE.exists():
        return "Planilha de controle ainda não existe (será criada ao escolher a etapa)."
    with trava_planilha():
        return _ajustar()


def _ajustar() -> str:
    df = ler_controle()
    mudou, voltaram = False, 0
    for i, r in df.iterrows():
        if not r["PASTA"]:
            continue
        aqui = pasta_etapa(r["ETAPA"]) / Path(r["PASTA"]).name
        antiga = DOCUMENTOS / Path(r["PASTA"]).name          # antes as pastas ficavam direto na raiz
        if not aqui.exists() and antiga.exists():
            aqui.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(antiga), str(aqui))
            except OSError as e:                              # pasta aberta no Explorer, etc.
                print(f"Não consegui mover '{antiga.name}' para a pasta da etapa: {e}")
                aqui = antiga
        if str(aqui) != r["PASTA"]:
            df.at[i, "PASTA"], mudou = str(aqui), True
        precisa = ["CONTRATO.pdf", "DOC_ORIGINAL.pdf"] + (["RG E CPF.pdf"] if r["STATUS ROBÔ"] == "CONFERIDO" else [])
        if r["STATUS ROBÔ"] in ("PREPARADO", "VERIFICAR", "CONFERIDO") and not all((aqui / f).exists() for f in precisa):
            df.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ", "PÁGINAS RG"]] = [
                "PENDENTE", "Documentos não estão neste computador - preparar de novo", ""]
            mudou, voltaram = True, voltaram + 1
    if mudou:
        salvar_controle(df)
    elif not RESUMO.exists():
        salvar_resumo(df)
    return f"Planilha ajustada a este computador ({voltaram} cliente(s) voltaram para PENDENTE)."


def salvar_linhas(df: pd.DataFrame, linhas, esperar: bool = True) -> None:
    """Grava só as linhas que este processo mexeu, em cima da versão mais recente da planilha
    (assim a tela de conferência e o protocolo podem rodar juntos sem um apagar o outro)."""
    linhas = list(dict.fromkeys(linhas))
    if not linhas:
        return
    with trava_planilha():
        atual = ler_controle()
        for i in linhas:
            atual.loc[i, COLUNAS] = df.loc[i, COLUNAS].tolist()
        salvar_controle(atual, esperar=esperar)


def _trocar_arquivo(tmp, destino) -> bool:
    """Troca o arquivo; bloqueio curto (outra parte do robô lendo) é tentado de novo por ~6s."""
    for _ in range(75):              # ~15s
        try:
            os.replace(tmp, destino)
            return True
        except PermissionError:
            time.sleep(0.2)
    return False


def _gravar_planilha(arquivo: Path, abas: dict) -> None:
    """Grava {nome da aba: tabela} formatada. Se estiver aberta no Excel não trava o robô
    (é regravada na próxima atualização)."""
    try:
        with pd.ExcelWriter(arquivo, engine="openpyxl") as w:
            for nome, tab in abas.items():
                tab.to_excel(w, index=False, sheet_name=(str(nome) or "Sem etapa")[:31])
    except PermissionError:
        print(f">>> '{arquivo.name}' está aberta no Excel - será atualizada depois.", flush=True)
        return
    wb = load_workbook(arquivo)
    for ws in wb.worksheets:
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
        for i, w in enumerate([10, 42, 16, 8, 8, 10, 24, 12, 10, 60], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    wb.save(arquivo)


def salvar_resumo(df: pd.DataFrame) -> None:
    feitos = df[df["STATUS ROBÔ"].eq("PROTOCOLADO")][list(COLUNAS_RESUMO)].rename(columns=COLUNAS_RESUMO)
    feitos = feitos.sort_values(["ETAPA", "QUADRA", "LOTE"], key=lambda s: pd.to_numeric(s, errors="coerce")
                                if s.name in ("QUADRA", "LOTE") else s)
    DOCUMENTOS.mkdir(parents=True, exist_ok=True)
    etapas = sorted(feitos["ETAPA"].unique(), key=lambda e: (len(str(e)), str(e)))
    # geral: aba "Todas" + uma aba por etapa
    _gravar_planilha(RESUMO, {"Todas": feitos, **{e: feitos[feitos["ETAPA"].eq(e)] for e in etapas}})
    # uma planilha só de cada etapa, dentro da pasta da etapa: "4ª Etapa\PROTOCOLOS 4ª Etapa.xlsx"
    for e in etapas:
        pasta = pasta_etapa(e)
        pasta.mkdir(parents=True, exist_ok=True)
        _gravar_planilha(pasta / f"PROTOCOLOS {e}.xlsx", {e: feitos[feitos["ETAPA"].eq(e)]})


def etapa_ativa() -> str:
    """Etapa escolhida no painel ("" = todas)."""
    try:
        return ETAPA_ATIVA.read_text(encoding="utf-8-sig").strip()   # aceita arquivo salvo com BOM
    except OSError:
        return ""


def definir_etapa(etapa: str) -> None:
    ETAPA_ATIVA.write_text(etapa.strip(), encoding="utf-8")


def filtro_etapa(df: pd.DataFrame) -> pd.Series:
    e = etapa_ativa()
    return df["ETAPA"].eq(e) if e else pd.Series(True, index=df.index)


def sem_acento(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper()


def _nome_pasta(row) -> str:
    nome = re.sub(r'[\\/:*?"<>|]', "", row["COMPRADOR"]).strip()
    return f"{nome} - QD{int(row['QD']):02d} LT{int(row['LT']):02d}"


def pasta_etapa(etapa: str) -> Path:
    """Subpasta da etapa dentro de PROTOCOLOS SEI CIDADE EXEMPLO (ex.: "4ª Etapa")."""
    return DOCUMENTOS / (re.sub(r'[\\/:*?"<>|]', "", str(etapa)).strip() or "SEM ETAPA")


def pasta_cliente(row) -> Path:
    """Pasta do cliente: PROTOCOLOS SEI CIDADE EXEMPLO\\<etapa>\\NOME - QDxx LTxx (cria se não existir)."""
    p = pasta_etapa(row["ETAPA"]) / _nome_pasta(row)
    p.mkdir(parents=True, exist_ok=True)
    return p


def pasta_atual(row) -> Path:
    """Onde estão os documentos do cliente agora: pasta da etapa, a gravada na planilha ou o lugar antigo."""
    nome = Path(row["PASTA"]).name if row["PASTA"] else _nome_pasta(row)
    for p in (pasta_etapa(row["ETAPA"]) / nome, Path(row["PASTA"]) if row["PASTA"] else None, DOCUMENTOS / nome):
        if p is not None and p.name and p.exists():
            return p
    return Path(row["PASTA"])


def conectar_navegador(p):
    """Conecta ao navegador do robô já aberto (abrir_navegador.py) e logado."""
    try:
        browser = p.chromium.connect_over_cdp(CDP)
    except Exception:
        raise SystemExit(
            "Navegador do robô não está aberto. No painel clique em 'Abrir navegador do robô', "
            "faça login no Loteamento e no SEI e tente de novo."
        )
    return browser, browser.contexts[0]


def aba(ctx, trecho_url: str, url_abrir: str | None = None):
    for pg in ctx.pages:
        if trecho_url in pg.url:
            return pg
    if url_abrir:
        pg = ctx.new_page()
        pg.goto(url_abrir)
        return pg
    raise SystemExit(f"Não encontrei a aba '{trecho_url}' no navegador do robô. Ela está aberta e logada?")
