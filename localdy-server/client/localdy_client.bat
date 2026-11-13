@echo off
rem Localdy 客户端启动器：双击运行 localdy_client.py，退出后暂停看结果。
rem 全局模式：直接双击启动即可播放服务端已索引的全部视频，无需输入路径。
rem 新路径会话（临时独立，只看这一个文件夹，不影响全局模式）：
rem   localdy_client.py --root "D:\Videos"
rem 会话记录存在客户端本地的 localdy_sessions.json 里（按文件夹分条），
rem 关掉不丢失，下次再打开同一个文件夹直接恢复；删掉文件即忘掉。
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1
if %errorlevel%==0 (
  python localdy_client.py %*
) else (
  py localdy_client.py %*
)
echo.
echo 客户端已退出，按任意键关闭窗口。
pause >nul
