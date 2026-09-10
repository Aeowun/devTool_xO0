Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$Screen = [System.Windows.Forms.Screen]::PrimaryScreen
$Width = $Screen.Bounds.Width
$Height = $Screen.Bounds.Height
$Left = $Screen.Bounds.Left
$Top = $Screen.Bounds.Top
$Bitmap = New-Object System.Drawing.Bitmap($Width, $Height)
$Graphics = [System.Drawing.Graphics]::FromImage($Bitmap)
$Graphics.CopyFromScreen($Left, $Top, 0, 0, $Bitmap.Size)
$Path = "$PSScriptRoot\screenshot.png"
$Bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
$Graphics.Dispose()
$Bitmap.Dispose()
Write-Host "Screenshot saved to: $Path"
