import os
import psutil
import shutil
import time
import json
import threading
import logging
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request
from functools import wraps
import humanize
import requests
from collections import deque
import signal
import sys
import platform
import subprocess
import re
import uuid
from pathlib import Path

app = Flask(__name__)

# Configuration
REFRESH_INTERVAL = 3000  # milliseconds
MAX_LOG_ENTRIES = 100
SYSTEM_LOG = []
MONITORING_INTERVAL = 1000  # milliseconds
CPU_HISTORY_SIZE = 60
MEMORY_HISTORY_SIZE = 60
NETWORK_HISTORY_SIZE = 60
PROCESS_HISTORY_SIZE = 20
TEMPERATURE_HISTORY_SIZE = 60
DISK_HISTORY_SIZE = 60

# Data structures for historical tracking
cpu_history = deque(maxlen=CPU_HISTORY_SIZE)
memory_history = deque(maxlen=MEMORY_HISTORY_SIZE)
network_history = deque(maxlen=NETWORK_HISTORY_SIZE)
process_history = deque(maxlen=PROCESS_HISTORY_SIZE)
temperature_history = deque(maxlen=TEMPERATURE_HISTORY_SIZE)
disk_history = deque(maxlen=DISK_HISTORY_SIZE)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('system_monitor.log'),
        logging.StreamHandler()
    ]
)

# Global variables for system state
system_state = {
    'is_running': True,
    'last_network': None,
    'last_time': time.time(),
    'network_stats': {'bytes_sent_per_sec': 0, 'bytes_recv_per_sec': 0},
    'last_temperature_check': 0,
    'temperature_data': [],
    'last_disk_check': 0,
    'disk_data': []
}

def log_system_event(level, message):
    """Log system events with timestamp"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = {
        'timestamp': timestamp,
        'level': level,
        'message': message
    }
    SYSTEM_LOG.append(log_entry)
    if len(SYSTEM_LOG) > MAX_LOG_ENTRIES:
        SYSTEM_LOG.pop(0)
    # Also log to file
    log_level = logging.INFO if level == 'info' else logging.WARNING if level == 'warning' else logging.ERROR
    logging.log(log_level, message)

def get_process_list():
    """Get process list with error handling"""
    processes = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'username']):
            try:
                pinfo = proc.as_dict(['pid', 'name', 'cpu_percent', 'memory_percent', 'username'])
                if pinfo['cpu_percent'] is not None:
                    processes.append(pinfo)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as e:
        log_system_event('error', f'Error getting process list: {str(e)}')
    return sorted(
        [p for p in processes if p['cpu_percent'] is not None],
        key=lambda x: x['cpu_percent'],
        reverse=True
    )[:10]

def get_system_info():
    """Get system information with error handling"""
    try:
        # CPU Information
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_cores = psutil.cpu_count()
        cpu_freq = psutil.cpu_freq().current / 1000 if psutil.cpu_freq() else 0
        
        # Memory Information
        memory = psutil.virtual_memory()
        
        # Disk Information
        disk = psutil.disk_usage('/')
        
        # System Uptime
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = str(datetime.now() - boot_time).split('.')[0]
        
        # Network Statistics
        network = psutil.net_io_counters()
        if not system_state['last_network']:
            system_state['last_network'] = network
            system_state['last_time'] = time.time()
        time_delta = time.time() - system_state['last_time']
        if time_delta > 0:
            system_state['network_stats'] = {
                'bytes_sent_per_sec': (network.bytes_sent - system_state['last_network'].bytes_sent) / time_delta,
                'bytes_recv_per_sec': (network.bytes_recv - system_state['last_network'].bytes_recv) / time_delta
            }
        system_state['last_network'] = network
        system_state['last_time'] = time.time()
        
        # Temperature Information
        temperatures = []
        try:
            if hasattr(psutil, 'sensors_temperatures'):
                temps = psutil.sensors_temperatures()
                for name, entries in temps.items():
                    for entry in entries:
                        temperatures.append({
                            'label': name,
                            'current': entry.current,
                            'high': entry.high or 100
                        })
                system_state['temperature_data'] = temperatures
        except Exception as e:
            log_system_event('warning', f'Could not get temperature readings: {str(e)}')
        
        # Disk Partition Information
        disk_partitions = []
        try:
            partitions = psutil.disk_partitions()
            for partition in partitions:
                try:
                    usage = psutil.disk_usage(partition.mountpoint)
                    disk_partitions.append({
                        'device': partition.device,
                        'mountpoint': partition.mountpoint,
                        'fstype': partition.fstype,
                        'total': humanize.naturalsize(usage.total),
                        'used': humanize.naturalsize(usage.used),
                        'free': humanize.naturalsize(usage.free),
                        'percent': round((usage.used / usage.total) * 100, 2)
                    })
                except PermissionError:
                    continue
            system_state['disk_data'] = disk_partitions
        except Exception as e:
            log_system_event('warning', f'Could not get disk partitions: {str(e)}')
        
        # System Alerts
        alerts = []
        if cpu_percent > 80:
            alerts.append({
                'type': 'danger',
                'icon': 'exclamation-triangle-fill',
                'message': f'High CPU usage: {cpu_percent}%'
            })
        if memory.percent > 80:
            alerts.append({
                'type': 'warning',
                'icon': 'exclamation-triangle-fill',
                'message': f'High memory usage: {memory.percent}%'
            })
        if disk.percent > 80:
            alerts.append({
                'type': 'warning',
                'icon': 'exclamation-triangle-fill',
                'message': f'Low disk space: {disk.percent}% used'
            })
        
        # Add to history
        cpu_history.append(cpu_percent)
        memory_history.append(memory.percent)
        network_history.append(system_state['network_stats'])
        temperature_history.append(temperatures)
        disk_history.append(disk_partitions)
        
        return {
            'cpu_percent': cpu_percent,
            'cpu_cores': cpu_cores,
            'cpu_freq': round(cpu_freq, 2),
            'memory': memory._asdict(),
            'disk': disk._asdict(),
            'uptime': uptime,
            'boot_time': boot_time.strftime('%Y-%m-%d %H:%M:%S'),
            'network': system_state['network_stats'],
            'temperatures': temperatures,
            'alerts': alerts,
            'disk_partitions': disk_partitions
        }
    except Exception as e:
        log_system_event('error', f'Error getting system info: {str(e)}')
        return {}

def get_file_list(path):
    """Get file list with error handling"""
    try:
        files = []
        for item in os.listdir(path):
            full_path = os.path.join(path, item)
            try:
                stat = os.stat(full_path)
                files.append({
                    'name': item,
                    'path': full_path,
                    'type': 'directory' if os.path.isdir(full_path) else 'file',
                    'size': humanize.naturalsize(stat.st_size) if not os.path.isdir(full_path) else '-',
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'permissions': oct(stat.st_mode)[-3:]
                })
            except (PermissionError, FileNotFoundError):
                continue
        return sorted(files, key=lambda x: (x['type'] != 'directory', x['name'].lower()))[:20]
    except Exception as e:
        log_system_event('error', f'Error accessing {path}: {str(e)}')
        return []

def get_system_performance_data():
    """Get performance data for charts"""
    return {
        'cpu_history': list(cpu_history),
        'memory_history': list(memory_history),
        'network_history': list(network_history)
    }

def get_top_processes_by_memory():
    """Get top processes by memory usage"""
    processes = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'memory_percent']):
            try:
                pinfo = proc.as_dict(['pid', 'name', 'memory_percent'])
                if pinfo['memory_percent'] is not None:
                    processes.append(pinfo)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as e:
        log_system_event('error', f'Error getting process list: {str(e)}')
    return sorted(
        [p for p in processes if p['memory_percent'] is not None],
        key=lambda x: x['memory_percent'],
        reverse=True
    )[:10]

def get_top_processes_by_cpu():
    """Get top processes by CPU usage"""
    processes = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent']):
            try:
                pinfo = proc.as_dict(['pid', 'name', 'cpu_percent'])
                if pinfo['cpu_percent'] is not None:
                    processes.append(pinfo)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as e:
        log_system_event('error', f'Error getting process list: {str(e)}')
    return sorted(
        [p for p in processes if p['cpu_percent'] is not None],
        key=lambda x: x['cpu_percent'],
        reverse=True
    )[:10]

def get_network_interfaces():
    """Get network interface information"""
    try:
        interfaces = psutil.net_if_addrs()
        interface_data = []
        # Handle the case where AF_INET might not be available
        af_inet = getattr(psutil, 'AF_INET', None)
        if af_inet is None:
            # Fallback to a known constant if AF_INET is not defined
            af_inet = 2  # This is the standard value for AF_INET on most platforms
        
        for interface_name, interface_addresses in interfaces.items():
            for address in interface_addresses:
                if address.family == af_inet:
                    interface_data.append({
                        'name': interface_name,
                        'ip': address.address,
                        'netmask': address.netmask,
                        'broadcast': address.broadcast
                    })
        return interface_data
    except Exception as e:
        log_system_event('error', f'Error getting network interfaces: {str(e)}')
        return []

def get_disk_partitions():
    """Get disk partition information"""
    try:
        partitions = psutil.disk_partitions()
        partition_data = []
        for partition in partitions:
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                partition_data.append({
                    'device': partition.device,
                    'mountpoint': partition.mountpoint,
                    'fstype': partition.fstype,
                    'total': humanize.naturalsize(usage.total),
                    'used': humanize.naturalsize(usage.used),
                    'free': humanize.naturalsize(usage.free),
                    'percent': round((usage.used / usage.total) * 100, 2)
                })
            except PermissionError:
                continue
        return partition_data
    except Exception as e:
        log_system_event('error', f'Error getting disk partitions: {str(e)}')
        return []

def get_system_uptime():
    """Get detailed system uptime information"""
    try:
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot_time
        uptime_seconds = int(uptime.total_seconds())
        days = uptime_seconds // 86400
        hours = (uptime_seconds % 86400) // 3600
        minutes = (uptime_seconds % 3600) // 60
        return {
            'days': days,
            'hours': hours,
            'minutes': minutes,
            'formatted': str(uptime).split('.')[0]
        }
    except Exception as e:
        log_system_event('error', f'Error getting system uptime: {str(e)}')
        return {}

def get_user_info():
    """Get current user information"""
    try:
        return {
            'username': os.getlogin(),
            'system': platform.system(),
            'release': platform.release(),
            'machine': platform.machine(),
            'processor': platform.processor()
        }
    except Exception as e:
        log_system_event('error', f'Error getting user info: {str(e)}')
        return {}

def get_system_load_avg():
    """Get system load average (Linux/macOS only)"""
    try:
        if hasattr(os, 'getloadavg'):
            load_avg = os.getloadavg()
            return {
                'one_min': round(load_avg[0], 2),
                'five_min': round(load_avg[1], 2),
                'fifteen_min': round(load_avg[2], 2)
            }
        else:
            return None
    except Exception as e:
        log_system_event('error', f'Error getting load average: {str(e)}')
        return None

def get_system_resources():
    """Get comprehensive system resources information"""
    try:
        # Get all process details
        processes = []
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'username']):
                try:
                    pinfo = proc.as_dict(['pid', 'name', 'cpu_percent', 'memory_percent', 'username'])
                    if pinfo['cpu_percent'] is not None:
                        processes.append(pinfo)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception as e:
            log_system_event('error', f'Error getting process list: {str(e)}')
        
        # Get current system info
        system_info = get_system_info()
        
        return {
            'processes': processes,
            'system_info': system_info,
            'timestamp': datetime.now().isoformat()
        }
    except Exception as e:
        log_system_event('error', f'Error getting system resources: {str(e)}')
        return {}

# HTML Template with enhanced features
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Enhanced System Monitor Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --primary-color: #4a5568;
            --secondary-color: #2d3748;
            --accent-color: #667eea;
            --success-color: #48bb78;
            --warning-color: #ed8936;
            --danger-color: #f56565;
            --bg-color: #f7fafc;
            --text-color: #2d3748;
            --card-bg: #ffffff;
            --border-color: #e2e8f0;
        }
        [data-theme="dark"] {
            --primary-color: #718096;
            --secondary-color: #4a5568;
            --accent-color: #9f7aea;
            --bg-color: #1a202c;
            --text-color: #e2e8f0;
            --card-bg: #2d3748;
            --border-color: #4a5568;
        }
        body {
            background-color: var(--bg-color);
            color: var(--text-color);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            margin: 0;
            padding: 0;
            height: 100vh;
            overflow-x: hidden;
            transition: background-color 0.3s ease, color 0.3s ease;
        }
        .navbar {
            background: var(--secondary-color);
            padding: 0.5rem 1rem;
            box-shadow: 0 2px 4px rgba(0,0,0,.1);
        }
        .main-container {
            height: calc(100vh - 56px);
            overflow-y: auto;
            padding: 1rem;
        }
        .stat-card {
            background: var(--card-bg);
            border-radius: 8px;
            padding: 1rem;
            margin-bottom: 1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.12);
            transition: transform 0.2s;
            border: 1px solid var(--border-color);
        }
        .stat-card:hover {
            transform: translateY(-2px);
        }
        .stat-icon {
            width: 40px;
            height: 40px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            margin-bottom: 0.5rem;
        }
        .progress {
            height: 8px;
            background-color: var(--border-color);
        }
        .progress-bar {
            transition: width 0.3s ease;
        }
        .chart-container {
            height: 200px;
            margin: 1rem 0;
        }
        .file-list, .process-list {
            background: var(--card-bg);
            border-radius: 8px;
            padding: 1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.12);
            max-height: 300px;
            overflow-y: auto;
            border: 1px solid var(--border-color);
        }
        .table {
            font-size: 0.875rem;
        }
        .log-container {
            background: var(--card-bg);
            color: var(--text-color);
            padding: 1rem;
            border-radius: 8px;
            height: 200px;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 0.875rem;
            border: 1px solid var(--border-color);
        }
        .alert-custom {
            border-radius: 8px;
            padding: 0.75rem;
            margin-bottom: 1rem;
            border: none;
        }
        .btn-sm {
            padding: 0.25rem 0.5rem;
            font-size: 0.875rem;
        }
        .modal-content {
            background: var(--card-bg);
            border-radius: 8px;
        }
        .modal-header {
            background: var(--secondary-color);
            color: white;
            border-radius: 8px 8px 0 0;
        }
        .theme-toggle {
            position: fixed;
            bottom: 20px;
            right: 20px;
            z-index: 1000;
        }
        .status-indicator {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 0.5rem;
        }
        .status-good { background-color: var(--success-color); }
        .status-warning { background-color: var(--warning-color); }
        .status-danger { background-color: var(--danger-color); }
        .log-entry {
            padding: 0.25rem;
            margin: 0.25rem 0;
            border-radius: 4px;
        }
        .log-entry.info {
            background-color: rgba(72, 187, 120, 0.1);
        }
        .log-entry.warning {
            background-color: rgba(237, 137, 54, 0.1);
        }
        .log-entry.error {
            background-color: rgba(245, 101, 101, 0.1);
        }
        .tab-content {
            min-height: 300px;
        }
        .history-chart-container {
            height: 300px;
            margin-top: 1rem;
        }
        .process-detail {
            max-height: 300px;
            overflow-y: auto;
        }
        .badge-process {
            font-size: 0.7em;
        }
        .cpu-usage-bar {
            height: 10px;
        }
        .memory-usage-bar {
            height: 10px;
        }
        .disk-usage-bar {
            height: 10px;
        }
        /* New styles for expanded features */
        .resource-card {
            border-left: 4px solid var(--accent-color);
        }
        .resource-card.warning {
            border-left: 4px solid var(--warning-color);
        }
        .resource-card.danger {
            border-left: 4px solid var(--danger-color);
        }
        .resource-card.success {
            border-left: 4px solid var(--success-color);
        }
        .system-status {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .system-status span {
            margin-right: 10px;
        }
        .status-badge {
            padding: 0.25rem 0.5rem;
            border-radius: 50px;
            font-size: 0.8rem;
        }
        .status-badge.active {
            background-color: var(--success-color);
            color: white;
        }
        .status-badge.inactive {
            background-color: var(--danger-color);
            color: white;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
        }
        .stat-box {
            background: var(--card-bg);
            border-radius: 8px;
            padding: 1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.12);
            border: 1px solid var(--border-color);
        }
        .stat-value {
            font-size: 1.5rem;
            font-weight: bold;
        }
        .stat-label {
            font-size: 0.875rem;
            color: var(--text-color);
        }
        .process-table th {
            font-size: 0.75rem;
        }
        .process-table td {
            font-size: 0.75rem;
        }
        .memory-breakdown {
            display: flex;
            align-items: center;
        }
        .memory-bar {
            flex-grow: 1;
            height: 10px;
            background-color: var(--border-color);
            border-radius: 5px;
            overflow: hidden;
            margin: 0 10px;
        }
        .memory-used {
            height: 100%;
            background-color: var(--success-color);
        }
        .memory-free {
            height: 100%;
            background-color: var(--primary-color);
        }
        .memory-available {
            height: 100%;
            background-color: var(--warning-color);
        }
        .disk-usage-container {
            display: flex;
            align-items: center;
            margin-bottom: 0.5rem;
        }
        .disk-usage-bar {
            flex-grow: 1;
            height: 10px;
            background-color: var(--border-color);
            border-radius: 5px;
            overflow: hidden;
            margin: 0 10px;
        }
        .disk-usage {
            height: 100%;
            background-color: var(--warning-color);
        }
        .disk-free {
            height: 100%;
            background-color: var(--success-color);
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-dark">
        <div class="container-fluid">
            <span class="navbar-brand mb-0 h1">
                <i class="bi bi-speedometer2"></i> Enhanced System Monitor
            </span>
            <span class="text-white" id="current-time"></span>
        </div>
    </nav>
    <div class="main-container">
        <!-- System Alerts -->
        <div id="system-alerts"></div>
        
        <!-- System Status Overview -->
        <div class="stats-grid">
            <div class="stat-box">
                <div class="stat-value" id="cpu-percent-display">0%</div>
                <div class="stat-label">CPU Usage</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="memory-percent-display">0%</div>
                <div class="stat-label">Memory Usage</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="disk-percent-display">0%</div>
                <div class="stat-label">Disk Usage</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="network-download-display">0 KB/s</div>
                <div class="stat-label">Network Download</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="network-upload-display">0 KB/s</div>
                <div class="stat-label">Network Upload</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="uptime-display">0d 0h 0m</div>
                <div class="stat-label">System Uptime</div>
            </div>
        </div>

        <!-- Detailed System Stats Grid -->
        <div class="row mt-3">
            <div class="col-md-3">
                <div class="stat-card resource-card" id="cpu-card">
                    <div class="stat-icon bg-primary text-white">
                        <i class="bi bi-cpu"></i>
                    </div>
                    <h6 class="mb-2">CPU</h6>
                    <div class="progress mb-2">
                        <div class="progress-bar bg-primary" id="cpu-bar"></div>
                    </div>
                    <small class="text-muted">
                        <span id="cpu-percent">0%</span>
                        <div id="cpu-cores"></div>
                    </small>
                </div>
            </div>
            <div class="col-md-3">
                <div class="stat-card resource-card" id="memory-card">
                    <div class="stat-icon bg-info text-white">
                        <i class="bi bi-memory"></i>
                    </div>
                    <h6 class="mb-2">Memory</h6>
                    <div class="progress mb-2">
                        <div class="progress-bar bg-info" id="memory-bar"></div>
                    </div>
                    <small class="text-muted">
                        <span id="memory-percent">0%</span>
                        <div id="memory-details"></div>
                    </small>
                </div>
            </div>
            <div class="col-md-3">
                <div class="stat-card resource-card" id="disk-card">
                    <div class="stat-icon bg-warning text-white">
                        <i class="bi bi-hdd"></i>
                    </div>
                    <h6 class="mb-2">Disk</h6>
                    <div class="progress mb-2">
                        <div class="progress-bar bg-warning" id="disk-bar"></div>
                    </div>
                    <small class="text-muted">
                        <span id="disk-percent">0%</span>
                        <div id="disk-details"></div>
                    </small>
                </div>
            </div>
            <div class="col-md-3">
                <div class="stat-card resource-card" id="uptime-card">
                    <div class="stat-icon bg-success text-white">
                        <i class="bi bi-clock"></i>
                    </div>
                    <h6 class="mb-2">Uptime</h6>
                    <div id="uptime" class="mb-1"></div>
                    <small class="text-muted" id="boot-time"></small>
                </div>
            </div>
        </div>
        
        <!-- Network and Temperature -->
        <div class="row mt-3">
            <div class="col-md-6">
                <div class="stat-card">
                    <h6 class="mb-3">
                        <i class="bi bi-wifi"></i> Network
                    </h6>
                    <div class="row mb-2">
                        <div class="col-6">
                            <small>↓ <span id="network-download">0 KB/s</span></small>
                        </div>
                        <div class="col-6">
                            <small>↑ <span id="network-upload">0 KB/s</span></small>
                        </div>
                    </div>
                    <div class="chart-container">
                        <canvas id="networkChart"></canvas>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="stat-card">
                    <h6 class="mb-3">
                        <i class="bi bi-thermometer-half"></i> Temperature
                    </h6>
                    <div id="temperature-stats" class="small"></div>
                </div>
            </div>
        </div>
        
        <!-- Additional Tabs for More Features -->
        <ul class="nav nav-tabs mt-3" id="systemTabs" role="tablist">
            <li class="nav-item" role="presentation">
                <button class="nav-link active" id="processes-tab" data-bs-toggle="tab" data-bs-target="#processes" type="button" role="tab">Processes</button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="files-tab" data-bs-toggle="tab" data-bs-target="#files" type="button" role="tab">Files</button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="system-info-tab" data-bs-toggle="tab" data-bs-target="#system-info" type="button" role="tab">System Info</button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="logs-tab" data-bs-toggle="tab" data-bs-target="#logs" type="button" role="tab">Logs</button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="resources-tab" data-bs-toggle="tab" data-bs-target="#resources" type="button" role="tab">Resources</button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="disk-tab" data-bs-toggle="tab" data-bs-target="#disk" type="button" role="tab">Disks</button>
            </li>
        </ul>
        <div class="tab-content" id="systemTabContent">
            <!-- Processes Tab -->
            <div class="tab-pane fade show active" id="processes" role="tabpanel">
                <div class="row mt-3">
                    <div class="col-md-6">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-gear-wide-connected"></i> Top CPU Processes
                            </h6>
                            <div class="process-detail" id="top-cpu-processes">
                                <table class="table table-sm process-table">
                                    <thead>
                                        <tr>
                                            <th>PID</th>
                                            <th>Name</th>
                                            <th>CPU</th>
                                        </tr>
                                    </thead>
                                    <tbody id="cpu-processes-list"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-6">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-database"></i> Top Memory Processes
                            </h6>
                            <div class="process-detail" id="top-memory-processes">
                                <table class="table table-sm process-table">
                                    <thead>
                                        <tr>
                                            <th>PID</th>
                                            <th>Name</th>
                                            <th>Memory</th>
                                        </tr>
                                    </thead>
                                    <tbody id="memory-processes-list"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="row mt-3">
                    <div class="col-12">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-list-task"></i> All Processes
                            </h6>
                            <div class="process-detail" id="all-processes">
                                <table class="table table-sm process-table">
                                    <thead>
                                        <tr>
                                            <th>PID</th>
                                            <th>Name</th>
                                            <th>CPU</th>
                                            <th>Memory</th>
                                            <th>User</th>
                                            <th></th>
                                        </tr>
                                    </thead>
                                    <tbody id="process-list"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Files Tab -->
            <div class="tab-pane fade" id="files" role="tabpanel">
                <div class="row mt-3">
                    <div class="col-md-6">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-folder2-open"></i> File Explorer
                            </h6>
                            <div class="d-flex justify-content-between align-items-center mb-2">
                                <div class="input-group input-group-sm" style="width: 100%;">
                                    <input type="text" class="form-control" id="current-path" value="/" style="width: 100%;">
                                    <button class="btn btn-outline-primary" type="button" onclick="updateFileList()">
                                        <i class="bi bi-folder2-open"></i>
                                    </button>
                                </div>
                            </div>
                            <div class="file-list" id="file-list-container">
                                <table class="table table-sm">
                                    <thead>
                                        <tr>
                                            <th>Name</th>
                                            <th>Size</th>
                                            <th></th>
                                        </tr>
                                    </thead>
                                    <tbody id="file-list"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-6">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-hdd-stack"></i> Disk Partitions
                            </h6>
                            <div class="process-detail" id="disk-partitions">
                                <table class="table table-sm">
                                    <thead>
                                        <tr>
                                            <th>Device</th>
                                            <th>Mountpoint</th>
                                            <th>Usage</th>
                                        </tr>
                                    </thead>
                                    <tbody id="disk-partitions-list"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- System Info Tab -->
            <div class="tab-pane fade" id="system-info" role="tabpanel">
                <div class="row mt-3">
                    <div class="col-md-6">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-info-circle"></i> System Information
                            </h6>
                            <div class="row">
                                <div class="col-12">
                                    <table class="table table-sm">
                                        <tbody id="system-info-table"></tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-6">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-network"></i> Network Interfaces
                            </h6>
                            <div class="process-detail" id="network-interfaces">
                                <table class="table table-sm">
                                    <thead>
                                        <tr>
                                            <th>Name</th>
                                            <th>IP Address</th>
                                        </tr>
                                    </thead>
                                    <tbody id="network-interfaces-list"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="row mt-3">
                    <div class="col-12">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-graph-up"></i> Performance History
                            </h6>
                            <div class="history-chart-container">
                                <canvas id="performanceChart"></canvas>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Logs Tab -->
            <div class="tab-pane fade" id="logs" role="tabpanel">
                <div class="row mt-3">
                    <div class="col-12">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-terminal"></i> System Log
                            </h6>
                            <div class="log-container" id="system-log"></div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Resources Tab -->
            <div class="tab-pane fade" id="resources" role="tabpanel">
                <div class="row mt-3">
                    <div class="col-md-6">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-memory"></i> Memory Breakdown
                            </h6>
                            <div id="memory-breakdown"></div>
                        </div>
                    </div>
                    <div class="col-md-6">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-cpu"></i> CPU Breakdown
                            </h6>
                            <div id="cpu-breakdown"></div>
                        </div>
                    </div>
                </div>
                <div class="row mt-3">
                    <div class="col-12">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-graph-up"></i> Resource Utilization
                            </h6>
                            <div class="history-chart-container">
                                <canvas id="resourceChart"></canvas>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Disk Tab -->
            <div class="tab-pane fade" id="disk" role="tabpanel">
                <div class="row mt-3">
                    <div class="col-12">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-hdd-stack"></i> Disk Usage Details
                            </h6>
                            <div id="disk-details-container"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Theme Toggle Button -->
    <button class="btn btn-primary theme-toggle" onclick="toggleTheme()">
        <i class="bi bi-moon-stars-fill" id="theme-icon"></i>
    </button>
    
    <!-- Delete Modal -->
    <div class="modal fade" id="deleteModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title">Confirm Delete</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <p>Are you sure you want to delete this item?</p>
                    <div class="form-check">
                        <input class="form-check-input" type="checkbox" id="permanentDelete">
                        <label class="form-check-label" for="permanentDelete">
                            Permanently delete
                        </label>
                    </div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-danger" id="confirmDelete">Delete</button>
                </div>
            </div>
        </div>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
        // Theme Management
        const currentTheme = localStorage.getItem('theme') || 'light';
        document.documentElement.setAttribute('data-theme', currentTheme);
        updateThemeIcon();
        function toggleTheme() {
            const theme = document.documentElement.getAttribute('data-theme');
            const newTheme = theme === 'light' ? 'dark' : 'light';
            document.documentElement.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            updateThemeIcon();
        }
        function updateThemeIcon() {
            const theme = document.documentElement.getAttribute('data-theme');
            const icon = document.getElementById('theme-icon');
            icon.className = theme === 'light' ? 'bi bi-moon-stars-fill' : 'bi bi-sun-fill';
        }
        
        // Global variables
        let deleteModal;
        let fileToDelete = null;
        let networkChart;
        let performanceChart;
        let resourceChart;
        document.addEventListener('DOMContentLoaded', function() {
            deleteModal = new bootstrap.Modal(document.getElementById('deleteModal'));
            initializeNetworkChart();
            initializePerformanceChart();
            initializeResourceChart();
            updateSystemInfo();
            updateFileList();
            updateProcesses();
            updateSystemLog();
            updateSystemInfoTab();
            updateResourcesTab();
            updateDiskTab();
            setInterval(updateSystemInfo, {{ refresh_interval }});
            setInterval(updateProcesses, 5000);
            setInterval(updateSystemLog, 2000);
            setInterval(updateCurrentTime, 1000);
            setInterval(updatePerformanceHistory, 10000);
            setInterval(updateResourcesTab, 10000);
            setInterval(updateDiskTab, 30000);
        });
        
        function updateCurrentTime() {
            document.getElementById('current-time').textContent = 
                new Date().toLocaleTimeString();
        }
        
        function initializeNetworkChart() {
            const ctx = document.getElementById('networkChart').getContext('2d');
            networkChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: Array.from({length: 30}, (_, i) => ''),
                    datasets: [{
                        label: 'Download',
                        data: Array(30).fill(0),
                        borderColor: 'rgb(75, 192, 192)',
                        backgroundColor: 'rgba(75, 192, 192, 0.2)',
                        tension: 0.1,
                        fill: true
                    }, {
                        label: 'Upload',
                        data: Array(30).fill(0),
                        borderColor: 'rgb(255, 99, 132)',
                        backgroundColor: 'rgba(255, 99, 132, 0.2)',
                        tension: 0.1,
                        fill: true
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: false
                        }
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            ticks: {
                                callback: function(value) {
                                    return value + ' KB/s';
                                }
                            }
                        }
                    }
                }
            });
        }
        
        function initializePerformanceChart() {
            const ctx = document.getElementById('performanceChart').getContext('2d');
            performanceChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: Array.from({length: 60}, (_, i) => ''),
                    datasets: [
                        {
                            label: 'CPU (%)',
                            data: Array(60).fill(0),
                            borderColor: 'rgb(54, 162, 235)',
                            backgroundColor: 'rgba(54, 162, 235, 0.2)',
                            tension: 0.1,
                            fill: true
                        },
                        {
                            label: 'Memory (%)',
                            data: Array(60).fill(0),
                            borderColor: 'rgb(255, 99, 132)',
                            backgroundColor: 'rgba(255, 99, 132, 0.2)',
                            tension: 0.1,
                            fill: true
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: true
                        }
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            max: 100,
                            ticks: {
                                callback: function(value) {
                                    return value + '%';
                                }
                            }
                        }
                    }
                }
            });
        }
        
        function initializeResourceChart() {
            const ctx = document.getElementById('resourceChart').getContext('2d');
            resourceChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: ['CPU', 'Memory', 'Disk'],
                    datasets: [{
                        label: 'Utilization (%)',
                        data: [0, 0, 0],
                        backgroundColor: [
                            'rgba(54, 162, 235, 0.7)',
                            'rgba(255, 99, 132, 0.7)',
                            'rgba(255, 159, 64, 0.7)'
                        ],
                        borderColor: [
                            'rgba(54, 162, 235, 1)',
                            'rgba(255, 99, 132, 1)',
                            'rgba(255, 159, 64, 1)'
                        ],
                        borderWidth: 1
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            display: false
                        }
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            max: 100,
                            ticks: {
                                callback: function(value) {
                                    return value + '%';
                                }
                            }
                        }
                    }
                }
            });
        }
        
        function updateSystemInfo() {
            fetch('/api/system_info')
                .then(response => response.json())
                .then(data => {
                    if (!data) return;
                    
                    // Update overview stats
                    document.getElementById('cpu-percent-display').textContent = data.cpu_percent.toFixed(1) + '%';
                    document.getElementById('memory-percent-display').textContent = data.memory.percent.toFixed(1) + '%';
                    document.getElementById('disk-percent-display').textContent = data.disk.percent.toFixed(1) + '%';
                    document.getElementById('network-download-display').textContent = humanizeSize(data.network.bytes_recv_per_sec) + '/s';
                    document.getElementById('network-upload-display').textContent = humanizeSize(data.network.bytes_sent_per_sec) + '/s';
                    document.getElementById('uptime-display').textContent = data.uptime;
                    
                    // Update detailed cards
                    // CPU
                    document.getElementById('cpu-bar').style.width = data.cpu_percent + '%';
                    document.getElementById('cpu-percent').textContent = data.cpu_percent.toFixed(1) + '%';
                    document.getElementById('cpu-cores').textContent = `${data.cpu_cores} cores @ ${data.cpu_freq}GHz`;
                    
                    // Memory
                    const memPercent = data.memory.percent;
                    document.getElementById('memory-bar').style.width = memPercent + '%';
                    document.getElementById('memory-percent').textContent = memPercent.toFixed(1) + '%';
                    document.getElementById('memory-details').textContent = 
                        `${humanizeSize(data.memory.used)} / ${humanizeSize(data.memory.total)}`;
                    
                    // Disk
                    const diskPercent = data.disk.percent;
                    document.getElementById('disk-bar').style.width = diskPercent + '%';
                    document.getElementById('disk-percent').textContent = diskPercent.toFixed(1) + '%';
                    document.getElementById('disk-details').textContent = 
                        `${humanizeSize(data.disk.used)} / ${humanizeSize(data.disk.total)}`;
                    
                    // Uptime
                    document.getElementById('uptime').textContent = data.uptime;
                    document.getElementById('boot-time').textContent = data.boot_time;
                    
                    // Network
                    document.getElementById('network-download').textContent = 
                        humanizeSize(data.network.bytes_recv_per_sec) + '/s';
                    document.getElementById('network-upload').textContent = 
                        humanizeSize(data.network.bytes_sent_per_sec) + '/s';
                    
                    // Update Network Chart
                    if (networkChart.data.labels.length > 30) {
                        networkChart.data.labels.shift();
                        networkChart.data.datasets[0].data.shift();
                        networkChart.data.datasets[1].data.shift();
                    }
                    networkChart.data.labels.push('');
                    networkChart.data.datasets[0].data.push(data.network.bytes_recv_per_sec / 1024);
                    networkChart.data.datasets[1].data.push(data.network.bytes_sent_per_sec / 1024);
                    networkChart.update('none');
                    
                    // Temperature
                    const tempContainer = document.getElementById('temperature-stats');
                    tempContainer.innerHTML = data.temperatures.map(temp => `
                        <div class="mb-2">
                            <small class="text-muted">${temp.label}</small>
                            <div class="progress" style="height: 8px;">
                                <div class="progress-bar ${temp.current > temp.high ? 'bg-danger' : 'bg-success'}" 
                                     style="width: ${(temp.current / temp.high * 100)}%">
                                    ${temp.current}°C
                                </div>
                            </div>
                        </div>
                    `).join('');
                    
                    // Alerts
                    const alertContainer = document.getElementById('system-alerts');
                    alertContainer.innerHTML = data.alerts.map(alert => `
                        <div class="alert alert-${alert.type} alert-dismissible fade show alert-custom" role="alert">
                            <span class="status-indicator status-${alert.type === 'danger' ? 'danger' : 'warning'}"></span>
                            ${alert.message}
                            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                        </div>
                    `).join('');
                    
                    // Update card colors based on usage
                    const cpuCard = document.getElementById('cpu-card');
                    const memoryCard = document.getElementById('memory-card');
                    const diskCard = document.getElementById('disk-card');
                    const uptimeCard = document.getElementById('uptime-card');
                    
                    cpuCard.className = 'stat-card resource-card';
                    memoryCard.className = 'stat-card resource-card';
                    diskCard.className = 'stat-card resource-card';
                    
                    if (data.cpu_percent > 80) {
                        cpuCard.classList.add('danger');
                    } else if (data.cpu_percent > 60) {
                        cpuCard.classList.add('warning');
                    } else {
                        cpuCard.classList.add('success');
                    }
                    
                    if (data.memory.percent > 80) {
                        memoryCard.classList.add('danger');
                    } else if (data.memory.percent > 60) {
                        memoryCard.classList.add('warning');
                    } else {
                        memoryCard.classList.add('success');
                    }
                    
                    if (data.disk.percent > 80) {
                        diskCard.classList.add('danger');
                    } else if (data.disk.percent > 60) {
                        diskCard.classList.add('warning');
                    } else {
                        diskCard.classList.add('success');
                    }
                })
                .catch(error => {
                    console.error('Error updating system info:', error);
                });
        }
        
        function updateProcesses() {
            Promise.all([
                fetch('/api/processes'),
                fetch('/api/top_processes/cpu'),
                fetch('/api/top_processes/memory')
            ])
            .then(responses => Promise.all(responses.map(r => r.json())))
            .then(([allProcesses, cpuProcesses, memoryProcesses]) => {
                // All processes
                const tbody = document.getElementById('process-list');
                tbody.innerHTML = allProcesses.map(process => `
                    <tr>
                        <td>${process.pid}</td>
                        <td>${process.name}</td>
                        <td>${process.cpu_percent.toFixed(1)}%</td>
                        <td>${process.memory_percent.toFixed(1)}%</td>
                        <td>${process.username || '-'}</td>
                        <td>
                            <button class="btn btn-sm btn-outline-danger" 
                                    onclick="killProcess(${process.pid})">
                                <i class="bi bi-x"></i>
                            </button>
                        </td>
                    </tr>
                `).join('');
                
                // Top CPU processes
                const cpuTbody = document.getElementById('cpu-processes-list');
                cpuTbody.innerHTML = cpuProcesses.map(process => `
                    <tr>
                        <td>${process.pid}</td>
                        <td>${process.name}</td>
                        <td>${process.cpu_percent.toFixed(1)}%</td>
                    </tr>
                `).join('');
                
                // Top memory processes
                const memoryTbody = document.getElementById('memory-processes-list');
                memoryTbody.innerHTML = memoryProcesses.map(process => `
                    <tr>
                        <td>${process.pid}</td>
                        <td>${process.name}</td>
                        <td>${process.memory_percent.toFixed(1)}%</td>
                    </tr>
                `).join('');
            })
            .catch(error => {
                console.error('Error updating processes:', error);
            });
        }
        
        function killProcess(pid) {
            if (confirm(`Kill process ${pid}?`)) {
                fetch('/api/kill_process', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({pid: pid})
                })
                .then(response => response.json())
                .then(data => {
                    if (data.status === 'success') {
                        updateProcesses();
                    }
                })
                .catch(error => {
                    console.error('Error killing process:', error);
                });
            }
        }
        
        function updateFileList() {
            const path = document.getElementById('current-path').value;
            fetch('/api/files?path=' + encodeURIComponent(path))
                .then(response => response.json())
                .then(files => {
                    const tbody = document.getElementById('file-list');
                    tbody.innerHTML = files.map(file => `
                        <tr>
                            <td>
                                <i class="bi bi-${file.type === 'directory' ? 'folder' : 'file'}"></i>
                                ${file.name}
                            </td>
                            <td>${file.size}</td>
                            <td>
                                ${file.type === 'directory' ? 
                                    `<button class="btn btn-sm btn-outline-primary" 
                                             onclick="navigateToDirectory('${file.path}')">
                                        <i class="bi bi-folder-open"></i>
                                    </button>` : ''}
                                <button class="btn btn-sm btn-outline-danger" 
                                        onclick="deleteFile('${file.path}')">
                                    <i class="bi bi-trash"></i>
                                </button>
                            </td>
                        </tr>
                    `).join('');
                })
                .catch(error => {
                    console.error('Error updating file list:', error);
                });
        }
        
        function navigateToDirectory(path) {
            document.getElementById('current-path').value = path;
            updateFileList();
        }
        
        function deleteFile(path) {
            fileToDelete = path;
            deleteModal.show();
        }
        
        document.getElementById('confirmDelete').addEventListener('click', function() {
            if (!fileToDelete) return;
            fetch('/api/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    path: fileToDelete,
                    permanent: document.getElementById('permanentDelete').checked
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    updateFileList();
                    deleteModal.hide();
                }
            })
            .catch(error => {
                console.error('Error deleting file:', error);
            });
            fileToDelete = null;
            document.getElementById('permanentDelete').checked = false;
        });
        
        function updateSystemLog() {
            fetch('/api/system_log')
                .then(response => response.json())
                .then(logs => {
                    const container = document.getElementById('system-log');
                    container.innerHTML = logs.map(log => `
                        <div class="log-entry ${log.level}">
                            <small class="text-muted">[${log.timestamp}]</small>
                            ${log.message}
                        </div>
                    `).join('');
                    container.scrollTop = container.scrollHeight;
                })
                .catch(error => {
                    console.error('Error updating system log:', error);
                });
        }
        
        function updateSystemInfoTab() {
            fetch('/api/system_info_extended')
                .then(response => response.json())
                .then(data => {
                    // System info table
                    const tableBody = document.getElementById('system-info-table');
                    tableBody.innerHTML = `
                        <tr><td>Username:</td><td>${data.user_info.username}</td></tr>
                        <tr><td>System:</td><td>${data.user_info.system} ${data.user_info.release}</td></tr>
                        <tr><td>Machine:</td><td>${data.user_info.machine}</td></tr>
                        <tr><td>Processor:</td><td>${data.user_info.processor}</td></tr>
                        <tr><td>Load Average:</td><td>${data.load_avg ? 
                            `${data.load_avg.one_min}, ${data.load_avg.five_min}, ${data.load_avg.fifteen_min}` : 'N/A'}</td></tr>
                    `;
                    // Network interfaces
                    const interfacesTbody = document.getElementById('network-interfaces-list');
                    interfacesTbody.innerHTML = data.network_interfaces.map(iface => `
                        <tr>
                            <td>${iface.name}</td>
                            <td>${iface.ip}</td>
                        </tr>
                    `).join('');
                    // Disk partitions
                    const partitionsTbody = document.getElementById('disk-partitions-list');
                    partitionsTbody.innerHTML = data.disk_partitions.map(partition => `
                        <tr>
                            <td>${partition.device}</td>
                            <td>${partition.mountpoint}</td>
                            <td>${partition.percent}%</td>
                        </tr>
                    `).join('');
                })
                .catch(error => {
                    console.error('Error updating system info tab:', error);
                });
        }
        
        function updatePerformanceHistory() {
            fetch('/api/performance_history')
                .then(response => response.json())
                .then(data => {
                    // Update performance chart
                    if (performanceChart.data.labels.length > 60) {
                        performanceChart.data.labels.shift();
                        performanceChart.data.datasets[0].data.shift();
                        performanceChart.data.datasets[1].data.shift();
                    }
                    performanceChart.data.labels.push('');
                    performanceChart.data.datasets[0].data.push(data.cpu_history.length > 0 ? 
                        data.cpu_history[data.cpu_history.length - 1] : 0);
                    performanceChart.data.datasets[1].data.push(data.memory_history.length > 0 ? 
                        data.memory_history[data.memory_history.length - 1] : 0);
                    performanceChart.update('none');
                })
                .catch(error => {
                    console.error('Error updating performance history:', error);
                });
        }
        
        function updateResourcesTab() {
            fetch('/api/resources')
                .then(response => response.json())
                .then(data => {
                    // Memory breakdown
                    const memoryBreakdown = document.getElementById('memory-breakdown');
                    if (data.system_info && data.system_info.memory) {
                        const mem = data.system_info.memory;
                        memoryBreakdown.innerHTML = `
                            <div class="memory-breakdown">
                                <span>Used: ${humanizeSize(mem.used)}</span>
                                <div class="memory-bar">
                                    <div class="memory-used" style="width: ${(mem.used / mem.total * 100)}%"></div>
                                </div>
                                <span>${Math.round((mem.used / mem.total) * 100)}%</span>
                            </div>
                            <div class="memory-breakdown">
                                <span>Free: ${humanizeSize(mem.available)}</span>
                                <div class="memory-bar">
                                    <div class="memory-free" style="width: ${(mem.available / mem.total * 100)}%"></div>
                                </div>
                                <span>${Math.round((mem.available / mem.total) * 100)}%</span>
                            </div>
                        `;
                    }
                    
                    // CPU breakdown
                    const cpuBreakdown = document.getElementById('cpu-breakdown');
                    if (data.system_info) {
                        const cpuPercent = data.system_info.cpu_percent;
                        cpuBreakdown.innerHTML = `
                            <div class="progress mb-2">
                                <div class="progress-bar bg-primary" style="width: ${cpuPercent}%"></div>
                            </div>
                            <div class="d-flex justify-content-between">
                                <span>Used: ${cpuPercent.toFixed(1)}%</span>
                                <span>Available: ${(100 - cpuPercent).toFixed(1)}%</span>
                            </div>
                        `;
                    }
                    
                    // Update resource chart
                    if (resourceChart) {
                        resourceChart.data.datasets[0].data = [
                            data.system_info?.cpu_percent || 0,
                            data.system_info?.memory?.percent || 0,
                            data.system_info?.disk?.percent || 0
                        ];
                        resourceChart.update();
                    }
                })
                .catch(error => {
                    console.error('Error updating resources tab:', error);
                });
        }
        
        function updateDiskTab() {
            fetch('/api/system_info_extended')
                .then(response => response.json())
                .then(data => {
                    const container = document.getElementById('disk-details-container');
                    if (data.disk_partitions && data.disk_partitions.length > 0) {
                        container.innerHTML = data.disk_partitions.map(partition => `
                            <div class="disk-usage-container">
                                <span>${partition.mountpoint}</span>
                                <div class="disk-usage-bar">
                                    <div class="disk-usage" style="width: ${partition.percent}%"></div>
                                </div>
                                <span>${partition.percent}%</span>
                            </div>
                        `).join('');
                    } else {
                        container.innerHTML = '<p>No disk partitions found.</p>';
                    }
                })
                .catch(error => {
                    console.error('Error updating disk tab:', error);
                });
        }
        
        function humanizeSize(bytes) {
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            let size = bytes;
            let unitIndex = 0;
            while (size >= 1024 && unitIndex < units.length - 1) {
                size /= 1024;
                unitIndex++;
            }
            return size.toFixed(2) + ' ' + units[unitIndex];
        }
    </script>
</body>
</html>
"""

# Routes
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, refresh_interval=REFRESH_INTERVAL)

@app.route('/api/system_info')
def system_info():
    return jsonify(get_system_info())

@app.route('/api/system_info_extended')
def system_info_extended():
    """Extended system information API endpoint"""
    return jsonify({
        'user_info': get_user_info(),
        'load_avg': get_system_load_avg(),
        'network_interfaces': get_network_interfaces(),
        'disk_partitions': get_disk_partitions()
    })

@app.route('/api/files')
def list_files():
    path = request.args.get('path', '/')
    return jsonify(get_file_list(path))

@app.route('/api/processes')
def list_processes():
    return jsonify(get_process_list())

@app.route('/api/top_processes/<category>')
def top_processes(category):
    """Get top processes by category"""
    if category == 'cpu':
        return jsonify(get_top_processes_by_cpu())
    elif category == 'memory':
        return jsonify(get_top_processes_by_memory())
    else:
        return jsonify([])

@app.route('/api/kill_process', methods=['POST'])
def kill_process():
    pid = request.json.get('pid')
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        log_system_event('info', f'Terminated process {pid}')
        return jsonify({'status': 'success'})
    except psutil.NoSuchProcess:
        log_system_event('error', f'Process {pid} not found')
        return jsonify({'status': 'error', 'message': 'Process not found'})
    except psutil.AccessDenied:
        log_system_event('error', f'Access denied when trying to kill process {pid}')
        return jsonify({'status': 'error', 'message': 'Access denied'})
    except Exception as e:
        log_system_event('error', f'Error killing process {pid}: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/delete', methods=['POST'])
def delete_file():
    file_path = request.json.get('path')
    permanent = request.json.get('permanent', False)
    try:
        if permanent:
            if os.path.isdir(file_path):
                shutil.rmtree(file_path)
            else:
                os.remove(file_path)
            log_system_event('warning', f'Permanently deleted: {file_path}')
        else:
            # Use send2trash if available
            try:
                from send2trash import send2trash
                send2trash(file_path)
            except ImportError:
                # Fallback to os.remove if send2trash is not available
                os.remove(file_path)
            log_system_event('info', f'Moved to trash: {file_path}')
        return jsonify({'status': 'success'})
    except Exception as e:
        log_system_event('error', f'Failed to delete {file_path}: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/system_log')
def get_system_log():
    return jsonify(SYSTEM_LOG[-50:])

@app.route('/api/performance_history')
def get_performance_history():
    return jsonify(get_system_performance_data())

@app.route('/api/resources')
def get_resources():
    return jsonify(get_system_resources())

# Graceful shutdown handler
def signal_handler(sig, frame):
    log_system_event('info', 'Shutting down system monitor...')
    system_state['is_running'] = False
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == '__main__':
    log_system_event('info', 'Enhanced System Monitor Dashboard started')
    app.run(debug=False, host='0.0.0.0', port=5000)
