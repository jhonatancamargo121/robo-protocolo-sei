"""Ponto de entrada do RoboSEI.exe (versão portátil, sem precisar instalar Python).

  RoboSEI.exe                      -> abre o PAINEL (sem janela) no navegador
  RoboSEI.exe preparar 20          -> as partes do robô, chamadas pelo próprio painel
  RoboSEI.exe protocolar [--apresentacao]
  RoboSEI.exe conferencia | gerar_fila <planilha> <etapa> | demonstracao
"""
import os
import runpy
import shutil
import subprocess
import sys

from config import AQUI, CONGELADO, DECISAO_PDF, DOCUMENTOS

PARTES = {"painel", "preparar", "protocolar", "conferencia", "gerar_fila", "demonstracao", "debitos"}


def diagnostico():
    """RoboSEI.exe diagnostico -> confere se esta máquina tem tudo para o robô rodar."""
    from playwright.sync_api import sync_playwright
    from config import CONTROLE, SMARTSHEET
    print("Pasta do robô ......", AQUI)
    print("Pasta protocolos ...", DOCUMENTOS, "OK" if DOCUMENTOS.exists() else "FALTA")
    print("Decisão judicial ...", DECISAO_PDF, "OK" if DECISAO_PDF.exists() else "FALTA")
    print("Planilha controle ..", CONTROLE, "OK" if CONTROLE.exists() else "(será criada ao escolher a etapa)")
    print("Smartsheet padrão ..", SMARTSHEET, "OK" if SMARTSHEET.exists() else "não encontrado (escolha no painel)")
    with sync_playwright() as p:
        exe = p.chromium.executable_path
    print("Navegador do robô ..", exe, "OK" if os.path.exists(exe) else "FALTA")


SEM_JANELA = 0x08000000  # CREATE_NO_WINDOW


def primeira_vez():
    """Prepara a máquina: navegador embutido, pasta dos protocolos e a Decisão judicial."""
    if CONGELADO:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(AQUI / "ms-playwright"))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    DOCUMENTOS.mkdir(parents=True, exist_ok=True)
    modelo = AQUI / "Decisão.pdf"
    if not DECISAO_PDF.exists() and modelo.exists():
        DECISAO_PDF.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(modelo, DECISAO_PDF)


def main():
    import warnings
    warnings.filterwarnings("ignore")          # avisos técnicos do Excel não aparecem no painel
    for fluxo in (sys.stdout, sys.stderr):     # acentos certos no quadro "Andamento"
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    primeira_vez()
    args = sys.argv[1:]
    if args[:1] == ["diagnostico"]:
        return diagnostico()
    # duplo clique no .exe: reabre o painel escondido (sem janela preta) e sai
    if not args and CONGELADO and os.environ.get("ROBO_PAINEL") != "1":
        env = dict(os.environ, ROBO_PAINEL="1")
        subprocess.Popen([sys.executable, "painel"], cwd=AQUI, env=env, creationflags=SEM_JANELA)
        return
    parte = args[0] if args and args[0] in PARTES else "painel"
    resto = args[1:] if args and args[0] in PARTES else args
    sys.argv = [parte + ".py", *resto]
    runpy.run_module(parte, run_name="__main__")


if __name__ == "__main__":
    main()
