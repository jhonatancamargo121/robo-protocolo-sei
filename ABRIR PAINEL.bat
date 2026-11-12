@echo off
rem Abre o painel do robo sem janela preta (se ja estiver aberto, so abre a pagina)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
start "" pythonw -W ignore painel.py
