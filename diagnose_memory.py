#!/usr/bin/env python3
"""
Quick diagnostic script to understand memory usage discrepancy
between memory_monitor.py and docker stats
"""

import psutil
import sys

def list_all_processes():
    """List all Python/Django processes running in the container"""
    print("="*70)
    print("ALL PYTHON/DJANGO PROCESSES")
    print("="*70)
    
    total_rss = 0
    total_vms = 0
    processes = []
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'memory_info', 'ppid']):
        try:
            cmdline = proc.info['cmdline']
            if cmdline:
                cmdline_str = ' '.join(cmdline)
                # Look for Python, manage.py, gunicorn, uwsgi, etc.
                if ('python' in cmdline_str.lower() or 
                    'manage.py' in cmdline_str or 
                    'gunicorn' in cmdline_str or 
                    'uwsgi' in cmdline_str):
                    
                    mem_info = proc.info['memory_info']
                    rss_mb = mem_info.rss / 1024 / 1024
                    vms_mb = mem_info.vms / 1024 / 1024
                    
                    processes.append({
                        'pid': proc.info['pid'],
                        'ppid': proc.info['ppid'],
                        'name': proc.info['name'],
                        'rss_mb': rss_mb,
                        'vms_mb': vms_mb,
                        'cmdline': ' '.join(cmdline[:3]) if len(cmdline) > 3 else ' '.join(cmdline)
                    })
                    
                    total_rss += rss_mb
                    total_vms += vms_mb
                    
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    
    # Print sorted by RSS
    processes.sort(key=lambda x: x['rss_mb'], reverse=True)
    
    print(f"\n{'PID':<8} {'PPID':<8} {'RSS (MB)':<12} {'VMS (GB)':<12} {'COMMAND'}")
    print("-"*70)
    
    for p in processes:
        print(f"{p['pid']:<8} {p['ppid']:<8} {p['rss_mb']:<12.2f} {p['vms_mb']/1024:<12.2f} {p['cmdline'][:50]}")
    
    print("-"*70)
    print(f"{'TOTAL':<8} {'':8} {total_rss:<12.2f} {total_vms/1024:<12.2f}")
    print()
    
    return len(processes), total_rss


def check_container_memory():
    """Check total container memory usage"""
    print("="*70)
    print("CONTAINER MEMORY (what docker stats shows)")
    print("="*70)
    
    mem = psutil.virtual_memory()
    
    print(f"Total memory available: {mem.total / 1024 / 1024:.2f} MB")
    print(f"Used memory:            {mem.used / 1024 / 1024:.2f} MB")
    print(f"Available memory:       {mem.available / 1024 / 1024:.2f} MB")
    print(f"Memory percent:         {mem.percent:.1f}%")
    print()


def check_cache_and_buffers():
    """Check for cached memory and buffers"""
    print("="*70)
    print("SYSTEM MEMORY BREAKDOWN")
    print("="*70)
    
    try:
        # This works on Linux
        with open('/proc/meminfo', 'r') as f:
            meminfo = {}
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip().split()[0]
                    meminfo[key] = int(value) / 1024  # Convert to MB
        
        print(f"Total:       {meminfo.get('MemTotal', 0):.2f} MB")
        print(f"Free:        {meminfo.get('MemFree', 0):.2f} MB")
        print(f"Buffers:     {meminfo.get('Buffers', 0):.2f} MB")
        print(f"Cached:      {meminfo.get('Cached', 0):.2f} MB")
        print(f"Active:      {meminfo.get('Active', 0):.2f} MB")
        print(f"Inactive:    {meminfo.get('Inactive', 0):.2f} MB")
        print()
    except FileNotFoundError:
        print("Not running on Linux, /proc/meminfo not available")
        print()


def main():
    print("\n" + "="*70)
    print("MEMORY DIAGNOSTIC TOOL")
    print("="*70)
    print()
    
    # Check container-level memory
    check_container_memory()
    
    # Check system memory breakdown
    check_cache_and_buffers()
    
    # List all Python processes
    num_processes, total_rss = list_all_processes()
    
    # Summary
    print("="*70)
    print("DIAGNOSIS")
    print("="*70)
    print()
    print(f"Found {num_processes} Python/Django process(es)")
    print(f"Total RSS (actual memory used by processes): {total_rss:.2f} MB")
    print()
    print("POSSIBLE CAUSES OF DISCREPANCY:")
    print()
    print("1. MULTIPLE PROCESSES:")
    print("   - If you have multiple worker processes, memory_monitor.py")
    print("     might only be tracking ONE of them")
    print("   - Solution: Use 'python memory_monitor.py --all' to track all")
    print()
    print("2. SHARED MEMORY & CACHE:")
    print("   - Docker stats includes page cache, buffers, and shared memory")
    print("   - RSS only counts private memory per process")
    print("   - Some memory might be counted multiple times in docker stats")
    print()
    print("3. CHILD PROCESSES:")
    print("   - Django might spawn child processes (celery, etc.)")
    print("   - Check PPID column above to see process relationships")
    print()
    print("4. MEMORY-MAPPED FILES:")
    print("   - Database files, static files might be memory-mapped")
    print("   - These count toward container memory but not process RSS")
    print()
    
    # Recommendation
    print("="*70)
    print("RECOMMENDATION")
    print("="*70)
    print()
    if num_processes > 1:
        print("⚠️  You have MULTIPLE Python processes!")
        print(f"   Total RSS from all processes: {total_rss:.2f} MB")
        print()
        print("   Run this command to track ALL processes:")
        print("   python memory_monitor.py --all")
    else:
        print("✓  Only 1 Python process found")
        print("   The discrepancy is likely due to:")
        print("   - Page cache and buffers")
        print("   - Shared libraries counted in container stats")
        print("   - Memory-mapped files")
    print()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

