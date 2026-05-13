#!/usr/bin/env python3
import subprocess
import time
import argparse
import re
import sys

SSH_CMD = []
TMUX_SESSION = "pg_tests"
SYSBENCH_TIME = 137

def detect_vm_ip():
    """Attempt to detect the IP of a running VM with 'pg' or 'postgres' in the name. Returns IP if exactly one is found."""
    candidates = []

    try:
        output = subprocess.check_output(["multipass", "list"], text=True, stderr=subprocess.DEVNULL)
        for line in output.splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "Running":
                name, ip = parts[0], parts[2]
                if "pg" in name.lower() or "postgres" in name.lower():
                    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", ip)
                    if match:
                        candidates.append(match.group(1))
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    try:
        output = subprocess.check_output(["lxc", "list", "-c", "n4", "--format", "csv"], text=True, stderr=subprocess.DEVNULL)
        for line in output.splitlines():
            parts = line.split(",")
            if len(parts) >= 2:
                name = parts[0]
                ip_raw = parts[1]
                if "pg" in name.lower() or "postgres" in name.lower():
                    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", ip_raw)
                    if match:
                        candidates.append(match.group(1))
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    try:
        output = subprocess.check_output(["virsh", "list", "--state-running", "--name"], text=True, stderr=subprocess.DEVNULL)
        for name in output.splitlines():
            name = name.strip()
            if name and ("pg" in name.lower() or "postgres" in name.lower()):
                try:
                    addr_out = subprocess.check_output(["virsh", "domifaddr", name], text=True, stderr=subprocess.DEVNULL)
                    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", addr_out)
                    if match:
                        candidates.append(match.group(1))
                except (FileNotFoundError, subprocess.CalledProcessError):
                    pass
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    if len(candidates) == 1:
        return candidates[0]
    elif len(candidates) > 1:
        print(f"Warning: Multiple matching VMs found ({candidates}). Defaulting to fallback.")
    return None


def set_globals(vm_ip, load_time):
    global SSH_CMD, SYSBENCH_TIME
    SSH_CMD = ["ssh", "-o", "StrictHostKeyChecking=no", "-i", "~/.ssh/id_ed25519_antigravity", f"ubuntu@{vm_ip}"]
    SYSBENCH_TIME = load_time

def run_remote(cmd, capture=True):
    """Run a command synchronously on the remote host."""
    full_cmd = SSH_CMD + [cmd]
    print(f"--> Executing: {cmd}")
    result = subprocess.run(full_cmd, text=True, capture_output=capture)
    if result.returncode != 0:
        print(f"Warning/Error from command: {result.stderr if capture else 'Check output'}")
    return result.stdout.strip() if capture else ""

def init_tmux():
    """Start tmux session if it doesn't exist."""
    run_remote(f"tmux new-session -s {TMUX_SESSION} -d || true")

def run_sysbench_workloads(creds, test_name):
    """Starts sysbench workloads A to E in background using tmux splits/windows."""
    print(f"Starting workloads for {test_name}")
    user = creds['user']
    password = creds['password']
    
    # Common sysbench arguments
    sb_base = f"sysbench --pgsql-user={user} --pgsql-password={password} --pgsql-port=5432 --pgsql-db=testdb --db-driver='pgsql' --threads='1' --tables='5' --report-interval=1 --time={SYSBENCH_TIME}"
    
    window = test_name.replace(" ", "_")
    run_remote(f"tmux new-window -t {TMUX_SESSION} -n {window} || true")
    
    workloads = [
        ("A", creds['site1_primary'], "oltp_write_only"),
        ("B", creds['site1_primary'], "oltp_read_only"),
        ("C", creds['site1_standby'], "oltp_read_only"),
        ("D", creds['site2_primary'], "oltp_read_only"), # site2 standby1 is its primary member
        ("E", creds['site2_standby'], "oltp_read_only")
    ]
    
    # Instead of tmux splits which might run out of space, we use background nohup inside tmux
    for name, ip, mode in workloads:
        cmd = f"nohup {sb_base} --pgsql-host={ip} {mode} run > ~/sysbench_{window}_{name}.log 2>&1 &"
        run_remote(f"tmux send-keys -t {TMUX_SESSION}:{window} \"{cmd}\" C-m")
    
    print("Workloads started in background.")

def setup_infrastructure(profile):
    print(f"Setting up infrastructure with profile {profile}...")
    script = f"""
    sudo apt -y update && sudo apt -y upgrade
    sudo snap install juju --channel=3.6/stable
    sudo snap install lxd --channel=5.21/stable
    sudo lxd init --auto
    sudo lxc network set lxdbr0 ipv6.address none
    sudo iptables -P FORWARD ACCEPT
    juju bootstrap localhost localhost || true
    juju add-model site1 || true
    juju deploy postgresql db1 --channel 16/stable --config profile={profile} --base ubuntu@24.04 || true
    juju deploy data-integrator di1 --config database-name=testdb --base ubuntu@24.04 || true
    juju relate db1 di1 || true
    juju add-unit db1 -n 1 || true
    juju config db1 synchronous-mode-strict=false
    juju offer db1:replication-offer replication-offer || true
    juju add-model site2 || true
    juju deploy postgresql db2 --channel 16/stable --config profile={profile} --base ubuntu@24.04 || true
    juju add-unit db2 -n 1 || true
    juju config db2 synchronous-mode-strict=false
    sleep 10
    juju consume site1.replication-offer || true
    juju integrate replication-offer db2:replication || true
    sudo apt install -y sysbench postgresql-client
    """
    for line in script.strip().split("\n"):
        if line.strip():
            run_remote(line.strip(), capture=False)

def wait_for_active(model_name, app_name=""):
    print(f"Waiting for {app_name} in {model_name} to settle...")
    while True:
        status = run_remote(f"juju status -m {model_name} --format=json")
        if status and '"status": "maintenance"' not in status and '"status": "waiting"' not in status and '"status": "allocating"' not in status:
            print(f"Deployments in {model_name} are active.")
            break
        print("Still waiting...")
        time.sleep(15)

def get_credentials_and_ips():
    print("Fetching credentials and IPs...")
    output = run_remote("juju run di1/leader get-credentials -m site1")
    
    creds = {}
    user_match = re.search(r"username:\s+(\S+)", output)
    pass_match = re.search(r"password:\s+(\S+)", output)
    if user_match and pass_match:
        creds['user'] = user_match.group(1)
        creds['password'] = pass_match.group(1)
    
    # We fetch IPs using juju status
    s1_status = run_remote("juju status -m site1")
    s2_status = run_remote("juju status -m site2")
    
    # Extract IPs
    site1_ips = re.findall(r"db1/\d+\*?\s+\S+\s+\S+\s+\d+\s+([\d\.]+)", s1_status)
    site2_ips = re.findall(r"db2/\d+\*?\s+\S+\s+\S+\s+\d+\s+([\d\.]+)", s2_status)
    
    # Usually db1/0 is primary, db1/1 is standby
    # Need to properly parse primary vs standby using juju status output
    # For simplicity assuming order or parsing Primary tag
    def get_primary_ip(status_text, app):
        match = re.search(rf"{app}/\d+\*\s+\S+\s+\S+\s+\d+\s+([\d\.]+)\s+.*?Primary", status_text)
        return match.group(1) if match else None

    def get_standby_ip(status_text, app):
        match = re.search(rf"{app}/\d+\s+\S+\s+\S+\s+\d+\s+([\d\.]+)\s+.*?(?!Primary)", status_text)
        return match.group(1) if match else None

    creds['site1_primary'] = get_primary_ip(s1_status, "db1") or site1_ips[0]
    creds['site1_standby'] = get_standby_ip(s1_status, "db1") or site1_ips[1]
    
    creds['site2_primary'] = get_primary_ip(s2_status, "db2") or site2_ips[0]
    creds['site2_standby'] = get_standby_ip(s2_status, "db2") or site2_ips[1]
    
    print("Credentials loaded:", creds)
    return creds

def baseline_validation(creds):
    print("Running sysbench prepare...")
    run_remote(f"sysbench --pgsql-host={creds['site1_primary']} --pgsql-user={creds['user']} --pgsql-password={creds['password']} --pgsql-port=5432 --pgsql-db=testdb --db-driver='pgsql' --threads='1' --tables='5' --table-size='1000000' oltp_read_only prepare")
    
    print("Verifying DB size...")
    query = "SELECT pg_size_pretty(SUM(pg_total_relation_size(c.oid))::bigint) AS total_size FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner WHERE r.rolname = current_user AND c.relkind IN ('r', 'p');"
    size = run_remote(f"PGPASSWORD='{creds['password']}' psql --host={creds['site1_primary']} --username={creds['user']} --port=5432 testdb -c \"{query}\"")
    print(size)

def test_replication_creation(creds):
    print("\n--- Test: Replication creation ---")
    run_sysbench_workloads(creds, "rep_create")
    time.sleep(10)
    run_remote("juju run -m site1 db1/leader create-replication || true")
    time.sleep(SYSBENCH_TIME)
    print("Expected: Replication is established under load")

def test_upgrade_site2(creds, target_branch):
    print("\n--- Test: Upgrade site2 ---")
    run_sysbench_workloads(creds, "upg_site2")
    time.sleep(10)
    run_remote("juju run db2/leader pre-refresh-check -m site2 || true")
    run_remote(f"juju refresh db2 --channel 16/{target_branch} -m site2")
    time.sleep(15)
    wait_for_active("site2")
    # Determine unit numbers dynamically
    s2 = run_remote("juju status -m site2")
    units = re.findall(r"db2/(\d+)", s2)
    for u in reversed(units):  # usually standbys first
        run_remote(f"juju run db2/{u} resume-refresh -m site2 || true")
        time.sleep(15)
        wait_for_active("site2")
    time.sleep(SYSBENCH_TIME)
    print("Expected: E fails then D fails separately. Others function.")

def test_upgrade_site1(creds, target_branch):
    print("\n--- Test: Upgrade site1 ---")
    run_sysbench_workloads(creds, "upg_site1")
    time.sleep(10)
    run_remote("juju run db1/leader pre-refresh-check -m site1 || true")
    run_remote(f"juju refresh db1 --channel 16/{target_branch} -m site1")
    time.sleep(15)
    wait_for_active("site1")
    s1 = run_remote("juju status -m site1")
    units = re.findall(r"db1/(\d+)", s1)
    for u in reversed(units):
        run_remote(f"juju run db1/{u} resume-refresh -m site1 || true")
        time.sleep(15)
        wait_for_active("site1")
    time.sleep(SYSBENCH_TIME)
    print("Expected: C fails then A+B fail separately. Primary switches once.")

def test_watcher_addition(creds, target_branch, profile):
    print("\n--- Test: Watcher addition ---")
    run_sysbench_workloads(creds, "watch_add")
    time.sleep(10)
    run_remote(f"juju deploy postgresql-watcher w1 --channel 16/{target_branch} --config profile={profile} --base ubuntu@24.04 -m site1 || true")
    run_remote(f"juju deploy postgresql-watcher w2 --channel 16/{target_branch} --config profile={profile} --base ubuntu@24.04 -m site2 || true")
    run_remote("juju relate db1 w1 -m site1 || true")
    run_remote("juju relate db2 w2 -m site2 || true")
    time.sleep(SYSBENCH_TIME)
    print("Expected: Nothing should fail")

def test_units_addition(creds):
    print("\n--- Test: Units addition ---")
    run_sysbench_workloads(creds, "unit_add")
    time.sleep(10)
    run_remote("juju add-unit db1 -n 2 -m site1")
    run_remote("juju add-unit db2 -n 1 -m site2")
    time.sleep(SYSBENCH_TIME)
    print("Expected: Nothing should fail")

def test_node_loss(creds):
    print("\n--- Test: Node loss (site1) ---")
    run_sysbench_workloads(creds, "node_loss")
    time.sleep(20)
    # Get lxc container name for site1 primary
    s1 = run_remote("juju status -m site1")
    mach = re.search(r"db1/\d+\*\s+.*?(\d+)\s+[\d\.]+\s+.*?Primary", s1)
    if mach:
        mach_id = mach.group(1)
        lxc_name = run_remote(f"juju show-machine {mach_id} -m site1 | grep 'instance-id' | awk '{{print $2}}'").strip()
        print(f"Stopping LXC container: {lxc_name}")
        run_remote(f"lxc stop {lxc_name} --force")
    time.sleep(SYSBENCH_TIME)
    print("Expected: A+B should fail. New primary elected.")

def test_abrupt_shutdown(creds):
    print("\n--- Test: Abrupt shutdown of site1 standby ---")
    run_sysbench_workloads(creds, "shut_standby")
    time.sleep(20)
    s1 = run_remote("juju status -m site1")
    # Find standby machine
    mach = re.search(r"db1/\d+\s+.*?(\d+)\s+[\d\.]+\s+.*?(?!Primary)", s1)
    if mach:
        mach_id = mach.group(1)
        lxc_name = run_remote(f"juju show-machine {mach_id} -m site1 | grep 'instance-id' | awk '{{print $2}}'").strip()
        print(f"Stopping standby LXC container: {lxc_name}")
        run_remote(f"lxc stop {lxc_name} --force")
    time.sleep(SYSBENCH_TIME)
    print("Expected: A+C fail. Others functional.")
    
    print("Recovering...")
    # Add recovery logic
    run_remote(f"juju remove-unit db1/{mach_id} -m site1 || true")
    run_remote("juju add-unit db1 -n 1 -m site1")

def run_chaos_tests(branch, profile):
    init_tmux()
    creds = get_credentials_and_ips()
    test_replication_creation(creds)
    test_upgrade_site2(creds, branch)
    test_upgrade_site1(creds, branch)
    test_watcher_addition(creds, branch, profile)
    test_units_addition(creds)
    test_node_loss(creds)
    test_abrupt_shutdown(creds)
    print("All tests completed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Charmed PostgreSQL Chaos Tests Automation")
    parser.add_argument("--setup", action="store_true", help="Run setup phase")
    parser.add_argument("--baseline", action="store_true", help="Run baseline preparation")
    parser.add_argument("--test", action="store_true", help="Run chaos tests")
    parser.add_argument("--branch", choices=["stable", "candidate", "beta", "edge"], default="edge", help="The branch to use for PostgreSQL upgrades and watchers (default: edge)")
    parser.add_argument("--profile", choices=["testing", "production"], default="testing", help="The profile config to use (default: testing)")
    parser.add_argument("--vm-ip", default=None, help="The IP address of the target VM (default: auto-detected or 10.83.30.177)")
    parser.add_argument("--load-time", type=int, default=137, help="Traffic loading time in seconds for sysbench (default: 137)")
    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    if args.vm_ip is None:
        args.vm_ip = detect_vm_ip() or "10.83.30.177"
        print(f"Using VM IP: {args.vm_ip}")

    set_globals(args.vm_ip, args.load_time)

    if args.setup:
        setup_infrastructure(args.profile)
        wait_for_active("site1", "db1")
        wait_for_active("site2", "db2")
    if args.baseline:
        creds = get_credentials_and_ips()
        baseline_validation(creds)
    if args.test:
        run_chaos_tests(args.branch, args.profile)
