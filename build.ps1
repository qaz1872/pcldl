# 打包单文件 exe（需要先 pip install pyinstaller）
# 产物：dist/pcldl.exe

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 图标（改了 make_icon.py 后重新生成）
if (-not (Test-Path "pcldl.ico")) {
    python make_icon.py
}

python -m pip install --upgrade --quiet pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name pcldl --icon pcldl.ico `
    --distpath dist --workpath build --specpath . pcldlgui.py

Write-Host ""
Write-Host "完成：$PSScriptRoot\dist\pcldl.exe" -ForegroundColor Green
