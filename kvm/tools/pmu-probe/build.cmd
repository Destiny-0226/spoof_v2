@echo off
setlocal
cd /d "%~dp0"
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -prerelease -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSROOT=%%i"
if not defined VSROOT exit /b 1
call "%VSROOT%\VC\Auxiliary\Build\vcvarsall.bat" x64
if errorlevel 1 exit /b 1
set "KIT=%ProgramFiles(x86)%\Windows Kits\10"
set "VER=10.0.28000.0"
if not exist build mkdir build
cl /nologo /W4 /WX /O2 /MT client.c /Fe:build\ovo-pmu-probe.exe /Fo:build\client.obj
if errorlevel 1 exit /b 1
cl /nologo /c /W4 /WX /O2 /Oi /GS /kernel /Zl /D_AMD64_ /DAMD64 /D_WIN64 /D_WIN32_WINNT=0x0A00 /DNTDDI_VERSION=0x0A000000 /I"%KIT%\Include\%VER%\km" /I"%KIT%\Include\%VER%\shared" /I"%KIT%\Include\%VER%\km\crt" driver.c /Fo:build\driver.obj
if errorlevel 1 exit /b 1
link /nologo /driver /subsystem:native /entry:GsDriverEntry /nodefaultlib /integritycheck /release /out:build\ovo-pmu-probe.sys build\driver.obj /libpath:"%KIT%\Lib\%VER%\km\x64" ntoskrnl.lib hal.lib wdmsec.lib BufferOverflowK.lib
exit /b %errorlevel%
