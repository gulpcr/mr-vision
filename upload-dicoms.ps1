# Upload DICOM files from DICOMS\ folder to Orthanc
# Usage: .\upload-dicoms.ps1
#        .\upload-dicoms.ps1 -Folder "C:\path\to\your\dicoms"

param(
    [string]$Folder = "$PSScriptRoot\DICOMS",
    [string]$OrthancUrl = "http://localhost:8042",
    [string]$Username = "orthanc",
    [string]$Password = "orthanc"
)

$cred = New-Object System.Management.Automation.PSCredential(
    $Username,
    (ConvertTo-SecureString $Password -AsPlainText -Force)
)

$files = Get-ChildItem -Path $Folder -Recurse -File | Where-Object {
    $_.Extension -in @(".dcm", ".DCM", "") -or $_.Name -match "^[A-Z]\d+"
}

if ($files.Count -eq 0) {
    Write-Host "No DICOM files found in: $Folder"
    exit 1
}

Write-Host "Found $($files.Count) DICOM file(s) in: $Folder"
Write-Host "Uploading to $OrthancUrl ..."
Write-Host ""

$success = 0
$failed  = 0
$i       = 0

foreach ($file in $files) {
    $i++
    $pct = [int](($i / $files.Count) * 100)
    Write-Progress -Activity "Uploading DICOM files" -Status "$i / $($files.Count) - $($file.Name)" -PercentComplete $pct

    try {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        $result = Invoke-RestMethod `
            -Uri "$OrthancUrl/instances" `
            -Method Post `
            -Body $bytes `
            -ContentType "application/dicom" `
            -Credential $cred `
            -ErrorAction Stop

        $success++
        if ($result.Status -eq "AlreadyStored") {
            Write-Host "  [SKIP] $($file.Name) - already in Orthanc"
        } else {
            Write-Host "  [OK]   $($file.Name) - ID: $($result.ID)"
        }
    } catch {
        $failed++
        Write-Host "  [FAIL] $($file.Name) - $($_.Exception.Message)"
    }
}

Write-Progress -Activity "Uploading DICOM files" -Completed
Write-Host ""
Write-Host "Done. Uploaded: $success  |  Failed: $failed  |  Total: $($files.Count)"
Write-Host "View studies at: $OrthancUrl/ui/app/"
