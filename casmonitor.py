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
from werkzeug.utils import secure_filename
import pty
import select
import struct
import fcntl
import termios

app = Flask(__name__)

# Terminal session management
terminal_sessions = {}
UPLOAD_FOLDER = os.getcwd()
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'py', 'js', 'html', 'css', 'json', 'md', 'cpp', 'c', 'h', 'sh', 'yaml', 'yml', 'xml', 'csv', 'log', 'conf', 'cfg', 'ini', 'sql', 'zip', 'tar', 'gz'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max upload

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS or '.' not in filename

# Configuration
REFRESH_INTERVAL = 1000  # milliseconds - faster updates for real-time monitoring
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
                is_dir = os.path.isdir(full_path)
                files.append({
                    'name': item,
                    'path': full_path,
                    'type': 'directory' if is_dir else 'file',
                    'size': humanize.naturalsize(stat.st_size) if not is_dir else '-',
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                    'permissions': oct(stat.st_mode)[-3:],
                    'extension': os.path.splitext(item)[1].lower() if not is_dir else ''
                })
            except (PermissionError, FileNotFoundError):
                continue
        # Sort: directories first, then by name (case-insensitive)
        return sorted(files, key=lambda x: (x['type'] != 'directory', x['name'].lower()))[:100]
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
        /* Terminal styles */
        .terminal-output {
            scrollbar-width: thin;
            scrollbar-color: #4a5568 #1e1e1e;
        }
        .terminal-output::-webkit-scrollbar {
            width: 8px;
        }
        .terminal-output::-webkit-scrollbar-track {
            background: #1e1e1e;
        }
        .terminal-output::-webkit-scrollbar-thumb {
            background-color: #4a5568;
            border-radius: 4px;
        }
        .terminal-command {
            color: #4ec9b0;
        }
        .terminal-output-text {
            color: #d4d4d4;
        }
        .terminal-error {
            color: #f14c4c;
        }
        .terminal-success {
            color: #89d185;
        }
        .terminal-info {
            color: #3794ff;
        }
        /* Upload section styles */
        .upload-section {
            background: var(--card-bg);
            border-color: var(--border-color) !important;
        }
        .upload-section:hover {
            border-color: var(--accent-color) !important;
        }
        #file-editor {
            background: var(--card-bg);
            color: var(--text-color);
            border-color: var(--border-color);
        }
        [data-theme="dark"] #file-editor {
            background: #1e1e1e;
            color: #d4d4d4;
        }
        [data-theme="dark"] #terminal-input {
            background: #1e1e1e !important;
            color: #d4d4d4 !important;
        }
        .file-action-btn {
            padding: 2px 6px;
            font-size: 11px;
        }
        /* Blinking animation for running status */
        @keyframes blink {
            0%, 50% { opacity: 1; }
            51%, 100% { opacity: 0.5; }
        }
        .blink {
            animation: blink 1s infinite;
        }
        /* Running indicator */
        .terminal-running {
            border: 2px solid #ffc107 !important;
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
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="terminal-tab" data-bs-toggle="tab" data-bs-target="#terminal" type="button" role="tab">Terminal</button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link" id="docker-tab" data-bs-toggle="tab" data-bs-target="#docker" type="button" role="tab">
                    <i class="bi bi-box-seam"></i> Docker
                </button>
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
                                    <input type="text" class="form-control" id="current-path" value="{{ cwd }}" style="width: 100%;">
                                    <button class="btn btn-outline-primary" type="button" onclick="updateFileList()">
                                        <i class="bi bi-folder2-open"></i>
                                    </button>
                                    <button class="btn btn-outline-secondary" type="button" onclick="goUpDirectory()">
                                        <i class="bi bi-arrow-up"></i>
                                    </button>
                                    <button class="btn btn-outline-info" type="button" onclick="goToHome()">
                                        <i class="bi bi-house"></i>
                                    </button>
                                </div>
                            </div>
                            <!-- File Upload Section -->
                            <div class="upload-section mb-3 p-2 border rounded">
                                <h6 class="mb-2"><i class="bi bi-cloud-upload"></i> Upload Files</h6>
                                <form id="upload-form" enctype="multipart/form-data">
                                    <div class="input-group input-group-sm">
                                        <input type="file" class="form-control" id="file-upload" multiple>
                                        <button class="btn btn-success" type="button" onclick="uploadFiles()">
                                            <i class="bi bi-upload"></i> Upload
                                        </button>
                                    </div>
                                </form>
                                <div id="upload-progress" class="mt-2" style="display: none;">
                                    <div class="progress" style="height: 20px;">
                                        <div class="progress-bar progress-bar-striped progress-bar-animated" id="upload-progress-bar" style="width: 0%">0%</div>
                                    </div>
                                </div>
                                <div id="upload-status" class="mt-2 small"></div>
                                <div class="mt-2 d-flex gap-2">
                                    <button class="btn btn-sm btn-outline-primary" onclick="createNewFile()">
                                        <i class="bi bi-file-plus"></i> New File
                                    </button>
                                    <button class="btn btn-sm btn-outline-secondary" onclick="createNewFolder()">
                                        <i class="bi bi-folder-plus"></i> New Folder
                                    </button>
                                </div>
                            </div>
                            <div class="file-list" id="file-list-container" style="max-height: 400px;">
                                <table class="table table-sm">
                                    <thead>
                                        <tr>
                                            <th>Name</th>
                                            <th>Size</th>
                                            <th>Modified</th>
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
                                <i class="bi bi-pencil-square"></i> File Editor
                            </h6>
                            <div class="mb-2">
                                <div class="input-group input-group-sm">
                                    <span class="input-group-text">File:</span>
                                    <input type="text" class="form-control" id="edit-file-path" placeholder="Select a file to edit" readonly>
                                    <button class="btn btn-outline-primary" type="button" onclick="loadFileContent()">
                                        <i class="bi bi-arrow-clockwise"></i> Reload
                                    </button>
                                </div>
                            </div>
                            <textarea id="file-editor" class="form-control" rows="18" style="font-family: monospace; font-size: 12px; resize: vertical;" placeholder="Select a file to edit its content..."></textarea>
                            <div class="mt-2 d-flex justify-content-between">
                                <small class="text-muted" id="editor-status">No file loaded</small>
                                <div>
                                    <button class="btn btn-sm btn-secondary" onclick="clearEditor()">
                                        <i class="bi bi-x-circle"></i> Clear
                                    </button>
                                    <button class="btn btn-sm btn-primary" onclick="saveFileContent()">
                                        <i class="bi bi-save"></i> Save
                                    </button>
                                    <button class="btn btn-sm btn-success" onclick="saveFileAs()">
                                        <i class="bi bi-save2"></i> Save As
                                    </button>
                                </div>
                            </div>
                        </div>
                        <div class="stat-card mt-3">
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
            
            <!-- Terminal Tab -->
            <div class="tab-pane fade" id="terminal" role="tabpanel">
                <div class="row mt-3">
                    <div class="col-12">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-terminal"></i> Interactive Terminal
                                <span class="badge bg-success ms-2" id="terminal-status">Ready</span>
                            </h6>
                            <div class="mb-2 d-flex justify-content-between align-items-center">
                                <div class="input-group input-group-sm" style="width: 300px;">
                                    <span class="input-group-text">Shell:</span>
                                    <select class="form-select" id="shell-select">
                                        <option value="auto">Auto Detect</option>
                                        <option value="/bin/bash">Bash</option>
                                        <option value="/bin/zsh">Zsh</option>
                                        <option value="/bin/sh">Sh</option>
                                        <option value="cmd.exe">CMD (Windows)</option>
                                        <option value="powershell.exe">PowerShell</option>
                                    </select>
                                </div>
                                <div>
                                    <button class="btn btn-sm btn-outline-danger" id="stop-btn" onclick="stopCommand()" style="display: none;">
                                        <i class="bi bi-stop-fill"></i> Stop
                                    </button>
                                    <button class="btn btn-sm btn-outline-secondary" onclick="clearTerminal()">
                                        <i class="bi bi-trash"></i> Clear
                                    </button>
                                    <button class="btn btn-sm btn-outline-warning" onclick="resetTerminal()">
                                        <i class="bi bi-arrow-clockwise"></i> Reset
                                    </button>
                                </div>
                            </div>
                            <div id="terminal-container" class="terminal-output" style="background: #1e1e1e; color: #d4d4d4; font-family: 'Courier New', monospace; font-size: 13px; padding: 10px; border-radius: 5px; height: 400px; overflow-y: auto; white-space: pre-wrap; word-wrap: break-word;"></div>
                            <div class="input-group mt-2">
                                <span class="input-group-text" id="terminal-prompt" style="background: #1e1e1e; color: #4ec9b0; border-color: #3c3c3c; font-family: monospace;">$</span>
                                <input type="text" class="form-control" id="terminal-input" placeholder="Type command and press Enter..." style="background: #1e1e1e; color: #d4d4d4; border-color: #3c3c3c; font-family: monospace;" autocomplete="off">
                                <button class="btn btn-primary" type="button" id="run-btn" onclick="executeCommand()">
                                    <i class="bi bi-play-fill"></i> Run
                                </button>
                            </div>
                            <div class="mt-2">
                                <small class="text-muted">
                                    <i class="bi bi-info-circle"></i> 
                                    Press Enter to execute | Up/Down arrows for history | Ctrl+C to interrupt
                                </small>
                            </div>
                            <div class="mt-2">
                                <span class="badge bg-secondary me-1" onclick="quickCommand('ls -la')" style="cursor: pointer;">ls -la</span>
                                <span class="badge bg-secondary me-1" onclick="quickCommand('pwd')" style="cursor: pointer;">pwd</span>
                                <span class="badge bg-secondary me-1" onclick="quickCommand('whoami')" style="cursor: pointer;">whoami</span>
                                <span class="badge bg-secondary me-1" onclick="quickCommand('df -h')" style="cursor: pointer;">df -h</span>
                                <span class="badge bg-secondary me-1" onclick="quickCommand('top -l 1 | head -20')" style="cursor: pointer;">top</span>
                                <span class="badge bg-secondary me-1" onclick="quickCommand('ps aux | head -20')" style="cursor: pointer;">ps aux</span>
                                <span class="badge bg-secondary me-1" onclick="quickCommand('netstat -an | head -20')" style="cursor: pointer;">netstat</span>
                                <span class="badge bg-secondary me-1" onclick="quickCommand('uname -a')" style="cursor: pointer;">uname</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Docker Tab -->
            <div class="tab-pane fade" id="docker" role="tabpanel">
                <div class="row mt-3">
                    <!-- Docker Status -->
                    <div class="col-12 mb-3">
                        <div class="stat-card">
                            <div class="d-flex justify-content-between align-items-center">
                                <h6 class="mb-0">
                                    <i class="bi bi-box-seam"></i> Docker Status
                                    <span class="badge bg-secondary ms-2" id="docker-status">Checking...</span>
                                </h6>
                                <div>
                                    <button class="btn btn-sm btn-outline-primary" onclick="refreshDocker()">
                                        <i class="bi bi-arrow-clockwise"></i> Refresh
                                    </button>
                                    <button class="btn btn-sm btn-success" onclick="showCreateContainerModal()">
                                        <i class="bi bi-plus-circle"></i> Create Container
                                    </button>
                                    <button class="btn btn-sm btn-info" onclick="showPullImageModal()">
                                        <i class="bi bi-cloud-download"></i> Pull Image
                                    </button>
                                </div>
                            </div>
                            <div class="row mt-3" id="docker-stats-row">
                                <div class="col-md-3">
                                    <div class="text-center">
                                        <h4 id="docker-containers-total">0</h4>
                                        <small class="text-muted">Total Containers</small>
                                    </div>
                                </div>
                                <div class="col-md-3">
                                    <div class="text-center">
                                        <h4 id="docker-containers-running" class="text-success">0</h4>
                                        <small class="text-muted">Running</small>
                                    </div>
                                </div>
                                <div class="col-md-3">
                                    <div class="text-center">
                                        <h4 id="docker-containers-stopped" class="text-warning">0</h4>
                                        <small class="text-muted">Stopped</small>
                                    </div>
                                </div>
                                <div class="col-md-3">
                                    <div class="text-center">
                                        <h4 id="docker-images-total">0</h4>
                                        <small class="text-muted">Images</small>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Containers List -->
                    <div class="col-md-8">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-box"></i> Containers
                            </h6>
                            <div class="table-responsive" style="max-height: 400px; overflow-y: auto;">
                                <table class="table table-sm table-hover">
                                    <thead class="sticky-top bg-body">
                                        <tr>
                                            <th>Name</th>
                                            <th>Image</th>
                                            <th>Status</th>
                                            <th>Ports</th>
                                            <th>CPU</th>
                                            <th>Memory</th>
                                            <th>Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody id="docker-containers-list">
                                        <tr><td colspan="7" class="text-center text-muted">Loading...</td></tr>
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Images List -->
                    <div class="col-md-4">
                        <div class="stat-card">
                            <h6 class="mb-3">
                                <i class="bi bi-layers"></i> Images
                            </h6>
                            <div class="table-responsive" style="max-height: 400px; overflow-y: auto;">
                                <table class="table table-sm table-hover">
                                    <thead class="sticky-top bg-body">
                                        <tr>
                                            <th>Repository</th>
                                            <th>Tag</th>
                                            <th>Size</th>
                                            <th></th>
                                        </tr>
                                    </thead>
                                    <tbody id="docker-images-list">
                                        <tr><td colspan="4" class="text-center text-muted">Loading...</td></tr>
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
                
                <!-- Container Logs -->
                <div class="row mt-3">
                    <div class="col-12">
                        <div class="stat-card">
                            <div class="d-flex justify-content-between align-items-center mb-2">
                                <h6 class="mb-0">
                                    <i class="bi bi-terminal"></i> Container Logs
                                    <span class="badge bg-secondary ms-2" id="logs-container-name">Select a container</span>
                                </h6>
                                <div>
                                    <select class="form-select form-select-sm d-inline-block" style="width: auto;" id="logs-lines-select">
                                        <option value="50">Last 50 lines</option>
                                        <option value="100" selected>Last 100 lines</option>
                                        <option value="500">Last 500 lines</option>
                                        <option value="1000">Last 1000 lines</option>
                                    </select>
                                    <button class="btn btn-sm btn-outline-secondary" onclick="clearContainerLogs()">
                                        <i class="bi bi-trash"></i> Clear
                                    </button>
                                </div>
                            </div>
                            <div id="container-logs" class="terminal-output" style="background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 12px; padding: 10px; border-radius: 5px; height: 250px; overflow-y: auto; white-space: pre-wrap;">
                                Select a container to view logs...
                            </div>
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
    
    <!-- Create Container Modal -->
    <div class="modal fade" id="createContainerModal" tabindex="-1">
        <div class="modal-dialog modal-lg">
            <div class="modal-content">
                <div class="modal-header bg-success text-white">
                    <h5 class="modal-title"><i class="bi bi-plus-circle"></i> Create Container</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <form id="create-container-form">
                        <div class="row">
                            <div class="col-md-6 mb-3">
                                <label class="form-label">Container Name</label>
                                <input type="text" class="form-control" id="container-name" placeholder="my-container">
                            </div>
                            <div class="col-md-6 mb-3">
                                <label class="form-label">Image *</label>
                                <input type="text" class="form-control" id="container-image" placeholder="nginx:latest" required>
                            </div>
                        </div>
                        <div class="row">
                            <div class="col-md-6 mb-3">
                                <label class="form-label">Port Mapping</label>
                                <input type="text" class="form-control" id="container-ports" placeholder="8080:80, 443:443">
                                <small class="text-muted">Format: host:container (comma separated)</small>
                            </div>
                            <div class="col-md-6 mb-3">
                                <label class="form-label">Volume Mapping</label>
                                <input type="text" class="form-control" id="container-volumes" placeholder="/host/path:/container/path">
                                <small class="text-muted">Format: host:container (comma separated)</small>
                            </div>
                        </div>
                        <div class="row">
                            <div class="col-md-12 mb-3">
                                <label class="form-label">Environment Variables</label>
                                <textarea class="form-control" id="container-env" rows="2" placeholder="KEY1=value1&#10;KEY2=value2"></textarea>
                                <small class="text-muted">One per line: KEY=value</small>
                            </div>
                        </div>
                        <div class="row">
                            <div class="col-md-6 mb-3">
                                <label class="form-label">Command (optional)</label>
                                <input type="text" class="form-control" id="container-command" placeholder="/bin/bash">
                            </div>
                            <div class="col-md-6 mb-3">
                                <label class="form-label">Network</label>
                                <select class="form-select" id="container-network">
                                    <option value="">Default (bridge)</option>
                                    <option value="host">Host</option>
                                    <option value="none">None</option>
                                </select>
                            </div>
                        </div>
                        <div class="row">
                            <div class="col-md-6 mb-3">
                                <label class="form-label">Restart Policy</label>
                                <select class="form-select" id="container-restart">
                                    <option value="">No</option>
                                    <option value="always">Always</option>
                                    <option value="unless-stopped">Unless Stopped</option>
                                    <option value="on-failure">On Failure</option>
                                </select>
                            </div>
                            <div class="col-md-6 mb-3">
                                <label class="form-label">Options</label>
                                <div class="form-check">
                                    <input class="form-check-input" type="checkbox" id="container-detach" checked>
                                    <label class="form-check-label" for="container-detach">Run in background (detached)</label>
                                </div>
                                <div class="form-check">
                                    <input class="form-check-input" type="checkbox" id="container-rm">
                                    <label class="form-check-label" for="container-rm">Remove when stopped</label>
                                </div>
                            </div>
                        </div>
                    </form>
                    <div id="create-container-output" class="mt-3" style="display: none;">
                        <div class="alert" id="create-container-alert"></div>
                    </div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-success" onclick="createContainer()">
                        <i class="bi bi-play-fill"></i> Create & Start
                    </button>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Pull Image Modal -->
    <div class="modal fade" id="pullImageModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header bg-info text-white">
                    <h5 class="modal-title"><i class="bi bi-cloud-download"></i> Pull Image</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="mb-3">
                        <label class="form-label">Image Name</label>
                        <input type="text" class="form-control" id="pull-image-name" placeholder="nginx:latest">
                        <small class="text-muted">Examples: ubuntu, nginx:alpine, mysql:8.0</small>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Popular Images</label>
                        <div class="d-flex flex-wrap gap-1">
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('nginx:latest')">nginx</span>
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('ubuntu:latest')">ubuntu</span>
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('alpine:latest')">alpine</span>
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('mysql:8.0')">mysql</span>
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('postgres:latest')">postgres</span>
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('redis:latest')">redis</span>
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('mongo:latest')">mongo</span>
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('node:lts')">node</span>
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('python:3.11')">python</span>
                            <span class="badge bg-secondary" style="cursor: pointer;" onclick="setPullImage('httpd:latest')">httpd</span>
                        </div>
                    </div>
                    <div id="pull-image-progress" style="display: none;">
                        <div class="progress mb-2">
                            <div class="progress-bar progress-bar-striped progress-bar-animated" id="pull-progress-bar" style="width: 0%"></div>
                        </div>
                        <div id="pull-image-status" class="small text-muted"></div>
                    </div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-info" id="pull-image-btn" onclick="pullImage()">
                        <i class="bi bi-cloud-download"></i> Pull
                    </button>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Container Exec Modal -->
    <div class="modal fade" id="execModal" tabindex="-1">
        <div class="modal-dialog modal-lg">
            <div class="modal-content">
                <div class="modal-header bg-dark text-white">
                    <h5 class="modal-title"><i class="bi bi-terminal"></i> Execute in Container: <span id="exec-container-name"></span></h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="mb-3">
                        <label class="form-label">Command</label>
                        <div class="input-group">
                            <input type="text" class="form-control" id="exec-command" placeholder="ls -la" value="sh">
                            <button class="btn btn-primary" onclick="execInContainer()">
                                <i class="bi bi-play-fill"></i> Execute
                            </button>
                        </div>
                        <small class="text-muted">Common: sh, bash, ls -la, cat /etc/os-release</small>
                    </div>
                    <div id="exec-output" class="terminal-output" style="background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 12px; padding: 10px; border-radius: 5px; height: 300px; overflow-y: auto; white-space: pre-wrap;">
                        Output will appear here...
                    </div>
                </div>
            </div>
        </div>
    </div>
    
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
            setInterval(updateSystemInfo, {{ refresh_interval }});  // 1 second
            setInterval(updateProcesses, 2000);  // 2 seconds - more frequent process updates
            setInterval(updateSystemLog, 1000);  // 1 second - real-time logs
            setInterval(updateCurrentTime, 1000);  // 1 second
            setInterval(updatePerformanceHistory, 3000);  // 3 seconds - faster chart updates
            setInterval(updateResourcesTab, 3000);  // 3 seconds
            setInterval(updateDiskTab, 10000);  // 10 seconds - disk doesn't change often
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
        
        // ==================== TERMINAL FUNCTIONS ====================
        let commandHistory = [];
        let historyIndex = -1;
        let currentWorkingDir = '{{ cwd }}';
        
        // Initialize terminal input handlers
        document.addEventListener('DOMContentLoaded', function() {
            const terminalInput = document.getElementById('terminal-input');
            if (terminalInput) {
                terminalInput.addEventListener('keydown', function(e) {
                    if (e.key === 'Enter') {
                        executeCommand();
                    } else if (e.key === 'ArrowUp') {
                        e.preventDefault();
                        navigateHistory(-1);
                    } else if (e.key === 'ArrowDown') {
                        e.preventDefault();
                        navigateHistory(1);
                    } else if (e.key === 'c' && e.ctrlKey) {
                        e.preventDefault();
                        if (isCommandRunning) {
                            appendToTerminal('^C - Attempting to cancel...', 'terminal-error');
                            // Note: Server-side cancellation would need additional implementation
                            setTerminalLocked(false);
                        } else {
                            terminalInput.value = '';
                        }
                    }
                });
            }
            // Initialize with welcome message
            appendToTerminal('Welcome to CasMonitor Terminal', 'terminal-info');
            appendToTerminal('Type commands below and press Enter to execute.', 'terminal-info');
            appendToTerminal('-------------------------------------------', 'terminal-info');
            updateTerminalPrompt();
        });
        
        let isCommandRunning = false;
        let currentEventSource = null;
        
        function executeCommand() {
            const input = document.getElementById('terminal-input');
            const command = input.value.trim();
            if (!command) return;
            
            // Block if command is already running
            if (isCommandRunning) {
                appendToTerminal('⚠ Please wait for the current command to finish...', 'terminal-error');
                return;
            }
            
            // Add to history
            commandHistory.push(command);
            historyIndex = commandHistory.length;
            
            // Display command
            const prompt = document.getElementById('terminal-prompt').textContent;
            appendToTerminal(prompt + ' ' + command, 'terminal-command');
            
            // Clear input and disable
            input.value = '';
            setTerminalLocked(true);
            
            // Get selected shell
            const shellSelect = document.getElementById('shell-select');
            const shell = shellSelect.value;
            
            // Check if command needs streaming (long-running commands)
            const streamingCommands = ['apt', 'brew', 'npm', 'pip', 'yarn', 'git', 'wget', 'curl', 'make', 'cmake', 'cargo', 'go', 'docker', 'kubectl', 'terraform', 'ansible'];
            const needsStreaming = streamingCommands.some(cmd => command.trim().startsWith(cmd + ' ') || command.trim() === cmd);
            
            if (needsStreaming) {
                executeStreamingCommand(command, shell);
            } else {
                executeNormalCommand(command, shell);
            }
        }
        
        function setTerminalLocked(locked) {
            isCommandRunning = locked;
            const input = document.getElementById('terminal-input');
            const runBtn = document.getElementById('run-btn');
            const stopBtn = document.getElementById('stop-btn');
            
            if (locked) {
                input.disabled = true;
                input.placeholder = 'Command running... please wait';
                input.style.opacity = '0.6';
                if (runBtn) runBtn.disabled = true;
                if (stopBtn) stopBtn.style.display = 'inline-block';
                document.getElementById('terminal-status').textContent = 'Running...';
                document.getElementById('terminal-status').className = 'badge bg-warning ms-2 blink';
            } else {
                input.disabled = false;
                input.placeholder = 'Type command and press Enter...';
                input.style.opacity = '1';
                if (runBtn) runBtn.disabled = false;
                if (stopBtn) stopBtn.style.display = 'none';
                document.getElementById('terminal-status').textContent = 'Ready';
                document.getElementById('terminal-status').className = 'badge bg-success ms-2';
                input.focus();
            }
        }
        
        function stopCommand() {
            if (isCommandRunning) {
                appendToTerminal('^C - Command cancelled by user', 'terminal-error');
                setTerminalLocked(false);
            }
        }
        
        function executeNormalCommand(command, shell) {
            fetch('/api/terminal/execute', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    command: command,
                    cwd: currentWorkingDir,
                    shell: shell
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.stdout) {
                    // Split output into lines for better display
                    data.stdout.split('\\n').forEach(line => {
                        if (line) appendToTerminal(line, 'terminal-output-text');
                    });
                }
                if (data.stderr) {
                    data.stderr.split('\\n').forEach(line => {
                        if (line) appendToTerminal(line, 'terminal-error');
                    });
                }
                if (data.cwd) {
                    currentWorkingDir = data.cwd;
                    updateTerminalPrompt();
                }
                if (data.error) {
                    appendToTerminal('Error: ' + data.error, 'terminal-error');
                }
                setTerminalLocked(false);
            })
            .catch(error => {
                appendToTerminal('Error: ' + error.message, 'terminal-error');
                setTerminalLocked(false);
            });
        }
        
        function executeStreamingCommand(command, shell) {
            // Use EventSource for streaming output
            const params = new URLSearchParams({
                command: command,
                cwd: currentWorkingDir,
                shell: shell
            });
            
            // Use fetch with streaming for POST request
            fetch('/api/terminal/stream', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    command: command,
                    cwd: currentWorkingDir,
                    shell: shell
                })
            })
            .then(response => {
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                
                function read() {
                    reader.read().then(({done, value}) => {
                        if (done) {
                            setTerminalLocked(false);
                            return;
                        }
                        
                        const text = decoder.decode(value);
                        const lines = text.split('\\n');
                        
                        lines.forEach(line => {
                            if (line.startsWith('data: ')) {
                                try {
                                    const data = JSON.parse(line.slice(6));
                                    if (data.output) {
                                        appendToTerminal(data.output.replace(/\\n$/, ''), 'terminal-output-text');
                                    }
                                    if (data.error) {
                                        appendToTerminal('Error: ' + data.error, 'terminal-error');
                                    }
                                    if (data.cwd) {
                                        currentWorkingDir = data.cwd;
                                        updateTerminalPrompt();
                                    }
                                    if (data.complete) {
                                        if (data.returncode === 0) {
                                            appendToTerminal('✓ Command completed successfully', 'terminal-success');
                                        } else if (data.returncode) {
                                            appendToTerminal('✗ Command exited with code ' + data.returncode, 'terminal-error');
                                        }
                                        setTerminalLocked(false);
                                    }
                                } catch (e) {}
                            }
                        });
                        
                        read();
                    });
                }
                
                read();
            })
            .catch(error => {
                appendToTerminal('Error: ' + error.message, 'terminal-error');
                setTerminalLocked(false);
            });
        }
        
        function appendToTerminal(text, className) {
            const container = document.getElementById('terminal-container');
            const line = document.createElement('div');
            line.className = className || '';
            line.textContent = text;
            container.appendChild(line);
            container.scrollTop = container.scrollHeight;
        }
        
        function clearTerminal() {
            document.getElementById('terminal-container').innerHTML = '';
            appendToTerminal('Terminal cleared.', 'terminal-info');
        }
        
        function resetTerminal() {
            currentWorkingDir = '/';
            commandHistory = [];
            historyIndex = -1;
            clearTerminal();
            appendToTerminal('Terminal reset. Working directory: /', 'terminal-info');
            updateTerminalPrompt();
        }
        
        function updateTerminalPrompt() {
            const prompt = document.getElementById('terminal-prompt');
            const shortPath = currentWorkingDir.length > 30 ? 
                '...' + currentWorkingDir.slice(-27) : currentWorkingDir;
            prompt.textContent = shortPath + ' $';
        }
        
        function navigateHistory(direction) {
            if (commandHistory.length === 0) return;
            
            historyIndex += direction;
            if (historyIndex < 0) historyIndex = 0;
            if (historyIndex >= commandHistory.length) {
                historyIndex = commandHistory.length;
                document.getElementById('terminal-input').value = '';
                return;
            }
            document.getElementById('terminal-input').value = commandHistory[historyIndex];
        }
        
        function quickCommand(cmd) {
            document.getElementById('terminal-input').value = cmd;
            executeCommand();
        }
        
        // ==================== FILE UPLOAD FUNCTIONS ====================
        function uploadFiles() {
            const fileInput = document.getElementById('file-upload');
            const files = fileInput.files;
            if (files.length === 0) {
                document.getElementById('upload-status').innerHTML = 
                    '<span class="text-warning">Please select files to upload.</span>';
                return;
            }
            
            const uploadPath = document.getElementById('current-path').value;
            const formData = new FormData();
            for (let i = 0; i < files.length; i++) {
                formData.append('files', files[i]);
            }
            formData.append('path', uploadPath);
            
            // Show progress
            document.getElementById('upload-progress').style.display = 'block';
            document.getElementById('upload-status').innerHTML = 
                '<span class="text-info">Uploading...</span>';
            
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/upload', true);
            
            xhr.upload.onprogress = function(e) {
                if (e.lengthComputable) {
                    const percent = Math.round((e.loaded / e.total) * 100);
                    document.getElementById('upload-progress-bar').style.width = percent + '%';
                    document.getElementById('upload-progress-bar').textContent = percent + '%';
                }
            };
            
            xhr.onload = function() {
                if (xhr.status === 200) {
                    const response = JSON.parse(xhr.responseText);
                    if (response.status === 'success') {
                        document.getElementById('upload-status').innerHTML = 
                            '<span class="text-success"><i class="bi bi-check-circle"></i> ' + 
                            response.uploaded.length + ' file(s) uploaded successfully!</span>';
                        updateFileList();
                    } else {
                        document.getElementById('upload-status').innerHTML = 
                            '<span class="text-danger"><i class="bi bi-x-circle"></i> ' + 
                            response.message + '</span>';
                    }
                } else {
                    document.getElementById('upload-status').innerHTML = 
                        '<span class="text-danger">Upload failed!</span>';
                }
                setTimeout(() => {
                    document.getElementById('upload-progress').style.display = 'none';
                    document.getElementById('upload-progress-bar').style.width = '0%';
                }, 2000);
            };
            
            xhr.onerror = function() {
                document.getElementById('upload-status').innerHTML = 
                    '<span class="text-danger">Upload error!</span>';
            };
            
            xhr.send(formData);
            fileInput.value = '';
        }
        
        // ==================== FILE EDITOR FUNCTIONS ====================
        let currentEditFile = null;
        let originalContent = '';
        
        function editFile(filePath) {
            currentEditFile = filePath;
            document.getElementById('edit-file-path').value = filePath;
            loadFileContent();
        }
        
        function loadFileContent() {
            const filePath = document.getElementById('edit-file-path').value;
            if (!filePath) {
                document.getElementById('editor-status').textContent = 'No file selected';
                return;
            }
            
            document.getElementById('editor-status').textContent = 'Loading...';
            
            fetch('/api/file/read?path=' + encodeURIComponent(filePath))
                .then(response => response.json())
                .then(data => {
                    if (data.status === 'success') {
                        document.getElementById('file-editor').value = data.content;
                        originalContent = data.content;
                        currentEditFile = filePath;
                        const lines = data.content.split('\\n').length;
                        const size = new Blob([data.content]).size;
                        document.getElementById('editor-status').textContent = 
                            `Loaded: ${lines} lines, ${humanizeSize(size)}`;
                    } else {
                        document.getElementById('editor-status').textContent = 'Error: ' + data.message;
                        document.getElementById('file-editor').value = '';
                    }
                })
                .catch(error => {
                    document.getElementById('editor-status').textContent = 'Error loading file';
                    console.error('Error:', error);
                });
        }
        
        function saveFileContent() {
            const filePath = document.getElementById('edit-file-path').value;
            const content = document.getElementById('file-editor').value;
            
            if (!filePath) {
                alert('No file selected. Use "Save As" to create a new file.');
                return;
            }
            
            document.getElementById('editor-status').textContent = 'Saving...';
            
            fetch('/api/file/write', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    path: filePath,
                    content: content
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    originalContent = content;
                    document.getElementById('editor-status').textContent = 
                        'Saved successfully at ' + new Date().toLocaleTimeString();
                    updateFileList();
                } else {
                    document.getElementById('editor-status').textContent = 'Error: ' + data.message;
                }
            })
            .catch(error => {
                document.getElementById('editor-status').textContent = 'Error saving file';
                console.error('Error:', error);
            });
        }
        
        function saveFileAs() {
            const content = document.getElementById('file-editor').value;
            const currentPath = document.getElementById('current-path').value;
            const fileName = prompt('Enter file name:', 'newfile.txt');
            
            if (!fileName) return;
            
            const fullPath = currentPath.endsWith('/') ? 
                currentPath + fileName : currentPath + '/' + fileName;
            
            fetch('/api/file/write', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    path: fullPath,
                    content: content
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    document.getElementById('edit-file-path').value = fullPath;
                    currentEditFile = fullPath;
                    originalContent = content;
                    document.getElementById('editor-status').textContent = 
                        'Saved as ' + fileName + ' at ' + new Date().toLocaleTimeString();
                    updateFileList();
                } else {
                    alert('Error: ' + data.message);
                }
            })
            .catch(error => {
                alert('Error saving file');
                console.error('Error:', error);
            });
        }
        
        function clearEditor() {
            if (document.getElementById('file-editor').value !== originalContent) {
                if (!confirm('You have unsaved changes. Clear anyway?')) return;
            }
            document.getElementById('file-editor').value = '';
            document.getElementById('edit-file-path').value = '';
            currentEditFile = null;
            originalContent = '';
            document.getElementById('editor-status').textContent = 'Editor cleared';
        }
        
        function goUpDirectory() {
            const currentPath = document.getElementById('current-path').value;
            const parentPath = currentPath.split('/').slice(0, -1).join('/') || '/';
            document.getElementById('current-path').value = parentPath;
            updateFileList();
        }
        
        function goToHome() {
            fetch('/api/home_dir')
                .then(response => response.json())
                .then(data => {
                    document.getElementById('current-path').value = data.home;
                    updateFileList();
                });
        }
        
        // Override updateFileList to add edit button
        const originalUpdateFileList = updateFileList;
        updateFileList = function() {
            const path = document.getElementById('current-path').value;
            fetch('/api/files?path=' + encodeURIComponent(path))
                .then(response => response.json())
                .then(files => {
                    const tbody = document.getElementById('file-list');
                    tbody.innerHTML = files.map(file => `
                        <tr>
                            <td>
                                <i class="bi bi-${file.type === 'directory' ? 'folder-fill text-warning' : 'file-text text-secondary'}"></i>
                                <span style="cursor: pointer;" onclick="${file.type === 'directory' ? `navigateToDirectory('${file.path.replace(/'/g, "\\'")}')` : `editFile('${file.path.replace(/'/g, "\\'")}')`}">
                                    ${file.name}
                                </span>
                            </td>
                            <td>${file.size}</td>
                            <td><small class="text-muted">${file.modified}</small></td>
                            <td>
                                ${file.type === 'directory' ? 
                                    `<button class="btn btn-sm btn-outline-primary file-action-btn" 
                                             onclick="navigateToDirectory('${file.path.replace(/'/g, "\\'")}')">
                                        <i class="bi bi-folder-symlink"></i>
                                    </button>` : 
                                    `<button class="btn btn-sm btn-outline-info file-action-btn" 
                                             onclick="editFile('${file.path.replace(/'/g, "\\'")}')">
                                        <i class="bi bi-pencil"></i>
                                    </button>
                                    <button class="btn btn-sm btn-outline-warning file-action-btn" 
                                             onclick="renameFile('${file.path.replace(/'/g, "\\'")}')">
                                        <i class="bi bi-pencil-square"></i>
                                    </button>
                                    <button class="btn btn-sm btn-outline-success file-action-btn" 
                                             onclick="downloadFile('${file.path.replace(/'/g, "\\'")}')">
                                        <i class="bi bi-download"></i>
                                    </button>`}
                                <button class="btn btn-sm btn-outline-danger file-action-btn" 
                                        onclick="deleteFile('${file.path.replace(/'/g, "\\'")}')">
                                    <i class="bi bi-trash"></i>
                                </button>
                            </td>
                        </tr>
                    `).join('');
                })
                .catch(error => {
                    console.error('Error updating file list:', error);
                });
        };
        
        function downloadFile(filePath) {
            window.open('/api/file/download?path=' + encodeURIComponent(filePath), '_blank');
        }
        
        function createNewFile() {
            const currentPath = document.getElementById('current-path').value;
            const fileName = prompt('Enter new file name:', 'newfile.txt');
            if (!fileName) return;
            
            const fullPath = currentPath.endsWith('/') ? 
                currentPath + fileName : currentPath + '/' + fileName;
            
            fetch('/api/file/write', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    path: fullPath,
                    content: ''
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    updateFileList();
                    editFile(fullPath);
                } else {
                    alert('Error: ' + data.message);
                }
            });
        }
        
        function createNewFolder() {
            const currentPath = document.getElementById('current-path').value;
            const folderName = prompt('Enter new folder name:', 'newfolder');
            if (!folderName) return;
            
            const fullPath = currentPath.endsWith('/') ? 
                currentPath + folderName : currentPath + '/' + folderName;
            
            fetch('/api/folder/create', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: fullPath})
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    updateFileList();
                } else {
                    alert('Error: ' + data.message);
                }
            });
        }
        
        function renameFile(filePath) {
            const oldName = filePath.split('/').pop();
            const newName = prompt('Enter new name:', oldName);
            if (!newName || newName === oldName) return;
            
            const newPath = filePath.replace(oldName, newName);
            
            fetch('/api/file/rename', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    oldPath: filePath,
                    newPath: newPath
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    updateFileList();
                } else {
                    alert('Error: ' + data.message);
                }
            });
        }
        
        // ==================== DOCKER FUNCTIONS ====================
        let createContainerModal, pullImageModal, execModal;
        let currentExecContainer = null;
        let selectedLogContainer = null;
        
        document.addEventListener('DOMContentLoaded', function() {
            createContainerModal = new bootstrap.Modal(document.getElementById('createContainerModal'));
            pullImageModal = new bootstrap.Modal(document.getElementById('pullImageModal'));
            execModal = new bootstrap.Modal(document.getElementById('execModal'));
            
            // Initial Docker check
            checkDockerStatus();
            
            // Refresh Docker data every 5 seconds
            setInterval(refreshDocker, 5000);
        });
        
        function checkDockerStatus() {
            fetch('/api/docker/status')
                .then(response => response.json())
                .then(data => {
                    const statusBadge = document.getElementById('docker-status');
                    if (data.available) {
                        statusBadge.textContent = 'Running';
                        statusBadge.className = 'badge bg-success ms-2';
                        document.getElementById('docker-stats-row').style.display = 'flex';
                        refreshDocker();
                    } else {
                        statusBadge.textContent = 'Not Available';
                        statusBadge.className = 'badge bg-danger ms-2';
                        document.getElementById('docker-stats-row').style.display = 'none';
                        document.getElementById('docker-containers-list').innerHTML = 
                            '<tr><td colspan="7" class="text-center text-danger">Docker is not running or not installed</td></tr>';
                        document.getElementById('docker-images-list').innerHTML = 
                            '<tr><td colspan="4" class="text-center text-danger">Docker is not available</td></tr>';
                    }
                })
                .catch(error => {
                    document.getElementById('docker-status').textContent = 'Error';
                    document.getElementById('docker-status').className = 'badge bg-danger ms-2';
                });
        }
        
        function refreshDocker() {
            // Get containers
            fetch('/api/docker/containers')
                .then(response => response.json())
                .then(data => {
                    if (data.error) {
                        document.getElementById('docker-containers-list').innerHTML = 
                            '<tr><td colspan="7" class="text-center text-danger">' + data.error + '</td></tr>';
                        return;
                    }
                    
                    const containers = data.containers || [];
                    const running = containers.filter(c => c.status.startsWith('Up')).length;
                    const stopped = containers.length - running;
                    
                    document.getElementById('docker-containers-total').textContent = containers.length;
                    document.getElementById('docker-containers-running').textContent = running;
                    document.getElementById('docker-containers-stopped').textContent = stopped;
                    
                    if (containers.length === 0) {
                        document.getElementById('docker-containers-list').innerHTML = 
                            '<tr><td colspan="7" class="text-center text-muted">No containers found</td></tr>';
                    } else {
                        document.getElementById('docker-containers-list').innerHTML = containers.map(c => {
                            const isRunning = c.status.startsWith('Up');
                            const statusClass = isRunning ? 'text-success' : 'text-warning';
                            const statusIcon = isRunning ? 'play-circle-fill' : 'stop-circle-fill';
                            return `
                                <tr>
                                    <td><strong>${c.name}</strong><br><small class="text-muted">${c.id.substring(0, 12)}</small></td>
                                    <td><small>${c.image}</small></td>
                                    <td><span class="${statusClass}"><i class="bi bi-${statusIcon}"></i> ${c.status}</span></td>
                                    <td><small>${c.ports || '-'}</small></td>
                                    <td>${c.cpu || '-'}</td>
                                    <td>${c.memory || '-'}</td>
                                    <td>
                                        ${isRunning ? 
                                            `<button class="btn btn-sm btn-outline-warning" onclick="stopContainer('${c.id}')" title="Stop">
                                                <i class="bi bi-stop-fill"></i>
                                            </button>
                                            <button class="btn btn-sm btn-outline-danger" onclick="restartContainer('${c.id}')" title="Restart">
                                                <i class="bi bi-arrow-clockwise"></i>
                                            </button>` :
                                            `<button class="btn btn-sm btn-outline-success" onclick="startContainer('${c.id}')" title="Start">
                                                <i class="bi bi-play-fill"></i>
                                            </button>`
                                        }
                                        <button class="btn btn-sm btn-outline-info" onclick="viewContainerLogs('${c.id}', '${c.name}')" title="Logs">
                                            <i class="bi bi-file-text"></i>
                                        </button>
                                        <button class="btn btn-sm btn-outline-dark" onclick="showExecModal('${c.id}', '${c.name}')" title="Exec">
                                            <i class="bi bi-terminal"></i>
                                        </button>
                                        <button class="btn btn-sm btn-outline-danger" onclick="removeContainer('${c.id}', '${c.name}')" title="Remove">
                                            <i class="bi bi-trash"></i>
                                        </button>
                                    </td>
                                </tr>
                            `;
                        }).join('');
                    }
                });
            
            // Get images
            fetch('/api/docker/images')
                .then(response => response.json())
                .then(data => {
                    if (data.error) {
                        document.getElementById('docker-images-list').innerHTML = 
                            '<tr><td colspan="4" class="text-center text-danger">' + data.error + '</td></tr>';
                        return;
                    }
                    
                    const images = data.images || [];
                    document.getElementById('docker-images-total').textContent = images.length;
                    
                    if (images.length === 0) {
                        document.getElementById('docker-images-list').innerHTML = 
                            '<tr><td colspan="4" class="text-center text-muted">No images found</td></tr>';
                    } else {
                        document.getElementById('docker-images-list').innerHTML = images.map(img => `
                            <tr>
                                <td><small>${img.repository}</small></td>
                                <td><span class="badge bg-secondary">${img.tag}</span></td>
                                <td><small>${img.size}</small></td>
                                <td>
                                    <button class="btn btn-sm btn-outline-danger" onclick="removeImage('${img.id}')" title="Remove">
                                        <i class="bi bi-trash"></i>
                                    </button>
                                </td>
                            </tr>
                        `).join('');
                    }
                });
        }
        
        function startContainer(containerId) {
            fetch('/api/docker/container/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({container_id: containerId})
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    refreshDocker();
                } else {
                    alert('Error: ' + data.message);
                }
            });
        }
        
        function stopContainer(containerId) {
            fetch('/api/docker/container/stop', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({container_id: containerId})
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    refreshDocker();
                } else {
                    alert('Error: ' + data.message);
                }
            });
        }
        
        function restartContainer(containerId) {
            fetch('/api/docker/container/restart', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({container_id: containerId})
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    refreshDocker();
                } else {
                    alert('Error: ' + data.message);
                }
            });
        }
        
        function removeContainer(containerId, containerName) {
            if (!confirm(`Remove container "${containerName}"? This cannot be undone.`)) return;
            
            fetch('/api/docker/container/remove', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({container_id: containerId, force: true})
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    refreshDocker();
                } else {
                    alert('Error: ' + data.message);
                }
            });
        }
        
        function removeImage(imageId) {
            if (!confirm('Remove this image? This cannot be undone.')) return;
            
            fetch('/api/docker/image/remove', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({image_id: imageId})
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    refreshDocker();
                } else {
                    alert('Error: ' + data.message);
                }
            });
        }
        
        function viewContainerLogs(containerId, containerName) {
            selectedLogContainer = containerId;
            document.getElementById('logs-container-name').textContent = containerName;
            
            const lines = document.getElementById('logs-lines-select').value;
            
            fetch(`/api/docker/container/logs?container_id=${containerId}&lines=${lines}`)
                .then(response => response.json())
                .then(data => {
                    if (data.logs) {
                        document.getElementById('container-logs').textContent = data.logs || 'No logs available';
                        document.getElementById('container-logs').scrollTop = 
                            document.getElementById('container-logs').scrollHeight;
                    } else {
                        document.getElementById('container-logs').textContent = 'Error: ' + (data.error || 'Unknown error');
                    }
                });
        }
        
        function clearContainerLogs() {
            document.getElementById('container-logs').textContent = 'Select a container to view logs...';
            document.getElementById('logs-container-name').textContent = 'Select a container';
            selectedLogContainer = null;
        }
        
        function showCreateContainerModal() {
            document.getElementById('create-container-form').reset();
            document.getElementById('create-container-output').style.display = 'none';
            createContainerModal.show();
        }
        
        function createContainer() {
            const image = document.getElementById('container-image').value;
            if (!image) {
                alert('Please enter an image name');
                return;
            }
            
            const config = {
                image: image,
                name: document.getElementById('container-name').value || null,
                ports: document.getElementById('container-ports').value || null,
                volumes: document.getElementById('container-volumes').value || null,
                env: document.getElementById('container-env').value || null,
                command: document.getElementById('container-command').value || null,
                network: document.getElementById('container-network').value || null,
                restart: document.getElementById('container-restart').value || null,
                detach: document.getElementById('container-detach').checked,
                rm: document.getElementById('container-rm').checked
            };
            
            const outputDiv = document.getElementById('create-container-output');
            const alertDiv = document.getElementById('create-container-alert');
            outputDiv.style.display = 'block';
            alertDiv.className = 'alert alert-info';
            alertDiv.textContent = 'Creating container...';
            
            fetch('/api/docker/container/create', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(config)
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    alertDiv.className = 'alert alert-success';
                    alertDiv.textContent = 'Container created successfully! ID: ' + (data.container_id || '').substring(0, 12);
                    refreshDocker();
                    setTimeout(() => createContainerModal.hide(), 2000);
                } else {
                    alertDiv.className = 'alert alert-danger';
                    alertDiv.textContent = 'Error: ' + data.message;
                }
            })
            .catch(error => {
                alertDiv.className = 'alert alert-danger';
                alertDiv.textContent = 'Error: ' + error.message;
            });
        }
        
        function showPullImageModal() {
            document.getElementById('pull-image-name').value = '';
            document.getElementById('pull-image-progress').style.display = 'none';
            document.getElementById('pull-image-btn').disabled = false;
            pullImageModal.show();
        }
        
        function setPullImage(imageName) {
            document.getElementById('pull-image-name').value = imageName;
        }
        
        function pullImage() {
            const imageName = document.getElementById('pull-image-name').value;
            if (!imageName) {
                alert('Please enter an image name');
                return;
            }
            
            document.getElementById('pull-image-progress').style.display = 'block';
            document.getElementById('pull-image-btn').disabled = true;
            document.getElementById('pull-image-status').textContent = 'Pulling image...';
            document.getElementById('pull-progress-bar').style.width = '50%';
            
            fetch('/api/docker/image/pull', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({image: imageName})
            })
            .then(response => response.json())
            .then(data => {
                document.getElementById('pull-progress-bar').style.width = '100%';
                if (data.status === 'success') {
                    document.getElementById('pull-image-status').textContent = 'Image pulled successfully!';
                    document.getElementById('pull-progress-bar').className = 'progress-bar bg-success';
                    refreshDocker();
                    setTimeout(() => pullImageModal.hide(), 2000);
                } else {
                    document.getElementById('pull-image-status').textContent = 'Error: ' + data.message;
                    document.getElementById('pull-progress-bar').className = 'progress-bar bg-danger';
                }
                document.getElementById('pull-image-btn').disabled = false;
            })
            .catch(error => {
                document.getElementById('pull-image-status').textContent = 'Error: ' + error.message;
                document.getElementById('pull-progress-bar').className = 'progress-bar bg-danger';
                document.getElementById('pull-image-btn').disabled = false;
            });
        }
        
        function showExecModal(containerId, containerName) {
            currentExecContainer = containerId;
            document.getElementById('exec-container-name').textContent = containerName;
            document.getElementById('exec-output').textContent = 'Output will appear here...';
            document.getElementById('exec-command').value = 'sh';
            execModal.show();
        }
        
        function execInContainer() {
            if (!currentExecContainer) return;
            
            const command = document.getElementById('exec-command').value;
            if (!command) {
                alert('Please enter a command');
                return;
            }
            
            document.getElementById('exec-output').textContent = 'Executing...';
            
            fetch('/api/docker/container/exec', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    container_id: currentExecContainer,
                    command: command
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.output !== undefined) {
                    document.getElementById('exec-output').textContent = data.output || '(no output)';
                } else {
                    document.getElementById('exec-output').textContent = 'Error: ' + (data.error || data.message || 'Unknown error');
                }
            })
            .catch(error => {
                document.getElementById('exec-output').textContent = 'Error: ' + error.message;
            });
        }
        
        // Auto-refresh logs if a container is selected
        setInterval(() => {
            if (selectedLogContainer) {
                viewContainerLogs(selectedLogContainer, document.getElementById('logs-container-name').textContent);
            }
        }, 5000);
    </script>
</body>
</html>
"""

# Routes
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, refresh_interval=REFRESH_INTERVAL, cwd=os.getcwd())

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

# ==================== TERMINAL API ====================
# Store running processes for streaming
running_processes = {}

@app.route('/api/terminal/execute', methods=['POST'])
def terminal_execute():
    """Execute a terminal command and return output"""
    try:
        data = request.json
        command = data.get('command', '')
        cwd = data.get('cwd', os.getcwd())
        shell_type = data.get('shell', 'auto')
        
        if not command:
            return jsonify({'error': 'No command provided'})
        
        # Validate and set working directory
        if not os.path.isdir(cwd):
            cwd = os.getcwd()
        
        # Handle cd command specially
        if command.strip().startswith('cd '):
            new_dir = command.strip()[3:].strip()
            if new_dir == '~':
                new_dir = os.path.expanduser('~')
            elif new_dir == '-':
                new_dir = cwd
            elif not os.path.isabs(new_dir):
                new_dir = os.path.join(cwd, new_dir)
            
            new_dir = os.path.normpath(new_dir)
            
            if os.path.isdir(new_dir):
                return jsonify({
                    'stdout': '',
                    'stderr': '',
                    'cwd': new_dir,
                    'returncode': 0,
                    'complete': True
                })
            else:
                return jsonify({
                    'stdout': '',
                    'stderr': f'cd: no such file or directory: {new_dir}',
                    'cwd': cwd,
                    'returncode': 1,
                    'complete': True
                })
        
        # Determine shell
        if shell_type == 'auto':
            if platform.system() == 'Windows':
                shell_cmd = ['cmd.exe', '/c']
            else:
                shell_cmd = ['/bin/bash', '-c']
        elif shell_type in ['cmd.exe', 'powershell.exe']:
            if shell_type == 'cmd.exe':
                shell_cmd = ['cmd.exe', '/c']
            else:
                shell_cmd = ['powershell.exe', '-Command']
        else:
            shell_cmd = [shell_type, '-c']
        
        # Execute command
        try:
            result = subprocess.run(
                shell_cmd + [command],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
                env={**os.environ, 'TERM': 'xterm-256color'}
            )
            
            return jsonify({
                'stdout': result.stdout,
                'stderr': result.stderr,
                'cwd': cwd,
                'returncode': result.returncode,
                'complete': True
            })
        except subprocess.TimeoutExpired:
            return jsonify({
                'stdout': '',
                'stderr': 'Command timed out after 5 minutes',
                'cwd': cwd,
                'returncode': -1,
                'complete': True
            })
        except FileNotFoundError as e:
            return jsonify({
                'stdout': '',
                'stderr': f'Command not found: {str(e)}',
                'cwd': cwd,
                'returncode': 127,
                'complete': True
            })
            
    except Exception as e:
        log_system_event('error', f'Terminal execution error: {str(e)}')
        return jsonify({'error': str(e), 'complete': True})

@app.route('/api/terminal/stream', methods=['POST'])
def terminal_stream():
    """Execute a terminal command with streaming output"""
    from flask import Response, stream_with_context
    
    data = request.json
    command = data.get('command', '')
    cwd = data.get('cwd', os.getcwd())
    shell_type = data.get('shell', 'auto')
    
    if not command:
        return jsonify({'error': 'No command provided'})
    
    if not os.path.isdir(cwd):
        cwd = os.getcwd()
    
    # Handle cd command specially
    if command.strip().startswith('cd '):
        new_dir = command.strip()[3:].strip()
        if new_dir == '~':
            new_dir = os.path.expanduser('~')
        elif new_dir == '-':
            new_dir = cwd
        elif not os.path.isabs(new_dir):
            new_dir = os.path.join(cwd, new_dir)
        new_dir = os.path.normpath(new_dir)
        if os.path.isdir(new_dir):
            return jsonify({'cwd': new_dir, 'complete': True})
        else:
            return jsonify({'stderr': f'cd: no such file or directory: {new_dir}', 'cwd': cwd, 'complete': True})
    
    # Determine shell
    if shell_type == 'auto':
        shell = '/bin/bash' if platform.system() != 'Windows' else 'cmd.exe'
    else:
        shell = shell_type
    
    def generate():
        try:
            if platform.system() == 'Windows':
                process = subprocess.Popen(
                    ['cmd.exe', '/c', command],
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env={**os.environ}
                )
            else:
                process = subprocess.Popen(
                    [shell, '-c', command],
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env={**os.environ, 'TERM': 'xterm-256color'}
                )
            
            # Stream output line by line
            for line in iter(process.stdout.readline, ''):
                if line:
                    yield f"data: {json.dumps({'output': line})}\n\n"
            
            process.wait()
            yield f"data: {json.dumps({'complete': True, 'returncode': process.returncode, 'cwd': cwd})}\n\n"
            
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e), 'complete': True})}\n\n"
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )

# ==================== FILE UPLOAD API ====================
@app.route('/api/upload', methods=['POST'])
def upload_files():
    """Handle file uploads"""
    try:
        if 'files' not in request.files:
            return jsonify({'status': 'error', 'message': 'No files provided'})
        
        files = request.files.getlist('files')
        upload_path = request.form.get('path', os.getcwd())
        
        # Validate upload path
        if not os.path.isdir(upload_path):
            upload_path = os.getcwd()
        
        uploaded = []
        errors = []
        
        for file in files:
            if file.filename == '':
                continue
            
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                # If secure_filename returns empty, use original with basic sanitization
                if not filename:
                    filename = file.filename.replace('/', '_').replace('\\', '_')
                
                filepath = os.path.join(upload_path, filename)
                
                # Handle duplicate filenames
                base, ext = os.path.splitext(filename)
                counter = 1
                while os.path.exists(filepath):
                    filename = f"{base}_{counter}{ext}"
                    filepath = os.path.join(upload_path, filename)
                    counter += 1
                
                file.save(filepath)
                uploaded.append(filename)
                log_system_event('info', f'Uploaded file: {filepath}')
            else:
                errors.append(f'{file.filename} - file type not allowed')
        
        if uploaded:
            return jsonify({
                'status': 'success',
                'uploaded': uploaded,
                'errors': errors
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'No files were uploaded',
                'errors': errors
            })
            
    except Exception as e:
        log_system_event('error', f'Upload error: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})

# ==================== FILE READ/WRITE API ====================
@app.route('/api/file/read')
def read_file():
    """Read file content"""
    try:
        file_path = request.args.get('path', '')
        
        if not file_path:
            return jsonify({'status': 'error', 'message': 'No file path provided'})
        
        if not os.path.isfile(file_path):
            return jsonify({'status': 'error', 'message': 'File not found'})
        
        # Check file size (limit to 10MB for reading)
        file_size = os.path.getsize(file_path)
        if file_size > 10 * 1024 * 1024:
            return jsonify({'status': 'error', 'message': 'File too large (max 10MB)'})
        
        # Try to read as text
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            # Try with latin-1 encoding
            try:
                with open(file_path, 'r', encoding='latin-1') as f:
                    content = f.read()
            except:
                return jsonify({'status': 'error', 'message': 'Cannot read binary file'})
        
        return jsonify({
            'status': 'success',
            'content': content,
            'size': file_size,
            'path': file_path
        })
        
    except PermissionError:
        return jsonify({'status': 'error', 'message': 'Permission denied'})
    except Exception as e:
        log_system_event('error', f'File read error: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/file/write', methods=['POST'])
def write_file():
    """Write content to file"""
    try:
        data = request.json
        file_path = data.get('path', '')
        content = data.get('content', '')
        
        if not file_path:
            return jsonify({'status': 'error', 'message': 'No file path provided'})
        
        # Create directory if it doesn't exist
        dir_path = os.path.dirname(file_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        log_system_event('info', f'File saved: {file_path}')
        
        return jsonify({
            'status': 'success',
            'path': file_path,
            'size': len(content)
        })
        
    except PermissionError:
        return jsonify({'status': 'error', 'message': 'Permission denied'})
    except Exception as e:
        log_system_event('error', f'File write error: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/home_dir')
def get_home_dir():
    """Get home directory path"""
    return jsonify({'home': os.path.expanduser('~')})

@app.route('/api/folder/create', methods=['POST'])
def create_folder():
    """Create a new folder"""
    try:
        data = request.json
        folder_path = data.get('path', '')
        
        if not folder_path:
            return jsonify({'status': 'error', 'message': 'No folder path provided'})
        
        os.makedirs(folder_path, exist_ok=True)
        log_system_event('info', f'Created folder: {folder_path}')
        
        return jsonify({'status': 'success', 'path': folder_path})
        
    except PermissionError:
        return jsonify({'status': 'error', 'message': 'Permission denied'})
    except Exception as e:
        log_system_event('error', f'Folder creation error: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/file/rename', methods=['POST'])
def rename_file():
    """Rename a file or folder"""
    try:
        data = request.json
        old_path = data.get('oldPath', '')
        new_path = data.get('newPath', '')
        
        if not old_path or not new_path:
            return jsonify({'status': 'error', 'message': 'Invalid paths provided'})
        
        if not os.path.exists(old_path):
            return jsonify({'status': 'error', 'message': 'Source file not found'})
        
        if os.path.exists(new_path):
            return jsonify({'status': 'error', 'message': 'Destination already exists'})
        
        os.rename(old_path, new_path)
        log_system_event('info', f'Renamed: {old_path} -> {new_path}')
        
        return jsonify({'status': 'success', 'oldPath': old_path, 'newPath': new_path})
        
    except PermissionError:
        return jsonify({'status': 'error', 'message': 'Permission denied'})
    except Exception as e:
        log_system_event('error', f'Rename error: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/file/download')
def download_file():
    """Download a file"""
    try:
        from flask import send_file
        file_path = request.args.get('path', '')
        
        if not file_path:
            return jsonify({'status': 'error', 'message': 'No file path provided'})
        
        if not os.path.isfile(file_path):
            return jsonify({'status': 'error', 'message': 'File not found'})
        
        return send_file(
            file_path,
            as_attachment=True,
            download_name=os.path.basename(file_path)
        )
        
    except Exception as e:
        log_system_event('error', f'File download error: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})

# ==================== DOCKER API ====================
def run_docker_command(args, timeout=30):
    """Run a docker command and return output - cross-platform"""
    try:
        # Determine docker command based on platform
        docker_cmd = 'docker'
        if platform.system() == 'Windows':
            # Check for Docker Desktop on Windows
            docker_paths = [
                'docker',
                r'C:\Program Files\Docker\Docker\resources\bin\docker.exe',
                r'C:\ProgramData\DockerDesktop\version-bin\docker.exe'
            ]
            for path in docker_paths:
                try:
                    result = subprocess.run([path, '--version'], capture_output=True, timeout=5)
                    if result.returncode == 0:
                        docker_cmd = path
                        break
                except:
                    continue
        
        result = subprocess.run(
            [docker_cmd] + args,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return {
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode
        }
    except subprocess.TimeoutExpired:
        return {'stdout': '', 'stderr': 'Command timed out', 'returncode': -1}
    except FileNotFoundError:
        return {'stdout': '', 'stderr': 'Docker not found', 'returncode': -1}
    except Exception as e:
        return {'stdout': '', 'stderr': str(e), 'returncode': -1}

@app.route('/api/docker/status')
def docker_status():
    """Check if Docker is available and running"""
    try:
        result = run_docker_command(['info'], timeout=10)
        if result['returncode'] == 0:
            return jsonify({'available': True, 'info': result['stdout']})
        else:
            return jsonify({'available': False, 'error': result['stderr']})
    except Exception as e:
        return jsonify({'available': False, 'error': str(e)})

@app.route('/api/docker/containers')
def docker_containers():
    """List all Docker containers with stats"""
    try:
        # Get all containers
        result = run_docker_command([
            'ps', '-a', '--format', 
            '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}'
        ])
        
        if result['returncode'] != 0:
            return jsonify({'error': result['stderr'] or 'Failed to list containers'})
        
        containers = []
        lines = result['stdout'].strip().split('\n') if result['stdout'].strip() else []
        
        for line in lines:
            if not line:
                continue
            parts = line.split('|')
            if len(parts) >= 5:
                container = {
                    'id': parts[0],
                    'name': parts[1],
                    'image': parts[2],
                    'status': parts[3],
                    'ports': parts[4] if parts[4] else '-',
                    'cpu': '-',
                    'memory': '-'
                }
                containers.append(container)
        
        # Get stats for running containers
        if containers:
            stats_result = run_docker_command([
                'stats', '--no-stream', '--format',
                '{{.Container}}|{{.CPUPerc}}|{{.MemUsage}}'
            ], timeout=15)
            
            if stats_result['returncode'] == 0 and stats_result['stdout']:
                stats_lines = stats_result['stdout'].strip().split('\n')
                stats_map = {}
                for stat_line in stats_lines:
                    stat_parts = stat_line.split('|')
                    if len(stat_parts) >= 3:
                        stats_map[stat_parts[0]] = {
                            'cpu': stat_parts[1],
                            'memory': stat_parts[2]
                        }
                
                for container in containers:
                    if container['id'] in stats_map:
                        container['cpu'] = stats_map[container['id']]['cpu']
                        container['memory'] = stats_map[container['id']]['memory']
                    elif container['name'] in stats_map:
                        container['cpu'] = stats_map[container['name']]['cpu']
                        container['memory'] = stats_map[container['name']]['memory']
        
        return jsonify({'containers': containers})
        
    except Exception as e:
        log_system_event('error', f'Docker containers error: {str(e)}')
        return jsonify({'error': str(e)})

@app.route('/api/docker/images')
def docker_images():
    """List all Docker images"""
    try:
        result = run_docker_command([
            'images', '--format',
            '{{.ID}}|{{.Repository}}|{{.Tag}}|{{.Size}}'
        ])
        
        if result['returncode'] != 0:
            return jsonify({'error': result['stderr'] or 'Failed to list images'})
        
        images = []
        lines = result['stdout'].strip().split('\n') if result['stdout'].strip() else []
        
        for line in lines:
            if not line:
                continue
            parts = line.split('|')
            if len(parts) >= 4:
                images.append({
                    'id': parts[0],
                    'repository': parts[1],
                    'tag': parts[2],
                    'size': parts[3]
                })
        
        return jsonify({'images': images})
        
    except Exception as e:
        log_system_event('error', f'Docker images error: {str(e)}')
        return jsonify({'error': str(e)})

@app.route('/api/docker/container/start', methods=['POST'])
def docker_container_start():
    """Start a container"""
    try:
        data = request.json
        container_id = data.get('container_id')
        
        if not container_id:
            return jsonify({'status': 'error', 'message': 'No container ID provided'})
        
        result = run_docker_command(['start', container_id])
        
        if result['returncode'] == 0:
            log_system_event('info', f'Started container: {container_id}')
            return jsonify({'status': 'success'})
        else:
            return jsonify({'status': 'error', 'message': result['stderr']})
            
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/docker/container/stop', methods=['POST'])
def docker_container_stop():
    """Stop a container"""
    try:
        data = request.json
        container_id = data.get('container_id')
        
        if not container_id:
            return jsonify({'status': 'error', 'message': 'No container ID provided'})
        
        result = run_docker_command(['stop', container_id], timeout=30)
        
        if result['returncode'] == 0:
            log_system_event('info', f'Stopped container: {container_id}')
            return jsonify({'status': 'success'})
        else:
            return jsonify({'status': 'error', 'message': result['stderr']})
            
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/docker/container/restart', methods=['POST'])
def docker_container_restart():
    """Restart a container"""
    try:
        data = request.json
        container_id = data.get('container_id')
        
        if not container_id:
            return jsonify({'status': 'error', 'message': 'No container ID provided'})
        
        result = run_docker_command(['restart', container_id], timeout=60)
        
        if result['returncode'] == 0:
            log_system_event('info', f'Restarted container: {container_id}')
            return jsonify({'status': 'success'})
        else:
            return jsonify({'status': 'error', 'message': result['stderr']})
            
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/docker/container/remove', methods=['POST'])
def docker_container_remove():
    """Remove a container"""
    try:
        data = request.json
        container_id = data.get('container_id')
        force = data.get('force', False)
        
        if not container_id:
            return jsonify({'status': 'error', 'message': 'No container ID provided'})
        
        args = ['rm']
        if force:
            args.append('-f')
        args.append(container_id)
        
        result = run_docker_command(args)
        
        if result['returncode'] == 0:
            log_system_event('warning', f'Removed container: {container_id}')
            return jsonify({'status': 'success'})
        else:
            return jsonify({'status': 'error', 'message': result['stderr']})
            
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/docker/container/logs')
def docker_container_logs():
    """Get container logs"""
    try:
        container_id = request.args.get('container_id')
        lines = request.args.get('lines', '100')
        
        if not container_id:
            return jsonify({'error': 'No container ID provided'})
        
        result = run_docker_command(['logs', '--tail', lines, container_id], timeout=30)
        
        # Combine stdout and stderr for logs
        logs = result['stdout'] + result['stderr']
        
        return jsonify({'logs': logs})
        
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/docker/container/exec', methods=['POST'])
def docker_container_exec():
    """Execute command in container"""
    try:
        data = request.json
        container_id = data.get('container_id')
        command = data.get('command', 'sh')
        
        if not container_id:
            return jsonify({'error': 'No container ID provided'})
        
        # Split command if it contains spaces
        cmd_parts = command.split() if ' ' in command else [command]
        
        result = run_docker_command(['exec', container_id] + cmd_parts, timeout=30)
        
        output = result['stdout'] + result['stderr']
        
        return jsonify({'output': output, 'returncode': result['returncode']})
        
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/docker/container/create', methods=['POST'])
def docker_container_create():
    """Create and optionally start a new container"""
    try:
        data = request.json
        image = data.get('image')
        
        if not image:
            return jsonify({'status': 'error', 'message': 'No image specified'})
        
        # Build docker run command
        args = ['run']
        
        # Detached mode
        if data.get('detach', True):
            args.append('-d')
        
        # Remove when stopped
        if data.get('rm', False):
            args.append('--rm')
        
        # Container name
        if data.get('name'):
            args.extend(['--name', data['name']])
        
        # Port mappings
        if data.get('ports'):
            ports = data['ports'].split(',')
            for port in ports:
                port = port.strip()
                if port:
                    args.extend(['-p', port])
        
        # Volume mappings
        if data.get('volumes'):
            volumes = data['volumes'].split(',')
            for volume in volumes:
                volume = volume.strip()
                if volume:
                    args.extend(['-v', volume])
        
        # Environment variables
        if data.get('env'):
            env_vars = data['env'].strip().split('\n')
            for env_var in env_vars:
                env_var = env_var.strip()
                if env_var and '=' in env_var:
                    args.extend(['-e', env_var])
        
        # Network
        if data.get('network'):
            args.extend(['--network', data['network']])
        
        # Restart policy
        if data.get('restart'):
            args.extend(['--restart', data['restart']])
        
        # Image
        args.append(image)
        
        # Command
        if data.get('command'):
            args.extend(data['command'].split())
        
        result = run_docker_command(args, timeout=120)
        
        if result['returncode'] == 0:
            container_id = result['stdout'].strip()
            log_system_event('info', f'Created container from {image}: {container_id}')
            return jsonify({'status': 'success', 'container_id': container_id})
        else:
            return jsonify({'status': 'error', 'message': result['stderr']})
            
    except Exception as e:
        log_system_event('error', f'Container creation error: {str(e)}')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/docker/image/pull', methods=['POST'])
def docker_image_pull():
    """Pull a Docker image"""
    try:
        data = request.json
        image = data.get('image')
        
        if not image:
            return jsonify({'status': 'error', 'message': 'No image specified'})
        
        result = run_docker_command(['pull', image], timeout=300)  # 5 min timeout for large images
        
        if result['returncode'] == 0:
            log_system_event('info', f'Pulled image: {image}')
            return jsonify({'status': 'success', 'output': result['stdout']})
        else:
            return jsonify({'status': 'error', 'message': result['stderr']})
            
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/docker/image/remove', methods=['POST'])
def docker_image_remove():
    """Remove a Docker image"""
    try:
        data = request.json
        image_id = data.get('image_id')
        
        if not image_id:
            return jsonify({'status': 'error', 'message': 'No image ID provided'})
        
        result = run_docker_command(['rmi', image_id])
        
        if result['returncode'] == 0:
            log_system_event('warning', f'Removed image: {image_id}')
            return jsonify({'status': 'success'})
        else:
            return jsonify({'status': 'error', 'message': result['stderr']})
            
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

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
