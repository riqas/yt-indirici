@echo off
REM ============================================================
REM YT Indirici - baslatici
REM Baska PC'ye kopyalaninca da calissin diye her acilista kontrol eder:
REM  1) ffmpeg yoksa uygulama klasorune tasinabilir kopya indirir (hash dogrulamali; winget gerekmez)
REM  2) .venv baska PC'den gelmis/bozuksa siler, yeniden kurar
REM  3) Python 3.12 yoksa python.org'dan indirir, hash'ini dogrular, kurar
REM  4) Paket eksikse (yt-dlp, PyQt6, deno) kurar, sonra uygulamayi acar
REM Test: YTI_DRYRUN=1 uygulamayi acmaz / YTI_TEST_NOFFMPEG=1 sistemdeki ffmpeg'i yok sayar
REM ============================================================
setlocal EnableExtensions
chcp 65001 >nul
title YT Indirici
cd /d "%~dp0"
set "APPDIR=%~dp0"
set "PYURL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
REM python.org resmi dosyasinin SHA-256'si (2026-10-18 dogrulandi)
set "PYSHA=67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
REM Gyan'in resmi ffmpeg 9.0.2 essentials paketi (GitHub release digest'i, 2026-10-21 indirilip dogrulandi)
set "FFURL=https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
set "FFSHA=60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
REM winget ile kurulmus ffmpeg varsa PATH yenilenmeden de bulunsun
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%ProgramFiles%\WinGet\Links;%PATH%"

REM ---------- 1) ffmpeg: uygulama klasorundeki kopya > sistemdeki > indir
if exist "%APPDIR%ffmpeg\bin\ffmpeg.exe" goto :ffmpeg_local
if not defined YTI_TEST_NOFFMPEG (
  where ffmpeg >nul 2>nul
  if not errorlevel 1 goto :venv
)
call :install_ffmpeg || exit /b 1
:ffmpeg_local
set "PATH=%APPDIR%ffmpeg\bin;%PATH%"

REM ---------- 2) .venv saglam mi? (baska PC'den kopyalanan .venv burada patlar)
:venv
REM pythonw.exe pencere (GUI) programi olmali; uv'nin kurdugu venv'de konsol programi cikiyor ve yaninda
REM siyah pencere aciliyor. Calismayan ya da boyle kurulmus .venv silinip yeniden kurulur.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import struct,sys;b=open(sys.argv[1],'rb').read(4096);o=struct.unpack_from('<I',b,60)[0];sys.exit(struct.unpack_from('<H',b,o+92)[0]!=2)" ".venv\Scripts\pythonw.exe" >nul 2>&1
  if errorlevel 1 (
    echo  Eski/bozuk kurulum bulundu, yeniden kuruluyor...
    rmdir /s /q ".venv"
  )
)
if exist ".venv\Scripts\pythonw.exe" goto :packages

REM ---------- 3) Python + .venv (venv her zaman Python'un kendi moduluyle; uv yalniz paket kurar)
echo  Ilk kurulum yapiliyor, bir kere surer...
call :find_py
if not defined PYDIR call :install_py || exit /b 1
"%PYDIR%\python.exe" -m venv .venv || goto :fail

REM ---------- 4) paketler
:packages
".venv\Scripts\python.exe" -c "import yt_dlp, yt_dlp_ejs, PyQt6.QtWidgets, deno" >nul 2>&1
if not errorlevel 1 goto :run
echo  Gerekli paketler kuruluyor (yt-dlp, PyQt6, deno)...
where uv >nul 2>nul
if not errorlevel 1 (
  uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt || goto :fail
) else (
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto :fail
)

:run
if defined YTI_DRYRUN (
  echo [DRYRUN] kurulum tamam, uygulama acilmadi
  exit /b 0
)
REM ilk "" pencere basligidir; o olmadan start, pythonw yolunu baslik sanip hicbir sey acmiyordu
start "" "%APPDIR%.venv\Scripts\pythonw.exe" "%APPDIR%app.py"
exit /b 0

REM ---------------------------------------------------------- yardimcilar
:find_py
set "PYDIR="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYDIR=%LOCALAPPDATA%\Programs\Python\Python312"
if not defined PYDIR if exist "%ProgramFiles%\Python312\python.exe" set "PYDIR=%ProgramFiles%\Python312"
if not defined PYDIR for /f "delims=" %%P in ('py -3.12 -c "import sys,os;print(os.path.dirname(sys.executable))" 2^>nul') do set "PYDIR=%%P"
exit /b 0

:install_py
echo  Python 3.12 bulunamadi. Indiriliyor (yaklasik 26 MB)...
set "INST=%TEMP%\yt_indirici_python_%RANDOM%.exe"
curl.exe -L --fail --progress-bar -o "%INST%" "%PYURL%"
if errorlevel 1 powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing -Uri '%PYURL%' -OutFile '%INST%'"
if not exist "%INST%" (
  echo  [HATA] Python indirilemedi. Internet baglantini kontrol et.
  pause
  exit /b 1
)
set "GOT="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%INST%" SHA256') do if not defined GOT set "GOT=%%H"
set "GOT=%GOT: =%"
if /i not "%GOT%"=="%PYSHA%" (
  del "%INST%" >nul 2>&1
  echo  [HATA] Indirilen Python dogrulanamadi ^(bozuk ya da degistirilmis^). Kurulmadi.
  pause
  exit /b 1
)
echo  Python kuruluyor, lutfen bekle (1-2 dakika)...
"%INST%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 InstallLauncherAllUsers=0 Include_test=0 Include_doc=0 Include_tcltk=0
del "%INST%" >nul 2>&1
call :find_py
if defined PYDIR exit /b 0
echo  [HATA] Python kurulamadi. Elle kur: https://www.python.org/downloads/release/python-31210/
echo  Kurarken "Add python.exe to PATH" kutusunu isaretle.
pause
exit /b 1

:install_ffmpeg
echo  ffmpeg bulunamadi. Indiriliyor (yaklasik 110 MB, bir kere)...
set "FZIP=%TEMP%\yt_indirici_ffmpeg_%RANDOM%.zip"
set "FTMP=%TEMP%\yt_indirici_ffmpeg_%RANDOM%"
curl.exe -L --fail --progress-bar -o "%FZIP%" "%FFURL%"
if errorlevel 1 powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing -Uri '%FFURL%' -OutFile '%FZIP%'"
if not exist "%FZIP%" goto :ffmpeg_winget
set "GOT="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%FZIP%" SHA256') do if not defined GOT set "GOT=%%H"
set "GOT=%GOT: =%"
if /i not "%GOT%"=="%FFSHA%" (
  del "%FZIP%" >nul 2>&1
  echo  [UYARI] Indirilen ffmpeg dogrulanamadi, kullanilmadi.
  goto :ffmpeg_winget
)
echo  ffmpeg aciliyor...
mkdir "%FTMP%" >nul 2>&1
tar -xf "%FZIP%" -C "%FTMP%" >nul 2>&1
if errorlevel 1 powershell -NoProfile -Command "Expand-Archive -LiteralPath '%FZIP%' -DestinationPath '%FTMP%' -Force"
del "%FZIP%" >nul 2>&1
mkdir "%APPDIR%ffmpeg\bin" >nul 2>&1
for /d %%D in ("%FTMP%\ffmpeg-*") do (
  copy /y "%%D\bin\ffmpeg.exe" "%APPDIR%ffmpeg\bin\" >nul
  copy /y "%%D\bin\ffprobe.exe" "%APPDIR%ffmpeg\bin\" >nul
)
rmdir /s /q "%FTMP%" >nul 2>&1
if exist "%APPDIR%ffmpeg\bin\ffmpeg.exe" exit /b 0

:ffmpeg_winget
REM yedek yol: winget (Microsoft'un paket yoneticisi) varsa onunla kur
where winget >nul 2>nul || goto :ffmpeg_fail
echo  winget ile deneniyor...
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
where ffmpeg >nul 2>nul && exit /b 0

:ffmpeg_fail
echo  [HATA] ffmpeg kurulamadi. Internet baglantini kontrol edip tekrar dene.
echo  Olmazsa elle: https://www.gyan.dev/ffmpeg/builds/ (release essentials) indir,
echo  icindeki bin klasorunu bu klasore "ffmpeg\bin" olarak koy.
pause
exit /b 1

:fail
echo  [HATA] Kurulum basarisiz. Yukaridaki hata mesajina bak; cozemezsen GitHub'da issue ac.
pause
exit /b 1
