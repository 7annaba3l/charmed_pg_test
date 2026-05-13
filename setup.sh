#!/bin/bash
set -e

echo "Starting setup on target VM directly..."

sudo apt -y update && sudo apt -y upgrade
sudo snap install juju --channel=3.6/stable
sudo snap install lxd --channel=5.21/stable

echo "Initializing LXD..."
sudo lxd init --auto
sudo lxc network set lxdbr0 ipv6.address none
sudo iptables -P FORWARD ACCEPT

echo "Bootstrapping Juju..."
juju bootstrap localhost localhost || echo "Already bootstrapped"

echo "Deploying site1..."
juju add-model site1 || echo "site1 exists"
juju deploy postgresql db1 --channel 16/stable --config profile=testing --base ubuntu@24.04 || echo "already deployed db1"
juju deploy data-integrator di1 --config database-name=testdb --base ubuntu@24.04 || echo "already deployed di1"
juju relate db1 di1 || echo "already related"
juju add-unit db1 -n 1 || echo "unit already added"
juju config db1 synchronous-mode-strict=false
juju offer db1:replication-offer replication-offer || echo "already offered"

echo "Deploying site2..."
juju add-model site2 || echo "site2 exists"
juju deploy postgresql db2 --channel 16/stable --config profile=testing --base ubuntu@24.04 || echo "already deployed db2"
juju add-unit db2 -n 1 || echo "unit already added"
juju config db2 synchronous-mode-strict=false

echo "Consuming replication..."
sleep 5
juju consume site1.replication-offer || echo "already consumed"
juju integrate replication-offer db2:replication || echo "already integrated"

sudo apt install -y sysbench

echo "Setup script finished successfully."
touch ~/setup_done
