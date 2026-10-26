[CmdletBinding()]
param(
    [string]$ExePath,
    [string]$UserSid,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

function Resolve-BeamerUserSid([string]$Identity) {
    if ($Identity -match '^S-1-') {
        return ([Security.Principal.SecurityIdentifier]$Identity).Value
    }
    return ([Security.Principal.NTAccount]$Identity).Translate([Security.Principal.SecurityIdentifier]).Value
}

function Update-BeamerAutostart([string]$ExePath, [string]$UserSid, [switch]$Remove) {
    $expectedPath = [IO.Path]::GetFullPath($ExePath)
    foreach ($task in @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop)) {
        if ($task.TaskName -ne 'Beamer' -and $task.TaskName -notlike 'Beamer-*') { continue }
        [xml]$xml = Export-ScheduledTask -TaskName $task.TaskName -TaskPath '\' -ErrorAction Stop
        $ns = New-Object Xml.XmlNamespaceManager($xml.NameTable)
        $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
        $principals = $xml.SelectNodes('/t:Task/t:Principals/t:Principal', $ns)
        $actions = $xml.SelectNodes('/t:Task/t:Actions/*', $ns)
        if ($principals.Count -ne 1 -or $actions.Count -ne 1 -or $actions[0].LocalName -ne 'Exec') { continue }
        $principal = $principals[0]
        if ($principal.LogonType -ne 'InteractiveToken') { continue }
        try {
            if ((Resolve-BeamerUserSid $principal.UserId) -ne $UserSid) { continue }
            $command = [string]$actions[0].Command
            if (-not [IO.Path]::IsPathRooted($command)) { continue }
            if (-not [string]::Equals([IO.Path]::GetFullPath($command), $expectedPath, [StringComparison]::OrdinalIgnoreCase)) { continue }
        } catch { continue }
        if ($Remove) {
            Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath '\' -Confirm:$false -ErrorAction Stop
            continue
        }
        if ($principal.RunLevel -ne 'HighestAvailable') { continue }
        # Keep the existing task definition: disabled startup, arguments and logon triggers are user choices.
        $principal.RunLevel = 'LeastPrivilege'
        Register-ScheduledTask -TaskName $task.TaskName -TaskPath '\' -Xml $xml.OuterXml -Force -ErrorAction Stop | Out-Null
    }
}

function Remove-BeamerFirewall([string]$ExePath) {
    foreach ($name in @('Beamer Receiver (TCP-In)', 'Beamer Pairing (UDP-In)', 'Beamer Pairing (TCP-In)', 'Beamer Audio (UDP-In)')) {
        foreach ($rule in @(Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
            $filter = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
            if ([string]::Equals($filter.Program, $ExePath, [StringComparison]::OrdinalIgnoreCase)) {
                $rule | Remove-NetFirewallRule -ErrorAction Stop
            }
        }
    }
}

function Repair-BeamerFirewall([string]$ExePath) {
    Remove-BeamerFirewall -ExePath $ExePath
    $rules = @(
        @{ Name = 'Beamer Receiver (TCP-In)'; Protocol = 'TCP'; Port = 24820 },
        @{ Name = 'Beamer Pairing (UDP-In)'; Protocol = 'UDP'; Port = 24821 },
        @{ Name = 'Beamer Pairing (TCP-In)'; Protocol = 'TCP'; Port = 24821 },
        @{ Name = 'Beamer Audio (UDP-In)'; Protocol = 'UDP'; Port = 24822 }
    )
    foreach ($rule in $rules) {
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Protocol $rule.Protocol -LocalPort $rule.Port -Action Allow -Profile Private -Program $ExePath -ErrorAction Stop | Out-Null
    }
}

function Repair-BeamerGameInput {
    if (Get-Service -Name 'GameInputSvc' -ErrorAction SilentlyContinue) {
        try {
            Restart-Service -Name 'GameInputSvc' -ErrorAction Stop
        } catch {
            # The receiver can still run; its remote controller reports when a local click is needed.
            Write-Warning 'GameInput could not be restarted. A local click may be needed to release remote controller focus.'
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Installer maintenance requires administrator permission.'
        }
        if (-not [IO.Path]::IsPathRooted($ExePath) -or [IO.Path]::GetFileName($ExePath) -ine 'Beamer.exe' -or -not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
            throw 'Installer maintenance requires the installed Beamer.exe path.'
        }
        $ExePath = [IO.Path]::GetFullPath($ExePath)
        $UserSid = ([Security.Principal.SecurityIdentifier]$UserSid).Value
        Update-BeamerAutostart -ExePath $ExePath -UserSid $UserSid -Remove:$Remove
        if ($Remove) {
            Remove-BeamerFirewall -ExePath $ExePath
        } else {
            Repair-BeamerFirewall -ExePath $ExePath
            Repair-BeamerGameInput
        }
        exit 0
    } catch {
        Write-Error $_ -ErrorAction Continue
        exit 1
    }
}
