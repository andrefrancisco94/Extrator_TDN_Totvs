# Configura o console cmd:
#  - Tamanho da janela maior
#  - Buffer com 5000 linhas (habilita scrollback)
#  - Fonte Cascadia Mono via SetCurrentConsoleFontEx (fallback p/ Consolas)
#
# Chamado pelo iniciar.bat logo apos a elevacao.
# Falhas sao silenciosas (cada bloco em try/catch).

# --- 1) Tamanho de janela + buffer (scroll) -------------------------------
try {
    $maxW = [System.Console]::LargestWindowWidth
    $maxH = [System.Console]::LargestWindowHeight
    $targetW = [Math]::Min(140, $maxW)
    $targetH = [Math]::Min(38, $maxH)

    $curBufW = [System.Console]::BufferWidth
    $curBufH = [System.Console]::BufferHeight

    # Ordem importa: buffer precisa ser >= janela. Para encolher janela
    # primeiro, depois ajustar buffer; para expandir, buffer primeiro.
    if ($targetW -gt $curBufW -or $targetH -gt $curBufH) {
        [System.Console]::SetBufferSize($targetW, [Math]::Max(5000, $curBufH))
        [System.Console]::SetWindowSize($targetW, $targetH)
    } else {
        [System.Console]::SetWindowSize($targetW, $targetH)
        [System.Console]::SetBufferSize($targetW, 5000)
    }
} catch {
    # Em Windows Terminal estas APIs podem nao se aplicar; ignora.
}

# --- 2) Fonte ------------------------------------------------------------
# Pula font tweaking se PowerShell esta em ConstrainedLanguage (AppLocker/DeviceGuard):
# Add-Type nao funciona nesse modo, evita exception ruidosa.
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    return
}

try {
    Add-Type -ErrorAction Stop -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class ConFont {
    [StructLayout(LayoutKind.Sequential)]
    public struct COORD {
        public short X;
        public short Y;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CONSOLE_FONT_INFOEX {
        public uint cbSize;
        public uint nFont;
        public COORD dwFontSize;
        public ushort FontFamily;
        public ushort FontWeight;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string FaceName;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetCurrentConsoleFontEx(
        IntPtr hConsoleOutput,
        bool MaximumWindow,
        ref CONSOLE_FONT_INFOEX info);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetCurrentConsoleFontEx(
        IntPtr hConsoleOutput,
        bool MaximumWindow,
        ref CONSOLE_FONT_INFOEX info);

    [DllImport("kernel32.dll")]
    public static extern IntPtr GetStdHandle(int nStdHandle);
}
"@

    $STD_OUTPUT_HANDLE = -11
    $hOut = [ConFont]::GetStdHandle($STD_OUTPUT_HANDLE)

    # FontFamily = FF_MODERN(0x30) | TMPF_TRUETYPE(0x04) | TMPF_VECTOR(0x02) = 54
    $FF_MODERN_TT = 54

    # Tenta fontes na ordem de preferencia
    $candidates = @("Cascadia Mono", "Cascadia Code", "Consolas", "Lucida Console")

    foreach ($name in $candidates) {
        $info = New-Object ConFont+CONSOLE_FONT_INFOEX
        $info.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf($info)
        $info.FaceName = $name
        $info.FontWeight = 400
        $info.FontFamily = $FF_MODERN_TT

        $coord = New-Object ConFont+COORD
        $coord.X = 0
        $coord.Y = 16
        $info.dwFontSize = $coord

        $ok = [ConFont]::SetCurrentConsoleFontEx($hOut, $false, [ref]$info)
        if ($ok) { break }
    }
} catch {
    # Sem permissao ou Windows Terminal: ignora.
}
