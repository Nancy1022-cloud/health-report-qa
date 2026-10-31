param([ValidateSet(1,4)][int]$BatchSize = 1)

# Dot-source this file from the health-report-qa project root.
$script:HealthM13Root = (Get-Location).Path
$script:HealthM13Compose = @('-f','compose.yaml','-f','compose.m11.yaml')
if ($BatchSize -eq 4) { $script:HealthM13Compose += @('-f','compose.m12.yaml') }

function Invoke-HealthM13 {
    & docker compose @script:HealthM13Compose run --rm -T --no-deps `
        -v "${script:HealthM13Root}:/m13-work" -w /m13-work `
        app python m13_bundle/run_m13.py @args
    if ($LASTEXITCODE -ne 0) { throw 'M13 本步骤未通过，请查看本阶段对应的 error 或 summary 文件。' }
}

function Wait-HealthM13 {
    for ($healthM13Try = 0; $healthM13Try -lt 40; $healthM13Try++) {
        try {
            $healthM13Health = Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 3
            $healthM13Worker = Invoke-RestMethod 'http://127.0.0.1:8000/api/tasks/worker' -TimeoutSec 3
            if ($healthM13Health.stage -eq 'M12' -and $healthM13Worker.online -eq $true) { return }
        } catch {}
        Start-Sleep -Seconds 2
    }
    & docker compose @script:HealthM13Compose logs --tail 80 app worker
    throw 'API / worker 未就绪。'
}

function Backup-HealthM13 {
    Invoke-HealthM13 start-backup
    $healthM13Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $healthM13Tag = [Guid]::NewGuid().ToString('N').Substring(0,12)
    $healthM13Relative = "backups/m13_${healthM13Stamp}_${healthM13Tag}"
    $healthM13Directory = Join-Path $script:HealthM13Root $healthM13Relative
    $healthM13Clone = "m13_restore_${healthM13Tag}"
    $healthM13Dump = "/tmp/m13_${healthM13Tag}.dump"
    $healthM13CloneCreated = $false
    New-Item -ItemType Directory -Path $healthM13Directory -ErrorAction Stop | Out-Null
    # Check active tasks before stopping, then capture again after every writer has stopped.
    Invoke-HealthM13 snapshot --output "$healthM13Relative/before_stop.json"
    & docker compose @script:HealthM13Compose stop app worker
    if ($LASTEXITCODE -ne 0) { throw '停止 app / worker 失败；未执行数据库导出。' }
    try {
        Invoke-HealthM13 snapshot --output "$healthM13Relative/snapshot.json"
        Invoke-HealthM13 archive-files --output "$healthM13Relative/uploads.zip"
        & docker compose @script:HealthM13Compose exec -T db sh -c `
            'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-acl -f "$1"' sh $healthM13Dump
        if ($LASTEXITCODE -ne 0) { throw 'pg_dump 失败。' }
        $healthM13DbId = (& docker compose @script:HealthM13Compose ps -q db).Trim()
        if (-not $healthM13DbId) { throw '没有找到运行中的 db 容器。' }
        & docker cp "${healthM13DbId}:$healthM13Dump" (Join-Path $healthM13Directory 'database.dump')
        if ($LASTEXITCODE -ne 0) { throw '复制数据库备份失败。' }
        # Fresh database name is generated above. No operation targets the original database.
        & docker compose @script:HealthM13Compose exec -T db sh -c `
            'createdb -U "$POSTGRES_USER" --maintenance-db="$POSTGRES_DB" "$1"' sh $healthM13Clone
        if ($LASTEXITCODE -ne 0) { throw '创建恢复演练数据库失败。' }
        $healthM13CloneCreated = $true
        & docker compose @script:HealthM13Compose exec -T db sh -c `
            'pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --no-acl --exit-on-error "$2"' sh $healthM13Clone $healthM13Dump
        if ($LASTEXITCODE -ne 0) { throw 'pg_restore 失败，原数据库未被覆盖。' }
        Invoke-HealthM13 restore-files --archive "$healthM13Relative/uploads.zip" --destination "$healthM13Relative/restore_root/uploads"
        & docker compose @script:HealthM13Compose run --rm -T --no-deps `
            -v "${script:HealthM13Root}:/m13-work" -w /m13-work `
            -e "POSTGRES_DB=$healthM13Clone" -e "UPLOAD_DIR=/m13-work/$healthM13Relative/restore_root/uploads" `
            app python m13_bundle/run_m13.py compare-restore --snapshot "$healthM13Relative/snapshot.json"
        if ($LASTEXITCODE -ne 0) { throw '恢复后的记录、表结构、序列或文件不一致；保留备份排查。' }
        Get-FileHash -LiteralPath (Join-Path $healthM13Directory 'database.dump'),(Join-Path $healthM13Directory 'uploads.zip') -Algorithm SHA256 |
            Select-Object Path,Hash | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $healthM13Directory 'backup_hashes.json') -Encoding UTF8
        @{
            backup_directory = $healthM13Relative
            dump_saved = $true
            files_saved = $true
            original_database_overwritten = $false
            contains_private_data = $true
        } | ConvertTo-Json | Set-Content -LiteralPath .\app\data\m13\v1\backup_location.json -Encoding UTF8
    } finally {
        if ($healthM13CloneCreated) {
            & docker compose @script:HealthM13Compose exec -T db sh -c `
                'dropdb -U "$POSTGRES_USER" --maintenance-db="$POSTGRES_DB" "$1"' sh $healthM13Clone
            if ($LASTEXITCODE -ne 0) { Write-Warning "临时数据库 $healthM13Clone 尚未清理，请保留名称排查。" }
        }
        & docker compose @script:HealthM13Compose exec -T db rm -f $healthM13Dump
        & docker compose @script:HealthM13Compose up -d app worker
        if ($LASTEXITCODE -ne 0) { throw '备份后的 app / worker 重启失败，请查看日志。' }
    }
    Wait-HealthM13
    Invoke-HealthM13 compare-restart --snapshot "$healthM13Relative/snapshot.json"
    Invoke-HealthM13 live
    Write-Host "备份及恢复核对完成：$healthM13Directory"
}
