"""Abre o navegador do robô (perfil próprio, separado do seu Chrome).

Faça login no Loteamento e no SEI nessa janela. O login fica salvo no perfil
"perfil_navegador" e o robô se conecta a essa janela pela porta 9222.
"""
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

AQUI = Path(__file__).parent
PERFIL = AQUI / "perfil_navegador"
DOWNLOADS = AQUI / "documentos"

URLS = [
    "http://loteamento.exemplo.com.br",
    "https://sei.prefeitura-exemplo.gov.br/sei/controlador_externo.php?acao=usuario_externo_logar&id_orgao_acesso_externo=0",
]

with sync_playwright() as p:
    exe = p.chromium.executable_path

PERFIL.mkdir(exist_ok=True)
DOWNLOADS.mkdir(exist_ok=True)
subprocess.Popen([
    exe,
    f"--user-data-dir={PERFIL}",
    "--remote-debugging-port=9222",
    "--no-first-run",
    "--no-default-browser-check",
    *URLS,
])
print("Navegador do robô aberto. Faça login no Loteamento e no SEI.")
