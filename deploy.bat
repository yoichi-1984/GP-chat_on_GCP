@echo off
setlocal enabledelayedexpansion

REM ====================================================
REM  GCP Cloud Run Deploy Script for Windows (Batch)
REM  (.env対応版 / タイムアウト60分延長版)
REM ====================================================

REM --- 1. .envファイルの読み込み ---
if not exist .env (
    echo [ERROR] .env file not found!
    echo Please create .env file with GCP_PROJECT_ID, SERVICE_NAME, etc.
    pause
    exit /b 1
)

echo Loading configurations from .env...
for /f "usebackq tokens=1* delims==" %%A in (".env") do (
    set "KEY=%%A"
    set "VAL=%%B"
    if not "!KEY!"=="" (
        REM コメント行(#)をスキップ
        set "FIRST_CHAR=!KEY:~0,1!"
        if not "!FIRST_CHAR!"=="#" (
            REM 変数名と値の前後スペースをトリミングし、ダブルクォーテーションを除去
            for /f "tokens=*" %%I in ("!KEY!") do set "KEY=%%I"
            for /f "tokens=*" %%I in ("!VAL!") do set "VAL=%%I"
            set "VAL=!VAL:"=!"
            set "!KEY!=!VAL!"
        )
    )
)

REM --- 2. 読み込み確認 (必須変数の検証) ---
echo.
echo ====================================================
echo  Loaded Configurations:
echo ====================================================
echo  Project ID:   %GCP_PROJECT_ID%
echo  Service:      %SERVICE_NAME%
echo  Run Region:   %GCP_REGION%
echo  Model Loc:    %GCP_LOCATION%
echo  Repo Name:    %REPO_NAME%
echo  Runtime SA:   %RUNTIME_SA%
echo ====================================================
echo.

REM 必須変数の存在チェック
if "%GCP_PROJECT_ID%"=="" goto :var_error
if "%SERVICE_NAME%"=="" goto :var_error
if "%GCP_REGION%"=="" goto :var_error
if "%REPO_NAME%"=="" goto :var_error

REM --- 3. デプロイコマンド実行 ---

echo [1/3] Setting Project to %GCP_PROJECT_ID%...
call gcloud config set project %GCP_PROJECT_ID%
if %errorlevel% neq 0 goto :error

echo.
echo [2/3] Building Container Image...
call gcloud builds submit --tag gcr.io/%GCP_PROJECT_ID%/%REPO_NAME%
if %errorlevel% neq 0 goto :error

echo.
echo [3/3] Deploying to Cloud Run...
call gcloud run deploy %SERVICE_NAME% ^
    --image gcr.io/%GCP_PROJECT_ID%/%REPO_NAME% ^
    --platform managed ^
    --region %GCP_REGION% ^
    --service-account %RUNTIME_SA% ^
    --allow-unauthenticated ^
    --memory=8Gi ^
    --cpu=2 ^
    --concurrency=15 ^
    --min-instances=1 ^
    --max-instances=15 ^
    --timeout=3600 ^
    --set-env-vars GCP_PROJECT_ID=%GCP_PROJECT_ID%,GCP_LOCATION=%GCP_LOCATION%,GEMINI_MODEL_ID=%GEMINI_MODEL_ID%,TITLE_GENERATION_MODEL_ID=%TITLE_GENERATION_MODEL_ID%,HOSTING_URL=%HOSTING_URL%
if %errorlevel% neq 0 goto :error

REM --- 4. 完了処理 ---
echo.
echo ====================================================
echo  DEPLOY SUCCESS!
echo ====================================================
echo Service URL:
call gcloud run services describe %SERVICE_NAME% --region %GCP_REGION% --format "value(status.url)"
echo.
goto :end

:var_error
echo.
echo [ERROR] Required environment variables are missing in .env!
echo Please check GCP_PROJECT_ID, SERVICE_NAME, GCP_REGION, and REPO_NAME.
goto :error

:error
echo.
echo ====================================================
echo  DEPLOY FAILED!
echo ====================================================
pause
endlocal
exit /b 1

:end
pause
endlocal
exit /b 0