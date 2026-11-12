# ASCII-only on purpose: runs under Windows PowerShell 5.1 regardless of BOM.
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
function CJK([int[]]$codes) { -join ($codes | ForEach-Object { [char]$_ }) }

$Account = 1
$accountPrefix = (CJK @(0x8D26, 0x53F7)) + ' 1' + [char]0xFF1A   # zhang hao 1 (fullwidth colon)
$ok     = CJK @(0x9884, 0x7EA6, 0x6210, 0x529F)                   # yu yue cheng gong
$partial = (CJK @(0x90E8, 0x5206)) + $ok                           # bu fen + ok
$fail   = CJK @(0x9884, 0x7EA6, 0x5931, 0x8D25)                   # yu yue shi bai
$skip   = (CJK @(0x672C, 0x6B21)) + (CJK @(0x9884, 0x7EA6)) + (CJK @(0x5DF2, 0x8DF3, 0x8FC7))  # ben ci yu yue yi tiao guo
$unknown = CJK @(0x7ED3, 0x679C, 0x672A, 0x77E5)                   # jie guo wei zhi
$seatTaken = CJK @(0x5DF2, 0x88AB, 0x9884, 0x7EA6)                 # yi bei yu yue
$alreadyHeld = CJK @(0x5DF2, 0x6709, 0x9884, 0x7EA6)                # yi you yu yue
$verifiedTag = [char]0xFF08 + (CJK @(0x5DF2, 0x6838, 0x5BF9)) + [char]0xFF09   # (yi he dui)
$noRecords = CJK @(0x6CA1, 0x6709, 0x67E5, 0x5230, 0x9884, 0x7EA6, 0x8BB0, 0x5F55)  # no records found
$verifyFailed = CJK @(0x6838, 0x5BF9, 0x5931, 0x8D25)               # he dui shi bai
$reservationInfo = CJK @(0x9884, 0x7EA6, 0x4FE1, 0x606F)            # yu yue xin xi

$log = [IO.Path]::GetTempFileName()
try {
    $cases = @(
        @{ Code=0; Events=@(@{status='complete'}); Expected=$ok },
        @{ Code=1; Events=@(@{status='partial_success'}); Expected=$partial },
        @{ Code=1; Events=@(@{status='failed';error='Timeout'}); Expected=$fail },
        @{ Code=0; Events=@(@{skipped='other account succeeded'}); Expected=$skip },
        @{ Code=1; Events=@(@{status='complete'}); Expected=$fail },
        @{ Code=0; Events=@(@{status='complete'}, @{timing='http';method='POST';path='/data/apps/seat/submit';status=200}); Expected=$ok },
        @{ Code=0; Events=@(@{timing='http';method='POST';path='/data/apps/seat/submit';status=200}); Expected=$unknown },
        @{ Code=0; Events=@(@{timing='http';method='POST';path='/data/apps/seat/submit';error='ReadTimeout'}, @{status='failed';target=@('15:00','22:00');missing=@()}); Expected=$fail },
        @{ Code=1; Events=@(@{success=$false;time=@('15:00','19:00');seatid=@('144');message='';reason='seat_taken'}, @{status='failed';target=@('15:00','22:00');missing=@(@('15:00','19:00'))}); Expected=$fail; Contains=$seatTaken },
        @{ Code=0; Events=@(@{success=$false;time=@('19:00','22:00');seatid=@('142');message='';reason='already_held'}, @{status='complete';target=@('11:00','22:00')}); Expected=$ok; Contains=$alreadyHeld },
        @{ Code=1; Events=@(@{status='failed';target=@('10:00','22:00')}, @{verify='account_result';account_index=1;verified='complete';reservations=@(@{time=@('11:00','15:00');seat='144';room='11031';id=9;status=0});missing=@()}); Expected=$ok; Contains=$verifiedTag; Contains2='144'; Contains3=$reservationInfo },
        @{ Code=1; Events=@(@{status='failed';target=@('10:00','22:00')}, @{verify='account_result';account_index=1;verified='none';reservations=@();missing=@(@('11:00','15:00'))}); Expected=$fail; Contains=$noRecords },
        @{ Code=1; Events=@(@{status='failed';target=@('10:00','22:00')}, @{verify='account_result';account_index=1;verified='unavailable';error='ReadTimeout';message='' }); Expected=$fail; Contains=$verifyFailed }
    )
    foreach ($case in $cases) {
        @($case.Events | ForEach-Object { $_ | ConvertTo-Json -Compress }) | Set-Content -LiteralPath $log -Encoding UTF8
        Show-ReservationResult $case.Code
        $want = "*$accountPrefix$($case.Expected)*"
        if ($script:shown -notlike $want) { throw "Wrong popup: $script:shown" }
        if ($case['Contains'] -and $script:shown -notlike "*$($case['Contains'])*") { throw "Missing detail: $script:shown" }
        if ($case['Contains2'] -and $script:shown -notlike "*$($case['Contains2'])*") { throw "Missing detail: $script:shown" }
        if ($case['Contains3'] -and $script:shown -notlike "*$($case['Contains3'])*") { throw "Missing detail: $script:shown" }
    }
    '13 popup classification checks passed (no popup displayed).'
} finally {
    Remove-Item -LiteralPath $log
}
