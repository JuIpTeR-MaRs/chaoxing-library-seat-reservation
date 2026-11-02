$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot '..\run-local.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$null, [ref]$null)
$definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Show-ReservationResult' }, $true)
. ([scriptblock]::Create($definition.Extent.Text))
function New-Object {
    param([string]$ComObject)
    $mock = [pscustomobject]@{}
    $mock | Add-Member ScriptMethod Popup { param($message, $seconds, $title, $flags) $script:shown = $message; return 1 }
    return $mock
}
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle)
    $command = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($ArgumentList[-1]))
    if ($command -notmatch 'Popup\(\$text, 0,') { throw 'Popup must wait for manual dismissal' }
    & ([scriptblock]::Create($command))
}
$Account = 1
$log = [IO.Path]::GetTempFileName()
try {
    $cases = @(
        @{ Code=0; Event=@{status='complete'}; Expected='预约成功' },
        @{ Code=1; Event=@{status='partial_success'}; Expected='部分预约成功' },
        @{ Code=1; Event=@{status='failed';error='Timeout'}; Expected='预约失败' },
        @{ Code=0; Event=@{skipped='other account succeeded'}; Expected='本次预约已跳过' },
        @{ Code=1; Event=@{status='complete'}; Expected='预约失败' }
    )
    foreach ($case in $cases) {
        $case.Event | ConvertTo-Json -Compress | Set-Content -LiteralPath $log -Encoding UTF8
        Show-ReservationResult $case.Code
        if ($script:shown -notlike "*账号 1：$($case.Expected)*") { throw "Wrong popup: $script:shown" }
    }
    '5 popup classification checks passed (no popup displayed).'
} finally {
    Remove-Item -LiteralPath $log
}
