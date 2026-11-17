@echo off
REM dy-filter 本地来源客户端启动脚本（Windows）
REM 用法：把下面的 ROOT_DIR 改成你的视频目录（可再加一行 --root），双击运行。
REM 运行后会打印订阅链接，粘贴到 dy-filter 任务中心「本地来源」卡片，点「同步本地」。
REM 可选加速：先在 dy-filter 任务中心「本地来源」卡片下载缓存清单 dy-cache-manifest.json，
REM 放到本脚本同目录，再把下面 set CACHE_MANIFEST=dy-cache-manifest.json 的 REM 去掉，
REM 客户端会对清单里有的文件直接复用清单里的哈希（115 文件用全文件哈希、本地文件用头部哈希）、不读文件现算；清单里没有的照常现算。
chcp 65001 >nul
cd /d "%~dp0"

set ROOT_DIR=D:\Videos
set PORT=18950
REM set CACHE_MANIFEST=dy-cache-manifest.json

if defined CACHE_MANIFEST (
  set CACHE_ARG=--cache "%CACHE_MANIFEST%"
) else (
  set CACHE_ARG=
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 local_client.py --root "%ROOT_DIR%" --port %PORT% %CACHE_ARG%
) else (
  python local_client.py --root "%ROOT_DIR%" --port %PORT% %CACHE_ARG%
)

echo.
echo 客户端已退出，按任意键关闭窗口。
pause >nul
