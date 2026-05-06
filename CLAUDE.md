# anima-vast

Vast.ai 上に ComfyUI + AnimaWebUI 環境を立ち上げることに特化したプロジェクト。

## 構成

```
anima-vast/
├── deploy.py           # Vast.ai インスタンス管理 (deploy/status/destroy/list)
├── onstart.sh          # インスタンス起動時セットアップスクリプト
├── config.ini          # API鍵・GPU設定 (gitignore)
├── config.ini.example  # 設定テンプレート
├── anima-webui/        # WebUI本体 (git submodule)
└── CLAUDE.md
```

## 使い方

```bash
# デプロイ
python deploy.py deploy

# 状態確認
python deploy.py status

# インスタンス破棄
python deploy.py destroy

# WebUIソースをインスタンスに転送（ホットデプロイ）
python deploy.py upload
```

## Vast.ai リモートアクセス

バックエンドURLはブローカー経由で取得：
```
https://anima-broker.beamman.workers.dev/backend
```

exec API でシェルコマンド実行：
```bash
python -c "import requests; r=requests.post('<URL>/api/admin/exec', json={'command':'ls -la /workspace/'}); print(r.text)"
```
