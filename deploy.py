"""
anima-vast deploy — Vast.ai に ComfyUI + AnimaWebUI をデプロイ
"""
import configparser
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.ini")
STATE_FILE = os.path.join(SCRIPT_DIR, ".instance_state.json")
ANIMA_SRC_DIR = os.path.join(SCRIPT_DIR, "anima-webui")

API_BASE = "https://console.vast.ai/api/v0"
SETUP_REPO_DEFAULT = "https://github.com/BeamManP/anima-vast.git"


# ──── 設定 ────

def load_config():
    if not os.path.exists(CONFIG_PATH):
        print("[ERROR] config.ini が見つかりません。")
        print("        config.ini.example をコピーして作成してください。")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg


def get_api_key(cfg):
    key = cfg.get("vastai", "api_key", fallback="")
    if not key or key == "YOUR_API_KEY_HERE":
        key = os.environ.get("VASTAI_API_KEY", "")
    if not key:
        print("[ERROR] Vast.ai APIキーが未設定です。config.ini を確認してください。")
        sys.exit(1)
    return key


def get_env_vars(cfg):
    """インスタンスに渡す環境変数を組み立てる"""
    env = {}
    for ini_section, ini_key, env_name in [
        ("anima", "passphrase", "ANIMA_PASSPHRASE"),
        ("models", "civitai_api_key", "CIVITAI_API_KEY"),
        ("github", "gh_token", "GH_TOKEN"),
        ("github", "branch", "DEPLOY_BRANCH"),
    ]:
        val = cfg.get(ini_section, ini_key, fallback="").strip()
        if val:
            env[env_name] = val

    # anima-webui のリポパス
    repo = cfg.get("github", "anima_repo",
                    fallback="https://github.com/BeamManP/anima-webui.git")
    repo_path = repo.split("github.com/")[-1].replace(".git", "")
    env["ANIMA_REPO_PATH"] = repo_path

    # セットアップリポ URL
    env["SETUP_REPO"] = cfg.get("github", "setup_repo", fallback=SETUP_REPO_DEFAULT)

    return env


# ──── Vast.ai API ────

def api(method, endpoint, api_key, data=None):
    url = f"{API_BASE}{endpoint}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[API ERROR] {e.code}: {e.read().decode('utf-8', errors='replace')}")
        raise
    except urllib.error.URLError as e:
        print(f"[NETWORK ERROR] {e.reason}")
        raise


def search_offers(api_key, cfg):
    query = {
        "gpu_name": {"eq": cfg.get("gpu", "gpu_name", fallback="RTX_4090")},
        "num_gpus": {"eq": cfg.getint("gpu", "num_gpus", fallback=1)},
        "gpu_ram": {"gte": cfg.getfloat("gpu", "min_gpu_ram", fallback=23)},
        "disk_space": {"gte": cfg.getfloat("instance", "disk_size", fallback=40)},
        "inet_down": {"gte": cfg.getfloat("instance", "min_inet_down", fallback=100)},
        "reliability": {"gte": cfg.getfloat("instance", "min_reliability", fallback=0.9)},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "type": cfg.get("instance", "instance_type", fallback="ondemand"),
        "order": [["dph_total", "asc"]],
    }
    if cfg.getboolean("instance", "secure_cloud", fallback=True):
        query["datacenter"] = {"eq": True}
    max_price = cfg.getfloat("instance", "max_price", fallback=0)
    if max_price > 0:
        query["dph_total"] = {"lte": max_price}

    result = api("GET", f"/bundles?q={urllib.parse.quote(json.dumps(query))}", api_key)
    return result.get("offers", [])


def get_instance(api_key, instance_id):
    result = api("GET", f"/instances/{instance_id}/", api_key)
    instances = result.get("instances")
    if isinstance(instances, dict):
        return instances
    if isinstance(instances, list) and instances:
        return instances[0]
    return result


def get_instances(api_key):
    return api("GET", "/instances/", api_key).get("instances", [])


# ──── onstart: bootstrap 方式 ────

def build_onstart_cmd(cfg):
    """短い bootstrap スクリプトを生成。本体は git clone で取得する。"""
    setup_repo = cfg.get("github", "setup_repo", fallback=SETUP_REPO_DEFAULT)

    bootstrap = (
        "#!/bin/bash\n"
        "set -e\n"
        "apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1\n"
        f'git clone --depth 1 "{setup_repo}" /workspace/anima-vast 2>/dev/null '
        "|| (cd /workspace/anima-vast && git pull --ff-only)\n"
        "bash /workspace/anima-vast/onstart.sh\n"
    )

    cmd_len = len(bootstrap)
    print(f"[INFO] onstart コマンド長: {cmd_len} / 4048 文字")
    if cmd_len > 4048:
        print(f"[ERROR] 4048 文字超過 ({cmd_len})。デプロイを中断します。")
        sys.exit(1)
    return bootstrap


# ──── 状態ファイル ────

def save_state(instance_id, offer_id=None):
    with open(STATE_FILE, "w") as f:
        json.dump({"instance_id": instance_id, "offer_id": offer_id}, f)


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def clear_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


# ──── SSH ヘルパー ────

def get_ssh_public_key(cfg):
    inline = cfg.get("ssh", "public_key", fallback="").strip()
    if inline:
        return inline
    pub_path = os.path.expanduser(
        cfg.get("ssh", "public_key_path", fallback="~/.ssh/id_ed25519.pub"))
    if os.path.exists(pub_path):
        with open(pub_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    print(f"[WARN] SSH 公開鍵が見つかりません: {pub_path}")
    return ""


def get_ssh_private_key_path(cfg):
    key_path = os.path.expanduser(
        cfg.get("ssh", "private_key_path", fallback="~/.ssh/id_ed25519"))
    return key_path if os.path.exists(key_path) else ""


def attach_ssh_key(api_key, instance_id, ssh_key, retries=6, delay=5):
    if not ssh_key:
        print("[WARN] SSH 鍵アタッチをスキップ。")
        return False
    for attempt in range(1, retries + 1):
        try:
            result = api("POST", f"/instances/{instance_id}/ssh/", api_key,
                         {"ssh_key": ssh_key})
            if result.get("success"):
                print("[OK]   SSH 鍵アタッチ完了。")
                return True
            print(f"[WARN] SSH 鍵アタッチ失敗: {result}")
        except Exception as e:
            print(f"[WARN] SSH 鍵アタッチ リトライ {attempt}/{retries}: {e}")
        time.sleep(delay)
    print("[WARN] SSH 鍵アタッチ未完了。")
    return False


def ssh_base_args(cfg, ssh_port, ssh_host):
    args = [
        "ssh", "-p", str(ssh_port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "IdentitiesOnly=yes",
        "-o", "LogLevel=ERROR",
    ]
    private_key = get_ssh_private_key_path(cfg)
    if private_key:
        args.extend(["-i", private_key])
    args.append(f"root@{ssh_host}")
    return args


def get_ssh_info(api_key, cfg):
    """state から SSH 接続情報を取得。返り値: (instance, ssh_host, ssh_port) or None"""
    state = load_state()
    if not state:
        print("[INFO] アクティブなインスタンスはありません。")
        return None

    instance_id = state["instance_id"]
    try:
        inst = get_instance(api_key, instance_id)
    except Exception as e:
        print(f"[ERROR] {e}")
        return None

    if inst.get("actual_status") != "running":
        print(f"[ERROR] インスタンス未起動 ({inst.get('actual_status')})")
        return None

    ssh_host = inst.get("ssh_host", "")
    ssh_port = inst.get("ssh_port", "")
    if not ssh_host or not ssh_port:
        print("[ERROR] SSH情報が取得できません。")
        return None

    return inst, ssh_host, ssh_port


def ssh_run(cfg, ssh_host, ssh_port, command):
    """SSH 経由でコマンドを実行し、結果を返す"""
    args = ssh_base_args(cfg, ssh_port, ssh_host) + [command]
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


# ──── URL 構築 ────

def build_urls(instance, cfg):
    urls = {}
    public_ip = instance.get("public_ipaddr", "")
    comfyui_port = str(cfg.getint("ports", "comfyui_port", fallback=8188))
    anima_port = str(cfg.getint("ports", "anima_port", fallback=8501))

    for port_key, mappings in instance.get("ports", {}).items():
        port_num = port_key.split("/")[0]
        if mappings and mappings[0].get("HostPort"):
            host_ip = mappings[0].get("HostIp", "")
            if host_ip in ("0.0.0.0", "::", ""):
                host_ip = public_ip
            host_port = mappings[0]["HostPort"]
            if host_ip and host_port:
                if port_num == comfyui_port:
                    urls["comfyui"] = f"http://{host_ip}:{host_port}"
                elif port_num == anima_port:
                    urls["anima"] = f"http://{host_ip}:{host_port}"

    instance_id = instance.get("id")
    if instance_id:
        urls.setdefault("comfyui", f"https://proxy-{instance_id}-{comfyui_port}.vast.ai")
        urls.setdefault("anima", f"https://proxy-{instance_id}-{anima_port}.vast.ai")

    return urls


def print_urls(urls):
    for label, key in [("AnimaWebUI", "anima"), ("ComfyUI", "comfyui")]:
        if key in urls:
            print(f"  {label:12s} {urls[key]}")


# ──── 起動待ち ────

def wait_for_running(api_key, instance_id, timeout=600):
    start = time.time()
    last_status = ""
    while time.time() - start < timeout:
        try:
            inst = get_instance(api_key, instance_id)
            status = inst.get("actual_status", inst.get("status_msg", "unknown"))
            if status != last_status:
                print(f"       状態: {status} ({int(time.time() - start)}s)")
                last_status = status
            if status == "running":
                return inst
            if status in ("exited", "error", "destroyed"):
                print(f"[ERROR] 異常終了: {status}")
                return None
        except Exception as e:
            print(f"       (リトライ: {e})")
        time.sleep(10)
    print(f"[ERROR] タイムアウト ({int(time.time() - start)}s)")
    return None


# ──── コマンド ────

def cmd_deploy(cfg, api_key):
    state = load_state()
    if state:
        print(f"[INFO] 既存インスタンスあり: {state['instance_id']}")
        print("       先に destroy してください。")
        return

    gpu_name = cfg.get("gpu", "gpu_name", fallback="RTX_4090")
    print(f"\n[1/4] {gpu_name} を検索中...")
    offers = search_offers(api_key, cfg)
    if not offers:
        print("[ERROR] 条件に合うGPUが見つかりません。")
        return

    best = offers[0]
    price = best.get("dph_total", 0)
    print(f"[OK]   {len(offers)} 件 | 最安: ${price:.4f}/h "
          f"| {best.get('gpu_name')} ({best.get('gpu_ram', 0):.0f}GB)")

    offer_id = best["id"]
    print(f"\n[2/4] インスタンス作成中... (Offer: {offer_id})")

    onstart_cmd = build_onstart_cmd(cfg)
    env_vars = get_env_vars(cfg)

    comfyui_port = cfg.getint("ports", "comfyui_port", fallback=8188)
    anima_port = cfg.getint("ports", "anima_port", fallback=8501)
    env_vars[f"-p {comfyui_port}:{comfyui_port}"] = "1"
    env_vars[f"-p {anima_port}:{anima_port}"] = "1"

    result = api("PUT", f"/asks/{offer_id}/", api_key, {
        "client_id": "me",
        "image": cfg.get("docker", "image", fallback="pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel"),
        "disk": cfg.getfloat("instance", "disk_size", fallback=40),
        "env": env_vars,
        "onstart": onstart_cmd,
        "runtype": "ssh_direct ssh_proxy",
    })

    if not result.get("success"):
        print(f"[ERROR] 作成失敗: {result}")
        return

    instance_id = result.get("new_contract")
    save_state(instance_id, offer_id)
    print(f"[OK]   ID: {instance_id}")

    ssh_pub = get_ssh_public_key(cfg)
    if ssh_pub:
        attach_ssh_key(api_key, instance_id, ssh_pub)

    print(f"\n[3/4] 起動待ち...")
    instance = wait_for_running(api_key, instance_id)
    if not instance:
        return

    print(f"\n[4/4] セットアップ中（数分待ってからアクセス）")
    urls = build_urls(instance, cfg)
    ssh_host = instance.get("ssh_host", "")
    ssh_port = instance.get("ssh_port", "")

    print(f"\n{'='*40}")
    print(f"  デプロイ完了！ (${price:.4f}/h)")
    print(f"{'='*40}\n")
    print_urls(urls)
    if ssh_host and ssh_port:
        print(f"\n  SSH: ssh -p {ssh_port} root@{ssh_host}")
    print(f"\n  セットアップ進捗:")
    print(f"    python deploy.py logs setup")
    print(f"  Instance ID: {instance_id}\n")


def cmd_status(cfg, api_key):
    state = load_state()
    if not state:
        print("[INFO] アクティブなインスタンスはありません。")
        return

    instance_id = state["instance_id"]
    try:
        inst = get_instance(api_key, instance_id)
    except Exception as e:
        print(f"[ERROR] 取得失敗: {e}")
        return

    status = inst.get("actual_status", inst.get("status_msg", "unknown"))
    price = inst.get("dph_total", 0)
    ssh_host = inst.get("ssh_host", "")
    ssh_port = inst.get("ssh_port", "")

    print(f"  ID:      {instance_id}")
    print(f"  状態:    {status}")
    print(f"  GPU:     {inst.get('gpu_name', '?')}")
    print(f"  料金:    ${price:.4f}/h")

    if status == "running":
        print()
        print_urls(build_urls(inst, cfg))

        if ssh_host and ssh_port:
            # setup_status を取得
            try:
                r = ssh_run(cfg, ssh_host, ssh_port,
                            "cat /workspace/setup_status.txt 2>/dev/null || echo 'unknown'")
                setup_status = r.stdout.strip() if r.returncode == 0 else "unknown"
            except Exception:
                setup_status = "unreachable"

            print(f"\n  セットアップ: {setup_status}")
            print(f"  SSH: ssh -p {ssh_port} root@{ssh_host}")

            # Cloudflare URL を取得
            try:
                r = ssh_run(cfg, ssh_host, ssh_port,
                            "grep -oP 'https://[a-z0-9-]+\\.trycloudflare\\.com' "
                            "/workspace/cloudflared.log 2>/dev/null | tail -1")
                cf_url = r.stdout.strip() if r.returncode == 0 else ""
                if cf_url:
                    print(f"  Cloudflare:  {cf_url}")
            except Exception:
                pass

    print()


def cmd_destroy(cfg, api_key):
    state = load_state()
    if not state:
        print("[INFO] アクティブなインスタンスはありません。")
        return

    instance_id = state["instance_id"]
    print(f"[WARN] インスタンス {instance_id} を破棄します。(y/N): ", end="")
    if input().strip().lower() != "y":
        print("[INFO] キャンセル。")
        return

    try:
        api("DELETE", f"/instances/{instance_id}/", api_key)
        clear_state()
        print(f"[OK] 破棄完了: {instance_id}")
    except Exception as e:
        print(f"[ERROR] 破棄失敗: {e}")


def cmd_list(cfg, api_key):
    instances = get_instances(api_key)
    if not instances:
        print("[INFO] アクティブなインスタンスはありません。")
        return
    print(f"{'ID':<12} {'GPU':<16} {'Status':<12} {'$/h':<10}")
    print("-" * 50)
    for i in instances:
        print(f"{i.get('id', '?'):<12} {i.get('gpu_name', '?'):<16} "
              f"{i.get('actual_status', '?'):<12} ${i.get('dph_total', 0):.4f}")


def cmd_ssh(cfg, api_key):
    info = get_ssh_info(api_key, cfg)
    if not info:
        return
    inst, ssh_host, ssh_port = info
    args = ssh_base_args(cfg, ssh_port, ssh_host)
    cmd_str = " ".join(args)
    print(f"[INFO] SSH 接続コマンド:\n  {cmd_str}\n")
    print("接続しますか？ (Y/n): ", end="")
    if input().strip().lower() != "n":
        subprocess.run(args)


def cmd_exec(cfg, api_key):
    if len(sys.argv) < 3:
        print("使い方: python deploy.py exec \"command\"")
        return

    remote_cmd = " ".join(sys.argv[2:])
    info = get_ssh_info(api_key, cfg)
    if not info:
        return
    inst, ssh_host, ssh_port = info

    print(f"[EXEC] {remote_cmd}")
    try:
        r = ssh_run(cfg, ssh_host, ssh_port, remote_cmd)
        if r.stdout:
            print(r.stdout, end="")
        if r.stderr:
            print(r.stderr, end="", file=sys.stderr)
        sys.exit(r.returncode)
    except subprocess.TimeoutExpired:
        print("[ERROR] タイムアウト (30s)")
    except Exception as e:
        print(f"[ERROR] {e}")


LOG_FILES = {
    "setup": "/workspace/setup.log",
    "comfyui": "/workspace/comfyui.log",
    "anima": "/workspace/anima.log",
    "cloudflared": "/workspace/cloudflared.log",
}


def cmd_logs(cfg, api_key):
    if len(sys.argv) < 3 or sys.argv[2] not in LOG_FILES:
        print(f"使い方: python deploy.py logs <{' | '.join(LOG_FILES.keys())}> [--tail N]")
        return

    log_name = sys.argv[2]
    log_path = LOG_FILES[log_name]

    tail_n = 50
    if "--tail" in sys.argv:
        idx = sys.argv.index("--tail")
        if idx + 1 < len(sys.argv):
            tail_n = int(sys.argv[idx + 1])

    info = get_ssh_info(api_key, cfg)
    if not info:
        return
    inst, ssh_host, ssh_port = info

    print(f"[LOGS] {log_name} (tail {tail_n})\n")
    try:
        r = ssh_run(cfg, ssh_host, ssh_port, f"tail -n {tail_n} {log_path} 2>/dev/null || echo '(ファイル未作成)'")
        print(r.stdout if r.stdout else "(空)")
    except subprocess.TimeoutExpired:
        print("[ERROR] タイムアウト")
    except Exception as e:
        print(f"[ERROR] {e}")


def cmd_upload(cfg, api_key):
    info = get_ssh_info(api_key, cfg)
    if not info:
        return
    inst, ssh_host, ssh_port = info

    if not os.path.isdir(ANIMA_SRC_DIR):
        print(f"[ERROR] anima-webui が見つかりません: {ANIMA_SRC_DIR}")
        print("        git submodule update --init を実行してください。")
        return

    tar_file = os.path.join(SCRIPT_DIR, ".anima-upload.tar.gz")
    excludes = ["node_modules", "data/images", "data/anima.db*", ".git"]
    exclude_args = " ".join([f'--exclude="{e}"' for e in excludes])

    print("[1/3] パッケージ中...")
    ret = subprocess.run(
        f'tar -czf "{tar_file}" -C "{SCRIPT_DIR}" {exclude_args} "anima-webui"',
        shell=True
    ).returncode
    if ret != 0:
        print("[ERROR] tar 失敗")
        return

    size_mb = os.path.getsize(tar_file) / (1024 * 1024)
    print(f"[OK]   {size_mb:.1f} MB")

    print("[2/3] SCP 転送中...")
    scp_args = [
        "scp", "-P", str(ssh_port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "IdentitiesOnly=yes",
    ]
    private_key = get_ssh_private_key_path(cfg)
    if private_key:
        scp_args.extend(["-i", private_key])
    scp_args.extend([tar_file, f"root@{ssh_host}:/workspace/anima-upload.tar.gz"])
    ret = subprocess.run(scp_args).returncode
    if ret != 0:
        print("[ERROR] SCP 失敗")
        os.remove(tar_file)
        return

    print("[3/3] リモート展開 + 再起動中...")
    r = ssh_run(cfg, ssh_host, ssh_port,
                "cd /workspace && tar -xzf anima-upload.tar.gz && rm anima-upload.tar.gz "
                "&& cd anima-webui && npm install --production 2>/dev/null "
                "&& pkill -f 'node server/index.js' 2>/dev/null; "
                "cd /workspace/anima-webui && nohup node server/index.js > /workspace/anima.log 2>&1 & "
                "echo DONE")
    os.remove(tar_file)

    if r.returncode == 0:
        print("\n[OK] 転送+再起動完了！")
        print_urls(build_urls(inst, cfg))
    else:
        print("[ERROR] リモート展開失敗")
        if r.stderr:
            print(r.stderr)


# ──── エントリーポイント ────

COMMANDS = {
    "deploy":  ("Vast.ai にデプロイ", cmd_deploy),
    "status":  ("インスタンス状態確認", cmd_status),
    "ssh":     ("SSH 接続", cmd_ssh),
    "exec":    ("リモートコマンド実行", cmd_exec),
    "logs":    ("リモートログ表示", cmd_logs),
    "upload":  ("WebUI をインスタンスに転送", cmd_upload),
    "destroy": ("インスタンス破棄", cmd_destroy),
    "list":    ("全インスタンス一覧", cmd_list),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("使い方: python deploy.py <command>\n")
        for name, (desc, _) in COMMANDS.items():
            print(f"  {name:<10} {desc}")
        print()
        return

    cfg = load_config()
    api_key = get_api_key(cfg)
    COMMANDS[sys.argv[1]][1](cfg, api_key)


if __name__ == "__main__":
    main()
