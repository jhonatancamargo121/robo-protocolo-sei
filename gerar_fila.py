"""Gera (ou atualiza) a planilha de controle a partir da exportação do Smartsheet.

Uso: python gerar_fila.py "C:\\caminho\\IPTU - CIDADE EXEMPLO.xlsx" "2ª Etapa"

Linhas que o robô já trabalhou (qualquer status diferente de PENDENTE) são preservadas.
"""
import sys

import pandas as pd

from comum import COLUNAS, ler_controle, salvar_controle, sem_acento, trava_planilha
from config import CONTROLE


def chave(r) -> str:
    """Identifica o cliente entre exportações: nº da venda (ou nome + quadra + lote se faltar)."""
    return r["Nº VENDA"] or f"{sem_acento(r['COMPRADOR'])}|{int(r['QD'])}|{int(r['LT'])}"

# Já reprotocolados manualmente antes do robô (nº venda -> protocolo, data)
JA_PROTOCOLADOS = {
    "63780": ("00349.0016493/2026-61", "02/10/2026"),  # RENATA RAMOS DE AZEVEDO (vídeo)
}


def gerar(planilha: str, etapa: str) -> None:
    df = pd.read_excel(planilha)
    df["LINHA SMARTSHEET"] = df.index + 2  # linha 1 é o cabeçalho
    # números digitados errado no Smartsheet (ex.: data no lugar do nº da venda) não travam a fila
    for c in ("QD", "LT", "MATRICULA PREFEITURA", "Nº VENDA"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    sel = df[
        df["Etapa"].eq(etapa)
        & df["SITUAÇÃO"].eq("TROCAR TITULAR P/ ATUAL COMPRADOR")
        & df["TITULARIDADE NA PREFEITURA"].astype(str).str.contains("Natela Empreendimentos", case=False)
        & df["COMPRADOR"].notna()
        & df["MATRICULA PREFEITURA"].notna()
        & df["QD"].notna() & df["LT"].notna()
    ]

    fila = pd.DataFrame({
        "LINHA SMARTSHEET": sel["LINHA SMARTSHEET"].astype(str),
        "COMPRADOR": sel["COMPRADOR"].astype(str).str.strip(),
        "QD": sel["QD"].astype(int).astype(str),
        "LT": sel["LT"].astype(int).astype(str),
        "CCI": sel["MATRICULA PREFEITURA"].astype(int).astype(str),
        "Nº VENDA": sel["Nº VENDA"].fillna(0).astype(int).astype(str).replace("0", ""),
        "ETAPA": sel["Etapa"],
        "PROCESSO ANTIGO": sel["NUMERO PROCESSO SEI"].fillna("").astype(str),
        "DATA PROCESSO ANTIGO": pd.to_datetime(sel["DATA ABERTURA PROCESSO SEI"], errors="coerce")
                                  .dt.strftime("%d/%m/%Y").fillna(""),
    })
    for c in COLUNAS:
        if c not in fila.columns:
            fila[c] = ""
    fila["STATUS ROBÔ"] = "PENDENTE"

    # "NOVO PROCESSO" já preenchido no Smartsheet (protocolo feito pela equipe à mão)
    novo_ss = (sel["NOVO PROCESSO"].fillna("").astype(str).str.strip().str.strip('"')
               if "NOVO PROCESSO" in sel.columns else pd.Series("", index=sel.index))
    for i, row in fila.iterrows():
        if row["Nº VENDA"] in JA_PROTOCOLADOS:
            proto, data = JA_PROTOCOLADOS[row["Nº VENDA"]]
            fila.loc[i, ["NOVO PROTOCOLO SEI", "DATA NOVO PROTOCOLO", "STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = \
                [proto, data, "PULAR", "Reprotocolado manualmente antes do robô"]

    with trava_planilha():
        outras = pd.DataFrame(columns=COLUNAS)
        sobras = pd.DataFrame(columns=COLUNAS)
        if CONTROLE.exists():
            atual = ler_controle()
            outras = atual[atual["ETAPA"].ne(etapa)]          # outras etapas ficam como estão
            # o que o robô já trabalhou nesta etapa, pela VENDA (o nº da linha muda a cada exportação)
            feitos = {chave(r): r for _, r in atual[atual["ETAPA"].eq(etapa)].iterrows()
                      if r["STATUS ROBÔ"] not in ("", "PENDENTE")}
            for i, row in fila.iterrows():
                velho = feitos.pop(chave(row), None)
                if velho is not None:
                    fila.loc[i, COLUNAS[1:]] = velho[COLUNAS[1:]].tolist()
            # trabalhados que saíram do filtro do Smartsheet (ex.: titular já trocado) continuam registrados
            sobras = pd.DataFrame(list(feitos.values()), columns=COLUNAS)

        # NUNCA protocolar de novo o que a equipe já lançou no "NOVO PROCESSO" do Smartsheet
        # (vale também para quem já estava preparado/conferido no robô; o que o robô protocolou não muda)
        for i in fila.index:
            v = novo_ss.get(i, "")
            if v and v != fila.at[i, "NOVO PROTOCOLO SEI"] and fila.at[i, "STATUS ROBÔ"] not in ("PROTOCOLADO", "EM ANDAMENTO"):
                fila.loc[i, ["STATUS ROBÔ", "OBSERVAÇÃO ROBÔ"]] = ["PULAR", f"Smartsheet já tem NOVO PROCESSO: {v} (feito pela equipe)"]

        fila = fila.sort_values(["QD", "LT"], key=lambda s: s.astype(int))
        tudo = pd.concat([x for x in (outras, fila[COLUNAS], sobras) if len(x)], ignore_index=True)
        salvar_controle(tudo)
    if len(sobras):
        print(f"{len(sobras)} cliente(s) já trabalhados não estão mais no filtro do Smartsheet (mantidos na planilha).")
    print(f"{etapa}: {len(fila)} clientes na fila ({len(tudo)} no total em {CONTROLE.name})")
    print(fila["STATUS ROBÔ"].value_counts().to_string())


def etapas_da_planilha(planilha: str) -> list[str]:
    e = pd.read_excel(planilha, usecols=["Etapa"])["Etapa"].dropna().astype(str)
    return sorted({x for x in e if "Etapa" in x}, key=lambda x: (len(x), x))


if __name__ == "__main__":
    # etapa vazia ou "TODAS" = monta/atualiza a fila de todas as etapas do Smartsheet
    alvo = sys.argv[2] if len(sys.argv) > 2 else "TODAS"
    for etapa in (etapas_da_planilha(sys.argv[1]) if alvo in ("", "TODAS") else [alvo]):
        gerar(sys.argv[1], etapa)
