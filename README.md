# Charmed PostgreSQL Chaos Tests Automation

A robust, fully-autonomous Python orchestrator for provisioning, deploying, and chaos-testing Canonical's Charmed PostgreSQL using LXD and Juju.

## Overview

The `test_pg_chaos.py` tool is designed to provide a completely hands-off end-to-end testing pipeline. It can dynamically provision its own isolated LXD virtual machine, bootstrap a Juju controller, deploy a robust, multi-site PostgreSQL architecture, populate it with synthetic data via `sysbench`, and inject infrastructure-level chaos (e.g., node loss, software upgrades) to validate database resiliency.

If a failure occurs during the chaos testing, the script features a failsafe routine that automatically extracts and packages Juju statuses, Juju debug logs, and Patroni/Raft data from the remote node for post-mortem forensics.

## Key Features

- **Zero-Touch Provisioning**: Spin up an isolated, correctly sized LXD VM (`--spawn-vm`) directly from the script.
- **Auto-Detection**: If a VM isn't specified, it automatically searches your host (Multipass, LXD) for active VMs containing `pg` or `postgres` in the name and targets them.
- **Resilient Execution**: Wraps remote commands in an intelligent SSH retry loop to gracefully handle transient network drops or package upgrade interruptions.
- **Failsafe Log Archiving**: A global exception handler ensures that if a command fails, times out, or crashes, all relevant Juju and PostgreSQL/Raft logs are automatically bundled into a timestamped directory on the VM.

## Prerequisites

- **Python 3**
- **LXD** (`sudo snap install lxd --channel=5.21/stable`)
- Your user must have `sudo` privileges to initialize the LXD VM and inject the SSH key natively.

## Usage

You can run the script step-by-step or chain arguments to execute an end-to-end pipeline in one go.

```bash
usage: test_pg_chaos.py [-h] [--setup] [--baseline] [--test]
                        [--branch {stable,candidate,beta,edge}]
                        [--profile {testing,production}] [--vm-ip VM_IP]
                        [--load-time LOAD_TIME] [--spawn-vm VM_NAME]
                        [--cpus CPUS] [--ram RAM] [--disk DISK]
                        [--ssh-pub-key SSH_PUB_KEY] [--collect-logs]
```

### Core Actions
- `--setup`: Triggers the deployment of the testing infrastructure (Juju bootstrap, site1/site2 models, database relations). This runs safely in the background on the VM to survive package upgrades.
- `--baseline`: Creates the `sysbench` test database, provisions tables, and records an initial traffic baseline.
- `--test`: Runs the chaos engineering suite (Primary node kill, minor/major upgrades) alongside a continuous `sysbench` load to track transaction drops.
- `--collect-logs`: A standalone action to manually extract Juju trace logs, statuses, and Patroni data from the target VM into a `failure_logs_<TIMESTAMP>` directory.
- `--agentchaos`: Activates the **AI vs. AI Wargame**. The script acts as a Game Master, pitting a local LLM BlackHat attacker against a WhiteHat defender in a turn-based battle to break and recover the cluster.

### AI Wargame Flags (requires local Ollama on port 11434)
- `--turns <NUM>`: Number of turns to run the wargame (default: 1).
- `--model <NAME>`: Symmetric mode. Uses the specified fuzzy-matched model for both attacker and defender.
- `--blackhat-model <NAME>`: Uses the specified model specifically for the attacker.
- `--whitehat-model <NAME>`: Uses the specified model specifically for the defender.

### Provisioning Flags
- `--spawn-vm <NAME>`: Creates a fresh LXD virtual machine.
- `--cpus <CORES>`: Sets the VM CPU count (default: 8).
- `--ram <SIZE>`: Sets the VM memory limit (default: 24GB).
- `--disk <SIZE>`: Sets the VM root storage size (default: 64GiB).
- `--ssh-pub-key <PATH>`: Path to the public SSH key to inject via cloud-init for seamless remote execution (default: `~/.ssh/id_ed25519_antigravity.pub`).

### Advanced Flags
- `--branch`: Target snap channel for PostgreSQL upgrades (default: `edge`).
- `--profile`: Juju configuration profile to deploy (`testing` or `production`).
- `--vm-ip`: Hardcode a target IP address, bypassing auto-detection.
- `--load-time`: The duration (in seconds) of the sysbench traffic loads (default: `137`).

## Examples

**1. Complete End-to-End Run**
Provision a highly-resourced VM, deploy Juju/Postgres, prepare the baseline, and run chaos tests:
```bash
python3 test_pg_chaos.py --spawn-vm pg-chaos-node --cpus 12 --ram 32GB --disk 100GiB --setup --baseline --test
```

**2. Re-run Tests on an Existing Auto-Detected VM**
If your VM is already spawned and setup, just run the tests. The script will automatically detect the VM's IP if it has "pg" in its name.
```bash
python3 test_pg_chaos.py --test
```

**3. Manually Collect Logs on Failure**
If you want to pull diagnostic data from a broken target IP.
```bash
python3 test_pg_chaos.py --collect-logs --vm-ip 10.83.30.177
```
