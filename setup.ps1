# Установка окружения для бота (Python 3.12 рекомендуется)
# Запуск:  powershell -ExecutionPolicy Bypass -File .\setup.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-Python312 {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Python\pythoncore-3.12-64\python.exe",
        "C:\Program Files\Python312\python.exe",
        "C:\Python312\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    # py launcher
    try {
        $out = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out.Trim())) {
            return $out.Trim()
        }
    } catch {}
    return $null
}

$py = Find-Python312
if (-not $py) {
    Write-Host ""
    Write-Host "Не найден Python 3.12." -ForegroundColor Red
    Write-Host "Скачай и установи: https://www.python.org/downloads/release/python-31210/"
    Write-Host "Обязательно отметь: Add python.exe to PATH"
    Write-Host "Потом снова запусти: powershell -ExecutionPolicy Bypass -File .\setup.ps1"
    exit 1
}

Write-Host "Используем: $py" -ForegroundColor Green
& $py --version

if (Test-Path ".venv") {
    Write-Host "Удаляю старый .venv..."
    Remove-Item -Recurse -Force .venv
}

Write-Host "Создаю .venv..."
& $py -m venv .venv

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Не удалось создать .venv\Scripts\python.exe" -ForegroundColor Red
    exit 1
}

Write-Host "Ставлю зависимости..."
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt

Write-Host ""
Write-Host "Готово. Запуск бота:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\python.exe bot.py"
Write-Host ""
Write-Host "Перед запуском заполни BOT_TOKEN и OCR_SPACE_API_KEY в файле .env"
