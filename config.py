"""Configurações fixas do robô de protocolo SEI - Cidade Exemplo."""
import os
import sys
from pathlib import Path

# Empacotado (RoboSEI.exe): os dados (planilha, logins, etapa) ficam AO LADO do .exe.
CONGELADO = getattr(sys, "frozen", False)
AQUI = Path(sys.executable).parent if CONGELADO else Path(__file__).parent


def _pasta_desktop() -> Path:
    """Desktop real do usuário (funciona também quando o Desktop está no OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]
            desktop = GUID(0xB4BFCC3A, 0xDB2C, 0x424C, (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(desktop), 0, None, ctypes.byref(ptr)) == 0:
                caminho = Path(ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                if caminho.exists():
                    return caminho
        except Exception:
            pass
    return Path.home() / "Desktop"


def _pasta_downloads() -> Path:
    return Path.home() / "Downloads"

PASTA_CLIENTES = AQUI.parent                      # Desktop\robo-protocolo-sei
CONTROLE = AQUI / "CONTROLE PROTOCOLOS.xlsx"
# Pasta no Desktop com uma subpasta por cliente ("NOME - QDxx LTxx") e a Decisão
DOCUMENTOS = _pasta_desktop() / "PROTOCOLOS SEI CIDADE EXEMPLO"
DECISAO_PDF = DOCUMENTOS / "DECISAO JUDICIAL" / "Decisão.pdf"   # a mesma para todos
ETAPA_ATIVA = AQUI / "etapa_ativa.txt"          # etapa escolhida no painel
SMARTSHEET = _pasta_downloads() / "IPTU - CIDADE EXEMPLO.xlsx"  # exportação padrão
CDP = "http://127.0.0.1:9222"                     # navegador do robô (abrir_navegador.py)

# ---- Loteamento (sistema do loteamento) ----
API_LOTEAMENTO = "http://api-loteamento.exemplo.com.br/api"
API_TOTVS = "http://api-erp.exemplo.com.br/api/sgi"
URL_ANEXO = "https://arquivos.exemplo.com.br/api-loteamento/anexos/{id}.pdf"

# ---- SIG Prefeitura ----
# Campo "Código do imóvel (CCI)": o SIG trocou o campo numérico por texto (20/10/2026) -> aceita os dois.
# Na tela, o 1º campo de texto do formulário é o CCI (depois vêm CPF e CNPJ).
CAMPO_CCI = "input[type=numeric], input[id$='inputText']"
URL_SIG = "https://sig.prefeitura-exemplo.gov.br/sig/app.html#/servicosonline/debito-imovel"

# ---- SEI ----
URL_SEI = "https://sei.prefeitura-exemplo.gov.br/sei/"
TIPO_PROCESSO = "Tributária: Requerimento Geral - Usuário Externo"
ESPECIFICACAO = "troca de titularidade"
CARGO = "Gerente Administrativo"

# Documentos anexados: (tipo no SEI, complemento)
DOC_RG = ("Documento Pessoal: RG e CPF", "documento pessoal")
DOC_CONTRATO = ("Contrato de Compra e Venda do Imóvel", "contrato de compra e venda do imovel")
DOC_DECISAO = ("Procuração", "decisao judicial")

# ---- Dados fixos da empresa (requerimento) ----
EMPRESA = {
    "nome": "NATELA EMPREENDIMENTOS LTDA",
    "cnpj": "12.345.678/0001-90",
    "telefone": "11 99999-0000",
    # endereço FIXO da empresa (não muda por cliente; quadra/lote do cliente só vão no PEDIDO)
    "endereco": "Rua Exemplo, 100 – Residencial Natela",
    "cep": "00000-000",
    "bairro": "RURAL",
    "cidade": "Cidade Exemplo",
    "email": "contato@exemplo.com.br",
    "representante": "REPRESENTANTE LEGAL",
}

FUNDAMENTACAO = (
    "A NATELA EMPREENDIMENTOS LTDA, sociedade empresária limitada, "
    "inscrita no CNPJ sob o nº 12.345.678/0001-90, estabelecida na Rua Exemplo, nº 100, Bairro Centro, "
    "Cidade Exemplo/GO, CEP: 00000-000, vem, perante V. Sª. SOLICITAR:"
)
# Pedido no fim do requerimento: só nome do cliente e CCI (sem quadra/lote)
PEDIDO = "- A transferência de titularidade para cliente {nome} CCI({cci})."

# Quantas páginas do DOC aparecem na tela de conferência
PAGINAS_CONFERENCIA = 4
