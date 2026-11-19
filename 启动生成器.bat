@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动桌宠生成器...

where python >nul 2>nul
if %errorlevel% equ 0 (
  python pet_builder.py
  goto :eof
)

where pythonw >nul 2>nul
if %errorlevel% equ 0 (
  pythonw pet_builder.py
  goto :eof
)

where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 pet_builder.py
  goto :eof
)

echo 未找到 PATH 中的 Python，正在查找 WorkBuddy 内置 Python...
for /d %%V in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
  if exist "%%V\python.exe" (
    "%%V\python.exe" pet_builder.py
    goto :eof
  )
)

echo.
echo 启动失败：未找到可用的 Python。
echo 请安装 Python 3.9+ 并加入 PATH，且确认 PySide6 / PyInstaller 已安装。
pause
