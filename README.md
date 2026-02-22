GP-chat_on_GCP: Gemini対応AI汎用チャットアプリ  
  
## Table of Contents  
  
- [概要](#概要)  
- [リポジトリ構成](#リポジトリ構成)  
- [インストール](#インストール例)  
- [環境設定](#環境設定)  
- [使い方](#使い方)  
- [主な機能](#主な機能)  
- [CHANGELOG](#changelog)  
- [ライセンス](#ライセンス)  
- [Author](#Author)  
  
---  
## 概要  
  
`GP-chat_on_GCP` は、  
GeminiAPIに対応した汎用のチャットアプリケーションです。  
本アプリケーションは、従来のチャット形式の対話に加え、PDF・画像・WORDファイルの添付機能と、  
複数のコードブロック（Canvas）をコンテキストとしてAIに提供できる「マルチコード」機能を  
搭載しています。  
  
CLIラッパー (`main_runner.py`) は `streamlit run main.py` を自動で呼び出します。  
  
---  
## リポジトリ構成  
.  
 ├── env/  # python仮想環境ファイル群
 ├── gp_chat/  
 │  ├── utils.py # ヘルパー関数群  
 │  ├── config.py # 定数・テキスト定義  
 │  ├── config.yaml # テキスト情報  
 │  ├── sidebar.py # サイドバー機能管理  
 │  └── prompts.yaml # プロンプト定義  
 ├──  main.py # Streamlit アプリケーション本体  
 ├──  main_runner.py # CLI からの起動用ラッパー  
 ├── .gitignore  
 ├── .dockerignore  
 ├── activate.bat  
 ├── .env  
 ├── ip_config.json  
 ├── Dockerfile  
 ├── deploy.bat
 ├── server.py
 ├── LICENSE  
 ├── README.md  
 ├── pyproject.toml  
 ├── requirements.txt  
 └── CHANGELOG.md  
  
---  
## 環境設定  
  
- GCP上で、サービスアカウントを作成し、適切なroleを設定    
- cloudrunの設定    
- firebaseのプロジェクト作成と設定    

---  
## 事前準備    
  
以下に記載の方法でPython仮想環境を構築  
https://note.com/yoichi_1984xx/n/n3c95602b011c  
  
  
---  
## 主な機能  
### AIモデルの選択:  
 サイドバー上部にAIモデルの選択リストがあります。使いたいモデルを選択してください。  
### AIの役割設定:  
 最初のチャット画面で、AIの役割を定義するシステムプロンプトを入力し、「この役割でチャットを開始する」ボタンをクリックします。  
### マルチ Canvas コードエディタ（最大 20）:  
 Canvasを用いてコードをAIに効率よく読ませることができます。マルチコード機能を有効にすることで、最大20個までCanvasを拡張することも可能です。  
### 会話履歴の JSON ダウンロード／アップロード:  
 AIの役割、チャット履歴、Canvasの内容すべてをJSON形式でダウンロードし、途中再開が可能です。  
 チャット再開時には、AIモデルの選択情報、Canvasに記述したコード、チャット内容すべて再開できます。  
### 応答ストリーミング＆停止ボタン:  
 APIからの応答をリアルタイム表示し、途中停止が可能。  
### トークン使用量の表示・累計:  
 AIモデルの最大トークンに考慮した形でチャットができるように、最新の使用トークンを表示します。  
  
---  
## CHANGELOG  
すべてのリリース履歴は CHANGELOG.md に記載しています。  
  
---  
## ライセンス  
 本ソフトウェアは「Apache License 2.0」に準拠しています。  
  
---  
## Author  
 -Yoichi-1984 (<yoichi.1984.engineer@gmail.com>)  
