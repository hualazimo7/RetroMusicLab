@echo off
chcp 65001 >nul
title WorkBuddy 配置迁移到 D 盘
echo.
echo 请先**完全退出 WorkBuddy**（含托盘图标），然后按任意键继续...
pause >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0migrate_workbuddy_config.ps1"
echo.
echo 按任意键关闭...
pause >nul
