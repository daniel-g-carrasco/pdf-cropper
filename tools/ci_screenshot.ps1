# CI helper: save a screenshot of the runner's desktop (the Windows GUI smoke
# test uses it to show what the window looked like).
#
#   powershell -NoProfile -File tools/ci_screenshot.ps1 out.png
param([string]$Path = "screenshot.png")

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bitmap.Save((Join-Path (Get-Location) $Path), [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
Write-Output "screenshot saved: $Path ($($bounds.Width)x$($bounds.Height))"
