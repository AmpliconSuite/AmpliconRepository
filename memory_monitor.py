#!/usr/bin/env python3
"""
Memory Leak Monitoring Script for CAPER Django Application

This script helps identify memory leaks by:
1. Monitoring Django process memory usage over time (RSS = physical memory)
2. Tracking Python object growth
3. Identifying top memory consumers
4. Logging memory snapshots before/after operations

Key Metrics:
- RSS (Resident Set Size): Physical RAM used by the process ← WATCH THIS FOR LEAKS
- VMS (Virtual Memory Size): Virtual address space (can be huge, includes mapped files)

Usage:
    python memory_monitor.py [--pid PID] [--interval SECONDS] [--output FILE]
"""

import argparse
import psutil
import time
import sys
import os
from datetime import datetime
import json

try:
    import objgraph
    HAS_OBJGRAPH = True
except ImportError:
    HAS_OBJGRAPH = False
    print("Warning: objgraph not installed. Install with: pip install objgraph")

try:
    from pympler import muppy, summary
    HAS_PYMPLER = True
except ImportError:
    HAS_PYMPLER = False
    print("Warning: pympler not installed. Install with: pip install pympler")


class MemoryMonitor:
    def __init__(self, pid=None, interval=5, output_file=None):
        self.pid = pid
        self.interval = interval
        self.output_file = output_file
        self.baseline_memory = None
        self.samples = []
        
    def find_django_process(self):
        """Find Django manage.py process"""
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info['cmdline']
                if cmdline and 'manage.py' in ' '.join(cmdline) and 'runserver' in ' '.join(cmdline):
                    return proc.info['pid']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None
    
    def get_process_info(self, pid):
        """Get detailed process information"""
        try:
            process = psutil.Process(pid)
            mem_info = process.memory_info()
            mem_percent = process.memory_percent()
            
            return {
                'timestamp': datetime.now().isoformat(),
                'rss_mb': mem_info.rss / 1024 / 1024,  # Resident Set Size
                'vms_mb': mem_info.vms / 1024 / 1024,  # Virtual Memory Size
                'percent': mem_percent,
                'num_threads': process.num_threads(),
                'num_fds': process.num_fds() if hasattr(process, 'num_fds') else None,
                'cpu_percent': process.cpu_percent(interval=0.1)
            }
        except psutil.NoSuchProcess:
            print(f"Process {pid} no longer exists")
            return None
        except psutil.AccessDenied:
            print(f"Access denied to process {pid}")
            return None
    
    def format_memory_mb(self, mb):
        """Format memory in MB"""
        if mb >= 1024:
            return f"{mb/1024:.2f} GB"
        return f"{mb:.2f} MB"
    
    def print_snapshot(self, info):
        """Print memory snapshot"""
        if info is None:
            return
            
        print(f"\n[{info['timestamp']}]")
        print(f"  RSS Memory (Physical): {self.format_memory_mb(info['rss_mb'])} ← Monitor this for leaks!")
        print(f"  VMS Memory (Virtual):  {self.format_memory_mb(info['vms_mb'])} (includes mapped files/libs)")
        print(f"  Memory %:   {info['percent']:.1f}%")
        print(f"  Threads:    {info['num_threads']}")
        if info['num_fds']:
            print(f"  File Descriptors: {info['num_fds']}")
        print(f"  CPU %:      {info['cpu_percent']:.1f}%")
        
        if self.baseline_memory:
            diff_mb = info['rss_mb'] - self.baseline_memory
            print(f"  RSS Change from baseline: {'+' if diff_mb > 0 else ''}{diff_mb:.2f} MB")
    
    def check_python_objects(self):
        """Check for Python object growth"""
        if HAS_OBJGRAPH:
            print("\n--- Top Python Object Types ---")
            objgraph.show_most_common_types(limit=15)
    
    def check_memory_summary(self):
        """Show memory summary using pympler"""
        if HAS_PYMPLER:
            print("\n--- Memory Summary (Top Objects) ---")
            all_objects = muppy.get_objects()
            sum_list = summary.summarize(all_objects)
            summary.print_(sum_list, limit=15)
    
    def save_to_file(self):
        """Save collected samples to file"""
        if self.output_file and self.samples:
            with open(self.output_file, 'w') as f:
                json.dump(self.samples, f, indent=2)
            print(f"\nSaved {len(self.samples)} samples to {self.output_file}")
    
    def monitor(self):
        """Main monitoring loop"""
        if self.pid is None:
            print("Searching for Django process...")
            self.pid = self.find_django_process()
            if self.pid is None:
                print("Error: Could not find Django manage.py process")
                print("Please start Django server or specify PID with --pid")
                return 1
        
        print(f"Monitoring process {self.pid}")
        print(f"Sampling every {self.interval} seconds")
        print("Press Ctrl+C to stop\n")
        
        try:
            # Get baseline
            baseline_info = self.get_process_info(self.pid)
            if baseline_info:
                self.baseline_memory = baseline_info['rss_mb']
                print("=== BASELINE ===")
                self.print_snapshot(baseline_info)
                self.samples.append(baseline_info)
            
            sample_count = 0
            while True:
                time.sleep(self.interval)
                sample_count += 1
                
                info = self.get_process_info(self.pid)
                if info is None:
                    break
                
                self.samples.append(info)
                print(f"\n=== SAMPLE {sample_count} ===")
                self.print_snapshot(info)
                
                # Show object growth every 10 samples
                if sample_count % 10 == 0:
                    self.check_python_objects()
                    self.check_memory_summary()
                
        except KeyboardInterrupt:
            print("\n\nStopping monitor...")
            self.save_to_file()
            self.print_summary()
        
        return 0
    
    def print_summary(self):
        """Print summary statistics"""
        if len(self.samples) < 2:
            return
        
        print("\n" + "="*60)
        print("SUMMARY")
        print("="*60)
        
        initial = self.samples[0]
        final = self.samples[-1]
        max_rss = max(s['rss_mb'] for s in self.samples)
        min_rss = min(s['rss_mb'] for s in self.samples)
        avg_rss = sum(s['rss_mb'] for s in self.samples) / len(self.samples)
        
        print(f"Duration: {len(self.samples) * self.interval} seconds ({len(self.samples)} samples)")
        print(f"\nMemory (RSS):")
        print(f"  Initial:  {self.format_memory_mb(initial['rss_mb'])}")
        print(f"  Final:    {self.format_memory_mb(final['rss_mb'])}")
        print(f"  Change:   {'+' if final['rss_mb'] > initial['rss_mb'] else ''}{final['rss_mb'] - initial['rss_mb']:.2f} MB")
        print(f"  Peak:     {self.format_memory_mb(max_rss)}")
        print(f"  Min:      {self.format_memory_mb(min_rss)}")
        print(f"  Average:  {self.format_memory_mb(avg_rss)}")
        
        print(f"\nThreads:")
        print(f"  Initial:  {initial['num_threads']}")
        print(f"  Final:    {final['num_threads']}")
        
        if initial['num_fds'] and final['num_fds']:
            print(f"\nFile Descriptors:")
            print(f"  Initial:  {initial['num_fds']}")
            print(f"  Final:    {final['num_fds']}")
            print(f"  Change:   {'+' if final['num_fds'] > initial['num_fds'] else ''}{final['num_fds'] - initial['num_fds']}")
        
        # Check for potential leak
        memory_growth = final['rss_mb'] - initial['rss_mb']
        if memory_growth > 100:  # More than 100MB growth
            print("\n⚠️  WARNING: Significant memory growth detected!")
            print(f"   Memory increased by {memory_growth:.2f} MB")
            print("   This may indicate a memory leak.")
        elif memory_growth > 50:
            print("\n⚠️  CAUTION: Moderate memory growth detected.")
            print(f"   Memory increased by {memory_growth:.2f} MB")
        else:
            print("\n✓  Memory growth appears normal.")


def main():
    parser = argparse.ArgumentParser(
        description='Monitor Django process memory usage for leak detection'
    )
    parser.add_argument(
        '--pid',
        type=int,
        help='Process ID to monitor (auto-detect if not specified)'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=5,
        help='Sampling interval in seconds (default: 5)'
    )
    parser.add_argument(
        '--output',
        type=str,
        help='Output file for JSON data (optional)'
    )
    
    args = parser.parse_args()
    
    monitor = MemoryMonitor(
        pid=args.pid,
        interval=args.interval,
        output_file=args.output
    )
    
    return monitor.monitor()


if __name__ == '__main__':
    sys.exit(main())

