"""
Anima WebUI - ワンクリック起動スクリプト
bat から呼ばれて、デプロイ → セットアップ待ち → ブラウザで開く → 待機 → 終了時に自動削除
"""
import atexit
import json
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from datetime import datetime

import deploy as dp

FRONT_URL = "https://anima-webui.pages.dev"

# ──── グローバル状態 ────

_cleanup_done = False
_api_key = None
_cfg = None
_instance_id = None
_race_instance_ids = []


def p(msg="", **kwargs):
    print(msg, flush=True, **kwargs)


# ──── クリーンアップ ────

def cleanup():
    global _cleanup_done, _instance_id, _race_instance_ids
    if _cleanup_done:
        return
    _cleanup_done = True

    to_destroy = set()
    if _instance_id:
        to_destroy.add(_instance_id)
    for rid in _race_instance_ids:
        to_destroy.add(rid)

    if not to_destroy or not _api_key:
        return

    p()
    p("=" * 50)
    p("  インスタンスを破棄しています...")
    p("=" * 50)

    for iid in to_destroy:
        try:
            dp.api("DELETE", f"/instances/{iid}/", _api_key)
            p(f"  [OK] {iid} を破棄しました。")
        except Exception as e:
            p(f"  [ERROR] {iid} の破棄に失敗: {e}")
            p(f"  ※ 手動で削除: python deploy.py destroy")

    if _instance_id in to_destroy:
        dp.clear_state()

    p("  ※ 課金は停止されます。")
    p()


def signal_handler(signum, frame):
    cleanup()
    sys.exit(0)


# ──── SSH ヘルパー ────

def ssh_read_remote(host, port, cmd, timeout=5):
    if not _cfg:
        return None
    try:
        args = dp.ssh_base_args(_cfg, port, host) + [cmd]
        result = subprocess.run(
            args, capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout + 5, stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


# ──── レース ────

def run_race(api_key, instance_ids, timeout=600):
    elapsed = 0
    interval = 10
    last_status = {}

    while elapsed < timeout:
        for iid in list(instance_ids):
            try:
                inst = dp.get_instance(api_key, iid)
                status = inst.get("actual_status", "unknown")
                if status != last_status.get(iid):
                    p(f"       {iid}: {status} ({elapsed}s)")
                    last_status[iid] = status
                if status == "running":
                    p(f"\n  >> {iid} が running に到達!")
                    return iid, inst
                if status in ("exited", "error", "destroyed"):
                    instance_ids.remove(iid)
            except Exception:
                pass

        if not instance_ids:
            return None, None

        loading = len(instance_ids)
        p(f"\r  レース中... ({elapsed}s) [待機: {loading}台]          ", end="")
        time.sleep(interval)
        elapsed += interval

    p()
    return None, None


# ──── セットアップ待ち ────

def wait_for_setup(api_key, instance_id, anima_url, timeout=600, interval=10):
    elapsed = 0
    last_phase = ""
    last_log_lines = set()
    ssh_host, ssh_port = None, None

    try:
        inst = dp.get_instance(api_key, instance_id)
        ssh_host = inst.get("ssh_host", "")
        ssh_port = inst.get("ssh_port", "")
    except Exception:
        pass

    phase_labels = {
        "Phase 1": "System packages",
        "Phase 2": "ComfyUI install",
        "Phase 3": "Custom Nodes",
        "Phase 4:": "HF models DL",
        "Phase 4b": "CivitAI models DL",
        "Phase 5": "AnimaWebUI setup",
        "Phase 6": "Start Services",
        "Phase 7": "Tunnel",
        "done": "Setup complete!",
    }

    while elapsed < timeout:
        # AnimaWebUI 応答チェック
        if anima_url:
            try:
                req = urllib.request.Request(anima_url, method="HEAD")
                urllib.request.urlopen(req, timeout=5)
                p()
                return True
            except Exception:
                pass

        # SSH 経由でセットアップ進捗
        if ssh_host and ssh_port:
            status_text = ssh_read_remote(ssh_host, ssh_port,
                                          "cat /workspace/setup_status.txt 2>/dev/null")
            if status_text:
                phase = status_text.strip()
                if phase != last_phase:
                    last_phase = phase
                    label = phase
                    for key, val in phase_labels.items():
                        if key in phase:
                            label = val
                            break
                    p(f"\n  [{label}]")

            log_tail = ssh_read_remote(ssh_host, ssh_port,
                                       "tail -5 /workspace/setup.log 2>/dev/null")
            if log_tail:
                for line in log_tail.strip().split("\n"):
                    line = line.strip()
                    if not line or line in last_log_lines:
                        continue
                    last_log_lines.add(line)
                    if "[DL]" in line:
                        p(f"    DL: {line.split('[DL]')[-1].strip()}")
                    elif "[OK]" in line:
                        p(f"    OK: {line.split('[OK]')[-1].strip()}")
                    elif "[SKIP]" in line:
                        p(f"    SKIP: {line.split('[SKIP]')[-1].strip()}")

        remaining = timeout - elapsed
        mins, secs = divmod(remaining, 60)
        phase_info = f" | {last_phase}" if last_phase else ""
        p(f"\r  waiting... ({mins}m{secs}s){phase_info}                    ", end="")

        time.sleep(interval)
        elapsed += interval

    p()
    return False


# ──── 待機ループ ────

def wait_loop(urls):
    anima_url = urls.get("anima", "")
    comfyui_url = urls.get("comfyui", "")

    p()
    p("=" * 50)
    p("  Anima WebUI running!")
    p("=" * 50)
    p()
    if anima_url:
        p(f"  AnimaWebUI: {anima_url}")
    if comfyui_url:
        p(f"  ComfyUI:    {comfyui_url}")
    p()
    p("  -----------------------------------------")
    p("  Close this window to auto-destroy instance")
    p("  -----------------------------------------")
    p()

    stop_event = threading.Event()
    last_status = {"queue": 0, "running": 0}

    def status_poller():
        if not anima_url:
            return
        api_url = f"{anima_url}/api/comfy/status"
        gen_count = 0
        err_count = 0

        while not stop_event.is_set():
            try:
                req = urllib.request.Request(api_url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                if not data.get("alive", True):
                    err_count += 1
                    if err_count == 1 or err_count % 5 == 0:
                        now = datetime.now().strftime("%H:%M:%S")
                        p(f"  [{now}] ComfyUI disconnected (x{err_count})")
                    stop_event.wait(3)
                    continue

                if err_count > 0:
                    now = datetime.now().strftime("%H:%M:%S")
                    p(f"  [{now}] ComfyUI reconnected (after {err_count} errors)")
                err_count = 0

                queue = data.get("queue", {})
                running = queue.get("running", 0)
                pending = queue.get("pending", 0)
                total = running + pending
                now = datetime.now().strftime("%H:%M:%S")

                if running > 0:
                    msg = f"  [{now}] Generating... | queue: {total}"
                elif pending > 0:
                    msg = f"  [{now}] Queue: {pending}"
                else:
                    if last_status["running"] > 0 or last_status["queue"] > 0:
                        gen_count += 1
                        p(f"  [{now}] Done! (total: {gen_count})")
                    msg = f"  [{now}] Idle | queue: 0"

                if total != last_status["queue"] or running != last_status["running"]:
                    p(msg)

                last_status["queue"] = total
                last_status["running"] = running

            except Exception as e:
                err_count += 1
                if err_count == 1:
                    now = datetime.now().strftime("%H:%M:%S")
                    p(f"  [{now}] Connection error: {e}")

            stop_event.wait(3)

    if anima_url:
        t = threading.Thread(target=status_poller, daemon=True)
        t.start()

    p("  Press Enter to stop and destroy instance...")
    p()

    try:
        input("  >>> ")
    except (EOFError, KeyboardInterrupt):
        pass

    stop_event.set()
    cleanup()


# ──── メイン ────

def main():
    global _api_key, _cfg, _instance_id, _race_instance_ids

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal_handler)
    atexit.register(cleanup)

    _cfg = dp.load_config()
    _api_key = dp.get_api_key(_cfg)

    p()
    p("  ==========================================")
    p("       Anima WebUI - Quick Launch")
    p("  ==========================================")
    p("    Auto deploy -> Auto setup -> Auto destroy")
    p("    Close window to stop and destroy")
    p("  ==========================================")
    p()

    # ── 既存インスタンスチェック ──
    state = dp.load_state()
    if state:
        _instance_id = state["instance_id"]
        p(f"[INFO] Existing instance found: {_instance_id}")

        try:
            inst = dp.get_instance(_api_key, _instance_id)
            status = inst.get("actual_status", "unknown")
        except Exception:
            status = "unknown"

        if status == "running":
            p(f"  Status: {status}")
            urls = dp.build_urls(inst, _cfg)
            anima_url = urls.get("anima", "")
            if anima_url:
                p(f"  URL: {anima_url}")
                p(f"  Front: {FRONT_URL}")
                webbrowser.open(FRONT_URL)
                wait_loop(urls)
                return
        elif status in ("exited", "error", "destroyed", "offline", ""):
            p(f"  Status: {status} -> deploying fresh instance.")
            dp.clear_state()
            _instance_id = None
        else:
            p(f"  Status: {status} -> deploying fresh instance.")
            dp.clear_state()
            _instance_id = None

    # ── デプロイ ──
    race_count = _cfg.getint("instance", "race_count", fallback=4)
    gpu_name = _cfg.get("gpu", "gpu_name", fallback="RTX_4090")
    p(f"[1/5] Searching {gpu_name}...")
    offers = dp.search_offers(_api_key, _cfg)

    if not offers:
        p("[ERROR] No matching GPU offers found.")
        _instance_id = None
        input("Enter to exit...")
        return

    race_offers = offers[:race_count]
    p(f"[OK]   {len(offers)} offers | Racing {len(race_offers)} instances")
    for i, o in enumerate(race_offers):
        price = o.get("dph_total", 0)
        p(f"       #{i+1} ${price:.4f}/h "
          f"| {o.get('gpu_name')} ({o.get('gpu_ram', 0):.0f}GB) "
          f"| DL: {o.get('inet_down', 0):.0f}Mbps "
          f"| machine {o.get('machine_id')}")

    p()
    p(f"[2/5] Creating {len(race_offers)} instances...")

    onstart_cmd = dp.build_onstart_cmd(_cfg)
    env_vars = dp.get_env_vars(_cfg)
    comfyui_port = _cfg.getint("ports", "comfyui_port", fallback=8188)
    anima_port = _cfg.getint("ports", "anima_port", fallback=8501)
    env_vars[f"-p {comfyui_port}:{comfyui_port}"] = "1"
    env_vars[f"-p {anima_port}:{anima_port}"] = "1"

    create_payload = {
        "client_id": "me",
        "image": _cfg.get("docker", "image",
                          fallback="pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel"),
        "disk": _cfg.getfloat("instance", "disk_size", fallback=40),
        "env": env_vars,
        "onstart": onstart_cmd,
        "runtype": "ssh_direct ssh_proxy",
    }

    _race_instance_ids = []
    race_offer_map = {}
    for i, offer in enumerate(race_offers):
        try:
            result = dp.api("PUT", f"/asks/{offer['id']}/", _api_key, create_payload)
            if result.get("success"):
                iid = result["new_contract"]
                _race_instance_ids.append(iid)
                race_offer_map[iid] = offer
                p(f"  #{i+1} OK: {iid}")
            else:
                p(f"  #{i+1} Failed: {result}")
        except Exception as e:
            p(f"  #{i+1} Error: {e}")
        if i < len(race_offers) - 1:
            time.sleep(2)

    if not _race_instance_ids:
        p("[ERROR] All instance creation failed.")
        input("Enter to exit...")
        return

    # SSH鍵アタッチ
    ssh_pub = dp.get_ssh_public_key(_cfg)
    if ssh_pub:
        for iid in _race_instance_ids:
            try:
                dp.attach_ssh_key(_api_key, iid, ssh_pub, retries=2, delay=3)
            except Exception:
                pass
            time.sleep(0.5)

    p()
    p(f"[3/5] Racing {len(_race_instance_ids)} instances...")

    winner_id, instance = run_race(_api_key, list(_race_instance_ids))

    if not winner_id:
        p("[ERROR] Race timed out. No winner.")
        cleanup()
        input("Enter to exit...")
        return

    p(f"[OK]   Winner: {winner_id}")

    # 敗者破棄
    losers = [iid for iid in _race_instance_ids if iid != winner_id]
    if losers:
        p(f"       Destroying {len(losers)} losers...")
        for lid in losers:
            try:
                dp.api("DELETE", f"/instances/{lid}/", _api_key)
                p(f"       {lid} destroyed")
            except Exception:
                pass
            time.sleep(0.5)

    _instance_id = winner_id
    _race_instance_ids.clear()
    winner_offer = race_offer_map.get(winner_id, {})
    dp.save_state(_instance_id, winner_offer.get("id"))
    price = winner_offer.get("dph_total", 0)

    urls = dp.build_urls(instance, _cfg)
    anima_url = urls.get("anima", "")

    # ── セットアップ待ち ──
    p()
    p(f"[4/5] Waiting for setup... (${price:.4f}/h)")
    p()

    if anima_url:
        ready = wait_for_setup(_api_key, _instance_id, anima_url,
                               timeout=600, interval=10)
        if ready:
            p()
            p("[OK]   AnimaWebUI is ready!")
        else:
            p()
            p("[WARN] Setup timed out. May still be in progress.")
    else:
        p("  Waiting 60s...")
        time.sleep(60)

    # ── ブラウザ ──
    p()
    p("[5/5] Opening browser...")
    webbrowser.open(FRONT_URL)

    # ── 待機ループ ──
    wait_loop(urls)


if __name__ == "__main__":
    main()
