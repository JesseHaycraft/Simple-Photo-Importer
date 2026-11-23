# Find-Missing-Photos.ps1
# Finds photos on an SD card that are NOT in your photo library (matched by filename),
# saves a list, and copies the missing photos to a new folder.
# How to run: right-click this file > "Run with PowerShell"

Add-Type -AssemblyName System.Windows.Forms

# File types to check. Add or remove extensions here if needed.
$extensions = @('.jpg','.jpeg','.png','.heic','.tif','.tiff',
                '.cr2','.cr3','.nef','.arw','.dng','.raf','.orf','.rw2',
                '.mp4','.mov','.avi')

# Shows a folder picker window and returns the chosen path (or $null if cancelled)
function Pick-Folder($description) {
    $owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true }
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = $description
    $result = $dialog.ShowDialog($owner)
    $owner.Dispose()
    if ($result -eq 'OK') { return $dialog.SelectedPath }
    return $null
}

# Returns all photo files in a folder, including subfolders
function Get-Photos($folder) {
    Get-ChildItem -LiteralPath $folder -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $extensions -contains $_.Extension.ToLower() }
}

# Shows a clear ending message and waits so the window does not vanish
function Finish-Script($text) {
    Write-Host ""
    Write-Host $text -ForegroundColor Yellow
    Write-Host "=== Script finished. It is safe to close this window. ===" -ForegroundColor Green
    Read-Host "Press Enter to close"
    exit
}

function Show-Message($text) {
    [void][System.Windows.Forms.MessageBox]::Show($text, 'Find Missing Photos')
}

# --- Step 1: SD card ---
$sdFolder = Pick-Folder 'Step 1: Select the SD card (or its DCIM folder)'
if (-not $sdFolder) { Finish-Script "Cancelled - no SD card folder chosen." }

# --- Step 2: One or more library folders ---
$libraryFolders = @()
do {
    $lib = Pick-Folder 'Step 2: Select a photo library folder on your computer'
    if ($lib) { $libraryFolders += $lib }
    $more = [System.Windows.Forms.MessageBox]::Show(
        "Folders added so far:`n$($libraryFolders -join "`n")`n`nAdd another library folder?",
        'Find Missing Photos', 'YesNo')
} while ($more -eq 'Yes')

if ($libraryFolders.Count -eq 0) { Show-Message 'No library folders chosen. Stopping.'; Finish-Script "Stopped - no library folders chosen." }

# --- Step 3: Where to save results ---
$saveFolder = Pick-Folder 'Step 3: Select where to save the results'
if (-not $saveFolder) { Finish-Script "Cancelled - no save folder chosen." }

# --- Scan the library and remember every filename (not case-sensitive) ---
Write-Host 'Scanning photo library... (this can take a while)'
$rawExtensions = @('.nef','.cr2','.cr3','.arw','.dng','.raf','.orf','.rw2')
$jpgExtensions = @('.jpg','.jpeg')
$knownNames    = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$knownRawBases = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($lib in $libraryFolders) {
    foreach ($file in Get-Photos $lib) {
        [void]$knownNames.Add($file.Name)
        # Remember RAW files by name without extension (DSC_0001.nef -> DSC_0001)
        if ($rawExtensions -contains $file.Extension.ToLower()) { [void]$knownRawBases.Add($file.BaseName) }
    }
}

# --- Compare against the SD card ---
Write-Host 'Scanning SD card...'
$sdPhotos = @(Get-Photos $sdFolder)
$missing  = @($sdPhotos | Where-Object {
    $alreadyHaveFile = $knownNames.Contains($_.Name)
    # A JPG counts as "already have" if its matching RAW is in the library
    $haveRawOfJpg = ($jpgExtensions -contains $_.Extension.ToLower()) -and $knownRawBases.Contains($_.BaseName)
    -not ($alreadyHaveFile -or $haveRawOfJpg)
})

if ($missing.Count -eq 0) {
    Show-Message "All $($sdPhotos.Count) photos on the SD card are already in your library."
    Finish-Script "No missing photos found."
}

# --- Save the list and copy missing photos ---
$outFolder = Join-Path $saveFolder ('Missing Photos ' + (Get-Date -Format 'yyyy-MM-dd HHmm'))
New-Item -ItemType Directory -Path $outFolder -Force | Out-Null
$missing | ForEach-Object { $_.FullName } | Set-Content -LiteralPath (Join-Path $outFolder 'missing-list.txt')

$count = 0
foreach ($file in $missing) {
    $count++
    Write-Host "Copying $count of $($missing.Count): $($file.Name)"
    $target = Join-Path $outFolder $file.Name
    $n = 1
    while (Test-Path -LiteralPath $target) {   # avoid overwriting if two files share a name
        $target = Join-Path $outFolder ('{0}_{1}{2}' -f $file.BaseName, $n, $file.Extension)
        $n++
    }
    Copy-Item -LiteralPath $file.FullName -Destination $target
}

Show-Message "Done! $($missing.Count) of $($sdPhotos.Count) photos were missing.`n`nCopied to:`n$outFolder"
Start-Process explorer.exe $outFolder

Finish-Script "Done! Copied $($missing.Count) missing photos."
