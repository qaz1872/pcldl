# 打包单文件 exe（需要先 pip install pyinstaller）
# 产物：dist/pcldl.exe

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --upgrade --quiet pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name pcldl `
    --distpath dist --workpath build --specpath . pcldlgui.py

Write-Host ""
Write-Host "完成：$PSScriptRoot\dist\pcldl.exe" -ForegroundColor Green
