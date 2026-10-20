# Drives the real Inno Setup wizard, including the pages skipped by /VERYSILENT.
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class InstallerUi {
    public delegate bool EnumProc(IntPtr window, IntPtr parameter);
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string name);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumProc callback, IntPtr parameter);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr window);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr window, StringBuilder text, int capacity);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr window, StringBuilder text, int capacity);
    [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr window);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr window, out Rect rect);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="SendMessageW")] public static extern IntPtr Send(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="SendMessageW")] public static extern IntPtr SetText(IntPtr window, uint message, IntPtr wparam, string text);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="SendMessageW")] public static extern IntPtr ReadText(IntPtr window, uint message, IntPtr wparam, StringBuilder text);
    public static IntPtr[] Children(IntPtr parent) {
        var list = new List<IntPtr>();
        EnumChildWindows(parent, (window, data) => { if (IsWindowVisible(window)) list.Add(window); return true; }, IntPtr.Zero);
        return list.ToArray();
    }
    public static string ClassName(IntPtr window) { var s=new StringBuilder(256); GetClassName(window,s,s.Capacity); return s.ToString(); }
    public static string Text(IntPtr window) { var s=new StringBuilder(1024); GetWindowText(window,s,s.Capacity); return s.ToString(); }
    public static string FieldText(IntPtr window) { var s=new StringBuilder(1024); ReadText(window,0x000D,new IntPtr(s.Capacity),s); return s.ToString(); }
    public static void Click(IntPtr window) { Send(window,0x00F5,IntPtr.Zero,IntPtr.Zero); }
}
'@
function Invoke-InstallerRepair {
    param([string]$Installer, [Security.SecureString]$Password)
    $process = Start-Process $Installer -ArgumentList '/SP- /LANG=english /SUPPRESSMSGBOXES /NORESTART' -PassThru
    $choiceSeen = $false
    $fieldsSeen = $false
    try {
        for ($attempt=0; $attempt -lt 180; $attempt++) {
            if ($process.HasExited) { break }
            $wizard = [InstallerUi]::FindWindow('TWizardForm', $null)
            if ($wizard -eq [IntPtr]::Zero) { Start-Sleep -Milliseconds 500; continue }
            $children = @([InstallerUi]::Children($wizard))
            $editChoice = @($children | Where-Object { [InstallerUi]::ClassName($_) -notmatch 'Edit' -and [InstallerUi]::Text($_) -eq 'Изменить подключение к PostgreSQL' })
            if ($editChoice.Count -gt 0) {
                [InstallerUi]::Click($editChoice[0]); $choiceSeen=$true
            }
            $edits = @($children | Where-Object { [InstallerUi]::ClassName($_) -match 'Edit' } | Sort-Object { $rect=New-Object InstallerUi+Rect; [InstallerUi]::GetWindowRect($_,[ref]$rect) | Out-Null; $rect.Top })
            if ($edits.Count -eq 5) {
                if (-not $choiceSeen) { throw 'Connection fields appeared without the saved-settings choice.' }
                if (-not $fieldsSeen) {
                    Check ([InstallerUi]::FieldText($edits[0]) -eq 'incorrect-host.example') 'Wizard prefills the saved server address after uninstall'
                    Check ([InstallerUi]::FieldText($edits[1]) -eq '1') 'Wizard prefills the saved PostgreSQL port'
                    Check ([InstallerUi]::FieldText($edits[4]) -eq '') 'Wizard never exposes the saved password'
                    Check ([InstallerUi]::Send($edits[4],0x00D2,[IntPtr]::Zero,[IntPtr]::Zero) -ne [IntPtr]::Zero) 'New password input is masked'
                    foreach ($edit in $edits) {
                        $rect=New-Object InstallerUi+Rect; $parent=New-Object InstallerUi+Rect
                        [InstallerUi]::GetWindowRect($edit,[ref]$rect) | Out-Null
                        [InstallerUi]::GetWindowRect([InstallerUi]::GetParent($edit),[ref]$parent) | Out-Null
                        Check ($rect.Bottom -le $parent.Bottom -and $rect.Top -ge $parent.Top) 'Connection input fits on the visible wizard page'
                    }
                    $values = @('127.0.0.1','55438','toolstock','postgres')
                    for ($i=0; $i -lt 4; $i++) { [InstallerUi]::SetText($edits[$i],0x000C,[IntPtr]::Zero,$values[$i]) | Out-Null }
                    $pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
                    try { [InstallerUi]::SetText($edits[4],0x000C,[IntPtr]::Zero,[Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)) | Out-Null }
                    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
                    $fieldsSeen=$true
                }
            }
            $button = @($children | Where-Object { [InstallerUi]::ClassName($_) -match 'Button' -and [InstallerUi]::IsWindowEnabled($_) -and ([InstallerUi]::Text($_) -replace '&','') -match '^(Next >|Install|Finish)$' } | Select-Object -First 1)
            if ($button.Count -gt 0) { [InstallerUi]::Click($button[0]) }
            Start-Sleep -Milliseconds 700
        }
        if (-not $process.HasExited) { throw 'Interactive installer timed out.' }
        Check ($choiceSeen -and $fieldsSeen) 'Reinstallation offers editing and displays the connection fields'
        Check ($process.ExitCode -eq 0) 'Interactive repair installer completes successfully'
    } finally {
        if (-not $process.HasExited) { & taskkill.exe /PID $process.Id /T /F | Out-Null }
    }
}
