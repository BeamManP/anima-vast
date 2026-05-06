# anima-vast

Vast.ai 上に ComfyUI + AnimaWebUI 環境を立ち上げることに特化したプロジェクト。

## 構成

```
anima-vast/
├── deploy.py           # Vast.ai インスタンス管理
├── onstart.sh          # インスタンス起動時セットアップスクリプト
├── config.ini          # API鍵・GPU設定 (gitignore)
├── config.ini.example  # 設定テンプレート
├── anima-webui/        # WebUI本体 (git submodule)
└── CLAUDE.md
```

## コマンド一覧

```bash
# ── デプロイ ──
python deploy.py deploy          # GPU検索 → インスタンス作成 → 起動待ち
python deploy.py status          # 状態・URL・セットアップ進捗・Cloudflare URL 表示
python deploy.py destroy         # インスタンス破棄

# ── リモート操作 ──
python deploy.py ssh             # SSH 接続（対話）
python deploy.py exec "command"  # リモートでコマンド実行
python deploy.py logs setup      # セットアップログ表示
python deploy.py logs comfyui    # ComfyUI ログ
python deploy.py logs anima      # AnimaWebUI ログ
python deploy.py logs cloudflared  # トンネルログ

# ── ホットデプロイ ──
python deploy.py upload          # ローカルの anima-webui を転送 + 再起動

# ── その他 ──
python deploy.py list            # 全インスタンス一覧
```

## アーキテクチャ

### bootstrap 方式
Vast.ai の onstart には 4048 文字制限がある。
deploy.py は短い bootstrap（anima-vast リポを clone → onstart.sh 実行）だけを送り、
本体のセットアップは onstart.sh がリポから取得して実行する。

### セットアップ進捗
onstart.sh は各フェーズで `/workspace/setup_status.txt` に現在のフェーズを書く。
`deploy.py status` で SSH 経由で読み取り表示する。

## Vast.ai リモートアクセス

バックエンドURLはブローカー経由で取得：
```
https://anima-broker.beamman.workers.dev/backend
```

exec API でシェルコマンド実行：
```bash
python -c "import requests; r=requests.post('<URL>/api/admin/exec', json={'command':'ls -la /workspace/'}); print(r.text)"
```
