[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9-]{0,38}$')]
    [string]$DataOwner = 'mrg321'
)

$ErrorActionPreference = 'Stop'
$gh = (Get-Command gh -CommandType Application -ErrorAction Stop).Source
$repository = "$DataOwner/DeyeCloud-data"
$workflowPath = '.github/workflows/deye-approval.yml'

function Assert-PrivateDestination {
    $response = & $gh api "repos/$repository" --jq '{name:.full_name,visibility:.visibility,branch:.default_branch}'
    if ($LASTEXITCODE -ne 0) { throw 'Could not verify the destination repository.' }
    $metadata = $response | ConvertFrom-Json
    if ($metadata.name -ne $repository -or $metadata.visibility -cne 'private' -or $metadata.branch -cne 'main') {
        throw 'The destination must be the requested private DeyeCloud-data repository with main as default.'
    }
}

Assert-PrivateDestination
$treeResponse = & $gh api "repos/$repository/git/trees/main?recursive=1"
if ($LASTEXITCODE -ne 0) { throw 'Could not read the main branch tree.' }
$tree = $treeResponse | ConvertFrom-Json
if ($tree.truncated -cne $false -or $null -eq $tree.tree) {
    throw 'Could not verify the complete main branch tree.'
}
$entry = $tree.tree | Where-Object { $_.path -ceq $workflowPath }
if ($entry) {
    if ($entry.type -cne 'blob' -or $entry.mode -cne '100644') {
        throw 'The existing workflow path cannot be verified as a regular file.'
    }
    Write-Host "Workflow already exists in $repository/main. No changes made."
    return
}

$template = Join-Path (Split-Path -Parent $PSScriptRoot) 'config/deye-approval.workflow.example.json'
$content = [IO.File]::ReadAllBytes($template)
$null = [Text.Encoding]::UTF8.GetString($content) | ConvertFrom-Json
if ($PSCmdlet.ShouldProcess("$repository/main/$workflowPath", 'Create approval workflow')) {
    Assert-PrivateDestination
    $payload = @{
        branch = 'main'
        message = 'Install private Deye approval workflow'
        content = [Convert]::ToBase64String($content)
    } | ConvertTo-Json -Compress
    # An update requires the existing blob SHA. Omitting it makes this create-only.
    $payload | & $gh api --method PUT "repos/$repository/contents/$workflowPath" --input - --jq '.content.path'
    if ($LASTEXITCODE -ne 0) { throw 'Workflow creation failed. Verify GitHub workflow permissions and rerun.' }
}
