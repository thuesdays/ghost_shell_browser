# ════════════════════════════════════════════════════════════════
#  build_wizard_images.ps1 — derive every wizard / GitHub-banner
#  asset from two high-resolution sources kept under installer\assets\:
#
#      banner.png                — wizard-specific source. Designed in
#                                  a wizard-friendly aspect (portrait
#                                  or near-portrait) so the BMPs come
#                                  out clean with simple resize, no
#                                  cropping required. Optional — if
#                                  absent the script falls back to the
#                                  GitHub banner, with letterboxing.
#
#      repository-template.png   — the master GitHub social-preview
#                                  banner (5+ MB, 2:1 landscape).
#                                  NEVER touched by this script.
#
#  Generated outputs (always regenerated, source PNGs stay untouched):
#
#      assets\wizard_image.bmp              164x314   (1x DPI)
#      assets\wizard_image_2x.bmp           328x628   (2x HiDPI)
#      assets\wizard_small_image.bmp         55x58    (1x DPI)
#      assets\wizard_small_image_2x.bmp     110x116   (2x HiDPI)
#      assets\repository-template_1280x640.png        (GitHub social preview)
#
#  Why a separate banner.png:
#    The earlier approach center-cropped the 2:1 GitHub banner into the
#    portrait wizard panels — sliced out 90% of the composition. The
#    user provides banner.png pre-composed for the wizard's ~1:1.9
#    aspect, so we can just resize without losing content.
#
#  Resize strategy:
#    Always FIT (scale-to-contain), never crop. If the source happens to
#    match the target aspect, FIT and a straight resize are identical.
#    If the source is wider/taller than the target, FIT pads the short
#    axis with the brand color ($0F1419) so all source pixels stay
#    visible. The padding is invisible against Inno's WizardImageBackColor.
#
#  Run manually:
#    powershell -NoProfile -ExecutionPolicy Bypass -File build_wizard_images.ps1
#  Or let build.bat invoke it before ISCC.
# ════════════════════════════════════════════════════════════════

[CmdletBinding()]
param(
    [string]$WizardSource = "",
    [string]$BannerSource = "",
    [string]$BigOut       = "",
    [string]$BigOut2x     = "",
    [string]$SmallOut     = "",
    [string]$SmallOut2x   = "",
    [string]$BannerOut    = ""
)

$ErrorActionPreference = "Stop"

# Resolve defaults relative to this script's location
$here   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$assets = Join-Path (Split-Path -Parent $here) "assets"

if (-not $WizardSource) { $WizardSource = Join-Path $assets "banner.png" }
if (-not $BannerSource) { $BannerSource = Join-Path $assets "repository-template.png" }
if (-not $BigOut)       { $BigOut       = Join-Path $assets "wizard_image.bmp" }
if (-not $BigOut2x)     { $BigOut2x     = Join-Path $assets "wizard_image_2x.bmp" }
if (-not $SmallOut)     { $SmallOut     = Join-Path $assets "wizard_small_image.bmp" }
if (-not $SmallOut2x)   { $SmallOut2x   = Join-Path $assets "wizard_small_image_2x.bmp" }
if (-not $BannerOut)    { $BannerOut    = Join-Path $assets "repository-template_1280x640.png" }

# Wizard fallback: if banner.png is missing, reuse the GitHub banner so
# the install still ships SOMETHING branded. The aspect mismatch will
# cause big letterbox bars, but at least no crop.
if (-not (Test-Path $WizardSource)) {
    Write-Host "[wizard-img] banner.png not found - falling back to repository-template.png for wizard"
    $WizardSource = $BannerSource
}

if (-not (Test-Path $BannerSource)) {
    Write-Host "[wizard-img] no usable source PNGs found - skipping"
    Write-Host "[wizard-img] installer will use Inno's default look"
    exit 0
}

Add-Type -AssemblyName System.Drawing

# Brand background color used to pad the letterboxed wizard images.
# RGB 15,20,25 = $0F1419 — same as WizardImageBackColor in the .iss so
# the padding is invisible against the wizard chrome.
$brandR = 15; $brandG = 20; $brandB = 25

function Convert-Image {
    param(
        [string]$Src,
        [string]$Dst,
        [int]$TargetW,
        [int]$TargetH,
        [string]$Format = "Bmp"
    )

    if ($Format -eq "Bmp") {
        $imgFmt = [System.Drawing.Imaging.ImageFormat]::Bmp
        $pxFmt  = [System.Drawing.Imaging.PixelFormat]::Format24bppRgb
    }
    elseif ($Format -eq "Png") {
        $imgFmt = [System.Drawing.Imaging.ImageFormat]::Png
        $pxFmt  = [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
    }
    else {
        throw "Unknown format: $Format"
    }

    $orig = [System.Drawing.Image]::FromFile($Src)
    $dstBmp = $null
    $g = $null
    try {
        $srcW = $orig.Width
        $srcH = $orig.Height

        # Fit / contain: scale entire source so it fits inside target
        # without cropping. If aspect matches, this is identical to a
        # straight resize. If it doesn't match, the short axis gets
        # padded with the brand color so all source pixels stay visible.
        $scale = [Math]::Min([double]$TargetW / [double]$srcW, [double]$TargetH / [double]$srcH)
        $drawW = [int]([double]$srcW * $scale)
        $drawH = [int]([double]$srcH * $scale)
        if ($drawW -lt 1) { $drawW = 1 }
        if ($drawH -lt 1) { $drawH = 1 }
        $drawX = [int](($TargetW - $drawW) / 2)
        $drawY = [int](($TargetH - $drawH) / 2)

        # Build destination bitmap. Use New-Object with -ArgumentList
        # to avoid the trailing-comma line-continuation parser quirk.
        $dstBmp = New-Object System.Drawing.Bitmap -ArgumentList @($TargetW, $TargetH, $pxFmt)
        $g      = [System.Drawing.Graphics]::FromImage($dstBmp)

        $g.InterpolationMode  = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.SmoothingMode      = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
        $g.PixelOffsetMode    = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality

        # Fill background with brand dark for any padded area. For BMP
        # we always fill (no alpha to preserve). For PNG we only fill
        # if the resized image won't fully cover the target.
        if ($Format -eq "Bmp") {
            $bgColor = [System.Drawing.Color]::FromArgb(255, $brandR, $brandG, $brandB)
            $g.Clear($bgColor)
        }
        elseif ($drawW -ne $TargetW -or $drawH -ne $TargetH) {
            $bgColor = [System.Drawing.Color]::FromArgb(255, $brandR, $brandG, $brandB)
            $g.Clear($bgColor)
        }

        $srcRect = New-Object System.Drawing.Rectangle -ArgumentList @(0, 0, $srcW, $srcH)
        $dstRect = New-Object System.Drawing.Rectangle -ArgumentList @($drawX, $drawY, $drawW, $drawH)
        $g.DrawImage($orig, $dstRect, $srcRect, [System.Drawing.GraphicsUnit]::Pixel)

        $dstBmp.Save($Dst, $imgFmt)
        $bytes = (Get-Item $Dst).Length
        $padTopBot = $TargetH - $drawH
        $padLeftRight = $TargetW - $drawW
        Write-Host ("[wizard-img] {0}  ({1}x{2}, src->{3}x{4}, pad={5}x{6}, {7:N0} bytes)" -f `
            $Dst, $TargetW, $TargetH, $drawW, $drawH, $padLeftRight, $padTopBot, $bytes)
    }
    finally {
        if ($g)      { $g.Dispose() }
        if ($dstBmp) { $dstBmp.Dispose() }
        if ($orig)   { $orig.Dispose() }
    }
}

$wizSrcBytes = (Get-Item $WizardSource).Length
$banSrcBytes = (Get-Item $BannerSource).Length
Write-Host ("[wizard-img] wizard source : {0} ({1:N0} bytes)" -f $WizardSource, $wizSrcBytes)
Write-Host ("[wizard-img] banner source : {0} ({1:N0} bytes)" -f $BannerSource, $banSrcBytes)
Write-Host "[wizard-img] writing wizard bitmaps + GitHub social-preview banner..."

# Wizard bitmaps come from banner.png (if present) — designed for the
# portrait wizard panels, so resize is clean. Falls back to repo banner
# only when banner.png is absent (with letterbox bars).
Convert-Image -Src $WizardSource -Dst $BigOut     -TargetW 164 -TargetH 314 -Format Bmp
Convert-Image -Src $WizardSource -Dst $BigOut2x   -TargetW 328 -TargetH 628 -Format Bmp
Convert-Image -Src $WizardSource -Dst $SmallOut   -TargetW  55 -TargetH  58 -Format Bmp
Convert-Image -Src $WizardSource -Dst $SmallOut2x -TargetW 110 -TargetH 116 -Format Bmp

# GitHub social-preview banner is always derived from the GitHub-aspect
# master (repository-template.png), regardless of whether banner.png
# exists. This way the user always gets a proper 2:1 social preview.
Convert-Image -Src $BannerSource -Dst $BannerOut -TargetW 1280 -TargetH 640 -Format Png

Write-Host "[wizard-img] done. Both source PNGs were NOT modified."
exit 0
