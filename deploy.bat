@echo off
setlocal enabledelayedexpansion

REM ====================================================
REM  GCP Cloud Run Deploy Script for Windows (Batch)
REM  (.env対応版 / 最終確認済)
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
    REM コメント行(#)や空行をスキップする簡易判定
    if not "!KEY:~0,1!"=="#" (
        set "!KEY!=!VAL!"
    )
)

REM --- 2. 読み込み確認 (デバッグ用) ---
echo Project ID:    %GCP_PROJECT_ID%
echo Service:       %SERVICE_NAME%
echo Run Region:    %GCP_REGION%
echo Model Loc:     %GCP_LOCATION%

REM --- 3. デプロイコマンド実行 ---

echo.
echo [1/3] Setting Project to %GCP_PROJECT_ID%...
call gcloud config set project %GCP_PROJECT_ID%
if %errorlevel% neq 0 goto :error

echo.
echo [2/3] Building Container Image...
REM .envの REPO_NAME を使用
call gcloud builds submit --tag gcr.io/%GCP_PROJECT_ID%/%REPO_NAME%
if %errorlevel% neq 0 goto :error

echo.
echo [3/3] Deploying to Cloud Run...
REM 環境変数を設定 (GCP_PROJECT_ID等は .env から読み込んだものを使用)
call gcloud run deploy %SERVICE_NAME% ^
    --image gcr.io/%GCP_PROJECT_ID%/%REPO_NAME% ^
    --platform managed ^
    --region %GCP_REGION% ^
    --service-account %RUNTIME_SA% ^
    --allow-unauthenticated ^
    --set-env-vars GCP_PROJECT_ID=%GCP_PROJECT_ID%,GCP_LOCATION=%GCP_LOCATION%,GEMINI_MODEL_ID=%GEMINI_MODEL_ID%,HOSTING_URL=%HOSTING_URL%

if %errorlevel% neq 0 goto :error

echo.
echo ====================================================
echo  DEPLOY SUCCESS!
echo ====================================================
call gcloud run services describe %SERVICE_NAME% --region %GCP_REGION% --format "value(status.url)"
echo.
pause
exit /b 0

:error
echo.
echo ====================================================
echo  DEPLOY FAILED!
echo ====================================================
pause
exit /b 1