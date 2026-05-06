# Vast.ai ComfyUI/AnimaWebUI デプロイ改善レポート

作成日: 2026-05-06  
対象: `F:\anima-vast`  
参考: `F:\wan22\vastai-deploy`

## 要約

このプロジェクトは、Vast.ai API で GPU インスタンスを作成し、`onstart.sh` で ComfyUI と AnimaWebUI を自動セットアップする構成です。方向性は妥当ですが、現状は「WebUI を使えるようにする」ことに寄っており、Vast.ai 上で普通にインスタンスを立てた時に使える Jupyter、対話コンソール、ログ確認、復旧操作の代替が不足しています。

特に不便さの中心は、Vast 側の手動操作画面に頼れないにもかかわらず、こちらのツール側にも十分な運用面の入口がないことです。ComfyUI/AnimaWebUI の URL は出ますが、セットアップ失敗時に何が起きたか、リモートでどう直すか、Jupyter 的に一時作業するにはどうするかが弱いです。

優先度の高い改善は次の 5 点です。

1. SSH 接続・ログ取得・リモートコマンド実行を `deploy.py` の正式コマンドにする。
2. JupyterLab または code-server を起動し、専用ポートを公開する。
3. `onstart.sh` の進捗ファイル、失敗マーカー、ログ末尾表示を標準化する。
4. `onstart` の 4048 文字制限に対して、現行の巨大 heredoc 方式をやめる。
5. 文字化けしている README/コメント/バッチ表示を UTF-8 として修復する。

## 現状の構成

`deploy.py` は Vast.ai REST API に直接アクセスし、オファー検索、インスタンス作成、状態確認、破棄、アップロードを担当しています。

- API ベース URL: `deploy.py:19`
- オファー検索条件: `deploy.py:84-104`
- `onstart.sh` の読み込みと heredoc 化: `deploy.py:123-145`
- ポート公開: `deploy.py:253-265`
- URL 組み立て: `deploy.py:169-193`
- アップロード処理: `deploy.py:348-411`

`onstart.sh` はインスタンス起動時に実行され、ComfyUI、カスタムノード、モデル、AnimaWebUI、Cloudflare Tunnel をまとめて構築します。

- `set -e`: `onstart.sh:4`
- ComfyUI インストール: `onstart.sh:25-35`
- HF モデル取得: `onstart.sh:49-79`
- CivitAI モデル取得: `onstart.sh:81-93`
- AnimaWebUI clone/config: `onstart.sh:95-118`
- ComfyUI/AnimaWebUI 起動: `onstart.sh:120-135`
- Cloudflare Tunnel 起動: `onstart.sh:137-150`

## 問題点

### 1. Jupyter/コンソール相当の入口がない

現状公開しているのは ComfyUI と AnimaWebUI の 2 ポートだけです。`deploy.py:253-255` では `comfyui_port` と `anima_port` のみ `-p` 指定されており、JupyterLab、code-server、TTY 代替のポートがありません。

Vast の通常 UI で使える Jupyter/console を手動利用できない前提なら、こちら側で最低限次のどれかを用意すべきです。

- JupyterLab: `8888`
- code-server: `8080` など
- SSH helper: `python deploy.py ssh`
- リモートコマンド実行: `python deploy.py exec "nvidia-smi"`
- ログ追跡: `python deploy.py logs setup|comfyui|anima|cloudflared`

今のままだと、セットアップ中に失敗した場合、ユーザーは Vast の UI に戻るか、API レスポンスから SSH 情報を探して手動接続する必要があります。

### 2. セットアップ失敗時の観測性が弱い

`onstart.sh` は `$W/setup.log` にログを tee していますが、`deploy.py` 側にそれを取得するコマンドがありません。`deploy.py status` は Vast のインスタンス状態と URL 表示が中心で、`setup.log`、`comfyui.log`、`anima.log`、`cloudflared.log` を読みに行きません。

参考側の `F:\wan22\vastai-deploy\run_anima.py` には、SSH 経由で `/workspace/setup_status.txt` や `/workspace/setup.log` を読む仕組みがあります。これを `F:\anima-vast` 側にも取り込む価値があります。

推奨:

- `/workspace/setup_status.txt` に現在フェーズを書き出す。
- `/workspace/setup_failed.txt` に失敗理由を書き出す。
- `deploy.py logs --tail 100` を追加する。
- `deploy.py doctor` で `nvidia-smi`, `python --version`, `node -v`, `curl /system_stats`, `ps aux` をまとめて確認する。

### 3. `onstart` の 4048 文字制限に対して脆い

`deploy.py` は `onstart.sh` 全体を `cat<<'S'>/tmp/s.sh ... S` として Vast の `onstart` に埋め込んでいます。長さチェックはありますが、超過しても警告だけで処理は続きます。

該当箇所:

- `onstart.sh` を全文読み込み: `deploy.py:129-130`
- heredoc として埋め込み: `deploy.py:138-139`
- 4048 文字チェック: `deploy.py:141-145`

`F:\anima-vast\onstart.sh` は 6000 文字以上あり、Vast 側の制限に引っかかる可能性が高いです。これはデプロイの成否に直結します。

推奨:

- `onstart` は短い bootstrap のみにする。
- 実体は GitHub raw URL、リポジトリ clone、または base64 圧縮済みスクリプト取得に寄せる。
- 4048 文字を超えたら警告ではなく deploy を中断する。

例:

```bash
#!/bin/bash
set -e
git clone --depth 1 -b "${DEPLOY_BRANCH:-stable}" "$REPO_URL" /workspace/anima-vast
bash /workspace/anima-vast/onstart.sh
```

### 4. SSH を使う前提なのに SSH 操作用コマンドがない

`deploy.py upload` は SSH/SCP を使っていますが、ユーザーが SSH で入るための補助コマンドはありません。`inst.get("ssh_host")` と `inst.get("ssh_port")` を取得しているため、同じ情報で簡単に `ssh` コマンドを表示できます。

推奨コマンド:

- `python deploy.py ssh`  
  `ssh -p <port> root@<host>` を表示または起動する。
- `python deploy.py exec "command"`  
  SSH 経由で単発コマンドを実行する。
- `python deploy.py pull-output`  
  `/workspace/ComfyUI/output` や Anima の出力をローカルへ rsync/scp する。

これがあるだけで、Vast 側の手動コンソールが使えない不便さはかなり減ります。

### 5. JupyterLab/code-server を公式運用に含めるべき

Vast 上で一時的にファイルを直す、モデル配置を見る、ログを読む、Python で小さく検証する、という用途には JupyterLab か code-server があると強いです。

推奨は `JupyterLab + token` です。理由は、Python 実行、ファイル閲覧、ターミナル相当、ノートメモを一つで扱えるためです。

追加案:

- config に `jupyter_port = 8888`
- config に `jupyter_token = 自動生成または任意`
- `onstart.sh` で `pip install jupyterlab`
- `nohup jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --ServerApp.token="$JUPYTER_TOKEN" ...`
- `deploy.py build_urls()` に Jupyter URL を追加

注意点として、トークンなし公開は避けるべきです。Vast の direct/proxy URL は外から到達できるため、Jupyter は必ず token または password 付きにするべきです。

### 6. Cloudflare Tunnel 依存が運用上の単一障害点になっている

`onstart.sh:137-150` では `cloudflared tunnel --url http://localhost:8501` を起動し、ログから URL を抽出しています。これは便利ですが、trycloudflare は rate limit や一時失敗があり、恒久的な運用基盤としては不安定です。

また、`deploy.py build_urls()` は Vast の direct/proxy URL を表示する一方、Cloudflare URL はローカルの `deploy.py` から取得できません。結果として、トンネル成功時の URL がログの中に埋もれます。

推奨:

- `cloudflared.log` を `deploy.py status` で読む。
- Cloudflare URL を `/workspace/service_urls.json` に書き出す。
- `status` で Vast proxy と Cloudflare の両方を表示する。
- Cloudflare は補助経路とし、Vast proxy/direct を主経路にする。

### 7. 文字化けが広範囲に残っている

`deploy.py`、`onstart.sh`、README 相当の文書、バッチ表示に文字化けが残っています。実行には支障がない箇所もありますが、エラー時の判断をかなり難しくします。

例:

- `deploy.py:2`
- `deploy.py:26-27`
- `onstart.sh:2-3`
- `onstart.sh:8`
- `onstart.sh:154`

このプロジェクトは失敗時の調査が重要なので、ログやエラーメッセージの可読性は機能の一部です。全ファイルを UTF-8 に統一し、バッチは `chcp 65001` とセットで確認するのがよいです。

### 8. API キーやトークンの扱いがやや危険

`deploy.py` は `config.ini` から CivitAI/GitHub/passphrase を読み、Vast の `env` に渡します。この方向は自然ですが、GitHub token を clone URL に埋め込む形はログやプロセス表示に出る危険があります。

該当:

- 環境変数組み立て: `deploy.py:44-56`
- `GH_TOKEN` を `env` に渡す: `deploy.py:47-55`
- `onstart.sh` 側で URL に token を埋める: `onstart.sh:98-102`

推奨:

- private repo が不要なら `GH_TOKEN` は使わない。
- 使う場合はログに clone URL を出さない。
- `config.ini` は `.gitignore` に入っていることを確認する。
- `deploy.py doctor` では secret を必ずマスクする。

### 9. `upload` は便利だが安全性と再現性が弱い

`deploy.py upload` はローカルの `anima-webui` を tar で固めて SCP します。素早い反映には便利ですが、実行コマンドを `os.system` で文字列連結しており、パスや値に特殊文字が入ると壊れやすいです。

該当:

- tar 作成: `deploy.py:376-383`
- scp: `deploy.py:389-397`
- ssh 展開/npm install: `deploy.py:399-405`

推奨:

- `subprocess.run([...])` に置き換える。
- `StrictHostKeyChecking=no` を使うなら `UserKnownHostsFile` も明示し、挙動を固定する。
- upload 後に `systemctl` 相当の再起動、または既存 node process の停止・再起動まで行う。
- 反映対象の git commit/branch を表示する。

### 10. 起動完了判定が弱い

`deploy.py` は Vast の `actual_status == running` を待った後、URL を表示します。しかし `running` はコンテナが立ったことを示すだけで、ComfyUI/AnimaWebUI の準備完了とは別です。

`onstart.sh` 側では ComfyUI の `/system_stats` を待っていますが、`deploy.py` 側はその結果を見ていません。また AnimaWebUI の HTTP 応答確認もありません。

推奨:

- `deploy.py wait` または deploy 内で以下を確認する。
- ComfyUI: `/system_stats`
- AnimaWebUI: `/` または health endpoint
- JupyterLab: `/lab`
- 失敗時はログ末尾を出す。

## 改善案

### 短期対応

すぐ効く順番です。

1. `deploy.py ssh` を追加する。
2. `deploy.py logs setup|comfyui|anima|cloudflared --tail 100` を追加する。
3. `onstart.sh` で `/workspace/setup_status.txt` と `/workspace/service_urls.json` を書く。
4. `deploy.py status` で SSH 接続コマンド、ログ取得コマンド、各 URL を表示する。
5. 4048 文字超過時は deploy を止める。
6. README と表示文言の文字化けを直す。

### 中期対応

Vast の手動 UI に頼らない運用体験を整える段階です。

1. JupyterLab を `8888` で起動し、token 付き URL を表示する。
2. `deploy.py exec` と `deploy.py doctor` を追加する。
3. セットアップ中のフェーズ進捗をローカルにリアルタイム表示する。
4. ComfyUI/AnimaWebUI/Jupyter の health check を deploy 完了条件にする。
5. 出力ファイル取得用の `pull-output` を追加する。

### 長期対応

安定運用向けです。

1. 毎回 onstart で巨大セットアップするのではなく、専用 Docker image を作る。
2. モデル保存先を永続 volume または network volume に寄せる。
3. systemd 風の supervisor、または `tmux`/`supervisord` でサービス管理する。
4. Cloudflare Tunnel は任意機能にし、Vast proxy/direct を主経路にする。
5. インスタンス作成、セットアップ、利用、破棄の状態遷移を state file に詳細保存する。

## 推奨するコマンド設計

```text
python deploy.py deploy
python deploy.py status
python deploy.py ssh
python deploy.py exec "nvidia-smi"
python deploy.py logs setup --tail 100
python deploy.py logs comfyui --tail 100
python deploy.py logs anima --tail 100
python deploy.py doctor
python deploy.py pull-output
python deploy.py destroy
```

`status` の理想出力は次のような形です。

```text
Instance: 12345678 running RTX 4090 $0.45/h

URLs:
  ComfyUI:    https://proxy-12345678-8188.vast.ai
  AnimaWebUI: https://proxy-12345678-8501.vast.ai
  Jupyter:    https://proxy-12345678-8888.vast.ai/lab?token=...
  Cloudflare: https://....trycloudflare.com

SSH:
  ssh -p 12345 root@1.2.3.4

Logs:
  python deploy.py logs setup --tail 100
```

## 結論

現状の最大の問題は、Vast.ai の「サーバは立つが手で触りにくい」という制約に対して、プロジェクト側の運用補助がまだ薄いことです。ComfyUI/AnimaWebUI の自動起動だけなら今の構成でも成立しますが、実際の運用では失敗調査、リモート修正、モデル確認、出力回収が必ず発生します。

まずは SSH/logs/exec/status の整備と JupyterLab の追加を優先するのがよいです。これだけで、Vast の Jupyter やコンソールを手動で使えない不便さの大半はプロジェクト側で吸収できます。その後、onstart の 4048 文字制限対策と専用 Docker image 化に進むのが堅実です。
